---
name: tracker-sheet
description: Use when an operator asks to create or file a NOC ticket in natural language (e.g. "create a ticket for Papa Colony, no power, I left an SMS" or "create a ticket for Alpha 2 Bravo AP, turned link"). Resolves the site/device, verifies against MyMate/LibreNMS/UISP, and appends one validated row to the NOC Ticket Tracker sheet (whose automation creates the real ticket) via the gated scripts/tracker_sheet.mjs helper.
---

# Tracker Sheet — Natural-Language Ticket Filing

> **Examples:** addresses and names in this skill belong to a fictional network. This organization's real ones come from `python3 scripts/resources.py list` and `site-pack/`; never reuse an example.

Turns `create a ticket for <site/device>, <problem>[, <contact note>]` into one
validated row in `NOC Ticket Tracker 2.1`
(spreadsheet id set locally via `TRACKER_SPREADSHEET_ID` from the sheet URL, tab `NOC Ticket Tracker`).
The sheet's own automation creates the real ticket from the row
(`Create Ticket=TRUE` → `Ticket #` backfilled). A wrong row = a wrong real
ticket: never hand-write cells, only the helper appends.

## 1. Parse the request

Extract: **site/device** (tower name, `Alpha 2 Bravo` link pair, or device
handle), **problem** (map to Issue below), **contact facts** (called/texted/SMS/
VM/TH name), **explicit overrides** (status, priority, repair-vs-outage).

`Tower 2 (ST)` is filled only for link tickets (90/97 misaligned rows);
single-site/device tickets leave it blank.

**What creates the real ticket:** the `Create Ticket` column (R, col 17), not the row write itself; see §7 for the ticket vs alert-only intent. The row is the *staging* record: with `Create Ticket=TRUE` the sheet automation produces the `Ticket #` that lands in column F, so appending such a row IS filing a ticket request.

## 2. Resolve site/device (verify, never guess)

1. Tower name must match `Pick List` col I exactly (helper validates; `Alpha`,
   not `alpha tower`). For `Alpha 2 Bravo` pairs both ends must match.
2. Corroborate with MyMate open outages (`mymate` skill): site-wide
   `unreachable` on router + sectors + switch = site down (power). Note
   down-since time and device count as evidence.
3. `Town Name, and problem device` (col D) follows per-type convention:
   - Power: `<Tower> Tower` (`Charlie Tower`, `Delta Tower`).
   - Link: `<SiteA>` + `2` + `<SiteB>` + ` <role> <tech>` — the role side (AP or ST)
     is REQUIRED, even when the MyMate device record omits it
     (`Delta2Echo WaveLR`, `Foxtrot TGM 2 Foxtrot WT ST WaveNano`
     — device record `foxtrotTGM2foxtrotWT Wave Nano` has no ST/AP suffix; the
     operator names the side explicitly and the operator's words win).
   - Device: the device handle (`Golf5s LAPGPS`, `golf5o`), and for
     links also the role suffix (`Hotel2India AP RP5AC`).
4. Match site coordinates (operator rule) — same-name towers exist (`Alpha`
   ≠ `North Alpha`, `Bravo` ≠ `Bravofield`). Sources of truth in order:
   #1 Sonar `network_sites` exact-name search → `geopoint` (`"lat,lng"`):
   `network_sites(search: [{string_fields: [{attribute: "name",
   search_value: "<tower>", match: true}]}]) { entities { id name geopoint
   height_in_meters } }` (`match` is substring — post-filter exact, one hit
   or ask). #2 your org's Tower Directory sheet (id/tab/column map from
   `site-pack/profile.json` `sheets.tower_directory`; template in
   `site-pack/profile.example.json`) via the same user-OAuth token as the tracker helper. After filing, the tracker row auto-fills `Tower1 Coords` /
   `Tower2 Coords` + `Distance (miles)` (cols Z-AA, AI-AK, AP) — read the row
   back and confirm they match #1/#2 within ~0.001° (~100 m) and the distance
   is sane for the link. Mismatch or absurd distance → stop and ask, never
   re-guess the tower. (Worked example: both sources matched within ~0.001°,
   distance sane for the link.)

## 3. Classify — v1 Issue table (from 772 real rows)

