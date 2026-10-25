---
name: correlated-issues
description: Use when correlating bottleneck analysis results across active NOC tickets to find shared root causes. After a successful DEGRADED analysis, searches for other tickets downstream of the same bottleneck device.
---

# Correlated Issues — Find Shared Root Causes

After a successful bottleneck analysis that returns DEGRADED status, search other active NOC tickets for tower sites that are **downstream** of the same bottleneck device. List all correlated tickets so a NOC tech can compare and close them together.

## When to Run

- **Run**: After bottleneck analysis completes with `DEGRADED` status
- **Skip**: If analysis is `HEALTHY` (no issue to match)
- **Skip**: If analysis failed or was not run

## Search Pattern

Bottleneck analysis comments are posted in two visible formats, and **both must be searched**:

| Format | Visible header | Used by |
|--------|----------------|---------|
| Legacy (analyzer API era) | `<p><strong>Network Bottleneck Analyzer - {site}</strong></p>` | predecessor of the live path-analyzer |
| Current (live `path-analyzer`) | `<p><strong>Direct Path Analysis - {site}</strong></p>` | `path-analyzer` skill |

**Sonar strips HTML comments on save** — the `<!-- path-analyzer -->` and `<!-- Network Bottleneck Analyzer -->` markers emitted by `path-analyzer` are NOT present in the stored body (verified on live tickets: zero stored comments contain them). Never rely on the HTML-comment marker for the search.

**Search term**: use `Direct Path Analysis` **or** `Network Bottleneck Analyzer` when scanning active NOC tickets, and treat a comment as an analysis comment when either string is present. Searching only the legacy string silently misses every analysis posted by the live `path-analyzer` (verified: live tickets store `Direct Path Analysis` only).

## Downstream Matching Logic

**Core concept**: If current ticket has a bottleneck at device X, search for other tickets whose path analysis includes device X. These tower sites are downstream of the same issue.

**Example**:
- Current: `[Alpha] bravo pppoe - slow tower` → bottleneck at `charlie backhauls` (hop 3)
- Found: `[Alpha] phi pppoe - slow speeds` → path includes `charlie backhauls` (hop 2)
- **Match**: Both sites are downstream of `charlie backhauls`

**Why this works**: Shared network devices (backhauls, fiber routers, CGNAT) affect all tower sites whose traffic flows through them.

## Workflow

### Step 1: Extract Bottleneck Devices from Current Analysis

From the current ticket's bottleneck analysis results, extract the device names where issues were detected:

- Look for **red** (critical) and **orange** (warning) callouts in the path analysis
- Extract the device name (e.g., `charlie backhauls`, `bravo backhauls`)
- These are the "bottleneck devices" to search for in other tickets

### Step 2: Search Active NOC Tickets

Search all active NOC tickets for analysis comments (`Direct Path Analysis` — plus the legacy predecessor header on older stored tickets):

```graphql
query {
  tickets(search: {
    integer_fields: [
      { attribute: "ticket_group_id", operator: EQ, search_value: <noc_group_id> }
    ],
    string_fields: [
      { attribute: "status", search_value: "OPEN", match: true },
      { attribute: "status", search_value: "PENDING_INTERNAL", match: true }
    ]
  }, paginator: { page: 1, records_per_page: 100 }) {
    entities {
      id
      subject
      status
    }
    page_info { total_count }
  }
}
```

### Step 3: Fetch Comments and Check for Bottleneck Analysis

For each active ticket, fetch comments and search for the `Direct Path Analysis` marker (legacy `Network Bottleneck Analyzer` also accepted so older stored comments still match):

```graphql
query {
  ticket_comments(search: {
    integer_fields: [
      { attribute: "ticket_id", operator: EQ, search_value: <ticket_id> }
    ]
  }, sorter: { attribute: "created_at", direction: DESC }, paginator: { page: 1, records_per_page: 5 }) {
    entities {
      id
      body
      user { name }
      created_at
    }
  }
}
```

**Skip tickets that don't have an analysis comment (either header).**

### Step 4: Parse ALL Hops and Check for Bottleneck Devices

For each ticket with a bottleneck analysis comment, parse the comment to extract **ALL hop device names**:

1. Extract site name from first line: `Direct Path Analysis - [site_name]` (legacy: `Network Bottleneck Analyzer - [site_name]`)
2. Extract status: `Status: HEALTHY` or `Status: DEGRADED`
3. **Parse ALL `Hop N:` blocks** — extract device names (e.g., `charlie backhauls`, `bravo pppoe`)
4. Check if any bottleneck device from Step 1 appears in the path

