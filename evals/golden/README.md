# Golden Eval Set

`example_set.json` is the tracked GENERIC example so the harness runs with zero setup.

Your org's real golden set belongs in `site-pack/evals/golden_set.json` (untracked).
Tooling resolves: `--golden` flag -> `site-pack/evals/golden_set.json` -> `example_set.json`.

Build yours from Sonar history, then hand-fix labels:
```
python3 evals/build_golden_set.py --limit 100
# review a sample, hand-fix mislabels in site-pack/evals/golden_set.json
python3 evals/build_golden_set.py --add-incident <ticket_id>
```
The classifier stays generic; only integer categories become profile/env-driven (see AGENTS.md).

Nothing real belongs in this folder: `.gitignore` ignores everything here except this README and the
example set, and the builder and `metrics_rollup.py --incident` write to `site-pack/evals/` by default.
A real golden set holds ticket subjects and snippets from your Sonar, so share it only through a private
repository (see `docs/team.md`), never the public one.
