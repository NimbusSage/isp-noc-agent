---
name: comment-formatting
description: Use when posting bottleneck analyzer results as Sonar ticket comments. Formats the analysis into an HTML comment with colored callouts, hop-by-hop path analysis, and condensed issue summaries for ISP NOC tickets.
---

# Comment Formatting — Bottleneck Analyzer Results

## Do Not Post Failed Results

**IMPORTANT:** If the analyzer test fails (e.g., site not found, API error, timeout, connection refused), do NOT post a comment to the ticket. A failed analysis result is not useful to anyone — it just creates noise. Log the error and move on.

**Known failure indicators — stop polling immediately, no results will come:**
- `"Router not in UISP"` / `"Checking Mikrotik import list"` — site not in analyzer inventory. Do NOT keep retrying.

## SINGLE Unified Comment Rule (required for `ihn-auto-return` / `peak-scheduler`)

**When the agent bounces a ticket (auto_bounced_ihn) or does a peak retest, it MUST post exactly ONE comment for that ticket in that run.** Do NOT post the standard path comment plus a second IHN paragraph — merge them. The unified comment keeps the `Direct Path Analysis - [site]` header as the first line so `correlated-issues` still indexes it (legacy stored comments keep the `Network Bottleneck Analyzer -` predecessor header, which the correlator still accepts). Off-peak night/peak tickets that are deferred (queued) post NO analyzer comment at all (Option A).

### Unified IHN comment — append to the template below when bouncing

After the standard `Path Analysis` + optional `Issues` blocks, append:

```html
<p><strong>Automated NOC Review — No Network Issue Detected</strong></p>
<p>Analyzer returned <strong>HEALTHY</strong> for <strong>[analyzer_site]</strong> ([tower_site] tower) — zero warnings/criticals end-to-end. Path above.</p>
<p><strong>Assessment:</strong> Issue is most likely <strong>IHN (In-Home Networking) or tower-connection related</strong>, not a network/backhaul issue.</p>
<p><em>Disclaimer: This ticket was returned automatically by the NOC Agent. If there is another issue besides speeds, or speeds remain slow after IHN checks, please send back to NOC with a note describing the specific issue — your feedback helps improve the agent.</em></p>
<p>Confidence: [tower_conf]% • Analyzer: [analyzer_site] • Ticket #[id]</p>
```

For a **peak retest** bounce, prefix the IHN block with `Peak Window Retest (8:xx PM CT) —`. For a **re-evaluation after CS return** (single-bounce anti-loop), replace the IHN block with the `Re-evaluation — Returned from CS (Previously Auto-Bounced)` block from `ihn-auto-return`.

## Comment Structure Template

```html
<p><strong>Direct Path Analysis - [site_name]</strong></p>
<p><strong>Status: [HEALTHY/DEGRADED]</strong> — [one-line summary]</p>

<p><strong>Path Analysis — [site_name] → Internet:</strong></p>
<p>
<strong>Hop 1:</strong> [device_name] ([IP]) — [bandwidth_down] down / [bandwidth_up] up<br>
&nbsp;&nbsp;&nbsp;&nbsp;→ [bottleneck callout or ✓ healthy]<br><br>

<strong>Hop 2:</strong> [device_name] ([IP]) — [bandwidth_down] down / [bandwidth_up] up<br>
&nbsp;&nbsp;&nbsp;&nbsp;→ [bottleneck callout or ✓ healthy]<br><br>

<!-- Repeat for each hop -->
</p>

<!-- ONLY include if status is DEGRADED — condensed, not the focus -->
<p><strong>Issues:</strong> [severity_count critical], [severity_count warning]</p>
<ul>
  <li><strong>[CRITICAL/WARNING]:</strong> [device] ([IP]) — [one-line summary]</li>
</ul>
```

## Path Analysis Rules

- Each hop is its own `<strong>Hop N:</strong>` block — no tables, no grid
- Show bandwidth as `[down] down / [up] up` — consistent ordering

## Colored Callout Lines (Required)

Every hop must have a colored callout line on the second line. Sonar renders HTML — use this for visual clarity.

### Healthy Hops — Green

Green bandwidth text + green callout:

```html
<strong>Hop 2:</strong> xi pppoe (10.0.10.46) — <span style="color:green">474.6 Mbps down / 376.8 Mbps up</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:green">→ ✓ ARM RB4011iGS+5HacQ2HnD, healthy</span><br><br>
```

### Critical Bottleneck Hops — Red

Red bandwidth text + red callout:

```html
<strong>Hop 1:</strong> omicron backhauls (10.0.11.1) — <span style="color:red"><strong>61.5 Mbps down / 48.7 Mbps up</strong></span><br>
&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:red"><strong>→ ▼ MIPSBE hEX PoE — architecture bottleneck, throughput limited</strong></span><br><br>
```

### Warning Hops — Amber/Orange

Amber/orange callout (no bandwidth color change):

```html
<strong>Hop 2:</strong> pi pppoe (10.0.12.1) — 9.5 Mbps down / 131.7 Mbps up<br>
&nbsp;&nbsp;&nbsp;&nbsp;→ ⚠ ARM RB3011UiAS, limited throughput<br><br>
```

### Fiber Backbone Hops — No Bandwidth

No bandwidth shown, just latency in callout:

```html
<strong>Hop 5:</strong> rho fiber router (10.0.13.1)<br>
&nbsp;&nbsp;&nbsp;&nbsp;→ fiber backbone, 6.6ms<br><br>
```

### Callout Content Rules

