r"""nuarr - the live numbers for work that runs outside the job queue.

WHY. A queue job's card has a bar, a rate, an ETA and read/write because a
Worker measures its ffmpeg. The Subtitle maker, the Qwen judge, the second
opinion, subtitle timing and the group measurer run their own child
processes, and their cards came with a stage line and nothing else - "fps -",
no disk figures, a bar that only moved when a stage changed.

WHAT IT DOES. Each of them hands its child's pid to set_pid(); fields() then
reports the disk traffic of that process AND everything it started (a Python
worker reads the file through an ffmpeg of its own, so the root process alone
reads nothing), smoothed the way the queue's cards are, plus a rate and an ETA
from the progress the caller reports. One place, so every outside card says
the same things in the same words.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_M: dict = {}          # key -> meter state


def set_pid(key: str, pid) -> None:
    with _LOCK:
        m = _M.setdefault(key, {})
        m.update(pid=int(pid or 0), started=m.get("started") or time.time())


def start(key: str) -> None:
    """A new piece of work under this key: totals and rates from zero."""
    with _LOCK:
        _M[key] = {"pid": 0, "started": time.time(), "per_pid": {}, "read_total": 0, "write_total": 0,
                   "read_bps": 0.0, "write_bps": 0.0, "at": 0.0, "prog": []}


def stop(key: str) -> None:
    with _LOCK:
        _M.pop(key, None)


def _tree(pid: int) -> list:
    try:
        import psutil
        p = psutil.Process(pid)
        return [p] + p.children(recursive=True)
    except Exception:                                            # noqa: BLE001
        return []


def _sample(m: dict) -> None:
    """Differenced per process, so a child that exits or a new one that
    starts never shows as a spike or a negative."""
    now = time.time()
    if now - m.get("at", 0.0) < 0.4:
        return
    dr = dw = 0
    seen = {}
    for p in _tree(int(m.get("pid") or 0)):
        try:
            io = p.io_counters()
        except Exception:                                        # noqa: BLE001
            continue
        last = m.setdefault("per_pid", {}).get(p.pid)
        if last:
            dr += max(0, io.read_bytes - last[0])
            dw += max(0, io.write_bytes - last[1])
        seen[p.pid] = (io.read_bytes, io.write_bytes)
    m["per_pid"] = seen
    dt = now - m["at"] if m.get("at") else 0.0
    m["at"] = now
    m["read_total"] = m.get("read_total", 0) + dr
    m["write_total"] = m.get("write_total", 0) + dw
    if dt > 0:
        ir, iw = dr / dt, dw / dt
        m["read_bps"] = 0.3 * ir + 0.7 * m.get("read_bps", ir)
        m["write_bps"] = 0.3 * iw + 0.7 * m.get("write_bps", iw)
        if not seen:
            m["read_bps"] = m["write_bps"] = 0.0


_DISK: dict = {}


def disk_of(file_id) -> str:
    """Which pool disk a file is on, remembered - the cards are polled often."""
    if not file_id:
        return ""
    k = int(file_id)
    if k not in _DISK:
        try:
            from .db import cursor
            with cursor() as cur:
                r = cur.execute("SELECT pool_disk FROM files WHERE id=?", (k,)).fetchone()
            _DISK[k] = (r["pool_disk"] if r else "") or ""
        except Exception:                                        # noqa: BLE001
            return ""
        if len(_DISK) > 500:
            _DISK.clear()
    return _DISK.get(k, "")


def fields(key: str, progress: float = 0.0, done: float | None = None, unit: str = "",
           per: float = 1.0, media_s: float = 0.0) -> dict:
    """The card's live numbers.

    progress  0..1, for the ETA (from the recent pace, not the average since
              the start - a job that waited for the GPU first is not slow)
    done      units finished (lines judged, files, frames) -> a rate in
              `unit` per `per` seconds, shown where a queue card shows fps
    media_s   seconds of the file this work covers -> "N x realtime"
    """
    with _LOCK:
        m = _M.get(key)
        if m is None:
            return {}
        _sample(m)
        now = time.time()
        el = now - float(m.get("started") or now)
        pr = m.setdefault("prog", [])
        p = max(0.0, min(1.0, float(progress or 0.0)))
        if not pr or pr[-1][1] != p:
            pr.append((now, p))
            del pr[:-30]
        eta = None
        if 0.01 < p < 1.0:
            old = [x for x in pr if now - x[0] <= 180] or pr
            t0, p0 = old[0]
            rate = (p - p0) / (now - t0) if now - t0 > 5 and p > p0 else (p / el if el > 0 else 0)
            if rate > 0:
                eta = round((1.0 - p) / rate)
        out = {"read_bps": round(m.get("read_bps", 0.0)), "write_bps": round(m.get("write_bps", 0.0)),
               "io_bps": round(m.get("read_bps", 0.0) + m.get("write_bps", 0.0)),
               "read_total": int(m.get("read_total", 0)), "write_total": int(m.get("write_total", 0)),
               "eta_s": eta, "eta_est": True}
        if done is not None and el > 1 and unit:
            out["rate"] = round(float(done) / el * per, 1)
            out["rate_unit"] = unit
        if media_s and el > 1 and p > 0:
            out["speed"] = round(media_s * p / el, 1)
        return out
