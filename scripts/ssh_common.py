#!/usr/bin/env python3
"""
ssh_common.py - safe ssh/sshpass invocation shared by the device-touching scripts.

* argv lists only, never a shell string (no shell=True, no quoting bugs, no injection
  through hostnames/commands).
* The password goes to `sshpass -e` through the CHILD's environment (SSHPASS), never
  `sshpass -p <password>` (that puts it in `ps` for every local user).
* Host keys are checked, not ignored. SSH_HOSTKEY_CHECKING (env, default accept-new):
      accept-new   trust a host on first contact, fail if its key later CHANGES
      yes          only hosts already in the known_hosts file
      no           legacy behaviour: no checking, nothing remembered (prints a warning)
  Keys are kept in a dedicated file - SSH_KNOWN_HOSTS_FILE, else
  ~/.cache/nocagent/known_hosts (dir 0700) - never in the operator's own ~/.ssh.
  A device that is factory-reset gets a NEW host key; ssh then refuses with
  "REMOTE HOST IDENTIFICATION HAS CHANGED". After confirming the reset is expected:
      ssh-keygen -R <ip> -f "$HOME/.cache/nocagent/known_hosts"     (or run once with
      SSH_HOSTKEY_CHECKING=no for that device).
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_MODES = ("accept-new", "yes", "no")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_warned = False

# Old airOS/WaveOS firmware only offers legacy RSA host/user key algorithms.
LEGACY_ALGOS = ["-o", "HostKeyAlgorithms=+ssh-rsa,ssh-dss", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]


class SshConfigError(Exception):
    pass


def hostkey_mode() -> str:
    mode = os.environ.get("SSH_HOSTKEY_CHECKING", "").strip().lower() or "accept-new"
    if mode not in _MODES:
        raise SshConfigError(f"SSH_HOSTKEY_CHECKING={mode!r} must be one of {', '.join(_MODES)}")
    return mode


def known_hosts_file() -> Path:
    override = os.environ.get("SSH_KNOWN_HOSTS_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
    else:
        cache = os.environ.get("XDG_CACHE_HOME", "").strip() or str(Path.home() / ".cache")
        p = Path(cache) / "nocagent" / "known_hosts"
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def hostkey_options() -> List[str]:
    global _warned
    mode = hostkey_mode()
    if mode == "no":
        if not _warned:
            print("WARNING: SSH_HOSTKEY_CHECKING=no - SSH host keys are NOT verified", file=sys.stderr)
            _warned = True
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    return ["-o", f"StrictHostKeyChecking={mode}", "-o", f"UserKnownHostsFile={known_hosts_file()}"]


def ssh_options(legacy: bool = False, connect_timeout: int = 8, extra: Optional[List[str]] = None) -> List[str]:
    opts = hostkey_options()
    opts += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
             "-o", f"ConnectTimeout={connect_timeout}", "-o", "LogLevel=ERROR"]
    if legacy:
        opts += LEGACY_ALGOS
    return opts + list(extra or [])


def check_target(user: str, host: str) -> None:
    if not _USER_RE.match(user or ""):
        raise SshConfigError(f"refusing unsafe ssh user {user!r}")
    ok = False
    try:
        ipaddress.ip_address(host)
        ok = True
    except ValueError:
        ok = bool(_HOST_RE.match(host or ""))
    if not ok or host.startswith("-"):
        raise SshConfigError(f"refusing unsafe ssh host {host!r}")


def build_argv(user: str, host: str, command: str, *, legacy: bool = False, connect_timeout: int = 8,
               extra: Optional[List[str]] = None, with_stdin: bool = False) -> List[str]:
    """argv for `sshpass -e ssh ...`. `-n` is only added when no stdin payload is sent
    (with -n ssh reads /dev/null, which would silently discard a piped file)."""
    check_target(user, host)
    argv = ["sshpass", "-e", "ssh"] + ssh_options(legacy, connect_timeout, extra)
    if not with_stdin:
        argv.append("-n")
    return argv + [f"{user}@{host}", command]


def run_ssh(user: str, host: str, command: str, password: str, *, timeout: float = 40, legacy: bool = False,
            connect_timeout: int = 8, extra: Optional[List[str]] = None,
            stdin_data: Optional[str] = None) -> Tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    """Run one remote command. Returns (CompletedProcess, None) or (None, error-text)
    on timeout/missing sshpass. The password lives only in the child's environment."""
    argv = build_argv(user, host, command, legacy=legacy, connect_timeout=connect_timeout, extra=extra,
                      with_stdin=stdin_data is not None)
    env = dict(os.environ, SSHPASS=password)
    try:
        r = subprocess.run(argv, input=stdin_data, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, "SSH timed out"
    except FileNotFoundError:
        return None, "sshpass is not installed"
    return r, None
