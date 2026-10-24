#!/usr/bin/env python3
"""
envfile.py - tiny, side-effect-free `.env` reader shared by the ops scripts.

Why: the scripts used to load tokens with `bash -c "source .env && echo $TOKEN"`.
That EVALUATES the file as shell, so a stray `$(...)`, backtick or `;` in any
value runs code as the NOC user. This parser only reads data; nothing is
ever executed or expanded.

Accepted syntax (the intersection of what systemd EnvironmentFile, docker
`--env-file` and the python-dotenv family agree on):

    KEY=value                  unquoted: trimmed; ` #...` starts a comment
    export KEY=value           optional `export ` prefix
    KEY="double quoted"        escapes \\\\ \\" \\n \\r \\t \\$ \\` are decoded;
                               may span lines; anything after the closing
                               quote (an inline comment) is ignored
    KEY='single quoted'        literal, no escapes; may span lines
    # comment / blank lines    ignored
    CRLF line endings, UTF-8 BOM, `KEY = value` spacing are tolerated

NOT supported on purpose: `$VAR` / `${VAR}` / `$(cmd)` expansion, line
continuations, `source`/`.` includes. A value that contains them is kept
verbatim as text.

Library use:
    import envfile
    envfile.get("SONAR_TOKEN")           # real environment first, then the .env file
    envfile.load()                        # {} when the file is missing/unreadable

Path: NOC_DOTENV env override, else <repo>/.env. A missing file is not an error.

CLI (debug helper, prints key names only, never values):
    python3 scripts/envfile.py [path]
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DQ_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t", "$": "$", "`": "`"}


def dotenv_path() -> Path:
    override = os.environ.get("NOC_DOTENV", "").strip()
    return Path(override) if override else ROOT / ".env"


def parse(text: str) -> Dict[str, str]:
    """Parse .env text into a dict (last assignment wins). Never raises."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    out: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        i += 1
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export") and len(stripped) > 6 and stripped[6] in " \t":
            stripped = stripped[6:].lstrip()
        eq = stripped.find("=")
        if eq <= 0:
            continue
        key = stripped[:eq].strip()
        if not _KEY_RE.match(key):
            continue
        rest = stripped[eq + 1:].lstrip(" \t")
        if rest.startswith('"'):
            value, i = _read_double(rest[1:], lines, i)
        elif rest.startswith("'"):
            value, i = _read_single(rest[1:], lines, i)
        else:
            value = _read_unquoted(rest)
        out[key] = value
    return out


def _read_unquoted(rest: str) -> str:
    m = re.search(r"\s#", rest)
    if m:
        rest = rest[:m.start()]
    return rest.strip()


def _read_single(first: str, lines: list, i: int):
    buf = []
    start_i = i
    cur = first
    while True:
        close = cur.find("'")
        if close != -1:
            buf.append(cur[:close])
            return "".join(buf), i
        buf.append(cur.rstrip("\r"))
        if i >= len(lines):  # unterminated quote: fall back to the first line, trimmed
            return first.rstrip("\r").strip(), start_i
        buf.append("\n")
        cur = lines[i]
        i += 1


def _read_double(first: str, lines: list, i: int):
    buf = []
    start_i = i
    cur = first
    while True:
        j = 0
        while j < len(cur):
            ch = cur[j]
            if ch == "\\" and j + 1 < len(cur):
                nxt = cur[j + 1]
                if nxt in _DQ_ESCAPES:
                    buf.append(_DQ_ESCAPES[nxt])
                else:
                    buf.append("\\" + nxt)
                j += 2
                continue
            if ch == '"':
                return "".join(buf), i
            buf.append(ch)
            j += 1
        if i >= len(lines):  # unterminated quote: fall back to the first line, trimmed
            return first.rstrip("\r").strip(), start_i
        buf.append("\n")
        cur = lines[i].rstrip("\r")
        i += 1


_CACHE: Dict[str, tuple] = {}


def load(path: Optional[os.PathLike] = None) -> Dict[str, str]:
    """Parse the .env file at `path` (default: dotenv_path()). {} if absent/unreadable."""
    p = Path(path) if path is not None else dotenv_path()
    try:
        st = p.stat()
        key = str(p)
        sig = (st.st_mtime_ns, st.st_size)
        cached = _CACHE.get(key)
        if cached and cached[0] == sig:
            return dict(cached[1])
        data = parse(p.read_text(encoding="utf-8", errors="replace"))
        _CACHE[key] = (sig, data)
        return dict(data)
    except (OSError, ValueError):
        return {}


def get(name: str, default: str = "", path: Optional[os.PathLike] = None) -> str:
    """Value of `name`: real environment first (if non-empty), then the .env file."""
    v = os.environ.get(name)
    if v is not None and v.strip():
        return v.strip()
    fv = load(path).get(name)
    if fv is not None and fv.strip():
        return fv.strip()
    return default


def main(argv: list) -> int:
    path = Path(argv[1]) if len(argv) > 1 else None
    data = load(path)
    print(f"{len(data)} key(s) in {path or dotenv_path()}: " + ", ".join(sorted(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
