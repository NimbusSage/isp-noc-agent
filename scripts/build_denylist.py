#!/usr/bin/env python3
"""build_denylist.py - build scripts/leak_denylist.sha256 from private word lists.

The word lists (org name, towns, tower/site names, domains, device-name stems,
people, ...) must live OUTSIDE this repository; only their sha256 hashes are
written here. This script never writes, prints or logs a plaintext word.

  build_denylist.py LIST [LIST ...] [--out FILE]
        one entry per line; blank lines and lines starting with "#" are ignored.
        An entry is lower-cased and reduced to its [A-Za-z0-9]+ tokens:
          - one token   -> hashed on its own (skipped when shorter than 4 chars)
          - several     -> hashed as their concatenation ("Blue Harbor" -> "blueharbor"),
                           so it matches "Blue Harbor", "blue_harbor", "BlueHarbor", "blue-harbor".
                           List the words separately as well if each alone is a leak.
  build_denylist.py --check WORD [--denylist FILE]
        exit 0 if WORD (normalised the same way) is in the list, 1 if not.
  build_denylist.py --verify LIST [--denylist FILE]
        exit 0 if every usable entry of LIST is already in the list, else 1
        (prints only counts).

The scanner (scripts/secret_scan.py) normalises text the same way, so a hash
here matches the same text there. Unsalted sha256 is a tripwire for careless
commits, not secrecy: a word list of town names can be brute-forced from the
file, so keep the list's *meaning* (what the entries are) out of the repo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_scan  # noqa: E402

HEADER = """\
# NocAgent leak deny-list: sha256 of lower-cased [A-Za-z0-9]+ tokens (see scripts/secret_scan.py).
# One 64-hex digest per line; "#" starts a comment. NO plaintext lives here or anywhere in this repo.
# Regenerate with: python3 scripts/build_denylist.py <private word lists outside the repo>
# Entries: {count}
"""


def read_entries(paths: list[Path]) -> tuple[set[str], int]:
    """Return (usable keys, skipped-too-short count). Never returns the raw lines."""
    keys: set[str] = set()
    skipped = 0
    for p in paths:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key = secret_scan.phrase_key(line)
            if len(key) < secret_scan.MIN_TOKEN:
                skipped += 1
                continue
            keys.add(key)
    return keys, skipped


def load_hashes(path: Path) -> set[str]:
    hashes, _real, _err = secret_scan._load_denylist(path)
    return hashes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lists", nargs="*", help="plaintext word-list files (outside the repo)")
    ap.add_argument("--out", default=str(secret_scan.DEFAULT_DENYLIST), help="output file")
    ap.add_argument("--denylist", default=str(secret_scan.DEFAULT_DENYLIST), help="list used by --check/--verify")
    ap.add_argument("--check", metavar="WORD", help="test one word against the list")
    ap.add_argument("--verify", metavar="LIST", help="test a whole word-list file against the list")
    ap.add_argument("--allow-in-repo", action="store_true",
                    help="permit a word list inside the repo tree (it would then be committed in plaintext)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0

    if args.check is not None:
        key = secret_scan.phrase_key(args.check)
        if len(key) < secret_scan.MIN_TOKEN:
            print(f"too short to ever match (< {secret_scan.MIN_TOKEN} chars after normalising)", file=sys.stderr)
            return 1
        present = secret_scan.hash_token(key) in load_hashes(Path(args.denylist))
        print("present" if present else "absent")
        return 0 if present else 1

    if args.verify is not None:
        keys, skipped = read_entries([Path(args.verify)])
        have = load_hashes(Path(args.denylist))
        missing = sum(1 for k in keys if secret_scan.hash_token(k) not in have)
        print(f"{len(keys)} usable entries, {missing} missing from the list, {skipped} too short")
        return 0 if missing == 0 else 1

    if not args.lists:
        ap.print_usage(sys.stderr)
        print("build_denylist: give at least one word-list file (or --check / --verify)", file=sys.stderr)
        return 2
    paths = [Path(p) for p in args.lists]
    for p in paths:
        if not p.is_file():
            print(f"build_denylist: not a file: {p}", file=sys.stderr)
            return 2
        if not args.allow_in_repo and secret_scan.ROOT.resolve() in p.resolve().parents:
            print(f"build_denylist: refusing a word list inside the repo ({p.name}); keep plaintext outside",
                  file=sys.stderr)
            return 2
    keys, skipped = read_entries(paths)
    if not keys:
        print("build_denylist: no usable entries (all blank, comments or shorter than 4 chars)", file=sys.stderr)
        return 2
    digests = sorted({secret_scan.hash_token(k) for k in keys})
    out = Path(args.out)
    out.write_text(HEADER.format(count=len(digests)) + "\n".join(digests) + "\n", encoding="utf-8")
    print(f"wrote {len(digests)} hashes to {out} ({skipped} entries shorter than {secret_scan.MIN_TOKEN} chars skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
