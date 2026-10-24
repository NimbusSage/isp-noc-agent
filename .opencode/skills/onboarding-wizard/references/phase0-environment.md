# Phase 0 reference - environment, harness, credential guard

## Prerequisites

| Tool | Minimum | Check | Notes |
|---|---|---|---|
| python3 | 3.9 | `python3 --version` | stdlib only, no pip installs |
| node | 20 | `node --version` | 18 still runs the demo but is end-of-life; use nvm or the nodejs.org installer |
| git | any recent | `git --version` | needed for the secret-scan hooks |
| sshpass | optional | `command -v sshpass` | only the reprogram and bring-up skills use it; install from your package manager if wanted |
| curl | any | `curl --version` | the demo uses it |

Windows: use WSL2 and run everything inside it. The repo can live in any folder;
nothing assumes a fixed install path (systemd units are rendered from templates).

## Harness table

`python3 scripts/harness_setup.py --list` prints the live table with verification
status. `--check` is read-only and reports `ok`, `WARN` or `FAIL` per item. Use it as
the proof, not this table.

| `--harness` | MCP registration file | Instructions | Skills |
|---|---|---|---|
| `opencode` | `opencode.json` | `AGENTS.md` | `.opencode/skills` (native) |
| `claude` | `.mcp.json` | `CLAUDE.md` importing `@AGENTS.md` | `.claude/skills` linked to `.opencode/skills` |
| `codex` | `.codex/config.toml` | `AGENTS.md` (32 KiB default cap) | `.agents/skills`, created with `--skills` |
| `gemini` | `.gemini/settings.json` | `AGENTS.md` via `context.fileName` | none: read `.opencode/skills/NAME/SKILL.md` directly |
| `cursor` | `.cursor/mcp.json` | `AGENTS.md` | none: read the SKILL.md directly |
| `vscode` | see `--list` | see `--list` | see `--list` |
| `generic` | prints what to configure by hand | `AGENTS.md` | read the SKILL.md directly |

Notes by harness:
- **opencode**: also scans `.claude/skills`, so skills can show up twice. `--check`
  prints the environment switch that stops it; apply what it says.
- **claude**: the first launch may ask you to approve the project MCP server; say yes.
  `.claude/settings.json` ships deny rules for reading and editing `.env`.
- **codex**: pass `--skills` to create the skills folder.
- **gemini and cursor**: no skill auto-discovery. Invoke the wizard by telling the
  agent to read `.opencode/skills/onboarding-wizard/SKILL.md` and follow it.
- **any harness without MCP support**: the same tools run from a shell. After T1,
  `cd mcp-server && node dist/tool-cli.js --list` lists them and
  `node dist/tool-cli.js TOOL 'JSON'` calls one through the identical code path
  (schemas, gates and audit behave the same as over MCP).
- If a registration file already exists, `--write` refuses; show the dry-run diff and
  only then use `--force` (a `.bak` is kept). `--copy` copies the skills folder instead
  of symlinking it (use on filesystems without symlinks).
- If `--check` warns that the MCP entry launches a path outside this checkout, the file
  was written for another clone: re-run `--write` (with `--force` after a dry-run).
- Registration changes are read at session start. After `--write`, the operator
  restarts the harness session and says `onboard me`; the state file resumes you.

## Running commands that need the operator's hands

Hidden prompts (secrets) and `sudo` need a real terminal and the operator's own
authority. Claude Code: the operator types the command with a leading `!` in the
session prompt. Everything else: a separate terminal opened in the repo root. After
the command, you verify with doctor; you never ask what was typed.

## Credential guard (what to tell the operator)

Secrets live only in `.env` (mode 0600, gitignored) and the AES-256-GCM encrypted
device store; the agent never asks for them, reads them or edits them. `env_set.py`
writes `.env` from a hidden prompt, `--fix-perms` repairs a loose mode, and doctor
reads `.env` in-process and prints key names and pass or fail, never values.

Claude Code additionally denies Read and Edit of `.env` and `.env.*` (except
`.env.example`) in `.claude/settings.json`. Other harnesses have no deny list in the
repo, so the rule is behavioral: do not read it. If the harness offers an ignore or
deny feature (for example an ignore file), suggest adding `.env` and `credentials*`.

Git hooks (`python3 scripts/harness_setup.py --hooks` sets `core.hooksPath` to
`.githooks`) run the secret scanner before commit and push, so a stray token fails the
commit. They change local git config only; never advise `--no-verify`.

## Phase 0 failures

| Symptom | Fix |
|---|---|
| `python3: command not found` or version below 3.9 | install python 3.9+; try `python3.11 scripts/...` if several are installed |
| node below 18 | install node 20+ (nvm: `nvm install 20`) |
| `harness_setup.py --check` shows `FAIL instructions` | wrong `--repo` or not in the repo root |
| skills not visible after restart | run `--check --harness NAME`; for gemini and cursor read the SKILL.md by path |
| `[FAIL] env_file  .env not found` | `cp -n .env.example .env && chmod 600 .env` (copying the example reveals nothing; the operator fills values later via `env_set.py`) |
| hooks refuse a commit | read the scanner message; remove the secret, never bypass |
