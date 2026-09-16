r"""
nuarr - durable rename queue (Sonarr + Radarr)

NOT a retry queue, despite what this file used to be called. EVERY finished
job is handed to it - jobs.py enqueues "post-transcode" unconditionally - so
the normal life of a processed file is to wait here for twenty seconds while
the arr rescans, then be renamed on the first attempt. Measured across the
whole history: 21,848 files through, 21,845 renamed first time, two that ever
needed a second. Retrying is a capability it has, not what it is for, and
every log line and label that said "retry" up front was describing the 0.01%.

WHY THIS EXISTS
---------------
A transcode changes what a file IS - audio tracks dropped, subs burned in - so
the arr's naming format produces a different filename. nuarr asks the arr to
rename right after committing. That inline attempt fails in ways that are all
recoverable, and used to be given up on:

  1. THE REFRESH DEBOUNCE (the one that actually bit).
     Refreshes are debounced to one per series per 120 s, or a finishing season
     triggers a refresh storm. But the rename plan is asked for immediately
     afterwards - so for every episode whose refresh was skipped, the arr still
     holds the OLD mediainfo, reports "no rename needed", and the file keeps a
     name advertising tracks that are gone. The log said
     "no rename needed - filename already matches" 8 times in a row while
     Sonarr's Organize dialog listed all 8 as pending.

  2. The arr is mid-import, mid-scan, or briefly unreachable.
  3. The file is locked by Plex or an antivirus scanner at that instant.

All three are "try again later", not "give up". This keeps a persistent row per
file and retries with backoff until the name matches or the attempts run out.
State lives in SQLite, so a restart resumes rather than forgetting.

Nothing here renames files directly - it drives the arr's own rename command
through renamer.apply_rename(), which verifies afterwards.
"""
from __future__ import annotations

import asyncio
import os
import time

from .config import SETTINGS
from . import schedules
from .db import cursor
from . import joblog

# Backoff schedule in seconds. Deliberately starts longer than the 120 s refresh
# debounce, so the first retry lands AFTER the arr has re-read the file - the
# whole point is to stop asking while its mediainfo is stale.
BACKOFF = [180, 600, 1800, 3600, 10800, 21600]
MAX_ATTEMPTS = len(BACKOFF)
# The loop interval bounds how late a due rename actually runs, so at a 60 s
# poll a 20 s delay still meant up to 80 s of waiting. run_due() is one indexed
# query against a small table, so polling often costs nothing worth saving.
POLL_S = 20.0
# HOW MANY TITLES ONE PASS TAKES ON.
#
# Measured: 78 renames queued, every one DUE, every one still at attempts=0 -
# the oldest 4.7 hours old - and nothing finished in six hours. Not held
# (drivepool.hold('renames') was False) and not failing, because a failure
# increments attempts. Simply never recorded.
#
# The pass attempted every parent and wrote every result at the end:
#
#     for key, grp in groups.items():
#         results += await _attempt_batch(grp)      # 5 titles, serial
#     for row, good, detail in results:            # the only writer
#
# Each _attempt_batch issues a RescanSeries and waits up to 300 s for it, so
# five titles is up to twenty-five minutes of awaiting before the FIRST row is
# written. Anything ending the pass in that window - a restart, an exception in
# a later group - threw away the work of every group that had already
# succeeded, and the rows went back to looking untouched. That is how a queue
# with nothing wrong with it sat still for six hours, and how the ones that did
# finish came to average ten hours with a worst case of twenty days.
#
# So each group is recorded as it completes (see _record), and a pass is
# bounded to a few titles; the rest come round on the next tick, twenty seconds
# later. Progress is kept either way.
GROUPS_PER_PASS = 3

# HOW LONG BEFORE THE FIRST ATTEMPT, by why it was queued.
#
# This queue is no longer a failure path - since the commit stopped renaming
# inline, EVERY transcoded file arrives here. Charging all of them the 180 s
# retry delay meant every rename waited three minutes even when nothing was
# wrong, which is a regression dressed up as a backoff.
#
# The 180 s only ever existed to outlast the 120 s refresh debounce, so it
# applies solely to files queued BECAUSE of that debounce. A normal
# post-transcode rename just needs the arr's rescan to land - _attempt() forces
# one itself and waits for it - so a short delay is enough.
FIRST_DELAY = {
    "post-transcode": 20.0,        # normal path: only the rescan has to land
    "after deferred commit": 20.0,
    "found by rename sweep": 5.0,  # the sweep already confirmed it needs work
    "refresh debounced": 180.0,    # must outlast REFRESH_DEBOUNCE_S
}
DEFAULT_FIRST_DELAY = 60.0


