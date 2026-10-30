"""Контракт телеметрії, команди й ACK."""
import json
import math
import unittest
from datetime import timedelta

import config
from contract import (ContractError, decode, make_ack, make_command, make_event,
                      validate_ack, validate_command, validate_event)
from helpers import ACK, T0, TELEMETRY

COMMAND = config.topic("command")


class Telemetry(unittest.TestCase):
    def check(self, reason, **overrides):
        event = make_event("scd40-1", 950.0, T0)
        event.update(overrides)
        with self.assertRaises(ContractError) as ctx:
            validate_event(event, TELEMETRY, T0)
        self.assertIn(reason, str(ctx.exception))

    def test_valid_event(self):
        event = make_event("scd40-2", 1012.5, T0)
        self.assertEqual(validate_event(event, TELEMETRY, T0), T0)

    def test_t5_value_types(self):
        for bad in ("950", True, None, math.nan, math.inf):
            with self.subTest(value=bad):
                self.check("not a number", value=bad)

    def test_t5_unit_metric_source(self):
        self.check("unknown unit", unit="ppb")
        self.check("unknown metric", metric="temperature")
        self.check("unknown source", source="manual")

    def test_range(self):
        self.check("out of range", value=-1)
        self.check("out of range", value=40000.1)

    def test_t6_topic_device_mismatch(self):
        self.check("mismatch", device_id="class-99")

    def test_unknown_sensor(self):
        self.check("unknown sensor_id", sensor_id="scd40-4")

    def test_fields_and_schema(self):
        event = make_event("scd40-1", 900.0, T0)
        del event["unit"]
        with self.assertRaisesRegex(ContractError, "missing field: unit"):
            validate_event(event, TELEMETRY, T0)
        self.check("unknown field", extra=1)
        self.check("schema_version", schema_version=2)
        self.check("event_id", event_id="bad id with spaces")

    def test_t9_stale_and_future(self):
        old = T0 - timedelta(seconds=config.MAX_AGE_S + 1)
        self.check("stale", observed_at=old.isoformat())
        future = T0 + timedelta(seconds=config.MAX_FUTURE_S + 1)
        self.check("future", observed_at=future.isoformat())
        self.check("without timezone", observed_at="2026-12-01T09:00:00")

    def test_payload_limits(self):
        with self.assertRaisesRegex(ContractError, "too large"):
            decode(b"{" + b" " * config.MAX_PAYLOAD + b"}")
        with self.assertRaisesRegex(ContractError, "invalid json"):
            decode(b"{not json")
        with self.assertRaisesRegex(ContractError, "not an object"):
            decode(json.dumps([1, 2]).encode())


class CommandAndAck(unittest.TestCase):
    def test_command_roundtrip(self):
        command = make_command(config.DEVICE_ID, "ventilation_on", T0, ttl_s=10)
        issued, expires = validate_command(command, COMMAND)
        self.assertEqual(expires - issued, timedelta(seconds=10))

    def test_command_rejects_foreign_device_and_action(self):
        command = make_command("class-99", "ventilation_on", T0)
        with self.assertRaisesRegex(ContractError, "mismatch"):
            validate_command(command, COMMAND)
        command = make_command(config.DEVICE_ID, "open_window", T0)
        with self.assertRaisesRegex(ContractError, "unknown action"):
            validate_command(command, COMMAND)

    def test_ack(self):
        ack = make_ack("cmd-1", config.DEVICE_ID, "applied", "on", T0)
        self.assertEqual(validate_ack(ack, ACK), T0)
        ack["status"] = "done"
        with self.assertRaisesRegex(ContractError, "ack status"):
            validate_ack(ack, ACK)


if __name__ == "__main__":
    unittest.main()
