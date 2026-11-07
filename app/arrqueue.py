r"""The queue janitor: downloads the arrs have finished with but not let go of.

WHAT IT CLEANS. Erik, 2026-09-27: eight entries in Sonarr's queue, every one
"completed", every one carrying a warning, none going anywhere - and the
folders in F:\Ready empty. Two shapes:

  * "Not a Custom Format upgrade for existing episode file(s)" - Sonarr took
    a release, a better one landed first (or nuarr's rewrite lifted the
    existing file's score), and now the download it already fetched is not
    worth importing. The arr will never import it and never removes it.
  * "No files found are eligible for import in <folder>" with the folder
    empty and the episode already holding a file - the import DID happen
    (through nuarr's import script), the tracking of the download just never
    closed, so the entry sits at "import pending" forever.

Both leave a finished download in the client - a stopped torrent in
qBittorrent, a completed job in SABnzbd - and a folder in the download
directory. The arr's own "remove completed downloads" only fires on a
download the arr counts as imported, which neither of these is.

WHAT IT DOES. Every few minutes, per arr: the queue is read, and an entry
that is finished downloading and carries one of those verdicts is removed
through the arr's own API with removeFromClient=true - so the arr deletes it
from whichever download client it came from, files included, exactly as it
would after an import. Nothing is blocklisted: the release was not bad, it
was late. Then the clients themselves are swept for finished items in the
arrs' categories that no arr is tracking any more (the arr let go, the
client did not), and those are removed with their files too.

DYNAMIC, as asked. The clients are not configured here. They are read from
each arr's own download-client list - host, port, credentials, category -
so a client the arrs are moved to is a client this sweeps, with nothing to
update on this side.

WHAT IT LEAVES ALONE. Anything still downloading. Anything the arr is still
willing to import (no verdict, or a verdict this does not recognise - an
unknown series, a missing episode match - which a person should look at).
Anything in a client outside the arrs' categories.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import joblog
from .db import kv_get, kv_set

POLL_S = 5 * 60
KV_RECENT = "arrqueue.recent"
RECENT_KEEP = 40

# THE VERDICTS THAT MEAN "THE ARR IS DONE WITH THIS". Matched on the arr's
# own status messages, case-insensitively, as substrings - the wording is
# Sonarr's and Radarr's and has been stable for years.
DONE_WITH = (
    "not a custom format upgrade",
    "not an upgrade for existing",
    "not a quality upgrade",
    "already imported",
    "episode file already imported",
    "movie file already imported",
    "has the same",                     # "... has the same quality/score"
)
# "No files found are eligible for import in X" is only a done-with verdict
# when X is empty (or gone) AND the arr already holds a file for the item -
# otherwise it is a real problem and stays for a person.
NOTHING_LEFT = "no files found are eligible for import"

# MALWARE DRESSED AS AN EPISODE. Erik, 2026-09-29: five "1080p WEB H264"
# releases for five different shows (Nyaa, LimeTorrents), every one a single
# 869,211,326-byte .exe under a real group's name. The arr notices ("Caution:
# Found executable file") and then does nothing - the entry sits in the queue
# and the .exe sits in F:\Ready. A title rule cannot catch these (the names are
# NTb, FLUX, MeGusta - groups whose real releases are wanted), so the check is
# on what the download actually holds: the torrent's own file list, read from
# the client as soon as the metadata is in - while it is still downloading,
# not only once it finishes. These alone are removed WITH the blocklist, so
# the arr never takes that release again and goes looking for another.
EXEC_EXT = (".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".lnk",
            ".vbs", ".js", ".jse", ".wsf", ".ps1", ".hta", ".jar", ".dll")
# DISK IMAGES DRESSED AS AN EPISODE. Erik, 2026-11-02: six "1080p WEB H264"
# releases (EZTV, MeGusta, CAKES via LimeTorrents), each one ~1 GB .iso. No
# WEB episode ships as a disk image, and the arr just parks them "unable to
# parse". Treated exactly like the programs above: removed and blocklisted.
EXEC_EXT = EXEC_EXT + (".iso", ".img", ".dmg", ".vhd", ".vhdx", ".wim")
EXEC_SAYS = ("found executable file", "potentially dangerous file")

# NOT A VIDEO AT ALL. Erik, 2026-11-07: two "Scavengers Reign ... HONE" jobs
# from SABnzbd, each one 1.3 GB "...mkv.bin" - one an rclone-encrypted blob
# (it starts "RCLONE"), the other all zeros. Sonarr said "Unable to determine
# if file is a sample" and parked them; the janitor counted them "kept for a
# person" every five minutes. What a file IS is read from its first bytes,
# not its name: a real video under a wrong name (".mkv.bin" that starts like
# Matroska) is renamed and the arr asked to import the folder again; a
# download with no real video in it is removed and blocklisted so the arr
# looks for another release; junk beside a real video (nfo, txt, sfv, jpg,
# .bin) is moved to the Recycle Bin, subtitles are kept.
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".webm", ".mpg", ".mpeg")
SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi")
LANDED_SAYS = ("unable to determine if file is a sample", "no files found are eligible for import",
               "invalid video file", "not a video", "sample")


def _sniff(path: str) -> str:
    """The container the first bytes say this is ('.mkv', '.mp4', ...) or ''."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(400)
    except OSError:
        return ""
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".mkv"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi"
    if head[:4] == b"\x30\x26\xb2\x75":
        return ".wmv"
    if head[:4] == b"\x00\x00\x01\xba":
        return ".mpg"
    if len(head) >= 377 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return ".ts"
    return ""


