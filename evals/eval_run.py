#!/usr/bin/env python3
"""
eval_run.py — Run the golden eval set against the deterministic classifier (and,
optionally, replay the real agent in dry-run mode).

Guide criteria covered: "Build an eval dataset from day one", "Run evals weekly",
"Agent drift is real — run automated regression tests".

Modes:
    deterministic (default)  classifier.py vs golden_set.json — fast, no Sonar reads,
                             no network; catches harness + labeling drift.
    --replay                 ALSO calls the OpenCode agent per ticket with a
                             classification-only prompt and DRY_RUN=1 so no Sonar
                             writes happen (see env contract below). Slow + costs
                             tokens — intended weekly.

Outputs (default dir <state dir>/evals, i.e. logs/evals; override with --out or NOC_EVAL_DIR):
    eval_<date>.json   full results of this run
    latest.json        summary of THIS run - rewritten every run, regressed or not
    baseline.json      the accepted reference run. Written ONLY by `--update-baseline`.
                       A regressed run never touches it, so a bad week cannot silently
                       become the new normal (the old single "latest" file did exactly that).
    stdout summary + exit 2 on regression vs baseline.json

Regression gate:
    deterministic accuracy must not drop more than EVAL_MAX_REGRESSION_PCT (default 2)
    percentage points below baseline.json (same golden set). No baseline yet (fresh
    install, CI): the run succeeds, reports baseline "none", and
    `--update-baseline` creates one. Legacy installs: eval_latest_deterministic.json is
    read as the baseline until baseline.json exists (it is never written again).
    `--update-baseline` is the explicit "accept this result" switch: it bypasses the
    regression exit for that run and replaces baseline.json.

Env:
    EVAL_MAX_REGRESSION_PCT   max allowed accuracy drop in points (default 2.0)
    NOC_EVAL_DIR              output dir (same as --out)
    DRY_RUN=1                 REQUIRED for --replay; refused without it (safety)
    OPENCODE_PATH / OPENCODE_MODEL  agent replay spawn (defaults mirror server)

Usage:
    eval_run.py [--golden evals/golden/golden_set.json] [--replay] [--limit N]
                [--out DIR] [--update-baseline]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))
import classifier  # noqa: E402
import noc_config  # noqa: E402
import statefile  # noqa: E402

MAX_REGRESSION = float(os.environ.get("EVAL_MAX_REGRESSION_PCT", 2.0))
LEGACY_BASELINE = "eval_latest_deterministic.json"


def eval_dir(cli_out) -> Path:
    if cli_out:
        return Path(cli_out)
    env = os.environ.get("NOC_EVAL_DIR", "").strip()
    return Path(env) if env else noc_config.state_dir() / "evals"


def load_baseline(out_dir: Path):
    """(baseline dict | None, source file name | None). baseline.json wins; the pre-2c
    eval_latest_deterministic.json is a read-only fallback."""
    for name in ("baseline.json", LEGACY_BASELINE):
        p = out_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                print(f"WARNING: {p} is unreadable; treating as no baseline", file=sys.stderr)
                continue
            if isinstance(data, dict) and data.get("deterministic_accuracy") is not None:
                return data, name
    return None, None


def accuracy(results: list) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r["match"]) / len(results) * 100


def run_deterministic(tickets: list) -> list:
    out = []
    for t in tickets:
        got = classifier.classify(t.get("subject", ""), t.get("description_snippet", ""))
        exp = t.get("expected", {})
        out.append({
            "ticket_id": t.get("ticket_id"),
            "expected_category": exp.get("classification_category"),
            "got_category": got["classification_category"],
            "expected_status": exp.get("expected_status"),
            "got_status": got["expected_status"],
            "match": got["classification_category"] == exp.get("classification_category"),
            "signals": got.get("signals", []),
            "derived_from": exp.get("derived_from", "unknown"),
        })
    return out


def run_replay(tickets: list) -> list:
    if os.environ.get("DRY_RUN", "") != "1":
        print("REFUSING: --replay requires DRY_RUN=1 env (safety: agent must not write Sonar during evals)", file=sys.stderr)
        sys.exit(1)
    opencode = os.environ.get("OPENCODE_PATH", "/root/.opencode/bin/opencode")
    model = os.environ.get("OPENCODE_MODEL", "opencode/big-pickle")
    out = []
    for t in tickets:
        prompt = (
            "You are the ISP NOC agent in EVAL MODE (DRY_RUN=1). Do NOT write to Sonar. "
            "Classify only. Return ONLY JSON: "
            "{\"classification\":{\"category\":\"slow_speeds|rain_fade|outage|peak_congestion|passthrough|duplicate|other\","
            "\"complexity\":\"low|medium|high\",\"priority\":\"P1|P2|P3|P4\"},"
            "\"confidence\":0-100,\"expected_status\":\"handled_duplicate|categorized|needs_human|auto_closed|auto_closed_ooo|"
            "auto_closed_maintenance|auto_bounced_ihn|queued_for_peak|correlated_bounced\"}\n\n"
            f"Subject: {t.get('subject', '')}\nDescription: {t.get('description_snippet', '')}"
        )
        try:
            # OpenCode v2 removed `run --dir` (see server/index.js); the workspace is the spawn cwd.
            r = subprocess.run(
                [opencode, "run", "--model", model, prompt],
                capture_output=True, text=True, timeout=300, cwd=str(ROOT),
                env={**os.environ, "DRY_RUN": "1"})
            raw = r.stdout.strip()
            # Extract first JSON object from output
            start = raw.find("{")
            end = raw.rfind("}")
            parsed = json.loads(raw[start:end + 1]) if start != -1 and end != -1 else {}
            got_cat = (parsed.get("classification") or {}).get("category")
            exp = t.get("expected", {})
            out.append({
                "ticket_id": t.get("ticket_id"),
                "expected_category": exp.get("classification_category"),
                "got_category": got_cat,
                "expected_status": exp.get("expected_status"),
                "got_status": parsed.get("expected_status"),
                "match": got_cat == exp.get("classification_category"),
                "confidence": parsed.get("confidence"),
                "raw": raw[:200],
            })
        except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as e:
            exp = t.get("expected", {})
            out.append({
                "ticket_id": t.get("ticket_id"),
                "expected_category": exp.get("classification_category"),
                "got_category": None,
                "expected_status": exp.get("expected_status"),
                "got_status": None,
                "match": False,
                "error": str(e)[:200],
            })
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Run golden eval set")
    p.add_argument("--golden", type=Path, default=None)  # site-pack -> example set
    p.add_argument("--replay", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", type=Path, default=None,
                   help="output dir (default: NOC_EVAL_DIR, else <state dir>/evals)")
    p.add_argument("--update-baseline", action="store_true",
                   help="accept this run: write it as baseline.json (and skip the regression exit)")
    args = p.parse_args()
    out_dir = eval_dir(args.out)

    if args.golden is None:
        site_pack = ROOT / "site-pack" / "evals" / "golden_set.json"
        example = ROOT / "evals" / "golden" / "example_set.json"
        args.golden = site_pack if site_pack.exists() else example
        if args.golden == example:
            print(json.dumps({"note": "no site-pack golden set; running the generic example set",
                              "hint": "python3 evals/build_golden_set.py --limit 100"}))
    if not args.golden.exists():
        print(json.dumps({
            "error": "golden set not found",
            "hint": "run: python3 evals/build_golden_set.py --limit 100",  # then edit site-pack/evals/golden_set.json
            "path": str(args.golden),
        }))
        sys.exit(1)

    dataset = json.loads(args.golden.read_text())
    tickets = dataset["tickets"][:args.limit] if args.limit else dataset["tickets"]

    det_results = run_deterministic(tickets)
    det_acc = accuracy(det_results)

    # Split accuracy by label source: sonar-labeled = real ground truth signal;
    # keyword-labeled = tautological for a keyword classifier (kept for coverage only).
    sonar_labeled = [r for r in det_results if r.get("derived_from") == "sonar"]
    sonar_acc = accuracy(sonar_labeled) if sonar_labeled else None

    replay_results = []
    replay_acc = None
    if args.replay:
        replay_results = run_replay(tickets)
        replay_acc = accuracy(replay_results)

    # Regression gate vs the accepted BASELINE of the same golden set (comparing across
    # different sets would flag every fallback/site-pack switch).
    baseline, baseline_src = load_baseline(out_dir)
    prev_acc = None
    regression = None
    if baseline is not None:
        prev_acc = baseline.get("deterministic_accuracy")
        if prev_acc is not None and baseline.get("golden_set") == str(args.golden):
            regression = det_acc - prev_acc

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    regressed = regression is not None and regression < -MAX_REGRESSION
    summary = {
        "ran_at": now,
        "golden_set": str(args.golden),
        "golden_count": len(tickets),
        "deterministic_accuracy": round(det_acc, 2),
        "sonar_labeled_count": len(sonar_labeled),
        "sonar_labeled_accuracy": round(sonar_acc, 2) if sonar_acc is not None else None,
        "replay_accuracy": round(replay_acc, 2) if replay_acc is not None else None,
        "baseline": baseline_src or "none",
        "prev_deterministic_accuracy": prev_acc,
        "regression_points": round(regression, 2) if regression is not None else None,
        "max_regression_allowed": MAX_REGRESSION,
        "regression_detected": regressed and not args.update_baseline,
        "baseline_updated": bool(args.update_baseline),
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    statefile.atomic_write_text(out_dir / f"eval_{stamp}.json", json.dumps(
        {"summary": summary, "deterministic": det_results, "replay": replay_results}, indent=2) + "\n", mode=0o644)
    statefile.atomic_write_text(out_dir / "latest.json", json.dumps(
        {"ran_at": now, "golden_set": str(args.golden), "deterministic_accuracy": round(det_acc, 2),
         "regression_detected": summary["regression_detected"]}, indent=2) + "\n", mode=0o644)
    if args.update_baseline:
        statefile.atomic_write_text(out_dir / "baseline.json", json.dumps(
            {"ran_at": now, "golden_set": str(args.golden), "deterministic_accuracy": round(det_acc, 2)},
            indent=2) + "\n", mode=0o644)

    print(json.dumps(summary, indent=2))
    if summary["regression_detected"]:
        print(f"REGRESSION: deterministic accuracy dropped {abs(regression):.1f} points "
              f"({prev_acc} -> {det_acc}) — investigate before shipping changes", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
