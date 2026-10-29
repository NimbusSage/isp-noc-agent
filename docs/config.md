# Configuration Reference

> Operator manual index lives in the repo root [README](../README.md).

`.env.example` lists every variable the code reads, with its default and the tier that needs it; `python3 scripts/docs_check.py` fails if the code reads a variable that `.env.example` does not list.

## Configuration

### Environment Variables (`.env`)

All secrets live in `.env` at the repo root. This file is gitignored and never committed.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SONAR_TOKEN` | Yes | — | Sonar API bearer token |
| `NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL` | Yes | — | Google Chat incoming webhook URL for NOC ticket alerts |
| `TICKET_WEBHOOK_AUTH_TOKEN` | Yes | — | Bearer or Basic auth token for the webhook server; the Basic username is ignored, only the password field is compared |
| `WEBHOOK_PORT` | No | `65333` | Port for the webhook server |
| `WEBHOOK_BIND` | No | `127.0.0.1` | Bind address for the webhook server; a cleartext token warning is logged when the address is not loopback and `WEBHOOK_TLS_PROXY=1` is not set |
| `OPENCODE_PATH` | No | `~/.opencode/bin/opencode` | Path to the OpenCode CLI binary |
| `AGENT_WORKSPACE` (or legacy `OPENCODE_WORKSPACE`) | No | install root | Workspace path passed to the agent harness via spawn `cwd` |
| `AGENT_HARNESS` | No | `opencode` | Which CLI launches the agent: `opencode` (default), `claude`, `codex`, `gemini`, or `command`. `codex` and `gemini` are experimental. |
| `CLAUDE_PATH` | No | `claude` | Claude Code CLI binary (only when `AGENT_HARNESS=claude`) |
| `AGENT_COMMAND_TEMPLATE` | No | — | Split into arguments and run without a shell. Must contain `{prompt}` or `{prompt_file}`; `{workspace}` and `{model}` are also available. `{prompt_file}` is a private temporary file removed after the run. Shell syntax such as pipes or `&&` is refused unless `AGENT_COMMAND_SHELL=1`, which passes the prompt in the `NOC_PROMPT` environment variable instead of pasting it into the command. |
| `BATCH_TIMEOUT_MS` | No | `2700000` (45 min) | Per-batch agent process timeout. Batches run one at a time, so a long run delays the queue rather than failing. |
| `WINDOW_DURATION_MS` | No | `60000` | Ticket collection window; mainly useful for testing (shorten to speed up batch turnaround). |
| `NOC_AGENT_LOG_DIR` | No | `<repo>/logs` | Fallback log dir when `NOC_LOG_DIR` is unset; webhook.log is written here; pending_tickets.json goes to `NOC_STATE_DIR` (defaults to log dir) |
| `IHN_TOWER_THRESHOLD` | No | `80` | Tower-correctness confidence threshold for IHN auto-return bounce (`ihn-auto-return`, +40 exact literal) |
| `CORRELATED_CONFIDENCE_THRESHOLD` | No | `75` | Correlated downstream bounce threshold (`correlated-auto-return`, IP+name exact) |
| `DUPLICATE_HIGH_THRESHOLD` | No | `85` | High-confidence duplicate auto-handle threshold (`duplicate-detection`) |
| `DUPLICATE_MED_THRESHOLD` | No | `50` | Medium-confidence duplicate informational threshold |
| `PEAK_WINDOW_TZ` | No | `America/Chicago` | TZ for peak retest window (`peak-scheduler`) |
| `PEAK_WINDOW_START` | No | `20:00` | Start of peak window in `PEAK_WINDOW_TZ` |
| `PEAK_WINDOW_END` | No | `21:00` | End of peak window in `PEAK_WINDOW_TZ` |
| `PEAK_RETEST_SPACING_MS` | No | `600000` (10 min) | Spacing between peak-queue items (serial analyzer runs) |
| `PEAK_QUEUE_FILE` | No | `<repo>/logs/pending_peak_checks.json` | Override for peak queue location |
| `PEAK_SCHEDULER_ENABLED` | No | `true` | Disable peak scheduler with `false` |
| `NOC_MCP_PASSPHRASE` | Only if using MCP | — | Passphrase for AES-256-GCM credential encryption |
| `NOC_MCP_CONFIG` | No | `mcp-server/config/server.json` | MCP server config path |
| `UISP_BASE_URL` | Only if using UISP | — | UISP controller base URL; usually registered with `python3 scripts/resources.py set uisp <url>` instead |
| `MYMATE_URL` | Only if using MyMate | — | MyMate network monitor base URL; usually registered with `python3 scripts/resources.py set mymate <url>` instead |
| `MYMATE_USER` | Only if using MyMate | — | Read-only MyMate operator account (email) |
| `MYMATE_PASS` | Only if using MyMate | — | Password for the MyMate operator account |
| `LIBRENMS_URL` | Only if using LibreNMS | — | LibreNMS base URL; usually registered with `python3 scripts/resources.py set librenms <url>` instead (sysName/hardware/geolocation corroboration, not site truth) |
| `LIBRENMS_TOKEN` | Only if using LibreNMS | — | LibreNMS `X-Auth-Token` for `<LIBRENMS_URL>/api/v0` (https://docs.librenms.org/API/) |
| `LIBRENMS_WEB_USER` | Only for authentic LibreNMS renders (`--librenms-render`) | — | Dedicated non-admin LibreNMS account. Used as the `X-Forwarded-Email` SSO header identity (`username`; header name set by `CHART_EMAIL_HEADER`); NO password check happens from a trusted source, so keep the LibreNMS `sso.trusted_proxies` list tight (the SSO proxy IP + the NocAgent NAT egress IP) and never share this value |
| `TEMP_OSPF_SPREADSHEET_ID` | Only for Temp OSPF helper | — | Google spreadsheet ID from its URL; share file with service-account email (service-account mode) |
| `TEMP_OSPF_AUTH_MODE` | No | `service-account` | `service-account` or `user-oauth`; use `user-oauth` when the sheet lives outside the company domain and can't share to the robot email |
| `TEMP_OSPF_CREDENTIALS_FILE` | Only for Temp OSPF helper (service-account mode) | — | Absolute path outside repo to service-account JSON key file; owner-only mode required |
| `TEMP_OSPF_OAUTH_FILE` | Only for Temp OSPF helper (user-oauth mode) | — | Absolute path outside repo to Desktop OAuth client JSON (`client_id`, `client_secret`, `refresh_token`); owner-only mode required |
| `TEMP_OSPF_TAB` | No | `Temp OSPF` | Exact sheet tab title |
| `TEMP_OSPF_ACCESS` | No | `readonly` | `readonly` for inspect; `write` is one of three required reservation gates |
| `TEMP_OSPF_READ_ROWS` | No | `40` | Maximum preview rows (1–10000), columns capped at AZ |
| `TEMP_OSPF_WRITE_ENABLED` | Only for approved row update | unset | Must equal `true` in addition to access=write and CLI `--confirm-write true` |
| `TEMP_OSPF_HEADER_ROW` | Only for approved row update | `0` | One-based header row; live sheet uses `1` (paired writer refuses anything else) |
| `TEMP_OSPF_IN_USE_VALUE` | No | Boolean `true` | Checkbox value; the helper accepts only `true` (or `TRUE`) to check it |
| `TEMP_OSPF_AUDIT_FILE` | No | `logs/temp-ospf-sheet.jsonl` | Local JSONL record of reservation attempts/outcomes |
| `TRACKER_SPREADSHEET_ID` | Yes (Tracker Sheet helper) | *(no default — org-specific)* | Tracker spreadsheet ID from its URL; build a matching sheet per docs/setup/tracker-sheet-template.md |
| `TRACKER_TAB` / `TRACKER_PICK_TAB` | No | `NOC Ticket Tracker` / `Pick List` | Exact sheet tab titles |
| `CALENDAR_OAUTH_FILE` | Only for schedule-field-tech | falls back to `TRACKER_OAUTH_FILE` | Owner-only user-OAuth secret outside the repo (`client_id`, `client_secret`, `refresh_token`); grant must cover BOTH calendar and Sheets scopes (use `oauth-url --scope all` when re-consenting the shared file) |
| `CALENDAR_AUTH_MODE` | No | `user-oauth` | Only `user-oauth` supported (Tower Dispatch consented account) |
| `CALENDAR_WRITE_ENABLED` | Only for created appointments | unset | Kill switch: must equal `true` in addition to CLI `--confirm-create true` |
| `CALENDAR_FALLBACK_CALENDAR_ID` | No | `primary` | Blanket Tower Dispatch calendar that fallback events are created UNDER (organizer = `towerdispatch@…`, guests get TD invites); the consenting account needs writer access to it |
| `CALENDAR_TECH_MAP_FILE` | No | `site-pack/tech_calendars.json` | Optional tech→calendar_id/attendee override map |
| `DISPATCH_TIMEZONE` / `DISPATCH_DEFAULT_START_HOUR` / `DISPATCH_DEFAULT_DURATION_MINUTES` | No | `America/Chicago` / `8` / `60` | Default appointment window when the operator gives no time (next business weekday) |
| `DISPATCH_NOTIFY` | No | `all` | Google Calendar `sendUpdates` for guests; `all` per operator rule — invites always go out unless a run is explicitly a quiet test |
| `DISPATCH_AUDIT_FILE` | No | `logs/schedule_dispatch_audit.jsonl` | JSONL audit of resolve/create/mark-dispatched actions |
| `TRACKER_AUTH_MODE` | No | `user-oauth` | Must be `user-oauth` (external-domain sheet; reuses `google_user_auth.mjs`) |
| `TRACKER_OAUTH_FILE` | Only for Tracker Sheet helper | — | Absolute path outside repo to Desktop OAuth client JSON (`client_id`, `client_secret`, `refresh_token`); owner-only mode required |
| `TRACKER_ACCESS` | No | `readonly` | `readonly` for inspect; `write` is one of three required append gates |
| `TRACKER_WRITE_ENABLED` | Only for approved row append | unset | Must equal `true` in addition to access=write and CLI `--confirm-write true` |
| `TRACKER_HEADER_ROW` | Only for approved row append | `1` | One-based header row; helper refuses on signature drift |
| `TRACKER_AUDIT_FILE` | No | `logs/tracker_sheet_audit.jsonl` | Local JSONL record of append attempts/outcomes |
| `TEMP_SITE_WPA_KEYS` | Only for UBNT temp to secured APs | unset | Fallback WPA key list when AP-config lookup is unavailable. JSON array (`["key1","key2"]`, tried in order) or a single bare key. Values never logged — audit records `env:<index>` only. Primary is always the AP's own `aaa.1.wpa.psk` via its exact-device binding. |
| `TEMP_SITE_WRITE_ENABLED` | Only for agent-driven temp writes | unset | Must equal `true` — runtime kill switch for all temp-site executors (forward + undo). Forwarded to the batch agent's MCP server via `server/index.js` spawn env. |
| `TEMP_SITE_AIROS_TRANSPORT` / `TEMP_SITE_ROUTEROS_TRANSPORT` / `TEMP_SITE_MT_TRANSPORT` | Only for agent-driven temp writes | unset | Each must equal `verified` — per-transport safe-area proof flags. |
| `TEMP_SITE_ALLOWED_HOSTS` | Only for agent-driven temp writes | unset | Scope allowlist: comma-separated exact IPv4 and/or CIDR ranges (e.g. `10.0.0.0/8`). Says *which networks*; exact-device bindings say *which devices*. Forwarded to the batch agent's MCP server via `server/index.js` spawn env. |
| `EDGE_ROUTER_IPS` | No | — | Comma-separated edge 2216 router addresses; path-analyzer uses these as a hint to find the fiber router in a traceroute (real values in `site-pack/topology/` or this setting) |
| `RDS2216_SUBNET` | No | — | LibreQoS RDS2216 subnet; the fiber walk uses it to recognise and skip the optional RDS2216 hop (real range in `site-pack/topology/fiber-topology.md`) |
| `PATH_TOWER_THRESHOLD` | No | `75` | Tower confidence floor for path-analyzer |
| `BTEST_DURATION` | No | `10` | btest duration seconds (baseline 10, direct /30 5) |
| `BTEST_PROTOCOL` | No | `tcp` | btest protocol |
| `BTEST_TIMEOUT_MS` | No | `60000` | Per-btest SSH timeout (btest blocks) |
| `BTEST_PASSWORD` | Only for a dedicated btest account | — | Operator-supplied at runtime; password of the dedicated btest account some fiber routers use (`path-analyzer`). Never stored in the repo |
| `PATH_TRIAGE_MODE` | No | `direct` | Triage mode `direct` (live) vs `analyzer` rollback |
| `AGENT_MODEL` | No | `opencode/big-pickle` | Model for agent runs; `OPENCODE_MODEL` is the legacy alias, read when `AGENT_MODEL` is unset |
| `MAX_BATCH_SIZE` | No | `3` | Max tickets per agent batch; larger bursts split into sequential jobs. Server refuses prompts estimated above 180,000 tokens (characters divided by 4); default model context window is 250,000 tokens |
| `BOUNCE_AUTONOMOUS_ENABLED` | No | `false` | Master kill switch: `false` blocks ALL autonomous bounces (`sonar_bounce.py` exit 4) |
| `BREAKER_WINDOW_SEC` | No | `604800` | Circuit breaker rolling window (7 days) |
| `BREAKER_MIN_RUNS` | No | `10` | Min runs in window before breaker rate is evaluated |
| `BREAKER_MAX_ERROR_RATE` | No | `0.15` | Breaker trips above this auto-action failure rate (analysis failures + bounce reverts) |

Example `.env`:
```bash
SONAR_TOKEN=<your_sonar_token>
NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL=<your_google_chat_webhook_url>
TICKET_WEBHOOK_AUTH_TOKEN=<your_webhook_auth_token>
WEBHOOK_PORT=65333
OPENCODE_PATH=/root/.opencode/bin/opencode
AGENT_WORKSPACE=<path-to-repo>
OPENCODE_MODEL=opencode/big-pickle
MAX_BATCH_SIZE=3
BATCH_TIMEOUT_MS=2700000
IHN_TOWER_THRESHOLD=80
PEAK_WINDOW_TZ=America/Chicago
PEAK_WINDOW_START=20:00
PEAK_WINDOW_END=21:00
# Only if using the MCP server:
# NOC_MCP_PASSPHRASE=<your_secure_passphrase>
# Only if using the MyMate monitor:
# MYMATE_URL=<url>
# MYMATE_USER=nocagent@example.com
# MYMATE_PASS=<your_my_mate_password>
```

### Org resources

The registry holds the addresses of this organization's own services. Values live in `site-pack/profile.json` under the key `resources`. The registry holds no secrets.

URL names: `uisp`, `librenms`, `mymate`, `oxidized`, `sonar`. Spreadsheet names (Google spreadsheet IDs): `tower_directory`, `ip_network`, `tracker`. Additional names can be registered with `set` (see `scripts/resource_catalog.json`).

```bash
python3 scripts/resources.py list             # every resource, its value, and where it came from
python3 scripts/resources.py get <name>       # print one resource
python3 scripts/resources.py set <name> <url> # register a resource
python3 scripts/resources.py migrate          # show what .env and older settings already hold
python3 scripts/resources.py migrate --write  # register those values
python3 scripts/resources.py check            # validate the registry
python3 scripts/resources.py check --probe    # validate and try each URL without credentials
```

Lookup order: environment variable (real environment first, then `.env`), then the `resources` block in `site-pack/profile.json`, then an older location (`org.uisp_host`, `org.sonar_base_url`, and `oxidized_base_url` in `site-pack/topology/temp-bgp-config.json`). When two sources disagree the first wins; `list` and `check` report the conflict.

A resource that is not registered is unavailable: `get` exits with status 3, and the agent stops and asks the operator instead of taking an address from an example, another system's device list, or DNS. `set` refuses example and placeholder values: an `example.com` host, a value in angle brackets, or a documentation address range. `set` and `migrate --write` refuse to run for batch agents, because the webhook server sets `NOC_UNATTENDED=1` for them; this is a guard against an agent registering an address on its own, not a security boundary. The MCP server allows the registered UISP host name for the `ubnt-uisp` device type without an entry in `NOC_READ_ALLOWED_HOSTS`, and for no other device type.

### Team roster and shared pack

When several people share one setup, the roster (`site-pack/team.json`) and the shared pack are described in [team.md](team.md). These variables are optional and listed in `.env.example`:

| Variable | Default | Description |
|----------|---------|-------------|
| `NOC_OPERATOR` | the handle saved by `scripts/team.py me`, else none | Who is running this: a handle from the roster, or `batch` for the webhook server's agents. Written into the decision log and the MCP audit log |
| `NOC_TEAM_FILE` | `<site-pack>/team.json` | Alternate roster path |
| `NOC_USER_CONFIG` | `~/.config/nocagent/user.json` | Per-user settings: the saved handle and `pack.require_signed` |
| `NOC_PACK_GIT_DIR` | `<site-pack>/.pack.git` | Alternate git directory of the shared pack |
| `NOC_PACK_SSH_KEY` | `~/.config/nocagent/pack_deploy_key` | Deploy key `pack.py` uses for git when the file exists (made by `pack.py deploy-key`) |

### Temp OSPF Google Sheet Helper

The helper is invoked on demand by the `temp-site-connection` skill or directly from the repository. It uses read-only OAuth scope unless write mode is deliberately enabled.

1. Enable Google Sheets API in the service-account project and share the dedicated spreadsheet with the service-account email as **Viewer**.
2. Transfer its JSON key to the host using an approved secret-transfer path. Keep it outside the repo, e.g. `~/.secrets/temp-ospf-sheets.json`, in a `0700` directory and `0600` file. Do not paste key contents into chat. The current webhook systemd service runs as root, so key-file permissions do not isolate the key from that same-root process; create a dedicated helper identity/process if that separation is required.
3. Add these non-secret values to `.env` (do not replace the rest of the file):

```dotenv
TEMP_OSPF_SPREADSHEET_ID=<spreadsheet-id-from-url>
TEMP_OSPF_CREDENTIALS_FILE=~/.secrets/temp-ospf-sheets.json
TEMP_OSPF_TAB=Temp OSPF
TEMP_OSPF_ACCESS=readonly
TEMP_OSPF_READ_ROWS=40
```

4. Run `node scripts/temp_ospf_sheet.mjs inspect`. This reads metadata and at most the configured preview rows. The live `Temp OSPF` tab uses paired single-host rows (col B host IP, col C site, col E `In use:`, col G reason; consecutive pairs = one `/30`). Share only relevant schema details, not credentials/tokens.
5. For a reviewed reservation writer only, grant the consenting Google user Editor access and set `TEMP_OSPF_ACCESS=write`, `TEMP_OSPF_WRITE_ENABLED=true`, `TEMP_OSPF_HEADER_ROW=1`, and re-consent with `oauth-url --scope write`. The three gates are (a) write access setting, (b) explicit kill switch, and (c) CLI `--confirm-write true`. Always dry-run first with `plan-reservation` (read-only: locates the pair, reports free/occupied, lists exact cells).
6. After independently selecting an existing free `/30` pair and checking live network overlap, reserve it with:

```bash
node scripts/temp_ospf_sheet.mjs mark-in-use \
  --subnet <selected-private-/30-network> \
  --site-a '<first-site>' --site-b '<second-site>' \
  --incident-id '<incident-or-change-id>' --confirm-write true
