r"""
nuarr - durable commit retry queue

WHY THIS EXISTS
---------------
Renames already survive failure: a blocked rename goes into rename_queue and is
retried with backoff until it works. The COMMIT - swapping the freshly encoded
file in for the original - had no such safety net, and it is the expensive one.

The old behaviour on a failed commit was:

    if not res.ok:
        _rm(out)                      # <- throws away the encode
        raise RuntimeError(...)       # <- job marked failed

fileops.safe_replace() already retries internally (5 attempts, 3 s backoff,
600 s lock wait), so it survives a brief lock. What it cannot survive is a LONG
one: someone watching the file in Plex for an hour, a backup job holding it, an
antivirus scan. Those exhaust the internal retries, and then an hour of NVENC
time is deleted and the file is queued to be encoded again from scratch - which
will hit the same lock.

So: keep the output in the cache, record the pending commit, and retry on a
timer. The encode is done; only the swap is outstanding. State lives in SQLite
so a restart resumes rather than forgetting - and startup cache purging must
skip anything referenced here, or it would delete the very file we preserved.
"""
from __future__ import annotations

import asyncio
import os
import time

from . import fileops, joblog
from .db import cursor
from . import schedules

# Longer than the rename backoff: a lock that beat safe_replace's own 600 s
# wait is usually a person watching something, so the useful retry window is
# minutes-to-hours, not seconds.
BACKOFF = [300, 900, 1800, 3600, 7200, 21600]
MAX_ATTEMPTS = len(BACKOFF)
POLL_S = 120.0


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS commit_queue (
                job_id      TEXT PRIMARY KEY,
                file_id     INTEGER,
                target      TEXT NOT NULL,
                cache_path  TEXT NOT NULL,
                size_before INTEGER,
                size_after  INTEGER,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT,
                next_try_at REAL NOT NULL,
                created_at  REAL NOT NULL,
                done_at     REAL
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_cq_due "
                    "ON commit_queue(done_at, next_try_at)")


def pending_cache_paths() -> set[str]:
    """Cache files that must NOT be purged - a commit still needs them."""
    try:
        with cursor() as cur:
            return {os.path.normcase(r["cache_path"]) for r in cur.execute(
                "SELECT cache_path FROM commit_queue WHERE done_at IS NULL")}
    except Exception:
        return set()


def enqueue(job_id: str, file_id: int, target: str, cache_path: str,
            size_before: int, size_after: int, why: str,
            retry_now: bool = False) -> None:
    """Hold a finished encode until its swap can happen.

    `retry_now` skips the initial back-off. The default 5 minutes exists
    because the usual reason a commit defers is a LOCK - Plex streaming the
    file, a backup, an AV scan - and hammering a locked file achieves nothing.
    A commit interrupted by a RESTART is not locked; nothing is holding it, and
    waiting five minutes before the first attempt just leaves tens of GB
    sitting in the cache for no reason. Measured: a 46.7 GB output waited 5
    minutes to start a swap that then took 5 minutes to run.
    """
    now = time.time()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO commit_queue(job_id,file_id,target,cache_path,"
            "size_before,size_after,attempts,last_error,next_try_at,created_at,"
            "done_at) VALUES(?,?,?,?,?,?,0,?,?,?,NULL) "
            "ON CONFLICT(job_id) DO UPDATE SET attempts=0, last_error=excluded.last_error, "
            "next_try_at=excluded.next_try_at, done_at=NULL",
            (job_id, file_id, target, cache_path, size_before, size_after,
             why[:400], now if retry_now else now + BACKOFF[0], now))
    joblog.log(f"commit deferred, output KEPT for retry: "
               f"{os.path.basename(target)} — {why}", "warn", job_id)


async def _attempt(row: dict) -> tuple[bool, str]:
    target, cache = row["target"], row["cache_path"]
    if not os.path.exists(cache):
        # Nothing to commit any more. Do not fail forever over it.
        return True, "cache output is gone - nothing left to commit"
    if not os.path.exists(target):
        # The original vanished (deleted, or an arr replaced it). Committing
        # would resurrect a file the library no longer expects.
        return True, "target no longer exists - dropping the pending commit"
    # SAME PATH, DIFFERENT FILE. A pending commit can wait hours behind a
    # lock - long enough for an arr upgrade to land a new release at this
    # exact path. The vanished-target check above cannot see that, and
    # replacing would overwrite the upgrade with a transcode of the file it
    # superseded. size_before is the source's size when the encode read it;
    # a target that no longer matches is no longer the file this output was
    # made from.
    try:
        if row.get("size_before") and \
                os.path.getsize(target) != row["size_before"]:
            try:
                os.remove(cache)          # made from bytes that are gone
            except OSError:
                pass
            return True, ("target was replaced since the encode (arr "
                          "upgrade) - dropping the pending commit")
    except OSError:
        pass
    who = await asyncio.to_thread(fileops.who_locks, target)
    if who:
        return False, "still in use by " + ", ".join(who)
    res = await asyncio.to_thread(fileops.safe_replace, target, cache)
    if not res.ok:
        return False, res.detail
    return True, res.detail


