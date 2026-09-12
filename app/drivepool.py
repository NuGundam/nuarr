r"""StableBit DrivePool - what it is doing, and what nuarr holds while it does.

WHY THIS EXISTS
---------------
DrivePool moves files between spindles on its own clock: balancing, duplication,
evacuating a disk being removed. Every one of those is a file being read from one
disk and written to another *underneath* whatever else has it open. nuarr's own
file mover found the race the expensive way - a commit landing on a file the
balancer was mid-move - and DrivePool's log shows the mirror image: sixteen
"Error moving ... Object Name not found" today, the balancer reaching for files
nuarr had just replaced.

The generic disk-load check (diskload.py) sees a balance as load and steers new
work around the busy spindles, which is right for throughput and says nothing
about the race. This is the product-specific half, put back deliberately: it
knows *what* DrivePool is doing, not merely that a disk is busy, and it lets the
two things nuarr does that can corrupt a move - replacing a file in place, and
asking an arr to rename one - wait until the move is over. Each hold is a switch
on the DrivePool page, like the arr renaming/import holds.

WHERE THE FACTS COME FROM
-------------------------
The service log, C:\ProgramData\StableBit DrivePool\Service\Logs\Service\
DrivePool.Service-YYYY-MM-DD.log. DrivePool writes its own UI state there as
notifications - the same flags its status bar shows:

    [CloudNotifications] Set Balancing (ValueKey=...~0.2696)   <- ratio balanced
    [CloudNotifications] Clear Balancing
    Set/Clear Duplicating, Measuring, RemovingPoolPart, MissingDisk,
    PoolPartBusy, DuplicationFileInUse, ...
    [FileMover] Error moving '<path>'. (0xC0000034) - Object Name not found.
    [Rebalance] Balancing pool parts...

There is no API. dpcmd can list pools and set duplication but cannot say "I am
balancing right now". The log can, to the second, and it is the same fact the
DrivePool window is showing. The service process's own I/O rate (psutil) is
read alongside as the "how hard" number - a balance is the service reading and
writing at the same rate - and as the fallback when the log is unreadable.

A pool without DrivePool - or a box where the log is elsewhere - simply reports
"not installed" and every hold is a no-op. Nothing here can stall a machine
that has nothing to wait for.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import subprocess
import time

from . import joblog
from .config import NO_WINDOW
from .db import cursor, kv_get, kv_set

LOG_DIR = r"C:\ProgramData\StableBit DrivePool\Service\Logs\Service"
EXE = r"C:\Program Files\StableBit\DrivePool\DrivePool.Service.exe"
POLL_S = 5.0
KEEP_EVENTS = 200

# The notifications that mean "DrivePool is moving data". Measuring reads
# only; PoolPartBusy flickers on every file placement and is not a move.
ACTIVITIES = {
    "Balancing": "balancing",
    "Duplicating": "duplicating",
    "RemovingPoolPart": "removing",
}
LABEL = {"balancing": "balancing", "duplicating": "duplicating",
         "removing": "removing a disk"}
# Other flags worth showing on the page, without holding anything.
FLAGS = ("Measuring", "MissingDisk", "PoolPartBusy", "DuplicationFileInUse",
         "DuplicationDiskFull", "DuplicationNotEnoughDisks", "UpdateAvailable")

# What nuarr may hold, per activity. Defaults: the two operations that can
# corrupt a move wait; new jobs keep going (the dispatcher already steers off
# busy spindles); a disk being removed holds everything, because every file
# on it is on the move and nuarr cannot know which.
DEFAULTS = {
    "drivepool.enabled": "1",
    # ASK IT TO STEP ASIDE RATHER THAN WAITING IT OUT. Off by default because
    # it reaches into another program's processes, and because the trade is
    # real: the balance takes longer. See set_priority for why it is safe -
    # the pool's data path is the kernel driver, not this service - and why
    # dpcmd cannot do it.
    "drivepool.yield": "0",
    "drivepool.balancing.jobs": "0",
    "drivepool.balancing.commits": "1",
    "drivepool.balancing.renames": "1",
    "drivepool.duplicating.jobs": "0",
    "drivepool.duplicating.commits": "1",
    "drivepool.duplicating.renames": "1",
    "drivepool.removing.jobs": "1",
    "drivepool.removing.commits": "1",
    "drivepool.removing.renames": "1",
}
HOLDS = ("jobs", "commits", "renames")

_LINE = re.compile(
    r"^DrivePool\.Service\.exe\t(\w+)\t\d+\t(.*?)\t(\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ)\t")
_NOTE = re.compile(r"\[CloudNotifications\] (Set|Clear) (\w+)(?: \(ValueKey=[^)]*~([\d.]+)\))?")
_MOVE_ERR = re.compile(r"\[FileMover\] Error moving '(.+?)'\.\s*(.*)$")

STATE: dict = {
    "installed": False, "running": False, "version": "", "log_ok": False,
    "log_file": "", "log_at": 0.0,
    "active": {},              # activity -> {"since": ts, "ratio": float|None}
    "flags": {},               # flag -> since ts
    "rate_bps": 0.0, "read_bps": 0.0, "write_bps": 0.0,
    "mover_errors": [],        # today's, newest last: {at, path, why}
    "last_error": "",
}
_TAIL = {"file": "", "pos": 0}
_IO = {"at": 0.0, "r": 0, "w": 0}

# ------------------------------------------------- asking it to step aside ---
#
# CAN DRIVEPOOL'S PRIORITY BE CHANGED IN REAL TIME FROM A COMMAND LINE? Not by
# dpcmd. Checked against the copy on this box - 2.3.13.1687 - and its entire
# command set is pool structure and duplication: add-poolpart,
# check-pool-fileparts, get/set-duplication, list-open-files, list-pools,
# remeasure-pool, refresh-all-poolparts, ignore/unignore-poolpart. There is no
# priority, no pause, no throttle. DrivePool's own advanced setting
# DrivePool_BackgroundTasksVeryLowPriority does put background tasks at IDLE
# cpu, but it is a config file read at service start: a permanent choice, not a
# live control, and it says nothing about I/O.
#
# BUT WINDOWS CAN, AND NUARR ALREADY KNOWS HOW. jobs._set_io_priority has been
# demoting nuarr's own ffmpeg children with psutil's ionice/nice for as long as
# the viewer yield has existed, and that call works on any process nuarr has
# rights to. Verified on DrivePool's own processes here: ionice read 2 (normal)
# and 0 (very low) was accepted and restored cleanly.
#
# WHICH PROCESS, AND WHY IT IS SAFE TO SLOW. The pool's DATA PATH is the
# covefs kernel driver - reads and writes of pooled files do not go through
# the service. DrivePool.Service.exe is the background half: balancing,
# duplication, re-measuring, the mover. Demoting it therefore slows the
# housekeeping and not the file system, which is exactly the half that gets in
# a viewer's way. The UI and notification processes are left alone; they cost
# nothing and slowing them would only make the tray look broken.
#
# AND IT IS THE OPPOSITE OF WHAT NUARR DOES NOW. Today a balance simply BLOCKS
# the queue: nuarr stops and waits for DrivePool to finish, which on a full
# rebalance is hours. Being able to demote the mover instead means the two can
# share - the balance takes longer, the queue keeps moving, and the viewer is
# ahead of both.
DP_PROCS = ("drivepool.service.exe", "drivepool.service.native.exe")

# HOW LONG IT STAYS STEPPED ASIDE AFTER THE REASON HAS GONE.
#
# Asymmetric on purpose, and for the same reason jobs.IO_HOLD_S exists: fast
# to yield, slow to take back. Plex closes one episode and opens the next, and
# for a second or two in between there is no session on any disk at all - so
# the mover was handed the array back at full speed, put 167 MB/s through a
# spindle, and was demoted again once the next episode registered. The viewer
# pays for that gap twice: once for the burst and once for the seek storm
# when it is throttled back. Erik watched exactly that happen and the next
# episode buffered.
#
# Being wrong about yielding costs a balance some minutes. Being wrong about
# taking it back costs somebody's playback.
DP_LINGER_S = 120.0
_PRIO = {"low": False, "at": 0.0, "why": "", "disks": [], "pids": {},
         "err": "", "can": None, "clear_at": 0.0}


def _dp_processes() -> list:
    try:
        import psutil
    except Exception:                                        # noqa: BLE001
        return []
    out = []
    try:
        for x in psutil.process_iter(["name", "pid"]):
            if (x.info["name"] or "").lower() in DP_PROCS:
                out.append(psutil.Process(x.info["pid"]))
    except Exception:                                        # noqa: BLE001
        return out
    return out


def priority() -> dict:
    """What priority DrivePool's background half is running at, right now."""
    try:
        import psutil
    except Exception:                                        # noqa: BLE001
        return {"ok": False, "why": "psutil is not available"}
    rows, low_n, norm_n = [], 0, 0
    for p in _dp_processes():
        try:
            io = p.ionice()
            cpu = p.nice()
            iv = int(io) if not hasattr(io, "value") else int(io.value)
            rows.append({"pid": p.pid, "name": p.name(),
                         "io": iv, "cpu": int(cpu),
                         "io_word": {0: "very low", 1: "low",
                                     2: "normal"}.get(iv, str(iv))})
            if iv <= 1:
                low_n += 1
            else:
                norm_n += 1
        except Exception:                                    # noqa: BLE001
            continue
    left = 0.0
    if _PRIO.get("low") and _PRIO.get("clear_at"):
        left = max(0.0, DP_LINGER_S - (time.time() - _PRIO["clear_at"]))
    return {"ok": bool(rows), "procs": rows, "low": bool(low_n and not norm_n),
            "mixed": bool(low_n and norm_n),
            "want_low": bool(_PRIO["low"]), "why": _PRIO["why"],
            # The disks the reason is about, so a panel can colour them the
            # way every other mention of a spindle on this page is coloured.
            "disks": list(_PRIO.get("disks") or []),
            "since": _PRIO["at"], "err": _PRIO["err"],
            "can": _PRIO["can"],
            # How much of the linger is left, so "why is it still low when
            # nobody is watching" has an answer on screen.
            "linger_s": DP_LINGER_S, "linger_left": round(left, 1),
            "enabled": get_toggle("drivepool.yield")}


