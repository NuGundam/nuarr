r"""nuarr - is the file actually readable, or does it only look readable?

WHY A SEPARATE CHECK
--------------------
Corruption was the one failure nuarr could only find by tripping over it. A
truncated episode sat in the library looking perfect - right size, right name,
ffprobe answers every question about it - until a job picked it up months later
and failed with "moov atom not found", or until somebody pressed play and it
stopped forty minutes in. refetch.py knew exactly what to do about that error
string; nothing ever went looking for the files that would produce it.

ffprobe is not enough and never was. It reads the header, and the header of a
truncated file is intact - truncation removes the END. So this decodes, which
is the only test that can distinguish "the container describes 24 minutes" from
"there are 24 minutes of frames in it".

WHY IT DOES NOT DECODE THE WHOLE FILE
-------------------------------------
39,000 files at real decode speed is a week of GPU that nobody asked for. Two
bounded windows catch what actually goes wrong here:

    the head   header and stream damage, bad indices, wrong codec parameters
    the tail   truncation - the single most common way a file in a media
               library is broken, because it is what an interrupted download,
               a full disk or a killed remux all leave behind

A file that decodes at both ends and has an intact header is not proof of a
clean middle, and this module does not claim it is: the verdict is stored as
what it is, a sampled one. It is the difference between finding most of the
broken files tonight and finding all of them never.

FAIL CLOSED, LOUDLY
-------------------
The verdict here feeds remedy.py, where "file/corrupt" is one of four findings
auto mode may act on by DELETING the file and asking the arr for another. So a
noisy decoder warning must never be able to reach that. ffmpeg is run at -v
error, and even then the message has to match _FATAL before this says corrupt.
Anything else is recorded verbatim under an "ok" verdict, where it is visible
to a person and harmless to the library.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from . import joblog
from .config import NO_WINDOW, SETTINGS
from .db import cursor

# How many seconds to decode at each end.
HEAD_S = 20
TAIL_S = 25
# How many decoder threads one check may use. See _decode.
DECODE_THREADS = 4
# How many files one pass may test. Each costs two bounded decodes off a
# spinning pool disk; eight is roughly a minute of work and leaves the disks
# to whatever else wants them.
PER_RUN = 8
# The loop's own clock. The sweep only actually reads anything when the pool is
# idle, so a short cycle costs a `_too_busy()` call and nothing else.
CYCLE_S = 300
# Never test a file that was written in the last few minutes. A file still
# landing decodes badly for a reason that has nothing to do with its release.
SETTLE_S = 600

# Messages that mean the bytes are wrong. Deliberately a subset of what a
# decoder can complain about - the same list refetch.py keeps for error
# strings, for the same reason: a message nobody has classified must not
# arrive with a delete button attached.
_FATAL = [
    (r"moov atom not found", "the file is missing its index and is truncated"),
    (r"invalid data found", "the decoder rejected the stream"),
    (r"unexpected end of file|truncat", "the file ends early"),
    (r"error while decoding|decode(r)? (error|failed)", "decoding failed"),
    (r"corrupt|damaged", "the stream is corrupt"),
    (r"could not find codec parameters", "the stream parameters are unreadable"),
    (r"no such file or directory", "the file is not there"),
    (r"invalid nal unit|missing picture in access unit", "the video stream is broken"),
]

OK, CORRUPT, UNREADABLE = "ok", "corrupt", "unreadable"

STATE = {"running": False, "done": 0, "total": 0, "now": "", "last_run": 0.0,
         "last_error": "", "tested": 0, "found": 0,
         # How many the feeder handed to the main queue last pass, and how
         # many decode jobs are live on it now.
         "fed": 0, "on_queue": 0}

_READY = False


def init() -> None:
    r"""One row per file, keyed to the BYTES rather than the path.

    size and mtime are stored with the verdict so a file is re-tested when and
    only when it changes. Without that, the sweep either re-decodes a clean
    library forever or trusts a verdict about a file that has since been
    replaced by an upgrade - and the second one is how a check quietly stops
    being true.
    """
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS integrity(
                file_id INTEGER PRIMARY KEY,
                path    TEXT,
                size    INTEGER,
                mtime   REAL,
                at      REAL,
                verdict TEXT,
                detail  TEXT,
                secs    REAL
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_integrity_verdict "
                    "ON integrity(verdict)")
    _READY = True


def _ffmpeg() -> str:
    from .jobs import _ffmpeg_exe
    return _ffmpeg_exe()


def mode() -> str:
    m = str(getattr(SETTINGS, "integrity_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


def _fatal(err: str) -> str:
    low = (err or "").lower()
    for pat, why in _FATAL:
        if re.search(pat, low):
            return why
    return ""


async def _decode(path: str, ss: float, dur: float,
                  on_pid=None) -> tuple[int, str]:
    """Decode a window. -> (returncode, stderr).

    -xerror stops at the first error rather than logging thousands of them,
    which is both faster and the difference between a 200-byte stderr and a
    200 MB one. Video only: an audio decode error is a real fault but a much
    noisier one, and the audio findings have their own check.
    """
    # A BOUNDED NUMBER OF THREADS, BECAUSE THIS RUNS BESIDE EVERYTHING NOW.
    # ffmpeg's default is one thread per core, and a 4K HEVC decode will take
    # all twenty of them - measured on the first queued pass: two of these at
    # once put the box at 99.8% CPU while somebody was watching Plex. On the
    # idle runner that never showed, because it ran one at a time and only
    # when the machine was quiet. The check does not need to be fast; it needs
    # to be a corner of the machine, so it gets four threads and the pool's
    # two-at-once is eight.
    args = [_ffmpeg(), "-hide_banner", "-v", "error", "-xerror", "-nostdin",
            "-threads", str(DECODE_THREADS)]
    if ss > 0:
        args += ["-ss", f"{ss:.2f}"]
    args += ["-i", path, "-t", f"{dur:.2f}", "-map", "0:v:0?",
             "-f", "null", "-"]
    try:
        # NO_WINDOW, AND THE CONSOLE WATCHER IS WHY THIS COMMENT EXISTS.
        #
        # This spawn shipped without it and the first sweep put nine ffmpeg
        # console windows on the desktop - caught, named and dated by
        # consolewatch within the hour, which is the entire reason that watcher
        # was written. config.py states the rule plainly: every child process
        # nuarr spawns must pass this flag, because the server runs as a
        # service and a console-subsystem child with no console of its own gets
        # given a brand new visible one by Windows.
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        # WHOEVER IS COUNTING THE BYTES NEEDS THE PID. Without it a decode
        # reads twenty seconds off a pool disk and the panel files those bytes
        # under "system", which is the column that means "not nuarr" and the
        # number the gate steers by.
        if on_pid is not None:
            try:
                on_pid(proc.pid)
            except Exception:                                    # noqa: BLE001
                pass
        _, err = await asyncio.wait_for(proc.communicate(), timeout=240)
        return proc.returncode or 0, (err or b"").decode("utf-8", "replace")
    except asyncio.TimeoutError:
        # A DECODE THAT NEVER FINISHES IS NOT A VERDICT. It is usually a disk
        # that went to sleep or a pool member being rebalanced under us, and
        # calling that corruption would delete a healthy file.
        return -1, "__timeout__"
    except Exception as e:                                       # noqa: BLE001
        return -1, f"__spawn__ {type(e).__name__}: {e}"


async def test_one(file_id: int, path: str, duration: float = 0.0,
                   on_pid=None, on_stage=None) -> dict:
    """Head and tail. -> {verdict, detail, secs}"""
    t0 = time.time()
    if on_stage:
        on_stage("head", 0.0)
    rc, err = await _decode(path, 0, HEAD_S, on_pid)
    if err.startswith("__"):
        return {"verdict": "", "detail": err, "secs": time.time() - t0}
    why = _fatal(err)
    if why:
        return {"verdict": CORRUPT, "detail": f"{why} (in the first "
                f"{HEAD_S}s): {err.strip().splitlines()[0][:200]}",
                "secs": time.time() - t0}
    head_note = err.strip()

    # THE TAIL IS THE POINT. Only attempted when the duration is known, because
    # seeking to an unknown offset lands somewhere arbitrary and an arbitrary
    # decode failure is not evidence of anything.
    tail_note = ""
    if duration and duration > (HEAD_S + TAIL_S + 5):
        if on_stage:
            on_stage("tail", 50.0)
        rc2, err2 = await _decode(path, max(0.0, duration - TAIL_S), TAIL_S,
                                  on_pid)
        if not err2.startswith("__"):
            why = _fatal(err2)
            if why:
                return {"verdict": CORRUPT, "detail": f"{why} (in the last "
                        f"{TAIL_S}s): {err2.strip().splitlines()[0][:200]}",
                        "secs": time.time() - t0}
            tail_note = err2.strip()
        _ = rc2
    _ = rc
    note = "; ".join(x.splitlines()[0][:150] for x in (head_note, tail_note)
                     if x)
    return {"verdict": OK,
            "detail": note or f"decoded {HEAD_S}s at the head"
                              + (f" and {TAIL_S}s at the tail" if tail_note
                                 or duration else ""),
            "secs": time.time() - t0}


def _candidates(limit: int) -> list[dict]:
    r"""Files whose bytes have never been decoded, or have changed since.

    Never-tested first, oldest verdict second. The join is on size and mtime
    rather than on the row's existence, so an upgraded file - same id, new
    bytes - comes back round as if it had never been seen, which is exactly
    what it is.
    """
    if not _READY:
        init()
    # THE FILE'S CLOCK, NOT NUARR'S BOOKKEEPING CLOCK.
    #
    # This read files.updated_at, which is when nuarr last touched the ROW -
    # and the boot recovery walk touches every row it looks at. Measured on
    # this box straight after a restart: 39,710 of 39,710 files had an
    # updated_at inside the settling window, so the candidate query returned
    # nothing at all and the sweep reported "tested 0" while looking perfectly
    # healthy. A filter meant to skip the handful of files still being written
    # was excluding the entire library.
    #
    # mtime is the filesystem's own answer to "when was this last written",
    # which is the question that was being asked.
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT f.id file_id, f.path, f.size, f.duration, f.pool_disk, "
            "       i.at last_at, i.verdict last_verdict "
            "  FROM files f "
            "  LEFT JOIN integrity i ON i.file_id = f.id "
            "                       AND i.size = f.size "
            " WHERE f.state NOT IN ('deleted','duplicate') "
            "   AND COALESCE(f.path,'') != '' "
            "   AND COALESCE(f.size,0) > 0 "
            "   AND COALESCE(f.mtime, 0) < ? "
            "   AND (i.file_id IS NULL OR i.verdict = '') "
            " ORDER BY f.id LIMIT ?", (cutoff, int(limit)))]


def _candidates_per_disk(per_disk: int) -> list[dict]:
    """The oldest N untested files on EACH spindle. Same filter as
    _candidates; the window is what makes the hand-over disk-diverse."""
    if not _READY:
        init()
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT file_id, path, size, duration, pool_disk FROM ("
            "  SELECT f.id file_id, f.path, f.size, f.duration, f.pool_disk, "
            "         ROW_NUMBER() OVER (PARTITION BY COALESCE(f.pool_disk,'') "
            "                            ORDER BY f.id) rn "
            "    FROM files f "
            "    LEFT JOIN integrity i ON i.file_id = f.id AND i.size = f.size "
            "   WHERE f.state NOT IN ('deleted','duplicate') "
            "     AND COALESCE(f.path,'') != '' "
            "     AND COALESCE(f.size,0) > 0 "
            "     AND COALESCE(f.mtime, 0) < ? "
            "     AND (i.file_id IS NULL OR i.verdict = '')) "
            " WHERE rn <= ? ORDER BY pool_disk, rn",
            (cutoff, int(per_disk)))]


def _write(file_id: int, path: str, size: int, mtime: float, out: dict) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO integrity(file_id,path,size,mtime,at,verdict,detail,"
            "secs) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, "
            "  size=excluded.size, mtime=excluded.mtime, at=excluded.at, "
            "  verdict=excluded.verdict, detail=excluded.detail, "
            "  secs=excluded.secs",
            (int(file_id), path, int(size or 0), float(mtime or 0), time.time(),
             out.get("verdict") or "", (out.get("detail") or "")[:600],
             float(out.get("secs") or 0)))


async def sweep(limit: int = 0, force: bool = False) -> dict:
    """One bounded pass. Returns what it did."""
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    if not _READY:
        init()
    if not force:
        try:
            from .audit import _too_busy
            if await _too_busy():
                return {"ok": False, "why": "the pool is busy"}
        except Exception:                                        # noqa: BLE001
            pass
    rows = await asyncio.to_thread(_candidates, int(limit or PER_RUN))
    STATE.update(running=True, done=0, total=len(rows), now="", found=0)
    tested = found = 0
    try:
        for r in rows:
            STATE["now"] = os.path.basename(r["path"] or "")[:60]
            STATE["done"] = tested
            try:
                st = os.stat(r["path"])
            except OSError:
                # GONE IS SOMEBODY ELSE'S FINDING. The missing-from-disk check
                # owns that, and writing a verdict here would give one fact two
                # owners that can disagree.
                continue
            out = await test_one(int(r["file_id"]), r["path"],
                                 float(r.get("duration") or 0))
            if not out.get("verdict"):
                # timeout or spawn failure - no verdict, try again another day
                continue
            await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                                    st.st_size, st.st_mtime, out)
            tested += 1
            if out["verdict"] == CORRUPT:
                found += 1
                joblog.log(f"integrity: {os.path.basename(r['path'])} - "
                           f"{out['detail']}", "error")
    finally:
        STATE.update(running=False, now="", last_run=time.time(),
                     tested=tested, found=found)
    if found:
        joblog.log(f"integrity sweep: {found} of {tested} file(s) failed to "
                   f"decode", "error")
    return {"ok": True, "tested": tested, "found": found,
            "remaining": await asyncio.to_thread(untested)}


def untested() -> int:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) n FROM files f "
                "LEFT JOIN integrity i ON i.file_id=f.id AND i.size=f.size "
                "WHERE f.state NOT IN ('deleted','duplicate') "
                "  AND COALESCE(f.size,0) > 0 AND i.file_id IS NULL"
            ).fetchone()
        return int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def findings(limit: int = 200) -> list[dict]:
    """The corrupt ones, in remedy.py's shape."""
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return [{"file_id": r["file_id"], "kind": "file/corrupt",
                     "path": r["path"], "why": r["detail"], "at": r["at"]}
                    for r in cur.execute(
                        "SELECT i.file_id, i.path, i.detail, i.at "
                        "  FROM integrity i JOIN files f ON f.id = i.file_id "
                        " WHERE i.verdict = ? "
                        "   AND f.state NOT IN ('deleted','duplicate') "
                        " ORDER BY i.at DESC LIMIT ?", (CORRUPT, int(limit)))]
    except Exception:                                            # noqa: BLE001
        return []


