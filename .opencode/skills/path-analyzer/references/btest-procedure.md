# btest Procedure — Verified Syntax

All via `mikrotik-ssh` admin creds (`mikrotik-ssh` pool; the btest password is the operator-supplied `BTEST_PASSWORD` environment variable, never read from a file). Same creds for btest target auth — **required** (missing user → `authentication failed`).

## MCP template

- `bandwidth-test` `diagnostic` `readOnly:true`
  Template: `/tool bandwidth-test address={{address}} protocol={{protocol}} duration={{duration}} direction={{direction}} user={{user}} password={{password}}`
  Defaults: `protocol=tcp`, `duration=10`, `direction=both`, `user=admin`, `password=<connecting cred password>` (or `$BTEST_PASSWORD` for a dedicated btest account — operator-supplied env var, not stored in the repo)

Timeout: 60s (`BTEST_TIMEOUT_MS=60000`, `mcp-server/config/server.json:13` override) because btest blocks for duration+connect.

## Baseline (Step E)

From tower router `10.0.1.1` to fiber `10.0.5.1`:

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"bandwidth-test","params":{"address":"<fiber_ip>","duration":5}}'
# status: running duration 3s tx-current 71.3Mbps rx-current 201.1Mbps ...
# status: done tx-10s-avg 64.1Mbps rx-avg 194.2Mbps local-cpu 41% remote 24%
```

Parse `tx-10-second-average` / `rx-10-second-average` + `local-cpu-load` / `remote-cpu-load`. `random-data: no`, `connection-count: 20`.

**Credentials:** RouterOS answers `authentication failed` without a user. Do not pass one: MCP fills `user`/`password` from the encrypted `mikrotik-ssh` pool, tries each entry until the btest server accepts one, and says which (`authenticated with pool credential #n`). Never read or pass a password yourself.

## Iterative (Step F)

Same MCP call with `host` = each hop router IP. Example from `10.0.2.1` to same fiber: `46.3/156.7Mbps local-cpu 36% remote 28%` — slightly lower than tower baseline because one hop closer (variance expected, investigate if >50% drop).

Cap 8 hops, serial only, total wall ~ `hops * (duration+startup+ssh)` ≤ 10 min for 6 hops.

## Direct /30 (Step G)

Get /30 peer first:

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"routing-ospf-neighbor"}'
# 0 D address=10.0.2.2 router-id=10.0.2.2 state Full
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"ip-address"}'
# 10.0.2.1/30 ... interface=ether1 network 10.0.2.0 → peer is 10.0.2.2

node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"bandwidth-test","params":{"address":"<next_hop_ip>","duration":5}}'
# 919.9Mbps tx / 860.9Mbps rx local-cpu 94% remote 83% + warning "results can be limited by cpu, note that traffic generation/termination performance might not be representative of forwarding performance"
```

**CPU-limited caveat:** when `local-cpu-load>90%` or `remote>80%`, generation bound not forwarding — note in comment, don't treat 919Mbps as true link capacity; variance vs full-path baseline will be huge but expected (CPU, not link). For variance math, note caveat explicitly.

Rare `/29/28/24` bridges: `/ip address print` shows `10.0.2.9/29 → gateway 10.0.2.10` (`reprogram-device:125`); use that peer IP same command, duration 5s.

## Parsing

Each `status: running` line carries cumulative averages; take final `status: done testing` `tx-10-second-average` + `rx-10-second-average` as result. If `status: connecting` persists >5s → unreachable/CPU overload → mark hop down.

## Errors

- `authentication failed` → credential mismatch; try the next credential in the `mikrotik-ssh` pool priority order via the MCP encrypted store (`admin-primary` → `admin-2nd` etc.); a dedicated btest account's password comes from the operator-supplied `BTEST_PASSWORD` env var
- `status: connecting` with no `running` → firewall or btest server disabled on fiber router (fallback to ping `mikrotik-ssh ping` and interface stats)
- High CPU during btest (>80%) → record as warning, not bottleneck if full-path hop not affected
