r"""Release groups, scored by what their files are actually like.

WHY. Picking between releases is guesswork from names and reputations. nuarr
holds 35,000 files from ~1,300 groups and can look at the pictures, so it can
say which groups deliver banding, blocking or smeared detail - measured, on
this library - and which ones keep turning up in the incident log.

WHAT IS MEASURED (no reference needed - a release has no perfect original):
  banding    CAMBI, Netflix's banding index (the quality search's measure)
  blocking   ffmpeg blockdetect - compression blocks
  blur       ffmpeg blurdetect  - smeared detail, upscales, heavy denoise
on three short samples per file (20/50/80 %), every third frame, at below-
normal priority. A group is sampled, not swept: at most PER_GROUP files per
library and PER_SHOW per show, so a group's score is not one show's encode.

WHAT IS COUNTED (already on record): bitrate for the resolution, codec and
bit depth, bans and strikes against the group, files the decode check found
broken, and how far conversions and shrinks could take its files at VMAF 95.

Read-only. Nothing here touches Sonarr, Radarr or a profile.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import subprocess
import tempfile
import time

from . import joblog
from .config import NO_WINDOW, hidden_si
from .db import cursor, kv_get, kv_set

PER_GROUP = 12
PER_SHOW = 3
SAMPLE_S = 4.0
REV = 1
KV_PAUSED = "groups.paused"
BELOW_NORMAL = 0x00004000
STATE: dict = {"running": False, "label": "", "at": 0.0, "last": "", "done_session": 0,
               "left": None, "left_at": 0.0}
# THE TWO SLOW PARTS OF THE SCORECARD, CACHED. Scanning 105 MB of plan_json
# for shrink results took 0.8 s and re-planning the whole queue to count
# what is left 0.4 s - on a page that refreshes every minute. Shrink results
# change a few times an hour, the count once per measured file.
_SHRINK: dict = {"at": 0.0, "rows": []}
SHRINK_TTL = 600.0


def _shrink_rows() -> list:
    if time.time() - _SHRINK["at"] < SHRINK_TTL:
        return _SHRINK["rows"]
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT file_id, size_before, size_after FROM jobs WHERE kind='transcode' AND state='done' "
            "AND size_before>0 AND size_after>0 AND size_after<size_before "
            "AND (plan_json LIKE '%convert the video to 10-bit HEVC%' OR plan_json LIKE '%shrink the video%')")]
    _SHRINK.update(at=time.time(), rows=rows)
    return rows


def _left() -> int | None:
    if paused():
        return None
    if STATE["left"] is None or time.time() - STATE["left_at"] > 600:
        STATE.update(left=len(plan(100000)), left_at=time.time())
    return STATE["left"]


def init() -> None:
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS group_measure(
            file_id INTEGER PRIMARY KEY, library TEXT, grp TEXT, show TEXT, path TEXT,
            height INTEGER, codec TEXT, tenbit INTEGER, mbps REAL,
            banding REAL, banding_max REAL, block REAL, blur REAL,
            at REAL, rev INTEGER, err TEXT)""")


def paused() -> bool:
    return str(kv_get(KV_PAUSED) or "0") == "1"


def set_paused(on: bool) -> None:
    kv_set(KV_PAUSED, "1" if on else "0")


def _show(path: str) -> str:
    parts = (path or "").split("\\")
    return parts[2] if len(parts) > 3 else (parts[-2] if len(parts) > 1 else "")


def _ff() -> str:
    from . import jobs
    return jobs._ffmpeg_exe()


