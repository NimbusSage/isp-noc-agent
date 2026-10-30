# NocAgent Permissions Policy

Formal permissions model per the production-ticket-agent criteria: what the agent does
**without approval**, what **requires human review**, and what is **completely off-limits**.
Enforced where possible by deterministic code (scripts + server), not prompt discipline.

Status: **Phase 2-3 hybrid** (triage + narrow gated autonomy). This document is the contract;
AGENTS.md workflow steps reference it. Changes to this file should be reflected in
[governance.md](governance.md) and reviewed weekly with metrics.

## Tier 1 — Autonomous (no approval)

Deterministic guardrails still apply (thresholds, anti-loop, breaker).

| Action | Guardrail | Enforcement |
|---|---|---|
| Read tickets/comments from Sonar | read-only queries | — |
| Categorize tickets (add categories, preserve existing) | must fetch-then-merge ids | prompt + review |
| Duplicate detection + informational comments | confidence thresholds env (`DUPLICATE_*`) | prompt |
| Auto-close passthroughs (OOO, PBS success, Maintenance) | exact subject/body matchers in AGENTS.md Step 0 | prompt |
| Post analysis comments | `scripts/sonar_comment.py --verify` (escaping + verify guards) | code |
| IHN / correlated auto-bounce to CS L2 | `scripts/sonar_bounce.py`: breaker + kill switch + anti-loop | code |
| Queue night/peak tickets for peak window | anti-loop markers, single retest | prompt + markers |
| Send Google Chat notifications | notification rules AGENTS.md Step 4 | prompt |
| Temporary-site recovery execution | An explicit `Temp connect <site>` request authorizes only the temporary infrastructure-radio role/config changes, temporary addressing, and OSPF changes needed to restore service; standing authorization includes a temporary customer interruption on the selected donor infrastructure AP. Exact targets, before-state, address conflicts, bounded sequence, verification, rollback, and audit are mandatory. Sheet/IPAM may be skipped for operator-curated addresses, but must not be represented as checked/reserved | Executor implemented and gated: `temp-radio-apply`/`rollback`, `temp-ospf-apply`/`rollback`. All writes require execute flag + `TEMP_SITE_WRITE_ENABLED` + transport-verified flag + host allowlist + exact-device binding. No raw SSH/config writes |
| Inspect Temp OSPF sheet schema | `scripts/temp_ospf_sheet.mjs inspect`, bounded ranges, read-only OAuth scope | deterministic helper; requires service-account config |
| Site rename planning + operator-led execution (`rename-site`) | An explicit `Rename site <OLD> to <NEW>` (or SSID-only) request covers only the confirmed rename table; backhaul /29-gateway move verified before any SSID drop; CPEs-before-AP order; snapshots + readback after each step; CPE logins via `customer-airos`/`customer-mt` pools only | prompt + markers; no typed rename executor — operator applies, agent verifies |
| Reserve one selected Temp OSPF `/30` pair | header-signature check, unique consecutive free host rows, explicit approval + write setting + kill switch, atomic 6-cell batch, both-row readback, local audit | deterministic helper; only after schema and Editor permission review; no device write follows automatically |
| Exact-device CPE diagnostics via `run-exact-device-diagnostics` | Existing allowlisted UBNT airOS reads only; exact device ID + host + exactly one encrypted credential binding; one auth attempt; no fallback; trusted caller-side inventory validation required | MCP code + tests; not provisioned for production CPE workflow |

## Tier 2 — Requires Human Review

Agent acts, but a human must confirm outcome; these always notify.

| Action | Current handling |
|---|---|
| Any bounce when breaker is open or kill switch off | exit 3/4 → `needs_human`, escalation_reason set |
| Bounce blocked by anti-loop marker | exit 5 → re-evaluation comment + `needs_human` |
| Low-confidence tower resolution (< `PATH_TOWER_THRESHOLD` 75) | low-confidence note + `needs_human` |
| Healthy but tower confidence < `IHN_TOWER_THRESHOLD` 80 | healthy comment, no bounce, notify |
| Weather-excluded bounce candidates | analysis posted, bounce blocked, notify |
| DEGRADED analyses without exact-IP correlated match | correlation comment + `needs_human` |
| Analysis failures (unreachable tower, btest auth fail) | no comment posted, `needs_human` |
| Medium-confidence duplicates (50-84%) | informational comment + notify |
| Peak-queued tickets | silent until 20:05 CT drain re-decides |
| Temporary-site recovery cannot pass an identity, compatibility, addressing, state-snapshot, verification, or rollback gate | Do not attempt the affected write; report the precise blocker and preserve the current state. A donor AP client count/customer-account mapping or customer CPE credential is not required merely to exercise the standing authorization for an infrastructure AP role change. |

## Tier 3 — Off-Limits (agent must never do)

