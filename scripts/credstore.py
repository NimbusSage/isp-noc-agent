#!/usr/bin/env python3
"""
credstore.py - device credentials for the python/bash ops scripts, WITHOUT the
plaintext mcp-server/config/credentials.json.

Source order for a device type (e.g. mikrotik-ssh, customer-airos, ubnt-wave, ubnt-uisp):
  1. Explicit operator-supplied environment variables (win when set):
         NOC_<TYPE>_USER, NOC_<TYPE>_PASSWORD      one credential
         NOC_<TYPE>_API_TOKEN                      token-style types (ubnt-uisp)
     <TYPE> = device type upper-cased with '-' -> '_', e.g. NOC_MIKROTIK_SSH_PASSWORD.
     mikrotik-ssh defaults its user to "admin".
  2. The encrypted MCP store mcp-server/config/credentials.enc, decrypted by
     scripts/cred_store.mjs (needs `node` and NOC_MCP_PASSPHRASE in the environment
     or .env). Secrets travel over a pipe; they are never put in argv.
  Neither available -> CredentialError with the message telling the operator how to fix it.

Use passwords with `sshpass -e` (SSHPASS in the child's environment), never `sshpass -p`.

CLI (for bash scripts; refuses to print to a tty):
    python3 scripts/credstore.py passwords <deviceType> [--user admin] [--b64]
      default: NUL-separated passwords; --b64: one base64 line per password (bash-variable safe)
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

HERE = Path(__file__).resolve().parent
NODE_HELPER = HERE / "cred_store.mjs"


class CredentialError(Exception):
    pass


def _env_prefix(device_type: str) -> str:
    return "NOC_" + device_type.upper().replace("-", "_")


def _hint(device_type: str) -> str:
    p = _env_prefix(device_type)
    return (f"provide credentials via {p}_USER/{p}_PASSWORD (explicit, per run) or the encrypted MCP store "
            "(mcp-server/config/credentials.enc with NOC_MCP_PASSPHRASE in the environment or .env). "
            "The plaintext mcp-server/config/credentials.json is no longer read.")


def env_override(device_type: str) -> List[Dict]:
    p = _env_prefix(device_type)
    token = envfile.get(f"{p}_API_TOKEN")
    password = os.environ.get(f"{p}_PASSWORD", "")      # not envfile: a password may legitimately have edge whitespace
    user = os.environ.get(f"{p}_USER", "").strip() or ("admin" if device_type == "mikrotik-ssh" else "")
    if token:
        return [{"name": "env", "username": user, "password": password, "apiToken": token, "priority": 100}]
    if password and user:
        return [{"name": "env", "username": user, "password": password, "priority": 100}]
    return []


def from_store(device_type: str, user: Optional[str] = None) -> List[Dict]:
    node = shutil.which("node")
    if not node:
        raise CredentialError("node is required to read the encrypted credential store (not found on PATH). "
                              + _hint(device_type))
    passphrase = envfile.get("NOC_MCP_PASSPHRASE")
    if not passphrase:
        raise CredentialError("NOC_MCP_PASSPHRASE is not set (environment or .env). " + _hint(device_type))
    cmd = [node, str(NODE_HELPER), "pool", device_type, "--reveal"]
    if user:
        cmd += ["--user", user]
    child_env = dict(os.environ, NOC_MCP_PASSPHRASE=passphrase)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=child_env, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise CredentialError(f"credential helper failed to run: {e}")
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise CredentialError(f"{detail[0]}. " + _hint(device_type))
    try:
        creds = json.loads(r.stdout)
    except ValueError:
        raise CredentialError("credential helper returned unparseable output")
    if not isinstance(creds, list) or not creds:
        raise CredentialError(f"no credentials for '{device_type}' in the store. " + _hint(device_type))
    return creds


def pool(device_type: str, user: Optional[str] = None) -> List[Dict]:
    """Credentials for `device_type`, highest priority first. Raises CredentialError."""
    if not device_type or not all(c.islower() or c.isdigit() or c == "-" for c in device_type):
        raise CredentialError(f"invalid device type {device_type!r}")
    override = env_override(device_type)
    if override:
        return [c for c in override if not user or c["username"] == user]
    return from_store(device_type, user)


def main(argv: List[str]) -> int:
    if len(argv) < 3 or argv[1] != "passwords":
        print("usage: credstore.py passwords <deviceType> [--user NAME]", file=sys.stderr)
        return 1
    if sys.stdout.isatty():
        print("credstore: refusing to print secrets to a terminal", file=sys.stderr)
        return 1
    user = argv[argv.index("--user") + 1] if "--user" in argv[3:] and argv.index("--user") + 1 < len(argv) else None
    try:
        creds = pool(argv[2], user)
    except CredentialError as e:
        print(f"credstore: {e}", file=sys.stderr)
        return 2
    if "--b64" in argv:
        sys.stdout.write("".join(base64.b64encode(c.get("password", "").encode()).decode() + "\n" for c in creds))
    else:
        sys.stdout.write("".join(c.get("password", "") + "\0" for c in creds))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
