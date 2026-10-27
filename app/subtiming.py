r"""nuarr - subtitle timing: find text subtitles that do not keep time with the speech, and fix them.

WHY. A subtitle can be the right words for the right episode and still be
wrong: every line two seconds late, or in step for the first scene and three
seconds behind by the end. Nothing else in nuarr looks at WHEN a line shows,
only at what it says and what it is for. Blue Skies S01E01 (NoRBiT WEB) had
both English tracks drifting 1 part in 1001 - timed against a 24 fps copy
of an episode that plays at 23.976 - so by minute 50 every line came 2.8 s
after it was spoken.

HOW IT MEASURES - see subtiming_worker. Speech is found in the audio with the
voice detector that ships with faster-whisper (CPU, about 15 s an episode),
each text track's spoken lines are lined up against it, every known
frame-rate mismatch and every offset up to 30 s is tried, and the file is
then checked five minutes at a time, before and after the correction.

WHAT IT DOES WITH THE ANSWER
  ok       in step - recorded, not looked at again until the file changes
  offset   every line early or late by the same amount
  drift    a frame-rate mismatch - further out each minute
           both are FIXED in auto: mkvmerge --sync moves (and stretches) that
           track's timestamps; nothing is converted, the track keeps its
           codec, its title, its flags. The rebuilt file is measured again and
           only replaces the original if every fixed track is now in step.
  messy    out of step but no single correction fixes it - another cut, a
           missing scene. Left alone and shown on the page.
  unsure   too little speech to tell (music, silence, a raw with signs only)

Picture tracks (PGS, VobSub) are not measured; forced and signs-only tracks
are skipped - they are not meant to follow the dialogue.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

from . import joblog
from .config import NO_WINDOW, hidden_si
from .db import cursor, kv_get, kv_set

KV_MODE = "subtiming.mode"          # off | advise | auto
KV_PER_HOUR = "subtiming.per_hour"
KV_PAUSED = "subtiming.paused"
DEFAULT_MODE = "auto"
DEFAULT_PER_HOUR = 60
TICK_S = 120.0
WORKER_TIMEOUT = 1800
TEXT_LIKE = tuple(f'%"codec_name":"{c}"%' for c in ("subrip", "ass", "ssa", "webvtt", "mov_text"))
_READY = False
_LOCK = threading.Lock()
STATE: dict = {"running": False, "file_id": 0, "label": "", "stage": "", "pct": 0.0, "started": 0.0,
               "last_run": 0.0, "last_result": "", "next_run": 0.0, "hour_count": 0, "hour_start": 0.0}
_STOP = {"flag": False}
_WAIT: dict = {"at": 0.0, "n": -1}


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS subtiming(
            file_id INTEGER PRIMARY KEY, at REAL NOT NULL, mtime REAL NOT NULL DEFAULT 0,
            path TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '', tracks TEXT NOT NULL DEFAULT '[]', secs REAL NOT NULL DEFAULT 0)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_subtiming_at ON subtiming(at)")
    _READY = True


def mode() -> str:
    m = str(kv_get(KV_MODE) or DEFAULT_MODE)
    return m if m in ("off", "advise", "auto") else DEFAULT_MODE


def set_mode(m: str) -> str:
    if m not in ("off", "advise", "auto"):
        raise ValueError("mode is off, advise or auto")
    kv_set(KV_MODE, m)
    return m


def per_hour() -> int:
    try:
        return max(1, min(600, int(kv_get(KV_PER_HOUR) or DEFAULT_PER_HOUR)))
    except Exception:                                            # noqa: BLE001
        return DEFAULT_PER_HOUR


def set_per_hour(n: int) -> int:
    kv_set(KV_PER_HOUR, str(max(1, min(600, int(n)))))
    return per_hour()


def paused() -> bool:
    return str(kv_get(KV_PAUSED) or "0") == "1"


def set_paused(on: bool) -> None:
    kv_set(KV_PAUSED, "1" if on else "0")


def _label(path: str) -> str:
    try:
        from .db import label_for
        with cursor() as cur:
            r = cur.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        return label_for(path=path, file_id=(r["id"] if r else None))
    except Exception:                                            # noqa: BLE001
        b = os.path.splitext(os.path.basename(path))[0]
        return b.split(" [")[0][:90]


# ------------------------------------------------------------------ queue ----
# NOT "LEFT JOIN subtiming ... (t.file_id IS NULL OR ABS(...) > 2)": for that
# shape the planner nested a full scan of subtiming inside the files loop -
# minutes a tick, every two minutes, so the checker managed 35 an hour
# against a cap of 200. NOT EXISTS is a key lookup per file: 3.4 s.
_UNCHECKED = "NOT EXISTS (SELECT 1 FROM subtiming t WHERE t.file_id=f.id AND ABS(t.mtime - f.mtime) <= 2)"


def candidates(limit: int = 20, skip_disks: set | None = None) -> list:
    """Files with a text subtitle track, not yet checked as they are now -
    newest first, so what was just imported is looked at within minutes.
    skip_disks: hot disks left out in the query itself - the newest files
    mostly land on one disk, and slicing after the fact left one or two."""
    init()
    like = " OR ".join(["p.json LIKE ?"] * len(TEXT_LIKE))
    skip = sorted(d for d in (skip_disks or set()) if d)
    skip_sql = f"AND COALESCE(f.pool_disk,'') NOT IN ({','.join('?' * len(skip))}) " if skip else ""
    with cursor() as cur:
        rows = cur.execute(
            f"SELECT f.id, f.path, f.mtime, f.pool_disk, f.duration FROM files f JOIN file_probes p ON p.file_id=f.id "
            f"WHERE f.state NOT IN ('deleted','duplicate') AND f.duration > 300 {skip_sql}"
            f"AND {_UNCHECKED} "
            f"AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=f.id AND j.state IN ('queued','running','pending')) "
            f"AND ({like}) "
            f"ORDER BY COALESCE(f.first_seen, 0) DESC LIMIT ?", (*skip, *TEXT_LIKE, int(limit))).fetchall()
    return [dict(r) for r in rows]


def waiting() -> int:
    """How many are left - counted at most every ten minutes (a LIKE over every probe)."""
    if time.time() - _WAIT["at"] < 600 and _WAIT["n"] >= 0:
        return _WAIT["n"]
    init()
    like = " OR ".join(["p.json LIKE ?"] * len(TEXT_LIKE))
    with cursor() as cur:
        n = cur.execute(
            f"SELECT COUNT(*) n FROM files f JOIN file_probes p ON p.file_id=f.id "
            f"WHERE f.state NOT IN ('deleted','duplicate') AND f.duration > 300 AND {_UNCHECKED} "
            f"AND ({like})", TEXT_LIKE).fetchone()["n"]
    _WAIT.update(at=time.time(), n=int(n))
    return _WAIT["n"]


# ---------------------------------------------------------------- measure ----
def _measure(path: str, tracks: str = "", check_only: bool = False) -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[:-5] + ".exe" if os.path.exists(py[:-5] + ".exe") else py
    cmd = [py, "-m", "app.subtiming_worker", "--src", path]
    if tracks:
        cmd += ["--tracks", tracks]
    if check_only:
        cmd += ["--check-only"]
    from . import outside as _out
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", cwd=root, creationflags=NO_WINDOW | 0x00004000,   # below normal
                         startupinfo=hidden_si(), stdin=subprocess.DEVNULL)
    _out.set_pid("subtiming", p.pid)
    # PROGRESS AS IT GOES: the worker says "PROG i/n what" on stderr per piece
    base, span = (82.0, 8.0) if check_only else (5.0, 30.0)
    errs = []

    def _err():
        for ln in p.stderr:
            if ln.startswith("PROG "):
                try:
                    i, n = ln.split()[1].split("/")
                    STATE["pct"] = base + span * int(i) / max(1, int(n))
                    if ln.count(" ") >= 2:
                        STATE["stage"] = ln.split(" ", 2)[2].strip()
                except Exception:                                # noqa: BLE001
                    pass
            else:
                errs.append(ln)
                del errs[:-20]
    th = threading.Thread(target=_err, daemon=True)
    th.start()
    out = ""
    try:
        for ln in p.stdout:
            out += ln
        p.wait(timeout=WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
    th.join(5)
    try:
        return json.loads((out or "").strip().splitlines()[-1])
    except Exception:                                            # noqa: BLE001
        return {"ok": False, "why": ([e.strip() for e in errs if e.strip()] or ["the measurement said nothing"])[-1][:300]}


def _ratio(k: float) -> str:
    """mkvmerge's o/p for a factor - exact for the mismatches we know."""
    from fractions import Fraction
    return str(Fraction(k).limit_denominator(100000)).replace(" ", "") if k != 1.0 else ""


