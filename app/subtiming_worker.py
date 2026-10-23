r"""nuarr - measure whether a file's text subtitles keep time with its speech.

Run as a child process (python -m app.subtiming_worker --src FILE), so an
hour of decoded audio and the speech detector never sit in the web server.
Prints one JSON object on stdout.

HOW IT MEASURES
---------------
1. The audio is decoded to 16 kHz mono and Silero's voice detector (shipped
   with faster-whisper, CPU only) marks where somebody is speaking.
2. Each text track's spoken cues - not "[door creaks]", not "♪", not an ASS
   sign that is pinned to a spot on the screen - become an on/off signal.
3. The two signals are lined up. A subtitle that keeps time rises and falls
   with the speech; one that is late does the same thing a little later.

WHAT IT LOOKS FOR. Two faults account for nearly every subtitle that is "out
of sync" without being for another cut:
   a FIXED OFFSET  every line late (or early) by the same amount, and
   a DRIFT         in step at the start, further out every minute - the
                   subtitle was timed against a copy running at another frame
                   rate (24 vs 23.976 is 1 part in 1001, 25 vs 24 is 4 %).
So every known rate is tried with every offset over the whole file, the best
is kept, and then the episode is cut into five-minute slices and each slice
is lined up again on its own: BEFORE (as it is) and AFTER (with the
correction). Only a correction that brings every slice into step counts as a
fix; anything else - a different cut, a missing scene, a re-edit - is a
subtitle this cannot straighten and is reported as such.

--check-only measures as the file is (no model search); it is what the
caller runs on the rebuilt file to prove the fix took.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys

import numpy as np

SR = 16000
STEP = 0.02                  # fine grid: 20 ms
COARSE = 0.1                 # whole-file search grid
MAX_OFFSET = 30.0            # whole-file offsets tried, seconds either way
WIN = 300.0                  # slice length
WIN_RANGE = 3.0              # a slice is searched this far either way
MIN_CUES = 80
# A LONG FILE IS LISTENED TO IN SAMPLES. Decoding the audio means reading the
# whole container, video and all - a 4K remux is 60 GB off a pool disk. Past
# FULL_UP_TO the file is heard at SAMPLES places of SAMPLE_S each, spread end
# to end: plenty to see an offset, and a drift shows across them just the same.
FULL_UP_TO = 1800.0
SAMPLES = 8
SAMPLE_S = 240.0
WINDOWS: list = []           # (start, end) seconds that were heard / are judged
MASK = np.zeros(0, np.float32)
TEXT = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
# (factor, name): subtitle time x factor = the right time
RATES = [(1.0, ""),
         (1000 / 1001, "timed for 24 fps, video 23.976"),
         (1001 / 1000, "timed for 23.976 fps, video 24"),
         (24 / 25, "timed for 25 fps (PAL), video 24"),
         (25 / 24, "timed for 24 fps, video 25 (PAL)"),
         (24000 / 1001 / 25, "timed for 25 fps (PAL), video 23.976"),
         (25 / (24000 / 1001), "timed for 23.976 fps, video 25 (PAL)")]
SIGN_STYLE = re.compile(r"sign|song|karaoke|kara|\bop\b|\bed\b|opening|ending|title|note|insert|logo|caption|eyecatch|preview", re.I)
SOUND_ONLY = re.compile(r"^\s*([\[\(].*[\]\)]|[♪♫#].*|.*[♪♫])\s*$", re.S)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _ff() -> tuple[str, str]:
    d = r"C:\ProgramData\nuarr\ffmpeg\bin"
    ff, fp = os.path.join(d, "ffmpeg.exe"), os.path.join(d, "ffprobe.exe")
    return (ff if os.path.exists(ff) else "ffmpeg"), (fp if os.path.exists(fp) else "ffprobe")


def _run(args, text=True, timeout=1800):
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                          **({"text": True, "encoding": "utf-8", "errors": "replace"} if text else {}))


def probe(src: str) -> dict:
    _, fp = _ff()
    r = _run([fp, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", src], timeout=180)
    return json.loads(r.stdout or "{}")


def plan_windows(dur: float) -> tuple[list, bool]:
    """(windows, sampled). Short files: every five minutes, heard whole.
    Long files: SAMPLES pieces spread from end to end."""
    if dur <= FULL_UP_TO:
        w, t = [], 0.0
        while t < dur - 60:
            w.append((t, min(dur, t + WIN)))
            t += WIN
        return w, False
    gap = dur / SAMPLES
    return [(max(0.0, gap * (i + 0.5) - SAMPLE_S / 2), min(dur, gap * (i + 0.5) + SAMPLE_S / 2))
            for i in range(SAMPLES)], True


def _ts(h, m, s, f) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(f) / (1000 if len(f) == 3 else 100)


def _parse(txt: str, codec: str, t0: float = 0.0) -> list:
    """(start, end) of every line that is somebody speaking, t0 added."""
    out = []
    if codec in ("ass", "ssa"):
        fmt = None
        for line in txt.splitlines():
            if line.startswith("Format:") and fmt is None and "Start" in line and "Text" in line:
                fmt = [x.strip().lower() for x in line[7:].split(",")]
            if not line.startswith("Dialogue:") or not fmt:
                continue
            parts = line[9:].split(",", len(fmt) - 1)
            if len(parts) < len(fmt):
                continue
            row = dict(zip(fmt, parts))
            body = row.get("text", "")
            if SIGN_STYLE.search(row.get("style", "")) or re.search(r"\\(pos|move|p[1-9])", body):
                continue
            plain = re.sub(r"\{[^}]*\}", "", body).replace("\\N", " ").strip()
            if not plain or SOUND_ONLY.match(plain):
                continue
            m1 = re.match(r"(\d+):(\d+):(\d+)\.(\d+)", row.get("start", "").strip())
            m2 = re.match(r"(\d+):(\d+):(\d+)\.(\d+)", row.get("end", "").strip())
            if m1 and m2:
                out.append((t0 + _ts(*m1.groups()), t0 + _ts(*m2.groups())))
    else:
        for blk in re.split(r"\n\s*\n", txt.replace("\r", "")):
            m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)", blk)
            if not m:
                continue
            body = re.sub(r"<[^>]+>|\{[^}]*\}", "", blk[m.end():]).strip()
            # "[DOOR CREAKS]" and "♪ music ♪" are not speech; "JOHN: hi" is
            body = re.sub(r"^[\[\(][^\]\)]*[\]\)]\s*", "", body)
            if not body or SOUND_ONLY.match(body):
                continue
            g = m.groups()
            out.append((t0 + _ts(*g[:4]), t0 + _ts(*g[4:])))
    return [(a, b) for a, b in out if b > a]


def cue_pieces(dur: float, sampled: bool, ks=None) -> list:
    """What to read of the subtitles. Whole files: all of it, once. Sampled:
    the stretches that can land in a heard window under any correction tried
    - each window, mapped back through each distinct rate, 35 s either side
    for the offset - merged where they overlap."""
    if not sampled:
        return [(0.0, dur)]
    ks = sorted(ks or {round(k, 2) for k, _ in RATES})
    iv = sorted((max(0.0, a / k - (MAX_OFFSET + 5)), min(dur, b / k + MAX_OFFSET + 5))
                for a, b in WINDOWS for k in ks)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _vad_file(fp: str, t0: float, sp: np.ndarray) -> None:
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    opts = VadOptions(min_silence_duration_ms=200, speech_pad_ms=30)
    n = len(sp)
    base = t0
    with open(fp, "rb") as f:
        while True:
            buf = f.read(SR * 600 * 2)
            if not buf:
                break
            a = np.frombuffer(buf[: len(buf) // 2 * 2], np.int16).astype(np.float32) / 32768
            for t in get_speech_timestamps(a, opts):
                i, j = int((base + t["start"] / SR) / STEP), int((base + t["end"] / SR) / STEP)
                sp[max(0, i):min(n, j)] = 1
            base += len(a) / SR


def read_all(src: str, dur: float, pieces: list, a_list: list, tracks: list) -> tuple[dict, dict]:
    """ONE READ PER PIECE FOR EVERYTHING. Decoding audio and pulling a
    subtitle out both mean reading the container, video and all; done
    separately a 4K film was read three or four times over. One ffmpeg per
    piece writes every audio language and every text track at once.
    Returns ({audio_ord: speech}, {sub_ord: cues})."""
    import tempfile
    ff, _ = _ff()
    n = int(dur / STEP) + 2
    sps = {i: np.zeros(n, np.float32) for i, _ in a_list}
    got = {t["ord"]: set() for t in tracks}
    with tempfile.TemporaryDirectory(prefix="nuarr_stm_") as d:
        for t0, t1 in pieces:
            # -copyts: a seek lands on the keyframe before t0, and without it a
            # subtitle's clock starts at its first packet after that keyframe -
            # 1.3 s wrong on Blue Skies. With it every time is the file's own;
            # the audio still starts exactly at t0 and -to ends both at t1.
            args = [ff, "-v", "error", "-nostdin", "-y", "-copyts"]
            if t0 > 0:
                args += ["-ss", f"{t0:.3f}"]
            args += ["-i", src]
            L = f"{t1:.3f}"
            for i, _ in a_list:
                args += ["-map", f"0:a:{i}", "-to", L, "-ac", "1", "-ar", str(SR), "-f", "s16le",
                         os.path.join(d, f"a{i}.raw")]
            outs = []
            for t in tracks:
                ass = t["codec"] in ("ass", "ssa")
                fp = os.path.join(d, f"s{t['ord']}.{'ass' if ass else 'srt'}")
                args += ["-map", f"0:s:{t['ord']}", "-to", L, "-c:s", "ass" if ass else "srt",
                         "-f", "ass" if ass else "srt", fp]
                outs.append((t, fp))
            _run(args)
            for i, _ in a_list:
                fp = os.path.join(d, f"a{i}.raw")
                if os.path.exists(fp):
                    _vad_file(fp, t0, sps[i])
                    os.remove(fp)
            for t, fp in outs:
                try:
                    txt = io.open(fp, encoding="utf-8", errors="replace").read()
                    os.remove(fp)
                except OSError:
                    continue
                got[t["ord"]].update((round(a, 3), round(b, 3)) for a, b in _parse(txt, t["codec"], 0.0))
    return sps, {k: sorted(v) for k, v in got.items()}


def cues_all(src: str, tracks: list, pieces: list) -> dict:
    """{ord: cues} for every track, ONE ffmpeg per piece for all of them -
    pulling a subtitle out reads the container end to end, and doing that once
    per track was most of the time on a file with several. A seek (-ss before
    -i) gives timestamps counted from the seek point; t0 is added back."""
    import tempfile
    ff, _ = _ff()
    got = {t["ord"]: set() for t in tracks}
    with tempfile.TemporaryDirectory(prefix="nuarr_stm_") as d:
        for t0, t1 in pieces:
            # -copyts: a seek lands on the keyframe before t0, and without it a
            # subtitle's clock starts at its first packet after that keyframe -
            # 1.3 s wrong on Blue Skies. With it every time is the file's own;
            # the audio still starts exactly at t0 and -to ends both at t1.
            args = [ff, "-v", "error", "-nostdin", "-y", "-copyts"]
            if t0 > 0:
                args += ["-ss", f"{t0:.3f}"]
            args += ["-i", src]
            outs = []
            for t in tracks:
                ass = t["codec"] in ("ass", "ssa")
                fp = os.path.join(d, f"{t['ord']}.{'ass' if ass else 'srt'}")
                args += ["-map", f"0:s:{t['ord']}", "-to", f"{t1:.3f}", "-c:s", "ass" if ass else "srt",
                         "-f", "ass" if ass else "srt", fp]
                outs.append((t, fp))
            _run(args)
            for t, fp in outs:
                try:
                    txt = io.open(fp, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                got[t["ord"]].update((round(a, 3), round(b, 3)) for a, b in _parse(txt, t["codec"], 0.0))
    return {k: sorted(v) for k, v in got.items()}


def _raster(cs, k: float, d: float, n: int, step: float) -> np.ndarray:
    s = np.zeros(n, np.float32)
    for a, b in cs:
        i, j = int((a * k + d) / step), int((b * k + d) / step)
        if j > 0 and i < n:
            s[max(0, i):min(n, j)] = 1
    return s


def _xcorr(sp: np.ndarray, sub: np.ndarray, lag: int):
    """Pearson correlation of speech with the subtitle signal moved by each
    lag in [-lag, lag] bins (positive = the subtitle is LATE by that much). FFT, so
    a whole hour is as cheap as a slice."""
    m = MASK_FOR(len(sp))
    frac = max(1e-6, float(m.mean()))
    x = (sp - sp[m > 0].mean()) * m if m.any() else sp - sp.mean()
    y = sub - sub.mean()
    nx, ny = np.linalg.norm(x), np.linalg.norm(y) * np.sqrt(frac)
    if nx == 0 or ny == 0:
        return np.zeros(2 * lag + 1)
    L = 1 << int(np.ceil(np.log2(len(x) + len(y))))
    c = np.fft.irfft(np.fft.rfft(y, L) * np.conj(np.fft.rfft(x, L)), L)
    # c[k] = sum y[t+k] x[t] -> y shifted by k matches x: subtitle LATE by k
    idx = np.r_[np.arange(-lag, 0) % L, np.arange(0, lag + 1)]
    return c[idx] / (nx * ny)


def MASK_FOR(n: int) -> np.ndarray:
    """The heard mask at this grid's length (the coarse grid is 5x shorter)."""
    if not len(MASK):
        return np.ones(n, np.float32)
    if len(MASK) >= n and len(MASK) < n + 4:
        return MASK[:n]
    f = int(round(len(MASK) / n)) or 1
    m = _down(MASK, f)[:n]
    return np.pad(m, (0, max(0, n - len(m))))


