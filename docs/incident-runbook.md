# NocAgent Incident Runbook

Operator procedures for governance incidents. Companion docs:
[permissions-policy.md](permissions-policy.md) (what the agent may do), [governance.md](governance.md)
(how governance works), [AGENTS.md](../AGENTS.md) (what the agent does).

**Golden rule:** every incident that shows the agent was wrong becomes a permanent
test case (§8) before you close the incident.

---

## 1. Circuit Breaker Tripped (auto)

**Signal:** Google Chat alert "❌ Circuit Breaker OPENED - Auto-Bounces Suspended"
or `GET /governance` shows `"breaker_open": true`.

**Meaning:** auto-action failure rate (`analysis_failed` + `bounce_reverted`) exceeded
`BREAKER_MAX_ERROR_RATE` (15%) over ≥10 runs in the last 7 days. IHN + correlated
bounces are blocked; the agent still triages, categorizes, comments, and notifies.

**Diagnose:**
```bash
python3 scripts/circuit_breaker.py status          # rate, failures, by_outcome
python3 scripts/decision_log.py query --kind bounce --since <7d ago>
python3 scripts/metrics_rollup.py --no-chat        # includes revert re-check
```
Look for what dominates: `analysis_failed` → usually a network-wide issue (auth
failures on btest, unreachable towers); `bounce_reverted` → wrong bounces (§3).

**Recover (after you understand why):**
```bash
python3 scripts/circuit_breaker.py clear
# or via API:
printf 'header = "Authorization: Bearer %s"\n' "$TICKET_WEBHOOK_AUTH_TOKEN" | curl -sS -K /dev/stdin -X POST http://127.0.0.1:65333/governance/breaker \
  -H 'Content-Type: application/json' \
  -d '{"action":"clear"}'
```
Do not clear blind — the breaker exists so that you look first. If the cause was a
transient outage, clear; if it was bad bounces, work §3 first.

---

## 2. Kill Switch / Bounce Freeze (manual)

**When:** you suspect the agent is bouncing wrongly and want an immediate stop while
investigating (e.g., CS complains about several bounced tickets).

```bash
# Immediate block of all autonomous bounces (agent keeps triaging + notifying)
python3 scripts/circuit_breaker.py open "CS reported wrong bounces - investigating"

# Harder stop (requires service restart to re-enable):
echo 'BOUNCE_AUTONOMOUS_ENABLED=false' >> .env && systemctl restart noc-agent
```

**Restore:**
```bash
python3 scripts/circuit_breaker.py clear
# and if the kill switch was used: set BOUNCE_AUTONOMOUS_ENABLED to true (or 1/yes/on) in .env, restart noc-agent
```

---

## 3. Wrong Bounce Reported (CS sends a ticket back)

**Signal:** a bounced ticket reappears in NOC, or CS says "this wasn't IHN."

```bash
python3 scripts/decision_log.py query --ticket <id>      # what the agent did + evidence
python3 scripts/metrics_rollup.py --no-chat              # confirms revert + feeds breaker
```

The weekly metrics job (or any manual run) detects the group move back to NOC and
records `bounce_reverted` into the breaker — several of these within a week is what
trips it. That is by design.

**Then:**
1. Fix the root cause (usually: tower-confidence scoring mis-credited a site → tune
   the `ihn-auto-return` scoring or raise `IHN_TOWER_THRESHOLD` in `.env`).
2. Add the ticket as a testcase (§8).
3. Note the threshold change in the next weekly report window (permissions-policy.md
   "Change Review").

---

## 4. Agent Failed to Parse / Silent Ticket

**Signal:** Chat card "🚨 Agent Processing Failed" or a ticket with no status in
`logs/webhook.log`.

```bash
tail -100 logs/webhook.log | grep -E 'parse|Failed' 
journalctl -u noc-agent --since "-1 hour" --no-pager | tail -50
```
Fallback notifications already cover the miss — the tickets listed in the card are not
lost, they just weren't processed. Re-send them via the webhook (see the 'Test Agent Directly' section in
docs/n8n-integration.md) or handle manually.

---

## 5. Peak Queue Stuck / Not Draining

**Signal:** tickets queued for peak (`queued_for_peak`) still pending after 21:00 CT.

```bash
cat logs/pending_peak_checks.json
journalctl -u noc-agent --since "-1 day" --no-pager | grep -i peak
```
The server schedules the next drain automatically; if the box was down at 20:05 CT,
the queue persists and drains at the next scheduled peak window: at 20:05 CT if the service starts before the window and today's drain has not run, about one second after start if it starts inside the 20:00-21:00 CT window after 20:05, or tomorrow if today's drain has already run or the window has passed. To intervene manually, process a ticket as
NOC normally (the anti-loop markers prevent double-bounces).

---

## 6. Decision Log / Governance Files Corrupted

**Signal:** `decision_log.py query` returns nothing, `circuit_breaker.py status` errors.

```bash
ls -la logs/decisions/            # monthly JSONL shards; torn lines are tolerated
cat logs/circuit_breaker.json     # rm it to reset the breaker (re-trips if failures recur)
```
Deleting `circuit_breaker.json` resets breaker state but not the decision log. JSONL
is append-only — a torn line from a crash is skipped on read, not fatal.

---

## 7. Weekly Report Looks Wrong (rates suddenly 0% or n/a)

**Meaning:** usually an empty decision log for the window (service down? webhook not
receiving?), not a real metric.

```bash
python3 scripts/decision_log.py query --since <7d ago> --format count
systemctl status noc-agent noc-metrics.timer noc-evals.timer
journalctl -u noc-metrics.service --since "-7 days" --no-pager | tail -20
```

---

## 8. Incident → Permanent Testcase (ALWAYS DO BEFORE CLOSING)

```bash
# Option A: full golden-set entry (has expected classification)
export $(grep SONAR_TOKEN .env | xargs)
python3 evals/build_golden_set.py --add-incident <ticket_id>

# Option B: pending stub (fill expected fields later)
python3 scripts/metrics_rollup.py --no-chat --incident <ticket_id>
```

Then run the eval to confirm the regression gate catches the old behavior:
```bash
python3 evals/eval_run.py
```

A bug the agent made once must be a test it can never silently fail again.

---

## Cheat Sheet

| Symptom | First command | Then |
|---|---|---|
| 🔴 Breaker alert | `python3 scripts/circuit_breaker.py status` | diagnose, `clear` |
| Wrong bounce | `python3 scripts/decision_log.py query --ticket <id>` | §3 + `--add-incident` |
| Freeze bounces now | `python3 scripts/circuit_breaker.py open "<reason>"` | §2 restore steps |
| "Agent Processing Failed" card | `journalctl -u noc-agent --since "-1h"` | re-send ticket |
| Peak ticket not drained | `cat logs/pending_peak_checks.json` | waits for next 20:05 CT |
| Report is n/a | `decision_log.py query --since <7d> --format count` | check service up |
| Eval regressed | `cat logs/evals/eval_*.json \| tail -50` | fix, then re-run |
