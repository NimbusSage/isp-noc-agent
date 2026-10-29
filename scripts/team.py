#!/usr/bin/env python3
"""Who is working, and what they may do: the team roster and the current operator (stdlib only).

    python3 scripts/team.py whoami               # the current operator and role; exit 3 when not resolved
    python3 scripts/team.py can comment          # exit 0 when the current operator may, 1 when not
    python3 scripts/team.py list                 # the roster
    python3 scripts/team.py me <handle>          # remember your handle on this machine
    python3 scripts/team.py add <handle> --name "Full Name" [--role viewer|tech|lead]
    python3 scripts/team.py set-role <handle> <viewer|tech|lead>
    python3 scripts/team.py disable <handle>     # and: enable <handle>
    python3 scripts/team.py check                # validate the roster

The roster is site-pack/team.json (non-secret; it travels with the site pack, see scripts/pack.py):

    {"version": 1,
     "members": {"<handle>": {"name": "...", "role": "viewer|tech|lead", "active": true}},
     "pools": {"<deviceType>": {"scope": "shared|personal"}},
     "approved_models": ["<provider>/<model>"]}

Roles: viewer reads and analyses. tech also posts Sonar comments, tracker rows and calendar entries
as themselves. lead also changes devices and settings. Batch runs (the webhook server) are
`batch` and are governed by the gates in AGENTS.md, not by this file.

The operator is NOC_OPERATOR (environment, then .env), else the "operator" key of ~/.config/nocagent/user.json. With no
roster file (a single-person install) nothing is restricted. With a roster, an unknown or missing
operator can only read. On a person's own machine this is a guard against mistakes and against an
agent doing more than the person asked, not a security boundary: whoever controls the machine
controls the files. The credential broker on the central box enforces roles server-side.

Changing the roster refuses to run when NOC_UNATTENDED=1 (batch agents). Exit codes: 0 ok, 1 not
allowed or a check failed, 2 usage or invalid value, 3 operator or member not found, 4 refused (unattended).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import noc_config  # noqa: E402
import statefile  # noqa: E402

ROLES = ("viewer", "tech", "lead")
SCOPES = ("shared", "personal")
CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    "read": ("viewer", "tech", "lead"),
    "comment": ("tech", "lead"),
    "device_write": ("lead",),
    "admin": ("lead",),
}
HANDLE_RE = re.compile(r"^[a-z][a-z0-9._-]{1,30}$")
RESERVED = ("batch",)


class TeamError(Exception):
    """Invalid input; maps to exit code 2."""


# --- files --------------------------------------------------------------------

def roster_path() -> Path:
    override = os.environ.get("NOC_TEAM_FILE", "").strip()
    if override:
        return Path(override)
    pack = os.environ.get("NOC_SITE_PACK_DIR", "").strip()
    return (Path(pack) if pack else noc_config.profile_path().parent) / "team.json"


def user_config_path() -> Path:
    override = os.environ.get("NOC_USER_CONFIG", "").strip()
    return Path(override) if override else Path.home() / ".config" / "nocagent" / "user.json"


def load_roster() -> Tuple[Optional[dict], Optional[str]]:
    """(roster, None); (None, None) when there is no roster file; (None, problem) when it is unreadable."""
    path = roster_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, "%s: %s" % (path, e.strerror or e)
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, "%s is not valid JSON: %s" % (path, e)
    if not isinstance(data, dict) or not isinstance(data.get("members", {}), dict):
        return None, "%s must be an object with a \"members\" object" % path
    return data, None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(str(path), str(path) + ".bak")
    statefile.atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=0o600)


# --- identity -----------------------------------------------------------------

@dataclass
class Operator:
    handle: str = ""
    role: str = ""           # viewer|tech|lead, or batch / unmanaged / unknown / disabled
    name: str = ""
    source: str = ""         # where the handle came from
    problem: str = ""

    @property
    def known(self) -> bool:
        return self.role in ROLES or self.role in ("batch", "unmanaged")


def configured_handle() -> Tuple[str, str]:
    """(handle, source): NOC_OPERATOR, else ~/.config/nocagent/user.json, else ("", "")."""
    env = envfile.get("NOC_OPERATOR").strip()
    if env:
        return env, "NOC_OPERATOR"
    path = user_config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("operator", "")
    except (OSError, ValueError, AttributeError):
        value = ""
    if isinstance(value, str) and value.strip():
        return value.strip(), str(path)
    return "", ""


def current() -> Operator:
    handle, source = configured_handle()
    if handle == "batch" or handle.startswith("batch:"):
        return Operator(handle=handle, role="batch", name="batch run", source=source)
    roster, problem = load_roster()
    if problem:
        return Operator(handle=handle, role="unknown", source=source, problem=problem)
    if roster is None:
        return Operator(handle=handle, role="unmanaged", source=source)
    if not handle:
        return Operator(role="unknown", problem="no operator set: run  python3 scripts/team.py me <your-handle>")
    member = roster.get("members", {}).get(handle)
    if not isinstance(member, dict):
        return Operator(handle=handle, role="unknown", source=source,
                        problem="%s is not in the roster (%s)" % (handle, roster_path()))
    if member.get("active") is False:
        return Operator(handle=handle, role="disabled", name=str(member.get("name", "")), source=source,
                        problem="%s is disabled" % handle)
    role = member.get("role")
    if role not in ROLES:
        return Operator(handle=handle, role="unknown", source=source, problem="%s has an invalid role" % handle)
    return Operator(handle=handle, role=role, name=str(member.get("name", "")), source=source)


def allowed(capability: str, op: Optional[Operator] = None) -> bool:
    """May the current operator do this? Everything is allowed without a roster and for batch runs."""
    if capability not in CAPABILITIES:
        raise TeamError("unknown capability %r; known: %s" % (capability, ", ".join(CAPABILITIES)))
    op = op or current()
    if op.role in ("batch", "unmanaged"):
        return True
    if op.role in ROLES:
        return op.role in CAPABILITIES[capability]
    return capability == "read" and op.role == "unknown"


def require(capability: str) -> Optional[str]:
    """None when allowed, else the sentence to show the person."""
    op = current()
    if allowed(capability, op):
        return None
    who = "%s (%s)" % (op.handle, op.role) if op.handle else "this session"
    why = op.problem or "the %s role may not %s" % (op.role, capability.replace("_", " "))
    return "%s is not allowed to %s: %s. Ask a lead." % (who, capability.replace("_", " "), why)


def model_status(model: str, roster: Optional[dict] = None) -> str:
    """approved | unlisted | no-list for a model id, against the roster's approved_models."""
    if roster is None:
        roster = load_roster()[0]
    listed = [m for m in (roster or {}).get("approved_models", []) if isinstance(m, str)]
    if not listed:
        return "no-list"
    return "approved" if model.strip().lower() in {m.strip().lower() for m in listed} else "unlisted"


