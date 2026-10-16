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


def candidates(file_id: int) -> tuple[list, dict]:
    """Releases that would bring the picture back, best first, and the ids."""
    from .arrqueue import _call
    x = next((r for r in _load() if r["file_id"] == int(file_id)), None)
    f = _file(file_id)
    if not x or not f or not f.get("arr_name"):
        return [], {"why": "nuarr does not know which arr item this file is"}
    a = _arr(f["arr_name"])
    if not a:
        return [], {"why": f"{f['arr_name']} is not enabled"}
    if a.kind == "sonarr":
        eps = [e for e in (_call(a, f"/api/v3/episode?seriesId={int(f['arr_parent_id'])}") or [])
               if e.get("episodeFileId") == f.get("arr_file_id")]
        if not eps:
            return [], {"why": "Sonarr no longer lists an episode on this file"}
        ids = {"arr": a, "seriesId": int(f["arr_parent_id"]), "episodeIds": [e["id"] for e in eps]}
        rel = []
        for e in eps[:1]:
            rel += _call(a, f"/api/v3/release?episodeId={e['id']}", timeout=240) or []
    else:
        ids = {"arr": a, "movieId": int(f["arr_parent_id"])}
        rel = _call(a, f"/api/v3/release?movieId={int(f['arr_parent_id'])}", timeout=240) or []
    want_res = (_RES.search(x["old"]) or _RES.search(x["new"])).group(1).lower()
    # HDR STAYS HDR. The first fuller copy fetched for Lioness S03E03 was
    # Amazon's 2160p at 20 Mbps - and 8-bit SDR, which its name never said.
    # When the file now carries HDR or Dolby Vision, a replacement has to say
    # so in its name too.
    had_hdr = bool(_HDR.search(f.get("path") or ""))

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
        out.append(r)
    out.sort(key=lambda r: (r.get("protocol") != "usenet", -(r.get("customFormatScore") or 0), -(r.get("size") or 0)))
    return out, ids


def fetch(file_id: int) -> dict:
    """Grab the best fuller copy, and remember it so the janitor imports it."""
    from .arrqueue import _call
    from . import joblog
    cands, ids = candidates(file_id)
    if not cands:
        why = ids.get("why") or ("no release of the same resolution (and HDR, where the file has it) "
                                 f"carries at least {round(REFETCH_AT * 100)}% of the old file's picture")
        set_state(file_id, "none", why)
        return {"ok": False, "why": why}
    r = cands[0]
    a = ids.pop("arr")
    _call(a, "/api/v3/release", "POST", {"guid": r["guid"], "indexerId": r["indexerId"]})
    set_state(file_id, "grabbed", f"grabbed {r['title']} ({r['size'] / 1e9:.2f} GB, {r.get('indexer')})",
              release=r["title"], arr=a.name, ids=ids)
    joblog.log(f"bitrate cut: grabbed a fuller copy - {r['title']} ({r['size'] / 1e9:.2f} GB)", "info")
    return {"ok": True, "release": r["title"], "gb": round(r["size"] / 1e9, 2)}


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
    eq, share = out.get("new_eq"), float(x.get("share") or 0)
    if share < 0.25 or (eq is not None and plenty and eq < plenty * 0.5):
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
    cands, ids = candidates(file_id)
    if not cands:
        return {"ok": False, "why": ids.get("why") or "nothing of the same resolution (and HDR, where the "
                                                       "file has it) carries enough picture"}
    return {"ok": True, "n": len(cands), "top": [
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