def _landed(a, row: dict) -> tuple[str, dict | None]:
    """A finished download the arr parked over what its files are.
    -> (why it was removed, or ''), (a recent-list row for a rename/clean-up, or None)."""
    if (row.get("status") or "") != "completed" or (row.get("sizeleft") or 0) > 0:
        return "", None
    if (row.get("trackedDownloadStatus") or "") != "warning":
        return "", None
    low = " | ".join(m for s_ in (row.get("statusMessages") or [])
                     for m in ([s_.get("title") or ""] + list(s_.get("messages") or []))).lower()
    if not any(k in low for k in LANDED_SAYS):
        return "", None
    out = (row.get("outputPath") or "").rstrip("\\/")
    if not out or not os.path.exists(out):
        return "", None
    files = [out] if os.path.isfile(out) else [os.path.join(r_, f) for r_, _d, fs in os.walk(out) for f in fs]
    real, junk, renamed = [], [], []
    for p in files:
        ext = os.path.splitext(p)[1].lower()
        is_ = _sniff(p)
        if is_:
            if ext in VIDEO_EXT:
                real.append(p)
                continue
            # a real video under a wrong name: "X.mkv.bin" -> "X.mkv"
            stem, _e = os.path.splitext(p)
            new = stem if os.path.splitext(stem)[1].lower() in VIDEO_EXT else stem + is_
            try:
                if not os.path.exists(new):
                    os.rename(p, new)
                    renamed.append(os.path.basename(new))
                    real.append(new)
            except OSError as e:
                STATS["detail"].append(f"{a.name}: could not rename {os.path.basename(p)[:50]}: {e}"[:160])
                real.append(p)
            continue
        if ext in SUB_EXT:
            continue
        junk.append(p)
    if not real:
        names = ", ".join(os.path.basename(p)[-50:] for p in junk[:2]) or "nothing"
        return (f"not a video: the download held only {names} - removed and blocklisted, "
                "the arr will look for another release"), None
    if not renamed and not junk:
        return "", None
    from . import fileops
    gone = []
    for p in junk:
        try:
            r = fileops.recycle(p)
            if getattr(r, "ok", False):
                gone.append(os.path.basename(p))
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not recycle {os.path.basename(p)[:50]}: {e}"[:160])
    try:
        cmd = "DownloadedEpisodesScan" if a.kind == "sonarr" else "DownloadedMoviesScan"
        body = {"name": cmd, "path": out if os.path.isdir(out) else os.path.dirname(out), "importMode": "Move"}
        if row.get("downloadId"):
            body["downloadClientId"] = row["downloadId"]
        _call(a, "/api/v3/command", "POST", body)
    except Exception as e:                                       # noqa: BLE001
        STATS["detail"].append(f"{a.name}: could not ask for a new import of {out[-50:]}: {e}"[:160])
    bits = []
    if renamed:
        bits.append("renamed " + ", ".join(renamed)[:120] + " (a real video under a wrong name)")
    if gone:
        bits.append(f"{len(gone)} non-media file(s) moved to the Recycle Bin")
    why = "; ".join(bits) + " - the arr was asked to import the folder again"
    STATS["detail"].append(f"{a.name}: {(row.get('title') or '')[:60]} - {why}")
    return "", {"at": time.time(), "arr": a.name, "title": row.get("title") or "",
                "client": row.get("downloadClient") or "", "why": why, "kind": "cleaned"}

STATS: dict = {"last_run": 0.0, "last_result": "", "next_run": 0.0,
               "removed": 0, "swept": 0, "kept": 0, "detail": [], "running": False}
KV_LAST = "arrqueue.last"     # last run survives a restart, so the card is not blank

# A DOWNLOAD GOING NOWHERE. A torrent with no seeds sits in the arr's queue
# as "downloading" for as long as anyone lets it; the arrs do nothing about
# it. One that has made no progress for STALL_H hours while qBittorrent
# calls it stalled is removed, blocklisted, and the arr searches again.
# Progress is remembered between passes, so a slow-but-moving torrent is
# not a stalled one.
STALL_H = 2.0
KV_STALL = "arrqueue.stall"   # {hash: {"p": progress, "since": epoch}}


