#!/usr/bin/env python3
"""Where this organization's own services live: one registry, one lookup rule (stdlib only).

    python3 scripts/resources.py list                  # every known resource, its value and where it came from
    python3 scripts/resources.py get uisp              # print the URL; exit 3 when it is not registered
    python3 scripts/resources.py get uisp --field host # just the host name
    python3 scripts/resources.py set uisp https://uisp.<your-domain>
    python3 scripts/resources.py migrate               # show what .env and older settings already hold
    python3 scripts/resources.py migrate --write       # register those values in site-pack/profile.json
    python3 scripts/resources.py check [--probe]       # validate the registry; --probe tries each URL without credentials

Values live in site-pack/profile.json under "resources" (no secrets). Lookup order, everywhere:
environment variable (real environment, then .env), then profile "resources", then an older
location (see scripts/resource_catalog.json). If two sources disagree the first wins and
`list` / `check` say so.

A resource that is not registered is not available. Ask the operator for it; never take an
address from an example in a skill, from another system's device list, or from DNS. Credentials
go only to a registered resource.

`set` and `migrate --write` change what the agent will trust, so they refuse to run when
NOC_UNATTENDED=1 (the webhook server sets it for batch agents). That is a guard against an agent
registering an address on its own, not a security boundary: the agent shares the operator's
account. Credentials go only to a registered resource: the MCP read-host policy enforces that for
host names, and the skills and AGENTS.md say the same for scripts.

Exit codes: 0 ok, 1 a check failed, 2 usage or invalid value, 3 not registered, 4 refused (unattended).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import noc_config  # noqa: E402

CATALOG_PATH = Path(__file__).resolve().parent / "resource_catalog.json"
SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")
NOT_REGISTERED = ("{name} is not registered. Ask the operator for its address and stop; do not take one from a skill "
                  "example, another system's device list or DNS. The operator registers it with: "
                  "python3 scripts/resources.py set {name} <value>")


class ResourceError(Exception):
    """Invalid input; maps to exit code 2."""


def catalog() -> Dict[str, dict]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return dict(data.get("resources") or {})


def site_pack_dir() -> Path:
    override = os.environ.get("NOC_SITE_PACK_DIR", "").strip()
    return Path(override) if override else noc_config.profile_path().parent


def unattended() -> bool:
    return os.environ.get("NOC_UNATTENDED", "").strip() == "1"


# --- value handling ----------------------------------------------------------

def placeholder_reason(value: str) -> Optional[str]:
    low = value.lower()
    if "<" in value or ">" in value:
        return "contains <...>, which marks a placeholder"
    if re.search(r"(^|[./@])example(\.|$)", low) or low.endswith((".invalid", ".test", ".example")):
        return "uses a reserved example domain"
    if "your-org" in low or "your_org" in low:
        return "looks like a template value"
    host = urllib.parse.urlsplit(value if "://" in value else "//" + value).hostname or ""
    if host.startswith(DOC_NETS):
        return "is in a documentation address range"
    return None


def normalize(value: str, kind: str) -> str:
    """Canonical form of a resource value, or ResourceError."""
    text = (value or "").strip()
    if not text:
        raise ResourceError("empty value")
    if kind == "sheet":
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
        text = m.group(1) if m else text
        if not SHEET_ID_RE.match(text):
            raise ResourceError("not a Google spreadsheet id (20-100 letters, digits, - or _) or sheet URL")
        return text
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ResourceError("not a http(s) URL or host name")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ResourceError("a resource URL must not carry credentials, a query or a fragment")
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    if port in ((443 if parts.scheme == "https" else 80),):
        port = None                     # default port: same canonical form as scripts/resources.mjs
    netloc = host + (":%d" % port if port else "")
    return "%s://%s%s" % (parts.scheme, netloc, parts.path.rstrip("/"))


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


# --- resolution --------------------------------------------------------------

@dataclass
class Resolved:
    name: str
    kind: str
    purpose: str
    value: str = ""
    source: str = ""
    others: List[Tuple[str, str]] = field(default_factory=list)   # disagreeing sources
    problems: List[str] = field(default_factory=list)
    credentials: Dict[str, bool] = field(default_factory=dict)
    mcp_device_type: str = ""

    @property
    def conflict(self) -> bool:
        return bool(self.others)


def _legacy_values(entry: dict, profile: dict, pack: Optional[Path] = None) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for leg in entry.get("legacy") or []:
        if "profile" in leg:
            v = noc_config.profile_get(profile, leg["profile"], "")
            where = "legacy profile " + ".".join(leg["profile"])
        else:
            where = "legacy file %s" % leg["file"]
            try:
                v = json.loads(((pack or site_pack_dir()) / leg["file"]).read_text(encoding="utf-8")).get(leg["key"], "")
            except (OSError, ValueError, AttributeError):
                v = ""
        if isinstance(v, str) and v.strip():
            out.append((where, v))
    return out


def resolve(name: str, profile: Optional[dict] = None, getenv: Optional[Callable[[str], str]] = None,
            pack: Optional[Path] = None) -> Resolved:
    """Resolve one resource. `getenv(name)` returns "" when unset (default: real environment, then .env)."""
    getenv = getenv or envfile.get
    profile = noc_config.load_profile() if profile is None else profile
    cat = catalog()
    registered = (profile.get("resources") or {}) if isinstance(profile.get("resources"), dict) else {}
    entry = cat.get(name)
    custom = registered.get(name) if isinstance(registered.get(name), dict) else None
    if entry is None:
        if custom is None:
            raise ResourceError("unknown resource %r; known: %s" % (name, ", ".join(sorted(set(cat) | set(registered)))))
        entry = {"kind": custom.get("kind", "url"), "purpose": custom.get("purpose", "registered by the operator"),
                 "env": [], "legacy": [], "credential": []}
    kind = entry.get("kind", "url")
    res = Resolved(name=name, kind=kind, purpose=entry.get("purpose", ""),
                   credentials={c: bool(getenv(c)) for c in entry.get("credential") or []},
                   mcp_device_type=entry.get("mcp_device_type", ""))
    raw: List[Tuple[str, str]] = []
    for var in entry.get("env") or []:
        v = getenv(var)
        if v:
            raw.append(("env " + var, v))
    if custom is not None:
        v = custom.get("id" if kind == "sheet" else "url")
        if isinstance(v, str) and v.strip():
            raw.append(("profile", v))
    raw += _legacy_values(entry, profile, pack)
    seen: List[Tuple[str, str]] = []
    for src, v in raw:
        try:
            seen.append((src, normalize(v, kind)))
        except ResourceError as e:
            res.problems.append("%s: %s" % (src, e))
    if seen:
        res.source, res.value = seen[0]
        res.others = [(s, v) for s, v in seen[1:] if v != res.value]
    return res


def resolve_all(profile: Optional[dict] = None, getenv: Optional[Callable[[str], str]] = None,
                pack: Optional[Path] = None) -> List[Resolved]:
    profile = noc_config.load_profile() if profile is None else profile
    reg = profile.get("resources") if isinstance(profile.get("resources"), dict) else {}
    names = list(catalog()) + sorted(n for n, v in reg.items() if n not in catalog() and isinstance(v, dict) and not n.startswith("_"))
    return [resolve(n, profile, getenv, pack) for n in names]


def get_url(name: str) -> str:
    """For other scripts: the registered URL (or id), or noc_config.ConfigError."""
    res = resolve(name)
    if not res.value:
        raise noc_config.ConfigError(NOT_REGISTERED.format(name=name))
    return res.value


# --- writing the profile -----------------------------------------------------

def write_profile(profile: dict) -> Path:
    path = noc_config.profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        shutil.copy2(path, str(path) + ".bak")
    fd, tmp = tempfile.mkstemp(prefix=".profile-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def register(profile: dict, name: str, value: str, kind: str) -> None:
    reg = profile.setdefault("resources", {})
    if not isinstance(reg, dict):
        raise ResourceError("profile 'resources' is not an object")
    entry = reg.get(name) if isinstance(reg.get(name), dict) else {}
    entry["id" if kind == "sheet" else "url"] = value
    if name not in catalog():
        entry["kind"] = kind
    reg[name] = entry


# --- devices.md hints --------------------------------------------------------

def device_table_hints() -> Dict[str, str]:
    """name -> address from a row of site-pack/devices.md whose device or role mentions it.
    A hint only: scheme, port and host name are unknown, so it is never written automatically."""
    out: Dict[str, str] = {}
    try:
        lines = (site_pack_dir() / "devices.md").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    cat = catalog()
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or set(cells[0]) <= set("-: "):
            continue
        address = next((c for c in cells[1:] if IPV4_RE.match(c)), "")
        if not address:
            continue
        head = (cells[0] + " " + cells[1]).lower()
        for name, entry in cat.items():
            if name not in out and any(h in head for h in entry.get("hint_names") or []):
                out[name] = address
    return out


# --- commands ----------------------------------------------------------------

def _refuse_if_unattended(action: str) -> Optional[int]:
    if unattended():
        print("refused: %s changes which services the agent trusts, so it only runs when the operator asks "
              "(NOC_UNATTENDED=1 here). Return needs_human and tell the operator." % action, file=sys.stderr)
        return 4
    return None


def cmd_list(args: argparse.Namespace) -> int:
    rows = resolve_all()
    if args.json:
        print(json.dumps([{"name": r.name, "kind": r.kind, "value": r.value or None, "source": r.source or None,
                           "conflict": [{"source": s, "value": v} for s, v in r.others],
                           "credentials": r.credentials, "problems": r.problems} for r in rows], indent=2))
        return 0
    width = max(len(r.name) for r in rows)
    vwidth = max([len(r.value) for r in rows if r.value] + [14])
    swidth = max([len(r.source) for r in rows] + [6])
    print("%-*s  %-*s  %-*s  %s" % (width, "NAME", vwidth, "VALUE", swidth, "SOURCE", "CREDENTIAL (presence only)"))
    for r in rows:
        creds = ", ".join("%s: %s" % (k, "set" if v else "missing") for k, v in r.credentials.items()) or "-"
        print("%-*s  %-*s  %-*s  %s" % (width, r.name, vwidth, r.value or "NOT REGISTERED", swidth, r.source or "-", creds))
        for src, val in r.others:
            print("%-*s  ^ also %s says %s (ignored; run `check`)" % (width, "", src, val))
        for p in r.problems:
            print("%-*s  ^ unusable value: %s" % (width, "", p))
    print("\nA NOT REGISTERED resource is not available: ask the operator for it. Never use an address from a skill "
          "example or another system's device list.")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    try:
        res = resolve(args.name)
    except ResourceError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not res.value:
        print(NOT_REGISTERED.format(name=args.name), file=sys.stderr)
        return 3
    print(host_of(res.value) if args.field == "host" and res.kind == "url" else res.value)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    refused = _refuse_if_unattended("`set`")
    if refused:
        return refused
    if not NAME_RE.match(args.name):
        print("name must be lowercase letters, digits or _ (at most 41 characters)", file=sys.stderr)
        return 2
    cat = catalog()
    kind = cat[args.name]["kind"] if args.name in cat else args.kind
    try:
        value = normalize(args.value, kind)
    except ResourceError as e:
        print("invalid value: %s" % e, file=sys.stderr)
        return 2
    why = placeholder_reason(value)
    if why:
        print("refusing %r: it %s. Register the real address." % (value, why), file=sys.stderr)
        return 2
    profile = noc_config.load_profile()
    try:
        register(profile, args.name, value, kind)
        path = write_profile(profile)
    except (ResourceError, OSError) as e:
        print("could not write the profile: %s" % e, file=sys.stderr)
        return 2
    print("registered %s = %s in %s" % (args.name, value, path.name))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    if args.write:
        refused = _refuse_if_unattended("`migrate --write`")
        if refused:
            return refused
    profile = noc_config.load_profile()
    hints = device_table_hints()
    plan: List[Tuple[str, str, str, str]] = []   # name, value, source, action
    for name in catalog():
        res = resolve(name, profile)
        if res.value and res.source == "profile":
            plan.append((name, res.value, res.source, "already registered"))
        elif res.value:
            why = placeholder_reason(res.value)
            plan.append((name, res.value, res.source, "SKIP: " + why if why else "register"))
        elif name in hints:
            plan.append((name, hints[name], "devices.md row", "suggestion only: confirm the real URL, then `set`"))
        else:
            plan.append((name, "", "-", "nothing found: ask the operator, then `set`"))
    width = max(len(p[0]) for p in plan)
    for name, value, source, action in plan:
        print("%-*s  %-40s  %-24s  %s" % (width, name, value or "-", source, action))
    todo = [p for p in plan if p[3] == "register"]
    if not args.write:
        print("\n%d value(s) would be registered. Nothing was written; add --write to register them." % len(todo))
        return 0
    for name, value, _src, _a in todo:
        register(profile, name, value, catalog()[name]["kind"])
    if todo:
        path = write_profile(profile)
        print("\nregistered %d value(s) in %s (previous file kept as %s.bak)" % (len(todo), path.name, path.name))
    else:
        print("\nnothing to register")
    return 0


def _probe(url: str) -> str:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "NocAgent-resources/1"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:   # no credentials are ever sent
            return "reachable (HTTP %d)" % resp.status
    except urllib.error.HTTPError as e:
        return "reachable (HTTP %d)" % e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "NOT reachable: %s" % getattr(e, "reason", e)


def cmd_check(args: argparse.Namespace) -> int:
    failures = 0
    for r in resolve_all():
        status = "ok" if r.value else "unset"
        notes: List[str] = []
        if r.value:
            why = placeholder_reason(r.value)
            if why:
                status = "FAIL"
                notes.append("value %s" % why)
        for p in r.problems:
            status = "FAIL"
            notes.append("unusable: " + p)
        if r.conflict:
            status = "FAIL"
            notes += ["%s says %s" % (s, v) for s, v in r.others]
        if r.value and r.source.startswith(("env", "legacy")):
            notes.append("not in the profile yet: run `migrate --write`")
        if args.probe and r.value and r.kind == "url" and status != "FAIL":
            notes.append(_probe(r.value))
        failures += status == "FAIL"
        print("[%s] %-16s %s%s" % (status.upper() if status != "FAIL" else "FAIL", r.name, r.value or "not registered",
                                  ("  (" + "; ".join(notes) + ")") if notes else ""))
    print("resources check:", "OK" if not failures else "%d problem(s)" % failures)
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="show every resource and where its value came from")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("get", help="print one resource; exit 3 when it is not registered")
    p.add_argument("name")
    p.add_argument("--field", choices=("url", "host"), default="url")
    p = sub.add_parser("set", help="register a resource (operator only)")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--kind", choices=("url", "sheet"), default="url", help="for a name that is not in the catalog")
    p = sub.add_parser("migrate", help="collect values from .env and older settings")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("check", help="validate the registry")
    p.add_argument("--probe", action="store_true", help="also try each URL without credentials")
    args = ap.parse_args(argv)
    return {"list": cmd_list, "get": cmd_get, "set": cmd_set, "migrate": cmd_migrate, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
