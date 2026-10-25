#!/usr/bin/env python3
"""Generate the repository layout shown in docs/architecture.md, so it can never go stale.

The tree comes from `git ls-files`. Each script is described by its own header (module docstring or leading
comment), each doc by its first heading, and each directory or top-level file by docs/tree-notes.txt
(`path :: description`; a leading `+` marks something that exists on your machine but is not in git).

    python3 scripts/gen_tree.py            # print the tree
    python3 scripts/gen_tree.py --write    # refresh the block between the tree markers in docs/architecture.md
    python3 scripts/gen_tree.py --check    # exit 1 if that block is stale or a note is missing or points nowhere

Test files are summarised per directory instead of listed one by one.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "architecture.md"
NOTES = ROOT / "docs" / "tree-notes.txt"
START, END = "<!-- tree:start -->", "<!-- tree:end -->"
COLUMN = 46
MAX_NOTE = 104

CODE_DIRS = ("scripts/", "evals/", "server/", "mcp-server/scripts/", "mcp-server/src/")
TEST_GLOBS = ("test_*.py", "*.test.mjs", "*.test.ts", "*.test.js")
# Directories whose contents are summarised as a count rather than listed.
SUMMARISE = ("mcp-server/tests", "scripts/demo/fixtures", "evals/golden")


def tracked() -> List[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return sorted(f for f in out.split("\n") if f)


def load_notes() -> Dict[str, Tuple[str, bool]]:
    notes: Dict[str, Tuple[str, bool]] = {}
    if NOTES.exists():
        for raw in NOTES.read_text().splitlines():
            if not raw.strip() or raw.startswith("#"):
                continue
            path, _, text = raw.partition(" :: ")
            untracked = path.startswith("+")
            notes[path.lstrip("+").strip()] = (text.strip(), untracked)
    return notes


def clip(text: str) -> str:
    text = " ".join(text.split())
    text = text[:1].upper() + text[1:]
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    if len(text) <= MAX_NOTE:
        return text
    cut = text[:MAX_NOTE].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"^[\w./-]+\.(?:py|mjs|js|ts|sh)\s*[—–:-]+\s*", "", text)   # drop a repeated file name
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


def script_header(path: Path) -> Optional[str]:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    if path.suffix == ".py":
        text = "\n".join(lines[:300])
        m = re.match(r'(?:#[^\n]*\n)*\s*(?:r|u)?("""|\'\'\')(.*?)\1', text, re.S)
        if m:
            body = m.group(2).strip().splitlines()
            return first_sentence(" ".join(body[:3])) if body else None
    elif path.suffix in (".mjs", ".js", ".ts"):
        body: List[str] = []
        for line in lines[:40]:
            s = line.strip()
            if s.startswith("#!") or not s:
                if body:
                    break
                continue
            if s.startswith(("//", "/*", "*")):
                body.append(s.lstrip("/*").strip())
                if len(body) >= 3 and body[-1] == "":
                    break
            else:
                break
        body = [b for b in body if b]
        return first_sentence(" ".join(body[:3])) if body else None
    elif path.suffix == ".sh":
        for line in lines[:12]:
            s = line.strip()
            if s.startswith("#") and not s.startswith("#!"):
                return first_sentence(s.lstrip("# ").strip())
    return None


def doc_title(path: Path) -> Optional[str]:
    try:
        for line in path.read_text(errors="ignore").splitlines()[:20]:
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return None


def is_test(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in TEST_GLOBS)


class Node:
    def __init__(self, name: str, path: str, is_dir: bool):
        self.name, self.path, self.is_dir = name, path, is_dir
        self.children: Dict[str, "Node"] = {}


def build(files: List[str], notes: Dict[str, Tuple[str, bool]]) -> Node:
    root = Node("NocAgent", "", True)
    for f in files:
        node, parts = root, f.split("/")
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            sub = "/".join(parts[: i + 1])
            node = node.children.setdefault(part, Node(part, sub, not last))
    for path, (_, untracked) in notes.items():
        if untracked:       # exists locally but is not in git
            parts = path.rstrip("/").split("/")
            node = root
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                node = node.children.setdefault(part, Node(part, "/".join(parts[: i + 1]), (not last) or path.endswith("/")))
    return root


