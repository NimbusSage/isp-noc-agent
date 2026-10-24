---
name: ticket-context
description: Use when processing a Sonar trouble ticket to identify the correct tower site name for the network bottleneck analyzer. Handles the operator's fiber-network vs tower-site naming convention, subject line parsing, and first-note extraction workflows.
---

# Ticket Context — Tower Site Identification

## CRITICAL WARNING

**Do NOT rely on the ticket subject alone to identify the problem site.** The subject often contains a fiber network prefix that is NOT the tower site in question. Always follow the full identification workflow below.

## Fiber Network vs Tower Site

The org's tickets follow the naming convention in the site-pack profile (`site_pack.ticket_context`): the **fiber network** and the **tower site** are different things, and path analysis needs the **tower site name**, not the fiber network.

- **Fiber networks** are named after locations: `Alpha`, `Bravo`, `Charlie`, `Delta`, `Echo`, `Foxtrot`, etc.
- **Tower sites** are named after the tower host (property owner): `Smith`, `JSmith`, `John Smith`, `MKilo`, `Dan Lima`, `Tango`, `Sigma`, etc.

## Tower Host Naming Convention

Your org's own convention lives in `site-pack/ticket-context/naming-conventions.md` (untracked; fictional schema example: `site-pack/example/naming-conventions.example.md`). If that file is absent, use the default pattern below and confirm with the operator before relying on it.

If the tower host's name is John Smith, the site names follow this pattern depending on how many towers they host:

| Tower # | Site Name |
|---------|-----------|
| #1 | `Smith` |
| #2 | `JSmith` |
| #3 | `John Smith` |
| #4 | `John Smith Two` |
| #5 | `John Smith Three` |

### Initial + Surname Abbreviation Pattern — CRITICAL

Tower site names use an abbreviated format: `[FirstInitial][LastName]`

| Ticket Site Name | Meaning | Possible Host Names |
|------------------|---------|---------------------|
| `MDane` | M. Dane | Mike Dane, Rebecca Dane, etc. |
| `MKilo` | M. Kilo | Mike Kilo, etc. |
| `JSmith` | J. Smith | John Smith, etc. |
| `LKilo` | L. Kilo | Larry Kilo, etc. |

**The first letter is the host's FIRST INITIAL, the rest is their LAST NAME.**

**CRITICAL:** You CANNOT determine the exact host from the abbreviation alone. `MDane` could be "Mike Dane" or "Rebecca Dane" — you MUST check the ticket comments to identify the correct host.

**For bottleneck analysis:** Use the search API with the host's LAST NAME (e.g., `Ross`) and pick the EXACT match from ticket comments.

## Entity Disambiguation — Tower Sites vs Tower Hosts vs Customers

The org's tickets reference three distinct entity types that share overlapping names. Confusing them leads to misdiagnosing network issues as customer issues (or vice versa).

### The Three Types

| Type | What it is | Example | How it appears in tickets |
|------|-----------|---------|--------------------------|
| **Tower Site** | Network node in inventory (router, AP, backhaul) | `Kilo`, `LKilo`, `MKilo`, `Dan Lima` | `Tower:` field, `<name> pppoe` suffix, subject as the site name |
| **Tower Host** | Property owner who hosts the tower on their land | `Eric Kilo`, `John Smith`, `Dan Lima` | `TH` / `Tower Host` in subject, "TH repair" tickets, site notes |
| **Customer** | Service subscriber at a location | `Kilo Farms`, `Customer A`, `Customer B` | `Customer Name:` field, account info, "cx called in" |

### Key Rules

1. **A tower host's person-name is NOT the same as the site name.** `Eric Kilo` (person) hosts towers at sites named `Kilo`, `LKilo`, etc. The bottleneck analyzer needs the **site name**, not the host's full name.

