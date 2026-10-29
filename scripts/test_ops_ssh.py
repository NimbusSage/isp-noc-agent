#!/usr/bin/env python3
"""
Tests for the device-touching ops scripts: scripts/ssh_common.py, scripts/credstore.py,
scripts/ssh_opts.sh, mk.sh, temp_bgp_rollback.sh, temp_bgp_peering.py, airos_rename.py,
wave_rename.py.

No device is ever contacted: `sshpass` is replaced by a fake on PATH that records its argv and
the SSHPASS variable. Passwords are fake strings built at runtime.

    python3 -B -m unittest discover -s scripts -p 'test_ops_ssh.py'
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import credstore  # noqa: E402
import ssh_common  # noqa: E402

FAKE_SSHPASS = """#!/usr/bin/env bash
{
  printf 'ARGV:'; printf ' [%s]' "$@"; printf '\\n'
  printf 'SSHPASS=%s\\n' "${SSHPASS-<unset>}"
} >> "$FAKE_SSH_LOG"
if [[ -n "${FAKE_SSH_STDIN_LOG:-}" ]]; then cat >> "$FAKE_SSH_STDIN_LOG"; fi
echo "fake-ok"
exit "${FAKE_SSH_RC:-0}"
"""

PW1 = "pw-" + os.urandom(4).hex() + '"$x y'      # awkward on purpose: quote, dollar, space
PW2 = "pw-" + os.urandom(4).hex()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="opsssh-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "sshpass"
        fake.write_text(FAKE_SSHPASS)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "ssh.log"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("NOC_", "SSH_", "FAKE_", "TEMP_BGP"))}
        env.update({
            "PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.tmp / "home"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"), "FAKE_SSH_LOG": str(self.log),
            "NOC_DOTENV": str(self.tmp / "absent.env"), "PYTHONDONTWRITEBYTECODE": "1",
            "NOC_STATE_DIR": str(self.tmp / "state"),
        })
        self.env = env

    def calls(self):
        text = self.log.read_text() if self.log.exists() else ""
        out = []
        for block in text.split("ARGV:")[1:]:
            argv_part, _, rest = block.partition("\n")
            out.append({"argv": re.findall(r"\[(.*?)\]", argv_part) if False else argv_part,
                        "sshpass": rest.split("SSHPASS=", 1)[1].split("\n", 1)[0] if "SSHPASS=" in rest else None})
        return out


class TestSshCommon(Sandbox):
    def setUp(self):
        super().setUp()
        self._old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._old)))

    def test_argv_shape_no_shell_no_password_no_dash_p(self):
        argv = ssh_common.build_argv("admin", "10.1.2.3", "/system identity print")
        self.assertEqual(argv[:3], ["sshpass", "-e", "ssh"])
        self.assertNotIn("-p", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("-n", argv)
        self.assertEqual(argv[-2:], ["admin@10.1.2.3", "/system identity print"])
        self.assertTrue(any(a.startswith("UserKnownHostsFile=") and a.endswith("nocagent/known_hosts") for a in argv))
        kh_dir = Path(os.environ["XDG_CACHE_HOME"]) / "nocagent"
        self.assertEqual(oct(kh_dir.stat().st_mode & 0o777), "0o700")

    def test_stdin_payload_drops_dash_n(self):
        # ssh -n would silently replace the piped file with /dev/null (a restore would write an empty config)
        self.assertNotIn("-n", ssh_common.build_argv("u", "10.0.0.1", "cat > /tmp/x", with_stdin=True))

    def test_hostkey_modes(self):
        os.environ["SSH_HOSTKEY_CHECKING"] = "yes"
        self.assertIn("StrictHostKeyChecking=yes", ssh_common.build_argv("u", "10.0.0.1", "c"))
        os.environ["SSH_HOSTKEY_CHECKING"] = "no"
        argv = ssh_common.build_argv("u", "10.0.0.1", "c")
        self.assertIn("StrictHostKeyChecking=no", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)
        os.environ["SSH_HOSTKEY_CHECKING"] = "bogus"
        with self.assertRaises(ssh_common.SshConfigError):
            ssh_common.build_argv("u", "10.0.0.1", "c")

    def test_default_is_never_strict_no(self):
        os.environ.pop("SSH_HOSTKEY_CHECKING", None)
        self.assertNotIn("StrictHostKeyChecking=no", " ".join(ssh_common.build_argv("u", "10.0.0.1", "c")))

    def test_unsafe_targets_refused(self):
        for user, host in (("a b", "10.0.0.1"), ("-oProxyCommand=x", "10.0.0.1"), ("u", "-oProxyCommand=x"),
                           ("u", "host;reboot"), ("u", "10.0.0.1 -v"), ("u", ""), ("", "10.0.0.1")):
            with self.assertRaises(ssh_common.SshConfigError, msg=(user, host)):
                ssh_common.build_argv(user, host, "c")

    def test_run_ssh_puts_password_only_in_child_env(self):
        r, err = ssh_common.run_ssh("admin", "10.1.2.3", "/ip address print", PW1)
        self.assertIsNone(err)
        self.assertEqual(r.returncode, 0)
        text = self.log.read_text()
        self.assertNotIn(PW1, text.split("SSHPASS=")[0])          # not in argv
        self.assertIn("SSHPASS=" + PW1, text)                     # but delivered through the environment
        self.assertIn("[-e]", text)

    def test_stdin_data_reaches_the_child(self):
        os.environ["FAKE_SSH_STDIN_LOG"] = str(self.tmp / "stdin.log")
        ssh_common.run_ssh("u", "10.0.0.1", "cat > /tmp/system.cfg", "pw", stdin_data="line1\nline2\n")
        self.assertEqual((self.tmp / "stdin.log").read_text(), "line1\nline2\n")


class TestCredstore(Sandbox):
    def setUp(self):
        super().setUp()
        self._old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._old)))
        self.node = shutil.which("node")

    def make_store(self, passphrase="phrase-" + os.urandom(3).hex()):  # secret-scan:allow(fake credential or address fixture for an offline test)
        store = {"version": 1, "deviceTypes": [
            {"deviceType": "mikrotik-ssh", "policy": {}, "credentials": [
                {"name": "a", "username": "admin", "password": PW2, "priority": 1, "tags": []},
                {"name": "b", "username": "admin", "password": PW1, "priority": 9, "tags": []}]},
            {"deviceType": "customer-airos", "policy": {}, "credentials": [
                {"name": "c", "username": "ubnt", "password": "cust", "priority": 1, "tags": []}]}],  # secret-scan:allow(fake credential or address fixture for an offline test)
            "exactDeviceCredentials": []}
        out = self.tmp / "credentials.enc"
        js = (f"import {{encryptStore}} from {json.dumps((SCRIPTS / 'cred_store.mjs').as_uri())};"
              "import {writeFileSync} from 'node:fs';"
              f"writeFileSync({json.dumps(str(out))}, encryptStore(JSON.parse(process.env.STORE_JSON), process.env.PP));")
        subprocess.run([self.node, "--input-type=module", "-e", js], check=True,
                       env=dict(os.environ, STORE_JSON=json.dumps(store), PP=passphrase))
        os.environ["NOC_CREDENTIALS_ENC"] = str(out)
        return passphrase

    def test_no_source_gives_a_clear_message(self):
        with self.assertRaises(credstore.CredentialError) as cm:
            credstore.pool("mikrotik-ssh")
        msg = str(cm.exception)
        self.assertIn("NOC_MIKROTIK_SSH_PASSWORD", msg)
        self.assertIn("encrypted MCP store", msg)

    def test_env_override_wins_and_defaults_admin_for_mikrotik(self):
        os.environ["NOC_MIKROTIK_SSH_PASSWORD"] = PW1
        creds = credstore.pool("mikrotik-ssh")
        self.assertEqual([(c["username"], c["password"]) for c in creds], [("admin", PW1)])
        os.environ["NOC_CUSTOMER_AIROS_PASSWORD"] = "x"
        with self.assertRaises(credstore.CredentialError):     # non-mikrotik types need an explicit user too
            credstore.pool("customer-airos")
        os.environ["NOC_CUSTOMER_AIROS_USER"] = "ubnt"
        self.assertEqual(credstore.pool("customer-airos")[0]["username"], "ubnt")

    @unittest.skipUnless(shutil.which("node"), "node required")
    def test_encrypted_store_is_read_in_priority_order(self):
        os.environ["NOC_MCP_PASSPHRASE"] = self.make_store()
        creds = credstore.pool("mikrotik-ssh")
        self.assertEqual([c["password"] for c in creds], [PW1, PW2])
        self.assertEqual([c["password"] for c in credstore.pool("mikrotik-ssh", user="admin")], [PW1, PW2])

    @unittest.skipUnless(shutil.which("node"), "node required")
    def test_passphrase_from_dotenv_file_and_wrong_passphrase(self):
        pp = self.make_store()
        (self.tmp / "absent.env").write_text(f"NOC_MCP_PASSPHRASE='{pp}'\n")
        self.assertEqual(len(credstore.pool("mikrotik-ssh")), 2)
        os.environ["NOC_MCP_PASSPHRASE"] = "wrong"
        with self.assertRaises(credstore.CredentialError) as cm:
            credstore.pool("mikrotik-ssh")
        self.assertNotIn(pp, str(cm.exception))

    @unittest.skipUnless(shutil.which("node"), "node required")
    def test_unknown_pool_in_store(self):
        os.environ["NOC_MCP_PASSPHRASE"] = self.make_store()
        with self.assertRaises(credstore.CredentialError):
            credstore.pool("ubnt-wave")

    def test_plaintext_credentials_json_is_never_consulted(self):
        src = (SCRIPTS / "credstore.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        self.assertNotIn("credentials.json", code.replace("credentials.json is no longer read", ""))

    def test_cli_refuses_tty_and_unknown(self):
        self.assertEqual(credstore.main(["credstore.py"]), 1)


class TestSourcesAreClean(unittest.TestCase):
    FILES = ["temp_bgp_peering.py", "temp_bgp_rollback.sh", "mk.sh", "wave_rename.py", "airos_rename.py",
             "fleet_bind_sweep.mjs", "oxidized_fetch.mjs", "provision_exact_binding.mjs", "mt_mactelnet.mjs",
             "wave_gui.mjs", "rssi_drop_graph.py", "cpe_binding_validate.mjs", "google_user_auth.mjs",
             "ssh_common.py", "ssh_opts.sh", "ssh_opts.mjs", "credstore.py", "cred_store.mjs"]

    def code_of(self, name):
        lines = []
        for l in (SCRIPTS / name).read_text().splitlines():
            t = l.lstrip()
            if t.startswith("#") or t.startswith("//"):
                continue
            lines.append(l)
        code = "\n".join(lines)
        return re.sub(r'""".*?"""', "", code, flags=re.S)

    def test_no_forbidden_patterns(self):
        forbidden = {
            "shell=True": r"shell\s*=\s*True",
            "sshpass -p": r"sshpass[\"', \[]+-p\b",  # secret-scan:allow(fake credential or address fixture for an offline test)
            "StrictHostKeyChecking=no literal outside the opt-in branch": None,
            "plaintext creds": r"credentials\.json",
            "bash -c source": r"source\s+\{|bash\s*,\s*\"-c\"",
        }
        for name in self.FILES:
            code = self.code_of(name)
            for label, pat in forbidden.items():
                if pat is None or (label == "plaintext creds" and name == "credstore.py"):  # message text only
                    continue
                self.assertIsNone(re.search(pat, code), (name, label))

    def test_strict_no_only_behind_the_opt_in(self):
        for name in self.FILES:
            code = self.code_of(name)
            hits = [m.start() for m in re.finditer(r"StrictHostKeyChecking=no", code)]
            if not hits:
                continue
            self.assertIn(name, {"ssh_common.py", "ssh_opts.sh", "ssh_opts.mjs"}, name)

    def test_bash_scripts_parse(self):
        for name in ("mk.sh", "temp_bgp_rollback.sh", "ssh_opts.sh"):
            r = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (name, r.stderr))


class TestMkSh(Sandbox):
    def run_mk(self, *args, extra=None):
        env = dict(self.env, **(extra or {}))
        return subprocess.run(["bash", str(SCRIPTS / "mk.sh"), *args], capture_output=True, text=True, env=env,
                              timeout=60)

    def test_runs_with_env_password_via_sshpass_e(self):
        r = self.run_mk("10.9.8.7", "/system identity print", extra={"NOC_MIKROTIK_SSH_PASSWORD": PW1})  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fake-ok", r.stdout)
        text = self.log.read_text()
        argv_line = text.split("SSHPASS=")[0]
        self.assertNotIn(PW1, argv_line)
        self.assertIn("SSHPASS=" + PW1, text)
        self.assertIn("[-e] [ssh]", argv_line)
        self.assertIn("StrictHostKeyChecking=accept-new", argv_line)
        self.assertIn("[admin@10.9.8.7]", argv_line)  # secret-scan:allow(fake credential or address fixture for an offline test)

    def test_no_credentials_fails_with_clear_message(self):
        r = self.run_mk("10.9.8.7", "/system identity print")  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOC_MIKROTIK_SSH_PASSWORD", r.stderr)
        self.assertFalse(self.log.exists())

    def test_rejects_non_ip_router(self):
        r = self.run_mk("10.0.0.1;reboot", "x", extra={"NOC_MIKROTIK_SSH_PASSWORD": PW1})
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_bad_hostkey_mode_rejected(self):
        r = self.run_mk("10.9.8.7", "x", extra={"NOC_MIKROTIK_SSH_PASSWORD": PW1, "SSH_HOSTKEY_CHECKING": "never"})  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.log.exists())


class TestRollbackSh(Sandbox):
    def setUp(self):
        super().setUp()
        # Private copy of the repo layout so the script reads OUR config, not site-pack/.
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "site-pack" / "topology").mkdir(parents=True)
        for name in ("temp_bgp_rollback.sh", "ssh_opts.sh", "credstore.py", "envfile.py", "noc_config.py", "cred_store.mjs"):
            shutil.copy(SCRIPTS / name, self.repo / "scripts" / name)
        self.cfg = {
            "oxidized_base_url": "http://ox.test.invalid:8888",
            "networks": {"fiber-a": {"rr_ips": ["10.20.0.1"]}},  # secret-scan:allow(fake credential or address fixture for an offline test)
            "default_rrs": ["10.20.0.1"], "peer_prefixes": {"fiber-a": "10.30."}, "test_rr": "10.20.0.1",  # secret-scan:allow(fake credential or address fixture for an offline test)
            "rr_credentials": [{"ip": "10.20.0.1", "password": PW1}],  # secret-scan:allow(fake credential or address fixture for an offline test)
        }
        self.write_cfg(self.cfg)
        self.env["NOC_MIKROTIK_SSH_PASSWORD"] = PW2

    def write_cfg(self, cfg):
        (self.repo / "site-pack" / "topology" / "temp-bgp-config.json").write_text(json.dumps(cfg))

    def run_rollback(self, *args):
        return subprocess.run(["bash", str(self.repo / "scripts" / "temp_bgp_rollback.sh"), *args],
                              capture_output=True, text=True, env=self.env, timeout=60)

    def test_router_password_can_live_in_the_local_overlay(self):
        cfg = json.loads(json.dumps(self.cfg))
        del cfg["rr_credentials"][0]["password"]
        self.write_cfg(cfg)
        (self.repo / "site-pack" / "topology" / "temp-bgp-config.local.json").write_text(
            json.dumps({"rr_credentials": [{"ip": "10.20.0.1", "password": PW1}]}))  # secret-scan:allow(fake credential or address fixture for an offline test)
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("SSHPASS=" + PW1, self.log.read_text())

    def test_broken_overlay_stops_the_run(self):
        (self.repo / "site-pack" / "topology" / "temp-bgp-config.local.json").write_text("{nope")
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("temp-bgp-config.local.json", r.stderr)

    def test_dry_run_uses_sshpass_e_and_keeps_passwords_out_of_argv(self):
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("[dry-run] would disable TEMP on 10.20.0.1", r.stdout)  # secret-scan:allow(fake credential or address fixture for an offline test)
        text = self.log.read_text()
        self.assertIn("SSHPASS=" + PW1, text)
        for chunk in text.split("SSHPASS="):
            pass
        argv_lines = [l for l in text.splitlines() if l.startswith("ARGV:")]
        self.assertTrue(argv_lines)
        for l in argv_lines:
            self.assertNotIn(PW1, l)
            self.assertNotIn(PW2, l)
            self.assertIn("[-e] [ssh]", l)
            self.assertNotIn("[-p]", l)

    def test_config_is_data_not_code(self):
        marker = self.tmp / "pwned"
        cfg = dict(self.cfg)
        cfg["rr_credentials"] = [{"ip": "10.20.0.1", "password": f'x"; touch {marker}; echo "'}]  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.write_cfg(cfg)
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(marker.exists())
        self.assertIn(f'SSHPASS=x"; touch {marker}; echo "', self.log.read_text())

    def test_bad_values_rejected(self):
        bad = dict(self.cfg)
        bad["networks"] = {"fiber-a": {"rr_ips": ["10.20.0.1; reboot"]}}  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.write_cfg(bad)
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())
        self.write_cfg(self.cfg)
        self.assertEqual(self.run_rollback("--network", "a b", "--dry-run").returncode, 2)

    def test_placeholder_config_and_missing_creds_fail_closed(self):
        ph = dict(self.cfg)
        ph["rr_credentials"] = [{"ip": "10.20.0.1", "password": "<rr-password-1>"}]  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.write_cfg(ph)
        self.assertEqual(self.run_rollback("--network", "fiber-a", "--dry-run").returncode, 2)
        self.write_cfg(self.cfg)
        del self.env["NOC_MIKROTIK_SSH_PASSWORD"]
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("NOC_MIKROTIK_SSH_PASSWORD", r.stderr)

    def test_password_env_indirection(self):
        cfg = dict(self.cfg)
        cfg["rr_credentials"] = [{"ip": "10.20.0.1", "password_env": "TEMP_BGP_RR1_PASSWORD"}]  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.write_cfg(cfg)
        self.env["TEMP_BGP_RR1_PASSWORD"] = PW1
        r = self.run_rollback("--network", "fiber-a", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SSHPASS=" + PW1, self.log.read_text())


class TestTempBgpPeering(Sandbox):
    def setUp(self):
        super().setUp()
        import importlib
        # Same isolation as the other classes: an in-process call must never see this machine's real
        # .env or credential store (NOC_DOTENV points at a missing file in self.env).
        self._old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._old)))
        self.mod = importlib.import_module("temp_bgp_peering")
        self.addCleanup(lambda: setattr(self.mod, "_CREDS", None))

    def test_import_has_no_side_effects(self):
        self.assertEqual(self.mod.BGP_CFG, {})                 # config is loaded by main(), not at import

    def test_subnet_and_network_validation(self):
        for bad in ("198.51.100.0/24; reboot", "$(id)", "1.2.3.4", "1.2.3.4/33x", "1.2.3.4/33", "999.1.1.1/24", "../../x"):  # secret-scan:allow(fake credential or address fixture for an offline test)
            with self.assertRaises(SystemExit, msg=bad):
                self.mod.validate_subnet(bad)
        self.assertEqual(self.mod.validate_subnet("198.51.100.0/24"), "198.51.100.0/24")
        for bad in ("Fiber A", "a;b", "", "-x", "a" * 41, "../x"):
            with self.assertRaises(SystemExit, msg=bad):
                self.mod.validate_network(bad)
        self.assertEqual(self.mod.validate_network("Fiber-1"), "fiber-1")

    def test_address_gate(self):
        with self.assertRaises(SystemExit):
            self.mod._ipv4("10.0.0.1; reboot")
        self.assertEqual(self.mod._ipv4(" 10.0.0.1 "), "10.0.0.1")

    def test_ssh_wrapper_runs_without_a_shell(self):
        calls = []

        def fake_run(user, host, cmd, pw, **kw):
            calls.append((user, host, cmd, pw))
            return (subprocess.CompletedProcess([], 0, "packet-loss=0%", ""), None)
        orig = self.mod.ssh_common.run_ssh
        self.mod.ssh_common.run_ssh = fake_run
        self.addCleanup(lambda: setattr(self.mod.ssh_common, "run_ssh", orig))
        r = self.mod.ssh([("admin", "p1")], "10.0.0.1", "/ping address=10.0.0.2 count=2")
        self.assertEqual(r.stdout, "packet-loss=0%")
        self.assertEqual(calls, [("admin", "10.0.0.1", "/ping address=10.0.0.2 count=2", "p1")])

    def test_permission_denied_falls_through_to_next_credential(self):
        seq = iter([(subprocess.CompletedProcess([], 255, "", "Permission denied"), None),
                    (subprocess.CompletedProcess([], 0, "fine", ""), None)])
        orig = self.mod.ssh_common.run_ssh
        self.mod.ssh_common.run_ssh = lambda *a, **k: next(seq)
        self.addCleanup(lambda: setattr(self.mod.ssh_common, "run_ssh", orig))
        self.assertEqual(self.mod.ssh([("a", "x"), ("b", "y")], "10.0.0.1", "c").stdout, "fine")

    def test_timeout_does_not_crash(self):
        orig = self.mod.ssh_common.run_ssh
        self.mod.ssh_common.run_ssh = lambda *a, **k: (None, "SSH timed out")
        self.addCleanup(lambda: setattr(self.mod.ssh_common, "run_ssh", orig))
        r = self.mod.ssh([("a", "x")], "10.0.0.1", "c")
        self.assertEqual((r.returncode, r.stdout), (255, ""))

    def test_missing_credentials_message(self):
        with self.assertRaises(SystemExit) as cm:
            self.mod.tower_creds()
        self.assertIn("NOC_MIKROTIK_SSH_PASSWORD", str(cm.exception))

    def test_cli_rejects_bad_args_before_touching_anything(self):
        for argv in (["--subnet", "1.2.3.4/24;reboot"], ["--network", "a b"]):  # secret-scan:allow(fake credential or address fixture for an offline test)
            r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "temp_bgp_peering.py"), *argv],
                               capture_output=True, text=True, env=self.env, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(self.log.exists())


class TestRenameScripts(Sandbox):
    def setUp(self):
        super().setUp()
        self._old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        os.environ.update({"NOC_CUSTOMER_AIROS_USER": "ubnt", "NOC_CUSTOMER_AIROS_PASSWORD": PW1,
                           "NOC_UBNT_WAVE_USER": "ubnt", "NOC_UBNT_WAVE_PASSWORD": PW2})
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._old)))
        import importlib
        self.airos = importlib.import_module("airos_rename")
        self.wave = importlib.import_module("wave_rename")
        for m in (self.airos, self.wave):
            m._POOLS.clear()
            m.time.sleep = lambda s: None            # dropbear throttle pause is irrelevant here

    def test_airos_ssh_goes_through_sshpass_e(self):
        out, err = self.airos.ssh("10.5.5.5", "ubnt", PW1, "cat /tmp/system.cfg")  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual((out.strip(), err), ("fake-ok", None))
        text = self.log.read_text()
        self.assertNotIn(PW1, text.split("SSHPASS=")[0])
        self.assertIn("HostKeyAlgorithms=+ssh-rsa", text)
        self.assertIn("StrictHostKeyChecking=accept-new", text)

    def test_restore_stdin_is_not_discarded_by_dash_n(self):
        os.environ["FAKE_SSH_STDIN_LOG"] = str(self.tmp / "stdin.log")
        self.airos.ssh("10.5.5.5", "ubnt", PW1, "cat > /tmp/system.cfg", stdin_data="wireless.1.ssid=ABC\n")  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual((self.tmp / "stdin.log").read_text(), "wireless.1.ssid=ABC\n")
        self.assertNotIn("[-n]", self.log.read_text())

    def test_try_pool_uses_env_credentials(self):
        out, cred = self.airos.try_pool("10.5.5.5", "customer-airos", "uptime")  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual((out.strip(), cred), ("fake-ok", "env"))
        out, cred = self.wave.try_pool("10.5.5.6", "ubnt-wave", "ucfg show")  # secret-scan:allow(fake credential or address fixture for an offline test)
        self.assertEqual(cred, "env")

    def test_unsafe_host_is_reported_not_executed(self):
        out, err = self.airos.ssh("-oProxyCommand=x", "ubnt", PW1, "id")
        self.assertIsNone(out)
        self.assertIn("unsafe", err)
        self.assertFalse(self.log.exists())

    def test_no_credentials_exits_with_instructions(self):
        os.environ.pop("NOC_CUSTOMER_AIROS_PASSWORD")
        self.airos._POOLS.clear()
        with self.assertRaises(SystemExit) as cm:
            self.airos.load_pool("customer-airos")
        self.assertIn("NOC_CUSTOMER_AIROS_USER", str(cm.exception))

    def test_cli_rejects_option_like_host(self):
        r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "airos_rename.py"), "assoc", "-oProxyCommand=x",
                            "--pool", "customer-airos"], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_selftests_still_pass(self):
        for name in ("airos_rename.py", "wave_rename.py"):
            r = subprocess.run([sys.executable, "-B", str(SCRIPTS / name), "selftest"], capture_output=True, text=True,
                               env=self.env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