# -------------------------------------------------------------------- fix ----
def fix(file_id: int, path: str, tracks: list) -> dict:
    """Rewrite the file with each named track's timing corrected; measure the
    result; replace the original only if every one is now in step."""
    from . import fileops
    from .subembed import _mkvmerge, have_mkvmerge, _live_sub_tracks, _run_reporting, _plain, _size
    if os.path.splitext(path)[1].lower() != ".mkv":
        return {"ok": False, "why": "not a Matroska file - mkvmerge cannot rewrite it in place"}
    if not have_mkvmerge():
        return {"ok": False, "why": "mkvmerge is not installed"}
    if fileops.is_locked(path):
        return {"ok": False, "why": "the file is in use"}
    live = {t["ord"]: t for t in _live_sub_tracks(path)}
    cmd_sync = []
    for t in tracks:
        m = live.get(t["ord"])
        if m is None:
            return {"ok": False, "why": f"subtitle track {t['ord'] + 1} is not where it was measured"}
        spec = f"{m['id']}:{int(round(t['offset'] * 1000))}"
        if t["rate"] != 1.0:
            spec += "," + _ratio(t["rate"])
        cmd_sync += ["--sync", spec]
    ok_room, why_room = fileops.cache_room(_size(path))
    if not ok_room:
        return {"ok": False, "why": why_room}
    tmp = fileops.cache_temp(".mkv", "subtiming")
    with fileops.cache_reserve(_size(path)):
        def _rep(p):
            STATE["pct"] = 40 + 0.4 * float(p or 0)
        STATE["stage"] = "rewriting the timing"
        from . import outside as _out
        r = _run_reporting([_mkvmerge(), "-o", tmp] + cmd_sync + [path], _rep,
                           on_pid=lambda pid: _out.set_pid("subtiming", pid))
        if r.returncode >= 2 or not os.path.exists(tmp):
            fileops._quiet_remove(tmp)
            return {"ok": False, "why": (r.stderr or r.stdout or "mkvmerge failed").strip()[:300]}
        # EXIT 0 IS NOT PROOF: measure the rebuilt file the same way.
        STATE.update(stage="measuring the result", pct=82)
        chk = _measure(tmp, ",".join(str(t["ord"]) for t in tracks), check_only=True)
        bad = [t for t in (chk.get("tracks") or []) if t.get("verdict") != "ok"]
        if not chk.get("ok") or bad or len(chk.get("tracks") or []) != len(tracks):
            fileops._quiet_remove(tmp)
            why = "; ".join(f"track {t['ord'] + 1}: {t.get('why', '')}" for t in bad) or chk.get("why") or "the check failed"
            return {"ok": False, "why": "the corrected file still is not in step - nothing was changed (" + why + ")"}
        STATE.update(stage="putting it back", pct=90)
        from .jobs import outside_commit as _oc
        res = fileops.safe_replace(path, tmp, **_oc(path, "subtiming"))
        fileops._quiet_remove(tmp)
        if not getattr(res, "ok", False):
            return {"ok": False, "why": f"could not put the rebuilt file in place: {getattr(res, 'why', '')}"}
    # a different file now: re-probe, new size and mtime
    try:
        from . import jobs as _jobs
        _r = _plain([_jobs._ffprobe_exe(), "-v", "quiet", "-print_format", "json", "-show_streams",
                     "-show_format", path], 180)
        _d = json.loads(_r[1] or "{}")
        if _d.get("streams"):
            _jobs.cache_probe(int(file_id), _d)
    except Exception:                                            # noqa: BLE001
        pass
    mt = os.path.getmtime(path)
    with cursor() as cur:
        cur.execute("UPDATE files SET size=?, mtime=?, updated_at=? WHERE id=?",
                    (_size(path), mt, time.time(), int(file_id)))
    return {"ok": True, "mtime": mt, "after": chk.get("tracks")}