# ---------------------------------------------------------------- measure --
def measure_one(row: dict) -> dict:
    """Banding, blocking and blur over three samples of one file."""
    ff = _ff()
    dur = float(row.get("duration") or 0)
    marks = [dur * f for f in (0.2, 0.5, 0.8)] if dur > 60 else [max(0.0, dur / 2 - 2)]
    bands, blocks, blurs = [], [], []
    tmp = tempfile.mkdtemp(prefix="nuarr-gs-")
    try:
        for k, t in enumerate(marks):
            log = os.path.join(tmp, f"c{k}.json")
            lp = log.replace("\\", "/").replace(":", "\\:")
            lav = (f"[0:v]select='not(mod(n\\,3))',blockdetect,blurdetect,format=yuv420p10le,split[a][b];"
                   f"[a][b]libvmaf=log_fmt=json:log_path='{lp}':n_threads=3:feature=name=cambi")
            STATE.update(pct=k / len(marks), stage=f"sample {k + 1} of {len(marks)} - banding, blocking, blur")
            pp = subprocess.Popen([ff, "-hide_banner", "-nostdin", "-nostats", "-v", "info", "-threads", "3",
                                   "-ss", f"{t:.2f}", "-t", f"{SAMPLE_S:.1f}", "-i", row["path"],
                                   "-an", "-sn", "-dn", "-lavfi", lav, "-f", "null", "-"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  errors="replace", creationflags=NO_WINDOW | BELOW_NORMAL, startupinfo=hidden_si())
            from . import outside as _out
            _out.set_pid("groups", pp.pid)
            try:
                _, err_ = pp.communicate(timeout=240)
            except subprocess.TimeoutExpired:
                pp.kill()
                _, err_ = pp.communicate()

            class _P:
                stderr = err_
            p = _P()
            m1 = re.search(r"block mean:\s*([\d.]+)", p.stderr or "")
            m2 = re.search(r"blur mean:\s*([\d.]+)", p.stderr or "")
            if m1:
                blocks.append(float(m1.group(1)))
            if m2:
                blurs.append(float(m2.group(1)))
            try:
                bands.append(float(json.load(open(log, encoding="utf-8"))["pooled_metrics"]["cambi"]["mean"]))
            except Exception:                            # noqa: BLE001
                pass
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    if not (bands or blocks or blurs):
        return {"err": "nothing could be measured"}
    return {"banding": round(statistics.mean(bands), 2) if bands else None,
            "banding_max": round(max(bands), 2) if bands else None,
            "block": round(statistics.mean(blocks), 3) if blocks else None,
            "blur": round(statistics.mean(blurs), 3) if blurs else None}


def as_running() -> list:
    """The measurer as a card in the Processing panel while it measures."""
    if not STATE.get("running"):
        return []
    from . import outside as _out
    el = time.time() - float(STATE.get("at") or time.time())
    pct = float(STATE.get("pct") or 0)
    try:
        from .db import label_for
        lbl = label_for(file_id=STATE.get("file_id"))
    except Exception:                                    # noqa: BLE001
        lbl = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", STATE.get("label") or "").split(" [")[0][:90]
    return [{"job_id": "groups-measure", "pool": "groups", "kind": "groups",
             "title": lbl, "file": STATE.get("label") or "",
             "stage": STATE.get("stage") or "measuring", "progress": pct, "elapsed_s": round(el),
             "eta_s": None, "eta_est": True,
             "plan": f"release group scorecard: how {STATE.get('grp') or 'this group'} encodes - "
                     "banding (CAMBI), blocking and blur on three samples",
             "why_pool": "CPU at low priority - one file at a time, only when the disk is free",
             "doing": {"tool": "ffmpeg libvmaf CAMBI + blockdetect + blurdetect", "hw": "CPU",
                       "why": STATE.get("stage") or ""},
             "disk": _out.disk_of(STATE.get("file_id")), "dest_disk": "", "fps": 0.0, "speed": 0.0,
             "paused_for_viewer": False, "paused_for_load": False, "cancelled": False, "by_bytes": False,
             **_out.fields("groups", pct)}]


def plan(limit: int = 50) -> list:
    """The next files to measure: groups with the fewest measurements first,
    at most PER_SHOW per show and PER_GROUP per library."""
    init()
    with cursor() as cur:
        done = {}
        shows = {}
        for r in cur.execute("SELECT library, grp, show FROM group_measure WHERE rev=?", (REV,)):
            k = (r["library"], r["grp"])
            done[k] = done.get(k, 0) + 1
            shows[(r["library"], r["grp"], r["show"])] = shows.get((r["library"], r["grp"], r["show"]), 0) + 1
        measured = {r["file_id"] for r in cur.execute("SELECT file_id FROM group_measure")}
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path, library, release_group grp, duration, height, size, pool_disk FROM files "
            "WHERE state NOT IN ('deleted','duplicate') AND release_group IS NOT NULL AND release_group!='' "
            "AND duration > 120 AND size > 0")]
    # ROUND BY ROUND, BIG GROUPS FIRST WITHIN A ROUND: every group gets its
    # first file before any gets its second, and among groups on the same
    # round the ones with the most files go first - they are most of the
    # library and the ones a decision would touch.
    size: dict = {}
    for r in rows:
        size[(r["library"], r["grp"])] = size.get((r["library"], r["grp"]), 0) + 1
    rows.sort(key=lambda r: r["id"] % 97)            # spread within a group, not oldest-first
    picked: dict = {}
    eligible = []
    for r in rows:
        if r["id"] in measured:
            continue
        k = (r["library"], r["grp"])
        n = done.get(k, 0) + picked.get(k, 0)
        if n >= PER_GROUP:
            continue
        sk = (r["library"], r["grp"], _show(r["path"]))
        if shows.get(sk, 0) >= PER_SHOW:
            continue
        shows[sk] = shows.get(sk, 0) + 1
        picked[k] = picked.get(k, 0) + 1
        r["_round"] = n
        eligible.append(r)
    eligible.sort(key=lambda r: (r["_round"], -size[(r["library"], r["grp"])]))
    out = eligible[:limit]
    return out