def set_priority(low: bool, why: str = "", disks=None) -> dict:
    """Demote or restore DrivePool's background half. Returns what happened.

    IDLE WAS THE WRONG FLOOR FOR NUARR'S OWN JOBS and it is the wrong floor
    here too, for the same reason jobs.py gives: an idle-class process can be
    starved outright by anything at all, and a mover stalled halfway through
    relocating a file is worse than a slow one. Very low I/O with
    below-normal CPU is the pairing that yields without stopping.
    """
    try:
        import psutil
    except Exception as e:                                   # noqa: BLE001
        _PRIO["err"] = f"{type(e).__name__}: {e}"
        return {"ok": False, "why": _PRIO["err"]}
    want_io = psutil.IOPRIO_VERYLOW if low else psutil.IOPRIO_NORMAL
    want_cpu = (psutil.BELOW_NORMAL_PRIORITY_CLASS if low
                else psutil.NORMAL_PRIORITY_CLASS)
    done, failed = 0, ""
    for p in _dp_processes():
        try:
            p.ionice(want_io)
            p.nice(want_cpu)
            done += 1
        except Exception as e:                               # noqa: BLE001
            failed = f"{type(e).__name__}: {e}"
    _PRIO["can"] = bool(done) if not failed else False
    if done:
        # THE REASON IS RESTATED, NOT REMEMBERED. It used to be written once,
        # when the state changed, and then left alone for as long as the state
        # held - so a demotion that began because somebody was watching
        # NU-DRIVE-6 went on saying NU-DRIVE-6 after they had moved to
        # NU-DRIVE-1 and the balance had moved on too. Ten minutes of a
        # confidently wrong sentence about which disk was being protected.
        changed = bool(_PRIO["low"]) != bool(low)
        if changed:
            _log(f"DrivePool background priority "
                 + ("lowered" if low else "restored")
                 + (f" - {why}" if why and low else ""),
                 "info" if low else "ok")
        _PRIO.update(low=bool(low), why=(why if low else ""),
                     disks=(list(disks or []) if low else []),
                     err=failed)
        # `at` is "since when has it been in this state", so only a real
        # change moves it; a restated reason must not reset the clock.
        if changed or not _PRIO.get("at"):
            _PRIO["at"] = time.time()
    else:
        _PRIO["err"] = failed or "DrivePool is not running"
    return {"ok": bool(done), "changed": done, "err": _PRIO["err"]}


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS drivepool_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT    NOT NULL,
                started_at REAL    NOT NULL,
                ended_at   REAL,
                ratio_from REAL,
                ratio_to   REAL,
                peak_bps   REAL    NOT NULL DEFAULT 0,
                sum_bps_s  REAL    NOT NULL DEFAULT 0,
                samples    INTEGER NOT NULL DEFAULT 0
            )""")


# ------------------------------------------------------------ toggles ----

def get_toggle(key: str) -> bool:
    v = kv_get(key)
    if v is None:
        v = DEFAULTS.get(key, "0")
    return str(v) == "1"


def toggles() -> dict:
    return {k: get_toggle(k) for k in DEFAULTS}


# ------------------------------------------------------------- reading ----

def _ts(s: str) -> float:
    """'2026-09-05 04:53:33Z' -> epoch."""
    import calendar
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%SZ"))


def _newest_log() -> str:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "DrivePool.Service-*.log")))
    return files[-1] if files else ""


def _apply(level: str, msg: str, at: float) -> None:
    """One log line into STATE, with episode bookkeeping."""
    m = _NOTE.search(msg)
    if m:
        verb, key, ratio = m.group(1), m.group(2), m.group(3)
        act = ACTIVITIES.get(key)
        if act:
            if verb == "Set":
                cur = STATE["active"].get(act)
                r = float(ratio) if ratio else None
                if cur is None:
                    STATE["active"][act] = {"since": at, "ratio": r, "ratio_from": r}
                    _episode_start(act, at, r)
                    _log(f"DrivePool started {LABEL[act]}", "info")
                else:
                    cur["ratio"] = r if r is not None else cur.get("ratio")
            else:
                cur = STATE["active"].pop(act, None)
                if cur is not None:
                    took = max(0.0, at - cur["since"])
                    ep = _episode_end(act, at, cur.get("ratio"))
                    moved = ""
                    if ep and ep.get("avg_bps"):
                        moved = (f" - about {ep['avg_bps']*ep['seen_s']/2/1e9:.0f} GB "
                                 f"moved at {ep['avg_bps']/2/1e6:.0f} MB/s while nuarr "
                                 f"was watching")
                    _log(f"DrivePool finished {LABEL[act]} after "
                               f"{int(took//3600)}h {int(took%3600//60)}m{moved}", "ok")
        elif key in FLAGS:
            if verb == "Set":
                STATE["flags"].setdefault(key, at)
            else:
                STATE["flags"].pop(key, None)
        return
    m = _MOVE_ERR.search(msg)
    if m:
        STATE["mover_errors"].append({"at": at, "path": m.group(1),
                                      "why": m.group(2).strip()[:120]})
        del STATE["mover_errors"][:-50]


def _read_new() -> None:
    """Tail the newest service log; re-read from the start when the day rolls."""
    f = _newest_log()
    if not f:
        STATE["log_ok"] = False
        return
    if f != _TAIL["file"]:
        boot = not _TAIL["file"]
        # A new file - at boot, or the day rolled. Establish state from the
        # whole of it and the previous file (a balance that began before
        # midnight UTC is still on). AT BOOT THAT IS HISTORY, NOT NEWS: it is
        # read silently and its episodes are already in the table. When the
        # day rolls while running, the new file's lines are news like any.
        _TAIL.update(file=f, pos=0)
        STATE["active"] = {}
        STATE["flags"] = {}
        STATE["mover_errors"] = []
        files = sorted(glob.glob(os.path.join(LOG_DIR, "DrivePool.Service-*.log")))
        for prev in files[-2:-1]:
            _consume(prev, 0, silent=True)
        if boot:
            _TAIL["pos"] = _consume(f, 0, silent=True)
    pos = _consume(f, _TAIL["pos"])
    _TAIL["pos"] = pos
    STATE.update(log_ok=True, log_file=os.path.basename(f))


def _consume(path: str, pos: int, silent: bool = False) -> int:
    try:
        with open(path, "rb") as fh:
            fh.seek(pos)
            data = fh.read()
            pos = fh.tell()
    except OSError:
        return pos
    if not data:
        return pos
    text = data.decode("utf-8", "replace")
    # Only whole lines; keep the partial tail for next time.
    if not text.endswith("\n"):
        cut = text.rfind("\n")
        if cut < 0:
            return pos - len(data)
        pos -= len(data) - len(text[:cut + 1].encode("utf-8"))
        text = text[:cut + 1]
    if silent:
        # History, not news: apply without logging by muting joblog briefly.
        global _MUTE
        _MUTE = True
    try:
        for line in text.splitlines():
            m = _LINE.match(line)
            if not m:
                continue
            try:
                at = _ts(m.group(3))
            except Exception:                                # noqa: BLE001
                continue
            _apply(m.group(1), m.group(2), at)
            STATE["log_at"] = at
    finally:
        _MUTE = False
    return pos


_MUTE = False
_real_log = joblog.log


def _log(text: str, level: str = "info", *a, **k):
    if not _MUTE:
        _real_log(text, level, *a, **k)



def _service_io() -> None:
    """The service's own read/write rate: how hard the mover is working."""
    try:
        import psutil
        p = next((x for x in psutil.process_iter(["name"])
                  if (x.info["name"] or "").lower() == "drivepool.service.exe"), None)
        STATE["running"] = p is not None
        if p is None:
            STATE.update(rate_bps=0.0, read_bps=0.0, write_bps=0.0)
            return
        c = p.io_counters()
        now = time.time()
        if _IO["at"]:
            dt = max(0.5, now - _IO["at"])
            r = (c.read_bytes - _IO["r"]) / dt
            w = (c.write_bytes - _IO["w"]) / dt
            STATE.update(read_bps=max(0.0, r), write_bps=max(0.0, w),
                         rate_bps=max(0.0, r + w))
        _IO.update(at=now, r=c.read_bytes, w=c.write_bytes)
    except Exception:                                        # noqa: BLE001
        pass


