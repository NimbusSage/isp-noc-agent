# Tracker Sheet — Sonar link, MyMate device notes, MyMate ticket link (reference)

Moved out of `SKILL.md` §12. Standard follow-through for every ticket row after `wait-for-ticket --row <n>` returns the Sonar `Ticket #`. Section numbers elsewhere in the skill still refer to the numbering in `SKILL.md`.

## 12. Sonar link + MyMate device notes + MyMate ticket link (standard — every ticket row)

After the sheet row sends and the Sonar `Ticket #` comes back
(`wait-for-ticket --row <n>`), ALWAYS finish the job in Sonar + MyMate.
This is standard operating procedure for every ticket row — not just
sites staying offline. Anything red/down in MyMate, or causing slowness
in the network, gets a dated MyMate device note + a MyMate ticket link
so the map shows what is broken and which ticket owns it.

1. **Link the Sonar ticket** (ticket rows only — alert-only rows have no
   ticket to link):
   - Site-wide issue (whole site down): link to the site —
     `linkEntityToTicket(id: <ticket_id>, input: { linked_entityable_type:
     NetworkSite, linked_entityable_id: <site_id> })`. Resolve the site by
     EXACT normalized name (`network_sites` search + post-filter; `Alpha`
     ≠ `North Alpha`). Ambiguous → ask, never guess. Verify with the
     ticket's `network_sites` connection. (Live-proven: ticket ↔ site link
     verified end-to-end once.) `ticketable_*` stays null — the link lives on
     the connection, not the ticketable fields.)
   - Single-device issue: link to the problem device's InventoryItem the same
     way (`linked_entityable_type: InventoryItem`) after an exact `NAME`
     inventory match per `sonar-api`. No exact single match → attach to the
     device's site instead and say so.
2. **Note the problem device(s) in MyMate — EVERY ticket row** (geomaps is
   canonical — odd diffs vs regular maps exist, but the notes API is
   map-agnostic). Target rule (operator SOP):
   - Anything red/down in MyMate, or causing slowness (no-ether, locked-up,
     100m/flapping port, misaligned/degraded link, slow-path hop) → note
     the DIRECT device in question (the problem radio/port/link endpoint).
   - Site-wide power (whole site dark, router + sectors + switch all
     `unreachable`) → note EACH down device at the site (the open-outage
     device list), so every red icon carries the ticket.
   - `GET /api/devices/{id}/notes` first — skip devices that already carry
     this ticket's note (idempotent).
   - `POST /api/devices/{id}/notes` `{ "body": "<note>" }` on each target
     device (the direct problem device, or each down device for site-wide
     power).
   - Format (exact, operator-verified): `{M/D/YYYY} {issue-short},
     {contact-note}, ticket #{n} -NOC_Agent` — date is the outage start in
     CT, e.g. `MM/DD/YYYY no power, contact TH in AM, ticket #<ticket-id>
     -NOC_Agent`. Contact-note follows the ticket facts (`contact TH in AM`
     past 9pm, `SMS sent` / `TH contacted` / `Left VM` otherwise).
   - **Signature rule (operator, explicit):** EVERY MyMate device note
     written by the agent ends with ` -NOC_Agent` (space, dash, `NOC_Agent`)
     — directly after the ticket reference on ticket rows, or after the
     final fact on alert-only notes. MyMate does store the creating user
     (`author_name`), but the signature in the note body makes attribution
     readable at a glance in the geomap right panel — never omit it, on
     ticket rows and alert-only rows alike.
   - Removal (only for own mistakes): `DELETE /api/notes/{noteId}`.
   - All three routes verified live. Device notes are a
     sub-resource — there is no writable `device_note` field on the device
     record (a PATCH with one is silently ignored).
