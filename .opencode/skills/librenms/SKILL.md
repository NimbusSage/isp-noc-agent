---
name: librenms
description: Use when corroborating a tower or fiber router device via LibreNMS inventory — fetch sysName, hardware, OS, location/geolocation, or port list for a known IP — as a secondary source (not site truth) alongside MyMate and Sonar.
---

# LibreNMS — Device Inventory Corroboration

> **Examples:** addresses and names in this skill belong to a fictional network. This organization's real ones come from `python3 scripts/resources.py list` and `site-pack/`; never reuse an example.

LibreNMS API at `<LIBRENMS_URL>/api/v0` (`python3 scripts/resources.py get librenms` prints the address). Docs: https://docs.librenms.org/API/

- **Auth:** `X-Auth-Token: $LIBRENMS_TOKEN` (from `.env`: `LIBRENMS_TOKEN=<VALUE FROM .env>` — never hardcode, load it with `set -a; . <(grep '^LIBRENMS_TOKEN=' .env); set +a` or take it from the environment). No MCP encryption per plan (unlike `mikrotik-ssh` creds).
- **Devices named sysName:** e.g., `zeta pppoe`, `Eta Fiber`, `edge-2216-a`, `mikrotik theta`. Grouped into geolocations — **not** source of truth for tower sites. Sonar `Tower:` field is truth (`ticket-context:118`).
- **Timeout:** 10s per request; best-effort. Failure → degrade to MyMate+Sonar.

## Endpoints (verified)

```bash
set -a; . <(grep -E '^LIBRENMS' .env); set +a   # loads only these variables; the token stays off the command line
# list (paginated; first call may stream 16k devices — use filtered or id)
printf 'header = "X-Auth-Token: %s"\n' "$LIBRENMS_TOKEN" | curl -s -K - "$LIBRENMS_URL/api/v0/devices" | python3 -m json.tool

# single device by IP/hostname (sysName or mgmt_ip works)
printf 'header = "X-Auth-Token: %s"\n' "$LIBRENMS_TOKEN" | curl -s -K - "$LIBRENMS_URL/api/v0/devices/<device_ip>" | python3 -m json.tool
# → {"status":"ok","devices":[{"device_id":101,"hostname":"10.0.1.1","sysName":"zeta backhauls","hardware":"RB4011iGS+","os":"routeros","status":true,"location":null}]}

# ports for error counters
printf 'header = "X-Auth-Token: %s"\n' "$LIBRENMS_TOKEN" | curl -s -K - "$LIBRENMS_URL/api/v0/devices/102/ports" | python3 -m json.tool
# ports: [{ifName:"ether1-bh-failover"}, ...]

# locations (geolocations) — verify grouping
printf 'header = "X-Auth-Token: %s"\n' "$LIBRENMS_TOKEN" | curl -s -K - "$LIBRENMS_URL/api/v0/resources/locations" | python3 -m json.tool

# IP addresses inventory
printf 'header = "X-Auth-Token: %s"\n' "$LIBRENMS_TOKEN" | curl -s -K - "$LIBRENMS_URL/api/v0/resources/ip/addresses" | python3 -m json.tool
```

LibreNMS `sysName` values observed: `zeta backhauls` dev 101, `zeta pppoe` reachable via `10.0.1.1`, `mikrotik theta` 102. Unknown transports (e.g., `10.0.4.1`, `10.0.3.1`) returned null — not in inventory, rely on traceroute instead.

## When to Use (and Not)

- **Do use:** confirm hardware/model, OS version, check `status` boolean (up/down), sanity-check sysName substring overlap for `PATH_TOWER_THRESHOLD` +10 points (`path-analyzer:Step A`).
- **Don't use** to pick tower site name — Sonar `Tower:` + MyMate `outages`/`links` cache is primary. Geolocation is grouped and drifts.
- **For path-analyzer:** one or two calls per ticket at most (tower device + fiber router). No bulk `GET /api/v0/devices` per ticket (heavy 16k devices, `json.tool` failed on bulk `601k` char unterminated → paginate).

## Env

- `LIBRENMS_URL`: no built-in default; `python3 scripts/resources.py get librenms` prints the registered address
- `LIBRENMS_TOKEN` required for above (export from `.env`)
