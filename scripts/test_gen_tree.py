"""Tests for scripts/gen_tree.py and the skills-index gate in scripts/docs_check.py (synthetic inputs)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs_check  # noqa: E402
import gen_tree  # noqa: E402

LONG_DOC = '#!/usr/bin/env python3\n"""x.py - does the thing.\n\n' + "\n".join(f"line {i}" for i in range(120)) + '\n"""\n'


class Header(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, name, text):
        p = self.root / name
        p.write_text(text)
        return p

    def test_module_docstring_with_repeated_file_name(self):
        p = self.write("a.py", '#!/usr/bin/env python3\n"""a.py - Build the thing.\n\nMore.\n"""\nimport os\n')
        self.assertEqual(gen_tree.script_header(p), "Build the thing.")

    def test_long_docstring_is_still_found(self):
        self.assertEqual(gen_tree.script_header(self.write("b.py", LONG_DOC)), "does the thing.")

    def test_function_docstring_is_not_mistaken_for_module_header(self):
        p = self.write("c.py", 'import os\n\n\ndef f():\n    """Not a module header."""\n')
        self.assertIsNone(gen_tree.script_header(p))

    def test_js_leading_comment(self):
        p = self.write("d.mjs", "#!/usr/bin/env node\n// d.mjs - Reads the store.\n// More.\nimport x from 'y';\n")
        self.assertEqual(gen_tree.script_header(p), "Reads the store.")

    def test_clip_capitalises_drops_final_stop_and_truncates(self):
        self.assertEqual(gen_tree.clip("build it."), "Build it")
        out = gen_tree.clip("word " * 60)
        self.assertLessEqual(len(out), gen_tree.MAX_NOTE + 1)
        self.assertTrue(out.endswith("…"))


class Layout(unittest.TestCase):
    FILES = ["README.md", "scripts/a.py", "scripts/test_a.py", "scripts/b.test.mjs", "docs/x.md", "deploy/u.service"]

    def test_tests_are_summarised_and_notes_are_used(self):
        notes = {"deploy/": ("Units.", False), "+.env": ("Secrets.", True)}
        notes = {k.lstrip("+"): v for k, v in notes.items()}
        text = "\n".join(gen_tree.render(gen_tree.build(self.FILES, notes), notes))
        self.assertIn("(2 test files)", text)
        self.assertNotIn("test_a.py", text)
        self.assertIn("# Units", text)
        self.assertIn(".env", text)

    def test_problems_name_missing_and_obsolete_notes(self):
        notes = {"deploy/": ("Units.", False), "gone/": ("Old.", False)}
        issues = gen_tree.problems(self.FILES, notes)
        self.assertTrue(any("describes gone/" in i for i in issues))
        self.assertTrue(any("no line for docs/" in i for i in issues))
        self.assertTrue(any("no line for README.md" in i for i in issues))
        self.assertFalse(any("no line for deploy/" in i for i in issues))

    def test_untracked_notes_are_not_checked_against_git(self):
        self.assertEqual([i for i in gen_tree.problems(["README.md"], {"README.md": ("R", False), ".env": ("E", True)})], [])


class SkillsIndex(unittest.TestCase):
    def test_unlisted_skill_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for name in ("alpha", "beta"):
                (root / ".opencode" / "skills" / name).mkdir(parents=True)
                (root / ".opencode" / "skills" / name / "SKILL.md").write_text("x")
            (root / "docs").mkdir()
            (root / "docs" / "skills.md").write_text("| [`alpha`](../.opencode/skills/alpha/SKILL.md) | x |\n")
            self.assertEqual(docs_check.check_skills_index(root), ["beta"])


if __name__ == "__main__":
    unittest.main()