def _probe_bits(cur, fid: int) -> tuple:
    r = cur.execute("SELECT json FROM file_probes WHERE file_id=?", (fid,)).fetchone()
    try:
        vs = next(s for s in json.loads(r["json"])["streams"] if s.get("codec_type") == "video")
        return vs.get("codec_name") or "", 1 if "10" in (vs.get("pix_fmt") or "") else 0
    except Exception:                                    # noqa: BLE001
        return "", 0


def _busy_disk(disk: str) -> bool:
    try:
        from . import gate
        if gate.global_hold()[0]:
            return True
        return bool(disk) and disk in (gate._PLEX_DISKS or set())
    except Exception:                                    # noqa: BLE001
        return False


async def step() -> bool:
    """Measure one file. False when there is nothing to do right now."""
    if paused():
        return False
    cand = await asyncio.to_thread(plan, 20)
    cand = [c for c in cand if not _busy_disk(c.get("pool_disk") or "")]
    if not cand:
        return False
    r = cand[0]
    from . import outside as _out
    _out.start("groups", r["id"])
    STATE.update(running=True, label=os.path.basename(r["path"]), at=time.time(), pct=0.0, stage="starting",
                 file_id=r["id"], grp=r["grp"])
    try:
        res = await asyncio.to_thread(measure_one, r) if os.path.exists(r["path"]) else {"err": "not on disk"}
    except Exception as e:                               # noqa: BLE001
        res = {"err": f"{type(e).__name__}: {e}"[:200]}
    with cursor() as cur:
        codec, ten = _probe_bits(cur, r["id"])
        mbps = (r["size"] or 0) * 8 / max(1.0, float(r["duration"] or 1)) / 1e6
        cur.execute("INSERT OR REPLACE INTO group_measure(file_id,library,grp,show,path,height,codec,tenbit,mbps,"
                    "banding,banding_max,block,blur,at,rev,err) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["library"], r["grp"], _show(r["path"]), r["path"], r["height"], codec, ten,
                     round(mbps, 2), res.get("banding"), res.get("banding_max"), res.get("block"), res.get("blur"),
                     time.time(), REV, res.get("err")))
    _out.stop("groups")
    STATE.update(running=False, label="", last=f"{r['grp']}: {os.path.basename(r['path'])[:60]}",
                 done_session=STATE["done_session"] + 1,
                 left=(STATE["left"] - 1) if STATE["left"] else STATE["left"])
    return True


async def watch() -> None:
    from . import schedules
    schedules.register("groupscore", "Release group scorecard", "Integrations", 30,
                       what="Measures banding, blocking and blur on a sample of each release group's files.")
    init()
    await asyncio.sleep(180)
    while True:
        try:
            schedules.beat("groupscore")
            did = await step()
            await asyncio.sleep(3 if did else 600)
        except Exception as e:                           # noqa: BLE001
            joblog.log(f"group scorecard: {type(e).__name__}: {e}", "warn")
            await asyncio.sleep(300)


