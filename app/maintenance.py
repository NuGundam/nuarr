r"""
nuarr - database housekeeping

WHY THIS EXISTS
---------------
Nothing in nuarr ever gave the database back. Every mechanism here addresses a
number that was measured on the live system, not a hypothetical:

  * the WAL had grown to 93.7 MB against a 4 MB autocheckpoint threshold
  * history held 35,741 rows and jobs 22,735, neither ever pruned
  * probe_json was 70.3 MB - 52% of a 135 MB database - and is NEVER READ

THE WAL, AND WHY THE AUTOMATIC CHECKPOINT WAS NOT ENOUGH
--------------------------------------------------------
SQLite's autocheckpoint (1000 pages, ~4 MB) is PASSIVE: it copies committed
pages back into the main database, but it does NOT shrink the -wal file. The
file only shrinks on a TRUNCATE or RESTART checkpoint, and only when no reader
holds an older snapshot.

So the WAL was being checkpointed correctly and still grew without bound - a
PASSIVE checkpoint run against a 93 MB file happily reported success while
leaving all 93 MB on disk. It costs a longer crash recovery, a bigger -shm
index, and every reader walking a larger WAL index to find recent pages.

The dashboard polls several endpoints every 1-3 s across long-lived per-thread
connections, so there is rarely a moment with no reader at all. TRUNCATE is
therefore attempted on a timer and allowed to fail quietly: a checkpoint that
cannot run right now is not an error, it just runs on the next pass.

RETENTION
---------
Old history and finished jobs are evidence, not state - nothing reads them back
into the running system. They are kept long enough to answer "what happened to
this file last month" and no longer.

Deletes are CHUNKED. A single unbounded DELETE takes a write lock for the whole
statement, and on this database that is long enough for the job pump and the
dashboard to pile up behind it.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time

from . import joblog
from .config import DB_PATH, SETTINGS
from . import schedules
from .db import cursor, kv_get, kv_set

# How long evidence is kept. Generous on purpose: the point is to stop unbounded
# growth, not to run a tight ship.
#
# THESE NUMBERS WERE CHECKED AGAINST WHAT THEY ACTUALLY COST, and the answer was
# that two of the three barely matter. Measured payload on a 39,447-file library:
#
#     file_probes   185.8 MB   73%    <- the whole story
#     jobs           38.3 MB   15%
#     files          13.9 MB    5%
#     history        12.0 MB    5%
#     rename_queue    3.7 MB    1%
#
# So HISTORY_DAYS is not a space control and shrinking it would reclaim single-
# digit MB. It is left wide because history is the audit trail and cheap.
#
# PROBE_DAYS is stranger: it never fires at all. file_probes holds exactly one
# row per file and `at` is refreshed on every probe, so nothing ages out - zero
# rows were older than SEVEN days when this was measured, against a 30-day
# window. That is not a leak. It is a working set: 4.7 KB of ffprobe JSON per
# file, and the file detail view reads it (web.py serves from p.json). Deleting
# it on a timer would only force it to be rebuilt. The window is kept as a
# backstop for probes belonging to files that have gone away.
#
# The real unbounded growth was never in the database - it was the job log
# FILES on disk, which nothing here used to touch. See LOG_DAYS below.
HISTORY_DAYS = 90
JOBS_DAYS = 90
PROBE_DAYS = 30          # backstop only; see above - in practice this never fires

# Completed rename rows. Nothing reads them: the queue only ever selects
# `done_at IS NULL`, and the single backward-looking query is a count of rows
# that gave up. Measured at 15,310 completed, 0 pending, 0 gave up - the entire
# table was finished work being carried forever. Gave-up rows are kept longer
# because they are the ones a person might actually come back to.
RENAME_DONE_DAYS = 14
RENAME_GAVEUP_DAYS = 90

# Job log files on disk. THIS is what had no bound at all: 50,955 files at
# roughly 10,000/day, oldest five days old, and prune() only ever covered
# database tables. Enumerating that directory already cost 1.8 s; left alone it
# reaches ~900,000 files by the time the 90-day windows above first fire.
LOG_DAYS = 21
# nuarr.log had no rotation either and had reached 139.6 MB, having roughly
# doubled in a single working session.
LOG_MAX_MB = 32
LOG_KEEP = 3

# Rows per DELETE. Small enough that the write lock is never held long enough to
# stall the pump, large enough that a big backlog still clears in one pass.
CHUNK = 2000

# Every 6 hours. This work is measured in milliseconds; the interval is about
# not thrashing the disk, not about keeping up.
POLL_S = 6 * 3600

STATS: dict = {"last_run": 0.0, "wal_mb": 0.0, "history_deleted": 0,
               "jobs_deleted": 0, "probes_deleted": 0, "last_error": ""}


def wal_mb() -> float:
    try:
        return os.path.getsize(str(DB_PATH) + "-wal") / 1048576
    except OSError:
        return 0.0


def checkpoint(mode: str = "TRUNCATE") -> dict:
    """Fold the WAL back into the database and shrink the file.

    Returns SQLite's own three-value answer: busy, wal pages, pages moved.
    busy=1 means a reader held an older snapshot and the file was left alone -
    normal, not a failure, and the next pass will get it.
    """
    before = wal_mb()
    try:
        with cursor() as cur:
            row = cur.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        busy, pages, moved = (row[0], row[1], row[2]) if row else (1, -1, -1)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "before_mb": round(before, 1)}
    after = wal_mb()
    STATS["wal_mb"] = round(after, 1)
    return {"ok": True, "busy": busy, "pages": pages, "moved": moved,
            "before_mb": round(before, 1), "after_mb": round(after, 1),
            "freed_mb": round(before - after, 1)}


def _delete_chunked(cur, sql: str, args: tuple) -> int:
    """Run a bounded DELETE repeatedly until it stops matching rows."""
    total = 0
    while True:
        cur.execute(sql, args)
        n = cur.rowcount or 0
        total += n
        if n < CHUNK:
            return total


def prune(dry_run: bool = False) -> dict:
    """Drop evidence older than the retention windows."""
    now = time.time()
    h_cut = now - HISTORY_DAYS * 86400
    j_cut = now - JOBS_DAYS * 86400
    p_cut = now - PROBE_DAYS * 86400
    rd_cut = now - RENAME_DONE_DAYS * 86400
    rg_cut = now - RENAME_GAVEUP_DAYS * 86400
    out: dict = {"dry_run": dry_run}

    with cursor() as cur:
        out["history_matched"] = cur.execute(
            "SELECT COUNT(*) c FROM history WHERE at < ?", (h_cut,)).fetchone()["c"]
        # Only FINISHED jobs. A queued or running job is live state no matter
        # how old its created_at is - a job that has been sitting in the queue
        # for four months is exactly the one you must not silently delete.
        out["jobs_matched"] = cur.execute(
            "SELECT COUNT(*) c FROM jobs WHERE state IN "
            "('done','failed','cancelled','skipped') AND "
            "COALESCE(finished_at, created_at) < ?", (j_cut,)).fetchone()["c"]
        try:
            out["probes_matched"] = cur.execute(
                "SELECT COUNT(*) c FROM file_probes WHERE at < ?",
                (p_cut,)).fetchone()["c"]
        except Exception:
            out["probes_matched"] = 0
        # Completed renames, split by outcome. A row that gave up is the only
        # kind anyone looks back at, so it outlives the ordinary ones.
        try:
            out["renames_matched"] = cur.execute(
                "SELECT COUNT(*) c FROM rename_queue WHERE done_at IS NOT NULL "
                "AND ((COALESCE(last_error,'') NOT LIKE 'gave up%' AND done_at < ?) "
                "  OR (COALESCE(last_error,'') LIKE 'gave up%' AND done_at < ?))",
                (rd_cut, rg_cut)).fetchone()["c"]
        except Exception:
            out["renames_matched"] = 0

        if dry_run:
            return out

        out["history_deleted"] = _delete_chunked(
            cur, "DELETE FROM history WHERE id IN "
                 f"(SELECT id FROM history WHERE at < ? LIMIT {CHUNK})", (h_cut,))
        out["jobs_deleted"] = _delete_chunked(
            cur, "DELETE FROM jobs WHERE id IN "
                 "(SELECT id FROM jobs WHERE state IN "
                 "('done','failed','cancelled','skipped') AND "
                 f"COALESCE(finished_at, created_at) < ? LIMIT {CHUNK})", (j_cut,))
        try:
            out["probes_deleted"] = _delete_chunked(
                cur, "DELETE FROM file_probes WHERE file_id IN "
                     f"(SELECT file_id FROM file_probes WHERE at < ? LIMIT {CHUNK})",
                (p_cut,))
        except Exception:
            out["probes_deleted"] = 0
        try:
            out["renames_deleted"] = _delete_chunked(
                cur, "DELETE FROM rename_queue WHERE rowid IN "
                     "(SELECT rowid FROM rename_queue WHERE done_at IS NOT NULL "
                     " AND ((COALESCE(last_error,'') NOT LIKE 'gave up%' "
                     "       AND done_at < ?) "
                     "   OR (COALESCE(last_error,'') LIKE 'gave up%' "
                     "       AND done_at < ?)) "
                     f" LIMIT {CHUNK})", (rd_cut, rg_cut))
        except Exception:
            out["renames_deleted"] = 0
        # SHAPE VERDICTS OUTLIVE THEIR FILES. sub_shape has no foreign key -
        # it is written by the OCR check against a file id and nothing removes
        # a row when that file leaves the library. forget_shapes() covers the
        # case that matters (the file was REWRITTEN, so the track numbers
        # slid), and pruning file_probes above quietly created a second one:
        # a verdict whose probe is gone can never be re-checked or re-used.
        #
        # None outstanding when this was written - the check is young - which
        # is the moment to add the sweep rather than the moment it is 40,000
        # rows deep.
        try:
            out["shapes_deleted"] = _delete_chunked(
                cur, "DELETE FROM sub_shape WHERE file_id IN "
                     "(SELECT s.file_id FROM sub_shape s "
                     "   LEFT JOIN files f ON f.id = s.file_id "
                     "  WHERE f.id IS NULL OR f.state IN ('deleted','duplicate') "
                     f" LIMIT {CHUNK})", ())
        except Exception:
            out["shapes_deleted"] = 0

    STATS.update(history_deleted=out.get("history_deleted", 0),
                 jobs_deleted=out.get("jobs_deleted", 0),
                 probes_deleted=out.get("probes_deleted", 0),
                 renames_deleted=out.get("renames_deleted", 0))
    return out


SUBOCR_WORK_HOURS = 6


def prune_subocr_work(dry_run: bool = False) -> dict:
    r"""Remove abandoned subocr_* and shape_* scratch directories from the cache.

    THE THIRD PLACE THINGS ACCUMULATE. run_one() creates a temp dir per job for
    the .sup dumps, the .srt output and (on the mux path) the whole rebuilt
    out.mkv. It deletes that dir itself on failure, and the caller deletes it
    after a SUCCESSFUL commit - but every other exit leaks it: a deferred
    commit, a cancelled job, a restart mid-OCR. Nothing ever came back for
    those.

    Measured before writing this: 86 directories, 116.6 GB, the oldest 25
    hours old, several holding a full 12-18 GB remux. The cache had 699 GB
    free so it never announced itself - it would have, eventually, as a job
    failing for want of space with no obvious cause.

    Age is the only safe test available here. A directory younger than
    SUBOCR_WORK_HOURS is left alone whatever it looks like, because it may
    belong to a running OCR (Kizumonogatari-class tracks run over an hour) or
    to a deferred commit whose out.mkv the commit queue still intends to use.
    Anything older than that has outlived every path that would legitimately
    still want it: the 3600s OCR timeout has fired, and commitqueue retries
    long before six hours pass.
    """
    root = getattr(SETTINGS, "cache_dir", "") or ""
    out = {"dirs_removed": 0, "mb_freed": 0.0, "dirs_kept": 0}
    if not root or not os.path.isdir(root):
        return out
    cutoff = time.time() - SUBOCR_WORK_HOURS * 3600
    # Age is a heuristic; this is not. A deferred commit parks the finished
    # out.mkv - which lives INSIDE the work dir - in the commit queue and comes
    # back for it later. Deleting one of those would destroy a completed
    # encode, so ask the queue directly rather than trusting the clock. (No row
    # pointed into a work dir when this was written; the point is that it
    # cannot start to without this noticing.)
    held: set[str] = set()
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT cache_path FROM commit_queue "
                                 "WHERE done_at IS NULL"):
                p = str(r["cache_path"] or "")
                if p:
                    held.add(os.path.normcase(os.path.dirname(p)))
    except Exception:
        # Cannot confirm what is held -> do not delete anything this pass.
        return out
    freed = 0
    for name in os.listdir(root):
        # shape_ AS WELL AS subocr_. measure_file() creates its own scratch dir
        # for the .sup it reads, deletes it in a finally - and a finally does
        # not run when the process is stopped mid-read, which on this machine
        # is every restart during a measuring pass. Found 52 of them holding
        # 345 MB of .sup, none younger than the cutoff, none known to anything.
        # Same class of directory, same owner, same rules: this sweep already
        # had the reasoning, it was only looking for one prefix.
        if not (name.startswith("subocr_") or name.startswith("shape_")):
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        if os.path.normcase(d) in held:
            out["dirs_kept"] += 1
            continue
        try:
            # mtime, not ctime: a long OCR keeps writing into its own directory,
            # so an hours-old dir that is still being fed is still alive.
            newest = os.path.getmtime(d)
            for base, _dirs, files in os.walk(d):
                for f in files:
                    try:
                        newest = max(newest,
                                     os.path.getmtime(os.path.join(base, f)))
                    except OSError:
                        pass
            if newest > cutoff:
                out["dirs_kept"] += 1
                continue
            size = 0
            for base, _dirs, files in os.walk(d):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(base, f))
                    except OSError:
                        pass
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
            freed += size
            out["dirs_removed"] += 1
        except Exception:
            continue                # one bad directory must not stop the sweep
    out["mb_freed"] = round(freed / 2 ** 20, 1)
    return out


def prune_logs(dry_run: bool = False) -> dict:
    """Age out job log files and rotate nuarr.log.

    THE PART prune() NEVER COVERED. Everything above operates on database
    tables; the job logs are one file per job on disk, written at roughly
    10,000 a day and deleted by nothing. 50,955 of them had accumulated in five
    days, and enumerating the directory already cost 1.8 seconds - a cost paid
    by anything that walks it, including backups and antivirus, not just nuarr.

    Deleting by mtime rather than by asking the database which jobs still
    exist: the log is worth keeping exactly as long as somebody might open it,
    which is a question about age, not about whether a row survives. It also
    means this still works if the two ever disagree.
    """
    from .joblog import JOB_LOG_DIR, MAIN_LOG

    now = time.time()
    cut = now - LOG_DAYS * 86400
    out: dict = {"dry_run": dry_run, "logs_matched": 0, "logs_deleted": 0,
                 "logs_mb_freed": 0.0, "rotated": ""}
    try:
        with os.scandir(JOB_LOG_DIR) as it:
            for e in it:
                if not e.name.endswith(".log"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if st.st_mtime >= cut:
                    continue
                out["logs_matched"] += 1
                if dry_run:
                    out["logs_mb_freed"] += st.st_size / 1048576
                    continue
                try:
                    os.remove(e.path)
                    out["logs_deleted"] += 1
                    out["logs_mb_freed"] += st.st_size / 1048576
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    out["logs_mb_freed"] = round(out["logs_mb_freed"], 1)

    # nuarr.log: rotate rather than truncate, so the lines that explain a
    # problem are still there after the rotation that the problem caused.
    #
    # The writer thread opens this file per batch and closes it again, so it
    # holds no persistent handle - but it can be mid-write when we arrive, and
    # on Windows os.replace on an open file raises PermissionError rather than
    # waiting. Hence the retries. If all of them lose the race the rotation is
    # simply skipped and the next sweep takes it; a log that is 6 hours too
    # long is not worth failing housekeeping over.
    try:
        p = str(MAIN_LOG)
        if os.path.getsize(p) / 1048576 >= LOG_MAX_MB:
            if not dry_run:
                oldest = f"{p}.{LOG_KEEP}"
                if os.path.exists(oldest):
                    os.remove(oldest)
                for i in range(LOG_KEEP - 1, 0, -1):
                    src, dst = f"{p}.{i}", f"{p}.{i + 1}"
                    if os.path.exists(src):
                        os.replace(src, dst)
                for attempt in range(4):
                    try:
                        os.replace(p, f"{p}.1")
                        out["rotated"] = p
                        break
                    except OSError:
                        if attempt == 3:
                            out["rotate_skipped"] = "file was busy"
                        else:
                            time.sleep(0.25)
            else:
                out["rotated"] = p
    except OSError:
        pass
    return out


def vacuum() -> dict:
    """Rewrite the database to reclaim freed pages.

    Deliberately NOT part of the periodic sweep. VACUUM takes an exclusive lock
    and rewrites the whole file - fine as a one-off after a big prune, wrong to
    run on a timer underneath a live job queue.

    TWO THINGS BOTH HAVE TO BE TRUE or this silently reclaims nothing, and
    both were observed here on a file holding 124.8 MB of real pages in 209 MB
    of disk:

    1. In WAL mode VACUUM writes the rewritten database THROUGH THE WAL, so on
       return the main file is still its old size and the -wal has ballooned to
       hold the entire rewrite. A TRUNCATE checkpoint has to follow.

    2. SQLite cannot shrink a file that is MEMORY-MAPPED, and every connection
       here maps 128 MB (see db._CONN_PRAGMAS). Running VACUUM on a mapped
       connection reported success, left freelist_count at 0, and changed the
       file size by exactly nothing. It has to run on a connection with
       mmap_size=0.

    So this uses its OWN connection rather than the shared thread-local one -
    the pooled connection cannot have its mmap turned off without disturbing
    every other user of it.
    """
    import sqlite3

    before = os.path.getsize(DB_PATH) / 1048576
    conn = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None)
    try:
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("VACUUM")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        ck = {"busy": row[0], "pages": row[1], "moved": row[2]} if row else {}
    finally:
        conn.close()
    after = os.path.getsize(DB_PATH) / 1048576
    STATS["wal_mb"] = round(wal_mb(), 1)
    return {"before_mb": round(before, 1), "after_mb": round(after, 1),
            "freed_mb": round(before - after, 1), "checkpoint": ck}


def sweep() -> dict:
    """One housekeeping pass: prune rows, prune log files, shrink the WAL."""
    res: dict = {}
    with joblog.section("housekeeping") as sec:
        try:
            p = prune()
            deleted = (p.get("history_deleted", 0) + p.get("jobs_deleted", 0)
                       + p.get("probes_deleted", 0) + p.get("renames_deleted", 0))
            if deleted:
                sec.keep()
                sec.note(f"pruned {p.get('history_deleted',0):,} history, "
                         f"{p.get('jobs_deleted',0):,} finished jobs, "
                         f"{p.get('probes_deleted',0):,} stored probes, "
                         f"{p.get('renames_deleted',0):,} completed renames", "ok")
            res["prune"] = p

            # Files on disk, not rows. Kept in the same sweep because it is the
            # same job - bounding the evidence - and because a separate timer
            # would be one more thing that can quietly stop running.
            lg = prune_logs()
            res["logs"] = lg
            if lg.get("logs_deleted"):
                sec.keep()
                sec.note(f"removed {lg['logs_deleted']:,} job logs older than "
                         f"{LOG_DAYS} days ({lg['logs_mb_freed']} MB)", "ok")
            sw = prune_subocr_work()
            res["subocr_work"] = sw
            if sw.get("dirs_removed"):
                sec.keep()
                sec.note(f"removed {sw['dirs_removed']} abandoned OCR work "
                         f"directories ({sw['mb_freed']:,.0f} MB)", "ok")

            if lg.get("rotated"):
                sec.keep()
                sec.note(f"rotated nuarr.log at {LOG_MAX_MB} MB", "ok")
            elif lg.get("rotate_skipped"):
                sec.note("nuarr.log rotation deferred: the writer had it open",
                         "warn")

            # After the deletes, so the pages they freed are folded in rather
            # than left sitting in the WAL.
            c = checkpoint("TRUNCATE")
            res["checkpoint"] = c
            # Only worth a line when it actually reclaimed something. A WAL that
            # was already small is the normal case and does not need announcing.
            if c.get("ok") and c.get("freed_mb", 0) >= 1:
                sec.keep()
                sec.note(f"WAL {c['before_mb']} MB -> {c['after_mb']} MB", "ok")
            elif c.get("ok") and c.get("busy"):
                res["note"] = "checkpoint deferred: a reader held an older snapshot"

            STATS.update(last_run=time.time(), last_error="")
            kv_set("maintenance.last_run", str(time.time()))
        except Exception as e:
            STATS["last_error"] = f"{type(e).__name__}: {e}"
            raise
    return res


async def watch() -> None:
    """Run housekeeping on a timer, forever."""
    # Not at startup: the first minutes are the busiest (recovery, first scan,
    # the arr fetch) and this is never urgent.
    await asyncio.sleep(300)
    while True:
        schedules.beat('maintenance')
        try:
            await asyncio.to_thread(sweep)
        except Exception as e:
            joblog.log(f"database housekeeping failed: {type(e).__name__}: {e}",
                       "error")
        await asyncio.sleep(POLL_S)


# --- memory diagnostics -----------------------------------------------------
# RSS here grew from 279 MB at rest to 650 MB across four scans - a rise that
# looks like a leak and could equally be Python's allocator holding freed
# arenas. Guessing between those two wasted more time than measuring would
# have, so this makes it measurable: arm it, do the thing you suspect, read the
# difference. tracemalloc roughly doubles allocation cost, which is why it is
# opt-in and off by default rather than always running.
_TRACE: dict = {"on": False, "baseline": None}


def mem_trace(action: str = "status", limit: int = 15) -> dict:
    import gc
    import tracemalloc

    if action == "start":
        if not _TRACE["on"]:
            tracemalloc.start(10)
            _TRACE["on"] = True
        _TRACE["baseline"] = tracemalloc.take_snapshot()
        return {"tracing": True, "baseline": "taken"}

    if action == "stop":
        if _TRACE["on"]:
            tracemalloc.stop()
            _TRACE.update(on=False, baseline=None)
        return {"tracing": False}

    out: dict = {"tracing": _TRACE["on"], "gc_objects": len(gc.get_objects()),
                 "gc_counts": gc.get_count()}
    try:
        import psutil
        p = psutil.Process(os.getpid())
        mi = p.memory_info()
        out["rss_mb"] = round(mi.rss / 1048576, 1)
        out["private_mb"] = round(getattr(mi, "private", mi.rss) / 1048576, 1)
        out["threads"] = p.num_threads()
    except Exception:
        pass
    if not _TRACE["on"]:
        out["hint"] = "POST /api/memory/trace?action=start, do the work, then read this"
        return out

    cur, peak = tracemalloc.get_traced_memory()
    out["traced_mb"] = round(cur / 1048576, 1)
    out["traced_peak_mb"] = round(peak / 1048576, 1)
    snap = tracemalloc.take_snapshot()
    # Against the baseline when there is one: what GREW is the question, not
    # what is merely big. A 30 MB allocation that was there at boot is not the
    # thing making RSS climb scan after scan.
    stats = (snap.compare_to(_TRACE["baseline"], "lineno")
             if _TRACE["baseline"] else snap.statistics("lineno"))
    rows = []
    for s in stats[:limit]:
        f = s.traceback[0]
        rows.append({
            "file": os.path.basename(f.filename), "line": f.lineno,
            "mb": round(getattr(s, "size_diff", s.size) / 1048576, 2),
            "count": getattr(s, "count_diff", s.count),
        })
    out["top"] = rows
    return out


def status() -> dict:
    """For the API - what housekeeping knows right now."""
    last = kv_get("maintenance.last_run")
    with cursor() as cur:
        counts = {
            "files": cur.execute("SELECT COUNT(*) c FROM files").fetchone()["c"],
            "jobs": cur.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
            "history": cur.execute("SELECT COUNT(*) c FROM history").fetchone()["c"],
        }
        try:
            counts["probes"] = cur.execute(
                "SELECT COUNT(*) c FROM file_probes").fetchone()["c"]
        except Exception:
            counts["probes"] = 0
        try:
            counts["renames"] = cur.execute(
                "SELECT COUNT(*) c FROM rename_queue").fetchone()["c"]
        except Exception:
            counts["renames"] = 0
    try:
        db_mb = round(os.path.getsize(DB_PATH) / 1048576, 1)
    except OSError:
        db_mb = 0.0
    # The log directory is reported alongside the database because it was the
    # thing actually growing without a bound, and a status view that showed
    # only rows is how that went unnoticed.
    logs = {"files": 0, "mb": 0.0}
    try:
        from .joblog import JOB_LOG_DIR, MAIN_LOG
        with os.scandir(JOB_LOG_DIR) as it:
            for e in it:
                if e.name.endswith(".log"):
                    logs["files"] += 1
                    try:
                        logs["mb"] += e.stat().st_size / 1048576
                    except OSError:
                        pass
        logs["mb"] = round(logs["mb"], 1)
        logs["main_log_mb"] = round(os.path.getsize(MAIN_LOG) / 1048576, 1)
    except OSError:
        pass
    return {"last_run": float(last) if last else 0.0, "db_mb": db_mb,
            "wal_mb": round(wal_mb(), 1), "rows": counts, "logs": logs,
            "retention": {"history_days": HISTORY_DAYS, "jobs_days": JOBS_DAYS,
                          "probe_days": PROBE_DAYS,
                          "rename_done_days": RENAME_DONE_DAYS,
                          "rename_gaveup_days": RENAME_GAVEUP_DAYS,
                          "log_days": LOG_DAYS, "log_max_mb": LOG_MAX_MB},
            "stats": dict(STATS)}
