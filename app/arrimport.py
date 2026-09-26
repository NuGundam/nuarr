r"""
nuarr - choosing the disk for what the arrs import

WHY
---
Every file Sonarr and Radarr import is a full copy from F:\Ready onto the pool,
and DrivePool puts a new file on the member with the most free BYTES. On a pool
of two 18 TB disks and ten 9-10 TB ones that is always NU-DRIVE-0 or -1:
measured on 2026-09-25, a plain write into Anime Shows, TV Shows and Movies all
landed on NU-DRIVE-1, while every disk sat within half a point of 53% used. So
imports piled onto the two big spindles - the same two nuarr then reads to
rewrite them - and whatever nuarr did not rewrite (about a quarter) waited for
the nightly balance to be copied somewhere else a second time.

HOW
---
Both arrs have "Import Using Script" (Settings > Media Management > Importing):
instead of copying the file themselves they run a script with the source and
the destination, and the script does the transfer. nuarr's script
(C:\nuarr\arr_import.cmd) asks this module to do it the way nuarr commits its
own files: placement.choose picks the emptiest disk by percent that nobody is
watching, the copy is written THROUGH the pool into that disk's pinned landing
folder, and a rename inside the pool puts it at the arr's destination. So the
file lands on the chosen spindle, DrivePool counts it as it lands, and nothing
needs balancing afterwards. The copy was going to happen anyway; this only
decides where.

WHEN IT STEPS ASIDE
-------------------
Anything short of a clean placement answers "defer", and the arr then imports
exactly as it always did - the script can only ever make an import land better,
never stop one:
  * source already on the pool (a move or hardlink there costs nothing, and a
    copy would be all cost);
  * placement switched off, or no disk suitable, or no landing folder;
  * the copy failed to verify - the partial copy is removed first.
"""
from __future__ import annotations

import os
import time

import json
import re
import threading

from . import joblog
from .db import kv_get, kv_set

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".webm"}
SCRIPT = r"C:\nuarr\arr_import.cmd"
_STATS: dict = {"placed": 0, "deferred": 0, "last": None}
# What is being copied right now - one entry per import in flight, keyed by a
# token, so the panel can draw a bar per file with its own speed.
CURRENT: dict = {}
_LOCK = threading.Lock()
RECENT_MAX = 20
_ARRS: dict = {"at": 0.0, "rows": []}


def enabled() -> bool:
    """nuarr's own switch. Off = every call defers, whatever the arrs say."""
    v = kv_get("arrimport.on")
    return v is None or str(v) not in ("0", "false", "")


def _recent_load() -> list:
    try:
        return json.loads(kv_get("arrimport.recent") or "[]")
    except Exception:                                        # noqa: BLE001
        return []


def _remember(row: dict) -> None:
    with _LOCK:
        r = [row] + _recent_load()
        kv_set("arrimport.recent", json.dumps(r[:RECENT_MAX]))


def _arr_call(a, path, method="GET", body=None):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(a.url.rstrip("/") + path, data=data, method=method,
                                 headers={"X-Api-Key": a.api_key,
                                          "Content-Type": "application/json"})
    t = urllib.request.urlopen(req, timeout=20).read().decode()
    return json.loads(t) if t.strip() else {}


def arr_state(fresh: bool = False) -> list:
    """Whether each arr actually has Import Using Script pointed at nuarr."""
    if not fresh and time.time() - _ARRS["at"] < 30 and _ARRS["rows"]:
        return _ARRS["rows"]
    from .config import SETTINGS
    rows = []
    for a in SETTINGS.arrs:
        try:
            mm = _arr_call(a, "/api/v3/config/mediamanagement")
            rows.append({"name": a.name, "on": bool(mm.get("useScriptImport")),
                         "ours": (mm.get("scriptImportPath") or "").lower() == SCRIPT.lower(),
                         "path": mm.get("scriptImportPath") or "", "error": ""})
        except Exception as e:                               # noqa: BLE001
            rows.append({"name": a.name, "on": False, "ours": False, "path": "",
                         "error": f"{type(e).__name__}: {e}"[:120]})
    _ARRS.update(at=time.time(), rows=rows)
    return rows


def set_enabled(on: bool) -> dict:
    """Switch it in nuarr AND in every arr, so off really means the arr copies."""
    from .config import SETTINGS
    kv_set("arrimport.on", "1" if on else "0")
    out = []
    for a in SETTINGS.arrs:
        try:
            mm = _arr_call(a, "/api/v3/config/mediamanagement")
            mm["useScriptImport"] = bool(on)
            if on:
                mm["scriptImportPath"] = SCRIPT
            _arr_call(a, f"/api/v3/config/mediamanagement/{mm.get('id', 1)}", "PUT", mm)
            out.append({"name": a.name, "ok": True})
        except Exception as e:                               # noqa: BLE001
            out.append({"name": a.name, "ok": False, "error": f"{type(e).__name__}: {e}"[:160]})
    try:
        joblog.log("arr imports: nuarr chooses the disk - "
                   + ("ON" if on else "OFF") + " ("
                   + ", ".join(f"{x['name']} {'ok' if x['ok'] else 'failed'}" for x in out)
                   + ")", "info")
    except Exception:                                        # noqa: BLE001
        pass
    arr_state(fresh=True)
    return {"on": on, "arrs": out}


