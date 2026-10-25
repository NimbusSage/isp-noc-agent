# Site Pack — the org-specific layer

NocAgent is deliberately split into two layers so the repo can move between
installations (or become a shipped product) without leaking or breaking one
ISP's configuration.

## Layer model

| Layer | Location | What lives here | In git? |
|---|---|---|---|
| **Core agent** | `AGENTS.md`, `.opencode/skills/*`, `scripts/`, `server/`, `mcp-server/`, `evals/` | All workflow logic, gates, skills, governance | Yes (safe to share) |
| **Site pack** | `site-pack/` | THIS ISP's identifiers: Sonar group/category IDs, org name/domains, fiber topology, BGP maps, naming conventions, `profile.json` | `profile.json` and `site-pack/topology/` are gitignored; `profile.example.json` is tracked |

**Topology canonical rule:** the real, current network data lives in
`site-pack/topology/` — `fiber-map.json`, `bgp-rr-map.json`,
`fiber-testing-ips.json`, `fiber-topology.md`, `verified-probes.md`. Skills
(`path-analyzer`, `temp-bgp-peering`) read this FIRST. Record new discoveries here.

## Rules that keep the split clean

1. **No real credentials ever enter the core layer.** Tokens live in `.env`
   (gitignored) or secret files outside the repo (OAuth — enforced by
   `scripts/google_user_auth.mjs`). Device creds live encrypted in
   `mcp-server/config/credentials.enc` (gitignored, passphrase in `.env`).
2. **Org identity goes in `site-pack/profile.json`.** The same file holds the `resources` block, which registers this organization's service addresses (UISP, LibreNMS, MyMate, Oxidized, Sonar, and its spreadsheets) with `python3 scripts/resources.py set` (see `site-pack/profile.example.json`).
3. **Shared infrastructure details stay private.**
4. **Nothing in the core layer should reference a person's personal
   calendar, email, or machine paths.** Runtime paths come from `.env`
   (`AGENT_WORKSPACE`, systemd units) — never hardcode `/root/...` in new
   code; use `scripts/install.sh` which templates the unit files.

## Fresh-ISP checklist (what the onboarding wizard automates)

- [ ] `cp site-pack/profile.example.json site-pack/profile.json` and fill in
- [ ] `.env` from `.env.example` (wizard walks every group)
- [ ] Service addresses registered: run `python3 scripts/resources.py migrate` to see what `.env` already holds, then `set` for the rest, then `python3 scripts/resources.py check` to validate
- [ ] `mcp-server/config/credentials.json` from example → encrypted store
- [ ] Recreate `site-pack/topology/*.json` topology for your network
- [ ] Sonar group/category IDs verified against your instance
- [ ] `python3 scripts/doctor.py` all green
