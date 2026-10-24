# Skills System

> Operator manual index lives in the repo root [README](../README.md).

## 11. Skills System

Skills are the agent's knowledge modules. Each lives in `.opencode/skills/<name>/SKILL.md` with YAML frontmatter. The agent loads them on demand at runtime via the `skill` tool — there is no build or registration step. Changes take effect on the next agent session.

### Current skills

| Skill | Purpose |
|-------|---------|
| [`path-analyzer`](../.opencode/skills/path-analyzer/SKILL.md) | Live router path triage — traceroute, btest to fiber hop-by-hop, /30 direct, variance + true multipath |
| [`wireless-health`](../.opencode/skills/wireless-health/SKILL.md) | Investigate a flagged wireless link (signal/CCQ/jitter/loss) — returns data to append to same path-analyzer comment |
| [`sonar-api`](../.opencode/skills/sonar-api/SKILL.md) | Sonar GraphQL reference for tickets plus verified account-name search, account-ID follow-up and forward service/inventory query shapes; target customer↔radio and live subscriber status still need exact linked evidence |
| [`ticket-context`](../.opencode/skills/ticket-context/SKILL.md) | Parse ticket subjects to identify the correct tower site (handles fiber-network vs tower-site naming; peak/night detection) |
| [`comment-formatting`](../.opencode/skills/comment-formatting/SKILL.md) | Format path analysis results as HTML Sonar comments with colored callouts; canonical `Direct Path Analysis` header (legacy `Network Bottleneck Analyzer` accepted when parsing stored comments) |
| [`duplicate-detection`](../.opencode/skills/duplicate-detection/SKILL.md) | Detect duplicate tickets by matching tower site + issue type; auto-handle high-confidence duplicates |
| [`correlated-issues`](../.opencode/skills/correlated-issues/SKILL.md) | After a DEGRADED analysis, find other active tickets downstream of the same bottleneck device |
| [`correlated-auto-return`](../.opencode/skills/correlated-auto-return/SKILL.md) | When DEGRADED path shares a critical red hop with another ticket, auto-return downstream to CS L2 as correlated upstream issue (IP+name exact, 75 threshold, no category change) |
| [`google-chat`](../.opencode/skills/google-chat/SKILL.md) | Format and send structured Google Chat notification cards; silent for handled_duplicate + auto_bounced_ihn(_peak) + correlated_bounced + queued_for_peak + auto_closed* |
| [`mymate`](../.opencode/skills/mymate/SKILL.md) | MyMate network monitor REST API — device status, outages, ping history, engine health (read-only `nocagent` auth) |
| [`librenms`](../.opencode/skills/librenms/SKILL.md) | LibreNMS REST API (`http://10.0.0.5/api/v0`, `X-Auth-Token`) — sysName/hardware/geolocation corroboration (not site truth) |
| [`ihn-auto-return`](../.opencode/skills/ihn-auto-return/SKILL.md) | Auto-return perfectly HEALTHY slow-tower tickets (all hops green, zero warnings) to CS Level 2 as IHN/tower-connection with tower-confidence scoring and single-bounce anti-loop (80, +40 exact) |
| [`peak-scheduler`](../.opencode/skills/peak-scheduler/SKILL.md) | Queue night/peak tickets outside 8-9 PM CT; drain at 20:05 CT, re-analyze at peak, bounce if still HEALTHY |
| [`temp-bgp-peering`](../.opencode/skills/temp-bgp-peering/SKILL.md) | Emergency fiber failover — detect looping advertised subnets (Echo 10.0.50.1 / Foxtrot 10.0.60.0/24) and add TEMP BGP peers to redundant fiber bgprr (Alpha 10.0.20.1 / Bravo 10.0.21.33) with loop-aware downstream iteration |
| [`temp-site-connection`](../.opencode/skills/temp-site-connection/SKILL.md) | Operator-authorized temporary site recovery, including necessary temporary customer interruption on the selected donor infrastructure AP. Sheet helper supports bounded inspect/reserve/release against the owner's tab layout; live typed executor with five gates + per-incident bindings; explicit OSPF turn-up rung (L2-proven ⇒ near executor + far mac-telnet OSPF, verify before root-cause). E2E-proven (newest canonical). Customer CPE credentials are not required for infrastructure radio discovery. |
| [`temp-site-connection-undo`](../.opencode/skills/temp-site-connection-undo/SKILL.md) | Reverse one temp connection by incident ID: verify repaired primary first, remove temp OSPF by tag, restore radios to saved originals (MT/airOS), release the Sheet row last. |
| [`tracker-sheet`](../.opencode/skills/tracker-sheet/SKILL.md) | File NOC tickets or alert-only rows from natural language — verifies via MyMate/LibreNMS/UISP and appends one gated row to the NOC Ticket Tracker sheet; intent is `create a ticket` (Create Ticket=TRUE → automation makes the real ticket) or `create an alert` (Create Ticket=FALSE → alert-only row, no ticket); 6 v1 types, after-9pm + outage-vs-repair rules, grid/limited-scope priority guidance; misaligned/chain-imbalance/turned link rows auto-post an AP-side RSSI evidence follow-up comment on the created Sonar ticket (`scripts/rssi_drop_graph.py` via UISP series, persistent ≥6 dB step detection) with the chart PNG uploaded to Sonar (`POST /api/files`, field `files[0]`) and embedded via `<img src="files/<id>">` on the comment; renderer rule (operator decision): the NocAgent/UISP chart is ALWAYS the deliverable (UISP first for Ubiquiti radios) — the authentic LibreNMS native render (`--librenms-render`, now working; SSO header auth via `LIBRENMS_WEB_USER`) is only an automatic fallback (`--librenms-fallback`) when UISP has no usable data or the device isn't in UISP — and NEVER a Tracking IP (a brief bad-signal return of a misaligned radio is not a resolution signal); live exact duplicates (same towers + issue, status ≠ Resolved) never get a second ticket — the helper refuses the ticket path, the workflow reuses the existing ticket # (see row) and files a follow-up alert-only row carrying that # in the Ticket # column (`ticket` field, operator decision) |
| [`schedule-field-tech`](../.opencode/skills/schedule-field-tech/SKILL.md) | Dispatch a field tech from a Sonar ticket via `scripts/schedule_dispatch.mjs`: subject→title, site coords→location, ticket details+comments→description; direct insert on the tech's calendar when the Tower Dispatch consented account has writer/owner access, else Tower Dispatch `primary` with the tech as guest; dedup by ticket key, readback-verified, audited; on dispatch the Sonar ticket flips `PENDING_INTERNAL → OPEN` with NO comment (dispatch lead's standard; gate `DISPATCH_SONAR_ENABLED` + `--confirm-sonar true`, other statuses refused); optional final step flips the tracker row to `Dispatched`. Live-validated (direct insert to a tech calendar, tracker row flipped) |
| [`reprogram-device`](../.opencode/skills/reprogram-device/SKILL.md) | Restore factory-reset UBNT devices (airOS airMAX AND Wave/MLO, incl. backhaul AP/ST) from their backup config (Oxidized/UISP). Handles multiple factory-reset waves on one gateway (one `waveDHCP` + subnet per radio) |
| [`program-new-device`](../.opencode/skills/program-new-device/SKILL.md) | First-time bring-up of BRAND-NEW devices (no backup config exists): /29 IP allocation from the org's IP Network sheet (site-pack profile) (correct network tab + traceroute in-use check), gateway + `bgp-networks` on the feeding MikroTik, Wave GUI SSH enable via `scripts/wave_gui.mjs` + SSH/`ucfg` programming (static /29, SNMP `<snmp-community>`/`[GPS]`/`<snmp-contact>`), LibreNMS + MyMate registration incl. new sites. HARD RULE: `PUT /api/v1.0/services` is full-replace — GET-merge-PUT the whole doc and re-banner-verify (burned live: AP lost ssh/web). Validated on a Wave-LR pair |
| [`rename-site`](../.opencode/skills/rename-site/SKILL.md) | Agent-driven site rename after tower-host/ownership change (executes via `scripts/airos_rename.py`): full mode (MikroTik identities, infra radios, backhaul/AP/CPE SSIDs, Netonix, MyMate site+map) plus SSID-only fast path; backhaul /29-gateway move before any SSID drop, CPEs-before-AP order, separate `customer-airos`/`customer-mt` credential pools |
| [`onboarding-wizard`](../.opencode/skills/onboarding-wizard/SKILL.md) | Interactive first-run setup — trigger `onboard me`: ability tour, `.env` walkthrough, Sonar + Google (webhook + per-feature OAuth via `scripts/google_user_auth.mjs`), optional integrations, MCP credential store, systemd install, site-pack profile, topology rebuild; every phase verified read-only with `scripts/doctor.py`; optional phases skippable, required phases fail closed |
| [`create-site`](../.opencode/skills/create-site/SKILL.md) | Create a NEW site pin across all four external records in order — Sonar `createNetworkSite` (required `height_in_meters`; coordinates ride on the address; custom state subdivision enum resolved via `addresses(city=...)`), Tower Directory alphabetical row insert (4-segment address commas `, City, ST, ZIP`; zero-padded Sonar ID column), UISP site (flat POST — no `data` wrapper, no geoposition per house convention), MyMate site (`kind:"other"`; device→site assignment stays operator-only). Validated live |

