# Path Discovery — Traceroute and Fallbacks

## Primary: /tool traceroute

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"tool-traceroute","params":{"address":"8.8.8.8","count":1}}'
```

- `count=1` is enough (verified: columns ADDRESS, LOSS, SENT, LAST/AVG/BEST/WORST). Use `count=3` if loss seen.
- `use-dns=no` avoids DNS stall.
- Dual `8.8.8.8` + `1.1.1.1` to catch different DIA paths.

Output parsing: extract ordered `ADDRESS` column non-empty rows. `10.` = internal, `198.51.100.` = public edge, `100.64.` = CGNAT, `192.178.` etc = google.

## Fallbacks when traceroute blocked

Check if firewalled: `hide sensitive`? No — use:

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"firewall-filter"}'
# look for rules that drop traceroute / ICMP time-exceeded
```

If blocked or returns empty hops beyond 1:

### Fallback 1: ip route next-hops

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"ip-route-active"}'
# gateway per prefix, recursive flag: build the next-hop chain; for dst-address 0.0.0.0/0 take
# gateway / immediate-gw as the first hop, then run the same call on that gateway and repeat
```

Iterative walk: tower → gateway → traceroute from gateway → repeat until a fiber router / edge `10.` hop (e.g. `10.0.5.1` / `10.0.6.1`) is reached.

### Fallback 2: MyMate links

- `node scripts/mymate_query.mjs links <device-id>` (`a_device_id/b_device_id` + `bw_*`, one call per device) — graph walk from tower device_id toward core.
- Requires cached `/api/devices` to map IPs → device_ids (see Step A). Inaccurate for separate fiber networks per user note — use only when traceroute unavailable.

### Fallback 3: OSPF neighbor walk

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"routing-ospf-neighbor"}'
# D address=10.0.2.2 router-id=... state Full
```

Each OSPF neighbor is directly connected; walk similarly but less informative for routed path beyond area.

## Timeout & sequencing

Traceroute budget 15s per address. Total path discovery ≤30s. If both traceroutes fail → mark `path_discovery_failed` and return `needs_human` with `Analysis: ✗ traceroute failed, path unknown`.
