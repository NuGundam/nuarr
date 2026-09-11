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
import json
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
from .config import NO_WINDOW, SETTINGS, hidden_si
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
_MODEL_DEV = ""

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
    # A RED CROSS MEANS BROKEN, and "the model has not been fetched yet" is
    # not broken - it is the documented state of a fresh install, fetched on
    # first use. Rows like that carry pending=True and draw amber, so the
    # column stops crying wolf and a real missing piece stays visible.
    rows = [
        {"what": "Model", "path": mc["path"] or str(MODEL_DIR),
         "ok": bool(mc["path"]), "pending": not mc["path"],
         "note": ("in nuarr's folder" if mc.get("managed")
                  else "still in the old user cache - moves on next load"
                  if mc["path"] else "downloads on first use (~464 MB) - not an error")},
        {"what": "Model folder nuarr uses", "path": str(MODEL_DIR),
         "ok": MODEL_DIR.exists(), "pending": not MODEL_DIR.exists(),
         "note": ("download_root passed to Whisper" if MODEL_DIR.exists()
                  else "created automatically with the first download")},
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
        # On a machine with no NVIDIA card, missing CUDA wheels are CORRECT,
        # not a fault - a deliberate CPU install must not spend forever with a
        # red cross telling its owner to install a runtime it cannot use.
        if _nvidia_present():
            rows.append({"what": "CUDA runtime", "path": "(none found)",
                         "ok": False,
                         "note": "an NVIDIA GPU is present - install GPU "
                                 "support above to use it"})
        else:
            rows.append({"what": "CUDA runtime", "path": "(not needed)",
                         "ok": True,
                         "note": "no NVIDIA GPU - running on the CPU is the "
                                 "correct configuration here"})
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
        "install": install_status(),
        # The GPU as WINDOWS sees it, not as ctranslate2 sees it - when the
        # package is missing, ctranslate2 cannot answer, and this is what lets
        # the page say "you have an NVIDIA card, the GPU install applies to you"
        # before anything is installed.
        "nvidia_present": _nvidia_present(),
        # CAN THIS MACHINE RUN IT AT ALL. Reported from the cached probe, and
        # only when the probe has actually been run - `None` means "not yet
        # asked", which the page must not present as "no". The installer runs
        # the same check, so a machine that failed it there arrives here
        # already knowing.
        "cpu_ok": _cpu_verdict()[0],
        "cpu_why": _cpu_verdict()[1],
    }


def _cpu_verdict() -> tuple[bool | None, str]:
    """The probe's answer for the CPU path, without triggering a probe.

    Reading only what is cached is deliberate: this is called by the status
    endpoint the page polls, and probing there would launch a process (and a
    model load) on every poll.
    """
    v = _PROBE.get(("cpu", "int8"))
    if v is None:
        return None, ""
    return v[0], v[1]


def probe_now() -> dict:
    """Run the load probe on demand and report it. For the page's button."""
    ok, why = _load_probe("cpu", "int8")
    return {"ok": ok, "why": why}


