r"""Quality-targeted encoding: the CQ each file needs, found by measuring.

ONE CQ FOR EVERY FILE IS RIGHT FOR NONE OF THEM. A fixed CQ 22 spends the same
quantiser on a flat-shaded cartoon, where it is far more than the eye can use,
and on a grainy 35 mm film, where it may be not quite enough. What a person
actually wants held constant is how the result LOOKS, and that is what VMAF
measures: Netflix's model of how a viewer scores a picture against its source,
0-100, where about 95 is "cannot tell them apart on a TV".

So before each encode this cuts a few short samples from across the file,
encodes them with the very settings the real encode will use, scores each
against the source with libvmaf, and searches for the HIGHEST CQ (smallest
file) whose samples still score the target. The real encode then runs at that
CQ. The result is a normal HEVC/H.264 file - nothing about playback changes.

BOUNDED BOTH WAYS. It never goes more than CQ_DOWN below the library's own CQ
(a file that cannot reach the target there gets that, not an unbounded
bitrate), nor more than CQ_UP above it. The library's maxrate cap still
applies on top.

IT LEARNS. The CQ chosen for each library / target / encoder is remembered,
and the median of the last few is the first guess for the next file - so a
library that settles at CQ 27 is usually confirmed in two probes instead of
searched in five. Every choice is logged with its score and the size it saved
against the fixed CQ, measured on the samples.

LEFT ALONE: files with a subtitle burned in (the picture is no longer the
source's, so there is nothing honest to compare against), HDR (VMAF's model
was trained on SDR and reads PQ wrongly), and anything shorter than a few
minutes. Those encode at the library's CQ exactly as before.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import time

from . import joblog
from .config import NO_WINDOW, hidden_si
from .db import cursor, kv_get, kv_set

KV = "vmaf.cfg"
KV_LEARN = "vmaf.learn"
DEFAULTS = {"on": True, "target": 95.0, "samples": 4, "sample_s": 6.0, "floor": 2.0}
CQ_DOWN = 4            # at most this many CQ steps BELOW the library's CQ
CQ_UP = 10             # at most this many above it
MIN_MINUTES = 4.0
MAX_PROBES = 6
LEARN_KEEP = 25

STATE: dict = {}       # job id -> what it is doing, for the page


# ------------------------------------------------------------- settings ----
def cfg() -> dict:
    try:
        d = json.loads(kv_get(KV) or "{}")
    except Exception:                                    # noqa: BLE001
        d = {}
    return {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}


def set_cfg(**kw) -> dict:
    c = cfg()
    for k, v in kw.items():
        if k not in DEFAULTS or v is None:
            continue
        if k == "on":
            c[k] = bool(v)
        elif k == "samples":
            c[k] = max(2, min(8, int(v)))
        elif k == "target":
            c[k] = max(80.0, min(99.0, float(v)))
        elif k == "sample_s":
            c[k] = max(3.0, min(15.0, float(v)))
        elif k == "floor":
            c[k] = max(0.0, min(10.0, float(v)))
    kv_set(KV, json.dumps(c))
    return c


def init() -> None:
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS vmaf_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, file_id INTEGER, job_id TEXT,
            path TEXT, library TEXT, target TEXT, family TEXT,
            cq_fixed INTEGER, cq_chosen INTEGER, vmaf REAL, vmaf_min REAL, want REAL,
            saved_pct REAL, probes INTEGER, secs REAL, note TEXT)""")


def _learn() -> dict:
    try:
        return json.loads(kv_get(KV_LEARN) or "{}") or {}
    except Exception:                                    # noqa: BLE001
        return {}


def _lkey(library: str | None, target: str, family: str) -> str:
    return f"{library or '-'}|{target}|{family}"


def guess(library, target, family) -> int | None:
    xs = _learn().get(_lkey(library, target, family)) or []
    return int(round(statistics.median(xs))) if len(xs) >= 2 else None


def _remember(library, target, family, cq: int) -> None:
    d = _learn()
    k = _lkey(library, target, family)
    d[k] = (d.get(k) or [])[-(LEARN_KEEP - 1):] + [int(cq)]
    kv_set(KV_LEARN, json.dumps(d))


# --------------------------------------------------------------- probes ----
def _run(args: list, timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW, startupinfo=hidden_si())
        return p.returncode, (p.stderr or "") + (p.stdout or "")
    except subprocess.TimeoutExpired:
        return 1, "timed out"


def _sample_times(duration: float, n: int, length: float) -> list[float]:
    # Spread across 10-90 %: the opening and closing credits are the easiest
    # pictures in the file and would flatter every CQ.
    lo, hi = duration * 0.10, duration * 0.90 - length
    if hi <= lo:
        return [max(0.0, duration / 2 - length / 2)]
    return [lo + (hi - lo) * i / max(1, n - 1) for i in range(n)]


def _encode_sample(ff, src, t, length, family, codec, cq, preset, ten, out) -> int:
    """One sample at one CQ; returns its size in bytes (0 on failure)."""
    from . import encoders
    args = [ff, "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.3f}", "-t", f"{length:.3f}",
            "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn"]
    args += encoders.video_args(family, codec, int(cq), preset, ten, False)
    args += [out]
    rc, err = _run(args, 300)
    return os.path.getsize(out) if rc == 0 and os.path.exists(out) else 0


