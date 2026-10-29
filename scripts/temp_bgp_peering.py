#!/usr/bin/env python3
"""
Temp BGP Peering — Emergency Fiber Failover
Usage: python3 scripts/temp_bgp_peering.py --network <fiber> [--dry-run] [--subnet <a.b.c.d/len>]
                                           [--remote-as <REMOTE_AS>]
Implements SKILL.md Steps A-H. See .opencode/skills/temp-bgp-peering/SKILL.md

Org inputs (RR IPs, RR passwords, peer prefixes, oxidized URL, fiber loopback
hints) come from the UNTRACKED site-pack/topology/temp-bgp-config.json —
schema/fields guide in site-pack/example/temp-bgp-config.example.json. The
script fails closed (exit 2) when the config is missing or carries the
tracked example's placeholder values, so a fresh clone never half-runs.
An `rr_credentials` entry may name an environment variable instead of holding
the password: {"ip": "...", "password_env": "TEMP_BGP_RR1_PASSWORD"}.

The iBGP AS number is org-specific too: pass --remote-as <REMOTE_AS> or set
`bgp.remote_as` in the UNTRACKED site-pack/profile.json. A live (non
--dry-run) run exits with a clear message when neither is present.
Optional config keys: `default_network` (used when --network is omitted) and
`public_hop_prefixes` (e.g. ["198.51.100."] — public traceroute hops to keep
in loop detection besides the 10.x.x.x hops).

Security model (hardened):
  * Every ssh is an argv list through scripts/ssh_common.py — no shell, so a
    hostname, subnet or router output can never become shell syntax. --network
    and --subnet are validated against strict patterns before first use; every
    address interpolated into a RouterOS command is parsed as an IPv4 address.
  * Passwords never appear in argv: `sshpass -e` with SSHPASS in the child's env.
  * Tower/router (mikrotik-ssh) credentials come from NOC_MIKROTIK_SSH_PASSWORD
    (+ optional NOC_MIKROTIK_SSH_USER, default admin) or the encrypted MCP store
    (credentials.enc + NOC_MCP_PASSPHRASE). The plaintext
    mcp-server/config/credentials.json is NOT read.
  * Host keys: SSH_HOSTKEY_CHECKING (default accept-new), dedicated known_hosts
    file — see scripts/ssh_common.py.
"""
import argparse
import ipaddress
import json
import os
import pathlib
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

# Harness-proofing: anchor repo-relative files to this script's location so the
# script runs from any cwd / agent harness (previously cwd-dependent).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TOPOLOGY = _REPO_ROOT / "site-pack" / "topology"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import credstore  # noqa: E402
import noc_config  # noqa: E402
import ssh_common  # noqa: E402
import statefile  # noqa: E402

PLACEHOLDER_MARKERS = ("<rr-password-1>", "<rr-password-2>", "example.invalid")

_NETWORK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SUBNET_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")

BGP_CFG = {}
RR_MAP = {}
FIBER_IPS = {}
RR_PW = {}
RR_LOOPBACK = {}
OXIDIZED = ""
_CREDS = None


def _load_json_or_die(path, what, overlay=False):
    """JSON from `path`; with overlay=True the machine-local `.local` file (passwords) is merged over it."""
    try:
        if overlay:
            return noc_config.load_json_overlay(pathlib.Path(path))
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit(f"temp_bgp_peering: missing {path} — cp "
                 f"site-pack/example/temp-bgp-config.example.json "
                 f"site-pack/topology/temp-bgp-config.json and fill in your "
                 f"org's {what} (this repo never ships org-specific values)")
    except ValueError as e:        # json.JSONDecodeError is a ValueError
        sys.exit(f"temp_bgp_peering: invalid JSON in {path}: {e}")


def init_config():
    """Load the org config (fail closed). Called from main(), not at import."""
    global BGP_CFG, RR_MAP, FIBER_IPS, RR_PW, RR_LOOPBACK, OXIDIZED
    BGP_CFG = _load_json_or_die(_TOPOLOGY / "temp-bgp-config.json", "temp-BGP inputs", overlay=True)
    # Fail closed on the tracked example (it must never run against real routers).
    blob = json.dumps(BGP_CFG)
    if any(m in blob for m in PLACEHOLDER_MARKERS):
        sys.exit("temp_bgp_peering: site-pack/topology/temp-bgp-config.json still "
                 "contains the tracked example's placeholders — fill in real values.")
    RR_MAP = _load_json_or_die(_TOPOLOGY / "bgp-rr-map.json", "BGP RR map")
    FIBER_IPS = _load_json_or_die(_TOPOLOGY / "fiber-testing-ips.json", "fiber testing IPs")
    RR_PW, RR_LOOPBACK = {}, {}
    for c in BGP_CFG.get("rr_credentials", []):
        ip = _ipv4(c.get("ip"), "rr_credentials.ip")
        pw = c.get("password") or os.environ.get(c.get("password_env") or "", "")
        if not pw:
            sys.exit(f"temp_bgp_peering: no password for RR {ip}: set `password` or `password_env` "
                     f"(an environment variable that is currently set) in rr_credentials")
        RR_PW[ip] = pw
        RR_LOOPBACK[ip] = c.get("fiber_loopback", "")
    OXIDIZED = (BGP_CFG.get("oxidized_base_url") or "").rstrip("/")
    if OXIDIZED and not re.match(r"^https?://[^/\s]+", OXIDIZED):
        sys.exit("temp_bgp_peering: oxidized_base_url must be an http(s) URL")


