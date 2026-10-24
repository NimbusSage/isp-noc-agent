#!/usr/bin/env python3
"""One-command rain-fade readiness for a Wave (WaveOS gen3) 60 GHz link.

Orchestrates the pieces the path-analyzer `rain-fade-readiness` mode uses, so
the whole assessment is reproducible as a single command instead of agent
memory:

    python3 scripts/rain_fade_readiness.py \\
        --wave-ap https://<ap-mgmt-ip> --wave-st https://<st-mgmt-ip> \\
        [--speedtest] [--speedtest-duration 10] [--freq 60] \\
        [--audit-config] [--mcp-tool-cli node mcp-server/dist/tool-cli.js]

Reuses, never reimplements:

* `scripts/wave_gui.mjs read` — live per-end dashboard truth (creds stay in the
  encrypted `ubnt-wave` credstore pool; this script never sees a password).
* `scripts/wave_gui.mjs speedtest` — the radio's built-in capacity test.
* `scripts/rain_fade_model.py` — ITU-R P.838-3 kill-rain-rate + tier.

What it computes (single JSON object on stdout):

* path length: haversine between the two radios' GPS fixes (each read's
  `localPeer.gps` is the far radio's fix — live-verified behavior).
* fade margin per end: day-average signal − empirical decode floor, where the
  decode floor is the lowest signal-bin histogram bar on the radio
  (`decodeFloorDbm`, in wave_gui read output). Worst end wins.
* kill rain rate + readiness tier via P.838-3 (`RF_TIER_RED_MM_H` /
  `RF_TIER_AMBER_MM_H` env overrides, same as the model CLI).
* with `--speedtest`: built-in speed test through the link (loads the radio;
  keep durations short when traffic is live).
* with `--audit-config`: `ucfg show` on both ends through the NOC MCP tool CLI
  (network.carrierd + wireless.radios.backup); degrades to a warning when the
  MCP CLI is unavailable.

Read-only: this script never mutates radios. Missing pieces become
`status: "partial"` plus `warnings[]` entries, never fabricated numbers.

Exit codes: 0 ok (or partial with warnings); 1 usage; 2 wave reads failed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import envfile  # noqa: E402
import rain_fade_model as rfm  # noqa: E402

WAVE_GUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wave_gui.mjs")
MAX_SPEEDTEST_S = 60   # per direction; the built-in test saturates the radios


def decode_floor_dbm(sig: dict) -> float | None:
    """Lowest nonzero signal-histogram bin in dBm (bin width = span/(bins-1)).

    Mirrors the wave_gui.mjs computation so fixtures and any legacy middleware
    can reproduce the field; the orchestrator itself trusts the value already in
    the read payload and never re-derives it here.
    """
    if not isinstance(sig, dict) or not isinstance(sig.get("histogram"), list):
        return None
    if not isinstance(sig.get("minSignal"), (int, float)):
        return None
    bins = sig["histogram"]
    first = next((i for i, v in enumerate(bins) if isinstance(v, (int, float)) and v > 0), None)
    if first is None:
        return None
    span = (sig["maxSignal"] - sig["minSignal"]
            if isinstance(sig.get("maxSignal"), (int, float)) else len(bins) - 1)
    width = (span / (len(bins) - 1)) if len(bins) > 1 else 0.0
    return round(sig["minSignal"] + width * first, 1)


def haversine_km(a: dict, b: dict) -> float:
    R = 6371.0
    la1, lo1 = math.radians(a["lat"]), math.radians(a["lon"])
    la2, lo2 = math.radians(b["lat"]), math.radians(b["lon"])
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(h)), 2)


def tier_overrides() -> tuple[float, float]:
    try:
        red = float(envfile.get("RF_TIER_RED_MM_H", str(rfm.RED_BELOW_MM_H)))
        amber = float(envfile.get("RF_TIER_AMBER_MM_H", str(rfm.AMBER_BELOW_MM_H)))
    except ValueError as exc:
        raise SystemExit(f"RF_TIER_RED_MM_H / RF_TIER_AMBER_MM_H must be numbers: {exc}")
    return red, amber


def run_wave_gui(*argv: str) -> dict:
    proc = subprocess.run(
        ["node", WAVE_GUI, *argv],
        capture_output=True, text=True, timeout=180,
        env={k: v for k, v in os.environ.items() if not k.startswith("WAVE_GUI")},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"wave_gui.mjs {argv[0]} exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"wave_gui.mjs {argv[0]} printed non-JSON: {str(exc)[:200]}") from exc


def read_end(url: str, warnings: list) -> dict | None:
    try:
        out = run_wave_gui("read", "--url", url)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"read failed {url}: {exc}")
        return None
    # Deliberate: the script prints only derived facts; raw read payloads stay
    # available from wave_gui directly, not re-echoed here.
    return out


def far_fix(read_out: dict) -> dict | None:
    """Far radio's GPS fix = the localPeer entry of this end's read (live-verified)."""
    try:
        g = read_out["radios"]["main"]["localPeer"]["gps"]
        if g and g.get("lat") is not None and g.get("lon") is not None:
            return {"lat": g["lat"], "lon": g["lon"]}
    except (KeyError, TypeError):
        pass
    return None


def end_margin(read_out: dict) -> tuple[float | None, float | None, float | None]:
    """(day_avg_signal, decode_floor, margin) from the empirical decode floor."""
    try:
        lp = read_out["radios"]["main"]["localPeer"]
        day, floor = lp.get("signalDayDbm"), lp.get("decodeFloorDbm")
    except (KeyError, TypeError):
        return None, None, None
    margin = None if (day is None or floor is None) else round(day - floor, 1)
    return day, floor, margin


def mcp_ucfg(host_ip: str, tool_cli: list, warnings: list) -> dict | None:
    cmd = [*tool_cli, "run-diagnostics",
           json.dumps({"deviceType": "ubnt-wave", "host": host_ip, "command": "ucfg-show"})]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"ucfg audit {host_ip}: MCP tool CLI failed: {exc}")
        return None
    if proc.returncode != 0:
        warnings.append(f"ucfg audit {host_ip}: tool CLI exit {proc.returncode}")
        return None
    try:
        resp = json.loads(proc.stdout)
    except ValueError:
        warnings.append(f"ucfg audit {host_ip}: non-JSON from tool CLI")
        return None
    result = resp.get("result") if isinstance(resp, dict) else None
    try:
        if isinstance(result, str):
            result = json.loads(result)
        inner = result if isinstance(result, dict) else json.loads(str(result))[0]
        return {
            "carrierd": inner.get("network", {}).get("carrierd"),
            "backup": inner.get("wireless", {}).get("radios", {}).get("backup"),
        }
    except (ValueError, TypeError, AttributeError) as exc:
        warnings.append(f"ucfg audit {host_ip}: unparseable result ({exc})")
        return None


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url


def run(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wave-ap", required=True, help="Gen3 web UI URL of the AP side")
    ap.add_argument("--wave-st", required=True, help="Gen3 web UI URL of the ST side")
    ap.add_argument("--freq", type=float, default=60.0)
    ap.add_argument("--elevation", type=float, default=0.0)
    ap.add_argument("--tau", type=float, default=45.0)
    ap.add_argument("--speedtest", action="store_true")
    ap.add_argument("--speedtest-duration", type=int, default=10,
                    help="seconds per direction (default 10, at most %d; the test loads the radios, so keep it short "
                         "and run it only when operations agree)" % MAX_SPEEDTEST_S)
    ap.add_argument("--speedtest-from", choices=["ap", "st"], default="ap")
    ap.add_argument("--audit-config", action="store_true",
                    help="add carrierd/backup config truth via the NOC MCP tool CLI")
    ap.add_argument("--mcp-tool-cli", nargs="+",
                    default=["node", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "mcp-server", "dist", "tool-cli.js")])
    args = ap.parse_args(argv)
    for flag, value in (("--wave-ap", args.wave_ap), ("--wave-st", args.wave_st)):
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            ap.error(f"{flag} must look like https://<radio-ip> (no credentials in the URL)")

    warnings: list = []
    out: dict = {"mode": "rain-fade-readiness", "ap": args.wave_ap, "st": args.wave_st,
                 "warnings": warnings, "timestamp": utc_now_iso()}

    report = read_end(args.wave_ap, warnings), read_end(args.wave_st, warnings)
    ap_read, st_read = report
    if not ap_read and not st_read:
        print(json.dumps({"error": "both wave reads failed", "warnings": warnings}))
        return 2

    ends = [("ap", ap_read), ("st", st_read)]
    margins, out["perEnd"] = {}, {}
    for name, r in ends:
        if r is None:
            continue
        day, floor, margin = end_margin(r)
        margins[name] = margin
        dev = r.get("device") or {}
        out["perEnd"][name] = {"model": dev.get("model"), "firmware": dev.get("firmware"),
                               "signalDayDbm": day, "decodeFloorDbm": floor, "marginDb": margin}

    f_ap, f_st = far_fix(ap_read) if ap_read else None, far_fix(st_read) if st_read else None
    out["distanceKm"] = haversine_km(f_ap, f_st) if f_ap and f_st else None
    if f_ap is None or f_st is None:
        warnings.append("missing GPS fix on at least one end — distance null")

    best_margin = min((m for m in margins.values() if m is not None), default=None)
    out["marginDbWorstEnd"] = best_margin
    if best_margin is None:
        warnings.append("no decodable margin (decode floor missing on both ends)")
    red, amber = tier_overrides()
    if best_margin is not None and out["distanceKm"]:
        try:
            kill = rfm.rain_rate_for_fade(best_margin, out["distanceKm"], args.freq,
                                          args.elevation, args.tau, "p838")
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            warnings.append(f"rain model failed: {exc}")
            kill = None
    else:
        kill = None
    out["killRainRateMmH"] = kill
    out["tier"] = rfm.tier_for(kill, red, amber)

    if args.speedtest:
        src = {"ap": args.wave_ap, "st": args.wave_st}[args.speedtest_from]
        try:
            out["speedtest"] = run_wave_gui("speedtest", "--url", src,
                                            "--duration", str(max(1, min(MAX_SPEEDTEST_S, args.speedtest_duration))))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"speedtest failed: {exc}")

    if args.audit_config:
        out["configAudit"] = {
            name: mcp_ucfg(host_of(url), args.mcp_tool_cli, warnings)
            for name, url in (("ap", args.wave_ap), ("st", args.wave_st))
        }

    out["status"] = "ok" if not out["warnings"] else "partial"
    print(json.dumps(out))
    return 0


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    try:
        sys.exit(run(sys.argv[1:]))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
