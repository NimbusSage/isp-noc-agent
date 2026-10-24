# Rain-Fade Readiness — Wave link mode inside path-analyzer

Event-driven mode (operator-invoked, or triggered when the ticket trail shows a 60 GHz
link that takes its tower down every rain event — e.g. a slow-tower ticket where
the customer's only egress sits behind a single 60 GHz backhaul to the fiber edge). For every Wave link on the traceroute path:
forecast (ITU-R P.838-3) the rain rate at which it dies, audit the backup layer by link
class, run the Wave built-in speed test where useful, and — when a ticket is given —
post ONE unified comment under its own header/marker.

Load, before acting: `wave-gui` (radio reads/speedtest), `ticket-context` (weather scan),
`mymate` + `librenms` (outage/error corroboration). Physics and tier thresholds:
`scripts/rain_fade_model.py` (P.838-3 coefficients inside, validated against the
ITU-validated ITU-Rpy implementation; unit tests `scripts/test_rain_fade_model.py`).

**Contract:** same as the rest of `path-analyzer` — a mode of the single unified
comment. When this mode runs on a ticket it posts ONE comment under header
`Rain Fade Readiness - {site}` with HTML marker `<!-- rain-fade-readiness -->`
(via `scripts/sonar_comment.py --verify`, `&quot;` for attributes), separate from and
optionally after a `Direct Path Analysis` run; never a second analyzer comment for the
same ticket in the same run. No bounce decision ever comes out of this mode — it is a
capacity/certainty report. Only the operator re-tunes radios or changes configs.

## Lessons from the first live run (2026-10-24, ticket <TICKET_ID>, an anonymized Wave Pro pair)

- **Margin source, verified shippable:** the `/statistics` endpoint exposes no modulation
  threshold table, but `linkQuality.signalHistogram` (min/bin-width from min..max)
  shows the lowest signal bin the radio still decoded frames at — the empirical survival
  floor. Margin = worst-end day-average signal − decode floor (12 dB on this run).
  Defensible and radio-reported; do not substitute vendor sensitivity guesses.
- **Run order:** live reads both ends → ucfg carrierd/backup audit (MCP) → OSPF check on
  the OSPF-hub router bordering the link → model → speed test LAST (it loads the radio).
- **Failover findings that changed the read:** one-sided backups are a real state (ST
  `backup.enabled=true` forever `connecting` because the AP's backup radio is
  `disabled`); the red flag to check is the border router's *default route*
  (`ip-route-active`) — a single 0.0.0.0/0 next-hop through the wave means the pocket
  behind it is single-path.
- **Operator correction (same live pair):** disabled Wave-internal backup radios on both
  ends are NOT proof of no rain redundancy — this pair had a dedicated parallel 5 GHz
  link (Class C) that the first run missed. The actionable gap was carrier-drop disabled
  on the 60 GHz radios, which lets a rain-degraded primary hang instead of failing over
  to the parallel link; the operator enables carrier drop at an acceptable threshold and
  to the parallel link; the operator enables carrier drop at an acceptable threshold and
  readiness jumps. Therefore: ALWAYS hunt for a parallel link between the same two sites
  (R4 Class C step 1) before reporting a missing backup layer, and report carrier-drop
  state as THE fix for Class C. Correction of the first re-test comment, same pair:
  carrier drop was treated as an AP+ST feature and the disabled AP side flagged for a
  "symmetry toggle" — wrong; carrier drop is ST-side only by design, the AP side stays
  `carrierd.enabled=false`, and the ST-side enable alone is the complete fix.
- **Evidence honesty:** MyMate may return zero outage rows for Wave radios (none stored
  for this pair) — say so in the comment instead of implying history. The ticket trail's
  weather citations + screenshots carry the real-world physics claim.
- **Speedtest politeness proved:** 10 s/direction on a link carrying ~80-230 Mbps carried
  fine alongside live traffic (radio airtime was only 7%, 660 Mbps dry capacity reported).
  Keep `--duration ≤ 10` / 15 unless the operator asks for longer.
- **Comment format:** header `Rain Fade Readiness - {site}` + `<!-- rain-fade-readiness -->`
  marker is now a recognized verify marker in `scripts/sonar_comment.py`; never post under
  a `Direct Path Analysis` header (that is the reserved correlator key).

## R1 — Identify the ticket/site and the weather stakes

- Input: bare site name, or ticket ID (resolve first note `Tower:` per `ticket-context`).
- Weather scan is mandatory here (not only in Step 3.7): subject + description + ALL
  `ticket_comments.body` for rain/storm/wet-weather words. Report `weather_excluded: true`
  when found — the report still runs (an operator asking for readiness on a rainy ticket
  is legitimate), but the comment must say the incident trail cites weather.

## R2 — Discover the path and pick the Wave links

Reuse path-analyzer Steps A/C/D (Sonar → MyMate → LibreNMS cascade, traceroute). Then:

1. Pull LibreNMS device list (cached fine): `os=airos-af60` + hardware starting with
   `Wave` = Wave family.
2. Match path hops to Wave radios: LibreNMS `sysName` convention `a2b ap|st` pairs at
   both ends of the RF link; MyMate device rows and CRE mgmt IPs corroborate.
3. Each **hop pair** on a Wave link needs BOTH mgmt IPs: name pattern
   `<link><class> ap`/`<link><class> st` where class ∈ wave60 nano/lr/pro base names.
   Where LibreNMS sysNames differ (re-badged radios), fall back to `site-pack/devices.md`.

## R3 — Read the radios (load `wave-gui`)

`node scripts/wave_gui.mjs read --url https://<mgmt_ip>` for **both ends** of every Wave
link. Per end, collect:

- `radios.main.linkState` − connected / not
- `radios.main.localPeer.{signalDbm,signalDayDbm,idealSignalDbm,minSignalDbm,maxSignalDbm}`
- `radios.main.localPeer.mcs` − {rxIdx, txIdx, rxLabel, txLabel, rxIdxIdeal/txIdxIdeal}
- `radios.main.localPeer.capacityMbps` − {combined, combinedIdeal, dl, dlIdeal, ul, ulIdeal}
- `radios.main.frequencyCenterMhz` (60 GHz), `antennaGainDbi`, `conductedTxDbm/eirpDbm`, `waveAi`
- `radios.backup.*` (exists or not, linkState)
- `carrierDrop.dropped`
- `peerMacs[]`, `device.{model,firmware,mac}`
- **The far end's gps** — each read reports the far radio's position in
  `localPeer.gps`/`remotePeer.gps` (live-verified: the AP read carries the ST-side fix and
  vice versa). Path length km = haversine(far(AP read), far(ST read)).

Config truth via MCP (not the GUI): `run-diagnostics ubnt-wave ucfg-show` on both ends →
`network.carrierd.{enabled,capDownRx,capDownTx,downDelay,upDelay}` +
`wireless.radios.backup.{enabled,txpower,bandwidth}`.

## R4 — Classify and audit the backup layer per link class

- **Class A — 5 GHz backup enabled & tuned** (`wireless.radios.backup.enabled=true`):
  when the operator asks for a capacity test, run the Wave built-in speed test from the AP
  (`wave_gui.mjs speedtest --url <AP> [--duration 10] [--direction rx|tx]`) to grade
  what the backup actually carries when the 60 GHz fades. Record avg/max Mbps. The
  unattended auto-trigger never runs it (it loads the radios).
- **Class B — 5 GHz backup disabled + carrier drop** (`backup.enabled=false` and/or the
  carrier-drop pattern in `ucfg show`): the survival mode is kill-the-port + OSPF
  reroute. Audit without changing anything:
  - `ucfg show` → `network.carrierd.capDown/Up (Bps threshold)`, delays, `upNoLink` —
    the ST turns its ether off below these capacities.
  - OSPF check on the tower routers bordering the link (`mikrotik-ssh` read path:
    `/routing ospf neighbor print`, default route `ip route print where dst-address=0.0.0.0/0 and active`) —
    confirm a viable alternate next-hop exists that is NOT the faded Wave link.
  - Alt-path load test (`btest` through the alternate, nothing disabled): confirms the
    reroute path actually converges and carries capacity.
  - **Red flag** when the only viable alternate next-hop is itself a Wave/60 GHz link —
    rain took down the primary, rain takes down the fallback.
- **Class C — dedicated parallel 5 GHz link beside the Wave pair** (a separate radio
  link between the SAME two sites, e.g. a 5 GHz MLO link; the Wave-internal backup radio
  disabled on both ends is normal here, NOT a missing backup):
  1. **First look for the parallel link before declaring anything dead.** On-site 5 GHz
     radios usually show up as their own devices next to the Wave pair in MyMate/LibreNMS
     (`<site1>2<site2> ... mlo/mlo5` naming, 5 GHz frequency, same two towers in
     geolocation) or as a second OSPF neighbor on the border routers. Search by site-name
     pattern across the device list; do not stop at the Wave-internal `radios.backup`.
  2. **The real red flag is carrier-drop OFF on the 60 GHz radios** (where this install
     uses it): with `network.carrierd.enabled=false` the rain-degraded primary does not
     yield cleanly — it can hang at a bad-enough MCS/loss instead of dropping, so traffic
     never moves to the parallel 5 GHz link. The fix is operational, not architectural:
     enable carrier drop at an acceptable threshold (operator step), then the 60 GHz port
     drops at rain-kill capacity and OSPF converges to the parallel link.
  3. **Carrier drop is a STATION-side-only feature on these Wave radios** (operator-confirmed:
     "carrier drop is literally only enabled on the station radio, not the AP at all").
     Judge the carrier-drop layer by the ST end's `network.carrierd.enabled` + thresholds.
     The AP reading `carrierd.enabled=false` is NORMAL — never flag it as a gap, never
     recommend enabling it, and never call one-sided carrier drop a "symmetry" problem.
  4. **Audit:** verify the parallel link is up end-to-end (both device states, signal),
     confirm an OSPF neighbor/next-hop over it exists that is NOT the Wave path, and
     (read-only) grade its capacity with a btest through it if it is path-testable.
     Carrier-drop sufficiency is judged on the ST end only (see item 3 above).
  5. **Comment must say which class was found** — a Class C finding replaces the
     "no 5 GHz backup" line: name the parallel link, its state, and whether carrier
     drop is engaged on the Wave pair (enabled/thresholds vs disabled).

## R5 — Physics: the rain rate each link dies at

```bash
python3 scripts/rain_fade_model.py --freq 60 --dist <km> --margin <dB>
```

- `dist_km` = haversine from R3 GPS far-fixes (or `wireless.apconfig.*.distance` in
  `ucfg show` when GPS is missing); margin = per-end
  `signalDbm − minimum modulation threshold dB` from the Wave radio (the GUI/API shows
  the modulation table; tx/rx MCS labels classify QPSK → 64b3/4 steps — see
  `wave-gui` SKILL.md field map). Worst end wins.
- Output: `kill_rain_rate_mm_h` and a computed **tier**:
  `red ≤ {tier_red} mm/h` (readiness env, default 25), `amber ≤ {tier_amber}` (50),
  else `green`. Show tier + margin + distance + forecast kill rain rate in the comment —
  plus tier thresholds re-verified against 7-day MyMate outage evidence per R6 — so the
  tier is a *calibration*, not a fiction.

## R6 — History: prove the fade behavior from reality

- 7-day MyMate outage clustering per link device id (`outages?state=open`, plus recent
  resolved via device notes history) — N outages on rain events and lost-service times
  prove the *real* kill rain rate for the tier (if P.838 says the link dies at 25 mm/h
  but MyMate shows it dropping at a much lower rate, shift the score).
- LibreNMS error counters on each Wave mgmt interface (graceful degradation when absent;
  NEVER the workflow's substitute for MyMate evidence).
- Every claim in the comment cites evidence lines, not model-only numbers.

## R7 — Comment (only when a ticket ID is given)

One block appended-or-posted under header `Rain Fade Readiness - {site}` with the
`<!-- rain-fade-readiness -->` marker (`comment-formatting`):

- Per link: `class A/B`, current signal/ideal/mcs, capacity vs ideal, backup state,
  carrier-drop state, distance km + margin dB + forecasts: `kill rain rate`/`tier`.
- Speed test results (Class A): per-direction avg/max Mbps.
- OSPF/carrier-drop audit (Class B): alt path state + load test.
- Evidence chronology: 7-day outage clusters, LibreNMS incident counts, weather-cited
  flag, `score: {bullet_readiness}/100`.

No Sonar group/category changes in this mode; no auto-bounce ever. When the operator
asks for radio re-tuning or 5 GHz retunes, that is a human step; recommendations are
ineffective inference.

## Env

| Variable | Default | Purpose |
|----------|---------|---------|
| `RF_TIER_RED_MM_H` | `25` | kill-rate tier red threshold in mm/h (environment or `.env`) |
| `RF_TIER_AMBER_MM_H` | `50` | tier amber threshold in mm/h (environment or `.env`) |
| `WAVE_GUI_USER` / `WAVE_GUI_PASS` | — | only when overriding the credstore pool |

All credential variables ride through the usual credstore path (`wave-gui` SKILL.md);
none appear in comments, logs, argv, or Sonar.
