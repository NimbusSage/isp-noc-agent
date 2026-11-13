#!/usr/bin/env python3
"""agent_identity.py - what THIS install's agent calls itself.

The same code runs as a company hub's agent and as one tech's own assistant. The name
shows wherever the agent writes its own name - the tech column of a tracker row, the
weekly metrics card - so a reader can tell which machine produced a row or a report.
Two agents in one organization must never be confused for each other.

It belongs to the install, not to the organization: it lives in `.env` and is never
written into `site-pack/`, which every member of the team shares.

    import agent_identity
    agent_identity.name()          # "NOC Agent" unless AGENT_NAME says otherwise

Env: AGENT_NAME (the real environment first, then `.env`; see scripts/envfile.py).
Default: "NOC Agent". The value lands in HTML (a Chat card) and in a spreadsheet cell,
so control characters and angle brackets are replaced with spaces, runs of whitespace
collapse, and anything past 60 characters is dropped. An empty or unusable value falls
back to the default instead of failing a run over a cosmetic setting.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

DEFAULT_NAME = "NOC Agent"
MAX_LEN = 60

# Control characters and the two characters that would change markup in a Chat card.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>]")


def sanitize(raw: str) -> str:
    """Reduce arbitrary text to a name that is safe in HTML and a sheet cell."""
    text = " ".join(_UNSAFE.sub(" ", raw or "").split())
    return text[:MAX_LEN].strip() or DEFAULT_NAME


def name(env: dict = None) -> str:
    """This install's agent name. Pass `env` in tests; production reads env then .env."""
    raw = env.get("AGENT_NAME", "") if env is not None else envfile.get("AGENT_NAME")
    return sanitize(raw)


if __name__ == "__main__":
    print(name())
