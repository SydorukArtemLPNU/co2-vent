"""Агент пристрою: команда -> застосування стану вентиляції -> ACK.

Виконавець моделюється змінною ventilation (on/off) у локальній БД агента.
ACK надсилається лише після того, як стан записано (застосовано).
Повторний command_id не виконується вдруге - агент повертає попередній результат.
Прострочену команду агент не виконує, а відповідає rejected/expired.
"""
import argparse
import logging
import sqlite3
import threading
import time

import config
from contract import (ContractError, decode, encode, iso, make_ack, topic_device,
                      utc_now, validate_command)

log = logging.getLogger("agent")

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
  command_id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT, ventilation TEXT NOT NULL, ack_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actuator (
  device_id TEXT PRIMARY KEY, ventilation TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class Agent:
    def __init__(self, db, publish, now=utc_now, device_id=None, delay_s=0.0):
        self.db = db
        self.publish = publish
        self.now = now
        self.device_id = device_id or config.DEVICE_ID
        self.delay_s = delay_s
        self.lock = threading.Lock()
        db.executescript(SCHEMA)

    @property
    def ventilation(self):
        row = self.db.execute("SELECT ventilation FROM actuator WHERE device_id = ?",
                              (self.device_id,)).fetchone()
        return row[0] if row else "off"         # безпечний стан після першого запуску

    def handle_command(self, topic, payload):
        with self.lock:
            return self._command(topic, payload)

    def _command(self, topic, payload):
        try:
            if topic_device(topic, "command") != self.device_id:
                raise ContractError("foreign command topic")
            command = decode(payload)
            _, expires = validate_command(command, topic)
        except ContractError as exc:
            # без валідного command_id для нашого пристрою відповідати нікому
            log.warning("COMMAND invalid: %s", exc)
            return None
        cid = command["command_id"]
        done = self.db.execute("SELECT * FROM processed WHERE command_id = ?", (cid,)).fetchone()
        if done:
            log.info("COMMAND duplicate command_id=%s - not applied again, repeat ack", cid)
            ack = make_ack(cid, self.device_id, done[2], done[4], self.now(), done[3])
            ack["ack_at"] = done[5]
            self.publish(config.topic("ack", self.device_id), encode(ack))
            return ack

        if self.delay_s:
            time.sleep(self.delay_s)                 # модель повільного виконавця
        now = self.now()
        if now > expires:
            status, reason, ventilation = "rejected", "expired", self.ventilation
        else:
            status, reason = "applied", None
            ventilation = "on" if command["action"] == "ventilation_on" else "off"
        ack = make_ack(cid, self.device_id, status, ventilation, now, reason)
        with self.db:                                # стан і журнал - одна транзакція
            if status == "applied":
                self.db.execute(
                    """INSERT INTO actuator (device_id, ventilation, updated_at) VALUES (?,?,?)
                       ON CONFLICT(device_id) DO UPDATE SET ventilation = excluded.ventilation,
                       updated_at = excluded.updated_at""", (self.device_id, ventilation, iso(now)))
            self.db.execute("INSERT INTO processed VALUES (?,?,?,?,?,?)",
                            (cid, command["action"], status, reason, ventilation, ack["ack_at"]))
        log.info("COMMAND %s command_id=%s action=%s -> ventilation=%s%s",
                 status, cid, command["action"], ventilation, f" reason={reason}" if reason else "")
        self.publish(config.topic("ack", self.device_id), encode(ack))   # ACK після застосування
        return ack


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="Агент вентиляції класу (модель виконавця)")
    parser.add_argument("--db", default=config.AGENT_DB_PATH)
    parser.add_argument("--delay", type=float, default=0.0, help="затримка перед виконанням, с")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="co2vent-agent", clean_session=False)
    config.configure_mqtt(client, role="node")
    agent = Agent(sqlite3.connect(args.db, timeout=5, check_same_thread=False),
                  lambda topic, payload: client.publish(topic, payload, qos=1), delay_s=args.delay)

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("MQTT connected rc=%s ventilation=%s", reason_code, agent.ventilation)
        client.subscribe(config.topic("command"), qos=1)

    def on_message(client, userdata, msg):
        # окремий потік: затримка виконавця не блокує мережевий цикл MQTT
        threading.Thread(target=agent.handle_command, args=(msg.topic, msg.payload), daemon=True).start()

    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(1, 5)
    client.connect_async(config.BROKER_HOST, config.BROKER_PORT)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        client.disconnect()


if __name__ == "__main__":
    main()
