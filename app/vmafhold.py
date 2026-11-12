r"""Encodes HELD for a low final VMAF, kept so a person can accept them anyway.

The hold rule (jobs.py, after vmaftarget.verify_final) keeps the original when
the finished file scores under vmaftarget.HOLD_BELOW. Throwing the encode away
there made the hold a one-way door: a number that is really a measurement
fault (a 59.998 fps WEBRip slipping frames inside a cut) cost a full re-encode
to undo. So the encode is parked instead:

    <cache>\vmaf-hold\<job_id>.mkv    the finished file, subtitles already in
    <cache>\vmaf-hold\<job_id>.json   what it is, what it scored, what it replaces

ACCEPT hands it to the deferred-commit queue (commitqueue.py) - the same path a
locked swap takes, which already refuses a target that changed since the
encode, tells the arr and Plex, and marks the job done. DISCARD deletes it.
Nothing is kept forever: a hold nobody looked at in KEEP_DAYS is discarded.

The cache sweeps leave the folder alone: purge_stale_cache removes top-level
FILES only, and the work-dir sweep only takes subocr_/shape_ directories.
"""
from __future__ import annotations

import json
import os
import shutil
import time

from . import joblog
from .db import cursor

KEEP_DAYS = 30


def hold_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, "vmaf-hold")


def _cache_dir() -> str:
    from .config import SETTINGS
    return getattr(SETTINGS, "cache_dir", "") or ""


def park(job_id: str, file_id: int, target: str, title: str, out: str, final: dict,
         size_before: int, size_after: int, cache_dir: str) -> str | None:
    """Move the finished encode into the hold folder. The path, or None when it
    could not be kept (the caller then deletes it, as before)."""
    try:
        d = hold_dir(cache_dir)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, f"{job_id}.mkv")
        shutil.move(out, dst)
        man = {"job_id": job_id, "file_id": file_id, "target": target, "title": title,
               "vmaf": final.get("vmaf"), "worst": final.get("worst"), "cambi": final.get("cambi"),
               "sample_vmaf": final.get("sample_vmaf"), "note": final.get("note") or "",
               "cuts_kept": bool(final.get("kept")),
               "size_before": int(size_before or 0), "size_after": int(size_after or 0), "at": time.time()}
        with open(os.path.join(d, f"{job_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(man, fh)
        return dst
    except Exception as e:                               # noqa: BLE001
        joblog.log(f"could not keep the held encode ({type(e).__name__}: {e}) - discarding it", "warn", job_id)
        return None


def _remove(d: str, job_id: str) -> None:
    for ext in (".mkv", ".json"):
        try:
            os.remove(os.path.join(d, job_id + ext))
        except OSError:
            pass


def items() -> list:
    """Every held encode, newest first. Expired ones are discarded on the way."""
    d = hold_dir(_cache_dir())
    if not os.path.isdir(d):
        return []
    out, now = [], time.time()
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        jid = name[:-5]
        try:
            m = json.load(open(os.path.join(d, name), encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            continue
        mkv = os.path.join(d, jid + ".mkv")
        if not os.path.exists(mkv):
            _remove(d, jid)
            continue
        if now - float(m.get("at") or now) > KEEP_DAYS * 86400:
            _remove(d, jid)
            joblog.log(f"held encode discarded after {KEEP_DAYS} days unreviewed: "
                       f"{os.path.basename(m.get('target') or jid)} - the original stays", "info", jid)
            continue
        m["target_ok"], m["why_not"] = _target_ok(m)
        out.append(m)
    out.sort(key=lambda m: -float(m.get("at") or 0))
    return out


def _target_ok(m: dict) -> tuple[bool, str]:
    t = m.get("target") or ""
    if not t or not os.path.exists(t):
        return False, "the original is no longer there"
    try:
        if m.get("size_before") and os.path.getsize(t) != int(m["size_before"]):
            return False, "the original was replaced since (an arr upgrade?) - this encode is of the old file"
    except OSError:
        pass
    return True, ""


async def accept(job_id: str) -> dict:
    d = hold_dir(_cache_dir())
    try:
        m = json.load(open(os.path.join(d, job_id + ".json"), encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {"ok": False, "why": "no held encode with that id"}
    mkv = os.path.join(d, job_id + ".mkv")
    if not os.path.exists(mkv):
        _remove(d, job_id)
        return {"ok": False, "why": "the held file is gone"}
    ok, why = _target_ok(m)
    if not ok:
        return {"ok": False, "why": why}
    from . import commitqueue
    commitqueue.init()
    commitqueue.enqueue(job_id, int(m["file_id"]), m["target"], mkv, int(m.get("size_before") or 0),
                        int(m.get("size_after") or 0),
                        f"accepted by you despite the final VMAF {m.get('vmaf')}", retry_now=True)
    try:
        os.remove(os.path.join(d, job_id + ".json"))   # the queue owns the .mkv now
    except OSError:
        pass
    with cursor() as cur:
        cur.execute("UPDATE files SET state_reason=NULL WHERE id=? AND state_reason LIKE 'held:%'",
                    (int(m["file_id"]),))
    joblog.log(f"held encode ACCEPTED by you (final VMAF {m.get('vmaf')}): "
               f"{os.path.basename(m['target'])} - swapping it in now", "ok", job_id)
    res = await commitqueue.run_due()
    return {"ok": True, "committed": bool(res.get("ok")), "queue": res}


def discard(job_id: str) -> dict:
    d = hold_dir(_cache_dir())
    if not os.path.exists(os.path.join(d, job_id + ".json")):
        return {"ok": False, "why": "no held encode with that id"}
    _remove(d, job_id)
    joblog.log("held encode discarded by you - the original stays", "info", job_id)
    return {"ok": True}
