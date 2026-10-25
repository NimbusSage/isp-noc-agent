@AGENTS.md

## Credential rules

- Never write secrets, passwords, tokens, API keys, SNMP community strings or real organization hostnames and addresses into any tracked file, skill, doc, test, commit message, log or chat. Refer to a secret by its environment-variable name or a `<PLACEHOLDER>`.
- Never read `.env`, `.env.*` (except `.env.example`), `.secrets/`, `mcp-server/config/credentials*` or `backups/` unless the owner explicitly asks. The scripts read `.env` themselves, so do not load it into your shell. If a tool needs one variable, load only that one, as AGENTS.md shows, and never print its value.
- Never put a secret on a command line. Use the environment or stdin.
- Never run `git commit --no-verify` or any other way around the hooks. Pre-commit runs `python3 scripts/secret_scan.py --staged`.
- Device and API credentials reach tools only through the encrypted store and the MCP tools (or `node mcp-server/dist/tool-cli.js`). Do not copy values out of the store.
- `.claude/settings.json` denies those reads, and the PreToolUse hook `scripts/hooks/claude_guard.py` blocks edits that contain secrets. If it blocks an edit, replace the literal. Do not work around it.

## Harness without skill discovery or MCP

- No skill discovery: when AGENTS.md says to load a skill, read `.opencode/skills/<name>/SKILL.md`.
- No MCP: call the same tools with `node mcp-server/dist/tool-cli.js <tool> '<json>'`.
- Setup and health check for any harness: `python3 scripts/harness_setup.py --check`.
