"""Tests for scripts/tree_guard.py (a throwaway git checkout; nothing is sent anywhere)."""
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
import hermetic_env  # noqa: E402,F401
import tree_guard  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo, self.state = root / "repo", root / "state"
        self.repo.mkdir()
        gitconfig = root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = T\n\temail = t@example.invalid\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n")
        env = {k: v for k, v in os.environ.items() if k not in ("NOC_UNATTENDED", "NOC_TREE_GUARD_REPO")}
        env.update({"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root),
                    "NOC_STATE_DIR": str(self.state), "NOC_TREE_GUARD_REPO": str(self.repo)})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "-q")
        (self.repo / ".gitignore").write_text("ignored/\nsite-pack/*\n")
        (self.repo / "tracked.py").write_text("print('v1')\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "a.md").write_text("one\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tree_guard.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class Detect(Base):
    def test_clean_checkout(self):
        code, out, _e = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_edited_deleted_staged_and_new_files_are_all_drift(self):
        (self.repo / "tracked.py").write_text("print('v2')\n")
        (self.repo / "docs" / "a.md").unlink()
        (self.repo / "scratch_probe.mjs").write_text("// probe\n")
        (self.repo / "docs" / "b.md").write_text("new\n")
        self.git("add", "docs/b.md")
        code, out, _e = self.run_cli("check")
        self.assertEqual(code, 1)
        for text in ("tracked.py  (edited)", "docs/a.md  (deleted)", "scratch_probe.mjs  (new file, not tracked and not ignored)",
                     "docs/b.md  (staged)", "4 path(s)"):
            self.assertIn(text, out)

    def test_ignored_files_are_not_drift(self):
        (self.repo / "ignored").mkdir()
        (self.repo / "ignored" / "x.txt").write_text("data\n")
        (self.repo / "site-pack").mkdir()
        (self.repo / "site-pack" / "profile.json").write_text("{}")
        self.assertEqual(self.run_cli("check")[0], 0)

    def test_file_contents_are_never_printed(self):
        (self.repo / "tracked.py").write_text("SUPER_SECRET_BODY_TEXT\n")
        code, out, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertNotIn("SUPER_SECRET_BODY_TEXT", out + err)
        code, out, _e = self.run_cli("check", "--json")
        self.assertEqual(json.loads(out)["paths"], [{"path": "tracked.py", "state": "edited"}])

    def test_not_a_git_checkout(self):
        with mock.patch.dict(os.environ, {"NOC_TREE_GUARD_REPO": str(self.state)}):
            self.state.mkdir(exist_ok=True)
            code, _o, err = self.run_cli("check")
        self.assertEqual(code, 2)
        self.assertIn("not a git checkout", err)


class Alerts(Base):
    def alert(self):
        with mock.patch("notify.send", return_value=True) as send:
            code, _o, _e = self.run_cli("check", "--alert", "--quiet")
        return code, send

    def test_one_alert_per_distinct_drift_and_a_new_one_when_it_changes(self):
        (self.repo / "tracked.py").write_text("print('v2')\n")
        code, send = self.alert()
        self.assertEqual(code, 1)
        self.assertEqual(send.call_count, 1)
        title, body = send.call_args.args[0], send.call_args.args[1]
        self.assertIn("modified", title)
        self.assertIn("tracked.py", body)
        self.assertNotIn("print('v2')", body)
        self.assertEqual(self.alert()[1].call_count, 0)               # same drift: silent
        (self.repo / "tracked.py").write_text("print('v3')\n")
        self.assertEqual(self.alert()[1].call_count, 1)               # edited again: alert again

    def test_a_failed_send_is_retried_next_time(self):
        (self.repo / "tracked.py").write_text("print('v2')\n")
        with mock.patch("notify.send", return_value=False):
            self.run_cli("check", "--alert", "--quiet")
        self.assertEqual(self.alert()[1].call_count, 1)

    def test_accept_silences_exactly_that_drift(self):
        (self.repo / "tracked.py").write_text("print('v2')\n")
        self.assertEqual(self.run_cli("accept")[0], 0)
        code, send = self.alert()
        self.assertEqual((code, send.call_count), (0, 0))
        self.assertIn("acknowledged", self.run_cli("check")[1])
        (self.repo / "tracked.py").write_text("print('v3')\n")
        code, send = self.alert()
        self.assertEqual((code, send.call_count), (1, 1))

    def test_clean_again_resets_the_state(self):
        (self.repo / "tracked.py").write_text("print('v2')\n")
        self.alert()
        self.git("checkout", "--", "tracked.py")
        self.assertEqual(self.alert()[1].call_count, 0)
        self.assertEqual(tree_guard.load_state(), {})
        (self.repo / "tracked.py").write_text("print('v2')\n")
        self.assertEqual(self.alert()[1].call_count, 1)               # a repeat of an old drift counts as new

    def test_accept_is_refused_unattended(self):
        (self.repo / "tracked.py").write_text("print('v2')\n")
        with mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            code, _o, err = self.run_cli("accept")
        self.assertEqual(code, 4)
        self.assertIn("refused", err)
        self.assertEqual(tree_guard.load_state(), {})


if __name__ == "__main__":
    unittest.main()
