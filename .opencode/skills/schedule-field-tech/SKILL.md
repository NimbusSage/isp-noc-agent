---
name: schedule-field-tech
description: Use when an operator asks to schedule/dispatch a field tech from a Sonar ticket — "schedule Tech One on ticket <ticket-id>", "put a tech appointment on the calendar for this ticket", "book the tower host visit" — or asks to flip the tracker row to Dispatched after scheduling.
---

# Schedule Field Tech — Sonar Ticket ➜ Google Calendar Appointment

Maps a Sonar ticket onto a Google Calendar appointment for a field tech via the
gated `scripts/schedule_dispatch.mjs` helper. All work runs through the helper;
never hand-create calendar events for dispatch.

## Mapping (operator rules)

- **Title** = the ticket subject (as-is; the helper trims HTML only).
- **Location** = site coordinates for the dispatch. First source: parsed from
  the ticket subject/description (`... Grain Leg at 120ft at 45.000, -93.000`
  — `parseTicketCoords` finds it and validates ranges). If the ticket carries
  no coordinates, resolve them explicitly — repair tickets usually exist as a
  row in the NOC Ticket Tracker sheet (coords columns, columns Z/AA) — or from
  Sonar `network_sites` geopoint (same rule as `tracker-sheet` §2), then pass
  `--lat <lat> --lon <lon>`. NEVER let the helper guess.
- **Description** = compact, MOBILE-FIRST plain text (techs read these on
  phones — operator rules):
  1. quick line: priority emoji (LOW 🟢 / MEDIUM 🟡 / HIGH 🟠 / CRITICAL 🔴)
     + `🧰 Tech: <name>`;
  2. bare ticket link on its own line (`🔗 <url>`) — NO ticket id outside the
     URL, NO status, NO created-stamp;
  3. `📍 <site from subject> — <lat>, <lon>`;
  4. `——— Ticket details ———` with Sonar's standalone `|` separator lines
     re-rendered as BLANK lines and repeated blanks squeezed;
  5. `——— Updates from the ticket ———` with each comment as a blank line, a
     `🕐 <time in CT> — <user>` stamp, then the note text (CT, never UTC).
- **Event time** = operator-specified, else next business weekday 08:00 CT for
  60 min. Pass `--start YYYY-MM-DDTHH:MM --end YYYY-MM-DDTHH:MM` (wall clock in
  America/Chicago) when the operator gives a time ("noon today" = `--start
  T12:00 --end T13:00`).

## Tech calendar strategy (operator caveat honored)

