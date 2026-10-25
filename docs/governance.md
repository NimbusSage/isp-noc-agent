# Governance, Metrics & Evals

> Operator manual index lives in the repo root [README](../README.md).

## Governance, Metrics & Evals

The agent's autonomy is bounded by deterministic code, not prompt discipline. Ticket handling uses gated scripts. Temporary site recovery has an explicit operator authorization scope for necessary temporary radio-role, addressing, and OSPF operations—including a temporary customer interruption on the selected donor infrastructure AP; the typed device executors are implemented and registered (`temp-*` tools, `mcp-server/src/tool-schemas.ts`), gated by kill-switch env flags, transport flags, host allowlist, exact-device bindings, and `execute:true` (see `temp-site-connection` and [permissions-policy.md](../docs/permissions-policy.md)). Curated address pairs can avoid Sheet access, not live conflict checks.

### Decision Log (audit trail)

Every settled ticket gets one JSONL record in `logs/decisions/<YYYY-MM>.jsonl`: `ts`, `kind`,
`ticket_id`, `status`, `confidence`, `classification`, `requires_human_review`,
`escalation_reason`, `evidence`. Bounces are logged by `scripts/sonar_bounce.py` (kind `bounce`);
the server appends `batch_result` records after each batch/peak-retest. Query with:

```bash
python3 scripts/decision_log.py query --since <date> --format count
python3 scripts/decision_log.py query --ticket <ticket-id>
```

### Circuit Breaker (deterministic auto-action guard)

The server feeds every outcome to `scripts/circuit_breaker.py`. Failure classes:
`analysis_failed` (path analysis failed) and `bounce_reverted` (a bounced ticket was moved
back to NOC — worst signal). `needs_human` is NOT a failure (correct escalation is desired).

- Trips when failure rate > `BREAKER_MAX_ERROR_RATE` (0.15) over ≥ `BREAKER_MIN_RUNS` (10)
  runs within `BREAKER_WINDOW_SEC` (7 days)
- While open: `sonar_bounce.py` refuses all bounces (exit 3) → agent falls back to
  `needs_human`; triage/categorize/notification continue
- Trip fires a Google Chat alert; clearing is audit-logged and prints 'Breaker CLEARED' but does not send a Chat message

```bash
python3 scripts/circuit_breaker.py status          # current window stats
printf 'header = "Authorization: Bearer %s"\n' "$TICKET_WEBHOOK_AUTH_TOKEN" | curl -sS -K /dev/stdin http://127.0.0.1:65333/governance
printf 'header = "Authorization: Bearer %s"\n' "$TICKET_WEBHOOK_AUTH_TOKEN" | curl -sS -K /dev/stdin -X POST http://127.0.0.1:65333/governance/breaker \
  -H 'Content-Type: application/json' \
  -d '{"action":"open","reason":"investigating <ticket-id> revert"}'   # or action:"clear"
```

### Gated Bounce Execution

IHN + correlated bounces (the two autonomy tiers that move tickets between groups) only
execute through `scripts/sonar_bounce.py`, which enforces in order: breaker assert (exit 3),
`BOUNCE_AUTONOMOUS_ENABLED` kill switch (exit 4; enabled only when set to `true`, `1`, `yes`, or `on`; unset or empty blocks), anti-loop marker check over the last 5
comments (exit 5), then the `updateTicket` write, then a decision-log record. The agent is
instructed (AGENTS.md + batch template) to never hand-write the mutation.

### Weekly Metrics + Eval Loop (the improvement cadence)

| Job | When | What |
|-----|------|------|
| `noc-metrics.timer` | Mon 08:00 box local time (template assumes America/Chicago) | `scripts/metrics_rollup.py`: straight-through rate, escalation rate, bounce rate, analysis failures, breaker status, **Sonar revert re-check** (a bounced ticket back in NOC records `bounce_reverted` into the breaker) → Chat report + `logs/metrics/weekly_<date>.json` |
| `noc-evals.timer` | Mon 08:30 box local time (template assumes America/Chicago) | `evals/eval_run.py` (deterministic): re-classifies the golden set, exits 2 when accuracy regression vs `baseline.json` exceeds `EVAL_MAX_REGRESSION_PCT` (default 2.0); a regressed run never replaces the baseline. Full agent replay is manual: `DRY_RUN=1 python3 evals/eval_run.py --replay` |

**Build the golden set** (once, then maintain):

```bash
python3 evals/build_golden_set.py --limit 100   # outcome-derived labels from Sonar history
# review a sample, hand-fix mislabels in evals/golden/golden_set.json
```

**Every production incident becomes a permanent test case:**

```bash
python3 evals/build_golden_set.py --add-incident 12345       # into golden set
python3 scripts/metrics_rollup.py --incident 12345           # stub in evals/golden/pending/
```

**Metrics tracked** (per the production-agent criteria): human correction rate (reverts),
straight-through processing rate, escalation rate, bounce rate, analysis failure rate,
breaker window rate. Files: `logs/metrics/weekly_*.json`, `logs/evals/eval_*.json`,
`logs/decisions/*.jsonl`.

---

