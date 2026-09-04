r"""
nuarr - durable Plex notification queue

WHY THIS REPLACED FIRE-AND-FORGET. The first version of telling Plex was a
daemon thread started from the commit path: scan the folder, look the item up,
analyze it, hope. That is fine until Plex is busy, restarting, or mid-scan -
and then the notification is simply gone, with nothing on disk to say it was
ever owed. The file is correct, Plex describes it wrongly, and the only thing
that will ever put that right is the daily scan the whole exercise was meant to
pre-empt.

Erik asked for the shape the rename queue already has, for the same reasons it
has it, and the reasons transfer exactly:

  * NOTHING BLOCKS A JOB. Enqueueing is one indexed write. A worker slot gates
    GPU throughput and must never be held open waiting on another server.
  * A RESTART RESUMES. State is a row, not a thread.
  * BUSY IS NOT FAILURE. Plex mid-scan answers slowly or not at all; that is
    "ask again in a minute", and the backoff says so.

AND IT CHECKS, WHICH THE THREAD COULD NOT. A notification is not delivered
because the request returned 200 - it is delivered when Plex is describing the
file the way the file actually is. Every row is verified against ffprobe before
it is closed, so "done" here means Plex and the disk agree, and a row that
cannot get there says why instead of disappearing quietly.

THE ORDER IS CHECK, THEN ACT, THEN CHECK. Most rows verify on the first look -
Plex's own scanner sometimes gets there first - and those cost one request and
no analysis at all. Only a row that actually disagrees is analyzed, and it is
re-checked afterwards rather than assumed.
"""
from __future__ import annotations

import asyncio
import os
import time

from . import joblog, plexnotify, schedules
from .db import cursor

# Shorter than the rename queue's. An analyze is seconds of Plex's time, and
# the failures this backs off from are "busy right now", not "the arr holds
# stale mediainfo for two minutes".
BACKOFF = [45, 180, 600, 1800, 7200, 21600]
MAX_ATTEMPTS = len(BACKOFF)
FIRST_DELAY = 15.0          # the commit has just landed; give Plex a moment
POLL_S = 25.0


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plex_queue (
                file_id     INTEGER PRIMARY KEY,
                path        TEXT,
                old_path    TEXT,
                why         TEXT,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT,
                next_try_at REAL NOT NULL,
                created_at  REAL NOT NULL,
                done_at     REAL
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_pq_due "
                    "ON plex_queue(done_at, next_try_at)")


def enqueue(file_id: int, path: str, old_path: str = "", why: str = "") -> None:
    """Note that Plex needs to look at this file again. Idempotent, cheap."""
    if not file_id or not path:
        return
    url, token = plexnotify._plex()
    if not url or not token:
        return                       # no Plex configured: nothing to owe it
    now = time.time()
    try:
        with cursor() as cur:
            row = cur.execute("SELECT file_id, done_at FROM plex_queue "
                              "WHERE file_id=?", (file_id,)).fetchone()
            if row and row["done_at"] is None:
                # Already pending. Keep the earlier old_path - it is where the
                # file came FROM, and a second rename before the first was
                # delivered must not lose the original location. Unless there
                # was not one: a row queued by the commit has no old_path, and
                # discarding the rename's meant the only record of where the
                # file came from was thrown away by the commit that preceded it.
                cur.execute(
                    "UPDATE plex_queue SET path=?, why=?, "
                    "old_path=CASE WHEN COALESCE(old_path,'')='' THEN ? "
                    "              ELSE old_path END "
                    "WHERE file_id=?", (path, why, old_path or "", file_id))
                return
            cur.execute(
                "INSERT INTO plex_queue(file_id,path,old_path,why,attempts,"
                "last_error,next_try_at,created_at,done_at) "
                "VALUES(?,?,?,?,0,NULL,?,?,NULL) "
                "ON CONFLICT(file_id) DO UPDATE SET "
                "path=excluded.path, old_path=excluded.old_path, "
                "why=excluded.why, attempts=0, last_error=NULL, "
                "next_try_at=excluded.next_try_at, done_at=NULL",
                (file_id, path, old_path or "", why, now + FIRST_DELAY, now))
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"could not queue a Plex refresh: {type(e).__name__}: {e}",
                   "debug")


