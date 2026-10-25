"""Tests for scripts/resources.py (temporary profile, empty .env; no real settings are read)."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noc_config  # noqa: E402
import resources  # noqa: E402

CATALOG_VARS = ["SONAR_BASE_URL", "ORG_SONAR_BASE_URL", "UISP_BASE_URL", "LIBRENMS_URL", "MYMATE_URL", "OXIDIZED_BASE_URL",
                "TEMP_OSPF_SPREADSHEET_ID", "TRACKER_SPREADSHEET_ID", "NOC_UNATTENDED"]
SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pack = self.root / "site-pack"
        self.pack.mkdir()
        self.profile = self.pack / "profile.json"
        env = {k: v for k, v in os.environ.items() if k not in CATALOG_VARS}
        env.update({"NOC_PROFILE_FILE": str(self.profile), "NOC_DOTENV": str(self.root / "absent.env"),
                    "NOC_SITE_PACK_DIR": str(self.pack)})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_profile(self, data):
        self.profile.write_text(json.dumps(data))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = resources.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class Resolution(Base):
    def test_unset_is_unset(self):
        self.assertEqual(resources.resolve("uisp").value, "")

    def test_profile_resource_is_used(self):
        self.write_profile({"resources": {"uisp": {"url": "https://uisp.corp-net.org/"}}})
        r = resources.resolve("uisp")
        self.assertEqual((r.value, r.source), ("https://uisp.corp-net.org", "profile"))

    def test_bare_legacy_host_becomes_https_url(self):
        self.write_profile({"org": {"uisp_host": "uisp.corp-net.org"}})
        r = resources.resolve("uisp")
        self.assertEqual(r.value, "https://uisp.corp-net.org")
        self.assertTrue(r.source.startswith("legacy"))

    def test_env_beats_profile_and_a_disagreement_is_reported(self):
        self.write_profile({"resources": {"librenms": {"url": "https://lnms.corp-net.org"}}})
        with mock.patch.dict(os.environ, {"LIBRENMS_URL": "http://10.1.2.3"}):
            r = resources.resolve("librenms")
        self.assertEqual(r.value, "http://10.1.2.3")
        self.assertEqual(r.source, "env LIBRENMS_URL")
        self.assertTrue(r.conflict)

    def test_same_value_in_two_places_is_not_a_conflict(self):
        self.write_profile({"resources": {"librenms": {"url": "http://10.1.2.3"}}})
        with mock.patch.dict(os.environ, {"LIBRENMS_URL": "http://10.1.2.3/"}):
            self.assertFalse(resources.resolve("librenms").conflict)

    def test_dotenv_value_is_used(self):
        (self.root / "absent.env").write_text('MYMATE_URL="https://mm.corp-net.org"\n')
        self.assertEqual(resources.resolve("mymate").value, "https://mm.corp-net.org")

    def test_legacy_file_value(self):
        (self.pack / "topology").mkdir()
        (self.pack / "topology" / "temp-bgp-config.json").write_text('{"oxidized_base_url": "http://10.4.4.4:8888"}')  # secret-scan:allow(fictional private addresses used as test input)
        self.assertEqual(resources.resolve("oxidized").value, "http://10.4.4.4:8888")  # secret-scan:allow(fictional private addresses used as test input)

    def test_sheet_url_is_reduced_to_its_id(self):
        self.write_profile({"resources": {"tracker": {"id": "https://docs.google.com/spreadsheets/d/%s/edit#gid=0" % SHEET}}})
        self.assertEqual(resources.resolve("tracker").value, SHEET)

    def test_credentials_report_presence_only(self):
        with mock.patch.dict(os.environ, {"LIBRENMS_TOKEN": "s3cret-value"}):  # secret-scan:allow(fake token value; the test proves it is never printed)
            r = resources.resolve("librenms")
        self.assertEqual(r.credentials, {"LIBRENMS_TOKEN": True})
        code, out, _ = self.run_cli("list")
        self.assertNotIn("s3cret-value", out)

    def test_unknown_name(self):
        with self.assertRaises(resources.ResourceError):
            resources.resolve("nope")

    def test_get_url_raises_the_config_error(self):
        with self.assertRaises(noc_config.ConfigError):
            resources.get_url("uisp")


class Commands(Base):
    def test_get_unset_exits_3_with_the_next_action(self):
        code, out, err = self.run_cli("get", "uisp")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("Ask the operator", err)
        self.assertIn("do not take one from a skill example", err)

    def test_get_prints_url_and_host(self):
        self.write_profile({"resources": {"uisp": {"url": "https://uisp.corp-net.org:8443/x"}}})
        self.assertEqual(self.run_cli("get", "uisp")[1].strip(), "https://uisp.corp-net.org:8443/x")
        self.assertEqual(self.run_cli("get", "uisp", "--field", "host")[1].strip(), "uisp.corp-net.org")

    def test_set_registers_keeps_other_keys_and_a_backup(self):
        self.write_profile({"org": {"name": "Acme"}})
        os.chmod(self.profile, 0o640)
        code, out, _ = self.run_cli("set", "uisp", "uisp.corp-net.org")
        self.assertEqual(code, 0, out)
        data = json.loads(self.profile.read_text())
        self.assertEqual(data["resources"]["uisp"]["url"], "https://uisp.corp-net.org")
        self.assertEqual(data["org"]["name"], "Acme")
        self.assertTrue((self.pack / "profile.json.bak").exists())
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o640)

    def test_set_creates_a_missing_profile_with_private_mode(self):
        self.assertEqual(self.run_cli("set", "oxidized", "http://10.4.4.4:8888")[0], 0)  # secret-scan:allow(fictional private addresses used as test input)
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o600)

    def test_set_refuses_placeholders_and_garbage(self):
        for bad in ("https://uisp.example.com", "<UISP_URL>", "https://203.0.113.9", "https://your-org.sonar.software",
                    "ftp://host.corp-net.org", "https://user:pw@host.corp-net.org"):  # secret-scan:allow(fixture: a URL with credentials must be refused)
            code, _, err = self.run_cli("set", "uisp", bad)
            self.assertEqual(code, 2, bad)
        self.assertFalse(self.profile.exists())

    def test_set_custom_name_needs_a_valid_name_and_stores_its_kind(self):
        self.assertEqual(self.run_cli("set", "Bad Name", "https://a.corp-net.org")[0], 2)
        self.assertEqual(self.run_cli("set", "wiki", "https://wiki.corp-net.org")[0], 0)
        self.assertEqual(resources.resolve("wiki").value, "https://wiki.corp-net.org")
        self.assertIn("wiki", self.run_cli("list")[1])

    def test_unattended_blocks_writes_but_not_reads(self):
        self.write_profile({"resources": {"uisp": {"url": "https://uisp.corp-net.org"}}})
        with mock.patch.dict(os.environ, {"NOC_UNATTENDED": "1"}):
            self.assertEqual(self.run_cli("set", "uisp", "https://evil.corp-net.org")[0], 4)
            self.assertEqual(self.run_cli("migrate", "--write")[0], 4)
            self.assertEqual(self.run_cli("get", "uisp")[0], 0)
        self.assertEqual(json.loads(self.profile.read_text())["resources"]["uisp"]["url"], "https://uisp.corp-net.org")

    def test_migrate_dry_run_writes_nothing_then_write_registers_env_and_legacy_values(self):
        self.write_profile({"org": {"uisp_host": "uisp.corp-net.org"}})
        with mock.patch.dict(os.environ, {"LIBRENMS_URL": "http://10.1.2.3"}):
            code, out, _ = self.run_cli("migrate")
            self.assertEqual(code, 0)
            self.assertNotIn("resources", json.loads(self.profile.read_text()))
            self.assertEqual(self.run_cli("migrate", "--write")[0], 0)
        res = json.loads(self.profile.read_text())["resources"]
        self.assertEqual(res["uisp"]["url"], "https://uisp.corp-net.org")
        self.assertEqual(res["librenms"]["url"], "http://10.1.2.3")

    def test_device_table_rows_are_only_suggestions(self):
        (self.pack / "devices.md").write_text("| Device | Role | Management IP |\n|---|---|---|\n| uisp | controller | 10.9.9.9 |\n")  # secret-scan:allow(fictional private addresses used as test input)
        code, out, _ = self.run_cli("migrate", "--write")
        self.assertEqual(code, 0)
        self.assertIn("suggestion only", out)
        self.assertFalse(self.profile.exists())

    def test_check_flags_conflicts_and_placeholders(self):
        self.write_profile({"resources": {"uisp": {"url": "https://uisp.example.org"}, "librenms": {"url": "https://a.corp-net.org"}}})
        with mock.patch.dict(os.environ, {"LIBRENMS_URL": "https://b.corp-net.org"}):
            code, out, _ = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("[FAIL]"), 2)

    def test_check_stops_asking_to_migrate_once_the_profile_holds_the_value(self):
        with mock.patch.dict(os.environ, {"LIBRENMS_URL": "http://10.1.2.3"}):  # secret-scan:allow(fictional private address used as test input)
            self.assertIn("not in the profile yet", self.run_cli("check")[1])
            self.run_cli("migrate", "--write")
            self.assertNotIn("not in the profile yet", self.run_cli("check")[1])

    def test_check_passes_when_everything_registered_is_sound(self):
        self.write_profile({"resources": {"uisp": {"url": "https://uisp.corp-net.org"}}})
        self.assertEqual(self.run_cli("check")[0], 0)

    def test_list_json(self):
        self.write_profile({"resources": {"uisp": {"url": "https://uisp.corp-net.org"}}})
        rows = {r["name"]: r for r in json.loads(self.run_cli("list", "--json")[1])}
        self.assertEqual(rows["uisp"]["value"], "https://uisp.corp-net.org")
        self.assertIsNone(rows["mymate"]["value"])


if __name__ == "__main__":
    unittest.main()
