"""Tests for scripts/path_probe.py with a fake MCP tool runner (recorded RouterOS output shapes; no devices)."""
import argparse
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import path_probe  # noqa: E402

TOWER, H1, H2, FIBER = "10.0.1.2", "10.0.2.1", "10.0.3.1", "10.0.9.1"

TRACE = """ # ADDRESS                          LOSS SENT    LAST     AVG    BEST   WORST STD-DEV STATUS
 1 10.0.2.1                           0%    1   0.3ms     0.3     0.3     0.3       0
 2 10.0.3.1                           0%    1   4.4ms     4.4     4.4     4.4       0
 3 10.0.9.1                           0%    1   9.8ms     9.8     9.8     9.8       0
 4 198.51.100.42                      0%    1  16.5ms    16.5    16.5    16.5       0
"""


def btest_text(tx, rx, lcpu=40, rcpu=20):
    run = f"status: running\n tx-current: 50Mbps\n tx-10-second-average: 1Mbps\n rx-10-second-average: 1Mbps\n"
    return run + (f"status: done testing\n tx-10-second-average: {tx}Mbps\n rx-10-second-average: {rx}Mbps\n"
                  f" local-cpu-load: {lcpu}%\n remote-cpu-load: {rcpu}%\n")


class FakeMcp:
    """Answers run-diagnostics like the MCP CLI does: {"result": <raw text or as-value records>, "warnings": [...]}."""

    def __init__(self, rates, direct, unreachable=(), cpu=None):
        self.rates, self.direct, self.unreachable, self.cpu = rates, direct, set(unreachable), cpu or {}
        self.calls = []

    def __call__(self, tool, args, timeout):
        self.calls.append((tool, json.loads(json.dumps(args))))
        if tool == "lookup-device-by-exact-ip":
            return {"ok": True, "data": {"devices": [{"sysName": f"librenms-{args['ip']}"}]}, "text": ""}
        host, cmd, params = args["host"], args["command"], args.get("params", {})
        assert "password" not in params and "user" not in params, "the probe must never pass btest credentials"
        if host in self.unreachable:
            return {"ok": False, "data": None, "text": "Authentication failed after policy-defined attempts"}
        ok = lambda result, warnings=(): {"ok": True, "data": {"status": "success", "result": result, "warnings": list(warnings)}, "text": ""}
        if cmd == "system-resource":
            return ok(f"uptime: 1w\n cpu-load: {self.cpu.get(host, 5)}%\n board-name: RB4011")
        if cmd == "system-identity":
            return ok(f"name: router-{host.split('.')[2]}")
        if cmd == "tool-traceroute":
            return ok(TRACE)
        if cmd == "ip-route-active":
            # the MCP parser turns key=value lines into one mangled record each
            return ok([{"DAo dst-address": "0.0.0.0/0 gateway=10.0.2.1%ether1 immediate-gw 10.0.2.1 distance=110"},
                       {"DAc dst-address": "10.0.1.0/30 gateway=ether1"}])
        if cmd == "bandwidth-test":
            dst = params["address"]
            tx, rx = (self.rates[host] if dst == FIBER else self.direct[(host, dst)])
            return ok(btest_text(tx, rx), ["authenticated with pool credential #0"])
        raise AssertionError(cmd)


def args(**kw):
    base = dict(tower=TOWER, fiber=FIBER, network=None, target="8.8.8.8", duration=10, direct_duration=5,
                max_hops=8, budget=540.0, no_btest=False)
    base.update(kw)
    return argparse.Namespace(**base)