| # | Issue | n | Status rule (new ticket) | T2 | Contact flags |
|---|---|---|---|---|---|
| 1 | No Power | 311 | `TH Contacted` if SMS/called/VM; else `TH Needs Contacted` | blank | Called/Texted per facts; `Last Time Called` `h:MM AM/PM` when called |
| 2 | Link Misaligned | 97 | `Repair Needs Scheduled` (realign requested) — see §4 for outage upgrade | pair | none |
| 3 | No Ether | 103 | §4 outage check | blank (device in D) | none unless TH involved |
| 4 | Locked Up | 43 | §4 outage check | blank | Texted if SMS asked TH for power reset |
| 5 | Ether Negotiation: 100mbps | 25 | §4 outage check | blank | none |
| 6 | Ether Port Flapping | 22 | §4 outage check | blank | none |
| — | Grid Outage (variant) | 22 | `Grid Outage` + Issue `Grid Outage` when multi-site grid failure | blank | per facts |

**Priority for known-grid / limited-scope outages:** see §6 — a grid outage
that only drops a couple/few towers behind it is usually `LOW` or `MEDIUM`,
not `HIGH`, because the cause is known and the scope is small.

### 3a. Column standards (columns the agent gets wrong if it thinks like a
network map instead of like dispatch) — operator rules, explicit

1. **`AP IP` (col I): NEVER fill it in.** It is deprecated for agent filing.
   The customer-impact fact lives in the notes instead: notes must state how
   many customers are offline / affected (e.g. `3x cx offline`).
   Leave `ap_ip` empty/omitted on every row.

2. **`Tracking IP` (col J) = the resolution-signal IP — ONLY for a device/site that is actually OFFLINE. NOT the row's coordinating/router mgmt IP.** The Tracking IP drives the outages chat: when the tracked IP pings again, that signals the ticket can be cancelled — that is the field's entire purpose. Therefore:
   - **Set it only** when the device/site is offline right now because of the issue (dead AP/port, down site) and its pinging again would mean the ticket is resolved. Worked example (correct): dead AP `10.0.80.131` → tracking_ip; the backhauls hEX `10.0.80.59` that is up and merely carries the dead port is NOT the tracking IP (router got pings even while the port was dark → hEX as tracking_ip was a filing mistake).
   - **Leave blank for simple repairs and any ticket where the tracked device is not offline** — degraded/dead links (chain imbalance, misaligned, low capacity), 100m negotiation, ether errors, locked-up devices that still pass traffic. Flapping is NOT offline: a flapping device pings again all the time, so a Tracking IP on it just spams the outages chat. Worked mistake (Juliet2Kilo chain-imbalance repair): the ST radio was flapping (down at filing but pinging fine most of the time), yet its IP was filed as Tracking IP — operator correction: it must be left blank. The row's tracking_ip was cleared after (see §11 rule 10).
   - **NEVER set a Tracking IP on misaligned/turned/chain-imbalance link tickets, even when the problem radio is fully offline** (operator rule, explicit): a misaligned radio coming back up briefly with terrible signals does NOT mean the ticket is resolved — which is the entire point of the Tracking IP column. Worked mistake (Lima2Mike turned-60G repair): ST 10.0.81.59 was hard-down when filed and its IP was set as Tracking IP; operator correction — the cell was cleared post-filing and the rule is now absolute for this issue family. Only a truly offline *non-link-quality* problem (e.g. a dead site with no power where ping-back means power restored) qualifies.
   - **Default blank when unsure** whether the device/site is truly offline: a missing Tracking IP costs nothing; a wrong one spams the outage chat.
3. **`Towers Affected` (col K) = how many towers are actually OFFLINE
   because of the ticket.** A repair ticket where a temp connection keeps
   traffic flowing (both ends up, customers served) fills nothing here —
   leave blank. Never write the link's two endpoints as "affected" when
   neither end is offline.
4. **`town_device` role suffix (col D)**: on link tickets, always include
   which side of the device/link the problem device is (`AP` / `ST`),
   matching the operator's naming (`Foxtrot TGM 2 Foxtrot WT ST WaveNano`),
   not the bare device-record form (`foxtrotTGM2foxtrotWT Wave Nano`).

`Priority` defaults `LOW` (~700/772 rows); escalate only when told.
`Tech Who Reached Out to TH` is always `NOC Agent`.
`is cx AP` TRUE only for a customer-AP device (11/772); else FALSE.

## 4. Outage-vs-repair intelligence (operator rule)

For issues 2–6: check MyMate (+LibreNMS corroboration) whether customers are
offline because of this problem.

- Site/device **down** (open MyMate outage on the path, CPEs unreachable,
  whole site dark) → Status `Outage Needs Scheduled`.
