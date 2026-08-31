"""
nuarr - live system load

Shown next to the workers because the two numbers that decide whether to add
another worker are the GPU encoder load and the pool disk throughput - not the
job count. An idle NVENC engine with four "running" jobs means they are all
stream copies and the GPU is free; a pegged encoder means adding workers will
only make each job slower.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

import psutil

from .config import NO_WINDOW, SETTINGS, hidden_si

_GPU_CACHE: tuple[float, dict] = (0.0, {})
_GPU_TTL = 2.0          # nvidia-smi costs ~100ms; do not run it per poll

# The latest sample, refreshed on a fixed cadence by sampler(). Reading this is
# free, so any number of pollers can look at it without cost or interference.
_LATEST: dict = {}
SAMPLE_S = 1.0


def _gpu() -> dict:
    """GPU load via nvidia-smi. Encoder utilisation is the one that matters."""
    global _GPU_CACHE
    now = time.time()
    if now - _GPU_CACHE[0] < _GPU_TTL:
        return _GPU_CACHE[1]

    out: dict = {}
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            # DECODER TOO. ffmpeg runs with -hwaccel cuda, so a re-encode uses
            # three separate engines: NVDEC to decode, the SMs for any filter
            # work, NVENC to encode. Reporting only two of them left the third
            # invisible - and it is the one that explains why a burn-in job is
            # slower than a plain encode.
            r = subprocess.run(
                [exe, "--query-gpu=name,utilization.gpu,utilization.encoder,"
                      "memory.used,memory.total,temperature.gpu,"
                      "utilization.decoder",
                 "--format=csv,noheader,nounits"],
                capture_output=True, timeout=8, creationflags=NO_WINDOW,
                startupinfo=hidden_si())
            line = r.stdout.decode("utf-8", "replace").strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            out = {
                "name": parts[0],
                # NOTE ON WHICH NUMBER MEANS WHAT.
                # gpu_pct is utilization.gpu - the SM/graphics cores. On this
                # workload it sits around 35-40% while NVENC is at 99%, because
                # the encode happens on a dedicated engine the SM figure does
                # not cover. Read alone it says "plenty of headroom" when there
                # is none, so the panel leads with the encoder.
                "gpu_pct": float(parts[1]),
                "encoder_pct": float(parts[2]),
                "vram_used_mb": float(parts[3]),
                "vram_total_mb": float(parts[4]),
                "temp_c": float(parts[5]),
                "decoder_pct": float(parts[6]) if len(parts) > 6 else None,
            }
        except Exception:
            out = {}
        # WHICH PROCESSES ARE ACTUALLY ON THE GPU, and how much VRAM each holds.
        #
        # The utilisation figures above say the card is busy; they cannot say
        # whether that is nuarr's ffmpeg or Plex transcoding for a viewer. On a
        # box where both compete for one NVENC engine that is the whole
        # question - "encoder 99%" reads as "nuarr is saturating it" when half
        # the time it is Plex, and the two call for opposite responses.
        #
        # Keyed by pid so the process panel can put the figure on the row it
        # belongs to rather than in a separate list nobody joins up.
        if exe:
            try:
                r = subprocess.run(
                    [exe, "--query-compute-apps=pid,used_memory",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, timeout=8, creationflags=NO_WINDOW,
                startupinfo=hidden_si())
                apps: list[dict] = []
                per_proc_vram = False
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    bits = [b.strip() for b in line.split(",")]
                    if len(bits) < 2 or not bits[0].isdigit():
                        continue
                    # "[N/A]" IS THE NORMAL ANSWER ON WINDOWS, not a parse
                    # error. Under WDDM the driver does not attribute video
                    # memory per process, so every row comes back as
                    # "7888, [N/A]" - and float() threw on all twenty of them,
                    # leaving the list empty. The panel then said "nothing is
                    # using the GPU" while NVENC sat at 81%.
                    try:
                        pid, vram = int(bits[0]), float(bits[1])
                        per_proc_vram = True
                    except ValueError:
                        pid, vram = int(bits[0]), None
                    # nvidia-smi reports a pid and nothing else useful. The
                    # name is what makes the row readable - "Plex
                    # Transcoder.exe 412 MB" answers the question; "12848
                    # 412 MB" does not.
                    try:
                        name = psutil.Process(pid).name()
                    except Exception:
                        name = f"pid {pid}"
                    apps.append({"pid": pid, "name": name, "vram_mb": vram})
                apps.sort(key=lambda a: -(a["vram_mb"] or 0))
                out["procs"] = apps
                # Whether the VRAM column means anything. Without it the list
                # is every process holding a graphics context - dwm.exe,
                # explorer.exe, two Chromes - which says nothing about who is
                # encoding, and presenting it as "using the GPU" would be
                # worse than saying the driver cannot tell us.
                out["per_proc_vram"] = per_proc_vram
            except Exception:
                # An older driver, or a card that does not report compute apps.
                # The utilisation figures are still worth having on their own.
                out.setdefault("procs", [])
                out.setdefault("per_proc_vram", False)
    _GPU_CACHE = (now, out)
    return out


# --- nuarr's own footprint ------------------------------------------------
# psutil.Process.cpu_percent(interval=None) reports load SINCE THAT OBJECT'S
# LAST CALL, so the objects have to be kept alive between samples. Rebuilding
# them each time returns 0.0 forever - the classic way this measurement silently
# reads zero. Keyed by pid, pruned as children exit.
_PROCS: dict[int, psutil.Process] = {}


# Cached per pid. A command line never changes for a living process, so this
# is read once and kept for as long as the pid is - the sampler runs every
# second and cmdline() is a real syscall.
_CMD: dict[int, list] = {}


def _cmdline(p) -> list:
    if p.pid not in _CMD:
        try:
            _CMD[p.pid] = p.cmdline() or []
        except Exception:                                # noqa: BLE001
            _CMD[p.pid] = []
    return _CMD[p.pid]


def _input_name(cmd: list) -> str:
    r"""The file an ffmpeg/ffprobe invocation is working on, said plainly.

    Taken from the argument after -i, or the last path-looking argument, then
    reduced to a title: the full pool path is 120 characters of
    P:\Anime Shows\..., and the filename after it is another 90 of release
    tags. "Leverage - Redemption - S03E08" is the part anyone reads.
    """
    raw = ""
    try:
        for i, a in enumerate(cmd):
            if a == "-i" and i + 1 < len(cmd):
                raw = cmd[i + 1]
                break
        if not raw:
            for a in reversed(cmd):
                if len(a) > 3 and (":\\" in a or a.startswith("\\\\")):
                    raw = a
                    break
    except Exception:                                    # noqa: BLE001
        return ""
    if not raw:
        return ""
    try:
        from .db import pretty_from_filename
        return pretty_from_filename(raw)
    except Exception:                                    # noqa: BLE001
        return os.path.basename(raw)


def _owner(name: str, cmd: list) -> tuple[str, str]:
    r"""WHICH NUARR ACTIVITY owns this process, and what it is doing.

    "ffprobe.exe" and "conhost.exe" are true and useless: they say a process
    exists, not what nuarr is doing with it. Everything nuarr spawns is
    distinguishable from its arguments, so the panel can name the actual work -
    a transcode of a named episode, a language sample, a library probe.

    Returns (activity, detail). An empty activity means "not recognised", which
    is itself worth seeing: nuarr should not be spawning things it cannot name.
    """
    low = (name or "").lower()
    joined = " ".join(cmd or [])
    jl = joined.lower()
    what = _input_name(cmd)

    if low.startswith("ffmpeg"):
        # The audio-language sampler is the only ffmpeg that decodes raw mono
        # f32 at 16 kHz - that is Whisper's input format, nothing else asks
        # for it.
        if "f32le" in jl and "16000" in jl:
            return "Audio language", (f"listening to {what}" if what
                                      else "listening to a sample")
        if "-progress" in jl:
            return "Transcode", (f"encoding {what}" if what else "encoding")
        return "ffmpeg", what
    if low.startswith("ffprobe"):
        if "format=duration" in jl:
            return "Audio language", (f"measuring {what}" if what else "measuring")
        return "Probe", (f"inspecting {what}" if what else "inspecting a file")
    if low.startswith("mkvpropedit"):
        return "Audio language", (f"writing the language tag on {what}"
                                  if what else "writing a language tag")
    if low.startswith("mkvmerge"):
        return "Subtitles", (f"embedding into {what}" if what else "embedding")
    if low.startswith(("powershell", "pwsh")):
        # Handler scripts are passed by path; name the script, not "powershell".
        m = re.search(r"([\w.-]+\.ps1)", joined, re.I)
        return "Handler script", (m.group(1) if m else "running a handler")
    if "tesseract" in low:
        return "Subtitle OCR", (f"reading {what}" if what else "reading subtitles")
    # THE OTHER OCR ENGINE, which was invisible here.
    #
    # This function names processes by their EXECUTABLE, and Tesseract is an
    # exe - so it got a row. PaddleOCR is a Python module driven by a worker
    # script, so the process is "python.exe" and it fell through to "not
    # recognised": a CPU-bound PaddleOCR run showed up in the total for
    # "nuarr and its children" but was named nowhere, which is exactly the
    # confusing half-answer the panel exists to avoid. Match on the script
    # nuarr launches, since that is what identifies the work.
    if "paddle_worker.py" in jl:
        dev = ("GPU" if "--device gpu" in jl
               else "CPU" if "--device cpu" in jl else "")
        eng = "Tesseract" if "--engine tesseract" in jl else "PaddleOCR"
        lead = f"{eng}{' on the ' + dev if dev else ''}"
        return "Subtitle OCR", (f"{lead} reading {what}" if what
                                else f"{lead} reading subtitles")
    # The Tesseract path runs through a wrapper that hides its console; the
    # wrapper itself is the process the panel sees while pgsrip works.
    if "pgsrip_hidden.py" in jl or "pgsrip" in jl:
        return "Subtitle OCR", (f"Tesseract reading {what}" if what
                                else "Tesseract reading subtitles")
    if low.startswith("conhost"):
        return "", "console host for another process"
    return "", ""


def _self_usage() -> dict:
    """CPU and RAM for nuarr AS A WHOLE - the server plus everything it spawns.

    The server process alone is misleading: during a transcode nearly all the
    cost is in a child ffmpeg, and the handlers run PowerShell. Reporting only
    the parent would show a near-idle number while the box is flat out.

    RAM is summed as RSS across the tree. That over-counts shared pages in
    principle, but the children here are separate executables sharing very
    little, so it tracks what Task Manager shows closely enough to be useful.
    """
    try:
        me = psutil.Process(os.getpid())
    except psutil.Error:
        return {}

    try:
        tree = [me] + me.children(recursive=True)
    except psutil.Error:
        tree = [me]

    alive = set()
    cpu = 0.0
    rss = 0
    # NAME EVERY PROCESS, not just count them. "11 proc" told you the number and
    # nothing else - whether that was eleven ffmpegs or one ffmpeg and ten stuck
    # PowerShell handlers, which are very different situations and the second one
    # is a bug. psutil caches name() and create_time() on the Process object, and
    # those objects live in _PROCS between samples, so this costs one syscall per
    # NEW pid rather than one per sample.
    procs: list[dict] = []
    for p in tree:
        alive.add(p.pid)
        tracked = _PROCS.get(p.pid)
        if tracked is None:
            _PROCS[p.pid] = p
            try:
                p.cpu_percent(None)          # prime; this call always yields 0.0
            except psutil.Error:
                pass
            tracked = p
        try:
            c = tracked.cpu_percent(None)
            m = tracked.memory_info().rss
            cpu += c
            rss += m
        except psutil.Error:
            continue                          # died mid-sample; drop it below
        try:
            name = tracked.name()
        except psutil.Error:
            name = "?"
        try:
            age = max(0.0, time.time() - tracked.create_time())
        except psutil.Error:
            age = 0.0
        act, detail = ("nuarr", "the server") if p.pid == me.pid \
            else _owner(name, _cmdline(tracked))
        try:
            ppid = tracked.ppid()
        except psutil.Error:
            ppid = 0
        procs.append({
            "pid": p.pid,
            "ppid": ppid,
            "name": name,
            "activity": act,
            "detail": detail,
            "rss_mb": round(m / 1024 ** 2, 1),
            "cpu_pct": round(c, 1),
            "age_s": round(age),
            "self": p.pid == me.pid,
        })

    # A conhost belongs to the process it is hosting. Attributing it there
    # turns "conhost.exe — ?" into "console for Transcode", which is the
    # difference between a mystery row and a footnote.
    by_pid = {x["pid"]: x for x in procs}
    for x in procs:
        if x["activity"] or not x["ppid"]:
            continue
        parent = by_pid.get(x["ppid"])
        if parent and parent["activity"] and not parent["self"]:
            x["activity"] = parent["activity"]
            x["detail"] = f"console for {parent['activity'].lower()}"

    for pid in [p for p in _PROCS if p not in alive]:
        _PROCS.pop(pid, None)
        _CMD.pop(pid, None)

    # Heaviest first: the one worth looking at is the one using the memory.
    procs.sort(key=lambda x: (not x["self"], -x["rss_mb"]))

    cores = psutil.cpu_count(logical=True) or 1
    return {
        # raw psutil percent is per-core and can exceed 100 on a 20-thread box;
        # normalise so the bar reads as a share of the whole machine
        "cpu_pct": round(min(cpu / cores, 100.0), 1),
        "cpu_pct_raw": round(cpu, 1),
        "ram_mb": round(rss / 1024 ** 2, 1),
        "procs": len(alive),
        "children": max(0, len(alive) - 1),
        "proc_list": procs,
    }


# The cache disk, resolved once. E:\nuarr-cache is the busiest volume in the
# system - every encode writes its output there and every commit reads it back
# - and it was the one number on the bar with no throughput beside it.
#
# psutil keys its per-disk counters by PHYSICAL drive on Windows
# (PhysicalDrive14), not by letter, so the letter has to be mapped once. That
# costs a PowerShell call, hence once at first use and then cached: the cache
# directory does not move while the process is running.
_CACHE_DISK: str | None = None
_CACHE_DISK_DONE = False
_CACHE_IO: tuple[float, int, int] = (0.0, 0, 0)


def _cache_disk_key() -> str | None:
    global _CACHE_DISK, _CACHE_DISK_DONE
    if _CACHE_DISK_DONE:
        return _CACHE_DISK
    _CACHE_DISK_DONE = True
    try:
        letter = os.path.splitdrive(os.path.abspath(SETTINGS.cache_dir))[0]
        letter = letter.rstrip(":")
        if not letter:
            return None
        # Ask WMI directly rather than through a shell - diskload already
        # holds the partition map and the in-process query that builds it,
        # so this is a lookup rather than another process.
        from . import diskload as _dl
        n = _dl.disk_number_for(f"{letter}:\\")
        if not n:
            rows = _dl.wmi_query(
                f"SELECT DiskNumber FROM MSFT_Partition WHERE DriveLetter='{letter}'",
                "root\\Microsoft\\Windows\\Storage")
            if rows:
                try:
                    n = str(int(rows[0].DiskNumber))
                except (TypeError, ValueError):
                    n = ""
        if n.isdigit():
            _CACHE_DISK = f"PhysicalDrive{n}"
    except Exception:
        _CACHE_DISK = None
    return _CACHE_DISK


def _cache_io() -> dict:
    """Read/write throughput on the cache volume, as a rate.

    Counters are cumulative, so this differences them against the previous
    sample. The sampler runs on a fixed 1 s cadence, which is what makes the
    result a rate rather than an artefact of when someone last looked.
    """
    global _CACHE_IO
    key = _cache_disk_key()
    if not key:
        return {}
    try:
        c = psutil.disk_io_counters(perdisk=True).get(key)
    except Exception:
        return {}
    if not c:
        return {}
    now = time.time()
    prev_t, prev_r, prev_w = _CACHE_IO
    _CACHE_IO = (now, c.read_bytes, c.write_bytes)
    dt = now - prev_t
    if not prev_t or dt <= 0 or dt > 30:
        return {}                       # first sample, or a long stall
    return {
        "read_bps": max(0.0, (c.read_bytes - prev_r) / dt),
        "write_bps": max(0.0, (c.write_bytes - prev_w) / dt),
        "disk": key,
    }


# WHAT ELSE IS ON THIS BOX.
#
# nuarr's own figures only answer half the question. "24% cpu" means nothing
# without knowing whether the machine is at 30% or at 95%, and if it is at 95%
# the useful next question is who has the rest - Plex serving a transcode, an
# arr mid-import, SABnzbd unpacking. Without that the header can say nuarr is
# behaving while the box is on its knees.
#
# Enumerating every process costs real time, so this runs on its own slower
# clock than the 1 s sampler and hands back a cached answer in between.
_TOP_CACHE: tuple[float, list] = (0.0, [])
_TOP_TTL = 6.0
_ALL_PROCS: dict[int, psutil.Process] = {}


def _top_external(own: set[int], limit: int = 6) -> list[dict]:
    """Heaviest processes that are NOT part of nuarr's own tree."""
    global _TOP_CACHE
    now = time.time()
    if now - _TOP_CACHE[0] < _TOP_TTL:
        return _TOP_CACHE[1]

    cores = psutil.cpu_count(logical=True) or 1
    rows: list[dict] = []
    live = set()
    try:
        for p in psutil.process_iter(["pid", "name"]):
            pid = p.info["pid"]
            live.add(pid)
            if pid in own or pid == 0:
                continue
            tracked = _ALL_PROCS.get(pid)
            if tracked is None:
                _ALL_PROCS[pid] = p
                try:
                    p.cpu_percent(None)      # prime; always 0.0 on first call
                except psutil.Error:
                    pass
                continue                     # no reading until the next pass
            try:
                cpu = tracked.cpu_percent(None) / cores
                rss = tracked.memory_info().rss
            except psutil.Error:
                continue
            rows.append({"pid": pid, "name": p.info.get("name") or "?",
                         "cpu_pct": round(cpu, 1),
                         "ram_mb": round(rss / 1024 ** 2, 1)})
    except Exception:
        pass
    for pid in [q for q in _ALL_PROCS if q not in live]:
        _ALL_PROCS.pop(pid, None)

    # Group by NAME. Chrome and Plex both run a dozen processes each; a list of
    # twelve chrome.exe rows at 2% is noise, one at 24% is the answer.
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["name"], {"name": r["name"], "cpu_pct": 0.0,
                                       "ram_mb": 0.0, "n": 0})
        a["cpu_pct"] += r["cpu_pct"]
        a["ram_mb"] += r["ram_mb"]
        a["n"] += 1
    out = sorted(agg.values(), key=lambda x: -x["ram_mb"])[:limit * 2]
    for a in out:
        a["cpu_pct"] = round(a["cpu_pct"], 1)
        a["ram_mb"] = round(a["ram_mb"], 1)
    _TOP_CACHE = (now, out)
    return out


