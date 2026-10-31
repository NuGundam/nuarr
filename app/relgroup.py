r"""The release group a file really came from, never a word the arr's parser
mistook for one (Erik, 2026-10-30).

WHY. The arrs read the group off the end of the release name, so a name that
ends "...Dual-Audio" or "...10-bit" files as group "Audio" or "bit", a hand
import as "Manual" or "Unknown", a "[RUBaDUB][1080p]" prefix as "RUBaDUB][".
The renamed file then carries the same word ("...-Audio.mkv"), so nothing on
disk says otherwise. Stored as-is, those words became "groups" with hundreds
of files across dozens of shows and the scorecard ranked them.

WHAT. clean(raw, *titles): the arr's word, tidied; when it is junk, the group
read off the first fallback title that gives a real one (the scene name, the
grabbed release's title); else None - no group beats a false one.
repair(): one pass over the stored names - tidy what can be tidied, then look
the junk ones up in each parent's import history (sourceTitle is the release
as grabbed), and clear what still has no real name.
"""
from __future__ import annotations

import os
import re

from . import joblog
from .db import cursor, kv_get, kv_set

KV_DONE = "relgroup.repaired"

JUNK = {"audio", "dual", "dualaudio", "bit", "8bit", "10bit", "anime", "unknown", "manual", "web",
        "webdl", "webrip", "multi", "hevc", "x265", "x264", "h264", "h265", "avc", "aac", "ac3",
        "eac3", "flac", "opus", "dts", "dub", "dubbed", "sub", "subs", "subbed", "raw", "raws",
        "eng", "jpn", "jap", "english", "japanese", "bluray", "bd", "bdrip", "remux", "hdr", "sdr",
        "dv", "uhd", "proper", "repack", "dl", "hdtv", "dvd", "dvdrip", "1080p", "720p", "2160p",
        "480p", "4k", "mkv", "mp4", "nogrp", "nogroup", "none", "null", "group", "release"}
_SHAPE = re.compile(r"[A-Za-z0-9][\w.+! &-]*")


def tidy(g) -> str:
    """Strip what the parser dragged along: trailing ][, brackets, spaces."""
    g = re.sub(r"[\[\]()]+$", "", str(g or "").strip()).strip()
    g = re.sub(r"^[\[\]()]+", "", g).strip()
    return re.sub(r"^(?:Dual-)?Audio-(?=\w)", "", g, flags=re.I)[:80]    # "Dual-Audio-AnoZu"


def junk(g) -> bool:
    g = tidy(g)
    return (len(g) < 2 or g.lower().replace("-", "").replace(" ", "") in JUNK
            or g.lower() in JUNK or not _SHAPE.fullmatch(g) or g.isdigit())


def from_title(title) -> str:
    """The group a release title names: "-GROUP" at the end, else a leading
    "[GROUP]" (fansub style). "" when neither is a real name."""
    t = re.sub(r"\.(mkv|mp4|m4v|avi|ts|webm)$", "", os.path.basename(str(title or "")).strip(), flags=re.I)
    if not t:
        return ""
    lead = re.match(r"^\[([^\]]+)\]", t)
    tail = re.search(r"-([A-Za-z0-9][\w.+!&]*)\s*(?:\[[0-9A-Fa-f]{8}\])?\s*$", t)
    for m in (tail, lead):
        if m and not junk(m.group(1)):
            return tidy(m.group(1))
    return ""


def clean(raw, *titles):
    """The real group or None."""
    g = tidy(raw)
    if g and not junk(g):
        return g
    for t in titles:
        if (h := from_title(t)):
            return h
    return None


async def repair(force: bool = False) -> dict:
    """One pass over the stored groups. Runs once (kv flag) unless forced."""
    if kv_get(KV_DONE) == "1" and not force:
        return {"skipped": "already repaired"}
    from .arr import shared_client
    from .config import SETTINGS
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, arr_name, arr_parent_id, arr_file_id, path, release_group g FROM files "
            "WHERE release_group IS NOT NULL AND release_group!=''")]
    tidied = cleared = recovered = 0
    bad: dict = {}
    with cursor() as cur:
        for r in rows:
            t = tidy(r["g"])
            if t and not junk(t):
                if t != r["g"]:
                    cur.execute("UPDATE files SET release_group=? WHERE id=?", (t, r["id"]))
                    tidied += 1
            else:
                bad.setdefault((r["arr_name"], r["arr_parent_id"]), []).append(r)
    arrs = {c.name: c for c in SETTINGS.arrs if c.enabled}
    for (arr, parent), rs in bad.items():
        cfg = arrs.get(arr)
        titles: dict = {}                    # arr file id -> candidate release names
        if cfg and parent:
            ep = (f"/history/series?seriesId={int(parent)}" if cfg.kind == "sonarr"
                  else f"/history/movie?movieId={int(parent)}")
            try:
                hist = await shared_client(cfg)._get(ep)
            except Exception:                                    # noqa: BLE001
                hist = []
            hist = hist if isinstance(hist, list) else []
            # THE GRAB NAMES THE RELEASE; the import names the file. Joined by
            # downloadId, the import's fileId leads back to the grabbed title.
            grabs = {h.get("downloadId"): h.get("sourceTitle") or "" for h in hist
                     if h.get("eventType") == "grabbed" and h.get("downloadId")}
            for h in hist:
                d = h.get("data") or {}
                if h.get("eventType") != "downloadFolderImported" or not d.get("fileId"):
                    continue
                dropped = d.get("droppedPath") or ""
                titles.setdefault(str(d["fileId"]), [grabs.get(h.get("downloadId"), ""), h.get("sourceTitle") or "",
                                                      os.path.basename(os.path.dirname(dropped)), dropped])
        with cursor() as cur:
            for r in rs:
                g = clean(None, *titles.get(str(r["arr_file_id"] or ""), []))
                cur.execute("UPDATE files SET release_group=? WHERE id=?", (g, r["id"]))
                recovered += bool(g)
                cleared += not g
    # THE SCORECARD'S MEASUREMENTS follow their file's new name, so "GHOST]["
    # and "GHOST" are one group's samples. A cleared file's samples keep the
    # old word but drop out: the scorecard only ranks names files still carry.
    with cursor() as cur:
        try:
            cur.execute("UPDATE group_measure SET grp=(SELECT release_group FROM files f WHERE f.id=group_measure.file_id) "
                        "WHERE EXISTS (SELECT 1 FROM files f WHERE f.id=group_measure.file_id "
                        "AND f.release_group IS NOT NULL AND f.release_group!=group_measure.grp)")
        except Exception:                                        # noqa: BLE001
            pass                                                 # no scorecard table yet
    kv_set(KV_DONE, "1")
    res = {"tidied": tidied, "recovered": recovered, "cleared": cleared}
    joblog.log(f"release groups repaired: {tidied} tidied, {recovered} recovered from history, "
               f"{cleared} cleared (no real name)", "ok")
    return res