async def run_due(limit: int = 10) -> dict:
    now = time.time()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT * FROM commit_queue WHERE done_at IS NULL AND next_try_at<=? "
            "ORDER BY next_try_at LIMIT ?", (now, limit))]
    if not rows:
        return {"tried": 0, "ok": 0, "failed": 0, "gave_up": 0}

    ok = failed = gave_up = 0
    sec = joblog.section("file replaces")
    sec.__enter__(); sec.keep()
    sec.note(f"{len(rows)} deferred commit(s) due")
    for row in rows:
        good, detail = await _attempt(row)
        if good:
            ok += 1
            with cursor() as cur:
                cur.execute("UPDATE commit_queue SET done_at=?, last_error=NULL "
                            "WHERE job_id=?", (time.time(), row["job_id"]))
                cur.execute("UPDATE jobs SET state='done', error=NULL, "
                            "size_before=?, size_after=? WHERE job_id=?",
                            (row["size_before"], row["size_after"], row["job_id"]))
            try:
                if os.path.exists(row["cache_path"]):
                    os.remove(row["cache_path"])
            except OSError:
                pass
            joblog.log(f"commit retry OK: {os.path.basename(row['target'])} "
                       f"— {detail}", "ok", row["job_id"])
            # PLEX TOO, FOR THE SAME REASON. A deferred commit is a file
            # change like any other - it just happened minutes or hours after
            # the encode, because the file was locked at the time. It never
            # went through _post_commit(), so nothing here had ever told Plex.
            try:
                from . import plexqueue
                plexqueue.enqueue(row["file_id"], row["target"],
                                  why="a deferred commit landed")
            except Exception:                                # noqa: BLE001
                pass
            # the file changed, so the arr still needs telling
            try:
                from . import renamequeue
                with cursor() as cur:
                    f = cur.execute("SELECT arr_name, arr_parent_id FROM files "
                                    "WHERE id=?", (row["file_id"],)).fetchone()
                if f and f["arr_name"]:
                    renamequeue.enqueue(row["file_id"], f["arr_name"],
                                        f["arr_parent_id"], row["target"],
                                        "after deferred commit")
            except Exception:
                pass
            continue

        attempts = row["attempts"] + 1
        failed += 1
        if attempts >= MAX_ATTEMPTS:
            gave_up += 1
            with cursor() as cur:
                cur.execute("UPDATE commit_queue SET attempts=?, last_error=?, "
                            "done_at=? WHERE job_id=?",
                            (attempts, f"gave up after {attempts}: {detail}"[:400],
                             time.time(), row["job_id"]))
            try:
                if os.path.exists(row["cache_path"]):
                    os.remove(row["cache_path"])
            except OSError:
                pass
            joblog.log(f"commit retry GAVE UP after {attempts}: "
                       f"{os.path.basename(row['target'])} — {detail}. "
                       f"The original is untouched; the encode was discarded.",
                       "error", row["job_id"])
        else:
            delay = BACKOFF[min(attempts, len(BACKOFF) - 1)]
            with cursor() as cur:
                cur.execute("UPDATE commit_queue SET attempts=?, last_error=?, "
                            "next_try_at=? WHERE job_id=?",
                            (attempts, detail[:400], time.time() + delay,
                             row["job_id"]))
            joblog.log(f"commit retry {attempts}/{MAX_ATTEMPTS} failed, next in "
                       f"{delay//60} min: {os.path.basename(row['target'])} "
                       f"— {detail}", "warn", row["job_id"])
    sec.result = f"{ok} swapped in, {failed} retrying, {gave_up} gave up"
    sec.__exit__(None, None, None)
    return {"tried": len(rows), "ok": ok, "failed": failed, "gave_up": gave_up}


async def watch() -> None:
    init()
    # 20s, not 60. A restart is exactly when this queue is most likely to have
    # something urgent in it - recover_interrupted() puts every interrupted
    # commit here during boot - and those hold the finished encode in the cache
    # until they run. Long enough to let the pool walk and the first scan
    # settle, short enough that a 46 GB output is not parked for a minute
    # waiting for a swap that has nothing blocking it.
    await asyncio.sleep(20)
    while True:
        schedules.beat('commitqueue')
        try:
            await run_due()
        except Exception as e:
            joblog.log(f"commit retry loop error: {type(e).__name__}: {e}", "error")
        from . import workers
        await asyncio.sleep(workers.tune("commit_retry_s"))


def stats() -> dict:
    with cursor() as cur:
        pend = cur.execute("SELECT COUNT(*) n FROM commit_queue "
                           "WHERE done_at IS NULL").fetchone()["n"]
        gave = cur.execute("SELECT COUNT(*) n FROM commit_queue WHERE done_at "
                           "IS NOT NULL AND last_error LIKE 'gave up%'").fetchone()["n"]
        held = cur.execute("SELECT COALESCE(SUM(size_after),0) b FROM commit_queue "
                           "WHERE done_at IS NULL").fetchone()["b"]
        rows = [dict(r) for r in cur.execute(
            "SELECT job_id,target,attempts,last_error,next_try_at FROM commit_queue "
            "WHERE done_at IS NULL ORDER BY next_try_at LIMIT 50")]
    return {"pending": pend, "gave_up": gave, "cache_held_bytes": held,
            "rows": rows}
