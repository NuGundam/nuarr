r"""nuarr - files waiting on a person, kept away from every other system.

WAITING ON YOU MEANS WAITING. Sonic Underground S01E40 (2026-11-02) is tagged
English and heard German at 98%, a file the Audio language panel was asking
you to correct or replace - and while it asked, the Subtitle maker wrote it a
subtitle, the timing checker measured it and a transcode set its flags. An
answer of "replace" throws all of that away, so a file with an open question
is left alone until the question is answered.

The sources are the per-file questions the Attention tile counts (see
userinput.py), each read the way its own panel reads it:

    Audio language   tagged one language, heard another (audiolang.awaiting)
    Audio User Input questions the audio queue will not answer itself
    Subtitle User Input  reader rows waiting on you + the subtitle queue's asks
    Raws             nothing you can read - Blocklist & re-download, or Keep
    Bitrate cuts     upgrades that cut the picture - fetch or keep

Not files, so not here: banned-term collisions, TheTVDB re-ids, download
leftovers. Not a question about the file either: "subtitle rules changed"
waits on a Requeue that only re-applies the rules.

Read-only checks still run on a held file (jobs.HELD_OK_KINDS) - they are how
a question gets asked again after a fix. Answering releases the file within a
minute.

SOME SOURCES ARE SLOW (Subtitle User Input is ~12 s cold) and the queue asks
on every claim, so the set is rebuilt on a side thread and the last one is
served meanwhile.
"""
from __future__ import annotations

import threading
import time

TTL_S = 60.0
_STATE: dict = {"at": 0.0, "why": {}, "busy": False}
_LOCK = threading.Lock()


def _collect() -> dict:
    why: dict[int, str] = {}

    def put(fid, text):
        try:
            fid = int(fid or 0)
        except (TypeError, ValueError):
            return
        if fid and fid not in why:
            why[fid] = text

    try:
        from . import audiolang
        for fid in audiolang.awaiting():
            put(fid, "the Audio language panel is asking you about this file")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import audqueue
        for q in audqueue.asking(5000) or []:
            if q.get("asks"):
                put(q.get("file_id"), "Audio User Input has a question on this file")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import subqueue
        for q in subqueue.asking(5000) or []:
            if q.get("asks"):
                put(q.get("file_id"), "Subtitle User Input has a question on this file")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import subkind
        f = subkind.findings()
        auto = f.get("mode") == "auto"
        for r in f.get("rows") or []:
            # the rows userinput counts as yours: an action, not done, read,
            # and not one auto is about to take itself
            if (r.get("action") and not r.get("done") and not r.get("unread")
                    and not subkind._noread(r)
                    and not (auto and r.get("auto") == "act")):
                put(r.get("file_id"), "Subtitle User Input is waiting on you for this file")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import subneed
        for r in subneed.missing(100000) or []:
            put(r.get("file_id"), "it is a raw - waiting on Blocklist & re-download or Keep")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import bitcut
        for x in bitcut.found():
            if x.get("state") in ("open", "none"):
                put(x.get("file_id"), "Bitrate cuts is waiting on fetch or keep for this file")
    except Exception:                                            # noqa: BLE001
        pass
    return why


def _refresh() -> None:
    try:
        why = _collect()
        with _LOCK:
            _STATE.update(at=time.time(), why=why)
    finally:
        _STATE["busy"] = False


def _kick() -> None:
    with _LOCK:
        if _STATE["busy"]:
            return
        _STATE["busy"] = True
    threading.Thread(target=_refresh, name="nuarr-hold", daemon=True).start()


def reasons() -> dict:
    """file_id -> why it waits on you. Never blocks: a stale set is served
    while a fresh one is built."""
    if time.time() - _STATE["at"] >= TTL_S:
        _kick()
    return _STATE["why"]


def files() -> frozenset:
    return frozenset(reasons())


def why(file_id: int) -> str:
    try:
        return reasons().get(int(file_id), "")
    except (TypeError, ValueError):
        return ""


def expire() -> None:
    """An answer was given somewhere - rebuild now rather than within a minute."""
    _STATE["at"] = 0.0
    _kick()
