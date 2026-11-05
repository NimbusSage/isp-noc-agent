#!/usr/bin/env python3
"""path_probe.py - path-analyzer Steps B-H (reachability, traceroute, hop names, bandwidth series, direct links,
variance, multipath) in one call, through the MCP tools.

Every device command goes through `node mcp-server/dist/tool-cli.js run-diagnostics` (the MCP executor: allowlisted
read-only commands, credentials from the encrypted store, audit log, redaction, host policy). No password is read,
passed or printed here: `bandwidth-test` is called without user/password and the MCP server authenticates it from
the pool itself. Hop names fall back to LibreNMS (`lookup-device-by-exact-ip`) when a router will not answer.

The agent still resolves the start router (Step A) and the fiber testing address (Step D table), and keeps the
judgment and the comment (Step I). This script measures and flags; it decides nothing.

Usage:
  python3 scripts/path_probe.py --tower <router-ip> --fiber <testing-ip> [--target 8.8.8.8]
                                [--duration 10] [--direct-duration 5] [--max-hops 8] [--budget 540] [--no-btest]
  python3 scripts/path_probe.py --tower <router-ip> --network <Network/Fiber name>   # fiber address from the table

Prints one JSON object. Exit 0 when the path was measured (flags may still show problems), 3 when the start
router is unreachable, 2 on bad input, 1 when the MCP tool cannot run at all.
"""
import argparse
import ipaddress
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
TOOL_CLI = REPO / "mcp-server" / "dist" / "tool-cli.js"
FIBER_TABLE = REPO / "site-pack" / "topology" / "fiber-testing-ips.json"
IP_RE = r"\d{1,3}(?:\.\d{1,3}){3}"

VARIANCE_LIMIT = 0.40      # AGENTS.md Step 3.5: |D_k - (R_{k+1}-R_k)| / D_k > 40%
LOW_DIRECT_RATIO = 0.60    # D_k below 60% of the median direct rate
PATH_DROP_RATIO = 0.50     # bandwidth drop > 50% between consecutive full-path hops
CPU_HIGH = 80              # router cpu-load %
CPU_LIMITED = 90           # btest local/remote cpu at or above this: result may be generation-bound
LATENCY_MS = 100           # internal hop latency


