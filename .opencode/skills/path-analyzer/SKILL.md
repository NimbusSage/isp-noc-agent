---
name: path-analyzer
description: Use when triaging slow-speed/tower NOC tickets by testing the live network path directly from the tower router — traceroute to internet, bandwidth-test to fiber router hop-by-hop, and /30 OSPF link tests — as a true NOC tech replacement for calling an external analyzer API.
---

# Path Analyzer — Live Router Triage

> **Examples:** addresses and names in this skill belong to a fictional network. This organization's real ones come from `python3 scripts/resources.py list` and `site-pack/`; never reuse an example.

True NOC workflow: log in to the tower router, traceroute to internet, btest to fiber router hop-by-hop, test each radio link over its OSPF /30, flag variance and real multipath.

Example path used throughout this skill: `zeta pppoe 10.0.1.1` → `Eta Fiber 10.0.5.1` → `edge-2216-a 10.0.6.1` (see `site-pack/topology/verified-probes.md` if your install keeps one). Before writing theory, test on real devices.

**Topology path priority:** the topology data lives in `site-pack/topology/` (untracked, per-org) — read `fiber-map.json`, `bgp-rr-map.json`, `fiber-testing-ips.json`, `fiber-topology.md`, `verified-probes.md` from there. Fictional schema examples ship in `site-pack/example/` (`fiber-map.example.json`, `bgp-rr-map.example.json`, `fiber-testing-ips.example.json`, `fiber-topology.example.md`): copy one to `site-pack/topology/` (dropping `.example`) and fill it in. **If a file is absent, do not fail:** ask the operator for the value or fall back to live discovery (Step C traceroute + Step D reversed walk) and say in the comment that the table lookup was skipped. Same rule when those paths appear in env-var defaults elsewhere in this skill.

## When to Run

- Slow speeds / slow tower (slow-speeds category `sonar.categories.slow_speeds` or slow keywords) per `ticket-context`
- Replaces any external-analyzer fallback (Step 3.5) — never run both
- Skip if not slow-tower, or if deferred to peak window via `peak-scheduler` (Option A)

## Single Unified Comment

`path-analyzer` is the **sole poster** for this path analysis. If `wireless-health` is triggered on a bad hop, its findings are **appended** to the same HTML buffer before the single `scripts/sonar_comment.py --verify` post — never a second comment. This respects `comment-formatting` single-comment rule and avoids spam. `correlated-issues` still indexes via header `Direct Path Analysis - {site}` plus HTML comment `<!-- path-analyzer -->`.

## Step A — Resolve Start Device (hardest)

Naming: `Smith` → first, `BSmith` → second, `Barry Smith` → third if `BSmith` taken, `BSmith Two` / `Barry Smith Two`. Sonar is source of truth; MyMate/UISP use full host name → confusion originates there.

**Cascade (Sonar truth first, case-insensitive):**

