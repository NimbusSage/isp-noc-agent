#!/usr/bin/env python3
"""
sonar_bounce.py — Deterministic gate + hardened execution for auto-bounces (IHN / correlated).

Guide criterion: "Agent interprets, code enforces." The agent may RECOMMEND a bounce,
but the group-change write only happens through this script, which enforces in order:

    1. Circuit breaker assert    (scripts/circuit_breaker.py assert-bounce)  -> exit 3
       FAIL CLOSED: only "rc 0 AND a parseable {"bounce_allowed": true}" lets the
       bounce continue. A crash, timeout, bad env, corrupt state or garbage output
       from the breaker blocks exactly like an open breaker.
    2. Permissions kill switch   (BOUNCE_AUTONOMOUS_ENABLED must be "true")  -> exit 4
       DEFAULT OFF: unset, empty or anything but true/1/yes/on blocks.
    3. Org configuration         (group/category ids resolved)               -> exit 4
    4. Confidence threshold      (--confidence >= threshold, required)       -> exit 6
    5. Anti-loop marker check    (fetch last 5 comments; block if a bounce marker
                                 is already present — single-bounce policy) -> exit 5
    6. updateTicket group move   (GraphQL variables, response validated)     -> exit 2 on failure
    7. Decision-log record       (<state dir>/decisions/<YYYY-MM>.jsonl, kind=bounce)

Steps 5-7 run under a per-ticket flock so two concurrent invocations cannot both
pass the anti-loop check.

If any gate fails, the bounce does NOT happen and the exit code tells the agent why.
The agent must then fall through to needs_human + Google Chat per AGENTS.md Step 4.

Usage:
    sonar_bounce.py bounce <ticket_id> [--reason IHN|CORRELATED] [--confidence N]
                         [--upstream-id ID] [--add-categories 131] [--remove-categories 16,17]
                         [--evidence "a,b"] [--dry-run]
    sonar_bounce.py check <ticket_id>

Actions:
    bounce <ticket_id>   gated group move to CS Level 2. ticket_id must be an integer.
    check <ticket_id>    run gates 1-3 and 5 only, no write — exit 0 = would pass

Org ids (NO built-in defaults — an unresolved id refuses with exit 4):
    target group      CS_LEVEL2_GROUP_ID (env or .env), else profile sonar.groups.cs_level_2.id
    --reason IHN, when --add-categories / --remove-categories are omitted:
      add             IHN_BOUNCE_CATEGORY_ID,  else profile sonar.categories.cx_th_side_issue.id
      remove          SLOW_SPEEDS_CATEGORY_ID, else profile sonar.categories.slow_speeds.id  (required)
                      NIGHT_PEAK_CATEGORY_ID,  else profile sonar.categories.night_peak.id   (optional)
    --reason CORRELATED: no category change unless the flags are given (AGENTS.md Step 3.6b)
    Passing a flag (even empty: --remove-categories "") overrides the profile for that side.
    Profile = site-pack/profile.json (override path: NOC_PROFILE_FILE). Env beats profile.

Confidence thresholds (required for both reasons, 0-100):
    IHN          IHN_TOWER_THRESHOLD             default 80   confidence >= threshold passes
    CORRELATED   CORRELATED_CONFIDENCE_THRESHOLD default 75

Env:
    SONAR_TOKEN                 required (environment or .env, parsed without a shell)
    BOUNCE_AUTONOMOUS_ENABLED   master kill switch; must be true/1/yes/on or ALL bounces block (exit 4)
    NOC_STATE_DIR               state dir (breaker, decision log, locks); default logs/
    BREAKER_STATE_FILE          forwarded to circuit_breaker.py if set

Anti-loop markers (case-insensitive substring over last 5 comment bodies):
    "Automated NOC Review"        IHN auto-bounce marker
    "Correlated Upstream Issue"   correlated bounce marker

Exit codes:
    0  bounce executed (or check passed, or --dry-run printed the mutation)
    1  usage / arg error (bad ticket id, bad --confidence, bad category list)
    2  Sonar API error or invalid mutation response (incl. "data": null) — logged as API_ERROR
    3  circuit breaker open, or breaker result unusable — bounces blocked
    4  blocked by configuration: kill switch off, OR group/category ids / thresholds unresolved
    5  anti-loop marker already present — single-bounce policy
    6  confidence missing or below the threshold for --reason
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_log  # noqa: E402
import envfile  # noqa: E402
import gql_client  # noqa: E402
import noc_config  # noqa: E402
import statefile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

EXIT_API, EXIT_BREAKER, EXIT_CONFIG, EXIT_LOOP, EXIT_CONFIDENCE = 2, 3, 4, 5, 6

# Markers that mean "this ticket was already auto-bounced once"
ANTI_LOOP_MARKERS = [
    "Automated NOC Review",
    "Correlated Upstream Issue",
]

DEFAULT_THRESHOLDS = {"IHN": ("IHN_TOWER_THRESHOLD", 80.0),
                      "CORRELATED": ("CORRELATED_CONFIDENCE_THRESHOLD", 75.0)}

_TRUTHY = {"true", "1", "yes", "on"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse defaults to exit 2, which this tool reserves for API errors
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(1)


def die(code: int, message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


# --- org configuration -------------------------------------------------------

def _id_or_die(label: str, env_names, profile_keys, profile) -> Optional[int]:
    try:
        return noc_config.resolve_id(env_names, profile_keys, profile)
    except noc_config.ConfigError as e:
        die(EXIT_CONFIG, f"BLOCKED (config): {label}: {e}")
    return None


def _need(value: Optional[int], label: str, hint: str) -> int:
    if value is None:
        die(EXIT_CONFIG, f"BLOCKED (config): {label} is not configured — {hint}. "
                         "There is no built-in default; set it in .env or site-pack/profile.json.")
    return int(value)


def resolve_target_group(profile: dict) -> int:
    gid = _id_or_die("CS Level 2 group", ["CS_LEVEL2_GROUP_ID", "CS_GROUP_ID"],
                     ["sonar", "groups", "cs_level_2"], profile)
    return _need(gid, "target group (CS Level 2)",
                 "set CS_LEVEL2_GROUP_ID or profile sonar.groups.cs_level_2.id")


def _parse_id_list(raw: str, flag: str) -> List[int]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            die(1, f"ERROR: {flag} must be a comma-separated list of positive integers, got {raw!r}")
        out.append(int(part))
    return out


def resolve_category_plan(reason: str, add_flag: Optional[str], remove_flag: Optional[str],
                          profile: dict):
    """(add_ids, remove_ids). Explicit flags win; omitted flags resolve from env/profile
    for IHN (never from a built-in constant) and mean "no change" for CORRELATED."""
    if add_flag is not None:
        add = _parse_id_list(add_flag, "--add-categories")
    elif reason == "IHN":
        cx = _id_or_die("CX/TH-side Issue category", ["IHN_BOUNCE_CATEGORY_ID"],
                        ["sonar", "categories", "cx_th_side_issue"], profile)
        add = [_need(cx, "IHN add-category (CX/TH-side Issue)",
                     "pass --add-categories, set IHN_BOUNCE_CATEGORY_ID, or set profile "
                     "sonar.categories.cx_th_side_issue.id")]
    else:
        add = []

    if remove_flag is not None:
        remove = _parse_id_list(remove_flag, "--remove-categories")
    elif reason == "IHN":
        slow = _id_or_die("Slow Speeds category", ["SLOW_SPEEDS_CATEGORY_ID"],
                          ["sonar", "categories", "slow_speeds"], profile)
        remove = [_need(slow, "IHN remove-category (Slow Speeds)",
                        "pass --remove-categories, set SLOW_SPEEDS_CATEGORY_ID, or set profile "
                        "sonar.categories.slow_speeds.id")]
        night = _id_or_die("Night/Peak category", ["NIGHT_PEAK_CATEGORY_ID"],
                           ["sonar", "categories", "night_peak"], profile)
        if night is not None:  # optional: not every org has a night-check category
            remove.append(night)
    else:
        remove = []
    return add, remove


def resolve_threshold(reason: str) -> float:
    env_name, default = DEFAULT_THRESHOLDS[reason]
    raw = envfile.get(env_name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        die(EXIT_CONFIG, f"BLOCKED (config): {env_name}={raw!r} is not a number")
    if not (0.0 <= v <= 100.0):
        die(EXIT_CONFIG, f"BLOCKED (config): {env_name}={raw!r} must be between 0 and 100")
    return v


# --- gates -------------------------------------------------------------------

def breaker_allows() -> None:
    """FAIL CLOSED circuit-breaker gate: exit 3 unless the breaker affirmatively allows."""
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "circuit_breaker.py"), "assert-bounce"],
            capture_output=True, text=True, env=dict(os.environ), timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        die(EXIT_BREAKER, f"BLOCKED: circuit breaker check failed to run ({type(e).__name__}: {e}) — failing closed")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        die(EXIT_BREAKER, f"BLOCKED: circuit breaker not clear (rc={result.returncode}) {detail}".rstrip())
    try:
        allowed = json.loads(result.stdout.strip().splitlines()[-1]).get("bounce_allowed") is True
    except (ValueError, IndexError, AttributeError):
        allowed = False
    if not allowed:
        die(EXIT_BREAKER, "BLOCKED: circuit breaker result unparseable — failing closed")


def kill_switch() -> None:
    raw = envfile.get("BOUNCE_AUTONOMOUS_ENABLED")
    if raw.strip().lower() not in _TRUTHY:
        state = "unset" if not raw else repr(raw)
        die(EXIT_CONFIG, f"BLOCKED: BOUNCE_AUTONOMOUS_ENABLED is {state} — autonomous bounces are OFF "
                         "unless it is explicitly true (default off)")


def check_confidence(reason: str, confidence: Optional[int]) -> None:
    threshold = resolve_threshold(reason)
    if confidence is None:
        die(EXIT_CONFIDENCE, f"BLOCKED: --confidence is required for --reason {reason}")
    if confidence < threshold:
        die(EXIT_CONFIDENCE, f"BLOCKED: confidence {confidence} is below the {reason} threshold {threshold:g}")


def check_anti_loop(ticket_id: int) -> bool:
    """True if any anti-loop marker appears in the last 5 comments."""
    try:
        comments = gql_client.fetch_recent_comments(ticket_id)
    except gql_client.GraphQLError as e:
        die(EXIT_API, f"ERROR: comment fetch failed ({e.kind}): {e.message}")
    bodies = " || ".join((c.get("body") or "") for c in comments if isinstance(c, dict)).lower()
    return any(m.lower() in bodies for m in ANTI_LOOP_MARKERS)


# --- mutation ----------------------------------------------------------------

def categories_query() -> str:
    sc = gql_client.id_scalar()
    return (f"query TicketCategories($id: {sc}!) {{ tickets(search: {{ integer_fields: "
            "[{ attribute: \"id\", operator: EQ, search_value: $id }] }) "
            "{ entities { id ticket_categories { entities { id } } } } }")


def bounce_mutation() -> str:
    sc = gql_client.id_scalar()
    return (f"mutation BounceTicket($id: {sc}!, $input: UpdateTicketMutationInput!) {{ "
            "updateTicket(id: $id, input: $input) { id ticket_group { id name } "
            "ticket_categories { entities { id name } } } }")


def resolve_categories(ticket_id: int, add: List[int], remove: List[int]) -> List[int]:
    """Fetch current categories, apply removes and adds (category ids REPLACE all)."""
    try:
        data = gql_client.execute(categories_query(), {"id": ticket_id})
        entities = gql_client.dig(data, "tickets", "entities")
        if not entities:
            raise gql_client.GraphQLError("shape", f"ticket {ticket_id} not found")
        cats = gql_client.dig(entities[0], "ticket_categories", "entities")
        current = [int(c["id"]) for c in cats]
    except (gql_client.GraphQLError, KeyError, TypeError, ValueError) as e:
        msg = e.message if isinstance(e, gql_client.GraphQLError) else f"unexpected category shape: {e}"
        die(EXIT_API, f"ERROR: could not fetch current categories: {msg}")
    final = [c for c in current if c not in remove]
    for c in add:
        if c not in final:
            final.append(c)
    return final


def build_variables(ticket_id: int, group_id: int, final_categories: List[int],
                    change_categories: bool) -> dict:
    # Sonar's ticket_category_ids REPLACES all categories, never merges, so it is
    # only sent when the plan changes categories.
    inp = {"ticket_group_id": group_id}
    if change_categories:
        inp["ticket_category_ids"] = list(final_categories)
    return {"id": ticket_id, "input": inp}


def _log(payload: dict, ticket_id: int):
    """Decision-log write that can never turn a finished bounce into a traceback."""
    try:
        return decision_log.append_decision(payload, kind="bounce", ticket_id=str(ticket_id))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: decision log write failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def run_bounce(args, profile: dict) -> None:
    ticket_id = args.ticket_id
    group_id = resolve_target_group(profile)
    add_ids, remove_ids = resolve_category_plan(args.reason, args.add_categories,
                                                args.remove_categories, profile)
    change_categories = bool(add_ids or remove_ids)

    check_confidence(args.reason, args.confidence)

    if check_anti_loop(ticket_id):
        _log({"action": "BLOCKED_ANTI_LOOP", "reason": args.reason,
              "upstream_id": args.upstream_id}, ticket_id)
        die(EXIT_LOOP, "BLOCKED: anti-loop marker already present — single-bounce policy")

    final_cats = resolve_categories(ticket_id, add_ids, remove_ids) if change_categories else []
    variables = build_variables(ticket_id, group_id, final_cats, change_categories)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "ticket_id": ticket_id, "reason": args.reason,
                          "mutation": bounce_mutation(), "variables": variables,
                          "final_categories": final_cats, "target_group": group_id}, indent=2))
        sys.exit(0)

    try:
        data = gql_client.execute(bounce_mutation(), variables)
        updated = gql_client.dig(data, "updateTicket")
        if not isinstance(updated, dict) or not updated.get("id"):
            raise gql_client.GraphQLError("shape", "updateTicket returned no ticket id")
        got_group = (updated.get("ticket_group") or {}).get("id")
        if got_group is not None and str(got_group) != str(group_id):
            raise gql_client.GraphQLError(
                "shape", f"updateTicket left the ticket in group {got_group}, expected {group_id}")
    except gql_client.GraphQLError as e:
        _log({"action": "API_ERROR", "reason": args.reason, "error_kind": e.kind,
              "response": e.message[:500]}, ticket_id)
        die(EXIT_API, f"ERROR: updateTicket failed ({e.kind}): {e.message[:300]}")

    # Success — record in decision log with full evidence
    evidence = [e.strip() for e in args.evidence.split(",") if e.strip()]
    if args.upstream_id:
        evidence.append(f"upstream:{args.upstream_id}")
    record = _log({
        "action": "BOUNCED",
        "reason": args.reason, "upstream_id": args.upstream_id,
        "confidence": args.confidence, "evidence": evidence,
        "final_categories": final_cats, "target_group": group_id,
        "profile_applied": bool(profile),
        "requires_human_review": False, "escalation_reason": None,
    }, ticket_id)
    print(json.dumps({"bounced": str(ticket_id), "group": group_id, "record": record}))


def main(argv: Optional[List[str]] = None) -> None:
    p = _Parser(description="Gated bounce executor")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bounce", help="gated group move to CS Level 2")
    b.add_argument("ticket_id")
    b.add_argument("--reason", choices=["IHN", "CORRELATED"], default="IHN")
    b.add_argument("--upstream-id", default=None)
    b.add_argument("--add-categories", default=None,
                   help="comma-separated ids to add (omitted: IHN resolves from env/profile)")
    b.add_argument("--remove-categories", default=None,
                   help="comma-separated ids to remove (omitted: IHN resolves from env/profile)")
    b.add_argument("--confidence", type=int, default=None,
                   help="decisive confidence 0-100; REQUIRED, must meet the reason's threshold")
    b.add_argument("--evidence", default="", help="comma-separated evidence strings")
    b.add_argument("--dry-run", action="store_true",
                   help="run all gates, print the mutation + variables, send nothing")

    c = sub.add_parser("check", help="gates only, no write")
    c.add_argument("ticket_id")

    args = p.parse_args(argv)
    try:
        args.ticket_id = gql_client.parse_ticket_id(args.ticket_id)
    except ValueError as e:
        die(1, f"ERROR: {e}")
    if args.cmd == "bounce":
        if args.confidence is not None and not (0 <= args.confidence <= 100):
            die(1, "ERROR: --confidence must be between 0 and 100")
        if args.upstream_id is not None:
            try:
                args.upstream_id = str(gql_client.parse_ticket_id(args.upstream_id))
            except ValueError:
                die(1, "ERROR: --upstream-id must be an integer ticket id")

    profile = noc_config.load_profile()

    if args.cmd == "check":
        breaker_allows()
        kill_switch()
        resolve_target_group(profile)
        if check_anti_loop(args.ticket_id):
            die(EXIT_LOOP, "BLOCKED: anti-loop marker already present")
        print(f"OK: gates pass for ticket {args.ticket_id}")
        sys.exit(0)

    # --- bounce ---
    breaker_allows()   # exit 3 unless affirmatively clear (fail closed)
    kill_switch()      # exit 4 unless BOUNCE_AUTONOMOUS_ENABLED is explicitly true

    lock_target = noc_config.state_dir() / "locks" / f"bounce-{args.ticket_id}"
    try:
        with statefile.locked(lock_target):
            run_bounce(args, profile)
    except (OSError, statefile.LockTimeout) as e:
        die(EXIT_CONFIG, f"BLOCKED (config): cannot take the per-ticket bounce lock under "
                         f"{lock_target.parent}: {e}")


if __name__ == "__main__":
    main()
