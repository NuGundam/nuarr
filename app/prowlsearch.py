r"""The Prowlarr searcher: nuarr's own look for a replacement, with the arr
asked to download what it finds.

WHY NOT THE ARR'S OWN SEARCH. Erik, 2026-11-08: "Sonarr/Radarr sometimes
misses better options." The arr's automatic search is one indexer round
scored by its own rules, and it never weighs a season pack against the one
episode it is looking for. This asks Prowlarr directly - every indexer it
has, not only the ones synced to that arr - reads each name with the arr's
own parser (quality, custom-format score, which episodes it holds), ranks
them by the arr's quality profile, and for a series decides whether a pack
beats the single: it does when it is at least as good as the best single AND
not a step down from any file already in that season. The winner is pushed
to the arr (release/push), so the arr downloads and imports it exactly as if
it had found it itself; a push the arr refuses (blocklisted, banned, over a
size limit) moves on to the next candidate. Nothing here downloads outside
the arr.

WHERE IT RUNS. refetch calls it after deleting a rejected file and before
blocklisting the old grab (the blocklist call also starts the arr's own
search, and two downloads for one episode are the queue janitor's to
settle). The Run now button sweeps the arrs' wanted lists for the items
their own searches have not filled.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
import urllib.request

from . import joblog
from .db import kv_get, kv_set

KV_RECENT = "prowlsearch.recent"
RECENT_KEEP = 40
PARSE_MAX = 70         # names read with the arr's parser per search
TRY_MAX = 6            # distinct names the arr may refuse before giving up
SEARCH_TIMEOUT = 240
SWEEP_EVERY_S = 6 * 3600
SWEEP_MAX = 12         # wanted items looked for per sweep
STATS: dict = {"last_run": 0.0, "last_result": "", "next_run": 0.0, "running": False,
               "searched": 0, "pushed": 0, "detail": []}
VIDEO_CATS = range(2000, 6000)   # Newznab movies 2000-2999, TV 5000-5999
# WHAT IS BEING SEARCHED RIGHT NOW, for the card's progress bar. One entry per
# search in flight (a refetch can run beside a sweep), keyed by its label;
# SWEEP is the sweep's own place in its list.
ACTIVE: dict = {}
SWEEP: dict = {}


def _at(label: str, step: str, **kw) -> None:
    e = ACTIVE.setdefault(label, {"item": label, "since": time.time()})
    e.update(step=step, step_at=time.time(), **kw)


# ------------------------------------------------------------ prowlarr ----
def _prowlarr() -> tuple[str, str]:
    from . import svcnurse
    return svcnurse.prowlarr_url(), svcnurse.prowlarr_key()


def _pget(path: str, **q):
    url, key = _prowlarr()
    if not url or not key:
        raise RuntimeError("Prowlarr is not configured (Settings > Health checks > services nurse)")
    u = url.rstrip("/") + path + ("?" + urllib.parse.urlencode({k: v for k, v in q.items() if v not in (None, "")}) if q else "")
    req = urllib.request.Request(u, headers={"X-Api-Key": key})
    return json.loads(urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT).read().decode())


async def _searches(queries: list[dict]) -> list[dict]:
    """Every query at once; results merged, one row per guid."""
    outs = await asyncio.gather(*(asyncio.to_thread(_pget, "/api/v1/search", **q) for q in queries),
                                return_exceptions=True)
    seen: dict = {}
    for o in outs:
        if isinstance(o, Exception):
            STATS["detail"].append(f"Prowlarr: {type(o).__name__}: {o}"[:160])
            continue
        for r in o or []:
            if r.get("guid") and r["guid"] not in seen:
                seen[r["guid"]] = r
    return list(seen.values())


# ------------------------------------------------------------ reading -----
_SE = re.compile(r"S(\d{1,2})[ ._-]?E(\d{1,3})", re.I)
_SPACK = re.compile(r"(?<![A-Z0-9])S(\d{1,2})(?![EP\d])", re.I)


def _prefilter(rows: list, season, episode, kind: str) -> list:
    """Drop what cannot be this item before the parser is asked: a single
    that names another episode. Names with no SxxEyy stay (anime absolute
    numbers, packs, films); the arr's parser decides those."""
    keep, rest = [], []
    for r in rows:
        t = str(r.get("title") or "")
        cats = [(c.get("id") if isinstance(c, dict) else c) for c in (r.get("categories") or [])]
        cats = [int(c) for c in cats if str(c).isdigit()]
        if cats and not any(c in VIDEO_CATS for c in cats):
            continue
        if kind == "sonarr" and season is not None:
            m = _SE.search(t)
            if m:
                if int(m.group(1)) != int(season) or (episode is not None and int(m.group(2)) != int(episode)):
                    continue
                keep.append(r)
                continue
            mp = _SPACK.search(t)
            if mp and int(mp.group(1)) != int(season):
                continue
        rest.append(r)
    out = keep + rest
    out.sort(key=lambda r: (0 if r.get("protocol") == "usenet" else 1, -(r.get("seeders") or 0), -(r.get("size") or 0)))
    return out[:PARSE_MAX]


