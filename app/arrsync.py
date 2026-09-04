r"""Does what the arr believes still match what is on disk?

WHY THIS EXISTS AS A PAGE RATHER THAN A HOPE. The language drift was found by
accident - a glance at a Sonarr import screen showed one file tagged English
beside siblings tagged Multi-Languages, and pulling that thread turned up 1,246
records describing audio tracks nuarr had removed months earlier. Nothing was
broken enough to complain, so nothing did.

That is the shape of every drift between two systems that each believe they own
the truth: no error, no failure, just two answers to the same question. The
answer is not to be more careful, it is to ask the question on a schedule and
put the number somewhere it is seen.

WHAT COUNTS AS DRIFT. Only differences where nuarr has better information than
the arr. nuarr probes the file with ffprobe after every rewrite; the arr's
record was written when the release was imported and is only refreshed when
something asks. So where the two disagree about the CONTENT of a file, the
probe is right - and where they disagree about anything else, this says so
rather than guessing which side to believe.
"""
from __future__ import annotations

import os
import time

from .config import SETTINGS
from .db import cursor
from . import joblog

# Recomputing this walks every arr record: minutes, not milliseconds. The card
# reads the last answer and asks for a new one on its own schedule.
_CACHE: dict = {"at": 0.0, "data": None, "running": False,
                "done": 0, "total": 0, "where": "",
                # WHAT WAS TRIED AND DID NOT WORK, kept separately from what
                # was merely found. A disagreement is normal and expected -
                # nuarr rewrites files, the arrs notice later. A disagreement
                # that survived a correction attempt is not: something is
                # wrong that no amount of waiting will fix, and it is the one
                # case where the mode does not matter.
                "failures": [], "fixed_at": 0.0, "last_fix": None,
                # Put right is minutes of work, so it reports like the scan
                # does rather than sitting behind one unmoving sentence.
                "fixing": None}
TTL_S = 1800.0
EVERY_S = 12 * 3600.0


def _retire(check: str, ids: set) -> None:
    r"""Drop rows that have just been corrected out of the live answer.

    THE NUMBER HAS TO COME DOWN WHILE YOU WATCH. Put right takes minutes, and
    a count that sits at 1,136 for all of them tells you nothing about whether
    it is working - you cannot tell progress from a stall. Retiring each row
    the moment its correction is CONFIRMED means the count falls, the list
    empties, and what is left at the end is exactly what did not work.

    Deliberately driven by confirmations rather than by attempts: a row that
    was tried and refused must stay on screen, because it is the part that
    still needs a person.
    """
    d = _CACHE.get("data")
    if not d or not ids:
        return
    keep = [r for r in (d.get("list") or [])
            if not (r.get("check") == check and r.get("file_id") in ids)]
    gone = len(d.get("list") or []) - len(keep)
    if not gone:
        return
    d["list"] = keep
    counts = dict(d.get("counts") or {})
    counts[check] = max(0, int(counts.get(check, 0)) - gone)
    d["counts"] = counts
    d["total"] = max(0, int(d.get("total", 0)) - gone)
    rows = dict(d.get("rows") or {})
    if check in rows:
        rows[check] = [r for r in rows[check] if r.get("file_id") not in ids]
        d["rows"] = rows

CHECKS = {
    "languages": {
        "label": "Language",
        "what": "the arr lists audio languages the file does not contain",
        "why": "nuarr's audio policy drops tracks the arr still remembers",
        "fixable": True,
        "fix": "sets the arr's languages to what ffprobe found",
    },
    "size": {
        "label": "File size",
        "what": "the arr's recorded size is not the size on disk",
        "why": "nuarr rewrote the file and the arr has not looked since",
        "fixable": True,
        "fix": "asks the arr to re-read the file",
    },
    "video": {
        "label": "Video",
        "what": "the arr's media info describes a different codec or height",
        "why": "the same - a rewrite the arr has not noticed",
        "fixable": True,
        "fix": "asks the arr to re-read the file",
    },
}


def _probe_rows() -> dict:
    """Everything nuarr knows, keyed the way an arr record can be looked up."""
    out = {}
    with cursor() as c:
        for r in c.execute(
                "SELECT f.arr_name, f.arr_file_id, f.arr_parent_id, f.path, "
                "       f.audio_langs, f.size, f.video_codec, f.height, p.json "
                "  FROM files f LEFT JOIN file_probes p ON p.file_id = f.id "
                " WHERE f.arr_file_id IS NOT NULL "
                "   AND f.state NOT IN ('deleted','duplicate')"):
            out[(r["arr_name"], r["arr_file_id"])] = dict(r)
    return out


