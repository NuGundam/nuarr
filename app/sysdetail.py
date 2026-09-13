r"""
nuarr - the four detail pages behind the task manager's cards

WHY
---
The task manager's four cards each answer one question with one line:
how much processor, how much memory, how much card, how much disk. That
is the right amount for a card and the wrong amount for the moment the
line does something you did not expect. "34% cpu" does not say whether
that is one thread pinned or twenty threads busy; "99% encoder" does not
say which engine, at what clock, for whose session; "222 MB/s read" does
not say which spindle it came off or which file was being read.

So each card opens. Erik: "it would open a new page under task manager
with more detailed info and graph like disk files R/W, detailed encoding,
decoding, core info, memory info, and cpu info".

HOW
---
Everything here is read from counters that are already being sampled once
a second, or from state nuarr holds in memory. The one exception is the
card, which is nvidia-smi - and that is asked for at most once every two
seconds and cached, the same as the headline figure. Nothing here opens a
file, walks the library or touches the database except for the one query
the disk page needs to name the volumes.

Each page answers in the same shape: {rows, cards, note} where rows is a
history the browser draws as lines and cards are the figures beside it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

import psutil

from . import system
from .config import NO_WINDOW, SETTINGS, hidden_si


# How many samples the detail pages send for their sparklines. See cpu().
SPARK = 120


# --------------------------------------------------------------- processor --
def cpu() -> dict:
    """Per core, per kind of work, and what nuarr is spending it on."""
    out: dict = {"at": time.time()}
    # TWO MINUTES, NOT FIVE. The ring holds five; a sparkline under a core
    # tile is a hundred pixels wide, and sending three hundred points for it
    # once a second is forty kilobytes a second of line nobody can see.
    hist = system.cores_history(SPARK)
    out["cores"] = {"rows": hist,
                    "n": len(hist[-1]["pct"]) if hist else
                         (psutil.cpu_count(logical=True) or 0)}
    try:
        out["counts"] = {"logical": psutil.cpu_count(logical=True),
                         "physical": psutil.cpu_count(logical=False)}
    except Exception:                                        # noqa: BLE001
        out["counts"] = {}
    out["times"] = _times_pct()
    try:
        f = psutil.cpu_freq()
        out["freq"] = ({"now": round(f.current), "min": round(f.min),
                        "max": round(f.max)} if f else {})
    except Exception:                                        # noqa: BLE001
        out["freq"] = {}
    # Context switches and interrupts as RATES. The counters are cumulative
    # since boot, which is a number nobody can read; the interesting figure
    # is how fast they are moving right now.
    out["stats"] = _stats_rate()
    try:
        la = psutil.getloadavg()
        out["loadavg"] = [round(x, 2) for x in la]
    except Exception:                                        # noqa: BLE001
        out["loadavg"] = []
    out["by_work"] = _by_work("cpu")
    out["top"] = _top_procs("cpu_pct", 12)
    return out


# A DELTA NEEDS TWO READINGS AND SOME TIME BETWEEN THEM.
#
# Everything below is "how much has this counter moved since last time", and
# the page can ask twice in the same tick - the summary poll and a repaint
# land together - which makes the second answer a division by nothing: every
# figure came back 0% and the interrupt rates came back empty. So each of
# these keeps its own last reading AND its last good answer, and refuses to
# recompute until half a second has passed.
MIN_DT = 0.5
_TIMES: dict = {"at": 0.0, "prev": None, "out": {}}


def _times_pct() -> dict:
    """user / system / idle / interrupt / dpc, as percentages of the whole.

    Computed here rather than with cpu_times_percent() for the reason above:
    psutil keeps one hidden previous reading and no minimum interval, so two
    calls in the same tick zero it out with nothing to say so.
    """
    try:
        cur = psutil.cpu_times()
    except Exception:                                        # noqa: BLE001
        return _TIMES["out"]
    now = time.time()
    prev, prev_at = _TIMES["prev"], _TIMES["at"]
    if prev is None:
        _TIMES.update(at=now, prev=cur)
        return {}
    if now - prev_at < MIN_DT:
        return _TIMES["out"]
    d = {k: max(0.0, getattr(cur, k) - getattr(prev, k))
         for k in cur._fields}
    total = sum(d.values())
    out = ({k: round(100.0 * v / total, 1) for k, v in d.items()}
           if total > 0 else _TIMES["out"])
    _TIMES.update(at=now, prev=cur, out=out)
    return out


_STATS_PREV: tuple = (0.0, None)
_STATS_OUT: dict = {}


def _stats_rate() -> dict:
    global _STATS_PREV, _STATS_OUT
    try:
        s = psutil.cpu_stats()
    except Exception:                                        # noqa: BLE001
        return _STATS_OUT
    now = time.time()
    prev_t, prev = _STATS_PREV
    if prev is not None and now - prev_t < MIN_DT:
        return _STATS_OUT
    _STATS_PREV = (now, s)
    if not prev or now - prev_t <= 0 or now - prev_t > 60:
        return _STATS_OUT
    dt = now - prev_t
    _STATS_OUT = {
        "ctx_switches": round((s.ctx_switches - prev.ctx_switches) / dt),
        "interrupts": round((s.interrupts - prev.interrupts) / dt),
        "soft_interrupts": round((s.soft_interrupts
                                  - prev.soft_interrupts) / dt),
        "syscalls": round((s.syscalls - prev.syscalls) / dt)}
    return _STATS_OUT


# ----------------------------------------------------------------- memory --
def mem() -> dict:
    """The machine's memory, nuarr's share of it, and who is holding it."""
    out: dict = {"at": time.time()}
    try:
        vm = psutil.virtual_memory()
        out["vm"] = {k: (round(v / 2**20) if k != "percent" else round(v, 1))
                     for k, v in vm._asdict().items()}
    except Exception:                                        # noqa: BLE001
        out["vm"] = {}
    try:
        sw = psutil.swap_memory()
        out["swap"] = {"total": round(sw.total / 2**20),
                       "used": round(sw.used / 2**20),
                       "percent": round(sw.percent, 1),
                       # Paging as a rate is the figure that matters: a full
                       # page file is normal, a busy one is not.
                       **_swap_rate(sw)}
    except Exception:                                        # noqa: BLE001
        out["swap"] = {}
    out["by_work"] = _by_work("rss_mb")
    out["top"] = _top_procs("rss_mb", 12)
    # What nuarr's own server is holding, and when its working set was last
    # handed back - the trim exists and saying so is how it stops looking
    # like a leak.
    try:
        out["trim"] = dict(system._TRIM)
    except Exception:                                        # noqa: BLE001
        out["trim"] = {}
    return out


_SWAP_PREV: tuple = (0.0, 0, 0)
_SWAP_OUT: dict = {}


def _swap_rate(sw) -> dict:
    global _SWAP_PREV, _SWAP_OUT
    now = time.time()
    prev_t, pin, pout = _SWAP_PREV
    if prev_t and now - prev_t < MIN_DT:
        return _SWAP_OUT
    _SWAP_PREV = (now, sw.sin, sw.sout)
    if not prev_t or now - prev_t <= 0 or now - prev_t > 60:
        return _SWAP_OUT
    dt = now - prev_t
    _SWAP_OUT = {"in_bps": max(0.0, (sw.sin - pin) / dt),
                 "out_bps": max(0.0, (sw.sout - pout) / dt)}
    return _SWAP_OUT


# --------------------------------------------------------------- graphics --
_GPU_D: tuple = (0.0, {})
_GPU_D_TTL = 2.0


def gpu() -> dict:
    """The card in full: clocks, power, both video engines, and its sessions.

    THE ENGINES ARE THE POINT. An encode uses three separate parts of the
    card - NVDEC to decode, the SMs for any filtering, NVENC to encode - and
    the headline figure only covers one of them. Sessions, clocks and the
    power cap say why a card at 99% encoder is or is not the bottleneck.
    """
    global _GPU_D
    now = time.time()
    if now - _GPU_D[0] < _GPU_D_TTL:
        return _GPU_D[1]
    out: dict = {"at": now}
    exe = shutil.which("nvidia-smi")
    if not exe:
        out["why"] = "nvidia-smi is not on this machine"
        _GPU_D = (now, out)
        return out
    fields = ["name", "driver_version", "pstate", "temperature.gpu",
              "utilization.gpu", "utilization.encoder", "utilization.decoder",
              "utilization.memory", "memory.used", "memory.total",
              "clocks.sm", "clocks.max.sm", "clocks.mem", "clocks.max.mem",
              "power.draw", "power.limit", "fan.speed",
              "encoder.stats.sessionCount", "encoder.stats.averageFps",
              "encoder.stats.averageLatency",
              "clocks_throttle_reasons.active"]
    try:
        r = subprocess.run(
            [exe, "--query-gpu=" + ",".join(fields),
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=8, creationflags=NO_WINDOW,
            startupinfo=hidden_si())
        line = r.stdout.decode("utf-8", "replace").strip().splitlines()[0]
        vals = [v.strip() for v in line.split(",")]

        def num(i):
            try:
                return float(vals[i])
            except (ValueError, IndexError):
                return None
        out["card"] = {
            "name": vals[0], "driver": vals[1], "pstate": vals[2],
            "temp_c": num(3), "sm_pct": num(4), "encoder_pct": num(5),
            "decoder_pct": num(6), "vram_bus_pct": num(7),
            "vram_used": num(8), "vram_total": num(9),
            "clock_sm": num(10), "clock_sm_max": num(11),
            "clock_mem": num(12), "clock_mem_max": num(13),
            "power": num(14), "power_limit": num(15), "fan": num(16),
            "sessions": num(17), "session_fps": num(18),
            "session_latency": num(19),
            "throttle": (vals[20] if len(vals) > 20 else ""),
        }
    except Exception as e:                                   # noqa: BLE001
        out["why"] = f"could not read the card: {type(e).__name__}"
    # WHO IS ON IT. The card is shared with Plex, and "encoder 99%" means
    # something different depending on whose session that is.
    try:
        out["procs"] = (system._gpu() or {}).get("procs") or []
        out["per_proc_vram"] = bool((system._gpu() or {}).get("per_proc_vram"))
    except Exception:                                        # noqa: BLE001
        out["procs"] = []
    # AND WHAT NUARR IS ASKING OF IT - each encode with its encoder, preset,
    # fps and speed, plus the OCR and listening work that runs on CUDA.
    out["encodes"] = _encodes()
    try:
        out["work"] = [w for w in system._gpu_work() if w.get("device") == "gpu"]
    except Exception:                                        # noqa: BLE001
        out["work"] = []
    _GPU_D = (now, out)
    return out


def _encodes() -> list:
    rows = []
    try:
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            d = w.as_dict()
            if not d.get("venc") and d.get("kind") != "transcode":
                continue
            v = d.get("venc") or {}
            rows.append({"title": d.get("title") or d.get("file") or "",
                         "stage": d.get("stage") or "", "fps": d.get("fps"),
                         "speed": d.get("speed"),
                         "progress": d.get("progress"),
                         "encoder": v.get("encoder") or "",
                         "family": v.get("family") or "",
                         "preset": v.get("preset") or "",
                         "cq": v.get("cq"), "doing": d.get("doing") or "",
                         "eta_s": d.get("eta_s")})
    except Exception:                                        # noqa: BLE001
        pass
    return rows


# ------------------------------------------------------------------- disk --
def disk() -> dict:
    """Every spindle's rate and depth, the volumes, and the files in flight."""
    out: dict = {"at": time.time()}
    hist = system.disks_history(SPARK)
    out["rows"] = hist
    names = sorted({n for row in hist[-30:] for n in row["d"]}) if hist else []
    out["disks"] = [{"name": n, "label": _label_for(n)} for n in names]
    out["by_disk"] = _disk_now(hist)
    out["volumes"] = _volumes()
    out["files"] = _files_in_flight()
    out["top"] = _top_procs("read_bps", 12, extra="write_bps")
    return out