def _nvidia_present() -> bool:
    """Is there an NVIDIA GPU at all - asked of the driver, not of CUDA."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                           text=True, timeout=10, creationflags=NO_WINDOW,
                startupinfo=hidden_si())
        return r.returncode == 0 and "GPU" in (r.stdout or "")
    except Exception:                                    # noqa: BLE001
        return False


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
            creationflags=NO_WINDOW,
            startupinfo=hidden_si())
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


def usable() -> bool:
    """Installed AND able to run here.

    `available()` answers "is the package present", which was the only
    question worth asking while the only failure mode was "not installed".
    It is not enough on a machine whose CPU cannot execute CTranslate2: the
    package imports perfectly and loading the model kills the process. Pages
    that decide whether to OFFER listening should ask this instead, so a
    machine that can never listen is not shown the machinery for it.
    """
    if not available():
        return False
    v = _PROBE.get(("cpu", "int8"))
    # CTranslate2 is one binary for both devices, so a CPU that cannot run it
    # cannot run it on the GPU either - the CPU verdict settles both.
    return not (v is not None and not v[0])


# ------------------------------------------------------------- installing ---
# INSTALLABLE FROM THE PAGE, not only from the setup wizard. The wizard offers
# Whisper exactly once, at install time, gated on a GPU being present that day.
# Machines change: a GPU gets added later, or the owner decides CPU inference
# (slow but it finishes - the model loader already falls back to int8 on CPU)
# is worth it. Re-running a 200 MB installer to flip one optional package is
# the wrong price, so the page can do it directly.

INSTALL = {"state": "idle", "mode": "", "log": "", "error": "",
           "started_at": 0.0, "finished_at": 0.0}
_INSTALL_LOCK = threading.Lock()

# What each button means, in pip terms. GPU adds the CUDA runtime wheels -
# their absence is the failure that presents as a hang, so they are never
# left to be discovered separately.
_INSTALL_PKGS = {
    "gpu":    ["faster-whisper", "nvidia-cublas-cu12", "nvidia-cudnn-cu12"],
    "cpu":    ["faster-whisper"],
    "update": ["--upgrade", "faster-whisper"],
}


def install_status() -> dict:
    return dict(INSTALL)


def install_start(mode: str) -> dict:
    """Kick off a pip install on a worker thread; the page polls the state."""
    from . import heavy
    if mode not in _INSTALL_PKGS:
        return {"ok": False, "error": f"unknown install mode {mode!r}"}
    # A GREYED BUTTON IS NOT A CHECK. If the probe has already established
    # that loading the model kills this machine, refuse here too - the page
    # can be old, the endpoint can be called directly, and the cost of
    # getting this wrong is the server going down on first use.
    verdict = _PROBE.get(("cpu", "int8"))
    if verdict is not None and not verdict[0]:
        return {"ok": False, "error": verdict[1]}
    # ONE HEAVY OPERATION AT A TIME across all the engines, not just this
    # one. The old check only stopped a SECOND Whisper install; starting
    # this while a PaddleOCR test had a model loaded was allowed, and on a
    # small machine that pair is enough to run it out of memory.
    got, holder = heavy.claim("Whisper install")
    if not got:
        return {"ok": False,
                "error": f"{holder} is running — engine work is done one at "
                         "a time so a small machine is never asked to load "
                         "two models at once. Try again when it finishes."}
    with _INSTALL_LOCK:
        if INSTALL["state"] == "installing":
            heavy.release("Whisper install")
            return {"ok": False, "error": "an install is already running"}
        INSTALL.update(state="installing", mode=mode, log="", error="",
                       started_at=time.time(), finished_at=0.0)
    threading.Thread(target=_install_worker, args=(mode,),
                     name="whisper-install", daemon=True).start()
    return {"ok": True, "mode": mode}


def _install_worker(mode: str) -> None:
    import sys
    from collections import deque
    cmd = [sys.executable, "-m", "pip", "install", "--prefer-binary",
           "--no-warn-script-location"] + _INSTALL_PKGS[mode]
    tail: deque = deque(maxlen=40)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             creationflags=NO_WINDOW,
            startupinfo=hidden_si())
        for line in p.stdout:                      # live tail for the page
            line = line.rstrip()
            if line:
                tail.append(line)
                INSTALL["log"] = "\n".join(tail)
        rc = p.wait()
        if rc != 0:
            raise RuntimeError(f"pip exited with code {rc}")
        # Make the new package importable IN THIS PROCESS - no restart. The
        # site-packages dir was on sys.path all along; only the import-system
        # caches and (for GPU) the DLL search path need refreshing.
        import importlib
        importlib.invalidate_caches()
        if mode == "gpu":
            _add_cuda_dirs()
        if mode == "update":
            # A loaded model keeps the OLD code alive; drop it so the next
            # pass runs on what was just installed.
            unload()
        # The folder the model will land in, made now rather than at first
        # load - so the "Where everything is" table goes green on install
        # instead of showing a cross for a directory nothing has needed yet.
        try:
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        INSTALL.update(state="done", finished_at=time.time())
        joblog.log(f"faster-whisper installed from the Whisper page "
                   f"({mode}) - detection now listens instead of inferring",
                   "ok", system="audiolang")
        pending_invalidate()
    except Exception as e:                               # noqa: BLE001
        INSTALL.update(state="error", finished_at=time.time(),
                       error=f"{type(e).__name__}: {str(e)[:160]}")
        joblog.log(f"whisper install failed: {e}", "warn", system="audiolang")
    finally:
        # The lane is claimed in install_start() and belongs to this thread
        # until it ends, whichever way it ends. Released here rather than at
        # the end of the happy path so a failed install cannot hold it.
        try:
            from . import heavy
            heavy.release("Whisper install")
        except Exception:                                # noqa: BLE001
            pass


# The probe's verdict, per (device, compute). Cached because the answer is a
# property of this machine and this wheel, not of the moment - and because the
# probe costs a process start and a model load.
_PROBE: dict[tuple, tuple[bool, str]] = {}
PROBE_TIMEOUT_S = 300.0


def probe_reset() -> None:
    """Forget the verdicts - after an install, or a hardware change."""
    _PROBE.clear()


def _blocked_on_disk() -> str:
    """The recorded 'this machine cannot' verdict, or ''.

    Written by whichever side found out first - Setup during an install, or
    this module the first time something tried to listen. Read before probing
    so a machine that has already proved it cannot run the model does not
    spend a process discovering it again after every restart.
    """
    try:
        p = os.path.join(str(DATA_DIR), "whisper-cannot-run.txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return fh.read().strip() or "recorded as unable to run here"
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def _load_probe(dev: str, ct: str) -> tuple[bool, str]:
    """(safe_to_load_here, why_not). See app/whisper_probe.py."""
    key = (dev, ct)
    if key in _PROBE:
        return _PROBE[key]
    known = _blocked_on_disk()
    if known:
        _PROBE[key] = (False, known)
        return _PROBE[key]
    import sys as _sys
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "whisper_probe.py")
    if not os.path.exists(script):
        _PROBE[key] = (True, "")          # no probe shipped: behave as before
        return _PROBE[key]
    try:
        r = subprocess.run(
            [_sys.executable, script, "--device", dev, "--compute", ct,
             "--root", str(MODEL_DIR), "--size", MODEL_SIZE],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
            creationflags=NO_WINDOW,
            startupinfo=hidden_si())
    except subprocess.TimeoutExpired:
        _PROBE[key] = (False, f"loading the model on {dev} did not finish in "
                              f"{int(PROBE_TIMEOUT_S)}s")
        return _PROBE[key]
    except Exception:                                    # noqa: BLE001
        # COULD NOT ASK IS NOT THE SAME AS NO. If the probe itself fails to
        # start, fall through to the old behaviour rather than disabling a
        # feature that may work perfectly well.
        _PROBE[key] = (True, "")
        return _PROBE[key]
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0 and out.startswith("OK"):
        _PROBE[key] = (True, "")
    elif r.returncode == 2:
        # ARGPARSE, NOT A CRASH. Python's argparse exits 2 on a usage error,
        # and "anything that is not 0 or 1" used to be read as "the process
        # was killed" - so a mistyped flag would have been reported to the
        # user as "this machine cannot run the model", which is a lie about
        # their hardware. Could-not-ask is not the same as no.
        _PROBE[key] = (True, "")
        joblog.log("whisper load probe: bad arguments (exit 2) — not treating "
                   "this as a hardware failure", "warn", system="audiolang")
        return _PROBE[key]
    elif r.returncode < 0 or r.returncode > 2:
        # KILLED. NAME THE CODE, DO NOT GUESS THE CAUSE.
        #
        # This used to assert "your CPU is missing AVX2" for one exit code and
        # imply it for the rest. Debugged on a real VM that crashes here: the
        # CPU is an i9-9900X reporting AVX, AVX2 AND AVX512, CTranslate2
        # imports fine and lists its CPU compute types, and the model
        # constructor still dies - with 0xC0000005, an access violation, not
        # 0xC000001D. Forcing a lower instruction set, reinstalling the wheel
        # and loading the smallest model all reproduce it. So AVX2 is claimed
        # ONLY when the code actually says illegal instruction, and every
        # other code is reported as what it is.
        code = r.returncode & 0xFFFFFFFF
        names = {0xC000001D: "illegal instruction",
                 0xC0000005: "access violation",
                 0xC0000409: "stack buffer overrun",
                 0xC0000135: "a required DLL was not found",
                 0xC000007B: "a DLL is the wrong architecture"}
        named = names.get(code, "")
        why = (f"loading the model on {dev} crashed this machine "
               f"(exit 0x{code:08X}{' — ' + named if named else ''})")
        if code == 0xC000001D:
            why = ("this CPU is missing an instruction set that the language "
                   "identifier requires (CTranslate2 needs AVX2) — it cannot "
                   "run here, on the GPU or the CPU. Common on virtual "
                   "machines whose host does not pass AVX2 through")
        elif code == 0xC0000005:
            why += (" — the library loads but cannot build a model here. "
                    "Seen on virtual machines; a page file of zero and little "
                    "free memory make it more likely")
        _PROBE[key] = (False, why)
        joblog.log(f"whisper load probe: {why}", "warn", system="audiolang")
        # TELL THE INSTALLER TOO. Setup reads this file to grey its Whisper
        # checkbox, so a machine that discovers the problem here is not
        # offered the same install again on the next upgrade. Same filename
        # the installer writes, in the data directory both of them share.
        try:
            with open(os.path.join(str(DATA_DIR), "whisper-cannot-run.txt"),
                      "w", encoding="utf-8") as fh:
                fh.write(why)
        except Exception:                                # noqa: BLE001
            pass
    else:
        _PROBE[key] = (False, out[:200] or f"exit {r.returncode}")
    return _PROBE[key]


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
            # ASK A CHILD FIRST. Constructing this model is native code, and on
            # a CPU without the instruction set CTranslate2 was built for it
            # does not raise - it executes an illegal instruction and Windows
            # kills the process. That is not a failure this function can catch:
            # pressing "Test detection now" on such a machine took the whole
            # server down, with WerFault.exe in the process list and "Failed to
            # fetch" on the page. The probe finds out in a process we can
            # afford to lose.
            ok, why = _load_probe(dev, ct)
            if not ok:
                last = RuntimeError(why)
                continue
            try:
                _MODEL = WhisperModel(MODEL_SIZE, device=dev, compute_type=ct,
                                      download_root=str(MODEL_DIR))
                _MODEL_ERR = ""
                global _MODEL_DEV
                _MODEL_DEV = dev
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
            creationflags=NO_WINDOW,
            startupinfo=hidden_si())
        return float((p.stdout or "0").strip() or 0)
    except Exception:                                    # noqa: BLE001
        return 0.0


# A HEALTHY WINDOW TAKES ONE TO FIVE SECONDS. Warehouse 13 S01E10 took 240
# on every window, three times over, and the verdict was "extract failed"
# after twelve minutes of a thread doing nothing: ffmpeg had read the header
# and then sat in a page-in wait on the pool - the disk not answering for
# that region of the file - with all twenty threads idle. Sixty seconds is
# twelve times the honest worst case and still short enough to be a report
# rather than an outage.
PCM_TIMEOUT_S = 60


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
         "-f", "f32le", "-"], capture_output=True, timeout=PCM_TIMEOUT_S,
        creationflags=NO_WINDOW, startupinfo=hidden_si())
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


# WHERE THE WINDOWS COME FROM, AND WHY THE MIDDLE WAS THE WRONG PLACE.
#
# The first pass took three windows at 0.30, 0.50 and 0.70 - all inside the
# middle 40% of the file. Invasion (2021) S03E02 is a mostly-English episode
# with a long Japanese stretch through its middle, so all three landed in the
# same foreign scene, agreed with each other, and reported Japanese at 92%.
# Nothing was wrong with the model or the judging: three windows that sit
# within forty percent of each other are one sample, not three.
#
# The same show proves it. Every other episode read English correctly, because
# their middles happen to be English:
#     S02E03  [en 0.996, en 0.953, en 0.952]
#     S02E06  [en 0.990, en 0.986, en 0.771]
#
# Five windows now, spread from a tenth in to five sixths through, so a scene
# of any one language cannot own the whole sample. The second look interleaves
# with the first rather than sitting inside it - three more windows in the same
# region would only have confirmed the same scene.
FIRST_LOOK = (0.10, 0.28, 0.46, 0.64, 0.85)
SECOND_LOOK = (0.05, 0.19, 0.37, 0.55, 0.73, 0.93)
OUTLIER_MAX = 0.80                 # a lone dissenter under this is not a second language


def _extract_why(e: Exception) -> str:
    """A cause, not the first 80 characters of a command line."""
    if isinstance(e, subprocess.TimeoutExpired):
        return (f"ffmpeg took over {int(e.timeout)}s to pull one 30s window - "
                "the disk was probably saturated; it will be tried again")
    msg = str(e)
    # str(CalledProcessError) is the whole argv; the useful part is the end
    return f"extract failed: {msg[-100:]}" if len(msg) > 100 else f"extract failed: {msg}"


def _judge(votes: list, strong_floor: float = MIN_PROB) -> dict:
    r"""The verdict for a set of windows, and whether it is worth a second look.

    Returns {code2, confidence, ok, why, retry}. `retry` says the outcome was
    the model being unsure rather than the file being unreadable - the only
    case where more windows could change the answer.
    """
    out = {"code2": "", "confidence": 0.0, "ok": False, "why": "", "retry": False}
    if not votes:
        out["retry"] = True
        return out
    # A WINDOW THAT IS NOT CONFIDENT IS NOT EVIDENCE, so it does not get a vote.
    #
    # This filter used to run AFTER the agreement check, which let noise veto a
    # near-certain result. Blassreiter S01E05 scored [zh 0.36, ja 0.985,
    # ja 0.994] and was thrown out as "windows disagree" - one junk window
    # outvoting two that were 99% sure. A 0.36 score is the model saying it
    # cannot tell, and "cannot tell" is not a dissenting opinion.
    strong = [v for v in votes if v[1] >= strong_floor]
    if not strong:
        best = max(votes, key=lambda v: v[1])
        out.update(code2=best[0], confidence=best[1], retry=True,
                   why=(f"best guess {best[0]} at {best[1]:.2f}, below the "
                        f"{strong_floor:.2f} floor"))
        return out
    by: dict[str, list] = {}
    for l, pr in strong:
        by.setdefault(l, []).append(pr)
    if len(by) > 1:
        # Still deliberately NOT a majority vote. Two CONFIDENT windows that
        # disagree is the signature of a dual-language track, and the right
        # answer there is to stop and let a human look.
        #
        # ONE EXCEPTION, EARNED BY THE SECOND LOOK. Five windows spread across
        # the file saying English at 1.00 and one saying Chinese at 0.66 is
        # not a dual-language track - a real one splits, because the windows
        # are spread. So a single dissenter, under OUTLIER_MAX, outvoted at
        # least four to one, is recorded and set aside. Anything closer than
        # that still stops.
        lead = max(by, key=lambda k: len(by[k]))
        others = [(l, pr) for l, prs in by.items() if l != lead for pr in prs]
        if (len(others) == 1 and others[0][1] < OUTLIER_MAX
                and len(by[lead]) >= 4):
            conf = round(min(by[lead]), 3)
            out.update(code2=lead, confidence=conf, ok=True,
                       why=(f"{len(by[lead])} window(s) agreed on {lead} at "
                            f"{conf:.2f}; one window said {others[0][0]} at "
                            f"{others[0][1]:.2f} and was set aside as an outlier"))
            return out
        out.update(retry=True,
                   why=("windows disagree: " + ", ".join(sorted(by))
                        + f" (from {len(strong)} confident window(s))"))
        return out
    code2 = strong[0][0]
    conf = round(min(v[1] for v in strong), 3)
    out.update(code2=code2, confidence=conf, ok=True)
    if len(strong) < len(votes):
        out["why"] = (f"{len(votes) - len(strong)} window(s) ignored as "
                      f"too uncertain to count; ")
    out["why"] += f"{len(strong)} window(s) agreed on {code2} at {conf:.2f}"
    return out


def detect(path: str, track: int = 0, fracs=FIRST_LOOK,
           secs: int = 30) -> dict:
    r"""Listen to `track` of `path` and report what language it is in.

    Returns {code, code2, confidence, votes, ok, why}. `code` is ISO 639-2 or
    "" when the answer is not trustworthy - and "not trustworthy" is a real
    outcome here, not a failure. Everything downstream treats "" as "leave it
    alone", which is the safe direction: a blank tag is already the status quo.

    TWO LOOKS BEFORE REFUSING. Five windows spread across the file settle most
    tracks. When they do not - all under the floor, or confident and
    disagreeing - six MORE are taken from between them (SECOND_LOOK) and the
    verdict is made over all eleven. A refusal after eleven windows spread
    across the file is worth something; a refusal after three that happened to
    land on a song, a shout or a foreign-language scene was not. Files the
    model cannot read at all are not retried: the second look is for
    uncertainty, not for a disk that timed out.
    """
    import numpy as np
    out = {"code": "", "code2": "", "confidence": 0.0, "votes": [],
           "ok": False, "why": "", "overall": [], "windows": 0, "looks": 0}
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
    err = ""

    def listen(where) -> int:
        """Take one window at each fraction; returns how many were readable."""
        nonlocal silent, err
        got = 0
        for fr in where:
            try:
                a = _pcm(path, track, dur * fr, secs)
            except subprocess.TimeoutExpired as e:
                # The next window is the same file on the same disk; a
                # second sixty seconds would only say the same thing.
                err = _extract_why(e)
                break
            except Exception as e:                       # noqa: BLE001
                err = _extract_why(e)
                continue
            if a.size < 16000:
                continue
            got += 1
            # A silent window identifies as whatever the model's prior likes.
            # It is not a vote, and three silent windows are not a consensus.
            if float(np.abs(a).mean()) < SILENCE:
                silent += 1
                continue
            try:
                lang, prob, all_probs = m.detect_language(a)
            except Exception as e:                       # noqa: BLE001
                err = f"detect failed: {str(e)[:80]}"
                continue
            votes.append((str(lang), round(float(prob), 3)))
            # KEEP THE RUNNERS-UP. The model scores every language it knows,
            # and discarding all but the winner throws away the only thing
            # that tells a decisive window from a coin flip: "ja 0.97, zh
            # 0.01" and "ja 0.51, zh 0.48" both reduce to a vote for "ja",
            # and only one of them is worth acting on.
            dists.append(_top(all_probs, 8))
        return got

    readable = listen(fracs)
    out["looks"] = 1
    v = _judge(votes)
    if not v["ok"] and v["retry"] and readable:
        # The file can be read and the model was unsure: look again, elsewhere.
        listen(SECOND_LOOK)
        out["looks"] = 2
        v = _judge(votes)

    out["votes"] = votes
    out["overall"] = aggregate(dists)
    out["windows"] = len(votes)
    second = " (after a second look at three more windows)" if out["looks"] == 2 else ""
    if not votes:
        out["why"] = err or (f"{silent} silent window(s), no speech found"
                             if silent else "no usable audio")
        return out
    out.update(code2=v["code2"], confidence=v["confidence"],
               code=_TO3.get(v["code2"], "") if v["ok"] else "")
    if not v["ok"]:
        out["why"] = v["why"] + second
        return out
    if not out["code"]:
        out["why"] = f"detected {v['code2']}, which has no ISO 639-2 mapping here"
        return out
    out["ok"] = True
    out["why"] = v["why"] + second
    return out


# ---------------------------------------------------------------- self-test

def self_test() -> dict:
    """Prove the whole pipeline on one real file - the codec page's
    test-encode, for ears.

    GRADED, NOT JUST EXERCISED. A test that only proves "it returned
    something" cannot tell a working detector from a confidently wrong one,
    so the file is chosen from tracks that already STATE a language: the
    detector listens blind, and its answer is marked against the tag. Nothing
    is written anywhere - this is a read, a listen, and a report.
    """
    t0 = time.time()

    def _done(d: dict) -> dict:
        d["elapsed"] = round(time.time() - t0, 1)
        return d

    if not available():
        return _done({"ok": False, "ran": False,
                      "why": "the language identifier is not installed - "
                             "install it above, or from Settings → Whisper"})
    had_model = bool(model_cache().get("path"))
    try:
        ensure_table()
        with cursor() as cur:
            rows = cur.execute(
                "SELECT id, path, title, audio_langs FROM files "
                " WHERE state != 'deleted' AND audio_langs IS NOT NULL "
                "   AND audio_langs != '' ORDER BY RANDOM() LIMIT 300"
            ).fetchall()
    except Exception as e:                               # noqa: BLE001
        return _done({"ok": False, "ran": False,
                      "why": f"could not read the library: {str(e)[:120]}"})
    pick = graded = fallback = None
    missing = 0
    for r in rows:
        if not os.path.exists(r["path"]):
            missing += 1
            continue
        if fallback is None:
            fallback = (r, 0)      # ungraded, but still a real run
        for ai, tg in enumerate((r["audio_langs"] or "").split(",")):
            tg = (tg or "").strip().lower()
            if tg and tg not in ("-", "und", "zxx"):
                pick, graded = (r, ai), tg
                break
        if pick:
            break
    if pick is None:
        pick = fallback
    if not pick:
        # SAME DISTINCTION AS THE OCR TEST. "scan a library first" is only
        # right when there is nothing indexed; when rows exist but every one
        # of them fails os.path.exists, the library is indexed and the files
        # are out of reach - which on a UNC path usually means the service
        # account cannot see the share, not that anything needs scanning.
        if not rows:
            why = ("nothing indexed yet - scan a library first")
        elif missing == len(rows):
            why = (f"all {len(rows)} indexed file(s) are unreadable from "
                   "here. If this library lives on a network share, "
                   "remember nuarr runs as a service: SYSTEM reaches the "
                   "network as the machine account, not as you, so a share "
                   "that opens fine in Explorer can still be invisible to "
                   "it. A local path, or granting the computer account "
                   "access to the share, resolves it")
        else:
            why = "no file with an audio track to test with"
        return _done({"ok": False, "ran": False, "why": why,
                      "candidates": len(rows), "unreachable": missing})
    r, track = pick
    res = detect(r["path"], track)
    heard = res.get("code") or ""
    return _done({
        "ok": bool(res.get("ok")), "ran": True,
        "title": r["title"] or os.path.basename(r["path"]),
        "path": r["path"], "track": track,
        "stated": graded or "", "heard": heard,
        "heard2": res.get("code2", ""),
        "confidence": res.get("confidence", 0.0),
        "windows": res.get("windows", 0),
        "votes": res.get("votes", []),
        "why": res.get("why", ""),
        # None = the file carried no tag to grade against; True/False = the
        # detector's blind answer against what the track says it is.
        "match": (heard == graded) if (graded and heard) else None,
        "device": _MODEL_DEV or ("cuda" if info().get("cuda_devices") else "cpu"),
        "downloaded_model": not had_model and bool(model_cache().get("path")),
    })


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
                           creationflags=NO_WINDOW,
            startupinfo=hidden_si())
    except Exception as e:                               # noqa: BLE001
        return False, str(e)[:160]
    if r.returncode != 0:
        return False, (r.stdout or r.stderr or "").strip()[:160]
    return True, ", ".join(f"a:{k}={v}" for k, v in sorted(tags.items()))


# ---- what a correction is doing, while it is doing it -------------------
# WHY THE STEPS ARE PUBLISHED RATHER THAN ANIMATED. Correcting a tag is not one
# action, it is several, and they do not cost the same: the header edit is
# milliseconds, re-reading the file is a full ffprobe of something that may be
# 40 GB on a spinning disk, and telling the arrs is HTTP to a Sonarr that may
# already be busy. A spinner over all of that says "busy" and nothing else, so
# a correction waiting on Sonarr looks exactly like one that has hung - and the
# button that only said "..." got pressed again.
#
# So the work reports where it is and the page reads it. The bar counts STEPS,
# and is labelled as counting steps: interpolating between steps of unequal
# cost would be a smoother animation telling a worse lie.
#
# Kept in memory, not in the database. This is the state of a request in
# flight; it has no meaning after a restart and writing it to disk would only
# create rows that outlive the thing they describe.
FIX_KEEP_S = 180.0

_FIX_LABEL = {
    "open":   "checking the file can be edited",
    "tag":    "writing the language tag",
    "title":  "correcting the track title",
    "reread": "re-reading the file",
    "rules":  "re-checking it against the rules",
    "arrs":   "telling Sonarr and Radarr",
}
_FIXING: dict[str, dict] = {}
_FIX_LOCK = threading.Lock()


def _fix_key(file_id: int, track: int) -> str:
    return f"{int(file_id)}:{int(track)}"


def _sweep_fixes() -> None:
    """Drop finished corrections nobody came back to read. Lock held."""
    now = time.time()
    for k in [k for k, v in _FIXING.items()
              if v.get("done") and now - v.get("ended", now) > FIX_KEEP_S]:
        _FIXING.pop(k, None)


def fix_begin(file_id: int, track: int, steps) -> str:
    """Declare the steps this correction will take, and start its clock.

    THE STEPS ARE DECLARED UP FRONT because the two callers do different work -
    the settings page writes a tag the person picked, the Attention tile also
    fixes a track title - and a bar that learns its own length as it goes would
    jump backwards the moment a path turned out to have one more step.
    """
    key = _fix_key(file_id, track)
    with _FIX_LOCK:
        _sweep_fixes()
        _FIXING[key] = {"steps": [str(s) for s in steps], "at": time.time(),
                        "i": 0, "step": "", "label": "starting",
                        "done": False, "ok": False, "why": "", "ended": 0.0}
    return key


def fix_step(key: str, name: str) -> None:
    with _FIX_LOCK:
        st = _FIXING.get(key)
        if not st or st["done"]:
            return
        try:
            st["i"] = st["steps"].index(name)
        except ValueError:
            # A step the caller did not declare. Advance rather than reset:
            # being one ahead of the plan is better than appearing to restart.
            st["i"] = min(st["i"] + 1, len(st["steps"]))
        st["step"] = name
        st["label"] = _FIX_LABEL.get(name, name)


def fix_end(key: str, ok: bool, why: str = "") -> None:
    """Finish. A FAILURE LEAVES THE BAR WHERE IT STOPPED.

    Filling the bar on the way out would say "all five steps done" about a
    correction that died on the first one - the shape of the thing on screen
    contradicting the words next to it. Stopping at a fifth is the more useful
    fact anyway: it says the file was never opened, not that the arrs refused.
    """
    with _FIX_LOCK:
        st = _FIXING.get(key)
        if not st:
            return
        st.update(done=True, ok=bool(ok), why=str(why or ""), step="",
                  ended=time.time(),
                  i=len(st["steps"]) if ok else st["i"],
                  label="corrected" if ok else "could not correct it")


def fix_state(file_id: int, track: int) -> dict:
    """What the page should draw. Empty when there is nothing to say."""
    with _FIX_LOCK:
        _sweep_fixes()
        st = _FIXING.get(_fix_key(file_id, track))
        if not st:
            return {}
        n = len(st["steps"]) or 1
        return {"running": not st["done"], "done": bool(st["done"]),
                "ok": bool(st["ok"]), "why": st["why"], "label": st["label"],
                # ONE-BASED, AND NEVER ZERO. `i` is the index of the step in
                # flight, so a running correction is on step i+1. A failed one
                # is not on a step at all - but "0 of 5" reads as "nothing was
                # attempted" when in truth the first step is exactly what
                # failed, so it stays at the step it died on.
                "step": max(1, min(st["i"] + (0 if st["done"] else 1), n)),
                "steps": n, "pct": min(100.0, st["i"] / n * 100.0),
                "elapsed": round(time.time() - st["at"], 1)}


def apply_and_restamp(file_id: int, path: str, tags: dict[int, str]) -> tuple[bool, str]:
    """apply_tags, then keep the verdicts that justified it valid."""
    ok, why = apply_tags(path, tags)
    if ok:
        restamp(file_id, path)
    return ok, why


def notify_arrs(file_ids) -> int:
    r"""Tell Sonarr/Radarr - and Plex - that these files changed.

    ONE PATH NOW, in notify.py, because this was the only system doing the
    job and it still had a hole. Plex was told solely as a SIDE EFFECT of the
    rename landing, so a library whose naming format carries no language token
    told Plex nothing at all - and Plex caches track languages in its own
    metadata just as Sonarr does. The file said Japanese and Plex went on
    saying English until its own scan came round, which on this server can be
    a day.

    The rename is still asked for: this library's naming format carries "[JA]",
    so a file imported while its track read English is now named wrongly.
    """
    from . import notify
    return notify.file_changed(file_ids,
                               why="audio language tag corrected",
                               plex=True, arrs=True, rename=True,
                               system="audiolang")["arrs"]


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


# FRESHLY LANDED FILES GO FIRST.
#
# unverified() walks the library oldest-id-last and hands back whatever it
# finds; on a backlog of 39,161 that means a file committed this evening waits
# behind twenty thousand that have been sitting there for months. The one that
# just landed is the one somebody is about to watch, and the one whose release
# is still fresh enough to blocklist usefully.
#
# Kept in a table rather than memory because the gap between "landed" and
# "listened to" spans restarts by design - the sweep runs on its own clock.
_JUMP_READY = False


def _jump_init() -> None:
    global _JUMP_READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audio_lang_queue(
                file_id INTEGER PRIMARY KEY,
                at      REAL NOT NULL
            )""")
    _JUMP_READY = True


