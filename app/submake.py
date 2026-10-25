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
KV_TRIED = "submake.tried"     # file_id -> {"ok", "skipped", "at", "why"}, kept for good
RECENT_KEEP = 40
LANE = "Subtitle maker"
RETRY_S = 6 * 3600          # a crashed run is offered again after this

STATE: dict = {"running": False, "file_id": None, "label": "", "stage": "", "pct": 0.0,
               "note": "", "started": 0.0, "waiting_for": ""}
_TASK: asyncio.Task | None = None
_PROC = None                   # the worker process while one runs
_STOPPED = False


def stop() -> bool:
    """Stop the file being made: the worker and anything it started."""
    global _STOPPED
    p = _PROC
    if p is None or p.returncode is not None:
        return False
    _STOPPED = True
    try:
        import subprocess
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    except Exception:                                            # noqa: BLE001
        try:
            p.kill()
        except Exception:                                        # noqa: BLE001
            return False
    return True


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


def _tried() -> dict:
    try:
        return json.loads(kv_get(KV_TRIED) or "{}")
    except Exception:                                            # noqa: BLE001
        return {}


def _remember(row: dict) -> None:
    kv_set(KV_RECENT, json.dumps(([row] + _recent())[:RECENT_KEEP]))
    # THE OUTCOME PER FILE, KEPT FOR GOOD. The list above is the last forty;
    # the Raws check asks "has this file been tried, and how did it go"
    # about any file, at any age, before it may delete one.
    if row.get("file_id"):
        t = _tried()
        t[str(int(row["file_id"]))] = {
            "ok": bool(row.get("ok")), "at": row.get("at") or time.time(),
            "skipped": "burned into the picture" in str(row.get("why") or ""),
            # A CRASH IS NOT AN ANSWER. Only "nothing was spoken or written"
            # says the file cannot be helped; a worker that died (WinError
            # 127, a missing model, an exception) says nothing about the file
            # and must never be what lets the Raws check delete it.
            "error": (not row.get("ok")
                      and "burned into the picture" not in str(row.get("why") or "")
                      and not str(row.get("why") or "").startswith("nothing was spoken")),
            "why": str(row.get("why") or "")[:200]}
        kv_set(KV_TRIED, json.dumps(t))


def gate_many(file_ids: list) -> dict:
    """gate() for a list, with the queue and the tried-record read once."""
    q = {int(x["file_id"]) for x in _queue()}
    t = _tried()
    now = time.time()
    out = {}
    for fid in file_ids:
        fid = int(fid)
        if STATE.get("file_id") == fid or fid in q:
            out[fid] = "pending"
            continue
        tr = t.get(str(fid))
        if not tr:
            out[fid] = "none"
        elif tr.get("ok"):
            out[fid] = "ok"
        elif tr.get("skipped"):
            out[fid] = "skipped"
        elif tr.get("error"):
            out[fid] = "none" if now - float(tr.get("at") or 0) > RETRY_S else "retry"
        else:
            out[fid] = "failed"
    return out


def gate(file_id: int) -> str:
    """Where the maker stands on this file, for a check that would delete it.

    'none'    never tried       'pending'  queued or being made now
    'ok'      subtitles made    'skipped'  English already burned in
    'failed'  tried, and there was nothing to make subtitles from
    'retry'   the worker itself failed - not a fact about the file; it is
              offered again ('none') once RETRY_S has passed
    """
    fid = int(file_id)
    if STATE.get("file_id") == fid or any(int(x["file_id"]) == fid for x in _queue()):
        return "pending"
    t = _tried().get(str(fid))
    if not t:
        return "none"
    if t.get("ok"):
        return "ok"
    if t.get("skipped"):
        return "skipped"
    if t.get("error"):
        return "none" if time.time() - float(t.get("at") or 0) > RETRY_S else "retry"
    return "failed"


def _row(file_id: int) -> dict | None:
    with cursor() as cur:
        r = cur.execute("SELECT id, path, library, title, season, episode, duration, "
                        "audio_langs FROM files WHERE id=?", (int(file_id),)).fetchone()
    return dict(r) if r else None


def _sidecar(path: str, ext: str = ".srt") -> str:
    return os.path.splitext(path)[0] + ".en" + ext


def _have_sidecar(path: str) -> bool:
    return any(os.path.exists(_sidecar(path, e)) for e in (".srt", ".ass"))


_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd"}
_NOT_DIALOGUE = ("sign", "song", "forced", "karaoke", "op/ed", "commentary")


def _lname(code: str) -> str:
    try:
        from .submake_worker import lang_plan
        return lang_plan(code)["name"]
    except Exception:                                            # noqa: BLE001
        return code


