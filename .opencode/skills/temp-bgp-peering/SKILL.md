---
name: temp-bgp-peering
description: Use when setting up emergency temp BGP peering for a fiber network outage (e.g. setup temp bgp peering to devices from the delta network), detecting looping advertised subnets and peering tower routers to a redundant fiber's bgprr via TEMP peers.
---

# Temp BGP Peering — Emergency Fiber Failover

Replicates the manual `Echo 10.0.50.1` fix as code: `traceroute <advertised_subnet>` that should be via BGP but isn't → loops `A-B-A-B` at end → last IP before bounce = tower router that needs `TEMP` peering to the redundant border network's `bgprr`. Handles the `hop-not-peered → downstream black-hole` case seen with `Foxtrot 10.0.60.0/24` looping `10.0.100.2 tango (10.0.101.2) ↔ 10.0.100.1 sierra (10.0.101.1)`; fixing `sierra 10.0.101.1` restored `ping Alpha→10.0.60.1 0%`.

Works for **ANY** fiber network via `traceroute` from loopback (OSPF must be running over network borders — loopbacks reachable). `Delta 10.0.22.1`, `Alpha 10.0.20.1`, `Bravo 10.0.21.33`, `Charlie 10.0.8.1` etc. per `fiber-testing-ips.json` and `bgp-rr-map.json`.

**Topology path priority:** the topology data (`bgp-rr-map.json`, `fiber-map.json`, `fiber-testing-ips.json`) lives in `site-pack/topology/` (untracked, per-org) — read it from there. Fictional schema examples ship in `site-pack/example/` (`bgp-rr-map.example.json`, `fiber-map.example.json`, `fiber-testing-ips.example.json`, `temp-bgp-config.example.json`): copy one to `site-pack/topology/` (dropping `.example`) and fill in your network. **If a file is absent, do not guess:** ask the operator for the RR list, or fall back to live discovery from the Oxidized/MyMate sources in Step A. New RR verifications are added to `site-pack/topology/bgp-rr-map.json`.

## When to Run

- Trigger phrases: `setup temp bgp peering to devices from the <network> network`, `failover delta`, `emergency bgp`, `traceroute 10.0.60.1 loops` — any request to bring BGP networks back online via redundant fiber.
- MyMate shows `10.0.200` loopbacks (e.g. `25`) still down but `pppoe 10.0.200.x 0` after initial peering — indicates downstream radio subnets still black-holed.
- Do **not** run if `Delta Fiber 10.0.22.1` is reachable (fiber not down) — use `path-analyzer` for slow-tower triage instead.

## Step A — Discover Affected Loopbacks (filter disabled/not-used)

1. **Primary: Oxidized** `GET http://10.0.0.6:8888/node/fetch/<rr_ip>` (`reprogram-device:46`) parse `/routing/bgp/connection` `remote.address` — e.g. `Delta RR 10.0.22.1` → `215` `10.0.200.x` (`delta_peers.json`). **Skip** `disabled=yes` (`X`) entries — old config not connected/not used contributed to incorrect devices.
2. **Live filter:** `ssh admin@<RR> '/routing/bgp/connection print where disabled=no'` + `session print where established` (`E`) to know which are actually used. Copy only `disabled=no` template.
3. **Fallback:** MyMate `GET /api/outages?state=open` `10.0.200` (`mymate:60`) when Oxidized stale, but prefer Oxidized for loopback list.

Names without `pppoe` (e.g. `2theta 10.0.200.40`, `2lambda 10.0.200.41`, `2epsilon 10.0.200.42`) are still `10.0.200.x` loopbacks — **include all `10.0.200.x`**, not just `pppoe|backhaul` (a missed loopback caused `Foxtrot` to stay down).

## Step B — Enumerate Advertised Subnets That Are Not Routed

For each loopback, fetch `ip firewall address-list print where list=bgp-networks` → e.g. `Foxtrot 10.0.60.0/24` (`10.0.60.0/24:3`). For each subnet, `ping` from redundant `RR` (`Alpha 10.0.20.1:<RR_PW_ALPHA>`, `Bravo 10.0.21.33:<RR_PW_BRAVO>`) `count=2` — if `100% loss` or `TTL exceeded`, it needs peering.

## Step C — Loop Detection (Echo method)

`ssh admin@<RR> '/tool traceroute address=<subnet_ip> count=1 use-dns=no'` (or `ssh admin@<tower> '/tool traceroute address=8.8.8.8'` for fiber classifier). Parse `ADDRESS` col.

**Loop signature:** end of trace bounces `... X,Y,X,Y` or `X,Y,X,Y,X` — last 4-6 hops repeat two IPs. `last_ip = hops[-3]` (last before bounce) — e.g. `10.0.60.1` trace: `... 10.0.100.1 → 10.0.100.2 → 10.0.100.1 → 10.0.100.2 → 10.0.100.1` → `last_ip=10.0.100.1` → `sierra pppoe (10.0.101.1)` (via `ip address print` `10.0.101.1/32 loopback` on `10.0.100.1`).

See `references/loop-detection.md` for pseudocode and the `10.0.60.1` example.

