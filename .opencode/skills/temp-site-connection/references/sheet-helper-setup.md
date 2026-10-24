# Temp OSPF Sheet helper: setup, auth and write conventions (reference)

Moved out of `SKILL.md` ("Available now: Temp OSPF Sheet helper"). Read when configuring Sheet access, enabling the constrained pair writer, or investigating a helper failure.

The skill is usable from an operator chat prompt (for example, `Use temp-site-connection to inspect the Temp OSPF allocation sheet`) and directly from this repository with Node.js 20+:

```bash
node scripts/temp_ospf_sheet.mjs inspect
```

`inspect` authenticates using the service account in `.env`, requests the Sheets read-only OAuth scope, and returns spreadsheet/tab metadata plus only the configured bounded range (default 40 rows × up to 52 columns). It never writes.

## Configure Google Sheets access

1. In Google Cloud, enable the Google Sheets API and create a service account (no domain-wide delegation required). Share only the target spreadsheet with the service-account email as **Viewer** to inspect schema. Sheet permissions are file-level; use the correct dedicated spreadsheet and do not grant Editor during inspection.
2. Deliver the downloaded JSON key directly to this machine using the organization's approved secure transfer (never chat/Git/tickets). Store it outside the repo, e.g. `~/.secrets/temp-ospf-sheets.json`. This service runs in an operator shell as the current user; the deployed webhook service runs as root. File permissions prevent other OS users from reading the key, but they do **not** isolate it from other code running as root, including the webhook agent. Use a dedicated least-privilege helper account/process if that threat boundary is required; do not claim chmod alone is process isolation.
3. Edit `.env` locally without printing or pasting its contents. Add the following, replacing placeholders with the spreadsheet ID (from its URL) and key path. Keep the rest of the existing `.env` unchanged:

```dotenv
TEMP_OSPF_SPREADSHEET_ID=<spreadsheet-id-from-url>
TEMP_OSPF_CREDENTIALS_FILE=~/.secrets/temp-ospf-sheets.json
TEMP_OSPF_TAB=Temp OSPF
TEMP_OSPF_ACCESS=readonly
TEMP_OSPF_READ_ROWS=40
# Writes stay off during discovery; do not set TEMP_OSPF_WRITE_ENABLED yet.
```

4. Create the key file in that location, owned by the OS identity that runs the helper, with mode `0600` (parent directory `0700`). The helper rejects keys inside the repository and files with group/other permission bits. Confirm the service-account principal is shared on the spreadsheet.
5. Run `node scripts/temp_ospf_sheet.mjs inspect`. If OAuth fails: check Sheets API enablement, spreadsheet sharing, spreadsheet ID, key-file path/permissions, and network egress. Do not paste the key or OAuth token into chat. Inspect output locally and share only relevant header names and non-sensitive allocation conventions with the agent.
6. Determine and confirm the actual one-based header row, exact header text for subnet/in-use/site A/site B, whether `TRUE`/checkbox is the established active value, and a test/copy-sheet procedure. Do not enable writes based merely on header guesses.

## External-domain sheets: user-OAuth instead of service-account share

When the `Temp OSPF` spreadsheet lives outside the company domain and its sharing policy blocks adding the service-account email, keep the service-account path off and use a user-consent OAuth refresh token from a Google identity that already has sheet access. The helper supports both modes via `TEMP_OSPF_AUTH_MODE`:

```dotenv
TEMP_OSPF_AUTH_MODE=user-oauth
TEMP_OSPF_OAUTH_FILE=~/.secrets/temp-ospf-oauth.json
TEMP_OSPF_SPREADSHEET_ID=<spreadsheet-id-from-url>
TEMP_OSPF_TAB=Temp OSPF
TEMP_OSPF_ACCESS=readonly
TEMP_OSPF_READ_ROWS=40
```

Setup (operator-run, secrets never in chat/Git/tickets):

1. In Google Cloud, create an OAuth client ID of type **Desktop app**. Download the client JSON via the organization's approved secure transfer.
2. Store `~/.secrets/temp-ospf-oauth.json` outside the repo, mode `0600` (parent `0700`), shaped as `{ "client_id": "...", "client_secret": "..." }`. The helper rejects files inside the repo or with group/other bits.
3. Run `node scripts/temp_ospf_sheet.mjs oauth-url --scope read` (use `--scope write` only after schema review and an Editor grant to your user). Open the printed URL as the Google user with sheet access, approve, and copy the `?code=` value.
4. Run `node scripts/temp_ospf_sheet.mjs oauth-exchange --code <code>` with the same redirect. It confirms a refresh token was issued; store it into the OAuth file as `refresh_token` via secure transfer (do not paste the token into chat). Re-run `inspect` to verify.
5. Refresh tokens act as the sheet user: use a least-privilege account with access to only the dedicated spreadsheet, and treat token rotation/revocation as part of offboarding. `inspect` uses the read-only scope; `mark-in-use` requests the full Sheets scope only when writes are gated on.

