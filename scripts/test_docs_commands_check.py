import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs_commands_check as dcc  # noqa: E402


class DocsCommandsCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dcc-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "tool.py").write_text("import argparse\np=argparse.ArgumentParser()\np.add_argument('--real')\n")
        (self.root / "scripts" / "env_ignore.txt").write_text("")
        (self.root / ".env.example").write_text("NOC_ONE=\nNOC_TWO=\nNOC_THREE=\n")

    def run_on(self, doc):
        (self.root / "README.md").write_text(doc)
        return [msg for _f, _n, msg in dcc.check(self.root)]

    def test_clean_doc_passes(self):
        self.assertEqual(self.run_on("Run `python3 scripts/tool.py --real` then set NOC_ONE.\n"), [])

    def test_missing_script_flag_and_variable_are_reported(self):
        msgs = self.run_on("Run `python3 scripts/tool.py --gone`, see scripts/missing.py and NOC_RENAMED.\n")
        self.assertTrue(any("--gone" in m for m in msgs))
        self.assertTrue(any("scripts/missing.py" in m for m in msgs))
        self.assertTrue(any("NOC_RENAMED" in m for m in msgs))

    def test_extension_prefixes_and_placeholders_are_not_scripts(self):
        self.assertEqual(self.run_on("The list is scripts/leak_denylist.sha256 and scripts/<name>.py and <NOC_PLACEHOLDER>.\n"), [])

    def test_lookalike_address_in_a_docs_command_is_reported(self):
        msgs = self.run_on("```bash\ncurl -sS http://10.4.5.6:8888/node/fetch/x\n```\n")  # secret-scan:allow(fictional address used as lint test input)
        self.assertTrue(any("looks like a real resource" in m for m in msgs), msgs)
        msgs = self.run_on("Open `https://uisp.example.com/nms` first.\n")
        self.assertTrue(any("looks like a real resource" in m for m in msgs), msgs)

    def test_placeholders_and_example_tagged_fences_are_fine(self):
        self.assertEqual(self.run_on("```bash\ncurl -sS http://<host>:8888/x\n```\n"), [])
        self.assertEqual(self.run_on("```bash example\ncurl -sS http://10.4.5.6/x\n```\n"), [])  # secret-scan:allow(fictional address used as lint test input)

    def test_the_real_repo_is_consistent(self):
        self.assertEqual(dcc.check(), [])


if __name__ == "__main__":
    unittest.main()
