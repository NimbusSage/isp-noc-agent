---
name: program-new-device
description: Use when a BRAND-NEW network device needs first-time bring-up — IPs allocated from the org's IP Network sheet (per-network tab, /29 backhaul blocks), static addressing + gateway + BGP advertisement on the feeding MikroTik, SSH enabled and SNMP configured on the device, unique hostnames, and registration in LibreNMS/MyMate (creating site records where missing). Covers new Ubiquiti Wave/Wave-LR PtP link pairs today; hardware families group here by programming method. NOT for factory-reset restores of previously configured devices — that is `reprogram-device` (a reset device has a backup config; a new device has none).
---

# Program New Device (First-Time Bring-Up)

Takes a device that has NO backup config (new install, tech-hung, possibly
named by the field-tech script) and does everything configure-from-scratch
requires: allocate IPs from your org's **IP Network** sheetbook (configured in the site-pack profile) for the
correct network, program the routing/gateway side on the feeding MikroTik,
program the device itself (addressing, SSH, SNMP), then register it in
LibreNMS and MyMate with the correct sites.

**Boundary:** `reprogram-device` restores a saved config to a reset device
(via Oxidized/UISP). This skill invents the config. A new device gets: unused
sheet IPs, verified not-in-use, gateway+advertise on the router, static IP +
SSH + SNMP on the radio, unique names, then monitoring registration.

**Validated live end-to-end on a Wave-LR pair (`Mike` ↔ `Hotel Fiber
Cabinet`)**: sheet allocation `10.0.50.184/29` (`<network-tab>`, three
rows), gateway `.185` + `bgp-networks` on `mike pppoe` ether8, ST
programmed to `.187` via `ucfg reload -c` over SSH, SNMP live both pre/post
flip, LibreNMS auto-detected `airos-af60` + polls up, MyMate site
`Hotel Fiber Cabinet` created from the official Google map + device
polling up. The AP side was lost to a Services-API mistake
mid-session (see hard rules 1 and 4) and is the worked failure record.

## Known-good sequence (Wave pair)

1. **Discover state.** Find the feeding router (MyMate device index /
   LibreNMS). From it: interface comments name the radio port
   (`/interface ethernet monitor <port> once` → link-ok + rate). Find both
   radios by ARP on that port (tech temp config: `192.168.1.1/24` on the
   router, radios at `.20`/`.21`, tech dst-nat for web GUI).
2. **Read the actual device names + roles** from each radio over its working
   path (SSH `ucfg show` or GUI dashboard) — do NOT infer roles from names.
   Field-tech scripts set the **same hostname on BOTH radios** (proven live:
   both were `hotelfibercab2mike`); the SSID carries the link name.
   Roles: GUI dashboard `Role: PtP Main` (AP, wired to the feeding router) /
   `PtP Station` (ST, at the far end). GPS shows on the dashboard for both.
3. **Allocate the /29** (below).
4. **Router first, radios second**: gateway IP on the feeding router's
   interface + `bgp-networks` address-list BEFORE flipping any radio IP —
   the radios' new management path IS this gateway (agent reaches `.x` routed
   through BGP).
5. **Program the radios** (enable SSH via GUI first if off; then SSH/ucfg).
6. **Register**: LibreNMS device-add, MyMate site (if new) + device; UISP +
   Sonar + Tower Directory per bring-up completeness.
7. **Clean temp changes** (dst-nats, temp subnets) once both sides answer
   routed — keep only what the repair/tech path needs.

## IP allocation (org IP Network sheetbook)

Spreadsheet `<SHEET_ID>` (`$TEMP_OSPF_SPREADSHEET_ID`) — SAME spreadsheet
as the Temp OSPF sheet, different tabs. Pick the tab by the site's network
(e.g. Mike → `<network-tab>`). Read it with the temp-ospf auth infra,
per-invocation overrides (inspected range caps at 10,000 rows × 27 cols):

```bash
TEMP_OSPF_TAB="<network-tab>" TEMP_OSPF_READ_ROWS=5000 \
  node scripts/temp_ospf_sheet.mjs inspect > /tmp/net_inspect.json
```

Layout (verified on a network tab): header row has `/30` (col E), `/29`
(col H), `loopbacks` (col K), `/24's` (col M). The `/29` column is
**per-host**, blocks of 8, three used per block: first usable = gateway
(`<router> name`), second = AP, third = ST; one blank separator row between
blocks (the tab's tail had TWO before the next free block —
count blank rows fresh each run). Names in col I in the sheet are the
real device hostnames — verify against the radios, never invent
(e.g. tech names were `mike2hotelfibercab` AP / `hotelfibercab2mike` ST).

