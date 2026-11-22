"""Find fuller copies: replace starved files with a fuller release.

The Bitrate check lists titles whose files sit under the floor for their
codec and resolution. This is the fix it offers: per file under the floor,
look for a fuller copy of that episode (or film) and hand it to the arr.

THE SEARCH IS THE PROWLARR SEARCHER'S ENGINE (prowlsearch.find_fuller): every
Prowlarr indexer, every name read with the arr's own parser, ranked by the
quality profile - so the fuller copy is never a worse release by the
profile's judgement (group tier, English audio, bans, blocklist). Kept: one
episode (no packs), not Remux, at least the file's resolution, implied
bitrate >= NEED_X x the file's and >= the floor, torrents with 2+ seeders.

ONE TITLE AT A TIME, as an asyncio task; titles queue. STATE is what the page
polls: the title, the file and the step, counts, and the last results.

AUTO BACKLOG (kv fuller.auto = 1): every AUTO_EVERY_S the Bitrate check's
"under" list is read, most starved first, and titles not tried lately are
queued until AUTO_FILES files are lined up. A title is tried again after
RETRY_GRABBED_S when something was grabbed (the download may fail and the
file stay starved) and after RETRY_NONE_S when nothing fuller existed.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque

from . import joblog
from .config import SETTINGS
from .db import cursor, kv_get, kv_set

NEED_X = 1.5
LOG_KEEP = 60
KV_AUTO = "fuller.auto"
KV_TRIED = "fuller.tried"
AUTO_EVERY_S = 3 * 3600
AUTO_FILES = 40
RETRY_GRABBED_S = 3 * 86400
RETRY_NONE_S = 21 * 86400

STATE: dict = {"running": False, "title": "", "arr": "", "pid": 0, "step": "", "file": "", "done": 0, "total": 0,
               "grabbed": 0, "none": 0, "skipped": 0, "started": 0.0, "log": [], "finished": [], "error": "",
               "cancel": False, "auto_next": 0.0, "auto_last": "", "source": ""}
_Q: deque = deque()
_TASK: asyncio.Task | None = None


def _arr(name: str):
    return next((a for a in SETTINGS.arrs if a.enabled and a.name == name), None)


def _call(a, path: str, method: str = "GET", body=None, timeout: float = 120.0):
    from .arrqueue import _call as c
    return c(a, path, method, body, timeout)


def auto_on() -> bool:
    return str(kv_get(KV_AUTO) or "0") == "1"


def set_auto(on: bool) -> dict:
    kv_set(KV_AUTO, "1" if on else "0")
    joblog.log(f"Fuller copies: auto backlog {'on' if on else 'off'}", "info")
    if on and not STATE["running"] and not _Q:
        STATE["auto_next"] = time.time() + 5
    return snapshot()


def _tried() -> dict:
    try:
        return json.loads(kv_get(KV_TRIED) or "{}")
    except Exception:                                            # noqa: BLE001
        return {}


def _mark(arr: str, pid: int, grabbed: int) -> None:
    t = _tried()
    t[f"{arr}/{pid}"] = {"at": time.time(), "grabbed": grabbed}
    kv_set(KV_TRIED, json.dumps(t))


def _say(kind: str, text: str) -> None:
    STATE["log"] = ([{"at": time.time(), "kind": kind, "title": STATE["title"], "text": text}]
                    + STATE["log"])[:LOG_KEEP]


def snapshot() -> dict:
    s = {k: v for k, v in STATE.items()}
    s["queue"] = [{"arr": q["arr"], "pid": q["pid"], "title": q["title"]} for q in list(_Q)]
    s["auto"] = auto_on()
    s["now"] = time.time()
    return s


def _ensure_worker() -> None:
    global _TASK
    if _Q and (_TASK is None or _TASK.done()):
        _TASK = asyncio.get_running_loop().create_task(_worker())


def queue(items: list, source: str = "by hand") -> dict:
    """items: [{arr, pid, title}]. Already queued or running titles are not added twice."""
    added = 0
    have = {(q["arr"], int(q["pid"])) for q in _Q}
    if STATE["running"]:
        have.add((STATE["arr"], int(STATE.get("pid") or 0)))
    for it in items or []:
        try:
            key = (str(it["arr"]), int(it["pid"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key in have or not _arr(key[0]):
            continue
        _Q.append({"arr": key[0], "pid": key[1], "title": str(it.get("title") or ""), "source": source})
        have.add(key)
        added += 1
    _ensure_worker()
    return {"added": added, **snapshot()}


def cancel() -> dict:
    n = len(_Q)
    _Q.clear()
    if STATE["running"]:
        STATE["cancel"] = True
    return {"cleared": n, **snapshot()}


async def _worker() -> None:
    while _Q:
        it = _Q.popleft()
        STATE.update(running=True, title=it["title"], arr=it["arr"], pid=it["pid"], step="reading the files",
                     file="", done=0, total=0, grabbed=0, none=0, skipped=0, started=time.time(),
                     error="", cancel=False, source=it.get("source") or "")
        try:
            await _title(it)
        except Exception as e:                                   # noqa: BLE001
            STATE["error"] = f"{type(e).__name__}: {e}"[:200]
            _say("error", STATE["error"])
            joblog.log(f"Fuller copies: {it['title']} failed - {STATE['error']}", "warn")
        _mark(it["arr"], it["pid"], STATE["grabbed"])
        STATE["finished"] = ([{"at": time.time(), "arr": it["arr"], "pid": it["pid"], "title": it["title"],
                               "grabbed": STATE["grabbed"], "none": STATE["none"], "skipped": STATE["skipped"],
                               "error": STATE["error"], "source": STATE["source"]}] + STATE["finished"])[:60]
        joblog.log(f"Fuller copies ({STATE['source']}): {it['title']} - {STATE['grabbed']} grabbed, "
                   f"{STATE['none']} with nothing fuller, {STATE['skipped']} skipped", "info")
        if STATE.get("cancel"):
            break
    STATE.update(running=False, step="", file="", cancel=False)


def _under_files(arr: str, pid: int) -> list:
    """The title's files under the floor, as the Bitrate check judges them."""
    from . import bitrate as br
    with cursor() as cur:
        rows = cur.execute(
            "SELECT id, arr_file_id, library, video_codec, height, duration, bitrate, size, season, episode "
            "FROM files WHERE arr_name=? AND arr_parent_id=? AND state != 'deleted' "
            "AND video_codec IS NOT NULL AND duration > ?", (arr, pid, br.MIN_SECS)).fetchall()
        meas = {r[0]: r[1] for r in cur.execute(
            "SELECT file_id, MAX(COALESCE(final_vmaf, vmaf)) FROM vmaf_log WHERE file_id IN "
            "(SELECT id FROM files WHERE arr_name=? AND arr_parent_id=?) GROUP BY file_id", (arr, pid))}
    out = []
    for r in rows:
        fam, res = br.family(r["video_codec"]), br.res_class(r["height"])
        lim = br.limits(fam, res, br.is_anim(r["library"]))
        bps = r["bitrate"] or (r["size"] * 8 / r["duration"] if r["size"] and r["duration"] else 0)
        if not lim or not bps:
            continue
        if br.verdict(bps / 1e6, lim, (meas.get(r["id"]) or 0) >= br.MEASURED_OK) != "under":
            continue
        out.append({**dict(r), "mbps": bps / 1e6, "floor": lim[0], "res": res})
    return out