def _first_delay(why: str) -> float:
    for k, v in FIRST_DELAY.items():
        if (why or "").startswith(k):
            return v
    return DEFAULT_FIRST_DELAY


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rename_queue (
                file_id     INTEGER PRIMARY KEY,
                arr_name    TEXT NOT NULL,
                parent_id   INTEGER,
                path        TEXT,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT,
                next_try_at REAL NOT NULL,
                created_at  REAL NOT NULL,
                done_at     REAL
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_rq_due "
                    "ON rename_queue(done_at, next_try_at)")


def enqueue(file_id: int, arr_name: str, parent_id: int | None,
            path: str, why: str = "") -> None:
    """Queue a file for a later rename attempt (idempotent)."""
    if not file_id or not arr_name:
        return
    now = time.time()
    with cursor() as cur:
        row = cur.execute("SELECT file_id, done_at FROM rename_queue "
                          "WHERE file_id=?", (file_id,)).fetchone()
        if row and row["done_at"] is None:
            return                       # already pending, leave the backoff be
        cur.execute(
            "INSERT INTO rename_queue(file_id,arr_name,parent_id,path,attempts,"
            "last_error,next_try_at,created_at,done_at) "
            "VALUES(?,?,?,?,0,?,?,?,NULL) "
            "ON CONFLICT(file_id) DO UPDATE SET "
            "arr_name=excluded.arr_name, parent_id=excluded.parent_id, "
            "path=excluded.path, attempts=0, last_error=excluded.last_error, "
            "next_try_at=excluded.next_try_at, done_at=NULL",
            (file_id, arr_name, parent_id, path, why,
             now + _first_delay(why), now))
    # Not "queued for retry" - this is where every processed file goes, and
    # the log said "retry" on a first pass, which reads as a failure that has
    # not happened. Same misnaming the panel carried.
    joblog.log(f"rename queued ({why or 'deferred'}): "
               f"{os.path.basename(path or '')}", "debug")


def _finish(file_id: int, ok: bool, detail: str) -> None:
    with cursor() as cur:
        if ok:
            cur.execute("UPDATE rename_queue SET done_at=?, last_error=NULL "
                        "WHERE file_id=?", (time.time(), file_id))
        else:
            # WRITE THE REASON TO THE LOG THE FIRST TIME IT CHANGES.
            #
            # A blocked rename used to be invisible outside the panel: the row
            # showed a category, the log showed nothing, and the only sign was
            # a count that never went down. Logging every retry would be worse
            # - the same sentence six times per file on a backoff schedule - so
            # it is logged when the reason is NEW or has CHANGED, which is the
            # moment there is something to learn.
            prev = cur.execute("SELECT last_error, path FROM rename_queue "
                               "WHERE file_id=?", (file_id,)).fetchone()
            cur.execute("UPDATE rename_queue SET last_error=? WHERE file_id=?",
                        (detail[:400], file_id))
    if not ok and (not prev or (prev["last_error"] or "") != detail):
        name = os.path.basename((prev["path"] if prev else "") or "")
        joblog.log(f"rename held — {name[:70]}: {detail}", "warn")


# Categories, in the words somebody reading the panel would use. The category
# itself is a routing key - "corrupt_parse", "orphan_record" - and printing it
# at a person explains nothing, which is exactly what the rename panel did for
# thirteen files over five retries.
_WHY_LABEL = {
    "corrupt_parse": "the release group in the name looks mangled",
    "orphan_record": "the arr is pointing at a file that is not there",
    "duplicate":     "something is already using that name",
    "too_long":      "the new name is longer than Windows allows",
    "locked":        "the file is in use",
    "unknown":       "the arr refused it and did not say why",
}


