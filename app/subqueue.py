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
               "gone": 0, "changed": 0, "rev": "", "err": ""}


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


def do_one(row: dict, report=None) -> dict:
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

    work = getattr(report, "task", None)
    mine = work is None
    if mine:
        work = idle.claim(SYSTEM, os.path.basename(path),
                          now=os.path.basename(path),
                          disk=row.get("disk") or "", note="subtitles")
    out = {"ok": True, "did": [], "why": ""}
    try:
        takes = [s for s in steps if s["do"] == "take"]
        drops = [s for s in steps if s["do"] in ("dropdup", "dropempty")]
        if takes or drops:
            r = _rewrite(fid, path, takes, drops, report, work, row)
            out["did"].append(r)
            if not r.get("ok"):
                return {"ok": False, "why": r.get("why") or "the rewrite failed",
                        "did": out["did"]}
        rt = [s for s in steps if s["do"] == "retitle"]
        if rt:
            out["did"].append(_retitle(fid, path, rt))
        mk = [s for s in steps if s["do"] == "mark"]
        if mk:
            out["did"].append(_mark_picture(fid, mk[0]))
        rc = [s for s in steps if s["do"] == "recycle"]
        if rc:
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
        for o in ords:
            t = by_ord.get(o)
            if t is None:
                return {"do": "rewrite", "ok": False,
                        "why": "the tracks have moved since this was planned"}
            counted.append((subdupe._events(path, t["id"]), t))
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
                                 keep=keep, removed=len(remove))
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


# ------------------------------------------------------------- the runner --
def _pending() -> list:
    return pending(500)


def _do_one(row: dict, report=None) -> dict:
    fid = int(row["file_id"])
    _mark(fid, RUNNING, started_at=time.time())
    t0 = time.time()
    try:
        r = do_one(row, report)
    except Exception as e:                                       # noqa: BLE001
        r = {"ok": False, "why": f"{type(e).__name__}: {e}"[:240]}
    took = time.time() - t0
    if r.get("ok"):
        _mark(fid, DONE, finished_at=time.time(), took=took, err="",
              result=json.dumps(r)[:4000])
        joblog.log(f"subtitles settled on {row.get('name') or fid}: "
                   f"{row.get('why') or ''}", "ok", system="subtitles")
    else:
        with cursor() as cur:
            cur.execute("UPDATE sub_queue SET tries=tries+1 WHERE file_id=?",
                        (fid,))
            n = cur.execute("SELECT tries FROM sub_queue WHERE file_id=?",
                            (fid,)).fetchone()
        tries = int((n["tries"] if n else 0) or 0)
        _mark(fid, FAILED if tries >= MAX_TRIES else QUEUED,
              finished_at=time.time(), took=took,
              err=str(r.get("why") or "")[:400],
              result=json.dumps(r)[:4000])
    return r


def _disk_of(row: dict) -> str:
    return row.get("disk") or ""


# How often the reader comes back. Fast while there is unread material, slow
# once the library is read - at which point it is only looking for files that
# changed and sidecars that appeared beside ones that did not.
READ_BUSY_S = 5.0
READ_IDLE_S = 300.0


async def _reader() -> None:
    r"""Read a slice of the library, then bring the queue into line with it.

    ITS OWN LOOP, BESIDE THE WORKER RATHER THAN INSIDE IT. idle.run never
    returns - it is the process's working life - so anything that has to keep
    happening cannot sit in front of it. And these are genuinely two different
    jobs: one gathers what is true and costs a listdir, the other rewrites
    containers. Tying them together would mean the library stopped being read
    whenever there was work, which is exactly when new work is arriving.

    IT STANDS ASIDE FOR THE SAME REASONS THE WORKER DOES. A listdir across a
    spun-down pool disk is not free while somebody is watching something, so
    the same gate answers for both.
    """
    from . import idle, subscan
    while True:
        left = 0
        try:
            b = await idle.busy()
            if not b["busy"]:
                await asyncio.to_thread(subscan.scan, subscan.BATCH)
                d = await asyncio.to_thread(replan)
                left = int(subscan.counts().get("left") or 0)
                # A NEW ROW IS NEWS. The worker sleeps five minutes after
                # finding nothing; ringing the bell means work found by this
                # pass starts now rather than when that sleep happens to end.
                if (d.get("made") or 0) or (d.get("changed") or 0):
                    idle.bump(KEY)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(READ_BUSY_S if left else READ_IDLE_S)


def _after(_d: dict) -> None:
    """A finished pass re-plans at once, rather than waiting for the reader."""
    try:
        replan()
    except Exception:                                            # noqa: BLE001
        pass


async def watch() -> None:
    """The reader and the worker, side by side for the life of the process."""
    from . import idle
    await asyncio.gather(
        _reader(),
        idle.run(KEY, TITLE, _pending, _do_one,
                 label=(lambda r: r.get("name") or ""),
                 system_name=SYSTEM, disk_of=_disk_of,
                 empty_s=300.0, pause_s=20.0, rank=40,
                 goto="/settings#lang", on_pass=_after,
                 note_of=(lambda r: r.get("why") or "")))
