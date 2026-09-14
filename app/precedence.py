r"""
nuarr - the order the systems take a file in, and why it is that order

WHY
---
Seven systems feed the one queue, and each decides on its own what to do
to a file. One live job per file keeps them from touching a file at the
same moment; nothing kept them from touching it in the wrong ORDER, and
the wrong order is how the same file gets rewritten three times:

  * the transcode keeps and drops audio by its language tags, and a tag
    that is wrong - Children of the Sea, a second Japanese track wearing
    "eng" - gives a wrong plan, so the listener finds it afterwards and
    the file is replaced and done again: nine minutes of card time gone;
  * subtitle OCR reads a picture track for a minute, then the transcode
    drops that track as a language nobody reads;
  * a subtitle fix is planned against the tracks the file has, the
    transcode moves them, and the fix fails with "the tracks have moved
    since this was planned" and is planned again.

Erik: "make sure they are done in the right order so they don't fight
each other and create more work".

THE ORDER
---------
Facts first, then in-place edits, then the one big rewrite, then the
rewrites that should see its result:

  decode     does the file decode at all - if not it is replaced, and
             every other minute spent on it was wasted
  listen     which language each track really is, so the plan keeps the
             right ones (a tag that is wrong is worse than one missing)
  audio      writes the corrected tags - in place, instant
  subread    which subtitle tracks are what, so the plan can flag and
             keep correctly
  transcode  the standardising rewrite: unwanted tracks dropped, audio
             converted, Dolby Vision stripped
  sub_ocr    reads the picture subtitles that SURVIVED - fewer of them,
             and never one that was about to be deleted
  subs       sidecars in, duplicates out, titles fixed - on the final
             container, so its instruction is not invalidated by a
             rewrite that comes later

HOW IT IS KEPT
--------------
At the one place every feeder passes through (jobs.enqueue / enqueue_many):
a job of kind K is not queued for a file while an earlier kind still has
work owed on that file. "Owed" is asked of each system's own record - the
integrity verdict, the audio tags and verdicts, the audio and subtitle
queues, the file's own state - so the feeders need no new bookkeeping.
The held file stays where it was (eligible, in sub_queue, in the sweep's
backlog) and is offered again next pass; nothing is lost, only deferred.

AND NOTHING CAN STARVE. A prerequisite whose pool is paused is not owed.
A file that has waited longer than MAX_WAIT_S on the same prerequisite is
let through and the log says so - a system that is stuck must not stop
every other system behind it. A person's own click (source manual / ui)
is never held: an explicit instruction outranks tidiness.
"""
from __future__ import annotations

import time

from .db import cursor

ORDER = ("decode", "listen", "audio", "subread", "transcode", "sub_ocr", "subs")
RANK = {k: i for i, k in enumerate(ORDER)}
# WHAT EACH KIND ACTUALLY WAITS FOR - not everything before it in ORDER,
# only the things whose absence makes its work wrong or wasted. A title fix
# is a header write; making it wait for a decode check that has not reached
# a file done months ago would hold the cheap systems behind the slow one
# for no saving at all. The expensive rewrites are where the order pays:
#   transcode  the plan is built from all four facts
#   sub_ocr    a minute per track - not on a file that will be replaced,
#              not on a track the transcode is about to drop
#   subs       planned against the container the transcode leaves behind
PREREQS = {"transcode": ("decode", "listen", "audio", "subread"),
           "sub_ocr": ("decode", "transcode"),
           "subs": ("transcode",)}
POOL_OF_KIND = {"decode": "decode", "listen": "listen", "audio": "audio",
                "subread": "subread", "transcode": "encode",
                "sub_ocr": "subocr", "subs": "subs"}
MAX_WAIT_S = 6 * 3600
UNHELD_SOURCES = ("manual", "ui")

# PUT THE THING THAT UNBLOCKS IT FORWARD.
#
# Holding a file is only half an answer. The other half is that the work it
# is waiting FOR should jump: a transcode waiting on a decode check is a
# decode check worth doing before the fifteen thousand that nobody is waiting
# on. So when a file is held, its prerequisite job is promoted to the front
# of that pool's queue - and where no job exists yet, the system that owns
# the answer is asked to take that file next.
#
# Lower priority number runs first (see jobs.enqueue_many). 10 is ahead of
# everything the feeders queue (50-95) and behind a person's own click.
PROMOTE_TO = 10
_PROMOTED_AT: dict = {}          # (file_id, prereq) -> when it was pushed
PROMOTE_EVERY_S = 120.0
# job_id -> what it is unblocking, for the queue panel.
UNBLOCKS: dict = {}
UNBLOCKS_MAX = 400

