r"""nuarr - what the download clients left behind in their completed folder.

WHY. The queue janitor reads the arrs' queues and the clients' lists - and a
folder neither of them lists any more is invisible to it. F:\Ready held 16 of
them (2026-10-15): six were empty shells whose files had already gone into the
library, and ten were releases that were grabbed and never imported - Ef: a
Tale of Memories and of Melodies, Taken 2, the Tanya film twice, 61 GB in all
- with no queue entry left to remove them through. "nothing to clean", while
the folder filled up.

WHAT IT DOES.
  - Finds the clients' completed folders: SABnzbd's own complete_dir, and the
    folder every arr import was taken from (history droppedPath), so a client
    nuarr cannot ask is still covered.
  - Leaves alone anything a client or an arr still knows: a queue row's
    outputPath, SABnzbd's queue and unfinished history, anything modified in
    the last MIN_AGE_H hours (a job still unpacking or moving).
  - An EMPTY leftover - no video and nothing over a few MB, the release having
    gone into the library - goes to the Recycle Bin on its own.
  - A leftover that still holds a video is NOT removed on a guess: it is
    listed for a person, with "Hand to the arr" (Sonarr/Radarr asked to import
    that folder) and "Delete" (Recycle Bin). Both restorable or retryable.
"""
from __future__ import annotations

import json
import os
import re
import time

MIN_AGE_H = 6.0
VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")
SMALL_B = 5 * 1024 * 1024
KV_KEPT = "dlleft.kept"
STATE: dict = {"at": 0.0, "roots": [], "listed": [], "removed": 0, "error": ""}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _arrs():
    from .config import SETTINGS
    return [a for a in SETTINGS.arrs if a.enabled and a.kind in ("sonarr", "radarr")]


_ROOTS: dict = {"at": 0.0, "d": None}


def roots() -> list:
    """The completed-download folders to look in - asked once an hour (the
    arrs' import history and SABnzbd's config are 5 s to read)."""
    if _ROOTS["d"] is not None and time.time() - _ROOTS["at"] < 3600:
        return list(_ROOTS["d"])
    d = _roots()
    _ROOTS.update(at=time.time(), d=d)
    return d


def _roots() -> list:
    from .arrqueue import _call, _clients, _Sab
    found: set = set()
    arrs = _arrs()
    try:
        for c in _clients(arrs):
            if c.get("impl") == "Sabnzbd" and c.get("apikey"):
                try:
                    misc = (_Sab(c)._get(mode="get_config", section="misc").get("config") or {}).get("misc") or {}
                    d = misc.get("complete_dir") or ""
                    if d and os.path.isabs(d) and os.path.isdir(d):
                        found.add(os.path.normpath(d))
                except Exception:                                # noqa: BLE001
                    pass
    except Exception:                                            # noqa: BLE001
        pass
    # where the arrs actually took imports from - covers a client nuarr cannot ask
    for a in arrs:
        try:
            h = _call(a, "/api/v3/history?page=1&pageSize=200&sortKey=date&sortDirection=descending&eventType=3")
            for e in h.get("records") or []:
                dp = (e.get("data") or {}).get("droppedPath") or ""
                parts = os.path.normpath(dp).split(os.sep)
                if len(parts) >= 3:
                    base = os.sep.join(parts[:2]) + os.sep if parts[0].endswith(":") else ""
                    cand = os.path.normpath(os.path.join(parts[0] + os.sep, parts[1]))
                    if os.path.isdir(cand) and not cand.lower().startswith(("p:\\", "\\\\")):
                        found.add(cand)
        except Exception:                                        # noqa: BLE001
            continue
    return sorted(found)


def _tracked() -> set:
    """Folder names a queue or a client still knows (normalised)."""
    from .arrqueue import _queue, _clients, _Sab
    out: set = set()
    arrs = _arrs()
    for a in arrs:
        try:
            for r in _queue(a):
                for k in ("outputPath", "title"):
                    v = r.get(k) or ""
                    if v:
                        out.add(_norm(os.path.basename(v.rstrip("\\/"))))
        except Exception:                                        # noqa: BLE001
            out.add("*unreadable*")
    try:
        for c in _clients(arrs):
            if c.get("impl") != "Sabnzbd" or not c.get("apikey"):
                continue
            s = _Sab(c)
            for slot in (s._get(mode="queue", limit=500).get("queue") or {}).get("slots") or []:
                out.add(_norm(slot.get("filename")))
            for slot in (s._get(mode="history", limit=300).get("history") or {}).get("slots") or []:
                if str(slot.get("status") or "").lower() not in ("completed", "failed"):
                    out.add(_norm(slot.get("name")))
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _measure(path: str) -> dict:
    size = big = 0
    vids = []
    for dp, _ds, fs in os.walk(path):
        for f in fs:
            try:
                n = os.path.getsize(os.path.join(dp, f))
            except OSError:
                continue
            size += n
            if n > SMALL_B:
                big += 1
            if f.lower().endswith(VIDEO):
                vids.append(f)
    return {"size": size, "big": big, "videos": vids}


