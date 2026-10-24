import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hermetic_env  # noqa: E402


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


class HermeticEnv(unittest.TestCase):
    def test_scrub_removes_repository_variables_but_keeps_config_switches(self):
        env = {"GIT_DIR": "/x", "GIT_INDEX_FILE": "/y", "GIT_WORK_TREE": "/z", "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin"}
        hermetic_env.scrub_git_env(env)
        self.assertEqual(sorted(env), ["GIT_CONFIG_GLOBAL", "PATH"])

    def test_git_running_tests_leave_a_hook_style_repository_untouched(self):
        """Run the git-using tests with GIT_DIR/GIT_WORK_TREE pointing at a sentinel repo, as a hook would."""
        tmp = Path(tempfile.mkdtemp(prefix="sentinel-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        base = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "PATH": os.environ["PATH"], "HOME": str(tmp)}
        self.assertEqual(run(["git", "init", "-q", str(tmp)], env=base).returncode, 0)
        before = run(["git", "-C", str(tmp), "status", "--porcelain", "--ignored"], env=base).stdout
        hook_env = dict(base, GIT_DIR=str(tmp / ".git"), GIT_WORK_TREE=str(tmp), GIT_INDEX_FILE=str(tmp / ".git" / "index"))
        res = run([sys.executable, "-B", "-m", "unittest", "test_secret_scan.StagedMode", "test_harness_setup"],
                  cwd=str(HERE), env=hook_env, timeout=300)
        self.assertEqual(res.returncode, 0, res.stdout[-600:] + res.stderr[-600:])
        after = run(["git", "-C", str(tmp), "status", "--porcelain", "--ignored"], env=base).stdout
        commits = run(["git", "-C", str(tmp), "rev-list", "--all", "--count"], env=base)
        self.assertEqual(after, before, "the tests changed the sentinel repository's index or working tree")
        self.assertIn(commits.stdout.strip(), ("0", ""), "the tests committed into the sentinel repository")


if __name__ == "__main__":
    unittest.main()
