# Tracker Sheet — one ticket for several links on one site (reference)

Read on demand from SKILL.md §7a. Operator spec for filing two or more
misaligned/degraded links that share one tower-site end as a SINGLE repair
ticket (`... are misaligned/turned, one ticket for both`). Worked end-to-end
on a live two-link filing; names below are fictional, the shape is real.

## When this applies

- The operator names two or more link radios tied to the same site.
- The ask is ONE ticket (their words: one row that creates the ticket, other
  links as detail rows). Never split into several tickets; never squeeze the
  second link's facts into the first row's column fields.
- Each link still gets its OWN row — the sheet keeps one row per link so the
  auto-filled coordinates, distances, Copy Cell and per-row alerts stay
  per-link; the differences are (a) which row creates the ticket and (b)
  that the ticket row's Notes carry the other links' Copy Cells.

Per-row column rules never change: §3a still applies on every row (no
Tracking IP on misaligned links, no AP IP, Towers Affected blank while both
ends are up, contact flags only from facts the operator gave).

## Step 1 — stage each non-ticket link as an alert-only row

1. `plan-append` → `append` per staged link with `create_ticket:false`
   (`Create Ticket=FALSE`, `Send Count=1`: the Sheet still alerts the
   company per link, but the automation must NOT create a ticket).
2. This is staging, NOT a high-confidence-duplicate row (§11 rule 2): never
   pass `ticket:` and never reference any existing ticket — there is none
   yet.
3. Read the row back (helper read `A{n}:U{n}` + Copy Cell `AT{n}`).
   Coordinates, distance and the Copy Cell populate after the send; do not
   quote a Copy Cell that a fresh readback has not confirmed.

## Step 2 — file the real ticket row; paste staged Copy Cells into its Notes

1. The ticket row is the link the operator's method treats as the one where
   they DO actually create the ticket. When the operator named a specific
   device/radio, that link is the primary row. Unsure → ask.
2. Validated row as usual (`create_ticket:true` omitted or TRUE).
3. Read each staged row's Copy Cell (col AT) and paste it VERBATIM into the
   ticket row's Notes under its per-link heading (format below). Never
   reword, trim, or re-layout a pasted Copy Cell — its `Link:` / `Reason:` /
   `| NOC's Notes:` / `TOWER1...` / `TOWER2...` / `Link Distance:` lines are
   the operator's own manual-ticket shape.
4. The ticket row's own link gets a block in the same Copy Cell shape.
5. Review the readback: distance sane, coords match Sonar §2 verification,
   notes carry both blocks, headings intact.

## The notes format (operator's manual form)

Every note fact for a single link stays grouped. Headings, one block per
link, in the order the operator listed them:

(inside the Notes cell)
LINK #1
<first staged row's Copy Cell, verbatim>

LINK #2
<ticket-row link's own block, same shape: `Link: <device> / Reason: <issue>
/ | NOC's Notes: <signals + the operator's own prose + bare counts> /
TOWER1..., TOWER2... / Link Distance: <auto> / NOC Tech - <tech>>

Rules:

- One `LINK #N` heading per link; nothing about link N outside block N.
- Inside a block, only the §8 note standards: live signals + reported
  capacity both sides (dBm + Mbps), the operator's own prose woven in as
  normal sentences, bare customer counts. NO analysis prose, NO timelines,
  NO `(Prompt: …)`.
- The staged Copy Cell rides VERBATIM inside its block. The Sheet
  automation copies the whole Notes cell into the ticket description, so
  the per-link blocks land on the ticket formatted as written.
- Post-filing reformat or clarity fix (blocks added late, heading cleanup):
  `node scripts/tracker_sheet.mjs set-notes --row <n> --notes '<text>'
  --confirm-write true` — Notes-only, never re-fires the alert. The Sonar
  ticket description was built from the ORIGINAL notes: say so in the
  report and ask if the operator also wants that description refreshed.

## Step 3 — send order and the Ticket #

1. `wait-for-ticket --row <ticket row>` for the Ticket # (staged rows never
   get one — no polling them).
2. One Ticket # serves every link; nothing else is filed per link beyond
   the follow-through below.

## Per-link follow-through on the single ticket (§12 + §13)

- Sonar site link (once): resolve the problem device's site per
  `references/post-filing-links.md`; for a shared-site filing that anchor
  site's InventoryItem/site link happens ONCE, and the report says which
  end was linked. (Auto-fallback: exact radio-name InventoryItem lookup
  returns zero/ambiguous hits → link the problem device's site and say so.)
- MyMate device note (one per problem device): each link's named problem
  side gets its note on its own device ID (MyMate list of the shared site's
  radios; a link missing from the site's list gets looked up by its other
  end or reported, never guessed). Standard format, ` -NOC_Agent` suffix.
- MyMate GeoMap ticket link (one per link): full-link rule — the symbol
  goes on each link's ST-side device (never the AP side unless that side is
  the dead/red one, then the AP and say so). Same Ticket # referenced on
  every symbol; idempotent GET-before-POST each time.
- RSSI evidence (§13): ONE chart PER LINK, all attached to the SAME
  ticket. Chart each link's AP side (or the side with usable series — see
  the `--series` gotcha in §13); attach under a mini heading naming the
  link so the ticket reads per-link, in `LINK #N` order.
- Report to the operator must list, per link: staged/ticket row #, Copy
  Cell provenance, chart posted, MyMate note + symbol posted, and the one
  site link. SKILL §12 steps 1–3 run once overall (site) and once per link
  (devices/symbols).
