"""Контракти повідомлень: телеметрія, команда, ACK.

Кожна перевірка кидає ContractError з короткою причиною, яка потрапляє
в журнал і таблицю rejected. Перевірки свіжості (stale/future) потребують
часу приймання, тому now передається явно - так їх легко тестувати.
"""
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

import config

EVENT_FIELDS = {"schema_version", "event_id", "device_id", "sensor_id",
                "observed_at", "metric", "value", "unit", "source"}
COMMAND_FIELDS = {"command_id", "device_id", "action", "issued_at", "expires_at"}
ACK_FIELDS = {"command_id", "device_id", "status", "ack_at", "ventilation"}
ACK_STATUSES = ("applied", "rejected")
STATE_FIELDS = {"device_id", "ventilation", "reason", "reported_at"}
STATE_REASONS = ("node_boot", "node_sync")   # скидання Uno / перша синхронізація після старту ESP
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ContractError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(text):
    """RFC 3339 з обов'язковою зоною; повертає aware datetime в UTC."""
    if not isinstance(text, str):
        raise ContractError("invalid timestamp")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("invalid timestamp") from exc
    if moment.tzinfo is None:
        raise ContractError("timestamp without timezone")
    return moment.astimezone(timezone.utc)


def topic_device(topic, kind):
    """pzir/course/<device>/<kind> -> device; інша тема - помилка."""
    parts = topic.split("/")
    prefix = config.TOPIC_PREFIX.split("/")
    if len(parts) != len(prefix) + 2 or parts[:len(prefix)] != prefix or parts[-1] != kind:
        raise ContractError("unexpected topic")
    return parts[-2]


def decode(payload):
    if len(payload) > config.MAX_PAYLOAD:
        raise ContractError("payload too large")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("invalid json") from exc
    if not isinstance(data, dict):
        raise ContractError("payload is not an object")
    return data


def encode(message):
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _check_fields(message, fields):
    missing = fields - set(message)
    if missing:
        raise ContractError("missing field: " + ",".join(sorted(missing)))
    extra = set(message) - fields
    if extra:
        raise ContractError("unknown field: " + ",".join(sorted(extra)))


def _check_id(value, name):
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ContractError(f"invalid {name}")


def make_event(sensor_id, value, observed_at, *, device_id=None, event_id=None,
               source="simulated", unit=config.UNIT):
    return {
        "schema_version": config.SCHEMA_VERSION,
        "event_id": event_id or str(uuid.uuid4()),
        "device_id": device_id or config.DEVICE_ID,
        "sensor_id": sensor_id,
        "observed_at": iso(observed_at),
        "metric": config.METRIC,
        "value": value,
        "unit": unit,
        "source": source,
    }


def validate_event(event, topic, now):
    """Повна перевірка події; повертає observed_at як datetime."""
    _check_fields(event, EVENT_FIELDS)
    if type(event["schema_version"]) is not int or event["schema_version"] != config.SCHEMA_VERSION:
        raise ContractError("unsupported schema_version")
    _check_id(event["event_id"], "event_id")
    _check_id(event["device_id"], "device_id")
    if event["device_id"] != topic_device(topic, "telemetry"):
        raise ContractError("device_id mismatch with topic")
    if event["sensor_id"] not in config.SENSORS:
        raise ContractError("unknown sensor_id")
    if event["metric"] != config.METRIC:
        raise ContractError("unknown metric")
    if event["unit"] != config.UNIT:
        raise ContractError("unknown unit")
    if event["source"] not in config.SOURCES:
        raise ContractError("unknown source")
    value = event["value"]
    # bool є підтипом int, тому type(), а не isinstance()
    if type(value) not in (int, float) or math.isnan(value) or math.isinf(value):
        raise ContractError("value is not a number")
    if not config.CO2_MIN <= value <= config.CO2_MAX:
        raise ContractError("value out of range")
    observed = parse_time(event["observed_at"])
    if observed > now + timedelta(seconds=config.MAX_FUTURE_S):
        raise ContractError("observed_at in the future")
    if observed < now - timedelta(seconds=config.MAX_AGE_S):
        raise ContractError("stale event")
    return observed


def make_command(device_id, action, now, ttl_s=None):
    ttl = config.COMMAND_TTL_S if ttl_s is None else ttl_s
    return {
        "command_id": "cmd-" + uuid.uuid4().hex[:12],
        "device_id": device_id,
        "action": action,
        "issued_at": iso(now),
        "expires_at": iso(now + timedelta(seconds=ttl)),
    }


def validate_command(command, topic):
    _check_fields(command, COMMAND_FIELDS)
    _check_id(command["command_id"], "command_id")
    _check_id(command["device_id"], "device_id")
    if command["device_id"] != topic_device(topic, "command"):
        raise ContractError("device_id mismatch with topic")
    if command["action"] not in config.ACTIONS:
        raise ContractError("unknown action")
    issued, expires = parse_time(command["issued_at"]), parse_time(command["expires_at"])
    if expires <= issued:
        raise ContractError("expires_at before issued_at")
    return issued, expires


def make_ack(command_id, device_id, status, ventilation, now, reason=None):
    ack = {"command_id": command_id, "device_id": device_id, "status": status,
           "ack_at": iso(now), "ventilation": ventilation}
    if reason:
        ack["reason"] = reason
    return ack


def validate_ack(ack, topic):
    fields = ACK_FIELDS | {"reason"} if "reason" in ack else ACK_FIELDS
    _check_fields(ack, fields)
    _check_id(ack["command_id"], "command_id")
    _check_id(ack["device_id"], "device_id")
    if ack["device_id"] != topic_device(topic, "ack"):
        raise ContractError("device_id mismatch with topic")
    if ack["status"] not in ACK_STATUSES:
        raise ContractError("unknown ack status")
    if ack["ventilation"] not in ("on", "off"):
        raise ContractError("invalid ventilation state")
    return parse_time(ack["ack_at"])


def make_state(device_id, ventilation, reason, now):
    """Звіт вузла про фактичний стан виконавця без команди (наприклад, після скидання Uno)."""
    return {"device_id": device_id, "ventilation": ventilation, "reason": reason,
            "reported_at": iso(now)}


def validate_state(report, topic):
    _check_fields(report, STATE_FIELDS)
    _check_id(report["device_id"], "device_id")
    if report["device_id"] != topic_device(topic, "state"):
        raise ContractError("device_id mismatch with topic")
    if report["ventilation"] not in ("on", "off"):
        raise ContractError("invalid ventilation state")
    if report["reason"] not in STATE_REASONS:
        raise ContractError("unknown state reason")
    return parse_time(report["reported_at"])
