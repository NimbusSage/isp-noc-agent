#!/usr/bin/env python3
"""Lint .opencode/skills/*/SKILL.md for portability and hygiene (stdlib only).

    skills_lint.py [--strict] [--json] [--repo DIR] [--baseline FILE]
    skills_lint.py --update-baseline

Rules (rule id: meaning)
  skill-missing        directory has no SKILL.md
  frontmatter          no frontmatter, unterminated, or an unparsable line
  frontmatter-yaml     unquoted value a YAML parser would reject or truncate
                       (": " inside, or " #" which starts a comment)
  name-mismatch        `name` differs from the directory name
  name-format          `name` is not lowercase-hyphen, or longer than 64
  description-missing  no `description`
  description-prefix   `description` does not start with "Use when"
  description-length   `description` longer than 1024 characters
  line-count           more than 500 lines (ratchet, counts per file)
  fence-unbalanced     a ``` or ~~~ fence is never closed
  dangling-link        markdown link or references/ scripts/ docs/ ... path
                       that does not exist (site-pack/ paths are optional
                       and untracked, so they are skipped)
  harness-wording      wording tied to one harness (OpenCode TUI, ask_user,
                       "the skill tool", "question tool")
  midword-placeholder  `<x>` glued to a word, such as tower<N>s
  secret-literal       literal secret in a shell block (via secret_scan)
  command-literal      a lookalike address (10.x, 172.16-31.x, *.example.com, *.invalid) used as the
                       target of a command: in a bash/sh fence, or in inline code that is a command or
                       URL. Use a <placeholder> or `python3 scripts/resources.py get <name>`; tag a fence
                       `bash example` to mark a deliberate fictional sample
  example-notice       a skill with 3 or more lookalike addresses lacks the standard notice that they
                       belong to a fictional network (see EXAMPLE_NOTICE)
  agents-size          AGENTS.md above 30 KB (Codex reads only 32 KiB by default)

Ratchet: scripts/skills_lint.baseline.json records today's counts. The default
run fails only when a (skill, rule) count grows, a new (skill, rule) pair
appears, a skill file grows past its recorded line count, or AGENTS.md grows.
--strict ignores the baseline. --update-baseline rewrites it from the current
tree (do that only after reviewing the diff). Exit: 0 pass, 1 violations,
2 usage error.

Neutral wording for harness-specific phrases: "load the skill via your
harness's skill loader, or read .opencode/skills/<name>/SKILL.md".
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

MAX_LINES = 500
MAX_DESCRIPTION = 1024
AGENTS_WARN_BYTES = 30 * 1024
SKILLS_REL = ".opencode/skills"
BASELINE_REL = "scripts/skills_lint.baseline.json"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SECRET_RULES = {
    "api-key", "aws-key", "bearer-token", "github-token", "google-api-key", "google-oauth",
    "literal-secret", "private-key", "slack-token", "sshpass-literal", "url-credentials",
    "url-secret-param",
}
SHELL_LANGS = {"", "sh", "bash", "shell", "console", "zsh", "terminal"}
HARNESS_PHRASES = [
    (re.compile(r"OpenCode TUI", re.I), "OpenCode TUI"),
    (re.compile(r"\bask_user\b"), "ask_user"),
    (re.compile(r"`skill`\s+tool|\bskill\s+tool\b", re.I), "the skill tool"),
    (re.compile(r"\bquestion tool\b", re.I), "question tool"),
]
HARNESS_SUGGESTION = ("load the skill via your harness's skill loader, "
                      "or read .opencode/skills/<name>/SKILL.md")
HTML_TAGS = {
    "a", "b", "i", "u", "p", "br", "hr", "em", "strong", "code", "pre", "span", "div", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "td", "th", "img", "h1", "h2", "h3", "h4", "h5", "h6", "small",
    "sup", "sub", "font", "center", "blockquote", "details", "summary", "button", "section",
}
MIDWORD_RE = re.compile(r"[A-Za-z0-9]<([A-Za-z][A-Za-z0-9_.-]*)>|<([A-Za-z][A-Za-z0-9_.-]*)>[A-Za-z0-9]")
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
PATH_RE = re.compile(
    r"(?<![\w/.\-<{$])((?:references|scripts|docs|evals|server|mcp-server)/[A-Za-z0-9_.\-/]+"
    r"\.(?:json|jsonl|md|py|mjs|js|ts|sh|yml|yaml|txt|toml)(?![A-Za-z0-9_]))"
)
# Addresses that look like a real resource. 192.168.x is left out on purpose: skills use it for device
# factory defaults and temporary procedure subnets, which are constants of the procedure.
LOOKALIKE_RE = re.compile(
    r"(?<![\w.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.]*\d)"
    r"|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:example\.(?:com|net|org)|invalid)\b")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
COMMAND_HINT_RE = re.compile(r"^(?:ssh|curl|ping|telnet|nc|wget|scp|sshpass|node|python3|GET|POST|PUT|PATCH|DELETE)\b|https?://|@\d")
COMMAND_FENCES = {"sh", "bash", "shell", "console", "zsh", "terminal"}
NOTICE_MIN_LITERALS = 3
EXAMPLE_NOTICE = ("> **Examples:** addresses and names in this skill belong to a fictional network. This organization's "
                  "real ones come from `python3 scripts/resources.py list` and `site-pack/`; never reuse an example.")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")

Finding = Dict[str, Any]


def _finding(skill: str, rule: str, line: int, message: str) -> Finding:
    return {"skill": skill, "rule": rule, "line": line, "message": message}


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------

def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, str]], List[Tuple[str, int, str]]]:
    """Tiny YAML-subset reader. Returns (fields or None, [(rule, line, msg)])."""
    problems: List[Tuple[str, int, str]] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, [("frontmatter", 1, "file must start with a --- frontmatter block")]
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        return None, [("frontmatter", 1, "frontmatter block is never closed with ---")]
    fields: Dict[str, str] = {}
    key: Optional[str] = None
    mode = ""  # "" plain, "block", "quoted"
    quote = ""
    for offset, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        if indented and key is not None:
            if mode == "quoted":
                fields[key] += " " + raw.strip()
                if raw.rstrip().endswith(quote):
                    fields[key] = fields[key].rstrip()[:-1]
                    mode = ""
            elif mode == "block":
                fields[key] += " " + raw.strip()
            else:
                fields[key] += " " + raw.strip()
                if ": " in raw.strip():
                    problems.append(("frontmatter-yaml", offset, "': ' inside an unquoted multi-line value"))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", raw)
        if not match:
            problems.append(("frontmatter", offset, "unparsable frontmatter line"))
            key = None
            continue
        key, value = match.group(1), match.group(2).rstrip()
        mode = ""
        if value[:1] in ("|", ">"):
            mode, fields[key] = "block", ""
        elif value[:1] in ("'", '"'):
            quote = value[0]
            if len(value) >= 2 and value.endswith(quote):
                fields[key] = value[1:-1]
            else:
                mode, fields[key] = "quoted", value[1:]
        else:
            if key in ("name", "description"):
                if ": " in value:
                    problems.append(("frontmatter-yaml", offset,
                                     "%s contains ': ' unquoted; a YAML parser will reject the file" % key))
                if " #" in value:
                    problems.append(("frontmatter-yaml", offset,
                                     "%s contains ' #' unquoted; a YAML parser drops the rest as a comment" % key))
                if value[:1] in ("[", "{", "*", "&", "!", "%", "@", "`"):
                    problems.append(("frontmatter-yaml", offset,
                                     "%s starts with %r, which YAML reserves; quote it" % (key, value[:1])))
            fields[key] = value
    return fields, problems


# --------------------------------------------------------------------------
# body scanning
# --------------------------------------------------------------------------

def scan_fences(lines: List[str]) -> Tuple[List[Tuple[int, int, str]], Optional[int]]:
    """Return ([(first_body_line, last_body_line, lang)], unterminated_open_line)."""
    blocks: List[Tuple[int, int, str]] = []
    open_char = ""
    open_len = 0
    open_at = 0
    lang = ""
    for idx, line in enumerate(lines, start=1):
        match = FENCE_RE.match(line)
        if not match:
            continue
        marker, info = match.group(1), match.group(2)
        if not open_char:
            open_char, open_len, open_at, lang = marker[0], len(marker), idx, info.lower()
        elif marker[0] == open_char and len(marker) >= open_len and not line.strip()[len(marker):].strip():
            blocks.append((open_at + 1, idx - 1, lang))
            open_char = ""
    return blocks, (open_at if open_char else None)


def in_block(blocks: List[Tuple[int, int, str]], line_no: int) -> bool:
    return any(start - 1 <= line_no <= end + 1 for start, end, _ in blocks)


_IGNORED_CACHE: Dict[Tuple[str, str], bool] = {}


def is_gitignored(repo: Path, rel: str) -> bool:
    """True when `rel` is gitignored: such paths are local-only and optional."""
    key = (str(repo), rel)
    if key not in _IGNORED_CACHE:
        try:
            res = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", rel],
                                 capture_output=True, timeout=5)
            _IGNORED_CACHE[key] = res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _IGNORED_CACHE[key] = False
    return _IGNORED_CACHE[key]


def load_secret_scan(repo: Path) -> Any:
    path = repo / "scripts" / "secret_scan.py"
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_skills_lint_secret_scan", str(path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module if callable(getattr(module, "scan_text", None)) else None
    except Exception:
        return None


def literal_findings(skill: str, label: str, lines: List[str], blocks: List[Tuple[int, int, str]]) -> List[Finding]:
    """command-literal: a lookalike address used as a command target (see the rule list above)."""
    out: List[Finding] = []
    spans = [(a, b, lang) for a, b, lang in blocks]
    for number, line in enumerate(lines, start=1):
        span = next(((a, lang) for a, b, lang in spans if a <= number <= b), None)
        if span is not None:
            first_body, lang = span
            tags = lines[first_body - 2].replace("`", " ").replace("~", " ").lower().split()[1:] if first_body >= 2 else []
            if lang in COMMAND_FENCES and "example" not in tags and not line.lstrip().startswith("#"):
                if LOOKALIKE_RE.search(line):
                    out.append(_finding(skill, "command-literal", number,
                                        "%s: a command uses an address that looks like a real resource; use a <placeholder> "
                                        "or `python3 scripts/resources.py get <name>`" % label))
            continue
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            continue
        for match in INLINE_CODE_RE.finditer(line):
            code = match.group(1)
            if LOOKALIKE_RE.search(code) and COMMAND_HINT_RE.search(code):
                out.append(_finding(skill, "command-literal", number,
                                    "%s: inline command or URL %r uses an address that looks like a real resource" % (label, code[:60])))
    return out


def count_lookalikes(lines: List[str]) -> int:
    return sum(len(LOOKALIKE_RE.findall(line)) for line in lines)


def lint_skill(repo: Path, skill_dir: Path, scanner: Any) -> Tuple[List[Finding], int]:
    skill = skill_dir.name
    out: List[Finding] = []
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        return [_finding(skill, "skill-missing", 0, "no SKILL.md in %s" % skill_dir.name)], 0
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    fields, problems = parse_frontmatter(text)
    for rule, line, msg in problems:
        out.append(_finding(skill, rule, line, msg))
    if fields is not None:
        name = fields.get("name", "").strip()
        desc = fields.get("description", "").strip()
        if name != skill:
            out.append(_finding(skill, "name-mismatch", 2, "name %r != directory %r" % (name, skill)))
        elif not NAME_RE.match(name) or len(name) > 64:
            out.append(_finding(skill, "name-format", 2, "name must be lowercase-hyphen, at most 64 chars"))
        if not desc:
            out.append(_finding(skill, "description-missing", 3, "no description"))
        else:
            if not desc.startswith("Use when"):
                out.append(_finding(skill, "description-prefix", 3, 'description must start with "Use when"'))
            if len(desc) > MAX_DESCRIPTION:
                out.append(_finding(skill, "description-length", 3,
                                    "description is %d chars (limit %d)" % (len(desc), MAX_DESCRIPTION)))

    if len(lines) > MAX_LINES:
        out.append(_finding(skill, "line-count", len(lines),
                            "%d lines (limit %d); move detail to references/" % (len(lines), MAX_LINES)))

    blocks, unterminated = scan_fences(lines)
    if unterminated:
        out.append(_finding(skill, "fence-unbalanced", unterminated, "code fence opened here is never closed"))

    for number, line in enumerate(lines, start=1):
        for regex, label in HARNESS_PHRASES:
            if regex.search(line):
                out.append(_finding(skill, "harness-wording", number,
                                    "%r is harness-specific; say: %s" % (label, HARNESS_SUGGESTION)))
        for match in MIDWORD_RE.finditer(line):
            tag = (match.group(1) or match.group(2) or "").lower()
            if tag not in HTML_TAGS:
                out.append(_finding(skill, "midword-placeholder", number,
                                    "placeholder %r glued to a word; separate it (e.g. <name>-x)" % match.group(0)))
        if not in_block(blocks, number):
            for link in LINK_RE.findall(line):
                target = link.split("#", 1)[0].split("?", 1)[0]
                if (not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I)
                        or any(c in target for c in "<{$*") or target.startswith("/")):
                    continue
                if (not (skill_dir / target).exists() and not (repo / target).exists()
                        and not is_gitignored(repo, target)):
                    out.append(_finding(skill, "dangling-link", number, "link target %s does not exist" % target))
        for ref in PATH_RE.findall(line):
            if ref.startswith("site-pack/") or "/logs/" in ref:
                continue
            if (not (skill_dir / ref).exists() and not (repo / ref).exists()
                    and not is_gitignored(repo, ref)):
                out.append(_finding(skill, "dangling-link", number, "referenced path %s does not exist" % ref))

    refs = sorted((skill_dir / "references").rglob("*.md")) if (skill_dir / "references").is_dir() else []
    literals = count_lookalikes(lines)
    out += literal_findings(skill, "SKILL.md", lines, blocks)
    for ref in refs:
        ref_lines = ref.read_text(encoding="utf-8", errors="replace").splitlines()
        ref_blocks, _ = scan_fences(ref_lines)
        literals += count_lookalikes(ref_lines)
        out += literal_findings(skill, ref.relative_to(skill_dir).as_posix(), ref_lines, ref_blocks)
    if literals >= NOTICE_MIN_LITERALS and not ("fictional network" in text and "resources.py" in text):
        out.append(_finding(skill, "example-notice", 1, "%d lookalike addresses but no notice that they are fictional; add: %s"
                            % (literals, EXAMPLE_NOTICE)))

    if scanner is not None:
        for start, end, lang in blocks:
            if lang not in SHELL_LANGS or end < start:
                continue
            body = "\n".join(lines[start - 1:end])
            try:
                hits = scanner.scan_text(body, "%s/%s/SKILL.md" % (SKILLS_REL, skill))
            except Exception:
                continue
            for hit in hits:
                rule = getattr(hit, "rule", "")
                if rule in SECRET_RULES:
                    out.append(_finding(skill, "secret-literal", start + int(getattr(hit, "line", 1)) - 1,
                                        "secret_scan rule %s in a shell block" % rule))
    return _dedupe(out), len(lines)


def _dedupe(items: List[Finding]) -> List[Finding]:
    seen = set()
    result = []
    for item in items:
        key = (item["skill"], item["rule"], item["line"], item["message"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# --------------------------------------------------------------------------
# whole-tree run, baseline ratchet
# --------------------------------------------------------------------------

def collect(repo: Path, skills_rel: str = SKILLS_REL) -> Dict[str, Any]:
    skills_dir = repo / skills_rel
    scanner = load_secret_scan(repo)
    findings: List[Finding] = []
    line_counts: Dict[str, int] = {}
    if skills_dir.is_dir():
        for entry in sorted(p for p in skills_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            found, count = lint_skill(repo, entry, scanner)
            findings.extend(found)
            line_counts[entry.name] = count
    agents = repo / "AGENTS.md"
    agents_bytes = agents.stat().st_size if agents.is_file() else 0
    if agents.is_file():
        # AGENTS.md is read by every harness, so harness-specific wording matters there too.
        for number, line in enumerate(agents.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            for regex, label in HARNESS_PHRASES:
                if regex.search(line):
                    findings.append(_finding("AGENTS.md", "harness-wording", number,
                                             "%r is harness-specific; say: %s" % (label, HARNESS_SUGGESTION)))
    if agents_bytes > AGENTS_WARN_BYTES:
        findings.append(_finding("AGENTS.md", "agents-size", 0,
                                 "%d bytes (warn above %d; Codex reads 32768 by default)" % (agents_bytes, AGENTS_WARN_BYTES)))
    return {"findings": findings, "line_counts": line_counts, "agents_bytes": agents_bytes,
            "skills": len(line_counts), "secret_scan": scanner is not None}


def counts_by_pair(findings: List[Finding]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in findings:
        if f["rule"] in ("line-count", "agents-size"):
            continue
        key = "%s:%s" % (f["skill"], f["rule"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def make_baseline(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": 1,
        "agents_md_bytes": result["agents_bytes"] if result["agents_bytes"] > AGENTS_WARN_BYTES else 0,
        "line_count": dict(sorted((s, n) for s, n in result["line_counts"].items() if n > MAX_LINES)),
        "findings": counts_by_pair(result["findings"]),
    }


def load_baseline(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "agents_md_bytes": 0, "line_count": {}, "findings": {}}
    return {"version": data.get("version", 1), "agents_md_bytes": int(data.get("agents_md_bytes", 0) or 0),
            "line_count": dict(data.get("line_count") or {}), "findings": dict(data.get("findings") or {})}


def evaluate(result: Dict[str, Any], baseline: Dict[str, Any], strict: bool) -> Dict[str, Any]:
    violations: List[Finding] = []
    grandfathered: Dict[str, int] = {}
    pair_counts = counts_by_pair(result["findings"])
    for f in result["findings"]:
        rule, skill = f["rule"], f["skill"]
        if rule == "line-count":
            allowed = baseline["line_count"].get(skill, MAX_LINES)
            if strict or f["line"] > allowed:
                msg = f["message"] + ("" if strict else " (baseline %d)" % allowed)
                violations.append(dict(f, message=msg))
            else:
                grandfathered["%s:line-count" % skill] = f["line"]
        elif rule == "agents-size":
            allowed = baseline["agents_md_bytes"]
            if strict or result["agents_bytes"] > allowed:
                violations.append(dict(f, message=f["message"] + ("" if strict else " (baseline %d)" % allowed)))
            else:
                grandfathered["AGENTS.md:agents-size"] = result["agents_bytes"]
        else:
            key = "%s:%s" % (skill, rule)
            allowed = 0 if strict else baseline["findings"].get(key, 0)
            if pair_counts[key] > allowed:
                note = "" if strict else " [%d now, baseline %d]" % (pair_counts[key], allowed)
                violations.append(dict(f, message=f["message"] + note))
            else:
                grandfathered[key] = pair_counts[key]
    tighter = [k for k, v in baseline["findings"].items() if pair_counts.get(k, 0) < v]
    return {"ok": not violations, "violations": violations, "grandfathered": dict(sorted(grandfathered.items())),
            "baseline_can_tighten": sorted(tighter)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Lint .opencode/skills for portability and hygiene.")
    p.add_argument("--repo", default=None, help="repo root (default: parent of scripts/)")
    p.add_argument("--baseline", default=None, help="baseline file (default: scripts/skills_lint.baseline.json)")
    p.add_argument("--strict", action="store_true", help="ignore the baseline; every finding fails")
    p.add_argument("--update-baseline", action="store_true", help="rewrite the baseline from the current tree")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    baseline_path = Path(args.baseline) if args.baseline else repo / BASELINE_REL
    result = collect(repo)
    if args.update_baseline:
        data = make_baseline(result)
        baseline_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote %s (%d tracked pairs, %d long files)" % (baseline_path, len(data["findings"]),
                                                                len(data["line_count"])))
        return 0
    verdict = evaluate(result, load_baseline(baseline_path), args.strict)
    if args.json:
        print(json.dumps({"ok": verdict["ok"], "skills": result["skills"], "strict": args.strict,
                          "secret_scan": result["secret_scan"], "violations": verdict["violations"],
                          "grandfathered": verdict["grandfathered"],
                          "baseline_can_tighten": verdict["baseline_can_tighten"]}, indent=2))
    else:
        for v in verdict["violations"]:
            where = v["skill"] if not v["line"] else "%s:%d" % (v["skill"], v["line"])
            print("%s: %s: %s" % (where, v["rule"], v["message"]))
        print("skills_lint: %d skills, %d violation(s), %d grandfathered by baseline%s" % (
            result["skills"], len(verdict["violations"]), len(verdict["grandfathered"]),
            "" if result["secret_scan"] else " (secret_scan not found: secret-literal check skipped)"))
        if verdict["baseline_can_tighten"]:
            print("note: counts fell below the baseline for %d pair(s); run --update-baseline to lock that in"
                  % len(verdict["baseline_can_tighten"]))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
