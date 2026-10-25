---
name: correlated-auto-return
description: Use when a DEGRADED bottleneck analysis has correlated downstream tickets sharing the same critical hop — evaluate downstream auto-return to CS Level 2 with confidence scoring and single-bounce anti-loop.
---

# Correlated Auto-Return — DEGRADED Downstream → CS Level 2

> **Examples:** addresses and names in this skill belong to a fictional network. This organization's real ones come from `python3 scripts/resources.py list` and `site-pack/`; never reuse an example.

When a tower’s network path is DEGRADED and another active ticket is downstream of the same critical device, the downstream will not clear until the upstream is fixed. Bounce the downstream to CS with a link to the upstream.

## When to Run

As **Step 3.6b** immediately after `correlated-issues` correlation (and before `3.7 IHN`):

- **Run**: current analysis `DEGRADED` with ≥1 `critical` red hop, correlation found ≥1 other NOC ticket whose stored analysis comment (`Direct Path Analysis`, legacy `Network Bottleneck Analyzer`) contains the same bottleneck device (exact IP + name).
- **Skip**: `HEALTHY`/`failed`/`deferred`, no correlation, weather/night excluded, already `correlated_bounced` marker.

## Eligibility Gates

### Gate A — Current DEGRADED

- `summary.status==degraded` (case-insensitive), `bottleneck_count>0`, at least one hop red (`comment-formatting:58` critical) — not warning-only amber. Extract bottleneck devices = hops with `color:red` + `is_bottleneck`/`cpu>80`/`throughput limited`.

### Gate B — Correlated Found With Real Hop Match

Via `correlated-issues` workflow: search all active NOC `OPEN/PENDING_INTERNAL` for analysis comments (`Direct Path Analysis`, legacy `Network Bottleneck Analyzer`), parse `hops[]` via `comment-formatting:122`. Require **exact device name (case-insensitive) AND IP** match to current red device (e.g., `<upsite> pppoe` + `10.0.12.1`). Name-only without IP → fuzzy → skip (would be low confidence).

### Gate C — Weather / Peak

Re-check both tickets: subject+description+ALL `ticket_comments.body` (paginator 20) for weather (`rain|storm|weather` + the weather category `sonar.categories.weather`) → if either has weather, skip bounce. Night/peak: if either has the night/peak category (`sonar.categories.night_peak`) or `night check|peak hours` and `isPeakWindow==false` → defer via `peak-scheduler` queue, don't bounce now. Only bounce when not excluded or at peak (`isPeakWindow true`).

### Gate D — Not Already Bounced

Before bouncing downstream, fetch `ticket_comments` DESC 5 for downstream candidate and check for marker `Correlated Upstream Issue — Auto-Returned` or prior status `correlated_bounced`. If found → already bounced once → skip, leave `needs_human`. Also check current upstream not already `correlated_bounced`.

## Downstream Determination (internet-source definition)

Downstream = farther from internet feed. Example: feed `Internet → … → <UpSite> → <DownSite> → CPE`, so `<DownSite>` is downstream of `<UpSite>`. `<DownSite>`’s path hop `<upsite> pppoe` at hop 3 transits through `<UpSite>`; `<UpSite>`’s own path has `<upsite> pppoe` at hop1 (its CPE *is* the device).

**Primary rule (works best)**: if `correlated.analyzer_site == bottleneck_short` (case-insensitive substring, e.g., correlated site `<UpSite>` == device `<upsite> pppoe`) → correlated is upstream root at the device → **keep upstream root, bounce downstream current** (the transiting ticket). In `#<ticket-id> <DownSite>` vs `#<upstream-ticket-id> <UpSite>`, the upstream ticket’s analyzer_site `<UpSite>` equals bottleneck `<upsite> pppoe` → it is the upstream root at the device, and `#<ticket-id> <DownSite>` is downstream where the device appears at hop3 → bounce `<DownSite>` (current).

**Fallback** if neither analyzer_site equals device (both are transit): bounce the one with larger `k` (more hops from its CPE to device) → farther downstream. Final tie-break `created_at` older = upstream root.

Log `k_current/k_corr/total_hops` and `downstream_reason` (`analyzer_site_match` / `larger_k` / `older_upstream`).

For current batch, we only bounce the **current ticket** when it is downstream of the correlated upstream. If correlated tickets are downstream of current, we do NOT bounce them in this run — they will be bounced when *they* are processed as current and find current as upstream. This avoids double-bounce in one run.

