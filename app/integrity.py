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
         "last_error": "", "tested": 0, "found": 0}

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
    args = [_ffmpeg(), "-hide_banner", "-v", "error", "-xerror", "-nostdin"]
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
    try:
        from . import idle as _idle
        _p = _idle.progress(KEY)
    except Exception:                                            # noqa: BLE001
        _p = {}
    _live = bool(_p.get("running") or _p.get("paused")) or STATE["running"]
    out = {"ok": 0, "corrupt": 0, "untested": untested(), "mode": mode(),
           "last_run": max(STATE["last_run"], _p.get("last_run") or 0.0),
           "running": _live,
           "now": (_p.get("now") or STATE["now"]) if _live else "",
           "done": _p.get("done") or STATE["done"],
           "total": _p.get("total") or STATE["total"],
           "paused": bool(_p.get("paused")),
           "paused_why": _p.get("paused_why") or "",
           "idle": _p,
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


async def watch() -> None:
    """Work through the untested files for as long as the box can spare it."""
    from . import idle
    await asyncio.sleep(120)
    await idle.run(KEY, TITLE, _pending, _do_one,
                   label=lambda r: os.path.basename(r.get("path") or "")[:120],
                   disk_of=lambda r: r.get("pool_disk") or "",
                   note_of=lambda r: "decoding both ends",
                   system_name="Does it decode?",
                   goto="/settings#health", on_pass=_after)
