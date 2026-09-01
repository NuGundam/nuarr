r"""Files the arrs manage that nuarr has never walked.

WHERE THIS CAME FROM. Putting the arrs' total next to nuarr's own showed a gap
of 38 on a library of nearly forty thousand, and the gap was the only place
that fact existed - the header could say "38 not walked yet" and then had
nothing further to offer. A number you cannot open is a rumour. This is the
list behind it.

NOT EVERY MISSING FILE IS NUARR BEING BEHIND, and that distinction is the
whole point of the check. An arr record with no nuarr row means one of four
quite different things:

  not walked yet   The file is on disk and nuarr simply has not got to it. A
                   recent import, almost always. Fixed by indexing it.
  written off      nuarr HAS a row and that row says deleted, while the file is
                   on disk and the arr still tracks it. Fixed by withdrawing
                   the verdict. This is the one that mattered - see below.
  gone from disk   The arr tracks a file that is not there. nuarr is right and
                   the arr is stale; walking harder will never find it, and
                   the fix belongs in the arr.
  excluded         A rule in nuarr's own settings says to skip this path. It is
                   absent on purpose and there is nothing to correct.
  outside          The arr manages a root that was never given to nuarr. Also
                   deliberate, and the fix is to add the library, not to scan.

Collapsing those five into one "missing" count would have been easy and would
have produced a number that goes up for good reasons and cannot be driven to
zero - which is how a check stops being believed and starts being ignored. The
first real run made the case by itself: of 38 files the arrs managed and nuarr
did not, 24 were a folder Erik had deliberately excluded and 14 were a genuine
fault that had been invisible for eleven days. One number would have said 38
and meant nothing.

WHY IT DOES NOT JUST TRIGGER A SCAN. A full pass walks the whole pool, and
measured here one disk alone reached 114,894 entries in 25 seconds without
finishing. To pick up twelve episodes that is an absurd amount of work, and on
a share it is hours. Each missing file is re-read from the arr and written
through webhooks._sync_file - the same door an arr import already comes
through - so a handful of files costs a handful of requests. The full rescan
stays available beside it for when the targeted walk keeps failing, because
"do it the thorough way" should be a button rather than a rewrite.
"""
from __future__ import annotations

import os
import time

from .config import SETTINGS
from .db import cursor
from . import joblog, scanner

EVERY_S = 6 * 3600.0

NOT_WALKED = "not walked yet"
WROTE_OFF = "written off, but on disk"
REJECTED = "rejected, waiting for a replacement"
GONE = "gone from disk"
EXCLUDED = "excluded by a rule"
OUTSIDE = "outside nuarr's libraries"

# THE FIFTH REASON WAS FOUND BY THE CHECK ON ITS FIRST REAL RUN, and it is the
# one that mattered. Fourteen episodes of See (2019) came back as "not walked
# yet"; they had in fact been walked, had rows, and those rows said DELETED
# while all fourteen files sat on disk and Sonarr went on tracking them. Being
# marked deleted is not a state a file recovers from on its own - nothing
# queues it, nothing counts it, nothing reports it - so they had been invisible
# to every part of nuarr since 20 August.
#
# The scan does not rescue them either, and this is why: when it re-finds a
# file it already has a row for, it writes the row's PREVIOUS state back. That
# is right for every state except this one, so a file that was written off in
# error stays written off no matter how many times it is walked past. The disk
# move recorded on 21 August proves the scanner had been touching these rows
# for a fortnight without ever reconsidering.
#
# Only the last two are nuarr's to fix, and the buttons must agree with the
# prose above rather than re-deciding it.
FIXABLE = (NOT_WALKED, WROTE_OFF)

_CACHE: dict = {
    "at": 0.0, "data": None, "running": False,
    "done": 0, "total": 0, "now": "", "t0": 0.0, "t1": 0.0,
    "arrs": {}, "fixing": None, "last_fix": None, "failures": [],
}


def mode() -> str:
    """"manual" or "auto". Anything unrecognised is manual - never auto."""
    m = str(getattr(SETTINGS, "arrgap_mode", "manual") or "manual").lower()
    return "auto" if m == "auto" else "manual"


def _every_h() -> float:
    try:
        return max(1.0, float(getattr(SETTINGS, "arrgap_every_h", 6) or 6))
    except Exception:                                        # noqa: BLE001
        return 6.0


def _rate() -> float:
    t0, t1 = _CACHE.get("t0") or 0.0, _CACHE.get("t1") or 0.0
    el = (t1 or time.time()) - t0
    return round(_CACHE.get("done", 0) / el, 1) if (t0 and el > 1.0) else 0.0


