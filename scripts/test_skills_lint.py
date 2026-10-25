"""Tests for scripts/skills_lint.py (synthetic skills in temp directories)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import skills_lint as sl  # noqa: E402

REAL_REPO = Path(__file__).resolve().parent.parent
GOOD = """---
name: demo
description: Use when you need a demo skill for lint tests.
---

# Demo

Plain text with `code` and <p>html</p> tags.

```bash
echo hello
```
"""

LITERAL_PW_CMD = "ssh" + "pass -p " + "literalvalue"  # assembled so this file holds no literal

STUB_SCANNER = '''
class Finding:
    def __init__(self, line, rule):
        self.line, self.rule = line, rule


def scan_text(text, path, config=None):
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        if "sshpass -p " in line and "$" not in line:
            out.append(Finding(number, "sshpass-literal"))
        if "10.0.0.1" in line:
            out.append(Finding(number, "rfc1918-ip"))
    return out
'''


def write_skill(repo: Path, name: str, text: str, files: Optional[Dict[str, str]] = None) -> Path:
    folder = repo / ".opencode" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text)
    for rel, body in (files or {}).items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return folder


def rules(repo: Path, strict: bool = True) -> set:
    result = sl.collect(repo)
    verdict = sl.evaluate(result, sl.load_baseline(repo / sl.BASELINE_REL), strict)
    return {v["rule"] for v in verdict["violations"]}


class LintCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve()
        (self.repo / "scripts").mkdir()
        (self.repo / "AGENTS.md").write_text("small\n")

    def assertFlags(self, text: str, rule: str, files: Optional[Dict[str, str]] = None) -> None:
        write_skill(self.repo, "demo", text, files)
        self.assertIn(rule, rules(self.repo))


class TestRules(LintCase):
    def test_good_skill_is_clean(self) -> None:
        write_skill(self.repo, "demo", GOOD)
        self.assertEqual(rules(self.repo), set())

    def test_missing_skill_md(self) -> None:
        (self.repo / ".opencode" / "skills" / "empty").mkdir(parents=True)
        self.assertIn("skill-missing", rules(self.repo))

    def test_frontmatter_problems(self) -> None:
        self.assertFlags("# no frontmatter\n", "frontmatter")
        self.assertFlags("---\nname: demo\ndescription: Use when x\n", "frontmatter")

    def test_yaml_traps_in_description(self) -> None:
        self.assertFlags("---\nname: demo\ndescription: Use when x: y\n---\n", "frontmatter-yaml")
        self.assertFlags("---\nname: demo\ndescription: Use when x # not a comment\n---\n", "frontmatter-yaml")
        write_skill(self.repo, "demo", '---\nname: demo\ndescription: "Use when x: y # z"\n---\n')
        self.assertNotIn("frontmatter-yaml", rules(self.repo))

    def test_folded_description_is_accepted(self) -> None:
        write_skill(self.repo, "demo", "---\nname: demo\ndescription: >\n  Use when a folded\n  description spans lines.\n---\n")
        self.assertEqual(rules(self.repo), set())

    def test_name_rules(self) -> None:
        self.assertFlags("---\nname: other\ndescription: Use when x\n---\n", "name-mismatch")
        write_skill(self.repo, "Bad_Name", "---\nname: Bad_Name\ndescription: Use when x\n---\n")
        self.assertIn("name-format", rules(self.repo))

    def test_description_rules(self) -> None:
        self.assertFlags("---\nname: demo\ndescription: Handles things\n---\n", "description-prefix")
        self.assertFlags("---\nname: demo\n---\n", "description-missing")
        self.assertFlags("---\nname: demo\ndescription: Use when " + "x" * 1100 + "\n---\n", "description-length")

    def test_line_count_limit(self) -> None:
        self.assertFlags(GOOD + "\nfiller\n" * 500, "line-count")

    def test_unbalanced_fence(self) -> None:
        self.assertFlags(GOOD + "\n```python\nprint(1)\n", "fence-unbalanced")

    def test_longer_fence_can_contain_shorter(self) -> None:
        write_skill(self.repo, "demo", GOOD + "\n````md\n```bash\nx\n```\n````\n")
        self.assertNotIn("fence-unbalanced", rules(self.repo))

    def test_dangling_links_and_paths(self) -> None:
        self.assertFlags(GOOD + "\nSee [guide](references/guide.md).\n", "dangling-link")
        self.assertFlags(GOOD + "\nRead `references/missing.md` first.\n", "dangling-link")
        self.assertFlags(GOOD + "\nRun scripts/not_there.py now.\n", "dangling-link")

    def test_existing_targets_pass(self) -> None:
        (self.repo / "scripts" / "tool.py").write_text("")
        write_skill(self.repo, "demo",
                    GOOD + "\n[guide](references/guide.md) and `references/guide.md` and scripts/tool.py,\n"
                           "plus [web](https://example.org/x.md), [anchor](#top), site-pack/profile.json.\n",
                    files={"references/guide.md": "ok"})
        self.assertNotIn("dangling-link", rules(self.repo))

    def test_extension_must_end_at_a_boundary(self) -> None:
        (self.repo / "mcp-server" / "config").mkdir(parents=True)
        (self.repo / "mcp-server" / "config" / "credentials.json").write_text("{}")
        write_skill(self.repo, "demo", GOOD + "\nSee mcp-server/config/credentials.json here.\n")
        self.assertNotIn("dangling-link", rules(self.repo))

    def test_harness_wording(self) -> None:
        for phrase in ("Run it in the OpenCode TUI.", "Use ask_user to confirm.",
                       "Load it with the skill tool.", "Use the question tool."):
            with self.subTest(phrase=phrase):
                self.assertFlags(GOOD + "\n" + phrase + "\n", "harness-wording")

    def test_agents_md_wording_and_size(self) -> None:
        write_skill(self.repo, "demo", GOOD)
        (self.repo / "AGENTS.md").write_text("Load via `skill` tool.\n" + "x" * 31000)
        found = rules(self.repo)
        self.assertIn("harness-wording", found)
        self.assertIn("agents-size", found)

    def test_midword_placeholders(self) -> None:
        self.assertFlags(GOOD + "\nThe tower<N>s are listed.\n", "midword-placeholder")
        self.assertFlags(GOOD + "\nUse <site>A for the pair.\n", "midword-placeholder")
        write_skill(self.repo, "demo", GOOD + "\nSSH to <ip> or <site>-router; use <br>text.\n")
        self.assertNotIn("midword-placeholder", rules(self.repo))

    def test_secret_literal_uses_secret_scan_on_shell_blocks_only(self) -> None:
        (self.repo / "scripts" / "secret_scan.py").write_text(STUB_SCANNER)
        bad = GOOD + "\n```bash\n" + LITERAL_PW_CMD + " ssh host\n```\n"
        write_skill(self.repo, "demo", bad)
        self.assertIn("secret-literal", rules(self.repo))
        write_skill(self.repo, "demo", GOOD + "\n```bash\nsshpass -p \"$PW\" ssh host\n```\n")
        self.assertNotIn("secret-literal", rules(self.repo))
        write_skill(self.repo, "demo", GOOD + "\n```python\nx = '" + LITERAL_PW_CMD + "'\n```\n")
        self.assertNotIn("secret-literal", rules(self.repo))
        # non-secret scanner rules (an address) are not this lint's business
        write_skill(self.repo, "demo", GOOD + "\n```bash\nping 10.0.0.1\n```\n")
        self.assertNotIn("secret-literal", rules(self.repo))

    def test_without_secret_scan_module_the_check_is_skipped(self) -> None:
        write_skill(self.repo, "demo", GOOD + "\n```bash\n" + LITERAL_PW_CMD + " ssh host\n```\n")
        self.assertNotIn("secret-literal", rules(self.repo))
        self.assertFalse(sl.collect(self.repo)["secret_scan"])


class TestRatchet(LintCase):
    def baseline(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            sl.main(["--repo", str(self.repo), "--update-baseline"])

    def run_lint(self, *extra: str):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sl.main(["--repo", str(self.repo), "--json"] + list(extra))
        return code, json.loads(out.getvalue())

    def test_baselined_findings_pass_and_strict_fails(self) -> None:
        write_skill(self.repo, "demo", GOOD + "\nThe tower<N>s are listed.\n")
        self.baseline()
        code, data = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(data["grandfathered"], {"demo:midword-placeholder": 1})
        code, _ = self.run_lint("--strict")
        self.assertEqual(code, 1)

    def test_growth_and_new_offenders_fail(self) -> None:
        write_skill(self.repo, "demo", GOOD + "\nThe tower<N>s are listed.\n")
        self.baseline()
        write_skill(self.repo, "demo", GOOD + "\nThe tower<N>s and ap<N>s are listed.\n")
        code, data = self.run_lint()
        self.assertEqual(code, 1)
        self.assertIn("baseline 1", data["violations"][0]["message"])
        write_skill(self.repo, "demo", GOOD + "\nThe tower<N>s are listed.\n")
        write_skill(self.repo, "other", GOOD.replace("name: demo", "name: other") + "\nBad tower<N>s.\n")
        code, data = self.run_lint()
        self.assertEqual(code, 1)
        self.assertEqual({v["skill"] for v in data["violations"]}, {"other"})

    def test_line_count_ratchet(self) -> None:
        def body(extra_lines: int) -> str:
            return GOOD + "filler\n" * extra_lines

        write_skill(self.repo, "demo", body(520))
        self.baseline()
        self.assertEqual(self.run_lint()[0], 0)
        write_skill(self.repo, "demo", body(540))
        self.assertEqual(self.run_lint()[0], 1)
        write_skill(self.repo, "demo", body(510))  # shrank, still over 500, below the baseline
        self.assertEqual(self.run_lint()[0], 0)
        write_skill(self.repo, "demo", body(10))
        self.assertEqual(self.run_lint()[0], 0)

    def test_agents_size_ratchet(self) -> None:
        write_skill(self.repo, "demo", GOOD)
        (self.repo / "AGENTS.md").write_text("x" * 40000)
        self.baseline()
        self.assertEqual(self.run_lint()[0], 0)
        (self.repo / "AGENTS.md").write_text("x" * 40500)
        self.assertEqual(self.run_lint()[0], 1)

    def test_fixing_lowers_the_bar_without_failing(self) -> None:
        write_skill(self.repo, "demo", GOOD + "\nThe tower<N>s are listed.\n")
        self.baseline()
        write_skill(self.repo, "demo", GOOD)
        code, data = self.run_lint()
        self.assertEqual(code, 0)
        self.assertEqual(data["baseline_can_tighten"], ["demo:midword-placeholder"])

    def test_baseline_file_is_sorted_and_stable(self) -> None:
        write_skill(self.repo, "zeta", GOOD.replace("demo", "zeta") + "\nBad tower<N>s.\n")
        write_skill(self.repo, "alpha", GOOD.replace("demo", "alpha") + "\nBad tower<N>s.\n")
        self.baseline()
        path = self.repo / sl.BASELINE_REL
        first = path.read_text()
        data = json.loads(first)
        self.assertEqual(list(data["findings"]), sorted(data["findings"]))
        self.baseline()
        self.assertEqual(path.read_text(), first)


class TestCommittedTree(unittest.TestCase):
    def test_baseline_exists_and_is_sorted(self) -> None:
        path = REAL_REPO / sl.BASELINE_REL
        if not path.is_file():
            self.skipTest("no committed baseline yet")
        data = json.loads(path.read_text())
        self.assertEqual(list(data["findings"]), sorted(data["findings"]))
        self.assertEqual(list(data["line_count"]), sorted(data["line_count"]))

    def test_current_tree_passes_against_baseline(self) -> None:
        if not (REAL_REPO / sl.BASELINE_REL).is_file():
            self.skipTest("no committed baseline yet")
        result = sl.collect(REAL_REPO)
        verdict = sl.evaluate(result, sl.load_baseline(REAL_REPO / sl.BASELINE_REL), False)
        self.assertEqual(verdict["violations"], [])


class LookalikeLiteralTests(LintCase):
    def skill(self, body, files=None):
        text = GOOD.replace("```bash\necho hello\n```\n", body)
        write_skill(self.repo, "demo", text, files)
        return rules(self.repo)

    def test_private_ip_in_a_shell_command_is_flagged(self):
        self.assertIn("command-literal", self.skill("```bash\nssh admin@10.9.8.7 '/system identity print'\n```\n"))  # secret-scan:allow(fictional address used as lint test input)

    def test_example_host_in_a_shell_command_is_flagged(self):
        self.assertIn("command-literal", self.skill('```bash\ncurl -sS "https://uisp.example.com/nms/api"\n```\n'))

    def test_placeholders_comments_and_192_168_are_fine(self):
        body = "```bash\n# ssh admin@10.9.8.7\nssh admin@<router_ip> '/ip address add address=192.168.1.1/24'\n```\n"  # secret-scan:allow(fictional address used as lint test input)
        self.assertNotIn("command-literal", self.skill(body))

    def test_an_example_tagged_fence_and_output_fences_are_exempt(self):
        body = "```bash example\nssh admin@10.9.8.7\n```\n\n```\n1 10.9.8.1\n```\n"  # secret-scan:allow(fictional address used as lint test input)
        self.assertNotIn("command-literal", self.skill(body))

    def test_inline_url_command_is_flagged_but_plain_prose_address_is_not(self):
        self.assertIn("command-literal", self.skill("Use `GET http://10.9.8.7:8888/node/fetch/<ip>`.\n"))  # secret-scan:allow(fictional address used as lint test input)
        self.assertNotIn("command-literal", self.skill("The tower router is `10.9.8.7` in this example.\n"))  # secret-scan:allow(fictional address used as lint test input)

    def test_references_are_checked_too(self):
        self.assertIn("command-literal", self.skill("text\n", {"references/a.md": "```bash\nping 10.9.8.7\n```\n"}))  # secret-scan:allow(fictional address used as lint test input)

    def test_three_lookalikes_need_the_notice(self):
        body = "Hops 10.1.1.1, 10.1.1.2 and 10.1.1.3 are examples.\n"  # secret-scan:allow(fictional address used as lint test input)
        self.assertIn("example-notice", self.skill(body))
        self.assertNotIn("example-notice", self.skill(body + sl.EXAMPLE_NOTICE + "\n"))
        self.assertNotIn("example-notice", self.skill("Hops 10.1.1.1 and 10.1.1.2.\n"))  # secret-scan:allow(fictional address used as lint test input)


if __name__ == "__main__":
    unittest.main()
