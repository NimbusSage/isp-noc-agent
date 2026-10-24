#!/usr/bin/env python3
"""
classifier.py — Deterministic baseline classifier for evals.

The eval harness compares two things:
    1. This deterministic classifier vs golden labels (baseline floor — must stay stable)
    2. (optional --replay) The real LLM agent vs golden labels (drift check)

Keeping (1) in code means any regression in the harness itself is separable from
regressions in the LLM's behavior.
"""

import re

# Word boundaries matter: "ice" used to match inside "service", "lag" inside
# "flag", etc. This keeps the classifier honest and the example set at 100%.
WEATHER = r"\b(?:rain|rain fade|storm|weather|weather fade|precipitation|wet weather|bad weather|thunder|lightning|wind damage|snow|ice)\b"
SPEED = r"\b(?:slow|speeds|bandwidth|lag|buffering|latency|btest)\b"
OUTAGE = r"\b(?:outage|offline|down|no internet|cant connect|can't connect|no connection|unreachable)\b"
NIGHT = r"\b(?:night check|n check|peak hours|peak time|nighttime|overnight|evening speeds|congestion at peak|night peak)\b|\b(?:7pm|19:00|20:00)\b"
PBS_OK = r"garbage collect datastore 'backups' successful"
OOO = r"\b(?:out of office|automatic reply|auto[- ]?reply|of office)\b"
MAINT = r"\b(?:scheduled maintenance|maintenance)\b"
DUP = r"\b(?:dupe|duplicate of)\b"


def classify(subject: str, description: str = "") -> dict:
    text = f"{subject or ''} {description or ''}".lower()
    s = (subject or "").lower()

    if re.search(PBS_OK, s):
        return {"classification_category": "passthrough", "expected_status": "auto_closed",
                "signals": ["pbs_success"], "confidence": 99}
    if re.search(OOO, s):
        return {"classification_category": "passthrough", "expected_status": "auto_closed_ooo",
                "signals": ["ooo_subject"], "confidence": 95}
    if re.search(MAINT, s):
        return {"classification_category": "passthrough", "expected_status": "auto_closed_maintenance",
                "signals": ["maintenance_subject"], "confidence": 85}
    if re.search(WEATHER, text):
        return {"classification_category": "rain_fade", "expected_status": "needs_human",
                "signals": ["weather_keywords"], "confidence": 80}
    if re.search(NIGHT, text):
        return {"classification_category": "peak_congestion", "expected_status": "queued_for_peak",
                "signals": ["night_keywords"], "confidence": 75}
    if re.search(OUTAGE, text):
        return {"classification_category": "outage", "expected_status": "categorized",
                "signals": ["outage_keywords"], "confidence": 70}
    if re.search(SPEED, text):
        return {"classification_category": "slow_speeds", "expected_status": "categorized",
                "signals": ["speed_keywords"], "confidence": 70}
    return {"classification_category": "other", "expected_status": "categorized",
            "signals": [], "confidence": 40}
