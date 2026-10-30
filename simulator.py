"""Програмний симулятор вузла Uno + ESP8266 з трьома SCD40 (маршрут А).

Відтворює CSV-сценарій за графіком offset_s / PZIR_TIME_SCALE і публікує
телеметрію в MQTT (QoS 1). observed_at - фактичний UTC-час публікації,
source=simulated. Контрольні вставки (quality) навмисно порушують контракт.
"""
import argparse
import csv
import json
import time
from datetime import timedelta

import config
from contract import encode, iso, make_event, utc_now


def build_message(row, now, last_by_sensor):
    """Повертає payload-словник або None для skip."""
    sensor, quality = row["sensor_id"], row["quality"]
    if quality == "skip":
        return None
    if quality == "duplicate":
        return last_by_sensor[sensor]
    value = float(row["value"])
    event = make_event(sensor, value, now)
    if quality == "bad_type":
        event["value"] = "abc"
    elif quality == "bad_unit":
        event["unit"] = "ppb"
    elif quality == "mismatch":
        event["device_id"] = "class-99"
    elif quality == "stale":
        event["observed_at"] = iso(now - timedelta(seconds=2 * config.MAX_AGE_S))
    elif quality == "out_of_range":
        event["value"] = 50000.0
    elif quality != "ok":
        raise ValueError(f"unknown quality {quality}")
    if quality == "ok":
        last_by_sensor[sensor] = event
    return event


def load(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main():
    import paho.mqtt.client as mqtt

    parser = argparse.ArgumentParser(description="Симулятор телеметрії CO2 (3 x SCD40)")
    parser.add_argument("--scenario", default="scenarios/class_day.csv")
    parser.add_argument("--limit", type=int, default=0, help="лише перші N рядків")
    args = parser.parse_args()
    rows = load(args.scenario)[: args.limit or None]

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="co2vent-simulator")
    if config.BROKER_USER:
        client.username_pw_set(config.BROKER_USER, config.BROKER_PASSWORD)
    client.reconnect_delay_set(1, 5)
    client.connect(config.BROKER_HOST, config.BROKER_PORT)
    client.loop_start()             # при розриві QoS 1 повідомлення чекають у черзі клієнта

    topic = config.topic("telemetry")
    last_by_sensor = {}
    start = time.monotonic()
    print(json.dumps({"scenario": args.scenario, "rows": len(rows), "time_scale": config.TIME_SCALE,
                      "topic": topic}, ensure_ascii=False), flush=True)
    try:
        for number, row in enumerate(rows, 1):
            delay = start + float(row["offset_s"]) / config.TIME_SCALE - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            event = build_message(row, utc_now(), last_by_sensor)
            if event is None:
                print(json.dumps({"row": number, "sensor_id": row["sensor_id"], "skipped": True,
                                  "note": row["note"]}, ensure_ascii=False), flush=True)
                continue
            client.publish(topic, encode(event), qos=1)
            print(json.dumps({"row": number, "quality": row["quality"], "note": row["note"],
                              "event": event}, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        time.sleep(0.5)             # дочекатися PUBACK останніх повідомлень
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
