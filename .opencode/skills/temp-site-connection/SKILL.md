---
name: temp-site-connection
description: Use when an operator asks to connect or restore a temporary path to an offline site, inspect/reserve a Temp OSPF /30, assess customer impact, or troubleshoot a backhaul. Defines the authorized temporary donor-AP interruption and guarded network-write workflow; live writes require the dedicated deterministic executor.
---

# Temporary Site Connection

Provides an operator-triggered workflow for restoring reachability to an isolated site or area. It guides discovery, candidate assessment, and—in a specifically configured deployment—inspection or reservation in the `Temp OSPF` Google Sheet. Paths may be direct, multi-hop, or use a wireless AP when a site's router is dead but its switch/radios remain online. The operator has authorized temporary customer disconnection on a donor AP when necessary for an explicitly requested recovery; do not request duplicate approval solely because that AP has customers. Minimize and disclose the expected impact, then restore its original role/configuration after the temporary path is established. Execution uses the live typed MCP executor (see **Live executor reference** below: `temp-radio-*`, `temp-ospf-*`, `temp-mt-*` tools with kill-switch, transport, allowlist, and exact-device-binding gates). The standing authorization covers the donor interruption; the executor's gates cover the technical path.

## Permission and activation boundary

This skill is **not itself a device-write tool** — but YOU, the agent executing this skill, **CAN and MUST write to network devices** whenever the workflow below requires it. The write channel is the live typed executor (MCP tools, or equivalently `mcp-server/dist/tool-cli.js` from any harness — same handler, same gates). Never stop at plan-only with "I cannot write" / "I have no write access": if the five gates pass (kill switch, transport flag, allowlist, `execute:true`, exact-device binding), you execute. A gate MISS is the only legitimate reason to stop, and you must name the exact gate. Current project controls are:

- MyMate `nocagent` is read-only.
- `mcp-server` network diagnostics are allowlisted read-only. Do not bypass them with arbitrary RouterOS commands.
- The operator has granted standing scope-specific authorization for this skill to temporarily interrupt customers associated with any donor AP selected as necessary during an explicitly requested temporary-site recovery. Do not ask for a second approval solely because that donor AP has customers; do report known/unknown client count, expected interruption, selected donor, and restoration plan. This does not authorize customer CPE/account changes or unrelated maintenance.
- Router/radio writes go through the live typed MCP executor (`temp-radio-apply`, `temp-ospf-apply`, `temp-mt-flip`, `temp-mt-bounce`, `temp-ap-wds`, each with `plan`/`rollback` counterparts). Every write requires ALL of: `TEMP_SITE_WRITE_ENABLED=true`, the matching transport flag (`TEMP_SITE_AIROS_TRANSPORT` / `TEMP_SITE_ROUTEROS_TRANSPORT` / `TEMP_SITE_MT_TRANSPORT` = `verified`), host in `TEMP_SITE_ALLOWED_HOSTS`, explicit `execute:true`, and a provisioned exact-device binding. Never bypass these gates with raw SSH, generic MCP diagnostics, or generated config text. `scripts/mk.sh` is the approved READ path (plus pre/post readbacks); it is never a write path.
- The Google Sheet helper `scripts/temp_ospf_sheet.mjs` reads the live `Temp OSPF` tab (paired host rows) with user-OAuth read scope. Writes stay disabled unless the three explicit gates and the command-line confirmation are enabled after schema review.
- The helper may update only one uniquely matching existing `/30` row's in-use field and two site fields, then verifies readback and audits the result. It does not choose a subnet, allocate IPs on routers, calculate overlap against device routes, or itself authorize network configuration. A curated `/30` supplied by the operator may be used without Sheets, but must still pass live address/route conflict checks.
- Network writes for this workflow go through the live typed executor above. Do not bypass its gates via raw SSH, generic MCP diagnostics, or generated commands. If a needed operation has no typed tool (or a tool lacks a parameter the link requires), stop and build/review/test the tool change first — do not hand-execute around it except for a single-field correction recorded same-session in the incident file (e.g. a `type=` flip, see §8).

When Sheet credentials/configuration are absent, continue with whatever independent read-only discovery is available and report the exact setup blocker. Once configured, use `node scripts/temp_ospf_sheet.mjs inspect` on demand before recommending a `/30`. The safe default for new configuration is read-only; upgrading Sheet permission to Editor and enabling writes are separate operator actions.

## Available now: Temp OSPF Sheet helper

`node scripts/temp_ospf_sheet.mjs <command>` (Node.js 20+). `inspect` and `plan-reservation` are read-only; `mark-in-use` and `release` write one `/30` pair of rows and are gated (`TEMP_OSPF_ACCESS=write`, `TEMP_OSPF_WRITE_ENABLED=true`, `--confirm-write true`, an Editor grant for the consenting Google user). Service-account and user-OAuth setup, `.env` settings, the sheet's header/row layout, house write conventions and the exact operator command: `references/sheet-helper-setup.md`. Rules that must hold:

- Run `plan-reservation` (read-only) before `mark-in-use`. The helper only records a reservation; do the live address/route overlap checks on the routers first, and read back its `marked_in_use` result and audit entry.
- `mark-in-use` writes Router name x2, checkbox x2 and incident reason x2 (6 cells, never `date re-opened`); `release` frees the same cells and stamps `date re-opened` = today on both rows (8 cells). Both re-read before writing, verify by readback and audit. An uncertain write is never retried automatically: inspect the rows in Google Sheets first.
- Never enable writes from header guesses; the helper fails closed on header drift, duplicate/missing/non-consecutive rows and occupied rows. Keys and tokens live outside the repo (mode 0600) and never go into chat, Git or tickets.
- Without Sheet configuration, continue with independent read-only discovery and report the exact setup blocker. A curated `/30` from the operator may skip the Sheet, never the live conflict checks.

## Implementation status

End-to-end live-proven on two incidents; the newer one extends the pattern with a token RF hop: donor 5GHz sector → island AP, temp /30 across two bridges, mac-telnet far leg. It is the newest canonical example — see its OSPF turn-up recipe below. **Staging purge (owner action):** all prior temp connections were reverted and every Temp OSPF reservation re-opened; the ONLY live staged undo in the system is the newest incident (temp /30 `10.0.70.48/30` re-reserved rows 39/40 plus alias + donor-sector flip, `logs/incidents/<incident-id>.md`). Older incident files carry a SUPERSEDED stamp — their cleanup inventories are historical, never action them without live tag verification. Earlier partial proofs (patterns, not staging): the flip pattern, the mac-telnet far leg (cost-10 hijack lesson), and undo discipline.

