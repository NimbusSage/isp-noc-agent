---
name: duplicate-detection
description: Use when processing a new NOC ticket to detect if it's a duplicate of an existing open ticket. Identifies duplicate tickets by matching tower site and issue type, then handles high-confidence duplicates by posting a comment and reassigning to CS Level 2.
---

# Duplicate Ticket Detection & Handling

## Overview

When processing a new NOC ticket, check if it's a duplicate of an existing open ticket. This prevents NOC agents from working on the same issue twice and ensures customers with ongoing issues are directed to the original ticket.

## When to Use This Skill

- When a new ticket arrives in the NOC group (`<noc_group_id>`)
- When processing existing open NOC tickets
- As part of the ticket intake workflow

## When NOT to Use This Skill

- Tickets created directly in the NOC group (not transferred from CS) — these are typically not duplicates
  - If `ticket_created` is `true`, skip duplicate detection
  - If `ticket_created` is `false`, proceed with duplicate check
- Tickets that are clearly different issues (different tower sites, different problem types)
- Tickets with no clear tower site identified

## Duplicate Detection Algorithm

### CRITICAL WARNING

**DO NOT match on fiber network name!**
- Fiber networks (Alpha, Bravo, Charlie, etc.) cover MANY different tower sites
- Two tickets on the same network are NOT duplicates unless they're for the SAME tower site
- Example: Alder and Alden are DIFFERENT sites on the Alpha network — NOT duplicates

**Site names must match EXACTLY (same spelling):**
- "golf" = "golf" ✓ — same site
- "alder" ≠ "alden" ✗ — different sites
- "hotel" ≠ "hotels" ✗ — different sites

### Step 1: Extract Tower Site

Use the `ticket-context` skill rules to extract the tower site from the ticket subject:

**Subject Line Patterns:**
- `[FiberNetwork] tower_site pppoe - description` → tower_site is after brackets
- `(FiberNetwork) tower_site pppoe - description` → tower_site is after parentheses
- `FiberNetwork tower_site - description` → tower_site is second word
- `FiberNetwork tower_site pppoe - description` → tower_site is second word

**Examples:**
| Subject | Tower Site |
|---------|-----------|
| `[Bravo] golf pppoe - [Slow Speeds]` | golf |
| `(Bravo) golf pppoe/ slow tower` | golf |
| `[Echo] Foxtrot pppoe - slow tower` | foxtrot |
| `[Charlie] MKilo pppoe - Slow tower` | mkilo |

**Important:** Normalize tower site names to lowercase for comparison.

### Duplicate Examples (What IS and IS NOT a Duplicate)

| Ticket A | Ticket B | Same Site? | Same Issue? | Duplicate? | Why? |
|----------|----------|------------|-------------|------------|------|
| [Alpha] Alder pppoe - slow tower | [Alpha] Alden pppoe - slow tower | NO (alder ≠ alden) | Yes | NO | Different site names |
| [Alpha] golf pppoe - slow tower | [Bravo] golf pppoe - slow tower | Yes | Yes | YES | Same site, same issue |
| [Alpha] hotel pppoe - slow speeds | [Alpha] hotel pppoe - public IP | Yes | NO | NO | Different issue types |
| [Echo] Foxtrot pppoe - outage | [Echo] Foxtrot pppoe - outage | Yes | Yes | YES | Same site, same issue |
| [Charlie] MKilo pppoe - slow | [Charlie] MKilo pppoe - slow | Yes | Yes | YES | Same site, same issue |

**Key takeaway:** Site names must match EXACTLY (same spelling). Fiber network name is irrelevant.

### Step 2: Extract Issue Type

Categorize the issue based on subject and description keywords:

| Issue Type | Keywords (case-insensitive) |
|------------|----------------------------|
| Slow Speeds | slow, speed, latency, throughput, bandwidth |
| Outage | outage, down, no internet, not working, offline |
| Intermittent | intermittent, flapping, dropping, unstable |
| High Latency | latency, ping, delay, lag |
| Packet Loss | packet loss, loss, drops, packet loss |
| Equipment | specific hardware mentions (RB4011, etc.) |

**Primary issue type** is determined by the first matching keyword in the subject line.

### Step 3: Search for Existing Tickets

Query all open NOC tickets:
- Status = OPEN or PENDING_INTERNAL (not CLOSED)
- Ticket group = NOC (`<noc_group_id>`)
- Created before the new ticket

```graphql
query {
  tickets(search: {
    integer_fields: [
      { attribute: "ticket_group_id", operator: EQ, search_value: <noc_group_id> }
    ]
    string_fields: [
      { attribute: "status", search_value: "OPEN", match: true },
      { attribute: "status", search_value: "PENDING_INTERNAL", match: true }
    ]
  }) {
    entities {
      id
      subject
      status
      created_at
      description
      ticket_categories { entities { id name } }
    }
    page_info { total_count }
  }
}
```

