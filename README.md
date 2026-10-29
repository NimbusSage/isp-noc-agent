# NocAgent — an AI-powered NOC agent for ISPs

A skills system for ISP network operations: it reads and categorizes support tickets, analyzes slow towers hop by hop, and handles renames, field-tech dispatch, and site records, acting on its own only inside gates and leaving risky actions to people. It grew at a regional WISP and is built to be portable to any ISP — see [docs/portability-reinstall.md](docs/portability-reinstall.md).

**Two ways to run it.** The repo is an agent first; the automated ticket pipeline is an optional add-on.

| | What it is | Who drives it |
|---|---|---|
| **Mode A — the agent (primary)** | Open the agent in the repo directory and ask it to do NOC work. You approve anything risky. | A NOC tech, interactively |
| **Mode B — the ticket pipeline (optional switch)** | A background Node/Express webhook service (`server/`) that receives Sonar tickets from N8N and runs the full batch workflow autonomously under governance gates. | Production box, 24/7 |

## Mode A — the agent, right now

```bash
git clone <repo-url> /any/path && cd /any/path
onboard me            # guided setup skill (from a blank checkout to verified credentials)
```

Then just ask, in plain language:

- *"Analyze this slow tower" / "What's wrong along this path?"*
- *"Traceroute from [tower] and btest each hop to fiber"*
- *"Create a site pin for <new fiber cabinet>" / "Rename this tower-host"*
- *"Schedule a field tech from this ticket" / "File this as an NOC ticket"*
- *"Restore this factory-reset radio" / "Temp-connect this site until the fiber is repaired"*
- *"Onboard me" / *"Summarize this ticket's wireless health"*

Every credential is verified read-only by the doctor: `python3 scripts/doctor.py`.

## Mode B — the ticket pipeline (optional)

The server is a *switch*, not the identity of the repo: install it only to let tickets process themselves.

- How tickets flow end-to-end: [docs/ticket-pipeline.md](docs/ticket-pipeline.md) (workflow, batch processing, peak scheduler, governance gates)
- Webhook API + N8N wiring: [docs/webhook-api.md](docs/webhook-api.md)
- Turn it on/off: `scripts/install.sh` installs the units; `systemctl disable --now noc-agent` turns the switch off without touching Mode A. Full running details: [docs/operations.md](docs/operations.md).

## Skills — and building MORE agents on top

The ability above comes from `.opencode/skills/*` ([docs/skills.md](docs/skills.md), [docs/mcp-server.md](docs/mcp-server.md)). Each skill is one domain: ticket triage, live path analysis, site creation, device reprogramming, credential-safe Google access, and so on.

**The design scales: the pattern "skills + AGENTS.md workflow + shipped ops scripts" has already spawned this repo's second agent (the ticket pipeline). You can build a third, fourth, tenth** — a dispatch agent, a provisioning agent, a billing-button agent — by combining existing skills under a new focused workflow, the same way every skill here was proven on a real ticket first.

If you build something genuinely useful for NOC work with this: *the widely-useful version of that agent should be free for every NOC user — clone it, build the skill, field it, and send it back upstream rather than paying a vendor for it. The only thing that costs anything is the inference.* PRs welcome: add the skill, document one validated live run, gate the writes.

- Full skills table + how skills load: [docs/skills.md](docs/skills.md)
- Onboarding wizard internals: `.opencode/skills/onboarding-wizard/SKILL.md` (trigger: `onboard me`)

## Prerequisites

- **Node.js 20+** (agent runtime + optional server), **Python 3** (ops scripts, stdlib only)
- The default agent host is the OpenCode CLI (`OPENCODE_PATH` in `.env`) — but Mode B is harness-agnostic: set `AGENT_HARNESS=claude` (Claude Code) or `AGENT_HARNESS=command` with `AGENT_COMMAND_TEMPLATE='<cmd> {prompt}'` to drive any CLI agent harness (openclaw, hermes, …). See `docs/config.md`.
- **A Sonar instance + Sonar API token** (ticket/billing system); Google Chat webhook; systemd for Mode B
- Google user-OAuth only for the Google-linked skills (Sheets/Drive/Calendar) — the wizard mints it

