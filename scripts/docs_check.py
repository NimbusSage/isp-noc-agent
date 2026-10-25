#!/usr/bin/env python3
"""docs_check.py - verify the operator manual wiring.

Six gates:
1. Links: every local markdown link target in README.md and docs/**/*.md
   (including docs/setup/ and docs/n8n/) must resolve on disk (anchors and
   queries stripped; external schemes ignored; fenced code blocks ignored).
   A README index entry whose file is missing is reported as such, never a
   crash.
2. Index: every top-level docs/*.md must be linked from README.md.
3. Reachability: every file in a docs/ subdirectory (setup/, n8n/, ...) must be
   referenced - by markdown link or by its plain path - from README.md or from
   some docs/**/*.md.

4. Env vars: every environment variable the code reads (see scan_env_reads)
   must be documented in .env.example (a `NAME=` line or a commented
   `# NAME=` line). Undocumented = failure. Documented but never read = a
   warning only. System/derived/internal names live in scripts/env_ignore.txt,
   each with a reason (`NAME  # reason`, `NAME_*` globs, `file:PATH  # reason`).

5. Skills: every .opencode/skills/<name>/ folder must be named in docs/skills.md.

6. Tree: the layout in docs/architecture.md must match `git ls-files` and every
   directory must have a line in docs/tree-notes.txt (scripts/gen_tree.py --write
   refreshes it).

Exit 0 = clean, 1 = anything broken. Run in CI and before doc-heavy commits.
"""

import fnmatch
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"

# Files (basename) or directories (name) exempt from the index/reachability
# gates. Keep this EMPTY unless a doc is intentionally unlisted; every entry
# must name a file that exists in the tree.
COMPLETENESS_EXEMPT: set = set()

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE_RE = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)[^\n]*$", re.S | re.M)
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def local_links(text: str):
    """Yield local link targets (no anchor/query) from markdown text."""
    for m in LINK_RE.finditer(FENCE_RE.sub("", text)):
        target = m.group(1).strip()
        if target.startswith(("<", "#")) or SCHEME_RE.match(target):
            continue
        path = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if path:
            yield path


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------
# Env-var check (gate 4). scan_env_reads() / documented_env_names() are shared
# helpers: scripts/test_docs_check_env.py exercises them on temp trees.
# --------------------------------------------------------------------------
ENV_EXAMPLE = ROOT / ".env.example"
ENV_IGNORE = ROOT / "scripts" / "env_ignore.txt"

# Code roots scanned for env reads (relative to the repo root). Tests, vendored
# and generated trees are skipped.
ENV_SCAN_GLOBS = (
    "server/*.js", "scripts/**/*", "evals/*.py", "mcp-server/src/**/*",
    "mcp-server/scripts/*", "deploy/*", "opencode.json",
)
ENV_SKIP_DIRS = {"node_modules", "dist", "__pycache__", ".git", "logs", "tests"}
ENV_CODE_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".py", ".sh", ".template", ".json"}

_N = r"[A-Z][A-Z0-9_]*"        # any env-looking name (PATH, HOME, ...)
_Q = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"   # quoted literal: must contain an underscore
_JS_RES = (
    re.compile(r"process\.env\.(%s)\b" % _N),
    re.compile(r"process\.env\[\s*['\"](%s)['\"]\s*\]" % _N),
    re.compile(r"\b(?:env|e)\.(%s)\b" % _N),
    re.compile(r"\benv\[\s*['\"](%s)['\"]\s*\]" % _N),
    re.compile(r"""['"`](%s)['"`]""" % _Q),
)
_PY_RES = (
    re.compile(r"(?:environ(?:\.get|\.pop|\.setdefault)?|getenv|envfile\.get|\benv(?:\.get)?)"
               r"\s*[\(\[]\s*['\"](%s)['\"]" % _N),
    re.compile(r"""['"](%s)['"]""" % _Q),
)
_SH_RES = (
    re.compile(r"\$\{(%s)(?::?[-=?+])" % _N),
)
_TEMPLATE_RES = (
    re.compile(r"^\s*Environment=(%s)=" % _N, re.M),
    re.compile(r"\$\{(%s)(?::?[-=?+][^}]*)?\}" % _N),
)
_OPENCODE_RES = (re.compile(r"\{env:(%s)\}" % _N),)


