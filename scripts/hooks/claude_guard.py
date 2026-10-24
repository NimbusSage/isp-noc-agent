#!/usr/bin/env python3
"""Claude Code PreToolUse guard: block edits that would write secrets.

Wired in .claude/settings.json for the matcher Write|Edit|MultiEdit:

    python3 "$CLAUDE_PROJECT_DIR"/scripts/hooks/claude_guard.py

Protocol (https://code.claude.com/docs/en/hooks, checked 2026-10-23):
  * stdin is one JSON object: hook_event_name, tool_name, cwd and
    tool_input. tool_input.file_path is absolute. Write carries `content`;
    Edit carries `old_string` / `new_string`. MultiEdit is a legacy tool
    name; its `edits[].new_string` is read too, as is NotebookEdit's
    `new_source`, so the guard keeps working if either shape comes back.
  * Exit 2 blocks the tool call and Claude receives stderr as the reason.
    Any other exit lets the call through.

Behaviour
  * Only the text being WRITTEN is scanned (content / new_string), with
    scripts/secret_scan.py scan_text(text, path). The message names rules
    and line numbers only, never the matched text.
  * Skipped without scanning: paths outside the project, anything under
    .git/ node_modules/ __pycache__/, and gitignored files (real org data
    in site-pack/, logs/, .env). Those never reach the public repo; reads
    and edits of the secret stores are handled by permissions.deny.
  * Fails OPEN: unreadable input, a missing scanner, or any internal error
    exits 0 and says so on stderr, so a broken guard can never wedge the
    editor. The pre-commit hook and CI run the same scanner as a second
    line of defence.

Defense in depth, not a sandbox: the Bash deny patterns in settings.json
(cat .env, printenv, sshpass with a password flag, ...) are best-effort string matches and a
shell can always find another spelling. The hard stops are the Read/Edit
deny rules, this hook, the pre-commit scan and the CI scan.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.dont_write_bytecode = True

SKIP_PARTS = {".git", "node_modules", "__pycache__"}
Scanner = Callable[[str, str], Sequence[Any]]


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def target_path(tool_input: Dict[str, Any]) -> Optional[str]:
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def collect_texts(tool_input: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(label, text) for every piece of new text in this tool call."""
    pieces: List[Tuple[str, str]] = []
    for key in ("content", "new_string", "new_source"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            pieces.append((key, value))
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for index, edit in enumerate(edits, 1):
            value = edit.get("new_string") if isinstance(edit, dict) else None
            if isinstance(value, str) and value:
                pieces.append(("edit %d" % index, value))
    return pieces


def project_dir(payload: Dict[str, Any]) -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return Path(os.path.realpath(str(raw)))


def relative_to_project(path_str: str, project: Path) -> Optional[Path]:
    path = Path(path_str)
    if not path.is_absolute():
        path = project / path
    try:
        return Path(os.path.realpath(str(path))).relative_to(project)
    except ValueError:
        return None


def is_gitignored(rel: Path, project: Path) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "-q", "--", rel.as_posix()],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def skip_reason(path_str: str, project: Path) -> Optional[str]:
    rel = relative_to_project(path_str, project)
    if rel is None:
        return "outside the project"
    if SKIP_PARTS.intersection(rel.parts):
        return "generated or VCS path"
    if is_gitignored(rel, project):
        return "gitignored"
    return None


def load_scanner(project: Path) -> Optional[Scanner]:
    """Import scripts/secret_scan.py by file path; return its scan_text."""
    candidates = [Path(__file__).resolve().parent.parent / "secret_scan.py",
                  project / "scripts" / "secret_scan.py"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("_noc_secret_scan", str(candidate))
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses needs the module registered
        spec.loader.exec_module(module)
        scan = getattr(module, "scan_text", None)
        if callable(scan):
            return scan
    return None


def format_block(rel: Path, hits: List[Tuple[str, str, int]]) -> str:
    lines = ["BLOCKED by the NocAgent credential guard: this edit to %s contains a secret or "
             "real-organization identifier." % rel.as_posix()]
    seen = set()
    for label, rule, line in hits:
        key = (label, rule, line)
        if key in seen:
            continue
        seen.add(key)
        lines.append("  - rule %s, line %s of the new text (%s)" % (rule, line, label))
    lines.append("Do not write the value. Refer to it by environment-variable name or a <PLACEHOLDER>; "
                 "real values belong in the encrypted credential store, which agents never edit.")
    lines.append("If you think this is a false positive, stop and ask the owner. Do not reword the "
                 "value to get past the scan.")
    return "\n".join(lines) + "\n"


def evaluate(payload: Dict[str, Any], scanner: Optional[Scanner] = None) -> Tuple[int, str]:
    """Return (exit_code, stderr_message) for one PreToolUse payload."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0, ""
    path_str = target_path(tool_input)
    if path_str is None:
        return 0, ""
    pieces = collect_texts(tool_input)
    if not pieces:
        return 0, ""
    project = project_dir(payload)
    if skip_reason(path_str, project):
        return 0, ""
    rel = relative_to_project(path_str, project)
    if rel is None:
        return 0, ""
    scan = scanner or load_scanner(project)
    if scan is None:
        return 0, "claude_guard: scripts/secret_scan.py not found; edit to %s was not scanned.\n" % rel.as_posix()
    hits: List[Tuple[str, str, int]] = []
    for label, text in pieces:
        for finding in scan(text, rel.as_posix()):
            hits.append((label, str(_field(finding, "rule", "unknown")), int(_field(finding, "line", 0) or 0)))
    if not hits:
        return 0, ""
    return 2, format_block(rel, hits)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:  # malformed or empty input
        sys.stderr.write("claude_guard: ignoring unreadable hook input (%s)\n" % type(exc).__name__)
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        code, message = evaluate(payload)
    except Exception as exc:  # fail open, but say so
        sys.stderr.write("claude_guard: internal error (%s: %s); edit allowed\n" % (type(exc).__name__, exc))
        return 0
    if message:
        sys.stderr.write(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
