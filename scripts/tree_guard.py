#!/usr/bin/env python3
"""Notice when the running checkout stops matching what was reviewed and committed (stdlib + git).

    python3 scripts/tree_guard.py check              # report drift; exit 1 when there is any
    python3 scripts/tree_guard.py check --alert      # also send one notification per new drift (timer mode)
    python3 scripts/tree_guard.py accept             # an operator acknowledges the current drift (stops the alerts for it)
    python3 scripts/tree_guard.py check --json

Drift is anything `git status` reports for this checkout that .gitignore does not cover: a tracked file that
was edited, staged or deleted, and a new file that is neither tracked nor ignored (an agent's scratch script, a
stray copy of a data file). The service runs from this tree, so an unreviewed edit here is live code that is not
in the repository: nobody reviewed it, CI never saw it, and the next deploy either refuses or overwrites it.

Only paths are ever printed or sent, never file contents. A change that keeps being edited re-alerts, because
the fingerprint covers the diff text; an acknowledged change does not alert again until it changes.

State: <state dir>/tree_guard.json  {"alerted": <fingerprint>, "accepted": <fingerprint>}
Env:   NOC_TREE_GUARD_REPO  checkout to inspect (default: the repository this script is in)

Exit codes: 0 clean (or acknowledged), 1 drift, 2 usage or not a git checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noc_config  # noqa: E402
import statefile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAX_LISTED = 15


def repo_dir() -> Path:
    override = os.environ.get("NOC_TREE_GUARD_REPO", "").strip()
    return Path(override) if override else ROOT


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in
           ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM")}
    return subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, env=env, check=False)


def drift(repo: Path) -> Optional[List[Tuple[str, str]]]:
    """[(code, path)] for every difference from the committed tree, or None when `repo` is not a git checkout."""
    r = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if r.returncode != 0:
        return None
    out: List[Tuple[str, str]] = []
    parts = r.stdout.split(b"\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2].decode("ascii", "replace"), entry[3:].decode("utf-8", "replace")
        if code[0] in "RC":          # renames and copies carry the old path as the next field
            i += 1
        out.append((code, path))
    return sorted(out, key=lambda t: t[1])


def fingerprint(repo: Path, entries: List[Tuple[str, str]]) -> str:
    """Stable id of this exact drift: the changed paths plus the diff text and the untracked files' sizes and times."""
    h = hashlib.sha256()
    for code, path in entries:
        h.update(f"{code} {path}\n".encode("utf-8", "replace"))
        if code == "??":
            try:
                st = (repo / path).stat()
                h.update(f"{st.st_size}:{int(st.st_mtime)}\n".encode())
            except OSError:
                pass
    h.update(_git(repo, "diff", "HEAD", "--binary").stdout)
    return h.hexdigest()


def state_path() -> Path:
    return noc_config.state_dir() / "tree_guard.json"


def load_state() -> Dict[str, str]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Dict[str, str]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with statefile.locked(path):
        statefile.atomic_write_text(path, json.dumps(state, indent=2) + "\n")


def describe(code: str) -> str:
    if code == "??":
        return "new file, not tracked and not ignored"
    if "D" in code:
        return "deleted"
    if code[0] != " " and code[1] == " ":
        return "staged"
    return "edited"


def message(repo: Path, entries: List[Tuple[str, str]]) -> str:
    lines = ["%d path(s) in %s differ from the committed code:" % (len(entries), repo)]
    for code, path in entries[:MAX_LISTED]:
        lines.append("  %s  (%s)" % (path, describe(code)))
    if len(entries) > MAX_LISTED:
        lines.append("  ... and %d more" % (len(entries) - MAX_LISTED))
    lines.append("Review with: git -C %s diff HEAD   Then commit it through a branch, discard it, or run "
                 "tree_guard.py accept." % repo)
    return "\n".join(lines)


def cmd_check(args: argparse.Namespace) -> int:
    repo = repo_dir()
    entries = drift(repo)
    if entries is None:
        print("error: %s is not a git checkout" % repo, file=sys.stderr)
        return 2
    state = load_state()
    fp = fingerprint(repo, entries) if entries else ""
    acknowledged = bool(entries) and state.get("accepted") == fp
    if args.json:
        print(json.dumps({"repo": str(repo), "clean": not entries, "acknowledged": acknowledged,
                          "paths": [{"path": p, "state": describe(c)} for c, p in entries]}, indent=2))
    elif not args.quiet:
        print("clean: the checkout matches the committed code" if not entries else message(repo, entries))
        if acknowledged:
            print("(acknowledged by an operator; no alert will be sent until it changes)")
    if args.alert:
        if entries and not acknowledged and state.get("alerted") != fp:
            import notify
            sent = notify.send("NocAgent: the running checkout was modified", message(repo, entries), level="warn")
            if sent:
                state["alerted"] = fp
                save_state(state)
        elif not entries and state.get("alerted"):
            state.pop("alerted", None)
            state.pop("accepted", None)
            save_state(state)
    return 0 if (not entries or acknowledged) else 1


def cmd_accept(_args: argparse.Namespace) -> int:
    if os.environ.get("NOC_UNATTENDED", "").strip() == "1":
        print("refused: tree_guard.py accept only runs when an operator asks (NOC_UNATTENDED=1 here).", file=sys.stderr)
        return 4
    repo = repo_dir()
    entries = drift(repo)
    if entries is None:
        print("error: %s is not a git checkout" % repo, file=sys.stderr)
        return 2
    if not entries:
        print("nothing to accept: the checkout is clean")
        return 0
    state = load_state()
    state["accepted"] = fingerprint(repo, entries)
    save_state(state)
    print("acknowledged %d path(s); alerts for this exact change are off until it changes again" % len(entries))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="report drift")
    p.add_argument("--alert", action="store_true", help="send one notification per new drift")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="print nothing (for the timer)")
    p.set_defaults(fn=cmd_check)
    sub.add_parser("accept", help="acknowledge the current drift").set_defaults(fn=cmd_accept)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
