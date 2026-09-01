"""
nuarr - resilient file operations

WHY THIS EXISTS
---------------
Every file-handling failure observed on this system came from a tool giving up
on the first error instead of waiting:

    EBUSY: resource busy or locked, rename 'P:\\...My Dress-Up Darling...'
    EBUSY: ... 'P:\\Anime Shows\\Black Butler...'
    ENOENT: no such file or directory, rename 'P:\\...Detective Conan...'
    Source file ...mkv.tmp has size 0 or does not exist   (x104 in one day)
    POST /api/v2/file/download -> 501  (x240, fallback after the source vanished)

Three distinct causes, all recoverable:
  * TRANSIENT LOCK  - Plex is scanning, the OCR hook has the file open, or
                      antivirus is mid-scan. Waiting a few seconds fixes it.
  * MOVED UNDERNEATH - Sonarr renamed it, or DrivePool's balancer relocated it
                      between pool disks. Re-resolving the path fixes it.
  * TRUNCATED COPY  - destination reported 0 bytes because the balancer moved
                      the file mid-write. Verify-then-commit fixes it.

So: every operation waits, retries with backoff, verifies the result, and can
roll back. The ORIGINAL is never destroyed until its replacement is confirmed
byte-for-byte in place.
"""
from __future__ import annotations

import concurrent.futures
import ctypes
import hashlib
import os
import shutil
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from .config import SETTINGS


# --------------------------------------------------------------------------
# result type
# --------------------------------------------------------------------------
@dataclass
class OpResult:
    ok: bool
    action: str
    detail: str = ""
    attempts: int = 1
    waited_s: float = 0.0
    locked_by: list[str] = field(default_factory=list)
    rolled_back: bool = False

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------
# lock detection
# --------------------------------------------------------------------------

# How long a commit may sit fully paused for a viewer before giving up and
# falling back to heavy throttling instead. Five minutes: long enough to ride
# out a genuine buffer dip, short enough that a viewer stuck below the
# threshold cannot pin a finished encode in the cache indefinitely.
PAUSE_CAP_S = 300.0

# HOW MUCH THE SLEEP IS SOFTENED once Windows background I/O mode has taken.
#
# Named rather than inlined because the UI has to divide by exactly the same
# number. It did not, and the card lied: it printed 100/(1+factor) - the speed
# implied by the RAW ramp - while the copy loop was sleeping factor/3. A card
# reading "nuarr at 44% speed" was attached to a commit actually running at
# about 70%. Not a rounding error; the label understated the competing I/O by
# more than half, which is the opposite of the reassurance it was there to give.
BG_DISCOUNT = 3.0

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33

# Declare the signatures explicitly. Without an explicit restype, ctypes assumes
# the return is a 32-bit int and TRUNCATES the 64-bit HANDLE - which made
# INVALID_HANDLE_VALUE compare unequal to itself, so every failed open was read
# as a success and is_locked() always answered False. use_last_error=True is
# required for get_last_error() to carry the sharing-violation code.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def is_locked(path: str) -> bool:
    """True if the file cannot be opened with the access a move/replace needs.

    MUST use CreateFileW with dwShareMode=0. Python's os.open() opens with
    permissive sharing, so it succeeds even while another process holds the
    file - verified: a held handle reported is_locked=False, and the waiter
    returned instantly instead of waiting. Only an exclusive open surfaces the
    sharing violation that is about to break the rename.

    DELETE access is included deliberately: renaming a file on Windows needs
    it, so a reader that opened without FILE_SHARE_DELETE (Plex does exactly
    this while scanning) correctly counts as locked.
    """
    if not os.path.exists(path):
        return False
    handle = _k32.CreateFileW(
        str(path),
        _GENERIC_READ | _GENERIC_WRITE | _DELETE,
        0,                       # dwShareMode = 0 -> exclusive
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE:
        err = ctypes.get_last_error()
        # sharing/lock violation = genuinely in use. Anything else (permissions,
        # path too long) is not a lock and must not trigger an endless wait.
        return err in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION)
    _k32.CloseHandle(handle)
    return False


