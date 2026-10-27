r"""Import loops: a release the arr takes, nuarr rewrites, and the arr takes again.

Bleach S17E50, 2026-10-27: Sonarr imported the same "[Omar] ... Dual-Audio"
release fifteen times in eight hours. The release's name says dual audio, so
the profile credits it +4000 (Anime Dual Audio); its audio is Arabic and
Japanese, nuarr drops the Arabic and renames [AR+JA] to [JA], Sonarr scores
the file it holds 310 again, and the next RSS sync sees the same release as a
4310 upgrade. Every lap cost a download, a probe, a passthrough, a rename, a
second opinion and a subtitle-timing check - and the arr's history shows the
laps as ordinary upgrades.

Two rules, read from the arr's own history every fifteen minutes:

  CAUSE. An import the arr credited with a dual-audio or dub format whose
  file, by the probe, carries no English audio: the release is not what its
  name says. Blocklist the grab as soon as the probe is in - no lap needed.

  LAP COUNT. The same release imported twice for the same episode or movie
  within two days, whatever the reason: blocklist every grab of it in the
  window - one per indexer and protocol, since the usenet copy and the
  torrent copy are separate blocklist entries. The file the arr holds stays.

markFailed is the arr UI's own "Blocklist and Search": the arr looks for the
next release by itself, and with the kept file scoring what it scores, takes
only something better.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from datetime import datetime

from . import joblog
from .db import cursor, kv_get, kv_set

POLL_S = 15 * 60
WINDOW_S = 2 * 86400
PAGE = 300
KV_DONE = "arrloop.done"          # grab history ids already blocklisted
KV_LAST = "arrloop.last"
KV_ARMED = "arrloop.armed"        # "1": blocklist; anything else: only say what it would do
NEEDS_ENGLISH = re.compile(r"dual|dub", re.I)   # format names that promise English audio

STATS: dict = {"last_run": 0.0, "last_result": "", "next_run": 0.0, "running": False,
               "blocked": 0, "detail": []}


def _call(a, path: str, method: str = "GET", body=None, timeout: float = 60.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(a.url.rstrip("/") + path, data=data, method=method,
                                 headers={"X-Api-Key": a.api_key,
                                          "Content-Type": "application/json"})
    t = urllib.request.urlopen(req, timeout=timeout).read().decode()
    return json.loads(t) if t.strip() else {}


def _norm(t) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:                                            # noqa: BLE001
        return 0.0


def _history(a, event_type: int, since: float) -> list:
    """Newest first, back to `since`. 1 = grabbed, 3 = imported."""
    out: list = []
    for page in (1, 2, 3):
        d = _call(a, f"/api/v3/history?page={page}&pageSize={PAGE}&sortKey=date"
                     f"&sortDirection=descending&eventType={event_type}")
        recs = d.get("records") or []
        for r in recs:
            if _ts(r.get("date")) < since:
                return out
            out.append(r)
        if len(recs) < PAGE:
            break
    return out


def _parent(a, r: dict):
    return r.get("episodeId") if a.kind == "sonarr" else r.get("movieId")


def _audio_langs(a, r: dict) -> str | None:
    """The probe's audio languages for the file this import produced, or None
    when nuarr has not probed it yet. By the arr's file id first - it survives
    nuarr's rename - then by the path the arr put it at."""
    fid = (r.get("data") or {}).get("fileId")
    path = (r.get("data") or {}).get("importedPath") or ""
    with cursor() as c:
        row = None
        if fid:
            row = c.execute("SELECT audio_langs FROM files WHERE arr_name=? AND arr_file_id=? "
                            "AND state NOT IN ('deleted','duplicate') ORDER BY id DESC LIMIT 1",
                            (a.name, int(fid))).fetchone()
        if row is None and path:
            row = c.execute("SELECT audio_langs FROM files WHERE path=? "
                            "AND state NOT IN ('deleted','duplicate') ORDER BY id DESC LIMIT 1",
                            (path,)).fetchone()
    return row["audio_langs"] if row else None