## Shared Google auth for other skills (`scripts/google_user_auth.mjs`)

The user-OAuth logic now lives in a shared module so any skill can act as the consenting user. From a script: `import { getGoogleUserToken } from './google_user_auth.mjs'` then `getGoogleUserToken(oauthFile, scope)` for any allowlisted scope (`spreadsheets.readonly`, `spreadsheets`, `drive.readonly`, `drive.file`). Adding a scope to a new skill = run `oauth-url` with the wider scope, re-consent once, and the stored refresh token covers it. Scope allowlist, owner-only file guards, and token caching are centralized there; `temp_ospf_sheet.mjs` is its first consumer.

## Enable the constrained pair writer (only after live schema review)

Live schema: header row 1 holds `/30` over the host-IP column (B), `Router name` (C — the per-row router/site column, formerly unlabeled), `Which radios are used` (D), `In use:` (E), `Date installed:` (F), `reason` (G), `date re-opened` (H). Each `/30` is a consecutive pair of host rows (e.g. `.49` + `.50` at rows 39/40). A free pair shows router-name `open` (or blank) + unchecked + empty reason; released rows additionally carry a `date re-opened` stamp, which does NOT block reuse. If the owner re-opens every reservation in bulk (all rows unchecked + stamped MM/DD/YYYY) when reverting prior temp connections, the historical reservation state is wiped; do not trust older incident files' row references. The helper fails closed on any header drift.

House write conventions: `mark-in-use` writes the two `Router name` cells (the near/far ROUTER names, e.g. `quebec pppoe` / `papa pppoe`), checkbox × 2, incident reason × 2 (6 cells; never touches `date re-opened`). `release` writes the same cells freed PLUS stamps `date re-opened` = today on both rows (8 cells, `USER_ENTERED`), matching the owner's own release habit.

Keep read-only access for inspect/plan. To enable writing, an authorized operator must grant the consenting Google user Editor access to the dedicated spreadsheet (Sheets cannot scope to one tab), set `TEMP_OSPF_HEADER_ROW=1`, then add the separate gates to `.env`:

```dotenv
TEMP_OSPF_ACCESS=write
TEMP_OSPF_WRITE_ENABLED=true
TEMP_OSPF_HEADER_ROW=1
```

`TEMP_OSPF_WRITE_ENABLED` is the runtime kill switch; remove it or set it to anything other than `true` to disable writes. Always run `plan-reservation` first (read-only: locates the pair, reports free/occupied, lists the exact 6 cells). The writer validates private aligned `/30` syntax, distinct router/site names, the full header signature anchored at `/30`, exactly one row per host with the two rows consecutive, and both rows free (`open`/blank + unchecked + empty reason — an old `date re-opened` stamp still counts as free); it re-reads both rows immediately before a single atomic 6-cell Sheets batch update (Router name × 2, checkbox TRUE × 2, incident reason × 2), then reads both rows back, verifies hosts + router names + checks + reasons, and writes a local JSONL audit record. `release` is the reverse (free both rows + stamp `date re-opened` today on both rows) and is likewise readback-verified. Duplicate/missing/non-consecutive rows, occupied rows, signature drift, concurrent local invocation, API error, audit failure, or unknown readback fail closed. An uncertain write is never automatically retried: inspect the rows in Google Sheets first.

Exact operator command (the CLI confirmation is an additional deliberate gate):

```bash
node scripts/temp_ospf_sheet.mjs mark-in-use \
  --subnet <selected-private-/30-network> \
  --site-a '<first-site>' --site-b '<second-site>' \
  --incident-id '<incident-or-change-id>' --confirm-write true
```

The helper only records the reservation; it does not verify that either usable host address is free on routers/routes. Perform approved live overlap checks and get a definitive selected allocation before invoking it. Read back its `marked_in_use` result and audit entry. The reservation is an input to the live executor (its preflight re-checks conflicts); the policy authorization does not replace the executor's gates.
