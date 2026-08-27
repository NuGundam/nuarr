r"""
nuarr - self-healing for "unmanaged" files

WHY THIS EXISTS
---------------
A file reads as unmanaged when arr_file_id IS NULL - nuarr can see it on the
pool, but no arr claims it. That is the mirror image of the missing case the
healer handles, and it goes stale for the same reasons:

  * the arr imported it minutes after our last scan, so our row never learned
    the file id
  * an import failed once and nobody rescanned the series since
  * the path differs from the arr's by case or a trailing space
  * the file arrived outside the arr entirely

Only the last of those is a real orphan. The rest resolve the moment somebody
asks the arr to look at the folder again - which, until now, only happened on a
full library scan, three hours apart, and only as a side effect.

WHY IT IS NOT JUST "RUN verify_unmanaged ON A TIMER"
----------------------------------------------------
verify_unmanaged() needs the whole arr catalogue - a ~40 s fan-out over every
series that materialises 39,000 objects. That is fine once per scan, when the
scan has already paid for it. It is not fine every few minutes for the sake of
six files.

So this works the way the healer does: per FILE, targeted. Ask the arr about
the one folder that matters, wait for it, re-check the one path. Six files cost
six small requests instead of one enormous one.

WHAT IT DOES
------------
For each unmanaged file, up to MAX_ATTEMPTS times:

  1. Find the arr that owns the folder - the series or movie whose path is a
     prefix of the file's.
  2. If no arr knows the folder at all, stop. Nothing will ever adopt this
     file until the show is added, so re-asking wastes requests and the answer
     is already actionable.
  3. Otherwise ask that arr to RESCAN the folder, so it imports anything it
     has not seen, and wait for the command.
  4. Re-read the parent's file list and look for our path. If it is there, the
     arr has adopted the file - write the id onto our row and it stops being
     unmanaged.

After MAX_ATTEMPTS the file is marked 'orphan': the arr knows the folder and
still will not take the file, which is a real import problem worth a human.
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

# A rescan is not instant and the arr may be busy with its own work. Retrying
# straight away would spend all three attempts inside the same stale window and
# confirm a perfectly adoptable file as an orphan.
BACKOFF_S = [120, 600, 1800]

POLL_S = 300                 # how often the adopter sweeps
_CMD_TIMEOUT_S = 90

# Small files are extras - OP/ED, AMVs, bonus clips - and the arrs are not
# meant to manage them. Chasing those would mean rescanning every series
# forever over files nobody wants imported.
MIN_SIZE_B = 100 * 1024 * 1024

STATS: dict = {"last_run": 0.0, "next_run": 0.0, "running": False,
               "checked": 0, "adopted": 0, "orphaned": 0, "no_folder": 0,
               "current": None, "total": 0, "done": 0}


def _reset_progress(total: int) -> None:
    STATS.update(running=True, total=total, done=0, current=None,
                 last_run=time.time())


def _cfg(arr_name: str):
    return next((a for a in SETTINGS.arrs
                 if a.name == arr_name and a.enabled and a.api_key), None)


# The arr's series/movie list, fetched ONCE per sweep rather than once per
# file. The first version called _owning_parent for every file, and that
# fetched the whole catalogue each time - about a thousand series, twice over
# (Sonarr and Radarr), for each of six files. The sweep took 44 s and almost
# all of it was re-downloading the same list. Cleared at the start of each pass
# so a series added between sweeps is still found.
_PARENTS: dict[str, list] = {}
# (arr_name, parent_id) already rescanned during THIS sweep.
_RESCANNED: set[tuple] = set()


async def _parents(c, cfg) -> list:
    if cfg.name not in _PARENTS:
        try:
            _PARENTS[cfg.name] = await c._get(
                "/series" if cfg.kind == "sonarr" else "/movie") or []
        except Exception:
            _PARENTS[cfg.name] = []
    return _PARENTS[cfg.name]


def _owning_parent(items: list, path: str):
    """The series/movie whose folder contains `path`, or None.

    Matched on the FOLDER, not the title: the title in a filename is whatever
    the release group typed, while the folder is what the arr itself created.
    """
    want = os.path.normcase(os.path.dirname(path))
    best = None
    for it in items or []:
        p = os.path.normcase(it.get("path") or "")
        if not p:
            continue
        if want == p or want.startswith(p + os.sep):
            # longest match wins - a season folder sits inside a series folder
            if best is None or len(p) > len(os.path.normcase(best.get("path") or "")):
                best = it
    return best


async def _adopt_one(row: dict) -> tuple[str, str]:
    """-> (outcome, detail). outcome in adopted|no_folder|still|error."""
    path = row["path"] or ""
    if not path or not os.path.exists(path):
        return "still", "file is no longer on disk"

    # Which arr could own this? Try each enabled one; the folder match decides.
    from .arr import shared_client
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or not cfg.api_key:
            continue
        c = shared_client(cfg)
        try:
            parent = _owning_parent(await _parents(c, cfg), path)
        except Exception as e:
            return "error", f"{cfg.name}: {type(e).__name__}: {e}"
        if not parent:
            continue

        pid = parent.get("id")
        title = parent.get("title") or f"id {pid}"
        try:
            # Rescan, not Refresh: Refresh re-reads metadata from TVDB/TMDB,
            # Rescan re-reads the FOLDER, which is the thing that has changed.
            #
            # AND WAIT FOR IT TO FINISH. The first version slept a flat 6 s and
            # then looked - which was reliably too soon. Two files were imported
            # by the rescan this triggered and still reported as "still
            # unmanaged", because the check happened before the arr had
            # finished; a later scan then picked up the adoption and the sweep
            # took no credit for work it had actually caused.
            key = "seriesId" if cfg.kind == "sonarr" else "movieId"
            # ONE RESCAN PER PARENT PER SWEEP. A rescan covers the whole
            # series, so four unmanaged episodes of the same show do not need
            # four of them - and the run above did exactly that, rescanning
            # "There Was a Cute Girl..." twice in one pass for two files that
            # sit in the same folder. The second rescan cannot find anything
            # the first one missed.
            if (cfg.name, pid) not in _RESCANNED:
                _RESCANNED.add((cfg.name, pid))
                cmd = await c._post("/command", {
                    "name": "RescanSeries" if cfg.kind == "sonarr"
                            else "RescanMovie", key: pid})
                cid = (cmd or {}).get("id")
                if cid:
                    await c.wait_command(cid, timeout_s=_CMD_TIMEOUT_S)
                # The import lands a moment after the command reports complete.
                await asyncio.sleep(4)

            kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
            files = await c._get(f"/{kind}", **{key: pid})
            want = os.path.normcase(path)

            # IS THE EPISODE ALREADY SATISFIED?
            #
            # "the arr will not import it" is true but unhelpful, and it is
            # what this reported for all four files on this server. The real
            # answer, in every case, was that the episode ALREADY HAS a file -
            # the arr owns a different copy, present on disk, and simply has no
            # use for a second one. That is a leftover from an upgrade or a
            # failed rename, not a failed import, and the fix is to delete it
            # rather than to rescan harder.
            already = _episode_already_has_file(files, path)
            if already:
                return "duplicate", already

            for f in files or []:
                if os.path.normcase(f.get("path") or "") == want:
                    with cursor() as cur:
                        cur.execute(
                            "UPDATE files SET arr_name=?, arr_file_id=?, "
                            "arr_parent_id=?, state_reason='adopted by the arr "
                            "after a rescan', adopt_state='adopted', "
                            "updated_at=? WHERE id=?",
                            (cfg.name, f.get("id"), pid, time.time(),
                             row["id"]))
                    return "adopted", f"{cfg.name} imported it into {title}"
            return "still", f"{cfg.name} knows {title} but has not imported it"
        except Exception as e:
            return "error", f"{cfg.name}: {type(e).__name__}: {e}"

    return "no_folder", "no arr manages this folder"


def _episode_already_has_file(files: list, path: str) -> str | None:
    """Does the arr already own a DIFFERENT file for this same episode?

    Matched on the season/episode in the filename against the arr's own files
    for that parent. Deliberately not on title: the title in a filename is
    whatever the release group typed, and three of the four leftovers on this
    server had a mangled one ("-VARYGThere Was a Cute Girl in the Heros
    Party..."), which is presumably why the arr could not place them either.
    """
    import re

    def _key(name: str):
        r"""What identifies the episode in this filename.

        TWO NAMING SHAPES, NOT ONE.
        SxxExx covers ordinary series. An air-by-date series never has it -
        WWE SmackDown files are named "2026-04-10 - SmackDown 1390" - so this
        check could not fire on a daily show at all, and a second copy of one
        night's episode would have gone unnoticed. SmackDown's history has
        five downloads of the same episode in a single morning; if that ever
        leaves two files behind, this is what would have to spot it.
        """
        m = re.search(r"S(\d+)E(\d+)", name, re.I)
        if m:
            return ("se", int(m.group(1)), int(m.group(2)))
        m = re.search(r"(?<!\d)(\d{4})[-. _](\d{2})[-. _](\d{2})(?!\d)", name)
        if m:
            return ("date", m.group(1), m.group(2) + m.group(3))
        return None

    want = _key(os.path.basename(path))
    if not want:
        return None
    mine = os.path.normcase(path)
    for f in files or []:
        p = f.get("path") or ""
        if os.path.normcase(p) == mine:
            continue                       # that is this very file
        if _key(os.path.basename(p)) != want:
            continue
        if not os.path.exists(p):
            continue                       # the arr's copy is gone; not a dupe
        gb = (f.get("size") or 0) / 1024 ** 3
        which = (f"S{want[1]:02d}E{want[2]:02d}" if want[0] == "se"
                 else f"the {want[1]}-{want[2][:2]}-{want[2][2:]} episode")
        return (f"the arr already has {which} — "
                f"{os.path.basename(p)[:60]} ({gb:.2f} GB). This file is a "
                f"leftover copy, not a failed import.")
    return None


def _due(limit: int = 25) -> list[dict]:
    """Unmanaged rows ready for another attempt."""
    now = time.time()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path, title, size, "
            "COALESCE(adopt_attempts,0) AS adopt_attempts, adopt_last_at, "
            "adopt_state FROM files "
            " WHERE arr_file_id IS NULL "
            "   AND state NOT IN ('duplicate','deleted') "
            "   AND COALESCE(size,0) >= ? "
            "   AND COALESCE(adopt_state,'') NOT IN "
            "       ('orphan','no_folder','duplicate') "
            "   AND COALESCE(adopt_attempts,0) < ? "
            " ORDER BY COALESCE(adopt_last_at,0) ASC LIMIT ?",
            (MIN_SIZE_B, MAX_ATTEMPTS, limit))]
    out = []
    for r in rows:
        n = int(r["adopt_attempts"] or 0)
        wait = BACKOFF_S[min(n, len(BACKOFF_S) - 1)] if n else 0
        if not r["adopt_last_at"] or (now - float(r["adopt_last_at"])) >= wait:
            out.append(r)
    return out


async def sweep() -> dict:
    """One adoption pass over everything currently unmanaged."""
    due = _due()
    _PARENTS.clear()             # one catalogue fetch per sweep, not per file
    _RESCANNED.clear()           # one rescan per series per sweep
    _reset_progress(len(due))
    adopted = orphaned = still = no_folder = dup = 0

    with joblog.section("unmanaged check") as sec:
        if due:
            sec.keep()
            sec.note(f"{len(due)} file(s) due for an adoption check")
        for r in due:
            title = (r["title"] or os.path.basename(r["path"] or "")
                     or f"id {r['id']}")
            STATS["current"] = title
            attempt = int(r["adopt_attempts"] or 0) + 1
            try:
                outcome, detail = await _adopt_one(r)
            except Exception as e:
                outcome, detail = "error", f"{type(e).__name__}: {e}"

            now = time.time()
            if outcome == "adopted":
                adopted += 1
                joblog.log(f"adopted: {title} — {detail}", "ok")
            elif outcome == "duplicate":
                # A settled fact, not something more rescans will change, so
                # it stops being retried immediately rather than burning the
                # remaining attempts on a question already answered.
                #
                # state='duplicate' TOO, not just adopt_state. The first
                # version wrote only the classification, so the row kept the
                # 'eligible' it got from the scan - and the drill panel showed
                # an eligible pill over a reason explaining the file should
                # not exist. Eligible means "queue me"; a leftover copy must
                # never be queued, and 'duplicate' is already excluded from
                # every queue path and requeue sweep.
                dup += 1
                with cursor() as cur:
                    cur.execute(
                        "UPDATE files SET adopt_state='duplicate', "
                        "state='duplicate', "
                        "state_reason=?, adopt_attempts=?, adopt_last_at=? "
                        "WHERE id=?", (detail, MAX_ATTEMPTS, now, r["id"]))
                joblog.log(f"leftover copy: {title} — {detail}", "warn")
            elif outcome == "no_folder":
                no_folder += 1
                with cursor() as cur:
                    cur.execute(
                        "UPDATE files SET adopt_state='no_folder', "
                        "state_reason=?, adopt_attempts=?, adopt_last_at=? "
                        "WHERE id=?",
                        ("no arr manages this folder — add the show or movie "
                         "to Sonarr/Radarr and it will be imported",
                         attempt, now, r["id"]))
                joblog.log(f"unmanaged: {title} — {detail}", "warn")
            else:
                still += 1
                final = attempt >= MAX_ATTEMPTS
                if final:
                    orphaned += 1
                with cursor() as cur:
                    cur.execute(
                        "UPDATE files SET adopt_attempts=?, adopt_last_at=?, "
                        "adopt_state=?, state_reason=? WHERE id=?",
                        (attempt, now, "orphan" if final else "checking",
                         (f"the arr knows this folder but will not import it "
                          f"after {MAX_ATTEMPTS} rescans — check its import "
                          f"log") if final else detail,
                         r["id"]))
                joblog.log(
                    f"unmanaged {attempt}/{MAX_ATTEMPTS}: {title} — {detail}",
                    "warn" if final else "debug")
            STATS["done"] = STATS.get("done", 0) + 1

        if due:
            sec.note(f"{adopted} adopted, {dup} leftover copies, "
                     f"{still} still unmanaged, {no_folder} not in any arr")

        # Confirmed leftovers past the waiting period go to the Recycle Bin.
        try:
            rec = await _recycle_leftovers()
        except Exception as e:
            rec = {"recycled": 0, "kept": 0}
            joblog.log(f"leftover cleanup failed (files untouched): "
                       f"{type(e).__name__}: {e}", "warn")
        if rec.get("recycled"):
            sec.keep()
            sec.note(f"{rec['recycled']} leftover cop"
                     f"{'y' if rec['recycled'] == 1 else 'ies'} moved to the "
                     f"Recycle Bin (restorable)", "warn")

    STATS.update(running=False, current=None, checked=len(due),
                 adopted=adopted, orphaned=orphaned, no_folder=no_folder,
                 recycled=rec.get("recycled", 0),
                 next_run=time.time() + POLL_S)
    return {"checked": len(due), "adopted": adopted, "still": still,
            "orphaned": orphaned, "no_folder": no_folder,
            "duplicates": dup, "recycled": rec.get("recycled", 0)}


# ---- leftover cleanup ------------------------------------------------------
# How this differs from everything else in this module: it is the one thing
# here that REMOVES a file. The bar is correspondingly higher - the verdict is
# re-earned fresh at the moment of removal, the removal is a Recycle Bin move
# that Explorer can undo, and any doubt at any step keeps the file.
#
# The waiting period is not politeness. A classification made 2 hours ago was
# made against the arr's state 2 hours ago; an import, a rename or a manual
# fix since then can change the answer. Old enough to have survived a full
# sweep interval unchallenged, re-verified now, and only then moved.
RECYCLE_AFTER_S = 6 * 3600
RECYCLE_TOGGLE = "adopt.recycle_dupes"


async def _still_duplicate(path: str) -> str | None:
    """Ask the arr NOW, not the database. None means 'do not touch'."""
    from .arr import shared_client
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or not cfg.api_key:
            continue
        c = shared_client(cfg)
        try:
            parent = _owning_parent(await _parents(c, cfg), path)
            if not parent:
                continue
            key = "seriesId" if cfg.kind == "sonarr" else "movieId"
            kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
            files = await c._get(f"/{kind}", **{key: parent.get("id")})
            mine = os.path.normcase(path)
            if any(os.path.normcase(f.get("path") or "") == mine
                   for f in files or []):
                return None      # the arr adopted it since - not a leftover
            return _episode_already_has_file(files, path)
        except Exception:
            return None          # cannot confirm -> keep the file
    return None


async def _untracked_sibling(path: str) -> str | None:
    """The OTHER copy of this episode in the same folder, if the arr does not
    track it. This is what actually ends an import ping-pong.

    Observed on Fist of the North Star S04E11: two copies of one episode on
    disk, and every rescan flipped which one Sonarr considered current - 15
    imports in a day, nuarr processing the "new" file each time. By cleanup
    time the arr had flipped onto the CLASSIFIED copy, so 'keep, it is
    tracked now' was true and useless: the loop just carried on with the
    roles reversed. The stable end state is one file, and the one to remove
    is whichever the arr does not claim at this exact moment.
    """
    import re
    from .arr import shared_client
    m = re.search(r"S(\d+)E(\d+)", os.path.basename(path), re.I)
    if not m:
        return None
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or not cfg.api_key:
            continue
        c = shared_client(cfg)
        try:
            parent = _owning_parent(await _parents(c, cfg), path)
            if not parent:
                continue
            key = "seriesId" if cfg.kind == "sonarr" else "movieId"
            kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
            files = await c._get(f"/{kind}", **{key: parent.get("id")})
            tracked = {os.path.normcase(f.get("path") or "")
                       for f in files or []}
            folder = os.path.dirname(path)
            for name in os.listdir(folder):
                cand = os.path.join(folder, name)
                if os.path.normcase(cand) == os.path.normcase(path):
                    continue
                fm = re.search(r"S(\d+)E(\d+)", name, re.I)
                if not fm or fm.groups() != m.groups():
                    continue
                if os.path.normcase(cand) in tracked:
                    continue
                if os.path.isfile(cand):
                    return cand
            return None
        except Exception:
            return None
    return None


async def _recycle_leftovers() -> dict:
    from .gate import get_toggle
    from . import fileops
    out = {"recycled": 0, "kept": 0}
    if not get_toggle(RECYCLE_TOGGLE):
        return out
    now = time.time()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path, title FROM files "
            "WHERE adopt_state='duplicate' AND state='duplicate' "
            "  AND adopt_last_at IS NOT NULL AND adopt_last_at < ? "
            "LIMIT 20", (now - RECYCLE_AFTER_S,))]
    for r in rows:
        path = r["path"] or ""
        title = r["title"] or os.path.basename(path)
        if not os.path.exists(path):
            # Someone beat us to it - record the fact, remove nothing.
            with cursor() as cur:
                cur.execute("UPDATE files SET state='deleted', state_reason="
                            "'leftover copy was already removed', "
                            "updated_at=? WHERE id=?", (now, r["id"]))
            continue
        why = await _still_duplicate(path)
        if not why:
            # The arr now tracks the copy we classified - either the verdict
            # was stale, or an import ping-pong flipped the roles. If a
            # same-episode sibling exists that the arr does NOT track, that
            # sibling is the leftover now; removing it is what ends the flip.
            other = await _untracked_sibling(path)
            if other and not await asyncio.to_thread(fileops.who_locks, other):
                res = await asyncio.to_thread(fileops.recycle, other)
                if res.ok:
                    out["recycled"] += 1
                    with cursor() as cur:
                        cur.execute(
                            "UPDATE files SET state='deleted', state_reason="
                            "'leftover copy (import ping-pong) moved to the "
                            "Recycle Bin (restorable)', updated_at=? "
                            "WHERE path=?", (now, other))
                        # Our row's file is the arr's current pick: let the
                        # scan re-sync it rather than leaving it condemned.
                        cur.execute(
                            "UPDATE files SET state='new', adopt_state=NULL, "
                            "adopt_attempts=0, state_reason='the arr adopted "
                            "this copy; its duplicate twin was recycled', "
                            "updated_at=? WHERE id=?", (now, r["id"]))
                        cur.execute(
                            "INSERT INTO history(file_id,event,detail,at) "
                            "VALUES(?,?,?,?)",
                            (r["id"], "recycled",
                             f"untracked twin -> Recycle Bin: "
                             f"{os.path.basename(other)}"[:300], now))
                    joblog.log(f"import ping-pong ended: untracked twin of "
                               f"{title} recycled (restorable)", "warn")
                    continue
            # No untracked sibling means the arr tracks EVERY copy - two
            # files, one episode, both owned. That is the one shape this
            # sweep must not resolve by itself: which release survives is a
            # quality call, and the arr's profile owns quality calls. Say so
            # where a human will read it, instead of silently keeping both
            # while every rescan churns import events.
            with cursor() as cur:
                cur.execute(
                    "UPDATE files SET state='new', adopt_state=NULL, "
                    "adopt_attempts=0, state_reason='the arr tracks this "
                    "file AND another copy of the same episode - delete one "
                    "of the two in Sonarr/Radarr to stop the import churn', "
                    "updated_at=? WHERE id=?", (now, r["id"]))
            joblog.log(f"both copies of {title} are tracked by the arr - "
                       f"needs a human: delete one in the arr UI", "warn")
            out["kept"] += 1
            continue
        who = await asyncio.to_thread(fileops.who_locks, path)
        if who:
            out["kept"] += 1     # in use; next sweep will get another chance
            continue
        res = await asyncio.to_thread(fileops.recycle, path)
        if not res.ok:
            out["kept"] += 1
            joblog.log(f"leftover copy NOT removed ({res.detail}): {title}",
                       "warn")
            continue
        out["recycled"] += 1
        with cursor() as cur:
            cur.execute("UPDATE files SET state='deleted', state_reason=?, "
                        "updated_at=? WHERE id=?",
                        (f"leftover copy moved to the Recycle Bin "
                         f"(restorable) — {why}"[:400], now, r["id"]))
            cur.execute("INSERT INTO history(file_id,event,detail,at) "
                        "VALUES(?,?,?,?)",
                        (r["id"], "recycled",
                         f"leftover duplicate -> Recycle Bin: "
                         f"{os.path.basename(path)}"[:300], now))
        joblog.log(f"leftover copy recycled (restorable from the Bin): "
                   f"{title}", "warn")
    return out


def counts() -> dict:
    """Split the unmanaged rows the way the tile wants to show them."""
    with cursor() as cur:
        r = cur.execute(
            "SELECT "
            " SUM(CASE WHEN COALESCE(adopt_state,'') IN "
            "          ('orphan','no_folder','duplicate') THEN 1 ELSE 0 END) confirmed,"
            " SUM(CASE WHEN COALESCE(adopt_state,'') NOT IN "
            "          ('orphan','no_folder','duplicate') THEN 1 ELSE 0 END) checking,"
            " SUM(CASE WHEN COALESCE(adopt_state,'')='no_folder' "
            "          THEN 1 ELSE 0 END) no_folder "
            " FROM files WHERE arr_file_id IS NULL "
            "   AND state NOT IN ('duplicate','deleted') "
            "   AND COALESCE(size,0) >= ?", (MIN_SIZE_B,)).fetchone()
    return {"confirmed": int(r["confirmed"] or 0),
            "checking": int(r["checking"] or 0),
            "no_folder": int(r["no_folder"] or 0)}


async def watch() -> None:
    """Run an adoption pass on a timer, forever."""
    # The cleanup defaults ON, once, on first run with the feature - after
    # that the stored value wins, so turning it off in Gate settings sticks.
    # Its whole design is fail-safe (re-verify, skip locked, Recycle Bin),
    # which is what justifies a default of on rather than off.
    try:
        from .db import kv_get
        from .gate import set_toggle
        if kv_get(RECYCLE_TOGGLE) is None:
            set_toggle(RECYCLE_TOGGLE, True)
    except Exception:
        pass
    # Later than the healer's 60 s so the two do not both hit the arrs the
    # moment the process starts, on top of the first scan.
    await asyncio.sleep(150)
    STATS["next_run"] = time.time()
    while True:
        schedules.beat('adopter')
        try:
            await sweep()
        except Exception as e:
            joblog.log(f"unmanaged adopter: {type(e).__name__}: {e}", "error")
            STATS.update(running=False, current=None,
                         next_run=time.time() + POLL_S)
        await asyncio.sleep(POLL_S)
