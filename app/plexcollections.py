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

# THE RULES A LIBRARY CAN KEEP, READ OFF ITS AUDIO LANGUAGE POLICY.
#
# Two are always on offer: Ended (Sonarr's word) and Unwatched (nothing of
# the show watched). The rest come from the library's own "which spoken
# languages a file keeps" rule: every kept language that is NOT the
# library's native one gets a collection of the shows that carry it on
# every regular episode, plus an Ended and an Unwatched cut of that. For
# anime the native language is Japanese, so English - the dub - is the
# collection worth having; for a live-action or animation library English
# IS the native language and a collection of it would be the whole shelf,
# so none is offered. Add Spanish to a library's policy and Spanish,
# Spanish Ended and Spanish Unwatched appear as options on the next look.
NATIVE = {"anime": "jpn", "animation": "eng", "live": "eng"}


def _lang_name(code: str) -> str:
    try:
        from . import langpolicy
        for x in langpolicy.iso_languages():
            if x.get("c") == code:
                return x.get("n") or code
    except Exception:                                        # noqa: BLE001
        pass
    return {"eng": "English", "jpn": "Japanese", "spa": "Spanish",
            "fre": "French", "ger": "German", "kor": "Korean",
            "chi": "Chinese", "ita": "Italian", "por": "Portuguese"}.get(code, code)


def rules_available(library: str) -> list:
    """[{key, title, what, lang}] this library may keep, in display order."""
    out = [{"key": "ended", "title": "Ended", "what": "Sonarr calls the show ended", "lang": ""},
           {"key": "unwatched", "title": "Unwatched",
            "what": "nothing of the show has been watched yet", "lang": ""},
           {"key": "unwatched_ended", "title": "Unwatched Ended",
            "what": "nothing of it watched yet, and Sonarr calls it ended - "
                    "a finished show you can start without waiting", "lang": ""}]
    try:
        from . import langpolicy, langkey
        pol = langpolicy.for_library(library, "audio") or {}
        native = NATIVE.get(langpolicy.kind_for(library=library), "eng")
        seen = set()
        for code in (pol.get("langs") or []):
            k = langkey.key(code)
            if not k or k in ("un", "und") or langkey.same(code, native) or k in seen:
                continue
            seen.add(k)
            name = _lang_name(code)
            out.append({"key": f"lang:{code}", "title": name, "lang": code,
                        "what": f"every regular episode has {name} audio "
                                f"(specials do not count)"})
            out.append({"key": f"lang_ended:{code}", "title": f"{name} Ended",
                        "lang": code, "what": f"{name}, and ended"})
            out.append({"key": f"lang_unwatched:{code}", "title": f"{name} Unwatched",
                        "lang": code,
                        "what": f"{name}, and nothing of it has been watched yet"})
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _title_of(key: str, avail: list) -> str:
    for r in avail:
        if r["key"] == key:
            return r["title"]
    return key


def _want(rule: str, f: dict) -> bool:
    st, un = f.get("status"), bool(f.get("unwatched"))
    langs = set(f.get("langs") or ())
    if rule == "ended":
        return st == "ended"
    if rule == "unwatched":
        return un
    if rule == "unwatched_ended":
        return un and st == "ended"
    kind, _, code = rule.partition(":")
    has = code in langs
    if kind == "lang":
        return has
    if kind == "lang_ended":
        return has and st == "ended"
    if kind == "lang_unwatched":
        return has and un
    return False

# The last pass, per library, for the card. In memory; kv holds the last
# summary across restarts so the page says something before the first run.
STATE: dict = {"running": False, "now": "", "last": {}}


# ------------------------------------------------------------- settings ---
def _key(library: str) -> str:
    return "plexcoll." + library


def enabled(library: str) -> bool:
    return (kv_get(_key(library)) or "") == "1"


def rules_for(library: str) -> list:
    """The rule keys this library keeps, in display order, limited to what
    its policy offers. Nothing stored yet: Ended, Unwatched, and every
    language cut the policy offers."""
    avail = [r["key"] for r in rules_available(library)]
    raw = (kv_get(_key(library) + ".rules") or "").strip()
    if not raw:
        return avail
    if raw == "-":
        return []
    keys = set(raw.split(","))
    return [k for k in avail if k in keys]


def set_enabled(library: str, on: bool, rules: str = "") -> None:
    kv_set(_key(library), "1" if on else "0")
    if rules is not None and rules != "":
        avail = {r["key"] for r in rules_available(library)}
        keep = [k for k in rules.split(",") if k in avail]
        kv_set(_key(library) + ".rules", ",".join(keep) or "-")


