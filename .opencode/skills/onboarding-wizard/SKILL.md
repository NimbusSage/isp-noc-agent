---
name: onboarding-wizard
description: Use when the operator says "onboard me", "set this up", "configure NocAgent" or has just cloned the repo and needs a working install on any coding-agent harness. Walks tier by tier (T0 demo, T1 devices, T2 Sonar and notifications, T3 Google) with a doctor-verified checkpoint after every phase, and every tier can be skipped.
---

# NocAgent Onboarding Wizard

Trigger phrase: `onboard me` (also "set this up", "configure NocAgent"). Goal: take
someone who just cloned the repo from nothing to a verified working install, on any
harness, without them reading the docs first. Deep detail lives in `references/`;
this file is the script you follow.

Nothing here depends on a harness-specific way of asking questions. Wherever this file says "ask", ask the operator in plain
text, one question at a time, offer numbered choices, and wait for the answer. If the
harness has no skill auto-discovery (Gemini CLI, Cursor), the operator starts you with
"read .opencode/skills/onboarding-wizard/SKILL.md and follow it".

## Ground rules (apply to every phase)

1. **Proof before claims.** A phase is `pass` only after you ran its doctor command
   and quoted the row that proves it. Doctor rows look like `[PASS] name  detail`,
   `[WARN]`, `[FAIL]` and `[  -  ]` (not configured or skipped). Exit code 0 with no
   `[FAIL]` row is green. No doctor line, no pass. Never say "should be fine".
2. **Secrets never pass through you.** Do not ask for, accept, repeat or store a
   secret. Do not run `cat`, `grep`, `head`, `source`, `env` or an editor on `.env`,
   `credentials.json` or any OAuth file, and do not edit `.env` yourself. The operator
   types every value in their own terminal (`scripts/env_set.py` uses a hidden
   prompt). You learn what is missing from `python3 scripts/env_set.py --list-missing
   --tier N` (key names only) and from doctor (it reads `.env` in-process and prints
   key names and statuses only). If a secret shows up in chat anyway: do not repeat
   it, say it must be treated as exposed (rotate it if it is real), carry on with
   `env_set.py`.
3. **No secret on a command line** (`ps` and shell history show it). Secrets come from
   a hidden prompt, an owner-only file, or `--generate`.
4. **Operator hands needed.** For anything with a hidden prompt, ask the operator to
   run the command. Claude Code: they type it in the session prefixed with `!`, for
   example `! python3 scripts/env_set.py LIBRENMS_TOKEN`. Any other harness: a
   separate terminal in the repo root. You run only the safe forms (`--generate`,
   `--list-missing`, `--fix-perms`, doctor).
5. **Every tier and phase is skippable.** Say in one line what stays off, mark it
   `skipped`, move on. Never nag.
6. **Resumable and idempotent.** Start by reading `logs/onboarding_state.json` if it
   exists and offer to continue. Re-run doctor for phases the file calls done instead
   of trusting it. Never overwrite an existing `.env`, `credentials.enc`,
   `site-pack/profile.json` or OAuth file without asking.
7. **Nothing that writes to production gets switched on here.** Leave
   `BOUNCE_AUTONOMOUS_ENABLED`, `NOC_WRITE_ENABLED`, `TEMP_SITE_WRITE_ENABLED`,
   `CALENDAR_WRITE_ENABLED`, `TRACKER_WRITE_ENABLED` and `TEMP_OSPF_WRITE_ENABLED` off.
   Tell the operator where to flip them later; do not flip them for them.
8. **Ask before** anything outside the repo (systemd units, `~/.config/nocagent`, git
   config), anything that sends data (chat test message, Sonar or Google calls) and
   anything with `sudo`.
9. **Examples use placeholders only**: `10.0.x.y`, `example.com`, `198.51.100.x`.
10. **On a red row**: show it, look it up in the table at the end of this file or in
    the tier reference, fix once, re-run doctor. Still red: offer to skip. Never fake
    green.

After each phase append its result to `logs/onboarding_state.json` (format in
`references/site-pack-and-state.md`) and give a one-line status: phase, pass or skip
or fail, the proving doctor row.

---

## Phase 0 - Where am I? (no credentials, about 5 minutes)

Say: "This checkout is not assumed to be a production box. It may be a laptop or a lab
VM. Phases 0 and T0 touch only this repo folder. Nothing is installed system-wide
unless you opt in to the optional server in T2."

1. **Harness.** Ask which coding agent is running you: `opencode`, `claude` (Claude
   Code), `codex`, `gemini`, `cursor`, `vscode`, or `generic`. Not sure: `generic`.
2. **Prerequisites.** Run `python3 --version`, `node --version`, `git --version`.
   Needs python3 3.9+, node 20+ (18 still runs but is end-of-life), git. `sshpass` is
   optional (only the reprogram and bring-up skills use it). Missing pieces: install
   hints in `references/phase0-environment.md`.