# WHICH FILES EACH SYSTEM IS BEING WAITED ON FOR - kind -> {file_id}.
#
# promote() lifts a job that EXISTS. The case it could not touch is the one
# that was actually happening: no job of that kind for that file anywhere, and
# a feeder choosing its next hundred files by its own rules, which do not
# include "somebody is waiting on this one". Measured on this box: fifteen
# files held on the listener, forty-seven listen jobs queued, and not one of
# the forty-seven was for a file anything was waiting on.
#
# So each feeder reads this and puts these files first. Rebuilt from the live
# held list on every breakdown pass, so a file that has since been answered
# stops being preferred without anybody having to remember to remove it.
WANTED: dict = {}


def wanted(kind: str) -> set:
    """The files something is waiting on this system for. Never raises."""
    try:
        return set(WANTED.get(kind) or ())
    except Exception:                                        # noqa: BLE001
        return set()


def wanted_first(kind: str, rows: list, key=None) -> list:
    """Stable-sort a feeder's candidate list so the blocking files lead."""
    w = wanted(kind)
    if not w or not rows:
        return rows
    if key is None:
        def key(r):
            return int((r or {}).get("file_id") or 0)
    try:
        return sorted(rows, key=lambda r: 0 if int(key(r)) in w else 1)
    except Exception:                                        # noqa: BLE001
        return rows

# (file_id, kind) -> when it was first deferred, for the starvation cap.
_FIRST: dict = {}
# What the last pass held and why, for the page and the log.
STATS: dict = {"held": 0, "let_through": 0, "by": {}, "at": 0.0}


def _paused() -> set:
    try:
        from . import workers
        return set(workers.paused())
    except Exception:                                        # noqa: BLE001
        return set()


# ------------------------------------------------------------ the oracles --
def _decode_owed(cur, f: dict) -> bool:
    """No fresh integrity verdict for this file at its current size."""
    if not f.get("size") or not f.get("path"):
        return False
    r = cur.execute("SELECT verdict FROM integrity WHERE file_id=? AND size=?",
                    (f["id"], f["size"])).fetchone()
    return not (r and (r["verdict"] or ""))


def _listen_owed(cur, f: dict) -> bool:
    """A track with no tag and no verdict, or tagged tracks nobody has heard."""
    langs = (f.get("audio_langs") or "")
    if not langs:
        return False
    codes = [c.strip() for c in langs.split(",")]
    rows = {int(r["track"]): (int(r["size"] or 0))
            for r in cur.execute("SELECT track, size FROM audio_lang "
                                 " WHERE file_id=?", (f["id"],))}
    size = int(f.get("size") or 0)
    # untagged tracks with no current verdict
    for i, c in enumerate(codes):
        if c == "-" and rows.get(i) != size:
            return True
    # tagged tracks nobody has ever checked - the wrong-label case
    if any(c and c != "-" for c in codes) and not rows:
        return True
    return False


def _audio_owed(cur, f: dict) -> bool:
    r = cur.execute("SELECT 1 FROM aud_queue WHERE file_id=? AND state='queued'",
                    (f["id"],)).fetchone()
    return bool(r)


def _subread_owed(cur, f: dict) -> bool:
    """A file with no subtitle track and nobody has looked for burned-in
    words yet; or a flagged track whose title has not been read."""
    if not (f.get("sub_langs") or "") and float(f.get("duration") or 0) > 120:
        r = cur.execute("SELECT 1 FROM hardsub WHERE file_id=? AND size=?",
                        (f["id"], f.get("size") or 0)).fetchone()
        if not r:
            return True
    try:
        from . import subtitletitle as _stt
        d = _stt._CACHE.get("data") or {}
        for row in (d.get("rows") or []):
            if int(row.get("file_id") or 0) == int(f["id"]) \
                    and row.get("unread") and row.get("mkv_id"):
                return True
    except Exception:                                        # noqa: BLE001
        pass
    return False


def _transcode_owed(cur, f: dict) -> bool:
    """The processing system has not been through this file yet."""
    return (f.get("state") or "") in ("new", "eligible", "queued", "running")


ORACLE = {"decode": _decode_owed, "listen": _listen_owed, "audio": _audio_owed,
          "subread": _subread_owed, "transcode": _transcode_owed}


