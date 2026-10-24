#!/usr/bin/env python3
"""doctor.py - verify a NocAgent installation. Read-only unless you pass --notify.

Nothing is written and nothing is sent: every check reads files, runs read-only
commands or makes read-only API calls. The single exception is a clearly labeled
test message to the configured notification provider (Google Chat unless
NOTIFY_PROVIDER names another), and only when you pass --notify (incoming webhooks have
no ping verb). Secret values are never printed; findings name the KEY only.

Tiers (a ladder - tier N is ready when every required check of tiers 0..N passes):
  0  demo      python3, node, curl and server dependencies: `python3 scripts/demo.py`
  1  devices   MCP server built, device credential store, harness registration
               (LibreNMS and MyMate are optional here)
  2  Sonar     Sonar API + site-pack/profile.json ids (Google Chat, the pipeline
               server and systemd are optional here)
  3  Google    OAuth files for Calendar / Tracker Sheet / Temp OSPF Sheet skills

The last line of a normal run is `ready tier: N` (or `ready tier: none`).

Output: human table by default, machine JSON with --json (consumed by the
onboarding wizard skill). Exit codes: 0 = no FAIL, 1 = any FAIL, 2 = usage error.
Every FAIL/WARN row carries a one-line `fix:` (JSON key `hint`).

Usage:
  python3 scripts/doctor.py                        # all tiers
  python3 scripts/doctor.py --tier 0               # only what the demo needs
  python3 scripts/doctor.py --only sonar_api,mcp_store   # unknown names: exit 2
  python3 scripts/doctor.py --json
  python3 scripts/doctor.py --skip mymate
  python3 scripts/doctor.py --notify               # also post ONE test message to the notification provider (Google Chat by default)
  python3 scripts/doctor.py --server               # also check the ticket webhook server settings
  python3 scripts/doctor.py --discover-ids         # list Sonar groups/categories, print a profile fragment
  python3 scripts/doctor.py --discover-ids --write-profile   # create site-pack/profile.json if absent
"""

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

INSECURE = ssl.create_default_context()
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE

TIER_LABELS = {0: "demo", 1: "devices", 2: "Sonar + notifications", 3: "Google/OAuth skills"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
OAUTH_KEYS = ["CALENDAR_OAUTH_FILE", "TRACKER_OAUTH_FILE", "TEMP_OSPF_OAUTH_FILE"]
NEUTRAL_SONAR_HOST = "your-org.sonar.software"

RESULTS = []
ENV = {}
ENV_EXISTS = False
_PARSED_ENV = {}
_SKIP = set()
ARGS = None
_CTX = {"check": None, "tier": None, "required": False}


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def load_env(path=None):
    """(env dict, file exists). Parsed with the repo's safe .env parser."""
    p = Path(path or (ROOT / ".env"))
    if not p.is_file():
        return {}, False
    return envfile.load(p), True


def one_line(text):
    return " ".join(str(text).split())


def add(name, status, detail, hint=None, tier=None, required=None):
    t = _CTX["tier"] if tier is None else tier
    req = _CTX["required"] if required is None else required
    hint = one_line(hint) if hint else None
    if status in ("fail", "warn") and not hint:
        hint = f"re-run for detail: python3 scripts/doctor.py --only {_CTX['check'] or name}"
    RESULTS.append({"name": name, "status": status, "detail": one_line(detail), "hint": hint,
                    "tier": t, "required": bool(req) if t is not None else False})


def env_val(environ, key):
    """Env is authoritative if already exported (systemd), else from the .env parse."""
    v = environ.get(key)
    if v is not None and v.strip():
        return v.strip()
    v = ENV.get(key)
    return v.strip() if v is not None and v.strip() else None


_PLACEHOLDER_FRAGMENTS = ("changeme", "change_me", "change-me", "your-", "your_", "example", "xxxx",
                          "replace_me", "replace-me", "...")


def is_placeholder(value):
    """True for empty values and the stand-ins shipped in .env.example / profile.example.json."""
    s = (value or "").strip().strip("'\"").strip()
    if not s:
        return True
    if re.search(r"<[^<>\n]*>", s):
        return True
    low = s.lower()
    return any(f in low for f in _PLACEHOLDER_FRAGMENTS)


def is_placeholder_url(value):
    """URLs may legitimately contain 'example' (e.g. a lab host), so test only the obvious stand-ins."""
    s = (value or "").strip()
    return (not s) or bool(re.search(r"<[^<>\n]*>", s)) or "..." in s or NEUTRAL_SONAR_HOST in s


def _opener(url, insecure=False):
    host = urllib.parse.urlsplit(url).hostname or ""
    handlers = []
    if host in LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))  # never proxy a local server
    if insecure:
        handlers.append(urllib.request.HTTPSHandler(context=INSECURE))
    return urllib.request.build_opener(*handlers)


def http_json(method, url, headers=None, body=None, timeout=15, insecure=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json", **(headers or {})})
    with _opener(url, insecure).open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)


