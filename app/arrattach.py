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
import re
import time

from .arr import shared_client
from .config import SETTINGS
from .db import cursor
from . import joblog
from . import pathmap
from .rules import _LANG_NAME

STATS: dict = {"checked": 0, "reattached": 0, "unmatched": 0, "last_run": 0.0}

_AIRDATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


async def _daily_episode(c, parent_id: int, path: str,
                         season: int | None) -> tuple[list[int], str]:
    """For an air-by-date series, the episode the filename's date names.

    WHY SONARR CANNOT DO THIS ITSELF. The daily naming format writes
    "WWE SmackDown (1999) - 2026-09-04 - SmackDown 1411 [...]", and Sonarr's
    parser reads the trailing "SmackDown 1411" as an absolute episode number,
    which turns the whole name into a standard-series parse with no date -
    /parse returns isDaily=false, no episodes, and the manual-import
    candidate carries no episode. Every existing file only maps because the
    mapping was stored at import time; the moment the path changes (nuarr
    renames .mp4 to .mkv) the file is unmatchable by name, the episode goes
    "missing", and Sonarr downloads it again.

    This is not a guess. Sonarr's own rule for a daily series is "the
    episode whose airDate equals the date in the release", and the date is
    in the filename because Sonarr wrote it there. It is applied only when:
    the series is typed daily, the name has exactly one date, exactly one
    episode has that airDate, and its season agrees with the one nuarr
    recorded for this file when Sonarr still owned it.
    """
    try:
        series = await c._get(f"/series/{int(parent_id)}")
    except Exception:
        return [], "could not read the series"
    if (series or {}).get("seriesType") != "daily":
        return [], "the arr could not match this file to an episode"
    dates = _AIRDATE.findall(os.path.basename(path))
    if len(dates) != 1:
        return [], ("the arr could not match this file to an episode, and the "
                    "name carries no single air date to match it by")
    want = "-".join(dates[0])
    try:
        eps = await c._get("/episode", seriesId=int(parent_id))
    except Exception:
        return [], "could not read the episode list"
    hits = [e for e in (eps or []) if e.get("airDate") == want]
    if len(hits) != 1:
        return [], (f"the arr could not match this file to an episode; "
                    f"{len(hits)} episode(s) air on {want}")
    ep = hits[0]
    if season is not None and int(ep.get("seasonNumber") or -1) != int(season):
        return [], (f"the episode airing {want} is in season "
                    f"{ep.get('seasonNumber')}, but this file was recorded "
                    f"under season {season} - not attaching")
    return [int(ep["id"])], (f"matched by air date {want} to S{ep.get('seasonNumber'):02d}"
                             f"E{ep.get('episodeNumber'):02d} {ep.get('title') or ''}".strip())


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
    return any(pathmap.same(f.get("path") or "", path)
               for f in files or [])


def _unknown(q) -> bool:
    return str(((q or {}).get("quality") or {}).get("name") or "Unknown") == "Unknown"


async def _fill_in(c, me: dict, path: str, audio_langs: str | None) -> list[str]:
    """Quality, language and group for a candidate the arr could not read.

    When the parser gives up on the name (see _daily_episode) it also gives
    up on everything else in it, and a file imported with quality Unknown is
    a file the arr will upgrade at the first opportunity - which is the
    re-download loop this module exists to stop. So the blanks are filled
    from sources that are not guesses:
      quality  - the arr's own /parse of the bare filename, which reads
                 "[WEBDL-1080p]" fine even when it cannot place the episode;
      language - the audio languages nuarr probed from the file itself;
      group    - the "-GROUP" suffix the naming format wrote.
    """
    did = []
    if _unknown(me.get("quality")):
        try:
            pr = await c._get("/parse", title=os.path.basename(path))
            q = ((pr or {}).get("parsedEpisodeInfo") or {}).get("quality")
            if q and not _unknown(q):
                me["quality"] = q
                did.append(f"quality {q['quality']['name']}")
        except Exception:
            pass
    langs = me.get("languages") or []
    if not langs or all((l.get("name") or "Unknown") == "Unknown" for l in langs):
        codes = [x.strip().lower() for x in (audio_langs or "").split(",") if x.strip()]
        names = []
        for code in codes:
            n = _LANG_NAME.get(code)
            if n and n not in names:
                names.append(n)
        if names:
            try:
                table = {l["name"]: l["id"] for l in await c._get("/language")}
                picked = [{"id": table[n], "name": n} for n in names if n in table]
                if picked:
                    me["languages"] = picked
                    did.append("language " + "/".join(x["name"] for x in picked))
            except Exception:
                pass
    if not me.get("releaseGroup"):
        m = re.search(r"-([A-Za-z0-9]+)(?:\.[A-Za-z0-9]+)?$", os.path.basename(path))
        if m:
            me["releaseGroup"] = m.group(1)
            did.append(f"group {m.group(1)}")
    return did