def queue_check(file_id: int) -> None:
    """Ask for this file to be listened to next. Cheap; never listens here."""
    try:
        if not _JUMP_READY:
            _jump_init()
        with cursor() as cur:
            cur.execute("INSERT INTO audio_lang_queue(file_id,at) VALUES(?,?) "
                        "ON CONFLICT(file_id) DO UPDATE SET at=excluded.at",
                        (int(file_id), time.time()))
    except Exception:                                    # noqa: BLE001
        pass


def _queue_rows(limit: int = 100000) -> list:
    """Queue rows joined to their file, in whatever state the file is in.

    A LEFT join, unlike the reader below, because the rows this is used to
    tidy up are exactly the ones an inner join hides.
    """
    with cursor() as cur:
        return cur.execute(
            "SELECT q.file_id, q.at, f.state, f.path, f.title, f.season, "
            "       f.episode, f.library, "
            "       COALESCE(f.audio_langs,'') AS audio_langs "
            "  FROM audio_lang_queue q LEFT JOIN files f ON f.id = q.file_id "
            " ORDER BY q.at LIMIT ?", (int(limit),)).fetchall()


def _verdicts_for(ids) -> dict:
    """Stored verdicts for JUST these files, keyed (file_id, track).

    This used to be `SELECT * FROM audio_lang` - 36,966 rows pulled into a
    dict on every pass to answer a question about twenty-five files.
    """
    ids = sorted({int(i) for i in ids})
    out: dict = {}
    if not ids:
        return out
    with cursor() as cur:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            qs = ",".join("?" * len(chunk))
            for r in cur.execute(
                    f"SELECT * FROM audio_lang WHERE file_id IN ({qs})", chunk):
                out[(r["file_id"], r["track"])] = r
    return out