def run_cmd(argv, timeout=30, cwd=None):
    """Subprocess with parsed .env merged in (values never revealed)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          env={**os.environ, **_PARSED_ENV})


def _git(args, timeout=10):
    return subprocess.run(["git", "-C", str(ROOT)] + list(args), capture_output=True, text=True, timeout=timeout)


def _own_git_repo():
    """True when ROOT is itself a git checkout (not a directory inside some other repo)."""
    try:
        res = _git(["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and Path(res.stdout.strip()).resolve() == ROOT.resolve()


def profile_path():
    override = os.environ.get("NOC_PROFILE_FILE", "").strip()
    return Path(override) if override else ROOT / "site-pack" / "profile.json"


def load_profile():
    """(dict or None, error or None). Missing file is (None, None)."""
    p = profile_path()
    if not p.is_file():
        return None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, str(e)
    return (data, None) if isinstance(data, dict) else (None, "top level is not a JSON object")


def sonar_base(env):
    """(base url, where it came from). SONAR_BASE_URL first, like scripts/gql_client.py, then the profile."""
    u = env_val(env, "SONAR_BASE_URL")
    if u and not is_placeholder_url(u):
        return u.rstrip("/"), "SONAR_BASE_URL"
    prof, _ = load_profile()
    u = ((prof or {}).get("org") or {}).get("sonar_base_url")
    if isinstance(u, str) and not is_placeholder_url(u):
        return u.strip().rstrip("/"), "site-pack/profile.json"
    return None, None


def server_in_scope(env):
    """The pipeline server is optional: check it when asked, or when its auth token is configured."""
    if ARGS is not None and (ARGS.server or {"webhook_server", "systemd"} & set(_only_names(ARGS))):
        return True
    return not is_placeholder(env_val(env, "TICKET_WEBHOOK_AUTH_TOKEN"))


def _only_names(args):
    return [s.strip() for s in args.only.split(",") if s.strip()]


# --------------------------------------------------------------------------
# tier 0: demo
# --------------------------------------------------------------------------

def check_python(env):
    v = sys.version_info
    ok = v >= (3, 9)
    add("python", "pass" if ok else "fail", f"Python {v.major}.{v.minor}.{v.micro}" + ("" if ok else " (need >= 3.9)"),
        None if ok else "install Python 3.9 or newer (python.org or your package manager), then run: python3.11 scripts/doctor.py")


def _node_version():
    """((major, minor, patch) or None, node found?)."""
    node = shutil.which("node")
    if not node:
        return None, False
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", out)
        return (tuple(int(x) for x in m.groups()) if m else None), True
    except (OSError, subprocess.SubprocessError):
        return None, True


def check_node(env):
    ver, found = _node_version()
    install = "install Node.js 20 LTS from https://nodejs.org (or your package manager) and make sure `node` is on PATH"
    if not found:
        add("node", "fail", "node is not on PATH", install)
    elif ver is None:
        add("node", "fail", "could not run `node --version`", install)
    elif ver[0] < 18:
        add("node", "fail", f"Node {ver[0]}.{ver[1]}.{ver[2]} is too old (supported >= 20, demo needs >= 18)", install)
    elif ver[0] < 20:
        add("node", "warn", f"Node {ver[0]}.{ver[1]}.{ver[2]} works but supported >= 20 (production may run 18)",
            "upgrade to Node.js 20 LTS when convenient: https://nodejs.org")
    else:
        add("node", "pass", f"Node {ver[0]}.{ver[1]}.{ver[2]}")


def check_demo(env):
    """What scripts/demo.py needs before it will start."""
    missing, fixes = [], []
    if not shutil.which("curl"):
        missing.append("curl")
        fixes.append("install curl (apt install curl / brew install curl)")
    if not (ROOT / "server" / "node_modules" / "express" / "package.json").exists():
        missing.append("server/node_modules")
        fixes.append("cd server && npm install")
    for rel in ("scripts/demo.py", "server/index.js"):
        if not (ROOT / rel).is_file():
            missing.append(rel)
            fixes.append(f"restore {rel} from a fresh git clone")
    if missing:
        add("demo", "fail", "demo prerequisites missing: " + ", ".join(missing), "; ".join(fixes))
    else:
        add("demo", "pass", "curl, server dependencies and scripts/demo.py present: python3 scripts/demo.py")


def check_git_hooks(env):
    if not _own_git_repo():
        add("git_hooks", "unconfigured", "not a git checkout, so there are no hooks to install")
        return
    try:
        res = _git(["config", "--get", "core.hooksPath"])
    except (OSError, subprocess.SubprocessError) as e:
        add("git_hooks", "warn", f"could not read git config: {e}", "python3 scripts/harness_setup.py --hooks")
        return
    path = res.stdout.strip() if res.returncode == 0 else ""
    if path == ".githooks":
        add("git_hooks", "pass", "core.hooksPath = .githooks (secret scan runs before commit/push)")
    elif (ROOT / ".githooks").is_dir():
        add("git_hooks", "warn", "git hooks are not installed (the secret scan will not run before commits)",
            "python3 scripts/harness_setup.py --hooks")
    else:
        add("git_hooks", "unconfigured", ".githooks directory not present")


# --------------------------------------------------------------------------
# environment values
# --------------------------------------------------------------------------

def _bad_keys(env, keys):
    return [k for k in keys if is_placeholder(env_val(env, k))]


def check_env_required(env):
    """.env present and the required values real (names only, never values)."""
    core = ("SONAR_TOKEN", "NOC_MCP_PASSPHRASE")
    if ENV_EXISTS:
        add("env_file", "pass", ".env found")
    elif any(env.get(k, "").strip() for k in core):
        add("env_file", "pass", "no .env file; using values from the process environment")
    else:
        add("env_file", "fail", ".env not found",
            "cp .env.example .env, then fill in the keys doctor names (or run the onboarding wizard: `onboard me`)")

    bad = _bad_keys(env, ["NOC_MCP_PASSPHRASE"])
    if bad:
        add("env_devices", "fail", f"empty, unset or still a placeholder: {', '.join(bad)}",
            "set NOC_MCP_PASSPHRASE in .env to a long passphrase you choose; it encrypts the device credential store (keep a copy in your password manager)",
            tier=1, required=True)
    else:
        add("env_devices", "pass", "device credential passphrase is set (NOC_MCP_PASSPHRASE)", tier=1, required=True)

    bad = _bad_keys(env, ["SONAR_TOKEN"])
    if bad:
        add("env_required", "fail", f"empty, unset or still a placeholder: {', '.join(bad)}",
            "create a Sonar API key (Sonar > Admin > Security > API Keys) and put it in .env as SONAR_TOKEN",
            tier=2, required=True)
    else:
        add("env_required", "pass", "required Sonar values are set (SONAR_TOKEN)", tier=2, required=True)

    if not server_in_scope(env):
        add("env_server", "unconfigured",
            "pipeline server not in scope (TICKET_WEBHOOK_AUTH_TOKEN is unset or a placeholder)",
            "only needed to run the ticket webhook server: set TICKET_WEBHOOK_AUTH_TOKEN in .env and re-run with --server",
            tier=2, required=False)
        return
    harness = (env_val(env, "AGENT_HARNESS") or "opencode").lower()
    keys = ["TICKET_WEBHOOK_AUTH_TOKEN"]
    if harness == "opencode":
        keys += ["OPENCODE_PATH", "OPENCODE_WORKSPACE"]  # only the opencode harness needs these
    elif harness == "command":
        keys += ["AGENT_COMMAND_TEMPLATE"]
    elif harness != "claude":
        add("env_server", "fail", "AGENT_HARNESS is not one of: opencode, claude, command",
            "set AGENT_HARNESS in .env to opencode, claude or command", tier=2, required=False)
        return
    bad = _bad_keys(env, keys)
    if bad:
        add("env_server", "fail", f"empty, unset or still a placeholder: {', '.join(bad)}",
            "set each listed key in .env (see the comments in .env.example for what each one is)",
            tier=2, required=False)
        return
    broken = []
    if harness == "opencode":
        exe, work = env_val(env, "OPENCODE_PATH"), env_val(env, "OPENCODE_WORKSPACE")
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            broken.append("OPENCODE_PATH (not an executable file)")
        if not os.path.isdir(work):
            broken.append("OPENCODE_WORKSPACE (not a directory)")
    if broken:
        add("env_server", "fail", f"set but not usable: {', '.join(broken)}",
            "point OPENCODE_PATH at the opencode binary (`which opencode`) and OPENCODE_WORKSPACE at an existing directory",
            tier=2, required=False)
    else:
        add("env_server", "pass", f"pipeline server settings are set ({', '.join(keys)})", tier=2, required=False)


# --------------------------------------------------------------------------
# tier 1: devices
# --------------------------------------------------------------------------

def check_mcp_build(env):
    entry = ROOT / "mcp-server" / "dist" / "index.js"
    if entry.is_file():
        add("mcp_build", "pass", "mcp-server/dist/index.js is built")
    else:
        add("mcp_build", "fail", "mcp-server is not built (dist/index.js missing)", "cd mcp-server && npm ci && npm run build")


def check_mcp_store(env):
    enc = ROOT / "mcp-server" / "config" / "credentials.enc"
    dist = ROOT / "mcp-server" / "dist"
    pw = env_val(env, "NOC_MCP_PASSPHRASE")
    if is_placeholder(pw):
        add("mcp_store", "unconfigured", "NOC_MCP_PASSPHRASE not set (or a placeholder)",
            "set NOC_MCP_PASSPHRASE in .env; it is needed to decrypt device credentials")
        return
    if not enc.exists():
        add("mcp_store", "unconfigured", "credentials.enc does not exist yet",
            "cp mcp-server/config/credentials.example.json mcp-server/config/credentials.json, edit it, then: cd mcp-server && npm run encrypt -- config/credentials.json")
        return
    if not dist.exists():
        add("mcp_store", "warn", "mcp-server not built (dist/ missing), so decryption cannot be verified",
            "cd mcp-server && npm ci && npm run build")
        return
    probe = ("const {CredentialStore} = await import(process.argv[1]);"
             "const s = new CredentialStore(process.argv[2], process.env.NOC_MCP_PASSPHRASE);"
             "const d = await s.load();"
             "console.log('decrypt-ok devicetypes=' + (d.deviceTypes ? d.deviceTypes.length : 0));")
    try:
        proc = run_cmd(["node", "--input-type=module", "-e", probe, (dist / "credentials" / "store.js").as_uri(), str(enc)],
                       timeout=30, cwd=str(ROOT / "mcp-server"))
        ok = "decrypt-ok" in proc.stdout
        add("mcp_store", "pass" if ok else "fail",
            proc.stdout.strip() or (proc.stderr.strip().splitlines()[-1][:120] if proc.stderr.strip() else "decrypt failed"),
            None if ok else "NOC_MCP_PASSPHRASE does not match credentials.enc: fix the passphrase in .env, or re-encrypt: cd mcp-server && npm run encrypt -- config/credentials.json")
    except Exception as e:
        add("mcp_store", "fail", str(e), "make sure `node` works (node --version) and mcp-server is built")


HARNESS_BINS = {"opencode": ("OPENCODE_PATH", "opencode"), "claude": ("CLAUDE_PATH", "claude"), "codex": (None, "codex"),
                "gemini": (None, "gemini"), "cursor": (None, "cursor-agent"), "vscode": (None, "code")}


def _have_binary(env, harness):
    key, exe = HARNESS_BINS.get(harness, (None, None))
    if key:
        p = env_val(env, key)
        if p and not is_placeholder(p) and os.path.isfile(p) and os.access(p, os.X_OK):
            return True
    return bool(exe and shutil.which(exe))


def check_harness(env):
    """Summarize `harness_setup.py --check --json` for the coding-agent CLI in use."""
    script = Path(__file__).resolve().parent / "harness_setup.py"
    wanted = (ARGS.harness if ARGS is not None and ARGS.harness else None) or (env_val(env, "AGENT_HARNESS") or "").lower()
    if wanted not in HARNESS_BINS:
        wanted = next((h for h in ("claude", "opencode", "codex", "gemini") if _have_binary(env, h)), "")
    if not wanted:
        add("harness", "unconfigured", "no coding-agent CLI found on PATH (looked for: claude, opencode, codex, gemini)",
            "install one agent CLI (claude, opencode, codex or gemini), then run: python3 scripts/harness_setup.py --harness <name> --write")
        return
    try:
        proc = subprocess.run([sys.executable, "-B", str(script), "--check", "--json", "--repo", str(ROOT)],
                              capture_output=True, text=True, timeout=60)
        report = json.loads(proc.stdout)
        checks = report["harnesses"][wanted]
    except Exception:
        add("harness", "fail", "harness_setup.py --check did not produce a report",
            f"run it by hand to see why: python3 scripts/harness_setup.py --check --harness {wanted}")
        return
    has_bin = _have_binary(env, wanted)
    parts = [f"{wanted}: binary on PATH {'yes' if has_bin else 'NO'}"]
    first_problem = None
    for c in checks:
        if c.get("name") in ("instructions", "skills", "mcp", "guard") or c.get("status") in ("fail",):
            parts.append(f"{c['name']} {c['status']}")
        if c.get("status") in ("fail", "warn") and first_problem is None:
            first_problem = c
    worst = "pass"
    if any(c.get("status") == "fail" for c in checks):
        worst = "fail"
    elif first_problem is not None or not has_bin:
        worst = "warn"
    if worst == "pass":
        hint = None
    elif not has_bin and first_problem is None:
        hint = f"install the {wanted} CLI and put it on PATH (or set {HARNESS_BINS[wanted][0] or 'PATH'} in .env)"
    else:
        hint = f"python3 scripts/harness_setup.py --harness {wanted} --write   (then re-check: python3 scripts/harness_setup.py --check --harness {wanted})"
    add("harness", worst, "; ".join(parts), hint)


def check_secret_hygiene(env):
    """File modes via os.stat only (contents are never read), plus git-ignore / tracked state."""
    cands = []
    for p in sorted(ROOT.glob(".env*")):
        if p.is_file() and p.name != ".env.example":
            cands.append(p)
    secrets_dir = ROOT / ".secrets"
    if secrets_dir.is_dir():
        cands += [p for p in sorted(secrets_dir.rglob("*"))[:200] if p.is_file()]
    cfg = ROOT / "mcp-server" / "config"
    if cfg.is_dir():
        cands += [p for p in sorted(cfg.glob("credentials*")) if p.is_file() and "example" not in p.name]
    for key in OAUTH_KEYS:
        v = env_val(env, key)
        if v and not is_placeholder(v) and Path(v).is_file():
            cands.append(Path(v))
    seen, files = set(), []
    for p in cands:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            files.append(p)

    git_ok = _own_git_repo()
    perm_bad, tracked, unignored = [], [], []
    for p in files:
        try:
            mode = os.stat(p).st_mode & 0o777
        except OSError:
            continue
        try:
            label = str(p.resolve().relative_to(ROOT.resolve()))
            inside = True
        except ValueError:
            label, inside = str(p), False
        if mode & 0o077:
            perm_bad.append((label, mode))
        if git_ok and inside:
            try:
                if _git(["ls-files", "--error-unmatch", "--", label]).returncode == 0:
                    tracked.append(label)
                elif _git(["check-ignore", "-q", "--", label]).returncode != 0:
                    unignored.append(label)
            except (OSError, subprocess.SubprocessError):
                pass

    if not files:
        add("secret_files", "pass", "no secret files present yet (.env, .secrets/, credentials, OAuth files)")
    else:
        problems, fixes = [], []
        for label, mode in perm_bad:
            problems.append(f"{label} is mode {mode:03o} (group/world readable)")
        if perm_bad:
            fixes.append("chmod 600 " + " ".join(l for l, _ in perm_bad))
        for label in tracked:
            problems.append(f"{label} is tracked by git (it would ship)")
            fixes.append(f"git rm --cached {label} (and add it to .gitignore)")
        for label in unignored:
            problems.append(f"{label} is not git-ignored")
        if unignored:
            fixes.append("add " + ", ".join(unignored) + " to .gitignore")
        note = "" if git_ok else " (git checks skipped: not a git checkout)"
        if perm_bad or tracked:
            add("secret_files", "fail", f"{len(files)} secret file(s) checked; " + "; ".join(problems) + note, "; ".join(fixes))
        elif unignored:
            add("secret_files", "warn", f"{len(files)} secret file(s) checked; " + "; ".join(problems) + note, "; ".join(fixes))
        else:
            add("secret_files", "pass", f"{len(files)} secret file(s) checked: mode 600, git-ignored, untracked{note}")

    plain, enc = cfg / "credentials.json", cfg / "credentials.enc"
    if plain.is_file() and enc.is_file():
        add("credentials_plaintext", "warn", "plaintext mcp-server/config/credentials.json sits next to credentials.enc",
            "after `doctor --only mcp_store` passes, delete the plaintext copy: shred -u mcp-server/config/credentials.json (keep an offline backup in a password manager)",
            required=False)
    baks = sorted(set(cfg.glob("credentials.enc.bak-*")) | set(cfg.glob("*.enc.bak"))) if cfg.is_dir() else []
    if baks:
        add("credentials_backups", "warn", f"{len(baks)} stale backup(s) of the encrypted store: " + ", ".join(b.name for b in baks[:5]),
            "once credentials.enc decrypts fine, remove the old copies: rm mcp-server/config/credentials.enc.bak-*", required=False)


def check_librenms(env):
    url, token = env_val(env, "LIBRENMS_URL"), env_val(env, "LIBRENMS_TOKEN")
    if is_placeholder(url) or is_placeholder(token):
        add("librenms", "unconfigured", "LIBRENMS_URL / LIBRENMS_TOKEN not set (optional: device corroboration)")
        return
    try:
        status, body = http_json("GET", f"{url.rstrip('/')}/api/v0/devices?limit=1", headers={"X-Auth-Token": token},
                                 insecure=True, timeout=90)
        ok = isinstance(body, dict) and body.get("status") == "ok"
        add("librenms", "pass" if ok else "fail", f"HTTP {status} (status={body.get('status') if isinstance(body, dict) else '?'})",
            None if ok else "check LIBRENMS_TOKEN (LibreNMS > API settings) and that LIBRENMS_URL ends at the host, not /api")
    except Exception as e:
        add("librenms", "fail", str(e), "check LIBRENMS_URL is reachable from this machine (VPN/route) and LIBRENMS_TOKEN is valid")


def check_mymate(env):
    base = (env_val(env, "MYMATE_URL") or "").rstrip("/")
    user, pw = env_val(env, "MYMATE_USER"), env_val(env, "MYMATE_PASS")
    if not base or is_placeholder(user) or is_placeholder(pw) or is_placeholder_url(base):
        add("mymate", "unconfigured", "MYMATE_URL/USER/PASS not set (optional feature)")
        return
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=INSECURE))
    try:
        opener.open(base + "/login", timeout=10)
        xsrf = urllib.parse.unquote(next((c.value for c in jar if c.name == "XSRF-TOKEN"), ""))
        req = urllib.request.Request(base + "/login", method="POST",
                                     data=json.dumps({"email": user, "password": pw}).encode(),
                                     headers={"X-XSRF-TOKEN": xsrf, "accept": "application/json", "content-type": "application/json"})
        resp = opener.open(req, timeout=10)
        ok = resp.status in (200, 204)
        add("mymate", "pass" if ok else "fail", f"login HTTP {resp.status}",
            None if ok else "verify MYMATE_USER / MYMATE_PASS (a read-only nocagent account is expected)")
    except urllib.error.HTTPError as e:
        add("mymate", "fail", f"HTTP {e.code} on login", "verify MYMATE_USER / MYMATE_PASS (a read-only nocagent account is expected)")
    except Exception as e:
        add("mymate", "fail", str(e), "check MYMATE_URL is reachable from this machine")


# --------------------------------------------------------------------------
# tier 2: Sonar + notifications
# --------------------------------------------------------------------------

SONAR_PING = ('{ accounts(search: [{string_fields: [{attribute: "name", search_value: "zzz_doctor_ping_no_match_zzz", '
              'match: true}]}]) { entities { id } } }')


def check_sonar(env):
    token = env_val(env, "SONAR_TOKEN")
    if is_placeholder(token):
        add("sonar_api", "unconfigured", "SONAR_TOKEN not set (or a placeholder)",
            "create a Sonar API key (Sonar > Admin > Security > API Keys) and put it in .env as SONAR_TOKEN")
        return
    base, _src = sonar_base(env)
    if not base:
        add("sonar_base_url", "warn", "no Sonar URL configured",
            "set SONAR_BASE_URL in .env, or run: python3 scripts/doctor.py --discover-ids --write-profile (needs the URL once)")
        add("sonar_api", "unconfigured", "no Sonar URL to probe",
            "set SONAR_BASE_URL=https://<your-instance>.sonar.software in .env, or org.sonar_base_url in site-pack/profile.json")
        return
    import gql_client
    try:
        resp = gql_client.request(SONAR_PING, {}, token=token, url=base + "/api/graphql", timeout=15)
    except gql_client.GraphQLError as e:
        if e.kind == "http" and ("HTTP 401" in e.message or "HTTP 403" in e.message):
            add("sonar_api", "fail", f"Sonar rejected the token ({e.message[:60]})",
                "SONAR_TOKEN is wrong or expired: create a new API key (Sonar > Admin > Security > API Keys)")
        else:
            add("sonar_api", "fail", e.message[:160],
                "check the Sonar URL (SONAR_BASE_URL or site-pack/profile.json org.sonar_base_url) and that this machine can reach it")
        return
    errs = resp.get("errors")
    if errs:
        first = errs[0].get("message", "") if isinstance(errs[0], dict) else str(errs[0])
        add("sonar_api", "fail", f"GraphQL error: {first[:100]}", "check SONAR_TOKEN permissions (the key needs read access to accounts and tickets)")
    else:
        add("sonar_api", "pass", f"GraphQL reachable at {base} (read-only search verified)")


def check_chat(env):
    url = env_val(env, "NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL")
    if is_placeholder_url(url):
        add("google_chat", "unconfigured", "NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL not set (optional: notifications)")
        return
    if "chat" in _SKIP:
        add("google_chat", "skipped", "check skipped by --skip")
        return
    if not ARGS.notify:
        add("google_chat", "skipped", "webhook is set; not tested because doctor sends nothing unless asked",
            "to post one clearly labeled test message to the chat space: python3 scripts/doctor.py --only google_chat --notify")
        return
    try:
        status, _ = http_json("POST", url, body={"text": "NocAgent doctor: credential check ran successfully (this is a test message)."})
        add("google_chat", "pass" if status == 200 else "fail",
            f"HTTP {status}" + (" - test message posted to NOC chat space" if status == 200 else ""),
            None if status == 200 else "re-create the webhook: Chat space settings > Apps & integrations > Webhooks")
    except Exception as e:
        add("google_chat", "fail", str(e), "the webhook URL may be expired: Chat space settings > Apps & integrations > Webhooks")


def check_notify(env):
    """Optional: the provider chosen with NOTIFY_PROVIDER (names only; never the URL or a token)."""
    import notify
    raw = (env_val(env, "NOTIFY_PROVIDER") or "").strip().lower()
    if not raw:
        add("notify", "unconfigured", "NOTIFY_PROVIDER not set (optional: the default is gchat, see the google_chat check)")
        return
    if raw not in notify.PROVIDERS:
        add("notify", "warn", "NOTIFY_PROVIDER is not a known provider",
            "set it to one of: " + ", ".join(notify.PROVIDERS))
        return
    if raw == "gchat":
        add("notify", "skipped", "provider gchat; the google_chat check covers it")
        return
    if raw == "stdout":
        add("notify", "pass", "provider stdout (prints to the terminal; nothing to test)")
        return
    url = env_val(env, "NOTIFY_WEBHOOK_URL")
    if is_placeholder_url(url):
        add("notify", "unconfigured", f"provider {raw} selected but NOTIFY_WEBHOOK_URL is not set",
            "set NOTIFY_WEBHOOK_URL in .env with: python3 scripts/env_set.py NOTIFY_WEBHOOK_URL")
        return
    if not notify.url_allowed(url):
        add("notify", "fail", f"provider {raw}: NOTIFY_WEBHOOK_URL must be an https:// URL",
            "use the https URL of the webhook (plain http needs NOTIFY_ALLOW_HTTP=1)")
        return
    if not ARGS.notify:
        add("notify", "skipped", f"provider {raw} is configured; not tested because doctor sends nothing unless asked",
            "to send one clearly labeled test message: python3 scripts/doctor.py --only notify --notify")
        return
    ok, detail = notify.deliver("NocAgent doctor", "Credential check ran successfully (this is a test message).",
                                provider=raw, webhook=url, ntfy_token=env_val(env, "NOTIFY_NTFY_TOKEN") or "")
    add("notify", "pass" if ok else "fail", f"provider {raw}: {detail}",
        None if ok else "check NOTIFY_WEBHOOK_URL (re-create the webhook if it was revoked) and NOTIFY_NTFY_TOKEN for ntfy")


def webhook_health_url(env):
    """http://<host>:<port>/health derived from WEBHOOK_BIND / WEBHOOK_PORT (a wildcard bind is probed on loopback)."""
    bind = (env_val(env, "WEBHOOK_BIND") or "127.0.0.1").strip()
    port = (env_val(env, "WEBHOOK_PORT") or "65333").strip()
    if bind in ("0.0.0.0", ""):
        bind = "127.0.0.1"
    elif bind in ("::", "[::]"):
        bind = "::1"
    host = f"[{bind}]" if ":" in bind and not bind.startswith("[") else bind
    return f"http://{host}:{port}/health", port


