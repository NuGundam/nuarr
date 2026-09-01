r"""
nuarr - library scanner

WHY IT WALKS POOLPARTS INSTEAD OF P:\
-------------------------------------
StableBit DrivePool presents P:\ as a single volume, but every file physically
lives in a "PoolPart.<guid>" folder on ONE underlying disk. When the balancer
(or a transcode) relocates a file between disks, its path on P:\ does not
change - so watching P:\ can never tell you a file moved. Walking the PoolPart
trees gives us the file list AND its physical disk in the same pass.

WHY IDENTITY IS THE ARR FILE ID
-------------------------------
This is the whole reason nuarr exists. Tdarr keys its skiplist on the filename,
so when Sonarr renamed 839 files they all looked new and were reprocessed from
scratch. Here a rename is an UPDATE to a mutable `path` column on a row whose
identity is (arr_name, arr_file_id) - processing state survives untouched.

Files the arrs do not know about (extras, orphans, junk) fall back to a content
signature, which is also rename-proof.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from . import fileops, joblog, pathmap
from .arr import ArrFile, gather_arr_files
from .config import SETTINGS
from .db import cursor, kv_get, kv_set, log_event

MEDIA_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".mpg", ".mpeg"}

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Declare signatures explicitly. Without a restype ctypes assumes a 32-bit int
# and TRUNCATES the 64-bit handle FindFirstVolumeW returns, so the enumeration
# fails or loops on garbage. On this box all 12 pool members are mounted WITHOUT
# drive letters, so a broken enumeration finds zero disks and every file looks
# deleted - the exact failure this guards against.
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_k32.FindFirstVolumeW.restype = wintypes.HANDLE
_k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
_k32.FindNextVolumeW.restype = wintypes.BOOL
_k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
_k32.FindVolumeClose.restype = wintypes.BOOL
_k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
_k32.GetVolumeInformationW.restype = wintypes.BOOL
_k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]
_k32.GetDriveTypeW.restype = wintypes.UINT
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
DRIVE_FIXED = 3


def _is_fixed(root: str) -> bool:
    r"""Is this a real internal disk?

    ASK BEFORE TOUCHING. GetVolumeInformationW and os.path.isdir on an empty
    optical drive, a card reader with no card, or a disconnected network mount
    do not fail quickly - they block on the device, for many seconds each, and
    on a headless service the hard-error dialog they can raise has nobody to
    dismiss it. Enumerating volumes and probing every one of them wedged the
    whole process: the log stopped mid-scan and the port never opened.

    GetDriveTypeW answers from the mount table without going near the device,
    so it is safe to call on anything, and DRIVE_FIXED excludes exactly the
    cases that hang. Media nuarr manages is on fixed disks by definition.
    """
    try:
        return _k32.GetDriveTypeW(root) == DRIVE_FIXED
    except Exception:
        return False


# ---------------------------------------------------------------- volumes ----
def _volume_label(vol: str) -> str:
    buf = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    maxlen = wintypes.DWORD()
    flags = wintypes.DWORD()
    try:
        ok = _k32.GetVolumeInformationW(
            vol, buf, 261, ctypes.byref(serial), ctypes.byref(maxlen),
            ctypes.byref(flags), fs, 261,
        )
    except Exception:
        ok = 0
    if ok and buf.value:
        return buf.value
    return f"VOL-{serial.value:08X}" if serial.value else "UNKNOWN"


def _enumerate_volumes() -> list[str]:
    """Every volume on the box, including ones with NO drive letter.

    DrivePool members are routinely mounted without letters, so enumerating
    A:..Z: alone would silently miss disks - and any file on a missed disk would
    look deleted.
    """
    out: list[str] = []
    buf = ctypes.create_unicode_buffer(260)
    h = _k32.FindFirstVolumeW(buf, 260)
    if not h or h == _INVALID_HANDLE_VALUE:
        return out
    try:
        while True:
            out.append(buf.value)
            if not _k32.FindNextVolumeW(h, buf, 260):
                break
    finally:
        _k32.FindVolumeClose(h)
    return out


_ROOTS_CACHE: dict[str, str] = {}
_ROOTS_AT = 0.0


def media_roots() -> dict[str, str]:
    """label -> a real path on the disk that label refers to.

    THE STORAGE-AGNOSTIC VERSION OF pool_disks().

    pool_disks() finds disks by looking for a folder called "PoolPart.*", which
    is StableBit DrivePool's on-disk signature. That is a fine way to find a
    DrivePool and the only way to find nothing else - a JBOD, a single disk, a
    Storage Spaces pool, a set of mount points or an unRAID share all have no
    PoolPart folder and would come back empty.

    So ask the library instead of the product. Every file already carries the
    disk it lives on, and any real path on a disk is enough to measure that
    disk: shutil.disk_usage() and the volume->physical-disk lookup both accept
    an ordinary file path and resolve the containing volume themselves. That
    also handles a folder mount point correctly, which a drive-letter scan
    does not.

    THE PATH MUST BE ON THE MEMBER VOLUME, NOT THE POOL.

    The obvious version of this - take a file path out of the database and use
    that - is wrong here, and wrong in an instructive way. Every path stored is
    a POOL path (on P:), because that is where the file is opened from. P: is
    a virtual volume: disk_usage() on it returns the whole pool, and the
    volume-to-physical-disk lookup resolves it to nothing, because no single
    spindle backs it. All twelve labels collapsed into one entry that way.

    So resolve by LABEL instead. pool_disk is already the member volume's
    label - scanner.py assigns it from _volume_label() - so enumerating the
    volumes on the box and matching labels gets back to the real disk with no
    knowledge of how the storage is assembled. That works for a pool, a set of
    mount points, a JBOD, or plain lettered drives, and needs nothing installed.

    Falls back to pool_disks() for anything still unmatched.
    """
    global _ROOTS_AT
    now = time.time()
    if _ROOTS_CACHE and now - _ROOTS_AT < 300:
        return _ROOTS_CACHE
    want: set[str] = set()
    try:
        from .db import cursor
        with cursor() as cur:
            want = {r["pool_disk"] for r in cur.execute(
                "SELECT DISTINCT pool_disk FROM files "
                "WHERE pool_disk IS NOT NULL AND pool_disk != '' "
                "AND state != 'deleted'").fetchall() if r["pool_disk"]}
    except Exception:
        pass

    out: dict[str, str] = {}
    # LABELS THAT ARE ALREADY PATHS measure themselves. The direct-walk scan
    # (non-pool libraries) labels files with the share root or the drive
    # letter rather than a volume label, and both are things disk_usage()
    # accepts as-is - a UNC root reports the share's capacity over SMB, which
    # is the only capacity that path HAS from this machine. Without this the
    # label matched no local volume and the panel showed 0 free of 0.
    for lbl in want:
        if lbl.startswith("\\\\"):
            out.setdefault(lbl, lbl if lbl.endswith("\\") else lbl + "\\")
        elif len(lbl) == 2 and lbl[1] == ":":
            out.setdefault(lbl, lbl + "\\")
    try:
        # Volumes WITHOUT a drive letter included: a pool member is routinely
        # mounted with none, and those are exactly the disks being measured.
        roots = _enumerate_volumes()
        roots += [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"]
        for root in roots:
            # Drive type FIRST - see _is_fixed(). isdir() on a removable bay
            # with nothing in it is itself one of the blocking calls, so this
            # test has to come before any filesystem access, not after it.
            if not _is_fixed(root):
                continue
            try:
                if not os.path.isdir(root):
                    continue
            except OSError:
                continue
            lbl = _volume_label(root)
            if lbl and (not want or lbl in want) and lbl not in out:
                out[lbl] = root
    except Exception:
        pass

    # Anything the label scan missed - an unlabelled member, say - falls back
    # to the DrivePool-specific lookup rather than being dropped.
    missing = (want - set(out)) if want else set()
    if missing or not out:
        try:
            for lbl, p in (pool_disks() or {}).items():
                if lbl not in out and (not want or lbl in want):
                    out[lbl] = p
        except Exception:
            pass
    if out:
        _ROOTS_CACHE.clear()
        _ROOTS_CACHE.update(out)
        _ROOTS_AT = now
    return _ROOTS_CACHE


def _register_with_storage() -> None:
    r"""Offer the PoolPart walk to storage.py as one way among several.

    THE VENDOR KNOWLEDGE STAYS HERE, and storage.py only learns that some
    function can identify a member of a virtual volume. It is consulted for a
    VIRTUAL volume and never otherwise, so on a plain disk or a RAID set this
    code is not merely skipped - it is never reached.
    """
    try:
        from . import storage
        # The RAW walk, not disk_of() - see the note there. Registering the
        # public function would make storage call back into the thing that
        # called it.
        storage.register_member_finder(_disk_of_poolpart)
    except Exception:                                        # noqa: BLE001
        pass


def pool_disks() -> dict[str, str]:
    """label -> path of that disk's PoolPart folder (DrivePool-specific).

    Kept as the fallback for media_roots(); prefer that.
    """
    disks: dict[str, str] = {}
    seen: set[str] = set()

    roots = _enumerate_volumes()
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        roots.append(f"{letter}:\\")

    for root in roots:
        try:
            if not os.path.isdir(root):
                continue
            entries = os.listdir(root)
        except OSError:
            continue
        for e in entries:
            if not e.lower().startswith("poolpart."):
                continue
            full = os.path.join(root, e)
            try:
                key = os.path.realpath(full).lower()
            except OSError:
                key = full.lower()
            if key in seen:
                continue
            seen.add(key)
            disks[_volume_label(root)] = full
            break
    return disks


_DISK_CACHE: dict[str, str] = {}


def strip_extended_prefix(p: str) -> str:
    r"""Drop Windows' \\?\ extended-length prefix.

    THIS IS WHY THE PLEX DISK PROTECTION NEVER FIRED.

    Tautulli reports a session's file exactly as Plex has it, and Plex uses the
    extended-length form for long paths:

        \\?\P:\Anime Shows\ROLL OVER AND DIE - ...\...S01E03...mkv

    os.path.exists() accepts that happily, so nothing looked wrong. But
    relpath(r"\\?\P:\x", "P:\\") does not resolve - the prefixed path is not
    under P:\ as far as the string is concerned - so disk_of() returned None
    for EVERY such session. _PLEX_DISKS stayed empty, the "avoid the spindle a
    viewer is reading from" rule had nothing to avoid, and nuarr would start a
    stream copy on the exact disk being watched. Measured with a viewer on
    NU-DRIVE-0 and a passthrough job running on NU-DRIVE-0.

    Meanwhile the blunt protections - holding the encode pool, dropping I/O
    priority - fired regardless, so the cost was being paid without the benefit.

    Fixed here rather than at the call site: every caller that takes a path from
    an outside system (Plex, the arrs, a webhook) is exposed to the same form.
    """
    if p.startswith("\\\\?\\UNC\\"):        # \\?\UNC\server\share -> \\server\share
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def disk_of(pooled_path: str, pool_root: str = "P:\\") -> str | None:
    r"""Which device holds this file. Works on any storage, not just a pool.

    THE PUBLIC ANSWER, and it no longer assumes DrivePool. It asks storage.py,
    which decides what kind of volume the path is on and only falls back to the
    PoolPart walk below when the volume turns out to be virtual. On a plain
    disk, an external drive, a RAID set or a mount point the walk is not merely
    skipped - it is never reached, because the path already names its device.

    KEPT SEPARATE FROM _disk_of_poolpart() ON PURPOSE. That function is what
    gets registered with storage.py as a member finder, so if this one were the
    registered callable it would call storage, storage would call it back, and
    the two would recurse until the stack ran out. Two names, one direction.
    """
    try:
        from . import storage
        label, _how = storage.device_of(pooled_path)
        if label:
            return label
    except Exception:                                        # noqa: BLE001
        pass
    # storage.py could not answer - no Windows storage API, or a path it does
    # not recognise. The pool walk is still the best guess available.
    return _disk_of_poolpart(pooled_path, pool_root)


def _disk_of_poolpart(pooled_path: str, pool_root: str = "P:\\") -> str | None:
    r"""Which PoolPart holds this path - the DrivePool-specific probe.

    A single file lives in exactly one PoolPart, so we just ask each one whether
    it has the file - 12 stat calls, far cheaper than re-walking the pool. Used
    by the webhook path, where one file changed and a full scan is absurd.

    Registered with storage.py rather than called directly, so it runs only for
    a volume that has been identified as virtual.
    """
    global _DISK_CACHE
    if not _DISK_CACHE:
        _DISK_CACHE = pool_disks()
    pooled_path = strip_extended_prefix(pooled_path)
    try:
        rel = os.path.relpath(pooled_path, pool_root)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    for label, part in _DISK_CACHE.items():
        if os.path.exists(os.path.join(part, rel)):
            return label
    # a disk may have been added since we cached; refresh once and retry
    _DISK_CACHE = pool_disks()
    for label, part in _DISK_CACHE.items():
        if os.path.exists(os.path.join(part, rel)):
            return label
    return None



# REGISTERED AT IMPORT, not from a start-up hook. This was defined and never
# called, so storage.device_of() had no way to identify a pool member and would
# have fallen back to naming the whole pool volume - correct-looking output,
# silently coarser than before. Importing scanner is what makes the PoolPart
# probe available, and everything that needs it imports scanner already.
_register_with_storage()


# ------------------------------------------------------------------ scan ----
# slots=True because there is one of these PER FILE ON THE POOL - 39,231 of
# them live at once during a scan. A plain dataclass gives every instance a
# __dict__, measured at 296 B on top of the 48 B object; slots stores the six
# fields inline instead. That is ~10 MB of pure bookkeeping on this library,
# for a record whose actual contents are two paths and three numbers.
@dataclass(slots=True)
class DiskFile:
    path: str          # the P:\ path the arrs will use
    real_path: str     # the physical PoolPart path
    disk: str
    library: str
    size: int
    mtime: float


# How far ahead of the clock a modification time may legitimately sit. Files
# arrive from other machines and DrivePool spans twelve disks, so a few
# minutes of skew is ordinary; an hour is generous and still nowhere near the
# kind of value this exists to catch.
FUTURE_SKEW_S = 3600.0


def _sane_mtime(st, now: float) -> float:
    """A modification time that is not in the future.

    Real media carries impossible timestamps. In this library:

        Pulse (1988) ... -GAZER.mkv     modified 2097-12-31

    which is the timestamp the release was packed with, not a fact about this
    machine. Every hold in nuarr is "mtime is at least hold_minutes old", so a
    date in 2097 means held until 2097: the file would never be processed, and
    the Settles column showed the honest but useless "625816h 58m".

    Creation time is preferred as the replacement because on Windows it is
    stamped by the copy that put the file on this pool - it is a real local
    event, and crucially it does not move on the next scan. Falling back to
    `now` would restart the hold on every pass and never expire either; that
    path only runs when creation time is unusable too, and mark_eligible()
    carries a separate net for it.
    """
    mt = st.st_mtime
    if mt <= now + FUTURE_SKEW_S:
        return mt
    ct = getattr(st, "st_ctime", 0.0) or 0.0
    return ct if 0.0 < ct <= now + FUTURE_SKEW_S else now


# Live walk progress. A full pass crosses 12 disks and ~40,000 files and can
# run for minutes; "scanning all" with no numbers gives no way to tell a slow
# scan from a wedged one.
PROGRESS: dict = {"disk": None, "disk_i": 0, "disks": 0, "library": None,
                  "files": 0, "started": None, "phase": None,
                  "phase_key": None, "phase_i": 0, "phase_started": None,
                  "eta_s": None, "pct": 0.0, "rate": 0.0, "detail": None,
                  "timings": {}}

# The phases a full pass goes through, in order, with the label shown in the UI.
# A single "scanning…" string could not distinguish a 90-second disk walk from a
# 130-second Sonarr fetch, so a scan that was progressing normally looked
# identical to one that had wedged.
PHASES: list[tuple[str, str]] = [
    ("walk",      "walking pool disks"),
    ("arrs",      "querying Sonarr/Radarr"),
    ("reconcile", "reconciling against the database"),
    ("promote",   "promoting eligible files"),
    ("missing",   "verifying missing files"),
    ("unmanaged", "verifying unmanaged files"),
    ("tidy",      "tidying resolved failures"),
]

# Fallback costs, in seconds, used only until a real scan has been measured.
# Roughly the observed shape on this box: the arr fetch dominates, not the walk.
_DEFAULT_COST = {"walk": 80.0, "arrs": 130.0, "reconcile": 8.0,
                 "promote": 1.0, "missing": 2.0, "unmanaged": 2.0, "tidy": 0.5}


def _expected() -> dict:
    """Per-phase durations LEARNED FROM THE LAST SCAN.

    Fixed weights would be wrong the moment the library grows or an arr slows
    down, and the arr fetch alone has ranged from 1.7 s (Radarr) to 130 s
    (Sonarr) on this box. Remembering what each phase actually cost last time
    makes the ETA track reality instead of a guess baked in months ago.
    """
    try:
        saved = json.loads(kv_get("scan.timings") or "{}")
    except Exception:
        saved = {}
    out = dict(_DEFAULT_COST)
    for k, v in (saved or {}).items():
        try:
            v = float(v)
            if v > 0:
                out[k] = v
        except (TypeError, ValueError):
            continue
    return out


def _recompute() -> None:
    """Overall percentage and ETA from where we are in the phase list."""
    key = PROGRESS.get("phase_key")
    if not key:
        return
    exp = _expected()
    keys = [k for k, _ in PHASES]
    if key not in keys:
        return
    i = keys.index(key)

    done_cost = sum(exp.get(k, 1.0) for k in keys[:i])
    cur_cost = exp.get(key, 1.0)
    later = sum(exp.get(k, 1.0) for k in keys[i + 1:])
    total = done_cost + cur_cost + later or 1.0

    started = PROGRESS.get("phase_started") or time.time()
    in_phase = max(0.0, time.time() - started)

    # Sub-progress inside the walk is known exactly - disks completed - so use
    # it rather than time, which would drift on a disk holding far more files.
    frac = None
    if key == "walk" and PROGRESS.get("disks"):
        frac = min(1.0, (PROGRESS.get("disk_i") or 0) / PROGRESS["disks"])
    if frac is None:
        frac = min(0.99, in_phase / cur_cost) if cur_cost > 0 else 0.0

    PROGRESS["pct"] = round(min(99.5, (done_cost + cur_cost * frac) / total * 100), 1)
    remaining = cur_cost * (1.0 - frac) + later

    # AN OVERRUNNING PHASE HAS NO ETA, AND SAYING "~1s" IS A LIE.
    #
    # frac saturates at 0.99, so once a phase passes its expected cost the
    # remaining figure collapses to cur_cost*0.01 + later and then hit a 1.0
    # floor. The result was a bar pinned at 99% reading "~1s left" for twelve
    # solid minutes while "verifying missing files" ran 30x over its estimate.
    # The old comment here claimed it reported "at least the time already spent
    # overrunning" - it did not; max(remaining, 1.0) is a one second floor.
    #
    # There is no honest estimate available in that state: the estimate is the
    # thing that turned out to be wrong. So publish nothing and let the UI say
    # the phase is overrunning, with how long it has been going.
    over = in_phase - cur_cost
    if over > max(2.0, cur_cost * 0.25) and frac < 1.0:
        PROGRESS["eta_s"] = None
        PROGRESS["overrun_s"] = round(over, 1)
    else:
        PROGRESS["eta_s"] = int(max(0, round(remaining)))
        PROGRESS["overrun_s"] = None

    files, st = PROGRESS.get("files") or 0, PROGRESS.get("started")
    if files and st:
        el = max(0.001, time.time() - st)
        PROGRESS["rate"] = round(files / el, 1)


def phase(key: str, detail: str | None = None) -> None:
    """Enter a scan phase, recording what the previous one cost."""
    now = time.time()
    prev, prev_at = PROGRESS.get("phase_key"), PROGRESS.get("phase_started")
    if prev and prev_at:
        PROGRESS["timings"][prev] = round(now - prev_at, 2)
    label = dict(PHASES).get(key, key)
    keys = [k for k, _ in PHASES]
    PROGRESS.update(phase_key=key, phase=label, phase_started=now,
                    phase_i=(keys.index(key) + 1) if key in keys else 0,
                    detail=detail)
    _recompute()


def begin_scan() -> None:
    PROGRESS.update(started=time.time(), files=0, disk=None, disk_i=0,
                    library=None, phase=None, phase_key=None,
                    phase_started=None, phase_i=0, eta_s=None, pct=0.0,
                    rate=0.0, detail=None, timings={})


# How much a new measurement moves the stored estimate. The timings were
# overwritten outright by whatever the last scan happened to cost, so ONE scan
# that ran while the box was flat out set the estimate for every scan after it -
# the stored arrs cost was 164.5 s against a measured 31 s idle, and the ETA
# stayed wrong until another scan happened to run quiet. Blending keeps a single
# bad sample from taking over while still tracking a real change within a few
# passes.
_EMA_ALPHA = 0.4


def end_scan() -> None:
    """Close the last phase and persist the timings for the next scan's ETA."""
    now = time.time()
    prev, prev_at = PROGRESS.get("phase_key"), PROGRESS.get("phase_started")
    if prev and prev_at:
        PROGRESS["timings"][prev] = round(now - prev_at, 2)
    try:
        old = _expected()
        blended = {}
        for k, v in PROGRESS["timings"].items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            prior = float(old.get(k) or 0.0)
            blended[k] = round(_EMA_ALPHA * v + (1 - _EMA_ALPHA) * prior
                               if prior > 0 else v, 2)
        if blended:
            kv_set("scan.timings", json.dumps(blended))
    except Exception:
        pass
    PROGRESS.update(phase="done", phase_key=None, pct=100.0, eta_s=0,
                    disk=None, library=None, detail=None)


