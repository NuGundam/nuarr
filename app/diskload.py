r"""
nuarr - how busy are the physical disks, and who is making them busy?

WHY THIS REPLACED THE DRIVEPOOL CHECK
-------------------------------------
The old gate asked StableBit DrivePool a product-specific question: "are you
balancing?" That worked, and only worked, for one product doing one thing. It
was blind to every other reason a disk is too busy to add work to - a Windows
backup, a big file copy, a SnapRAID sync, an unRAID parity check, a Storage
Spaces rebuild, another Plex server on the same spindles, Immich thumbnailing.

The generalisation is to stop naming causes and measure the effect. A disk that
is pinned is a disk that should not be given more work, whatever is pinning it.
That question has the same answer on a single SSD, a hardware RAID array, a
Storage Spaces pool, a JBOD, or a DrivePool - and needs nothing installed.

WHAT IT MEASURES, AND WHY THAT COUNTER
--------------------------------------
Win32_PerfFormattedData_PerfDisk_PhysicalDisk, per physical disk:

    busy %  = 100 - PercentIdleTime
    queue   = CurrentDiskQueueLength
    bytes/s = DiskReadBytesPerSec + DiskWriteBytesPerSec

% Idle Time, NOT % Disk Time. Measured on this box while one disk was
streaming: PercentDiskTime read 145 - the counter is a sum over queued
requests and routinely exceeds 100 on anything that overlaps I/O, so it cannot
be compared against a threshold. Idle time is bounded 0-100 by construction.

PHYSICAL DISKS, NOT VOLUMES. In the same sample, P:\ reported 100% idle while
its member disk 0 was at 0% idle and 51 MB/s. A pooled or virtual volume shows
none of the load carried by the disks underneath it, so a check that watched
the library's drive letter would see an idle system during a rebuild.

THE FEEDBACK TRAP
-----------------
nuarr's own encodes make disks busy. A naive "pause while the disk is busy"
gate therefore pauses on its own work, goes quiet, unpauses, and oscillates -
and on a fast machine it would never run at all.

So this reports EXTERNAL load: total throughput on the disk minus what nuarr's
own workers are moving there. A disk saturated purely by our own job is not a
reason to hold - that case is already handled by disk_wait_pct, which stops a
SECOND job piling onto a spindle we are using. The two rules are complementary:

    disk_wait_pct   - do not stack our own jobs on one spindle
    disk_busy_pct   - do not start work while something ELSE owns the disk
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# One sample is a spike; a disk being busy for a while is a condition. Samples
# are kept for this long and the gate asks for the median, so a burst of
# read-ahead cannot hold the queue and a genuine rebuild cannot be missed.
WINDOW_S = 20.0
_SAMPLE_TTL = 2.0

_lock = threading.Lock()
_hist: list[tuple[float, dict]] = []      # (at, {key: {...}})
_last: dict = {"at": 0.0, "rows": {}}
_ERR: dict = {"at": 0.0, "msg": ""}


# --------------------------------------------------------------- sampling ---
# THE SAME COUNTERS, WITHOUT LAUNCHING A SHELL TO READ THEM.
#
# This module used to answer "how busy are the disks" by running powershell.exe
# once per sample. It worked, and it cost a process launch every few seconds
# for the life of the server - measured at 513 ms of wall time per sample on
# this machine, roughly one PowerShell start every 3.5 s, forever. Watching
# process creation for 45 seconds caught thirteen of them.
#
# The counters are WMI; PowerShell was only ever the messenger. pywin32 is
# already a dependency, so the same query runs in-process against the same
# provider - no child process, no console (hidden or otherwise), no shell
# startup. The connection is cached per thread because binding to the CIM
# service is the expensive part and the sampler runs from a worker thread.
#
# The PowerShell path is KEPT as a fallback: if COM is unavailable for any
# reason this is a monitoring feature that must degrade, not fail.
_com = threading.local()


def _wmi_service():
    """A cached SWbemServices for this thread, or None if COM is unusable."""
    svc = getattr(_com, "svc", None)
    if svc is not None:
        return svc
    if getattr(_com, "failed", False):
        return None
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        loc = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        _com.svc = loc.ConnectServer(".", "root\\cimv2")
        return _com.svc
    except Exception:                                    # noqa: BLE001
        _com.failed = True                # do not retry per sample
        return None


def _read_counters_wmi() -> dict[str, dict] | None:
    """The counters straight from WMI. None means 'could not', not 'idle'."""
    svc = _wmi_service()
    if svc is None:
        return None
    try:
        rows = svc.ExecQuery(
            "SELECT Name, PercentIdleTime, CurrentDiskQueueLength, "
            "DiskReadBytesPerSec, DiskWriteBytesPerSec "
            "FROM Win32_PerfFormattedData_PerfDisk_PhysicalDisk")
        out: dict[str, dict] = {}
        for r in rows:
            name = str(r.Name or "").strip()
            if not name or name == "_Total":
                continue
            try:
                idle = float(r.PercentIdleTime or 100)
                queue = float(r.CurrentDiskQueueLength or 0)
                rd = float(r.DiskReadBytesPerSec or 0)
                wr = float(r.DiskWriteBytesPerSec or 0)
            except (TypeError, ValueError):
                continue
            out[name] = {
                "name": name,
                "busy": max(0.0, min(100.0, 100.0 - idle)),
                "queue": queue,
                "bps": rd + wr,
                "read_bps": rd,
                "write_bps": wr,
            }
        return out or None
    except Exception as e:                               # noqa: BLE001
        # A failure here is worth one retry through a fresh connection - a
        # stale service object survives a WMI restart as a live COM pointer
        # that raises on use.
        _com.svc = None
        _ERR.update(at=time.time(), msg=f"wmi: {type(e).__name__}: {e}"[:200])
        return None


def _read_counters() -> dict[str, dict]:
    """Per-physical-disk load. {} when the counters cannot be read.

    Uses CIM rather than psutil because psutil exposes no busy/idle time on
    Windows - only cumulative bytes, which cannot answer "is this disk at its
    limit" for a spinning disk whose limit is seeks, not bytes.
    """
    fast = _read_counters_wmi()
    if fast is not None:
        return fast
    # ---- fallback: the original shell path, unchanged ----
    ps = (
        "Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk | "
        "Where-Object { $_.Name -ne '_Total' } | "
        "ForEach-Object { '{0}|{1}|{2}|{3}|{4}' -f $_.Name, $_.PercentIdleTime, "
        "$_.CurrentDiskQueueLength, $_.DiskReadBytesPerSec, $_.DiskWriteBytesPerSec }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        out: dict[str, dict] = {}
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) != 5:
                continue
            name = parts[0].strip()
            try:
                idle = float(parts[1] or 100)
                queue = float(parts[2] or 0)
                rd = float(parts[3] or 0)
                wr = float(parts[4] or 0)
            except ValueError:
                continue
            out[name] = {
                "name": name,
                # the counter is a percentage already; clamp because a sample
                # taken across a counter roll can land outside the range
                "busy": max(0.0, min(100.0, 100.0 - idle)),
                "queue": queue,
                "bps": rd + wr,
                "read_bps": rd,
                "write_bps": wr,
            }
        if not out and (r.stderr or "").strip():
            _ERR.update(at=time.time(), msg=(r.stderr or "").strip()[:200])
        return out
    except Exception as e:
        _ERR.update(at=time.time(), msg=f"{type(e).__name__}: {e}")
        return {}


def sample() -> dict[str, dict]:
    """Current per-disk load, cached briefly and appended to the window."""
    now = time.time()
    with _lock:
        if now - _last["at"] < _SAMPLE_TTL:
            return _last["rows"]
    rows = _read_counters()
    with _lock:
        _last.update(at=now, rows=rows)
        if rows:
            _hist.append((now, rows))
            cut = now - WINDOW_S
            while _hist and _hist[0][0] < cut:
                _hist.pop(0)
    return rows


# ------------------------------------------------------- background ticker ---
# The window is only worth having if it actually FILLS. Sampling lazily, from
# whoever asks, ties the sample rate to the caller's cache TTL - the gate reuses
# a probe for 20 s, so a 20 s window would have held one or two samples and the
# median would have been the same spike it was meant to reject.
#
# A dedicated ticker decouples the two: the window is always several seconds of
# real history no matter how often anyone reads it. It is one CIM query every
# few seconds on a daemon thread, so it costs nothing and dies with the process.
TICK_S = 3.0
_ticker: threading.Thread | None = None


def _tick() -> None:
    while True:
        try:
            sample()
        except Exception:
            pass
        time.sleep(TICK_S)


def start() -> None:
    """Begin filling the window. Idempotent; safe to call from anywhere."""
    global _ticker
    with _lock:
        if _ticker is not None and _ticker.is_alive():
            return
        _ticker = threading.Thread(target=_tick, name="diskload",
                                   daemon=True)
        _ticker.start()


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def sustained() -> dict[str, dict]:
    """Median load per disk across the window - the shape the gate reads.

    Median, not mean: a single 100% sample from a read-ahead burst should not
    move the answer, and a median is immune to it in a way an average is not.
    """
    start()          # idempotent - first read is what turns the ticker on
    with _lock:
        hist = list(_hist)
    if not hist:
        # READ THE WINDOW, do not fill it - except when it is empty. This is
        # called from the 2 s UI poll, and _read_counters() spawns PowerShell
        # and takes the better part of a second. Sampling here would put that
        # cost on a request path that has to stay fast; the ticker above is
        # what keeps the data fresh. The one exception is the very first call,
        # before the ticker has produced anything, where a cold read beats
        # returning nothing.
        sample()
        with _lock:
            hist = list(_hist)
        if not hist:
            return {}
    keys: set[str] = set()
    for _at, rows in hist:
        keys |= set(rows)
    out: dict[str, dict] = {}
    for k in keys:
        busy = [rows[k]["busy"] for _a, rows in hist if k in rows]
        bps = [rows[k]["bps"] for _a, rows in hist if k in rows]
        q = [rows[k]["queue"] for _a, rows in hist if k in rows]
        # READ AND WRITE KEPT APART. They were summed into `bps` and the split
        # thrown away, which is enough to say a disk is busy and not enough to
        # say what it is doing - and the difference is the whole signal for
        # spotting data being MOVED between disks. A balance, a rebuild or a
        # plain file copy all look the same in a total; in the split they are
        # one disk reading and another writing the same number of bytes.
        rd = [rows[k]["read_bps"] for _a, rows in hist if k in rows]
        wr = [rows[k]["write_bps"] for _a, rows in hist if k in rows]
        out[k] = {"name": k, "busy": _median(busy), "bps": _median(bps),
                  "read_bps": _median(rd), "write_bps": _median(wr),
                  "queue": _median(q), "samples": len(busy)}
    return out


# ---------------------------------------------------------------- mapping ---
# A path is on a volume; a volume lives on a physical disk; the counter is
# named after that disk. Windows will not join those for you, so this does it
# once and remembers - the wiring does not change while the process runs.
_MAP: dict[str, str] = {}          # volume-guid or drive-letter -> counter key
_MAP_DONE = False


def _load_map() -> dict[str, str]:
    global _MAP_DONE
    if _MAP_DONE:
        return _MAP
    _MAP_DONE = True
    ps = (
        "Get-CimInstance -Namespace root\\Microsoft\\Windows\\Storage "
        "-ClassName MSFT_Partition | ForEach-Object { $d=$_.DiskNumber; "
        "foreach($p in $_.AccessPaths){ '{0}|{1}' -f $d, $p } }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           creationflags=NO_WINDOW)
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split("|", 1)
            if len(parts) != 2:
                continue
            num, path = parts[0].strip(), parts[1].strip()
            if not num.isdigit() or not path:
                continue
            _MAP[path.lower()] = num
    except Exception as e:
        _ERR.update(at=time.time(), msg=f"map: {type(e).__name__}: {e}")
    return _MAP


def _counter_key(disk_num: str, rows: dict[str, dict]) -> str | None:
    """Counter instances are '5' or '12 C:' - match on the leading number."""
    for k in rows:
        head = k.split(" ", 1)[0].strip()
        if head == disk_num:
            return k
    return None


_PATH_CACHE: dict[str, str | None] = {}


def key_for_path(path: str) -> str | None:
    """Counter key for the physical disk holding `path`, or None.

    Deliberately fails soft. Network storage, a disk the counters do not
    expose, or an unreadable path all return None - and a check that cannot
    measure a disk must never hold work on it, or an unmapped setup would
    stall permanently.
    """
    if not path:
        return None
    p = os.path.abspath(path)
    if p in _PATH_CACHE:
        return _PATH_CACHE[p]
    res: str | None = None
    try:
        m = _load_map()
        rows = sample()
        # Longest match first: a volume mounted INTO a folder on another
        # volume must win over the drive letter that contains the folder.
        best = ""
        low = p.lower()
        for vol in m:
            if low.startswith(vol) and len(vol) > len(best):
                best = vol
        if not best:
            # plain drive letter, e.g. 'e:\...' -> 'e:\'
            drive = os.path.splitdrive(p)[0]
            if drive:
                best = (drive + "\\").lower()
                best = best if best in m else ""
        if best:
            res = _counter_key(m[best], rows)
    except Exception:
        res = None
    if len(_PATH_CACHE) > 500:
        _PATH_CACHE.clear()
    # DO NOT CACHE A MISS. A lookup made before the counters have produced
    # their first sample legitimately fails, and remembering that answer meant
    # eleven of twelve disks stayed unmapped for the life of the process - the
    # panel showed one row and the gate watched one spindle. A miss is cheap to
    # retry and a hit is permanent, so only hits are worth keeping.
    if res:
        _PATH_CACHE[p] = res
    return res


def status() -> dict:
    """Everything the panel needs, without the gate's opinion attached."""
    rows = sustained()
    return {"disks": sorted(rows.values(), key=lambda d: d["busy"], reverse=True),
            "window_s": WINDOW_S, "error": _ERR.get("msg", "")}