1. Fetch first note `ticket_comments ASC 1` and subject (in a batch run they are already in the batch file: `facts.first_note`, `facts.tower_hints`). If `Tower: X` → canonical site (`ticket-context:118`). Else `TH:`/`Tower Host:`/`Relayed from` + subject parsing `[Fiber] site pppoe` (`ticket-context:92`). Expand a tech-initials host abbreviation (e.g. `<tech-initials>`) via the TH field.
2. **Sonar sanity:** keep abbreviation literal for search if no TH; don't guess host.
3. **MyMate (cheapest):** `node scripts/mymate_query.mjs site <site>` — one call returning the open outages for the site (`device_id`+`mgmt_ip`) and its devices from MyMate's server-side search (`?q=`; the device list itself is paginated, never walk it per ticket). Match `site_name` exact → `name` substring → `mgmt_ip`.
4. **LibreNMS corroboration only:** `GET $LIBRENMS_URL/api/v0/devices` or `/api/v0/devices/:hostname` with `X-Auth-Token: $LIBRENMS_TOKEN` (`<LIBRENMS_URL>/api/v0` from `python3 scripts/resources.py get librenms`; docs https://docs.librenms.org/API/). sysName/geolocation confirms hardware/OS but is **not** source of truth for tower site (geolocation grouped). Timeout 10s.
5. **Oxidized existence:** `GET <OXIDIZED_URL>/node/fetch/<ip>` (`python3 scripts/resources.py get oxidized`) (`reprogram-device:46`).

**Tower confidence 0-100 (like `ihn-auto-return:63`, threshold env `PATH_TOWER_THRESHOLD` default 75):**

| Signal | Points |
|--------|--------|
| Exact literal `lower(parsed)==lower(found)` (e.g., `zeta==zeta`) | +40 |
| First note `Tower:` explicit and matches parsed | +30 |
| TH / `Relayed from` resolving abbreviation | +25 |
| MyMate outage/devices exact match single | +20 (contains but not single → +15) |
| LibreNMS sysName substring overlap | +10 |
| Device reachable (`system-resource` ok) | +30 |
| Fallback empty / no note | -30 / -40 (-20 if exact literal true) |

Clamp 0-100. `<75` → post `HEALTHY but low tower confidence {n}% — needs human` + Google Chat (like `ihn-auto-return:88`). Bounce only when ≥ threshold.

## Steps B-H in one call (use this first)

```bash
python3 scripts/path_probe.py --tower <tower_ip> --network <Network/Fiber>    # or --fiber <internal testing ip>
```

Give the shell call a timeout of at least 600000 ms (it runs every bandwidth test in sequence, budget 540 s). It
runs Steps B-H through the MCP tools and prints one JSON object: `hops` (name, cpu, latency), `to_fiber` (the
R series), `direct` (each router to the next), `flags` (`path_drop`, `unexplainable_variance`, `low_direct`,
`cpu_high`, `latency`), `variance`, `multipath` and `errors`. Exit 3 = start router unreachable (`needs_human`,
`Analysis: ✗ tower router unreachable`). It measures and flags only: you still judge the result, run the
port-speed check and `wireless-health` where flagged, and write the comment. Fill in any step it reports in
`errors` by hand with the MCP calls below; `--no-btest` gives the path without load-generating tests.

**Device access is MCP only.** Every router command below is `node mcp-server/dist/tool-cli.js run-diagnostics` with JSON arguments. Never use
`ssh`/`sshpass`, never read `mcp-server/config/credentials.json`, and never put a password in a command:
`bandwidth-test` takes no user or password, because the MCP server authenticates it from the encrypted pool.

## Step B — Validate Reachability

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"system-resource"}'
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"system-identity"}'
```

If unreachable → `needs_human`, `Analysis: ✗ tower router unreachable`, notify via `google-chat`.

## Step C — Discover Path (traceroute)

**MCP `tool-traceroute`** (`mikrotik-ssh`, `diagnostic`, `readOnly:true`):

- Template: `/tool traceroute address={{address}} count={{count}} use-dns=no`
- Default `count=1`, `address=8.8.8.8` (and `1.1.1.1` dual to catch DIA vs the upstream transit provider)

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"tool-traceroute","params":{"address":"8.8.8.8","count":1}}'
```

