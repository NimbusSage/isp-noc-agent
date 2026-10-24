---
name: mymate
description: Use when querying your org's MyMate network monitor (`<mymate-base-url>` from `.env`) for live device status, current outages, ping/latency history, link topology, or monitoring-engine health — e.g. corroborating a slow-speed or outage NOC ticket against monitored device state. Also use when checking whether MyMate itself is healthy or how to authenticate to it.
---

# MyMate Network Monitor API

## Overview

MyMate (mymate.network) is the operator's network monitor (a modern replacement for MikroTik The Dude). It pings every device for up/down, tracks outages and alert events, draws a live topology map, and exposes a REST JSON API. Deployed at `https://10.0.0.7`, version **1.7.2** (this skill was written against 1.4.0; the sonar-tickets section was verified on 1.7.2). This skill documents how the NOC agent reads monitored state through that API using the read-only `nocagent` operator account.

## Auth (session login)

- **Base URL:** `https://10.0.0.7` — self-signed TLS, so every request needs `-k`
- **Credentials in `.env`:** `MYMATE_URL`, `MYMATE_USER`, `MYMATE_PASS`
- **The `nocagent` account is admin** (verified: `is_admin:true`,
  same-value `PATCH /api/devices/<device-id>` returned 200). Writes technically work —
  but the agent still treats MyMate as **read-only by default** and only writes
  when a skill explicitly authorizes a specific write. Never freelance edits.

Login is a Sanctum SPA session. The login endpoint is `POST /login` (not `/api/login`):

```bash
export $(grep -E '^MYMATE' .env | xargs)
JAR=/tmp/mymate.jar; rm -f $JAR
curl -s -k -c $JAR "$MYMATE_URL/sanctum/csrf-cookie" -o /dev/null
XSRF=$(grep XSRF-TOKEN $JAR | awk '{print $NF}' | python3 -c "import sys,urllib.parse;print(urllib.parse.unquote(sys.stdin.read().strip()))")
curl -s -k -b $JAR -c $JAR -X POST "$MYMATE_URL/login" \
  -H 'Content-Type: application/json' -H "X-XSRF-TOKEN: $XSRF" -H 'Accept: application/json' \
  -d "{\"email\":\"$MYMATE_USER\",\"password\":\"$MYMATE_PASS\"}"
# IMPORTANT: login rotates the session and re-encrypts the cookie — re-read XSRF from the JAR now:
XSRF=$(grep XSRF-TOKEN $JAR | awk '{print $NF}' | python3 -c "import sys,urllib.parse;print(urllib.parse.unquote(sys.stdin.read().strip()))")
```

### Every API request needs all of these

1. The session cookie from login (`-b $JAR`)
2. `Origin: $MYMATE_URL` and `Referer: $MYMATE_URL/` headers — the Sanctum stateful guard requires them, otherwise you get `401 Unauthenticated`
3. For any POST/PUT/PATCH/DELETE: `X-XSRF-TOKEN: $XSRF` (re-read from the JAR **after** login — a stale token gives `419`)
4. `-k` for the self-signed cert

Helper:

```bash
export $(grep -E '^MYMATE' .env | xargs)
JAR=/tmp/mymate.jar
H=(-H "Accept: application/json" -H "Origin: $MYMATE_URL" -H "Referer: $MYMATE_URL/")
# GET:   curl -s -k -b $JAR "${H[@]}" "$MYMATE_URL/api/..."
# POST:  curl -s -k -b $JAR "${H[@]}" -X POST "$MYMATE_URL/api/..." \
#          -H "X-XSRF-TOKEN: $XSRF" -H 'Content-Type: application/json' -d '...'
```

## Critical gotchas (verified live)

