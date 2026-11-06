r"""nuarr - is this film as long as the arr says it is?

The integrity check asks whether a file decodes for as long as its own header
claims. Macross II: Lovers Again (2026-11-05) decodes perfectly for all of its
25 minutes - of a 137-minute film. The header and the bytes agree with each
other, so nothing that only reads the file can notice. Only the outside
figure can: Radarr's runtime.

FILMS ONLY. A series has one runtime for episodes that vary, so a double
episode or a short special would read as a fault here.

LISTED, NEVER ACTED ON. A short film can also be a wrong listing or a real
short cut, and the figure is the arr's and not a measurement. file/runtime can
be replaced by a person and never by a sweep; Keep says this one is right, and
holds until the file's size changes.
"""
from __future__ import annotations

import asyncio
import time

from . import joblog
from .config import SETTINGS
from .db import cursor

SHARE = 0.6              # under this share of the listed runtime is a finding
MIN_LISTED_MIN = 30      # a listed short cannot be told from a bad listing
EVERY_S = 6 * 3600
KIND = "file/runtime"
_READY = False


def init() -> None:
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS runtime_short(
                file_id INTEGER PRIMARY KEY,
                path    TEXT,
                title   TEXT,
                size    INTEGER,
                listed  REAL,
                actual  REAL,
                at      REAL,
                kept    INTEGER DEFAULT 0
            )""")
    _READY = True


def judge(listed_min: float, actual_s: float) -> bool:
    return listed_min >= MIN_LISTED_MIN and 0 < actual_s / 60 < listed_min * SHARE


def _apply(found: dict) -> int:
    """Write this pass's films. A row whose file changed is asked about again."""
    new = 0
    with cursor() as cur:
        old = {r["file_id"]: r for r in cur.execute("SELECT * FROM runtime_short")}
        for fid in set(old) - set(found):
            cur.execute("DELETE FROM runtime_short WHERE file_id=?", (fid,))
        for fid, x in found.items():
            o = old.get(fid)
            kept = int(o["kept"] or 0) if o and int(o["size"] or 0) == x["size"] else 0
            new += 0 if o else 1
            cur.execute(
                "INSERT OR REPLACE INTO runtime_short(file_id,path,title,size,listed,"
                "actual,at,kept) VALUES(?,?,?,?,?,?,?,?)",
                (fid, x["path"], x["title"], x["size"], x["listed"], x["actual"],
                 o["at"] if o else time.time(), kept))
    return new


async def sweep() -> dict:
    from .arr import shared_client
    if not _READY:
        init()
    found: dict = {}
    for cfg in SETTINGS.arrs:
        if not (cfg.enabled and cfg.api_key and cfg.kind == "radarr"):
            continue
        movies = await shared_client(cfg)._get("/movie")
        listed = {(m.get("movieFile") or {}).get("id"): m for m in movies or []
                  if m.get("movieFile")}
        with cursor() as cur:
            rows = [dict(r) for r in cur.execute(
                "SELECT id, arr_file_id, path, size, duration FROM files "
                " WHERE arr_name=? AND arr_file_id IS NOT NULL AND duration>0 "
                "   AND state NOT IN ('deleted','duplicate')", (cfg.name,))]
        for r in rows:
            m = listed.get(r["arr_file_id"])
            if m and judge(float(m.get("runtime") or 0), float(r["duration"])):
                found[int(r["id"])] = {
                    "path": r["path"], "size": int(r["size"] or 0),
                    "title": f"{m.get('title')} ({m.get('year')})",
                    "listed": float(m["runtime"]),
                    "actual": round(float(r["duration"]) / 60, 1)}
    new = await asyncio.to_thread(_apply, found)
    if new:
        joblog.log(f"runtime check: {new} film(s) far shorter than listed", "warn")
    return {"found": len(found), "new": new}


def findings(limit: int = 200) -> list[dict]:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return [{"file_id": r["file_id"], "kind": KIND, "path": r["path"],
                     "title": r["title"], "at": r["at"], "keep": True,
                     "why": f"runs {r['actual']:.0f} min, Radarr lists "
                            f"{r['listed']:.0f} - cut short, the wrong file, "
                            f"or a wrong listing"}
                    for r in cur.execute(
                        "SELECT n.* FROM runtime_short n JOIN files f ON f.id=n.file_id "
                        " WHERE COALESCE(n.kept,0)=0 "
                        "   AND f.state NOT IN ('deleted','duplicate') "
                        " ORDER BY n.actual / n.listed LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def keep(file_id: int) -> bool:
    if not _READY:
        init()
    with cursor() as cur:
        n = cur.execute("UPDATE runtime_short SET kept=1 WHERE file_id=?",
                        (int(file_id),)).rowcount
    return bool(n)


async def watch() -> None:
    from . import schedules
    schedules.register(
        "runtimecheck", "Films shorter than listed", "Library", EVERY_S,
        what="Compares each film's length with Radarr's runtime and lists any "
             "under 60 % of it. Lists only - a person decides.")
    await asyncio.sleep(240)
    while True:
        schedules.beat("runtimecheck")
        try:
            await sweep()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"runtime check: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(EVERY_S)