def _episode_start(kind: str, at: float, ratio) -> None:
    try:
        with cursor() as cur:
            # Replayed history must not mint a second row for the same start.
            if cur.execute("SELECT 1 FROM drivepool_events WHERE kind=? AND started_at=?",
                           (kind, at)).fetchone():
                return
            cur.execute("INSERT INTO drivepool_events(kind, started_at, ratio_from) "
                        "VALUES (?,?,?)", (kind, at, ratio))
    except Exception:                                        # noqa: BLE001
        pass


def _episode_sample(kind: str, bps: float) -> None:
    try:
        with cursor() as cur:
            cur.execute("UPDATE drivepool_events SET peak_bps=MAX(peak_bps,?), "
                        "sum_bps_s=sum_bps_s+?, samples=samples+1 "
                        "WHERE kind=? AND ended_at IS NULL",
                        (bps, bps * POLL_S, kind))
    except Exception:                                        # noqa: BLE001
        pass


def _episode_end(kind: str, at: float, ratio) -> dict | None:
    try:
        with cursor() as cur:
            r = cur.execute("SELECT id, started_at, sum_bps_s, samples FROM drivepool_events "
                            "WHERE kind=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                            (kind,)).fetchone()
            if not r:
                return None
            if at < r["started_at"]:
                return None                       # stale Clear from a replay
            cur.execute("UPDATE drivepool_events SET ended_at=?, ratio_to=? WHERE id=?",
                        (at, ratio, r["id"]))
            cur.execute("DELETE FROM drivepool_events WHERE id NOT IN "
                        "(SELECT id FROM drivepool_events ORDER BY id DESC LIMIT ?)",
                        (KEEP_EVENTS,))
            seen = r["samples"] * POLL_S
            return {"avg_bps": (r["sum_bps_s"] / seen) if seen else 0.0,
                    "seen_s": seen}
    except Exception:                                        # noqa: BLE001
        return None


def refresh() -> None:
    """One poll: the log, then the service's I/O. Never raises."""
    STATE["installed"] = os.path.exists(EXE)
    if STATE["installed"] and not STATE["version"]:
        try:
            import subprocess
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{EXE}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=20,
                creationflags=0x08000000).stdout.strip()
            STATE["version"] = out
        except Exception:                                    # noqa: BLE001
            STATE["version"] = ""
    if not STATE["installed"]:
        return
    try:
        _read_new()
    except Exception as e:                                   # noqa: BLE001
        STATE["last_error"] = f"{type(e).__name__}: {e}"
        STATE["log_ok"] = False
    _service_io()
    for kind in list(STATE["active"]):
        _episode_sample(kind, STATE["rate_bps"])


