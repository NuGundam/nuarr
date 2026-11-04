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
DEFAULTS = {"on": True, "target": 95.0, "samples": 4, "sample_s": 6.0, "floor": 2.0, "banding": 4.0,
            # keep the reference cuts and score the FINISHED file against them
            # before it is committed - see verify_final()
            "verify": True}
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
        if k == "on" or k == "verify":
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
        have = [r["name"] for r in cur.execute("PRAGMA table_info(vmaf_log)")]
        if "cambi" not in have:
            cur.execute("ALTER TABLE vmaf_log ADD COLUMN cambi REAL")
        # THE FINAL FILE'S OWN SCORES, filled in by verify_final() once the
        # real encode exists. NULL until then - and forever, for a file that
        # was searched before this existed or with the check switched off.
        for col in ("final_vmaf REAL", "final_vmaf_min REAL", "final_cambi REAL", "final_at REAL", "final_note TEXT",
                    "final_samples TEXT"):
            if col.split()[0] not in have:
                cur.execute(f"ALTER TABLE vmaf_log ADD COLUMN {col}")


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


# THE SEARCH WAS THE CPU COST OF AN ENCODE, not the encode. Measured on a
# 24 s 1080p cut, per probe: FFV1 reference decoded twice on the CPU (once to
# feed NVENC, once to feed libvmaf) plus the distorted side's software decode
# came to 88 cpu-s, and cutting the FFV1 reference another 32 - against ~15
# cpu-s for the whole real encode. Five probes a file, four files at once.
#
# NVENC's lossless HEVC is bit-exact (PSNR inf against the FFV1 cut, 8- and
# 10-bit, checked) and NVDEC can decode it, which FFV1 it cannot. So the
# reference is cut on the card (1.2 cpu-s instead of 32), the sample encode
# never leaves the card (1.0 instead of 21), and the scorer takes both sides
# off NVDEC (43 instead of 67 - libvmaf itself is the rest, and stays on the
# CPU: this build has no libvmaf_cuda). FFV1 remains the fallback, taken for
# the whole search when any cut fails on the card, since a mixed set of
# references would need two scorer paths.
_CARD = {"ok": True, "why": ""}
_LOSSLESS_NVENC = ["-c:v", "hevc_nvenc", "-preset", "p1", "-tune", "lossless", "-rc", "constqp", "-qp", "0"]
_NVDEC = ["-hwaccel", "cuda"]
_NVDEC_CARD = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]


def _card_wanted(family: str) -> bool:
    return family == "nvenc" and _CARD.get("ok", True)


def _card_failed(why: str) -> None:
    _CARD.update(ok=False, why=(why or "")[:200])
    try:
        from . import joblog
        joblog.log(f"VMAF target: card path failed, references on FFV1 from here - {(why or '')[:160]}", "warn")
    except Exception:                                        # noqa: BLE001
        pass


def _cut_reference(ff, src, t, length, out, card: bool = False) -> bool:
    """The source's frames at this moment, cut ONCE and losslessly (FFV1).

    WHY. Seeking the source twice - once to encode the sample, once to score
    it - does not reliably land on the same first frame: the encoder's seek
    and the scorer's seek resolve differently around B-frames and odd start
    times (JUJUTSU KAISEN starts at 0.007 s, Slime at 0.021 s). One frame of
    drift turns a 96 into a 41 on a moving shot, and the search then believes
    the target is unreachable. Encoding FROM this cut and scoring AGAINST it
    means both sides hold exactly the same frames, numbered from zero.

    `card`: decode on NVDEC, keep the frames there, write NVENC lossless HEVC
    (see _CARD above). False on any failure so the caller can fall back."""
    if card:
        args = ([ff, "-hide_banner", "-nostdin", "-y", "-v", "error"] + _NVDEC_CARD +
                ["-ss", f"{t:.3f}", "-t", f"{length:.3f}", "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn"]
                + _LOSSLESS_NVENC + [out])
        rc, err = _run(args, 300)
        if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return True
        _card_failed(err.strip().splitlines()[-1] if err and err.strip() else f"rc {rc}")
        return False
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


