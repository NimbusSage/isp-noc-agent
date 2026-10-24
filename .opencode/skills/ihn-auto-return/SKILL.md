---
name: ihn-auto-return
description: Use when a slow-tower ticket has a perfect HEALTHY bottleneck analysis (all hops green, zero warnings/criticals) and you need to decide whether to auto-return it to CS Level 2 as IHN/tower-connection rather than network issue. Covers eligibility, peak-aware exclusion, tower-correctness confidence scoring, single-comment formatting, category replacement, and single-bounce anti-loop.
---

# IHN Auto-Return — Healthy Slow-Tower → CS Level 2

Auto-return tickets to CS when the network path is proven healthy end-to-end. The issue is then IHN (In-Home Networking) or tower-connection, not a network/backhaul problem on the provider side.

## When to Run

Run as **Step 3.7** after bottleneck analysis, **before** correlation/notification:

- **Run**: ticket is slow speeds/slow tower, analysis `completed`, result is **perfect HEALTHY** (see definition), not excluded by weather, peak gate handled.
- **Skip**: analysis failed/not run, status is DEGRADED/WARNING, weather exclusion hit, peak gate queued (see `peak-scheduler`), already handled duplicate, already bounced once (see Anti-loop).

## Eligibility Gates (all must pass to reach scoring)

### Gate A — Eligible type

- `ticket_group_id == <noc_group_id>` (NOC) or name contains "noc", status is OPEN/PENDING_INTERNAL
- Categorized as **NOC - Slow Speeds** (`sonar.categories.slow_speeds`) OR subject/description/comments match `/slow|speed|throughput|bandwidth|latency/i` as primary issue
- NOT already `handled_duplicate` (≥85% duplicate) and NOT `auto_closed*`

### Gate B — Weather exclusion (ABSOLUTE for bounce, but NOT for scheduled peak analysis)

Check **subject + description + ALL ticket_comments.body** (fetch with `ticket_comments` paginator page 1 records_per_page 20 sorted ASC/DESC). If ANY hold, mark weather-excluded.

- Weather category (`sonar.categories.weather`, NOC - Rain Fade Issues) present → exclude
- Keywords (case-insensitive): `rain`, `storm`, `weather`, `precipitation`, `rain fade`, `weather fade`, `wet weather`, `bad weather`, `thunder`, `lightning`, `wind damage`, `rain-related`, `storm-related`
- **LLM reasoning override**: if the model reads phrases like "went out during the storm last night, still slow today", "customer says it only drops when it rains", "wet weather in area", treat as weather even if literal keyword list misses — be conservative and exclude. Log the phrase that triggered exclusion.

**Effect:**
- **Normal (non-peak) flow**: if weather-excluded → STOP, return `needs_human`, do NOT score/bounce, post path comment only (no IHN block).
- **Scheduled peak retest** (`peak-scheduler` queue drain at 20:05 CT): **still run the bottleneck analyzer at peak regardless of weather keywords** (a "tuned for rain fade" tuning note once falsely cancelled a peak run). At peak, if `weather_excluded==true` → still post full HEALTHY/DEGRADED path analysis, but do NOT bounce (return `needs_human` with weather note + `weather_excluded:true`). Analysis is always posted; only bounce is blocked.

Weather exclusion always blocks **bounce**, but scheduled peak analysis is never cancelled due to keywords.

### Gate C — Night/Peak conditional exclusion + scheduler handoff

Night/peak tickets require a **peak-window test**. Only bounce them if you actually tested inside `20:00-21:00 America/Chicago` (8-9 PM Central).

- Detect night/peak if **any**: the night/peak category (`sonar.categories.night_peak`, NOC - Night Checks) present OR corpus (subject+description+all comments) matches `/night\s*check|n\s*check|night\s*checks|peak\s*hours?|peak\s*time|nighttime|overnight|evening\s*speeds?|congestion\s*at\s*peak|7\s*pm|19:00|20:00|night\s*peak/i` (normalize for strings like `[NIGHT CHECK]`)
- Check current CT time via `date` in America/Chicago: `TZ=America/Chicago date +%H:%M` → `HH:MM`
- **isPeakWindow = 20:00 ≤ now < 21:00 CT**
- **Anti-loop**: before enqueue, fetch `ticket_comments` DESC 20 and check for marker `Peak Window Retest` / `Peak retest at`. If found → ticket already got its scheduled peak analysis — **do NOT re-enqueue** (prevents testing over and over after job done). Return `needs_human` with note.
- Logic:
  - If **not** night/peak → ignore Gate C, proceed to Gate D (can bounce anytime).
  - If night/peak and `isPeakWindow == true` → **eligible to bounce at peak** — proceed to Gate D (you are doing the required peak check now). That peak HEALTHY result satisfies the night-check requirement.
  - If night/peak and `isPeakWindow == false` and no prior peak marker → **do NOT bounce now**. Instead follow `peak-scheduler` skill: enqueue for next peak window, post a category-only comment "Queued for peak-window retest", categorize, return `queued_for_peak` (or `needs_human` with `peak_queued:true`), and send Google Chat noting queued. Do NOT score. The scheduler will re-run analyzer at 20:05 CT and bounce then if healthy (single test per ticket, then dequeue).

