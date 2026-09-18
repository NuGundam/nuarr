r"""nuarr - the subtitle queue: one row per file that wants something.

WHAT THIS IS. subscan says what is true, subplan says what should happen, and
this holds the answer, runs it, and remembers what you taught it on the way.
It is the same shape as the transcode queue deliberately - a row per file, a
state, a plan in the row, workers that take the oldest first and a panel that
shows what is running - because that shape is already understood here and a
second vocabulary for the same idea is how a page becomes hard to read.

WHY THE INSTRUCTION LIVES IN THE ROW. A queue holding only file ids would have
to re-plan at dispatch, and then the thing you looked at on the page and the
thing that ran would be two different decisions separated by however long the
row waited. The steps are written down when the row is made, stamped with the
rules revision that produced them, and that is what runs.

AND WHY THE STAMP MATTERS. Move a switch in the Subtitle rules panel and
subplan.revision() changes. Every row carrying the old stamp is re-planned -
from the facts table, without touching a disk - and the queue reshapes itself:
files that no longer need anything leave, files that now do arrive, files
whose instruction changed get the new one. That is the whole of "when changes
happen it should update the central queue and requeue files that need work".

ONE REWRITE PER FILE. take, dropdup and dropempty are a single mkvmerge
command: the sidecars come in as new inputs and the unwanted tracks are named
in one --subtitle-tracks exclusion. That is the actual merge of the sidecar
and duplicate systems. It used to be two full container copies of the same
file, one after the other, each verified and committed separately - for work
that fits in one pass. retitle is mkvpropedit and rewrites nothing; mark and
recycle do not touch the video either.

WHAT IT WILL NOT DO. It will not run a step whose rule is off. It will not
guess between two tracks it cannot tell apart - that becomes a question. It
will not act on a file it has not read since the file last changed. And it
never removes a track without reading the rebuilt file back first and finding
everything it meant to keep still in it.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from . import joblog
from .db import cursor

KEY = "subqueue"
TITLE = "Subtitles"
SYSTEM = "Subtitles"

# States a row can be in.
QUEUED, RUNNING, ASK, DONE, FAILED, HELD = ("queued", "running", "ask",
                                            "done", "failed", "held")

# A failed row is not retried forever; after this many tries it sits in the
# failures list waiting for a person, exactly like the sidecar sweep learned
# to do with a subtitle file mkvmerge will never accept.
MAX_TRIES = 3
# How long a finished row stays visible before it is cleared out.
DONE_KEEP_S = 24 * 3600.0

_READY = False
_CTX: dict = {"at": 0.0, "data": None}
_CTX_TTL = 60.0

STATE: dict = {"replanning": False, "at": 0.0, "took": 0.0, "made": 0,
               "gone": 0, "changed": 0, "rev": "", "err": "",
               # How many rows the last pass handed to the main job queue, and
               # how many subtitle jobs are live on it right now.
               "fed": 0, "on_queue": 0}


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sub_queue(
                file_id     INTEGER PRIMARY KEY,
                state       TEXT    NOT NULL DEFAULT 'queued',
                steps       TEXT    NOT NULL DEFAULT '[]',
                asks        TEXT    NOT NULL DEFAULT '[]',
                n           INTEGER NOT NULL DEFAULT 0,
                why         TEXT    NOT NULL DEFAULT '',
                rev         TEXT    NOT NULL DEFAULT '',
                rewrite     INTEGER NOT NULL DEFAULT 0,
                priority    INTEGER NOT NULL DEFAULT 100,
                path        TEXT    NOT NULL DEFAULT '',
                name        TEXT    NOT NULL DEFAULT '',
                library     TEXT    NOT NULL DEFAULT '',
                disk        TEXT    NOT NULL DEFAULT '',
                queued_at   REAL    NOT NULL DEFAULT 0,
                started_at  REAL    NOT NULL DEFAULT 0,
                finished_at REAL    NOT NULL DEFAULT 0,
                took        REAL    NOT NULL DEFAULT 0,
                tries       INTEGER NOT NULL DEFAULT 0,
                err         TEXT    NOT NULL DEFAULT '',
                result      TEXT    NOT NULL DEFAULT '{}'
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_sub_queue_state "
                    "ON sub_queue(state, priority, queued_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_sub_queue_fin "
                    "ON sub_queue(finished_at DESC)")
        # A ROW MARKED RUNNING WITH NOBODY RUNNING IT IS A ROW FROM BEFORE THE
        # RESTART. The worker sets that state and the process that was going
        # to clear it is gone, so without this the file is stuck: replan will
        # not touch a running row, and no worker will ever pick it up again
        # because pending() only looks at queued ones. Put it back rather than
        # fail it - nothing was necessarily wrong with the file, the machine
        # simply stopped.
        try:
            cur.execute(
                "UPDATE sub_queue SET state='queued', started_at=0 "
                " WHERE state='running' AND file_id NOT IN "
                "   (SELECT file_id FROM jobs WHERE kind='subs' "
                "     AND state='running' AND file_id IS NOT NULL)")
        except Exception:                                        # noqa: BLE001
            pass
    _READY = True


def _ctx(force: bool = False) -> dict:
    """The planner's context, held for a minute - it costs a second to build."""
    from . import subplan
    now = time.time()
    if force or _CTX["data"] is None or now - _CTX["at"] > _CTX_TTL:
        _CTX.update(at=now, data=subplan.context())
    return _CTX["data"]


def bump() -> None:
    """The rules moved. Forget the context so the next pass reads them."""
    _CTX.update(at=0.0)