**Operator rule (mandatory): before allocating, traceroute the candidate
first IP from the site router** (`/tool traceroute address=<ip> count=1
use-dns=no`). If a device answers (traceroute terminates), the block is in
use: mark it `IN USE` in the sheet and move to the next block. Confirm with
ping (expect 100% loss) + ARP (empty) + no address-list/bgp entries.

Write the 3 rows (gateway/AP/ST with real names) — first run: operator
marks the cells; automate only behind a gated writer later. Never hand-write
the sheet otherwise.

## Router side (feeding MikroTik)

```bash
# creds: resolve at runtime from the MCP encrypted store (mikrotik-ssh pool, in priority order)
/ip address add address=<block>.1/29 interface=<radio-port-iface>
/ip firewall address-list add list=bgp-networks address=<block>.0/29 comment="<site short name>"
```

Conventions proven: addresses carry NO comment (port comment covers it);
address-list comments are bare site names (`hotel fiber`) — **never put
the `;;;` prefix in the value**, the print view adds it (fix applied live).
Readback both. BGP advertisement is automatic from `bgp-networks`.

## Wave/Wave-LR radio programming (method family — same GUI/SSH/ucfg pattern)

### Tech baseline at hang time

Both radios name-set by the tech script (SAME hostname on both — always
re-read and fix colliding names), wave creds = `ubnt-wave` pool `default`
entry (techs script sets that "1st default" password), linked up in bridge
mode, router carries temp `192.168.1.1/24` + a web GUI dst-nat for the AP.
**Techs do NOT enable SSH.**

### GUI driver (scripts/wave_gui.mjs, puppeteer-core + system Chrome)

Credentials via `WAVE_GUI_USER`/`WAVE_GUI_PASS` env (resolve from the
credential store; never print). `WAVE_TOKEN_OUT=/tmp/xx` captures the
session bearer token (40-hex) for the `/api/v1.0/*` API. Session files →
`logs/gui/<session>/` (screenshots + element maps).

```bash
# probe only: login + element map (+ optional --click "Text" / --clickxy x,y)
node scripts/wave_gui.mjs probe --url https://<relay>/ --session NAME
# mutate: needs --execute
node scripts/wave_gui.mjs enable-ssh --url https://<relay>/ --session NAME --execute
```

Gating + proven facts:
- Page uses icon-only nav → **icon coordinates SHIFT between pages. Always
  navigate fresh from login; never chain clicks across page loads.** From a
  fresh dashboard, the settings page is the **4th sidebar icon
  `(28,238)`** (per-icon walk, one page per login).
- SSH control: `input#sshServer\\.enabled` (CSS dot MUST be escaped —
  `#sshServer.enabled` is id+**class** and finds nothing). Custom-styled
  checkbox: click the input, poll for state change, fall back to a
  synthesized `change`/`input` event pair.
- `Save Changes` is DISABLED until dirty — became-enabled IS the
  change-confirmation signal; poll for it (up to 8 s), then click, then
  re-read the checkbox after save.
- Token capture also works per probe run for API steps.

### SSH/ucfg (modern OpenSSH — no legacy crypto flags, unlike airOS)

```bash
SSHPASS='<wave pw>' sshpass -e ssh -p <port> ubnt@<host> 'ucfg show > /tmp/c.json'
SSHPASS='<wave pw>' sshpass -e scp -O -P <port> <local.json> ubnt@<host>:/tmp/new.json
# apply = LAST command on that path; the session DIES on network change (expected):
SSHPASS='<wave pw>' sshpass -e ssh -p <port> ubnt@<host> 'ucfg reload /tmp/new.json -c'
```

Surgical full-document discipline: scp the FULL live `ucfg show` down, change
only the target keys (e.g. `network.interfaces.data.ipv4.{cidr,defaultGateway}`
→ the new /29 + router gateway; or `services.snmp`), scp up, `ucfg reload -c`.
Never send a partial object — reload **replaces** the whole doc.
Verified live: IP flip `.21/24 → 10.0.50.187/29` on the ST with the session
dying at apply, then full routed verify from the agent (ping 0% loss,
`SSH-2.0-dropbear_2025.89` banner, SNMP sysLocation readback).

### SNMP (site-wide convention, operator-set)

