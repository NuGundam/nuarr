r"""Files marked "Don't encode": the video is never re-encoded, whatever the rules say.

Erik, 2026-11-03. Two Sonic Underground DVD remuxes came out of the MPEG-2 to
h264 rebuild with banding the source did not have (1.4 became 9.9 and 5.9).
Re-downloading them would only run the same encode on the same bytes, so a file
can now be excluded by hand.

Keyed by file id, which survives a rename. A re-download into a new record gets
a new id and has to be marked again. rules.decide() reads this; everything
except the video (audio changes, subtitles, renames) carries on as normal.
"""
from __future__ import annotations

import json
import threading
import time

from .db import kv_get, kv_set

KV = "encode.never"
_TTL = 10.0
_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "ids": {}}


def _load() -> dict:
    try:
        return json.loads(kv_get(KV) or "{}") or {}
    except Exception:                                            # noqa: BLE001
        return {}


def all_ids() -> dict:
    """{file id as a string: {at, why}} - rules.decide() calls this per file."""
    with _LOCK:
        if time.time() - _CACHE["at"] > _TTL:
            _CACHE.update(at=time.time(), ids=_load())
        return _CACHE["ids"]


def has(file_id) -> bool:
    return bool(file_id) and str(int(file_id)) in all_ids()


def mark(file_id: int, on: bool, why: str = "") -> dict:
    with _LOCK:
        ids = _load()
        if on:
            ids[str(int(file_id))] = {"at": time.time(), "why": why}
        else:
            ids.pop(str(int(file_id)), None)
        kv_set(KV, json.dumps(ids))
        _CACHE.update(at=time.time(), ids=ids)
    return {"ok": True, "file_id": int(file_id), "on": bool(on), "count": len(ids)}