class Parsers(unittest.TestCase):
    def test_btest_uses_the_final_block_and_flags_cpu(self):
        b = path_probe.parse_btest(btest_text(64.1, 194.2, lcpu=94))
        self.assertEqual((b["tx_mbps"], b["rx_mbps"], b["effective_mbps"], b["status"]), (64.1, 194.2, 64.1, "done"))
        self.assertTrue(b["cpu_limited"])
        self.assertTrue(path_probe.parse_btest("status: authentication failed")["auth_failed"])
        self.assertEqual(path_probe.parse_btest("tx-10-second-average: 1.2Gbps rx-10-second-average: 900kbps")["effective_mbps"], 0.9)

    def test_traceroute_and_gateways(self):
        hops = path_probe.parse_traceroute(TRACE)
        self.assertEqual([h["ip"] for h in hops], [H1, H2, FIBER, "198.51.100.42"])
        self.assertEqual(hops[1]["rtt_ms"], 4.4)
        self.assertEqual(path_probe.parse_default_gateways("DAo dst-address=0.0.0.0/0 gateway=10.0.2.1%ether1\n"
                                                           "DAo 0.0.0.0/0 10.0.4.1%ether2 110"), ["10.0.2.1", "10.0.4.1"])

    def test_fiber_table_lookup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "f.json"
            t.write_text(json.dumps({"_source": "x", "Romeo": {"internal": "10.0.7.77", "public": "x", "notes": ""},
                                     "Kilo": {"internal": "xxx", "public": "x", "notes": ""}}))
            self.assertEqual(path_probe.fiber_from_table("romeo", t)["ip"], "10.0.7.77")
            self.assertIsNone(path_probe.fiber_from_table("Kilo", t)["ip"])
            self.assertIn("not in the fiber testing table", path_probe.fiber_from_table("nope", t)["note"])


class Probe(unittest.TestCase):
    def test_full_path_series_direct_links_and_flags(self):
        # tower->fiber 60, hop1->fiber 62, hop2->fiber 200: the hop1<->hop2 link is where throughput is lost
        fake = FakeMcp(rates={TOWER: (60, 70), H1: (62, 70), H2: (200, 210)},
                       direct={(TOWER, H1): (900, 880), (H1, H2): (65, 70), (H2, FIBER): (940, 930)})
        out = path_probe.probe(args(), run=fake)
        self.assertTrue(out["ok"])
        self.assertEqual([h["ip"] for h in out["hops"]], [TOWER, H1, H2, FIBER])
        self.assertEqual(out["hops"][1]["name"], "router-2")
        self.assertEqual([s["effective_mbps"] for s in out["to_fiber"]], [60, 62, 200])
        self.assertEqual([d["k"] for d in out["direct"]], [0, 1, 2])
        kinds = {(f["flag"], f["k"]) for f in out["flags"]}
        self.assertIn(("path_drop", 1), kinds)                      # 62 < 50% of 200
        self.assertIn(("low_direct", 1), kinds)                     # 65 vs median 880
        self.assertIn(("unexplainable_variance", 0), kinds)         # direct 880 vs drop 2
        self.assertEqual(out["multipath"], {"default_gateways": [H1], "load_balanced": False})
        self.assertEqual(out["to_fiber"][0]["auth"], "authenticated with pool credential #0")
        self.assertEqual(out["errors"], [])
        btests = [a for t, a in fake.calls if t == "run-diagnostics" and a["command"] == "bandwidth-test"]
        self.assertEqual(len(btests), 6)
        self.assertTrue(all(set(a["params"]) == {"address", "duration", "protocol", "direction"} for a in btests))

    def test_unreachable_start_router_stops_early(self):
        fake = FakeMcp(rates={}, direct={}, unreachable={TOWER})
        out = path_probe.probe(args(), run=fake)
        self.assertEqual((out["ok"], out["stage"]), (False, "reachability"))
        self.assertEqual(len(fake.calls), 1)

    def test_no_btest_mode_and_unnamed_hop_falls_back_to_librenms(self):
        fake = FakeMcp(rates={}, direct={}, unreachable={H2})
        out = path_probe.probe(args(no_btest=True), run=fake)
        self.assertTrue(out["ok"])
        self.assertEqual(out["bandwidth"], "skipped (--no-btest)")
        h2 = [h for h in out["hops"] if h["ip"] == H2][0]
        self.assertEqual((h2["name"], h2["source"]), (f"librenms-{H2}", "librenms"))
        self.assertFalse(any(a.get("command") == "bandwidth-test" for _, a in fake.calls))

    def test_budget_and_input_checks(self):
        ticks = iter([0, 0] + [1000] * 200)
        out = path_probe.probe(args(budget=10), run=FakeMcp(rates={}, direct={}), clock=lambda: next(ticks))
        self.assertFalse(out["ok"])                                   # nothing runs once the budget is gone
        self.assertEqual(path_probe.main(["--tower", "10.0.0.1; reboot", "--fiber", FIBER]), 2)


if __name__ == "__main__":
    unittest.main()