def title_for(library: str) -> str:
    """The first kept collection's title - what the health row names."""
    r = rules_for(library)
    return _title_of(r[0], rules_available(library)) if r else TITLE_DEFAULT


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
                    # Nothing watched at all - a show somebody has started is
                    # not "unwatched", whatever Plex's own smart filter calls
                    # a show with unwatched episodes left in it.
                    "unwatched": int(m.get("viewedLeafCount") or 0) == 0,
                    "in": [c.get("tag") or "" for c in (m.get("Collection") or [])]})
    return out


def _plex_viewed_since(section: str, since: float) -> list[dict]:
    """The shows anybody has watched something of since a time."""
    d = _get(f"/library/sections/{section}/all?type=2&includeGuids=1"
             f"&lastViewedAt>>={int(since)}")
    out = []
    for m in (d.get("MediaContainer") or {}).get("Metadata") or []:
        out.append({"key": str(m.get("ratingKey") or ""),
                    "unwatched": int(m.get("viewedLeafCount") or 0) == 0})
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
    """{tvdb id (str): status}, {series path (lower): status}, and the Sonarr
    series id -> tvdb id map nuarr's files table is keyed on."""
    from .arr import shared_client
    by_tvdb: dict = {}
    by_path: dict = {}
    by_sid: dict = {}
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
            tv = str(s.get("tvdbId") or "")
            if tv:
                by_tvdb[tv] = st
                by_sid[(cfg.name, int(s.get("id") or 0))] = tv
            if s.get("path"):
                by_path[str(s["path"]).rstrip("\\/").lower()] = st
    return {"tvdb": by_tvdb, "path": by_path, "sid": by_sid}


# ------------------------------------------------------ languages carried ---
def _langs_by_tvdb(library: str, arr: dict, codes: list) -> dict:
    """{tvdb id: set of codes} - the codes every regular episode of the show
    carries, from nuarr's own probe of every file.

    Specials (season 0) do not count either way: an English OVA does not make
    a Japanese-only show English, and a Japanese-only extra does not take a
    dubbed show out. A file nuarr has not probed counts against the show -
    "every episode" has to mean every episode nuarr can vouch for.
    """
    from .db import cursor
    from . import langkey
    out: dict = {}
    if not codes:
        return out
    per: dict = {}
    with cursor() as cur:
        rows = cur.execute(
            "SELECT arr_name, arr_parent_id, COALESCE(audio_langs,'') al "
            "  FROM files "
            " WHERE library = ? AND state NOT IN ('deleted','duplicate') "
            "   AND COALESCE(season, 0) > 0 AND arr_parent_id IS NOT NULL",
            (library,)).fetchall()
    for r in rows:
        sid = (r["arr_name"], int(r["arr_parent_id"] or 0))
        have = {t.strip() for t in r["al"].split(",") if t.strip()}
        d = per.setdefault(sid, {"n": 0, "hit": {c: 0 for c in codes}})
        d["n"] += 1
        for c in codes:
            if any(langkey.same(c, t) for t in have):
                d["hit"][c] += 1
    for sid, d in per.items():
        tv = arr["sid"].get(sid)
        if not tv or d["n"] == 0:
            continue
        out[tv] = {c for c in codes if d["hit"][c] == d["n"]}
    return out


# ------------------------------------------------------------------ sync ---
#
# A FULL SWEEP, THEN ONLY THE CHANGES. A sweep reads every show in the
# library from Plex with its collections and watched state, works out what
# nuarr knows about each - Sonarr's status, English audio, unwatched - and
# tags the difference for every rule the library keeps. Right the first
# time and once a day to catch drift. Between sweeps the answer can only
# change a few ways: a status changed in Sonarr, a show's files changed
# under nuarr, somebody watched something, a show arrived in Plex. The
# incremental pass asks Sonarr for statuses, nuarr's own table for English,
# and Plex only for shows added or viewed since the last pass, and works
# the rest from what it remembers.
FULL_EVERY_S = 24 * 3600


def _mem_key(library: str) -> str:
    return "plexcoll.mem." + library


def _mem_load(library: str) -> dict:
    try:
        d = json.loads(kv_get(_mem_key(library)) or "{}")
        if d.get("full_at") and d.get("shows") is not None:
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
            STATE["now"] = (f"{what} {batch[0]['title']} to {title}"
                            + (f" and {len(batch) - 1} more" if len(batch) > 1 else "")
                            + f" ({i + len(batch)} of {len(rows)})")
            try:
                await asyncio.to_thread(_tag, section, [b["key"] for b in batch],
                                        title, on)
                res["added"] += len(batch) if on else 0
                res["removed"] += 0 if on else len(batch)
            except Exception as e:                           # noqa: BLE001
                res["error"] = (f"could not {'tag' if on else 'untag'} "
                                f"{batch[0]['title']}: {type(e).__name__}")


