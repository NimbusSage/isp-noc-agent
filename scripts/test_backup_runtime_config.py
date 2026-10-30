#!/usr/bin/env python3
"""
Tests for scripts/backup_runtime_config.py in a private fake repo layout (copy of the script + its
helpers), so the real repo's .env / site-pack / logs are never read. Passphrases are made up at runtime.

    python3 -B -m unittest discover -s scripts -p 'test_backup_runtime_config.py'
"""

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
HAVE_OPENSSL = shutil.which("openssl") is not None


class BackupBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        for n in ("backup_runtime_config.py", "envfile.py", "noc_config.py"):
            shutil.copy(SCRIPTS / n, self.repo / "scripts" / n)
        (self.repo / ".env").write_text("NOC_MCP_PASSPHRASE=fake-mcp-pass\n")
        (self.repo / "site-pack" / "topology").mkdir(parents=True)
        (self.repo / "site-pack" / "profile.json").write_text('{"org": {}}')
        (self.repo / "site-pack" / "profile.local.json").write_text('{"org": {"portability_tokens": ["x"]}}')
        (self.repo / "site-pack" / "topology" / "temp-bgp-config.local.json").write_text("{}")
        (self.repo / "site-pack" / "topology" / "map.json").write_text("{}")
        cfg = self.repo / "mcp-server" / "config"
        cfg.mkdir(parents=True)
        (cfg / "credentials.enc").write_bytes(os.urandom(64))
        self.state = self.tmp / "state"
        (self.state / "decisions").mkdir(parents=True)
        (self.state / "decisions" / "2026-10.jsonl").write_text('{"kind":"bounce"}\n')
        (self.state / "circuit_breaker.json").write_text('{"runs": []}')
        (self.state / "pending_peak_checks.json").write_text("[]")
        self.out = self.tmp / "backups"
        self.pw = "bk-" + os.urandom(6).hex()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BACKUP_", "NOC_", "PEAK_", "DECISION_", "BREAKER_"))}
        env.update({"BACKUP_DIR": str(self.out), "NOC_STATE_DIR": str(self.state), "PYTHONDONTWRITEBYTECODE": "1",
                    "NOC_DOTENV": str(self.repo / ".env")})
        self.env = env

    def run_script(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", str(self.repo / "scripts" / "backup_runtime_config.py"), *args],
                              capture_output=True, text=True, env=env or self.env, timeout=120)

    def archives(self):
        return sorted(p.name for p in self.out.iterdir() if p.name.startswith("nocagent-config-"))