- **Up but degraded** (link passes traffic, port negotiates, flapping but
  alive) → Status `Repair Needs Scheduled`.
- Operator saying `outage`/`customers down`/`dispatched`-adjacent forces
  `Outage Needs Scheduled`; saying `repair`/`realign`/`schedule` forces
  `Repair Needs Scheduled`. Explicit words beat the check.

Power tickets keep the TH-contact rule (§3 row 1) regardless.

## 5. After-9pm rule (operator rule)

No SMS/call to tower hosts past 9pm CT. If now ≥ 9pm America/Chicago:
Called=FALSE, Texted=FALSE, no `Last Time Called`. Never invent a contact
note to fill the gap — no `contact TH in AM`, no `before dispatching`
phrasing, unless the operator explicitly said it. Whether a site needs a
call before dispatching lives in its own sheet field (per-site Yes/No) —
the agent does not know it and must not guess it in notes.

## 6. Priority guidance (operator rule)

Default priority is `LOW` (~700/772 real rows). Escalate only when the operator
explicitly says so or the ticket is a live multi-customer outage.

- **Known-grid / known-intermittent source, small scope** (a couple/few towers
  behind a known grid outage, a planned maintenance window, a single-device
  issue with a known cause): default `LOW` unless the operator says otherwise.
  Do NOT auto-escalate to HIGH just because it is an outage.
- **High priority** is for: active multi-customer outages with an unknown or
  broad cause, customer-impact events that are escalating, explicit operator
  urgency, or a live outage that is spreading.

A grid outage that only drops a couple/few towers behind it is a known-cause,
limited-scope event — usually `LOW` or `MEDIUM`, not `HIGH`.

## 7. Ticket vs alert-only intent

`create a ticket ...` selects the ticket path; `create an alert ...` (grid outages, planned or prearranged power events, anything the operator wants alerted without a ticket) selects the alert-only path. The helper's `create_ticket` flag carries that intent and sets the sheet's `Create Ticket` column (R, col 17), which is what the sheet automation reads to create the real Sonar ticket and backfill `Ticket #` (column F): TRUE requests a real ticket, FALSE is alert-only. An omitted `create_ticket` means `true`. `Tech Who Reached Out to TH` is always `NOC Agent`. `send: false` stages a row without sending (`Send Count` blank); use it only when the operator says stage/draft/hold, and confirm which `Create Ticket` value the staged row should carry. End states: §10. Live duplicates: §11 rule 2.

## 8. Notes standards (with verification evidence)

Per-issue wording, worked examples and the port-comment stamp: `references/notes-standards.md` (read it before writing any `notes` value). Non-negotiable:

- Notes carry what the NOC tech verified, terse: problem + down-since time + contact facts the operator actually gave. Never invent contact instructions (`contact TH in AM`, `before dispatching`). No IP lists, device counts, coordinates, analysis prose, timelines or `(Prompt: …)` quoting; the sheet copies the Notes cell into the real ticket description.
- Link tickets: pull live signals + reported capacity first and cite both sides (dBm + Mbps); a turned-link claim needs §9.
- No Ether notes always carry the customer count (`3x cx offline` style, `0 cx` when none), never in the AP IP column.
- No Power: never write `power outage` / `grid outage` unless the TH confirmed it; write `no power observed to our devices`.
- Ether 100mbps: resolve the exact switch port before filing (MyMate interfaces + live SSH verify); add the forced-PoE sentence when `poe-out=forced-on`; stamp the port comment only after the Ticket # is backfilled.

## 9. Turned-vs-foliage (misalignment evidence)

A `turned` claim needs a step-drop, not a drift:

- Pull ~90d signal history (UISP `device-statistics` numeric series first;
  LibreNMS graphs corroborate; airOS live `status` = current only).
- Step-drop ≥~6 dB within ~48h that stays down = turned/misaligned → cite
  date + before/after dBm in notes.
- Slow spring decline + fall recovery = foliage → say so, do NOT claim turned.
- History unavailable → `unknown history`, never guessed.

## 10. Helper contract (only write path)

```bash
node scripts/tracker_sheet.mjs inspect
node scripts/tracker_sheet.mjs plan-append --data '{...}'
node scripts/tracker_sheet.mjs append --data '{...}' --confirm-write true
node scripts/tracker_sheet.mjs wait-for-ticket --row <n> [--interval-ms 60000] [--timeout-ms 600000]
node scripts/tracker_sheet.mjs export-training [--out file]
```