# ------------------------------------------------------------- the replan --
def replan(limit: int = 100000, force: bool = False) -> dict:
    r"""Bring the queue into line with the facts and the rules. Touches no disk.

    WHAT IT DOES TO EACH FILE. Plans it; then:
      nothing to do        -> the row goes (unless it is running, or done and
                              still inside its keep window)
      same as the row      -> left exactly alone, so a queue position and a
                              retry count survive a re-plan
      different, or new    -> written, and put back in the queue

    A ROW THAT IS RUNNING IS NEVER TOUCHED. It has a process attached to it
    and a temp file on the cache; re-planning it underneath would leave the
    worker committing an instruction that no longer exists.
    """
    from . import subplan, subscan
    if STATE["replanning"]:
        return {"ok": False, "why": "already re-planning"}
    STATE.update(replanning=True, err="", made=0, gone=0, changed=0)
    t0 = time.time()
    try:
        init()
        ctx = _ctx(force)
        rev = ctx["rev"]
        rows = subscan.interesting(limit)
        with cursor() as cur:
            have = {int(r["file_id"]): dict(r) for r in cur.execute(
                "SELECT file_id, state, steps, asks, rev, tries "
                "  FROM sub_queue")}
        seen = set()
        for f in rows:
            fid = int(f["file_id"])
            seen.add(fid)
            old = have.get(fid)
            if old and old["state"] == RUNNING:
                continue
            p = subplan.plan(f, ctx)
            want = bool(p["steps"] or p["asks"])
            if not want:
                if old and old["state"] not in (DONE, RUNNING):
                    _delete(fid)
                    STATE["gone"] += 1
                continue
            same = (old is not None
                    and old["rev"] == rev
                    and old["steps"] == json.dumps(p["steps"])
                    and old["asks"] == json.dumps(p["asks"]))
            if same and old["state"] in (QUEUED, ASK, FAILED, HELD):
                continue
            if same and old["state"] == DONE:
                # It was done under these very rules and the file has not
                # changed since; re-queueing it would redo work that already
                # happened. The facts row is what says the file changed.
                continue
            _put(p, rev, old)
            if old is None:
                STATE["made"] += 1
            else:
                STATE["changed"] += 1
        # Rows whose file no longer has any facts at all - deleted, or moved
        # out of an interesting shape by somebody else's work.
        for fid, old in have.items():
            if fid in seen or old["state"] in (RUNNING, DONE):
                continue
            _delete(fid)
            STATE["gone"] += 1
        _sweep_done()
        STATE.update(rev=rev, at=time.time())
        return {"ok": True, "rev": rev, "made": STATE["made"],
                "changed": STATE["changed"], "gone": STATE["gone"],
                "took": round(time.time() - t0, 2)}
    except Exception as e:                                       # noqa: BLE001
        STATE["err"] = f"{type(e).__name__}: {e}"[:200]
        return {"ok": False, "why": STATE["err"]}
    finally:
        STATE.update(replanning=False, took=time.time() - t0)


def _put(p: dict, rev: str, old: dict | None) -> None:
    """Write one instruction into the queue, keeping the retry count."""
    state = ASK if (p["asks"] and not p["steps"]) else QUEUED
    with cursor() as cur:
        cur.execute(
            "INSERT INTO sub_queue(file_id,state,steps,asks,n,why,rev,rewrite,"
            "  priority,path,name,library,disk,queued_at,tries) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(file_id) DO UPDATE SET state=excluded.state,"
            "  steps=excluded.steps, asks=excluded.asks, n=excluded.n,"
            "  why=excluded.why, rev=excluded.rev, rewrite=excluded.rewrite,"
            "  path=excluded.path, name=excluded.name,"
            "  library=excluded.library, disk=excluded.disk,"
            "  queued_at=excluded.queued_at, err='', finished_at=0",
            (p["file_id"], state, json.dumps(p["steps"]),
             json.dumps(p["asks"]), p["n"], p["why"], rev,
             1 if p["rewrite"] else 0,
             # A file with a question on it waits behind files that do not,
             # and a file that only needs a recycle goes first because it
             # costs nothing.
             (60 if not p["rewrite"] else 100),
             p["path"], p["name"], p["library"], p["disk"], time.time()))


def _delete(file_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM sub_queue WHERE file_id=?", (int(file_id),))


def forget(file_id: int, why: str = "") -> None:
    r"""Drop the instruction for a file that is no longer there.

    Called by the job runner when it finds the source gone. The row is an
    instruction ABOUT a file; with the file replaced by an arr upgrade the
    instruction describes tracks that no longer exist, and the reading writes
    a fresh row for the new file when it is scanned. Keeping the old one only
    guarantees the same dead job every top-up - which is exactly what was
    happening: 18,667 subtitle jobs skipped over two days for 154 files.
    """
    _delete(file_id)
    joblog.log(f"subtitles: forgot the instruction for file {file_id}"
               f"{' - ' + why if why else ''}", "warn")


def _sweep_done() -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM sub_queue WHERE state=? AND finished_at < ?",
                    (DONE, time.time() - DONE_KEEP_S))
        # AND THE INSTRUCTIONS WHOSE FILE IS NOT THERE ANY MORE. The hand-over
        # refuses to offer these (see _to_hand_over), which stops the dead
        # jobs but leaves the rows sitting in the queue forever, counted as
        # work waiting. 59 of them were found the day this was written. The
        # replan's own "gone" pass only covers files that still have facts.
        cur.execute("DELETE FROM sub_queue WHERE state=? AND file_id NOT IN "
                    "(SELECT id FROM files WHERE state NOT IN "
                    "('deleted','duplicate'))", (QUEUED,))


# ---------------------------------------------------------------- the work --
def pending(limit: int = 500) -> list:
    """What is waiting, cheapest first, oldest first within that."""
    init()
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT file_id, path, name, library, disk, steps, n, why, rewrite "
            "  FROM sub_queue WHERE state=? "
            " ORDER BY priority, queued_at LIMIT ?", (QUEUED, int(limit)))]


def _mark(file_id: int, state: str, **kw) -> None:
    sets = ["state=?"]
    args: list = [state]
    for k, v in kw.items():
        sets.append(f"{k}=?")
        args.append(v)
    args.append(int(file_id))
    with cursor() as cur:
        cur.execute(f"UPDATE sub_queue SET {', '.join(sets)} WHERE file_id=?",
                    args)


def row_for(file_id: int) -> dict:
    r"""The instruction for one file, marked as running. For the job worker.

    MARKED HERE RATHER THAN WHEN THE JOB WAS MADE. There can be minutes
    between a row being handed to the queue and a worker picking it up, and in
    that time a rule change or a rescan may have re-planned it - so the state
    that means "a worker has this" is set by the worker, and the row it reads
    is whatever the row says at that moment rather than what it said when the
    job row was written.
    """
    init()
    with cursor() as cur:
        r = cur.execute(
            "SELECT file_id, path, name, library, disk, steps, n, why, rewrite "
            "  FROM sub_queue WHERE file_id=?", (int(file_id),)).fetchone()
    if not r:
        return {}
    d = dict(r)
    try:
        steps = json.loads(d.get("steps") or "[]")
    except Exception:                                            # noqa: BLE001
        steps = []
    if not steps:
        return {}
    d["plan"] = {"steps": [_step_words(s) for s in steps],
                 "why": [s.get("why") or "" for s in steps]}
    _mark(int(file_id), RUNNING, started_at=time.time())
    return d