def queued(limit: int = 200) -> list[dict]:
    """The jump queue, oldest request first, as pending()-shaped rows."""
    out: list[dict] = []
    try:
        if not _JUMP_READY:
            _jump_init()
        ensure_table()
        rows = [r for r in _queue_rows(limit)
                if r["state"] == "done" and (r["audio_langs"] or "")]
        have = _verdicts_for([r["file_id"] for r in rows])
    except Exception:                                    # noqa: BLE001
        return out
    for r in rows:
        codes = (r["audio_langs"] or "").split(",")
        for ai, raw_code in enumerate(codes):
            prev = have.get((r["file_id"], ai))
            if prev is not None and row_fresh(prev, r["path"]):
                continue
            out.append({"file_id": r["file_id"], "path": r["path"], "track": ai,
                        "title": r["title"] or "", "season": r["season"],
                        "episode": r["episode"], "library": r["library"] or "",
                        "tagged": (raw_code or "").strip().strip("-"),
                        "jumped": True,
                        "n_audio": len(codes)})
    return out


def unqueue(file_ids) -> None:
    try:
        ids = [int(x) for x in file_ids]
        if not ids:
            return
        qs = ",".join("?" * len(ids))
        with cursor() as cur:
            cur.execute(f"DELETE FROM audio_lang_queue WHERE file_id IN ({qs})",
                        ids)
    except Exception:                                    # noqa: BLE001
        pass


# HOW THE QUEUE LEAKED, AND WHY unqueue() ALONE COULD NEVER FIX IT.
#
# run_once() unqueues what it was given. What it was given is queued(), which
# only returns rows that still NEED listening to - it drops any file that is
# not state='done', and skips any track whose verdict is already fresh. So a
# row that queued() filtered out was never in `todo`, was never handed to
# unqueue(), and stayed in the table being counted forever.
#
# Measured on this library at 25 rows: 2 had work, 11 were files not in
# 'done', and 12 were files whose every track had already been judged by some
# other path. Twenty-three of twenty-five permanent. The tile read "14 just
# landed" and the number only ever went up.
#
# The distinction that matters, and the reason this is not just a DELETE:
#
#   state='done', nothing outstanding -> ANSWERED. Drop it.
#   file gone, or state='deleted'     -> never coming back. Drop it.
#   any other state                   -> STILL LANDING. Keep its place. This
#                                        is the whole point of the queue: the
#                                        file was noticed at import and has
#                                        not finished being imported yet.
#
# Bounded per call, because it stats each file to decide whether a verdict
# still describes it, and a re-import of five thousand files must not turn a
# poll into a disk storm. Anything past the cap is tidied on the next call.
QSYNC_MAX = 500
QSYNC_TTL = 10.0
_QSYNC = {"at": 0.0}


def queue_sync(limit: int = QSYNC_MAX) -> int:
    """Drop queue rows with nothing left to answer. Returns the number gone."""
    try:
        if not _JUMP_READY:
            _jump_init()
        ensure_table()
        rows = _queue_rows(limit)
    except Exception:                                    # noqa: BLE001
        return 0
    if not rows:
        return 0
    have = _verdicts_for([r["file_id"] for r in rows if r["state"] == "done"])
    drop: list[int] = []
    for r in rows:
        st = r["state"]
        if st is None or st == "deleted":
            drop.append(r["file_id"])
            continue
        if st != "done":
            continue
        codes = [c for c in (r["audio_langs"] or "").split(",") if c != ""]
        outstanding = any(
            not row_fresh(have.get((r["file_id"], ai)), r["path"])
            for ai in range(len(codes)))
        if not outstanding:
            drop.append(r["file_id"])
    if drop:
        unqueue(drop)
    return len(drop)


def queue_count() -> int:
    """How many files are still waiting their turn.

    The count itself is one cheap COUNT(*); the tidy that makes it TRUE runs
    on a timer beside it, so the number corrects itself within ten seconds
    rather than waiting for the next sweep - and a poll every 1.2s does not
    pay for it every time.
    """
    try:
        if not _JUMP_READY:
            _jump_init()
        now = time.time()
        if now - _QSYNC["at"] >= QSYNC_TTL:
            _QSYNC["at"] = now
            queue_sync()
        with cursor() as cur:
            r = cur.execute("SELECT COUNT(*) n FROM audio_lang_queue").fetchone()
        return int(r["n"] or 0)
    except Exception:                                    # noqa: BLE001
        return 0