### Step 4: Compare and Score

For each existing ticket, calculate a duplicate confidence score:

**CRITICAL RULE: Site names must match EXACTLY (same spelling)**
- "alder" ≠ "alden" — different sites, NOT duplicates
- "golf" = "golf" — same site, potential duplicate
- Normalize to lowercase for comparison, but spelling must be identical

**Required Matches (must have BOTH for duplicate):**
1. **Tower site match**: EXACT same tower site name (same spelling, normalized to lowercase)
2. **Issue type match**: Same issue category (slow speeds, outage, etc.)

**What does NOT count as a match:**
- Same fiber network (Alpha, Bravo, etc.) — networks cover many sites
- Similar site names (alder vs alden) — different sites
- Same site but different issue types (slow speeds vs public IP issue)

**Bonus Points (increase confidence):**
- Same fiber network: +0% (not a factor — ignore this)
- Tickets within 7 days: +10%
- Tickets within 30 days: +5%
- Different customers: +10%
- Similar description keywords: +5%

**Confidence Thresholds (tunable via env):**
- **High (≥`DUPLICATE_HIGH_THRESHOLD`, default 85%)**: Auto-handle duplicate
- **Medium (≥`DUPLICATE_MED_THRESHOLD`, default 50% to high-1)**: Post informational comment only
- **Low (<med)**: Not a duplicate, skip

Env vars `DUPLICATE_HIGH_THRESHOLD` / `DUPLICATE_MED_THRESHOLD` override defaults (see AGENTS.md). IHN bounce uses separate `IHN_TOWER_THRESHOLD` (see `ihn-auto-return`).

### Step 5: Handle Duplicates

#### High Confidence (≥85%) — Auto-Handle

1. **Post comment on duplicate ticket:**
```html
<p><strong>Duplicate Ticket Detected</strong></p>
<p>This ticket appears to be a duplicate of <a href="https://your-org.sonar.software/app#/tickets/show/{original_id}">#{original_id}</a> ({original_subject}).</p>
<p>Both tickets are for <strong>{tower_site}</strong> tower site with <strong>{issue_type}</strong> issues.</p>
<p>Original ticket created: {original_date}</p>
<p>This ticket has been reassigned to CS Level 2 for review.</p>
```

2. **Reassign to CS Level 2:**
```graphql
mutation {
  updateTicket(id: {ticket_id}, input: {
    ticket_group_id: <cs_level_2_id>
  }) {
    id
    ticket_group { id name }
  }
}
```

**Note:** Do NOT update the ticket status. Keep it active (OPEN or PENDING_INTERNAL).

#### Medium Confidence (50-84%) — Informational Comment

Post a comment suggesting it might be a duplicate, but take no action:
```html
<p><strong>Possible Duplicate</strong></p>
<p>This ticket may be a duplicate of <a href="https://your-org.sonar.software/app#/tickets/show/{original_id}">#{original_id}</a> ({original_subject}).</p>
<p>Both tickets are for <strong>{tower_site}</strong> tower site.</p>
<p>No action taken — please review and close if confirmed duplicate.</p>
```

#### Low Confidence (<50%) — Skip

Do nothing. The ticket is not a duplicate.

## GraphQL Operations

### Query: Search for existing open NOC tickets
```graphql
query {
  tickets(search: {
    integer_fields: [
      { attribute: "ticket_group_id", operator: EQ, search_value: <noc_group_id> }
    ]
    string_fields: [
      { attribute: "status", search_value: "OPEN", match: true },
      { attribute: "status", search_value: "PENDING_INTERNAL", match: true }
    ]
  }) {
    entities {
      id
      subject
      status
      created_at
      description
      ticket_categories { entities { id name } }
    }
  }
}
```

### Mutation: Reassign ticket to CS Level 2
```graphql
mutation {
  updateTicket(id: {ticket_id}, input: {
    ticket_group_id: <cs_level_2_id>
  }) {
    id
    ticket_group { id name }
  }
}
```

### Mutation: Post duplicate comment
```graphql
mutation {
  createTicketComment(input: {
    ticket_id: {ticket_id}
    body: "<p><strong>Duplicate Ticket Detected</strong></p>..."
  }) {
    id
  }
}
```

## curl Template

```bash
# Source the token
export $(grep SONAR_TOKEN .env | xargs)

# Search for existing tickets
curl -s -X POST 'https://your-org.sonar.software/api/graphql' \
  -H "Authorization: Bearer $SONAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"<graphql query>"}'
```