- Include device model and architecture in the callout for context (e.g., "MIPSBE hEX PoE bottleneck", "ARM RB4011, healthy")
- Latency: show inline if >30ms, otherwise omit to reduce noise
- CPU/Mem: only show if a bottleneck contributor (>80% CPU, >90% mem), otherwise omit

## Issues Found Rules

- **Collapsed below Path Analysis** — the path is the star, issues are supplementary
- Condensed to a one-liner header: `Issues: 4 critical, 6 warnings`
- Each item is a single `<li>` — device name, IP, one-line summary
- No duplicate info already shown in path callouts
- For HEALTHY tickets: skip the issues section entirely

## General Rules

- No `<table>` tags — Sonar strips/poorly renders them
- No recommendations — the analyzer isn't accurate enough yet; just present the data
- No multipath/load balance info — data isn't meaningful yet; skip this section entirely
- No preamble like "Analysis completed on..." — jump straight to the data
- Use `<br>` for line breaks within `<p>` blocks

## Posting Comments — Use the Helper (Required)

**DO NOT hand-roll curl/json escaping.** Use the canonical helper `scripts/sonar_comment.py`.

```bash
# 1. Write HTML to a file — use &quot; for ALL attribute quotes, never raw "
cat > /tmp/comment.html << 'HTMLEOF'
<p><strong>Direct Path Analysis - Alpha</strong></p>
<p><span style=&quot;color:red&quot;>→ ▼ bottleneck</span></p>
HTMLEOF

# 2. Post via helper (does json.dumps + verification; rejects BODYPLACEHOLDER; auto-collapses newlines to single line)
python3 scripts/sonar_comment.py 12345 /tmp/comment.html --verify
```

The helper handles `json.dumps` correctly, rejects literal `BODYPLACEHOLDER`, collapses newlines (GraphQL rejects literal `\n`), and verifies the stored body contains `Network Bottleneck Analyzer`.

### Why &quot;?

GraphQL string inside JSON: a raw `"` needs `\\\"` → `\"` → `"`. That double-escaping fails easily (4 failed attempts plus a placeholder bug where `payload_str.replace('"BODYPLACEHOLDER"', json.dumps(html))` missed due to escaped `\\"`). Using `&quot;` avoids any `"` in the HTML payload, so single `json.dumps` suffices.

### Forbidden (hard-rejected by helper)

* **Never** use a `BODYPLACEHOLDER` template + `replace()` hack — the `payload_str.replace('"BODYPLACEHOLDER"', ...)` pattern fails because `json.dumps` escapes quotes to `\\"BODYPLACEHOLDER\\"`. Helper exits 2 on any `bodyplaceholder` substring (case-insensitive).
* **Never** use raw `style="color:..."` — always `style=&quot;color:...&quot;`. Helper exits 2 on raw `style="`.
* **Never** write multiline HTML with literal `\n` — helper collapses to single line, but Sonar GraphQL `body:"..."` rejects `\n` as `Unterminated string` (39 newlines once forced bisect probes). Write `<<'HTMLEOF'` single-block is fine — helper will flatten, but keep file single-line when possible.
* **Never** post probe/test payloads (`test`, `line1`, `aaa×2000`) to a prod ticket — helper exits 2 if `len < 50`; use a dedicated test ticket or `--dry-run` mental check instead.

### Legacy Method 1 — Single-Quoted Outer JSON (Deprecated, do not use for new code)

```bash
-d '{"query":"mutation { createTicketComment(input: { ticket_id: 12345, body: \"<p><span style=\\\"color:red\\\">text</span></p>\" }) { id } }"}'
```
Kept for reference only — fragile, prefer the helper.

### Verification (Required)

After `createTicketComment`, the helper with `--verify` fetches the most recent comment and asserts:
* Body does NOT contain `BODYPLACEHOLDER` (any case, substring — not just `==`) — a past ticket had `bodyplaceholder` residue from a template
* Body has no literal `\n`/`\r` (helper collapses to single line — GraphQL rejects newline as `Unterminated string`, seen once with 39 newlines → probe posts)
* Body `len >= 50` (probe guard: earlier `test` / `line1 line2` probe comments of 24-28 chars were left behind as garbage)
* Body contains expected marker (`Direct Path Analysis`, legacy `Network Bottleneck Analyzer`, `Duplicate`, or `Correlated`)
* Body uses `&quot;` style (`style=&quot;color:...&quot;` renders as `style="color:..."` in Sonar; raw `style="color:..."` is hard-rejected). If verification fails, retry once then notify.
* Do NOT post incremental probes to the production ticket — helper will reject multiline/placeholder/short locally before any `curl`, preventing that garbage pattern.

**If the colored spans don't render in Sonar**, the escaping is wrong. Fetch the posted comment back and check if `style="color:..."` is intact vs `style=\\\"color:...\\\"` (double-escaped = broken).

## Search Pattern for Correlation

All path analysis comments start with `Direct Path Analysis - [site_name]`. Older comments in the ticket history use the predecessor header `Network Bottleneck Analyzer - [site_name]` — the `correlated-issues` skill searches BOTH so pre-existing tickets stay correlated.

**Do NOT change this format** — the correlated-issues skill depends on the exact structure:
- First line: `<p><strong>Direct Path Analysis - [site_name]</strong></p>`
- Status line: `<p><strong>Status: [HEALTHY/DEGRADED]</strong> — [summary]</p>`
- Hops: `<strong>Hop N:</strong> [device_name] ([IP]) — [bandwidth]<br>`

The `Direct Path Analysis` string (and its legacy predecessor) is the search key that makes correlation possible.