_LABELS: dict = {}


def _label_for(name: str) -> str:
    """PhysicalDrive7 -> NU-DRIVE-3, where nuarr knows which is which."""
    if not _LABELS:
        try:
            from . import diskload, scanner
            for label, path in (scanner.media_roots() or {}).items():
                n = diskload.disk_number_for(path)
                if n:
                    _LABELS[f"PhysicalDrive{n}"] = label
        except Exception:                                    # noqa: BLE001
            pass
        try:
            k = system._cache_disk_key()
            if k:
                _LABELS.setdefault(k, "the cache")
        except Exception:                                    # noqa: BLE001
            pass
    return _LABELS.get(name, "")


def _disk_now(hist: list) -> list:
    """The latest rate per disk, with a short average beside it."""
    if not hist:
        return []
    last = hist[-1]["d"]
    recent = hist[-10:]
    out = []
    for name, v in sorted(last.items()):
        vals = [row["d"].get(name) for row in recent if name in row["d"]]
        out.append({
            "name": name, "label": _label_for(name),
            "read_bps": v[0], "write_bps": v[1],
            "reads": v[2], "writes": v[3],
            "avg_read": sum(x[0] for x in vals) / len(vals) if vals else 0.0,
            "avg_write": sum(x[1] for x in vals) / len(vals) if vals else 0.0,
        })
    out.sort(key=lambda d: -(d["read_bps"] + d["write_bps"]))
    return out