def _profile(client_get_sync, a, profile_id: int) -> dict:
    """{allowed quality name: rank}, min format score."""
    pr = client_get_sync(f"/api/v3/qualityprofile/{int(profile_id)}") or {}
    names: list = []
    for it in pr.get("items") or []:
        if not it.get("allowed"):
            continue
        if it.get("quality"):
            names.append((it["quality"] or {}).get("name") or "")
        else:
            names.extend(((q.get("quality") or {}).get("name") or "") for q in (it.get("items") or []))
    return {"rank": {n: i for i, n in enumerate(names)}, "min_score": int(pr.get("minFormatScore") or 0),
            "name": pr.get("name") or str(profile_id)}


def _banned(title: str) -> str:
    """The nuarr ban this name hits, or ''."""
    try:
        from . import arrban
        low = title.lower()
        grp = (arrban.group_of(title) or "").lower()
        for b in arrban.rows(False):
            v = str(b.get("value") or "")
            if not v:
                continue
            k = b.get("kind")
            if k == "release" and arrban._clean_title(v).lower() == arrban._clean_title(title).lower():
                return f"banned release {v[:40]}"
            if k == "group" and grp and grp == v.lower():
                return f"banned group {v}"
            if k == "term" and v.lower() in low:
                return f"banned term {v}"
    except Exception:                                            # noqa: BLE001
        pass
    return ""


async def _read(client, a, rows: list, want: dict, prof: dict) -> list:
    """Each name through the arr's parser -> candidates for this item."""
    sem = asyncio.Semaphore(6)

    async def one(r):
        async with sem:
            try:
                return r, await client._get("/parse", title=r.get("title") or "")
            except Exception:                                    # noqa: BLE001
                return r, None
    out = []
    for r, p in await asyncio.gather(*(one(r) for r in rows)):
        if not p:
            continue
        if a.kind == "sonarr":
            info = p.get("parsedEpisodeInfo") or {}
            if (p.get("series") or {}).get("id") != want["seriesId"]:
                continue
            eps = [e.get("id") for e in (p.get("episodes") or [])]
            pack = bool(info.get("fullSeason")) and int(info.get("seasonNumber") or -1) == int(want["season"])
            if want["episodeId"] not in eps and not pack:
                continue
            if not pack and len(eps) > 1 and want["episodeId"] in eps:
                pack = False      # a multi-episode file: judged as a single, the arr imports what it holds
        else:
            info = p.get("parsedMovieInfo") or {}
            if (p.get("movie") or {}).get("id") != want["movieId"]:
                continue
            eps, pack = [], False
        q = ((info.get("quality") or {}).get("quality") or {}).get("name") or ""
        if q not in prof["rank"]:
            continue
        score = int(p.get("customFormatScore") or 0)
        if score < prof["min_score"]:
            continue
        ban = _banned(r.get("title") or "")
        if ban:
            continue
        # A TORRENT NOBODY SEEDS is a download that sits at 0 % until the
        # janitor's two-hour stall rule removes it. Seeders unknown stays.
        if r.get("protocol") == "torrent" and r.get("seeders") is not None and int(r.get("seeders") or 0) < 1:
            continue
        out.append({"title": r.get("title") or "", "guid": r.get("guid"), "indexerId": r.get("indexerId"),
                    "indexer": r.get("indexer") or "", "protocol": r.get("protocol") or "",
                    "size": int(r.get("size") or 0), "seeders": r.get("seeders"),
                    "downloadUrl": r.get("downloadUrl") or r.get("magnetUrl") or "",
                    "publishDate": r.get("publishDate") or "", "quality": q, "qrank": prof["rank"][q],
                    "score": score, "pack": pack, "eps": eps,
                    "cfs": [c.get("name") for c in (p.get("customFormats") or [])]})
    return out


