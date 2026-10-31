"""Перевірка з'єднання з брокером (локальним або хмарним) з параметрів config/.env.

python scripts/check_broker.py   -> публікує й отримує тестове повідомлення, секрети не друкує.
"""
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paho.mqtt.client as mqtt  # noqa: E402

import config  # noqa: E402

topic = f"{config.TOPIC_PREFIX}/{config.DEVICE_ID}/selftest"
token = uuid.uuid4().hex
got = threading.Event()
result = {}


def on_connect(client, userdata, flags, reason_code, properties):
    result["connect"] = str(reason_code)
    if not reason_code.is_failure:
        client.subscribe(topic, qos=1)


def on_subscribe(client, userdata, mid, reason_codes, properties):
    started = time.monotonic()
    result["t0"] = started
    client.publish(topic, token, qos=1)


def on_message(client, userdata, msg):
    if msg.payload.decode() == token:
        result["rtt_ms"] = (time.monotonic() - result["t0"]) * 1000
        got.set()


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"co2vent-check-{token[:6]}")
config.configure_mqtt(client)
client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
print(f"broker={config.BROKER_HOST}:{config.BROKER_PORT} tls={config.BROKER_TLS} "
      f"user={'set' if config.BROKER_USER else '-'} password={'set' if config.BROKER_PASSWORD else '-'}")
try:
    client.connect(config.BROKER_HOST, config.BROKER_PORT, keepalive=15)
except Exception as exc:                     # DNS, TLS, відмова з'єднання
    print("CONNECT ERROR:", type(exc).__name__, exc)
    sys.exit(1)
client.loop_start()
ok = got.wait(10)
client.loop_stop()
client.disconnect()
print("connect:", result.get("connect", "no CONNACK"))
if ok:
    print(f"OK: round trip publish -> subscribe {result['rtt_ms']:.0f} ms via {topic}")
else:
    print("FAIL: test message not received within 10 s")
sys.exit(0 if ok else 2)