## Posting Comments — Use the Helper (Required)

```bash
cat > /tmp/comment.html << 'HTMLEOF'
<p><strong>Duplicate Ticket Detected</strong></p><p>This ticket appears to be a duplicate of <a href=&quot;https://your-org.sonar.software/app#/tickets/show/12344&quot;>#12344</a></p>
HTMLEOF
python3 scripts/sonar_comment.py 12345 /tmp/comment.html --verify
```
See the `comment-formatting` skill — use `&quot;` for all href/style quotes, never `BODYPLACEHOLDER` replace hacks. Helper verifies stored body.

## Legacy Templates (Deprecated — use helper instead)

```python
import json, subprocess, os
token = subprocess.run(
    ['bash', '-c', 'source .env && echo $SONAR_TOKEN'],
    capture_output=True, text=True
).stdout.strip()
html = '<p><strong>Duplicate Ticket Detected</strong></p>'
mutation = f'mutation {{ createTicketComment(input: {{ ticket_id: {ticket_id}, body: "{html}" }}) {{ id }} }}'
query = json.dumps({"query": mutation})
```

## JSON Escaping for HTML (Deprecated)

**Method 1 — Single-Quoted Outer JSON (fragile):**
```bash
-d '{"query":"mutation { createTicketComment(input: { ticket_id: 12345, body: \"<p>Duplicate of <a href=\\\"https://your-org.sonar.software/app#/tickets/show/12344\\\">#12344</a></p>\" }) { id } }"}'
```

**Method 2 — Use helper with `&quot;` (required):**
```python
html = '<p>Duplicate of <a href=&quot;https://your-org.sonar.software/app#/tickets/show/12344&quot;>#12344</p>'
```

## Edge Cases

1. **Same customer, same site, same issue**: Might be legitimate recurring issue → lower confidence (add -15% if customer name matches)
2. **Different issue types on same site**: Not duplicates → skip
3. **Ticket with no clear tower site**: Cannot determine duplicates → skip
4. **Multiple potential duplicates**: Match against the oldest open ticket
5. **Ticket already has duplicate comment**: Check if already handled → skip
6. **Ticket created directly in NOC group**: These are typically not duplicates → skip (check if ticket was transferred from CS by looking at categories or first comment)

## Workflow Integration

### For New Tickets (Webhook-triggered)

When a new ticket arrives in NOC group:
1. Extract tower site from subject
2. Run duplicate detection algorithm
3. If high confidence duplicate → auto-handle
4. If medium confidence → post informational comment
5. Continue with normal ticket processing

### For Batch Processing

When reviewing all open NOC tickets:
1. Query all open NOC tickets
2. Group by tower site
3. Within each group, identify potential duplicates
4. Handle high-confidence duplicates
5. Report findings

## Example: Golf Duplicate Detection

**New ticket:** <ticket-id-1> "[Bravo] golf pppoe - [Slow Speeds]"
**Existing ticket:** <ticket-id-2> "(Bravo) golf pppoe/ slow tower"

**Comparison:**
- Tower site: golf = golf ✓
- Issue type: slow speeds = slow speeds ✓
- Fiber network: Bravo = Bravo ✓
- Time proximity: 40 days apart (within 30-day window? No, but still relevant)
- Different customers: Yes ✓

**Confidence calculation:**
- Base: 80% (tower + issue type match)
- Same fiber network: +10%
- Different customers: +10%
- **Total: 100%** → High confidence → Auto-handle

**Action:** Post comment, reassign to CS Level 2

## Success Metrics

- Reduce duplicate ticket handling time by 80%+
- Zero false positives (incorrectly merging non-duplicates)
- Clear audit trail via comments
- Customers directed to original ticket for faster resolution

## Notification Rules After Duplicate Handling

**After handling a duplicate, follow these notification rules:**

### High-Confidence Duplicate (≥85%) — NO Notification
- Status: `handled_duplicate`
- Action: Post comment, reassign to CS Level 2
- **Do NOT send Google Chat notification**
- Why: Ticket is already resolved, notification is noise

### Medium-Confidence Duplicate (50-84%) — SEND Notification
- Status: `needs_human`
- Action: Post informational comment only
- **SEND Google Chat notification**
- Why: Human judgment needed to confirm

### Low-Confidence Duplicate (<50%) — Not a Duplicate
- Status: `categorized` or `needs_human`
- Action: Continue normal processing
- **SEND Google Chat notification if needed**
- Why: Normal processing rules apply

**Summary:**
- `handled_duplicate` → Silent (no notification)
- All other outcomes → Notify Google Chat
