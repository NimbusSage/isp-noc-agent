#!/usr/bin/env python3
"""opencode_usage.py - how many model requests and tokens OpenCode runs used, from OpenCode's own session store.

Reads OpenCode 2.x's SQLite database READ-ONLY (it never writes or locks it for writing) and reports, per kind of
run, how many sessions ran, how many model requests they made and how much context those requests carried. Use it
to see what a ticket costs before and after a change, and whether quota-refused runs are piling up.

Kinds, from each session's first user message:
  batch  - the server's ticket batches ("# Batch Ticket Processing")
  peak   - the server's peak-window retests ("Peak-window retest for ticket")
  other  - everything else (interactive sessions, scripts)

Every model request is one assistant message; its context is input plus cache-read tokens. A session with at most
one request is counted as "failed fast" (typically a provider refusal on the first request).

The report holds counts only: no titles, prompts or ticket text.

Usage:
  python3 scripts/opencode_usage.py [--days 7] [--db PATH] [--json] [--dir /path/to/checkout]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "opencode.db"
BATCH_MARK = "# Batch Ticket Processing"
PEAK_MARK = "Peak-window retest for ticket"


def connect_ro(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise SystemExit(f"opencode_usage: no OpenCode database at {db}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def kind_of(first_user_text: str) -> str:
    if BATCH_MARK in first_user_text:
        return "batch"
    if PEAK_MARK in first_user_text:
        return "peak"
    return "other"


def _context(tokens: dict) -> int:
    if not isinstance(tokens, dict):
        return 0
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    return int(tokens.get("input") or 0) + int(cache.get("read") or 0)


def _pct(values, p):
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def collect(conn: sqlite3.Connection, since_ms: int, directory: str = "") -> dict:
    sql = "select id, directory from session_v2 where time_created >= ?"
    args = [since_ms]
    if directory:
        sql += " and directory = ?"
        args.append(directory)
    sessions = {sid: {"kind": "other", "requests": 0, "context_total": 0, "context_peak": 0}
                for sid, _ in conn.execute(sql, args)}
    if not sessions:
        return {"sessions": {}, "models": {}}
    seen_first = set()
    models = Counter()
    rows = conn.execute(
        "select session_id, type, data from session_message where time_created >= ? and type in ('user', 'assistant') "
        "order by session_id, seq", (since_ms,))
    for sid, typ, data in rows:
        rec = sessions.get(sid)
        if rec is None:
            continue
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if typ == "user":
            if sid not in seen_first:
                seen_first.add(sid)
                rec["kind"] = kind_of(str(msg.get("text") or ""))
            continue
        rec["requests"] += 1
        ctx = _context(msg.get("tokens"))
        rec["context_total"] += ctx
        rec["context_peak"] = max(rec["context_peak"], ctx)
        model = msg.get("model") if isinstance(msg.get("model"), dict) else {}
        models[f"{model.get('providerID') or '?'}/{model.get('id') or '?'}"] += 1
    return {"sessions": sessions, "models": dict(models)}


def summarize(data: dict) -> dict:
    out = {}
    for kind in ("batch", "peak", "other"):
        recs = [r for r in data["sessions"].values() if r["kind"] == kind]
        ran = [r for r in recs if r["requests"] > 1]
        out[kind] = {
            "sessions": len(recs),
            "failed_fast": len(recs) - len(ran),
            "requests": sum(r["requests"] for r in recs),
            "context_tokens": sum(r["context_total"] for r in recs),
            "requests_per_run_p50": _pct([r["requests"] for r in ran], 50),
            "requests_per_run_p95": _pct([r["requests"] for r in ran], 95),
            "requests_per_run_max": max([r["requests"] for r in ran], default=0),
            "peak_context_p50": _pct([r["context_peak"] for r in ran], 50),
            "peak_context_max": max([r["context_peak"] for r in ran], default=0),
        }
    out["models"] = dict(sorted(data["models"].items(), key=lambda kv: -kv[1]))
    return out


def render(summary: dict, days: float) -> str:
    lines = [f"OpenCode usage, last {days:g} day(s) (requests = model calls; context = input + cache-read tokens)"]
    for kind in ("batch", "peak", "other"):
        s = summary[kind]
        lines.append(
            f"  {kind:<5} sessions {s['sessions']:>4} (failed fast {s['failed_fast']:>3})  requests {s['requests']:>6}  "
            f"context {s['context_tokens'] / 1e6:>8.1f}M  per run p50/p95/max {s['requests_per_run_p50']}/"
            f"{s['requests_per_run_p95']}/{s['requests_per_run_max']}  peak context p50/max "
            f"{s['peak_context_p50'] / 1000:.0f}k/{s['peak_context_max'] / 1000:.0f}k")
    lines.append("  models: " + (", ".join(f"{m} {n}" for m, n in summary["models"].items()) or "none"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=float, default=7.0, help="look-back window in days (default 7)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"OpenCode database (default {DEFAULT_DB})")
    ap.add_argument("--dir", default="", help="only sessions started in this directory (e.g. the checkout)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)
    if args.days <= 0:
        ap.error("--days must be positive")
    since_ms = int((time.time() - args.days * 86400) * 1000)
    conn = connect_ro(args.db)
    try:
        summary = summarize(collect(conn, since_ms, args.dir))
    finally:
        conn.close()
    print(json.dumps(summary, indent=2) if args.json else render(summary, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
