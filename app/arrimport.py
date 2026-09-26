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
# A compiled client, not the Python one: the arrs start it once per file and
# python.exe took ~1 s to start on this box - longer than copying a 200 MB
# episode. arr_import.exe starts in ~140 ms. arr_import.cmd/.py stay as the
# readable reference and a fallback.
SCRIPT = r"C:\nuarr\arr_import.exe"
_STATS: dict = {"placed": 0, "deferred": 0, "last": None}
# What is being copied right now - one entry per import in flight, keyed by a
# token, so the panel can draw a bar per file with its own speed.
CURRENT: dict = {}
_LOCK = threading.Lock()
RECENT_MAX = 20
_ARRS: dict = {"at": 0.0, "rows": []}
# A SEASON PACK IS ONE THING TO WATCH, NOT FORTY. The arrs import a pack one
# file at a time and each 200 MB episode is over in half a second, so a
# per-file bar is a flicker. Files from the same source folder within a few
# minutes of each other are one batch: how many of how many, how many GB,
# the running speed and what is left - counted from the folder itself, so a
# pack whose files are moved out as they import still knows its total.
BATCHES: dict = {}
BATCH_GAP_S = 180.0
KEEP_DONE_S = 2.5


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
    _t = {"in": time.time()}
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
        root, label, why = _choose(dst, size)
        _t["chosen"] = time.time()
    except Exception as e:                                   # noqa: BLE001
        return _defer(f"placement failed: {type(e).__name__}", src, arr)
    if not root or not label:
        return _defer(why or "no disk chosen", src, arr)
    land = fileops.landing_dir(dst, label)
    if not land:
        return _defer(f"no landing folder for {label}", src, arr)
    _batch_start(src, arr)
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
    # Kept on screen a moment at 100%: a half-second copy that vanishes
    # before the page next asks was never seen at all.
    CURRENT[tok].update(bytes=size, total=size, done=True, finished=time.time())
    # A MOVE MEANS THE SOURCE IS GONE AFTERWARDS - that is what the arr would
    # have done. Copy and the hardlink modes keep it (a torrent still seeding).
    if (mode or "").lower() == "move":
        try:
            os.remove(src)
        except OSError:
            pass
    took = time.time() - t0
    _t["out"] = time.time()
    _batch_done(src, os.path.basename(dst), size, label)
    _STATS["placed"] += 1
    _STATS["last"] = {"at": time.time(), "file": os.path.basename(dst), "placed": label,
                      "why": why, "arr": arr, "mb_s": round(size / 2**20 / max(took, 0.1))}
    # THE ARR IS ANSWERED THE MOMENT THE FILE IS IN PLACE. The bookkeeping -
    # the recent list, the files row, the log - is database writes, and on a
    # busy server each can wait on a lock; the arr imports the next file of
    # the pack while they happen.
    threading.Thread(target=_after, args=(dst, label, size, took, why, arr),
                     daemon=True).start()
    return {"ok": True, "placed_on": label, "why": why, "seconds": round(took, 1),
            "timing": {"choose": round(_t.get("chosen", _t["in"]) - _t["in"], 3),
                       "copy": round(took, 3),
                       "server": round(_t["out"] - _t["in"], 3)}}


# ONE DECISION PER PACK, NOT ONE PER FILE. placement.choose stats every
# member, asks which disks Plex and the queue are using and reads DrivePool's
# balancer state - 0.15 s on a quiet box, and 0.5-2.4 s measured inside a busy
# server, which on a 200 MB episode is longer than the copy. Placing one
# episode does not change which disk is emptiest by percent, so for the next
# few seconds the answer is reused, as long as the disk still has room.
_CHOICE: dict = {"at": 0.0, "root": None, "label": "", "why": ""}
CHOICE_TTL_S = 15.0


def _choose(dst: str, size: int):
    from . import placement
    import shutil as _sh
    c = _CHOICE
    if c["root"] and time.time() - c["at"] < CHOICE_TTL_S and size < 20 * 2**30:
        try:
            if _sh.disk_usage(c["root"]).free > size + 50 * 2**30:
                return c["root"], c["label"], c["why"]
        except OSError:
            pass
    root, label, why = placement.choose(dst, size)
    if root:
        c.update(at=time.time(), root=root, label=label, why=why)
    return root, label, why


# ---- a door of its own ------------------------------------------------------
# The arrs' client used to POST to the web server, and the request waited its
# turn on nuarr's event loop behind every page poll: measured 1.2-3 s per file
# before place() even started. This is a plain threaded HTTP listener on
# 127.0.0.1:8771 that calls place() directly - no event loop in the way. The
# web route stays as the fallback the client uses if this port does not answer.
DIRECT_PORT = 8771
_SRV: dict = {"srv": None}


