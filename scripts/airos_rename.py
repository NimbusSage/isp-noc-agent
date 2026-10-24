#!/usr/bin/env python3
"""airOS rename executor: anchored single-key SSID edits with snapshot/verify/rollback.

Mirrors the audited cycle in mcp-server/src/temp-site/airos-ssh.ts
(snapshot -> anchored sed -> re-read verify -> softrestart save ->
post-verify -> stdin-write restore on miss + boot_id tripwire), driven
step-by-step by the agent (never one blind run).

Rename-only allowlist (closed; adding a key needs the same review as a
new executor capability):
  wireless.1.ssid                         (AP broadcast SSID)
  aaa.1.ssid                              (AP RADIUS accounting SSID mirror)
  wpasupplicant.profile.1.network.1.ssid  (station supplicant SSID)
  resolv.host.1.name                      (device display hostname — identity
                                           only, no RF/security material;
                                           added for a live rename job:
                                           prefix-swap, suffix preserved)

Secrets: read at runtime from the encrypted MCP store (credentials.enc,
NOC_MCP_PASSPHRASE) or an explicit NOC_<POOL>_USER/NOC_<POOL>_PASSWORD env pair
(scripts/credstore.py; the plaintext credentials.json is NOT read), tried in
priority order within the named pool. Never printed, never logged.

Usage:
  airos_rename.py --selftest
  airos_rename.py snapshot <host> --pool customer-airos --dir /tmp/rename-x
  airos_rename.py set <host> <key> <value> --pool ... --dir ...
  airos_rename.py activate <host> --pool ...            # softrestart save
  airos_rename.py verify <host> <key=value>... --pool ...
  airos_rename.py assoc <host> --pool ...               # AP station table
  airos_rename.py restore <host> --pool ... --dir ...   # snapshot restore
"""
import json
import os
import re
import subprocess
import sys
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import credstore  # noqa: E402
import ssh_common  # noqa: E402

ALLOWLIST = (
    "wireless.1.ssid",
    "aaa.1.ssid",
    "wpasupplicant.profile.1.network.1.ssid",
    "resolv.host.1.name",
)

# SSH is run by scripts/ssh_common.py: argv list, password via `sshpass -e` (SSHPASS in the
# child's env, never on a command line), host keys per SSH_HOSTKEY_CHECKING (default
# accept-new) with a dedicated known_hosts file, legacy RSA algorithms for old firmware.
SSH_CONNECT_TIMEOUT = 12


def escape_sed_value(value):
    if re.search(r"[\"'`$\\!;|&<>]", value):
        raise ValueError("Edit value contains shell metacharacters")
    return value.replace("/", "\\/").replace("\n", "")


def build_set_command(key, value):
    if key not in ALLOWLIST:
        raise ValueError("Refusing to edit non-allowlisted key: %s" % key)
    return "sed -i 's/^%s=.*/%s=%s/' /tmp/system.cfg" % (
        key, key, escape_sed_value(value))


def parse_cfg(text):
    out = {}
    for line in text.split("\n"):
        if line.startswith("#"):
            continue
        eq = line.find("=")
        if eq > 0:
            out[line[:eq]] = line[eq + 1:]
    return out


_POOLS = {}


def load_pool(pool):
    if pool not in _POOLS:
        try:
            _POOLS[pool] = credstore.pool(pool)
        except credstore.CredentialError as e:
            sys.exit("Cannot load credential pool %r: %s" % (pool, e))
    return _POOLS[pool]


def ssh(host, user, password, command, timeout=40, stdin_data=None):
    try:
        r, err = ssh_common.run_ssh(user, host, command, password, timeout=timeout, legacy=True,
                                    connect_timeout=SSH_CONNECT_TIMEOUT, stdin_data=stdin_data)
    except ssh_common.SshConfigError as e:
        return None, str(e)
    if r is None:
        return None, err
    if r.returncode != 0:
        return None, (r.stderr.strip() or "rc=%d" % r.returncode)[:200]
    return r.stdout, None


def try_pool(host, pool, command, timeout=40, stdin_data=None):
    """Try each credential in pool order. Returns (output, cred_name)."""
    last_err = "no credentials"
    for cred in load_pool(pool):
        user = cred.get("username") or cred.get("user")
        out, err = ssh(host, user, cred.get("password", ""), command,
                       timeout, stdin_data)
        time.sleep(3)  # dropbear throttles rapid reconnects
        if out is not None:
            return out, cred.get("name")
        last_err = err
        if "Permission denied" not in (err or ""):
            break  # Cred worked but command failed; don't spray others
    raise RuntimeError("All credentials failed on %s: %s" % (host, last_err))


def snapshot_path(host, directory):
    safe = re.sub(r"[^0-9a-zA-Z.-]", "_", host)
    return os.path.join(directory, safe + ".cfg")


def cmd_snapshot(args):
    os.makedirs(args.dir, exist_ok=True)
    boot, _ = try_pool(args.host, args.pool,
                       "cat /proc/sys/kernel/random/boot_id")
    boot = boot.strip()
    if not re.match(r"^[0-9a-f-]{36}$", boot):
        sys.exit("Could not read target boot_id; refusing (unknown state)")
    cfg, _ = try_pool(args.host, args.pool, "cat /tmp/system.cfg")
    if "wireless.1.ssid=" not in cfg:
        sys.exit("Snapshot does not look like an airOS system.cfg; refusing")
    path = snapshot_path(args.host, args.dir)
    with open(path, "w") as f:
        f.write(cfg)
    os.chmod(path, 0o600)
    with open(os.path.join(args.dir, re.sub(r"[^0-9a-zA-Z.-]", "_", args.host) + ".bootid"), "w") as f:
        f.write(boot + "\n")
    print("snapshot: %d bytes -> %s sha256=%s boot_id=%s" %
          (len(cfg), path, hashlib.sha256(cfg.encode()).hexdigest()[:12], boot[:8]))


