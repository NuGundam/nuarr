r"""Which quality profile a series belongs on, decided from what is released.

THE QUESTION. A show that is released in 4K belongs on the 2160p profile; one
that only ever appears in 1080p belongs on the 1080p one. Nothing in TheTVDB,
TMDB or Sonarr records which a show is, so it used to be set by hand when a
series was added and never looked at again (Erik, 2026-11-14: The Boys with 31
Blu-ray 1080p files on a 4K profile, The Punisher on the Anime profile).

WHY NOT "EVERYTHING ON 4K, SHIFT DOWN LATER". On Nu 2160p Efficient a plain
1080p release scores under the profile's minimum - 61 of WWE SmackDown's 62
files would. A 1080p-only show parked there grabs NOTHING until it is moved,
so the decision has to be made from evidence, early, not waited out.

THE EVIDENCE is Sonarr's own release search for an episode that has already
aired (GET /release?episodeId=): every indexer's answer, already parsed. A
2160p release from a web or disc source, from two or more groups, says the
show exists in 4K. The search costs about a minute and a query on every
indexer, so it is rationed: PER_HOUR series an hour, one per tick.

    on the 4K profile:   the latest aired episode has 4K           -> stays
                         the last DOWN_EPS aired episodes have
                         1080p releases and no 4K                  -> 1080p
    anywhere else:       the latest aired episode has no 4K        -> 1080p
                         the last UP_EPS aired episodes all have 4K -> 4K
    nothing aired yet, or too few releases to say                  -> left alone

Going up needs more episodes than going down because going up re-downloads
the show.

WHAT A MOVE COSTS is counted before it is made: the series' own files are
scored against the target profile, and the ones that would fall under its
minimum are the ones Sonarr would then replace. A move that puts more than
MAX_BELOW files there is never made unasked - it waits for a click.

Nothing reaches Sonarr until kv profadv.cfg says armed; before that the pass
only fills the table and the Arrs page shows what it would do.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import joblog
from .config import SETTINGS
from .db import cursor, kv_get, kv_set

KV = "profadv.cfg"
TICK_S = 600
SEP = chr(92)
DEFAULTS = {
    "armed": False,
    "per_hour": 4,                 # series looked up per hour (each is 1-3 release searches)
    "max_below": 3,                # a move that puts more files than this under the minimum waits for a click
    "wait_h": 36,                  # an episode counts once it aired this long ago
    "down_eps": 2, "up_eps": 3,
    "recheck_d": 14, "recheck_ended_d": 120,
    # per root folder: the 4K profile, the 1080p one, and others that are fine as the 1080p answer
    "roots": {
        "TV Shows": {"hi": "Nu 2160p Efficient", "lo": "Nu 1080p Efficient", "lo_ok": ["Nu 1080p Quality HDR"]},
        "Animated Shows": {"hi": "Nu 2160p Animation", "lo": "Nu 1080p Quality HDR Animation",
                           "lo_ok": ["Nu 1080p Remux Animation"]},
    },
}
MIN_4K_GROUPS = 2                  # distinct release groups with a 2160p web/disc release
MIN_1080 = 3                       # distinct 1080p releases before "no 4K" means anything
REAL_SOURCES = ("web", "webRip", "bluray", "blurayRaw")
STATE: dict = {"running": False, "last": "", "at": 0.0, "checked": [], "err": ""}
_CLIENT: dict = {}


def cfg() -> dict:
    try:
        d = json.loads(kv_get(KV) or "{}")
    except Exception:                                            # noqa: BLE001
        d = {}
    c = {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    if not isinstance(c.get("roots"), dict) or not c["roots"]:
        c["roots"] = DEFAULTS["roots"]
    return c


def set_cfg(**kw) -> dict:
    c = cfg()
    for k, v in kw.items():
        if v is None or k not in DEFAULTS or k == "roots":
            continue
        if k == "armed":
            c[k] = bool(v)
        elif k == "per_hour":
            c[k] = max(1, min(30, int(v)))
        elif k == "max_below":
            c[k] = max(0, min(500, int(v)))
        else:
            c[k] = max(1, int(v))
    kv_set(KV, json.dumps(c))
    return c


def init() -> None:
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS profadv(
            series_id INTEGER PRIMARY KEY, title TEXT, root TEXT, profile TEXT, status TEXT,
            verdict TEXT, want TEXT, evidence TEXT, impact TEXT, note TEXT,
            checked_at REAL, seasons INTEGER, applied_at REAL, applied_from TEXT)""")