def unverified(limit: int = 5000) -> list[dict]:
    r"""Tracks that CARRY a tag nobody has ever checked against the audio.

    THE HOLE THIS FILLS, AND HOW IT WAS FOUND. pending() returns tracks whose
    language tag is missing - `audio_langs` holding a "-" - and listens to
    those. It has been right about every file it looked at and blind to the
    interesting one:

        Pass the Monster Meat, Milady! S01E12   [JA+EN]
            track 0   tagged jpn   heard jpn   0.961
            track 1   tagged eng   heard jpn   0.958

    Both tracks are Japanese. The release said dual audio, the container says
    dual audio, and the second track is the first one wearing a different
    label. pending() never offered it to Whisper because the tag was not
    missing - it was WRONG, which is a different thing and the harder one,
    because nothing downstream has any reason to doubt it.

    A missing tag is a gap; a wrong tag is a lie, and only listening can tell.
    So this returns the other population: tagged tracks with no fresh verdict.
    It is much larger - 16,339 files claim two or more languages and 21 of them
    had ever been read - so it is drained after pending(), never instead of it.
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
                " WHERE state='done' AND COALESCE(audio_langs,'') != '' "
                "   AND audio_langs != '-' "
                " ORDER BY id DESC").fetchall()
    except Exception:                                    # noqa: BLE001
        return out
    for r in rows:
        if len(out) >= limit:
            break
        codes = (r["audio_langs"] or "").split(",")
        for ai, raw_code in enumerate(codes):
            raw_code = (raw_code or "").strip()
            if not raw_code or raw_code == "-":
                continue                    # pending() owns the untagged ones
            prev = have.get((r["id"], ai))
            if prev is not None and row_fresh(prev, r["path"]):
                continue
            out.append({"file_id": r["id"], "path": r["path"], "track": ai,
                        "title": r["title"] or "", "season": r["season"],
                        "episode": r["episode"], "library": r["library"] or "",
                        "tagged": raw_code,
                        "n_audio": len(codes)})
    return out


_PACE = {"at": 0.0, "each": 0.0}


def secs_each_seen(sample: int = 400) -> float:
    """Seconds a track, from what has actually been listened to.

    A RATE THAT SURVIVES A RESTART. The pass in flight is the best evidence
    there is, and between passes there is none - so the backlog said nothing
    about how long it would take for most of the day, which is exactly when
    somebody asks. The verdict table has a timestamp per track: the gaps
    between consecutive ones ARE the pace, once the gaps that are really
    "asleep between passes" are dropped.
    """
    now = time.time()
    if now - _PACE["at"] < 300 and _PACE["each"]:
        return _PACE["each"]
    gaps: list = []
    try:
        ensure_table()
        with cursor() as cur:
            rows = [r["checked_at"] for r in cur.execute(
                "SELECT checked_at FROM audio_lang "
                " WHERE COALESCE(checked_at,0) > 0 "
                " ORDER BY checked_at DESC LIMIT ?", (int(sample),))]
        for a, b in zip(rows, rows[1:]):
            g = float(a or 0) - float(b or 0)
            # A gap longer than two minutes is the loop sleeping, not a track
            # taking two minutes; a zero gap is two tracks of one file.
            if 0.05 <= g <= 120:
                gaps.append(g)
    except Exception:                                            # noqa: BLE001
        return _PACE["each"]
    if not gaps:
        return _PACE["each"]
    gaps.sort()
    each = gaps[len(gaps) // 2]          # the median, not the mean: one
    _PACE.update(at=now, each=each)      # 40 GB outlier should not set the pace
    return each


def unverified_count() -> int:
    """How many tagged tracks have never been listened to."""
    try:
        ensure_table()
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) n FROM files f "
                " WHERE f.state='done' AND COALESCE(f.audio_langs,'') != '' "
                "   AND f.audio_langs != '-' "
                "   AND NOT EXISTS (SELECT 1 FROM audio_lang a "
                "                    WHERE a.file_id = f.id)").fetchone()
        return int(r["n"] or 0)
    except Exception:                                    # noqa: BLE001
        return 0


def _name_of(code: str) -> str:
    """The display name for a language code, whichever shape it arrives in.

    _LANG_NAME is keyed on two-letter codes and the tags are three-letter, so
    a single lookup returned "" and a rewrite quietly deleted the word it was
    supposed to replace - "English E-AC3 5.1" became " E-AC3 5.1". Every
    shape is tried, and an empty answer means do nothing at all.
    """
    from . import langkey
    c = (code or "").strip().lower()
    if not c:
        return ""
    # CHOICES IS THE THREE-LETTER TABLE and _LANG_NAME the two-letter one;
    # tags are three-letter, so looking only in the second returned "" for
    # every code a file actually carries. Both, then the key function.
    for x in CHOICES:
        if x.get("code") == c:
            return x.get("name") or ""
    for k in (c, c[:2]):
        if k in _LANG_NAME:
            return _LANG_NAME[k]
    try:
        k = langkey.key(c)
        if k in _LANG_NAME:
            return _LANG_NAME[k]
        for x in CHOICES:
            if langkey.key(x.get("code") or "") == k:
                return x.get("name") or ""
    except Exception:                                            # noqa: BLE001
        pass
    return ""


def title_lies(file_id: int) -> list[dict]:
    """Tracks whose TITLE still names a language the track is not.

    The tag and the title are two different fields and only one of them was
    ever corrected. A file can sit at lang=jpn title="English E-AC3 5.1"
    forever: the rules read the tag and are content, and the only person who
    sees the title is the one choosing a track in a player.
    """
    from . import langkey
    out: list[dict] = []
    try:
        with cursor() as cur:
            f = cur.execute("SELECT path, audio_langs FROM files WHERE id=?",
                            (int(file_id),)).fetchone()
        if not f:
            return out
        codes = [c.strip() for c in (f["audio_langs"] or "").split(",")]
        for i, code in enumerate(codes):
            if not code or code == "-":
                continue
            title = (_track_title(f["path"], i) or "").strip()
            if not title:
                continue
            for key, name in _LANG_NAME.items():
                if not re.search(rf"\b{re.escape(name)}\b", title, re.I):
                    continue
                try:
                    if langkey.same(key, code):
                        continue
                except Exception:                                # noqa: BLE001
                    pass
                want_name = _name_of(code)
                if not want_name:
                    break            # no name to put there: leave it alone
                out.append({"track": i, "title": title, "names": name,
                            "code": code,
                            "want": re.sub(rf"\b{re.escape(name)}\b",
                                           want_name, title,
                                           flags=re.IGNORECASE)})
                break
    except Exception:                                            # noqa: BLE001
        return out
    return [x for x in out if x["want"] and x["want"] != x["title"]]


# --------------------------------------------- what you have already said --
# A DISAGREEMENT IS ALMOST NEVER ABOUT ONE FILE.
#
# Primal is the proof. Twenty episodes of a show that tells its story without
# dialogue, whose few invented lines the model hears as Arabic or Swedish, and
# every one of those twenty is the same question with the same answer.
# Answering them one at a time is twenty presses to say one thing, and the
# thing being said is identical every time.
#
# So an answer is remembered, and remembered TWICE IN ONE SERIES means the
# series. Same bar and the same reasoning as the hardsub ignore list: one
# answer can be somebody clicking the wrong row, two is a pattern.
#
# THE HIGH END IS NOT RETIRED WITH IT. "This show is generally fine" is not
# the same claim as "every file in it is fine" - a Spanish dub of one episode
# tagged English at 0.99 is a real fault inside a show you have correctly
# excused, and it is exactly the thing that would otherwise slip through. So
# the memory retires the BAND, where the evidence was always ambiguous, and
# anything at or past the act line still appears.
#
# What it stops being is AUTOMATIC. Past the line auto rewrites the header on
# its own, and doing that inside a show whose tags you have twice said to
# leave alone is the machine overruling the person who has watched it. It is
# shown, it says why, and it waits.
#
# A MOVIE CAN NEVER REACH TWO, by construction: its series key is its own arr
# id, so a film's answer covers that film and nothing else. That is right
# rather than a limitation - two unrelated films sharing a library say nothing
# about each other.
LEAVE_MIN = 2
_LEFT: dict = {"at": 0.0, "series": set(), "pairs": set()}
_LEFT_TTL = 60.0


def ensure_left_table() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audio_lang_left(
                file_id  INTEGER NOT NULL,
                track    INTEGER NOT NULL,
                series   TEXT    NOT NULL DEFAULT '',
                label    TEXT    NOT NULL DEFAULT '',
                tagged   TEXT    NOT NULL DEFAULT '',
                heard    TEXT    NOT NULL DEFAULT '',
                at       REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY(file_id, track))""")


def series_key(arr_name, parent_id, library, title) -> str:
    r"""What counts as "the same show" for the memory.

    The arr's own series id where there is one: it survives a rename, a
    re-import and a folder move, none of which a title does. The title is the
    fallback for anything nuarr knows about that no arr owns.
    """
    if arr_name and parent_id:
        return f"{arr_name}#{int(parent_id)}"
    return f"~{(library or '').strip().lower()}#{(title or '').strip().lower()}"


def left_reset() -> None:
    """An answer must count at once, not in a minute."""
    _LEFT["at"] = 0.0


