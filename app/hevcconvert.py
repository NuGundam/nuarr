r"""Library conversion to 10-bit HEVC, fed into the queue a little at a time.

WHY. 8-bit H.264 anime bands at any CQ that saves much (CAMBI 5-6 at CQ 20,
measured), while 10-bit HEVC held VMAF 95 with banding ~2.5 at CQ 26-29 and
came out 85-90 % smaller on the 322 oversized files. The rest of the anime
library is mostly 8-bit H.264 below the shrink ceiling - 16,795 files, 10 TB.
The lean ones (under ~4 Mbps at 1080p) have little to give and a generation
to lose, so a floor per resolution decides which are worth it.

HOW. The planner does the work (rules.decide reads this module's config:
a file in a chosen library, 8-bit H.264, SDR, above the floor becomes a
10-bit HEVC encode at the VMAF-searched CQ). Any plan for such a file - a new
arrival included - converts it. This module only FEEDS the backlog: it keeps
`keep` conversion jobs queued, highest bitrate first, spread across disks,
so the queue stays a buffer and other work is never starved.

A conversion must save at least MIN_SAVING or the encode is discarded and the
original kept (jobs.py size gate, via plan.venc["min_saving"]). A file that
was discarded, failed or skipped once is not offered again.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import joblog
from .db import cursor, kv_get, kv_set

KV = "convert.hevc10"
SOURCE = "hevc10 convert"
MIN_SAVING = 0.20
DEFAULTS = {"on": False, "libraries": ["Anime Shows"],
            "min_mbps": {"1080": 4.0, "720": 3.0, "sd": 2.0}, "keep": 24}
TICK_S = 120
STATE: dict = {"at": 0.0, "cands": [], "cand_at": 0.0, "last": ""}


def cfg() -> dict:
    try:
        d = json.loads(kv_get(KV) or "{}")
    except Exception:                                    # noqa: BLE001
        d = {}
    c = {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    c["min_mbps"] = {**DEFAULTS["min_mbps"], **(c.get("min_mbps") or {})}
    return c


def set_cfg(**kw) -> dict:
    c = cfg()
    for k, v in kw.items():
        if v is None or k not in DEFAULTS:
            continue
        if k == "on":
            c[k] = bool(v)
        elif k == "keep":
            c[k] = max(1, min(200, int(v)))
        elif k == "libraries":
            c[k] = [x for x in v if x]
        elif k == "min_mbps":
            c[k] = {**c["min_mbps"], **{t: max(0.5, float(x)) for t, x in v.items()}}
    kv_set(KV, json.dumps(c))
    STATE["cand_at"] = 0.0
    return c


def tier(height: int) -> str:
    return "1080" if height > 760 else "720" if height > 580 else "sd"


def lib_cfg(library: str | None) -> dict | None:
    """The library's own switch and floors, from the Video codec page -
    None when the library does not convert."""
    if not library:
        return None
    try:
        from . import codecpolicy
        v = codecpolicy.for_library(library, "video")
    except Exception:                                    # noqa: BLE001
        return None
    if not v.get("hevc10_convert"):
        return None
    return {"1080": float(v.get("hevc10_1080_mbps") or 4), "720": float(v.get("hevc10_720_mbps") or 3),
            "sd": float(v.get("hevc10_sd_mbps") or 2)}


def libraries() -> list:
    """Every library whose policy has the conversion switched on."""
    try:
        from . import codecpolicy
        return [name for name in (codecpolicy.load() or {}) if lib_cfg(name)]
    except Exception:                                    # noqa: BLE001
        return []


def wants(library: str | None, vcodec: str, ten_bit: bool, hdr: bool,
          height: int, mbps: float) -> bool:
    """The planner's question: should this file become 10-bit HEVC?
    The library's switch decides; the master switch (on) only pauses it."""
    if not cfg()["on"]:
        return False
    floors = lib_cfg(library)
    if floors is None or vcodec != "h264" or ten_bit or hdr:
        return False
    return mbps >= floors.get(tier(height or 0), 99.0)


# A TRIAL. Files picked by hand (kv convert.hevc10.trial, a list of file ids)
# convert while their library's switch is still off and whatever their
# bitrate - a handful of results to look at before a whole library goes.
# Same limits as the real thing: 8-bit H.264, SDR, and the 20% saving gate.
# The list lives in kv because a plan is decided again when the job starts.
TRIAL_KV = "convert.hevc10.trial"


def trial_ids() -> set:
    try:
        return {int(x) for x in json.loads(kv_get(TRIAL_KV) or "[]")}
    except Exception:                                    # noqa: BLE001
        return set()


