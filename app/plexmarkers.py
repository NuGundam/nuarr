"""Does anything fall through Plex's intro and credits detection?

Read straight from Plex's own library database, read-only, once a day. Per
library: how many episodes or films there are, on how many intro detection
has RUN (a record of the attempt is on the file's part, empty when nothing
was found), how many have an intro marker, how many have a credits marker -
and the files Plex has never analysed at all although they have been in the
library for days, by name.

WHY THE DATABASE and not Plex's web API: the API answers this one item at a
time, and there are forty thousand. The database answers it in two queries.
It is opened read-only (mode=ro); nothing is ever written to it.

WHAT THE FIGURES MEAN
  intro tried    the part carries the "pv:intros" field. Plex writes it when
                 detection has run, empty if no intro was found. This is a
                 reading of an undocumented field (2026-11-27: present on
                 38,854 of 38,870 episodes), so it is shown as what it is.
  never tried    no such field, and the item is older than three days - a new
                 file is given time before it is called missed.
  credits        a credits marker exists (online or detected locally). With
                 Marker source on "only online" an episode the online
                 database lacks never gets one; on "both" Plex detects it
                 itself in its maintenance window, so the missing count
                 should fall night by night. The trail below shows whether
                 it does.

A day's figures are kept (120 days) so the trend can be seen.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time

from .db import kv_get, kv_set

KV = "plexmarkers.last"
KV_TRAIL = "plexmarkers.trail"
EVERY_S = 24 * 3600
GRACE_S = 3 * 86400
STATE: dict = {"running": False, "error": ""}
_REL = os.path.join("Plex Media Server", "Plug-in Support", "Databases", "com.plexapp.plugins.library.db")


def db_path() -> str:
    """Plex's library database: where Plex says its data is, else the usual place."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as k:
            base = winreg.QueryValueEx(k, "LocalAppDataPath")[0]
            p = os.path.join(base, _REL)
            if os.path.exists(p):
                return p
    except Exception:                                            # noqa: BLE001
        pass
    for base in (os.environ.get("LOCALAPPDATA") or "", os.path.expanduser(r"~\AppData\Local")):
        p = os.path.join(base, _REL)
        if base and os.path.exists(p):
            return p
    return ""


def marker_source() -> str:
    """Plex's Marker source setting: any (both), cloud (only online), local."""
    try:
        from . import plexnotify
        d = json.loads(plexnotify._get("/:/prefs", timeout=15))
        for s in d["MediaContainer"]["Setting"]:
            if s.get("id") == "MarkerSource":
                return str(s.get("value") or "")
    except Exception:                                            # noqa: BLE001
        pass
    return ""


def scan() -> dict:
    p = db_path()
    if not p:
        return {"ok": False, "why": "Plex's library database was not found on this machine"}
    con = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True, timeout=15)
    try:
        q = lambda s, *a: con.execute(s, a).fetchall()           # noqa: E731
        now = time.time()
        marked: dict = {"intro": set(), "credits": set()}
        for mid, kind in q("SELECT t.metadata_item_id, t.text FROM taggings t JOIN tags g ON g.id = t.tag_id "
                           "WHERE g.tag_type = 12 AND t.text IN ('intro','credits')"):
            marked[kind].add(mid)
        libs, missed = [], []
        for sid, name, st in q("SELECT id, name, section_type FROM library_sections ORDER BY id"):
            if st not in (1, 2):
                continue
            show = st == 2
            rows = q("SELECT m.id, m.added_at, p.extra_data, m.title, m.\"index\", m.parent_id "
                     "FROM metadata_items m JOIN media_items mi ON mi.metadata_item_id = m.id "
                     "JOIN media_parts p ON p.media_item_id = mi.id "
                     "WHERE m.library_section_id = ? AND m.metadata_type = ? AND p.deleted_at IS NULL",
                     sid, 4 if show else 1)
            items: dict = {}
            for mid, added, extra, title, idx, parent in rows:
                it = items.setdefault(mid, {"added": added or 0, "tried": False, "title": title or "",
                                            "idx": idx, "parent": parent})
                if '"pv:intros"' in (extra or ""):
                    it["tried"] = True
            old = {mid: it for mid, it in items.items() if now - it["added"] > GRACE_S}
            lib = {"name": name, "kind": "shows" if show else "films", "items": len(items),
                   "credits": sum(1 for mid in items if mid in marked["credits"]),
                   "credits_missing": sum(1 for mid in old if mid not in marked["credits"])}
            if show:
                never = [mid for mid, it in old.items() if not it["tried"]]
                lib.update(intro_tried=sum(1 for it in items.values() if it["tried"]),
                           intro=sum(1 for mid in items if mid in marked["intro"]),
                           intro_never=len(never))
                for mid in never[:200]:
                    it = items[mid]
                    par = q("SELECT s.\"index\", g.title FROM metadata_items s LEFT JOIN metadata_items g "
                            "ON g.id = s.parent_id WHERE s.id = ?", it["parent"])
                    sn, gt = (par[0] if par else (None, ""))
                    missed.append({"library": name, "show": gt or "", "title": it["title"],
                                   "ep": (f"S{int(sn):02d}E{int(it['idx']):02d}"
                                          if sn is not None and it["idx"] is not None else ""),
                                   "added": it["added"]})
            libs.append(lib)
    finally:
        con.close()
    out = {"ok": True, "at": time.time(), "libs": libs, "missed": missed[:200], "source": marker_source()}
    kv_set(KV, json.dumps(out))
    try:
        trail = json.loads(kv_get(KV_TRAIL) or "[]")
    except Exception:                                            # noqa: BLE001
        trail = []
    day = time.strftime("%Y-%m-%d")
    trail = [t for t in trail if t.get("day") != day]
    trail.append({"day": day, "at": out["at"],
                  "intro_never": sum(x.get("intro_never", 0) for x in libs),
                  "credits_missing": sum(x["credits_missing"] for x in libs if x["kind"] == "shows"),
                  "credits_missing_films": sum(x["credits_missing"] for x in libs if x["kind"] == "films")})
    kv_set(KV_TRAIL, json.dumps(trail[-120:]))
    return out


def last() -> dict:
    try:
        d = json.loads(kv_get(KV) or "{}")
    except Exception:                                            # noqa: BLE001
        d = {}
    try:
        d["trail"] = json.loads(kv_get(KV_TRAIL) or "[]")
    except Exception:                                            # noqa: BLE001
        d["trail"] = []
    d["running"] = STATE["running"]
    d["error"] = STATE["error"]
    return d


async def run_once() -> dict:
    if STATE["running"]:
        return last()
    STATE.update(running=True, error="")
    try:
        r = await asyncio.to_thread(scan)
        if not r.get("ok"):
            STATE["error"] = r.get("why") or "failed"
    except Exception as e:                                       # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        STATE["running"] = False
    return last()


async def watch() -> None:
    await asyncio.sleep(180)
    while True:
        try:
            d = last()
            if time.time() - float(d.get("at") or 0) >= EVERY_S - 600:
                await run_once()
                d = last()
                if d.get("ok"):
                    from . import joblog
                    n = sum(x.get("intro_never", 0) for x in d["libs"])
                    c = sum(x["credits_missing"] for x in d["libs"] if x["kind"] == "shows")
                    joblog.log(f"plex markers: {n} episode(s) never analysed for an intro, "
                               f"{c} episode(s) without a credits marker", "info")
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(3600)