def progress() -> dict:
    """Snapshot for the API, with the live figures refreshed."""
    _recompute()
    return dict(PROGRESS)


def _progress_reset(n_disks: int) -> None:
    PROGRESS.update(disk=None, disk_i=0, disks=n_disks, library=None,
                    files=0, started=PROGRESS.get("started") or time.time())


def _watched_disks() -> set:
    """Spindles a Plex client is reading from right now. Never raises."""
    try:
        from . import gate
        return set(gate.plex_disks() or ())
    except Exception:
        return set()


def scan_pool(pool_root: str = "P:\\", libraries: list[str] | None = None
              ) -> dict[str, DiskFile]:
    r"""Walk every pool disk. Returns normcased P:\ path -> DiskFile.

    THE SCAN IS DISK WORK TOO, AND IT WAS THE ONE PATH THAT IGNORED VIEWERS.

    Everything else here steps around someone watching: the claimer refuses a
    job whose file is on their spindle, encoders drop I/O priority, the commit
    paces itself and now runs in background I/O mode. The scan did none of it
    while walking twelve disks and stat-ing ~39,000 files - a metadata storm,
    which on a spinning disk is the most disruptive kind of load there is,
    because it is all seeks.

    Two cheap changes rather than one clever one:

      * WATCHED SPINDLES GO LAST. A pass takes tens of seconds per disk, so
        deferring the one being read from often means arriving after the
        viewer has finished - the contention is avoided outright rather than
        merely softened.
      * BACKGROUND I/O WHILE ON ONE. If the viewer is still there when we get
        to their disk, the walk runs at Very Low I/O priority so their reads
        overtake ours. Re-checked per disk, so someone starting mid-scan is
        picked up on the next spindle.
    """
    libs = libraries or [l.name for l in SETTINGS.libraries if l.enabled]
    lib_lookup = {l.lower(): l for l in libs}
    found: dict[str, DiskFile] = {}
    # One clock reading for the whole pass. Taken per file it would drift by
    # the minutes a pass takes, which is exactly the kind of moving target
    # that makes a hold never expire.
    walk_now = time.time()

    disks = pool_disks()
    # QUIETEST FIRST, BUSIEST LAST - and a viewer's disk last of all.
    #
    # Deferring a viewer's spindle was already here and works for the reason
    # written above: a walk takes minutes, so arriving late often means the
    # contention never happens. The same argument applies to a disk that is
    # merely busy, and nothing was using it - the walk marched through 0, 1,
    # 2... regardless of what any of them were doing, so a scan starting while
    # an encode saturated NU-DRIVE-0 went straight at it.
    #
    # Ordering rather than skipping. Every disk still gets walked in the same
    # pass; the only question is when, and answering it costs nothing because
    # the load figures are already sampled continuously for the gate.
    watched = _watched_disks()
    try:
        from . import diskload
        load = {r["name"]: r["busy"] for r in diskload.sustained().values()}
        keys = {}
        for lbl in disks:
            k = diskload.key_for_path(disks[lbl])
            keys[lbl] = load.get(k, 0.0) if k else 0.0
    except Exception:
        keys = {lbl: 0.0 for lbl in disks}
    # A viewer outranks any amount of measured load: 40% busy from a remux is
    # something to work around, a person watching is something to stay off.
    order = sorted(disks, key=lambda l: (1 if l in watched else 0, keys.get(l, 0.0)))
    if order != list(disks):
        head, tail = order[0], order[-1]
        joblog.log(
            f"scan order: starting on {head} ({keys.get(head, 0):.0f}% busy), "
            f"leaving {tail} until last"
            + (f" — someone is watching from {', '.join(sorted(watched))}"
               if watched else f" ({keys.get(tail, 0):.0f}% busy)"), "debug")
        disks = {l: disks[l] for l in order}

    from .fileops import _BackgroundIO
    bg = _BackgroundIO()
    _progress_reset(len(disks))
    try:
        for i, (label, part) in enumerate(disks.items(), 1):
            # Re-read per disk: viewers start and stop during a pass.
            bg.set(label in _watched_disks())
            PROGRESS.update(disk=label, disk_i=i)
            for lib_dir in os.listdir(part) if os.path.isdir(part) else []:
                real_lib = lib_lookup.get(lib_dir.lower())
                if not real_lib:
                    continue
                PROGRESS["library"] = real_lib
                base = os.path.join(part, lib_dir)
                for dirpath, _dirs, files in os.walk(base):
                    for fn in files:
                        if os.path.splitext(fn)[1].lower() not in MEDIA_EXT:
                            continue
                        real = os.path.join(dirpath, fn)
                        rel = os.path.relpath(real, part)
                        pooled = os.path.join(pool_root, rel)
                        # check the pooled P:\ form - that is what exclusions
                        # are written against, not the per-disk PoolPart path
                        if is_excluded(pooled) or is_excluded(real):
                            continue
                        try:
                            st = os.stat(real)
                        except OSError:
                            continue
                        found[os.path.normcase(pooled)] = DiskFile(
                            path=pooled, real_path=real, disk=label,
                            library=real_lib,
                            size=st.st_size, mtime=_sane_mtime(st, walk_now),
                        )
                        PROGRESS["files"] = len(found)
    finally:
        # Hand the pooled thread back at normal priority, or whatever runs on
        # it next inherits Very Low I/O for the life of the process.
        bg.set(False)

    # ---- LIBRARIES THAT ARE NOT ON THE POOL AT ALL -------------------------
    # Everything above walks DrivePool's PoolPart folders, which is right for
    # the pool and DEAD WRONG as the only path: a library on a plain folder -
    # C:\Movies on an ordinary PC, or \\server\share from a machine that
    # mounts its media over the network - matched no PoolPart and was simply
    # never walked. The scan reported disks=0, on_disk=0, and the library sat
    # at zero files forever while its folder plainly held media. Found on the
    # first install whose storage was not this dev machine's pool, which is
    # to say: found on the first install.
    #
    # These get a direct walk instead. The "disk" label - which everything
    # downstream treats as an opaque spindle name - becomes the share root
    # (\\server\share) or the drive letter, which is exactly the granularity
    # the OS actually offers here: one filesystem, one queue, one label. The
    # per-spindle machinery keys off those labels and degrades honestly to
    # per-filesystem.
    pooled_norm = os.path.normcase(os.path.normpath(pool_root))
    for lib in SETTINGS.libraries:
        if not lib.enabled or lib.name not in libs:
            continue
        lp = os.path.normpath(lib.path)
        if os.path.normcase(lp).startswith(pooled_norm):
            continue                 # on the pool - the PoolPart walk owns it
        if not os.path.isdir(lp):
            continue                 # missing folders are reported elsewhere
        if lp.startswith("\\\\"):
            parts = lp.lstrip("\\").split("\\")
            label = "\\\\" + "\\".join(parts[:2]) if len(parts) >= 2 else lp
        else:
            label = os.path.splitdrive(lp)[0] or lp
        PROGRESS.update(disk=label, library=lib.name)
        for dirpath, _dirs, files in os.walk(lp):
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in MEDIA_EXT:
                    continue
                real = os.path.join(dirpath, fn)
                if is_excluded(real):
                    continue
                try:
                    st = os.stat(real)
                except OSError:
                    continue
                # path == real_path here: there is no pooled alias to translate
                # to, the path the scanner saw IS the path everything else uses.
                found[os.path.normcase(real)] = DiskFile(
                    path=real, real_path=real, disk=label,
                    library=lib.name,
                    size=st.st_size, mtime=_sane_mtime(st, walk_now),
                )
                PROGRESS["files"] = len(found)

    PROGRESS.update(disk=None, library=None)
    return found