# -------------------------------------------------------------- scorecard --
def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def _pct_rank(vals: dict, key) -> dict:
    """Each group's place among the others on one measure, 0 = best."""
    items = [(g, v) for g, v in vals.items() if v is not None]
    if len(items) < 2:
        return {g: 0.5 for g, _ in items}
    srt = sorted(v for _, v in items)
    n = len(srt) - 1
    return {g: srt.index(v) / n for g, v in items}


def scorecard(library: str = "") -> dict:
    init()
    with cursor() as cur:
        libs = [r["library"] for r in cur.execute(
            "SELECT DISTINCT library FROM files WHERE library IS NOT NULL ORDER BY library")]
        lib = library or (libs[0] if libs else "")
        files = [dict(r) for r in cur.execute(
            "SELECT id, path, release_group grp, height, size, duration FROM files "
            "WHERE library=? AND state NOT IN ('deleted','duplicate') AND release_group IS NOT NULL "
            "AND release_group!='' AND duration>0", (lib,))]
        meas = [dict(r) for r in cur.execute("SELECT * FROM group_measure WHERE library=? AND rev=? AND err IS NULL",
                                             (lib, REV))]
        strikes = {r["grp"]: int(r["n"] or 0) for r in cur.execute("SELECT grp, n FROM arr_ban_strikes")}
        bans = [dict(r) for r in cur.execute("SELECT kind, value FROM arr_bans WHERE enabled=1")]
        broken = {r["file_id"] for r in cur.execute("SELECT file_id FROM integrity WHERE verdict='corrupt'")}
        pending = cur.execute("SELECT COUNT(*) n FROM group_measure").fetchone()["n"]
    shrink = _shrink_rows()
    grp_of = {f["id"]: f["grp"] for f in files}
    by: dict = {}
    for f in files:
        g = by.setdefault(f["grp"], {"files": 0, "mbps1080": [], "shows": set(), "broken": 0, "shrink": []})
        g["files"] += 1
        g["shows"].add(_show(f["path"]))
        if 760 < (f["height"] or 0) <= 1100:
            g["mbps1080"].append(f["size"] * 8 / f["duration"] / 1e6)
        if f["id"] in broken:
            g["broken"] += 1
    for s in shrink:
        gname = grp_of.get(s["file_id"])
        if gname in by:
            by[gname]["shrink"].append(100.0 * (1 - s["size_after"] / s["size_before"]))
    mby: dict = {}
    for m in meas:
        mby.setdefault(m["grp"], []).append(m)
    rows = []
    for gname, g in by.items():
        ms = mby.get(gname, [])
        rel_bans = sum(1 for b in bans if b["kind"] == "release" and re.search(rf"-{re.escape(gname)}\b", b["value"] or "", re.I))
        grp_ban = any(b["kind"] == "group" and (b["value"] or "").lower() == gname.lower() for b in bans)
        rows.append({
            "grp": gname, "files": g["files"], "shows": len(g["shows"]), "measured": len(ms),
            "banding": _med([m["banding"] for m in ms]), "banding_max": _med([m["banding_max"] for m in ms]),
            "block": _med([m["block"] for m in ms]), "blur": _med([m["blur"] for m in ms]),
            "mbps1080": round(statistics.median(g["mbps1080"]), 1) if g["mbps1080"] else None,
            "hevc": round(100 * sum(1 for m in ms if m["codec"] == "hevc") / len(ms)) if ms else None,
            "tenbit": round(100 * sum(1 for m in ms if m["tenbit"]) / len(ms)) if ms else None,
            "shrink": round(statistics.median(g["shrink"]), 0) if g["shrink"] else None,
            "shrunk": len(g["shrink"]),
            "issues": rel_bans + strikes.get(gname, 0) + g["broken"] + (5 if grp_ban else 0),
            "issue_detail": {"release bans": rel_bans, "strikes": strikes.get(gname, 0),
                             "broken files": g["broken"], "group banned": grp_ban},
        })
    # A SCORE ONLY WHERE THERE IS ENOUGH TO SCORE: three measured files.
    scored = {r["grp"]: r for r in rows if r["measured"] >= 3}
    ranks = [_pct_rank({g: r[k] for g, r in scored.items()}, k) for k in ("banding", "block", "blur")]
    for g, r in scored.items():
        parts = [rk[g] for rk in ranks if g in rk]
        r["score"] = round(100 * (1 - sum(parts) / len(parts))) if parts else None
    for r in rows:
        r.setdefault("score", None)
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), -r["files"]))
    # HEAD TO HEAD: shows with two or more measured groups.
    h2h: dict = {}
    for m in meas:
        h2h.setdefault(m["show"], {}).setdefault(m["grp"], []).append(m)
    heads = []
    for show, gs in h2h.items():
        if len(gs) < 2:
            continue
        heads.append({"show": show, "groups": sorted(
            [{"grp": gname, "n": len(ms), "banding": _med([m["banding"] for m in ms]),
              "block": _med([m["block"] for m in ms]), "blur": _med([m["blur"] for m in ms]),
              "mbps": _med([m["mbps"] for m in ms]),
              "codec": "/".join(sorted({(m["codec"] or "?") + (" 10-bit" if m["tenbit"] else "") for m in ms}))}
             for gname, ms in gs.items()], key=lambda x: (x["banding"] if x["banding"] is not None else 99))})
    heads.sort(key=lambda h: -sum(g["n"] for g in h["groups"]))
    left = _left()
    return {"library": lib, "libraries": libs, "groups": rows, "head_to_head": heads[:60],
            "measured_total": pending, "left": left, "paused": paused(),
            "running": STATE["running"], "now": STATE["label"], "last": STATE["last"],
            "per_group": PER_GROUP, "per_show": PER_SHOW}


