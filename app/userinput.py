r"""nuarr - everything that is waiting on a person, for the Attention tile.

WHY ONE PLACE. Questions nuarr will not answer for you are spread over the
settings pages - Subtitle User Input and Raws on Subtitles, Audio User Input
on Audio language, the bitrate cuts and the ban list on Arrs - and each panel
said "waiting on you" only to somebody already looking at it. The dashboard's
Attention tile counted file errors, rule-check findings and arr warnings, and
said "nothing needs attention" while six subtitle questions sat unanswered.

Each source here is counted the way its own panel counts it, so the tile and
the panel agree, and carries a link of the form /settings#<page>@<element>:
the settings page opens that page and scrolls the panel into view.

Only things a PERSON has to do are counted. A list auto is already working
through is not attention - telling somebody about work in hand is how a tile
stops being believed (see arrsync.attention for the same rule).
"""
from __future__ import annotations

import threading
import time

_CACHE: dict = {"at": 0.0, "rows": None}
_LOCK = threading.Lock()
TTL_S = 60.0     # findings() is ~1 s; the panels poll it themselves, so once a minute here


def _subtitle_input() -> dict | None:
    """Subtitle User Input: its table rows plus the queue's questions - the
    same sum its own header shows (skPaint: c.actionable + skAsksShown())."""
    from . import subkind, subqueue
    f = subkind.findings()
    rows = f.get("rows") or []
    need = int((f.get("counts") or {}).get("actionable") or 0)
    shown = {int(r.get("file_id") or 0) for r in rows}
    asks = 0
    sample = [r for r in rows if r.get("action") and not r.get("done") and not r.get("unread")
              and not (f.get("mode") == "auto" and r.get("auto") == "act")]
    for q in subqueue.asking(500):
        for a in q.get("asks") or []:
            if a.get("q") not in ("picture", "title") or int(q.get("file_id") or 0) not in shown:
                asks += 1
    n = need + asks
    if not n:
        return None
    return {"what": "subtitle questions", "n": n,
            "note": f"{n} waiting in Subtitle User Input",
            "goto": "/settings#lang@skPanel",
            "items": [{"title": r.get("label") or "", "detail": (f"{r.get('sure')}% sure - "
                       if r.get("sure") is not None else "") + (r.get("why") or "")[:140]}
                      for r in sample[:6]]}


def _raws() -> dict | None:
    """Raws - nothing here you can read: files with no track in a language
    the library requires, waiting on Blocklist & re-download or Keep."""
    from . import subneed
    n = int((subneed.counts() or {}).get(subneed.MISSING) or 0)
    if not n:
        return None
    items = []
    try:
        for r in (subneed.missing(6, "") or [])[:6]:
            items.append({"title": r.get("label") or r.get("title") or "", "detail": (r.get("why") or "")[:140]})
    except Exception:                                            # noqa: BLE001
        pass
    return {"what": "raws", "n": n, "note": f"{n} with nothing you can read",
            "goto": "/settings#lang@subsRow-language", "items": items}


def _audio_questions() -> dict | None:
    """Audio User Input: questions the audio queue will not answer itself."""
    from . import audqueue
    asking = audqueue.asking(500) or []
    n = sum(len(q.get("asks") or []) for q in asking)
    if not n:
        return None
    return {"what": "audio questions", "n": n, "note": f"{n} waiting in Audio User Input",
            "goto": "/settings#alang@alAskPanel",
            "items": [{"title": q.get("label") or q.get("name") or "",
                       "detail": "; ".join(str(a.get("why") or a.get("q") or "") for a in (q.get("asks") or []))[:140]}
                      for q in asking[:6]]}


def _rules_gap() -> dict | None:
    """Files the subtitle rules changed for, in MANUAL mode only - auto drains them."""
    from . import subocr
    if subocr.gap_mode() != "manual":
        return None
    n = int((subocr.rules_gap(force=False) or {}).get("total") or 0)
    if not n:
        return None
    return {"what": "subtitle rules changed", "n": n, "note": f"{n} files waiting on Requeue",
            "goto": "/settings#lang@gapHost", "items": []}