def scan() -> dict:
    """Find leftovers; recycle the empty ones; list the rest. Runs on a thread."""
    from . import fileops, joblog
    rs = roots()
    tracked = _tracked()
    if "*unreadable*" in tracked:
        STATE.update(at=time.time(), roots=rs, error="an arr queue could not be read - nothing touched")
        return {"removed": [], "listed": STATE["listed"]}
    kept = _kept()
    removed, listed = [], []
    now = time.time()
    for root in rs:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            p = os.path.join(root, name)
            if not os.path.isdir(p) or name.startswith(("_UNPACK_", "_ADMIN_")):
                continue
            try:
                age_h = (now - os.path.getmtime(p)) / 3600
            except OSError:
                continue
            if age_h < MIN_AGE_H or _norm(name) in tracked:
                continue
            m = _measure(p)
            if not m["videos"] and m["big"] == 0:
                res = fileops.recycle(p)
                if res.ok:
                    removed.append({"name": name, "root": root, "size": m["size"]})
                continue
            if p in kept:
                continue
            kind, match = _kind_of(name)
            listed.append({"name": name, "path": p, "root": root, "size": m["size"],
                           "videos": m["videos"][:20], "n_videos": len(m["videos"]),
                           "age_h": round(age_h, 1), "kind": kind, "match": match})
    if removed:
        joblog.log(f"download leftovers: {len(removed)} empty folder(s) in the completed folder "
                   f"moved to the Recycle Bin", "info")
    # A SWEEP KEEPS WHAT WAS DONE TO A ROW. The list is rebuilt every janitor
    # pass, and rebuilding it used to forget a hand-off - the row went back to
    # looking untouched five minutes after "Hand to the arr".
    before = {x["path"]: x for x in STATE.get("listed") or []}
    for x in listed:
        if before.get(x["path"], {}).get("handed"):
            x["handed"] = before[x["path"]]["handed"]
    STATE.update(at=time.time(), roots=rs, listed=listed, error="",
                 removed=STATE.get("removed", 0) + len(removed))
    return {"removed": removed, "listed": listed}


_KIND_CACHE: dict = {}


def _kind_of(name: str) -> tuple[str, str]:
    """('sonarr'|'radarr', what the arr matched it to) - BY ASKING THE ARRS.

    A name pattern sent "Detective Conan - 031 Remastered - The TV Station
    Murder Case" to Radarr (its "- 031" never matched), and Radarr can only
    refuse an episode. Sonarr's and Radarr's own parsers know their libraries:
    whichever recognises the name - a series and episodes, or a film - is the
    one. Only when neither does is the name pattern the answer."""
    if name in _KIND_CACHE:
        return _KIND_CACHE[name]
    from .arrqueue import _call
    import urllib.parse
    got = ("", "")
    for a in _arrs():
        try:
            p = _call(a, "/api/v3/parse?" + urllib.parse.urlencode({"title": name}), timeout=20) or {}
        except Exception:                                        # noqa: BLE001
            continue
        if a.kind == "sonarr" and (p.get("series") or {}).get("id"):
            eps = [f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}"
                   for e in p.get("episodes") or []]
            got = ("sonarr", f"{p['series'].get('title')} {' '.join(eps[:3])}".strip())
            break
        if a.kind == "radarr" and (p.get("movie") or {}).get("id"):
            got = ("radarr", f"{p['movie'].get('title')} ({p['movie'].get('year')})")
            break
    if not got[0]:
        got = (_guess_kind(name), "")
    _KIND_CACHE[name] = got
    return got


def _guess_kind(name: str) -> str:
    """The fallback when no arr recognises the name: 'sonarr' for an episode or
    season-looking name, else 'radarr'. " - 031 " counts as an episode number
    (the old \\b- \\d pattern needed a word boundary before the dash, which a
    space never gives)."""
    if re.search(r"\bS\d{1,2}(E\d{1,3})?\b|\bSeason\s*\d|(?:^|\s)-\s*\d{1,4}\b|\bEp?\s?\d{1,4}\b|"
                 r"\b\d{3,4}\s*-\s*\d{3,4}\b", name, re.I):
        return "sonarr"
    return "radarr"


def _kept() -> set:
    from .db import kv_get
    try:
        return set(json.loads(kv_get(KV_KEPT) or "[]"))
    except Exception:                                            # noqa: BLE001
        return set()


