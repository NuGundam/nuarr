r"""
nuarr - retry files that failed for a momentary reason

WHY
---
A Cardcaptor Sakura episode sat in the Errors tile for 24 DAYS. Its whole
failure was "database is locked" - SQLite telling us two writers collided for
a fraction of a second. Nothing was wrong with the file, the disk, the rules
or the arrs. It needed one more attempt, and instead it needed a human.

Two faults produced that, and both are fixed:

  * The error was MISREAD. refetch.classify() matched the bare word "locked"
    and reported "the file is locked or unreadable - free it and requeue" - a
    diagnosis about the media file, when the sentence was about the database.
    A remedy that could not work, offered confidently. See _TRANSIENT there.

  * Nothing ever tried again. Every other momentary fault in nuarr has a
    retry: renamequeue backs off and re-attempts, the commit queue waits for
    a lock to clear, the encoder probe retries once under load. A failed JOB
    was the one outcome treated as final on the first go.

WHAT THIS DOES
--------------
Watches for files left in `error` whose reason classifies as transient, and
requeues them on the SAME BACKOFF SHAPE the rename queue uses - three minutes,
ten, thirty, an hour, three hours, six - then gives up and says so. A fault
that is really momentary clears on the first or second attempt; one that keeps
coming back was never momentary, and after six tries it is reported as a real
error for a human, which is the honest outcome.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not touch `content` errors (the bytes are wrong - retrying reads the
same wrong bytes), `policy` errors (a lock, a full disk, a too-long path - the
condition has to change first), or `unknown` ones. Retrying an error we cannot
name is how a queue starts chewing the same file forever.
"""
from __future__ import annotations

import asyncio
import time

from .db import cursor
from . import joblog
from . import refetch

# Same ladder as renamequeue.BACKOFF, and for the same reason: it is long
# enough to outlast a busy period and short enough that a real fault surfaces
# the same day.
BACKOFF = [180, 600, 1800, 3600, 10800, 21600]
MAX_ATTEMPTS = len(BACKOFF)
POLL_S = 60.0

_TABLE = """
CREATE TABLE IF NOT EXISTS error_retry(
    file_id   INTEGER PRIMARY KEY,
    attempts  INTEGER NOT NULL DEFAULT 0,
    first_at  REAL,
    next_at   REAL,
    reason    TEXT,
    state     TEXT NOT NULL DEFAULT 'waiting'   -- waiting | gave_up
)
"""


def init() -> None:
    with cursor() as cur:
        cur.execute(_TABLE)
        cur.execute("CREATE INDEX IF NOT EXISTS ix_er_due "
                    "ON error_retry(state, next_at)")


def _transient(reason: str | None) -> tuple[bool, str]:
    kind, why = refetch.classify(reason)
    return kind == "transient", why


def state() -> dict:
    """What is waiting, what gave up - for the Errors panel."""
    try:
        with cursor() as cur:
            rows = [dict(r) for r in cur.execute(
                "SELECT file_id, attempts, next_at, reason, state "
                "  FROM error_retry")]
    except Exception:                                        # noqa: BLE001
        return {"waiting": [], "gave_up": []}
    now = time.time()
    return {
        "waiting": [r | {"in_s": max(0, (r["next_at"] or 0) - now)}
                    for r in rows if r["state"] == "waiting"],
        "gave_up": [r for r in rows if r["state"] == "gave_up"],
    }


def note_failure(file_id: int, reason: str | None) -> bool:
    """Called when a file lands in `error`. True if a retry was scheduled."""
    ok, _why = _transient(reason)
    if not ok:
        return False
    now = time.time()
    with cursor() as cur:
        cur.execute("SELECT attempts, state FROM error_retry WHERE file_id=?",
                    (file_id,))
        row = cur.fetchone()
        if row and row["state"] == "gave_up":
            return False                     # already exhausted; leave it be
        attempts = (row["attempts"] if row else 0) or 0
        if attempts >= MAX_ATTEMPTS:
            return False
        cur.execute(
            "INSERT INTO error_retry(file_id,attempts,first_at,next_at,reason,"
            "                        state) VALUES(?,?,?,?,?,'waiting') "
            "ON CONFLICT(file_id) DO UPDATE SET next_at=excluded.next_at, "
            "  reason=excluded.reason, state='waiting'",
            (file_id, attempts, now, now + BACKOFF[min(attempts,
                                                       len(BACKOFF) - 1)],
             (reason or "")[:400]))
    return True


def forget(file_id: int) -> None:
    """A file that succeeded (or was requeued by hand) starts clean."""
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM error_retry WHERE file_id=?", (file_id,))
    except Exception:                                        # noqa: BLE001
        pass