def _r(sp: np.ndarray, sub: np.ndarray) -> float:
    """Pearson correlation over what was heard - the honest score."""
    m = MASK_FOR(len(sp)) > 0
    x, y = sp[m], sub[m]
    x, y = x - x.mean(), y - y.mean()
    d = np.linalg.norm(x) * np.linalg.norm(y)
    return float(np.dot(x, y) / d) if d else 0.0


def _down(sp: np.ndarray, f: int) -> np.ndarray:
    m = len(sp) // f * f
    return sp[:m].reshape(-1, f).mean(axis=1)


def slices(sp: np.ndarray, cs, k: float, d: float, dur: float) -> list:
    """Each slice (five minutes, or one heard sample) lined up on its own: how
    late the subtitle is there (seconds, + = late) and how clearly it lines up."""
    n = len(sp)
    sub = _raster(cs, k, d, n, STEP)
    lag = int(WIN_RANGE / STEP)
    out = []
    for t, t1 in WINDOWS:
        # SCORED INSIDE WHAT WAS READ. A sampled file has no subtitle beyond
        # its pieces, so a shift that reached past the edge would be scored
        # against nothing and could invent a match; the score is taken over
        # the piece less the search range at each end, so every shift tried
        # stays inside lines that were actually read.
        a, b = int(t / STEP) + lag, min(n, int(t1 / STEP)) - lag
        x = sp[a:b]
        cues_in = sum(1 for c0, c1 in cs if a * STEP <= c0 * k + d < b * STEP)
        ln = round((t1 - t) / 60, 1)
        if x.sum() * STEP < min(20, (t1 - t) / 10) or cues_in < 8:
            out.append({"t": round(t / 60, 1), "len": ln, "late": None, "r": 0.0, "cues": cues_in})
            continue
        best, bl = -2.0, 0
        xc = x - x.mean()
        nx = np.linalg.norm(xc)
        rs = {}
        for L in range(-lag, lag + 1):
            if a + L < 0 or b + L > n:
                continue
            y = sub[a + L:b + L]
            yc = y - y.mean()
            ny = np.linalg.norm(yc)
            if ny == 0:
                continue
            r = float(np.dot(xc, yc) / (nx * ny))
            rs[L] = r
            if r > best:
                best, bl = r, L
        # HOW CLEAR THE ANSWER IS: the best match against the best one at
        # least 0.6 s away. Music, a quiet scene, a cue every few seconds -
        # the peak there is no taller than its neighbours and means nothing.
        far = [r for L, r in rs.items() if abs(L - bl) * STEP > 0.6]
        margin = best - max(far) if far else 0.0
        edge = abs(bl) >= lag - 1
        out.append({"t": round(t / 60, 1), "len": ln, "late": round(bl * STEP, 2), "r": round(best, 3),
                    "margin": round(margin, 3), "edge": edge, "cues": cues_in})
    return out