def check_webhook_server(env):
    url, port = webhook_health_url(env)
    try:
        status, _ = http_json("GET", url, timeout=5)
        add("webhook_server", "pass" if status == 200 else "warn", f"HTTP {status} on :{port}",
            "systemctl status noc-agent" if status != 200 else None)
    except Exception:
        add("webhook_server", "unconfigured", f"nothing listening on :{port}",
            "optional: start the pipeline server (sudo bash scripts/install.sh, then systemctl start noc-agent) or `python3 scripts/demo.py` to see it run")


def check_site_profile(env):
    prof, err = load_profile()
    if err:
        add("site_profile", "fail", f"profile.json is not valid JSON: {err}",
            "fix the syntax, or regenerate it: python3 scripts/doctor.py --discover-ids --write-profile --force")
        return
    if prof is None:
        add("site_profile", "warn", "site-pack/profile.json missing",
            "python3 scripts/doctor.py --discover-ids --write-profile (reads your Sonar groups/categories), or cp site-pack/profile.example.json site-pack/profile.json and edit")
        return
    sonar = prof.get("sonar") or {}
    null_ids, same_as_example = [], []
    example = _load_example()
    for section, keys in PROFILE_ID_KEYS.items():
        for k in keys:
            node = (sonar.get(section) or {}).get(k)
            v = node.get("id") if isinstance(node, dict) else None
            if not (isinstance(v, int) and not isinstance(v, bool) and v > 0):
                null_ids.append(f"{section}.{k}")
            else:
                ex = ((example.get("sonar") or {}).get(section) or {}).get(k)
                if isinstance(ex, dict) and ex.get("id") == v:
                    same_as_example.append(f"{section}.{k}")
    org = (prof.get("org") or {}).get("name") or "unnamed org"
    hint = "python3 scripts/doctor.py --discover-ids (prints the ids from your Sonar), then add --write-profile for a new profile"
    if null_ids:
        add("site_profile", "warn", f"profile.json loaded ({org}) but ids are null: {', '.join(null_ids)}", hint)
    elif same_as_example and len(same_as_example) == sum(len(v) for v in PROFILE_ID_KEYS.values()):
        add("site_profile", "warn", f"profile.json loaded ({org}) but every id equals profile.example.json (unverified: they belong to another Sonar instance)", hint)
    else:
        add("site_profile", "pass", f"profile.json loaded ({org}); Sonar group/category ids present")


