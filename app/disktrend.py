r"""Which way is each disk going, and how fast.

WHY THIS EXISTS
---------------
The disk panel says how full every spindle is right now, and "67.3% full" is
a fact with no direction. The questions that actually matter are the ones a
single reading cannot answer: is this disk filling or emptying, is the pool
balancing or has it stopped, is the 2.57 TB nuarr has saved showing up as free
space or being eaten as fast as it is made, and how long until something is
full. All of those are the same question - what did this number do over time -
and nothing was writing the number down.

WHAT IT DOES
------------
Every SAMPLE_S it records used, free, total and file count for every disk the
library knows about, into disk_samples. That is the only writer. Everything
else is arithmetic over the rows: the change over the last day and the last
week, the rate that implies in bytes per day, and - when the rate is upward -
how many days of free space that leaves. A seven-day sparkline series comes
out of the same rows so the panel can draw the shape rather than just the
slope.

HONEST ABOUT HOW MUCH IT HAS SEEN. For the first week the "7d" figure is not
over seven days, and for the first day the "24h" figure is not over 24 hours.
Each window reports the span it actually covers, and the UI says "over 3h"
rather than pretending. Below MIN_SPAN_S there is no trend at all, only
"measuring", because a rate from two readings twenty minutes apart is noise
dressed as a number.

DrivePool balancing shows up here as a disk falling while another rises with
no change to the pool total - which is exactly the thing worth seeing.
"""
from __future__ import annotations

import asyncio
import shutil
import time

from . import joblog
from .db import cursor

SAMPLE_S = 5 * 60           # one reading per disk every five minutes
KEEP_S = 120 * 86400        # four months; ~3,500 rows a day on 12 disks
FIRST_S = 60                # first reading a minute after boot
MIN_SPAN_S = 3600           # under an hour of history there is no trend
STEADY_BPD = 1e9            # under a gigabyte a day reads as "steady"
SPARK_POINTS = 7 * 24       # one point per hour for a week
NOW_S = 30 * 60             # "right now" is the last half hour, against a live reading

