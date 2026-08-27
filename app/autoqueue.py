r"""
nuarr - automatic queue top-up

WHAT IT DOES
------------
Keeps a steady amount of work in the queue, drawn from every eligible file in
the library, OLDEST FILE FIRST. It adds; it never removes, never reorders what
is already queued, and never touches anything a human put there.

WHY TOP UP RATHER THAN QUEUE EVERYTHING
---------------------------------------
The obvious version - "queue all 24,000 eligible files" - was measured and is a
bad trade:

  * every enqueue PROBES AND PLANS the file first, so one pass is hours of
    ffprobe before a single frame is encoded;
  * a 24,000-row queue makes the panel, the reordering buttons and the disk
    breakdown unwieldy, and Stop becomes an all-or-nothing act;
  * the queue is the database, so it all has to be written, polled and paged.

Holding a target depth gets the same throughput - the encoders are the
bottleneck, not the queue - while keeping every one of those costs small. The
queue becomes a buffer, not a manifest.

OLDEST FIRST
------------
By file mtime ascending, so the backlog is worked through in the order it
arrived and the result is predictable: you can point at the queue and say what
comes next. New imports join the back. The alternative - newest first - starves
the old material indefinitely, which on a library this size means most of it
never gets touched.

WHAT IT WILL NOT DO
-------------------
  * it will not queue a file that is already queued or running
  * it will not run while the whole gate is shut (that would just build a
    backlog nobody asked for while the machine is deliberately idle)
  * it does not pause for Plex on its own - the GATE decides what may RUN, and
    holding the top-up as well would only mean the encoders sit idle with an
    empty queue the moment playback stops
"""
from __future__ import annotations

import asyncio
import os
import time

from . import joblog
from .db import cursor, display_label, kv_get, kv_set
from . import schedules

# Defaults. Both are live-editable through the API, so a change takes effect on
# the next pass without a restart.
DEFAULT_TARGET = 200        # how many queued jobs to keep available
DEFAULT_BATCH = 25          # most to add in one pass, so a pass stays short
POLL_S = 20.0

STATE: dict = {
    "running": False,       # a top-up pass is in progress right now
    "last_run": 0.0,
    "last_added": 0,
    "last_nowork": 0,
    "added_total": 0,
    "nowork_total": 0,
    "last_error": "",
    "current": "",          # the file being CHECKED right now
    "verdict": "",          # how the current one went, once known
    # The last file to get an answer, so the panel can show what just happened
    # rather than only what is happening - most checks finish in a second or
    # two and would otherwise flash past unread.
    "last_verdict": None,   # (queued|skipped, title)
    # Every file currently mid-check. "current" only ever held the most recent
    # one, so with six probes in flight the panel could name one file and a
    # "+5 more" it could say nothing about. Each entry carries what the panel
    # needs to draw a live row: {label, file, disk, size, at}. `at` is what
    # lets the browser animate elapsed time without polling faster.
    "checking_names": [],
    # What the file being probed turned out to need, keyed by file id, so a
    # probe row can show its verdict for a beat before it leaves the list
    # rather than just vanishing. Trimmed with the rows themselves.
    "last_result": {},
}


PROBE_LINGER_S = 2.5


def _reap_probes() -> list[dict]:
    """Probe rows still worth showing: live ones, plus finished ones for a
    couple of seconds so their verdict can be read."""
    now = time.time()
    rows = [c for c in STATE.get("checking_names", [])
            if not c.get("done_at") or now - c["done_at"] < PROBE_LINGER_S]
    STATE["checking_names"] = rows
    res = STATE.get("last_result", {})
    return [dict(c, **{"result": res.get(c["id"])}) for c in rows]


def _note_check(r: dict, verdict: str, plan=None, err: str = "") -> None:
    """Stamp the outcome onto the probe row, briefly.

    A row that simply disappears the instant its answer arrives tells you
    nothing: on this library most probes end in "already fine", and the whole
    point of watching is seeing WHICH way each one went. The row is held for
    a moment wearing its verdict, then dropped by the reaper below.
    """
    res = STATE.setdefault("last_result", {})
    res[r["id"]] = {
        "at": time.time(), "verdict": verdict,
        "summary": (plan.summary() if plan is not None else
                    "already set up correctly" if verdict == "skipped"
                    else (err[:120] or verdict)),
    }
    # bounded: only rows still on screen matter
    if len(res) > 60:
        for k in sorted(res, key=lambda k: res[k]["at"])[:30]:
            res.pop(k, None)


