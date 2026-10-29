# NocAgent Architecture

> Operator manual index lives in the repo root [README](../README.md).

## Architecture

```
N8N Workflow
    |
    v
Webhook Server (Express, port 65333)        <-- server/index.js
    |  batches tickets for 60s (max MAX_BATCH_SIZE per chunk), enqueues one job per chunk, runs one at a time
    v
Configured agent harness (OpenCode, Claude Code, Codex, Gemini CLI, or custom)  <-- spawns per batch
    |  reads AGENTS.md, loads skills on demand
    v
+-------------------+   +---------------------+   +---------------------+
| Sonar API         |   | Path Analyzer       |   | Google Chat         |
| (GraphQL)         |   | (in-band, device    |   | (incoming webhook)  |
|                   |   |  MCP, live tests)   |   |                     |
+-------------------+   +---------------------+   +---------------------+
```

**Two core components:**

| Component | Location | Role |
|-----------|----------|------|
| Webhook server | `server/` | Node.js/Express. Receives tickets, batches them for 60 seconds, starts the configured agent harness per batch (OpenCode by default), and logs results. Runs as systemd service `noc-agent`. |
| Agent knowledge | `.opencode/skills/` | Markdown skill modules, loaded on demand by the harness or read directly from `SKILL.md`. No build step. |

**Optional component:**

| Component | Location | Role |
|-----------|----------|------|
| MCP server | `mcp-server/` | Network device diagnostics and bounded writes (MikroTik, UBNT). Read tools run only allowlisted commands against private and loopback addresses by default. Writes go only through typed, gated tools with explicit flags and exact-device bindings. Device credentials are kept encrypted and out of model context. See [mcp-server/README.md](../mcp-server/README.md). |

Temporary-site policy authorizes an explicit `Temp connect <site>` workflow to make narrowly scoped temporary infrastructure radio-role, addressing, and OSPF changes, including interruption on a selected donor AP. Customer CPE credentials are not needed for infrastructure-radio discovery. If MyMate's links/outages omit a named radio, continue with exact-IP inventory lookup (UISP where available; LibreNMS only as corroboration), rather than treating MyMate absence as device absence.

---

## Repository Layout