def _diff_and_members(rules: list, titles: dict, facts: dict, members: dict) -> tuple:
    """For every rule: what to add, what to remove, and the members after.
    facts: key -> {title, status, langs, unwatched}; members: title -> set."""
    adds: dict = {}
    removes: dict = {}
    after: dict = {}
    for rk in rules:
        title = titles[rk]
        have = set(members.get(title) or ())
        want = {k for k, f in facts.items() if _want(rk, f)}
        adds[title] = [{"key": k, "title": facts[k]["title"]} for k in sorted(want - have)]
        removes[title] = [{"key": k, "title": facts[k].get("title", k)}
                          for k in sorted(have - want) if k in facts]
        after[title] = sorted(want)
    return adds, removes, after


async def sync_library(lib, arr: dict | None = None, dry: bool = False,
                       full: bool = False) -> dict:
    """Bring one library's kept collections in line. Returns what it did."""
    rules = rules_for(lib.name)
    avail = rules_available(lib.name)
    codes = sorted({r["lang"] for r in avail if r["lang"]})
    titles = {rk: _title_of(rk, avail) for rk in rules}
    res = {"library": lib.name, "rules": rules,
           "titles": [titles[r] for r in rules], "at": time.time(),
           "plex_shows": 0, "counts": {}, "added": 0, "removed": 0,
           "unmatched": 0, "unmatched_titles": [], "error": "", "mode": "full"}
    if not rules:
        res["error"] = "no rules are switched on for this library"
        return res
    mem = _mem_load(lib.name)
    now = time.time()
    if (not full and mem and mem.get("rules") == rules
            and now - float(mem.get("full_at") or 0) < FULL_EVERY_S):
        return await _sync_changes(lib, arr, mem, res, dry, titles, codes)
    try:
        section = await asyncio.to_thread(_section_for, lib.path)
        if not section:
            res["error"] = "Plex has no TV library at this path"
            return res
        shows = await asyncio.to_thread(_plex_shows, section)
        if arr is None:
            arr = await _sonarr_status()
        by_lang = await asyncio.to_thread(_langs_by_tvdb, lib.name, arr, codes)
    except Exception as e:                                   # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    res["plex_shows"] = len(shows)
    facts: dict = {}
    members: dict = {titles[r]: set() for r in rules}
    for s in shows:
        st = _status_of(s, arr)
        if st is None:
            res["unmatched"] += 1
            if len(res["unmatched_titles"]) < 100:
                res["unmatched_titles"].append(s["title"])
            continue                     # Sonarr does not know it: leave it be
        facts[s["key"]] = {"title": s["title"], "tvdb": s["tvdb"], "status": st,
                           "langs": sorted(by_lang.get(s["tvdb"]) or ()),
                           "unwatched": bool(s["unwatched"])}
        for t in members:
            if t in s["in"]:
                members[t].add(s["key"])
    adds, removes, after = _diff_and_members(rules, titles, facts, members)
    res["counts"] = {t: len(v) for t, v in after.items()}
    if dry:
        res.update(added=sum(len(v) for v in adds.values()),
                   removed=sum(len(v) for v in removes.values()), dry=True)
        return res
    for t in members:
        await _apply(section, t, adds[t], removes[t], res)
    try:
        res["posters"] = await asyncio.to_thread(
            tidy_collections, section, list(members), mem.get("posters") or {})
    except Exception as e:                                   # noqa: BLE001
        res["posters"] = {"error": f"{type(e).__name__}: {e}"}
    if not res["error"]:
        _mem_save(lib.name, {"full_at": now, "changes_at": now, "section": section,
                             "rules": rules, "shows": facts, "members": after,
                             "plex_shows": len(shows),
                             "unmatched_titles": res["unmatched_titles"],
                             "posters": (res.get("posters") or {}).get("stamps")
                                        or mem.get("posters") or {}})
    return res


