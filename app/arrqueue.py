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
EXEC_SAYS = ("found executable file", "potentially dangerous file")

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
               "could not be parsed", "manual import required", "unknown episode")
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
                        if str(x.get("nzo_id") or "") not in tracked
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
        new_parsed = _import_unread(unread)
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
        bits.append(f"{malware} executable(s) posing as video removed and blocklisted")
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


async def run() -> str:
    STATS["running"] = True
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
    if res != "nothing to clean":
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
    await asyncio.sleep(300)             # never compete with startup
    while True:
        try:
            if get_toggle("arrs.queue_janitor"):
                await run()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"arr queue janitor: {type(e).__name__}: {e}", "error")
        STATS["next_run"] = time.time() + POLL_S
        await asyncio.sleep(POLL_S)


def snapshot() -> dict:
    # The removals are the table below the card; the detail lines carry only
    # what went wrong, so the card is not the same list twice.
    problems = [ln for ln in STATS["detail"] if ": removed " not in ln]
    return {"stats": {k: v for k, v in STATS.items() if k != "detail"} | {"detail": problems[:12]},
            "recent": _recent()[:RECENT_KEEP], "poll_s": POLL_S}