### Gate D — Perfect HEALTHY (all hops green, ZERO warnings/criticals)

All must hold on `GET /api/results/<sid>` JSON:

- `summary.status` case-insensitive == `healthy`
- `summary.bottleneck_count == 0`
- `bottlenecks` array is empty (no critical/warning objects)
- No hop would render with red or orange per `comment-formatting` (i.e. no hop has `is_bottleneck`, no high `cpu_load>80`, no high `interface_utilization>80`, no bandwidth collapse >50% between hops)

Any warning/amber/orange fails Gate D — do NOT bounce (post healthy-but-not-perfect path comment normally and fall through to `needs_human`/`categorized`).

## Confidence Scoring — Tower Correctness (only after Gates A-D pass)

Weather exclusion already handled; peak was handled at Gate C. Only tower confidence remains.

Score 0-100:

| Signal | Points | How to verify |
|--------|--------|---------------|
| **Exact literal match** `lower(parsed_site)==lower(analyzer_site)` (e.g., `lima==lima`) | **+40** | Compare parsed ticket site vs picked analyzer site |
| First note has explicit `Tower: <name>` and equals parsed ticket site (`ticket-context`) | +30 | Fetch first comment ASC records_per_page 1, look for `Tower:` |
| First note has `TH:` / `Tower Host:` / `Relayed from` resolving tech-initials ambiguity and analyzer pick matches that host | +25 | Use the analysis comment's site pick vs the TH field |
| Search `GET /api/sites/search?q=<last>` returned **exactly 1** match | +20 | Count results array; if not 1 but **contains exact literal** award +15 instead (not +20) |
| Search contains exact literal even with multiple results (e.g., `lima` in 2) | +15 | Check if any result `name` lower equals `parsed_site` lower |
| Tech-initials-style `[A-Z][last]` pattern resolved via ticket comments, not guessed (no TH field → guess is -40, not +15) | +15 | Check abbreviation vs TH presence |
| Analyzer succeeded with `total_hops>5` (proves inventory existence, not spurious) | +30 | From `GET /api/results/<sid>` hops length |
| Analyzer site substring overlap ticket site (case-insensitive) | +10 | Compare `analyzer_site` vs parsed site |
| Fallback: search empty then retried with broader term | -30 | Log |
| No first note / no Tower/TH field found at all | -40 (-20 if `exact_literal_match` true, to avoid double-penalizing literal sites like `lima` with image-only first note) | Log |

Clamp 0-100. Log `confidence_breakdown` as JSON in webhook.log and return field. Example `lima` (ticket `<ticket-id>`): `40 exact +0 Tower +0 TH +0 exactly1 +15 contains_exact +0 initials +30 hops>5 +10 substring -20 no_tower_with_exact =75` → with `total_hops` bonus the example is near threshold; adding any other signal (e.g., `Network - Alpha` already present) reaches **80**. `exact_literal_match` is **+40** and the threshold is **80** — so `lima` passes at 80 exactly (exact alone 40 still fails, needs hops+contains).

**Env var**: `IHN_TOWER_THRESHOLD` **default `80`**. Each distinct score should be tunable via env (see `peak-scheduler` for its own threshold if needed; duplicate thresholds use `DUPLICATE_HIGH_THRESHOLD`).

**Decision**:
- `tower_conf >= IHN_TOWER_THRESHOLD (80)` → **BOUNCE** (reassign).
- `< threshold` → **do NOT bounce** — post the unified path comment *without* IHN section (or with a note "Healthy but low tower confidence 62% — needs human"), return `needs_human`/`categorized` and send Google Chat with `Tower confidence low`.