# --- validation ---------------------------------------------------------------

def problems(roster: dict) -> Tuple[List[str], List[str]]:
    """(failures, warnings) for a roster."""
    bad: List[str] = []
    warn: List[str] = []
    members = roster.get("members", {})
    for handle, m in members.items():
        if not HANDLE_RE.match(handle) or handle in RESERVED:
            bad.append("handle %r: lowercase letters, digits, . _ - (2-31 characters, starts with a letter), not reserved" % handle)
        if not isinstance(m, dict):
            bad.append("%s: entry must be an object" % handle)
            continue
        if m.get("role") not in ROLES:
            bad.append("%s: role must be one of %s" % (handle, ", ".join(ROLES)))
        if not isinstance(m.get("name"), str) or not m["name"].strip():
            bad.append("%s: name is required" % handle)
        if "active" in m and not isinstance(m["active"], bool):
            bad.append("%s: active must be true or false" % handle)
    if not any(isinstance(m, dict) and m.get("role") == "lead" and m.get("active", True) is not False
               for m in members.values()):
        warn.append("no active lead: nobody can change the roster or use device writes")
    pools = roster.get("pools", {})
    if not isinstance(pools, dict):
        bad.append("pools must be an object")
    else:
        for dtype, p in pools.items():
            if not isinstance(p, dict) or p.get("scope") not in SCOPES:
                bad.append("pools.%s.scope must be one of %s" % (dtype, ", ".join(SCOPES)))
    models = roster.get("approved_models", [])
    if not isinstance(models, list) or not all(isinstance(x, str) and x.strip() for x in models):
        bad.append("approved_models must be a list of model ids")
    return bad, warn


