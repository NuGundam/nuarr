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
import re
import time
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

STATS: dict = {"last_run": 0.0, "last_result": "", "next_run": 0.0,
               "removed": 0, "swept": 0, "kept": 0, "detail": [], "running": False}


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


def _remove(a, row: dict) -> None:
    _call(a, f"/api/v3/queue/{row['id']}?removeFromClient=true&blocklist=false"
             "&skipRedownload=false", "DELETE")


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
        rows = json.loads(self._req("/torrents/info") or "[]")
        return [t for t in rows if str(t.get("category") or "").lower() in cats
                and t.get("progress", 0) >= 1.0
                and t.get("state") in ("stoppedUP", "pausedUP", "queuedUP", "uploading",
                                       "stalledUP", "forcedUP", "checkingUP")]

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
    removed = kept = 0
    tracked: set = set()
    new: list = []
    for a in arrs:
        try:
            rows = _queue(a)
        except Exception as e:                                   # noqa: BLE001
            STATS["detail"].append(f"{a.name}: could not read the queue: {type(e).__name__}: {e}"[:160])
            continue
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
            grp.sort(key=lambda x: (-(x.get("customFormatScore") or 0), x.get("id") or 0))
            best = grp[0]
            for r in grp[1:]:
                losers[r["id"]] = (f"a better download for the same {'episode' if a.kind == 'sonarr' else 'movie'} "
                                   f"is in the queue ({r.get('customFormatScore') or 0} vs "
                                   f"{best.get('customFormatScore') or 0}: {(best.get('title') or '')[:60]})")
        for r in rows:
            if r.get("downloadId"):
                tracked.add(str(r["downloadId"]).upper())
            why = losers.get(r["id"]) or _verdict(a, r)
            if not why:
                if (r.get("trackedDownloadStatus") or "") == "warning":
                    kept += 1
                continue
            try:
                _remove(a, r)
                removed += 1
                new.append({"at": time.time(), "arr": a.name, "title": r.get("title") or "",
                            "client": r.get("downloadClient") or "", "why": why})
                STATS["detail"].append(f"{a.name}: removed {(r.get('title') or '')[:70]} - {why}")
            except Exception as e:                               # noqa: BLE001
                STATS["detail"].append(f"{a.name}: could not remove {(r.get('title') or '')[:60]}: "
                                       f"{type(e).__name__}: {e}"[:160])
    # The removals above also told the clients; anything the clients still
    # hold in an arr category, finished, that no arr tracks, goes now.
    swept, lines, srows = _sweep_clients(arrs, tracked)
    STATS["detail"] += lines
    _remember(new + srows)
    STATS.update(removed=STATS["removed"] + removed, swept=STATS["swept"] + swept, kept=kept)
    bits = []
    if removed:
        bits.append(f"{removed} finished download(s) the arr would not import, removed with their files")
    if swept:
        bits.append(f"{swept} left in the clients, removed")
    if kept:
        bits.append(f"{kept} with a warning kept for a person")
    return "; ".join(bits) or "nothing to clean"


async def run() -> str:
    STATS["running"] = True
    try:
        res = await asyncio.to_thread(_pass)
    finally:
        STATS["running"] = False
    STATS["last_run"] = time.time()
    STATS["last_result"] = res
    if res != "nothing to clean":
        joblog.log("arr queue janitor: " + res, "ok")
        for ln in STATS["detail"]:
            joblog.log("  " + ln, "debug")
    return res


async def watch() -> None:
    from .gate import get_toggle
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