# --------------------------------------------------------------- reconcile ---
@dataclass
class ScanReport:
    disks: int = 0
    on_disk: int = 0
    from_arrs: int = 0
    inserted: int = 0
    renamed: int = 0
    moved_disk: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    superseded: int = 0
    orphans: int = 0
    duplicates: int = 0
    seconds: float = 0.0
    # How much of the reconcile was spent hashing orphan files rather than in
    # memory or SQLite. The rest of the phase measures ~1.1 s, so this is the
    # number that explains a slow one.
    sig_seconds: float = 0.0
    # Orphans whose signature had to be READ off disk, as opposed to reused
    # from the row we already had. The gap between this and `orphans` is the
    # saving.
    sig_hashed: int = 0
    sweep_skipped: str | None = None
    arr_status: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"disks={self.disks} on_disk={self.on_disk:,} arr_files={self.from_arrs:,}\n"
            f"inserted={self.inserted:,} renamed={self.renamed:,} "
            f"moved_disk={self.moved_disk:,} changed={self.changed:,} "
            f"unchanged={self.unchanged:,}\n"
            f"missing={self.missing:,} superseded={self.superseded:,} "
            f"orphans(not in arrs)={self.orphans:,} "
            f"byte-identical duplicates={self.duplicates:,} "
            f"in {self.seconds:.1f}s"
            + (f"\n! {self.sweep_skipped}" if self.sweep_skipped else "")
        )


OUTSIDE = "(outside configured libraries)"


def is_excluded(path: str | None) -> bool:
    """Is this path under a configured exclusion?

    Applied to BOTH sources of truth. The pool walk is the obvious one, but the
    case that actually bit was arr-sourced: Sonarr had series rooted outside the
    pool, so 37 rows arrived through the API with no mtime and no pool disk and
    could never be promoted. Filtering only the walk would have left them.
    """
    if not path:
        return False
    n = os.path.normcase(os.path.normpath(path))
    for raw in getattr(SETTINGS, "exclude_paths", []) or []:
        b = os.path.normcase(os.path.normpath(raw)).rstrip(os.sep)
        if n == b or n.startswith(b + os.sep):
            return True
    return False


