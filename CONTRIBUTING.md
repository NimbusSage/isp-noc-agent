# Governance: who owns the core contract

NocAgent works because a stable set of files implement the agent's operating
contract — the workflow, the gates, the safety model. Community contributions
are very welcome for skills, integrations, porters (Cisco/Calix/juniper, other
harnesses), and docs; the files below change only under maintainer review so
the agent's behavior stays coherent and the safety gates cannot drift.

## Protected core (require maintainer review)

The path spec is intentionally tight — renames inside a directory are caught
too:

* `AGENTS.md` — the ticket workflow, thresholds, gates, and output contract
* `server/batch_prompt_template.md` — the agent prompt/output contract
* `scripts/sonar_bounce.py` — bounce gate (breaker + kill switch + anti-loop)
* `scripts/circuit_breaker.py` — governance breaker
* `.opencode/skills/` — see break-down below

Everything else (`README.md`, `docs/`, `mcp-server/` except CODEOWNERS paths, eval fixtures, new
skills) is normal-review territory: open a PR and expect comments, not veto.

## Skill tiers

| Tier | Paths | Rule |
|---|---|---|
| Safety-critical | `temp-bgp-peering`, `temp-site-connection*`, `reprogram-device`, `rename-site` | Maintainer review — these execute network changes |
| Core workflow logic | `path-analyzer`, `wireless-health`, `ticket-context`, `sonar-api`, `duplicate-detection`, `google-chat`, `tracker-sheet`, `correlated-*`, `ihn-auto-return`, `peak-scheduler`, `comment-formatting`, `schedule-field-tech`, `create-site`, `program-new-device` | Maintainer review |
| Additive knowledge | any NEW skill directory; small corrections that don't change behavior (typos, wording) | Normal review |

## How to contribute a change the right way

1. **New skills go in `.opencode/skills/<name>/SKILL.md`**, created with
   `python3 scripts/new_skill.py <name> --description "Use when ..." --summary "<one line>"`;
   it also lists the skill in `AGENTS.md` and `docs/skills.md`. Keep the body under ~500
   lines and move deep detail into `references/`. Rules and required checks are in
   `.opencode/skills/skill-authoring/SKILL.md`.
2. **Prompts/config stay org-agnostic.** No real IPs, org names, ticket IDs,
   or date-stamped "verified on" notes in tracked files — the CI portability
   gate will bounce you. Anything org-specific goes in the gitignored
   `site-pack/` or env vars.
3. **If your change affects operator-facing behavior** (workflow steps, new
   endpoint, new env var, changed skill), update the matching `README.md`
   section; workflow, env and endpoint details belong in the matching `docs/` page (linked from the README's Documentation index), and CI's docs check
   (`scripts/docs_check.py`) guards its cross-references.
4. **Behavior changes need verification**: run the offline test batteries
   (`node --test scripts/*.test.mjs`, `mcp-server npm test`,
   `python3 evals/eval_run.py --golden evals/golden/example_set.json`) and say
   what you exercised. Golden-set stories ("verified on ticket …") aren't
   needed; the git log already carries dates.

## Style expectations

- `AGENTS.md` stays lean: processes and gates, not narratives or per-site data.
- Prefer `python3 scripts/…` and `node scripts/…` command examples over prose.
- No secrets, ever. See `docs/permissions-policy.md` for agent permission tiers.

## Commit sign-off

Sign every commit with `git commit -s`. That adds a `Signed-off-by:` line to the commit message saying you have the right to submit the change under the project's license. The full text of the Developer Certificate of Origin is at https://developercertificate.org. This project is licensed GPL-3.0.

CI does not enforce this yet. It is a request, not a gate. Please do it anyway.

## Secrets, hooks, and guard files

Run `python3 scripts/harness_setup.py --hooks` once after each clone. It sets `core.hooksPath` to `.githooks` so the pre-commit hook is active.

The pre-commit hook runs `python3 scripts/secret_scan.py --staged`. It scans only the lines you are adding. If it finds a possible secret or real network identifier, it blocks the commit. It also blocks if `python3` or the scanner itself is missing. Do not use `git commit --no-verify`. CI runs the same scan on the full tracked tree, so bypassing the hook only moves the failure to CI.

Real values do not belong in tracked files. Use placeholders instead:

- Wrap config values in angle brackets: `<LIKE_THIS>`
- Reference an environment variable: `$BTEST_PASSWORD`
- Look up the value from `site-pack/profile.json` at runtime

For addresses: `10.0.x.y` for private ranges, `198.51.100.x` or `203.0.113.x` for public examples. Use `example.com` for domains.

If a literal value is intentional, say a fake credential in a test, add `secret-scan:allow(<reason>)` on that line. The reason is required. The scanner will not suppress without it.

Real passwords, tokens, and device credentials never go in the repo. They live in a local `.env` file and in an encrypted store, both outside git.

These files set the rules everything else depends on, and changes to them need maintainer review: `scripts/secret_scan.py`, `scripts/leak_denylist.sha256`, `.githooks/`, `.github/`, `.claude/settings.json`, `.mcp.json`, `opencode.json`.

If you work with a coding agent: Claude Code reads `CLAUDE.md` and `.claude/settings.json` in this repo; other agents read `AGENTS.md`. `CLAUDE.md` and `.claude/settings.json` tell Claude Code not to read `.env` or the credential files, and they block edits that contain secrets; `AGENTS.md` gives other agents the same rules, but a rule in `AGENTS.md` cannot block anything. Tell your agent it must not work around a blocked commit. Fix the content, not the gate.
