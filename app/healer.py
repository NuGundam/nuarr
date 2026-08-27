r"""
nuarr - self-healing for "missing" files

WHY THIS EXISTS
---------------
verify_missing() flags a file when THE ARR STILL TRACKS A FILE RECORD AND THAT
PATH IS NOT ON DISK. That is the right definition, but it is a snapshot, and
most snapshots that look alarming are simply stale:

  * Sonarr renamed the file seconds ago and its record has not caught up
  * an upgrade replaced it and the old record is still in the index
  * DrivePool relocated it between PoolParts mid-scan
  * nuarr's own commit had the file open under a .nuarr-new sibling

All four resolve themselves within a minute or two, yet each one used to land
in the Missing tile as though a disk had died - training you to ignore the tile,
which is the worst outcome for a number whose whole job is to be believed.

WHAT IT DOES
------------
For every file marked missing, up to MAX_ATTEMPTS times:

  1. Ask the arr to refresh the PARENT - RefreshSeries for Sonarr, RefreshMovie
     for Radarr - so the arr re-reads the folder and updates its own record.
  2. Wait for the arr to finish that command.
  3. Re-check the ONE file: fetch its current record from the arr and test the
     path. Targeted, not a library scan - a full pass costs ~100 s and would
     make healing three files an all-afternoon job.
  4. Resolve it if the path is back, if the arr moved the record to a new path,
     or if the arr has dropped the record entirely.

Only after MAX_ATTEMPTS failures is a file marked 'confirmed' and shown as
genuinely missing. Until then it sits in 'checking' and is kept out of the
Missing count.

WHY THE ARR IS THE AUTHORITY, NOT THE DISK
-------------------------------------------
It would be simpler to just re-stat the path. That would miss the most common
case: the file is fine and has simply MOVED, so the old path is gone forever
and no number of re-stats will ever succeed. Refreshing the arr and re-reading
its record catches the move and adopts the new path.
"""
from __future__ import annotations

import asyncio
import os
import time

from . import joblog
from .config import SETTINGS
from . import schedules
from .db import cursor

MAX_ATTEMPTS = 3

# Wait between attempts on the same file. A refresh is not instant and the arr
# needs a moment to rescan the folder; retrying immediately would just spend all
# three attempts inside the same stale window and confirm a healthy file as
# missing. Grows so a genuinely absent file stops being asked about.
BACKOFF_S = [90, 300, 900]

POLL_S = 120                 # how often the healer sweeps
_CMD_TIMEOUT_S = 90          # how long to wait for a refresh command

# next_run/current/total/done exist so the dashboard can show the same thing
# for this sweep as for the unmanaged one: a live indicator while it is working
# and, when it is not, when it will next look. A timer nobody can see is
# indistinguishable from a timer that has stopped.
STATS: dict = {"last_run": 0.0, "next_run": 0.0, "checked": 0, "healed": 0,
               "confirmed": 0, "running": False,
               "current": None, "total": 0, "done": 0}


def _client(arr_name: str):
    from .arr import ArrClient
    cfg = next((a for a in SETTINGS.arrs
                if a.name == arr_name and a.enabled and a.api_key), None)
    return ArrClient(cfg) if cfg else None


async def _refresh_parent(c, parent_id: int | None) -> bool:
    """Re-read the series/movie folder so the arr updates its own record.

    Refresh, not Rescan. Rescan only re-reads the disk; Refresh also re-checks
    the metadata and reconciles the file records against what it finds, which
    is what actually clears a stale entry. Radarr takes movieIds as a LIST,
    Sonarr takes a single seriesId - passing the wrong shape is accepted and
    silently does nothing.
    """
    if not parent_id:
        return False
    sonarr = (c.cfg.kind or "").lower() == "sonarr"
    payload = ({"name": "RefreshSeries", "seriesId": int(parent_id)} if sonarr
               else {"name": "RefreshMovie", "movieIds": [int(parent_id)]})
    try:
        r = await c._post("/command", payload)
    except Exception:
        return False
    cmd_id = (r or {}).get("id")
    if not cmd_id:
        return False
    ok, _status = await c.wait_command(cmd_id, timeout_s=_CMD_TIMEOUT_S, poll_s=3)
    return ok