def trial(file_id, vcodec: str, ten_bit: bool, hdr: bool) -> bool:
    if not file_id or vcodec != "h264" or ten_bit or hdr or not cfg()["on"]:
        return False
    return int(file_id) in trial_ids()


# A CONVERSION IS KNOWN BY WHAT ITS PLAN SAYS, not by who queued it: a
# re-plan sweep ("subtitle rules changed") queued 100 of them on 2026-10-22,
# and the panel, the pace and the do-not-retry list all missed them.
PLAN_MARK = "%convert the video to 10-bit HEVC%"


def _done_ids(cur) -> set:
    """Files that have already had a go - converted, discarded or failed."""
    return {r["file_id"] for r in cur.execute(
        "SELECT DISTINCT file_id FROM jobs WHERE (source=? OR plan_json LIKE ?) "
        "AND state NOT IN ('queued','running')", (SOURCE, PLAN_MARK))}


_GAVE_UP: dict = {"at": 0.0, "map": {}}


def gave_up(file_id: int, size_bytes: int = 0) -> bool:
    """THE PLANNER'S DO-NOT-RETRY. The feeder above already skips a file that
    has had a go, but the planner did not - so every other path that re-plans
    a file (the subtitle rules drain above all) asked for the conversion again,
    the 20% gate threw it away again, and whatever else rode on that plan (a
    subtitle strip, a burn) was thrown away with it. Measured 2026-11-01: 41
    files, 1,777 discarded encodes, ~294 encode-hours; Cyberpunk: Edgerunners
    S02E03 alone ~80 times, never losing its 36 unwanted subtitle tracks.

    Same file only: a discard recorded against a different size (a new
    release, a rewrite) does not count, so a replacement gets its own try.
    Cached for five minutes - decide() is called for every plan."""
    now = time.time()
    if now - _GAVE_UP["at"] > 300:
        m: dict = {}
        try:
            with cursor() as cur:
                for r in cur.execute(
                        "SELECT file_id, size_before FROM jobs "
                        " WHERE plan_json LIKE ? AND state='skipped' "
                        "   AND (coalesce(result_json,'') LIKE '%discarded%' "
                        "        OR coalesce(error,'') LIKE '%discarded%')",
                        (PLAN_MARK,)):
                    m.setdefault(int(r["file_id"] or 0), set()).add(int(r["size_before"] or 0))
        except Exception:                                        # noqa: BLE001
            m = _GAVE_UP["map"]
        _GAVE_UP.update(at=now, map=m)
    sizes = _GAVE_UP["map"].get(int(file_id or 0))
    if not sizes:
        return False
    return not size_bytes or int(size_bytes) in sizes or 0 in sizes


def candidates(force: bool = False) -> list:
    """Every file still worth converting, highest bitrate first. Cached 30 min."""
    if not force and STATE["cands"] and time.time() - STATE["cand_at"] < 1800:
        return STATE["cands"]
    libs = libraries()
    # THE HAND-PICKED LIST IS FED TOO, a few at a time like a library. For
    # TV Shows it is the files Sonarr scores the same or better as 10-bit
    # HEVC (2026-11-11: 511 of 1,332; the other 821 are h264 WEB-DLs that
    # lose the profile's "1080p WEB-DL (Efficient)" 860,000 once converted,
    # fall under the profile minimum and would be downloaded again).
    picked = sorted(trial_ids())
    out = []
    if not libs and not picked:
        STATE.update(cands=[], cand_at=time.time())
        return out
    with cursor() as cur:
        tried = _done_ids(cur)
        busy = {r["file_id"] for r in cur.execute(
            "SELECT file_id FROM jobs WHERE state IN ('queued','running') AND file_id IS NOT NULL")}
        q = ",".join("?" * len(libs)) or "''"
        pq = ",".join(str(int(x)) for x in picked) or "0"
        for r in cur.execute(
                f"SELECT f.id, f.path, f.title, f.size, f.duration, f.height, f.pool_disk, f.library, p.json probe "
                f"FROM files f JOIN file_probes p ON p.file_id=f.id WHERE (f.library IN ({q}) OR f.id IN ({pq})) "
                f"AND f.state NOT IN ('deleted','duplicate') AND f.duration>240 AND f.size>0",
                tuple(libs)):
            if r["id"] in tried or r["id"] in busy:
                continue
            try:
                vs = next(s for s in json.loads(r["probe"] or "{}").get("streams", [])
                          if s.get("codec_type") == "video")
            except StopIteration:
                continue
            mbps = r["size"] * 8 / r["duration"] / 1e6
            hdr = str(vs.get("color_transfer") or "") in ("smpte2084", "arib-std-b67")
            ten = "10" in (vs.get("pix_fmt") or "")
            if (wants(r["library"], vs.get("codec_name") or "", ten, hdr, r["height"] or 0, mbps)
                    or trial(r["id"], vs.get("codec_name") or "", ten, hdr)):
                out.append({"id": r["id"], "path": r["path"], "title": r["title"] or "",
                            "mbps": round(mbps, 2), "gb": r["size"] / 1e9, "disk": r["pool_disk"] or ""})
    out.sort(key=lambda x: -x["mbps"])
    STATE.update(cands=out, cand_at=time.time())
    return out