def _library_of(path: str) -> str | None:
    """Which configured library owns this path.

    Returns OUTSIDE rather than guessing a name from the path. Guessing invented
    a phantom library called 'BackUp Data' out of 24 Sonarr files that are
    stored off-pool - which reads as a real library in any report and hides the
    actual problem, that those files are outside every configured root.
    """
    norm = os.path.normcase(os.path.normpath(path))
    for l in SETTINGS.libraries:
        base = os.path.normcase(os.path.normpath(l.path))
        if norm.startswith(base + os.sep):
            return l.name
    return OUTSIDE


async def scan(full: bool = True, probe_orphans: bool = True,
               libraries: list[str] | None = None) -> ScanReport:
    """One reconciliation pass: disk truth + arr truth -> database.

    `libraries` limits the walk to named libraries. A single-library rescan is
    seconds rather than the ~3 minutes a full pass takes, which makes "I just
    changed one show" practical. The missing-sweep is disabled for a partial
    scan, since it only saw part of the world.
    """
    t0 = time.time()
    rep = ScanReport()
    if libraries:
        full = False

    begin_scan()
    phase("walk")
    disk_map = await asyncio.to_thread(scan_pool, libraries=libraries)
    rep.on_disk = len(disk_map)
    rep.disks = len(await asyncio.to_thread(pool_disks))
    # Hand the walk's answer to verify_missing so it does not re-ask the
    # filesystem 39,000 times. FULL scans only - a partial walk covers one
    # library and would make every file in the others look missing.
    if full and not libraries:
        LAST_ON_DISK.update(paths=set(disk_map.keys()), at=time.time())
    else:
        LAST_ON_DISK.update(paths=None, at=0.0)

    # Separate phase, not a footnote on the walk. Querying the arrs takes long
    # enough that a single combined label left the UI reading "walking disks"
    # with a frozen 12/12 counter - indistinguishable from a hung scan.
    phase("arrs")
    arr_files, arr_status = await gather_arr_files(SETTINGS.arrs, _prev_sigs())

    # EVERYTHING BELOW RUNS IN A THREAD.
    #
    # scan() contains exactly one await - the arr fetch on the line above.
    # The other ~270 lines are synchronous: a pool walk, quick_sig() reads on
    # orphan files, and several executemany passes over 39,000 rows. Sitting
    # directly in a coroutine, all of it held the event loop, so uvicorn could
    # not answer anything for the duration of a scan. That is the whole reason
    # the dashboard felt frozen: /api/startup, which reads a dict in memory,
    # was measured at 20 s during a scan and 7 ms outside one.
    #
    # A nested function rather than a module-level one so the reconcile body
    # keeps reading against the locals it already used. arr_files is passed as
    # a default argument because it is REBOUND below (_reconstruct_skipped);
    # left as a closure reference that assignment would make it local to this
    # function and raise UnboundLocalError before the first statement ran.
    # Everything else here is only read or mutated in place, so plain closure
    # capture is correct for it.
    def _reconcile(arr_files=arr_files):
        arr_files = _reconstruct_skipped(arr_files, arr_status)
        _store_sigs(arr_status)
        # Both of those keys are working data - a 1,085-entry signature map and a
        # list of every skipped series id, per arr. arr_status is handed to the
        # dashboard and kept in LAST_ARR_FETCH, so leaving them in would ship
        # thousands of ids to the browser on every poll. Replace them with the one
        # number anybody looking at this actually wants.
        for _st in arr_status.values():
            _st.pop("series_sig", None)
            _st["skipped_parents"] = len(_st.get("skipped_parents") or ())
        rep.from_arrs = len(arr_files)
        rep.arr_status = arr_status
        # Publish it for the verify passes that run right after this scan, so they
        # reuse this fetch instead of each paying another ~26 s and another 39,000
        # transient objects. Cleared when it goes stale.
        LAST_ARR_FETCH.update(files=arr_files, status=arr_status, at=time.time())

        now = time.time()
        phase("reconcile")

        with cursor() as cur:
            rows = cur.execute(
                "SELECT id, arr_name, arr_file_id, content_sig, path, size, mtime, "
                "pool_disk, state, state_reason FROM files"
            ).fetchall()

            by_arr = {(r["arr_name"], r["arr_file_id"]): r for r in rows
                      if r["arr_file_id"] is not None}
            by_sig = {r["content_sig"]: r for r in rows if r["content_sig"]}
            # Indexed by PATH as well, so an orphan's stored signature can be
            # reused instead of re-read off the pool. See the orphan loop.
            by_path = {os.path.normcase(r["path"]): r for r in rows if r["path"]}
            seen_ids: set[int] = set()

            inserts, updates, events, dup_inserts = [], [], [], []
            sig_inserts: list = []
            matched_paths: set[str] = set()
            sig_claimed: dict[str, str] = {s: r["path"] for s, r in by_sig.items()}

            # A PARTIAL scan only walked SOME libraries, so it only knows about
            # those files. Reconciling every arr file against that narrow view
            # wiped pool_disk / size / mtime for everything else - 97.8% of the
            # library lost its disk placement after one per-library rescan.
            scope = None
            if libraries:
                scope = {os.path.normcase(os.path.normpath(l.path))
                         for l in SETTINGS.libraries if l.name in libraries}

            def in_scope(p: str) -> bool:
                if scope is None:
                    return True
                n = os.path.normcase(os.path.normpath(p))
                return any(n.startswith(b + os.sep) for b in scope)

            # THE LIBRARIES ARE THE BOUNDARY. An arr usually manages more
            # roots than nuarr was given - a one-library install against a
            # ten-root Sonarr used to inherit all ten as "(outside configured
            # libraries)" rows, and the dashboard read like nuarr managed the
            # lot. Files outside every configured root are counted and
            # logged, never inserted: adding the library is the way to opt
            # its files in.
            outside_n = 0
            for af in arr_files:
                if not af.path or not in_scope(af.path) or is_excluded(af.path):
                    continue
                if _library_of(af.path) == OUTSIDE:
                    outside_n += 1
                    continue
                key = os.path.normcase(af.path)
                matched_paths.add(key)
                df = disk_map.get(key)
                size = df.size if df else af.size
                mtime = df.mtime if df else None
                disk = df.disk if df else None
                lib = df.library if df else _library_of(af.path)

                prev = by_arr.get((af.arr_name, af.file_id))
                if prev is None:
                    inserts.append((
                        af.arr_name, af.file_id, af.parent_id, None, af.path, lib,
                        af.title, af.season, af.episode, size, mtime, disk,
                        "new", None, now, now, now,
                    ))
                    rep.inserted += 1
                    continue

                seen_ids.add(prev["id"])
                state = prev["state"]
                # THE REASON IS THE AUDIT TRAIL FOR EXACTLY TWO STATES, and this
                # was wiping it on every pass. A row parked at 'deleted' by a
                # refetch carries "rejected and re-searched: blocklisted <x>,
                # asked Sonarr to search again" - the entire record of a
                # decision nuarr made on your behalf. The next scan re-found the
                # file, wrote the state back unchanged, and set the reason to
                # NULL, leaving fourteen rows that said 'deleted' and could not
                # say why.
                #
                # Found by the not-walked check: it could see the files were
                # written off and had nothing left to tell it whether that was
                # deliberate. Clearing the reason is right for the ordinary
                # states, where it describes a condition that has passed; for
                # 'deleted' and 'error' it describes a verdict that has not.
                reason = (prev["state_reason"]
                          if state in ("deleted", "error") else None)

                # A RENAME. The row keeps its identity and simply learns a new path -
                # this is the single behaviour that stops a rename resetting work.
                if os.path.normcase(prev["path"] or "") != key:
                    rep.renamed += 1
                    events.append((prev["id"], "renamed",
                                   f"{prev['path']} -> {af.path}", now))

                if disk and prev["pool_disk"] and disk != prev["pool_disk"]:
                    rep.moved_disk += 1
                    events.append((prev["id"], "moved_disk",
                                   f"{prev['pool_disk']} -> {disk}", now))

                # CONTENT changed - that does invalidate prior processing
                content_changed = (
                    prev["size"] is not None and size is not None
                    and prev["size"] != size
                )
                if content_changed:
                    rep.changed += 1
                    state, reason = "new", "content changed since last pass"
                    events.append((prev["id"], "content_changed",
                                   f"{prev['size']} -> {size} bytes", now))
                else:
                    rep.unchanged += 1

                updates.append((
                    af.parent_id, af.path, lib, af.title, af.season, af.episode,
                    size, mtime, disk, state, reason, now, now, prev["id"],
                ))

            # ---------------- files on disk the arrs know nothing about ----------
            # TIMED, because this is the only part of reconcile that touches the
            # DISK rather than memory or SQLite. quick_sig() reads 8 MB from the
            # head and 8 MB from the tail of each orphan, and on a pool the
            # encoders are already saturating those reads queue behind a 200 MB/s
            # copy. Everything else in this phase measured at ~1.1 s combined,
            # so if the phase costs 24 s this is where it went - but that is a
            # claim worth a number rather than an assumption.
            _sig_s = 0.0
            for key, df in disk_map.items():
                if key in matched_paths:
                    continue
                rep.orphans += 1
                sig = None
                if probe_orphans:
                    # DO NOT RE-HASH A FILE THAT HAS NOT CHANGED.
                    #
                    # A signature is size + blake2b of the first and last 8 MB.
                    # If we already stored one for this exact path, and the walk
                    # reports the same size and mtime, the bytes cannot have
                    # changed and neither can the signature - so reading 16 MB
                    # off the pool to recompute a value we already hold is pure
                    # cost. Measured: 7 orphans, 14.5 s, every single scan,
                    # because these are files the arrs never adopt and so they
                    # stay orphans forever and got re-hashed forever.
                    #
                    # The tail read is what hurts. It is a seek to the end of a
                    # multi-GB file on a spindle the encoders are saturating,
                    # which is why 16 MB can take two seconds.
                    prev_row = by_path.get(key)
                    if (prev_row and prev_row["content_sig"]
                            and prev_row["size"] == df.size
                            and prev_row["mtime"] is not None
                            and abs(prev_row["mtime"] - df.mtime) < 0.001):
                        sig = prev_row["content_sig"]
                    else:
                        _t = time.time()
                        try:
                            sig = fileops.quick_sig(df.real_path)
                        except OSError:
                            sig = None
                        _sig_s += time.time() - _t
                        rep.sig_hashed += 1
                if not sig:
                    continue
                prev = by_sig.get(sig)
                if prev is None:
                    # A signature can legitimately repeat: the library really does
                    # contain byte-identical copies of the same file. Identity must
                    # stay unique, so the FIRST file owns the signature and later
                    # ones are recorded as duplicates (with the sig in the reason)
                    # instead of blowing up the whole scan on a UNIQUE violation.
                    owner = sig_claimed.get(sig)
                    if owner is None:
                        sig_claimed[sig] = df.path
                        # kept separate from the arr-keyed inserts: these conflict
                        # on ux_files_sig, a DIFFERENT partial index, so they need
                        # their own conflict target
                        sig_inserts.append((
                            None, None, None, sig, df.path, df.library, None, None,
                            None, df.size, df.mtime, df.disk, "new", None, now, now, now,
                        ))
                        rep.inserted += 1
                    else:
                        dup_inserts.append((
                            None, None, None, None, df.path, df.library, None, None,
                            None, df.size, df.mtime, df.disk, "duplicate",
                            f"byte-identical to {owner} (sig {sig})", now, now, now,
                        ))
                        rep.duplicates += 1
                else:
                    seen_ids.add(prev["id"])
                    if os.path.normcase(prev["path"] or "") != key:
                        rep.renamed += 1
                        events.append((prev["id"], "renamed",
                                       f"{prev['path']} -> {df.path}", now))
                    updates.append((
                        None, df.path, df.library, None, None, None, df.size, df.mtime,
                        df.disk, prev["state"],
                        # Same rule as the arr-keyed branch above: a verdict
                        # keeps its explanation, a passing condition does not.
                        (prev["state_reason"]
                         if prev["state"] in ("deleted", "error") else None),
                        now, now, prev["id"],
                    ))

            # UPSERT, not a bare INSERT. A plain INSERT aborts the whole pass the
            # moment one row already exists:
            #     UNIQUE constraint failed: files.arr_name, files.arr_file_id
            #
            # This is a read-then-write race with a ~100-SECOND WINDOW. The pass
            # snapshots `files` up front, then spends a minute and a half walking
            # 12 disks and querying Sonarr before it writes. Anything that creates
            # a file row in between is invisible to the snapshot, so the scan still
            # thinks the file is new and inserts a row that now already exists.
            #
            # The other writer is the WEBHOOK handler: an arr import fires
            # on_file_imported, which inserts immediately. So the failure needs no
            # second scan at all - it just needs Sonarr to import something while a
            # scan is in flight, which during an upgrade batch is close to certain.
            # (Observed: a scan started 18:33:10 died at 18:35:18 with no other
            # scan running, while Sonarr's file count moved 37,197 -> 37,206.)
            #
            # Losing an entire reconciliation because one row arrived early is a
            # wildly disproportionate failure. Upserting makes the write correct
            # under any concurrent writer rather than only under a lock we
            # remembered to take.
            #
            # first_seen is deliberately NOT updated on conflict - it is the one
            # column that should keep its original value.
            _INSERT_SQL = (
                "INSERT INTO files(arr_name,arr_file_id,arr_parent_id,content_sig,"
                "path,library,title,season,episode,size,mtime,pool_disk,state,"
                "state_reason,first_seen,last_seen,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            )
            # ux_files_arr and ux_files_sig are PARTIAL indexes. SQLite will not
            # match a conflict target to a partial index unless the target repeats
            # the index's WHERE clause verbatim - omit it and you get
            # "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
            # constraint" at runtime, which is a worse failure than the one being
            # fixed. Hence two statements rather than one.
            _SET = (
                " DO UPDATE SET "
                "arr_parent_id=excluded.arr_parent_id, path=excluded.path, "
                "library=excluded.library, title=excluded.title, "
                "season=excluded.season, episode=excluded.episode, "
                "size=excluded.size, mtime=excluded.mtime, "
                "pool_disk=excluded.pool_disk, last_seen=excluded.last_seen, "
                "updated_at=excluded.updated_at"
            )
            _UPSERT_ARR = (_INSERT_SQL +
                           "ON CONFLICT(arr_name,arr_file_id) "
                           "WHERE arr_file_id IS NOT NULL" + _SET)
            _UPSERT_SIG = (_INSERT_SQL +
                           "ON CONFLICT(content_sig) "
                           "WHERE content_sig IS NOT NULL AND arr_file_id IS NULL"
                           + _SET)
            if inserts:
                cur.executemany(_UPSERT_ARR, inserts)
            if sig_inserts:
                cur.executemany(_UPSERT_SIG, sig_inserts)
            # duplicates carry neither key, so no unique index applies to them
            if dup_inserts:
                cur.executemany(_INSERT_SQL, dup_inserts)
            if updates:
                cur.executemany(
                    "UPDATE files SET arr_parent_id=?, path=?, library=?, title=?, "
                    "season=?, episode=?, size=?, mtime=?, pool_disk=?, state=?, "
                    "state_reason=?, last_seen=?, updated_at=? WHERE id=?", updates)
                # A subtitle-OCR rejection belongs to the BYTES that were
                # rejected. When the content changes - an upgrade replacing the
                # file under the same row - the old verdict must not follow the
                # new file, or an upgrade whose predecessor had no usable subs
                # would be silently banned from OCR forever.
                changed_ids = [ev[0] for ev in events
                               if ev[1] == "content_changed"]
                if changed_ids:
                    qs = ",".join("?" * len(changed_ids))
                    cur.execute(f"UPDATE files SET subocr_state=NULL "
                                f"WHERE id IN ({qs}) AND subocr_state "
                                f"IS NOT NULL", changed_ids)
                    # The adoption verdict belongs to the old bytes for the
                    # same reason: 'duplicate' or 'orphan' was decided about a
                    # file that no longer exists, and carrying it forward
                    # could feed a brand-new file to the leftover cleanup.
                    cur.execute(f"UPDATE files SET adopt_state=NULL, "
                                f"adopt_attempts=0 WHERE id IN ({qs}) "
                                f"AND adopt_state IS NOT NULL", changed_ids)
            if events:
                cur.executemany(
                    "INSERT INTO history(file_id,event,detail,at) VALUES(?,?,?,?)",
                    events)

            # ------------------------------------------------- gone this pass ----
            # Only sweep when the pass is TRUSTWORTHY. If an arr was unreachable, or
            # no pool disk enumerated, this pass saw an artificially small world and
            # sweeping would mark thousands of healthy files missing.
            enabled = [a for a in SETTINGS.arrs if a.enabled and a.api_key]
            arrs_ok = enabled and all(arr_status.get(a.name, {}).get("ok")
                                      for a in enabled)
            trustworthy = full and arrs_ok and rep.disks > 0 and rep.on_disk > 0

            # Rows from before the library boundary existed - arr files
            # outside every configured root, inherited by older databases.
            # Bookkeeping rows only, so deleting them touches no media; left
            # in place they would all flip to "missing" the moment the sweep
            # below runs, which is a worse lie than absence.
            stale_outside = [r["id"] for r in rows
                             if r["id"] not in seen_ids
                             and r["path"]
                             and _library_of(r["path"]) == OUTSIDE]
            if stale_outside:
                cur.executemany("DELETE FROM files WHERE id=?",
                                [(i,) for i in stale_outside])
            if outside_n or stale_outside:
                joblog.log(
                    f"outside configured libraries: {outside_n} arr file(s) "
                    f"not managed"
                    + (f", {len(stale_outside)} old row(s) cleaned up"
                       if stale_outside else "")
                    + " - add the library to opt them in", "info")

            if full and not trustworthy:
                rep.sweep_skipped = (
                    f"missing-sweep skipped: arrs_ok={bool(arrs_ok)} "
                    f"disks={rep.disks} on_disk={rep.on_disk}"
                )

            if trustworthy:
                gone = ({r["id"] for r in rows} - seen_ids
                        - set(stale_outside))

                # SUPERSEDED, NOT MISSING.
                # When an arr re-imports a file it issues a NEW file id. Identity is
                # (arr, file_id), so that creates a new row and strands the old one
                # - which then gets swept as "missing" even though the file is right
                # there under the same path. 611 rows ended up like that after the
                # filename repairs. If another live row already owns the path, the
                # old row is simply out of date: delete it rather than reporting a
                # file that is not actually gone.
                if gone:
                    live_paths = {os.path.normcase(r["path"] or "")
                                  for r in rows if r["id"] in seen_ids}
                    superseded = [r["id"] for r in rows
                                  if r["id"] in gone
                                  and os.path.normcase(r["path"] or "") in live_paths]
                    if superseded:
                        cur.executemany("DELETE FROM files WHERE id=?",
                                        [(i,) for i in superseded])
                        gone -= set(superseded)
                        rep.superseded = len(superseded)
                if gone:
                    # Do NOT delete. A disk that failed to enumerate would otherwise
                    # wipe thousands of rows and all their processing state.
                    cur.executemany(
                        "UPDATE files SET state='missing', state_reason='not seen in "
                        "last full scan', updated_at=? WHERE id=? AND state!='missing'",
                        [(now, i) for i in gone])
                    rep.missing = len(gone)

        rep.seconds = time.time() - t0
        rep.sig_seconds = round(_sig_s, 2)
        return rep

    return await asyncio.to_thread(_reconcile)


# The most recent full arr fetch, so the verify passes that run immediately
# after a scan can reuse it. Holds ~39k objects, so it is dropped as soon as it
# is consumed rather than kept resident between scans.
LAST_ARR_FETCH: dict = {"files": None, "status": None, "at": 0.0}
ARR_FETCH_TTL_S = 300.0

# WHAT THE WALK ALREADY PROVED IS ON DISK.
#
# scan_pool() has just enumerated every media file on all twelve pool disks and
# knows their P:\ paths. verify_missing() then threw that away and asked the
# filesystem the same question 39,244 more times, one os.path.exists() per arr
# record.
#
# Measured on this box: the median stat is 0.18 ms, so the loop "should" cost
# 7 s - but a small tail of calls take 400-1,500 ms each when the spindles are
# busy, and the phase came in at 54 s idle-ish and 772 s while six passthrough
# jobs and a rename batch were hammering the same disks. The tail is not
# controllable; the number of chances to hit it is.
#
# Only the path SET is kept, not the DiskFile objects - those are the scan's
# heap high-water and there is no reason to hold 39,000 of them past the walk.
# The strings are the dict's own keys, so the set costs pointers, not copies.
LAST_ON_DISK: dict = {"paths": None, "at": 0.0}


def take_arr_fetch():
    """Consume the cached fetch if it is fresh, then release it."""
    if (LAST_ARR_FETCH["files"] is not None
            and time.time() - LAST_ARR_FETCH["at"] < ARR_FETCH_TTL_S):
        f, s = LAST_ARR_FETCH["files"], LAST_ARR_FETCH["status"]
        return f, s
    return None, None


def drop_arr_fetch() -> None:
    LAST_ARR_FETCH.update(files=None, status=None, at=0.0)
    LAST_ON_DISK.update(paths=None, at=0.0)


def take_on_disk() -> set | None:
    """The walk's path set, if this scan produced one recently.

    Only valid for a FULL scan: a partial walk covers one library, so using it
    would report every file in every other library as missing.
    """
    if (LAST_ON_DISK["paths"] is not None
            and time.time() - LAST_ON_DISK["at"] < ARR_FETCH_TTL_S):
        return LAST_ON_DISK["paths"]
    return None


# --- skipping unchanged series in the arr fetch -----------------------------
# The arrs phase is ~96% of a scan (about 31 s against this Sonarr, versus 6 s
# to walk all twelve pool disks). It is one /episodefile call per series, and
# measurement showed the concurrency is already optimal - 12 parallel requests
# beat 24, 48 and 96, because Sonarr serialises them internally. The only way
# to make it faster is to ask for less.
#
# /series is a SINGLE call (~0.7 s) and carries per-series statistics. If a
# series' file count, total size on disk and folder path are all unchanged, its
# files are almost certainly unchanged too, and the copies already in nuarr's
# own files table are still good.
#
# "Almost certainly" is doing real work in that sentence, so:
#   * the skip list is rebuilt from scratch every scan, never accumulated
#   * FULL_REFRESH_S forces a complete fan-out periodically no matter what
#   * anything unexpected (no signatures, no stored baseline) fetches everything
#
# The gap it leaves is an episode-level rename, which changes neither count nor
# size nor folder. Those arrive over the webhook stream in real time, and the
# forced full pass below is the backstop if one is ever missed.
FULL_REFRESH_S = 86400.0
_SIG_KEY = "arr.series_sig"


def _prev_sigs() -> dict[str, dict]:
    """Signatures from the last fetch, or nothing if a full pass is due."""
    if time.time() - float(kv_get("arr.last_full_fetch") or 0.0) > FULL_REFRESH_S:
        return {}                      # periodic full pass: skip nothing
    try:
        raw = json.loads(kv_get(_SIG_KEY) or "{}")
    except Exception:
        return {}
    # JSON object keys are strings; the ids compared against them are ints.
    return {arr: {int(k): v for k, v in sigs.items()}
            for arr, sigs in (raw or {}).items() if isinstance(sigs, dict)}


def _store_sigs(status: dict) -> None:
    """Remember this fetch's signatures, and when we last fetched everything."""
    out = {name: st.get("series_sig") or {}
           for name, st in (status or {}).items()
           if st.get("ok") and st.get("series_sig")}
    if not out:
        return
    kv_set(_SIG_KEY, json.dumps(out))
    # "Full" means nothing was skipped anywhere - that is the pass whose
    # freshness the 24 h backstop is measured from.
    if not any(st.get("skipped_parents") for st in status.values()):
        kv_set("arr.last_full_fetch", str(time.time()))


def _reconstruct_skipped(arr_files: list, status: dict) -> list:
    """Re-add records for series we deliberately did not fetch.

    THIS IS THE SAFETY-CRITICAL HALF of skipping. Reconcile treats an arr file
    that is absent from this list as gone, so returning a short list without
    filling the gap would mark thousands of healthy files missing or unmanaged.

    Only the eight fields reconcile actually reads are rebuilt; quality and
    media_info are not consumed anywhere (verified) and are left unset.
    """
    skipped = {name: set(st.get("skipped_parents") or ())
               for name, st in (status or {}).items() if st.get("ok")}
    if not any(skipped.values()):
        return arr_files
    have = {(a.arr_name, a.file_id) for a in arr_files}
    added = 0
    with cursor() as cur:
        for arr_name, sids in skipped.items():
            if not sids:
                continue
            # Chunked so the IN list stays within SQLite's parameter limit.
            sid_list = list(sids)
            for i in range(0, len(sid_list), 500):
                chunk = sid_list[i:i + 500]
                qs = ",".join("?" * len(chunk))
                for r in cur.execute(
                    f"SELECT arr_name, arr_file_id, arr_parent_id, path, title, "
                    f"season, episode, size FROM files "
                    f"WHERE arr_name=? AND arr_parent_id IN ({qs}) "
                    f"AND arr_file_id IS NOT NULL",
                        (arr_name, *chunk)):
                    key = (r["arr_name"], r["arr_file_id"])
                    if key in have:
                        continue
                    arr_files.append(ArrFile(
                        arr_name=r["arr_name"], file_id=r["arr_file_id"],
                        parent_id=r["arr_parent_id"], path=r["path"] or "",
                        title=r["title"] or "", season=r["season"],
                        episode=r["episode"], size=r["size"] or 0,
                        quality=None))
                    have.add(key)
                    added += 1
    if added:
        joblog.log(f"arr fetch: reused {added:,} record(s) from "
                   f"{sum(len(v) for v in skipped.values()):,} unchanged "
                   f"series", "debug")
    return arr_files


_EP_RE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")


def _ep_key(path: str | None) -> str | None:
    """folder + episode token - identifies 'the same episode, any filename'."""
    m = _EP_RE.search(os.path.basename(path or ""))
    if not m:
        return None
    return (f"{os.path.dirname(path or '').lower()}|"
            f"s{int(m.group(1)):02d}e{int(m.group(2)):02d}")


async def verify_missing(arr_files=None, status=None) -> dict:
    r"""Decide what is missing by ASKING THE ARRS, not by trusting our own rows.

    The original definition was backwards. It meant "nuarr has a row and the
    path no longer exists", which measures nuarr's database lagging reality -
    not a missing file. It reported 766, then 42 after the stale sweep, while
    Sonarr and Radarr both showed nothing missing. Checking against the arrs,
    all 42 were rows for file records the arrs had already dropped.

    A file is genuinely missing when THE ARR STILL TRACKS A FILE RECORD AND
    THAT PATH IS NOT ON DISK. That is a real integrity problem: the arr
    believes it has a file it cannot serve. Anything else is our own staleness.

    So this runs in both directions:
      * every 'missing' row the arrs no longer know about  -> delete, it is ours
      * every arr file record whose path is absent on disk -> mark missing
    """
    # Reuse the caller's fetch when there is one. A full pass costs ~26 s and
    # materialises 39,000 objects; the scan had already paid for it moments
    # earlier, and verify_missing + verify_unmanaged were each paying it again.
    t0 = time.time()
    refetched = False
    if arr_files is None:
        # THIS is the expensive branch, not the stat loop. A fresh fan-out over
        # every series costs ~40 s and 39,000 objects. It should never happen
        # inside a scan, because scan() published its own fetch moments earlier
        # - so if it does, the timing needs to say so rather than leaving the
        # cost attributed to the file checks.
        refetched = True
        arr_files, status = await gather_arr_files(SETTINGS.arrs)
    fetch_s = time.time() - t0
    status = status or {}
    if not any((s or {}).get("ok") for s in status.values()):
        # Never mass-delete on the word of an unreachable arr.
        return {"skipped": "no arr reachable", "status": status}

    out = await asyncio.to_thread(_verify_missing_sync, arr_files, status,
                                  take_on_disk())
    out["arr_refetched"] = refetched
    out["fetch_s"] = round(fetch_s, 2)
    out["total_s"] = round(time.time() - t0, 2)
    return out


def _verify_missing_sync(arr_files, status, on_disk=None) -> dict:
    r"""The blocking half of verify_missing, kept OFF the event loop.

    This was inline in the async function, which made it a stop-the-world pause
    for the entire web server. The loop below calls os.path.exists() once per
    arr file record - 39,000 stat calls - against twelve letterless DrivePool
    disks that ffmpeg is already saturating. Each stat is fast when the spindle
    is idle and not fast at all when it is queued behind a 200 MB/s copy.

    Nothing yields in that loop, so uvicorn could not service a single request
    until the whole pass finished. Measured from the browser: /api/startup, a
    handler that reads a dict in memory and normally answers in 7 ms, took over
    8 seconds; a queue fetch took 22. py-spy found the process at 0.1% CPU with
    MainThread parked on this exact line - idle, but holding the loop.

    That also explains why the latency looked random and unattributable: it had
    nothing to do with the endpoint being called, only with whether a scheduled
    scan happened to be in this loop at the time.
    """
    live_ids = {(a.arr_name, a.file_id) for a in arr_files}
    now = time.time()
    truly, restored, dropped = [], [], []

    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path, arr_name, arr_file_id, state FROM files "
            "WHERE state IN ('missing','new','eligible','done','error')")]
        by_key = {(r["arr_name"], r["arr_file_id"]): r for r in rows
                  if r["arr_file_id"] is not None}

        # 1. our 'missing' rows the arrs have forgotten -> stale, remove
        for r in rows:
            if r["state"] != "missing":
                continue
            if (r["arr_name"], r["arr_file_id"]) not in live_ids:
                dropped.append(r["id"])
            elif r["path"] and os.path.exists(r["path"]):
                restored.append(r["id"])

        # 2. arr records pointing at files that are not there -> really missing
        #
        # THE WALK ALREADY ANSWERED THIS for every file it saw, so its set is
        # the fast path and a stat is only paid when the set says a path is
        # absent. That ordering matters for correctness, not just speed:
        #
        #   in the set     -> it was there minutes ago. Trust it. A deletion in
        #                     that window is caught by the next scan, which is
        #                     the same latency the three-strike heal already
        #                     assumes.
        #   not in the set -> could be genuinely gone, or could have been
        #                     written AFTER the walk. Stat it and be sure,
        #                     because this is the branch that marks a file
        #                     missing and starts the healer.
        #
        # So the 39,244 stats become 39,244 set lookups plus a stat for the
        # handful that miss - on the last measured scan, 193 of them.
        stats_done = 0
        for a in arr_files:
            if not a.path:
                continue
            # ASK ABOUT THE PATH WE CAN REACH. a.path is the arr's spelling,
            # and on a machine that reaches the library by another road it
            # names nothing: the walk set is keyed on what nuarr walked, and
            # os.path.exists() on P:\... is False when the drive letter only
            # exists where Sonarr runs. Both tests then said "missing" about
            # 243 files that were plainly there, the healer found them on the
            # row's own path and healed them, and the pair flip-flopped every
            # couple of minutes. Translate once, ask both questions properly.
            probe_path = pathmap.to_local(a.path)
            if on_disk is not None and pathmap._norm(probe_path) in on_disk:
                continue
            # Either there is no walk set, or the set says this path is absent.
            # Both cases need the filesystem's answer before anything is marked
            # missing, because that is what starts the healer.
            stats_done += 1
            if os.path.exists(probe_path):
                continue
            row = by_key.get((a.arr_name, a.file_id))
            if row:
                truly.append(row["id"])

        if dropped:
            qs = ",".join("?" * len(dropped))
            cur.execute(f"DELETE FROM files WHERE id IN ({qs})", dropped)
        if restored:
            qs = ",".join("?" * len(restored))
            cur.execute(f"UPDATE files SET state='new', state_reason="
                        f"'reappeared on disk', updated_at=? WHERE id IN ({qs})",
                        [now] + restored)
        if truly:
            qs = ",".join("?" * len(truly))
            # Reset the heal counters only for rows that were NOT already
            # missing. Without the state guard, every scan would rewind the
            # attempt count of a file already under investigation and it could
            # never reach the third strike - it would be re-checked forever and
            # never confirmed.
            cur.execute(f"UPDATE files SET state='missing', state_reason="
                        f"'the arr tracks this file but it is not on disk — "
                        f"verifying', updated_at=?, heal_attempts=0, "
                        f"heal_last_at=NULL, heal_state='checking' "
                        f"WHERE id IN ({qs}) AND state<>'missing'", [now] + truly)
            cur.execute(f"UPDATE files SET state='missing', updated_at=? "
                        f"WHERE id IN ({qs}) AND state='missing'", [now] + truly)

    return {"arr_files": len(arr_files), "stale_rows_removed": len(dropped),
            "reappeared": len(restored), "really_missing": len(truly),
            # Observable rather than inferred: this is the number that used to
            # be len(arr_files) and is what made the phase's cost swing between
            # 26 s and 772 s depending on how busy the spindles were.
            "stats_done": stats_done,
            "used_walk_set": on_disk is not None,
            "status": status}


