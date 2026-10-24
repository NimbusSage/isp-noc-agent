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