def _sum(sl: list, min_r: float = 0.12) -> dict:
    # a slice counts when its peak is real: a fair match, and either clearly
    # taller than any other shift or pinned at the end of the range (further
    # out than the search reaches - which is itself the answer)
    good = [s for s in sl if s["late"] is not None and s["r"] >= min_r
            and (s.get("margin", 1) >= 0.03 or s.get("edge") or s["r"] >= 0.45)]
    if not good:
        return {"n": 0, "median_abs": None, "within": 0.0}
    ab = np.array([abs(s["late"]) for s in good])
    return {"n": len(good), "median_abs": round(float(np.median(ab)), 2),
            "within": round(float((ab <= 0.3).mean()), 2),
            "first": good[0]["late"], "last": good[-1]["late"],
            "worst": max(good, key=lambda s: abs(s["late"]))["late"],
            "worst_at": (lambda w: w["t"] + w.get("len", 5) / 2)(max(good, key=lambda s: abs(s["late"])))}


def measure_track(sp: np.ndarray, cs: list, dur: float, search: bool) -> dict:
    n = len(sp)
    f = int(round(COARSE / STEP))
    spc = _down(sp, f)
    lagc = int(MAX_OFFSET / COARSE)
    base_r = _r(sp, _raster(cs, 1.0, 0.0, n, STEP))
    out = {"r_before": round(base_r, 3)}
    before = slices(sp, cs, 1.0, 0.0, dur)
    out["before"] = before
    out["before_sum"] = _sum(before)
    if not search:
        return out
    best = (-9.0, 1.0, "", 0.0)
    rates = list(RATES)
    fit = _fit_drift(before)
    if fit is not None:
        rates.append(fit)
    for k, name in rates:
        c = _xcorr(spc, _down(_raster(cs, k, 0.0, n, STEP), f), lagc)
        i = int(np.argmax(c))
        # another rate has to beat "as it is" clearly, not by noise
        if c[i] > best[0] + (0.0 if k == 1.0 else 0.01):
            # lag i-lagc bins: subtitle late by that much -> correct by moving it earlier
            best = (float(c[i]), k, name, -(i - lagc) * COARSE)
    r, k, name, d = best
    # fine offset, 20 ms, a second either way of the coarse answer
    fl = int(1.0 / STEP)
    c = _xcorr(sp, _raster(cs, k, d, n, STEP), fl)
    i = int(np.argmax(c))
    d = round(d - (i - fl) * STEP, 3)
    after = slices(sp, cs, k, d, dur)
    out.update(rate=k, rate_name=name, offset=d, r_after=round(_r(sp, _raster(cs, k, d, n, STEP)), 3),
               after=after, after_sum=_sum(after))
    return out