# "THE FILE HAS CHANGED SINCE IT WAS READ" IS NOT A FAILURE TO RETRY.
#
# do_one refuses to act when the plan's track ordinals no longer match the
# container - correctly, because acting on a stale map is how the wrong track
# gets deleted. What happened next was wrong: note_result counted it as an
# attempt and re-queued the SAME stale plan, which cannot succeed, three times,
# and then marked the row failed and left the file's subtitles unsettled for
# good with nothing scheduled to look again.
#
# Measured at the audit: 30 such failures across 10 files, three runs each,
# every run a full container read to enumerate tracks that were never going to
# match. The answer is to throw the READING away, not the file: drop the row
# and the facts it was planned from, and the reader re-reads the file on its
# next pass and plans it again from what is there now.
_STALE_MARKS = ("have moved since", "has changed since", "not where the plan")


# HOW MANY TIMES RE-READING IS ALLOWED TO BE THE ANSWER.
#
# Three, and then the file waits for a person. The re-read exists for a file
# that genuinely changed between being read and being worked on: read it again
# and the new plan fits. That is a ONE-TIME event per change. A file that
# comes back stale three times running is not a file that keeps changing - it
# is a file the reader and the container disagree about, and re-reading it a
# fourth time produces the fourth identical impossible plan.
MAX_STALE_REPLANS = 3