def _raw_client():
    """Sonarr with a long timeout: a release search asks every indexer."""
    from .arr import ArrClient
    scfg = next((a for a in SETTINGS.arrs if a.kind == "sonarr" and a.enabled), None)
    if scfg is None:
        return None
    key = (scfg.api, scfg.api_key)
    c = _CLIENT.get("c")
    if c is None or _CLIENT.get("key") != key or c._client.is_closed:
        c = ArrClient(scfg, timeout=300.0)
        _CLIENT.update(c=c, key=key)
    return c


class _Retry:
    """Sonarr drops a kept-alive connection while a minute-long search runs
    on another; the next request on it fails once. Ask again on a new one."""

    def __init__(self, c):
        self._c = c

    async def _do(self, name, *a, **kw):
        import httpx
        try:
            return await getattr(self._c, name)(*a, **kw)
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.WriteError):
            try:
                await self._c.close()
            except Exception:                                    # noqa: BLE001
                pass
            _CLIENT.pop("c", None)
            self._c = _raw_client()
            return await getattr(self._c, name)(*a, **kw)

    async def _get(self, *a, **kw):
        return await self._do("_get", *a, **kw)

    async def _put(self, *a, **kw):
        return await self._do("_put", *a, **kw)

    async def _post(self, *a, **kw):
        return await self._do("_post", *a, **kw)

    async def close(self):
        await self._c.close()


def _client():
    c = _raw_client()
    return _Retry(c) if c is not None else None


def _root(s: dict) -> str:
    p = (s.get("path") or "").split(SEP)
    return p[1] if len(p) > 2 else ""


def _allowed(p: dict) -> set:
    out = set()
    for it in p.get("items") or []:
        if it.get("allowed"):
            if it.get("quality"):
                out.add(it["quality"]["name"])
            for q in it.get("items") or []:
                out.add(q["quality"]["name"])
    return out


def _impact(efs: list, p: dict) -> dict:
    """The series' own files against a profile: how many Sonarr would want replaced."""
    sc = {x["name"]: x["score"] for x in p.get("formatItems") or []}
    al = _allowed(p)
    scores = [sum(sc.get(c["name"], 0) for c in e.get("customFormats") or []) for e in efs]
    return {"files": len(efs),
            "below_min": sum(1 for x in scores if x < (p.get("minFormatScore") or 0)),
            "not_allowed": sum(1 for e in efs if e["quality"]["quality"]["name"] not in al)}


def _count(rel: list) -> dict:
    """Distinct releases of one episode by resolution. Season packs and
    upscaled TV captures are not evidence of a 4K show."""
    g4, t4, t1 = set(), set(), set()
    for r in rel:
        q = (r.get("quality") or {}).get("quality") or {}
        if r.get("fullSeason") or not r.get("episodeRequested", True):
            continue
        res, src = int(q.get("resolution") or 0), q.get("source")
        title = (r.get("title") or "").lower()
        if res == 2160 and src in REAL_SOURCES:
            t4.add(title)
            g4.add((r.get("releaseGroup") or "").lower() or title)
        elif res == 1080:
            t1.add(title)
    return {"n4": len(t4), "g4": len(g4), "n1080": len(t1), "all": len(rel)}


