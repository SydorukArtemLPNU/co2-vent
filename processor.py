"""Обробник: MQTT -> валідація -> SQLite -> правило -> команда -> облік ACK.

Логіка зібрана в класі Processor без залежності від мережі: publish і годинник
передаються ззовні, тому тести викликають ті самі методи, що й MQTT-клієнт.

Узгодження (reconcile): правило змінює лише бажаний стан вентиляції. Якщо
підтверджений ACK-ом стан виконавця відрізняється від бажаного (або невідомий)
і немає відкритої команди, видається нова команда. Так один механізм покриває
перше ввімкнення, повтор після тайм-ауту і відновлення після перезапуску.
"""
import argparse
import logging
import threading
import time

import config
import storage
from contract import (ContractError, decode, encode, iso, make_command, parse_time,
                      utc_now, validate_ack, validate_event, validate_state)
from policy import advance, aggregate

log = logging.getLogger("processor")


class Processor:
    def __init__(self, db, publish, now=utc_now, device_id=None):
        self.db = db
        self.publish = publish
        self.now = now
        self.device_id = device_id or config.DEVICE_ID
        self.lock = threading.Lock()
        self.state = storage.load_state(db, self.device_id)   # відновлення після рестарту

    # --- телеметрія ------------------------------------------------------

    def handle_telemetry(self, topic, payload):
        with self.lock:
            return self._telemetry(topic, payload)

    def _reject(self, topic, payload, reason, event_id=None):
        storage.record_rejection(self.db, self.now(), topic, reason, payload, event_id)
        log.warning("REJECT topic=%s event_id=%s reason=%s", topic, event_id, reason)
        return {"result": "rejected", "reason": reason, "event_id": event_id}

    def _telemetry(self, topic, payload):
        received = self.now()
        try:
            event = decode(payload)
        except ContractError as exc:
            return self._reject(topic, payload, str(exc))
        event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else None
        # дубль перевіряємо першим: повторна доставка QoS 1 не має ставати "stale"
        if event_id and storage.event_exists(self.db, event_id):
            return self._reject(topic, payload, "duplicate event_id", event_id)
        try:
            observed = validate_event(event, topic, received)
        except ContractError as exc:
            return self._reject(topic, payload, str(exc), event_id)
        if event["device_id"] != self.device_id:
            return self._reject(topic, payload, "unknown device", event_id)

        latest = storage.latest_by_sensor(self.db, self.device_id)
        previous = latest.get(event["sensor_id"])
        if previous and observed <= previous[0]:
            return self._reject(topic, payload, "out of order for sensor", event_id)
        if not storage.insert_event_once(self.db, event, observed, received):
            return self._reject(topic, payload, "duplicate event_id", event_id)

        latest[event["sensor_id"]] = (observed, float(event["value"]))
        median = aggregate(latest, observed)
        storage.set_event_median(self.db, event_id, median)
        self.state, action = advance(self.state, observed, median)
        storage.save_state(self.db, self.device_id, self.state, received)
        log.info("EVENT event_id=%s sensor=%s value=%s median=%s desired=%s high_since=%s",
                 event_id, event["sensor_id"], event["value"], median,
                 "on" if self.state.on else "off",
                 iso(self.state.high_since) if self.state.high_since else "-")
        if action:
            log.info("RULE %s (trigger event_id=%s, median=%s)", action, event_id, median)
        command = self._reconcile(event_id)
        return {"result": "stored", "event_id": event_id, "median": median,
                "action": action, "command": command}

    # --- команди ---------------------------------------------------------

    def _reconcile(self, trigger_event_id=None):
        desired = "on" if self.state.on else "off"
        actuator = storage.get_actuator(self.db, self.device_id)
        if actuator and actuator["ventilation"] == desired:
            return None
        if storage.open_commands(self.db, self.device_id):
            return None                      # чекаємо ACK або тайм-аут попередньої
        command = make_command(self.device_id, f"ventilation_{desired}", self.now())
        storage.save_command(self.db, command, "created", trigger_event_id)
        self._send(command)
        return command["command_id"]

    def _send(self, command):
        # запис у БД і publish не є однією транзакцією: команда, що лишилась
        # created після збою, дообробляється в recover()
        self.publish(config.topic("command", command["device_id"]), encode(command))
        storage.set_command_status(self.db, command["command_id"], "sent", from_statuses=("created",))
        log.info("COMMAND sent command_id=%s action=%s expires_at=%s",
                 command["command_id"], command["action"], command["expires_at"])

    def handle_ack(self, topic, payload):
        with self.lock:
            return self._ack(topic, payload)

    def _ack(self, topic, payload):
        received = self.now()
        try:
            ack = decode(payload)
            ack_at = validate_ack(ack, topic)
        except ContractError as exc:
            return self._reject(topic, payload, "ack: " + str(exc))
        command = storage.get_command(self.db, ack["command_id"])
        if command is None or command["device_id"] != ack["device_id"]:
            return self._reject(topic, payload, "ack: unknown command_id")
        cid, stamp = ack["command_id"], iso(ack_at)

        if command["status"] in ("applied", "rejected"):
            log.info("ACK duplicate command_id=%s (status already %s)", cid, command["status"])
            return {"result": "duplicate", "command_id": cid}
        late = received > parse_time(command["expires_at"])
        if ack["status"] == "rejected":
            if not late:
                storage.set_command_status(self.db, cid, "rejected", from_statuses=("created", "sent"),
                                           ack_at=stamp, reason=ack.get("reason"))
            log.warning("ACK rejected command_id=%s reason=%s", cid, ack.get("reason"))
            return {"result": "rejected", "command_id": cid}
        if late:
            # після дедлайну команда лишається expired, але факт стану виконавця врахуємо
            storage.set_command_status(self.db, cid, "expired", from_statuses=("created", "sent"))
            storage.note_command(self.db, cid, "late ack after deadline", stamp)
            storage.set_actuator(self.db, self.device_id, ack["ventilation"], cid, stamp)
            log.warning("ACK late command_id=%s ventilation=%s - status stays expired", cid, ack["ventilation"])
            self._reconcile()
            return {"result": "late", "command_id": cid}
        storage.set_command_status(self.db, cid, "applied", from_statuses=("created", "sent"), ack_at=stamp)
        storage.set_actuator(self.db, self.device_id, ack["ventilation"], cid, stamp)
        log.info("ACK applied command_id=%s ventilation=%s", cid, ack["ventilation"])
        self._reconcile()        # бажаний стан міг змінитися, поки чекали ACK
        return {"result": "applied", "command_id": cid}

    def handle_state(self, topic, payload):
        with self.lock:
            return self._state(topic, payload)

    def _state(self, topic, payload):
        """Звіт вузла (node_boot): фактичний стан виконавця відомий без команди -
        після скидання Uno реле вимкнене, і бажаний стан треба відновити."""
        try:
            report = decode(payload)
            reported = validate_state(report, topic)
        except ContractError as exc:
            return self._reject(topic, payload, "state: " + str(exc))
        if report["device_id"] != self.device_id:
            return self._reject(topic, payload, "state: unknown device")
        stamp = iso(reported)
        storage.set_actuator(self.db, self.device_id, report["ventilation"],
                             "report:" + report["reason"], stamp)
        log.warning("STATE %s ventilation=%s at %s", report["reason"], report["ventilation"], stamp)
        return {"result": "state", "command": self._reconcile()}

    def sweep(self):
        """Тайм-аут: відкриті команди після expires_at -> expired."""
        with self.lock:
            now = self.now()
            expired = []
            for command in storage.open_commands(self.db, self.device_id):
                if now > parse_time(command["expires_at"]):
                    if storage.set_command_status(self.db, command["command_id"], "expired",
                                                  from_statuses=("created", "sent"),
                                                  reason="no ack before deadline"):
                        expired.append(command["command_id"])
                        log.warning("TIMEOUT command_id=%s action=%s -> expired, ventilation unknown",
                                    command["command_id"], command["action"])
            return expired

    def recover(self):
        """Після рестарту: дослати created-команди, ще не прострочені."""
        self.sweep()
        with self.lock:
            for command in storage.open_commands(self.db, self.device_id):
                if command["status"] == "created":
                    log.info("RECOVER resend created command_id=%s", command["command_id"])
                    self._send({key: command[key] for key in
                                ("command_id", "device_id", "action", "issued_at", "expires_at")})
        log.info("RECOVER state desired=%s high_since=%s last_at=%s",
                 "on" if self.state.on else "off", self.state.high_since, self.state.last_at)


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="Обробник телеметрії CO2 (варіант 20)")
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    db = storage.connect(args.db)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="co2vent-processor",
                         clean_session=False)    # брокер зберігає QoS 1 на час нашої відсутності
    if config.BROKER_USER:
        client.username_pw_set(config.BROKER_USER, config.BROKER_PASSWORD)

    def publish(topic, payload):
        client.publish(topic, payload, qos=1)

    processor = Processor(db, publish)

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("MQTT connected rc=%s", reason_code)
        # підписка в on_connect - відновлюється після кожного перепідключення
        client.subscribe([(f"{config.TOPIC_PREFIX}/+/{kind}", 1) for kind in ("telemetry", "ack", "state")])

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected rc=%s - reconnecting", reason_code)

    def on_message(client, userdata, msg):
        if msg.topic.endswith("/telemetry"):
            processor.handle_telemetry(msg.topic, msg.payload)
        elif msg.topic.endswith("/ack"):
            processor.handle_ack(msg.topic, msg.payload)
        elif msg.topic.endswith("/state"):
            processor.handle_state(msg.topic, msg.payload)

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(1, 5)
    processor.recover()
    log.info("time_scale=%s hold=%.1fs ttl=%.1fs db=%s", config.TIME_SCALE, config.HOLD_S,
             config.COMMAND_TTL_S, args.db)
    client.connect_async(config.BROKER_HOST, config.BROKER_PORT)
    client.loop_start()
    try:
        while True:
            processor.sweep()
            time.sleep(config.SWEEP_PERIOD_S)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
