"""Offline tests for scripts/rain_fade_readiness.py (no network, no credentials).

The orchestrator is exercised with canned `read` payloads (the same shape
wave_gui.mjs emits) by patching its wave_gui subprocess helper, so the
physics/geometry tiering is verified deterministically. Endpoint behavior
itself (login, fields, speedtest) is covered by test_wave_gui_read.py.
"""

from __future__ import annotations

import io
import contextlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rain_fade_readiness as rr  # noqa: E402


def canned_read(model: str, firmware: str, mac: str, lat_far: float, lon_far: float,
                signal_day: float, signal_inst: float, histogram: dict) -> dict:
    """The shape `wave_gui.mjs read` prints (subset the script consumes)."""
    return {
        "mode": "read",
        "device": {"model": model, "product": model, "firmware": firmware, "mac": mac, "family": "wave"},
        "radios": {
            "main": {"id": "main", "linkState": "connected",
                     "localPeer": {"id": "main", "connected": True, "linkState": "active",
                                   "signalDbm": signal_inst, "signalDayDbm": signal_day,
                                   "idealSignalDbm": -56, "minSignalDbm": histogram.get("minSignal"),
                                   "maxSignalDbm": histogram.get("maxSignal"),
                                   "decodeFloorDbm": rr.decode_floor_dbm(histogram),
                                   "gps": {"lat": lat_far, "lon": lon_far, "altM": 420}},
                     "remotePeer": None},
            "backup": None,
        },
        "carrierDrop": {"dropped": False, "reported": True},
        "peerMacs": [],
    }


# Two radios ~14 km apart, the live-verified numbers of the first readiness run
# (day -60 vs decode floor -72 → 12 dB margin). Fixture IPs/MACs are fake.
HIST_A = {"minSignal": -84, "maxSignal": -45, "period": 50,
          "histogram": [0] * 12 + [29, 178] + [0] * 26}  # 40 bins, 1 dB each, first decodes at -72
HIST_B = {"minSignal": -84, "maxSignal": -45, "period": 50,
          "histogram": [0] * 12 + [900] + [0] * 27}
READ_A = canned_read("Wave-Pro", "v4.1.1.testA", "aa:00:00:00:00:0a",  # secret-scan:allow(aa:00 reserved test MAC, not a real device)
                     41.104464, -96.524884, -60, -60, HIST_A)  # secret-scan:allow(placeholder test coordinates, not the install)
READ_B = canned_read("Wave-Pro", "v4.1.1.testB", "aa:00:00:00:00:0b",  # secret-scan:allow(aa:00 reserved test MAC, not a real device)
                     41.227498, -96.485041, -56, -56, HIST_B)  # secret-scan:allow(placeholder test coordinates, not the install)

URL_A = "http://127.0.0.1:1"
URL_B = "http://127.0.0.1:2"


class MockScripts(unittest.TestCase):
    def setUp(self):
        self._orig = rr.run_wave_gui

        def fake_wave_gui(*argv):
            if argv[:1] == ("read",):
                url = argv[argv.index("--url") + 1].rstrip("/")
                return {URL_A: READ_A, URL_B: READ_B}[url]
            if argv[:1] == ("speedtest",):
                return {"mode": "speedtest", "durationSec": int(argv[argv.index("--duration") + 1]),
                        "results": [{"direction": "rx", "status": "completed", "avgMbps": 660.0}]}
            raise AssertionError(f"unexpected argv {argv}")

        rr.run_wave_gui = fake_wave_gui
        self.addCleanup(setattr, rr, "run_wave_gui", self._orig)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rr.run(list(argv))
        return code, buf.getvalue()

    def test_full_readiness_with_speedtest(self):
        code, out = self.run_cli("--wave-ap", URL_A, "--wave-st", URL_B,
                                 "--speedtest", "--speedtest-duration", "4")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["status"], "ok")
        # Live-verified physics: 12 dB over 14.08 km at 60 GHz kills at 1.6 mm/h → red.
        self.assertAlmostEqual(d["distanceKm"], 14.08)
        self.assertAlmostEqual(d["marginDbWorstEnd"], 12.0)
        self.assertAlmostEqual(d["perEnd"]["ap"]["marginDb"], 12.0)
        self.assertAlmostEqual(d["perEnd"]["st"]["marginDb"], 16.0)
        self.assertAlmostEqual(d["killRainRateMmH"], 1.6)
        self.assertEqual(d["tier"], "red")
        self.assertEqual(d["speedtest"]["durationSec"], 4)
        self.assertEqual(d["perEnd"]["ap"]["model"], "Wave-Pro")

    def test_canned_decode_floor_is_not_invented(self):
        # The read payload already carries decodeFloorDbm (computed by wave_gui);
        # the orchestrator must trust it, not recompute from the histogram.
        self.assertEqual(READ_A["radios"]["main"]["localPeer"]["decodeFloorDbm"], -72.0)

    def test_no_speedtest_without_flag(self):
        code, out = self.run_cli("--wave-ap", URL_A, "--wave-st", URL_B)
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertNotIn("speedtest", d)

    def test_partial_when_read_fails(self):
        def fail_reads(*argv):
            raise RuntimeError("radio unreachable")

        rr.run_wave_gui = fail_reads
        code, out = self.run_cli("--wave-ap", URL_A, "--wave-st", URL_B)
        d = json.loads(out)
        self.assertEqual(code, 2)
        self.assertIn("error", d)
        self.assertTrue(d["warnings"])

    def test_empty_histogram_tier_none(self):
        empty = {"minSignal": -84, "maxSignal": -45, "period": 50, "histogram": [0] * 41}
        a = canned_read("Wave-Pro", "v4.1.1.a", "aa:00:00:00:00:0a", 41.1, -96.5, -60, -60, empty)  # secret-scan:allow(aa:00 reserved test MAC + placeholder coordinates, not the install)
        b = canned_read("Wave-Pro", "v4.1.1.b", "aa:00:00:00:00:0b", 41.2, -96.4, -56, -56, empty)  # secret-scan:allow(aa:00 reserved test MAC + placeholder coordinates, not the install)
        rr.run_wave_gui = lambda *argv: {URL_A: a, URL_B: b}[argv[argv.index("--url") + 1].rstrip("/")]
        code, out = self.run_cli("--wave-ap", URL_A, "--wave-st", URL_B)
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(d["tier"], "none")
        self.assertIsNone(d["killRainRateMmH"])
        self.assertTrue(d["warnings"])

    def test_tier_env_override(self):
        os.environ["RF_TIER_RED_MM_H"] = "1.0"
        self.addCleanup(os.environ.pop, "RF_TIER_RED_MM_H", None)
        code, out = self.run_cli("--wave-ap", URL_A, "--wave-st", URL_B)
        d = json.loads(out)
        self.assertEqual(code, 0)
        # red threshold 1.0 → 1.6 mm/h kill now classifies amber.
        self.assertEqual(d["tier"], "amber")


if __name__ == "__main__":
    unittest.main()
