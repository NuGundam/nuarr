"""What language is this audio track ACTUALLY in - decided by listening to it.

WHY THIS EXISTS. Every other signal nuarr has about audio language is a claim
someone else made: the container tag, the release name, the arr's metadata. The
tag is the one that matters, and it is the one most often absent or wrong. A
missing tag is not neutral - Sonarr, Radarr and most players read a blank as
English - so a Japanese track with no tag presents itself as English and the
language policy can never act on it. It cannot prefer the original language
over a dub when it has been told the original IS the dub.

Inference from metadata got most of the way there and then broke on two cases,
both real, both found in this library:

  * "carries full dialogue subtitles" was used as the signature of a subtitled
    original. A RAW release has no subtitles at all, so a Japanese episode with
    an untagged track and no subs looked like nothing at all. That is exactly
    how S01E12 of "This Monster Wants to Eat Me" got through.

  * the mirror image - "nobody subtitles dialogue in the language it is already
    spoken in" - is simply false. English dubs ship with English subtitles all
    the time. Drunken Master, Rumble in the Bronx and REC each have an English
    dub as the only audio track, and inference wanted to call all three
    Japanese/Chinese/Spanish. That rule was withdrawn after it produced 573
    false positives.

There is no arrangement of metadata that separates those cases, because the
metadata does not contain the answer. The audio does. So this module opens the
audio and listens to it.

HOW. Whisper's language identification head, via faster-whisper on the GPU.
Three 30-second windows are taken from the middle of the file, never the start:
a cold open is often silent, or music, or a production-company sting, and any
of those will identify as whatever the model's prior favours. The three windows
must AGREE before the answer is used. Disagreement means something odd - a
dual-language track, a long musical stretch, a bad sample - and the honest
answer there is "unknown", not a majority vote.

COST. About 1-8 seconds per file, almost all of it the seek and decode rather
than the model. Results are cached in `audio_lang` keyed by (file_id, track)
along with the file's size and mtime, so a re-scan is free and a replaced file
is re-checked automatically.

THIS MODULE NEVER WRITES TO MEDIA. It reports. rules.decide() chooses what to
do with the report, and the user can turn that off per library.
"""

from __future__ import annotations

import glob
import os
import re
import site
import subprocess
import threading
import time

from . import joblog
# NO_WINDOW is CREATE_NO_WINDOW. EVERY child process nuarr spawns must pass it
# or Windows hands it a console of its own - which is what put a flickering
# ffmpeg window on screen for each of the ~1,700 samples a full pass takes.
from .config import NO_WINDOW, SETTINGS
from .db import cursor

# ---------------------------------------------------------------- CUDA setup

def _add_cuda_dirs() -> list[str]:
    r"""Put the pip-installed CUDA runtime DLLs where ctranslate2 will find them.

    ctranslate2 resolves cublas64_12.dll / cudnn by plain name, which searches
    PATH - `os.add_dll_directory` alone is NOT enough and fails with
    "Library cublas64_12.dll is not found or cannot be loaded" at the first
    encode, long after the model reports itself loaded. Both are done here, and
    both must happen BEFORE faster_whisper is imported.
    """
    dirs: list[str] = []
    for s in site.getsitepackages():
        dirs += glob.glob(os.path.join(s, "nvidia", "*", "bin"))
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
    return dirs


# ---------------------------------------------------------------- the model

_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_ERR = ""

MODEL_SIZE = "small"

# WHERE THE MODEL LIVES. Beside nuarr's ffmpeg in ProgramData, not in the
# Administrator account's HuggingFace cache.
#
# It landed there originally because that is faster-whisper's default, and it
# is the wrong place for three reasons: nuarr runs as a scheduled task and its
# managed files belong together; a 927 MB download hidden under a user profile
# is invisible to anyone looking at what nuarr uses; and a backup or a profile
# reset would silently take it away.
from .config import DATA_DIR

MODEL_DIR = DATA_DIR / "whisper"

# The default location, kept only so an existing download can be MOVED rather
# than fetched a second time.
_LEGACY_DIR = os.path.join(
    os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"),
                                              ".cache", "huggingface"),
    "hub")


def _model_folder_name() -> str:
    return f"models--Systran--faster-whisper-{MODEL_SIZE}"


def migrate_model() -> str:
    """Move a previously downloaded model into nuarr's own folder.

    Returns a note for the log, or "" if there was nothing to do. Deliberately
    a MOVE and not a re-download: it is the same bytes, and re-fetching a
    gigabyte to change a directory would be a poor trade.
    """
    import shutil
    src = os.path.join(_LEGACY_DIR, _model_folder_name())
    dst = MODEL_DIR / _model_folder_name()
    if not os.path.isdir(src) or dst.exists():
        return ""
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(src, str(dst))
        return f"moved the Whisper model into {MODEL_DIR}"
    except Exception as e:                               # noqa: BLE001
        # Not fatal - the model simply stays where it is and still loads.
        return f"could not move the Whisper model: {str(e)[:110]}"


def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:                                    # noqa: BLE001
        return ""


def model_cache() -> dict:
    r"""Where the downloaded model lives and how big it is.

    Worth showing because it is the one part of this that arrives over the
    network and sits on disk afterwards - roughly 500 MB for `small` - and
    nothing else in nuarr would tell you it was there.
    """
    # nuarr's own folder first, then the old default - so a machine that has
    # not restarted since the move still reports honestly instead of claiming
    # the model is missing.
    path, size, legacy = "", 0, False
    for root, is_legacy in ((str(MODEL_DIR), False), (_LEGACY_DIR, True)):
        cand = os.path.join(root, _model_folder_name())
        if os.path.isdir(cand):
            path, legacy = cand, is_legacy
            break
    if path:
        # SKIP SYMLINKS. A HuggingFace cache keeps the real weights in blobs/
        # and links them into snapshots/, so following both counted every byte
        # twice - this reported a 486 MB model as 927 MB, which is exactly the
        # kind of number someone would act on.
        for dp, _dn, fn in os.walk(path):
            for f in fn:
                p = os.path.join(dp, f)
                try:
                    if os.path.islink(p):
                        continue
                    size += os.path.getsize(p)
                except OSError:
                    pass
    return {"path": path, "size_mb": round(size / 1024 ** 2, 1) if size else 0,
            "managed": bool(path) and not legacy,
            "wanted_dir": str(MODEL_DIR)}


