# Tracker Sheet — Notes standards (reference)

Moved out of `SKILL.md` §8. Read before writing any `notes` value. Section numbers elsewhere in the skill still refer to the numbering in `SKILL.md`.

## 8. Notes standards (with verification evidence)

Notes carry what the NOC tech verified, not just the complaint — but stay
terse: problem + down-since time + contact facts the operator actually gave.
Device-level detail (IPs, device counts, signal series) lives in the helper
audit log and the agent's report to the operator, never in the sheet note.
Same for coordinates: the sheet auto-pulls `Tower1 Coords` / `Tower2 Coords`
from the site selected in column B — NEVER write coordinates into notes.
Use them only to verify you picked the right site (§2 step 4). Never invent
contact instructions (`contact TH in AM`, `before dispatching`, etc.) — if
the operator gave no contact facts, notes carry no contact sentence at all.

- **Row notes = what the tech reads in the ticket description (operator spec, explicit):** the sheet automation copies the Notes cell into the real Sonar ticket
  description (`NOC's Notes: …` block). The notes should contain ONLY: **the current signals
  and capacity of the link** (e.g. `0 signal/capacity` when disconnected) + **the operator's
  own note incorporated as plain prose** + bare facts like `0 cx offline`. NO analysis prose,
  NO timelines, NO "suspected cause" editorializing, NO detailed link/timeline evidence —
  that lives ONLY in the §13 RSSI evidence chart comment on the Sonar ticket.
  **NO `(Prompt: …)` clause (operator, explicit):** do NOT quote the operator's
  message back in parentheses, ever — operator-given facts (contact instructions, swap plans,
  tuning context) are woven directly into the notes as normal sentences; the verification
  evidence the agent gathered (down-since, signals) already tells the rest, and the
  `(Prompt: …)` wrapper is redundant noise on every ticket.
  Worked correction: initial notes carried a full
  signal-slide timeline; final form was
  `60G link disconnected - 0 signal/capacity, 0 cx offline. Link looks like it was turned
  twice in September, 60ghz currently will not link at all.` — cell trimmed post-filing
  and the description's `NOC's Notes:` block spliced to match (regex replace up to
  `<br>|<br>TOWER1`). (This historical example predates the no-`(Prompt:…)` rule;
  today the same facts are written without the parenthetical wrapper.)
- **No Power:** `No power, sent sms to TH / Left VM / Will try to reach in the
  morning` + down-since time. Live example (worked row):
  `Site looks like no power - whole site offline since 7:21 PM CT. TH needs
  contacted in the AM.` — no IP list, no device count, no coordinates. The
  `TH needs contacted` sentence was the operator's fact for that ticket,
  not a default to reuse.
- **No Power wording rule (operator, explicit):** on No Power tickets NEVER
  write `power outage` / `site power outage` / similar in notes (sheet note,
  MyMate note, chat text). `No Power` means only that our devices at the site
  are dark — no power OBSERVED TO OUR DEVICES — and it does NOT imply the
  tower host's site power is actually out (the TH's power may be fine while a
  downlink/AP path, breaker, PoE, or cable is what killed our equipment).
  Phrase it factually instead: `no power observed to our devices`, `whole
  site dark (our equipment unreachable)`. The wording `power outage` or
  `grid outage` is reserved for when the TH was contacted and CONFIRMED the
  site itself is without power (or the row is filed as a Grid Outage
  alert/ticket backed by the utility). If the TH confirms site power loss,
  restate the fact as the TH's words, e.g. `TH confirmed site power out`.
- **All link tickets (upgrade, misaligned, any link Issue):** always pull live
  signals + reported capacity before filing — live UISP device detail
  (`/nms/api/v2.1/devices/{id}` overview: `signal` both ends,
  `downlinkCapacity`/`uplinkCapacity`) for UBNT, MyMate ping-samples for loss.
  Cite both-sides dBm + Mbps in notes; if a side is unreachable, say which and
  file with what you have (never guess).
- **Link Upgrade:** current signals both sides + reported capacity + swap plan
  + approver + side-work: `Operator-approved swap PB5ACs to AF5xHDs for rain fade
  (approver signed off). Current signals -55/-58 dBm, capacity ~210/195 Mbps,
  link up 0% loss. One side's backhaul router also needs hEX -> hEX S (ARM).`
  (worked row example).
