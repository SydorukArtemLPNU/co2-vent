"""HTTP API і вебпанель (лише читання БД обробника).

GET /                                   панель моніторингу
GET /api/devices/<id>/latest            остання подія або null (200), невідомий пристрій - 404
GET /api/devices/<id>/state             медіана, давачі, правило, виконавець, остання команда
GET /api/devices/<id>/events?limit=30[&sensor=scd40-1]
GET /api/devices/<id>/commands?limit=10
GET /api/rejected?limit=10
Якщо задано PZIR_PANEL_KEY, кожен запит має нести заголовок X-Panel-Key (інакше 403).
"""
import hmac
import json
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import config
import storage
from contract import ID_RE, iso, parse_time, utc_now

DASHBOARD = Path(__file__).with_name("dashboard.html")


def ventilation_view(actuator, command, now=None):
    """Стан для інтерфейсу: on/off лише за ACK; без ACK до дедлайну - unknown.
    Дедлайн перевіряється тут же, тож панель не чекає наступного sweep() обробника."""
    if command and command["status"] in ("created", "sent"):
        if now is not None and now > parse_time(command["expires_at"]):
            return "unknown"
        return "pending"
    if command and command["status"] in ("expired", "rejected"):
        if not actuator or actuator["confirmed_at"] < command["issued_at"]:
            return "unknown"
    return actuator["ventilation"] if actuator else "unknown"


def device_state(db, device_id, now):
    rule = db.execute("SELECT * FROM rule_state WHERE device_id = ?", (device_id,)).fetchone()
    median = db.execute("""SELECT co2_median, observed_at FROM events WHERE device_id = ?
                           AND co2_median IS NOT NULL ORDER BY observed_at DESC LIMIT 1""",
                        (device_id,)).fetchone()
    sensors = {}
    for sensor in config.SENSORS:
        row = db.execute("""SELECT value, observed_at, source FROM events WHERE device_id = ?
                            AND sensor_id = ? ORDER BY observed_at DESC LIMIT 1""",
                         (device_id, sensor)).fetchone()
        if row:
            age = (now - parse_time(row["observed_at"])).total_seconds()
            sensors[sensor] = {"value": row["value"], "observed_at": row["observed_at"],
                               "source": row["source"], "age_s": round(age, 1),
                               "fresh": age <= config.SENSOR_FRESH_S}
        else:
            sensors[sensor] = None
    actuator = storage.get_actuator(db, device_id)
    command = storage.last_command(db, device_id)
    hold_elapsed = None
    if rule and rule["high_since"] and rule["last_at"]:
        hold_elapsed = (parse_time(rule["last_at"]) - parse_time(rule["high_since"])).total_seconds()
    return {
        "device_id": device_id, "now": iso(now), "time_scale": config.TIME_SCALE,
        "thresholds": {"high_ppm": config.CO2_HIGH, "low_ppm": config.CO2_LOW,
                       "hold_s": config.HOLD_S, "command_ttl_s": config.COMMAND_TTL_S},
        "co2_median": median["co2_median"] if median else None,
        "median_at": median["observed_at"] if median else None,
        "data_stale": not median or now - parse_time(median["observed_at"])
                      > timedelta(seconds=config.MAX_GAP_S),
        "sensors": sensors,
        "rule": {"desired": rule["ventilation_desired"], "high_since": rule["high_since"],
                 "hold_elapsed_s": hold_elapsed} if rule else None,
        "ventilation": ventilation_view(actuator, command, now),
        "actuator": actuator,
        "last_command": command,
    }


class Handler(BaseHTTPRequestHandler):
    db_path = config.DB_PATH
    panel_key = config.PANEL_KEY

    def log_message(self, format, *args):      # без журналу кожного запиту
        pass

    def do_GET(self):
        if self.panel_key and not hmac.compare_digest(self.headers.get("X-Panel-Key", ""), self.panel_key):
            return self.reply(403, {"error": "forbidden"})
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            return self.send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
        parts = url.path.strip("/").split("/")
        db = storage.connect(self.db_path, init=False)   # схему створює main()
        try:
            if parts == ["api", "rejected"]:
                limit = self.limit(query, 10)
                return limit and self.reply(200, storage.recent_rejected(db, limit))
            if len(parts) != 4 or parts[:2] != ["api", "devices"] \
                    or parts[3] not in ("latest", "state", "events", "commands"):
                return self.reply(404, {"error": "route not found"})
            device_id, view = parts[2], parts[3]
            if not ID_RE.match(device_id):
                return self.reply(400, {"error": "invalid device_id"})
            if not storage.device_known(db, device_id):
                return self.reply(404, {"error": "unknown device"})
            if view == "latest":
                rows = storage.recent_events(db, device_id, 1)
                return self.reply(200, rows[-1] if rows else None)
            if view == "state":
                return self.reply(200, device_state(db, device_id, utc_now()))
            limit = self.limit(query, 30 if view == "events" else 10)
            if not limit:
                return None
            if view == "events":
                sensor = query.get("sensor", [None])[0]
                if sensor is not None and sensor not in config.SENSORS:
                    return self.reply(400, {"error": "unknown sensor"})
                return self.reply(200, storage.recent_events(db, device_id, limit, sensor))
            return self.reply(200, storage.recent_commands(db, device_id, limit))
        finally:
            db.close()

    def limit(self, query, default):
        try:
            value = int(query.get("limit", [default])[0])
        except ValueError:
            value = 0
        if not 1 <= value <= 500:
            self.reply(400, {"error": "limit must be 1..500"})
            return None
        return value

    def reply(self, status, value):
        self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                  "application/json; charset=utf-8")

    def send(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="HTTP API і панель вентиляції класу")
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()
    Handler.db_path = args.db
    storage.connect(args.db).close()       # створити схему, якщо обробник ще не стартував
    print(f"API http://{config.API_HOST}:{config.API_PORT}/  db={args.db}", flush=True)
    ThreadingHTTPServer((config.API_HOST, config.API_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