**Note for `lima` vs `TLima`**: `lima` literal exact (`lima==lima`) scores high; `TLima` → `Tom Lima` would score via `TH:`/`Tower:` + exact literal `TLima != Tom Lima` so no literal bonus, but TH resolution would supply it. Test live search-only: `GET /api/sites/search?q=lima` (expect `lima` + `Tom Lima` 2 hits), `q=TLima` → `Tom Lima` single, to confirm.

## Action on High Confidence — Single Unified Comment + Reassign (silent)

**IMPORTANT: One comment per agent run.** Do NOT post the normal `comment-formatting` healthy comment *plus* a second IHN comment. Merge into one.

### Sonar mutations (order: GATE FIRST, comment SECOND)

**CRITICAL ordering: run the deterministic gate, THEN post the comment.** The `IHN` unified comment contains the anti-loop marker text `Automated NOC Review — ...`. The bounce gate (`scripts/sonar_bounce.py`) scans the ticket's last 5 comment bodies for that marker (`ANTI_LOOP_MARKERS`) and refuses with exit 5 if it is already present. Posting the comment before bouncing means the gate matches your own brand-new comment and blocks the group move every time (past tickets were blocked this way, leaving HEALTHY IHN tickets stuck in NOC). Correct sequence:

1) **Run the gates + execute the group/category change ONLY via the deterministic gate** (enforces circuit breaker + `BOUNCE_AUTONOMOUS_ENABLED` kill switch + anti-loop markers; NEVER write the `updateTicket` group change yourself):

```bash
# dry-run first: all gates, prints the mutation, no write. Non-zero exits (table: docstring of
# scripts/sonar_bounce.py): 2 API error, 3 breaker open, 4 blocked by config (kill switch off or
# unresolved ids), 5 anti-loop marker present, 6 confidence missing or below threshold
# -> fall back to needs_human with the matching escalation_reason
python3 scripts/sonar_bounce.py bounce <ticket_id> --reason IHN --confidence <tower_conf> --dry-run
# if gates pass, execute for real (add/remove category ids resolve from env, then
# site-pack/profile.json; pass --add-categories/--remove-categories only to override):
python3 scripts/sonar_bounce.py bounce <ticket_id> --reason IHN --confidence <tower_conf> --evidence "<csv evidence>"
```

The script fetches current categories, applies removes/adds (category ids REPLACE all), moves the ticket to the CS Level 2 group, and verifies the mutation. Honor every non-zero exit code — fall back to `needs_human` with the matching `escalation_reason` (`circuit_breaker_open` for 3, `kill_switch_off` for 4, `anti_loop_marker` for 5, else `bounce_gate_error`); do NOT retry the mutation manually.

2) **Only after the gate succeeds, post the single unified HTML comment** (see Template below) via `scripts/sonar_comment.py` (use `&quot;` for all `style=` attrs, never raw `"` or `BODYPLACEHOLDER` — see `comment-formatting`). Example: `cat > /tmp/comment.html << 'HTMLEOF'` with `&quot;` HTML then `python3 scripts/sonar_comment.py <id> /tmp/comment.html --verify`. Verify response has `id` and stored body contains marker (helper does this). From this point on, the marker in the comment is what protects the ticket against a future re-bounce — exactly as intended.

If the gate blocks (exit 5) and you have NOT yet posted any marker comment: fall back to `needs_human` and post the path comment WITHOUT the IHN block (no marker text), noting the block. If you have already (mistakenly) posted a marker comment before bouncing, the gate stays blocked — NEVER edit or delete comments to evade the gate; leave the ticket with a clarifying note for the human instead.

Do NOT close the ticket — keep OPEN/PENDING_INTERNAL for CS triage.

### Unified Comment Template (single post, preserves correlation marker)

Keep the analysis header (`Direct Path Analysis - {analyzer_site}`; legacy comments preserve their original marker) so `correlated-issues` can still find it. Append IHN assessment at bottom.