2. **Multiple sites can share a host surname.** The Kilo family hosts multiple towers: `Kilo` (tower #1), `LKilo` (tower #2), `Eric Kilo` (tower #3). These are distinct network nodes — do not conflate them.

3. **`TH` in a subject = tower host / customer-side issue.** A ticket like "Eric Kilo TH repair" is about the tower host's equipment or premises, NOT a network site outage. These often belong in `NOC - CX/TH-side Issue` (`sonar.categories.cx_th_side_issue`).

4. **`pppoe` suffix = network site.** A ticket like "Kilo pppoe - slow speeds" is about the `Kilo` network site. The `pppoe` indicates a PPPoE session on the CPE at that site.

5. **Same name, different context.** `Dan Lima` can be a tower site name (network node) AND a tower host (person). Look at the ticket context:
   - `(oscar) dan lima - slow speeds` → `Dan Lima` is the **tower site**
   - "Dan Lima TH repair" → `Dan Lima` is the **tower host** (customer-side)

### How to Tell from the Ticket

| Signal | Entity Type | Action |
|--------|------------|--------|
| `Tower: <name>` or `<name> pppoe` in description/subject | **Tower Site** | Use as bottleneck analyzer input |
| `TH` or `Tower Host` in subject or description | **Tower Host** | Likely CX/TH-side issue, not a network site problem |
| `Customer Name:` field populated | **Customer** | Customer info — check if issue is customer-side |
| "cx called in" / "customer reported" in description | **Customer report** | May be customer-side; investigate network before assuming tower issue |
| `Network/Fiber: <name>` | **Fiber network** | NOT the tower site — this is the upstream fiber path |
| `Link: <site1> 2 <site2>` | **Backhaul link** | Two tower sites connected; check both ends |

### Search Implications

When searching historical tickets for a site (using the `sonar-api` search-all-tickets query):
- Search for the **site name** (e.g., `LKilo`), not the host's full name
- A search for `Kilo` will return tickets for the `Kilo` site AND `LKilo` AND host-family tickets — **post-filter** by checking the `Tower:` field in descriptions
- Look at the first note's `Tower:` field to confirm which network site a ticket actually relates to

## Subject Line Parsing Rules

The ticket subject typically follows one of these formats:

- `[FiberNetwork] tower_site pppoe - description`
- `(FiberNetwork) tower_site pppoe - description`
- `FiberNetwork tower_site - description`

**CRITICAL RULE:** When the subject starts with a name in brackets `[...]` or parentheses `(...)` at the beginning, that is the **fiber network**, NOT the tower site. The tower site is the name that follows it.

## Examples

| Subject | Fiber Network | Tower Site (use this) |
|---------|--------------|----------------------|
| `[Charlie] MKilo pppoe - Slow tower` | Charlie | **MKilo** |
| `(oscar) dan lima - slow speeds` | Oscar | **Dan Lima** |
| `(Bravo) golf pppoe/ slow tower` | Bravo | **Golf** |
| `[Charlie] Sigma pppoe - slow speeds` | Charlie | **Sigma** |
| `[Bravo] tlima pppoe - Slow Speeds` | Bravo | **TLima** |
| `[Foxtrot] Whiskey Router - Slow Speed` | Foxtrot | **Whiskey** |
| `Charlie Zulu Slow Tower` | Charlie | **Zulu** |

**No prefix case:** If the subject does NOT have a bracketed/parenthetical prefix, the entire subject may refer to the tower site directly (e.g., `Charlie Yankee Slow Tower` → tower site is `Yankee`).

## Workflow for Identifying the Tower Site

1. Fetch the ticket (includes `description` and `subject`)
2. Fetch the ticket's **first note** — look for explicit `Tower:` or `Network/Fiber:` fields
3. **If the first note has `Tower: <name>`** — use that as the tower site
4. **If the first note has `Network/Fiber: <name>`** — that is the fiber network, NOT the tower site
5. **If the first note lacks explicit fields** — parse the subject using the rules above:
   - Subject starts with `[X]` or `(X)` → X is the fiber network, the next name is the tower site
   - Subject has no prefix → the subject itself likely names the tower site
6. Only then run the bottleneck analyzer with the tower site name

## How to Fetch Ticket Notes

```graphql
query {
  ticket_comments(search: {
    integer_fields: [
      { attribute: "ticket_id", operator: EQ, search_value: <ticket_id> }
    ]
  }, sorter: { attribute: "created_at", direction: ASC }, paginator: { page: 1, records_per_page: 1 }) {
    entities {
      id
      body
      user { name }
      created_at
    }
  }
}
```

## Patterns to Look For in First Note Body

- `Tower: <name>` or `Tower: <name> | <ip>` — this is the tower site
- `Radio AP: <name> | <ip>` — this may be the tower site
- `Network/Fiber: <name>` — this is the fiber network (NOT the tower site)
- `<site> pppoe` in the subject or body — the pppoe name is often the tower site

## Important Note

The site name passed to the bottleneck analyzer should match the `site_name` field the analyzer recognizes (typically the tower/site name as it appears in the network inventory).

## Peak / Night-Check Identification (for `ihn-auto-return` & `peak-scheduler`)

Tickets requiring a **peak-window retest** before IHN bounce must be flagged here. Scan **subject + description + ALL `ticket_comments.body`** (fetch paginator page 1 records_per_page 20):

- Night/peak category (`sonar.categories.night_peak`, NOC - Night Checks) present → peak
- Keywords (case-insensitive): `night check`, `n check`, `night checks`, `peak hours`, `peak hour`, `peak time`, `nighttime`, `overnight`, `evening speeds`, `congestion at peak`, plus time tokens `7pm`, `7 pm`, `19:00`, `20:00`, `night peak`. Brackets like `[NIGHT CHECK]` normalize to `night check`.
- **Peak window**: `20:00-21:00 America/Chicago` (8-9 PM Central) — defined via env `PEAK_WINDOW_START`/`PEAK_WINDOW_END`/`PEAK_WINDOW_TZ`. Use `TZ=America/Chicago date +%H:%M` or `Intl.DateTimeFormat` with that TZ.
- **Logic per `peak-scheduler`**: peak ticket + `isPeakWindow==true` → eligible to bounce at peak (peak check satisfied); peak ticket + `isPeakWindow==false` → **do NOT bounce now** — enqueue to `logs/pending_peak_checks.json` and defer analyzer to next 20:05 CT drain.
- Weather exclusion (Rain Fade below) is **absolute** — it blocks bounce even at peak and also unqueues.

## Rain Fade Category Identification

Tickets should be tagged with **NOC - Rain Fade Issues** (`sonar.categories.weather`) when the description or comments indicate weather-related problems.

### Indicators of Rain Fade

- Explicit mention of rain, storm, weather, precipitation
- Issues that started during or after rain/weather events
- Intermittent problems that correlate with weather patterns
- Customer reports "works fine when it's dry, goes out when it rains"
- Signal degradation mentioned during weather events
- Tickets referencing "rain fade", "weather fade", or "wet weather"
- **Scan scope for IHN bounce**: subject + description + **ALL `ticket_comments.body`** — not just description. Treat indirect phrasing ("went out during storm last night, still slow today", "wet weather in area") as weather via LLM reasoning; be conservative and exclude. Weather exclusion is absolute and never queued for peak.

### When NOT to Tag Rain Fade

- Slow speeds with no weather mention
- General outages without weather correlation
- Equipment issues (radio failures, misconfigurations)
- Customer-side problems unrelated to weather

### How to Add the Category

After identifying a ticket as rain fade, add the category:

```graphql
mutation {
  updateTicket(id: <ticket_id>, input: {
    ticket_category_ids: [<existing_cat_ids>, <weather_category_id>]
  }) {
    id
  }
}
```

**Note:** Include all existing category IDs plus the weather category ID. The mutation replaces all categories.

## Duplicate Ticket Detection

After identifying the tower site, check for duplicate tickets using the `duplicate-detection` skill. This prevents NOC agents from working on the same issue twice.

**When to check:** When processing any NOC ticket (new or existing)

**Quick check:** Search for other open tickets with the same tower site name. If found, compare issue types to determine if duplicate.

See `duplicate-detection` skill for full algorithm and handling workflow.