def _size_on_disk(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


async def scan() -> dict:
    r"""Compare every arr record against nuarr's probe. Read-only, and slow."""
    from .arr import shared_client
    from . import arrlang

    import asyncio

    probe = _probe_rows()
    found = {k: [] for k in CHECKS}
    counts = {"checked": 0}

    async def one_arr(cfg) -> None:
        r"""One arr's whole pass, with its own progress.

        RUN CONCURRENTLY, NOT ONE AFTER THE OTHER. This walk is almost entirely
        waiting: a request goes to the arr, and nothing happens on this machine
        until it answers. Doing Sonarr's 1,100 titles and then Radarr's 3,142 in
        sequence spends the whole of each arr's latency twice over for no
        reason - they are separate services on separate ports that know nothing
        of each other.

        Each keeps its OWN counters rather than sharing one pair. Two arrs
        advancing a single total produced a number that could not be read: it
        moved at the sum of two rates, past a total that grew whenever either
        was still counting, and said nothing about which one was slow.
        """
        st = _CACHE["arrs"][cfg.name]
        client = shared_client(cfg)
        kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
        key = "seriesId" if cfg.kind == "sonarr" else "movieId"
        try:
            parents = {f.parent_id for f in await client.list_files()}
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"arr sync check: cannot list {cfg.name}: "
                       f"{type(e).__name__}", "warn")
            st.update(done=0, total=0, running=False, t1=time.time(),
                      error=type(e).__name__)
            return
        st["total"] = len(parents)
        for pid in parents:
            st["done"] += 1
            try:
                raw = await client._get(f"/{kind}", **{key: pid})
            except Exception:                                # noqa: BLE001
                continue
            for rec in raw or []:
                mine = probe.get((cfg.name, rec.get("id")))
                if not mine:
                    continue
                counts["checked"] += 1
                st["files"] += 1
                # NAME WHAT IS UNDER THE NEEDLE. A bar that only moves is
                # evidence of motion; a bar that also says which file it is on
                # is evidence of PROGRESS, and it is the difference between
                # watching a spinner and watching work.
                st["now"] = os.path.basename(mine.get("path") or "")
                # 'arr' IS THE ARR'S NAME AND NOTHING ELSE. The size and video
                # rows below used to add arr=<the value the arr believed>,
                # which overwrote it - so a size row carried arr=1332586482
                # where the name should be, fix() could not match that to any
                # configured arr, and every one of them was skipped. It logged
                # "asked the arrs to re-read 0 file(s)" and the panel called
                # that success. The believed value is 'was' now; the two are
                # never the same key again.
                row = {"arr": cfg.name, "kind": cfg.kind,
                       "file_id": rec.get("id"),
                       "parent_id": mine.get("arr_parent_id"),
                       "path": mine.get("path") or rec.get("path") or ""}

                # --- languages, the one that started this ------------------
                actual = arrlang._canon(arrlang._iso_set(
                    mine.get("audio_langs") or ""))
                claimed = arrlang._canon(arrlang._claimed_iso(rec))
                if actual and claimed and claimed > actual:
                    found["languages"].append(
                        dict(row, claimed=sorted(claimed),
                             actual=sorted(actual)))

                # --- size --------------------------------------------------
                # NUARR'S RECORD FIRST, THE DISK ONLY TO SETTLE IT. Statting
                # every file to answer this meant 39,588 round trips to twelve
                # spinning disks that are usually busy with actual work, for a
                # question the scan already knows the answer to nearly always.
                # nuarr's size column is refreshed on every walk, so it screens
                # out the 99% that agree; the disk is then asked only about the
                # handful that do not, which is where the file being the
                # authority actually matters.
                arr_size = int(rec.get("size") or 0)
                my_size = int(mine.get("size") or 0)
                # A byte or two is not drift; anything a rewrite would cause is
                # far larger. 1 MB keeps rounding and metadata edits out of a
                # list meant for real differences.
                if arr_size and my_size and abs(my_size - arr_size) > 1_000_000:
                    disk = _size_on_disk(mine.get("path") or "")
                    if disk and abs(disk - arr_size) > 1_000_000:
                        # The DIFFERENCE, carried explicitly. Both numbers
                        # round to "1.2 GB" on a panel, so a row showing
                        # 1.2 -> 1.2 looks like a bug in the check rather than
                        # an 8.6 MB drift.
                        found["size"].append(
                            dict(row, was=arr_size, disk=disk,
                                 delta=disk - arr_size))

                # --- video codec / height ---------------------------------
                mi = rec.get("mediaInfo") or {}
                a_codec = str(mi.get("videoCodec") or "").lower()
                a_h = int(mi.get("height") or 0)
                m_codec = str(mine.get("video_codec") or "").lower()
                m_h = int(mine.get("height") or 0)
                if m_codec and a_codec and not _codec_same(a_codec, m_codec):
                    found["video"].append(
                        dict(row, was=a_codec, actual=m_codec, field="codec"))
                elif m_h and a_h and abs(a_h - m_h) > 8:
                    found["video"].append(
                        dict(row, was=f"{a_h}p", actual=f"{m_h}p",
                             field="height"))
        st["running"] = False
        st["t1"] = time.time()

    live = [c for c in (SETTINGS.arrs or []) if getattr(c, "enabled", True)]
    now = time.time()
    _CACHE["arrs"] = {
        c.name: {"name": c.name, "kind": c.kind, "done": 0, "total": 0,
                 "files": 0, "now": "", "running": True, "t0": now, "t1": 0.0,
                 "error": ""}
        for c in live}
    # gather, not a loop of awaits. return_exceptions so one arr falling over
    # cannot take the other's results down with it - a half answer from a
    # reachable arr is worth more than no answer at all.
    results = await asyncio.gather(*(one_arr(c) for c in live),
                                   return_exceptions=True)
    for c, r in zip(live, results):
        if isinstance(r, Exception):
            joblog.log(f"arr sync check: {c.name} failed: "
                       f"{type(r).__name__}: {r}", "warn")
            st = _CACHE["arrs"].get(c.name)
            if st:
                st.update(running=False, t1=time.time(),
                          error=type(r).__name__)
    checked = counts["checked"]

    # ONE FLAT LIST for the panel, so it can be sorted and scrolled like any
    # other table. Keeping them in per-check buckets meant the page could show
    # a count and two examples and nothing in between - a number with no way to
    # ask which files it means.
    flat = []
    for k, rows in found.items():
        for r in rows:
            flat.append(dict(r, check=k))
    flat.sort(key=lambda r: (r["check"], r.get("path") or ""))
    # ROWS ARE KEPT WHOLE. They used to be truncated to 200 per check here,
    # which was fine for the panel and wrong for everything else: fix() works
    # off rows, so pressing Put right on 1,136 records corrected the first 200
    # and reported success. The cap belongs on the way OUT to the browser, not
    # on the answer itself - see cached().
    return {"checked": checked, "at": time.time(),
            "counts": {k: len(v) for k, v in found.items()},
            "rows": found,
            "list": flat[:2000],
            "total": sum(len(v) for v in found.values())}


