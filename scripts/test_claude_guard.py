"""Tests for scripts/hooks/claude_guard.py (payload-level, temp directories)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

sys.dont_write_bytecode = True

SCRIPTS = Path(__file__).resolve().parent
GUARD_SRC = SCRIPTS / "hooks" / "claude_guard.py"
REAL_SCANNER = SCRIPTS / "secret_scan.py"

# Assembled at run time so this file never carries a literal secret.
FAKE_PASSWORD = "hunter" + "2" + "hunter" + "2"
SECRET_LINE = "pass" + "word=" + FAKE_PASSWORD

STUB_SCANNER = '''
from types import SimpleNamespace


def scan_text(text, path, config=None):
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        if "password=" in line and "<" not in line and "$" not in line:
            out.append(SimpleNamespace(rule="literal-secret", line=number, path=path, snippet="pass..."))
    return out
'''

CRASHING_SCANNER = "def scan_text(text, path, config=None):\n    raise RuntimeError('scanner exploded')\n"


def load_guard():
    spec = importlib.util.spec_from_file_location("claude_guard_under_test", str(GUARD_SRC))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_guard()


def fake_scan(text: str, path: str) -> List[Any]:
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if "password=" in line and "<" not in line and "$" not in line:
            hits.append(SimpleNamespace(rule="literal-secret", line=number))
    return hits


def payload(project: Path, tool: str, rel: str, **tool_input: Any) -> Dict[str, Any]:
    tool_input["file_path"] = str(project / rel)
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(project), "tool_input": tool_input}


class GuardCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name).resolve()
        self._env = dict(os.environ)
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)


class TestEvaluate(GuardCase):
    def test_secret_in_write_is_blocked_without_echoing_it(self) -> None:
        code, message = guard.evaluate(
            payload(self.project, "Write", "docs/a.md", content="intro\n" + SECRET_LINE + "\n"), fake_scan)
        self.assertEqual(code, 2)
        self.assertIn("literal-secret", message)
        self.assertIn("line 2", message)
        self.assertIn("docs/a.md", message)
        self.assertNotIn(FAKE_PASSWORD, message)

    def test_placeholder_is_allowed(self) -> None:
        for text in ("password=<YOUR_PASSWORD>\n", "password=$NOC_MCP_PASSPHRASE\n", "see NOC_MCP_PASSPHRASE\n"):
            with self.subTest(text=text):
                code, message = guard.evaluate(payload(self.project, "Write", "docs/a.md", content=text), fake_scan)
                self.assertEqual((code, message), (0, ""))

    def test_edit_scans_new_string_not_old_string(self) -> None:
        clean = payload(self.project, "Edit", "docs/a.md", old_string=SECRET_LINE, new_string="password=<X>")
        self.assertEqual(guard.evaluate(clean, fake_scan)[0], 0)
        dirty = payload(self.project, "Edit", "docs/a.md", old_string="x", new_string=SECRET_LINE)
        self.assertEqual(guard.evaluate(dirty, fake_scan)[0], 2)

    def test_multiedit_reports_which_edit(self) -> None:
        edits = [{"old_string": "a", "new_string": "fine"}, {"old_string": "b", "new_string": SECRET_LINE}]
        code, message = guard.evaluate(payload(self.project, "MultiEdit", "docs/a.md", edits=edits), fake_scan)
        self.assertEqual(code, 2)
        self.assertIn("edit 2", message)

    def test_notebook_new_source_is_scanned(self) -> None:
        data = {"tool_name": "NotebookEdit", "cwd": str(self.project),
                "tool_input": {"notebook_path": str(self.project / "n.ipynb"), "new_source": SECRET_LINE}}
        self.assertEqual(guard.evaluate(data, fake_scan)[0], 2)

    def test_paths_that_are_never_scanned(self) -> None:
        outside = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/elsewhere.txt", "content": SECRET_LINE}}
        self.assertEqual(guard.evaluate(outside, fake_scan), (0, ""))
        for rel in (".git/config", "node_modules/x/index.js", "pkg/__pycache__/m.py"):
            with self.subTest(rel=rel):
                data = payload(self.project, "Write", rel, content=SECRET_LINE)
                self.assertEqual(guard.evaluate(data, fake_scan), (0, ""))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignored_files_are_skipped(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / ".gitignore").write_text(".env\nsite-pack/*\n")
        for rel in (".env", "site-pack/profile.json"):
            with self.subTest(rel=rel):
                self.assertEqual(guard.evaluate(payload(self.project, "Write", rel, content=SECRET_LINE), fake_scan),
                                 (0, ""))
        self.assertEqual(guard.evaluate(payload(self.project, "Write", "docs/a.md", content=SECRET_LINE), fake_scan)[0], 2)

    def test_odd_payload_shapes_are_ignored(self) -> None:
        for data in ({}, {"tool_input": "text"}, {"tool_input": {}}, {"tool_input": {"file_path": "x"}},
                     {"tool_input": {"content": SECRET_LINE}}, {"tool_input": {"file_path": "a", "content": 5}}):
            with self.subTest(data=data):
                self.assertEqual(guard.evaluate(data, fake_scan), (0, ""))

    def test_findings_can_be_dicts(self) -> None:
        def scan(text: str, path: str) -> List[Dict[str, Any]]:
            return [{"rule": "aws-key", "line": 3}]

        code, message = guard.evaluate(payload(self.project, "Write", "a.md", content="x"), scan)
        self.assertEqual(code, 2)
        self.assertIn("aws-key", message)


def run_guard(script: Path, stdin: str, project: Path):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    return subprocess.run([sys.executable, "-B", str(script)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=60)


class TestProcess(GuardCase):
    """End to end: copy the guard into a temp scripts/ tree beside a stub scanner."""

    def install(self, scanner_source: Optional[str]) -> Path:
        (self.project / "scripts" / "hooks").mkdir(parents=True)
        script = self.project / "scripts" / "hooks" / "claude_guard.py"
        shutil.copy2(str(GUARD_SRC), str(script))
        if scanner_source is not None:
            (self.project / "scripts" / "secret_scan.py").write_text(scanner_source)
        return script

    def test_exit_codes(self) -> None:
        script = self.install(STUB_SCANNER)
        blocked = run_guard(script, json.dumps(payload(self.project, "Write", "a.md", content=SECRET_LINE)), self.project)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("BLOCKED", blocked.stderr)
        self.assertNotIn(FAKE_PASSWORD, blocked.stderr + blocked.stdout)
        allowed = run_guard(script, json.dumps(payload(self.project, "Write", "a.md", content="password=<X>")), self.project)
        self.assertEqual((allowed.returncode, allowed.stderr), (0, ""))

    def test_malformed_input_fails_open(self) -> None:
        script = self.install(STUB_SCANNER)
        for raw in ("", "{not json", "[1, 2]", "null"):
            with self.subTest(raw=raw):
                self.assertEqual(run_guard(script, raw, self.project).returncode, 0)
        self.assertIn("unreadable", run_guard(script, "{not json", self.project).stderr)

    def test_scanner_crash_fails_open_and_says_so(self) -> None:
        script = self.install(CRASHING_SCANNER)
        res = run_guard(script, json.dumps(payload(self.project, "Write", "a.md", content="x")), self.project)
        self.assertEqual(res.returncode, 0)
        self.assertIn("internal error", res.stderr)

    def test_missing_scanner_fails_open_and_says_so(self) -> None:
        script = self.install(None)
        res = run_guard(script, json.dumps(payload(self.project, "Write", "a.md", content=SECRET_LINE)), self.project)
        self.assertEqual(res.returncode, 0)
        self.assertIn("not scanned", res.stderr)


@unittest.skipUnless(REAL_SCANNER.is_file(), "scripts/secret_scan.py not present")
class TestWithRealScanner(GuardCase):
    def test_real_scanner_blocks_and_allows(self) -> None:
        project = SCRIPTS.parent
        os.environ["CLAUDE_PROJECT_DIR"] = str(project)
        blocked = run_guard(GUARD_SRC, json.dumps(payload(project, "Write", "docs/guard_probe.md", content=SECRET_LINE + "\n")), project)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("literal-secret", blocked.stderr)
        self.assertNotIn(FAKE_PASSWORD, blocked.stderr)
        ok = run_guard(GUARD_SRC, json.dumps(payload(project, "Write", "docs/guard_probe.md",
                                                     content="Set PASSWORD=<YOUR_PASSWORD> and read NOC_MCP_PASSPHRASE.\n")), project)
        self.assertEqual((ok.returncode, ok.stderr), (0, ""))

    def test_settings_json_points_at_this_script(self) -> None:
        settings = json.loads((SCRIPTS.parent / ".claude" / "settings.json").read_text())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertTrue(command.endswith("/scripts/hooks/claude_guard.py"))


if __name__ == "__main__":
    unittest.main()