# -------------------------------------------------------------- holds ----

def moving() -> dict:
    """Activities in progress right now, oldest first."""
    return dict(sorted(STATE["active"].items(), key=lambda kv: kv[1]["since"]))


def hold(kind: str) -> tuple[bool, str]:
    """Should nuarr hold `kind` (jobs | commits | renames), and why."""
    if not get_toggle("drivepool.enabled") or not STATE.get("installed"):
        return False, ""
    for act, info in moving().items():
        if get_toggle(f"drivepool.{act}.{kind}"):
            return True, describe(act, info)
    return False, ""


def hold_active(kind: str) -> tuple[bool, str]:
    """The hold, weighed against what the disks are actually doing.

    hold() is the switch; this is the decision. A rename is a metadata
    operation racing the mover for the same file, so its hold is the switch
    alone. New jobs and file replaces are I/O, and for those a balance that
    is barely touching the disks is no reason to stop: nuarr shares the
    spindle at a measured pace (gate.load_pace) and holds only while the
    move has a disk at quarter-speed level or worse - which is what "pause
    only if it is really high" means in practice.
    """
    held, why = hold(kind)
    if not held or kind == "renames":
        return held, why
    # A DISK BEING REMOVED holds everything, whatever the load: every file
    # on it is on the move and nuarr cannot know which.
    if "removing" in moving():
        return held, why
    if kind == "commits":
        # A FILE REPLACE IS PACED WHERE IT LANDS, NOT HELD FOR THE POOL.
        # This used to wait while ANY disk was at quarter-speed level, on
        # the reasoning that DrivePool might place the new file on it. Seen
        # live: eight finished jobs sat on NU-DRIVE-1 "waiting for
        # DrivePool" while their own disk read 9% busy and "full speed" -
        # because NU-DRIVE-11, which none of them would touch, was at 100%.
        # The copy learns its destination within the first chunk and the
        # per-chunk ramp (gate.load_pace) slows or stops it on THAT disk,
        # which is the hold this was trying to be. So it goes ahead.
        return False, (f"{why} - going ahead, paced per chunk against the "
                       f"disk the file lands on")
    try:
        from . import gate as _g
        heavy = _g.heavy_disks()
    except Exception:                                    # noqa: BLE001
        return held, why
    if heavy:
        return True, f"{why} - hitting {', '.join(heavy)} hard"
    return False, f"{why} - but the disks have headroom, so going ahead at a measured pace"