| Action | Why |
|---|---|
| Change customer records, status of CS-owned tickets, assignees | customer-impacting writes |
| Post customer-facing replies | comms stay human-owned |
| Delete tickets or comments | irreversible |
| Modify Sonar categories on CS L2 tickets | outside NOC scope |
| Arbitrary device writes, unrelated maintenance, customer CPE/account changes, or any write outside the typed temporary-site executor | Do not use raw SSH, generic MCP diagnostics, or model-generated command/config text. The executor may only apply the separately enumerated temporary-radio/address/OSPF operations after the explicit workflow trigger and deterministic preflight gates. Router-dead `.1/24`/BGP advertisement remains a separate design and is not implied by `Temp connect`. Site-rename writes are operator-applied under the `rename-site` confirmed-table workflow, never agent-executed. |
| Arbitrary IPAM writes or treating an operator-curated address pair as a verified reservation | The Sheet helper remains the only Sheet write path. If the operator explicitly skips Sheets and supplies curated addresses, the executor must label them operator-curated, check live conflicts, and never claim the sheet was checked/reserved. A Sheet reservation alone never permits a device write |
| Run a customer-CPE scan whose effect on its live wireless association is unknown | May interrupt a paying customer's service; exact-device credentials do not authorize an unverified survey |
| Clear/trip the circuit breaker from agent context | breaker is operator-only (`POST /governance/breaker` or CLI) |
| Bypass `sonar_bounce.py` / `sonar_comment.py` with hand-rolled mutations | governance bypass |
| Register or change a service address (`resources.py set`, `migrate --write`) as a batch agent; or send a credential to an address that is not registered | batch agents refused (NOC_UNATTENDED=1); MCP read-host policy enforces the host name restriction |

## Kill Switches (operator controls)

| Control | Effect |
|---|---|
| `BOUNCE_AUTONOMOUS_ENABLED` unset, empty, or any value other than `true`/`1`/`yes`/`on` (.env, restart svc) | all autonomous bounces blocked (exit 4); `.env.example` ships it commented out |
| `python3 scripts/circuit_breaker.py open <reason>` | bounces blocked until `clear` |
| `python3 scripts/circuit_breaker.py clear` | re-enable bounces after tripping |
| `systemctl stop noc-agent` | stop all processing (pending tickets alerted) |

## Temporary Site Connection Phase Status

- **Available:** human-guided discovery/planning, the paired-row Temp OSPF Sheet helper (`mark-in-use`/`release`/`plan-reservation`, live write+readback verified), read-only `lookup-device-by-exact-ip` LibreNMS corroboration, cached MyMate name resolution (`inventory_resolve.mjs`), Oxidized config pull (`oxidized_fetch.mjs`), and the gated device executors below. For untested operations the operator performs one manually agreed step at a time; the agent reviews sanitized before/after config and readback before suggesting the next. This is not a generic command relay.
- **Device executors (all default-off, all tested offline):** `temp-radio-plan`/`apply`/`rollback` (airOS SSH sed + operator-verified softrestart save; reads live-proven, first live write held only by target stability), `temp-ospf-plan`/`apply`/`rollback` (RouterOS; OSPF turn-up rung via `temp-ospf-apply`, `temp-site-connection` E2E-proven INC-B-01), and `temp-mt-flip`/`rollback`, `temp-ap-wds`, `temp-mt-bounce`, `temp-mt-alias`/`alias-remove`. Every write requires execute flag + `TEMP_SITE_WRITE_ENABLED` + transport-verified flag + host allowlist + exact-device binding; type-wide credentials are never used for writes.
- **Verified/documented:** Authenticated Sonar `accounts.search` by `name` plus account-ID follow-up and forward account-service/inventory query shapes run successfully. `match:true` is substring search and must be exact-post-filtered. Inventory `NAME` value search has no reverse account relation. This unresolved customer↔radio mapping blocks individual CPE changes, not the operator-authorized infrastructure donor AP interruption.
- **Implemented but not operationally provisioned:** backward-compatible encrypted exact-device credential bindings and a separate one-attempt allowlisted airOS diagnostics tool. Existing type-wide tower/path diagnostics are unchanged. No customer bindings, trusted inventory validation, or isolated CPE-secret process are supplied. The MCP registers 16 tools unconditionally; `lookup-device-by-exact-ip` is added when `config.inventory.enabled` is set. Write tools are refused at call time unless kill-switch env flags, transport flag, host allowlist, exact-device binding, and `execute:true` all hold.
- **Implemented, live write+readback verified:** `scripts/temp_ospf_sheet.mjs` supports read-only tab inspection plus a shared user-OAuth module (`scripts/google_user_auth.mjs`) reused by future skills. The reservation writer was reworked for the live paired-host-row schema (13/13 offline tests; `plan-reservation` live write+readback verified; `temp-site-connection` E2E-proven INC-B-01); writes need the Editor grant, full Sheets re-consent, and all three gates. Operator-curated address pairs may bypass Sheets when explicitly recorded and checked for live conflicts.
- A service-account private key must never be sent in chat or committed. The helper supports key-file paths outside the repo with restrictive mode and OAuth read-only scope by default. `noc-agent.service` runs as root; owner-only filesystem permissions do not isolate the key from same-root processes. See the setup and threat-boundary notes in the `temp-site-connection` skill and [config.md](config.md).
- Candidate paths and device state still require fresh authoritative lookup. MyMate omission is not proof of device absence. LibreNMS exact-IP lookup is corroboration only and its `status`/poll time are not live reachability proof; require exact identity agreement and current device reads. Temporary donor-AP customer interruption is authorized for requested recovery, but no device changes execute through MCP. Operator-led manual command validation must preserve snapshots, a working management path, readback, and a tested restore path; the typed executor is deferred until these concrete operations are validated together.

