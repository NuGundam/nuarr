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
    # cpu_pct, NOT cpu. proc_list carries cpu_pct, so the old key summed a
    # field that was never there and every bar read 0%.
    out["temp"] = _cpu_temp()
    out["by_work"] = _by_work("cpu_pct")
    out["top"] = _top_procs("cpu_pct", 12)
    return out


_TEMP: dict = {"at": 0.0, "out": {}}


def _cpu_temp() -> dict:
    r"""How hot the processor is, if anything on this machine will say.

    WINDOWS DOES NOT KNOW. There is no supported interface for a CPU core
    temperature: reading it means talking to the chipset over a kernel
    driver, which is what LibreHardwareMonitor and HWiNFO install and why
    they can answer when nothing else can. Four sources are tried in the
    order of how much they can be trusted, and when none of them answers
    the page says so rather than showing a number from somewhere else -
    measured on this box: the ACPI thermal zone returns nothing, and the
    two temperature probes cimv2 lists are LM78A stubs with a null
    reading, which is the usual state of affairs on a server board.

    Cached for ten seconds: a temperature does not move faster than that
    and each attempt is a WMI round trip.
    """
    if _TEMP["out"] and time.time() - _TEMP["at"] < 10:
        return _TEMP["out"]
    out: dict = {}
    # 0. THE CHIP'S OWN SENSORS, through LibreHardwareMonitor's library -
    # see cputemp.py. Package temperature leads, every core beside it.
    driver_why = ""
    try:
        from . import cputemp
        t = cputemp.read()
        if t.get("ok"):
            lead = t.get("package") if t.get("package") is not None else t.get("max")
            out = {"c": round(float(lead), 1), "source": t.get("source", ""),
                   "what": ("CPU package" if t.get("package") is not None
                            else "hottest core"),
                   "cores": [round(float(v)) for v in (t.get("cores") or [])],
                   "max": t.get("max"), "avg": t.get("avg"),
                   "tjmax_margin": t.get("tjmax_margin"),
                   "chip": t.get("chip") or ""}
            # A DRIVER READ IS FRESHER THAN A WMI ONE, so it is not held
            # for the full ten seconds - cputemp paces itself.
            _TEMP.update(at=time.time() - 8, out=out)
            return out
        if t.get("opening"):
            return {"c": None, "source": "", "opening": True,
                    "why": "opening the sensor driver…"}
        driver_why = t.get("why") or ""
    except Exception as e:                                   # noqa: BLE001
        driver_why = f"{type(e).__name__}"
    try:
        from . import diskload
        # 1 + 2. LibreHardwareMonitor / OpenHardwareMonitor, when running.
        for ns, who in ((r"root\LibreHardwareMonitor", "LibreHardwareMonitor"),
                        (r"root\OpenHardwareMonitor", "OpenHardwareMonitor")):
            best, name = None, ""
            for r in diskload.wmi_query(
                    "SELECT Name, Value, SensorType, Parent FROM Sensor", ns):
                if str(getattr(r, "SensorType", "")) != "Temperature":
                    continue
                nm = str(getattr(r, "Name", "") or "")
                par = str(getattr(r, "Parent", "") or "")
                if "cpu" not in (nm + par).lower():
                    continue
                try:
                    v = float(getattr(r, "Value", 0) or 0)
                except (TypeError, ValueError):
                    continue
                # "CPU Package" is the one to lead with; otherwise the
                # hottest core is the honest answer.
                if "package" in nm.lower():
                    best, name = v, nm
                    break
                if best is None or v > best:
                    best, name = v, nm
            if best:
                out = {"c": round(best, 1), "source": who, "what": name}
                break
        # 3. The ACPI thermal zone - tenths of a kelvin.
        if not out:
            vals = []
            for r in diskload.wmi_query(
                    "SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature",
                    r"root\wmi"):
                try:
                    k = float(getattr(r, "CurrentTemperature", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if k > 2000:                      # 273.15 K is 0 C
                    vals.append(k / 10.0 - 273.15)
            if vals:
                out = {"c": round(max(vals), 1), "source": "ACPI thermal zone",
                       "what": "the warmest zone"}
        # 4. A board sensor, where one is actually wired up.
        if not out:
            for r in diskload.wmi_query(
                    "SELECT CurrentReading, Description FROM Win32_TemperatureProbe"):
                v = getattr(r, "CurrentReading", None)
                if v in (None, ""):
                    continue
                try:
                    out = {"c": round(float(v) / 10.0 - 273.15, 1),
                           "source": "board sensor",
                           "what": str(getattr(r, "Description", "") or "")}
                    break
                except (TypeError, ValueError):
                    continue
    except Exception:                                        # noqa: BLE001
        pass
    if not out:
        out = {"c": None, "source": "",
               "why": ("Windows exposes no processor temperature on this "
                       "machine - the ACPI thermal zone reports nothing and "
                       "the board probes read null."
                       + (f" The sensor driver could not be used: {driver_why}"
                          if driver_why else
                          " pip install WinTmp and this fills itself in."))}
    _TEMP.update(at=time.time(), out=out)
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
    r"""Every job on the video path, and whether it is on the card at all.

    NOT EVERY TRANSCODE IS AN ENCODE. A repack is a stream copy: it runs at
    276 fps through the CPU and the disk and never touches NVENC, so listing
    it with four empty encoder columns said "this is an encode nuarr cannot
    describe" when the truth is "this is not an encode". The row now carries
    what is actually running (the tool and the silicon, from the same
    what_runs() the job cards use) and says plainly when there is no encoder
    in the job.
    """
    rows = []
    try:
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            d = w.as_dict()
            if not d.get("venc") and d.get("kind") != "transcode":
                continue
            v = d.get("venc") or {}
            doing = d.get("doing") or {}
            if isinstance(doing, dict):
                tool, hw = doing.get("tool") or "", doing.get("hw") or ""
                why = doing.get("why") or ""
            else:
                tool, hw, why = str(doing), "", ""
            fam = (v.get("family") or "").lower()
            # THE AUDIO HALF. "Keeping the picture as-is" is not "doing
            # nothing": most passthrough jobs convert a track - TrueHD
            # down to 5.1, EAC3 to AAC stereo - and strip a Dolby Vision
            # layer, and the page showed none of it because the video
            # encoder was the only thing it asked about.
            plan = getattr(getattr(w, "job", None), "plan", None)
            aud = []
            try:
                for op in (getattr(plan, "audio_ops", None) or []):
                    to = str(op.get("to") or "copy").lower()
                    if to == "copy":
                        continue
                    ch = op.get("ch")
                    br = op.get("br")
                    aud.append(to + (f" {ch}ch" if ch else "")
                               + (f" {int(br)}k" if br else ""))
            except Exception:                                # noqa: BLE001
                aud = []
            n_copy = 0
            try:
                n_copy = sum(1 for op in (getattr(plan, "audio_ops", None) or [])
                             if str(op.get("to") or "copy").lower() == "copy")
            except Exception:                                # noqa: BLE001
                pass
            rows.append({"title": d.get("title") or d.get("file") or "",
                         "video": (v.get("encoder") or v.get("family")
                                   or "copy"),
                         "strip_dv": bool(getattr(plan, "strip_dv", False)),
                         "audio": aud, "audio_copy": n_copy,
                         "audio_what": [a.get("what") for a in (d.get("actions") or [])
                                        if a.get("kind") == "audio"],
                         "stage": d.get("stage") or "", "fps": d.get("fps"),
                         "speed": d.get("speed"),
                         # A COPY HAS NO SPEED MULTIPLIER - ffmpeg's figure is
                         # derived from out_time, which does not move on a
                         # stream copy, so the worker blanks it on purpose.
                         # What a copy does have is a write rate.
                         "write_bps": d.get("write_bps") or 0,
                         "progress": d.get("progress"),
                         "encoder": v.get("encoder") or "",
                         "family": v.get("family") or "",
                         "preset": v.get("preset") or "",
                         "cq": v.get("cq"),
                         "tool": tool, "hw": hw, "why": why,
                         "pool": d.get("pool") or "", "kind": d.get("kind") or "",
                         # Whether this one is on the card. A stream copy is
                         # not, and a CPU encoder is not either.
                         "on_gpu": bool(fam and fam not in ("cpu", "x264", "x265")),
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
    names = sorted({n for row in hist[-30:] for n in row["d"]},
                   key=_disk_order) if hist else []
    out["disks"] = [_named(n) for n in names]
    out["by_disk"] = _disk_now(hist)
    out["volumes"] = _volumes()
    out["files"] = _files_in_flight()
    out["top"] = _top_procs("read_bps", 12, extra="write_bps")
    return out


_LABELS: dict = {}
_NAMES: dict = {}
_NAMES_AT = 0.0


def _win_names() -> dict:
    r"""What WINDOWS calls each physical disk: its letters, volume labels and
    the model on the label.

    Five of the disks on this box are not pool members and not the cache, so
    nuarr had no name for them and the page printed "PhysicalDrive12" - which
    is a number, not an answer. Windows knows all of it: MSFT_Disk has the
    model, MSFT_Partition maps a disk number to its drive letters, and
    MSFT_Volume has the label somebody typed. One query each, asked at most
    once a minute, through the same in-process WMI the disk-load sampler
    already uses - no powershell.exe.
    """
    global _NAMES, _NAMES_AT
    if _NAMES and time.time() - _NAMES_AT < 60:
        return _NAMES
    out: dict = {}
    try:
        from . import diskload
        ST = "root\\Microsoft\\Windows\\Storage"
        # TWO PROPERTIES, NOT FIVE. Asking MSFT_Disk for Size, BusType and
        # MediaType alongside these returns an empty set on this build -
        # no error, no rows - while the same query for Number and
        # FriendlyName returns all seventeen. Measured, not assumed.
        for r in diskload.wmi_query(
                "SELECT Number, FriendlyName FROM MSFT_Disk", ST):
            try:
                n = int(r.Number)
            except (TypeError, ValueError):
                continue
            out[f"PhysicalDrive{n}"] = {
                "model": str(getattr(r, "FriendlyName", "") or "").strip(),
                "letters": [], "labels": []}
        letters: dict = {}
        for r in diskload.wmi_query(
                "SELECT DiskNumber, DriveLetter FROM MSFT_Partition", ST):
            try:
                n = int(r.DiskNumber)
            except (TypeError, ValueError):
                continue
            # The storage namespace hands a drive letter back as a character
            # CODE - 70 for F - and a partition without one as 0.
            ch = getattr(r, "DriveLetter", None)
            if isinstance(ch, int):
                ch = chr(ch) if 65 <= ch <= 90 else ""
            ch = str(ch or "").strip().strip("\x00").upper()
            if not ch.isalpha():
                continue
            d = out.setdefault(f"PhysicalDrive{n}",
                               {"model": "", "letters": [], "labels": []})
            if ch not in d["letters"]:
                d["letters"].append(ch)
            letters.setdefault(ch, n)
        # THE NAME SOMEBODY TYPED, from cimv2 rather than from MSFT_Volume.
        # MSFT_Volume has the label and returns None for DriveLetter in the
        # same projection, so there is nothing to join it to; Win32_LogicalDisk
        # carries both in one row.
        for r in diskload.wmi_query(
                "SELECT DeviceID, VolumeName FROM Win32_LogicalDisk"):
            dev = str(getattr(r, "DeviceID", "") or "").strip().rstrip(":")
            lab = str(getattr(r, "VolumeName", "") or "").strip()
            n = letters.get(dev.upper())
            if n is None or not lab:
                continue
            d = out.get(f"PhysicalDrive{n}")
            if d and lab not in d["labels"]:
                d["labels"].append(lab)
    except Exception:                                        # noqa: BLE001
        pass
    if out:
        _NAMES, _NAMES_AT = out, time.time()
    return _NAMES


def _pool_labels() -> dict:
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
                _LABELS.setdefault(k, "Nuarr Cache")
        except Exception:                                    # noqa: BLE001
            pass
    return _LABELS


def _label_for(name: str) -> str:
    """PhysicalDrive7 -> NU-DRIVE-3, where nuarr knows which is which."""
    return _pool_labels().get(name, "")


def _named(name: str) -> dict:
    """Everything there is to call this disk: nuarr's name, Windows', both."""
    w = _win_names().get(name) or {}
    letters = w.get("letters") or []
    labs = w.get("labels") or []
    mine = _label_for(name)
    # WHAT TO LEAD WITH. nuarr's own name where it has one, because that is
    # what every other panel calls it; otherwise the volume label somebody
    # typed, then the drive letters, then the model on the case.
    lead = mine or (labs[0] if labs else "") or \
        (":, ".join(letters) + ":" if letters else "") or w.get("model") or name
    bits = []
    if letters:
        bits.append(", ".join(f"{c}:" for c in letters))
    if labs and labs[0] != lead:
        bits.append(labs[0])
    if w.get("model"):
        bits.append(w["model"])
    return {"name": name, "label": lead, "mine": bool(mine),
            "note": " · ".join(bits), "model": w.get("model") or ""}


def _disk_order(name: str):
    """PhysicalDrive2 before PhysicalDrive10, and nuarr's own disks first.

    SORTED BY NAME, NOT BY RATE. Ordering seventeen rows by how busy they are
    means every row moves every second and the one you were reading is
    somewhere else by the time you find it - which is the same complaint the
    process table was rewritten for.
    """
    d = _named(name)
    try:
        n = int(name.replace("PhysicalDrive", ""))
    except ValueError:
        n = 9999
    lab = d["label"]
    # NU-DRIVE-2 before NU-DRIVE-10: the tail digits sort as a number.
    tail = ""
    for ch in reversed(lab):
        if ch.isdigit():
            tail = ch + tail
        else:
            break
    return (0 if d["mine"] else 1, lab[:len(lab) - len(tail)].lower(),
            int(tail) if tail else -1, n)


def _disk_now(hist: list) -> list:
    """The latest rate per disk, with a short average beside it."""
    if not hist:
        return []
    last = hist[-1]["d"]
    recent = hist[-10:]
    out = []
    for name, v in sorted(last.items(), key=lambda kv: _disk_order(kv[0])):
        vals = [row["d"].get(name) for row in recent if name in row["d"]]
        out.append({
            **_named(name),
            "read_bps": v[0], "write_bps": v[1],
            "reads": v[2], "writes": v[3],
            "avg_read": sum(x[0] for x in vals) / len(vals) if vals else 0.0,
            "avg_write": sum(x[1] for x in vals) / len(vals) if vals else 0.0,
        })
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
            drive = os.path.splitdrive(os.path.abspath(path))[0] or path
            out.append({"label": "Nuarr Cache", "note": drive,
                        "total": u.total, "used": u.used,
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
