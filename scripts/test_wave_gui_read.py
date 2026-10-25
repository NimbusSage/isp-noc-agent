"""Tests for scripts/wave_gui.mjs `read` and `speedtest` (HTTP mock, no real radio).

The mock reimplements the live-verified gen3 API surface (2026-10-24, Wave Pro
firmware 4.1.1, an anonymized 60 GHz link pair): POST /api/v1.0/user/login {username,password} → 200
+x-auth-token; GET /statistics /device; the speedtest start/get/stop trio with
directions rx|tx|dx ("all" is rejected, per the API's own error text).
Credentials reach the script only through the environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODE = "node"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wave_gui.mjs")
PEER_MAC = "aa:bb:cc:dd:ee:02"  # referenced by FIXTURE_STATS above the dict body
USER = "ubnt-test"
PASSWORD = "wavegui-test-pass-1"  # secret-scan:allow(fixture credential)

FIXTURE_STATS = {
    "wireless": {
        "linkQuality": {"capacity": {"combined": 1751750, "combinedIdeal": 3200000, "dl": 1751750, "dlIdeal": 2700000, "ul": 1751750, "ulIdeal": 2700000}},
        "radios": [
            {"id": "main", "linkState": "connected", "frequency": {"center": 69120}, "channelWidth": {"tx": 2160},
             "waveAi": True, "serviceUptime": 3381338, "serviceDowntime": 0,
             "outputPower": {"conducted": 20, "eirp": 66}, "antenna": {"name": "WAVE_PRO-60G", "gain": 46, "type": "built_in"}},
            {"id": "backup", "linkState": "disabled", "frequency": {"center": 0}, "channelWidth": {"tx": 0},
             "serviceUptime": 0, "serviceDowntime": 0, "outputPower": {"conducted": 0, "eirp": 22},
             "antenna": {"name": "WAVE-PRO-5G", "gain": 22, "type": "built_in"}},
        ],
        "peers": [{
            "local": [
                {"id": "main", "remoteId": "main", "active": True, "connected": True, "linkState": "active",
                 "connectionTime": 798497,
                 "linkQuality": {"signal": -60, "signalDay": -60, "idealSignal": -56,
                                 "signalHistogram": {"minSignal": -84, "maxSignal": -45, "period": 50,
                                                     "histogram": [0,0,0,0,0,0,0,0,0,0,0,0,29,178,90,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]},
                                 "mcs": {"rxIdx": 9, "txIdx": 9, "rxIdxIdeal": 12, "txIdxIdeal": 12,
                                         "rxLabel": "QPSK", "txLabel": "QPSK"},
                                 "capacity": {"combined": 1751750, "combinedIdeal": 3200000, "dl": 1751750,
                                              "dlIdeal": 2700000, "ul": 1751750, "ulIdeal": 2700000},
                                 "linkScore": {"ul": 76, "dl": 82}, "airtime": {"dl": 15.85, "ul": 6.68}},
                 "gps": {"lat": 41.104477, "lon": -96.524698, "alt": 434.7}},  # secret-scan:allow(placeholder test coordinates, not the install)
            ],
            "remote": [
                {"id": "main", "remoteId": "main", "name": "60 GHz", "connected": True, "mac": PEER_MAC,
                 "linkQuality": {"signal": -55, "idealSignal": -56,
                                 "mcs": {"rxIdx": 9, "txIdx": 9}},
                 "gps": {"lat": 41.22734, "lon": -96.485238, "alt": 428.0}},  # secret-scan:allow(placeholder test coordinates, not the install)
            ],
        }],
        "carrierDrop": {"dropped": False},
    }
}
FIXTURE_DEVICE = {"identification": {"family": "wave", "model": "Wave-Pro", "product": "Wave Pro",
                                     "firmware": "MGMP.ipq807x.v4.1.1.test", "mac": "aa:bb:cc:dd:ee:01"}}

TOKEN = "tok0123456789tok0123456789tok012345"  # secret-scan:allow(fixture token)


def make_handler(state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def send_json(self, code, obj, headers=()):
            body = json.dumps(obj).encode()
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode() or "{}")

        def do_POST(self):
            if self.path == "/api/v1.0/user/login":
                data = self.read_body()
                if data.get("username") == USER and data.get("password") == PASSWORD:
                    self.send_json(200, {"statusCode": 200, "error": 0}, [("x-auth-token", TOKEN)])
                else:
                    self.send_json(401, {"error": 1, "detail": "invalid credentials"})
                return
            if self.path == "/api/v1.0/tools/airos/speedtest/start":
                data = self.read_body()
                if self.headers.get("x-auth-token") != TOKEN:
                    self.send_json(401, {"error": 1}); return
                if data.get("direction") not in ("rx", "tx", "dx") or not data.get("mac"):
                    self.send_json(400, {"error": 1, "detail": "Bad direction specified, should be rx, tx or dx"})
                    return
                state["direction"] = data.get("direction", "rx")
                state["started_ms"] = state.get("now_ms", 0)
                state["running"] = True
                state["completed"] = False
                self.send_json(200, {"statusCode": 200, "error": 0})
                return
            if self.path == "/api/v1.0/tools/airos/speedtest/stop":
                if self.headers.get("x-auth-token") != TOKEN:
                    self.send_json(401, {"error": 1}); return
                state["running"] = False
                self.send_json(200, {"statusCode": 200, "error": 0})
                return
            self.send_json(404, {"error": 1})

        def do_GET(self):
            tok = self.headers.get("x-auth-token")
            if self.path == "/api/v1.0/statistics":
                if tok != TOKEN: self.send_json(401, {"error": 1}); return
                self.send_json(200, [FIXTURE_STATS]); return
            if self.path == "/api/v1.0/device":
                if tok != TOKEN: self.send_json(401, {"error": 1}); return
                self.send_json(200, FIXTURE_DEVICE); return
            if self.path == "/api/v1.0/tools/airos/speedtest":
                if tok != TOKEN: self.send_json(401, {"error": 1}); return
                direction = state.get("direction") or "rx"
                rx_field, tx_field = (1, 0) if direction in ("rx", "dx") else (0, 1)
                obj = {"status": "completed" if state.get("completed") else "running",
                       "duration": 5.0 if state.get("completed") else 0.0,
                       "avg_rx": 400.1 * rx_field, "max_rx": 405.0 * rx_field,
                       "avg_tx": 399.2 * tx_field, "max_tx": 404.0 * tx_field,
                       "rx_speed": [0, 400] if rx_field else [], "tx_speed": [0, 399] if tx_field else []}
                self.send_json(200, obj); return
            self.send_json(404, {"error": 1})

    return Handler


class MockWave(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(os.path.join(os.path.dirname(SCRIPT), "..", "mcp-server", "dist", "security", "hosts.js")):
            raise unittest.SkipTest("the host policy lives in the compiled MCP server: cd mcp-server && npm ci && npm run build")
        cls.state = {"running": False, "completed": False, "direction": "rx", "now_ms": 0}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.server = srv
        cls.port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        # The script drives polling itself; the mock simply toggles completed
        # on the FIRST status poll after a start (see `watchdog` thread below).
        def toggle():
            while True:
                if cls.state.get("running") and not cls.state.get("completed"):
                    cls.state["completed"] = True
                threading.Event().wait(0.2)
        threading.Thread(target=toggle, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_cli(self, *argv, user=USER, password=PASSWORD):
        env = dict(os.environ, WAVE_GUI_USER=user, WAVE_GUI_PASS=password, WAVE_GUI_PRETTY="1")
        return subprocess.run([NODE, SCRIPT, *argv], capture_output=True, text=True, env=env, timeout=90)

    def test_read_positive_shape(self):
        r = self.run_cli("read", "--url", f"http://127.0.0.1:{self.port}")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["mode"], "read")
        self.assertEqual(out["device"]["mac"], "aa:bb:cc:dd:ee:01")
        self.assertEqual(out["radios"]["main"]["linkState"], "connected")
        self.assertEqual(out["radios"]["main"]["frequencyCenterMhz"], 69120)
        peer = out["radios"]["main"]["localPeer"]
        self.assertEqual(peer["signalDbm"], -60)
        self.assertEqual(peer["idealSignalDbm"], -56)
        self.assertEqual(peer["mcs"]["rxIdx"], 9)
        self.assertEqual(peer["mcs"]["rxLabel"], "QPSK")
        # Empirical decode floor from the histogram: first nonzero bin (idx 12 of
        # 41, span -84..-45) → -84 + (39/40)*12 = -72.3.
        self.assertAlmostEqual(peer["decodeFloorDbm"], -72.3)
        # kbps → Mbps conversion (verified numerically against live units)
        self.assertEqual(peer["capacityMbps"]["combined"], 1751.75)
        self.assertEqual(peer["capacityMbps"]["combinedIdeal"], 3200.0)
        self.assertEqual(peer["gps"]["lat"], 41.104477)
        self.assertEqual(out["carrierDrop"]["dropped"], False)  # secret-scan:allow(fixtured booleans over the live-verified gen3 API)
        self.assertEqual(out["peerMacs"], [PEER_MAC])  # own MAC excluded, peer listed

    def test_bad_password_exit_1_and_no_secret_leak(self):
        r = self.run_cli("read", "--url", f"http://127.0.0.1:{self.port}", user="wrong", password="nope")  # secret-scan:allow(fake wrong-password fixture; the test asserts the CLI leaks no credential)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        self.assertIn("credential", (r.stderr + r.stdout).lower())

    def test_speedtest_complete_both(self):
        self.state.update({"completed": False, "running": False})
        r = self.run_cli("speedtest", "--url", f"http://127.0.0.1:{self.port}", "--duration", "4")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["mode"], "speedtest")
        self.assertEqual(out["peerMac"], PEER_MAC)  # auto-derived
        dirs = [x["direction"] for x in out["results"]]
        self.assertEqual(dirs, ["rx", "tx"])
        rx = out["results"][0]
        self.assertEqual(rx["status"], "completed")
        self.assertAlmostEqual(rx["avgMbps"], 400.1)
        self.assertAlmostEqual(rx["maxMbps"], 405.0)
        tx = out["results"][1]
        self.assertAlmostEqual(tx["avgMbps"], 399.2)
        self.assertAlmostEqual(tx["maxMbps"], 404.0)

    def test_speedtest_direction_validation(self):
        r = self.run_cli("speedtest", "--url", f"http://127.0.0.1:{self.port}",
                         "--duration", "4", "--direction", "all")
        self.assertEqual(r.returncode, 1)
        self.assertIn("rx, tx or both", r.stderr or "")
        # API-side vocabulary (live-verified) would reject "all" too.

    def test_read_requires_url(self):
        r = self.run_cli("read")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
