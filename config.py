"""Параметри системи «Вентиляція класу» (ПЗІР, курсова, варіант 20).

Усі значення можна перевизначити змінними середовища (див. config.example.env).
Тривалості правила задано в секундах «реального» часу; PZIR_TIME_SCALE > 1
пропорційно їх скорочує для прискореної демонстрації (симулятор при цьому
публікує частіше). Пороги ppm від масштабу не залежать.
"""
import os
from pathlib import Path


def _load_dotenv(path=Path(__file__).with_name(".env")):
    """Підхоплює локальний .env (секрети хмарного брокера); змінні середовища мають пріоритет."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _env(name, default):
    return os.environ.get(name) or default


def _env_float(name, default):
    return float(os.environ.get(name) or default)


# Мережа і сховище
BROKER_HOST = _env("PZIR_BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(_env_float("PZIR_BROKER_PORT", 1883))
BROKER_USER = _env("PZIR_BROKER_USER", None)        # секрети лише через env
BROKER_PASSWORD = _env("PZIR_BROKER_PASSWORD", None)
BROKER_TLS = _env("PZIR_BROKER_TLS", "0") in ("1", "true", "yes")   # хмарний брокер: TLS на 8883
BROKER_CA = _env("PZIR_BROKER_CA", None)        # власний CA-файл; інакше - системні сертифікати
BROKER_CERT = _env("PZIR_BROKER_CERT", None)    # клієнтський сертифікат X.509 (AWS IoT Core, mTLS)
BROKER_KEY = _env("PZIR_BROKER_KEY", None)      # закритий ключ до нього
NODE_CERT = _env("PZIR_NODE_CERT", None)        # сертифікат вузла (ESP, агент, симулятор), якщо окремий
NODE_KEY = _env("PZIR_NODE_KEY", None)
DB_PATH = _env("PZIR_DB", "co2vent.db")
AGENT_DB_PATH = _env("PZIR_AGENT_DB", "agent-state.db")
API_HOST = _env("PZIR_API_HOST", "127.0.0.1")
API_PORT = int(_env_float("PZIR_API_PORT", 8000))
PANEL_KEY = _env("PZIR_PANEL_KEY", None)    # хмара: панель відповідає лише проксі з цим ключем (X-Panel-Key)

# Пристрій: вузол Arduino Uno + ESP8266 з трьома SCD40 у класі
DEVICE_ID = _env("PZIR_DEVICE_ID", "class-20")
SENSORS = ("scd40-1", "scd40-2", "scd40-3")
TOPIC_PREFIX = "pzir/course"

# Контракт телеметрії
SCHEMA_VERSION = 1
METRIC, UNIT = "co2", "ppm"
SOURCES = ("simulated", "modeled_adc", "sensor")
CO2_MIN, CO2_MAX = 0, 40000          # вихідний діапазон SCD40 за даташитом
MAX_PAYLOAD = 1024                   # байт
SAMPLE_PERIOD_S = 5.0                # період periodic measurement SCD40

# Правило варіанта 20: > 1000 ppm протягом 3 хв -> вентиляція; < 800 ppm -> стоп
CO2_HIGH, CO2_LOW = 1000.0, 800.0
TIME_SCALE = _env_float("PZIR_TIME_SCALE", 1.0)
if TIME_SCALE <= 0:
    raise ValueError("PZIR_TIME_SCALE must be positive")
HOLD_S = 180.0 / TIME_SCALE          # час утримання перевищення
MAX_GAP_S = 15.0 / TIME_SCALE        # довший розрив валідних даних обнуляє відлік
SENSOR_FRESH_S = 15.0 / TIME_SCALE   # показ давача старший за це не входить у медіану
MIN_SENSORS = 2                      # мінімум свіжих давачів для рішення
MAX_AGE_S = 60.0 / TIME_SCALE        # старіша подія відхиляється як stale
MAX_FUTURE_S = 5.0                   # допустимий випередження годинника джерела

# Команди
ACTIONS = ("ventilation_on", "ventilation_off")
COMMAND_TTL_S = 10.0 / TIME_SCALE
SWEEP_PERIOD_S = 0.1                 # період перевірки тайм-аутів (К4: запізнення ≤ 0,1 с)


def topic(kind, device_id=None):
    return f"{TOPIC_PREFIX}/{device_id or DEVICE_ID}/{kind}"


def configure_mqtt(client, role="server"):
    """Логін і TLS для клієнта paho. role="node" - пристрій (модель ESP, агент, симулятор):
    у AWS IoT він має власний сертифікат і політику лише на теми свого класу."""
    if BROKER_USER:
        client.username_pw_set(BROKER_USER, BROKER_PASSWORD)
    if BROKER_TLS:
        import ssl
        here = Path(__file__).parent
        path = lambda p: str(here / p) if p and not Path(p).is_absolute() else p   # noqa: E731
        cert, key = (NODE_CERT, NODE_KEY) if role == "node" and NODE_CERT else (BROKER_CERT, BROKER_KEY)
        client.tls_set(ca_certs=path(BROKER_CA), certfile=path(cert), keyfile=path(key),
                       cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    return client