def _cut_reference_burned(ff, src, t, length, out, burn: int, image: bool, ten: bool, card: bool = False) -> bool:
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
    args += ["-ss", f"{t:.3f}", "-t", f"{length:.3f}", "-an", "-sn", "-dn"]
    # The painting needs CPU frames, so the decode stays in software; the
    # lossless WRITE still goes to NVENC when the card path is on, which is
    # the FFV1 encode's share of the cost and makes the reference NVDEC-able.
    rc, err = _run(args + (_LOSSLESS_NVENC if card else ["-c:v", "ffv1", "-level", "3"]) + [out], 300)
    if card and not (rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0):
        _card_failed(err.strip().splitlines()[-1] if err and err.strip() else f"rc {rc}")
        return False
    return rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0


def _encode_sample(ff, ref, family, codec, cq, preset, ten, out, card: bool = False, src10: bool = False) -> int:
    """One sample at one CQ, from the reference cut; its size in bytes (0 on failure).

    `card`: the reference is NVDEC-able and the frames stay on the card into
    NVENC - the same path the real encode takes (jobs.build_ffmpeg), down to
    scale_cuda widening an 8-bit reference for a 10-bit sample."""
    from . import encoders
    args = [ff, "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if card:
        args += _NVDEC_CARD
    args += ["-i", ref, "-map", "0:v:0", "-an", "-sn", "-dn"]
    if card and ten and not src10:
        args += ["-vf", "scale_cuda=format=p010le"]
    args += encoders.video_args(family, codec, int(cq), preset, ten, card)
    args += [out]
    rc, err = _run(args, 300)
    return os.path.getsize(out) if rc == 0 and os.path.exists(out) else 0


def _score(ff, ref, enc, ten, threads, card: bool = False) -> tuple[float, float, float] | None:
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
    # Both sides off NVDEC when the references are HEVC; libvmaf itself
    # wants system frames, so the format= step above is where they come down.
    hw = _NVDEC if card else []
    args = [ff, "-hide_banner", "-nostdin", "-v", "error"] + hw + ["-i", enc] + hw + ["-i", ref,
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
    # reference folders left by a job that never came back for them
    try:
        sweep(os.path.dirname(os.path.normpath(work_dir)))
    except Exception:                                    # noqa: BLE001
        pass
    work_dir = os.path.join(work_dir, _re.sub(r"[^\w-]", "_", str(job_id)) or "job")
    os.makedirs(work_dir, exist_ok=True)
    # HOW FAR ALONG, HONESTLY. The probes a search will need are not known
    # up front: a library with a learned CQ usually settles in 3-4, a cold one
    # takes up to MAX_PROBES. The estimate starts there and grows if the
    # search runs past it, so the bar never sits at 100 % while work remains.
    _vs0 = next((x for x in (probe or {}).get("streams", []) if x.get("codec_type") == "video"), None) or {}
    src10 = "10" in str(_vs0.get("pix_fmt") or "") or str(_vs0.get("bits_per_raw_sample") or "") == "10"
    card = _card_wanted(family)
    _g0 = guess(library, target, family)
    est = {"probes": 4 if _g0 is not None else MAX_PROBES}
    tried: dict[int, dict] = {}
    keep_refs = [False]

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
        ok = (_cut_reference_burned(ff, src, t, length, ref, int(_burn), bool(getattr(plan, "burn_image", False)), ten, card)
              if _burn is not None else _cut_reference(ff, src, t, length, ref, card))
        if not ok and card:
            # ONE SET OF REFERENCES, ONE PATH: start the cuts over on FFV1.
            card = False
            for r0 in refs:
                try:
                    os.remove(r0)
                except OSError:
                    pass
            refs = []
            ok = (_cut_reference_burned(ff, src, t, length, ref, int(_burn), bool(getattr(plan, "burn_image", False)), ten)
                  if _burn is not None else _cut_reference(ff, src, t, length, ref))
            for k0, t_0 in list(enumerate(times))[:k]:       # the ones already cut on the card
                r0 = os.path.join(work_dir, f"ref-{k0}.mkv")
                if (_cut_reference_burned(ff, src, t_0, length, r0, int(_burn), bool(getattr(plan, "burn_image", False)), ten)
                        if _burn is not None else _cut_reference(ff, src, t_0, length, r0)):
                    refs.append(r0)
        if ok:
            refs.append(ref)
    if not refs:
        _sh.rmtree(work_dir, ignore_errors=True)
        return {"cq": None, "note": "could not cut reference samples from the source"}
    # each sample's own banding, measured once (the cut scored against itself)
    src_band = []
    for k, ref in enumerate(refs):
        _report(f"measuring the source's own banding {k + 1}/{len(refs)}", 1.0)
        sc0 = _score(ff, ref, ref, ten, threads, card)
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
                sz = _encode_sample(ff, ref, family, codec, cq, preset, ten, out, card, src10)
                if not sz:
                    raise RuntimeError("sample encode failed")
                sc = _score(ff, ref, out, ten, threads, card)
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
        # KEEP THE REFERENCES FOR THE FINAL FILE. Everything above scored
        # SAMPLE encodes; the real encode is a different ffmpeg run over the
        # whole file. With the cuts kept, verify_final() can put the finished
        # file up against the very same frames and record what it really
        # scored, not what the samples predicted.
        if c.get("verify"):
            try:
                with open(os.path.join(work_dir, "verify.json"), "w", encoding="utf-8") as fh:
                    json.dump({"refs": [os.path.basename(r) for r in refs], "times": times, "length": length,
                               "ten": ten, "card": card, "threads": threads, "src": src,
                               "sample_vmaf": chosen["vmaf"], "sample_worst": chosen["worst"],
                               "sample_cambi": chosen["cambi"]}, fh)
                keep_refs[0] = True
            except OSError:
                pass
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
        if keep_refs[0]:
            # the sample encodes and their logs go; the reference cuts and the
            # manifest stay for verify_final(), which removes the folder
            for name in os.listdir(work_dir):
                if not (name.startswith("ref-") or name == "verify.json"):
                    try:
                        os.remove(os.path.join(work_dir, name))
                    except OSError:
                        pass
        else:
            _sh.rmtree(work_dir, ignore_errors=True)


def _job_dir(cache_dir: str, job_id: str) -> str:
    import re as _re
    return os.path.join(cache_dir, "vmaf", _re.sub(r"[^\w-]", "_", str(job_id)) or "job")


def discard(cache_dir: str, job_id: str) -> None:
    """Drop a job's kept reference cuts - the job ended without a file to check."""
    import shutil as _sh
    d = _job_dir(cache_dir, job_id)
    if os.path.isdir(d):
        _sh.rmtree(d, ignore_errors=True)


def sweep(cache_dir: str, max_age_s: float = 3 * 86400) -> int:
    """Reference folders nobody came back for (a crash between search and
    commit): gone after three days. Returns how many were removed."""
    import shutil as _sh
    base = os.path.join(cache_dir, "vmaf")
    n = 0
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            try:
                if os.path.isdir(p) and time.time() - os.path.getmtime(p) > max_age_s:
                    _sh.rmtree(p, ignore_errors=True)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


# A FINAL SCORE UNDER 90, KEPT TO BE LOOKED AT. Erik, 2026-11-03. 84 of 756 Anime
# Shows conversions scored under 90 with a worst moment near 0, after samples
# that predicted 95+. The originals were gone and the cuts deleted, so it could
# not be told whether the score or the file was wrong. Now such a file keeps its
# reference cuts, its final cuts, libvmaf's per-frame logs and a scores.json, in
# their own folder: sweep() reads only "vmaf", so they outlive the three days.
# ~0.5-1 GB a file, so only the newest KEEP_MAX stay.
KEEP_BELOW = 90.0
KEEP_MAX = 30


def _keep_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, "vmaf-low")


def _keep_cuts(cache_dir: str, job_id: str, work_dir: str, facts: dict) -> str:
    """Move a job's cuts to the kept folder and drop the oldest past KEEP_MAX."""
    import shutil as _sh
    base = _keep_dir(cache_dir)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(work_dir, "scores.json"), "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=1)
    dest = os.path.join(base, os.path.basename(_job_dir(cache_dir, job_id)))
    _sh.rmtree(dest, ignore_errors=True)
    os.replace(work_dir, dest)
    kept = sorted((os.path.join(base, n) for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))),
                  key=os.path.getmtime)
    for old in kept[:-KEEP_MAX]:
        _sh.rmtree(old, ignore_errors=True)
    return dest