def _why(d, prefix: str = "") -> str:
    """One line: what is wrong, and what would fix it.

    The remedy matters as much as the cause. "the release group looks mangled"
    tells you the diagnosis; "correct the filename by hand, then rescan" tells
    you it will never clear on its own, which is the part that decides whether
    to keep waiting.
    """
    label = _WHY_LABEL.get(d.category, d.category)
    head = f"{prefix} — {label}" if prefix else label
    bits = [head]
    if d.explanation:
        bits.append(d.explanation.rstrip("."))
    if d.remedy:
        bits.append(f"To fix: {d.remedy.rstrip('.')}")
    return ". ".join(bits) + "."


async def _attempt_batch(rows: list[dict]) -> list[tuple[dict, bool, str]]:
    r"""Attempt every queued rename that shares one parent, in ONE pass.

    THIS IS THE BACKLOG FIX.

    _attempt() does a full RescanSeries and waits for it, then fetches the
    rename plan for the whole series - per FILE. Twenty queued episodes of one
    show therefore meant twenty identical series rescans, each awaited, and the
    arrs run commands serially. At roughly 5-15 s a rescan that is minutes of
    work to rename twenty files the arr could have done in one command, which
    is how the queue outran the drain rate and simply grew.

    Grouping collapses that to one rescan and one plan fetch per parent, then
    applies each file's rename against the plan already in hand. The per-file
    decisions - corrupt_parse repair, too_long, blocked - are unchanged.
    """
    # Imported here, not at module scope: arr imports config which imports this
    # chain back, so a top-level import is a cycle.
    from .arr import ArrClient
    from . import renamer

    out: list[tuple[dict, bool, str]] = []
    first = rows[0]
    cfg = next((a for a in SETTINGS.arrs
                if a.name == first["arr_name"] and a.enabled and a.api_key), None)
    if not cfg:
        return [(r, False, f"arr {first['arr_name']!r} not configured") for r in rows]

    client = ArrClient(cfg)
    try:
        parent = first["parent_id"]
        if parent:
            cmd = "RescanSeries" if cfg.kind == "sonarr" else "RescanMovie"
            key = "seriesId" if cfg.kind == "sonarr" else "movieId"
            r = await client._post("/command", {"name": cmd, key: parent})
            if r.get("id"):
                await client.wait_command(r["id"], timeout_s=300)

        plans = await renamer.plan_for_parent(client, parent)
        by_path = {os.path.normcase(p.existing_abs): p for p in plans}

        # ONE RenameFiles COMMAND FOR THE WHOLE TITLE.
        #
        # Batching the rescan was only half the cost: apply_rename() issues a
        # RenameFiles per file and waits for it, and the arrs run commands
        # serially - measured at ~13 s per file even after the rescan was
        # shared, which is what let the queue outgrow the drain rate.
        #
        # The arr's own command already accepts a LIST of file ids, so ask it
        # to do all of this title's files at once. apply_rename() still runs
        # afterwards per file, but its FIRST step is a verify - so for anything
        # this command already renamed it returns immediately instead of
        # issuing a second command. Failures simply fall through to the
        # existing per-file path, which is unchanged.
        want = set()
        for row in rows:
            with cursor() as cur:
                f = cur.execute("SELECT path FROM files WHERE id=?",
                                (row["file_id"],)).fetchone()
            want.add(os.path.normcase((f["path"] if f else None)
                                      or row["path"] or ""))
        ids = [p.file_id for p in plans
               if not p.blocked and os.path.normcase(p.existing_abs) in want]
        if len(ids) > 1 and parent:
            try:
                r = await client.rename_files(parent, ids)
                if r.get("id"):
                    await client.wait_command(r["id"], timeout_s=900)
                joblog.log(f"renamed {len(ids)} file(s) of "
                           f"{plans[0].parent_title if plans else parent} "
                           f"in one command", "ok")
                # the plan's paths are now stale - re-read so the per-file pass
                # verifies against what the arr actually did
                plans = await renamer.plan_for_parent(client, parent)
                by_path = {os.path.normcase(p.existing_abs): p for p in plans}
            except Exception as e:
                joblog.log(f"batch rename failed, falling back to one at a "
                           f"time: {type(e).__name__}: {e}", "warn")

        for row in rows:
            with cursor() as cur:
                f = cur.execute("SELECT path FROM files WHERE id=?",
                                (row["file_id"],)).fetchone()
            cur_path = (f["path"] if f else None) or row["path"] or ""
            p = by_path.get(os.path.normcase(cur_path))
            if not p:
                out.append((row, True, "name already matches"))
                continue

            if p.blocked:
                d = renamer.diagnose(p)
                if d.category == "corrupt_parse":
                    res = await renamer.repair_corrupt_name(client, p, confirm=True)
                    if res.ok:
                        if d.candidate:
                            with cursor() as c2:
                                c2.execute("UPDATE files SET path=? WHERE id=?",
                                           (d.candidate, row["file_id"]))
                        out.append((row, False,
                                    "name repaired on disk; re-checking next pass"))
                    else:
                        # NOT "repair failed: not auto-fixable: corrupt_parse".
                        # That said the category name three times and the reason
                        # zero times. The diagnosis has carried a sentence and a
                        # remedy all along; they just never reached the row.
                        out.append((row, False, _why(d, "could not be repaired")))
                    continue
                if d.category == "too_long":
                    out.append((row, False, _why(d, "not retrying")))
                    continue
                out.append((row, False, _why(d)))
                continue

            res = await renamer.apply_rename(client, p, confirm=True)
            if res.ok:
                with cursor() as cur:
                    cur.execute("UPDATE files SET path=? WHERE id=?",
                                (p.new_abs, row["file_id"]))
                out.append((row, True,
                            f"renamed -> {os.path.basename(p.new_rel)}"))
            else:
                # .say(), not .detail - the holders are the point. See
                # fileops.OpResult.say().
                out.append((row, False, res.say()))
    except Exception as e:
        done = {id(r) for r, _, _ in out}
        for r in rows:
            if id(r) not in done:
                out.append((r, False, f"{type(e).__name__}: {e}"))
    finally:
        await client.close()
    return out


