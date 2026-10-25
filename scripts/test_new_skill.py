"""Tests for scripts/new_skill.py (synthetic repository; the scaffold must pass skills_lint)."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import new_skill  # noqa: E402

AGENTS = "# X\n\n## Skills index\n\nLoad when the trigger applies.\n\n- `alpha` — first\n- `beta` — second\n\n## Next\n\ntext\n"
SKILLS_DOC = ("# Skills\n\n### Current skills\n\n| Skill | Purpose |\n|---|---|\n"
              "| [`alpha`](../.opencode/skills/alpha/SKILL.md) | first |\n"
              "| [`beta`](../.opencode/skills/beta/SKILL.md) | second |\n\n### How to add\n\ntext\n")
DESC = "Use when a test needs a scaffolded skill"


class Scaffold(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "AGENTS.md").write_text(AGENTS)
        (self.root / "docs" / "skills.md").write_text(SKILLS_DOC)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = new_skill.main(list(argv), root=self.root)
        return code, out.getvalue(), err.getvalue()

    def test_creates_skill_and_registers_it_in_both_lists(self):
        code, out, _ = self.run_main("gamma-tool", "--description", DESC, "--summary", "does the gamma thing")
        self.assertEqual(code, 0)
        skill = (self.root / ".opencode/skills/gamma-tool/SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: gamma-tool\ndescription: " + DESC + "\n---\n"))
        agents = (self.root / "AGENTS.md").read_text().split("\n")
        self.assertEqual(agents[agents.index("- `beta` — second") + 1], "- `gamma-tool` — does the gamma thing")
        rows = (self.root / "docs/skills.md").read_text().split("\n")
        i = next(n for n, l in enumerate(rows) if l.startswith("| [`beta`]"))
        self.assertEqual(rows[i + 1], "| [`gamma-tool`](../.opencode/skills/gamma-tool/SKILL.md) | does the gamma thing |")
        self.assertIn("skills_lint.py --strict", out)

    def test_scaffold_passes_the_skills_lint(self):
        self.run_main("gamma-tool", "--description", DESC, "--summary", "s")
        r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "skills_lint.py"), "--strict", "--repo", str(self.root)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_description_with_a_colon_is_quoted(self):
        self.run_main("gamma-tool", "--description", "Use when a: b", "--summary", "s")
        text = (self.root / ".opencode/skills/gamma-tool/SKILL.md").read_text()
        self.assertIn('description: "Use when a: b"', text)

    def test_invalid_input_writes_nothing(self):
        for argv in (("Bad_Name", "--description", DESC, "--summary", "s"),
                     ("ok-name", "--description", "Helps with things", "--summary", "s"),
                     ("ok-name", "--description", DESC, "--summary", "has | pipe"),
                     ("ok-name", "--description", DESC, "--summary", "two\nlines")):
            code, _, err = self.run_main(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("error:", err)
        self.assertFalse((self.root / ".opencode").exists())
        self.assertEqual((self.root / "AGENTS.md").read_text(), AGENTS)

    def test_existing_skill_is_never_overwritten(self):
        self.run_main("gamma-tool", "--description", DESC, "--summary", "s")
        before = (self.root / ".opencode/skills/gamma-tool/SKILL.md").read_text()
        code, _, err = self.run_main("gamma-tool", "--description", DESC, "--summary", "other")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual((self.root / ".opencode/skills/gamma-tool/SKILL.md").read_text(), before)

    def test_missing_index_is_an_error_and_dry_run_writes_nothing(self):
        (self.root / "AGENTS.md").write_text("# X\n")
        code, _, err = self.run_main("gamma-tool", "--description", DESC, "--summary", "s")
        self.assertEqual(code, 1)
        self.assertIn("Skills index", err)
        (self.root / "AGENTS.md").write_text(AGENTS)
        code, out, _ = self.run_main("gamma-tool", "--description", DESC, "--summary", "s", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would create", out)
        self.assertFalse((self.root / ".opencode").exists())


if __name__ == "__main__":
    unittest.main()
