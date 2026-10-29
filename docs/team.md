# Working as a team

> Operator manual index lives in the repo root [README](../README.md).

NocAgent works for one person with no extra setup. This page is for an organization where several people share one setup: the same service addresses, topology and device table, a role for each person, their own tokens, and logs that say who did what.

Two files and two scripts make that work:

- `site-pack/team.json` is the roster: who is on the team, their role, which credential pools are shared or personal, and which models the team has approved. Manage it with `scripts/team.py`.
- The site pack itself (`profile.json` with the service registry, `devices.md`, `topology/`, the roster, and so on) is shared through a **private** git repository. Manage that with `scripts/pack.py`. The public repository never sees any of it.

## Shared and personal

| Shared through the pack | Personal to each person |
|---|---|
| Service addresses (the `resources` block of `profile.json`), Sonar group and category IDs, topology, the device table, ticket-context notes, calendar names, the roster, the approved model list, org-private skills | `SONAR_TOKEN`, Google OAuth files, the `.env` file, their handle (`~/.config/nocagent/user.json`), the model they run, and whatever device logins the team gives them |

Registering a service once for everyone is the point: a colleague who joins gets the same UISP, LibreNMS and MyMate addresses without typing them, and brings only their own tokens. `python3 scripts/resources.py list` shows, for every service, whether the credential names it needs are present on this machine. It never prints a value.

## Roles

Every member has one role. A role is a set of capabilities, and `python3 scripts/team.py can <capability>` answers for the current operator (exit 0 means yes).

| Role | `read` | `comment` | `device_write` | `admin` |
|---|---|---|---|---|
| `viewer` | yes | | | |
| `tech` | yes | yes | | |
| `lead` | yes | yes | yes | yes |

`read` is analysis and lookups. `comment` is a Sonar comment, a tracker row or a calendar entry. `device_write` is any change to a device or a setting, and every such change still has to pass its own gates (kill switch, host allowlist, exact-device binding). `admin` is changing the roster. New people start as `viewer`.

The current operator is `NOC_OPERATOR`, else the handle saved by `python3 scripts/team.py me <handle>`. With no roster file, nothing is restricted. With a roster, a missing or unknown operator can read and nothing more. Batch runs from the webhook server are `batch`; the gates in `AGENTS.md` govern them, not the roster.

On a person's own machine the roster is a file that person can edit. There, roles guard against mistakes and against an agent doing more than it was asked; they are not a defence against the person at the keyboard. The enforcing limits are the systems themselves: with a personal Sonar token, Sonar applies that person's own permissions, and a device accepts only the logins it was given.

## Start a team (a lead)

1. Add yourself as the first member. The first member has to be a lead, and anyone can add them:

   ```bash
   python3 scripts/team.py add <handle> --name "<Full Name>" --role lead
   python3 scripts/team.py me <handle>
   ```

2. Fill in the rest of `site-pack/team.json` in an editor if you want it. The file looks like this:

   ```json
   {
     "version": 1,
     "members": {
       "<handle>": { "name": "<Full Name>", "role": "lead", "active": true }
     },
     "pools": { "mikrotik-ssh": { "scope": "shared" } },
     "approved_models": ["<provider>/<model>"]
   }
   ```

   `pools` records whether a device credential pool is `shared` (one login for the team) or `personal` (each person's own). `approved_models` is the list of models you allow for ticket work; `doctor.py` warns when the configured model is not on it, because ticket text can include customer details.

3. Check it: `python3 scripts/team.py check`.

4. Create a **private** repository for the pack, then publish:

   ```bash
   python3 scripts/pack.py init <private-remote-url>
   python3 scripts/pack.py git push -u origin main
   ```

   `init` refuses to run if any file looks like it holds a credential (tokens, keys, passwords, URL credentials), and so does the commit hook it installs. Addresses, names and IDs are fine; that is what the pack is for. Backups (`*.bak*`), `.env*` files and key files are never tracked. On the hosting side, require review for changes to `team.json`, `profile.json` and `skills/`, because those decide what the agent trusts.

5. Add a colleague, then share the roster:

   ```bash
   python3 scripts/team.py add <handle> --name "<Full Name>"
   python3 scripts/pack.py git add team.json
   python3 scripts/pack.py git commit -m "chore: add <handle>"
   python3 scripts/pack.py git push
   ```

   Send them the repository URL and their handle. Neither is a secret. Promote them later with `python3 scripts/team.py set-role <handle> tech`.

`team.py` refuses to remove the last active lead, and only a lead can change the roster.

## Join a team (a colleague)

Tell your agent `onboard me` and choose the team path, or do it by hand:

```bash
python3 scripts/pack.py join <private-remote-url>
python3 scripts/team.py me <handle>
python3 scripts/doctor.py --only team,resources
```

`join` fetches the pack, prints everything it would install and writes nothing until you say yes (or pass `--yes` after reading the list). It only accepts plain files in the places a pack may use: `profile.json`, `devices.md`, `tech_calendars.json`, `team.json`, and the folders `topology/`, `ticket-context/`, `rename-site/`, `evals/` and `skills/`. Symlinks, other paths and files that look like secrets or backups are refused, and a file that already exists in `site-pack/` is never overwritten.

After joining, create your own tokens (`python3 scripts/env_set.py SONAR_TOKEN`, and the Google OAuth files if you use the sheets and calendar). Do not ask a colleague for theirs and do not copy their `.env`. Ask a lead how the team provides device logins.

## Keeping the pack current

```bash
python3 scripts/pack.py status --fetch
python3 scripts/pack.py pull
```

`pull` lists what changed before it applies anything. Changes to things the agent trusts are listed first, one line each: a service address (`service uisp changed: <old> -> <new>`), a Sonar group or category ID, a member or role, a credential pool, an approved model, or a file under `skills/`. Everything else is listed by file name. Read the list, then apply it with `python3 scripts/pack.py pull --yes`. A swapped service address is exactly the kind of change this is meant to catch.

`pull` refuses when your copy has local commits the team does not have, and when files you edited would be overwritten. To accept only signed changes, put `{"pack": {"require_signed": true}}` in `~/.config/nocagent/user.json`; a pull then needs a valid signature on the newest commit.

`join`, `pull`, `init` and `team.py` edits refuse to run when `NOC_UNATTENDED=1`, which is how the webhook server starts its batch agents. A batch run cannot change who is on the team or what the agent trusts.

## Who did what

Each record in `logs/decisions/<YYYY-MM>.jsonl` and each entry in the MCP audit log carries an `operator` field: the handle of the person, or `batch` for the webhook server and the agents it starts. A single-person install without a handle has no field. To see one person's decisions:

```bash
jq 'select(.operator == "<handle>")' logs/decisions/<YYYY-MM>.jsonl
```

## What this does not do yet

- Roles are not enforced across machines. Each machine reads its own copy of the roster.
- Device logins are still per machine. A colleague needs the credential store entries the team gives them; nothing serves a shared login from a central place.
- There is no shared record of who is working on which ticket. Check with your colleagues, or look at the ticket's comments.
