#!/usr/bin/env python3
"""ticket_rules.py - the literal keyword rules of the AGENTS.md ticket workflow, in one place.

Every term here is copied from AGENTS.md (Steps 0, 3.5b, 3.7) and the comment markers the skills write. A test
(scripts/test_ticket_facts.py) fails when a term here no longer appears in AGENTS.md, so a rule change there must
be mirrored here. These are keyword HITS only: whether a ticket is weather-related "in other words", or which
tower it names, stays the agent's judgment.
"""
import re
from typing import Dict, Iterable, List, Tuple

# Step 0: out-of-office auto-reply (Sys Admin only). Subject terms AND body terms must both hit.
OOO_SUBJECT = ("out of office", "of office", "automatic reply", "auto reply", "auto-reply", "autoreply", "ooo")
OOO_BODY = ("currently traveling", "will respond as time permits", "on vacation", "on leave",
            "away from the office", "will be out of the office", "out of the office", "currently away")
# Step 0: Proxmox Backup Server garbage-collect notice and the Maintenance auto-close.
PBS_SUBJECT = "Garbage Collect Datastore 'backups' successful"
MAINTENANCE_SUBJECT = "Maintenance"
SYS_ADMIN_NAME = "sys admin"

# Step 3.7 gate 2 (and Step 3.5b): weather terms over subject, description and every comment.
WEATHER = ("rain", "storm", "weather", "precipitation", "rain fade", "weather fade", "wet weather", "bad weather",
           "thunder", "lightning", "wind damage", "wet")
# Step 3.7 gate 3: night/peak terms.
NIGHT_PEAK = ("night check", "n check", "night checks", "peak hours", "peak time", "nighttime", "overnight",
              "evening speeds", "congestion at peak", "7pm", "19:00", "20:00", "night peak")
NIGHT_CHECK_SUBJECT = "[NIGHT CHECK]"

# Comment markers the workflow reads back (anti-loop, one-per-ticket rules).
MARKERS: Dict[str, Tuple[str, ...]] = {
    "path_analysis": ("Direct Path Analysis", "<!-- path-analyzer -->", "Network Bottleneck Analyzer"),
    "ihn_auto_return": ("Automated NOC Review — No Network Issue", "Automated NOC Review - No Network Issue"),
    "peak_retest": ("Peak Window Retest", "Peak retest at"),
    "rain_fade_readiness": ("Rain Fade Readiness", "<!-- rain-fade-readiness -->"),
    "correlated_auto_return": ("Correlated Upstream Issue — Auto-Returned", "Correlated Upstream Issue - Auto-Returned"),
    "re_evaluation": ("Re-evaluation — Returned from CS", "Re-evaluation - Returned from CS"),
}


def _term_re(term: str) -> "re.Pattern[str]":
    # A term must start a word ("rain" hits "raining", not "drain"); multi-word terms allow any whitespace run.
    body = r"\s+".join(re.escape(p) for p in term.split())
    lead = r"\b" if term[:1].isalnum() else ""
    return re.compile(lead + body, re.IGNORECASE)


_COMPILED: Dict[str, "re.Pattern[str]"] = {}


def hits(text: str, terms: Iterable[str]) -> List[str]:
    """The terms that occur in `text` (case-insensitive, word-start anchored), in list order."""
    text = text or ""
    found = []
    for term in terms:
        rx = _COMPILED.get(term)
        if rx is None:
            rx = _COMPILED[term] = _term_re(term)
        if rx.search(text):
            found.append(term)
    return found


def markers_in(text: str) -> List[str]:
    """Names of the MARKERS whose literal appears in `text` (exact, case-sensitive)."""
    text = text or ""
    return [name for name, literals in MARKERS.items() if any(lit in text for lit in literals)]