1. **`GET /api/devices` is huge — ~35 MB, 2+ min, tens of thousands of devices. Do NOT fetch it wholesale.** Prefer `GET /api/devices/{id}` (once you have the ID) or `GET /api/outages`. If you must pull the full list, fetch once and cache to disk with a timestamp; never fetch it per ticket. The index is the ONLY response that reliably carries `site_name`/`site_id` (device `show` sometimes omits them).
2. **Deployed version is 1.4.0.** Upstream `main` has more endpoints, but on THIS server these 404: `tools/ping|trace|sweep|portscan|bgp`, `devices/{id}/trace`, `devices/{id}/probes`, `devices/{id}/icon`, `graphs`. Do not call them until the server is upgraded.
3. **Throughput/metric/sensor/alert history is currently empty** on this instance — `devices/{id}/samples`, `devices/{id}/metric-samples`, `interfaces/{id}/samples`, `devices/{id}/sensors`, `sensors`, `alert-events`, `maintenance-windows` all return `{"data":[]}`. Only **ping history** (`ping-samples`) and **outages** have live data today.
4. **Writes now succeed — stay disciplined.** The account is admin, so the old
   `403` guardrail is gone. Default to reads; only perform writes a skill
   explicitly authorizes, prefer same-value/test writes first, and re-read
   after every write.

## NOC workflows

### What's down right now (per site)

```bash
curl -s -k -b $JAR "${H[@]}" "$MYMATE_URL/api/outages?state=open" | python3 -m json.tool
```

`state` is `open` or `closed`. Each outage: `device_id`, `device_name`, `site_id`, `site_name`, `mgmt_ip`, `started_at`, `ended_at`, `duration_s`, `ongoing`, `cause`, `acknowledged`, `is_cpe`. Returns up to 500. This is the best "is this site having issues" source — filter on `site_name`/`device_name`.

### Device live status (by ID)

`GET /api/devices/{id}` — key fields: `status` (`up`/`down`), `mgmt_ip`, `poll_method` (`snmp`/`routeros`), `monitored`, `device_type`, `vendor`, `model`, `rtt_ms`, `loss_pct`, `ping_at`, `signal_dbm`, `snr_db`, `ccq_pct`, `wireless_clients`, `freq_mhz`, `cpu_pct`, `mem_used_pct`, `temp_c`, `uptime_seconds`, `last_change`, `site_id`/`site_name` (when present), `discovery_error`.

### Ping/latency history (recent)

```bash
FROM=$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%S)
curl -s -k -b $JAR "${H[@]}" "$MYMATE_URL/api/devices/<device-id>/ping-samples?from=$FROM"
```

Series of `{ts, rtt_ms, loss_pct, jitter_ms}`. Default window is 1 hour when `from`/`to` are omitted; accept ISO timestamps.

### Engine health

`GET /api/system-status` — array of `{key, label, status, detail}`: `database`, `redis`, `workers`, `polling` ("Last tick Ns ago"), `websockets`, `backups`. Also `GET /api/health` is public (`{status, checks:{database,redis}}`).

### Device notes (geomaps right-panel notes — verified)

Device notes are a sub-resource, NOT a field on the device record (there is no
writable `device_note`; a PATCH with one is silently ignored). The operator
works primarily from **geomaps, not regular maps** (odd diffs exist) — the
notes API below is what the geomaps right-panel edits:

```bash
# list
curl -s -k -b $JAR "${H[@]}" "$MYMATE_URL/api/devices/{id}/notes"
# → {"data":[{"id":1,"body":"...","author_id":1,"author_name":"...","created_at":"...","editable":true}]}
# create (CSRF header required, like all writes)
curl -s -k -b $JAR "${H[@]}" -X POST "$MYMATE_URL/api/devices/{id}/notes" \
  -H "X-XSRF-TOKEN: $XSRF" -H 'Content-Type: application/json' -d '{"body":"..."}'
# delete one note (own mistakes only)
curl -s -k -b $JAR "${H[@]}" -X DELETE "$MYMATE_URL/api/notes/{noteId}" \
  -H "X-XSRF-TOKEN: $XSRF"
```

Short-note format for outage tickets (see `tracker-sheet` §11):
`{M/D/YYYY} {issue-short}, {contact-note}, ticket #{n}`. Check existing notes
first — one note per ticket per device, never duplicates.

