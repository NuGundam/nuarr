r"""nuarr - "upgrades" that cut the picture's bitrate, and fetching it back.

WHY. An arr ranks releases by custom-format score, and a score has no idea
how many bits a release spent on the picture. Lioness S03E03 went from a
7.42 GB 2160p copy (about 19 Mbps) to a 2.66 GB one (about 7 Mbps) on
2026-09-20, as an UPGRADE: the new name carried PMTP, Dolby Vision and Atmos,
and each of those scores. It looked worse, and an Xbox that Plex had been
transcoding the big episodes for was handed this one to direct play - and
choked on it.

WHAT COUNTS AS A CUT. Same resolution, the old file over 1 GB, and the new
one carrying less than half the old one's picture - with the codec allowed
for: HEVC needs about half the bits of H.264 for the same picture and AV1 a
little less again, so an x264 file replaced by an x265 one at half the size
is the profile's "Efficient" formats doing their job, not a cut. A Remux
replaced by an encode is left alone too: a remux is the disc's own size by
design, and the encode is what the profile asked for.

WHAT IS DONE ABOUT IT. Nothing by itself. A cut is listed (on the arrs page,
under the queue janitor) and said on the phone; "Find a fuller copy" asks the
arr for every release of that episode or film and grabs the best-scored one
that is the same resolution, at least four fifths of the old file's picture,
not banned, and not refused for anything but "not an upgrade". The arr will
call that download "not a Custom Format upgrade" when it lands - which is the
whole point - so the queue janitor imports it by hand instead of clearing it.
"""
from __future__ import annotations

import json
import re
import time

KV_FOUND = "bitcut.found"
KV_BACKFILL = "bitcut.backfilled"
RATIO = 0.5          # below this share of the old picture, an upgrade is a cut
REFETCH_AT = 0.8     # a replacement must carry at least this share of it
_RES = re.compile(r"(2160p|1080p|720p|576p|480p)", re.I)
_HDR = re.compile(r"HDR|DV|DoVi|Dolby.?Vision|HLG", re.I)


def _eff(desc: str) -> float:
    low = (desc or "").lower()
    if re.search(r"\bav1\b", low):
        return 0.5
    if re.search(r"\b(x265|h265|hevc)\b", low):
        return 0.65
    return 1.0


# ENOUGH IS ENOUGH. A cut from a very generous file to a merely good one is
# not worth a refetch: Spirited Away went 13.4 GB x265 -> 10.3 GB x264, which
# is about 11 Mbps of H.264 for a 1080p film. In H.264 terms (the codec
# allowance above), a file at or over these is left alone however much it lost.
PLENTY_MBPS = {"2160p": 15.0, "1080p": 5.0, "720p": 3.0, "576p": 1.5, "480p": 1.5}


def parse(detail: str):
    """'<old> · 7.42 GB  ->  <new> · 2.66 GB' -> (old, old_gb, new, new_gb)"""
    m = re.search(r"^(.*?)·\s*([\d.,]+)\s*GB\s*->\s*(.*?)·\s*([\d.,]+)\s*GB", detail or "")
    if not m:
        return None
    return (m.group(1).strip(), float(m.group(2).replace(",", "")),
            m.group(3).strip(), float(m.group(4).replace(",", "")))


def ratio(old: str, old_gb: float, new: str, new_gb: float, secs: float = 0.0):
    """The share of the old picture the new one carries, or None when the
    rule does not apply (different resolution, small file, remux source, or
    a new file that still has plenty)."""
    ra, rb = _RES.search(old or ""), _RES.search(new or "")
    if not (ra and rb) or ra.group(1).lower() != rb.group(1).lower() or old_gb < 1:
        return None
    if "remux" in (old or "").lower():
        return None
    if secs > 60:
        mbps = new_gb * 8000 / secs / _eff(new)
        if mbps >= PLENTY_MBPS.get(ra.group(1).lower(), 99):
            return None
    return (new_gb / _eff(new)) / (old_gb / _eff(old))


