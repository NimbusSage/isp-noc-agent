---
name: rename-site
description: Use when an operator asks to rename a site after a tower-host or ownership change (e.g. "rename site omega to sigma", "rename SSIDs at Sigma from omega to sigma"). Covers MikroTik identities, infra radios, backhaul/AP/CPE SSIDs, Netonix names, and MyMate site plus map records.
---

# Rename Site

> **Examples:** addresses and names in this skill belong to a fictional network. This organization's real ones come from `python3 scripts/resources.py list` and `site-pack/`; never reuse an example.

Rename every network object carrying a site's prefix after a tower-host change. Two modes: full `Rename site <OLD> to <NEW>` (identities + radios + SSIDs + Netonix + MyMate) and SSID-only `Rename SSIDs at <SITE> from <OLD> to <NEW>` (SSID fast path).

**Execution model v2: agent-driven.** The agent plans from read-only discovery, confirms the rename table with the operator, then executes end to end via `scripts/airos_rename.py` (airOS: snapshot → anchored single-key set → re-read verify → `softrestart save` → post-verify → snapshot restore on any miss) or `scripts/wave_rename.py` (WaveOS: `ucfg show` snapshot → `ucfg edit` filter → re-read verify → `ucfg commit` → post-verify + `apstats -R` → key restore on any miss), one device at a time with readback between steps. Infra pools (`ubnt-airos`, `ubnt-wave`) for APs/backhauls, separate customer pool (`customer-airos`/`customer-mt`) for CPEs — never mixed. Direct SSH happens only inside those scripts (stored creds, priority order, password never printed); never ad-hoc SSH or model-generated config text.

## Naming standard (normative reference)

Tower-host order is defined by `ticket-context`'s naming table (host surname → abbreviated host → full-name host → enumerated same-name sites; see that skill for the actual pattern). This skill cites that table; it never invents a new convention. Per-type mapping lives in `site-pack/rename-site/naming-convention.md` (untracked; build yours from your own host/tower inventory — schema example: `site-pack/example/naming-convention.example.md`).

Prefix-swap rule: exact case-insensitive **prefix** replacement of `<OLD>` with `<NEW>` across all names/SSIDs. Suffixes (the peer-link suffix: `2`, then the peer name, then `AP` or `ST`; `5n/5s/5e/5w/5x`, `pppoe`, `backhauls`, `sectorswitch`) are preserved verbatim. Anything that is not an exact prefix match is left untouched and reported.

## Phase 0 — Discovery (read-only)

