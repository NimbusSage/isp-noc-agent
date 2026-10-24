# Device Procedures (rename-site)

Agent executes via `scripts/airos_rename.py` (snapshot/set/activate/verify/restore subcommands), one device at a time with readback between steps. All reads may use existing MCP paths; airOS writes go only through the script's rename allowlist (`wireless.1.ssid`, `aaa.1.ssid`, `wpasupplicant.profile.1.network.1.ssid`).

## MikroTik RouterOS

- Identity: `/system identity print` (read) → operator: `/system identity set name="<new>"` → read back.
- Wireless SSID (AP/CPE/station): `/interface wireless print detail` (read all five station fields: mode, ssid, frequency, wireless-protocol, rate-set) → operator sets SSID (stations: full five-field recipe per `temp-site-connection` MT recipe — mode=station-bridge, ssid, frequency=auto, wireless-protocol=any, rate-set=default — when the mt radio itself is the renamed endpoint) → read back + registration table.
- **MT SSID rename is a single atomic `set` — safe mode optional.** Verified live on `phi5o` (OmniTIK, nv2, hidden SSID): operator applied `ssid="phi omnitik"` → `"phi5o"` on AP + station CPE directly with no safe mode, zero outage beyond reassociation. For a pure rename keep frequency/protocol/security untouched (minimal diff — do NOT force the five-field station recipe when only the SSID drifts). Safe-mode sessions remain available for riskier multi-field changes.
- **Credential variance per router is normal:** different routers accept different pool entries (verified: `phi5o` AP took `admin-2nd`, `phi pppoe` took `admin-primary`). Try all `mikrotik-ssh` (and `customer-mt`) creds in priority order — first-reject means try-next, not wrong-password.
- **Old MT CPEs need legacy host-key flags:** `ssh-rsa,ssh-dss` via `-o HostKeyAlgorithms=+ssh-rsa,ssh-dss` (verified on a legacy MT CPE — modern handshake fails with `no matching host key type` before auth).
- Safe mode: wrap in a safe-mode session where available so a dropped session reverts automatically.
- Addresses/routes/OSPF/BGP (gateway move only): `/ip address print`, `/ip route print`, `/routing ospf neighbor print`, `/routing/bgp/connection print` reads; operator disables/adds per backhaul-safety §3 with `comment="rename <OLD>2<NEW> ..."` tags.

## UBNT airOS (airMAX)

- SSH needs legacy crypto flags (`ssh-rsa`/`ssh-dss`, dropbear) per `reprogram-device` Step 4.
- Hostname/SSID keys in `/tmp/system.cfg`: device hostname, `wireless.1.ssid` (AP own SSID), `wpasupplicant.profile.1.network.1.ssid` (station supplicant). Edits are anchored single-key sets on the allowlisted keys only; transfer via SCP, never shell-echo (password hashes contain `$`).
- Device hostname is `resolv.host.1.name` (e.g. `siteA2siteB APv3 UNMS` — prefix-swap the site leg, preserve the `APv3/STv3` + `UNMS` suffixes byte-for-byte; values contain spaces, which the executor allows). Rename via the same snapshot → set → activate → verify cycle as SSIDs; no RF impact (SSID untouched) but still serially with readback. Never infer it from the MyMate display name — read it live during discovery.
- Apply: `/usr/etc/rc.d/rc.softrestart save` (operator-verified; no full reboot needed for SSID/hostname). Re-read keys + `iwconfig ESSID` after.
- Rollback: restore exact saved `/tmp/system.cfg` + `softrestart save`.

## UBNT Wave / MLO (WaveOS, verified live)

- Transport: SSH port 22 (modern Buildroot — standard handshake, no legacy flags needed, but the executor keeps them harmlessly). MCP pool `ubnt-wave` (`ucfg-show`, `assoc-stations`, `iwconfig`); writes only via `scripts/wave_rename.py`.
- Keys live in the `ucfg show` JSON: `system.hostname` (display name) and `wireless.ssid` (broadcast SSID — **leading spaces are significant**, preserve byte-for-byte except the site-leg swap, e.g. `" siteA2siteB"` → `" siteB2siteA"`). Role is `wireless.apMode` (`true` = AP, `false` = ST). Keys, never the factory wizard.
- Cycle: snapshot (`ucfg show` → local JSON 0600 + boot tripwire) → `ucfg edit '<jq filter>'` (anchored single-key, JSON-encoded value, single-quotes rejected) → re-read `ucfg show` and compare → `ucfg commit` (persist; 25 s settle) → post-verify + `apstats -R` assoc proof + `iwconfig` ESSID readback. Rollback re-sets the changed keys to snapshot values + commit (no full-JSON restore — unknown keys are never touched).
- **Live-apply warning:** unlike airOS staging, `ucfg edit` hits the RUNNING config immediately — the ST drops (and goes dark) at `set` time. The executor reports ST-DARK and exits 0; the agent must flip the AP side promptly in the same run, never stop between.
- Same ST-dark-between-flips expectation as airOS (ST return path crosses the RF link): verify the ST only after the AP flip + reassociation.
- Snapshot/verify/rollback live in `scripts/wave_rename.py` (`snapshot/set/commit/verify/assoc/restore/selftest`, pool `ubnt-wave`); MCP `run-diagnostics` covers reads only.

## Netonix

- Per-device web UI, operator-led: rename device label only; preserve port comments and PoE settings. No central API assumed. Agent records before/after labels + management IP.
- If a Netonix exposes SNMP sysName, re-read it as corroboration after the UI change.

## MyMate

- Site record: `PATCH /api/sites/{id}` `{"name":"<NEW>"}` + re-read (what geomaps render).
- Regular submap: `PATCH /api/maps/{id}` `{"name":"<operator-confirmed title>"}` + re-read. Both required — renaming one does not rename the other.
- Device names: `PATCH /api/devices/{id}` only for devices in the confirmed rename table.
- Same-value PATCH first as a route check (proven no-op pattern). Auth: session login with `Origin`/`Referer` + `X-XSRF-TOKEN` per `mymate` skill.