def annotation(node: Node, notes: Dict[str, Tuple[str, bool]]) -> str:
    key = node.path + ("/" if node.is_dir else "")
    for k in (key, node.path):
        if k in notes:
            return clip(notes[k][0])
    p = ROOT / node.path
    if not node.is_dir and p.exists():
        if node.path.startswith(CODE_DIRS) and p.suffix in (".py", ".mjs", ".sh", ".js", ".ts"):
            h = script_header(p)
            return clip(h) if h else ""
        if node.path.startswith("docs/") and p.suffix == ".md":
            t = doc_title(p)
            return clip(t) if t else ""
    return ""


def render(root: Node, notes: Dict[str, Tuple[str, bool]]) -> List[str]:
    lines = [f"{root.name}/"]

    def emit(prefix: str, label: str, note: str) -> None:
        text = prefix + label
        lines.append(text + (" " * max(2, COLUMN - len(text)) + "# " + note if note else ""))

    def walk(node: Node, prefix: str) -> None:
        kids = sorted(node.children.values(), key=lambda n: (not n.is_dir, n.name.lower()))
        tests = [k for k in kids if not k.is_dir and is_test(k.name)]
        kids = [k for k in kids if k not in tests]
        entries: List[Tuple[str, str, Optional[Node]]] = []
        for k in kids:
            if k.is_dir and k.path in SUMMARISE:
                n = sum(1 for _ in _all_files(k))
                entries.append((k.name + "/", f"{n} files", None))
            elif k.is_dir and k.path.startswith(".opencode/skills/") and k.path.count("/") == 2:
                refs = [c for c in k.children.values() if c.is_dir]
                extra = "".join(f", {c.name}/ ({sum(1 for _ in _all_files(c))} files)" for c in refs)
                entries.append((k.name + "/", "SKILL.md" + extra, None))
            else:
                entries.append((k.name + ("/" if k.is_dir else ""), annotation(k, notes), k))
        if tests:
            patterns = sorted({g for g in TEST_GLOBS if any(fnmatch.fnmatch(t.name, g) for t in tests)})
            entries.append((f"({len(tests)} test files)", "matching " + ", ".join(patterns), None))
        for i, (label, note, child) in enumerate(entries):
            last = i == len(entries) - 1
            emit(prefix + ("└── " if last else "├── "), label, note)
            if child is not None and child.is_dir:
                walk(child, prefix + ("    " if last else "│   "))

    walk(root, "")
    return lines


def _all_files(node: Node):
    for k in node.children.values():
        if k.is_dir:
            yield from _all_files(k)
        else:
            yield k


def block(lines: List[str]) -> str:
    return f"{START}\n```\n" + "\n".join(lines) + f"\n```\n{END}"


def problems(files: List[str], notes: Dict[str, Tuple[str, bool]]) -> List[str]:
    issues: List[str] = []
    tracked_dirs, tracked_set = set(), set(files)
    for f in files:
        parts = f.split("/")
        for i in range(1, len(parts)):
            tracked_dirs.add("/".join(parts[:i]))
    for path, (_, untracked) in notes.items():
        base = path.rstrip("/")
        if not untracked and base not in tracked_dirs and base not in tracked_set:
            issues.append(f"docs/tree-notes.txt describes {path}, which is not in git")
    need = [d for d in tracked_dirs if d.count("/") <= 1 and not d.startswith(".opencode/skills/")
            and d not in SUMMARISE and not d.startswith("scripts/demo/fixtures") and d != ".opencode/skills"]
    need += [f for f in files if "/" not in f]
    need += [d for d in tracked_dirs if d in ("mcp-server/src", ".opencode/skills") or d.startswith("mcp-server/src/")]
    for p in sorted(set(need)):
        key_dir, key_file = p + "/", p
        if key_dir not in notes and key_file not in notes:
            issues.append(f"docs/tree-notes.txt has no line for {p}" + ("/" if p in tracked_dirs else ""))
    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files, notes = tracked(), load_notes()
    text = block(render(build(files, notes), notes))
    if not (args.write or args.check):
        print(text)
        return 0
    doc = DOC.read_text()
    if START not in doc or END not in doc:
        print(f"docs/architecture.md has no {START} ... {END} markers", file=sys.stderr)
        return 1
    current = doc[doc.index(START): doc.index(END) + len(END)]
    if args.write:
        DOC.write_text(doc.replace(current, text))
        print("docs/architecture.md tree refreshed")
        return 0
    issues = problems(files, notes)
    if current != text:
        issues.append("docs/architecture.md tree is stale: run python3 scripts/gen_tree.py --write")
    for i in issues:
        print(i)
    print("tree check:", "OK" if not issues else f"{len(issues)} problem(s)")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
