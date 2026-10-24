"""Tests for scripts/env_set.py (temp dirs only; getpass and the tty probe are monkeypatched)."""
import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env_set  # noqa: E402
import envfile  # noqa: E402

EXAMPLE = '''# header
# --- Tier 1: devices ---
# Passphrase. REQUIRED.
NOC_MCP_PASSPHRASE="abc"
# Optional thing. OPTIONAL.
LIBRENMS_URL=""
# --- Tier 2: sonar ---
# Token. REQUIRED.
SONAR_TOKEN=""
# Port. Default: 65333. OPTIONAL.
# WEBHOOK_PORT="65333"
# Needed workspace. REQUIRED.
AGENT_WORKSPACE=""
# --- Tier 3: google ---
# Sheet id. REQUIRED.
TRACKER_SPREADSHEET_ID=""
'''
SECRET = 'S3cr3t "quoted" $HOME `id` \\ end'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.example = self.dir / ".env.example"
        self.example.write_text(EXAMPLE)
        self.env = self.dir / ".env"
        p1 = mock.patch.object(env_set, "_has_tty", lambda: True)
        p1.start()
        self.addCleanup(p1.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_cli(self, *argv, answers=None, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        answers = list(answers or [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(env_set.getpass, "getpass", lambda prompt="": answers.pop(0)), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
            rc = env_set.main(["--env-file", str(self.env), "--example", str(self.example), *argv])
        return rc, out.getvalue(), err.getvalue()

    def mode(self, p=None):
        return stat.S_IMODE(os.stat(p or self.env).st_mode)


class SetTests(Base):
    def test_creates_env_from_example_0600_blanked(self):
        rc, out, err = self.run_cli("SONAR_TOKEN", answers=["tok123"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.mode(), 0o600)
        text = self.env.read_text()
        self.assertIn("# header", text)
        self.assertIn('NOC_MCP_PASSPHRASE=""', text)          # example value blanked
        self.assertIn('# WEBHOOK_PORT="65333"', text)          # commented line untouched
        self.assertIn('SONAR_TOKEN="tok123"', text)

    def test_value_never_printed(self):
        rc, out, err = self.run_cli("SONAR_TOKEN", answers=[SECRET])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), f"set SONAR_TOKEN ({len(SECRET)} chars)")
        self.assertNotIn("S3cr3t", out + err)

    def test_special_chars_roundtrip_through_envfile_and_shell_quoting(self):
        self.run_cli("SONAR_TOKEN", answers=[SECRET])
        self.assertEqual(envfile.parse(self.env.read_text())["SONAR_TOKEN"], SECRET)
        line = [l for l in self.env.read_text().splitlines() if l.startswith("SONAR_TOKEN=")][0]
        self.assertNotIn("$HOME", line.replace("\\$HOME", ""))   # every $ is escaped

    def test_updates_only_that_key_and_keeps_others(self):
        self.run_cli("SONAR_TOKEN", answers=["one"])
        before = self.env.read_text()
        self.run_cli("SONAR_TOKEN", answers=["two"])
        after = self.env.read_text()
        self.assertEqual(before.replace('"one"', '"two"'), after)
        self.assertEqual(after.count("SONAR_TOKEN="), 1)

    def test_appends_when_key_missing_and_handles_no_trailing_newline(self):
        self.env.write_text("A_KEY=\"1\"")
        os.chmod(self.env, 0o600)
        self.run_cli("NEW_KEY", answers=["v"])
        self.assertEqual(self.env.read_text(), 'A_KEY="1"\nNEW_KEY="v"\n')

    def test_replaces_multiline_quoted_value_whole(self):
        self.env.write_text('A="1"\nB="line1\nline2"\nC="3"\n')
        os.chmod(self.env, 0o600)
        self.run_cli("B", answers=["x"])
        self.assertEqual(self.env.read_text(), 'A="1"\nB="x"\nC="3"\n')

    def test_duplicate_active_lines_collapse_to_one(self):
        self.env.write_text('B="1"\nA="a"\nB="2"\n')
        os.chmod(self.env, 0o600)
        self.run_cli("B", answers=["x"])
        self.assertEqual(self.env.read_text(), 'B="x"\nA="a"\n')

    def test_does_not_touch_commented_or_prefix_keys(self):
        self.env.write_text('# WIDGET_KEY="old"\nWIDGET_KEY_EXTRA="keep"\n')
        os.chmod(self.env, 0o600)
        self.run_cli("WIDGET_KEY", answers=["fresh"])
        text = self.env.read_text()
        self.assertIn('# WIDGET_KEY="old"', text)
        self.assertIn('WIDGET_KEY_EXTRA="keep"', text)
        self.assertIn('WIDGET_KEY="fresh"', text)

    def test_multiple_keys_prompt_each(self):
        rc, out, _ = self.run_cli("SONAR_TOKEN", "LIBRENMS_URL", answers=["a", "b"])
        self.assertEqual(rc, 0)
        vals = envfile.parse(self.env.read_text())
        self.assertEqual((vals["SONAR_TOKEN"], vals["LIBRENMS_URL"]), ("a", "b"))

    def test_invalid_key_rejected(self):
        for bad in ("lower", "1ABC", "A-B", "A B", "A;rm", "$X"):
            rc, _, err = self.run_cli(bad, answers=["v"])
            self.assertEqual(rc, 2, bad)
            self.assertFalse(self.env.exists(), bad)

    def test_invalid_values_rejected_nothing_written(self):
        for bad in ("", "a\nb", "a\rb", "a\x00b"):
            rc, out, err = self.run_cli("SONAR_TOKEN", answers=[bad])
            self.assertEqual(rc, 2)
            self.assertNotIn("a\nb", out + err)
        self.assertNotIn("SONAR_TOKEN=\"a", self.env.read_text() if self.env.exists() else "")

    def test_whitespace_value_warns_but_is_kept(self):
        rc, out, err = self.run_cli("SONAR_TOKEN", answers=["  pad "])
        self.assertEqual(rc, 0)
        self.assertIn("whitespace", err)
        self.assertNotIn("pad", out + err)
        self.assertEqual(envfile.parse(self.env.read_text())["SONAR_TOKEN"], "  pad ")

    def test_refuses_group_or_world_readable_unless_fix_perms(self):
        self.env.write_text('A="1"\n')
        os.chmod(self.env, 0o644)
        rc, _, err = self.run_cli("SONAR_TOKEN", answers=["v"])
        self.assertEqual(rc, 2)
        self.assertIn("--fix-perms", err)
        self.assertEqual(self.env.read_text(), 'A="1"\n')
        rc, _, _ = self.run_cli("--fix-perms", "SONAR_TOKEN", answers=["v"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.mode(), 0o600)

    def test_refuses_symlink(self):
        real = self.dir / "real.env"
        real.write_text('A="1"\n')
        os.chmod(real, 0o600)
        self.env.symlink_to(real)
        rc, _, err = self.run_cli("SONAR_TOKEN", answers=["v"])
        self.assertEqual(rc, 2)
        self.assertEqual(real.read_text(), 'A="1"\n')

    def test_no_temp_files_left_and_mode_kept(self):
        self.run_cli("SONAR_TOKEN", answers=["v"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".env", ".env.example"])
        self.assertEqual(self.mode(), 0o600)

    def test_failed_replace_keeps_original(self):
        self.run_cli("SONAR_TOKEN", answers=["v"])
        before = self.env.read_text()
        with mock.patch.object(env_set.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                env_set.atomic_write(self.env, "garbage")
        self.assertEqual(self.env.read_text(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".env", ".env.example"])

    def test_no_tty_refuses_without_stdin_flag(self):
        with mock.patch.object(env_set, "_has_tty", lambda: False):
            rc, _, err = self.run_cli("SONAR_TOKEN", answers=["v"])
        self.assertEqual(rc, 2)
        self.assertIn("--stdin", err)
        self.assertFalse(self.env.exists())

    def test_stdin_mode(self):
        with mock.patch.object(env_set, "_has_tty", lambda: False):
            rc, out, _ = self.run_cli("--stdin", "SONAR_TOKEN", stdin="piped-value\n")
        self.assertEqual(rc, 0)
        self.assertEqual(envfile.parse(self.env.read_text())["SONAR_TOKEN"], "piped-value")
        self.assertNotIn("piped", out)


class GenerateTests(Base):
    def test_generate_writes_random_token_without_printing(self):
        rc, out, err = self.run_cli("--generate", "SONAR_TOKEN")
        self.assertEqual(rc, 0)
        val = envfile.parse(self.env.read_text())["SONAR_TOKEN"]
        self.assertGreaterEqual(len(val), 43)
        self.assertNotIn(val, out + err)
        self.assertEqual(out.strip(), f"generated SONAR_TOKEN ({len(val)} chars)")

    def test_generate_two_differ_and_refuses_overwrite_without_force(self):
        self.run_cli("--generate", "SONAR_TOKEN", "--generate", "LIBRENMS_URL")
        v = envfile.parse(self.env.read_text())
        self.assertNotEqual(v["SONAR_TOKEN"], v["LIBRENMS_URL"])
        rc, _, err = self.run_cli("--generate", "SONAR_TOKEN")
        self.assertEqual(rc, 2)
        self.assertEqual(envfile.parse(self.env.read_text())["SONAR_TOKEN"], v["SONAR_TOKEN"])
        rc, _, _ = self.run_cli("--generate", "SONAR_TOKEN", "--force")
        self.assertEqual(rc, 0)
        self.assertNotEqual(envfile.parse(self.env.read_text())["SONAR_TOKEN"], v["SONAR_TOKEN"])


class ListMissingTests(Base):
    def test_all_required_missing_when_no_env_file(self):
        rc, out, _ = self.run_cli("--list-missing")
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines(), ["tier 1: NOC_MCP_PASSPHRASE", "tier 2: SONAR_TOKEN",
                                            "tier 2: AGENT_WORKSPACE", "tier 3: TRACKER_SPREADSHEET_ID"])
        self.assertFalse(self.env.exists())

    def test_tier_is_cumulative_and_exact_narrows(self):
        _, out, _ = self.run_cli("--list-missing", "--tier", "2")
        self.assertNotIn("TRACKER_SPREADSHEET_ID", out)
        self.assertIn("NOC_MCP_PASSPHRASE", out)
        _, out, _ = self.run_cli("--list-missing", "--tier", "2", "--exact")
        self.assertEqual(out.splitlines(), ["tier 2: SONAR_TOKEN", "tier 2: AGENT_WORKSPACE"])

    def test_set_values_drop_out_and_values_never_printed(self):
        self.env.write_text('NOC_MCP_PASSPHRASE="pw-secret"\nSONAR_TOKEN=""\nAGENT_WORKSPACE="/opt/x"\n')
        os.chmod(self.env, 0o600)
        rc, out, err = self.run_cli("--list-missing", "--tier", "2")
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines(), ["tier 2: SONAR_TOKEN"])
        self.assertNotIn("pw-secret", out + err)
        self.run_cli("SONAR_TOKEN", answers=["t"])
        rc, out, _ = self.run_cli("--list-missing", "--tier", "2")
        self.assertEqual((rc, out), (0, ""))

    def test_optional_and_commented_vars_never_listed(self):
        _, out, _ = self.run_cli("--list-missing")
        self.assertNotIn("LIBRENMS_URL", out)
        self.assertNotIn("WEBHOOK_PORT", out)

    def test_real_example_parses_with_required_in_expected_tiers(self):
        entries = env_set.parse_example(env_set.ROOT / ".env.example")
        req = {n: t for n, t, r in entries if r}
        self.assertEqual(req.get("NOC_MCP_PASSPHRASE"), 1)
        for name in ("SONAR_TOKEN", "TICKET_WEBHOOK_AUTH_TOKEN", "AGENT_WORKSPACE"):
            self.assertEqual(req.get(name), 2, name)
        self.assertGreater(len(entries), 100)


if __name__ == "__main__":
    unittest.main()
