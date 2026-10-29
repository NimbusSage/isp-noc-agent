"""Tests for scripts/harness_setup.py (temp directories only, no network)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermetic_env  # noqa: F401,E402  (drops GIT_* variables a git hook exports)
import harness_setup as hs  # noqa: E402

REAL_REPO = Path(__file__).resolve().parent.parent
MCP_FILES = {
    "claude": ".mcp.json",
    "cursor": ".cursor/mcp.json",
    "vscode": ".vscode/mcp.json",
    "gemini": ".gemini/settings.json",
    "codex": ".codex/config.toml",
    "opencode": "opencode.json",
}
RELATIVE_HARNESSES = ("claude", "cursor", "vscode", "opencode")
ABSOLUTE_HARNESSES = ("gemini", "codex")


def make_repo(root: Path, agents_bytes: int = 200) -> Path:
    (root / "mcp-server" / "dist").mkdir(parents=True)
    (root / "mcp-server" / "dist" / "index.js").write_text("// stub\n")
    (root / "mcp-server" / "dist" / "tool-cli.js").write_text("// stub\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "mcp_launch.mjs").write_text("// stub\n")
    (root / ".opencode" / "skills" / "demo").mkdir(parents=True)
    (root / ".opencode" / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n")
    (root / "AGENTS.md").write_text("x" * agents_bytes)
    (root / "CLAUDE.md").write_text("@AGENTS.md\n")
    return root


def run_main(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = hs.main(list(argv))
        except SystemExit as exc:  # argparse errors
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class TempRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(Path(self._tmp.name).resolve())


class TestList(unittest.TestCase):
    def test_every_harness_has_a_known_status(self) -> None:
        rows = hs.list_harnesses()
        self.assertEqual({r["harness"] for r in rows},
                         {"opencode", "claude", "codex", "gemini", "cursor", "vscode", "generic"})
        for row in rows:
            self.assertIn(row["status"], {"verified", "shimmed", "generic"})

    def test_list_json_cli(self) -> None:
        code, out, _ = run_main("--list", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 7)

    def test_no_action_is_usage_error(self) -> None:
        code, _, err = run_main()
        self.assertEqual(code, 2)
        self.assertIn("choose one of", err)


class TestWrite(TempRepoCase):
    def test_each_harness_writes_valid_file_without_secrets(self) -> None:
        for harness, rel in MCP_FILES.items():
            with self.subTest(harness=harness):
                shutil.rmtree(self.repo / ".claude", ignore_errors=True)
                result = hs.do_write(harness, self.repo)
                self.assertTrue(result["ok"], result)
                text = (self.repo / rel).read_text()
                if rel.endswith(".json"):
                    data = json.loads(text)
                    self.assertIn(hs.SERVER_NAME, json.dumps(data))
                elif sys.version_info >= (3, 11):
                    import tomllib
                    table = tomllib.loads(text)["mcp_servers"][hs.SERVER_NAME]
                    self.assertEqual(table["command"], "node")
                    self.assertEqual(table["env_vars"], list(hs.PASS_THROUGH))
                self.assertNotIn("/root/NocAgent", text)
                # env pass-through is by NAME only: no literal value follows an "=" or ":" sign
                for name in hs.PASS_THROUGH:
                    if name in text:
                        self.assertNotRegex(text, name + r'"?\s*[:=]\s*"[^${%}]')

    def test_path_rules(self) -> None:
        for harness in RELATIVE_HARNESSES:
            with self.subTest(harness=harness):
                hs.do_write(harness, self.repo)
                self.assertNotIn(str(self.repo), (self.repo / MCP_FILES[harness]).read_text())
        for harness in ABSOLUTE_HARNESSES:
            with self.subTest(harness=harness):
                hs.do_write(harness, self.repo)
                self.assertIn(str(self.repo), (self.repo / MCP_FILES[harness]).read_text())

    def test_launcher_vs_cwd_style(self) -> None:
        claude = json.loads(hs.render("claude", self.repo, None))["mcpServers"][hs.SERVER_NAME]
        self.assertTrue(claude["args"][0].endswith("scripts/mcp_launch.mjs"))
        self.assertNotIn("cwd", claude)
        vscode = json.loads(hs.render("vscode", self.repo, None))["servers"][hs.SERVER_NAME]
        self.assertTrue(vscode["args"][0].endswith("mcp-server/dist/index.js"))
        self.assertTrue(vscode["cwd"].endswith("/mcp-server"))
        self.assertEqual(vscode["type"], "stdio")
        gemini = json.loads(hs.render("gemini", self.repo, None))["mcpServers"][hs.SERVER_NAME]
        self.assertNotIn("trust", gemini)

    def test_existing_file_not_overwritten_without_force(self) -> None:
        path = self.repo / ".cursor" / "mcp.json"
        path.parent.mkdir()
        original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
        path.write_text(original)
        result = hs.do_write("cursor", self.repo)
        self.assertFalse(result["ok"])
        self.assertEqual(result["files"][0]["action"], "refused")
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.repo / ".cursor" / "mcp.json.bak").exists())

    def test_force_merges_and_keeps_backup(self) -> None:
        path = self.repo / ".cursor" / "mcp.json"
        path.parent.mkdir()
        original = json.dumps({"mcpServers": {"other": {"command": "x"}}})
        path.write_text(original)
        result = hs.do_write("cursor", self.repo, force=True)
        self.assertTrue(result["ok"])
        data = json.loads(path.read_text())
        self.assertEqual(set(data["mcpServers"]), {"other", hs.SERVER_NAME})
        self.assertEqual((self.repo / ".cursor" / "mcp.json.bak").read_text(), original)

    def test_second_write_is_unchanged(self) -> None:
        hs.do_write("gemini", self.repo)
        again = hs.do_write("gemini", self.repo)
        self.assertTrue(again["ok"])
        self.assertEqual(again["files"][0]["action"], "unchanged")

    def test_dry_run_writes_nothing(self) -> None:
        result = hs.do_write("vscode", self.repo, dry_run=True)
        self.assertEqual(result["files"][0]["action"], "would-write")
        self.assertTrue(result["files"][0]["diff"])
        self.assertFalse((self.repo / ".vscode").exists())

    def test_invalid_existing_json_is_left_alone(self) -> None:
        path = self.repo / ".vscode" / "mcp.json"
        path.parent.mkdir()
        path.write_text("{ not json")
        result = hs.do_write("vscode", self.repo, force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(path.read_text(), "{ not json")

    def test_gemini_sets_context_file_name(self) -> None:
        path = self.repo / ".gemini" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"theme": "dark", "context": {"fileName": ["GEMINI.md"]}}))
        hs.do_write("gemini", self.repo, force=True)
        data = json.loads(path.read_text())
        self.assertEqual(data["context"]["fileName"], ["AGENTS.md", "GEMINI.md"])
        self.assertEqual(data["theme"], "dark")

    def test_codex_raises_doc_limit_and_is_idempotent(self) -> None:
        shutil.rmtree(self.repo)
        self.repo = make_repo(Path(self._tmp.name) / "big", agents_bytes=58000)
        hs.do_write("codex", self.repo)
        text = (self.repo / ".codex" / "config.toml").read_text()
        self.assertIn("project_doc_max_bytes = 65536", text)
        self.assertEqual(text.count("[mcp_servers.%s]" % hs.SERVER_NAME), 1)
        again = hs.do_write("codex", self.repo, force=True)
        self.assertEqual(again["files"][0]["action"], "unchanged")

    def test_codex_replaces_only_its_own_table(self) -> None:
        path = self.repo / ".codex" / "config.toml"
        path.parent.mkdir()
        path.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "y"\n\n'
                        '[mcp_servers.%s]\ncommand = "old"\n\n[mcp_servers.%s.env]\nA = "b"\n'
                        % (hs.SERVER_NAME, hs.SERVER_NAME))
        hs.do_write("codex", self.repo, force=True)
        text = path.read_text()
        self.assertIn('model = "x"', text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertNotIn('"old"', text)
        self.assertNotIn("[mcp_servers.%s.env]" % hs.SERVER_NAME, text)
        self.assertEqual(text.count("[mcp_servers.%s]" % hs.SERVER_NAME), 1)

    def test_opencode_repoints_paths_and_keeps_everything_else(self) -> None:
        prod_like = {
            "$schema": "https://opencode.ai/config.json",
            "model": "opencode/big-pickle",
            "mcp": {hs.SERVER_NAME: {
                "type": "local",
                "command": ["node", "/elsewhere/mcp-server/dist/index.js"],
                "cwd": "/elsewhere/mcp-server",
                "enabled": True,
                "environment": {"NOC_MCP_PASSPHRASE": "{env:NOC_MCP_PASSPHRASE}",
                                "TEMP_SITE_ALLOWED_HOSTS": "{env:TEMP_SITE_ALLOWED_HOSTS}"},
            }},
        }
        path = self.repo / "opencode.json"
        path.write_text(json.dumps(prod_like))
        self.assertFalse(hs.do_write("opencode", self.repo)["ok"])
        self.assertEqual(json.loads(path.read_text()), prod_like)
        self.assertTrue(hs.do_write("opencode", self.repo, force=True)["ok"])
        data = json.loads(path.read_text())
        entry = data["mcp"][hs.SERVER_NAME]
        self.assertEqual(data["model"], "opencode/big-pickle")
        self.assertEqual(entry["command"], ["node", "scripts/mcp_launch.mjs"])
        self.assertNotIn("cwd", entry)
        self.assertIn("TEMP_SITE_ALLOWED_HOSTS", entry["environment"])

    def test_opencode_v2_layout_gets_disabled_false(self) -> None:
        path = self.repo / "opencode.json"
        path.write_text(json.dumps({"mcp": {"servers": {}}}))
        hs.do_write("opencode", self.repo, force=True)
        entry = json.loads(path.read_text())["mcp"]["servers"][hs.SERVER_NAME]
        self.assertIs(entry["disabled"], False)
        self.assertNotIn("enabled", entry)

    def test_generic_has_nothing_to_write(self) -> None:
        result = hs.do_write("generic", self.repo)
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"], [])
        self.assertTrue(any("tool-cli" in n for n in result["notes"]))

    def test_unbuilt_server_is_called_out(self) -> None:
        (self.repo / "mcp-server" / "dist" / "index.js").unlink()
        result = hs.do_write("gemini", self.repo)
        self.assertTrue(any("npm run build" in n for n in result["notes"]))


class TestSkillsShim(TempRepoCase):
    def test_claude_write_links_relative_skills(self) -> None:
        result = hs.do_write("claude", self.repo)
        self.assertEqual(result["skills"]["action"], "linked")
        link = self.repo / ".claude" / "skills"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(str(link)), os.path.join("..", ".opencode", "skills"))
        self.assertTrue((link / "demo" / "SKILL.md").is_file())

    def test_copy_mode_makes_real_directory(self) -> None:
        result = hs.do_write("claude", self.repo, copy=True)
        self.assertEqual(result["skills"]["action"], "copied")
        target = self.repo / ".claude" / "skills"
        self.assertFalse(target.is_symlink())
        self.assertTrue((target / "demo" / "SKILL.md").is_file())

    def test_real_directory_is_not_replaced_without_force(self) -> None:
        (self.repo / ".claude" / "skills").mkdir(parents=True)
        result = hs.do_write("claude", self.repo)
        self.assertEqual(result["skills"]["action"], "skipped")

    def test_codex_skills_are_opt_in(self) -> None:
        self.assertIsNone(hs.do_write("codex", self.repo)["skills"])
        shutil.rmtree(self.repo / ".codex")
        result = hs.do_write("codex", self.repo, skills=True)
        self.assertEqual(result["skills"]["action"], "linked")
        self.assertTrue(any("twice" in n for n in result["notes"]))


class TestCheck(TempRepoCase):
    def statuses(self, harness: str) -> dict:
        report = hs.do_check(self.repo, harness)
        return {c["name"]: c["status"] for c in report["harnesses"][harness]}

    def test_unconfigured_harness_fails_only_when_selected(self) -> None:
        self.assertEqual(self.statuses("cursor")["mcp"], "fail")
        full = hs.do_check(self.repo, None)
        self.assertEqual({c["name"]: c["status"] for c in full["harnesses"]["cursor"]}["mcp"], "warn")

    def test_written_config_checks_ok(self) -> None:
        for harness in ("claude", "cursor", "vscode", "gemini", "codex"):
            with self.subTest(harness=harness):
                hs.do_write(harness, self.repo)
                report = hs.do_check(self.repo, harness)
                self.assertNotIn("fail", {c["status"] for c in report["harnesses"][harness]},
                                 report["harnesses"][harness])

    def test_missing_build_is_a_warning_with_hint(self) -> None:
        hs.do_write("gemini", self.repo)
        (self.repo / "mcp-server" / "dist" / "index.js").unlink()
        report = hs.do_check(self.repo, "gemini")
        details = [c for c in report["harnesses"]["gemini"] if c["name"] == "build"]
        self.assertEqual(details[0]["status"], "warn")
        self.assertIn("npm ci && npm run build", details[0]["detail"])
        # the registration itself now points at a missing file
        self.assertEqual({c["name"]: c["status"] for c in report["harnesses"]["gemini"]}["mcp"], "fail")

    def test_registration_for_another_checkout_fails(self) -> None:
        path = self.repo / ".gemini" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {hs.SERVER_NAME: {
            "command": "node", "args": ["/no/such/checkout/mcp-server/dist/index.js"]}}}))
        self.assertEqual(self.statuses("gemini")["mcp"], "fail")

    def test_registration_for_a_different_existing_checkout_warns(self) -> None:
        other_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(other_tmp.cleanup)
        other = make_repo(Path(other_tmp.name).resolve())
        path = self.repo / ".gemini" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {hs.SERVER_NAME: {
            "command": "node", "args": [str(other / "mcp-server" / "dist" / "index.js")]}}}))
        checks = {c["name"]: c for c in hs.do_check(self.repo, "gemini")["harnesses"]["gemini"]}
        self.assertEqual(checks["mcp"]["status"], "warn")
        self.assertIn("outside this checkout", checks["mcp"]["detail"])

    def test_fallback_command_is_always_reported(self) -> None:
        report = hs.do_check(self.repo, "generic")
        fallback = [c for c in report["harnesses"]["generic"] if c["name"] == "fallback"][0]
        self.assertIn("node mcp-server/dist/tool-cli.js", fallback["detail"])

    def test_claude_instruction_import_is_checked(self) -> None:
        self.assertEqual(self.statuses("claude")["instructions"], "ok")
        (self.repo / "CLAUDE.md").write_text("no import here\n")
        self.assertEqual(self.statuses("claude")["instructions"], "fail")

    def test_codex_warns_about_32k_cap(self) -> None:
        (self.repo / "AGENTS.md").write_text("x" * 40000)
        self.assertEqual(self.statuses("codex")["instructions"], "info")
        hs.do_write("codex", self.repo)
        self.assertEqual(self.statuses("codex")["instructions-size"], "ok")

    def test_opencode_warns_about_duplicate_skill_dirs(self) -> None:
        hs.do_write("opencode", self.repo)
        hs.do_write("claude", self.repo)
        self.assertEqual(self.statuses("opencode")["skills-duplicate"], "warn")

    def test_cli_exit_codes(self) -> None:
        code, out, _ = run_main("--repo", str(self.repo), "--check", "--harness", "cursor", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])
        run_main("--repo", str(self.repo), "--harness", "cursor", "--write")
        code, _, _ = run_main("--repo", str(self.repo), "--check", "--harness", "cursor")
        self.assertEqual(code, 0)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestHooks(TempRepoCase):
    def test_hooks_sets_core_hooks_path(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / ".githooks").mkdir()
        code, _, _ = run_main("--repo", str(self.repo), "--hooks")
        self.assertEqual(code, 0)
        res = subprocess.run(["git", "-C", str(self.repo), "config", "--get", "core.hooksPath"],
                             capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), ".githooks")

    def test_hooks_needs_git_checkout_and_hooks_dir(self) -> None:
        code, _, _ = run_main("--repo", str(self.repo), "--hooks")
        self.assertEqual(code, 1)


class TestCommittedConfig(unittest.TestCase):
    """The files shipped in the repo must stay valid and consistent with the generator."""

    def load(self, rel: str) -> dict:
        path = REAL_REPO / rel
        if not path.is_file():
            self.skipTest("%s not present" % rel)
        return json.loads(path.read_text())

    def test_mcp_json_matches_generator(self) -> None:
        shipped = self.load(".mcp.json")
        generated = json.loads(hs.render("claude", REAL_REPO, None))
        self.assertEqual(shipped, generated)
        self.assertNotIn("/root/", json.dumps(shipped))

    def test_claude_settings_guard_is_wired(self) -> None:
        settings = self.load(".claude/settings.json")
        deny = settings["permissions"]["deny"]
        for rule in ("Read(.env)", "Read(.env.*)", "Read(!.env.example)", "Read(.secrets/**)",
                     "Read(mcp-server/config/credentials.json)", "Read(backups/**)"):
            self.assertIn(rule, deny)
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Write|Edit|MultiEdit")
        self.assertIn("$CLAUDE_PROJECT_DIR", hook["hooks"][0]["command"])
        self.assertTrue((REAL_REPO / "scripts" / "hooks" / "claude_guard.py").is_file())

    def test_opencode_json_keeps_server_name_and_model(self) -> None:
        data = self.load("opencode.json")
        self.assertIn(hs.SERVER_NAME, data["mcp"])
        self.assertIn("model", data)

    def test_claude_md_imports_agents_md(self) -> None:
        text = (REAL_REPO / "CLAUDE.md").read_text()
        self.assertRegex(text, r"(?m)^@AGENTS\.md$")

    def test_claude_skills_link_resolves(self) -> None:
        link = REAL_REPO / ".claude" / "skills"
        if not link.exists() and not link.is_symlink():
            self.skipTest("no .claude/skills")
        self.assertEqual(os.path.realpath(str(link)), os.path.realpath(str(REAL_REPO / ".opencode" / "skills")))


class TestUnreadablePaths(unittest.TestCase):
    def test_path_exists_treats_permission_errors_as_absent(self):
        with mock.patch.object(Path, "exists", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(hs.path_exists(Path("/root/somewhere/else")))
        self.assertTrue(hs.path_exists(Path(__file__)))

    def test_check_survives_an_opencode_config_that_points_into_an_unreadable_checkout(self):
        repo = Path(tempfile.mkdtemp(prefix="hs-perm-"))
        self.addCleanup(shutil.rmtree, repo, True)
        (repo / "opencode.json").write_text(json.dumps({"mcp": {hs.SERVER_NAME: {
            "type": "local", "command": ["node", "/nonexistent-home/NocAgent/mcp-server/dist/index.js"],
            "cwd": "/nonexistent-home/NocAgent/mcp-server"}}}))
        real_exists = Path.exists

        def deny(self_path, *a, **k):
            if str(self_path).startswith("/nonexistent-home"):
                raise PermissionError(13, "Permission denied")
            return real_exists(self_path, *a, **k)

        real_is_dir = Path.is_dir

        def deny_dir(self_path, *a, **k):
            if str(self_path).startswith("/nonexistent-home"):
                raise PermissionError(13, "Permission denied")
            return real_is_dir(self_path, *a, **k)

        with mock.patch.object(Path, "exists", deny), mock.patch.object(Path, "is_dir", deny_dir):
            checks = hs.check_harness(repo, "opencode", False)      # must not raise
        mcp = [c for c in checks if c["name"] == "mcp"]
        self.assertTrue(mcp and mcp[0]["status"] == "fail", checks)


if __name__ == "__main__":
    unittest.main()
