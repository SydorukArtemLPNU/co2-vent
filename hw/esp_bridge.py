"""Емулятор ESP8266: міст UART (рядки Uno) <-> Wi-Fi/MQTT (JSON-контракт курсової).

Це модель прошивки ESP, а не емуляція чипа. Обмеження взято з реальних бібліотек,
щоб проблеми проявилися вже в емуляції:
  - MQTT-клієнт як arduino-mqtt/lwmqtt: буфер пакета MQTT_BUFFER байт (MQTTClient(512)),
    QoS 1 з одним повідомленням у польоті, clean session, без власної черги бібліотеки.
    (PubSubClient за замовчуванням має буфер 256 Б і публікує лише QoS 0 - для нашого
    JSON ~250 Б він не підходить без setBufferSize.)
  - власна черга на час розриву Wi-Fi/MQTT - OFFLINE_QUEUE повідомлень (RAM ESP),
    при переповненні відкидається найстаріше;
  - годинник лише після NTP: до синхронізації вимірювання відкидаються (немає observed_at);
  - пам'ять виконаних command_id - DEDUP останніх (у RAM, губиться після скидання ESP).
Час observed_at ставить ESP у момент отримання рядка S від Uno (у Uno немає RTC).
"""
import argparse
import logging
import os
import threading
import time
from collections import Counter, OrderedDict, deque

import config
from contract import (ContractError, decode, encode, make_ack, make_event, make_state,
                      utc_now, validate_command)

log = logging.getLogger("esp")

MQTT_BUFFER = 512
OFFLINE_QUEUE = 16
DEDUP = 16
LINE_MAX = 64
UNO_ACK_TIMEOUT_S = 1.0


def publish_packet_size(topic, payload, qos=1):
    """Розмір MQTT 3.1.1 PUBLISH: заголовок + довжина + тема + packet id + payload."""
    body = 2 + len(topic.encode()) + (2 if qos else 0) + len(payload.encode())
    length_bytes = 1 if body < 128 else 2 if body < 16384 else 3
    return 1 + length_bytes + body


