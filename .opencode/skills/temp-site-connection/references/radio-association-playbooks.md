# Reaching the far-side radio and weak-signal association (reference)

Moved out of `SKILL.md`. The apply-time safety rules (safe-mode deadlines, stability pre-check, firmware match, literal restore) stay in `SKILL.md`.

## Reaching the far-side radio: temp IP + port forward (validated)

Once the temporary L2 exists but the far radio's subnet is unreachable from
the reachable side, the operator-validated pattern is:

1. **Temp IP:** assign `.1` of the far subnet locally first (a conflict
   usually just works; on the rare failure it is obvious immediately), ping
   the intended final temp IP (e.g. `.222`) from the bridge, and if free use
   that as the final temp address. Avoid known-used hosts (gateway, sectors,
   switch, target radio itself).
2. **Port forwards on the reachable-side router:** DSTNAT for each needed
   service (80/443/22) to the far radio's management IP, plus the SRCNAT
   return-path rule. Tag every rule with a `temp <site>` comment so cleanup
   is unambiguous. Export/paste the exact NAT lines into the incident record.
3. **Proof is login, not ping:** WebUI load or SSH banner through the
   forward. While inside, read-only first: far radio mode/clients/uptime/
   firmware — the firmware answer decides whether a downgrade rung exists.

## Weak-signal station association playbook (low-margin links, ~-80 dBm and worse)

A temp link near the usability floor (verified scan around -81 dBm) may not associate on the first attempt, or at all for a while. This is normal and recoverable — the agent is expected to work the problem patiently through the ladder below rather than declaring the path infeasible after one or two tries. Every rung is one change, then observe, then decide. Never stack two changes without observing in between.

**Recognize the two classic failure signatures in the station-side log:**
1. **Frequency-select loop:** the station repeatedly scans/selects the AP frequency over and over without ever associating. Indicates it hears the AP but cannot complete the handshake — usually interference or marginal signal, not wrong credentials.
2. **4-way handshake timeout:** association starts and dies at the 4-way handshake. Indicates encryption/key exchange failing over a lossy path — same ladder applies; do not "fix" it by changing keys or security settings unilaterally.

**Remediation ladder, in order (operator executes, agent reviews each result):**
1. **Interference check (read-only first):** list nearby sectors/links on the same or overlapping frequency from inventory and current device state. If a nearby radio shares/overlaps the temp-link frequency, change frequency **on the accessible side only** to a clean frequency that interferes with neither the temp link nor anything else in the environment. Record the original frequency as the restore value before changing.
2. **Full reboot of the associating radio:** power-cycle/reboot, then allow a full observation window (several minutes) — low-margin links often associate on their own after a clean boot while the operator watches the log.
3. **Firmware to the standard version (`8.5.12`) + reboot:** only with a saved config backup first, only to this vetted standard version (never an arbitrary newer/older build), then another full observation window.
4. **Wait and retry:** marginal links sometimes associate after an extended period with no changes at all. Bounded retries with growing observation windows beat rapid-fire re-attempts, which can keep a flapping link from ever settling.

**Agent discipline for this playbook:**
- One rung at a time; read back state and the relevant log tail after every attempt before suggesting the next.
- Never run airView/spectrum/frequency scans on either link radio as a diagnostic — they disconnect clients/stations and can destroy a half-established link. Interference is assessed from inventory + the station's own passive scan results.
- Never change SSID/BSSID/key/security, and never touch the far-side radio's frequency (it may be unreachable mid-test and unrecoverable).
- Time-box the loop with the operator up front (e.g. max rungs, max elapsed); if the ladder is exhausted, restore the donor AP to its saved config, verify its customers recover, and report the link infeasible with the full log evidence — do not keep the donor interrupted indefinitely.
- Log every attempt (timestamp, rung, before/after state, outcome) so a human can see the patience was methodical, not thrashing.

**Failure threshold and next-AP fallback:** agree the give-up criteria BEFORE starting the ladder (e.g. N completed rungs with zero association, or T minutes with no forward progress). When the threshold is hit: restore the donor, verify its recovery, record the infeasible pair with log evidence — then, if an alternative candidate AP exists from discovery, restart the protocol at step 1 for that pair. A failed pair is data, not a failed incident.