def verify_final(job_id: str, final: str, cache_dir: str, tick=None) -> dict | None:
    """Score the FINISHED encode against the reference cuts the search kept.

    The search's VMAF is a prediction: sample encodes against sample cuts. This
    is the measurement: the same moments of the real output file, decoded and
    cut the same way, against the same reference frames. Mean and worst VMAF
    over the samples and the worst banding, written onto the file's vmaf_log
    row as final_*; None when there is nothing kept to check against.

    ALIGNMENT. The reference was cut from the source at t; the final is cut
    from the encode at t. Both seeks are exact, but the encode's first frame
    can sit a frame off the source's (timestamps are rebuilt by the encode),
    and one frame of drift on a moving shot reads as a 40-point loss. Each
    sample is scored at three alignments - as cut, reference one frame on,
    output one frame on - and the best is the true one: a wrong alignment can
    only ever score lower than the right one."""
    import shutil as _sh
    from . import jobs
    init()
    work_dir = _job_dir(cache_dir, job_id)
    man = os.path.join(work_dir, "verify.json")
    if not os.path.exists(man):
        return None
    try:
        m = json.load(open(man, encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        _sh.rmtree(work_dir, ignore_errors=True)
        return None
    ff = jobs._ffmpeg_exe()
    ten, card, length = bool(m["ten"]), bool(m["card"]), float(m["length"])
    threads = max(2, int(m.get("threads") or 4))
    fmt = "yuv420p10le" if ten else "yuv420p"
    means, worsts, bands, per_sample = [], [], [], []
    t0 = time.time()
    try:
        for k, name in enumerate(m["refs"]):
            ref = os.path.join(work_dir, name)
            try:
                idx = int(name[4:-4])                  # ref-{k}.mkv -> the time it was cut at
                t = float(m["times"][idx])
            except (ValueError, IndexError):
                continue
            if not os.path.exists(ref):
                continue
            if tick:
                try:
                    tick(f"checking the final file: sample {k + 1}/{len(m['refs'])}", k / max(1, len(m["refs"])))
                except Exception:                        # noqa: BLE001
                    pass
            cut = os.path.join(work_dir, f"fin-{idx}.mkv")
            if not _cut_reference(ff, final, t, length, cut, card):
                if not (card and _cut_reference(ff, final, t, length, cut, False)):
                    continue
            best = None
            for dskip, rskip in ((0, 0), (0, 1), (1, 0)):
                log = cut + f".{dskip}{rskip}.json"
                lav = (f"[0:v]format={fmt},trim=start_frame={dskip},setpts=PTS-STARTPTS[d];"
                       f"[1:v]format={fmt},trim=start_frame={rskip},setpts=PTS-STARTPTS[r];"
                       f"[d][r]libvmaf=log_fmt=json:log_path='{log.replace(chr(92), '/').replace(':', chr(92) + ':')}'"
                       f":n_threads={threads}:n_subsample={SUBSAMPLE}:feature=name=cambi")
                hw = _NVDEC if card else []
                rc, _err = _run([ff, "-hide_banner", "-nostdin", "-v", "error"] + hw + ["-i", cut] + hw + ["-i", ref,
                                 "-lavfi", lav, "-f", "null", "-"], 600)
                if rc != 0 or not os.path.exists(log):
                    continue
                try:
                    d = json.load(open(log, encoding="utf-8"))
                    frames = [f["metrics"]["vmaf"] for f in d.get("frames") or [] if "vmaf" in f.get("metrics", {})]
                    mean = float(d["pooled_metrics"]["vmaf"]["mean"])
                    low = sorted(frames)[max(0, len(frames) // 20)] if frames else mean
                    cambi = float(((d["pooled_metrics"].get("cambi") or {}).get("mean")) or 0.0)
                except Exception:                        # noqa: BLE001
                    continue
                if best is None or mean > best[0]:
                    best = (mean, float(low), cambi, dskip, rskip)
            if best:
                means.append(best[0]); worsts.append(best[1]); bands.append(best[2])
                per_sample.append({"t": round(t, 3), "vmaf": round(best[0], 2), "worst": round(best[1], 2),
                                   "cambi": round(best[2], 2), "final_skip": best[3], "ref_skip": best[4]})
        if not means:
            res = {"vmaf": None, "worst": None, "cambi": None, "samples": 0, "secs": round(time.time() - t0, 1),
                   "note": "could not score the final file against the kept samples"}
        else:
            res = {"vmaf": round(statistics.mean(means), 2), "worst": round(min(worsts), 2),
                   "cambi": round(max(bands), 2), "samples": len(means), "secs": round(time.time() - t0, 1),
                   "note": f"{len(means)} of {len(m['refs'])} samples" if len(means) < len(m["refs"]) else "all samples",
                   "sample_vmaf": m.get("sample_vmaf"), "sample_worst": m.get("sample_worst"),
                   "sample_cambi": m.get("sample_cambi")}
        low = res["vmaf"] is not None and res["vmaf"] < KEEP_BELOW
        if low:
            try:
                res["kept"] = _keep_cuts(cache_dir, job_id, work_dir, {
                    "job": job_id, "src": m.get("src"), "final": final, "at": time.time(), "vmaf": res["vmaf"],
                    "worst": res["worst"], "cambi": res["cambi"], "sample_vmaf": res.get("sample_vmaf"),
                    "sample_worst": res.get("sample_worst"), "sample_cambi": res.get("sample_cambi"),
                    "length": length, "samples": per_sample})
                res["note"] += "; cuts kept"
            except Exception as e:                       # noqa: BLE001
                res["note"] += f"; could not keep the cuts ({type(e).__name__})"
        try:
            with cursor() as cur:
                cur.execute("UPDATE vmaf_log SET final_vmaf=?, final_vmaf_min=?, final_cambi=?, final_at=?, final_note=?, "
                            "final_samples=? WHERE id=(SELECT MAX(id) FROM vmaf_log WHERE job_id=?)",
                            (res["vmaf"], res["worst"], res["cambi"], time.time(), res["note"],
                             json.dumps(per_sample) if low else None, job_id))
        except Exception:                                # noqa: BLE001
            pass
        return res
    finally:
        _sh.rmtree(work_dir, ignore_errors=True)


# ----------------------------------------------------------------- page ----
_SRC_BAND = re.compile(r"banding [0-9.]+ \(source ([0-9.]+)\)")
_SRC_SIZE = re.compile(r'"src_size": ?([0-9]+)')


def snapshot() -> dict:
    init()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT at,file_id,job_id,path,library,target,family,cq_fixed,cq_chosen,vmaf,vmaf_min,want,saved_pct,"
            "probes,secs,note,cambi,final_vmaf,final_vmaf_min,final_cambi,final_note FROM vmaf_log ORDER BY id DESC LIMIT 100")]
        # BEFORE AND AFTER. The job row has the real file sizes, and the plan
        # text the search wrote has the source's banding ("banding 1.2 (source
        # 0.9)") - so this works for files encoded before this table showed it.
        ids = [r["job_id"] for r in rows if r.get("job_id")]
        jobs_by = {}
        if ids:
            ph = ",".join("?" * len(ids))
            jobs_by = {j["job_id"]: dict(j) for j in cur.execute(
                f"SELECT job_id,state,size_before,size_after,plan_json FROM jobs WHERE job_id IN ({ph})", ids)}
        tot = cur.execute("SELECT COUNT(*) n, AVG(saved_pct) s, AVG(secs) t, AVG(probes) p, "
                          "AVG(cq_chosen - cq_fixed) d FROM vmaf_log").fetchone()
        by = [dict(r) for r in cur.execute(
            "SELECT library, target, family, COUNT(*) n, AVG(cq_chosen) cq, AVG(cq_fixed) fixed, "
            "AVG(saved_pct) saved, AVG(vmaf) vmaf FROM vmaf_log GROUP BY library, target, family ORDER BY n DESC")]
    for r in rows:
        r["name"] = os.path.basename(r.pop("path") or "")
        j = jobs_by.get(r.pop("job_id", None)) or {}
        plan = j.get("plan_json") or ""
        m = _SRC_BAND.search(plan)
        r["src_cambi"] = float(m.group(1)) if m else None
        before = j.get("size_before")
        if not before:
            m = _SRC_SIZE.search(plan)
            before = int(m.group(1)) if m else None
        r["size_before"], r["size_after"], r["job_state"] = before, j.get("size_after"), j.get("state")
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
