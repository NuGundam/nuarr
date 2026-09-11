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


def _sweep_done() -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM sub_queue WHERE state=? AND finished_at < ?",
                    (DONE, time.time() - DONE_KEEP_S))


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


def note_result(file_id: int, res: dict) -> None:
    """What the worker found, written back onto the row."""
    init()
    fid = int(file_id)
    if res.get("ok"):
        _mark(fid, DONE, finished_at=time.time(), err="",
              result=json.dumps(res)[:4000])
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
    """How much room the queue has, and which rows would fill it."""
    init()
    with cursor() as cur:
        have = int(cur.execute(
            "SELECT COUNT(*) n FROM jobs "
            " WHERE kind='subs' AND state IN ('queued','running')"
        ).fetchone()["n"] or 0)
        room = max(0, int(depth) - have)
        if not room:
            return have, []
        return have, [dict(r) for r in cur.execute(
            "SELECT file_id, path, name, rewrite, steps, why FROM sub_queue "
            " WHERE state=? ORDER BY priority, queued_at LIMIT ?",
            (QUEUED, room))]


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
    made = skipped = 0
    try:
        have, rows = await asyncio.to_thread(_to_hand_over, depth)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    for r in rows:
        try:
            await jobs.enqueue(int(r["file_id"]), r["path"],
                               r.get("name") or "", kind="subs",
                               priority=50, source="subtitles",
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
            counted.append((subdupe._events(path, t["id"], on_pid=on_pid), t))
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
        r = subembed._embed_tail(fid, path, tk, [], cmd, tmp, report, work,
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
    with cursor() as cur:
        for r in cur.execute(
                "SELECT file_id, path, name, library, disk, asks, steps, why "
                "  FROM sub_queue WHERE asks != '[]' "
                " ORDER BY queued_at LIMIT ?", (int(limit),)):
            d = dict(r)
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
            if not b["busy"]:
                await asyncio.to_thread(subscan.scan, subscan.BATCH)
                await asyncio.to_thread(replan)
                left = int(subscan.counts().get("left") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            d = await topup()
            STATE["fed"] = int(d.get("made") or 0)
            STATE["on_queue"] = int(d.get("on_queue") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(READ_BUSY_S if left else READ_IDLE_S)


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
