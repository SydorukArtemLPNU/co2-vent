"""Обробник без брокера: повний цикл подія -> БД -> правило -> команда -> ACK."""
import math
import unittest
from datetime import timedelta

import config
import storage
from api import ventilation_view
from contract import encode, iso, make_ack, make_state
from helpers import ACK, Bench

P = config.SAMPLE_PERIOD_S
HOLD_TICKS = math.ceil(config.HOLD_S / P) + 1     # тактів понад поріг до команди


class Base(unittest.TestCase):
    def setUp(self):
        self.b = Bench()
        self.b.tick(700)       # перша подія: виконавець невідомий -> команда off
        self.b.settle()        # агент підтвердив off

    def tearDown(self):
        self.b.close()

    def view(self):
        db = self.b.db
        return ventilation_view(storage.get_actuator(db, config.DEVICE_ID),
                                storage.last_command(db, config.DEVICE_ID))

    def drive_on(self):
        self.b.ticks([1150] * HOLD_TICKS)
        return self.b.commands("ventilation_on")


class FullCycle(Base):
    def test_safe_state_on_first_event(self):
        b = Bench()
        b.tick(600)
        self.assertEqual([c["action"] for c in b.commands()], ["ventilation_off"])
        b.close()

    def test_t1_one_command_after_hold(self):
        self.b.ticks([1150] * (HOLD_TICKS - 1))
        self.assertEqual(self.b.commands(), [])
        self.assertEqual(self.view(), "off")
        self.b.tick(1150)                          # рівно 180 с від першого перевищення
        commands = self.b.commands("ventilation_on")
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(self.view(), "pending")
        self.assertEqual(self.b.ack(command)["result"], "applied")
        self.assertEqual(self.view(), "on")
        row = storage.get_command(self.b.db, command["command_id"])
        self.assertEqual(row["status"], "applied")
        self.assertIsNotNone(row["trigger_event_id"])
        self.b.ticks([1300] * 10)                 # подальше перевищення - без нових команд
        self.assertEqual(len(self.b.commands()), 1)

    def test_t2_boundary_1000_no_command(self):
        self.b.ticks([1000.0] * (HOLD_TICKS * 2))
        self.assertEqual(self.b.commands(), [])

    def test_t3_off_only_below_800(self):
        self.b.ack(self.drive_on()[0])
        self.b.ticks([950, 850, 800.0, 800.0])
        self.assertEqual(self.b.commands("ventilation_off"), [])
        self.b.tick(790)
        off = self.b.commands("ventilation_off")
        self.assertEqual(len(off), 1)
        self.b.ack(off[0])
        self.assertEqual(self.view(), "off")

    def test_median_ignores_one_faulty_sensor(self):
        # один давач показує 2500, два інші - норма: команди немає
        for _ in range(HOLD_TICKS + 2):
            self.b.send(self.b.event("scd40-1", 700))
            self.b.send(self.b.event("scd40-2", 710))
            self.b.send(self.b.event("scd40-3", 2500))
            self.b.clock.advance(P)
        self.assertEqual(self.b.commands(), [])

    def test_two_sensors_enough_one_is_not(self):
        two = ("scd40-1", "scd40-2")
        self.b.ticks([1150] * HOLD_TICKS, sensors=two)
        self.assertEqual(len(self.b.commands("ventilation_on")), 1)

        other = Bench()
        other.tick(700)
        other.settle()
        other.ticks([1150] * HOLD_TICKS * 2, sensors=("scd40-1",))
        self.assertEqual(other.commands(), [])
        other.close()


