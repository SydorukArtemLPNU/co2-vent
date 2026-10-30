"""Чисте правило варіанта 20 і агрегація трьох давачів (без БД і MQTT)."""
import unittest
from datetime import timedelta

from helpers import T0
from policy import VentState, advance, aggregate

P = 5.0   # період давача, с


def run(values, state=None, start=T0, period=P, **kw):
    """Подає ряд значень з кроком period; повертає (стан, [(індекс, дія)])."""
    state = state or VentState()
    actions = []
    for index, value in enumerate(values):
        state, action = advance(state, start + timedelta(seconds=index * period), value,
                                hold_s=180, max_gap_s=15, **kw)
        if action:
            actions.append((index, action))
    return state, actions


class HoldRule(unittest.TestCase):
    def test_t1_trigger_exactly_after_180_s(self):
        # перше перевищення - індекс 2; 180 с = 36 періодів -> дія на індексі 38
        _, actions = run([900, 950] + [1001] * 40)
        self.assertEqual(actions, [(38, "ventilation_on")])

    def test_179_s_is_not_enough(self):
        _, actions = run([1200] * 36)              # 0...175 с
        self.assertEqual(actions, [])

    def test_t2_exactly_1000_is_not_exceeding(self):
        _, actions = run([1000.0] * 80)
        self.assertEqual(actions, [])

    def test_value_at_threshold_resets_series(self):
        # 120 с понад поріг, одне значення рівно 1000, знову 120 с - разом > 180 с, але не безперервно
        _, actions = run([1100] * 25 + [1000] + [1100] * 25)
        self.assertEqual(actions, [])

    def test_gap_longer_than_limit_resets_series(self):
        state, _ = run([1100] * 20)                 # 0...95 с
        late = T0 + timedelta(seconds=95 + 20)      # 20 с без валідних даних > 15 с
        state, _ = run([1100] * 30, state=state, start=late)
        self.assertEqual(state.high_since, late)

    def test_short_gap_keeps_series(self):
        state, _ = run([1100] * 20)
        resume = T0 + timedelta(seconds=95 + 10)    # 10 с <= 15 с
        _, actions = run([1100] * 20, state=state, start=resume)
        self.assertEqual(len(actions), 1)

    def test_missing_aggregate_is_not_normal(self):
        state, _ = run([1100] * 10)
        same, action = advance(state, T0 + timedelta(seconds=50), None)
        self.assertEqual((same, action), (state, None))

    def test_late_event_does_not_rewind(self):
        state, _ = run([1100] * 10)
        same, action = advance(state, T0, 500)
        self.assertEqual((same, action), (state, None))


class StopRule(unittest.TestCase):
    def on_state(self):
        return VentState(on=True, last_at=T0 - timedelta(seconds=P))

    def test_t3_stop_only_below_800(self):
        state, actions = run([1100, 999, 900, 800.0, 800.0, 799.9, 700], state=self.on_state())
        self.assertEqual(actions, [(5, "ventilation_off")])
        self.assertFalse(state.on)

    def test_hysteresis_band_keeps_ventilation(self):
        state, actions = run([950, 850, 801, 1000, 820], state=self.on_state())
        self.assertEqual(actions, [])
        self.assertTrue(state.on)


class Aggregate(unittest.TestCase):
    def test_median_of_three(self):
        latest = {s: (T0, v) for s, v in (("scd40-1", 900), ("scd40-2", 1100), ("scd40-3", 1000))}
        self.assertEqual(aggregate(latest, T0, fresh_s=15, min_sensors=2), 1000)

    def test_stuck_sensor_does_not_move_median(self):
        latest = {s: (T0, v) for s, v in (("scd40-1", 700), ("scd40-2", 710), ("scd40-3", 2500))}
        self.assertEqual(aggregate(latest, T0, fresh_s=15, min_sensors=2), 710)

    def test_stale_sensor_excluded_two_left(self):
        latest = {"scd40-1": (T0, 1000), "scd40-2": (T0, 1100),
                  "scd40-3": (T0 - timedelta(seconds=30), 400)}
        self.assertEqual(aggregate(latest, T0, fresh_s=15, min_sensors=2), 1050)

    def test_single_fresh_sensor_gives_no_decision(self):
        latest = {"scd40-1": (T0, 1500), "scd40-2": (T0 - timedelta(seconds=60), 1500)}
        self.assertIsNone(aggregate(latest, T0, fresh_s=15, min_sensors=2))


if __name__ == "__main__":
    unittest.main()