### Sonar tickets (GeoMap Link Ticket — verified on v1.7.2)

This MyMate fork links Sonar tickets to a **subject** (site, device, or link) —
this is the GeoMap `Link ticket` button, NOT device notes. A linked ticket
renders a visual ticket symbol over the site for as long as the ticket is open
in Sonar (closed tickets stay listed as history, no symbol). Reverse-engineered
from the UI bundle (`/build/assets/*.js`: `Link ticket` / `Unlink Sonar
ticket`); subject path is `/{kind}s/{id}`:

```bash
# list (read-only) — e.g. a site showed link `<link-id>` (`<ticket-id>` CLOSED, history)
curl -s -k -b $JAR "${H[@]}" "$MYMATE_URL/api/sites/<site-id>/sonar-tickets"
# link (CSRF header required, like all writes) — reference accepts id, #id, or Sonar URL
curl -s -k -b $JAR "${H[@]}" -X POST "$MYMATE_URL/api/sites/<site-id>/sonar-tickets" \
  -H "X-XSRF-TOKEN: $XSRF" -H 'Content-Type: application/json' -d '{"reference":"<ticket-id>"}'
# → {"data":{"id":<link-id>,"ticket_id":<ticket-id>,"status":"PENDING_INTERNAL",...,"linked_by_name":"NocAgent",...}}
# refresh cached Sonar state / unlink (own mistakes only)
curl -s -k -b $JAR "${H[@]}" -X POST "$MYMATE_URL/api/sonar-tickets/<link-id>/refresh" -H "X-XSRF-TOKEN: $XSRF"
curl -s -k -b $JAR "${H[@]}" -X DELETE "$MYMATE_URL/api/sonar-tickets/<link-id>" -H "X-XSRF-TOKEN: $XSRF"
```

Same shape works for devices and links (`/api/devices/{id}/sonar-tickets`,
`/api/links/{id}/sonar-tickets`). Always GET-list first and re-read after
linking. Linking is authorized only when a skill explicitly covers it
(`tracker-sheet` §11 authorizes the offline-site ticket link); never freelance
links. Guessed routes (`/api/tickets`, `/api/sites/{id}/tickets`,
`/api/ticket-links`, `/api/linked-tickets`, `/api/sonar-tickets` as a
collection) all 404 — the subject-scoped path above is the only one.

### Site rename (keep maps + sites in sync — verified)

Map titles and site records are separate names (see confusion note above).
Renaming one does not rename the other — e.g. renaming the "Zulu Silo" map
left the geomap showing "Zulu" until the site record was patched. Always do both:

```bash
# 1. Find the site (thousands of records — pull once, filter client-side, exact match)
curl -s -k -b $JAR "${H[@]}" "$MYMATE_URL/api/sites" | python3 -c "import json,sys; [print(s['id'], s['name']) for s in json.load(sys.stdin)['data'] if 'zulu' in s['name'].lower()]"
# 2. Rename the site record (what geomaps render) + re-read to verify
curl -s -k -b $JAR "${H[@]}" -X PATCH "$MYMATE_URL/api/sites/<site-id>" \
  -H "X-XSRF-TOKEN: $XSRF" -H 'Content-Type: application/json' -d '{"name":"<New Host Name>"}'
# 3. Find the matching regular submap (thousands of maps — same pull-and-filter; try
#    the old name and "<name> Silo" variants; ask the operator if ambiguous)
# 4. Rename the map + verify
curl -s -k -b $JAR "${H[@]}" -X PATCH "$MYMATE_URL/api/maps/<map-id>" \
  -H "X-XSRF-TOKEN: $XSRF" -H 'Content-Type: application/json' -d '{"name":"<New Host Name>"}'
```

Route check without changing anything: PATCH the current name back onto
itself (same-value PATCH 200s and changes nothing — proven on both
`/api/sites/<site-id>` style records and `/api/maps/<map-id>`). Example: site
"Zulu" → "<New Host Name>" + map "Zulu Silo" → "<New Host Name>"
(operator did the map in UI, agent did the site via API).