class EspNode:
    def __init__(self, uart_write, mqtt_publish, now=utc_now, wall=time.monotonic, device_id=None,
                 mqtt_buffer=MQTT_BUFFER):
        self.uart_write = uart_write
        self.mqtt_publish = mqtt_publish
        self.now = now
        self.wall = wall
        self.device_id = device_id or config.DEVICE_ID
        self.mqtt_buffer = mqtt_buffer
        self.ntp_synced = False
        self.mqtt_up = False
        self.queue = deque()
        self.pending = {}                    # command_id -> (дедлайн відповіді Uno, команда)
        self.results = OrderedDict()         # command_id -> ACK (для повторів)
        self.report_due = None               # причина звіту стану, що чекає NTP/MQTT
        self.report_state = "off"
        self.uno_uptime = None               # з останнього H; None - Uno ще не чули
        self.stats = Counter()

    # --- вихід у MQTT ---
    def _publish(self, kind, message):
        topic = config.topic(kind, self.device_id)
        payload = encode(message)
        size = publish_packet_size(topic, payload)
        if size > self.mqtt_buffer:
            self.stats["oversize"] += 1
            log.error("DROP oversize packet %d > %d bytes topic=%s", size, self.mqtt_buffer, topic)
            return False
        if self.mqtt_up:
            self.mqtt_publish(topic, payload)
            self.stats["published_" + kind] += 1
            return True
        if len(self.queue) == OFFLINE_QUEUE:
            self.queue.popleft()
            self.stats["queue_dropped"] += 1
            log.warning("QUEUE full (%d) - oldest message dropped", OFFLINE_QUEUE)
        self.queue.append((kind, topic, payload))
        self.stats["queued"] += 1
        return True

    def on_mqtt_connected(self):
        self.mqtt_up = True
        flushed = 0
        while self.queue and self.mqtt_up:
            kind, topic, payload = self.queue.popleft()
            self.mqtt_publish(topic, payload)
            self.stats["published_" + kind] += 1
            flushed += 1
        log.info("MQTT up, flushed %d queued messages", flushed)
        self._maybe_boot_report()

    def on_mqtt_disconnected(self):
        self.mqtt_up = False

    def on_ntp_synced(self):
        self.ntp_synced = True
        log.info("NTP synced")
        self._maybe_boot_report()

    def _schedule_report(self, reason, ventilation):
        self.report_due, self.report_state = reason, ventilation
        self._maybe_boot_report()

    def _maybe_boot_report(self):
        if self.report_due and self.ntp_synced:
            reason, self.report_due = self.report_due, None
            self._publish("state", make_state(self.device_id, self.report_state, reason, self.now()))

    # --- вхід з UART ---
    def on_uart_line(self, text):
        if len(text) > LINE_MAX:
            self.stats["bad_line"] += 1
            return
        parts = text.split(",")
        kind = parts[0]
        if kind == "B":
            log.warning("UNO boot %s - relay is off after reset", text)
            self.pending.clear()
            self.uno_uptime = 0
            self._schedule_report("node_boot", "off")      # setup() вимикає реле
        elif kind == "H" and len(parts) == 3:
            self._heartbeat(parts[1], parts[2])
        elif kind == "I":
            log.info("UNO sensor init %s", text)
        elif kind == "S" and len(parts) == 6:
            self._measurement(parts)
        elif kind == "E":
            self.stats["sensor_error"] += 1
            log.warning("UNO sensor error %s", text)
        elif kind == "A" and len(parts) == 4:
            self._uno_ack(parts[1], parts[3])
        elif kind == "X":
            self.stats["uno_rejected_line"] += 1
            log.warning("UNO rejected line: %s", text)
        else:
            self.stats["bad_line"] += 1
            log.warning("UART garbage: %r", text)

    def _heartbeat(self, uptime_text, state_text):
        try:
            uptime = int(uptime_text)
        except ValueError:
            self.stats["bad_line"] += 1
            return
        ventilation = "on" if state_text == "1" else "off"
        if self.uno_uptime is None:
            log.info("UNO first heartbeat uptime=%ss relay=%s - sync state", uptime, ventilation)
            self._schedule_report("node_sync", ventilation)
        elif uptime < self.uno_uptime:
            log.warning("UNO uptime went back %s -> %s: missed boot line", self.uno_uptime, uptime)
            self.pending.clear()
            self._schedule_report("node_boot", ventilation)
        self.uno_uptime = uptime

    def _measurement(self, parts):
        try:
            channel, co2 = int(parts[2]), int(parts[3])
        except ValueError:
            self.stats["bad_line"] += 1
            return
        if not 0 <= channel < len(config.SENSORS):
            self.stats["bad_line"] += 1
            return
        if not self.ntp_synced:
            self.stats["dropped_no_time"] += 1
            return
        event = make_event(config.SENSORS[channel], float(co2), self.now(), device_id=self.device_id)
        self._publish("telemetry", event)

    def _uno_ack(self, command_id, state):
        entry = self.pending.pop(command_id, None)
        if entry is None:
            self.stats["uno_ack_unexpected"] += 1
            log.warning("UNO ack for unknown/late command %s", command_id)
            return
        ack = make_ack(command_id, self.device_id, "applied", "on" if state == "1" else "off", self.now())
        self._remember(command_id, ack)
        self._publish("ack", ack)

    def _remember(self, command_id, ack):
        self.results[command_id] = ack
        while len(self.results) > DEDUP:
            self.results.popitem(last=False)

    # --- команди з MQTT ---
    def on_command(self, topic, payload):
        try:
            command = decode(payload)
            _, expires = validate_command(command, topic)
        except ContractError as exc:
            self.stats["bad_command"] += 1
            log.warning("COMMAND invalid: %s", exc)
            return
        cid = command["command_id"]
        if command["device_id"] != self.device_id:
            return
        if cid in self.results:
            self.stats["duplicate_command"] += 1
            log.info("COMMAND duplicate %s - repeat stored ack", cid)
            self._publish("ack", self.results[cid])
            return
        if cid in self.pending:
            return                                   # уже передано Uno, чекаємо A
        if not self.ntp_synced or self.now() > expires:
            reason = "expired" if self.ntp_synced else "clock not synced"
            ack = make_ack(cid, self.device_id, "rejected", "off", self.now(), reason)
            log.warning("COMMAND %s rejected: %s", cid, reason)
            self._remember(cid, ack)
            self._publish("ack", ack)
            return
        bit = "1" if command["action"] == "ventilation_on" else "0"
        self.pending[cid] = (self.wall() + UNO_ACK_TIMEOUT_S / config.TIME_SCALE, command)
        self.uart_write(f"C,{cid},{bit}\n")
        log.info("COMMAND %s -> UART C,%s,%s", command["action"], cid, bit)

    def tick(self):
        now = self.wall()
        for cid, (deadline, command) in list(self.pending.items()):
            if now > deadline:
                del self.pending[cid]
                self.stats["uno_timeout"] += 1
                log.error("UNO no response for %s - no ack sent", cid)


