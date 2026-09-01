r"""How big is the library, according to the services that already know?

WHY ASK ANYONE ELSE. nuarr counts files by walking them. On a machine with the
storage attached that is fast and it is the truth. On a machine reaching the
library over SMB it is neither: measured on the sandbox, it had reached 251
files of roughly forty thousand and was still going, so the header read
"251 files - 1.13 TB" for a library holding sixty terabytes. Not wrong, exactly
- it is an honest report of what has been scanned - but read as a total it is
badly misleading, and the number a person wants is the one they cannot see.

Sonarr and Radarr already hold that answer indexed. Measured against this
library: 39,633 files and 60.92 TB in 28 seconds, without touching the share at
all. Plex answers a similar question in 0.4 s and is deliberately NOT used for
this - it counts episodes rather than files, so an episode with two files
counts once, and its music sections are not nuarr's business. The arrs return
the actual file records, which is what nuarr means by a file.

WHY IT DOES NOT REPLACE THE SCAN. The scan knows things the arrs cannot: what
is on disk that no arr manages, which spindle holds what, what has been
processed. This answers exactly one question - how much is there in total - and
only when the local answer cannot be trusted yet.
"""
from __future__ import annotations

import time

from .config import SETTINGS
from . import joblog

# 28 seconds of API calls, and the answer moves when an import lands. Half an
# hour is far longer than an import cycle and far shorter than a scan.
TTL_S = 1800.0
# ON A MACHINE THAT CAN SEE THE DISKS, THIS IS A COMPARISON AND NOT THE ANSWER.
# There the local walk is the truth and the arr total exists only to say how far
# apart the two are - a number that moves when an import lands, which is hours,
# not half-hours. Measured cost of one pass on Erik's box: Sonarr 27.0s, Radarr
# 1.0s. Paying that every thirty minutes to refresh a comparison would be twenty
# minutes of API traffic a day for a figure nobody is waiting on.
LOCAL_TTL_S = 21600.0
_CACHE: dict = {"at": 0.0, "n": 0, "bytes": 0, "by_arr": {}, "running": False}


def _ttl() -> float:
    """How long an answer stays good, which depends on who is asking."""
    try:
        from . import hostio
        return TTL_S if hostio.servers() else LOCAL_TTL_S
    except Exception:                                        # noqa: BLE001
        return TTL_S


def _arr_set() -> tuple:
    """Which arrs are enabled right now, as a comparable key."""
    return tuple(sorted(
        c.name for c in (SETTINGS.arrs or []) if getattr(c, "enabled", True)))


def cached() -> dict:
    r"""The last answer, without asking. Never blocks.

    AN ANSWER ABOUT A DIFFERENT SET OF ARRS IS NOT AN OLD ANSWER, IT IS A
    WRONG ONE. Erik added Sonarr to a machine that had only Radarr, and the
    header went on reporting 2,042 files as authoritative for another
    twenty-five minutes - the cache was inside its half-hour TTL and had no
    idea the question had changed underneath it. Time is the wrong test for
    that; the set of arrs is.
    """
    if _CACHE["n"] and _CACHE.get("arrs") != _arr_set():
        _CACHE.update(n=0, bytes=0, by_arr={}, at=0.0)
    return {"n": _CACHE["n"], "bytes": _CACHE["bytes"],
            "by_arr": dict(_CACHE["by_arr"]),
            "age_s": (round(time.time() - _CACHE["at"], 1)
                      if _CACHE["at"] else None),
            "fresh": bool(_CACHE["at"] and
                          time.time() - _CACHE["at"] < _ttl())}


async def refresh() -> dict:
    """Ask every enabled arr how many files it manages, and how big they are."""
    from .arr import shared_client

    if _CACHE["running"]:
        return cached()
    _CACHE["running"] = True
    n = b = 0
    by: dict = {}
    asked = answered = 0
    try:
        for cfg in (SETTINGS.arrs or []):
            if not getattr(cfg, "enabled", True):
                continue
            asked += 1
            try:
                files = await shared_client(cfg).list_files()
                answered += 1
            except Exception as e:                           # noqa: BLE001
                joblog.log(f"library total: {cfg.name} did not answer: "
                           f"{type(e).__name__}", "warn")
                continue
            cn = len(files)
            cb = sum(int(getattr(f, "size", 0) or 0) for f in files)
            by[cfg.name] = {"n": cn, "bytes": cb}
            n += cn
            b += cb
        # EVERY ARR, OR NONE OF THEM. A partial answer is the dangerous case
        # and it is the one that actually happened: on the sandbox, Radarr
        # replied and Sonarr did not, so the total came back as 2,042 files
        # against a library of nearly forty thousand - and was published as
        # authoritative, complete with "(from the arrs)". Five per cent of the
        # truth wearing the label of the whole.
        #
        # A missing arr is a missing LIBRARY, not a smaller one. Better to keep
        # the local count, which at least knows what it does not know.
        if n and answered == asked and asked:
            _CACHE.update(n=n, bytes=b, by_arr=by, at=time.time(),
                          asked=asked, answered=answered, arrs=_arr_set())
        elif n:
            joblog.log(f"library total: only {answered} of {asked} arr(s) "
                       f"answered, so the total would be short - keeping the "
                       f"local count instead", "warn")
            _CACHE.update(partial=True, asked=asked, answered=answered)
    finally:
        _CACHE["running"] = False
    return cached()


async def watch() -> None:
    r"""Keep the total current, but only where it is needed.

    WHY IT NOW RUNS EVERYWHERE. It used to return immediately on a machine with
    the storage attached, on the reasoning that the local walk is faster and
    truer - which is right about the HEADLINE and wrong about the question
    underneath it. "How many files do the arrs manage, and how many of those has
    nuarr got hold of?" cannot be answered by a machine that only ever asks
    itself, and on the attached pool that gap is the more interesting number:
    Erik's box was 39,634 to 39,596, and the 38 turned out to be two series
    imported minutes earlier and not yet walked. A library that is quietly one
    series behind looks identical to one that is not, unless something asks.

    It asks far less often there, though - see LOCAL_TTL_S. Where the walk is
    the headline this is a footnote, and footnotes do not need refreshing every
    half hour.
    """
    import asyncio

    from . import schedules
    # Give the first scan a chance; if it turns out to be fast, this is never
    # needed and never runs.
    await asyncio.sleep(120)
    schedules.register(
        "arrtotals", "Library total from the arrs", "Library", _ttl(),
        what="Asks Sonarr and Radarr how many files the library holds and how "
             "much space they take. On network storage this is the headline "
             "total, because walking the share to count is far slower than "
             "asking the service that already knows. On local storage it is the "
             "comparison against nuarr's own walk, so it runs every six hours "
             "rather than every thirty minutes.")
    while True:
        try:
            d = await refresh()
            schedules.beat("arrtotals", f"{d['n']:,} files")
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"library total: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(_ttl())
