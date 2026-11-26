r"""Subtitles in another language, burned into the picture.

THE CASE. Please Excuse My Younger Brothers S01E14/16/19, "Baha WEB-DL -MWeb":
Japanese audio, no subtitle track, and Chinese captions painted into every
frame of dialogue. nuarr made English subtitles for them - and drew them on
top of the Chinese, so both were unreadable. hardsub.py could not have caught
it: it looks for burned-in ENGLISH (a file that already has what a viewer
needs). This is the opposite finding - a file that carries something nobody
here can read and that can never be taken off.

HOW IT IS FOUND. A dozen frames across the file, the bottom third of each
through the OCR reader hardsub.py already keeps (PaddleOCR reads Chinese,
Korean, Cyrillic). A frame counts when the caption band holds a line in a
script that is not the show's own: four or more Han characters with no kana
(Japanese artwork nearly always carries kana; Chinese captions never do),
Hangul, Cyrillic, Thai or Arabic. Four different such lines and the file is
called burned. A show whose original language uses that script is skipped -
a Chinese drama has Chinese captions in every copy there is.

WHAT HAPPENS THEN
    1  The arr is asked what else exists for that episode. A release from a
       source + group pair already seen burned (Baha + MWeb) does not count.
    2  Something else exists: the file is replaced the usual way (refetch).
    3  Nothing else: the file stays, and the subtitles nuarr made for it move
       to the TOP of the picture, clear of the burned captions. Asked again
       in a week.
    4  A source + group pair seen burned twice and never clean is banned in
       the arrs, so the next episode is not grabbed from it at all.

Only files with no English audio are read: about 5,600 of 40,000.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

from . import joblog
from .config import SETTINGS
from .db import cursor, kv_get, kv_set

KIND = "file/foreign-burn"
KEY = "foreignburn"
TITLE = "Foreign subtitles burned in"
ENABLED = "foreignburn.enabled"
SAMPLES = 12
HITS = 4                     # frames with a foreign caption line, each a different line
GIVE_UP = 7                  # this many frames read and not one hit: clean
MIN_LEN_S = 240.0
PASS = 30
RETRY_S = 7 * 86400.0        # a kept file is asked about again after this
RETRY_READ_S = 1800.0
MAX_PER_EPISODE = 3          # burned copies of one episode before nuarr stops replacing
BAN_AT = 2                   # a source + group pair seen burned this often, never clean
_READY = False
_LATER: dict = {}
_PEND = {"at": 0.0, "rows": []}
_SCENE: dict = {}            # (arr, parent) -> (at, {arr_file_id: sceneName})

# the scripts a caption can be told by, and the original language that makes it the show's own
_HAN = re.compile(r"[一-鿿㐀-䶿]")
_KANA = re.compile(r"[぀-ヿ]")
_SCRIPTS = (("Korean", re.compile(r"[가-힯]"), 4, {"korean"}),
            ("Cyrillic", re.compile(r"[Ѐ-ӿ]"), 6, {"russian", "ukrainian", "bulgarian", "serbian"}),
            ("Thai", re.compile(r"[฀-๿]"), 4, {"thai"}),
            ("Arabic", re.compile(r"[؀-ۿ]"), 4, {"arabic", "persian", "urdu"}))
_OWN_HAN = {"chinese", "mandarin", "cantonese"}


def init() -> None:
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS foreign_burn(
                file_id INTEGER PRIMARY KEY, arr_file_id INTEGER, at REAL,
                verdict TEXT, script TEXT, hits INTEGER, samples INTEGER, sample TEXT,
                path TEXT, title TEXT, epkey TEXT, scene TEXT, source TEXT, grp TEXT,
                state TEXT DEFAULT '', state_at REAL, note TEXT, kept INTEGER DEFAULT 0)""")
    _READY = True


def enabled() -> bool:
    return str(kv_get(ENABLED) or "1") == "1"


def set_enabled(on: bool) -> None:
    from . import idle
    kv_set(ENABLED, "1" if on else "0")
    _PEND["at"] = 0.0
    idle.bump(KEY)


# ------------------------------------------------------------ reading ------
def script_of(text: str, orig: str = "") -> str:
    """The foreign script a caption line is written in, '' when it is not one."""
    t = str(text or "")
    o = str(orig or "").strip().lower()
    if len(_HAN.findall(t)) >= 4 and not _KANA.search(t) and o not in _OWN_HAN:
        return "Chinese"
    for name, rx, need, own in _SCRIPTS:
        if len(rx.findall(t)) >= need and o not in own:
            return name
    return ""