Parse ordered `ADDRESS` column (e.g., Zeta's: `10.0.1.1` → `10.0.2.1 iota pppoe` → `10.0.3.1 kappa pppoe` → `10.0.4.1` transport → `10.0.5.1 Eta Fiber` → `10.0.6.1 edge-2216-a` → `198.51.100.42` public).

**Resolve router name for every hop (never show bare IP alone):** for each `10.` hop, run MCP `system-identity` on that IP (primary). Fallback: `GET $LIBRENMS_URL/api/v0/devices/<ip>` `sysName` (`librenms/SKILL.md:1`), then MyMate `GET /api/devices/<id>` `site_name`/`name` (`mymate:60`). Cache results in `map ip→name` and use `"{name} ({ip})"` for every hop line (e.g., `kappa pppoe (10.0.3.1)` not `10.0.3.1`). If name fetch fails, show last known `name` from traceroute transport comment, never bare IP.

**Fallback when firewalled** (check `firewall-filter`): reconstruct via `ip-route` next-hops + MyMate `GET /api/links` (`mymate:106`) topology walk toward core. See `references/path-discovery.md`.

## Step D — Identify Fiber Router + Testing IP (table-first, traceroute fallback)

**Primary: table lookup** `site-pack/topology/fiber-testing-ips.json` (and live Google Sheets when `GOOGLE_SHEETS_FIBER_URL` env set, see `site-pack/topology/fiber-testing-ips.md` if you keep one). Key = `Network/Fiber` field from first note (e.g., `romeo` → `10.0.7.77` internal, `Charlie` → `10.0.8.1` internal). Use `internal` (Loopback) for `bandwidth-test` through private network — this is the correct `address=` for Steps E/F, **not** the traceroute last-10 guess. `public` (CGNAT) is for optional public-path test; `xxx` → skip and note `test to the core 2216 edge`. `Subleased Networks N/A` → skip private btest (VPN-constrained). Where the table's Extra Notes name a dedicated btest account for a fiber router (e.g. `November 10.0.9.3`), that account belongs in the `mikrotik-ssh` credential pool (ask the operator to add it); MCP then tries it automatically. Until then see the dedicated-account call under Steps E-G.

**Fallback when fiber field missing or `xxx`:** walk traceroute reversed per verified topology (`site-pack/topology/fiber-topology.md`): edges `EDGE_ROUTER_IPS` (`edge-2216-a 10.0.6.1`, `edge-2216-b 10.0.6.2` dual-homed) → optional `RDS2216` (`RDS2216_SUBNET` name `rds`/`cgnat`) → first `10.` after edge not in RR set (`site-pack/topology/bgp-rr-map.json` from `/routing/bgp/connection print`, not `peer print`, e.g. `alpha-bgprr1 10.0.20.1`), name `*fiber*` or owns `/24`. Then also try static `site-pack/topology/fiber-map.json`.

**Live Sheets override:** when `GOOGLE_SHEETS_FIBER_URL` set, `curl -s "$GOOGLE_SHEETS_FIBER_URL"` CSV → `/tmp/fiber_testing_ips_live.json` (published CSV avoids OAuth; private sheet uses `sheets.googleapis.com/v4/...?key=$GOOGLE_SHEETS_API_KEY`). Prefer live; fallback to `fiber-testing-ips.json` on fetch fail. Your Sheet edits translate live without agent code change.

**DIA (`5009` WireGuard):** flag `dia_monitored` — still btest to `internal` but note VPN.

Resolves `fiber_target = internal` (loopback) for btest; traceroute `FIBER_ROUTER` hop name still via identity lookup for display.

## Steps E-G — Bandwidth Tests

`bandwidth-test` runs through MCP (`diagnostic`, `readOnly:true`, audit logged) **without** `user`/`password`: the MCP server authenticates the test with the `mikrotik-ssh` pool itself, trying each pool entry until the btest server accepts one, and reports which entry (`authenticated with pool credential #n`) or `no pool credential was accepted by the test server`.

Env: `BTEST_DURATION` default 10s, `BTEST_PROTOCOL=tcp`, `BTEST_TIMEOUT_MS=60000` (btest blocks; `mcp-server/config/server.json:13` override). MCP caps `duration` at 30 s.

- **E Baseline:** tower → fiber, **address = `internal` from `fiber-testing-ips.json` / Sheets for that `Network/Fiber`**, not the traceroute guess (fixes cases like `romeo` where the traceroute guess `10.0.7.5` failed but table `10.0.7.77` succeeds; `Charlie` table `10.0.8.1` internal vs `10.0.8.200` public)
  ```bash
  FIBER=$(python3 -c "import json; m=json.load(open('site-pack/topology/fiber-testing-ips.json')); print(m['<network>']['internal'])")   # the internal address of that Network/Fiber
  node mcp-server/dist/tool-cli.js run-diagnostics "{\"host\":\"<tower_router_ip>\",\"deviceType\":\"mikrotik-ssh\",\"command\":\"bandwidth-test\",\"params\":{\"address\":\"$FIBER\",\"duration\":10}}"
  # Zeta baseline: 47-71Mbps tx, 168-205 rx local-cpu 40-41% (not CPU-limited)
  ```
  Dedicated btest account not yet in the pool: build the arguments from the environment so the password never reaches a command line, the transcript or the audit log (`BTEST_PASSWORD` is operator-supplied):
  ```bash
  python3 -c 'import json,os; print(json.dumps({"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"bandwidth-test","params":{"address":"<fiber_ip>","user":"btest","password":os.environ["BTEST_PASSWORD"]}}))' | node mcp-server/dist/tool-cli.js run-diagnostics --stdin
  ```
  For `xxx` public (core edge etc.) skip private btest and note the core 2216 edge.

- **F Iterative:** for each hop router IP in traceroute, run the same MCP `bandwidth-test` with `host` = that router and the same fiber address. Yields series `R_N … R_1` where drop `R_{k+1}-R_k` localizes bottleneck to link `k↔k+1`.

- **G Direct /30:** for each adjacent `(k,k+1)`:
  1. Primary: MCP `routing-ospf-neighbor` on the outer router → peer `address` = /30 peer IP; MCP `ip-address` shows the subnet.
     ```bash
     node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"routing-ospf-neighbor"}'
     # 0 D address=10.0.2.2 router-id=10.0.2.2 state=Full
     node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"ip-address"}'
     # 10.0.2.0/30 network 10.0.2.0
     ```
  2. Fallback: traceroute hop IP (usually OSPF /30 as you noted).
  3. Bridges rare `/29/28/24`: `ip-address` subnet determines — use whatever peer IP present.

  Direct btest:
  ```bash
  node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_router_ip>","deviceType":"mikrotik-ssh","command":"bandwidth-test","params":{"address":"<next_hop_ip>","duration":5}}'
  # verified 919.9Mbps tx / 860.9Mbps rx local-cpu 94% remote 83% — CPU-limited caveat "traffic generation ... might not be representative of forwarding performance"
  ```

  Direct hop duration 5s (shorter). Serial only, cap 8 hops, cap total skill wall ~20 min (respects `BATCH_TIMEOUT_MS=2700000`).

## Step H — Variance + Multipath

**Variance (tightened vs analyzer 50%):** `|direct_30 - (R_{k+1}-R_k)| / direct_30 >40%` → flag `unexplainable variance` → trigger `wireless-health` + fetch `interface-list` + `log-recent`.

**Multipath (true NOC method, not interface RX packet inference):**

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<tower_ip>","deviceType":"mikrotik-ssh","command":"ip-route-active"}'
# look only at dst-address 0.0.0.0/0. Zeta: single DAo 0.0.0.0/0 gateway=10.0.2.2%ether1 distance=110 → not multipath → OMIT multipath line entirely
# multipath = count distinct gateways >1 active → ONLY then note load-balanced and list gateways
```

Distinct `gateway` count >1 = real `load_balanced:true`. **NEVER mention multipath/load-balanced unless `>1` gateways detected — omit the `<p><strong>Multipath:</strong>…</p>` block entirely when single gateway.**

**NEVER show `single active default via … — not load-balanced`** — that line is noise; only when true multipath exists, add the block with both gateways.

**Port-speed check (wired before wireless):** a degraded hop with clean
ethernet error counters is NOT automatically a bad radio. Before blaming wireless, check the
negotiated rate/duplex on EVERY wired hand-off of that hop pair — tower/transport router ether
ports, intermediate switch ports, AND the radio-reported ethernet rate (radio UI/UISP; airFiber
falls back to 100M on a bad cable/POE while the router side can still show 1G). A 100M (or
half-duplex) port in a gigabit path caps + drops under load and mimics wireless loss exactly
(an airFiber link between two towers was once misdiagnosed as bad wireless; the true cause was a 100M ethernet port).

```bash
node mcp-server/dist/tool-cli.js run-diagnostics '{"host":"<hop_ip>","deviceType":"mikrotik-ssh","command":"interface-ethernet-monitor","params":{"interface":"<iface>"}}'
# require: rate: 1Gbps (or higher) + full-duplex: yes on both ends of every hand-off
# MCP already tries every mikrotik-ssh pool credential in priority order (some routers and switches only accept non-first ones)
# then the switch in between (/system identity confirms, /interface print comments name the peer)
# then radio-reported rate via UISP/radio UI when router+switch ports are all 1G but loss persists
```

Rate <1Gbps or half-duplex anywhere on the pair → root cause is wired; fix cable/POE/port
(speed/duplex forced mismatch is also suspect) and do NOT dispatch wireless work. Only when all
wired hand-offs are 1G/full AND radio-reported rate is full should `wireless-health` blame the RF path.

**Bottleneck criteria** (reuse the standard 50% rule):

- Bandwidth drop >50% between consecutive full-path hops
- CPU>80% (`/system resource print cpu-load`)
- Negotiated ethernet rate <1Gbps or half-duplex on any hand-off of the pair (wired cause — see above)
- Interface util>80% (`/interface print` RX/TX stats vs speed)
- Latency spike >100ms internal (traceroute AVG)

Any → mark hop `critical` (red) / `warning` (amber) per `comment-formatting`.

## Step I — Single Unified Comment

**One `scripts/sonar_comment.py --verify` post per ticket.** Template (`comment-formatting` rules: `&quot;` for attrs, `BODYPLACEHOLDER` forbidden `sonar_comment.py:76`):

**CRITICAL: Every hop must show bandwidth + colored callout — never list hops as plain `→ hop`.** Each hop's `R_k` (full-path btest from that hop to fiber, Step F) and `D_k` (direct /30 to next hop, Step G) give the color. Fallback when btest unavailable: show ping/latency but still color by CPU/traceroute latency vs thresholds.

```html
<p><strong>Direct Path Analysis - {tower_site}</strong></p><!-- path-analyzer --><!-- Network Bottleneck Analyzer -->
<p><strong>Status: HEALTHY/DEGRADED</strong> — {reason} — {hops} hops to {fiber_router} ({fiber_ip})</p>
<p><strong>Path Analysis — {tower} → Internet:</strong></p>
<p>
<strong>Hop 1:</strong> iota pppoe (10.0.2.1)<br>
&nbsp;&nbsp;Network bandwidth: <span style=&quot;color:red&quot;><strong>56.1 Mbps down / 10.4 Mbps up</strong></span><br>
&nbsp;&nbsp;Single hop test: <span style=&quot;color:green&quot;>582 Mbps down / 560 Mbps up</span> (to next hop) — link healthy<br>
&nbsp;&nbsp;<span style=&quot;color:red&quot;><strong>→ ▼ bottleneck — network rx 10 Mbps (healthy single-hop proves radio link is fine, bottleneck is upstream)</strong></span><br><br>
<strong>Hop 2:</strong> kappa pppoe (10.0.3.1)<br>
&nbsp;&nbsp;Network bandwidth: <span style=&quot;color:red&quot;><strong>38.8 Mbps down / 9.4 Mbps up</strong></span><br>
&nbsp;&nbsp;Single hop test: <span style=&quot;color:orange&quot;>23.0 Mbps down / 18 Mbps up</span> (CPU-limited)<br>
&nbsp;&nbsp;<span style=&quot;color:red&quot;><strong>→ ▼ bottleneck — also degraded upstream (not this radio alone)</strong></span><br><br>
<strong>Hop 3:</strong> mu backhauls (10.0.4.1)<br>
&nbsp;&nbsp;Network bandwidth: <span style=&quot;color:green&quot;>420 Mbps down / 380 Mbps up</span><br>
&nbsp;&nbsp;Single hop test: <span style=&quot;color:green&quot;>645 Mbps down / 610 Mbps up</span> — healthy<br>
&nbsp;&nbsp;<span style=&quot;color:green&quot;>→ ✓ healthy</span><br><br>
<!-- repeat per hop: two lines per hop when both tests exist -->
<strong>Hop N:</strong> Eta Fiber (10.0.5.1)<br>
&nbsp;&nbsp;→ fiber router (no test — this is the destination)<br><br>
<strong>Hop N+1:</strong> edge-2216-a (198.51.100.42)<br>
&nbsp;&nbsp;→ internet edge<br><br>
</p>
<!-- only if wireless-health triggered -->
<p><strong>Wireless Link Health — {pair}:</strong></p>
<p><span style=&quot;color:red&quot;>→ CCQ 62% · signal -81 dBm · SNR 12 dB — link degraded</span><br>
&nbsp;&nbsp; radio rate 433/433 Mbps · jitter 2ms loss 0%</p>
<!-- ONLY if multipath detected (distinct gateways >1 per /ip route dst=0.0.0.0/0 active) -->
<!-- <p><strong>Multipath:</strong> load-balanced via 2 gateways 10.0.2.2 + 10.0.2.6</p> -->
```

**Per-hop coloring rules (apply to every `<strong>Hop N:</strong>` line, including intermediate hops — do not skip):**

| Hop condition | Bandwidth span color | Callout |
|---------------|----------------------|---------|
| `R_k` drop >50% vs `R_{k+1}` OR `R_k rx <20 Mbps` OR `local-cpu>80%` | `color:red` + `→ ▼ bottleneck` | red |
| `15<R_k rx <40 Mbps` warning OR `cpu 60-80%` OR variance `>40%` | `color:#FFA500` amber `→ ⚠` | amber |
| `R_k` healthy `>80 Mbps` and `cpu<60%` | `color:green` `→ ✓ healthy, R_k=…` | green |
| `D_k` CPU>90% → add `(D_k CPU-limited — generation bound)` note but still color by `R_k` |
| Fiber backbone hops (no `R_k`, `R_N` is itself): show `→ fiber router` (no bandwidth) but still use name+IP |
| If hop btest failed (fiber auth, timeout): show `<span style=&quot;color:orange&quot;>→ ⚠ btest to fiber auth-failed — check fiber btest port / credential</span>` but still try next hop |

- **Every hop format is two lines when both tests exist:**<br>
  `Network bandwidth: {R_k down} Mbps down / {R_k up} Mbps up` (full path through entire network to fiber — this determines bottleneck color)<br>
  `Single hop test: {D_k down} Mbps down / {D_k up} Mbps up` (PTP radio link only to next hop — validates if that single link is the cause)<br>
  Resolve name for **all** hops via `/system identity print` (SSH to ip) → fallback `LIBRENMS sysName` → `MyMate site_name`; **never** show bare `10.x.x.x` alone. Label lines exactly `Network bandwidth:` and `Single hop test:` — plain English, not `R_k`/`D_k`/`RΔ`.
- Each hop must have `Network bandwidth:` line when `R_k` exists; `Single hop test:` line when `D_k` exists; **never** `Hop N: X (IP) → hop` without bandwidth.
- Colors apply to `Network bandwidth:` line (determines red/yellow/green). `Single hop test:` line colored by its own result but caller notes if healthy while network is degraded → upstream bottleneck, not that radio.
- No preamble, no recommendations, no tables.

**Verification:** helper collapses newlines and verifies `Direct Path Analysis` present, not `BODYPLACEHOLDER` (`sonar_comment.py:183`). Preserve HTML comment `<!-- path-analyzer -->` for `correlated-issues` indexing (update that skill to also match `Direct Path Analysis`).

## Rain-Fade Readiness mode (Wave links)

Operator-invoked (or triggered by a per-storm ticket trail) mode for 60 GHz Wave links:
for every Wave link on the path, forecast the rain rate at which it dies (ITU-R P.838-3,
`scripts/rain_fade_model.py`), audit the 5 GHz backup / carrier-drop layer for the link's
class (Wave internal backup radio vs carrier-drop + OSPF reroute), run the Wave built-in
speed test only when the operator asks, and post one comment under header `Rain Fade Readiness - {site}` with marker
`<!-- rain-fade-readiness -->`. Load `wave-gui` for the radio reads. Full procedure:
`references/rain-fade-readiness.md`. Read-only + capacity tests only; never a bounce.

**Batch auto-trigger (AGENTS.md Step 3.5b):** after the Direct Path Analysis comment, when
the path contains a 60 GHz Wave hop whose BOTH mgmt URLs are known, the ticket trail cites
weather, and the ticket has no prior `Rain Fade Readiness` header/marker, run the one-command
orchestrator `python3 scripts/rain_fade_readiness.py --wave-ap <URL> --wave-st <URL>
--audit-config --speedtest` and post the readiness comment from its JSON. One readiness
comment per ticket, ever; script failure or missing credentials is a silent skip (note in
evidence, never an escalation on its own).

## Env

| Variable | Default | Purpose |
|----------|---------|---------|
| `LIBRENMS_URL` | unset (register with `scripts/resources.py`) | LibreNMS base |
| `LIBRENMS_TOKEN` | — | `X-Auth-Token` (source `.env`, never hardcoded) |
| `EDGE_ROUTER_IPS` | unset (example `10.0.6.1`) | Edge 2216 router(s) (+ the second edge when known) |
| `RDS2216_SUBNET` | unset (example `10.0.30.0/24`) | LibreQoS CGNAT range (tune) |
| `FIBER_MAP_FILE` | `site-pack/topology/fiber-map.json` | Fiber network → router IP |
| `BGP_RR_MAP_FILE` | `site-pack/topology/bgp-rr-map.json` | RR name → IP |
| `PATH_TOWER_THRESHOLD` | `75` | Tower confidence floor |
| `BTEST_DURATION` | `10` | Seconds per btest (baseline) |
| `BTEST_PROTOCOL` | `tcp` | tcp/udp |
| `BTEST_TIMEOUT_MS` | `60000` | Per-btest SSH timeout |
| `PATH_TRIAGE_MODE` | `direct` | immediate cutover |
| `RF_TIER_RED_MM_H` | `25` | rain-fade tier red threshold in mm/h (environment or `.env`) |
| `RF_TIER_AMBER_MM_H` | `50` | rain-fade tier amber threshold in mm/h (environment or `.env`) |

## Integration with AGENTS.md

Replaces the external-analyzer era entirely. Steps 3.6 `correlated-issues`, 3.6b `correlated-auto-return`, 3.7 `ihn-auto-return`, 3.8 `peak-scheduler` stay — they consume new hops but keep weather (`ticket-context:169`) absolute exclusion, peak 20:00-21:00 CT gate, tower confidence, and single-bounce anti-loop.

For `ihn-auto-return` Gate D `perfect HEALTHY` (status healthy + bottleneck_count 0 + all green) the definition maps to: no hop red/amber, no CPU>80, no util>80, multipath gateways balanced — then score tower vs `PATH_TOWER_THRESHOLD`.

Weather/peak: still scan `subject+description+ALL ticket_comments.body` (`ihn-auto-return: Gate B`) + `peak-scheduler` queue `logs/pending_peak_checks.json` (enqueue stores `analyzer_site`, `fiber_router_ip` too).

## References

- `site-pack/topology/verified-probes.md` — live traceroute/btest/bgp dumps (your install's own examples)
- `site-pack/topology/fiber-topology.md` — edge→fiber walk (schema example: `site-pack/example/fiber-topology.example.md`)
- `references/path-discovery.md` — traceroute fallback
- `references/btest-procedure.md` — params + CPU caveat
- `references/variance.md` — direct vs full-path comparison