async def _current_path(c, file_id: int) -> tuple[str | None, bool]:
    """The arr's CURRENT path for this file record.

    Returns (path, still_tracked). still_tracked=False means the arr has
    dropped the record, which RESOLVES the problem - nothing is missing if
    nothing claims to have it.

    Deliberately not using ArrClient.file_record(), which swallows every
    exception and returns None. That would make a network blip indistinguishable
    from a genuine 404, and we would "heal" a file by concluding the arr had
    forgotten it when in fact we simply failed to ask.
    """
    ep = "/episodefile/" if (c.cfg.kind or "").lower() == "sonarr" else "/moviefile/"
    import httpx
    try:
        r = await c._get(f"{ep}{int(file_id)}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None, False
        raise
    if not r:
        return None, False
    return (r.get("path") or None), True


async def _heal_one(row: dict) -> tuple[str, str]:
    """Try to resolve one missing file. Returns (outcome, detail)."""
    fid, path = row["id"], row["path"] or ""
    arr_name = row["arr_name"]
    file_id = row["arr_file_id"]
    parent = row["arr_parent_id"]

    # Cheapest possible check first: it may simply be back.
    if path and os.path.exists(path):
        return "healed", "path is back on disk"

    c = _client(arr_name)
    if not c or not file_id:
        if c:
            await c.close()
        return "skip", f"no reachable arr for {arr_name!r}"

    try:
        refreshed = await _refresh_parent(c, parent)
        new_path, tracked = await _current_path(c, file_id)

        if not tracked:
            return "healed", "the arr no longer tracks this file record"
        if new_path and os.path.normcase(new_path) != os.path.normcase(path):
            if os.path.exists(new_path):
                with cursor() as cur:
                    cur.execute("UPDATE files SET path=?, state='new', "
                                "state_reason='healed: the arr had moved it', "
                                "updated_at=? WHERE id=?",
                                (new_path, time.time(), fid))
                return "healed", f"moved -> {new_path}"
            return "still-missing", f"arr now says {new_path}, also absent"
        if new_path and os.path.exists(new_path):
            return "healed", "path is back on disk after refresh"
        return ("still-missing",
                "refreshed, still absent" if refreshed else
                "refresh did not complete, still absent")
    finally:
        try:
            await c.close()
        except Exception:
            pass


def _due(limit: int = 25) -> list[dict]:
    """Missing rows ready for another attempt."""
    now = time.time()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path, arr_name, arr_file_id, arr_parent_id, title, "
            "COALESCE(heal_attempts,0) AS heal_attempts, heal_last_at, heal_state "
            "FROM files WHERE state='missing' "
            "AND COALESCE(heal_state,'') <> 'confirmed' "
            "AND COALESCE(heal_attempts,0) < ? "
            "ORDER BY COALESCE(heal_last_at,0) ASC LIMIT ?",
            (MAX_ATTEMPTS, limit))]
    out = []
    for r in rows:
        n = int(r["heal_attempts"] or 0)
        wait = BACKOFF_S[min(n, len(BACKOFF_S) - 1)] if n else 0
        if not r["heal_last_at"] or (now - float(r["heal_last_at"])) >= wait:
            out.append(r)
    return out


async def sweep() -> dict:
    """One healing pass over everything currently marked missing."""
    due = _due()
    STATS.update(last_run=time.time(), running=True, total=len(due),
                 done=0, current=None)
    healed = confirmed = still = 0

    with joblog.section("missing check") as sec:
        if due:
            sec.keep()
            sec.note(f"{len(due)} file(s) due for verification")
        for r in due:
            title = (r["title"] or os.path.basename(r["path"] or "")
                     or f"id {r['id']}")
            STATS["current"] = title
            attempt = int(r["heal_attempts"] or 0) + 1
            try:
                outcome, detail = await _heal_one(r)
            except Exception as e:
                outcome, detail = "still-missing", f"{type(e).__name__}: {e}"

            now = time.time()
            if outcome == "healed":
                with cursor() as cur:
                    # state may already have been set by _heal_one for a move
                    cur.execute("UPDATE files SET state=CASE WHEN state='missing' "
                                "THEN 'new' ELSE state END, "
                                "heal_attempts=0, heal_last_at=?, heal_state=NULL, "
                                "state_reason=? WHERE id=?",
                                (now, f"healed: {detail}", r["id"]))
                healed += 1
                sec.note(f"healed: {title} - {detail}", "ok")
            elif outcome == "skip":
                with cursor() as cur:
                    cur.execute("UPDATE files SET heal_last_at=? WHERE id=?",
                                (now, r["id"]))
                sec.note(f"skipped: {title} - {detail}", "warn")
            else:
                final = attempt >= MAX_ATTEMPTS
                with cursor() as cur:
                    cur.execute("UPDATE files SET heal_attempts=?, heal_last_at=?, "
                                "heal_state=?, state_reason=? WHERE id=?",
                                (attempt, now,
                                 "confirmed" if final else "checking",
                                 (f"missing - confirmed after {attempt} checks: {detail}"
                                  if final else
                                  f"checking ({attempt}/{MAX_ATTEMPTS}): {detail}"),
                                 r["id"]))
                if final:
                    confirmed += 1
                    sec.note(f"CONFIRMED MISSING: {title} - {detail} "
                             f"(checked {attempt} times)", "error")
                else:
                    still += 1
                    sec.note(f"still absent ({attempt}/{MAX_ATTEMPTS}): {title} "
                             f"- {detail}", "warn")
            STATS["done"] = STATS.get("done", 0) + 1
        if due:
            sec.result = (f"healed {healed}, still checking {still}, "
                          f"confirmed {confirmed}")

    from . import workers
    STATS.update(running=False, current=None, checked=len(due), healed=healed,
                 confirmed=confirmed,
                 next_run=time.time() + workers.tune("missing_poll_s"))
    return {"checked": len(due), "healed": healed, "still_checking": still,
            "confirmed": confirmed}


def counts() -> dict:
    """Split the missing rows into 'being checked' and 'real'."""
    with cursor() as cur:
        r = cur.execute(
            "SELECT "
            " SUM(CASE WHEN COALESCE(heal_state,'')='confirmed' THEN 1 ELSE 0 END) c,"
            " SUM(CASE WHEN COALESCE(heal_state,'')<>'confirmed' THEN 1 ELSE 0 END) k"
            " FROM files WHERE state='missing'").fetchone()
    return {"confirmed": int((r["c"] if r else 0) or 0),
            "checking": int((r["k"] if r else 0) or 0)}


async def watch() -> None:
    """Run a healing pass on a timer, forever."""
    await asyncio.sleep(60)          # let the first scan settle
    STATS["next_run"] = time.time()
    while True:
        schedules.beat('healer')
        try:
            await sweep()
        except Exception as e:
            joblog.log(f"missing healer: {type(e).__name__}: {e}", "error")
            # A failed sweep must still leave a next_run behind, or the panel
            # reads "running" forever on a loop that is actually just sleeping.
            from . import workers as _w
            STATS.update(running=False, current=None,
                         next_run=time.time() + _w.tune("missing_poll_s"))
        from . import workers
        await asyncio.sleep(workers.tune("missing_poll_s"))