def _scan(folder: str) -> None:
    """Partial scan of one directory. Failure is not fatal to the attempt."""
    import urllib.parse
    key, _kind = plexnotify.section_for(folder)
    if not key:
        return
    try:
        plexnotify._get(f"/library/sections/{key}/refresh?path="
                        + urllib.parse.quote(folder), timeout=15)
    except Exception:                                        # noqa: BLE001
        pass


HOLD_S = 30.0                # how long to wait between looks while renaming

# WHAT THE CURRENT PASS IS DOING. The card could say how many were waiting and
# nothing else, so a queue that was working looked identical to a queue that
# was stuck - both are "22 waiting" for twenty-five seconds at a time. This is
# the difference, and it costs one dict.
RUN_STATE: dict = {"running": False, "started": 0.0, "at": 0.0,
                   "done": 0, "total": 0, "corrected": 0, "already": 0,
                   "failed": 0, "held": 0, "now": ""}


def _renaming() -> set:
    """file_ids the rename queue still owes the arrs a rename for."""
    try:
        with cursor() as cur:
            return {r["file_id"] for r in cur.execute(
                "SELECT file_id FROM rename_queue WHERE done_at IS NULL")}
    except Exception:                                        # noqa: BLE001
        return set()


def _attempt(row: dict) -> tuple[bool, str, str]:
    r"""One row. Returns (delivered, headline, what was actually seen).

    THE THIRD VALUE IS THE POINT OF THIS FUNCTION EXISTING IN THIS SHAPE. A log
    line saying "Plex was asked and has not caught up" tells you the outcome and
    nothing that would let you decide whether to care. What it was describing,
    and what the file actually holds, is the evidence - and it is free here,
    because both signatures had to be computed to reach the verdict anyway.
    """
    from . import subocr
    path = row["path"] or ""
    if not path:
        return True, "nothing to tell Plex about", ""
    folder = os.path.dirname(path)
    key, kind = plexnotify.section_for(folder)
    if kind == -1:
        # Plex could not be asked. That is the whole reason this is a queue
        # rather than a thread: come back later, do not throw the row away.
        return False, "Plex could not be reached", ""
    if not key:
        # nuarr manages libraries Plex may not have. Not an error, and not
        # something to keep retrying either.
        return True, "Plex does not serve this path", folder

    # A FILE THAT IS GONE STILL NEEDS SAYING. Deleted or replaced releases are
    # exactly the case where Plex keeps offering something that cannot play, so
    # the folder is scanned and the row closes - there is nothing left to
    # verify against.
    if not os.path.exists(path):
        _scan(folder)
        return (True, "the file is gone - Plex was told to re-read the folder",
                folder)

    probe = subocr._probe_streams(path)
    if not probe:
        return False, "could not read the file to check Plex against it", ""
    want = plexnotify.probe_sig(probe)

    rk = plexnotify.rating_key_for(path, key, kind)
    if not rk:
        # Either Plex has not indexed it yet or it moved. Scan both ends and
        # come back - this is the ordinary state of a just-renamed file.
        old = os.path.dirname(row["old_path"] or "")
        if old and old.lower() != folder.lower():
            _scan(old)
            _scan(folder)
            return (False, "Plex has not indexed this path yet",
                    f"scanned both {os.path.basename(old) or old} and "
                    f"{os.path.basename(folder) or folder}")
        _scan(folder)
        return (False, "Plex has not indexed this path yet",
                f"scanned {os.path.basename(folder) or folder}")

    part = plexnotify.part_of(rk)
    if not part:
        return False, "Plex would not describe this item", f"item {rk}"
    got = plexnotify.plex_sig(part)
    if not plexnotify.sig_differs(got, want):
        return (True, "Plex already had it right",
                plexnotify.describe(want))

    # It disagrees, so make it look again - scan first for a renamed file, then
    # analyze, which is the step that re-reads the streams.
    seen = plexnotify.diff_words(got, want)
    old = os.path.dirname(row["old_path"] or "")
    if old and old.lower() != folder.lower():
        _scan(old)
    _scan(folder)
    if not plexnotify.analyze(rk):
        return False, "Plex would not accept an analyze right now", seen

    # Analysis is quick but not instant - measured at under six seconds on this
    # server. Checking immediately would fail a delivery that is about to
    # succeed, so this waits, and the backoff covers the case where it does not.
    for wait in (2.0, 4.0, 8.0):
        time.sleep(wait)
        part = plexnotify.part_of(rk)
        if part and not plexnotify.sig_differs(plexnotify.plex_sig(part), want):
            return True, "Plex re-read the file and now agrees", seen
    return (False, "Plex was asked but is still describing the old file", seen)


