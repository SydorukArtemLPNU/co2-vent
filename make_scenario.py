"""Генератор відтворюваного сценарію для трьох SCD40 (фіксований seed).

Рядок CSV: offset_s, sensor_id, value, quality, note.
quality: ok | skip | duplicate | bad_type | bad_unit | mismatch | stale | out_of_range.
Такти через SAMPLE_PERIOD_S (5 с, як periodic measurement SCD40).
"""
import argparse
import csv
import random

import config

# (перший такт, останній такт, базове значення на початку, в кінці, опис фази)
PHASES = [
    (0, 11, 650, 900, "норма: клас наповнюється"),
    (12, 29, 1060, 1110, "коротке перевищення 90 с - без команди"),
    (30, 30, 1000, 1000, "межа: рівно 1000 ppm не є перевищенням"),
    (31, 33, 960, 960, "спад нижче порога - відлік скинуто"),
    (34, 75, 1060, 1350, "тривале перевищення > 3 хв -> ventilation_on"),
    (76, 85, 1300, 1010, "вентиляція працює, CO2 падає"),
    (86, 86, 1000, 1000, "межа: рівно 1000 при ввімкненій вентиляції"),
    (87, 94, 980, 830, "зона гістерезису 800...1000 - вентиляція працює"),
    (95, 95, 800, 800, "межа: рівно 800 ppm не зупиняє"),
    (96, 105, 780, 650, "нижче 800 -> ventilation_off"),
    (106, 111, 640, 620, "норма після провітрювання"),
]
OFFSETS = {"scd40-1": 0.0, "scd40-2": 15.0, "scd40-3": -10.0}   # розкид екземплярів
EXACT = {30, 86, 95}            # такти-межі: усі давачі показують рівно базове значення
# (такт, давач) -> вставка несправності замість нормального рядка
FAULTS = {
    (25, "scd40-2"): "bad_type",
    (40, "scd40-1"): "bad_unit",
    (55, "scd40-2"): "mismatch",
    (60, "scd40-3"): "stale",
    (65, "scd40-1"): "out_of_range",
}
DROPOUT = ("scd40-3", range(45, 53))          # давач 3 зник на 40 с - медіана з двох
STUCK = ("scd40-3", range(100, 104), 2500.0)  # давач 3 "залип" високо - медіана стійка
DUPLICATE_AFTER = (20, "scd40-1")             # повтор останнього event_id давача


def base_value(tick):
    for first, last, start, end, note in PHASES:
        if first <= tick <= last:
            share = 0 if last == first else (tick - first) / (last - first)
            return start + (end - start) * share, note
    raise ValueError(tick)


def build(seed):
    rng = random.Random(seed)
    rows = []
    for tick in range(PHASES[-1][1] + 1):
        base, note = base_value(tick)
        for index, sensor in enumerate(config.SENSORS):
            offset_s = round(tick * config.SAMPLE_PERIOD_S + index * 0.1, 1)
            if tick in EXACT:
                value = base
            else:
                value = base + OFFSETS[sensor] + rng.gauss(0, 8)
            value = round(value, 1)
            quality, row_note = "ok", note
            if sensor == DROPOUT[0] and tick in DROPOUT[1]:
                quality, row_note = "skip", "давач не відповідає"
            elif sensor == STUCK[0] and tick in STUCK[1]:
                value, row_note = STUCK[2], "несправність: давач залип на 2500"
            fault = FAULTS.get((tick, sensor))
            if fault:
                quality, row_note = fault, f"вставка: {fault}"
            rows.append([offset_s, sensor, value, quality, row_note])
            if (tick, sensor) == DUPLICATE_AFTER:
                rows.append([round(offset_s + 0.05, 2), sensor, value, "duplicate",
                             "вставка: повтор event_id"])
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20)
    parser.add_argument("--out", default="scenarios/class_day.csv")
    args = parser.parse_args()
    rows = build(args.seed)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["offset_s", "sensor_id", "value", "quality", "note"])
        writer.writerows(rows)
    sent = sum(1 for row in rows if row[3] != "skip")
    print(f"{args.out}: {len(rows)} рядків, {sent} повідомлень, "
          f"{rows[-1][0]:.0f} с сценарного часу, seed={args.seed}")


if __name__ == "__main__":
    main()