`services.snmpAgent` / ucfg key `services.snmp`:
`enabled=true, community=<snmp-community>, location=[<device GPS lat,long>],
contact=<org-contact>`. **The `[lat,long]` brackets are required syntax so
LibreNMS auto-plots the device** (proven: bracket string survives GLPI map).
GPS comes from the radio itself (dashboard Location or the wave GPS) —
waves are GPS-exact per operator. Readback via SNMP from the radio:
`sysName` (hostname), `sysLocation` (bracket string), `sysContact`.

### Services of concern in ucfg (verified)

`services.snmp{enabled,community,location,contact}`, `services.sshServer{enabled,
passwordAuthentication,port}`, `services.webServer`, `services.systemLog`.
GUI Settings page order (rows visible): `HTTPS Server Port`, `SNMP Agent`,
`SSH Server`, `Remote System Log`, `Save Changes`.

## LibreNMS + MyMate registration

- **LibreNMS** `POST $LIBRENMS_URL/api/v0/devices`
  `{"hostname":"<ip>","version":"v2c","community":"<snmp-community>"}` carried the
  full config (auto `os: airos-af60`, sysName from SNMP). Env not in the
  default token export — source the whole `.env` (`set -a; . ./.env; set +a`),
  not a single-key grep.
- **MyMate** login flow (mymate skill): Sanctum + Origin/Referer + CSRF.
  `POST /api/sites {name,latitude,longitude}` works for new in-town sites
  (`kind:"other"`). `POST /api/devices {name,mgmt_ip,poll_method:
  "snmp",monitored:true,site_id,...}`: **omit `device_type`** (only
  `unknown` template is valid; `radio` is rejected); vendor via same-value
  PATCH style. **The `site_id` PATCH 403s** — `can_move_devices:false` on the
  nocagent account — final site assignment is an operator one-click
  (admin), same rule family as MyMate's other privileged writes.
  New-site coordinates: the source of truth = **official
  Google My Map** KMZ (fetch `google.com/maps/d/kml?mid=<mid>&force=lite`,
  it is a ZIP — unzip `doc.kml`, find placemark+coordinates) corroborated
  with the radio's own GPS (agreed within ~6 m). Sonar `network_sites` is
  the other source per operator; Tower Directory sheet rows are inserted
  alphabetically with extreme care (operator rule — deliver exact cells,
  never bulk-write).
- **Ticket follow-through** (tracker-sheet §12) — when the new device break
  causes a filed ticket: link ticket↔site in Sonar, date-stamp MyMate device
  note (`{M/D} {issue}, {contact}, ticket #{n}`), single ticket link per SOP.

## Burned-lesson hard rules (do NOT relearn)

1. **`PUT /api/v1.0/services` is FULL-REPLACE, not patch.** A partial body
   reset `sshServer.enabled`→false AND disabled `webServer` → AP lost ALL
   management (ssh/http/https refused) in one call. Technically the radio
   stayed alive + sat SNMP; recovery uncertainty (unknown whether the
   botched doc was committed to flash) made a field reset the safe path
   rather than a blind reboot. **Rule: GET the full doc → merge locally →
   PUT the whole doc → GET readback → diff EVERY key (unms auto-flag noted)
   → immediately re-banner ssh+https after any services move.**
2. **Env helpers need the caller to source `.env`** (`set -a; . ./.env;
   set +a`): `tracker_sheet.mjs` has no self-loader, and one-key greps
   (`grep LIBRENMS_TOKEN`) miss sibling vars like `LIBRENMS_URL`.
3. **Single cred, then back off.** mk.sh's credential burst (6× attempts)
   tripped RouterOS throttling into sequential AUTH_FAILEDs mid-job —
   retry once with a cooldown, prefer a single stored credential.
4. **Ticket for self-inflicted damage, immediately** (tracker-sheet flow,
   honest notes, `0 cx` count, no invented contact facts). Worked example:
   row `<row>` `Locked Up / Repair Needs Scheduled / LOW / Mike` for the
   bricked AP, filed by the agent that broke it.
5. **Roles are read from the device, never from naming/position.** Both
   radios' identical hostnames + GPS (AP at Mike, ST at Hotel) +
   dashboard `PtP Main/Station` settled it after DOM-order ambiguity.
6. **Cleanup = remove only what you created.** Dead dst-nats removed by
   tag; the tech's own forwarding rule stays for their repair visit.

Related env/tools: `TRACKER_OAUTH_FILE` in `.env` (reuses the
temp-ospf user-OAuth token file — helpers need caller-sourced env),
`puppeteer-core` at repo root (node_modules gitignored),
`scripts/wave_gui.mjs` (reusable GUI driver with token capture).
