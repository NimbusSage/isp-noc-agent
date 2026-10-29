"""Tests for scripts/team.py (temporary roster and user config; no real settings are read)."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team  # noqa: E402

ROSTER = {
    "version": 1,
    "members": {
        "ada": {"name": "Ada Lead", "role": "lead", "active": True},
        "tom": {"name": "Tom Tech", "role": "tech", "active": True},
        "vic": {"name": "Vic Viewer", "role": "viewer", "active": True},
        "gone": {"name": "Gone Away", "role": "tech", "active": False},
    },
    "pools": {"mikrotik-ssh": {"scope": "shared"}},
    "approved_models": ["opencode/big-pickle"],
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.roster = self.root / "team.json"
        self.user = self.root / "user.json"
        env = {k: v for k, v in os.environ.items() if k not in ("NOC_OPERATOR", "NOC_UNATTENDED")}
        env.update({"NOC_TEAM_FILE": str(self.roster), "NOC_USER_CONFIG": str(self.user),
                    "NOC_DOTENV": str(self.root / "absent.env")})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_roster(self, data=None):
        self.roster.write_text(json.dumps(ROSTER if data is None else data))

    def as_operator(self, handle):
        return mock.patch.dict(os.environ, {"NOC_OPERATOR": handle})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = team.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class Identity(Base):
    def test_no_roster_means_nothing_is_restricted(self):
        op = team.current()
        self.assertEqual(op.role, "unmanaged")
        for cap in team.CAPABILITIES:
            self.assertTrue(team.allowed(cap))
        self.assertEqual(self.run_cli("can", "device_write")[0], 0)

    def test_batch_runs_are_not_restricted_by_the_roster(self):
        self.write_roster()
        with self.as_operator("batch"):
            self.assertEqual(team.current().role, "batch")
            self.assertTrue(team.allowed("device_write"))
        with self.as_operator("batch:webhook"):
            self.assertTrue(team.allowed("comment"))

    def test_roles_map_to_capabilities(self):
        self.write_roster()
        table = {"vic": (True, False, False, False), "tom": (True, True, False, False), "ada": (True, True, True, True)}
        for handle, expected in table.items():
            with self.subTest(handle=handle), self.as_operator(handle):
                got = tuple(team.allowed(c) for c in ("read", "comment", "device_write", "admin"))
                self.assertEqual(got, expected)

    def test_no_operator_set_can_only_read(self):
        self.write_roster()
        self.assertTrue(team.allowed("read"))
        self.assertFalse(team.allowed("comment"))
        code, _out, err = self.run_cli("whoami")
        self.assertEqual(code, 3)
        self.assertIn("team.py me", err)

    def test_unknown_and_disabled_operators(self):
        self.write_roster()
        with self.as_operator("stranger"):
            self.assertTrue(team.allowed("read"))
            self.assertFalse(team.allowed("comment"))
            self.assertIn("not in the roster", team.require("comment"))
        with self.as_operator("gone"):
            self.assertEqual(team.current().role, "disabled")
            self.assertFalse(team.allowed("read"))

    def test_handles_are_case_insensitive(self):
        self.write_roster()
        with self.as_operator("ADA"):
            self.assertEqual((team.current().handle, team.current().role), ("ada", "lead"))
        with self.as_operator("ada"):
            self.assertEqual(self.run_cli("add", "NewPerson", "--name", "New Person")[0], 0)
            self.assertEqual(self.run_cli("set-role", "NEWPERSON", "tech")[0], 0)
        member = json.loads(self.roster.read_text())["members"]["newperson"]
        self.assertEqual(member["role"], "tech")
        self.assertEqual(self.run_cli("me", "TOM")[0], 0)
        self.assertEqual(json.loads(self.user.read_text())["operator"], "tom")

    def test_env_beats_user_config(self):
        self.write_roster()
        self.user.write_text(json.dumps({"operator": "vic"}))
        self.assertEqual(team.current().handle, "vic")
        with self.as_operator("tom"):
            self.assertEqual(team.current().handle, "tom")

    def test_unreadable_roster_is_not_treated_as_unmanaged(self):
        self.roster.write_text("{not json")
        op = team.current()
        self.assertEqual(op.role, "unknown")
        self.assertFalse(team.allowed("comment"))

    def test_model_status(self):
        self.write_roster()
        self.assertEqual(team.model_status("OpenCode/Big-Pickle"), "approved")
        self.assertEqual(team.model_status("other/model"), "unlisted")
        self.write_roster({"members": {}})
        self.assertEqual(team.model_status("any/model"), "no-list")


class Editing(Base):
    def test_first_member_must_be_a_lead_and_needs_no_identity(self):
        code, _o, err = self.run_cli("add", "ada", "--name", "Ada Lead")
        self.assertEqual(code, 2)
        self.assertIn("first member must be a lead", err)
        self.assertEqual(self.run_cli("add", "ada", "--name", "Ada Lead", "--role", "lead")[0], 0)
        data = json.loads(self.roster.read_text())
        self.assertEqual(data["members"]["ada"], {"name": "Ada Lead", "role": "lead", "active": True})
        self.assertEqual(stat.S_IMODE(self.roster.stat().st_mode), 0o600)

    def test_only_a_lead_changes_the_roster(self):
        self.write_roster()
        with self.as_operator("tom"):
            code, _o, err = self.run_cli("add", "new", "--name", "New Person")
            self.assertEqual(code, 1)
            self.assertIn("not allowed to admin", err)
        self.assertNotIn("new", json.loads(self.roster.read_text())["members"])
        with self.as_operator("ada"):
            self.assertEqual(self.run_cli("add", "new", "--name", "New Person", "--chat", "@new")[0], 0)
        member = json.loads(self.roster.read_text())["members"]["new"]
        self.assertEqual((member["role"], member["chat"]), ("viewer", "@new"))

    def test_invalid_handles_and_duplicates_are_refused(self):
        self.write_roster()
        with self.as_operator("ada"):
            for handle in ("x", "BATCH", "has space", "1abc", "a/b"):
                self.assertEqual(self.run_cli("add", handle, "--name", "N")[0], 2, handle)
            self.assertEqual(self.run_cli("add", "tom", "--name", "Tom Again")[0], 2)

    def test_set_role_disable_enable_and_the_last_lead_guard(self):
        self.write_roster()
        with self.as_operator("ada"):
            self.assertEqual(self.run_cli("set-role", "vic", "tech")[0], 0)
            self.assertEqual(self.run_cli("disable", "tom")[0], 0)
            self.assertEqual(self.run_cli("enable", "tom")[0], 0)
            code, _o, err = self.run_cli("set-role", "ada", "tech")
            self.assertEqual(code, 2)
            self.assertIn("no active lead", err)
            self.assertEqual(self.run_cli("disable", "ada")[0], 2)
            self.assertEqual(self.run_cli("set-role", "nobody", "tech")[0], 3)
        members = json.loads(self.roster.read_text())["members"]
        self.assertEqual((members["vic"]["role"], members["tom"]["active"], members["ada"]["role"]), ("tech", True, "lead"))

    def test_edit_keeps_a_backup(self):
        self.write_roster()
        with self.as_operator("ada"):
            self.run_cli("set-role", "vic", "tech")
        self.assertEqual(json.loads(Path(str(self.roster) + ".bak").read_text()), ROSTER)

    def test_unattended_runs_cannot_change_anything(self):
        self.write_roster()
        before = self.roster.read_text()
        with mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1", "NOC_OPERATOR": "ada"}):
            for argv in (("add", "x1", "--name", "N"), ("set-role", "vic", "lead"), ("disable", "vic"), ("me", "ada")):
                code, _o, err = self.run_cli(*argv)
                self.assertEqual(code, 4, argv)
                self.assertIn("refused", err)
        self.assertEqual(self.roster.read_text(), before)
        self.assertFalse(self.user.exists())

    def test_me_remembers_a_rostered_handle_only(self):
        self.write_roster()
        self.assertEqual(self.run_cli("me", "stranger")[0], 3)
        self.assertEqual(self.run_cli("me", "tom")[0], 0)
        self.assertEqual(json.loads(self.user.read_text())["operator"], "tom")
        self.assertEqual(team.current().handle, "tom")


class Check(Base):
    def test_clean_roster_passes(self):
        self.write_roster()
        code, out, _e = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("4 member(s)", out)

    def test_bad_values_fail(self):
        bad = json.loads(json.dumps(ROSTER))
        bad["members"]["tom"]["role"] = "boss"
        bad["members"]["Bad Handle"] = {"name": "X", "role": "viewer"}
        bad["pools"]["mikrotik-ssh"]["scope"] = "everyone"
        bad["approved_models"] = ["ok", ""]
        self.write_roster(bad)
        code, out, _e = self.run_cli("check")
        self.assertEqual(code, 1)
        for text in ("tom: role", "Bad Handle", "pools.mikrotik-ssh.scope", "approved_models"):
            self.assertIn(text, out)

    def test_missing_lead_is_a_warning(self):
        self.write_roster({"members": {"vic": {"name": "Vic", "role": "viewer"}}})
        code, out, _e = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("WARN no active lead", out)

    def test_list_marks_you_and_disabled(self):
        self.write_roster()
        with self.as_operator("tom"):
            _c, out, _e = self.run_cli("list")
        self.assertIn("tom", out)
        self.assertIn("(you)", out)
        self.assertIn("disabled", out)


if __name__ == "__main__":
    unittest.main()