async def run_due(limit: int = 40) -> dict:
    r"""Every due row, oldest first. One scan per folder, one check per file.

    THE RENAME GOES FIRST, ALWAYS. A transcode commit queues two things: this,
    and a rename. The rename is the arr's, lands about twenty seconds later,
    and CHANGES THE PATH - so telling Plex before it happens is telling it
    about a file that is on the point of ceasing to exist. Plex re-reads the
    old path, the arr renames underneath it, and the row that the rename queue
    then files makes Plex do the whole thing again: two analyses, one of them
    against a path that will be a ghost entry until the next scan clears it.

    So a file with an outstanding rename is HELD, not attempted. Holding is not
    failing and does not spend an attempt - the six-attempt budget is for Plex
    being unreachable, and burning it on the normal twenty-second wait would
    have rows giving up before the rename they were waiting for had even been
    tried.

    EVERY PASS IS A LABELLED BLOCK, like the rename queue's. Four workers log
    into the same stream, so loose lines from a background loop scatter among
    them and there is no way to see where a pass began, what it did, or whether
    it finished. A pass that found nothing to do leaves nothing behind.
    """
    init()
    now = time.time()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT * FROM plex_queue WHERE done_at IS NULL AND next_try_at<=? "
            "ORDER BY next_try_at LIMIT ?", (now, limit))]
    if not rows:
        return {"tried": 0, "ok": 0, "failed": 0, "gave_up": 0, "held": 0}

    pending_rename = _renaming()
    held = [r for r in rows if r["file_id"] in pending_rename]
    rows = [r for r in rows if r["file_id"] not in pending_rename]
    if held:
        with cursor() as cur:
            cur.executemany(
                "UPDATE plex_queue SET next_try_at=?, last_error=? "
                "WHERE file_id=?",
                [(now + HOLD_S, "waiting for the arr to rename this file first",
                  r["file_id"]) for r in held])
    if not rows:
        # Held-only passes are worth ONE line and no block: this is the normal
        # twenty seconds after a commit, it happens for every processed file,
        # and a labelled section per poll would be four an hour saying nothing.
        if held:
            joblog.log(f"plex catch-up: holding {len(held)} file(s) until the "
                       f"arr rename lands", "debug")
        # A HELD-ONLY PASS IS STILL A PASS. It used to return without touching
        # the state, so the card's "last pass" could be minutes old while the
        # loop was running every twenty-five seconds - which is exactly the
        # "is this thing alive?" question the state exists to answer.
        RUN_STATE.update(running=False, at=time.time(), now="", done=0,
                         total=0, corrected=0, already=0, failed=0,
                         held=len(held))
        return {"tried": 0, "ok": 0, "failed": 0, "gave_up": 0,
                "held": len(held)}

    ok = failed = gave_up = corrected = 0
    RUN_STATE.update(running=True, started=time.time(), done=0,
                     total=len(rows), corrected=0, already=0, failed=0,
                     held=len(held), now="")
    sec = joblog.section("plex catch-up")
    sec.__enter__()
    sec.note(f"{len(rows)} file(s) due"
             + (f", {len(held)} more held for a rename" if held else ""))
    for row in rows:
        RUN_STATE["now"] = os.path.basename(row["path"] or "")[:70]
        try:
            good, why, seen = await asyncio.to_thread(_attempt, row)
        except Exception as e:                               # noqa: BLE001
            good, why, seen = False, f"{type(e).__name__}: {e}", ""
        name = os.path.basename(row["path"] or "")
        tail = f" — {seen}" if seen else ""
        if good:
            ok += 1
            with cursor() as cur:
                cur.execute("UPDATE plex_queue SET done_at=?, last_error=NULL "
                            "WHERE file_id=?", (time.time(), row["file_id"]))
            # A FILE PLEX HAD WRONG AND NOW HAS RIGHT IS THE WHOLE POINT, so it
            # is logged as work done. A file it already had right cost one
            # request and changed nothing, so it is not.
            RUN_STATE["done"] += 1
            if why.startswith("Plex re-read"):
                corrected += 1
                RUN_STATE["corrected"] = corrected
                sec.keep()
                sec.note(f"{name} — {why}{tail}"
                         + (f" · after {row['attempts'] + 1} attempts"
                            if row["attempts"] else ""), "ok")
            else:
                RUN_STATE["already"] += 1
                joblog.log(f"plex catch-up: {name} — {why}{tail}", "debug")
            continue

        attempts = (row["attempts"] or 0) + 1
        failed += 1
        RUN_STATE["done"] += 1
        RUN_STATE["failed"] = failed
        if attempts >= MAX_ATTEMPTS:
            gave_up += 1
            with cursor() as cur:
                cur.execute("UPDATE plex_queue SET attempts=?, last_error=?, "
                            "done_at=? WHERE file_id=?",
                            (attempts, f"gave up after {attempts}: {why}"[:400],
                             time.time(), row["file_id"]))
            sec.keep()
            sec.note(f"{name} — GAVE UP after {attempts} attempts: {why}{tail}. "
                     f"The file is correct; Plex will catch up on its own daily "
                     f"scan.", "error")
        else:
            delay = BACKOFF[min(attempts, len(BACKOFF) - 1)]
            with cursor() as cur:
                cur.execute("UPDATE plex_queue SET attempts=?, last_error=?, "
                            "next_try_at=? WHERE file_id=?",
                            (attempts, why[:400], time.time() + delay,
                             row["file_id"]))
            sec.keep()
            sec.note(f"{name} — {why}{tail} · attempt {attempts}/"
                     f"{MAX_ATTEMPTS}, trying again in "
                     f"{_pretty(delay)}", "warn")
    bits = []
    if corrected:
        bits.append(f"{corrected} corrected")
    if ok - corrected:
        bits.append(f"{ok - corrected} already right")
    if failed - gave_up:
        bits.append(f"{failed - gave_up} still waiting")
    if gave_up:
        bits.append(f"{gave_up} gave up")
    if held:
        bits.append(f"{len(held)} held for a rename")
    sec.result = ", ".join(bits) or "nothing to do"
    sec.__exit__(None, None, None)
    RUN_STATE.update(running=False, at=time.time(), now="",
                     done=len(rows), total=len(rows))
    return {"tried": len(rows), "ok": ok, "failed": failed,
            "gave_up": gave_up, "held": len(held), "corrected": corrected}