# ----------------------------------------------------------------- one file --
def check_one(row: dict, act: bool | None = None) -> dict:
    """Measure one file, fix what can be fixed (auto), record it."""
    init()
    fid, path = int(row["id"]), row["path"]
    act = (mode() == "auto") if act is None else act
    t0 = time.time()
    from . import outside as _out
    _out.start("subtiming", fid)
    STATE.update(running=True, file_id=fid, label=_label(path), stage="listening for speech", pct=2.0, started=t0)
    try:
        if not os.path.exists(path):
            # RENAMED SINCE THE BATCH WAS PICKED - an encode committing, the
            # Subtitle maker embedding. Ask by id; the files row follows renames.
            with cursor() as cur:
                r = cur.execute("SELECT path, mtime FROM files WHERE id=? AND state NOT IN ('deleted','duplicate')",
                                (fid,)).fetchone()
            if r and r["path"] != path and os.path.exists(r["path"]):
                path = r["path"]
                row["mtime"] = r["mtime"]
                STATE["label"] = _label(path)
        if not os.path.exists(path):
            # NOT RECORDED: a "failed" row with the file's mtime would never be
            # offered again, and eleven of the first twelve such files were
            # back under a new name within the hour. The scan marks a file that
            # is really gone as deleted, and candidates() leaves those out.
            joblog.log(f"subtitle timing: {_label(path)} - not on disk, left for the next scan", "info")
            STATE.update(last_run=time.time(), last_result=f"{_label(path)}: not on disk - left for the next scan")
            return {"file_id": fid, "verdict": "skipped", "action": "not on disk", "note": "", "secs": 0}
        res = _measure(path)
        tracks = res.get("tracks") or []
        verdict, action, note = "", "", ""
        if not res.get("ok"):
            verdict, action, note = "failed", "not measured", res.get("why") or ""
        else:
            kinds = [t.get("verdict") for t in tracks]
            fixable = [t for t in tracks if t.get("verdict") in ("offset", "drift")]
            verdict = ("drift" if "drift" in kinds else "offset" if "offset" in kinds else "messy" if "messy" in kinds
                       else "ok" if "ok" in kinds else "unsure" if "unsure" in kinds else "skipped")
            if fixable and act:
                f = fix(fid, path, [{"ord": t["ord"], "offset": float(t["offset"]), "rate": float(t["rate"])}
                                    for t in fixable])
                if f.get("ok"):
                    action = f"fixed {len(fixable)} track(s)"
                    row["mtime"] = f["mtime"]
                    after = {t["ord"]: t for t in (f.get("after") or [])}
                    for t in fixable:
                        t["fixed"] = True
                        t["check"] = (after.get(t["ord"]) or {}).get("before") or []
                    joblog.log(f"subtitle timing: fixed {len(fixable)} track(s) in {_label(path)} - "
                               + "; ".join(f"track {t['ord'] + 1} {t['why']}" for t in fixable), "ok")
                else:
                    action, note = "fix failed", f.get("why") or ""
                    joblog.log(f"subtitle timing: {_label(path)} - could not fix: {note}", "warn")
            elif fixable:
                action = "found - waiting for you (advise mode)"
            elif verdict == "messy":
                action = "left alone"
                joblog.log(f"subtitle timing: {_label(path)} is out of step in a way one correction cannot fix", "warn")
            else:
                action = "nothing to do"
        mt = float(row.get("mtime") or (os.path.getmtime(path) if os.path.exists(path) else 0))
        secs = round(time.time() - t0, 1)
        with cursor() as cur:
            cur.execute("INSERT OR REPLACE INTO subtiming(file_id, at, mtime, path, label, verdict, action, note, tracks, secs) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (fid, time.time(), mt, path, _label(path), verdict, action, note, json.dumps(tracks), secs))
        STATE.update(last_run=time.time(), last_result=f"{_label(path)}: {verdict} - {action}")
        return {"file_id": fid, "verdict": verdict, "action": action, "note": note, "secs": secs}
    finally:
        STATE.update(running=False, stage="", pct=0.0)
        _out.stop("subtiming")


def busy_disks() -> set:
    try:
        from . import gate
        return set(gate.plex_disks()) | set(gate.busy_disks())
    except Exception:                                            # noqa: BLE001
        return set()


def run_batch(n: int, file_id: int = 0) -> dict:
    if not _LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        if file_id:
            with cursor() as cur:
                r = cur.execute("SELECT id, path, mtime, pool_disk FROM files WHERE id=?", (int(file_id),)).fetchone()
            rows = [dict(r)] if r else []
        else:
            rows = candidates(n, busy_disks())
        done = []
        for r in rows:
            if _STOP["flag"]:
                break
            done.append(check_one(r))
        if done:
            _WAIT["n"] = max(0, _WAIT["n"] - len(done)) if _WAIT["n"] > 0 else _WAIT["n"]
        return {"done": len(done), "fixed": sum(1 for d in done if d["action"].startswith("fixed")), "results": done}
    finally:
        _STOP["flag"] = False
        _LOCK.release()


def stop() -> bool:
    _STOP["flag"] = True
    return True


def file_by_path(path: str) -> int:
    with cursor() as cur:
        r = cur.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
    return int(r["id"]) if r else 0


async def watch() -> None:
    await asyncio.sleep(90)
    try:
        await asyncio.to_thread(waiting)
    except Exception:                                            # noqa: BLE001
        pass
    await asyncio.sleep(330)
    while True:
        t_tick = time.time()
        STATE["next_run"] = t_tick + TICK_S
        try:
            await asyncio.to_thread(waiting)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            if mode() != "off" and not paused():
                now = time.time()
                if now - STATE["hour_start"] > 3600:
                    STATE.update(hour_start=now, hour_count=0)
                room = per_hour() - STATE["hour_count"]
                if room > 0:
                    n = max(1, min(room, int(per_hour() * TICK_S / 3600 + 0.999)))
                    r = await asyncio.to_thread(run_batch, n)
                    STATE["hour_count"] += int(r.get("done") or 0)
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"subtitle timing: {type(e).__name__}: {e}", "error")
        # THE TICK IS START TO START. Sleeping a full TICK_S after the batch
        # added the batch's own time to every cycle: at 200/hour, 7 files
        # (~80 s) + 120 s = 200 s a cycle, which is 126 an hour, not 200.
        await asyncio.sleep(max(10.0, TICK_S - (time.time() - t_tick)))


