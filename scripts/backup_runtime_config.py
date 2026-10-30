#!/usr/bin/env python3
"""backup_runtime_config.py — archive NocAgent's runtime secrets and state OUTSIDE the repo.

Why: .env, site-pack/profile.json, mcp-server/config/credentials.enc and the
Google OAuth secret JSONs are intentionally gitignored — they exist only on
the working box. If it is lost, onboarding must be redone from memory. The
audit trail (decision log), circuit-breaker state and the pending peak-retest
queue live in the state dir (logs/ by default) and are lost the same way.

What it does:
- Collects: .env, site-pack/profile.json, its secret overlay site-pack/profile.local.json
  (the overlays under topology/ come with that tree), credentials.enc, every
  *_OAUTH_FILE path found in .env (files must live outside the repo —
  their contents are archived, never printed), the gitignored
  site-pack/topology/ tree (org network knowledge — untracked by design), and
  runtime state: logs/decisions/*.jsonl, circuit_breaker.json and the peak
  queue (pending_peak_checks.json, or PEAK_QUEUE_FILE).
- Writes a 0600 archive to the backup dir (default /root/nocagent-backups,
  override with BACKUP_DIR; created 0700). NEVER inside this repo. The tarball
  is created 0600 from its first byte (no umask window).
- ENCRYPTION: the archive holds .env (which carries NOC_MCP_PASSPHRASE) AND
  credentials.enc side by side, so a plain tarball is the whole credential store
  in the clear. Set BACKUP_PASSPHRASE (or BACKUP_PASSPHRASE_FILE=<path to a 0600
  file holding it>) and the tarball is encrypted with
      openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass env:BACKUP_PASSPHRASE
  (the passphrase travels in the child's environment, never argv) and the
  plaintext tar is deleted; the result is nocagent-config-<stamp>.tgz.enc.
  Without BACKUP_PASSPHRASE or without openssl you still get a 0600 plaintext
  .tgz and a loud WARNING. Keep the passphrase OFF this host (password manager):
  it is deliberately not read from .env, because .env is inside the archive.
- Retention: keeps the newest N (default 14) archives (.tgz and .tgz.enc), deletes older.
- --list: enumerate an archive's members (paths only); --restore TO_DIR:
  extract 0600 file modes into TO_DIR/runtime (manual copy-back to the new install).

Usage:
  python3 scripts/backup_runtime_config.py
  python3 scripts/backup_runtime_config.py --list <archive>
  python3 scripts/backup_runtime_config.py --restore <archive> /tmp/noc-restore

Env: BACKUP_DIR, BACKUP_KEEP, BACKUP_PASSPHRASE, BACKUP_PASSPHRASE_FILE,
     NOC_STATE_DIR (state root; default NOC_AGENT_LOG_DIR, else <repo>/logs),
     DECISION_LOG_DIR, BREAKER_STATE_FILE, PEAK_QUEUE_FILE.

RESTORE (e.g. onto a rebuilt host):
  1. Decrypt + unpack (encrypted archives; passphrase from your password manager):
       export BACKUP_PASSPHRASE='...'
       python3 scripts/backup_runtime_config.py --restore nocagent-config-<stamp>.tgz.enc /tmp/noc-restore
     (--restore decrypts automatically for *.enc). Manual equivalent:
       openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -pass env:BACKUP_PASSPHRASE \\
           -in nocagent-config-<stamp>.tgz.enc | tar xz -C /tmp/noc-restore
  2. Copy files from /tmp/noc-restore/runtime/ into place (all extracted 0600):
       .env                        -> <repo>/.env
       profile.json                -> <repo>/site-pack/profile.json
       profile.local.json          -> <repo>/site-pack/profile.local.json
       credentials.enc             -> <repo>/mcp-server/config/credentials.enc
       <name>.json (OAuth)         -> the path named by the matching *_OAUTH_FILE in .env
       topology__a__b.json         -> <repo>/site-pack/topology/a/b.json   ("__" = "/")
       state__decisions__YYYY-MM.jsonl -> <state dir>/decisions/YYYY-MM.jsonl
       state__circuit_breaker.json -> <state dir>/circuit_breaker.json
       state__pending_peak_checks.json -> <state dir>/pending_peak_checks.json
  3. shred/delete /tmp/noc-restore afterwards; never commit any of it.

Moving to a new machine: --install puts the members where they belong in a fresh checkout instead of
copying them by hand.
       python3 scripts/backup_runtime_config.py --install <archive> <repo> [--dry-run] [--force] [--with-state]
  It places .env, credentials.enc, profile.json, profile.local.json, the topology tree and the OAuth
  files (each at the path its *_OAUTH_FILE setting names, which must be under your home directory and
  outside the repo) with mode 0600. A file that already exists is kept and reported (so a site pack
  fetched with scripts/pack.py join stays authoritative); --force replaces it. State (decision log,
  breaker, peak queue) is skipped unless --with-state. --dry-run lists the plan and writes nothing.
  Nothing is ever printed from inside a file.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402
import noc_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
OPENSSL_ARGS = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000"]
ARCHIVE_GLOBS = ("nocagent-config-*.tgz", "nocagent-config-*.tgz.enc")


def load_env() -> dict:
    return envfile.load(ROOT / ".env")


def state_members():
    """(path, arcname) for decision-log shards, breaker state and the peak queue that exist."""
    state = noc_config.state_dir()
    out = []
    decisions = Path(os.environ.get("DECISION_LOG_DIR", "").strip() or state / "decisions")
    if decisions.is_dir():
        for p in sorted(decisions.glob("*.jsonl")):
            out.append((p, "state__decisions__" + p.name))
    breaker = Path(os.environ.get("BREAKER_STATE_FILE", "").strip() or state / "circuit_breaker.json")
    if breaker.is_file():
        out.append((breaker, "state__" + breaker.name))
    peak = Path(envfile.get("PEAK_QUEUE_FILE") or state / "pending_peak_checks.json")
    if peak.is_file():
        out.append((peak, "state__" + peak.name))
    return out


def collect_members():
    env = load_env()
    members = []
    for rel in (".env", "site-pack/profile.json", "site-pack/profile.local.json", "mcp-server/config/credentials.enc"):
        p = ROOT / rel
        if p.exists():
            members.append(p)
        else:
            print(f"  note: {rel} not present on this install (fine)")
    for key, val in sorted(env.items()):
        if key.endswith("_OAUTH_FILE") and val:
            p = Path(val)
            if p.exists():
                members.append(p)
            else:
                print(f"  note: {key} points to missing file {val}")
    topo = ROOT / "site-pack" / "topology"
    if topo.is_dir():
        for p in sorted(topo.rglob("*")):
            if p.is_file():
                members.append(p)
    else:
        print("  note: site-pack/topology/ not present on this install (fine)")
    if not members:
        raise SystemExit("nothing to back up — no .env, profile.json or credentials.enc found")
    # Step 1: collapse by RESOLVED path — many vars may point at one shared
    # secret file (this install: all three *_OAUTH_FILE -> one file). Archiving
    # the same real file multiple times is duplicate noise, not risk.
    by_real, unique = {}, []
    for m in members:
        real = os.path.realpath(m)
        if real not in by_real:
            by_real[real] = m
            unique.append(m)
    # Step 2: distinct arcnames even when different files share a basename —
    # duplicate arcnames in a tar silently keep only the last.
    in_use, result = {}, []
    for m in unique:
        # Topology files keep their tree (site-pack/topology/...) encoded in
        # the arcname so a restore can map them back one-for-one; flat
        # runtime secrets keep their plain basename.
        topo_prefix = ROOT / "site-pack" / "topology"
        if topo_prefix in m.parents:
            arc = "topology__" + str(m.relative_to(topo_prefix)).replace(os.sep, "__")
        else:
            arc = m.name
            if arc in in_use:
                arc = f"{m.parent.name}__{m.name}"
        if arc in in_use:
            raise SystemExit(f"arcname collision for {m}; refusing to overwrite another secret")
        in_use[arc] = m
        result.append((m, arc))
    for path, arc in state_members():
        if arc in in_use:
            raise SystemExit(f"arcname collision for {path}; refusing to overwrite another file")
        in_use[arc] = path
        result.append((path, arc))
    return result


def backup_dir() -> Path:
    d = Path(os.environ.get("BACKUP_DIR", "/root/nocagent-backups"))
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.stat(d).st_mode & 0o077:
        os.chmod(d, 0o700)
    return d


def get_passphrase() -> str:
    """BACKUP_PASSPHRASE, else the first line of BACKUP_PASSPHRASE_FILE. Not read from .env on purpose."""
    pw = os.environ.get("BACKUP_PASSPHRASE", "")
    if pw:
        return pw
    pf = os.environ.get("BACKUP_PASSPHRASE_FILE", "").strip()
    if pf:
        try:
            st = os.stat(pf)
            if st.st_mode & 0o077:
                print(f"WARNING: {pf} is group/world accessible; chmod 600 it", file=sys.stderr)
            return Path(pf).read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError) as e:
            print(f"WARNING: BACKUP_PASSPHRASE_FILE unreadable: {e}", file=sys.stderr)
    return ""


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    """openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt -pass env:BACKUP_PASSPHRASE."""
    env = dict(os.environ, BACKUP_PASSPHRASE=passphrase)
    subprocess.run(["openssl", *OPENSSL_ARGS, "-salt", "-pass", "env:BACKUP_PASSPHRASE",
                    "-in", str(src), "-out", str(dst)],
                   check=True, env=env, capture_output=True, timeout=600)


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    env = dict(os.environ, BACKUP_PASSPHRASE=passphrase)
    subprocess.run(["openssl", *OPENSSL_ARGS, "-d", "-pass", "env:BACKUP_PASSPHRASE",
                    "-in", str(src), "-out", str(dst)],
                   check=True, env=env, capture_output=True, timeout=600)


def _new_private_file(path: Path) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def run_backup() -> Path:
    d = backup_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    members = collect_members()

    # Tarball is born 0600 (mkstemp) inside the 0700 backup dir; never visible with a wider mode.
    fd, tmp_name = tempfile.mkstemp(prefix=".nocagent-config-", suffix=".partial", dir=str(d))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for m, arc in members:
                tar.add(m, arcname=f"runtime/{arc}")  # source file perms untouched
            raw.flush()
        os.chmod(tmp, 0o600)

        passphrase = get_passphrase()
        encrypted = False
        target = d / f"nocagent-config-{stamp}.tgz"
        if passphrase and shutil.which("openssl"):
            enc_target = d / f"nocagent-config-{stamp}.tgz.enc"
            try:
                _new_private_file(enc_target)
                encrypt_file(tmp, enc_target, passphrase)
                os.chmod(enc_target, 0o600)
                target, encrypted = enc_target, True
            except (subprocess.SubprocessError, OSError) as e:
                detail = getattr(e, "stderr", b"") or b""
                print(f"WARNING: encryption failed ({type(e).__name__}: "
                      f"{detail.decode(errors='replace').strip()[:120] or e}); falling back to a PLAINTEXT archive",
                      file=sys.stderr)
                enc_target.unlink(missing_ok=True)
        if encrypted:
            tmp.unlink()                                  # delete the plaintext tar
        else:
            os.replace(tmp, target)
            os.chmod(target, 0o600)
            why = ("BACKUP_PASSPHRASE is not set" if not passphrase
                   else "openssl is not installed" if not shutil.which("openssl") else "encryption failed")
            print("=" * 78, file=sys.stderr)
            print(f"WARNING: {target.name} is NOT ENCRYPTED ({why}). It contains .env (with "
                  "NOC_MCP_PASSPHRASE) next to credentials.enc.", file=sys.stderr)
            print("         Set BACKUP_PASSPHRASE (or BACKUP_PASSPHRASE_FILE) and install openssl; "
                  "keep that passphrase off this host.", file=sys.stderr)
            print("=" * 78, file=sys.stderr)
    finally:
        if tmp.exists():
            tmp.unlink()

    kept = sorted(p for g in ARCHIVE_GLOBS for p in d.glob(g))
    kept.sort(key=lambda p: p.name)
    for old in kept[:-KEEP] if len(kept) > KEEP else []:
        old.unlink()
    mode = "AES-256-CBC/PBKDF2 encrypted" if encrypted else "PLAINTEXT"
    print(f"OK: archived {len(members)} runtime files -> {target} ({mode}, mode 600, dir 700; retention {KEEP})")
    print("  members:", ", ".join(sorted(arc for _, arc in members)))
    return target


def _open_archive(archive: str):
    """Return (tarfile, cleanup_callable). Decrypts *.enc into a private temp file first."""
    path = Path(archive)
    if path.suffix != ".enc":
        return tarfile.open(path, "r:gz"), (lambda: None)
    passphrase = get_passphrase()
    if not passphrase:
        raise SystemExit("this archive is encrypted: set BACKUP_PASSPHRASE (or BACKUP_PASSPHRASE_FILE)")
    if not shutil.which("openssl"):
        raise SystemExit("openssl is required to read an encrypted archive")
    fd, tmp_name = tempfile.mkstemp(prefix="nocagent-restore-", suffix=".tgz")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        decrypt_file(path, tmp, passphrase)
        tar = tarfile.open(tmp, "r:gz")
    except (subprocess.CalledProcessError, tarfile.TarError, OSError):
        tmp.unlink(missing_ok=True)
        raise SystemExit("could not decrypt/open the archive (wrong BACKUP_PASSPHRASE or corrupt file)")
    return tar, (lambda: tmp.unlink(missing_ok=True))


def do_list(archive: str) -> None:
    tar, cleanup = _open_archive(archive)
    try:
        for m in tar.getmembers():
            print(f"  {m.name}  ({m.size} bytes)")
    finally:
        tar.close()
        cleanup()


def do_restore(archive: str, to_dir: str) -> None:
    """Safe manual extraction: only flat runtime/<file> members, mode 0600.

    (tarfile's filter= param for extract is 3.12+; do it manually so this runs
    on 3.9+ — and to avoid over-permissive modes these secrets shouldn't have.)
    """
    dst = Path(to_dir) / "runtime"
    dst.mkdir(mode=0o700, parents=True, exist_ok=True)
    tar, cleanup = _open_archive(archive)
    try:
        for m in tar.getmembers():
            if not (m.isfile() and m.name.startswith("runtime/")):
                print(f"  skip non-runtime member: {m.name}")
                continue
            flat = Path(m.name).name
            if flat in ("", ".", ".."):
                continue
            b = tar.extractfile(m).read()
            fd = os.open(str(dst / flat), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(b)
    finally:
        tar.close()
        cleanup()
    print(f"restored to {dst.parent} with 0600 modes — copy files into place manually; never commit them")


FIXED_TARGETS = {
    ".env": ".env",
    "profile.json": "site-pack/profile.json",
    "profile.local.json": "site-pack/profile.local.json",
    "credentials.enc": "mcp-server/config/credentials.enc",
}


def _safe_parts(name: str) -> list:
    parts = name.split("__")
    if not parts or any(p in ("", ".", "..") or "/" in p or "\\" in p for p in parts):
        return []
    return parts


def _oauth_targets(env: dict, repo: Path) -> dict:
    """flat archive name -> target path for each *_OAUTH_FILE in the restored .env (home-relative, outside the repo only)."""
    home, out = Path.home().resolve(), {}
    for key, val in sorted(env.items()):
        if not (key.endswith("_OAUTH_FILE") and val):
            continue
        p = Path(val).expanduser()
        if not p.is_absolute():
            continue
        resolved = p.resolve()
        inside_home = home == resolved or home in resolved.parents
        inside_repo = repo.resolve() == resolved or repo.resolve() in resolved.parents
        if not inside_home or inside_repo:
            continue
        out.setdefault(p.name, p)
        out.setdefault(f"{p.parent.name}__{p.name}", p)
    return out


def _write_private(target: Path, data: bytes) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".restore-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def do_install(archive: str, repo_dir: str, force: bool = False, dry_run: bool = False, with_state: bool = False) -> int:
    repo = Path(repo_dir)
    if not repo.is_dir():
        raise SystemExit(f"{repo} is not a directory")
    tar, cleanup = _open_archive(archive)
    try:
        members = {}
        for m in tar.getmembers():
            if m.isfile() and m.name.startswith("runtime/"):
                flat = Path(m.name).name
                if flat not in ("", ".", ".."):
                    members[flat] = tar.extractfile(m).read()
    finally:
        tar.close()
        cleanup()
    env_text = members.get(".env", b"").decode("utf-8", errors="replace")
    existing_env = repo / ".env"
    env = envfile.parse(existing_env.read_text(encoding="utf-8", errors="replace")) if existing_env.exists() and not force else envfile.parse(env_text)
    oauth = _oauth_targets(env, repo)
    state_dir = noc_config.state_dir() if repo.resolve() == ROOT.resolve() else repo / "logs"
    plan, manual = [], []
    for flat in sorted(members):
        if flat in FIXED_TARGETS:
            target = repo / FIXED_TARGETS[flat]
        elif flat.startswith("topology__"):
            parts = _safe_parts(flat[len("topology__"):])
            target = repo.joinpath("site-pack", "topology", *parts) if parts else None
        elif flat.startswith("state__"):
            if not with_state:
                plan.append((flat, None, "skipped (state; use --with-state)"))
                continue
            rest = flat[len("state__"):]
            if rest.startswith("decisions__") and _safe_parts(rest[len("decisions__"):]):
                target = state_dir / "decisions" / rest[len("decisions__"):]
            elif rest in ("circuit_breaker.json", "pending_peak_checks.json"):
                target = state_dir / rest
            else:
                target = None
        elif flat in oauth:
            target = oauth[flat]
        else:
            target = None
        if target is None:
            manual.append(flat)
            continue
        if target.exists() and not force:
            plan.append((flat, target, "kept (already exists)"))
        else:
            plan.append((flat, target, "would place" if dry_run else "placed"))
    for flat, target, action in plan:
        where = str(target.relative_to(repo)) if target is not None and repo.resolve() in target.resolve().parents else str(target)
        print(f"  {action:28s} {flat}" + (f" -> {where}" if target is not None else ""))
        if action == "placed":
            _write_private(target, members[flat])
    for flat in manual:
        print(f"  {'not placed':28s} {flat} (no known destination: copy it by hand, or its *_OAUTH_FILE path is not under your home)")
    placed = sum(1 for _f, _t, a in plan if a in ("placed", "would place"))
    kept = sum(1 for _f, _t, a in plan if a.startswith("kept"))
    verb = "would place" if dry_run else "placed"
    print(f"{verb} {placed} file(s), kept {kept} existing, {len(manual)} to place by hand. Modes are 0600; never commit any of it.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", metavar="ARCHIVE")
    ap.add_argument("--restore", nargs=2, metavar=("ARCHIVE", "TO_DIR"), default=None)
    ap.add_argument("--install", nargs=2, metavar=("ARCHIVE", "REPO"), default=None,
                    help="place an archive's members in a fresh checkout (see the docstring)")
    ap.add_argument("--dry-run", action="store_true", help="with --install: show the plan, write nothing")
    ap.add_argument("--force", action="store_true", help="with --install: replace files that already exist")
    ap.add_argument("--with-state", action="store_true", help="with --install: also place decision log, breaker and peak queue")
    args = ap.parse_args()
    if args.list:
        do_list(args.list)
    elif args.restore:
        do_restore(*args.restore)
    elif args.install:
        sys.exit(do_install(args.install[0], args.install[1], force=args.force, dry_run=args.dry_run, with_state=args.with_state))
    else:
        run_backup()


if __name__ == "__main__":
    main()
