r"""Put a file back on its episode when the arr has lost track of it.

THE PROBLEM, IN THE WORDS OF SOMEBODY WHO HIT IT WITH TDARR FIRST
-----------------------------------------------------------------
"if you deleted the daily episode and did a scan which shows it as removed,
then put the file back, the scan wouldn't find it - you had to do it from the
Manage Episodes function."

That is exactly right, and it is why the Tdarr flow for WWE replaced the file
and notified Sonarr, rather than deleting, notifying, replacing and notifying
again. For an air-by-date series a plain disk scan will not re-attach a file
whose episodeFile record has been deleted; only a manual import will. Sonarr
happily lists the file as an import candidate - all 48 SmackDown files map to
an episode by air date - it just will not do it on its own.

The consequence when it goes wrong is not a cosmetic gap. Sonarr sees the
episode as having no file, searches, grabs it again, imports it, and nuarr
processes the new copy - which is the five-cycle loop in SmackDown's history
on 2026-04-11, the same episode downloaded five times in one morning.

WHAT THIS DOES
--------------
After nuarr has rewritten a file, confirm the arr still owns something at that
path. If it does not, re-attach it the only way that works: ask for the manual
import candidates for that folder and issue a ManualImport command for the
episode the arr itself matched.

It never guesses a match. If the arr cannot map the file to an episode it is
reported and left alone - a wrong attachment is worse than a missing one,
because it hides the problem behind a file on the wrong episode.
"""
from __future__ import annotations

import asyncio
import os
import time

from .arr import shared_client
from .config import SETTINGS
from .db import cursor
from . import joblog

STATS: dict = {"checked": 0, "reattached": 0, "unmatched": 0, "last_run": 0.0}


def _cfg(name: str):
    return next((c for c in SETTINGS.arrs
                 if c.name == name and c.enabled and c.api_key), None)


async def arr_has_file(cfg, parent_id: int, path: str) -> bool:
    """Does the arr hold an episodeFile/movieFile at exactly this path?"""
    try:
        if cfg.kind == "sonarr":
            files = await shared_client(cfg)._get("/episodefile",
                                                  seriesId=int(parent_id))
        else:
            files = await shared_client(cfg)._get("/moviefile",
                                                  movieId=int(parent_id))
    except Exception:
        return True          # cannot tell - assume fine rather than act on noise
    want = os.path.normcase(os.path.normpath(path))
    return any(os.path.normcase(os.path.normpath(f.get("path") or "")) == want
               for f in files or [])


async def reattach(cfg, parent_id: int, path: str) -> tuple[bool, str]:
    """Attach an existing file to its episode via ManualImport."""
    c = shared_client(cfg)
    folder = os.path.dirname(path)
    want = os.path.normcase(os.path.normpath(path))
    try:
        if cfg.kind == "sonarr":
            cands = await c._get("/manualimport", folder=folder,
                                 seriesId=int(parent_id),
                                 filterExistingFiles="false")
        else:
            cands = await c._get("/manualimport", folder=folder,
                                 movieId=int(parent_id),
                                 filterExistingFiles="false")
    except Exception as e:
        return False, f"could not list import candidates: {type(e).__name__}"

    me = next((x for x in (cands or [])
               if os.path.normcase(os.path.normpath(x.get("path") or "")) == want),
              None)
    if not me:
        return False, "the arr does not offer this file as an import candidate"

    if cfg.kind == "sonarr":
        eps = [e.get("id") for e in (me.get("episodes") or []) if e.get("id")]
        if not eps:
            # Deliberately not guessed. An air date the arr could not place is
            # a real question - a special, a date the provider has wrong - and
            # attaching it to the nearest episode would bury that.
            return False, "the arr could not match this file to an episode"
        payload = [{
            "path": me["path"],
            "seriesId": int(parent_id),
            "episodeIds": eps,
            "quality": me.get("quality"),
            "languages": me.get("languages"),
            "releaseGroup": me.get("releaseGroup") or "",
            "indexerFlags": me.get("indexerFlags") or 0,
        }]
    else:
        payload = [{
            "path": me["path"],
            "movieId": int(parent_id),
            "quality": me.get("quality"),
            "languages": me.get("languages"),
            "releaseGroup": me.get("releaseGroup") or "",
            "indexerFlags": me.get("indexerFlags") or 0,
        }]
    try:
        # importMode "Move" with source and destination identical is how the
        # arr's own UI re-attaches a file already sitting in the library. It
        # does not copy anything; the file does not move.
        await c._post("/command", {"name": "ManualImport", "files": payload,
                                   "importMode": "Move"})
    except Exception as e:
        return False, f"ManualImport refused: {type(e).__name__}: {e}"
    return True, "re-attached by manual import"


async def check_file(file_id: int) -> tuple[bool, str]:
    """One file: does its arr still own it, and re-attach if not."""
    with cursor() as cur:
        r = cur.execute("SELECT path, arr_name, arr_parent_id, title "
                        "FROM files WHERE id=?", (file_id,)).fetchone()
    if not r or not r["arr_name"] or not r["arr_parent_id"]:
        return True, "not managed by an arr"
    if not os.path.exists(r["path"]):
        return True, "file is not on disk"
    cfg = _cfg(r["arr_name"])
    if not cfg:
        return True, "arr not configured"
    STATS["checked"] += 1
    if await arr_has_file(cfg, r["arr_parent_id"], r["path"]):
        return True, "the arr still has it"
    ok, why = await reattach(cfg, r["arr_parent_id"], r["path"])
    if ok:
        STATS["reattached"] += 1
        joblog.log(f"{cfg.name} had lost {os.path.basename(r['path'])[:60]} — "
                   f"{why}. A rescan would not have found it; that is the "
                   f"air-by-date behaviour that makes an episode look missing "
                   f"and get downloaded again.", "warn")
    else:
        STATS["unmatched"] += 1
        joblog.log(f"{cfg.name} has no file for "
                   f"{os.path.basename(r['path'])[:60]} and it could not be "
                   f"re-attached: {why}", "error")
    return ok, why


async def sweep(limit: int = 400) -> dict:
    """Check recently-processed files, newest first."""
    since = time.time() - 7 * 86400
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT DISTINCT f.id FROM files f JOIN jobs j ON j.file_id=f.id "
            " WHERE j.finished_at > ? AND f.state='done' "
            "   AND f.arr_file_id IS NOT NULL "
            " ORDER BY j.finished_at DESC LIMIT ?", (since, limit))]
    fixed = missed = 0
    for r in rows:
        try:
            ok, _ = await check_file(r["id"])
        except Exception:
            continue
        if ok is False:
            missed += 1
        elif STATS["reattached"]:
            pass
    STATS["last_run"] = time.time()
    return {"looked_at": len(rows), "reattached": STATS["reattached"],
            "unmatched": STATS["unmatched"]}
