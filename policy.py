"""Чиста логіка рішення (без MQTT і БД) - варіант 20.

1. Агрегація трьох SCD40: медіана останніх показів, свіжих відносно моменту
   рішення (не старших за SENSOR_FRESH_S); потрібно щонайменше MIN_SENSORS.
   Медіана стійка до одного несправного давача.
2. Автомат стану:
   OFF: медіана > 1000 ppm безперервно HOLD_S (180 с) -> ventilation_on.
        Значення рівно 1000 не є перевищенням і скидає відлік.
   ON:  медіана < 800 ppm -> ventilation_off; 800...1000 - вентиляція працює
        (гістерезис). Рівно 800 не зупиняє.
   Відсутність рішення (мало свіжих давачів) не рахується ні нормою, ні
   перевищенням; якщо валідних рішень не було довше MAX_GAP_S, безперервність
   не доведена і відлік перевищення починається заново.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

import config


@dataclass(frozen=True)
class VentState:
    on: bool = False
    high_since: datetime | None = None   # перше значення поточної серії > HIGH
    last_at: datetime | None = None      # час останнього врахованого рішення


def aggregate(latest, at, fresh_s=None, min_sensors=None):
    """latest: {sensor_id: (observed_at, value)} -> медіана або None."""
    fresh = timedelta(seconds=config.SENSOR_FRESH_S if fresh_s is None else fresh_s)
    need = config.MIN_SENSORS if min_sensors is None else min_sensors
    # abs: давачі одного такту приходять у довільному порядку
    values = [value for observed, value in latest.values() if abs(at - observed) <= fresh]
    return float(median(values)) if len(values) >= need else None


def advance(state, at, value, *, high=None, low=None, hold_s=None, max_gap_s=None):
    """Повертає (новий стан, дія або None)."""
    high = config.CO2_HIGH if high is None else high
    low = config.CO2_LOW if low is None else low
    hold = timedelta(seconds=config.HOLD_S if hold_s is None else hold_s)
    max_gap = timedelta(seconds=config.MAX_GAP_S if max_gap_s is None else max_gap_s)

    if value is None:
        return state, None
    if state.last_at is not None and at < state.last_at:
        return state, None                       # запізніла подія не відкочує час
    high_since = state.high_since
    if high_since is not None and state.last_at is not None and at - state.last_at > max_gap:
        high_since = None                        # розрив: безперервність не доведена

    if state.on:
        if value < low:
            return VentState(False, None, at), "ventilation_off"
        return VentState(True, None, at), None

    if value > high:
        high_since = high_since or at
        if at - high_since >= hold:
            return VentState(True, None, at), "ventilation_on"
        return VentState(False, high_since, at), None
    return VentState(False, None, at), None
