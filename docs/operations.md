# Operations Manual

> Operator manual index lives in the repo root [README](../README.md).

## Running the Agent

### Systemd (production)

```bash
# Install (renders deploy/*.template into /etc/systemd/system/)
sudo bash scripts/install.sh
# install.sh runs daemon-reload, enables noc-agent and the metrics/evals timers, and starts noc-agent when .env passes --check-config (pass --no-start to skip start; --enable-backup adds noc-backup.timer, daily 03:17)
sudo systemctl daemon-reload
sudo systemctl enable noc-agent
sudo systemctl start noc-agent

# Status
sudo systemctl status noc-agent

# Logs (live)
sudo journalctl -u noc-agent -f
```

The unit sets `Environment=XDG_RUNTIME_DIR=/run/user/0` so headless `opencode run` processes spawned by the webhook can reach the user's runtime socket directory. If it moves, update this variable to match.

### Manual (foreground)

```bash
cd server
node index.js
```

### Health Check

```bash
curl http://localhost:65333/health
# {"status":"ok","timestamp":"...","service":"noc-agent-webhook","version":"1.0.0"}
```

### Weekly Metrics & Eval Timers

```bash
# units/timers are installed by scripts/install.sh (deploy/*.template)
sudo systemctl daemon-reload
sudo systemctl enable --now noc-metrics.timer noc-evals.timer

# Run once manually
sudo systemctl start noc-metrics.service   # Chat report + logs/metrics/weekly_<date>.json
sudo systemctl start noc-evals.service     # deterministic golden-set eval
```

---

## Monitoring & Troubleshooting

### Logs

| Source | Command |
|--------|---------|
| Webhook server (file) | `tail -f logs/webhook.log` |
| Webhook server (journal) | `journalctl -u noc-agent -f` |
| Last 50 lines | `journalctl -u noc-agent -n 50` |

The webhook log is JSON-lines format — each entry has `timestamp`, `message`, and `data`.

### Common issues

**Agent not responding / tickets not processing:**
1. Check service: `sudo systemctl status noc-agent`
2. Check logs: `journalctl -u noc-agent -n 50`
3. Test health: `curl http://localhost:65333/health`
4. Run `python3 scripts/doctor.py --only harness` to verify the agent CLI is on PATH and sees the instructions, skills and MCP registration (prints PASS or FAIL)
5. Check the harness binary path in `.env`: `OPENCODE_PATH` for opencode, or `CLAUDE_PATH`, `CODEX_PATH`, `GEMINI_PATH` for the others; `AGENT_HARNESS` selects the active harness

**Authentication errors (401):**
1. Verify `TICKET_WEBHOOK_AUTH_TOKEN` in `.env` matches what N8N sends
2. Verify Base64 encoding: `echo -n "OCAGENT:<token>" | base64`
3. Test with curl directly

**Timeout / agent killed:**
1. Increase `BATCH_TIMEOUT_MS` in `.env` (default 2700000ms / 45 min)
2. Live path analysis (`path-analyzer`) runs traceroute + bandwidth tests in-band on the device path; serial hops on a long path routinely take ~9 min — the watchdog allows it
3. On timeout there is no external analyzer to poll: the fallback notification fires and the affected tickets queue for a human
4. Check `journalctl -u noc-agent` + `logs/webhook.log` for the failure point

**Context length / prompt too large (256k):**
- `AGENT_MODEL` → `OPENCODE_MODEL` → `opencode/big-pickle` model resolution; `opencode/big-pickle` has a context window of at most 250,000 tokens; the server rejects prompts estimated above 180,000 tokens (characters divided by 4); `MAX_BATCH_SIZE` defaults to 3 and larger bursts are split into serial jobs; `Spawning agent` logs `model` and `approx_tokens`
- Guard: `server/index.js` logs `Prompt token guard triggered` at >180k est.; check `logs/webhook.log` `approx_tokens`
- `server/index.js` now reads `logs/result_<jobId>.json` (file-authoritative) before stdout scan

**Parse fallback (silent loss) — now noisy:**
- `parseBatchResults` no longer returns silent `status:processed`; returns `needs_human` + `bottleneck_analysis:parse_failed` and `sendFallbackNotification` fires (`server/index.js`)
- Check `logs/webhook.log` `Failed to parse batch results JSON` + `Batch had parse failures`

### Governance incidents

For breaker trips, wrong bounces, stuck peak queues, corrupted governance files, and the
incident→testcase procedure, see the **[Incident Runbook](../docs/incident-runbook.md)**.
Quick checks:

```bash
python3 scripts/circuit_breaker.py status                 # window stats
python3 scripts/decision_log.py query --since <date>      # what the agent did
python3 scripts/metrics_rollup.py --no-chat               # rates + revert re-check
```