def start_direct() -> None:
    if _SRV["srv"] is not None:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):                          # quiet
            pass

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n) or b"{}")
                r = place(str(b.get("src") or ""), str(b.get("dst") or ""),
                          str(b.get("mode") or ""), str(b.get("arr") or ""))
            except Exception as e:                           # noqa: BLE001
                r = {"ok": False, "defer": True, "why": f"{type(e).__name__}: {e}"[:160]}
            out = json.dumps(r).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", DIRECT_PORT), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="arrimport-direct",
                         daemon=True).start()
        _SRV["srv"] = srv
    except OSError as e:
        joblog.log(f"arr import listener could not open port {DIRECT_PORT}: {e} - "
                   f"imports go through the web server instead", "warn")


def _after(dst, label, size, took, why, arr) -> None:
    from . import placement
    m = re.search(r"at (\d+)% \((\d+) GB free\)", why or "")
    try:
        _remember({"at": time.time(), "file": os.path.basename(dst), "arr": arr,
                   "placed": label, "gb": round(size / 2**30, 2), "seconds": round(took, 1),
                   "pct": int(m.group(1)) if m else None,
                   "free_gb": int(m.group(2)) if m else None, "why": why})
    except Exception:                                        # noqa: BLE001
        pass
    try:
        placement.landed(dst, label, True)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        joblog.log(f"{arr or 'arr'} import placed on {label} through the pool "
                   f"({size / 2**30:.2f} GB in {took:.1f}s): {os.path.basename(dst)}",
                   "info")
    except Exception:                                        # noqa: BLE001
        pass


def _videos_in(d: str) -> dict:
    out = {}
    try:
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.splitext(f)[1].lower() in VIDEO_EXT and os.path.isfile(p):
                out[f] = os.path.getsize(p)
    except OSError:
        pass
    return out


def _batch_start(src: str, arr: str) -> None:
    key = os.path.dirname(os.path.abspath(src)).lower()
    now = time.time()
    with _LOCK:
        b = BATCHES.get(key)
        # a finished batch that gets another file is a NEW batch, not file 29
        # of a pack that ended a minute ago
        if (not b or now - b["last_at"] > BATCH_GAP_S
                or (b["done"] >= b["files"] and os.path.basename(src) not in b["seen"])):
            b = BATCHES[key] = {"dir": os.path.basename(os.path.dirname(src)) or src,
                                "arr": arr, "started": now, "last_at": now, "done": 0,
                                "bytes_done": 0, "files": 0, "bytes": 0, "seen": set(),
                                "disks": {}, "last_file": ""}
        left = {f: z for f, z in _videos_in(os.path.dirname(src)).items()
                if f not in b["seen"]}
        b["files"] = max(b["files"], b["done"] + len(left))
        b["bytes"] = max(b["bytes"], b["bytes_done"] + sum(left.values()))
        b["last_at"] = now


def _batch_done(src: str, name: str, size: int, label: str) -> None:
    key = os.path.dirname(os.path.abspath(src)).lower()
    with _LOCK:
        b = BATCHES.get(key)
        if not b:
            return
        b["seen"].add(os.path.basename(src))
        b["done"] += 1
        b["bytes_done"] += size
        b["last_at"] = time.time()
        b["last_file"] = name
        b["disks"][label] = b["disks"].get(label, 0) + 1


def status(fresh: bool = False) -> dict:
    now = time.time()
    for k, v in list(CURRENT.items()):
        if v.get("done") and now - v.get("finished", now) > KEEP_DONE_S:
            CURRENT.pop(k, None)
    batches = []
    with _LOCK:
        for k, b in list(BATCHES.items()):
            if now - b["last_at"] > BATCH_GAP_S:
                BATCHES.pop(k, None)
                continue
            if b["files"] < 2:
                continue                      # a single file is not a batch
            el = max(0.1, b["last_at"] - b["started"])
            rate = b["bytes_done"] / el if b["done"] else 0.0
            left = max(0, b["bytes"] - b["bytes_done"])
            batches.append({"dir": b["dir"], "arr": b["arr"], "done": b["done"],
                            "files": b["files"], "bytes_done": b["bytes_done"],
                            "bytes": b["bytes"], "bps": rate, "elapsed": round(now - b["started"]),
                            "eta": (left / rate) if rate and left else 0,
                            "idle": round(now - b["last_at"]), "last_file": b["last_file"],
                            "disks": dict(b["disks"]),
                            "complete": b["done"] >= b["files"]})
    return {"on": enabled(), "script": SCRIPT, "arrs": arr_state(fresh),
            "placed": _STATS["placed"], "deferred": _STATS["deferred"],
            "current": [dict(v, elapsed=round(now - v["started"], 1))
                        for v in list(CURRENT.values())],
            "batches": batches, "recent": _recent_load()[:8]}