## Documentation index

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System architecture, repository layout |
| [docs/config.md](docs/config.md) | Every env var, credential handling, MCP store setup |
| [docs/operations.md](docs/operations.md) | Running modes, logs, troubleshooting, development, file reference |
| [docs/ticket-pipeline.md](docs/ticket-pipeline.md) | Ticket workflow end-to-end |
| [docs/webhook-api.md](docs/webhook-api.md) | Webhook API reference + N8N setup |
| [docs/n8n-template.md](docs/n8n-template.md) | Step-by-step N8N workflow build + importable scrubbed JSON template |
| [docs/n8n-integration.md](docs/n8n-integration.md) | N8N workflow setup (HTTP node config, fallback logic, example JSON) |
| [docs/skills.md](docs/skills.md) | Skills system + extension guide |
| [docs/mcp-server.md](docs/mcp-server.md) | Network MCP server: devices, allowlists, credential store |
| [docs/harnesses.md](docs/harnesses.md) | Runs in OpenCode, Claude Code, Gemini CLI, Codex, Cursor, VS Code, or any harness with shell and file access. See [docs/harnesses.md](docs/harnesses.md) for setup. |
| [docs/quickstart.md](docs/quickstart.md) | Run the demo against fictional tickets with no credentials (`python3 scripts/demo.py`), then tell your agent `onboard me` to wire up real systems one tier at a time. |
| [docs/governance.md](docs/governance.md) | Decision log, circuit breaker, metrics + evals loop |
| [docs/gh-token-setup.md](docs/gh-token-setup.md) | Per-user GitHub token setup (no shared secret) |
| [docs/portability-reinstall.md](docs/portability-reinstall.md) | Moving/reinstalling, credential inventory, doctor, wizard |
| [docs/permissions-policy.md](docs/permissions-policy.md) | Autonomous / review / off-limits action tiers |
| [docs/team.md](docs/team.md) | Several people, one setup: roster and roles, a shared private pack, who-did-what logs |
| [docs/incident-runbook.md](docs/incident-runbook.md) | Operator procedures for incidents |

## Credentials & portability (essentials)

- Real secrets never enter git: `.env` (Sonar/webhook/monitor tokens), OAuth JSON outside the repo (enforced at runtime), device creds encrypted in `mcp-server/config/credentials.enc`, org identifiers in `site-pack/profile.json` (Sonar group/category IDs, org name, base URL — read by the gates).
- Moving/reinstalling anywhere: `git clone` + `sudo bash scripts/install.sh` regenerates systemd units at the actual path; `onboard me` re-aims credentials; `scripts/doctor.py` verifies. Details: [docs/portability-reinstall.md](docs/portability-reinstall.md).
- **Org identifiers live in the untracked site pack** — skills reference topology via `site-pack/topology/`, code reads Sonar/Oxidized/UISP bases from `site-pack/profile.json` + env, and the go-public gate (`python3 scripts/doctor.py --only portability_scan`) is at ZERO hits. Anything org-specific you add to tracked files re-fills the gate immediately.

## Testing

`node --test scripts/*.test.mjs` (offline helpers, node:test) · `npm test` in `mcp-server/` (vitest) · `evals/` golden-set regression gate ([docs/governance.md](docs/governance.md)) · `python3 scripts/docs_check.py` (docs links + index completeness) · `python3 scripts/backup_runtime_config.py` (secrets snapshot; see [docs/portability-reinstall.md](docs/portability-reinstall.md)). CI runs the offline test suites + compile checks on every push (`.github/workflows/ci.yml`), and a pre-push hook (`git config core.hooksPath .githooks`) runs the same offline gates locally before anything lands. The nightly secrets snapshot ships as `noc-backup.timer` (installed by `scripts/install.sh`); every runtime file that lives only on the box is archived — see the backup script header for the member list.

## License

GPL-3.0 — see [LICENSE](LICENSE). Free for any ISP or NOC tech to use, run, and build on; anything you distribute must stay open under the same terms. A commercially licensed, closed version is available by arrangement with the maintainers — open an issue or PR to start that conversation.
