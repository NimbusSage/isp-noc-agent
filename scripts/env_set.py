#!/usr/bin/env python3
"""env_set.py - put a secret into .env without pasting it into a chat or onto a command line.

    python3 scripts/env_set.py SONAR_TOKEN                  # hidden prompt, then writes .env
    python3 scripts/env_set.py KEY1 KEY2                    # one hidden prompt per key
    python3 scripts/env_set.py --generate TICKET_WEBHOOK_AUTH_TOKEN
    python3 scripts/env_set.py --list-missing --tier 2      # required names still unset
    pass show nocagent/sonar | python3 scripts/env_set.py --stdin SONAR_TOKEN

In Claude Code run it as `! python3 scripts/env_set.py KEY`: the leading `!` runs it in
your own shell, so the value you type never enters the conversation.

What it guarantees
  * The value is read with getpass (no echo) or from stdin (--stdin), never from argv,
    so it is not in `ps`, shell history or an agent transcript.
  * The value is never printed. Output is only `set KEY (N chars)`.
  * `.env` is created from .env.example if missing (comments kept, active values
    blanked), mode 0600. It is replaced atomically: temp file in the same
    directory, fsync, os.replace, mode 0600. Only the named key changes.
  * It refuses to touch a `.env` that is group/world-readable (or a symlink)
    unless you pass --fix-perms, which chmods it to 0600 first.
  * Values are written double-quoted with \\ \" $ ` escaped, so `source .env` and
    scripts/envfile.py both read exactly what you typed.

--list-missing reads .env in-process only to test presence and prints key NAMES, never
values. Tiers and REQUIRED markers come from the `# --- Tier N: ...` headers and the
`REQUIRED.` word on the comment line above each variable in .env.example. `--tier N`
means tiers 0..N (what you need to run at that level); add --exact for tier N alone.
Exit status of --list-missing: 0 nothing missing, 1 something missing.

Exit codes: 0 ok, 1 --list-missing found gaps, 2 usage / refused / invalid input.
"""

import argparse
import getpass
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import envfile  # noqa: E402  (sibling module; path inserted above)

ROOT = HERE.parent
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TIER_RE = re.compile(r"^#\s*---\s*Tier\s+(\d+)\b")
ACTIVE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
COMMENTED_RE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)\s*=")
REQUIRED_RE = re.compile(r"\bREQUIRED\b")


class EnvSetError(Exception):
    """A refusal or invalid input; the message never contains a secret value."""


# ---------------------------------------------------------------- validation

def validate_key(key: str) -> str:
    if not KEY_RE.match(key or ""):
        raise EnvSetError(f"invalid key {key[:40]!r}: use UPPER_CASE letters, digits and underscores, starting with a letter")
    return key


def validate_value(value: str, key: str) -> None:
    if value == "":
        raise EnvSetError(f"{key}: empty value; nothing written")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise EnvSetError(f"{key}: value contains a newline or NUL byte, which .env cannot hold; nothing written")
    if value != value.strip():
        print(f"warning: {key}: value has leading or trailing whitespace; it is stored exactly as typed", file=sys.stderr)


def quote(value: str) -> str:
    """Double-quote for both bash `source` and envfile.py: escape \\ \" $ and backtick."""
    out = []
    for ch in value:
        out.append("\\" + ch if ch in '\\"$`' else ch)
    return '"' + "".join(out) + '"'


# ------------------------------------------------------------------ file ops

def _quoted_end(lines, i, pos):
    """Index of the line holding the closing quote of the value opened at lines[i][pos]; i if unterminated."""
    q, j, k = lines[i][pos], i, pos + 1
    while j < len(lines):
        s = lines[j]
        while k < len(s):
            if q == '"' and s[k] == "\\":
                k += 2
                continue
            if s[k] == q:
                return j
            k += 1
        j, k = j + 1, 0
    return i


def _assignment_spans(lines, key):
    """Return [(start, end_inclusive)] of active `KEY=...` assignments, multi-line quotes included."""
    spans, i = [], 0
    while i < len(lines):
        m = ACTIVE_RE.match(lines[i])
        if not m or m.group(1) != key:
            i += 1
            continue
        rest = lines[i][m.end():]
        pos = m.end() + (len(rest) - len(rest.lstrip(" \t")))
        end = _quoted_end(lines, i, pos) if lines[i][pos:pos + 1] in ('"', "'") else i
        spans.append((i, end))
        i = end + 1
    return spans


def update_text(text: str, key: str, value: str) -> str:
    """Replace the first active KEY assignment (drop later duplicates) or append one."""
    lines = text.splitlines(keepends=True)
    new_line = f"{key}={quote(value)}\n"
    spans = _assignment_spans(lines, key)
    if not spans:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
        return "".join(lines)
    out, i = [], 0
    for n, (start, end) in enumerate(spans):
        out.extend(lines[i:start])
        if n == 0:
            out.append(new_line)
        i = end + 1
    out.extend(lines[i:])
    return "".join(out)


def current_value(text: str, key: str) -> str:
    return envfile.parse(text).get(key, "")


def blank_example(text: str) -> str:
    """.env.example -> fresh .env body: comments kept, active values blanked."""
    out = []
    for line in text.splitlines(keepends=True):
        m = ACTIVE_RE.match(line)
        if m:
            out.append(f'{m.group(1)}=""\n')
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out)