3. **Register the harness.**
   ```bash
   python3 scripts/harness_setup.py --list
   python3 scripts/harness_setup.py --harness <h> --dry-run
   python3 scripts/harness_setup.py --harness <h> --write
   python3 scripts/harness_setup.py --check --harness <h>
   ```
   Show the dry-run, get a yes, then `--write`. It registers the MCP server and links
   or copies the skills where the harness needs them (add `--skills` for codex,
   `--copy` where symlinks are unwelcome, `--force` only to change a file that
   already exists). Per-harness notes: `references/phase0-environment.md`.
   If `--check` says the harness must reload (new MCP entry or skills folder), tell
   the operator to restart the session and say `onboard me` again; the state file
   resumes you.
4. **Git secret-scan hooks** (changes this repo's git config, so ask first):
   `python3 scripts/harness_setup.py --hooks`
5. **Explain the credential guard in two sentences.** Secrets live only in `.env`
   (mode 0600) and the AES-encrypted device store; you never ask for them or read
   them, and Claude Code additionally denies reads and edits of `.env` in
   `.claude/settings.json`. Doctor and `env_set.py` are the only things that touch
   those values, and they print key names, never values.
6. **Verify:** `python3 scripts/doctor.py --tier 0`
   Proof: exit 0 and no `[FAIL]` row. Failure: `references/phase0-environment.md`.
   Skip: not skippable (it is the floor), but nothing in it needs an account.

## Phase 1 - Pick a tier

Explain in plain words and ask which to do now (cumulative is typical, any can be
skipped, T0 is recommended for everyone):

| Tier | What it gives you | What it needs |
|---|---|---|
| T0 demo | The whole ticket pipeline running against a fake Sonar and a stub agent. Proves the machine works. | nothing |
| T1 devices | Read-only path and wireless triage on MikroTik and Ubiquiti gear, LibreNMS lookups, the encrypted device-credential store. | device logins, optionally a LibreNMS token |
| T2 Sonar and notifications | Ticket analysis against your real Sonar, chat notifications, optionally the webhook server that N8N feeds. | Sonar API token, optional chat webhook |
| T3 Google | Tracker sheet, field-tech calendar dispatch, temp-OSPF sheet. | Google OAuth client and a Workspace user |

Record the chosen tiers in the state file, then run them in order.

---

## T0 - Demo (no credentials)

```bash
cd server && npm ci && cd ..
python3 scripts/demo.py
python3 scripts/doctor.py --tier 0
```
`demo.py` starts a mock Sonar and the real server on 127.0.0.1 with a synthetic
environment, posts 8 fictional tickets, and compares what happened to
`scripts/demo/fixtures/expected.json`. Nothing leaves the machine.

Walk the operator through the result using `references/tier0-demo.md` (one line per
ticket: what the agent decided and why, with `sed -n` or `jq` ranges of
`expected.json`). Offer one scenario run to see a safety gate refuse:
`python3 scripts/demo.py --scenario kill-switch`.

Proof: `demo.py` exits 0 with all checks passed, and doctor `--tier 0` is green.
Failure: demo exit 2 is a missing prerequisite (usually `server/node_modules`), exit 3
means the pipeline did not run (`--keep` prints a temp dir with `server.out`), exit 1
is a failed expectation. Details in the reference. Skip: say so, go to Phase 1 choices.

## T1 - Devices (MikroTik, Ubiquiti, LibreNMS)

Full walkthrough with the credentials layout: `references/tier1-devices.md`.

1. **Build the MCP server:** `cd mcp-server && npm ci && npm run build`
   Proof: `cd mcp-server && node dist/tool-cli.js --list` prints the tool names
   (`run-diagnostics`, `list-device-types`, and so on).
2. **Device credential store (AES-256-GCM).** The operator, in an editor, copies
   `mcp-server/config/credentials.example.json` to `config/credentials.json` (inside
   `mcp-server`) and replaces every `CHANGE_ME`. You never open that file. Then:
   - `python3 scripts/env_set.py --generate NOC_MCP_PASSPHRASE` (you may run this;
     the value is never printed; the operator should copy it from `.env` into a
     password manager with their editor, because losing it loses the store).
   - The operator runs, in their own terminal from the repo root:
     `set -a; . <(grep '^NOC_MCP_PASSPHRASE=' .env); set +a`, then
     `cd mcp-server && npm run encrypt -- config/credentials.json`.
   - `encrypt` does NOT delete the plaintext. The operator removes it
     (`shred -u config/credentials.json`, or `rm` where shred is missing). Confirm
     with `ls mcp-server/config`: `credentials.enc` present, `credentials.json` gone.
3. **LibreNMS and btest.** `LIBRENMS_URL` is not secret (for example
   `http://10.0.x.y`): the operator sets it with `env_set.py` or an editor. Secrets,
   run by the operator: `python3 scripts/env_set.py NOC_MCP_PASSPHRASE LIBRENMS_TOKEN
   BTEST_PASSWORD` for any of them still missing (skip a key you do not have; ask
   what each is for from `references/tier1-devices.md`).
4. **Read-host policy.** The MCP server only connects to private, loopback and
   link-local addresses by default. If the gear uses CGNAT (carrier-grade NAT) or public
   addresses, the operator sets `NOC_READ_ALLOWED_HOSTS` (comma list of exact IPs and
   CIDRs; it REPLACES the default, so include the private ranges too).
5. **Verify:** `python3 scripts/doctor.py --tier 1`
   Proof: `[PASS] mcp_store  decrypt-ok devicetypes=N` and, if you configured it,
   `[PASS] librenms ...`. Failure: `references/tier1-devices.md` (wrong passphrase,
   missing dist, host blocked). Skip: tell them path analysis, wireless health and
   device lookups stay off; tickets can still be analyzed from Sonar text alone.
6. **First read-only smoke test** (ask for one router address they own and confirm it
   is inside the allowed hosts):
   ```bash
   cd mcp-server
   node dist/tool-cli.js --list
   node dist/tool-cli.js run-diagnostics '{"host":"10.0.x.y","deviceType":"mikrotik-ssh","command":"system-resource"}'
   ```
   Proof: exit 0 and RouterOS version and uptime in the output. It is read-only by
   construction (command allowlist).

## T2 - Sonar and notifications

Full walkthrough, ID checklist and server hardening: `references/tier2-sonar-pipeline.md`.

1. **Connect Sonar.** The operator sets `SONAR_BASE_URL` (for example
   `https://example.sonar.software`) and then `python3 scripts/env_set.py SONAR_TOKEN`
   (Sonar admin, Security, API keys; a dedicated service user with the access the
   agent needs; never pasted in chat).
   Proof: `python3 scripts/doctor.py --only sonar_api` shows `[PASS] sonar_api`
   (read-only probe, an account search that matches nothing).
2. **Discover IDs.** Group and category IDs differ per Sonar instance.
   `python3 scripts/doctor.py --discover-ids` lists them (read-only) and matches by
   name. First make sure the operator has created the groups and categories listed in
   the checklist in the reference (NOC, CS Level 2 and Sys Admin groups; Slow Speeds,
   Night Checks, Rain Fade, CX/TH-side Issue categories). Show the proposed mapping,
   get a yes, then `python3 scripts/doctor.py --write-profile` to write
   `site-pack/profile.json`. Never overwrite an existing profile without showing a
   diff. Proof: `[PASS] site_profile` in `python3 scripts/doctor.py --only site_profile`.
3. **Notifications (optional).** `python3 scripts/env_set.py
   NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL` (the URL contains a key, so it is a secret).
   With the operator's OK: `python3 scripts/doctor.py --notify` posts ONE labeled test
   card. Proof: `[PASS] google_chat` and the operator sees the card. Skip: alerts stay
   in logs only.
4. **Pipeline server (optional; only if tickets will arrive by webhook from N8N).**
   Ask first; skipping leaves you with the interactive agent, which is fully usable.
   ```bash
   cd server && npm ci && cd ..
   python3 scripts/env_set.py --generate TICKET_WEBHOOK_AUTH_TOKEN
   node server/index.js --check-config
   sudo bash scripts/install.sh --dry-run
   ```
   Explain, from the reference: `AGENT_WORKSPACE` and `AGENT_HARNESS` (opencode,
   claude, codex, gemini, command); `WEBHOOK_BIND` defaults to 127.0.0.1 and should
   change to 0.0.0.0 only with `WEBHOOK_TLS_PROXY=1` when N8N is on another host
   behind TLS or a VPN; the bounce kill switch `BOUNCE_AUTONOMOUS_ENABLED` is set
   explicitly to `false` (never rely on the default) until the operator decides, and the thresholds are IHN 80 and correlated
   75. The real install (`sudo bash scripts/install.sh`, writes systemd units outside
   the repo) is run by the operator after reading the dry-run. N8N wiring:
   `docs/n8n-template.md`. Proof: `--check-config` prints no `FAIL`,
   `[PASS] webhook_server` after install, and the "Send a test ticket" section of
   `docs/webhook-api.md` returns 202.
5. **Verify:** `python3 scripts/doctor.py --tier 2`
   Proof: no `[FAIL]`; `sonar_api` and `site_profile` pass. Skip: Sonar-dependent
   skills (ticket analysis, bounce, comments) stay off.

## T3 - Google (Sheets and Calendar)

Full walkthrough, scopes and the headless tunnel: `references/tier3-google.md`.

1. **Operator, in the Google Cloud console:** enable the Sheets, Drive and Calendar
   APIs, create an OAuth client of type Desktop app, and save the client secret into an
   owner-only file outside the repo (`~/.config/nocagent/client_secret.txt`, mode 0600,
   folder 0700) with an editor. The client ID is not secret; the secret never goes in
   chat or on a command line.
2. **Create and consent**, one OAuth file per feature (tracker, calendar, temp OSPF):
   ```bash
   node scripts/google_user_auth.mjs init --oauth-file ~/.config/nocagent/tracker_oauth.json \
        --client-id <CLIENT_ID> --client-secret-file ~/.config/nocagent/client_secret.txt
   node scripts/google_user_auth.mjs authorize --oauth-file ~/.config/nocagent/tracker_oauth.json \
        --scopes "https://www.googleapis.com/auth/spreadsheets.readonly"
   ```
   `authorize` prints a consent URL and listens on `127.0.0.1:53682` for five minutes
   (fixed port). On a headless box the operator first runs
   `ssh -L 53682:127.0.0.1:53682 user@that-box` from their laptop and opens the URL in
   the laptop's browser. Use a dedicated Workspace user, not a personal account. Start
   read-only; add write scopes later only when they choose to enable writes.
3. **Settings:** the `CALENDAR_*`, `DISPATCH_*`, `TRACKER_*` and `TEMP_OSPF_*` block of
   `.env.example` (path to each OAuth file, spreadsheet IDs, tab names, time zone).
   The `*_WRITE_ENABLED` flags stay `false`. List what is still missing with
   `python3 scripts/env_set.py --list-missing --tier 3`.
4. **Verify:** `node scripts/google_user_auth.mjs status --oauth-file <path>` then
   `python3 scripts/doctor.py --tier 3`
   Proof: `[PASS] oauth_files` naming each configured file. Skip: tracker filing,
   calendar dispatch and the temp-OSPF sheet stay off.

## Optional - site pack (after T1 or T2)

The tracked tree ships schemas only; your network reality (fiber map, BGP route
reflectors, naming standards, device table) lives in the untracked `site-pack/`. Offer
to build it by discovery rather than from memory:
`references/site-pack-and-state.md`.

## Done - closing summary

Run the doctor for the highest tier reached one last time, then tell the operator:

- **Ready tier:** the highest tier whose doctor run was green (T0 to T3), and which
  tiers were skipped.
- **Unlocked:** one line per green tier (T0 safe sandbox; T1 read-only device triage;
  T2 ticket analysis and alerts; T3 sheets and dispatch).
- **Still off on purpose:** the write switches from rule 7, and how to enable each
  later (`docs/governance.md`, `docs/permissions-policy.md`).
- **Next steps:** ask the agent to analyze a ticket ("analyze Sonar ticket 12345",
  needs T2) or to check a router ("run system-resource on 10.0.x.y", needs T1); rerun
  `python3 scripts/demo.py --scenario breaker-open` to see a gate refuse; doctor any
  time with `python3 scripts/doctor.py`.
- **Where docs live:** `README.md` (index), `docs/ticket-pipeline.md`, `docs/config.md`
  (every setting), `docs/skills.md`, `docs/operations.md`, `docs/incident-runbook.md`,
  `docs/mcp-server.md`, `docs/governance.md`.

If a tier you attempted is not green, say exactly which row is red and stop short of
claiming it is ready.

## Quick failure table

| Row or symptom | Likely cause | Fix |
|---|---|---|
| `[FAIL] env_file  .env not found` | `.env` not created | `cp -n .env.example .env && chmod 600 .env`, or `python3 scripts/env_set.py --fix-perms` |
| `.env` permissions warning | mode wider than 0600 | `python3 scripts/env_set.py --fix-perms` |
| `[  -  ] mcp_store` | passphrase or `credentials.enc` missing | T1 step 2 |
| `[FAIL] mcp_store` wrong passphrase | `.enc` made with a different passphrase | re-encrypt from plaintext with the current one |
| `[WARN] mcp_store` dist missing | not built | `cd mcp-server && npm ci && npm run build` |
| tool says host not permitted | read-host policy | set `NOC_READ_ALLOWED_HOSTS` (T1 step 4) |
| `[FAIL] sonar_api` 401 or 403 | bad or under-privileged token | new token via `env_set.py SONAR_TOKEN` |
| `[WARN] site_profile` | no `site-pack/profile.json` | T2 step 2 |
| `[FAIL] oauth_files` file too open | OAuth file not 0600 | `chmod 600` it; parent folder `chmod 700` |
| OAuth consent never returns | headless box, no tunnel | SSH tunnel in T3 step 2 |
| `--check-config` WARN on bind | non-loopback bind without TLS ack | `references/tier2-sonar-pipeline.md` |

More rows per tier are in the references.