<!-- tree:start -->
```
NocAgent/
├── .claude/                                  # Claude Code rules denying credential reads and blocking secret edits
│   └── settings.json
├── .githooks/                                # Pre-commit secret scan and pre-push offline test hooks
│   ├── pre-commit
│   └── pre-push
├── .github/                                  # CI workflow, issue templates, pull request template and CODEOWNERS
│   ├── ISSUE_TEMPLATE/                       # Bug report and feature request issue templates
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   ├── workflows/                            # CI workflow definition
│   │   └── ci.yml
│   ├── CODEOWNERS
│   └── PULL_REQUEST_TEMPLATE.md
├── .opencode/                                # Parent directory for the skills folder
│   └── skills/                               # One folder per skill; agent loads each on demand from SKILL.md
│       ├── comment-formatting/               # SKILL.md
│       ├── correlated-auto-return/           # SKILL.md
│       ├── correlated-issues/                # SKILL.md
│       ├── create-site/                      # SKILL.md
│       ├── duplicate-detection/              # SKILL.md
│       ├── google-chat/                      # SKILL.md
│       ├── ihn-auto-return/                  # SKILL.md
│       ├── librenms/                         # SKILL.md
│       ├── mymate/                           # SKILL.md
│       ├── onboarding-wizard/                # SKILL.md, references/ (6 files)
│       ├── path-analyzer/                    # SKILL.md, references/ (4 files)
│       ├── peak-scheduler/                   # SKILL.md
│       ├── program-new-device/               # SKILL.md
│       ├── rename-site/                      # SKILL.md, references/ (4 files)
│       ├── reprogram-device/                 # SKILL.md, references/ (4 files)
│       ├── schedule-field-tech/              # SKILL.md
│       ├── skill-authoring/                  # SKILL.md
│       ├── sonar-api/                        # SKILL.md
│       ├── temp-bgp-peering/                 # SKILL.md, references/ (3 files)
│       ├── temp-site-connection/             # SKILL.md, references/ (4 files)
│       ├── temp-site-connection-undo/        # SKILL.md
│       ├── ticket-context/                   # SKILL.md
│       ├── tracker-sheet/                    # SKILL.md, references/ (3 files)
│       ├── wave-gui/                         # SKILL.md
│       └── wireless-health/                  # SKILL.md
├── deploy/                                   # Systemd templates for the webhook service and backup, evals and metrics timers
│   ├── noc-agent.service.template
│   ├── noc-backup.service.template
│   ├── noc-backup.timer.template
│   ├── noc-evals.service.template
│   ├── noc-evals.timer.template
│   ├── noc-metrics.service.template
│   └── noc-metrics.timer.template
├── docs/                                     # Operator manual pages
│   ├── n8n/                                  # Importable n8n workflow JSON
│   │   └── workflow-template.json
│   ├── setup/                                # NOC ticket tracker Google Sheet template
│   │   └── tracker-sheet-template.md         # NOC Ticket Tracker Sheet Template
│   ├── architecture.md                       # NocAgent Architecture
│   ├── config.md                             # Configuration Reference
│   ├── gh-token-setup.md                     # GitHub token setup (per user)
│   ├── governance.md                         # Governance, Metrics & Evals
│   ├── harnesses.md                          # Which harnesses can run NocAgent
│   ├── incident-runbook.md                   # NocAgent Incident Runbook
│   ├── mcp-server.md                         # MCP Server (Network Troubleshooter)
│   ├── n8n-integration.md                    # N8N Workflow Modification Guidelines
│   ├── n8n-template.md                       # N8N Ticket Workflow Template
│   ├── operations.md                         # Operations Manual
│   ├── permissions-policy.md                 # NocAgent Permissions Policy
│   ├── portability-reinstall.md              # Portability: Moving & Reinstalling
│   ├── quickstart.md                         # Try the demo before connecting anything real
│   ├── skills.md                             # Skills System
│   ├── team.md                               # Working as a team
│   ├── ticket-pipeline.md                    # Ticket Pipeline — How Tickets Flow Through the Agent
│   ├── tree-notes.txt
│   └── webhook-api.md                        # Webhook API & N8N Integration
├── evals/                                    # Regression evaluation of the ticket classifier against a golden set
│   ├── golden/                               # 2 files
│   ├── build_golden_set.py                   # Build the golden eval dataset from Sonar ticket history
│   ├── classifier.py                         # Deterministic baseline classifier for evals
│   └── eval_run.py                           # Run the golden eval set against the deterministic classifier (and, optionally, replay the real agent in…
├── logs/                                     # Runtime logs and state files; never committed
├── mcp-server/                               # TypeScript MCP server: allowlisted device diagnostics, gated write tools, encrypted credentials
│   ├── config/                               # Credential template and server settings file
│   │   ├── credentials.enc                   # Encrypted device credential store
│   │   ├── credentials.example.json
│   │   ├── credentials.json                  # Plaintext device credentials; delete after encrypting
│   │   └── server.json
│   ├── dist/                                 # MCP server build output from npm run build
│   ├── scripts/                              # Script that encrypts credentials.json into credentials.enc
│   │   └── encrypt-credentials.ts
│   ├── src/                                  # TypeScript source of the MCP server
│   │   ├── audit/                            # Audit log writer: one record per tool call, allowed or blocked
│   │   │   ├── logger.ts
│   │   │   └── operator.ts
│   │   ├── commands/                         # Per-device-type command allowlist, parameter checks and read-only validator
│   │   │   ├── allowlist.ts
│   │   │   ├── params.ts                     # Parameter validation and template rendering for read-only device commands
│   │   │   └── validator.ts
│   │   ├── config/                           # Server settings schema and repo-root .env fallback loader
│   │   │   ├── repo-env.ts                   # Repo-root .env fallback loader
│   │   │   ├── resources.ts                  # Where this organization's own services live (the TypeScript side of scripts/resources.py)
│   │   │   └── schema.ts
│   │   ├── credentials/                      # Credential store formats, encrypted store and exact-device resolution
│   │   │   ├── resolver.ts
│   │   │   ├── schema.ts
│   │   │   └── store.ts
│   │   ├── devices/                          # Device clients for MikroTik, UISP, airOS and Wave
│   │   │   ├── factory.ts
│   │   │   ├── mikrotik.ts
│   │   │   ├── types.ts
│   │   │   ├── ubnt-airos.ts
│   │   │   ├── ubnt-uisp.ts
│   │   │   └── ubnt-wave.ts
│   │   ├── inventory/                        # Exact-IP device lookup in LibreNMS inventory
│   │   │   └── librenms.ts
│   │   ├── redaction/                        # Redacts secrets from command output before it is returned
│   │   │   ├── engine.ts
│   │   │   └── patterns.ts
│   │   ├── security/                         # SSH host-key pinning, host rules and command-value allowlists
│   │   │   ├── hostkeys.ts                   # SSH host-key pinning (and TLS certificate observation) for device connections
│   │   │   ├── hosts.ts                      # Strict host/address handling shared by every tool that connects to a device
│   │   │   └── validators.ts                 # Positive allowlists for every value that ends up inside a device command
│   │   ├── temp-site/                        # Gated executors for temporary-site tools: airOS radio, RouterOS OSPF, snapshots
│   │   │   ├── airos-ssh.ts
│   │   │   ├── allowlist.ts                  # Host allowlist for write gates (TEMP_SITE_ALLOWED_HOSTS, NOC_WRITE_ALLOWED_HOSTS)
│   │   │   ├── mikrotik.ts
│   │   │   ├── ospf.ts
│   │   │   ├── radio.ts
│   │   │   └── snapshots.ts                  # Private storage for temp-site pre-apply snapshots (airOS system.cfg) and boot-id tripwire records
│   │   ├── writes/                           # Gated RouterOS write executor: PoE and interface operations
│   │   │   └── routeros-writes.ts
│   │   ├── index.ts
│   │   ├── register-tools.ts
│   │   ├── server.ts
│   │   ├── tool-cli.ts                       # Universal tool CLI — harness-independent access to the MCP executor
│   │   └── tool-schemas.ts                   # Single source of truth for every tool's INPUT schema
│   ├── state/                                # Rollback snapshots written by the temporary-site tools
│   ├── tests/                                # 31 files
│   ├── .gitignore
│   ├── package-lock.json
│   ├── package.json
│   ├── README.md
│   ├── tsconfig.json
│   └── vitest.config.ts
├── scripts/                                  # Deterministic ops scripts in Python, Node and shell, plus their tests
│   ├── demo/                                 # Fixtures and files for the zero-credential demo
│   │   └── fixtures/                         # 12 files
│   ├── hooks/                                # Claude Code hook that blocks edits writing secrets
│   │   └── claude_guard.py                   # Claude Code PreToolUse guard: block edits that would write secrets
│   ├── airos_rename.py                       # AirOS rename executor: anchored single-key SSID edits with snapshot/verify/rollback
│   ├── backup_runtime_config.py              # Archive NocAgent's runtime secrets and state OUTSIDE the repo
│   ├── build_denylist.py                     # Build scripts/leak_denylist.sha256 from private word lists
│   ├── chat_alert.py                         # Canonical Google Chat alert sender for NocAgent
│   ├── circuit_breaker.py                    # Deterministic circuit breaker for NOC auto-bounces
│   ├── cpe_binding_validate.mjs              # Validates a per-incident CPE binding file WITHOUT printing the password
│   ├── cred_store.mjs                        # The ONE place the ops scripts read the encrypted MCP credential store…
│   ├── credstore.py                          # Device credentials for the python/bash ops scripts, WITHOUT the plaintext…
│   ├── decision_log.py                       # Structured JSONL decision log for every agent decision
│   ├── demo.py                               # Run the NocAgent ticket pipeline end to end with zero real systems
│   ├── demo_agent_stub.py                    # Deterministic stand-in for the NOC agent, for demo mode
│   ├── docs_check.py                         # Verify the operator manual wiring
│   ├── docs_commands_check.py                # Check that what the docs and agent instructions tell people to run actually exists
│   ├── doctor.py                             # Verify a NocAgent installation
│   ├── env_ignore.txt
│   ├── env_load.mjs                          # Let a Node helper read ONLY the .env settings it needs, so nobody has to `source` the whole file
│   ├── env_set.py                            # Put a secret into .env without pasting it into a chat or onto a command line
│   ├── envfile.py                            # Tiny, side-effect-free `.env` reader shared by the ops scripts
│   ├── fleet_bind_sweep.mjs                  # One-time (or periodic) sweep: pin already-configured pool credentials to exact device ID+host bindings…
│   ├── gen_tree.py                           # Generate the repository layout shown in docs/architecture.md, so it can never go stale
│   ├── google_user_auth.mjs                  # Shared Google user-OAuth for NOC skills ("agent acts as the operator")
│   ├── gql_client.py                         # Minimal Sonar GraphQL client for the ops scripts
│   ├── harness_setup.py                      # Harness portability setup for NocAgent (stdlib only, Python 3.9+)
│   ├── hermetic_env.py                       # Import this from any test that runs git: it removes repository-local GIT_* variables
│   ├── install.sh                            # Deploy NocAgent at ANY path (no hardcoded install location)
│   ├── inventory_resolve.mjs                 # Resolve a radio/site name to device IDs + management IPs via a cached MyMate device index
│   ├── leak_denylist.sha256
│   ├── mcp_launch.mjs                        # Harness-neutral stdio launcher for the NOC MCP server
│   ├── metrics_rollup.py                     # Weekly NOC agent metrics from the decision log
│   ├── mk.sh                                 # Run a RouterOS command on a MikroTik using stored credentials (tried in order)
│   ├── mock_sonar.py                         # In-memory Sonar GraphQL mock for NocAgent demo mode
│   ├── mt_mactelnet.mjs                      # SSH -> near RouterOS -> /tool mac-telnet -> far RouterOS
│   ├── new_skill.py                          # Scaffold a new skill and register it in the two places that list skills (stdlib only)
│   ├── noc_config.py                         # Shared config resolution for the ops scripts
│   ├── notify.py                             # Provider-neutral notifications for NocAgent (stdlib only)
│   ├── oxidized_fetch.mjs                    # Deterministic Oxidized backup-config fetch for the agent
│   ├── pack.py                               # Share the organization's site pack between colleagues through a private git repository (stdlib + git)
│   ├── provision_exact_binding.mjs           # Provision (or remove) an exact-device credential binding in the encrypted MCP store by pinning an…
│   ├── rain_fade_model.py                    # Predict the rain rate that kills a fading-prone link
│   ├── rain_fade_readiness.py                # One-command rain-fade readiness for a Wave (WaveOS gen3) 60 GHz link
│   ├── resource_catalog.json
│   ├── resources.mjs                         # Node side of the resource registry (see scripts/resources.py for the full description)
│   ├── resources.py                          # Where this organization's own services live: one registry, one lookup rule (stdlib only)
│   ├── rssi_drop_graph.py                    # RSSI drop evidence chart for turned/misaligned wireless links (tracker-sheet SOP)
│   ├── schedule_dispatch.mjs                 # Field-tech dispatch scheduler — Sonar ticket ➜ Google Calendar appointment
│   ├── secret_scan.py                        # The one leak/secret scanner (stdlib only, no network)
│   ├── skills_lint.baseline.json
│   ├── skills_lint.py                        # Lint .opencode/skills/*/SKILL.md for portability and hygiene (stdlib only)
│   ├── sonar_bounce.py                       # Deterministic gate + hardened execution for auto-bounces (IHN / correlated)
│   ├── sonar_comment.py                      # Canonical helper to post HTML comments to Sonar tickets
│   ├── sonar_schema_check.py                 # Read-only check that the GraphQL names our scripts rely on exist on the configured Sonar
│   ├── ssh_common.py                         # Safe ssh/sshpass invocation shared by the device-touching scripts
│   ├── ssh_opts.mjs                          # Host-key policy shared by the node ops scripts (mirrors scripts/ssh_common.py)
│   ├── ssh_opts.sh                           # Sourced by the bash ops scripts (mk.sh, temp_bgp_rollback.sh)
│   ├── statefile.py                          # Crash-safe, multi-process-safe state file primitives
│   ├── team.py                               # Who is working, and what they may do: the team roster and the current operator (stdlib only)
│   ├── temp_bgp_peering.py                   # Temp BGP Peering — Emergency Fiber Failover Usage: python3 scripts/temp_bgp_peering.py --network…
│   ├── temp_bgp_rollback.sh                  # Temp BGP Rollback — disable only TEMP peers created during outage
│   ├── temp_ospf_sheet.mjs                   # Temp OSPF sheet: read-only inspector and gated row reservation for the temporary-site workflow
│   ├── tracker_sheet.mjs                     # Tracker Sheet helper — NOC Ticket Tracker 2.1 append-only writer
│   ├── wave_gui.mjs                          # Wave radio web UI driver (puppeteer-core + system Chrome)
│   ├── wave_rename.py                        # WaveOS rename executor: jq-filter SSID/hostname edits with snapshot/verify/rollback
│   └── (42 test files)                       # matching *.test.mjs, test_*.py
├── server/                                   # Node.js/Express webhook server and its batch prompt template
│   ├── batch_prompt_template.md
│   ├── index.js
│   ├── package-lock.json
│   ├── package.json
│   └── (1 test files)                        # matching *.test.mjs
├── site-pack/                                # Your organization's own data; only the README and examples are tracked
│   ├── example/                              # Fictional example data files to copy and fill in
│   │   ├── bgp-rr-map.example.json
│   │   ├── cpe-credentials.example.md
│   │   ├── devices.example.md
│   │   ├── fiber-map.example.json
│   │   ├── fiber-testing-ips.example.json
│   │   ├── fiber-topology.example.md
│   │   ├── naming-convention.example.md
│   │   ├── naming-conventions.example.md
│   │   ├── team.example.json
│   │   ├── tech_calendars.example.json
│   │   ├── temp-bgp-config.example.json
│   │   └── verified-probes.example.md
│   ├── profile.example.json
│   ├── profile.json                          # Your organization's profile: Sonar IDs, names and settings
│   └── README.md
├── .env                                      # Your settings and secrets; created from .env.example, never committed
├── .env.example                              # All settings the code reads, grouped by tier, with defaults
├── .gitignore                                # Excludes .env, logs, credentials and real site-pack data from git
├── .gitleaks.toml                            # Gitleaks CI scan config; test file fake credentials are allowed
├── .mcp.json                                 # Registers the MCP server for Claude Code
├── AGENTS.md                                 # Agent instructions: ticket workflow gates, thresholds, exit codes and credential rules
├── CLAUDE.md                                 # Imports AGENTS.md for Claude Code and states credential rules
├── CONTRIBUTING.md                           # Contribution guide: protected files, skill tiers, sign-off, hooks and secrets
├── LICENSE                                   # GPL-3.0
├── opencode.json                             # Registers the MCP server for OpenCode
├── package-lock.json                         # Lockfile for the root package.json
├── package.json                              # Root dependency for scripts/wave_gui.mjs (puppeteer-core)
├── README.md                                 # Entry point: two run modes, prerequisites and documentation index
└── SECURITY.md                               # How to report a vulnerability or a leaked secret
```
<!-- tree:end -->

---

