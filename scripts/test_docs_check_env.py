"""Tests for the env-var gate in scripts/docs_check.py (synthetic trees; plus the real repo)."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs_check  # noqa: E402


class Tree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def put(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def names(self):
        return set(docs_check.scan_env_reads(self.root))


class ScanTests(Tree):
    def test_js_forms(self):
        self.put("server/index.js", "const a = process.env.ALPHA_ONE; const b = env.BETA_TWO;\n"
                 "const c = process.env['GAMMA_THREE']; const d = intVar('DELTA_FOUR', 3, 1, 9);\n"
                 "const e2 = e.EPSILON_FIVE; const s = 'not_env'; const x = 'lowerCase_Mixed';\n")
        self.assertEqual(self.names(), {"ALPHA_ONE", "BETA_TWO", "GAMMA_THREE", "DELTA_FOUR", "EPSILON_FIVE"})

    def test_python_forms(self):
        self.put("scripts/a.py", 'import os\nx = os.environ.get("PY_ONE")\ny = os.environ["PY_TWO"]\n'
                 'z = os.getenv("PY_THREE", "d")\nw = envfile.get("PY_FOUR")\nv = pick("PY_FIVE", a)\n'
                 'h = os.environ.get("HOME")\nq = "lowercase_name"\nr = "NOUNDERSCORE"\n')
        self.assertEqual(self.names(), {"PY_ONE", "PY_TWO", "PY_THREE", "PY_FOUR", "PY_FIVE", "HOME"})

    def test_shell_only_counts_default_forms(self):
        self.put("scripts/x.sh", 'a="${SH_ONE:-8}"\nb="$LOCAL_VAR"\nc="${LOCAL_TWO}"\nd="${SH_THREE:?need}"\n')
        self.assertEqual(self.names(), {"SH_ONE", "SH_THREE"})

    def test_template_and_opencode_json(self):
        self.put("deploy/u.service.template", "Environment=NODE_ENV=production\nExecStart={{INSTALL_DIR}}/x ${UNIT_VAR}\n")
        self.put("opencode.json", '{"env": {"A": "{env:OC_TOKEN}"}}')
        self.assertEqual(self.names(), {"NODE_ENV", "UNIT_VAR", "OC_TOKEN"})

    def test_skips_tests_vendored_and_non_code(self):
        self.put("server/node_modules/pkg/i.js", "process.env.VENDORED_VAR")
        self.put("server/index.test.mjs", "process.env.TEST_ONLY_VAR")
        self.put("scripts/test_x.py", 'os.environ.get("TEST_PY_VAR")')
        self.put("mcp-server/src/dist/b.ts", "process.env.DIST_VAR")
        self.put("mcp-server/src/a.ts", "process.env.REAL_TS_VAR")
        self.put("scripts/notes.txt", "process.env.TXT_VAR")
        self.assertEqual(self.names(), {"REAL_TS_VAR"})

    def test_ignored_file_is_skipped(self):
        self.put("scripts/install.sh", 'a="${WITH_GUI:-0}"')
        self.put("scripts/b.sh", 'a="${KEPT_VAR:-0}"')
        got = docs_check.scan_env_reads(self.root, {"scripts/install.sh"})
        self.assertEqual(set(got), {"KEPT_VAR"})
        self.assertEqual(got["KEPT_VAR"], ["scripts/b.sh"])


class DocumentedTests(Tree):
    def test_documented_forms(self):
        p = self.put(".env.example", '# prose line mentioning FOO_PROSE and more\n# FOO_PROSE - not a var line\n'
                     'ACTIVE_ONE=""\n# COMMENTED_TWO="x"\nexport EXPORTED_THREE="y"\n  # INDENTED_FOUR=1\nlower=1\n')
        self.assertEqual(docs_check.documented_env_names(p),
                         {"ACTIVE_ONE", "COMMENTED_TWO", "EXPORTED_THREE", "INDENTED_FOUR"})


class CheckEnvTests(Tree):
    def test_undocumented_fails_documented_passes(self):
        self.put("scripts/a.py", 'os.environ.get("DOC_ME")\nos.environ.get("HAVE_IT")\n')
        self.put(".env.example", '# HAVE_IT="x"\n')
        undoc, unread, errs = docs_check.check_env(self.root)
        self.assertEqual(set(undoc), {"DOC_ME"})
        self.assertEqual(undoc["DOC_ME"], ["scripts/a.py"])
        self.assertEqual((unread, errs), ([], []))

    def test_documented_but_unread_is_a_warning_list(self):
        self.put("scripts/a.py", 'os.environ.get("USED_ONE")\n')
        self.put(".env.example", 'USED_ONE=""\n# STALE_TWO="x"\n')
        undoc, unread, _ = docs_check.check_env(self.root)
        self.assertEqual((undoc, unread), ({}, ["STALE_TWO"]))

    def test_ignore_file_globs_files_and_unread_suppression(self):
        self.put("scripts/a.py", 'os.environ.get("NOC_FOO_USER")\nos.environ.get("API_ERROR_X")\n')
        self.put("scripts/install.sh", 'a="${WITH_GUI:-0}"')
        self.put(".env.example", '# AGENT_X_ARGS=""\n')
        self.put("scripts/env_ignore.txt", "# c\nNOC_*_USER  # built at run time\nAPI_ERROR_X  # status string\n"
                 "AGENT_*_ARGS  # dynamic\nfile:scripts/install.sh  # installer flags\n")
        undoc, unread, errs = docs_check.check_env(self.root)
        self.assertEqual((undoc, unread, errs), ({}, [], []))

    def test_ignore_entry_without_reason_is_an_error(self):
        self.put("scripts/env_ignore.txt", "GOOD_ONE  # fine\nBAD_ONE\nALSO_BAD  #   \n")
        _, _, errs = docs_check.check_env(self.root)
        self.assertEqual(len(errs), 2)
        self.assertIn(":2:", errs[0])
        self.assertIn(":3:", errs[1])

    def test_is_ignored_glob(self):
        self.assertTrue(docs_check.is_ignored("NOC_X_PASSWORD", ["NOC_*_PASSWORD"]))
        self.assertFalse(docs_check.is_ignored("NOC_X_PASSWORDS", ["NOC_*_PASSWORD"]))


class RealRepoTests(unittest.TestCase):
    def test_every_variable_the_code_reads_is_documented(self):
        undoc, _unread, errs = docs_check.check_env(docs_check.ROOT)
        self.assertEqual(errs, [])
        self.assertEqual(undoc, {}, "undocumented env vars: " + ", ".join(sorted(undoc)))

    def test_known_variables_are_found_and_documented(self):
        reads = docs_check.scan_env_reads(docs_check.ROOT)
        documented = docs_check.documented_env_names(docs_check.ENV_EXAMPLE)
        for name in ("SONAR_TOKEN", "TICKET_WEBHOOK_AUTH_TOKEN", "IHN_BOUNCE_CATEGORY_ID", "BOUNCE_AUTONOMOUS_ENABLED",
                     "NOC_SSH_HOSTKEY_MODE", "WEBHOOK_BIND", "NOC_WRITE_ENABLED", "TEMP_SITE_WRITE_ENABLED"):
            self.assertIn(name, reads, name)
            self.assertIn(name, documented, name)
        self.assertNotIn("BOUNCE_CATEGORY_ID", documented)


class MainTests(unittest.TestCase):
    def run_main(self, result):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(docs_check, "check_env", return_value=result):
            rc = docs_check.main()
        return rc, out.getvalue()

    def test_undocumented_makes_exit_nonzero_and_lists_it(self):
        rc, out = self.run_main(({"MISSING_VAR": ["scripts/a.py"]}, [], []))
        self.assertEqual(rc, 1)
        self.assertIn("MISSING_VAR", out)
        self.assertIn("scripts/a.py", out)

    def test_unread_only_is_a_warning_exit_zero(self):
        rc, out = self.run_main(({}, ["STALE_VAR"], []))
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", out)
        self.assertIn("STALE_VAR", out)

    def test_ignore_list_errors_fail(self):
        rc, _ = self.run_main(({}, [], ["scripts/env_ignore.txt:3: entry needs `NAME  # reason`"]))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