def check_systemd(env):
    unit = "noc-agent.service"
    try:
        proc = run_cmd(["systemctl", "is-active", unit], timeout=10)
        if proc.returncode == 0:
            add("systemd", "pass", f"{unit} active")
        else:
            add("systemd", "unconfigured", f"{unit} not active", "optional: sudo bash scripts/install.sh installs and starts it")
    except FileNotFoundError:
        add("systemd", "skipped", "systemctl not available (non-Linux or container)")
    except Exception as e:
        add("systemd", "warn", str(e), "check `systemctl status noc-agent` by hand")


# --------------------------------------------------------------------------
# tier 3: Google / OAuth
# --------------------------------------------------------------------------

def check_oauth_files(env):
    any_configured, failed, ok = False, [], []
    for key in OAUTH_KEYS:
        path = env_val(env, key)
        if not path or is_placeholder(path):
            continue
        any_configured = True
        rowname = f"oauth_{key.removesuffix('_OAUTH_FILE').lower()}"
        if not Path(path).exists():
            add(rowname, "unconfigured", f"{key} points at a file that does not exist yet",
                "node scripts/google_user_auth.mjs init --oauth-file <path> --client-id ... --client-secret ...", required=False)
            failed.append(rowname)
            continue
        if os.stat(path).st_mode & 0o077:
            add(rowname, "fail", f"{key} file permissions too broad (must be 600)", f"chmod 600 {path}", required=False)
            failed.append(rowname)
            continue
        try:
            proc = run_cmd(["node", str(ROOT / "scripts" / "google_user_auth.mjs"), "refresh",
                            "--oauth-file", path, "--scope", "openid"], timeout=40)
            good = proc.returncode == 0
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            detail = "token refresh succeeded" if good else (lines[-1][:120] if lines else "token refresh failed")
            add(rowname, "pass" if good else "fail", detail,
                None if good else "re-consent: node scripts/google_user_auth.mjs authorize --oauth-file <path> --scopes ...", required=False)
            (ok if good else failed).append(rowname)
        except Exception as e:
            add(rowname, "fail", str(e), "make sure `node` works (node --version)", required=False)
            failed.append(rowname)
    if not any_configured:
        add("oauth_files", "unconfigured", "no *_OAUTH_FILE set; Google user-auth features not yet enabled",
            "run the onboarding wizard (`onboard me`) or node scripts/google_user_auth.mjs init for Calendar, Tracker Sheet, Temp OSPF Sheet")
    elif failed:
        add("oauth_files", "unconfigured", f"{len(failed)} of {len(failed) + len(ok)} OAuth file(s) not ready: {', '.join(failed)}",
            "fix the rows above, then re-run: python3 scripts/doctor.py --only oauth_files")
    else:
        add("oauth_files", "pass", f"{len(ok)} OAuth file(s) refresh fine")