`/governance` and `POST /governance/breaker` authenticate via `GOVERNANCE_AUTH_TOKEN` if set, otherwise fall back to `TICKET_WEBHOOK_AUTH_TOKEN`; `GOVERNANCE_AUTH_TOKEN` requires the same 24-character minimum. The command below reads the token from the environment so it does not appear on the command line:

```bash
printf 'header = "Authorization: Bearer %s"\n' "$TICKET_WEBHOOK_AUTH_TOKEN" | curl -sS -K /dev/stdin http://127.0.0.1:65333/governance
```

**Agent process fails:**
- The webhook server sends a **fallback notification** to Google Chat listing all affected tickets so they're not silently dropped
- Check `webhook.log` for the error details (`model` + `approx_tokens` logged at `Spawning agent` `server/index.js`)

**Graceful shutdown:**
- On SIGTERM/SIGINT, the server clears the batch timer and logs any dropped queued tickets. If tickets are still queued/in-flight it fires a Google Chat alert and clears `logs/pending_tickets.json`. In-flight OpenCode processes are terminated.

---

## Development

### Where things live

| What | Where |
|------|-------|
| Agent workflow logic | [AGENTS.md](../AGENTS.md) |
| Skill knowledge | `.opencode/skills/<name>/SKILL.md` |
| Webhook server code | `server/index.js` |
| MCP server code | `mcp-server/src/` |
| N8N setup guide | [docs/n8n-integration.md](../docs/n8n-integration.md) |

### Testing

- Temp OSPF Sheet helper (offline mock tests): `node --test scripts/temp_ospf_sheet.test.mjs`
- airOS rename executor (offline selftest): `python3 scripts/airos_rename.py selftest`
- Syntax check: `node --check scripts/temp_ospf_sheet.mjs`
- `mcp-server`: `cd mcp-server && npm test` (vitest)
- `server/`: `node --check server/index.js` + manual `curl` batch test; `MAX_BATCH_SIZE=3` keeps prompts small, check `logs/webhook.log` for `model` pin
- Governance scripts: `python3 scripts/circuit_breaker.py status` · `python3 scripts/sonar_bounce.py check <id>` (gates only, no write)
- Evals: `python3 evals/eval_run.py` (deterministic, always safe) · `DRY_RUN=1 python3 evals/eval_run.py --replay` (agent replay, no writes)
- Skills: load by name in your agent harness to test, or send a test ticket to the webhook

### Modifying the batch window

Set `WINDOW_DURATION_MS` in `.env` (default 60000ms) or edit `server/index.js`. Restart the service.

---

## File Quick Reference

| Task | Edit this file |
|------|----------------|
| Change the ticket workflow | `AGENTS.md` |
| Add a new skill | `python3 scripts/new_skill.py` — creates `.opencode/skills/<name>/SKILL.md` and adds it to the Skills index in `AGENTS.md` |
| Update an existing skill | `.opencode/skills/<name>/SKILL.md` |
| Change webhook port | `.env` -> `WEBHOOK_PORT` |
| Change webhook auth token | `.env` -> `TICKET_WEBHOOK_AUTH_TOKEN` |
| Change batch collection window | `.env` -> `WINDOW_DURATION_MS` |
| Suspend/resume auto-bounces | `python3 scripts/circuit_breaker.py open|clear` or `POST /governance/breaker`; hard kill switch `.env` -> `BOUNCE_AUTONOMOUS_ENABLED=false` |
| Change breaker sensitivity | `.env` -> `BREAKER_MAX_ERROR_RATE` / `BREAKER_MIN_RUNS` / `BREAKER_WINDOW_SEC` |
| Change what the agent may do autonomously | [docs/permissions-policy.md](../docs/permissions-policy.md) + this table |
| Change agent process timeout | `.env` -> `BATCH_TIMEOUT_MS` |
| Change OpenCode binary path | `.env`: `OPENCODE_PATH` (opencode), `CLAUDE_PATH`, `CODEX_PATH` or `GEMINI_PATH` |
| Change pinned model | `.env` → `AGENT_MODEL` (or legacy `OPENCODE_MODEL`); `opencode.json` `model` entry also applies |
| Change max batch size | `.env` -> `MAX_BATCH_SIZE` (`server/index.js`) |
| Modify systemd service | `deploy/noc-agent.service.template` |
| Add device credentials (MCP) | `mcp-server/config/credentials.json` (then re-encrypt) |
| Modify N8N workflow | `docs/n8n-integration.md` + N8N UI |
| Add/review alert-only tracker rows (no ticket) | `.opencode/skills/tracker-sheet/SKILL.md` + `scripts/tracker_sheet.mjs` — intent is `create an alert`, helper writes `Create Ticket=FALSE` + `Send Count=1`; sheet automation must NOT create a ticket from FALSE rows; a live exact duplicate also files an alert-only row, but with the EXISTING ticket # in the Ticket # column — never a second ticket |

---