## Protecting the running checkout

The service runs from this checkout, so an edit made here by an agent is live code that nobody reviewed and CI never saw. Agents have done this before: one overwrote `site-pack/profile.json`, others left uncommitted code changes and one-off probe scripts in the production tree. Several layers guard against it. They are listed from the weakest to the strongest, because the weakest ones depend on the agent cooperating.

| Layer | What it does | Covers | Limit |
|---|---|---|---|
| Instructions (`AGENTS.md`, "Repository integrity") | Tells every agent to treat tracked files as read-only, to work in a worktree when asked to change the project, to keep scratch files in `/tmp`, to change data files only through their scripts, and to stop at a refused call | any harness | A model can talk itself past a rule. In a test, a weak model found no script for a change, decided to edit the file by hand, and was stopped only by the layer below |
| OpenCode batch profile (`deploy/opencode-batch.json`, `AGENT_PERMISSION_PROFILE`) | The server hands unattended OpenCode runs a permission profile: edits only under `/tmp` and `logs/`, no reads of `.env`, the credential store or backups, no shell git or package or service commands that change the checkout | OpenCode 2.x batch runs | The shell cannot be fully fenced: `python3 -c` and `tee` still write. Other OpenCode major versions get a warning from `--check-config` and no profile |
| Claude Code hook and prompts (`scripts/hooks/claude_guard.py`, `.claude/settings.json`) | An unattended run (`NOC_UNATTENDED=1`) cannot write any project file outside `logs/`. Interactive sessions are asked before they edit code or instructions, rewrite or discard work with git, install packages or control the service | Claude Code | Interactive prompts rely on the person reading them |
| Read-only checkout for the service (`install.sh --protect-tree`) | The service and every agent it starts see the checkout as read-only except `logs/`, the MCP audit and snapshot folders and the host-key folder, whatever tool or shell trick they use. `.env`, `site-pack/`, `.git`, code and skills cannot be changed; `credentials.enc` and `server.json` stay read-only even inside the writable `mcp-server/config/` | everything the service starts | Does not apply to a person's own shell or an interactive agent session. Takes effect on the next restart. Reading is unaffected |
| Drift detector (`scripts/tree_guard.py`, `noc-treeguard.timer`, `doctor.py --only tree`) | Every 15 minutes it compares the checkout with the committed code and sends one notification for each new change: an edited, staged or deleted tracked file, or a new file that is neither tracked nor ignored. Only paths are sent | every cause, including interactive sessions | Reports after the fact |

**How to change this project safely.** Do not edit in the production checkout. Make a worktree (`git worktree add <dir> -b <branch>`), change and test there, push, and deploy to the production checkout with `git merge --ff-only`. That keeps the running tree identical to reviewed, tested commits, and the drift detector then has nothing to report. If the production tree is already dirty, a fast-forward refuses, and that is the cue to look at the change rather than force it.

**Turning the read-only checkout on.** `bash scripts/install.sh --protect-tree` installs a systemd drop-in; run `systemctl restart noc-agent` when the queue is empty to apply it (see [operations.md](operations.md)). `bash scripts/install.sh --unprotect-tree` removes it. The installer insists on Node.js 20 or newer and reinstalls dependencies, so on a host whose service still runs an older Node, install only the drop-in by hand, from the checkout:

```bash
mkdir -p logs mcp-server/logs mcp-server/state /etc/systemd/system/noc-agent.service.d
sed "s|{{INSTALL_DIR}}|$PWD|g" deploy/noc-agent.protect-tree.conf.template > /etc/systemd/system/noc-agent.service.d/10-protect-tree.conf
systemctl daemon-reload
```

Delete that file and run `systemctl daemon-reload` to turn it off again. Before relying on it, watch `python3 scripts/tree_guard.py check` for a while: anything the running system writes inside the checkout shows up there as drift, and each such path would need to be added to the writable list in `deploy/noc-agent.protect-tree.conf.template`.

## Audit Trail

- `logs/decisions/<YYYY-MM>.jsonl` — one record per ticket decision (status, confidence,
  classification, evidence, escalation_reason) + bounce records (action, reason, upstream).
- `logs/webhook.log` — raw batch/job lifecycle.
- Sonar comments — human-visible record of analyses and bounces.
- `GET /governance` — live breaker state.

## Change Review

Threshold changes (`IHN_TOWER_THRESHOLD`, `CORRELATED_CONFIDENCE_THRESHOLD`,
`BREAKER_MAX_ERROR_RATE`, etc.) are policy changes: update this file + `docs/config.md` env table in
the same commit, and note the change in the weekly metrics report window.