def keep(path: str) -> None:
    from .db import kv_set
    k = _kept() | {path}
    kv_set(KV_KEPT, json.dumps(sorted(k)))
    STATE["listed"] = [x for x in STATE["listed"] if x["path"] != path]


def delete(path: str) -> dict:
    from . import fileops, joblog
    if not any(path == x["path"] for x in STATE["listed"]):
        return {"ok": False, "why": "not a listed leftover"}
    res = fileops.recycle(path)
    if res.ok:
        STATE["listed"] = [x for x in STATE["listed"] if x["path"] != path]
        joblog.log(f"download leftover moved to the Recycle Bin: {os.path.basename(path)}", "info")
    return {"ok": bool(res.ok), "why": getattr(res, "detail", "") or ""}


def hand_to_arr(path: str, kind: str = "") -> dict:
    """Ask the arr to import this folder - its own parser and rules decide."""
    from .arrqueue import _call
    from . import joblog
    x = next((x for x in STATE["listed"] if x["path"] == path), None)
    if not x:
        return {"ok": False, "why": "not a listed leftover"}
    kind = kind or x["kind"]
    a = next((a for a in _arrs() if a.kind == kind), None)
    if not a:
        return {"ok": False, "why": f"no {kind} configured"}
    cmd = "DownloadedEpisodesScan" if kind == "sonarr" else "DownloadedMoviesScan"
    r = _call(a, "/api/v3/command", "POST", {"name": cmd, "path": path, "importMode": "Move"})
    joblog.log(f"download leftover handed to {a.name} to import: {os.path.basename(path)}", "info")
    x["handed"] = {"arr": a.name, "kind": kind, "at": time.time(), "command": (r or {}).get("id"),
                   "videos": x.get("n_videos") or 0, "state": "working", "result": ""}
    return {"ok": True, "arr": a.name}


def bulk(paths: list, action: str, kinds: dict | None = None) -> dict:
    """The same action on several rows; 'hand' sends each to its own arr
    unless kinds names one for that path."""
    kinds = kinds or {}
    done, failed = 0, []
    for p in paths:
        try:
            if action == "hand":
                r = hand_to_arr(p, kinds.get(p, ""))
            elif action == "delete":
                r = delete(p)
            elif action == "keep":
                keep(p)
                r = {"ok": True}
            else:
                return {"ok": False, "why": "unknown action"}
        except Exception as e:                                   # noqa: BLE001
            r = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if r.get("ok"):
            done += 1
        else:
            failed.append({"path": p, "why": r.get("why") or ""})
    return {"ok": not failed, "done": done, "failed": failed}


def _check_handed() -> None:
    """WHAT THE ARR DID. A hand-off used to say "handed to Radarr just now"
    forever, whether Radarr imported the folder or refused it. The arr's
    command says when it is finished; the folder says what it took."""
    from .arrqueue import _call
    arrs = {a.name: a for a in _arrs()}
    keep_rows = []
    for x in STATE.get("listed") or []:
        h = x.get("handed") or {}
        if h.get("state") == "working" and h.get("command") and h.get("arr") in arrs:
            try:
                c = _call(arrs[h["arr"]], f"/api/v3/command/{h['command']}", timeout=10) or {}
            except Exception:                                    # noqa: BLE001
                c = {}
            st = str(c.get("status") or "").lower()
            if st in ("completed", "failed", "aborted", "cancelled", "orphaned") or \
                    (not c and time.time() - float(h.get("at") or 0) > 600):
                left = _measure(x["path"])["videos"] if os.path.isdir(x["path"]) else []
                took = (h.get("videos") or 0) - len(left)
                if not left:
                    h.update(state="imported", result="the folder is empty now")
                elif took > 0:
                    h.update(state="partly", result=f"{took} of {h.get('videos')} taken, {len(left)} left - its rules refused the rest")
                else:
                    msg = str(c.get("message") or "").strip()
                    h.update(state="refused", result=(f"{msg} - " if msg and msg.lower() != "completed" else "")
                             + ("neither arr recognised the name - try the other one" if (x.get("match") or "") == "" else
                                "most likely the library already has it at the same or better quality"))
                h["done_at"] = time.time()
        # a folder the arr emptied leaves the list a minute after it was told so
        if h.get("state") == "imported" and time.time() - float(h.get("done_at") or 0) > 60:
            continue
        keep_rows.append(x)
    STATE["listed"] = keep_rows


def snapshot() -> dict:
    try:
        _check_handed()
    except Exception:                                            # noqa: BLE001
        pass
    return {"roots": STATE["roots"], "listed": STATE["listed"], "at": STATE["at"],
            "removed_total": STATE.get("removed", 0), "error": STATE.get("error", ""),
            "min_age_h": MIN_AGE_H}