def _codec_same(a: str, b: str) -> bool:
    r"""x265, HEVC and h265 are one codec with three names.

    The arrs and ffprobe do not spell these the same way, and a comparison that
    does not know that reports every HEVC file in the library as drift.
    """
    fam = {
        "hevc": "hevc", "h265": "hevc", "x265": "hevc", "h.265": "hevc",
        "avc": "h264", "h264": "h264", "x264": "h264", "h.264": "h264",
        "av1": "av1", "vp9": "vp9", "mpeg2video": "mpeg2", "mpeg2": "mpeg2",
        "mpeg4": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4", "vc1": "vc1",
        "vc-1": "vc1", "wmv3": "vc1",
    }
    return fam.get(a, a) == fam.get(b, b)


async def fix(kinds: list | None = None) -> dict:
    r"""Run a correction, and never leave the panel thinking one is running.

    A wrapper only, so that a fault anywhere in the work below cannot strand
    the progress bar at "running" forever - a spinner that never stops is
    worse than an error, because it invites you to keep waiting.
    """
    if (_CACHE.get("fixing") or {}).get("running"):
        return {"fixed": 0, "refreshed": 0, "failed": 0,
                "error": "a correction is already running"}
    try:
        return await _fix(kinds)
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"arr sync fix: {type(e).__name__}: {e}", "warn")
        raise
    finally:
        if _CACHE.get("fixing"):
            _CACHE["fixing"]["running"] = False