async def verify_unmanaged(arr_files=None, status=None) -> dict:
    r"""Re-check 'unmanaged' against the arrs, and explain what is left.

    Same discipline as verify_missing(): our own row is not evidence. A file
    reads as unmanaged whenever arr_file_id is NULL, which is also what a
    FAILED LINK looks like - the arr imported it after our last scan, or the
    path differs by case or a trailing space. Those should be adopted, not
    reported as orphans.

    Whatever is genuinely untracked gets split by whether the arr knows the
    FOLDER, because the two cases need opposite actions:

      arr knows the show, not the file -> the arr never imported it. A rescan
      of that series usually adopts it. This is the common case for an episode
      that failed import or arrived outside the arr.

      arr does not know the folder at all -> the show/movie is not in the arr.
      Nothing will ever manage it until you add it.
    """
    if arr_files is None:
        arr_files, status = await gather_arr_files(SETTINGS.arrs)
    status = status or {}
    if not any((s or {}).get("ok") for s in status.values()):
        return {"skipped": "no arr reachable", "status": status}

    return await asyncio.to_thread(_verify_unmanaged_sync, arr_files, status)


def _verify_unmanaged_sync(arr_files, status) -> dict:
    """Blocking half of verify_unmanaged - same reason as _verify_missing_sync.

    Lighter than the missing pass (no per-file stat), but it still builds three
    dictionaries over 39,000 arr records and then walks the files table, and it
    runs immediately after verify_missing in the same scan. Anything measured
    in hundreds of milliseconds does not belong on the loop when the cost of
    moving it off is one to_thread.
    """
    # LEARN THE MAPPING HERE, because this is the one place that holds both
    # the arr's paths and nuarr's in the same breath. Anywhere else would have
    # to re-fetch the arr's file list purely to work out a prefix.
    try:
        pathmap.learn([a.path for a in arr_files if a.path])
    except Exception:                                        # noqa: BLE001
        pass

    # Everything below compares in the LOCAL spelling: the arr's paths are
    # translated once, here, so the lookups stay dictionary hits rather than
    # a translate-and-compare for every row against every arr file.
    def _loc(p):
        return pathmap._norm(pathmap.to_local(p or ""))

    by_path = {_loc(a.path): a for a in arr_files if a.path}
    known_dirs = {os.path.dirname(_loc(a.path))
                  for a in arr_files if a.path}
    # a series folder is the PARENT of a season folder - match either level
    known_parents = {os.path.dirname(d) for d in known_dirs}

    cut = SETTINGS.min_orphan_size_mb * 1024 * 1024
    linked, orphan_in_known, orphan_unknown = 0, [], []
    merged = 0          # duplicate rows for a file the arr already owns
    now = time.time()

    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path, size FROM files WHERE arr_file_id IS NULL "
            "AND state NOT IN ('duplicate','deleted') AND COALESCE(size,0)>=?",
            (cut,))]
        for r in rows:
            p = pathmap._norm(r["path"] or "")
            hit = by_path.get(p)
            if hit:
                # ADOPT it - the arr does track this file, we simply had not
                # linked the row. Leaving it as an orphan would invite deleting
                # a managed file.
                #
                # But an identity can only belong to ONE row, and this UPDATE
                # was blind to that. It is how the pass killed itself:
                #
                #   scan()            inserts a row for arr file X at path P
                #   verify_unmanaged  finds a leftover orphan row ALSO at P,
                #                     matches it to X, and assigns X to it
                #   sqlite            UNIQUE constraint failed:
                #                     files.arr_name, files.arr_file_id
                #
                # Both rows describe the same file on disk - one keyed by
                # content signature from before the arr knew about it, one
                # keyed by arr identity from this pass. The arr-keyed row is
                # authoritative, so the orphan row is redundant bookkeeping and
                # is dropped rather than adopted. Checking first also means the
                # scan no longer dies 97 seconds in over a duplicate row.
                held = cur.execute(
                    "SELECT id FROM files WHERE arr_name=? AND arr_file_id=?",
                    (hit.arr_name, hit.file_id)).fetchone()
                if held and held["id"] != r["id"]:
                    cur.execute("DELETE FROM files WHERE id=?", (r["id"],))
                    merged += 1
                    continue
                # CLEAR THE ADOPTION VERDICT, which has just been disproved.
                # adopt_state='no_folder' means "no arr manages this folder",
                # and the row now carries the id of the arr file that does. It
                # was left standing, so a linked file went on displaying "it
                # cannot be adopted automatically" underneath "linked by
                # unmanaged verify" - the record of a failure sitting next to
                # the success that ended it.
                # TAKE THE TITLE TOO. Linking copied the ids and nothing else,
                # so 243 freshly-linked files stayed "(untitled)" - in the
                # activity list, in the queue, and in every log line about
                # them, which made a working pipeline unreadable. The arr knows
                # what they are called; this is the moment it is being asked.
                cur.execute(
                    "UPDATE files SET arr_name=?, arr_file_id=?, arr_parent_id=?,"
                    " title=COALESCE(NULLIF(?,''), title),"
                    " season=COALESCE(?, season), episode=COALESCE(?, episode),"
                    " adopt_state=NULL, adopt_attempts=0, adopt_last_at=NULL,"
                    " state_reason='linked by unmanaged verify', updated_at=? "
                    "WHERE id=?",
                    (hit.arr_name, hit.file_id, hit.parent_id,
                     hit.title or "", hit.season, hit.episode, now, r["id"]))
                linked += 1
                continue
            d = os.path.dirname(pathmap._norm(r["path"] or ""))
            if d in known_dirs or d in known_parents or os.path.dirname(d) in known_parents:
                orphan_in_known.append(r["id"])
            else:
                orphan_unknown.append(r["id"])

        if orphan_in_known:
            qs = ",".join("?" * len(orphan_in_known))
            cur.execute(f"UPDATE files SET state_reason='the arr manages this "
                        f"folder but never imported this file - try a rescan' "
                        f"WHERE id IN ({qs})", orphan_in_known)
        if orphan_unknown:
            qs = ",".join("?" * len(orphan_unknown))
            cur.execute(f"UPDATE files SET state_reason='not in any arr - the "
                        f"show/movie is not added' WHERE id IN ({qs})",
                        orphan_unknown)

        # A LINKED FILE CANNOT BE UN-ADOPTABLE. 'no_folder' means no arr
        # manages this folder; a row carrying an arr file id is proof that one
        # does. Rows linked before this rule existed kept the old verdict and
        # went on saying "it cannot be adopted automatically" underneath the
        # line announcing that it had been - so the contradiction is swept up
        # here rather than left waiting for each file to be touched again.
        cur.execute("UPDATE files SET adopt_state=NULL, adopt_attempts=0 "
                    " WHERE arr_file_id IS NOT NULL "
                    "   AND COALESCE(adopt_state,'')='no_folder'")
        stale_cleared = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        # A LINKED FILE SHOULD KNOW ITS NAME. Rows linked before the title was
        # carried across stayed "(untitled)" everywhere they appeared - the
        # queue, the activity list, every log line - and would have stayed that
        # way until each file happened to be re-linked, which for an already
        # linked file is never. The arr's answer is in hand right now, so the
        # blanks are filled from it. Only blanks: a title we already have is
        # not the arr's to overwrite.
        named = 0
        for a in arr_files:
            if not a.title:
                continue
            cur.execute(
                "UPDATE files SET title=?, season=COALESCE(season,?), "
                "  episode=COALESCE(episode,?) "
                " WHERE arr_name=? AND arr_file_id=? "
                "   AND COALESCE(title,'')=''",
                (a.title, a.season, a.episode, a.arr_name, a.file_id))
            named += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    if stale_cleared:
        joblog.log(f"cleared a stale 'cannot be adopted' note from "
                   f"{stale_cleared} file(s) the arr does in fact manage",
                   "info")
    if named:
        joblog.log(f"took the title from the arr for {named} file(s) that had "
                   f"none", "info")
    return {"checked": len(rows), "linked_to_arr": linked,
            "stale_adopt_cleared": stale_cleared, "titles_filled": named,
            "merged_duplicate_rows": merged,
            "arr_knows_folder_not_file": len(orphan_in_known),
            "not_in_any_arr": len(orphan_unknown), "status": status}