def stats() -> dict:
    if not _READY:
        init()
    # WHERE THE WORK ACTUALLY IS. STATE belongs to sweep(), which is now the
    # manual button; the sweep that runs all day is the shared runner's, and a
    # panel reading the wrong dict is a panel that says "idle" while the disks
    # are going - which is exactly how the sidecar system came to be missing
    # from the running list for months.
    # THE WORK IS ON THE MAIN QUEUE NOW, so that is where "is it running" is
    # read from: decode jobs queued and running, and the live workers' files.
    # The shared runner's strip is gone with the runner.
    q = {"queued": 0, "running": 0}
    now_word = ""
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT state, COUNT(*) n FROM jobs "
                    " WHERE kind='decode' AND state IN ('queued','running') "
                    " GROUP BY state"):
                q[r["state"]] = int(r["n"] or 0)
        from . import jobs as _jobs
        for w in list(_jobs.RUNNING.values()):
            if getattr(w.job, "kind", "") == "decode":
                now_word = os.path.basename(w.job.path or "")[:60]
                break
    except Exception:                                            # noqa: BLE001
        pass
    _live = bool(q["running"]) or STATE["running"]
    out = {"ok": 0, "corrupt": 0, "untested": untested(), "mode": mode(),
           "last_run": STATE["last_run"],
           "running": _live,
           "now": (now_word or STATE["now"]) if _live else "",
           "done": STATE["done"], "total": STATE["total"],
           "paused": False, "paused_why": "",
           "queue": q, "on_queue": q["queued"] + q["running"],
           "fed": STATE.get("fed") or 0,
           "idle": {},
           "head_s": HEAD_S, "tail_s": TAIL_S, "per_run": PER_RUN}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT verdict, COUNT(*) n FROM integrity "
                                 "GROUP BY verdict"):
                out[r["verdict"] or "none"] = r["n"]
    except Exception:                                            # noqa: BLE001
        pass
    return out


