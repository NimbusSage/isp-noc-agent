#!/usr/bin/env python3
"""
circuit_breaker.py — Deterministic circuit breaker for NOC auto-bounces.

Deterministic code, not prompt: enforces the guide's ">5% error rate → pause auto-actions"
criterion with zero LLM involvement. The server (server/index.js feed_record) records
outcomes after every settled batch; scripts/sonar_bounce.py asserts this gate before any
updateTicket that changes ticket_group_id. The agent never opens/clears the breaker itself.

Actions (exit codes matter — sonar_bounce.py gates on exit code):
    record <outcome> [ticket_id]   record an auto-action outcome:
                                     auto_actions_ok | needs_human | analysis_failed |
                                     bounce_success | bounce_reverted
    status                         print status JSON (debugging / weekly report)
    open <reason>                  manually trip the breaker (blocks all bounces)
    clear                          manually clear a tripped breaker
    assert-bounce                  exit 0 if bounces allowed, exit 3 if BLOCKED

State file: <state dir>/circuit_breaker.json
    * every read-modify-write runs under an exclusive fcntl.flock on
      circuit_breaker.json.lock (the server spawns one `record` process per ticket,
      so several run at once);
    * writes are atomic: unique mkstemp file in the same directory -> fsync ->
      os.replace -> fsync(dir). No fixed `.tmp` name, no torn file.

Fail closed:
    A state file that exists but cannot be read/parsed (corrupt JSON, wrong shape,
    unreadable) is treated as an OPEN breaker with reason "corrupt_state: ...".
    The next mutating command moves the bad file aside to
    circuit_breaker.json.corrupt-<unix ts> (evidence is never overwritten) and
    persists the OPEN state. `clear` (operator action) is the only way back.

Trip alert (server pages Google Chat from this):
    When `record` newly trips the breaker it prints, on STDOUT, one JSON line
        {"event": "CIRCUIT BREAKER OPENED", "breaker_open": true, "tripped": true,
         "reason": "...", "opened_at": ..., ...}
    (the literal text matches what server/index.js greps for). Human-readable text
    still goes to STDERR. The normal `record` summary JSON follows as the last line.

Env:
    NOC_STATE_DIR           state directory (default: NOC_AGENT_LOG_DIR, else <repo>/logs)
    BREAKER_STATE_FILE      override the state file path entirely
    BREAKER_WINDOW_SEC      rolling window seconds (default 604800 = 7 days)
    BREAKER_MIN_RUNS        minimum runs in window before rate is evaluated (default 10)
    BREAKER_MAX_ERROR_RATE  trip threshold as fraction (default 0.15)
    A malformed numeric env value is a configuration error: the process exits
    non-zero (sonar_bounce.py treats any non-zero result as BLOCKED).

Failure classes:
    analysis_failed   path analysis / tower resolution failed (failure)
    bounce_reverted   bounced ticket was moved back to NOC within the window (worst signal
                      — the bounce was wrong; failure)
    needs_human       correct escalation — NOT a failure. Routing uncertain cases to humans
                      is desired behavior; the breaker tracks autonomous-action quality.

Exit codes: 0 ok / bounces allowed, 1 usage or internal error, 3 assert-bounce BLOCKED.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noc_config  # noqa: E402
import statefile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TRIP_EVENT = "CIRCUIT BREAKER OPENED"


def _env_number(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        print(f"ERROR: {name}={raw!r} is not a valid {cast.__name__}", file=sys.stderr)
        sys.exit(1)


STATE_FILE = Path(os.environ.get("BREAKER_STATE_FILE", "").strip() or noc_config.state_dir() / "circuit_breaker.json")

WINDOW_SEC = _env_number("BREAKER_WINDOW_SEC", 604800, int)
MIN_RUNS = _env_number("BREAKER_MIN_RUNS", 10, int)
MAX_ERROR_RATE = _env_number("BREAKER_MAX_ERROR_RATE", 0.15, float)

FAILURE_OUTCOMES = {"analysis_failed", "bounce_reverted"}
NEUTRAL_OUTCOMES = {"needs_human"}
SUCCESS_OUTCOMES = {"auto_actions_ok", "bounce_success"}
VALID_OUTCOMES = SUCCESS_OUTCOMES | NEUTRAL_OUTCOMES | FAILURE_OUTCOMES


class CorruptState(Exception):
    pass


def _fresh_state() -> dict:
    return {"runs": [], "breaker_open": False, "opened_reason": None, "opened_at": None}


def _normalize(state: dict) -> dict:
    """Validate shape + prune; raises CorruptState on anything unexpected."""
    if not isinstance(state, dict):
        raise CorruptState("state is not a JSON object")
    runs = state.get("runs", [])
    if not isinstance(runs, list):
        raise CorruptState("'runs' is not a list")
    now = time.time()
    kept = []
    for r in runs:
        if not isinstance(r, dict) or "outcome" not in r:
            raise CorruptState("malformed run entry")
        try:
            ts = float(r.get("ts", 0))
        except (TypeError, ValueError):
            raise CorruptState("run entry has a non-numeric ts")
        if now - ts <= WINDOW_SEC:
            kept.append(r)
    out = {
        "runs": kept,
        "breaker_open": bool(state.get("breaker_open", False)),
        "opened_reason": state.get("opened_reason"),
        "opened_at": state.get("opened_at"),
    }
    return out


def read_state() -> dict:
    """Current state. Missing file = fresh closed state (a new install). Present-but-
    unusable file = OPEN with reason corrupt_state (never silently reset to closed).
    The returned dict carries '_corrupt': <detail> in that case."""
    try:
        text = STATE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fresh_state()
    except OSError as e:
        return _corrupt_state(f"unreadable: {e}")
    try:
        return _normalize(json.loads(text))
    except (ValueError, CorruptState) as e:
        return _corrupt_state(str(e))


def _corrupt_state(detail: str) -> dict:
    st = _fresh_state()
    st["breaker_open"] = True
    st["opened_reason"] = f"corrupt_state: {detail}"
    st["opened_at"] = time.time()
    st["_corrupt"] = detail
    return st


def _quarantine_corrupt(state: dict) -> None:
    """Under the lock: preserve the bad file as .corrupt-<ts> before it is replaced."""
    if "_corrupt" not in state:
        return
    backup = Path(f"{STATE_FILE}.corrupt-{int(time.time())}")
    n = 0
    while backup.exists():
        n += 1
        backup = Path(f"{STATE_FILE}.corrupt-{int(time.time())}-{n}")
    try:
        shutil.move(str(STATE_FILE), str(backup))
        print(f"WARNING: corrupt breaker state moved to {backup}", file=sys.stderr)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"WARNING: could not preserve corrupt state file: {e}", file=sys.stderr)
    state.pop("_corrupt", None)
    # reason stays "corrupt_state: ..." and the breaker stays OPEN until `clear`


def save_state(state: dict) -> None:
    state = {k: v for k, v in state.items() if not k.startswith("_")}
    statefile.atomic_write_text(STATE_FILE, json.dumps(state), mode=0o600)


def failure_rate(state: dict) -> tuple:
    """Return (rate, failures, total) over the window; total counts failures+successes+neutral."""
    runs = state["runs"]
    total = len(runs)
    failures = sum(1 for r in runs if r.get("outcome") in FAILURE_OUTCOMES)
    return (failures / total if total else 0.0, failures, total)


def maybe_trip(state: dict) -> bool:
    """Trip the breaker if the error rate exceeds the threshold with enough samples.
    Returns True only when THIS call newly opened it."""
    if state.get("breaker_open"):
        return False
    rate, failures, total = failure_rate(state)
    if total >= MIN_RUNS and rate > MAX_ERROR_RATE:
        state["breaker_open"] = True
        state["opened_reason"] = (
            f"auto: failure rate {rate:.1%} ({failures}/{total}) exceeds "
            f"{MAX_ERROR_RATE:.0%} over rolling {WINDOW_SEC}s window"
        )
        state["opened_at"] = time.time()
        return True
    return False


def record(outcome: str, ticket_id: str = "") -> tuple:
    """Returns (state, newly_tripped, was_corrupt)."""
    if outcome not in VALID_OUTCOMES:
        print(f"ERROR: invalid outcome '{outcome}' (valid: {', '.join(sorted(VALID_OUTCOMES))})", file=sys.stderr)
        sys.exit(1)
    with statefile.locked(STATE_FILE):
        state = read_state()
        was_corrupt = "_corrupt" in state
        _quarantine_corrupt(state)
        state["runs"].append({"ts": time.time(), "outcome": outcome, "ticket_id": str(ticket_id)})
        tripped = False
        if outcome in FAILURE_OUTCOMES:
            tripped = maybe_trip(state)
        save_state(state)
    return state, tripped, was_corrupt


def _emit_trip(state: dict, ticket_id: str) -> None:
    reason = state.get("opened_reason")
    print(f"{TRIP_EVENT}: {reason}", file=sys.stderr)
    print(json.dumps({
        "event": TRIP_EVENT,
        "breaker_open": True,
        "tripped": True,
        "reason": reason,
        "opened_at": state.get("opened_at"),
        "trigger_ticket_id": str(ticket_id),
    }), flush=True)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0]

    try:
        _dispatch(cmd, args)
    except statefile.LockTimeout as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: breaker state I/O failed: {e}", file=sys.stderr)
        sys.exit(1)


def _dispatch(cmd: str, args: list) -> None:
    if cmd == "record":
        if len(args) < 2:
            print("ERROR: record requires an outcome", file=sys.stderr)
            sys.exit(1)
        ticket = args[2] if len(args) > 2 else ""
        state, tripped, was_corrupt = record(args[1], ticket)
        if tripped or was_corrupt:
            _emit_trip(state, ticket)
        rate, failures, total = failure_rate(state)
        print(json.dumps({
            "recorded": args[1],
            "ticket_id": ticket,
            "breaker_open": state["breaker_open"],
            "window_failures": failures,
            "window_total": total,
            "window_rate": round(rate, 4),
        }))

    elif cmd == "status":
        state = read_state()
        rate, failures, total = failure_rate(state)
        by_outcome = {}
        for r in state["runs"]:
            by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        out = {
            "breaker_open": state["breaker_open"],
            "opened_reason": state["opened_reason"],
            "opened_at": state["opened_at"],
            "window_sec": WINDOW_SEC,
            "window_total": total,
            "window_failures": failures,
            "window_rate": round(rate, 4),
            "by_outcome": by_outcome,
            "thresholds": {"min_runs": MIN_RUNS, "max_error_rate": MAX_ERROR_RATE},
        }
        if "_corrupt" in state:
            out["corrupt_state"] = state["_corrupt"]
        print(json.dumps(out, indent=2))

    elif cmd == "open":
        reason = " ".join(args[1:]) or "manual"
        with statefile.locked(STATE_FILE):
            state = read_state()
            _quarantine_corrupt(state)
            state["breaker_open"] = True
            state["opened_reason"] = f"manual: {reason}"
            state["opened_at"] = time.time()
            save_state(state)
        print(f"Breaker OPENED: {state['opened_reason']}")

    elif cmd == "clear":
        with statefile.locked(STATE_FILE):
            state = read_state()
            _quarantine_corrupt(state)
            state["breaker_open"] = False
            state["opened_reason"] = None
            state["opened_at"] = None
            save_state(state)
        print("Breaker CLEARED — bounces re-enabled")

    elif cmd == "assert-bounce":
        state = read_state()
        if state.get("breaker_open"):
            print(
                f"BLOCKED: circuit breaker open since {state.get('opened_at')} — {state.get('opened_reason')}",
                file=sys.stderr,
            )
            sys.exit(3)
        # Machine-readable allow token: sonar_bounce.py requires rc 0 AND this line.
        print(json.dumps({"bounce_allowed": True}))
        sys.exit(0)

    else:
        print(f"ERROR: unknown action '{cmd}'", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