_HOLD_SAID = {"at": 0.0}

# WHAT THE LAST PASS DID, FOR THE PANEL TO READ.
#
# Erik: "on the Renames waiting on the arrs panel show job gate pause info
# like from the drive pool just like the processing system panel".
#
# The queue has a gate - drivepool.hold("renames"), because a rename moves a
# file the balancer may be moving too - and when it closes, run_due returns
# without touching a row. Nothing said so anywhere a person would look: the
# panel went on reporting "78 due now", the count did not move, and the only
# record was a joblog line written at most once every ten minutes. A queue
# that is deliberately holding and a queue that is broken looked identical,
# which is exactly the confusion the rename investigation started in.
#
# So the pass writes down what it did and why, and stats() hands it to the
# panel beside the counts.
STATE = {"at": 0.0, "held": False, "why": "", "tried": 0, "ok": 0,
         "failed": 0, "titles": 0, "picked": 0}


def _note_hold(text: str) -> None:
    """Say it once every ten minutes, not every pass."""
    if time.time() - _HOLD_SAID["at"] >= 600:
        _HOLD_SAID["at"] = time.time()
        joblog.log(text, "info")


def _record(results) -> tuple:
    """Write down what a batch achieved. Counts (ok, failed, gave_up).

    This is run_due's own loop body, lifted out so it can be called per group
    rather than once at the end - see the note above GROUPS_PER_PASS.
    """
    ok = failed = gave_up = 0
    for row, good, detail in results:
        if good:
            ok += 1
            _finish(row["file_id"], True, detail)
            tries = row["attempts"] or 0
            joblog.log(f"rename {'retry ' if tries else ''}ok: "
                       f"{os.path.basename(row['path'] or '')} \u2014 {detail}", "ok")
            # DID THE ARR KEEP HOLD OF IT? For an air-by-date series a lost
            # episodeFile record is not recovered by a rescan, and an episode
            # the arr thinks has no file gets searched and downloaded again.
            try:
                from . import arrattach
                asyncio.create_task(arrattach.check_file(row["file_id"]))
            except Exception:                                # noqa: BLE001
                pass
            # AND PLEX, WHICH THE ARR DOES NOT TELL. Both folders are scanned
            # when the file crossed one, or the old location keeps a ghost.
            try:
                from . import plexqueue
                with cursor() as cur:
                    f = cur.execute("SELECT path FROM files WHERE id=?",
                                    (row["file_id"],)).fetchone()
                if f and f["path"]:
                    plexqueue.enqueue(row["file_id"], f["path"],
                                      old_path=row["path"] or "",
                                      why="the arr renamed this file")
            except Exception:                                # noqa: BLE001
                pass
            continue

        attempts = (row["attempts"] or 0) + 1
        failed += 1
        if attempts >= MAX_ATTEMPTS:
            gave_up += 1
            with cursor() as cur:
                cur.execute("UPDATE rename_queue SET attempts=?, last_error=?, "
                            "done_at=? WHERE file_id=?",
                            (attempts, f"gave up after {attempts}: {detail}"[:400],
                             time.time(), row["file_id"]))
            joblog.log(f"rename GAVE UP after {attempts} attempts: "
                       f"{os.path.basename(row['path'] or '')} \u2014 {detail}", "error")
        else:
            delay = BACKOFF[min(attempts, len(BACKOFF) - 1)]
            with cursor() as cur:
                cur.execute("UPDATE rename_queue SET attempts=?, last_error=?, "
                            "next_try_at=? WHERE file_id=?",
                            (attempts, detail[:400], time.time() + delay,
                             row["file_id"]))
            joblog.log(f"rename attempt {attempts}/{MAX_ATTEMPTS} failed, next "
                       f"in {delay//60} min: "
                       f"{os.path.basename(row['path'] or '')} \u2014 {detail}",
                       "warn")
    return ok, failed, gave_up


