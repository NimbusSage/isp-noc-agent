---
name: create-site
description: Use when a NEW site (visit location — tower, fiber cabinet, hub) must exist in the operator's external records — the Tower Directory sheet (Master Tower List), a Sonar network_site, a UISP site, or a MyMate site — including the verified API shapes, address/comma conventions, coordinate source-of-truth rules, and alphabetical row insertion. NOT for device bring-up (program-new-device) or renames (rename-site); this skill runs whenever a site pin exists but its records do not.
---

# Create Site (Tower Directory + Sonar + UISP + MyMate)

A "new site" is a pin — from an official tower map, a new fiber cabinet, or a
bring-up — that is missing from one or more of the four site records. This
skill creates them in the right order with the verified shapes (validated
live end-to-end on a new fiber-cabinet site).

## Order of operations (Sonar first — its id feeds everything else)

1. **Coordinates** — source of truth is the **official Google My Map** KMZ
   (`google.com/maps/d/kml?mid=<mid>&force=lite` → unzip → `doc.kml` →
   placemark coordinates), corroborated with the device's own GPS if it has
   one (agreed within ~6 m on the validated site). In-town cabinets correctly absent from
   UISP/MyMate/Directory may exist only on the My Map — that is why the map
   wins.
2. **Address principle (operator rule):** fill ONLY what is accurately
   ascertainable. Reverse-geocoder house numbers are NOT evidence
   (`12345` on Example Rd was a snap, not a mapped building) — omit `line1`
   unless the street address is independently verified. City + ZIP are safe
   when the pin is inside a known postal boundary; keep the state.
3. **Sonar network_site** (id needed for directory V column + notes).
4. **Tower Directory row** (alphabetical insert; Sonar ID col V).
5. **UISP site** (name + note; coordinates intentionally left null).
6. **MyMate site** — normally during `program-new-device` registration; if
   the site must exist standalone, create it there. Device→site assignment
   is always an operator click (PATCH `site_id` 403s, `can_move_devices:false`).

## Coordinates format per resource

| Resource | Format |
|---|---|
| Sonar | 10-decimal padded geopoint, e.g. `45.000,-93.000` padded with trailing zeros to 10 decimals (rides on the address lat/long) |
| Tower Directory slot C | `lat, lon` ~5 decimals, e.g. `45.000, -93.000` |
| MyMate `POST /api/sites` | raw floats `latitude: 45.000, longitude: -93.000` |
| UISP | **not set** — house convention leaves all 21k site coords null |

## Sonar `createNetworkSite` (verified working)

```graphql
mutation { createNetworkSite(input: {
  name: "Hotel Fiber Cabinet"
  height_in_meters: 0
  address: {
    city: "Hotel"
    zip: "00000"
    subdivision: <STATE_ENUM>
    country: US
    latitude: 45.000
    longitude: -93.000
  }
}) { id name geopoint } }
```

Hard rules:
- `height_in_meters` is **REQUIRED** (Float) — `0` for a ground cabinet.
- `subdivision` is **REQUIRED for US addresses** — the instance uses a custom
  state enum, all FIPS-styled. Resolve the county from an existing site in the
  same county via the `addresses(city:"...")` query (`network_sites` nested
  getters frequently 500): saved addresses in that county show
  `subdivision: <STATE_ENUM>, county: <COUNTY_FIPS>`. The state-level enum is what
  house sites carry; do NOT guess county-enum strings.
- Coordinates ride **on the `address` input** (sets the geopoint); there is no
  site-level geopoint field.
- The mutation **returns the site object directly** (`{ id name geopoint }`).
- Response and address readback accessible via the returned geopoint; the
  `network_sites` getter is flaky (500s) — record the id immediately.

## Tower Directory row (Master Tower List)

