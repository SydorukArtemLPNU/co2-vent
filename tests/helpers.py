"""Спільні засоби тестів: керований годинник, обробник із фіктивним publish."""
import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import storage
from contract import encode, make_ack, make_event
from processor import Processor

logging.disable(logging.CRITICAL)      # журнали обробника не потрібні у виводі тестів

T0 = datetime(2026, 12, 1, 9, 0, 0, tzinfo=timezone.utc)
TELEMETRY = config.topic("telemetry")
ACK = config.topic("ack")


class Clock:
    def __init__(self, start=T0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Bench:
    """Обробник на тимчасовій БД; опубліковані команди накопичуються в sent."""

    def __init__(self, db_path=None):
        self.tmp = None
        if db_path is None:
            self.tmp = tempfile.TemporaryDirectory()
            db_path = str(Path(self.tmp.name) / "test.db")
        self.db_path = db_path
        self.clock = Clock()
        self.sent = []
        self.db = storage.connect(db_path)
        self.processor = Processor(self.db, self.publish, now=self.clock)

    def publish(self, topic, payload):
        self.sent.append((topic, json.loads(payload)))

    def restart(self):
        """Новий екземпляр обробника на тій самій БД (модель перезапуску)."""
        self.db.close()
        self.db = storage.connect(self.db_path)
        self.processor = Processor(self.db, self.publish, now=self.clock)
        return self.processor

    def close(self):
        self.db.close()
        if self.tmp:
            self.tmp.cleanup()

    # --- джерело ---
    def event(self, sensor, reading, **overrides):
        event = make_event(sensor, reading, self.clock())
        event.update(overrides)
        return event

    def send(self, event, topic=TELEMETRY):
        payload = event if isinstance(event, (bytes, str)) else encode(event)
        return self.processor.handle_telemetry(topic, payload.encode() if isinstance(payload, str) else payload)

    def tick(self, value, sensors=config.SENSORS, period=config.SAMPLE_PERIOD_S):
        """Один такт: усі давачі показують value; потім годинник +period."""
        results = [self.send(self.event(sensor, value)) for sensor in sensors]
        self.clock.advance(period)
        return results

    def ticks(self, values, **kwargs):
        return [self.tick(value, **kwargs) for value in values]

    # --- агент ---
    def ack(self, command, status="applied", ventilation=None):
        ventilation = ventilation or ("on" if command["action"] == "ventilation_on" else "off")
        ack = make_ack(command["command_id"], command["device_id"], status, ventilation, self.clock())
        return self.processor.handle_ack(ACK, encode(ack).encode())

    def commands(self, action=None):
        return [message for _, message in self.sent if action in (None, message["action"])]

    def settle(self):
        """Підтвердити стартову команду приведення до безпечного стану (off)."""
        for command in self.commands():
            if storage.get_command(self.db, command["command_id"])["status"] == "sent":
                self.ack(command)
        self.sent.clear()

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def rejected_reasons(self):
        return [row[0] for row in self.db.execute("SELECT reason FROM rejected ORDER BY id")]