async def _fix(kinds: list | None = None) -> dict:
    r"""Put right what can be put right, leaving the rest visible.

    Languages are set directly, because nuarr knows the answer. Size and video
    drift are not corrected by writing a number into the arr - the arr's job is
    to read its own files, so it is asked to, and it comes back with the truth
    on its own. Telling it what to think would make nuarr the authority on a
    field the arr owns.
    """
    from .arr import shared_client
    from . import arrlang

    # THE ANSWER ON SCREEN IS THE ONE TO ACT ON. This used to re-scan first,
    # which meant pressing Put right spent minutes recomputing the very list
    # you were looking at before it corrected anything - and then corrected a
    # set that no longer matched what you had read. The cached scan is used
    # when there is one; a scan only happens when there is nothing to work
    # from.
    data = _CACHE.get("data") or await scan()
    kinds = kinds or list(CHECKS)
    out = {"fixed": 0, "refreshed": 0, "failed": 0}
    failures: list = []

    total = sum(len(data["rows"].get(k) or []) for k in kinds)
    _CACHE["fixing"] = {"running": True, "done": 0, "total": total,
                        "where": "", "fixed": 0, "failed": 0}
    prog = _CACHE["fixing"]

    if "languages" in kinds and data["rows"]["languages"]:
        prog["where"] = "language records"

        def _lang_done(file_ids, ok):
            prog["done"] += len(file_ids)
            if ok:
                prog["fixed"] += len(file_ids)
                _retire("languages", set(file_ids))
            else:
                prog["failed"] += len(file_ids)

        res = await arrlang.fix(data["rows"]["languages"], on_chunk=_lang_done)
        out["fixed"] += res.get("fixed", 0)
        out["failed"] += res.get("failed", 0)
        if res.get("failed"):
            failures.append({"check": "languages", "n": res["failed"],
                             "why": "the arr would not take the language "
                                    "correction"})

    # One refresh per PARENT, not per file: a series with forty stale episodes
    # is one rescan, and forty would be forty times the work for one answer.
    # Which rows each parent covers, so a confirmed re-read can retire exactly
    # the files it accounted for and nothing else.
    parents: dict = {}
    for k in ("size", "video"):
        if k not in kinds:
            continue
        for r in data["rows"][k]:
            if r.get("parent_id"):
                parents.setdefault((r["arr"], r["parent_id"]), []).append(
                    (k, r.get("file_id")))
    if parents:
        prog["where"] = "asking the arrs to re-read"
    for (arr_name, pid), members in parents.items():
        n = len(members)
        cfg = next((a for a in (SETTINGS.arrs or [])
                    if a.name == arr_name), None)
        if not cfg:
            continue
        try:
            await shared_client(cfg).notify_file_changed(int(pid))
            out["refreshed"] += n
            prog["fixed"] += n
            for k in ("size", "video"):
                _retire(k, {fid for kk, fid in members if kk == k})
        except Exception as e:                               # noqa: BLE001
            out["failed"] += n
            prog["failed"] += n
            failures.append({"check": "rescan", "n": n, "arr": arr_name,
                             "parent_id": pid,
                             "why": f"{arr_name} refused the re-read request: "
                                    f"{type(e).__name__}"})
        prog["done"] += n
        prog["where"] = f"{arr_name} — re-reading"

    # THE FAILURES ARE THE POINT, not the successes. Whatever was corrected is
    # gone from the next scan and needs no memory; what could not be corrected
    # is the only part a person has to act on, so it is what is kept.
    _CACHE["failures"] = failures[:200]
    _CACHE["fixed_at"] = time.time()
    _CACHE["last_fix"] = dict(out)
    if _CACHE.get("fixing"):
        _CACHE["fixing"]["running"] = False
    if out["fixed"] or out["refreshed"]:
        joblog.log(f"arr sync: corrected {out['fixed']} language record(s) and "
                   f"asked the arrs to re-read {out['refreshed']} file(s)",
                   "ok")
    if out["failed"]:
        joblog.log(f"arr sync: {out['failed']} record(s) could not be put "
                   f"right - these need a look, they will not clear on their "
                   f"own", "warn")
    return out