def probe(path: str, duration: float, orig: str = "") -> dict:
    """{'verdict': foreign|none|unknown, 'script', 'hits', 'samples', 'sample'}"""
    from . import hardsub
    if not hardsub._worker_engine(hardsub.ocr_engine()):
        return {"verdict": "unknown", "why": "the OCR reader in use only reads Latin letters"}
    seen: dict = {}
    read = 0
    # spread so the first few already cover the whole file
    order = sorted(range(SAMPLES), key=lambda i: (i % 3, i))
    for i in order:
        t = duration * (0.12 + 0.76 * i / (SAMPLES - 1))
        text = hardsub._read_text(path, t, hardsub.LOW_BAND)
        read += 1
        s = script_of(text, orig)
        if s:
            seen.setdefault(re.sub(r"\s+", "", text)[:40], s)
        if len(seen) >= HITS:
            break
        if read >= GIVE_UP and not seen:
            break
    if len(seen) >= HITS:
        scripts = list(seen.values())
        return {"verdict": "foreign", "script": max(set(scripts), key=scripts.count), "hits": len(seen),
                "samples": read, "sample": " / ".join(list(seen)[:3])}
    return {"verdict": "none", "script": "", "hits": len(seen), "samples": read, "sample": " / ".join(list(seen)[:3])}


# ------------------------------------------------------------ the list -----
_COLS = ("id, arr_name, arr_file_id, arr_parent_id, path, library, title, season, episode, duration, "
         "audio_langs, orig_lang, pool_disk, first_seen, release_group")


def _all_pending() -> list:
    if not _READY:
        init()
    if not enabled():
        return []
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            f"SELECT {_COLS} FROM files f WHERE arr_file_id IS NOT NULL AND duration > ? "
            "  AND state NOT IN ('deleted','duplicate') AND COALESCE(audio_langs,'') NOT LIKE '%eng%' "
            "  AND NOT EXISTS (SELECT 1 FROM foreign_burn b WHERE b.file_id=f.id AND b.arr_file_id=f.arr_file_id) "
            " ORDER BY first_seen DESC", (MIN_LEN_S,))]


def _pending(limit: int = PASS) -> list:
    if time.time() - _PEND["at"] > 120:
        _PEND.update(rows=_all_pending(), at=time.time())
        _due()
    now = time.time()
    return [r for r in _PEND["rows"] if _LATER.get(r["id"], 0) < now][:limit]


def _due() -> None:
    """A file kept for want of anything better is a finding again after a week."""
    try:
        with cursor() as cur:
            cur.execute("UPDATE foreign_burn SET state='', state_at=? WHERE state='kept' AND kept=0 "
                        " AND state_at < ?", (time.time(), time.time() - RETRY_S))
    except Exception:                                            # noqa: BLE001
        pass


def _epkey(r: dict) -> str:
    return f"{r.get('arr_name')}|{r.get('arr_parent_id')}|{r.get('season')}|{r.get('episode')}"


def _label(r: dict) -> str:
    try:
        if r.get("season") is not None and str(r.get("episode") or "") != "":
            return f"{r['title']} S{int(r['season']):02}E{int(r['episode']):02}"
    except (TypeError, ValueError):
        pass
    return str(r.get("title") or os.path.basename(r.get("path") or ""))


def source_of(name: str) -> str:
    """'Baha' from '...1080p.Baha.WEB-DL...' - the service a web release was taken from."""
    m = re.search(r"(?:^|[ ._\[(])([A-Za-z][A-Za-z0-9+]{1,11})[ ._]+WEB(?:[-. ]?DL|[-. ]?Rip)?(?![A-Za-z])",
                  str(name or ""), re.I)
    s = m.group(1) if m else ""
    return "" if s.lower() in ("proper", "repack", "multi", "dual", "the", "and", "uhd", "hdr", "dv") else s


def _pair(name: str) -> tuple:
    from . import arrban
    return (source_of(name).lower(), str(arrban.group_of(name) or "").lower())