def cmd_set(args):
    if args.key not in ALLOWLIST:
        sys.exit("Refusing non-allowlisted key: %s" % args.key)
    out, cred = try_pool(args.host, args.pool, build_set_command(args.key, args.value))
    check, _ = try_pool(args.host, args.pool,
                        "grep -E '^(%s)=' /tmp/system.cfg" % re.escape(args.key))
    got = parse_cfg(check).get(args.key)
    if got != args.value:
        sys.exit("RE-READ MISMATCH: %s is %r, want %r (change staged but NOT saved; restore before activate)"
                 % (args.key, got, args.value))
    print("staged+verified (unsaved): %s=%s via %s" % (args.key, got, cred))


def cmd_activate(args):
    out, cred = try_pool(args.host, args.pool,
                         "/usr/etc/rc.d/rc.softrestart save", timeout=60)
    print("softrestart save issued via %s; waiting 25s for wireless restart" % cred)
    time.sleep(25)


def cmd_verify(args):
    keys = [kv.split("=", 1)[0] for kv in args.kv]
    want = dict(kv.split("=", 1) for kv in args.kv)
    check, _ = try_pool(args.host, args.pool,
                        "grep -E '^(%s)=' /tmp/system.cfg" % "|".join(re.escape(k) for k in keys))
    got = parse_cfg(check)
    bad = ["%s=%r want %r" % (k, got.get(k), v) for k, v in want.items() if got.get(k) != v]
    if bad:
        sys.exit("VERIFY FAILED: " + "; ".join(bad))
    ping = subprocess.run(["ping", "-c", "3", "-W", "2", args.host],
                          capture_output=True, text=True, timeout=20)
    print("verify OK: %s | ping rc=%d" %
          (", ".join("%s=%s" % (k, got.get(k)) for k in keys), ping.returncode))


def cmd_assoc(args):
    out, _ = try_pool(args.host, args.pool, "wlanconfig ath0 list sta")
    lines = [l for l in out.strip().split("\n") if l.strip()]
    print("%d associated stations:" % max(0, len(lines) - 1))
    for l in lines[:12]:
        print("  " + " ".join(l.split()[:8]))


def cmd_restore(args):
    path = snapshot_path(args.host, args.dir)
    with open(path) as f:
        snapshot = f.read()
    # stdin-stream write: dropbear has no sftp-server; content never
    # appears in any command string (mirrors SshRunner.write).
    _, cred = try_pool(args.host, args.pool, "cat > /tmp/system.cfg",
                       stdin_data=snapshot)
    try_pool(args.host, args.pool, "/usr/etc/rc.d/rc.softrestart save", timeout=60)
    print("snapshot restored via %s + softrestart save; re-verify after 25s" % cred)
    time.sleep(25)


def selftest():
    assert build_set_command("wireless.1.ssid", "customer5o") == \
        "sed -i 's/^wireless.1.ssid=.*/wireless.1.ssid=customer5o/' /tmp/system.cfg"
    assert build_set_command("resolv.host.1.name", "siteA2siteB APv3 UNMS") == \
        "sed -i 's/^resolv.host.1.name=.*/resolv.host.1.name=siteA2siteB APv3 UNMS/' /tmp/system.cfg"
    try:
        build_set_command("users.1.password", "x")
        raise SystemExit("allowlist FAIL: password key accepted")
    except ValueError:
        pass
    try:
        build_set_command("wireless.1.ssid", "a;b")
        raise SystemExit("escape FAIL: metachars accepted")
    except ValueError:
        pass
    assert parse_cfg("a=1\n# c\nb=x=y").get("b") == "x=y"
    print("selftest: allowlist, sed-escape, cfg-parse OK")


def main():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="op", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("host"); s.add_argument("--pool", required=True); s.add_argument("--dir", required=True)
    s = sub.add_parser("set"); s.add_argument("host"); s.add_argument("key"); s.add_argument("value"); s.add_argument("--pool", required=True); s.add_argument("--dir", required=True)
    s = sub.add_parser("activate"); s.add_argument("host"); s.add_argument("--pool", required=True)
    s = sub.add_parser("verify"); s.add_argument("host"); s.add_argument("kv", nargs="+"); s.add_argument("--pool", required=True)
    s = sub.add_parser("assoc"); s.add_argument("host"); s.add_argument("--pool", required=True)
    s = sub.add_parser("restore"); s.add_argument("host"); s.add_argument("--pool", required=True); s.add_argument("--dir", required=True)
    sub.add_parser("selftest")
    args = p.parse_args()
    if args.op != "selftest":
        try:
            ssh_common.check_target("admin", args.host)  # strict host syntax: no option injection into ssh/ping
        except ssh_common.SshConfigError as e:
            sys.exit(str(e))
    {"snapshot": cmd_snapshot, "set": cmd_set, "activate": cmd_activate,
     "verify": cmd_verify, "assoc": cmd_assoc, "restore": cmd_restore,
     "selftest": lambda a: selftest()}[args.op](args)


if __name__ == "__main__":
    main()
