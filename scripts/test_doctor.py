#!/usr/bin/env python3
"""
Tests for scripts/doctor.py (tiers, placeholders, secret hygiene, --discover-ids, --notify).

Run from the repo root:
    python3 -B -m unittest scripts.test_doctor

Every test works in a temp directory with doctor.ROOT and os.environ patched; fake values are
generated here and never real. The only network is the in-process mock Sonar on 127.0.0.1
(scripts/mock_sonar.py) and a throwaway local HTTP recorder that stands in for the chat webhook.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import doctor  # noqa: E402
import mock_sonar  # noqa: E402

FAKE_TOKEN = "tok_9f3a7c1d5e2b44aa"
FAKE_PASS = "pass_8d2c4e6a0b1f77cc"
NEED_TOOLS = unittest.skipUnless(shutil.which("node") and shutil.which("curl"), "needs node and curl on PATH")


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="doctor-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        patcher = mock.patch.object(doctor, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "WEBHOOK_PORT": "1"}
        envpatch = mock.patch.dict(os.environ, env, clear=True)
        envpatch.start()
        self.addCleanup(envpatch.stop)
        self.demo_files(True)

    # -- helpers ------------------------------------------------------------
    def demo_files(self, present):
        """The files scripts/demo.py needs, in the temp root."""
        index = self.root / "server" / "index.js"
        express = self.root / "server" / "node_modules" / "express" / "package.json"
        demo = self.root / "scripts" / "demo.py"
        if present:
            for p in (index, express, demo):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("// fixture\n")
        else:
            shutil.rmtree(self.root / "server" / "node_modules", ignore_errors=True)

    def write_env(self, **values):
        p = self.root / ".env"
        p.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        os.chmod(p, 0o600)
        return p

    def run_doctor(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = doctor.run(list(argv))
        return code, out.getvalue(), err.getvalue()

    def rows(self, *argv):
        code, out, _ = self.run_doctor("--json", *argv)
        return code, json.loads(out)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root)] + list(args), check=True, capture_output=True)


class TierTests(DoctorCase):
    @NEED_TOOLS
    def test_tier0_ready(self):
        code, out, _ = self.run_doctor("--tier", "0")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "ready tier: 0")
        names = [l.split()[1] for l in out.splitlines() if l.startswith("[")]
        self.assertTrue({"python", "node", "demo"} <= set(names))
        self.assertNotIn("sonar_api", names)  # higher tiers are not run
        self.assertIn("tier 1 (devices): not checked", out)

    @NEED_TOOLS
    def test_tier0_blocked_gives_fix_and_none(self):
        self.demo_files(False)
        code, out, _ = self.run_doctor("--tier", "0")
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().splitlines()[-1], "ready tier: none")
        self.assertIn("fix: cd server && npm install", out)

    @NEED_TOOLS
    def test_default_run_reports_all_tiers_and_highest_ready(self):
        code, data = self.rows("--skip", "harness")
        self.assertEqual(set(data), {"results", "ok", "ready_tier", "tiers"})
        self.assertEqual(data["ready_tier"], 0)
        self.assertEqual(sorted(data["tiers"]), ["0", "1", "2", "3"])
        self.assertTrue(data["tiers"]["0"]["ready"])
        self.assertFalse(data["tiers"]["1"]["ready"])
        self.assertIn("mcp_build", data["tiers"]["1"]["blocking"])
        for r in data["results"]:
            self.assertEqual(set(r), {"name", "status", "detail", "hint", "tier", "required"})
        self.assertEqual(code, 1)  # missing .env / build are FAILs, as before

    @NEED_TOOLS
    def test_text_output_ends_with_ready_tier_line(self):
        _, out, _ = self.run_doctor("--skip", "harness")
        self.assertRegex(out.strip().splitlines()[-1], r"^ready tier: (none|\d)$")

    def test_every_fail_and_warn_row_has_a_fix_line(self):
        _, out, _ = self.run_doctor("--skip", "harness")
        lines = out.splitlines()
        flagged = [i for i, l in enumerate(lines) if l.startswith(("[FAIL]", "[WARN]"))]
        self.assertTrue(flagged)
        for i in flagged:
            self.assertIn("fix: ", lines[i + 1], lines[i])
        _, data = self.rows("--skip", "harness")
        for r in data["results"]:
            if r["status"] in ("fail", "warn"):
                self.assertTrue(r["hint"], r["name"])

    def test_only_unknown_exits_2(self):
        code, _, err = self.run_doctor("--only", "bogus")
        self.assertEqual(code, 2)
        self.assertIn("unknown check name", err)
        proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "doctor.py"), "--only", "bogus"],
                              capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(proc.returncode, 2)

    def test_write_profile_requires_discover(self):
        code, _, err = self.run_doctor("--write-profile")
        self.assertEqual(code, 2)
        self.assertIn("--discover-ids", err)

    def test_node_below_20_warns_and_below_18_fails(self):
        with mock.patch.object(doctor, "_node_version", return_value=((18, 19, 0), True)):
            code, out, _ = self.run_doctor("--only", "node")
        self.assertEqual(code, 0)
        self.assertIn("[WARN]", out)
        self.assertIn("supported >= 20", out)
        with mock.patch.object(doctor, "_node_version", return_value=((16, 2, 0), True)):
            code, out, _ = self.run_doctor("--only", "node")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)


class PlaceholderTests(DoctorCase):
    def test_placeholder_values_fail_naming_keys_only(self):
        self.write_env(SONAR_TOKEN="<your_sonar_api_token>", NOC_MCP_PASSPHRASE="changeme")
        code, out, _ = self.run_doctor("--only", "env_required")
        self.assertEqual(code, 1)
        self.assertIn("SONAR_TOKEN", out)
        self.assertIn("NOC_MCP_PASSPHRASE", out)
        self.assertNotIn("your_sonar_api_token", out)
        self.assertNotIn("changeme", out)

    def test_empty_and_other_placeholder_shapes_fail(self):
        for bad in ("", "your-token-here", "xxxxxxxx", "https://chat.googleapis.com/v1/spaces/.../messages"):
            self.assertTrue(doctor.is_placeholder(bad), bad)
        for good in (FAKE_TOKEN, FAKE_PASS):
            self.assertFalse(doctor.is_placeholder(good), good)

    def test_real_values_pass_and_are_never_printed(self):
        self.write_env(SONAR_TOKEN=FAKE_TOKEN, NOC_MCP_PASSPHRASE=FAKE_PASS)
        code, out, _ = self.run_doctor("--only", "env_required")
        self.assertEqual(code, 0, out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn(FAKE_PASS, out)

    def test_opencode_keys_only_required_for_opencode_harness_with_server(self):
        self.write_env(SONAR_TOKEN=FAKE_TOKEN, NOC_MCP_PASSPHRASE=FAKE_PASS, TICKET_WEBHOOK_AUTH_TOKEN="<generate_a_long_random_string>")
        # server token is a placeholder and --server was not given: pipeline server is out of scope, nothing FAILs
        code, out, _ = self.run_doctor("--only", "env_required")
        self.assertEqual(code, 0, out)
        self.assertNotIn("OPENCODE", out)
        # asked to check the server: default harness is opencode, so its keys are named (keys only)
        code, out, _ = self.run_doctor("--only", "env_required", "--server")
        self.assertEqual(code, 1)
        for key in ("TICKET_WEBHOOK_AUTH_TOKEN", "OPENCODE_PATH", "OPENCODE_WORKSPACE"):
            self.assertIn(key, out)
        self.assertNotIn("generate_a_long_random_string", out)
        # keys set but pointing nowhere: named, values not printed
        self.write_env(SONAR_TOKEN=FAKE_TOKEN, NOC_MCP_PASSPHRASE=FAKE_PASS, TICKET_WEBHOOK_AUTH_TOKEN=FAKE_PASS,
                       OPENCODE_PATH="/nonexistent/opencode-bin", OPENCODE_WORKSPACE="/nonexistent/workdir")
        code, out, _ = self.run_doctor("--only", "env_required", "--server")
        self.assertEqual(code, 1)
        self.assertIn("OPENCODE_PATH", out)
        self.assertNotIn("/nonexistent", out)
        # claude harness: OPENCODE_* are not required
        self.write_env(SONAR_TOKEN=FAKE_TOKEN, NOC_MCP_PASSPHRASE=FAKE_PASS, TICKET_WEBHOOK_AUTH_TOKEN=FAKE_PASS, AGENT_HARNESS="claude")
        code, out, _ = self.run_doctor("--only", "env_required", "--server")
        self.assertEqual(code, 0, out)
        self.assertNotIn("OPENCODE", out)

    def test_google_chat_webhook_is_never_required(self):
        self.write_env(SONAR_TOKEN=FAKE_TOKEN, NOC_MCP_PASSPHRASE=FAKE_PASS)
        _, out, _ = self.run_doctor("--only", "env_required")
        self.assertNotIn("GOOGLE_CHAT", out)

    def test_webhook_health_url_follows_bind_and_port(self):
        with mock.patch.object(doctor, "ENV", {}):
            self.assertEqual(doctor.webhook_health_url({"WEBHOOK_BIND": "0.0.0.0", "WEBHOOK_PORT": "7000"})[0],
                             "http://127.0.0.1:7000/health")
            self.assertEqual(doctor.webhook_health_url({"WEBHOOK_BIND": "198.51.100.7"})[0], "http://198.51.100.7:65333/health")
            self.assertEqual(doctor.webhook_health_url({"WEBHOOK_BIND": "::", "WEBHOOK_PORT": "9"})[0], "http://[::1]:9/health")
            self.assertEqual(doctor.webhook_health_url({})[0], "http://127.0.0.1:65333/health")


class HygieneTests(DoctorCase):
    def test_loose_permissions_fail_with_chmod_fix_and_content_is_not_printed(self):
        p = self.write_env(SONAR_TOKEN=FAKE_TOKEN)
        os.chmod(p, 0o644)
        code, out, _ = self.run_doctor("--only", "secret_hygiene")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)
        self.assertIn("644", out)
        self.assertIn("chmod 600 .env", out)
        self.assertNotIn(FAKE_TOKEN, out)
        os.chmod(p, 0o600)
        code, out, _ = self.run_doctor("--only", "secret_hygiene")
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS]", out)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_ignore_and_tracked_state(self):
        self.git("init", "-q")
        self.write_env(SONAR_TOKEN=FAKE_TOKEN)
        code, out, _ = self.run_doctor("--only", "secret_hygiene")
        self.assertEqual(code, 0)
        self.assertIn("[WARN]", out)
        self.assertIn("not git-ignored", out)
        (self.root / ".gitignore").write_text(".env\n")
        code, out, _ = self.run_doctor("--only", "secret_hygiene")
        self.assertIn("[PASS]", out, out)
        self.git("add", "-f", ".env")
        code, out, _ = self.run_doctor("--only", "secret_hygiene")
        self.assertEqual(code, 1)
        self.assertIn("tracked by git", out)
        self.assertIn("git rm --cached .env", out)

    def test_plaintext_credentials_next_to_encrypted_store_warns(self):
        cfg = self.root / "mcp-server" / "config"
        cfg.mkdir(parents=True)
        for name in ("credentials.json", "credentials.enc", "credentials.enc.bak-20250101"):
            (cfg / name).write_text("x")
            os.chmod(cfg / name, 0o600)
        code, out, _ = self.run_doctor("--only", "secret_hygiene")
        self.assertEqual(code, 0, out)
        self.assertIn("credentials_plaintext", out)
        self.assertIn("shred -u mcp-server/config/credentials.json", out)
        self.assertIn("credentials_backups", out)
        self.assertIn("credentials.enc.bak-20250101", out)

    def test_git_hooks_warns_with_fix(self):
        if not shutil.which("git"):
            self.skipTest("needs git")
        self.git("init", "-q")
        (self.root / ".githooks").mkdir()
        _, out, _ = self.run_doctor("--only", "git_hooks")
        self.assertIn("[WARN]", out)
        self.assertIn("python3 scripts/harness_setup.py --hooks", out)
        self.git("config", "core.hooksPath", ".githooks")
        _, out, _ = self.run_doctor("--only", "git_hooks")
        self.assertIn("[PASS]", out)


class SonarTests(DoctorCase):
    def setUp(self):
        super().setUp()
        self.token = "tok_" + "ab12cd34ef56ab78"
        self.srv = mock_sonar.MockSonarServer(tokens=[self.token]).start()
        self.addCleanup(self.srv.stop)
        os.environ["SONAR_BASE_URL"] = self.srv.url
        os.environ["SONAR_TOKEN"] = self.token
        (self.root / "site-pack").mkdir()
        shutil.copy(REPO / "site-pack" / "profile.example.json", self.root / "site-pack" / "profile.example.json")

    def test_sonar_api_probe_honors_sonar_base_url(self):
        code, out, _ = self.run_doctor("--only", "sonar_api")
        self.assertEqual(code, 0, out)
        self.assertIn("[PASS]", out)
        self.assertNotIn(self.token, out)

    def test_sonar_api_wrong_token_fails_with_fix(self):
        os.environ["SONAR_TOKEN"] = "tok_wrong0000000000"
        code, out, _ = self.run_doctor("--only", "sonar_api")
        self.assertEqual(code, 1)
        self.assertIn("fix: ", out)
        self.assertNotIn("tok_wrong0000000000", out)

    def test_discover_ids_lists_groups_and_categories(self):
        code, out, _ = self.run_doctor("--discover-ids")
        self.assertEqual(code, 0, out)
        self.assertIn("Ticket groups (3):", out)
        self.assertIn("CS Level 2", out)
        self.assertNotIn(self.token, out)
        code, out, _ = self.run_doctor("--discover-ids", "--json")
        data = json.loads(out)
        sonar = data["fragment"]["sonar"]
        self.assertEqual(sonar["groups"]["noc"]["id"], 41)
        self.assertEqual(sonar["groups"]["cs_level_2"]["id"], 42)
        self.assertEqual(sonar["groups"]["sys_admin"]["id"], 43)
        self.assertEqual(sonar["categories"]["slow_speeds"]["id"], 201)
        self.assertEqual(sonar["categories"]["night_peak"]["id"], 202)
        self.assertEqual(sonar["categories"]["cx_th_side_issue"]["id"], 203)
        self.assertEqual(sonar["categories"]["weather"]["id"], 204)
        self.assertEqual(data["notes"], [])
        self.assertIsNone(data["profile_written"])
        self.assertFalse((self.root / "site-pack" / "profile.json").exists())  # discovery alone writes nothing

    def test_discover_ids_without_token_fails_with_fix(self):
        del os.environ["SONAR_TOKEN"]
        code, _, err = self.run_doctor("--discover-ids")
        self.assertEqual(code, 1)
        self.assertIn("SONAR_TOKEN", err)
        self.assertIn("fix:", err)

    def test_write_profile_only_if_absent_unless_force(self):
        target = self.root / "site-pack" / "profile.json"
        code, out, _ = self.run_doctor("--discover-ids", "--write-profile")
        self.assertEqual(code, 0, out)
        doc = json.loads(target.read_text())
        self.assertEqual(doc["sonar"]["groups"]["cs_level_2"]["id"], 42)
        self.assertEqual(doc["sonar"]["categories"]["weather"]["id"], 204)
        self.assertIn("match", doc["sonar"]["groups"]["noc"])  # example keys are kept
        self.assertEqual(os.stat(target).st_mode & 0o077, 0)
        _, out, _ = self.run_doctor("--only", "site_profile")
        self.assertIn("[PASS]", out)
        # second run: refuses, file untouched
        target.write_text(target.read_text().replace('"cs_level_2"', '"cs_level_2"'))  # same bytes
        before = target.read_text()
        code, out, _ = self.run_doctor("--discover-ids", "--write-profile")
        self.assertEqual(code, 2)
        self.assertIn("not overwritten", out)
        self.assertEqual(target.read_text(), before)
        # --force replaces it and keeps a backup
        code, out, _ = self.run_doctor("--discover-ids", "--write-profile", "--force")
        self.assertEqual(code, 0, out)
        self.assertTrue(list((self.root / "site-pack").glob("profile.json.bak-*")))

    def test_site_profile_warns_on_null_ids_and_example_copy(self):
        target = self.root / "site-pack" / "profile.json"
        shutil.copy(self.root / "site-pack" / "profile.example.json", target)
        _, out, _ = self.run_doctor("--only", "site_profile")
        self.assertIn("[WARN]", out)
        self.assertIn("--discover-ids", out)
        doc = json.loads(target.read_text())
        doc["sonar"]["groups"]["noc"]["id"] = None
        target.write_text(json.dumps(doc))
        _, out, _ = self.run_doctor("--only", "site_profile")
        self.assertIn("groups.noc", out)


class _Recorder(BaseHTTPRequestHandler):
    posts = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        _Recorder.posts.append(self.rfile.read(n))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class NotifyTests(DoctorCase):
    def setUp(self):
        super().setUp()
        _Recorder.posts = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.hook = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        os.environ["NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL"] = self.hook

    def test_nothing_is_sent_without_notify(self):
        code, out, _ = self.run_doctor()
        self.assertEqual(_Recorder.posts, [])
        code, out, _ = self.run_doctor("--only", "google_chat")
        self.assertEqual(code, 0)
        self.assertEqual(_Recorder.posts, [])
        self.assertIn("--notify", out)
        self.assertNotIn(self.hook, out)

    def test_notify_posts_exactly_one_labeled_message(self):
        code, out, _ = self.run_doctor("--only", "google_chat", "--notify")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(_Recorder.posts), 1)
        self.assertIn("this is a test message", json.loads(_Recorder.posts[0])["text"])

    def test_skip_chat_wins_over_notify(self):
        self.run_doctor("--only", "google_chat", "--skip", "google_chat", "--notify")
        self.assertEqual(_Recorder.posts, [])


class HarnessTests(unittest.TestCase):
    def test_harness_row_summarizes_harness_setup(self):
        with mock.patch.object(doctor, "ROOT", REPO), mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                doctor.run(["--json", "--only", "harness", "--harness", "claude"])
        row = json.loads(out.getvalue())["results"][0]
        self.assertEqual(row["name"], "harness")
        self.assertIn("claude: binary on PATH", row["detail"])
        self.assertIn(row["status"], ("pass", "warn", "fail"))
        if row["status"] != "pass":
            self.assertTrue(row["hint"])


if __name__ == "__main__":
    unittest.main()
