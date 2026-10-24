# Verification (rename-site)

## Snapshot contract (before first change per object)

| Type | Save | Checksum |
|---|---|---|
| airOS radio | full `/tmp/system.cfg` | line count + `grep -E '^netconf\.[0-9]+\.ip=\|^aaa\.1\.ssid=\|^wireless\.1\.ssid=\|^resolv\.host\.1\.name='` |
| Wave radio | `config.json` + `board.json` tar | `ucfg backup validate` |
| MT router/radio | `/export` + `/system identity print` + wireless detail | file size + identity string |
| Router L3 (gateway move) | `/ip address print`, `/ip route print`, OSPF/BGP state, firewall address-lists | saved text |
| MyMate | site + map IDs/names, device IDs/names | API re-read |

Rollback is literal restore of the saved state (including a wrong value — retunes are separate maintenance, never part of a revert).

## Per-object readback checklist

- [ ] Identity/hostname reads back NEW (`/system identity print`, `system.cfg` hostname, `ucfg` hostname, Netonix label, MyMate record).
- [ ] SSID broadcasting NEW (`iwconfig ESSID` / registration table / `scan_results` from an independent radio where available).
- [ ] Backhaul: ST associated (`wpa_state=COMPLETED` or registration entry), signal/CCQ ≥ baseline, traffic counters moving.
- [ ] AP domain: all reachable CPEs reassociated; unresolved/non-reconnect list with per-CPE evidence.
- [ ] L3: OSPF `Full` on moved gateway, gateway pingable, BGP advertisements correct.
- [ ] MyMate: site + map render NEW, devices `up`.

## Report template (operator record)

```
Rename <OLD> → <NEW> (<mode: full|ssid-only>, <date>, operator: <name>)
Table rows: <object> <ip> <old> → <new> <✓/✗> <readback evidence>
Backhaul links: <link> <AP ip> <ST ip> <gateway before> → <after> <assoc proof>
AP domains: <AP> <reassoc N/M> <non-reconnect: list + evidence>
MyMate: site <id> <old> → <new>, map <id> <old> → <new>
Snapshots: <location + checksums>
Rollback: <per-object literal restore command>
Unresolved: <list>
```

One AP domain / one backhaul link per report section; serial readbacks between objects. A timeout after an operator apply is an unknown result: inspect live state before retrying or rolling back.