def run_tool_cli(tool: str, args: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """One MCP tool call. Returns {"ok": bool, "data": parsed JSON or None, "text": str}."""
    if not TOOL_CLI.is_file():
        raise RuntimeError("mcp-server/dist/tool-cli.js is missing (build the MCP server)")
    p = subprocess.run(["node", str(TOOL_CLI), tool, "--stdin"], input=json.dumps(args), capture_output=True,
                       text=True, timeout=timeout, cwd=str(REPO))
    text = p.stdout.strip()
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    return {"ok": p.returncode == 0, "data": data, "text": text if data is None else ""}


def result_text(res: Dict[str, Any]) -> str:
    """The device output as plain text, whichever shape the MCP parser left it in."""
    data = res.get("data")
    if not isinstance(data, dict):
        return res.get("text") or ""
    r = data.get("result")
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        return "\n".join(" ".join(f"{k}={v}" for k, v in rec.items()) if isinstance(rec, dict) else str(rec) for rec in r)
    return json.dumps(r) if r is not None else ""


def warnings_of(res: Dict[str, Any]) -> List[str]:
    data = res.get("data")
    return [str(w) for w in (data.get("warnings") or [])] if isinstance(data, dict) else []


def _num(pattern: str, text: str) -> Optional[float]:
    found = re.findall(pattern, text)
    return float(found[-1]) if found else None


def _mbps(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    m = re.match(r"([\d.]+)\s*([kMG]?)bps", raw.strip())
    if not m:
        return None
    return round(float(m.group(1)) * {"": 1e-6, "k": 1e-3, "M": 1.0, "G": 1e3}[m.group(2)], 1)


def parse_btest(text: str) -> Dict[str, Any]:
    """Last values of a RouterOS bandwidth-test run (the stream repeats; the final block is the result)."""
    def last(key: str) -> Optional[str]:
        found = re.findall(rf"{key}\s*[:=]\s*(\S+)", text)
        return found[-1] if found else None
    status = last("status")
    out = {
        "status": status,
        "tx_mbps": _mbps(last("tx-10-second-average") or last("tx-total-average")),
        "rx_mbps": _mbps(last("rx-10-second-average") or last("rx-total-average")),
        "local_cpu": _num(r"local-cpu-load\s*[:=]\s*(\d+)%", text),
        "remote_cpu": _num(r"remote-cpu-load\s*[:=]\s*(\d+)%", text),
        "auth_failed": bool(re.search(r"authentication failed", text, re.I)),
    }
    rates = [v for v in (out["tx_mbps"], out["rx_mbps"]) if v is not None]
    out["effective_mbps"] = min(rates) if rates else None
    out["cpu_limited"] = any(c is not None and c >= CPU_LIMITED for c in (out["local_cpu"], out["remote_cpu"]))
    return out


def parse_traceroute(text: str) -> List[Dict[str, Any]]:
    hops = []
    for line in text.splitlines():
        m = re.match(rf"^\s*(\d+)\s+({IP_RE})\b(.*)$", line)
        if not m:
            continue
        rest = m.group(3)
        ms = re.search(r"([\d.]+)\s*ms", rest)
        loss = re.search(r"(\d+)%", rest)
        hops.append({"n": int(m.group(1)), "ip": m.group(2), "rtt_ms": float(ms.group(1)) if ms else None,
                     "loss_pct": int(loss.group(1)) if loss else None})
    return hops


def parse_default_gateways(text: str) -> List[str]:
    gws = set()
    for line in text.splitlines():
        if "0.0.0.0/0" not in line:
            continue
        m = re.search(rf"gateway\s*=\s*({IP_RE})", line) or re.search(rf"0\.0\.0\.0/0\s+({IP_RE})", line)
        if m:
            gws.add(m.group(1))
    return sorted(gws)


def is_private(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def fiber_from_table(network: str, table: Path = FIBER_TABLE) -> Dict[str, Any]:
    try:
        data = json.loads(table.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ip": None, "source": None, "note": "fiber testing table not found"}
    for key, val in data.items():
        if key.startswith("_") or not isinstance(val, dict) or key.lower() != network.strip().lower():
            continue
        internal = str(val.get("internal") or "").strip()
        if re.fullmatch(IP_RE, internal):
            return {"ip": internal, "source": "table:internal", "note": val.get("notes")}
        return {"ip": None, "source": "table", "note": f"no internal testing address ({internal or 'empty'}): skip the private btest"}
    return {"ip": None, "source": None, "note": f"network not in the fiber testing table: {network}"}


class Probe:
    def __init__(self, run: Callable[[str, Dict[str, Any], float], Dict[str, Any]], budget_s: float,
                 clock: Callable[[], float] = time.monotonic):
        self.run = run
        self.clock = clock
        self.deadline = clock() + budget_s
        self.errors: List[str] = []
        self.calls = 0

    def left(self) -> float:
        return self.deadline - self.clock()

    def diag(self, host: str, command: str, params: Optional[dict] = None, timeout: float = 45) -> Dict[str, Any]:
        if self.left() < 5:
            return {"ok": False, "data": None, "text": "budget exhausted"}
        self.calls += 1
        args = {"host": host, "deviceType": "mikrotik-ssh", "command": command}
        if params:
            args["params"] = params
        try:
            return self.run("run-diagnostics", args, min(timeout, self.left()))
        except subprocess.TimeoutExpired:
            return {"ok": False, "data": None, "text": "timed out"}

    def name_of(self, ip: str) -> Dict[str, Any]:
        r = self.diag(ip, "system-identity", timeout=30)
        m = re.search(r"name\s*[:=]\s*(.+)", result_text(r)) if r["ok"] else None
        if m:
            return {"name": m.group(1).strip().strip('"')[:60], "source": "identity"}
        try:
            self.calls += 1
            lr = self.run("lookup-device-by-exact-ip", {"ip": ip}, min(30, max(self.left(), 1)))
        except subprocess.TimeoutExpired:
            lr = {"ok": False}
        if lr.get("ok"):
            m = re.search(r'"sysName"\s*:\s*"([^"]+)"', json.dumps(lr.get("data") or lr.get("text") or ""))
            if m:
                return {"name": m.group(1)[:60], "source": "librenms"}
        return {"name": None, "source": None}

    def cpu_of(self, ip: str) -> Optional[float]:
        r = self.diag(ip, "system-resource", timeout=30)
        return _num(r"cpu-load\s*[:=]\s*(\d+)%", result_text(r)) if r["ok"] else None

    def btest(self, src: str, dst: str, duration: int) -> Dict[str, Any]:
        r = self.diag(src, "bandwidth-test", {"address": dst, "duration": duration, "protocol": "tcp", "direction": "both"},
                      timeout=duration + 40)
        out = parse_btest(result_text(r))
        out["from"], out["to"] = src, dst
        notes = [w for w in warnings_of(r) if "pool credential" in w]
        if notes:
            out["auth"] = notes[-1]
        if not r["ok"] or out["effective_mbps"] is None:
            out["error"] = (r.get("text") or "; ".join(warnings_of(r)) or out.get("status") or "no result")[:160]
        return out


def analyze(series: List[Dict[str, Any]], directs: List[Dict[str, Any]], hops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Step H flags from the measured numbers. series[k] = router k -> fiber, directs[k] = router k -> router k+1."""
    flags: List[Dict[str, Any]] = []
    rates = [s.get("effective_mbps") for s in series]
    for k in range(len(series) - 1):
        r_k, r_next = rates[k], rates[k + 1]
        if r_k is not None and r_next is not None and r_next > 0 and r_k < (1 - PATH_DROP_RATIO) * r_next:
            flags.append({"flag": "path_drop", "k": k, "detail": f"{r_k} vs {r_next} Mbps toward the fiber router"})
    d_vals = [d.get("effective_mbps") for d in directs if d.get("effective_mbps")]
    med = statistics.median(d_vals) if d_vals else None
    variance = []
    for k, d in enumerate(directs):
        dk = d.get("effective_mbps")
        if not dk:
            continue
        entry: Dict[str, Any] = {"k": k, "direct_mbps": dk, "cpu_limited": d.get("cpu_limited", False)}
        if k + 1 < len(rates) and rates[k] is not None and rates[k + 1] is not None:
            drop = rates[k + 1] - rates[k]
            ratio = abs(dk - drop) / dk
            entry.update({"drop_mbps": round(drop, 1), "ratio": round(ratio, 2)})
            if ratio > VARIANCE_LIMIT:
                flags.append({"flag": "unexplainable_variance", "k": k, "detail": f"|{dk} - ({drop:.1f})| / {dk} = {ratio:.0%}"})
        if med and dk < LOW_DIRECT_RATIO * med:
            flags.append({"flag": "low_direct", "k": k, "detail": f"{dk} Mbps < 60% of median {med:.1f}"})
        variance.append(entry)
    for h in hops:
        if h.get("cpu_load") is not None and h["cpu_load"] > CPU_HIGH:
            flags.append({"flag": "cpu_high", "k": h["k"], "detail": f"cpu-load {h['cpu_load']:.0f}%"})
        if h.get("rtt_ms") is not None and h["rtt_ms"] > LATENCY_MS:
            flags.append({"flag": "latency", "k": h["k"], "detail": f"{h['rtt_ms']} ms"})
    return {"flags": flags, "variance": variance, "median_direct_mbps": med}


def probe(args: argparse.Namespace, run=run_tool_cli, clock=time.monotonic) -> Dict[str, Any]:
    started = clock()
    p = Probe(run, args.budget, clock)
    out: Dict[str, Any] = {"ok": False, "tower": args.tower, "target": args.target}

    # Step B: reachability (and the start router's own cpu)
    res = p.diag(args.tower, "system-resource", timeout=30)
    if not res["ok"]:
        out.update({"stage": "reachability", "error": (res.get("text") or "; ".join(warnings_of(res)) or "no answer")[:200]})
        return out
    tower_cpu = _num(r"cpu-load\s*[:=]\s*(\d+)%", result_text(res))

    # Step C: traceroute, then names for the private hops (the routers this ISP runs)
    tr = p.diag(args.tower, "tool-traceroute", {"address": args.target, "count": 1}, timeout=60)
    trace = parse_traceroute(result_text(tr)) if tr["ok"] else []
    if not trace:
        p.errors.append("traceroute returned no hops" if tr["ok"] else f"traceroute failed: {(tr.get('text') or '')[:120]}")
    fiber = {"ip": args.fiber, "source": "argument", "note": None} if args.fiber else (
        fiber_from_table(args.network) if args.network else {"ip": None, "source": None, "note": "no --fiber or --network"})
    hop_ips: List[str] = []
    for h in trace:
        if not is_private(h["ip"]) or h["ip"] == args.tower:
            continue
        hop_ips.append(h["ip"])
        if h["ip"] == fiber["ip"] or len(hop_ips) >= args.max_hops:
            break
    tower_name = p.name_of(args.tower)
    hops = [{"k": 0, "ip": args.tower, **tower_name, "cpu_load": tower_cpu, "rtt_ms": None}]
    by_ip = {h["ip"]: h for h in trace}
    for i, ip in enumerate(hop_ips, start=1):
        hops.append({"k": i, "ip": ip, **p.name_of(ip), "cpu_load": p.cpu_of(ip), "rtt_ms": by_ip.get(ip, {}).get("rtt_ms")})

    # Multipath: distinct active default gateways on the start router (omit the line in the comment unless > 1)
    rt = p.diag(args.tower, "ip-route-active", timeout=30)
    gws = parse_default_gateways(result_text(rt)) if rt["ok"] else []
    out["multipath"] = {"default_gateways": gws, "load_balanced": len(gws) > 1}

    series: List[Dict[str, Any]] = []
    directs: List[Dict[str, Any]] = []
    if args.no_btest:
        out["bandwidth"] = "skipped (--no-btest)"
    elif not fiber["ip"]:
        out["bandwidth"] = f"skipped: {fiber['note']}"
    else:
        # Steps E-F: every router (start router first) to the fiber router, then Step G: each router to the next
        routers = [h for h in hops if h["ip"] != fiber["ip"]]
        for h in routers:
            series.append({"k": h["k"], **p.btest(h["ip"], fiber["ip"], args.duration)})
        for a, b in zip(hops, hops[1:]):
            d = p.btest(a["ip"], b["ip"], args.direct_duration)
            d["k"] = a["k"]
            directs.append(d)
        if all(s.get("auth_failed") for s in series if "error" in s) and series and all("error" in s for s in series):
            p.errors.append("no pool credential was accepted by the btest servers")
    analysis = analyze(series, directs, hops)
    out.update({
        "ok": True,
        "fiber": fiber,
        "hops": hops,
        "to_fiber": series,
        "direct": directs,
        "flags": analysis["flags"],
        "variance": analysis["variance"],
        "median_direct_mbps": analysis["median_direct_mbps"],
        "traceroute": trace,
        "errors": p.errors,
        "mcp_calls": p.calls,
        "elapsed_s": round(clock() - started, 1),
        "budget_left_s": round(p.left(), 1),
    })
    return out


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tower", required=True, help="start router management IP (Step A)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fiber", help="fiber router testing IP (Step D internal address)")
    g.add_argument("--network", help="Network/Fiber name to look up in site-pack/topology/fiber-testing-ips.json")
    ap.add_argument("--target", default="8.8.8.8", help="traceroute target (default 8.8.8.8)")
    ap.add_argument("--duration", type=int, default=int(os.environ.get("BTEST_DURATION") or 10), help="btest seconds to the fiber router")
    ap.add_argument("--direct-duration", type=int, default=5, help="btest seconds for direct router-to-router links")
    ap.add_argument("--max-hops", type=int, default=8)
    ap.add_argument("--budget", type=float, default=540.0, help="wall-clock budget in seconds (default 540)")
    ap.add_argument("--no-btest", action="store_true", help="path, names, cpu and multipath only; no load-generating tests")
    args = ap.parse_args(argv)
    for ip in (args.tower, args.fiber, args.target):
        if ip is not None and not re.fullmatch(IP_RE, ip):
            print(json.dumps({"ok": False, "error": f"not an IPv4 address: {ip[:40]}"}))
            return 2
    if not (1 <= args.duration <= 30 and 1 <= args.direct_duration <= 30 and 1 <= args.max_hops <= 12):
        print(json.dumps({"ok": False, "error": "duration 1-30 s, max-hops 1-12"}))
        return 2
    try:
        out = probe(args)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