def paths() -> list[dict]:
    r"""Every file this depends on, and where it actually is.

    For the case where detection has stopped working and the answer is not in
    a log line - a CUDA DLL that did not install, a model directory that was
    cleared, an ffmpeg that moved. Each row is resolved live rather than
    described, because "should be at" is exactly the assumption that breaks.
    """
    import site as _s
    import sys as _sys

    def spec(mod: str) -> str:
        try:
            import importlib.util as _u
            s = _u.find_spec(mod)
            if s and s.origin:
                return os.path.dirname(s.origin)
        except Exception:                                # noqa: BLE001
            pass
        return ""

    mc = model_cache()
    dll_dirs = []
    for sp in _s.getsitepackages():
        dll_dirs += glob.glob(os.path.join(sp, "nvidia", "*", "bin"))
    ff, fp = _ff()
    rows = [
        {"what": "Model", "path": mc["path"] or str(MODEL_DIR),
         "ok": bool(mc["path"]),
         "note": ("in nuarr's folder" if mc.get("managed")
                  else "still in the old user cache - moves on next load"
                  if mc["path"] else "not downloaded yet")},
        {"what": "Model folder nuarr uses", "path": str(MODEL_DIR),
         "ok": MODEL_DIR.exists(), "note": "download_root passed to Whisper"},
        {"what": "faster-whisper", "path": spec("faster_whisper"),
         "ok": bool(spec("faster_whisper")), "note": "the language identifier"},
        {"what": "CTranslate2", "path": spec("ctranslate2"),
         "ok": bool(spec("ctranslate2")), "note": "the inference engine"},
    ]
    for d in dll_dirs:
        name = os.path.basename(os.path.dirname(d))
        rows.append({"what": f"CUDA runtime · {name}", "path": d, "ok": True,
                     "note": "added to PATH before CTranslate2 is imported"})
    if not dll_dirs:
        rows.append({"what": "CUDA runtime", "path": "(none found)", "ok": False,
                     "note": "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"})
    rows += [
        {"what": "ffmpeg", "path": ff, "ok": os.path.exists(ff),
         "note": "decodes the sample windows"},
        {"what": "ffprobe", "path": fp, "ok": os.path.exists(fp),
         "note": "reads duration to place the windows"},
        {"what": "Python", "path": _sys.executable, "ok": True,
         "note": "the interpreter pip installs into"},
    ]
    return rows


# Upstream, for when the answer is not on this machine. Listed rather than
# linked blindly: each one is the project that owns the piece above it.
SOURCES = [
    {"name": "faster-whisper", "what": "the language identifier nuarr calls",
     "url": "https://github.com/SYSTRAN/faster-whisper"},
    {"name": "CTranslate2", "what": "the engine it runs the model on",
     "url": "https://github.com/OpenNMT/CTranslate2"},
    {"name": "the model itself", "what": f"Systran/faster-whisper-{MODEL_SIZE}",
     "url": f"https://huggingface.co/Systran/faster-whisper-{MODEL_SIZE}"},
    {"name": "Whisper", "what": "the original model this is a port of",
     "url": "https://github.com/openai/whisper"},
    {"name": "faster-whisper on PyPI", "what": "what the update check reads",
     "url": "https://pypi.org/project/faster-whisper/"},
]


def info() -> dict:
    """Everything the Whisper settings panel needs, without loading the model."""
    import importlib.util as _u
    have = _u.find_spec("faster_whisper") is not None
    cuda_n = 0
    cuda_err = ""
    try:
        import ctranslate2
        cuda_n = ctranslate2.get_cuda_device_count()
    except Exception as e:                               # noqa: BLE001
        cuda_err = f"{type(e).__name__}: {str(e)[:90]}"
    # The CUDA runtime DLLs are pip packages, separate from the wheel itself,
    # and their absence is the failure that presents as a hang rather than an
    # error - so it is reported explicitly rather than left to be discovered.
    import glob as _g
    import site as _s
    dll_dirs = []
    for sp in _s.getsitepackages():
        dll_dirs += _g.glob(os.path.join(sp, "nvidia", "*", "bin"))
    cublas = any(_g.glob(os.path.join(d, "cublas64_*.dll")) for d in dll_dirs)
    cudnn = any(_g.glob(os.path.join(d, "cudnn*.dll")) for d in dll_dirs)
    return {
        "installed": have,
        "faster_whisper": _pkg_version("faster-whisper"),
        "ctranslate2": _pkg_version("ctranslate2"),
        "cublas": _pkg_version("nvidia-cublas-cu12"),
        "cudnn": _pkg_version("nvidia-cudnn-cu12"),
        "cublas_dll": cublas,
        "cudnn_dll": cudnn,
        "cuda_devices": cuda_n,
        "cuda_error": cuda_err,
        "model": MODEL_SIZE,
        "model_cache": model_cache(),
        "paths": paths(),
        "sources": SOURCES,
        "loaded": _MODEL is not None,
        "device": ("cuda" if cuda_n else "cpu"),
        "min_prob": MIN_PROB,
        "last_error": _MODEL_ERR,
    }


