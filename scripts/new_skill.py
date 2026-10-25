#!/usr/bin/env python3
"""Scaffold a new skill and register it in the two places that list skills (stdlib only).

    python3 scripts/new_skill.py NAME --description "Use when ..." --summary "one line"
    python3 scripts/new_skill.py NAME --description "Use when ..." --summary "one line" --dry-run

Creates .opencode/skills/NAME/SKILL.md from a template, appends NAME to the "Skills index" list in
AGENTS.md and to the "Current skills" table in docs/skills.md, then prints the checks to run. It never
overwrites a skill that exists and writes nothing when any input is invalid.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME, MAX_DESCRIPTION, MAX_SUMMARY = 64, 1024, 200

TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

One or two sentences: what this skill lets the agent do, and what it never does.

## When to use

- The trigger, in the words an operator or ticket would use.

## Steps

1. The first action, with the exact command or tool call.

## Never

- Put a secret, a real hostname, address, person or ticket number in this file. Name a secret by its
  environment variable and use `<placeholder>` for site data (it belongs in `site-pack/`).
- Write a service address as a literal. Look it up with `python3 scripts/resources.py get <name>`
  (stop and ask the operator when it is not registered).
- Change a device or Sonar outside a gated tool or script.

## References

Put deep detail in `references/<topic>.md` and link it from here.
"""


def problems(name: str, description: str, summary: str, root: Path) -> List[str]:
    out: List[str] = []
    if not NAME_RE.match(name) or len(name) > MAX_NAME:
        out.append(f"name must be lowercase words joined by hyphens, at most {MAX_NAME} characters")
    if not description.startswith("Use when"):
        out.append('description must start with "Use when"')
    if len(description) > MAX_DESCRIPTION or "\n" in description:
        out.append(f"description must be one line of at most {MAX_DESCRIPTION} characters")
    if not summary or "\n" in summary or "|" in summary or len(summary) > MAX_SUMMARY:
        out.append(f"summary must be one line of at most {MAX_SUMMARY} characters without a | character")
    if (root / ".opencode" / "skills" / name).exists():
        out.append(f".opencode/skills/{name} already exists")
    return out


def frontmatter_value(text: str) -> str:
    """Plain when YAML accepts it as is, otherwise a double-quoted JSON string."""
    if ": " in text or " #" in text or text[:1] in "[{*&!%@`'\"|>":
        return json.dumps(text, ensure_ascii=False)
    return text


def add_to_index(agents: str, name: str, summary: str) -> Optional[str]:
    lines = agents.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "## Skills index")
    except StopIteration:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = max((i for i in range(start + 1, end) if lines[i].startswith("- `")), default=None)
    if last is None:
        return None
    lines.insert(last + 1, f"- `{name}` — {summary}")
    return "\n".join(lines)


def add_to_table(doc: str, name: str, summary: str) -> Optional[str]:
    lines = doc.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "### Current skills")
    except StopIteration:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    last = max((i for i in range(start + 1, end) if lines[i].startswith("| [`")), default=None)
    if last is None:
        return None
    lines.insert(last + 1, f"| [`{name}`](../.opencode/skills/{name}/SKILL.md) | {summary} |")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name")
    ap.add_argument("--description", required=True, help='frontmatter description; must start with "Use when"')
    ap.add_argument("--summary", required=True, help="one line for the AGENTS.md index and the docs/skills.md table")
    ap.add_argument("--dry-run", action="store_true", help="validate and show what would be written")
    args = ap.parse_args(argv)

    bad = problems(args.name, args.description, args.summary, root)
    agents_path, doc_path = root / "AGENTS.md", root / "docs" / "skills.md"
    new_agents = add_to_index(agents_path.read_text(), args.name, args.summary) if agents_path.exists() else None
    new_doc = add_to_table(doc_path.read_text(), args.name, args.summary) if doc_path.exists() else None
    if new_agents is None:
        bad.append('AGENTS.md has no "## Skills index" list to add to')
    if new_doc is None:
        bad.append('docs/skills.md has no "### Current skills" table to add to')
    if bad:
        for b in bad:
            print("error: " + b, file=sys.stderr)
        return 1

    skill = TEMPLATE.format(name=args.name, description=frontmatter_value(args.description),
                            title=args.name.replace("-", " ").title())
    if args.dry_run:
        print(f"would create .opencode/skills/{args.name}/SKILL.md and add {args.name} to AGENTS.md and docs/skills.md")
        return 0
    target = root / ".opencode" / "skills" / args.name / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(skill)
    agents_path.write_text(new_agents)
    doc_path.write_text(new_doc)
    print(f"created .opencode/skills/{args.name}/SKILL.md; added {args.name} to AGENTS.md and docs/skills.md")
    print("next: fill in SKILL.md, then run")
    print("  git add .opencode/skills AGENTS.md docs/skills.md")
    print("  python3 scripts/gen_tree.py --write")
    print("  python3 scripts/skills_lint.py --strict")
    print("  python3 scripts/docs_check.py && python3 scripts/docs_commands_check.py")
    print("  python3 scripts/secret_scan.py --tracked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
