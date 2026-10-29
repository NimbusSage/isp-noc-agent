"""Tests for the machine-local overlay in scripts/noc_config.py (temporary files only)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noc_config  # noqa: E402


class Overlay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.base = self.root / "profile.json"

    def test_names(self):
        self.assertEqual(noc_config.overlay_path(Path("/x/profile.json")), Path("/x/profile.local.json"))
        self.assertEqual(noc_config.overlay_path(Path("topology/temp-bgp-config.json")), Path("topology/temp-bgp-config.local.json"))

    def test_objects_merge_and_lists_are_replaced_whole(self):
        merged = noc_config.merge_overlay(
            {"a": {"x": 1, "y": 2}, "l": [1, 2, 3], "keep": True},
            {"a": {"y": 9, "z": 3}, "l": [7]})
        self.assertEqual(merged, {"a": {"x": 1, "y": 9, "z": 3}, "l": [7], "keep": True})

    def test_overlay_is_optional_and_the_base_is_required(self):
        self.base.write_text(json.dumps({"a": 1}))
        self.assertEqual(noc_config.load_json_overlay(self.base), {"a": 1})
        with self.assertRaises(FileNotFoundError):
            noc_config.load_json_overlay(self.root / "absent.json")

    def test_invalid_json_names_the_file(self):
        self.base.write_text(json.dumps({"a": 1}))
        (self.root / "profile.local.json").write_text("{nope")
        with self.assertRaises(ValueError) as cm:
            noc_config.load_json_overlay(self.base)
        self.assertIn("profile.local.json", str(cm.exception))

    def test_load_profile_merges_the_overlay_and_ignores_a_broken_one(self):
        self.base.write_text(json.dumps({"org": {"name": "N"}, "conventions": {"snmp": {"provider": "P"}}}))
        local = self.root / "profile.local.json"
        local.write_text(json.dumps({"conventions": {"snmp": {"community": "c-value"}}}))  # secret-scan:allow(fictional test value)
        with mock.patch.dict(os.environ, {"NOC_PROFILE_FILE": str(self.base)}):
            profile = noc_config.load_profile()
            self.assertEqual(profile["conventions"]["snmp"], {"provider": "P", "community": "c-value"})  # secret-scan:allow(fictional test value)
            local.write_text("{nope")
            self.assertEqual(noc_config.load_profile(), {})              # same rule as an unreadable profile


if __name__ == "__main__":
    unittest.main()
