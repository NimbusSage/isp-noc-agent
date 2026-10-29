"""Tests for scripts/decision_log.py operator stamping (temporary directories; no real logs)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_log  # noqa: E402


class OperatorStamp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = {k: v for k, v in os.environ.items() if k != "NOC_OPERATOR"}
        env.update({"NOC_USER_CONFIG": str(self.root / "absent.json"), "NOC_DOTENV": str(self.root / "absent.env")})
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(decision_log, "DECISIONS_DIR", self.root / "decisions")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def last(self):
        files = sorted((self.root / "decisions").glob("*.jsonl"))
        return json.loads(files[-1].read_text().splitlines()[-1])

    def test_single_person_install_adds_no_operator(self):
        decision_log.append_decision({"status": "categorized"}, kind="batch_result", ticket_id="1")
        self.assertNotIn("operator", self.last())

    def test_operator_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"NOC_OPERATOR": "ada"}):
            decision_log.append_decision({"status": "needs_human"}, kind="bounce", ticket_id="2")
        self.assertEqual(self.last()["operator"], "ada")

    def test_batch_runs_are_named_batch(self):
        with mock.patch.dict(os.environ, {"NOC_OPERATOR": "batch"}):
            decision_log.append_decision({"status": "categorized"}, kind="batch_result", ticket_id="3")
        self.assertEqual(self.last()["operator"], "batch")

    def test_a_payload_cannot_forge_the_operator(self):
        with mock.patch.dict(os.environ, {"NOC_OPERATOR": "ada"}):
            decision_log.append_decision({"operator": "lead-someone"}, kind="feedback", ticket_id="4")
        self.assertEqual(self.last()["operator"], "ada")

    def test_per_user_file_is_used_when_no_environment_value(self):
        user = self.root / "user.json"
        user.write_text(json.dumps({"operator": "vic"}))
        with mock.patch.dict(os.environ, {"NOC_USER_CONFIG": str(user)}):
            decision_log.append_decision({}, kind="feedback", ticket_id="5")
        self.assertEqual(self.last()["operator"], "vic")


if __name__ == "__main__":
    unittest.main()