def _spread(rows: list, n: int) -> list:
    """Highest bitrate first, but round-robin across disks."""
    by: dict = {}
    for r in rows:
        by.setdefault(r["disk"], []).append(r)
    out = []
    while len(out) < n and any(by.values()):
        for d in list(by):
            if by[d]:
                out.append(by[d].pop(0))
                if len(out) >= n:
                    break
    return out


async def feed() -> int:
    c = cfg()
    if not c["on"]:
        return 0
    from . import jobs
    with cursor() as cur:
        queued = cur.execute("SELECT COUNT(*) n FROM jobs WHERE (source=? OR plan_json LIKE ?) "
                             "AND state IN ('queued','running')", (SOURCE, PLAN_MARK)).fetchone()["n"]
    room = c["keep"] - queued
    if room <= 0:
        return 0
    pick = _spread(await asyncio.to_thread(candidates), room)
    n = 0
    for r in pick:
        try:
            if await jobs.enqueue(r["id"], r["path"], r["title"], source=SOURCE, priority=80):
                n += 1
        except Exception as e:                           # noqa: BLE001
            joblog.log(f"HEVC conversion: could not queue {r['path'][-60:]}: {e}", "warn")
    if n:
        STATE["cands"] = [x for x in STATE["cands"] if x["id"] not in {p["id"] for p in pick}]
        STATE["last"] = f"queued {n}"
    return n


def progress() -> dict:
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT state, size_before, size_after, started_at, finished_at FROM jobs "
            "WHERE source=? OR plan_json LIKE ?", (SOURCE, PLAN_MARK))]
    done = [r for r in rows if r["state"] == "done" and r["size_after"]]
    b = sum(r["size_before"] or 0 for r in done) / 1e9
    a = sum(r["size_after"] or 0 for r in done) / 1e9
    hr = [r for r in done if (r["finished_at"] or 0) > time.time() - 6 * 3600]
    left = len(STATE["cands"]) + sum(1 for r in rows if r["state"] in ("queued", "running"))
    rate = len(hr) / 6.0
    # WHAT THE SLOTS CAN DO, when the measured pace says less than half of it:
    # a held queue or a restart in the window is not the conversion's speed.
    durs = sorted((r["finished_at"] - r["started_at"]) for r in done
                  if r.get("finished_at") and r.get("started_at"))
    if durs:
        try:
            from . import workers
            slots = max(1, int(workers.get().as_dict()["encode_workers"]["value"]))
        except Exception:                                # noqa: BLE001
            slots = 4
        capacity = slots * 3600.0 / durs[len(durs) // 2]
        if rate < capacity / 2:
            rate = capacity
    return {"cfg": cfg(), "libraries": libraries(), "done": len(done), "before_gb": round(b, 1), "after_gb": round(a, 1),
            "kept_original": sum(1 for r in rows if r["state"] == "skipped"),
            "failed": sum(1 for r in rows if r["state"] == "failed"),
            "queued": sum(1 for r in rows if r["state"] in ("queued", "running")),
            "left": left, "left_gb": round(sum(x["gb"] for x in STATE["cands"]), 0),
            "per_hour": round(rate, 1), "eta_h": round(left / rate, 1) if rate else None,
            "min_saving": MIN_SAVING, "last": STATE["last"]}


async def watch() -> None:
    from . import schedules
    schedules.register("hevcconvert", "HEVC conversion feeder", "Processing", TICK_S,
                       what="Keeps a few library conversions to 10-bit HEVC queued, highest bitrate first.")
    await asyncio.sleep(90)
    while True:
        try:
            schedules.beat("hevcconvert")
            n = await feed()
            if n:
                schedules.REG["hevcconvert"]["last_result"] = f"queued {n}"
        except Exception as e:                           # noqa: BLE001
            joblog.log(f"HEVC conversion feeder: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(TICK_S)