def _key(c: dict) -> tuple:
    return (c["qrank"], c["score"], 1 if c["protocol"] == "usenet" else 0, int(c.get("seeders") or 0), c["size"])


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(t or "").lower())


async def _blocklisted(client, a, want: dict) -> set:
    """Normalised names the arr has blocklisted for this item - a push of
    one of those is refused, so they are not tried. God of Gamblers III
    (2026-11-08): four pushes, four 'Release is blocklisted', all one name
    offered by four indexers."""
    try:
        if a.kind == "sonarr":
            b = await client._get("/blocklist", pageSize=500, seriesIds=want["seriesId"])
        else:
            b = await client._get("/blocklist/movie", movieId=want["movieId"])
        rows = b.get("records") if isinstance(b, dict) else b
        return {_norm(r.get("sourceTitle")) for r in rows or [] if r.get("sourceTitle")}
    except Exception:                                            # noqa: BLE001
        return set()


def _distinct(order: list) -> list:
    """One candidate per release name - the best-ranked copy of it."""
    seen: set = set()
    out = []
    for c in order:
        k = _norm(c["title"])
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _pack_beats(pack: dict, best_single: dict | None, season_files: list, prof: dict) -> str:
    """Why the pack is the better grab, or '' when it is not."""
    if best_single and _key(pack)[:2] < _key(best_single)[:2]:
        return ""
    worse = []
    for f in season_files:
        fq = ((f.get("quality") or {}).get("quality") or {}).get("name") or ""
        fr = prof["rank"].get(fq, -1)
        fs = int(f.get("customFormatScore") or 0)
        if (pack["qrank"], pack["score"]) < (fr, fs):
            worse.append(f"S{f.get('seasonNumber'):02d}E{f.get('episodeNumber'):02d} {fq} {fs:,}")
    if worse:
        return ""
    n = len(season_files)
    return (f"the season pack is at least as good as the best single and not a step down from "
            f"any of the {n} file(s) already in the season" if n else
            "the season pack is at least as good as the best single and the season is otherwise empty")