def _sample() -> dict:
    me = _self_usage()
    vm = psutil.virtual_memory()
    # interval=None returns the load SINCE THE LAST CALL. That makes the caller
    # the clock - and there were two of them (/api/jobs and /api/system), each
    # resetting the other's window, so the CPU figure was averaged over an
    # unpredictable period and jittered accordingly. Only the sampler calls
    # this now, on a fixed cadence, so the number means something.
    cpu = psutil.cpu_percent(interval=None)

    cache_free_gb = 0.0
    try:
        cache_free_gb = shutil.disk_usage(SETTINGS.cache_dir).free / 1024 ** 3
    except OSError:
        pass

    return {
        "cpu_pct": round(cpu, 1),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_used_gb": round((vm.total - vm.available) / 1024 ** 3, 1),
        "ram_total_gb": round(vm.total / 1024 ** 3, 1),
        "ram_pct": round(vm.percent, 1),
        "cache_free_gb": round(cache_free_gb, 1),
        "cache_io": _cache_io(),
        "gpu": _gpu(),
        "nuarr": me,
        # Everything on the box that is NOT nuarr, so the header can answer
        # "is it us or is it them".
        "others": _top_external({p["pid"] for p in me.get("proc_list", [])}),
        "at": time.time(),
    }


def snapshot() -> dict:
    """The most recent sample. Never blocks, never runs a subprocess.

    Falls back to sampling inline only if the background task has not produced
    anything yet - otherwise a dashboard load before startup finishes would
    show empty gauges.
    """
    out = dict(_LATEST or _sample())
    out["gpu_work"] = _gpu_work()
    return out


