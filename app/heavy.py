r"""nuarr - one lane for the heavy, memory-hungry, human-triggered jobs.

WHY THIS EXISTS
---------------
Installing an engine and testing an engine are both button presses, and both
are expensive in the same resource: an install downloads and unpacks hundreds
of megabytes to gigabytes, a test loads a neural model into RAM (and possibly
VRAM). Nothing stopped them overlapping. On a machine with room to spare that
is merely wasteful; on a small VM, starting a Whisper install while a
PaddleOCR test is loading its model is enough to run the box out of memory and
take the server down with it - which is exactly what happened.

The queue and the workers are NOT governed by this. They have their own pools,
their own gate, and they already yield to viewers. This is only for the
handful of operations a person can start from a settings page, where two at
once is never what was meant.

WHAT IT IS NOT
--------------
Not a lock held across a request. The operations here run on threads and can
last minutes; blocking an HTTP handler on them would just move the problem.
`claim()` either succeeds immediately or reports who has the lane, so the page
can say "Whisper is installing - try again when it finishes" instead of
starting something that cannot end well.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_holder: dict = {"what": "", "since": 0.0}

# A claim that is never released - a thread died, a process was killed
# mid-install - must not wedge the lane forever. Nothing here legitimately
# runs longer than this.
STALE_S = 45 * 60.0


def _expired() -> bool:
    return bool(_holder["what"]) and (time.time() - _holder["since"]) > STALE_S


def busy() -> str:
    """What holds the lane right now, or '' when it is free."""
    with _lock:
        if _expired():
            _holder.update(what="", since=0.0)
        return _holder["what"]


def claim(what: str) -> tuple[bool, str]:
    """Take the lane for `what`. -> (got_it, who_has_it_otherwise)."""
    with _lock:
        if _holder["what"] and not _expired():
            return False, _holder["what"]
        _holder.update(what=what, since=time.time())
        return True, ""


def release(what: str = "") -> None:
    """Give the lane back. A mismatched name is ignored, not an error -
    releasing something you do not hold should never raise inside a
    finally block."""
    with _lock:
        if not what or _holder["what"] == what:
            _holder.update(what="", since=0.0)


class Lane:
    """`with Lane("Whisper install") as ok:` - ok is False when busy."""

    def __init__(self, what: str) -> None:
        self.what = what
        self.got = False
        self.holder = ""

    def __enter__(self) -> "Lane":
        self.got, self.holder = claim(self.what)
        return self

    def __exit__(self, *exc) -> None:
        if self.got:
            release(self.what)
