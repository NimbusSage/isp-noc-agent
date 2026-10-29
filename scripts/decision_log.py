#!/usr/bin/env python3
"""
decision_log.py — Structured JSONL decision log for every agent decision.

Guide criterion: "Log prompts, context, outputs, and actions" — every ticket outcome
gets one JSON line in logs/decisions/YYYY-MM.jsonl (monthly shard). This is the audit
trail AND the source for metrics_rollup.py and the weekly report.

Writers:
    server/index.js recordOutcome()  — one `batch_result` line per settled ticket
    scripts/sonar_bounce.py          — one `bounce` line per attempted/executed bounce
    evals/eval_run.py                — one `eval` line per eval run summary

Line schema (flat, extra keys allowed — schema is append-friendly):
    ts                    ISO8601 UTC
    kind                  batch_result | bounce | breaker | eval
    ticket_id             string
    status                agent status code (batch_result/bounce lines)
    confidence            0-100 when scored
    tower_site, analyzer_site, bottleneck_analysis, notification_sent
    classification        {category, complexity, priority} when known
    requires_human_review bool
    evidence              [strings] — markers, correlated ticket ids, hop evidence
    escalation_reason     null | string
    operator              who ran it: the NOC_OPERATOR handle (see scripts/team.py), "batch" for the server;
                          absent on a single-person install that has no handle
    ...any extra keys from --json payload

CLI:
    decision_log.py append --json '<json-payload>' [--kind K] [--ticket ID]
    decision_log.py query --since 2026-09-01 [--kind batch_result] [--ticket 123]
                           [--format json|count]

Concurrency / durability:
    Many processes append at once (the server spawns one `append` per settled ticket).
    Each record is written with ONE os.write on an O_APPEND descriptor while holding an
    exclusive flock on `<shard>.lock`, then fsync'd, so lines never interleave or tear.

Env:
    NOC_STATE_DIR       state root; the log lives in <root>/decisions
                        (default: NOC_AGENT_LOG_DIR, else <repo>/logs)
    NOC_AGENT_LOG_DIR   legacy spelling of NOC_STATE_DIR (the webhook server sets it)
    DECISION_LOG_DIR    override the decisions dir directly
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noc_config  # noqa: E402
import statefile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG_ROOT = noc_config.state_dir()
DECISIONS_DIR = Path(os.environ.get("DECISION_LOG_DIR", "").strip() or LOG_ROOT / "decisions")


def shard_path(ts: float) -> Path:
    month = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
    return DECISIONS_DIR / f"{month}.jsonl"


def _operator() -> str:
    """The current operator's handle, or "". Never raises: a decision must be logged either way."""
    try:
        import team
        return team.configured_handle()[0]
    except Exception:
        return ""


def append_decision(payload: dict, kind: str = None, ticket_id: str = None) -> dict:
    ts = time.time()
    record = {"ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")}
    if kind:
        record["kind"] = kind
    if ticket_id:
        record["ticket_id"] = str(ticket_id)
    operator = _operator()
    if operator:
        record["operator"] = operator
    # Payload keys never overwrite the envelope keys set above
    for k, v in payload.items():
        if k not in record:
            record[k] = v
    path = shard_path(ts)
    line = json.dumps(record, default=str)
    # O_APPEND + flock + single write + fsync (see statefile.append_line)
    statefile.append_line(path, line)
    return record


def query(since: str, until: str = None, kind: str = None, ticket_id: str = None) -> list:
    since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) if since else None
    until_dt = datetime.fromisoformat(until).replace(tzinfo=timezone.utc) if until else None
    out = []
    if not DECISIONS_DIR.exists():
        return out
    for path in sorted(DECISIONS_DIR.glob("*.jsonl")):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate a torn write
                    if kind and rec.get("kind") != kind:
                        continue
                    if ticket_id and str(rec.get("ticket_id")) != str(ticket_id):
                        continue
                    if since_dt or until_dt:
                        try:
                            rec_dt = datetime.fromisoformat(rec["ts"])
                        except (KeyError, ValueError):
                            continue
                        if since_dt and rec_dt < since_dt:
                            continue
                        if until_dt and rec_dt > until_dt:
                            continue
                    out.append(rec)
        except OSError:
            continue
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Structured JSONL decision log")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append", help="append one decision record")
    a.add_argument("--json", required=True, help="JSON payload (extra keys allowed)")
    a.add_argument("--kind", default=None)
    a.add_argument("--ticket", default=None, dest="ticket_id")

    q = sub.add_parser("query", help="query records")
    q.add_argument("--since", default=None)
    q.add_argument("--until", default=None)
    q.add_argument("--kind", default=None)
    q.add_argument("--ticket", default=None, dest="ticket_id")
    q.add_argument("--format", choices=["json", "count"], default="json")

    args = p.parse_args()

    if args.cmd == "append":
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"ERROR: --json is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(payload, dict):
            print("ERROR: --json must be a JSON object", file=sys.stderr)
            sys.exit(1)
        rec = append_decision(payload, kind=args.kind, ticket_id=args.ticket_id)
        print(json.dumps({"appended": str(shard_path(time.time())), "record": rec}))
    elif args.cmd == "query":
        recs = query(args.since, args.until, args.kind, args.ticket_id)
        if args.format == "count":
            print(len(recs))
        else:
            print(json.dumps(recs, indent=2))


if __name__ == "__main__":
    main()
