"""Find fuller copies: replace starved files with a fuller release.

The Bitrate check lists titles whose files sit under the floor for their
codec and resolution. This is the fix it offers: per file under the floor,
ask the arr for every release of that episode (or film) and grab the best
one that is genuinely fuller.

A RELEASE IS TAKEN ONLY WHEN
- the arr would accept it, or rejects it only for "not an upgrade"-type
  reasons (the arr does not know a bigger copy of the same quality is
  better; that is exactly the point here). Any other rejection - blocklisted,
  banned group, wrong language, too small - rules it out;
- it is the same resolution or higher, not a Remux, not a season pack, and
  covers one episode (multi-episode releases make the per-episode bitrate
  meaningless);
- its implied bitrate (size x 8 / runtime) is at least NEED_X times the
  file's and at least the floor for its codec and resolution;
- a torrent has MIN_SEEDS seeders or more.
Among those: the highest custom-format score, then the highest bitrate. The
score is the profile's own judgement (group tier, English audio, ...), so the
fuller copy is never a worse release.

USENET COPIES OF SOME 4K WEB RELEASES ARE TAKEN DOWN (Doom Patrol, The Pitt,
Strange New Worlds - "Aborted, cannot be completed" twice). The arr then
blocklists them, so a second run skips them on its own.

One title at a time, in a background thread; titles queue. STATE is what the
page polls: the title and file being searched, counts, and the last results.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from . import joblog
from .config import SETTINGS
from .db import cursor

NEED_X = 1.5
MIN_SEEDS = 2
SOFT = ("equal or higher", "not an upgrade", "not a Custom Format upgrade", "is not wanted in profile",
        "Existing file", "cutoff", "already meets", "existing file on disk")

STATE: dict = {"running": False, "title": "", "arr": "", "step": "", "file": "", "done": 0, "total": 0,
               "grabbed": 0, "none": 0, "skipped": 0, "started": 0.0, "queue": [], "log": [],
               "finished": [], "error": ""}
_Q: deque = deque()
_LOCK = threading.Lock()
_T: threading.Thread | None = None
LOG_KEEP = 60


def _arr(name: str):
    return next((a for a in SETTINGS.arrs if a.enabled and a.name == name), None)


def _call(a, path: str, method: str = "GET", body=None, timeout: float = 180.0):
    from .arrqueue import _call as c
    return c(a, path, method, body, timeout)


def _say(kind: str, text: str) -> None:
    STATE["log"] = ([{"at": time.time(), "kind": kind, "title": STATE["title"], "text": text}]
                    + STATE["log"])[:LOG_KEEP]


def snapshot() -> dict:
    s = dict(STATE)
    s["queue"] = [{"arr": q["arr"], "pid": q["pid"], "title": q["title"]} for q in list(_Q)]
    s["now"] = time.time()
    return s


def queue(items: list) -> dict:
    """items: [{arr, pid, title}]. Already queued or running titles are not added twice."""
    global _T
    added = 0
    with _LOCK:
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
            _Q.append({"arr": key[0], "pid": key[1], "title": str(it.get("title") or "")})
            have.add(key)
            added += 1
        if _Q and (_T is None or not _T.is_alive()):
            _T = threading.Thread(target=_worker, daemon=True, name="fuller")
            _T.start()
    return {"added": added, **snapshot()}


def cancel() -> dict:
    with _LOCK:
        n = len(_Q)
        _Q.clear()
    STATE["cancel"] = True
    return {"cleared": n, **snapshot()}


def _worker() -> None:
    while True:
        with _LOCK:
            if not _Q:
                STATE.update(running=False, step="", file="")
                return
            it = _Q.popleft()
        STATE.update(running=True, title=it["title"], arr=it["arr"], pid=it["pid"], step="reading the files",
                     file="", done=0, total=0, grabbed=0, none=0, skipped=0, started=time.time(),
                     error="", cancel=False)
        try:
            _title(it)
        except Exception as e:                                   # noqa: BLE001
            STATE["error"] = f"{type(e).__name__}: {e}"[:200]
            _say("error", STATE["error"])
            joblog.log(f"Fuller copies: {it['title']} failed - {STATE['error']}", "warn")
        STATE["finished"] = ([{"at": time.time(), "arr": it["arr"], "pid": it["pid"], "title": it["title"],
                               "grabbed": STATE["grabbed"], "none": STATE["none"], "skipped": STATE["skipped"],
                               "error": STATE["error"]}] + STATE["finished"])[:40]
        joblog.log(f"Fuller copies: {it['title']} - {STATE['grabbed']} grabbed, {STATE['none']} with nothing "
                   f"fuller, {STATE['skipped']} skipped", "info")


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


def _height(q: dict) -> int:
    return int(((q or {}).get("quality") or {}).get("resolution") or 0)


def _pick(rels: list, secs: float, need: float, min_h: int):
    ok = []
    for r in rels or []:
        q = (r.get("quality") or {}).get("quality") or {}
        if int(q.get("resolution") or 0) < min_h or "Remux" in str(q.get("name") or "") or r.get("fullSeason"):
            continue
        if len(r.get("mappedEpisodeNumbers") or r.get("episodeNumbers") or [1]) > 1:
            continue
        if [x for x in r.get("rejections") or [] if not any(s in str(x) for s in SOFT)]:
            continue
        if r.get("protocol") == "torrent" and (r.get("seeders") or 0) < MIN_SEEDS:
            continue
        mbps = (r.get("size") or 0) * 8 / secs / 1e6
        if mbps < need:
            continue
        ok.append((r.get("customFormatScore") or 0, mbps, r))
    ok.sort(key=lambda t: (-t[0], -t[1]))
    return ok[0] if ok else None


def _min_h(res: str) -> int:
    return {"2160p": 2160, "1080p": 1080, "720p": 720}.get(res, 0)


def _title(it: dict) -> None:
    a = _arr(it["arr"])
    files = _under_files(it["arr"], it["pid"])
    STATE["total"] = len(files)
    if not files:
        _say("none", "no file is under the floor any more")
        return
    sonarr = a.kind == "sonarr"
    busy = set()
    try:
        q = _call(a, "/api/v3/queue?page=1&pageSize=1000", timeout=60).get("records") or []
        busy = {r.get("episodeId") if sonarr else r.get("movieId") for r in q}
    except Exception:                                            # noqa: BLE001
        pass
    if sonarr:
        eps = _call(a, f"/api/v3/episode?seriesId={it['pid']}")
        by_file: dict = {}
        for e in eps:
            if e.get("episodeFileId"):
                by_file.setdefault(e["episodeFileId"], []).append(e)
    else:
        movie = _call(a, f"/api/v3/movie/{it['pid']}")
    files.sort(key=lambda f: (f.get("season") or 0, str(f.get("episode") or "")))
    for f in files:
        if STATE.get("cancel"):
            _say("skip", "stopped")
            break
        need = max(f["mbps"] * NEED_X, f["floor"])
        secs = f["duration"] or 0
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
            target, rid = f"episodeId={e['id']}", e["id"]
        else:
            tag = it["title"]
            target, rid = f"movieId={it['pid']}", it["pid"]
            secs = secs or (movie.get("runtime") or 0) * 60
        STATE["file"] = f"{tag} · {f['mbps']:.1f} Mbps"
        if rid in busy:
            STATE["skipped"] += 1
            STATE["done"] += 1
            _say("skip", f"{tag}: already downloading")
            continue
        STATE["step"] = "searching the indexers"
        rels = None
        for _try in range(3):
            try:
                rels = _call(a, f"/api/v3/release?{target}", timeout=300)
                break
            except Exception:                                    # noqa: BLE001
                time.sleep(15)
        if rels is None:
            STATE["none"] += 1
            STATE["done"] += 1
            _say("none", f"{tag}: the search failed 3 times")
            continue
        STATE["step"] = "choosing"
        best = _pick(rels, secs or 2700, need, _min_h(f["res"]))
        if not best:
            STATE["none"] += 1
            STATE["done"] += 1
            _say("none", f"{tag}: nothing fuller than {f['mbps']:.1f} Mbps (needs {need:.1f}) among {len(rels)} releases")
            continue
        sc, mbps, r = best
        STATE["step"] = "grabbing"
        try:
            _call(a, "/api/v3/release", "POST", {"guid": r["guid"], "indexerId": r["indexerId"]}, timeout=120)
            STATE["grabbed"] += 1
            _say("grab", f"{tag}: {f['mbps']:.1f} → {mbps:.1f} Mbps · {r.get('title', '')[:90]}"
                         + (f" · {r.get('seeders')} seeds" if r.get("protocol") == "torrent" else " · usenet"))
        except Exception as e:                                   # noqa: BLE001
            STATE["none"] += 1
            _say("error", f"{tag}: grab refused - {type(e).__name__}: {str(e)[:120]}")
        STATE["done"] += 1
    STATE["step"] = ""
    STATE["file"] = ""