def _score(ff, src, t, length, enc, ten, threads) -> tuple[float, float] | None:
    """(mean, 5th-percentile-ish min) VMAF of an encoded sample against the source."""
    fmt = "yuv420p10le" if ten else "yuv420p"
    log = enc + ".json"
    # Distorted first, reference second - libvmaf's order. Both normalised to
    # the same format and timeline; the source is cut at the same instant the
    # sample was, so frame N meets frame N.
    lav = (f"[0:v]format={fmt},setpts=PTS-STARTPTS[d];"
           f"[1:v]format={fmt},setpts=PTS-STARTPTS[r];"
           f"[d][r]libvmaf=log_fmt=json:log_path='{log.replace(chr(92), '/').replace(':', chr(92) + ':')}'"
           f":n_threads={threads}:n_subsample=2")
    args = [ff, "-hide_banner", "-nostdin", "-v", "error", "-i", enc,
            "-ss", f"{t:.3f}", "-t", f"{length:.3f}", "-i", src,
            "-lavfi", lav, "-f", "null", "-"]
    rc, err = _run(args, 600)
    if rc != 0 or not os.path.exists(log):
        return None
    try:
        d = json.load(open(log, encoding="utf-8"))
        frames = [f["metrics"]["vmaf"] for f in d.get("frames") or [] if "vmaf" in f.get("metrics", {})]
        mean = float(d["pooled_metrics"]["vmaf"]["mean"])
        low = sorted(frames)[max(0, len(frames) // 20)] if frames else mean
        return mean, float(low)
    except Exception:                                    # noqa: BLE001
        return None
    finally:
        try:
            os.remove(log)
        except OSError:
            pass


# --------------------------------------------------------------- choose ----
def skip_reason(plan, probe: dict | None, duration: float) -> str:
    if not getattr(plan, "encode", False):
        return "no encode"
    if getattr(plan, "burn_index", None) is not None:
        return "a subtitle is burned in - nothing honest to compare against"
    if duration and duration < MIN_MINUTES * 60:
        return f"shorter than {MIN_MINUTES:g} minutes"
    vs = next((s for s in (probe or {}).get("streams", []) if s.get("codec_type") == "video"), None) or {}
    if str(vs.get("color_transfer") or "") in ("smpte2084", "arib-std-b67"):
        return "HDR - VMAF's model is trained on SDR"
    return ""


def choose(job_id: str, file_id: int, src: str, plan, probe: dict | None, duration: float,
           library: str | None, work_dir: str, tick=None) -> dict:
    """Find the CQ for this file. Returns {"cq": int|None, ...}; None = use the plan's."""
    c = cfg()
    if not c["on"]:
        return {"cq": None, "note": "off"}
    why = skip_reason(plan, probe, duration)
    if why:
        return {"cq": None, "note": why}
    init()
    from . import jobs, encoders
    venc = dict(getattr(plan, "venc", None) or {})
    family = venc.get("family") or "nvenc"
    target = plan.target or "h264"
    codec = "hevc" if target == "h265" else "h264"
    preset = str(venc.get("preset") or "p5")
    ten = bool(getattr(plan, "ten_bit", False))
    fixed = int(venc.get("cq") or 22)
    lo, hi = fixed - CQ_DOWN, fixed + CQ_UP
    if family == "cpu":
        lo, hi = max(lo, 14), min(hi, 34)
    ff = jobs._ffmpeg_exe()
    want, floor = float(c["target"]), float(c["floor"])
    n, length = int(c["samples"]), float(c["sample_s"])
    times = _sample_times(duration, n, length)
    threads = max(2, (os.cpu_count() or 8) // 4)
    os.makedirs(work_dir, exist_ok=True)
    t0 = time.time()
    tried: dict[int, dict] = {}
    STATE[job_id] = {"label": os.path.basename(src), "probe": 0, "cq": None, "at": t0}

    def probe_cq(cq: int) -> dict:
        if cq in tried:
            return tried[cq]
        STATE[job_id].update(probe=len(tried) + 1, cq=cq)
        sizes, means, lows = 0, [], []
        for k, t in enumerate(times):
            if tick:
                tick(f"VMAF: CQ {cq}, sample {k + 1}/{len(times)}", (len(tried) + k / len(times)) / MAX_PROBES)
            out = os.path.join(work_dir, f"vmaf-{os.getpid()}-{cq}-{k}.mkv")
            try:
                sz = _encode_sample(ff, src, t, length, family, codec, cq, preset, ten, out)
                if not sz:
                    raise RuntimeError("sample encode failed")
                sc = _score(ff, src, t, length, out, ten, threads)
                if not sc:
                    raise RuntimeError("VMAF could not score the sample")
                sizes += sz
                means.append(sc[0])
                lows.append(sc[1])
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass
        r = {"cq": cq, "size": sizes, "vmaf": round(statistics.mean(means), 2), "low": round(min(lows), 2),
             "worst": round(min(means), 2)}
        # PASSES = the samples average the target AND none of them falls more
        # than `floor` below it - one bad scene is what people notice.
        r["ok"] = r["vmaf"] >= want and r["worst"] >= want - floor
        tried[cq] = r
        return r

    try:
        # start where this library usually lands, else at the library's CQ
        g = guess(library, target, family)
        start = min(hi, max(lo, g if g is not None else fixed))
        r = probe_cq(start)
        best = r["cq"] if r["ok"] else None
        if r["ok"]:
            # climb: is a higher CQ still fine? gallop then bisect
            a, step = start, 2
            while a < hi and len(tried) < MAX_PROBES:
                b = min(hi, a + step)
                if probe_cq(b)["ok"]:
                    best, a, step = b, b, step * 2
                else:
                    # bisect between a (ok) and b (not)
                    while b - a > 1 and len(tried) < MAX_PROBES:
                        m = (a + b) // 2
                        if probe_cq(m)["ok"]:
                            best, a = m, m
                        else:
                            b = m
                    break
        else:
            # descend until it passes, or the floor of the range
            a, step = start, 2
            while a > lo and len(tried) < MAX_PROBES:
                b = max(lo, a - step)
                if probe_cq(b)["ok"]:
                    best = b
                    # bisect between b (ok) and a (not)
                    while a - b > 1 and len(tried) < MAX_PROBES:
                        m = (a + b + 1) // 2
                        if probe_cq(m)["ok"]:
                            best, b = m, m
                        else:
                            a = m
                    break
                a, step = b, step * 2
            if best is None:
                best = lo           # the target is out of reach; the most bits the range allows
        chosen = tried.get(best) or probe_cq(best)
        base = tried.get(fixed)
        if base is None and len(tried) < MAX_PROBES + 1:
            base = probe_cq(fixed)              # the honest comparison: what fixed CQ would have cost
        saved = round(100.0 * (1 - chosen["size"] / base["size"]), 1) if base and base["size"] else None
        secs = round(time.time() - t0, 1)
        note = ("reached the target" if chosen["ok"] else f"could not reach {want:g} within CQ {lo}-{hi}")
        _remember(library, target, family, best)
        with cursor() as cur:
            cur.execute("INSERT INTO vmaf_log(at,file_id,job_id,path,library,target,family,cq_fixed,cq_chosen,"
                        "vmaf,vmaf_min,want,saved_pct,probes,secs,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), file_id, job_id, src, library, target, family, fixed, best,
                         chosen["vmaf"], chosen["worst"], want, saved, len(tried), secs, note))
        return {"cq": best, "fixed": fixed, "vmaf": chosen["vmaf"], "worst": chosen["worst"], "saved": saved,
                "probes": len(tried), "secs": secs, "note": note,
                "tried": sorted(({"cq": k, "vmaf": v["vmaf"], "worst": v["worst"], "ok": v["ok"]}
                                 for k, v in tried.items()), key=lambda x: x["cq"])}
    except Exception as e:                               # noqa: BLE001
        joblog.log(f"VMAF search failed ({type(e).__name__}: {e}) - encoding at the library's CQ {fixed}",
                   "warn", job_id)
        return {"cq": None, "note": f"failed: {type(e).__name__}"}
    finally:
        STATE.pop(job_id, None)


# ----------------------------------------------------------------- page ----
def snapshot() -> dict:
    init()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT at,file_id,path,library,target,family,cq_fixed,cq_chosen,vmaf,vmaf_min,want,saved_pct,"
            "probes,secs,note FROM vmaf_log ORDER BY id DESC LIMIT 40")]
        tot = cur.execute("SELECT COUNT(*) n, AVG(saved_pct) s, AVG(secs) t, AVG(probes) p, "
                          "AVG(cq_chosen - cq_fixed) d FROM vmaf_log").fetchone()
        by = [dict(r) for r in cur.execute(
            "SELECT library, target, family, COUNT(*) n, AVG(cq_chosen) cq, AVG(cq_fixed) fixed, "
            "AVG(saved_pct) saved, AVG(vmaf) vmaf FROM vmaf_log GROUP BY library, target, family ORDER BY n DESC")]
    for r in rows:
        r["name"] = os.path.basename(r.pop("path") or "")
    learn = _learn()
    for b in by:
        xs = learn.get(_lkey(b["library"], b["target"], b["family"])) or []
        b["guess"] = int(round(statistics.median(xs))) if len(xs) >= 2 else None
    return {"cfg": cfg(), "recent": rows, "count": int(tot["n"] or 0),
            "avg_saved": round(tot["s"], 1) if tot["s"] is not None else None,
            "avg_secs": round(tot["t"] or 0, 1), "avg_probes": round(tot["p"] or 0, 1),
            "avg_shift": round(tot["d"], 1) if tot["d"] is not None else None,
            "by": by, "running": [dict(v, job=k) for k, v in STATE.items()],
            "bounds": {"down": CQ_DOWN, "up": CQ_UP, "min_minutes": MIN_MINUTES, "max_probes": MAX_PROBES}}
