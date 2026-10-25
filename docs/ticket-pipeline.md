# Ticket Pipeline — How Tickets Flow Through the Agent

> Canonical step-by-step logic: [AGENTS.md](../AGENTS.md) (the agent's instruction file).
> Operator index: [README](../README.md). Related: [docs/webhook-api.md](webhook-api.md),
> [docs/governance.md](governance.md), [docs/permissions-policy.md](permissions-policy.md).

## End-to-end flow (summary table)

The full step-by-step workflow is documented in [AGENTS.md](../AGENTS.md) (the agent's instruction file). Summary:

| Step | Action | Outcome |
|------|--------|---------|
| 0 | **Passthrough / auto-close** | Sys Admin out-of-office auto-replies auto-closed. Sys Admin tickets forwarded to Google Chat. PBS success tickets auto-closed. PBS failures get a summary comment + alert. Subject containing 'Maintenance' or 'Scheduled Maintenance' auto-closed silently (`auto_closed_maintenance`). |
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
