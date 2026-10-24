# T2 reference - Sonar, notifications, optional pipeline server

## Sonar connection

| Key | Secret? | Notes |
|---|---|---|
| `SONAR_BASE_URL` | no | for example `https://example.sonar.software` (also `org.sonar_base_url` in the profile) |
| `SONAR_TOKEN` | yes | Sonar admin, Security, API keys. Analysis and discovery need read; posting comments and the gated bounce need write. Use a dedicated service user. |

The operator enters both (`env_set.py` prompts hidden for secrets). Proof:
`python3 scripts/doctor.py --only sonar_api` gives `[PASS] sonar_api` (a read-only
account search that matches nothing). 401 or 403 means a bad or under-privileged
token; a timeout means the URL or the network.

## Checklist: create these in Sonar first

IDs are per Sonar instance. The numbers in `site-pack/profile.example.json` belong to
the original install; yours differ, so create the objects, then discover them.

| Profile key | Object to have in Sonar | Used for |
|---|---|---|
| `sonar.groups.noc` | the NOC ticket group | incoming tickets |
| `sonar.groups.cs_level_2` | the customer-support escalation group | target of the bounce gate |
| `sonar.groups.sys_admin` | the sysadmin group | forward-only passthrough tickets |
| `sonar.categories.slow_speeds` | "NOC - Slow Speeds" | marks slow-tower tickets; removed on bounce |
| `sonar.categories.night_peak` | "NOC - Night Checks" | night or peak tickets; removed on bounce |
| `sonar.categories.cx_th_side_issue` | "CX/TH-side Issue" | added on a healthy-path return |
| `sonar.categories.weather` | "NOC - Rain Fade Issues" | weather exclusion |
| `sonar.categories.network_fiber.names` | one "Network - NAME" category per fiber | shared-fiber correlation signal |

`org.name`, `org.web_domain` and `org.portability_tokens` in the same file are names
and words the repo's portability scan must keep out of tracked files.

## Discovery and the profile

```bash
python3 scripts/doctor.py --discover-ids          # read-only: lists groups and categories, matches by name
python3 scripts/doctor.py --write-profile         # writes site-pack/profile.json after confirmation
python3 scripts/doctor.py --only site_profile     # proof: [PASS] site_profile
```
Show the operator the proposed mapping (name to id) and confirm each before writing.
Unmatched objects: ask whether to create them in Sonar or map a different name. If
`site-pack/profile.json` exists, compare first and ask. Env variables override the
profile (`CS_LEVEL2_GROUP_ID`, `IHN_BOUNCE_CATEGORY_ID`, `SLOW_SPEEDS_CATEGORY_ID`,
`NIGHT_PEAK_CATEGORY_ID`, `NOC_GROUP_ID`, `SYSADMIN_GROUP_ID`); the profile is the
preferred place. `docs/config.md` lists every setting.

Time zone: set `PEAK_WINDOW_TZ` (night-check retest window, default America/Chicago,
20:00 to 21:00) and `DISPATCH_TIMEZONE` to the operator's real zone.

## Notifications

`NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL` is a secret (the URL carries a key): operator runs
`python3 scripts/env_set.py NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL`. Create it in the chat
space under Apps and integrations, Webhooks. Then, with the operator's yes,
`python3 scripts/doctor.py --notify` posts one clearly labeled test card. Proof:
`[PASS] google_chat` and the operator sees the card. A plain `doctor.py` run also
posts one test message unless `--skip google_chat` is passed; tell them before running it.

## Optional pipeline server

Only if tickets will be pushed in by N8N (or curl). Without it the interactive agent
still analyzes any ticket you point it at.