def _rate(arrs: list) -> float:
    r"""Combined files per second, summed from the arrs actually running.

    Built from the per-arr rows rather than a second set of counters. Two
    concurrent walks with one shared pair of totals produced a headline that
    could not be reconciled with the bars beneath it, and when two numbers on
    one screen disagree the screen is what stops being trusted.
    """
    return round(sum(a["rate"] for a in arrs), 1)


def _eta(arrs: list) -> float:
    r"""Seconds until the LAST arr finishes - they run at the same time.

    The slowest one decides, not the sum: adding the two estimates together
    would describe a sequence that is not happening.
    """
    live = [a["eta_s"] for a in arrs if a["running"] and a["eta_s"]]
    return max(live) if live else 0.0


def mode() -> str:
    """"manual" or "auto". Anything unrecognised is manual - never auto."""
    m = str(getattr(SETTINGS, "arrsync_mode", "manual") or "manual").lower()
    return "auto" if m == "auto" else "manual"


def attention() -> dict | None:
    r"""What the Attention tile should say about this, or nothing at all.

    THE TILE HAS TO EARN ITS NUMBER. It is read as "things that need you", so
    every entry has to be something a person can actually do something about,
    or the number is noise and the tile stops being looked at.

    That splits three ways, and the mode only matters for one of them:

      - Anything nuarr TRIED to correct and could not. A human is needed here
        whatever the mode, because nothing left running will clear it. Always
        raised, and raised first - it is the more serious of the two.
      - Disagreements in MANUAL mode. Real work, waiting on a click. Raised,
        because a list nobody is told about is a list nobody reads.
      - Disagreements in AUTO mode. Not raised. They are already being dealt
        with on the next sweep, and being told about work that is already in
        hand is exactly how a tile stops being believed - the same mistake as
        counting findings that had already been fixed.
    """
    fails = _CACHE.get("failures") or []
    n_fail = sum(int(f.get("n") or 0) for f in fails)
    if n_fail:
        return {"what": "arr disagreement", "n": n_fail,
                "note": "could not be put right - needs a look",
                "goto": "/settings#arrsync"}
    d = _CACHE.get("data") or {}
    n = int(d.get("total") or 0)
    if n and mode() == "manual":
        return {"what": "arr disagreement", "n": n,
                "note": "waiting on Put right", "goto": "/settings#arrsync"}
    return None


async def watch() -> None:
    r"""Re-ask the question on a schedule, and in auto mode act on the answer.

    THE CHECK EXISTED BUT NOTHING RAN IT. It only happened when someone opened
    the page and pressed a button, which makes it a thing you have to remember
    to do - and the whole argument for this check is that drift is silent and
    nobody remembers to look. A question worth asking is worth asking on a
    timer.
    """
    import asyncio

    from . import schedules
    schedules.register(
        "arrsync", "Arr agreement check", "Arrs", EVERY_S,
        what="Compares every arr record against the file nuarr probed, and "
             "reports where they have drifted apart. In auto mode it also "
             "corrects what it finds; in manual mode it waits for Put right.")
    # Not on boot. A start-up already has a scan, the arr clients and the
    # probe cache to get through, and this walks every title in the library.
    await asyncio.sleep(600)
    while True:
        try:
            every = max(1.0, float(getattr(SETTINGS, "arrsync_every_h", 12)))
            await refresh()
            d = _CACHE.get("data") or {}
            n = int(d.get("total") or 0)
            schedules.beat("arrsync",
                           f"{n} record(s) disagree" if n else "all agreed")
            if n and mode() == "auto":
                joblog.log(f"arr sync: {n} record(s) disagree with the file on "
                           f"disk - correcting them (auto mode)", "info")
                await fix()
                # Re-ask straight after, so the panel and the tile show what
                # the correction actually achieved rather than what it found
                # before it ran.
                await refresh()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"arr agreement check: {type(e).__name__}: {e}", "warn")
            every = 12.0
        await asyncio.sleep(every * 3600.0)


