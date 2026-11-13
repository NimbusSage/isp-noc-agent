"""Tests for scripts/hub_tls.py (no network, no lego, no real certificate)."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import hub_tls  # noqa: E402

HOST = "hub.example.com"
EMAIL = "operator" + "@" + "example.com"
TSIG_VALUE = "placeholder" + "-value"
TOKEN_VALUE = "placeholder" + "-token"
BASE = {
    "HUB_TLS_HOSTNAME": HOST,
    "HUB_TLS_EMAIL": EMAIL,
    "HUB_TLS_DNS_PROVIDER": "dnsupdate",
    "HUB_TLS_STATE_DIR": "/tmp/hub-tls-test-state",
    "DNSUPDATE_NAMESERVER": "dns.internal",
    "DNSUPDATE_TSIG_KEY": "acme-key.",
    "DNSUPDATE_TSIG_SECRET": TSIG_VALUE,
    "DNSUPDATE_TSIG_ALGORITHM": "HMAC-SHA256",
}


def run_main(argv, env=None):
    """main() with a fixed environment and lego faked as present and capable."""
    out, err = io.StringIO(), io.StringIO()
    patched = [mock.patch.object(hub_tls.envfile, "get", side_effect=lambda n, d="": (env or {}).get(n, d)),
               mock.patch.object(hub_tls.shutil, "which", side_effect=lambda b: f"/usr/bin/{b}"),
               mock.patch.object(hub_tls, "lego_supports", return_value=True)]
    for p in patched:
        p.start()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = hub_tls.main(argv)
    finally:
        for p in patched:
            p.stop()
    return code, out.getvalue(), err.getvalue()


class Hostname(unittest.TestCase):
    def test_accepts_a_plain_name_and_lowercases_it(self):
        self.assertEqual(hub_tls.validate_hostname("Hub.Example.COM."), HOST)

    def test_refuses_ip_wildcard_and_underscore(self):
        for bad in ("10.0.0.5", "*.example.com", "_x.example.com", "hub", ""):
            with self.assertRaises(hub_tls.HubTlsError, msg=bad):
                hub_tls.validate_hostname(bad)


class Algorithm(unittest.TestCase):
    def test_technitium_style_becomes_lego_style(self):
        self.assertEqual(hub_tls.normalize_tsig_algorithm("HMAC-SHA256"), "hmac-sha256.")
        self.assertEqual(hub_tls.normalize_tsig_algorithm("hmac-sha512."), "hmac-sha512.")
        self.assertEqual(hub_tls.normalize_tsig_algorithm(""), "hmac-sha256.")

    def test_refuses_unknown_algorithm(self):
        with self.assertRaises(hub_tls.HubTlsError):
            hub_tls.normalize_tsig_algorithm("md5")


class Secrets(unittest.TestCase):
    def test_secret_never_reaches_the_command_line(self):
        s = hub_tls.settings(dict(BASE), lego="/opt/lego")
        argv = " ".join(hub_tls.lego_argv(s, "run"))
        self.assertNotIn(TSIG_VALUE, argv)
        self.assertNotIn("dns.internal", argv)
        self.assertIn("--dns dnsupdate", argv)

    def test_provider_env_carries_only_the_active_backend(self):
        s = hub_tls.settings(dict(BASE, TECHNITIUM_API_TOKEN=TOKEN_VALUE))
        env = hub_tls.provider_env(s, base_env={"PATH": "/usr/bin"})
        self.assertEqual(env["DNSUPDATE_TSIG_SECRET"], TSIG_VALUE)
        self.assertEqual(env["DNSUPDATE_TSIG_ALGORITHM"], "hmac-sha256.")
        self.assertNotIn("TECHNITIUM_API_TOKEN", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_technitium_backend_sends_its_own_variables(self):
        s = hub_tls.settings({"HUB_TLS_HOSTNAME": HOST, "HUB_TLS_EMAIL": EMAIL,
                              "HUB_TLS_DNS_PROVIDER": "technitium",
                              "TECHNITIUM_SERVER_BASE_URL": "https://dns.internal:5380",
                              "TECHNITIUM_API_TOKEN": TOKEN_VALUE})
        env = hub_tls.provider_env(s, base_env={})
        self.assertEqual(env["TECHNITIUM_API_TOKEN"], TOKEN_VALUE)
        self.assertNotIn("DNSUPDATE_TSIG_SECRET", env)


class Caddy(unittest.TestCase):
    def test_site_block_points_at_the_lego_files_and_the_upstream(self):
        s = hub_tls.settings(dict(BASE, HUB_TLS_UPSTREAM="127.0.0.1:9000"))
        text = hub_tls.caddy_site(s)
        self.assertIn(HOST + " {", text)
        self.assertIn(f"tls /tmp/hub-tls-test-state/certificates/{HOST}.crt "
                      f"/tmp/hub-tls-test-state/certificates/{HOST}.key", text)
        self.assertIn("reverse_proxy 127.0.0.1:9000", text)

    def test_write_caddy_creates_the_file(self):
        target = Path("/tmp/hub-tls-test-Caddyfile")
        try:
            code, out, _ = run_main(["--write-caddy", "--caddy-config", str(target)], env=BASE)
            self.assertEqual(code, 0, out)
            self.assertIn(HOST, target.read_text())
        finally:
            if target.exists():
                target.unlink()

    def test_dry_run_writes_nothing(self):
        target = Path("/tmp/hub-tls-test-Caddyfile-dry")
        code, out, _ = run_main(["--write-caddy", "--dry-run", "--caddy-config", str(target)], env=BASE)
        self.assertEqual(code, 0)
        self.assertIn(HOST, out)
        self.assertFalse(target.exists())


class Check(unittest.TestCase):
    def test_ready_configuration_is_green(self):
        code, out, _ = run_main(["--check"], env=BASE)
        self.assertEqual(code, 0, out)
        self.assertIn("ok: ready to --issue", out)

    def test_missing_names_are_named_and_exit_3(self):
        code, out, err = run_main(["--check"], env={})
        self.assertEqual(code, 3)
        self.assertIn("HUB_TLS_HOSTNAME", out + err)
        self.assertIn("HUB_TLS_EMAIL", out + err)

    def test_missing_provider_variables_depend_on_the_backend(self):
        env = dict(BASE)
        for name in ("DNSUPDATE_NAMESERVER", "DNSUPDATE_TSIG_KEY", "DNSUPDATE_TSIG_SECRET"):
            env.pop(name)
        code, out, _ = run_main(["--check"], env=env)
        self.assertEqual(code, 3)
        self.assertIn("DNSUPDATE_TSIG_SECRET", out)

    def test_tsig_algorithm_is_optional(self):
        env = dict(BASE)
        env.pop("DNSUPDATE_TSIG_ALGORITHM")
        code, out, _ = run_main(["--check"], env=env)
        self.assertEqual(code, 0, out)
        env_used = hub_tls.provider_env(hub_tls.settings(env), base_env={})
        self.assertEqual(env_used["DNSUPDATE_TSIG_ALGORITHM"], "hmac-sha256.")

    def test_json_reports_the_same_verdict(self):
        code, out, _ = run_main(["--check", "--json"], env=BASE)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "dnsupdate")

    def test_unknown_provider_is_usage_error(self):
        code, _, err = run_main(["--check"], env=dict(BASE, HUB_TLS_DNS_PROVIDER="bind"))
        self.assertEqual(code, 2)
        self.assertIn("HUB_TLS_DNS_PROVIDER", err)

    def test_issue_refuses_when_a_setting_is_missing(self):
        code, _, err = run_main(["--issue"], env={})
        self.assertEqual(code, 3)
        self.assertIn("nothing issued", err)

    def test_issue_dry_run_shows_the_command_without_values(self):
        code, out, _ = run_main(["--issue", "--dry-run"], env=BASE)
        self.assertEqual(code, 0, out)
        self.assertIn("would run: lego", out)
        self.assertNotIn(TSIG_VALUE, out)

    def test_staging_flag_switches_the_endpoint(self):
        code, out, _ = run_main(["--check", "--staging"], env=BASE)
        self.assertEqual(code, 0)
        self.assertIn("staging", out)


class Environment(unittest.TestCase):
    def test_reads_real_environment_first(self):
        with mock.patch.dict(os.environ, {"HUB_TLS_EMAIL": EMAIL}, clear=False):
            s = hub_tls.settings()
        self.assertEqual(s["email"], EMAIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
