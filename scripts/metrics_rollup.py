#!/usr/bin/env python3
"""
metrics_rollup.py — Weekly NOC agent metrics from the decision log.

Guide criteria covered: "Monitor the right metrics" + "Every production incident should
become a permanent test case". Reads logs/decisions/<YYYY-MM>.jsonl (written by the server
hook after every settled ticket), computes:

    - total processed            tickets settled in window
    - straight-through rate      handled without human touch
    - needs_human rate           escalated (incl. peak-queued)
    - bounce rate                IHN + correlated + peak bounces
    - bounce revert count        bounced tickets whose Sonar group is NOC again
      -> each revert is recorded into the circuit breaker as bounce_reverted
    - breaker status             current open/closed + window failure rate
    - incident → testcase        any ticket_id passed via --incident becomes a pending
                                 golden-set stub in site-pack/evals/pending/ (the org's own data,
                                 never the tracked tree)

Usage:
    metrics_rollup.py [--since 2026-09-21] [--until 2026-09-28] [--no-chat]
                      [--incident 12345] [--incident 12346 ...]

Env:
    SONAR_TOKEN (for revert re-check; skips revert check if absent)
    NOC_GROUP_ID                NOC group id; bounced tickets found back in it are "reverted"
                                (else profile sonar.groups.noc.id; unresolved = revert check skipped)
    CS_LEVEL2_GROUP_ID / CS_GROUP_ID   CS Level 2 group id (else profile sonar.groups.cs_level_2.id)
    NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL (report delivery; skipped with --no-chat)
    NOC_STATE_DIR               state root; the report lands in <root>/metrics (default logs/)

No org ids are hard-coded: group ids come from env/.env or site-pack/profile.json.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_log  # noqa: E402
import envfile  # noqa: E402
import gql_client  # noqa: E402
import noc_config  # noqa: E402
import noc_config  # noqa: E402
import statefile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _group_id(env_names, profile_key):
    """Configured Sonar group id or None (no built-in default)."""
    try:
        return noc_config.resolve_id(env_names, ["sonar", "groups", profile_key])
    except noc_config.ConfigError as e:
        print(f"WARNING: {e}", file=sys.stderr)
        return None


CS_GROUP_ID = _group_id(["CS_LEVEL2_GROUP_ID", "CS_GROUP_ID"], "cs_level_2")
NOC_GROUP_ID = _group_id(["NOC_GROUP_ID"], "noc")

HANDLED_STATUSES = {
    "handled_duplicate", "categorized", "passthrough", "auto_closed",
    "auto_closed_ooo", "auto_closed_maintenance",
    "auto_bounced_ihn", "auto_bounced_ihn_peak", "correlated_bounced",
}
ESCALATED_STATUSES = {"needs_human", "queued_for_peak", "pbs_failure"}
BOUNCE_STATUSES = {"auto_bounced_ihn", "auto_bounced_ihn_peak", "correlated_bounced"}
ANALYSIS_FAILURE_STATUSES = {"needs_human"}  # with bottleneck_analysis == failed


GROUP_QUERY = ("query TicketGroup($id: %s!) { tickets(search: { integer_fields: "
               "[{ attribute: \"id\", operator: EQ, search_value: $id }] }) "
               "{ entities { id ticket_group { id name } } } }")


def get_token() -> str:
    return gql_client.get_token()


def current_group(ticket_id: str):
    """Current ticket_group id, or None on ANY failure (transport, errors, null data, odd shape)."""
    try:
        data = gql_client.execute(GROUP_QUERY % gql_client.id_scalar(), {"id": gql_client.parse_ticket_id(ticket_id)})
        return gql_client.dig(data, "tickets", "entities")[0]["ticket_group"]["id"]
    except (gql_client.GraphQLError, ValueError, IndexError, KeyError, TypeError):
        return None


def _webhook_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return bool(parts.hostname) and (parts.scheme == "https" or
                                     (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")))


def send_chat(report: dict) -> None:
    webhook = envfile.get("NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL")
    if not webhook:
        print("NOTE: NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL not set — report not sent", file=sys.stderr)
        return
    if not _webhook_ok(webhook):
        print("NOTE: chat webhook must be an https:// URL — report not sent", file=sys.stderr)
        return
    m = report["metrics"]
    lines = [
        "*NOC Agent — Weekly Metrics*",
        f"Window: {report['window']['since'][:10]} → {report['window']['until'][:10]}",
        "",
        f"• Processed: *{m['total_processed']}* "
        f"(dup {m['by_status'].get('handled_duplicate', 0)}, "
        f"categorized {m['by_status'].get('categorized', 0)}, "
        f"auto-closed {sum(v for k, v in m['by_status'].items() if k.startswith('auto_closed'))}, "
        f"bounced {m['bounces']}, escalated {m['escalated']})",
        f"• Straight-through: *{m['straight_through_rate']}*  • Escalation: *{m['needs_human_rate']}*  • Bounce: *{m['bounce_rate']}*",
        f"• Bounce reverts: *{m['bounce_reverts']}*  • Analysis failures: *{m['analysis_failures']}*",
        f"• Breaker: {'🔴 OPEN — bounces blocked' if m['breaker_open'] else '🟢 closed'} "
        f"(window error rate {m['breaker_window_rate']}, {m['breaker_window_failures']}/{m['breaker_window_total']} runs)",
    ]
    if report.get("incident_testcases"):
        lines.append(f"• New golden-set stubs from incidents: {', '.join('#' + t for t in report['incident_testcases'])}")
    lines.append("")
    lines.append("_Files: logs/metrics/ · logs/decisions/ · site-pack/evals/_ · _Runbook: docs/incident-runbook.md_")
    card = {
        "cardsV2": [{
            "cardId": "noc-weekly-metrics",
            "card": {
                "header": {
                    "title": "\u2705 NOC Agent \u2014 Weekly Metrics",
                    "subtitle": f"{report['window']['since'][:10]} \u2014 {report['window']['until'][:10]}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/materialicons/check_circle/v1/24px.svg",
                    "imageAltText": "Resolved",
                    "imageType": "CIRCLE",
                },
                "sections": [
                    {
                        "header": "Totals",
                        "widgets": [
                            {"textParagraph": {"text": "<b>\U0001f4ca Processed:</b> " + str(m["total_processed"])}},
                            {"textParagraph": {"text": "<b>By status:</b> " + ", ".join(f"{k}: {v}" for k, v in sorted(m["by_status"].items()))}},
                        ],
                    },
                    {
                        "header": "Rates",
                        "widgets": [
                            {"textParagraph": {"text": "<b>Straight-through:</b> " + str(m["straight_through_rate"])
                                     + " \u2022 <b>Escalation:</b> " + str(m["needs_human_rate"])
                                     + " \u2022 <b>Bounce:</b> " + str(m["bounce_rate"])}},
                            {"textParagraph": {"text": "<b>Bounce reverts:</b> " + str(m["bounce_reverts"])
                                     + " \u2022 <b>Analysis failures:</b> " + str(m["analysis_failures"])}},
                            {"textParagraph": {"text": "<b>Breaker:</b> " + ("OPEN \u2014 bounces blocked" if m["breaker_open"] else "closed")
                                     + " (window " + str(m["breaker_window_rate"]) + ", "
                                     + str(m["breaker_window_failures"]) + "/" + str(m["breaker_window_total"]) + " runs)"}},
                        ],
                    },
                    {
                        "widgets": [
                            {"decoratedText": {"text": "Files: logs/metrics/ \u2022 logs/decisions/ \u2022 site-pack/evals/<br>Runbook: docs/incident-runbook.md"}},
                        ],
                    },
                ],
            },
        }],
        "text": "\n".join(lines),  # plain-text fallback (mobile / notification preview)
    }
    try:
        # urllib: the webhook URL (it embeds key+token) never appears in argv / `ps`.
        req = urllib.request.Request(webhook, data=json.dumps(card).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:  # noqa: BLE001 - report delivery is best effort; never echo the URL
        print(f"NOTE: chat delivery failed ({type(e).__name__})", file=sys.stderr)


def incident_stub_dir() -> Path:
    """Incident testcase stubs hold real ticket ids and outcomes, so they live in the site pack."""
    return noc_config.eval_dir() / "pending"


def main() -> None:
    p = argparse.ArgumentParser(description="Weekly NOC agent metrics rollup")
    p.add_argument("--since", default=None, help="ISO date (default: 7 days ago)")
    p.add_argument("--until", default=None, help="ISO date (default: now)")
    p.add_argument("--no-chat", action="store_true")
    p.add_argument("--incident", action="append", default=[],
                   help="ticket id to convert into a golden-set testcase stub (repeatable)")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    since = (datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
             if args.since else now - timedelta(days=7))
    until = (datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
             if args.until else now)

    recs = decision_log.query(since.isoformat(), until.isoformat(), kind="batch_result")

    # Keep the latest record per ticket (re-processing overwrites)
    latest = {}
    for r in recs:
        tid = str(r.get("ticket_id", ""))
        if tid:
            latest[tid] = r  # query returns ascending by file+line order
    records = list(latest.values())

    by_status = Counter(str(r.get("status", "unknown")) for r in records)
    total = len(records)
    handled = sum(by_status.get(s, 0) for s in HANDLED_STATUSES)
    escalated = sum(by_status.get(s, 0) for s in ESCALATED_STATUSES)
    bounces = sum(by_status.get(s, 0) for s in BOUNCE_STATUSES)
    analysis_failures = sum(
        1 for r in records
        if r.get("status") in ESCALATED_STATUSES and r.get("bottleneck_analysis") == "failed")

    def pct(n: int) -> str:
        return f"{(n / total * 100):.0f}%" if total else "n/a"

    # Breaker status
    breaker = {}
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "circuit_breaker.py"), "status"],
                           capture_output=True, text=True, timeout=10)
        breaker = json.loads(r.stdout) if r.stdout else {}
    except Exception:
        pass

    # Bounce revert check: re-check group of bounced tickets in window
    bounce_ids = [str(r["ticket_id"]) for r in records if r.get("status") in BOUNCE_STATUSES]
    reverts = []
    if bounce_ids and NOC_GROUP_ID is None:
        print("NOTE: NOC group id not configured (NOC_GROUP_ID or profile sonar.groups.noc.id) "
              "— bounce revert check skipped", file=sys.stderr)
    elif bounce_ids and get_token():
        for tid in bounce_ids:
            gid = current_group(tid)
            if gid is None:
                continue  # Sonar unreachable / odd answer: unknown, so record neither success nor failure
            if str(gid) == str(NOC_GROUP_ID):
                reverts.append(tid)
                subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "circuit_breaker.py"),
                     "record", "bounce_reverted", tid],
                    capture_output=True, timeout=10)
            else:
                subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "circuit_breaker.py"),
                     "record", "bounce_success", tid],
                    capture_output=True, timeout=10)

    # Incident → permanent testcase stub
    incident_stubs = []
    if args.incident:
        golden_dir = incident_stub_dir()
        golden_dir.mkdir(parents=True, exist_ok=True)
        for tid in args.incident:
            try:
                tid = str(gql_client.parse_ticket_id(tid))  # digits only: it becomes part of a file name
            except ValueError as e:
                print(f"ERROR: --incident: {e}", file=sys.stderr)
                sys.exit(1)
            stub_path = golden_dir / f"incident_{tid}.json"
            if not stub_path.exists():
                rec = next((r for r in records if str(r.get("ticket_id")) == str(tid)), {})
                stub = {
                    "ticket_id": str(tid),
                    "source": "incident",
                    "created_at": now.isoformat(timespec="seconds"),
                    "expected": {
                        "status": rec.get("status", "needs_human"),
                        "escalation_reason": rec.get("escalation_reason"),
                    },
                    "note": "Stub created from production incident — fill expected classification + add notes.",
                }
                stub_path.write_text(json.dumps(stub, indent=2) + "\n")
                incident_stubs.append(str(tid))

    report = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "metrics": {
            "total_processed": total,
            "straight_through_rate": pct(handled),
            "needs_human_rate": pct(escalated),
            "bounce_rate": pct(bounces),
            "bounces": bounces,
            "escalated": escalated,
            "handled": handled,
            "bounce_reverts": len(reverts),
            "analysis_failures": analysis_failures,
            "by_status": dict(by_status),
            "breaker_open": breaker.get("breaker_open", False),
            "breaker_window_rate": breaker.get("window_rate", "n/a"),
            "breaker_window_failures": breaker.get("window_failures", 0),
            "breaker_window_total": breaker.get("window_total", 0),
        },
        "reverted_ticket_ids": reverts,
        "incident_testcases": incident_stubs,
    }

    out_dir = noc_config.state_dir() / "metrics"
    out_path = out_dir / f"weekly_{until.strftime('%Y-%m-%d')}.json"
    statefile.atomic_write_text(out_path, json.dumps(report, indent=2) + "\n", mode=0o600)
    print(json.dumps(report, indent=2))
    print(f"\nWritten: {out_path}", file=sys.stderr)

    if not args.no_chat:
        send_chat(report)

    # Nonzero exit only on hard problems an operator should see
    if report["metrics"]["breaker_open"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