def _left() -> tuple[set, set]:
    """(series retired, exact tracks answered). Memoised - read on every row."""
    now = time.time()
    if now - _LEFT["at"] < _LEFT_TTL:
        return _LEFT["series"], _LEFT["pairs"]
    series, pairs = set(), set()
    try:
        ensure_left_table()
        with cursor() as cur:
            for r in cur.execute("SELECT file_id, track, series "
                                 "  FROM audio_lang_left"):
                pairs.add((int(r["file_id"]), int(r["track"])))
            for r in cur.execute(
                    "SELECT series, COUNT(*) n FROM audio_lang_left "
                    " WHERE COALESCE(series,'') != '' GROUP BY series"):
                if int(r["n"]) >= LEAVE_MIN:
                    series.add(r["series"])
    except Exception:                                            # noqa: BLE001
        return _LEFT["series"], _LEFT["pairs"]
    _LEFT.update(at=now, series=series, pairs=pairs)
    return series, pairs


def leave(file_id: int, track: int, on: bool = True) -> dict:
    r"""Record that the tag on this track is right after all.

    Written whatever the model thinks, and never checked against it: the whole
    value of this answer is that the person giving it knows something the
    model does not.
    """
    fid, trk = int(file_id), int(track)
    try:
        ensure_left_table()
        with cursor() as cur:
            r = cur.execute(
                "SELECT arr_name, arr_parent_id, library, title, season, "
                "       episode, audio_langs "
                "  FROM files WHERE id=?", (fid,)).fetchone()
            if not r:
                return {"ok": False, "why": "no such file"}
            key = series_key(r["arr_name"], r["arr_parent_id"],
                             r["library"], r["title"])
            if not on:
                cur.execute("DELETE FROM audio_lang_left "
                            " WHERE file_id=? AND track=?", (fid, trk))
            else:
                codes = (r["audio_langs"] or "").split(",")
                tagged = (codes[trk] or "").strip() if trk < len(codes) else ""
                heard = ""
                h = cur.execute("SELECT code FROM audio_lang "
                                " WHERE file_id=? AND track=?",
                                (fid, trk)).fetchone()
                if h:
                    heard = h["code"] or ""
                cur.execute(
                    "INSERT INTO audio_lang_left"
                    "(file_id,track,series,label,tagged,heard,at) "
                    "VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(file_id,track) DO UPDATE SET "
                    "  series=excluded.series, label=excluded.label, "
                    "  tagged=excluded.tagged, heard=excluded.heard, "
                    "  at=excluded.at",
                    (fid, trk, key, (r["title"] or ""), tagged, heard,
                     time.time()))
            n = int(cur.execute("SELECT COUNT(*) c FROM audio_lang_left "
                                " WHERE series=?", (key,)).fetchone()["c"])
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    left_reset()
    retired = n >= LEAVE_MIN
    return {"ok": True, "series": key, "label": r["title"] or "", "n": n,
            "retired": retired, "need": max(0, LEAVE_MIN - n),
            "why": (f"{n} answer(s) on this show - the rest of it is left "
                    f"alone below the {fix_at()}% line" if retired
                    else "noted. One more answer on this show and the rest "
                         "of it stops being asked about")}


def forget_series(key: str) -> dict:
    """Undo. Every answer for one show, gone - it starts being asked again."""
    try:
        ensure_left_table()
        with cursor() as cur:
            cur.execute("DELETE FROM audio_lang_left WHERE series=?", (key,))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    left_reset()
    return {"ok": True}


def left_shows() -> list[dict]:
    """Which shows are being left alone, and on what evidence."""
    out: list[dict] = []
    try:
        ensure_left_table()
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT series, COUNT(*) n, MAX(at) at, "
                    "       MAX(label) label FROM audio_lang_left "
                    " WHERE COALESCE(series,'') != '' "
                    " GROUP BY series ORDER BY n DESC, at DESC LIMIT 200"):
                out.append({"series": r["series"], "n": int(r["n"]),
                            "at": float(r["at"] or 0),
                            "label": r["label"] or r["series"],
                            "retired": int(r["n"]) >= LEAVE_MIN})
    except Exception:                                            # noqa: BLE001
        return out
    return out


def verdict_for(row) -> dict:
    r"""verdict_of, plus what this show has already been told about.

    ONE PLACE DECIDES. verdict_of knows only a number, and a number cannot
    know that you have twice said this show is fine. Every caller that used to
    ask verdict_of about a ledger row asks this instead, so the panel, the
    auto pass, the backlog count and the attention tile cannot drift apart -
    which is the fault that put two different floors on this feature.
    """
    d = verdict_of(row.get("confidence"))
    if row.get("held") and d["auto"] == "act":
        d["auto"] = "ask"
        d["held"] = True
        d["why"] = (f"{d['sure']}% is past the {fix_at()}% line, but you have "
                    f"already left this show's tags alone {LEAVE_MIN} times - "
                    f"so it is shown rather than corrected on its own")
    return d


def mismatches(limit: int = 200, floor: float | None = None,
               respect_answers: bool = True) -> list[dict]:
    r"""Tracks where what was heard is not what the tag claims.

    Only confident disagreements. A verdict this acts on gets a file rebuilt
    and a release blocklisted, so a shaky one has to stay a curiosity rather
    than become an action.
    """
    out: list[dict] = []
    try:
        from . import langkey
        ensure_table()
        with cursor() as cur:
            rows = cur.execute(
                "SELECT a.file_id, a.track, a.code, a.confidence, a.checked_at,"
                "       a.votes, a.overall, "
                "       f.path, f.library, f.title, f.audio_langs, "
                "       f.arr_name, f.arr_parent_id "
                "  FROM audio_lang a JOIN files f ON f.id = a.file_id "
                " WHERE a.ok = 1 AND COALESCE(a.code,'') != '' "
                "   AND f.state = 'done' "
                # AND HEARD FROM THE FILE THAT IS THERE NOW. store() has
                # always recorded the size it listened to; nothing read it
                # back, so a re-encode or a remux left the old verdict
                # standing and the panel offered to correct a track that may
                # not exist any more.
                "   AND (a.size IS NULL OR a.size = 0 OR f.size IS NULL "
                "        OR a.size = f.size) "
                " ORDER BY a.checked_at DESC").fetchall()
    except Exception:                                    # noqa: BLE001
        return out
    for r in rows:
        if len(out) >= limit:
            break
        codes = (r["audio_langs"] or "").split(",")
        if r["track"] >= len(codes):
            continue
        tagged = (codes[r["track"]] or "").strip()
        if not tagged or tagged == "-":
            continue                        # untagged is a gap, not a lie
        try:
            same = langkey.same(tagged, r["code"])
        except Exception:                                # noqa: BLE001
            same = tagged[:2].lower() == (r["code"] or "")[:2].lower()
        if same:
            continue
        # TWO FLOORS THAT DID NOT KNOW ABOUT EACH OTHER. This one was a flat
        # 0.85 and the panel's is the leave-alone line, so a disagreement at
        # 82% was listed, offered a button, and then told "no confident
        # mismatch recorded for that track" - the check refusing to find the
        # row it had just drawn. The default is the line a person set; a
        # caller acting on one named track passes 0, because a floor decides
        # what to OFFER and a person pressing a button has already decided.
        if float(r["confidence"] or 0) < (leave_at() / 100.0
                                          if floor is None else floor):
            continue
        # AND THE GUARD THAT INVASION EARNED. A track is only lying if the
        # language it claims was never heard in it. If ANY confident window
        # heard the tagged language, the track is mixed - which is a property
        # of the show, not a fault in the file - and calling that a mislabel
        # would invite blocklisting a release for being bilingual on purpose.
        #
        # Read from the per-window votes rather than the verdict, because the
        # verdict is the majority and the whole point here is the minority.
        try:
            votes = json.loads(r["votes"] or "[]")
        except Exception:                                # noqa: BLE001
            votes = []
        heard_the_tag = any(
            langkey.same(v[0], tagged) and float(v[1] or 0) >= 0.60
            for v in votes if isinstance(v, (list, tuple)) and len(v) >= 2)
        if heard_the_tag:
            continue
        # AND THE WHOLE SAMPLE, NOT ITS LOUDEST MOMENT. A verdict is the most
        # confident window; `overall` is every window's distribution added up,
        # and when the two disagree the aggregate is the better evidence.
        #
        # Primal is the case that proves it: a show that tells its story
        # without dialogue, and what little speech it has is invented. One
        # window of S01E10 came back Arabic at 0.97 while three others heard
        # English at 0.31-0.57 - too quiet to pass the guard above, but the
        # aggregate is en 0.30 against ar 0.19. Correcting that tag would say
        # a show with no English audio, which turns on forced subtitles, and
        # tells the arr its English release is not English.
        #
        # So: if the tagged language leads the aggregate, the tag is not lying,
        # whatever one window shouted. On this library it drops exactly the two
        # Primal rows and keeps all thirty-one real ones - the true positives
        # sit at 0.74-1.00 for the heard language with the tag at 0.00-0.11.
        try:
            overall = json.loads(r["overall"] or "[]")
        except Exception:                                # noqa: BLE001
            overall = []
        if overall:
            top = (overall[0] or [""])[0]
            try:
                if top and langkey.same(top, tagged):
                    continue
            except Exception:                            # noqa: BLE001
                pass
        # AND WHAT YOU HAVE ALREADY SAID ABOUT THIS SHOW.
        #
        # Last of the guards on purpose: the ones above decide whether this is
        # a real disagreement, and this one decides whether a real
        # disagreement is still worth putting in front of you.
        #
        # A caller acting on ONE NAMED TRACK passes respect_answers=False, for
        # the same reason it passes floor=0: these decide what to OFFER, and
        # somebody pressing a button has already decided.
        held = False
        if respect_answers:
            _series, _pairs = _left()
            if (r["file_id"], r["track"]) in _pairs:
                continue            # you answered this exact track yourself
            _key = series_key(r["arr_name"], r["arr_parent_id"],
                              r["library"], r["title"])
            if _key in _series:
                if int(round(float(r["confidence"] or 0) * 100)) < fix_at():
                    continue        # the band: retired with the show
                held = True         # past the line: shown, but never on its own
        # AND THE ONE THAT MATTERS MOST: is this track a duplicate of another
        # one in the same file, wearing a different label? That is what turns
        # "a tag is wrong" into "this release lied about being dual audio".
        heard_twice = sum(1 for i, c in enumerate(codes)
                          if i != r["track"] and langkey.same(c, r["code"]))
        out.append({"file_id": r["file_id"], "track": r["track"],
                    "tagged": tagged, "heard": r["code"],
                    "confidence": round(float(r["confidence"] or 0), 3),
                    "path": r["path"], "library": r["library"] or "",
                    "title": r["title"] or "",
                    "at": r["checked_at"],
                    "held": held,
                    "series": series_key(r["arr_name"], r["arr_parent_id"],
                                         r["library"], r["title"]),
                    "fake_dual": bool(heard_twice),
                    "langs": r["audio_langs"] or ""})
    return out


