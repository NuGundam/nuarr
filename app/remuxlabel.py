r"""nuarr - a file nuarr re-encoded is not a remux any more.

The arr keeps the quality it imported, so an encoded Remux goes on saying
"Remux" in its name and its record (969 files, 2026-11-05), and every format
keyed on Remux goes on scoring it as one.

ONLY WHERE THE SCORE DOES NOT FALL. The Anime profile scores Remux above
Bluray, with a 50-point upgrade step and every file under its cutoff score:
relabelling those 698 drops them 210, enough for the arr to fetch a real remux
of each and start the encode over. So a file whose score would fall keeps its
label, and is counted in STATS for a person. A file downloaded as HEVC keeps
its label too: only nuarr's own encodes are proven not to be remuxes.

The new name comes from the arr, through the same rename queue as every encode.
"""
from __future__ import annotations

import asyncio
import re
import time

from . import joblog
from .config import SETTINGS
from .db import cursor

EVERY_S = 3600
PER_PASS = 30            # renames per hour, so Plex and the arrs are not flooded
STATS: dict = {"last_run": 0.0, "relabelled": 0, "kept_lower": 0}


def plain_name(quality: str) -> str:
    """'Remux-1080p' and 'Bluray-1080p Remux' -> 'Bluray-1080p'; '' if not a remux."""
    m = re.fullmatch(r"(?:Remux-(\d+p)|Bluray-(\d+p) Remux)", quality or "")
    return f"Bluray-{m.group(1) or m.group(2)}" if m else ""


def score_after(score: int, formats: list, scores: dict, resolution: int) -> int:
    """The score once the Remux formats stop matching and the Bluray one starts."""
    drop = sum(int(scores.get(n, 0)) for n in formats if "emux" in n)
    gain = int(scores.get({1080: "1080p Bluray", 2160: "UHD Bluray"}.get(resolution, ""), 0))
    return int(score) - drop + gain


def _rows(arr_name: str) -> list[dict]:
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT f.id, f.arr_file_id, f.arr_parent_id, f.path FROM files f "
            " WHERE f.arr_name=? AND f.video_codec='hevc' AND f.path LIKE '%Remux%' "
            "   AND f.state='done' AND f.arr_file_id IS NOT NULL "
            "   AND EXISTS (SELECT 1 FROM history h WHERE h.file_id=f.id "
            "               AND h.event='transcoded' AND h.detail LIKE '%HEVC%')",
            (arr_name,))]


async def _one_arr(cfg, room: int) -> tuple[int, int]:
    from . import renamequeue
    from .arr import shared_client
    c = shared_client(cfg)
    sonarr = cfg.kind == "sonarr"
    rows = await asyncio.to_thread(_rows, cfg.name)
    if not rows:
        return 0, 0
    profiles = {p["id"]: {x["name"]: x["score"] for x in p["formatItems"]}
                for p in await c._get("/qualityprofile")}
    parents = {p["id"]: p for p in await c._get("/series" if sonarr else "/movie")}
    defs = {d["quality"]["name"]: d["quality"] for d in await c._get("/qualitydefinition")}
    by_parent: dict = {}
    for r in rows:
        by_parent.setdefault(r["arr_parent_id"], {})[r["arr_file_id"]] = r
    done = lower = 0
    for pid, mine in by_parent.items():
        parent = parents.get(pid)
        if not parent:
            continue
        scores = profiles.get(parent.get("qualityProfileId")) or {}
        files = await (c._get("/episodefile", seriesId=pid) if sonarr
                       else c._get("/moviefile", movieId=pid))
        groups: dict = {}
        for e in files or []:
            r = mine.get(e.get("id"))
            q = (e.get("quality") or {}).get("quality") or {}
            new = plain_name(q.get("name") or "")
            if not r or new not in defs:
                continue
            after = score_after(e.get("customFormatScore") or 0,
                                [f["name"] for f in e.get("customFormats") or []],
                                scores, int(q.get("resolution") or 0))
            if after < int(e.get("customFormatScore") or 0):
                lower += 1
            elif done + sum(map(len, groups.values())) < room:
                groups.setdefault(new, []).append(r)
        for new, rs in groups.items():
            ids = [r["arr_file_id"] for r in rs]
            await c._put("/episodefile/editor" if sonarr else "/moviefile/editor", {
                ("episodeFileIds" if sonarr else "movieFileIds"): ids,
                "quality": {"quality": defs[new],
                            "revision": {"version": 1, "real": 0, "isRepack": False}}})
            for r in rs:
                renamequeue.enqueue(r["id"], cfg.name, pid, r["path"],
                                    why="relabelled - encoded, so no longer a remux")
            done += len(rs)
    return done, lower


async def sweep() -> dict:
    from .gate import get_toggle
    if get_toggle("gate.manual_pause"):
        return {"relabelled": 0, "why": "paused"}
    done = lower = 0
    for cfg in SETTINGS.arrs:
        if cfg.enabled and cfg.api_key and cfg.kind in ("sonarr", "radarr"):
            d, l = await _one_arr(cfg, PER_PASS - done)
            done += d
            lower += l
    STATS.update(last_run=time.time(), kept_lower=lower,
                 relabelled=STATS["relabelled"] + done)
    if done:
        joblog.log(f"remux labels: {done} encoded file(s) relabelled", "info")
    return {"relabelled": done, "kept_lower": lower}


async def watch() -> None:
    from . import schedules
    schedules.register(
        "remuxlabel", "Encoded files labelled Remux", "Library", EVERY_S,
        what="Moves files nuarr re-encoded from Remux to Bluray quality in the "
             "arr, and renames them. Leaves any file whose score would fall.")
    await asyncio.sleep(420)
    while True:
        schedules.beat("remuxlabel")
        try:
            await sweep()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"remux labels: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(EVERY_S)