class Faults(Base):
    def test_t4_duplicate_event_no_second_record_or_command(self):
        self.b.ticks([1150] * (HOLD_TICKS - 1))
        event = self.b.event("scd40-1", 1150)
        self.b.send(event)
        before = self.b.count("events")
        result = self.b.send(event)                  # повторна доставка QoS 1
        self.assertEqual(result["reason"], "duplicate event_id")
        self.assertEqual(self.b.count("events"), before)
        self.drive_on()
        self.assertEqual(len(self.b.commands("ventilation_on")), 1)

    def test_t5_invalid_value_rejected_state_unchanged(self):
        state = self.b.processor.state
        for bad in ({"value": "abc"}, {"unit": "ppb"}, {"value": 50000.0}):
            result = self.b.send(self.b.event("scd40-2", 900, **bad))
            self.assertEqual(result["result"], "rejected")
        self.assertEqual(self.b.processor.state, state)
        self.assertEqual(self.b.rejected_reasons(),
                         ["value is not a number", "unknown unit", "value out of range"])

    def test_t6_topic_payload_mismatch_logged(self):
        result = self.b.send(self.b.event("scd40-1", 900, device_id="class-99"))
        self.assertEqual(result["reason"], "device_id mismatch with topic")
        row = self.b.db.execute("SELECT topic, payload FROM rejected").fetchone()
        self.assertIn("class-99", row["payload"])

    def test_t7_no_ack_expired_then_unknown(self):
        command = self.drive_on()[0]
        self.b.clock.advance(config.COMMAND_TTL_S + 1)
        self.assertEqual(self.b.processor.sweep(), [command["command_id"]])
        row = storage.get_command(self.b.db, command["command_id"])
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["closed_at"], iso(self.b.clock()))   # момент закриття фіксується (К4)
        self.assertEqual(self.view(), "unknown")            # не "off"
        # запізнілий ACK не робить команду applied
        self.assertEqual(self.b.ack(command)["result"], "late")
        self.assertEqual(storage.get_command(self.b.db, command["command_id"])["status"], "expired")

    def test_view_unknown_right_after_deadline_before_sweep(self):
        command = self.drive_on()[0]
        db = self.b.db
        row = storage.last_command(db, config.DEVICE_ID)
        actuator = storage.get_actuator(db, config.DEVICE_ID)
        self.assertEqual(ventilation_view(actuator, row, self.b.clock()), "pending")
        self.b.clock.advance(config.COMMAND_TTL_S + 0.01)        # sweep() ще не викликався
        self.assertEqual(storage.get_command(db, command["command_id"])["status"], "sent")
        self.assertEqual(ventilation_view(actuator, row, self.b.clock()), "unknown")

    def test_after_timeout_reconcile_issues_new_command(self):
        first = self.drive_on()[0]
        self.b.clock.advance(config.COMMAND_TTL_S + 1)
        self.b.processor.sweep()
        self.b.tick(1200)
        commands = self.b.commands("ventilation_on")
        self.assertEqual(len(commands), 2)
        self.assertNotEqual(commands[1]["command_id"], first["command_id"])

    def test_t9_stale_event_rejected(self):
        old = self.b.clock() - timedelta(seconds=config.MAX_AGE_S * 2)
        result = self.b.send(self.b.event("scd40-3", 1500, observed_at=old.isoformat()))
        self.assertEqual(result["reason"], "stale event")

    def test_out_of_order_rejected(self):
        self.b.tick(900)
        earlier = self.b.clock() - timedelta(seconds=P * 2)
        result = self.b.send(self.b.event("scd40-1", 900, observed_at=earlier.isoformat()))
        self.assertEqual(result["reason"], "out of order for sensor")

    def test_ack_unknown_or_repeated(self):
        fake = make_ack("cmd-unknown", config.DEVICE_ID, "applied", "on", self.b.clock())
        self.assertEqual(self.b.processor.handle_ack(ACK, encode(fake).encode())["result"], "rejected")
        command = self.drive_on()[0]
        self.b.ack(command)
        self.assertEqual(self.b.ack(command)["result"], "duplicate")

    def test_agent_reject_ack(self):
        command = self.drive_on()[0]
        self.b.ack(command, status="rejected", ventilation="off")
        self.assertEqual(storage.get_command(self.b.db, command["command_id"])["status"], "rejected")
        self.assertEqual(self.view(), "unknown")


class Restart(Base):
    def test_t10_rule_state_restored(self):
        half = HOLD_TICKS // 2
        self.b.ticks([1150] * half)
        high_since = self.b.processor.state.high_since
        processor = self.b.restart()
        self.assertEqual(processor.state.high_since, high_since)
        self.b.ticks([1150] * (HOLD_TICKS - half))        # відлік продовжено, не з нуля
        self.assertEqual(len(self.b.commands("ventilation_on")), 1)

    def test_created_command_resent_after_crash(self):
        def crash(topic, payload):
            raise ConnectionError("broker down")
        self.b.processor.publish = crash
        with self.assertRaises(ConnectionError):
            self.drive_on()
        row = storage.open_commands(self.b.db, config.DEVICE_ID)[0]
        self.assertEqual(row["status"], "created")
        self.b.restart().recover()
        self.assertEqual(self.b.commands("ventilation_on")[0]["command_id"], row["command_id"])
        self.assertEqual(storage.get_command(self.b.db, row["command_id"])["status"], "sent")


class NodeState(Base):
    def report(self, reason="node_boot", ventilation="off"):
        message = make_state(config.DEVICE_ID, ventilation, reason, self.b.clock())
        return self.b.processor.handle_state(config.topic("state"), encode(message).encode())

    def test_uno_reset_restores_ventilation(self):
        self.b.ack(self.drive_on()[0])
        self.assertEqual(self.view(), "on")
        self.b.clock.advance(1)
        result = self.report()                    # Uno скинувся - реле фізично вимкнене
        self.assertIsNotNone(result["command"])   # бажаний on -> нова команда одразу
        again = self.b.commands("ventilation_on")
        self.assertEqual(len(again), 2)
        self.assertEqual(self.view(), "pending")

    def test_sync_report_consistent_state_no_command(self):
        self.assertIsNone(self.report("node_sync", "off")["command"])

    def test_bad_state_report_rejected(self):
        message = make_state("class-99", "off", "node_boot", self.b.clock())
        result = self.b.processor.handle_state(config.topic("state"), encode(message).encode())
        self.assertEqual(result["result"], "rejected")


if __name__ == "__main__":
    unittest.main()
