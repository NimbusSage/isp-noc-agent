# Link-already-up fast path and Temp OSPF turn-up (reference)

Moved out of `SKILL.md`. The SKILL.md summary of the four turn-up rungs is binding; this file holds the proofs, traps and worked detail.

## Link-already-up fast path: check for an existing temp association first

Before planning any flip, check whether a previous temp link is still in
place. A donor left in `station-bridge` against the island AP means the L2
already exists and the ONLY remaining work is the temp OSPF `/30` — no
customer interruption at all. (First seen live: a donor omni `omega24o` was
still `station-bridge → ssid sigma24o` from an earlier incident, live
signal -60 dBm, while every routed path was dark.)

1. **Oxidized archaeology (read-only HTTP, no MCP needed):**
   `GET http://10.0.0.6:8888/node/fetch/<donor-ip>` and `/fetch/<island-ip>`.
   Donor `mode=station-bridge` + `ssid=<island-ssid>` against island
   `mode=ap-bridge` + same `ssid` = the pair. The donor backup is live
   (device up); the island backup is pre-outage but its ssid/mode stands.
2. **Live corroboration:** donor MyMate `signal_dbm` healthy (e.g. -60) with
   `wireless_clients >= 1` = associated NOW. Signal symmetry (donor -60 vs
   island last-reported -62) confirms the two ends see each other. Definitive
   proof is the donor registration table (`wireless-registration-table` via
   `mikrotik-ssh` once MCP is up) — peer MAC/hostname must be the island radio.
3. **If confirmed:** skip the entire flip sequence (no Phase 1/2, no donor
   interruption, no reboot-first — the link is production). Go straight to
   temp OSPF addressing below. Record `L2 pre-existing; donor flip skipped`
   with the evidence (backup lines + signal + registration peer).
4. **Donor-restore note:** the donor is NOT in its native role. Its native AP
   ssid is unverified (presumed own-ssid, e.g. `omega24o`) — mark SUSPECT per
   the leftover-ssid rule and resolve it before permanent-repair cleanup
   (UISP/Oxidized history or operator memory). Snapshot the current
   station config NOW so undo never depends on reconstruction.

## Temp OSPF over an existing L2 (near-side apply + far-side mac-telnet)

### Default next step once L2 is proven: run both OSPF ends NOW (operator-redirected)

**The recurring failure mode:** after the donor flip + L2 proof is done, drifting into island-router/uplink
forensics (route dumps, remote-end radio diagnosis) instead of turning up the OSPF adjacency — the operator had to
redirection-grade the step: "next step is mac-telnet from the near router into the island router, to setup OSPF
between them". L2-proven ⇒ your next action is ALWAYS the four rungs below, in this order. Root-cause work on the
broken upstream happens AFTER island service is restored, never before.

1. **Reserve the /30 (or curated with live conflict checks):** `temp_ospf_sheet.mjs plan-reservation` → `mark-in-use`
   (free pair from `find-free`); if the Sheet is blocked, curated pair + both routers checked (`/ip address print
   where address~"10.0.70"`, `/ip route print where dst-address~"10.0.70"`).
2. **NEAR side first — typed executor, never raw SSH:** `place /30 host-A on the facing interface (bridge port
   check per rung 6 in the far-leg ladder below)`; run
   `node mcp-server/dist/tool-cli.js temp-ospf-plan '{...}'` then `temp-ospf-apply` with
   `execute:true` + the pinned binding. `ospfType`: bridge side ⇒ `broadcast` (hard rule below).
   Executor self-verifies (address + adjacency present).
3. **FAR side (island router) — mac-telnet from the near router is the ONLY way to deliver it:** island SSH is a
   chicken/egg dead-end until the island has a default route — do NOT even try (`mt_mactelnet.mjs --jump <near>
   --mac <island-MAC> --write --incident-id <id>`; MAC = island bridge MAC from near-router MNDP). Push the
   **executor-identical** commands (address host-B + interface-template with area/type/cost/timers/priority +
   incident tag), then read back BOTH lines in the same session. Island RouterOS may hang prints shorter than the
   pty height — use `print without-paging` on wide tables.
4. **Verify in this order:** `/routing ospf neighbor print` (expect host-A ↔ host-B `Full`; bridge type elects
   DR/BDR — that is normal) → island default route restored (`/ip route print where dst-address=0.0.0.0/0`, expect
   OSPF 110 via host-A) → outbound ping from the island (/30 src, e.g. 1.1.1.1) → iBGP auto re-formation
   (`/routing bgp session print without-paging` on the island: E + prefix-count rising — make ZERO BGP changes)
   → customer round-trip from the agent host (ping the island CX PPPoE gateway addrs). Only then report.

