"""Bitrate check: is each library file's bitrate sensible for its codec and
resolution?

WHY THIS EXISTS. The Sandman S01E05 was a 5 Mbps 4K file - the arr profile
picked it by release-group tier, and nothing looked at how many bits it had.
Dark scenes showed outlines around faces. This table is the missing look:
per codec and resolution, the bitrate below which a copy is starved (replace
it with a fuller one) and above which it is oversized (bits past the point
anyone can see).

SOURCES (2026-11-22): streaming-encoder tables (H.264 1080p 4-8, HEVC
2.5-5, AV1 2-4; 4K 12-20 / 8-15 / 6-12), Netflix's 4K ladder (8-16 Mbps),
YouTube's upload table, and TRaSH's minimum sizes (1080p WEB 15 MB/min =
2 Mbps, 2160p WEB 25-34.5 MB/min = 3.3-4.6 Mbps, anime 5 MB/min).

THE FIGURES are whole-file bitrates in Mbps (video + audio, what the probe
stored), for live action:
  floor   under this the copy is starved        -> "under", replace
  lo..hi  the recommended range
  ceil    over this it is oversized             -> "over"
Animation compresses far better (flat colour, little grain), so the four
animation libraries use ANIM_SCALE of every figure.

A FILE NUARR ENCODED ITSELF IS JUDGED BY ITS MEASUREMENT, NOT ITS BITRATE.
The quality search picked its CQ by VMAF, so a low bitrate there is the
content being easy, not the copy being starved: a row in vmaf_log with a
score of MEASURED_OK or better is "measured", never "under".
"""
from __future__ import annotations

import time

from .db import cursor

# (codec family, resolution class) -> (floor, lo, hi, ceil) Mbps
TABLE = {
    ("hevc", "2160p"): (8.0, 12.0, 25.0, 40.0),
    ("hevc", "1080p"): (2.0, 2.5, 6.0, 10.0),
    ("hevc", "720p"): (1.0, 1.5, 3.0, 5.0),
    ("hevc", "SD"): (0.5, 0.8, 1.5, 3.0),
    ("h264", "2160p"): (12.0, 20.0, 40.0, 60.0),
    ("h264", "1080p"): (3.0, 4.5, 8.0, 16.0),
    ("h264", "720p"): (1.8, 2.5, 5.0, 8.0),
    ("h264", "SD"): (0.8, 1.2, 2.5, 4.0),
    ("av1", "2160p"): (6.0, 8.0, 15.0, 30.0),
    ("av1", "1080p"): (1.5, 2.0, 4.5, 8.0),
    ("av1", "720p"): (0.7, 1.0, 2.0, 4.0),
    ("av1", "SD"): (0.4, 0.6, 1.2, 2.5),
}
ANIM_SCALE = 0.6
# THE ANIMATION FLOOR IS LOWER STILL. At 60 % the floor called 4,117 anime
# H.264 1080p files starved, and they are not: TRaSH's own minimum for every
# anime quality is 5 MB/min (0.7 Mbps), against 15 MB/min (2 Mbps) for a
# live-action 1080p WEB-DL. So the floor alone uses 40 %.
ANIM_FLOOR_SCALE = 0.4
MEASURED_OK = 93.0
MIN_SECS = 300            # trailers and extras are not judged
MEMO_S = 600
_MEMO: dict = {"at": 0.0, "data": None}
RES_ORDER = ["2160p", "1080p", "720p", "SD"]
FAM_NAME = {"hevc": "HEVC (H.265)", "h264": "H.264 (AVC)", "av1": "AV1"}


def family(codec: str) -> str:
    c = str(codec or "").lower()
    if c in ("hevc", "h265", "x265"):
        return "hevc"
    if c in ("h264", "avc", "x264"):
        return "h264"
    if c == "av1":
        return "av1"
    return ""


def res_class(height) -> str:
    # By height alone, so the bands leave room for scope crops: a 2.39:1 4K
    # film is 3840x1600 and a 1080p one 1920x800.
    h = int(height or 0)
    return "2160p" if h >= 1500 else "1080p" if h >= 780 else "720p" if h >= 500 else "SD"


def is_anim(library: str) -> bool:
    return "anim" in str(library or "").lower()


def limits(fam: str, res: str, anim: bool):
    t = TABLE.get((fam, res))
    if not t:
        return None
    if not anim:
        return t
    return (round(t[0] * ANIM_FLOOR_SCALE, 2),) + tuple(round(x * ANIM_SCALE, 2) for x in t[1:])


