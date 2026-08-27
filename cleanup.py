"""
nuarr - library cleanup

Finds two kinds of junk the scan turned up:

  1. TDARR CACHE LEFTOVERS - '-TdarrCacheFile-<id>' files abandoned inside the
     library folders when a job died mid-write. ~150 GB here.
  2. SMALL ORPHANS - tiny media files the arrs know nothing about: samples,
     trailers, stray extras, truncated downloads.

SAFETY
------
* Dry run by default. Nothing is removed without --delete.
* Removal goes to the RECYCLE BIN, not a permanent delete, so a mistake is
  recoverable from Explorer.
* Anything Sonarr or Radarr has a record of is EXCLUDED, always - being managed
  is disqualifying no matter how small or how odd the name.
* --max-size guards against a bad filter nominating a 27 GB remux.

Usage
-----
    python cleanup.py                          report only (default)
    python cleanup.py --cache                  only Tdarr cache leftovers
    python cleanup.py --small --under 100      orphans under 100 MB
    python cleanup.py --cache --delete         send cache files to Recycle Bin
    python cleanup.py --small --under 50 --delete
"""
from __future__ import annotations

import argparse
import os
import sys

from app.db import cursor, log_event


def _gb(b: float) -> str:
    return f"{b / 1024 ** 3:.2f} GB" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.1f} MB"


def find_cache() -> list[dict]:
    """Tdarr cache leftovers sitting in library folders."""
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT id,path,size,arr_file_id,library FROM files "
            "WHERE path LIKE '%TdarrCacheFile%' AND state!='deleted' "
            "ORDER BY size DESC")]


def find_small_orphans(under_mb: int) -> list[dict]:
    """Small files the arrs have no record of."""
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT id,path,size,arr_file_id,library FROM files "
            "WHERE arr_file_id IS NULL AND state!='deleted' "
            "AND size IS NOT NULL AND size < ? ORDER BY size DESC",
            (under_mb * 1024 * 1024,))]


SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}


def find_sub_litter() -> list[dict]:
    """Subtitle sidecars carrying a TdarrCacheFile name.

    Found by walking the libraries, not the database: the scanner only indexes
    video, so these were invisible to it. They are left behind when a transcode
    dies mid-job, and several are 0 bytes - which Plex will happily present as a
    real subtitle track that shows nothing.
    """
    from app.config import SETTINGS

    out: list[dict] = []
    for lib in SETTINGS.libraries:
        if not lib.enabled or not os.path.isdir(lib.path):
            continue
        for dirpath, _d, files in os.walk(lib.path):
            for fn in files:
                if "tdarrcachefile" not in fn.lower():
                    continue
                if os.path.splitext(fn)[1].lower() not in SUB_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                out.append({"id": None, "path": p, "size": sz,
                            "arr_file_id": None, "library": lib.name})
    return sorted(out, key=lambda r: -r["size"])


def recycle(path: str) -> tuple[bool, str]:
    """Send one file to the Recycle Bin via the Windows shell."""
    try:
        from send2trash import send2trash
    except ImportError:
        return False, "send2trash not installed (pip install send2trash)"
    try:
        send2trash(os.path.abspath(path))
        return True, "recycled"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def report(rows: list[dict], label: str, show: int) -> int:
    total = sum(r["size"] or 0 for r in rows)
    print(f"\n=== {label}: {len(rows)} file(s), {_gb(total)} ===")
    for r in rows[:show]:
        managed = "  [MANAGED BY ARR - will be skipped]" if r["arr_file_id"] else ""
        print(f"  {_gb(r['size'] or 0):>10}  {r['path'][-100:]}{managed}")
    if len(rows) > show:
        print(f"  ... and {len(rows) - show} more")
    return total


def do_delete(rows: list[dict], max_size_gb: float) -> tuple[int, int, list[str]]:
    ok = 0
    freed = 0
    problems: list[str] = []
    for r in rows:
        path, size = r["path"], r["size"] or 0

        if r["arr_file_id"]:
            problems.append(f"SKIP (managed by arr): {path}")
            continue
        if size > max_size_gb * 1024 ** 3:
            problems.append(f"SKIP (over --max-size {max_size_gb} GB): {path}")
            continue
        if not os.path.exists(path):
            problems.append(f"SKIP (already gone): {path}")
            continue

        good, msg = recycle(path)
        if good:
            ok += 1
            freed += size
            if r["id"] is not None:
                with cursor() as cur:
                    cur.execute("UPDATE files SET state='deleted', "
                                "state_reason='recycled by cleanup.py' WHERE id=?",
                                (r["id"],))
                log_event(r["id"], "recycled", path)
        else:
            problems.append(f"FAILED {path}: {msg}")
    return ok, freed, problems


def main() -> None:
    ap = argparse.ArgumentParser(description="Clean Tdarr cache files and small orphans")
    ap.add_argument("--cache", action="store_true", help="Tdarr cache leftovers")
    ap.add_argument("--small", action="store_true", help="small orphan files")
    ap.add_argument("--subs", action="store_true",
                    help="subtitle sidecars named TdarrCacheFile (walks disk)")
    ap.add_argument("--under", type=int, default=100,
                    help="'small' means under this many MB (default 100)")
    ap.add_argument("--delete", action="store_true",
                    help="actually remove (to Recycle Bin)")
    ap.add_argument("--max-size", type=float, default=5.0,
                    help="refuse to remove anything larger, in GB (default 5)")
    ap.add_argument("--show", type=int, default=25)
    args = ap.parse_args()

    if not args.cache and not args.small and not args.subs:
        args.cache = args.small = args.subs = True

    targets: list[dict] = []
    grand = 0
    if args.cache:
        rows = find_cache()
        grand += report(rows, "Tdarr cache leftovers", args.show)
        targets += rows
    if args.small:
        rows = find_small_orphans(args.under)
        grand += report(rows, f"Orphans under {args.under} MB (not in Sonarr/Radarr)",
                        args.show)
        print("  NOTE: on this library these are mostly OP/ED sequences, AMVs,")
        print("        specials and bonus features - real content, not junk.")
        print("        Review before using --small --delete.")
        targets += rows
    if args.subs:
        rows = find_sub_litter()
        grand += report(rows, "Subtitle sidecars named TdarrCacheFile", args.show)
        targets += rows

    print(f"\ntotal identified: {len(targets)} file(s), {_gb(grand)}")

    if not args.delete:
        print("\nDRY RUN - nothing was removed.")
        print("Re-run with --delete to send these to the Recycle Bin.")
        return

    print(f"\nremoving to Recycle Bin (skipping anything over {args.max_size} GB "
          f"or managed by an arr)...")
    ok, freed, problems = do_delete(targets, args.max_size)
    print(f"\nrecycled {ok} file(s), freed {_gb(freed)}")
    if problems:
        print(f"{len(problems)} skipped/failed:")
        for p in problems[:20]:
            print("  " + p)
    print("\nFiles are in the Recycle Bin - restore from there if anything looks wrong.")


if __name__ == "__main__":
    sys.exit(main())
