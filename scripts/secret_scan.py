#!/usr/bin/env python3
"""secret_scan.py - the one leak/secret scanner (stdlib only, no network).

Reused by: scripts/doctor.py (portability_scan), .githooks/pre-commit and
pre-push, CI (.github/workflows/ci.yml) and the editor hook (import it).

CLI
  secret_scan.py --staged               added lines of `git diff --cached`
  secret_scan.py --tracked              every file in `git ls-files`
  secret_scan.py --stdin --path P       text on stdin, labelled as path P
  secret_scan.py PATH [PATH...]         files / directories
  options: --json  --denylist FILE  --profile FILE  --no-profile
  exit: 0 clean, 1 findings, 2 usage error

Python API (import secret_scan)
  scan_text(text, path, config=None)  -> list[Finding]
  scan_paths(paths, config=None, require_denylist=True, base=None) -> list[Finding]
  scan_paths_detailed(...)            -> ScanResult (findings, suppressions, ...)
  Finding(path, line, rule, snippet)  snippet is cut to at most 4 chars + "..."
                                      so a finding never echoes a full secret.

Rules
  Hashed deny-list ("denylist"): lower-cased [A-Za-z0-9]+ tokens of >= 4 chars
  (camelCase / letter-digit pieces, and up to 4 adjacent words joined) are
  sha256-hashed and looked up in scripts/leak_denylist.sha256 (one hex per
  line, "#" comments; build it with scripts/build_denylist.py from word lists
  that live OUTSIDE the repo). site-pack/profile.json `org.portability_tokens`
  (read only that key) are matched in plain text ("portability-token").
  An absent / empty list is itself a finding ("denylist-empty") in
  --tracked/--staged mode: the scanner fails closed.

  Structural rules: lat/long decimals, MAC addresses (except AA:BB:CC:* and
  00:00:00:*), Google sheet/drive ids, UUIDs, AWS/GitHub/Slack/Google/OpenAI
  keys, JWTs, Bearer values, private-key blocks, URL credentials and secret
  query parameters, secrets assigned a literal (password / passwd / pwd /
  passphrase / secret / token / api_key / community / psk), sshpass given a literal
  password, BGP remote AS numbers, e-mail addresses, IPv4 (RFC1918 / CGNAT / public
  outside documentation blocks), scrub-damage artifacts, forbidden file names.

Suppression: put `secret-scan:allow(<reason>)` on the line. The reason is
mandatory (a bare marker is itself a finding and suppresses nothing). A
suppression never hides an identifier written inside its own reason text.
--json reports how many suppressions were used.

Skipped paths: package-lock.json / npm-shrinkwrap.json / *.lock / pnpm-lock,
node_modules/, .git/, scripts/leak_denylist.sha256, LICENSE, binary files.
docs/n8n/*.json is scanned by every rule except `uuid` (n8n node ids are
random UUIDs by design); its webhookId values are flagged separately.

Known limits: encoded text (base64 / hex) and translated or misspelled
identifiers are not caught; gitleaks in CI covers generic entropy secrets.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DEFAULT_DENYLIST = SCRIPT_DIR / "leak_denylist.sha256"
DEFAULT_PROFILE = ROOT / "site-pack" / "profile.json"

MIN_TOKEN = 4          # shortest token that is hashed
MAX_TOKEN = 48         # longer tokens are never hashed (no deny-list entry is that long)
MAX_PHRASE = 4         # adjacent words joined for multi-word deny-list entries
MAX_FILE_BYTES = 8_000_000

# --------------------------------------------------------------------------
# data types
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    snippet: str

    def as_dict(self) -> Dict[str, object]:
        return {"path": self.path, "line": self.line, "rule": self.rule, "snippet": self.snippet}

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.snippet}"


@dataclasses.dataclass
class ScanResult:
    findings: List[Finding]
    suppressions: int = 0          # valid reasoned suppression markers seen
    suppressed_findings: int = 0   # findings those markers actually hid
    files_scanned: int = 0
    denylist_entries: int = 0
    elapsed_ms: int = 0

    def as_dict(self) -> Dict[str, object]:
        by_rule: Dict[str, int] = {}
        for f in self.findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        return {
            "ok": not self.findings,
            "findings": [f.as_dict() for f in self.findings],
            "by_rule": dict(sorted(by_rule.items())),
            "suppressions": self.suppressions,
            "suppressed_findings": self.suppressed_findings,
            "files_scanned": self.files_scanned,
            "denylist_entries": self.denylist_entries,
            "elapsed_ms": self.elapsed_ms,
        }


def _snip(matched: str) -> str:
    s = "".join(ch if ch.isprintable() else "?" for ch in matched.strip())
    if not s:
        return "..."
    n = min(4, max(1, len(s) - 1))
    return s[:n] + "..."


# --------------------------------------------------------------------------
# deny-list / config
# --------------------------------------------------------------------------

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_PLACEHOLDER_TOKEN = re.compile(r"^\s*<.*>\s*$")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def phrase_key(entry: str) -> str:
    """Normalise a word-list entry exactly as the scanner normalises text:
    lower-case alphanumeric tokens, concatenated."""
    return "".join(t.lower() for t in _ALNUM_RUN.findall(entry))


@dataclasses.dataclass
class ScanConfig:
    hashes: frozenset = frozenset()
    real_entries: int = 0              # entries not marked "# placeholder"
    denylist_path: Optional[Path] = None
    denylist_error: Optional[str] = None
    plain_words: frozenset = frozenset()          # profile tokens, alnum, >= 4 chars
    token_regex: Optional["re.Pattern[str]"] = None   # other profile tokens
    profile_path: Optional[Path] = None
    memo: Dict[str, bool] = dataclasses.field(default_factory=dict)

    @property
    def denylist_usable(self) -> bool:
        return self.real_entries > 0


def _load_denylist(path: Path) -> Tuple[Set[str], int, Optional[str]]:
    hashes: Set[str] = set()
    real = 0
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return hashes, 0, f"cannot read {path.name}: {e.__class__.__name__}"
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        first = s.split()[0].lower()
        if not _HEX64.match(first):
            continue
        hashes.add(first)
        if "placeholder" not in s.lower():
            real += 1
    return hashes, real, None


def _load_profile_tokens(path: Optional[Path]) -> Tuple[frozenset, Optional["re.Pattern[str]"]]:
    if path is None or not path.exists():
        return frozenset(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tokens = data.get("org", {}).get("portability_tokens", [])
    except (OSError, ValueError, AttributeError):
        return frozenset(), None
    if not isinstance(tokens, list):
        return frozenset(), None
    plain: Set[str] = set()
    other: List[str] = []
    for t in tokens:
        if not isinstance(t, str):
            continue
        t = t.strip()
        if not t or _PLACEHOLDER_TOKEN.match(t):
            continue  # untouched example placeholders must not match the docs
        if t.isalnum() and len(t) >= MIN_TOKEN:
            plain.add(t.lower())
        else:
            parts = [re.escape(p) for p in re.split(r"[ _\-]+", t) if p]
            other.append(r"[ _\-.]?".join(parts))
    rx = None
    if other:
        rx = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(other) + r")(?![A-Za-z0-9])", re.I)
    return frozenset(plain), rx


_CONFIG_CACHE: Dict[tuple, ScanConfig] = {}


def _mtime(p: Optional[Path]) -> int:
    try:
        return p.stat().st_mtime_ns if p else -1
    except OSError:
        return -1


def load_config(denylist: Optional[Path] = None, profile: Union[Path, str, None] = "default") -> ScanConfig:
    """Build (and cache) the scan configuration.

    denylist: path to the hash file (default scripts/leak_denylist.sha256)
    profile:  path to site-pack/profile.json, None to ignore it, default = repo's
    """
    dl = Path(denylist) if denylist else DEFAULT_DENYLIST
    pf = DEFAULT_PROFILE if profile == "default" else (Path(profile) if profile else None)
    key = (str(dl), _mtime(dl), str(pf), _mtime(pf))
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached
    hashes, real, err = _load_denylist(dl)
    plain, rx = _load_profile_tokens(pf)
    cfg = ScanConfig(hashes=frozenset(hashes), real_entries=real, denylist_path=dl,
                     denylist_error=err, plain_words=plain, token_regex=rx, profile_path=pf)
    _CONFIG_CACHE.clear()
    _CONFIG_CACHE[key] = cfg
    return cfg


def denylist_problem(cfg: ScanConfig) -> Optional[str]:
    """None when the deny-list is usable, else a short reason (fail-closed)."""
    if cfg.denylist_error:
        return cfg.denylist_error
    if cfg.real_entries == 0:
        return "deny-list has no entries (build it: scripts/build_denylist.py)"
    return None


# --------------------------------------------------------------------------
# helpers shared by the rules
# --------------------------------------------------------------------------

_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tif", ".tiff", ".pdf", ".zip",
    ".gz", ".tgz", ".bz2", ".xz", ".7z", ".tar", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".webm", ".wav", ".so", ".dll", ".exe", ".bin", ".class", ".jar",
    ".pyc", ".wasm", ".sqlite", ".db", ".enc",
}
_SKIP_BASENAMES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
                   "LICENSE", "LICENSE.md", "LICENSE.txt"}
_SKIP_SEGMENTS = {"node_modules", ".git"}
_SKIP_PATHS = {"scripts/leak_denylist.sha256"}

_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".java", ".go", ".rs", ".rb",
             ".c", ".h", ".cpp", ".cs", ".php", ".kt", ".swift"}
_DOC_EXT = {".md", ".markdown", ".txt", ".rst", ".adoc"}
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|fixtures?)/|\.(?:test|spec)\.[cm]?[jt]sx?$|(?:^|/)test_[^/]*\.py$|_test\.py$")


def _posix(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def should_skip(path: str) -> bool:
    p = _posix(path)
    if p in _SKIP_PATHS or os.path.basename(p) in _SKIP_BASENAMES or p.endswith(".lock"):
        return True
    if _SKIP_SEGMENTS.intersection(p.split("/")):
        return True
    return os.path.splitext(p)[1].lower() in _BINARY_EXT


_PLACEHOLDER_SUBSTR = (
    "changeme", "change-me", "change_me", "example", "xxxx", "****", "....", "...", "\u2026",
    "your", "placeholder", "redacted", "replace", "todo", "fixme", "dummy", "${", "{{", "{env:",
    "%s", "%(", "$(", "-here", "_here", "<", ">",
)
_NON_SECRET_WORDS = {
    "string", "number", "boolean", "bool", "any", "unknown", "never", "void", "object", "null",
    "none", "nil", "undefined", "true", "false", "str", "int", "bytes", "float", "list", "dict",
    "required", "optional", "password", "passwd", "secret", "token", "pass", "public", "private",
    "hidden", "masked", "secure", "yes", "no", "on", "off", "enabled", "disabled", "empty",
    "bearer", "basic",
}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_FAKE_IN_TESTS = re.compile(
    r"(?i)pass|secret|test|fake|dummy|mock|example|sample|hunter|admin|ubnt|demo|foo|bar|abc|123|"
    r"token|tok|key|psk|xyz|refresh|should|sentinel|canary|fixture|^x+$|^y+$|^z+$")


def is_placeholder(v: str) -> bool:
    s = v.strip().strip("\"'`")
    if not s:
        return True
    low = s.lower()
    if s[0] in "$<{%*([@#" or low in _NON_SECRET_WORDS:
        return True
    if s.strip("x*.-_") == "":
        return True
    return any(p in low for p in _PLACEHOLDER_SUBSTR)


def _is_repeated_fixture(v: str) -> bool:
    """An id built from one or two repeated characters (c_0000000000000000) is a fixture, not a real id."""
    chars = {c for c in v.lower() if c.isalnum()}
    return len(v) >= 8 and len(chars) <= 3


def _real_id(v: str) -> bool:
    """Looks like a real generated id (not YOUR_SHEET_ID / <id>)."""
    if is_placeholder(v):
        return False
    has_digit = any(c.isdigit() for c in v)
    has_alpha = any(c.isalpha() for c in v)
    mixed = any(c.islower() for c in v) and any(c.isupper() for c in v)
    return (has_digit and has_alpha) or (mixed and len(v) >= 30)


# --------------------------------------------------------------------------
# structural regexes
# --------------------------------------------------------------------------

_RE_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_RE_GITHUB = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")
_RE_SLACK = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{6,}")
_RE_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{35}")
_RE_GOOGLE_OAUTH = re.compile(
    r"\bGOCSPX-[A-Za-z0-9_-]{20,}|\bya29\.[0-9A-Za-z_-]{30,}|\b1//[0-9A-Za-z_-]{30,}|"
    r"\b\d{8,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
_RE_OPENAI = re.compile(r"\bsk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{32,}")
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_RE_BEARER = re.compile(r"\bBearer[ \t]+([A-Za-z0-9._-]{20,})")
_RE_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_RE_SSHPASS = re.compile(r"\bsshpass[ \t]+-p[ \t]*(\S+)")
_RE_REMOTE_AS = re.compile(r"\bremote[.-]as(?:[ \t]*=[ \t]*|[ \t]+)(\d+)", re.I)
_RE_URL_CREDS = re.compile(
    r"\b[a-z][a-z0-9+.-]{1,15}://([^\s:/@\"'<>{}$`]+):([^\s@/\"'<>{}$`]+)@[A-Za-z0-9.\[\]-]+")
_RE_URL_PARAM = re.compile(
    r"[?&](?:key|token|api_?key|apikey|access_token|auth|secret|password|sig|signature)="
    r"([A-Za-z0-9_%.~-]{16,})", re.I)
_RE_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
_RE_UUID = re.compile(r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])")
_RE_MAC = re.compile(r"(?<![0-9A-Za-z:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Za-z:-])")
_RE_LATLON_PAIR = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{4,}[ \t]*,[ \t]*-?\d{1,3}\.\d{4,}(?!\d)")
_RE_LATLON_LABEL = re.compile(
    r"(?<![A-Za-z])(?:lat|latitude|lon|lng|long|longitude)(?![A-Za-z])[\"']?[ \t]*[:=][ \t]*[\"']?-?\d{1,3}\.\d{4,}",
    re.I)
_RE_GSHEET = re.compile(r"spreadsheets/d/([A-Za-z0-9_-]{15,})")
_RE_GDRIVE = re.compile(r"/d/([A-Za-z0-9_-]{25,})")
_RE_GID_LABEL = re.compile(
    r"(?<![A-Za-z])(?:spreadsheet|sheet|doc|document|folder|drive|calendar|file)[_-]?id[\"']?[ \t]*[:=][ \t]*[\"']?"
    r"([A-Za-z0-9_-]{25,})", re.I)
_RE_IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3}))(?!\d|\.\d)")
_RE_IP_VERSION_CTX = re.compile(r"(?i)(?:version|ver\.?|release|rev|build|v)[ \t]*[:=]?[ \t]*$")
_RE_N8N_WEBHOOK_ID = re.compile(r"\"webhookId\"[ \t]*:[ \t]*\"[^\"]+\"")

# scrub-damage artifacts (left over from automated identifier removal)
_HTML_OR_TYPE_TAGS = {
    "br", "b", "i", "u", "s", "p", "a", "em", "strong", "code", "sub", "sup", "span", "div", "li",
    "ul", "ol", "tr", "td", "th", "pre", "hr", "kbd", "details", "summary", "number", "string",
    "boolean", "void", "any", "unknown", "never", "null", "undefined", "object", "t", "k", "v",
    "u8", "int", "float", "bytes", "str", "bool", "dict", "list",
}
_RE_MIDWORD = re.compile(r"[A-Za-z]<([a-z0-9-]+)>[A-Za-z]")
_RE_SCRUB_NAMES = re.compile(r"<(?:donor-router|tech-init|site-a|site-b|flat-site)>")
_RE_SCRUB_IP = re.compile(r"10\.NNN")
_RE_EMPTY_PARENS = re.compile(r"(?<=\s)\([ \t]*\)")
_RE_PAREN_COLON = re.compile(r"\(:[ \t]")
_RE_SECTION_SPLIT = re.compile(r"section split, \.")

# assignment of a literal to a sensitive-looking name
_SENS = r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|apikey|community|psk)"
_RE_SENSKEY = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9_.\-]*" + _SENS + r"[0-9]*)([\"'`]?)(?![A-Za-z0-9_])", re.I)
_RE_OP = re.compile(r"[ \t]*(=>|:=|=(?![=~])|:(?![:=]))[ \t]*")
_RE_ANNOT_ASSIGN = re.compile(r"[A-Za-z_][\w.|]*(?:\[[^\]\n]*\])?[ \t]*=(?![=>])[ \t]*")
_RE_BARE_VALUE = re.compile(r"[^\s,;\"'`)\]}]*")

_DOC_ASNS = [(64496, 64511), (65536, 65551)]
_EMAIL_OK_DOMAINS = {"example.com", "example.org", "example.net", "example.edu",
                     "users.noreply.github.com", "noreply.github.com", "anthropic.com",
                     "libssh.org", "openssh.com"}
_EMAIL_OK_TLDS = {"example", "invalid", "test", "localhost", "local", "internal"}
_FORBIDDEN_NAME = re.compile(
    r"(?:^|/)\.env(?:\.(?!example$)[^/]*)?$|\.(?:pem|p12|pfx|key|keystore|jks)$|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$|"
    r"(?:^|/)credentials(?:\.(?!example\.)[^/]*)?\.(?:json|enc)(?:\.[^/]*)?$|(?:^|/)\.secrets/|^backups/|^logs/|^archive/")


def _forbidden_file(path: str) -> bool:
    p = _posix(path)
    if p.startswith("site-pack/"):
        rest = p[len("site-pack/"):]
        if rest == "README.md" or rest.startswith("example/") or ".example." in os.path.basename(rest):
            return False
        return True
    base = os.path.basename(p)
    if ".example" in base and not base.endswith((".pem", ".key", ".p12")):
        return False
    return bool(_FORBIDDEN_NAME.search(p))


def _classify_ip(m: "re.Match[str]", text: str) -> Optional[str]:
    parts = m.group(1).split(".")
    try:
        a, b, c, d = (int(x) for x in parts)
    except ValueError:
        return None
    if max(a, b, c, d) > 255 or any(len(x) > 1 and x.startswith("0") for x in parts):
        return None  # not an address (or zero-padded version-ish)
    if a in (0, 127) or a >= 224 or (a, b) == (169, 254):
        return None
    if (a, b, c) in ((192, 0, 2), (198, 51, 100), (203, 0, 113)) or (a == 198 and b in (18, 19)):
        return None
    if m.group(1) in ("1.1.1.1", "1.0.0.1", "9.9.9.9", "1.3.6.1") or (a, b, c) in ((8, 8, 8), (8, 8, 4)):
        return None
    if _RE_IP_VERSION_CTX.search(text[max(0, m.start() - 12):m.start()]):
        return None
    after = text[m.end():m.end() + 4]
    if (a, b, c, d) in ((10, 0, 0, 0), (172, 16, 0, 0), (192, 168, 0, 0)) and re.match(r"/(?:8|12|16)\b", after):
        return None  # the RFC1918 block definitions themselves
    if a == 10:
        return None if (b == 0 or (b == 1 and c == 2)) else "rfc1918-ip"
    if a == 172 and 16 <= b <= 31:
        return "rfc1918-ip"
    if a == 192 and b == 168:
        return None if c in (1, 2, 3, 88) else "rfc1918-ip"
    if a == 100 and 64 <= b <= 127:
        return "cgnat-ip"
    return "public-ip"


def _uuid_is_fake(u: str) -> bool:
    return len(set(u.replace("-", "").lower())) <= 6


_FAKE_COMMON = re.compile(
    r"(?i)^(?:ubnt|admin\d*|root|user|guest|default|test(?:ing)?\d*|pass\d*|passwd\d*|password\d*|secret\d*|"
    r"token\d*|hunter2|foo|bar|baz|qux|x+|y+|z+|abc\d*|\d{1,6}|changeme\d*|s3cr3t|letmein|qwerty|public|private)$")


_ALWAYS_OK_LITERALS = {"ubnt", "public", "private"}     # vendor / protocol defaults, not secrets


def _is_fake_literal(val: str, is_test: bool, relaxed: bool) -> bool:
    """Factory defaults and obvious fixture values (tests, docs and *.example.* files)."""
    if val.lower() in _ALWAYS_OK_LITERALS:
        return True
    if (relaxed or is_test) and _FAKE_COMMON.match(val):
        return True
    return is_test and bool(_FAKE_IN_TESTS.search(val))


def _fence_ranges(text: str) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    pos = 0
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            if start is None:
                start = pos
            else:
                out.append((start, pos + len(line)))
                start = None
        pos += len(line) + 1
    if start is not None:
        out.append((start, len(text)))
    return out


def _literal_secrets(text: str, starts: List[int], kind: str, is_test: bool,
                     is_example: bool) -> List[Tuple[int, str]]:
    """Secrets assigned a literal: password / token / community / psk ... = "value"."""
    out: List[Tuple[int, str]] = []
    for m in _RE_SENSKEY.finditer(text):
        key_q = m.group(2)
        eol = text.find("\n", m.end())
        rest = text[m.end(): eol if eol != -1 else len(text)]
        mo = _RE_OP.match(rest)
        if not mo:
            continue
        op = mo.group(1)
        adjacent = mo.group(0) == op          # key=value with no spaces
        vstart = mo.end()
        if op == ":":
            ma = _RE_ANNOT_ASSIGN.match(rest, vstart)   # `password: str = "x"`
            if ma:
                op, vstart, adjacent = "=", ma.end(), False
        quoted = False
        if vstart < len(rest) and rest[vstart] in "\"'`":
            close = rest.find(rest[vstart], vstart + 1)
            if close == -1:
                continue
            val, quoted = rest[vstart + 1:close], True
        else:
            val = _RE_BARE_VALUE.match(rest, vstart).group()
        if not val or is_placeholder(val) or _ENV_NAME.match(val):
            continue
        if len(re.findall(r"[A-Za-z0-9]", val)) < 3:
            continue        # regex fragments, masks, punctuation
        if val.startswith(("/", "./", "~/", "http")) or (not quoted and ("(" in val or val.isdigit())):
            continue        # path / url / call expression / bare number
        if quoted and re.search(r"\s", val):
            continue        # prose, not a credential
        if _is_fake_literal(val, is_test, is_example or kind == "doc"):
            continue
        if not quoted:
            ls = starts[bisect.bisect_right(starts, m.start()) - 1]
            inside = _in_string(text[ls:m.start()], key_q)
            if kind == "code" and not (inside and adjacent and op == "="):
                continue            # `token = os.environ.get(...)`-style code
            if kind == "doc" and not (adjacent and op == "="):
                continue            # prose: "Password: required", "PSK = donor key"
        out.append((m.start(), m.group()))
    return out


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

_SUPPRESS = re.compile(r"secret-scan:allow\(([^)\n]*)\)")


def _normalise(text: str) -> str:
    if not text.isascii():
        text = unicodedata.normalize("NFKC", text.translate(_ZERO_WIDTH))
    return text


def _line_starts(text: str) -> List[int]:
    starts = [0]
    find = text.find
    i = find("\n")
    while i != -1:
        starts.append(i + 1)
        i = find("\n", i + 1)
    return starts


def _file_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _CODE_EXT:
        return "code"
    if ext in _DOC_EXT:
        return "doc"
    return "config"


def _in_string(prefix: str, key_quote: str) -> bool:
    prefix = re.sub(r"\\.", "", prefix)
    for q in "\"'`":
        n = prefix.count(q)
        if q == key_quote and n % 2 == 1:
            n -= 1  # the opening quote belongs to the (quoted) key itself
        if n % 2 == 1:
            return True
    return False


def _denylist_hits(text: str, cfg: ScanConfig) -> List[Tuple[int, str]]:
    """(offset, matched text) for every hashed deny-list / plain profile-token hit."""
    out: List[Tuple[int, str]] = []
    hashes, plain, memo = cfg.hashes, cfg.plain_words, cfg.memo
    if not hashes and not plain:
        return out

    def hit(tok: str) -> bool:
        r = memo.get(tok)
        if r is None:
            r = tok in plain or hash_token(tok) in hashes
            memo[tok] = r
        return r

    words: List[Tuple[int, int, str, int, bool]] = []   # start, end, lowered, run id, is_piece
    run_id = 0
    for m in _ALNUM_RUN.finditer(text):
        run = m.group()
        s, e = m.span()
        if MIN_TOKEN <= len(run) <= MAX_TOKEN and hit(run.lower()):
            out.append((s, run))
        pieces = _PIECE.findall(run) if (not run.islower() or not run.isalpha()) else None
        if pieces and len(pieces) > 1:
            pos = s
            for p in pieces:
                words.append((pos, pos + len(p), p.lower(), run_id, True))
                pos += len(p)
        else:
            words.append((s, e, run.lower(), run_id, False))
        run_id += 1
    n = len(words)
    for i in range(n):
        s0, e0, w0, r0, piece = words[i]
        if piece and len(w0) >= MIN_TOKEN and hit(w0):
            out.append((s0, w0))        # one piece of a camelCase / letter-digit run
        joined = w0
        prev_end, prev_run = e0, r0
        for j in range(i + 1, min(n, i + MAX_PHRASE)):
            sj, ej, wj, rj, _p = words[j]
            gap = text[prev_end:sj]
            if rj == prev_run:
                if gap:
                    break
            elif len(gap) > 3 or gap.strip(" \t_-."):
                break
            joined += wj
            if len(joined) > MAX_TOKEN:
                break
            if len(joined) >= MIN_TOKEN and hit(joined):
                out.append((s0, text[s0:ej]))
            prev_end, prev_run = ej, rj
    return out


def _scan_core(text: str, path: str, cfg: ScanConfig,
               lineno_map: Optional[Sequence[int]] = None) -> Tuple[List[Finding], int, int]:
    path = _posix(path)
    text = _normalise(text)
    starts = _line_starts(text)
    kind = _file_kind(path)
    is_test = bool(_TEST_PATH.search(path))
    is_n8n = path.startswith("docs/n8n/") and path.endswith(".json")
    is_example = ".example." in os.path.basename(path) or "/example/" in "/" + path

    def lineno(pos: int) -> int:
        idx = bisect.bisect_right(starts, pos) - 1
        return lineno_map[idx] if lineno_map is not None and idx < len(lineno_map) else idx + 1

    def line_text(pos: int) -> str:
        idx = bisect.bisect_right(starts, pos) - 1
        end = starts[idx + 1] - 1 if idx + 1 < len(starts) else len(text)
        return text[starts[idx]:end]

    raw: List[Tuple[int, str, str]] = []     # (pos, rule, matched)

    def add(pos: int, rule: str, matched: str) -> None:
        raw.append((pos, rule, matched))

    # -- suppression markers -------------------------------------------------
    supp_lines: Set[int] = set()
    marker_spans: List[Tuple[int, int]] = []
    supp_count = 0
    if "secret-scan:allow" in text:
        for m in _SUPPRESS.finditer(text):
            reason = m.group(1).strip()
            if reason.startswith("<") and reason.endswith(">"):
                continue  # documentation of the syntax itself
            if len(reason) < 3 or not re.search(r"[A-Za-z]{2}", reason) or reason.lower() in ("reason", "why", "ok", "todo"):
                add(m.start(), "suppression-no-reason", m.group())
                continue
            supp_count += 1
            marker_spans.append(m.span())
            supp_lines.add(lineno(m.start()))

    # -- hashed deny-list + profile tokens ------------------------------------
    for pos, matched in _denylist_hits(text, cfg):
        add(pos, "denylist", matched)
    if cfg.token_regex is not None:
        for m in cfg.token_regex.finditer(text):
            add(m.start(), "portability-token", m.group())

    # -- secrets with fixed shapes --------------------------------------------
    for rx, rule in ((_RE_AWS, "aws-key"), (_RE_GITHUB, "github-token"), (_RE_SLACK, "slack-token"),
                     (_RE_GOOGLE_KEY, "google-api-key"), (_RE_GOOGLE_OAUTH, "google-oauth"),
                     (_RE_OPENAI, "api-key"), (_RE_JWT, "jwt"), (_RE_PEM, "private-key")):
        for m in rx.finditer(text):
            add(m.start(), rule, m.group())
    for m in _RE_BEARER.finditer(text):
        if not is_placeholder(m.group(1)) and not (is_test and _FAKE_IN_TESTS.search(m.group(1))):
            add(m.start(), "bearer-token", m.group())
    for m in _RE_SSHPASS.finditer(text):
        pw = m.group(1).strip("\"'")
        if not is_placeholder(pw) and pw.lower() not in _ALWAYS_OK_LITERALS:
            add(m.start(), "sshpass-literal", m.group())
    for m in _RE_REMOTE_AS.finditer(text):
        asn = int(m.group(1))
        if not any(lo <= asn <= hi for lo, hi in _DOC_ASNS):
            add(m.start(), "bgp-remote-as", m.group())
    for m in _RE_URL_CREDS.finditer(text):
        if not is_placeholder(m.group(1)) and not is_placeholder(m.group(2)) \
                and not (is_test and _FAKE_IN_TESTS.search(m.group(2))):
            add(m.start(), "url-credentials", m.group())
    for m in _RE_URL_PARAM.finditer(text):
        if not is_placeholder(m.group(1)):
            add(m.start(), "url-secret-param", m.group())

    # -- identifiers -------------------------------------------------------------
    for m in _RE_EMAIL.finditer(text):
        local, domain = m.group(1), m.group(2).lower()
        tld = domain.rsplit(".", 1)[-1]
        if domain in _EMAIL_OK_DOMAINS or tld in _EMAIL_OK_TLDS or local == "git" \
                or domain.endswith(".example.com") or is_placeholder(local) \
                or _is_repeated_fixture(local):
            continue
        add(m.start(), "email", m.group())
    if not is_n8n:
        for m in _RE_UUID.finditer(text):
            if not _uuid_is_fake(m.group()):
                add(m.start(), "uuid", m.group())
    for m in _RE_MAC.finditer(text):
        pre = m.group().replace("-", ":").upper()[:8]
        if pre not in ("AA:BB:CC", "00:00:00"):
            add(m.start(), "mac-address", m.group())
    for rx in (_RE_LATLON_PAIR, _RE_LATLON_LABEL):
        for m in rx.finditer(text):
            add(m.start(), "lat-long", m.group())
    for m in _RE_GSHEET.finditer(text):
        if _real_id(m.group(1)):
            add(m.start(), "google-id", m.group())
    for m in _RE_GDRIVE.finditer(text):
        if _real_id(m.group(1)):
            add(m.start(), "google-id", m.group())
    for m in _RE_GID_LABEL.finditer(text):
        if _real_id(m.group(1)):
            add(m.start(), "google-id", m.group())
    if is_n8n:
        for m in _RE_N8N_WEBHOOK_ID.finditer(text):
            add(m.start(), "n8n-webhook-id", m.group())
    for m in _RE_IPV4.finditer(text):
        rule = _classify_ip(m, text)
        if rule:
            add(m.start(), rule, m.group(1))

    # -- scrub damage ----------------------------------------------------------
    for m in _RE_MIDWORD.finditer(text):
        if m.group(1) not in _HTML_OR_TYPE_TAGS:
            add(m.start(), "scrub-midword-placeholder", m.group())
    for rx, rule in ((_RE_SCRUB_NAMES, "scrub-artifact"), (_RE_SCRUB_IP, "scrub-ip-mask"),
                     (_RE_PAREN_COLON, "scrub-damage"), (_RE_SECTION_SPLIT, "scrub-damage")):
        for m in rx.finditer(text):
            add(m.start(), rule, m.group())
    if kind == "doc":
        fences = _fence_ranges(text)
        for m in _RE_EMPTY_PARENS.finditer(text):
            ls = starts[bisect.bisect_right(starts, m.start()) - 1]
            if text[ls:m.start()].count("`") % 2 == 1 or any(a <= m.start() < b for a, b in fences):
                continue    # inside a code span / fenced block: real code, not prose
            add(m.start(), "scrub-damage", m.group())

    # -- literal assigned to a sensitive name ---------------------------------
    for pos, matched in _literal_secrets(text, starts, kind, is_test, is_example):
        add(pos, "literal-secret", matched)

    # -- resolve to findings --------------------------------------------------
    findings: List[Finding] = []
    seen: Set[Tuple[int, str, str]] = set()
    suppressed = 0
    for pos, rule, matched in sorted(raw, key=lambda r: r[0]):
        ln = lineno(pos)
        if ln in supp_lines and rule != "suppression-no-reason":
            in_marker = any(s <= pos < e for s, e in marker_spans)
            if not (in_marker and rule in ("denylist", "portability-token")):
                suppressed += 1          # an identifier inside a reason is never hidden
                continue
        key = (ln, rule, matched[:6].lower())
        if key in seen:
            continue
        seen.add(key)
        findings.append(Finding(path, ln, rule, _snip(matched)))
    return findings, supp_count, suppressed


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def scan_text(text: str, path: str, config: Optional[ScanConfig] = None) -> List[Finding]:
    """Scan one text blob. `path` is used for labelling and path-based policy."""
    if should_skip(path):
        return []
    cfg = config or load_config()
    return _scan_core(text, path, cfg)[0]


def _read_text(p: Path) -> Optional[str]:
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _expand(paths: Iterable[object], base: Path) -> List[str]:
    out: List[str] = []
    for raw in paths:
        p = Path(str(raw))
        full = p if p.is_absolute() else base / p
        if full.is_dir():
            for sub in sorted(full.rglob("*")):
                if sub.is_file() and not _SKIP_SEGMENTS.intersection(sub.relative_to(full).parts):
                    out.append(_rel(sub, base))
        else:
            out.append(_rel(full, base))
    return out


def _rel(p: Path, base: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _denylist_finding(cfg: ScanConfig) -> Optional[Finding]:
    problem = denylist_problem(cfg)
    if problem is None:
        return None
    name = (cfg.denylist_path or DEFAULT_DENYLIST).name
    return Finding(f"scripts/{name}", 0, "denylist-empty", "fail-closed")


def scan_paths_detailed(paths: Iterable[object], config: Optional[ScanConfig] = None,
                        require_denylist: bool = True, base: Optional[Path] = None) -> ScanResult:
    t0 = time.monotonic()
    cfg = config or load_config()
    base = Path(base) if base else ROOT
    findings: List[Finding] = []
    supp = suppressed = files = 0
    if require_denylist:
        f = _denylist_finding(cfg)
        if f:
            findings.append(f)
    for rel in _expand(paths, base):
        if _forbidden_file(rel):
            findings.append(Finding(rel, 0, "forbidden-file", _snip(os.path.basename(rel))))
        if should_skip(rel):
            continue
        full = base / rel
        if full.is_symlink():
            continue
        text = _read_text(full)
        if text is None:
            continue
        files += 1
        hits = _denylist_hits(_normalise(rel), cfg)
        if hits:
            findings.append(Finding(rel, 0, "denylist-path", _snip(hits[0][1])))
        fs, sc, sp = _scan_core(text, rel, cfg)
        findings.extend(fs)
        supp += sc
        suppressed += sp
    return ScanResult(findings, supp, suppressed, files, len(cfg.hashes),
                      int((time.monotonic() - t0) * 1000))


def scan_paths(paths: Iterable[object], config: Optional[ScanConfig] = None,
               require_denylist: bool = True, base: Optional[Path] = None) -> List[Finding]:
    return scan_paths_detailed(paths, config, require_denylist, base).findings


def tracked_files(repo: Optional[Path] = None) -> List[str]:
    out = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", "-z"], capture_output=True,
                         cwd=str(repo or ROOT), timeout=60)
    if out.returncode != 0:
        raise RuntimeError("git ls-files failed")
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def staged_added_lines(repo: Optional[Path] = None) -> Dict[str, List[Tuple[int, str]]]:
    """{path: [(line_number, text), ...]} of lines ADDED in the index."""
    out = subprocess.run(
        ["git", "-c", "core.quotepath=off", "diff", "--cached", "--unified=0", "--no-color",
         "--no-ext-diff", "--no-textconv", "--diff-filter=ACMR"],
        capture_output=True, cwd=str(repo or Path.cwd()), timeout=60)
    if out.returncode != 0:
        raise RuntimeError("git diff --cached failed: " + out.stderr.decode("utf-8", "replace")[:200])
    files: Dict[str, List[Tuple[int, str]]] = {}
    cur: Optional[str] = None
    ln = 0
    for line in out.stdout.decode("utf-8", "replace").split("\n"):
        if line.startswith("+++ "):
            tgt = line[4:].split("\t")[0]
            cur = tgt[2:] if tgt.startswith("b/") else None
            if cur is not None:
                files.setdefault(cur, [])
        elif line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", line)
            ln = int(m.group(1)) if m else 0
        elif line.startswith("+") and cur is not None and not line.startswith("+++"):
            files[cur].append((ln, line[1:]))
            ln += 1
    return files


def scan_staged(repo: Optional[Path] = None, config: Optional[ScanConfig] = None,
                require_denylist: bool = True) -> ScanResult:
    t0 = time.monotonic()
    cfg = config or load_config()
    findings: List[Finding] = []
    supp = suppressed = files_n = 0
    added = staged_added_lines(repo)
    if require_denylist and added:
        f = _denylist_finding(cfg)
        if f:
            findings.append(f)
    for rel, lines in sorted(added.items()):
        if _forbidden_file(rel):
            findings.append(Finding(rel, 0, "forbidden-file", _snip(os.path.basename(rel))))
        if should_skip(rel) or not lines:
            continue
        files_n += 1
        hits = _denylist_hits(_normalise(rel), cfg)
        if hits:
            findings.append(Finding(rel, 0, "denylist-path", _snip(hits[0][1])))
        text = "\n".join(t for _, t in lines)
        fs, sc, sp = _scan_core(text, rel, cfg, lineno_map=[n for n, _ in lines])
        findings.extend(fs)
        supp += sc
        suppressed += sp
    return ScanResult(findings, supp, suppressed, files_n, len(cfg.hashes),
                      int((time.monotonic() - t0) * 1000))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_result(res: ScanResult, as_json: bool, mode: str) -> None:
    if as_json:
        print(json.dumps(res.as_dict(), indent=2))
        return
    for f in res.findings:
        print(f)
    n_files = len({f.path for f in res.findings})
    if res.findings:
        by_rule: Dict[str, int] = {}
        for f in res.findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        top = ", ".join(f"{k}={v}" for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1]))
        print(f"\nsecret_scan ({mode}): {len(res.findings)} finding(s) in {n_files} file(s) [{top}]; "
              f"{res.suppressions} suppression(s) used", file=sys.stderr)
    else:
        print(f"secret_scan ({mode}): clean - {res.files_scanned} file(s), "
              f"{res.suppressions} suppression(s) used, {res.elapsed_ms} ms", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scan for secrets and org identifiers (see module docstring).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="added lines of the git index")
    mode.add_argument("--tracked", action="store_true", help="all git-tracked files")
    mode.add_argument("--stdin", action="store_true", help="read text from stdin (needs --path)")
    ap.add_argument("paths", nargs="*", help="files or directories to scan")
    ap.add_argument("--path", help="label for --stdin")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--denylist", help="hash list to use (default scripts/leak_denylist.sha256)")
    ap.add_argument("--profile", help="profile.json to read org.portability_tokens from")
    ap.add_argument("--no-profile", action="store_true", help="ignore site-pack/profile.json")
    ap.add_argument("--allow-empty-denylist", action="store_true",
                    help="--staged/--tracked: do not fail closed on an empty deny-list (tests only)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not (args.staged or args.tracked or args.stdin or args.paths):
        ap.print_usage(sys.stderr)
        print("secret_scan: choose --staged, --tracked, --stdin or give paths", file=sys.stderr)
        return 2
    if args.stdin and not args.path:
        print("secret_scan: --stdin needs --path", file=sys.stderr)
        return 2
    profile = None if args.no_profile else (Path(args.profile) if args.profile else "default")
    cfg = load_config(Path(args.denylist) if args.denylist else None, profile)

    try:
        if args.staged:
            res = scan_staged(Path.cwd(), cfg, require_denylist=not args.allow_empty_denylist)
            label = "staged"
        elif args.tracked:
            res = scan_paths_detailed(tracked_files(ROOT), cfg, not args.allow_empty_denylist, ROOT)
            label = "tracked"
        elif args.stdin:
            t0 = time.monotonic()
            data = sys.stdin.read()
            fs, sc, sp = _scan_core(data, args.path, cfg) if not should_skip(args.path) else ([], 0, 0)
            res = ScanResult(fs, sc, sp, 1, len(cfg.hashes), int((time.monotonic() - t0) * 1000))
            label = "stdin"
        else:
            res = scan_paths_detailed(args.paths, cfg, False, Path.cwd())
            label = "paths"
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"secret_scan: {e}", file=sys.stderr)
        return 2
    _print_result(res, args.json, label)
    return 1 if res.findings else 0


if __name__ == "__main__":
    sys.exit(main())