| Capability | State | Boundary |
|---|---|---|
| MyMate topology/outage and device reads | Existing read-only API; exact MyMate links/outages can omit valid inventory devices | Absence from MyMate links/outages is not proof of device absence. Continue exact-IP discovery via authoritative inventory (UISP, LibreNMS corroboration); do not infer customer count from missing data |
| Sonar GraphQL customer/inventory query | **Account-name search and ID follow-up query verified; target customer↔radio linkage unresolved** | `accounts.search` by `name` uses substring matching. Authenticated search returned zero candidates for one radio handle; another returned one non-exact name candidate with a non-activating account status, enabled catalog definitions, no package assignments, and no linked inventory items. `Service.enabled` is not per-customer subscription status. Radio-value lookup has no reverse item relation. Do not equate these with target CPE ownership or no subscriber |
| Exact-device customer CPE credential resolution | **Narrow, read-only mechanism implemented; not provisioned or enabled for this workflow** | Optional encrypted-store bindings and a separate one-attempt airOS diagnostics tool require exact device ID + host. Existing type-wide diagnostics remain unchanged. No real CPE credentials/bindings, trusted ID↔host inventory validation, isolated credential process, or CPE Site Survey are deployed |
| Individual customer-CPE station-side Site Survey | **Blocked / not implemented** | Not required for infrastructure-radio inventory or the named temporary AP/ST link. No allowlisted command or model/firmware-verified safe method exists for a separate CPE reassociation workflow; never substitute airView or another potentially disconnecting scan |
| Temp OSPF Sheet paired reservation | **Live-proven (re-verified against the owner's new tab layout)** | `plan-reservation` (read-only) locates the consecutive host pair, reports free/occupied, lists exact cells. `mark-in-use` writes Router name × 2 + checkbox × 2 + incident reason × 2 with prewrite re-read, readback verify, audit. `release` stamps `date re-opened` on both rows. The owner re-open wiped all prior reservations — only the current incident's `/30` (rows 39/40) is reserved. Writes stay gated (`ACCESS=write` + `WRITE_ENABLED=true` + `--confirm-write`) and need an Editor grant + full Sheets scope re-consent. Operator-curated addresses may bypass Sheet only when explicitly recorded and checked against live conflicts |
| Temporary donor-radio/address/OSPF executor | **Live-proven end-to-end** | Radio: `temp-radio-plan` (read-only) + `temp-radio-apply`/`rollback` (SSH sed + softrestart save, snapshot, gates + host allowlist). Router: `temp-ospf-plan` + `temp-ospf-apply`/`rollback` (preflight conflict check, tag removal, explicit `ospfType`). MT: `temp-mt-flip`/`rollback` (full 5-field station recipe), `temp-mt-bounce`, `temp-ap-wds`. Bindings: `provision_exact_binding.mjs` (pool-pin, dry-run default) + `fleet_bind_sweep.mjs` (banner-probe + auto-bind, customer-skipping; airOS only — routers pinned manually per incident). Type-wide creds never used for writes. Missing flags/bindings fail closed with the exact gate named. |
| Candidate plan/report | **Available when execution is blocked or deferred** | Report evidence and unknowns; do not claim a live path, zero customers, reservation, or successful connection. When the executor runs, the result format below carries the verified outcome |

## Live executor reference (authoritative)

This section describes the system as it actually runs. If code and this section ever disagree, the code governs AND this section must be updated in the same change — context drift is a defect.

### Tools (MCP `noc-network-troubleshooter`, source `mcp-server/src/temp-site/`)

| Tool | What it does | Gates |
|---|---|---|
| `temp-radio-plan` / `temp-ospf-plan` | Read-only validation + exact apply/verify/rollback steps. Never connects. | None (always available) |
| `temp-radio-apply` / `rollback` | airOS SSH: snapshot → anchored sed on allowlisted `/tmp/system.cfg` keys → re-read verify → `rc.softrestart save` → reboot → confirm-window check, auto-restore on miss. Secured-AP joins resolve the PSK at execute time (`pskSource: ap-config` preferred, `env` fallback); audit records the SOURCE only. | All five executor gates below |
| `temp-ospf-apply` / `rollback` | RouterOS over one persistent SSH session: preflight (tag-collision + CIDR readback rules) → tagged `/30` address + interface-template → readback verify → tag rollback on miss. | All five executor gates below |
| `temp-mt-flip` / `rollback` | MikroTik full 5-field station recipe (`mode=station-bridge`, ssid, `frequency=auto`, `wireless-protocol=any`, `rate-set=default`) with literal-previous restore. | All five executor gates below |
| `temp-mt-bounce` | Re-associate bounce for station-bridge clients (no config change). | All five executor gates below |
| `temp-mt-alias` / `temp-mt-alias-remove` | Same-subnet temp address on a station radio bridge (island-subnet alias for forced-L2 ping/SSH diagnosis), tagged for incident cleanup. | All five executor gates below |
| `temp-ap-wds` | AP-side `wds-mode` toggle with previous-value snapshot/restore. | All five executor gates below |

### Calling the executor from ANY harness (universal CLI)

The tools above are MCP-tool names, but they are not tied to any one harness. Every one is callable from any shell or agent harness via `mcp-server/src/tool-cli.ts`, which invokes the exact `NocMcpServer.handleToolCall` entry point the stdio transport uses — same schemas, same five gates, same bindings, same audit. There is no second implementation and no gate bypass.

```bash
cd mcp-server && npm run build
npm run tool -- --list                                    # all tool names
npm run tool -- temp-radio-plan '{...}'                   # read-only plan (always available)
node dist/tool-cli.js temp-radio-apply --args-file /tmp/args.json   # gated write
 echo '{...}' | node dist/tool-cli.js temp-ospf-plan --stdin
```

Exit 0 = success, 1 = tool blocked/failed (gate misses fail closed naming the gate — e.g. `execute:true` with no binding is refused before any connection), 2 = usage/JSON error. Same repo `.env` fallback as the MCP server. Provision per-incident bindings first (`scripts/provision_exact_binding.mjs --from-pool <name> --apply`; airOS sweep via `fleet_bind_sweep.mjs --site <frag>`); without a binding every apply fails closed. Proven: `temp-radio-plan` + `temp-ospf-plan` + `temp-radio-apply execute:false` dry-runs plus a binding-gate refusal, all from a plain shell with no MCP client.

#### Tool → CLI mapping (every MCP tool, same name, JSON args)

| MCP tool (auto-available where the harness supports MCP) | Any-harness equivalent (from repo root) |
|---|---|
| `temp-radio-plan` | `node mcp-server/dist/tool-cli.js temp-radio-plan '{...}'` |
| `temp-radio-apply` / `rollback` | `node mcp-server/dist/tool-cli.js temp-radio-apply '{... "execute":true}'` |
| `temp-ospf-plan` | `node mcp-server/dist/tool-cli.js temp-ospf-plan '{...}'` |
| `temp-ospf-apply` / `rollback` | `node mcp-server/dist/tool-cli.js temp-ospf-apply '{... "execute":true}'` |
| `temp-mt-flip` / `rollback` / `bounce` | `node mcp-server/dist/tool-cli.js temp-mt-flip '{...}'` (etc.) |
| `temp-mt-alias` / `alias-remove` | `node mcp-server/dist/tool-cli.js temp-mt-alias '{...}'` (etc.) |
| `temp-ap-wds` | `node mcp-server/dist/tool-cli.js temp-ap-wds '{...}'` |
| `run-diagnostics` | `node mcp-server/dist/tool-cli.js run-diagnostics '{"host":..., "deviceType":"mikrotik-ssh", "command":"..."}'` |
| `lookup-device-by-exact-ip` | `node mcp-server/dist/tool-cli.js lookup-device-by-exact-ip '{"ip":"..."}'` |
| `list-device-types` | `node mcp-server/dist/tool-cli.js --list` |

From any other cwd, prefix with `NOC_MCP_DOTENV=<repo>/.env NOC_MCP_CONFIG=<repo>/mcp-server/config/server.json` and call `dist/tool-cli.js` by absolute path — credential/audit paths self-anchor to the mcp-server root, so reads AND gated writes work identically (proven from `/tmp`: `lookup-device-by-exact-ip` matched a backhauls router fresh, `run-diagnostics system-resource` succeeded). Companion scripts are equally cwd-proof: `scripts/mk.sh`, `temp_ospf_sheet.mjs`, `provision_exact_binding.mjs`, `sonar_comment.py`, `sonar_bounce.py` all resolve the repo root from their own location (`NOC_DOTENV` override for the `.env`); only the systemd units (rendered by `scripts/install.sh`) and `opencode.json` keep absolute install paths, which is correct for machine-local service registration.

### The five executor gates (every apply carries all five; any miss fails closed naming the gate)

1. `TEMP_SITE_WRITE_ENABLED=true` (runtime kill switch).
2. Transport flag `=verified` for the device family (`TEMP_SITE_AIROS_TRANSPORT` / `TEMP_SITE_ROUTEROS_TRANSPORT` / `TEMP_SITE_MT_TRANSPORT`).
3. Host in `TEMP_SITE_ALLOWED_HOSTS` (exact IP or CIDR).
4. Explicit `execute:true` (absence/`false` returns the dry-run plan only).
5. Exactly one exact-device binding for (`deviceType`, `deviceId`, `host`) — provisioned per incident with `scripts/provision_exact_binding.mjs --from-pool <name> --apply` (dry-run default; prints names only, never secrets). `fleet_bind_sweep.mjs --site <frag>` auto-binds airOS radios (dropbear banner + single echo probe, customer subnets/names always skipped); routers are pinned manually. Type-wide pool creds are NEVER used for writes. Pool names differ per site (e.g. one router took `admin-primary`, another needed `admin-2nd`) — an auth failure names the binding, not the password; try the next pool cred instead of retrying blindly.

### OSPF template type rule (hard requirement, schema-enforced)

`ospfType` (`ptp` | `broadcast`) is REQUIRED on every `temp-ospf-plan`/`apply` — no default, ever. The executor previously hard-coded `type=ptp`, which a live run proved wrong: both `/30`s landed on `bridge1` (broadcast segments) and adjacency stalled at Exchange with retransmits until both templates were flipped to `broadcast`, after which Full formed. Determine the type from LIVE state before planning:

- Facing interface is a bridge or a bridge-port member (`/interface bridge port print` shows it) → `broadcast`.
- Facing interface is a standalone ether carrying only the temp link → `ptp`.
- **Both ends MUST use the same type — `ptp` on one end + `broadcast` on the other NEVER forms adjacency.** The bridge side dictates: if either facing interface is a bridge, use `broadcast` on BOTH ends (broadcast on a standalone ether still elects DR/BDR fine with 2 speakers). Example: near side first went up `ptp` (ether5 standalone) while the far bridge1 side was `broadcast` — no adjacency until the near template was set to `broadcast`, then Full in ~2 min.
- **Apply on the router that owns the L2, and verify which one that is FIRST.** `/system identity print` before every apply: backhauls ≠ pppoe. The correct near router is the one whose `/ip neighbor` table hears the far MAC and whose port feeds the AP-side radio. Example: a `ptp` template + `.97/30` went onto a site's BACKHAULS ether5 (wrong router — no neighbor ever appeared) while the live leg belonged on the same site's PPPOE bridge1 (`.121/30`) — caught only because the far router's `Full` neighbor entry named router-id `10.0.80.60`. Strays on the wrong router must be removed (exact-tag `remove`), not left.
- MTU is a separate check (both ends `1500`; 1472-byte payloads passed while Exchange still stalled — large-packet pass does NOT rule out a type mismatch).
- Readback verification checks `type=` alongside cost and network.

### OSPF area-name note

Areas match on area-ID, not name. The donor router uses `backbone-v2`, the island router uses `backbone` — both are area-id `0.0.0.0`, so adjacency forms with each router's LOCAL area name in its own template. Never "normalize" one side's area name to the other's.

### PTP/PTMP protocol lock (operator rule)

Temp flows NEVER change airMAX/MikroTik PTP/PTMP protocol modes as part of a flip. The airOS allowlist (`mcp-server/src/temp-site/airos-ssh.ts`) is closed to protocol keys by design; the MT station recipe pins `wireless-protocol=any` (join whatever the AP speaks) and never selects a specific protocol. Protocol-mode changes happen ONLY when the specific link requires it — e.g. joining a backhaul radio to a customer AP for a temp path — as a separate operator-reviewed step with its own readback, never bundled into a mode/ssid/frequency change.

### Worked incident

Most recent canonical run, use it first: donor sector station → island AP RF hop, temp alias for L2 proof, temp `/30` `10.0.70.48/30` across the donor router `bridge1` ↔ the island router `bridge1` (broadcast both ends, cost 1000, tagged), far leg via mac-telnet (`scripts/mt_mactelnet.mjs --jump <near-router> --mac <island-MAC> --write --incident-id <incident-id>`), Full adjacency + DR/BDR, island default restored (OSPF 110), island iBGP to both RRs re-formed with ZERO BGP changes, customer PPPoE peers round-trip from the agent host. Also proven there: a hung donor radio answers on-subnet pings only — the fix is a PoE power-cycle of its feed port from the host router (documented AGENTS.md procedure), not an executor reboot. An earlier run (donor router ↔ island router, `10.0.70.76/30`, cost 1000, Full both sides, island default unchanged via the upstream site) is now SUPERSEDED-stamped in its incident file. Other precedents: the two-phase flip, the mac-telnet far leg (mesh cost 10 hijack — hence the 1000/10000 schema lock), and undo discipline.


## Hard safety rules

1. **Temporary donor-AP customer interruptions are authorized within this skill's scope.** A `Temp connect <site>` request authorizes necessary temporary role/config changes to infrastructure backhaul/sector radios, including disconnecting customers associated with a selected donor AP. Do not ask for duplicate approval solely because that AP has customers. Report known/unknown client count, minimize outage duration, and restore the donor's verified original configuration. This does not authorize customer CPE/account changes or unrelated maintenance.
2. Per-customer Sonar identity/status checks are not required for a donor infrastructure AP role change. Do not claim the donor has zero customers unless fresh evidence supports that claim.
3. Any separately requested customer-CPE reconfiguration needs its own validated identity/status and safe fallback workflow; this donor-AP authorization does not cover it.
4. Prefer a feasible lower-impact option when operationally comparable, but do not delay necessary outage recovery solely to avoid the authorized temporary donor interruption.
5. Scouting scans are authorized within bounds. A brief MikroTik wireless scan (`/interface wireless scan`, ~10s off-channel) to listen for an island beacon is allowed: prefer 0-client APs; APs WITH customers may scan when island-adjacent, because a 0-connection AP found that way becomes the donor and the flip pattern preserves customers afterward (verified: approved scans; one run used 16 scouts). Disclose any customer blip. Never run airView, spectral analysis, dwell-type frequency scans, or customer-CPE Site Survey for discovery — listening for beacons is not surveying customers. A separate customer-CPE survey operation must still be model/firmware-validated and allowlisted before use.
6. Do not guess radio configuration, RouterOS syntax, credential, interface, SSID/security, subnet, route, or BGP export policy. Read and verify current state first; unsupported/ambiguous state is a blocker.
7. Do not run concurrent changes. Read-only lookups may be bounded/parallel; reservations and device writes are serialized, one link at a time, with a readback after each operation. A timeout after a write is an unknown result: inspect state before retrying.
8. Every temporary object must be uniquely tagged with an incident/change ID and a clear `REMOVE WHEN REPAIRED` marker where applicable. Save before-state and exact rollback/cleanup actions before the first write. Never remove an existing object merely because it resembles a temporary object.

## Inputs and trigger parsing

Accept requests such as:

- `temp connect to <site>` — infer the failed segment and find paths into the offline area.
- `connect <siteA> 5N to <siteB> 5S` — treat the named sector endpoints as operator-supplied candidates, not as pre-verified topology or customer-safe choices.
- `Temp connect Yankee site` — for the operator-proposed example, discovery seeds are Zulu 5S (`10.0.80.5`), Yankee5N, Yankee5S, and curated host addresses `10.0.70.113/30` + `10.0.70.114/30`. These names, roles, outage conditions, address availability, and sequence remain subject to live verification; the curated addresses mean Sheet access may be skipped, not that conflict checking may be skipped.

Resolve canonical site/device/radio identities from authoritative inventory, using MyMate outage/link/device reads as one source. If MyMate does not return a user-named radio, continue with an exact management-IP lookup in LibreNMS and UISP inventory; MyMate's links/outages are not a complete device catalog, and absence there is not proof the radio does not exist. Use LibreNMS only to corroborate hardware/name/status, not as site truth. Avoid repeatedly downloading MyMate's full ~35 MB device catalog. A user-supplied IP/name is a discovery seed; verify the inventory match and current state before planning writes. Ask a concise clarification if multiple devices or site identities remain plausible. For an area outage, determine which sites are in the affected island and include any reachable downstream site that could provide a valid way in, including a daisy-chain through an online switch and functioning radio when a router is dead.

### Transit-shadowed outages: monitor-down is not power-dead (verified)

When a transit site loses power, every site behind it drops in MyMate with near-identical timestamps — that is monitor-path loss, NOT proof the island lost power. Example: Victor (power-dead, operator-confirmed) and Whiskey (powered, cut off) went dark the same second, yet the island radio `whiskey24o` was still beaconing, heard steady at -72 dBm / SNR ~31 from the donor. The earlier agent run misread this as a two-site power outage and declared the job blocked; the operator correction is now the rule:

- Same-second multi-site drops sharing one backhaul chain mean ONE transit failure shadowing downstream sites — treat the cut-off site as a SINGLE island, not a multi-site power event. Find the chain in the transit backhauls router's ether comments (e.g. Victor backhauls `ether4 "whiskey pb5"` = Whiskey is fed through Victor) and in site geo-proximity.
- Never declare island power state from MyMate `down` alone when the monitor path traverses a dead transit site. Island power is proven ONLY by RF evidence (donor-side scan showing the island ssid/BSSID) or tower-host confirmation.
- Gate order is therefore scan-first, flip-second (see island-beacon gate rung 0 above). The scan is authorized scouting (~10s off-channel blip, disclose it); it costs nothing against a dead island, while a blind flip strands the donor in station mode for no reason.

### Ether-down vs power-dead: the facing-port check (verified)

Same-second whole-site dark is NOT by itself a power outage. Before declaring the island power-dead, log into the NEAR-side router(s) and read the facing ethernet state — an earlier agent run got this wrong (declared the island power-dead from MyMate `down` + a stale `no power` device note) and the operator correction is now the rule:

- **Read the facing port first:** `/interface ethernet print` + `monitor <iface> once` on the router feeding the downed AP. `no-link` on exactly that port while sibling ports (sectors, other backhauls) are RUNNING localizes the fault to that cable/radio, not the island.
- **PoE + power-source facts decide the meaning:** `poe-out=off` with an `(external PoE)` comment (or injector/switch feed) means the radio has its OWN power — a `no-link` there is an ether drop on a powered radio, never evidence the island lost power. If PoE was tested (`auto`/`forced`, no draw) and deliberately left off to avoid damaging the radio, record that — do not re-enable blindly.
- **Stale notes are not diagnosis:** a `no power` device note from a week-old ticket (e.g. `MM/DD/YYYY … <ticket-id>`) must never outweigh fresh facing-port + RF evidence. Quote the note's date and keep it as history.
- **RF settles it:** if the island sectors beacon (donor-side scan hears the island ssid) AND/OR the downed AP itself beacons, the island is powered. A powered island behind a dead backhaul ether is the temp-link case, not a dispatch-first case.
- **pppoe vs backhauls:** check BOTH routers. Example: Quebec pppoe eth1–5 all RUNNING (sectors alive) while Quebec backhauls ether5 (`papa colony`) showed `no-link` — the fault sat exactly on the backhaul-AP ether. Report per-router, per-port.

### Autonomous ether-down workflow: temp-link to reach and reboot the AP

When the triage above shows a powered island (or powered downed-AP) behind a dead backhaul ether, the NOC agent works the full loop autonomously — temp-link in, diagnose the AP over the bridge, reboot-or-ticket, verify, undo:

1. **Rank donor APs by cardinal direction + names + the grading rubric.** From online nearby sites, pick sectors whose names face the island (`5e/5n/5s/5w` + site orientation) and that plausibly see it, then grade every pair per the **AP candidate grading rubric** (flip-pattern section — island-zero is a hard gate, donor-empty outranks, signal only orders the eligible set). Record signal + client identities + grade per candidate (example: `quebec5e → papacolony5w` PRIMARY, 0 CX both ends, -77 dBm; `quebec5n → papacolony5w` BACKUP-or-DISQUALIFIED depending on live `papacolony5w` client check, -73 dBm but donor CX present).
2. **Scan (twice) before flipping.** Down-site APs sometimes need 2 GUI scans to appear in noisy environments — over SSH mimic this with an extended-duration scan or a repeat round, and require a steady ssid/signal across rounds before planning the flip. Never declare no-beacon off a single short scan.
3. **Temp-link with zero extra customers offline.** Flip the empty donor first (donor-always-first), island second per the flip pattern; a 0-customer donor keeps the whole operation at 0 interrupted customers — state that explicitly instead of the generic donor-interruption disclosure. Reboot-first each radio; NO reboot after the station apply (assoc forms without one).
4. **Island-side AP check via temp /29 gateway.** Once bridged, put a temp address from the island subnet on the donor/tunnel path and probe the original downed AP's mgmt IP over its wireless side (it may still be associated to its island ST even with its ether down). Classify: alive-online / alive-unassociated / dead (beacon + assoc + SSH + traffic counters decide, in that order).
5. **Reboot-or-ticket.** If the AP is alive: SSH in (bridge/tunnel, or DSTNAT+SRCNAT port-forward on the reachable-side router tagged `temp <site>`) and reboot it — ether ports frequently come back from the radio side (try this even if the near-side port was already bumped and the near router already rebooted). If it is dead: stop flipping, file a repair ticket for the dead radio now (temp link carries the site — repair priority, note island CX count; 0-CX islands may not justify the temp path at all).
6. **Verify then undo-or-keep.** Ether BACK → verify link + OSPF Full + island default via the normal path, then one-sided undo (island snapshot-restore, alias removal, sheet release last). Ether still DOWN → keep the temp path, repair ticket stands, undo only when the permanent repair lands.

### Island RouterOS access: mac-telnet from the near router is THE WAY (verified)

Whenever island RouterOS devices (pppoe/backhauls) are MNDP-visible from a reachable near router, you access them via **SSH → near router → `/tool mac-telnet <far-MAC>` → island router** — exactly as a NOC tech would. A live run proved it: MyMate showed the whole island dark, yet the near pppoe router's neighbor table heard `papa colony backhauls` + `papa colony pppoe` (uptime 1w10h, age ~15s) plus the tower-host router — full read-only diagnosis (ether states, OSPF Full internally, NO default route, 2 PPPoE sessions) with zero writes and zero flips.

- **MAC source:** `/ip neighbor print detail` on the near router (identity, MAC, uptime, age). Fresh age + long uptime = alive island behind a dead routed path. (MNDP alone is not a data path — the mac-telnet login + command output is the proof.)
- **Execution:** `node scripts/mt_mactelnet.mjs --jump <near-ip> --mac <far-mac> --cmd '<read>' [--cmd ...]` — repo-relative, any cwd/harness, exit 0/1/2. Passwords resolve at runtime from the credential store (pool order, outer-then-inner) and are redacted from all output. Read-only by default (mutating verbs refused without `--write --incident-id`).
- **Transport facts (do not re-learn the hard way):** (1) `/tool mac-telnet` over an SSH *exec channel* silently does nothing — piped stdin is ignored (1s empty exit). It REQUIRES a real interactive shell (pty). (2) RouterOS pty line discipline needs `\r` endings — `\n` alone echoes but never executes. (3) After the last inner command, send inner `/quit` and close on the returning outer prompt — never blind-send an outer quit. (4) There is no MCP `mac-telnet` tool and no allowlisted one-shot command for it (interactive by nature) — the helper script IS the interface.
- **Scope:** mac-telnet reaches RouterOS devices only. airOS island radios are reached by IP-SSH once bridged (temp /29 gateway or existing subnet path). Use mac-telnet FIRST for every island-router read (identity, ether states, OSPF, routes, logs, PPPoE counts); port-forwards and temp-IP tunnels are fallback, not default.

### Nearby-AP candidate discovery (verified)

When the operator names no endpoints, find donors in this order:

1. **Geo shortlist:** one `GET /api/sites` pull, filter client-side, haversine from the island site. Plausible for the band (2.4 GHz omni: under ~8 km; 5 GHz sector: check facing/orientation). A donor-to-island pair measured 4.7 km.
2. **Inventory filter:** the cached device index (`logs/mymate-device-index.json`: id/name/mgmt_ip/status/site) by neighbor-site + band/model (`24o` = 2.4 GHz Tik omni, `5n/5s/5e/5w` = 5 GHz sectors). Never re-pull the full ~35 MB `/api/devices`.
3. **Config compat (Oxidized read-only `GET /node/fetch/<ip>`):** wireless `mode` (need ap-bridge on at least the beaconing end), `ssid`, `frequency`, `wireless-protocol` (nstreme-vs-nv2 mismatch is handled by the `any` station recipe — record, don't reject), `wds-mode` (note mismatches for the WDS ladder), `band`/`channel-width`. Cross-band 2.4↔5 GHz never pairs.
4. **Client cost (live, `scripts/mk.sh` read path):** `/interface wireless registration-table print` on each donor candidate — an EMPTY table (an empty donor omni showed zero rows) means a zero-interruption flip and outranks every busy AP. Record client identities, not just counts.
5. **Beacon proof:** donor-side scan for the island ssid; require steady signal/SNR across rounds before planning the flip.
6. **Operator heuristic:** check the 5 GHz APs facing the island first, but a facing pair is eligible ONLY if BOTH the donor AP and the island AP it faces are empty of connections — otherwise the far side cannot be reverted after linking. If every facing 5 GHz pair carries customers, drop to the 2.4 GHz omni (still preferring the empty one). This is why one run went `xray24o → whiskey24o`.

### Operator-manual fallback protocol

When the typed executor is unavailable or a step has no typed tool, the operator performs each change and the agent only reviews sanitized before/after config and read-only verification, one step at a time (worked example with the full seven-step protocol: `references/operator-manual-fallback.md`). Never paste credentials or private keys; a timed-out or ambiguous operation is an unknown result, so inspect live state before retrying or restoring.

### Transport robustness on lossy paths (verified)

A NOC tech works through packet loss; the tooling must too. Hard requirements, all learned the painful way in one night:

1. **Never leak sessions.** Every SSH/SFTP connection is destroyed on ALL exit paths — success, error, and especially timeout. A timed-out command that abandons its connection strands an authenticated dropbear session on the radio; enough of them hit MaxStartups and the radio refuses new SSH while ping and WebUI stay green — indistinguishable from a dead radio until you know. (This bug existed in the first transport build and caused exactly that signature.)
2. **Batch per connection.** Snapshot + edits + re-read ride one SSH session (`execMany`), never one connection per command — dropbear throttles rapid reconnects.
3. **Ride out flaps, don't sprint them.** Connect with backoff spanning minutes (5 attempts, 10s steps), 30s handshake timeout, SSH keepalives. A flap-riding retry that gives up in under a minute will fail exactly when patience would have won.
4. **Loss is normal; reboots are not.** Brief dark windows with 30–50% ping loss happen network-wide all night (monitor-proven on untouched radios) — wait them out. An actual uptime reset is a different animal: power/hardware, hands-on diagnosis, never remote retries.

### MikroTik wireless station recipe (verified live)

A mode+ssid flip alone does NOT associate. The complete station recipe is five fields, set in one `/interface wireless set` command: `mode=station-bridge`, target `ssid`, `frequency=auto`, `wireless-protocol=any` (so it can join an AP on any protocol, e.g. nstreme), and `rate-set=default` (MCS rates must be default, never a configured subset). Snapshot and verify all five; restore all five. Snapshot source is per-field `:put [/interface wireless get <iface> <field>]` — ROS7 terse `print where` omits `rate-set` (an earlier run refused on it), and bare `get` prints nothing over non-interactive SSH (a second refusal). Dynamic ARP entries are NOT proof of delivery: only a `C - complete` flag (or a completed handshake) proves unicast — an incomplete entry means the island never answered. Security profiles are never read or written (open-auth + PPPoE sectors) or handled separately for WPA sectors — never paste keys. station-bridge (not plain station) is the bridged-temp-link mode, operator-confirmed.

### airOS station recipe for secured APs (verified live)

MT sectors run open auth; UBNT plant APs are WPA2-PSK, and an open-profile station scans forever but never completes against them (exact failure: `wpa_state=SCANNING`, target visible in `scan_results`). The station needs the AP's key plus `key_mgmt` flipped `NONE→WPA-PSK`:

1. **Auth check = scan-result flags, never cfg grep.** `wpa_cli scan_results` shows `[WPA2-PSK-CCMP][ESS]` authoritatively. (A cfg grep for `wpapsk` once falsely read OPEN — do not repeat this.)
2. **Key sources, in order:** (a) AP-config primary — read `aaa.1.wpa.psk` from the target AP's own live config over its exact-device binding; (b) `TEMP_SITE_WPA_KEYS` env fallback list. The typed tool (`temp-radio-apply` + `pskSource`) resolves at execute time and audits the SOURCE only.
3. **Value-hiding recon is mandatory:** quote key NAMES + lengths + charset class, never values. Move key material file-to-file (stdin-redirect — airOS dropbear has NO sftp-server, so SFTP `put` fails there), verify with count/non-empty checks (`grep -c '^...psk=.\+'`), shred both ends immediately after apply. Never paste keys into chat, commands, or logs.
4. **Sequence hazard:** a `save` that tears down the link carrying your own SSH kills everything after it — the save is always the LAST remote command on that path (a trailing `reboot` was never delivered for exactly this reason).
5. Full recipe: 4 mode/ssid/supplicant edits + psk + key_mgmt, re-read verify, `rc.softrestart save`, reboot, then `wpa_state=COMPLETED` + traffic counters as proof (not assoc alone).

### Flip-the-temp-link pattern: donor ↔ island AP (verified live)

When the only entry to a dark island is an island AP stuck in master mode, the temp link is built in two phases with the roles REVERSED in between. End state: donor back as AP (customers + island station) — or, for a 0-customer donor, simply left serving the temp link with no restore flip needed (rung 7) — island radio as station. A live run proved the with-customers variant: a donor AP (5 customers) ↔ an island AP:

**Order of operations is fixed — the donor is ALWAYS configured first, the island second.** The island radio is unreachable by definition (that is why you are temp-connecting); no island-side change can be the first step because there is no path to deliver it. The recurring planning failure is proposing "flip the island AP to station toward the donor" while the donor is still an AP — that end-state describes Phase 2, not the opening move. Phase 1 (donor → station → island beacon) is mandatory whenever the island is unreachable: it is the step that CREATES the management path, not an optional optimization. Customer impact is conditional, not automatic: a 0-customer donor means ZERO interruption (state that explicitly); a donor WITH customers drops them here under the standing interruption authorization — disclose the count and prefer after-hours. Never describe a with-customers flip as zero-interruption unless the link-already-up fast path below is confirmed.

0. **Island-beacon gate (before Phase 1).** Phase 1 only works if the island AP is powered and beaconing. Prefer scan-first verification (see **Transit-shadowed outages** below): a donor-side `/interface wireless scan wlan1 duration=10` proving the island ssid/BSSID with steady signal/SNR across rounds beats a flip-and-observe — it passes the gate with zero role change. Flip-and-observe (snapshot the donor, flip to station targeting the island ssid, watch for association) remains the fallback when the donor cannot scan. If association never forms within the agreed window, the island radio is dark (power/hardware), NOT misconfigured — restore the donor to AP **if it had customers** (skip the restore write for a 0-customer donor per rung 7), verify customer recovery where applicable, and fix island power before any retry. Never leave the donor stranded in station mode against a dead island.

1. **Phase 1 — flip to enter.** Donor → station → island AP's beacon (its ssid/PSK from the island radio's backup config or AP-config lookup). A donor WITH customers drops them here — disclose and confirm; after-hours preferred. A 0-customer donor drops nothing. Snapshot + boot_id first; slow pacing (60s+ between sessions); save with 120s settle; NO reboot (assoc forms without one — the reboot was the crash vector on a previous run).
2. **Diagnose over the bridge.** Same-subnet temp alias on the donor (`ifconfig br0:0`, RAM-only) + SSH-forward tunnel through the donor gives full shells on island RADIOS (airOS — mac-telnet cannot reach them). Island ROUTERS come first via mac-telnet from the nearest reachable RouterOS (see **Island RouterOS access** below) — port-forwards and temp-IP tunnels are fallback, not default. Map the island (alive radios? gateway? switch? pppoe?) before building further.
3. **Phase 2 — flip to proper.** Island radio → station → donor ssid (its supplicant profile may already match from a previous temp — verify, don't assume; PSK = donor AP key). Then donor snapshot-restore → AP. Island station associates; donor customers reassociate on their own.
4. **Alias AFTER the final save.** A `save`/softrestart flushes runtime aliases — re-add the temp alias after the last save and re-verify island reachability (a past run went dark post-restore until re-added).
5. **Leftover-ssid rule.** If the about-to-be-replaced supplicant ssid already equals the temp target, mark previous-ssid SUSPECT: record the radio's native AP ssid (`wireless.1.ssid` own-identity, operator-confirmed; e.g. the island AP's supplicant ssid was the donor's name while its native ssid was its own name) alongside. Undo prefers native when suspect. Full snapshot restore sidesteps this entirely — prefer it.
6. **Baseline client IDENTITIES, not counts.** A count-only baseline (5) can't confirm recovery when the end state mixes customers + temp station (4 named + the island AP). Record hostnames/MACs pre-change.
7. **Undo: donor-restore is conditional, island-restore is mandatory.** Undo ALWAYS restores the island radio (snapshot-restore mode + NATIVE ssid, alias removal, bridge verification). The donor is restored to AP **ONLY if the donor had customers at flip time** — a 0-customer donor needs no restore flip (correction: flipping an empty donor back to AP is a needless write with zero customer benefit). Record the donor's pre-flip client table WITH the snapshot so undo knows which branch applies without re-probing.

### AP candidate grading rubric (authoritative)

Every donor/island AP pair is graded on ALL of these before any flip. Customer counts are DISQUALIFIERS, not preferences:

- **Island-side AP MUST have zero customers — hard gate.** The flip is only an option if the island AP we connect to has ZERO associated customers/CPEs. If it carries customers, flipping strands them with no management path to restore (the island is unreachable by definition) — disqualify the pair, do not flip, pick another candidate or stop.
- **Donor-side AP: zero customers outranks everything.** An EMPTY donor means Phase 1 interrupts nobody, needs no after-hours window, and needs no restore flip at undo (rung 7). A donor WITH customers is still eligible under the standing interruption authorization, but it costs a customer drop, an after-hours preference, full identity baselining (rung 6), AND a mandatory restore-at-undo — so it always ranks below any workable empty donor.
- **Facing-pair rule (both ends):** a donor→island pair is eligible ONLY if the customer state of BOTH ends is known AND the island end is zero. Donor-empty + island-empty = ideal (zero-impact both directions). Donor-busy + island-empty = allowed with disclosure. Anything with island-busy = disqualified.
- **Signal ranks within the eligible set only** (-73 dBm beats -77 dBm, low-margin still links; steady across 2 scan rounds required) — never promote a strong-signal pair over a customer-count disqualification.
- **Record per candidate:** client identities (not counts), signal/SNR per round, frequency/band compat, and the resulting grade (PRIMARY / BACKUP / DISQUALIFIED + reason). The grade travels with the plan so the flip step never re-litigates it.

### Link-already-up fast path and Temp OSPF turn-up

Before planning any flip, check whether a previous temp association is still in place (the donor left in `station-bridge` against the island AP). If so the L2 already exists, no customer is interrupted, and the only work left is the temp OSPF `/30`. Evidence recipe (Oxidized archaeology, live MyMate signal, registration table) and the donor-restore note: `references/ospf-turn-up.md`.

**Once L2 is proven, your next action is ALWAYS the OSPF turn-up below, in this order. Do not drift into island-router or upstream forensics first; root-cause work happens after island service is restored.**

1. Reserve the /30: `temp_ospf_sheet.mjs plan-reservation` then `mark-in-use`, or an operator-curated pair with live conflict checks on both routers.
2. NEAR side first, through the typed executor, never raw SSH: `temp-ospf-plan` then `temp-ospf-apply` (`execute:true`, pinned binding). `ospfType` follows the facing interface (a bridge on either side means `broadcast` on BOTH ends).
3. FAR side (island router) by mac-telnet from the near router, the only way to deliver it: `node scripts/mt_mactelnet.mjs --jump <near-ip> --mac <island-MAC> --write --incident-id <id>`. Push the executor-identical address + interface-template commands and read both back in the same session.
4. Verify in this order: OSPF neighbor `Full`; island default route restored via OSPF; outbound ping from the island; iBGP re-formed (make ZERO BGP changes); customer round-trip from the agent host. Only then report.

Required before and during the rungs, detail in `references/ospf-turn-up.md`: L2-unicast proof (an MNDP neighbor is not a data path; forced-L2 ping needs `interface=bridge1`), placement of the `/30` on the interface that actually faces the donor radio path (check `/interface bridge port print` first), the WDS remediation ladder (one rung, observe, decide), leaving working radios alone, cost `1000` (never the mesh `10`), tag everything `TEMP <incident-id> REMOVE-WHEN-REPAIRED`.

### Reboot-first rule (clear transients before the first change)

Radios commonly reboot on the first settings change of a temp-connection sequence and are fine afterward — a surprise mid-sequence reboot is worse than a planned one. So step 0 of every execution is a deliberate reboot of each radio about to be reconfigured (donor AP first): snapshot config, reboot, then verify full recovery (same config, same role, expected clients reassociated, fresh uptime counting) before any change is made. If a radio does not come back clean, stop — it is unfit as a temp endpoint until remediated. (Observed: first-change reboots are routine and single; planned reboot converts them from incident to procedure.)

### Far-side radio access and weak-signal links

Reaching a far radio whose subnet is unreachable from the reachable side (temp IP + tagged DSTNAT/SRCNAT port forwards; proof is a login, not a ping) and working a marginal link near -80 dBm (frequency-select loop and 4-way handshake signatures, the one-rung-at-a-time remediation ladder, failure threshold agreed up front, next-AP fallback): `references/radio-association-playbooks.md`. Never run airView, spectrum or frequency scans on either link radio, never change SSID/BSSID/key/security, and never touch the far-side radio's frequency.

### Apply-time safety rules (safe-mode, stability, firmware, restore)

**Safe-mode discipline (no stranded config):** every change carries an explicit confirmation deadline. The live executor implements this as automatic rollback on verification timeout (`temp-radio-apply` restores the snapshot on a missed confirm; `temp-ospf-apply` rolls back by tag) — not as operator memory. Operator-led steps: use the WebUI safe/test mode (or equivalent timed-apply) wherever available. Agent rule: state the deadline with every proposed change ("if X is not verified by T+N min, revert to <saved state>"), and treat a missed deadline as a failed gate — revert first, diagnose second.

**Target-stability pre-check (no writes to flapping radios):** immediately before any apply, the target must show continuous uptime and a clean ping sample (e.g. 30/30). A radio that rebooted or dropped within the last ~10 minutes is held — writes during a reboot loop risk a mid-write reboot where even rollback cannot connect, leaving manual recovery as the only path. Connect-level failures (refused/handshake/unreachable) happen BEFORE any snapshot or write and change nothing; still, back off and re-verify stability rather than retrying into a flap. (example: four consecutive apply attempts fail-closed on a reboot-looping radio with zero bytes written.)
**Corroborate with the independent monitor:** before an apply, pull the target's MyMate `ping-samples` for the last hours. Loss episodes with NO agent contact (two sector radios each showed several overnight episodes) prove chronic path/power flapping independent of any tooling — if the monitor shows recent loss, hold the apply regardless of one clean local ping. A single clean sample means nothing on a chronically flapping path.

**Firmware-match hard rule:** if the two temp-link radios report different firmware, align BOTH to the site standard (`8.5.12`) — with config backups first — before the first association attempt. Mismatch was ruled out as the cause once (8.5.12 vs 8.7.19, downgrade changed nothing), but it is a free variable that costs one reboot to eliminate; never debug handshake failures across mismatched firmware.

**Restore-recipe rule (literal previous, always):** the donor revert restores LITERALLY the previous value of every changed field — including a wrong one. Frequency tuning (AUTO cleanup, documented-frequency lookup, retunes for cleanliness) is general AP maintenance, not temp-connection logic, and this skill does not do it. Record the literal previous values in the incident log; any retune is a separate maintenance action outside this workflow.

### Gated target workflow (execution requires the dedicated typed executor)

### 1. Establish outage scope and trustworthy inventory

1. Load `mymate`, `wireless-health`, `sonar-api`, `path-analyzer`, and `reprogram-device` as needed. Use `librenms` only as hardware/name corroboration, not site truth.
2. **airOS transport facts (verified):** UBNT airMAX radios run legacy dropbear SSH — modern clients fail with `no matching host key type found` BEFORE any password is tried, which audits misreport as auth failure. Always negotiate `ssh-rsa` host keys explicitly. HTTP Basic does NOT work on airOS 8 (session-only `api/auth` + CSRF flow; unauthenticated `.cgi` returns bare 403). Agent reads and writes to airOS go over SSH, never the WebUI HTTP paths.
2. Read MyMate open outages, links, and individual device records. Use cached device inventory if needed; never repeatedly download the documented ~35 MB full `/api/devices` list. Record timestamps/freshness and whether the monitoring engine is healthy.
3. Resolve each site's router, switch, backhaul AP/ST, sector APs, radio model, management IP, status, links, and customer-device relationships. MyMate links are leads, not proof of live RF connectivity or topology correctness.
4. Mark the boundary between reachable network and offline island. Verify whether a candidate switch is still powered/reachable and whether radios attached to it can operate without the failed router. Do not route through an unknown Layer-2 bridge or create a loop.
5. If MyMate lacks an endpoint, query an approved authoritative inventory by exact supplied IP/name (e.g. LibreNMS exact-IP device lookup, then UISP inventory if available). Do not equate missing MyMate links/outages with device absence. If no authoritative match can be established, inventory is stale, or sources disagree on a fact needed for the action, stop before writing and identify the exact unresolved identity/state.

### 2. Find and rank compatible radio paths

Build candidate edges from (a) links into the offline site/area and (b) valid multi-hop paths from a reachable router to those links. A path is only feasible if every radio edge and every intermediate routing/switch boundary is understood.

Hard compatibility checks before ranking:

- 2.4 GHz connects only to a compatible 2.4 GHz radio; 5 GHz only to compatible 5 GHz radio.
- For named infrastructure radios, resolve actual model, firmware, protocol, channel-width/shift, frequency/regulatory constraints, encryption/key, role, antenna direction/beam, and whether the intended AP mode accepts the station using trusted inventory and read-only device state. Do not infer compatibility from the brand/name alone. A customer station-side Site Survey is not a prerequisite for infrastructure AP/ST role planning.
- Known likely airMAX compatibility to consider: LAP-GPS, PB5AC, and RP5AC are possible 5 GHz airMAX candidates with one another; confirm exact configuration and firmware before use. Different protocol generations/modes may not associate despite matching band.
- MikroTik wireless candidates are usually 2.4OS-to-2.4OS or 5OS-to-5OS; directional models and protocol settings require per-model verification. For infrastructure backhauls, compare only compatible, inventory-verified radios; do not treat customer AP visibility as established without separate evidence.

Rank surviving candidates in this order:

1. **Customer impact:** prefer a feasible lower-impact path when otherwise equivalent, but a customer-bearing or unknown-client donor infrastructure AP remains eligible under the standing temporary-interruption authorization. Report count/freshness when available; do not misstate unknown as zero. Per-customer identity/account lookup is not required for a planned infrastructure-AP role change.
2. **Observed feasibility:** for already named infrastructure radios, use current radio status/configuration and measured link health where available; for candidate paths that depend on an individual customer CPE seeing an alternate AP, a safe station-side survey is a separate prerequisite. Include loss, jitter, latency, capacity, antenna alignment, and channel congestion when available.
3. **Compatibility and recoverability:** known hardware/protocol/config match, reachable management on both sides, clear rollback, stable route back to the core.
4. **Path cost and operational simplicity:** fewest hops and lowest risk while retaining an independent path to the upstream network.

Directional sector naming is only a discovery hint, not proof of an RF path. For an operator-named infrastructure pair, inspect both radios' actual role/configuration and inventory. For an individual customer CPE alternate, verify visibility through a safe, supported survey before recommending it.

Record each candidate and its evidence (endpoint IDs/names, bands/models/roles, link direction, monitored customer count and freshness when available, RF/capacity indicators, and blockers). Prefer a comparably viable lower-impact path, but customer count/account resolution is not an eligibility gate for an explicitly authorized donor infrastructure AP interruption.

### 3. Assess donor infrastructure AP impact (customer CPE account lookup not required)

1. Resolve the exact donor AP and target station from trusted inventory/device reads. If current associations/client count are available, record the count and timestamp to describe the planned interruption; if unavailable, report `unknown`, not zero. These details inform the operator report but do not revoke the standing authorization for a necessary donor-AP interruption.
2. **Unexpected-client check:** if a radio you are about to temp-connect shows a client you did not expect (e.g. an unexpected sector client on a 5w radio), verify what it is before assuming it is a customer. Wireless association counts from LibreNMS/UISP are accurate and current — use them. A previously temp-connected sector, a backhaul station, or another NOC-known radio is handled differently from a paying customer CPE: record the identity and get operator confirmation of its role before any change that would drop it.
3. **Unexpected clients are also candidate leads:** an unexpected radio attached to either endpoint may be the BETTER donor or path — closer site, stronger signal, an existing backhaul relationship. When one appears, evaluate it as an alternative before committing to the original plan: compare signal (an alternate sector pair at -70 beat the planned pair at -78/-81), site proximity, backhaul topology (the alternate site feeds the island site), and client counts on each option. The agent is expected to surface "the unexpected radio may be the better option, and scans support it" unprompted — not just to tolerate the surprise client.
3. This temporary-link authorization covers changing the role/configuration of infrastructure backhaul/sector APs and the resulting temporary disconnection of their associated customers. It does **not** authorize logging into or reconfiguring individual customer CPEs, changing customer/account records, or modifying unrelated radios.
3. Prefer a feasible path with less customer interruption when operationally comparable. Do not delay a necessary outage recovery solely to prove each affected subscriber's account status.
4. Before the executor acts, identify exact donor and target radios, save both before-states, report known/unknown affected-client count, confirm the precise restore sequence, and define a bounded restoration condition. The intended outcome is restored donor AP service plus recovery at the outage site.
5. Do not run disruptive scans on a donor radio as a substitute for its verified configuration/inventory. For a named infrastructure radio pair, use trusted inventory and exact-device state reads to verify role/configuration; do not require customer CPE credentials or a customer-station Site Survey.

### 3b. Separate customer-CPE changes (not covered by this authorization)

If a separate workflow proposes changing an individual customer's CPE, it must resolve the exact device and customer relationship, verify service status, and obtain its own approved workflow. Unknown identity/status is not permission to change a CPE. Never print customer PII unnecessarily.

### 4. If a customer's CPE must move, survey from that CPE first

This step applies only before selecting or changing an individual customer's temporary/secondary CPE association; it is not a prerequisite for the authorized temporary infrastructure AP/ST link.

1. This section applies only if the task separately requires an individual customer CPE to be read or changed. It is not needed for discovery or role changes on the infrastructure donor/target APs, and customer CPE credentials are not required for this temporary infrastructure link.
2. The MCP credential-store format accepts exact-device bindings keyed by `deviceType` + stable `deviceId` + `host`. The separate `run-exact-device-diagnostics` path requires exactly one matching encrypted binding, is currently restricted to allowlisted UBNT airOS reads, attempts once, and has no type-wide fallback. Never expose/log a password. The tool's caller-supplied ID and host are not inventory proof: first validate their mapping against a trusted UISP/inventory source; missing/duplicate/mismatched binding stops without network auth.
3. From the customer station, run its normal **Site Survey / AP selection scan** only after model/firmware-specific non-disruptive behavior is verified. The new exact-device tool reuses existing allowlisted commands and does **not** add a Site Survey command. Survey remains blocked pending a spare/lab test and safe command validation.
4. Do not confuse station AP discovery with airView/spectrum analyzer. Ubiquiti documents airView on airMAX M as terminating wireless connections; MikroTik frequency scans disconnect clients/stations. If the required station scan's effect on association is unknown, do not automate it against an active customer.
5. Match scan results to live candidate APs by BSSID/SSID, band, protocol/security and target site; compare signal (dBm), noise/SNR and link stability. Choose the strongest *compatible and policy-safe* alternative, not merely the loudest incompatible SSID. If no valid AP is visible or quality is too poor/unstable, reject that path.
6. Confirm how this exact customer CPE supports the temporary/secondary SSID/profile and its fallback priority. Inspect current configuration and use model/firmware-supported settings; do not assume all Ubiquiti devices implement backup SSID the same way. Prepare and verify fallback before taking away its primary AP. If a safe preconfigure/verify sequence is unavailable, stop.
7. Make changes serially and one customer at a time. After each, verify association to intended AP, stable signal/rates, management reachability, customer path to Internet, and latency/loss before continuing.

### Per-incident CPE credentials (operator-held, no vault)

There is no long-term CPE password store: the shared MCP credential file is readable by the root-run agent process, so customer secrets must never be added to type-wide `ubnt-airos` credentials or committed. For a separately authorized single-CPE move, CS obtains customer permission, then the operator stages a single-use file outside the repo (`~/.secrets/cpe-<incident>.json`, `0600`) shaped as `{deviceType: ubnt-airos, deviceId, host, credential: {name, username, password}}`, validates it with `node scripts/cpe_binding_validate.mjs <file>` (password is never printed), uses the password locally from the operator workstation for that maintenance window only, verifies via the infrastructure AP registration table, then deletes the staged file. The `run-exact-device-diagnostics` binding format stays reserved for the future isolated helper; until that exists, the agent never handles CPE passwords.

### 5. Select and reserve temporary addressing

1. Decide the topology first: existing routers on both sides normally use a temporary OSPF point-to-point `/30`; a dead/missing router may instead require its verified former `.1/24` subnet to be temporarily hosted on the connected online router. An operator-curated pair such as `10.0.70.113/30` and `10.0.70.114/30` denotes the two interface hosts in `10.0.70.112/30`; it is not evidence the addresses are unallocated. Validate against live interface addresses and routes on both endpoints before configuring. If the operator explicitly elects to skip the Sheet, record `IPAM: operator-curated; Sheet not checked` and do not claim a reservation.
2. For OSPF, identify whether endpoints belong to the same fiber-drain network or different drain networks from authoritative topology. **Cost rule (explicit): if the temp connection is NOT a cross-fiber-drain connection, OSPF cost = `1000`. It is NEVER `10` — even when every existing interface-template on the router uses mesh cost `10`, the temp link must not inherit it.** Only a verified cross-fiber-drain connection uses cost `10000`. No other value is ever permitted, and `temp-ospf-plan` / `temp-ospf-apply` reject anything else. Never copy a router's existing interface-template cost: normal mesh costs (e.g. `10`) would make the temp path preferred and hijack traffic (a past cost-lock review used cost `10`). Copy only timers/priority/auth from existing PTP templates. Omit `cost` to take the `1000` default; pass `10000` only for verified cross-drain. Do not guess the fiber network from site names alone.
3. If using the Sheet, inspect its actual live allocation tab and schema before any reservation. If the operator explicitly skips it and supplies curated addresses, do not require a Sheet connection; record `operator-curated; Sheet not checked` and independently verify address conflicts in live router state. The two approaches must never be conflated.
4. Whether using a Sheet row or curated addresses, independently confirm the private aligned `/30` has no overlap with live router addresses/routes or another active allocation. The Sheet helper cannot infer which subnet is free or calculate live device conflicts.
5. If using the Sheet, invoke the constrained `mark-in-use` command only after schema and allocation review; it updates the existing checkbox and both site fields and verifies/audits readback. If the Sheet is explicitly skipped, keep the curated source in the incident record and require live conflict checks. A reservation or curated pair is an input to the live executor, not permission to bypass its preflight.
6. Declare the OSPF template type explicitly (`ospfType: ptp` or `broadcast` — see the type rule in **Live executor reference**). The facing interface's bridge membership decides it; the plan fails closed without it.

### 5b. Place the /30 where the traffic actually flows (verified)

Two topology traps, both learned on a live test:

1. **OSPF goes on the CLOSEST router to the connected AP** — not default-route to the site PPPoE router. On the example backhauls site the sector plugs into the backhauls router (bridged via VLAN to PPPoE), so the /30 went on `vlan100bridge` there, not bridge1 on PPPoE. Always trace the AP's physical path (radio eth → switch → which router, which VLAN/bridge) before choosing the router.
2. **Verify the AP-facing interface is IN the bridge you configure.** A MikroTik sector that pulls its own IP via PPPoE is typically NOT bridged (its ether stands alone), while UBNT APs basically always sit on the bridge. Putting the /30 on bridge1 when the radio's ether isn't a member fails silently (addresses apply cleanly, zero traffic). Check `/interface bridge port print` for the radio-facing interface FIRST — if absent, configure the /30 on the actual facing interface, never on an assumed bridge.

3. **Check bridge HORIZON on every switch in the path, and remove it.** Horizon on bridge ports kills traffic through the switch in exactly the way a temp link exposes (works locally, dies across) — and it also disables hardware offloading, so it is incorrect config regardless. `/interface bridge port print` must show `none` with HW offload active on all member ports. If a sector switch carries horizon, mac-telnet into that switch and clear it. (Verified clean on the donor pppoe bridge1 + the donor sector switch, all `none` + HW.)

### 5c. Far-side access defaults to mac-telnet (no L3 is the normal case)

A temp connection usually exists precisely because there is no working L3 into the far network — so reaching the far-side router means `/tool mac-telnet <far-router-MAC>` from a device already on (or bridged into) its LAN, most often the near-side router once the temp L2 flows. Record both routers' bridge MACs during discovery; they are the mac-telnet targets. Agent-side mac-telnet transport (interactive session handling) is still pending build — until then far-side router changes stay operator-led, with the agent supplying exact commands and verification steps.

**Verified live, including agent-driven runs:** mac-telnet is the standard NOC-tech path and is ALWAYS reliable once a solid temp wireless link exists — preferred over port-forward gymnastics. Prove L2 adjacency first with `/ip neighbor print detail` on the near router (MNDP shows identity/version/uptime — MNDP revealed the island `pppoe` identity, RB4011, ROS 7.11.2, up 6h = outage start). Operator-confirmed mac-pingable = mac-telnet reachable: if EITHER island radio answers mac-ping from the donor, go straight to mac-telnet instead of burning rounds on radio self-pings. Non-interactive mac-telnet from an agent harness needs an `expect` driver (TTY login): one SSH session to the donor, `/tool mac-telnet <MAC>`, answer Login:/Password: from 600 password files (never argv/env — shell quoting breaks special chars; never one variable for both hops — donor and island pools routinely differ), TERM=dumb, pager branch (`Q quit` → space), `/quit` twice to unwind. A repeated identical deny means wrong pool — try the next pool cred, don't retry blindly. The runner is incident tooling (kept with the incident record), not committed code.

Sonar customer-query syntax is documented in `sonar-api` for workflows that change individual customer CPEs. CPE/account status is not a prerequisite for the authorized infrastructure donor-radio role change.

### 6. Preflight and execution capability (authorized scope; live executor)

The operator has authorized a `Temp connect <site>` workflow to interrupt customers on a selected donor infrastructure AP as necessary and perform only the network changes required to establish, verify, and clean up that temporary recovery. That is the corrected policy scope, not an ad hoc request for a second approval. Technical writes go through the live typed executor (five gates, §Live executor reference); do not attempt live steps through direct SSH, generic MCP diagnostics, or prompt-generated commands. Every apply must confirm:

- An explicit `Temp connect <site>` request, a unique incident ID, exact endpoint identities, and no operator hold/maintenance conflict. The explicit request invokes the standing customer-interruption authorization; do not ask for duplicate approval solely because the selected donor AP has customers.
- The selected radio roles, frequency/protocol/security/model compatibility, management path, and topology are verified from authoritative device state. User-provided device names/IPs are discovery seeds, not proof of current settings.
- Before-state/config snapshots are captured for each exact device; the donor's expected temporary customer outage and best available associated-client count are recorded; original settings and deterministic restore sequence are known.
- `/30` addresses are selected from a configured IPAM reservation OR explicitly marked operator-curated, and verified conflict-free on every router/interface/route involved. Sheet access must not be a mandatory prerequisite when the operator has deliberately supplied curated addresses.
- The required interface, OSPF instance/area/authentication/cost, filters, and routing policy are read and validated. Cost must be `1000` (same-fiber-drain / non-cross-drain temp connection — the explicit default; NEVER `10`) or `10000` (cross-drain) only. Copy timers/auth from existing PTP templates, never the cost.
- Every action has typed parameters and explicit preconditions, bounded timeouts, idempotency/unknown-result handling, readback, audit, and rollback. Never accept free-form RouterOS commands or arbitrary radio config text from model output.

Required future sequence, serialized one device/link at a time:

1. Apply only the reviewed donor/target radio role and association changes needed for temporary access. Read back both ends and verify expected peer identity, stable association, RF quality, and management reachability. The planned donor customer interruption is authorized; unexpected impact to unrelated links, inability to manage either side, or failure to verify requires stopping and restoring the saved state.
2. Use the temporary path to perform the next planned radio transition only after its endpoint, intended AP/ST direction, and restoration ordering are unambiguous. Preserve the working access path until the target link is verified; do not infer radio roles from names.
3. Restore the donor AP to its exact saved original configuration at the planned handoff point. Verify its normal AP is back, expected customer associations recover, and the target backhaul radio is associated as intended. Do not claim customer recovery based solely on a successful config write.
4. If both routers are functioning, apply the selected `/30` host addresses to verified link-facing interfaces and configure only the intended OSPF adjacency/cost. Verify neighbor Full and expected routes without displacing or looping existing paths.
5. If a router is dead, stop and use the separately reviewed router-dead design; do not opportunistically add a temporary `.1/24`, BGP advertisement, NAT, or port forward under the OSPF path.
6. If any write times out, the outcome is unknown: read back actual state before retrying or rolling back. If rollback itself fails, preserve the best-known working path, stop further changes, and escalate to the operator.

This section is the implementation contract for the standing operator authorization; the executor section above is the technical write path. A tool change (new op, new parameter) must be reviewed, tested (`vitest` + `tsc`), and documented here in the same change before live use.

### 7. Verify recovery and stop conditions (required after every apply)

These are required post-change checks after every executor apply. They are not evidence of a current live connection. For each link, verify the actual outcome, not merely command success:

- Wireless registration is established at both ends with expected peer identity; signal/SNR/CCQ/rates are viable and stable, with loss/latency checked over a bounded observation period.
- OSPF case: neighbor reaches Full on the intended point-to-point adjacency; both routers show the reserved `/30`; expected routes appear through the temporary path; no unintended default, duplicate route, loop, or existing path displacement appears.
- Router-dead case, if separately authorized and supported by its executor: intended temporary `.1/24` is reachable, BGP advertisement is present exactly once and accepted by intended peers, MyMate's expected devices recover, and customer traffic reaches the Internet. This path is not implied by a normal OSPF `Temp connect` request.
- Check for unexpected outages, disconnections beyond the planned donor-AP interruption, route changes, high latency/loss, and MyMate/monitoring mismatch after each change. On unexpected impact, stop, restore the last change using saved state, verify the original path, and escalate. The planned donor AP interruption is authorized; verify its customer associations return after restoration.
- A MyMate status color alone is not proof of end-to-end Internet service. Verify device reachability and routed service independently.

Hard stop: incompatible radios; unresolved endpoint identity or AP/ST direction; stale/unavailable topology; bad or unstable RF; unverified address conflict; unknown RouterOS interface/OSPF behavior; failed readback; unexpected impact outside the planned donor-radio/customer interruption; inability to restore donor AP/service; uncertain post-write state; or no rollback. A known/unknown client count on the selected donor AP alone is not a hard stop under the standing operator authorization. A failed gate (kill switch, transport, allowlist, binding, validation) is a stop-work event: report the named gate, do not retry around it.

### 8. Closeout, rollback, and repair follow-up

- Keep the temporary path until the permanent router/backhaul is repaired and independently verified.
- Record incident ID, start/end, sites/devices, path/hops, used `/30` or temporary `/24`, cost, exact config objects, planned donor-AP interruption and best available affected-client count/freshness (or unknown), verification, and cleanup owner in the approved audit log. Record the IPAM row only if a Sheet reservation was actually made.
- Persist the undo inputs verbatim at apply time (reconstruction is not restoration): the flip/apply result's full `previous` object (all five MT fields: mode, ssid, frequency, wireless-protocol, rate-set — never a subset), the AP's previous `wds-mode`, airOS snapshot presence (or its absence), and the ACTUAL comment string on each router's temp objects (tag convention is not guaranteed — in one run the backhauls carried `temp papa-legacy via quebec24o`). An omitted field is a future undo failure; treat a missing `previous` as a blocker before proceeding.
- Any temp write executed outside the MCP typed tools (supervised direct SSH, operator console) must be appended to the incident record the same session with transport noted (`direct-ssh`, exact commands, readback). MCP audit cannot see non-MCP transports — the incident record is the audit trail there. Never leave an executed write recorded only in chat history.
- Attach `REMOVE WHEN REPAIRED` to temporary `.1/24` and BGP advertisement entries, and create a clear follow-up to remove them when repaired. Do not silently forget temporary customer SSIDs, AP/ST changes, routes, firewall/NAT, or reservations.
- On permanent repair, verify normal path first; then remove only this incident's temporary configuration in reverse dependency order and verify normal routing/customers restored. Update/release a Sheet row only if this incident actually reserved one, and only after the temporary `/30` is no longer configured anywhere; an operator-curated address pair has no Sheet row to release.
- If execution becomes uncertain, do not blindly retry or remove state. Read actual state, preserve the working path, and ask the operator.

## Required result format

Return a concise, precise summary:

- **Status:** `connected`, `candidate_only`, or `blocked`
- **Path:** ordered site/radio/router hops
- **Wireless:** peer/association and measured RF/latency/loss
- **Impact:** planned donor-AP customer interruption, best available associated-client count and freshness (or unknown), and whether service recovered after restoration; no customer PII
- **Routing:** OSPF neighbor and cost, or clearly identify the separate router-dead design as blocked pending its own approval/executor
- **IPAM:** Sheet reservation and incident marker, or operator-curated address source with `Sheet not checked` and conflict-check status
- **Cleanup:** temporary objects, repair follow-up, and release condition
- **Blocker/next action:** exact missing evidence or failed gate

Never report `connected` from a plan, attempted command, AP association alone, or MyMate green state.