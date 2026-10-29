#!/usr/bin/env python3
"""Share the organization's site pack between colleagues through a private git repository (stdlib + git).

    python3 scripts/pack.py init <remote-url>     # a lead: turn this site-pack/ into a pack repository
    python3 scripts/pack.py join <remote-url>     # a colleague: fetch the team's pack into site-pack/
    python3 scripts/pack.py status [--fetch]      # where this copy stands against the team's
    python3 scripts/pack.py pull [--yes]          # show what changed, then apply it
    python3 scripts/pack.py git <git arguments>   # a lead: run git against the pack (add, commit, push, branch)

The pack holds only the org-specific files that the public repository ignores: profile.json (with
the service registry), devices.md, tech_calendars.json, team.json, topology/, ticket-context/,
rename-site/, evals/ and skills/. Its git directory is site-pack/.pack.git with site-pack/ as the
work tree, so the paths skills use (site-pack/devices.md) do not change and the public repository
never sees any of it. Keep the remote private.

Trust: a pack change can move a service address, add a member, widen a role or add a skill, and
the agent trusts those. So `pull` and `join` first print a summary that names every changed
service address, roster entry and skill, and apply nothing without a yes (--yes, or answer the
prompt). Without a terminal and without --yes they print the summary and exit 2. They never run
when NOC_UNATTENDED=1. If ~/.config/nocagent/user.json has {"pack": {"require_signed": true}}, a
change is applied only when its newest commit has a valid signature.

Secrets do not belong in a pack. `init` and every commit (a pre-commit hook in the pack's git
directory) refuse content that looks like a credential: tokens, keys, passwords, URL credentials.
Addresses, names and IDs are fine here; that is what the pack is for.

Exit codes: 0 ok, 1 failed (credential found, git error, diverged), 2 usage, invalid pack, or needs
a yes, 3 no pack repository here, 4 refused (unattended).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noc_config  # noqa: E402
import secret_scan  # noqa: E402
import team  # noqa: E402

PACK_FILES = ("profile.json", "devices.md", "tech_calendars.json", "team.json")
PACK_DIRS = ("topology", "ticket-context", "rename-site", "evals", "skills")
NEVER = ("*.bak*", ".env*", "*.pem", "*.key", "*oauth*", ".secrets")
CREDENTIAL_RULES = frozenset({
    "aws-key", "github-token", "slack-token", "google-api-key", "google-oauth", "api-key", "jwt", "private-key",
    "bearer-token", "sshpass-literal", "url-credentials", "url-secret-param", "literal-secret",
})
KEEP_GIT_ENV = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"}
NO_PACK = "no pack repository here (%s). A lead creates one with pack.py init; a colleague joins with pack.py join <url>."


class PackError(Exception):
    """Invalid input or an unusable pack; maps to exit code 2."""


# --- locations and git --------------------------------------------------------

def pack_dir() -> Path:
    override = os.environ.get("NOC_SITE_PACK_DIR", "").strip()
    return Path(override) if override else noc_config.profile_path().parent


def git_dir() -> Path:
    override = os.environ.get("NOC_PACK_GIT_DIR", "").strip()
    return Path(override) if override else pack_dir() / ".pack.git"


def _env() -> Dict[str, str]:
    """The environment without repository-local GIT_* variables (a hook exports GIT_DIR and friends),
    and with only plain transports allowed (no ext:: or other remote helpers)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in KEEP_GIT_ENV}
    env["GIT_ALLOW_PROTOCOL"] = "file:https:ssh:git"
    return env