**Key**: Compare ALL hops, not just bottlenecked ones. A device at hop 10 in one ticket might be at hop 3 in another (shorter path).

### Step 5: Post Correlation Comment

If matches found, post a comment to the current ticket listing all correlated tickets:

```html
<p><strong>Correlated Issues Found</strong></p>
<p>The following active NOC tickets are downstream of the same bottleneck:</p>
<ul>
  <li><a href="https://your-org.sonar.software/app/tickets/<ticket-id-2>"><ticket-id-2></a> - bravo (Alpha) — downstream of charlie backhauls</li>
  <li><a href="https://your-org.sonar.software/app/tickets/<ticket-id-3>"><ticket-id-3></a> - phi (Alpha) — downstream of charlie backhauls</li>
</ul>
<p><strong>Bottleneck Device:</strong> charlie backhauls (hop 3)</p>
<p><strong>Issue:</strong> Bandwidth drop — 61.5 Mbps down (should be 400+ Mbps)</p>
<p><strong>Fiber Network:</strong> Alpha</p>
<p>Consider closing all tickets together when the upstream issue is resolved.</p>
```

## Comment Parsing Details

The bottleneck analysis comment has this structure:

```html
<p><strong>Direct Path Analysis - [site_name]</strong></p>
<p><strong>Status: [HEALTHY/DEGRADED]</strong> — [summary]</p>
<p><strong>Path Analysis — [site_name] → Internet:</strong></p>
<p>
<strong>Hop 1:</strong> [device_name] ([IP]) — [bandwidth]<br>
&nbsp;&nbsp;&nbsp;&nbsp;→ [callout]<br><br>
<strong>Hop 2:</strong> [device_name] ([IP]) — [bandwidth]<br>
&nbsp;&nbsp;&nbsp;&nbsp;→ [callout]<br><br>
<!-- Repeat for each hop -->
</p>
```

**Extract from each hop**:
- Device name (e.g., `charlie backhauls`, `bravo pppoe`, `delta fiber`)
- Whether it's a bottleneck (red/orange callout)

**Extract from fiber backbone hops**:
- Fiber network name (e.g., `charlie fiber` → `charlie`, `delta fiber` → `delta`)

## Posting Comments — Use the Helper (Required)

Use `scripts/sonar_comment.py` with `&quot;` HTML (see the `comment-formatting` `BODYPLACEHOLDER` note). Never use `BODYPLACEHOLDER` replace hacks.

```bash
cat > /tmp/comment.html << 'HTMLEOF'
<p><span style=&quot;color:green&quot;>→ ✓ ARM RB4011, healthy</span></p>
HTMLEOF
python3 scripts/sonar_comment.py 12345 /tmp/comment.html --verify
```

## Example: Full Workflow

**Current ticket**: `[Alpha] bravo pppoe - slow tower` (ID: <ticket-id-1>)

**Step 1**: Bottleneck analysis completes → DEGRADED
- Bottleneck at `charlie backhauls` (hop 3): bandwidth drop 61.5 Mbps down

**Step 2**: Search active NOC tickets
- Find 10 open NOC tickets

**Step 3**: Check each for an analysis-marker comment (`Direct Path Analysis`, legacy `Network Bottleneck Analyzer`)
- Ticket <ticket-id-1> (current) — has analysis (skip, it's the current ticket)
- Ticket <ticket-id-2> — has analysis
- Ticket <ticket-id-3> — has analysis
- Other tickets — no analysis (skip)

**Step 4**: Parse hops and check for `charlie backhauls`
- <ticket-id-2>: hops = [bravo pppoe, charlie backhauls, charlie fiber] → `charlie backhauls` found → **MATCH**
- <ticket-id-3>: hops = [chi pppoe, oscar backhauls, oscar fiber] → no match

**Step 5**: Post correlation comment to <ticket-id-1> listing <ticket-id-2>

## Efficiency Tips

1. **Skip HEALTHY**: If current analysis is HEALTHY, stop immediately
2. **Skip tickets without analysis**: Don't fetch comments for tickets that have neither `Direct Path Analysis` nor the legacy `Network Bottleneck Analyzer` marker
3. **Batch comment fetching**: Fetch comments for multiple tickets in parallel if possible
4. **Cache device names**: Extract bottleneck device names once, reuse for all comparisons
5. **Limit context**: Parse comments to extract only device names, not full analysis

## Edge Cases

- **No other tickets with analysis**: Post nothing (or optionally post "No correlated tickets found")
- **Multiple bottleneck devices**: Search for all of them, match any
- **Same device, different issue types**: Still correlate — the device is the common factor
- **Current ticket is the only one with the device**: No correlation (post nothing)
