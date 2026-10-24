#!/usr/bin/env python3
"""Harness portability setup for NocAgent (stdlib only, Python 3.9+).

Registers the NOC MCP server (and, where the harness has one, the shared
skills folder) with the coding-agent harness you use, with every path
resolved from the repo root at run time. Nothing here reads or writes
secrets: the generated entries pass environment variables by NAME only, and
the MCP server loads the repo .env itself (ensureRepoEnv).

    harness_setup.py --list [--json]
    harness_setup.py --check [--harness H] [--json]
    harness_setup.py --harness H --write [--force] [--dry-run] [--copy] [--skills]
    harness_setup.py --hooks

Universal fallback for any harness without MCP support:
    node mcp-server/dist/tool-cli.js <tool> '<json-args>'

Why a launcher: mcp-server/dist/index.js resolves config/server.json, the
credential store, the audit log and the repo .env from process.cwd(), so it
only works with cwd = mcp-server/. Harnesses that document a cwd field
(OpenCode, Codex, Gemini CLI, VS Code) get `node mcp-server/dist/index.js`
with that cwd. Claude Code and Cursor document none, so they get
`node scripts/mcp_launch.mjs`, which anchors the cwd itself.

VERIFICATION RECORD (2026-10-23). Status per harness:
  verified = format confirmed from official docs and/or the installed CLI
  shimmed  = believed right, NOT confirmed -> written as EXPERIMENTAL
  generic  = no per-harness support; use AGENTS.md + tool-cli
No harness is currently "shimmed" for the MCP file itself; unconfirmed
aspects are listed per harness in HARNESSES[...]["unverified"].

  claude   .mcp.json: mcpServers / command / args / env, ${VAR:-default},
             ${CLAUDE_PROJECT_DIR}; no cwd field documented
             https://code.claude.com/docs/en/mcp
           Live probe (claude 2.1.310, stub server, 2026-10-24): a stdio
             server from project .mcp.json is spawned with cwd = the project
             root and CLAUDE_PROJECT_DIR set; ${CLAUDE_PROJECT_DIR:-.}
             expands in args; ${VAR:-} is empty when VAR is unset and passes
             the parent value when set; ${VAR} without a default stays the
             literal text "${VAR}" (never used here, it would defeat the
             .env fallback).
           @AGENTS.md import (relative to the importing file), AGENTS.md
             read directly only when no CLAUDE.md exists
             https://code.claude.com/docs/en/memory
           project skills in .claude/skills/<name>/SKILL.md; the docs cover
             symlinked skill FOLDERS only. A symlinked skills DIRECTORY
             (.claude/skills -> ../.opencode/skills) was confirmed by
             running claude 2.1.310 in a scratch project (2026-10-24): the
             skill name and description were visible to the model.
             https://code.claude.com/docs/en/skills
           permissions.deny: Read/Edit gitignore-style paths, Read deny also
             blocks Write/Edit on the path, "!" carve-outs, Write(...) rules
             never match (use Edit), Bash(...) wildcards
             https://code.claude.com/docs/en/permissions
           PreToolUse hook: stdin JSON (tool_name, tool_input.file_path,
             Write.content, Edit.new_string), exit 2 blocks and stderr goes
             to Claude, $CLAUDE_PROJECT_DIR; MultiEdit is legacy
             https://code.claude.com/docs/en/hooks
             https://code.claude.com/docs/en/hooks-guide
  opencode opencode.json "mcp": type=local, command[], cwd (relative paths
             resolve from the workspace), environment, {env:VAR}. OpenCode 2
             moves servers to mcp.servers.<name> and uses "disabled"; the
             installed 2.1.3 still reads the 1.x layout (opencode debug
             config shows it normalised).
             https://opencode.ai/docs/mcp-servers/
             https://opencode.ai/v2/docs/mcp-servers
           permission schema differs between 1.x ("permission") and 2.x
             ("permissions" array) -> deliberately NOT added to opencode.json
             https://opencode.ai/docs/permissions/
             https://opencode.ai/v2/docs/permissions
           AGENTS.md read at project root; skills in .opencode/skills AND
             .claude/skills AND .agents/skills (so a .claude/skills link
             shows every skill twice; OPENCODE_DISABLE_CLAUDE_CODE_SKILLS
             turns the .claude scan off)
             https://opencode.ai/docs/rules/  https://opencode.ai/docs/skills/
  codex    ~/.codex/config.toml or trusted-project .codex/config.toml,
             [mcp_servers.<name>] command/args/env/env_vars/cwd; CLI 0.128.0
             `codex mcp add --help`
             https://learn.chatgpt.com/docs/extend/mcp?surface=cli
           AGENTS.md at repo root, combined size capped by
             project_doc_max_bytes (32 KiB default)
             https://learn.chatgpt.com/docs/agent-configuration/agents-md.md
           skills in .agents/skills (symlinked skill folders followed)
             https://learn.chatgpt.com/docs/build-skills.md
  gemini   .gemini/settings.json mcpServers: command/args/env/cwd/trust;
             $VAR / ${VAR} in env; format also confirmed by running
             `gemini mcp add` (0.60.0) in a scratch directory
             https://geminicli.com/docs/tools/mcp-server/
           context.fileName = ["AGENTS.md", ...]
             https://geminicli.com/docs/cli/gemini-md/
  cursor   .cursor/mcp.json mcpServers: type/command/args/env/envFile,
             ${workspaceFolder}, ${env:NAME}; no cwd field; AGENTS.md
             supported at project root
             https://cursor.com/docs/context/mcp
             https://cursor.com/docs/context/rules
  vscode   .vscode/mcp.json: servers / type=stdio / command / args / cwd /
             env, ${workspaceFolder}, ${env:NAME}
             https://code.visualstudio.com/docs/agents/reference/mcp-configuration
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVER_NAME = "network-troubleshooter"
PASS_THROUGH = ("NOC_MCP_PASSPHRASE", "LIBRENMS_TOKEN", "LIBRENMS_URL")
CODEX_DOC_LIMIT = 32768  # project_doc_max_bytes default (Codex docs)
TOOL_CLI = "node mcp-server/dist/tool-cli.js"
BUILD_HINT = "cd mcp-server && npm ci && npm run build"
SKILLS_SRC = ".opencode/skills"

# Harness registry. "status" is the overall verification state for the MCP
# registration; "unverified" lists aspects that are NOT confirmed.
HARNESSES: Dict[str, Dict[str, Any]] = {
    "opencode": {
        "title": "OpenCode",
        "status": "verified",
        "instructions": "AGENTS.md",
        "skills": ".opencode/skills (native)",
        "mcp_file": "opencode.json",
        "cwd": True,
        "unverified": [
            "relative command path resolution (cwd is documented, command is not)",
            "permission deny schema (1.x and 2.x differ)",
        ],
    },
    "claude": {
        "title": "Claude Code",
        "status": "verified",
        "instructions": "CLAUDE.md (@AGENTS.md import)",
        "skills": ".claude/skills -> .opencode/skills",
        "mcp_file": ".mcp.json",
        "cwd": False,
        "unverified": [
            "a symlinked .claude/skills directory is not in the docs (confirmed by a live probe on claude 2.1.310)",
            "cwd field in .mcp.json (undocumented, so the launcher is used)",
        ],
    },
    "codex": {
        "title": "OpenAI Codex CLI",
        "status": "verified",
        "instructions": "AGENTS.md (32 KiB default cap)",
        "skills": ".agents/skills (opt-in --skills)",
        "mcp_file": ".codex/config.toml",
        "cwd": True,
        "unverified": [
            "relative cwd resolution (absolute path written)",
            "project_doc_max_bytes honoured in project-level config",
        ],
    },
    "gemini": {
        "title": "Gemini CLI",
        "status": "verified",
        "instructions": "AGENTS.md via context.fileName",
        "skills": "none (read .opencode/skills/<name>/SKILL.md)",
        "mcp_file": ".gemini/settings.json",
        "cwd": True,
        "unverified": ["skill auto-discovery"],
    },
    "cursor": {
        "title": "Cursor",
        "status": "verified",
        "instructions": "AGENTS.md",
        "skills": "none (read .opencode/skills/<name>/SKILL.md)",
        "mcp_file": ".cursor/mcp.json",
        "cwd": False,
        "unverified": ["skill auto-discovery", "behaviour of ${env:NAME} when unset (env not passed)"],
    },
    "vscode": {
        "title": "VS Code (Copilot agent mode)",
        "status": "verified",
        "instructions": "unverified (read AGENTS.md manually)",
        "skills": "none (read .opencode/skills/<name>/SKILL.md)",
        "mcp_file": ".vscode/mcp.json",
        "cwd": True,
        "unverified": ["instruction file loading", "skill auto-discovery"],
    },
    "generic": {
        "title": "Any other harness",
        "status": "generic",
        "instructions": "AGENTS.md",
        "skills": "none (read .opencode/skills/<name>/SKILL.md)",
        "mcp_file": None,
        "cwd": True,
        "unverified": [],
    },
}


# --------------------------------------------------------------------------
# paths and small helpers
# --------------------------------------------------------------------------

def default_repo() -> Path:
    return Path(__file__).resolve().parent.parent


def server_paths(repo: Path) -> Dict[str, Path]:
    return {
        "mcp_root": repo / "mcp-server",
        "entry": repo / "mcp-server" / "dist" / "index.js",
        "launcher": repo / "scripts" / "mcp_launch.mjs",
        "tool_cli": repo / "mcp-server" / "dist" / "tool-cli.js",
    }


def path_exists(path: Path) -> bool:
    """Path.exists() that treats an unreadable location (another user's home, a missing mount) as absent."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def path_is_dir(path: Path) -> bool:
    """Path.is_dir() that treats an unreadable location as not a directory."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    text = read_text(path)
    if text is None:
        return None, "missing"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, "invalid JSON (%s)" % exc


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def expand_placeholders(value: str, repo: Path) -> str:
    """Resolve the harness variables this tool emits, for existence checks."""
    for token in ("${CLAUDE_PROJECT_DIR:-.}", "${CLAUDE_PROJECT_DIR}", "${workspaceFolder}"):
        value = value.replace(token, str(repo))
    return value


# --------------------------------------------------------------------------
# entry builders (one per harness; return the JSON/TOML payload)
# --------------------------------------------------------------------------

def _claude_entry(repo: Path) -> Dict[str, Any]:
    return {
        "type": "stdio",
        "command": "node",
        "args": ["${CLAUDE_PROJECT_DIR:-.}/scripts/mcp_launch.mjs"],
        "env": {name: "${%s:-}" % name for name in PASS_THROUGH},
    }


def _cursor_entry(repo: Path) -> Dict[str, Any]:
    # No env block: Cursor documents ${env:NAME} but not what an unset name
    # becomes, and a literal placeholder would defeat the repo .env fallback.
    return {
        "type": "stdio",
        "command": "node",
        "args": ["${workspaceFolder}/scripts/mcp_launch.mjs"],
    }


def _vscode_entry(repo: Path) -> Dict[str, Any]:
    return {
        "type": "stdio",
        "command": "node",
        "args": ["${workspaceFolder}/mcp-server/dist/index.js"],
        "cwd": "${workspaceFolder}/mcp-server",
    }


def _gemini_entry(repo: Path) -> Dict[str, Any]:
    paths = server_paths(repo)
    return {
        "command": "node",
        "args": [str(paths["entry"])],
        "cwd": str(paths["mcp_root"]),
        "env": {name: "$%s" % name for name in PASS_THROUGH},
    }


def _opencode_entry(repo: Path) -> Dict[str, Any]:
    paths = server_paths(repo)
    return {
        "type": "local",
        "command": ["node", str(paths["entry"])],
        "cwd": str(paths["mcp_root"]),
        "enabled": True,
        "environment": {name: "{env:%s}" % name for name in PASS_THROUGH},
    }


def _codex_block(repo: Path, doc_bytes: Optional[int]) -> str:
    paths = server_paths(repo)
    lines = [
        "[mcp_servers.%s]" % SERVER_NAME,
        'command = "node"',
        "args = [%s]" % json.dumps(str(paths["entry"])),
        "cwd = %s" % json.dumps(str(paths["mcp_root"])),
        "env_vars = [%s]" % ", ".join(json.dumps(n) for n in PASS_THROUGH),
        "startup_timeout_sec = 20",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# merging into existing files
# --------------------------------------------------------------------------

def merge_json(existing: Optional[str], keys: List[str], entry: Dict[str, Any],
               extra_top: Optional[Dict[str, Any]] = None) -> str:
    """Return JSON text with `entry` set at keys[0][...][name]; other content kept."""
    if existing is None or not existing.strip():
        data: Dict[str, Any] = {}
    else:
        data = json.loads(existing)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value is not an object")
    node = data
    for key in keys:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[SERVER_NAME] = entry
    for key, value in (extra_top or {}).items():
        data.setdefault(key, value)
    return dump_json(data)


def merge_opencode(existing: Optional[str], repo: Path) -> str:
    """Create the 1.x-layout entry, or repoint an existing entry in place."""
    entry = _opencode_entry(repo)
    if existing is None or not existing.strip():
        return dump_json({"$schema": "https://opencode.ai/config.json", "mcp": {SERVER_NAME: entry}})
    data = json.loads(existing)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    mcp = data.setdefault("mcp", {})
    servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else None
    holder = servers if servers is not None else mcp
    current = holder.get(SERVER_NAME)
    if not isinstance(current, dict):
        if servers is not None:  # 2.x layout uses "disabled", not "enabled"
            v2 = dict(entry)
            v2.pop("enabled", None)
            v2["disabled"] = False
            entry = v2
        holder[SERVER_NAME] = entry
        return dump_json(data)
    paths = server_paths(repo)
    command = current.get("command")
    if isinstance(command, list):
        current["command"] = [
            str(paths["entry"]) if isinstance(part, str) and part.endswith("dist/index.js") else part
            for part in command
        ]
    else:
        current["command"] = ["node", str(paths["entry"])]
    current["cwd"] = str(paths["mcp_root"])
    return dump_json(data)


def merge_gemini(existing: Optional[str], repo: Path) -> str:
    text = merge_json(existing, ["mcpServers"], _gemini_entry(repo))
    data = json.loads(text)
    context = data.get("context")
    if not isinstance(context, dict):
        context = {}
        data["context"] = context
    names = context.get("fileName")
    if names is None:
        context["fileName"] = ["AGENTS.md", "GEMINI.md"]
    elif isinstance(names, str):
        context["fileName"] = [names] if names == "AGENTS.md" else ["AGENTS.md", names]
    elif isinstance(names, list) and "AGENTS.md" not in names:
        context["fileName"] = ["AGENTS.md"] + names
    return dump_json(data)


_TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def merge_codex(existing: Optional[str], repo: Path) -> str:
    agents = repo / "AGENTS.md"
    size = agents.stat().st_size if agents.exists() else 0
    doc_bytes = None
    if size > CODEX_DOC_LIMIT:
        doc_bytes = max(65536, ((size + 16383) // 16384) * 16384)
    src = (existing or "").splitlines()
    kept: List[str] = []
    skipping = False
    target = "mcp_servers.%s" % SERVER_NAME
    for line in src:
        match = _TABLE_RE.match(line)
        if match:
            name = match.group(1).replace('"', "").strip()
            skipping = name == target or name.startswith(target + ".")
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    head: List[str] = []
    if not existing or not existing.strip():
        head.append("# Generated by scripts/harness_setup.py. Machine-local paths: do not commit.")
    has_limit = any(re.match(r"^\s*project_doc_max_bytes\s*=", l) for l in kept)
    if doc_bytes and not has_limit:
        head.append("# AGENTS.md exceeds the 32 KiB Codex default cap; raise it.")
        head.append("project_doc_max_bytes = %d" % doc_bytes)
    out = head + ([""] if head and kept else []) + kept
    if out:
        out.append("")
    out.append(_codex_block(repo, doc_bytes).rstrip("\n"))
    return "\n".join(out) + "\n"


def render(harness: str, repo: Path, existing: Optional[str]) -> str:
    if harness == "claude":
        return merge_json(existing, ["mcpServers"], _claude_entry(repo))
    if harness == "cursor":
        return merge_json(existing, ["mcpServers"], _cursor_entry(repo))
    if harness == "vscode":
        return merge_json(existing, ["servers"], _vscode_entry(repo))
    if harness == "gemini":
        return merge_gemini(existing, repo)
    if harness == "opencode":
        return merge_opencode(existing, repo)
    if harness == "codex":
        return merge_codex(existing, repo)
    raise ValueError("no MCP file for harness %r" % harness)


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------

def diff_summary(rel: str, old: Optional[str], new: str, limit: int = 60) -> List[str]:
    before = (old or "").splitlines()
    after = new.splitlines()
    lines = list(difflib.unified_diff(before, after, "a/" + rel, "b/" + rel, lineterm="", n=1))
    if len(lines) > limit:
        lines = lines[:limit] + ["... (%d more diff lines)" % (len(lines) - limit)]
    return lines


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def ensure_skills(repo: Path, target_rel: str, copy: bool, force: bool, dry_run: bool) -> Dict[str, Any]:
    src = repo / SKILLS_SRC
    target = repo / target_rel
    result: Dict[str, Any] = {"path": target_rel, "action": "none", "detail": ""}
    if not src.is_dir():
        result.update(action="error", detail="%s not found" % SKILLS_SRC)
        return result
    rel_link = os.path.relpath(str(src), str(target.parent))
    if target.is_symlink():
        if os.path.realpath(str(target)) == os.path.realpath(str(src)):
            result.update(action="unchanged", detail="symlink already points at %s" % SKILLS_SRC)
            return result
    elif target.is_dir() and not copy:
        result.update(action="skipped", detail="real directory exists (use --force --copy to refresh)")
        if not force:
            return result
    if target.exists() or target.is_symlink():
        if not force:
            result.update(action="skipped", detail="exists; use --force to replace")
            return result
        if not dry_run:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(str(target))
            else:
                target.unlink()
    if dry_run:
        result.update(action="would-copy" if copy else "would-link",
                      detail="%s -> %s" % (target_rel, rel_link if not copy else SKILLS_SRC))
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(str(src), str(target))
        result.update(action="copied", detail="copied %s (re-run after skills change)" % SKILLS_SRC)
        return result
    try:
        os.symlink(rel_link, str(target), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        result.update(action="error", detail="symlink failed (%s); retry with --copy" % exc)
        return result
    result.update(action="linked", detail="%s -> %s" % (target_rel, rel_link))
    return result


def do_write(harness: str, repo: Path, force: bool = False, dry_run: bool = False,
             copy: bool = False, skills: bool = False) -> Dict[str, Any]:
    spec = HARNESSES[harness]
    out: Dict[str, Any] = {"harness": harness, "files": [], "skills": None, "notes": [], "ok": True}
    rel = spec["mcp_file"]
    if rel is None:
        out["notes"].append("generic harness: nothing to write. Use stdio command "
                            "`node %s` (any cwd) or the fallback `%s`." % (
                                server_paths(repo)["launcher"], TOOL_CLI))
        return out
    path = repo / rel
    old = read_text(path) if path.exists() else None
    entry: Dict[str, Any] = {"path": rel, "action": "", "diff": []}
    try:
        new = render(harness, repo, old)
    except ValueError as exc:
        entry.update(action="error", detail="cannot merge into %s: %s. Edit it by hand." % (rel, exc))
        out["files"].append(entry)
        out["ok"] = False
        return out
    if old is not None and old == new:
        entry.update(action="unchanged")
    elif old is not None and not force:
        entry.update(action="refused", diff=diff_summary(rel, old, new),
                     detail="%s exists; re-run with --force to apply the change above" % rel)
        out["ok"] = False
    else:
        entry["diff"] = diff_summary(rel, old, new)
        if dry_run:
            entry["action"] = "would-write"
        else:
            if old is not None:
                shutil.copy2(str(path), str(path) + ".bak")
                entry["backup"] = rel + ".bak"
            atomic_write(path, new)
            entry["action"] = "wrote"
    out["files"].append(entry)

    if harness == "opencode" and old is not None and entry["action"] in ("wrote", "would-write"):
        out["notes"].append("opencode.json is tracked: this edit is machine-local. Keep it out of "
                            "commits (git update-index --skip-worktree opencode.json).")
    if harness in ("codex", "gemini", "opencode") and entry["action"] in ("wrote", "would-write", "unchanged"):
        out["notes"].append("%s contains absolute paths for this checkout: do not commit it." % rel)
    if harness in ("cursor", "vscode"):
        out["notes"].append("No env is passed to the server: it reads the repo .env itself.")

    skill_target = {"claude": ".claude/skills", "codex": ".agents/skills"}.get(harness)
    if skill_target and (harness == "claude" or skills):
        out["skills"] = ensure_skills(repo, skill_target, copy, force, dry_run)
        if out["skills"]["action"] == "error":
            out["ok"] = False
        if harness == "codex":
            out["notes"].append("OpenCode also scans .agents/skills, so skills load twice there "
                                "(set OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 only covers .claude).")
    elif skills and harness != "opencode":
        out["notes"].append("%s has no confirmed skills directory; read %s/<name>/SKILL.md." % (
            HARNESSES[harness]["title"], SKILLS_SRC))
    if not server_paths(repo)["entry"].exists():
        out["notes"].append("mcp-server/dist is not built: %s" % BUILD_HINT)
    return out


# --------------------------------------------------------------------------
# check (read-only)
# --------------------------------------------------------------------------

Check = Dict[str, str]


def _c(name: str, status: str, detail: str) -> Check:
    return {"name": name, "status": status, "detail": detail}


def count_skills(path: Path) -> int:
    try:
        return len([p for p in path.iterdir() if (p / "SKILL.md").is_file()])
    except OSError:
        return 0


def check_skills_dir(repo: Path, rel: str, label: str, required: bool) -> Check:
    path = repo / rel
    if path.is_symlink() or path.exists():
        n = count_skills(path)
        if n:
            kind = "symlink" if path.is_symlink() else "directory"
            return _c("skills", "ok", "%s %s with %d skills (%s)" % (rel, kind, n, label))
        if path.is_file():
            return _c("skills", "fail", "%s is a plain file (symlink flattened by checkout); "
                      "run harness_setup.py --harness claude --write --force --copy" % rel)
        return _c("skills", "fail", "%s exists but holds no SKILL.md folders" % rel)
    if required:
        return _c("skills", "fail", "%s missing; run harness_setup.py --harness %s --write" % (rel, label))
    return _c("skills", "info", "%s not present" % rel)


def _entry_target(entry: Dict[str, Any], repo: Path) -> Optional[Path]:
    """The script a registration launches, as an existing-or-not Path."""
    command = entry.get("command")
    args = entry.get("args")
    candidates: List[str] = []
    if isinstance(command, list):
        candidates.extend(str(p) for p in command)
    elif isinstance(command, str):
        candidates.append(command)
    if isinstance(args, list):
        candidates.extend(str(p) for p in args)
    for item in candidates:
        if item.endswith(("dist/index.js", "mcp_launch.mjs")):
            expanded = expand_placeholders(item, repo)
            p = Path(expanded)
            return p if p.is_absolute() else repo / p
    return None


def check_mcp_json(repo: Path, rel: str, keys: List[str], strict: bool, harness: str) -> List[Check]:
    path = repo / rel
    miss = "fail" if strict else "warn"
    data, err = load_json(path)
    if err == "missing":
        return [_c("mcp", miss, "%s missing; run harness_setup.py --harness %s --write" % (rel, harness))]
    if err:
        return [_c("mcp", "fail", "%s: %s" % (rel, err))]
    node: Any = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict) and harness == "opencode" and isinstance(data, dict):
        node = data.get("mcp") if isinstance(data.get("mcp"), dict) else None
    entry = node.get(SERVER_NAME) if isinstance(node, dict) else None
    if not isinstance(entry, dict):
        return [_c("mcp", miss, "%s has no %r entry" % (rel, SERVER_NAME))]
    out: List[Check] = []
    target = _entry_target(entry, repo)
    if target is None:
        out.append(_c("mcp", "fail", "%s entry does not launch dist/index.js or mcp_launch.mjs" % rel))
    elif not path_exists(target):
        hint = "points at %s which does not exist" % target
        if target.name == "index.js" and not str(target).startswith(str(repo)):
            hint += " (another checkout? run harness_setup.py --harness %s --write --force)" % harness
        out.append(_c("mcp", "fail", hint))
    elif not str(target).startswith(str(repo) + os.sep):
        out.append(_c("mcp", "warn", "%s launches %s, which is outside this checkout; "
                      "run harness_setup.py --harness %s --write --force to repoint it" % (rel, target, harness)))
    else:
        out.append(_c("mcp", "ok", "%s -> %s" % (rel, target.relative_to(repo))))
    cwd = entry.get("cwd")
    if isinstance(cwd, str):
        cwd_path = Path(expand_placeholders(cwd, repo))
        if cwd_path.is_absolute() and not path_is_dir(cwd_path):
            out.append(_c("mcp-cwd", "fail", "cwd %s does not exist" % cwd_path))
    return out


def check_codex_toml(repo: Path, strict: bool) -> List[Check]:
    rel = ".codex/config.toml"
    text = read_text(repo / rel)
    miss = "fail" if strict else "warn"
    if text is None:
        return [_c("mcp", miss, "%s missing (a user-level ~/.codex/config.toml is not inspected); "
                   "run harness_setup.py --harness codex --write" % rel)]
    header = "[mcp_servers.%s]" % SERVER_NAME
    if header not in text:
        return [_c("mcp", miss, "%s has no %s table" % (rel, header))]
    out: List[Check] = []
    block = text.split(header, 1)[1].split("\n[", 1)[0]
    match = re.search(r'^\s*args\s*=\s*\[\s*"([^"]+)"', block, re.M)
    if not match:
        out.append(_c("mcp", "fail", "%s: no args entry in the server table" % rel))
    elif not path_exists(Path(match.group(1))):
        out.append(_c("mcp", "fail", "args path %s does not exist (another checkout?)" % match.group(1)))
    else:
        out.append(_c("mcp", "ok", "%s -> %s" % (rel, match.group(1))))
    agents = repo / "AGENTS.md"
    if agents.exists() and agents.stat().st_size > CODEX_DOC_LIMIT:
        if re.search(r"^\s*project_doc_max_bytes\s*=", text, re.M):
            out.append(_c("instructions-size", "ok", "project_doc_max_bytes set in %s" % rel))
        else:
            out.append(_c("instructions-size", "warn", "AGENTS.md is %d bytes; Codex stops at %d "
                          "unless project_doc_max_bytes is raised" % (agents.stat().st_size, CODEX_DOC_LIMIT)))
    return out


def check_instructions(repo: Path, harness: str) -> List[Check]:
    agents = repo / "AGENTS.md"
    if not agents.is_file():
        return [_c("instructions", "fail", "AGENTS.md missing")]
    size = agents.stat().st_size
    if harness == "claude":
        claude = read_text(repo / "CLAUDE.md")
        if claude is None:
            return [_c("instructions", "warn", "CLAUDE.md missing: Claude would read AGENTS.md directly "
                       "only on v2.1.277+ with no CLAUDE.md anywhere above")]
        if re.search(r"^@AGENTS\.md\s*$", claude, re.M):
            return [_c("instructions", "ok", "CLAUDE.md imports @AGENTS.md (%d bytes)" % size)]
        return [_c("instructions", "fail", "CLAUDE.md does not import @AGENTS.md")]
    if harness == "gemini":
        data, _ = load_json(repo / ".gemini" / "settings.json")
        names = (data or {}).get("context", {}).get("fileName") if isinstance(data, dict) and isinstance(
            data.get("context"), dict) else None
        if (repo / "GEMINI.md").is_file():
            return [_c("instructions", "ok", "GEMINI.md present")]
        if isinstance(names, list) and "AGENTS.md" in names or names == "AGENTS.md":
            return [_c("instructions", "ok", "context.fileName includes AGENTS.md")]
        return [_c("instructions", "warn", "Gemini reads GEMINI.md; run harness_setup.py --harness gemini --write "
                   "to set context.fileName")]
    if harness == "codex":
        return [_c("instructions", "ok" if size <= CODEX_DOC_LIMIT else "info",
                   "AGENTS.md %d bytes (Codex default cap %d)" % (size, CODEX_DOC_LIMIT))]
    return [_c("instructions", "ok", "AGENTS.md (%d bytes)" % size)]


def git_hooks_path(repo: Path) -> Optional[str]:
    try:
        res = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None if res.returncode == 0 else None


def check_harness(repo: Path, harness: str, strict: bool) -> List[Check]:
    spec = HARNESSES[harness]
    paths = server_paths(repo)
    checks: List[Check] = []
    checks.extend(check_instructions(repo, harness))
    if harness == "claude":
        checks.append(check_skills_dir(repo, ".claude/skills", "claude", True))
        checks.extend(check_mcp_json(repo, ".mcp.json", ["mcpServers"], strict, harness))
        settings, err = load_json(repo / ".claude" / "settings.json")
        if err:
            checks.append(_c("guard", "warn", ".claude/settings.json: %s" % err))
        else:
            deny = (settings.get("permissions") or {}).get("deny") or []
            hooks = (settings.get("hooks") or {}).get("PreToolUse") or []
            guard = (repo / "scripts" / "hooks" / "claude_guard.py").is_file()
            if deny and hooks and guard:
                checks.append(_c("guard", "ok", "%d deny rules, PreToolUse hook, claude_guard.py present" % len(deny)))
            else:
                checks.append(_c("guard", "warn", "credential guard incomplete (deny=%d hooks=%d script=%s)" % (
                    len(deny), len(hooks), guard)))
    elif harness == "opencode":
        checks.append(check_skills_dir(repo, SKILLS_SRC, "opencode", True))
        checks.extend(check_mcp_json(repo, "opencode.json", ["mcp"], strict, harness))
        dup = [d for d in (".claude/skills", ".agents/skills") if (repo / d).exists() or (repo / d).is_symlink()]
        if dup:
            checks.append(_c("skills-duplicate", "warn", "OpenCode also scans %s, so each skill appears twice; "
                             "set OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 for .claude (no switch known for .agents)" %
                             ", ".join(dup)))
    elif harness == "codex":
        checks.append(check_skills_dir(repo, ".agents/skills", "codex", False))
        checks.extend(check_codex_toml(repo, strict))
    elif harness == "gemini":
        checks.append(_c("skills", "info", "no confirmed skills directory; read %s/<name>/SKILL.md" % SKILLS_SRC))
        checks.extend(check_mcp_json(repo, ".gemini/settings.json", ["mcpServers"], strict, harness))
    elif harness == "cursor":
        checks.append(_c("skills", "info", "no confirmed skills directory; read %s/<name>/SKILL.md" % SKILLS_SRC))
        checks.extend(check_mcp_json(repo, ".cursor/mcp.json", ["mcpServers"], strict, harness))
    elif harness == "vscode":
        checks.append(_c("skills", "info", "no confirmed skills directory; read %s/<name>/SKILL.md" % SKILLS_SRC))
        checks.extend(check_mcp_json(repo, ".vscode/mcp.json", ["servers"], strict, harness))
    else:
        checks.append(_c("skills", "info", "read %s/<name>/SKILL.md" % SKILLS_SRC))
        checks.append(_c("mcp", "info", "stdio command: node %s (any cwd)" % paths["launcher"]))
    if spec["mcp_file"] is not None or harness == "generic":
        if not paths["launcher"].is_file() and harness in ("claude", "cursor"):
            checks.append(_c("launcher", "fail", "scripts/mcp_launch.mjs missing"))
        if paths["entry"].is_file():
            checks.append(_c("build", "ok", "mcp-server/dist/index.js built"))
        else:
            checks.append(_c("build", "warn", "mcp-server/dist is not built: %s" % BUILD_HINT))
    cli = paths["tool_cli"]
    checks.append(_c("fallback", "ok" if cli.is_file() else "warn",
                     "%s --list  (works in any harness, same code path as MCP)" % TOOL_CLI))
    return checks


def do_check(repo: Path, harness: Optional[str]) -> Dict[str, Any]:
    names = [harness] if harness else [h for h in HARNESSES]
    report: Dict[str, Any] = {"repo": str(repo), "harnesses": {}, "ok": True}
    for name in names:
        checks = check_harness(repo, name, strict=harness is not None)
        report["harnesses"][name] = checks
        if any(c["status"] == "fail" for c in checks):
            report["ok"] = False
    hooks = git_hooks_path(repo)
    githooks = (repo / ".githooks").is_dir()
    if githooks and hooks != ".githooks":
        report["hooks"] = _c("git-hooks", "warn", "core.hooksPath is %r; run harness_setup.py --hooks" % hooks)
    elif githooks:
        report["hooks"] = _c("git-hooks", "ok", "core.hooksPath = .githooks")
    else:
        report["hooks"] = _c("git-hooks", "info", ".githooks not present")
    return report


# --------------------------------------------------------------------------
# hooks, list, CLI
# --------------------------------------------------------------------------

def do_hooks(repo: Path) -> Tuple[int, str]:
    if not (repo / ".git").exists():
        return 1, "not a git checkout: %s" % repo
    if not (repo / ".githooks").is_dir():
        return 1, ".githooks directory not found"
    res = subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".githooks"],
                         capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        return 1, "git config failed: %s" % res.stderr.strip()
    return 0, "core.hooksPath set to .githooks for %s" % repo


def list_harnesses() -> List[Dict[str, Any]]:
    rows = []
    for key, spec in HARNESSES.items():
        rows.append({"harness": key, "title": spec["title"], "status": spec["status"],
                     "mcp_file": spec["mcp_file"], "instructions": spec["instructions"],
                     "skills": spec["skills"], "cwd_field": spec["cwd"], "unverified": spec["unverified"]})
    return rows


def print_check(report: Dict[str, Any]) -> None:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}
    print("repo: %s" % report["repo"])
    for name, checks in report["harnesses"].items():
        print("\n[%s] %s (%s)" % (name, HARNESSES[name]["title"], HARNESSES[name]["status"]))
        for c in checks:
            print("  %s %-17s %s" % (mark[c["status"]], c["name"], c["detail"]))
    hooks = report["hooks"]
    print("\n  %s %-17s %s" % (mark[hooks["status"]], hooks["name"], hooks["detail"]))
    print("\nfallback for any harness: %s <tool> '<json>'   (--list shows the tools)" % TOOL_CLI)


def print_write(result: Dict[str, Any]) -> None:
    for f in result["files"]:
        print("%s: %s" % (f["path"], f["action"]))
        if f.get("detail"):
            print("  " + f["detail"])
        for line in f.get("diff", []):
            print("  " + line)
        if f.get("backup"):
            print("  previous version saved as %s" % f["backup"])
    if result["skills"]:
        s = result["skills"]
        print("%s: %s %s" % (s["path"], s["action"], s["detail"]))
    for note in result["notes"]:
        print("note: " + note)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Register NocAgent's MCP server and skills with a coding-agent harness.")
    p.add_argument("--list", action="store_true", help="list supported harnesses and their verification status")
    p.add_argument("--harness", choices=sorted(HARNESSES), help="target harness")
    p.add_argument("--check", action="store_true", help="read-only report (all harnesses unless --harness is given)")
    p.add_argument("--write", action="store_true", help="generate the MCP registration for --harness")
    p.add_argument("--force", action="store_true", help="allow changing a file that already exists (a .bak is kept)")
    p.add_argument("--dry-run", action="store_true", help="show what --write would change without writing")
    p.add_argument("--copy", action="store_true", help="copy the skills folder instead of symlinking it")
    p.add_argument("--skills", action="store_true", help="also create the harness skills folder (codex)")
    p.add_argument("--hooks", action="store_true", help="git config core.hooksPath .githooks")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--repo", default=None, help="repo root (default: parent of scripts/)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve() if args.repo else default_repo()
    if not (args.list or args.check or args.write or args.hooks):
        build_parser().print_usage(sys.stderr)
        print("choose one of --list, --check, --write, --hooks", file=sys.stderr)
        return 2
    if args.list:
        rows = list_harnesses()
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print("%-9s %-9s %-22s %s" % ("harness", "status", "mcp file", "instructions / skills"))
            for r in rows:
                print("%-9s %-9s %-22s %s | %s" % (r["harness"], r["status"], r["mcp_file"] or "-",
                                                    r["instructions"], r["skills"]))
                for item in r["unverified"]:
                    print("%-9s %-9s %-22s unverified: %s" % ("", "", "", item))
            print("\nfallback for any harness: %s <tool> '<json>'" % TOOL_CLI)
        return 0
    if args.hooks:
        code, msg = do_hooks(repo)
        print(json.dumps({"ok": code == 0, "detail": msg}) if args.json else msg)
        if code or not (args.check or args.write):
            return code
    if args.check:
        report = do_check(repo, args.harness)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_check(report)
        return 0 if report["ok"] else 1
    if args.write:
        if not args.harness:
            print("--write needs --harness", file=sys.stderr)
            return 2
        result = do_write(args.harness, repo, force=args.force, dry_run=args.dry_run,
                          copy=args.copy, skills=args.skills)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_write(result)
        return 0 if result["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