async def _run_due(limit: int = 20) -> int:
    from . import jobs

    now = time.time()
    with cursor() as cur:
        due = [dict(r) for r in cur.execute(
            "SELECT e.file_id, e.attempts, e.reason, f.path, f.title, f.state "
            "  FROM error_retry e JOIN files f ON f.id = e.file_id "
            " WHERE e.state='waiting' AND COALESCE(e.next_at,0) <= ? "
            " LIMIT ?", (now, limit))]
    done = 0
    for r in due:
        fid = r["file_id"]
        # It fixed itself (a later pass succeeded), or somebody dealt with it.
        if r["state"] != "error":
            forget(fid)
            continue
        attempts = (r["attempts"] or 0) + 1
        if attempts > MAX_ATTEMPTS:
            with cursor() as cur:
                cur.execute("UPDATE error_retry SET state='gave_up' "
                            " WHERE file_id=?", (fid,))
            joblog.log(f"gave up retrying {r['title'] or r['path']} after "
                       f"{MAX_ATTEMPTS} attempts - it is a real error now, "
                       f"not a momentary one", "warn")
            continue
        with cursor() as cur:
            cur.execute(
                "UPDATE error_retry SET attempts=?, next_at=? WHERE file_id=?",
                (attempts, now + BACKOFF[min(attempts, len(BACKOFF) - 1)], fid))
            cur.execute("UPDATE files SET state='eligible', state_reason=? "
                        "WHERE id=?",
                        (f"retry {attempts}/{MAX_ATTEMPTS} after a momentary "
                         f"failure: {(r['reason'] or '')[:120]}", fid))
        try:
            await jobs.enqueue(fid, r["path"], r["title"] or "",
                               source="error-retry")
            done += 1
            joblog.log(f"retrying {r['title'] or r['path']} "
                       f"(attempt {attempts}/{MAX_ATTEMPTS}) - previous "
                       f"failure looked momentary", "info")
        except jobs.NothingToDo:
            forget(fid)                       # re-planned clean; nothing to do
        except ValueError:
            pass                              # already queued or running
        except Exception as e:                                # noqa: BLE001
            joblog.log(f"error-retry enqueue failed for {r['path']}: "
                       f"{type(e).__name__}: {e}", "debug")
    return done


def adopt_existing() -> int:
    """Pick up files already sitting in `error` from before this existed.

    The 24-day-old one is exactly this case: it failed, was misclassified, and
    no retry row was ever written for it.
    """
    try:
        with cursor() as cur:
            rows = [dict(r) for r in cur.execute(
                "SELECT id, state_reason FROM files WHERE state='error' "
                "  AND id NOT IN (SELECT file_id FROM error_retry)")]
    except Exception:                                        # noqa: BLE001
        return 0
    n = 0
    for r in rows:
        if note_failure(r["id"], r.get("state_reason")):
            n += 1
    return n


def adopt_not_landed() -> int:
    r"""Schedule retries for files stuck on a FAILED OR BLOCKED job.

    THE GAP THIS CLOSES. adopt_existing() looks at files.state='error', but a
    job that is blocked writes state='blocked' and a job that fails may leave
    the file untouched entirely - so thirteen files sat stuck for weeks with no
    retry row between them, waiting for somebody to notice and press a button.
    The renamer never had this problem because its queue retries on its own.

    RETRYABILITY IS ASKED FRESH, not read off the old message. pipeline
    re-tests conditions that settings can change - nine of those thirteen were
    blocked under a path-length limit that has since moved, so they are
    retryable now and were not when the message was written. Deciding from the
    stored text would keep them out of the queue forever.
    """
    try:
        from . import pipeline
        rows = pipeline.not_landed()
    except Exception:                                        # noqa: BLE001
        return 0
    if not rows:
        return 0
    try:
        with cursor() as cur:
            known = {r[0] for r in cur.execute(
                "SELECT file_id FROM error_retry")}
    except Exception:                                        # noqa: BLE001
        known = set()
    n = 0
    now = time.time()
    for r in rows:
        if r["id"] in known or not r.get("retryable"):
            continue
        # Straight into the ladder rather than through note_failure(), which
        # would ask refetch.classify() and get "not transient" for a stale
        # path-length block - the very rows this exists to rescue.
        try:
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO error_retry(file_id, attempts, first_at, "
                    "next_at, reason, state) VALUES(?,?,?,?,?,'waiting') "
                    "ON CONFLICT(file_id) DO NOTHING",
                    (r["id"], 0, now, now + BACKOFF[0],
                     (r.get("error") or "")[:300]))
            n += 1
        except Exception as e:                               # noqa: BLE001
            # NOT SILENT. The first version wrote last_at, which is not a
            # column here, so every insert raised and was swallowed by a bare
            # continue - the sweep reported "0 scheduled" and looked like it
            # had simply found nothing to do. A scheduler that cannot schedule
            # has to say so.
            joblog.log(f"could not put file {r['id']} on the retry ladder: "
                       f"{type(e).__name__}: {e}", "warn")
            continue
    return n


async def watch() -> None:
    """Adopt what is already stuck, then retry what comes due."""
    await asyncio.sleep(45)
    try:
        init()
        n = await asyncio.to_thread(adopt_existing)
        if n:
            joblog.log(f"{n} file(s) that failed for a momentary reason are "
                       f"queued to retry rather than waiting for a human",
                       "info")
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"error-retry init: {type(e).__name__}: {e}", "error")
    # RE-SWEEP, not once at boot. A file becomes retryable long after startup:
    # a job fails at three in the morning, or a setting changes and unblocks
    # nine files that were parked. Adopting only at boot means the queue is
    # correct exactly once and drifts from then on.
    last_sweep = 0.0
    while True:
        try:
            await _run_due()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"error-retry: {type(e).__name__}: {e}", "debug")
        if time.time() - last_sweep > 900:
            last_sweep = time.time()
            try:
                n = await asyncio.to_thread(adopt_not_landed)
                if n:
                    joblog.log(f"{n} file(s) stuck on a failed or blocked job "
                               f"are now on the retry ladder", "info")
            except Exception as e:                           # noqa: BLE001
                joblog.log(f"error-retry sweep: {type(e).__name__}: {e}",
                           "debug")
        await asyncio.sleep(POLL_S)