def describe(act: str, info: dict | None = None) -> str:
    info = info or STATE["active"].get(act) or {}
    bits = [f"DrivePool is {LABEL.get(act, act)}"]
    if info.get("ratio") is not None:
        bits.append(f"{info['ratio']*100:.0f}% through this run")
    if STATE.get("rate_bps", 0) > 1e6:
        bits.append(f"moving at {STATE['rate_bps']/2/1e6:.0f} MB/s")
    if info.get("since"):
        s = time.time() - info["since"]
        bits.append(f"for {int(s//3600)}h {int(s%3600//60)}m" if s >= 3600
                    else f"for {int(s//60)}m")
    return " - ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]


STORE_JSON = r"C:\ProgramData\StableBit DrivePool\Service\Store\Json"
STORE = r"C:\ProgramData\StableBit DrivePool\Service\Store"
INSTALL = r"C:\Program Files\StableBit\DrivePool"
_BAL = {"at": 0.0, "v": None}

# ---- DRIVEPOOL'S OWN TARGETS, read straight from its store -----------------
#
# The triangles on DrivePool's disk bars ("Un-duplicated target for
# rebalancing (121 GB)") are per-disk PoolPartInfo records: which disks are
# moving in or out, the fill ratio each one is heading for, and how many
# bytes are still to move. They are .NET BinaryFormatter blobs, so the
# honest way to read them is with .NET itself: a PowerShell child loads
# DrivePool's own assemblies (read-only) and deserialises. That takes a
# couple of seconds, so it runs in a thread and is cached - a minute while
# a move is on, ten when idle. Anything that fails leaves the previous
# answer in place; the page then says less, never something wrong.
_PS_TARGETS = r"""
$ErrorActionPreference = 'Stop'
$base = '%(install)s'
foreach ($n in 'Cove.Util.dll','Cove.Native.dll','DrivePool.Comm.dll') { [void][Reflection.Assembly]::LoadFrom("$base\$n") }
$bf = New-Object System.Runtime.Serialization.Formatters.Binary.BinaryFormatter
$out = @()
foreach ($f in (Get-ChildItem '%(store)s' -File | Where-Object { $_.Name -notmatch '\.' })) {
  try {
    $fs = [IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite')
    try { $node = $bf.Deserialize($fs) } finally { $fs.Close() }
  } catch { continue }
  $item = $node.Item
  if ($null -eq $item -or $item.GetType().Name -ne 'PoolPartInfo') { continue }
  $out += [pscustomobject]@{
    name = $item.Name; moving_in = $item.IsMovingIn; moving_out = $item.IsMovingOut;
    missing = $item.IsMissing; removing = $item.IsRemoving;
    target = $item.UnprotectedMoveTarget; delta = $item.UnprotectedMoveDelta;
    prot_target = $item.ProtectedMoveTarget; prot_delta = $item.ProtectedMoveDelta;
    use_limit = $item.UseUnprotectedLimit; limit = $item.UnprotectedLimit
  }
}
$out | ConvertTo-Json -Depth 2 -Compress
"""
_TGT: dict = {"at": 0.0, "v": {}, "busy": False, "err": ""}
_TGT_KV = "drivepool.targets.history"


def _disk_used() -> dict[str, int]:
    """Used bytes per pool disk label, live from the volume."""
    import shutil
    out: dict[str, int] = {}
    try:
        from . import scanner
        for label, path in (scanner.media_roots() or {}).items():
            try:
                out[label] = shutil.disk_usage(path).used
            except OSError:
                pass
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _tgt_history_load() -> dict:
    import json as _j
    try:
        return _j.loads(kv_get(_TGT_KV) or "{}") or {}
    except Exception:                                        # noqa: BLE001
        return {}


def _tgt_history_save(v: dict) -> None:
    import json as _j
    keep = {k: {f: t.get(f) for f in ("delta", "plan", "changed_at", "used_at", "first_delta", "prev_delta")}
            for k, t in v.items()}
    try:
        kv_set(_TGT_KV, _j.dumps(keep))
    except Exception:                                        # noqa: BLE001
        pass


def _read_targets() -> dict:
    import base64
    import json as _j
    import subprocess
    script = _PS_TARGETS % {"install": INSTALL, "store": STORE}
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", enc],
        capture_output=True, text=True, timeout=60, creationflags=0x08000000)
    txt = (out.stdout or "").strip()
    if not txt:
        raise RuntimeError((out.stderr or "no output").strip()[:200])
    rows = _j.loads(txt)
    if isinstance(rows, dict):
        rows = [rows]
    res = {}
    for r in rows:
        label = re.sub(r"\s*\(Disk \d+\)\s*$", "", str(r.get("name") or "")).strip()
        if not label:
            continue
        res[label] = {
            "moving_in": bool(r.get("moving_in")), "moving_out": bool(r.get("moving_out")),
            "missing": bool(r.get("missing")), "removing": bool(r.get("removing")),
            "target": r.get("target"), "delta": r.get("delta"),
            "prot_target": r.get("prot_target"), "prot_delta": r.get("prot_delta"),
            "limit": (r.get("limit") if r.get("use_limit") else None),
        }
    return res


