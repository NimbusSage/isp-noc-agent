"""temp_bgp_peering reads the router passwords from the machine-local overlay (temporary files only)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import temp_bgp_peering as tbp  # noqa: E402


class OverlayLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cfg = self.dir / "temp-bgp-config.json"
        self.cfg.write_text(json.dumps({"default_rr": "192.0.2.1", "rr_credentials": [{"ip": "192.0.2.1"}]}))

    def test_password_comes_from_the_overlay(self):
        (self.dir / "temp-bgp-config.local.json").write_text(
            json.dumps({"rr_credentials": [{"ip": "192.0.2.1", "password": "overlay-pass-test"}]}))
        merged = tbp._load_json_or_die(self.cfg, "temp-BGP inputs", overlay=True)
        self.assertEqual(merged["rr_credentials"][0]["password"], "overlay-pass-test")
        self.assertEqual(merged["default_rr"], "192.0.2.1")

    def test_without_the_flag_the_overlay_is_not_read(self):
        (self.dir / "temp-bgp-config.local.json").write_text(json.dumps({"rr_credentials": [{"password": "x" * 8}]}))
        self.assertNotIn("password", tbp._load_json_or_die(self.cfg, "temp-BGP inputs")["rr_credentials"][0])

    def test_a_broken_overlay_stops_the_run_and_names_the_file(self):
        (self.dir / "temp-bgp-config.local.json").write_text("{nope")
        with self.assertRaises(SystemExit) as cm:
            tbp._load_json_or_die(self.cfg, "temp-BGP inputs", overlay=True)
        self.assertIn("temp-bgp-config.local.json", str(cm.exception))

    def test_a_missing_shared_file_still_explains_how_to_create_it(self):
        with self.assertRaises(SystemExit) as cm:
            tbp._load_json_or_die(self.dir / "absent.json", "temp-BGP inputs", overlay=True)
        self.assertIn("temp-bgp-config.example.json", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