def text_source(r: dict) -> dict | None:
    """A subtitle track in another language this file already has, as text.
    -> {"spec": "stream:N" | "file:path", "lang", "what"} or None.

    Inside the file first (ffprobe's stream index, which ffmpeg -map takes),
    then files beside it. Picture tracks are not text; tracks titled signs,
    songs or forced are not the dialogue; English and untagged are skipped -
    English needs nothing, and untagged could be anything."""
    import subprocess
    from .jobs import _ffprobe_exe
    try:
        raw = subprocess.run([_ffprobe_exe(), "-v", "error", "-select_streams", "s",
                              "-show_entries", "stream=index,codec_name:stream_tags=language,title:"
                              "stream_disposition=forced", "-of", "json", r["path"]],
                             capture_output=True, text=True, timeout=60,
                             creationflags=NO_WINDOW).stdout
        streams = json.loads(raw or "{}").get("streams") or []
    except Exception:                                            # noqa: BLE001
        streams = []
    best = None
    for st in streams:
        lang = str((st.get("tags") or {}).get("language") or "").lower()
        title = str((st.get("tags") or {}).get("title") or "").lower()
        if (st.get("codec_name") or "").lower() not in _TEXT_CODECS:
            continue
        if lang in ("", "und", "eng", "en") or (st.get("disposition") or {}).get("forced"):
            continue
        if any(w in title for w in _NOT_DIALOGUE):
            continue
        best = best or {"spec": f"stream:{int(st['index'])}", "lang": lang,
                        "what": f"the {_lname(lang)} subtitle track inside the file"}
    if best:
        return best
    try:
        from . import subembed
        for side in subembed.sidecars_for(r["path"], fresh=True):
            if not side.lower().endswith((".srt", ".ass", ".ssa", ".vtt")):
                continue
            nm = subembed.read_sidecar_name(r["path"], side)
            lang = str(nm.get("lang") or "").lower()
            if not nm.get("ok") or lang in ("", "und", "eng", "en"):
                continue
            if any(w in str(nm.get("role") or "").lower() for w in ("forced", "sdh")):
                continue
            return {"spec": f"file:{side}", "lang": lang,
                    "what": f"the {_lname(lang)} subtitle file beside it ({os.path.basename(side)})"}
    except Exception:                                            # noqa: BLE001
        pass
    return None


def _lang_of(r: dict) -> str:
    langs = [x.strip() for x in str(r.get("audio_langs") or "").split(",") if x.strip()]
    for x in langs:
        if x not in ("eng", "en", "und", "un"):
            return x
    return langs[0] if langs else "jpn"


def enqueue(file_ids: list, overwrite: bool = False, by: str = "you") -> dict:
    q = _queue()
    have = {int(x["file_id"]) for x in q}
    added, skipped = 0, []
    for fid in [int(x) for x in file_ids or []]:
        r = _row(fid)
        if not r:
            skipped.append(f"{fid}: not in the library"); continue
        if fid in have or STATE.get("file_id") == fid:
            skipped.append(f"{display_label(r['title'], r['season'], r['episode'], r['path'])}: already queued"); continue
        if _have_sidecar(r["path"]) and not overwrite:
            skipped.append(f"{display_label(r['title'], r['season'], r['episode'], r['path'])}: "
                           "already has an English subtitle beside it"); continue
        q.append({"file_id": fid, "overwrite": bool(overwrite), "at": time.time(), "by": by})
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


KV_PAUSED = "submake.paused"


def paused() -> bool:
    return str(kv_get(KV_PAUSED) or "0") == "1"


def set_paused(on: bool) -> None:
    kv_set(KV_PAUSED, "1" if on else "0")
    if not on:
        kick()