def _arr_progress() -> list:
    r"""One progress row per arr, because they now run at the same time.

    A single pair of counters could not describe two concurrent walks: the
    number moved at the sum of two rates, past a total that grew whenever
    either arr was still counting its titles, and said nothing about which one
    was holding things up. Each gets its own row, its own rate and its own
    estimate.
    """
    out = []
    for name, st in (_CACHE.get("arrs") or {}).items():
        t0, t1 = st.get("t0") or 0.0, st.get("t1") or 0.0
        el = (t1 or time.time()) - t0
        done, total = st.get("done", 0), st.get("total", 0)
        rate = round(st.get("files", 0) / el, 1) if (t0 and el > 1.0) else 0.0
        eta = 0.0
        if t0 and total and done > 10 and el > 3.0 and not t1:
            eta = round((total - done) * (el / done), 0)
        out.append({"name": name, "kind": st.get("kind", ""),
                    "done": done, "total": total, "files": st.get("files", 0),
                    "now": st.get("now", ""), "running": bool(st.get("running")),
                    "error": st.get("error", ""),
                    "rate": rate, "eta_s": eta,
                    "frac": (done / total) if total else 0.0})
    out.sort(key=lambda r: r["name"])
    return out


def cached() -> dict:
    """The last answer - and the first one is fetched on demand.

    See schedules.first_look(): an empty answer after a restart used to sit
    there reading "not checked yet" until the loop woke up.
    """
    from . import schedules as _sch
    _sch.first_look("arr agreement", refresh,
                    not (_CACHE.get("data") or _CACHE.get("running")))
    d = _CACHE.get("data")
    arrs = _arr_progress()
    # The aggregate is still reported, because it is what a single headline
    # number should say, but it is now the SUM of the per-arr rows rather than
    # its own set of counters that could drift from them.
    done = sum(a["done"] for a in arrs) or _CACHE.get("done", 0)
    total = sum(a["total"] for a in arrs) or _CACHE.get("total", 0)
    return {"have": bool(_CACHE.get("at")), "running": bool(_CACHE.get("running")),
            "age_s": (round(time.time() - _CACHE["at"], 1)
                      if _CACHE.get("at") else None),
            "checks": CHECKS, "mode": mode(),
            "every_h": int(getattr(SETTINGS, "arrsync_every_h", 12) or 12),
            "failures": _CACHE.get("failures") or [],
            "last_fix": _CACHE.get("last_fix"),
            "fixing": _CACHE.get("fixing"),
            "progress": {"done": done, "total": total,
                         "arrs": arrs,
                         "where": _CACHE.get("where", ""),
                         "now": _CACHE.get("now", ""),
                         "files": sum(a["files"] for a in arrs),
                         # A RATE ANSWERS THE QUESTION THE BAR RAISES. "How
                         # long is this going to take" is what a person wants
                         # from a progress bar, and a percentage alone cannot
                         # say - measured over the run rather than guessed.
                         "rate": _rate(arrs),
                         "eta_s": _eta(arrs),
                         # Total grows as each arr is reached, so a fraction
                         # would jump backwards. Reported only once both arrs
                         # have been counted, and as a plain count until then.
                         "frac": (done / total) if total else 0.0},
            # THE CAP LIVES HERE, on the way to the browser, not on the answer
            # itself. The panel reads 'list'; sending 1,136 full rows a second
            # time under 'rows' is a payload nobody asked for.
            **({**d, "rows": {k: v[:200]
                              for k, v in (d.get("rows") or {}).items()}}
               if d else {"checked": 0, "counts": {}, "rows": {}, "list": [],
                          "total": 0})}


async def refresh() -> dict:
    """Run a scan and keep it. One at a time."""
    if _CACHE.get("running"):
        return cached()
    _CACHE.update(running=True, done=0, total=0, where="", now="", files=0,
                  t0=time.time(), t1=0.0)
    try:
        d = await scan()
        _CACHE.update(data=d, at=time.time())
        # A CLEAN SCAN RETIRES THE FAILURES. They are a record of records that
        # would not take a correction; if a later scan finds nothing to
        # correct, whatever they described has resolved - by hand, by the arr
        # re-reading on its own, or because the file was replaced. Keeping
        # them would leave the Attention tile holding a number for work that
        # no longer exists, which is the failure mode this whole tile has
        # already been fixed for once.
        if not d.get("total"):
            _CACHE["failures"] = []
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"arr sync check: {type(e).__name__}: {e}", "warn")
    finally:
        _CACHE["running"] = False
        _CACHE["t1"] = time.time()          # freeze the rate at what it was
    return cached()