class TestPlaintextFallback(BackupBase):
    def test_default_invocation_works_with_private_modes_and_loud_warning(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING", r.stderr)
        self.assertIn("NOT ENCRYPTED", r.stderr)
        self.assertIn("BACKUP_PASSPHRASE is not set", r.stderr)
        names = self.archives()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".tgz"))
        arc = self.out / names[0]
        self.assertEqual(oct(arc.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o700")
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith(".")], [])   # no leftover partials
        with tarfile.open(arc) as t:
            members = sorted(m.name for m in t.getmembers())
        for want in ("runtime/.env", "runtime/profile.json", "runtime/profile.local.json", "runtime/credentials.enc",
                     "runtime/topology__map.json", "runtime/topology__temp-bgp-config.local.json", "runtime/state__decisions__2026-10.jsonl",
                     "runtime/state__circuit_breaker.json", "runtime/state__pending_peak_checks.json"):
            self.assertIn(want, members)

    def test_wide_existing_backup_dir_is_tightened(self):
        self.out.mkdir(mode=0o755)
        os.chmod(self.out, 0o755)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o700")

    def test_missing_state_files_are_fine(self):
        shutil.rmtree(self.state)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_peak_queue_env_override_and_retention(self):
        custom = self.tmp / "custom-queue.json"
        custom.write_text("[1]")
        env = dict(self.env, PEAK_QUEUE_FILE=str(custom), BACKUP_KEEP="2")
        for _ in range(3):
            r = self.run_script(env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            import time
            time.sleep(1.1)                                  # archive names carry a 1s-resolution stamp
        self.assertEqual(len(self.archives()), 2)
        with tarfile.open(self.out / self.archives()[-1]) as t:
            self.assertIn("runtime/state__custom-queue.json", [m.name for m in t.getmembers()])

    def test_no_openssl_still_writes_0600_with_warning(self):
        empty = self.tmp / "emptybin"
        empty.mkdir()
        env = dict(self.env, PATH=str(empty), BACKUP_PASSPHRASE=self.pw)
        r = self.run_script(env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("openssl is not installed", r.stderr)
        self.assertNotIn(self.pw, r.stdout + r.stderr)
        arc = self.out / self.archives()[0]
        self.assertTrue(arc.name.endswith(".tgz"))
        self.assertEqual(oct(arc.stat().st_mode & 0o777), "0o600")

    def test_help_contains_restore_instructions(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0)
        for needle in ("RESTORE", "openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000", "BACKUP_PASSPHRASE",
                       "state__decisions__YYYY-MM.jsonl", "topology__a__b.json"):
            self.assertIn(needle, r.stdout)


@unittest.skipUnless(HAVE_OPENSSL, "openssl required")
class TestEncrypted(BackupBase):
    def test_encrypts_and_deletes_the_plaintext_tar(self):
        r = self.run_script(env=dict(self.env, BACKUP_PASSPHRASE=self.pw))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("NOT ENCRYPTED", r.stderr)
        self.assertNotIn(self.pw, r.stdout + r.stderr)
        names = self.archives()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".tgz.enc"))
        enc = self.out / names[0]
        self.assertEqual(oct(enc.stat().st_mode & 0o777), "0o600")
        self.assertEqual(enc.read_bytes()[:8], b"Salted__")             # openssl enc -salt format
        self.assertEqual([p.name for p in self.out.iterdir() if not p.name.startswith("nocagent-config-")], [])
        self.assertIn("encrypted", r.stdout)

    def test_manual_openssl_decrypt_round_trip(self):
        self.run_script(env=dict(self.env, BACKUP_PASSPHRASE=self.pw))
        enc = self.out / self.archives()[0]
        dec = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                              "-pass", "env:BACKUP_PASSPHRASE", "-in", str(enc)],
                             capture_output=True, env=dict(os.environ, BACKUP_PASSPHRASE=self.pw))
        self.assertEqual(dec.returncode, 0, dec.stderr)
        plain = self.tmp / "plain.tgz"
        plain.write_bytes(dec.stdout)
        with tarfile.open(plain) as t:
            data = t.extractfile("runtime/.env").read()
        self.assertEqual(data, b"NOC_MCP_PASSPHRASE=fake-mcp-pass\n")
        bad = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                              "-pass", "env:BACKUP_PASSPHRASE", "-in", str(enc)],
                             capture_output=True, env=dict(os.environ, BACKUP_PASSPHRASE="wrong"))  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertNotEqual(bad.returncode, 0)

    def test_restore_and_list_decrypt_automatically(self):
        env = dict(self.env, BACKUP_PASSPHRASE=self.pw)
        self.run_script(env=env)
        enc = str(self.out / self.archives()[0])
        dest = self.tmp / "restore"
        r = self.run_script("--restore", enc, str(dest), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = dest / "runtime" / ".env"
        self.assertEqual(got.read_text(), "NOC_MCP_PASSPHRASE=fake-mcp-pass\n")
        self.assertEqual(oct(got.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((dest / "runtime").stat().st_mode & 0o777), "0o700")
        self.assertTrue((dest / "runtime" / "state__decisions__2026-10.jsonl").exists())
        lst = self.run_script("--list", enc, env=env)
        self.assertIn("runtime/credentials.enc", lst.stdout)

    def test_restore_needs_the_right_passphrase(self):
        self.run_script(env=dict(self.env, BACKUP_PASSPHRASE=self.pw))
        enc = str(self.out / self.archives()[0])
        r = self.run_script("--restore", enc, str(self.tmp / "r1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("encrypted", r.stderr)
        r = self.run_script("--restore", enc, str(self.tmp / "r2"), env=dict(self.env, BACKUP_PASSPHRASE="wrong"))  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("wrong BACKUP_PASSPHRASE", r.stderr)
        self.assertEqual([p.name for p in Path(tempfile.gettempdir()).glob("nocagent-restore-*")
                          if p.stat().st_mtime > os.path.getmtime(self.tmp)], [])

    def test_passphrase_file_variant(self):
        pf = self.tmp / "pp.txt"
        pf.write_text(self.pw + "\n")
        pf.chmod(0o600)
        r = self.run_script(env=dict(self.env, BACKUP_PASSPHRASE_FILE=str(pf)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.archives()[0].endswith(".tgz.enc"))
        self.assertNotIn(self.pw, r.stdout + r.stderr)

    def test_passphrase_is_not_taken_from_dotenv(self):
        (self.repo / ".env").write_text(f"BACKUP_PASSPHRASE={self.pw}\n")
        r = self.run_script()
        self.assertTrue(self.archives()[0].endswith(".tgz"))      # .env is inside the archive: circular, so ignored
        self.assertIn("NOT ENCRYPTED", r.stderr)

    def test_source_never_puts_the_passphrase_in_argv(self):
        src = (SCRIPTS / "backup_runtime_config.py").read_text()
        self.assertIn('"-pass", "env:BACKUP_PASSPHRASE"', src)
        self.assertNotIn('"-pass", passphrase', src)
        self.assertNotIn("pass:", src.replace("env:BACKUP_PASSPHRASE", ""))


class TestInstall(BackupBase):
    """--install places an archive's members in a fresh checkout."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        oauth_dir = self.home / ".config" / "nocagent"
        oauth_dir.mkdir(parents=True)
        self.oauth = oauth_dir / "tracker_oauth.json"
        self.oauth.write_text('{"refresh_token": "fake-refresh-' + os.urandom(4).hex() + '"}')
        (self.repo / ".env").write_text(f"NOC_MCP_PASSPHRASE=fake-mcp-pass\nTRACKER_OAUTH_FILE={self.oauth}\n")
        (self.repo / "site-pack" / "profile.local.json").write_text('{"conventions": {"x": "fake-overlay-value"}}')
        (self.repo / "site-pack" / "topology" / "temp-bgp-config.local.json").write_text('{"rr_credentials": []}')
        (self.repo / "site-pack" / "topology" / "sub").mkdir()
        (self.repo / "site-pack" / "topology" / "sub" / "deep.json").write_text('{"deep": true}')
        (self.repo / "mcp-server" / "config").mkdir(parents=True, exist_ok=True)
        (self.repo / "mcp-server" / "config" / "credentials.enc").write_bytes(os.urandom(48))
        self.env["HOME"] = str(self.home)
        self.assertEqual(self.run_script().returncode, 0)
        self.archive = str(self.out / self.archives()[0])
        self.new = self.tmp / "newrepo"
        self.new.mkdir()

    def install(self, *extra, env=None):
        return self.run_script("--install", self.archive, str(self.new), *extra, env=env)

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        r = self.install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        for want in (".env -> .env", "credentials.enc -> mcp-server/config/credentials.enc",
                     "profile.json -> site-pack/profile.json", "profile.local.json -> site-pack/profile.local.json",
                     "topology__sub__deep.json -> site-pack/topology/sub/deep.json", "would place"):
            self.assertIn(want, r.stdout)
        self.assertIn(str(self.oauth), r.stdout)
        self.assertNotIn("fake-mcp-pass", r.stdout + r.stderr)
        self.assertNotIn("fake-overlay-value", r.stdout + r.stderr)
        self.assertEqual([p for p in self.new.iterdir()], [])

    def test_places_every_member_privately_and_byte_for_byte(self):
        self.oauth.rename(self.oauth.with_suffix(".moved"))                  # the new machine has no OAuth file yet
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        for rel in (".env", "mcp-server/config/credentials.enc", "site-pack/profile.json", "site-pack/profile.local.json",
                    "site-pack/topology/temp-bgp-config.local.json", "site-pack/topology/sub/deep.json"):
            target, source = self.new / rel, self.repo / rel
            self.assertEqual(target.read_bytes(), source.read_bytes(), rel)
            self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600", rel)
        self.assertTrue(self.oauth.exists())                                 # restored at the path .env names
        self.assertEqual(oct(self.oauth.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("fake-mcp-pass", r.stdout + r.stderr)

    def test_existing_files_are_kept_unless_forced(self):
        (self.new / "site-pack").mkdir()
        (self.new / "site-pack" / "profile.json").write_text("from the pack")
        r = self.install()
        self.assertIn("kept (already exists)", r.stdout)
        self.assertEqual((self.new / "site-pack" / "profile.json").read_text(), "from the pack")
        self.assertEqual(self.install("--force").returncode, 0)
        self.assertEqual((self.new / "site-pack" / "profile.json").read_text(), '{"org": {}}')

    def test_state_is_skipped_by_default_and_placed_on_request(self):
        r = self.install()
        self.assertIn("skipped (state; use --with-state)", r.stdout)
        self.assertFalse((self.new / "logs").exists())
        self.assertEqual(self.install("--with-state").returncode, 0)
        self.assertEqual((self.new / "logs" / "decisions" / "2026-10.jsonl").read_text(), '{"kind":"bounce"}\n')
        self.assertTrue((self.new / "logs" / "circuit_breaker.json").exists())

    def test_an_oauth_path_outside_the_home_directory_is_never_written(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        target = outside / "tracker_oauth.json"
        (self.repo / ".env").write_text(f"NOC_MCP_PASSPHRASE=fake-mcp-pass\nTRACKER_OAUTH_FILE={target}\n")
        self.assertEqual(self.run_script().returncode, 0)
        self.archive = str(self.out / self.archives()[-1])
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(target.exists())

    def test_a_member_name_cannot_escape_the_site_pack(self):
        evil = self.tmp / "evil.tgz"
        with tarfile.open(evil, "w:gz") as t:
            for name in ("runtime/topology_____evil.json", "runtime/topology__a____b.json", "runtime/topology__ok.json"):
                data = self.tmp / "payload"
                data.write_text("{}")
                t.add(data, arcname=name)
        self.archive = str(evil)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.new / "site-pack" / "topology" / "ok.json").exists())
        placed = [p for p in self.new.rglob("*") if p.is_file()]
        self.assertEqual([p.name for p in placed], ["ok.json"])
        self.assertEqual(r.stdout.count("not placed"), 2)

    @unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
    def test_an_encrypted_archive_installs_with_the_passphrase(self):
        self.assertEqual(self.run_script(env=dict(self.env, BACKUP_PASSPHRASE=self.pw)).returncode, 0)
        enc = [n for n in self.archives() if n.endswith(".enc")][0]
        self.archive = str(self.out / enc)
        self.assertNotEqual(self.install().returncode, 0)                          # no passphrase
        self.assertEqual(self.install(env=dict(self.env, BACKUP_PASSPHRASE=self.pw)).returncode, 0)
        self.assertTrue((self.new / ".env").exists())


if __name__ == "__main__":
    unittest.main()
