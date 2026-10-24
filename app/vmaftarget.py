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
import threading
import time

from . import joblog
from .config import NO_WINDOW, hidden_si
from .db import cursor, kv_get, kv_set

KV = "vmaf.cfg"
KV_LEARN = "vmaf.learn"
DEFAULTS = {"on": True, "target": 95.0, "samples": 4, "sample_s": 6.0, "floor": 2.0, "banding": 4.0}
CQ_DOWN = 4            # at most this many CQ steps BELOW the library's CQ
CQ_UP = 14             # at most this many above it (was 10; burn-heavy anime passed VMAF 96-98 at the old cap of +10)
MIN_MINUTES = 4.0
MAX_PROBES = 6
# THE SEARCH IS CPU WORK, THE ENCODE IS GPU WORK. With four encode slots and a
# search in three of them the CPU sat at 97 % and NVENC at 19-28 %: the card
# waited on the scorer. Two searches at a time keeps the other slots encoding,
# and every 4th frame scored (about 36 a sample) halves each search's cost.
MAX_SEARCHES = 2               # the default; kv vmaf.searches overrides (1-4)
# BANDING IS JUDGED AGAINST THE SOURCE, not against a fixed line. Chicago P.D.
# S14E03 (Amazon WEB-DL, 8-bit) bands on its own - CAMBI 6 to 18 on the source
# samples - so no encode could ever get under 4 and the search gave up at the
# library CQ, while its encodes measured 9.5-12.5: smoother than the source.
# An encode passes when each sample is under the fixed line OR adds no more
# than this much over that sample's own banding.
BAND_MARGIN = 1.0
SUBSAMPLE = 4
_COND = threading.Condition()
_ACTIVE = {"n": 0}


def searches() -> int:
    try:
        return max(1, min(4, int(kv_get("vmaf.searches") or MAX_SEARCHES)))
    except (TypeError, ValueError):
        return MAX_SEARCHES


def set_searches(n: int) -> int:
    kv_set("vmaf.searches", str(max(1, min(4, int(n)))))
    with _COND:
        _COND.notify_all()
    return searches()


def active() -> int:
    return _ACTIVE["n"]


def _acquire(block: bool, timeout: float = 5.0) -> bool:
    with _COND:
        if _ACTIVE["n"] < searches():
            _ACTIVE["n"] += 1
            return True
        if not block:
            return False
        _COND.wait(timeout)
        if _ACTIVE["n"] < searches():
            _ACTIVE["n"] += 1
            return True
        return False


def _release() -> None:
    with _COND:
        _ACTIVE["n"] = max(0, _ACTIVE["n"] - 1)
        _COND.notify_all()
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
        elif k == "banding":
            c[k] = max(1.0, min(20.0, float(v)))
    kv_set(KV, json.dumps(c))
    return c