# The handful of names a release actually writes into a track title. Not a
# full ISO table: this is only ever compared against a title to decide whether
# the title is merely restating the language, and a name nobody uses would
# never match anyway.
_LANG_NAME = {
    "en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "hi": "Hindi",
    "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish", "tr": "Turkish", "th": "Thai",
    "vi": "Vietnamese", "id": "Indonesian", "he": "Hebrew", "cs": "Czech",
    "hu": "Hungarian", "el": "Greek", "uk": "Ukrainian", "ro": "Romanian",
}


def _track_title(path: str, track: int) -> str:
    """The title on one audio track.

    THE PROBE FIRST, THEN THE FILE. The stored probe is free and usually
    right, but it is keyed on a path that renaming changes and it is written
    before a correction rather than after - and when it comes back empty this
    returned "", which the retitle step read as "no title to fix". That is how
    a track ended up tagged jpn and titled "English E-AC3 5.1": the lie was
    never seen, because the only place it was looked for was out of date.
    """
    try:
        with cursor() as cur:
            r = cur.execute("SELECT p.json FROM file_probes p "
                            "JOIN files f ON f.id = p.file_id "
                            "WHERE f.path = ?", (path,)).fetchone()
        if r:
            auds = [s for s in (json.loads(r["json"]).get("streams") or [])
                    if s.get("codec_type") == "audio"]
            if track < len(auds):
                t = str((auds[track].get("tags") or {}).get("title") or "")
                if t:
                    return t
    except Exception:                                    # noqa: BLE001
        pass
    return _track_title_live(path, track)


def _track_title_live(path: str, track: int) -> str:
    """Straight from the container's header. One mkvmerge call, no decode."""
    try:
        exe = getattr(SETTINGS, "mkvmerge", "") or \
            r"C:\Program Files\MKVToolNix\mkvmerge.exe"
        if not os.path.exists(exe) or not os.path.exists(path):
            return ""
        r = subprocess.run([exe, "-J", path], capture_output=True, text=True,
                           timeout=120, creationflags=NO_WINDOW,
                           startupinfo=hidden_si())
        auds = [t for t in (json.loads(r.stdout or "{}").get("tracks") or [])
                if t.get("type") == "audio"]
        if track < len(auds):
            return str((auds[track].get("properties") or {}).get(
                "track_name") or "")
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def _set_track_title(path: str, track: int, title: str) -> bool:
    """mkvpropedit again - same 1-based audio numbering as apply_tags."""
    exe = getattr(SETTINGS, "mkvpropedit", "") or \
        r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
    if not os.path.exists(exe) or not os.path.exists(path):
        return False
    try:
        r = subprocess.run(
            [exe, path, "--edit", f"track:a{int(track) + 1}",
             "--set", f"name={title}"],
            capture_output=True, text=True, timeout=600,
            creationflags=NO_WINDOW, startupinfo=hidden_si())
        return r.returncode == 0
    except Exception:                                    # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# WHEN A DISAGREEMENT IS SETTLED ENOUGH TO ACT ON
