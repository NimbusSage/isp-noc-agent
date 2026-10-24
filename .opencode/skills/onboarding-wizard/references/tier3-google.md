# T3 reference - Google user OAuth (Sheets and Calendar)

The agent acts as the consenting Google user: one Desktop OAuth client, one owner-only
JSON file per feature outside the repo holding `client_id`, `client_secret` and
`refresh_token`. `.env` stores only the file PATHS. Use a dedicated Workspace user,
not a personal account.

## Console steps (operator)

1. Google Cloud console: create or pick a project; enable the Google Sheets, Google
   Drive and Google Calendar APIs.
2. OAuth consent screen: internal if Workspace, otherwise add the user as a test user.
3. Credentials: create an OAuth client ID of type Desktop app. If the console asks for a
   redirect URI, use `http://127.0.0.1:53682/callback`.
4. Put the client secret in an owner-only file outside the repo:
   ```bash
   mkdir -p ~/.config/nocagent && chmod 700 ~/.config/nocagent
   # in an editor: save ONLY the secret, one line, as ~/.config/nocagent/client_secret.txt
   chmod 600 ~/.config/nocagent/client_secret.txt
   ```
   The client ID is not secret and may be pasted in chat; the secret never is. The tool
   rejects `--client-secret` on the command line (it would show in `ps` and history) and
   also accepts `GOOGLE_CLIENT_SECRET` in the environment.

## Per-feature files and scopes

Run once per feature (commands print no tokens):
```bash
node scripts/google_user_auth.mjs init --oauth-file ~/.config/nocagent/tracker_oauth.json \
     --client-id CLIENT_ID_HERE --client-secret-file ~/.config/nocagent/client_secret.txt
node scripts/google_user_auth.mjs authorize --oauth-file ~/.config/nocagent/tracker_oauth.json \
     --scopes "https://www.googleapis.com/auth/spreadsheets.readonly"
node scripts/google_user_auth.mjs status --oauth-file ~/.config/nocagent/tracker_oauth.json
```
Commands of the tool: `init | authorize | refresh | status`. `refresh --scope openid`
is a silent ok or fail check. `init` writes the file with mode 0600; the tool refuses
files inside the repo or with loose permissions.

| Feature | OAuth file | Scopes (space-separated in one `--scopes` string) |
|---|---|---|
| Tracker sheet (read) | `tracker_oauth.json` | `https://www.googleapis.com/auth/spreadsheets.readonly` |
| Tracker sheet (write, later) | same file | `https://www.googleapis.com/auth/spreadsheets` |
| Calendar dispatch | `calendar_oauth.json` | `https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly` |
| Temp-OSPF sheet | `temp_ospf_oauth.json` | `https://www.googleapis.com/auth/spreadsheets.readonly` (write later) |

Start read-only. A new scope later means one more `authorize` run; the stored refresh
token then covers everything granted.

## The consent callback

`authorize` prints a consent URL and listens on `127.0.0.1:53682` for five minutes (the
port is fixed). Open the URL in a browser, consent as the Workspace user, and the page
says it can be closed.

Headless server or remote VM: the browser is on the operator's laptop, but the listener
is on the server. From the laptop run
`ssh -L 53682:127.0.0.1:53682 USER@SERVER`, keep it open, then open the printed URL in
the laptop browser; Google redirects to `127.0.0.1:53682`, which the tunnel carries to
the server. If port 53682 is busy on either side, stop whatever uses it first.

## Settings (from the Google block of `.env.example`)

Point every path at the operator's real home folder, absolute (the example shows
`/root/...`; change it). Spreadsheet IDs are not secret.

| Feature | Keys |
|---|---|
| Calendar dispatch | `CALENDAR_AUTH_MODE=oauth`, `CALENDAR_OAUTH_FILE`, `CALENDAR_WRITE_ENABLED=false`, `CALENDAR_FALLBACK_CALENDAR_ID`, `CALENDAR_TECH_MAP_FILE` (default `site-pack/tech_calendars.json`), `DISPATCH_TIMEZONE`, `DISPATCH_DEFAULT_START_HOUR`, `DISPATCH_DEFAULT_DURATION_MINUTES`, `DISPATCH_NOTIFY`, `DISPATCH_SONAR_ENABLED` |
| Tracker sheet | `TRACKER_AUTH_MODE=oauth`, `TRACKER_OAUTH_FILE`, `TRACKER_ACCESS=readonly`, `TRACKER_WRITE_ENABLED=false`, `TRACKER_SPREADSHEET_ID`, `TRACKER_TAB`, `TRACKER_PICK_TAB`, `TRACKER_HEADER_ROW` |
| Temp-OSPF sheet | `TEMP_OSPF_AUTH_MODE=oauth`, `TEMP_OSPF_OAUTH_FILE`, `TEMP_OSPF_ACCESS=readonly`, `TEMP_OSPF_WRITE_ENABLED=false`, `TEMP_OSPF_SPREADSHEET_ID`, `TEMP_OSPF_HEADER_ROW` |

The tracker sheet needs a specific column layout: `docs/setup/tracker-sheet-template.md`.
The tech to calendar map is `site-pack/tech_calendars.json` (copy from
`site-pack/example/`). `python3 scripts/env_set.py --list-missing --tier 3` names the keys
still empty. The three `*_WRITE_ENABLED` flags stay `false` until the operator decides.

## Verify

```bash
node scripts/google_user_auth.mjs status --oauth-file ~/.config/nocagent/tracker_oauth.json
python3 scripts/doctor.py --tier 3
```
Proof: `[PASS] oauth_files` listing each configured file. Optional: with the operator
watching, ask the agent to read the first rows of the tracker tab (read-only).

## T3 failures

| Symptom | Fix |
|---|---|
| `client secret missing` | pass `--client-secret-file` or export `GOOGLE_CLIENT_SECRET` |
| `must be outside the repository` | move the OAuth file under `~/.config/nocagent` |
| permissions too broad | `chmod 600` the file, `chmod 700` its folder |
| browser shows a redirect or connection error | the tunnel is missing or the port is taken |
| `access_denied` or "app not verified" | add the user as a test user, or use an internal app |
| `Consent failed` or timeout after 5 minutes | rerun `authorize` and finish consent faster |
| `invalid_grant` on refresh | refresh token revoked; rerun `authorize` |
| Sheets 403 | the Workspace user lacks access to that spreadsheet |
