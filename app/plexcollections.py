"""
nuarr - collections Plex cannot build for itself

WHY
---
Plex smart collections filter on what Plex knows: year, genre, rating,
watched. Plex does not know whether a show has ENDED - that is Sonarr's
word, read from TheTVDB, and it changes when a network announces another
season. So "every finished show in Anime Shows" cannot be a smart
collection, and a hand-made one goes stale the day a show comes back.

Nuarr knows both sides. Sonarr says which series are ended and which are
continuing; Plex says which shows are in the library and what their tvdb id
is. This module keeps a collection per library in step with that answer:
shows that are ended are in it, shows that are continuing or upcoming are
not, and a show that goes from ended to continuing leaves on the next pass.

HOW
---
Plex's tag interface, not its newer collections endpoint: setting the
"collection" tag on a show puts it in a collection of that name (creating
it the first time), and removing the tag takes it out. One PUT per change,
nothing to create or delete by hand, and Plex owns the collection's poster
and ordering as it does for any other.

Matching is by tvdb id - Plex carries `tvdb://NNN` in a show's Guid list and
Sonarr carries tvdbId - with the series path as the fallback for a show Plex
has not matched to an agent.

Runs every six hours, five minutes after startup, and on demand. Per
library, switchable, off unless turned on: a collection appearing in a
library nobody asked for it in is a surprise.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
import urllib.request

from . import joblog, schedules
from .config import SETTINGS
from .db import kv_get, kv_set

EVERY_S = 6 * 3600
TITLE_DEFAULT = "Ended"

# The last pass, per library, for the card. In memory; kv holds the last
# summary across restarts so the page says something before the first run.
STATE: dict = {"running": False, "now": "", "last": {}}


# ------------------------------------------------------------- settings ---
def _key(library: str) -> str:
    return "plexcoll." + library


def enabled(library: str) -> bool:
    return (kv_get(_key(library)) or "") == "1"


def title_for(library: str) -> str:
    return (kv_get(_key(library) + ".title") or "").strip() or TITLE_DEFAULT


def set_enabled(library: str, on: bool, title: str = "") -> None:
    kv_set(_key(library), "1" if on else "0")
    if title:
        kv_set(_key(library) + ".title", title.strip()[:60])


def tv_libraries() -> list:
    """Every library Sonarr feeds, whatever it is called."""
    out = []
    for lib in (SETTINGS.libraries or []):
        kind = (getattr(lib, "kind", "") or getattr(lib, "type", "") or "").lower()
        if kind and "movie" in kind:
            continue
        out.append(lib)
    return out


# ------------------------------------------------------------------ plex ---
def _plex() -> tuple[str, str]:
    from . import plexnotify
    return plexnotify._plex()


def _get(path: str, timeout: float = 60.0) -> dict:
    url, token = _plex()
    req = urllib.request.Request(
        url + path, headers={"X-Plex-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _put(path: str, timeout: float = 30.0) -> None:
    url, token = _plex()
    req = urllib.request.Request(
        url + path, method="PUT",
        headers={"X-Plex-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def _section_for(lib_path: str) -> str:
    want = (lib_path or "").rstrip("\\/").lower()
    d = _get("/library/sections")
    for s in (d.get("MediaContainer") or {}).get("Directory") or []:
        if (s.get("type") or "") != "show":
            continue
        for loc in s.get("Location") or []:
            if (loc.get("path") or "").rstrip("\\/").lower() == want:
                return str(s.get("key") or "")
    return ""


def _plex_shows(section: str, added_since: float = 0.0) -> list[dict]:
    """[{key, title, tvdb, path, in: [collection titles]}] for one section -
    the whole of it, or only the shows Plex added since a time."""
    q = "&addedAt>>=" + str(int(added_since)) if added_since else ""
    d = _get(f"/library/sections/{section}/all?type=2&includeGuids=1{q}")
    out = []
    for m in (d.get("MediaContainer") or {}).get("Metadata") or []:
        tvdb = ""
        for g in m.get("Guid") or []:
            gid = str(g.get("id") or "")
            if gid.startswith("tvdb://"):
                tvdb = gid[7:]
                break
        loc = ""
        for l in m.get("Location") or []:
            loc = (l.get("path") or "")
            if loc:
                break
        out.append({"key": str(m.get("ratingKey") or ""),
                    "title": m.get("title") or "", "tvdb": tvdb,
                    "path": loc.rstrip("\\/").lower(),
                    "in": [c.get("tag") or "" for c in (m.get("Collection") or [])]})
    return out


def _tag(section: str, keys, title: str, on: bool) -> None:
    """Tag (or untag) one show or a batch of them in a single PUT.

    Plex takes a second or so per tag write, and the first pass on a library
    of six hundred ended shows is six hundred of them - ten minutes done one
    at a time, and it was. The endpoint accepts a comma-separated list of
    ids, so a batch of fifty costs about what one did.
    """
    if isinstance(keys, str):
        keys = [keys]
    ids = ",".join(str(k) for k in keys if k)
    if not ids:
        return
    q = urllib.parse.urlencode({"type": 2, "id": ids, "collection.locked": 1})
    field = ("collection[0].tag.tag=" if on else "collection[].tag.tag-=") \
        + urllib.parse.quote(title)
    _put(f"/library/sections/{section}/all?{q}&{field}", timeout=120)


BATCH = 50


# ---------------------------------------------------------------- sonarr ---
async def _sonarr_status() -> dict:
    """{tvdb id (str): status} and {series path (lower): status} from every
    enabled Sonarr."""
    from .arr import shared_client
    by_tvdb: dict = {}
    by_path: dict = {}
    for cfg in (SETTINGS.arrs or []):
        if cfg.kind != "sonarr" or not cfg.enabled or not cfg.api_key:
            continue
        try:
            series = await shared_client(cfg)._get("/series")
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"collections: {cfg.name} did not answer: "
                       f"{type(e).__name__}: {e}", "warn")
            continue
        for s in series or []:
            st = str(s.get("status") or "").lower()
            if s.get("tvdbId"):
                by_tvdb[str(s["tvdbId"])] = st
            if s.get("path"):
                by_path[str(s["path"]).rstrip("\\/").lower()] = st
    return {"tvdb": by_tvdb, "path": by_path}


# ------------------------------------------------------------------ sync ---
#
# A FULL SWEEP, THEN ONLY THE CHANGES. A sweep reads every show in the
# library from Plex with its collections, matches each to Sonarr, and tags
# the difference - the right thing to do the first time and once a day to
# catch drift. Between sweeps the answer can only change three ways: a show
# changed status in Sonarr, a show arrived in Plex, a show left Plex. The
# incremental pass asks Sonarr for statuses (one call, always), asks Plex
# only for shows added since the sweep, and compares against what it
# remembers - so a six-hourly pass on a library of seven hundred shows is
# two small requests and usually no writes at all.
FULL_EVERY_S = 24 * 3600


def _mem_key(library: str) -> str:
    return "plexcoll.mem." + library


def _mem_load(library: str) -> dict:
    try:
        d = json.loads(kv_get(_mem_key(library)) or "{}")
        if d.get("full_at"):
            return d
    except Exception:                                        # noqa: BLE001
        pass
    return {}


def _mem_save(library: str, mem: dict) -> None:
    try:
        kv_set(_mem_key(library), json.dumps(mem))
    except Exception:                                        # noqa: BLE001
        pass


def _status_of(s: dict, arr: dict):
    st = arr["tvdb"].get(s["tvdb"]) if s["tvdb"] else None
    if st is None and s["path"]:
        st = arr["path"].get(s["path"])
    return st


async def _apply(section: str, title: str, add: list, remove: list, res: dict) -> None:
    for what, rows, on in (("adding", add, True), ("removing", remove, False)):
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            STATE["now"] = (f"{what} {batch[0]['title']}"
                            + (f" and {len(batch) - 1} more" if len(batch) > 1 else "")
                            + f" ({i + len(batch)} of {len(rows)})")
            try:
                await asyncio.to_thread(_tag, section, [b["key"] for b in batch],
                                        title, on)
                res["added" if on else "removed"] += len(batch)
            except Exception as e:                           # noqa: BLE001
                res["error"] = (f"could not {'tag' if on else 'untag'} "
                                f"{batch[0]['title']}: {type(e).__name__}")


async def sync_library(lib, arr: dict | None = None, dry: bool = False,
                       full: bool = False) -> dict:
    """Bring one library's Ended collection in line. Returns what it did."""
    title = title_for(lib.name)
    res = {"library": lib.name, "title": title, "at": time.time(),
           "plex_shows": 0, "ended": 0, "in_collection": 0,
           "added": 0, "removed": 0, "unmatched": 0, "unmatched_titles": [],
           "error": "", "mode": "full"}
    mem = _mem_load(lib.name)
    now = time.time()
    if (not full and mem and mem.get("title") == title
            and now - float(mem.get("full_at") or 0) < FULL_EVERY_S):
        return await _sync_changes(lib, section_hint=mem.get("section", ""),
                                   arr=arr, mem=mem, res=res, dry=dry)
    try:
        section = await asyncio.to_thread(_section_for, lib.path)
        if not section:
            res["error"] = "Plex has no TV library at this path"
            return res
        shows = await asyncio.to_thread(_plex_shows, section)
        if arr is None:
            arr = await _sonarr_status()
    except Exception as e:                                   # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    res["plex_shows"] = len(shows)
    add, remove = [], []
    keys: dict = {}          # tvdb -> rating key, for the incremental passes
    status: dict = {}        # tvdb -> status as of this sweep
    members: dict = {}       # rating key -> title, as of this sweep
    for s in shows:
        st = _status_of(s, arr)
        if st is None:
            res["unmatched"] += 1
            if len(res["unmatched_titles"]) < 100:
                res["unmatched_titles"].append(s["title"])
            continue                     # Sonarr does not know it: leave it be
        want = st == "ended"
        have = title in s["in"]
        if s["tvdb"]:
            keys[s["tvdb"]] = s["key"]
            status[s["tvdb"]] = st
        if want:
            res["ended"] += 1
        if have:
            res["in_collection"] += 1
        if want and not have:
            add.append(s)
        elif have and not want:
            remove.append(s)
        if want:
            members[s["key"]] = s["title"]
    if dry:
        res.update(added=len(add), removed=len(remove), dry=True)
        return res
    await _apply(section, title, add, remove, res)
    res["in_collection"] = res["in_collection"] + res["added"] - res["removed"]
    # The finishing touches, on the full sweep only: the kept collection
    # sorted by title, and a poster for every collection in the library that
    # Plex leaves blank. Never fatal - a poster is not a membership.
    try:
        res["posters"] = await asyncio.to_thread(
            tidy_collections, section, title, mem.get("posters") or {})
    except Exception as e:                                   # noqa: BLE001
        res["posters"] = {"error": f"{type(e).__name__}: {e}"}
    if not res["error"]:
        _mem_save(lib.name, {"full_at": now, "section": section, "title": title,
                             "keys": keys, "status": status, "members": members,
                             "plex_shows": len(shows),
                             "unmatched_titles": res["unmatched_titles"],
                             "posters": (res["posters"] or {}).get("stamps")
                                        or mem.get("posters") or {}})
    return res