_CACHE_TTL = 60.0           # how long the loaded series is reused


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS disk_samples (
                ts     REAL    NOT NULL,
                disk   TEXT    NOT NULL,
                used   INTEGER NOT NULL,
                free   INTEGER NOT NULL,
                total  INTEGER NOT NULL,
                files  INTEGER,
                PRIMARY KEY (disk, ts)
            )""")


# ------------------------------------------------------------- sampling --

def _readings() -> list[dict]:
    """One row per disk, from the same two sources the panel itself uses."""
    from . import scanner
    roots = scanner.media_roots()
    if not roots:
        return []
    counts: dict[str, int] = {}
    try:
        with cursor() as cur:
            for disk, n in cur.execute(
                    "SELECT pool_disk, COUNT(*) FROM files "
                    "WHERE pool_disk IS NOT NULL AND state!='deleted' "
                    "GROUP BY pool_disk"):
                counts[disk] = int(n)
    except Exception:                                        # noqa: BLE001
        pass
    out = []
    for disk, path in roots.items():
        try:
            u = shutil.disk_usage(path)
        except OSError:
            continue
        out.append({"disk": disk, "used": int(u.used), "free": int(u.free),
                    "total": int(u.total), "files": counts.get(disk)})
    return out


def sample() -> int:
    """Take one reading of every disk. Returns how many rows were written."""
    rows = _readings()
    if not rows:
        return 0
    now = time.time()
    with cursor() as cur:
        cur.executemany(
            "INSERT OR REPLACE INTO disk_samples "
            "(ts, disk, used, free, total, files) VALUES (?,?,?,?,?,?)",
            [(now, r["disk"], r["used"], r["free"], r["total"], r["files"])
             for r in rows])
        cur.execute("DELETE FROM disk_samples WHERE ts < ?", (now - KEEP_S,))
    _SERIES["at"] = 0.0                       # a new reading invalidates the maths
    return len(rows)


def _gb(v: float) -> str:
    v = abs(v)
    return f"{v/1e12:.2f} TB" if v >= 1e12 else f"{v/1e9:.1f} GB"


def _sgn(v: float) -> str:
    return ("+" if v > 0 else "−" if v < 0 else "") + _gb(v)


def daily_line() -> str:
    """One line a day: where the pool and its most-moved disks are going."""
    t = trends()
    pool = t.get("POOL") or {}
    if pool.get("d24") is None:
        return ""
    disks = {k: v for k, v in t.items() if k != "POOL" and v and v.get("d24") is not None}
    bits = [f"pool {_sgn(pool['d24'])} over the last {round(pool['span24_s']/3600)}h"]
    if disks:
        up = max(disks.items(), key=lambda kv: kv[1]["d24"])
        dn = min(disks.items(), key=lambda kv: kv[1]["d24"])
        if up[1]["d24"] > 0:
            bits.append(f"filling fastest {up[0]} {_sgn(up[1]['d24'])}")
        if dn[1]["d24"] < 0:
            bits.append(f"emptying fastest {dn[0]} {_sgn(dn[1]['d24'])}")
    if pool.get("days_left"):
        bits.append(f"at this rate the pool is full in {round(pool['days_left'])} days")
    return "disk trend: " + " · ".join(bits)


async def watch() -> None:
    await asyncio.sleep(FIRST_S)
    from . import schedules
    # A SAMPLER THAT SAYS NOTHING WHEN IT WORKS IS INDISTINGUISHABLE FROM ONE
    # THAT NEVER STARTED. One line at boot with what it is watching, one line
    # a day with what it saw. Every five minutes would be noise; never is a
    # gap - and this was the one system with no log entry at all.
    last_daily = 0.0
    first = True
    while True:
        schedules.beat("disktrend")
        try:
            n = await asyncio.to_thread(sample)
            if first:
                joblog.log(f"disk trend: sampling {n} disk(s) every "
                           f"{SAMPLE_S // 60} min, keeping {KEEP_S // 86400} days",
                           "debug")
                first = False
            if time.time() - last_daily >= 86400:
                line = await asyncio.to_thread(daily_line)
                if line:
                    joblog.log(line, "info")
                    last_daily = time.time()
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"disk trend sample: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(SAMPLE_S)


# --------------------------------------------------------------- maths --

def _at_or_before(series: list[tuple], ts: float) -> tuple | None:
    """The latest row no later than ts; else the earliest row at all."""
    best = None
    for row in series:                      # ascending by ts
        if row[0] <= ts:
            best = row
        else:
            break
    return best or (series[0] if series else None)


def _window(series: list[tuple], now_row: tuple, back_s: float) -> dict:
    """Change from the reading nearest `back_s` ago to now, with its real span."""
    then = _at_or_before(series, now_row[0] - back_s)
    if not then or then is now_row:
        return {"delta": None, "span_s": 0}
    span = now_row[0] - then[0]
    if span < MIN_SPAN_S:
        return {"delta": None, "span_s": span}
    return {"delta": now_row[1] - then[1], "span_s": span}


def _spark(series: list[tuple], now: float) -> list:
    """Last week as one value per hour: the last reading in each hour."""
    start = now - SPARK_POINTS * 3600
    buckets: dict[int, tuple] = {}
    for ts, used, *_ in series:
        if ts < start:
            continue
        buckets[int((ts - start) // 3600)] = (ts, used)
    return [[round(v[0]), v[1]] for _, v in sorted(buckets.items())]


def _now_rate(series: list[tuple], live: tuple | None) -> dict:
    r"""Bytes per second over the last NOW_S, ending at the live reading.

    THIS IS THE NUMBER THAT SEES A BALANCER MOVE. The day rate is the honest
    long view, but it takes a day to tell you what happened, and a
    rebalance or a batch of commits is over in an hour. The live reading
    comes from the same disk_usage() call the panel already makes, so it is
    as fresh as the row it sits on; the far end is the stored reading
    nearest half an hour back.
    """
    end = live or (series[-1] if series else None)
    if not end or not series:
        return {"bps": None, "span_s": 0, "delta": None}
    then = _at_or_before(series, end[0] - NOW_S)
    if not then or then[0] >= end[0]:
        return {"bps": None, "span_s": 0, "delta": None}
    span = end[0] - then[0]
    if span < 240:                                   # under four minutes: noise
        return {"bps": None, "span_s": span, "delta": None}
    delta = end[1] - then[1]
    return {"bps": delta / span, "span_s": span, "delta": delta}


def _trend_of(series: list[tuple], now: float, live: tuple | None = None) -> dict | None:
    if not series and not live:
        return None
    latest = live or series[-1]
    d1 = _window(series, latest, 86400) if series else {"delta": None, "span_s": 0}
    d7 = _window(series, latest, 7 * 86400) if series else {"delta": None, "span_s": 0}
    basis = d1 if d1["delta"] is not None else d7
    bpd = None
    if basis["delta"] is not None and basis["span_s"] > 0:
        bpd = basis["delta"] / basis["span_s"] * 86400
    # NO FORECAST FROM A FEW HOURS. Two hours of the queue committing at
    # 2.4 TB a day said "full in 2 days" - true if it kept up, which it never
    # does, and the same over-reach as the sparkline that just came out. A
    # day's worth of readings is the least that can carry a forecast.
    days_left = None
    if bpd and bpd > 0 and latest[2] > 0 and basis["span_s"] >= 12 * 3600:
        days_left = latest[2] / bpd
    nowr = _now_rate(series, live)
    first = series[0][0] if series else latest[0]
    return {
        "bpd": bpd,                             # bytes per day, over the last day
        "now_bps": nowr["bps"],                 # bytes per second, last half hour
        "now_span_s": round(nowr["span_s"]), "now_delta": nowr["delta"],
        "d24": d1["delta"], "span24_s": round(d1["span_s"]),
        "d7": d7["delta"], "span7_s": round(d7["span_s"]),
        "days_left": (round(days_left, 1) if days_left is not None else None),
        "steady": (bpd is not None and abs(bpd) < STEADY_BPD),
        "first_ts": first, "last_ts": latest[0],
        "history_s": round(latest[0] - first),
        "n": len(series) + (1 if live else 0),
    }


_SERIES: dict = {"at": 0.0, "by_disk": {}, "pool": []}


def _load(now: float) -> tuple[dict, list]:
    """Eight days of readings, per disk and summed, cached a minute."""
    if _SERIES["by_disk"] and now - _SERIES["at"] < _CACHE_TTL:
        return _SERIES["by_disk"], _SERIES["pool"]
    by_disk: dict[str, list[tuple]] = {}
    pool: dict[float, list] = {}
    try:
        with cursor() as cur:
            rows = cur.execute(
                "SELECT ts, disk, used, free FROM disk_samples "
                "WHERE ts >= ? ORDER BY ts", (now - 8 * 86400,)).fetchall()
    except Exception:                                        # noqa: BLE001
        rows = []
    for ts, disk, used, free in rows:
        by_disk.setdefault(disk, []).append((ts, used, free))
        p = pool.setdefault(ts, [0, 0, 0])
        p[0] += used
        p[1] += free
        p[2] += 1
    # THE POOL ONLY COUNTS READINGS THAT SAW EVERY DISK. A sample that missed
    # one spindle would show the pool dropping by a whole disk's worth.
    full = max((p[2] for p in pool.values()), default=0)
    pseries = [(ts, p[0], p[1]) for ts, p in sorted(pool.items()) if p[2] == full]
    _SERIES.update(at=now, by_disk=by_disk, pool=pseries)
    return by_disk, pseries


def trends(live: dict | None = None) -> dict:
    r"""Per-disk trend plus the pool. Numbers only - series are fetched on demand.

    `live` is {disk: (used, free)} from the reading the panel just took, so
    the "now" rate ends at this second rather than at the last stored sample.
    """
    now = time.time()
    by_disk, pseries = _load(now)
    live = live or {}
    out = {}
    for d, ser in by_disk.items():
        lv = live.get(d)
        out[d] = _trend_of(ser, now, (now, lv[0], lv[1]) if lv else None)
    if live and pseries and len(live) == len(by_disk):
        lp = (now, sum(v[0] for v in live.values()), sum(v[1] for v in live.values()))
    else:
        lp = None
    out["POOL"] = _trend_of(pseries, now, lp)
    return out


def series(disk: str) -> dict:
    """The two charts for one disk (or POOL): last 24h at full grain, last 7d hourly."""
    now = time.time()
    by_disk, pseries = _load(now)
    ser = pseries if disk == "POOL" else by_disk.get(disk, [])
    day = [[round(ts), used] for ts, used, *_ in ser if ts >= now - 86400]
    return {"disk": disk, "now": round(now), "day": day, "week": _spark(ser, now),
            "n": len(ser)}