async def _push(client, a, c: dict) -> tuple[bool, str]:
    body = {"title": c["title"], "downloadUrl": c["downloadUrl"], "protocol": c["protocol"],
            "publishDate": c["publishDate"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "size": c["size"], "indexer": c["indexer"]}
    try:
        r = await client._post("/release/push", body)
    except Exception as e:                                       # noqa: BLE001
        txt = ""
        try:
            txt = e.response.text[:200]                          # type: ignore[attr-defined]
        except Exception:                                        # noqa: BLE001
            pass
        return False, f"{type(e).__name__}: {txt or e}"[:200]
    rows = r if isinstance(r, list) else [r]
    rej = [j for x in rows if isinstance(x, dict) for j in (x.get("rejections") or [])]
    ok = any(isinstance(x, dict) and (x.get("approved") or x.get("downloadAllowed")) for x in rows) and not rej
    if not ok and not rej and rows and isinstance(rows[0], dict) and "approved" not in rows[0]:
        ok = True        # an older arr answers with the release and no verdict: the grab went through
    return ok, "; ".join(str(j) for j in rej)[:200]


# ------------------------------------------------------------ one item ----
async def find(a, want: dict, source: str = "") -> dict:
    """want: sonarr {seriesId, episodeId, season, episode, title, tvdbId, profileId}
             radarr {movieId, title, year, tmdbId, profileId}
    -> {ok, pushed: title, why, tried: n, found: n}"""
    from .arr import shared_client
    from .arrqueue import _call
    client = shared_client(a)
    label = (f"{want['title']} S{int(want['season']):02d}E{int(want['episode']):02d}" if a.kind == "sonarr"
             else f"{want['title']} ({want.get('year') or ''})")
    _at(label, "reading the quality profile", arr=a.name, source=source)
    try:
        return await _find(a, want, source, client, label)
    finally:
        ACTIVE.pop(label, None)


async def _find(a, want: dict, source: str, client, label: str) -> dict:
    from .arrqueue import _call
    prof = await asyncio.to_thread(_profile, lambda p: _call(a, p), a, want["profileId"])
    if a.kind == "sonarr":
        qs = [{"query": want["title"], "type": "tvsearch", "season": want["season"], "ep": want["episode"], "limit": 200},
              {"query": want["title"], "type": "tvsearch", "season": want["season"], "limit": 200}]
        if want.get("tvdbId"):
            qs.append({"query": want["title"], "type": "tvsearch", "tvdbId": want["tvdbId"], "season": want["season"], "ep": want["episode"], "limit": 200})
    else:
        qs = [{"query": f"{want['title']} {want.get('year') or ''}".strip(), "type": "movie", "limit": 200}]
        if want.get("tmdbId"):
            qs.append({"query": want["title"], "type": "movie", "tmdbId": want["tmdbId"], "limit": 200})
    t0 = time.time()
    _at(label, f"asking Prowlarr ({len(qs)} queries, every indexer)")
    raw = await _searches(qs)
    rows = _prefilter(raw, want.get("season"), want.get("episode"), a.kind)
    _at(label, f"reading {min(len(rows), PARSE_MAX)} of {len(raw)} names with {a.name}'s parser")
    cands = await _read(client, a, rows, want, prof)
    STATS["searched"] += 1
    blocked = await _blocklisted(client, a, want)
    n_blocked = sum(1 for c in cands if _norm(c["title"]) in blocked)
    cands = [c for c in cands if _norm(c["title"]) not in blocked]
    singles = sorted([c for c in cands if not c["pack"]], key=_key, reverse=True)
    packs = sorted([c for c in cands if c["pack"]], key=_key, reverse=True)
    order: list = []
    pack_why = ""
    if a.kind == "sonarr" and packs:
        files = [f for f in (await client._get("/episodefile", seriesId=want["seriesId"]) or [])
                 if int(f.get("seasonNumber") or -1) == int(want["season"])]
        for p in packs:
            pack_why = _pack_beats(p, singles[0] if singles else None, files, prof)
            if pack_why:
                order.append(p)
                break
    order += singles
    order += [p for p in packs if p not in order]
    order = _distinct(order)
    found = len(cands)
    base = {"item": label, "arr": a.name, "found": found, "raw": len(raw), "secs": round(time.time() - t0),
            "blocked": n_blocked, "source": source, "at": time.time()}
    if not order:
        why = (f"Prowlarr offered {len(raw)} name(s); none is this item in a quality the profile "
               f"'{prof['name']}' allows, above its minimum score and outside the bans"
               + (f" ({n_blocked} fit but the arr has blocklisted them)" if n_blocked else ""))
        _remember({**base, "ok": False, "why": why})
        return {"ok": False, "why": why, "found": 0, "tried": 0}
    refused = []
    for c in order[:TRY_MAX]:
        _at(label, f"handing {c['title'][:70]} to {a.name}" + (f" (try {len(refused) + 1})" if refused else ""))
        ok, why = await _push(client, a, c)
        if ok:
            kind = "season pack" if c["pack"] else "single"
            story = (f"{kind} {c['quality']} score {c['score']:,} from {c['indexer']} "
                     f"({c['size'] / 1e9:.2f} GB{', ' + str(c['seeders']) + ' seeders' if c.get('seeders') is not None else ''})"
                     + (f" - {pack_why}" if c["pack"] and pack_why else "")
                     + (f"; the arr refused {len(refused)} better-ranked name(s) first" if refused else ""))
            STATS["pushed"] += 1
            _remember({**base, "ok": True, "release": c["title"], "why": story,
                       "beat": [f"{x['quality']} {x['score']:,} {x['indexer']}" for x in order[:6] if x is not c][:5]})
            joblog.log(f"prowlarr search: {label} - pushed {c['title']} ({story})", "ok")
            return {"ok": True, "pushed": c["title"], "why": story, "found": found, "tried": len(refused) + 1}
        refused.append(f"{c['title'][:50]}: {why or 'refused'}")
    why = (f"the arr refused every name tried ({len(refused)} of {len(order)} distinct, {found} copies"
           + (f", {n_blocked} more already blocklisted" if n_blocked else "") + "): " + " | ".join(refused))
    _remember({**base, "ok": False, "why": why[:400]})
    joblog.log(f"prowlarr search: {label} - {why}"[:300], "info")
    return {"ok": False, "why": why[:400], "found": found, "tried": len(refused)}


async def replace(row: dict, reason: str = "") -> dict:
    """For refetch: the file row that was just deleted. Resolves the item and searches."""
    from .gate import get_toggle
    if not get_toggle("arrs.prowlarr_search"):
        return {"ok": False, "why": "the Prowlarr searcher is off"}
    from .config import SETTINGS
    from .arr import shared_client
    a = next((x for x in SETTINGS.arrs if x.enabled and x.name == row.get("arr_name")), None)
    if not a or not row.get("arr_parent_id"):
        return {"ok": False, "why": "no arr for this file"}
    client = shared_client(a)
    try:
        if a.kind == "sonarr":
            s = await client._get(f"/series/{int(row['arr_parent_id'])}")
            if row.get("season") is None or not str(row.get("episode") or "").strip():
                return {"ok": False, "why": "no season/episode on the file row"}
            ep_no = int(re.match(r"\d+", str(row["episode"])).group(0))
            eps = await client._get("/episode", seriesId=s["id"])
            e = next((x for x in eps if int(x.get("seasonNumber") or -1) == int(row["season"])
                      and int(x.get("episodeNumber") or -1) == ep_no), None)
            if not e:
                return {"ok": False, "why": f"S{int(row['season']):02d}E{ep_no:02d} is not in {a.name}"}
            want = {"seriesId": s["id"], "episodeId": e["id"], "season": int(row["season"]), "episode": ep_no,
                    "title": s["title"], "tvdbId": s.get("tvdbId"), "profileId": s["qualityProfileId"]}
        else:
            m = await client._get(f"/movie/{int(row['arr_parent_id'])}")
            want = {"movieId": m["id"], "title": m["title"], "year": m.get("year"), "tmdbId": m.get("tmdbId"),
                    "profileId": m["qualityProfileId"]}
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    return await find(a, want, source=f"refetch: {reason}"[:120] if reason else "refetch")


# ------------------------------------------------------------ the sweep ---
async def sweep(limit: int = SWEEP_MAX) -> str:
    """The arrs' wanted lists: items their own searches have not filled."""
    from .config import SETTINGS
    from .arr import shared_client
    done, pushed, lines = 0, 0, []
    SWEEP.clear()
    SWEEP.update(i=0, n=limit, n_total=limit, started=time.time(), step="reading the arrs' wanted lists")
    for a in [x for x in SETTINGS.arrs if x.enabled and x.kind in ("sonarr", "radarr")]:
        client = shared_client(a)
        try:
            if a.kind == "sonarr":
                w = await client._get("/wanted/missing", pageSize=limit, monitored="true", includeSeries="true",
                                      sortKey="airDateUtc", sortDirection="descending")
                items = [{"seriesId": e["seriesId"], "episodeId": e["id"], "season": e["seasonNumber"],
                          "episode": e["episodeNumber"], "title": e["series"]["title"], "tvdbId": e["series"].get("tvdbId"),
                          "profileId": e["series"]["qualityProfileId"]} for e in w.get("records") or []
                         if e.get("series") and e.get("airDateUtc") and e["airDateUtc"] < time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())]
            else:
                w = await client._get("/wanted/missing", pageSize=limit, monitored="true",
                                      sortKey="movieMetadata.sortTitle")
                items = [{"movieId": m["id"], "title": m["title"], "year": m.get("year"), "tmdbId": m.get("tmdbId"),
                          "profileId": m["qualityProfileId"]} for m in w.get("records") or []
                         # released, not merely "available" by the profile's minimum: a film
                         # still in cinemas has only fakes on offer, and a 2027 title has worse
                         if m.get("status") == "released" and m.get("isAvailable", True)]
        except Exception as e:                                   # noqa: BLE001
            lines.append(f"{a.name}: could not read the wanted list: {type(e).__name__}: {e}"[:160])
            continue
        # ALREADY ON ITS WAY. An item with a download in the arr's queue is
        # the arr's (or the janitor's) to finish; searching it again would
        # only add a second download for the same thing.
        try:
            q = await client._get("/queue", pageSize=500)
            key = "episodeId" if a.kind == "sonarr" else "movieId"
            inq = {r.get(key) for r in (q.get("records") or [])}
            items = [w for w in items if w.get(key) not in inq]
        except Exception:                                        # noqa: BLE001
            pass
        for want in items[:limit]:
            if done >= limit:
                break
            done += 1
            SWEEP.update(i=done, n=min(limit, SWEEP.get("n_total") or limit), arr=a.name)
            try:
                r = await find(a, want, source="wanted sweep")
            except Exception as e:                               # noqa: BLE001
                lines.append(f"{a.name}: {want.get('title', '')[:40]}: {type(e).__name__}: {e}"[:160])
                continue
            if r.get("ok"):
                pushed += 1
    STATS["detail"] = lines + STATS["detail"][-10:]
    return f"{done} wanted item(s) searched, {pushed} handed to the arrs" if done else "nothing wanted that has aired"