def _stalled(arrs, ids: set) -> dict:
    """{download id: hours stalled} for torrents past STALL_H."""
    if not ids:
        return {}
    try:
        mem = json.loads(kv_get(KV_STALL) or "{}")
    except Exception:                                            # noqa: BLE001
        mem = {}
    now = time.time()
    seen: dict = {}
    for c in _clients(arrs):
        if c["impl"] != "QBittorrent":
            continue
        try:
            q = _QBit(c)
            if q.login():
                seen.update(q.stalled(ids))
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{c['name']}: could not read states: {type(e).__name__}: {e}"[:160])
    out: dict = {}
    for h, p in seen.items():
        m = mem.get(h)
        if not m or float(m.get("p") or 0) < p:
            mem[h] = {"p": p, "since": now}
            continue
        hrs = (now - float(m.get("since") or now)) / 3600
        if hrs >= STALL_H:
            out[h] = hrs
    # a torrent no longer stalled (or gone) starts over next time it stalls
    mem = {h: v for h, v in mem.items() if h in seen}
    try:
        kv_set(KV_STALL, json.dumps(mem))
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ------------------------------------------------------------ the arrs -----
def _call(a, path: str, method: str = "GET", body=None, timeout: float = 60.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(a.url.rstrip("/") + path, data=data, method=method,
                                 headers={"X-Api-Key": a.api_key,
                                          "Content-Type": "application/json"})
    t = urllib.request.urlopen(req, timeout=timeout).read().decode()
    return json.loads(t) if t.strip() else {}


def _queue(a) -> list:
    q = _call(a, "/api/v3/queue?page=1&pageSize=500&includeUnknownSeriesItems=true"
                 "&includeUnknownMovieItems=true&includeSeries=false&includeEpisode=false"
                 "&includeMovie=false")
    return q.get("records") or []


def _has_file(a, row: dict) -> bool:
    """Does the arr already hold a file for what this download was for?"""
    try:
        if a.kind == "sonarr" and row.get("episodeId"):
            return bool(_call(a, f"/api/v3/episode/{row['episodeId']}").get("hasFile"))
        if a.kind == "radarr" and row.get("movieId"):
            return bool(_call(a, f"/api/v3/movie/{row['movieId']}").get("hasFile"))
    except Exception:                                            # noqa: BLE001
        pass
    return False


def _folder_empty(path: str) -> bool:
    """No video left under it (a folder that is gone counts as empty)."""
    if not path:
        return False
    p = path.rstrip("\\/")
    if not os.path.exists(p):
        return True
    if os.path.isfile(p):
        return False
    for root, _d, files in os.walk(p):
        for f in files:
            if os.path.splitext(f)[1].lower() in (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm"):
                return False
    return True


def _verdict(a, row: dict) -> str:
    """Why this entry can go, or '' to leave it."""
    if (row.get("status") or "") != "completed":
        return ""
    if (row.get("sizeleft") or 0) > 0:
        return ""
    msgs = " | ".join(m for s in (row.get("statusMessages") or [])
                      for m in ([s.get("title") or ""] + list(s.get("messages") or [])))
    low = msgs.lower()
    for k in DONE_WITH:
        if k in low:
            return "the arr will not import it: " + (
                "not an upgrade on the file it already has" if "upgrade" in k or "same" in k
                else "already imported")
    if NOTHING_LEFT in low and _folder_empty(row.get("outputPath") or "") and _has_file(a, row):
        return "already imported - the download folder is empty and the arr has the file"
    return ""


# ----------------------------------------- names the arr could not read --
# "[Hakata Ramen] Pokemon Movie 09 - Pokmon Ranger and the Temple of the Sea"
# finished downloading and sat in Radarr's queue as "Unable to parse release":
# the arr does not know what it is, so it never imports it and never lets it
# go. Qwen reads the name; the file is imported only when that reading matches
# exactly ONE film (title and year) or ONE episode (series, season and
# episode) the arr has, and the arr itself offers the file for it. Anything
# else stays for a person. Each download is tried once.
UNREAD_SAYS = ("unable to parse", "unknown series", "unknown movie", "not parsed",
               "could not be parsed", "manual import required", "unknown episode",
               "automatic import is not possible")      # Sonarr's wording of "matched by id"
KV_PARSED = "arrqueue.parsed"


def _unread(a, row: dict) -> bool:
    if (row.get("status") or "") != "completed" or (row.get("sizeleft") or 0) > 0:
        return False
    if a.kind == "sonarr" and not row.get("seriesId"):
        return True
    if a.kind == "radarr" and not row.get("movieId"):
        return True
    low = " | ".join(m for s in (row.get("statusMessages") or [])
                     for m in ([s.get("title") or ""] + list(s.get("messages") or []))).lower()
    return any(k in low for k in UNREAD_SAYS)


def _norm(t) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def _match(a, p: dict) -> dict | None:
    """The one item in this arr Qwen's reading names, or None."""
    want = _norm(p.get("title"))
    if not want:
        return None
    if a.kind == "radarr":
        hits = []
        for m in _call(a, "/api/v3/movie") or []:
            names = {_norm(m.get("title")), _norm(m.get("originalTitle"))} | {
                _norm(x.get("title")) for x in (m.get("alternateTitles") or [])}
            if want in names and (not p.get("year") or abs(int(m.get("year") or 0) - int(p["year"])) <= 1):
                hits.append(m)
        return {"movieId": hits[0]["id"], "what": f"{hits[0]['title']} ({hits[0].get('year')})"} \
            if len(hits) == 1 else None
    if p.get("season") is None or p.get("episode") is None:
        return None
    hits = [s_ for s_ in (_call(a, "/api/v3/series") or [])
            if want in ({_norm(s_.get("title"))} | {_norm(x.get("title")) for x in (s_.get("alternateTitles") or [])})]
    if len(hits) != 1:
        return None
    eps = [e for e in (_call(a, f"/api/v3/episode?seriesId={hits[0]['id']}") or [])
           if e.get("seasonNumber") == int(p["season"]) and e.get("episodeNumber") == int(p["episode"])]
    if len(eps) != 1:
        return None
    return {"seriesId": hits[0]["id"], "episodeIds": [eps[0]["id"]],
            "what": f"{hits[0]['title']} S{int(p['season']):02d}E{int(p['episode']):02d}"}


def _import_unread(items: list) -> list:
    """items: [(arr, row)]. -> rows for the recent list."""
    import subprocess
    from .config import NO_WINDOW
    from .db import kv_get, kv_set
    from . import heavy
    try:
        tried = set(json.loads(kv_get(KV_PARSED) or "[]"))
    except Exception:                                            # noqa: BLE001
        tried = set()
    items = [(a, r) for a, r in items if str(r.get("downloadId") or r.get("id")) not in tried]
    if not items:
        return []
    got, holder = heavy.claim("Qwen release names", ttl=900)
    if not got:
        STATS["detail"].append(f"unreadable release names wait - {holder} has the GPU lane")
        return []
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-m", "app.qwentools", "--parse-names"],
                           input=json.dumps([row.get("title") or "" for _a, row in items]),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=root, timeout=600, creationflags=NO_WINDOW)
    finally:
        heavy.release("Qwen release names")
    parsed = []
    for ln in (r.stdout or "").splitlines():
        try:
            d = json.loads(ln)
        except Exception:                                        # noqa: BLE001
            continue
        parsed = d.get("parsed") or parsed
    rows = []
    for (a, row), p in zip(items, parsed):
        tried.add(str(row.get("downloadId") or row.get("id")))
        name = row.get("title") or ""
        hit = None
        try:
            hit = _match(a, p)
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not look up {name[:50]}: {type(e).__name__}")
        if not hit:
            STATS["detail"].append(f"{a.name}: {name[:60]} - read as {p.get('title')!r}"
                                   f"{' S%sE%s' % (p.get('season'), p.get('episode')) if p.get('episode') is not None else ''}"
                                   f", which matches no single item here - left for you")
            continue
        try:
            # BY THE DOWNLOAD ALONE - with a seriesId Sonarr lists the series
            # folder instead (see bitcut.import_wanted).
            q = urllib.parse.urlencode({"downloadId": row.get("downloadId") or "",
                                        "filterExistingFiles": "false"})
            cands = [c for c in (_call(a, "/api/v3/manualimport?" + q) or [])
                     if os.path.splitext(c.get("path") or "")[1].lower() in (".mkv", ".mp4", ".avi", ".m4v", ".ts")]
            if len(cands) != 1:
                STATS["detail"].append(f"{a.name}: {name[:60]} - {len(cands)} video files offered, left for you")
                continue
            c = cands[0]
            f = {"path": c["path"], "quality": c.get("quality"), "languages": c.get("languages") or [],
                 "releaseGroup": c.get("releaseGroup") or "", "downloadId": row.get("downloadId") or ""}
            f.update({k: hit[k] for k in ("seriesId", "episodeIds", "movieId") if k in hit})
            _call(a, "/api/v3/command", "POST", {"name": "ManualImport", "files": [f], "importMode": "auto"})
            rows.append({"at": time.time(), "arr": a.name, "title": name, "client": row.get("downloadClient") or "",
                         "why": f"the arr could not read this name; Qwen read it as {hit['what']}, the only match "
                                f"here, and it was imported", "kind": "parsed"})
            STATS["detail"].append(f"{a.name}: imported {name[:60]} as {hit['what']} (name read by Qwen)")
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not import {name[:60]}: {type(e).__name__}: {e}"[:160])
    kv_set(KV_PARSED, json.dumps(sorted(tried)[-500:]))
    return rows


def _video_cands(a, row: dict) -> list:
    # BY THE DOWNLOAD ALONE - with a seriesId Sonarr lists the series folder
    # instead (see bitcut.import_wanted).
    q = urllib.parse.urlencode({"downloadId": row.get("downloadId") or "", "filterExistingFiles": "false"})
    return [c for c in (_call(a, "/api/v3/manualimport?" + q) or [])
            if os.path.splitext(c.get("path") or "")[1].lower() in (".mkv", ".mp4", ".avi", ".m4v", ".ts")]


def _parse_agrees(a, row: dict) -> str:
    """'' when the arr's own parser ties this release name to the row's
    series + episode (or movie); otherwise why not. For a film the parser
    cannot place at all (Fate/stay night: 'Unknown Movie'), the year in the
    name has to be the film's - that is as far as trust goes."""
    name = row.get("title") or ""
    p = _call(a, "/api/v3/parse?" + urllib.parse.urlencode({"title": name})) or {}
    if a.kind == "sonarr":
        sid = (p.get("series") or {}).get("id")
        eps = {e.get("id") for e in p.get("episodes") or []}
        if sid == row.get("seriesId") and row.get("episodeId") in eps:
            return ""
        if sid is None:
            return "the arr's parser finds no series in this name"
        if sid != row.get("seriesId"):
            return f"the arr's parser reads it as another series ({(p.get('series') or {}).get('title')})"
        return "the arr's parser reads it as a different episode"
    mid = (p.get("movie") or {}).get("id")
    if mid == row.get("movieId"):
        return ""
    if mid is not None:
        return f"the arr's parser reads it as another film ({(p.get('movie') or {}).get('title')})"
    m = _call(a, f"/api/v3/movie/{row['movieId']}") or {}
    yr = str(m.get("year") or "")
    if yr and yr in name:
        return ""
    return f"the arr's parser finds no film in this name and it does not carry the year ({yr})"


def _import_known(items: list) -> list:
    """"Found matching movie via grab history, but release was matched to
    movie by ID. Manual Import required." The arr KNOWS which movie (or
    episode) it is - the queue row carries the id - it only will not import
    on its own. Fate/stay night HF-I sat completed for a day with that.
    Imported here with the row's own ids: one video in the download only."""
    rows = []
    gone: set = set()
    for a, row in items:
        low = " | ".join(m for s in (row.get("statusMessages") or [])
                         for m in list(s.get("messages") or [])).lower()
        if "matched to" not in low or "by id" not in low:
            continue
        ids = {"movieId": row["movieId"]} if a.kind == "radarr" and row.get("movieId") else \
              {"seriesId": row["seriesId"], "episodeIds": [row["episodeId"]]} \
              if a.kind == "sonarr" and row.get("seriesId") and row.get("episodeId") else None
        name = row.get("title") or ""
        if not ids:
            continue
        did = str(row.get("downloadId") or "").upper()
        if did and did in gone:
            row["_nuarr_imported"] = True            # its pack went with an earlier row
            continue
        # THE ID CAN BE WRONG. An episode search for Detective Conan brought
        # back its movies, and a 2025 special series "Detective Conan vs The
        # Black Organization", each tied by the indexer to S01E03, S01E05...
        # Sonarr's own parser finds NO series in those names - which is why
        # it would not import them. Imported by the row's id they would have
        # become S01E03. So the arr's parser has to agree with the row; when
        # it does not, the grab was a mis-match: removed and blocklisted, or
        # the next search grabs it again.
        try:
            verdict = _parse_agrees(a, row)
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not parse {name[:60]}: {type(e).__name__}")
            continue
        if verdict:
            try:
                _remove(a, row, blocklist=True)
            except urllib.error.HTTPError as he:
                if he.code != 404:
                    raise
            if did:
                gone.add(did)
            row["_nuarr_imported"] = True
            rows.append({"at": time.time(), "arr": a.name, "title": name, "client": row.get("downloadClient") or "",
                         "why": f"a mis-matched grab: {verdict} - removed and blocklisted so it is not grabbed again",
                         "kind": "misgrab"})
            STATS["detail"].append(f"{a.name}: removed mis-matched grab {name[:60]} - {verdict}")
            continue
        try:
            cands = _video_cands(a, row)
            if len(cands) != 1:
                STATS["detail"].append(f"{a.name}: {name[:60]} - {len(cands)} video files offered, left for you")
                continue
            c = cands[0]
            f = {"path": c["path"], "quality": c.get("quality"), "languages": c.get("languages") or [],
                 "releaseGroup": c.get("releaseGroup") or "", "downloadId": row.get("downloadId") or ""}
            f.update(ids)
            _call(a, "/api/v3/command", "POST", {"name": "ManualImport", "files": [f], "importMode": "auto"})
            row["_nuarr_imported"] = True
            rows.append({"at": time.time(), "arr": a.name, "title": name, "client": row.get("downloadClient") or "",
                         "why": "the arr knew what it was (matched by id from its grab) but asked for a manual "
                                "import - imported with that id", "kind": "parsed"})
            STATS["detail"].append(f"{a.name}: imported {name[:60]} by the id the arr matched it to")
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not import {name[:60]}: {type(e).__name__}: {e}"[:160])
    return rows


# ------------------------------------- refused for its shape, not its worth --
# "Episode file on disk contains more episodes than this file contains": a
# 2160p single refused because a 1080p DOUBLE (E09-E10) sits on disk. "Episode
# 1x05 was not found in the grabbed release": Sonarr's parser and its own
# grab disagree about a name. Neither says the file is worse - Sonarr simply
# will not decide. So it is decided here, the way Sonarr would for a clean
# import: better quality in the series' profile, or the same quality and a
# higher custom-format score, than EVERY file it would replace -> imported
# with the arr's own mapping (ManualImport ignores the shape check). Not
# better than any of them -> removed without a blocklist, exactly as the
# arr's own "not an upgrade" rows are. Mixed, or no mapping -> left for you.
SHAPE_SAYS = ("contains more episodes than", "was not found in the grabbed release")


def _shape_refused(row: dict) -> bool:
    low = " | ".join(m for s in (row.get("statusMessages") or [])
                     for m in list(s.get("messages") or [])).lower()
    return any(k in low for k in SHAPE_SAYS)


def _profile_rank(a, profile_id: int, cache: dict) -> dict:
    """quality name -> position in the profile, low to high."""
    if profile_id in cache:
        return cache[profile_id]
    names: list = []
    for it in (_call(a, f"/api/v3/qualityprofile/{int(profile_id)}") or {}).get("items") or []:
        if it.get("quality"):
            names.append((it["quality"] or {}).get("name") or "")
        else:
            names.extend(((q.get("quality") or {}).get("name") or "") for q in (it.get("items") or []))
    cache[profile_id] = {n: i for i, n in enumerate(names)}
    return cache[profile_id]


def _import_better(queues: list) -> tuple[list, set]:
    """-> (rows for the recent list, queue ids removed here)."""
    rows: list = []
    gone_ids: set = set()
    for a, qrows in queues:
        if a.kind != "sonarr":
            continue
        groups: dict = {}
        for r in qrows:
            if (r.get("trackedDownloadStatus") or "") != "warning" or not _shape_refused(r):
                continue
            if (r.get("status") or "") != "completed" or (r.get("sizeleft") or 0) > 0:
                continue
            if r.get("downloadId"):
                groups.setdefault(str(r["downloadId"]), []).append(r)
        ranks: dict = {}
        for did, grp in groups.items():
            name = grp[0].get("title") or ""
            have = ""
            try:
                cands = _video_cands(a, grp[0])
                series = grp[0].get("series") or {}
                sid = grp[0].get("seriesId") or series.get("id")
                if not cands or not sid:
                    STATS["detail"].append(f"{a.name}: {name[:60]} - nothing the arr would offer for import, left for you")
                    continue
                series = series or (_call(a, f"/api/v3/series/{int(sid)}") or {})
                rank = _profile_rank(a, int(series.get("qualityProfileId") or 0), ranks)
                eps = {int(e["id"]): e for e in (_call(a, f"/api/v3/episode?seriesId={int(sid)}") or [])}
                files = {int(f["id"]): f for f in (_call(a, f"/api/v3/episodefile?seriesId={int(sid)}") or [])}
                plan: list = []
                better = worse = 0
                for c in cands:
                    ep_ids = [int(e["id"]) for e in (c.get("episodes") or []) if e.get("id")]
                    if not ep_ids and len(cands) == 1 and len(grp) == 1 and grp[0].get("episodeId"):
                        ep_ids = [int(grp[0]["episodeId"])]       # the row's own episode, one file
                    if not ep_ids:
                        plan = []
                        break
                    cq = ((c.get("quality") or {}).get("quality") or {}).get("name") or ""
                    ccf = int(c.get("customFormatScore") or grp[0].get("customFormatScore") or 0)
                    for eid in ep_ids:
                        fid = (eps.get(eid) or {}).get("episodeFileId") or 0
                        f = files.get(int(fid)) if fid else None
                        if not f:
                            better += 1
                            continue
                        fq = ((f.get("quality") or {}).get("quality") or {}).get("name") or ""
                        fcf = int(f.get("customFormatScore") or 0)
                        if rank.get(cq, -1) > rank.get(fq, -1) or (rank.get(cq, -1) == rank.get(fq, -1) and ccf > fcf):
                            better += 1
                        else:
                            worse += 1
                            have = f"{fq} score {fcf}"
                    plan.append({"path": c["path"], "quality": c.get("quality"), "languages": c.get("languages") or [],
                                 "releaseGroup": c.get("releaseGroup") or "", "downloadId": did,
                                 "seriesId": int(sid), "episodeIds": ep_ids})
                if not plan:
                    STATS["detail"].append(f"{a.name}: {name[:60]} - the arr cannot say which episode a file is, left for you")
                    continue
                said = "; ".join(sorted({m for r in grp for s in (r.get("statusMessages") or [])
                                         for m in (s.get("messages") or [])}))[:120]
                if better and not worse:
                    _call(a, "/api/v3/command", "POST", {"name": "ManualImport", "files": plan, "importMode": "auto"})
                    for r in grp:
                        r["_nuarr_imported"] = True
                    rows.append({"at": time.time(), "arr": a.name, "title": name, "client": grp[0].get("downloadClient") or "",
                                 "why": f"the arr refused it for its shape ({said}), not its worth - better than the "
                                        f"{better} file(s) it replaces, so imported", "kind": "better"})
                    STATS["detail"].append(f"{a.name}: imported {name[:60]} - better than what was on disk")
                elif worse and not better:
                    for r in grp:
                        try:
                            _remove(a, r, blocklist=False)
                        except urllib.error.HTTPError as he:
                            if he.code != 404:
                                raise
                        gone_ids.add(r.get("id"))
                    rows.append({"at": time.time(), "arr": a.name, "title": name, "client": grp[0].get("downloadClient") or "",
                                 "why": f"not better than the file already here ({have}) - removed, not blocklisted",
                                 "kind": ""})
                    STATS["detail"].append(f"{a.name}: removed {name[:60]} - not better than the file on disk")
                else:
                    STATS["detail"].append(f"{a.name}: {name[:60]} - better than some of the files it replaces and not "
                                           f"others, left for you")
            except Exception as e:                               # noqa: BLE001
                STATS["detail"].append(f"{a.name}: could not judge {name[:60]}: {type(e).__name__}: {e}"[:160])
    return rows, gone_ids


def _remove(a, row: dict, blocklist: bool = False) -> None:
    _call(a, f"/api/v3/queue/{row['id']}?removeFromClient=true"
             f"&blocklist={'true' if blocklist else 'false'}&skipRedownload=false", "DELETE")


def _malware(row: dict, exe: dict) -> str:
    """Why this download is an executable, or ''. `exe` is what the client's
    file lists found (download id -> file name); the arr's own caution is the
    second opinion for a client this cannot read (SABnzbd)."""
    f = exe.get(str(row.get("downloadId") or "").upper())
    if f:
        return f"malware: the download is {f}, not a video"
    low = " | ".join(m for s in (row.get("statusMessages") or [])
                     for m in list(s.get("messages") or [])).lower()
    for k in EXEC_SAYS:
        if k in low:
            m = re.search(r"extension:\s*'([^']+)'", low)
            return f"malware: the arr found an executable ({m.group(1) if m else 'unknown type'}) in it"
    # SABNZBD HANDS OVER THE PROGRAM ITSELF. A fake "Marshals S02E03 ... -CAKES"
    # unpacks to "F:\Ready\Marshals S02E03 ... -CAKES.exe"; Sonarr says only
    # "No files found are eligible for import in ...exe" and the row sat there,
    # "kept for a person", because neither phrase above appears.
    for p in [row.get("outputPath") or ""] + re.findall(r"[a-z]:\\[^|]*", low):
        ext = os.path.splitext(p.strip().rstrip("\\/"))[1].lower()
        if ext in EXEC_EXT:
            return f"malware: the download is {os.path.basename(p.strip())}, not a video"
    return ""


def _exe_downloads(arrs, ids: set) -> dict:
    """{download id: file name} for torrents whose biggest file is a program.
    Only the torrents the arrs' queues point at are opened."""
    out: dict = {}
    if not ids:
        return out
    for c in _clients(arrs):
        if c["impl"] != "QBittorrent":
            continue
        try:
            q = _QBit(c)
            if not q.login():
                continue
            for t in json.loads(q._req("/torrents/info") or "[]"):
                h = str(t.get("hash") or "").upper()
                if h not in ids:
                    continue
                files = json.loads(q._req("/torrents/files?hash=" + t["hash"]) or "[]")
                if not files:                          # metadata not in yet
                    continue
                big = max(files, key=lambda f: f.get("size") or 0)
                name = str(big.get("name") or "")
                if os.path.splitext(name)[1].lower() in EXEC_EXT:
                    out[h] = os.path.basename(name.replace("\\", "/"))
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{c['name']}: could not read file lists: {type(e).__name__}: {e}"[:160])
    return out


# ------------------------------------------------------- the clients -------
def _clients(arrs) -> list:
    """Every enabled download client the arrs use, with the arr categories
    it holds - read from the arrs, never configured here."""
    out: dict = {}
    for a in arrs:
        try:
            for c in _call(a, "/api/v3/downloadclient"):
                if not c.get("enable"):
                    continue
                f = {x["name"]: x.get("value") for x in c.get("fields") or []}
                key = (c.get("implementation"), str(f.get("host") or "").lower(), f.get("port"))
                d = out.setdefault(key, {"impl": c.get("implementation"), "name": c.get("name"),
                                         "host": f.get("host"), "port": f.get("port"),
                                         "ssl": bool(f.get("useSsl")), "base": f.get("urlBase") or "",
                                         "user": f.get("username") or "", "pw": f.get("password") or "",
                                         "apikey": f.get("apiKey") or "", "cats": set(), "arrs": set()})
                for k in ("tvCategory", "movieCategory", "musicCategory", "bookCategory"):
                    if f.get(k):
                        d["cats"].add(str(f[k]).lower())
                d["arrs"].add(a.name)
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not read its download clients: "
                                   f"{type(e).__name__}: {e}"[:160])
    # THE ARRS HIDE THEIR SECRETS. The download-client list comes back with
    # every password and API key masked ("********"), so SABnzbd answered
    # "API Key Incorrect" to the first sweep. qBittorrent lets this server in
    # without a password (its auth whitelist), and SABnzbd's real key is in
    # its own sabnzbd.ini when it runs on this machine - the key is read from
    # there. A client whose secret cannot be found is reported, not guessed.
    for c in out.values():
        if c["impl"] == "Sabnzbd" and _masked(c["apikey"]):
            k = _sab_key_local(c["host"])
            if k:
                c["apikey"] = k
            else:
                STATS["detail"].append(f"{c['name']}: the arr hides its API key and no "
                                       f"local sabnzbd.ini was found - the client sweep skips it")
                c["skip"] = True
    return [c for c in out.values() if not c.get("skip")]