def _load() -> list:
    from .db import kv_get
    try:
        return json.loads(kv_get(KV_FOUND) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _save(rows: list) -> None:
    from .db import kv_set
    kv_set(KV_FOUND, json.dumps(rows[:400]))


def _file(file_id: int) -> dict:
    from .db import cursor
    with cursor() as cur:
        r = cur.execute("SELECT id, path, title, season, episode, library, arr_name, arr_file_id, "
                        "arr_parent_id, state, duration FROM files WHERE id=?", (int(file_id),)).fetchone()
    return dict(r) if r else {}


def note(file_id: int, detail: str, at: float | None = None, quiet: bool = False) -> dict | None:
    """One upgrade's detail line -> a finding, when it is a cut."""
    p = parse(detail)
    if not p:
        return None
    f = _file(file_id)
    r = ratio(*p, secs=float(f.get("duration") or 0))
    if r is None or r >= RATIO:
        return None
    rows = _load()
    if any(x["file_id"] == int(file_id) and x.get("detail") == detail for x in rows):
        return None
    from .db import display_label
    label = display_label(f.get("title"), f.get("season"), f.get("episode"), f.get("path") or "") if f else str(file_id)
    row = {"file_id": int(file_id), "at": at or time.time(), "label": label, "library": f.get("library") or "",
           "detail": detail, "old": p[0], "old_gb": p[1], "new": p[2], "new_gb": p[3],
           "share": round(r, 2), "state": "open", "note": ""}
    rows = [x for x in rows if x["file_id"] != int(file_id) or x.get("state") in ("fixed",)] + [row]
    rows.sort(key=lambda x: -(x.get("at") or 0))
    _save(rows)
    if not quiet:
        from . import joblog
        joblog.log(f"upgrade cut the bitrate: {label} - {p[1]:.2f} GB -> {p[3]:.2f} GB "
                   f"({round(r * 100)}% of the picture, codec allowed for)", "warn")
        try:
            import asyncio
            from . import pushover
            if pushover.enabled():
                asyncio.get_running_loop().create_task(pushover.send(
                    "nuarr · an upgrade cut the bitrate",
                    f"<b>{pushover._esc(label)}</b>\n{pushover._esc(p[0])} · {p[1]:.2f} GB\n→ "
                    f"{pushover._esc(p[2])} · {p[3]:.2f} GB\nabout {round(r * 100)}% of the picture "
                    f"- Find a fuller copy is on the arrs page"))
        except Exception:                                        # noqa: BLE001
            pass
    return row


def note_arr_file(arr_name: str, arr_file_id: int, detail: str) -> None:
    from .db import cursor
    with cursor() as cur:
        r = cur.execute("SELECT id FROM files WHERE arr_name=? AND arr_file_id=? ORDER BY id DESC LIMIT 1",
                        (arr_name, int(arr_file_id))).fetchone()
    if r:
        note(int(r["id"]), detail)


def backfill(force: bool = False) -> int:
    """Every upgrade on record, once: the latest one per file still here."""
    from .db import cursor, kv_get, kv_set
    if kv_get(KV_BACKFILL) and not force:
        return 0
    n = 0
    with cursor() as cur:
        rows = cur.execute(
            "SELECT h.file_id, h.detail, h.at FROM history h JOIN files f ON f.id = h.file_id "
            " WHERE h.event='upgraded' AND f.state NOT IN ('deleted','duplicate') "
            "   AND h.at = (SELECT MAX(at) FROM history WHERE file_id=h.file_id AND event='upgraded')"
        ).fetchall()
    for r in rows:
        if note(int(r["file_id"]), r["detail"] or "", at=float(r["at"] or 0), quiet=True):
            n += 1
    kv_set(KV_BACKFILL, str(time.time()))
    return n


def found(include_closed: bool = False) -> list:
    rows = _load()
    return rows if include_closed else [x for x in rows if x.get("state") not in ("kept", "fixed")]


def set_state(file_id: int, state: str, note_: str = "", **extra) -> None:
    rows = _load()
    for x in rows:
        if x["file_id"] == int(file_id) and x.get("state") not in ("fixed",):
            x.update(state=state, note=note_, state_at=time.time(), **extra)
    _save(rows)


# ------------------------------------------------------------- fetching --
def _arr(name: str):
    from .config import SETTINGS
    return next((a for a in SETTINGS.arrs if a.name == name and a.enabled), None)


def _ids(file_id: int):
    """(finding, file row, arr, ids) or a 'why' string."""
    from .arrqueue import _call
    x = next((r for r in _load() if r["file_id"] == int(file_id)), None)
    f = _file(file_id)
    if not x or not f or not f.get("arr_name"):
        return "nuarr does not know which arr item this file is"
    a = _arr(f["arr_name"])
    if not a:
        return f"{f['arr_name']} is not enabled"
    if a.kind == "sonarr":
        eps = [e for e in (_call(a, f"/api/v3/episode?seriesId={int(f['arr_parent_id'])}") or [])
               if e.get("episodeFileId") == f.get("arr_file_id")]
        if not eps:
            return "Sonarr no longer lists an episode on this file"
        ids = {"arr": a, "seriesId": int(f["arr_parent_id"]), "episodeIds": [e["id"] for e in eps]}
    else:
        ids = {"arr": a, "movieId": int(f["arr_parent_id"])}
    return x, f, a, ids


# WHAT IT IS DOING, FOR THE ROW. A release search is the arr asking every
# indexer at once and takes ten seconds to a minute; the row shows the step it
# is on and, from the searches before it, how long one usually takes.
PROG: dict = {}            # file_id -> {"what", "step", "started", "step_at"}
_SEARCH_S: list = []       # the last few release searches, in seconds


def _step(file_id, step: str, what: str = "") -> None:
    p = PROG.setdefault(int(file_id), {"what": what or "working", "started": time.time()})
    if what:
        p["what"] = what
    p.update(step=step, step_at=time.time())


def _done(file_id) -> None:
    PROG.pop(int(file_id), None)


def progress() -> dict:
    avg = round(sum(_SEARCH_S) / len(_SEARCH_S)) if _SEARCH_S else None
    now = time.time()
    return {"rows": {str(k): {**v, "elapsed": round(now - v["started"])} for k, v in PROG.items()},
            "search_s": avg}


def _releases(a, ids: dict, file_id: int = 0) -> list:
    from .arrqueue import _call
    if file_id:
        _step(file_id, f"asking every indexer through {a.name} for this "
                       f"{'episode' if a.kind == 'sonarr' else 'film'}'s releases")
        PROG[int(file_id)]["search_at"] = time.time()
    t0 = time.time()
    if a.kind == "sonarr":
        out = _call(a, f"/api/v3/release?episodeId={ids['episodeIds'][0]}", timeout=240) or []
    else:
        out = _call(a, f"/api/v3/release?movieId={ids['movieId']}", timeout=240) or []
    _SEARCH_S.append(time.time() - t0)
    del _SEARCH_S[:-8]
    if file_id:
        PROG[int(file_id)].pop("search_at", None)
        _step(file_id, f"weighing {len(out)} release(s) against the old file")
    return out


def candidates(file_id: int) -> tuple[list, dict]:
    """Releases that would bring the picture back, best first, and the ids."""
    got = _ids(file_id)
    if isinstance(got, str):
        return [], {"why": got}
    x, f, a, ids = got
    rel = _releases(a, ids, file_id)
    want_res = (_RES.search(x["old"]) or _RES.search(x["new"])).group(1).lower()
    # HDR STAYS HDR. The first fuller copy fetched for Lioness S03E03 was
    # Amazon's 2160p at 20 Mbps - and 8-bit SDR, which its name never said.
    # When the file now carries HDR or Dolby Vision, a replacement has to say
    # so in its name too.
    had_hdr = bool(_HDR.search(f.get("path") or ""))
    lc = _langs(x)
    # what the old file had, and English if the new one brought it - not every
    # extra the upgrade carried (Terminator 2's added a Spanish track)
    need_langs = set(lc.get("old") or []) | ({"English"} & set(lc.get("new") or []))

    need = (x["old_gb"] / _eff(x["old"])) * REFETCH_AT
    ok_words = ("existing file", "upgrade", "cutoff", "is already in the queue")
    out = []
    for r in rel:
        res = str(((r.get("quality") or {}).get("quality") or {}).get("resolution") or "")
        if not res or f"{res}p" != want_res:
            continue
        if any(str(c.get("name") or "").startswith("Nuarr:") for c in (r.get("customFormats") or [])):
            continue
        rej = [str(j) for j in (r.get("rejections") or [])]
        if any(not any(w in j.lower() for w in ok_words) for j in rej):
            continue
        gb = (r.get("size") or 0) / 1e9
        if gb / _eff(r.get("title") or "") < need:
            continue
        if r.get("protocol") == "torrent" and (r.get("seeders") or 0) < 5:
            continue
        if had_hdr and not _HDR.search(r.get("title") or ""):
            continue
        have = {str(l.get("name")) for l in (r.get("languages") or [])}
        if need_langs and not need_langs <= have:
            continue
        out.append(r)
    out.sort(key=lambda r: (r.get("protocol") != "usenet", -(r.get("customFormatScore") or 0), -(r.get("size") or 0)))
    return out, ids


def fetch(file_id: int) -> dict:
    """Grab the best fuller copy, and remember it so the janitor imports it."""
    _step(file_id, "finding the episode or film in the arr", "finding a fuller copy")
    try:
        return _fetch(file_id)
    finally:
        _done(file_id)


def _fetch(file_id: int) -> dict:
    from .arrqueue import _call
    from . import joblog
    cands, ids = candidates(file_id)
    if not cands:
        why = ids.get("why") or ("no release of the same resolution (and HDR, where the file has it, and "
                                 "the old file's audio languages, and English) carries at least "
                                 f"{round(REFETCH_AT * 100)}% of the old file's picture")
        set_state(file_id, "none", why)
        return {"ok": False, "why": why}
    r = cands[0]
    a = ids.pop("arr")
    _step(file_id, f"grabbing {r['title']}")
    _call(a, "/api/v3/release", "POST", {"guid": r["guid"], "indexerId": r["indexerId"]})
    set_state(file_id, "grabbed", f"grabbed {r['title']} ({r['size'] / 1e9:.2f} GB, {r.get('indexer')})",
              release=r["title"], arr=a.name, ids=ids, cut_release=_safe_now(file_id))
    joblog.log(f"bitrate cut: grabbed a fuller copy - {r['title']} ({r['size'] / 1e9:.2f} GB)", "info")
    return {"ok": True, "release": r["title"], "gb": round(r["size"] / 1e9, 2)}


# ------------------------------------------------- what the sound did --
# A SMALLER PICTURE CAN BE THE PRICE OF A LANGUAGE. An anime film going from
# a Japanese-only release to a dual-audio one gains English and often loses
# bits - that is the upgrade working, and the row says keep. And the other
# way: Pokemon the Movie: Secrets of the Jungle went from English + Japanese
# (EMBER) to English + Turkish (TURKO) - the "upgrade" dropped the original
# audio as well as half the picture. The arrs' history carries each event's
# languages: the upgrade's deletion has the old file's, its import the new
# file's. A fuller copy has to carry every language either of them had.
def langs_change(file_id: int) -> dict:
    import datetime as _dt
    x = next((r for r in _load() if r["file_id"] == int(file_id)), None)
    if x and x.get("ids") and x.get("arr") and _arr(x["arr"]):
        a, ids = _arr(x["arr"]), dict(x["ids"])
    else:
        got = _ids(file_id)
        if isinstance(got, str):
            return {}
        x, f, a, ids = got
    h = _hist(a, ids)

    def ts(e):
        try:
            return _dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp()
        except Exception:                                        # noqa: BLE001
            return 0.0
    dels = [e for e in h if e.get("eventType") in ("movieFileDeleted", "episodeFileDeleted")
            and str((e.get("data") or {}).get("reason") or "").lower() == "upgrade"]
    if not dels:
        return {}
    d = min(dels, key=lambda e: abs(ts(e) - float(x.get("at") or 0)))
    imps = [e for e in h if e.get("eventType") == "downloadFolderImported" and abs(ts(e) - ts(d)) < 600]
    if not imps:
        return {}
    imp = min(imps, key=lambda e: abs(ts(e) - ts(d)))

    def names(e):
        return [str(l.get("name")) for l in (e.get("languages") or [])
                if l.get("name") and str(l.get("name")).lower() not in ("unknown", "any")]
    old, new = names(d), names(imp)
    if not old or not new:
        return {"old": old, "new": new}
    return {"old": old, "new": new, "gained": [l for l in new if l not in old],
            "lost": [l for l in old if l not in new]}


def _langs(x: dict) -> dict:
    """The row's language change, looked up once and kept on the row."""
    if "langs" in x:
        return x["langs"] or {}
    try:
        ch = langs_change(x["file_id"])
    except Exception:                                            # noqa: BLE001
        return {}
    rows = _load()
    for r in rows:
        if r["file_id"] == x["file_id"]:
            r["langs"] = ch
    _save(rows)
    x["langs"] = ch
    return ch


# --------------------------------------------- and the cut copy stays out --
# BRINGING THE PICTURE BACK IS NOT ENOUGH ON ITS OWN. The release that cut it
# still outscores what replaced it - that is how it won the first time - so
# the arr's next search would "upgrade" straight back to it. Once the fuller
# copy is in, the cut release's NAME is banned the way nuarr bans a rejected
# release (arrban: a -999999 custom format on every indexer). Other releases
# of the same small cut can still come; one that does is listed here again.
def now_release(file_id: int) -> str:
    """The release name of the upgrade that cut the picture, from history."""
    import datetime as _dt
    x = next((r for r in _load() if r["file_id"] == int(file_id)), None)
    if x and x.get("ids") and x.get("arr") and _arr(x["arr"]):
        # after the import the file row and the arr's file id have moved on;
        # the ids kept at grab time still name the episode or film
        a, ids = _arr(x["arr"]), dict(x["ids"])
    else:
        got = _ids(file_id)
        if isinstance(got, str):
            return ""
        x, f, a, ids = got
    h = _hist(a, ids)

    def ts(e):
        try:
            return _dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp()
        except Exception:                                        # noqa: BLE001
            return 0.0
    dels = [e for e in h if e.get("eventType") in ("movieFileDeleted", "episodeFileDeleted")
            and str((e.get("data") or {}).get("reason") or "").lower() == "upgrade"]
    if not dels:
        return ""
    d = min(dels, key=lambda e: abs(ts(e) - float(x.get("at") or 0)))
    imps = [e for e in h if e.get("eventType") == "downloadFolderImported" and abs(ts(e) - ts(d)) < 600]
    return (min(imps, key=lambda e: abs(ts(e) - ts(d))).get("sourceTitle") or "") if imps else ""


def _safe_now(file_id: int) -> str:
    try:
        return now_release(file_id)
    except Exception:                                            # noqa: BLE001
        return ""


def _safe_ban(file_id: int) -> None:
    try:
        ban_cut(file_id)
    except Exception as e:                                       # noqa: BLE001
        from . import joblog
        joblog.log(f"bitrate cut: could not ban the cut release: {type(e).__name__}: {e}", "warn")


def ban_cut(file_id: int, title: str = "") -> str:
    """Ban the release that cut this file's picture. -> the name, or ''."""
    from . import arrban, joblog
    x = next((r for r in _load() if r["file_id"] == int(file_id)), None)
    title = title or (x or {}).get("cut_release") or now_release(file_id)
    if not title or not x:
        return ""
    a = _arr((x.get("arr") or _file(file_id).get("arr_name") or ""))
    arrban.add("release", title, f"an upgrade that cut {x['label']} to {round(x['share'] * 100)}% of "
                                 f"its picture - the fuller copy was brought back", "bitrate cut",
               by="nuarr", arrs=(a.kind if a else ""))
    arrban.kick(arrban.SYNC_HAND_S)
    rows = _load()
    for r in rows:
        if r["file_id"] == int(file_id):
            r["banned"] = title
    _save(rows)
    joblog.log(f"bitrate cut: {x['label']} - banned the release that cut it: {title}", "info")
    return title


# ------------------------------------------------------- the old one back --
# WHEN NOTHING FULLER IS OUT THERE, THE OLD FILE STILL WAS. The arr's history
# says what it was: the upgrade's deletion of it, and the import before that
# with the release name it came from. Demon City Shinjuku's was "[Bluray-1080p]
# [FLAC 2.0][x265]-Iznjie Biznjie" from NZBFinder, 13.5 GB, swapped on
# 2026-10-04 for a 1.2 GB x264 copy. Two ways back, in this order: the arr's
# own recycle bin (it keeps a replaced file for the days it is set to), and
# that same release, grabbed again by name.
def _hist(a, ids: dict) -> list:
    from .arrqueue import _call
    if a.kind == "sonarr":
        h = _call(a, f"/api/v3/history/series?seriesId={ids['seriesId']}") or []
        h = [e for e in h if e.get("episodeId") in ids["episodeIds"]]
    else:
        h = _call(a, f"/api/v3/history/movie?movieId={ids['movieId']}") or []
    return sorted(h, key=lambda e: e.get("date") or "", reverse=True)


def old_release(file_id: int) -> dict:
    """What the file was before the upgrade that cut it - from the arr's history."""
    got = _ids(file_id)
    if isinstance(got, str):
        return {"why": got}
    x, f, a, ids = got
    h = _hist(a, ids)
    dels = [e for e in h if e.get("eventType") in ("movieFileDeleted", "episodeFileDeleted")
            and str((e.get("data") or {}).get("reason") or "").lower() == "upgrade"]
    if not dels:
        return {"why": "the arr's history has no upgrade deletion for this file"}
    # the deletion nearest the finding (its time is the upgrade's)
    import datetime as _dt

    def ts(e):
        try:
            return _dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp()
        except Exception:                                        # noqa: BLE001
            return 0.0
    d = min(dels, key=lambda e: abs(ts(e) - float(x.get("at") or 0)))
    imp = next((e for e in h if e.get("eventType") == "downloadFolderImported" and ts(e) < ts(d) - 1), None)
    if not imp:
        return {"why": "the arr's history does not say which release the old file came from",
                "deleted": d.get("sourceTitle")}
    grab = next((e for e in h if e.get("eventType") == "grabbed" and ts(e) <= ts(imp)
                 and _key(e.get("sourceTitle")) == _key(imp.get("sourceTitle"))), None)
    return {"title": imp.get("sourceTitle") or "", "deleted": d.get("sourceTitle") or "",
            "deleted_at": d.get("date"), "gb": round(int((d.get("data") or {}).get("size") or 0) / 1e9, 2),
            "indexer": ((grab or {}).get("data") or {}).get("indexer") or "", "arr": a.name}


def _in_recycle(a, deleted: str) -> str:
    """The replaced file, if the arr's recycle bin still has it."""
    import os
    from .arrqueue import _call
    try:
        rb = (_call(a, "/api/v3/config/mediamanagement") or {}).get("recycleBin") or ""
    except Exception:                                            # noqa: BLE001
        return ""
    if not rb or not deleted:
        return ""
    name = os.path.basename(deleted)
    parts = os.path.normpath(deleted).split(os.sep)
    # the arr keeps the folder: <bin>\<show or film folder>[\<season>]\<file>
    for k in (3, 2):
        cand = os.path.join(rb, *parts[-k:])
        if os.path.isfile(cand):
            return cand
    return ""


def restore(file_id: int) -> dict:
    """Bring the file the upgrade replaced back: from the recycle bin, or by
    grabbing its release again. The janitor imports a grab as for a fuller copy."""
    _step(file_id, "reading the arr's history for the file the upgrade replaced", "getting the old one back")
    try:
        return _restore(file_id)
    finally:
        _done(file_id)


def _restore(file_id: int) -> dict:
    import os
    from .arrqueue import _call
    from . import joblog
    got = _ids(file_id)
    if isinstance(got, str):
        return {"ok": False, "why": got}
    x, f, a, ids = got
    old = old_release(file_id)
    if not old.get("title"):
        set_state(file_id, "none", old.get("why") or "the old release is not known")
        return {"ok": False, "why": old.get("why")}
    # 1. the recycle bin
    _step(file_id, f"looking in {a.name}'s recycle bin for {old['title'][:60]}")
    src = _in_recycle(a, old.get("deleted"))
    if src:
        _step(file_id, "found it in the recycle bin - putting it back and importing it")
        dst = os.path.join(os.path.dirname(f["path"]), os.path.basename(src))
        os.replace(src, dst)
        q = __import__("urllib.parse").parse.urlencode(
            {"folder": os.path.dirname(dst), "filterExistingFiles": "false",
             **({"seriesId": ids["seriesId"]} if "seriesId" in ids else {"movieId": ids["movieId"]})})
        c = next((c for c in (_call(a, "/api/v3/manualimport?" + q) or [])
                  if os.path.normcase(c.get("path") or "") == os.path.normcase(dst)), None)
        if not c:
            return {"ok": False, "why": f"put {os.path.basename(dst)} back beside the file, but the arr does "
                                        f"not offer it for import - import it in the arr by hand"}
        fl = {"path": c["path"], "quality": c.get("quality"), "languages": c.get("languages") or [],
              "releaseGroup": c.get("releaseGroup") or ""}
        fl.update({k: ids[k] for k in ("seriesId", "episodeIds", "movieId") if k in ids})
        _call(a, "/api/v3/command", "POST", {"name": "ManualImport", "files": [fl], "importMode": "move"})
        cut = _safe_now(file_id)
        set_state(file_id, "fixed", f"the old file came back from {a.name}'s recycle bin: {os.path.basename(dst)}",
                  arr=a.name, cut_release=cut)
        _safe_ban(file_id)
        joblog.log(f"bitrate cut: {x['label']} - the old file restored from the recycle bin", "ok")
        return {"ok": True, "how": "recycle bin", "file": os.path.basename(dst)}
    # 2. the same release, again
    _step(file_id, "not in the recycle bin - looking for the same release on the indexers")
    rel = _releases(a, ids, file_id)
    k = _key(old["title"])
    hits = [r for r in rel if _key(r.get("title")) == k
            and not any(str(c.get("name") or "").startswith("Nuarr:") for c in (r.get("customFormats") or []))]
    hits.sort(key=lambda r: (r.get("protocol") != "usenet", -(r.get("seeders") or 0)))
    if not hits:
        why = (f"the old release ({old['title']}) is not in the recycle bin and no indexer offers it now")
        set_state(file_id, "none", why)
        return {"ok": False, "why": why}
    r = hits[0]
    _step(file_id, f"grabbing {r['title']} again")
    _call(a, "/api/v3/release", "POST", {"guid": r["guid"], "indexerId": r["indexerId"]})
    ids.pop("arr", None)
    set_state(file_id, "grabbed", f"grabbing the old release again: {r['title']} ({r['size'] / 1e9:.2f} GB, "
                                  f"{r.get('indexer')})", release=r["title"], arr=a.name, ids=ids,
              cut_release=_safe_now(file_id))
    joblog.log(f"bitrate cut: {x['label']} - grabbing the old release again: {r['title']}", "info")
    return {"ok": True, "how": "grabbed", "release": r["title"]}


def wanted() -> dict:
    """{release title (lower): finding} - downloads fetched to undo a cut."""
    return {_key(x.get("release")): x for x in _load()
            if x.get("state") == "grabbed" and x.get("release")}


def _key(title) -> str:
    """Indexers and queues spell one name with dots or spaces - compare the
    letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", str(title or "").lower())


def import_wanted(queues: list) -> tuple[list, set]:
    """Called by the queue janitor before it clears anything: a fetched copy
    that finished is imported by hand. -> (rows for its recent list, queue
    ids it must leave alone)."""
    import os
    import urllib.parse
    from .arrqueue import _call, STATS
    w = wanted()
    if not w:
        return [], set()
    rows, hold = [], set()
    for a, qrows in queues:
        for q in qrows:
            x = w.get(_key(q.get("title")))
            if not x or x.get("arr") != a.name:
                continue
            hold.add(q.get("id"))
            if (q.get("status") or "") != "completed" or (q.get("sizeleft") or 0) > 0:
                continue
            state = str(q.get("trackedDownloadState") or "").lower()
            if state not in ("importblocked", "importpending", "importfailed") and \
                    str(q.get("trackedDownloadStatus") or "").lower() != "warning":
                continue
            ids = x.get("ids") or {}
            try:
                # BY THE DOWNLOAD ALONE. Given a seriesId as well, Sonarr lists
                # the SERIES folder - 24 Lioness episodes already in place -
                # and never looks in the download.
                qs = urllib.parse.urlencode({"downloadId": q.get("downloadId") or "",
                                             "filterExistingFiles": "false"})
                cands = [c for c in (_call(a, "/api/v3/manualimport?" + qs) or [])
                         if os.path.splitext(c.get("path") or "")[1].lower() in (".mkv", ".mp4", ".m4v", ".ts")]
                if len(cands) != 1:
                    STATS["detail"].append(f"{a.name}: fuller copy {x['release'][:50]} - {len(cands)} video files "
                                           f"offered, left for you")
                    continue
                c = cands[0]
                f = {"path": c["path"], "quality": c.get("quality"), "languages": c.get("languages") or [],
                     "releaseGroup": c.get("releaseGroup") or "", "downloadId": q.get("downloadId") or ""}
                f.update({k: ids[k] for k in ("seriesId", "episodeIds", "movieId") if k in ids})
                _call(a, "/api/v3/command", "POST", {"name": "ManualImport", "files": [f], "importMode": "auto"})
                set_state(x["file_id"], "fixed", f"imported {x['release']} by hand - the arr called it "
                                                 f"not an upgrade, which is why nuarr fetched it")
                _safe_ban(x["file_id"])
                rows.append({"at": time.time(), "arr": a.name, "title": q.get("title") or "",
                             "client": q.get("downloadClient") or "", "kind": "parsed",
                             "why": "a fuller copy nuarr fetched after an upgrade cut the bitrate; "
                                    "imported by hand over the arr's 'not an upgrade'"})
                STATS["detail"].append(f"{a.name}: imported the fuller copy {x['release'][:60]}")
            except Exception as e:                               # noqa: BLE001
                STATS["detail"].append(f"{a.name}: could not import {x['release'][:50]}: {type(e).__name__}")
    return rows, hold


_AUD = re.compile(r"(\d\.\d)")


def explain(x: dict) -> dict:
    """What the % means for this row, in numbers, and what to do about it."""
    f = _file(x["file_id"])
    secs = float(f.get("duration") or 0)
    res = (_RES.search(x.get("old") or "") or _RES.search(x.get("new") or ""))
    res = res.group(1).lower() if res else ""
    plenty = PLENTY_MBPS.get(res, 0)
    out = {"secs": secs, "res": res, "plenty": plenty,
           "old_codec_f": _eff(x.get("old")), "new_codec_f": _eff(x.get("new"))}
    if secs > 60:
        for k in ("old", "new"):
            mbps = x[f"{k}_gb"] * 8000 / secs
            out[f"{k}_mbps"] = round(mbps, 1)
            out[f"{k}_eq"] = round(mbps / _eff(x.get(k)), 1)
    notes = []
    # what else the "upgrade" changed, beside the picture
    ao, an = _AUD.search(x.get("old") or ""), _AUD.search(x.get("new") or "")
    if ao and an and float(an.group(1)) < float(ao.group(1)):
        notes.append(f"the sound went from {ao.group(1)} to {an.group(1)} channels too")
    so, sn = (x.get("old") or "").split("-")[0].lower(), (x.get("new") or "").split("-")[0].lower()
    if so.startswith("bluray") and sn.startswith(("webdl", "webrip")):
        notes.append("the source went from Blu-ray to a web copy")
    if "x264" in (x.get("new") or "").lower() and re.search(r"x265|h265", x.get("old") or "", re.I):
        notes.append("the codec went from HEVC to H.264, which needs more bits for the same picture")
    lc = _langs(x)
    out["langs"] = lc
    if lc.get("lost"):
        notes.append("the upgrade dropped " + " and ".join(lc["lost"]) + " audio")
    eq, share = out.get("new_eq"), float(x.get("share") or 0)
    if "English" in (lc.get("gained") or []):
        rec, why = "keep", ("the upgrade brought English audio - the smaller picture was the price of it; "
                            "a fuller copy would have to be dual-audio too")
    elif lc.get("lost"):
        rec, why = "fetch", "it lost " + " and ".join(lc["lost"]) + " audio as well as picture"
    elif share < 0.25 or (eq is not None and plenty and eq < plenty * 0.5):
        rec, why = "fetch", ("well under what this resolution needs" if eq is not None and plenty
                             and eq < plenty * 0.5 else "most of the picture went")
    elif share < 0.4:
        rec, why = "fetch", "a clear loss, worth a fuller copy if one exists"
    else:
        rec, why = "maybe", "borderline - a fuller copy is better, but this one is watchable"
    out.update(rec=rec, rec_why=why, notes=notes)
    return out


def preview(file_id: int) -> dict:
    """What Find a fuller copy WOULD grab - the top few - without grabbing."""
    _step(file_id, "finding the episode or film in the arr", "checking what is available")
    try:
        return _preview(file_id)
    finally:
        _done(file_id)


def _preview(file_id: int) -> dict:
    cands, ids = candidates(file_id)
    _step(file_id, "reading the arr's history for the file the upgrade replaced")
    try:
        old = old_release(file_id)
    except Exception:                                            # noqa: BLE001
        old = {}
    if not cands:
        return {"ok": False, "old": old, "why": ids.get("why") or "nothing of the same resolution (and HDR, "
                                                                  "where the file has it) carries enough picture"}
    return {"ok": True, "old": old, "n": len(cands), "top": [
        {"title": r.get("title"), "gb": round((r.get("size") or 0) / 1e9, 2), "score": r.get("customFormatScore"),
         "indexer": r.get("indexer"), "protocol": r.get("protocol"), "seeders": r.get("seeders")}
        for r in cands[:4]]}


def snapshot() -> dict:
    rows = _load()
    for x in rows:
        try:
            x["why"] = explain(x)
        except Exception:                                        # noqa: BLE001
            x["why"] = {}
    return {"rows": rows, "open": sum(1 for x in rows if x.get("state") == "open"),
            "rule": {"ratio": RATIO, "refetch_at": REFETCH_AT}}