async def run() -> str:
    if STATS["running"]:
        return STATS["last_result"]
    STATS["running"] = True
    STATS["detail"] = []
    try:
        res = await sweep()
    finally:
        STATS["running"] = False
        SWEEP.clear()
    STATS["last_run"] = time.time()
    STATS["last_result"] = res
    joblog.log("prowlarr search sweep: " + res, "ok")
    return res


async def watch() -> None:
    from .gate import get_toggle
    STATS["next_run"] = time.time() + 1800
    await asyncio.sleep(1800)            # never compete with startup
    while True:
        try:
            if get_toggle("arrs.prowlarr_search"):
                await run()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"prowlarr search: {type(e).__name__}: {e}", "error")
        STATS["next_run"] = time.time() + SWEEP_EVERY_S
        await asyncio.sleep(SWEEP_EVERY_S)


# ------------------------------------------------------------ the card ----
def _recent() -> list:
    try:
        return json.loads(kv_get(KV_RECENT) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _remember(row: dict) -> None:
    try:
        rows = [row] + _recent()
        kv_set(KV_RECENT, json.dumps(rows[:RECENT_KEEP]))
    except Exception:                                            # noqa: BLE001
        pass


def start() -> str:
    """Run now from the page: the sweep goes in the background (it takes
    minutes) and the card follows it through snapshot()."""
    if STATS["running"]:
        return "already running"
    asyncio.get_running_loop().create_task(run())     # run() marks itself running on its first step
    return "started"


def snapshot() -> dict:
    return {"stats": {k: v for k, v in STATS.items() if k != "detail"} | {"detail": STATS["detail"][:12]},
            "recent": _recent()[:RECENT_KEEP],
            "active": [dict(v) for v in ACTIVE.values()],
            "sweep": dict(SWEEP), "now": time.time()}
