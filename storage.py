"""SQLite-сховище обробника: історія подій, відхилення, стан правила, команди.

Час зберігається як ISO 8601 UTC з мілісекундами і суфіксом Z (contract.iso),
тому рядки порівнюються лексикографічно в тому самому порядку, що й моменти.
Усі запити параметризовані.
"""
import sqlite3

from contract import iso, parse_time
from policy import VentState

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, sensor_id TEXT NOT NULL,
  observed_at TEXT NOT NULL, received_at TEXT NOT NULL,
  metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, source TEXT NOT NULL,
  co2_median REAL                       -- медіана трьох давачів після цієї події
);
CREATE INDEX IF NOT EXISTS events_device_time ON events(device_id, observed_at);
CREATE INDEX IF NOT EXISTS events_sensor_time ON events(device_id, sensor_id, observed_at);
CREATE TABLE IF NOT EXISTS rejected (
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
  topic TEXT NOT NULL, event_id TEXT, reason TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_state (
  device_id TEXT PRIMARY KEY, ventilation_desired TEXT NOT NULL,
  high_since TEXT, last_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
  command_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('created','sent','applied','rejected','expired')),
  issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  trigger_event_id TEXT, ack_at TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS commands_device_time ON commands(device_id, issued_at);
CREATE TABLE IF NOT EXISTS actuator (
  device_id TEXT PRIMARY KEY, ventilation TEXT NOT NULL,
  command_id TEXT NOT NULL, confirmed_at TEXT NOT NULL
);
"""


def connect(path, init=True):
    # timeout: обробник і API можуть одночасно відкривати ту саму БД
    db = sqlite3.connect(path, timeout=5, check_same_thread=False)
    db.row_factory = sqlite3.Row
    if init:
        db.execute("PRAGMA journal_mode=WAL")   # API читає паралельно з обробником
        db.executescript(SCHEMA)
    return db


# --- події ---------------------------------------------------------------

def event_exists(db, event_id):
    return db.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone() is not None


def insert_event_once(db, event, observed, received):
    """Ідемпотентний запис за event_id; False - така подія вже є."""
    cur = db.execute(
        """INSERT OR IGNORE INTO events (event_id, device_id, sensor_id, observed_at,
           received_at, metric, value, unit, source) VALUES (?,?,?,?,?,?,?,?,?)""",
        (event["event_id"], event["device_id"], event["sensor_id"], iso(observed),
         iso(received), event["metric"], float(event["value"]), event["unit"], event["source"]))
    db.commit()
    return cur.rowcount == 1


def set_event_median(db, event_id, value):
    db.execute("UPDATE events SET co2_median = ? WHERE event_id = ?", (value, event_id))
    db.commit()


def latest_by_sensor(db, device_id):
    """{sensor_id: (observed_at, value)} - останній запис кожного давача."""
    rows = db.execute(
        """SELECT e.sensor_id, e.observed_at, e.value FROM events e
           JOIN (SELECT sensor_id, MAX(observed_at) AS last FROM events
                 WHERE device_id = ? GROUP BY sensor_id) m
             ON e.sensor_id = m.sensor_id AND e.observed_at = m.last
           WHERE e.device_id = ?""", (device_id, device_id)).fetchall()
    return {row["sensor_id"]: (parse_time(row["observed_at"]), row["value"]) for row in rows}


def record_rejection(db, received, topic, reason, payload, event_id=None):
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    db.execute("INSERT INTO rejected (received_at, topic, event_id, reason, payload) VALUES (?,?,?,?,?)",
               (iso(received), topic, event_id, reason, text[:300]))
    db.commit()


# --- стан правила --------------------------------------------------------

def load_state(db, device_id):
    row = db.execute("SELECT * FROM rule_state WHERE device_id = ?", (device_id,)).fetchone()
    if row is None:
        return VentState()
    return VentState(
        on=row["ventilation_desired"] == "on",
        high_since=parse_time(row["high_since"]) if row["high_since"] else None,
        last_at=parse_time(row["last_at"]) if row["last_at"] else None)


def save_state(db, device_id, state, now):
    db.execute(
        """INSERT INTO rule_state (device_id, ventilation_desired, high_since, last_at, updated_at)
           VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET
           ventilation_desired = excluded.ventilation_desired, high_since = excluded.high_since,
           last_at = excluded.last_at, updated_at = excluded.updated_at""",
        (device_id, "on" if state.on else "off",
         iso(state.high_since) if state.high_since else None,
         iso(state.last_at) if state.last_at else None, iso(now)))
    db.commit()


# --- команди й підтверджений стан виконавця -------------------------------

def save_command(db, command, status="created", trigger_event_id=None):
    db.execute(
        """INSERT INTO commands (command_id, device_id, action, status, issued_at,
           expires_at, trigger_event_id) VALUES (?,?,?,?,?,?,?)""",
        (command["command_id"], command["device_id"], command["action"], status,
         command["issued_at"], command["expires_at"], trigger_event_id))
    db.commit()


def set_command_status(db, command_id, status, *, from_statuses, ack_at=None, reason=None):
    """Перехід стану лише з дозволених станів; повертає True, якщо відбувся."""
    marks = ",".join("?" * len(from_statuses))
    cur = db.execute(
        f"""UPDATE commands SET status = ?, ack_at = COALESCE(?, ack_at),
            reason = COALESCE(?, reason)
            WHERE command_id = ? AND status IN ({marks})""",
        (status, ack_at, reason, command_id, *from_statuses))
    db.commit()
    return cur.rowcount == 1


def note_command(db, command_id, reason, ack_at=None):
    """Додає примітку без зміни статусу (наприклад, запізнілий ACK)."""
    db.execute("UPDATE commands SET reason = ?, ack_at = COALESCE(?, ack_at) WHERE command_id = ?",
               (reason, ack_at, command_id))
    db.commit()


def get_command(db, command_id):
    row = db.execute("SELECT * FROM commands WHERE command_id = ?", (command_id,)).fetchone()
    return dict(row) if row else None


def open_commands(db, device_id=None):
    sql = "SELECT * FROM commands WHERE status IN ('created','sent')"
    args = ()
    if device_id:
        sql += " AND device_id = ?"
        args = (device_id,)
    return [dict(row) for row in db.execute(sql + " ORDER BY issued_at", args)]


def last_command(db, device_id):
    row = db.execute("SELECT * FROM commands WHERE device_id = ? ORDER BY issued_at DESC, rowid DESC LIMIT 1",
                     (device_id,)).fetchone()
    return dict(row) if row else None


def set_actuator(db, device_id, ventilation, command_id, confirmed_at):
    db.execute(
        """INSERT INTO actuator (device_id, ventilation, command_id, confirmed_at) VALUES (?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET ventilation = excluded.ventilation,
           command_id = excluded.command_id, confirmed_at = excluded.confirmed_at""",
        (device_id, ventilation, command_id, confirmed_at))
    db.commit()


def get_actuator(db, device_id):
    row = db.execute("SELECT * FROM actuator WHERE device_id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


# --- запити для API ------------------------------------------------------

def device_known(db, device_id):
    return any(db.execute(f"SELECT 1 FROM {table} WHERE device_id = ? LIMIT 1", (device_id,)).fetchone()
               for table in ("events", "rule_state", "commands"))


def recent_events(db, device_id, limit, sensor_id=None):
    sql = "SELECT * FROM events WHERE device_id = ?"
    args = [device_id]
    if sensor_id:
        sql += " AND sensor_id = ?"
        args.append(sensor_id)
    rows = db.execute(sql + " ORDER BY observed_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(row) for row in reversed(rows)]      # у часовому порядку


def recent_commands(db, device_id, limit):
    rows = db.execute("SELECT * FROM commands WHERE device_id = ? ORDER BY issued_at DESC, rowid DESC LIMIT ?",
                      (device_id, limit)).fetchall()
    return [dict(row) for row in rows]


def recent_rejected(db, limit):
    rows = db.execute("SELECT * FROM rejected ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
