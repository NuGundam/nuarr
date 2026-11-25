r"""nuarr - is this episode really the previous one under a new name?

FBI S09E08 "22 Minutes" (2026-11-24) was S09E07 "Posterity", twice: a 0zzi
release on the Monday and a FLUX one the day after, both posted a week before
E08 aired. Each decodes perfectly, is the right length, speaks the right
language and has the right name. Only listening to it NEXT TO the episode
before it shows what it is.

HOW. Six 30-second clips from across the new file are looked for in the
neighbour's sound, within REACH_S either side of the same place. Sound is
compared as band energies (16 bands, 20 frames a second), so a different
codec, bitrate, volume or an inch of trimmed recap does not matter. Measured
on FBI: the same recording scores 0.91 - 1.00 at every point, different
episodes 0.10 - 0.50. A shared intro or credits can match at one point, which
is why HIT_SHARE of them have to. Two clips are tried first and two misses end
it, which is how nearly every pair ends.

COMPARED WITH the two episodes before it and the one after, same season, when
the lengths are close. Single-episode Sonarr files only; specials are skipped
(a recap special really is the same sound).

WHICH OF THE TWO IS WRONG is never guessed from the sound - that cannot tell.
It is read from the dates: if the other file was on disk half a day or more
before this episode aired, then the sound existed before this episode did, and
this is the fake. That is certain, and the integrity check's mode (auto or
manual) decides whether the remedy acts. Anything else - two old episodes
downloaded in one evening - is listed for a person.

A FAKE OF AN EPISODE THAT HAS NOT AIRED is deleted and the episode is
unmonitored until its air time, because there is nothing real to search for:
every search would bring the next fake. It is not banned by name either - the
real release, when it comes, will very likely have the same one.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime

from . import joblog
from .config import NO_WINDOW, SETTINGS
from .db import cursor, kv_get, kv_set

KIND = "file/same-episode"
KEY = "samecheck"
TITLE = "Same episode twice"
ENABLED = "samecheck.enabled"    # on unless turned off
HOLDS = "samecheck.holds"        # [{arr, episode, air, title, at}]

SR, HOP, WIN = 8000, 400, 1024   # 20 frames a second
CLIP_S, REACH_S = 30.0, 100.0
POINTS = (.45, .75, .15, .30, .60, .88)   # the first two decide most pairs
HIT = 0.80
HIT_SHARE = 0.8
MIN_HEARD = 4
LAG_SPREAD_S = 60.0
QUIET = 0.0008                   # a clip below this RMS proves nothing
LEN_TOL = 0.12                   # neighbours further apart in length are not compared
MIN_LEN_S = 240.0
BACK_DAYS = 30                   # how far back the first pass looks
EARLY_S = 12 * 3600.0
PASS = 40
RETRY_S = 1800.0
_LATER: dict = {}                # a pair that could not be read, and when to ask again
_READY = False
_EDGES = None


def init() -> None:
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS same_checked(
                file_id INTEGER, other_id INTEGER, size_key TEXT,
                at REAL, verdict TEXT, detail TEXT,
                PRIMARY KEY(file_id, other_id, size_key))""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS same_episode(
                file_id INTEGER PRIMARY KEY, other_id INTEGER,
                path TEXT, title TEXT, why TEXT, arr_file_id INTEGER,
                certain INTEGER DEFAULT 0, at REAL, kept INTEGER DEFAULT 0)""")
    _READY = True


# ------------------------------------------------------------ the sound ----
def _edges():
    global _EDGES
    if _EDGES is None:
        import numpy as np
        _EDGES = np.unique(np.round(np.geomspace(150, 3600, 17) / (SR / WIN)).astype(int))
    return _EDGES


