"""Tests for scripts/opencode_usage.py against a small fixture database (never the real OpenCode store)."""
import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opencode_usage  # noqa: E402

NOW = int(time.time() * 1000)


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("create table session_v2 (id text primary key, directory text, time_created integer)")
    conn.execute("create table session_message (id text primary key, session_id text, type text, seq integer, "
                 "time_created integer, data text)")
    seq = 0

    def session(sid, directory, first_text, contexts, model=("opencode", "big-pickle"), age_ms=1000):
        nonlocal seq
        t = NOW - age_ms
        conn.execute("insert into session_v2 values (?, ?, ?)", (sid, directory, t))
        seq += 1
        conn.execute("insert into session_message values (?, ?, 'user', ?, ?, ?)",
                     (f"m{seq}", sid, seq, t, json.dumps({"text": first_text})))
        for ctx in contexts:
            seq += 1
            data = {"model": {"providerID": model[0], "id": model[1]},
                    "tokens": {"input": 100, "output": 5, "cache": {"read": ctx - 100, "write": 0}}}
            conn.execute("insert into session_message values (?, ?, 'assistant', ?, ?, ?)",
                         (f"m{seq}", sid, seq, t, json.dumps(data)))

    session("b1", "/repo", "# Batch Ticket Processing\n\nTickets: secret-site-name", [20000, 40000, 60000])
    session("b2", "/repo", "# Batch Ticket Processing\n...", [30000])                       # refused on request 1
    session("p1", "/repo", "Peak-window retest for ticket 9:\n...", [25000, 35000])
    session("o1", "/elsewhere", "help me with a draft", [1000, 2000], model=("opencode-go", "glm-5.3"))
    session("old", "/repo", "# Batch Ticket Processing", [99999, 99999], age_ms=40 * 86400 * 1000)
    conn.commit()
    conn.close()


class OpencodeUsage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "opencode.db"
        make_db(self.db)

    def run_json(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(opencode_usage.main(["--db", str(self.db), "--json", *args]), 0)
        return json.loads(buf.getvalue())

    def test_kinds_requests_and_context(self):
        s = self.run_json("--days", "7")
        self.assertEqual(s["batch"]["sessions"], 2)
        self.assertEqual(s["batch"]["failed_fast"], 1)
        self.assertEqual(s["batch"]["requests"], 4)
        self.assertEqual(s["batch"]["context_tokens"], 150000)
        self.assertEqual(s["batch"]["requests_per_run_max"], 3)
        self.assertEqual(s["batch"]["peak_context_max"], 60000)
        self.assertEqual(s["peak"]["sessions"], 1)
        self.assertEqual(s["other"]["requests"], 2)
        self.assertEqual(s["models"], {"opencode/big-pickle": 6, "opencode-go/glm-5.3": 2})

    def test_window_and_directory_filters(self):
        self.assertEqual(self.run_json("--days", "60")["batch"]["sessions"], 3)
        s = self.run_json("--dir", "/elsewhere")
        self.assertEqual((s["batch"]["sessions"], s["other"]["sessions"]), (0, 1))

    def test_report_has_counts_only_and_never_writes(self):
        before = self.db.read_bytes()
        buf = io.StringIO()
        with redirect_stdout(buf):
            opencode_usage.main(["--db", str(self.db)])
        self.assertNotIn("secret-site-name", buf.getvalue())
        self.assertIn("batch", buf.getvalue())
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_db_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as cm:
            opencode_usage.main(["--db", str(Path(self._tmp.name) / "none.db")])
        self.assertIn("no OpenCode database", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