```html
<p><strong>Direct Path Analysis - {analyzer_site}</strong></p>
<p><strong>Status: HEALTHY</strong> — No bottlenecks end-to-end ({hops} hops, all green)</p>
<p><strong>Path Analysis — {analyzer_site} → Internet:</strong></p>
<p>
<strong>Hop 1:</strong> {device_name} ({ip}) — <span style=&quot;color:green&quot;>{down} down / {up} up</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;<span style=&quot;color:green&quot;>→ ✓ {model}, healthy</span><br><br>
<!-- repeat per hop, ALL green per comment-formatting -->
</p>
<p><strong>Automated NOC Review — No Network Issue Detected</strong></p>
<p>Analyzer returned <strong>HEALTHY</strong> for <strong>{analyzer_site}</strong> ({tower_site} tower) — zero warnings/criticals end-to-end. Path above.</p>
<p><strong>Assessment:</strong> Issue is most likely <strong>IHN (In-Home Networking) or tower-connection related</strong>, not a network/backhaul issue.</p>
<p><em>Disclaimer: This ticket was returned automatically by the NOC Agent. If there is another issue besides speeds, or speeds remain slow after IHN checks, please send back to NOC with a note describing the specific issue — your feedback helps improve the agent.</em></p>
<p>Confidence: {tower_conf}% • Analyzer: {analyzer_site} • Ticket #{id}</p>
```

Use `&quot;` for all style attrs (`comment-formatting:119`), no `<table>`. No recommendations section. No multipath. No preamble.

**Return status**: `auto_bounced_ihn` (or `auto_bounced_ihn_peak` when bounced inside peak). **Silent** — do NOT send Google Chat (new `google-chat` Rule for this status). Log to webhook.log with `confidence`, `confidence_breakdown`, `peak_window:true/false`.

### Return JSON

```json
[
  {"ticket_id":"12345","status":"auto_bounced_ihn","tower_site":"tlima","analyzer_site":"Tom Lima","bottleneck_analysis":"completed","confidence":95,"confidence_breakdown":{"tower_explicit":30,"th_resolved":25},"peak_test":false,"notification_sent":false},
  {"ticket_id":"12346","status":"categorized","tower_site":"hotel","analyzer_site":"Hotel","bottleneck_analysis":"completed","confidence":55,"peak_test":false,"notification_sent":true}
]
```

### Anti-Loop — Single Bounce Max

Marker for prior bounce: existing comment body contains `Automated NOC Review — No Network Issue` **OR** (`ticket_categories` ∋ the cx/th-side category and group already CS Level 2 with that marker). Check `ticket_comments` paginator page 1 records_per_page 5 DESC before bouncing.

- If marker found → this is a **return**. Do NOT bounce again.
- Instead **re-run bottleneck analyzer once more** on same `analyzer_site`, post a **re-evaluation unified comment** titled `Re-evaluation — Returned from CS (Previously Auto-Bounced)` with fresh path + note:

```
<p><strong>Re-evaluation — Returned from CS (Previously Auto-Bounced)</strong></p>
<p>Previously bounced as IHN. Re-ran peak/off-peak analysis on {analyzer_site} at {timestamp CT} — result: {HEALTHY|DEGRADED}.</p>
<p>Not re-bouncing per single-bounce policy — leaving for NOC tech. Previous confidence: {old_conf}%.</p>
```

  Append path, and set status `needs_human` with `notification_sent:true` (Google Chat alert with "Returned after IHN bounce — re-analyzed"). Never auto-bounce same ticket_id twice.

### Interaction With Other Steps

- **Duplicate**: high-confidence duplicate reassigns before this step — skip IHN.
- **Correlate**: HEALTHY → skip correlate (no device to match) per `correlated-issues`.
- **Night/Peak**: handoff to `peak-scheduler` queue when outside window — do not score; but once queued, the **scheduled** peak retest always runs analyzer even if weather keywords appear (only bounce is blocked).
- **Weather**: absolute exclusion for bounce — but during scheduled peak retest, analyzer still runs and posts (per Gate B fix).

## Verification

After posting, fetch the comment back via `ticket_comments` and confirm `style=&quot;color:green&quot;` rendered vs `style=\"color:green\"` double-escape. Confirm the bounce script reported the CS Level 2 group / cx-th category (the script verifies its own mutation readback). Order check: bounce executed BEFORE the unified comment was posted (see ordering note above).

## Env Vars

- `IHN_TOWER_THRESHOLD` default **80** (exact literal +40 alone still fails, but exact+contains+hop>5+substring reaches 80 for a literal-site ticket)
- `DUPLICATE_HIGH_THRESHOLD` / `DUPLICATE_MED_THRESHOLD` already defined (default 85/50) — make sure server reads them
- `IHN_EXACT_LITERAL_BONUS` (internal 40) — part of `IHN_TOWER_THRESHOLD` scoring

Peak window vars belong to `peak-scheduler` (TZ, start/end) but IHN respects them for Gate C.