def _file(cur, file_id: int) -> dict | None:
    r = cur.execute("SELECT id, path, state, size, duration, audio_langs, "
                    "       sub_langs FROM files WHERE id=?",
                    (int(file_id),)).fetchone()
    return dict(r) if r else None


def owed_before(file_id: int, kind: str) -> str:
    """The earliest prerequisite still owed on this file, or ''."""
    need = PREREQS.get(kind) or ()
    if not need:
        return ""
    paused = _paused()
    with cursor() as cur:
        f = _file(cur, file_id)
        if not f:
            return ""
        for k in need:
            if POOL_OF_KIND.get(k, "") in paused:
                continue                       # a paused system is not owed
            fn = ORACLE.get(k)
            if fn is None:
                continue
            try:
                if fn(cur, f):
                    return k
            except Exception:                                # noqa: BLE001
                continue
    return ""


def ready(file_id: int, kind: str, source: str = "") -> tuple[bool, str]:
    """(may queue now, what it is waiting on). Never holds a person's click,
    never holds forever."""
    if source in UNHELD_SOURCES or kind not in PREREQS:
        return True, ""
    k = owed_before(file_id, kind)
    key = (int(file_id), kind)
    if not k:
        _FIRST.pop(key, None)
        return True, ""
    first = _FIRST.setdefault(key, time.time())
    promote(int(file_id), k, kind)
    if time.time() - first > MAX_WAIT_S:
        STATS["let_through"] = STATS.get("let_through", 0) + 1
        try:
            from . import joblog
            joblog.log(f"[order] {kind} let through after "
                       f"{(time.time() - first) / 3600:.1f}h waiting on "
                       f"{k} - file {file_id}", "warn")
        except Exception:                                    # noqa: BLE001
            pass
        _FIRST.pop(key, None)
        return True, ""
    return False, k


def filter_rows(rows: list, kind: str, source: str = "") -> tuple[list, dict]:
    """The rows that may be queued now, and a count of what was held by what."""
    if source in UNHELD_SOURCES or kind not in PREREQS or not rows:
        return list(rows), {}
    keep, held = [], {}
    for r in rows:
        fid = r.get("file_id")
        if not fid:
            keep.append(r)
            continue
        ok, on = ready(int(fid), kind, source)
        if ok:
            keep.append(r)
        else:
            held[on] = held.get(on, 0) + 1
    if held:
        STATS.update(held=sum(held.values()), by=dict(held), at=time.time())
    return keep, held


def describe(held: dict) -> str:
    if not held:
        return ""
    return "held for order: " + ", ".join(
        f"{n} behind {k}" for k, n in sorted(held.items(), key=lambda kv: -kv[1]))

# --------------------------------------- why the processing system is idle --
#
# "Nothing running" has five different causes and the panel said none of them.
# This answers the one question worth asking when the queue is not moving:
# how many files COULD be processed, and what each of them is waiting for.
# Cached, and only computed when somebody asks - the panel asks while the
# encode and passthrough pools are empty, which is exactly when nobody is
# paying for it.
_BREAK: dict = {"at": 0.0, "data": {}}
_BREAK_TTL = 20.0


def eligible_breakdown(force: bool = False) -> dict:
    """{total, ready, by: {prereq: n}, oldest} over the files the processing
    system could take next."""
    now = time.time()
    if not force and _BREAK["data"] and now - _BREAK["at"] < _BREAK_TTL:
        return _BREAK["data"]
    out = {"total": 0, "ready": 0, "by": {}, "queued": 0, "at": now}
    held: list = []
    try:
        paused = _paused()
        with cursor() as cur:
            # THE SAME POPULATION THE FEEDER DRAWS FROM - eligible, and known
            # to an arr (autoqueue's own filter), minus anything already on
            # the queue.
            rows = [dict(r) for r in cur.execute(
                "SELECT f.id, f.path, f.state, f.size, f.duration, "
                "       f.audio_langs, f.sub_langs "
                "  FROM files f "
                " WHERE f.state='eligible' AND f.arr_file_id IS NOT NULL "
                "   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=f.id "
                "                    AND j.state IN ('queued','running')) "
                " ORDER BY f.id LIMIT 4000")]
            out["total"] = len(rows)
            for f in rows:
                on = ""
                for k in PREREQS["transcode"]:
                    if POOL_OF_KIND.get(k, "") in paused:
                        continue
                    fn = ORACLE.get(k)
                    try:
                        if fn and fn(cur, f):
                            on = k
                            break
                    except Exception:                        # noqa: BLE001
                        continue
                if on:
                    out["by"][on] = out["by"].get(on, 0) + 1
                    held.append((int(f["id"]), on))
                else:
                    out["ready"] += 1
            out["queued"] = int(cur.execute(
                "SELECT COUNT(*) n FROM jobs WHERE state='queued' "
                "  AND pool IN ('encode','passthrough')").fetchone()["n"] or 0)
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:120]
    # AND WHILE WE ARE HERE, PUT THE WORK THEY ARE WAITING ON FORWARD.
    #
    # The other caller of promote() is the enqueue path, which only fires when
    # a feeder happens to offer a held file - and a feeder that has nothing to
    # offer (every eligible file already carries a job of another kind) never
    # fires it at all. This runs whenever the processing system is idle and
    # something is waiting, which is exactly when the unblocking matters.
    # promote() throttles itself per file, so the repetition costs nothing.
    # AND THE AUTHORITATIVE LIST OF WHO IS WAITING ON WHAT. Rebuilt rather
    # than added to: this is the complete held set as of this pass, so a file
    # that has been answered since the last one drops out by itself.
    fresh: dict = {}
    for fid, on in held:
        fresh.setdefault(on, set()).add(int(fid))
    WANTED.clear()
    WANTED.update(fresh)
    for fid, on in held[:200]:
        promote(fid, on, "the processing system")
    _BREAK.update(at=now, data=out)
    return out