### How to add a new skill

1. Create the directory:
   ```bash
   mkdir -p .opencode/skills/<skill-name>
   ```

2. Write `.opencode/skills/<skill-name>/SKILL.md` with frontmatter:
   ```markdown
   ---
   name: <skill-name>
   description: Use when <trigger condition>. <What it does.>
   ---

   # <Skill Title>

   <Knowledge content>
   ```

   **Frontmatter rules:**
   - `name` must match the directory name
   - `description` must start with "Use when..."

3. Keep the body under 500 lines. Move deep detail into a `references/` subdirectory.

4. Add the skill to the Skills list in [AGENTS.md](../AGENTS.md) so the agent knows it exists.

5. Test it by loading the skill in the OpenCode TUI:
   ```
   opencode
   > /skill <skill-name>
   ```

### Skill design principles

- **One domain per skill** — don't mix unrelated knowledge
- **Self-contained** — an agent loading one skill shouldn't need another to understand it
- **Reference, not narrative** — proven techniques and patterns, not stories
- **Progressive disclosure** — `SKILL.md` for overview, `references/` for deep detail

### How skills are loaded

The agent uses the `skill` tool at runtime to load a skill's `SKILL.md` into its context. There is no registration, build, or restart step. Editing a `SKILL.md` file changes agent behavior on the next session — the running webhook server does not need a restart (it spawns a fresh OpenCode process per batch).

---

