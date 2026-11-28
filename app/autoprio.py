"""Auto priority: the low priority tier follows what the REST of the machine wants.

The disk side of nuarr already does this - a job steps aside on the spindle
somebody else is reading and comes back when they are done. This is the same
idea for the processor, the graphics card and memory: measure what everything
that is NOT nuarr is using, and pick how much nuarr runs at once from that.

WHAT IS MEASURED (only what is not nuarr's own):
  cpu   the box's processor use minus nuarr's own tree, as a share of all
        threads, averaged over the last 30 s
  gpu   the busiest engine (NVENC, NVDEC, CUDA, 3D) summed over every process
        outside nuarr's tree, from Windows' per-process GPU counters, averaged
        over the last 30 s. Ollama is left out: it is the model nuarr itself
        asks (the Qwen-VL reader), and counting it would have nuarr slowing
        down for its own work.
  ram   memory still available on the box, in percent

WHAT IT PICKS - the strictest tier any one of them asks for:
            other cpu   other gpu   ram free
  half        >= 20 %     >= 25 %    < 10 %
  two         >= 40 %     >= 50 %    <  6 %
  one         >= 60 %     >= 75 %    <  3 %

HOW IT MOVES - quick to give way, slow to take back, like the disk yield:
  tighter  as soon as the stricter tier has been asked for 15 s running; it
           goes straight there, however many tiers that is
  looser   one tier at a time, and only after 3 minutes in which every input
           stayed under 80 % of the mark that holds the current tier - so a
           load sitting right on a mark does not flap the queue

It only ever narrows what STARTS. A running job is never stopped by a tier.

NOT SEEN: load inside the Hyper-V machines (the host's counters do not carry
it), and video memory per process (the driver does not report it here).
"""
from __future__ import annotations

import asyncio
import time

from .db import kv_get, kv_set

KEY = "gate.low_auto"
TICK_S = 5.0
WINDOW_S = 30.0
TIGHTEN_S = 15.0
RELAX_S = 180.0
RELAX_BAND = 0.8
ORDER = ["", "half", "two", "one"]                # loosest to strictest
#            cpu other, gpu other, ram free below
MARKS = {"half": (20.0, 25.0, 10.0), "two": (40.0, 50.0, 6.0), "one": (60.0, 75.0, 3.0)}
NAMES = {"": "Normal", "half": "Half", "two": "Two", "one": "One"}
_SKIP_GPU = ("ollama", "llama-server")

STATE: dict = {"on": False, "tier": "", "want": "", "why": "", "cause": "",
               "cpu": 0.0, "gpu": 0.0, "ram_free": 100.0, "gpu_seen": True,
               "tighten_since": 0.0, "relax_since": 0.0, "relax_in": 0, "at": 0.0,
               "changed_at": 0.0}
_GPU_RING: list = []                              # (at, percent)
_PNAME: dict = {}


def enabled() -> bool:
    return str(kv_get(KEY) or "0") == "1"


def set_enabled(on: bool) -> None:
    kv_set(KEY, "1" if on else "0")
    STATE.update(on=bool(on), tighten_since=0.0, relax_since=0.0, relax_in=0)
    if on:
        # PRESSING AUTO ASKS FOR AUTO'S ANSWER NOW, not for the tier that was
        # in force to be walked back three minutes a step: it starts at what
        # the figures of the last half minute ask for.
        from . import gate
        STATE["tier"] = STATE.get("want") or ""
        gate.set_low_tier(STATE["tier"])


def _pname(pid: int) -> str:
    n = _PNAME.get(pid)
    if n is None:
        try:
            import psutil
            n = psutil.Process(pid).name().lower()
        except Exception:                                        # noqa: BLE001
            n = ""
        if len(_PNAME) > 2000:
            _PNAME.clear()
        _PNAME[pid] = n
    return n


def _gpu_other(latest: dict) -> tuple[float, bool]:
    """(busiest engine among everything outside nuarr's tree, was it measurable)."""
    from . import system
    own = {int(p.get("pid") or 0) for p in ((latest.get("nuarr") or {}).get("proc_list") or [])}
    by = system.gpu_by_pid()
    if system._PDH.get("dead"):
        # No per-process counters: the card's figure is somebody else's only
        # while nuarr has no work on it at all - the header chip's own rule.
        g = latest.get("gpu") or {}
        try:
            busy = any((w.get("n") or 0) > 0 for w in system._gpu_work() if w.get("device") == "gpu")
        except Exception:                                        # noqa: BLE001
            busy = True
        if busy:
            return 0.0, False
        return max(float(g.get("gpu_pct") or 0), float(g.get("encoder_pct") or 0),
                   float(g.get("decoder_pct") or 0)), True
    eng: dict = {}
    for pid, d in by.items():
        if pid in own or _pname(pid).startswith(_SKIP_GPU):
            continue
        for lab, v in d.items():
            eng[lab] = eng.get(lab, 0.0) + float(v)
    return min(100.0, max(eng.values(), default=0.0)), True


