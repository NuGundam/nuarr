r"""
nuarr - the processor's temperature, read the only way Windows allows

WHY
---
Windows has no supported interface for a CPU core temperature. The ACPI
thermal zone is empty on a server board, the cimv2 probes are LM78A stubs
reading null, and psutil has no sensors on Windows at all. What works is
what HWiNFO and LibreHardwareMonitor do: a small kernel driver that reads
the chip's own digital thermal sensors over MSRs. LibreHardwareMonitor's
library is open source and a .NET assembly; the WinTmp package on PyPI
ships that assembly with pythonnet to load it. Erik asked for the
temperature and pointed at WinTmp; this is that, done carefully.

HOW, AND WHAT IS DELIBERATELY NOT DONE
--------------------------------------
WinTmp's own module opens a Computer with CPU, GPU, memory, motherboard
AND storage enabled, at import, and never closes it. Storage means SMART
reads on all seventeen spindles - a page poll is not the place for that,
and the card already has nvidia-smi. So only the assembly is borrowed;
the Computer is nuarr's own, CPU only.

  * Opened LAZILY, on its own thread, the first time a page asks - it
    loads a kernel driver, which takes a second and needs the process to
    be elevated (nuarr's task runs at Highest). Nothing at startup.
  * One thread for all of it. pythonnet and the driver are happiest when
    every call comes from the same place, so reads are serialised.
  * Updated at most every five seconds. The chip does not change faster
    than that in any way worth drawing, and each update is ~110 ms.
  * CLOSED AT EXIT, so the driver is unloaded rather than left behind.
  * Never a hard dependency: without WinTmp, without pythonnet, or
    without elevation, read() says why and everything else carries on.

WHAT COMES BACK. The package temperature leads - it is the figure the
chip throttles on - with the hottest core and the average beside it,
every core's own reading for the tiles, and how far the hottest core is
from TjMax, which is the number that says "fine" or "not fine" without
needing to know what a good temperature for this chip is.
"""
from __future__ import annotations

import atexit
import concurrent.futures
import importlib.util
import os
import threading
import time

EVERY_S = 5.0

_LOCK = threading.Lock()
_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix="cputemp")
_HW = None                      # the open Computer, on the reader thread
_STATE: dict = {"at": 0.0, "out": {}, "why": "", "opening": False,
                "tried": False}


def _dll() -> str:
    """LibreHardwareMonitorLib.dll, as WinTmp ships it. '' when absent."""
    try:
        spec = importlib.util.find_spec("WinTmp")
        if not spec or not spec.origin:
            return ""
        p = os.path.join(os.path.dirname(spec.origin),
                         "LibreHardwareMonitorLib.dll")
        return p if os.path.exists(p) else ""
    except Exception:                                        # noqa: BLE001
        return ""


def _open() -> str:
    """Load the assembly and open a CPU-only Computer. Reader thread only."""
    global _HW
    dll = _dll()
    if not dll:
        return ("the WinTmp package is not installed - pip install WinTmp "
                "puts LibreHardwareMonitor's library where nuarr can load it")
    try:
        import clr                                          # pythonnet
    except Exception as e:                                   # noqa: BLE001
        return f"pythonnet is not usable here ({type(e).__name__})"
    try:
        clr.AddReference(dll)
        from LibreHardwareMonitor import Hardware
        hw = Hardware.Computer()
        hw.IsCpuEnabled = True
        hw.Open()
        _HW = hw
        return ""
    except Exception as e:                                   # noqa: BLE001
        msg = str(e)[:160]
        if "access" in msg.lower() or "denied" in msg.lower():
            msg += " - the driver needs nuarr to run elevated"
        return f"could not open the sensor driver: {type(e).__name__}: {msg}"


def _read_now() -> dict:
    """One update of the open Computer. Reader thread only."""
    if _HW is None:
        return {}
    out: dict = {"cores": [], "package": None, "max": None, "avg": None,
                 "tjmax_margin": None, "chip": ""}
    for h in _HW.Hardware:
        if str(h.HardwareType) != "Cpu":
            continue
        h.Update()
        out["chip"] = str(h.Name or "")
        margins = []
        for s in h.Sensors:
            if str(s.SensorType) != "Temperature":
                continue
            name = str(s.Name or "")
            try:
                v = float(s.Value) if s.Value is not None else None
            except (TypeError, ValueError):
                v = None
            if v is None:
                continue
            low = name.lower()
            if "distance to tjmax" in low:
                margins.append(v)
            elif low == "cpu package" or low.endswith("package"):
                out["package"] = v
            elif low == "core max":
                out["max"] = v
            elif low == "core average":
                out["avg"] = v
            elif low.startswith("cpu core #"):
                try:
                    n = int(low.split("#", 1)[1].split()[0])
                except ValueError:
                    continue
                out["cores"].append((n, v))
        if margins:
            out["tjmax_margin"] = min(margins)
        break
    out["cores"] = [v for _, v in sorted(out["cores"])]
    if out["max"] is None and out["cores"]:
        out["max"] = max(out["cores"])
    if out["avg"] is None and out["cores"]:
        out["avg"] = round(sum(out["cores"]) / len(out["cores"]), 1)
    return out


def _refresh() -> None:
    """Open if needed, then read. Runs on the reader thread."""
    try:
        if _HW is None:
            why = _open()
            if why:
                _STATE.update(why=why, at=time.time(), opening=False)
                return
        out = _read_now()
        _STATE.update(out=out, at=time.time(), why="", opening=False)
    except Exception as e:                                   # noqa: BLE001
        _STATE.update(why=f"{type(e).__name__}: {str(e)[:120]}",
                      at=time.time(), opening=False)


def read() -> dict:
    """The latest reading, refreshed in the background when it is stale.

    NEVER BLOCKS THE CALLER. The first call kicks off the open on the
    reader thread and returns 'opening'; the page's next poll has the
    figure. Later calls hand back the cached reading and, if it is older
    than EVERY_S, queue one refresh - never two.
    """
    now = time.time()
    with _LOCK:
        stale = now - _STATE["at"] >= EVERY_S
        if stale and not _STATE["opening"]:
            _STATE["opening"] = True
            _STATE["tried"] = True
            _EXEC.submit(_refresh)
        out = dict(_STATE["out"])
        why = _STATE["why"]
        first = _HW is None and not why
    if out.get("package") is not None or out.get("cores"):
        return {"ok": True, "source": "LibreHardwareMonitor", **out}
    if first:
        return {"ok": False, "why": "opening the sensor driver…", "opening": True}
    return {"ok": False, "why": why or "no reading yet"}


def _close() -> None:
    global _HW
    hw, _HW = _HW, None
    if hw is not None:
        try:
            hw.Close()
        except Exception:                                    # noqa: BLE001
            pass


# On the exiting thread rather than the reader's: concurrent.futures joins
# its own workers before atexit handlers run, so there is no reader thread
# to hand this to by then. Close() from another thread is fine.
atexit.register(_close)