`--data` fields: `status, tower1, tower2, town_device, issue, is_cx_ap,
ap_ip, tracking_ip, towers_affected, called, last_called, texted,
oncall_dispatched, tech, notes, priority, send, create_ticket, ticket`.
`Ticket #` (F) on the ticket path stays blank (automation fills); only
alert-only rows may pass `ticket: "<existing #>"` (high-confidence-duplicate
workflow, §11 rule 2) — validateTicket rejects an explicit Ticket # on the
ticket path. `Create Ticket` (R, col 17) is what creates the real Sonar
ticket — see §7.

Two valid end states today:

- **Ticket path:** `create_ticket: true` (or omitted, since the default is
  `true`), `Create Ticket=TRUE`, `Send Count=1`. The sheet automation creates
  the real ticket from this row.
- **Alert-only path (no ticket):** `create_ticket: false`,
  `Create Ticket=FALSE`, `Send Count=1`. N8N still picks up the row via Send
  Count and can send the Google Chat alert, but the sheet automation must NOT
  create a ticket because `Create Ticket` is FALSE. Typical cases: grid
  outages, planned/prearranged power events, or anything the operator said
  should just alert without a ticket.

`create_ticket` is the agent-facing intent flag; `Create Ticket` is the sheet
column the automation reads. The helper translates `create_ticket` into the
`Create Ticket` column value. The default (omitted `create_ticket`) is
`true` so that the existing "create a ticket" behavior is unchanged.

Placement is top-down like a tech: the helper scans `A2:U1000` for the first
row with no content and writes exactly there with `values.update` (never
`append`, which would bury the row below pre-filled template boilerplate).
Before writing it re-reads the target row — if a tech took it meanwhile, it
fails closed and you re-run `plan-append` for a fresh row. Never overwrite a
row that has anyone else's data.

Column D is `Distance (miles)` — a sheet ARRAYFORMULA that auto-calculates
miles between the two sites (`n/a` for single-site). NEVER write it: the
helper sends each row as two ranges (`A:C` + `E:U`) in one batchUpdate so D
is untouched. Writing any value into D breaks the spill and takes D1 down
with `#REF!` (done once live, cleaned up by hand). Sending runs
through `Send Count` (col U), which N8N watches
for changes: new tickets write `U=1`. The `Send to Auto Tracker` checkbox
(col T) stays FALSE — that is the human path, not the API path.
`send: false` stages the row (`U` blank) without sending. Resends/modifies
increment the existing `U` value; that workflow is future work, not v1.

