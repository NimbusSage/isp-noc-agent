#!/usr/bin/env python3
"""Tests for scripts/secret_scan.py, scripts/build_denylist.py and doctor's --only.

Every fake secret is assembled at runtime from fragments, so this file (which
the scanner scans like any other) contains no scannable literal itself.

Run: python3 -B -m unittest discover -s scripts -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import build_denylist  # noqa: E402
import hermetic_env  # noqa: F401,E402  (drops GIT_* variables a git hook exports)
import secret_scan  # noqa: E402

PY = sys.executable


def cat(*parts):
    return "".join(parts)


ALLOW = cat("secret-scan:", "allow")      # assembled so this file holds no live marker


def ip(a, b, c, d):
    return ".".join(str(x) for x in (a, b, c, d))


def write_denylist(directory, words, placeholder=False):
    """Hash `words` (plaintext only ever lives in this process) into a list file."""
    path = Path(directory) / "dl.sha256"
    lines = ["# test deny-list"]
    for w in words:
        key = secret_scan.phrase_key(w)
        lines.append(secret_scan.hash_token(key) + ("  # placeholder" if placeholder else ""))
    path.write_text("\n".join(lines) + "\n")
    return path


def cfg_for(denylist=None):
    return secret_scan.load_config(denylist, profile=None)


EMPTY = secret_scan.ScanConfig()


def rules(text, path="app/config.py", cfg=EMPTY):
    return sorted({f.rule for f in secret_scan.scan_text(text, path, cfg)})


class StructuralRules(unittest.TestCase):
    def test_password_literal_is_caught(self):
        pw = cat("Zq8", "vLm2", "xKw")
        self.assertIn("literal-secret", rules(cat("DB_PASS", "WORD = '", pw, "'\n")))
        self.assertIn("literal-secret", rules(cat('"pass', 'word": "', pw, '"\n'), "cfg/app.json"))
        self.assertIn("literal-secret", rules(cat("snmp-comm", "unity=", pw, "\n"), "notes.md"))
        self.assertIn("literal-secret", rules(cat("api_k", "ey: ", pw, "\n"), "deploy/app.yml"))

    def test_sshpass_literal(self):
        self.assertIn("sshpass-literal", rules(cat("sshp", "ass -p ", "Zq8vLm2xKw", " ssh host\n"), "run.sh"))
        self.assertEqual(rules(cat("sshp", "ass -p ", '"$PW"', " ssh host\n"), "run.sh"), [])
        self.assertEqual(rules(cat("sshp", "ass -p ubnt ssh host\n"), "run.sh"), [])   # vendor default

    def test_token_prefixes(self):
        body = "A1b2C3d4E5" * 4
        self.assertIn("github-token", rules(cat("gh", "p_", body, "\n")))
        self.assertIn("aws-key", rules(cat("AK", "IA", "ABCDEFGH12345678", "\n")))
        self.assertIn("slack-token", rules(cat("xo", "xb-", "123456789012-abcdefgh", "\n")))
        self.assertIn("google-api-key", rules(cat("AI", "za", "A" * 35, "\n")))
        self.assertIn("bearer-token", rules(cat("Authorization: Bear", "er ", "q" * 8, "Z" * 8, "7" * 8, "\n")))
        self.assertIn("api-key", rules(cat("s", "k-", "a1B2" * 9, "\n")))
        jwt = cat("ey", "Jhbgcio1234", ".", "ey", "Jzdwi123456", ".", "abcdefgh1234")
        self.assertIn("jwt", rules(jwt + "\n"))

    def test_mac_addresses(self):
        self.assertIn("mac-address", rules(cat("64:D1:54", ":BB:54:8E\n")))
        self.assertIn("mac-address", rules(cat("64-d1-54", "-bb-54-8e\n")))
        self.assertEqual(rules(cat("AA:BB", ":CC:DD:EE:FF\n")), [])
        self.assertEqual(rules(cat("00:00", ":00:12:34:56\n")), [])

    def test_lat_long(self):
        lat, lon = cat("44", ".17058"), cat("-95", ".26363")
        self.assertIn("lat-long", rules(cat("at ", lat, ", ", lon, "\n")))
        self.assertIn("lat-long", rules(cat('"la', 't": ', lat, "\n"), "x.json"))
        self.assertEqual(rules("version 1.2, build 3\n"), [])

    def test_uuid_and_lockfiles(self):
        u = cat("a0d4f3b7", "-3c1c-", "417c", "-bc57-", "b82534e234c6")
        self.assertIn("uuid", rules(u + "\n", "docs/notes.md"))
        self.assertEqual(rules(u + "\n", "package-lock.json"), [])
        self.assertEqual(rules(u + "\n", "sub/yarn.lock"), [])
        self.assertEqual(rules("00000000-0000-0000-0000-000000000000\n", "docs/notes.md"), [])
        # n8n exports carry random node ids by design: UUID rule off, other rules still on
        self.assertEqual(rules(u + "\n", "docs/n8n/flow.json"), [])
        self.assertIn("n8n-webhook-id", rules(cat('"webhook', 'Id": "', u, '"\n'), "docs/n8n/flow.json"))

    def test_private_key_block(self):
        self.assertIn("private-key", rules(cat("-----BEGIN ", "OPENSSH PRIVATE", " KEY-----\n"), "id.txt"))
        self.assertIn("private-key", rules(cat("-----BEGIN ", "RSA PRIVATE", " KEY-----\n"), "id.txt"))

    def test_google_ids(self):
        sheet = cat("1BxiMVs0XRA5nFMd", "KvBdBZjgmUUqptlbs74OgvE2upms")
        self.assertIn("google-id", rules(cat("https://docs.google.com/spreadsheets/d/", sheet, "/edit\n"), "a.md"))
        self.assertEqual(rules("https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit\n", "a.md"), [])
        self.assertEqual(rules("https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit\n", "a.md"), [])

    def test_url_credentials_and_params(self):
        self.assertIn("url-credentials", rules(cat("https://bob:", "Zq8vLm2xKw", "@host.example.net/x\n"), "a.md"))
        self.assertIn("url-secret-param", rules(cat("https://x.example.net/hook?ke", "y=", "Zq8vLm2xKw12345678", "\n"), "a.md"))
        self.assertEqual(rules("https://user:<password>@host.example.net/x\n", "a.md"), [])

    def test_ipv4_classification(self):
        for bad in (ip(10, 5, 5, 5), ip(172, 20, 1, 1), ip(192, 168, 5, 9), ip(100, 70, 1, 1), ip(45, 33, 32, 156)):
            self.assertTrue(rules(f"host {bad} up\n"), bad)
        for ok in (ip(10, 0, 1, 1), ip(10, 1, 2, 9), ip(192, 168, 1, 1), ip(192, 168, 88, 1), ip(192, 0, 2, 7),
                   ip(198, 51, 100, 7), ip(203, 0, 113, 7), ip(8, 8, 8, 8), ip(1, 1, 1, 1), ip(127, 0, 0, 1),
                   ip(0, 0, 0, 0), ip(255, 255, 255, 0)):
            self.assertEqual(rules(f"host {ok} up\n"), [], ok)
        self.assertEqual(rules(cat("v", ip(1, 2, 3, 4), " released\n")), [])
        self.assertEqual(rules(cat("oid .1.3.6.1.2.1.1.5.0\n")), [])
        self.assertEqual(rules(f"allow {ip(10, 0, 0, 0)}/8\n"), [])

    def test_remote_as(self):
        self.assertIn("bgp-remote-as", rules(cat("remote", ".as=", "13335\n"), "x.rsc"))
        self.assertEqual(rules(cat("remote", ".as=", "64500\n"), "x.rsc"), [])   # documentation ASN

    def test_scrub_damage(self):
        self.assertIn("scrub-midword-placeholder", rules(cat("WO<", "x-y", ">SPACE\n"), "a.md"))
        self.assertIn("scrub-artifact", rules(cat("<", "donor-router", ">\n"), "a.md"))
        self.assertIn("scrub-artifact", rules(cat("<", "site-a", "> to <", "site-b", ">\n"), "a.md"))
        self.assertIn("scrub-ip-mask", rules(cat("10.", "NNN", ".1\n"), "a.md"))
        self.assertIn("scrub-damage", rules(cat("the rule (", "): applies\n"), "a.md"))
        self.assertIn("scrub-damage", rules(cat("verified (", ": the thing)\n"), "a.md"))
        self.assertIn("scrub-damage", rules(cat("section split", ", .\n"), "a.md"))
        # legitimate look-alikes
        self.assertEqual(rules("call foo() then `bar ()` and Map<string>\n", "a.md"), [])
        self.assertEqual(rules("const x = new Map<string, number>();\n", "a.ts"), [])
        self.assertEqual(rules("```js\nsetTimeout(async () => {});\n```\n", "a.md"), [])

    def test_email(self):
        self.assertIn("email", rules(cat("ops", "@", "real-isp", ".net\n"), "a.md"))
        self.assertEqual(rules(cat("ops", "@", "example", ".com\n"), "a.md"), [])
        self.assertEqual(rules(cat("git", "@", "github", ".com:o/r.git\n"), "a.md"), [])

    def test_forbidden_file_names(self):
        for bad in (".env", "svc/.env.prod", "keys/id.pem", "mcp-server/config/credentials.json",
                    "mcp-server/config/credentials.enc", "site-pack/profile.json", "backups/x.txt"):
            self.assertTrue(secret_scan._forbidden_file(bad), bad)
        for ok in (".env.example", "mcp-server/config/credentials.example.json", "site-pack/README.md",
                   "site-pack/profile.example.json", "site-pack/example/a.json", "src/keys.ts"):
            self.assertFalse(secret_scan._forbidden_file(ok), ok)


class PlaceholdersPass(unittest.TestCase):
    def test_placeholders_and_code_are_not_findings(self):
        samples = {
            "a.md": [
                'password = "<password>"', "password=$PASSWORD", 'token = "${TOKEN}"',
                "api_key: {env:API_KEY}", "secret: {{ secrets.X }}", "community=<snmp-community>",
                "password=changeme", "token=...", "psk=xxxx", "Password: required",
                "contact ops@example.com or see https://example.com/docs",
                "curl -H 'X-Auth-Token: $TOKEN' https://example.net",
            ],
            "a.ts": [
                "password: string;", "const passwordField = 'x';", "token: z.string().optional(),",
                "const token = process.env.SONAR_TOKEN;", "libreNmsTokenEnvVar: 'LIBRENMS_TOKEN',",
                "headers: { 'X-Auth-Token': options.token },", "const { password } = credential;",
                "passphraseEnvVar: z.string(),",
            ],
            "a.py": [
                "password: str", "token = get_token()", "token = os.environ.get('SONAR_TOKEN', '')",
                "secret = None", "password = ''", "def f(password, token=None):",
                'f"password={pw}"', "community = args.community",
            ],
            ".env.example": ["SONAR_TOKEN=", "NOC_MCP_PASSPHRASE=changeme", "TOKEN_FILE=/run/secrets/t"],
            "ci.yml": ["GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}"],
        }
        for path, lines in samples.items():
            for line in lines:
                self.assertEqual(rules(line + "\n", path), [], f"{path}: {line}")

    def test_test_files_may_use_obvious_fixtures(self):
        self.assertEqual(rules("const c = { password: 'secret123', apiToken: 'test-token-1' };\n",
                               "mcp-server/tests/x.test.ts"), [])
        # ...but a realistic-looking value in a test is still caught
        self.assertIn("literal-secret", rules(cat("const c = { pass", "word: 'Zq8vLm2xKw' };\n"),
                                              "mcp-server/tests/x.test.ts"))


class Snippets(unittest.TestCase):
    def test_findings_never_echo_the_full_secret(self):
        secret = cat("ghp", "_", "A1b2C3d4E5" * 4)
        for f in secret_scan.scan_text(secret + "\n", "a.txt", EMPTY):
            self.assertLessEqual(len(f.snippet.rstrip(".")), 4)
            self.assertNotIn(secret, str(f))
        pw = "Zq8vLm2xKw"
        found = secret_scan.scan_text(cat("DB_PASS", "WORD='", pw, "'\n"), "a.py", EMPTY)
        self.assertTrue(found)
        self.assertNotIn(pw, " ".join(str(f) for f in found))


class DenyList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_hashed_match(self):
        word = cat("qvx", "lorp", "ton")
        cfg = cfg_for(write_denylist(self.tmp.name, [word, cat("Red Wi", "ng Tow", "er")]))
        self.assertEqual(rules(f"the {word} tower\n", cfg=cfg), ["denylist"])
        self.assertEqual(rules(f"the {word.upper()} tower\n", cfg=cfg), ["denylist"])
        self.assertEqual(rules(f"{word}_site\n", cfg=cfg), ["denylist"])
        self.assertEqual(rules(f"get{word.capitalize()}Name()\n", cfg=cfg), ["denylist"])   # camelCase piece
        self.assertEqual(rules(cat("Red Wi", "ng Tow", "er 2\n"), cfg=cfg), ["denylist"])    # multi-word entry
        self.assertEqual(rules(cat("red-wi", "ng_tow", "er\n"), cfg=cfg), ["denylist"])
        self.assertEqual(rules(cat("RedWi", "ngTow", "er\n"), cfg=cfg), ["denylist"])
        self.assertEqual(rules("nothing to see here\n", cfg=cfg), [])
        self.assertEqual(rules(cat("red wi", "ng alone\n"), cfg=cfg), [])        # entry was the 3-word phrase

    def test_zero_width_and_fullwidth_evasion(self):
        word = cat("qvx", "lorp", "ton")
        cfg = cfg_for(write_denylist(self.tmp.name, [word]))
        self.assertEqual(rules(word[:3] + "​" + word[3:] + "\n", cfg=cfg), ["denylist"])
        wide = "".join(chr(ord(c) + 0xFEE0) for c in word)
        self.assertEqual(rules(wide + "\n", cfg=cfg), ["denylist"])

    def test_path_names_are_checked(self):
        word = cat("qvx", "lorp", "ton")
        cfg = cfg_for(write_denylist(self.tmp.name, [word]))
        (Path(self.tmp.name) / f"{word}-notes.md").write_text("harmless\n")
        res = secret_scan.scan_paths_detailed([f"{word}-notes.md"], cfg, require_denylist=True, base=Path(self.tmp.name))
        self.assertEqual([f.rule for f in res.findings], ["denylist-path"])

    def test_empty_denylist_fails_closed(self):
        d = Path(self.tmp.name)
        (d / "ok.txt").write_text("harmless\n")
        header_only = d / "empty.sha256"
        header_only.write_text("# nothing here\n")
        placeholder_only = write_denylist(self.tmp.name, ["placeholderword"], placeholder=True)
        for dl in (header_only, placeholder_only, d / "missing.sha256"):
            cfg = cfg_for(dl)
            found = secret_scan.scan_paths(["ok.txt"], cfg, require_denylist=True, base=d)
            self.assertEqual([f.rule for f in found], ["denylist-empty"], dl.name)
            self.assertEqual(secret_scan.scan_paths(["ok.txt"], cfg, require_denylist=False, base=d), [])
        good = cfg_for(write_denylist(self.tmp.name, ["qvxlorpton"]))
        self.assertEqual(secret_scan.scan_paths(["ok.txt"], good, require_denylist=True, base=d), [])

    def test_cli_tracked_reports_empty_denylist(self):
        empty = Path(self.tmp.name) / "empty.sha256"
        empty.write_text("# nothing\n")
        out = subprocess.run([PY, "-B", str(HERE / "secret_scan.py"), "--tracked", "--no-profile",
                              "--denylist", str(empty)], capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(out.returncode, 1)
        self.assertIn("denylist-empty", out.stdout)

    def test_profile_tokens(self):
        prof = Path(self.tmp.name) / "profile.json"
        word = cat("zork", "mid")
        prof.write_text(json.dumps({"org": {"portability_tokens": [word, cat("acme", " corp"),
                                                                  cat("198.51", ".100"), "<your-brand-word>"]}}))
        cfg = secret_scan.load_config(write_denylist(self.tmp.name, ["qvxlorpton"]), profile=prof)
        self.assertEqual(rules(f"{word} here\n", cfg=cfg), ["denylist"])        # plain alnum token
        self.assertEqual(rules(cat("Acme", "_Corp rocks\n"), cfg=cfg), ["portability-token"])
        self.assertIn("portability-token", rules(cat("net 198.51", ".100 up\n"), cfg=cfg))
        self.assertEqual(rules("keep <your-brand-word> as is\n", cfg=cfg), [])  # untouched placeholder

    def test_profile_tokens_also_come_from_the_local_overlay(self):
        prof = Path(self.tmp.name) / "profile.json"
        word = cat("zork", "mid")
        moved = cat("acme", " corp")
        prof.write_text(json.dumps({"org": {"portability_tokens": [word]}}))
        (Path(self.tmp.name) / "profile.local.json").write_text(json.dumps({"org": {"portability_tokens": [word, moved]}}))
        cfg = secret_scan.load_config(write_denylist(self.tmp.name, ["qvxlorpton"]), profile=prof)
        self.assertEqual(rules(cat("Acme", "_Corp rocks\n"), cfg=cfg), ["portability-token"])   # only the overlay lists it
        self.assertEqual(rules(f"{word} here\n", cfg=cfg), ["denylist"])


class Suppression(unittest.TestCase):
    def test_reason_is_mandatory(self):
        bad = cat("sshp", "ass -p Zq8vLm2xKw ssh h")
        self.assertEqual(rules(f"{bad}  # {ALLOW}(fixture for the parser test)\n", "t.sh"), [])
        got = rules(f"{bad}  # {ALLOW}()\n", "t.sh")
        self.assertEqual(got, ["sshpass-literal", "suppression-no-reason"])
        got = rules(f"{bad}  # {ALLOW}(x)\n", "t.sh")
        self.assertEqual(got, ["sshpass-literal", "suppression-no-reason"])
        # documentation of the syntax is not a suppression and not a finding
        self.assertEqual(rules(f"write {ALLOW}(<reason>) on the line\n", "a.md"), [])

    def test_suppressions_are_counted(self):
        text = cat("sshp", "ass -p Zq8vLm2xKw ssh h  # ", ALLOW, "(documented fixture)\n")
        res = secret_scan.scan_text(text, "t.sh", EMPTY)
        self.assertEqual(res, [])
        _f, supp, hidden = secret_scan._scan_core(text, "t.sh", EMPTY)
        self.assertEqual((supp, hidden), (1, 1))

    def test_identifier_in_reason_is_not_hidden(self):
        word = cat("qvx", "lorp", "ton")
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_for(write_denylist(d, [word]))
            self.assertEqual(rules(f"x # {ALLOW}(about {word} tower)\n", "t.sh", cfg), ["denylist"])
            self.assertEqual(rules(f"{word}\nx # {ALLOW}(unrelated reason here)\n", "t.sh", cfg), ["denylist"])
            self.assertEqual(rules(f"{word} # {ALLOW}(known public landmark)\n", "t.sh", cfg), [])


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})


class StagedMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.dl = write_denylist(self.tmp.name, [cat("qvx", "lorp", "ton")])

    def run_staged(self, *extra):
        return subprocess.run([PY, "-B", str(HERE / "secret_scan.py"), "--staged", "--no-profile",
                               "--denylist", str(self.dl), *extra], cwd=str(self.repo), capture_output=True, text=True)

    def test_added_lines_only_with_real_line_numbers(self):
        f = self.repo / "notes.txt"
        old = cat("old ", "sshp", "ass -p Zq8vLm2xKw ssh h\n")
        f.write_text(old + "second\n")
        git(self.repo, "add", "notes.txt")
        git(self.repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "seed")
        f.write_text(old + "second\nthird\n" + cat("the qvx", "lorp", "ton tower\n"))
        git(self.repo, "add", "notes.txt")
        out = self.run_staged()
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("notes.txt:4: [denylist]", out.stdout)
        self.assertNotIn("sshpass-literal", out.stdout)       # the committed line is not re-reported
        self.assertNotIn(cat("qvx", "lorp", "ton"), out.stdout + out.stderr)

    def test_unstaged_content_is_ignored_and_clean_passes(self):
        (self.repo / "a.txt").write_text("fine\n")
        git(self.repo, "add", "a.txt")
        (self.repo / "b.txt").write_text(cat("sshp", "ass -p Zq8vLm2xKw ssh h\n"))   # untracked, unstaged
        out = self.run_staged()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_json_output_and_forbidden_file(self):
        (self.repo / ".env").write_text("A=1\n")
        git(self.repo, "add", "-f", ".env")
        out = self.run_staged("--json")
        data = json.loads(out.stdout)
        self.assertEqual(out.returncode, 1)
        self.assertFalse(data["ok"])
        self.assertIn("forbidden-file", data["by_rule"])
        self.assertIn("suppressions", data)

    def test_staged_fails_closed_without_denylist(self):
        (self.repo / "a.txt").write_text("fine\n")
        git(self.repo, "add", "a.txt")
        self.dl.write_text("# empty\n")
        out = self.run_staged()
        self.assertEqual(out.returncode, 1)
        self.assertIn("denylist-empty", out.stdout)


class CommandLine(unittest.TestCase):
    def scan(self, *args, stdin=None):
        return subprocess.run([PY, "-B", str(HERE / "secret_scan.py"), "--no-profile", *args], input=stdin,
                              capture_output=True, text=True, cwd=str(ROOT))

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.scan().returncode, 2)
        self.assertEqual(self.scan("--stdin").returncode, 2)
        self.assertEqual(self.scan("--bogus-flag").returncode, 2)

    def test_stdin_mode(self):
        bad = self.scan("--stdin", "--path", "x.sh", stdin=cat("sshp", "ass -p Zq8vLm2xKw ssh h\n"))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("x.sh:1: [sshpass-literal]", bad.stdout)
        self.assertEqual(self.scan("--stdin", "--path", "x.sh", stdin="echo hi\n").returncode, 0)

    def test_paths_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.txt"
            p.write_text(cat("ghp", "_", "A1b2C3d4E5" * 4, "\n"))
            out = self.scan(str(p), "--json")
            data = json.loads(out.stdout)
            self.assertEqual(out.returncode, 1)
            self.assertEqual(data["by_rule"], {"github-token": 1})


def bd(args):
    """build_denylist.main with its console output captured."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return build_denylist.main(args)