def enabled() -> bool:
    return (kv_get("autoqueue.enabled") or "0") == "1"


def set_enabled(on: bool) -> None:
    kv_set("autoqueue.enabled", "1" if on else "0")
    joblog.log(f"auto-queue {'enabled' if on else 'disabled'}", "info")


def target() -> int:
    try:
        return max(0, int(kv_get("autoqueue.target") or DEFAULT_TARGET))
    except (TypeError, ValueError):
        return DEFAULT_TARGET


def set_target(n: int) -> int:
    n = max(0, min(int(n), 5000))
    kv_set("autoqueue.target", str(n))
    return n


def _queued_depth() -> int:
    with cursor() as cur:
        return cur.execute(
            "SELECT COUNT(*) n FROM jobs WHERE state='queued'").fetchone()["n"]


def candidates(limit: int, only_disks: list[str] | None = None) -> list[dict]:
    r"""Eligible files not already in the queue, oldest on disk first.

    Mirrors the manual path's definition of "queueable" exactly - eligible,
    known to an arr, not already queued or running - so the two cannot
    disagree about what is available. Only the ORDER differs: manual queueing
    is largest-first (get the big wins), auto is oldest-first (work the
    backlog).

    mtime can be NULL on rows a scan has not fully populated; those sort last
    rather than jumping the queue on a missing value.

    SPREAD ACROSS SPINDLES, not just oldest-first.
    ----------------------------------------------
    Pure oldest-first produced a queue that no scheduler could rescue. Measured
    on a 1,966-job queue: 944 stream copies on NU-DRIVE-0 and 980 on
    NU-DRIVE-1, with every other disk holding nothing but a handful of encodes.
    That is not a coincidence - the oldest files in the library are the ones
    DrivePool placed first, so they cluster on the disks that filled first.

    With eight passthrough workers and only two disks carrying passthrough
    work, at most two can run without fighting each other for a spindle. The
    claim path can only choose among what is queued; if the queue is 96% two
    disks, no amount of scheduling helps. So the spread has to happen HERE.

    Files are taken round-robin across disks - oldest first WITHIN each disk,
    so the backlog is still worked in order per spindle, but a top-up of 200
    draws from every disk that has work rather than 200 from the same one.
    Requeued files keep their absolute priority and are exempt.
    """
    # TAKE THE OLDEST FEW FROM EACH DISK, not the oldest few overall.
    #
    # An earlier version of this over-fetched a global oldest-N and interleaved
    # the result, which looked right and was not: when the clustering is severe
    # the whole window comes from one or two spindles, and interleaving a
    # single-disk window still yields a single-disk queue. Measured after
    # clearing a concentrated queue - a 400-row window returned exactly two
    # disks, so a top-up of 25 alternated 0 1 0 1 0 1 and reached nothing else.
    #
    # ROW_NUMBER partitioned by disk fixes it at the source: each spindle
    # contributes its own oldest rows regardless of how the ages compare across
    # disks. Requeued files bypass this entirely - an explicit "run this next"
    # is not something to balance.
    per_disk = max(2, (limit // 6) + 2)
    base_where = ("state='eligible' AND arr_file_id IS NOT NULL "
                  "AND id NOT IN (SELECT file_id FROM jobs "
                  "               WHERE file_id IS NOT NULL "
                  "                 AND state IN ('queued','running')) ")
    cols = ("id, path, title, season, episode, mtime, size, requeued_at, "
            "pool_disk")
    # TARGETED TOP-UP. When the caller is filling a full-but-stuck queue, the
    # ONLY useful rows are the ones on the idle spindles - anything else would
    # deepen a queue that is already too deep to start.
    disk_only = ""
    disk_params: tuple = ()
    if only_disks:
        disk_only = ("AND pool_disk IN ("
                     + ",".join("?" * len(only_disks)) + ") ")
        disk_params = tuple(only_disks)
    base_where += disk_only
    with cursor() as cur:
        # Requeued first - but PARTITIONED BY DISK, for the same reason the
        # ordinary query is. A flat "ORDER BY requeued_at DESC LIMIT n" over a
        # bulk requeue returns n rows that all share a timestamp, so the tie is
        # broken by rowid, which follows the scan, which follows the disk: the
        # window itself came back single-spindle and no amount of dealing
        # afterwards could spread what was never fetched.
        rows = [dict(r) for r in cur.execute(
            f"SELECT {cols} FROM (SELECT {cols}, ROW_NUMBER() OVER ("
            "    PARTITION BY pool_disk ORDER BY requeued_at DESC"
            f"  ) rn FROM files WHERE {base_where} "
            "  AND requeued_at IS NOT NULL) "
            "WHERE rn <= ? ORDER BY rn, requeued_at DESC",
            disk_params + (per_disk,))]
        if len(rows) < limit:
            rows += [dict(r) for r in cur.execute(
                f"SELECT {cols} FROM (SELECT {cols}, ROW_NUMBER() OVER ("
                "    PARTITION BY pool_disk "
                "    ORDER BY CASE WHEN mtime IS NULL THEN 1 ELSE 0 END, mtime"
                f"  ) rn FROM files WHERE {base_where} "
                "  AND requeued_at IS NULL) "
                "WHERE rn <= ? "
                "ORDER BY rn, CASE WHEN mtime IS NULL THEN 1 ELSE 0 END, mtime",
                disk_params + (per_disk,))]

    rows = _spread_by_disk(rows, limit)
    for r in rows:
        r["label"] = display_label(r.get("title"), r.get("season"),
                                   r.get("episode"))
    return rows


def _deal(rows: list[dict], limit: int, key="mtime") -> list[dict]:
    """Round-robin one group of rows across spindles, order kept per disk."""
    by_disk: dict[str, list] = {}
    for r in rows:
        by_disk.setdefault(r.get("pool_disk") or "", []).append(r)
    if not by_disk:
        return []
    # Start with the disk holding the oldest file, so the head of the queue is
    # still the oldest thing on disk rather than an arbitrary spindle.
    order = sorted(by_disk, key=lambda d: (by_disk[d][0].get(key) or 0))
    out: list[dict] = []
    i = 0
    while len(out) < limit and any(by_disk.values()):
        d = order[i % len(order)]
        i += 1
        if by_disk[d]:
            out.append(by_disk[d].pop(0))
    return out[:limit]


def _spread_by_disk(rows: list[dict], limit: int) -> list[dict]:
    """Round-robin `rows` across pool disks, preserving order within each.

    REQUEUED FILES ARE SPREAD TOO. They used to be emitted first and untouched,
    on the reasoning that an explicit "run this next" outranks balancing. That
    is right for the handful of files someone requeues by hand from the panel,
    and badly wrong for the case that actually happens: a rule change marks
    8,402 files requeued at once, every row carries a requeued_at within the
    same second, so "preserve their order" preserves nothing but rowid order -
    and rowid order follows the scan, which follows the disk.

    Measured on this library right after such a requeue: 864 queued jobs, ALL
    of them on NU-DRIVE-0 and NU-DRIVE-1, ten spindles idle, two workers busy
    out of twelve. The queue was full and the machine was asleep.

    Requeued still outranks everything - they are dealt first - but they are
    dealt across disks, because within one bulk requeue the order between two
    files was never meaningful in the first place.
    """
    req = [r for r in rows if r.get("requeued_at")]
    out = _deal(req, limit, key="requeued_at") if req else []
    if len(out) >= limit:
        return out
    rest = [r for r in rows if not r.get("requeued_at")]
    return (out + _deal(rest, limit - len(out)))[:limit]


def progress() -> dict:
    """How far through the library this has got, for the panel."""
    with cursor() as cur:
        # THE DENOMINATOR IS THE LIBRARY, NOT THE TABLE.
        #
        # This counted every row, including 'deleted' - rows for files that are
        # not on the pool any more. They can never reach 'done', so each one
        # permanently caps the bar below 100%. With 171 of them the panel read
        # "PROCESSED 99.6%" next to "STILL TO QUEUE 0" and an empty queue, which
        # is a progress bar that can never finish and therefore stops meaning
        # anything.
        #
        # 'duplicate' is excluded for the same reason: a row kept to record that
        # a byte-identical copy exists is bookkeeping, not outstanding work.
        row = cur.execute(
            "SELECT "
            " SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) done, "
            " SUM(CASE WHEN state='eligible' THEN 1 ELSE 0 END) eligible, "
            " COUNT(*) total FROM files "
            " WHERE state NOT IN ('deleted','duplicate')").fetchone()
        # Eligible files with no queue entry yet - the actual remaining work,
        # which is not the same as "eligible" once a few hundred are queued.
        waiting = cur.execute(
            "SELECT COUNT(*) n FROM files WHERE state='eligible' "
            "AND arr_file_id IS NOT NULL "
            "AND id NOT IN (SELECT file_id FROM jobs WHERE file_id IS NOT NULL "
            "               AND state IN ('queued','running'))").fetchone()["n"]
        queued = cur.execute(
            "SELECT COUNT(*) n FROM jobs WHERE state='queued'").fetchone()["n"]
        by_src = {r["source"] or "manual": r["n"] for r in cur.execute(
            "SELECT COALESCE(source,'manual') source, COUNT(*) n FROM jobs "
            "WHERE state='queued' GROUP BY 1")}
        # The oldest file still waiting says how deep into the backlog this is,
        # which a bare count cannot.
        oldest = cur.execute(
            "SELECT title, mtime FROM files WHERE state='eligible' "
            "AND arr_file_id IS NOT NULL AND mtime IS NOT NULL "
            "AND id NOT IN (SELECT file_id FROM jobs WHERE file_id IS NOT NULL "
            "               AND state IN ('queued','running')) "
            "ORDER BY mtime ASC LIMIT 1").fetchone()
        # HOW WIDE THE QUEUE IS SPREAD. The whole point of the per-disk
        # selection is that work lands on many spindles rather than two, and
        # that is invisible from a total. Shown as counts per disk so a queue
        # that has drifted back onto one disk is obvious at a glance.
        spread = [{"disk": r["d"], "n": r["n"]} for r in cur.execute(
            "SELECT COALESCE(f.pool_disk,'—') d, COUNT(*) n FROM jobs j "
            "LEFT JOIN files f ON f.id=j.file_id WHERE j.state='queued' "
            "GROUP BY 1 ORDER BY 2 DESC")]
        # And how many spindles still HAVE work to draw from, which is the
        # ceiling on how wide the queue can ever be.
        pool_disks = cur.execute(
            "SELECT COUNT(DISTINCT pool_disk) n FROM files WHERE state='eligible' "
            "AND arr_file_id IS NOT NULL AND pool_disk IS NOT NULL "
            "AND id NOT IN (SELECT file_id FROM jobs WHERE file_id IS NOT NULL "
            "               AND state IN ('queued','running'))").fetchone()["n"]

    done = int((row["done"] if row else 0) or 0)
    eligible = int((row["eligible"] if row else 0) or 0)
    total = int((row["total"] if row else 0) or 0)
    return {
        "enabled": enabled(),
        "target": target(),
        "queued": queued,
        "by_source": by_src,
        "waiting": waiting,
        "done": done,
        "eligible": eligible,
        "total": total,
        # Share of the managed library already processed. Files in neither
        # state (missing, blocked) are in the denominator on purpose - the
        # question is "how much of the library is done", not "how much of the
        # part I like the look of".
        "pct": round(done / total * 100, 1) if total else 0.0,
        "oldest_waiting": (oldest["title"] if oldest else None),
        "oldest_at": (oldest["mtime"] if oldest else None),
        "spread": spread,
        "spread_disks": len(spread),
        "source_disks": pool_disks,
        # WHY IT IS NOT TOPPING UP. Every early return in _top_up_locked() is a
        # deliberate decision, and without saying so a queue sitting at 77 of a
        # 2,000 target looks broken rather than intentional.
        "hold_reason": _hold_reason(queued),
        **STATE,
        # after **STATE so the reaped view wins over the raw list
        "checking_names": _reap_probes(),
    }


def _hold_reason(queued: int) -> str | None:
    """The reason the next top-up will add nothing, or None if it will run."""
    if not enabled():
        return "auto-queue is off"
    if queued >= target():
        return f"queue is at its target of {target():,}"
    # The gate check in _top_up_locked is async; mirror its verdict from the
    # cached state rather than re-probing here, since this runs on every poll.
    try:
        from . import gate
        if gate.get_toggle("gate.manual_pause"):
            return ("everything is held (manual pause) — no backlog is built "
                    "while the machine is deliberately idle")
    except Exception:
        pass
    return None


# ONE PASS AT A TIME.
#
# candidates() reads "eligible and not already queued", then enqueues. Between
# those two steps a second pass sees the same rows as still free and queues
# them again - measured: the timer and the "Top up now" button overlapped and
# produced 18 files queued twice, which would have re-encoded every one of them
# a second time. The check and the insert have to be one critical section.
_LOCK = asyncio.Lock()


async def top_up() -> int:
    """One pass. Returns how many jobs were added."""
    from . import gate, jobs

    if not enabled():
        return 0
    if _LOCK.locked():
        # Already topping up. A second concurrent pass has nothing useful to
        # add - the running one is working through the same list.
        return 0
    async with _LOCK:
        return await _top_up_locked()


def _starving_disks() -> list[str]:
    r"""Spindles with a free worker's worth of nothing to do.

    DEPTH IS NOT THE SAME AS USEFUL DEPTH.
    candidates() already deals rows out across disks, so a freshly built queue
    is well spread. It does not stay that way: the queue DRAINS unevenly.
    Whichever spindles are fastest, or least contended, empty first, and what
    is left is a deep queue concentrated on the two disks that are already
    busy - or worse, on the one disk a viewer is streaming from, which the
    claim path is right to refuse. Depth says 2,000; the number of jobs that
    can actually START says zero, and every worker sits idle in front of a
    full queue.

    So this asks the question the depth check cannot: which disks have a
    free slot and no queued work waiting for them? If any do, a top-up is
    worth running even though the queue is nominally full.
    """
    from . import jobs
    try:
        load = jobs.disk_load()
        watched = set(jobs.gate.plex_disks())
    except Exception:
        return []
    with cursor() as cur:
        known = [r["pool_disk"] for r in cur.execute(
            "SELECT DISTINCT pool_disk FROM files WHERE pool_disk IS NOT NULL "
            "AND pool_disk != ''")]
        queued = {r["pool_disk"]: r["n"] for r in cur.execute(
            "SELECT f.pool_disk, COUNT(*) n FROM jobs j "
            "JOIN files f ON f.id = j.file_id "
            "WHERE j.state='queued' GROUP BY f.pool_disk")}
    # A disk is starving when nothing heavy is on it, nothing is queued for it,
    # and no viewer is reading from it - i.e. it could be working and is not.
    return [d for d in known
            if d not in watched
            and load.get(d, 0) == 0
            and queued.get(d, 0) == 0]


def rebalance(max_per_disk: int = 60) -> dict:
    r"""Dequeue the excess from over-represented spindles.

    Spreading only shapes what gets ADDED. It cannot fix a queue that is
    already lopsided, and a lopsided queue outlives the fix by however long it
    takes to drain - which, at 841 jobs on two disks with two workers able to
    touch them, is days of a twelve-worker machine running at two.

    Dequeuing is cheap and safe here: a queued job is a plan over a file that
    is still marked eligible, so removing the row returns the file to the pool
    of work and the next top-up picks it up again - this time dealt across
    disks. Nothing is lost, and the plan is recomputed from a fresh probe when
    it comes back, which is strictly better than replaying a stale one.

    Running jobs are never touched. Neither is anything a person queued by
    hand: source='manual' is an explicit instruction and outranks tidiness.
    """
    with cursor() as cur:
        counts = {r["d"]: r["n"] for r in cur.execute(
            "SELECT f.pool_disk d, COUNT(*) n FROM jobs j "
            "JOIN files f ON f.id=j.file_id WHERE j.state='queued' "
            "GROUP BY 1")}
        over = {d: n for d, n in counts.items() if n > max_per_disk}
        removed = 0
        for d, n in over.items():
            cur.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT j.id FROM jobs j JOIN files f ON f.id=j.file_id "
                "   WHERE j.state='queued' AND f.pool_disk=? "
                "     AND COALESCE(j.source,'') != 'manual' "
                "   ORDER BY j.priority DESC, j.created_at DESC LIMIT ?)",
                (d, n - max_per_disk))
            removed += cur.rowcount
    if removed:
        joblog.log(f"queue rebalanced: dropped {removed} job(s) from "
                   f"{', '.join(sorted(over))} so the other spindles can be "
                   f"fed — the files stay eligible and come back spread out",
                   "info")
    return {"removed": removed, "over": over, "counts": counts}


def _lopsided(min_disks: int = 4) -> bool:
    """Is the queue concentrated on too few spindles to keep workers busy?"""
    with cursor() as cur:
        rows = [r["n"] for r in cur.execute(
            "SELECT COUNT(*) n FROM jobs j JOIN files f ON f.id=j.file_id "
            "WHERE j.state='queued' GROUP BY f.pool_disk")]
    return bool(rows) and sum(rows) >= 100 and len(rows) < min_disks


async def _top_up_locked() -> int:
    from . import gate, jobs
    # A deep queue on two spindles is not a full queue, it is a stuck one.
    # Trim it before deciding how much to add, so the depth check below is
    # measuring reachable work rather than a pile.
    if _lopsided():
        await asyncio.to_thread(rebalance)

    depth = _queued_depth()
    want = target() - depth
    starving: list[str] = []
    if want <= 0:
        # The queue is full by count. That only means the machine is busy if
        # the work is reachable - see _starving_disks().
        starving = _starving_disks()
        if not starving:
            return 0
        # Take a small, targeted bite: enough to give each idle spindle
        # something, not enough to blow past the target in a way that matters.
        want = min(DEFAULT_BATCH, max(4, len(starving) * 3))
        joblog.log(f"auto-queue: the queue is full but "
                   f"{len(starving)} disk(s) have nothing to do "
                   f"({', '.join(sorted(starving)[:6])}) — pulling work for "
                   f"them so the encoders stay fed", "debug")

    # If EVERYTHING is held there is no point building a backlog - the machine
    # is deliberately idle and the queue would just grow behind a closed door.
    # A partial hold (Plex holding encodes only) is fine: passthrough work still
    # runs, and the queue should be ready for when the hold lifts.
    try:
        st = await gate.status()
        if not st.open and not any(st.open_for(p)
                                   for p in ("encode", "passthrough")):
            return 0
    except Exception:
        pass                      # a gate probe failure must not stop top-up

    rows = candidates(min(want, DEFAULT_BATCH), only_disks=starving or None)
    if not rows:
        return 0

    added = 0
    nowork = 0
    STATE.update(running=True, last_error="")

    # PROBE SEVERAL AT ONCE.
    #
    # This was a plain `for` loop awaiting each enqueue in turn, so 25 files
    # took ~55 s at roughly 2.2 s each. ffprobe is not compute - it reads a
    # container header and a little of the stream - so a probe spends nearly
    # all its time waiting on a disk, and the files are spread across twelve
    # independent spindles. Serialising them left eleven idle.
    #
    # Bounded by probe_workers (the same tunable the rest of the system uses
    # for probe concurrency) so this cannot swamp the pool that the ENCODERS
    # are also reading from - the queue being fed faster is worthless if it
    # starves the jobs already running.
    try:
        from . import workers as _w
        limit = max(1, int(_w.get().probe_workers))
    except Exception:
        limit = 4
    sem = asyncio.Semaphore(limit)
    STATE["checking"] = 0

    async def one(r):
        nonlocal added, nowork
        async with sem:
            if not enabled():          # turned off mid-pass; stop promptly
                return
            # "checking", not "adding" - the probe decides, and with several in
            # flight the panel shows the most recent name plus a count rather
            # than pretending it is working through them one at a time.
            STATE["current"] = r["label"]
            STATE["checking"] = STATE.get("checking", 0) + 1
            STATE.setdefault("checking_names", []).append({
                "id": r["id"], "label": r["label"],
                "file": os.path.basename(r.get("path") or ""),
                "disk": r.get("pool_disk") or "", "size": r.get("size") or 0,
                "at": time.time()})
            try:
                j = await jobs.enqueue(r["id"], r["path"], r["label"],
                                       source="auto")
                added += 1
                STATE["last_verdict"] = ("queued", r["label"])
                # the plan just decided IS the detail worth showing - what will
                # be done to this file and why, transcoding-panel style
                _note_check(r, "queued", getattr(j, "plan", None))
            except jobs.NothingToDo:
                # The probe found nothing to do, so no job was made and the
                # file is already marked done. This is a RESULT, not an error -
                # on this library it is the majority outcome, and counting it
                # as a failure would make the log unreadable.
                nowork += 1
                STATE["last_verdict"] = ("skipped", r["label"])
                _note_check(r, "skipped")
            except Exception as e:
                # One bad file must not stall the whole mechanism.
                joblog.log(f"auto-queue skipped {r['label']}: "
                           f"{type(e).__name__}: {e}", "debug")
                _note_check(r, "error", err=f"{type(e).__name__}: {e}")
            finally:
                STATE["checking"] = max(0, STATE.get("checking", 1) - 1)
                # LINGER, don't vanish. The row keeps its place wearing the
                # verdict for a couple of seconds so the answer can be read;
                # _reap_probes drops it afterwards. Without this, a fast probe
                # appeared and disappeared between two polls and was never
                # seen at all.
                for c in STATE.get("checking_names", []):
                    if c.get("id") == r["id"]:
                        c["done_at"] = time.time()
                        break

    try:
        await asyncio.gather(*(one(r) for r in rows))
    finally:
        STATE.update(running=False, current="", verdict="", checking=0,
                     checking_names=[], last_run=time.time(),
                     last_added=added, last_nowork=nowork)
        STATE["added_total"] = STATE.get("added_total", 0) + added
        STATE["nowork_total"] = STATE.get("nowork_total", 0) + nowork
    if added or nowork:
        oldest = rows[0].get("mtime")
        when = (time.strftime("%Y-%m-%d", time.localtime(oldest))
                if oldest else "unknown date")
        joblog.log(f"auto-queue: {added} queued, {nowork} needed no work "
                   f"(from {when} onward; {depth + added} of {target()} target)",
                   "debug")
    return added


async def watch() -> None:
    """Top the queue up forever, gently."""
    await asyncio.sleep(45)       # let the first scan and recovery settle
    while True:
        schedules.beat('autoqueue')
        try:
            await top_up()
        except Exception as e:
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            joblog.log(f"auto-queue failed: {type(e).__name__}: {e}", "error")
        from . import workers
        await asyncio.sleep(workers.tune("autoqueue_poll_s"))


def prune_noop() -> int:
    """Resolve queued jobs whose stored plan already says there is no work.

    These are files queued before the enqueue path learned to short-circuit.
    Their plan was computed from a real probe and says 'not needed', so
    dispatching them can only re-probe, re-reach the same conclusion and
    finish as 'skipped' - measured at ~1.6 s of worker time each, and 514 of
    them were sitting in a 739-deep queue (69.6%).

    Uses the STORED plan only. Nothing is re-probed and no file is touched;
    this just applies a decision that was already made and recorded.
    """
    import json as _json

    resolved = 0
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT job_id, file_id, plan_json, title FROM jobs "
            "WHERE state='queued' AND plan_json IS NOT NULL "
            "AND file_id IS NOT NULL")]
    victims = []
    for r in rows:
        try:
            p = _json.loads(r["plan_json"]) or {}
        except Exception:
            continue
        if p.get("skip_reason") or not p.get("needed"):
            victims.append(r)
    if not victims:
        return 0
    now = time.time()
    with cursor() as cur:
        for r in victims:
            cur.execute("UPDATE files SET state='done', "
                        "state_reason=?, processed_at=? WHERE id=?",
                        (r["plan_json"] and (
                            _json.loads(r["plan_json"]).get("skip_reason")
                            or "no work needed"), now, r["file_id"]))
            cur.execute("DELETE FROM jobs WHERE job_id=?", (r["job_id"],))
            resolved += 1
    joblog.log(f"resolved {resolved} queued job(s) that had no work to do - "
               f"their plans already said so, so they were removed rather than "
               f"dispatched", "ok")
    return resolved


def clear(source: str) -> int:
    """Remove QUEUED jobs of one origin. Running work is never touched."""
    src = (source or "").lower()
    with cursor() as cur:
        if src == "all":
            cur.execute("DELETE FROM jobs WHERE state='queued'")
        else:
            cur.execute("DELETE FROM jobs WHERE state='queued' "
                        "AND COALESCE(source,'manual')=?", (src,))
        return cur.rowcount