# What each prerequisite is called on a page, and where its own system lives.
WHERE = {"decode": ("the decode check", "#health"),
         "listen": ("audio listening", "#alang"),
         "audio": ("audio tag fixes", "#alang"),
         "subread": ("subtitle reads", "#lang")}


def promote(file_id: int, prereq: str, blocking: str = "") -> None:
    """Push the work this file is waiting on to the front of its own queue.

    Two halves, because the answer may or may not already be queued:
      * a QUEUED job of that kind for this file has its priority lifted to
        PROMOTE_TO, so the next free worker in that pool takes it;
      * where nothing is queued, the system that owns the answer is asked to
        take this file next - the listener has its own jump queue, the audio
        queue has a priority column, and the decode and subtitle feeders
        already order eligible files first.

    Throttled per file and prerequisite: this is called from the enqueue
    path, which a feeder may hit hundreds of times a minute.
    """
    now = time.time()
    key = (int(file_id), prereq)
    if now - _PROMOTED_AT.get(key, 0.0) < PROMOTE_EVERY_S:
        return
    _PROMOTED_AT[key] = now
    lifted = 0
    try:
        with cursor() as cur:
            rows = cur.execute(
                "SELECT job_id, priority FROM jobs "
                " WHERE file_id=? AND kind=? AND state='queued'",
                (int(file_id), prereq)).fetchall()
            for r in rows:
                if int(r["priority"] or 100) > PROMOTE_TO:
                    cur.execute("UPDATE jobs SET priority=? WHERE job_id=?",
                                (PROMOTE_TO, r["job_id"]))
                    lifted += 1
                UNBLOCKS[r["job_id"]] = blocking or "the next step"
            if len(UNBLOCKS) > UNBLOCKS_MAX:
                for jid in list(UNBLOCKS)[:len(UNBLOCKS) - UNBLOCKS_MAX]:
                    UNBLOCKS.pop(jid, None)
            if prereq == "audio":
                # Its own queue has a priority column, read by pending().
                cur.execute("UPDATE aud_queue SET priority=? "
                            " WHERE file_id=? AND state='queued'",
                            (PROMOTE_TO, int(file_id)))
    except Exception:                                        # noqa: BLE001
        pass
    if lifted:
        try:
            from . import joblog
            joblog.log(f"[order] moved a {prereq} job to the front - "
                       f"{blocking or 'work'} on file {file_id} is waiting "
                       f"on it", "debug")
        except Exception:                                    # noqa: BLE001
            pass
        return
    # NOTHING QUEUED YET: ask the system that owns the answer for this file.
    #
    # AND SAY THAT SOMETHING IS BLOCKED ON IT, not merely that it is wanted.
    # The listener's jump queue took the request and then dropped it on the
    # way out: queued() only handed over files in state 'done', and a file
    # waiting to be transcoded is 'eligible' by definition - so the one queue
    # built for exactly this could never serve it. The flag rides with the
    # request now and the feeder reads it.
    WANTED.setdefault(prereq, set()).add(int(file_id))
    try:
        if prereq == "listen":
            from . import audiolang
            audiolang.queue_check(int(file_id), blocking=True)
    except Exception:                                        # noqa: BLE001
        pass