def _bitrate_cuts() -> dict | None:
    """Upgrades that cut the bitrate: every open row in manual; in auto, only
    the ones auto leaves alone (your call, keep, nothing fuller found)."""
    from . import bitcut
    auto = bitcut.mode() == "auto"
    rows = []
    for x in bitcut.found():
        if x.get("state") not in ("open", "none"):
            continue
        try:
            rec = bitcut.explain(x).get("rec")
        except Exception:                                        # noqa: BLE001
            rec = ""
        if auto and x.get("state") == "open" and rec == "fetch" and not x.get("auto_tried"):
            continue                       # auto will fetch it on its next sweep
        rows.append((x, rec))
    if not rows:
        return None
    return {"what": "bitrate cuts", "n": len(rows), "note": f"{len(rows)} upgrades cut the picture",
            "goto": "/settings#arrs@bcBox",
            "items": [{"title": x.get("label") or "",
                       "detail": f"kept {round((x.get('share') or 0) * 100)}% of the picture - "
                                 + ("nothing fuller found" if x.get("state") == "none"
                                    else "advice: " + ("fetch" if rec == "fetch" else "keep" if rec == "keep" else "your call"))}
                      for x, rec in rows[:6]]}


def _ban_collisions() -> dict | None:
    """Banned terms that also match a show title and are not group names -
    nuarr cannot move them by itself (arrban.fix_collisions)."""
    from . import arrban
    left = arrban.COLLISIONS.get("left") or []
    if not left:
        return None
    return {"what": "banned terms", "n": len(left), "note": "a banned term also matches a show title",
            "goto": "/settings#arrs@job-release_ban",
            "items": [{"title": f"{c.get('value')}", "detail": f"matches the title \"{c.get('title')}\""}
                      for c in left[:6]]}


def _tvdb_left() -> dict | None:
    """Series TheTVDB re-added that the repair could not match by itself."""
    from . import tvdbfix
    snap = tvdbfix.snapshot()
    last = float((snap.get("stats") or {}).get("last_run") or 0)
    left = [r for r in (snap.get("recent") or [])
            if not r.get("ok") and float(r.get("at") or 0) >= last - 600]
    if not left:
        return None
    return {"what": "tvdb re-ids", "n": len(left), "note": "series TheTVDB re-added, left for you",
            "goto": "/settings#arrs@job-tvdb_fix",
            "items": [{"title": r.get("title") or "", "detail": (r.get("why") or "")[:140]} for r in left[:6]]}


def _download_leftovers() -> dict | None:
    """Folders left in the completed-download folder with video still in them."""
    from . import dlleft
    rows = dlleft.STATE.get("listed") or []
    if not rows:
        return None
    return {"what": "download leftovers", "n": len(rows),
            "note": f"{len(rows)} left in the download folder with video in them",
            "goto": "/settings#arrs@dlBox",
            "items": [{"title": r["name"], "detail": f"{r['n_videos']} video(s), "
                       f"{round(r['size'] / 1e9, 1)} GB, {round(r['age_h'])} h old"} for r in rows[:6]]}


SOURCES = (_subtitle_input, _raws, _audio_questions, _rules_gap, _bitrate_cuts,
           _ban_collisions, _tvdb_left, _download_leftovers)


def _build() -> list:
    out = []
    for fn in SOURCES:
        try:
            r = fn()
        except Exception:                                        # noqa: BLE001
            r = None
        if r:
            out.append(r)
    _CACHE.update(at=time.time(), rows=out)
    return out


def _rebuild() -> None:
    if not _LOCK.acquire(blocking=False):
        return
    try:
        _build()
    finally:
        _LOCK.release()


def rows() -> list:
    """[{what, n, note, goto, items}] - every source waiting on a person.

    NEVER WAITED FOR. The Subtitle User Input count is the panel's own
    findings() - 0.25 s warm, 3.3 s cold - and this rides the dashboard's
    summary poll on every page. The last answer is returned and a thread
    builds the next one; before the first build there is nothing to say yet.
    """
    if _CACHE["rows"] is None or time.time() - _CACHE["at"] >= TTL_S:
        threading.Thread(target=_rebuild, daemon=True, name="userinput").start()
    return list(_CACHE["rows"] or [])