def git(*args: str, gdir: Optional[Path] = None, wtree: Optional[Path] = None, stdin: Optional[str] = None,
        interactive: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git", "--git-dir", str(gdir or git_dir()), "--work-tree", str(wtree or pack_dir())] + list(args)
    if interactive:      # let git show its own output and prompts (push, fetch asking for a passphrase)
        return subprocess.run(cmd, env=_env(), text=True, check=False)
    return subprocess.run(cmd, env=_env(), input=stdin, text=True, capture_output=True, check=False)


def initialised() -> bool:
    return (git_dir() / "HEAD").is_file()


def _exclude_text() -> str:
    lines = ["/*"] + ["!/%s" % f for f in PACK_FILES] + ["!/%s/" % d for d in PACK_DIRS] + list(NEVER)
    return "\n".join(lines) + "\n"


def _setup_repo(url: str, branch: str) -> None:
    if not url or url.startswith("-") or "::" in url:
        raise PackError("%r is not a git remote URL" % url)
    if not branch or branch.startswith("-"):
        raise PackError("%r is not a branch name" % branch)
    gdir = git_dir()
    gdir.parent.mkdir(parents=True, exist_ok=True)
    r = git("init", "-q")
    if r.returncode != 0:
        raise PackError("git init failed: " + r.stderr.strip())
    (gdir / "info").mkdir(exist_ok=True)
    (gdir / "info" / "exclude").write_text(_exclude_text(), encoding="utf-8")
    git("remote", "add", "origin", url)
    git("symbolic-ref", "HEAD", "refs/heads/" + branch)


def _discard_repo() -> None:
    shutil.rmtree(str(git_dir()), ignore_errors=True)


def _unattended(action: str) -> Optional[int]:
    if os.environ.get("NOC_UNATTENDED", "").strip() == "1":
        print("refused: pack.py %s changes what the agent trusts, so it only runs when the operator asks "
              "(NOC_UNATTENDED=1 here). Return needs_human and tell the operator." % action, file=sys.stderr)
        return 4
    return None


def _confirm(yes: bool, question: str) -> bool:
    if yes:
        return True
    if sys.stdin.isatty() and sys.stdout.isatty():
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    return False


# --- what a change contains ---------------------------------------------------

def _blob(ref: Optional[str], path: str) -> str:
    if not ref:
        return ""
    r = git("show", "%s:%s" % (ref, path))
    return r.stdout if r.returncode == 0 else ""


def _json(text: str) -> dict:
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _resources(profile: dict) -> Dict[str, str]:
    reg = profile.get("resources") if isinstance(profile.get("resources"), dict) else {}
    out = {}
    for name, entry in reg.items():
        if isinstance(entry, dict):
            out[name] = str(entry.get("url") or entry.get("id") or "")
    return out


def _diff_maps(label: str, old: Dict[str, str], new: Dict[str, str]) -> List[str]:
    lines = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            lines.append("%s %s added: %s" % (label, key, new[key]))
        elif key not in new:
            lines.append("%s %s removed (was %s)" % (label, key, old[key]))
        elif old[key] != new[key]:
            lines.append("%s %s changed: %s -> %s" % (label, key, old[key], new[key]))
    return lines


def _settings(profile: dict) -> Dict[str, str]:
    """Values that steer where the agent sends tickets or which Sonar it talks to."""
    out: Dict[str, str] = {}
    for key in ("sonar_base_url", "uisp_host"):
        v = (profile.get("org") or {}).get(key) if isinstance(profile.get("org"), dict) else None
        if isinstance(v, str):
            out["org." + key] = v
    sonar = profile.get("sonar") if isinstance(profile.get("sonar"), dict) else {}
    for section in ("groups", "categories"):
        for name, entry in (sonar.get(section) or {}).items():
            if isinstance(entry, dict) and "id" in entry:
                out["sonar.%s.%s.id" % (section, name)] = str(entry["id"])
    return out


def _members(roster: dict) -> Dict[str, str]:
    out = {}
    for handle, m in (roster.get("members") or {}).items():
        if isinstance(m, dict):
            out[handle] = "%s%s" % (m.get("role", "?"), "" if m.get("active", True) is not False else " (disabled)")
    return out


def _pools(roster: dict) -> Dict[str, str]:
    return {k: str(v.get("scope", "?")) for k, v in (roster.get("pools") or {}).items() if isinstance(v, dict)}


def summarise(old: Optional[str], new: str) -> Tuple[List[str], List[str]]:
    """(trust-relevant lines, other lines) for the change from `old` (None = nothing) to `new`."""
    if old:
        names = git("diff", "--name-status", old, new).stdout.splitlines()
        changed = [ln.split("\t", 1)[1] for ln in names if "\t" in ln]
    else:
        changed = git("ls-tree", "-r", "--name-only", new).stdout.splitlines()
    trust: List[str] = []
    old_profile, new_profile = _json(_blob(old, "profile.json")), _json(_blob(new, "profile.json"))
    trust += _diff_maps("service", _resources(old_profile), _resources(new_profile))
    trust += _diff_maps("setting", _settings(old_profile), _settings(new_profile))
    old_roster, new_roster = _json(_blob(old, "team.json")), _json(_blob(new, "team.json"))
    trust += _diff_maps("member", _members(old_roster), _members(new_roster))
    trust += _diff_maps("pool", _pools(old_roster), _pools(new_roster))
    old_models = {m: "" for m in old_roster.get("approved_models") or [] if isinstance(m, str)}
    new_models = {m: "" for m in new_roster.get("approved_models") or [] if isinstance(m, str)}
    trust += ["approved model %s %s" % (m, "added" if m in new_models else "removed")
              for m in sorted(set(old_models) ^ set(new_models))]
    skills = sorted(p for p in changed if p.startswith("skills/"))
    trust += ["skill file %s" % p for p in skills]
    other = [p for p in changed if not p.startswith("skills/") and p != "team.json"]
    return trust, other


def _print_summary(trust: List[str], other: List[str]) -> None:
    if trust:
        print("changes the agent will trust:")
        for line in trust:
            print("  " + line)
    if other:
        print("other files (%d):" % len(other))
        for p in other[:40]:
            print("  " + p)
        if len(other) > 40:
            print("  ... and %d more" % (len(other) - 40))
    if not trust and not other:
        print("no file changes")


def _signed_ok(ref: str) -> Optional[str]:
    """None when fine or not required, else the reason a change must not be applied."""
    try:
        required = bool(((json.loads(team.user_config_path().read_text(encoding="utf-8")) or {}).get("pack") or {}).get("require_signed"))
    except (OSError, ValueError, AttributeError):
        required = False
    if not required:
        return None
    r = git("verify-commit", ref)
    return None if r.returncode == 0 else "require_signed is on and %s has no valid signature" % ref


# --- credential guard ---------------------------------------------------------

def credential_findings(items: List[Tuple[str, str]]) -> List[str]:
    out = []
    for path, text in items:
        for f in secret_scan.scan_text(text, path):
            if f.rule in CREDENTIAL_RULES:
                out.append("%s:%d %s" % (path, f.line, f.rule))
    return out


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in NEVER)