### Finding a device ID for a site (e.g. "golf")

1. `GET /api/outages?state=open` and filter by `site_name`/`device_name` — outages carry `device_id` and `mgmt_ip`.
2. No outage? The only name→ID map is the heavy `/api/devices` index. Fetch once, cache to disk, and match on `site_name` substring, `name` substring, and `mgmt_ip`. Don't re-pull per ticket.

## Endpoint quick reference (verified on v1.4.0; sonar-tickets rows on v1.7.2)

| Endpoint | Notes |
|---|---|
| `GET /api/health` | Public; DB + Redis status |
| `GET /api/system-status` | Engine health board |
| `GET /api/user` | Current operator (`is_admin`) |
| `GET /api/devices` | **HEAVY ~35 MB** — avoid wholesale, cache |
| `GET /api/devices/{id}` | Live status fields |
| `GET /api/devices/{id}/interfaces` | Interface list (`if_index`, `name`, `speed_mbps`, `util_in/out`, `bps_in/out`) |
| `GET /api/devices/{id}/ping-samples?from&to` | rtt/loss/jitter history — **has data** |
| `GET /api/devices/{id}/samples?from&to` | Device throughput — currently empty |
| `GET /api/devices/{id}/metric-samples?from&to` | cpu/mem/temp history — currently empty |
| `GET /api/interfaces/{id}/samples?from&to` | Interface throughput — currently empty |
| `GET /api/devices/{id}/sensors` | Custom SNMP sensors — currently empty |
| `GET /api/devices/{id}/backups/latest` | Latest config backup (empty if backups not configured) |
| `GET /api/devices/{id}/backups/versions` | Git-backed config version history |
| `GET /api/links` | Topology links (`a_device_id`, `b_device_id`, interface bindings, `bw_*_mbps`, `eff_*_mbps`) |
| `GET /api/maps` | All maps with `device_count` |
| `GET /api/maps/{id}` | Map detail: `positions` (device_id + x/y), child maps, notes |
| `GET /api/outages?state=open` | Outage timeline — **has data** |
| `GET /api/alert-events` | Alert log — currently empty |
| `GET /api/maintenance-windows` | Maintenance windows — currently empty |
| `GET /api/update-check` | `{current:"1.7.2", latest, update_available}` |
| `GET /{subjects}/{id}/sonar-tickets` | Linked Sonar tickets (`{subjects}` = `sites`, `devices`, `links`) |
| `POST /{subjects}/{id}/sonar-tickets` | Link ticket `{reference: "<id|#id|url>"}` — GeoMap symbol while open |

## Write operations (work now — still needs explicit skill authorization)

Useful admin endpoints the `nocagent` account can now use, but only when a skill explicitly authorizes the specific write: `POST /api/devices`, `PATCH /api/devices/{id}`, `POST /api/subnets`, `POST /api/discovery-candidates/{id}/approve|ignore`, `POST /api/alert-events/{id}/ack`, maintenance-windows CRUD, `POST /api/backups/run-all`, `POST /{subjects}/{id}/sonar-tickets` (link), `DELETE /api/sonar-tickets/{linkId}` (unlink own mistakes), `POST /api/sonar-tickets/{linkId}/refresh`. If a ticket needs one of these without a covering skill, ask a human admin.

## Common mistakes

- Forgetting `Origin`/`Referer` headers → `401 Unauthenticated` (session not treated as stateful).
- Forgetting `X-XSRF-TOKEN` on writes → `419` CSRF mismatch.
- Fetching the full `/api/devices` per ticket → 35 MB / 2+ min download. Cache it.
- Calling upstream-only endpoints (`tools/*`, `trace`, `probes`, `graphs`) → `404` on this server.
- Expecting samples/sensor/alert data → currently empty; use outages + ping-samples + device status instead.
- Treating MyMate like a write source → every write is `403`; it is a read-only monitoring source for the agent.