def verdict(mbps: float, lim, measured: bool) -> str:
    if mbps < lim[0]:
        return "measured" if measured else "under"
    if mbps > lim[3]:
        return "over"
    return "ok"


def report(force: bool = False) -> dict:
    if not force and _MEMO["data"] and time.time() - _MEMO["at"] < MEMO_S:
        return _MEMO["data"]
    with cursor() as cur:
        rows = cur.execute(
            "SELECT id, path, library, title, size, video_codec, height, duration, bitrate, "
            "       arr_name, arr_parent_id FROM files "
            "WHERE state != 'deleted' AND video_codec IS NOT NULL AND duration > ?", (MIN_SECS,)).fetchall()
        meas = {r[0]: r[1] for r in cur.execute(
            "SELECT file_id, MAX(COALESCE(final_vmaf, vmaf)) FROM vmaf_log "
            "WHERE file_id IS NOT NULL GROUP BY file_id").fetchall() if r[1] is not None}
    cells: dict = {}
    groups: dict = {"under": {}, "over": {}}
    for r in rows:
        fam, res = family(r["video_codec"]), res_class(r["height"])
        anim = is_anim(r["library"])
        lim = limits(fam, res, anim)
        if not lim:
            continue
        bps = r["bitrate"] or (r["size"] * 8 / r["duration"] if r["size"] and r["duration"] else 0)
        if not bps:
            continue
        mbps = bps / 1e6
        v = verdict(mbps, lim, (meas.get(r["id"]) or 0) >= MEASURED_OK)
        c = cells.setdefault((fam, res, anim), {"n": 0, "under": 0, "over": 0, "ok": 0, "measured": 0,
                                                "gb": 0.0, "over_gb": 0.0, "all": []})
        c["n"] += 1
        c[v] += 1
        c["gb"] += (r["size"] or 0) / 1e9
        c["all"].append(mbps)
        if v == "over":
            c["over_gb"] += (r["size"] or 0) / 1e9
        if v in groups:
            g = groups[v].setdefault((r["library"], r["title"] or "?", fam, res), {
                "library": r["library"], "title": r["title"] or "?", "fam": fam, "res": res,
                "anim": anim, "lim": lim, "n": 0, "sum": 0.0, "lo": 1e9, "hi": 0.0, "gb": 0.0,
                "arr": r["arr_name"], "pid": r["arr_parent_id"]})
            g["n"] += 1
            g["sum"] += mbps
            g["lo"] = min(g["lo"], mbps)
            g["hi"] = max(g["hi"], mbps)
            g["gb"] += (r["size"] or 0) / 1e9
    table = []
    for (fam, res, anim), c in cells.items():
        a = sorted(c.pop("all"))
        lim = limits(fam, res, anim)
        table.append({"fam": fam, "name": FAM_NAME[fam], "res": res, "anim": anim,
                      "floor": lim[0], "lo": lim[1], "hi": lim[2], "ceil": lim[3],
                      "median": round(a[len(a) // 2], 1), **{k: (round(v, 1) if isinstance(v, float) else v)
                                                             for k, v in c.items()}})
    table.sort(key=lambda t: (t["anim"], ["hevc", "h264", "av1"].index(t["fam"]), RES_ORDER.index(t["res"])))

    def out(d, key):
        rows = []
        for g in d.values():
            rows.append({**{k: g[k] for k in ("library", "title", "fam", "res", "anim", "n", "arr", "pid")},
                         "avg": round(g["sum"] / g["n"], 1), "lo": round(g["lo"], 1),
                         "hi": round(g["hi"], 1), "gb": round(g["gb"], 1),
                         "floor": g["lim"][0], "ceil": g["lim"][3], "rlo": g["lim"][1], "rhi": g["lim"][2]})
        rows.sort(key=key)
        return rows
    data = {"at": time.time(), "anim_scale": ANIM_SCALE, "anim_floor_scale": ANIM_FLOOR_SCALE, "measured_ok": MEASURED_OK,
            "files": sum(t["n"] for t in table), "table": table,
            # under: the most starved first (furthest below the floor, as a share)
            "under": out(groups["under"], lambda g: (g["avg"] / g["floor"], -g["n"])),
            # over: the most disk to win back first
            "over": out(groups["over"], lambda g: -g["gb"])}
    _MEMO.update(at=time.time(), data=data)
    return data
