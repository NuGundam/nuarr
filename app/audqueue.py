r"""nuarr - the audio queue: one row per file whose tags do not match its audio.

WHAT THIS IS. audiolang's `audio_lang` table says what was HEARD, audplan says
what should HAPPEN, and this holds the answer, hands it to the main job queue,
and remembers what you taught it on the way. It is subqueue's shape, on
purpose: a row per file, a state, the instruction written into the row, workers
that take the oldest first, and a panel that shows what is running. A second
vocabulary for the same idea is how a page becomes hard to read, and these two
pages are now the same idea twice.

WHY THE INSTRUCTION LIVES IN THE ROW. A queue holding only file ids would have
to re-plan at dispatch, and then the thing you looked at on the page and the
thing that ran would be two different decisions separated by however long the
row waited. The steps are written down when the row is made, stamped with the
rules revision that produced them, and that is what runs.

AND WHY THE STAMP MATTERS. Move the confidence lines, or the mode switch, and
audplan.revision() changes. Every row carrying the old stamp is re-planned -
from the measurements, without touching a disk - and the queue reshapes itself:
files that no longer need anything leave, files that now do arrive, files whose
instruction changed get the new one.

THE WORK IS SMALL AND THE MEASUREMENT IS NOT. This is worth being clear about,
because it is the one place audio genuinely differs from subtitles. Correcting
a tag is mkvpropedit writing a header - a fraction of a second, no rewrite, no
re-encode, nothing re-read. The expensive part is the LISTENING that produced
the verdict, and that already has a reader, a pass and a progress bar of its
own. So this queue is about fixes, it drains fast, and the backlog you watch on
the page is the reader's.

WHICH IS ALSO WHY THE DISK SPREAD STILL MATTERS. A header write is quick but it
is still a seek into a pool disk, and a file being watched on that spindle is
still a file nuarr should stand off. Rows are dealt to the job queue round
robin by disk for the same reason the subtitle queue learned to - so one busy
viewer stops the files on THEIR disk rather than stopping everything.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from .db import cursor

KEY = "audqueue"
TITLE = "Audio languages"
SYSTEM = "Audio languages"

# States a row can be in.
QUEUED, RUNNING, ASK, DONE, FAILED, HELD = ("queued", "running", "ask",
                                            "done", "failed", "held")

# A failed row is not retried forever; after this many tries it sits in the
# failures list waiting for a person.
MAX_TRIES = 3
# How long a finished row stays visible before it is cleared out.
DONE_KEEP_S = 24 * 3600.0

_READY = False
_CTX: dict = {"at": 0.0, "data": None}
_CTX_TTL = 60.0

STATE: dict = {"replanning": False, "at": 0.0, "took": 0.0, "made": 0,
               "gone": 0, "changed": 0, "rev": "", "err": "",
               # How many rows the last pass handed to the main job queue, and
               # how many audio jobs are live on it right now.
               "fed": 0, "on_queue": 0}


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS aud_queue(
                file_id     INTEGER PRIMARY KEY,
                state       TEXT    NOT NULL DEFAULT 'queued',
                steps       TEXT    NOT NULL DEFAULT '[]',
                asks        TEXT    NOT NULL DEFAULT '[]',
                n           INTEGER NOT NULL DEFAULT 0,
                why         TEXT    NOT NULL DEFAULT '',
                rev         TEXT    NOT NULL DEFAULT '',
                unread      INTEGER NOT NULL DEFAULT 0,
                priority    INTEGER NOT NULL DEFAULT 100,
                path        TEXT    NOT NULL DEFAULT '',
                name        TEXT    NOT NULL DEFAULT '',
                library     TEXT    NOT NULL DEFAULT '',
                disk        TEXT    NOT NULL DEFAULT '',
                series      TEXT    NOT NULL DEFAULT '',
                queued_at   REAL    NOT NULL DEFAULT 0,
                started_at  REAL    NOT NULL DEFAULT 0,
                finished_at REAL    NOT NULL DEFAULT 0,
                took        REAL    NOT NULL DEFAULT 0,
                tries       INTEGER NOT NULL DEFAULT 0,
                err         TEXT    NOT NULL DEFAULT '',
                result      TEXT    NOT NULL DEFAULT '{}'
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_aud_queue_state "
                    "ON aud_queue(state, priority, queued_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_aud_queue_fin "
                    "ON aud_queue(finished_at DESC)")
        # A ROW MARKED RUNNING WITH NOBODY RUNNING IT IS A ROW FROM BEFORE THE
        # RESTART. The worker sets that state and the process that was going to
        # clear it is gone, so without this the file is stuck: replan will not
        # touch a running row and no worker will pick it up again. Put it back
        # rather than fail it - the machine stopped, the file is probably fine.
        try:
            cur.execute(
                "UPDATE aud_queue SET state='queued', started_at=0 "
                " WHERE state='running' AND file_id NOT IN "
                "   (SELECT file_id FROM jobs WHERE kind='audio' "
                "     AND state='running' AND file_id IS NOT NULL)")
        except Exception:                                        # noqa: BLE001
            pass
    _READY = True


def _ctx(force: bool = False) -> dict:
    """The planner's context, held for a minute."""
    from . import audplan
    now = time.time()
    if force or _CTX["data"] is None or now - _CTX["at"] > _CTX_TTL:
        _CTX.update(at=now, data=audplan.context())
    return _CTX["data"]


