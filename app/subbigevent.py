"""One subtitle event too big for Plex - found and taken out, by itself.

Plex's intro detection runs a small transcode of each episode, and that
transcode carries the subtitle track. A typeset release can hold a single
ASS event that is a megabyte of vector drawing; Plex's transcoder has a fixed
buffer for one event, says "Buffer too small for ASS event", and the whole
detection fails for that file - every night, for good. Seen 2026-11-27 on
Demon Slayer S03E03 (one 1.1 MB event) and Horimiya S01E04 (two of 1.6 MB):
the only two episodes of 38,870 Plex had never analysed. With the events
out, Plex found both intros within a minute.

WHEN IT RUNS: after the daily look at Plex's markers (plexmarkers), for the
episodes that look reports as never analysed - nothing else. A file is only
read for this when Plex has already failed on it.

WHAT IT DOES TO A FILE: every ASS track is read; a Dialogue event over
LIMIT bytes is dropped (the next biggest events in those files were 82 KB
and 20 KB - a normal event is a few hundred bytes); the tracks that changed
are swapped in one mkvmerge pass with their names, languages, flags and
place in the track order; the rebuilt file is compared with the original
(same tracks, attachments, duration) and only then replaces it, by
fileops.safe_replace. Then Plex is told to re-read the file and analyse it.

WHAT IT LEAVES ALONE: a file in use or being worked on by a job; a file with
no oversized event - that is remembered against the file's size, so the same
file is not read again every night for a cause this cannot fix; and never
more than MAX_A_DAY files in one day.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

from .db import kv_get, kv_set

LIMIT = 500_000
MAX_A_DAY = 6
KV = "subbigevent.seen"           # {path: {size, at, result}}
KV_LOG = "subbigevent.log"        # the last 100 files mended
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _seen() -> dict:
    try:
        return json.loads(kv_get(KV) or "{}")
    except Exception:                                            # noqa: BLE001
        return {}


def _remember(path: str, result: str) -> None:
    d = _seen()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    d[path] = {"size": size, "at": time.time(), "result": result}
    if len(d) > 2000:
        for k in sorted(d, key=lambda x: d[x]["at"])[:len(d) - 2000]:
            d.pop(k, None)
    kv_set(KV, json.dumps(d))


def log() -> list:
    try:
        return json.loads(kv_get(KV_LOG) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, **kw)


def strip(path: str) -> dict:
    """{ok, cut, bytes, why}. cut = oversized events removed (0: none found)."""
    from . import fileops, subembed
    from .jobs import outside_commit
    if not path.lower().endswith(".mkv") or not os.path.exists(path):
        return {"ok": False, "cut": 0, "why": "not an mkv on disk"}
    if fileops.is_locked(path):
        return {"ok": False, "cut": 0, "why": "the file is in use", "retry": True}
    merge = subembed._mkvmerge()
    extract = os.path.join(os.path.dirname(merge), "mkvextract.exe")
    if not os.path.exists(extract):
        return {"ok": False, "cut": 0, "why": "mkvextract is not installed"}

    def info(p):
        r = _run([merge, "-J", p], text=True, encoding="utf-8", errors="replace", timeout=180)
        return json.loads(r.stdout or "{}")
    j = info(path)
    tracks = j.get("tracks") or []
    work, cut, cut_bytes, temps = {}, 0, 0, []
    try:
        for t in tracks:
            if t.get("type") != "subtitles" or "SubStationAlpha" not in (t.get("codec") or ""):
                continue
            out = fileops.cache_temp(".ass", "bigevent")
            temps.append(out)
            _run([extract, "tracks", path, f"{t['id']}:{out}"], timeout=900)
            try:
                lines = open(out, "rb").read().split(b"\n")
            except OSError:
                continue
            big = [ln for ln in lines if ln.startswith(b"Dialogue:") and len(ln) > LIMIT]
            if not big:
                continue
            with open(out, "wb") as f:
                f.write(b"\n".join(ln for ln in lines if not (ln.startswith(b"Dialogue:") and len(ln) > LIMIT)))
            work[t["id"]] = out
            cut += len(big)
            cut_bytes += sum(len(x) for x in big)
        if not cut:
            return {"ok": True, "cut": 0, "why": "no oversized subtitle event in this file"}
        ok_room, why_room = fileops.cache_room(os.path.getsize(path))
        if not ok_room:
            return {"ok": False, "cut": 0, "why": why_room, "retry": True}
        tmp = fileops.cache_temp(".mkv", "bigevent")
        temps.append(tmp)
        # the mended tracks go in as files 1..n; --track-order puts each back
        # where the track it replaces was
        cmd = [merge, "-o", tmp, "--subtitle-tracks", "!" + ",".join(str(i) for i in work), path]
        order, n = [], 0
        for t in tracks:
            if t["id"] in work:
                n += 1
                p = t.get("properties") or {}
                cmd += ["--language", "0:" + (p.get("language") or "und"),
                        "--track-name", "0:" + (p.get("track_name") or ""),
                        "--default-track-flag", "0:" + ("yes" if p.get("default_track") else "no"),
                        "--forced-display-flag", "0:" + ("yes" if p.get("forced_track") else "no"),
                        work[t["id"]]]
                order.append(f"{n}:0")
            else:
                order.append(f"0:{t['id']}")
        cmd += ["--track-order", ",".join(order)]
        r = _run(cmd, text=True, encoding="utf-8", errors="replace", timeout=3600)
        if r.returncode not in (0, 1):
            return {"ok": False, "cut": 0, "why": "mkvmerge said: " + (r.stdout or "")[-200:]}
        k = info(tmp)

        def shape(d):
            return [(t.get("type"), t.get("codec"), (t.get("properties") or {}).get("language"),
                     (t.get("properties") or {}).get("track_name"),
                     bool((t.get("properties") or {}).get("default_track")),
                     bool((t.get("properties") or {}).get("forced_track"))) for t in d.get("tracks") or []]
        d0 = float((j.get("container") or {}).get("properties", {}).get("duration") or 0) / 1e9
        d1 = float((k.get("container") or {}).get("properties", {}).get("duration") or 0) / 1e9
        if shape(j) != shape(k) or len(j.get("attachments") or []) != len(k.get("attachments") or []) \
                or (d0 and abs(d0 - d1) > 2):
            return {"ok": False, "cut": 0, "why": "the rebuilt file does not match the original - nothing was replaced"}
        rep = fileops.safe_replace(path, tmp, **outside_commit(path, "subtitle event strip"))
        if not getattr(rep, "ok", False):
            return {"ok": False, "cut": 0, "why": "could not put the rebuilt file in place", "retry": True}
        return {"ok": True, "cut": cut, "bytes": cut_bytes, "tracks": len(work), "why": ""}
    finally:
        for p in temps:
            try:
                fileops._quiet_remove(p)
            except Exception:                                    # noqa: BLE001
                pass


def _follow(path: str) -> None:
    """Only subtitle tracks changed: the file's audio and decode verdicts follow it."""
    try:
        from .db import cursor
        with cursor() as cur:
            cur.execute("SELECT id FROM files WHERE path = ?", (path,))
            row = cur.fetchone()
        if not row:
            return
        fid = int(row["id"])
        from . import audiolang as _al, integrity as _integ, jobs as _jobs
        from .config import quiet_run
        _al.restamp(fid, path)
        _integ.carry(fid, path)
        pr = quiet_run([_jobs._ffprobe_exe(), "-v", "quiet", "-print_format", "json", "-show_streams",
                        "-show_format", path], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
        data = json.loads(pr.stdout or "{}")
        if data.get("streams"):
            _jobs.cache_probe(fid, data)
    except Exception:                                            # noqa: BLE001
        pass


def _busy(path: str) -> str:
    try:
        from . import jobs
        from .db import cursor
        with cursor() as cur:
            cur.execute("SELECT id FROM files WHERE path = ?", (path,))
            row = cur.fetchone()
        return jobs.busy_with(int(row["id"])) if row else ""
    except Exception:                                            # noqa: BLE001
        return ""


async def unblock(missed: list) -> list:
    """missed: plexmarkers' never-analysed episodes, each with file + key.
    Returns what was done, one row per file touched."""
    from . import joblog, plexnotify
    seen, done, today = _seen(), [], 0
    day = time.time() - 86400
    today = sum(1 for x in log() if (x.get("at") or 0) > day)
    for m in missed:
        path, key = m.get("file") or "", str(m.get("key") or "")
        if not path or not os.path.exists(path) or today >= MAX_A_DAY:
            continue
        was = seen.get(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if was and was.get("size") == size and was.get("result") != "retry":
            continue                                  # read already, as it is now
        if _busy(path):
            continue
        name = f"{m.get('show') or ''} {m.get('ep') or ''}".strip() or os.path.basename(path)
        res = await asyncio.to_thread(strip, path)
        if res.get("retry"):
            _remember(path, "retry")
            continue
        if not res.get("ok"):
            _remember(path, "failed")
            joblog.log(f"plex markers: {name} - could not be mended ({res.get('why')})", "warn")
            done.append({"name": name, "ok": False, "why": res.get("why")})
            continue
        if not res.get("cut"):
            _remember(path, "none")
            joblog.log(f"plex markers: {name} has never been analysed by Plex and holds no oversized "
                       "subtitle event - the cause is something else", "warn")
            done.append({"name": name, "ok": True, "cut": 0, "why": res.get("why")})
            continue
        today += 1
        await asyncio.to_thread(_follow, path)
        _remember(path, "mended")
        joblog.log(f"plex markers: {name} - {res['cut']} oversized subtitle event(s) removed "
                   f"({res['bytes'] / 1e6:.1f} MB) so Plex can analyse the file; Plex asked to re-read it", "ok")
        rows = ([{"at": time.time(), "name": name, "file": os.path.basename(path), "cut": res["cut"],
                  "bytes": res["bytes"]}] + log())[:100]
        kv_set(KV_LOG, json.dumps(rows))
        done.append({"name": name, "ok": True, "cut": res["cut"], "bytes": res["bytes"]})
        try:
            await asyncio.to_thread(plexnotify.refresh_path, path, "subtitle event stripped")
            await asyncio.sleep(75)                    # Plex re-reads the file first
            if key:
                await asyncio.to_thread(plexnotify.analyze, key)
        except Exception:                                        # noqa: BLE001
            pass
    return done
