r"""nuarr - which line of nuarr started that process?

WHY THIS EXISTS. The console watcher can say a console window appeared, that
ffmpeg.exe owned it, and that its parents run back to Nuarr.exe. That is enough
to know it is nuarr's fault and not enough to fix it: nuarr spawns ffmpeg from
twenty-six places, and "one of those twenty-six forgot CREATE_NO_WINDOW" is a
morning of reading rather than an answer.

The information that closes the gap only exists for a moment, inside the
process doing the spawning, and it is the Python stack at the instant of the
call. Captured there, it costs a stack walk per spawn and turns
"ffmpeg.exe <- Nuarr.exe <- svchost.exe" into "integrity.py:148".

ONE HOOK, NOT TWENTY-SIX CALL SITES. Every way of starting a process in Python
ends at subprocess.Popen: subprocess.run, check_output and check_call are
wrappers around it, and on Windows asyncio's own subprocess transport goes
through asyncio.windows_utils.Popen, which subclasses it. Wrapping the one
constructor catches all of them, including any spawn inside a library nuarr did
not write - which is exactly the case a list of known call sites would miss and
the case hardest to find by reading.

DELIBERATELY RECORD-ONLY. The wrapper calls the original first and does its own
work afterwards inside a try, so a fault in this file can slow a spawn down but
cannot stop one. A diagnostic that can break the thing it is diagnosing is not
worth having.
"""
from __future__ import annotations

import os
import threading
import time
import traceback

# pid -> {"exe", "where", "at", "argv"}. Bounded: this is a lookup for consoles
# caught in the last few seconds, not a history. The console watcher samples
# every two seconds, so anything older than a few hundred spawns ago has either
# been matched already or was never going to be.
MAX = 500
_RING: dict = {}
_ORDER: list = []
_LOCK = threading.Lock()

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_SELF = os.path.abspath(__file__)
# Where the standard library lives, so asyncio's own subprocess plumbing can be
# told apart from code somebody wrote.
try:
    _STDLIB = os.path.dirname(os.path.abspath(os.__file__))
except Exception:                                                # noqa: BLE001
    _STDLIB = ""

STATE = {"installed": False, "seen": 0, "error": ""}


def _where() -> str:
    r"""The innermost nuarr frame that led to this spawn.

    Innermost rather than outermost on purpose. The outermost is always the
    event loop or the thread entry point, which is the same answer for every
    spawn in the program and therefore no answer at all. What somebody needs is
    the line holding the arguments - the one that either passes the flag or
    does not.
    """
    # SKIP BY FILENAME, NOT BY COUNTING FRAMES OFF THE END. The first version
    # dropped a fixed two frames for _where() and note(), which is correct only
    # while the call path between the wrapper and here has exactly that shape -
    # and it also let the FALLBACK return this file, so every spawn from
    # outside app/ was reported as "spawntrace.py:115". A trace that names the
    # tracer is worse than no trace: it is confidently wrong.
    # THREE TIERS, BECAUSE THE INNERMOST FRAME IS USUALLY PLUMBING. asyncio's
    # own subprocess transport calls Popen from windows_utils.py, so "the
    # nearest frame that is not this file" answered windows_utils.py:153 for
    # every async spawn in the program - true, identical every time, and no
    # use to anybody. nuarr's own code first; then the innermost frame outside
    # the standard library, which is where a third-party package would show
    # up; and only then whatever is left.
    nearest = lib = ""
    try:
        for fr in reversed(traceback.extract_stack()):
            fn = os.path.abspath(fr.filename)
            if fn == _SELF:
                continue
            where = f"{os.path.basename(fn)}:{fr.lineno}"
            if os.path.dirname(fn) == _APP_DIR:
                return where
            if _STDLIB and fn.startswith(_STDLIB):
                lib = lib or where
            elif not nearest:
                nearest = where
    except Exception:                                            # noqa: BLE001
        pass
    return nearest or lib


def note(pid: int, exe: str, argv: str = "", where: str = "") -> None:
    if not pid:
        return
    with _LOCK:
        _RING[int(pid)] = {"exe": exe or "", "where": where or _where(),
                           "argv": (argv or "")[:300], "at": time.time()}
        _ORDER.append(int(pid))
        STATE["seen"] += 1
        while len(_ORDER) > MAX:
            _RING.pop(_ORDER.pop(0), None)


def where(pid: int) -> dict:
    """What we know about this pid, or {}."""
    with _LOCK:
        return dict(_RING.get(int(pid)) or {})


def install() -> None:
    r"""Wrap subprocess.Popen once. Safe to call again; does nothing twice."""
    import subprocess
    if getattr(subprocess.Popen.__init__, "_nuarr_traced", False):
        STATE["installed"] = True
        return
    original = subprocess.Popen.__init__

    def traced(self, args, *a, **kw):
        # THE ORIGINAL RUNS FIRST AND UNGUARDED. If it raises there is no
        # process to record, and swallowing that exception here would turn a
        # failed spawn into a silent one.
        original(self, args, *a, **kw)
        try:
            if isinstance(args, (list, tuple)):
                exe = str(args[0]) if args else ""
                argv = " ".join(str(x) for x in args[1:])
            else:
                exe = str(args or "").split(" ")[0]
                argv = str(args or "")
            note(getattr(self, "pid", 0) or 0, os.path.basename(exe), argv)
        except Exception as e:                                   # noqa: BLE001
            STATE["error"] = f"{type(e).__name__}: {e}"

    traced._nuarr_traced = True                                  # type: ignore
    try:
        subprocess.Popen.__init__ = traced                       # type: ignore
        STATE["installed"] = True
    except Exception as e:                                       # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"
