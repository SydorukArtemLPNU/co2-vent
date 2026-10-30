"""Зведення прогонів logs/run-*: затримки й перевірка критеріїв приймання за БД обробника.

python scripts/report_runs.py [logs] > logs/SUMMARY.md

Затримки (реальний час, не масштабований):
  телеметрія  received_at - observed_at  (джерело/ESP -> брокер -> обробник -> запис у БД)
  команда     ack_at - issued_at         (обробник -> брокер -> агент/ESP -> Uno -> ACK)
Час утримання: від першої події серії з медіаною > 1000 ppm до події-тригера ventilation_on,
у сценарних секундах (x time_scale).
К4: запізнення позначки expired = closed_at - expires_at (стовпець closed_at є з версії v1.1).
"""
import re
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

HOLD_S, PERIOD_S = 180.0, 5.0


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def pct(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def stats_ms(values):
    if not values:
        return "—"
    return (f"{statistics.median(values):.1f} / {pct(values, 0.95):.1f} / {max(values):.1f}"
            f" (n={len(values)})")


def analyse(run):
    db = sqlite3.connect(run / "co2vent.db")
    db.row_factory = sqlite3.Row
    scale = float(re.search(r"-x([\d.]+)", run.name).group(1))
    events = db.execute("SELECT * FROM events ORDER BY observed_at, rowid").fetchall()
    telemetry = [(ts(e["received_at"]) - ts(e["observed_at"])).total_seconds() * 1000 for e in events]
    commands = db.execute("SELECT * FROM commands ORDER BY issued_at").fetchall()
    applied = [c for c in commands if c["status"] == "applied" and c["ack_at"]]
    rtt = [(ts(c["ack_at"]) - ts(c["issued_at"])).total_seconds() * 1000 for c in applied]
    statuses = {}
    for c in commands:
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
    rejected = dict(db.execute("SELECT reason, COUNT(*) FROM rejected GROUP BY reason").fetchall())

    holds = []
    for c in commands:
        if c["action"] != "ventilation_on" or not c["trigger_event_id"]:
            continue
        trigger = next((i for i, e in enumerate(events) if e["event_id"] == c["trigger_event_id"]), None)
        if trigger is None:
            continue
        start = trigger
        while start > 0 and (events[start - 1]["co2_median"] or 0) > 1000:
            start -= 1
        holds.append((ts(events[trigger]["observed_at"]) - ts(events[start]["observed_at"])).total_seconds() * scale)
    columns = {row[1] for row in db.execute("PRAGMA table_info(commands)")}
    lateness = None
    if "closed_at" in columns:
        lateness = [(ts(c["closed_at"]) - ts(c["expires_at"])).total_seconds() * 1000
                    for c in commands if c["status"] == "expired" and c["closed_at"]]
    return {"run": run.name, "scale": scale, "events": len(events), "telemetry": telemetry,
            "rtt": rtt, "statuses": statuses, "rejected": rejected, "holds": holds,
            "lateness": lateness}


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "logs")
    runs = sorted(p for p in root.glob("run-*") if (p / "co2vent.db").exists())
    results = [analyse(run) for run in runs]
    print("# Зведення прогонів\n")
    print("Затримки, мс: медіана / p95 / максимум. Утримання — у сценарних секундах.\n")
    print("| Прогін | ×k | Подій у БД | Телеметрія, мс | Команда → ACK, мс | Команди за статусом | Відхилено | Утримання до ventilation_on, с |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(r["statuses"].items())) or "—"
        rejected = ", ".join(f"{k} {v}" for k, v in sorted(r["rejected"].items())) or "—"
        holds = ", ".join(f"{h:.1f}" for h in r["holds"]) or "—"
        print(f"| `{r['run']}` | {r['scale']:g} | {r['events']} | {stats_ms(r['telemetry'])} | "
              f"{stats_ms(r['rtt'])} | {statuses} | {rejected} | {holds} |")
    print("\n## Критерій К1: утримання 180 с\n")
    print(f"Очікувано: {HOLD_S:.0f} с ≤ утримання < {HOLD_S + PERIOD_S:.0f} с "
          "(рішення на першій події після 180 с; період давачів 5 с).\n")
    for r in results:
        for hold in r["holds"]:
            verdict = "✅" if HOLD_S <= hold < HOLD_S + PERIOD_S else "❌"
            print(f"- {verdict} `{r['run']}`: {hold:.2f} с")
    print("\n## Критерій К4: позначка expired не пізніше TTL + 0,5 с\n")
    print("Запізнення = closed_at − expires_at, мс (лише прогони з v1.1, де є closed_at).\n")
    for r in results:
        if r["lateness"] is None:
            if r["statuses"].get("expired"):
                print(f"- — `{r['run']}`: closed_at не записувався (до v1.1)")
            continue
        if r["lateness"]:
            worst = max(r["lateness"])
            verdict = "✅" if worst <= 500 else "❌"
            print(f"- {verdict} `{r['run']}`: {len(r['lateness'])} команд expired, "
                  f"запізнення {min(r['lateness']):.0f}…{worst:.0f} мс")


if __name__ == "__main__":
    main()
