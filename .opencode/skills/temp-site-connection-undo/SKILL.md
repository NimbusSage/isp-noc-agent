---
name: temp-site-connection-undo
description: Use when an operator asks to undo, remove, or roll back a temporary site connection (e.g. "undo the Papa temp connection"). Restores donor/target radios to saved originals, removes temp OSPF objects by incident tag, and releases the Temp OSPF sheet row — only after the repaired primary path is verified carrying traffic.
---

# Temporary Site Connection — Undo

Reverses exactly one temp connection built under `temp-site-connection`. The trigger is an explicit operator request naming the incident (e.g. `undo the Papa temp connection`, incident `INC-EX-01`). The incident ID is the key to everything: radio snapshots, OSPF tags, and the sheet row are all bound to it.

## Authority and gates (same as forward — no new permissions)

- Undo runs under the same standing `Temp connect <site>` authorization and the same typed-tool gates: `TEMP_SITE_WRITE_ENABLED=true`, the per-transport `verified` flags (`TEMP_SITE_ROUTEROS_TRANSPORT`, `TEMP_SITE_MT_TRANSPORT`, `TEMP_SITE_AIROS_TRANSPORT`), exact-device bindings, and `TEMP_SITE_ALLOWED_HOSTS` covering every router/radio touched (exact IPs or CIDR ranges, e.g. `10.0.0.0/8` — scope says which networks, bindings say which devices). A gate failure stops that step; never work around it with raw SSH or generic diagnostics.
- Rollback tools execute on invocation (no `execute:true` flag except `temp-ap-wds`, which needs `execute:true` like the forward path). Gate note: `temp-ospf-rollback` / `temp-mt-rollback` enforce the full gate set (write flag + transport verified + allowed-hosts + binding); `temp-radio-rollback` (airOS) enforces write flag + binding + snapshot presence only. Read every result's `rolledBack`/`applied` fields; `false` means verify manually, not retry blindly.
- Undo order is the reverse of build: **confirm primary → remove temp OSPF → restore radios → release sheet**. Traffic must leave the temp path before the temp path is dismantled, and the sheet row is released last so IPAM never shows free while objects still exist.

## Step 0: Confirm the repaired primary path FIRST (hard gate)

Never undo into an island. Before touching any temp object:

1. Verify the original outage is repaired: the site's normal upstream adjacency is Full (or the dead router is replaced/reachable) and expected routes/CPE traffic flow over the primary path — not the temp /30.
2. Confirm on both temp routers that the temp adjacency is no longer the best/only path (OSPF neighbor table + route check). If the primary is not verifiably carrying the site, STOP and tell the operator the temp must stay.

## Step 1: Assemble the undo plan (read-only)

From the incident record (`logs/temp-connect-<site>-<date>.md`) plus live state, list every temp object. A complete plan names, per incident:

- **Routers:** each router host + deviceId + RouterOS version + temp interface + temp /30 + the ACTUAL comment string on its temp objects. The incident tag (`TEMP-<INCIDENT>-REMOVE-WHEN-REPAIRED`) is the convention, not a guarantee — operator-created objects may carry a different comment (in the worked example the backhaul-side objects used `temp papa-legacy via quebec24o`), and tag-filtered removal silently misses those. Read the live comment per router during planning; removal targets the recorded string, never an assumed tag.
- **MikroTik radios:** each flipped radio host + deviceId + iface + the saved `previous` five fields (mode, ssid, frequency, wireless-protocol, rate-set). Sources in order: the flip result's `previous` in the incident record, MCP audit `temp-mt-flip` metadata, then live reconstruction with operator confirm for any gap. Never invent a previous value.
- **MikroTik AP WDS:** any AP whose `wds-mode` was changed (audit `temp-ap-wds` metadata `previous`; default plant value is `disabled`).
- **airOS radios:** each changed radio host + deviceId + op type (set-mode / set-frequency). Snapshot lives at `/tmp/noc-temp-site-snapshots/<sha256(incidentId:host)[0:24]>.cfg` on the MCP server host — check it exists BEFORE promising snapshot restore. `/tmp` is volatile across server restarts; a missing snapshot is expected, not an error.
- **Radio TEMP addresses:** any `TEMP-DIAG` / `TEMP-<INCIDENT>` `/ip address` rows left on radios (forward testing sometimes adds them).
- **Sheet:** the reserved /30 network + incident ID for `release`.

Present the plan to the operator in one message and proceed only on explicit confirmation naming the incident.

## Step 2: Remove temp OSPF from every router

Per router, in any order (adjacency loss is intended — primary was verified in Step 0):

```
temp-ospf-rollback { incidentId, host, deviceId, routerOsVersion }
```

Verify per router: `/ip address print where comment="<tag>"` and the OSPF template/interface print show nothing tagged; the temp OSPF neighbor is gone; the primary neighbor(s) remain Full. If `rolledBack:false`, inspect manually — do not continue to radio restore while temp L3 objects are half-removed; a radio restored early while temp OSPF still points at it blackholes nothing (primary carries traffic) but leaves a confusing half-state.

## Step 3: Restore radios

### 3a. MikroTik flipped station → original AP

```
temp-mt-rollback { incidentId, host, deviceId, iface?, mode, ssid, frequency?, protocol?, rateset? }
```

