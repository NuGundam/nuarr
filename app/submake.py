r"""nuarr - the subtitle maker: English subtitles for a file that has none.

WHY. A raw - a film spoken in a language nobody here follows, with nothing to
read - had one remedy: blocklist it and hope another release has subtitles.
For a film only ever released one way there is no other release. Erik,
2026-10-14, on Sumikko Gurashi (one AMZN WEB-DL, no subtitles anywhere): make
them. submake_worker.py does the listening, reading and translating; this
module queues the work, runs one file at a time, and decides what happens to
the result.

THE RESULT FOLLOWS THE SUBTITLE RULES, NOT ITS OWN. The worker writes an
English sidecar beside the file - "<name>.en.srt", which Plex offers as
English straight away. Whether it then goes INSIDE the file is the library's
"embed sidecars" rule, the same switch every other sidecar answers to: on, and
subembed.embed_one takes it in with all its own guards (the language must be
one the library keeps, the file must be Matroska and not in use, the new file
is read back before the sidecar is recycled); off, and it stays a sidecar.

ONE AT A TIME, ON THE HEAVY LANE. Whisper large-v3, PaddleOCR and a 7B
language model are each gigabytes of GPU memory, loaded one after another in
one child process. Two of those at once, or one beside a Whisper install, is
how a box runs out of memory - heavy.py exists for exactly that, and a run
waits its turn there.

AN EXISTING ENGLISH SIDECAR IS NOT OVERWRITTEN unless asked: somebody may
have put a better one there by hand.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from . import heavy, joblog
from .config import DATA_DIR, NO_WINDOW
from .db import cursor, display_label, kv_get, kv_set

KV_QUEUE = "submake.queue"
KV_RECENT = "submake.recent"
RECENT_KEEP = 40
LANE = "Subtitle maker"

STATE: dict = {"running": False, "file_id": None, "label": "", "stage": "", "pct": 0.0,
               "note": "", "started": 0.0, "waiting_for": ""}
_TASK: asyncio.Task | None = None


def _queue() -> list:
    try:
        return json.loads(kv_get(KV_QUEUE) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _save_queue(q: list) -> None:
    kv_set(KV_QUEUE, json.dumps(q))


def _recent() -> list:
    try:
        return json.loads(kv_get(KV_RECENT) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _remember(row: dict) -> None:
    kv_set(KV_RECENT, json.dumps(([row] + _recent())[:RECENT_KEEP]))


def _row(file_id: int) -> dict | None:
    with cursor() as cur:
        r = cur.execute("SELECT id, path, library, title, season, episode, duration, "
                        "audio_langs FROM files WHERE id=?", (int(file_id),)).fetchone()
    return dict(r) if r else None


def _sidecar(path: str) -> str:
    return os.path.splitext(path)[0] + ".en.srt"


def _lang_of(r: dict) -> str:
    langs = [x.strip() for x in str(r.get("audio_langs") or "").split(",") if x.strip()]
    for x in langs:
        if x not in ("eng", "en", "und", "un"):
            return x
    return langs[0] if langs else "jpn"


def enqueue(file_ids: list, overwrite: bool = False) -> dict:
    q = _queue()
    have = {int(x["file_id"]) for x in q}
    added, skipped = 0, []
    for fid in [int(x) for x in file_ids or []]:
        r = _row(fid)
        if not r:
            skipped.append(f"{fid}: not in the library"); continue
        if fid in have or STATE.get("file_id") == fid:
            skipped.append(f"{display_label(r['title'], r['season'], r['episode'], r['path'])}: already queued"); continue
        if os.path.exists(_sidecar(r["path"])) and not overwrite:
            skipped.append(f"{display_label(r['title'], r['season'], r['episode'], r['path'])}: "
                           "already has an English subtitle beside it"); continue
        q.append({"file_id": fid, "overwrite": bool(overwrite), "at": time.time()})
        have.add(fid)
        added += 1
    _save_queue(q)
    kick()
    return {"added": added, "skipped": skipped, "queued": len(q)}


def cancel(file_id: int) -> bool:
    q = _queue()
    keep = [x for x in q if int(x["file_id"]) != int(file_id)]
    _save_queue(keep)
    return len(keep) != len(q)


def kick() -> None:
    global _TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _TASK is None or _TASK.done():
        _TASK = loop.create_task(_runner())


async def _runner() -> None:
    while _queue():
        got, holder = heavy.claim(LANE, ttl=4 * 3600)
        if not got:
            STATE["waiting_for"] = holder
            await asyncio.sleep(60)
            continue
        STATE["waiting_for"] = ""
        try:
            q = _queue()
            if not q:
                break
            job = q[0]
            try:
                await _one(int(job["file_id"]), bool(job.get("overwrite")))
            except Exception as e:                               # noqa: BLE001
                joblog.log(f"subtitle maker: {type(e).__name__}: {e}", "error")
                _remember({"at": time.time(), "file_id": int(job["file_id"]),
                           "label": STATE.get("label") or str(job["file_id"]), "ok": False,
                           "why": f"{type(e).__name__}: {e}"[:200]})
            _save_queue([x for x in _queue() if int(x["file_id"]) != int(job["file_id"])])
        finally:
            heavy.release(LANE)
            STATE.update(running=False, file_id=None, label="", stage="", pct=0.0, note="")


async def _one(file_id: int, overwrite: bool) -> None:
    from .jobs import _ffmpeg_exe
    r = _row(file_id)
    if not r or not os.path.exists(r["path"]):
        return
    label = display_label(r["title"], r["season"], r["episode"], r["path"])
    side = _sidecar(r["path"])
    if os.path.exists(side) and not overwrite:
        _remember({"at": time.time(), "file_id": file_id, "label": label, "ok": False,
                   "why": "an English subtitle is already beside it"})
        return
    lang = _lang_of(r)
    STATE.update(running=True, file_id=file_id, label=label, stage="starting", pct=0.0,
                 note="", started=time.time())
    joblog.log(f"subtitle maker: {label} ({lang} -> English)", "info")
    tmp = os.path.join(str(DATA_DIR), "submake.tmp.srt")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    film = r["title"] or os.path.splitext(os.path.basename(r["path"]))[0]
    cmd = [sys.executable, "-m", "app.submake_worker", "--src", r["path"], "--out", tmp,
           "--lang", lang, "--title", film, "--duration", str(float(r["duration"] or 0)),
           "--data", str(DATA_DIR), "--ffmpeg", _ffmpeg_exe()]
    p = await asyncio.create_subprocess_exec(*cmd, cwd=root, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL,
                                             creationflags=NO_WINDOW)
    end: dict = {}
    while True:
        ln = await p.stdout.readline()
        if not ln:
            break
        try:
            d = json.loads(ln.decode("utf-8", "replace"))
        except Exception:                                        # noqa: BLE001
            continue                                             # a library printing to stdout
        if "stage" in d:
            STATE.update(stage=d["stage"], pct=float(d.get("pct") or 0), note=d.get("note") or "")
        if d.get("done") or d.get("error"):
            end = d
    await p.wait()
    row = {"at": time.time(), "file_id": file_id, "label": label, "lang": lang,
           "secs": round(time.time() - STATE["started"])}
    if not end.get("done") or not os.path.exists(tmp):
        row.update(ok=False, why=end.get("error") or f"the worker stopped (exit {p.returncode})")
        _remember(row)
        joblog.log(f"subtitle maker: {label} failed - {row['why']}", "warn")
        return
    # ACROSS DRIVES IS A COPY. The worker writes on the data drive and the
    # file lives on the pool; os.replace cannot cross that, so the copy lands
    # beside the video under a temporary name and is renamed there.
    import shutil
    shutil.copyfile(tmp, side + ".part")
    os.replace(side + ".part", side)
    try:
        os.remove(tmp)
    except OSError:
        pass
    row.update(ok=True, narration=end.get("narration", 0), onscreen=end.get("onscreen", 0),
               sidecar=os.path.basename(side))
    # THE LIBRARY'S RULE DECIDES WHERE IT LIVES - see the module docstring.
    from . import subembed
    try:
        subembed.forget_sidecars(r["path"])
    except Exception:                                            # noqa: BLE001
        pass
    lib = r["library"] or ""
    if subembed.enabled(lib):
        STATE.update(stage="putting it inside the file", pct=98)
        try:
            res = await asyncio.to_thread(subembed.embed_one, file_id)
        except Exception as e:                                   # noqa: BLE001
            res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if res.get("ok") and res.get("embedded"):
            row.update(where="embedded", why="embedded in the file - the library embeds sidecars")
        else:
            row.update(where="sidecar", why="kept beside the file - could not embed: " + str(res.get("why") or "nothing to take")[:160])
    else:
        row.update(where="sidecar", why=f"kept beside the file - {lib or 'this library'} does not embed sidecars")
    _remember(row)
    joblog.log(f"subtitle maker: {label} - {row['narration']} spoken + {row['onscreen']} on-screen "
               f"line(s), {row['why']}", "ok")
    try:
        from . import pushover
        if pushover.enabled():
            await pushover.send("nuarr · subtitles made",
                                f"<b>{pushover._esc(label)}</b>\n{row['narration']} spoken + "
                                f"{row['onscreen']} on-screen lines\n{pushover._esc(row['why'])}")
    except Exception:                                            # noqa: BLE001
        pass


def snapshot() -> dict:
    q = _queue()
    rows = []
    for x in q:
        r = _row(int(x["file_id"]))
        if r:
            rows.append({"file_id": r["id"], "label": display_label(r["title"], r["season"], r["episode"], r["path"])})
    return {"state": dict(STATE), "queue": rows, "recent": _recent(), "lane": heavy.state()}


async def watch() -> None:
    """Resume a queue left by a restart."""
    await asyncio.sleep(120)
    if _queue():
        kick()