def init() -> None:
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS vmaf_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, file_id INTEGER, job_id TEXT,
            path TEXT, library TEXT, target TEXT, family TEXT,
            cq_fixed INTEGER, cq_chosen INTEGER, vmaf REAL, vmaf_min REAL, want REAL,
            saved_pct REAL, probes INTEGER, secs REAL, note TEXT)""")
        if "cambi" not in [r["name"] for r in cur.execute("PRAGMA table_info(vmaf_log)")]:
            cur.execute("ALTER TABLE vmaf_log ADD COLUMN cambi REAL")


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


def _cut_reference(ff, src, t, length, out) -> bool:
    """The source's frames at this moment, cut ONCE and losslessly (FFV1).

    WHY. Seeking the source twice - once to encode the sample, once to score
    it - does not reliably land on the same first frame: the encoder's seek
    and the scorer's seek resolve differently around B-frames and odd start
    times (JUJUTSU KAISEN starts at 0.007 s, Slime at 0.021 s). One frame of
    drift turns a 96 into a 41 on a moving shot, and the search then believes
    the target is unreachable. Encoding FROM this cut and scoring AGAINST it
    means both sides hold exactly the same frames, numbered from zero."""
    args = [ff, "-hide_banner", "-nostdin", "-y", "-v", "error", "-ss", f"{t:.3f}", "-t", f"{length:.3f}",
            "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", "ffv1", "-level", "3", out]
    rc, err = _run(args, 300)
    return rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0


BURN_LEAD_S = 15.0


def burn_filter(src: str, burn: int, image: bool, ten: bool) -> tuple[str, str]:
    """(kind, filter) - THE SAME burn the real encode applies (jobs.build_ffmpeg):
    a picture track overlaid (scaled to the video when the config says so),
    a text track rendered by libass. Kept as one definition so the sample and
    the encode cannot drift into burning differently."""
    from . import rules
    if image:
        ov = "overlay=format=yuv420p10" if ten else "overlay"
        fc = (f"[0:s:{burn}][0:v:0]scale2ref[sp][vp];[vp][sp]{ov}[v]"
              if rules.CONFIG.get("scalePgsToVideo", True) else f"[0:v:0][0:s:{burn}]{ov}[v]")
        return "complex", fc
    from .jobs import _filter_path
    vf = f"subtitles={_filter_path(src)}:si={burn}"
    if ten:
        vf += ",format=yuv420p10le"
    return "vf", vf


def _cut_reference_burned(ff, src, t, length, out, burn: int, image: bool, ten: bool) -> bool:
    """The reference with the signs painted on, as the encode will paint them.

    WHY. VMAF scores an encode against its reference. With a burn, the encode
    carries signs the plain source does not, every sign reads as damage, and
    the search believes it needs more bits than it does - so burn jobs were
    left at the library CQ. Painting the same signs on the reference makes
    both sides identical but for compression.

    TIMING. Decoding starts BURN_LEAD_S early so a sign that began before the
    sample is already on screen when it starts (a picture track only shows a
    cue once its packet is decoded), and -copyts keeps the original clock so
    libass draws the lines that belong at this point in the episode rather
    than the first ones in the file. The output -ss then cuts at the true
    moment."""
    lead = min(BURN_LEAD_S, t)
    kind, f = burn_filter(src, burn, image, ten)
    args = [ff, "-hide_banner", "-nostdin", "-y", "-v", "error", "-copyts",
            "-ss", f"{t - lead:.3f}", "-i", src]
    if kind == "complex":
        args += ["-filter_complex", f, "-map", "[v]"]
    else:
        args += ["-map", "0:v:0", "-vf", f]
    args += ["-ss", f"{t:.3f}", "-t", f"{length:.3f}", "-an", "-sn", "-dn",
             "-c:v", "ffv1", "-level", "3", out]
    rc, err = _run(args, 300)
    return rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def _encode_sample(ff, ref, family, codec, cq, preset, ten, out) -> int:
    """One sample at one CQ, from the reference cut; its size in bytes (0 on failure)."""
    from . import encoders
    args = [ff, "-hide_banner", "-nostdin", "-y", "-v", "error",
            "-i", ref, "-map", "0:v:0", "-an", "-sn", "-dn"]
    args += encoders.video_args(family, codec, int(cq), preset, ten, False)
    args += [out]
    rc, err = _run(args, 300)
    return os.path.getsize(out) if rc == 0 and os.path.exists(out) else 0


def _score(ff, ref, enc, ten, threads) -> tuple[float, float, float] | None:
    """(mean VMAF, 5th-percentile-ish VMAF, mean CAMBI) of an encoded sample.

    CAMBI is Netflix's banding index, computed by the same libvmaf call. VMAF
    is known to under-score banding - the flat gradients and dark skies that
    cel animation is made of - and banding is exactly why anime was kept out
    of the shrink in the first place (Oshi no Ko -73% and the worst-looking).
    About 5 is where Netflix finds banding becomes visible."""
    fmt = "yuv420p10le" if ten else "yuv420p"
    log = enc + ".json"
    # Distorted first, reference second - libvmaf's order. The reference is
    # the very cut the sample was encoded from, so frame N meets frame N.
    lav = (f"[0:v]format={fmt},setpts=PTS-STARTPTS[d];"
           f"[1:v]format={fmt},setpts=PTS-STARTPTS[r];"
           f"[d][r]libvmaf=log_fmt=json:log_path='{log.replace(chr(92), '/').replace(':', chr(92) + ':')}'"
           f":n_threads={threads}:n_subsample={SUBSAMPLE}:feature=name=cambi")
    args = [ff, "-hide_banner", "-nostdin", "-v", "error", "-i", enc, "-i", ref,
            "-lavfi", lav, "-f", "null", "-"]
    rc, err = _run(args, 600)
    if rc != 0 or not os.path.exists(log):
        return None
    try:
        d = json.load(open(log, encoding="utf-8"))
        frames = [f["metrics"]["vmaf"] for f in d.get("frames") or [] if "vmaf" in f.get("metrics", {})]
        mean = float(d["pooled_metrics"]["vmaf"]["mean"])
        low = sorted(frames)[max(0, len(frames) // 20)] if frames else mean
        cambi = float(((d["pooled_metrics"].get("cambi") or {}).get("mean")) or 0.0)
        return mean, float(low), cambi
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
    if duration and duration < MIN_MINUTES * 60:
        return f"shorter than {MIN_MINUTES:g} minutes"
    vs = next((s for s in (probe or {}).get("streams", []) if s.get("codec_type") == "video"), None) or {}
    if str(vs.get("color_transfer") or "") in ("smpte2084", "arib-std-b67"):
        return "HDR - VMAF's model is trained on SDR"
    return ""


def choose(job_id: str, file_id: int, src: str, plan, probe: dict | None, duration: float,
           library: str | None, work_dir: str, tick=None) -> dict:
    """Find the CQ for this file - one of MAX_SEARCHES at a time (see above).
    A file that has to wait says so on its card."""
    if not cfg()["on"] or skip_reason(plan, probe, duration):
        return _choose(job_id, file_id, src, plan, probe, duration, library, work_dir, tick)
    if not _acquire(False):
        t0 = time.time()
        STATE[job_id] = {"label": os.path.basename(src), "probe": 0, "cq": None, "at": t0, "waiting": True}
        try:
            while not _acquire(True, 5.0):
                if tick:
                    try:
                        tick(f"waiting for a quality-search slot ({int(time.time() - t0)}s) - "
                             f"{searches()} run at once", 0.0, {})
                    except TypeError:
                        tick("waiting for a quality-search slot", 0.0)
        except BaseException:
            STATE.pop(job_id, None)           # the waiting entry must not outlive the wait
            raise
    try:
        return _choose(job_id, file_id, src, plan, probe, duration, library, work_dir, tick)
    finally:
        _release()


def _choose(job_id: str, file_id: int, src: str, plan, probe: dict | None, duration: float,
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
    want, floor, banding = float(c["target"]), float(c["floor"]), float(c["banding"])
    n, length = int(c["samples"]), float(c["sample_s"])
    times = _sample_times(duration, n, length)
    threads = max(2, (os.cpu_count() or 8) // (searches() + 1))
    # ONE FOLDER PER JOB. The first trial ran three encodes at once, all at
    # CQ 20, and every sample file was named by process, CQ and sample - so
    # the three wrote over each other and none could be scored.
    import re as _re
    import shutil as _sh
    work_dir = os.path.join(work_dir, _re.sub(r"[^\w-]", "_", str(job_id)) or "job")
    os.makedirs(work_dir, exist_ok=True)
    # HOW FAR ALONG, HONESTLY. The probes a search will need are not known
    # up front: a library with a learned CQ usually settles in 3-4, a cold one
    # takes up to MAX_PROBES. The estimate starts there and grows if the
    # search runs past it, so the bar never sits at 100 % while work remains.
    _g0 = guess(library, target, family)
    est = {"probes": 4 if _g0 is not None else MAX_PROBES}
    tried: dict[int, dict] = {}

    def _report(text, done_units):
        if not tick:
            return
        total = 1 + est["probes"]                    # 1 unit for cutting, 1 per probe
        info = {"tried": sorted(({"cq": k2, "vmaf": v["vmaf"], "cambi": v["cambi"], "ok": v["ok"]}
                                 for k2, v in tried.items()), key=lambda x: x["cq"]),
                "want": want, "banding": banding, "probes_est": est["probes"]}
        try:
            tick(text, min(0.99, done_units / total), info)
        except TypeError:
            tick(text, min(0.99, done_units / total))

    refs = []
    for k, t in enumerate(times):
        _report(f"cutting sample {k + 1}/{len(times)} from the source", k / max(1, len(times)))
        ref = os.path.join(work_dir, f"ref-{k}.mkv")
        _burn = getattr(plan, "burn_index", None)
        ok = (_cut_reference_burned(ff, src, t, length, ref, int(_burn), bool(getattr(plan, "burn_image", False)), ten)
              if _burn is not None else _cut_reference(ff, src, t, length, ref))
        if ok:
            refs.append(ref)
    if not refs:
        _sh.rmtree(work_dir, ignore_errors=True)
        return {"cq": None, "note": "could not cut reference samples from the source"}
    # each sample's own banding, measured once (the cut scored against itself)
    src_band = []
    for k, ref in enumerate(refs):
        _report(f"measuring the source's own banding {k + 1}/{len(refs)}", 1.0)
        sc0 = _score(ff, ref, ref, ten, threads)
        src_band.append(float(sc0[2]) if sc0 else 0.0)
    t0 = time.time()
    STATE[job_id] = {"label": os.path.basename(src), "probe": 0, "cq": None, "at": t0}

    def probe_cq(cq: int) -> dict:
        if cq in tried:
            return tried[cq]
        STATE[job_id].update(probe=len(tried) + 1, cq=cq)
        sizes, means, lows, bands = 0, [], [], []
        est["probes"] = max(est["probes"], len(tried) + 1)
        for k, ref in enumerate(refs):
            _report(f"CQ {cq}: sample {k + 1}/{len(refs)}", 1 + len(tried) + k / len(refs))
            out = os.path.join(work_dir, f"enc-{cq}-{k}.mkv")
            try:
                sz = _encode_sample(ff, ref, family, codec, cq, preset, ten, out)
                if not sz:
                    raise RuntimeError("sample encode failed")
                sc = _score(ff, ref, out, ten, threads)
                if not sc:
                    raise RuntimeError("VMAF could not score the sample")
                sizes += sz
                means.append(sc[0])
                lows.append(sc[1])
                bands.append(sc[2])
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass
        r = {"cq": cq, "size": sizes, "vmaf": round(statistics.mean(means), 2), "low": round(min(lows), 2),
             "worst": round(min(means), 2), "cambi": round(max(bands), 2),
             "src_cambi": round(max(src_band), 2) if src_band else None,
             "band_added": round(max((b - sb) for b, sb in zip(bands, src_band)), 2) if src_band else None}
        # PASSES = the samples average the target AND none of them falls more
        # than `floor` below it - one bad scene is what people notice - AND no
        # sample bands visibly.
        band_ok = all(b <= max(banding, sb + BAND_MARGIN)
                      for b, sb in zip(bands, src_band or [0.0] * len(bands)))
        r["ok"] = r["vmaf"] >= want and r["worst"] >= want - floor and band_ok
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
                # OUT OF REACH KEEPS THE LIBRARY'S CQ. Reaching for more bits
                # here made the first trial's samples 63-98 % LARGER than the
                # fixed CQ; a file that cannot score the target at the
                # library's own setting is better left at it than inflated.
                best = fixed
        chosen = tried.get(best) or probe_cq(best)
        # A SCORE THAT CANNOT BE RIGHT IS A BROKEN MEASUREMENT, not a verdict.
        # A modern encoder at the library's CQ does not score under 70 on a
        # clean source; when the library's CQ (or the start point) does, the
        # samples did not line up and nothing above can be trusted.
        _sane = [v for k2, v in tried.items() if k2 <= max(fixed, start)]
        if _sane and min(v["worst"] for v in _sane) < 70:
            raise RuntimeError(f"implausible VMAF ({min(v['worst'] for v in _sane):.0f} at CQ <= {max(fixed, start)})"
                               " - the samples did not line up")
        base = tried.get(fixed)
        if base is None and len(tried) < MAX_PROBES + 1:
            base = probe_cq(fixed)              # the honest comparison: what fixed CQ would have cost
        saved = round(100.0 * (1 - chosen["size"] / base["size"]), 1) if base and base["size"] else None
        secs = round(time.time() - t0, 1)
        note = ("reached the target" if chosen["ok"] else
                f"could not reach {want:g} without banding within CQ {lo}-{hi}")
        _remember(library, target, family, best)
        with cursor() as cur:
            cur.execute("INSERT INTO vmaf_log(at,file_id,job_id,path,library,target,family,cq_fixed,cq_chosen,"
                        "vmaf,vmaf_min,want,saved_pct,probes,secs,note,cambi) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), file_id, job_id, src, library, target, family, fixed, best,
                         chosen["vmaf"], chosen["worst"], want, saved, len(tried), secs, note, chosen["cambi"]))
        return {"cq": best, "fixed": fixed, "vmaf": chosen["vmaf"], "worst": chosen["worst"], "saved": saved,
                "cambi": chosen["cambi"],
                "probes": len(tried), "secs": secs, "note": note,
                "src_cambi": chosen.get("src_cambi"), "band_added": chosen.get("band_added"),
                "tried": sorted(({"cq": k, "vmaf": v["vmaf"], "worst": v["worst"], "cambi": v["cambi"], "ok": v["ok"]}
                                 for k, v in tried.items()), key=lambda x: x["cq"])}
    except Exception as e:                               # noqa: BLE001
        joblog.log(f"VMAF search failed ({type(e).__name__}: {e}) - encoding at the library's CQ {fixed}",
                   "warn", job_id)
        return {"cq": None, "note": f"failed: {type(e).__name__}"}
    finally:
        STATE.pop(job_id, None)
        _sh.rmtree(work_dir, ignore_errors=True)


# ----------------------------------------------------------------- page ----
def snapshot() -> dict:
    init()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT at,file_id,path,library,target,family,cq_fixed,cq_chosen,vmaf,vmaf_min,want,saved_pct,"
            "probes,secs,note,cambi FROM vmaf_log ORDER BY id DESC LIMIT 40")]
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