def _volumes() -> list:
    out = []
    seen = set()
    try:
        from . import scanner
        for label, path in sorted((scanner.media_roots() or {}).items()):
            try:
                u = shutil.disk_usage(path)
            except OSError:
                continue
            seen.add(label)
            out.append({"label": label, "total": u.total, "used": u.used,
                        "free": u.free, "kind": "pool"})
    except Exception:                                        # noqa: BLE001
        pass
    for path, kind in ((SETTINGS.cache_dir, "cache"),):
        try:
            u = shutil.disk_usage(path)
            out.append({"label": os.path.splitdrive(os.path.abspath(path))[0]
                        or path, "total": u.total, "used": u.used,
                        "free": u.free, "kind": kind})
        except OSError:
            pass
    return out


def _files_in_flight() -> list:
    r"""WHICH FILE EACH BYTE BELONGS TO.

    The disk card can say 222 MB/s and nothing about what is being read, and
    that is the question somebody opening this page has. Every running job
    knows its file, the spindle it came off, the disk it is being written
    back to and its own read and write rates - so the answer is a join
    nuarr can already do, not a filesystem trace.
    """
    rows = []
    try:
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            d = w.as_dict()
            rows.append({
                "title": d.get("title") or "", "file": d.get("file") or "",
                "path": d.get("path") or "",
                "kind": d.get("kind") or "", "pool": d.get("pool") or "",
                "stage": d.get("stage") or "",
                "from": d.get("disk") or "", "to": d.get("dest_disk") or "",
                "staging": d.get("stage_device") or "",
                "read_bps": d.get("read_bps") or 0,
                "write_bps": d.get("write_bps") or 0,
                "progress": d.get("progress") or 0,
                "src_bytes": d.get("src_bytes") or 0,
                "out_bytes": d.get("out_bytes") or 0,
                "commit_bps": d.get("commit_bps") or 0,
                "commit_phase": d.get("commit_phase") or "",
                "paced": bool(d.get("paused_for_viewer")
                              or d.get("paused_for_load")),
                "pace_why": d.get("pace_why") or "",
            })
    except Exception:                                        # noqa: BLE001
        pass
    rows.sort(key=lambda r: -(r["read_bps"] + r["write_bps"]))
    return rows