def clear_resolved_failures() -> dict:
    r"""Drop 'failed' job rows that have since been proven resolved.

    A failure record is worth keeping only while it still describes something
    wrong. Once the same file has been processed successfully, or its file
    record has gone entirely, the row is just a red mark that never clears -
    and a panel showing failures that cannot be acted on is one you stop
    reading.

    Deliberately conservative. A row is removed ONLY with positive evidence:

      * a LATER job for the same target finished done/skipped, or
      * the file record it points at no longer exists (superseded or removed)

    Nothing is dropped for being merely old, and anything without evidence is
    left exactly where it is. All seven failures on this box at the time of
    writing qualified under the first two rules; none needed a time-based one.
    """
    sql = """
    SELECT f.id, f.kind, f.title,
      CASE
        WHEN f.file_id IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM files x WHERE x.id = f.file_id)
          THEN 'file record gone - superseded or removed'
        WHEN EXISTS (SELECT 1 FROM jobs l
                     WHERE l.state IN ('done','skipped')
                       AND COALESCE(l.created_at,0) > COALESCE(f.created_at,0)
                       AND ((f.file_id IS NOT NULL AND l.file_id = f.file_id)
                         OR (f.file_id IS NULL AND l.file_id IS NULL
                             AND l.kind = f.kind)))
          THEN 'a later job for the same target succeeded'
        ELSE NULL
      END AS why
    FROM jobs f WHERE f.state = 'failed'
    """
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(sql)]
        gone = [r for r in rows if r["why"]]
        if gone:
            cur.executemany("DELETE FROM jobs WHERE id=?",
                            [(r["id"],) for r in gone])
    kept = len(rows) - len(gone)
    reasons: dict[str, int] = {}
    for r in gone:
        reasons[r["why"]] = reasons.get(r["why"], 0) + 1
    return {"checked": len(rows), "cleared": len(gone), "kept": kept,
            "reasons": reasons,
            "titles": [r["title"] or r["kind"] for r in gone][:10]}


