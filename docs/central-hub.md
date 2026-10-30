# Setting up a central hub (or moving to a new machine)

> Operator manual index lives in the repo root [README](../README.md).

The hub is the machine that runs the ticket server and its batch agents for the whole team, with a team login for the model provider instead of one person's. The same steps move NocAgent to any new machine. Colleagues' own machines join the team through [team.md](team.md); they do not need any of this.

Run these steps in order. Nothing here changes the old machine except step 1, and the old machine keeps working until you cut over in step 9.

## 1. On the old machine: make the pack and an encrypted backup current

The site pack carries the shared, non-secret settings. The backup carries the secrets (`.env`, the credential store, OAuth files, the secret overlays).

```bash
python3 scripts/pack.py status --fetch          # the pack should be "ahead: 0" and "changes: 0"
python3 scripts/doctor.py --only team,resources
```

Create a passphrase file that never appears in your shell history or on a command line, then take an encrypted backup. Keep the passphrase in your password manager; the backup is useless without it, and the old machine does not need to keep the file.

```bash
umask 077; mkdir -p ~/.config/nocagent
read -rsp 'Backup passphrase: ' P; printf '%s' "$P" > ~/.config/nocagent/backup_passphrase; unset P; echo
BACKUP_PASSPHRASE_FILE=~/.config/nocagent/backup_passphrase python3 scripts/backup_runtime_config.py
ls -l /root/nocagent-backups | tail -3          # the newest file ends in .tgz.enc
```

The nightly timer runs without a passphrase, so its archives are plain `.tgz` files with `.env` and `credentials.enc` side by side. Do not copy one of those between machines. To make the nightly archives encrypted as well, give the backup service the passphrase file: `sudo systemctl edit noc-backup.service` and add `Environment=BACKUP_PASSPHRASE_FILE=/root/.config/nocagent/backup_passphrase`. That protects archives that leave the machine; it does not protect against someone who already controls it.

## 2. Prepare the new machine

An LXC or VM on your network. It needs `git`, `python3` (3.9 or newer), Node.js 20 or newer (22 LTS is a good choice), `openssl` and `openssh-client`. `sshpass` is only used by the device bring-up skills. The machine must reach the same devices and services the old one does: the routers and radios, UISP, LibreNMS, MyMate, Oxidized and Sonar.

Install **OpenCode 2**. The default install script and `npm install -g opencode-ai` install OpenCode 1, and the unattended permission profile (`deploy/opencode-batch.json`) is written for 2.x, so a hub built that way would run its agents without it:

```bash
curl -fsSL https://opencode.ai/v2/install | bash
opencode --version        # must print v2.x
```

## 3. Get the code

```bash
git clone https://github.com/<your-account>/<your-fork-or-the-public-repo>.git /opt/nocagent
cd /opt/nocagent
(cd server && npm ci)
(cd mcp-server && npm ci && npm run build)
python3 scripts/doctor.py --tier 0
```

Any path works; `scripts/install.sh` writes the service units for wherever the checkout is.

## 4. Join the team's site pack

The hub has no personal git login, so give it a deploy key for the private pack repository (a read-only key is enough unless this machine will publish):

```bash
python3 scripts/pack.py deploy-key
```

Add the public key it prints under the pack repository's Settings, Deploy keys. Then fetch the pack. It lists everything it would install and asks first:

```bash
python3 scripts/pack.py join git@github.com:<org>/<pack-repo>.git
python3 scripts/team.py me <your-handle>
```

## 5. Bring over the secrets

Copy the encrypted archive across (the passphrase travels separately, in your password manager), then place it. `--dry-run` shows the plan without writing; existing files are kept, so the pack from step 4 stays authoritative:

