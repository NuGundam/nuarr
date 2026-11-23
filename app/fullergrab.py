"""Fuller-copy grabs the arr would refuse to import.

WHY THIS EXISTS (2026-11-22). Find fuller grabbed The Pitt S02 copies at
20 Mbps to replace 7 Mbps ones. They downloaded, and Sonarr refused every
one at import: "not an upgrade" - its custom-format score for FLUX/Kitsune
was below the HONE files', and the score knows nothing of bitrate. nuarr's
queue cleaner then took "not an upgrade" as finished business and removed
them with their files. Seven full downloads, twice, for nothing.

So each fuller grab is remembered here (arr, episode or film, release
name), and when the queue cleaner meets one of them refused as "not an
upgrade", it imports it by hand instead (ManualImport executes without the
upgrade check - the same path a person takes in the arr's own Manual Import
dialog). The match is exact: the arr's own grab history for that episode or
film must hold this download id under the remembered release name.
"""
from __future__ import annotations

import json
import re
import time

from .db import kv_get, kv_set

KV = "fuller.grabs"
KEEP_S = 14 * 86400        # a grab older than this is not ours to force any more


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _load() -> list:
    try:
        return json.loads(kv_get(KV) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def remember(arr: str, kind: str, item_id: int, release: str) -> None:
    """kind 'episode' or 'movie'."""
    now = time.time()
    rows = [r for r in _load() if now - r.get("at", 0) < KEEP_S
            and not (r["arr"] == arr and r["item"] == int(item_id) and r["kind"] == kind)]
    rows.append({"arr": arr, "kind": kind, "item": int(item_id), "release": release, "at": now, "forced": ""})
    kv_set(KV, json.dumps(rows[-500:]))


def match(a, row: dict, call) -> dict | None:
    """The remembered grab this queue row is, or None. `call` is the arr GET."""
    kind, item = ("episode", row.get("episodeId")) if a.kind == "sonarr" else ("movie", row.get("movieId"))
    did = str(row.get("downloadId") or "").upper()
    if not item or not did:
        return None
    now = time.time()
    for r in _load():
        if r["arr"] != a.name or r["kind"] != kind or r["item"] != int(item) or now - r["at"] > KEEP_S:
            continue
        if r.get("forced") == did:
            return None                     # tried once already; let the cleaner have it
        q = (f"/api/v3/history/series?seriesId={row.get('seriesId')}&episodeId={item}&eventType=1"
             if kind == "episode" else f"/api/v3/history/movie?movieId={item}&eventType=1")
        try:
            hist = call(q) or []
            if isinstance(hist, dict):
                hist = hist.get("records") or []
        except Exception:                                        # noqa: BLE001
            return None
        want = _norm(r["release"])
        for h in hist:
            # the endpoint ignores episodeId, so the episode is checked here
            if kind == "episode" and h.get("episodeId") != int(item):
                continue
            if str(h.get("downloadId") or "").upper() == did and _norm(h.get("sourceTitle")) == want:
                return r
    return None


def mark_forced(r: dict, did: str) -> None:
    rows = _load()
    for x in rows:
        if x["arr"] == r["arr"] and x["kind"] == r["kind"] and x["item"] == r["item"]:
            x["forced"] = str(did).upper()
    kv_set(KV, json.dumps(rows))