Gates (append needs ALL): `TRACKER_ACCESS=write` +
`TRACKER_WRITE_ENABLED=true` + `--confirm-write true`. The helper checks the
header signature, validates Status/Issue/Priority/Towers against the live
Pick List, refuses a **second ticket** on live duplicates (same towers +
issue, status ≠ Resolved) but allows alert-only duplicate rows that carry the
existing Ticket # (§11 rule 2), re-reads before write, appends one row, reads
it back (including Ticket #), and audits to
`logs/tracker_sheet_audit.jsonl` (duplicate alert-only rows record
`duplicate_alert_only` + `duplicate_row`/`duplicate_ticket`).
`wait-for-ticket` is read-only (no gates); it refuses rows whose Create
Ticket is not TRUE, polls `F{row}` to success, and fails closed with an audit
entry on timeout. Tests: `node --test
scripts/tracker_sheet.test.mjs` (offline mocked tests — count grows with coverage, see suite).

## 11. Hard safety rules

1. New tickets NEVER `Dispatched` (field-tech actually dispatched, set later by humans) and NEVER `Resolved`. The helper rejects both.
2. **One row per ticket/alert + high-confidence-duplicate workflow (operator, No Power example):** a live duplicate (same `tower1` + `tower2` + exact issue, status ≠ Resolved) must NEVER become a second ticket.
   - The helper's duplicate gate refuses the ticket path (`create_ticket` true/omitted) automatically — never bypass it. The refusal message points to this workflow.
   - When the operator confirms it is the same event: (a) **reuse the EXISTING ticket** — append the new info to that ticket (follow-up comment/notes), don't reopen anything; (b) file a NEW row as **alert-only** (`create_ticket: false`) AND pass `ticket: "<existing #>"` so the existing ticket number lands in the Ticket # column (F) — the Send Count alert still fires so the company sees the site/device state, but the row points at the active ticket; (c) **skip the §12 Sonar-link + MyMate-note steps** for this event — the existing ticket already carries its links/notes and re-adding them duplicates history (worked probe + abort).
   - A live row with a DIFFERENT issue or different towers is NOT a duplicate — the gate never catches it, and slightly-different issues are only called duplicates after operator verification (duplicates must be high confidence, never "a slightly different issue").
3. Dropdowns come from the live Pick List only — never invent a Status/Issue.
4. No contact claims without facts: no `TH Contacted` without SMS/call/VM,
   and no invented follow-ups (`contact TH in AM`, `before dispatching`) —
   contact sentences only repeat what the operator said.
5. After 9pm CT → §5, no exceptions.
6. Machine output for future automation: `{ok, sheet_row, status, tower1,
   tower2, issue, priority, tech, send_count, audit_file}`; failures are
   non-zero exit + `tracker_sheet: <reason>`.
7. Filing sends by default (`Send Count` = 1, which N8N picks up). Only use
   `send: false` when the operator explicitly says stage/draft/hold.
8. Alert-only rows (`Create Ticket=FALSE`) still write `Send Count=1` so the alert path fires — they are real sends, just not ticket requests.
9. `export-training` snapshots labeled rows for the future local-classifier
   follow-up — training itself is out of scope.
10. **Tracking IP (col J) is for OFFLINE devices/sites only, and never for misaligned/turned/chain-imbalance links**
    (operator rules): the Tracking IP is what signals the outages chat that a ticket can be
    cancelled — it must only name a device/site that is currently OFFLINE because of the issue, so it is high-signal
    when it pings again. Simple repairs and any ticket where the device is not offline — degraded links, 100m
    negotiation, ether errors, locked-up-but-passing — leave it BLANK. Flapping is NOT offline: never set a Tracking
    IP on a flapping device; it spams the outages chat. **Misalignment exclusion (absolute):** even when a turned/
    misaligned/chain-imbalance radio is completely down at filing, do NOT set its IP as Tracking IP — a brief return
    with bad signals is not a resolution signal, and the outages chat would fire a false "resolved".
    When unsure the device is truly offline in the *ping-back-means-fixed* sense, omit it (see §3a.2).

## 12. Sonar link + MyMate device notes + MyMate ticket link (standard — every ticket row)

After the row sends and the Sonar `Ticket #` comes back (`wait-for-ticket --row <n>`), finish the job in Sonar and MyMate. Full procedure, exact formats and worked examples: `references/post-filing-links.md`. Gates in short:

1. Link the Sonar ticket to the site (site-wide issue) or to the problem device's InventoryItem (single-device issue); exact normalized name match only, ambiguous = ask. Ticket rows only.
2. MyMate device note on the direct problem device (every down device for site-wide power), idempotent (`GET /api/devices/{id}/notes` first). Format `{M/D/YYYY} {issue-short}, {contact-note}, ticket #{n} -NOC_Agent`; every agent-written note ends with ` -NOC_Agent`.
3. Link the Sonar ticket in MyMate exactly ONCE. Full-link tickets link the ST-side device, never the AP side (unless the AP side is dead/red, then link the AP device and say so in the report).

Alert-only rows skip steps 1 and 3, including duplicate alert-only rows that carry an existing Ticket # (§11 rule 2). Alert-only GRID OUTAGE rows still get the step 2 notes on every down device, in the form `{M/D/YYYY} grid outage, alert only — no ticket, ticket # —` plus the suffix. Other alert-only rows have no standing note rule: ask the operator.

## 13. RSSI evidence chart for misaligned / chain-imbalance / turned link tickets

For every filed row whose Issue is Link Misaligned (turned), Chain Imbalance, Faulty Dish/Radio, Radio Separation or Link Downgrade, post a follow-up comment with AP-side RSSI evidence after `wait-for-ticket` returns the Ticket # (same phase as §12). Data source, renderer rule, script flags, upload/attach mechanics and render gotchas: `references/rssi-evidence-chart.md`. Must not be lost:

- The NocAgent/UISP chart (`python3 scripts/rssi_drop_graph.py <uisp-device-id> ...`) is ALWAYS the deliverable, UISP first for Ubiquiti radios. The LibreNMS native render is only the automatic fallback (`--librenms-fallback`) when UISP has no usable data or the device is not in UISP.
- AP side, unless only the ST exists or has good data. Step-drop of 6 dB or more that stays down = turned; slow decline with recovery = foliage; unavailable history = `unknown history`.
- Fail closed on an unvalidated PNG. Sonar strips inline `data:` images: upload with `POST /api/files` (multipart field `files[0]`), attach to the comment, embed `<img src="files/<id>">`.

