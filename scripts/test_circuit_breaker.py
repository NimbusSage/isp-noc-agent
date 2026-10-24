#!/usr/bin/env python3
"""
Tests for scripts/circuit_breaker.py, scripts/decision_log.py and scripts/statefile.py.

All state lives in a per-test temp dir (NOC_STATE_DIR); nothing touches the repo's logs/.

    python3 -B -m unittest discover -s scripts -p 'test_circuit_breaker.py'
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import statefile  # noqa: E402

BREAKER = str(SCRIPTS / "circuit_breaker.py")
DLOG = str(SCRIPTS / "decision_log.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="breaker-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BREAKER_", "NOC_", "DECISION_"))}
        env.update({"NOC_STATE_DIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1",
                    "BREAKER_MIN_RUNS": "5", "BREAKER_MAX_ERROR_RATE": "0.2"})
        self.env = env
        self.state = self.tmp / "circuit_breaker.json"

    def cb(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", BREAKER, *args], capture_output=True, text=True,
                              env=env or self.env, timeout=60)

    def status(self):
        r = self.cb("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class TestBreakerBasics(Base):
    def test_fresh_state_allows_bounces_and_prints_allow_token(self):
        r = self.cb("assert-bounce")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), {"bounce_allowed": True})

    def test_manual_open_blocks_and_clear_reenables(self):
        self.assertEqual(self.cb("open", "drill").returncode, 0)
        r = self.cb("assert-bounce")
        self.assertEqual(r.returncode, 3)
        self.assertIn("drill", r.stderr)
        self.assertEqual(self.cb("clear").returncode, 0)
        self.assertEqual(self.cb("assert-bounce").returncode, 0)

    def test_invalid_outcome_and_unknown_action(self):
        self.assertEqual(self.cb("record", "banana").returncode, 1)
        self.assertEqual(self.cb("frobnicate").returncode, 1)

    def test_bad_numeric_env_exits_nonzero(self):
        self.env["BREAKER_MAX_ERROR_RATE"] = "lots"
        r = self.cb("assert-bounce")
        self.assertEqual(r.returncode, 1)
        self.assertIn("BREAKER_MAX_ERROR_RATE", r.stderr)

    def test_state_file_is_private_and_leaves_no_temp_files(self):
        self.cb("record", "auto_actions_ok", "1")
        self.assertEqual(oct(self.state.stat().st_mode & 0o777), "0o600")
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_legacy_env_names_still_choose_the_state_dir(self):
        alt = self.tmp / "legacy"
        env = {k: v for k, v in self.env.items() if k != "NOC_STATE_DIR"}
        env["NOC_AGENT_LOG_DIR"] = str(alt)
        self.assertEqual(self.cb("record", "auto_actions_ok", "1", env=env).returncode, 0)
        self.assertTrue((alt / "circuit_breaker.json").exists())
        env2 = dict(self.env, BREAKER_STATE_FILE=str(self.tmp / "custom.json"))
        self.cb("record", "auto_actions_ok", "2", env=env2)
        self.assertTrue((self.tmp / "custom.json").exists())


class TestTripAlert(Base):
    def test_trip_prints_machine_readable_json_on_stdout(self):
        for i in range(4):
            self.cb("record", "auto_actions_ok", str(i))
        r = self.cb("record", "analysis_failed", "900")
        # 1 failure / 5 runs = 20%: not > 0.2
        self.assertNotIn("CIRCUIT BREAKER OPENED", r.stdout)
        r = self.cb("record", "analysis_failed", "901")   # 2/6 = 33% > 20% -> trips
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(x) for x in r.stdout.strip().splitlines()]
        trip = lines[0]
        self.assertEqual((trip["event"], trip["breaker_open"], trip["tripped"]),
                         ("CIRCUIT BREAKER OPENED", True, True))
        self.assertEqual(trip["trigger_ticket_id"], "901")
        self.assertIn("failure rate", trip["reason"])
        self.assertIn("CIRCUIT BREAKER OPENED", r.stdout)   # the substring server/index.js greps for
        self.assertIn("CIRCUIT BREAKER OPENED", r.stderr)    # human text stays on stderr
        self.assertTrue(lines[-1]["breaker_open"])           # normal summary line still last
        self.assertEqual(self.cb("assert-bounce").returncode, 3)

    def test_trip_alert_fires_once(self):
        for i in range(6):
            self.cb("record", "analysis_failed", str(i))
        again = self.cb("record", "analysis_failed", "99")
        self.assertNotIn("CIRCUIT BREAKER OPENED", again.stdout)
        self.assertTrue(json.loads(again.stdout.strip().splitlines()[-1])["breaker_open"])


class TestCorruptState(Base):
    def test_corrupt_file_reads_as_open_with_reason(self):
        self.state.write_text('{"runs": [ truncated')
        st = self.status()
        self.assertTrue(st["breaker_open"])
        self.assertTrue(st["opened_reason"].startswith("corrupt_state"))
        r = self.cb("assert-bounce")
        self.assertEqual(r.returncode, 3)
        self.assertIn("corrupt_state", r.stderr)
        # read-only commands must not destroy the evidence
        self.assertEqual(self.state.read_text(), '{"runs": [ truncated')

    def test_wrong_shapes_are_corrupt(self):
        for bad in ('[]', '"x"', '{"runs": null}', '{"runs": [1]}', '{"runs": [{"outcome": "x", "ts": "soon"}]}'):
            self.state.write_text(bad)
            self.assertTrue(self.status()["breaker_open"], bad)

    def test_record_quarantines_backup_pages_and_stays_open(self):
        self.state.write_text("garbage")
        r = self.cb("record", "auto_actions_ok", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        backups = list(self.tmp.glob("circuit_breaker.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "garbage")
        self.assertIn("CIRCUIT BREAKER OPENED", r.stdout)            # operator gets paged
        st = self.status()
        self.assertTrue(st["breaker_open"])
        self.assertIn("corrupt_state", st["opened_reason"])
        self.assertEqual(self.cb("assert-bounce").returncode, 3)      # still fail closed
        # second record: state is valid now, no repeated page, still open
        r2 = self.cb("record", "auto_actions_ok", "8")
        self.assertNotIn("CIRCUIT BREAKER OPENED", r2.stdout)
        self.assertTrue(self.status()["breaker_open"])

    def test_clear_is_the_only_way_back(self):
        self.state.write_text("garbage")
        self.assertEqual(self.cb("clear").returncode, 0)
        self.assertEqual(len(list(self.tmp.glob("circuit_breaker.json.corrupt-*"))), 1)
        self.assertEqual(self.cb("assert-bounce").returncode, 0)

    def test_missing_file_is_a_normal_new_install(self):
        self.assertFalse(self.status()["breaker_open"])


class TestConcurrency(Base):
    def test_parallel_records_do_not_lose_or_corrupt_state(self):
        n = 16
        env = dict(self.env, BREAKER_MIN_RUNS="1000")
        procs = [subprocess.Popen([sys.executable, "-B", BREAKER, "record",
                                   "analysis_failed" if i % 4 == 0 else "auto_actions_ok", str(i)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(n)]
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        data = json.loads(self.state.read_text())            # parses -> not torn
        self.assertEqual(len(data["runs"]), n)               # none lost to a read-modify-write race
        self.assertEqual(sorted(r["ticket_id"] for r in data["runs"]), sorted(str(i) for i in range(n)))
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])


class TestDecisionLog(Base):
    def test_parallel_appends_are_whole_lines(self):
        n = 20
        big = "x" * 9000           # > PIPE_BUF so only locking keeps the lines apart
        procs = [subprocess.Popen([sys.executable, "-B", DLOG, "append", "--json",
                                   json.dumps({"n": i, "pad": big}), "--kind", "batch_result",
                                   "--ticket", str(1000 + i)],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(n)]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        shards = list((self.tmp / "decisions").glob("*.jsonl"))
        self.assertEqual(len(shards), 1)
        lines = shards[0].read_text().splitlines()
        self.assertEqual(len(lines), n)
        self.assertEqual(sorted(json.loads(x)["n"] for x in lines), list(range(n)))
        self.assertEqual(oct(shards[0].stat().st_mode & 0o777), "0o600")

    def test_query_roundtrip_and_legacy_env(self):
        env = {k: v for k, v in self.env.items() if k != "NOC_STATE_DIR"}
        env["NOC_AGENT_LOG_DIR"] = str(self.tmp / "legacy")
        r = subprocess.run([sys.executable, "-B", DLOG, "append", "--json", '{"status":"needs_human"}',
                            "--kind", "batch_result", "--ticket", "5"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        q = subprocess.run([sys.executable, "-B", DLOG, "query", "--kind", "batch_result", "--format", "count"],
                           env=env, capture_output=True, text=True)
        self.assertEqual(q.stdout.strip(), "1")

    def test_bad_payload_is_usage_error(self):
        r = subprocess.run([sys.executable, "-B", DLOG, "append", "--json", "[1]"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)


class TestStatefile(unittest.TestCase):
    def test_atomic_write_replaces_without_leftovers(self):
        d = Path(tempfile.mkdtemp(prefix="statefile-"))
        self.addCleanup(shutil.rmtree, d, True)
        target = d / "sub" / "x.json"
        statefile.atomic_write_text(target, "one")
        statefile.atomic_write_text(target, "two")
        self.assertEqual(target.read_text(), "two")
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["x.json"])
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")

    def test_lock_times_out_when_held(self):
        d = Path(tempfile.mkdtemp(prefix="statefile-"))
        self.addCleanup(shutil.rmtree, d, True)
        with statefile.locked(d / "f"):
            with self.assertRaises(statefile.LockTimeout):
                with statefile.locked(d / "f", timeout=0.2):
                    pass


if __name__ == "__main__":
    unittest.main()