# --- commands -----------------------------------------------------------------

def _refuse_if_unattended(action: str) -> Optional[int]:
    if os.environ.get("NOC_UNATTENDED", "").strip() == "1":
        print("refused: %s changes who may do what, so it only runs when the operator asks "
              "(NOC_UNATTENDED=1 here). Return needs_human and tell the operator." % action, file=sys.stderr)
        return 4
    return None


def _load_for_edit() -> dict:
    roster, problem = load_roster()
    if problem:
        raise TeamError(problem)
    return roster if roster is not None else {"version": 1, "members": {}, "pools": {}, "approved_models": []}


def _active_leads(roster: dict) -> List[str]:
    return [h for h, m in roster.get("members", {}).items()
            if isinstance(m, dict) and m.get("role") == "lead" and m.get("active", True) is not False]


def _need_admin(roster: dict) -> Optional[int]:
    """Only a lead changes the roster; the very first lead can be added by anyone."""
    if not _active_leads(roster):
        return None
    msg = require("admin")
    if msg:
        print("refused: " + msg, file=sys.stderr)
        return 1
    return None


def cmd_whoami(_args: argparse.Namespace) -> int:
    op = current()
    print("handle: %s\nrole:   %s\nname:   %s\nfrom:   %s" % (op.handle or "-", op.role, op.name or "-", op.source or "-"))
    if op.problem:
        print("problem: " + op.problem, file=sys.stderr)
    return 0 if op.known else 3


def cmd_can(args: argparse.Namespace) -> int:
    try:
        ok = allowed(args.capability)
    except TeamError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if not ok:
        print(require(args.capability), file=sys.stderr)
    return 0 if ok else 1


def cmd_list(_args: argparse.Namespace) -> int:
    roster, problem = load_roster()
    if problem:
        print("error: " + problem, file=sys.stderr)
        return 2
    if roster is None:
        print("no roster (%s): single-person install, nothing is restricted" % roster_path())
        return 0
    me = configured_handle()[0]
    members = roster.get("members", {})
    for handle in sorted(members):
        m = members[handle] if isinstance(members[handle], dict) else {}
        flags = ("" if m.get("active", True) is not False else " disabled") + (" (you)" if handle == me else "")
        print("%-16s %-7s %s%s" % (handle, m.get("role", "?"), m.get("name", ""), flags))
    return 0


def cmd_me(args: argparse.Namespace) -> int:
    refused = _refuse_if_unattended("team.py me")
    if refused:
        return refused
    roster, problem = load_roster()
    if problem:
        print("error: " + problem, file=sys.stderr)
        return 2
    if roster is not None and not isinstance(roster.get("members", {}).get(args.handle), dict):
        print("error: %s is not in the roster; ask a lead to add you" % args.handle, file=sys.stderr)
        return 3
    path = user_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data["operator"] = args.handle
    path.parent.mkdir(parents=True, exist_ok=True)
    statefile.atomic_write_text(path, json.dumps(data, indent=2) + "\n", mode=0o600)
    print("saved: %s is your handle on this machine (%s). NOC_OPERATOR overrides it." % (args.handle, path))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    refused = _refuse_if_unattended("team.py add")
    if refused:
        return refused
    try:
        roster = _load_for_edit()
        if not HANDLE_RE.match(args.handle) or args.handle in RESERVED:
            raise TeamError("handle: lowercase letters, digits, . _ - (2-31 characters, starts with a letter), not 'batch'")
        if not args.name.strip():
            raise TeamError("--name is required")
        if args.handle in roster["members"]:
            raise TeamError("%s is already in the roster" % args.handle)
        if not _active_leads(roster) and args.role != "lead":
            raise TeamError("the first member must be a lead (--role lead); nobody could change the roster otherwise")
        denied = _need_admin(roster)
        if denied:
            return denied
        entry: Dict[str, Any] = {"name": args.name.strip(), "role": args.role, "active": True}
        for key in ("chat", "calendar"):
            if getattr(args, key):
                entry[key] = getattr(args, key)
        roster["members"][args.handle] = entry
        _write_json(roster_path(), roster)
    except TeamError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print("added %s as %s in %s" % (args.handle, args.role, roster_path()))
    return 0


