"""Tests for scripts/pack.py (local bare repositories as the private remote; nothing leaves the temp directory)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermetic_env  # noqa: E402,F401  (drops GIT_* variables a hook may have exported)
import pack  # noqa: E402

TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"      # shaped like a GitHub token, built at run time
PROFILE = {"org": {"name": "Example Org"}, "resources": {"uisp": {"url": "https://uisp.corp-net.org"}},
           "sonar": {"groups": {"cs_level_2": {"id": 2}}}}
ROSTER = {"version": 1, "members": {"ada": {"name": "Ada Lead", "role": "lead", "active": True}},
          "pools": {}, "approved_models": []}


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[commit]\n\tgpgsign = false\n"
                             "[init]\n\tdefaultBranch = main\n")
        self.remote = self.root / "remote.git"
        sh("git", "init", "-q", "--bare", str(self.remote), cwd=str(self.root))
        env = {k: v for k, v in os.environ.items() if k not in ("NOC_UNATTENDED", "NOC_OPERATOR", "NOC_PACK_GIT_DIR")}
        env.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.root),
                    "NOC_USER_CONFIG": str(self.root / "user.json")})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lead = self.root / "lead" / "site-pack"
        self.mate = self.root / "mate" / "site-pack"

    def at(self, directory):
        return mock.patch.dict(os.environ, {"NOC_SITE_PACK_DIR": str(directory)})

    def seed_lead(self):
        self.lead.mkdir(parents=True)
        (self.lead / "profile.json").write_text(json.dumps(PROFILE))
        (self.lead / "team.json").write_text(json.dumps(ROSTER))
        (self.lead / "devices.md").write_text("| tower | router |\n")
        (self.lead / "topology").mkdir()
        (self.lead / "topology" / "map.md").write_text("fiber map\n")
        (self.lead / "README.md").write_text("public file, not part of the pack\n")
        (self.lead / "profile.json.bak").write_text("{}")
        (self.lead / ".env.local").write_text("X=1\n")

    def run_pack(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO("")):
            code = pack.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def publish(self):
        """A lead initialises the pack and pushes it to the shared remote."""
        self.seed_lead()
        with self.at(self.lead):
            self.assertEqual(self.run_pack("init", str(self.remote))[0], 0)
            self.assertEqual(pack.main(["git", "push", "-q", "-u", "origin", "main"]), 0)

    def lead_commits(self, message, **files):
        with self.at(self.lead):
            for rel, text in files.items():
                path = self.lead / rel.replace("__", "/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            self.assertEqual(pack.main(["git", "add", "-A"]), 0)
            self.assertEqual(pack.main(["git", "commit", "-q", "-m", message]), 0)
            self.assertEqual(pack.main(["git", "push", "-q", "origin", "main"]), 0)

    def tracked(self, directory):
        with self.at(directory):
            return set(pack.git("ls-files").stdout.split())


class Init(Base):
    def test_tracks_only_pack_files_and_installs_the_hook(self):
        self.seed_lead()
        with self.at(self.lead):
            code, out, _e = self.run_pack("init", str(self.remote))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tracked(self.lead), {"profile.json", "team.json", "devices.md", "topology/map.md"})
        self.assertTrue(os.access(self.lead / ".pack.git" / "hooks" / "pre-commit", os.X_OK))
        self.assertIn("git push -u origin main", out)
        with self.at(self.lead):
            self.assertEqual(self.run_pack("init", str(self.remote))[0], 2)

    def test_credentials_stop_init_without_leaving_anything_behind(self):
        self.seed_lead()
        (self.lead / "devices.md").write_text("token = %s\n" % TOKEN)
        with self.at(self.lead):
            code, out, err = self.run_pack("init", str(self.remote))
        self.assertEqual(code, 1)
        self.assertIn("devices.md:1 github-token", err)
        self.assertNotIn(TOKEN, out + err)
        self.assertFalse((self.lead / ".pack.git").exists())

    def test_the_commit_hook_refuses_a_credential(self):
        self.publish()
        with self.at(self.lead):
            (self.lead / "devices.md").write_text("token = %s\n" % TOKEN)
            pack.main(["git", "add", "-A"])
            self.assertNotEqual(pack.main(["git", "commit", "-q", "-m", "oops"]), 0)
            self.assertEqual(pack.git("log", "--oneline").stdout.count("\n"), 1)

    def test_remote_url_must_look_like_a_remote(self):
        self.seed_lead()
        with self.at(self.lead):
            with self.assertRaises(SystemExit):                       # argparse already refuses an option-looking value
                self.run_pack("init", "--upload-pack=x")
            for argv in (("init", "--", "--upload-pack=x"), ("init", "ext::sh -c id"), ("init", "")):
                self.assertEqual(self.run_pack(*argv)[0], 2, argv)
        self.assertFalse((self.lead / ".pack.git").exists())


class Join(Base):
    def test_join_installs_the_pack_and_leaves_other_files_alone(self):
        self.publish()
        self.mate.mkdir(parents=True)
        (self.mate / "README.md").write_text("mine\n")
        with self.at(self.mate):
            code, out, _e = self.run_pack("join", str(self.remote), "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("service uisp added: https://uisp.corp-net.org", out)
        self.assertIn("member ada added: lead", out)
        self.assertEqual(json.loads((self.mate / "profile.json").read_text()), PROFILE)
        self.assertEqual((self.mate / "README.md").read_text(), "mine\n")
        self.assertFalse((self.mate / "profile.json.bak").exists())
        with self.at(self.mate):
            self.assertEqual(self.run_pack("join", str(self.remote), "--yes")[0], 2)
            self.assertEqual(self.run_pack("status")[0], 0)

    def test_nothing_is_written_without_a_yes(self):
        self.publish()
        with self.at(self.mate):
            code, out, err = self.run_pack("join", str(self.remote))
        self.assertEqual(code, 2)
        self.assertIn("service uisp added", out)
        self.assertIn("--yes", err)
        self.assertFalse((self.mate / "profile.json").exists())
        self.assertFalse((self.mate / ".pack.git").exists())

    def test_existing_files_are_never_overwritten(self):
        self.publish()
        self.mate.mkdir(parents=True)
        (self.mate / "profile.json").write_text("mine")
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("profile.json", err)
        self.assertEqual((self.mate / "profile.json").read_text(), "mine")
        self.assertFalse((self.mate / ".pack.git").exists())

    def test_a_pack_may_not_carry_other_paths_or_symlinks(self):
        evil = self.root / "evil"
        sh("git", "init", "-q", str(evil))
        (evil / "README.md").write_text("x")
        sh("git", "add", "-A", cwd=str(evil))
        sh("git", "commit", "-q", "-m", "x", cwd=str(evil))
        sh("git", "push", "-q", str(self.remote), "HEAD:refs/heads/evil", cwd=str(evil))
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--branch", "evil", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("README.md is outside what a pack may hold", err)
        (evil / "README.md").unlink()
        (evil / "devices.md").symlink_to("/etc/passwd")
        sh("git", "add", "-A", cwd=str(evil))
        sh("git", "commit", "-q", "-m", "y", cwd=str(evil))
        sh("git", "push", "-q", "-f", str(self.remote), "HEAD:refs/heads/evil", cwd=str(evil))
        with self.at(self.mate):
            code, _o, err = self.run_pack("join", str(self.remote), "--branch", "evil", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("symlink", err)
        self.assertFalse((self.mate / "devices.md").exists())


class Pull(Base):
    def join_mate(self):
        self.publish()
        with self.at(self.mate):
            self.assertEqual(self.run_pack("join", str(self.remote), "--yes")[0], 0)

    def test_pull_names_every_trusted_change_and_waits_for_a_yes(self):
        self.join_mate()
        changed = json.loads(json.dumps(PROFILE))
        changed["resources"]["uisp"]["url"] = "https://uisp.elsewhere.net"
        changed["sonar"]["groups"]["cs_level_2"]["id"] = 9
        roster = json.loads(json.dumps(ROSTER))
        roster["members"]["newbie"] = {"name": "New Person", "role": "viewer", "active": True}
        roster["members"]["ada"]["role"] = "tech"
        self.lead_commits("change", **{"profile.json": json.dumps(changed), "team.json": json.dumps(roster),
                                       "skills__org-x__SKILL.md": "---\nname: org-x\n---\n"})
        with self.at(self.mate):
            code, out, err = self.run_pack("pull")
            self.assertEqual(code, 2)
            for text in ("service uisp changed: https://uisp.corp-net.org -> https://uisp.elsewhere.net",
                         "setting sonar.groups.cs_level_2.id changed: 2 -> 9",
                         "member newbie added: viewer", "member ada changed: lead -> tech",
                         "skill file skills/org-x/SKILL.md"):
                self.assertIn(text, out)
            self.assertIn("--yes", err)
            self.assertEqual(json.loads((self.mate / "profile.json").read_text()), PROFILE)
            self.assertEqual(self.run_pack("pull", "--yes")[0], 0)
            self.assertEqual(json.loads((self.mate / "profile.json").read_text()), changed)
            self.assertTrue((self.mate / "skills" / "org-x" / "SKILL.md").exists())
            self.assertIn("up to date", self.run_pack("pull")[1])

    def test_diverged_copies_are_not_merged(self):
        self.join_mate()
        self.lead_commits("lead change", **{"devices.md": "| a | b |\n"})
        with self.at(self.mate):
            (self.mate / "devices.md").write_text("| mine |\n")
            pack.main(["git", "add", "-A"])
            pack.main(["git", "commit", "-q", "-m", "local"])
            code, _o, err = self.run_pack("pull", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("local commit", err)

    def test_required_signatures_are_enforced(self):
        self.join_mate()
        Path(os.environ["NOC_USER_CONFIG"]).write_text(json.dumps({"pack": {"require_signed": True}}))
        self.lead_commits("unsigned", **{"devices.md": "| a | b |\n"})
        with self.at(self.mate):
            code, _o, err = self.run_pack("pull", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no valid signature", err)
        self.assertEqual((self.mate / "devices.md").read_text(), "| tower | router |\n")


class Misc(Base):
    def test_git_paths_are_relative_to_the_pack(self):
        self.publish()
        (self.lead / "devices.md").write_text("| changed |\n")
        with self.at(self.lead):
            self.assertEqual(pack.main(["git", "add", "devices.md"]), 0)
            self.assertIn("devices.md", pack.git("diff", "--cached", "--name-only").stdout)

    def test_status_without_a_pack(self):
        self.mate.mkdir(parents=True)
        with self.at(self.mate):
            self.assertEqual(self.run_pack("status")[0], 3)
            self.assertEqual(self.run_pack("pull")[0], 3)

    def test_status_reports_counts(self):
        self.publish()
        with self.at(self.lead):
            code, out, _e = self.run_pack("status", "--fetch")
        self.assertEqual(code, 0)
        self.assertIn("ahead:   0", out)
        self.assertIn("behind:  0", out)

    def test_unattended_runs_change_nothing(self):
        self.seed_lead()
        with self.at(self.lead), mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            for argv in (("init", str(self.remote)), ("join", str(self.remote), "--yes"), ("pull", "--yes"), ("git", "status")):
                code, _o, err = self.run_pack(*argv)
                self.assertEqual(code, 4, argv)
                self.assertIn("refused", err)
        self.assertFalse((self.lead / ".pack.git").exists())


if __name__ == "__main__":
    unittest.main()