async def reattach(cfg, parent_id: int, path: str,
                   season: int | None = None,
                   audio_langs: str | None = None) -> tuple[bool, str]:
    """Attach an existing file to its episode via ManualImport."""
    c = shared_client(cfg)
    how = "re-attached by manual import"
    # THE ARR HAS TO BE ASKED IN ITS OWN SPELLING. This folder goes over the
    # wire to the arr, which will look for it on its own filesystem - handing
    # it a UNC path it cannot resolve returns an empty candidate list and the
    # honest-looking message "the arr does not offer this file", which sent us
    # looking at import rules rather than at the path.
    folder = pathmap.to_arr(os.path.dirname(path))
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
               if pathmap.same(x.get("path") or "", path)), None)
    if not me:
        return False, "the arr does not offer this file as an import candidate"

    if cfg.kind == "sonarr":
        eps = [e.get("id") for e in (me.get("episodes") or []) if e.get("id")]
        if not eps:
            # Not guessed - see _daily_episode for the one rule that is
            # allowed to answer here. Anything else (a special, a date the
            # provider has wrong) is reported and left alone, because a wrong
            # attachment hides the problem behind a file on the wrong episode.
            eps, why = await _daily_episode(c, parent_id, path, season)
            if not eps:
                return False, why
            how = f"re-attached by manual import - {why}"
        filled = await _fill_in(c, me, path, audio_langs)
        if filled:
            how += " (" + ", ".join(filled) + " read from the file, since the arr's parser could not)"
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
    # CHECK WHAT LANDED. The import is a queued command; give it a moment,
    # then read the record back. If the arr stored Unknown for quality or
    # language despite what was sent, set them through the editor endpoint -
    # a file at Unknown quality is one the arr will "upgrade" at the next
    # search, which is the re-download loop this whole module is here to
    # prevent. Verified live on SmackDown 1411: the ManualImport payload's
    # languages were dropped, the editor call was honoured.
    if cfg.kind == "sonarr" and (not _unknown(me.get("quality")) or me.get("languages")):
        for _ in range(6):
            await asyncio.sleep(2)
            try:
                files = await c._get("/episodefile", seriesId=int(parent_id))
            except Exception:
                break
            mine = next((f for f in files or [] if pathmap.same(f.get("path") or "", path)), None)
            if not mine:
                continue
            fix: dict = {"episodeFileIds": [int(mine["id"])]}
            if _unknown(mine.get("quality")) and not _unknown(me.get("quality")):
                fix["quality"] = me["quality"]
            ls = mine.get("languages") or []
            if (not ls or all((l.get("name") or "Unknown") == "Unknown" for l in ls)) and me.get("languages"):
                fix["languages"] = me["languages"]
            if not mine.get("releaseGroup") and me.get("releaseGroup"):
                fix["releaseGroup"] = me["releaseGroup"]
            if len(fix) > 1:
                try:
                    await c._put("/episodefile/editor", fix)
                    how += " - and set " + ", ".join(k for k in fix if k != "episodeFileIds") + " on the record afterwards"
                except Exception as e:
                    how += f" - but could not set {', '.join(k for k in fix if k != 'episodeFileIds')} afterwards ({type(e).__name__})"
            break
    return True, how


async def check_file(file_id: int) -> tuple[bool, str]:
    """One file: does its arr still own it, and re-attach if not."""
    with cursor() as cur:
        r = cur.execute("SELECT path, arr_name, arr_parent_id, title, season, "
                        "audio_langs FROM files WHERE id=?", (file_id,)).fetchone()
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
    ok, why = await reattach(cfg, r["arr_parent_id"], r["path"], r["season"],
                             r["audio_langs"])
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
