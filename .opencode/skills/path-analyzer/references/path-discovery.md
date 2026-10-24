# Path Discovery — Traceroute and Fallbacks

## Primary: /tool traceroute

```bash
export PW="${BTEST_PASSWORD:?set BTEST_PASSWORD in the environment (operator-supplied, see .env.example); never read credentials.json}"
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/tool traceroute address=8.8.8.8 count=1 use-dns=no'
```

- `count=1` is enough (verified: columns ADDRESS, LOSS, SENT, LAST/AVG/BEST/WORST). Use `count=3` if loss seen.
- `use-dns=no` avoids DNS stall.
- Dual `8.8.8.8` + `1.1.1.1` to catch different DIA paths.

Output parsing: extract ordered `ADDRESS` column non-empty rows. `10.` = internal, `198.51.100.` = public edge, `100.64.` = CGNAT, `192.178.` etc = google.

## Fallbacks when traceroute blocked

Check if firewalled: `hide sensitive`? No — use:

```bash
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/ip firewall filter print' 2>&1 | grep -i traceroute
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/ip firewall filter print stats'
```

If blocked or returns empty hops beyond 1:

### Fallback 1: ip route next-hops

```bash
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/ip route print where active and dst-address!=0.0.0.0/0'
# shows gateway per prefix, recursive flag; build next-hop chain
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/ip route print detail where dst-address=0.0.0.0/0 and active'
# take gateway / immediate-gw for first hop, then SSH to that gateway and repeat
```

Iterative walk: tower → gateway → traceroute from gateway → repeat until a fiber router / edge `10.` hop (e.g. `10.0.5.1` / `10.0.6.1`) is reached.

### Fallback 2: MyMate links

- `GET /api/links` (`mymate:106`) `a_device_id/b_device_id` + `bw_*` — graph walk from tower device_id toward core.
- Requires cached `/api/devices` to map IPs → device_ids (see Step A). Inaccurate for separate fiber networks per user note — use only when traceroute unavailable.

### Fallback 3: OSPF neighbor walk

```bash
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/routing ospf neighbor print'
# D address=10.0.2.2 router-id=... state Full
```

Each OSPF neighbor is directly connected; walk similarly but less informative for routed path beyond area.

## Timeout & sequencing

Traceroute budget 15s per address. Total path discovery ≤30s. If both traceroutes fail → mark `path_discovery_failed` and return `needs_human` with `Analysis: ✗ traceroute failed, path unknown`.