def targets(max_age: float | None = None) -> dict:
    """Per-disk balancing targets, refreshed in the background when stale."""
    import threading
    now = time.time()
    age = max_age if max_age is not None else (10.0 if STATE["active"] else 600.0)
    if now - _TGT["at"] > age and not _TGT["busy"] and STATE.get("installed"):
        _TGT["busy"] = True

        def _go():
            try:
                v = _read_targets()
                # HOW THE TARGET IS MOVING. Each reading carries the previous
                # delta, when the delta last changed, and the pace between
                # readings - so the page can show "+3.5 GB in, was +4.2 GB,
                # changed 40 s ago" rather than a number that may or may not
                # be live. Carried in memory across readings; the first
                # reading after a restart has no history.
                # THE DELTA IS THE PLAN, NOT THE PROGRESS. DrivePool writes
                # UnprotectedMoveDelta when it plans a pass and leaves it
                # alone while the mover works - every disk read "changed 38m
                # ago" through a run that had moved 250 GB. So the moment a
                # plan changes, the disk's used bytes are recorded beside it
                # (used_at); what has moved since is used - used_at, and what
                # is left is delta minus that. The page does that arithmetic
                # against the live used figure. Kept in the kv store so a
                # restart mid-run does not forget where the plan started.
                old = _TGT["v"] or _tgt_history_load()
                now2 = time.time()
                used_now = None
                changed = False
                for k, t in v.items():
                    o = old.get(k) or {}
                    d_now, d_old = t.get("delta"), o.get("delta")
                    # both halves of the plan count as "the plan changed"
                    if d_now is not None:
                        d_now = d_now + (t.get("prot_delta") or 0)
                    if d_old is not None and o.get("plan") is not None:
                        d_old = o["plan"]
                    t["plan"] = d_now
                    t["read_at"] = now2
                    if d_old is None or d_now is None:
                        t["changed_at"] = o.get("changed_at") or now2
                        t["prev_delta"] = d_old
                        t["delta_bps"] = 0.0
                        t["used_at"] = o.get("used_at")
                        if t["used_at"] is None and d_now is not None:
                            used_now = _disk_used() if used_now is None else used_now
                            t["used_at"] = used_now.get(k)
                            changed = True
                    elif abs(d_now - d_old) >= 1:
                        t["changed_at"] = now2
                        t["prev_delta"] = d_old
                        dt = max(1.0, now2 - float(o.get("read_at") or _TGT["at"] or now2))
                        t["delta_bps"] = (d_now - d_old) / dt
                        used_now = _disk_used() if used_now is None else used_now
                        t["used_at"] = used_now.get(k)
                        changed = True
                    else:
                        t["changed_at"] = o.get("changed_at") or now2
                        t["prev_delta"] = o.get("prev_delta", d_old)
                        t["delta_bps"] = 0.0
                        t["used_at"] = o.get("used_at")
                    t["first_delta"] = o.get("first_delta", d_old if d_old is not None else d_now)
                if changed or not _TGT["v"]:
                    _tgt_history_save(v)
                _TGT.update(v=v, at=time.time(), err="")
            except Exception as e:                           # noqa: BLE001
                _TGT.update(at=time.time(), err=f"{type(e).__name__}: {e}")
            finally:
                _TGT["busy"] = False
        threading.Thread(target=_go, daemon=True, name="dp-targets").start()
    return _TGT["v"]


def balance_info() -> dict:
    """DrivePool's own verdict: how balanced, bytes to move, which plugins are on."""
    import glob as _g
    import json as _j
    out: dict = {}
    try:
        for f in _g.glob(os.path.join(STORE_JSON, "*_FileBalanceInfo.json")):
            item = (_j.load(open(f, encoding="utf-8")).get("Item") or {})
            out["ratio"] = item.get("BalanceRatio")
            out["bytes_to_balance"] = item.get("BytesToBalance")
            out["waiting"] = bool(item.get("IsWaiting"))
            out["reason"] = item.get("BalanceReason")
            out["plugins"] = [{"name": b.get("Name"), "on": bool(b.get("IsEnabled"))}
                              for b in ((item.get("Balancers") or {}).get("$values") or [])]
            break
    except Exception:                                        # noqa: BLE001
        pass
    return out