def _pack_files(base: Path) -> List[Path]:
    found: List[Path] = []
    for f in PACK_FILES:
        if (base / f).is_file():
            found.append(base / f)
    for d in PACK_DIRS:
        root = base / d
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file() and not p.is_symlink() and not any(_excluded(part) for part in p.relative_to(base).parts):
                    found.append(p)
    return found


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --- commands -----------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    refused = _unattended("init")
    if refused:
        return refused
    base = pack_dir()
    if initialised():
        print("error: %s already holds a pack repository; use  pack.py status  or  pack.py pull" % base, file=sys.stderr)
        return 2
    files = _pack_files(base)
    if not files:
        print("error: nothing to track in %s (expected profile.json, topology/, ...)" % base, file=sys.stderr)
        return 2
    findings = credential_findings([(str(p.relative_to(base)), _read_text(p)) for p in files])
    if findings:
        print("refused: these files look like they hold a credential. Remove it (secrets belong in .env or the "
              "credential store), then run init again:", file=sys.stderr)
        for line in findings:
            print("  " + line, file=sys.stderr)
        return 1
    try:
        _setup_repo(args.url, args.branch)
        git("add", "-A")
        r = git("commit", "-q", "-m", "chore: initial site pack")
        if r.returncode != 0:
            raise PackError("git commit failed: %s (set user.name and user.email, or fix commit signing)" % r.stderr.strip())
        hook = git_dir() / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\nexec python3 "%s" guard --git-dir "%s" --work-tree "%s"\n' % (
            Path(__file__).resolve(), git_dir(), base), encoding="utf-8")
        hook.chmod(0o755)
    except PackError as e:
        _discard_repo()
        print("error: %s" % e, file=sys.stderr)
        return 2
    print("pack repository created in %s (%d file(s) committed on %s)." % (git_dir(), len(files), args.branch))
    print("next: create the PRIVATE repository %s, then publish with:" % args.url)
    print("  python3 scripts/pack.py git push -u origin %s" % args.branch)
    return 0