def _eta() -> float:
    t0, t1 = _CACHE.get("t0") or 0.0, _CACHE.get("t1") or 0.0
    done, total = _CACHE.get("done", 0), _CACHE.get("total", 0)
    el = (t1 or time.time()) - t0
    if t0 and total and done > 10 and el > 3.0 and not t1:
        return round((total - done) * (el / done), 0)
    return 0.0


def _why(path: str, state: str | None, reason: str | None = None) -> str:
    """Which of the five this file is - decided in the cheapest order.

    Both settings answers cost nothing and are certain; the disk check costs a
    stat per file and is the only one that can be wrong for a moment, so it
    goes last and is never reached for a file that was never nuarr's business.

    `state` is the state of an EXISTING row, or None when there is no row at
    all. A deleted row and no row look identical from the outside - neither is
    counted, neither is queued - but they need opposite fixes: one is a file to
    index, the other is a verdict to withdraw.
    """
    if not path:
        return GONE
    if scanner.is_excluded(path):
        return EXCLUDED
    if scanner._library_of(path) == scanner.OUTSIDE:
        return OUTSIDE
    if not os.path.exists(path):
        # A deleted row and a missing file AGREE, and agreement is not a
        # problem. The arr is the one out of date here.
        return GONE
    if state != "deleted":
        return NOT_WALKED
    # A DELIBERATE REJECTION IS NOT AN ACCIDENT, and putting it back would
    # undo a decision nuarr made on purpose. refetch parks a row at 'deleted'
    # after blocklisting a damaged release and asking the arr to search again;
    # the file stays on disk until the replacement lands. That is the system
    # working, and it must not appear on a list of things to correct.
    #
    # It is only distinguishable by the reason - which the scan used to erase
    # on every pass, so on Erik's box all fourteen of these arrived here
    # anonymous. That erasure is fixed in scanner.py; rows written off before
    # the fix will still read as WROTE_OFF, because the evidence is gone and
    # guessing at it would be worse than saying so.
    if str(reason or "").startswith("rejected and re-searched"):
        return REJECTED
    return WROTE_OFF


async def scan() -> dict:
    """Ask every enabled arr what it manages, and find what nuarr has not got.

    ONE LIST PER ARR AND NOTHING PER FILE. The arr answers with its whole file
    table in a single request - 27s for 37,592 records here - and nuarr's side
    is one indexed query. The expensive-looking part of this check is the part
    that is already paid for.
    """
    from .arr import shared_client

    if _CACHE["running"]:
        return cached()
    _CACHE.update(running=True, done=0, total=0, now="",
                  t0=time.time(), t1=0.0, arrs={})
    rows: list = []
    checked = asked = answered = 0
    try:
        for cfg in (SETTINGS.arrs or []):
            if not getattr(cfg, "enabled", True):
                continue
            asked += 1
            st = _CACHE["arrs"].setdefault(
                cfg.name, {"kind": cfg.kind, "done": 0, "total": 0,
                           "missing": 0, "error": ""})
            try:
                files = await shared_client(cfg).list_files()
                answered += 1
            except Exception as e:                           # noqa: BLE001
                st["error"] = type(e).__name__
                joblog.log(f"not-walked check: {cfg.name} did not answer: "
                           f"{type(e).__name__}", "warn")
                continue
            # STATE, NOT JUST PRESENCE. Asking only "is there a row" would have
            # reported the fourteen written-off files as fully accounted for,
            # which is how they stayed hidden in the first place; asking only
            # for live rows would have called them unwalked, which is what the
            # first version of this did and it was wrong in a way that made the
            # fix a no-op. The state is the whole answer, so fetch the state.
            with cursor() as cur:
                have = {r["arr_file_id"]: (r["state"], r["state_reason"])
                        for r in cur.execute(
                            "SELECT arr_file_id, state, state_reason FROM files "
                            "WHERE arr_name=? AND arr_file_id IS NOT NULL",
                            (cfg.name,)).fetchall()}
            st["total"] = len(files)
            _CACHE["total"] = _CACHE.get("total", 0) + len(files)
            for f in files:
                st["done"] += 1
                _CACHE["done"] += 1
                checked += 1
                fid = getattr(f, "file_id", None)
                if fid is None:
                    continue
                state, reason = have.get(fid, ("", None))
                if fid in have and state != "deleted":
                    continue                      # nuarr has it and counts it
                path = getattr(f, "path", "") or ""
                _CACHE["now"] = os.path.basename(path)
                st["missing"] += 1
                rows.append({
                    "arr": cfg.name, "kind": cfg.kind, "file_id": fid,
                    "parent_id": getattr(f, "parent_id", None),
                    "path": path,
                    "name": os.path.basename(path),
                    # The folder two up is the series or the film, which is what
                    # a person recognises - twelve rows of episode filenames all
                    # begin with the same forty characters.
                    "group": os.path.basename(os.path.dirname(
                        os.path.dirname(path))) or os.path.basename(
                            os.path.dirname(path)),
                    "size": int(getattr(f, "size", 0) or 0),
                    "library": scanner._library_of(path),
                    "state": state or None,
                    "note": reason or "",
                    "why": _why(path, state, reason),
                })
        by_why: dict = {}
        for r in rows:
            b = by_why.setdefault(r["why"], {"n": 0, "bytes": 0})
            b["n"] += 1
            b["bytes"] += r["size"]
        _CACHE["data"] = {
            "checked": checked, "rows": rows, "total": len(rows),
            "by_why": by_why,
            "fixable": sum(1 for r in rows if r["why"] in FIXABLE),
            "asked": asked, "answered": answered,
            "partial": answered != asked,
        }
        _CACHE["at"] = time.time()
    finally:
        _CACHE.update(running=False, t1=time.time())
    return cached()