def balancers() -> dict:
    r"""The two balancer settings that decide which disk is next. Cached 10 min.

    DrivePool keeps each balancer plugin's settings in its store as a .NET
    BinaryFormatter blob, base64 in JSON. Two of them say where a balance
    moves data:

      Prevent Drive Overfill (DriveFillInfo): _FillRatio / _EmptyRatio -
        a disk over the empty line is drained until it is under it, and no
        disk is filled past the fill line. Here both are 0.65, which is
        exactly why NU-DRIVE-0 and NU-DRIVE-1 at 67% are the ones emptying.
      Disk Space Equalizer: _equalizeByPercent / ByFreeSpace / ByUsedSpace -
        which measure "emptiest" is taken by, so the target of a move is
        predictable.

    Read by field name, not by offset: the blob is a class name, its field
    names, then the values in declaration order, and the doubles are the
    first two 8-byte IEEE values after the last field name. Anything that
    does not parse simply returns nothing, and the page says less.
    """
    import base64
    import glob as _g
    import json as _j
    import struct
    now = time.time()
    if _BAL["v"] is not None and now - _BAL["at"] < 600:
        return _BAL["v"]
    out: dict = {}
    try:
        for f in _g.glob(os.path.join(STORE_JSON, "*_BalancerSettingsStateInfo.json")):
            try:
                raw = base64.b64decode(
                    (_j.load(open(f, encoding="utf-8")).get("Item") or {}).get("SettingsState") or "")
            except Exception:                            # noqa: BLE001
                continue
            txt = raw.decode("latin-1")
            if "DriveFillInfo" in txt:
                i = txt.find("_EmptyBytes")
                doubles = []
                j = i
                while j < len(raw) - 8 and len(doubles) < 2:
                    v = struct.unpack("<d", raw[j:j + 8])[0]
                    if 0.05 <= v <= 1.0:
                        doubles.append(round(v, 4)); j += 8
                    else:
                        j += 1
                if len(doubles) == 2:
                    out["overfill"] = {"fill": doubles[0], "empty": doubles[1]}
            elif "DiskSpaceEqualizerPlugin" in txt:
                i = txt.find("_overfillLimit")
                # values: a small int32 (version), then five bools in field order
                m = re.search(rb"[\x00-\x10]\x00\x00\x00([\x00\x01])([\x00\x01])([\x00\x01])([\x00\x01])([\x00\x01])",
                              raw[i:])
                if m:
                    out["equalizer"] = {"protected": m.group(1) == b"\x01",
                                        "unprotected": m.group(2) == b"\x01",
                                        "by_percent": m.group(3) == b"\x01",
                                        "by_free": m.group(4) == b"\x01",
                                        "by_used": m.group(5) == b"\x01"}
    except Exception:                                    # noqa: BLE001
        pass
    # ONLY THE PLUGINS THAT ARE ON. The store keeps every plugin's settings
    # whether or not it is enabled, and the page was quoting Prevent Drive
    # Overfill's lines after Erik had switched it off for All In One.
    try:
        on = {p["name"] for p in (balance_info().get("plugins") or []) if p["on"]}
        if on:
            if "Prevent Drive Overfill" not in on:
                out.pop("overfill", None)
            if "Disk Space Equalizer" not in on and "All In One" not in on:
                out.pop("equalizer", None)
            out["enabled"] = sorted(on)
    except Exception:                                    # noqa: BLE001
        pass
    _BAL.update(at=now, v=out)
    return out


# ------------------------------------------------- starting and stopping it --
#
# DRIVEPOOL HAS NO SWITCH FOR THIS THAT ANYTHING BUT ITS OWN UI CAN REACH.
# dpcmd can add and remove pool parts, set duplication and remeasure; it
# cannot start or stop a balance. The UI talks to the service over a private
# .NET channel. What there IS: the service is an ordinary Windows service, and
# the pool is NOT the service - it is the covefs kernel driver, which keeps the
# pool mounted, readable and writable whether the service is up or not.
# Measured here before any of this was written: service stopped in 3 s, the
# pool listed 748 folders and read a file while it was down, service back in
# under a second.
#
# So "stop balancing" is: stop the service, which abandons the move in
# progress (DrivePool copies to a temp name and cleans up on the next start),
# set the pool's automatic-balancing option to "do not balance automatically",
# and start the service again. "Start balancing" sets the option to "balance
# immediately" and does the same restart, after which DrivePool begins a pass
# whenever the pool is outside its own thresholds, throttled to one every ten
# minutes. Those two options are exactly the radio DrivePool's own Balancing
# settings page shows; nuarr writes the same value to the same store.
#
# WHAT A RESTART COSTS. Three seconds without the service: no balancing, no
# background duplication, no UI, no notifications. The data path is untouched.
# The option is edited only while the service is stopped, because the running
# service owns that store and writes it back on its own schedule.
SERVICE = "DrivePoolService"
AUTO_WORD = {0: "do not balance automatically", 1: "balance immediately"}


def service_state() -> str:
    """running | stopped | <other> | unknown"""
    try:
        r = subprocess.run(["sc.exe", "query", SERVICE], capture_output=True,
                           text=True, timeout=15, creationflags=NO_WINDOW)
        m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", r.stdout or "")
        return (m.group(1).lower() if m else "unknown")
    except Exception:                                        # noqa: BLE001
        return "unknown"


def _pool_options_path() -> str:
    import glob as _g
    hits = sorted(_g.glob(os.path.join(STORE_JSON, "*_PoolOptions.json")))
    return hits[0] if hits else ""


def _read_pool_options() -> dict:
    p = _pool_options_path()
    if not p:
        return {}
    try:
        import json as _j
        with open(p, encoding="utf-8") as fh:
            return _j.load(fh)
    except Exception:                                        # noqa: BLE001
        return {}


def balancing() -> dict:
    """Where the switch is, whether the service is up, and whether it is moving."""
    d = _read_pool_options()
    item = d.get("Item") or {}
    auto = item.get("AutoBalanceType")
    try:
        auto = int(auto)
    except (TypeError, ValueError):
        auto = None
    return {"service": service_state(),
            "auto": auto, "auto_word": AUTO_WORD.get(auto, "unknown"),
            "allow_immediate": bool(item.get("AllowBalanceImmediately", True)),
            "throttle": str(item.get("ImmediateBalanceThrottle") or ""),
            "time_of_day": str(item.get("BalancingTimeOfDay") or ""),
            "moving": "balancing" in moving(),
            "store": bool(_pool_options_path()),
            "last": dict(_BAL_LAST)}


_BAL_LAST: dict = {"at": 0.0, "what": "", "ok": None, "why": ""}