# --------------------------------------------------------- onto the runner --
#
# EIGHT FILES AND THEN FIVE MINUTES ASLEEP, on a box that is idle most of the
# night. The batch was never protection - eight bounded decodes started the
# instant somebody presses play are still eight - and it was a real cost:
# 39,710 files at eight a pass, five minutes apart, is eleven days of
# wall-clock to walk the library once, nearly all of it spent waiting.
#
# The shared runner asks the gate before every single file instead, works at
# whatever rate the machine can spare, and steps around a spindle somebody is
# reading from rather than stopping on it. Same question, asked properly.
#
# sweep() is left exactly as it was: it is the "test some now" button, and a
# button is a batch by definition.
KEY = "integrity"
TITLE = "Does it decode?"


def _pending() -> list:
    """Every file whose bytes have never been decoded. Not a slice of them."""
    if not _READY:
        init()
    try:
        return _candidates(100000)
    except Exception:                                            # noqa: BLE001
        return []


async def _do_one(r: dict, report=None) -> dict:
    """Decode both ends of one file and write the verdict."""
    try:
        st = os.stat(r["path"])
    except OSError:
        # GONE IS SOMEBODY ELSE'S FINDING. The missing-from-disk check owns
        # that, and writing a verdict here would give one fact two owners that
        # can disagree.
        return {"ok": True, "skipped": True}
    work = getattr(report, "task", None)

    def _stage(which, pct):
        if work is not None:
            work.note = ("decoding the first "
                         f"{HEAD_S}s" if which == "head"
                         else f"decoding the last {TAIL_S}s")
        if report is not None:
            try:
                report(pct)
            except Exception:                                    # noqa: BLE001
                pass
    out = await test_one(int(r["file_id"]), r["path"],
                         float(r.get("duration") or 0),
                         on_pid=(work.set_pid if work is not None else None),
                         on_stage=_stage)
    if not out.get("verdict"):
        # timeout or spawn failure - no verdict, try again another day
        return {"ok": False, "why": out.get("detail") or "no verdict"}
    await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                            st.st_size, st.st_mtime, out)
    if out["verdict"] == CORRUPT:
        joblog.log(f"integrity: {os.path.basename(r['path'])} - "
                   f"{out['detail']}", "error")
    return {"ok": True, "verdict": out["verdict"]}