def latest_version(pkg: str = "faster-whisper") -> dict:
    """Ask pip what the newest published release is.

    `pip index versions` is the supported way to ask that question and it goes
    through whatever index the machine is already configured to trust - no
    separate network path, no second set of credentials.
    """
    import sys as _sys
    try:
        r = subprocess.run(
            [_sys.executable, "-m", "pip", "index", "versions", pkg],
            capture_output=True, text=True, timeout=90,
            creationflags=NO_WINDOW)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"LATEST:\s*([0-9][\w.\-]*)", out)
        if not m:
            m = re.search(rf"{re.escape(pkg)}\s*\(([^)]+)\)", out)
        return {"ok": bool(m), "latest": m.group(1) if m else "",
                "detail": "" if m else out.strip()[:200]}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "latest": "", "detail": str(e)[:200]}


def available() -> bool:
    """Can we detect at all? Cheap - does not load the model."""
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def _model():
    """Load once, share across calls.

    A Whisper model is ~500 MB of VRAM and several seconds to load. Loading it
    per file would dominate the cost and, worse, several concurrent loads will
    contend for the GPU alongside nuarr's own NVENC work.
    """
    global _MODEL, _MODEL_ERR
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        _add_cuda_dirs()
        note = migrate_model()
        if note:
            joblog.log(note, "info", system="audiolang")
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        from faster_whisper import WhisperModel
        last = None
        # GPU first, CPU as a fallback. int8 on CPU is slow but still finishes,
        # and a slow answer beats no answer on a machine without a usable CUDA
        # runtime.
        for dev, ct in (("cuda", "float16"), ("cpu", "int8")):
            try:
                _MODEL = WhisperModel(MODEL_SIZE, device=dev, compute_type=ct,
                                      download_root=str(MODEL_DIR))
                _MODEL_ERR = ""
                joblog.log(f"audio language ID ready: {MODEL_SIZE} on {dev}", "info", system="audiolang")
                return _MODEL
            except Exception as e:                      # noqa: BLE001
                last = e
        _MODEL_ERR = f"{type(last).__name__}: {last}"
        raise RuntimeError(_MODEL_ERR)


def unload() -> None:
    """Release the VRAM. The sweep is a one-off; the GPU is for encoding."""
    global _MODEL
    with _MODEL_LOCK:
        _MODEL = None


# ---------------------------------------------------------------- ffmpeg I/O

def _ff() -> tuple[str, str]:
    """THE SAME ffmpeg every other part of nuarr uses.

    This deliberately delegates rather than resolving its own path. An earlier
    version globbed C:\\ProgramData\\nuarr\\ffmpeg\\*\\ffmpeg.exe and picked the
    last match, which happened to be right - and would have quietly stayed
    right until the day someone pinned a build or rolled one back, at which
    point detection would have carried on using a binary nobody had chosen.

    Going through jobs means the pin, the rollback and the "Uses ffmpeg" table
    on the ffmpeg page all apply here too, and there is one answer to "which
    ffmpeg is running" instead of two.
    """
    try:
        from . import jobs
        ff, fp = jobs._ffmpeg_exe(), jobs._ffprobe_exe()
        if ff and fp:
            return ff, fp
    except Exception:                                    # noqa: BLE001
        pass
    ff = getattr(SETTINGS, "ffmpeg", "") or "ffmpeg"
    fp = getattr(SETTINGS, "ffprobe", "") or "ffprobe"
    return ff, fp