def bump() -> None:
    """The rules moved. Forget the context so the next pass reads them."""
    _CTX.update(at=0.0)


# ------------------------------------------------------------- the replan --
def replan(limit: int = 100000, force: bool = False) -> dict:
    r"""Bring the queue into line with the measurements and the rules.

    TOUCHES NO DISK. Everything it reads is already in the database - what was
    heard, how sure, what the tags say, what the probes saw - so this is free
    enough to run on every pass and after every answer.

    WHAT IT DOES TO EACH FILE. Plans it; then:
      nothing to do        -> the row goes (unless it is running, or done and
                              still inside its keep window)
      same as the row      -> left exactly alone, so a queue position and a
                              retry count survive a re-plan
      different, or new    -> written, and put back in the queue

    A ROW THAT IS RUNNING IS NEVER TOUCHED. It has a worker attached to it, and
    re-planning underneath would leave that worker writing a header for an
    instruction that no longer exists.
    """
    from . import audplan
    if STATE["replanning"]:
        return {"ok": False, "why": "already re-planning"}
    STATE.update(replanning=True, err="", made=0, gone=0, changed=0)
    t0 = time.time()
    try:
        init()
        ctx = _ctx(force)
        rev = ctx["rev"]
        rows = audplan.by_file(ctx)[:int(limit)]
        with cursor() as cur:
            have = {int(r["file_id"]): dict(r) for r in cur.execute(
                "SELECT file_id, state, steps, asks, rev, tries "
                "  FROM aud_queue")}
        seen = set()
        for f in rows:
            fid = int(f["file_id"])
            seen.add(fid)
            old = have.get(fid)
            if old and old["state"] == RUNNING:
                continue
            p = audplan.plan(f, ctx)
            if not (p["steps"] or p["asks"]):
                if old and old["state"] not in (DONE, RUNNING):
                    _delete(fid)
                    STATE["gone"] += 1
                continue
            same = (old is not None
                    and old["rev"] == rev
                    and old["steps"] == json.dumps(p["steps"])
                    and old["asks"] == json.dumps(p["asks"]))
            if same and old["state"] in (QUEUED, ASK, FAILED, HELD, DONE):
                # DONE under these very rules, with the same instruction, means
                # the work already happened; re-queueing would redo it.
                continue
            _put(p, rev, old)
            STATE["made" if old is None else "changed"] += 1
        # Rows whose file has nothing to say any more - corrected by somebody
        # else, deleted, or retired with its show.
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
            "INSERT INTO aud_queue(file_id,state,steps,asks,n,why,rev,unread,"
            "  priority,path,name,library,disk,series,queued_at,tries) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(file_id) DO UPDATE SET state=excluded.state,"
            "  steps=excluded.steps, asks=excluded.asks, n=excluded.n,"
            "  why=excluded.why, rev=excluded.rev, unread=excluded.unread,"
            "  priority=excluded.priority, path=excluded.path,"
            "  name=excluded.name, library=excluded.library,"
            "  disk=excluded.disk, series=excluded.series,"
            "  queued_at=excluded.queued_at, err='', finished_at=0",
            (p["file_id"], state, json.dumps(p["steps"]),
             json.dumps(p["asks"]), p["n"], p["why"], rev,
             1 if p.get("titles_unread") else 0,
             # A file with a question on it waits behind files that do not.
             (100 if p["asks"] else 50),
             p["path"], p["name"], p["library"], p["disk"],
             p.get("series") or "", time.time()))