- **Link Misaligned:** current signals both sides + tune attempt + LibreNMS
  capacity: `signals at -60/-60, already tried to tune but no luck, as per
  checking to libre link can do -57/-57`; `low capacity 300mbps, signals at
  -69/-70, already tried to tune but no luck`. Turned-link claim needs §9 (turned vs foliage).
- **No Ether:** `<device> on <port> of <router/switch>[, cable test result].
  3x cx offline/affected` (a count, then `x cx offline/affected`) — the cx count is REQUIRED in the notes (that is
  how dispatch reads customer impact); never leave it out, and never put it
  in the AP IP column instead (§3a.1). cx impact decides outage upgrade.
  `0 cx` must still be written when none are down.
- **Locked Up:** `<device> locked up on <port> of <router>, tried bump ether
  but no luck[, TH sms for breaker reset]. 4 cx in it.` (a count)
- **Ether 100mbps (negotiating-ethernet standard):**
  ALWAYS resolve the exact switch port before filing — never file `stuck at
  100m` without `on etherX of <site> sector switch hEX` in notes. Procedure:
  (1) MyMate devices index → problem device (`golf5e LAPGPS 5580/40`,
  10.0.82.97) + sector switch (`golf sector switch`,
  10.0.82.104, RB960PGS hEX PoE); (2) `GET
  /api/devices/{switch_id}/interfaces` — the 100-port is the one with
  `speed_mbps: 100` while siblings are 1000 (here ether4 `5e` = 100, ether2/3/5
  = 1000); description comment (`5e`) must match the device suffix — do NOT
  assume the port (operator correction: 5e is ether4, not ether5); (3) live SSH
  verify on the switch (`/interface ethernet print detail` +
  `/interface ethernet monitor <port> once` + `/interface ethernet poe print
  detail`): confirm `speed=100Mbps` / `rate: 100Mbps` and record
  `poe-out` mode. Notes format: `<device> stuck at 100m on <port> of
  <site> sector switch hEX, tried fixing but no luck.` Live example:
  `golf5e LAPGPS stuck at 100m on ether4 of golf sector switch hEX,
  tried fixing but no luck. Only takes power when poe is forced (ether4
  poe-out=forced-on).` Link-partner advertising 10/100-only (no 1000M) is the
  confirming signal — cite it in the operator report, not the sheet note.
- **Forced-PoE note (companion to any negotiating-ethernet ticket):** when the
  port only powers the radio with `poe-out=forced-on` (vs siblings on
  `auto-on`), ALWAYS append a second notes sentence: `Only takes power when
  poe is forced (<port> poe-out=forced-on).` This is a separate fact from the
  100m negotiation — include both, never fold them into one clause.
- **Port-comment stamp (negotiating-ethernet standard):** after the sheet automation backfills the Sonar `Ticket #`
  (`wait-for-ticket --row <n>`), SSH to the sector switch and APPEND
  `<M/D/YYYY> stuck at 100m, ticket #<n> -NocAgent` to that ether port's
  existing comment (keep the device label prefix, e.g. `5e - MM/DD/YYYY stuck
  at 100m, ticket <ticket-id> -NocAgent`), then read the port back to verify:
  `/interface ethernet set <port> comment="<existing> - <M/D/YYYY> stuck at
  100m, ticket #<n> -NocAgent"` + `/interface ethernet print detail where
  name=<port>`. Date is CT (`TZ=America/Chicago date +%m/%d/%Y`). This is an
  operator-authorized device write (like the temp-site workflow) — only for
  the verified 100m port, never bulk.
- **Flapping:** flap pattern (`intermittent flapping, sometimes less than a
  minute, sometimes up to 4 hrs`) + where loss shows.
