r"""
nuarr - where is Plex's file pointer in the file it is serving?

WHY THIS EXISTS
---------------
For a DIRECT PLAY, Plex exposes nothing about how far ahead the client has
buffered - TranscodeSession.maxOffsetAvailable only exists for transcodes, and
integrating /statistics/bandwidth measures growth, not level: it undershoots on
sessions joined mid-stream and overshoots on fresh starts whose range-probes
double-count bytes. Measured live: 30 s reported against a client sitting on
291 s.

But the server itself KNOWS. Plex Media Server opens the file and reads it to
serve the client's range requests, so the position of its file handle is the
furthest byte it has served - which is precisely the "buffered to 14:54" line
the player draws. This module reads that position:

    NtQuerySystemInformation(SystemExtendedHandleInformation)
        -> every open handle on the system, with owning PID
    filter to Plex Media Server's PIDs
        -> DuplicateHandle into this process
    GetFileType == DISK          (safe on pipes; the name queries are not)
        -> GetFinalPathNameByHandleW to match the file we care about
    NtQueryInformationFile(FilePositionInformation)
        -> the byte offset

THE PIPE TRAP. Enumerating handles is a well-known minefield: querying the
NAME of a synchronous pipe handle can hang the querying thread forever. The
order above is deliberate - GetFileType is cheap and safe and eliminates
everything that is not a disk file BEFORE any name lookup happens.

Byte -> time is linear interpolation over the whole file. VBR makes that
approximate (a dark intro packs more seconds per byte than an action scene),
but Matroska interleaves roughly linearly and the error is a few percent -
against an alternative that was wrong by a factor of ten.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes as wt

_ntdll = ctypes.WinDLL("ntdll")
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)

_SystemExtendedHandleInformation = 64
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_PROCESS_DUP_HANDLE = 0x0040
_DUPLICATE_SAME_ACCESS = 0x0002
_FILE_TYPE_DISK = 0x0001
_FilePositionInformation = 14

# Explicit signatures, because the defaults truncate. GetCurrentProcess
# returns the pseudo-handle -1, which as an unsigned 64-bit value overflows
# ctypes' default int conversion the moment it is passed back in.
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.DuplicateHandle.restype = wt.BOOL
_k32.DuplicateHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p,
                                 wt.DWORD, wt.BOOL, wt.DWORD]
_k32.GetFileType.restype = wt.DWORD
_k32.GetFileType.argtypes = [ctypes.c_void_p]
_k32.GetFinalPathNameByHandleW.restype = wt.DWORD
_k32.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                           wt.DWORD, wt.DWORD]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_ntdll.NtQueryInformationFile.restype = wt.ULONG
_ntdll.NtQueryInformationFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, wt.ULONG,
                                          ctypes.c_int]


class _HANDLE_ENTRY(ctypes.Structure):
    _fields_ = [("Object", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p),
                ("HandleValue", ctypes.c_void_p),
                ("GrantedAccess", wt.ULONG),
                ("CreatorBackTraceIndex", wt.USHORT),
                ("ObjectTypeIndex", wt.USHORT),
                ("HandleAttributes", wt.ULONG),
                ("Reserved", wt.ULONG)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]


def _enable_debug_privilege() -> None:
    """SeDebugPrivilege lets an admin duplicate handles out of a service.
    Best effort - without it we simply see fewer handles."""
    try:
        TOKEN_ADJUST = 0x0020 | 0x0008
        htok = ctypes.c_void_p()
        if not _advapi.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_ADJUST,
                                        ctypes.byref(htok)):
            return

        class LUID(ctypes.Structure):
            _fields_ = [("Lo", wt.DWORD), ("Hi", wt.LONG)]

        class LUID_ATTR(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

        class TOKEN_PRIVS(ctypes.Structure):
            _fields_ = [("Count", wt.DWORD), ("Privs", LUID_ATTR * 1)]

        luid = LUID()
        if _advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege",
                                         ctypes.byref(luid)):
            tp = TOKEN_PRIVS()
            tp.Count = 1
            tp.Privs[0].Luid = luid
            tp.Privs[0].Attributes = 0x0002        # SE_PRIVILEGE_ENABLED
            _advapi.AdjustTokenPrivileges(htok, False, ctypes.byref(tp),
                                          0, None, None)
        _k32.CloseHandle(htok)
    except Exception:
        pass


_enable_debug_privilege()


def _pids_named(names: set[str]) -> list[int]:
    """PIDs of processes with the given executable names."""
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    out: list[int] = []
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        low = {n.lower() for n in names}
        if _k32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.lower() in low:
                    out.append(int(e.th32ProcessID))
                if not _k32.Process32NextW(snap, ctypes.byref(e)):
                    break
    finally:
        _k32.CloseHandle(snap)
    return out


def _all_handles() -> tuple[ctypes.Array, int]:
    """The system handle table. Grows the buffer until it fits."""
    size = 1 << 21
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = wt.ULONG(0)
        st = _ntdll.NtQuerySystemInformation(
            _SystemExtendedHandleInformation, buf, size, ctypes.byref(needed))
        st &= 0xFFFFFFFF
        if st == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, int(needed.value) + (1 << 16))
            continue
        if st != 0:
            return (ctypes.Array(), 0) if False else (None, 0)  # type: ignore
        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[0]
        entries = ctypes.cast(
            ctypes.byref(buf, ctypes.sizeof(ctypes.c_size_t) * 2),
            ctypes.POINTER(_HANDLE_ENTRY * count)).contents
        # keep buf alive by attaching it
        entries._buf = buf                                     # type: ignore
        return entries, count


def read_offsets(paths: dict[str, int],
                 proc_names: set[str] | None = None) -> dict[str, int]:
    """Highest CURRENT file-pointer offset per path, for the given processes.

    `paths` maps lowercase absolute path -> anything (only keys are used).
    Returns lowercase path -> byte offset for every match found.
    """
    if not paths:
        return {}
    pids = _pids_named(proc_names or {"Plex Media Server.exe"})
    if not pids:
        return {}
    entries, count = _all_handles()
    if not count:
        return {}
    me = _k32.GetCurrentProcess()
    procs: dict[int, ctypes.c_void_p] = {}
    out: dict[str, int] = {}
    namebuf = ctypes.create_unicode_buffer(4096)
    iosb = _IO_STATUS_BLOCK()
    pos = ctypes.c_longlong(0)
    try:
        for pid in pids:
            h = _k32.OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
            if h:
                procs[pid] = h
        want_pids = set(procs)
        for i in range(count):
            e = entries[i]
            pid = int(e.UniqueProcessId or 0)
            if pid not in want_pids:
                continue
            dup = ctypes.c_void_p()
            if not _k32.DuplicateHandle(procs[pid], e.HandleValue, me,
                                        ctypes.byref(dup), 0, False,
                                        _DUPLICATE_SAME_ACCESS):
                continue
            try:
                # SAFETY ORDER: type first (never hangs), name second
                if _k32.GetFileType(dup) != _FILE_TYPE_DISK:
                    continue
                n = _k32.GetFinalPathNameByHandleW(dup, namebuf, 4096, 0)
                if not n or n >= 4096:
                    continue
                p = namebuf.value
                if p.startswith("\\\\?\\"):
                    p = p[4:]
                p = p.lower()
                if p not in paths:
                    continue
                st = _ntdll.NtQueryInformationFile(
                    dup, ctypes.byref(iosb), ctypes.byref(pos),
                    ctypes.sizeof(pos), _FilePositionInformation)
                if (st & 0xFFFFFFFF) == 0:
                    off = int(pos.value)
                    if off > out.get(p, -1):
                        out[p] = off
            finally:
                _k32.CloseHandle(dup)
    finally:
        for h in procs.values():
            _k32.CloseHandle(h)
    return out


if __name__ == "__main__":
    import sys
    t0 = time.time()
    targets = {a.lower(): 0 for a in sys.argv[1:]}
    r = read_offsets(targets)
    print(f"{(time.time()-t0)*1000:.0f} ms")
    for p, off in r.items():
        sz = os.path.getsize(p)
        print(f"  offset {off:,} of {sz:,}  = {off/sz*100:.1f}%  {p[-60:]}")
    if not r:
        print("  no matching handles found")
