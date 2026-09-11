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

# ---------------------------------------------------------------- history ---
#
# A GRAPH THAT STARTS EMPTY ANSWERS NOTHING. The sampler has always kept
# exactly one reading - the latest - which is all a gauge needs and useless to
# a line. Opening a page and being told to wait a minute before it can show
# you what the last minute looked like is the wrong way round: the minute has
# already happened, and the cost of remembering it is a handful of floats a
# second.
#
# Fifteen minutes at one second, which is long enough to cover a whole encode
# commit or a sidecar remux and short enough that the whole ring is smaller
# than one of the JSON payloads that carries it.
#
# NUARR'S OWN NUMBERS, BESIDE THE MACHINE'S. Both, deliberately: "nuarr is at
# 6%" and "the box is at 97%" are different facts and the interesting thing is
# usually the gap between them - see the CPU gate, which had to learn the same
# lesson.
HIST_MAX = 900
_HIST: list = []


def _hist_push(s: dict) -> None:
    me = s.get("nuarr") or {}
    g = s.get("gpu") or {}
    io_ = s.get("cache_io") or {}
    row = {
        "at": round(float(s.get("at") or time.time()), 1),
        "cpu": float(me.get("cpu_pct") or 0.0),
        "cpu_all": float(s.get("cpu_pct") or 0.0),
        "ram_mb": float(me.get("ram_mb") or 0.0),
        "ram_all_pct": float(s.get("ram_pct") or 0.0),
        "procs": int(me.get("procs") or 0),
        "gpu": float(g.get("gpu_pct") or 0.0),
        "enc": float(g.get("encoder_pct") or 0.0),
        "dec": float(g.get("decoder_pct") or 0.0),
        "gpu_mem": float(g.get("mem_used") or 0.0),
        # THE CACHE VOLUME, which is where every encode and every remux
        # stages its output, so it is the one disk whose figure is almost
        # entirely nuarr's doing.
        "cache_read": float(io_.get("read_bps") or 0.0),
        "cache_write": float(io_.get("write_bps") or 0.0),
        # AND NUARR'S OWN BYTES, SUMMED FROM ITS OWN PROCESSES. Not the disk
        # counters minus a guess - the actual per-process totals this sample
        # just differenced, which is the only figure here that cannot include
        # somebody else's work by construction.
        "read": float(sum(p.get("read_bps") or 0
                          for p in (me.get("proc_list") or []))),
        "write": float(sum(p.get("write_bps") or 0
                           for p in (me.get("proc_list") or []))),
    }
    _HIST.append(row)
    if len(_HIST) > HIST_MAX:
        del _HIST[:len(_HIST) - HIST_MAX]


def history(n: int = 0) -> dict:
    """The ring, oldest first. `n` trims to the most recent n samples."""
    rows = _HIST[-int(n):] if n else list(_HIST)
    return {"rows": rows, "every_s": SAMPLE_S, "max": HIST_MAX,
            "cores": psutil.cpu_count(logical=True) or 1}


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


# pid -> (at, read_bytes, write_bytes) for the rate above.
_PIO: dict = {}

# WHICH ENGINE A PROCESS IS ON, WITHOUT INVENTING A PERCENTAGE.
#
# Windows does not let nvidia-smi answer this. Checked on this box against
# the RTX A5000: --query-compute-apps=used_gpu_memory returns [N/A] for every
# process, and `nvidia-smi pmon` prints a dash under sm, mem, enc and dec for
# all of them. That is WDDM, not a missing flag - the numbers are simply not
# exposed per process, and no amount of asking differently will produce them.
#
# So the column does not pretend. What CAN be known exactly is which engine a
# process was launched to use, because nuarr wrote the command line: an
# encode asking for h264_nvenc is on NVENC by definition, and one with
# -hwaccel cuda is decoding on NVDEC. That is a fact about the process rather
# than a measurement of it, and it is the fact people actually want from this
# column - "is this one on the card or not". The card's own engine
# percentages stay on the Graphics chart, where being whole-card figures is
# correct rather than misleading.
_GPU_ARG = (
    ("nvenc",   "NVENC"),          # encoding on the dedicated encoder
    ("cuvid",   "NVDEC"),          # decoding on the dedicated decoder
    ("nvdec",   "NVDEC"),
    ("hwaccel cuda", "CUDA"),
    ("-hwaccel_device", "CUDA"),
    ("cuda",    "CUDA"),
)


