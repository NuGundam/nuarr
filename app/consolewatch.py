r"""Catch console windows that actually appear, and say what opened them.

WHY THIS EXISTS. Console flashes are the hardest class of bug to report and the
easiest to dismiss. They last a fraction of a second, they happen when nobody is
looking, and by the time you think "what was that" there is nothing left to
inspect. Erik reported them twice; both times the only way to find the source
was to sit and sample every process on the machine at 4 Hz until one was caught
in the act. That is a thing to run once, not a way to live.

So this watches continuously and writes down what it sees, and the answer is
waiting the next time the question comes up.

WHAT COUNTS AS ONE. On Windows a console window is drawn by conhost.exe, one
instance per console, spawned as a child of whatever asked for it. So a conhost
appearing is the signal, and its parent is the culprit.

But a console being ALLOCATED is not the same as a console being SEEN.
CREATE_NO_WINDOW asks for no console; STARTF_USESHOWWINDOW with SW_HIDE says a
console that does get created must not be shown. A correctly hidden console
still has a conhost, and logging those would bury the handful that flashed
under thousands that behaved. So every candidate is checked for a window that
is genuinely on screen, and only those are kept.

WHAT IT COSTS. Nothing that would show up. The process list comes from a
toolhelp snapshot through ctypes - no subprocess, which matters more than it
sounds: the ad-hoc diagnostic that found the last one spawned a PowerShell four
times a second, and a process watcher that spawns processes is measuring its
own noise. Roughly a millisecond every two seconds.

DELIBERATELY NOT LIMITED TO NUARR. A flash from Plex, an arr, a scheduled task
or an installer looks exactly the same from the sofa, and a log that only knew
about nuarr's would show nothing at all on the day it was something else. Every
console is recorded; the ones whose ancestry runs back to nuarr are marked, so
"is this ours" is a filter rather than a blind spot.
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes

from . import joblog
from .db import cursor

EVERY_S = 2.0
# A console that opens and closes inside one sampling gap is missed. Two
# seconds catches anything a person would notice as a flash while costing
# nothing; going faster buys very little and starts to be visible in the
# process list itself.

_IS_WIN = os.name == "nt"
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260

STATE: dict = {"running": False, "seen": 0, "visible": 0, "last_at": 0.0,
               "started_at": 0.0, "error": ""}


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * MAX_PATH)]


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS console_windows(
                id          INTEGER PRIMARY KEY,
                at          REAL NOT NULL,
                pid         INTEGER,
                owner_pid   INTEGER,
                owner       TEXT,
                owner_path  TEXT,
                ancestry    TEXT,
                ours        INTEGER DEFAULT 0,
                visible     INTEGER DEFAULT 1,
                title       TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_cw_at "
                    "ON console_windows(at DESC)")
        # Added after the first version shipped rows that named the process
        # but not what it was running. Migrated rather than rebuilt, so
        # anything already caught is kept.
        for col in ("cmdline", "root"):
            try:
                cur.execute(f"ALTER TABLE console_windows ADD COLUMN {col} TEXT")
            except Exception:                                # noqa: BLE001
                pass                                         # already there


def _snapshot() -> dict:
    """pid -> (name, ppid). One call, no subprocess."""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    out = {}
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (
                e.szExeFile.decode("mbcs", "replace"),
                int(e.th32ParentProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def _image_path(pid: int) -> str:
    """Full path of a running process, best effort."""
    k32 = ctypes.windll.kernel32
    # PROCESS_QUERY_LIMITED_INFORMATION - enough for the path, and granted for
    # processes this one could not otherwise open.
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(MAX_PATH * 2)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        k32.CloseHandle(h)
    return ""


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


def _command_line(pid: int) -> str:
    r"""The arguments a process was started with, read from its own memory.

    "python.exe" identifies nothing - it is the command line that says which
    script, and that is the whole answer to "where did this come from". There
    is no Win32 call for another process's command line, so it is read out of
    the PEB: ProcessParameters -> CommandLine, two pointer hops and a string.

    WMI would answer this in one query and is not used, because a query means
    spawning a process, and a watcher that spawns processes creates exactly the
    thing it is watching for. This runs only when a console has already been
    detected, so it is rare and its cost does not matter.

    Best effort throughout: a protected or already-dead process simply returns
    nothing, and the row is still worth having without it.
    """
    if not _IS_WIN:
        return ""
    try:
        k32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
        # QUERY_INFORMATION | VM_READ - needed to walk the PEB.
        h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not h:
            return ""
        try:
            pbi = _PROCESS_BASIC_INFORMATION()
            if ntdll.NtQueryInformationProcess(
                    h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0:
                return ""
            if not pbi.PebBaseAddress:
                return ""

            def read(addr, size):
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf,
                                             size, ctypes.byref(got)):
                    return None
                return buf.raw[:got.value]

            ptr = ctypes.sizeof(ctypes.c_void_p)
            # PEB->ProcessParameters sits at 0x20 on x64, 0x10 on x86.
            off = 0x20 if ptr == 8 else 0x10
            raw = read(pbi.PebBaseAddress + off, ptr)
            if not raw:
                return ""
            params = int.from_bytes(raw, "little")
            # RTL_USER_PROCESS_PARAMETERS->CommandLine: 0x70 on x64, 0x40 x86.
            coff = 0x70 if ptr == 8 else 0x40
            raw = read(params + coff, ctypes.sizeof(_UNICODE_STRING))
            if not raw:
                return ""
            us = _UNICODE_STRING.from_buffer_copy(raw)
            if not us.Length or not us.Buffer:
                return ""
            data = read(us.Buffer, min(us.Length, 4096))
            if not data:
                return ""
            return data.decode("utf-16-le", "replace").strip()
        finally:
            k32.CloseHandle(h)
    except Exception:                                        # noqa: BLE001
        return ""


def _tidy(cmd: str, exe: str = "") -> str:
    r"""The part of a command line worth reading.

    A full command line is mostly a path to an interpreter followed by the
    thing that actually matters. "C:\...\Python313\python.exe C:\nuarr\app\
    paddle_worker.py E:\cache\t0.en.sup --out ..." says paddle_worker, and
    everything before it is noise repeated on every row.
    """
    if not cmd:
        return ""
    out = cmd.strip()
    # Drop a leading quoted or bare executable path - it is already the name.
    if out.startswith('"'):
        end = out.find('"', 1)
        if end > 0:
            out = out[end + 1:].strip()
    elif exe and out.lower().startswith(exe.lower()):
        out = out[len(exe):].strip()
    else:
        first = out.split(" ", 1)
        if "\\" in first[0] and first[0].lower().endswith(".exe"):
            out = first[1].strip() if len(first) > 1 else ""
    # Long absolute paths inside the arguments carry little here either.
    out = re.sub(r"[A-Za-z]:\\[^\s\"]*\\([^\\\s\"]+)", r"\1", out)
    return out[:150]


def _visible_window(pid: int) -> tuple[bool, str]:
    r"""Does this pid own a window that is actually on screen?

    THIS IS THE WHOLE POINT OF THE CHECK. A conhost with no visible window is a
    console that was allocated and correctly hidden - normal, and not what
    anybody is complaining about. Only a window that IsWindowVisible agrees is
    showing gets recorded.
    """
    u32 = ctypes.windll.user32
    found = {"vis": False, "title": ""}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        found["vis"] = True
        found["title"] = buf.value
        return False                       # stop at the first visible one

    try:
        u32.EnumWindows(cb, 0)
    except Exception:                                        # noqa: BLE001
        return False, ""
    return found["vis"], found["title"]


def _ancestry(pid: int, procs: dict, depth: int = 8) -> list:
    """Names up the parent chain, oldest last."""
    out, seen = [], set()
    cur_pid = pid
    while cur_pid and cur_pid not in seen and len(out) < depth:
        seen.add(cur_pid)
        ent = procs.get(cur_pid)
        if not ent:
            break
        out.append(ent[0])
        cur_pid = ent[1]
    return out


def _readable(chain: list) -> str:
    r"""The chain as something a person can scan.

    PIDs ARE NOISE HERE. They were in the first version and they are the least
    useful thing on the row: by the time anybody reads it the process is gone,
    so the number identifies nothing and cannot be looked up. What matters is
    the shape - who owns the console and who is ultimately behind it.

    Consecutive repeats collapse too. "python.exe <- python.exe <- python.exe"
    is one fact, not three, and spelling it out three times pushed the part
    that identifies the culprit off the end of the row.
    """
    if not chain:
        return ""
    parts, i = [], 0
    while i < len(chain):
        n = 1
        while i + n < len(chain) and chain[i + n] == chain[i]:
            n += 1
        parts.append(chain[i] if n == 1 else f"{chain[i]} ×{n}")
        i += n
    return " ← ".join(parts)


def _is_ours(chain: list) -> bool:
    """Does this console's ancestry run back to nuarr?"""
    joined = " ".join(chain).lower()
    return ("nuarr" in joined or "paddle_worker" in joined
            or "pgsrip" in joined)