# --------------------------------------------------------------------------
# go-public gate
# --------------------------------------------------------------------------

def check_portability_scan(env):
    """Go-public gate: no org identifiers, credentials or internal IPs in tracked files.

    One engine (scripts/secret_scan.py) shared with the git hooks and CI: a
    hashed deny-list of org identifiers (scripts/leak_denylist.sha256, built
    from private word lists), org.portability_tokens from the untracked
    site-pack/profile.json, and structural rules (IPs, MACs, coordinates,
    credentials, scrub damage). Scans git-tracked files only; what is not
    tracked never ships. Fails closed: an empty deny-list is a failure.

    Opt-in locally (--public-mode / --only portability_scan); always runs when
    CI=true so the gate cannot be skipped on the build server.
    """
    import secret_scan
    try:
        tracked = secret_scan.tracked_files(ROOT)
    except Exception as e:
        add("portability_scan", "fail" if _in_ci() else "warn", f"could not enumerate git files: {e}",
            "run from a git checkout; the gate scans exactly what would ship")
        return
    res = secret_scan.scan_paths_detailed(tracked, require_denylist=True, base=ROOT)
    if not res.findings:
        add("portability_scan", "pass",
            f"{res.files_scanned} tracked files, {res.denylist_entries} deny-list hashes, "
            f"{res.suppressions} reasoned suppressions - no identifiers or credentials", None)
        return
    by_rule, by_file = {}, {}
    for f in res.findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        by_file[f.path] = by_file.get(f.path, 0) + 1
    rules = ", ".join(f"{k} x{v}" for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1]))
    files = ", ".join(f"{k} (x{v})" for k, v in sorted(by_file.items(), key=lambda kv: -kv[1])[:8])
    add("portability_scan", "fail", f"{len(res.findings)} findings in {len(by_file)} files [{rules}]; top: {files}",
        "list them: python3 scripts/secret_scan.py --tracked ; replace each with a placeholder or, for a "
        "deliberate literal, add a reason-bearing secret-scan:allow(<reason>) marker on that line")


