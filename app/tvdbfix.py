r"""TVDB re-ID repair: a series TheTVDB deleted and re-added under a new id.

WHY. Erik, 2026-10-13: Sonarr's health page said three series "were removed
from TheTVDB". They had not gone anywhere - TheTVDB had deleted each entry
and created it again under a new id (468809 -> 483427 and so on). Sonarr
cannot follow that by itself: it keeps the dead id, stops getting new
episodes and metadata, and shows a red health line until someone deletes
the series and adds it back.

WHAT IT DOES. Every few hours, Sonarr's series list is read for status
"deleted" (that is what the health check is built from). For each, Sonarr's
own lookup is asked for the title. A replacement is taken ONLY when exactly
one result has the same title (punctuation and case aside), the same year,
a different id, and is not already in Sonarr. Then, the way it was done by
hand:

  * the series is removed from Sonarr WITH ITS FILES KEPT,
  * its folder is renamed to carry the new {tvdb-...} id,
  * it is added back with the same profile, tags, type and monitoring, no
    search, and Sonarr's rescan re-imports the files already there.

Anything else - no match, two matches, a different year - is listed for a
person and nothing is touched. Sonarr's history for a repaired series is
lost; the files and their episodes are not.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse

from . import joblog
from .db import kv_get, kv_set

POLL_S = 6 * 3600
KV_RECENT = "tvdbfix.recent"
KV_LAST = "tvdbfix.last"
RECENT_KEEP = 30

STATS: dict = {"running": False, "last_run": 0.0, "next_run": 0.0,
               "last_result": "", "detail": [], "fixed": 0}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(t or "").lower())


def _recent() -> list:
    try:
        return json.loads(kv_get(KV_RECENT) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _remember(rows: list) -> None:
    if rows:
        kv_set(KV_RECENT, json.dumps((rows + _recent())[:RECENT_KEEP]))


def _candidate(a, s: dict, have: set) -> tuple[dict | None, str]:
    from .arrqueue import _call
    res = _call(a, "/api/v3/series/lookup?term=" + urllib.parse.quote(s["title"])) or []
    hits = [r for r in res
            if _norm(r.get("title")) == _norm(s["title"])
            and int(r.get("year") or 0) == int(s.get("year") or 0)
            and r.get("tvdbId") and r["tvdbId"] != s.get("tvdbId")
            and r["tvdbId"] not in have]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, "no entry on TheTVDB with the same title and year"
    return None, f"{len(hits)} entries on TheTVDB with the same title and year - which one is yours to pick"


def _repair(a, s: dict, new: dict) -> dict:
    from .arrqueue import _call
    old_path = s["path"]
    tag = f"{{tvdb-{s['tvdbId']}}}"
    new_path = (old_path.replace(tag, f"{{tvdb-{new['tvdbId']}}}")
                if tag in old_path else old_path)
    _call(a, f"/api/v3/series/{s['id']}?deleteFiles=false&addImportListExclusion=false", "DELETE")
    if new_path != old_path and os.path.isdir(old_path) and not os.path.exists(new_path):
        os.rename(old_path, new_path)
    body = dict(new)
    body.pop("id", None)
    body.update(qualityProfileId=s["qualityProfileId"],
                languageProfileId=s.get("languageProfileId") or 1,
                tags=s.get("tags") or [], seriesType=s.get("seriesType") or "standard",
                seasonFolder=bool(s.get("seasonFolder", True)),
                monitored=bool(s.get("monitored", True)),
                monitorNewItems=s.get("monitorNewItems") or "all",
                rootFolderPath=s.get("rootFolderPath") or os.path.dirname(old_path),
                path=new_path,
                addOptions={"monitor": "all", "searchForMissingEpisodes": False,
                            "searchForCutoffUnmetEpisodes": False})
    r = _call(a, "/api/v3/series", "POST", body)
    return {"new_id": r.get("id"), "path": new_path}


def _pass() -> str:
    from .arrqueue import _call
    from .config import SETTINGS
    STATS["detail"] = []
    fixed, left, rows = 0, 0, []
    for a in [x for x in SETTINGS.arrs if x.enabled and x.kind == "sonarr"]:
        try:
            series = _call(a, "/api/v3/series") or []
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not read series: {type(e).__name__}: {e}"[:160])
            continue
        have = {x.get("tvdbId") for x in series}
        for s in [x for x in series if str(x.get("status") or "").lower() == "deleted"]:
            st = s.get("statistics") or {}
            try:
                new, why = _candidate(a, s, have)
                if not new:
                    left += 1
                    STATS["detail"].append(f"{a.name}: {s['title']} (tvdb {s.get('tvdbId')}): {why}")
                    rows.append({"at": time.time(), "arr": a.name, "title": s["title"],
                                 "old": s.get("tvdbId"), "new": None, "ok": False, "why": why})
                    continue
                r = _repair(a, s, new)
                have.add(new["tvdbId"])
                fixed += 1
                msg = (f"{s['title']}: tvdb {s.get('tvdbId')} -> {new['tvdbId']}, "
                       f"{st.get('episodeFileCount') or 0} file(s) kept")
                STATS["detail"].append(f"{a.name}: {msg}")
                rows.append({"at": time.time(), "arr": a.name, "title": s["title"],
                             "old": s.get("tvdbId"), "new": new["tvdbId"], "ok": True,
                             "why": f"re-linked, {st.get('episodeFileCount') or 0} file(s) kept, folder now {os.path.basename(r['path'])}"})
            except Exception as e:                               # noqa: BLE001
                left += 1
                STATS["detail"].append(f"{a.name}: {s['title']}: {type(e).__name__}: {e}"[:160])
                rows.append({"at": time.time(), "arr": a.name, "title": s["title"],
                             "old": s.get("tvdbId"), "new": None, "ok": False,
                             "why": f"failed: {type(e).__name__}: {e}"[:200]})
    _remember(rows)
    STATS["fixed"] += fixed
    bits = []
    if fixed:
        bits.append(f"{fixed} series re-linked to their new TVDB id")
    if left:
        bits.append(f"{left} removed series left for you")
    return "; ".join(bits) or "no series removed from TheTVDB"


async def run() -> str:
    STATS["running"] = True
    try:
        res = await asyncio.to_thread(_pass)
    finally:
        STATS["running"] = False
    STATS["last_run"] = time.time()
    STATS["last_result"] = res
    try:
        kv_set(KV_LAST, json.dumps({"at": STATS["last_run"], "result": res}))
    except Exception:                                            # noqa: BLE001
        pass
    if res != "no series removed from TheTVDB":
        joblog.log("tvdb re-id repair: " + res, "ok")
        for ln in STATS["detail"]:
            joblog.log("  " + ln, "debug")
        try:
            from . import pushover
            if pushover.enabled() and STATS["detail"]:
                await pushover.send("nuarr · TVDB re-ID repair",
                                    "<b>" + pushover._esc(res) + "</b>\n"
                                    + "\n".join(pushover._esc(x) for x in STATS["detail"][:8]))
        except Exception:                                        # noqa: BLE001
            pass
    return res


async def watch() -> None:
    from .gate import get_toggle
    try:
        last = json.loads(kv_get(KV_LAST) or "{}")
        STATS["last_run"] = float(last.get("at") or 0.0)
        STATS["last_result"] = str(last.get("result") or "")
    except Exception:                                            # noqa: BLE001
        pass
    STATS["next_run"] = time.time() + 600
    await asyncio.sleep(600)
    while True:
        try:
            if get_toggle("arrs.tvdb_fix"):
                await run()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"tvdb re-id repair: {type(e).__name__}: {e}", "error")
        STATS["next_run"] = time.time() + POLL_S
        await asyncio.sleep(POLL_S)


def snapshot() -> dict:
    return {"stats": dict(STATS), "recent": _recent(), "poll_s": POLL_S}