class BuildDenylist(unittest.TestCase):
    def test_build_check_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as d:
            words = Path(d) / "words.txt"
            words.write_text("# comment\n" + cat("qvx", "lorp", "ton") + "\n" + cat("Red Wi", "ng Tow", "er") +
                             "\nab\n\n" + cat("QVX", "LORP", "TON") + "\n")
            out = Path(d) / "out.sha256"
            self.assertEqual(bd([str(words), "--out", str(out)]), 0)
            text = out.read_text()
            hashes = [l for l in text.splitlines() if l and not l.startswith("#")]
            self.assertEqual(len(hashes), 2)                       # duplicate collapsed, 'ab' skipped
            self.assertEqual(hashes, sorted(hashes))
            for w in (cat("qvx", "lorp", "ton"), "lorp", cat("red", "wing", "tower")):
                self.assertNotIn(w, text.lower())
            self.assertEqual(bd(["--check", cat("qvx", "lorp", "ton"), "--denylist", str(out)]), 0)
            self.assertEqual(bd(["--check", cat("Red-Wi", "ng_Tow", "er"), "--denylist", str(out)]), 0)
            self.assertEqual(bd(["--check", "unlisted", "--denylist", str(out)]), 1)
            self.assertEqual(bd(["--verify", str(words), "--denylist", str(out)]), 0)
            cfg = cfg_for(out)
            self.assertTrue(cfg.denylist_usable)
            self.assertEqual(rules(cat("see ", "qvx", "lorp", "ton\n"), cfg=cfg), ["denylist"])

    def test_refuses_word_list_inside_repo_and_empty_input(self):
        inside = ROOT / "scripts" / "_tmp_wordlist_for_test.txt"
        inside.write_text("whatever\n")
        try:
            self.assertEqual(bd([str(inside), "--out", os.devnull]), 2)
        finally:
            inside.unlink()
        with tempfile.TemporaryDirectory() as d:
            only_short = Path(d) / "w.txt"
            only_short.write_text("ab\n# c\n")
            self.assertEqual(bd([str(only_short), "--out", str(Path(d) / "o")]), 2)
        self.assertEqual(bd([]), 2)