def _in_ci():
    return os.environ.get("CI", "").strip().lower() in ("1", "true", "yes")


# (name, function, tier, required). `tier`/`required` are the defaults for the rows a check adds;
# a check may override them per row (env_required adds rows for tiers 1 and 2).
CHECKS = [
    ("python", check_python, 0, True),
    ("node", check_node, 0, True),
    ("demo", check_demo, 0, True),
    ("git_hooks", check_git_hooks, 0, False),
    ("env_required", check_env_required, 1, True),
    ("secret_hygiene", check_secret_hygiene, 1, True),
    ("mcp_build", check_mcp_build, 1, True),
    ("mcp_store", check_mcp_store, 1, True),
    ("harness", check_harness, 1, True),
    ("librenms", check_librenms, 1, False),
    ("mymate", check_mymate, 1, False),
    ("sonar_api", check_sonar, 2, True),
    ("site_profile", check_site_profile, 2, False),
    ("webhook_server", check_webhook_server, 2, False),
    ("google_chat", check_chat, 2, False),
    ("notify", check_notify, 2, False),
    ("systemd", check_systemd, 2, False),
    ("oauth_files", check_oauth_files, 3, True),
    ("portability_scan", check_portability_scan, None, False),
]

PROFILE_ID_KEYS = {
    "groups": ["noc", "cs_level_2", "sys_admin"],
    "categories": ["slow_speeds", "night_peak", "cx_th_side_issue", "weather"],
}