def _stale_init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sub_stale(
                file_id INTEGER PRIMARY KEY,
                n       INTEGER NOT NULL DEFAULT 0,
                first   REAL    NOT NULL DEFAULT 0,
                last    REAL    NOT NULL DEFAULT 0,
                why     TEXT    NOT NULL DEFAULT ''
            )""")


def stale_count(fid: int) -> int:
    try:
        _stale_init()
        with cursor() as cur:
            r = cur.execute("SELECT n FROM sub_stale WHERE file_id=?",
                            (int(fid),)).fetchone()
        return int((r["n"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def stale_clear(fid: int) -> None:
    """A pass that finished is a file that is no longer arguing with itself."""
    try:
        _stale_init()
        with cursor() as cur:
            cur.execute("DELETE FROM sub_stale WHERE file_id=?", (int(fid),))
    except Exception:                                            # noqa: BLE001
        pass


def _replan_from_scratch(fid: int, why: str) -> None:
    r"""Forget what was read about this file so the reader reads it again.

    AND COUNT IT, WHICH THE FIRST VERSION DID NOT.
    ---------------------------------------------
    This deleted the sub_queue row and the sub_facts row and returned. The
    queue row is where `tries` lives - so deleting it reset the attempt
    counter, every time, and the file could never reach MAX_TRIES. The reader
    re-read it on the next pass, produced the same plan from the same
    container, the job failed with the same sentence, and the row was deleted
    again.

    Measured on Erik's library when he reported the Activity panel looked
    wrong: 1,125 failed subs jobs carrying "the tracks have moved since this
    was planned", 265 of them in one hour, five Dark Winds episodes going
    round at about one a minute for 52 minutes - one file 342 times. Each lap
    wrote a history row, which is what filled his panel.

    The count lives in its own table precisely BECAUSE the queue row is
    deleted here; a counter kept in the thing being deleted is not a counter.
    """
    n = stale_count(fid) + 1
    now = time.time()
    try:
        _stale_init()
        with cursor() as cur:
            cur.execute(
                "INSERT INTO sub_stale(file_id,n,first,last,why) "
                "VALUES(?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET "
                " n=excluded.n, last=excluded.last, why=excluded.why",
                (int(fid), n, now, now, why[:200]))
    except Exception:                                            # noqa: BLE001
        pass

    if n > MAX_STALE_REPLANS:
        # STOP. The row stays, marked failed, with the reason - so the file is
        # visible as something to look at rather than silently spinning.
        try:
            with cursor() as cur:
                cur.execute(
                    "UPDATE sub_queue SET state=?, finished_at=?, err=? "
                    " WHERE file_id=?",
                    (FAILED, now,
                     f"re-read {n - 1} times and the plan still does not fit "
                     f"the file - {why[:160]}", int(fid)))
        except Exception:                                        # noqa: BLE001
            pass
        joblog.log(
            f"subtitles: file {fid} has been re-read {n - 1} times and the "
            f"plan still does not match the container - giving up rather than "
            f"looping. The reader and the file disagree; this one needs a "
            f"person ({why[:80]})", "warn")
        return

    try:
        with cursor() as cur:
            cur.execute("DELETE FROM sub_queue WHERE file_id=?", (fid,))
            cur.execute("DELETE FROM sub_facts WHERE file_id=?", (fid,))
        joblog.log(f"subtitles: file {fid} changed since it was read - the "
                   f"instruction is void, re-reading rather than retrying it "
                   f"(attempt {n} of {MAX_STALE_REPLANS}; {why[:60]})", "warn")
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"could not clear the stale subtitle reading for {fid}: "
                   f"{type(e).__name__}: {e}", "debug")


def note_result(file_id: int, res: dict) -> None:
    """What the worker found, written back onto the row."""
    init()
    fid = int(file_id)
    if res.get("ok"):
        _mark(fid, DONE, finished_at=time.time(), err="",
              result=json.dumps(res)[:4000])
        stale_clear(fid)
        return
    why = str(res.get("why") or "")
    if any(m in why.lower() for m in _STALE_MARKS):
        _replan_from_scratch(fid, why)
        return
    with cursor() as cur:
        cur.execute("UPDATE sub_queue SET tries=tries+1 WHERE file_id=?", (fid,))
        n = cur.execute("SELECT tries FROM sub_queue WHERE file_id=?",
                        (fid,)).fetchone()
    tries = int((n["tries"] if n else 0) or 0)
    _mark(fid, FAILED if tries >= MAX_TRIES else QUEUED,
          finished_at=time.time(), err=str(res.get("why") or "")[:400],
          result=json.dumps(res)[:4000])


# How many subtitle jobs to keep on the main queue at once. THE MANIFEST IS
# NOT THE QUEUE - there are five thousand files wanting something and putting
# all of them in the jobs table would make the queue panel a scrollbar and
# every poll of it a page fault. autoqueue reached the same conclusion about
# transcodes for the same reasons; this is the same answer with a smaller
# number, because these drain much faster.
QUEUE_DEPTH = 200


def _to_hand_over(depth: int) -> tuple:
    r"""How much room the queue has, and which rows would fill it.

    SPREAD ACROSS SPINDLES, NOT TAKEN IN ORDER.
    -------------------------------------------
    Measured here the first time this ran for real: of 5,300 files waiting,
    3,302 were on NU-DRIVE-1 - that is simply where the duplicate-heavy shows
    live. Taking the oldest two hundred therefore took two hundred files from
    NU-DRIVE-1, every one of them, and the whole queue then sat still behind
    one viewer on that disk. Eleven other spindles had between 105 and 397
    files of work on them and not one of those files was ever offered.

    The dispatcher already refuses to put two heavy jobs on one spindle, and
    that rule is right; it cannot help if every job it is given is on the same
    spindle. So the hand-over deals the rows out round-robin by disk. Two
    hundred rows then cover every disk that has work, the two workers can
    always find a spindle nobody is reading from, and a viewer stops the files
    on THEIR disk rather than stopping everything.

    It is still oldest-first WITHIN a disk, so nothing is starved and the
    order inside a show is preserved.
    """
    init()
    with cursor() as cur:
        have = int(cur.execute(
            "SELECT COUNT(*) n FROM jobs "
            " WHERE kind='subs' AND state IN ('queued','running')"
        ).fetchone()["n"] or 0)
        room = max(0, int(depth) - have)
        if not room:
            return have, []
        # Read a wider slice than needed so there is something from the
        # smaller disks to deal out. Bounded, because this is a poll.
        # AND NEVER A FILE THAT IS NOT THERE ANY MORE.
        #
        # An arr upgrade removes the row in `files` (or marks it deleted) and
        # leaves this instruction pointing at a path nothing can open. The job
        # it makes finishes 'skipped' in under two milliseconds having done
        # nothing and told nobody, and the next top-up hands the same row
        # straight back. Measured before this join existed: 59 such rows,
        # 18,667 skipped subtitle jobs over two days, 353 of them for one
        # episode of Ishura. A file that is gone has no subtitles to settle.
        # AND THE PATH THE FILE HAS NOW, NOT THE ONE IT HAD WHEN PLANNED.
        #
        # q.path is frozen at planning time. nuarr rewrites the file, the arr
        # renames it to match what changed - "[AAC 5.1]" becomes "[EAC3 5.1]"
        # - and files.path follows while the instruction does not. Handing
        # over the stale name gave the job a path nothing could open: it
        # finished 'skipped' in milliseconds, _forget_queue_row dropped the
        # row, the sweep re-planned the same file, and round it went.
        # Measured: 151 such skips in 24h across 14 files, 72 in the last
        # hour, 106 skipped against 56 done in three hours - and every one of
        # those files was sitting on disk under its new name the whole time.
        #
        # The guard below already refuses a file whose ROW has gone. That is
        # the other half of the question, and it was never the half that was
        # failing. files.path is written by the scan and by the rename
        # handler, so it is the one that knows.
        rows = [dict(r) for r in cur.execute(
            "SELECT q.file_id, COALESCE(f.path, q.path) AS path, "
            "       q.name, q.rewrite, q.steps, q.why, q.disk "
            "  FROM sub_queue q LEFT JOIN files f ON f.id = q.file_id "
            " WHERE q.state=? AND f.id IS NOT NULL "
            "   AND (f.state IS NULL OR f.state NOT IN ('deleted','duplicate')) "
            " ORDER BY q.priority, q.queued_at LIMIT ?",
            (QUEUED, min(20000, max(room * 20, 2000))))]
        # The row is now handed over correctly whatever it stores, but a
        # stored path that disagrees with the file is a lie the panel and the
        # log both repeat. Realign the ones this pass touched.
        drift = [(r["path"], r["file_id"]) for r in rows]
        if drift:
            cur.executemany(
                "UPDATE sub_queue SET path=? WHERE file_id=? AND path<>?",
                [(p, i, p) for p, i in drift])
    by_disk: dict = {}
    for r in rows:
        by_disk.setdefault(r.get("disk") or "?", []).append(r)
    # Deal one per disk, then the next, until the room is full. Disks with
    # more waiting are not favoured: a spindle with a hundred files is as
    # useful to a stalled worker as one with three thousand.
    out: list = []
    lanes = [iter(v) for _k, v in sorted(by_disk.items())]
    while lanes and len(out) < room:
        alive = []
        for it in lanes:
            if len(out) >= room:
                alive.append(it)
                continue
            try:
                out.append(next(it))
                alive.append(it)
            except StopIteration:
                pass
        lanes = alive
    return have, out


def _job_plan(r: dict) -> str:
    r"""The instruction, in the shape the queue panel reads plans in.

    THE QUEUE SAID NOTHING UNDER "Planned work" for these, which is the one
    column that answers "what is this job going to do to my file" - and for a
    job that removes subtitle tracks that is not a detail. The transcode path
    writes its plan at enqueue time and the panel has read it ever since;
    this is the same thing said in the same place.
    """
    try:
        steps = json.loads(r.get("steps") or "[]")
    except Exception:                                            # noqa: BLE001
        steps = []
    return json.dumps({
        "subs": True,
        "rewrite": bool(r.get("rewrite")),
        "summary": r.get("why") or "",
        # Same shape the transcode plan uses for its sentences, so the row's
        # hover and the job card's action list need no special case.
        "actions": [{"kind": "subtitle", "what": _step_words(s),
                     "why": s.get("why") or "", "detail": ""}
                    for s in steps],
    })


def _step_words(s: dict) -> str:
    """One step as a sentence, for the plan and the job card."""
    d = s.get("do")
    if d == "take":
        w = f"take in the {s.get('cls') or 'full'} {s.get('lang') or ''} " \
            f"subtitle sitting beside it"
        if s.get("replaces"):
            w += f", replacing {len(s['replaces'])} track(s) already inside"
        return w
    if d == "recycle":
        return f"recycle {os.path.basename(s.get('side') or '')}"
    if d == "dropdup":
        if int(s.get("ord", -1)) < 0:
            return (f"count the lines in {len(s.get('weigh') or [])} "
                    f"{s.get('cls') or ''} {s.get('lang') or ''} tracks and "
                    f"remove all but the fullest")
        return f"remove a second {s.get('cls') or ''} {s.get('lang') or ''} track"
    if d == "dropempty":
        return f"remove an empty {s.get('lang') or ''} track"
    if d == "retitle":
        return (f"correct track {int(s.get('ord') or 0)}'s title to "
                f"{s.get('to') or ''}")
    if d == "mark":
        return "add the blank marker saying the words are in the picture"
    return str(d or "")


async def topup(depth: int = QUEUE_DEPTH) -> dict:
    r"""Hand what the reading is sure about to the main queue.

    ONLY WHAT IT IS SURE ABOUT. A row with a question on it is in state 'ask'
    and is never handed over - that is what "Yours to call" means. Answering
    one re-plans it into 'queued', and then it comes through here like
    anything else, which is why there is no separate route from an answer to
    the work. One road in, and the thing you were asked about takes it like
    everything else.

    AND THE DUPLICATE GUARD IS RESPECTED RATHER THAN WORKED AROUND.
    jobs.enqueue refuses a second live job for a file and raises to say so. A
    file already being transcoded is a file whose subtitles the transcode will
    carry in, or which can be settled the moment it is done - so a refusal is
    counted and the row simply waits for the next top-up.
    """
    from . import jobs
    try:
        have, rows = await jobs.in_work(_to_hand_over, depth)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    # ONE TRANSACTION, OFF THE LOOP - see jobs.enqueue_many.
    r = await jobs.in_work(jobs.enqueue_many,
                           [{**x, "plan_json": _job_plan(x)} for x in rows],
                           "subs", 50, "subtitles")
    made, skipped = int(r.get("made") or 0), int(r.get("skipped") or 0)
    return {"ok": True, "made": made, "skipped": skipped,
            "on_queue": have + made}


# A SUBTITLE JOB IS THREE THINGS, AND THE BAR WAS DRAWING THEM AS ONE.
#
# It counted the lines in each track (mkvextract, minutes), then rewrote the
# container (mkvmerge, minutes), then read the result back and committed it -
# and every one of those reported its own 0 to 100 into the same bar. So the
# bar ran to the end, snapped back to nothing, ran to the end again, and sat
# at 100% for the whole of the longest phase. Erik: "it looks like it's been
# stuck for a while".
#
# The phases get shares of the bar instead, in the proportion they actually
# take on this library, and each one says what it is. A number that only ever
# goes forwards is the whole point of a progress bar.
PHASES = (("counting the lines", 0.0, 55.0),
          ("rewriting the container", 55.0, 92.0),
          ("reading it back and committing", 92.0, 100.0))


def _phase_report(report, lo: float, hi: float, note: str = ""):
    """A reporter that maps a tool's own 0-100 into [lo, hi] of the bar.

    Keeps the attributes the callers hang off the reporter - the ledger entry,
    the pid hook and the stage hook - so a phase is a drop-in for the original.
    """
    if report is None:
        return None

    def rep(p):
        try:
            p = max(0.0, min(100.0, float(p)))
            report(lo + (hi - lo) * p / 100.0)
        except Exception:                                        # noqa: BLE001
            pass
    for a in ("task", "on_pid", "on_stage"):
        try:
            setattr(rep, a, getattr(report, a, None))
        except Exception:                                        # noqa: BLE001
            pass
    if note:
        stage = getattr(report, "on_stage", None)
        if stage is not None:
            try:
                stage(note)
            except Exception:                                    # noqa: BLE001
                pass
    return rep


def do_one(row: dict, report=None, claim: bool = True) -> dict:
    r"""Carry out one file's whole instruction.

    THE ORDER IS NOT NEGOTIABLE. The container pass first, because everything
    after it is about tracks that pass may have moved or removed; then the
    title, which is written in place; then the marker; then the loose copies,
    which are only binned once the file that replaces them is known good.
    """
    from . import fileops, idle, subembed, subscan
    fid = int(row["file_id"])
    path = row.get("path") or ""
    try:
        steps = json.loads(row.get("steps") or "[]")
    except Exception:                                            # noqa: BLE001
        steps = []
    if not steps:
        return {"ok": True, "why": "nothing to do"}
    if not os.path.exists(path):
        return {"ok": False, "why": "the file is not on disk"}

    # A JOB IS ALREADY A LEDGER ENTRY. When this runs inside a worker the job
    # card is what says which file, which disk and how far along - claiming a
    # second entry in the background ledger would put the same file on the
    # disk panel twice and count its bytes twice with it.
    work = getattr(report, "task", None)
    mine = claim and work is None
    if mine:
        work = idle.claim(SYSTEM, os.path.basename(path),
                          now=os.path.basename(path),
                          disk=row.get("disk") or "", note="subtitles")
    out = {"ok": True, "did": [], "why": ""}
    # WHICH OF THE FOUR THINGS IT IS DOING RIGHT NOW. A card that says
    # "subtitles" for three minutes is a card that cannot tell a stalled
    # container copy from a title being written, and those differ by three
    # orders of magnitude in how long they should take.
    stage = getattr(report, "on_stage", None)

    def say(name):
        if stage is not None:
            try:
                stage(name)
            except Exception:                                    # noqa: BLE001
                pass
    try:
        takes = [s for s in steps if s["do"] == "take"]
        drops = [s for s in steps if s["do"] in ("dropdup", "dropempty")]
        if takes or drops:
            say("rebuilding the file"
                + (f" — taking in {len(takes)}" if takes else "")
                + (f", removing {len(drops)}" if drops else ""))
            r = _rewrite(fid, path, takes, drops, report, work, row)
            out["did"].append(r)
            if not r.get("ok"):
                return {"ok": False, "why": r.get("why") or "the rewrite failed",
                        "did": out["did"]}
        rt = [s for s in steps if s["do"] == "retitle"]
        if rt:
            say("correcting the title")
            out["did"].append(_retitle(fid, path, rt))
        mk = [s for s in steps if s["do"] == "mark"]
        if mk:
            say("adding the marker track")
            out["did"].append(_mark_picture(fid, mk[0]))
        rc = [s for s in steps if s["do"] == "recycle"]
        if rc:
            say(f"recycling {len(rc)} loose cop"
                + ("y" if len(rc) == 1 else "ies"))
            gone, kept = 0, []
            for s in rc:
                rr = fileops.recycle(s["side"])
                if getattr(rr, "ok", False):
                    gone += 1
                else:
                    kept.append(os.path.basename(s["side"]))
            out["did"].append({"do": "recycle", "ok": True, "gone": gone,
                               "kept": kept})
        # THE FILE IS A DIFFERENT FILE NOW, so what nuarr knows about its
        # subtitles is out of date by definition. Re-reading it here is what
        # stops the next re-plan queueing the same work again.
        say("re-reading what it carries now")
        try:
            subscan.scan_one(fid)
        except Exception:                                        # noqa: BLE001
            pass
        return out
    finally:
        if mine and work is not None:
            work.close()


def _rewrite(fid: int, path: str, takes: list, drops: list, report, work,
             row: dict) -> dict:
    r"""One mkvmerge: sidecars in, unwanted tracks out, read back, committed.

    THIS IS THE MERGE. Two systems used to do a container copy each - the
    sidecar sweep to add, the duplicate sweep to remove - so a file needing
    both was copied twice, verified twice and committed twice. Naming the
    removals in the same command costs nothing at all: mkvmerge is already
    writing every other track out.
    """
    from . import fileops, subdupe, subembed
    if os.path.splitext(path)[1].lower() != ".mkv":
        return {"do": "rewrite", "ok": False,
                "why": "only Matroska can carry these tracks"}
    if not subembed.have_mkvmerge():
        return {"do": "rewrite", "ok": False,
                "why": "mkvmerge is not installed - see Settings, MKVToolNix"}
    if fileops.is_locked(path):
        return {"do": "rewrite", "ok": False, "why": "the file is in use"}

    # THE FILE AS IT IS RIGHT NOW, not as it was when the plan was written.
    # mkvmerge numbers tracks its own way and the plan speaks in subtitle
    # ordinals, so the two have to be joined against the live container - and
    # if it has changed since, that is what this discovers.
    live = subembed._live_sub_tracks(path)
    by_ord = {t["ord"]: t for t in live}
    remove: list = []

    def gone(o):
        t = by_ord.get(int(o))
        if t is not None:
            remove.append(t)

    for s in drops:
        if s["do"] == "dropempty" or int(s.get("ord", -1)) >= 0:
            o = int(s["ord"])
            t = by_ord.get(o)
            if t is None or t["lang"] != s.get("lang") \
                    or t["class"] != s.get("cls", t["class"]):
                return {"do": "rewrite", "ok": False,
                        "why": ("the track this would have removed is not "
                                "where the plan said it was - the file has "
                                "changed since it was read")}
            gone(o)
            continue
        # A WEIGH: nobody has counted these yet, so count them now and drop
        # the losers. Extraction is the only honest way to tell two tracks
        # apart that have the same header, and it is the worker's job rather
        # than a question for a person.
        ords = [int(x) for x in (s.get("weigh") or [])]
        counted = []
        on_pid = getattr(report, "on_pid", None)
        stage = getattr(report, "on_stage", None)
        for i, o in enumerate(ords):
            t = by_ord.get(o)
            if t is None:
                return {"do": "rewrite", "ok": False,
                        "why": "the tracks have moved since this was planned"}
            # THE LONGEST PART OF THE JOB, SAID OUT LOUD. Three extractions of
            # a 5 GB container take longer than the remux that follows them,
            # and the card used to show that time under one motionless word.
            if stage is not None:
                try:
                    stage(f"counting the lines in track {i + 1} of {len(ords)}")
                except Exception:                                # noqa: BLE001
                    pass
            # THE WEIGH IS MOST OF THE JOB, SO THE BAR FOLLOWS IT. Each track
            # is an equal share of the bar, and mkvextract's own percentage
            # moves within that share - the rewrite that follows is short by
            # comparison and reports through the same hook after.
            # The counting owns the first slice of the bar, and each track
            # owns an equal share of that slice - so two tracks reach 27% and
            # 55%, not 50% and 100%. See PHASES.
            _lo, _hi = PHASES[0][1], PHASES[0][2]

            def _pct(p, _i=i, _n=len(ords)):
                if report is not None:
                    try:
                        share = (_i + max(0.0, min(100.0, p)) / 100.0) / _n
                        report(_lo + (_hi - _lo) * share)
                    except Exception:                            # noqa: BLE001
                        pass
            counted.append((subdupe._events(path, t["id"], on_pid=on_pid,
                                            on_pct=_pct), t))
        counted.sort(key=lambda ct: (-(ct[0] if ct[0] >= 0 else -1),
                                     ct[1]["ord"]))
        for n, t in counted[1:]:
            remove.append(t)
    # Sidecars replacing a track name that track by its ordinal too.
    for t in takes:
        for o in (t.get("replaces") or []):
            hit = by_ord.get(int(o))
            if hit is None:
                return {"do": "rewrite", "ok": False,
                        "why": ("the track this would have replaced is not "
                                "where the plan said it was")}
            remove.append(hit)

    # WHAT MUST SURVIVE IS SIMPLY WHAT IS NOT BEING REMOVED, and it is worked
    # out once, after every removal is settled. The first version subtracted
    # kinds from a set as it went and needed to reason about whether a
    # survivor was itself about to be removed later in the same loop - which
    # is the sort of thing that is right until the day two rules touch the
    # same track. The marker is excluded because it is not a subtitle and its
    # absence is not a loss; the languages being taken IN are checked
    # separately by the tail, against the sidecars it was handed.
    rem = {t["ord"] for t in remove}
    keep = {(t["lang"], t["class"]) for t in live
            if t["class"] != "marker" and t["ord"] not in rem}

    ok_room, why_room = fileops.cache_room(subembed._size(path))
    if not ok_room:
        return {"do": "rewrite", "ok": False, "why": why_room}
    claim = fileops.cache_reserve(subembed._size(path))
    claim.__enter__()
    tmp = fileops.cache_temp(".mkv", "subs")
    try:
        cmd = [subembed._mkvmerge(), "-o", tmp]
        if remove:
            ids = sorted({str(t["id"]) for t in remove})
            cmd += ["--subtitle-tracks", "!" + ",".join(ids)]
        cmd += [path]
        for t in takes:
            cmd += ["--language", f"0:{t['lang']}"]
            if "forced" in (t.get("role") or ""):
                cmd += ["--forced-track", "0:yes"]
            cmd += [t["side"]]
        # The sidecar sweep's own tail: run, read back, commit, re-probe,
        # recycle. Given the keeps so a pass that removes tracks is verified
        # the way subdupe verified its own.
        tk = [{"sidecar": t["side"], "lang": t["lang"],
               "role": t.get("role") or "", "replaces": t.get("replaces") or []}
              for t in takes]
        # The rewrite gets the middle slice, and says so - the card used to
        # show "counting the lines in track 2 of 2" for the whole of it.
        r = subembed._embed_tail(fid, path, tk, [], cmd, tmp,
                                 _phase_report(report, PHASES[1][1],
                                               PHASES[1][2], PHASES[1][0]),
                                 work,
                                 keep=keep, removed=len(remove),
                                 on_pid=getattr(report, "on_pid", None))
        return {"do": "rewrite", **r, "removed": len(remove),
                "took_in": len(takes)}
    finally:
        claim.__exit__(None, None, None)


def _retitle(fid: int, path: str, steps: list) -> dict:
    from . import subtitletitle as stt
    edits = [{"track": int(s["ord"]), "old": s.get("from") or "",
              "new": s.get("to") or ""} for s in steps if s.get("to")]
    if not edits:
        return {"do": "retitle", "ok": True, "n": 0}
    ok, detail = stt._fix_file(path, edits)
    if ok:
        try:
            stt._restamp(fid, path, edits)
        except Exception:                                        # noqa: BLE001
            pass
    return {"do": "retitle", "ok": bool(ok), "n": len(edits),
            "why": "" if ok else detail, "detail": detail}


def _mark_picture(fid: int, step: dict) -> dict:
    from . import hardsub
    try:
        r = hardsub.mark_one(int(fid), step.get("kind") or "")
        return {"do": "mark", "ok": bool(r.get("ok")), **r}
    except Exception as e:                                       # noqa: BLE001
        return {"do": "mark", "ok": False, "why": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------ the answers --
def asking(limit: int = 200) -> list:
    """Every file with a question on it, and what the question is."""
    init()
    out = []
    from .db import display_label
    with cursor() as cur:
        for r in cur.execute(
                # THE SAME LABEL AND DATE THE READER ROWS CARRY, so a question
                # can be drawn as one of them rather than beside them.
                "SELECT q.file_id, q.path, q.name, q.library, q.disk, q.asks, "
                "       q.steps, q.why, f.title, f.season, f.episode, "
                "       f.first_seen "
                "  FROM sub_queue q LEFT JOIN files f ON f.id = q.file_id "
                " WHERE q.asks != '[]' "
                " ORDER BY q.queued_at LIMIT ?", (int(limit),)):
            d = dict(r)
            try:
                d["label"] = display_label(d.pop("title", None),
                                           d.pop("season", None),
                                           d.pop("episode", None)) or ""
            except Exception:                                    # noqa: BLE001
                d["label"] = ""
            d["added"] = float(d.pop("first_seen", 0) or 0.0)
            try:
                d["asks"] = json.loads(d["asks"] or "[]")
            except Exception:                                    # noqa: BLE001
                d["asks"] = []
            try:
                d["steps"] = json.loads(d["steps"] or "[]")
            except Exception:                                    # noqa: BLE001
                d["steps"] = []
            out.append(d)
    return out


def answer(file_id: int, question: str, choice: str,
           scope: str = "both") -> dict:
    r"""What you decided, applied to this file and remembered for the next.

    THE ANSWER GOES IN THE MEMORY FIRST AND THE QUEUE SECOND, deliberately.
    Re-planning is what turns an answer into steps, and it consults the
    memory - so writing the memory first means this file is re-planned with
    your answer already in force, by the same code path that will plan every
    other episode of that show. There is no separate "apply it here" route to
    disagree with the general one.
    """
    from . import subplan, subscan
    init()
    with cursor() as cur:
        r = cur.execute("SELECT path FROM sub_queue WHERE file_id=?",
                        (int(file_id),)).fetchone()
    path = (r["path"] if r else "") or ""
    # "just this one" IS STILL SOMETHING TO REMEMBER. It has to be, or the
    # file you answered comes straight back with the same question - so the
    # narrow answer is written against the file itself, and subplan.recall
    # reads file, then show, then group, narrowest first.
    scopes = {"both": ("file", "show", "group"), "show": ("file", "show"),
              "group": ("file", "group"),
              "file": ("file",)}.get(scope, ("file", "show", "group"))
    learned = subplan.remember(int(file_id), path, question, choice,
                               scopes=scopes)
    bump()
    f = subscan.facts(int(file_id))
    p = subplan.plan(f, _ctx(True))
    if p["steps"] or p["asks"]:
        _put(p, p["rev"], None)
    else:
        _delete(int(file_id))
    return {"ok": True, "learned": learned.get("learned") or [],
            "now": p["why"], "queued": bool(p["steps"])}


def requeue(file_id: int) -> dict:
    """Put a failed or finished file back on, as it stands today."""
    from . import subplan, subscan
    init()
    f = subscan.facts(int(file_id), read_now=True)
    p = subplan.plan(f, _ctx())
    if not (p["steps"] or p["asks"]):
        _delete(int(file_id))
        return {"ok": True, "why": "nothing to do to this file"}
    _put(p, p["rev"], None)
    return {"ok": True, "why": p["why"]}


# -------------------------------------------------------------- the panel --
def snapshot(limit: int = 60) -> dict:
    """What is running, what is waiting, what finished and what failed."""
    init()
    out = {"counts": {}, "running": [], "queued": [], "failed": [],
           "recent": [], "state": dict(STATE)}
    with cursor() as cur:
        for r in cur.execute("SELECT state, COUNT(*) n, SUM(n) steps "
                             "  FROM sub_queue GROUP BY state"):
            out["counts"][r["state"]] = {"files": int(r["n"] or 0),
                                         "steps": int(r["steps"] or 0)}
        def rows(sql, args):
            return [dict(x) for x in cur.execute(sql, args)]
        cols = ("file_id, name, path, library, disk, n, why, steps, asks, "
                "state, queued_at, started_at, finished_at, took, tries, err")
        out["running"] = rows(f"SELECT {cols} FROM sub_queue WHERE state=? "
                              " ORDER BY started_at", (RUNNING,))
        out["queued"] = rows(f"SELECT {cols} FROM sub_queue WHERE state=? "
                             " ORDER BY priority, queued_at LIMIT ?",
                             (QUEUED, int(limit)))
        out["failed"] = rows(f"SELECT {cols} FROM sub_queue WHERE state=? "
                             " ORDER BY finished_at DESC LIMIT ?",
                             (FAILED, int(limit)))
        out["recent"] = rows(f"SELECT {cols} FROM sub_queue WHERE state=? "
                             " ORDER BY finished_at DESC LIMIT ?",
                             (DONE, int(limit)))
    for k in ("running", "queued", "failed", "recent"):
        for r in out[k]:
            for j in ("steps", "asks"):
                try:
                    r[j] = json.loads(r.get(j) or "[]")
                except Exception:                                # noqa: BLE001
                    r[j] = []
    return out


def stats() -> dict:
    r"""The queue's own numbers, plus the runner's.

    THE COUNTS LIVE UNDER "q" ON PURPOSE. idle.merge_stats adds the runner's
    state to whatever it is handed, and the runner has a key called `running`
    meaning "is a file being worked on"; the queue has a state called
    `running` meaning "how many rows are". Two different questions with one
    name in one dict is a bug waiting for a panel to find it.
    """
    init()
    q = {"queued": 0, "asking": 0, "failed": 0, "done": 0, "running": 0,
         "ask": 0, "held": 0, "steps": 0}
    with cursor() as cur:
        for r in cur.execute("SELECT state, COUNT(*) n, SUM(n) s "
                             "  FROM sub_queue GROUP BY state"):
            q[r["state"]] = int(r["n"] or 0)
            if r["state"] in (QUEUED, RUNNING):
                q["steps"] += int(r["s"] or 0)
        r = cur.execute("SELECT COUNT(*) n FROM sub_queue "
                        " WHERE asks != '[]'").fetchone()
        q["asking"] = int(r["n"] or 0)
    out = {"q": q}
    try:
        from . import idle
        out = idle.merge_stats(KEY, out)
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ------------------------------------------------------- feeding the queue --
def run_now(file_id: int) -> dict:
    r"""Carry out one file's instruction here and now, without a job.

    The one path that does not go through the queue, and it is for a person
    pressing a button about one file they are looking at. Everything the
    SYSTEM decides to do goes through the queue; this exists so that asking
    for something by hand does not mean waiting for a top-up.
    """
    row = row_for(int(file_id))
    if not row:
        return {"ok": True, "why": "nothing to do to this file"}
    try:
        r = do_one(row)
    except Exception as e:                                       # noqa: BLE001
        r = {"ok": False, "why": f"{type(e).__name__}: {e}"[:240]}
    note_result(int(file_id), r)
    return r


# How often the reader comes back. Fast while there is unread material, slow
# once the library is read - at which point it is only looking for files that
# changed and sidecars that appeared beside ones that did not.
READ_BUSY_S = 5.0
READ_IDLE_S = 300.0


async def _reader_and_feeder() -> None:
    r"""Read a slice of the library, then bring the queue into line with it.

    THREE THINGS IN ORDER, AND NONE OF THEM DOES THE WORK.

        read     a slice of the library into the facts table
        plan     bring the queue into line with the facts and the rules
        hand over  put what it is SURE about onto the main job queue

    That last step is the change Erik asked for: the reading no longer feeds a
    worker of its own, it feeds the queue everything else goes through, and
    the work appears in Processing System beside the transcodes. There is one
    place to look at what nuarr is doing to a file again.

    READING STANDS ASIDE; HANDING OVER DOES NOT. A listdir across a spun-down
    pool disk is not free while somebody is watching something, so the scan
    waits for the gate. Writing a row to the jobs table costs nothing and
    blocks nobody, and whether the WORK may start is not this loop's question -
    it is asked per job at dispatch, by the same gate that holds a transcode.
    Holding the hand-over as well would only mean the queue sat empty at
    exactly the moment it was allowed to drain.
    """
    from . import idle, subscan
    while True:
        left = 0
        try:
            b = await idle.busy()
            # WHETHER IT IS ALLOWED TO READ, recorded either way. The panel
            # says "next pass in 40s" and a held sweep makes that a lie unless
            # the hold is said too - see subscan.STATE["gated"].
            subscan.STATE["gated"] = bool(b["busy"])
            if not b["busy"]:
                from . import jobs as _jobs
                await _jobs.in_work(subscan.scan, subscan.BATCH)
                await _jobs.in_work(replan)
                left = int(subscan.counts().get("left") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            d = await topup()
            STATE["fed"] = int(d.get("made") or 0)
            STATE["on_queue"] = int(d.get("on_queue") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        # AND THE BATONS NOBODY CAN TAKE. Prepared OCR subtitles wait here for
        # whatever rewrites the file next; when that has already happened, or
        # the file is gone, or a day has passed with nothing queued, they are
        # so much cache. This is the subtitle system's own loop and they are
        # subtitle work, so it sweeps them - throttled to once every ten
        # minutes inside sweep_pending, which is why calling it every pass is
        # not a cost. See subocr.pending_state.
        try:
            from . import jobs as _j, subocr as _so
            await _j.in_work(_so.sweep_pending)
        except Exception:                                        # noqa: BLE001
            pass
        _nap = READ_BUSY_S if left else READ_IDLE_S
        # NOT BEFORE, rather than a promise: the pass at the top of this loop
        # only reads if the box is idle, so this is when it will next LOOK.
        try:
            subscan.STATE["next_at"] = time.time() + _nap
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(_nap)


async def watch() -> None:
    r"""Read, plan, hand over. The working is somebody else's job now.

    THIS USED TO RUN A WORKER OF ITS OWN, on the shared background runner,
    with its own pacing and its own idea of when the box was free. That was
    one more schedule deciding when a library file gets rewritten, and it
    meant the answer to "what is nuarr doing" depended on which page you had
    open. The instruction is handed to the queue now and the queue's workers
    carry it out under the gate, on the spindle rule, through the commit
    path - the same treatment a transcode gets, because it is the same kind
    of risk.
    """
    await _reader_and_feeder()