def _min_h(res: str) -> int:
    return {"2160p": 2160, "1080p": 1080, "720p": 720}.get(res, 0)


async def _title(it: dict) -> None:
    from . import prowlsearch
    a = _arr(it["arr"])
    files = await asyncio.to_thread(_under_files, it["arr"], it["pid"])
    STATE["total"] = len(files)
    if not files:
        _say("none", "no file is under the floor any more")
        return
    sonarr = a.kind == "sonarr"
    busy = set()
    try:
        q = (await asyncio.to_thread(_call, a, "/api/v3/queue?page=1&pageSize=1000")).get("records") or []
        busy = {r.get("episodeId") if sonarr else r.get("movieId") for r in q}
    except Exception:                                            # noqa: BLE001
        pass
    if sonarr:
        s = await asyncio.to_thread(_call, a, f"/api/v3/series/{it['pid']}")
        eps = await asyncio.to_thread(_call, a, f"/api/v3/episode?seriesId={it['pid']}")
        by_file: dict = {}
        for e in eps:
            if e.get("episodeFileId"):
                by_file.setdefault(e["episodeFileId"], []).append(e)
        base = {"seriesId": s["id"], "title": s["title"], "tvdbId": s.get("tvdbId"), "profileId": s["qualityProfileId"]}
    else:
        m = await asyncio.to_thread(_call, a, f"/api/v3/movie/{it['pid']}")
        base = {"movieId": m["id"], "title": m["title"], "year": m.get("year"), "tmdbId": m.get("tmdbId"),
                "profileId": m["qualityProfileId"]}
    files.sort(key=lambda f: (f.get("season") or 0, str(f.get("episode") or "")))
    for f in files:
        if STATE.get("cancel"):
            _say("skip", "stopped")
            break
        need = max(f["mbps"] * NEED_X, f["floor"])
        secs = f["duration"] or (0 if sonarr else (m.get("runtime") or 0) * 60) or 2700
        if sonarr:
            es = by_file.get(f["arr_file_id"]) or []
            if len(es) != 1:
                STATE["skipped"] += 1
                STATE["done"] += 1
                _say("skip", f"file {f['arr_file_id']}: " + ("not matched to an episode" if not es
                                                              else "multi-episode file"))
                continue
            e = es[0]
            tag = f"S{e['seasonNumber']:02}E{e['episodeNumber']:02}"
            want = {**base, "episodeId": e["id"], "season": e["seasonNumber"], "episode": e["episodeNumber"]}
            rid = e["id"]
        else:
            tag, want, rid = it["title"], base, it["pid"]
        STATE["file"] = f"{tag} · {f['mbps']:.1f} Mbps → needs {need:.1f}"
        if rid in busy:
            STATE["skipped"] += 1
            STATE["done"] += 1
            _say("skip", f"{tag}: already downloading")
            continue

        def step(s):
            STATE["step"] = s
        try:
            r = await prowlsearch.find_fuller(a, want, int(need * 1e6 / 8 * secs), _min_h(f["res"]), step=step)
        except Exception as ex:                                  # noqa: BLE001
            r = {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:200]}
        if r.get("ok"):
            STATE["grabbed"] += 1
            mbps = r["size"] * 8 / secs / 1e6
            _say("grab", f"{tag}: {f['mbps']:.1f} → {mbps:.1f} Mbps · {r['release'][:90]} · {r['indexer']}"
                         + (f" · {r['seeders']} seeds" if r.get("protocol") == "torrent" else " · usenet")
                         + (" · grabbed past 'not an upgrade'" if str(r.get("how", "")).startswith("grabbed") else ""))
        else:
            STATE["none"] += 1
            _say("none", f"{tag}: {r.get('why') or 'nothing fuller'}")
        STATE["done"] += 1
    STATE["step"] = ""
    STATE["file"] = ""