# --------------------------------------------------------- the finishing ---
#
# SORTED BY TITLE. A collection Plex builds from tags lists its members in
# the order they were added, which for ours is the order Plex returned the
# library - useless to browse. collectionSort=1 is Plex's own "alphabetical".
#
# A POSTER FOR THE ONES PLEX LEAVES BLANK. Plex draws a 2x2 collage for an
# ordinary collection and NOTHING for a smart one - the composite URL it
# advertises for a smart collection answers 404, which is why "English
# Unwatched" was a grey tile. So nuarr draws the same collage itself, from
# the first four members' posters, and uploads it. Re-uploaded only when
# those four change, so the poster list does not fill with copies.
COLLAGE_W, COLLAGE_H = 400, 600


def _posters_of(coll_key: str) -> list:
    d = _get(f"/library/collections/{coll_key}/children?X-Plex-Container-Size=4"
             "&X-Plex-Container-Start=0")
    out = []
    for m in (d.get("MediaContainer") or {}).get("Metadata") or []:
        if m.get("thumb"):
            out.append((str(m.get("ratingKey") or ""), m["thumb"]))
    return out[:4]


def _fetch_image(path: str) -> bytes:
    url, token = _plex()
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        f"{url}{path}{sep}width=200&height=300&X-Plex-Token={token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _collage(thumbs: list) -> bytes:
    import io as _io
    from PIL import Image
    canvas = Image.new("RGB", (COLLAGE_W, COLLAGE_H), (15, 18, 22))
    cw, ch = COLLAGE_W // 2, COLLAGE_H // 2
    for i, (_k, th) in enumerate(thumbs[:4]):
        try:
            im = Image.open(_io.BytesIO(_fetch_image(th))).convert("RGB")
        except Exception:                                    # noqa: BLE001
            continue
        # cover-fit into the cell
        r = max(cw / im.width, ch / im.height)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))))
        x0 = (im.width - cw) // 2
        y0 = (im.height - ch) // 2
        im = im.crop((x0, y0, x0 + cw, y0 + ch))
        canvas.paste(im, ((i % 2) * cw, (i // 2) * ch))
    b = _io.BytesIO()
    canvas.save(b, "JPEG", quality=88)
    return b.getvalue()


def _upload_poster(coll_key: str, data: bytes) -> None:
    url, token = _plex()
    req = urllib.request.Request(
        f"{url}/library/metadata/{coll_key}/posters", data=data, method="POST",
        headers={"X-Plex-Token": token, "Content-Type": "image/jpeg"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def tidy_collections(section: str, kept_title: str, stamps: dict) -> dict:
    """Sort the kept collection by title; give every blank one a collage.
    Returns {"stamps": {coll_key: "k1,k2,k3,k4"}, "posted": n, "sorted": bool}."""
    out = {"stamps": dict(stamps), "posted": 0, "sorted": False}
    d = _get(f"/library/sections/{section}/collections")
    for c in (d.get("MediaContainer") or {}).get("Metadata") or []:
        key = str(c.get("ratingKey") or "")
        if not key:
            continue
        if (c.get("title") or "") == kept_title and str(c.get("collectionSort")) != "1":
            _put(f"/library/metadata/{key}/prefs?collectionSort=1")
            out["sorted"] = True
        # Does its poster actually answer? Plex's own composite does for an
        # ordinary collection and does not for a smart one; an uploaded
        # poster always does. Only a blank tile gets a collage.
        thumb = c.get("thumb") or ""
        blank = False
        if "/composite/" in thumb:
            try:
                _fetch_image(thumb)
            except Exception:                                # noqa: BLE001
                blank = True
        if not blank and key not in out["stamps"]:
            continue                       # has a poster nuarr did not make
        thumbs = _posters_of(key)
        stamp = ",".join(k for k, _t in thumbs)
        if not thumbs or out["stamps"].get(key) == stamp:
            continue                       # nothing to draw, or unchanged
        _upload_poster(key, _collage(thumbs))
        out["stamps"][key] = stamp
        out["posted"] += 1
    return out


async def _sync_changes(lib, section_hint: str, arr: dict | None, mem: dict,
                        res: dict, dry: bool = False) -> dict:
    """Only what can have changed since the last full sweep."""
    title = res["title"]
    res["mode"] = "changes"
    section = section_hint
    try:
        if arr is None:
            arr = await _sonarr_status()
        since = float(mem.get("full_at") or 0) - 3600     # an hour of slack
        new_shows = await asyncio.to_thread(_plex_shows, section, since)
    except Exception as e:                                   # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    keys = dict(mem.get("keys") or {})
    status = dict(mem.get("status") or {})
    members = dict(mem.get("members") or {})
    add, remove = [], []
    # 1. status changes in Sonarr for shows the sweep matched
    for tvdb, key in keys.items():
        new = arr["tvdb"].get(tvdb)
        if new is None or new == status.get(tvdb):
            continue
        was_in = key in members
        want = new == "ended"
        status[tvdb] = new
        if want and not was_in:
            add.append({"key": key, "title": f"tvdb {tvdb}"})
            members[key] = f"tvdb {tvdb}"
        elif was_in and not want:
            remove.append({"key": key, "title": f"tvdb {tvdb}"})
            members.pop(key, None)
    # 2. shows Plex added since the sweep
    seen_new = 0
    for s in new_shows:
        if s["tvdb"] and s["tvdb"] in keys and keys[s["tvdb"]] == s["key"]:
            continue                      # the sweep already had it
        seen_new += 1
        st = _status_of(s, arr)
        if st is None:
            res["unmatched"] += 1
            if len(res["unmatched_titles"]) < 100:
                res["unmatched_titles"].append(s["title"])
            continue
        if s["tvdb"]:
            keys[s["tvdb"]] = s["key"]
            status[s["tvdb"]] = st
        want, have = st == "ended", title in s["in"]
        if want and not have:
            add.append(s)
            members[s["key"]] = s["title"]
        elif have and not want:
            remove.append(s)
            members.pop(s["key"], None)
        elif want:
            members[s["key"]] = s["title"]
    # what the sweep knew, plus the ones that were not in Sonarr then either
    old_unmatched = list(mem.get("unmatched_titles") or [])
    for t in old_unmatched:
        if t not in res["unmatched_titles"] and len(res["unmatched_titles"]) < 100:
            res["unmatched_titles"].append(t)
    res["unmatched"] = len(res["unmatched_titles"])
    res["plex_shows"] = int(mem.get("plex_shows") or 0) + seen_new
    res["ended"] = sum(1 for v in status.values() if v == "ended")
    if dry:
        res.update(added=len(add), removed=len(remove), dry=True,
                   in_collection=len(members))
        return res
    await _apply(section, title, add, remove, res)
    res["in_collection"] = len(members)
    if not res["error"]:
        mem.update(keys=keys, status=status, members=members,
                   plex_shows=res["plex_shows"],
                   unmatched_titles=res["unmatched_titles"], changes_at=time.time())
        _mem_save(lib.name, mem)
    return res


async def sync(force: bool = False) -> dict:
    """Every switched-on library. Returns {library: result}. force = a full
    sweep now, whatever the memory says."""
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    libs = [l for l in tv_libraries() if enabled(l.name)]
    if not libs:
        return {"ok": True, "libraries": {}}
    STATE.update(running=True, now="asking Sonarr")
    out: dict = {}
    try:
        arr = await _sonarr_status()
        for lib in libs:
            r = await sync_library(lib, arr, full=force)
            out[lib.name] = r
            if r.get("error"):
                joblog.log(f"collections: {lib.name} '{r['title']}' - "
                           f"{r['error']}", "warn")
            elif r["added"] or r["removed"]:
                joblog.log(f"collections: {lib.name} '{r['title']}' - "
                           f"{r['added']} added, {r['removed']} removed, "
                           f"{r['in_collection']} in it now "
                           f"({r.get('mode', 'full')} pass)", "ok")
        STATE["last"] = out
        try:
            kv_set("plexcoll.last", json.dumps(out))
        except Exception:                                    # noqa: BLE001
            pass
    finally:
        STATE.update(running=False, now="")
    return {"ok": True, "libraries": out}


def status() -> dict:
    last = STATE.get("last") or {}
    if not last:
        try:
            last = json.loads(kv_get("plexcoll.last") or "{}")
        except Exception:                                    # noqa: BLE001
            last = {}
    libs = []
    for lib in tv_libraries():
        libs.append({"library": lib.name, "on": enabled(lib.name),
                     "title": title_for(lib.name),
                     "last": last.get(lib.name) or {}})
    return {"running": STATE["running"], "now": STATE["now"],
            "every_h": EVERY_S // 3600, "libraries": libs}


async def watch() -> None:
    schedules.register(
        "plexcoll", "Plex collections", "Plex", EVERY_S,
        what="Keeps an 'Ended' collection per switched-on TV library in step "
             "with Sonarr: shows Sonarr calls ended are in it, shows that are "
             "continuing or come back are not.")
    await asyncio.sleep(300)
    while True:
        schedules.beat("plexcoll")
        try:
            await sync()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"collections: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(EVERY_S)
