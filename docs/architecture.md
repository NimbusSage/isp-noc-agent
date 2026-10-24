# NocAgent Architecture

> Operator manual index lives in the repo root [README](../README.md).

## 2. Architecture

```
N8N Workflow
    |
    v
Webhook Server (Express, port 65333)        <-- server/index.js
    |  batches tickets for 60s (max MAX_BATCH_SIZE per chunk), enqueues one job per chunk, runs one at a time
    v
OpenCode CLI (non-interactive)              <-- spawns per batch
    |  loads AGENTS.md, invokes skills on demand
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
| Webhook server | `server/` | Node.js/Express. Receives tickets, batches them, spawns OpenCode CLI, logs results. Runs as systemd service `noc-agent`. |
| Agent knowledge | `.opencode/skills/` | Skill modules (Markdown), loaded on demand via the `skill` tool. No build step. |

**Optional component:**

| Component | Location | Role |
|-----------|----------|------|
| MCP server | `mcp-server/` | Network device diagnostics and bounded writes (MikroTik, UBNT). Read tools run only allowlisted commands against private and loopback addresses by default. Writes go only through typed, gated tools with explicit flags and exact-device bindings. Device credentials are kept encrypted and out of model context. See [mcp-server/README.md](../mcp-server/README.md). |

Temporary-site policy authorizes an explicit `Temp connect <site>` workflow to make narrowly scoped temporary infrastructure radio-role, addressing, and OSPF changes, including interruption on a selected donor AP. Customer CPE credentials are not needed for infrastructure-radio discovery. If MyMate's links/outages omit a named radio, continue with exact-IP inventory lookup (UISP where available; LibreNMS only as corroboration), rather than treating MyMate absence as device absence.

---

## 3. Repository Layout

```
NocAgent/
├── AGENTS.md                      # Agent instructions (the agent's "source of truth")
├── README.md                      # This file (human operator manual)
├── deploy/                        # systemd service/timer templates (rendered by scripts/install.sh)
├── scripts/                       # Deterministic ops scripts (Python 3, no deps)
│   ├── sonar_comment.py           #   canonical HTML comment poster (--verify)
│   ├── sonar_bounce.py            #   gated bounce executor (breaker + kill switch + anti-loop)
│   ├── circuit_breaker.py         #   rolling-window failure-rate breaker
│   ├── decision_log.py            #   JSONL decision log append/query
│   ├── metrics_rollup.py          #   weekly rates + revert check + Chat report
│   ├── temp_bgp_peering.py        #   emergency fiber failover
│   ├── temp_ospf_sheet.mjs        #   bounded Temp OSPF inspect + gated /30 reservation
│   └── temp_ospf_sheet.test.mjs   #   offline mock tests (node:test)
│   ├── airos_rename.py            #   airOS rename executor (snapshot/set/activate/verify/restore)
│   ├── wave_gui.mjs               #   Wave radio web GUI driver (puppeteer-core + Chrome): probe/enable-ssh, bearer-token capture for /api/v1.0

├── evals/                         # Eval harness
│   ├── build_golden_set.py        #   build golden set from Sonar history
│   ├── classifier.py              #   deterministic baseline classifier
│   ├── eval_run.py                #   eval runner + regression gate
│   └── golden/                    #   golden_set.json + incident stubs
├── .env                           # Secrets (never committed) -- see Configuration
├── .gitignore                     # Excludes .env, logs, node_modules, encrypted creds
├── server/                        # Webhook server (Node.js / Express)
│   ├── index.js                   # Server + batch logic + OpenCode spawner
│   └── package.json
├── .opencode/
│   ├── skills/                    # Agent knowledge modules (loaded on demand)
│   │   ├── path-analyzer/SKILL.md       # Live router path triage
│   │   │   └── references/              #   verified-probes, fiber/bgp maps, btest proc
│   │   ├── wireless-health/SKILL.md     # Wireless link investigation (helper)
│   │   ├── sonar-api/SKILL.md
│   │   ├── ticket-context/SKILL.md
│   │   ├── duplicate-detection/SKILL.md
│   │   ├── correlated-issues/SKILL.md
│   │   ├── correlated-auto-return/SKILL.md
│   │   ├── google-chat/SKILL.md
│   │   ├── mymate/SKILL.md
│   │   ├── temp-site-connection/SKILL.md # Authorized temporary recovery + guarded Sheet helper; live typed executor; OSPF turn-up rung
│   │   ├── temp-site-connection-undo/SKILL.md # Incident-scoped undo: primary-verify, tag rollback, radio restore, sheet release
│   │   ├── tracker-sheet/SKILL.md # NL ticket/alert-only filing — MyMate/LibreNMS verified, gated Tracker Sheet append (Create Ticket=TRUE creates the real ticket; FALSE is alert-only, no ticket; live duplicates → alert-only row carrying the existing ticket #) |
│   ├── ../scripts/temp_ospf_sheet.mjs # Temp OSPF Sheet inspector/reservation helper
│   └── ../scripts/temp_ospf_sheet.test.mjs # offline node:test suite
│   ├── ../scripts/tracker_sheet.mjs # Tracker Sheet inspect/plan/append/export-training helper (create_ticket flag → Create Ticket TRUE/FALSE; alert-only rows send via Send Count; duplicate gate refuses second tickets, allows alert-only dup rows referencing the existing Ticket #) |
│   ├── ../scripts/tracker_sheet.test.mjs # offline node:test suite
│   └── ../scripts/rssi_drop_graph.py # AP-side RSSI evidence chart + persistent ≥6 dB drop detection (UISP series → PNG; tracker-sheet §13; --selftest)
│   │   ├── ihn-auto-return/SKILL.md
│   │   ├── peak-scheduler/SKILL.md
│   │   ├── temp-bgp-peering/SKILL.md    # Emergency fiber failover — loop-aware TEMP BGP peering (Alpha/Bravo)
│   │   │   └── references/              #   loop-detection, fiber-classifier, peering-template
│   │   └── reprogram-device/
│   │       ├── SKILL.md
│   │       └── references/
│           ├── wave-ucfg.md            # Wave/MLO ucfg command reference
│           └── build_restore.py        # Builds a restorable ucfg tar (config.json + device board.json)
│   │   ├── program-new-device/SKILL.md # First-time bring-up of new devices (IP sheet allocation, router+radio programming, monitoring registration)
│   │   ├── create-site/SKILL.md       # Site-creation across Tower Directory/Sonar/UISP/MyMate
│   └── agents/                    # Custom agent definitions (future)
├── mcp-server/                    # Optional: encrypted-credential MCP server
│   ├── README.md                  # Full MCP documentation
│   ├── src/                       # TypeScript source
│   ├── config/
│   │   ├── credentials.example.json   # Template (tracked)
│   │   └── credentials.json           # Plaintext source (gitignored; encrypted store is credentials.enc)
│   └── package.json
├── docs/
│   ├── n8n-integration.md         # N8N workflow setup guide
│   └── permissions-policy.md      # Autonomous / review / off-limits tiers + kill switches
└── logs/
    └── webhook.log                # JSON-line runtime log
```

---