```

The writer rejects duplicate/missing/non-consecutive host rows, occupied pairs, header-signature drift, and unexpected row changes. It batches site × 2 + checkbox × 2 + incident reason × 2, reads both rows back, verifies all values, and records an audit event. The checked value is boolean `true` (the target cell must have checkbox validation). Never retry an unknown outcome until checking the Sheet. This helper does not check network address conflicts or permit device writes. Disable row writes by unsetting `TEMP_OSPF_WRITE_ENABLED` or setting it to anything other than `true`.

### Encrypted Credentials (MCP Server)

The MCP server stores device credentials (MikroTik, UBNT, plus isolated customer pools) in an AES-256-GCM encrypted file. The passphrase lives only in the `NOC_MCP_PASSPHRASE` env var — never in the repo. Existing `run-diagnostics` keeps its type-wide bounded behavior for tower/router analysis; rename-site CPE reads use the separate `customer-airos` / `customer-mt` pools (same read-only commands, isolated credential lists — never mix customer secrets into infra pools). A separate exact-device airOS read path uses one encrypted device+host binding with no fallback. It is not operationally safe for production CPE until trusted ID↔host resolution and process isolation exist; no customer bindings are configured. See [mcp-server/README.md](../mcp-server/README.md).

```bash
# 1. Edit the template with your device credentials
cp mcp-server/config/credentials.example.json mcp-server/config/credentials.json
nano mcp-server/config/credentials.json