def _change(args: argparse.Namespace, action: str, mutate) -> int:
    refused = _refuse_if_unattended("team.py " + action)
    if refused:
        return refused
    try:
        roster = _load_for_edit()
        member = roster["members"].get(args.handle)
        if not isinstance(member, dict):
            print("error: %s is not in the roster" % args.handle, file=sys.stderr)
            return 3
        denied = _need_admin(roster)
        if denied:
            return denied
        before = _active_leads(roster)
        mutate(member)
        after = _active_leads(roster)
        if before and not after:
            raise TeamError("that would leave no active lead; promote someone else first")
        _write_json(roster_path(), roster)
    except TeamError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print("%s: %s" % (action, args.handle))
    return 0


def cmd_set_role(args: argparse.Namespace) -> int:
    return _change(args, "set-role " + args.role, lambda m: m.__setitem__("role", args.role))


def cmd_disable(args: argparse.Namespace) -> int:
    return _change(args, "disable", lambda m: m.__setitem__("active", False))


def cmd_enable(args: argparse.Namespace) -> int:
    return _change(args, "enable", lambda m: m.__setitem__("active", True))


def cmd_check(_args: argparse.Namespace) -> int:
    roster, problem = load_roster()
    if problem:
        print("FAIL " + problem)
        return 1
    if roster is None:
        print("no roster (%s): single-person install, nothing to check" % roster_path())
        return 0
    bad, warn = problems(roster)
    for b in bad:
        print("FAIL " + b)
    for w in warn:
        print("WARN " + w)
    if not bad and not warn:
        n = len(roster.get("members", {}))
        print("ok: %d member(s), %d pool rule(s), %d approved model(s)" % (
            n, len(roster.get("pools", {}) or {}), len(roster.get("approved_models", []) or [])))
    return 1 if bad else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami", help="the current operator and role").set_defaults(fn=cmd_whoami)
    p = sub.add_parser("can", help="exit 0 when the current operator may do this")
    p.add_argument("capability", choices=sorted(CAPABILITIES))
    p.set_defaults(fn=cmd_can)
    sub.add_parser("list", help="show the roster").set_defaults(fn=cmd_list)
    p = sub.add_parser("me", help="remember your handle on this machine")
    p.add_argument("handle")
    p.set_defaults(fn=cmd_me)
    p = sub.add_parser("add", help="add a member (a lead does this)")
    p.add_argument("handle")
    p.add_argument("--name", required=True)
    p.add_argument("--role", choices=ROLES, default="viewer")
    p.add_argument("--chat", default="", help="chat handle, for mentions")
    p.add_argument("--calendar", default="", help="calendar name, for dispatch")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("set-role", help="change a member's role (a lead does this)")
    p.add_argument("handle")
    p.add_argument("role", choices=ROLES)
    p.set_defaults(fn=cmd_set_role)
    for name, fn in (("disable", cmd_disable), ("enable", cmd_enable)):
        p = sub.add_parser(name, help=name + " a member (a lead does this)")
        p.add_argument("handle")
        p.set_defaults(fn=fn)
    sub.add_parser("check", help="validate the roster").set_defaults(fn=cmd_check)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