#
# "Nuarr does not touch these automatically" was the right rule when a
# disagreement had no number on it. It has one: three windows of audio, each
# with a probability, aggregated into a confidence. On this library the
# disagreements run from 0.60 to 0.99, and the two ends are not the same kind
# of claim - a Spanish drama tagged English at 0.99 is not a judgement call,
# and a 0.60 is exactly the shaky verdict that rule was written to protect.
#
# So it is a band, like every other decision nuarr makes. Above the line it
# can act on its own; below the lower line it is not worth showing; between
# them it is yours. The old absolutism survives as the DEFAULT - manual - so
# nothing changes until somebody moves the switch.
def mode() -> str:
    m = str(getattr(SETTINGS, "audiolang_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


def fix_at() -> int:
    try:
        return max(70, min(100, int(getattr(SETTINGS, "audiolang_fix_at", 95))))
    except Exception:                                            # noqa: BLE001
        return 95


def leave_at() -> int:
    """Never allowed to meet the other line: a band of zero width is one
    threshold wearing two names."""
    try:
        v = int(getattr(SETTINGS, "audiolang_leave_at", 60))
    except Exception:                                            # noqa: BLE001
        v = 60
    return max(0, min(fix_at() - 10, v))


def verdict_of(confidence: float) -> dict:
    """What auto WOULD do with this disagreement, whether or not auto is on."""
    pct = int(round(float(confidence or 0) * 100))
    if pct >= fix_at():
        return {"sure": pct, "auto": "act",
                "why": f"{pct}% is at or above the {fix_at()}% line"}
    if pct <= leave_at():
        return {"sure": pct, "auto": "leave",
                "why": f"{pct}% is at or below the {leave_at()}% line, so the "
                       f"tag is left alone and the row is not offered"}
    return {"sure": pct, "auto": "ask",
            "why": f"{pct}% sits between {leave_at()}% and {fix_at()}%, so "
                   f"this one is yours to call"}


AUTO_PER_PASS = 20
AUTO_STATE: dict = {"at": 0.0, "fixed": 0, "failed": 0, "queued": 0,
                    "runs": 0, "running": False, "now": "",
                    "done": 0, "total": 0, "t0": 0.0, "secs_each": 0.0,
                    "last_took": 0.0}


def auto_progress() -> dict:
    """The pass in flight, measured - not guessed.

    Everything here comes from the run that is happening: how many of how
    many, how long each has really taken, and therefore when it ends. Nothing
    is reported before there is a measurement to report, because a rate
    invented from one file is not a rate.
    """
    d = dict(AUTO_STATE)
    now = time.time()
    el = (now - d["t0"]) if (d["running"] and d["t0"]) else 0.0
    rate = (d["done"] / el) if (el > 0.5 and d["done"]) else 0.0
    left = max(0, (d.get("total") or 0) - (d.get("done") or 0))
    d["elapsed"] = round(el, 1)
    d["rate"] = round(rate, 3)
    d["eta"] = round(left / rate) if rate else 0
    d["secs_each"] = round(d.get("secs_each") or (1 / rate if rate else 0), 2)
    d["per_pass"] = AUTO_PER_PASS
    d["mode"] = mode()
    # WHEN IT RUNS AGAIN. Auto rides the listen pass, so the listener's clock
    # is its clock - one schedule, one next time.
    try:
        from . import schedules
        for r in (schedules.snapshot() or {}).get("rows", []):
            if r.get("key") == "audiolang":
                d["next_run"] = r.get("next_run") or 0.0
                d["cycle_s"] = r.get("every_s") or 1800
                break
    except Exception:                                            # noqa: BLE001
        pass
    # AND THE WHOLE QUEUE, not just this pass: how many are past the line and
    # how long the lot will take at the pace this machine really manages.
    try:
        waiting = sum(1 for r in mismatches(2000)
                      if verdict_for(r).get("auto") == "act")
    except Exception:                                            # noqa: BLE001
        waiting = 0
    d["waiting"] = waiting
    each = d["secs_each"] or 0.0
    d["backlog_eta"] = round(max(
        (waiting / max(1, AUTO_PER_PASS)) * float(d.get("cycle_s") or 1800),
        waiting * each if each else 0)) if waiting else 0
    return d


def auto_pass() -> dict:
    """Correct the disagreements past the line. Bounded, and gated by mode.

    EACH ONE IS A HEADER EDIT AND A REQUEUE, not a re-encode - but it is still
    a write to a file somebody may be watching, so a pass takes a bounded
    number and the rest wait. The order is most-confident first: if a pass is
    going to be interrupted, the ones it got through should be the ones least
    in doubt.
    """
    out = {"fixed": 0, "failed": 0, "queued": 0}
    if mode() != "auto":
        return out
    # verdict_for, not verdict_of: a show you have twice left alone is
    # never corrected on its own, however loud one episode of it is.
    rows = [r for r in mismatches(1000)
            if verdict_for(r).get("auto") == "act"]
    rows.sort(key=lambda r: -float(r.get("confidence") or 0))
    out["queued"] = max(0, len(rows) - AUTO_PER_PASS)
    batch = rows[:AUTO_PER_PASS]
    t0 = time.time()
    AUTO_STATE.update(running=True, now="", queued=out["queued"],
                      done=0, total=len(batch), t0=t0)
    try:
        for r in batch:
            AUTO_STATE["now"] = os.path.basename(r.get("path") or "")
            try:
                res = fix_mislabel(int(r["file_id"]), int(r["track"]))
            except Exception as e:                               # noqa: BLE001
                res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            if res.get("ok"):
                out["fixed"] += 1
            else:
                out["failed"] += 1
            AUTO_STATE["done"] = out["fixed"] + out["failed"]
    finally:
        took = max(0.001, time.time() - t0)
        n = max(1, AUTO_STATE.get("done") or 0)
        prev = AUTO_STATE.get("secs_each") or 0.0
        this = took / n
        AUTO_STATE.update(running=False, now="", at=time.time(),
                          fixed=out["fixed"], failed=out["failed"],
                          last_took=round(took, 1), t0=0.0,
                          secs_each=(this if not prev else prev * .7 + this * .3),
                          runs=(AUTO_STATE.get("runs") or 0) + 1)
    if out["fixed"] or out["failed"]:
        joblog.log(f"audio language: corrected {out['fixed']} tag(s) on its "
                   f"own at or above the {fix_at()}% line"
                   + (f", {out['failed']} could not be" if out["failed"] else "")
                   + (f" - {out['queued']} wait for the next pass"
                      if out["queued"] else ""), "info", system="audiolang")
    return out


def forget(file_ids) -> int:
    """Drop every verdict heard from bytes that are no longer there."""
    ids = [int(i) for i in (file_ids or []) if i]
    if not ids:
        return 0
    try:
        ensure_table()
        with cursor() as cur:
            qs = ",".join("?" * len(ids))
            cur.execute(f"DELETE FROM audio_lang WHERE file_id IN ({qs})", ids)
            return cur.rowcount or 0
    except Exception:                                            # noqa: BLE001
        return 0


def path_of(file_id: int) -> str:
    try:
        with cursor() as cur:
            r = cur.execute("SELECT path FROM files WHERE id=?",
                            (int(file_id),)).fetchone()
        return r["path"] if r else ""
    except Exception:                                            # noqa: BLE001
        return ""


def fix_mislabel(file_id: int, track: int) -> dict:
    r"""Correct a lying tag, then let the rules deal with what that reveals.

    THE SECOND STEP IS THE ONE THAT COSTS NOTHING. Once the tag says what the
    audio actually is, a file with two Japanese tracks is a file with two
    Japanese tracks - and `audio/dedupe` has been in the rule set the whole
    time, waiting for a file honest enough to trip it. So this writes one
    header and requeues; nothing here knows how to drop a track, and nothing
    here should.

    The blocklist is deliberately NOT done here. Retagging is safe and always
    an improvement; asking the arr for a different release is a decision with a
    cost, and it belongs to the remedy layer next to every other one.
    """
    from . import langkey
    m = [x for x in mismatches(2000, floor=0.0, respect_answers=False)
         if x["file_id"] == int(file_id) and x["track"] == int(track)]
    if not m:
        # ALREADY DONE IS NOT A FAILURE. The row on the page can be a minute
        # old, and pressing a second time should say the work is behind you -
        # in the words for it, and with the row leaving.
        lies = title_lies(int(file_id))
        if lies:
            fixed = []
            for t in lies:
                if _set_track_title(path_of(int(file_id)), t["track"], t["want"]):
                    fixed.append(f"track {t['track']}: "
                                 f"{t['title']!r} -> {t['want']!r}")
            if fixed:
                _reprobe_quiet(int(file_id), path_of(int(file_id)))
                return {"ok": True, "gone": True,
                        "why": "the tag was already right; corrected the title "
                               "it still carried - " + "; ".join(fixed)}
        return {"ok": True, "gone": True,
                "why": "the tag and the audio already agree - nothing left "
                       "to correct on this track"}
    x = m[0]
    key = fix_begin(int(file_id), int(track), ("open", "tag", "title", "reread"))
    with cursor() as cur:
        r = cur.execute("SELECT path FROM files WHERE id=?",
                        (int(file_id),)).fetchone()
    if not r:
        fix_end(key, False, "no such file")
        return {"ok": False, "why": "no such file"}
    path = r["path"]
    if not can_fast_path(path):
        fix_end(key, False, "this container cannot be edited in place")
        return {"ok": False, "why": "this container cannot be edited in place"}
    fix_step(key, "tag")
    ok, why = apply_and_restamp(int(file_id), path, {int(track): x["heard"]})
    if not ok:
        fix_end(key, False, why)
        return {"ok": False, "why": why}
    # AND THE THIRD LIE, WHICH NOBODY OWNED. Correcting the language left the
    # file reading `lang=jpn title=English` - a track that now says the right
    # thing in the field the rules read and the wrong thing in the field a
    # viewer reads. The audio-title check does not catch it either: it looks
    # for titles naming a CODEC the file does not have, not a language.
    #
    # ONLY THE LANGUAGE WORD, WHEREVER IT SITS. The first version rewrote the
    # title only when it was exactly the old language's name, which left
    # "English E-AC3 5.1" on a track now tagged jpn - the codec and the
    # channels are true and the one word that matters is still a lie. So the
    # word is replaced in place and everything around it kept: "English E-AC3
    # 5.1" becomes "Japanese E-AC3 5.1". Whole words only, so "Englishman"
    # survives, and if the name is not in there nothing is touched - a release
    # that called it "Commentary" is saying something this has no business
    # rewriting.
    retitled = ""
    fix_step(key, "title")
    try:
        old_name = _name_of(x["tagged"])
        new_name = _name_of(x["heard"])
        cur_title = (_track_title(path, int(track)) or "").strip()
        if old_name and new_name and cur_title:
            want = re.sub(rf"\b{re.escape(old_name)}\b", new_name, cur_title,
                          flags=re.IGNORECASE)
            if want != cur_title and _set_track_title(path, int(track), want):
                retitled = (f", and its title from {cur_title!r} to {want!r}")
    except Exception:                                    # noqa: BLE001
        pass
    fix_step(key, "reread")
    _reprobe_quiet(int(file_id), path)
    try:
        from . import joblog
        joblog.log(f"audio language: track {track} of "
                   f"{os.path.basename(path)} said {x['tagged']} and is "
                   f"{x['heard']} - tag corrected", "warn", system="audiolang")
    except Exception:                                    # noqa: BLE001
        pass
    _ = langkey
    fix_end(key, True, f"now tagged {x['heard']}")
    return {"ok": True, "tagged": x["tagged"], "heard": x["heard"],
            "fake_dual": x["fake_dual"], "path": path, "retitled": retitled,
            "why": f"tag corrected to {x['heard']}" + retitled
                   + (" - the duplicate track will be dropped on the next "
                      "rebuild" if x["fake_dual"] else "")}


def attention() -> dict | None:
    r"""What the Attention tile should say, or nothing.

    A LIE IS WORTH RAISING WHEN IT IS STILL YOURS. Correcting a tag is safe;
    what follows - a release blocklisted and re-searched because it claimed
    dual audio and shipped one language twice - is a decision, and a decision
    belongs on the tile that means "this needs you".

    In auto the confident ones are no longer yours: they are queued and will
    be corrected on the next pass, so counting them here would be the tile
    asking for a decision that has already been made. What stays is the band
    between the lines, which is exactly the part auto will not touch.
    """
    try:
        m = mismatches(500)
        if mode() == "auto":
            m = [x for x in m
                 if verdict_for(x).get("auto") == "ask"]
    except Exception:                                    # noqa: BLE001
        return None
    if not m:
        return None
    fake = [x for x in m if x["fake_dual"]]
    if fake:
        return {"what": "mislabelled audio", "n": len(fake),
                "note": "claim dual audio and carry one language twice",
                "goto": "/settings#alang"}
    return {"what": "mislabelled audio", "n": len(m),
            "note": "a track is tagged a language it is not",
            "goto": "/settings#alang"}


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
                with joblog.section("Audio language listen"):
                    await asyncio.to_thread(run_once)
            # AND THEN ACT ON WHAT IT HEARD. Listening and correcting are the
            # same job here - unlike the subtitle readers, where reading is
            # half an hour and acting is seconds, a mislabel is only found by
            # listening and there is nothing to act on until a pass has run.
            if mode() == "auto":
                await asyncio.to_thread(auto_pass)
        except Exception as e:                           # noqa: BLE001
            PROGRESS.update(state="error", error=f"{type(e).__name__}: {e}")
            joblog.log(f"audio language: {type(e).__name__}: {e}", "warn", system="audiolang")
        await asyncio.sleep(1800)


def run_once(limit: int = 400, apply: bool = True,
             verify: bool = True) -> dict:
    r"""One pass: listen to what needs listening to, and write what was heard.

    TWO POPULATIONS, IN THIS ORDER. Untagged tracks first, because a missing
    language actively breaks the planner's rules and is the smaller pile. Then
    tagged tracks nobody has verified, which is where the lies live and is
    twenty thousand files deep - so it is only ever the remainder of a pass,
    never allowed to starve the gaps.
    """
    from . import joblog
    pending_invalidate()
    # THREE POPULATIONS NOW, AND FRESH BEATS EVERYTHING. A file that landed an
    # hour ago is the one somebody is about to watch and the one whose release
    # can still usefully be blocklisted; a file from March has waited this long
    # and can wait for the next pass.
    todo = queued(limit)
    jumped = len(todo)
    if len(todo) < limit:
        todo += pending(limit - len(todo))
    gaps = len(todo)
    if verify and len(todo) < limit:
        todo += unverified(limit - len(todo))
    PROGRESS.update(state="scanning", done=0, total=len(todo), current="",
                    started_at=time.time(), finished_at=0.0, found=0,
                    applied=0, refused=0, error="")
    if not todo:
        PROGRESS.update(state="idle", finished_at=time.time())
        return {"checked": 0, "found": 0, "applied": 0}

    joblog.log(f"audio language: {len(todo)} track(s) to listen to "
               f"({jumped} just landed, {gaps - jumped} with no tag, "
               f"{len(todo) - gaps} to verify)", "info", system="audiolang")
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
            # WRITING A TAG AND CORRECTING ONE ARE NOT THE SAME ACT. Filling a
            # blank is safe and always right. Overwriting a tag that a human or
            # a release group chose is a correction, it can be wrong, and it
            # belongs to the remedy that also drops the duplicate track and
            # asks for a better release - not to a sweep quietly editing
            # headers. So a verify hit is recorded and surfaced; only a gap is
            # filled here.
            if not t.get("tagged"):
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
    # Whatever was asked for has now been answered, right or wrong - leaving
    # it queued would mean listening to the same file every pass forever.
    unqueue({t["file_id"] for t in todo if t.get("jumped")})
    # AND THE ONES THAT WERE NEVER HANDED OVER. See queue_sync: a row queued()
    # filtered out never reached the line above, so the pass that should have
    # cleared it did not know it existed.
    _QSYNC["at"] = 0.0
    queue_sync()
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
                           creationflags=NO_WINDOW,
            startupinfo=hidden_si())
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