def _fit_drift(before: list):
    """A straight line through how late each slice is: the subtitle runs at
    a rate of its own - a cut a few frames different, a conversion done with
    the wrong figure. Not a rate in the table, but mkvmerge applies any
    ratio. Only offered when the slope is real and the line fits."""
    good = [s for s in before if s["late"] is not None and s["r"] >= 0.2
            and (s.get("margin", 1) >= 0.03 or s["r"] >= 0.45) and not s.get("edge")]
    if len(good) < 4:
        return None
    t = np.array([s["t"] * 60 + s.get("len", 5) * 30 for s in good])
    y = np.array([s["late"] for s in good])
    w = np.array([s["r"] for s in good])
    A = np.vstack([np.ones_like(t), t]).T * w[:, None]
    try:
        (a, b), *_ = np.linalg.lstsq(A, y * w, rcond=None)
    except Exception:                                            # noqa: BLE001
        return None
    resid = y - (a + b * t)
    if abs(b) < 2e-5 or abs(b) > 0.06 or np.median(np.abs(resid)) > 0.3:
        return None
    k = 1.0 / (1.0 + b)
    if any(abs(k - kk) < 5e-5 for kk, _ in RATES):
        return None
    return (k, f"runs {abs(b) * 100:.3f}% {'slow' if b > 0 else 'fast'} - a fitted drift")