def _svc(verb: str, want: str, timeout: float = 45.0) -> tuple:
    try:
        subprocess.run(["sc.exe", verb, SERVICE], capture_output=True,
                       text=True, timeout=20, creationflags=NO_WINDOW)
    except Exception as e:                                   # noqa: BLE001
        return False, f"sc {verb}: {type(e).__name__}: {e}"[:160]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if service_state() == want:
            return True, ""
        time.sleep(0.5)
    return False, f"the service did not reach '{want}' within {int(timeout)}s"


def _write_auto(auto: int) -> tuple:
    """Set AutoBalanceType in the pool's own store. Only while stopped."""
    import json as _j
    p = _pool_options_path()
    if not p:
        return False, "no PoolOptions in DrivePool's store"
    try:
        with open(p, encoding="utf-8") as fh:
            d = _j.load(fh)
        item = d.setdefault("Item", {})
        if int(item.get("AutoBalanceType", -1)) == int(auto):
            return True, "already set"
        item["AutoBalanceType"] = int(auto)
        if auto == 1:
            item["AllowBalanceImmediately"] = True
        tmp = p + ".nuarr.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _j.dump(d, fh, indent=2)
        os.replace(tmp, p)
        return True, ""
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:160]


def set_balancing(action: str) -> dict:
    r"""start | stop. Restarts the service around the option change.

    Synchronous and a few seconds long; the caller runs it off the loop. It
    says exactly what it did and where it stopped if it could not finish, so
    a half-done restart is never silent.
    """
    action = (action or "").strip().lower()
    if action not in ("start", "stop"):
        return {"ok": False, "why": "action must be start or stop"}
    auto = 1 if action == "start" else 0
    steps: list = []
    was = service_state()
    steps.append(f"service was {was}")
    if was == "running":
        ok, why = _svc("stop", "stopped")
        steps.append("stopped" if ok else f"could not stop: {why}")
        if not ok:
            _BAL_LAST.update(at=time.time(), what=action, ok=False, why=why)
            return {"ok": False, "why": why, "steps": steps}
    ok, why = _write_auto(auto)
    steps.append(f"set '{AUTO_WORD[auto]}'" + (f" ({why})" if why else "")
                 if ok else f"could not set the option: {why}")
    ok2, why2 = _svc("start", "running")
    steps.append("started" if ok2 else f"could not start: {why2}")
    done = ok and ok2
    _BAL_LAST.update(at=time.time(), what=action, ok=done,
                     why=(why or why2) if not done else "")
    try:
        joblog.log("DrivePool: " + ("balancing started - " if action == "start"
                                    else "balancing stopped - ")
                   + "; ".join(steps), "info" if done else "warn")
    except Exception:                                        # noqa: BLE001
        pass
    try:
        STATE["at"] = 0.0                # make the watcher re-read at once
    except Exception:                                        # noqa: BLE001
        pass
    return {"ok": done, "why": (why or why2), "steps": steps,
            "balancing": balancing()}


def status() -> dict:
    """Everything the page shows."""
    ev = []
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT * FROM drivepool_events ORDER BY id DESC LIMIT 20"):
                d = dict(r)
                seen = d["samples"] * POLL_S
                d["avg_bps"] = (d["sum_bps_s"] / seen) if seen else 0.0
                d["moved_est"] = d["sum_bps_s"] / 2            # read + write of the same bytes
                d["seen_s"] = seen
                ev.append(d)
    except Exception:                                        # noqa: BLE001
        pass
    holds = {k: hold_active(k) for k in HOLDS}
    try:
        from . import gate as _g
        press = _g.pressure()
    except Exception:                                    # noqa: BLE001
        press = {}
    today = time.time() - 86400
    return {
        "installed": STATE["installed"], "running": STATE["running"],
        "version": STATE["version"], "log_ok": STATE["log_ok"],
        "log_file": STATE["log_file"], "log_at": STATE["log_at"],
        "active": {k: dict(v, label=LABEL[k], text=describe(k, v))
                   for k, v in moving().items()},
        "flags": dict(STATE["flags"]),
        "rate_bps": STATE["rate_bps"], "read_bps": STATE["read_bps"],
        "write_bps": STATE["write_bps"],
        "mover_errors": [e for e in STATE["mover_errors"] if e["at"] >= today],
        "holds": {k: {"on": v[0], "why": v[1]} for k, v in holds.items()},
        "toggles": toggles(), "labels": LABEL, "kinds": HOLDS,
        "events": ev, "last_error": STATE["last_error"],
        "poll_s": POLL_S, "balancers": balancers(),
        "pressure": press,
        "targets": targets(), "targets_at": _TGT["at"], "targets_err": _TGT["err"],
        "balance": balance_info(),
        # THE START/STOP SWITCH, and where DrivePool's own option sits.
        "balancing": balancing(),
        # WHAT PRIORITY ITS HOUSEKEEPING IS RUNNING AT, and whether nuarr is
        # the reason. See set_priority: dpcmd cannot do this, Windows can.
        "priority": priority(),
    }


async def watch() -> None:
    from . import schedules
    schedules.register(
        "drivepool", "DrivePool watcher", "Integrations", POLL_S,
        what="Reads DrivePool's own service log for balancing, duplication and "
             "disk removal, and holds file replaces and renames while a move "
             "is in progress - each hold a switch on the DrivePool page.")
    await asyncio.sleep(20)
    first = True
    while True:
        schedules.beat("drivepool")
        try:
            await asyncio.to_thread(refresh)
            if first:
                first = False
                if STATE["installed"]:
                    act = ", ".join(LABEL[k] for k in moving()) or "idle"
                    joblog.log(f"DrivePool {STATE['version'] or ''}: watching "
                               f"{os.path.basename(_TAIL['file']) or 'the service log'}"
                               f" - {act} right now", "debug")
                else:
                    joblog.log("DrivePool: not installed on this box - the "
                               "integration is idle", "debug")
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"DrivePool watcher: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(POLL_S)