def _ipv4(value, what="address"):
    """Return value as a canonical IPv4 string or exit. Everything that reaches a
    RouterOS command line or a URL goes through here."""
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except ValueError:
        sys.exit(f"temp_bgp_peering: {what} {value!r} is not a valid IPv4 address")


def validate_network(value):
    v = (value or "").strip().lower()
    if not _NETWORK_RE.match(v):
        sys.exit(f"temp_bgp_peering: --network {value!r} must match [a-z0-9][a-z0-9_-]{{0,39}}")
    return v


def validate_subnet(value):
    v = (value or "").strip()
    if not _SUBNET_RE.match(v):
        sys.exit(f"temp_bgp_peering: --subnet {value!r} must look like 198.51.100.0/24")
    try:
        net = ipaddress.ip_network(v, strict=False)
    except ValueError:
        sys.exit(f"temp_bgp_peering: --subnet {value!r} is not a valid IPv4 network")
    return v if net.version == 4 else sys.exit("temp_bgp_peering: --subnet must be IPv4")


def _profile():
    """site-pack/profile.json (untracked, org-specific). Missing/invalid -> {}."""
    return noc_config.load_profile()


def resolve_remote_as(cli_value):
    """iBGP AS: --remote-as wins, else profile.json bgp.remote_as. None if absent."""
    raw = cli_value if cli_value not in (None, "") else (_profile().get("bgp") or {}).get("remote_as")
    if raw in (None, ""):
        return None
    try:
        asn = int(str(raw).strip())
    except ValueError:
        sys.exit(f"temp_bgp_peering: remote AS {raw!r} is not an integer")
    if not 1 <= asn <= 4294967295:
        sys.exit(f"temp_bgp_peering: remote AS {asn} is out of range")
    return asn


def tower_creds():
    """[(user, password)] for mikrotik-ssh, priority order: env pair or encrypted store."""
    global _CREDS
    if _CREDS is None:
        try:
            _CREDS = [(c["username"], c["password"]) for c in credstore.pool("mikrotik-ssh")]
        except credstore.CredentialError as e:
            sys.exit(f"temp_bgp_peering: {e}")
    return _CREDS


def _result(returncode, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout or "", stderr=stderr or "")


def ssh(creds, host, cmd, timeout=15):
    """Run one RouterOS command, trying (user, password) pairs in order while the
    router answers 'Permission denied'. Returns an object with returncode/stdout/stderr."""
    r = None
    for user, pw in creds:
        try:
            cp, err = ssh_common.run_ssh(user, host, cmd, pw, timeout=timeout, connect_timeout=5)
        except ssh_common.SshConfigError as e:
            sys.exit(f"temp_bgp_peering: {e}")
        r = _result(255, "", err) if cp is None else _result(cp.returncode, cp.stdout, cp.stderr)
        if "Permission denied" not in (r.stdout + r.stderr):
            return r
        if r.returncode == 0:
            return r
    return r


def fetch_oxidized_peers(rr_ip):
    if not OXIDIZED:
        print("temp_bgp_peering: oxidized_base_url not set in config — skipping Oxidized fetch", file=sys.stderr)
        return []
    try:
        txt = urllib.request.urlopen(f"{OXIDIZED}/node/fetch/{_ipv4(rr_ip, 'RR address')}", timeout=10).read().decode()
        peers = []
        for line in txt.splitlines():
            if "remote.address" in line and line.strip().startswith("add"):
                m = re.search(r'remote\.address=([0-9.]+)', line)
                if m and _valid_ipv4(m.group(1)):
                    peers.append(m.group(1))
        return peers
    except Exception as e:
        print(f"Oxidized fetch failed {e}, fallback to live RR", file=sys.stderr)
        return []


def _valid_ipv4(value):
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def fetch_live_rr_peers(rr_ip, rr_pw):
    r = ssh([("admin", rr_pw)], rr_ip, '/routing/bgp/connection print where disabled=no')
    if not r:
        return []
    return [p for p in re.findall(r'remote\.address=([0-9.]+)', r.stdout) if _valid_ipv4(p)]