async def fix(only: list | None = None) -> dict:
    """Walk just the files that are missing because nobody has walked them.

    THE SAME DOOR AN IMPORT COMES THROUGH. webhooks._sync_file re-reads one
    record from the arr and upserts it, applying nuarr's exclusion rules and
    library boundary on the way in. Writing a second insert here would have
    been a few lines shorter and would have been a second definition of what a
    file row means - the kind of duplication that stays correct for exactly as
    long as nobody edits either copy.
    """
    from . import webhooks
    from .arr import shared_client

    if (_CACHE.get("fixing") or {}).get("running"):
        return cached()
    d = _CACHE.get("data") or {}
    todo = [r for r in (d.get("rows") or []) if r["why"] in FIXABLE]
    if only:
        todo = [r for r in todo if r["arr"] in only]
    fx = {"running": True, "done": 0, "total": len(todo), "walked": 0,
          "failed": 0, "where": "", "t0": time.time()}
    _CACHE["fixing"] = fx
    fails: list = []
    by_arr = {c.name: c for c in (SETTINGS.arrs or [])}
    try:
        clients: dict = {}
        titles: dict = {}
        for r in todo:
            fx["done"] += 1
            fx["where"] = r["name"]
            cfg = by_arr.get(r["arr"])
            if cfg is None:
                fx["failed"] += 1
                continue
            try:
                cl = clients.get(cfg.name)
                if cl is None:
                    cl = clients[cfg.name] = shared_client(cfg)
                await webhooks._sync_file(
                    cfg, r["file_id"], r.get("parent_id"),
                    "found by the not-walked check", client=cl,
                    title_cache=titles.setdefault(cfg.name, {}))
                # WITHDRAW THE VERDICT, which the upsert above will not do.
                # _sync_file conflicts on (arr_name, arr_file_id) and updates
                # path, size, disk and the rest - deliberately never state,
                # because an import must not reset a file's processing
                # progress. So on a row marked deleted it refreshed fourteen
                # rows perfectly and left all fourteen written off.
                #
                # Narrow on purpose: the arr still tracks this file AND it is
                # on disk. Both halves are checked here rather than trusted
                # from the scan, because the scan may be minutes old and this
                # is the write that undoes a deletion.
                if r["why"] == WROTE_OFF and os.path.exists(r["path"]):
                    with cursor() as cur:
                        cur.execute(
                            "UPDATE files SET state='new', state_reason=?, "
                            "updated_at=? WHERE arr_name=? AND arr_file_id=? "
                            "AND state='deleted'",
                            ("written off, but the arr still tracks it and it "
                             "is on disk - put back by the not-walked check",
                             time.time(), cfg.name, r["file_id"]))
                # THE CONFIRMATION HAS TO BE THE THING THAT WAS WRONG. This
                # asked only whether a row existed, and a row DID exist - the
                # deleted one - so the first run reported "walked 14, failed 0"
                # having changed nothing at all. A check that cannot fail is
                # not a check.
                with cursor() as cur:
                    got = cur.execute(
                        "SELECT state FROM files WHERE arr_name=? AND "
                        "arr_file_id=?", (cfg.name, r["file_id"])).fetchone()
                if got and got["state"] != "deleted":
                    fx["walked"] += 1
                elif got:
                    fx["failed"] += 1
                    fails.append(dict(r, why="still marked deleted afterwards"))
                else:
                    fx["failed"] += 1
                    fails.append(dict(r, why="the walk did not produce a row"))
            except Exception as e:                           # noqa: BLE001
                fx["failed"] += 1
                fails.append(dict(r, why=f"{type(e).__name__}: {e}"))
        _CACHE["last_fix"] = {"walked": fx["walked"], "failed": fx["failed"],
                              "at": time.time()}
        _CACHE["failures"] = fails
        if fx["walked"]:
            joblog.log(f"not-walked check: indexed {fx['walked']} file(s) the "
                       f"arrs already knew about", "info")
        if fails:
            joblog.log(f"not-walked check: {len(fails)} file(s) could not be "
                       f"indexed - see the card", "warn")
    finally:
        fx["running"] = False
        fx["t1"] = time.time()
    await scan()
    return cached()


