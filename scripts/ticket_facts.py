#!/usr/bin/env python3
"""ticket_facts.py - fetch each batch ticket's record and comments once, before the agent starts.

The server runs this on the batch file before it spawns the agent. For every ticket it adds a `facts` object:
the Sonar ticket (subject, status, priority, group, categories, description), its first note and newest comments
(HTML stripped, trimmed), and deterministic flags from scripts/ticket_rules.py: Step 0 matches, weather and
night/peak keyword hits over the whole trail, the comment markers present (anti-loop and one-per-ticket rules),
whether "now" is inside the peak window, and the `Tower:` / `Network/Fiber:` fields. The agent then reads all of
it with the batch file instead of spending a dozen model requests fetching and re-reading it.

Read-only: queries only, through scripts/gql_client.py (token from the environment or .env, never argv). Every
string it copies from a ticket is untrusted data, exactly like the rest of the batch file. A ticket whose fetch
fails gets `facts_error` instead and the agent falls back to fetching it itself.

Usage:
  python3 scripts/ticket_facts.py --batch-file logs/batch_<job>.json   # enrich in place, print a one-line summary
  python3 scripts/ticket_facts.py --ticket 123456                      # print one ticket's facts as JSON
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import gql_client  # noqa: E402
import noc_config  # noqa: E402
import ticket_rules as rules  # noqa: E402

COMMENTS_FETCHED = 50       # flags see this many (oldest first, so the first note is always included)
COMMENTS_SHOWN = 15         # newest comments copied into the batch file
COMMENT_CHARS = 600
DESCRIPTION_CHARS = 2000
FIRST_NOTE_CHARS = 1500

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n\s*\n+")
_FIELD_RES = {
    "tower": re.compile(r"(?im)^[ \t>*-]*Tower:[ \t]*([^\n|]+)"),
    "network_fiber": re.compile(r"(?im)^[ \t>*-]*Network/Fiber:[ \t]*([^\n|]+)"),
    "radio_ap": re.compile(r"(?im)^[ \t>*-]*Radio AP:[ \t]*([^\n|]+)"),
}
_PREFIX_RE = re.compile(r"^\s*[\[(]([^\])]{1,60})[\])]\s*(.*)$")


def ticket_query() -> str:
    sc = gql_client.id_scalar()
    return (f"query TicketFacts($id: {sc}!) {{ tickets(search: {{ integer_fields: "
            "[{ attribute: \"id\", operator: EQ, search_value: $id }] }) { entities { id subject status priority "
            "description created_at ticket_group { id name } ticket_categories { entities { id name } } } } }")


def comments_query() -> str:
    sc = gql_client.id_scalar()
    return (f"query TicketFactsComments($id: {sc}!) {{ ticket_comments(search: {{ integer_fields: "
            "[{ attribute: \"ticket_id\", operator: EQ, search_value: $id }] }, sorter: { attribute: \"created_at\", "
            f"direction: ASC }}, paginator: {{ page: 1, records_per_page: {COMMENTS_FETCHED} }}) "
            "{ entities { id body created_at } page_info { total_count } } }")


def plain(text: Any) -> str:
    """HTML to plain text: tags dropped (block tags become line breaks), entities decoded, whitespace collapsed."""
    s = str(text or "")
    s = re.sub(r"(?i)<\s*img\b[^>]*>", " [image] ", s)   # screenshots (speed tests) would otherwise read as empty
    s = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr|/h\d)\s*/?\s*>", "\n", s)
    s = html.unescape(_TAG_RE.sub("", s))
    s = _WS_RE.sub(" ", s)
    return _NL_RE.sub("\n", s).strip()


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _ids(env_names, profile_keys, profile) -> Optional[int]:
    try:
        return noc_config.resolve_id(env_names, profile_keys, profile)
    except Exception:  # a misconfigured id must not stop the facts; the agent resolves ids itself
        return None


def peak_window(now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    from zoneinfo import ZoneInfo
    tzname = envfile.get("PEAK_WINDOW_TZ") or "America/Chicago"
    start = envfile.get("PEAK_WINDOW_START") or "20:00"
    end = envfile.get("PEAK_WINDOW_END") or "21:00"
    try:
        tz = ZoneInfo(tzname)
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
        cur = local.strftime("%H:%M")
        inside = (start <= cur < end) if start <= end else (cur >= start or cur < end)
        return {"now_local": local.strftime("%Y-%m-%d %H:%M"), "window": f"{start}-{end} {tzname}", "inside": inside}
    except Exception as e:  # bad zone or format: say so, decide nothing
        return {"window": f"{start}-{end} {tzname}", "inside": None, "error": str(e)[:80]}


def fields(text: str) -> Dict[str, Optional[str]]:
    out = {}
    for name, rx in _FIELD_RES.items():
        m = rx.search(text or "")
        out[name] = clip(m.group(1).strip(), 80) if m else None
    return out


def build_facts(ticket_id: int, ticket: dict, comments: List[dict], profile: Optional[dict],
                now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    subject = str(ticket.get("subject") or "")
    description = plain(ticket.get("description"))
    group = ticket.get("ticket_group") or {}
    cats = [{"id": c.get("id"), "name": c.get("name")}
            for c in ((ticket.get("ticket_categories") or {}).get("entities") or []) if isinstance(c, dict)]
    cat_ids = {c["id"] for c in cats}
    bodies_raw = [str(c.get("body") or "") for c in comments]
    bodies = [plain(b) for b in bodies_raw]
    trail = "\n".join([subject, description, *bodies])

    sys_admin_id = _ids(["SYSADMIN_GROUP_ID"], ["sonar", "groups", "sys_admin"], profile)
    cat = {
        "night_peak": _ids(["NIGHT_PEAK_CATEGORY_ID"], ["sonar", "categories", "night_peak"], profile),
        "weather": _ids(["WEATHER_CATEGORY_ID"], ["sonar", "categories", "weather"], profile),
        "slow_speeds": _ids(["SLOW_SPEEDS_CATEGORY_ID"], ["sonar", "categories", "slow_speeds"], profile),
        "cx_th_side_issue": _ids(["IHN_BOUNCE_CATEGORY_ID"], ["sonar", "categories", "cx_th_side_issue"], profile),
    }
    last5 = set()
    for b in bodies_raw[-5:]:
        last5.update(rules.markers_in(b))
    last20 = set()
    for b in bodies_raw[-20:]:
        last20.update(rules.markers_in(b))
    anywhere = set()
    for b in bodies_raw:
        anywhere.update(rules.markers_in(b))

    first = comments[0] if comments else None
    first_text = plain(first.get("body")) if first else ""
    prefix = _PREFIX_RE.match(subject)
    newest = list(zip(comments, bodies))[-COMMENTS_SHOWN:][::-1]
    return {
        "source": "scripts/ticket_facts.py",
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ticket": {
            "id": ticket.get("id") or ticket_id,
            "subject": clip(subject, 300),
            "status": ticket.get("status"),
            "priority": ticket.get("priority"),
            "created_at": ticket.get("created_at"),
            "group": {"id": group.get("id"), "name": group.get("name")},
            "categories": cats,
            "description": clip(description, DESCRIPTION_CHARS),
        },
        "first_note": ({"created_at": first.get("created_at"), "body": clip(first_text, FIRST_NOTE_CHARS)}
                       if first else None),
        "comments": {
            "fetched": len(comments),
            "total": None,
            "newest_first": [{"created_at": c.get("created_at"), "body": clip(b, COMMENT_CHARS)} for c, b in newest],
        },
        "flags": {
            "sys_admin": bool((sys_admin_id is not None and group.get("id") == sys_admin_id)
                              or rules.SYS_ADMIN_NAME in str(group.get("name") or "").lower()),
            "ooo_subject_hits": rules.hits(subject, rules.OOO_SUBJECT),
            "ooo_body_hits": rules.hits(description, rules.OOO_BODY),
            "pbs_subject_exact": subject.strip() == rules.PBS_SUBJECT,
            "pbs_successful": "successful" in subject.lower() and "successful" in description.lower(),
            "pbs_failed": "failed" in subject.lower() or "failed" in description.lower(),
            "maintenance_subject": rules.MAINTENANCE_SUBJECT.lower() in subject.lower(),
            "weather_hits": rules.hits(trail, rules.WEATHER),
            "night_peak_hits": rules.hits(trail, rules.NIGHT_PEAK),
            "night_check_subject": rules.NIGHT_CHECK_SUBJECT.lower() in subject.lower(),
            "categories_present": {k: (v in cat_ids if v is not None else None) for k, v in cat.items()},
            "markers": {"last_5": sorted(last5), "last_20": sorted(last20), "any": sorted(anywhere)},
            "peak_window": peak_window(now),
        },
        "tower_hints": {
            "first_note_fields": fields(first_text),
            "description_fields": fields(description),
            "subject_prefix": clip(prefix.group(1).strip(), 60) if prefix else None,
            "subject_after_prefix": clip(prefix.group(2).strip(), 120) if prefix else None,
        },
    }


def fetch(ticket_id: int, **kw: Any) -> Dict[str, Any]:
    data = gql_client.execute(ticket_query(), {"id": ticket_id}, **kw)
    entities = gql_client.dig(data, "tickets", "entities")
    if not isinstance(entities, list) or not entities:
        raise gql_client.GraphQLError("shape", "ticket not found")
    ticket = entities[0]
    cdata = gql_client.execute(comments_query(), {"id": ticket_id}, **kw)
    comments = gql_client.dig(cdata, "ticket_comments", "entities")
    if not isinstance(comments, list):
        raise gql_client.GraphQLError("shape", "ticket_comments.entities is not a list")
    facts = build_facts(ticket_id, ticket, comments, noc_config.load_profile())
    total = gql_client.dig(cdata, "ticket_comments", "page_info", "total_count")
    facts["comments"]["total"] = total if isinstance(total, int) else None
    return facts


def enrich_batch(path: Path, timeout: float = 20.0) -> Dict[str, int]:
    items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("batch file is not a JSON array")
    ok = failed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            tid = gql_client.parse_ticket_id(item.get("ticket_id"))
            item["facts"] = fetch(tid, timeout=timeout)
            item.pop("facts_error", None)
            ok += 1
        except (ValueError, gql_client.GraphQLError) as e:
            item["facts_error"] = clip(str(getattr(e, "message", e)), 160)
            failed += 1
    tmp = path.with_name(path.name + ".facts.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"tickets": ok + failed, "with_facts": ok, "failed": failed}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch-file", type=Path, help="batch JSON array to enrich in place")
    g.add_argument("--ticket", help="print one ticket's facts as JSON")
    ap.add_argument("--timeout", type=float, default=20.0, help="per-request timeout in seconds (default 20)")
    args = ap.parse_args(argv)
    if not gql_client.get_token():
        print("ticket_facts: SONAR_TOKEN is not set; no facts fetched", file=sys.stderr)
        return 2
    if args.ticket:
        try:
            print(json.dumps(fetch(gql_client.parse_ticket_id(args.ticket), timeout=args.timeout), indent=2))
        except (ValueError, gql_client.GraphQLError) as e:
            print(f"ticket_facts: {getattr(e, 'message', e)}", file=sys.stderr)
            return 1
        return 0
    try:
        summary = enrich_batch(args.batch_file, timeout=args.timeout)
    except (OSError, ValueError) as e:
        print(f"ticket_facts: cannot enrich {args.batch_file.name}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
