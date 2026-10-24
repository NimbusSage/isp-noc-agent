#!/usr/bin/env python3
"""Tests for scripts/envfile.py and scripts/noc_config.py (no shell evaluation, strict ids)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import envfile  # noqa: E402
import noc_config  # noqa: E402


class TestParser(unittest.TestCase):
    def test_basic_forms(self):
        d = envfile.parse('# c\nA=1\nexport B="two words" # trailing\nC=\'single $(x)\'\nD=plain # comment\n'
                          'E="a#b"\nF=\nK = spaced\nL=tok#en\n\n')
        self.assertEqual(d, {"A": "1", "B": "two words", "C": "single $(x)", "D": "plain", "E": "a#b",
                             "F": "", "K": "spaced", "L": "tok#en"})

    def test_double_quote_escapes_and_multiline(self):
        d = envfile.parse('G="multi\\nline \\"q\\" \\\\ \\$HOME"\nH="open\nline2"\nI=after\n')
        self.assertEqual(d["G"], 'multi\nline "q" \\ $HOME')
        self.assertEqual(d["H"], "open\nline2")
        self.assertEqual(d["I"], "after")

    def test_crlf_bom_and_export_variants(self):
        d = envfile.parse('\ufeffA=1\r\nexport   B=2\r\nexportC=3\r\nexport\tD=4\r\n')
        self.assertEqual(d, {"A": "1", "B": "2", "D": "4", "exportC": "3"})  # `exportC` is just a key name

    def test_bad_lines_are_skipped_not_fatal(self):
        d = envfile.parse('bad key=1\n=novalue\n9X=1\nOK=1\n$(touch x)=1\nJUST_A_WORD\n')
        self.assertEqual(d, {"OK": "1"})

    def test_unterminated_quote_does_not_swallow_the_file(self):
        d = envfile.parse("I='no close\nJ=after\n")
        self.assertEqual(d["J"], "after")
        self.assertEqual(d["I"], "no close")

    def test_last_assignment_wins(self):
        self.assertEqual(envfile.parse("A=1\nA=2\n")["A"], "2")

    def test_no_shell_evaluation_ever(self):
        d = Path(tempfile.mkdtemp(prefix="envfile-"))
        self.addCleanup(shutil.rmtree, d, True)
        marker = d / "pwned"
        f = d / ".env"
        f.write_text(f"SONAR_TOKEN=abc\nX=$(touch {marker})\nY=`touch {marker}`\nZ=a;touch {marker}\n"
                     f"W=\"$(touch {marker})\"\n")
        data = envfile.load(f)
        self.assertFalse(marker.exists())
        self.assertEqual(data["SONAR_TOKEN"], "abc")
        self.assertEqual(data["X"], f"$(touch {marker})")      # kept as inert text
        self.assertEqual(data["Z"], f"a;touch {marker}")


class TestLoadAndGet(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="envfile-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.old = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.old)

    def test_missing_file_is_empty(self):
        self.assertEqual(envfile.load(self.d / "nope.env"), {})
        os.environ["NOC_DOTENV"] = str(self.d / "nope.env")
        self.assertEqual(envfile.get("ANYTHING", "dflt"), "dflt")

    def test_unreadable_path_is_empty(self):
        self.assertEqual(envfile.load(self.d), {})            # a directory, not a file

    def test_get_prefers_real_environment(self):
        f = self.d / "e.env"
        f.write_text("TOK_X=from_file\nONLY_FILE=yes\n")
        os.environ["NOC_DOTENV"] = str(f)
        os.environ["TOK_X"] = "from_env"
        self.assertEqual(envfile.get("TOK_X"), "from_env")
        self.assertEqual(envfile.get("ONLY_FILE"), "yes")
        os.environ["TOK_X"] = "   "                            # blank env does not shadow the file
        self.assertEqual(envfile.get("TOK_X"), "from_file")

    def test_cache_notices_edits(self):
        f = self.d / "e.env"
        f.write_text("A=1\n")
        self.assertEqual(envfile.load(f)["A"], "1")
        f.write_text("A=22\n")
        self.assertEqual(envfile.load(f)["A"], "22")


class TestNocConfig(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="nocconfig-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.old = dict(os.environ)
        self.addCleanup(self._restore)
        os.environ["NOC_DOTENV"] = str(self.d / "absent.env")
        os.environ["NOC_PROFILE_FILE"] = str(self.d / "profile.json")
        for k in ("NOC_STATE_DIR", "NOC_AGENT_LOG_DIR", "CS_LEVEL2_GROUP_ID", "SONAR_BASE_URL"):
            os.environ.pop(k, None)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.old)

    def test_resolve_id_env_then_profile_then_none(self):
        prof = {"sonar": {"groups": {"cs_level_2": {"id": 7}}}}
        keys = ["sonar", "groups", "cs_level_2"]
        self.assertEqual(noc_config.resolve_id(["CS_LEVEL2_GROUP_ID"], keys, prof), 7)
        os.environ["CS_LEVEL2_GROUP_ID"] = "9"
        self.assertEqual(noc_config.resolve_id(["CS_LEVEL2_GROUP_ID"], keys, prof), 9)
        self.assertIsNone(noc_config.resolve_id(["NOPE"], ["sonar", "groups", "missing"], {}))

    def test_resolve_id_rejects_malformed_values(self):
        os.environ["CS_LEVEL2_GROUP_ID"] = "4x"
        with self.assertRaises(noc_config.ConfigError):
            noc_config.resolve_id(["CS_LEVEL2_GROUP_ID"], ["a"], {})
        del os.environ["CS_LEVEL2_GROUP_ID"]
        with self.assertRaises(noc_config.ConfigError):
            noc_config.resolve_id([], ["a", "b"], {"a": {"b": {"id": "two"}}})
        with self.assertRaises(noc_config.ConfigError):
            noc_config.resolve_id([], ["a", "b"], {"a": {"b": {"id": 0}}})

    def test_profile_loader_is_tolerant(self):
        self.assertEqual(noc_config.load_profile(), {})
        (self.d / "profile.json").write_text("not json")
        self.assertEqual(noc_config.load_profile(), {})
        (self.d / "profile.json").write_text("[1]")
        self.assertEqual(noc_config.load_profile(), {})
        (self.d / "profile.json").write_text(json.dumps({"org": {"sonar_base_url": "https://x.example/"}}))
        self.assertEqual(noc_config.sonar_base_url(), "https://x.example")

    def test_state_dir_precedence(self):
        self.assertEqual(noc_config.state_dir(), noc_config.ROOT / "logs")
        os.environ["NOC_AGENT_LOG_DIR"] = "/a"
        self.assertEqual(noc_config.state_dir(), Path("/a"))
        os.environ["NOC_STATE_DIR"] = "/b"
        self.assertEqual(noc_config.state_dir(), Path("/b"))


if __name__ == "__main__":
    unittest.main()