## Confidence Scoring (tunable `CORRELATED_CONFIDENCE_THRESHOLD` default 75)

Lower threshold (vs IHN 80) since we match the real hop list.

| Signal | Points |
|--------|--------|
| IP exact match (e.g., `10.0.12.1`) | +30 |
| Name exact (case-insensitive `<upsite> pppoe`) | +25 |
| Both tickets `DEGRADED` with same device `critical` | +15 |
| Shared `Network - X` / `fiber` category | +10 |
| Both `total_hops>5` (path not truncated) | +10 |
| Fuzzy name-only without IP | -30 |
| Stale correlated >30d `PENDING_INTERNAL` | -40 |
| No `total_hops` present | -20 |

Clamp 0-100. Log `correlated_confidence_breakdown`. Bounce only if `≥75`.

## Action on High Confidence — Bounce Downstream Quietly

**No category changes.** Keep downstream ticket’s existing `ticket_category_ids` as-is; only move group.

**CRITICAL ordering: gate FIRST, then comment.** The correlation comment contains the anti-loop marker `Correlated Upstream Issue`; `scripts/sonar_bounce.py` scans the last 5 comment bodies for that marker and exits 5 (refusing the move) if it is already there. Never post the comment before running the gate.

1) **Execute the group move ONLY via the deterministic gate** (enforces circuit breaker + kill switch + anti-loop; NEVER write the `updateTicket` group change yourself):

```bash
# dry-run: gates only, no write (any non-zero exit -> needs_human: 3 breaker, 4 config/kill switch, 5 anti-loop, 6 confidence; table in the scripts/sonar_bounce.py docstring)
python3 scripts/sonar_bounce.py bounce <downstream_id> --reason CORRELATED \
  --upstream-id <upstream_id> --confidence <corr_conf> --evidence "<csv evidence>" --dry-run
# real run without --dry-run when gates pass
```

Keep `OPEN/PENDING_INTERNAL`. Silent (no Google Chat at bounce time; correlation comment already posted on upstream).

2) **After the gate succeeds, post single new comment on downstream** (use `scripts/sonar_comment.py` with `&quot;` for href, never raw `"` or `BODYPLACEHOLDER` — preserve the already-present analysis header (either marker) from the prior analysis — this is an additional correlation auto-return comment, allowed as second comment for DEGRADED path):
```html
<p><strong>Correlated Upstream Issue — Auto-Returned to CS</strong></p>
<p>This tower is downstream of same bottleneck as <a href="https://your-org.sonar.software/app#/tickets/show/{upstream_id}">#{upstream_id} {upstream_site}</a> (<strong>{device} {ip} hop {k}</strong> — {critical reason} in analysis).</p>
<p>Downstream of <strong>{device}</strong>; upstream fix will clear this. Please check back on correlated ticket #{upstream_id} — reopen with note if distinct issue after upstream resolved.</p>
<p>Confidence: {corr_conf}% • Device: {device} {ip}</p>
```

**Return status** for batch JSON: downstream gets `correlated_bounced` (silent, like `auto_bounced_ihn`), upstream stays `needs_human`/`categorized`. Example:
```json
[
  {"ticket_id":"<ticket-id>","status":"correlated_bounced","tower_site":"<downsite>","analyzer_site":"<DownSite>","bottleneck_analysis":"completed","correlated_bounced":true,"upstream_id":"<upstream-ticket-id>","correlated_device":"<upsite> pppoe 10.0.12.1","correlated_confidence":90,"notification_sent":false},
  {"ticket_id":"<upstream-ticket-id>","status":"needs_human","bottleneck_analysis":"completed","notification_sent":true}
]
```

If no downstream (current is upstream root), just post `Correlated Issues Found` as before and stay `needs_human`.

## Env

- `CORRELATED_CONFIDENCE_THRESHOLD` default 75 (int)
- Uses `PEAK_WINDOW_*` for night/peak check, `IHN_*` not needed here but shares weather scan.

## Verification

Confirm the bounce gate executed the move and moved the ticket to the CS Level 2 group (the script verifies its own mutation readback), and that the group move happened BEFORE the "Correlated Upstream Issue" comment was posted (see ordering note above). Fetch the comment back to verify `href` intact vs double-escaped `&quot;`.