def _hop_regex():
    """10.x.x.x hops plus any org public prefixes from the config
    (`public_hop_prefixes`, e.g. ["198.51.100."])."""
    parts = [r'\b10\.\d+\.\d+\.\d+\b']
    for pfx in BGP_CFG.get("public_hop_prefixes", []):
        octets = [o for o in str(pfx).strip(".").split(".") if o]
        if not octets or len(octets) > 3 or not all(o.isdigit() for o in octets):
            continue
        parts.append(r'\b' + r'\.'.join(octets) + r''.join(r'\.\d+' for _ in range(4 - len(octets))) + r'\b')
    return "|".join(parts)


def traceroute(host, dst, creds=None):
    creds = creds or tower_creds()
    r = ssh(creds, host, f'/tool traceroute address={_ipv4(dst, "traceroute target")} count=1 use-dns=no', timeout=20)
    if not r:
        return []
    # ordered as appears
    return [ip for ip in re.findall(_hop_regex(), r.stdout) if _valid_ipv4(ip)]


def detect_loop(ips):
    if len(ips) < 4:
        return None
    tail = ips[-6:]
    # A-B-A-B pattern
    if len(tail) >= 4 and tail[-1] == tail[-3] and tail[-2] == tail[-4]:
        return tail[-3]
    if len(tail) >= 5 and tail[-1] == tail[-3] == tail[-5] and tail[-2] == tail[-4]:
        return tail[-3]
    return None


def classify_fiber(last_ip):
    """Pick the RR that reaches `last_ip`: known fiber-loopback hint first,
    then live ping round (lowest config-order RR that answers)."""
    default_rr = BGP_CFG.get("default_rr") or (BGP_CFG.get("rr_credentials") or [{}])[0].get("ip")
    for rr_ip, lo in RR_LOOPBACK.items():
        if lo and lo in last_ip:
            return rr_ip, RR_PW[rr_ip]
    # live ping probe: prefer default RR first
    order = [default_rr] + [ip for ip in RR_PW if ip != default_rr] if default_rr else list(RR_PW)
    for rr in order:
        if rr not in RR_PW:
            continue
        r = ssh([("admin", RR_PW[rr])], rr, f'/ping address={_ipv4(last_ip, "loopback")} count=2', timeout=10)
        if r and "packet-loss=0%" in r.stdout:
            return rr, RR_PW[rr]
    return default_rr, RR_PW.get(default_rr, "")