# ------------------------------------------------------------------ shared --
def _procs() -> list:
    try:
        return (system.snapshot().get("nuarr") or {}).get("proc_list") or []
    except Exception:                                        # noqa: BLE001
        return []


def _by_work(field: str) -> list:
    """nuarr's processes folded into the work they are doing, like the table."""
    agg: dict = {}
    for p in _procs():
        k = p.get("activity") or p.get("name") or "?"
        a = agg.setdefault(k, {"key": k, "n": 0, "v": 0.0})
        a["n"] += 1
        a["v"] += float(p.get(field) or 0)
    rows = sorted(agg.values(), key=lambda a: -a["v"])
    return rows


def _top_procs(field: str, limit: int, extra: str = "") -> list:
    rows = sorted(_procs(), key=lambda p: -(float(p.get(field) or 0)
                                            + float(p.get(extra) or 0)
                                            if extra else
                                            float(p.get(field) or 0)))
    out = []
    for p in rows[:limit]:
        out.append({"pid": p.get("pid"), "name": p.get("name"),
                    "activity": p.get("activity") or "",
                    "detail": p.get("detail") or "",
                    "cpu_pct": p.get("cpu_pct"), "rss_mb": p.get("rss_mb"),
                    "read_bps": p.get("read_bps"), "write_bps": p.get("write_bps"),
                    "threads": p.get("threads"), "age_s": p.get("age_s"),
                    "io_prio": p.get("io_prio")})
    return out


def detail(which: str) -> dict:
    fn = {"cpu": cpu, "mem": mem, "gpu": gpu, "disk": disk}.get(
        (which or "").strip().lower())
    if not fn:
        return {"error": f"no such view '{which}'"}
    return fn()
