# Portability: Moving & Reinstalling

> Operator manual index lives in the repo root [README](../README.md).

## Portability & Reinstalling

This repo is built to move: clone it anywhere, rename the org, reinstall on a
new box. The repo is **public**; org-specific reality lives entirely outside the
tracked tree (`.env`, `site-pack/`, OAuth files — all gitignored).
Your own fork can be private if you prefer, but the upstream must stay clean.

### Moving / reinstalling

```bash
git clone <repo-url> /any/new/path   # any path works
cd /any/new/path
sudo bash scripts/install.sh         # regenerates ALL systemd units at this path
cp -n .env.example .env              # then fill credentials (or: onboard me)
python3 scripts/doctor.py            # read-only verification per credential
```

For a full move with secrets, the pack and a team login, follow [central-hub.md](central-hub.md); `python3 scripts/backup_runtime_config.py --install <archive> <repo>` places an archive's files in the new checkout.

Nothing in the deployment is hardcoded to `<install-path>` anymore —
systemd units are **generated from `deploy/*.service.template`** with the
actual install path substituted at install time. Old units are overwritten,
so a reinstall on a new path replaces the old one cleanly (disable the old
service if moving, not cloning).

### Credential inventory (where every secret lives)

**Backup these before trusting any single box:** every secret below lives
ONLY on this machine (gitignored by design). `python3 scripts/backup_runtime_config.py`
archives `.env`, `site-pack/profile.json`, its secret overlay `site-pack/profile.local.json`
(the overlays under `site-pack/topology/` ride along with that tree; see [team.md](team.md)),
`credentials.enc` and all `*_OAUTH_FILE` files into `/root/nocagent-backups/` (mode 0600/0700, retention 14, nothing printed
or added to git); `--list <archive>` enumerates; `--restore <archive> <dir>` extracts
with 0600 modes. `BACKUP_DIR` and `BACKUP_KEEP` env vars are used. If the box dies:
restore members into the same paths on the new install (plus the passphrase and your
Google consent) and `scripts/doctor.py` confirms everything still works. Crypto note:
the tarball is unencrypted at rest — keep the backup dir as protected (0700/0600) as
the secrets it contains, ideally on a second encrypted disk.
Set `BACKUP_PASSPHRASE` or `BACKUP_PASSPHRASE_FILE` to encrypt the archive with openssl and delete the plaintext tar; without a passphrase the archive is a plaintext `.tgz` (mode 0600) and a warning is logged; `BACKUP_KEEP` (default 14) keeps the newest archives of either kind.

| Secret | Location | In git? |
|---|---|---|
| Sonar, webhook, MyMate, LibreNMS tokens | `.env` | never (gitignored) |
| Google user OAuth refresh tokens | outside the repo (`CALENDAR_OAUTH_FILE` etc.), mode 0600 | never (enforced at runtime) |
| Device credentials (MCP) | `mcp-server/config/credentials.enc` + passphrase in `.env` | never (both gitignored) |
| Org identity | `site-pack/profile.json` (Sonar IDs, org name/domains) | never (profile.json gitignored; profile.example.json tracked) |

### Verification = `scripts/doctor.py`

Read-only checks per credential: Sonar GraphQL, Google Chat webhook, webhook
server health, MyMate session login, LibreNMS, each OAuth token refresh, MCP
store decryption, site profile, systemd unit. `--json` for machines, `--only` /
`--skip` for single checks (exit 2 on unknown names); pass `--notify` to post ONE labeled test message to the
NOC chat space (`--skip chat` to stay silent). Exit 0 only when nothing fails.

### The onboarding wizard

Open the agent in the repo directory and say **`onboard me`**. The
`onboarding-wizard` skill walks: tour of abilities → `.env` baseline → Sonar →
Google (webhook + OAuth per feature) → optional integrations (MyMate,
LibreNMS, Tracker Sheet, Temp OSPF, dispatch) → device credential store (MCP) →
systemd install → site-pack profile → topology references → final all-green
gate, recording state in `logs/onboarding_state.json`. Optional phases can be
skipped; required phases fail closed (no fake "done").

### What NOT to share (contributor rules)

The upstream repo is public and must STAY clean:

- Never commit `.env`, `site-pack/`, MCP plaintext `credentials.json`, or OAuth
  secret files — all are gitignored by design. `python3 scripts/doctor.py --only
  portability_scan` (runs in CI) fails the build on any real RFC1918 IP or on
  your org's brand tokens in a tracked file.
- New org-specific values (group/category IDs, tower/device maps, calendars)
  go into your **untracked** site pack or `.env`, never into tracked files.
  `site-pack/example/` shows the tracked schema shapes.
