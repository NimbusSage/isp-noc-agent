#!/usr/bin/env python3
"""
Tests for scripts/sonar_bounce.py (the autonomous-action gate).

Runs the real script as a subprocess against the in-process scripts/mock_sonar.py
(loopback, fake tokens built at runtime). Every test gets its own temp state dir,
profile and dotenv path, so nothing touches the repo's logs/ or site-pack/.

    python3 -B -m unittest discover -s scripts -p 'test_sonar_bounce.py'
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import mock_sonar  # noqa: E402

FIXTURES = SCRIPTS / "demo" / "fixtures"
BOUNCE = str(SCRIPTS / "sonar_bounce.py")
COMMENT = str(SCRIPTS / "sonar_comment.py")

# Ids used by the demo fixtures (fictional): NOC 41, CS L2 42; slow 201, night 202, cx/th 203.
PROFILE = {"sonar": {"groups": {"noc": {"id": 41}, "cs_level_2": {"id": 42}},
                     "categories": {"slow_speeds": {"id": 201}, "night_peak": {"id": 202},
                                    "cx_th_side_issue": {"id": 203}}}}

_STRIP_PREFIXES = ("BOUNCE_", "CS_", "IHN_", "SLOW_", "NIGHT_", "CORRELATED_", "SONAR_", "NOC_", "BREAKER_",
                   "DECISION_", "ORG_")


class GateBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = "demo-" + secrets.token_hex(16)
        cls.srv = mock_sonar.MockSonarServer(FIXTURES, [cls.token], port=0).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.srv.engine.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="bounce-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.profile_path = self.tmp / "profile.json"
        self.profile_path.write_text(json.dumps(PROFILE))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(_STRIP_PREFIXES) and "proxy" not in k.lower()}
        env.update({
            "NO_PROXY": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1",
            "SONAR_BASE_URL": self.srv.url, "SONAR_TOKEN": self.token,
            "NOC_STATE_DIR": str(self.tmp / "state"),
            "NOC_DOTENV": str(self.tmp / "absent.env"),
            "NOC_PROFILE_FILE": str(self.profile_path),
            "BOUNCE_AUTONOMOUS_ENABLED": "true",
        })
        self.env = env

    def run_bounce(self, *argv, env=None):
        return subprocess.run([sys.executable, "-B", BOUNCE, *argv], capture_output=True, text=True,
                              env=env if env is not None else self.env, cwd=str(self.tmp), timeout=60)

    def audit(self):
        return self.srv.engine.audit_snapshot()["audit"]

    def mutations(self):
        return [a for a in self.audit() if a.get("field") == "updateTicket"]

    def decisions(self):
        out = []
        for p in (self.tmp / "state" / "decisions").glob("*.jsonl"):
            out += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        return out


class TestKillSwitch(GateBase):
    def test_unset_means_off(self):
        del self.env["BOUNCE_AUTONOMOUS_ENABLED"]
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("OFF", r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_empty_and_garbage_mean_off(self):
        for val in ("", "maybe", "0", "false", "FALSE"):
            self.env["BOUNCE_AUTONOMOUS_ENABLED"] = val
            r = self.run_bounce("bounce", "91001", "--confidence", "95")
            self.assertEqual(r.returncode, 4, (val, r.stderr))
        self.assertEqual(self.mutations(), [])

    def test_explicit_true_variants_pass(self):
        for val in ("true", "TRUE", "1", "yes", "on"):
            self.srv.engine.reset()
            self.env["BOUNCE_AUTONOMOUS_ENABLED"] = val
            r = self.run_bounce("bounce", "91001", "--confidence", "95")
            self.assertEqual(r.returncode, 0, (val, r.stderr))

    def test_kill_switch_read_from_dotenv_without_shell(self):
        del self.env["BOUNCE_AUTONOMOUS_ENABLED"]
        marker = self.tmp / "pwned"
        (self.tmp / "absent.env").write_text(
            f"export BOUNCE_AUTONOMOUS_ENABLED=\"true\"\nEVIL=$(touch {marker})\n`touch {marker}`\n")
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(marker.exists(), ".env content must never be executed")


class TestFailClosedBreaker(GateBase):
    def test_breaker_crash_blocks(self):
        # A malformed breaker env var makes circuit_breaker.py exit non-zero (rc 1).
        self.env["BREAKER_MAX_ERROR_RATE"] = "not-a-float"
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_corrupt_breaker_state_blocks(self):
        state = self.tmp / "state"
        state.mkdir()
        (state / "circuit_breaker.json").write_text("{ this is not json")
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("corrupt_state", r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_open_breaker_blocks(self):
        subprocess.run([sys.executable, "-B", str(SCRIPTS / "circuit_breaker.py"), "open", "test"],
                       env=self.env, check=True, capture_output=True)
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_unparseable_breaker_output_blocks(self):
        # Replace the breaker script via a sitecustomize-free route: point sys.executable at a
        # wrapper is overkill; instead run the gate function with a stubbed subprocess.
        sys.path.insert(0, str(SCRIPTS))
        import importlib
        import sonar_bounce
        importlib.reload(sonar_bounce)

        class R:
            returncode = 0
            stdout = "all good\n"
            stderr = ""

        orig = sonar_bounce.subprocess.run
        sonar_bounce.subprocess.run = lambda *a, **k: R()
        try:
            with self.assertRaises(SystemExit) as cm:
                sonar_bounce.breaker_allows()
            self.assertEqual(cm.exception.code, 3)
            R.stdout = '{"bounce_allowed": true}\n'
            sonar_bounce.breaker_allows()  # affirmative result passes
            R.returncode = 7
            with self.assertRaises(SystemExit) as cm:
                sonar_bounce.breaker_allows()
            self.assertEqual(cm.exception.code, 3)

            def boom(*a, **k):
                raise subprocess.TimeoutExpired("x", 1)
            sonar_bounce.subprocess.run = boom
            with self.assertRaises(SystemExit) as cm:
                sonar_bounce.breaker_allows()
            self.assertEqual(cm.exception.code, 3)
        finally:
            sonar_bounce.subprocess.run = orig


class TestConfidence(GateBase):
    def test_missing_confidence_refused(self):
        r = self.run_bounce("bounce", "91001")
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_ihn_threshold_default_80(self):
        low = self.run_bounce("bounce", "91001", "--confidence", "79")
        self.assertEqual(low.returncode, 6, low.stderr)
        self.assertEqual(self.mutations(), [])
        ok = self.run_bounce("bounce", "91001", "--confidence", "80")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(len(self.mutations()), 1)

    def test_ihn_threshold_env_override(self):
        self.env["IHN_TOWER_THRESHOLD"] = "90"
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "85").returncode, 6)
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "90").returncode, 0)

    def test_correlated_threshold_default_75(self):
        self.assertEqual(self.run_bounce("bounce", "91001", "--reason", "CORRELATED",
                                         "--confidence", "74").returncode, 6)
        r = self.run_bounce("bounce", "91001", "--reason", "CORRELATED", "--confidence", "75")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_correlated_uses_its_own_env(self):
        self.env["IHN_TOWER_THRESHOLD"] = "99"          # must not apply to CORRELATED
        self.env["CORRELATED_CONFIDENCE_THRESHOLD"] = "60"
        r = self.run_bounce("bounce", "91001", "--reason", "CORRELATED", "--confidence", "60")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_threshold_env_is_config_error(self):
        self.env["IHN_TOWER_THRESHOLD"] = "eighty"
        r = self.run_bounce("bounce", "91001", "--confidence", "99")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_out_of_range_confidence_is_usage_error(self):
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "101").returncode, 1)
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "abc").returncode, 1)


class TestConfigResolution(GateBase):
    def test_no_ids_anywhere_refuses_exit_4(self):
        self.profile_path.write_text("{}")
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("no built-in default", r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_missing_profile_file_refuses(self):
        self.env["NOC_PROFILE_FILE"] = str(self.tmp / "nope.json")
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_no_hardcoded_org_ids_in_source(self):
        src = Path(BOUNCE).read_text()
        for needle in ("DEFAULT_CX_TH_CATEGORY_ID", "DEFAULT_SLOW_SPEEDS_CATEGORY_ID", "DEFAULT_NIGHT_PEAK_CATEGORY_ID",
                       ", 131)", ", 16)", ", 17)", ", 2)"):
            self.assertNotIn(needle, src)

    def test_categories_resolve_from_profile(self):
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self.mutations()[-1]["after"]
        self.assertEqual(after["ticket_group_id"], 42)
        self.assertEqual(after["ticket_category_ids"], [203])  # 201 (slow) removed, 203 added

    def test_env_beats_profile(self):
        self.env["CS_LEVEL2_GROUP_ID"] = "43"
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations()[-1]["after"]["ticket_group_id"], 43)

    def test_explicit_flags_override_profile(self):
        r = self.run_bounce("bounce", "91001", "--confidence", "95",
                            "--add-categories", "204", "--remove-categories", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations()[-1]["after"]["ticket_category_ids"], [201, 204])

    def test_missing_slow_speeds_with_ihn_defaults_refuses(self):
        prof = json.loads(self.profile_path.read_text())
        del prof["sonar"]["categories"]["slow_speeds"]
        self.profile_path.write_text(json.dumps(prof))
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 4, r.stderr)
        # ...but explicit flags make the profile category unnecessary
        ok = self.run_bounce("bounce", "91001", "--confidence", "95",
                             "--add-categories", "203", "--remove-categories", "201")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_night_peak_is_optional(self):
        prof = json.loads(self.profile_path.read_text())
        del prof["sonar"]["categories"]["night_peak"]
        self.profile_path.write_text(json.dumps(prof))
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_non_numeric_env_id_is_config_error_not_silent_fallback(self):
        self.env["CS_LEVEL2_GROUP_ID"] = "forty-two"
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_correlated_default_changes_group_only(self):
        r = self.run_bounce("bounce", "91001", "--reason", "CORRELATED", "--confidence", "90",
                            "--upstream-id", "91002")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.mutations()[-1]
        self.assertEqual(m["after"]["ticket_group_id"], 42)
        self.assertEqual(m["after"]["ticket_category_ids"], [201])  # untouched
        self.assertNotIn("ticket_category_ids", m["args"]["input"])


class TestDryRunAndValidation(GateBase):
    def test_dry_run_prints_mutation_and_sends_nothing(self):
        r = self.run_bounce("bounce", "91001", "--confidence", "95", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["dry_run"])
        self.assertIn("updateTicket", out["mutation"])
        self.assertIn("$input", out["mutation"])
        self.assertEqual(out["variables"], {"id": 91001, "input": {"ticket_group_id": 42,
                                                                      "ticket_category_ids": [203]}})
        self.assertEqual(self.mutations(), [])

    def test_dry_run_still_runs_gates(self):
        del self.env["BOUNCE_AUTONOMOUS_ENABLED"]
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "95", "--dry-run").returncode, 4)
        self.env["BOUNCE_AUTONOMOUS_ENABLED"] = "true"
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "10", "--dry-run").returncode, 6)

    def test_ticket_id_must_be_an_integer(self):
        for bad in ("91001; drop", "9100x", "-5", "0", "1e3", "abc", "9" * 25):
            r = self.run_bounce("bounce", bad, "--confidence", "95")
            self.assertEqual(r.returncode, 1, (bad, r.stderr))
        self.assertEqual(self.audit(), [])

    def test_graphql_is_variable_based(self):
        # The query text is a fixed string; hostile evidence/ids never reach it.
        r = self.run_bounce("bounce", "91001", "--confidence", "95",
                            "--evidence", '"} } mutation { evil } #,tower:ok')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.mutations()), 1)
        rec = [d for d in self.decisions() if d.get("action") == "BOUNCED"][0]
        self.assertIn('"} } mutation { evil } #', rec["evidence"])

    def test_check_action(self):
        r = self.run_bounce("check", "91001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutations(), [])
        del self.env["BOUNCE_AUTONOMOUS_ENABLED"]
        self.assertEqual(self.run_bounce("check", "91001").returncode, 4)

    def test_usage_error_exit_is_1_not_2(self):
        r = self.run_bounce("bounce")
        self.assertEqual(r.returncode, 1)


class TestAntiLoopAndLogging(GateBase):
    def post_marker(self, text):
        f = self.tmp / "c.html"
        f.write_text(f"<p><strong>Direct Path Analysis - Alpha</strong></p><p>{text}</p><p>padding padding padding</p>")
        r = subprocess.run([sys.executable, "-B", COMMENT, "91001", str(f)], capture_output=True, text=True,
                           env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_anti_loop_marker_blocks_with_exit_5(self):
        self.post_marker("Automated NOC Review - No Network Issue")
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(self.mutations(), [])
        self.assertTrue(any(d.get("action") == "BLOCKED_ANTI_LOOP" for d in self.decisions()))

    def test_correlated_marker_blocks_too(self):
        self.post_marker("Correlated Upstream Issue - Auto-Returned to CS")
        self.assertEqual(self.run_bounce("bounce", "91001", "--confidence", "95").returncode, 5)

    def test_bounce_is_logged(self):
        r = self.run_bounce("bounce", "91001", "--confidence", "91", "--evidence", "tower:note,hops:5")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = [d for d in self.decisions() if d.get("action") == "BOUNCED"][0]
        self.assertEqual((rec["ticket_id"], rec["confidence"], rec["target_group"], rec["final_categories"]),
                         ("91001", 91, 42, [203]))

    def test_unknown_ticket_is_api_error(self):
        r = self.run_bounce("bounce", "99999", "--confidence", "95")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.mutations(), [])

    def test_bad_token_is_api_error_exit_2(self):
        self.env["SONAR_TOKEN"] = "demo-" + secrets.token_hex(8)
        r = self.run_bounce("bounce", "91001", "--confidence", "95")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn(self.env["SONAR_TOKEN"], r.stderr)
        self.assertEqual(self.mutations(), [])


class _NullDataHandler(BaseHTTPRequestHandler):
    mode = "null"

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0") or 0)
        self.rfile.read(n)
        if self.mode == "null":
            body = json.dumps({"data": None}).encode()
        elif self.mode == "empty":
            body = json.dumps({}).encode()
        else:
            body = b"<html>oops</html>"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


class TestNullDataHandling(GateBase):
    def serve(self, mode):
        handler = type("H", (_NullDataHandler,), {"mode": mode})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        self.env["SONAR_BASE_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}"

    def test_data_null_is_clean_api_error_not_traceback(self):
        for mode in ("null", "empty", "html"):
            self.serve(mode)
            r = self.run_bounce("bounce", "91001", "--confidence", "95")
            self.assertEqual(r.returncode, 2, (mode, r.stderr))
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