def judge(m: dict) -> tuple[str, str]:
    """ok | offset | drift | messy | unsure, and the sentence that says why."""
    b, a = m["before_sum"], m.get("after_sum") or {}
    k, d = m.get("rate", 1.0), m.get("offset", 0.0)
    heard = sum(1 for x in (m.get("after") or m.get("before") or []) if x["late"] is not None)
    am = a.get("median_abs")
    bm = b.get("median_abs")
    # IN STEP AFTER THE CORRECTION: enough slices judged, the typical slice
    # within 0.2 s (a subtitle's own scatter is that much), three in four
    # within 0.3 s, and clearly better than it was. A perfect 0.0 counts - it
    # was once read as "no data" and two perfect corrections were thrown away.
    fixed = (a.get("n", 0) >= max(2, int(0.6 * heard)) and am is not None and am <= 0.2
             and a.get("within", 0) >= 0.75 and (m.get("r_after") or 0) >= m["r_before"] + 0.01
             and (bm is None or am <= max(0.1, bm * 0.6)))
    # NOTHING LINES UP AS IT IS - which is what a 4 % drift looks like, and
    # also what a track with no real dialogue looks like. A correction that
    # brings every slice into step tells the two apart.
    if not b["n"] and not fixed:
        return "unsure", "the speech and the lines barely line up anywhere - too little talk, or music over it"
    bad_before = not b["n"] or (b["median_abs"] or 0) >= 0.4 or b["within"] < 0.6
    if not bad_before and (k == 1.0 and abs(d) < 0.35):
        return "ok", (f"in step - lines start within {b['median_abs']:.2f}s of the speech"
                      if b["median_abs"] is not None else "in step")
    if fixed and k != 1.0:
        return "drift", ((f"in step at the start, {b.get('worst', 0):+.1f}s out by minute {b.get('worst_at', 0):.0f} - "
                          if b["n"] else "further out every minute - ")
                         + f"{m['rate_name']}; corrected every slice is within {a['median_abs']:.2f}s")
    if fixed and abs(d) >= 0.3:
        return "offset", (f"every line {'late' if d < 0 else 'early'} by {abs(d):.2f}s; "
                          f"moved, every slice is within {a['median_abs']:.2f}s")
    if not bad_before:
        return "ok", "close enough - no correction does better"
    return "messy", ("out of step in places but no single shift or frame-rate correction fixes it "
                     "(another cut of the episode, a missing scene) - left alone")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--tracks", default="", help="subtitle ordinals to measure (default: every text track)")
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    pj = probe(a.src)
    st = pj.get("streams") or []
    dur = float((pj.get("format") or {}).get("duration") or 0)
    auds = [s for s in st if s.get("codec_type") == "audio"]
    subs = [s for s in st if s.get("codec_type") == "subtitle"]
    if not auds or dur < 300:
        print(json.dumps({"ok": False, "why": "no audio" if not auds else "shorter than five minutes"}))
        return 0
    # EVERY LANGUAGE ONCE. A Japanese show's English subtitles are timed to
    # the Japanese voices, not to the English dub - so each distinct audio
    # language is listened to (default first, at most three) and each track
    # is judged against the one it lines up with best.
    a_ord = next((i for i, s in enumerate(auds) if (s.get("disposition") or {}).get("default")), 0)
    order, seen = [a_ord] + [i for i in range(len(auds)) if i != a_ord], set()
    a_list = []
    for i in order:
        lg = ((auds[i].get("tags") or {}).get("language") or "und").lower()
        if lg in seen:
            continue
        seen.add(lg)
        a_list.append((i, lg))
    a_list = a_list[:3]
    want = {int(x) for x in a.tracks.split(",") if x.strip().isdigit()} if a.tracks else None
    tracks = []
    for i, s in enumerate(subs):
        if want is not None and i not in want:
            continue
        codec = (s.get("codec_name") or "").lower()
        t = s.get("tags") or {}
        info = {"ord": i, "codec": codec, "lang": t.get("language") or "und", "title": t.get("title") or "",
                "forced": bool((s.get("disposition") or {}).get("forced"))}
        if codec not in TEXT:
            info.update(verdict="skipped", why="a picture track - not measured")
        tracks.append(info)
    todo = [t for t in tracks if "verdict" not in t]
    if not todo:
        print(json.dumps({"ok": True, "duration": dur, "tracks": tracks}))
        return 0
    global WINDOWS, MASK
    WINDOWS, sampled = plan_windows(dur)
    MASK = np.zeros(int(dur / STEP) + 2, np.float32)
    trim = MAX_OFFSET if sampled else 0.0      # same reason as in slices()
    for t0, t1 in WINDOWS:
        MASK[int((t0 + trim) / STEP):int((t1 - trim) / STEP)] = 1
    for t in todo:
        if t["forced"]:
            t.update(verdict="skipped", why="forced - only a few lines")
    todo = [t for t in todo if "verdict" not in t]
    spd, allc = read_all(a.src, dur, WINDOWS, a_list, todo)
    sps = [(i, lg, spd[i]) for i, lg in a_list]
    sp = sps[0][2]
    min_cues = MIN_CUES if not sampled else 30

    def measure(t):
        cs = allc.get(t["ord"]) or []
        t["cues"] = len(cs)
        if len(cs) < min_cues:
            t.update(verdict="skipped", why=f"only {len(cs)} spoken lines - signs or songs, not dialogue")
            return
        best = None
        for i, lg, s_ in sps:
            m_ = measure_track(s_, cs, dur, not a.check_only)
            score = max(m_["r_before"], m_.get("r_after") or 0)
            if best is None or score > best[0] + 0.02:
                best = (score, m_, i, lg)
        m = best[1]
        t.update(m)
        t["audio_ord"], t["audio_lang"] = best[2], best[3]
        if a.check_only:
            b = m["before_sum"]
            t["verdict"] = ("ok" if b["n"] and b["median_abs"] is not None and b["median_abs"] <= 0.2
                            and b["within"] >= 0.7 else "off")
            t["why"] = f"median {b['median_abs']}s off, {int(b['within'] * 100)}% of slices within 0.3s"
        else:
            t["verdict"], t["why"] = judge(m)

    for t in todo:
        measure(t)
    # PAL, ONLY WHEN IT COULD BE PAL. A 4 % mismatch moves a line minutes by
    # the end of a film - out of reach of the windows already read. Those
    # stretches are read (subtitles only) when a track came out unclear.
    if sampled and not a.check_only:
        again = [t for t in todo if t.get("verdict") in ("messy", "unsure")]
        if again:
            more = cues_all(a.src, again, cue_pieces(dur, True, {0.96, 1.04}))
            for t in again:
                t["cues_extra"] = len(more.get(t["ord"]) or [])
                allc[t["ord"]] = sorted(set(allc.get(t["ord"]) or []) | set(more.get(t["ord"]) or []))
                for k_ in [k_ for k_ in list(t) if k_ not in ("ord", "codec", "lang", "title", "forced")]:
                    t.pop(k_)
                measure(t)
    print(json.dumps({"ok": True, "duration": dur, "speech_min": round(float(sp.sum()) * STEP / 60, 1),
                      "audio": [lg for _, lg in a_list], "sampled": sampled,
                      "heard_min": round(sum(b - a for a, b in WINDOWS) / 60, 1), "tracks": tracks}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
