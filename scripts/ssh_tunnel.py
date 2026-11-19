#!/usr/bin/env python3
"""
ssh_tunnel.py - one local port forward through a router, credentials from the store.

The wired path for a factory-reset radio: the router can reach the device's factory
address, the agent cannot. With `/ip ssh forwarding-enabled=yes` on the router
(`routeros-temp-access` op `temp-ssh`; we leave it on, it is the house default on
most routers) `ssh -L` makes the device's web UI or SSH reachable on 127.0.0.1 with
no NAT rule, no forward-chain accept and no interface change.

* The password reaches ssh through `sshpass -e`: SSHPASS lives in the child's
  environment, never in argv and never printed by this script.
* Credentials come from the encrypted MCP store (or NOC_<TYPE>_USER/_PASSWORD when
  the operator exported them). Every credential in the pool is tried in order; the
  first that holds a working forward wins.
* Host keys follow `SSH_HOSTKEY_CHECKING` (default accept-new), same file as the
  other ops scripts (`scripts/ssh_opts.sh`).

Usage:
    python3 scripts/ssh_tunnel.py --host 10.0.0.1 --local-port 18444 \\
        --remote 192.168.1.20:443

    --device-type mikrotik-ssh   credential pool (default mikrotik-ssh)
    --user admin                 pin the login instead of trying the pool's users
    --wait 20                    seconds to wait for the forward to come up
    --connect-timeout 10         per-attempt ssh timeout

Runs in the foreground and holds the tunnel until SIGTERM/SIGINT. Start it as a
background job when the caller needs to keep working in another shell.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import credstore  # noqa: E402
import ssh_common  # noqa: E402

_REMOTE_RE = re.compile(r"^[A-Za-z0-9.\[\]:-]{3,255}$")


def build_forward_argv(user: str, host: str, local_port: int, remote: str, connect_timeout: int) -> List[str]:
    """argv for `sshpass -e ssh -N -L ...`. No command, no shell, no password in argv."""
    ssh_common.check_target(user, host)
    opts = ssh_common.ssh_options(
        connect_timeout=connect_timeout,
        extra=["-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{local_port}:{remote}"],
    )
    return ["sshpass", "-e", "ssh"] + opts + [f"{user}@{host}"]


def port_is_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_open(port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if port_is_open(port):
            return True
        time.sleep(0.3)
    return port_is_open(port)


def credentials(device_type: str, user: Optional[str]) -> List[dict]:
    creds = credstore.env_override(device_type)
    if not creds:
        creds = credstore.from_store(device_type, user)
    elif user:
        creds = [c for c in creds if c.get("username") == user]
    return [c for c in creds if c.get("username") and c.get("password")]


def try_credential(cred: dict, host: str, local_port: int, remote: str, connect_timeout: int,
                   wait_seconds: float) -> Tuple[Optional[subprocess.Popen], str]:
    """Start one forward. Returns (process, '') when it is up, else (None, error text)."""
    argv = build_forward_argv(cred["username"], host, local_port, remote, connect_timeout)
    env = dict(os.environ, SSHPASS=cred["password"])
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if wait_open(local_port, time.monotonic() + max(wait_seconds, 1.0)) and proc.poll() is None:
        return proc, ""
    proc.terminate()
    try:
        _, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return None, (err or "").strip().splitlines()[-1] if (err or "").strip() else f"forward did not come up (exit {proc.returncode})"


def main() -> int:
    ap = argparse.ArgumentParser(description="local port forward through a router, credentials from the store")
    ap.add_argument("--host", required=True, help="router address the store has a binding for")
    ap.add_argument("--device-type", default="mikrotik-ssh")
    ap.add_argument("--user", default=None, help="pin the login (default: try each pool credential)")
    ap.add_argument("--local-port", type=int, required=True)
    ap.add_argument("--remote", required=True, help="host:port as seen from the router, e.g. 192.168.1.20:443")
    ap.add_argument("--wait", type=float, default=20.0)
    ap.add_argument("--connect-timeout", type=int, default=10)
    args = ap.parse_args()

    if not 1024 <= args.local_port <= 65535:
        print("ssh_tunnel: --local-port must be 1024-65535", file=sys.stderr)
        return 2
    if not _REMOTE_RE.match(args.remote) or ":" not in args.remote:
        print("ssh_tunnel: --remote must be host:port", file=sys.stderr)
        return 2
    if port_is_open(args.local_port):
        print(f"ssh_tunnel: 127.0.0.1:{args.local_port} is already in use", file=sys.stderr)
        return 2

    try:
        creds = credentials(args.device_type, args.user)
    except credstore.CredentialError as exc:
        print(f"ssh_tunnel: {exc}", file=sys.stderr)
        return 2
    except ssh_common.SshConfigError as exc:
        print(f"ssh_tunnel: {exc}", file=sys.stderr)
        return 2
    if not creds:
        print(f"ssh_tunnel: no usable {args.device_type} credential for {args.host}", file=sys.stderr)
        return 2

    failures: List[str] = []
    for cred in creds:
        try:
            proc, err = try_credential(cred, args.host, args.local_port, args.remote,
                                       args.connect_timeout, args.wait)
        except ssh_common.SshConfigError as exc:
            print(f"ssh_tunnel: {exc}", file=sys.stderr)
            return 2
        if proc is None:
            failures.append(f"{cred['username']}: {err}")
            continue
        print(f"tunnel up: 127.0.0.1:{args.local_port} -> {args.remote} via {args.host} "
              f"({cred['username']}, pid {proc.pid})", flush=True)
        try:
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()

    print("ssh_tunnel: no credential could hold the forward", file=sys.stderr)
    for line in failures:
        print(f"  {line}", file=sys.stderr)
    print("ssh_tunnel: enable it on the router first: routeros-temp-access --op temp-ssh", file=sys.stderr)
    return 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
