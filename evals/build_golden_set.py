#!/usr/bin/env python3
"""
build_golden_set.py — Build the golden eval dataset from Sonar ticket history.

Guide criterion: "Collect 50-100 real tickets with known correct classifications;
re-run this dataset every time you change prompts, models, or logic."

Pulls NOC tickets (the configured NOC group) from Sonar via GraphQL, derives an expected label for
each from its OUTCOME (categories + final group + status — the ground truth Sonar
already recorded), and writes site-pack/evals/golden_set.json (the real tickets never go in the
tracked tree; keep that file in a private repository, see docs/team.md):

    {
      "built_at": "...",
      "source": "sonar",
      "tickets": [
        {
          "ticket_id": "100001",
          "subject": "[Alpha] alpha pppoe - slow speeds",
          "description_snippet": "...",
          "expected": {
            "classification_category": "slow_speeds",   // derived from Sonar categories
            "expected_status": "auto_bounced_ihn",      // derived from outcome
            "notes": "derived from final group=2 + category 131"
          }
        }, ...
      ]
    }

Outcome derivation heuristics (deterministic, editable):
    - final group CS L2 + CX/TH-side bounce category      -> auto_bounced_ihn
    - weather category / weather keywords                  -> weather_excluded (needs_human)
    - Slow Speeds category still in NOC                    -> categorized
    - status CLOSED + passthrough-ish subject              -> auto_closed*
Label quality note: these are heuristic ground truth. Review a sample after building;
hand-fix mislabels in the output set (they are yours to edit — the file is the dataset).

Usage:
    build_golden_set.py --limit 100 [--group <noc_group_id>] [--out path]
    build_golden_set.py --add-incident 12345   # merge one incident ticket into existing set
    build_golden_set.py --relabel-keywords     # re-derive only keyword-derived labels

Group IDs are org-specific: resolve in order (1) --group flag / env, (2) site-pack
profile (site-pack/profile.json sonar.groups / sonar.categories), (3) nothing — exit
with a clear hint. Never hardcode an org's IDs here.

Env: SONAR_TOKEN, NOC_GROUP_ID, CS_LEVEL2_GROUP_ID, BOUNCE_CATEGORY_ID
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))
import classifier as kw_classifier  # noqa: E402  (keyword fallback for unlabeled tickets)
import gql_client  # noqa: E402  (urllib + GraphQL variables; token never in argv)
import noc_config  # noqa: E402

IDS = {"noc_group": None, "cs_group": None, "slow_cat": None,
       "night_cat": None, "bounce_cat": None, "weather_cat": None}

# Real tickets stay out of the tracked tree: the default output is the org's site pack
# (site-pack/evals/golden_set.json, which eval_run.py reads first). evals/golden/ holds only the example set.
GOLDEN_DIR = noc_config.eval_dir()
DEFAULT_OUT = GOLDEN_DIR / "golden_set.json"
PASSTHROUGH_SUBJECTS = [
    (r"garbage collect datastore 'backups' successful", "auto_closed"),
    (r"out of office|automatic reply|auto[- ]?reply", "auto_closed_ooo"),
    (r"maintenance", "auto_closed_maintenance"),
]


def sonar_graphql(query: str, variables: dict = None) -> dict:
    """Run one operation through the shared client; exits 2 with a clean message on any failure."""
    try:
        return gql_client.execute(query, variables or {}, timeout=60)
    except gql_client.GraphQLError as e:
        print(f"ERROR: Sonar API ({e.kind}): {e.message}", file=sys.stderr)
        sys.exit(2)


def fetch_group_tickets(limit: int, group_id: int) -> list:
    sc = gql_client.id_scalar()
    q = (
        f"query GroupTickets($group: {sc}!, $per: Int!) {{ tickets(search: {{ integer_fields: "
        "[{ attribute: \"ticket_group_id\", operator: EQ, search_value: $group }] }, "
        "sorter: { attribute: \"created_at\", direction: DESC }, "
        "paginator: { page: 1, records_per_page: $per }) { "
        "entities { id subject status description ticket_group { id name } "
        "ticket_categories { entities { id name } } } page_info { total_count } } }"
    )
    data = sonar_graphql(q, {"group": int(group_id), "per": min(int(limit), 100)})
    try:
        return data["tickets"]["entities"]
    except (KeyError, TypeError):
        print(f"ERROR: unexpected tickets response: {json.dumps(data)[:300]}", file=sys.stderr)
        sys.exit(2)


def _profile() -> dict:
    return noc_config.load_profile()


def resolve_ids() -> dict:
    """Resolve org-specific Sonar ids: env override > site-pack profile > missing.

    Missing ids are handled by main() with a clear hint — this builder must work
    for any org, so nothing here hardcodes a Sonar group or category number.
    """
    prof = _profile()
    sg = (prof.get("sonar") or {}).get("groups") or {}
    sc = (prof.get("sonar") or {}).get("categories") or {}

    def pick(env, prof_val, *profile_keys):
        v = os.environ.get(env)
        if v and v.strip().isdigit():
            return int(v)
        node = prof_val or {}
        for k in profile_keys:
            node = (node.get(k) or {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and isinstance(node.get("id"), int):
            return int(node["id"])
        return None

    return {
        "noc_group": pick("NOC_GROUP_ID", sg, "noc"),
        "cs_group": pick("CS_LEVEL2_GROUP_ID", sg, "cs_level_2"),
        "slow_cat": pick("SLOW_SPEEDS_CATEGORY_ID", sc, "slow_speeds"),
        "night_cat": pick("NIGHT_PEAK_CATEGORY_ID", sc, "night_peak"),
        "bounce_cat": pick("BOUNCE_CATEGORY_ID", sc, "cx_th_side_issue"),
        "weather_cat": pick("WEATHER_CATEGORY_ID", sc, "weather"),
    }


def fetch_noc_tickets(limit: int, group_id: int, cs_group: int, bounce_cat: int) -> list:
    """NOC tickets + bounced tickets (CS L2 group carrying the bounce category).

    Bounced tickets LEAVE the NOC group when the agent moves them to CS L2, so a
    NOC-only fetch can never contain real auto_bounced_ihn examples — the
    highest-value eval class. Fetch the CS L2 group as well and keep only tickets
    with the bounce category.
    """
    noc = fetch_group_tickets(limit, group_id)
    if cs_group is None or bounce_cat is None:
        return noc  # no CS L2 group / bounce category configured: NOC-only set
    cs = fetch_group_tickets(min(limit, 100), cs_group)  # CS L2 — keep only bounce signature
    bounced = [t for t in cs if any(c["id"] == bounce_cat for c in (t.get("ticket_categories") or {}).get("entities", []))]
    seen = set()
    merged = []
    for t in noc + bounced:
        tid = str(t["id"])
        if tid not in seen:
            seen.add(tid)
            merged.append(t)
    return merged


def derive_expected(t: dict) -> dict:
    cats = {c["id"]: c["name"] for c in (t.get("ticket_categories") or {}).get("entities", [])}
    cat_ids = set(cats.keys())
    group_id = ((t.get("ticket_group") or {}).get("id"))
    status = t.get("status", "")
    subject = (t.get("subject") or "")
    desc = re.sub(r"<[^>]*>", "", (t.get("description") or ""))

    ids = IDS
    if group_id == ids["cs_group"] and ids["bounce_cat"] in cat_ids:
        return {"classification_category": "slow_speeds", "expected_status": "auto_bounced_ihn",
                "derived_from": "sonar", "notes": f"final group=CS L2 + bounce category {ids['bounce_cat']}"}
    if ids["weather_cat"] is not None and ids["weather_cat"] in cat_ids:
        return {"classification_category": "rain_fade", "expected_status": "needs_human",
                "derived_from": "sonar", "notes": f"weather category {ids['weather_cat']} present"}
    for pattern, label in PASSTHROUGH_SUBJECTS:
        if re.search(pattern, subject.lower()):
            return {"classification_category": "passthrough", "expected_status": label,
                    "derived_from": "sonar", "notes": f"subject matches /{pattern}/"}
    if ids["slow_cat"] is not None and ids["slow_cat"] in cat_ids:
        return {"classification_category": "slow_speeds", "expected_status": "categorized",
                "derived_from": "sonar", "notes": f"slow-speeds category {ids['slow_cat']} present, still in NOC"}
    if ids["night_cat"] is not None and ids["night_cat"] in cat_ids:
        return {"classification_category": "peak_congestion", "expected_status": "queued_for_peak",
                "derived_from": "sonar", "notes": f"night/peak category {ids['night_cat']} present"}
    # Labeling gap: Sonar has no NOC-relevant categories. Fall back to keyword derivation
    # (same families as evals/classifier.py) so the floor eval has a comparable label.
    # These are marked derived_from=keywords and scored separately by eval_run.py —
    # the sonar-labeled subset is the real accuracy signal.
    kw = kw_classifier.classify(subject, desc)
    return {"classification_category": kw["classification_category"],
            "expected_status": kw["expected_status"],
            "derived_from": "keywords",
            "notes": f"no Sonar categories ({sorted(cat_ids)}); keyword-derived label", }



def build(limit: int, group_id: int, out: Path) -> None:
    tickets = fetch_noc_tickets(limit, group_id, IDS["cs_group"], IDS["bounce_cat"])
    entries = []
    for t in tickets:
        expected = derive_expected(t)
        entries.append({
            "ticket_id": str(t["id"]),
            "subject": t.get("subject") or "",
            "description_snippet": re.sub(r"<[^>]*>", "", (t.get("description") or ""))[:200],
            "sonar_status": t.get("status"),
            "sonar_categories": {str(k): v for k, v in
                                 {c["id"]: c["name"] for c in (t.get("ticket_categories") or {}).get("entities", [])}.items()},
            "expected": expected,
        })
    out.parent.mkdir(parents=True, exist_ok=True)
    dataset = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "sonar",
        "count": len(entries),
        "tickets": entries,
    }
    out.write_text(json.dumps(dataset, indent=2) + "\n")
    print(json.dumps({"written": str(out), "count": len(entries),
                      "label_breakdown": _label_stats(entries)}, indent=2))


def relabel_keywords(out: Path) -> None:
    """Re-derive labels ONLY for entries marked derived_from=keywords.

    Run this after changing evals/classifier.py regex families: keyword-derived
    labels are coverage labels tautological with the classifier, so they must be
    refreshed when the classifier's keyword surface changes or every such change
    looks like a regression. Sonar ground-truth entries (derived_from=sonar) are
    never touched, and hand-authored labels keep whatever "derived_from" you set
    for them (anything not exactly "keywords" is left alone).
    """
    if not out.exists():
        print(json.dumps({"error": "set not found", "path": str(out),
                          "hint": "build one first: python3 evals/build_golden_set.py --limit 100"}),
              file=sys.stderr)
        sys.exit(1)
    dataset = json.loads(out.read_text())
    changed = 0
    keyword_entries = 0
    for entry in dataset.get("tickets", []):
        expected = entry.get("expected") or {}
        if expected.get("derived_from") != "keywords":
            continue
        keyword_entries += 1
        kw = kw_classifier.classify(entry.get("subject", ""), entry.get("description_snippet", ""))
        if (expected.get("classification_category") != kw["classification_category"]
                or expected.get("expected_status") != kw["expected_status"]):
            changed += 1
        expected["classification_category"] = kw["classification_category"]
        expected["expected_status"] = kw["expected_status"]
        expected["notes"] = ("keyword-derived label re-derived "
                             f"{datetime.now(timezone.utc).date().isoformat()}")
    dataset["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out.write_text(json.dumps(dataset, indent=2) + "\n")
    print(json.dumps({"relabeled": str(out), "keyword_entries": keyword_entries,
                      "changed": changed}, indent=2))


def _label_stats(entries: list) -> dict:
    from collections import Counter
    return dict(Counter(e["expected"]["expected_status"] for e in entries))


def add_incident(ticket_id: str, out: Path) -> None:
    try:
        tid = gql_client.parse_ticket_id(ticket_id)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    sc = gql_client.id_scalar()
    q = (
        f"query IncidentTicket($id: {sc}!) {{ tickets(search: {{ integer_fields: "
        "[{ attribute: \"id\", operator: EQ, search_value: $id }] }) { entities { id subject status description "
        "ticket_group { id name } ticket_categories { entities { id name } } } } }"
    )
    data = sonar_graphql(q, {"id": tid})
    try:
        t = data["tickets"]["entities"][0]
    except (KeyError, IndexError, TypeError):
        print(f"ERROR: ticket {ticket_id} not found", file=sys.stderr)
        sys.exit(2)
    dataset = json.loads(out.read_text()) if out.exists() else {"built_at": None, "source": "sonar", "count": 0, "tickets": []}
    entry = {
        "ticket_id": str(t["id"]),
        "subject": t.get("subject") or "",
        "description_snippet": re.sub(r"<[^>]*>", "", (t.get("description") or ""))[:200],
        "sonar_status": t.get("status"),
        "expected": derive_expected(t),
    }
    dataset["tickets"] = [x for x in dataset["tickets"] if x["ticket_id"] != str(ticket_id)] + [entry]
    dataset["count"] = len(dataset["tickets"])
    dataset["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, indent=2) + "\n")
    print(json.dumps({"added": str(ticket_id), "count": dataset["count"]}))


def main() -> None:
    global IDS
    IDS = resolve_ids()
    p = argparse.ArgumentParser(description="Build golden eval set from Sonar")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--group", type=int, default=None,
                   help="NOC group id (default: NOC_GROUP_ID env, then site-pack profile)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--add-incident", type=str, default=None)
    p.add_argument("--relabel-keywords", action="store_true",
                   help="re-derive only derived_from=keywords labels from the current "
                        "classifier (run after editing evals/classifier.py)")
    args = p.parse_args()

    if args.relabel_keywords:
        relabel_keywords(args.out)
    elif args.add_incident:
        add_incident(args.add_incident, args.out)
    else:
        if IDS["noc_group"] is None:
            print("ERROR: org-specific Sonar ids not resolved. Set NOC_GROUP_ID "
                  "(and ideally CS_LEVEL2_GROUP_ID) in the environment, or add a "
                  "gitignored site-pack/profile.json with a sonar.groups block:",
                  json.dumps({"sonar": {"groups": {"noc": {"id": 0}, "cs_level_2": {"id": 0}},
                                        "categories": {"slow_speeds": {"id": 0}, "night_peak": {"id": 0},
                                                       "cx_th_side_issue": {"id": 0}, "weather": {"id": 0}}}}),
                  sep="\n", file=sys.stderr)
            sys.exit(2)
        build(args.limit, args.group if args.group is not None else IDS["noc_group"], args.out)


if __name__ == "__main__":
    main()