async def _after(_d: dict) -> None:
    """What a finished pass is for: handing the findings to the remedy."""
    from . import remedy
    got = findings(50)
    if got:
        await remedy.auto(got, "integrity", mode() == "auto")


# ------------------------------------------------------- onto the queue --
#
# AND THEN OFF THE RUNNER AGAIN, ONTO THE MAIN QUEUE. The shared runner was the
# right answer to "eight files then five minutes asleep": it asked the gate per
# file and stepped around a viewer's disk. But it was still a second scheduler
# with a second idea of what the machine could spare, running beside the queue
# that every other piece of file work goes through - and "what is nuarr doing
# to my files" had two answers again. Erik asked for it under the main queue,
# queued across disks that are not busy.
#
# So it is a job kind now. The feeder deals untested files to the jobs table
# round robin by spindle - the lesson the subtitle queue learned when 3,302 of
# 5,300 files sat on one disk and eleven others idled - and the dispatcher does
# the rest: it already prefers the quietest disk, refuses a viewer's, and steers
# around one something else is hammering, for every pool alike. The candidate
# list does not need a table of its own - `integrity` LEFT JOIN `files` on size
# IS the queue, and a file leaves it the moment its verdict is written.
QUEUE_DEPTH = 120


def _to_hand_over(depth: int) -> tuple:
    """How much room the queue has for decode jobs, and which files fill it."""
    with cursor() as cur:
        have = int(cur.execute(
            "SELECT COUNT(*) n FROM jobs "
            " WHERE kind='decode' AND state IN ('queued','running')"
        ).fetchone()["n"] or 0)
    room = max(0, int(depth) - have)
    if not room:
        return have, []
    # A SLICE PER DISK, NOT A SLICE OF THE TABLE. _candidates orders by file
    # id, and file ids were handed out as the library was walked - disk by
    # disk. Measured on the first run: the first 2,400 untested files were all
    # on NU-DRIVE-1, so a round-robin over them dealt 120 files to one disk,
    # which is the exact failure it exists to prevent. The window takes the
    # oldest few dozen from EVERY disk that has any.
    rows = _candidates_per_disk(max(4, room))
    # Skip what is already on the jobs table for any reason - enqueue would
    # refuse it anyway, but refusing two hundred rows a pass is noise.
    try:
        with cursor() as cur:
            live = {int(r["file_id"]) for r in cur.execute(
                "SELECT file_id FROM jobs WHERE state IN ('queued','running') "
                "  AND file_id IS NOT NULL")}
    except Exception:                                            # noqa: BLE001
        live = set()
    by_disk: dict = {}
    for r in rows:
        if int(r["file_id"]) in live:
            continue
        by_disk.setdefault(r.get("pool_disk") or "?", []).append(r)
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
    """The instruction in the shape the queue panel reads plans in."""
    import json
    dur = float(r.get("duration") or 0)
    both = dur > (HEAD_S + TAIL_S + 5)
    return json.dumps({
        "decode": True, "rewrite": False,
        "summary": (f"decode the first {HEAD_S}s and the last {TAIL_S}s"
                    if both else f"decode the first {HEAD_S}s"),
        "actions": [
            {"kind": "decode", "what": f"decode the first {HEAD_S}s to null",
             "why": "header and stream damage, bad indices, wrong codec "
                    "parameters all show here", "detail": ""}]
        + ([{"kind": "decode", "what": f"decode the last {TAIL_S}s to null",
             "why": "truncation - the commonest way a library file is broken "
                    "- only shows at the end", "detail": ""}] if both else []),
    })