def _delete(file_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM aud_queue WHERE file_id=?", (int(file_id),))


def _sweep_done() -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM aud_queue WHERE state=? AND finished_at < ?",
                    (DONE, time.time() - DONE_KEEP_S))


# ---------------------------------------------------------------- the work --
def pending(limit: int = 500) -> list:
    """What is waiting, cheapest first, oldest first within that."""
    init()
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT file_id, path, name, library, disk, steps, n, why "
            "  FROM aud_queue WHERE state=? "
            " ORDER BY priority, queued_at LIMIT ?", (QUEUED, int(limit)))]


def _mark(file_id: int, state: str, **kw) -> None:
    sets = ["state=?"]
    args: list = [state]
    for k, v in kw.items():
        sets.append(f"{k}=?")
        args.append(v)
    args.append(int(file_id))
    with cursor() as cur:
        cur.execute(f"UPDATE aud_queue SET {', '.join(sets)} WHERE file_id=?",
                    args)


def row_for(file_id: int) -> dict:
    r"""The instruction for one file, marked as running. For the job worker.

    MARKED HERE RATHER THAN WHEN THE JOB WAS MADE. There can be minutes between
    a row being handed to the queue and a worker picking it up, and in that time
    a rule change or a new reading may have re-planned it - so the state that
    means "a worker has this" is set by the worker, and the row it reads is
    whatever the row says at that moment.
    """
    init()
    with cursor() as cur:
        r = cur.execute(
            "SELECT file_id, path, name, library, disk, steps, n, why, unread "
            "  FROM aud_queue WHERE file_id=?", (int(file_id),)).fetchone()
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


def note_result(file_id: int, res: dict) -> None:
    """What the worker found, written back onto the row."""
    init()
    fid = int(file_id)
    if res.get("ok"):
        _mark(fid, DONE, finished_at=time.time(), err="",
              result=json.dumps(res, default=str)[:4000])
        return
    with cursor() as cur:
        cur.execute("UPDATE aud_queue SET tries=tries+1 WHERE file_id=?", (fid,))
        n = cur.execute("SELECT tries FROM aud_queue WHERE file_id=?",
                        (fid,)).fetchone()
    tries = int((n["tries"] if n else 0) or 0)
    _mark(fid, FAILED if tries >= MAX_TRIES else QUEUED,
          finished_at=time.time(), err=str(res.get("why") or "")[:400],
          result=json.dumps(res, default=str)[:4000])


# How many audio jobs to keep on the main queue at once. THE LIST IS NOT THE
# QUEUE - putting every waiting file in the jobs table would make the queue
# panel a scrollbar and every poll of it a page fault. Same answer subqueue and
# autoqueue reached, for the same reason.
QUEUE_DEPTH = 200


