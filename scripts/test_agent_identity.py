#!/usr/bin/env python3
"""Tests for agent_identity.py (run: python3 scripts/test_agent_identity.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_identity  # noqa: E402


class Name(unittest.TestCase):
    def test_default_when_unset(self):
        self.assertEqual(agent_identity.name({}), "NOC Agent")

    def test_the_install_sets_its_own_name(self):
        self.assertEqual(agent_identity.name({"AGENT_NAME": "Ethan's Agent"}), "Ethan's Agent")

    def test_empty_falls_back_instead_of_failing(self):
        self.assertEqual(agent_identity.name({"AGENT_NAME": "   "}), "NOC Agent")

    def test_markup_and_control_characters_are_removed(self):
        self.assertEqual(agent_identity.sanitize("Hub <b>Agent</b>\t team"), "Hub b Agent /b team")

    def test_whitespace_collapses(self):
        self.assertEqual(agent_identity.sanitize("  NOC    Agent  "), "NOC Agent")

    def test_long_name_is_capped(self):
        self.assertEqual(len(agent_identity.sanitize("a" * 200)), 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