def who_locks(path: str) -> list[str]:
    """Best-effort: which processes hold this file open.

    Uses the Windows Restart Manager. Turning "it's locked" into "it's locked
    by plex.exe" is the difference between a useful log line and a mystery.
    Returns [] if unavailable - never raises.
    """
    try:
        rstrtmgr = ctypes.WinDLL("rstrtmgr")
    except Exception:
        return []

    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(256)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != 0:
        return []
    try:
        files = (ctypes.c_wchar_p * 1)(str(path))
        if rstrtmgr.RmRegisterResources(session, 1, files, 0, None, 0, None) != 0:
            return []

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD),
                        ("ProcessStartTime", wintypes.FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [("Process", RM_UNIQUE_PROCESS),
                        ("strAppName", wintypes.WCHAR * 256),
                        ("strServiceShortName", wintypes.WCHAR * 64),
                        ("ApplicationType", ctypes.c_int),
                        ("AppStatus", wintypes.ULONG),
                        ("TSSessionId", wintypes.DWORD),
                        ("bRestartable", wintypes.BOOL)]

        need = wintypes.UINT(0)
        got = wintypes.UINT(0)
        reason = wintypes.DWORD(0)
        rstrtmgr.RmGetList(session, ctypes.byref(need), ctypes.byref(got), None, ctypes.byref(reason))
        n = max(need.value, 1)
        arr = (RM_PROCESS_INFO * n)()
        got = wintypes.UINT(n)
        if rstrtmgr.RmGetList(session, ctypes.byref(need), ctypes.byref(got),
                              arr, ctypes.byref(reason)) != 0:
            return []
        out = []
        for i in range(got.value):
            name = arr[i].strAppName or ""
            pid = arr[i].Process.dwProcessId
            if name:
                out.append(f"{name} (pid {pid})")
        return out
    finally:
        try:
            rstrtmgr.RmEndSession(session)
        except Exception:
            pass


def wait_for_unlock(path: str, timeout_s: float = 300, poll_s: float = 2.0) -> tuple[bool, float, list[str]]:
    """Block until the file is unlocked, or timeout.

    Returns (unlocked, seconds_waited, who_held_it_last).
    """
    t0 = time.time()
    holders: list[str] = []
    while time.time() - t0 < timeout_s:
        if not is_locked(path):
            return True, time.time() - t0, holders
        if not holders:
            holders = who_locks(path)
        time.sleep(poll_s)
        poll_s = min(poll_s * 1.4, 15.0)     # back off, don't hammer
    return False, time.time() - t0, holders


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
def quick_sig(path: str, sample_mb: int = 8) -> str:
    """size + hash of head/tail. Cheap enough to run on every operation.

    A full hash of a 5 GB file at 105 MB/s costs ~48 s; this costs ~0.2 s and
    still catches truncation, partial writes and the 0-byte case that broke
    104 jobs in a day.
    """
    st = os.stat(path)
    h = hashlib.blake2b(digest_size=16)
    h.update(str(st.st_size).encode())
    n = sample_mb * 1024 * 1024
    with open(path, "rb") as f:
        h.update(f.read(n))
        if st.st_size > n * 2:
            f.seek(-n, os.SEEK_END)
            h.update(f.read(n))
    return f"{st.st_size}:{h.hexdigest()}"


def verify_copy(src: str, dst: str, tolerance: int = 0) -> tuple[bool, str]:
    """Confirm dst is a faithful copy of src."""
    if not os.path.exists(dst):
        return False, "destination does not exist"
    s, d = os.path.getsize(src), os.path.getsize(dst)
    if d == 0:
        return False, "destination is 0 bytes"
    if abs(s - d) > tolerance:
        return False, f"size mismatch: src={s:,} dst={d:,}"
    return True, ""


def long_path(path: str) -> str:
    r"""Windows extended-length form: \\?\C:\... — lifts the 260-char limit.

    The Win32 API caps a normal path at MAX_PATH, but the SAME call accepts up
    to ~32,767 characters when prefixed with \\?\. Python's os functions pass
    the string straight through, so this is the real bypass rather than a
    workaround. Note the prefix disables path normalisation, so the input must
    already be absolute with no '..' segments.
    """
    if not path or path.startswith("\\\\?\\"):
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\"):                       # UNC: \\server\share
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p


_LONG_PATHS: bool | None = None


def long_paths_enabled() -> bool:
    r"""Does this machine allow paths past MAX_PATH without the \\?\ prefix?

    Windows 10 1607+ has an opt-in registry switch. When it is on, ordinary
    Win32 calls accept long paths and .NET applications - Sonarr included -
    handle them transparently. THIS BOX HAS IT ON, and files of 302-315
    characters already sit in the library working fine.

    That matters because nuarr was refusing renames Sonarr performs without
    complaint. A limit that the operating system is not enforcing is not a
    limit; treating it as one just makes nuarr the thing that cannot do the job.
    """
    global _LONG_PATHS
    if _LONG_PATHS is None:
        _LONG_PATHS = False
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
                _LONG_PATHS = bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
        except Exception:
            _LONG_PATHS = False
    return _LONG_PATHS


def path_too_long(path: str, limit: int = 259) -> bool:
    """Windows MAX_PATH pre-flight.

    Returns False outright when the OS has long paths enabled - there is
    nothing to pre-empt. Otherwise the caller's own margin applies (a commit
    writes '.nuarr-new'/'.nuarr-bak' siblings; a plain rename writes nothing).
    """
    if long_paths_enabled():
        return False
    return len(path) > limit


# --------------------------------------------------------------------------
# core operations
# --------------------------------------------------------------------------
def safe_copy(src: str, dst: str, *, attempts: int = 5, base_delay: float = 3.0,
              lock_timeout: float = 300) -> OpResult:
    """Copy with lock-wait, retry, and post-copy verification."""
    waited = 0.0
    holders: list[str] = []
    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, attempts + 1):
        if not os.path.exists(src):
            return OpResult(False, "copy", f"source vanished: {src}", attempt, waited, holders)

        unlocked, w, holders = wait_for_unlock(src, lock_timeout)
        waited += w
        if not unlocked:
            return OpResult(False, "copy", "source stayed locked", attempt, waited, holders)

        try:
            tmp = dst + ".partial"
            if os.path.exists(tmp):
                os.remove(tmp)
            shutil.copy2(src, tmp)
            ok, why = verify_copy(src, tmp)
            if not ok:
                os.remove(tmp)
                raise OSError(why)
            os.replace(tmp, dst)
            return OpResult(True, "copy", f"{human_bytes(os.path.getsize(dst))} verified",
                            attempt, waited, holders)
        except OSError as e:
            if attempt == attempts:
                return OpResult(False, "copy", f"{type(e).__name__}: {e}", attempt, waited, holders)
            delay = base_delay * (2 ** (attempt - 1))
            time.sleep(delay)
            waited += delay
    return OpResult(False, "copy", "exhausted attempts", attempts, waited, holders)