def measure() -> dict:
    """The three figures the choice is made from."""
    from . import system
    now = time.time()
    rows = [r for r in system._HIST[-int(WINDOW_S) - 5:] if now - float(r.get("at") or 0) <= WINDOW_S]
    cpu = (sum(max(0.0, r["cpu_all"] - r["cpu"]) for r in rows) / len(rows)) if rows else 0.0
    ram_free = 100.0 - ((sum(r["ram_all_pct"] for r in rows) / len(rows)) if rows else 0.0)
    g, seen = _gpu_other(system._LATEST or {})
    _GPU_RING.append((now, g))
    while _GPU_RING and now - _GPU_RING[0][0] > WINDOW_S:
        _GPU_RING.pop(0)
    gpu = sum(v for _, v in _GPU_RING) / len(_GPU_RING)
    return {"cpu": round(cpu, 1), "gpu": round(gpu, 1), "ram_free": round(ram_free, 1), "gpu_seen": seen}


def tier_for(m: dict, band: float = 1.0) -> tuple[str, str, str]:
    """(tier, cause, sentence) these figures ask for. band < 1 lowers every
    mark - used on the way back, so leaving a tier takes a clear margin."""
    for t in ("one", "two", "half"):
        c, g, r = MARKS[t]
        if m["cpu"] >= c * band:
            return t, "processor", f"other programs are using {m['cpu']:.0f} % of the processor"
        if m["gpu"] >= g * band:
            return t, "graphics card", f"other programs are using {m['gpu']:.0f} % of the graphics card"
        if m["ram_free"] < r / band:
            return t, "memory", f"only {m['ram_free']:.0f} % of memory is free"
    return "", "", "nothing else is asking for the machine"


def step(m: dict, now: float | None = None) -> str | None:
    """One decision. Returns the new tier when it changes, else None. Pure
    apart from STATE, so it can be run against made-up figures."""
    now = time.time() if now is None else now
    cur = STATE["tier"]
    want, cause, why = tier_for(m)
    STATE.update(want=want, cause=cause, why=why, at=now, **m)
    ci, wi = ORDER.index(cur), ORDER.index(want)
    if wi > ci:                                   # asked to give way
        STATE["relax_since"] = 0.0
        STATE["relax_in"] = 0
        if not STATE["tighten_since"]:
            STATE["tighten_since"] = now
        if now - STATE["tighten_since"] >= TIGHTEN_S:
            STATE.update(tier=want, tighten_since=0.0, changed_at=now)
            return want
        return None
    STATE["tighten_since"] = 0.0
    if ci == 0:
        STATE["relax_since"] = 0.0
        STATE["relax_in"] = 0
        return None
    # may it take a tier back? only with every input clear of the current mark
    calm = ORDER.index(tier_for(m, RELAX_BAND)[0]) < ci
    if not calm:
        STATE["relax_since"] = 0.0
        STATE["relax_in"] = int(RELAX_S)
        return None
    if not STATE["relax_since"]:
        STATE["relax_since"] = now
    STATE["relax_in"] = max(0, int(RELAX_S - (now - STATE["relax_since"])))
    if now - STATE["relax_since"] >= RELAX_S:
        new = ORDER[ci - 1]
        STATE.update(tier=new, relax_since=0.0, changed_at=now, relax_in=0)
        return new
    return None


def status() -> dict:
    return {k: STATE[k] for k in ("on", "tier", "want", "why", "cause", "cpu", "gpu",
                                  "ram_free", "gpu_seen", "relax_in", "at")} | {
        "marks": MARKS, "relax_s": RELAX_S, "tighten_s": TIGHTEN_S}


async def watch() -> None:
    from . import gate, joblog
    # after a restart with auto on: carry on from the tier that was in force
    STATE["on"] = enabled()
    if STATE["on"]:
        STATE["tier"] = gate.low_tier()
    await asyncio.sleep(20)                       # let the sampler fill its ring
    while True:
        try:
            on = enabled()
            STATE["on"] = on
            m = await asyncio.to_thread(measure)
            if on:
                before = STATE["tier"]
                new = step(m)
                if new is not None:
                    gate.set_low_tier(new)
                    joblog.log(f"auto priority: {NAMES[before]} -> {NAMES[new]} - {STATE['why']}"
                               if ORDER.index(new) > ORDER.index(before) else
                               f"auto priority: {NAMES[before]} -> {NAMES[new]} - the machine has been "
                               f"quiet for {int(RELAX_S // 60)} minutes", "info")
                elif gate.low_tier() != STATE["tier"]:
                    gate.set_low_tier(STATE["tier"])      # somebody moved it underneath
            else:
                want, cause, why = tier_for(m)
                STATE.update(want=want, cause=cause, why=why, at=time.time(), **m)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(TICK_S)
