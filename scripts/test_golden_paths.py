"""Real eval data must default to the site pack and stay out of the tracked tree."""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermetic_env  # noqa: E402,F401
import noc_config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


class Defaults(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pack = Path(self._tmp.name) / "site-pack"
        patcher = mock.patch.dict(os.environ, {"NOC_SITE_PACK_DIR": str(self.pack)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_eval_dir_is_inside_the_site_pack(self):
        self.assertEqual(noc_config.eval_dir(), self.pack / "evals")

    def test_site_pack_dir_falls_back_to_the_profile_folder(self):
        with mock.patch.dict(os.environ, {"NOC_SITE_PACK_DIR": "", "NOC_PROFILE_FILE": str(self.pack / "profile.json")}):
            self.assertEqual(noc_config.site_pack_dir(), self.pack)

    def test_the_golden_set_builder_writes_to_the_site_pack_by_default(self):
        sys.path.insert(0, str(REPO / "evals"))
        try:
            builder = importlib.import_module("build_golden_set")
            builder = importlib.reload(builder)
            self.assertEqual(builder.DEFAULT_OUT, self.pack / "evals" / "golden_set.json")
            self.assertNotIn(REPO / "evals", builder.DEFAULT_OUT.parents)
        finally:
            sys.path.remove(str(REPO / "evals"))

    def test_incident_stubs_go_to_the_site_pack(self):
        import metrics_rollup
        self.assertEqual(metrics_rollup.incident_stub_dir(), self.pack / "evals" / "pending")


class Ignored(unittest.TestCase):
    def ignored(self, path):
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path], capture_output=True)
        return r.returncode == 0

    def test_hand_made_copies_in_the_tracked_tree_are_ignored(self):
        for path in ("evals/golden/golden_set.json", "evals/golden/incident_123456.json", "evals/golden/pending/x.json"):
            self.assertTrue(self.ignored(path), path)

    def test_the_example_set_and_readme_stay_tracked(self):
        for path in ("evals/golden/example_set.json", "evals/golden/README.md"):
            self.assertFalse(self.ignored(path), path)

    def test_stray_incident_probes_are_ignored(self):
        self.assertTrue(self.ignored("scripts/probe_ssh_phase.mjs"))


if __name__ == "__main__":
    unittest.main()