def _res_for(path: Path):
    if path.suffix in (".js", ".mjs", ".cjs", ".ts"):
        return _JS_RES
    if path.suffix == ".py":
        return _PY_RES
    if path.suffix == ".sh":
        return _SH_RES
    if path.suffix == ".template":
        return _TEMPLATE_RES
    if path.name == "opencode.json":
        return _OPENCODE_RES
    return ()


def load_env_ignore(path: Path):
    """Parse env_ignore.txt -> (patterns, files, errors). Every entry needs a `# reason`."""
    patterns, files, errors = [], [], []
    for n, raw in enumerate(read(path).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, _, reason = line.partition("#")
        body = body.strip()
        if not body or not reason.strip():
            errors.append(f"{rel(path)}:{n}: entry needs `NAME  # reason`")
            continue
        (files if body.startswith("file:") else patterns).append(body[5:] if body.startswith("file:") else body)
    return patterns, files, errors


def is_ignored(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in patterns)


def scan_env_reads(root: Path, ignored_files=()):
    """Return {NAME: sorted([relative files])} for every env name the code reads.

    Python/JS/TS count an explicit env access (environ.get, process.env.X,
    env.X, ...) OR any quoted UPPER_SNAKE literal (so helper calls such as
    intVar('X', ...) and name lists are caught). Shell counts `${X:-...}` style
    defaults; systemd templates `Environment=X=`/`${X}`; opencode.json `{env:X}`.
    Over-matching (status strings, dynamic prefixes) is silenced in env_ignore.txt.
    """
    reads = {}
    seen = set()
    for pattern in ENV_SCAN_GLOBS:
        for f in sorted(root.glob(pattern)):
            if not f.is_file() or f in seen:
                continue
            seen.add(f)
            parts = f.relative_to(root).parts
            if set(parts[:-1]) & ENV_SKIP_DIRS:
                continue
            if f.suffix not in ENV_CODE_SUFFIXES or ".test." in f.name or f.name.startswith("test_"):
                continue
            rpath = f.relative_to(root).as_posix()
            if rpath in ignored_files:
                continue
            text = read(f)
            for rx in _res_for(f):
                for m in rx.finditer(text):
                    reads.setdefault(m.group(1), set()).add(rpath)
    return {k: sorted(v) for k, v in reads.items()}


ENV_DOC_RE = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.M)


def documented_env_names(path: Path):
    """Names documented in .env.example: `NAME=` or commented `# NAME=` lines."""
    return set(ENV_DOC_RE.findall(read(path)))


def check_env(root: Path):
    """Return (undocumented {name: files}, unread [names], ignore_errors)."""
    patterns, files, errors = load_env_ignore(root / "scripts" / "env_ignore.txt")
    reads = scan_env_reads(root, set(files))
    documented = documented_env_names(root / ".env.example")
    undocumented = {n: f for n, f in reads.items()
                    if n not in documented and not is_ignored(n, patterns)}
    unread = sorted(n for n in documented if n not in reads and not is_ignored(n, patterns))
    return undocumented, unread, errors


def check_skills_index(root: Path):
    """Skill folders that docs/skills.md never names."""
    skills_doc = root / "docs" / "skills.md"
    text = read(skills_doc) if skills_doc.exists() else ""
    folder = root / ".opencode" / "skills"
    if not folder.is_dir():
        return []
    return sorted(d.name for d in folder.iterdir()
                  if d.is_dir() and (d / "SKILL.md").exists() and f"skills/{d.name}/" not in text)


