r"""Keep the README's facts true without anyone remembering to edit them.

WHY THIS EXISTS. The Status section said "Version 1.0.6" while the build was
1.11.1 - ten releases stale - and the headline said 39,556 files and 2.36 TB
saved while the pool held 39,590 and 2.49 TB. Neither was wrong when it was
written. That is the whole problem with a hand-maintained fact: it is correct
once, and then it quietly stops being, and the person reading it has no way to
know which.

TWO KINDS OF FIX, and the difference matters.

Anything GitHub already knows - the newest release, when it was published, the
licence, when the last commit landed - becomes a badge. A badge is not a copy
of the fact, it is a window onto it: nothing has to run for it to stay right,
including this script.

Anything only this machine knows - how many files are in the library, how much
has been saved - is rewritten between markers by this script, from the running
instance rather than from memory. It is run as part of cutting a release, so
the numbers are as old as the release and no older.

Usage:  python tools\update_readme.py [--repo PATH] [--check]
        --check reports what would change and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = r"P:\BackUp Data\Home_Assistant_Backup\Claude\nuarr-repo"
API = "http://127.0.0.1:8770"

# Content between these is generated. Everything outside is written by a human
# and is never touched - the markers are what make it safe to run this on a
# file somebody else is also editing.
BEGIN = "<!-- nuarr:stats -->"
END = "<!-- /nuarr:stats -->"


def version() -> str:
    src = os.path.join(os.path.dirname(HERE), "app", "version.py")
    try:
        with open(src, encoding="utf-8") as fh:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def library() -> dict:
    """Ask the running instance. Absent numbers are left out, never guessed."""
    try:
        with urllib.request.urlopen(f"{API}/api/summary", timeout=20) as r:
            s = json.load(r)
    except Exception:                                        # noqa: BLE001
        return {}
    tot = s.get("totals") or {}
    saved = s.get("saved") or {}
    return {"files": tot.get("n") or 0,
            "bytes": tot.get("bytes") or 0,
            "saved": saved.get("net") or 0,
            "before": saved.get("before_b") or 0}


def tb(n: int) -> str:
    r"""The app's own unit, to the app's own precision.

    1024-based and labelled TB, because that is exactly what the header does -
    and a README that says 66.73 TB beside a dashboard saying 60.69 TB makes a
    reader doubt both. Matching a convention beats being right in isolation.
    """
    return f"{n / 1_099_511_627_776:.2f} TB"


def block(v: str, lib: dict) -> str:
    lines = []
    if lib.get("files"):
        # The header's ratio: what was saved against what those files weighed
        # BEFORE - not against the library total, which would answer a
        # different question and print a different number.
        pct = (100.0 * lib["saved"] / lib["before"]) if lib["before"] else 0.0
        lines.append(
            f"Running against a 12-disk pool: **{lib['files']:,} files, "
            f"{tb(lib['bytes'])}, {tb(lib['saved'])} saved ({pct:.1f}%).**")
    if v:
        lines.append("")
        lines.append(f"<sub>Figures from the {v} build. The badges above come "
                     f"straight from GitHub and are always current.</sub>")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    path = os.path.join(a.repo, "README.md")
    if not os.path.exists(path):
        print(f"no README at {path}")
        return 1
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if BEGIN not in text or END not in text:
        print(f"markers not found - add {BEGIN} / {END} around the stats line")
        return 2

    v, lib = version(), library()
    if not lib:
        print("nuarr is not answering on 8770; leaving the numbers alone")
        return 3
    new = f"{BEGIN}\n{block(v, lib)}\n{END}"
    out = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), new, text,
                 flags=re.S)
    if out == text:
        print("already current")
        return 0
    print(block(v, lib))
    if a.check:
        print("\n(--check: nothing written)")
        return 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"\nupdated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