async def check(series_id: int) -> dict:
    """Look one series up and write its row. Changes nothing in Sonarr."""
    init()
    c = _client()
    if c is None:
        return {"ok": False, "why": "no Sonarr"}
    conf = cfg()
    s = await c._get(f"/series/{int(series_id)}")
    profs = {p["id"]: p for p in await c._get("/qualityprofile")}
    by_name = {p["name"]: p for p in profs.values()}
    root = _root(s)
    rc = conf["roots"].get(root)
    now_name = (profs.get(s.get("qualityProfileId")) or {}).get("name") or "?"
    row = {"series_id": s["id"], "title": s.get("title") or "", "root": root, "profile": now_name, "status": "skip",
           "verdict": "", "want": "", "evidence": [], "impact": {}, "note": "",
           "seasons": int((s.get("statistics") or {}).get("seasonCount") or 0)}
    if not rc or rc["hi"] not in by_name or rc["lo"] not in by_name:
        row["note"] = "this folder has no pair of profiles to choose between"
        return _save(row)
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - conf["wait_h"] * 3600))
    eps = [e for e in await c._get("/episode", seriesId=s["id"])
           if e.get("seasonNumber") and e.get("airDateUtc") and e["airDateUtc"] < cutoff]
    eps.sort(key=lambda e: e["airDateUtc"], reverse=True)
    if not eps:
        row.update(status="unknown", verdict="unknown", note="nothing has aired yet")
        return _save(row)
    on_hi = now_name == rc["hi"]
    need = min(len(eps), conf["down_eps"] if on_hi else conf["up_eps"])
    ev = []
    for e in eps[:need]:
        rel = await c._get("/release", episodeId=e["id"])
        k = _count(rel)
        k.update(ep=f"S{e['seasonNumber']:02}E{e['episodeNumber']:02}", air=e["airDateUtc"][:10])
        ev.append(k)
        has4 = k["g4"] >= MIN_4K_GROUPS
        # one answer is enough when it agrees with where the series already is
        if on_hi and has4:
            break
        if not on_hi and not has4:
            break
    row["evidence"] = ev
    with4 = [k for k in ev if k["g4"] >= MIN_4K_GROUPS]
    no4 = [k for k in ev if k["n4"] == 0 and k["n1080"] >= MIN_1080]
    if on_hi:
        verdict = "4k" if with4 else "1080" if len(no4) == len(ev) == need else "unknown"
    else:
        verdict = "4k" if len(with4) == len(ev) == need and need >= min(conf["up_eps"], len(eps)) else \
                  "1080" if no4 and not with4 else "unknown"
    row["verdict"] = verdict
    if verdict == "unknown":
        row.update(status="unknown", note="too few releases to say, or the episodes disagree")
        return _save(row)
    want = rc["hi"] if verdict == "4k" else rc["lo"]
    if now_name == want or (verdict == "1080" and now_name in (rc.get("lo_ok") or [])):
        row.update(status="ok", want=now_name)
        return _save(row)
    efs = await c._get("/episodefile", seriesId=s["id"])
    imp = _impact(efs, by_name[want])
    row.update(want=want, impact=imp,
               status="needs_ok" if imp["below_min"] > conf["max_below"] else "would")
    if row["status"] == "needs_ok":
        row["note"] = (f"{imp['below_min']} of {imp['files']} files would fall under the minimum of {want} "
                       f"and Sonarr would look to replace them")
    saved = _save(row)
    if conf["armed"] and row["status"] == "would":
        return await apply(s["id"], auto=True)
    return saved


def _save(row: dict) -> dict:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO profadv(series_id,title,root,profile,status,verdict,want,evidence,impact,note,checked_at,seasons) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(series_id) DO UPDATE SET title=excluded.title, "
            "root=excluded.root, profile=excluded.profile, status=excluded.status, verdict=excluded.verdict, "
            "want=excluded.want, evidence=excluded.evidence, impact=excluded.impact, note=excluded.note, "
            "checked_at=excluded.checked_at, seasons=excluded.seasons",
            (row["series_id"], row["title"], row["root"], row["profile"], row["status"], row["verdict"], row["want"],
             json.dumps(row["evidence"]), json.dumps(row["impact"]), row["note"], time.time(), row["seasons"]))
    return {"ok": True, **row}


async def apply(series_id: int, auto: bool = False) -> dict:
    """Move one series to the profile its row asks for."""
    init()
    c = _client()
    with cursor() as cur:
        r = cur.execute("SELECT * FROM profadv WHERE series_id=?", (int(series_id),)).fetchone()
    if c is None or not r or r["status"] not in ("would", "needs_ok") or not r["want"]:
        return {"ok": False, "why": "nothing to apply for this series"}
    by_name = {p["name"]: p for p in await c._get("/qualityprofile")}
    if r["want"] not in by_name:
        return {"ok": False, "why": f"Sonarr has no profile called {r['want']}"}
    s = await c._get(f"/series/{int(series_id)}")
    old = s.get("qualityProfileId")
    s["qualityProfileId"] = by_name[r["want"]]["id"]
    await c._put(f"/series/{int(series_id)}", s)
    # THE EPISODES IT MISSED WHILE ON THE WRONG PROFILE: aired in the last
    # two weeks, monitored, no file. Only those - a search of the whole
    # series would go looking for upgrades nobody asked for.
    searched = 0
    try:
        lo = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 14 * 86400))
        hi = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        miss = [e["id"] for e in await c._get("/episode", seriesId=int(series_id))
                if e.get("monitored") and not e.get("hasFile") and lo < (e.get("airDateUtc") or "") < hi]
        if miss:
            await c._post("/command", {"name": "EpisodeSearch", "episodeIds": miss})
            searched = len(miss)
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"profile advisor: could not search the missed episodes of {r['title']}: {e}", "warn")
    with cursor() as cur:
        cur.execute("UPDATE profadv SET status='moved', profile=?, applied_at=?, applied_from=?, note=? "
                    "WHERE series_id=?",
                    (r["want"], time.time(), r["profile"],
                     (f"searched {searched} missed episode(s)" if searched else ""), int(series_id)))
    joblog.log(f"profile advisor: {r['title']} moved from {r['profile']} to {r['want']}"
               + (" (armed)" if auto else " (by hand)")
               + (f"; searching {searched} missed episode(s)" if searched else ""), "ok")
    return {"ok": True, "series_id": int(series_id), "from": r["profile"], "to": r["want"],
            "old_profile_id": old, "searched": searched}


