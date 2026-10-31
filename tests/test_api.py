"""HTTP API на тимчасовій БД (реальний сервер на випадковому порту)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import api
import config
from helpers import Bench


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Bench()
        cls.b.ticks([900, 950])
        cls.b.send(cls.b.event("scd40-1", "abc"))
        handler = type("H", (api.Handler,), {"db_path": cls.b.db_path})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.b.close()

    def get(self, path, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers or {})) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_latest_and_events(self):
        device = config.DEVICE_ID
        status, latest = self.get(f"/api/devices/{device}/latest")
        self.assertEqual((status, latest["value"]), (200, 950))
        status, events = self.get(f"/api/devices/{device}/events?limit=4")
        self.assertEqual((status, len(events)), (200, 4))
        times = [event["observed_at"] for event in events]
        self.assertEqual(times, sorted(times))                    # часовий порядок
        status, events = self.get(f"/api/devices/{device}/events?limit=10&sensor=scd40-2")
        self.assertEqual({event["sensor_id"] for event in events}, {"scd40-2"})

    def test_state_shows_pending_command(self):
        status, state = self.get(f"/api/devices/{config.DEVICE_ID}/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["ventilation"], "pending")        # off-команда без ACK
        self.assertEqual(state["co2_median"], 950)
        self.assertEqual(state["sensors"]["scd40-1"]["source"], "simulated")

    def test_errors(self):
        self.assertEqual(self.get("/api/devices/class-77/latest")[0], 404)
        self.assertEqual(self.get("/api/devices/class-20/unknown")[0], 404)
        self.assertEqual(self.get("/api/devices/class-20/events?limit=0")[0], 400)
        self.assertEqual(self.get("/api/devices/class-20/events?limit=x")[0], 400)
        self.assertEqual(self.get("/api/devices/class-20/events?sensor=scd40-9")[0], 400)
        self.assertEqual(self.get("/api/devices/bad%20id/latest")[0], 400)

    def test_rejected_listed(self):
        status, rows = self.get("/api/rejected?limit=5")
        self.assertEqual((status, rows[0]["reason"]), (200, "value is not a number"))

    def test_panel_key_required_when_set(self):
        path = f"/api/devices/{config.DEVICE_ID}/latest"
        try:
            self.server.RequestHandlerClass.panel_key = "k1"
            self.assertEqual(self.get(path)[0], 403)
            self.assertEqual(self.get(path, {"X-Panel-Key": "bad"})[0], 403)
            self.assertEqual(self.get(path, {"X-Panel-Key": "k1"})[0], 200)
        finally:
            self.server.RequestHandlerClass.panel_key = None


if __name__ == "__main__":
    unittest.main()