# ------------------------------------------------------------ auto --------
def _auto_pick() -> list:
    from . import bitrate
    d = bitrate.report(force=True)
    tried = _tried()
    now = time.time()
    out, files = [], 0
    for g in d["under"]:
        if not g.get("pid") or not g.get("arr"):
            continue
        t = tried.get(f"{g['arr']}/{g['pid']}")
        if t and now - t["at"] < (RETRY_GRABBED_S if t.get("grabbed") else RETRY_NONE_S):
            continue
        out.append({"arr": g["arr"], "pid": g["pid"], "title": g["title"]})
        files += g["n"]
        if files >= AUTO_FILES:
            break
    return out


async def watch() -> None:
    STATE["auto_next"] = time.time() + 1800
    while True:
        await asyncio.sleep(30)
        if not auto_on() or time.time() < STATE["auto_next"]:
            continue
        if STATE["running"] or _Q:
            STATE["auto_next"] = time.time() + 600
            continue
        try:
            items = await asyncio.to_thread(_auto_pick)
            r = queue(items, source="auto backlog")
            STATE["auto_last"] = (f"{time.strftime('%H:%M')} queued {r['added']} title(s)" if r["added"]
                                  else f"{time.strftime('%H:%M')} nothing due - every starved title was tried lately")
        except Exception as e:                                   # noqa: BLE001
            STATE["auto_last"] = f"error: {type(e).__name__}: {e}"[:160]
        STATE["auto_next"] = time.time() + AUTO_EVERY_S