class DoctorOnly(unittest.TestCase):
    def doctor(self, *args):
        return subprocess.run([PY, "-B", str(HERE / "doctor.py"), *args], capture_output=True, text=True, cwd=str(ROOT))

    def test_unknown_check_name_exits_2_and_lists_valid_names(self):
        out = self.doctor("--only", "bogus")
        self.assertEqual(out.returncode, 2)
        self.assertIn("portability_scan", out.stderr)
        self.assertNotIn("ALL GREEN", out.stdout + out.stderr)
        self.assertEqual(self.doctor("--only", "site_profile,bogus").returncode, 2)

    def test_known_name_runs(self):
        out = self.doctor("--only", "site_profile", "--json")
        self.assertIn(out.returncode, (0, 1))
        self.assertEqual([r["name"] for r in json.loads(out.stdout)["results"]], ["site_profile"])


class SelfHosting(unittest.TestCase):
    def test_scanner_and_its_tests_are_clean(self):
        files = ["scripts/secret_scan.py", "scripts/build_denylist.py", "scripts/test_secret_scan.py",
                 ".githooks/pre-commit", ".githooks/pre-push", ".github/workflows/ci.yml"]
        found = secret_scan.scan_paths(files, EMPTY, require_denylist=False, base=ROOT)
        self.assertEqual([str(f) for f in found], [])


class FixtureAllowances(unittest.TestCase):
    def test_factory_dhcp_subnets_are_allowed(self):
        for c in (1, 2, 3, 88):
            self.assertEqual(rules("gateway 192.168.%d.1\n" % c, "a.md"), [])
        self.assertIn("rfc1918-ip", rules("gateway 192.168.7.1\n", "a.md"))  # secret-scan:allow(address fixture for the factory-subnet allowance test)

    def test_repeated_character_ids_are_fixtures_but_real_looking_ones_are_not(self):
        fake = "c_" + "0" * 16 + "@group.calendar.google.com"
        self.assertEqual(rules('"x": "%s"\n' % fake, "a.json"), [])
        real = "c_" + "a1b2c3d4e5f60718" + "@group.calendar.google.com"
        self.assertIn("email", rules('"x": "%s"\n' % real, "a.json"))


if __name__ == "__main__":
    unittest.main()