| Setting | Meaning |
|---|---|
| `AGENT_WORKSPACE` | absolute path of this checkout (the agent's working folder) |
| `AGENT_HARNESS` | `opencode` (default), `claude`, `codex`, `gemini` or `command` |
| `OPENCODE_PATH` / `CLAUDE_PATH` | binary of the chosen harness |
| `AGENT_COMMAND_TEMPLATE` | with `command`: shell template using `{prompt}`, `{workspace}`, `{model}` for any other CLI agent |
| `WEBHOOK_PORT` | default 65333 |
| `TICKET_WEBHOOK_AUTH_TOKEN` | Basic-auth token; generate it, never type it: `python3 scripts/env_set.py --generate TICKET_WEBHOOK_AUTH_TOKEN` |
| `WEBHOOK_BIND` | default `127.0.0.1` (loopback only) |
| `WEBHOOK_TLS_PROXY` | set `1` only when `WEBHOOK_BIND` is non-loopback AND a TLS proxy or VPN fronts the port |
| `BATCH_TIMEOUT_MS` | per-batch timeout, default 45 minutes |

`WEBHOOK_BIND`: leave it alone when N8N runs on the same host. Use `0.0.0.0` (or a
specific interface) only when N8N is on another host, and then also set
`WEBHOOK_TLS_PROXY=1` and really have TLS or a VPN, because the token is a bearer
secret. `--check-config` warns otherwise.

Steps (ask before each that leaves the repo):
```bash
cd server && npm ci && cd ..
python3 scripts/env_set.py --generate TICKET_WEBHOOK_AUTH_TOKEN
node server/index.js --check-config                  # proof: no FAIL lines
sudo bash scripts/install.sh --dry-run               # prints the rendered systemd units, changes nothing
sudo bash scripts/install.sh                         # operator only, after reading the dry run
python3 scripts/doctor.py --only webhook_server,systemd
```
`install.sh` options: `--no-mcp`, `--no-start` (install without starting),
`--with-gui`, `--enable-backup`, `--node-bin PATH`. It refuses to start the service
until `--check-config` passes. Logs: `journalctl -u noc-agent` and `logs/webhook.log`.

N8N: import `docs/n8n/workflow-template.json` and follow `docs/n8n-template.md`. The
operator copies the webhook token from `.env` (with their editor) into the N8N
credential; request format and the 202 response are in `docs/webhook-api.md` (see "Send
a test ticket" there for the end-to-end check).

## Safety switches and thresholds

- `BOUNCE_AUTONOMOUS_ENABLED`: master kill switch for automatic returns to CS Level 2.
  Set it to `false` explicitly in `.env` during onboarding (do not rely on the default). The bounce gate then refuses (exit 4) and every
  affected ticket goes to a human. The operator turns it on only after watching the
  decisions in `logs/decisions/` for a while; see `docs/governance.md`.
- Circuit breaker: trips on a high failure rate and blocks bounces (exit 3).
  `python3 scripts/circuit_breaker.py status` shows it.
- Gate exit codes: 3 breaker open, 4 kill switch off, 5 already bounced once. The agent
  never retries a gated mutation by hand.
- Thresholds (defaults): `IHN_TOWER_THRESHOLD=80`, `CORRELATED_CONFIDENCE_THRESHOLD=75`,
  `PATH_TOWER_THRESHOLD=75`, `DUPLICATE_HIGH_THRESHOLD=85`, `DUPLICATE_MED_THRESHOLD=50`.
  Do not lower them during onboarding.
- Weather, night-check and anti-loop rules are in `docs/ticket-pipeline.md`.

## T2 verify and failures

`python3 scripts/doctor.py --tier 2`: no `[FAIL]`, `sonar_api` and `site_profile`
pass (plus `google_chat` and `webhook_server` if configured).

| Symptom | Fix |
|---|---|
| `[FAIL] sonar_api` 401 or 403 | new token via `env_set.py SONAR_TOKEN` |
| `--discover-ids` finds nothing | wrong base URL or a token without read access to groups |
| `[WARN] site_profile` | run discovery and `--write-profile` |
| `AGENT_WORKSPACE` FAIL in `--check-config` | set an absolute existing folder |
| `AGENT_HARNESS` FAIL | one of opencode, claude, codex, gemini, command |
| `WEBHOOK_BIND` WARN | loopback again, or the TLS ack as above |
| 401 on a test ticket | N8N and `.env` hold different tokens |