def _record(pid: int, owner_pid: int, procs: dict, title: str,
            visible: int = 1) -> None:
    ent = procs.get(owner_pid) or ("?", 0)
    chain = _ancestry(owner_pid, procs)
    ours = _is_ours(chain)
    # WHAT it was running, not just what it was called. The command line is
    # read while the process is still alive; a moment later it is unavailable
    # at any price.
    cmd = _tidy(_command_line(owner_pid), ent[0])
    # The oldest ancestor still traceable - the application ultimately behind
    # this, which is what a person actually wants to know first.
    root = chain[-1] if len(chain) > 1 else ""
    row = (time.time(), pid, owner_pid, ent[0], _image_path(owner_pid),
           _readable(chain), int(ours), int(visible), title[:160],
           cmd, root)
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO console_windows(at,pid,owner_pid,owner,"
                "owner_path,ancestry,ours,visible,title,cmdline,root) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    except Exception:                                        # noqa: BLE001
        return
    STATE["visible"] += 1
    where = ("was on screen" if visible == 1
             else "opened and closed before it could be checked")
    # Logged at warn only for our own, because those are the ones that are a
    # defect rather than an observation. Somebody else's installer showing a
    # console is not nuarr's business to complain about - it is recorded, and
    # that is enough.
    joblog.log(f"a console window appeared ({where}): {ent[0]} "
               f"— {' <- '.join(chain[:4])}",
               "warn" if ours else "info")


def sample(prev: dict) -> dict:
    """One pass. Returns the new process map for the next call."""
    procs = _snapshot()
    if not procs:
        return prev
    STATE["last_at"] = time.time()
    for pid, (name, ppid) in procs.items():
        if pid in prev:
            continue                       # not new
        if name.lower() != "conhost.exe":
            continue
        STATE["seen"] += 1
        # THE WINDOW BELONGS TO THE CONSOLE'S OWNER, NOT TO CONHOST. Measured
        # rather than assumed, and it is the opposite of what it sounds like:
        # launching a plain visible console gives a conhost whose own
        # IsWindowVisible is False, while its PARENT - the cmd.exe that asked
        # for the console - owns the window titled "C:\Windows\SYSTEM32\
        # cmd.exe". Checking conhost caught nothing at all.
        vis, title = _visible_window(ppid)
        if vis:
            _record(pid, ppid, procs, title, visible=1)
            continue
        # Owner already gone. A console that opened and closed inside one
        # sampling gap is EXACTLY the flash being hunted, so it is recorded
        # with the visibility marked unknown rather than dropped - the whole
        # point is to catch the ones nobody manages to look at in time.
        if ppid not in procs:
            _record(pid, ppid, procs, "", visible=-1)
    return procs


async def watch() -> None:
    """Background loop. Silent on anything that is not Windows."""
    import asyncio

    if not _IS_WIN:
        return
    from . import schedules
    schedules.register(
        "consolewatch", "Console window watch", "System", EVERY_S,
        what="Notices console windows that actually appear on screen and "
             "records what opened them, with the chain of parents above it. "
             "Consoles that are allocated but correctly hidden are ignored - "
             "only what was visible is kept.")
    try:
        await asyncio.to_thread(init)
    except Exception as e:                                   # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}"
        return
    STATE.update(running=True, started_at=time.time())
    prev = await asyncio.to_thread(_snapshot)
    beat = 0.0
    while True:
        await asyncio.sleep(EVERY_S)
        try:
            prev = await asyncio.to_thread(sample, prev)
            STATE["error"] = ""
        except Exception as e:                               # noqa: BLE001
            STATE["error"] = f"{type(e).__name__}: {e}"
        # A heartbeat every few minutes rather than every pass: the schedules
        # page wants to know it is alive, not a running commentary.
        if time.time() - beat > 300:
            beat = time.time()
            schedules.beat("consolewatch",
                           f"{STATE['visible']} window(s) recorded")


def recent(limit: int = 200, ours_only: bool = False) -> dict:
    """What has been caught, newest first, with a summary by source."""
    try:
        with cursor() as cur:
            q = ("SELECT at,pid,owner_pid,owner,owner_path,ancestry,ours,"
                 "title,cmdline,root FROM console_windows ")
            if ours_only:
                q += "WHERE ours=1 "
            q += "ORDER BY at DESC LIMIT ?"
            rows = [dict(r) for r in cur.execute(q, (int(limit),))]
            tot = cur.execute(
                "SELECT COUNT(*) n, SUM(ours) ours FROM console_windows"
            ).fetchone()
            # Grouped by WHO IS BEHIND IT, not by what got the console. Ten
            # rows of "cmd.exe" says nothing; "cmd.exe, from windows-mcp" and
            # "cmd.exe, from Plex" are two different problems.
            by = [dict(r) for r in cur.execute(
                "SELECT owner, COALESCE(root,'') root, ours, COUNT(*) n, "
                "       MAX(at) last_at "
                "FROM console_windows GROUP BY owner, root, ours "
                "ORDER BY n DESC LIMIT 30")]
    except Exception as e:                                   # noqa: BLE001
        return {"rows": [], "by_source": [], "total": 0, "ours": 0,
                "error": f"{type(e).__name__}"}
    return {"rows": rows, "by_source": by,
            "total": int(tot["n"] or 0), "ours": int(tot["ours"] or 0),
            "watching": bool(STATE.get("running")),
            "since": STATE.get("started_at") or 0.0,
            "consoles_seen": STATE.get("seen", 0),
            "error": STATE.get("error", "")}


def forget() -> int:
    """Clear the record. Returns how many were dropped."""
    with cursor() as cur:
        n = cur.execute("SELECT COUNT(*) n FROM console_windows").fetchone()["n"]
        cur.execute("DELETE FROM console_windows")
    STATE["visible"] = 0
    joblog.log(f"console window log cleared ({n} entries)", "info")
    return int(n or 0)