def _pretty(seconds: float) -> str:
    """"45 seconds" / "3 minutes" / "6 hours" - never "0 min"."""
    s = int(seconds)
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{round(s / 60)} minutes"
    return f"{round(s / 3600)} hours"


async def watch() -> None:
    init()
    await asyncio.sleep(60)
    while True:
        schedules.beat("plexqueue")
        try:
            await run_due()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"Plex queue loop error: {type(e).__name__}: {e}",
                       "error")
        await asyncio.sleep(POLL_S)


def stats() -> dict:
    r"""What the dashboard panel shows. Same shape as the rename queue's.

    WAITING IS NOT FAILING here either, and for a closer reason: most rows are
    delivered on the first look without Plex being asked to do anything at all,
    because its own scanner sometimes got there first. A row with one attempt
    against it is the happy path; only attempts>1 means something actually
    missed.
    """
    try:
        now = time.time()
        with cursor() as cur:
            pend = cur.execute("SELECT COUNT(*) n FROM plex_queue "
                               "WHERE done_at IS NULL").fetchone()["n"]
            due = cur.execute("SELECT COUNT(*) n FROM plex_queue "
                              "WHERE done_at IS NULL AND next_try_at<=?",
                              (now,)).fetchone()["n"]
            first = cur.execute("SELECT COUNT(*) n FROM plex_queue "
                                "WHERE done_at IS NULL AND attempts<=1"
                                ).fetchone()["n"]
            retry = cur.execute("SELECT COUNT(*) n FROM plex_queue "
                                "WHERE done_at IS NULL AND attempts>1"
                                ).fetchone()["n"]
            # Counted separately from both: a held row is not waiting on Plex
            # at all, it is waiting on the arr, and the panel should say which.
            holding = cur.execute(
                "SELECT COUNT(*) n FROM plex_queue q WHERE q.done_at IS NULL "
                "  AND EXISTS (SELECT 1 FROM rename_queue r "
                "              WHERE r.file_id = q.file_id "
                "                AND r.done_at IS NULL)").fetchone()["n"]
            gave = cur.execute(
                "SELECT COUNT(*) n FROM plex_queue WHERE done_at IS NOT NULL "
                "AND last_error LIKE 'gave up%'").fetchone()["n"]
            done = cur.execute(
                "SELECT COUNT(*) n FROM plex_queue WHERE done_at IS NOT NULL "
                "AND (last_error IS NULL OR last_error NOT LIKE 'gave up%')"
                ).fetchone()["n"]
            rows = [dict(r) for r in cur.execute(
                "SELECT q.file_id, q.path, q.why, q.attempts, q.last_error, "
                "       q.next_try_at, "
                "       EXISTS (SELECT 1 FROM rename_queue r "
                "               WHERE r.file_id = q.file_id "
                "                 AND r.done_at IS NULL) AS held "
                "  FROM plex_queue q WHERE q.done_at IS NULL "
                " ORDER BY q.next_try_at LIMIT 60")]
            stuck = [dict(r) for r in cur.execute(
                "SELECT file_id, path, why, attempts, last_error, done_at "
                "  FROM plex_queue WHERE done_at IS NOT NULL "
                "   AND last_error LIKE 'gave up%' "
                " ORDER BY done_at DESC LIMIT 20")]
            # WHEN THE NEXT ONE IS OWED, which is the question "22 waiting"
            # cannot answer. A row on its first pass is due in seconds; one
            # that has missed six times is due in six hours, and those two
            # states look the same in a count.
            nxt = cur.execute(
                "SELECT MIN(next_try_at) t FROM plex_queue "
                "WHERE done_at IS NULL").fetchone()["t"]
        return {"pending": pend, "due": due, "first_try": first,
                "retrying": retry, "gave_up": gave, "done": done,
                "holding": holding, "rows": rows, "stuck": stuck,
                "max_attempts": MAX_ATTEMPTS, "poll_s": POLL_S,
                "next_due_in": (max(0.0, (nxt or 0) - now) if nxt else None),
                "run": dict(RUN_STATE)}
    except Exception:                                        # noqa: BLE001
        return {"pending": 0, "due": 0, "first_try": 0, "retrying": 0,
                "gave_up": 0, "done": 0, "holding": 0, "rows": [],
                "stuck": [], "max_attempts": MAX_ATTEMPTS, "poll_s": POLL_S,
                "next_due_in": None, "run": dict(RUN_STATE)}