async def _next() -> int | None:
    """The series most worth a look now: never seen, then a new season, then stale."""
    c = _client()
    if c is None:
        return None
    conf = cfg()
    series = await c._get("/series")
    with cursor() as cur:
        have = {int(r["series_id"]): dict(r) for r in cur.execute(
            "SELECT series_id, checked_at, seasons, status, verdict FROM profadv")}
    now = time.time()
    best, best_key = None, None
    for s in series:
        if _root(s) not in conf["roots"]:
            continue
        h = have.get(int(s["id"]))
        cont = (s.get("status") or "") == "continuing"
        seasons = int((s.get("statistics") or {}).get("seasonCount") or 0)
        if h is None:
            key = (0, 0 if cont else 1, -int(s["id"]))
        else:
            age = now - float(h["checked_at"] or 0)
            if cont and seasons > int(h["seasons"] or 0) and age > 86400:
                key = (1, age and -age, 0)
            elif h["status"] == "unknown" and cont and age > 3 * 86400:
                key = (2, -age, 0)
            elif age > (conf["recheck_d"] if cont else conf["recheck_ended_d"]) * 86400:
                key = (3, -age, 0)
            else:
                continue
        if best_key is None or key < best_key:
            best, best_key = int(s["id"]), key
    return best


async def run_one() -> dict:
    if STATE["running"]:
        return {"ok": False, "why": "already looking one up"}
    STATE["running"] = True
    try:
        sid = await _next()
        if sid is None:
            STATE["last"] = "every series is up to date"
            return {"ok": True, "checked": None}
        r = await check(sid)
        STATE["checked"] = [t for t in STATE["checked"] if time.time() - t < 3600] + [time.time()]
        STATE.update(last=f"{r.get('title')}: {r.get('verdict') or r.get('status')}", at=time.time(), err="")
        return r
    except Exception as e:                                       # noqa: BLE001
        STATE["err"] = f"{type(e).__name__}: {e}"[:200]
        return {"ok": False, "why": STATE["err"]}
    finally:
        STATE["running"] = False


def snapshot() -> dict:
    init()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute("SELECT * FROM profadv ORDER BY checked_at DESC")]
    for r in rows:
        for k in ("evidence", "impact"):
            try:
                r[k] = json.loads(r[k] or "null")
            except Exception:                                    # noqa: BLE001
                r[k] = None
    counts: dict = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    show = [r for r in rows if r["status"] in ("would", "needs_ok", "moved")]
    return {"cfg": cfg(), "counts": counts, "seen": len(rows), "total": STATE.get("total"),
            "rows": show, "recent": rows[:12], "running": STATE["running"], "last": STATE["last"],
            "last_at": STATE["at"], "err": STATE["err"]}


async def watch() -> None:
    from . import schedules
    schedules.register("profadvisor", "Profile advisor", "Arrs", TICK_S,
                       what="Looks up what a series is released in and says which quality profile it belongs on.")
    await asyncio.sleep(240)
    while True:
        try:
            schedules.beat("profadvisor")
            conf = cfg()
            c = _client()
            if c is not None:
                try:
                    STATE["total"] = sum(1 for s in await c._get("/series") if _root(s) in conf["roots"])
                except Exception:                                # noqa: BLE001
                    pass
            recent = [t for t in STATE["checked"] if time.time() - t < 3600]
            if len(recent) < conf["per_hour"]:
                await run_one()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"profile advisor: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(TICK_S)
