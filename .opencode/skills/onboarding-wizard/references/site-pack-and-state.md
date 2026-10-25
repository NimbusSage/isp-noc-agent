# Site pack and onboarding state

## Site pack (optional, after T1 or T2)

The tracked tree ships schemas and placeholders only. Your network's reality lives in
the untracked `site-pack/` (`site-pack/README.md` describes the layout; the fictional
copy-and-fill examples are in `site-pack/example/`: drop `.example` from the name).

- **Topology** (`site-pack/topology/`): fiber map, BGP route-reflector map, testing IPs,
  fiber-topology notes, temp BGP config. The skills read the site pack first. Build it
  by walking the network with the agent over the read-only device path (traceroutes,
  route tables). Never enter geometry from memory without a discovered hop behind it.
- **Org profile** (`site-pack/profile.json`): Sonar group and category IDs, the
  `resources` block (this organization's UISP, LibreNMS, MyMate, Oxidized and Sonar
  addresses and its sheet IDs; write it with `python3 scripts/resources.py set`, T1 step 3),
  portability tokens. T2 writes the IDs from discovery.
- **Device inventory** (`site-pack/devices.md`, example `devices.example.md`) and the
  dispatch tech to calendar map (`site-pack/tech_calendars.json`).
- **Naming standards** (`site-pack/ticket-context/naming-conventions.md` and
  `site-pack/rename-site/naming-convention.md`): do not paste a template. Derive them:
  inspect live devices (`run-diagnostics` hostnames, `/ppp active print`, radio identity
  fields, ticket subjects), present the inferred pattern with ambiguities (same-surname
  hosts, abbreviated tech initials), ask targeted questions until the standard is
  settled, then write both files. If the inventory contradicts itself, say so and let
  the operator set the rule.

Offer this phase; do not insist. Anything with a real hostname or address stays inside
`site-pack/` and never goes into a tracked file.

## State file

Path: `logs/onboarding_state.json` (the `logs/` folder is gitignored; create it if
missing). Read it at the start, rewrite it after each phase. It stores statuses and
the proving doctor row only, never a value from `.env`.

```json
{
  "ts": "2026-01-01T12:00:00Z",
  "harness": "claude",
  "tiers_selected": ["T0", "T1"],
  "phases": {"phase0": "pass", "t0": "pass", "t1": "pass", "t2": "skipped", "t3": "skipped"},
  "proof": {"t0": "demo.py exit 0", "t1": "[PASS] mcp_store decrypt-ok devicetypes=3"},
  "ready_tier": 1,
  "complete": false
}
```
Status values: `pass`, `fail`, `skipped`, `pending`. `ready_tier` is the highest tier whose
doctor run was green. `complete` is true when every selected tier is `pass`. On resume,
re-run doctor for each `pass` before trusting it; a regression flips it back to `fail`.