def _gpu_work() -> list[dict]:
    """What nuarr is running on the GPU right now, besides encodes.

    The panel already counts encode jobs, and for years that was the whole
    answer - then subtitle OCR (PaddleOCR) and audio-language listening
    (Whisper) arrived, both of which run on the CUDA cores and neither of
    which is an encode. Without this list the panel watched nuarr's own OCR
    load and attributed it to Plex. Cheap by construction: reads in-memory
    state only, no subprocess, because snapshot() serves every dashboard poll.
    """
    work: list[dict] = []
    try:
        from . import subocr
        if subocr.engine() == "paddle":
            from . import jobs as _jobs
            n = sum(1 for w in _jobs.RUNNING.values()
                    if getattr(getattr(w, "job", None), "kind", "") == "sub_ocr"
                    or getattr(w, "sub_ocr_active", False))
            if n:
                cached = subocr._PADDLE_CACHE.get("data")
                # An empty cache is "not asked yet", not "no CUDA" - claiming
                # (CPU) on a GPU build right after boot would be invented.
                dev = ("" if cached is None
                       else " (GPU)" if cached.get("cuda") else " (CPU)")
                work.append({"kind": "subocr",
                             "label": f"subtitle OCR — PaddleOCR{dev}",
                             "n": n})
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import audiolang
        p = audiolang.progress()
        if (p.get("state") or "") == "listening":
            work.append({"kind": "whisper",
                         "label": "audio language — Whisper",
                         "n": 1,
                         "detail": (p.get("current") or "")[:60]})
        elif audiolang._MODEL is not None:
            work.append({"kind": "whisper",
                         "label": "Whisper model loaded (idle, holding VRAM)",
                         "n": 0})
    except Exception:                                    # noqa: BLE001
        pass
    return work


async def sampler() -> None:
    """Refresh the load figures on a fixed 1 s cadence.

    Previously these rode along with /api/jobs, which polls every 2 s and can
    itself take 0.5-2.6 s under load - so the bar updated raggedly and stalled
    exactly when the machine was busiest, which is when you are watching it.
    Sampling on its own clock decouples the two: the readings stay smooth no
    matter how slow a job query is, and nvidia-smi never runs inside a request.
    """
    import asyncio

    global _LATEST
    while True:
        try:
            _LATEST = await asyncio.to_thread(_sample)
        except Exception:
            pass
        await asyncio.sleep(SAMPLE_S)


# Prime the CPU counter so the first real reading is not 0.0
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass
