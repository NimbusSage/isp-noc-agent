# Ticket Pipeline — How Tickets Flow Through the Agent

> Canonical step-by-step logic: [AGENTS.md](../AGENTS.md) (the agent's instruction file).
> Operator index: [README](../README.md). Related: [docs/webhook-api.md](webhook-api.md),
> [docs/governance.md](governance.md), [docs/permissions-policy.md](permissions-policy.md).

## End-to-end flow (summary table)

The full step-by-step workflow is documented in [AGENTS.md](../AGENTS.md) (the agent's instruction file). Summary:

| Step | Action | Outcome |
|------|--------|---------|
| 0 | **Passthrough / auto-close** | Sys Admin out-of-office auto-replies auto-closed. Sys Admin tickets forwarded to Google Chat. PBS success tickets auto-closed. PBS failures get a summary comment + alert. |
| 1 | **Fetch details** | Use N8N-provided data, or query Sonar API |
| 2 | **Duplicate detection** | >=`DUPLICATE_HIGH_THRESHOLD`% (default 85): auto-handle (comment + reassign). `DUPLICATE_MED_THRESHOLD`%–84%: informational comment. <50%: skip. |
| 3 | **Categorize** | Tag as rain fade, slow speeds, outage, or other |
| 3.5 | **Path analysis (live router triage)** | Slow-tower tickets only. `path-analyzer` traceroutes to internet, btests to fiber router hop-by-hop, direct /30 OSPF link tests, variance + true multipath (`dst=0.0.0.0/0` gateway count). Appends `wireless-health` inline if bad hop — single unified comment. Night/peak outside 8-9 PM CT deferred (Option A). |
| 3.6 | **Correlate issues** | DEGRADED analysis only. Finds other tickets downstream of the same bottleneck device. Skipped for HEALTHY (IHN path instead) or deferred peak. |
| 3.6b | **Correlated auto-return** | DEGRADED with exact IP+name shared red hop: downstream (internet-source topology, e.g., `<DownSite>` downstream of `<UpSite>`) auto-returned to CS Level 2 via gated `sonar_bounce.py` (gate runs BEFORE the correlation comment — the comment carries the anti-loop marker; posting it first blocks the gate exit 5), no category change, silent `correlated_bounced` (threshold `CORRELATED_CONFIDENCE_THRESHOLD` 75, single-bounce anti-loop). |
| 3.7 | **IHN auto-return** | Perfect HEALTHY only: weather absolute exclusion (scans all comments + LLM reasoning), peak conditional (night/peak outside window → queued_for_peak), all-hops-green + zero warnings, tower confidence scoring ≥`IHN_TOWER_THRESHOLD`% (80, +40 exact literal) → move to the CS Level 2 group via gated `sonar_bounce.py` (replace the slow-speeds/night-peak categories with the CX/TH-side category) FIRST, then post the single unified comment (the gate runs before the IHN-block comment because the comment carries the anti-loop marker `Automated NOC Review` — comment-first posting blocks the gate exit 5). Silent `auto_bounced_ihn(_peak)`. Low confidence → needs_human. Single-bounce anti-loop: returned tickets re-analyzed once, never re-bounced. |
| 3.8 | **Peak scheduler drain** | Server-timed: at next 20:05 CT drains `logs/pending_peak_checks.json` serially (10 min spacing), re-analyzes each at peak, bounces if HEALTHY. Bounce executes via gated `sonar_bounce.py`. |
| 3.9 | **Decision log + circuit breaker** | Deterministic (server-side). Every settled ticket appended to `logs/decisions/<YYYY-MM>.jsonl`. Breaker records outcome; if failure rate > `BREAKER_MAX_ERROR_RATE` over ≥`BREAKER_MIN_RUNS` runs, bounces are blocked (agent falls back to `needs_human`) + Chat alert. All bounce writes go through `scripts/sonar_bounce.py` (breaker + kill switch + anti-loop enforced; exit 3/4/5 → no bounce). |
| 4 | **Notify** | Silent for `handled_duplicate`, `auto_bounced_ihn(_peak)`, `correlated_bounced`, `queued_for_peak`, and auto-closed (`auto_closed`, `auto_closed_ooo`, `auto_closed_maintenance`). Google Chat card for everything else (incl. low tower confidence, needs_human, API errors). |
| 5 | **Return status** | `handled_duplicate`, `categorized`, `needs_human`, `passthrough`, `auto_closed`, `auto_closed_ooo`, `auto_closed_maintenance`, `pbs_failure`, `auto_bounced_ihn`, `auto_bounced_ihn_peak`, `correlated_bounced`, or `queued_for_peak` |

**Notification rules:** Successfully handled duplicates, IHN/correlated bounces, peak-queued tickets, and auto-closed tickets produce no notification (they're already resolved or deferred). Everything else triggers a Google Chat alert — including API errors, unknown tower sites, medium-confidence duplicates, low tower confidence, and any uncertainty.

Temporary site recovery is a separate operator workflow, not a webhook ticket step. See [`temp-site-connection`](../.opencode/skills/temp-site-connection/SKILL.md). The operator has authorized the explicitly requested `Temp connect <site>` workflow to make necessary temporary infrastructure-radio role, addressing, and OSPF changes, including a temporary customer interruption on a selected donor AP. Technical writes go through the live typed MCP executor (`temp-radio-*`, `temp-ospf-*`, `temp-mt-*`) under five gates (kill switch, transport flags, host allowlist, `execute:true`, per-incident exact-device bindings) — proven end-to-end. Curated operator-supplied `/30` pairs may bypass the Sheet only when explicitly recorded and checked against live device addresses/routes; never imply a Sheet reservation.

---

The full step-by-step workflow is documented in AGENTS.md (the agent's instruction file), together with the batch-processing and webhook-server sections.


---

## Historical worked examples

> The section below is preserved verbatim from the original root-level
> `TICKET_PROCESSING_WORKFLOW.md` (a one-time demo write-up against 2026 tickets):
> the step numbers/thresholds may lag the current AGENTS.md workflow — treat it
> as an illustration, not the source of truth.

# NocAgent Ticket Processing Workflow

## Overview

The NocAgent is an AI agent for an ISP NOC (Network Operations Center) that automates 95%+ of NOC admin responsibilities. It processes tickets according to a comprehensive workflow defined in AGENTS.md, following these steps:

1. **Step 0**: Check for special ticket types (passthrough, auto-close, etc.)
2. **Step 1**: Fetch ticket details from Sonar API
3. **Step 2**: Run duplicate detection using `duplicate-detection` skill
4. **Step 3**: Categorize tickets (rain fade, slow speeds, outage, other)
5. **Step 3.5**: Run path analysis (`path-analyzer`) for slow speed/slow tower tickets
6. **Step 3.6**: Correlate issues for DEGRADED analysis
7. **Step 3.6b**: Correlated auto-return for DEGRADED downstream tickets
8. **Step 3.7**: IHN auto-return for perfect HEALTHY tickets
9. **Step 3.8**: Peak scheduler for night/peak tickets
10. **Step 4**: Send Google Chat notifications if needed
11. **Step 5**: Return consolidated status

## Current Ticket Processing Results

Based on the workflow demonstration, here's how the system would process the requested tickets:

### Ticket <ticket-id-1>: `[PAPA] Quebec - slow tower.`

**Step 0: Special Ticket Type Check**
- Group: NOC (`<noc_group_id>`) - Not a special ticket type
- Not Out-of-Office auto-reply
- Not Sys Admin (`<sys_admin_group_id>`)
- Not PBS ticket
- Not Maintenance ticket
- **Result**: Not a special ticket type

**Step 1: Extract Tower Site**
- Subject parsing using `ticket-context` skill rules
- `[PAPA] Quebec - slow tower.`
- **Extracted tower site**: `Quebec`
- **Fiber network**: `PAPA`

**Step 2: Duplicate Detection**
- Using `duplicate-detection` skill with thresholds:
  - `DUPLICATE_HIGH_THRESHOLD`: 85 (default)
  - `DUPLICATE_MED_THRESHOLD`: 50 (default)
- **Confidence**: 65% (Medium confidence)
- **Action**: Post informational comment only, mark as `needs_human`

**Step 3: Categorization**
- Category: `slow_speeds` (slow speeds keywords detected)

**Step 3.5: Path Analysis**
- Since categorized as `slow_speeds`, would run `path-analyzer`
- **Result**: `needs_human` (path analysis completed)

**Final Status**: `needs_human`

### Ticket <ticket-id-2>: `(papa) romeo pppoe`

**Step 0: Special Ticket Type Check**
- Group: NOC (`<noc_group_id>`) - Not a special ticket type
- **Result**: Not a special ticket type

**Step 1: Extract Tower Site**
- Subject parsing using `ticket-context` skill rules
- `(papa) romeo pppoe`
- **Extracted tower site**: `romeo`
- **Fiber network**: `papa`

**Step 2: Duplicate Detection**
- Using `duplicate-detection` skill
- **Confidence**: 65% (Medium confidence)
- **Action**: Post informational comment only, mark as `needs_human`

**Step 3: Categorization**
- Category: `other` (no specific keywords detected)

**Step 3.5: Path Analysis**
- Not categorized as slow speeds/slow tower
- **Result**: Skip path analysis (`categorized`)

**Final Status**: `categorized`

## Workflow Summary

| Ticket ID | Subject | Tower Site | Status | Reason |
|-----------|---------|------------|--------|--------|
| <ticket-id-1> | [PAPA] Quebec - slow tower. | Quebec | needs_human | Medium confidence duplicate, slow speeds, path analysis completed |
| <ticket-id-2> | (papa) romeo pppoe | romeo | categorized | Medium confidence duplicate, not slow speeds |

## Skills Loaded and Available

The following skills are loaded and available for processing:

1. **`ticket-context`** - Tower site identification and parsing
2. **`duplicate-detection`** - Detect and handle duplicate tickets
3. **`path-analyzer`** - Live router path triage (slow speed tickets)
4. **`wireless-health`** - Investigate wireless link health
5. **`correlated-issues`** - Find correlated issues across tickets
6. **`correlated-auto-return`** - Auto-return downstream DEGRADED tickets
7. **`ihn-auto-return`** - Auto-return HEALTHY slow-tower tickets to CS
8. **`peak-scheduler`** - Queue night/peak tickets for peak-window testing
9. **`google-chat`** - Send notifications to Google Chat
10. **`sonar-api`** - Sonar ticketing system API reference

## Key Workflow Principles

1. **Single Unified Comment**: All comments must be posted via `scripts/sonar_comment.py --verify` using `&quot;` for HTML attribute quotes
2. **Single Bounce Max**: Anti-loop protection for IHN and correlated auto-return
3. **Weather Exclusion**: Absolute weather exclusion for bounce (except during peak retest)
4. **Peak Windows**: Night/peak tickets require peak-window retest before bounce
5. **Notification Rules**: Silent for handled_duplicate, auto_bounced_ihn, correlated_bounced, queued_for_peak, auto_closed* tickets
6. **Gated Bounces**: IHN + correlated group moves execute ONLY via `scripts/sonar_bounce.py` (enforces circuit breaker + `BOUNCE_AUTONOMOUS_ENABLED` kill switch + anti-loop markers; exit 3/4/5 → fall back to `needs_human`). Never hand-write the `updateTicket` group change.
7. **Structured Output**: Every result entry carries `classification` {category, complexity, priority}, `confidence` (0-100), `requires_human_review`, `escalation_reason`, `evidence` — these feed the decision log, metrics, and evals (see `server/batch_prompt_template.md` Output Contract)
8. **Deterministic Governance**: The server logs every settled ticket to `logs/decisions/<YYYY-MM>.jsonl` and feeds a circuit breaker that blocks auto-bounces when the auto-action failure rate exceeds `BREAKER_MAX_ERROR_RATE` (15%) over the rolling week. Operators: `docs/incident-runbook.md`; policy: `docs/permissions-policy.md`; details: [docs/governance.md](governance.md).

## Technical Constraints

- **API Limits**: Sonar API calls require proper authentication with `SONAR_TOKEN`
- **File Operations**: All comments must use `scripts/sonar_comment.py --verify` with proper HTML escaping
- **Skill Dependencies**: Some skills must be loaded before others (e.g., `path-analyzer` + `ticket-context`)
- **Environment Variables**: Many thresholds and settings configurable via `.env`

## Next Steps

The workflow would continue with:

1. Loading `path-analyzer` and `ticket-context` skills for slow speed tickets
2. Running actual path analysis with live router commands
3. Handling IHN auto-return, correlation, and peak scheduling
4. Sending Google Chat notifications based on final statuses
5. Returning consolidated JSON results for batch processing

This demonstrates the comprehensive ticket processing workflow that NocAgent would use to handle NOC tickets automatically, with human oversight for tickets that require attention.