def _manifest_problem(ref: str) -> Optional[str]:
    """Why a pack at `ref` must not be installed, or None."""
    r = git("ls-tree", "-r", ref)
    if r.returncode != 0:
        return "cannot read the pack: " + r.stderr.strip()
    allowed_dirs = tuple(d + "/" for d in PACK_DIRS)
    for line in r.stdout.splitlines():
        meta, _tab, path = line.partition("\t")
        mode = meta.split()[0] if meta.split() else ""
        if mode in ("120000", "160000"):
            return "%s is a symlink or submodule; a pack holds plain files only" % path
        if path.startswith(("/", "../")) or "/../" in path or ".." in Path(path).parts:
            return "%s: unsafe path" % path
        if path not in PACK_FILES and not path.startswith(allowed_dirs):
            return "%s is outside what a pack may hold (%s)" % (path, ", ".join(PACK_FILES + tuple(d + "/" for d in PACK_DIRS)))
        if any(_excluded(part) for part in Path(path).parts):
            return "%s looks like a secret or backup file; a pack must not contain it" % path
    return None


def cmd_join(args: argparse.Namespace) -> int:
    refused = _unattended("join")
    if refused:
        return refused
    base = pack_dir()
    if initialised():
        print("error: %s already holds a pack repository; use  pack.py pull  to update it" % base, file=sys.stderr)
        return 2
    base.mkdir(parents=True, exist_ok=True)
    try:
        _setup_repo(args.url, args.branch)
        fetched = git("fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (args.branch, args.branch))
        if fetched.returncode != 0:
            raise PackError("could not fetch branch %s: %s" % (args.branch, fetched.stderr.strip()))
        ref = "origin/" + args.branch
        problem = _manifest_problem(ref) or _signed_ok(ref)
        if problem:
            raise PackError(problem)
        trust, other = summarise(None, ref)
        clash = [p for p in git("ls-tree", "-r", "--name-only", ref).stdout.splitlines() if (base / p).exists()]
        if clash:
            raise PackError("these files already exist here and would be overwritten: %s. Move them aside, then join again."
                            % ", ".join(clash[:8]))
        print("the pack at %s (%s) will be written into %s" % (args.url, args.branch, base))
        _print_summary(trust, other)
        if not _confirm(args.yes, "Install this pack?"):
            _discard_repo()
            print("not installed. Review the list above, then run again with --yes.", file=sys.stderr)
            return 2
        r = git("checkout", "-q", "-B", args.branch, ref)
        if r.returncode != 0:
            raise PackError("checkout failed: " + r.stderr.strip())
        git("branch", "--set-upstream-to", ref, args.branch)
    except PackError as e:
        _discard_repo()
        print("error: %s" % e, file=sys.stderr)
        return 2
    print("joined. next: python3 scripts/team.py me <your-handle>   then   python3 scripts/doctor.py --only team,resources")
    return 0


def _branch() -> str:
    return git("symbolic-ref", "--short", "HEAD").stdout.strip() or "main"


def _counts(ref_a: str, ref_b: str) -> Tuple[int, int]:
    r = git("rev-list", "--left-right", "--count", "%s...%s" % (ref_a, ref_b))
    parts = r.stdout.split()
    return (int(parts[0]), int(parts[1])) if r.returncode == 0 and len(parts) == 2 else (0, 0)


def cmd_status(args: argparse.Namespace) -> int:
    if not initialised():
        print(NO_PACK % pack_dir())
        return 3
    branch = _branch()
    if args.fetch:
        f = git("fetch", "-q", "origin", branch)
        if f.returncode != 0:
            print("warning: fetch failed: " + f.stderr.strip(), file=sys.stderr)
    ahead, behind = _counts("HEAD", "origin/" + branch)
    last = git("log", "-1", "--format=%h %cs %an: %s").stdout.strip()
    dirty = [ln for ln in git("status", "--porcelain").stdout.splitlines() if ln.strip()]
    print("pack:    %s (branch %s)" % (git_dir(), branch))
    print("latest:  %s" % (last or "no commits"))
    print("ahead:   %d local commit(s) not shared" % ahead)
    print("behind:  %d commit(s) to pull%s" % (behind, "" if args.fetch else " (as of the last fetch; use --fetch)"))
    print("changes: %d file(s) edited here and not committed" % len(dirty))
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    refused = _unattended("pull")
    if refused:
        return refused
    if not initialised():
        print(NO_PACK % pack_dir(), file=sys.stderr)
        return 3
    branch = _branch()
    ref = "origin/" + branch
    f = git("fetch", "-q", "origin", branch)
    if f.returncode != 0:
        print("error: fetch failed: " + f.stderr.strip(), file=sys.stderr)
        return 1
    ahead, behind = _counts("HEAD", ref)
    if behind == 0:
        print("up to date" + ("; %d local commit(s) not shared yet" % ahead if ahead else ""))
        return 0
    if ahead:
        print("refused: this copy has %d local commit(s) and the team has %d new one(s). Merge them with a lead "
              "(pack.py git ...); nothing was changed." % (ahead, behind), file=sys.stderr)
        return 1
    problem = _manifest_problem(ref) or _signed_ok(ref)
    if problem:
        print("refused: " + problem, file=sys.stderr)
        return 1
    trust, other = summarise("HEAD", ref)
    print("%d new commit(s) from the team:" % behind)
    _print_summary(trust, other)
    if not _confirm(args.yes, "Apply these changes?"):
        print("not applied. Review the list above, then run again with --yes.", file=sys.stderr)
        return 2
    m = git("merge", "--ff-only", "-q", ref)
    if m.returncode != 0:
        print("error: could not apply (files edited here may conflict): " + m.stderr.strip(), file=sys.stderr)
        return 1
    print("applied. run: python3 scripts/doctor.py --only team,resources")
    return 0


def cmd_git(args: argparse.Namespace) -> int:
    refused = _unattended("git")
    if refused:
        return refused
    if not initialised():
        print(NO_PACK % pack_dir(), file=sys.stderr)
        return 3
    return git(*args.git_args, interactive=True).returncode


def cmd_guard(args: argparse.Namespace) -> int:
    """pre-commit hook: refuse staged content that looks like a credential."""
    gdir, wtree = Path(args.git_dir), Path(args.work_tree)
    names = git("diff", "--cached", "--name-only", "--diff-filter=AM", gdir=gdir, wtree=wtree).stdout.splitlines()
    items = [(n, git("show", ":" + n, gdir=gdir, wtree=wtree).stdout) for n in names]
    findings = credential_findings(items)
    if findings:
        print("pack commit refused: these staged files look like they hold a credential:", file=sys.stderr)
        for line in findings:
            print("  " + line, file=sys.stderr)
        print("Remove it (secrets belong in .env or the credential store) and commit again.", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("init", cmd_init, "a lead: create the pack repository from this site-pack/"),
                            ("join", cmd_join, "a colleague: fetch the team's pack")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("url", help="the private remote, for example git@github.com:<org>/<pack-repo>.git")
        p.add_argument("--branch", default="main")
        if name == "join":
            p.add_argument("--yes", action="store_true", help="install without asking (after reading the summary)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("status", help="where this copy stands")
    p.add_argument("--fetch", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("pull", help="show what changed, then apply it")
    p.add_argument("--yes", action="store_true", help="apply without asking (after reading the summary)")
    p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("git", help="run git against the pack")
    p.add_argument("git_args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_git)
    p = sub.add_parser("guard", help="pre-commit hook (internal)")
    p.add_argument("--git-dir", required=True)
    p.add_argument("--work-tree", required=True)
    p.set_defaults(fn=cmd_guard)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