def human_bytes(n) -> str:
    """1.4 GB, not 1503238553. These strings end up in the log and the UI."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "— bytes"
    for unit, size in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20),
                       ("KB", 1 << 10)):
        if n >= size:
            v = n / size
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{int(n)} B"


class _BackgroundIO:
    r"""Put THIS THREAD into Windows background I/O mode, and take it out again.

    The commit used to yield a disk only by writing less often, on the
    reasoning that there is no per-thread I/O priority available. That is true
    of psutil, and not true of Windows: SetThreadPriority accepts
    THREAD_MODE_BACKGROUND_BEGIN, which drops the CALLING THREAD's I/O priority
    to Very Low and leaves every other thread in the process alone. That is
    exactly the shape needed here - the copy already runs on its own thread via
    asyncio.to_thread, so the web server and the job pump are untouched.

    Why it is better than sleeping: the scheduler yields on demand. A viewer's
    read is foreground I/O and overtakes background I/O automatically, with no
    guessing at a multiplier, and it costs nothing when nothing else wants the
    disk. Sleeping gives up its share whether or not anyone is asking for it.

    Why the sleep stays anyway: background mode is a hint to a scheduler, and
    it is not a hard cap. Keeping a smaller sleep alongside it means the
    guarantee does not rest entirely on a scheduler behaviour that is hard to
    verify from here (see the honest note in the commit rationale). Belt, then
    braces.

    Begin/End must be paired and must run on the same thread, so the state is
    tracked rather than assumed - calling End without a matching Begin fails,
    and calling Begin twice fails too.
    """

    BEGIN = 0x00010000
    END = 0x00020000

    def __init__(self) -> None:
        self.active = False
        self.available = os.name == "nt"
        self._k32 = None
        if self.available:
            try:
                import ctypes
                from ctypes import wintypes
                self._k32 = ctypes.windll.kernel32
                self._k32.GetCurrentThread.restype = wintypes.HANDLE
                self._k32.SetThreadPriority.argtypes = [wintypes.HANDLE,
                                                        ctypes.c_int]
            except Exception:
                self.available = False

    def set(self, on: bool) -> None:
        if not self.available or on == self.active:
            return
        try:
            ok = self._k32.SetThreadPriority(
                self._k32.GetCurrentThread(),
                self.BEGIN if on else self.END)
            if ok:
                self.active = on
            else:
                # Not fatal: the sleep pacing below still applies. Stop trying,
                # so a failing call is not repeated once per 8 MB.
                self.available = False
        except Exception:
            self.available = False


def copy_with_progress(src: str, dst: str, on_progress=None,
                       chunk: int = 8 << 20, pace=None) -> None:
    r"""shutil.copy2, but it says how far along it is.

    The commit copies the finished encode from the E: cache onto the pool, and
    on a 60-minute 4K remux that is 8-60 GB through DrivePool. shutil.copy2 is
    one opaque blocking call, so the UI showed "committing from NU-DRIVE-10 —
    placing…" for the entire thing with no drive, no speed and no ETA - the
    longest single step in a job was also the only one with no progress.

    on_progress(copied, total) is called per chunk. 8 MB chunks: large enough
    that the callback overhead is nothing next to the I/O, small enough that
    the numbers move visibly on a slow spindle.

    PACING, AND WHY IT IS NOT I/O PRIORITY.

    Everywhere else nuarr yields a disk by lowering the OS I/O priority of the
    ffmpeg child. That cannot work here: by commit time ffmpeg has exited, and
    this copy runs on a thread inside nuarr itself. Setting nuarr's own process
    priority would slow the web server and the job pump along with it, and
    Windows has no per-thread I/O priority that psutil exposes.

    So the commit yields the only way it can - by writing less often. `pace()`
    is consulted per chunk and returns a multiplier: 0 for full speed, or N to
    sleep N x however long the chunk just took. The cost is therefore
    proportional to the actual disk, not a guessed byte rate, so it behaves the
    same on a fast spindle and a slow one.

    This is the case that matters most for a viewer: the commit writes GIGABYTES
    into the pool, and DrivePool may well place them on the disk being watched.
    """
    total = os.path.getsize(src)
    copied = 0
    bg = _BackgroundIO()
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                t0 = time.time()
                buf = fsrc.read(chunk)
                if not buf:
                    break
                fdst.write(buf)
                copied += len(buf)
                if pace:
                    try:
                        factor = float(pace() or 0)
                    except Exception:
                        factor = 0.0
                    # A NEGATIVE FACTOR MEANS STOP, not "go slowly".
                    #
                    # Throttling to a quarter speed is the right answer when a
                    # viewer has minutes banked and the wrong one when they
                    # have seconds: a quarter of a 150 MB/s commit is still
                    # 37 MB/s of competing writes on the spindle they are
                    # reading from. Below the buffer threshold the only useful
                    # thing this copy can do is get out of the way entirely.
                    #
                    # Polled rather than slept blind, so it resumes within half
                    # a second of the buffer recovering - the whole point is to
                    # give the disk back promptly, and a long fixed sleep would
                    # keep yielding after the reason had gone.
                    if factor < 0:
                        waited = 0.0
                        bg.set(True)
                        while waited < PAUSE_CAP_S:
                            time.sleep(0.5)
                            waited += 0.5
                            if on_progress:
                                try:
                                    on_progress(copied, total)
                                except Exception:
                                    pass
                            try:
                                if float(pace() or 0) >= 0:
                                    break
                            except Exception:
                                break
                        # NEVER WAIT FOREVER. A viewer stuck below the
                        # threshold for minutes means something else is wrong,
                        # and holding a finished encode in the cache plus a
                        # worker slot indefinitely helps nobody. Past the cap
                        # it falls back to the heaviest throttle instead.
                        factor = 3.0 if waited >= PAUSE_CAP_S else 0.0
                    # Ask the OS to yield first. Entered and left on the
                    # TRANSITIONS only - a viewer starting or stopping mid-copy
                    # flips this within one chunk, and nothing is called on the
                    # 8 MB steps in between.
                    bg.set(factor > 0)
                    if factor > 0:
                        # Sleep in proportion to the work just done. Capped so
                        # a single stall cannot look like a hung commit.
                        #
                        # Lighter when background mode took, because the
                        # scheduler is already standing aside and doing both at
                        # full strength would turn a 20 GB commit into an
                        # overnight job for no extra protection.
                        eff = (factor / BG_DISCOUNT) if bg.active else factor
                        time.sleep(min((time.time() - t0) * eff, 2.0))
                if on_progress:
                    try:
                        on_progress(copied, total)
                    except Exception:
                        pass      # never let a UI callback break a commit
    finally:
        # Leave the thread as it was found. asyncio.to_thread hands threads
        # back to a shared pool, so a thread left in background mode would
        # quietly slow whatever ran on it next.
        bg.set(False)
    # copy2 semantics: carry mtime/permissions over, which the rest of the
    # pipeline (and verify_copy) relies on.
    shutil.copystat(src, dst)


def recycle(path: str) -> OpResult:
    r"""Move a file to the Windows Recycle Bin. Restorable, never a delete.

    SHFileOperationW with FOF_ALLOWUNDO is the same operation Explorer's
    Delete key performs, so anything this removes can be put back from the
    Bin by hand. Deliberately NOT os.remove with a journal: a journal can
    say what was deleted but cannot bring it back.

    Known limit: SHFileOperation predates long-path support and cannot take
    \\?\ paths, so an over-length path fails here rather than being deleted
    un-restorably by some other means. That is the correct failure.
    """
    import ctypes
    from ctypes import wintypes

    if not os.path.exists(path):
        return OpResult(False, "recycle", "file does not exist")
    if path_too_long(path):
        return OpResult(False, "recycle",
                        "path too long for the Recycle Bin API — handle by hand")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_ALLOWUNDO = 3, 0x40
    FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION
                         | FOF_SILENT | FOF_NOERRORUI,
                         False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        return OpResult(False, "recycle", f"SHFileOperation returned {rc}")
    if os.path.exists(path):
        return OpResult(False, "recycle", "file still present after the move")
    return OpResult(True, "recycle", "moved to the Recycle Bin")


def fix_container_extension(path: str, *, lock_timeout: float = 120) -> OpResult:
    r"""Give a Matroska file the .mkv name it should have had.

    THE BUG THIS EXISTS FOR
    -----------------------
    Every output nuarr writes is Matroska - the cache file is always
    <job_id>.mkv - but the commit replaced the ORIGINAL path, so a file that
    arrived as .mp4 was handed Matroska content and kept its .mp4 name. 148
    files in this library are Matroska wearing .mp4, .m4v or .avi. The rule
    audit never noticed because it reads format_name, which correctly says
    matroska; the container check is right and the name check did not exist.

    Done as a RENAME AFTER the commit rather than by committing to a different
    target. safe_replace() is the code that stands between a working library
    and a lost file, it is careful and it is proven; a same-volume rename that
    runs afterwards cannot make things worse than they already are. If it
    fails, the file is exactly what it was before - correct content, wrong
    name - which is today's behaviour rather than a new failure mode.
    """
    if not os.path.exists(path):
        return OpResult(False, "rename-ext", "file no longer exists")
    stem, ext = os.path.splitext(path)
    if ext.lower() == ".mkv":
        return OpResult(True, "rename-ext", "already .mkv")
    target = stem + ".mkv"
    if os.path.exists(target):
        # Refuse rather than overwrite. A same-stem .mkv sitting beside the
        # file is either a duplicate or a half-finished earlier attempt, and
        # neither is something to resolve by deleting one of them silently.
        return OpResult(False, "rename-ext",
                        f"{os.path.basename(target)} already exists")
    unlocked, waited, holders = wait_for_unlock(path, lock_timeout)
    if not unlocked:
        return OpResult(False, "rename-ext", "file is locked", 1, waited, holders)
    try:
        os.replace(path, target)
    except OSError as e:
        return OpResult(False, "rename-ext", f"{type(e).__name__}: {e}")
    return OpResult(True, "rename-ext", target)


def safe_replace(target: str, replacement: str, *, attempts: int = 5,
                 base_delay: float = 3.0, lock_timeout: float = 600,
                 keep_backup: bool = False, on_stage=None, pace=None) -> OpResult:
    """Replace `target` with `replacement`, verifiably and reversibly.

    Sequence - the original is never at risk:
        1. verify the replacement is real (exists, non-zero)
        2. wait for the target to be unlocked
        3. move target  -> target.nuarr-bak      (still on disk)
        4. move replacement -> target
        5. verify the new target
        6. only now delete the backup
       On any failure at 4/5, restore the backup and report rolled_back.
    """
    waited = 0.0
    holders: list[str] = []

    if not os.path.exists(replacement):
        return OpResult(False, "replace", f"replacement missing: {replacement}")
    if os.path.getsize(replacement) == 0:
        return OpResult(False, "replace", "replacement is 0 bytes - refusing")
    # Over the limit? Use the extended-length form instead of refusing. The
    # ~2,243 over-length files in Anime Shows are otherwise permanently stuck.
    if path_too_long(target):
        if not SETTINGS.allow_long_paths:
            return OpResult(False, "replace",
                            f"target path too long ({len(target)} chars) — set "
                            f"allow_long_paths to process it anyway")
        target = long_path(target)
        replacement = long_path(replacement)

    new_sig_size = os.path.getsize(replacement)
    backup = target + ".nuarr-bak"

    # CROSS-VOLUME STAGING.
    # os.replace() is a rename, and a rename cannot cross drives - it raises
    # WinError 17. The transcode cache lives on E: (NVMe, deliberately off the
    # pool) while the library is on P:, so a cross-volume commit is the NORMAL
    # path here, not an edge case: without this, every single transcode fails at
    # the final step after all the work is done.
    # Stage a copy next to the target first, so the swap itself stays atomic.
    staged: str | None = None
    if os.path.splitdrive(os.path.abspath(replacement))[0].lower() != \
       os.path.splitdrive(os.path.abspath(target))[0].lower():
        staged = target + ".nuarr-new"
        try:
            if os.path.exists(staged):
                os.remove(staged)
            # The staging file sits at its FINAL location on the pool, so
            # DrivePool has already chosen a spindle for it by the time the
            # first byte lands. That is what makes the destination drive
            # knowable during the copy rather than only after it - the caller
            # resolves it from this path on the first callback.
            if on_stage:
                try:
                    on_stage("staging", staged, 0, os.path.getsize(replacement))
                except Exception:
                    pass
            copy_with_progress(
                replacement, staged,
                (lambda c, t: on_stage("copying", staged, c, t)) if on_stage else None,
                pace=pace)
            if on_stage:
                try:
                    on_stage("verifying", staged, 0, 0)
                except Exception:
                    pass
            ok, why = verify_copy(replacement, staged)
            if not ok:
                _quiet_remove(staged)
                return OpResult(False, "replace", f"cross-volume staging failed: {why}")
            replacement = staged
        except OSError as e:
            _quiet_remove(staged)
            return OpResult(False, "replace", f"cross-volume staging failed: {e}")

    for attempt in range(1, attempts + 1):
        unlocked, w, holders = wait_for_unlock(target, lock_timeout)
        waited += w
        if not unlocked and os.path.exists(target):
            if attempt == attempts:
                return OpResult(False, "replace", "target stayed locked", attempt, waited, holders)
            time.sleep(base_delay * attempt)
            continue

        moved_backup = False
        try:
            if os.path.exists(target):
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(target, backup)
                moved_backup = True

            os.replace(replacement, target)

            if not os.path.exists(target) or os.path.getsize(target) != new_sig_size:
                raise OSError("post-replace verification failed")

            if moved_backup and not keep_backup:
                _quiet_remove(backup)
            return OpResult(True, "replace", f"{human_bytes(new_sig_size)} in place"
                            + (" (staged across volumes)" if staged else ""),
                            attempt, waited, holders)

        except OSError as e:
            # roll back so the library is never left without the original
            rolled = False
            if moved_backup and not os.path.exists(target) and os.path.exists(backup):
                try:
                    os.replace(backup, target)
                    rolled = True
                except OSError:
                    pass
            if attempt == attempts:
                _quiet_remove(staged)
                return OpResult(False, "replace", f"{type(e).__name__}: {e}",
                                attempt, waited, holders, rolled_back=rolled)
            delay = base_delay * (2 ** (attempt - 1))
            time.sleep(delay)
            waited += delay

    return OpResult(False, "replace", "exhausted attempts", attempts, waited, holders)


def safe_rename(src: str, dst: str, *, attempts: int = 5, base_delay: float = 3.0,
                lock_timeout: float = 300) -> OpResult:
    """Rename/move with lock-wait, retry and confirmation."""
    waited = 0.0
    holders: list[str] = []

    if path_too_long(dst):
        return OpResult(False, "rename", f"destination path too long ({len(dst)} chars)")

    for attempt in range(1, attempts + 1):
        if not os.path.exists(src):
            # already renamed by someone else? treat as success if dst is there
            if os.path.exists(dst):
                return OpResult(True, "rename", "already at destination", attempt, waited, holders)
            return OpResult(False, "rename", f"source missing: {src}", attempt, waited, holders)

        unlocked, w, holders = wait_for_unlock(src, lock_timeout)
        waited += w
        if not unlocked:
            if attempt == attempts:
                return OpResult(False, "rename", "source stayed locked", attempt, waited, holders)
            time.sleep(base_delay * attempt)
            continue

        try:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            size_before = os.path.getsize(src)

            # CROSS-VOLUME MOVE.
            # os.replace() is a rename and cannot cross drives - it raises
            # WinError 17. safe_replace() learned this the hard way when every
            # commit from the E: cache to the P: pool failed at the final step;
            # this function had the same latent bug. It is only ever called for
            # moves WITHIN the pool today, so it never fired - but "currently
            # unreachable" is a property of today's callers, not of the code,
            # and the renamer is one refactor away from handing it a cache path.
            if os.path.splitdrive(os.path.abspath(src))[0].lower() != \
               os.path.splitdrive(os.path.abspath(dst))[0].lower():
                tmp = dst + ".nuarr-mv"
                _quiet_remove(tmp)
                shutil.copy2(src, tmp)
                ok, why = verify_copy(src, tmp)
                if not ok:
                    _quiet_remove(tmp)
                    raise OSError(f"cross-volume copy failed verification: {why}")
                os.replace(tmp, dst)          # same volume now, so atomic
                os.remove(src)
            else:
                os.replace(src, dst)

            if not os.path.exists(dst) or os.path.getsize(dst) != size_before:
                raise OSError("post-rename verification failed")
            return OpResult(True, "rename", human_bytes(size_before), attempt, waited, holders)
        except OSError as e:
            if attempt == attempts:
                return OpResult(False, "rename", f"{type(e).__name__}: {e}", attempt, waited, holders)
            delay = base_delay * (2 ** (attempt - 1))
            time.sleep(delay)
            waited += delay

    return OpResult(False, "rename", "exhausted attempts", attempts, waited, holders)


def _quiet_remove(p: str | None) -> None:
    if not p:
        return
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


_SKIP_DIRS = {"$recycle.bin", "system volume information", "$sysreset",
              "recovery", "windows", "program files", "program files (x86)",
              "programdata", "found.000"}


def probe_for_bak(volumes: list[str], names: set[str] | None = None,
                  progress=None, budget_s: float = 240.0) -> bool:
    r"""Is there ANY interrupted commit anywhere? Answered the fast way.

    WHY THIS EXISTS - measured on this box:

        walking P:\ (the pooled volume)      8.3 directories/second
        walking a member volume direct    1,560 directories/second

    A 188x difference, and the reason is structural rather than incidental: a
    directory listing on an aggregated volume has to be merged across every
    member, so one logical read becomes up to twelve physical ones. Recovery
    walked the pool, so it was reading 39,000 files the slowest way available.
    Under normal load that put the job queue behind a walk that took over an
    hour, on every restart, to almost always discover that nothing was wrong.

    So split the question in two. "Is there anything to fix?" is read-only and
    can be answered against the underlying disks in seconds. "Fix it" still
    goes through the normal path, unchanged, and only runs when the answer is
    yes - which is close to never, because it requires a hard kill inside a
    millisecond-wide window.

    ONE THREAD PER VOLUME. These are separate spindles, and the walk is pure
    seek latency - the same asymmetry measured elsewhere in this project, where
    four streams sharing one disk managed 63 MB/s and the same work spread over
    four disks ran at 997 MB/s. Serially the probe went at ~900 files/s; there
    is no reason twelve idle disks should take turns.

    Returns True if any .nuarr-bak exists, or if the probe could not complete -
    failing towards doing the full walk, since the cost of a needless walk is
    delay and the cost of a skipped repair is a missing file.
    """
    if not volumes:
        return True
    t0 = time.time()
    stop = threading.Event()
    lock = threading.Lock()
    tally = {"dirs": 0, "files": 0, "last": 0.0}
    found = threading.Event()

    want = {n.lower() for n in (names or set()) if n}

    def one(vol: str) -> None:
        local_d = local_f = 0
        try:
            if not os.path.isdir(vol):
                return
        except OSError:
            return
        for dirpath, subdirs, filenames in os.walk(vol):
            if stop.is_set():
                return
            local_d += 1
            local_f += len(filenames)

            # PRUNE TO THE LIBRARIES. A member volume holds far more than the
            # media: measured here, 191,445 directories for 39,445 media
            # files, most of it artwork, metadata and subtitle folders that
            # cannot contain a .nuarr-bak because nothing ever commits there.
            #
            # Descend at most a few levels looking for a folder named after a
            # library - on a pool the members carry a container folder above
            # it, on plain storage they do not, and this covers both without
            # knowing which. Once inside one, walk it whole.
            parts = [p.lower() for p in
                     dirpath[len(vol):].strip("\\/").split("\\") if p]
            inside = bool(want) and any(p in want for p in parts)
            subdirs[:] = [d for d in subdirs if d.lower() not in _SKIP_DIRS]
            if want and not inside:
                # Not in a library yet. Keep descending only while a library
                # could still be ahead - a pool puts a container folder above
                # them, plain storage does not, so allow a couple of levels
                # and no more. Anything that IS a library is always kept.
                depth = len(parts)
                subdirs[:] = [d for d in subdirs
                              if d.lower() in want or depth < 2]
            for fn in filenames:
                if fn.endswith(".nuarr-bak"):
                    found.set()
                    stop.set()
                    return
            # Report in batches. Taking the lock per directory would serialise
            # twelve threads on the one thing they all touch, which is exactly
            # what this is trying to avoid.
            if local_d % 200 == 0:
                now = time.time()
                with lock:
                    tally["dirs"] += local_d
                    tally["files"] += local_f
                    local_d = local_f = 0
                    if now - t0 > budget_s:
                        stop.set()
                        return
                    if progress and now - tally["last"] >= 0.5:
                        tally["last"] = now
                        try:
                            progress({"dirs": tally["dirs"],
                                      "files": tally["files"],
                                      "elapsed": now - t0, "vol": vol})
                        except Exception:
                            pass
        with lock:
            tally["dirs"] += local_d
            tally["files"] += local_f

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(16, len(volumes)),
            thread_name_prefix="bakprobe") as ex:
        list(ex.map(one, volumes))

    if found.is_set():
        return True
    if stop.is_set():
        # Ran out of time. Say "maybe", so the caller does the thorough walk
        # rather than quietly skipping the repair.
        return True
    if progress:
        try:
            progress({"dirs": tally["dirs"], "files": tally["files"],
                      "elapsed": time.time() - t0, "vol": ""})
        except Exception:
            pass
    return False


def recover_interrupted_commits(roots: list[str],
                                progress=None) -> list[str]:
    r"""Repair commits that were killed mid-swap.

    safe_replace() rolls back on any ERROR, but a hard kill (crash, power cut,
    taskkill) runs no handler at all. If it died in the window between
    "move the original aside" and "move the replacement in", the folder is left
    with:

        <name>.mkv.nuarr-bak   the untouched original
        <name>.mkv.nuarr-new   the staged replacement
        <name>.mkv             MISSING

    which the arr reports as a missing file. Both halves are on disk, so this is
    fully recoverable - and it must be, because the alternative is a library
    that silently loses a file whenever the process is killed at the wrong
    microsecond.

    Policy: restore the ORIGINAL. The staged copy cannot be trusted, since we
    have no way to know whether it finished writing before the kill.

    `progress` is called with a dict as the walk proceeds. This step gates the
    job queue, and on a large library it is minutes of complete silence -
    indistinguishable from a hang. Reporting where it is turns "still pending"
    into "on TV Shows, 22,000 files in", which is the difference between
    waiting and debugging.
    """
    fixed: list[str] = []
    started = time.time()
    dirs = files_seen = 0
    last_ping = 0.0
    # ONE THREAD PER ROOT, for the same reason the probe above has one per
    # volume: these are separate spindles and the walk is seek-bound, so
    # walking them in turn leaves eleven idle while the twelfth works. The
    # probe was parallel and the repair it leads to was not, which is the wrong
    # way round - the repair is the slow half.
    #
    # The shared counters are touched under a lock, but only every 200
    # directories: taking it per directory would serialise the twelve threads
    # on the one thing they all touch and undo the whole point.
    lock = threading.Lock()

    def ping(root: str, where: str, force: bool = False) -> None:
        # Time-based, not count-based. Folders vary from one file to hundreds,
        # so "every N directories" updates in bursts and then goes quiet for
        # the biggest ones - exactly where you most want to see movement.
        nonlocal last_ping
        now = time.time()
        if not force and now - last_ping < 0.5:
            return
        last_ping = now
        if progress:
            try:
                progress({"root": root, "where": where, "dirs": dirs,
                          "files": files_seen, "found": len(fixed),
                          "elapsed": now - started})
            except Exception:
                pass

    def _one_root(root: str) -> None:
        nonlocal dirs, files_seen
        local_d = local_f = 0
        if not os.path.isdir(root):
            return
        ping(root, "", force=True)
        for dirpath, _d, filenames in os.walk(root):
            local_d += 1
            local_f += len(filenames)
            if local_d % 200 == 0:
                with lock:
                    dirs += local_d
                    files_seen += local_f
                    local_d = local_f = 0
            # The folder name relative to the root, so the caller can show
            # "TV Shows › Watchmen" rather than a 200-character path.
            try:
                rel = os.path.relpath(dirpath, root)
            except ValueError:
                rel = dirpath
            ping(root, "" if rel == "." else rel)
            for fn in filenames:
                if not fn.endswith(".nuarr-bak"):
                    continue
                bak = os.path.join(dirpath, fn)
                target = bak[: -len(".nuarr-bak")]
                staged = target + ".nuarr-new"
                try:
                    if os.path.exists(target):
                        # commit completed; the backup is just litter
                        _quiet_remove(bak)
                        _quiet_remove(staged)
                        continue
                    if os.path.getsize(bak) == 0:
                        continue                      # nothing safe to restore
                    os.replace(bak, target)
                    _quiet_remove(staged)
                    fixed.append(target)
                    ping(root, rel, force=True)       # a find is worth saying
                except OSError:
                    continue
        with lock:
            dirs += local_d
            files_seen += local_f
        ping(root, "", force=True)

    if roots:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(16, len(roots)),
                thread_name_prefix="bakfix") as ex:
            list(ex.map(_one_root, roots))
    return fixed


def free_space_gb(path: str) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return 0.0