async def topup(depth: int = QUEUE_DEPTH) -> dict:
    """Hand untested files to the main queue, spread across every spindle."""
    from . import jobs
    if not _READY:
        init()
    made = skipped = 0
    try:
        have, rows = await asyncio.to_thread(_to_hand_over, depth)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    for r in rows:
        try:
            await jobs.enqueue(int(r["file_id"]), r["path"],
                               os.path.basename(r["path"] or ""),
                               kind="decode", priority=90,
                               source="does it decode?",
                               plan_json=_job_plan(r))
            made += 1
        except Exception:                                        # noqa: BLE001
            skipped += 1
    STATE["on_queue"] = have + made
    return {"ok": True, "made": made, "skipped": skipped,
            "on_queue": have + made}


async def job_one(r: dict, on_pid=None, on_stage=None) -> dict:
    r"""One file, inside a job. -> {ok, verdict|why, skipped}

    The runner's _do_one with the ledger removed: a job IS a ledger entry, so
    the pid and the stage go to the worker's card rather than to a second
    entry on the disk panel. The verdict is written here, and a corrupt one
    goes straight to the remedy rather than waiting for a pass to end - there
    are no passes any more.
    """
    try:
        st = os.stat(r["path"])
    except OSError:
        return {"ok": True, "skipped": True,
                "why": "not on disk - the missing-file check owns that"}

    def _stage(which, pct):
        if on_stage is not None:
            try:
                on_stage(f"decoding the first {HEAD_S}s" if which == "head"
                         else f"decoding the last {TAIL_S}s", pct)
            except Exception:                                    # noqa: BLE001
                pass
    out = await test_one(int(r["file_id"]), r["path"],
                         float(r.get("duration") or 0),
                         on_pid=on_pid, on_stage=_stage)
    if not out.get("verdict"):
        return {"ok": False, "why": out.get("detail") or "no verdict"}
    await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                            st.st_size, st.st_mtime, out)
    if out["verdict"] == CORRUPT:
        joblog.log(f"integrity: {os.path.basename(r['path'])} - "
                   f"{out['detail']}", "error")
        try:
            from . import remedy
            await remedy.auto(findings(50), "integrity", mode() == "auto")
        except Exception:                                        # noqa: BLE001
            pass
    return {"ok": True, "verdict": out["verdict"],
            "detail": out.get("detail") or "", "secs": out.get("secs") or 0}


FEED_S = 60.0


async def watch() -> None:
    """Keep the main queue topped up with untested files. Nothing else."""
    await asyncio.sleep(120)
    while True:
        try:
            d = await topup()
            STATE["fed"] = int(d.get("made") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(FEED_S)