## Step D — Redundant Fiber Classifier (accurate border)

From `last_ip` router, `traceroute 8.8.8.8 count=1` → walk per `site-pack/topology/fiber-topology.md`: last `10.` before `198.51.100.x` is edge (`10.0.6.1 edge-2216-a`, `10.0.6.2 edge-2216-b`, etc.), fiber is one before edge. Check `10.0.20.5` ⇒ `Alpha 10.0.20.1`, `10.0.21.13` ⇒ `Bravo 10.0.21.33`, else `ping` both RRs pick `0% loss` lower `avg-rtt`.

Confirm `RR` is actually running redundantly: `ping <RR> 0%` from `last_ip` (OSPF border running) — you must accurately know which sites are actually running redundantly.

See `references/fiber-classifier.md` and `site-pack/topology/bgp-rr-map.json` (schema: `site-pack/example/bgp-rr-map.example.json`; e.g. `2golfbgprr 10.0.23.1`, plus `Alpha 10.0.20.1`, `Bravo 10.0.21.33`).

## Step E — Live Template Copy (instance-aware, secure)

Credentials come from the MCP encrypted store (`mikrotik-ssh` pool, priority order; passphrase `NOC_MCP_PASSPHRASE`), resolved at runtime by the scripts (`scripts/credstore.py`) and passed to ssh with `sshpass -e` (password in the `SSHPASS` environment variable), never hard-coded (see `AGENTS.md` credential rules). RR passwords and peer prefixes for this workflow come from `site-pack/topology/temp-bgp-config.json` (schema: `site-pack/example/temp-bgp-config.example.json`).

- **RR side:** `ssh admin@$RR '/routing/bgp/connection print detail where remote.address=10.0.200.30'` (existing `2xray`) → copy `remote.as=<REMOTE_AS> templates=default multihop=yes output.network=bgp-networks` (+ `output.filter-chain` if present) `local.address=<RR> local.role=ibgp-rr`
- **Tower side:** `ssh admin@$tower '/routing/bgp/connection print detail where name~delta'` → if `instance=bgp-instance-1` present (e.g. `Whiskey 10.0.200.31` on RouterOS `7.22.3`), include `instance=bgp-instance-1` else omit (RouterOS `missing instance` error). Use `local.address=<loopback> local.role=ibgp`

See `references/peering-template.md` for the two-password (second-cred vs primary) RR handling.

## Step F — Dual-Side TEMP Add

```bash
# PW_RR / PW_TOWER: resolved at runtime from the MCP encrypted store (mikrotik-ssh pool) or the temp-bgp-config rr_credentials; never typed into a file
# REMOTE_AS: your iBGP AS number from site-pack/topology/temp-bgp-config.json (or the existing peer's remote.as)
# RR
SSHPASS="$PW_RR" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@$RR \
  "/routing/bgp/connection add name=\"TEMP $orig\" remote.address=$loopback remote.as=$REMOTE_AS local.address=$RR local.role=ibgp-rr routing-table=main templates=default multihop=yes output.network=bgp-networks"
# Tower (handle instance)
SSHPASS="$PW_TOWER" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@$tower \
  "/routing/bgp/connection add ${INST:+instance=$INST }name=\"TEMP 2alphabgprr\" remote.address=$RR remote.as=$REMOTE_AS local.address=$loopback local.role=ibgp routing-table=main templates=default multihop=yes output.network=bgp-networks"
```

Naming `TEMP` prefix for idempotent `find where name~TEMP` and disable-only rollback.

## Step G — Verify

- `ssh admin@$RR '/routing/bgp/session print where remote.address~"<loopback>"'` → `E` + `prefix-count>0` (e.g. `Foxtrot 24`)
- `ssh admin@$RR '/ip route print where dst-address=<subnet>'` → `DAb <subnet> via <loopback> immediate-gw 10.0.100.1` + `distance 200`
- `ssh admin@$RR '/ping address=<subnet_ip> count=3'` → `0% loss` (was `TTL exceeded` at `10.0.100.1`)
- `traceroute <subnet_ip>` no longer loops (`9 hops 10.0.20.1→...→10.0.60.1`)

If still loops, re-traceroute same subnet → next `last_ip` downstream, repeat Step F (fixes `hop-not-peered → downstream black-hole`).

## Step H — Batch + Logging

- Deduplicate `last_ip` set, serial only, 10s spacing, cap `BATCH_TIMEOUT_MS=2700000`, log `logs/temp_bgp_peering_<network>_<ts>.json` `success 100 fail 25` + `MyMate` `10.0.200 164→25`
- Script: `scripts/temp_bgp_peering.py` (Oxidized → loop → peer) + `scripts/temp_bgp_rollback.sh` (disable only `TEMP`)

## References

- `references/loop-detection.md` — Echo-method walk (`10.0.60.1` example)
- `references/fiber-classifier.md` — `10.0.20.5` vs `10.0.21.13`
- `references/peering-template.md` — `instance` + second-password handling
- `site-pack/topology/bgp-rr-map.json` — RR name → IP map (schema: `site-pack/example/bgp-rr-map.example.json`)
