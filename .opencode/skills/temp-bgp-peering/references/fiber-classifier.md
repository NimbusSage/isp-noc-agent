# Fiber Classifier — Accurate Border Detection

Reuses the verified-path walk in `site-pack/topology/fiber-topology.md` (schema example: `site-pack/example/fiber-topology.example.md`): `zeta 10.0.1.1 → 10.0.5.1 Eta Fiber → 10.0.6.1 edge-2216-a`.

## Redundant Fiber via Traceroute from Last_IP Router

```bash
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@$last_ip '/tool traceroute address=8.8.8.8 count=1 use-dns=no'
```

Parse `ADDRESS` col:
- `10.0.20.5 Alpha Fiber 5009` → `Alpha 10.0.20.1` (`fiber-testing-ips.json` `Alpha internal 10.0.20.1`)
- `10.0.21.13 bravo fiber` → `Bravo 10.0.21.33` (`10.0.21.31`)
- `10.0.8.3 Charlie Fiber` → `Charlie 10.0.8.119` (`bgp-rr-map.json`)
- `10.0.5.1 Eta Fiber` → `Eta 10.0.5.1`

Walk reverse per the `fiber-topology.md` pseudocode:
```
hops = traceroute_sorted
public_idx = first 10. not in 10.x (198.51.100.42)
edge_idx = public_idx-1 # 10.0.6.1 or 10.0.6.2 edge-2216
if hops[edge_idx].ip in RDS2216_SUBNET or name~rds|cgnat: fiber_idx = edge_idx-2 else fiber_idx = edge_idx-1
fiber = hops[fiber_idx]
```

## Confirm Border Actually Running

Requirement: accurately know which sites are actually running redundantly on the border network.

After classifying, `ping $RR count=2` from `last_ip` must be `0% loss` (OSPF border running). If `100% loss`, try the other RR (e.g. `sierra 10.0.101.1` pings `Alpha 0%` and `Bravo 0%` — pick lower `avg-rtt`).

Example:
- `victor 10.0.101.9` → `10.0.100.9` → `Alpha` `ping Alpha 0%` `RTT 4ms` vs `Bravo 8ms` → `Alpha`
- `sierra 10.0.101.1` → `10.0.100.1` → `Alpha` (was `10.0.21.13` before, now `Alpha` after reconvergence) — pick live lower RTT.

## BGP RR Map Extension

Copy `site-pack/topology/bgp-rr-map.json` (schema: `site-pack/example/bgp-rr-map.example.json`) and extend with live discoveries:

```json
{
  "2alphabgprr1": {"ip": "10.0.20.1", "network": "Alpha", "verified_on": "10.0.20.1"},
  "2bravobgprr": {"ip": "10.0.21.33", "network": "Bravo", "verified_on": "10.0.21.33"},
  "2deltabgprr": {"ip": "10.0.22.1", "network": "Delta", "verified_on": "10.0.22.1"}
}
```

Store at `site-pack/topology/bgp-rr-map.json` for next run.

## Fallback

If traceroute firewalled (`firewall-filter` check), fallback to `ip route` next-hops + MyMate `GET /api/links` (`mymate:106`) per `path-analyzer/references/path-discovery.md`, but prefer live traceroute — loopback IPs are usually accessible as long as OSPF is running.
