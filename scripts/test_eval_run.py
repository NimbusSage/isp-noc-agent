#!/usr/bin/env python3
"""
Tests for evals/eval_run.py: baseline protection, output-dir override, first-run behaviour and the
--replay spawn contract. Output always goes to a temp dir (NOC_EVAL_DIR / --out).

    python3 -B -m unittest discover -s scripts -p 'test_eval_run.py'
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
EVAL = str(REPO / "evals" / "eval_run.py")

TICKETS = [
    {"ticket_id": "1", "subject": "Garbage Collect Datastore 'backups' successful", "description_snippet": "",
     "expected": {"classification_category": "passthrough", "expected_status": "auto_closed", "derived_from": "sonar"}},
    {"ticket_id": "2", "subject": "[Alpha] alpha pppoe - slow tower", "description_snippet": "very slow speeds",
     "expected": {"classification_category": "slow_speeds", "expected_status": "categorized", "derived_from": "sonar"}},
    {"ticket_id": "3", "subject": "Out of office: back Monday", "description_snippet": "automatic reply",
     "expected": {"classification_category": "passthrough", "expected_status": "auto_closed_ooo", "derived_from": "sonar"}},
    {"ticket_id": "4", "subject": "Rain fade on the Bravo link", "description_snippet": "storm",
     "expected": {"classification_category": "rain_fade", "expected_status": "needs_human", "derived_from": "sonar"}},
]


def broken(tickets):
    """Same tickets, labels wrong for half of them -> accuracy 50%."""
    out = json.loads(json.dumps(tickets))
    out[0]["expected"]["classification_category"] = "outage"
    out[1]["expected"]["classification_category"] = "outage"
    return out


class EvalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"
        self.golden = self.tmp / "golden.json"
        self.write_golden(TICKETS)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("EVAL_", "NOC_", "DRY_RUN", "OPENCODE"))}
        self.env.update({"NOC_EVAL_DIR": str(self.out), "PYTHONDONTWRITEBYTECODE": "1",
                         "NOC_STATE_DIR": str(self.tmp / "state")})
        self.repo_logs_existed = (REPO / "logs").exists()
        self.addCleanup(self.assert_repo_logs_untouched)

    def assert_repo_logs_untouched(self):
        if not self.repo_logs_existed:
            self.assertFalse((REPO / "logs").exists(), "eval_run wrote into the repo's logs/")

    def write_golden(self, tickets, path=None):
        (path or self.golden).write_text(json.dumps({"tickets": tickets}))

    def run_eval(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", EVAL, "--golden", str(self.golden), *args],
                              capture_output=True, text=True, env=env or self.env, timeout=120)

    def summary(self, r):
        start = r.stdout.index('{\n  "ran_at"')          # the example-set run prints a one-line note first
        return json.loads(r.stdout[start:r.stdout.rindex("}") + 1])

    def read(self, name):
        return json.loads((self.out / name).read_text())


class TestBaseline(EvalBase):
    def test_first_run_without_baseline_succeeds_and_does_not_invent_one(self):
        r = self.run_eval()
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.summary(r)
        self.assertEqual((s["baseline"], s["regression_detected"], s["deterministic_accuracy"]), ("none", False, 100.0))
        self.assertTrue((self.out / "latest.json").exists())
        self.assertFalse((self.out / "baseline.json").exists())
        self.assertTrue(any(p.name.startswith("eval_2") for p in self.out.iterdir()))

    def test_update_baseline_creates_it(self):
        r = self.run_eval("--update-baseline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("baseline.json")["deterministic_accuracy"], 100.0)
        self.assertTrue(self.summary(r)["baseline_updated"])

    def test_regressed_run_does_not_overwrite_the_baseline(self):
        self.run_eval("--update-baseline")
        good = (self.out / "baseline.json").read_text()
        self.write_golden(broken(TICKETS))
        r = self.run_eval()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REGRESSION", r.stderr)
        self.assertEqual((self.out / "baseline.json").read_text(), good)        # untouched
        self.assertEqual(self.read("latest.json")["deterministic_accuracy"], 50.0)
        self.assertTrue(self.read("latest.json")["regression_detected"])
        # ...and it keeps failing until someone decides, instead of the bad week becoming normal
        self.assertEqual(self.run_eval().returncode, 2)

    def test_explicit_update_accepts_a_regression(self):
        self.run_eval("--update-baseline")
        self.write_golden(broken(TICKETS))
        r = self.run_eval("--update-baseline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("baseline.json")["deterministic_accuracy"], 50.0)
        self.assertEqual(self.run_eval().returncode, 0)

    def test_small_drop_within_tolerance_passes(self):
        many = [dict(TICKETS[i % 4], ticket_id=str(i)) for i in range(100)]
        self.write_golden(many)
        self.run_eval("--update-baseline")
        worse = json.loads(json.dumps(many))
        worse[0]["expected"]["classification_category"] = "outage"             # -1 point
        self.write_golden(worse)
        r = self.run_eval(env=dict(self.env, EVAL_MAX_REGRESSION_PCT="2"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_eval(env=dict(self.env, EVAL_MAX_REGRESSION_PCT="0.5"))
        self.assertEqual(r.returncode, 2)

    def test_baseline_of_a_different_golden_set_is_not_compared(self):
        self.run_eval("--update-baseline")
        other = self.tmp / "other.json"
        self.write_golden(broken(TICKETS), other)
        r = subprocess.run([sys.executable, "-B", EVAL, "--golden", str(other)], capture_output=True, text=True,
                           env=self.env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.summary(r)["regression_points"])

    def test_legacy_latest_file_acts_as_read_only_baseline(self):
        self.out.mkdir()
        legacy = self.out / "eval_latest_deterministic.json"
        legacy.write_text(json.dumps({"golden_set": str(self.golden), "deterministic_accuracy": 100.0}))
        before = legacy.read_text()
        self.write_golden(broken(TICKETS))
        r = self.run_eval()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.summary(r)["baseline"], "eval_latest_deterministic.json")
        self.assertEqual(legacy.read_text(), before)                           # never rewritten
        self.assertFalse((self.out / "baseline.json").exists())

    def test_corrupt_baseline_is_ignored_with_a_warning(self):
        self.out.mkdir()
        (self.out / "baseline.json").write_text("{nope")
        r = self.run_eval()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unreadable", r.stderr)

    def test_out_flag_overrides_env(self):
        alt = self.tmp / "alt"
        r = self.run_eval("--out", str(alt))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((alt / "latest.json").exists())
        self.assertFalse((self.out / "latest.json").exists())

    def test_default_dir_follows_state_dir(self):
        env = {k: v for k, v in self.env.items() if k != "NOC_EVAL_DIR"}
        r = self.run_eval(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.tmp / "state" / "evals" / "latest.json").exists())


FAKE_OPENCODE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_OC_LOG"], "w") as f:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "dry_run": os.environ.get("DRY_RUN")}}, f)
print(json.dumps({{"classification": {{"category": "passthrough"}}, "expected_status": "auto_closed", "confidence": 90}}))
"""