def attention() -> dict | None:
    r"""What the Attention tile should say, or nothing at all.

    THE SAME THREE-WAY SPLIT AS THE OTHER CHECKS. Files nuarr TRIED to walk and
    could not need a person in either mode. Files waiting in MANUAL mode are
    work waiting on a click. In AUTO mode they are not raised, because the next
    pass already has them.

    The other three reasons never appear here at all: an excluded file, a file
    outside the configured libraries, and a stale arr record are all either
    deliberate or somebody else's to fix, and a tile that lists things you are
    not going to act on is a tile you stop reading.
    """
    fails = _CACHE.get("failures") or []
    if fails:
        return {"what": "files not walked", "n": len(fails),
                "note": "could not be indexed - needs a look",
                "goto": "/settings#libs"}
    d = _CACHE.get("data") or {}
    n = int(d.get("fixable") or 0)
    if n and mode() == "manual":
        return {"what": "files not walked", "n": n,
                "note": "the arrs track these and nuarr has no entry",
                "goto": "/settings#libs"}
    return None


def cached() -> dict:
    d = _CACHE.get("data")
    return {"have": bool(d), "running": bool(_CACHE.get("running")),
            "age_s": (round(time.time() - _CACHE["at"], 1)
                      if _CACHE.get("at") else None),
            "mode": mode(), "every_h": int(_every_h()),
            "arrs": dict(_CACHE.get("arrs") or {}),
            "progress": {"done": _CACHE.get("done", 0),
                         "total": _CACHE.get("total", 0),
                         "now": _CACHE.get("now", ""),
                         "files": _CACHE.get("done", 0),
                         "rate": _rate(), "eta_s": _eta()},
            "fixing": _CACHE.get("fixing"),
            "last_fix": _CACHE.get("last_fix"),
            "failures": _CACHE.get("failures") or [],
            "kinds": {"not_walked": NOT_WALKED, "gone": GONE,
                      "excluded": EXCLUDED, "outside": OUTSIDE},
            **(d or {"checked": 0, "rows": [], "total": 0, "fixable": 0,
                     "by_why": {}})}


def by_library() -> dict:
    """library -> {n, fixable}, for the line under each row in Settings."""
    out: dict = {}
    for r in ((_CACHE.get("data") or {}).get("rows") or []):
        lib = r.get("library") or ""
        if not lib or lib == scanner.OUTSIDE:
            continue
        e = out.setdefault(lib, {"n": 0, "fixable": 0})
        e["n"] += 1
        if r["why"] in FIXABLE:
            e["fixable"] += 1
    return out


async def watch() -> None:
    r"""Re-ask on a schedule, and in auto mode walk what it finds.

    Six hours by default rather than the twelve the agreement check uses. This
    one is looking for files that arrived MINUTES ago and have not been indexed
    yet, so a long interval would mostly be measuring how long ago the last
    scan ran. It is also cheap in the way that matters - two list calls, no
    disk walk - so the cost of asking more often is a few seconds of Sonarr's
    time, not a pass over the pool.
    """
    import asyncio

    from . import schedules
    schedules.register(
        "arrgap", "Files not walked", "Library", EVERY_S,
        what="Compares what Sonarr and Radarr manage against what nuarr has "
             "indexed, and lists what nuarr has no entry for - separating a "
             "recent import it has not reached yet from a stale arr record, "
             "an excluded path, and a root nuarr was never given. In auto "
             "mode it indexes the first kind; the other three are never "
             "touched, because none of them is nuarr's to fix.")
    # Let the first scan of a cold start finish before adding API traffic - and
    # give it a chance to make this check unnecessary in the first place.
    await asyncio.sleep(300)
    while True:
        try:
            await scan()
            d = _CACHE.get("data") or {}
            n, fixable = int(d.get("total") or 0), int(d.get("fixable") or 0)
            schedules.beat("arrgap",
                           f"{fixable} to walk" if fixable
                           else (f"{n} accounted for" if n else "all walked"))
            if fixable and mode() == "auto":
                joblog.log(f"not-walked check: {fixable} file(s) the arrs "
                           f"track are not indexed - walking them (auto mode)",
                           "info")
                await fix()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"not-walked check: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(_every_h() * 3600.0)