def _sound(path: str, at: float, secs: float, stream: int):
    """(band energies, rms) or None when nothing came back."""
    import numpy as np
    from .jobs import _ffmpeg_exe
    p = subprocess.run(
        [_ffmpeg_exe(), "-v", "error", "-nostdin", "-ss", f"{max(0.0, at):.2f}", "-t", f"{secs:.2f}",
         "-i", path, "-map", f"0:a:{stream}", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "-"], capture_output=True, timeout=240, creationflags=NO_WINDOW)
    x = np.frombuffer(p.stdout[:len(p.stdout) // 2 * 2], np.int16).astype(np.float32) / 32768
    n = (len(x) - WIN) // HOP
    if n < 20:
        return None
    idx = np.arange(WIN)[None] + HOP * np.arange(n)[:, None]
    s = np.abs(np.fft.rfft(x[idx] * np.hanning(WIN), axis=1)) ** 2
    e = _edges()
    b = np.stack([s[:, i:j].sum(1) for i, j in zip(e[:-1], e[1:])], 1)
    return np.log10(b + 1e-4).astype(np.float64), float(np.sqrt((x ** 2).mean()))


def _best(short, long):
    """Best normalised correlation of `short` inside `long`, and the frame."""
    import numpy as np
    n, m = len(short), len(long)
    if m < n:
        return 0.0, 0
    a = short - short.mean(0)
    long = long - long.mean(0)
    fl = 1 << int(np.ceil(np.log2(m + n)))
    num = np.fft.irfft(np.fft.rfft(long, fl, axis=0) * np.conj(np.fft.rfft(a, fl, axis=0)),
                       fl, axis=0)[:m - n + 1].sum(1)
    z = np.zeros((1, long.shape[1]))
    c1 = np.vstack([z, np.cumsum(long, 0)])
    c2 = np.vstack([z, np.cumsum(long ** 2, 0)])
    var = (c2[n:] - c2[:-n]) - (c1[n:] - c1[:-n]) ** 2 / n
    den = np.sqrt(max(float((a ** 2).sum()), 1e-9) * np.maximum(var.sum(1), 1e-3))
    r = num / den
    k = int(r.argmax())
    return float(r[k]), k


def _streams(a: dict, b: dict) -> tuple[int, int]:
    """The first language both files have, as an audio stream index in each."""
    la = [x.strip() for x in str(a.get("audio_langs") or "").split(",") if x.strip()]
    lb = [x.strip() for x in str(b.get("audio_langs") or "").split(",") if x.strip()]
    for i, x in enumerate(la):
        if x in lb:
            return i, lb.index(x)
    return 0, 0


def compare(a: dict, b: dict) -> dict:
    """{verdict: same|different|unknown, points: [...]}. Reads both files."""
    sa, sb = _streams(a, b)
    pts, lags = [], []
    for i, p in enumerate(POINTS):
        at = p * float(a["duration"])
        fa = _sound(a["path"], at, CLIP_S, sa)
        fb = _sound(b["path"], at - REACH_S, CLIP_S + 2 * REACH_S, sb)
        if fa is None or fb is None or len(fa[0]) < CLIP_S * SR / HOP * .6 or fa[1] < QUIET:
            pts.append(None)
        else:
            r, k = _best(fa[0], fb[0])
            pts.append(round(r, 3))
            if r >= HIT:
                lags.append(k * HOP / SR - min(REACH_S, at))
        heard = [x for x in pts if x is not None]
        if i >= 1 and len(heard) >= 2 and not [x for x in heard if x >= HIT]:
            return {"verdict": "different", "points": pts}
    heard = [x for x in pts if x is not None]
    if len(heard) < MIN_HEARD:
        return {"verdict": "unknown", "points": pts}
    hits = [x for x in heard if x >= HIT]
    same = len(hits) >= max(MIN_HEARD, HIT_SHARE * len(heard)) and max(lags) - min(lags) <= LAG_SPREAD_S
    return {"verdict": "same" if same else "different", "points": pts}


# --------------------------------------------------------- what to compare --
def _num(v) -> int | None:
    s = str(v if v is not None else "").strip()
    return int(s) if s.isdigit() else None


def _key(a: dict, b: dict) -> str:
    """A conversion keeps the sound, so size is not part of this; a new
    download is a new arr file or a new first_seen."""
    return f"{a.get('arr_file_id')}/{b.get('arr_file_id')}"


def enabled() -> bool:
    return str(kv_get(ENABLED) or "1") == "1"


def set_enabled(on: bool) -> None:
    from . import idle
    kv_set(ENABLED, "1" if on else "0")
    idle.bump(KEY)


def _pending(limit: int = PASS) -> list:
    """[(file, neighbour)] not compared yet, newest file first."""
    if not _READY:
        init()
    if not enabled():
        return []
    sonarr = [c.name for c in SETTINGS.arrs if c.enabled and c.kind == "sonarr"]
    if not sonarr:
        return []
    q = ",".join("?" * len(sonarr))
    cols = ("id, arr_name, arr_file_id, arr_parent_id, path, library, title, season, episode, "
            "duration, audio_langs, pool_disk, first_seen")
    with cursor() as cur:
        new = [dict(r) for r in cur.execute(
            f"SELECT {cols} FROM files WHERE arr_name IN ({q}) AND first_seen > ? AND season > 0 "
            "  AND duration > ? AND arr_file_id IS NOT NULL AND state NOT IN ('deleted','duplicate') "
            " ORDER BY first_seen DESC", (*sonarr, time.time() - BACK_DAYS * 86400, MIN_LEN_S))]
        seasons: dict = {}
        for k in {(r["arr_name"], r["arr_parent_id"], r["season"]) for r in new}:
            seasons[k] = {}
            for r in cur.execute(
                    f"SELECT {cols} FROM files WHERE arr_name=? AND arr_parent_id=? AND season=? "
                    "  AND duration > ? AND arr_file_id IS NOT NULL "
                    "  AND state NOT IN ('deleted','duplicate')", (*k, MIN_LEN_S)):
                n = _num(r["episode"])
                if n is not None:
                    seasons[k][n] = dict(r)
        done = {(r[0], r[1], r[2]) for r in cur.execute("SELECT file_id, other_id, size_key FROM same_checked")}
    out, seen = [], set()
    for r in new:
        n = _num(r["episode"])
        if n is None:
            continue
        s = seasons.get((r["arr_name"], r["arr_parent_id"], r["season"])) or {}
        for m in (n - 1, n - 2, n + 1):
            o = s.get(m)
            if not o or o["id"] == r["id"]:
                continue
            pair = frozenset((r["id"], o["id"]))
            if pair in seen:
                continue
            seen.add(pair)
            if abs(o["duration"] - r["duration"]) > LEN_TOL * r["duration"]:
                continue
            if (r["id"], o["id"], _key(r, o)) in done or (o["id"], r["id"], _key(o, r)) in done:
                continue
            if _LATER.get((r["id"], o["id"]), 0) > time.time():
                continue
            out.append((r, o))
            if len(out) >= limit:         # a short pass, so a new arrival is not behind the backlog
                return out
    return out


def _tag(r: dict) -> str:
    return f"S{int(r['season']):02}E{int(r['episode']):02}"


def _ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:                                            # noqa: BLE001
        return 0.0


def judge(a: dict, b: dict, air_a: float, air_b: float) -> int | None:
    """0 = a is certainly the wrong one, 1 = b is, None = the dates do not say."""
    def wrong(x_air: float, y_air: float, y_seen: float) -> bool:
        # y's file was here before x aired, and y itself had aired by then
        return bool(x_air and y_air and y_seen < x_air - EARLY_S and y_seen >= y_air - EARLY_S)
    wa = wrong(air_a, air_b, float(b["first_seen"] or 0))
    wb = wrong(air_b, air_a, float(a["first_seen"] or 0))
    if wa == wb:
        return None
    return 0 if wa else 1


async def _episodes(arr_name: str, series_id: int) -> list:
    from .arr import shared_client
    cfg = next(c for c in SETTINGS.arrs if c.name == arr_name)
    return await shared_client(cfg)._get("/episode", seriesId=int(series_id))


def _store(a: dict, b: dict, res: dict) -> None:
    with cursor() as cur:
        cur.execute("INSERT OR REPLACE INTO same_checked(file_id,other_id,size_key,at,verdict,detail) "
                    "VALUES(?,?,?,?,?,?)", (a["id"], b["id"], _key(a, b), time.time(), res["verdict"],
                                            json.dumps(res["points"])))


async def _do_one(pair, report=None) -> dict:
    a, b = pair
    _LATER[(a["id"], b["id"])] = time.time() + RETRY_S
    if not (os.path.exists(a["path"]) and os.path.exists(b["path"])):
        return {"ok": False}                # being replaced or renamed; asked again next pass
    res = await asyncio.to_thread(compare, a, b)
    if res["verdict"] == "unknown" and not (os.path.exists(a["path"]) and os.path.exists(b["path"])):
        return {"ok": False}
    if res["verdict"] == "same":
        await _found(a, b, res)             # before the pair is marked done: a failure here is retried
    await asyncio.to_thread(_store, a, b, res)
    _LATER.pop((a["id"], b["id"]), None)
    return {"ok": True}


async def _found(a: dict, b: dict, res: dict) -> None:
    eps = await _episodes(a["arr_name"], a["arr_parent_id"])
    by = {(e["seasonNumber"], e["episodeNumber"]): e for e in eps}
    ea = by.get((int(a["season"]), int(a["episode"]))) or {}
    eb = by.get((int(b["season"]), int(b["episode"]))) or {}
    w = judge(a, b, _ts(ea.get("airDateUtc")), _ts(eb.get("airDateUtc")))
    # when the dates do not say, the one that arrived later is the one listed
    bad, good, ebad = (a, b, ea) if w == 0 or (w is None and a["first_seen"] >= b["first_seen"]) else (b, a, eb)
    heard = [x for x in res["points"] if x is not None]
    why = (f"same recording as {_tag(good)} ({sum(x >= HIT for x in heard)} of {len(heard)} clips match, "
           f"{min(heard):.2f}-{max(heard):.2f})")
    if w is None:
        why += " - the dates do not say which of the two is the wrong one"
    else:
        why += f"; {_tag(good)} was here before {_tag(bad)} aired"
    with cursor() as cur:
        cur.execute(
            "INSERT INTO same_episode(file_id,other_id,path,title,why,arr_file_id,certain,at,kept) "
            "VALUES(?,?,?,?,?,?,?,?,0) ON CONFLICT(file_id) DO UPDATE SET other_id=excluded.other_id, "
            "path=excluded.path, why=excluded.why, certain=excluded.certain, at=excluded.at, "
            "kept=CASE WHEN same_episode.arr_file_id=excluded.arr_file_id THEN same_episode.kept ELSE 0 END, "
            "arr_file_id=excluded.arr_file_id",
            (bad["id"], good["id"], bad["path"], f"{bad['title']} {_tag(bad)}", why, bad["arr_file_id"],
             0 if w is None else 1, time.time()))
    joblog.log(f"same episode: {bad['title']} {_tag(bad)} - {why}", "warn")
    if w is not None:
        from . import integrity, remedy
        await remedy.auto([f for f in findings() if f["file_id"] == bad["id"]], KEY,
                          integrity.mode() == "auto")
    else:
        try:
            from . import userinput
            userinput.rows(fresh=True)
        except Exception:                                        # noqa: BLE001
            pass


# ------------------------------------------------------------- the panel ----
def findings(limit: int = 200) -> list[dict]:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return [{"file_id": r["file_id"], "kind": KIND, "path": r["path"], "title": r["title"],
                     "at": r["at"], "keep": True, "certain": bool(r["certain"]), "why": r["why"]}
                    for r in cur.execute(
                        "SELECT s.* FROM same_episode s JOIN files f ON f.id=s.file_id "
                        "  JOIN files o ON o.id=s.other_id "
                        " WHERE COALESCE(s.kept,0)=0 AND f.arr_file_id=s.arr_file_id "
                        "   AND f.state NOT IN ('deleted','duplicate') "
                        "   AND o.state NOT IN ('deleted','duplicate') "
                        " ORDER BY s.at DESC LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def keep(file_id: int) -> bool:
    if not _READY:
        init()
    with cursor() as cur:
        return bool(cur.execute("UPDATE same_episode SET kept=1 WHERE file_id=?", (int(file_id),)).rowcount)


_LEFT = {"at": 0.0, "n": 0}


def stats() -> dict:
    """The Janitor panel."""
    from . import idle, integrity
    if not _READY:
        init()
    if time.time() - _LEFT["at"] > 60:
        _LEFT.update(n=len(_pending(10 ** 9)), at=time.time())
    with cursor() as cur:
        n = {r[0]: r[1] for r in cur.execute("SELECT verdict, COUNT(*) FROM same_checked GROUP BY 1")}
        last = [dict(r) for r in cur.execute(
            "SELECT c.at, c.verdict, c.detail, f.title, f.season, f.episode, o.episode AS other "
            "  FROM same_checked c JOIN files f ON f.id=c.file_id JOIN files o ON o.id=c.other_id "
            " ORDER BY c.at DESC LIMIT 12")]
        rows = [dict(r) for r in cur.execute(
            "SELECT s.*, f.state, f.arr_file_id AS now_id, o.state AS ostate, o.path AS opath, "
            "  (SELECT detail FROM remedy_log r WHERE r.file_id=s.file_id AND r.kind=? AND r.ok=1 "
            "    ORDER BY r.at DESC LIMIT 1) AS did, "
            "  (SELECT detail FROM remedy_log r WHERE r.file_id=s.file_id AND r.kind=? AND r.ok=0 "
            "    ORDER BY r.at DESC LIMIT 1) AS failed "
            "  FROM same_episode s LEFT JOIN files f ON f.id=s.file_id LEFT JOIN files o ON o.id=s.other_id "
            " ORDER BY s.at DESC LIMIT 100", (KIND, KIND))]
    out = []
    for r in rows:
        gone = r["state"] in (None, "deleted", "duplicate") or r["now_id"] != r["arr_file_id"]
        other_gone = r["ostate"] in (None, "deleted", "duplicate")
        st = ("removed" if r["did"] else "kept" if r["kept"] else "gone" if gone
              else "othergone" if other_gone else "auto" if r["certain"] else "yours")
        out.append({"file_id": r["file_id"], "title": r["title"], "path": r["path"], "other": r["opath"] or "",
                    "why": r["why"], "at": r["at"], "state": st, "did": r["did"] or "",
                    "failed": r["failed"] or ""})
    p = idle.progress(KEY)
    return {"on": enabled(), "mode": integrity.mode(), "back_days": BACK_DAYS, "hit": HIT,
            "compared": sum(n.values()), "unknown": n.get("unknown", 0), "left": _LEFT["n"],
            "rows": out, "holds": sorted(_holds(), key=lambda h: h["air"]),
            "last": [{**r, "points": json.loads(r["detail"] or "[]")} for r in last],
            "run": {k: p.get(k) for k in ("running", "paused", "paused_why", "now", "last_error", "secs_each")}
            | {"flight": [f.get("now") for f in p.get("flight") or []]}}


async def look_now() -> dict:
    from . import idle
    _LATER.clear()
    _LEFT["at"] = 0.0
    idle.bump(KEY)
    await release_holds()
    return await asyncio.to_thread(stats)


async def unhold(episode: int) -> dict:
    """Monitor a held episode now instead of when it airs."""
    from .arr import shared_client
    h = next((x for x in _holds() if x["episode"] == int(episode)), None)
    if not h:
        return {"ok": False, "why": "not held"}
    cl = shared_client(next(c for c in SETTINGS.arrs if c.name == h["arr"]))
    await cl._put("/episode/monitor", {"episodeIds": [h["episode"]], "monitored": True})
    kv_set(HOLDS, json.dumps([x for x in _holds() if x["episode"] != int(episode)]))
    joblog.log(f"same episode: {h['title']} monitored again by hand", "ok")
    return {"ok": True}


# ---------------------------------------------- an episode not aired yet ----
def _holds() -> list:
    try:
        return json.loads(kv_get(HOLDS) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


async def reject_unaired(file_id: int, why: str) -> dict | None:
    """Delete the fake and leave the episode alone until it airs. None when
    the episode has aired - the caller then replaces it the usual way."""
    from .arr import shared_client
    with cursor() as cur:
        r = cur.execute("SELECT * FROM files WHERE id=?", (int(file_id),)).fetchone()
    row = dict(r) if r else None
    if not row or not row.get("arr_file_id"):
        return None
    ep = next((e for e in await _episodes(row["arr_name"], row["arr_parent_id"])
               if e.get("episodeFileId") == row["arr_file_id"]), None)
    air = _ts((ep or {}).get("airDateUtc"))
    if not ep or air <= time.time():
        return None
    cl = shared_client(next(c for c in SETTINGS.arrs if c.name == row["arr_name"]))
    was = bool(ep.get("monitored"))
    if was:
        await cl._put("/episode/monitor", {"episodeIds": [ep["id"]], "monitored": False})
        kv_set(HOLDS, json.dumps([h for h in _holds() if h["episode"] != ep["id"]] + [
            {"arr": row["arr_name"], "episode": ep["id"], "air": air, "at": time.time(),
             "title": f"{row['title']} {_tag(row)}"}]))
    await cl.delete_file(int(row["arr_file_id"]))
    did = ["deleted the file",
           "it has not aired, so nothing was searched for"
           + (" - unmonitored until " + time.strftime("%d %b %H:%M", time.localtime(air)) if was else "")]
    with cursor() as cur:
        cur.execute("UPDATE files SET state='deleted', state_reason=?, updated_at=? WHERE id=?",
                    (f"rejected: {'; '.join(did)}", time.time(), row["id"]))
        cur.execute("INSERT INTO history(file_id,event,detail,at) VALUES(?,?,?,?)",
                    (row["id"], "deleted", f"nuarr rejected this release — {why}: {'; '.join(did)}"[:600],
                     time.time()))
    joblog.log(f"same episode: {row['title']} {_tag(row)}: {'; '.join(did)}", "warn")
    try:
        from . import plexqueue
        plexqueue.enqueue(row["id"], row["path"] or "", why="the release was rejected and deleted")
    except Exception:                                            # noqa: BLE001
        pass
    return {"ok": True, "did": did, "title": row["title"], "path": row["path"]}


async def release_holds() -> int:
    """Monitor each held episode again once it airs. The date is asked for
    afresh: it moves."""
    from .arr import shared_client
    left, n = [], 0
    for h in _holds():
        try:
            cl = shared_client(next(c for c in SETTINGS.arrs if c.name == h["arr"]))
            try:
                e = await cl._get(f"/episode/{h['episode']}")
            except Exception as ex:                              # noqa: BLE001
                if getattr(getattr(ex, "response", None), "status_code", 0) == 404:
                    continue                                     # the episode is gone
                raise
            air = _ts(e.get("airDateUtc")) or h["air"]
            if air > time.time():
                left.append({**h, "air": air})
                continue
            await cl._put("/episode/monitor", {"episodeIds": [h["episode"]], "monitored": True})
            joblog.log(f"same episode: {h['title']} has aired - monitored again", "ok")
            n += 1
        except Exception:                                        # noqa: BLE001
            left.append(h)
    if n or left != _holds():
        kv_set(HOLDS, json.dumps(left))
    return n


async def _hold_loop() -> None:
    from . import schedules
    schedules.register("samecheck", "Episodes held until they air", "Library", 900,
                       what="Monitors an episode again once it airs, after a fake of it was deleted.")
    while True:
        schedules.beat("samecheck")
        try:
            await release_holds()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"same episode holds: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(900)


async def watch() -> None:
    from . import idle
    await asyncio.to_thread(init)
    await asyncio.sleep(200)
    asyncio.create_task(_hold_loop())
    await idle.run(KEY, TITLE, _pending, _do_one,
                   label=lambda p: f"{p[0]['title']} {_tag(p[0])} / {_tag(p[1])}"[:120],
                   disk_of=lambda p: p[0].get("pool_disk") or "",
                   note_of=lambda p: f"listening for the same episode twice ({_tag(p[0])} / {_tag(p[1])})",
                   system_name=TITLE, goto="/settings#janitor@samBox")