The consenting Google account (the dispatcher's own account) holds the OAuth
grant: writer/owner access is what makes direct inserts possible, and when a
fallback event is needed it must live under the BLANKET ACCOUNT
`towerdispatch@example.com` (`CALENDAR_FALLBACK_CALENDAR_ID`, shared with
the consented account as writer) — NOT the consenting account's own primary.
Verified: a `create` into that calendar produced
`organizer: towerdispatch@example.com` with the tech as guest, so guest
invites come from the Tower Dispatch address. Direct write to a tech's
calendar exists only where that account holds owner/writer access.

1. **Resolve the tech's calendar**: `site-pack/tech_calendars.json` override map
   first (normalized exact-name keys), else the consenting account's
   calendarList matched by EXACT normalized display name (`summary` /
   `summaryOverride`). Fuzzy matches, zero matches, or same-rank ambiguity all
   fail loudly — never guess a tech from a partial name.
2. **Attempt DIRECT insert** on the tech's calendar when access is owner/writer.
3. **Fallback**: on 401/403/404 (not allowed), or when the shared calendar is
   read-only, insert on the blanket Tower Dispatch calendar
   (`CALENDAR_FALLBACK_CALENDAR_ID`, default in `.env:
   towerdispatch@example.com`) with the tech added as
   an **attendee guest** (`tech_calendars.json` attendee, the user-email-shaped
   calendarList id, or `--attendee`). The guest email must be known — the
   helper refuses a guest-less fallback rather than mailing the wrong address.
   Organizer-side self-invites send no email; DIFFERENT guests always do.

## Helper contract

```bash
set -a; . ./.env; set +a
node scripts/schedule_dispatch.mjs oauth-url                       # one-time consent (calendar scopes)
node scripts/schedule_dispatch.mjs oauth-bootstrap --code <CODE>   # one-time token storage (0600 file)
node scripts/schedule_dispatch.mjs resolve --ticket <ticket-id> --tech "Tech One"   # read-only plan
node scripts/schedule_dispatch.mjs create --ticket <ticket-id> --tech "Tech One" \
  --start T12:00 --end T13:00 --confirm-create true
node scripts/schedule_dispatch.mjs sonar-open --ticket <ticket-id> --confirm-sonar true
node scripts/schedule_dispatch.mjs delete-event --ticket <ticket-id> --confirm-delete true  # remove own test/erroneous event
node scripts/schedule_dispatch.mjs mark-dispatched --ticket <ticket-id> --confirm-write true
```

- `resolve` is read-only and prints the full plan: ticket, coords, window,
  resolved tech calendar (`mode: direct|fallback`, accessRole), the built event
  body, and duplicate search results. Run it first, show the plan, then create.
- **Gates for `create` (ALL required):** `CALENDAR_WRITE_ENABLED=true` +
  `--confirm-create true`. Kill-switch flip back to `false` when disabled per
  `docs/permissions-policy.md`.
- **Gates for `mark-dispatched` (ALL required):** `TRACKER_ACCESS=write` +
  `TRACKER_WRITE_ENABLED=true` + `--confirm-write true`. Note this flips a
  lifecycle status (`Dispatched`) the tracker helper itself refuses to file —
  it is legal here only as the operator-directed AFTERMATH of a real scheduled
  appointment, on the ticket's own `Ticket #` row (col F).
- **Dedup:** every event carries `extendedProperties.private.sonar_ticket_id`.
  `create` refuses while an event with that key exists on the target or
  fallback calendar (single dispatch per ticket; `--allow-dup` is an explicit
  override that should almost certainly be a conversation, not a flag).
- **Verification:** after insert the helper GETs the event back and checks
  summary/location/ticket key; mismatch = error, not success.
- **Audit:** every resolve/create/mark action appends to
  `logs/schedule_dispatch_audit.jsonl`.
- **Delete:** `delete-event` is destructive and DUAL-GATED
  (`CALENDAR_WRITE_ENABLED=true` + `--confirm-delete true`); it touches ONLY
  events carrying the ticket's dedup key, then verifies the event reads back
  as `status: "cancelled"` (Google does NOT 404 deletes — verified live)
  or 404 before reporting success.
- **Invites:** operator rule: appointment guests ALWAYS get the
  notification — the helper default is `DISPATCH_NOTIFY=all` and the agent
  passes `--notify all` unless the operator explicitly says a run is a quiet
  test. Never silently skip the guest email to save noise.

## Sonar teardown on dispatch (the dispatch manager's standard)

Once the appointment is created, the dispatched Sonar ticket flips
`PENDING_INTERNAL → OPEN` — and NO comment is posted at scheduling time. The
comment only comes later, when the tech has done the job and has notes; the
scheduler must never write a placeholder or "scheduled" comment-as-find.
Context: NOC typically dispatches ONLY night shifts and weekends, when the
dispatch manager is off the clock — so the flip preserves the dispatch
manager's standard without double-dispatching their work.

```bash
node scripts/schedule_dispatch.mjs sonar-open --ticket <id> --confirm-sonar true
```

- **Gates (ALL required):** `DISPATCH_SONAR_ENABLED=true` + `--confirm-sonar true`.
- **Transition guard:** the helper first reads the ticket's live status. Only
  `PENDING_INTERNAL → OPEN` mutates; already-`OPEN` is a safe no-op; any other
  status (CLOSED, PENDING_EXTERNAL, …) is refused loudly — never a silent
  status rewrite of a ticket in a state the standard doesn't cover.
- **Mutation verification:** the `updateTicket` response must carry the ticket
  id and `status: OPEN`, or the helper fails closed (per `sonar-api`
  verification rule). Audit entry `sonar-open` per action.
- No comment, ever, from this workflow: `sonar-open`, `create`, and
  `mark-dispatched` skill steps write NOTHING into the Sonar ticket body.

## Tech names vs emails (ask by name)

Operators dispatch by REGULAR NAME — no emails needed in the request. The
chain is fully name-driven: bare-name match against the decorated calendarList
("*Tech One | <ST>"), and the guest email auto-derives from the entry's user-
email-shaped id (`techone@example.com`). Only when a calendar's id
cannot double as the guest address (group-style calendar ids) does the tech
map file carry an `attendee` email — fill it ONCE in `site-pack/tech_calendars.json`,
then keep dispatching by name forever. An explicit `--attendee` always wins.

## Tech map file (optional, untracked)

`site-pack/tech_calendars.json` (schema example: `site-pack/example/tech_calendars.example.json`; when the file is absent the helper resolves by calendarList name only) — `{ "Display Name": { "calendar_id": "...",
"attendee": "tech@..." } }`. Load is normalizing/exact: keys are matched after
whitespace/case collapse. Use it when the display name in the calendarList is
not the operator's name for the tech, or when the guest email must differ from
the shared calendar's id.

Real calendarList display names are decorated (dozens of entries like
`*Tech One | <ST>`, `3Tech Two | <ST>`, `Tech Three - Hotel <ST>`,
`Tech Four | L2 | Hotel, <ST>`). The helper strips leading marks/digits
and the trailing `| location` / `- location` segment (`techNameFromSummary`)
and matches the BARE tech name exactly — "Tech One" matches
`*Tech One | <ST>`. Same-name duplicates with different access ranks resolve
deterministically to the higher rank (owner > writer); same-rank ambiguity
still fails loudly (e.g. a ticket resolved `*Tech One | <ST>`
`techone@example.com` writer → DIRECT insert, no fallback needed).

## Env

| Var | Meaning |
|-----|---------|
| `CALENDAR_OAUTH_FILE` | owner-only user-OAuth secret outside the repo (defaults to `TRACKER_OAUTH_FILE`) |
| `CALENDAR_AUTH_MODE` | `user-oauth` only |
| `CALENDAR_WRITE_ENABLED` | `true` enables appointment creation (kill switch) |
| `CALENDAR_FALLBACK_CALENDAR_ID` | tech guest fallback calendar (default `primary`) |
| `CALENDAR_TECH_MAP_FILE` | optional tech map override path |
| `DISPATCH_TIMEZONE` / `DISPATCH_DEFAULT_START_HOUR` / `DISPATCH_DEFAULT_DURATION_MINUTES` | default window (America/Chicago / 8 / 60) |
| `DISPATCH_NOTIFY` | Google Calendar `sendUpdates` — `all` per operator rule (guests always get the invite email unless a run is explicitly a quiet test) |
| `DISPATCH_SONAR_ENABLED` | `true` enables the gated `sonar-open` flip (`PENDING_INTERNAL → OPEN` on dispatch, NO comment) |
| `SONAR_TOKEN` | Sonar GraphQL fetch |

## Workflow (agent)

1. Load this skill; take ticket id + tech name + (optional) time from the
   operator request. Time missing → default window (next business weekday
   08:00 CT) and SAY SO in the report.
2. Run `resolve` and review the printed plan: correct ticket, sensible coords
   (within ~0.001° if cross-checking the tracker row), right tech calendar, no
   duplicates. Any surprise → stop and confirm with the operator.
3. Run `create` with gates. Report `mode` (`direct` vs `fallback` + which
   calendar), event link, and time.
4. Run `sonar-open` — standard teardown per the dispatch-lead convention:
   `PENDING_INTERNAL → OPEN`, verified, NO comment added. If the ticket is not
   PENDING_INTERNAL (already CLOSED, PENDING_EXTERNAL…), report the refusal
   instead of forcing it.
5. Run `mark-dispatched` only if the operator asked for the tracker flip (or it
   is standing SOP) — and say the row number.
6. Google Chat announcements for dispatch events are out of scope for v1; the
   audit log + Sonar/tracker state carry the record.

Tests: `node --test scripts/schedule_dispatch.test.mjs` (offline, mocked
fetch — proves gates, direct-vs-fallback, dedup refusal, and readback
verification without touching Sonar/Google/the tracker sheet).

## Live validation

First live run, all steps verified:

1. `resolve` printed the full plan; then `create` inserted DIRECT on the
   tech's own calendar (accessRole `writer`), 11:00–11:30 CT, readback
   verified (summary/location/ticket key). No attendee guest on direct writes
   (needs none).
2. Re-running `resolve` AFTER create found the event via the
   privateExtendedProperty key → re-create would now be refused (dedup works
   live).
3. `mark-dispatched` flipped the tracker row (`Ticket #` col F = the ticket id)
   to `Dispatched` with readback.

Fallback path live-validated: a tech resolved with reader access
(`techtwo@example.com`) → helper took the fallback branch WITHOUT attempting
a doomed direct insert, inserted on the Tower Dispatch `primary` with explicit
`--attendee techthree@example.com` as guest. The wall-of-text first draft was
deleted (`delete-event`) and recreated with the mobile-first description; the
new-format event now carries the guest + priority emoji + CT-stamped
comments. One retest ran INCLUDING a brief duplicate (event deleted → Google
returns `status: "cancelled"`, NOT a 404 — the delete-readback accepts both).

A creator-side self-invite (Tower Dispatch account as BOTH organizer on its
own `primary` and attendee) sends NO email — Google does not notify the
organizer of their own event; only DIFFERENT guests get invites.

Live-verified API quirks baked into the helper:

- `privateExtendedProperty` dedup queries must be `key=value`
  (`sonar_ticket_id=<ticket-id>`); the colon form 400s with
  `incompleteExtendedPropertyMatch`.
- Calendar List + event insert 403 until the **Google Calendar API** is
  enabled on the OAuth client's GCP project (one-time console click).
- Sheets `values.update` echoes data only with `includeValuesInResponse=true`;
  the readback expects `updatedData.values[0][0]`.
- The Sheets base URL is already `…/v4/spreadsheets` — never double the
  segment (a `/spreadsheets/spreadsheets/<id>/…` path 404s as HTML).
- A calendar-scope-only consent REPLACED the shared OAuth file's Sheets grant
  once (token-file union rule): when re-consenting the shared file, request
  `oauth-url --scope all` so Sheets scopes survive.
