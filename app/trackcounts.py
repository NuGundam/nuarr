r"""How many audio tracks and subtitle tracks the library holds.

WHY IT IS NOT A COLUMN SUM. The obvious source, files.audio_codecs, is
DEDUPED - a file with two AAC tracks says "aac" - so counting its commas
gives 41,546 against a true 56,485. sub_langs is per track and lands within
a few dozen of the truth, but "within a few dozen" is not a number to put in
a header that otherwise says exactly what it counted.

The truth is in the probes, and walking 39,869 of them takes two seconds -
too slow for a summary that is asked every five, fine once every ten
minutes on a thread nobody waits for. So this is the same shape as
arrtotals: cached() answers instantly with the last count, and refreshes
itself in the background when the answer is old or the file count moved.
"""
from __future__ import annotations

import json
import threading
import time

from .db import cursor

TTL_S = 600.0

_CACHE: dict = {"audio": 0, "subs": 0, "files": 0, "at": 0.0,
                "seen_n": -1, "running": False}
_LOCK = threading.Lock()


def _file_count() -> int:
    try:
        with cursor() as cur:
            return int(cur.execute(
                "SELECT COUNT(*) FROM files WHERE state!='deleted'"
            ).fetchone()[0] or 0)
    except Exception:                                    # noqa: BLE001
        return -1


def _walk() -> None:
    a = s = n = 0
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT p.json FROM files f "
                    "  JOIN file_probes p ON p.file_id=f.id "
                    " WHERE f.state!='deleted'"):
                try:
                    pr = json.loads(r["json"])
                except Exception:                        # noqa: BLE001
                    continue
                n += 1
                for st in pr.get("streams") or []:
                    k = st.get("codec_type")
                    if k == "audio":
                        a += 1
                    elif k == "subtitle":
                        s += 1
        with _LOCK:
            _CACHE.update(audio=a, subs=s, files=n, at=time.time(),
                          seen_n=_file_count())
    except Exception:                                    # noqa: BLE001
        pass
    finally:
        with _LOCK:
            _CACHE["running"] = False


def cached() -> dict:
    """The last count, at once. Starts a refresh if it is due; never waits."""
    now = time.time()
    with _LOCK:
        stale = (now - _CACHE["at"]) > TTL_S
        run = _CACHE["running"]
        out = {"audio": _CACHE["audio"], "subs": _CACHE["subs"],
               "files": _CACHE["files"],
               "age_s": round(now - _CACHE["at"], 1) if _CACHE["at"] else None}
    # THE FILE COUNT MOVING IS ALSO "DUE". A library that just grew by a
    # season should not report last night's track count for ten minutes.
    if not stale and not run:
        try:
            stale = _file_count() != _CACHE["seen_n"]
        except Exception:                                # noqa: BLE001
            stale = False
    if stale and not run:
        with _LOCK:
            if not _CACHE["running"]:
                _CACHE["running"] = True
                threading.Thread(target=_walk, name="trackcounts",
                                 daemon=True).start()
    return out
