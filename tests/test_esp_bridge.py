"""Модель прошивки ESP8266: UART-рядки Uno -> MQTT і назад, з обмеженнями реальних бібліотек."""
import json
import unittest

import config
from contract import encode, make_command, validate_event
from helpers import Clock
from hw.esp_bridge import OFFLINE_QUEUE, EspNode, publish_packet_size

COMMAND = config.topic("command")


class Wall:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class EspTest(unittest.TestCase):
    def setUp(self):
        self.clock, self.wall = Clock(), Wall()
        self.uart, self.out = [], []
        self.node = EspNode(self.uart.append, lambda t, p: self.out.append((t, json.loads(p))),
                            now=self.clock, wall=self.wall)

    def online(self):
        self.node.on_ntp_synced()
        self.node.on_mqtt_connected()

    def kinds(self):
        return [topic.rsplit("/", 1)[1] for topic, _ in self.out]

    def test_measurement_becomes_valid_event(self):
        self.online()
        self.node.on_uart_line("S,7,1,1012,2249,4499")
        topic, event = self.out[0]
        self.assertEqual(topic, config.topic("telemetry"))
        self.assertEqual((event["sensor_id"], event["value"], event["source"]), ("scd40-2", 1012.0, "simulated"))
        validate_event(event, topic, self.clock())       # той самий контракт, що й у симулятора

    def test_no_ntp_no_telemetry(self):
        self.node.on_mqtt_connected()
        self.node.on_uart_line("S,1,0,700,2249,4499")
        self.assertEqual((self.out, self.node.stats["dropped_no_time"]), ([], 1))

    def test_packet_fits_lwmqtt_buffer_but_not_pubsubclient_default(self):
        self.online()
        self.node.on_uart_line("S,1,0,1012,2249,4499")
        topic, event = self.out[0]
        size = publish_packet_size(topic, encode(event))
        self.assertLessEqual(size, 512)
        self.assertGreater(size, 256)                     # PubSubClient за замовчуванням - 256 Б
        small = EspNode(self.uart.append, lambda t, p: None, now=self.clock, mqtt_buffer=256)
        small.on_ntp_synced()
        small.on_mqtt_connected()
        small.on_uart_line("S,1,0,1012,2249,4499")
        self.assertEqual(small.stats["oversize"], 1)

    def test_offline_queue_bounded_and_flushed(self):
        self.node.on_ntp_synced()
        for seq in range(OFFLINE_QUEUE + 4):
            self.node.on_uart_line(f"S,{seq},0,{700 + seq},2249,4499")
        self.assertEqual(self.node.stats["queue_dropped"], 4)
        self.node.on_mqtt_connected()
        values = [event["value"] for _, event in self.out]
        self.assertEqual(values[0], 704.0)                # найстаріші 4 відкинуто
        self.assertEqual(len(values), OFFLINE_QUEUE)

    def test_command_roundtrip_ack_after_uno(self):
        self.online()
        command = make_command(config.DEVICE_ID, "ventilation_on", self.clock(), ttl_s=10)
        self.node.on_command(COMMAND, encode(command).encode())
        self.assertEqual(self.uart, [f"C,{command['command_id']},1\n"])
        self.assertEqual(self.out, [])                    # ACK лише після відповіді Uno
        self.node.on_uart_line(f"A,{command['command_id']},applied,1")
        _, ack = self.out[0]
        self.assertEqual((ack["status"], ack["ventilation"]), ("applied", "on"))

    def test_duplicate_command_not_sent_to_uno_again(self):
        self.online()
        command = make_command(config.DEVICE_ID, "ventilation_off", self.clock(), ttl_s=10)
        payload = encode(command).encode()
        self.node.on_command(COMMAND, payload)
        self.node.on_uart_line(f"A,{command['command_id']},applied,0")
        self.node.on_command(COMMAND, payload)
        self.assertEqual(len(self.uart), 1)
        self.assertEqual(self.kinds(), ["ack", "ack"])

    def test_expired_command_rejected_without_uart(self):
        self.online()
        command = make_command(config.DEVICE_ID, "ventilation_on", self.clock(), ttl_s=10)
        self.clock.advance(11)
        self.node.on_command(COMMAND, encode(command).encode())
        self.assertEqual(self.uart, [])
        self.assertEqual(self.out[0][1]["reason"], "expired")

    def test_uno_silent_no_ack(self):
        self.online()
        command = make_command(config.DEVICE_ID, "ventilation_on", self.clock(), ttl_s=10)
        self.node.on_command(COMMAND, encode(command).encode())
        self.wall.value += 5
        self.node.tick()
        self.node.on_uart_line(f"A,{command['command_id']},applied,1")   # запізно
        self.assertEqual(self.out, [])
        self.assertEqual(self.node.stats["uno_timeout"], 1)

    def test_boot_line_reports_off(self):
        self.online()
        self.node.on_uart_line("B,uno_node,2")
        _, report = self.out[0]
        self.assertEqual((report["reason"], report["ventilation"]), ("node_boot", "off"))

    def test_missed_boot_detected_by_uptime(self):
        self.online()
        self.node.on_uart_line("H,50,1")                  # перше «знайомство» після старту ESP
        self.node.on_uart_line("H,60,1")
        self.node.on_uart_line("H,3,0")                   # uptime зменшився: Uno скинувся, B загублено
        reasons = [message["reason"] for topic, message in self.out]
        self.assertEqual(reasons, ["node_sync", "node_boot"])

    def test_garbage_lines_counted(self):
        self.online()
        for line in ("\x00\xff", "S,1,9,700,1,1", "S,x,0,abc,1,1", "Q" * 80):
            self.node.on_uart_line(line)
        self.assertEqual((self.out, self.node.stats["bad_line"]), ([], 4))


if __name__ == "__main__":
    unittest.main()