async def _scene(r: dict) -> str:
    """The release name the arr imported this file from. One call per series, kept 10 minutes."""
    from .arr import shared_client
    k = (r["arr_name"], r["arr_parent_id"])
    hit = _SCENE.get(k)
    if not hit or time.time() - hit[0] > 600:
        cfg = next((c for c in SETTINGS.arrs if c.name == r["arr_name"]), None)
        names = {}
        if cfg:
            try:
                cl = shared_client(cfg)
                rows = (await cl._get("/episodefile", seriesId=r["arr_parent_id"]) if cfg.kind == "sonarr"
                        else await cl._get("/moviefile", movieId=r["arr_parent_id"])) or []
                names = {x.get("id"): x.get("sceneName") or "" for x in rows}
            except Exception:                                    # noqa: BLE001
                names = {}
        hit = _SCENE[k] = (time.time(), names)
    return hit[1].get(r["arr_file_id"]) or ""


async def _do_one(r: dict, report=None) -> dict:
    _LATER[r["id"]] = time.time() + RETRY_READ_S
    if not os.path.exists(r["path"]):
        return {"ok": False}
    res = await asyncio.to_thread(probe, r["path"], float(r["duration"] or 0), r.get("orig_lang") or "")
    if res["verdict"] == "unknown":
        return {"ok": False, "why": res.get("why", "")}
    if not os.path.exists(r["path"]):
        return {"ok": False}
    scene = await _scene(r)
    src, grp = _pair(scene) if scene else ("", str(r.get("release_group") or "").lower())
    with cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO foreign_burn(file_id,arr_file_id,at,verdict,script,hits,samples,sample,"
            " path,title,epkey,scene,source,grp,state,state_at,note,kept) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',?,'',0)",
            (r["id"], r["arr_file_id"], time.time(), res["verdict"], res.get("script") or "", res["hits"],
             res["samples"], res.get("sample") or "", r["path"], _label(r), _epkey(r), scene, src, grp, time.time()))
    _PEND["rows"] = [x for x in _PEND["rows"] if x["id"] != r["id"]]
    _LATER.pop(r["id"], None)
    if res["verdict"] == "foreign":
        joblog.log(f"foreign subtitles burned in: {_label(r)} has {res['script']} captions in the picture "
                   f"({res['hits']} of {res['samples']} frames) - {os.path.basename(r['path'])}", "warn")
        await asyncio.to_thread(_maybe_ban, src, grp, res["script"])
    return {"ok": True}


def _pairs() -> dict:
    """(source, group) -> [burned, clean]"""
    out: dict = {}
    with cursor() as cur:
        for r in cur.execute("SELECT source, grp, verdict, COUNT(*) n FROM foreign_burn "
                             " WHERE COALESCE(grp,'')<>'' GROUP BY 1,2,3"):
            c = out.setdefault((r["source"] or "", r["grp"]), [0, 0])
            c[0 if r["verdict"] == "foreign" else 1] += r["n"]
    return out


def _maybe_ban(src: str, grp: str, script: str) -> None:
    if not (src and grp):
        return
    burned, clean = _pairs().get((src, grp), [0, 0])
    if burned < BAN_AT or clean:
        return
    try:
        from . import arrban
        rx = f"/(?<![A-Za-z0-9]){re.escape(src)}(?![A-Za-z0-9]).*(?<![A-Za-z0-9]){re.escape(grp)}(?![A-Za-z0-9])/"
        got = arrban.add("term", rx, f"{script} subtitles burned into the picture in {burned} of {burned} "
                                     f"files read from this source and group", source=KEY, by="nuarr")
        if got.get("new"):
            arrban.kick()
            joblog.log(f"foreign subtitles burned in: {src} releases by {grp} are banned in the arrs - "
                       f"{burned} read, every one with {script} captions in the picture", "warn")
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"foreign subtitles burned in: could not ban {src}/{grp}: {type(e).__name__}: {e}", "warn")


# ------------------------------------------------------------ findings -----
def burned(file_id: int) -> bool:
    """Does this file carry foreign captions? Subtitles made for it go at the top."""
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return bool(cur.execute(
                "SELECT 1 FROM foreign_burn b JOIN files f ON f.id=b.file_id AND f.arr_file_id=b.arr_file_id "
                " WHERE b.file_id=? AND b.verdict='foreign' AND b.kept=0", (int(file_id),)).fetchone())
    except Exception:                                            # noqa: BLE001
        return False