Spreadsheet `<SHEET_ID>` (`sheets.tower_directory.id` in `site-pack/profile.json`), tab
`Master Tower List` — **operator's external domain, user-OAuth only**
(`scripts/google_user_auth.mjs` `getGoogleUserToken`, scopes `.../spreadsheets`).
23 columns. Established-site conventions (studied from ~2.7k rows):

| Col | Content | Convention |
|---|---|---|
| A | MyMate Name | must match site record exactly |
| B | TH Name | blank when no tower host |
| C | Coordinates | `lat, lon` ~5 decimals |
| D | Address | 4 comma segments `street, city, ST, ZIP`; **if no verified street use leading empty segment: `, City, ST, ZIP`** (this comma count feeds tracker-sheet automation — never a different segment count) |
| E | State | duplicate of D's state |
| V | Sonar ID | written RAW as number; column format renders zero-padded 5 digits (`123` → `00123`, matching legacy `00045`) — do not write a padded string |
| F–U | blank for ground cabinets without equipment |

Insert procedure (verified): `batchUpdate` with
`insertDimension { sheetId: 0, dimension: 'ROWS', startIndex: <row-1>,
endIndex: <row> }` to make room **at the alphabetical position**, then
`values.update` the new row's range (columns A to V, e.g. `A12:V12`) with
`valueInputOption=RAW`, then **read back one row above to one row below, columns A to W (e.g. `A11:W13`)** and confirm the
row content AND both neighbors (alphabet preserved — e.g. `Hotel Fiber Cabinet`
landed between `Hmike` and `Hotel WT North (Upper)`).

## UISP site (verified working)

Address: `UISP=$(python3 scripts/resources.py get uisp)`. If it is not registered, stop and ask
the operator; do not take a UISP address from another system. For the MCP `ubnt-uisp` tool the
host is `python3 scripts/resources.py get uisp --field host`.
Token: the `ubnt-uisp` credential field **`apiToken`** from the MCP
encrypted store (header `x-auth-token`).

```bash
POST $UISP/nms/api/v2.1/sites  -d '{"name":"Hotel Fiber Cabinet","type":"site"}'
```
- FLAT payload — no `{"data":{...}}` wrapper ("name is required" 400 otherwise).
- **`geoposition` is NOT accepted** on create or update; there is no geolink
  route for sites (`PUT /sites/{id}/geolink` → 404). House convention leaves
  coordinates null; do not fight the schema.
- Note: `PUT /sites/{id}` needs a full **SiteUpdate** — include the whole
  current `identification` object (id, name, parent, status, suspended, type)
  and a complete `description` (address, note, contact{name,phone,email},
  location, height, elevation, endpoints, ucrmId, regulatoryDomain). Output
  helper fields (`deviceCount`, `deviceListStatus`, `deviceOutageCount`,
  `ipAddresses`, `sla`, computed device stats) must stay out of the PUT.
- Keep responses with computed fields read-back; PATCH on sites → 404.
- Verify with `GET /sites/{id}` (name + note).

## MyMate site (also in program-new-device §registration)

Sanctum session auth per `mymate` skill (CSRF + Origin/Referer). Verified:

```bash
POST /api/sites -d '{"name":"Hotel Fiber Cabinet","latitude":45.000,"longitude":-93.000}'
```
- Creates as `kind:"other"` for in-town sites (e.g. a fiber cabinet = MyMate site `<MYMATE_SITE_ID>`).
- Device site_id PATCH → **403** (`can_move_devices:false`) — site assignment
  on the device record is an operator one-click, not agent-recoverable.
- Note convention on important sites (style seen on existing sites):
  `Created YYYY-MM-DD. Sonar network_site <id>. <one-line context>`.

## Verification checklist

For every resource created this round, re-read it before reporting success:
Sonar geopoint from the mutation response; Directory readback rows ±1; UISP
`GET /sites/{id}` name+note; MyMate `GET /api/sites/{id}` (or device index)
status/lat-long. Sonar's flaky getters are NOT a failure signal if the
mutation response was captured — say so instead of re-querying.