def group_detail(library: str, grp: str) -> dict:
    """One group up close: its shows, every measured file, and its issues."""
    init()
    with cursor() as cur:
        files = [dict(r) for r in cur.execute(
            "SELECT id, path, height, size, duration FROM files WHERE library=? AND release_group=? "
            "AND state NOT IN ('deleted','duplicate') AND duration>0", (library, grp))]
        meas = {r["file_id"]: dict(r) for r in cur.execute(
            "SELECT * FROM group_measure WHERE library=? AND grp=? AND rev=? AND err IS NULL", (library, grp, REV))}
        bans = [dict(r) for r in cur.execute("SELECT kind, value, why, added_at FROM arr_bans WHERE enabled=1")]
        strike = cur.execute("SELECT n, why, last FROM arr_ban_strikes WHERE grp=?", (grp,)).fetchone()
        broken = {r["file_id"]: r["detail"] for r in cur.execute("SELECT file_id, detail FROM integrity WHERE verdict='corrupt'")}
    shows: dict = {}
    for f in files:
        sh = shows.setdefault(_show(f["path"]), {"show": _show(f["path"]), "files": 0, "measured": [], "mbps": []})
        sh["files"] += 1
        sh["mbps"].append(f["size"] * 8 / f["duration"] / 1e6)
        if f["id"] in meas:
            m = meas[f["id"]]
            sh["measured"].append({"name": os.path.basename(f["path"]), "banding": m["banding"],
                                   "banding_max": m["banding_max"], "block": m["block"], "blur": m["blur"],
                                   "mbps": m["mbps"], "codec": m["codec"], "tenbit": m["tenbit"]})
    out = []
    for sh in shows.values():
        ms = sh["measured"]
        out.append({"show": sh["show"], "files": sh["files"], "mbps": round(statistics.median(sh["mbps"]), 1),
                    "banding": _med([m["banding"] for m in ms]), "block": _med([m["block"] for m in ms]),
                    "blur": _med([m["blur"] for m in ms]), "measured": ms})
    out.sort(key=lambda s: (-len(s["measured"]), -s["files"]))
    rx = re.compile(rf"-{re.escape(grp)}\b", re.I)
    issues = [{"what": "release banned", "detail": b["value"], "why": b["why"]} for b in bans
              if b["kind"] == "release" and rx.search(b["value"] or "")]
    issues += [{"what": "group banned", "detail": grp, "why": b["why"]} for b in bans
               if b["kind"] == "group" and (b["value"] or "").lower() == grp.lower()]
    if strike:
        issues.append({"what": f"{strike['n']} strike(s)", "detail": "", "why": strike["why"]})
    issues += [{"what": "broken file", "detail": os.path.basename(f["path"]), "why": broken[f["id"]]}
               for f in files if f["id"] in broken]
    return {"library": library, "grp": grp, "files": len(files), "shows": out, "issues": issues}