- `mode` + `ssid` are required; pass `frequency`/`protocol`/`rateset` whenever the incident record has them (Papa example: `ap-bridge` / `quebec24o` / `2467` / `nstreme` / plant-standard rate-set — confirm the last from readback, see below).
- Verify: mode + ssid read back correct, the AP is broadcasting its original SSID, and expected clients re-associate (registration table / AP clients, multi-minute window on low-margin links). `rolledBack:true` covers config only — association recovery is a separate check.
- Then, if WDS was touched on the partner AP: `temp-ap-wds { incidentId, host, deviceId, wdsMode:"disabled", execute:true }` (or the recorded previous value) and verify the setting plus a station re-association.

### 3b. airOS radio → snapshot restore (preferred) or re-apply (fallback)

Preferred — snapshot exists:

```
temp-radio-rollback { incidentId, host, deviceId }
```

- Restores the exact saved `/tmp/system.cfg`, reboots the radio, and DELETES the snapshot file. Verify after boot: original `radio.1.mode`, original SSID/frequency, expected associations, management reachability. Snapshot deletion means a second rollback is impossible — get verification right the first time.
- Fallback — snapshot missing (blocked with `no snapshot on file`): do not improvise cfg edits. Restore from the operator-held backup via the `reprogram-device` skill (Oxidized/UISP source), which is the audited path for full-config restore on airOS. For a pure role revert with no other changes, `temp-radio-apply` `set-mode` back to the recorded original role is acceptable, followed by the same verification.

### 3c. Leftover TEMP addresses on radios

Check each touched radio for `/ip address` rows tagged `TEMP-DIAG` or `TEMP-<INCIDENT>`. **No typed executor exists for radio address removal** — this step is operator-led with exact commands (`/ip address remove [find comment="<tag>"]`) and readback. Record the gap; do not route around it.

## Step 4: Release the sheet row (LAST)

Only after Steps 2–3 verify clean on all devices:

```bash
node scripts/temp_ospf_sheet.mjs release --subnet <network/30> --incident-id <id> --confirm-write true
```

`release` requires the same Sheet write gates as `mark-in-use`. It frees both rows AND stamps `date re-opened` = today (owner tab convention; schema: C `Router name`, H `date re-opened`). If the row reads partially-used or mismatched, stop for human review — never force-release a row whose state you cannot explain.

## Step 5: Close out

1. Re-verify the site end-to-end over the primary path (OSPF Full on original adjacencies, CPE-side traffic, MyMate clear if applicable).
2. Append the undo to the incident record: timestamp, per-object results, any manual/operator-led steps, residual gaps.
3. Report: what was removed, what was restored (with client-recovery evidence), what remains (if anything), and that the incident ID's tag/snapshot/sheet row are fully retired. The incident ID must never be reused.
4. Bindings persist by design: exact-device bindings are per deviceId+host, not per incident, so future temps touching the same routers/radios reuse them with zero setup. Never remove a binding during undo — remove only decommissioned devices (`provision_exact_binding.mjs --remove`). The router set is small and stable, so accumulation is slow; radio entries are a few lines of JSON each.

## Failure handling

- Any rollback returning `rolledBack:false` or erroring: stop the sequence, preserve the working primary path, and escalate with the exact failed object + readback. Never retry a failed radio restore more than once without operator direction — a second write to a confused radio risks the management path.
- If a radio becomes unreachable mid-undo, it keeps its last config: temp OSPF already removed means it carries no traffic, so the blast radius is contained to that radio's normal clients. Recover via wired-side access (`reprogram-device`), not repeated wireless writes.
- Snapshot-restore reboot on airOS drops that radio's clients for minutes — expected, and disclosed in the Step 1 plan.

## Worked example: INC-EX-01

Incident `INC-EX-01`, sheet `10.0.70.124/30` rows `<row-a>`/`<row-b>`, tag `TEMP-INC-EX-01-REMOVE-WHEN-REPAIRED`.

1. Primary check: Papa native path repaired (operator confirms; temp adjacency no longer sole path).
2. `temp-ospf-rollback { INC-EX-01, 10.0.71.1, papa pppoe, ros7 }` — removes `.125/30` on ether4 + template.
3. `temp-ospf-rollback { INC-EX-01, 10.0.72.1, quebec switch, ros7 }` — removes `.126/30` on vlan100bridge + template (binding for `quebec switch` provisioned; objects carried the non-standard comment `temp papa-legacy via quebec24o`, recorded live during planning).
4. `temp-mt-rollback { INC-EX-01, 10.0.72.2, quebec24o, mode:"ap-bridge", ssid:"quebec24o", frequency:"2467", protocol:"nstreme", rateset:"<from incident record>" }` — verify AP broadcasting `quebec24o`, clients recover. (`papa24o` was never changed — verify-only, no write. Its `wds-mode` was already restored to `disabled` during testing — verify-only.)
5. Radio TEMP addresses: already removed during testing — verify-only.
6. `release --subnet 10.0.70.124/30 --incident-id INC-EX-01 --confirm-write true`.
7. Close out per Step 5.

## Known gaps (do not work around)

- No typed removal for TEMP-tagged `/ip address` rows on radios — operator-led (Step 3c).
- airOS snapshot is a single volatile `/tmp` file, consumed on use — if the forward test spanned a server restart, expect the `reprogram-device` fallback.
- `temp-mt-rollback` needs explicit previous values; the forward skill must record the flip result's `previous` verbatim or undo degrades to reconstruction.
