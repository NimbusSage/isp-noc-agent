---
name: wireless-health
description: Use when a path-analyzer hop shows degraded throughput or unexplainable variance between full-path and /30 direct tests and you need to investigate the wireless link health of that pair — pulls radio signal/CCQ, jitter/loss, and registration details, then returns structured data to be appended to the single path-analyzer comment.
---

# Wireless Health — Link Investigation Helper

Helper invoked **by** `path-analyzer` (not standalone posting). Investigates a single bad pair `(k, k+1)` after Step H flags `unexplainable variance` (`|D - (R_{k+1}-R_k)|/D >40%`) or `D_k <60% median`.

**Contract:** returns data, never posts. `path-analyzer` formats into the one unified `sonar_comment.py` HTML — never a second comment (spam guard).

## When Triggered

- Single pair only per ticket (the worst flagged), not every hop — limits load
- Only after `path-analyzer` already completed E-G and has `R_k` + `D_k` + `/30` peer IPs
- Input: `pair = { a_ip, b_ip, a_name, b_name, D_mbps, R_delta }`, `tower_router_ip`

## Sources (best-effort, any may be unavailable)

### 1. MyMate (primary, when devices indexed)

- `GET /api/devices/{id}` for each side after mapping `ip → device_id` via cached `/api/devices` or `GET /api/outages?state=open` (`mymate:69` `signal_dbm/snr_db/ccq_pct/freq_mhz/cpu_pct`).
- `GET /api/devices/{id}/ping-samples?from=6hAgo` → `rtt_ms/jitter_ms/loss_pct` series (`mymate:72`).
- `GET /api/links` (`mymate:106`) interface bindings for link name.

MyMate today has **ping + outages live** but `samples/metric-samples/interfaces/samples empty` (`mymate:52`) — CCQ/signal may be null; handle gracefully.

### 2. MikroTik live (via `mikrotik-ssh` creds)

For each side that is a MikroTik AP/backhaul (not UBNT):

```bash
export PW="${BTEST_PASSWORD:?set BTEST_PASSWORD in the environment (operator-supplied, see .env.example); never read credentials.json}"
# registration table (MikroTik wireless)
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<radio_ip> '/interface wireless registration-table print detail'
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<tower_ip> '/interface wireless registration-table print where interface~"wlan"'
# ethernet/phys
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<ip> '/interface wireless print detail'
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<ip> '/interface print detail where name~"wlan|ether"'
# logs
SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@<ip> '/log print without-paging where topics~"wireless"'
```

New MCP templates for these are in `path-analyzer` allowlist additions (`wireless-registration-table`, `wireless-print`). If device is UBNT `ubnt-uisp`/`ubnt-airos` (`allowlist.ts:152-251`), use `device-detail`/`device-statistics` + `status`/`stations`.

### 3. LibreNMS ports

```bash
printf 'header = "X-Auth-Token: %s"\n' "$LIBRENMS_TOKEN" | curl -s -K - "$LIBRENMS_URL/api/v0/devices/<ip>/ports" 2>/dev/null
# port alerts: ifInErrors/ifOutErrors, ifInDiscards rising
```

Grouped geolocations not used for site truth — just error counters.

### 4. UISP (UBNT only)

`device-statistics` interval `HOUR` for the radio's wireless rate.

## Thresholds (flag degraded)

- **Wired first:** before returning `degraded` for RF reasons, verify negotiated
  ethernet rate/duplex on every wired hand-off of the pair (`/interface ethernet monitor <iface> once`
  on both routers + any switch between + radio-reported rate via UISP/radio UI). A 100M or
  half-duplex port mimics wireless loss exactly — if found, return `status: degraded` with
  `log_hint: wired 100M/half-duplex port <device>/<iface>, not RF` so path-analyzer does not
  dispatch wireless work.
- CCQ < 80% or signal < -75 dBm or SNR < 15 dB → degraded
- jitter > 10ms or loss >1% on 6h ping-samples → unstable
- registration tx-rate far below interface capability (< 50% of `interface-list` advertised speed)
- Recent wireless log flaps (disconnected/reconnected) in last 24h

## Return Shape

```json
{
  "pair": "zeta pppoe ↔ zeta backhauls (10.0.1.1)",
  "D_mbps": 919,
  "R_delta": -17.8,
  "variance_pct": 520,
  "status": "degraded|healthy|unknown",
  "signal_dbm": -67,
  "snr_db": 28,
  "ccq_pct": 94,
  "freq_mhz": 5755,
  "jitter_ms": 2.1,
  "loss_pct": 0.2,
  "registration": "MCS9/80MHz tx 433Mbps rx 433Mbps",
  "ports": {"ifInErrors": 0, "ifOutDiscards": 12},
  "log_hint": "no recent flaps",
  "source": "mymate+wireless-registration"
}
```

`path-analyzer` then appends formatted HTML to its single comment (see `path-analyzer/SKILL.md` Step I template):

```html
<p><strong>Wireless Link Health — zeta pppoe ↔ zeta backhauls:</strong></p>
<p><span style=&quot;color:green&quot;>→ CCQ 94% · signal -67 dBm · SNR 28 dB — link healthy (D 919 Mbps CPU-limited, R delta -17 Mbps, variance flagged but generation bound)</span></p>
```

If sources empty → `status: unknown`, line reads `Data unavailable — MyMate samples empty, no wireless registration (not a MikroTik AP)`.

## Errors

- Map miss (`ip → device_id` no match) → skip MyMate, try wireless table directly
- UBNT vs MikroTik detection via LibreNMS `os` (`routeros` vs `airos`/`wave`) or sysDescr `RouterOS` vs `UISP`
- Never throw — return `unknown` so `path-analyzer` still posts its path comment successfully

## Env

- Reuses `LIBRENMS_URL`/`LIBRENMS_TOKEN`, `PATH_TOWER_THRESHOLD`, MyMate `MYMATE_URL` (`mymate:14` `-k` self-signed, `Origin`/`Referer` + `X-XSRF-TOKEN` required)
