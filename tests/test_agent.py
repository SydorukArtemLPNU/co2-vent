"""Агент: застосування, ідемпотентність, чужий пристрій, прострочення, рестарт."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import config
from agent import Agent
from contract import encode, make_command
from helpers import Clock

COMMAND = config.topic("command")


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "agent.db")
        self.clock = Clock()
        self.acks = []
        self.agent = self.make()

    def tearDown(self):
        self.agent.db.close()
        self.tmp.cleanup()

    def make(self):
        return Agent(sqlite3.connect(self.path), lambda t, p: self.acks.append(json.loads(p)), now=self.clock)

    def command(self, action="ventilation_on", device=None):
        return make_command(device or config.DEVICE_ID, action, self.clock(), ttl_s=10)

    def send(self, command, topic=COMMAND):
        return self.agent.handle_command(topic, encode(command).encode())

    def applied_count(self):
        return self.agent.db.execute("SELECT COUNT(*) FROM processed WHERE status='applied'").fetchone()[0]

    def test_apply_then_ack(self):
        self.assertEqual(self.agent.ventilation, "off")
        ack = self.send(self.command())
        self.assertEqual((ack["status"], ack["ventilation"]), ("applied", "on"))
        self.assertEqual(self.agent.ventilation, "on")

    def test_duplicate_not_applied_again(self):
        command = self.command()
        first = self.send(command)
        self.clock.advance(1)
        second = self.send(command)
        self.assertEqual(self.applied_count(), 1)
        self.assertEqual(second["ack_at"], first["ack_at"])      # попередній результат

    def test_foreign_device_ignored(self):
        foreign = self.command(device="class-99")
        self.assertIsNone(self.send(foreign, topic=config.topic("command", "class-99")))
        self.assertIsNone(self.send(foreign))                     # тема наша, payload чужий
        self.assertEqual((self.acks, self.agent.ventilation), ([], "off"))

    def test_expired_command_not_applied(self):
        command = self.command()
        self.clock.advance(11)
        ack = self.send(command)
        self.assertEqual((ack["status"], ack.get("reason")), ("rejected", "expired"))
        self.assertEqual(self.agent.ventilation, "off")

    def test_state_and_history_survive_restart(self):
        command = self.command()
        self.send(command)
        self.agent.db.close()
        self.agent = self.make()
        self.assertEqual(self.agent.ventilation, "on")
        self.send(command)
        self.assertEqual(self.applied_count(), 1)


if __name__ == "__main__":
    unittest.main()