async def run_due(limit: int = 60) -> dict:
    """Process every due row, batched by parent. Returns a small summary."""
    now = time.time()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT * FROM rename_queue WHERE done_at IS NULL AND next_try_at<=? "
            "ORDER BY next_try_at LIMIT ?", (now, limit))]
    if not rows:
        return {"tried": 0, "ok": 0, "failed": 0, "gave_up": 0}
    # A RENAME MOVES A FILE THE BALANCER MAY BE MOVING TOO. The arr does the
    # rename, but it is nuarr that asks for it now rather than later, and
    # later is free: the rows keep their place and their backoff.
    try:
        from . import drivepool as _dp
        held, why = _dp.hold("renames")
    except Exception:                                    # noqa: BLE001
        held, why = False, ""
    if held:
        _note_hold(f"{len(rows)} rename(s) waiting - {why}")
        STATE.update(at=time.time(), held=True, why=why, tried=0, ok=0,
                     failed=0, titles=0, picked=0)
        return {"tried": 0, "ok": 0, "failed": 0, "gave_up": 0, "held": why}
    STATE.update(held=False, why="")

    # Group by the series/movie they belong to - one rescan serves all of them.
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["arr_name"], r["parent_id"]), []).append(r)

    sec = joblog.section("rename queue")
    sec.__enter__(); sec.keep()
    # A FEW TITLES AT A TIME. Each one costs a RescanSeries the arr runs
    # serially and this awaits for up to 300 s; taking them all meant a pass
    # that could outlive the process that started it. The rest are still due
    # and come round on the next tick.
    picked = list(groups.items())[:GROUPS_PER_PASS]
    sec.note(f"{len(rows)} rename(s) due across {len(groups)} title(s); "
             f"doing {len(picked)} this pass")

    results: list[tuple[dict, bool, str]] = []
    tally: list[tuple] = []
    for key, grp in picked:
        # WRITTEN AS IT LANDS, not banked to the end. See the note above
        # GROUPS_PER_PASS: whatever this group achieved is recorded before the
        # next one is started, so an interruption costs the group in flight
        # and nothing else.
        got = await _attempt_batch(grp)
        results += got
        try:
            tally.append(_record(got))
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"rename bookkeeping: {type(e).__name__}: {e}", "error")

    # The counts come from _record, which did the writing group by group.
    ok = sum(x[0] for x in tally)
    failed = sum(x[1] for x in tally)
    gave_up = sum(x[2] for x in tally)
    STATE.update(at=time.time(), held=False, why="", tried=len(results),
                 ok=ok, failed=failed, titles=len(groups), picked=len(picked))
    sec.result = f"{ok} ok, {failed} retrying, {gave_up} gave up"
    sec.__exit__(None, None, None)
    return {"tried": len(rows), "ok": ok, "failed": failed, "gave_up": gave_up}


# How long a finished row is kept. Nothing reads one after the panel has shown
# it; the count of what is OUTSTANDING is what these tables are for. See the
# sweep in watch().
DONE_KEEP_S = 14 * 24 * 3600

_TABLE = 'rename_queue'