def _duration(path: str) -> float:
    ff, fp = _ff()
    try:
        p = subprocess.run(
            [fp, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
            creationflags=NO_WINDOW)
        return float((p.stdout or "0").strip() or 0)
    except Exception:                                    # noqa: BLE001
        return 0.0


def _pcm(path: str, track: int, start: float, secs: int):
    """One mono 16 kHz float32 window, straight out of ffmpeg into memory.

    No temp file: a WAV on disk would add a pool write per sample for no
    benefit, and this runs against a library that is being streamed from.
    `-ss` goes BEFORE `-i` so ffmpeg seeks rather than decoding up to the mark.
    """
    import numpy as np
    ff, _fp = _ff()
    p = subprocess.run(
        [ff, "-nostdin", "-y", "-v", "quiet", "-ss", str(int(start)), "-i", path,
         "-map", f"0:a:{track}", "-t", str(secs), "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"], capture_output=True, timeout=240,
        creationflags=NO_WINDOW)
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


# ---------------------------------------------------------------- detection

# Whisper speaks ISO 639-1. nuarr, ffmpeg and Matroska speak ISO 639-2. This
# covers what actually appears in this library; anything missing falls through
# as unknown rather than being guessed at.
_TO3 = {
    "en": "eng", "ja": "jpn", "ko": "kor", "zh": "chi", "es": "spa",
    "fr": "fre", "de": "ger", "it": "ita", "pt": "por", "ru": "rus",
    "nl": "dut", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "pl": "pol", "tr": "tur", "ar": "ara", "hi": "hin", "th": "tha",
    "vi": "vie", "id": "ind", "he": "heb", "cs": "cze", "hu": "hun",
    "el": "gre", "uk": "ukr", "ro": "ron", "ca": "cat", "tl": "tgl",
}

MIN_PROB = 0.60          # a single window below this is not evidence
SILENCE = 1e-4           # mean |amplitude| under this is silence, not speech

# Offered in the manual override. Ordered by what this library actually
# contains rather than alphabetically, so the likely answer is near the top.
CHOICES = [
    {"code": "jpn", "name": "Japanese"}, {"code": "eng", "name": "English"},
    {"code": "chi", "name": "Chinese"},  {"code": "kor", "name": "Korean"},
    {"code": "spa", "name": "Spanish"},  {"code": "por", "name": "Portuguese"},
    {"code": "fre", "name": "French"},   {"code": "ger", "name": "German"},
    {"code": "ita", "name": "Italian"},  {"code": "rus", "name": "Russian"},
    {"code": "dut", "name": "Dutch"},    {"code": "swe", "name": "Swedish"},
    {"code": "pol", "name": "Polish"},   {"code": "tha", "name": "Thai"},
    {"code": "hin", "name": "Hindi"},    {"code": "ara", "name": "Arabic"},
    {"code": "tur", "name": "Turkish"},  {"code": "vie", "name": "Vietnamese"},
    {"code": "ind", "name": "Indonesian"}, {"code": "cze", "name": "Czech"},
    {"code": "dan", "name": "Danish"},   {"code": "nor", "name": "Norwegian"},
    {"code": "fin", "name": "Finnish"},  {"code": "gre", "name": "Greek"},
    {"code": "hun", "name": "Hungarian"}, {"code": "heb", "name": "Hebrew"},
    {"code": "ukr", "name": "Ukrainian"}, {"code": "ron", "name": "Romanian"},
    {"code": "und", "name": "Undetermined (leave blank)"},
]


def _top(all_probs, n: int = 8) -> list:
    """The n most likely languages for one window, as [code, prob] pairs.

    faster-whisper hands back either a sorted list of pairs or a dict of every
    language it knows, depending on version, so both are accepted rather than
    assumed.
    """
    try:
        items = (list(all_probs.items()) if hasattr(all_probs, "items")
                 else list(all_probs))
        items = [(str(k), float(v)) for k, v in items]
    except Exception:                                    # noqa: BLE001
        return []
    items.sort(key=lambda kv: -kv[1])
    return [[k, round(v, 4)] for k, v in items[:n] if v >= 0.001]


def aggregate(dists: list) -> list:
    """Combine several windows into ONE set of percentages.

    The mean probability per language across windows, not a count of wins.
    Averaging is the honest summary because it keeps the near-misses: three
    windows at ja 0.55 / zh 0.44 average to a genuinely uncertain 55/44, while
    a straight vote would report "3 of 3 chose Japanese" and hide the doubt
    entirely.

    Windows that saw nothing contribute nothing; a language missing from a
    window's top list counts as zero there, which is what pulls down anything
    that only looked good once.
    """
    if not dists:
        return []
    tot: dict[str, float] = {}
    for d in dists:
        for code, p in (d or []):
            tot[code] = tot.get(code, 0.0) + float(p)
    n = len(dists)
    out = [[c, round(v / n, 4)] for c, v in tot.items()]
    out.sort(key=lambda kv: -kv[1])
    return out[:8]


def detect(path: str, track: int = 0, fracs=(0.30, 0.50, 0.70),
           secs: int = 30) -> dict:
    """Listen to `track` of `path` and report what language it is in.

    Returns {code, code2, confidence, votes, ok, why}. `code` is ISO 639-2 or
    "" when the answer is not trustworthy - and "not trustworthy" is a real
    outcome here, not a failure. Everything downstream treats "" as "leave it
    alone", which is the safe direction: a blank tag is already the status quo.
    """
    import numpy as np
    out = {"code": "", "code2": "", "confidence": 0.0, "votes": [],
           "ok": False, "why": "", "overall": [], "windows": 0}
    if not os.path.exists(path):
        out["why"] = "file not found"
        return out
    try:
        m = _model()
    except Exception as e:                               # noqa: BLE001
        out["why"] = f"model unavailable: {e}"
        return out

    dur = _duration(path) or 1400.0
    votes: list[tuple[str, float]] = []
    dists: list[list] = []
    silent = 0
    for fr in fracs:
        try:
            a = _pcm(path, track, dur * fr, secs)
        except Exception as e:                           # noqa: BLE001
            out["why"] = f"extract failed: {str(e)[:80]}"
            continue
        if a.size < 16000:
            continue
        # A silent window identifies as whatever the model's prior likes. It is
        # not a vote, and three silent windows are not a consensus.
        if float(np.abs(a).mean()) < SILENCE:
            silent += 1
            continue
        try:
            lang, prob, all_probs = m.detect_language(a)
        except Exception as e:                           # noqa: BLE001
            out["why"] = f"detect failed: {str(e)[:80]}"
            continue
        votes.append((str(lang), round(float(prob), 3)))
        # KEEP THE RUNNERS-UP. The model scores every language it knows, and
        # discarding all but the winner throws away the only thing that tells
        # a decisive window from a coin flip: "ja 0.97, zh 0.01" and
        # "ja 0.51, zh 0.48" both reduce to a vote for "ja", and only one of
        # them is worth acting on.
        dists.append(_top(all_probs, 8))

    out["votes"] = votes
    out["overall"] = aggregate(dists)
    out["windows"] = len(votes)
    if not votes:
        out["why"] = out["why"] or (f"{silent} silent window(s), no speech found"
                                    if silent else "no usable audio")
        return out

    # A WINDOW THAT IS NOT CONFIDENT IS NOT EVIDENCE, so it does not get a vote.
    #
    # This filter used to run AFTER the agreement check, which let noise veto a
    # near-certain result. Blassreiter S01E05 scored [zh 0.36, ja 0.985,
    # ja 0.994] and was thrown out as "windows disagree" - one junk window
    # outvoting two that were 99% sure. A 0.36 score is the model saying it
    # cannot tell, and "cannot tell" is not a dissenting opinion.
    strong = [v for v in votes if v[1] >= MIN_PROB]
    if not strong:
        best = max(votes, key=lambda v: v[1])
        out.update(code2=best[0], confidence=best[1])
        out["why"] = (f"best guess {best[0]} at {best[1]:.2f}, below the "
                      f"{MIN_PROB:.2f} floor")
        return out

    langs = {v[0] for v in strong}
    if len(langs) > 1:
        # Still deliberately NOT a majority vote. Two CONFIDENT windows that
        # disagree is the signature of a dual-language track, and the right
        # answer there is to stop and let a human look.
        out["why"] = ("windows disagree: "
                      + ", ".join(f"{l} " for l in sorted(langs)).strip()
                      + f" (from {len(strong)} confident window(s))")
        return out

    code2 = strong[0][0]
    conf = round(min(v[1] for v in strong), 3)
    out.update(code2=code2, confidence=conf,
               code=_TO3.get(code2, ""), ok=False)
    if len(strong) < len(votes):
        out["why"] = (f"{len(votes) - len(strong)} window(s) ignored as "
                      f"too uncertain to count; ")
    else:
        out["why"] = ""
    if not out["code"]:
        out["why"] += f"detected {code2}, which has no ISO 639-2 mapping here"
        return out
    out["ok"] = True
    out["why"] += f"{len(strong)} window(s) agreed on {code2} at {conf:.2f}"
    return out


# ---------------------------------------------------------------- the cache

def ensure_table() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audio_lang(
                file_id     INTEGER NOT NULL,
                track       INTEGER NOT NULL,
                code        TEXT    NOT NULL DEFAULT '',
                code2       TEXT    NOT NULL DEFAULT '',
                confidence  REAL    NOT NULL DEFAULT 0,
                ok          INTEGER NOT NULL DEFAULT 0,
                why         TEXT    NOT NULL DEFAULT '',
                votes       TEXT    NOT NULL DEFAULT '',
                overall     TEXT    NOT NULL DEFAULT '',
                size        INTEGER NOT NULL DEFAULT 0,
                mtime       REAL    NOT NULL DEFAULT 0,
                checked_at  REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY(file_id, track))""")
        # Added after the table shipped, so an existing database needs it.
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(audio_lang)")}
        if "overall" not in cols:
            cur.execute("ALTER TABLE audio_lang ADD COLUMN overall TEXT "
                        "NOT NULL DEFAULT ''")


def _stat(path: str) -> tuple[int, float]:
    try:
        st = os.stat(path)
        return int(st.st_size), round(st.st_mtime, 3)
    except OSError:
        return 0, 0.0


def confirm(path: str, track: int, want: str) -> dict:
    r"""Listen HARDER, then say whether `want` is what is actually there.

    The routine pass takes three 30-second windows. This takes five 45-second
    windows spread wider through the file - two and a half times the audio -
    because it is answering a different question. The routine pass asks "is
    this obvious?", and is allowed to answer no. This one is asked when a
    person has already decided, and its job is to tell them whether the file
    agrees before anything is written.

    It never blocks the write. A person who has listened to the episode knows
    something the model does not, and the ten unresolved tracks in this library
    are unresolved precisely because the model could not settle them. The
    result here is EVIDENCE, presented as percentages, not a gate.
    """
    res = detect(path, track, fracs=(0.15, 0.32, 0.50, 0.68, 0.85), secs=45)
    overall = res.get("overall") or []
    pct = {c: p for c, p in overall}
    want2 = _TO2.get(want, "")
    mine = pct.get(want2, 0.0)
    lead = overall[0] if overall else None
    agrees = bool(lead and want2 and lead[0] == want2)
    return {
        "want": want, "want2": want2,
        "share": round(mine, 4),
        "leader": (lead[0] if lead else ""),
        "leader_share": round(lead[1], 4) if lead else 0.0,
        "leader3": _TO3.get(lead[0], "") if lead else "",
        "agrees": agrees,
        "overall": overall,
        "votes": res.get("votes") or [],
        "windows": res.get("windows", 0),
        "detected": res.get("code", ""),
        "why": res.get("why", ""),
    }


# The reverse of _TO3, so a user's ISO 639-2 choice can be compared against
# what the model reports in ISO 639-1.
_TO2 = {v: k for k, v in _TO3.items()}


def cached(file_id: int, track: int, path: str) -> dict | None:
    """A previous answer, but only if the file is still the same file.

    Size and mtime are checked because a replaced file is the whole point:
    Erik re-downloaded S01E12 and the correct answer changed with it. Keying on
    file_id alone would have kept serving the old verdict.
    """
    import json as _json
    size, mtime = _stat(path)
    with cursor() as cur:
        r = cur.execute("SELECT * FROM audio_lang WHERE file_id=? AND track=?",
                        (file_id, track)).fetchone()
    if not r:
        return None
    if int(r["size"]) != size or abs(float(r["mtime"]) - mtime) > 1.0:
        return None
    try:
        votes = _json.loads(r["votes"] or "[]")
    except Exception:                                    # noqa: BLE001
        votes = []
    try:
        overall = _json.loads(r["overall"] or "[]")
    except Exception:                                    # noqa: BLE001
        overall = []
    return {"code": r["code"], "code2": r["code2"],
            "confidence": float(r["confidence"]), "ok": bool(r["ok"]),
            "why": r["why"], "votes": votes, "overall": overall,
            "cached": True}


def store(file_id: int, track: int, path: str, res: dict) -> None:
    import json as _json
    size, mtime = _stat(path)
    ensure_table()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO audio_lang(file_id,track,code,code2,confidence,ok,why,"
            "votes,overall,size,mtime,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id,track) DO UPDATE SET "
            "code=excluded.code, code2=excluded.code2, "
            "confidence=excluded.confidence, ok=excluded.ok, why=excluded.why, "
            "votes=excluded.votes, overall=excluded.overall, "
            "size=excluded.size, mtime=excluded.mtime, "
            "checked_at=excluded.checked_at",
            (file_id, track, res.get("code", ""), res.get("code2", ""),
             float(res.get("confidence") or 0), 1 if res.get("ok") else 0,
             res.get("why", ""), _json.dumps(res.get("votes") or []),
             _json.dumps(res.get("overall") or []),
             size, mtime, time.time()))


def check(file_id: int, path: str, track: int = 0, refresh: bool = False) -> dict:
    """Cached detection. This is the entry point everything else should use."""
    ensure_table()
    if not refresh:
        c = cached(file_id, track, path)
        if c is not None:
            return c
    res = detect(path, track)
    store(file_id, track, path, res)
    res["cached"] = False
    return res


def apply_tags(path: str, tags: dict[int, str]) -> tuple[bool, str]:
    r"""Write language tags onto a file WITHOUT rewriting it.

    A language tag is a handful of bytes in the Matroska header. Getting it
    there via ffmpeg means a stream copy, and a stream copy reads and writes
    the entire file - 0.71 TB across this library to change metadata. That is
    not a rounding error on a pool that is also serving Plex.

    mkvpropedit edits the header in place instead: no re-encode, no re-mux, no
    new file, milliseconds regardless of size. The trade is that it only speaks
    Matroska, so callers must fall back to the normal pipeline for anything
    else - `can_fast_path()` answers that.

    NOTE ON TRACK NUMBERING. `--edit track:aN` is 1-BASED and counts audio
    tracks only, while everything else here (ffmpeg's `0:a:N`, the probe's
    enumerate, plan.audio_lang_tags) is 0-based. The +1 below is the whole
    difference and getting it wrong silently tags the neighbouring track.
    """
    exe = getattr(SETTINGS, "mkvpropedit", "") or r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
    if not os.path.exists(exe):
        return False, "mkvpropedit not installed"
    if not os.path.exists(path):
        return False, "file not found"
    cmd = [exe, path]
    for track, code in sorted(tags.items()):
        cmd += ["--edit", f"track:a{int(track) + 1}", "--set", f"language={code}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           creationflags=NO_WINDOW)
    except Exception as e:                               # noqa: BLE001
        return False, str(e)[:160]
    if r.returncode != 0:
        return False, (r.stdout or r.stderr or "").strip()[:160]
    return True, ", ".join(f"a:{k}={v}" for k, v in sorted(tags.items()))


def apply_and_restamp(file_id: int, path: str, tags: dict[int, str]) -> tuple[bool, str]:
    """apply_tags, then keep the verdicts that justified it valid."""
    ok, why = apply_tags(path, tags)
    if ok:
        restamp(file_id, path)
    return ok, why


_ARR_TOLD: dict[tuple, float] = {}
ARR_DEBOUNCE_S = 120.0


def notify_arrs(file_ids) -> int:
    r"""Tell Sonarr/Radarr the file changed, so they re-read its languages.

    WITHOUT THIS THE WHOLE FEATURE IS INVISIBLE TO THE ARRS. Sonarr caches a
    file's audio languages in its own database and shows them in its UI; a
    header edit changes the file but not that cache, so a track nuarr just
    named Japanese would still read as English in Sonarr - which is the exact
    confusion this feature exists to remove, moved one layer up.

    A RESCAN, not a refresh: the file on disk changed, the show's metadata did
    not. Debounced per parent for the same reason the transcode path is - a
    batch that names 400 tracks across 20 series must not fire 400 rescans.
    """
    ids = [int(i) for i in (file_ids or [])]
    if not ids:
        return 0
    try:
        from .config import SETTINGS
        with cursor() as cur:
            ph = ",".join("?" * len(ids))
            rows = cur.execute(
                f"SELECT DISTINCT arr_name, arr_parent_id FROM files "
                f" WHERE id IN ({ph}) AND arr_name IS NOT NULL "
                f"   AND arr_parent_id IS NOT NULL", tuple(ids)).fetchall()
    except Exception:                                    # noqa: BLE001
        return 0

    # QUEUE A RENAME TOO. Sonarr and Radarr naming formats can include the
    # audio languages - "[JA]" on this library - so a file that was imported
    # while its track read as English may now be named wrongly. The rename
    # queue already knows how to do this safely: it waits for the rescan, backs
    # off, retries, and refuses to act on a file the arr cannot see. Doing it
    # here would be a second, worse copy of that.
    #
    # Queued per FILE, not per parent, and it is idempotent - a file already
    # pending keeps its existing back-off.
    try:
        from . import renamequeue
        with cursor() as cur:
            ph = ",".join("?" * len(ids))
            frows = cur.execute(
                f"SELECT id, arr_name, arr_parent_id, path FROM files "
                f" WHERE id IN ({ph}) AND arr_name IS NOT NULL "
                f"   AND arr_parent_id IS NOT NULL", tuple(ids)).fetchall()
        for f in frows:
            renamequeue.enqueue(f["id"], f["arr_name"], f["arr_parent_id"],
                                f["path"], why="audio language tag corrected")
    except Exception as e:                               # noqa: BLE001
        joblog.log(f"could not queue rename after tagging: {str(e)[:90]}", "warn", system="audiolang")

    now = time.time()
    todo = []
    for r in rows:
        key = (r["arr_name"], r["arr_parent_id"])
        if now - _ARR_TOLD.get(key, 0.0) < ARR_DEBOUNCE_S:
            continue
        _ARR_TOLD[key] = now
        todo.append(key)
    if not todo:
        return 0

    async def _go() -> int:
        from .arr import ArrClient
        n = 0
        for name, pid in todo:
            cfg = next((c for c in SETTINGS.arrs if c.name == name), None)
            if not cfg:
                continue
            client = ArrClient(cfg)
            try:
                await client.notify_file_changed(pid)
                n += 1
            except Exception as e:                       # noqa: BLE001
                joblog.log(f"could not tell {name} the file changed: "
                           f"{str(e)[:90]}", "warn", system="audiolang")
            finally:
                try:
                    await client.close()
                except Exception:                        # noqa: BLE001
                    pass
        return n

    try:
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_go())        # plain thread: own loop is fine
        # Already on the event loop - schedule it and do not block the caller.
        asyncio.create_task(_go())
        return len(todo)
    except Exception as e:                               # noqa: BLE001
        joblog.log(f"arr notify failed: {str(e)[:90]}", "warn", system="audiolang")
        return 0


def can_fast_path(path: str) -> bool:
    """Is this a container mkvpropedit can edit in place?"""
    return os.path.splitext(path)[1].lower() in (".mkv", ".mka", ".mks", ".webm")


# --------------------------------------------------------------- the loop

# Live progress, so the sweep is not a black box. A GPU pass over the library
# that reports nothing is indistinguishable from one that has hung.
PROGRESS: dict = {"state": "idle", "done": 0, "total": 0, "current": "",
                  "started_at": 0.0, "finished_at": 0.0, "found": 0,
                  "applied": 0, "refused": 0, "error": ""}


def progress() -> dict:
    return dict(PROGRESS)


def pending(limit: int = 5000) -> list[dict]:
    r"""Tracks with no usable language tag and no current verdict.

    READS THE EXTRACTED COLUMN, not the probe blobs. This used to join
    file_probes and json.loads() all 39,563 of them - the same 188 MB scan that
    was removed from three screens - and it is called by the job gate, which
    polls every few seconds. `audio_langs` says exactly what is needed: one
    short string per file, "-" for a track with no tag.

    The SQL does the filtering, so a library with nothing outstanding costs one
    indexed scan of a small column rather than a gigabyte of JSON.
    """
    out: list[dict] = []
    try:
        ensure_table()
        with cursor() as cur:
            have = {(r["file_id"], r["track"]): r for r in
                    cur.execute("SELECT * FROM audio_lang").fetchall()}
            rows = cur.execute(
                "SELECT id, path, title, season, episode, library, audio_langs "
                "  FROM files "
                " WHERE state!='deleted' AND audio_langs IS NOT NULL "
                "   AND (audio_langs = '-' OR audio_langs LIKE '-,%' "
                "        OR audio_langs LIKE '%,-' OR audio_langs LIKE '%,-,%')"
            ).fetchall()
    except Exception:                                    # noqa: BLE001
        return out
    for r in rows:
        if len(out) >= limit:
            break
        for ai, raw in enumerate((r["audio_langs"] or "").split(",")):
            if raw != "-":
                continue
            prev = have.get((r["id"], ai))
            # Re-check when the verdict predates the file. Skip when a fresh
            # verdict already exists, whatever it said - including a refusal,
            # because re-listening to the same audio gives the same answer and
            # would burn the GPU on a loop.
            if prev is not None and row_fresh(prev, r["path"]):
                continue
            out.append({"file_id": r["id"], "path": r["path"], "track": ai,
                        "title": r["title"] or "", "season": r["season"],
                        "episode": r["episode"], "library": r["library"] or "",
                        "n_audio": len((r["audio_langs"] or "").split(","))})
    return out


_PENDING_CACHE: dict = {"n": 0, "at": 0.0}
_PENDING_TTL = 20.0


def pending_count() -> int:
    """How many tracks are waiting, cheaply enough for the gate to ask often.

    The gate polls every few seconds and this is the only thing it needs, so
    the answer is cached briefly. A count that is up to twenty seconds old is
    fine for a queue nobody is waiting on; re-running the scan per poll is not.
    """
    now = time.time()
    if now - _PENDING_CACHE["at"] < _PENDING_TTL:
        return int(_PENDING_CACHE["n"])
    try:
        n = len(pending(limit=100000))
    except Exception:                                    # noqa: BLE001
        n = 0
    _PENDING_CACHE.update(n=n, at=now)
    return n


def pending_invalidate() -> None:
    """Drop the cached count - after a pass, or after a tag is written."""
    _PENDING_CACHE["at"] = 0.0


async def watch() -> None:
    """Listen to anything new that arrives with an untagged audio track.

    WHY A LOOP AND NOT A STEP IN THE PIPELINE. Detection wants the GPU, and so
    does NVENC. Hanging it off the probe path would put a multi-second CUDA
    call in front of every import at exactly the moment the encoder is busiest.
    On a timer it can be skipped, deferred and reported on, and a slow answer
    costs nothing because the tag is not needed until the file is next planned.
    """
    import asyncio

    from . import joblog, schedules
    schedules.register("audiolang", "Audio language", "Library", 1800,
                       what="Listens to any audio track that arrived without a "
                            "language tag and records what it actually is. "
                            "A blank tag is read as English by Sonarr, Radarr "
                            "and every player, so it is not a harmless gap.")
    await asyncio.sleep(120)              # let the first scan and probes settle
    while True:
        try:
            schedules.beat("audiolang")
            if available():
                await asyncio.to_thread(run_once)
        except Exception as e:                           # noqa: BLE001
            PROGRESS.update(state="error", error=f"{type(e).__name__}: {e}")
            joblog.log(f"audio language: {type(e).__name__}: {e}", "warn", system="audiolang")
        await asyncio.sleep(1800)


def run_once(limit: int = 400, apply: bool = True) -> dict:
    """One pass: find untagged tracks, listen, and write what was heard."""
    from . import joblog
    pending_invalidate()
    todo = pending(limit)
    PROGRESS.update(state="scanning", done=0, total=len(todo), current="",
                    started_at=time.time(), finished_at=0.0, found=0,
                    applied=0, refused=0, error="")
    if not todo:
        PROGRESS.update(state="idle", finished_at=time.time())
        return {"checked": 0, "found": 0, "applied": 0}

    joblog.log(f"audio language: {len(todo)} untagged track(s) to listen to",
               "info", system="audiolang")
    by_file: dict[int, dict] = {}
    for i, t in enumerate(todo, 1):
        # The title, not the release name - this string is shown on the Job
        # gate panel and in the progress bar, both of which are one line wide.
        try:
            from .db import pretty_from_filename
            label = pretty_from_filename(t["path"])
        except Exception:                                # noqa: BLE001
            label = os.path.basename(t["path"])
        PROGRESS.update(state="listening", done=i - 1, current=label[:90])
        try:
            d = check(t["file_id"], t["path"], t["track"])
        except Exception as e:                           # noqa: BLE001
            PROGRESS["error"] = str(e)[:120]
            continue
        if d.get("ok") and d.get("code"):
            PROGRESS["found"] += 1
            by_file.setdefault(t["file_id"],
                               {"path": t["path"], "tags": {}})["tags"][t["track"]] = d["code"]
        else:
            PROGRESS["refused"] += 1
        PROGRESS["done"] = i

    applied = 0
    done_ids: list[int] = []
    if apply:
        PROGRESS["state"] = "writing"
        for fid, v in by_file.items():
            if not can_fast_path(v["path"]):
                continue
            ok, _why = apply_and_restamp(fid, v["path"], v["tags"])
            if ok:
                applied += 1
                PROGRESS["applied"] = applied
                _reprobe_quiet(fid, v["path"])
                done_ids.append(fid)
    told = 0
    if done_ids:
        PROGRESS["state"] = "telling the arrs"
        told = notify_arrs(done_ids)
    PROGRESS.update(state="idle", finished_at=time.time(), current="")
    pending_invalidate()
    unload()                       # give the VRAM back; the GPU is for encoding
    if applied:
        joblog.log(f"audio language: named {applied} file(s) by listening"
                   + (f", asked the arrs to rescan {told} title(s)" if told else ""),
                   "ok", system="audiolang")
    return {"checked": len(todo), "found": PROGRESS["found"],
            "applied": applied, "arrs_told": told}


def _reprobe_quiet(file_id: int, path: str) -> None:
    """Refresh the stored probe after a header edit, keeping verdicts valid."""
    _ff, fp = _ff_pair()
    try:
        q = subprocess.run([fp, "-v", "quiet", "-print_format", "json",
                            "-show_streams", "-show_format", path],
                           capture_output=True, text=True, timeout=120,
                           creationflags=NO_WINDOW)
        if q.returncode == 0 and q.stdout:
            import json as _json
            with cursor() as cur:
                cur.execute("UPDATE file_probes SET json=? WHERE file_id=?",
                            (q.stdout, file_id))
                cur.execute("UPDATE files SET size=? WHERE id=?",
                            (os.path.getsize(path), file_id))
            from . import jobs
            jobs.refresh_track_langs(file_id, _json.loads(q.stdout))
        restamp(file_id, path)     # header edit only - tracks did not move
    except Exception:                                    # noqa: BLE001
        pass


def _ff_pair() -> tuple[str, str]:
    return _ff()


def row_fresh(row, path: str) -> bool:
    """Does this stored verdict still describe the file that is there now?

    THIS IS NOT OPTIONAL, and leaving it out of the read paths produced a real
    false alarm. TaleSpin was a 17-track Disney+ release whose track 0 was an
    untagged Chinese dub. nuarr correctly kept only the English track and
    dropped the other sixteen - at which point the surviving track became
    track 0, and the stored "track 0 is Chinese" verdict now pointed at the
    English audio. Seven good files were reported as mislabelled.

    A remux renumbers tracks. Any verdict older than the file it describes is
    not stale in the harmless sense of "a bit out of date", it is attached to
    the wrong track.
    """
    if not row:
        return False
    size, mtime = _stat(path)
    try:
        return (int(row["size"]) == size
                and abs(float(row["mtime"]) - mtime) <= 1.0)
    except Exception:                                    # noqa: BLE001
        return False


def restamp(file_id: int, path: str) -> None:
    """Re-fingerprint stored verdicts after a HEADER-ONLY edit.

    mkvpropedit changes size and mtime, which is exactly the signal `row_fresh`
    uses to spot a rewritten file - so writing a tag would immediately discard
    the verdict that justified writing it, and the whole library would read as
    unverified the moment it was fixed.

    The distinction that makes this safe: a header edit does not add, remove or
    reorder tracks. The audio behind track N is the same audio it was a moment
    ago, so the verdict still describes it. A REMUX is the opposite case and
    must still invalidate - see invalidate().
    """
    size, mtime = _stat(path)
    try:
        ensure_table()
        with cursor() as cur:
            cur.execute("UPDATE audio_lang SET size=?, mtime=? WHERE file_id=?",
                        (size, mtime, file_id))
    except Exception:                                    # noqa: BLE001
        pass


def invalidate(file_id: int) -> None:
    """Drop every verdict for a file. Call this whenever the file is rewritten."""
    try:
        ensure_table()
        with cursor() as cur:
            cur.execute("DELETE FROM audio_lang WHERE file_id=?", (file_id,))
    except Exception:                                    # noqa: BLE001
        pass


def for_file(file_id: int, track: int = 0) -> dict | None:
    """Read-only lookup for the planner - never triggers a detection.

    rules.decide() runs inside the enqueue path and on the settings preview,
    where a multi-second GPU call per file would be unacceptable. The sweep
    populates the table; the planner only reads it.

    A verdict that does not match the file on disk is treated as absent, which
    means the track is simply left alone - the safe direction.
    """
    try:
        ensure_table()
        with cursor() as cur:
            r = cur.execute("SELECT * FROM audio_lang "
                            "WHERE file_id=? AND track=? AND ok=1",
                            (file_id, track)).fetchone()
            if not r or not r["code"]:
                return None
            f = cur.execute("SELECT path FROM files WHERE id=?",
                            (file_id,)).fetchone()
        if not f or not row_fresh(r, f["path"]):
            return None
        return {"code": r["code"], "confidence": float(r["confidence"])}
    except Exception:                                    # noqa: BLE001
        pass
    return None