def _to_hand_over(depth: int) -> tuple:
    r"""How much room the queue has, and which rows would fill it.

    SPREAD ACROSS SPINDLES, NOT TAKEN IN ORDER. The subtitle queue learned this
    the expensive way: of 5,300 files waiting, 3,302 lived on one disk, so the
    oldest two hundred were two hundred files on that disk and the whole queue
    sat behind one viewer while eleven spindles idled. The dispatcher already
    refuses to put two heavy jobs on one spindle and that rule is right; it
    cannot help if every job it is handed is on the same spindle.

    So the rows are dealt out round robin by disk. Still oldest-first WITHIN a
    disk, so nothing is starved and the order inside a show is preserved.
    """
    init()
    with cursor() as cur:
        have = int(cur.execute(
            "SELECT COUNT(*) n FROM jobs "
            " WHERE kind='audio' AND state IN ('queued','running')"
        ).fetchone()["n"] or 0)
        room = max(0, int(depth) - have)
        if not room:
            return have, []
        rows = [dict(r) for r in cur.execute(
            "SELECT file_id, path, name, steps, why, disk "
            "  FROM aud_queue WHERE state=? "
            " ORDER BY priority, queued_at LIMIT ?",
            (QUEUED, min(20000, max(room * 20, 2000))))]
    by_disk: dict = {}
    for r in rows:
        by_disk.setdefault(r.get("disk") or "?", []).append(r)
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

    Planned work is the one column that answers "what is this job going to do
    to my file", and a job that rewrites a language header should not leave it
    blank. Same shape the transcode and subtitle plans use, so the row's hover
    and the job card's action list need no special case.
    """
    try:
        steps = json.loads(r.get("steps") or "[]")
    except Exception:                                            # noqa: BLE001
        steps = []
    return json.dumps({
        "audio": True,
        "rewrite": False,          # a header edit; the video is never touched
        "summary": r.get("why") or "",
        "actions": [{"kind": "audio", "what": _step_words(s),
                     "why": s.get("why") or "", "detail": ""}
                    for s in steps],
    })


def _step_words(s: dict) -> str:
    """One step as a sentence, for the plan and the job card."""
    d = s.get("do")
    t = int(s.get("track") or 0) + 1
    if d == "tag":
        return (f"correct track {t}'s language from {s.get('from') or '?'} "
                f"to {s.get('to') or '?'}")
    if d == "retitle":
        return (f"correct track {t}'s title to {s.get('to') or ''}")
    return str(d or "")


async def topup(depth: int = QUEUE_DEPTH) -> dict:
    r"""Hand what the readings are sure about to the main queue.

    ONLY WHAT IT IS SURE ABOUT. A row with a question on it is in state 'ask'
    and is never handed over - that is what Audio User Input means. Answering
    one re-plans it into 'queued', and then it comes through here like anything
    else. One road in, and the thing you were asked about takes it like
    everything else.

    AND THE DUPLICATE GUARD IS RESPECTED RATHER THAN WORKED AROUND.
    jobs.enqueue refuses a second live job for a file and raises to say so. A
    file already being transcoded is a file whose tags can be corrected the
    moment it is done, so a refusal is counted and the row waits for the next
    top-up.
    """
    from . import jobs
    made = skipped = 0
    try:
        have, rows = await asyncio.to_thread(_to_hand_over, depth)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    for r in rows:
        try:
            await jobs.enqueue(int(r["file_id"]), r["path"],
                               r.get("name") or "", kind="audio",
                               priority=50, source="audio languages",
                               plan_json=_job_plan(r))
            made += 1
        except ValueError:
            skipped += 1
        except Exception:                                        # noqa: BLE001
            skipped += 1
    return {"ok": True, "made": made, "skipped": skipped,
            "on_queue": have + made}


def do_one(row: dict, report=None, claim: bool = True) -> dict:
    r"""Carry out one file's whole instruction.

    THE TAGS FIRST, THEN THE TITLES. audiolang.fix_mislabel already corrects
    the title word alongside the tag it is changing - a track that goes from
    eng to jpn should not be left titled "English E-AC3 5.1" - so running the
    tags first means the standalone retitle steps are only the ones no tag
    change covered.

    NOTHING HERE REWRITES A CONTAINER. Every step is mkvpropedit editing a
    header in place: no temp file, no cache reservation, no commit, no verify
    pass. That is worth stating because the subtitle queue next door does all
    four, and someone reading the two side by side should not have to wonder
    whether this one forgot.
    """
    from . import audiolang, idle
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
    # card says which file, which disk and how far along; claiming a second
    # entry would put the same file on the disk panel twice.
    work = getattr(report, "task", None)
    mine = claim and work is None
    if mine:
        work = idle.claim(SYSTEM, os.path.basename(path),
                          now=os.path.basename(path),
                          disk=row.get("disk") or "", note="audio languages")
    stage = getattr(report, "on_stage", None)

    def say(name):
        if stage is not None:
            try:
                stage(name)
            except Exception:                                    # noqa: BLE001
                pass

    out = {"ok": True, "did": [], "why": ""}
    try:
        tags = [s for s in steps if s.get("do") == "tag"]
        for i, s in enumerate(tags):
            say(f"correcting language tag {i + 1} of {len(tags)}")
            try:
                r = audiolang.fix_mislabel(fid, int(s.get("track") or 0))
            except Exception as e:                               # noqa: BLE001
                r = {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
            out["did"].append({"do": "tag", "track": s.get("track"), **r})
            if not r.get("ok"):
                return {"ok": False, "did": out["did"],
                        "why": r.get("why") or "the tag could not be written"}

        # THE TITLES NO TAG CHANGE ALREADY COVERED, and re-read rather than
        # trusted. The plan's title steps may have come from a stored probe, and
        # fix_mislabel above may have rewritten some of those very titles - so
        # what is left to do is asked of the file, once, now.
        rt = [s for s in steps if s.get("do") == "retitle"]
        if rt:
            say("correcting track titles")
            lies = {int(t["track"]): t for t in (audiolang.title_lies(fid) or [])}
            fixed, left = [], []
            for s in rt:
                t = lies.get(int(s.get("track") or 0))
                if not t:
                    continue        # already right, or already fixed above
                if audiolang._set_track_title(path, t["track"], t["want"]):
                    fixed.append(f"track {int(t['track']) + 1}: "
                                 f"{t['title']!r} -> {t['want']!r}")
                else:
                    left.append(int(t["track"]) + 1)
            if fixed:
                try:
                    audiolang._reprobe_quiet(fid, path)
                except Exception:                                # noqa: BLE001
                    pass
            out["did"].append({"do": "retitle", "ok": not left,
                               "n": len(fixed), "fixed": fixed, "left": left,
                               "why": "" if not left else
                               f"could not write the title on track(s) "
                               f"{', '.join(str(x) for x in left)}"})
            if left:
                return {"ok": False, "did": out["did"],
                        "why": out["did"][-1]["why"]}
        out["why"] = "; ".join(_step_words(s) for s in steps)
        return out
    finally:
        if mine and work is not None:
            work.close()


# ------------------------------------------------------------ the answers --
def asking(limit: int = 400) -> list:
    """Every file with a question on it, and what the question is."""
    init()
    out = []
    with cursor() as cur:
        for r in cur.execute(
                "SELECT file_id, path, name, library, disk, series, asks, "
                "       steps, why FROM aud_queue WHERE asks != '[]' "
                " ORDER BY queued_at LIMIT ?", (int(limit),)):
            d = dict(r)
            for j in ("asks", "steps"):
                try:
                    d[j] = json.loads(d.get(j) or "[]")
                except Exception:                                # noqa: BLE001
                    d[j] = []
            out.append(d)
    return out


def answer(file_id: int, question: str, choice: str,
           scope: str = "both") -> dict:
    r"""What you decided, applied to this file and remembered for the next.

    THE ANSWER GOES IN THE MEMORY FIRST AND THE QUEUE SECOND, deliberately.
    Re-planning is what turns an answer into steps, and it consults the memory -
    so writing the memory first means this file is re-planned with your answer
    already in force, by the same code that will plan every other episode of
    that show. There is no separate "apply it here" route to disagree with the
    general one.
    """
    from . import audplan
    init()
    with cursor() as cur:
        r = cur.execute("SELECT path FROM aud_queue WHERE file_id=?",
                        (int(file_id),)).fetchone()
    path = (r["path"] if r else "") or ""
    if not path:
        with cursor() as cur:
            r = cur.execute("SELECT path FROM files WHERE id=?",
                            (int(file_id),)).fetchone()
        path = (r["path"] if r else "") or ""
    # "just this one" IS STILL SOMETHING TO REMEMBER, or the file you answered
    # comes straight back with the same question - so the narrow answer is
    # written against the file itself, and audplan.recall reads file, then show,
    # then group, narrowest first.
    scopes = {"both": ("file", "show", "group"), "show": ("file", "show"),
              "group": ("file", "group"),
              "file": ("file",)}.get(scope, ("file", "show", "group"))
    learned = audplan.remember(int(file_id), path, question, choice,
                               scopes=scopes)
    bump()
    ctx = _ctx(True)
    f = ([x for x in audplan.by_file(ctx) if x["file_id"] == int(file_id)]
         or [None])[0]
    if f is None:
        _delete(int(file_id))
        return {"ok": True, "learned": learned.get("learned") or [],
                "now": "nothing left to correct on this file", "queued": False,
                "settled": True}
    p = audplan.plan(f, ctx)
    if p["steps"] or p["asks"]:
        _put(p, p["rev"], None)
    else:
        _delete(int(file_id))
    return {"ok": True, "learned": learned.get("learned") or [],
            "now": p["why"], "queued": bool(p["steps"]),
            "settled": not (p["steps"] or p["asks"])}


def requeue(file_id: int) -> dict:
    """Put a failed or finished file back on, as it stands today."""
    from . import audplan
    init()
    ctx = _ctx(True)
    f = ([x for x in audplan.by_file(ctx) if x["file_id"] == int(file_id)]
         or [None])[0]
    if f is None:
        _delete(int(file_id))
        return {"ok": True, "why": "nothing to do to this file"}
    p = audplan.plan(f, ctx, live_titles=True)
    if not (p["steps"] or p["asks"]):
        _delete(int(file_id))
        return {"ok": True, "why": "nothing to do to this file"}
    _put(p, p["rev"], None)
    return {"ok": True, "why": p["why"]}


def run_now(file_id: int) -> dict:
    r"""Carry out one file's instruction here and now, without a job.

    The one path that does not go through the queue, and it is for a person
    pressing a button about one file they are looking at. Everything the SYSTEM
    decides to do goes through the queue; this exists so that asking for
    something by hand does not mean waiting for a top-up.
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