def sweep_missing() -> dict:
    r"""Offline pass: drop 'missing' rows superseded by an upgrade.

    Cheap and arr-free, so it can run at the end of every scan. It handles the
    bulk case - an upgrade replaced the file and the old row lingered - while
    verify_missing() is the authoritative check against the arrs.
    """
    with cursor() as cur:
        miss = [dict(r) for r in cur.execute(
            "SELECT id, path FROM files WHERE state='missing'")]
        live_keys = set()
        for r in cur.execute("SELECT path FROM files "
                             "WHERE state NOT IN ('missing','deleted')"):
            k = _ep_key(r["path"])
            if k:
                live_keys.add(k)

        superseded, back = [], []
        for r in miss:
            if r["path"] and os.path.exists(r["path"]):
                back.append(r["id"])
            elif _ep_key(r["path"]) in live_keys:
                superseded.append(r["id"])

        if superseded:
            qs = ",".join("?" * len(superseded))
            cur.execute(f"DELETE FROM files WHERE id IN ({qs})", superseded)
        if back:
            qs = ",".join("?" * len(back))
            cur.execute(f"UPDATE files SET state='new', state_reason="
                        f"'reappeared on disk', updated_at=? WHERE id IN ({qs})",
                        [time.time()] + back)
        real = len(miss) - len(superseded) - len(back)
    return {"checked": len(miss), "superseded_removed": len(superseded),
            "reappeared": len(back), "still_missing": real}