def as_running() -> list:
    if not STATE.get("running"):
        return []
    el = time.time() - float(STATE.get("started") or time.time())
    from . import outside as _out
    pct = max(0.0, min(1.0, float(STATE["pct"] or 0) / 100.0))
    return [{"job_id": f"subtiming-{STATE['file_id']}", "pool": "subtiming", "kind": "subtiming",
             "title": STATE["label"], "file": STATE["label"], "stage": STATE["stage"], "progress": pct,
             "elapsed_s": round(el), "eta_s": None, "eta_est": True,
             "plan": "subtitle timing: the speech in the audio against each text track's lines",
             "why_pool": "CPU - the voice detector, then mkvmerge if a track needs moving",
             "doing": {"tool": "Silero VAD + ffmpeg" if pct < 0.4 else "mkvmerge --sync", "hw": "CPU",
                       "why": STATE["stage"]},
             "disk": _out.disk_of(STATE["file_id"]), "dest_disk": "", "fps": 0.0, "speed": 0.0,
             "paused_for_viewer": False, "paused_for_load": False, "cancelled": False, "by_bytes": False,
             **_out.fields("subtiming", pct)}]


def snapshot() -> dict:
    init()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT * FROM subtiming WHERE verdict NOT IN ('ok','skipped','unsure') OR action LIKE 'fixed%' "
            "ORDER BY at DESC LIMIT 60")]
        recent = [dict(r) for r in cur.execute("SELECT file_id, at, label, verdict, action, secs FROM subtiming "
                                                "ORDER BY at DESC LIMIT 15")]
        tot = {r["verdict"]: r["n"] for r in cur.execute("SELECT verdict, COUNT(*) n FROM subtiming GROUP BY verdict")}
        fixed = cur.execute("SELECT COUNT(*) n FROM subtiming WHERE action LIKE 'fixed%'").fetchone()["n"]
        last = [float(r["secs"] or 0) for r in cur.execute("SELECT secs FROM subtiming WHERE secs>0 ORDER BY at DESC LIMIT 20")]
        hour_n = cur.execute("SELECT COUNT(*) n FROM subtiming WHERE at > ?", (time.time() - 3600,)).fetchone()["n"]
    for r in rows:
        try:
            r["tracks"] = json.loads(r["tracks"] or "[]")
        except Exception:                                        # noqa: BLE001
            r["tracks"] = []
        # THE PAGE DRAWS THE SLICES, NOT THE SEARCH. The sums, every slice's
        # match figures and the search's bookkeeping made 60 rows 232 KB on
        # a poll every 30 s; what is drawn is the lag per slice and the words.
        for t in r["tracks"]:
            for k in ("before_sum", "after_sum", "r_before", "r_after", "cues_extra"):
                t.pop(k, None)
            for key in ("before", "after", "check"):
                if isinstance(t.get(key), list):
                    t[key] = [{"t": s.get("t"), "late": s.get("late"), "r": s.get("r")} for s in t[key]]
    wait = _WAIT["n"]          # counted by watch(), never on the page's request
    avg = sum(last) / len(last) if last else 0.0
    eta = 0.0
    if wait > 0 and mode() != "off":
        eta = max(wait * (avg or 15.0), wait / per_hour() * 3600)
    el = (time.time() - float(STATE["started"] or time.time())) if STATE.get("running") else 0.0
    return {"mode": mode(), "per_hour": per_hour(), "paused": paused(), "rows": rows, "recent": recent,
            "total": sum(tot.values()), "by": tot, "fixed": int(fixed), "waiting": wait,
            "avg_secs": round(avg, 1), "hour_n": int(hour_n), "eta_s": round(eta), "elapsed_s": round(el),
            **{k: STATE[k] for k in ("running", "label", "stage", "pct", "last_run", "last_result", "next_run")}}