3. **Link the Sonar ticket in MyMate — EVERY ticket row, exactly ONCE**
   (GeoMap ticket symbol — this is separate from device notes; notes are
   per-device text, the link is what renders the symbol over the site/device
   while the ticket is open). One ticket = one link, never both ends of a
   link or every noted device. Target rule (operator SOP): direct device
   for device/slowness issues, site for site-wide power. Whole-link
   (two-site) tickets:
   - The link target is the DEVICE THE OPERATOR NAMED AS THE PROBLEM
     DEVICE — only for genuinely single-device issues — never a side
     deduced from MyMate naming perspective. The operator's side-label is
     authoritative even when the device record carries no role suffix.
     Worked example: operator named the problem device
     `Foxtrot TGM 2 Foxtrot WT ST WaveNano` (MyMate
     `foxtrotTGM2foxtrotWT Wave Nano`, hanging off the Foxtrot TGM
     router's ether2) — NOT the other end (`FoxtrotWT2FoxtrotTGM Wave Nano`)
     even though a perspective-based guess calls that one the ST; the wrong
     link was removed (`DELETE /api/sonar-tickets/<link-id>`) and re-posted to
     the named device.
   - **Full-link tickets ALWAYS link the STATION (ST)-side device, never
     the AP/feeding side** (operator rule). When the
     whole link is the problem (misaligned, chain imbalance, low capacity,
     downgraded, flapped/degraded link) — even if the operator's message
     names a side (e.g. "Juliet 2 Kilo AP") — the ticket symbol goes
     on the station/slave radio only. Reason: link problems show their
     symptoms on the ST side and downstream of it (customers hang off the
     ST path), so the map must flag that end. Worked example: a ticket
     linked to the ST device only. Worked correction: a ticket
     was first linked to the AP-side device because the operator
     named "Juliet 2 Kilo AP"; under this rule the link was moved to
     the ST device (`kilo2mikeST ISO 5545`) — the named side still
     drives `town_device` and the device note, but not the link target.
   - Exception: if the **AP-side** device is offline/dead/red in MyMate,
     that is an AP/AP-site issue, NOT a link-quality ticket — link the AP
     device instead and flag in the operator report. This does not override
     the ST rule above: it handles a dead-AP case where there is no link
     problem to chase on the ST side.
   - `GET /api/devices/{id}/sonar-tickets` (or
     `GET /api/sites/{site_id}/sonar-tickets` for site-wide) first — skip
     if this ticket is already linked (idempotent).
   - `POST /api/devices/{id}/sonar-tickets` (or
     `POST /api/sites/{site_id}/sonar-tickets`) `{ "reference": "<ticket#>" }`
     (accepts id, `#id`, or Sonar URL), then re-read the list to verify.
     Same shape works for links (`/api/links/{id}/sonar-tickets`) when the
     ticket is a link issue. Worked example: device
     `lima5s LAPGPS` link ↔ ticket `<ticket-id>` + note
     (`MM/DD/YYYY no ether, repair needs scheduled, ticket <ticket-id>`).
   - Refresh/unlink (own mistakes only): `POST
     /api/sonar-tickets/{linkId}/refresh`, `DELETE
     /api/sonar-tickets/{linkId}`. See `mymate` skill (a CLOSED ticket's old link stays as history).

**Alert-only rows skip the Sonar link + MyMate ticket link** (no ticket # to
link, no symbol to render) — including duplicate alert-only rows that
carry an existing Ticket # (§11 rule 2): that ticket's links/notes already
exist from its original filing, so this path never re-posts them. But **alert-only GRID OUTAGE rows still get the
standard MyMate device notes** (operator SOP — worked example:
 4 down devices, one note each): on filing an alert-only
grid-outage row, note EVERY device that is down in MyMate because of the
grid outage (the site's open-outage device list), so every red icon carries
the record. Notes are idempotent (`GET /api/devices/{id}/notes` first, skip
devices already carrying this outage's note). Format — same shape as the
ticket-note standard but with the ticket reference explicitly disclaimed
(no ticket exists, so never leave the `#{n}` slot looking open):

```
{M/D/YYYY} grid outage, alert only — no ticket, ticket # —
```

Every alert-only note still ends with the standard ` -NOC_Agent` suffix
(signature rule §12.2). Live example: `MM/DD/YYYY grid
outage, alert only — no ticket, ticket # —` on each of the 4 down devices at
the site. Down-date is the outage start in CT. Do NOT POST a sonar-tickets
link for alert-only rows.

Non-grid alert-only rows (planned/prearranged power events, anything else the
operator said should just alert) have NO standing note rule — ask the operator
before writing notes for those.