# ------------------------------------------------------- lock settling ----
# WHY A TIMESTAMP IS NOT ENOUGH.
#
# hold_minutes asks "has nothing written to this file for N minutes", which is
# a proxy for "is anybody using it". The proxy fails in both directions: an
# import that stalls mid-copy stops updating mtime and looks settled, while a
# file Plex is streaming or DrivePool is duplicating has an old mtime and is
# very much in use - and a transcode that starts there fights the reader for
# the spindle, or hits the sharing violation the commit was built to avoid.
#
# So the hold now ALSO requires the file to be openable exclusively - the same
# CreateFileW(dwShareMode=0) test fileops uses before a replace, which is
# exactly the access the job will need - and to have STAYED that way for
# LOCK_QUIET_S. One clean probe is not evidence: Plex opens and closes a file
# repeatedly while scanning, so a single lucky moment between reads would
# promote a file that is about to be locked again. Two clean probes 30 s apart
# is a much stronger claim, and costs one file handle each.
LOCK_QUIET_S = 30.0

# file id -> {"since": when it first probed clean, "holders": [...], "at": last probe}
_LOCK_WATCH: dict[int, dict] = {}
# How many held files to probe per pass. Each probe is a CreateFileW and,
# only when locked, a Restart Manager query; bounded so a 2,000-file backlog
# cannot turn a 60 s scan into a lock-probing marathon.
LOCK_PROBE_MAX = 120


def _lock_state(file_id: int, path: str) -> tuple[bool, list[str]]:
    """(quiet_long_enough, who_holds_it). Never raises."""
    from . import fileops
    now = time.time()
    st = _LOCK_WATCH.get(file_id)
    try:
        locked = fileops.is_locked(path)
    except Exception:
        return True, []                  # cannot tell - do not invent a hold
    if locked:
        holders: list[str] = []
        try:
            holders = fileops.who_locks(path)
        except Exception:
            pass
        _LOCK_WATCH[file_id] = {"since": 0.0, "holders": holders, "at": now}
        return False, holders
    since = (st or {}).get("since") or 0.0
    if not since:
        since = now                      # first clean probe starts the clock
    _LOCK_WATCH[file_id] = {"since": since, "holders": [], "at": now}
    return (now - since) >= LOCK_QUIET_S, []


def held_count() -> int:
    """How many files are waiting on the lock check right now."""
    try:
        with cursor() as cur:
            return int(cur.execute(
                "SELECT COUNT(*) n FROM files WHERE state='new'").fetchone()["n"])
    except Exception:
        return 0


def lock_note(file_id: int) -> dict | None:
    """What the last probe saw, for the Held tile's drill-down."""
    st = _LOCK_WATCH.get(file_id)
    if not st:
        return None
    if st.get("holders"):
        return {"locked": True, "holders": st["holders"], "at": st["at"]}
    since = st.get("since") or 0.0
    return {"locked": False, "holders": [],
            "quiet_for_s": round(time.time() - since, 1) if since else 0.0,
            "needs_s": LOCK_QUIET_S, "at": st["at"]}


def mark_eligible() -> int:
    """Promote quiet files to 'eligible'.

    A file is held until it has been untouched for hold_minutes AND has been
    openable-exclusively for LOCK_QUIET_S, so we never spend GPU time on
    something Sonarr is still importing, Plex is streaming, or DrivePool is
    moving between spindles.
    """
    with cursor() as cur:
        # Do NOT blanket-null state_reason here. It is the only place a
        # diagnosis lives, and promotion runs every 60 s - it was erasing the
        # unmanaged classification ("the arr manages this folder but never
        # imported this file") within a minute of it being written, so nothing
        # downstream could ever act on it. Only clear text this function's own
        # hold placed there.
        # ASK THE FILE, NOT THE CLOCK.
        #
        # This used to require mtime to be hold_minutes old before it would
        # even look, so a finished import sat in Held for five minutes doing
        # nothing while nobody had it open. The timestamp was only ever a proxy
        # for "is anybody using this", and the lock probe answers that question
        # directly - a file still being written by an arr cannot be opened
        # exclusively, so it holds itself without needing a timer.
        #
        # Dropping the clock also disposes of a whole class of bug it created:
        # a NULL mtime, or the 2097 timestamp on Pulse, used to mean "held
        # forever" and needed a separate observation net to rescue it. A file
        # with a nonsense date is now just a file - probed like any other.
        #
        # Oldest first so a large import cannot starve anything behind it.
        due = [dict(r) for r in cur.execute(
            "SELECT id, path FROM files WHERE state='new' "
            "ORDER BY COALESCE(first_seen, 0) LIMIT ?", (LOCK_PROBE_MAX,))]
        ok_ids, blocked = [], 0
        for r in due:
            p = r["path"] or ""
            if not p or not os.path.exists(p):
                ok_ids.append(r["id"])   # gone or unreadable: not our hold
                continue
            quiet, holders = _lock_state(r["id"], p)
            if quiet:
                ok_ids.append(r["id"])
            else:
                blocked += 1
                # TWO DIFFERENT HOLDS, and calling both "in use" was wrong:
                # a file can be perfectly free and simply not have been free
                # for LOCK_QUIET_S yet, which is the normal path EVERY file
                # takes on its first probe. Saying "in use by another program"
                # there invents a reader that does not exist.
                st = _LOCK_WATCH.get(r["id"]) or {}
                if holders or not st.get("since"):
                    who = ", ".join(holders[:3]) if holders else "another program"
                    reason = f"held — in use by {who}"
                else:
                    left = max(0.0, LOCK_QUIET_S - (time.time() - st["since"]))
                    reason = (f"settling — free, needs {left:.0f}s more "
                              f"without a reader")
                cur.execute("UPDATE files SET state_reason=? WHERE id=?",
                            (reason, r["id"]))
        promoted = 0
        if ok_ids:
            qs = ",".join("?" * len(ok_ids))
            cur.execute(
                "UPDATE files SET state='eligible', updated_at=?, "
                "state_reason=CASE WHEN state_reason LIKE '%settl%' "
                "                   OR state_reason LIKE '%hold%' "
                "                   OR state_reason LIKE '%in use by%' "
                "                  THEN NULL ELSE state_reason END "
                f"WHERE state='new' AND id IN ({qs})",
                (time.time(), *ok_ids))
            promoted = cur.rowcount
            for i in ok_ids:
                _LOCK_WATCH.pop(i, None)
        if blocked:
            joblog.log(f"{blocked} settled file(s) still open in another "
                       f"program — held until they are released", "debug")

        # The "nothing may be held forever" net used to live here: a file whose
        # mtime was NULL or in the future could never satisfy the timestamp
        # test, so it needed rescuing after a day. Nothing gates on mtime any
        # more, so a nonsense date holds nothing and the net has no work left
        # to do - it was a patch on a rule that has since been removed.
        return promoted


if __name__ == "__main__":
    import asyncio
    from .db import init_db

    init_db()
    r = asyncio.run(scan())
    print(r)
    print("arr status:", r.arr_status)
    print("promoted to eligible:", mark_eligible())