def _gpu_of(cmd: list) -> str:
    """Which GPU engine this command was asked to use, from its own arguments."""
    jl = " ".join(cmd or []).lower()
    if not jl:
        return ""
    for needle, label in _GPU_ARG:
        if needle in jl:
            return label
    return ""


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
            # COPYING IS NOT ENCODING, and the GPU column made that visible:
            # a job labelled "encoding" with nothing on the card and -c:v copy
            # on its command line is a repack, which is the cheapest thing the
            # queue does and reads as the most expensive.
            if "-c:v copy" in jl or "-c:v:0 copy" in jl:
                return "Repack", (f"copying {what}" if what else "copying")
            return "Transcode", (f"encoding {what}" if what else "encoding")
        # THE DECODE CHECK, WHICH HAD BEEN ARRIVING AS "ffmpeg". Nothing else
        # nuarr runs decodes to nowhere: -f null with -xerror is the integrity
        # sweep reading both ends of a file to see whether the bytes are good.
        # A panel whose whole point is naming the work should not have two
        # rows on it called after the executable.
        if "-f null" in jl and "-xerror" in jl:
            return "Does it decode?", (f"reading both ends of {what}" if what
                                       else "reading both ends of a file")
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
        _cmd = _cmdline(tracked)
        act, detail = ("nuarr", "the server") if p.pid == me.pid \
            else _owner(name, _cmd)
        # THE SERVER'S OWN GPU WORK IS NOT ON A COMMAND LINE. Whisper and
        # PaddleOCR load into this process, so there is no child to inspect -
        # the engines themselves report which device they came up on, and
        # _gpu_work already collects that for the GPU panel.
        if p.pid == me.pid:
            try:
                _on = [w for w in _gpu_work() if w.get("device") == "gpu"]
            except Exception:                                # noqa: BLE001
                _on = []
            gpu_use = ", ".join(sorted({("OCR" if w.get("kind") == "subocr"
                                         else "Whisper") for w in _on}))
        else:
            gpu_use = _gpu_of(_cmd)
        try:
            ppid = tracked.ppid()
        except psutil.Error:
            ppid = 0
        # PER-PROCESS I/O, DIFFERENCED HERE BECAUSE THIS IS THE ONLY PLACE
        # WITH A CLOCK. io_counters is a running total; a rate needs the
        # previous total and the gap, and the sampler is the one thing that
        # visits on a fixed cadence.
        rd = wr = 0.0
        try:
            ioc = tracked.io_counters()
            prev = _PIO.get(p.pid)
            nowt = time.time()
            if prev:
                dt = max(0.25, nowt - prev[0])
                rd = max(0.0, (ioc.read_bytes - prev[1]) / dt)
                wr = max(0.0, (ioc.write_bytes - prev[2]) / dt)
            _PIO[p.pid] = (nowt, ioc.read_bytes, ioc.write_bytes)
        except Exception:                                    # noqa: BLE001
            pass
        # AND AT WHAT PRIORITY, because nuarr demotes its own children when a
        # viewer wants their spindle and the only way to see that was to
        # believe the panel that did it.
        try:
            _io = tracked.ionice()
            _iov = int(_io) if not hasattr(_io, "value") else int(_io.value)
        except Exception:                                    # noqa: BLE001
            _iov = None
        try:
            _cpu_cls = int(tracked.nice())
        except Exception:                                    # noqa: BLE001
            _cpu_cls = None
        procs.append({
            "pid": p.pid,
            "ppid": ppid,
            "name": name,
            "activity": act,
            "detail": detail,
            "rss_mb": round(m / 1024 ** 2, 1),
            "cpu_pct": round(c, 1),
            "age_s": round(age),
            "read_bps": round(rd),
            "write_bps": round(wr),
            "io_prio": _iov,
            "cpu_prio": _cpu_cls,
            "gpu": gpu_use,
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
        _PIO.pop(pid, None)

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
    # ONE LIST, SPLIT BY WHERE IT ACTUALLY RUNS. Every entry says which chip
    # it is on, so the GPU panel gets the CUDA work and the CPU panel gets the
    # rest - and "audio language: Whisper" stops appearing under the GPU on a
    # box whose Whisper was installed for the CPU.
    _work = _gpu_work()
    out["gpu_work"] = [w for w in _work if w.get("device") == "gpu"]
    out["cpu_work"] = [w for w in _work if w.get("device") == "cpu"]
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
    # WHICH CHIP, DECIDED BY HOW IT WAS SET UP - not by which panel is asking.
    # PaddleOCR is a GPU engine only when its CUDA build is the one installed;
    # Tesseract is always CPU; Whisper is whatever faster-whisper was installed
    # for. Listing all of it under the GPU because that is where the label used
    # to live told a CPU-only box that its card was busy with Whisper.
    try:
        from . import subocr
        from . import jobs as _jobs
        n = sum(1 for w in _jobs.RUNNING.values()
                if getattr(getattr(w, "job", None), "kind", "") == "sub_ocr"
                or getattr(w, "sub_ocr_active", False))
        eng = subocr.engine()
        if n and eng == "paddle":
            cached = subocr._PADDLE_CACHE.get("data")
            # An empty cache is "not asked yet", not "no CUDA" - claiming
            # CPU on a GPU build right after boot would be invented, so an
            # unknown device is reported as gpu (its usual home) and said so.
            on_gpu = True if cached is None else bool(cached.get("cuda"))
            work.append({"kind": "subocr",
                         "label": "subtitle OCR — PaddleOCR"
                                  + ("" if cached is None else
                                     (" (CUDA)" if on_gpu else " (CPU build)")),
                         "n": n, "device": "gpu" if on_gpu else "cpu"})
        elif n:
            work.append({"kind": "subocr",
                         "label": f"subtitle OCR — {eng or 'tesseract'}",
                         "n": n, "device": "cpu"})
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import audiolang
        p = audiolang.progress()
        # The device the model actually loaded on, once it has; before that,
        # the one it was installed for. Same rule audiolang.info() applies.
        dev = (audiolang._MODEL_DEV
               or ("cuda" if audiolang.info().get("cuda_devices") else "cpu"))
        on_gpu = str(dev).lower().startswith("cuda")
        where = "gpu" if on_gpu else "cpu"
        if (p.get("state") or "") == "listening":
            work.append({"kind": "whisper",
                         "label": "audio language — Whisper"
                                  + (" (CUDA)" if on_gpu else " (CPU)"),
                         "n": 1, "device": where,
                         "detail": (p.get("current") or "")[:60]})
        elif audiolang._MODEL is not None:
            work.append({"kind": "whisper",
                         "label": ("Whisper model loaded (idle, holding VRAM)"
                                   if on_gpu else
                                   "Whisper model loaded (idle, in memory)"),
                         "n": 0, "device": where})
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
            _hist_push(_LATEST)
        except Exception:
            pass
        await asyncio.sleep(SAMPLE_S)


# Prime the CPU counter so the first real reading is not 0.0
try:
    psutil.cpu_percent(interval=None)
except Exception:
    pass


# ---------------------------------------------------- working-set trim ----
# WHERE THE RAM WENT. Audited on this box with nuarr idle: 623 MB resident
# and 90,000 Python objects - the objects account for tens of MB. Handing
# every untouched page back to the OS dropped it to 11 MB, and a minute
# later it had settled at 140-250 MB: THAT is what nuarr actually touches.
# The other ~400 MB was the high-water mark of a library scan - two arrs'
# JSON for 39,000 files parsed into dicts, plus the walk and the reconcile
# snapshot, all alive at once for a few seconds - which the allocator keeps
# mapped afterwards, resident but never read again.
#
# So the scan gives it back when it finishes. The pages are not lost:
# anything touched again is faulted back in from the standby list in
# microseconds. What changes is that the memory is available to Plex and
# the OS file cache instead of sitting in nuarr's column of Task Manager.
_TRIM = {"at": 0.0, "from_mb": 0.0, "to_mb": 0.0, "n": 0}


def trim_working_set(reason: str = "") -> dict:
    """Release untouched pages to the OS; returns before/after in MB."""
    import ctypes
    import gc
    try:
        import psutil
        p = psutil.Process()
        before = p.memory_info().rss
        gc.collect()
        ctypes.windll.psapi.EmptyWorkingSet(ctypes.c_void_p(-1))
        after = p.memory_info().rss
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    _TRIM.update(at=time.time(), from_mb=before / 2**20, to_mb=after / 2**20,
                 n=_TRIM["n"] + 1)
    try:
        from . import joblog
        joblog.log(f"memory: released {(before - after) / 2**20:.0f} MB of untouched "
                   f"pages{' after ' + reason if reason else ''} - {before / 2**20:.0f} MB "
                   f"resident before, working set rebuilds to what is in use", "debug")
    except Exception:                                        # noqa: BLE001
        pass
    return {"ok": True, "from_mb": round(before / 2**20, 1), "to_mb": round(after / 2**20, 1)}


def trim_if_bloated(threshold_mb: float = 400.0, min_gap_s: float = 1800.0,
                    reason: str = "") -> dict | None:
    """Trim only when resident memory has climbed well past what is in use."""
    try:
        import psutil
        rss = psutil.Process().memory_info().rss / 2**20
    except Exception:                                        # noqa: BLE001
        return None
    if rss < threshold_mb or time.time() - _TRIM["at"] < min_gap_s:
        return None
    return trim_working_set(reason)