def main():
    import paho.mqtt.client as mqtt
    import serial

    parser = argparse.ArgumentParser(description="Емулятор ESP8266 (UART <-> MQTT)")
    parser.add_argument("--port", default="/tmp/simavr-uart0")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--wifi-connect", type=float, default=2.0, help="час підключення Wi-Fi, с")
    parser.add_argument("--ntp-delay", type=float, default=1.0, help="час до синхронізації NTP, с")
    parser.add_argument("--wifi-drop", default="", help="<через с>:<на с> - обрив Wi-Fi (можна через кому)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    scale = config.TIME_SCALE

    while not os.path.exists(args.port):
        time.sleep(0.1)
    uart = serial.Serial(args.port, args.baud, timeout=0.02)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"esp8266-{config.DEVICE_ID}",
                         clean_session=True)
    client.max_inflight_messages_set(1)
    if config.BROKER_USER:
        client.username_pw_set(config.BROKER_USER, config.BROKER_PASSWORD)
    node = EspNode(lambda text: uart.write(text.encode()),
                   lambda topic, payload: client.publish(topic, payload, qos=1))

    lock = threading.Lock()          # колбеки paho і основний цикл - як один потік loop() ESP

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(config.topic("command"), qos=1)
        with lock:
            node.on_mqtt_connected()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        with lock:
            node.on_mqtt_disconnected()
        log.warning("MQTT down rc=%s", reason_code)

    def on_message(client, userdata, msg):
        with lock:
            node.on_command(msg.topic, msg.payload)

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(1, 5)

    drops = []
    for spec in filter(None, args.wifi_drop.split(",")):
        at, length = (float(x) for x in spec.split(":"))
        drops.append((at, at + length))
    start = time.monotonic()
    wifi_up_at = start + args.wifi_connect / scale
    ntp_at = wifi_up_at + args.ntp_delay / scale
    wifi = "connecting"
    buffer = b""
    log.info("ESP8266 model: buffer=%d queue=%d dedup=%d port=%s", MQTT_BUFFER, OFFLINE_QUEUE, DEDUP, args.port)
    while True:
        now = time.monotonic()
        elapsed = now - start
        dropped = any(a <= elapsed < b for a, b in drops)
        if wifi == "connecting" and now >= wifi_up_at and not dropped:
            wifi = "up"
            log.info("WIFI connected")
            client.connect_async(config.BROKER_HOST, config.BROKER_PORT, keepalive=15)
            client.loop_start()
        elif wifi == "up" and dropped:
            wifi = "down"
            log.warning("WIFI lost (emulated) stats=%s", dict(node.stats))
            client.disconnect()
            client.loop_stop()
            with lock:
                node.on_mqtt_disconnected()
        elif wifi == "down" and not dropped:
            wifi = "up"
            log.info("WIFI restored")
            client.connect_async(config.BROKER_HOST, config.BROKER_PORT, keepalive=15)
            client.loop_start()
        chunk = uart.read(256)
        with lock:
            if not node.ntp_synced and wifi == "up" and now >= ntp_at:
                node.on_ntp_synced()
            if chunk:
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    node.on_uart_line(raw.decode("ascii", "replace").strip())
                if len(buffer) > LINE_MAX:      # рядок без '\n' - відкинути
                    buffer = b""
                    node.stats["bad_line"] += 1
            node.tick()


if __name__ == "__main__":
    main()
