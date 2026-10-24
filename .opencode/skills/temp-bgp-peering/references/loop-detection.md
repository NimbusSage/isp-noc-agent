# Loop Detection — Echo Method

## Trigger
`traceroute <advertised_subnet>` that should be via BGP but `ping` from `RR` shows `100% loss` or `TTL exceeded`. Example `Echo 10.0.50.1` and `Foxtrot 10.0.60.1` (`10.0.60.0/24` via `Foxtrot pppoe 10.0.60.254`).

## Example Loop (Foxtrot)

```
Alpha 10.0.20.1 → 10.0.60.1
1 10.0.20.2
2 10.0.20.6
3 10.0.20.10
4 10.0.20.14
5 10.0.99.5 uniform pppoe
6 10.0.100.2 tango pppoe (10.0.101.2)
7 10.0.100.1 sierra pppoe (10.0.101.1) ← last before bounce
8 10.0.100.2  ← bounce back
9 10.0.100.1
10 10.0.100.2
11 10.0.100.1
```

Before fix `sierra 10.0.101.1` had no `DAb 10.0.60.0/24` (only `DAo 10.0.60.254/32 via 10.0.99.5`), so it forwarded via OSPF to `10.0.100.2`, which had `DAb 10.0.60.0/24 via 10.0.100.1` via `10.0.100.1` → loop.

After peering `sierra 10.0.101.1` to `Alpha 10.0.20.1` (`TEMP 2sierra` + `TEMP 2alphabgprr`), `sierra` got `DAb 10.0.60.0/24 via 10.0.99.5` and traceroute became `... 10.0.99.5 → 10.0.100.1 → 10.0.60.1` `0% loss`.

## Pseudocode

```python
def detect_loop(traceroute_ips):
    # traceroute_ips = ordered list from /tool traceroute ADDRESS col
    if len(traceroute_ips) < 4:
        return None
    # check last 6 hops for A-B-A-B pattern
    tail = traceroute_ips[-6:]
    # count occurrences of last two distinct IPs
    if tail[-1] == tail[-3] == tail[-5] and tail[-2] == tail[-4]:
        # e.g. [A,B,A,B] or [A,B,A,B,A]
        last_ip = tail[-3]  # the one before bounce, e.g. 10.0.100.1
        return last_ip
    # also check for simple 2-IP bounce at end
    if len(traceroute_ips) >= 4 and traceroute_ips[-1] == traceroute_ips[-3] and traceroute_ips[-2] == traceroute_ips[-4]:
        return traceroute_ips[-3]
    return None

def last_ip_before_bounce(traceroute_text):
    ips = parse_traceroute(traceroute_text)  # ordered 10. hops
    loop_ip = detect_loop(ips)
    if loop_ip:
        # resolve loop_ip to loopback via /ip address print or /system identity
        # e.g. 10.0.100.1 → 10.0.101.1 via ssh admin@10.0.100.1 '/ip address print where interface=loopback'
        return resolve_loopback(loop_ip)
    # no loop but still unreachable → use last hop before timeout
    # e.g. traceroute ends with 0% loss but no public hop → last 10. hop is fiber edge, not loop
    return None
```

## Downstream Iteration

After peering `last_ip`, re-traceroute same `<subnet_ip>` from same `RR`:
- If still `TTL exceeded` at new last IP, repeat → next downstream tower (e.g. after fixing `sierra`, `Foxtrot 10.0.60.1` still might loop via `10.0.99.5` if `uniform` not peered).
- Stop when `ping 0%` and `traceroute` reaches subnet without bounce.

## Wire for Echo 10.0.50.1

Same method: `traceroute 10.0.50.1` from `Delta` redundant `RR` (likely `Alpha 10.0.20.1` or `Bravo 10.0.21.33` depending on site's `traceroute 8.8.8.8` fiber), find loop, peer the `10.0.150.x` gateway router.

## MyMate Corroboration

`GET /api/outages?state=open` `10.0.200 164→25` after fixing `sierra` shows `Foxtrot 14→0`. Use as batch progress metric.