def _defer(why: str, src: str, arr: str) -> dict:
    _STATS["deferred"] += 1
    _STATS["last"] = {"at": time.time(), "file": os.path.basename(src), "placed": "",
                      "why": why, "arr": arr}
    _remember({"at": time.time(), "file": os.path.basename(src), "arr": arr,
               "placed": "", "why": why})
    try:
        joblog.log(f"{arr or 'arr'} import left to the arr ({why}): "
                   f"{os.path.basename(src)}", "debug")
    except Exception:                                        # noqa: BLE001
        pass
    return {"ok": False, "defer": True, "why": why}


def place(src: str, dst: str, mode: str = "", arr: str = "") -> dict:
    """Copy `src` to `dst` on the disk nuarr would choose, or say defer."""
    from . import fileops, placement, scanner
    if not enabled():
        return _defer("switched off in nuarr", src, arr)
    if not src or not dst or not os.path.isfile(src):
        return _defer("source not found", src, arr)
    if os.path.splitext(dst)[1].lower() not in VIDEO_EXT:
        return _defer("not a video file", src, arr)
    sd = os.path.splitdrive(os.path.abspath(src))[0].lower()
    dd = os.path.splitdrive(os.path.abspath(dst))[0].lower()
    if sd == dd:
        return _defer("source is already on the pool", src, arr)
    try:
        size = os.path.getsize(src)
        root, label, why = placement.choose(dst, size)
    except Exception as e:                                   # noqa: BLE001
        return _defer(f"placement failed: {type(e).__name__}", src, arr)
    if not root or not label:
        return _defer(why or "no disk chosen", src, arr)
    land = fileops.landing_dir(dst, label)
    if not land:
        return _defer(f"no landing folder for {label}", src, arr)
    staged = os.path.join(land, f"{os.path.basename(dst)}.{os.getpid()}-"
                                f"{time.time_ns() % 10**9}.nuarr-new")
    t0 = time.time()
    tok = f"{os.getpid()}-{time.time_ns()}"
    CURRENT[tok] = {"file": os.path.basename(dst), "arr": arr, "disk": label,
                    "bytes": 0, "total": size, "started": t0, "bps": 0.0}

    def _prog(c, t):
        el = time.time() - t0
        CURRENT[tok].update(bytes=int(c), total=int(t or size),
                            bps=(c / el) if el > 0.3 else 0.0)
    try:
        fileops.copy_with_progress(src, staged, _prog)
        ok, vwhy = fileops.verify_copy(src, staged)
        if not ok:
            fileops._quiet_remove(staged)
            CURRENT.pop(tok, None)
            return _defer(f"copy did not verify: {vwhy}", src, arr)
        # which member actually has it - the label follows the truth
        try:
            rel = os.path.relpath(staged, os.path.splitdrive(staged)[0] + "\\")
            on = [l for l, r in (scanner.pool_disks() or {}).items()
                  if os.path.exists(os.path.join(r, rel))]
            if on and on != [label]:
                joblog.log(f"landing folder for {label} did not pin: the copy is on "
                           f"{', '.join(on)} - check its DrivePool rule", "warn")
                label = on[0]
        except Exception:                                    # noqa: BLE001
            pass
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(staged, dst)
        if not os.path.exists(dst) or os.path.getsize(dst) != size:
            raise OSError("the file is not at the destination after the rename")
    except Exception as e:                                   # noqa: BLE001
        fileops._quiet_remove(staged)
        CURRENT.pop(tok, None)
        return _defer(f"{type(e).__name__}: {e}"[:160], src, arr)
    finally:
        pass
    CURRENT.pop(tok, None)
    # A MOVE MEANS THE SOURCE IS GONE AFTERWARDS - that is what the arr would
    # have done. Copy and the hardlink modes keep it (a torrent still seeding).
    if (mode or "").lower() == "move":
        try:
            os.remove(src)
        except OSError:
            pass
    took = time.time() - t0
    m = re.search(r"at (\d+)% \((\d+) GB free\)", why or "")
    _remember({"at": time.time(), "file": os.path.basename(dst), "arr": arr,
               "placed": label, "gb": round(size / 2**30, 2), "seconds": round(took, 1),
               "pct": int(m.group(1)) if m else None,
               "free_gb": int(m.group(2)) if m else None, "why": why})
    _STATS["placed"] += 1
    _STATS["last"] = {"at": time.time(), "file": os.path.basename(dst), "placed": label,
                      "why": why, "arr": arr, "mb_s": round(size / 2**20 / max(took, 0.1))}
    try:
        placement.landed(dst, label, True)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        joblog.log(f"{arr or 'arr'} import placed on {label} through the pool "
                   f"({size / 2**30:.1f} GB in {took:.0f}s): {os.path.basename(dst)}",
                   "info")
    except Exception:                                        # noqa: BLE001
        pass
    return {"ok": True, "placed_on": label, "why": why, "seconds": round(took, 1)}


def status(fresh: bool = False) -> dict:
    now = time.time()
    return {"on": enabled(), "script": SCRIPT, "arrs": arr_state(fresh),
            "placed": _STATS["placed"], "deferred": _STATS["deferred"],
            "current": [dict(v, elapsed=round(now - v["started"], 1))
                        for v in list(CURRENT.values())],
            "recent": _recent_load()[:8]}