def check_tree():
    """Problems with the generated layout in docs/architecture.md (empty when current)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gen_tree
    files, notes = gen_tree.tracked(), gen_tree.load_notes()
    issues = gen_tree.problems(files, notes)
    doc = gen_tree.DOC.read_text() if gen_tree.DOC.exists() else ""
    if gen_tree.START not in doc or gen_tree.END not in doc:
        issues.append(f"docs/architecture.md has no {gen_tree.START} ... {gen_tree.END} markers")
    elif doc[doc.index(gen_tree.START): doc.index(gen_tree.END) + len(gen_tree.END)] != gen_tree.block(
            gen_tree.render(gen_tree.build(files, notes), notes)):
        issues.append("docs/architecture.md tree is stale: run python3 scripts/gen_tree.py --write")
    return issues


def main() -> int:
    md_files = [README] if README.exists() else []
    md_files += sorted(DOCS.rglob("*.md")) if DOCS.is_dir() else []
    texts = {f: read(f) for f in md_files}

    broken, missing_index = [], []
    resolved_links = set()          # every existing path linked from README or docs
    readme_links = set()
    for f, text in texts.items():
        for target in local_links(text):
            resolved = (f.parent / target).resolve()
            if not resolved.exists():
                entry = f"{rel(f)} -> {target}"
                bucket = missing_index if f == README and target.startswith("docs/") else broken
                if entry not in bucket:
                    bucket.append(entry)
                continue
            resolved_links.add(rel(resolved))
            if f == README:
                readme_links.add(rel(resolved))

    all_text = "\n".join(texts.values())
    orphans = []
    if DOCS.is_dir():
        for f in sorted(p for p in DOCS.rglob("*") if p.is_file()):
            relpath = rel(f)
            parts = f.relative_to(DOCS).parts
            if f.name in COMPLETENESS_EXEMPT or any(p in COMPLETENESS_EXEMPT for p in parts[:-1]):
                continue
            if len(parts) == 1:
                if f.suffix == ".md" and relpath not in readme_links:
                    orphans.append(f"{relpath} (not linked from README.md)")
            elif relpath not in resolved_links and relpath not in all_text:
                orphans.append(f"{relpath} (not referenced from README.md or any docs page)")

    env_undoc, env_unread, env_errors = check_env(ROOT)
    if env_unread:
        print("WARNING: documented in .env.example but never read by code "
              "(drop it, or add it to scripts/env_ignore.txt with a reason):")
        for n in env_unread:
            print("  " + n)
    if env_errors:
        print("ENV IGNORE LIST ERRORS:")
        for e in env_errors:
            print("  " + e)
    if env_undoc:
        print("ENV VARS READ BY CODE BUT NOT IN .env.example (document them, or add to "
              "scripts/env_ignore.txt with a reason):")
        for n, fs in sorted(env_undoc.items()):
            print(f"  {n}  ({', '.join(fs[:3])}{', ...' if len(fs) > 3 else ''})")

    skills_missing = check_skills_index(ROOT)
    if skills_missing:
        print("SKILLS NOT LISTED IN docs/skills.md:")
        for n in skills_missing:
            print("  " + n)
    tree_issues = check_tree()
    if tree_issues:
        print("LAYOUT TREE:")
        for t in tree_issues:
            print("  " + t)

    if missing_index:
        print("README INDEX ENTRIES WITH NO FILE:")
        for b in missing_index:
            print("  " + b)
    if broken:
        print("BROKEN LINKS:")
        for b in broken:
            print("  " + b)
    if orphans:
        print("UNREFERENCED DOCS:")
        for o in orphans:
            print("  " + o)
    if not (broken or missing_index or orphans or env_undoc or env_errors or skills_missing or tree_issues):
        print(f"docs check OK - {len(md_files)} files link-checked (README + docs/**), "
              "all local links resolve, every docs file indexed or referenced, every skill listed, layout tree current; "
              f"every env var the code reads is in .env.example ({len(env_unread)} warning(s))")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