def sweep_done(keep_s: float = DONE_KEEP_S) -> int:
    """Drop finished rows older than keep_s. -> how many went."""
    try:
        with cursor() as cur:
            n = cur.execute(
                f"DELETE FROM {_TABLE} WHERE done_at IS NOT NULL AND done_at < ?",
                (time.time() - float(keep_s),)).rowcount
        return int(n or 0)
    except Exception:                                            # noqa: BLE001
        return 0


_LAST_SWEEP = {"at": 0.0}


def _sweep_done_sometimes() -> None:
    """At most once an hour, from the loop. Never raises."""
    now = time.time()
    if now - _LAST_SWEEP["at"] < 3600:
        return
    _LAST_SWEEP["at"] = now
    n = sweep_done()
    if n:
        joblog.log(f"{_TABLE}: forgot {n:,} finished row(s) older than "
                   f"{DONE_KEEP_S / 86400:.0f} days", "debug")


async def watch() -> None:
    init()
    await asyncio.sleep(45)
    while True:
        schedules.beat('renamequeue')
        try:
            await run_due()
            await asyncio.to_thread(_sweep_done_sometimes)
        except Exception as e:
            joblog.log(f"rename queue loop error: {type(e).__name__}: {e}", "error")
        from . import workers
        await asyncio.sleep(workers.tune("rename_poll_s"))


def stats() -> dict:
    with cursor() as cur:
        pend = cur.execute("SELECT COUNT(*) n FROM rename_queue "
                           "WHERE done_at IS NULL").fetchone()["n"]
        gave = cur.execute("SELECT COUNT(*) n FROM rename_queue "
                           "WHERE done_at IS NOT NULL AND last_error LIKE 'gave up%'"
                           ).fetchone()["n"]
        due = cur.execute("SELECT COUNT(*) n FROM rename_queue "
                          "WHERE done_at IS NULL AND next_try_at<=?",
                          (time.time(),)).fetchone()["n"]
        # WAITING IS NOT FAILING, AND THE PANEL HAD NO WAY TO SAY SO.
        #
        # Every finished job is handed to this queue - jobs.py enqueues
        # "post-transcode" unconditionally - so the normal state of a
        # just-processed file is to sit here for twenty seconds while the arr
        # rescans. Measured over the whole history: 21,848 files have passed
        # through and 21,845 completed on the first attempt; two ever needed a
        # second. Counting all of them as "pending renames" under a heading
        # that reads like a fault was wrong about the common case by a factor
        # of ten thousand.
        first = cur.execute("SELECT COUNT(*) n FROM rename_queue "
                            "WHERE done_at IS NULL AND attempts<=1"
                            ).fetchone()["n"]
        retry = cur.execute("SELECT COUNT(*) n FROM rename_queue "
                            "WHERE done_at IS NULL AND attempts>1"
                            ).fetchone()["n"]
        rows = [dict(r) for r in cur.execute(
            "SELECT file_id,path,attempts,last_error,next_try_at FROM rename_queue "
            "WHERE done_at IS NULL ORDER BY next_try_at LIMIT 50")]
        done = cur.execute("SELECT COUNT(*) n FROM rename_queue "
                           "WHERE done_at IS NOT NULL").fetchone()["n"]
    # THE GATE, AND THE LAST PASS. A count with no explanation is what made
    # a held queue unreadable - see the note above STATE. The hold is asked
    # for fresh (it is a switch plus a look at what DrivePool is doing, not a
    # disk read); everything else is what the last pass wrote down.
    held, why = False, ""
    try:
        from . import drivepool as _dp
        held, why = _dp.hold("renames")
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import workers as _w
        every = float(_w.tune("rename_poll_s") or POLL_S)
    except Exception:                                    # noqa: BLE001
        every = POLL_S
    gate = {"held": bool(held), "why": why or STATE.get("why") or "",
            "at": STATE.get("at") or 0.0, "every_s": every,
            "per_pass": GROUPS_PER_PASS,
            "last": {"tried": STATE.get("tried") or 0,
                     "ok": STATE.get("ok") or 0,
                     "failed": STATE.get("failed") or 0,
                     "titles": STATE.get("titles") or 0,
                     "picked": STATE.get("picked") or 0}}
    return {"pending": pend, "due": due, "gave_up": gave, "rows": rows,
            "first_try": first, "retrying": retry, "done": done,
            "gate": gate}