def findings(limit: int = 200) -> list[dict]:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return [{"file_id": r["file_id"], "kind": KIND, "path": r["path"], "title": r["title"],
                     "at": r["at"], "keep": True,
                     "why": f"{r['script']} subtitles are burned into the picture "
                            f"({r['hits']} of {r['samples']} frames read)"}
                    for r in cur.execute(
                        "SELECT b.* FROM foreign_burn b JOIN files f ON f.id=b.file_id "
                        " WHERE b.verdict='foreign' AND b.kept=0 AND COALESCE(b.state,'')='' "
                        "   AND f.arr_file_id=b.arr_file_id AND f.state NOT IN ('deleted','duplicate') "
                        " ORDER BY b.at DESC LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def keep(file_id: int) -> bool:
    if not _READY:
        init()
    with cursor() as cur:
        return bool(cur.execute("UPDATE foreign_burn SET kept=1 WHERE file_id=? AND verdict='foreign'",
                                (int(file_id),)).rowcount)


_SOFT = re.compile(r"existing file|on disk|cutoff|upgrade|in queue|equal or higher|preferred|already meets", re.I)


async def better(file_id: int) -> dict | None:
    """Something else the arr could download for this file's episode or film: not the same release,
    not from a source + group pair seen burned, not smaller in resolution."""
    from .arrqueue import _call
    with cursor() as cur:
        f = cur.execute(f"SELECT {_COLS} FROM files WHERE id=?", (int(file_id),)).fetchone()
        b = cur.execute("SELECT * FROM foreign_burn WHERE file_id=?", (int(file_id),)).fetchone()
        n = cur.execute("SELECT COUNT(*) FROM foreign_burn WHERE epkey=? AND verdict='foreign'",
                        ((b["epkey"] if b else ""),)).fetchone()[0]
    if not f or not b or n > MAX_PER_EPISODE:
        return None
    cfg = next((c for c in SETTINGS.arrs if c.name == f["arr_name"]), None)
    if not cfg:
        return None

    def ask():
        kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
        rec = _call(cfg, f"/api/v3/{kind}/{int(f['arr_file_id'])}", timeout=30) or {}
        if cfg.kind == "sonarr":
            eps = _call(cfg, f"/api/v3/episode?episodeFileId={int(f['arr_file_id'])}", timeout=30) or []
            if not eps:
                return rec, []
            return rec, _call(cfg, f"/api/v3/release?episodeId={int(eps[0]['id'])}", timeout=240) or []
        return rec, _call(cfg, f"/api/v3/release?movieId={int(f['arr_parent_id'])}", timeout=240) or []
    try:
        rec, rels = await asyncio.to_thread(ask)
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"foreign subtitles burned in: could not ask {cfg.name} what else exists for "
                   f"{b['title']}: {type(e).__name__}", "debug")
        return None
    res_now = int((((rec.get("quality") or {}).get("quality") or {}).get("resolution")) or 0)
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", str(s or "").lower())    # noqa: E731
    mine = norm(b["scene"])
    bad = {k for k, (burnt, clean) in _pairs().items() if burnt and not clean}
    bad.add((b["source"] or "", b["grp"] or ""))
    best = None
    for x in rels:
        title = x.get("title") or ""
        if not title or norm(title) == mine or _pair(title) in bad:
            continue
        if any(not _SOFT.search(str(rj)) for rj in (x.get("rejections") or [])):
            continue
        res = int((((x.get("quality") or {}).get("quality") or {}).get("resolution")) or 0)
        if res_now and res < res_now:
            continue
        if x.get("protocol") == "torrent" and not int(x.get("seeders") or 0):
            continue
        if best is None or int(x.get("customFormatScore") or 0) > int(best.get("customFormatScore") or 0):
            best = x
    return {"title": best["title"]} if best else None


async def settle(file_id: int, why: str = "") -> dict | None:
    """The remedy's first question. None = something better exists, replace it the usual way.
    Otherwise the file is kept and its made subtitles go to the top of the picture."""
    b = await better(int(file_id))
    with cursor() as cur:
        row = cur.execute("SELECT title, script FROM foreign_burn WHERE file_id=?", (int(file_id),)).fetchone()
    label = row["title"] if row else f"file {file_id}"
    if b:
        joblog.log(f"foreign subtitles burned in: {label} - replacing, {b['title']} is there to download", "info")
        with cursor() as cur:
            cur.execute("UPDATE foreign_burn SET state='replacing', state_at=?, note=? WHERE file_id=?",
                        (time.time(), b["title"][:200], int(file_id)))
        return None
    moved = await asyncio.to_thread(place_top, int(file_id))
    note = "nothing better to download - kept; " + moved
    with cursor() as cur:
        cur.execute("UPDATE foreign_burn SET state='kept', state_at=?, note=? WHERE file_id=?",
                    (time.time(), note[:200], int(file_id)))
    joblog.log(f"foreign subtitles burned in: {label} - {note}", "info")
    return {"ok": True, "did": [note], "kept": True}


def place_top(file_id: int) -> str:
    """Move the subtitles nuarr made for this file to the top of the picture."""
    try:
        from . import submake
        r = submake.restyle_one(int(file_id), force=True)
    except Exception as e:                                       # noqa: BLE001
        return f"the subtitles could not be moved ({type(e).__name__}: {e})"[:140]
    if r.get("ok"):
        return "the subtitles nuarr made now sit at the top of the picture"
    return f"no subtitles of nuarr's to move ({r.get('why') or '?'}); ones made later go at the top"


# ------------------------------------------------------------ the panel ----
_LEFT = {"at": 0.0, "n": 0}


def stats() -> dict:
    from . import idle, integrity
    if not _READY:
        init()
    if time.time() - _LEFT["at"] > 60:
        _LEFT.update(n=len(_all_pending()), at=time.time())
    with cursor() as cur:
        n = {r[0]: r[1] for r in cur.execute("SELECT verdict, COUNT(*) FROM foreign_burn GROUP BY 1")}
        rows = [dict(r) for r in cur.execute(
            "SELECT b.*, f.state AS fstate, f.arr_file_id AS now_id, "
            "  (SELECT detail FROM remedy_log r WHERE r.file_id=b.file_id AND r.kind=? AND r.ok=0 "
            "    ORDER BY r.at DESC LIMIT 1) AS failed "
            "  FROM foreign_burn b LEFT JOIN files f ON f.id=b.file_id WHERE b.verdict='foreign' "
            " ORDER BY b.at DESC LIMIT 200", (KIND,))]
    out = []
    for r in rows:
        gone = r["fstate"] in (None, "deleted", "duplicate") or r["now_id"] != r["arr_file_id"]
        st = ("replaced" if gone else "ok" if r["kept"] else "kept" if r["state"] == "kept"
              else "replacing" if r["state"] == "replacing" else "open")
        out.append({"file_id": r["file_id"], "title": r["title"], "path": r["path"], "script": r["script"],
                    "hits": r["hits"], "samples": r["samples"], "sample": r["sample"], "at": r["at"],
                    "source": r["source"], "grp": r["grp"], "state": st, "note": r["note"] or "",
                    "failed": r["failed"] or "", "again": (r["state_at"] or 0) + RETRY_S if st == "kept" else 0})
    bans = [{"source": k[0], "grp": k[1], "burned": v[0]} for k, v in _pairs().items()
            if k[0] and v[0] >= BAN_AT and not v[1]]
    p = idle.progress(KEY)
    return {"on": enabled(), "mode": integrity.mode(), "read": sum(n.values()), "left": _LEFT["n"],
            "found": n.get("foreign", 0), "rows": out, "bans": bans,
            "run": {k: p.get(k) for k in ("running", "paused", "paused_why", "now", "last_error")}
            | {"flight": [f.get("now") for f in p.get("flight") or []]}}


def look_now() -> dict:
    from . import idle
    _LATER.clear()
    _PEND["at"] = 0.0
    _LEFT["at"] = 0.0
    idle.bump(KEY)
    return stats()


async def watch() -> None:
    from . import idle
    await asyncio.to_thread(init)
    await asyncio.sleep(260)
    await idle.run(KEY, TITLE, _pending, _do_one, label=lambda r: _label(r)[:120],
                   disk_of=lambda r: r.get("pool_disk") or "", lanes=1,
                   note_of=lambda r: "reading the picture for burned-in foreign subtitles",
                   system_name=TITLE, goto="/settings#janitor@fbBox")


def worker_row() -> dict:
    """This check as a lane in the Processing system: running, waiting, its switch."""
    from . import idle
    p = idle.progress(KEY)
    run = bool(p.get("running"))
    try:
        left = len(_all_pending()) if time.time() - _LEFT["at"] > 60 else _LEFT["n"]
    except Exception:                                            # noqa: BLE001
        left = 0
    return {"key": KEY, "pool": "burned", "label": TITLE, "running": 1 if run else 0, "cap": 1,
            "paused": not enabled(), "now": ("reading" if run else ""), "queued": int(left),
            "hint": "Reads a dozen frames of each file with no English audio for another language's subtitles painted into the picture. One file at a time, only while the machine is quiet. Its results are on Settings - Janitor."}
