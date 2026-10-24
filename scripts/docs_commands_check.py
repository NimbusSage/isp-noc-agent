#!/usr/bin/env python3
"""Check that what the docs and agent instructions tell people to run actually exists.

Scans README.md, CONTRIBUTING.md, SECURITY.md, AGENTS.md, CLAUDE.md, docs/**/*.md,
mcp-server/README.md, site-pack/README.md and .opencode/skills/**/*.md, and fails when:

  1. a `scripts/<name>.<ext>` file is referenced but does not exist;
  2. a `--flag` written on the same command as `python3 scripts/x.py`, `node scripts/x.mjs`
     or `bash scripts/x.sh` is not mentioned anywhere in that script's source;
  3. an UPPER_SNAKE variable that carries a known settings prefix (NOC_, AGENT_, WEBHOOK_,
     BOUNCE_, PEAK_, NOTIFY_ ...) is named in a doc but is neither read by the code,
     nor listed in .env.example, nor ignored in scripts/env_ignore.txt. This is what catches
     a renamed or mangled variable name.

Optional site-pack files and gitignored helpers (scripts/_*) are not checked.
Exit 0 = consistent, 1 = findings (printed as file:line: message).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs_check  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

DOC_GLOBS = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "AGENTS.md", "CLAUDE.md", "docs/**/*.md",
             "mcp-server/README.md", "site-pack/README.md", ".opencode/skills/**/*.md")

SCRIPT_REF = re.compile(r"(?<![\w./-])((?:\./)?scripts/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|mjs|js|sh))(?![\w])")
COMMAND = re.compile(r"(?:python3|python|node|bash|sh)\s+(?:-[A-Za-z]\s+)*((?:\./)?scripts/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|mjs|js|sh))(?![\w])([^\n`|;&<>]*)")
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")
ENV_TOKEN = re.compile(r"(?<![A-Za-z0-9_<])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_])")

# Documentation placeholders that look like script names but are patterns, not files.
PLACEHOLDER_HINTS = ("<", ">", "*", "{", "}", "...")


def doc_files(root: Path) -> List[Path]:
    seen, out = set(), []
    for pat in DOC_GLOBS:
        for f in sorted(root.glob(pat)):
            if f.is_file() and f not in seen:
                seen.add(f)
                out.append(f)
    return out


def env_prefixes(documented: Iterable[str], min_count: int = 3) -> Set[str]:
    counts: Dict[str, int] = {}
    for name in documented:
        head = name.split("_", 1)[0] + "_"
        counts[head] = counts.get(head, 0) + 1
    return {h for h, n in counts.items() if n >= min_count}


def check(root: Path = ROOT) -> List[Tuple[str, int, str]]:
    findings: List[Tuple[str, int, str]] = []
    patterns, ignore_files, _errors = docs_check.load_env_ignore(root / "scripts" / "env_ignore.txt")
    reads = docs_check.scan_env_reads(root, set(ignore_files))
    documented = docs_check.documented_env_names(root / ".env.example")
    prefixes = env_prefixes(documented)
    known = set(reads) | documented
    source_cache: Dict[Path, str] = {}

    def source(p: Path) -> str:
        if p not in source_cache:
            source_cache[p] = p.read_text(errors="ignore")
        return source_cache[p]

    for f in doc_files(root):
        rel = f.relative_to(root).as_posix()
        text = f.read_text(errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            for m in SCRIPT_REF.finditer(line):
                ref = m.group(1)
                if any(h in ref for h in PLACEHOLDER_HINTS) or "/_" in ref or ref.startswith("scripts/_"):
                    continue
                if not (root / ref.lstrip("./")).exists():
                    findings.append((rel, n, f"references {ref}, which does not exist"))
            for m in COMMAND.finditer(line):
                ref, rest = m.group(1), m.group(2)
                target = root / ref.lstrip("./")
                if not target.exists() or any(h in ref for h in PLACEHOLDER_HINTS):
                    continue
                src = source(target)
                for flag in FLAG.findall(rest):
                    if flag in ("--help", "--version"):
                        continue
                    if flag not in src:
                        findings.append((rel, n, f"{ref} has no {flag} option"))
            for m in ENV_TOKEN.finditer(line):
                name = m.group(1)
                head = name.split("_", 1)[0] + "_"
                if head not in prefixes or name in known or docs_check.is_ignored(name, patterns):
                    continue
                findings.append((rel, n, f"names {name}, which no code reads and .env.example does not list"))
    return findings


def main() -> int:
    findings = check()
    for rel, n, msg in findings:
        print(f"{rel}:{n}: {msg}")
    print(f"docs commands check: {'OK' if not findings else str(len(findings)) + ' finding(s)'} "
          f"({len(doc_files(ROOT))} files)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