```bash
scp old-machine:/root/nocagent-backups/nocagent-config-<stamp>.tgz.enc /root/
umask 077; mkdir -p ~/.config/nocagent
read -rsp 'Backup passphrase: ' P; printf '%s' "$P" > ~/.config/nocagent/backup_passphrase; unset P; echo
export BACKUP_PASSPHRASE_FILE=~/.config/nocagent/backup_passphrase
python3 scripts/backup_runtime_config.py --install /root/nocagent-config-<stamp>.tgz.enc . --dry-run
python3 scripts/backup_runtime_config.py --install /root/nocagent-config-<stamp>.tgz.enc .
shred -u /root/nocagent-config-<stamp>.tgz.enc ~/.config/nocagent/backup_passphrase
```

This places `.env`, the encrypted credential store, the secret overlays and each OAuth file (at the path its `*_OAUTH_FILE` setting names, if that is under your home directory). Add `--with-state` to bring the decision log and circuit-breaker state as well. Anything it cannot place is listed as "not placed"; copy those by hand.

Then change what is specific to the hub in `.env`: the bind address and port you want n8n to call, and `AGENT_MODEL`. Use `python3 scripts/env_set.py <NAME>` for any secret.

## 6. Log in with the team model account

Create the team account with the model provider, then log in on the hub as the user that will run the service:

```bash
opencode auth login
opencode auth list
```

Set the model in `.env` (`AGENT_MODEL`) to one the team account can use (`opencode models` lists them). The roster's approved list should name it; see step 8.

To use OpenRouter models as well, run `opencode auth login`, choose OpenRouter and paste the API key at the hidden prompt; it is stored in `~/.local/share/opencode/auth.json` (mode 0600, never in `.env`). Check the exact model ids with `opencode models`.

Free tiers are small. OpenRouter's `:free` models allow 20 requests per minute and, until you have bought $10 of credit once, 50 requests per day (1,000 after that); the daily count resets at UTC midnight. One ticket analysis takes dozens of model requests (every tool call is one), so 50 a day covers only a couple of tickets. Free models can also be served by providers that log or train on prompts, and ticket text can include customer details: read the provider's data terms and your OpenRouter privacy settings before putting customer data through a free model, and list only the models you accept in `approved_models` (step 8). When the quota runs out, the batch fails and its tickets come back as `needs_human`; watch for that after the cutover.

## 7. Check everything before it takes tickets

```bash
node server/index.js --check-config --env-file .env
python3 scripts/doctor.py
python3 scripts/pack.py status --fetch
```

All rows should be PASS or intentionally not configured. Then follow "Send a test ticket" in [webhook-api.md](webhook-api.md) against the hub's own address. Do not point n8n at the hub yet.

## 8. Roster, pools and approved models

```bash
python3 scripts/team.py list
python3 scripts/team.py add <handle> --name "<Full Name>"          # a colleague, as a viewer
python3 scripts/team.py set-role <handle> tech
```

Edit `site-pack/team.json` for `approved_models` (the models you allow for ticket work, written as `<provider>/<model>`) and `pools` (whether each device credential pool is `shared` or `personal`), run `python3 scripts/team.py check`, then publish:

```bash
python3 scripts/pack.py git add team.json
python3 scripts/pack.py git commit -m "chore: roster and approved models"
python3 scripts/pack.py git push
```

`doctor.py` warns when the configured model is not on the approved list, because ticket text can include customer details. See [team.md](team.md) for the roles.

## 9. Cut over

Two machines must never process the same tickets, or each ticket is analysed and commented twice.

1. Install the service on the hub: `sudo bash scripts/install.sh --dry-run`, read the rendered units, then `sudo bash scripts/install.sh --enable-backup`.
2. Confirm `systemctl is-active noc-agent` on the hub and `curl` its `/health`.
3. Point n8n at the hub's address ([n8n-template.md](n8n-template.md)).
4. Watch the first real tickets on the hub (`GET /governance`, `logs/decisions/`).
5. Stop the old service: `sudo systemctl disable --now noc-agent` on the old machine.

Keep the old machine's backups for a while. Rotate any credential that was exposed during the move.