def cost() -> dict:
    """For the Concurrency page: what one file costs, and how long one took."""
    secs = sorted(int(r.get("secs") or 0) for r in _recent() if r.get("ok") and r.get("secs"))
    return {"cpu": 2, "gpu": 3, "ram": 2, "disk": 1, "lead": "gpu",
            "why": ("THREE MODELS, ONE AFTER ANOTHER, ALL ON THE CARD. Whisper large-v3 listens to "
                    "every stretch of speech (about 3 GB of video memory), PaddleOCR reads two "
                    "frames a second off the picture, and Qwen2.5-7B translates what it read (about "
                    "8 GB). ffmpeg decodes the audio and the frames on the processor. Measured: a "
                    "22-minute episode takes 7-8 minutes."),
            "measured": ({"jobs": len(_tried()), "secs": secs[len(secs) // 2]} if secs else None)}


def forget(ats: list) -> int:
    """Take finished rows off the list, by their time. Nothing on disk changes."""
    want = {round(float(x), 3) for x in ats or []}
    rows = _recent()
    keep = [r for r in rows if round(float(r.get("at") or 0), 3) not in want]
    kv_set(KV_RECENT, json.dumps(keep))
    return len(rows) - len(keep)


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
        if paused():
            STATE["waiting_for"] = "paused on the Concurrency page"
            await asyncio.sleep(30)
            continue
        got, holder = heavy.claim(LANE, ttl=4 * 3600)
        if not got:
            STATE["waiting_for"] = holder
            await asyncio.sleep(60)
            continue
        # Whisper and Qwen both load in the maker's worker; room for the bigger.
        from . import gpubudget as _gb
        _ok, _why = await asyncio.to_thread(_gb.make_room, _gb.COST_GB["qwen"], "submake", 300)
        if not _ok:
            heavy.release(LANE)
            STATE["waiting_for"] = f"GPU memory - {_why}"
            await asyncio.sleep(60)
            continue
        STATE["waiting_for"] = ""
        try:
            q = _queue()
            if not q:
                break
            job = q[0]
            # NOT WHILE THE QUEUE HAS IT. The job commits a new file and the
            # arr renames it; a make running across that writes beside a
            # name that no longer exists. Behind the others it goes, and
            # the lane is given back meanwhile.
            try:
                from . import jobs as _jobs
                who = _jobs.busy_with(int(job["file_id"]))
            except Exception:                                    # noqa: BLE001
                who = ""
            if who:
                STATE["waiting_for"] = f"{who} has this file - the maker waits for it"
                if len(q) > 1:
                    _save_queue(q[1:] + [job])
                await asyncio.sleep(60)
                continue
            try:
                await _one(int(job["file_id"]), bool(job.get("overwrite")), job.get("by") or "you")
            except Exception as e:                               # noqa: BLE001
                joblog.log(f"subtitle maker: {type(e).__name__}: {e}", "error")
                _remember({"at": time.time(), "file_id": int(job["file_id"]),
                           "label": STATE.get("label") or str(job["file_id"]), "ok": False,
                           "why": f"{type(e).__name__}: {e}"[:200]})
            _save_queue([x for x in _queue() if int(x["file_id"]) != int(job["file_id"])])
        finally:
            heavy.release(LANE)
            STATE.update(running=False, file_id=None, label="", stage="", pct=0.0, note="")


async def _one(file_id: int, overwrite: bool, by: str = "you") -> None:
    from .jobs import _ffmpeg_exe
    r = _row(file_id)
    if not r or not os.path.exists(r["path"]):
        return
    label = display_label(r["title"], r["season"], r["episode"], r["path"])
    # THE LOOK FOLLOWS THE KIND OF MEDIA - see substyle. Plain is an .srt;
    # every other style is an .ass with the fonts attached when it goes in.
    from . import substyle
    style = substyle.for_library(r["library"] or "")
    side = _sidecar(r["path"], ".srt" if style == "plain" else ".ass")
    if _have_sidecar(r["path"]) and not overwrite:
        _remember({"at": time.time(), "file_id": file_id, "label": label, "ok": False,
                   "why": "an English subtitle is already beside it"})
        return
    lang = _lang_of(r)
    from . import outside as _out
    _out.start("submake", file_id)
    STATE.update(running=True, file_id=file_id, label=label, stage="starting", pct=0.0,
                 duration=float(r["duration"] or 0),
                 note="", started=time.time(), library=r["library"] or "",
                 file=os.path.basename(r["path"]), lang=lang)
    joblog.log(f"subtitle maker: {label} ({lang} -> English)", "info")
    tmp = os.path.join(str(DATA_DIR), "submake.tmp.srt")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    film = r["title"] or os.path.splitext(os.path.basename(r["path"]))[0]
    cmd = [sys.executable, "-m", "app.submake_worker", "--src", r["path"], "--out", tmp,
           "--lang", lang, "--title", film, "--duration", str(float(r["duration"] or 0)),
           "--data", str(DATA_DIR), "--ffmpeg", _ffmpeg_exe()] + (["--force"] if overwrite else [])
    # WHAT THIS SHOW HAS TAUGHT US - see sublearn. The show's names (gathered
    # once from the English subtitles already in its folder) go to Whisper as
    # a prompt and to Qwen as spellings to keep; its corrections are applied
    # to what comes back.
    from . import sublearn
    show = sublearn.show_of(r["path"])
    learn = os.path.join(str(DATA_DIR), "submake.learn.json")
    try:
        STATE.update(stage="gathering the show's names", pct=1)
        await asyncio.wait_for(asyncio.to_thread(sublearn.build_names, show), timeout=600)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        fx = sublearn.fixes(show, "whisper")
        fx += [p for p in sublearn.fixes(show, "qwen") if p not in fx]
        with open(learn, "w", encoding="utf-8") as f:
            json.dump({"names": sublearn.names(show), "fixes": fx}, f)
        cmd += ["--learn", learn]
    except Exception:                                            # noqa: BLE001
        pass
    # TRANSLATE WHAT IS WRITTEN BEFORE LISTENING TO WHAT IS SAID. A text track
    # in another language is a person's timing and a person's words; Whisper
    # is the fallback for a file that has none.
    src = await asyncio.to_thread(text_source, r)
    if src:
        cmd += ["--from-subs", src["spec"], "--sub-lang", src["lang"]]
        STATE["note"] = "translating " + src["what"]
        joblog.log(f"subtitle maker: {label} - translating {src['what']}", "info")
    global _PROC, _STOPPED
    _STOPPED = False
    p = await asyncio.create_subprocess_exec(*cmd, cwd=root, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL,
                                             creationflags=NO_WINDOW)
    _PROC = p
    _out.set_pid("submake", p.pid)
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
    _PROC = None
    if _STOPPED and not end.get("done"):
        end = {"error": "stopped by you"}
    row = {"at": time.time(), "file_id": file_id, "label": label, "lang": lang,
           "library": r["library"] or "", "file": os.path.basename(r["path"]),
           "secs": round(time.time() - STATE["started"]), "by": by}
    if not end.get("done") or not os.path.exists(tmp):
        row.update(ok=False, why=end.get("error") or f"the worker stopped (exit {p.returncode})")
        _remember(row)
        joblog.log(f"subtitle maker: {label} failed - {row['why']}", "warn")
        return
    # THE FILE MAY HAVE MOVED WHILE THE MAKER WORKED - an encode that
    # committed and was renamed under it. The sidecar is named after the
    # file as it is NOW, not as it was when the make began; written under
    # the old name it was never found again.
    cur_r = _row(file_id)
    if not cur_r or not os.path.exists(cur_r["path"]):
        row.update(ok=False, why="the file is gone from the library")
        _remember(row)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return
    if cur_r["path"] != r["path"]:
        joblog.log(f"subtitle maker: {label} - the file was renamed while the subtitles were made; "
                   f"following it to {os.path.basename(cur_r['path'])}", "info")
        r = cur_r
        side = _sidecar(r["path"], os.path.splitext(side)[1])
        row["file"] = os.path.basename(r["path"])
    # ACROSS DRIVES IS A COPY. The worker writes on the data drive and the
    # file lives on the pool; os.replace cannot cross that, so the copy lands
    # beside the video under a temporary name and is renamed there.
    import shutil
    if style == "plain":
        shutil.copyfile(tmp, side + ".part")
    else:
        with open(tmp, encoding="utf-8", errors="replace") as f:
            ass = substyle.to_ass(f.read(), style, label, override=sublearn.style_for(show))
        with open(side + ".part", "w", encoding="utf-8-sig") as f:
            f.write(ass)
    os.replace(side + ".part", side)
    for other in (".srt", ".ass"):
        o = _sidecar(r["path"], other)
        if o != side and os.path.exists(o) and overwrite:
            try:
                os.remove(o)
            except OSError:
                pass
    try:
        made_text = open(tmp, encoding="utf-8", errors="replace").read()
    except OSError:
        made_text = ""
    try:
        os.remove(tmp)
    except OSError:
        pass

    def _learnt():
        # kept as written, to learn later from what you change in it
        try:
            sublearn.remember_made(file_id, r["path"], made_text, "submake")
            sublearn.note_applied(show, end.get("applied") or {})
        except Exception:                                        # noqa: BLE001
            pass
    row.update(ok=True, narration=end.get("narration", 0), onscreen=end.get("onscreen", 0),
               translated=end.get("translated", 0), source=(src or {}).get("what", ""),
               sidecar=os.path.basename(side), style=style)
    # THE LIBRARY'S RULE DECIDES WHERE IT LIVES - see the module docstring.
    from . import subembed
    try:
        subembed.forget_sidecars(r["path"])
    except Exception:                                            # noqa: BLE001
        pass
    lib = r["library"] or ""
    if overwrite and subembed.enabled(lib) and r["path"].lower().endswith(".mkv"):
        STATE.update(stage="replacing nuarr's track inside the file", pct=98)
        try:
            text = open(side, encoding="utf-8-sig", errors="replace").read()
            srt = text if side.endswith(".srt") else substyle.ass_to_srt(text)
            res = await asyncio.to_thread(restyle_one, file_id, style, srt)
        except Exception as e:                                   # noqa: BLE001
            res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            try:
                os.remove(side)
            except OSError:
                pass
            row.update(where="embedded", why="replaced nuarr's own track inside the file")
            _learnt()
            _remember(row)
            joblog.log(f"subtitle maker: {label} - remade, nuarr's track replaced", "ok")
            return
    # ALWAYS INSIDE, when the container allows it. The library's sidecar rule
    # is about other people's loose files; this one is nuarr's own work, and
    # a loose file is exactly what the arrs lose when the file is renamed,
    # upgraded or edited. Beside the file only when it is not an mkv or the
    # embed itself failed - and then the reason is said.
    if r["path"].lower().endswith(".mkv") and subembed.have_mkvmerge():
        STATE.update(stage="putting it inside the file", pct=98)
        try:
            res = await asyncio.to_thread(subembed.embed_one, file_id, None, "", True)
        except Exception as e:                                   # noqa: BLE001
            res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if res.get("ok") and res.get("embedded"):
            row.update(where="embedded", why="embedded in the file")
        else:
            row.update(where="sidecar", why="kept beside the file - could not embed: " + str(res.get("why") or "nothing to take")[:160])
    elif not r["path"].lower().endswith(".mkv"):
        row.update(where="sidecar", why="kept beside the file - only an mkv can take a subtitle track")
    else:
        row.update(where="sidecar", why="kept beside the file - mkvmerge is not installed")
    _learnt()
    _remember(row)
    joblog.log(f"subtitle maker: {label} - "
               + (f"{row['translated']} line(s) translated from {row['source']}" if row.get("translated")
                  else f"{row['narration']} spoken + {row['onscreen']} on-screen line(s)")
               + f", {row['why']}", "ok")
    try:
        from . import pushover
        if pushover.enabled():
            await pushover.send("nuarr · subtitles made",
                                f"<b>{pushover._esc(label)}</b>\n{row['narration']} spoken + "
                                f"{row['onscreen']} on-screen lines\n{pushover._esc(row['why'])}")
    except Exception:                                            # noqa: BLE001
        pass


# ------------------------------------------------------------ restyle --
# A STYLE CHOSEN AFTER THE FACT. The subtitles already made keep the look they
# were made with until this rewrites them: a sidecar is rewritten in place; a
# track inside the file is swapped for the restyled one in one mkvmerge pass
# (fonts attached), read back, and committed through fileops.safe_replace -
# the same road every other rewrite takes. Only nuarr's own track is touched:
# the one named "English (nuarr)", or - for files made before tracks were
# named - the file's only English text track, on a file the maker's own
# history says it embedded into.
TRACK_NAME = "English (nuarr)"


def _mkvj(path: str) -> dict:
    from .subembed import _mkvmerge
    from .config import quiet_run
    r = quiet_run([_mkvmerge(), "-J", path], capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=120)
    return json.loads(r.stdout or "{}")


def _style_of_track(path: str, t: dict, sub_ord: int) -> str:
    from .jobs import _ffmpeg_exe
    from .config import quiet_run
    from . import substyle
    if "SubRip" in (t.get("codec") or ""):
        return "plain"
    r = quiet_run([_ffmpeg_exe(), "-v", "error", "-i", path, "-map", f"0:s:{sub_ord}", "-f", "ass", "-"],
                  capture_output=True, timeout=300)
    m = __import__("re").search(r"; nuarr style: (\w+)", r.stdout.decode("utf-8", "replace")[:3000])
    return m.group(1) if m else "?"


def restyle_one(file_id: int, style: str = "", srt_text: str = "") -> dict:
    """Rewrite one made subtitle in the style its group has now. With
    srt_text, the track inside the file is REPLACED by that text - a remake
    with overwrite, which the sidecar embed would otherwise throw away as a
    second copy of a language the file already has."""
    import re
    import shutil
    from . import fileops, substyle, subembed
    from .jobs import _ffmpeg_exe
    from .config import quiet_run
    r = _row(file_id)
    if not r or not os.path.exists(r["path"]):
        return {"ok": False, "why": "the file is not on disk"}
    path = r["path"]
    style = style or substyle.for_library(r["library"] or "")
    label = display_label(r["title"], r["season"], r["episode"], path)
    # A SIDECAR: rewritten beside the file, in place.
    for ext in (() if srt_text else (".srt", ".ass")):
        side = _sidecar(path, ext)
        if os.path.exists(side):
            text = open(side, encoding="utf-8-sig", errors="replace").read()
            was = "plain" if ext == ".srt" else (substyle.preset_of_ass(side) or "?")
            if was == style:
                return {"ok": True, "same": True, "why": "already in that style"}
            srt = text if ext == ".srt" else substyle.ass_to_srt(text)
            new = _sidecar(path, ".srt" if style == "plain" else ".ass")
            body = srt if style == "plain" else substyle.to_ass(srt, style, label)
            with open(new + ".part", "w", encoding="utf-8" if style == "plain" else "utf-8-sig") as f:
                f.write(body)
            os.replace(new + ".part", new)
            if new != side:
                os.remove(side)
            return {"ok": True, "where": "sidecar", "was": was, "now": style}
    # INSIDE THE FILE.
    if not path.lower().endswith(".mkv"):
        return {"ok": False, "why": "not .mkv"}
    if fileops.is_locked(path):
        return {"ok": False, "why": "the file is in use"}
    j = _mkvj(path)
    subs = [t for t in j.get("tracks", []) if t.get("type") == "subtitles"]
    eng_text = [(i, t) for i, t in enumerate(subs)
                if (t.get("properties") or {}).get("language") in ("eng", "en")
                and any(k in (t.get("codec") or "") for k in ("SubRip", "SubStationAlpha", "SSA", "ASS"))]
    named = [(i, t) for i, t in eng_text if (t.get("properties") or {}).get("track_name") == TRACK_NAME]
    made_here = any(x.get("file_id") == int(file_id) and x.get("ok") and x.get("where") == "embedded"
                    for x in _recent())
    pick = named[0] if named else (eng_text[0] if len(eng_text) == 1 and made_here else None)
    if not pick:
        return {"ok": False, "why": "cannot tell which English track is the one nuarr made - left alone"}
    ordn, t = pick
    was = _style_of_track(path, t, ordn)
    if was == style and not srt_text:
        return {"ok": True, "same": True, "why": "already in that style"}
    fmt = "srt" if "SubRip" in (t.get("codec") or "") else "ass"
    raw = quiet_run([_ffmpeg_exe(), "-v", "error", "-i", path, "-map", f"0:s:{ordn}", "-f", fmt, "-"],
                    capture_output=True, timeout=300).stdout.decode("utf-8", "replace")
    srt = srt_text or (raw if fmt == "srt" else substyle.ass_to_srt(raw))
    if len(substyle.srt_cues(srt)) < (1 if srt_text else 3):
        return {"ok": False, "why": "could not read the track back out"}
    work = os.path.join(str(DATA_DIR), f"restyle.{file_id}" + (".srt" if style == "plain" else ".ass"))
    with open(work, "w", encoding="utf-8" if style == "plain" else "utf-8-sig") as f:
        f.write(srt if style == "plain" else substyle.to_ass(srt, style, label))
    ok_room, why_room = fileops.cache_room(os.path.getsize(path))
    if not ok_room:
        return {"ok": False, "why": why_room}
    tmp = fileops.cache_temp(".mkv", "restyle")
    have = {(a.get("file_name") or "").lower() for a in j.get("attachments", [])}
    cmd = [subembed._mkvmerge(), "-o", tmp, "--subtitle-tracks", "!" + str(t["id"]), path,
           "--language", "0:eng", "--track-name", f"0:{TRACK_NAME}", "--default-track-flag", "0:yes", work]
    for fp in ([] if style == "plain" else substyle.font_paths(style)):
        if os.path.basename(fp).lower() not in have:
            cmd += ["--attachment-mime-type", "application/x-truetype-font", "--attach-file", fp]
    try:
        res = quiet_run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        if res.returncode not in (0, 1):
            return {"ok": False, "why": f"mkvmerge said: {(res.stdout or '')[-200:]}"}
        # READ IT BACK before anything replaces the original.
        k = _mkvj(tmp)
        kinds = lambda d, ty: sum(1 for x in d.get("tracks", []) if x.get("type") == ty)
        if kinds(k, "video") != kinds(j, "video") or kinds(k, "audio") != kinds(j, "audio") \
                or kinds(k, "subtitles") != kinds(j, "subtitles"):
            return {"ok": False, "why": "the rebuilt file does not have the same tracks - nothing was replaced"}
        new = [x for x in k.get("tracks", []) if (x.get("properties") or {}).get("track_name") == TRACK_NAME]
        want = "SubRip" if style == "plain" else "SubStationAlpha"
        if not new or want not in (new[0].get("codec") or ""):
            return {"ok": False, "why": "the restyled track is not in the rebuilt file - nothing was replaced"}
        d0 = float((j.get("container") or {}).get("properties", {}).get("duration") or 0) / 1e9
        d1 = float((k.get("container") or {}).get("properties", {}).get("duration") or 0) / 1e9
        if d0 and abs(d0 - d1) > 2:
            return {"ok": False, "why": f"the rebuilt file runs {d1:.0f}s against {d0:.0f}s - nothing was replaced"}
        from .jobs import outside_commit as _oc
        rep = fileops.safe_replace(path, tmp, **_oc(path, "submake"))
        if not getattr(rep, "ok", False):
            return {"ok": False, "why": f"could not put the rebuilt file in place: {getattr(rep, 'why', '')}"}
    finally:
        fileops._quiet_remove(tmp)
        try:
            os.remove(work)
        except OSError:
            pass
    try:
        from . import jobs as _jobs
        pr = quiet_run([_jobs._ffprobe_exe(), "-v", "quiet", "-print_format", "json", "-show_streams",
                        "-show_format", path], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
        data = json.loads(pr.stdout or "{}")
        if data.get("streams"):
            _jobs.cache_probe(int(file_id), data)
    except Exception:                                            # noqa: BLE001
        pass
    joblog.log(f"subtitle maker: {label} restyled {was} -> {style}", "ok")
    return {"ok": True, "where": "embedded", "was": was, "now": style}


RESTYLE: dict = {"running": False, "done": 0, "total": 0, "last": [], "at": 0.0}


def _set_style(file_id: int, style: str) -> None:
    rows = _recent()
    for x in rows:
        if x.get("file_id") == file_id and x.get("ok"):
            x["style"] = style
    kv_set(KV_RECENT, json.dumps(rows))


def style_counts() -> dict:
    """{group: {"made": n, "other": n}} - made ones not in the group's style
    now (a row from before styles existed was plain SRT)."""
    from . import substyle
    ch = substyle.chosen()
    out = {g: {"made": 0, "other": 0} for g in ch}
    seen = set()
    for x in _recent():
        if not x.get("ok") or x.get("file_id") in seen:
            continue
        seen.add(x.get("file_id"))
        g = substyle.group_of(x.get("library") or "")
        out[g]["made"] += 1
        out[g]["other"] += int((x.get("style") or "plain") != ch[g])
    return out


def restyle_all(group: str = "") -> dict:
    """Every subtitle the maker has made, in its group's style now."""
    from . import heavy as _heavy, substyle
    rows = [x for x in _recent() if x.get("ok") and x.get("file_id")]
    seen, todo = set(), []
    for x in rows:
        if x["file_id"] in seen:
            continue
        seen.add(x["file_id"])
        if not group or substyle.group_of(x.get("library") or "") == group:
            todo.append(x)
    got, holder = _heavy.claim("Subtitle restyle", ttl=3600)
    if not got:
        return {"ok": False, "why": f"{holder} has the lane - try again when it is done"}
    RESTYLE.update(running=True, done=0, total=len(todo), last=[], at=time.time())
    try:
        for x in todo:
            try:
                res = restyle_one(int(x["file_id"]))
            except Exception as e:                               # noqa: BLE001
                res = {"ok": False, "why": f"{type(e).__name__}: {e}"[:160]}
            RESTYLE["last"].append({"label": x.get("label"), **res})
            if res.get("ok") and res.get("now"):
                _set_style(int(x["file_id"]), res["now"])
            RESTYLE["done"] += 1
    finally:
        RESTYLE["running"] = False
        _heavy.release("Subtitle restyle")
    return {"ok": True, "results": RESTYLE["last"]}


def as_running() -> list:
    """The maker as a worker card, for the job feed the dashboard and the
    Processing System draw from - so a file being given subtitles shows where
    every other piece of running work does, not only on the Subtitles page.
    Empty when nothing is being made."""
    if not STATE.get("running"):
        return []
    from . import outside as _out
    fid = STATE.get("file_id")
    el = time.time() - float(STATE.get("started") or time.time())
    pct = max(0.0, min(1.0, float(STATE.get("pct") or 0) / 100.0))
    eta = round(el / pct - el) if 0.05 < pct < 1 else None
    stage = STATE.get("stage") or "starting"
    return [{
        "job_id": f"submake-{fid}", "pool": "submake", "kind": "submake",
        "title": STATE.get("label") or "", "file": STATE.get("file") or "",
        "path": "", "stage": stage + (f" - {STATE['note']}" if STATE.get("note") else ""),
        "progress": pct, "elapsed_s": round(el), "eta_s": eta, "eta_est": True,
        "plan": "make English subtitles - Whisper for what is said, OCR and Qwen for what is written",
        "why_pool": ("The Subtitle maker: English subtitles for a file that has none. One file "
                     "at a time on the heavy lane; it runs outside the job queue, in its own "
                     "process, and follows the library's sidecar rule when it is done."),
        "doing": {"tool": "Whisper large-v3 + PaddleOCR + Qwen2.5-7B", "hw": "GPU",
                  "why": stage},
        "actions": [{"kind": "subtitle", "what": "make English subtitles",
                     "why": "the file has no subtitles anyone here can read", "detail": ""}],
        "venc": None, "fps": 0.0, "speed": 0.0, "paused_for_viewer": False,
        "paused_for_load": False, "pace_factor": 0.0, "pace_why": "", "cancelled": False,
        "by_bytes": False, "disk": _out.disk_of(fid), "dest_disk": "", "lang": STATE.get("lang") or "",
        "library": STATE.get("library") or "", "file_id": fid,
        **{k: v for k, v in _out.fields("submake", pct, media_s=float(STATE.get("duration") or 0)).items()
           if v is not None or k != "eta_s"},
    }]


# ------------------------------------------------------------- the models --
LLM_REPO = "jncraton/Qwen2.5-7B-Instruct-ct2-int8"
LLM_DIR = os.path.join(str(DATA_DIR), "llm", "qwen2.5-7b-instruct-ct2-int8")
WHISPER_LARGE_DIR = os.path.join(str(DATA_DIR), "whisper", "models--Systran--faster-whisper-large-v3")
# NLLB-200 was tried first and could not read hiragana (see submake_worker's
# docstring). Its download stayed behind; it is offered for removal, never used.
NLLB_DIR = os.path.join(str(DATA_DIR), "nllb")
LLM_STATE: dict = {"busy": "", "result": None, "error": "", "at": 0.0}


def _dir_size(p: str) -> int:
    # Each file once: the Hugging Face cache links the same blob under both
    # blobs/ and snapshots/, and adding both doubled large-v3 to 6.2 GB.
    n, seen = 0, set()
    for root, _d, files in os.walk(p):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if st.st_ino and key in seen:
                continue
            seen.add(key)
            n += st.st_size
    return n


def _last_use(kind: str) -> float:
    """When a made file last used this model: Whisper for any made file,
    Qwen only for one that had on-screen text to translate."""
    for r in _recent():
        if r.get("ok") and (kind == "whisper" or (r.get("onscreen") or 0) > 0):
            return float(r.get("at") or 0)
    return 0.0


def models_info() -> dict:
    try:
        import ctranslate2
        ct2, cuda = ctranslate2.__version__, ctranslate2.get_cuda_device_count()
    except Exception as e:                                       # noqa: BLE001
        ct2, cuda = f"not importable: {type(e).__name__}", 0
    llm_ok = os.path.exists(os.path.join(LLM_DIR, "model.bin"))
    wl_ok = os.path.isdir(WHISPER_LARGE_DIR)
    return {
        "llm": {"repo": LLM_REPO, "name": "Qwen2.5-7B-Instruct", "quant": "int8 (CTranslate2)",
                "path": LLM_DIR, "present": llm_ok,
                "size": _dir_size(LLM_DIR) if llm_ok else 0,
                "tokenizer": os.path.exists(os.path.join(LLM_DIR, "tokenizer.json")),
                "ctranslate2": ct2, "cuda_devices": cuda,
                "compute": "int8_float16 on the GPU" if cuda else "int8 on the CPU",
                "last_used": _last_use("llm")},
        "whisper_large": {"name": "large-v3", "path": WHISPER_LARGE_DIR, "present": wl_ok,
                          "size": _dir_size(WHISPER_LARGE_DIR) if wl_ok else 0,
                          "last_used": _last_use("whisper")},
        "nllb": {"path": NLLB_DIR, "present": os.path.isdir(NLLB_DIR),
                 "size": _dir_size(NLLB_DIR) if os.path.isdir(NLLB_DIR) else 0},
        "languages": languages(),
        "state": dict(LLM_STATE),
        "lane": heavy.state(),
    }


def languages() -> list:
    """Every audio language the library holds, and what the Subtitle maker
    can do for it - worked out from the library each time, so a language that
    shows up tomorrow is on this list tomorrow with nothing to add."""
    from . import langpolicy
    from .submake_worker import lang_plan
    tot: dict = {}
    libs: dict = {}
    for lib, sides in (langpolicy.library_languages() or {}).items():
        for code, n in ((sides or {}).get("audio") or {}).items():
            tot[code] = tot.get(code, 0) + int(n or 0)
            libs.setdefault(code, []).append(lib)
    out = []
    for code, n in sorted(tot.items(), key=lambda x: -x[1]):
        p = lang_plan(code)
        out.append({"code": code, "name": p["name"], "tracks": n, "libraries": libs[code],
                    "english": p["a2"] == "en", "untagged": code in ("und", "un", "zxx", "mis", ""),
                    "speech": bool(p["whisper"]), "picture": p["ocr"] or "",
                    "translate": p["a2"] != "en"})
    return out


def remove_nllb() -> dict:
    import shutil
    if not os.path.isdir(NLLB_DIR):
        return {"ok": True, "freed": 0}
    n = _dir_size(NLLB_DIR)
    shutil.rmtree(NLLB_DIR, ignore_errors=True)
    joblog.log(f"removed the unused NLLB translation model ({n / 1e9:.1f} GB)", "info")
    return {"ok": not os.path.isdir(NLLB_DIR), "freed": n}


async def llm_job(what: str) -> None:
    """'test' translates a few lines; 'fetch' downloads the model. Both in a
    worker process, on the heavy lane, so neither can collide with a run."""
    got, holder = heavy.claim(f"Qwen {what}", ttl=3600)
    if not got:
        LLM_STATE.update(error=f"{holder} is using the GPU lane - try when it finishes", busy="")
        return
    LLM_STATE.update(busy=what, result=None, error="", at=time.time())
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.submake_worker", "--src", "-", "--out", "-",
            "--data", str(DATA_DIR), "--ffmpeg", "-", f"--llm-{what}", cwd=root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=NO_WINDOW)
        out, _ = await p.communicate()
        for ln in out.decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(ln)
            except Exception:                                    # noqa: BLE001
                continue
            if "llm" in d:
                LLM_STATE["result"] = d["llm"]
            if d.get("error"):
                LLM_STATE["error"] = d["error"]
        if p.returncode and not LLM_STATE["result"] and not LLM_STATE["error"]:
            LLM_STATE["error"] = f"the worker stopped (exit {p.returncode})"
    finally:
        LLM_STATE["busy"] = ""
        heavy.release(f"Qwen {what}")


def snapshot() -> dict:
    q = _queue()
    rows = []
    for x in q:
        r = _row(int(x["file_id"]))
        if r:
            rows.append({"file_id": r["id"], "label": display_label(r["title"], r["season"], r["episode"], r["path"]),
                         "library": r["library"] or "", "file": os.path.basename(r["path"]),
                         "lang": _lang_of(r), "at": x.get("at")})
    return {"state": dict(STATE), "queue": rows, "recent": _recent(), "lane": heavy.state()}


async def watch() -> None:
    """Resume a queue left by a restart."""
    await asyncio.sleep(120)
    if _queue():
        kick()