def _masked(v: str) -> bool:
    v = str(v or "")
    return (not v) or set(v) <= {"*"} or len(v) < 16


def _sab_key_local(host: str) -> str:
    """SABnzbd's own api_key, when it runs on this machine."""
    import glob
    import re
    import socket
    h = str(host or "").lower()
    local = h in ("127.0.0.1", "localhost", "::1") or h == socket.gethostname().lower()
    if not local:
        try:
            local = h in {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)}
        except Exception:                                        # noqa: BLE001
            local = False
    if not local:
        return ""
    for ini in (glob.glob(r"C:\Users\*\AppData\Local\sabnzbd\sabnzbd.ini")
                + glob.glob(r"C:\ProgramData\sabnzbd\sabnzbd.ini")):
        try:
            m = re.search(r"^api_key\s*=\s*(\S+)", open(ini, encoding="utf-8", errors="replace").read(), re.M)
            if m:
                return m.group(1).strip()
        except OSError:
            continue
    return ""


def _url(c: dict) -> str:
    base = ("/" + c["base"].strip("/")) if c["base"] else ""
    return f"http{'s' if c['ssl'] else ''}://{c['host']}:{c['port']}{base}"


class _QBit:
    def __init__(self, c: dict):
        self.u = _url(c)
        self.jar = ""
        self.c = c

    def _req(self, path, data=None):
        req = urllib.request.Request(self.u + "/api/v2" + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Referer": self.u, "Cookie": self.jar,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            sc = r.headers.get("Set-Cookie") or ""
            if "SID=" in sc:
                self.jar = sc.split(";")[0]
            return r.read().decode()

    def login(self):
        t = self._req("/auth/login", urllib.parse.urlencode(
            {"username": self.c["user"], "password": self.c["pw"]}).encode())
        return t.strip() == "Ok."

    def finished(self, cats: set) -> list:
        # STOPPED, NOT MERELY DOWNLOADED. This used to count "uploading" and
        # "stalledUP" as finished, and the arr drops a torrent from its queue
        # the moment it imports - so every torrent was deleted ten minutes
        # after it completed and the seeding limits set in qBittorrent never
        # got a say. qBittorrent stops a torrent itself when its ratio or
        # seeding time is reached; that is the signal this waits for.
        rows = json.loads(self._req("/torrents/info") or "[]")
        return [t for t in rows if str(t.get("category") or "").lower() in cats
                and t.get("progress", 0) >= 1.0
                and t.get("state") in ("stoppedUP", "pausedUP")]

    def stalled(self, ids: set) -> dict:
        """{hash: progress} for the arrs' torrents that qBittorrent calls
        stalled (no peers sending anything) or still waiting for metadata."""
        out = {}
        for t in json.loads(self._req("/torrents/info") or "[]"):
            h = str(t.get("hash") or "").upper()
            if h in ids and t.get("state") in ("stalledDL", "metaDL"):
                out[h] = float(t.get("progress") or 0.0)
        return out

    def remove(self, hashes: list):
        self._req("/torrents/delete", urllib.parse.urlencode(
            {"hashes": "|".join(hashes), "deleteFiles": "true"}).encode())


class _Sab:
    def __init__(self, c: dict):
        self.u = _url(c)
        self.k = c["apikey"]

    def _get(self, **q):
        q.update(apikey=self.k, output="json")
        with urllib.request.urlopen(self.u + "/api?" + urllib.parse.urlencode(q), timeout=30) as r:
            return json.loads(r.read().decode())

    def finished(self, cats: set) -> list:
        h = self._get(mode="history", limit=200).get("history") or {}
        return [s for s in h.get("slots") or []
                if str(s.get("category") or "").lower() in cats
                and str(s.get("status") or "").lower() == "completed"]

    def remove(self, ids: list):
        self._get(mode="history", name="delete", value=",".join(ids), del_files=1)


def _sweep_clients(arrs, tracked: set) -> tuple[int, list, list]:
    """Finished items in the arrs' categories that no arr tracks any more.
    Returns (count, log lines, rows for the recent list)."""
    n, lines, rows = 0, [], []
    for c in _clients(arrs):
        try:
            if c["impl"] == "QBittorrent":
                q = _QBit(c)
                if not q.login():
                    lines.append(f"{c['name']}: login refused"); continue
                gone = [t for t in q.finished(c["cats"])
                        if str(t.get("hash") or "").upper() not in tracked
                        and time.time() - (t.get("completion_on") or 0) > 600]
                if gone:
                    q.remove([t["hash"] for t in gone])
                    n += len(gone)
                    lines += [f"{c['name']}: removed {t.get('name', '')[:70]} (finished, no arr tracks it)"
                              for t in gone]
                    rows += [{"at": time.time(), "arr": "", "title": t.get("name") or "", "client": c["name"],
                              "why": "finished in the client, no arr tracks it"} for t in gone]
            elif c["impl"] == "Sabnzbd":
                s = _Sab(c)
                gone = [x for x in s.finished(c["cats"])
                        # UPPER, as `tracked` is: SABnzbd's ids are "SABnzbd_nzo_..."
                        # and compared as written none matched - every finished
                        # job still waiting on an import was taken as untracked
                        # and deleted with its files (Fate/stay night HF-I, 10-16).
                        if str(x.get("nzo_id") or "").upper() not in tracked
                        and time.time() - (x.get("completed") or 0) > 600]
                if gone:
                    s.remove([x["nzo_id"] for x in gone])
                    n += len(gone)
                    lines += [f"{c['name']}: removed {x.get('name', '')[:70]} (completed, no arr tracks it)"
                              for x in gone]
                    rows += [{"at": time.time(), "arr": "", "title": x.get("name") or "", "client": c["name"],
                              "why": "completed in the client, no arr tracks it"} for x in gone]
        except Exception as e:                                   # noqa: BLE001
            lines.append(f"{c['name']}: {type(e).__name__}: {e}"[:160])
    return n, lines, rows


# ------------------------------------------------------------ the pass -----
def _recent() -> list:
    try:
        return json.loads(kv_get(KV_RECENT) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _remember(rows: list) -> None:
    if not rows:
        return
    try:
        kv_set(KV_RECENT, json.dumps((rows + _recent())[:RECENT_KEEP]))
    except Exception:                                            # noqa: BLE001
        pass


def _pass() -> str:
    from .config import SETTINGS
    arrs = [a for a in SETTINGS.arrs if a.enabled and a.kind in ("sonarr", "radarr")]
    STATS["detail"] = []
    removed = kept = malware = stalls = 0
    tracked: set = set()
    new: list = []
    queues: list = []
    for a in arrs:
        try:
            queues.append((a, _queue(a)))
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not read the queue: {type(e).__name__}: {e}"[:160])
    ids = {str(r["downloadId"]).upper() for _a, rows in queues for r in rows if r.get("downloadId")}
    exe = _exe_downloads(arrs, ids)
    stalled = _stalled(arrs, ids)
    # A QUEUE THIS PASS COULD NOT READ leaves its downloads out of `tracked`,
    # and the client sweep would then take them as untracked - an import in
    # flight deleted because Sonarr was slow to answer. No sweep this pass.
    sweep_ok = len(queues) == len(arrs)
    unread = [(a, r) for a, rows in queues for r in rows if _unread(a, r)]
    if unread:
        new_parsed = _import_known(unread)
        new_parsed += _import_unread([(a, r) for a, r in unread if not r.get("_nuarr_imported")])
    else:
        new_parsed = []
    # A FULLER COPY NUARR FETCHED is "not a Custom Format upgrade" to the arr
    # by design - see bitcut.py. It is imported by hand here, and left out of
    # everything below, which would otherwise clear it as exactly that.
    try:
        from . import bitcut
        cut_rows, hold = bitcut.import_wanted(queues)
    except Exception as e:                                       # noqa: BLE001
        cut_rows, hold = [], set()
        STATS["detail"].append(f"fuller copies: {type(e).__name__}: {e}"[:160])
    if hold:
        for _a, rows in queues:
            for r in rows:
                if r.get("id") in hold and r.get("downloadId"):
                    tracked.add(str(r["downloadId"]).upper())
                    tracked.add(str(r["downloadId"]))
        queues = [(a, [r for r in rows if r.get("id") not in hold]) for a, rows in queues]
    new_parsed = cut_rows + new_parsed
    # REFUSED FOR ITS SHAPE - see _import_better. Imported or removed here;
    # the rows it removed leave the pass so nothing below asks after them.
    try:
        better_rows, gone_now = _import_better(queues)
    except Exception as e:                                       # noqa: BLE001
        better_rows, gone_now = [], set()
        STATS["detail"].append(f"shape-refused imports: {type(e).__name__}: {e}"[:160])
    if gone_now:
        queues = [(a, [r for r in rows if r.get("id") not in gone_now]) for a, rows in queues]
    new_parsed = better_rows + new_parsed
    for a, rows in queues:
        # TWO DOWNLOADS FOR ONE EPISODE: KEEP THE BEST, DROP THE REST NOW.
        # The arr grabs a release, a better one appears, it grabs that too as
        # an upgrade - and lets the first one finish downloading before
        # deciding it was "not an upgrade". Erik: "is it possible to tell
        # when 2 of the same are downloading at the same time and the one
        # that has a lower score gets removed". It is: the queue carries
        # each entry's custom-format score. Equal scores keep the one that
        # was grabbed first (the lower queue id).
        key = "episodeId" if a.kind == "sonarr" else "movieId"
        by: dict = {}
        for r in rows:
            if r.get(key):
                by.setdefault(r[key], []).append(r)
        losers: dict = {}
        for k, grp in by.items():
            if len(grp) < 2:
                continue
            # equal scores keep the one that is further along
            grp.sort(key=lambda x: (-(x.get("customFormatScore") or 0),
                                    x.get("sizeleft") or 0, x.get("id") or 0))
            best = grp[0]
            for r in grp[1:]:
                losers[r["id"]] = (f"a better download for the same {'episode' if a.kind == 'sonarr' else 'movie'} "
                                   f"is in the queue ({r.get('customFormatScore') or 0} vs "
                                   f"{best.get('customFormatScore') or 0}: {(best.get('title') or '')[:60]})")
        # ONE DOWNLOAD, MANY ROWS. A season pack is one torrent and one row
        # per episode in the queue; removing it through the first row takes
        # the whole download, and every later row then answered 404 - twelve
        # "could not remove Sugar.Apple.Fairy.Tale..." lines for one success.
        gone: set = set()
        for r in rows:
            if r.get("downloadId"):
                tracked.add(str(r["downloadId"]).upper())
            did = str(r.get("downloadId") or "").upper()
            if did and did in gone:
                continue
            bad = _malware(r, exe)
            if not bad:
                try:
                    bad, cleaned = _landed(a, r)
                except Exception as e:                           # noqa: BLE001
                    bad, cleaned = "", None
                    STATS["detail"].append(f"{a.name}: landed-file check: {type(e).__name__}: {e}"[:160])
                if cleaned:
                    new.append(cleaned)
                    continue                # the arr is importing it now
            stall = ""
            if did in stalled and (r.get("sizeleft") or 0) > 0:
                stall = (f"stalled: no progress for {stalled[did]:.0f} h at "
                         f"{100 * (1 - (r.get('sizeleft') or 0) / max(1, r.get('size') or 1)):.0f}% - "
                         "blocklisted, the arr will look for another release")
            why = bad or stall or losers.get(r["id"]) or _verdict(a, r)
            if not why:
                if (r.get("trackedDownloadStatus") or "") == "warning":
                    kept += 1
                continue
            try:
                try:
                    _remove(a, r, blocklist=bool(bad or stall))
                except urllib.error.HTTPError as he:
                    if he.code != 404:              # 404: already gone with its pack
                        raise
                if did:
                    gone.add(did)
                removed += 1
                if stall:
                    stalls += 1
                if bad:
                    malware += 1
                    why += (f" (from {r.get('indexer') or 'an unknown indexer'}) - "
                            "blocklisted, the arr will look for another release")
                new.append({"at": time.time(), "arr": a.name, "title": r.get("title") or "",
                            "client": r.get("downloadClient") or "", "why": why,
                            "kind": "malware" if bad else "stalled" if stall else ""})
                STATS["detail"].append(f"{a.name}: removed {(r.get('title') or '')[:70]} - {why}")
            except Exception as e:                               # noqa: BLE001
                STATS["detail"].append(f"{a.name}: could not remove {(r.get('title') or '')[:60]}: "
                                       f"{type(e).__name__}: {e}"[:160])
    # The removals above also told the clients; anything the clients still
    # hold in an arr category, finished, that no arr tracks, goes now.
    if sweep_ok:
        swept, lines, srows = _sweep_clients(arrs, tracked)
        STATS["detail"] += lines
    else:
        swept, srows = 0, []
        STATS["detail"].append("client sweep skipped: an arr's queue could not be read this pass")
    _remember(new_parsed + new + srows)
    STATS.update(removed=STATS["removed"] + removed, swept=STATS["swept"] + swept, kept=kept)
    bits = []
    if malware:
        bits.append(f"{malware} fake(s) posing as video removed and blocklisted")
    if stalls:
        bits.append(f"{stalls} stalled download(s) removed and blocklisted, searched again")
    if removed - malware - stalls:
        bits.append(f"{removed - malware - stalls} finished download(s) the arr would not import, removed with their files")
    if swept:
        bits.append(f"{swept} left in the clients, removed")
    if kept:
        bits.append(f"{kept} with a warning kept for a person")
    # THE FOLDER, NOT ONLY THE LISTS. A download no queue and no client lists
    # any more is still on disk - see dlleft.py.
    try:
        from . import dlleft
        dl = dlleft.scan()
        if dl["removed"]:
            bits.append(f"{len(dl['removed'])} empty leftover folder(s) in the download folder recycled")
            _remember([{"at": time.time(), "arr": "", "title": r["name"], "client": "folder",
                        "why": "an empty leftover in the completed folder - its files had already gone; "
                               "moved to the Recycle Bin", "kind": "leftover"} for r in dl["removed"]])
        if dl["listed"]:
            bits.append(f"{len(dl['listed'])} leftover download(s) with video in them, listed for you")
    except Exception as e:                                       # noqa: BLE001
        STATS["detail"].append(f"download folder sweep: {type(e).__name__}: {e}"[:160])
    return "; ".join(bits) or "nothing to clean"


# WOKEN BY THE ARR. Sonarr and Radarr post a ManualInteractionRequired
# webhook the moment they park a download (see webhooks._handle); that sets
# this and the loop runs a pass KICK_DELAY_S later - long enough for the arr
# to finish writing its own queue - instead of up to five minutes later.
_KICK: asyncio.Event | None = None
KICK_DELAY_S = 20
KICKED: dict = {"why": "", "at": 0.0}


def kick(why: str = "") -> None:
    KICKED.update(why=why, at=time.time())
    if _KICK is not None:
        _KICK.set()


async def run() -> str:
    if STATS["running"]:
        return STATS["last_result"]
    STATS["running"] = True
    before = (STATS["removed"], STATS["swept"])
    prev = STATS["last_result"]
    try:
        res = await asyncio.to_thread(_pass)
    finally:
        STATS["running"] = False
    STATS["last_run"] = time.time()
    STATS["last_result"] = res
    try:
        kv_set(KV_LAST, json.dumps({"at": STATS["last_run"], "result": res}))
    except Exception:                                            # noqa: BLE001
        pass
    # ONE LINE PER CHANGE, NOT ONE PER PASS. "2 with a warning kept for a
    # person" every five minutes for a day is 288 copies of one fact; it is
    # logged when it changes, and every pass that removed or cleaned something.
    acted = (STATS["removed"], STATS["swept"]) != before or any(
        ": removed " in ln or " - renamed " in ln or "Recycle Bin" in ln for ln in STATS["detail"])
    if res != "nothing to clean" and (acted or res != prev):
        joblog.log("arr queue janitor: " + res, "ok")
        for ln in STATS["detail"]:
            joblog.log("  " + ln, "debug")
    return res


async def watch() -> None:
    from .gate import get_toggle
    STATS["next_run"] = time.time() + 300
    try:
        last = json.loads(kv_get(KV_LAST) or "{}")
        STATS["last_run"] = float(last.get("at") or 0.0)
        STATS["last_result"] = str(last.get("result") or "")
    except Exception:                                            # noqa: BLE001
        pass
    global _KICK
    _KICK = asyncio.Event()
    await asyncio.sleep(300)             # never compete with startup
    while True:
        try:
            if get_toggle("arrs.queue_janitor"):
                await run()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"arr queue janitor: {type(e).__name__}: {e}", "error")
        STATS["next_run"] = time.time() + POLL_S
        _KICK.clear()
        try:
            await asyncio.wait_for(_KICK.wait(), timeout=POLL_S)
        except asyncio.TimeoutError:
            continue
        # woken by a webhook: give the arr a moment to finish its own bookkeeping
        STATS["next_run"] = time.time() + KICK_DELAY_S
        await asyncio.sleep(KICK_DELAY_S)


def snapshot() -> dict:
    # The removals are the table below the card; the detail lines carry only
    # what went wrong, so the card is not the same list twice.
    problems = [ln for ln in STATS["detail"] if ": removed " not in ln]
    return {"stats": {k: v for k, v in STATS.items() if k != "detail"} | {"detail": problems[:12]},
            "recent": _recent()[:RECENT_KEEP], "poll_s": POLL_S, "kicked": KICKED}