def _blocklist(a, grabs: list, parent, title: str, done: set, dry: bool) -> list:
    """markFailed every grab of this release for this parent in the window,
    one per indexer and protocol. Returns the ids it sent."""
    seen: set = set()
    sent: list = []
    for g in grabs:                                  # newest first
        if _parent(a, g) != parent or _norm(g.get("sourceTitle")) != _norm(title):
            continue
        d = g.get("data") or {}
        key = (d.get("indexer") or "", d.get("protocol") or "")
        if key in seen or int(g["id"]) in done:
            continue
        seen.add(key)
        if not dry:
            _call(a, f"/api/v3/history/failed/{int(g['id'])}", "POST", {})
        done.add(int(g["id"]))
        sent.append(int(g["id"]))
    return sent


def _pass(dry: bool = False) -> str:
    from .config import SETTINGS
    from .arrlang import _iso_set
    arrs = [x for x in SETTINGS.arrs if x.enabled and x.kind in ("sonarr", "radarr")]
    try:
        done = set(int(x) for x in json.loads(kv_get(KV_DONE) or "[]"))
    except Exception:                                            # noqa: BLE001
        done = set()
    since = time.time() - WINDOW_S
    detail: list = []
    blocked = 0
    for a in arrs:
        try:
            imports = _history(a, 3, since)
            grabs = _history(a, 1, since)
        except Exception as e:                                   # noqa: BLE001
            detail.append(f"{a.name}: history unreadable - {type(e).__name__}: {e}")
            continue
        by_parent: dict = {}
        for r in imports:
            by_parent.setdefault(_parent(a, r), []).append(r)
        for parent, rs in by_parent.items():
            # A LAP IS THE SAME RELEASE TWICE IN A ROW. Two imports of one
            # release with another release between them is a deliberate
            # return - the bitrate-cut panel grabbing the old release back
            # after a thin upgrade - and is left alone.
            rs.sort(key=lambda r: _ts(r.get("date")))
            run_n, run_t = 0, ""
            for r in rs:
                t = _norm(r.get("sourceTitle"))
                run_n, run_t = (run_n + 1, t) if t == run_t else (1, t)
            newest = rs[-1]
            title = newest.get("sourceTitle") or ""
            why = ""
            if run_n >= 2:
                why = f"imported {run_n} times in a row in two days"
            else:
                names = [c.get("name") or "" for c in (newest.get("customFormats") or [])]
                promised = [n for n in names if NEEDS_ENGLISH.search(n)]
                if promised:
                    langs = _audio_langs(a, newest)
                    if langs is not None and "eng" not in _iso_set(langs):
                        why = (f"credited {', '.join(promised)} but the file's audio is "
                               f"{langs or 'none'} - no English")
            if not why:
                continue
            sent = _blocklist(a, grabs, parent, title, done, dry)
            if not sent:
                continue
            blocked += len(sent)
            line = f"{a.name}: {'would blocklist' if dry else 'blocklisted'} {title!r} - {why}"
            detail.append(line)
            if not dry or line not in STATS["detail"]:
                joblog.log("import loop: " + line, "warn")
    if not dry:
        try:
            kv_set(KV_DONE, json.dumps(sorted(done)[-500:]))
        except Exception:                                        # noqa: BLE001
            pass
    STATS["detail"] = detail
    STATS["blocked"] += 0 if dry else blocked
    if blocked:
        return f"blocklisted {blocked} release(s)"
    return "no loop" + ("; " + "; ".join(detail) if detail else "")


async def run() -> str:
    """Unarmed, it only reports; the kv flag arms it (Erik's call, 2026-10-27)."""
    dry = (kv_get(KV_ARMED) or "") != "1"
    STATS["running"] = True
    try:
        res = await asyncio.to_thread(_pass, dry)
    finally:
        STATS["running"] = False
    STATS["last_run"] = time.time()
    STATS["last_result"] = res
    try:
        kv_set(KV_LAST, json.dumps({"at": STATS["last_run"], "result": res}))
    except Exception:                                            # noqa: BLE001
        pass
    return res


async def watch() -> None:
    STATS["next_run"] = time.time() + 420
    try:
        last = json.loads(kv_get(KV_LAST) or "{}")
        STATS["last_run"] = float(last.get("at") or 0.0)
        STATS["last_result"] = str(last.get("result") or "")
    except Exception:                                            # noqa: BLE001
        pass
    await asyncio.sleep(420)             # never compete with startup
    while True:
        try:
            await run()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"import loop: {type(e).__name__}: {e}", "error")
        STATS["next_run"] = time.time() + POLL_S
        await asyncio.sleep(POLL_S)