1. Resolve the site per `path-analyzer` Step A cascade: Sonar first-note `Tower:` truth, MyMate `outages`/`devices` cache (never bulk-fetch per ticket — 35 MB guard), LibreNMS corroboration only, Oxidized existence check.
2. Enumerate every object with the OLD prefix: routers (`/system identity print`), backhaul radios (`<siteA>`, the digit `2`, `<siteB>`, then `AP` or `ST`), sector APs, Netonix switches (neighbor print + device_type/vendor), CPEs (AP registration table → MAC, see `references/ap-cpe-migration.md`). For every infra radio, read the LIVE device hostname (`resolv.host.1.name` from `/tmp/system.cfg` via the rename script's snapshot path) — **MyMate display names are corroboration only, never proof of rename** (live rename: MyMate showed display names already carrying the new site name while live hostnames were still `oldA2oldB` variants — assumed-done hostnames were silently skipped).
3. Build the rename table with SEPARATE hostname and SSID columns per device IP (`old hostname → new hostname`, `old SSID → new SSID`). The operator confirms it before any preflight. Unresolvable identity (zero or multiple management IPs) = stop.

SSID-only mode skips identity/MyMate rows: those are re-read as `already-renamed, verified-only`, never rewritten. `Verified-only` means a LIVE readback of the hostname key — not the MyMate record (see Phase 0 step 2).

## Phase 1 — Backhaul preflight (before any SSID change)

Full procedure in `references/backhaul-safety.md`. Per backhaul link, in order:

1. **AP/ST determination (authoritative):** read live `radio.1.mode` / RouterOS wireless `mode` on BOTH ends. Exactly one AP/master/ap-bridge + one ST/managed/station-bridge required. Anything else = hard stop.
2. **/29 gateway check:** compute `/29` from the radio IP (network = IP & `0xFFFFFFF8`, gateway = network+1). Confirm which router owns the gateway via `/ip address print`. Correct owner is the router where the **AP-side** radio is plugged in (trace sector-switch/CRS per `reprogram-device` Step 2c–2d). If the gateway lives on the ST-side router: **move first** — disable the `/29` + associated `/ip/firewall/address/bgp-networks` advertisement on the ST router, add on the AP router, verify OSPF `Full` + gateway ping — before touching any SSID.
3. **Snapshots:** full `/tmp/system.cfg` (airOS) / `ucfg` JSON + `board.json` (Wave) / RouterOS `/export` + identity + wireless section. Checksums recorded; rollback is literal restore, never reconstruction.
4. **Safe mode:** RouterOS safe-mode session where available; airOS staged edits + operator-verified `rc.softrestart save`; every change carries a confirm-by deadline — missed verification reverts first.

## Phase 2 — Backhaul SSID order

1. Rename the **ST side first** (it drops; AP stays up; expect the ST unreachable until step 2 — verify only after the AP flip, see `backhaul-safety` §4).
2. Rename the **AP side identically**. The ST reassociates.
3. Proof is `wpa_state=COMPLETED` + traffic counters on secured plant, not assoc alone. Hostnames/identities only after RF is stable on the new SSID.
4. Never touch far-side frequency, keys, or security during a rename.

## Phase 3 — AP + CPE SSID order

Full procedure in `references/ap-cpe-migration.md`.

1. **CPEs first, AP last.** Stage the new SSID on every CPE (they drop; AP still old — expected), verify staging per CPE, then update the AP SSID identically. CPEs reassociate on their own.
2. CPE credentials come from the separate customer pool (`site-pack/rename-site/cpe-credentials.md`; example: `site-pack/example/cpe-credentials.example.md`) — never the infra pool, never chat/logs.
3. Warn upfront: some CPEs never reconnect until rebooted. Safe-mode revert proves immediate reversal if needed.
4. One AP domain at a time, serially. No stacked changes without observation.

## Phase 4 — Infra + Netonix + MyMate renames

Per-type commands in `references/device-procedures.md`:

- MikroTik `/system/identity/name`, UBNT airOS hostname keys, Wave `ucfg` hostname/SSID, Netonix via its UI per device (operator-led, no central API assumed).
- MyMate: rename **both** the site record (`PATCH /api/sites/{id}`, what geomaps render) **and** the regular submap (`PATCH /api/maps/{id}`) — separate names, both required. Same-value PATCH first as route check. UISP/Sonar/Oxidized/LibreNMS refresh from device readback; report drift rather than writing all masters blindly.

## Phase 5 — Verify, rollback, report

Checklist and report template in `references/verification.md`. Per device: identity, hostname, SSID broadcast, OSPF `Full`, gateway ping, MyMate `up`, CPE reassoc count + non-reconnect list. Rollback table persisted verbatim at apply time.

## Hard stops

| Condition | Action |
|---|---|
| AP/ST roles not exactly one AP + one ST | Stop, report both readbacks |
| /29 gateway on ST-side router and move not yet verified | Stop — move gateway first |
| Rename table not operator-confirmed | No preflight, no writes |
| CPE IP unresolvable (no PPPoE/ARP/DHCP match for MAC) | Skip that CPE, list as unresolved, continue rest |
| Link down at discovery (no assoc baseline before any change) | Stop — file/flag the outage first, rename only a healthy link |
| Link down BETWEEN ST and AP flips during an authorized rename | Expected — proceed PROMPTLY to the AP flip, verify after reassoc (stopping here strands the link) |
| Safe-mode / snapshot missing for target | No write to that target |
| Any readback mismatch after operator apply | Revert that object first, diagnose second |
| Concurrent changes requested | Serialize — one object at a time, readback between each |