# 2. Encrypt it (writes credentials.enc alongside credentials.json; the plaintext source is not overwritten)
NOC_MCP_PASSPHRASE="<your_passphrase>" npm run encrypt --prefix mcp-server -- mcp-server/config/credentials.json
```

**Credential rotation:** Add the new credential at higher `priority` and remove the old one later. The server tries all credentials for a device type until one works — no downtime during rotation.

See [mcp-server/README.md](../mcp-server/README.md) for the full credential store format, available commands, and security features.

### Security Notes

- `.env` is gitignored at the repo root — never commit it.
- `mcp-server/config/credentials.json` is gitignored; only `credentials.example.json` is tracked.
- The MCP server redacts passwords, API tokens, private keys, serials, and non-allowlisted hostnames from all output before it reaches the agent. IPs and MACs are left visible (operator policy: they are the payload of ppp/ARP/DHCP/registration diagnostics and nearly always RFC 1918). Exact-device CPE credentials must use the separate one-attempt path, not the shared type-level pool; it does not establish a trusted device-ID-to-host match or provide a Site Survey operation.
- The MCP server read tools (`run-diagnostics`, `run-exact-device-diagnostics`, `lookup-device-by-exact-ip`, `list-device-types`) accept only allowlisted commands and fail closed on unknown input; write tools (`routeros-write`, `temp-*`) are available but gated and off by default.
- N8N integration examples use placeholders for all tokens. See [docs/n8n-integration.md](../docs/n8n-integration.md).

---

