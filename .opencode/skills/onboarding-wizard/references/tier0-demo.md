# T0 reference - the offline demo

`python3 scripts/demo.py` needs python 3.9+, node, curl and `cd server && npm ci`
(nothing else, no accounts). It starts `scripts/mock_sonar.py` and the real
`server/index.js` on 127.0.0.1 in a temp folder, with `AGENT_HARNESS=command` driving
`scripts/demo_agent_stub.py` (a scripted stand-in for the LLM), posts 8 fictional
tickets as N8N would, waits for the batches, and compares decisions, ticket state,
mutation order, comments, chat cards and the decision log with
`scripts/demo/fixtures/expected.json`. The temp folder is deleted (`--keep` keeps it).

Read the expectations in small ranges, for example
`python3 -c "import json;d=json.load(open('scripts/demo/fixtures/expected.json'));print(json.dumps(d['tickets']['91001'],indent=1))"`
(or `jq '.tickets["91001"]'` where jq exists). Tickets live in
`scripts/demo/fixtures/tickets.json`.

## The 8 tickets

| Ticket | Subject (fictional) | Outcome | What it shows |
|---|---|---|---|
| 91001 | Alpha tower, slow | `auto_bounced_ihn` | Path analysis found every hop healthy and tower confidence cleared the threshold. The bounce gate (`scripts/sonar_bounce.py`) moved it to CS Level 2, swapped the Slow Speeds category for CX/TH-side Issue (fixture ids 201 to 203), then the unified comment ("Direct Path Analysis - Alpha", "Automated NOC Review") was posted. Silent: no chat message. Gate first, comment second. |
| 91002 | Bravo tower, slow | `categorized` | Path is DEGRADED at `bravo-agg`. A real problem, so nothing is bounced: comment with the findings, category kept, one chat card for a human. |
| 91003 | Backup-server garbage-collect "successful" | `auto_closed` | Known-noise passthrough rule: closed, no comment, no chat. |
| 91004 | Scheduled maintenance notice | `auto_closed_maintenance` | Same idea: maintenance notices close silently. |
| 91005 | "Automatic reply: Out of Office" in the Sys Admin group | `auto_closed_ooo` | Subject and body both say out-of-office, so it is closed. A real Sys Admin ticket would be forwarded to chat instead. |
| 91006 | Delta tower, outage | `categorized` | Outage category added, one chat card. This is the original of the next ticket. |
| 91007 | Delta tower, outage (another customer) | `handled_duplicate` | Same tower and same issue as 91006: a "Duplicate Ticket" comment pointing at 91006 and a move to CS Level 2. High-confidence duplicates are silent. |
| 91008 | Echo tower, slow "since the storm" | `needs_human` (`weather_excluded`) | Path is healthy, but the text mentions a storm. Weather blocks any auto-return, so a person decides. One chat card. |

Every returned entry also carries the decision fields (classification, confidence,
`requires_human_review`, `escalation_reason`, evidence) that feed the decision log in
`logs/decisions/`.

## Scenarios

`python3 scripts/demo.py --scenario NAME` reruns the same tickets with one safety
mechanism changed, so the 91001 bounce is refused instead:

| NAME | Change | Result for 91001 |
|---|---|---|
| `default` | live gate against the mock | bounced |
| `dry-run` | gate runs with `--dry-run` | stays in NOC, nothing written |
| `kill-switch` | `BOUNCE_AUTONOMOUS_ENABLED=false` | gate exits 4, needs a human |
| `breaker-open` | circuit breaker opened via `POST /governance/breaker` | gate exits 3, needs a human |

Other flags: `--check` (quiet, one result line), `--json`, `--keep`, `--timeout N`.

## Exit codes and failures

| Exit | Meaning | Fix |
|---|---|---|
| 0 | all checks passed | done |
| 1 | a check differs from `expected.json` | rerun with `--keep`, read `server.out` in the printed temp folder, report the first differing line |
| 2 | missing prerequisite | `cd server && npm ci`; install curl, node 18+ or python 3.9+ |
| 3 | the pipeline did not run | `--keep`; usually a port clash or an old `node`; try `--timeout 180` |

The demo never reads `.env`, calls a real service or binds a non-loopback address.