# --------------------------------------------------------------------------
# --discover-ids / --write-profile
# --------------------------------------------------------------------------

# Fallback name fragments when neither profile.json nor profile.example.json carries a "match" list.
DEFAULT_MATCH = {
    "groups": {"noc": ["noc"], "cs_level_2": ["cs level 2", "cs level2", "customer service level 2"],
               "sys_admin": ["sys admin", "sysadmin", "system admin"]},
    "categories": {"slow_speeds": ["slow speed"], "night_peak": ["night", "peak"],
                   "cx_th_side_issue": ["cx/th", "th-side", "th side"], "weather": ["weather", "rain"]},
}
LIST_QUERY = ("query ListNames($paginator: Paginator) {{ {field}(paginator: $paginator) "
              "{{ entities {{ id name }} page_info {{ page total_pages }} }} }}")


def _load_example():
    try:
        data = json.loads((ROOT / "site-pack" / "profile.example.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _match_terms(section, key):
    prof, _ = load_profile()
    for doc in (prof or {}, _load_example()):
        node = ((doc.get("sonar") or {}).get(section) or {}).get(key)
        terms = node.get("match") if isinstance(node, dict) else None
        if isinstance(terms, list) and terms and all(isinstance(t, str) for t in terms):
            return [t.lower() for t in terms]
    return DEFAULT_MATCH[section][key]


def _term_in(term, name):
    return re.search(r"(?<![a-z0-9])" + re.escape(term), name) is not None  # word start; "slow speed" fits "Slow Speeds"


def match_rows(section, key, rows):
    """(row or None, note or None): the single row whose name fits the key's fragments."""
    terms = _match_terms(section, key)
    hits = [r for r in rows if any(_term_in(t, r["name"].lower()) for t in terms)]
    if len(hits) > 1:  # prefer an exact name over a looser fragment match
        exact = [r for r in hits if r["name"].strip().lower() in terms]
        hits = exact if len(exact) == 1 else hits
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"sonar.{section}.{key}: no name matches {terms}; set the id by hand"
    return None, (f"sonar.{section}.{key}: ambiguous, {len(hits)} names match "
                  f"({', '.join(str(h['id']) + ' ' + h['name'] for h in hits[:4])}); set the id by hand")


def _list_all(gql, field, token, url):
    rows, page = [], 1
    while page <= 50:
        data = gql.execute(LIST_QUERY.format(field=field), {"paginator": {"page": page, "records_per_page": 100}},
                           token=token, url=url, timeout=20)
        conn = data.get(field) or {}
        for e in conn.get("entities") or []:
            if isinstance(e, dict) and e.get("id") is not None:
                try:
                    rows.append({"id": int(e["id"]), "name": str(e.get("name") or "")})
                except (TypeError, ValueError):
                    pass
        total = (conn.get("page_info") or {}).get("total_pages") or 1
        if page >= total:
            break
        page += 1
    return rows


def build_fragment(groups, categories):
    frag = {"sonar": {"groups": {}, "categories": {}}}
    notes = []
    for section, rows in (("groups", groups), ("categories", categories)):
        for key in PROFILE_ID_KEYS[section]:
            row, note = match_rows(section, key, rows)
            frag["sonar"][section][key] = {"name": row["name"] if row else None, "id": row["id"] if row else None}
            if note:
                notes.append(note)
    fibers = sorted(r["name"] for r in categories if r["name"].lower().startswith("network - "))
    if fibers:
        frag["sonar"]["categories"]["network_fiber"] = {"names": fibers}
    return frag, notes


def write_profile(fragment, base_url, force):
    """Create site-pack/profile.json from profile.example.json + discovered ids. (path, message)."""
    target = profile_path()
    if target.exists() and not force:
        return None, f"{target} already exists; not overwritten (add --force to replace it; the old file is kept as a .bak-<time> copy)"
    example = ROOT / "site-pack" / "profile.example.json"
    try:
        doc = json.loads(example.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"cannot read site-pack/profile.example.json: {e}"
    for section, keys in fragment["sonar"].items():
        dest = doc.setdefault("sonar", {}).setdefault(section, {})
        for k, v in keys.items():
            node = dest.setdefault(k, {})
            node.update(v)
    if base_url and base_url.startswith("https://"):
        doc.setdefault("org", {})["sonar_base_url"] = base_url
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        bak = target.with_name(target.name + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
        shutil.copy2(target, bak)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, target)
    return target, f"wrote {target} (fill in the org name/domain and portability_tokens by hand)"


def discover_ids(args):
    import gql_client
    token = env_val(dict(os.environ), "SONAR_TOKEN")
    if is_placeholder(token):
        print("doctor: SONAR_TOKEN is empty, unset or a placeholder", file=sys.stderr)
        print("fix: put a Sonar API key in .env as SONAR_TOKEN (Sonar > Admin > Security > API Keys)", file=sys.stderr)
        return 1
    base, _ = sonar_base(dict(os.environ))
    if not base:
        print("doctor: no Sonar URL configured", file=sys.stderr)
        print("fix: set SONAR_BASE_URL=https://<your-instance>.sonar.software in .env", file=sys.stderr)
        return 1
    try:
        groups = _list_all(gql_client, "ticket_groups", token, base + "/api/graphql")
        categories = _list_all(gql_client, "ticket_categories", token, base + "/api/graphql")
    except gql_client.GraphQLError as e:
        print(f"doctor: Sonar query failed ({e.kind}): {e.message[:200]}", file=sys.stderr)
        print("fix: check SONAR_TOKEN, the Sonar URL, and that the API key may read ticket groups and categories", file=sys.stderr)
        return 1
    fragment, notes = build_fragment(groups, categories)
    written, message = None, None
    if args.write_profile:
        written, message = write_profile(fragment, base, args.force)
    if args.json:
        print(json.dumps({"groups": groups, "categories": categories, "fragment": fragment, "notes": notes,
                          "profile_written": str(written) if written else None, "message": message}, indent=2))
    else:
        for title, rows in (("Ticket groups", groups), ("Ticket categories", categories)):
            print(f"{title} ({len(rows)}):")
            for r in rows[:40]:
                print(f"  {r['id']:>6}  {r['name']}")
            if len(rows) > 40:
                print(f"  ... {len(rows) - 40} more (use --json for the full list)")
        print("\nPaste into site-pack/profile.json (merge into the existing \"sonar\" object; null = no match):")
        print(json.dumps(fragment, indent=2))
        for n in notes:
            print("note: " + n)
        if message:
            print(message)
    return 2 if (args.write_profile and not written) else 0


# --------------------------------------------------------------------------
# summary + main
# --------------------------------------------------------------------------

def tier_summary(results, max_tier):
    """{tier: {"label","ready","blocking"}} plus the highest ready tier (None if tier 0 is blocked)."""
    tiers, ready, broken = {}, None, False
    for t in range(0, max_tier + 1):
        blocking = [r["name"] for r in results
                    if r["tier"] == t and r["required"] and r["status"] not in ("pass", "warn")]
        tiers[t] = {"label": TIER_LABELS[t], "ready": not blocking, "blocking": blocking}
        if blocking:
            broken = True
        if not broken:
            ready = t
    return tiers, ready


def run(argv=None):
    """Run the doctor; returns the exit code. (main() wraps this in sys.exit.)"""
    global ARGS, ENV, ENV_EXISTS, _SKIP
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output for the onboarding wizard")
    ap.add_argument("--tier", type=int, choices=[0, 1, 2, 3], default=None,
                    help="only run the checks for tiers 0..N (0 demo, 1 devices, 2 Sonar, 3 Google); default: all")
    ap.add_argument("--only", default="", help="comma-separated check names filter")
    ap.add_argument("--skip", default="", help="comma-separated checks to skip (e.g. mymate)")
    ap.add_argument("--public-mode", action="store_true", help="also run the go-public portability_scan (always on when CI=true)")
    ap.add_argument("--notify", action="store_true", help="also post ONE labeled test message to the notification provider, Google Chat by default (off by default: doctor is read-only)")
    ap.add_argument("--server", action="store_true", help="also check the ticket webhook server settings (optional component)")
    ap.add_argument("--harness", default=None, help="coding-agent CLI to verify (default: AGENT_HARNESS, else the first one on PATH)")
    ap.add_argument("--discover-ids", action="store_true", help="list Sonar ticket groups/categories and print a site-pack/profile.json fragment")
    ap.add_argument("--write-profile", action="store_true", help="with --discover-ids: create site-pack/profile.json (only if absent unless --force)")
    ap.add_argument("--force", action="store_true", help="with --write-profile: replace an existing profile.json (a .bak copy is kept)")
    args = ap.parse_args(argv)
    ARGS = args
    ENV, ENV_EXISTS = load_env()
    _PARSED_ENV.clear()
    _PARSED_ENV.update(ENV)
    RESULTS.clear()

    if args.write_profile and not args.discover_ids:
        print("doctor: --write-profile needs --discover-ids", file=sys.stderr)
        return 2
    if args.discover_ids:
        return discover_ids(args)  # SONAR_TOKEN / SONAR_BASE_URL come from the process env, then .env (never argv)

    only = set(_only_names(args))
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    valid = [c[0] for c in CHECKS]
    unknown = sorted(only - set(valid))
    if unknown:
        print(f"doctor: unknown check name(s) for --only: {', '.join(unknown)}", file=sys.stderr)
        print(f"valid names: {', '.join(valid)}", file=sys.stderr)
        return 2
    _SKIP = skip
    max_tier = args.tier if args.tier is not None else 3

    for name, fn, tier, required in CHECKS:
        if name == "portability_scan" and not (args.public_mode or _in_ci()) and name not in only:
            continue  # local runs: opt-in; CI always runs it
        if only and name not in only:
            continue
        if tier is not None and tier > max_tier:
            continue
        _CTX.update(check=name, tier=tier, required=required)
        if name in skip:
            add(name, "skipped", "skipped by --skip")
            continue
        env = dict(os.environ)
        try:
            fn(env)
        except Exception as e:  # a check bug must never crash the doctor
            add(name, "fail", f"check crashed: {type(e).__name__}: {e}", f"report it, and run the rest meanwhile: python3 scripts/doctor.py --skip {name}")
    _CTX.update(check=None, tier=None, required=False)
    RESULTS[:] = [r for r in RESULTS if r["tier"] is None or r["tier"] <= max_tier]

    if not RESULTS:
        print("doctor: no checks ran (nothing matched --only / --tier, or everything was skipped)", file=sys.stderr)
        return 2

    failed = [r for r in RESULTS if r["status"] == "fail"]
    tiers, ready = tier_summary(RESULTS, max_tier)
    partial = bool(only)
    ready_text = "n/a (partial run: --only)" if partial else ("none" if ready is None else str(ready))
    if args.json:
        print(json.dumps({"results": RESULTS, "ok": not failed,
                          "ready_tier": None if partial else ready, "tiers": {str(k): v for k, v in tiers.items()}}, indent=2))
        return 1 if failed else 0

    icons = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skipped": "SKIP", "unconfigured": "  -  "}
    for r in RESULTS:
        fix = f"\n        fix: {r['hint']}" if r.get("hint") else ""
        print(f"[{icons.get(r['status'], r['status']).center(4)}] {r['name']:<20} {r['detail']}{fix}")
    print(f"\n{len(RESULTS)} checks, {len(failed)} failed - " + ("ALL GREEN" if not failed else "fix failures above (or mark intentionally unconfigured features)"))
    if not partial:
        for t in range(0, 4):
            if t in tiers:
                info = tiers[t]
                state = "ready" if info["ready"] else "needs " + ", ".join(info["blocking"])
                print(f"tier {t} ({info['label']}): {state}")
            else:
                print(f"tier {t} ({TIER_LABELS[t]}): not checked (--tier {args.tier})")
    print(f"ready tier: {ready_text}")
    return 1 if failed else 0


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
