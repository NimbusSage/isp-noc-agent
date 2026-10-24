# Backhaul Safety (rename-site)

Why this exists: renaming a backhaul SSID drops the link. If the `/29` management gateway lives on the wrong (ST-side) router, the drop strands both radios with no management path. This preflight prevents that.

## 1. Determine AP/ST authoritatively

Read live state on BOTH ends over exact-device bindings — never infer from names:

- airOS: `radio.1.mode` (`master` = AP, `managed` = ST) + `wireless.1.ssid` own-identity from `/tmp/system.cfg`.
- Wave: `wireless.apMode` via `ucfg show` (`true` = AP, `false` = ST) + `wireless.ssid` (leading spaces significant).
- MikroTik: `/interface wireless print detail` `mode` (`ap-bridge` vs `station-bridge`).

Require exactly one AP + one ST. Record firmware on both; a mismatch is a warning (align to site standard only as separate maintenance, never mid-rename).

**Baseline before snapshot:** record assoc proof (airOS: AP station table shows the ST; Wave: `apstats -R` STA entry + moving counters) and confirm the ST is manageable from here. Rename only a healthy link — **but** distinguish down-*at-discovery* (stop, file the outage) from down-*between-flips* during an authorized rename (expected ST-dark; proceed promptly to the AP flip — stopping strands the link).

## 2. /29 gateway check

1. Compute from either radio IP: `network = IP & 0xFFFFFFF8`, `gateway = network+1`, mask `/29` (e.g. `10.0.41.3/29` → network `10.0.41.0`, gateway `10.0.41.1`).
2. On each candidate router: `/ip address print` — which router owns the gateway IP, and on which interface? Which router has it **disabled** (classic ST-side leftover)?
3. Trace the AP-side physical path: config gateway → neighbor print → sector/CRS switch port comments (`reprogram-device` Step 2c–2d). The gateway MUST live on the router where the AP radio is plugged in.

## 3. Gateway move (only if on the wrong router)

Before any SSID change, operator-led with agent readback after each line:

1. Snapshot both routers (`/export`, `/ip address print`, `/ip route print`, OSPF instance/area, `/routing/bgp` advertisements, firewall address-lists referencing the `/29`).
2. On the ST-side router: **disable** (not delete — keeps rollback one command) the `/29` address + associated `/ip/firewall/address` entry + BGP network advertisement. Tag with `comment="rename <OLD>2<NEW> temp gateway move"`.
3. On the AP-side router: add the gateway `/29`, re-create firewall/address + BGP advertisement entries with identical parameters.
4. Verify: OSPF neighbor `Full`, gateway pingable from both routers, both radios manageable via the new gateway path.
5. Only then proceed to SSID rename. On rollback, re-enable in reverse.

## 4. SSID rename order (per link)

1. Snapshot both `/tmp/system.cfg` / `ucfg` (checksums).
2. ST side SSID → NEW (ST drops; AP stays up). Stage is re-read verified *before* activate — but do NOT expect a post-activate verify yet (see note below).
3. AP side SSID → NEW (identical string) **promptly**. Read back AP config.
4. Verify the ST only AFTER the AP flip + reassociation (same step as 6).

> **ST unreachable between flips is expected, not failure.** The ST's management return path typically crosses the RF link itself (its /29 gateway is homed on the AP-side router), so after the ST's `softrestart save` it goes `No route to host` until its AP broadcasts the new SSID and it reassociates. Verified live (all three STs dark ~2 min, each returned on its own after its AP flipped — no revert needed). Forward is the only path: a stranded ST cannot be reverted remotely, so the pre-activate staged re-read is the safety net — never activate an ST whose staged keys did not re-read verified.
5. Apply via verified method only: airOS `rc.softrestart save` (no full reboot unless assoc fails), Wave `ucfg backup restore`/`reload` path, MT wireless `set` + confirm.
6. Verify: ST `wpa_state=COMPLETED` (secured) or registration-table entry (open), signal/CCQ at/above baseline, traffic counters moving, management reachable to both ends through the new gateway.
7. Safe-mode discipline: every change states its confirm-by deadline up front; a missed deadline reverts that object before diagnosing.
