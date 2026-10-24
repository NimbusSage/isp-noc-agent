# Which harnesses can run NocAgent

NocAgent was built in OpenCode. Any coding agent with shell access, file access, and the ability to read `AGENTS.md` can drive it. Skill discovery and MCP support are both optional, because each one has a fallback.

The table below covers OpenCode, Claude Code, Codex, Gemini CLI, Cursor, VS Code, and a generic "any other harness" row. If your tool is on that list, the setup script writes its config file for you. If it is not on the list, the fallbacks in the next section still apply.

## Minimum requirements and fallbacks

A harness needs three things: shell access, file access, and the ability to read `AGENTS.md`. That is the whole minimum. Skill discovery and MCP support improve the experience but neither is required.

**Without skill discovery:** when `AGENTS.md` says to load a skill, read `.opencode/skills/<name>/SKILL.md` directly. The full skill content is in that file.

**Without MCP:** run tools from the shell instead:

```
node mcp-server/dist/tool-cli.js <tool> '<json>'
```

The MCP server must be built before either path works. See Setup.

## Setup

Build the MCP server once. You only need to do this again if you update the server code:

```
cd mcp-server && npm ci && npm run build
```

Then use the setup script:

```
python3 scripts/harness_setup.py --list
```

Lists the harnesses the script knows about.

```
python3 scripts/harness_setup.py --check
```

Shows what is missing for your current environment.

```
python3 scripts/harness_setup.py --harness <name> --write
```

Writes the MCP config file for that harness. The script works out all paths from where the repo is. It will not overwrite an existing config file unless you add `--force`.

```
python3 scripts/harness_setup.py --hooks
```

Enables the repo's git hooks.

For Claude Code specifically, `--harness claude --write` also creates the `.claude/skills` symlink. See Notes.

## Harness table

| Harness | MCP config file | Instruction file | Skill discovery |
|---|---|---|---|
| OpenCode | `opencode.json` | `AGENTS.md` | `.opencode/skills`, native |
| Claude Code | `.mcp.json` | `CLAUDE.md` (imports `AGENTS.md`) | `.claude/skills` (symlink created by setup script) |
| Codex | `.codex/config.toml` | `AGENTS.md` | `.agents/skills`, only if you pass `--skills` |
| Gemini CLI | `.gemini/settings.json` | `AGENTS.md`, named in `context.fileName` | None |
| Cursor | `.cursor/mcp.json` | `AGENTS.md` | None |
| VS Code | `.vscode/mcp.json` | Instruction loading not confirmed; read `AGENTS.md` yourself | None |
| Any other | None | `AGENTS.md` | None |

**MCP launcher note.** Claude Code and Cursor start the MCP server through `node scripts/mcp_launch.mjs`. Those two tools do not document a setting for the working directory, so the launcher changes into `mcp-server/` before starting the server. OpenCode, Codex, Gemini CLI, and VS Code document a working-directory setting and run `node mcp-server/dist/index.js` directly.

## Notes

**What has been confirmed and what has not.** The OpenCode (2.1.3), Claude Code (2.1.310), and Gemini CLI (0.60.0) setups were confirmed by running those tools. The Codex, Cursor, and VS Code configurations follow each tool's documentation (for Codex, its `codex mcp add --help` output) and have not been run against the tool. Treat those three as a reasonable starting point rather than a confirmed setup.

**Why the `.claude/skills` symlink is not in git.** OpenCode also reads `.claude/skills`. If that symlink were committed, OpenCode would list every skill twice. Running `--harness claude --write` creates it on your machine, and `.gitignore` covers it. The symlink never reaches the repo.

**Codex and document size.** Codex reads at most 32 KiB of `AGENTS.md` by default. `AGENTS.md` in this repo is about 20 KB, so it fits within that limit.

**Claude Code credential handling.** The `.claude/settings.json` blocks Claude Code from reading `.env` and credential files, and a hook blocks edits that include secrets. Other harnesses receive the same rules as text inside `AGENTS.md`, which cannot enforce anything at the filesystem level.

**Ticket pipeline harness choices.** The optional background ticket pipeline can drive a harness on its own. Set the `AGENT_HARNESS` environment variable to `opencode`, `claude`, `codex`, `gemini`, or `command`. OpenCode and Claude Code are the two the project has used. The `codex` and `gemini` options are experimental: their command-line flags were verified against `--help` output only, not by running a full ticket batch. The `command` option runs a template you write, substituting `{prompt}` or `{prompt_file}`, without a shell.

**Adding a harness.** If your harness is not in the table, the "any other harness" row applies: give it shell and file access, point it at `AGENTS.md`, and use the shell fallback for tools. To add it as a named entry, the change is a new entry in `scripts/harness_setup.py`. Pull requests that include a verified setup are welcome.