def get_instance(host):
    r = ssh(tower_creds(), host, '/routing/bgp/connection print detail where name~"TEMP"', timeout=10)
    if r:
        m = re.search(r'instance=([A-Za-z0-9_.-]+)', r.stdout)
        if m:
            return m.group(1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default=None, help="fiber network key from the temp-BGP config (default: config `default_network`)")
    ap.add_argument("--remote-as", dest="remote_as", default=None, help="iBGP AS number <REMOTE_AS> (default: site-pack/profile.json bgp.remote_as)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--subnet", help="specific advertised subnet to fix, e.g. 198.51.100.0/24")
    args = ap.parse_args()

    # Validate operator input BEFORE anything is loaded or any router is touched.
    subnet_arg = validate_subnet(args.subnet) if args.subnet else None
    network_arg = validate_network(args.network) if args.network else None
    init_config()

    network = network_arg
    if not network and BGP_CFG.get("default_network"):
        network = validate_network(BGP_CFG["default_network"])
    if not network:
        sys.exit("temp_bgp_peering: no network given — pass --network <fiber> "
                 "or set `default_network` in site-pack/topology/temp-bgp-config.json")
    remote_as = resolve_remote_as(args.remote_as)
    if remote_as is None and not args.dry_run:
        sys.exit("temp_bgp_peering: remote AS not set — pass --remote-as <REMOTE_AS> "
                 "or set bgp.remote_as in site-pack/profile.json (refusing to add peers)")
    nets = BGP_CFG.get("networks", {})
    if network in nets and nets[network].get("rr_ips"):
        rr_ip = _ipv4(nets[network]["rr_ips"][0], "networks.rr_ips")
    else:
        # fall back to the site-pack RR map (per-network verified RR)
        for k, v in RR_MAP.items():
            if isinstance(v, dict) and v.get("network", "").lower() == network:
                rr_ip = _ipv4(v["ip"], "bgp-rr-map ip")
                break
        else:
            sys.exit(f"temp_bgp_peering: unknown network {network!r} — add it to "
                     f"site-pack/topology/temp-bgp-config.json 'networks' or bgp-rr-map.json")

    creds = tower_creds()   # fail early, with a clear message, if no credentials are provided
    print(f"Network {network} RR {rr_ip}")
    peers = fetch_oxidized_peers(rr_ip)
    if not peers:
        peers = []
        for rr in BGP_CFG.get("default_rrs", []):
            pw = RR_PW.get(rr)
            if not pw:
                print(f"temp_bgp_peering: no rr_credentials entry for default RR {rr} — skipped", file=sys.stderr)
                continue
            peers += fetch_live_rr_peers(rr, pw)
    # filter to the network's backhaul prefix (config-driven)
    filter_prefix = BGP_CFG.get("peer_prefixes", {}).get(network)
    if filter_prefix:
        peers = [p for p in peers if p.startswith(filter_prefix)]
    print(f"Discovered {len(peers)} loopbacks")

    # For each subnet, loop detection
    # If --subnet given, only that subnet
    subnets = []
    if subnet_arg:
        subnets = [subnet_arg]
    else:
        # enumerate bgp-networks from each peer's firewall list (sample)
        for ip in peers[:5]:
            r = ssh(creds, ip, '/ip firewall address-list print where list=bgp-networks', timeout=10)
            if r:
                for line in r.stdout.splitlines():
                    m = re.search(r'(\d+\.\d+\.\d+\.\d+/\d+)', line)
                    if m and m.group(1) not in subnets:
                        try:
                            validate_subnet(m.group(1))
                        except SystemExit:
                            continue
                        subnets.append(m.group(1))
        if not subnets:
            subnets = [s for s in (BGP_CFG.get("example_subnets", {}).get(network) or []) if _SUBNET_RE.match(str(s))]

    test_rr = BGP_CFG.get("test_rr") or BGP_CFG.get("default_rr")
    if test_rr not in RR_PW:
        sys.exit(f"temp_bgp_peering: test_rr/default_rr {test_rr} has no rr_credentials password entry")
    log = {"network": network, "started": datetime.now(timezone.utc).isoformat(), "peers": [], "loops": []}
    for subnet in subnets:
        target = subnet.split("/")[0]
        r = ssh([("admin", RR_PW[test_rr])], test_rr, f'/ping address={_ipv4(target, "subnet")} count=2', timeout=10)
        if r and "packet-loss=0%" in r.stdout:
            continue
        ips = traceroute(test_rr, target)
        loop_ip = detect_loop(ips)
        if loop_ip:
            print(f"Loop for {subnet} last_ip {loop_ip}")
            # resolve loop_ip to loopback
            r2 = ssh(creds, _ipv4(loop_ip, "loop hop"), '/ip address print where interface=loopback', timeout=10)
            m = re.search(r'(\d+\.\d+\.\d+\.\d+)/32', r2.stdout if r2 else "")
            loopback = _ipv4(m.group(1) if m else loop_ip, "loopback")
            # classify
            rr, pw = classify_fiber(loopback)
            rr = _ipv4(rr, "route reflector")
            print(f"  classify {loopback} -> {rr}")
            if args.dry_run:
                log["loops"].append({"subnet": subnet, "loop_ip": loop_ip, "loopback": loopback, "rr": rr})
                continue
            # add both sides
            safe = re.sub(r'[^a-zA-Z0-9 ]', '', loopback)[:30]
            ssh([("admin", pw)], rr, f'/routing/bgp/connection add name="TEMP {safe}" remote.address={loopback} remote.as={remote_as} local.address={rr} local.role=ibgp-rr routing-table=main templates=default multihop=yes output.network=bgp-networks', timeout=15)
            inst = get_instance(loopback)
            inst_arg = f"instance={inst} " if inst else ""
            ssh(creds, loopback, f'/routing/bgp/connection add {inst_arg}name="TEMP failover-rr" remote.address={rr} remote.as={remote_as} local.address={loopback} local.role=ibgp routing-table=main templates=default multihop=yes output.network=bgp-networks', timeout=15)
            # verify
            time.sleep(3)
            vr = ssh([("admin", pw)], rr, f'/routing/bgp/session print where remote.address~"{loopback}"', timeout=10)
            vt = ssh(creds, loopback, f'/routing/bgp/session print where remote.address~"{rr}"', timeout=10)
            log["peers"].append({"subnet": subnet, "loopback": loopback, "rr": rr,
                                 "rr_est": "E" in (vr.stdout if vr else ""),
                                 "tower_est": "E" in (vt.stdout if vt else "")})
        else:
            print(f"No loop for {subnet} ips {ips[-5:]}")

    out = noc_config.state_dir() / f"temp_bgp_peering_{network}_{int(time.time())}.json"
    statefile.atomic_write_text(out, json.dumps(log, indent=2))
    print(json.dumps(log, indent=2))


if __name__ == "__main__":
    main()
