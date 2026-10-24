#!/usr/bin/env python3
"""WaveOS rename executor: jq-filter SSID/hostname edits with snapshot/verify/rollback.

Mirrors the audited cycle in scripts/airos_rename.py
(snapshot -> anchored edit -> re-read verify -> commit ->
post-verify -> key-restore on miss + boot tripwire), driven
step-by-step by the agent (never one blind run).

WaveOS (Buildroot) has no /tmp/system.cfg. Live config is the ucfg JSON
(`ucfg show`); edits apply via `ucfg edit '<jq filter>'` and persist via
`ucfg commit`. Verified live 2026-10-11 on a Wave-LR AP.

CRITICAL SEMANTIC DIFFERENCE FROM airOS: `ucfg edit` applies to the
RUNNING config IMMEDIATELY (there is no staging). For an ST, the link
drops at `set`, not at `commit` — and the ST's management return path
typically crosses the RF link, so the post-set re-read fails with
No-route-to-host. That darkness is EXPECTED (same as airOS post-activate):
cmd_set treats edit-ok + re-read-unreachable as ST-DARK and exits 0 so the
agent proceeds PROMPTLY to the AP flip. Stopping between ST-set and AP-set
strands the link (live Wave rename lesson). `commit` only persists; run it
promptly after verify so a reboot cannot revert the change.

Rename-only allowlist (closed; adding a key needs the same review as a
new executor capability):
  .wireless.ssid    (broadcast SSID — leading spaces are significant,
                     preserve byte-for-byte except the site-leg swap)
  .system.hostname  (device display hostname — identity only)

Secrets: read at runtime from the encrypted MCP store (credentials.enc,
NOC_MCP_PASSPHRASE) or an explicit NOC_<POOL>_USER/NOC_<POOL>_PASSWORD env pair
(scripts/credstore.py; the plaintext credentials.json is NOT read), tried in
priority order within the named pool (ubnt-wave). Never printed, never logged.

Usage:
  wave_rename.py --selftest
  wave_rename.py snapshot <host> --pool ubnt-wave --dir /tmp/rename-x
  wave_rename.py set <host> <jqpath> <value> --pool ... --dir ...
  wave_rename.py commit <host> --pool ...                 # ucfg commit
  wave_rename.py verify <host> <jqpath=value>... --pool ...
  wave_rename.py assoc <host> --pool ...                  # apstats -R
  wave_rename.py restore <host> --pool ... --dir ...      # key restore
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
    ".wireless.ssid",
    ".system.hostname",
)

# SSH is run by scripts/ssh_common.py: argv list, password via `sshpass -e` (SSHPASS in the
# child's env, never on a command line), host keys per SSH_HOSTKEY_CHECKING (default
# accept-new) with a dedicated known_hosts file, legacy RSA algorithms for old firmware.
SSH_CONNECT_TIMEOUT = 12


def build_edit_filter(path, value):
    if path not in ALLOWLIST:
        raise ValueError("Refusing to edit non-allowlisted path: %s" % path)
    if "'" in value or "\n" in value:
        raise ValueError("Edit value contains quote/newline (shell-unsafe)")
    return "%s = %s" % (path, json.dumps(value))


def get_path(cfg, dotted):
    cur = cfg
    for part in dotted.lstrip(".").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


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
    return os.path.join(directory, safe + ".ucfg.json")


def fetch_cfg(host, pool):
    cfg_text, _ = try_pool(host, pool, "ucfg show")
    try:
        return json.loads(cfg_text), cfg_text
    except json.JSONDecodeError:
        sys.exit("Snapshot does not look like WaveOS ucfg JSON; refusing")


def boot_tripwire(host, pool, directory):
    """Record boot identity. boot_id preferred; uptime-monotonic fallback."""
    safe = re.sub(r"[^0-9a-zA-Z.-]", "_", host)
    try:
        boot, _ = try_pool(host, pool, "cat /proc/sys/kernel/random/boot_id")
        boot = boot.strip()
        if re.match(r"^[0-9a-f-]{36}$", boot):
            with open(os.path.join(directory, safe + ".bootid"), "w") as f:
                f.write("boot_id:" + boot + "\n")
            return "boot_id=" + boot[:8]
    except RuntimeError:
        pass
    up, _ = try_pool(host, pool, "cat /proc/uptime")
    secs = float(up.split()[0])
    with open(os.path.join(directory, safe + ".bootid"), "w") as f:
        f.write("uptime_at_snapshot:%.0f\n" % secs)
    return "uptime=%.0fs" % secs


def cmd_snapshot(args):
    os.makedirs(args.dir, exist_ok=True)
    cfg, cfg_text = fetch_cfg(args.host, args.pool)
    if get_path(cfg, ".wireless.ssid") is None:
        sys.exit("Snapshot lacks .wireless.ssid; refusing (not a Wave radio?)")
    trip = boot_tripwire(args.host, args.pool, args.dir)
    path = snapshot_path(args.host, args.dir)
    with open(path, "w") as f:
        f.write(cfg_text)
    os.chmod(path, 0o600)
    print("snapshot: %d bytes -> %s sha256=%s %s" %
          (len(cfg_text), path,
           hashlib.sha256(cfg_text.encode()).hexdigest()[:12], trip))


def cmd_set(args):
    filt = build_edit_filter(args.path, args.value)
    out, cred = try_pool(args.host, args.pool,
                         "ucfg edit '%s'" % filt)
    # The edit above applied LIVE (Wave has no staging). Re-read to confirm.
    # If the host is now unreachable, the edit itself just dropped the link
    # (expected for an ST whose return path crosses RF) — report ST-DARK and
    # exit 0 so the agent proceeds PROMPTLY to the AP flip. A value mismatch
    # (reachable but wrong) is still a hard failure.
    try:
        cfg, _ = fetch_cfg(args.host, args.pool)
    except SystemExit:
        raise
    except Exception:
        print("ST-DARK (expected): edit applied via %s but host unreachable "
              "for re-read — link dropped as designed; flip the AP side "
              "PROMPTLY, verify both ends after reassociation" % cred)
        return
    got = get_path(cfg, args.path)
    if got != args.value:
        sys.exit("RE-READ MISMATCH: %s is %r, want %r (restore before commit)"
                 % (args.path, got, args.value))
    print("applied+verified live: %s=%r via %s (commit to persist)" %
          (args.path, got, cred))


def cmd_commit(args):
    out, cred = try_pool(args.host, args.pool, "ucfg commit", timeout=60)
    print("ucfg commit issued via %s; waiting 25s for wireless restart" % cred)
    time.sleep(25)


def cmd_verify(args):
    cfg, _ = fetch_cfg(args.host, args.pool)
    bad = []
    for kv in args.kv:
        path, want = kv.split("=", 1)
        if get_path(cfg, path) != want:
            bad.append("%s=%r want %r" % (path, get_path(cfg, path), want))
    if bad:
        sys.exit("VERIFY FAILED: " + "; ".join(bad))
    ping = subprocess.run(["ping", "-c", "3", "-W", "2", args.host],
                          capture_output=True, text=True, timeout=20)
    print("verify OK: %s | ping rc=%d" %
          (", ".join("%s=%r" % tuple(kv.split("=", 1)) for kv in args.kv),
           ping.returncode))


def cmd_assoc(args):
    out, _ = try_pool(args.host, args.pool, "apstats -R")
    print(out[:2000])


def cmd_restore(args):
    path = snapshot_path(args.host, args.dir)
    with open(path) as f:
        snap = json.load(f)
    for keypath in ALLOWLIST:
        want = get_path(snap, keypath)
        if want is None:
            continue
        filt = build_edit_filter(keypath, want)
        try_pool(args.host, args.pool, "ucfg edit '%s'" % filt)
    try_pool(args.host, args.pool, "ucfg commit", timeout=60)
    print("snapshot keys restored + committed; re-verify after 25s")
    time.sleep(25)


def selftest():
    assert build_edit_filter(".wireless.ssid", " siteB2siteA") == \
        '.wireless.ssid = " siteB2siteA"'
    try:
        build_edit_filter(".wireless.encryption.passphrase", "x")
        raise SystemExit("allowlist FAIL: passphrase path accepted")
    except ValueError:
        pass
    try:
        build_edit_filter(".wireless.ssid", "a'b")
        raise SystemExit("escape FAIL: single-quote accepted")
    except ValueError:
        pass
    assert get_path({"wireless": {"ssid": "x"}}, ".wireless.ssid") == "x"
    assert get_path({}, ".wireless.ssid") is None
    print("selftest: allowlist, jq-escape, cfg-parse OK")


def main():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="op", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("host"); s.add_argument("--pool", required=True); s.add_argument("--dir", required=True)
    s = sub.add_parser("set"); s.add_argument("host"); s.add_argument("path"); s.add_argument("value"); s.add_argument("--pool", required=True); s.add_argument("--dir", required=True)
    s = sub.add_parser("commit"); s.add_argument("host"); s.add_argument("--pool", required=True)
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
    {"snapshot": cmd_snapshot, "set": cmd_set, "commit": cmd_commit,
     "verify": cmd_verify, "assoc": cmd_assoc, "restore": cmd_restore,
     "selftest": lambda a: selftest()}[args.op](args)


if __name__ == "__main__":
    main()