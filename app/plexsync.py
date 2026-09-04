r"""Does Plex still agree with the files? The Plex half of the arr check.

WHAT IT ASKS. For every file nuarr manages, Plex holds its own idea of what is
inside: which streams, in what order, with which flags. That idea is written
when Plex analyses the file and is never revisited unless something asks. nuarr
rewrites files - drops audio tracks, removes picture subtitles once the text
exists, changes which subtitle is default - so the two drift, silently, and the
symptom is a subtitle menu offering tracks that are not there.

The notify queue keeps them together going forward. This is the backstop that
says whether they ARE together, the same way the arr agreement check answers
that question for Sonarr and Radarr, and it exists for the same reason: an
integration you cannot audit is one you are trusting rather than relying on.

HOW IT IS CHEAP ENOUGH TO RUN. Plex will describe sixty items in one request -
measured at 0.15s for 60, against 12ms each one at a time - so a 39,000 file
library is about 650 requests and under a minute of Plex's time. The comparison
is against nuarr's STORED probe rather than a fresh ffprobe, because 39,000
ffprobes is an hour of disk; the stored probe is refreshed by every job and by
the scanner, and where the two disagree the file itself is read once to settle
it. That last step matters: without it this check cannot tell "Plex is stale"
from "nuarr's probe is stale", and would report the wrong system.

WHAT IT DOES ABOUT IT. Nothing, unless asked - or, in auto mode, it hands each
disagreement to the same queue the commit path uses, which analyses and then
verifies. It never edits Plex's database and never touches a file.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import joblog, plexnotify
from .config import SETTINGS
from .db import cursor

CYCLE_S = 6 * 3600
BATCH = 60                       # ratingKeys per Plex request
# How many disagreements may be settled by reading the file itself in one pass.
#
# SMALL ON PURPOSE, AND NOT LOAD-BEARING. The first version allowed 600 and ran
# for over twelve minutes: each read is an ffprobe of a multi-gigabyte remux on
# a pool that is usually busy transcoding, and it was competing with the very
# queue it exists to serve.
#
# It does not need to be thorough, because it is not the authority. Every row
# this check produces is handed to the Plex catch-up queue, and the queue reads
# the file itself before it does anything - so a row that is wrong here costs
# one queue entry that verifies "Plex already had it right" and closes. The
# budget is only here to keep the headline COUNT roughly honest.
ARBITRATE_MAX = 2500
# ...bounded by the clock rather than by the count, because the cost of one
# read depends entirely on what the pool is doing. The first pass capped at 150
# and every one of those 150 turned out to be nuarr's OWN probe being stale
# rather than Plex - which made the headline ("869 items describe a file that
# has since changed") a claim the check had not actually tested. A time budget
# settles as many as it can and says how far it got, which is the difference
# between a number and a guess.
ARBITRATE_SECONDS = 300.0

STATE: dict = {"at": 0.0, "running": False, "checked": 0, "total": 0,
               "rows": [], "by_library": {}, "last_error": "",
               "started": 0.0, "fixing": False, "queued": 0,
               "arbitrated": 0, "stale_probes": 0}


def mode() -> str:
    m = (getattr(SETTINGS, "plexsync_mode", "") or "").strip().lower()
    return m if m in ("auto", "manual") else "manual"


def _files_by_path() -> dict:
    r"""Everything nuarr manages, keyed by lower-case path.

    THE SIGNATURE IS COMPUTED HERE AND THE PROBE THROWN AWAY. Keeping the raw
    JSON for 35,000 files is ~160 MB held for the length of the walk, in the
    process that also runs the encoder - the same mistake the out-of-sync check
    made and had fixed. A signature is a few dozen bytes and is the only part
    of the probe this check ever looks at.
    """
    out = {}
    with cursor() as cur:
        for r in cur.execute(
                "SELECT f.id, f.path, f.title, f.season, f.episode, f.library, "
                "       p.json "
                "  FROM files f JOIN file_probes p ON p.file_id = f.id "
                " WHERE f.state = 'done' AND f.path IS NOT NULL"):
            try:
                sig = plexnotify.probe_sig(json.loads(r["json"]))
            except Exception:                                # noqa: BLE001
                continue
            out[plexnotify.norm_path(r["path"])] = {
                "id": r["id"], "path": r["path"], "title": r["title"],
                "season": r["season"], "episode": r["episode"],
                "library": r["library"], "sig": sig}
    return out


def _section_items(key: str, kind: int) -> list[str]:
    """Every ratingKey in one section. One request."""
    try:
        d = json.loads(plexnotify._get(
            f"/library/sections/{key}/all?type={kind}", timeout=120))
        return [str(m.get("ratingKey")) for m in
                (d.get("MediaContainer") or {}).get("Metadata") or []
                if m.get("ratingKey")]
    except Exception:                                        # noqa: BLE001
        return []


def _batch(keys: list[str]) -> list[dict]:
    """Metadata for up to BATCH items, streams included."""
    try:
        d = json.loads(plexnotify._get("/library/metadata/" + ",".join(keys),
                                       timeout=60))
        return (d.get("MediaContainer") or {}).get("Metadata") or []
    except Exception:                                        # noqa: BLE001
        return []


def _label(f: dict) -> str:
    from .subocr import ep_label
    ep = ep_label(f.get("season"), f.get("episode"))
    return ((f.get("title") or "") + (" · " + ep if ep else "")) or ""


def _describe(sig: tuple) -> str:
    """One phrasing for both systems - see plexnotify.describe()."""
    return plexnotify.describe(sig)


def scan() -> dict:
    r"""Compare every Plex item against the file nuarr knows about.

    Runs on a thread; the caller is a loop or a button, never a request.
    """
    from . import subocr
    if STATE["running"]:
        return STATE
    STATE.update(running=True, checked=0, total=0, rows=[], by_library={},
                 started=time.time(), last_error="")
    rows: list[dict] = []
    arbitrated = 0
    stale_probes = 0
    arb_started = time.time()
    try:
        files = _files_by_path()
        seen_sections = []
        secs_now = plexnotify._sections()
        if not secs_now:
            raise RuntimeError("Plex did not answer when asked for its libraries")
        for root, key, kind in secs_now:
            if not any(k.startswith(root) for k in files):
                continue                 # a section nuarr does not manage
            if key in [k for k, _ in seen_sections]:
                continue
            seen_sections.append((key, kind))
        keys: list[tuple[str, int]] = seen_sections
        allk: list[str] = []
        for key, kind in keys:
            allk += _section_items(key, kind)
        STATE["total"] = len(allk)
        for i in range(0, len(allk), BATCH):
            for m in _batch(allk[i:i + BATCH]):
                part = ((m.get("Media") or [{}])[0].get("Part") or [{}])[0]
                path = (part.get("file") or "")
                f = files.get(plexnotify.norm_path(path))
                if not f:
                    continue             # Plex has it, nuarr does not manage it
                want = f["sig"]
                got = plexnotify.plex_sig(part)
                if not plexnotify.sig_differs(got, want):
                    continue
                # WHO IS STALE? The stored probe is usually right and free;
                # when it disagrees with Plex, only the file settles it. That
                # read costs ~100 ms, which is nothing for the 1% of items that
                # reach here and an hour if it were done for all of them -
                # hence the cap, which turns a runaway into a bounded pass that
                # says how far it got.
                if (arbitrated < ARBITRATE_MAX
                        and time.time() - arb_started < ARBITRATE_SECONDS):
                    arbitrated += 1
                    fresh = subocr._probe_streams(f["path"])
                    if fresh:
                        want = plexnotify.probe_sig(fresh)
                        if not plexnotify.sig_differs(got, want):
                            # PLEX WAS RIGHT AND NUARR WAS NOT. This turned out
                            # to be the common case, not the rare one: 2 Guns
                            # had four subtitle tracks on disk, Plex knew, and
                            # file_probes still described the two it had 28 days
                            # ago. That table is what the subtitle out-of-sync
                            # check reads, so a stale row there is not a
                            # cosmetic problem - it is a wrong answer on another
                            # page. Write the truth down while we have it.
                            stale_probes += 1
                            try:
                                from . import jobs as _jobs
                                _jobs.cache_probe(f["id"], fresh)
                            except Exception:                # noqa: BLE001
                                pass
                            continue
                rows.append({
                    "file_id": f["id"], "path": f["path"],
                    "label": _label(f) or path.rsplit("\\", 1)[-1],
                    "library": f["library"] or "",
                    "rating_key": str(m.get("ratingKey") or ""),
                    "plex": _describe(got), "disk": _describe(want),
                    "n_plex": len(got), "n_disk": len(want)})
            STATE["checked"] = min(i + BATCH, len(allk))
        by: dict = {}
        for r in rows:
            by[r["library"]] = by.get(r["library"], 0) + 1
        STATE.update(rows=rows, by_library=by, at=time.time(),
                     arbitrated=arbitrated, stale_probes=stale_probes)
        tail = (f"; refreshed {stale_probes} stale probe(s) of nuarr's own"
                if stale_probes else "")
        if rows:
            joblog.log(f"Plex agreement check: {len(rows)} of "
                       f"{STATE['total']} item(s) describe a file that has "
                       f"since changed{tail}", "warn")
        else:
            joblog.log(f"Plex agreement check: all {STATE['total']} item(s) "
                       f"match the files{tail}", "ok")
    except Exception as e:                                   # noqa: BLE001
        STATE["last_error"] = f"{type(e).__name__}: {e}"
        joblog.log(f"Plex agreement check failed: {STATE['last_error']}",
                   "error")
    finally:
        STATE["running"] = False
    return STATE


def fix(limit: int = 0, library: str = "") -> dict:
    """Hand every disagreement to the notify queue, which verifies each one.

    Deliberately not a direct analyze: the queue already knows how to wait for
    Plex, re-check, back off and give up out loud. Two pieces of code that both
    talk to Plex would drift, and the queue is the one that reports itself.
    """
    from . import plexqueue
    rows = list(STATE.get("rows") or [])
    if library:
        rows = [r for r in rows if (r.get("library") or "") == library]
    if limit:
        rows = rows[:limit]
    n = 0
    for r in rows:
        try:
            plexqueue.enqueue(r["file_id"], r["path"],
                              why="Plex disagreed with the file")
            n += 1
        except Exception:                                    # noqa: BLE001
            continue
    STATE["queued"] = n
    # HANDED OVER IS NOT OUTSTANDING. The rows now belong to the queue, which
    # reports itself and will say out loud if it cannot deliver one - leaving
    # them here too would have the card and the health row counting work that
    # is already being done, and the count would not move until the next full
    # walk six hours later.
    if n:
        done = {r["file_id"] for r in rows}
        STATE["rows"] = [r for r in (STATE.get("rows") or [])
                         if r["file_id"] not in done]
        by: dict = {}
        for r in STATE["rows"]:
            by[r["library"]] = by.get(r["library"], 0) + 1
        STATE["by_library"] = by
    if n:
        joblog.log(f"Plex agreement check: handed {n} item(s) to the Plex "
                   f"catch-up queue", "ok")
    return {"ok": True, "queued": n}


def libraries() -> list:
    r"""One entry per library nuarr manages, whether or not it has a problem.

    BUILT FROM WHAT IS THERE, NOT FROM WHAT WAS FOUND. Listing only the
    libraries with disagreements means a library that is entirely correct
    vanishes from the page - and so does the difference between "checked and
    fine" and "never looked at". It also means a folder added yesterday would
    not appear until something in it broke. The list comes from the configured
    libraries, and the counts are laid over it.

    Each row carries its files, and each file carries what the catch-up queue
    is doing about it, so the page can say which are already on their way.
    """
    from .config import SETTINGS as _S
    rows = STATE.get("rows") or []
    by: dict = {}
    for r in rows:
        by.setdefault(r["library"] or "", []).append(r)

    # What the queue holds, keyed by file - one query rather than one per row.
    qstate: dict = {}
    try:
        now = time.time()
        with cursor() as cur:
            for q in cur.execute(
                    "SELECT file_id, attempts, last_error, next_try_at "
                    "  FROM plex_queue WHERE done_at IS NULL"):
                qstate[q["file_id"]] = {
                    "queued": True,
                    "attempts": q["attempts"] or 0,
                    "retrying": (q["attempts"] or 0) > 0,
                    "why": q["last_error"] or "",
                    "due_in": max(0.0, (q["next_try_at"] or 0) - now)}
    except Exception:                                        # noqa: BLE001
        pass

    names = [l.name for l in (getattr(_S, "libraries", None) or [])
             if getattr(l, "name", "")]
    for name in by:
        if name and name not in names:
            names.append(name)           # Plex has it, the config no longer does

    out = []
    for name in names:
        mine = by.get(name, [])
        # WHICH ONES GO NEXT. The queue takes them oldest-due first, so the
        # order here is the order they will actually be attempted - and a row
        # that is already queued is not "next", it is under way.
        pend = [r for r in mine if qstate.get(r["file_id"], {}).get("queued")]
        free = [r for r in mine if not qstate.get(r["file_id"], {}).get("queued")]
        files = []
        for i, r in enumerate(mine):
            q = qstate.get(r["file_id"]) or {}
            files.append({**r, **q,
                          "next": (not q.get("queued")) and r in free[:5]})
        out.append({"library": name, "bad": len(mine), "queued": len(pend),
                    "files": files[:400]})
    return out


def _thread_scan():
    """Off the request thread: the walk takes minutes."""
    def _go():
        try:
            scan()                       # scan() stores STATE itself
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"first Plex agreement check: "
                       f"{type(e).__name__}: {e}", "warn")
    import threading
    threading.Thread(target=_go, name="plexsync-first", daemon=True).start()


def cached() -> dict:
    r"""The last answer - and if there is no answer yet, go and get one.

    THE HEALTH PAGE READS THIS, AND SO DOES THE CARD. It used to never scan,
    so after a restart the Plex row said "not checked yet" while the subtitle
    row beside it - whose check starts itself the moment anybody asks - showed
    a real number. Two rows, same page, same kind of question, different
    answers, entirely because of which one happened to have a loop behind it.
    Starting here rather than in the endpoint means every way in behaves the
    same: health page, Plex card, dashboard tile. Still never blocks; the walk
    is a thread and the caller gets the empty answer with running=True.
    """
    from . import schedules as _sch
    _sch.first_look("Plex agreement", lambda: _thread_scan(),
                    not (STATE.get("at") or STATE.get("running"))
                    and all(plexnotify._plex()))
    d = dict(STATE)
    rows = d.pop("rows", []) or []
    return {"have": bool(d.get("at")), "running": bool(d.get("running")),
            "checked": d.get("checked", 0), "total": d.get("total", 0),
            "age_s": (round(time.time() - d["at"], 1) if d.get("at") else None),
            "mode": mode(), "every_h": round(CYCLE_S / 3600),
            "by_library": d.get("by_library", {}),
            "last_error": d.get("last_error", ""),
            "queued": d.get("queued", 0),
            "arbitrated": d.get("arbitrated", 0),
            "stale_probes": d.get("stale_probes", 0),
            "total_bad": len(rows), "rows": rows[:400],
            "libraries": libraries()}


async def refresh() -> dict:
    return await asyncio.to_thread(scan)


async def watch() -> None:
    await asyncio.sleep(180)
    from . import schedules
    while True:
        schedules.beat("plexsync")
        try:
            url, token = plexnotify._plex()
            fresh = time.time() - float(STATE.get("at") or 0)
            if url and token and fresh > CYCLE_S - 120:
                await asyncio.to_thread(scan)
            if url and token and mode() == "auto" and STATE.get("rows"):
                await asyncio.to_thread(fix)
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"Plex agreement loop error: {type(e).__name__}: {e}",
                       "error")
        await asyncio.sleep(CYCLE_S)
