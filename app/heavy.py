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
_holder: dict = {"what": "", "since": 0.0, "ttl": 0.0}

# A claim that is never released - a thread died, a request was cancelled
# between the claim and the work, a process was killed mid-install - must not
# wedge the lane. THE TIMEOUT BELONGS TO THE CLAIM, not to the module: an
# install may legitimately run for half an hour on a slow line, while a test
# that has not finished in fifteen minutes is not going to.
#
# This was learned the hard way within an hour of writing it: a cancelled
# test left the lane held with no process behind it and nothing on any page
# could say so, which is precisely the kind of invisible stuck state the rest
# of nuarr goes out of its way to avoid.
STALE_S = 45 * 60.0          # default, and the ceiling for installs
TEST_TTL_S = 15 * 60.0       # anything called a test


def _ttl() -> float:
    return float(_holder.get("ttl") or STALE_S)


def _expired() -> bool:
    return bool(_holder["what"]) and (time.time() - _holder["since"]) > _ttl()


def busy() -> str:
    """What holds the lane right now, or '' when it is free."""
    with _lock:
        if _expired():
            _holder.update(what="", since=0.0, ttl=0.0)
        return _holder["what"]


def state() -> dict:
    """The lane, for a page or a log to show. Never raises."""
    with _lock:
        expired = _expired()
        if expired:
            _holder.update(what="", since=0.0, ttl=0.0)
        held = _holder["what"]
        return {"busy": bool(held), "what": held,
                "for_s": round(time.time() - _holder["since"], 1) if held else 0,
                "expires_in_s": (round(_holder["since"] + _ttl() - time.time())
                                 if held else 0)}


def clear() -> str:
    """Force the lane open. Returns what was released, for the log."""
    with _lock:
        was = _holder["what"]
        _holder.update(what="", since=0.0, ttl=0.0)
        return was


def claim(what: str, ttl: float = 0.0) -> tuple[bool, str]:
    """Take the lane for `what`. -> (got_it, who_has_it_otherwise)."""
    with _lock:
        if _holder["what"] and not _expired():
            return False, _holder["what"]
        if not ttl:
            ttl = TEST_TTL_S if "test" in what.lower() else STALE_S
        _holder.update(what=what, since=time.time(), ttl=ttl)
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

    def __init__(self, what: str, ttl: float = 0.0) -> None:
        self.what = what
        self.ttl = ttl
        self.got = False
        self.holder = ""

    def __enter__(self) -> "Lane":
        self.got, self.holder = claim(self.what, self.ttl)
        return self

    def __exit__(self, *exc) -> None:
        if self.got:
            release(self.what)