async def _sync_changes(lib, arr: dict | None, mem: dict, res: dict,
                        dry: bool = False, titles: dict | None = None,
                        codes: list | None = None) -> dict:
    """Only what can have changed since the last pass."""
    res["mode"] = "changes"
    rules = res["rules"]
    section = mem.get("section") or ""
    try:
        if arr is None:
            arr = await _sonarr_status()
        by_lang = await asyncio.to_thread(_langs_by_tvdb, lib.name, arr, codes or [])
        since = float(mem.get("changes_at") or mem.get("full_at") or 0) - 3600
        new_shows = await asyncio.to_thread(
            _plex_shows, section, float(mem.get("full_at") or 0) - 3600)
        viewed = await asyncio.to_thread(_plex_viewed_since, section, since)
    except Exception as e:                                   # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    facts = {k: dict(v) for k, v in (mem.get("shows") or {}).items()}
    # 1. what Sonarr and nuarr's own table say now, for every remembered show
    for k, f in facts.items():
        tv = f.get("tvdb") or ""
        if tv and tv in arr["tvdb"]:
            f["status"] = arr["tvdb"][tv]
        f["langs"] = sorted(by_lang.get(tv) or ()) if tv else []
    # 2. somebody watched something
    for v in viewed:
        if v["key"] in facts:
            facts[v["key"]]["unwatched"] = v["unwatched"]
    # 3. shows Plex added since the sweep
    seen_new = 0
    for s in new_shows:
        if s["key"] in facts:
            continue
        seen_new += 1
        st = _status_of(s, arr)
        if st is None:
            if s["title"] not in res["unmatched_titles"] and len(res["unmatched_titles"]) < 100:
                res["unmatched_titles"].append(s["title"])
            continue
        facts[s["key"]] = {"title": s["title"], "tvdb": s["tvdb"], "status": st,
                           "langs": sorted(by_lang.get(s["tvdb"]) or ()),
                           "unwatched": bool(s["unwatched"])}
    for t in (mem.get("unmatched_titles") or []):
        if t not in res["unmatched_titles"] and len(res["unmatched_titles"]) < 100:
            res["unmatched_titles"].append(t)
    res["unmatched"] = len(res["unmatched_titles"])
    res["plex_shows"] = int(mem.get("plex_shows") or 0) + seen_new
    members = {t: set(v) for t, v in (mem.get("members") or {}).items()}
    for rk in rules:
        members.setdefault(titles[rk], set())
    adds, removes, after = _diff_and_members(rules, titles, facts, members)
    res["counts"] = {t: len(v) for t, v in after.items()}
    if dry:
        res.update(added=sum(len(v) for v in adds.values()),
                   removed=sum(len(v) for v in removes.values()), dry=True)
        return res
    for rk in rules:
        t = titles[rk]
        await _apply(section, t, adds[t], removes[t], res)
    if not res["error"]:
        mem.update(shows=facts, members=after, plex_shows=res["plex_shows"],
                   unmatched_titles=res["unmatched_titles"], changes_at=time.time())
        _mem_save(lib.name, mem)
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


def tidy_collections(section: str, kept_titles: list, stamps: dict) -> dict:
    """Sort the kept collections by title; give every blank one a collage.
    Returns {"stamps": {coll_key: "k1,k2,k3,k4"}, "posted": n, "sorted": bool}."""
    out = {"stamps": dict(stamps), "posted": 0, "sorted": False}
    d = _get(f"/library/sections/{section}/collections")
    for c in (d.get("MediaContainer") or {}).get("Metadata") or []:
        key = str(c.get("ratingKey") or "")
        if not key:
            continue
        if (c.get("title") or "") in kept_titles and str(c.get("collectionSort")) != "1":
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
                joblog.log(f"collections: {lib.name} - {r['error']}", "warn")
            elif r["added"] or r["removed"]:
                joblog.log(f"collections: {lib.name} - {r['added']} added, "
                           f"{r['removed']} removed across "
                           f"{', '.join(r['titles'])} "
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
        rules = rules_for(lib.name)
        libs.append({"library": lib.name, "on": enabled(lib.name),
                     "rules": rules, "available": rules_available(lib.name),
                     "title": title_for(lib.name),
                     "last": last.get(lib.name) or {}})
    return {"running": STATE["running"], "now": STATE["now"],
            "every_h": EVERY_S // 3600, "libraries": libs}


async def watch() -> None:
    schedules.register(
        "plexcoll", "Plex collections", "Plex", EVERY_S,
        what="Keeps collections per switched-on TV library in step with what "
             "nuarr knows: Ended (Sonarr), Unwatched (Plex), and for every "
             "non-native language the library's audio policy keeps, the shows "
             "that carry it on every regular episode, with Ended and "
             "Unwatched cuts.")
    await asyncio.sleep(300)
    while True:
        schedules.beat("plexcoll")
        try:
            await sync()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"collections: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(EVERY_S)
