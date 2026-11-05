"""Tests for scripts/ticket_facts.py and scripts/ticket_rules.py (in-process mock Sonar, loopback, fake tokens)."""
import datetime as dt
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import mock_sonar  # noqa: E402
import ticket_facts  # noqa: E402
import ticket_rules  # noqa: E402

FIXTURES = SCRIPTS / "demo" / "fixtures"
REPO = SCRIPTS.parent
_STRIP = ("BOUNCE_", "CS_", "IHN_", "SLOW_", "NIGHT_", "WEATHER_", "SYSADMIN_", "PEAK_", "SONAR_", "NOC_", "ORG_")


class Rules(unittest.TestCase):
    def test_every_rule_term_is_still_in_agents_md(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8").lower()
        terms = [*ticket_rules.OOO_SUBJECT, *ticket_rules.OOO_BODY, *ticket_rules.WEATHER, *ticket_rules.NIGHT_PEAK,
                 ticket_rules.PBS_SUBJECT, ticket_rules.NIGHT_CHECK_SUBJECT, ticket_rules.MAINTENANCE_SUBJECT,
                 ticket_rules.SYS_ADMIN_NAME, *(lits[0] for lits in ticket_rules.MARKERS.values())]
        missing = [t for t in terms if t.lower() not in text]
        self.assertEqual(missing, [], "AGENTS.md no longer names these; update scripts/ticket_rules.py to match")

    def test_terms_match_at_word_start_only(self):
        self.assertEqual(ticket_rules.hits("It has been raining all week", ["rain"]), ["rain"])
        self.assertEqual(ticket_rules.hits("the drain and the train", ["rain"]), [])
        self.assertEqual(ticket_rules.hits("checked at 7PM, an  check", ["7pm", "n check"]), ["7pm"])
        self.assertEqual(ticket_rules.hits("wet\n weather", ["wet weather"]), ["wet weather"])


class BuildFacts(unittest.TestCase):
    PROFILE = json.loads((FIXTURES / "profile.demo.json").read_text())

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="facts-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP)}
        env["NOC_DOTENV"] = str(Path(tmp) / "absent.env")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def facts(self, comments, subject="[Alpha] alpha1 pppoe - slow tower", description="<p>Slow &amp; laggy</p>",
              group=None, cats=(201,), now=None):
        ticket = {"id": 91001, "subject": subject, "status": "OPEN", "priority": "MEDIUM", "description": description,
                  "ticket_group": group or {"id": 41, "name": "NOC"},
                  "ticket_categories": {"entities": [{"id": c, "name": str(c)} for c in cats]}}
        return ticket_facts.build_facts(91001, ticket, comments, self.PROFILE, now=now)

    def test_flags_markers_fields_and_trimming(self):
        old = {"created_at": "2026-01-01", "body": "Tower: Alpha | 10.0.0.1<br>Network/Fiber: Charlie<br>"
                                                    "Automated NOC Review — No Network Issue"}
        mid = [{"created_at": f"2026-01-0{i}", "body": f"note {i}"} for i in range(2, 8)]
        new = {"created_at": "2026-01-09", "body": "<b>Direct Path Analysis - Alpha</b> went out during the storm " + "x" * 900}
        f = self.facts([old, *mid, new])
        self.assertEqual(f["ticket"]["description"], "Slow & laggy")
        self.assertEqual(f["tower_hints"]["first_note_fields"]["tower"], "Alpha")
        self.assertEqual(f["tower_hints"]["first_note_fields"]["network_fiber"], "Charlie")
        self.assertEqual((f["tower_hints"]["subject_prefix"], f["tower_hints"]["subject_after_prefix"]),
                         ("Alpha", "alpha1 pppoe - slow tower"))
        fl = f["flags"]
        self.assertEqual(fl["weather_hits"], ["storm"])
        self.assertEqual(fl["markers"]["last_5"], ["path_analysis"])
        self.assertEqual(fl["markers"]["any"], ["ihn_auto_return", "path_analysis"])
        self.assertEqual(fl["categories_present"]["slow_speeds"], True)
        self.assertEqual(fl["categories_present"]["night_peak"], False)
        self.assertFalse(fl["sys_admin"])
        newest = f["comments"]["newest_first"]
        self.assertTrue(newest[0]["body"].startswith("Direct Path Analysis - Alpha"))
        self.assertLessEqual(len(newest[0]["body"]), ticket_facts.COMMENT_CHARS)
        self.assertEqual(f["first_note"]["created_at"], "2026-01-01")

    def test_step0_flags(self):
        f = self.facts([], subject="Automatic reply: Out of Office", description="I am currently traveling",
                       group={"id": 43, "name": "Sys Admin"}, cats=())
        self.assertTrue(f["flags"]["sys_admin"])
        self.assertIn("automatic reply", f["flags"]["ooo_subject_hits"])
        self.assertEqual(f["flags"]["ooo_body_hits"], ["currently traveling"])
        f = self.facts([], subject="Garbage Collect Datastore 'backups' successful", description="successful run")
        self.assertTrue(f["flags"]["pbs_subject_exact"] and f["flags"]["pbs_successful"])
        self.assertFalse(f["flags"]["pbs_failed"])
        self.assertTrue(self.facts([], subject="Scheduled Maintenance: splice")["flags"]["maintenance_subject"])
        self.assertIsNone(self.facts([])["first_note"])

    def test_peak_window_uses_the_configured_zone(self):
        inside = dt.datetime(2026, 11, 5, 2, 30, tzinfo=dt.timezone.utc)    # 20:30 CST
        outside = dt.datetime(2026, 11, 5, 4, 0, tzinfo=dt.timezone.utc)    # 22:00 CST
        self.assertTrue(self.facts([], now=inside)["flags"]["peak_window"]["inside"])
        self.assertFalse(self.facts([], now=outside)["flags"]["peak_window"]["inside"])
        self.assertEqual(self.facts([], now=inside)["flags"]["peak_window"]["window"], "20:00-21:00 America/Chicago")


class AgainstMockSonar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = "demo-" + secrets.token_hex(16)
        cls.srv = mock_sonar.MockSonarServer(FIXTURES, [cls.token], port=0).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.srv.engine.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix="facts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copy(FIXTURES / "profile.demo.json", self.tmp / "profile.json")
        env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIP) and "proxy" not in k.lower()}
        env.update({"NO_PROXY": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1", "SONAR_BASE_URL": self.srv.url,
                    "SONAR_TOKEN": self.token, "NOC_DOTENV": str(self.tmp / "absent.env"),
                    "NOC_PROFILE_FILE": str(self.tmp / "profile.json")})
        self.env = env

    def run_facts(self, *argv, env=None):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "ticket_facts.py"), *argv], capture_output=True,
                              text=True, env=env or self.env, timeout=60)

    def test_enriches_the_batch_file_in_place(self):
        batch = self.tmp / "batch_x.json"
        batch.write_text(json.dumps([{"ticket_id": "91001", "subject": "s"}, {"ticket_id": "91005"},
                                     {"ticket_id": "91003"}, {"ticket_id": "not-a-number"}]))
        os.chmod(batch, 0o600)
        r = self.run_facts("--batch-file", str(batch))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"tickets": 4, "with_facts": 3, "failed": 1})
        items = json.loads(batch.read_text())
        self.assertEqual(items[0]["subject"], "s", "the webhook payload is kept")
        self.assertTrue(items[0]["facts"]["flags"]["categories_present"]["slow_speeds"])
        self.assertEqual(items[0]["facts"]["comments"]["fetched"], 1)
        self.assertTrue(items[1]["facts"]["flags"]["sys_admin"])
        self.assertTrue(items[2]["facts"]["flags"]["pbs_subject_exact"])
        self.assertIn("facts_error", items[3])
        self.assertEqual(stat.S_IMODE(batch.stat().st_mode), 0o600)
        self.assertNotIn(self.token, batch.read_text())
        self.assertEqual([a for a in self.srv.engine.audit_snapshot()["audit"] if a.get("kind") == "mutation"], [])

    def test_single_ticket_and_missing_token(self):
        r = self.run_facts("--ticket", "91006")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["ticket"]["id"], 91006)
        env = dict(self.env)
        env.pop("SONAR_TOKEN")
        batch = self.tmp / "b.json"
        batch.write_text("[]")
        r = self.run_facts("--batch-file", str(batch), env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("SONAR_TOKEN", r.stderr)


if __name__ == "__main__":
    unittest.main()