class TestReplay(EvalBase):
    def test_replay_refused_without_dry_run(self):
        r = self.run_eval("--replay")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DRY_RUN=1", r.stderr)

    def test_replay_does_not_pass_the_removed_dir_flag_and_uses_cwd(self):
        fake = self.tmp / "fake_opencode"
        fake.write_text(FAKE_OPENCODE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        log = self.tmp / "oc.json"
        env = dict(self.env, DRY_RUN="1", OPENCODE_PATH=str(fake), FAKE_OC_LOG=str(log))
        r = self.run_eval("--replay", "--limit", "1", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        call = json.loads(log.read_text())
        self.assertNotIn("--dir", call["argv"])
        self.assertEqual(call["argv"][0], "run")
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(REPO))
        self.assertEqual(call["dry_run"], "1")
        self.assertEqual(self.summary(r)["replay_accuracy"], 100.0)


class TestShippedExampleSet(EvalBase):
    def test_ci_invocation_on_the_example_set_passes_with_no_baseline(self):
        r = subprocess.run([sys.executable, "-B", EVAL], capture_output=True, text=True, env=self.env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        s = self.summary(r)
        self.assertEqual(s["baseline"], "none")
        self.assertGreater(s["deterministic_accuracy"], 90)


if __name__ == "__main__":
    unittest.main()