**The far leg itself is done from the near router via mac-telnet — this is the primary method, proven live.** Jump-SSH to the near router (pool-pinned exact-device binding, password at runtime via `SSHPASS`, never
printed/logged, one batched connection, temp tooling shredded after), then `/tool mac-telnet <far-router-MAC>` (MAC
from `/ip neighbor print detail` over the same session — MNDP reveals identity/version/uptime even with no L3). The
donor station path works too when the near router has no L2 sightline, but the near router is first choice: it is
where the `/30` lives and where adjacency is verified.

1. **L2-unicast proof FIRST — MNDP neighbor is NOT a data path.** MNDP/CDP
   multicast crosses even when all unicast is dead (example: both island
   devices visible via MNDP, 100% unicast loss). Required proof, in order:
   (a) station registration shows the island AP, (b) forced-L2 ping
   `/ping <island-AP-IP> interface=bridge1 count=3` from the station
   succeeds — but treat a radio self-ping failure as WEAK evidence.
   **Traps:** station-sourced pings follow the station's default route
   (`pppoe-out1`), NOT wlan1 — without `interface=bridge1` the test is
   invalid. AND even with `interface=bridge1`, a radio with no bridge IP and
   `wds=no` may fail its own forced-L2 ping (incomplete ARP) while the
   end-to-end router path works fine (example: radio self-ping
   100% loss, yet OSPF Full + router pings 3/3 over the same L2). The strong
   proof is router-sourced: near-router /30 ping + `Full` adjacency. Run the
   WDS ladder only after a router-sourced test fails — never on a failed
   radio self-ping alone.
2. **WDS rule (decides whether OSPF can ever form) — but check placement
   FIRST.** Near-router OSPF Hellos must exit the facing interface: if the
   `/30` sits on the wrong interface (example: `bridge1` instead of standalone
   `ether1`), Hellos never leave and every unicast test fails EXACTLY like a
   dead data path — including a misleading `wds=no` on the station. Only once
   placement is verified (adjacency attempt + bridge-port proof) does `wds=no`
   + no dynamic wds interface + forced-L2 ping loss mean a genuine L2 fault
   for the WDS ladder below. `ap=yes` + signal alone never prove the path.
3. **WDS remediation ladder (one rung, observe, decide):**
   (a) `temp-mt-bounce` the station (fresh association; binding required);
   (b) pin `wireless-protocol` to the AP's actual protocol (example: AP is
   `nstreme`, station was `any` — pinning changed nothing, reverted);
   (c) AP-side toggle/reboot via mac-telnet from the station IF mac-telnet
   itself connects (it needs only station-own unicast);
   (d) if `wds=no` persists: AP-side forwarding fault, unreachable — stop,
   call the tower host for a power-cycle (contact in the `pppoe` device
   name) or dispatch. Never thrash repeated flips against a steady `wds=no`.
4. **Far-leg OSPF (only after proof):** run the four-rung turn-up above —
   the validated `/30` host-B + `interface-template` (cost `1000`,
   timers/auth copied) pushed over the mac-telnet session. Read back both
   lines in-session, exit, then verify `Full` from the near side. Rollback
   commands from `temp-ospf-plan` on any miss.
5. **Leave the radios alone** once the data path works. No frequency touch
   on a working temp L2. Record the station's literal before-state (full
   `/interface wireless print detail`) before ANY radio write; revert
   restores it verbatim.
6. **Addressing specifics:** lowest free pair from `find-free`; conflict-check
   BOTH routers (`/ip address print where address~"10.0.70"`,
   `/ip route print where dst-address~"10.0.70"` — near side live, far side
   over mac-telnet); `mark-in-use` before apply (Sheet 429 → `operator-
   curated; Sheet not checked` + double checks). Near host-A goes on the
   interface that actually faces the donor radio's ethernet path — DETERMINE
   with `/interface bridge port print` FIRST, never assume: bridge member →
   the `/30` goes on that bridge; empty/missing port table with a standalone
   ether → the `/30` goes on the ether itself. One run placed it on `bridge1`
   while the radio path hangs off standalone `ether1`: Hellos never left the
   router, zero adjacency, and the unicast failures were misdiagnosed as a
   WDS fault. Correct placement formed `Full` immediately — test every
   placement by adjacency forming.
   Copy only timers/auth/priority/type from existing PTP templates, cost
   `1000` always (never the mesh `10`). Tag everything
   `TEMP <incident-id> REMOVE-WHEN-REPAIRED`.
