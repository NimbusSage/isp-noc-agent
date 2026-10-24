<!-- Purpose: step-by-step for operators to build the org's own NOC Ticket Tracker
sheet in Google Sheets, matching what scripts/tracker_sheet.mjs (tracker-sheet
skill) expects. Real deployments point .env at their own copy. -->

# NOC Ticket Tracker Sheet Template

The `tracker-sheet` skill files tickets by appending rows to a Google Sheet
tab named `NOC Ticket Tracker`. The helper is sheet-agnostic: it targets
whatever spreadsheet `TRACKER_SPREADSHEET_ID` points at and refuses to write
if the header row drifts from its expected signature (`checkHeaderSignature`
in `scripts/tracker_sheet.mjs`).

## 1. Create the spreadsheet

1. Create a Google Sheet owned by a user account on **your** Google Workspace
   domain (or a personal account you control).
2. Rename the first sheet tab to `NOC Ticket Tracker` (or set `TRACKER_TAB`
   in `.env` to match).
3. Optional: add a `Pick List` tab holding your canonical tower/site names
   (col I = tower names). The helper validates tower names against
   `TRACKER_PICK_TAB` (default `Pick List`). If you skip this, tower-name
   validation runs client-side only —  keep the tab so typos can't happen.
4. Copy the spreadsheet ID from its URL — the long path segment between
   `/d/` and `/edit` — and set in `.env`:
   ```bash
   TRACKER_SPREADSHEET_ID=<your-sheet-id>
   TRACKER_TAB="NOC Ticket Tracker"
   TRACKER_PICK_TAB="Pick List"
   ```

## 2. Header row (row 1)

Columns A–U are the live data columns the helper reads and writes. Exact
header text (order and casing both matter — the signature check is
index-based):

| Col | Header | Written by helper? |
|-----|--------|--------------------|
| A | `Status` | yes |
| B | `Tower 1 (AP)` | yes |
| C | `Tower 2 (ST)` | yes (link tickets only; blank otherwise) |
| D | `Distance (miles)` | **never — formula column** (see below) |
| E | `Town Name, and problem device` | yes |
| F | `Ticket #` | automation fills it after ticket creation |
| G | `Issue` | yes |
| H | `is cx AP` | yes (TRUE/FALSE) |
| I | `AP IP` | **never — deprecated for agent filing** |
| J | `Tracking IP` | yes — only for OFFLINE devices/sites (outages-chat signal) |
| K | `Towers Affected` | yes |
| L | `Called TH` | yes (TRUE/FALSE) |
| M | `Last Time Called` | yes (`h:MM AM/PM`) |
| N | `Texted TH` | yes (TRUE/FALSE) |
| O | `On-Call Dispatched` | yes (TRUE/FALSE) |
| P | `Tech Who Reached Out to TH` | yes (helper writes `NOC Agent` by default) |
| Q | `Notes` | yes |
| R | `Create Ticket` | yes — `TRUE` = ticket request for the sheet automation |
| S | `Priority` | yes (must match your Priority dropdown exactly) |
| T | `Send to Auto Tracker` | helper always writes FALSE (human path) |
| U | `Send Count` | yes — increment to trigger the N8N auto-tracker watcher |

Columns V–AT (the worked deployment uses 46 columns ≈ `A1:AT`) carry
template formulas and human-only helpers (`Tower1 Coords` / `Tower2 Coords`
lookups, dispatch decor column `Tech Who Reached Out to TH` used by the
dispatch flow, `Send Count` watchers, etc.). The helper only reads the
header row as far as its signature and appends into A–U; V onward is
pass-through. If you make V+ columns, replicate whatever your ticket
automation needs — nothing in this repo depends on exact names there.

> Customizing: keep the 12 anchor columns from `HEADER_SIGNATURE`
> (`Status`, `Tower 1 (AP)`, `Tower 2 (ST)`, `Distance (miles)`,
> `Town Name, and problem device`, `Ticket #`, `Issue`,
> `Tech Who Reached Out to TH`, `Notes`, `Create Ticket`, `Priority`,
> `Send to Auto Tracker`) at indexes 0,1,2,3,4,5,6,15,16,17,18,19 —
> renaming any of those requires editing `HEADER_SIGNATURE` and the
> `buildRow`/`CONTENT_CELLS` column map in `tracker_sheet.mjs` in the
> same commit.

## 3. `Create Ticket` column semantics

`Create Ticket=TRUE` means "create a real ticket from this row" — it is the
staging automation's trigger, not a status. The helper writes TRUE when you
ask it to create a ticket and FALSE when you ask for an alert-only row.
`Ticket #` is filled by your automation (not the agent), and the alert-on-dup
rule reuses an existing live ticket number there.

## 4. Dropdowns (Data validation)

- `Status` (A): at minimum `TH Contacted`, `TH Needs Contacted`,
  `Repair Needs Scheduled`, `Grid Outage`, `Resolved`, `Dispatched`
  (the helper treats exactly `Resolved` as closed/live-safe and retries
  conflicted appends with a `Dispatched` fallback matching your statuses).
- `Issue` (G): the v1 table in the skill (`No Power`, `Link Misaligned`,
  `No Ether`, `Locked Up`, `Ether Negotiation: 100mbps`,
  `Ether Port Flapping`, `Grid Outage`).
- `Priority` (S): `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` (or your org's labels —
  the helper validates against live dropdown values).
- Booleans (H, L, N, O, R, T): TRUE/FALSE checkboxes render as plain
  `TRUE`/`FALSE` strings.

## 5. Column D formula (`Distance (miles)`)

D is a helper formula column that never receives a direct write. With coords
in the V+ area, the worked deployment uses:

```
={ARRAYFORMULA(... )}   // see tracker_sheet.mjs buildRow comment + your template
```

Any formula works — the agent only needs D to stay untouched so its value
derives from the coords columns rather than a stale literal.

## 6. OAuth + service wiring

Tracking uses editable user-OAuth (external-domain sheet), not the same read
vault as other skills:

1. Run `scripts/google_user_auth.mjs` to mint an OAuth secret under
   `.secrets/` (never committed) with Sheets + Drive scopes.
2. Set `TRACKER_OAUTH_FILE=<path>` and `TRACKER_AUTH_MODE=user-oauth` in `.env`.
3. Verify:
   ```bash
   node scripts/tracker_sheet.mjs inspect    # header signature + row count, read-only
   node scripts/tracker_sheet.mjs --help     # all modes, incl. gated append + audit
   ```

## 7. Alternative: non-Google source of truth

`tracker_sheet.mjs` is the only boundary to Sheets. To swap the tracker for
another data source (Airtable, Postgres, your CRM via API), reimplement the
same commands (`inspect`, `append`, `find-open`, `mark-dispatched`, …)
against your backend and keep the row/appends contract identical — the
`tracker-sheet` skill and CI tests continue to pass against any backend that
returns compatible responses.
