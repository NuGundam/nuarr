r"""Change tracking for the libraries nuarr manages - so it rescans what
changed, not everything, every few hours.

WHY THIS EXISTS
---------------
A full scan walks every pool disk (39,000 files, minutes) and asks both arrs
for everything they know, then reconciles. That is the right thing to do
once in a while: it is the only pass that notices a file DrivePool moved to
another spindle, a file that appeared without an arr import, or one that
vanished. But it ran every few hours to catch changes that are rare and
local, and between runs the dashboard could be wrong about a file for hours
- the "no arr tracks" tile in particular is only ever corrected by a scan.

Windows will simply say what changed. ReadDirectoryChangesW on each disk's
PoolPart folder (the real NTFS volumes underneath the pool, not the virtual
P:\, so DrivePool's own moves between disks are seen too) delivers every
create, delete, rename and write as it happens. This module keeps one such
watcher per pool disk, folds the events into "which library changed", waits
for the writes to settle, and asks for a rescan of just that library - which
takes seconds. The scheduled full scan then only needs to run as a backstop.

WHAT A WATCHER CANNOT DO. The kernel buffer is finite; if events arrive
faster than they are read (a balance moving thousands of files while the
service is busy), the OS reports an overflow and DISCARDS the backlog.
That is detected and answered honestly: the library set is unknown, so the
next full scan is brought forward. Nothing here ever claims to have seen a
change it missed.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import threading
import time
from collections import deque
from ctypes import wintypes as wt

from . import joblog
from .db import kv_get

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x0007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_NOTIFY_CHANGE_FILE_NAME = 0x0001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x0002
FILE_NOTIFY_CHANGE_SIZE = 0x0008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x0010
ERROR_NOTIFY_ENUM_DIR = 1022
ACTIONS = {1: "added", 2: "removed", 3: "modified", 4: "renamed from", 5: "renamed to"}

_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, ctypes.c_void_p]
_k32.ReadDirectoryChangesW.restype = wt.BOOL
_k32.ReadDirectoryChangesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                                       wt.BOOL, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                       ctypes.c_void_p, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

DEFAULTS = {"changewatch.enabled": "1",
            # while tracking is healthy the FULL scan only needs to be a
            # backstop; this replaces scan_every_min as its interval
            "changewatch.full_every_min": "720"}
SETTLE_S = 45.0          # quiet time after the last event before a rescan
MIN_GAP_S = 300.0        # never rescan the same library more often than this
BUF_BYTES = 256 * 1024

STATE: dict = {
    "watchers": {},       # disk label -> {"alive": bool, "events": int, "error": str}
    "pending": {},        # library -> last event time
    "rescans": {},        # library -> last rescan time
    "overflow_at": 0.0,   # last time a watcher lost its backlog
    "events_today": 0, "day": 0,
    "recent": deque(maxlen=60),
    "started_at": 0.0,
}
RESCAN = None            # set by web.py: async def (library: str) -> None
_lock = threading.Lock()


def enabled() -> bool:
    v = kv_get("changewatch.enabled")
    return str(v if v is not None else DEFAULTS["changewatch.enabled"]) == "1"


def full_every_min() -> int:
    try:
        return max(60, int(kv_get("changewatch.full_every_min")
                           or DEFAULTS["changewatch.full_every_min"]))
    except (TypeError, ValueError):
        return 720


def healthy() -> bool:
    """Every pool disk watched, none of them recently overflowed."""
    w = STATE["watchers"]
    if not w or not enabled():
        return False
    if any(not x.get("alive") for x in w.values()):
        return False
    return time.time() - STATE["overflow_at"] > 3600


def _note(disk: str, action: str, pool_path: str, library: str | None) -> None:
    now = time.time()
    with _lock:
        day = int(now // 86400)
        if day != STATE["day"]:
            STATE["day"], STATE["events_today"] = day, 0
        STATE["events_today"] += 1
        STATE["watchers"][disk]["events"] = STATE["watchers"][disk].get("events", 0) + 1
        STATE["recent"].append({"at": now, "disk": disk, "action": action,
                                "path": pool_path, "library": library or ""})
        if library:
            STATE["pending"][library] = now


def _watch_thread(disk: str, part: str, pool_root: str) -> None:
    from . import scanner
    st = STATE["watchers"][disk]
    h = _k32.CreateFileW(part, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                         OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if h == INVALID_HANDLE_VALUE or not h:
        st.update(alive=False, error=f"could not open {part}: {ctypes.get_last_error()}")
        return
    buf = ctypes.create_string_buffer(BUF_BYTES)
    got = wt.DWORD(0)
    flt = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME
           | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE)
    st.update(alive=True, error="")
    try:
        while not STATE.get("stop"):
            ok = _k32.ReadDirectoryChangesW(h, buf, BUF_BYTES, True, flt,
                                            ctypes.byref(got), None, None)
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_NOTIFY_ENUM_DIR:
                    STATE["overflow_at"] = time.time()
                    joblog.log(f"change tracking: {disk} produced changes faster than "
                               f"they could be read - the backlog was lost, so the next "
                               f"full scan is brought forward", "warn")
                    continue
                st.update(alive=False, error=f"ReadDirectoryChangesW failed: {err}")
                return
            if got.value == 0:
                # buffer too small for the burst - same answer as an overflow
                STATE["overflow_at"] = time.time()
                continue
            off = 0
            while True:
                nxt, action, nlen = ctypes.cast(
                    ctypes.addressof(buf) + off, ctypes.POINTER(wt.DWORD * 3)).contents
                name = ctypes.wstring_at(ctypes.addressof(buf) + off + 12, nlen // 2)
                ext = os.path.splitext(name)[1].lower()
                if ext in scanner.MEDIA_EXT:
                    pool_path = os.path.join(pool_root, name)
                    try:
                        lib = scanner._library_of(pool_path)
                        if lib == scanner.OUTSIDE:
                            lib = None
                    except Exception:                        # noqa: BLE001
                        lib = None
                    _note(disk, ACTIONS.get(action, str(action)), pool_path, lib)
                if not nxt:
                    break
                off += nxt
    finally:
        _k32.CloseHandle(h)
        st["alive"] = False


def start(pool_root: str = "P:\\") -> None:
    """One watcher thread per pool disk. Safe to call again; restarts dead ones."""
    from . import scanner
    if not enabled():
        return
    try:
        parts = scanner.pool_disks()
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"change tracking: could not list pool disks: {e}", "warn")
        return
    STATE["started_at"] = STATE["started_at"] or time.time()
    for disk, part in parts.items():
        cur = STATE["watchers"].get(disk)
        if cur and cur.get("alive"):
            continue
        STATE["watchers"][disk] = {"alive": False, "events": (cur or {}).get("events", 0),
                                   "error": "", "part": part}
        threading.Thread(target=_watch_thread, args=(disk, part, pool_root),
                         name=f"changewatch-{disk}", daemon=True).start()


def status() -> dict:
    with _lock:
        w = {k: dict(v) for k, v in STATE["watchers"].items()}
        return {"enabled": enabled(), "healthy": healthy(), "watchers": w,
                "pending": dict(STATE["pending"]), "rescans": dict(STATE["rescans"]),
                "overflow_at": STATE["overflow_at"], "events_today": STATE["events_today"],
                "recent": list(STATE["recent"])[-30:], "full_every_min": full_every_min(),
                "settle_s": SETTLE_S, "min_gap_s": MIN_GAP_S}


async def watch() -> None:
    """Fold settled changes into per-library rescans."""
    from . import schedules
    schedules.register(
        "changewatch", "Change tracking", "Library", 15,
        what="Watches every pool disk for files added, removed, renamed or "
             "rewritten and rescans just the library that changed once the "
             "writes settle - so the full scan can run as a backstop rather "
             "than the only way to notice anything.")
    await asyncio.sleep(25)
    start()
    while True:
        schedules.beat("changewatch")
        try:
            if enabled():
                start()                                      # revive dead watchers
                now = time.time()
                due = []
                with _lock:
                    for lib, at in list(STATE["pending"].items()):
                        if now - at < SETTLE_S:
                            continue
                        if now - STATE["rescans"].get(lib, 0.0) < MIN_GAP_S:
                            continue
                        due.append(lib)
                        STATE["pending"].pop(lib, None)
                for lib in due:
                    n = sum(1 for r in STATE["recent"] if r["library"] == lib
                            and r["at"] > STATE["rescans"].get(lib, 0.0))
                    STATE["rescans"][lib] = time.time()
                    joblog.log(f"change tracking: {n} change(s) settled in {lib} - "
                               f"rescanning that library", "info")
                    if RESCAN is not None:
                        try:
                            await RESCAN(lib)
                        except Exception as e:               # noqa: BLE001
                            joblog.log(f"change tracking: rescan of {lib} failed: "
                                       f"{type(e).__name__}: {e}", "warn")
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"change tracking: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(15)