def create_env_file(path: Path, example: Path) -> None:
    body = blank_example(example.read_text(encoding="utf-8")) if example.is_file() else "# NocAgent environment (created by scripts/env_set.py)\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def check_perms(path: Path, fix: bool) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise EnvSetError(f"{path} is a symlink; refusing to write through it")
    if not stat.S_ISREG(st.st_mode):
        raise EnvSetError(f"{path} is not a regular file")
    if st.st_mode & 0o077:
        if not fix:
            raise EnvSetError(f"{path} is readable by group/other (mode {stat.S_IMODE(st.st_mode):04o}); "
                              f"run `chmod 600 {path}` or pass --fix-perms")
        os.chmod(path, 0o600)
        print(f"fixed permissions on {path} (now 0600)", file=sys.stderr)


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # make the rename durable; not available everywhere
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# ------------------------------------------------------------ tiers / missing

def parse_example(path: Path):
    """Return [(name, tier, required)] from .env.example (active and commented variable lines)."""
    entries, tier, last_comment = [], 0, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        h = TIER_RE.match(line)
        if h:
            tier, last_comment = int(h.group(1)), ""
            continue
        m = ACTIVE_RE.match(line) or COMMENTED_RE.match(line.lstrip())
        if m:
            entries.append((m.group(1), tier, bool(REQUIRED_RE.search(last_comment))))
            last_comment = ""
            continue
        last_comment = line if line.lstrip().startswith("#") else ""
    return entries


def list_missing(env_file: Path, example: Path, tier=None, exact=False):
    entries = parse_example(example)
    values = {}
    if env_file.is_file():
        try:
            values = envfile.parse(env_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            values = {}
    missing = []
    for name, t, required in entries:
        if not required:
            continue
        if tier is not None and (t != tier if exact else t > tier):
            continue
        if not values.get(name, "").strip():
            missing.append((t, name))
    return missing


# ----------------------------------------------------------------------- CLI

def _has_tty() -> bool:
    try:
        if sys.stdin.isatty():
            return True
        with open("/dev/tty"):
            return True
    except (OSError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="env_set.py", description="Set secrets in .env without exposing them. "
                                "In Claude Code run: ! python3 scripts/env_set.py KEY")
    p.add_argument("keys", nargs="*", metavar="KEY", help="variable(s) to set; each gets a hidden prompt")
    p.add_argument("--generate", action="append", default=[], metavar="KEY",
                   help="write secrets.token_urlsafe(32) to KEY without printing it (repeatable)")
    p.add_argument("--force", action="store_true", help="let --generate replace an existing non-empty value")
    p.add_argument("--stdin", action="store_true", help="read the value of the single KEY from stdin instead of prompting")
    p.add_argument("--list-missing", action="store_true", help="print REQUIRED names that are unset (never values)")
    p.add_argument("--tier", type=int, metavar="N", help="with --list-missing: tiers 0..N (see --exact)")
    p.add_argument("--exact", action="store_true", help="with --tier: only tier N")
    p.add_argument("--env-file", metavar="PATH", help="target file (default: $NOC_DOTENV or <repo>/.env)")
    p.add_argument("--example", metavar="PATH", help="template (default: <repo>/.env.example)")
    p.add_argument("--fix-perms", action="store_true", help="chmod a group/world-readable .env to 0600 instead of refusing")
    return p


def _target(args) -> Path:
    if args.env_file:
        return Path(args.env_file)
    override = os.environ.get("NOC_DOTENV", "").strip()
    return Path(override) if override else ROOT / ".env"


def _prepare(env_file: Path, example: Path, fix: bool) -> None:
    if not env_file.exists() and not env_file.is_symlink():
        if not env_file.parent.is_dir():
            raise EnvSetError(f"directory {env_file.parent} does not exist")
        create_env_file(env_file, example)
        print(f"created {env_file} (mode 0600) from {example.name if example.is_file() else 'an empty template'}", file=sys.stderr)
    check_perms(env_file, fix)


def _store(env_file: Path, key: str, value: str, verb: str) -> None:
    text = env_file.read_text(encoding="utf-8")
    atomic_write(env_file, update_text(text, key, value))
    print(f"{verb} {key} ({len(value)} chars)")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    env_file = _target(args)
    example = Path(args.example) if args.example else ROOT / ".env.example"
    try:
        if args.list_missing:
            if args.keys or args.generate:
                raise EnvSetError("--list-missing takes no KEY arguments")
            if not example.is_file():
                raise EnvSetError(f"{example} not found")
            if env_file.is_file() and os.stat(env_file).st_mode & 0o077:
                print(f"warning: {env_file} is readable by group/other; chmod 600 it", file=sys.stderr)
            missing = list_missing(env_file, example, args.tier, args.exact)
            for t, name in missing:
                print(f"tier {t}: {name}")
            return 1 if missing else 0

        keys = [validate_key(k) for k in args.keys]
        gen = [validate_key(k) for k in args.generate]
        if not keys and not gen:
            raise EnvSetError("give at least one KEY, --generate KEY, or --list-missing (see --help)")
        if args.stdin and len(keys) != 1:
            raise EnvSetError("--stdin needs exactly one KEY")
        if keys and not args.stdin and not _has_tty():
            raise EnvSetError("no terminal for a hidden prompt. Run it yourself (in Claude Code: `! python3 scripts/env_set.py KEY`) "
                              "or pipe the value with --stdin")

        _prepare(env_file, example, args.fix_perms)

        for key in gen:
            if current_value(env_file.read_text(encoding="utf-8"), key) and not args.force:
                raise EnvSetError(f"{key} already has a value; pass --force to replace it with a new random one")
            _store(env_file, key, secrets.token_urlsafe(32), "generated")
        for key in keys:
            if args.stdin:
                value = sys.stdin.readline().rstrip("\r\n")
            else:
                value = getpass.getpass(f"{key} (hidden): ")
            validate_value(value, key)
            _store(env_file, key, value, "set")
        return 0
    except EnvSetError as exc:
        print(f"env_set: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("env_set: cancelled; nothing further written", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