# -------------------------------------------------------------- the panel --
def snapshot(limit: int = 60) -> dict:
    """What is running, what is waiting, what finished and what failed."""
    init()
    out = {"counts": {}, "running": [], "queued": [], "failed": [],
           "recent": [], "state": dict(STATE)}
    with cursor() as cur:
        for r in cur.execute("SELECT state, COUNT(*) n, SUM(n) steps "
                             "  FROM aud_queue GROUP BY state"):
            out["counts"][r["state"]] = {"files": int(r["n"] or 0),
                                         "steps": int(r["steps"] or 0)}

        def rows(sql, args):
            return [dict(x) for x in cur.execute(sql, args)]

        cols = ("file_id, name, path, library, disk, series, n, why, steps, "
                "asks, state, queued_at, started_at, finished_at, took, "
                "tries, err")
        out["running"] = rows(f"SELECT {cols} FROM aud_queue WHERE state=? "
                              " ORDER BY started_at", (RUNNING,))
        out["queued"] = rows(f"SELECT {cols} FROM aud_queue WHERE state=? "
                             " ORDER BY priority, queued_at LIMIT ?",
                             (QUEUED, int(limit)))
        out["failed"] = rows(f"SELECT {cols} FROM aud_queue WHERE state=? "
                             " ORDER BY finished_at DESC LIMIT ?",
                             (FAILED, int(limit)))
        out["recent"] = rows(f"SELECT {cols} FROM aud_queue WHERE state=? "
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
    r"""The queue's own numbers.

    THE COUNTS LIVE UNDER "q" ON PURPOSE, the same as subqueue's. A queue state
    called `running` means "how many rows are"; a runner's `running` means "is
    a file being worked on". Two different questions with one name in one dict
    is a bug waiting for a panel to find it.
    """
    init()
    q = {"queued": 0, "asking": 0, "failed": 0, "done": 0, "running": 0,
         "ask": 0, "held": 0, "steps": 0}
    with cursor() as cur:
        for r in cur.execute("SELECT state, COUNT(*) n, SUM(n) s "
                             "  FROM aud_queue GROUP BY state"):
            q[r["state"]] = int(r["n"] or 0)
            if r["state"] in (QUEUED, RUNNING):
                q["steps"] += int(r["s"] or 0)
        r = cur.execute("SELECT COUNT(*) n FROM aud_queue "
                        " WHERE asks != '[]'").fetchone()
        q["asking"] = int(r["n"] or 0)
    return {"q": q}


# ------------------------------------------------------- feeding the queue --
# How often the planner comes back. There is no library read here - the
# listening has its own pass and its own pacing - so this is only asking
# whether the measurements or the rules have moved since last time, which is
# two queries and a hash.
PLAN_S = 60.0


async def _planner_and_feeder() -> None:
    r"""Plan, then hand over. Neither of them does the work.

        plan       bring the queue into line with what has been heard
        hand over  put what it is SURE about onto the main job queue

    The work appears in Processing System beside the transcodes and the
    subtitle jobs, which is the point: one place to look at what nuarr is doing
    to a file.

    NEITHER STEP WAITS FOR THE GATE. Planning reads the database and writing a
    row to the jobs table costs nothing and blocks nobody. Whether the WORK may
    start is not this loop's question - it is asked per job at dispatch, by the
    same gate that holds a transcode. Holding the hand-over as well would only
    mean the queue sat empty at exactly the moment it was allowed to drain.
    """
    while True:
        try:
            await asyncio.to_thread(replan)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            d = await topup()
            STATE["fed"] = int(d.get("made") or 0)
            STATE["on_queue"] = int(d.get("on_queue") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(PLAN_S)


async def watch() -> None:
    """Plan and hand over, forever."""
    await _planner_and_feeder()
