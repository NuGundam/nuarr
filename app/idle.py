r"""nuarr - the shared way background fixers wait their turn.

THE PROBLEM THIS REPLACES
-------------------------
Every system in nuarr that quietly fixes media in the background grew its own
answer to the same two questions, and the answers did not match:

    "may I run right now?"      audit._too_busy() asked the gate about Plex
                                and counted workers; subtitletitle delegated
                                to it; audiolang asked a third way; subembed
                                asked nothing at all.
    "how do I not hog the box?" a fixed batch per pass - twelve files, twenty
                                tracks, twenty-five marks - chosen so that a
                                bad moment could only be so bad.

A FIXED BATCH IS A GUESS ABOUT A BAD MOMENT. It costs you the good ones too:
this box is idle most of the time, and a sweep that does twelve files and then
sleeps ten minutes leaves 434 of them waiting through an empty night. Worse,
the batch is no protection when the moment IS bad - twelve container rewrites
started the instant somebody presses play are still twelve.

So the batch goes and the question is asked before every single item instead.
Busy means pause, not stop; free means keep going. Work is done at whatever
rate the machine can spare, which on an idle box is all of it and during a
film is none.

WHAT BUSY MEANS, ONCE
---------------------
  somebody is watching     the gate's own Plex check, not a second opinion
  the encoders are full    four or more transcode workers running
  a disk is saturated      the gate's per-spindle measurement, which also
                           catches a DrivePool balance, a backup, a big copy
  the CPU is busy          from the sampler that already runs, not a new one
  the GPU is busy          same sampler; encoder utilisation is the figure
                           that matters
  DrivePool is holding     a balance or duplication pass in flight

Every one of those is something nuarr ALREADY measures for another reason.
Nothing here samples anything new, and nothing here decides "busy" differently
from the gate - it just collects the answers in one place and says which one
spoke, so a panel can tell you what it is waiting for rather than only that it
is waiting.

WHAT A CALLER PROVIDES
----------------------
    pending()   -> a list of things to do, cheap enough to call each cycle
    do_one(x)   -> does one of them, synchronously; run on a thread here
    label(x)    -> what to show while it is happening

and gets back a progress dict of a fixed shape, so one panel renderer draws
every system that adopts this.
"""
from __future__ import annotations

import asyncio
import time

from . import joblog

# ---------------------------------------------------------------- the lines --
# WHERE "BUSY" STARTS. High on purpose: this is not a courtesy threshold, it
# is the point past which one more container rewrite would be felt. A box at
# 60% CPU has room for a stream copy; a box at 85% does not.
# HOW MANY AT ONCE, AND WHY IT IS NOT ONE.
#
# "Is two or more at the same time not possible?" - it was not, and only
# because nothing had asked for it: the loop below did one item, waited for
# it, then did the next. Nothing about the work requires that.
#
# But two is not simply twice as fast, and which two matters more than how
# many. A container rewrite is almost pure movement - read the whole file off
# the disk it lives on, write it to the cache, read it back, write it home -
# so two files on the SAME spindle take turns on one arm and finish in the
# time one pair of them would have taken anyway, having made each other slower
# the whole way. Two files on DIFFERENT spindles overlap almost perfectly:
# separate arms, separate queues, one cache in the middle.
#
# So the rule is not a worker count, it is an exclusion: at most `lanes` in
# flight, and never two that share a disk. Set this to 1 to go back to one at
# a time; the gate still stops everything the instant somebody presses play,
# whatever the number is.
LANES = 2

CPU_BUSY_PCT = 80.0
GPU_BUSY_PCT = 80.0
# Four transcodes is a full house on this machine - the number the audit
# already used for the same judgement.
WORKERS_BUSY = 4
# How long to wait before asking again once something said no. Long enough not
# to poll the gate to death, short enough that a paused sweep resumes within a
# minute of the film ending.
PAUSE_S = 20.0
# How long to wait when there is simply nothing to do.
EMPTY_S = 300.0

STATES: dict = {}


def _fresh(key: str, title: str) -> dict:
    return {
        "key": key, "title": title,
        "running": False, "paused": False, "paused_why": "",
        "now": "", "done": 0, "total": 0, "ok": 0, "failed": 0,
        # HOW FAR THROUGH THE ONE IN FRONT OF IT. A 63-second remux with no
        # inner progress is indistinguishable from a stall, and this work is
        # measured in whole files - so the overall bar can sit still for a
        # minute at a time while everything is perfectly fine.
        "item_pct": 0.0, "item_at": 0.0,
        "t0": 0.0, "secs_each": 0.0,
        "last_run": 0.0, "last_done": 0, "last_took": 0.0, "runs": 0,
        "next_look": 0.0, "idle_why": "", "last_error": "",
    }


def state(key: str, title: str = "") -> dict:
    d = STATES.get(key)
    if d is None:
        d = STATES[key] = _fresh(key, title or key)
    if title:
        d["title"] = title
    return d


# ------------------------------------------------------------- is it busy? --
def _machine() -> dict:
    """CPU and GPU, from the sampler that is already running.

    NEVER SAMPLES. system.snapshot() returns the last reading taken on a fixed
    cadence; asking it to measure here would put a psutil interval or an
    nvidia-smi launch inside a loop that runs once per file.
    """
    try:
        from . import system
        s = system.snapshot() or {}
    except Exception:                                            # noqa: BLE001
        return {}
    gpu = s.get("gpu") or {}
    # THE ENCODER FIGURE LEADS, and system.py explains why at the point it is
    # read: NVENC sits at 99% while utilization.gpu reports 35-40%, because
    # the encode runs on a dedicated engine the SM figure does not cover.
    # Taking the higher of the two means neither can hide a busy card.
    g = 0.0
    for k in ("encoder_pct", "gpu_pct"):
        try:
            v = gpu.get(k)
            if v is not None:
                g = max(g, float(v))
        except Exception:                                        # noqa: BLE001
            pass
    return {"cpu": float(s.get("cpu_pct") or 0.0), "gpu": g}


async def busy(disk: str = "") -> dict:
    r"""May a background fixer do one more thing right now, and if not, why.

    `disk` IS THE HALF THIS WAS MISSING, and it showed up the first evening.
    The transcode queue holds work PER SPINDLE - sixteen Outlander jobs sat
    there saying "waiting on: viewer on NU-DRIVE-1" - while this said "not
    busy" and rewrote Outlander episodes on that same disk, because
    check_plex() answers a GLOBAL question and was blocked=False: one session
    playing, nothing transcoding, the pool as a whole fine.
    So the polite system stopped and the new one walked straight past it, onto
    the exact spindle the viewer was reading from. The gate already publishes
    the per-disk answer; this asks for it.

    ORDERED BY WHO IS WAITING. A person watching something comes first, then
    the work nuarr has already committed to, then the hardware. The first
    answer wins and the rest are not asked - they all mean the same thing to
    the caller, and the reason it shows should be the one a person would
    give.
    """
    # 1. somebody is watching
    # ON A THREAD, AND NOT AWAITED. check_plex is a SYNCHRONOUS function that
    # makes an httpx call with a ten-second timeout, and it returns a Reason
    # dataclass. audit._too_busy has been writing `await gate.check_plex()`
    # since it was written - awaiting a dataclass raises TypeError, the bare
    # except swallowed it, and the Plex half of that check has therefore never
    # once fired. subtitletitle delegates to it, so "yield before every file"
    # was only ever counting workers.
    try:
        from . import gate
        st = await asyncio.to_thread(gate.check_plex)
        if st is not None and getattr(st, "blocked", False):
            return {"busy": True, "why": "somebody is watching",
                    "detail": getattr(st, "detail", "") or ""}
    except Exception:                                            # noqa: BLE001
        pass
    # 2. the encoders are full
    try:
        from . import jobs
        live = jobs.live_snapshot() or {}
        n = len([w for w in (live.get("workers") or [])
                 if (w or {}).get("state") == "running"])
        if n >= WORKERS_BUSY:
            return {"busy": True, "why": f"{n} transcodes are running",
                    "detail": "the encoders and the disks they read are "
                              "already carrying everything they can"}
    except Exception:                                            # noqa: BLE001
        pass
    # 3. a disk is saturated - a balance, a backup, a copy, anything
    try:
        from . import gate
        st = await asyncio.to_thread(gate.check_disk_activity)
        if st and getattr(st, "blocked", False):
            return {"busy": True, "why": "a disk is busy",
                    "detail": getattr(st, "detail", "") or ""}
    except Exception:                                            # noqa: BLE001
        pass
    # 4. DrivePool is moving things about
    try:
        from . import drivepool
        # "jobs" is the toggle that means file work - there is no separate
        # one for background fixers, and inventing a fourth kind would be a
        # switch nobody knows to set.
        held, why = drivepool.hold("jobs")
        if held:
            return {"busy": True, "why": "DrivePool is working",
                    "detail": why or ""}
    except Exception:                                            # noqa: BLE001
        pass
    # 5. the hardware itself
    m = _machine()
    if m.get("cpu", 0) >= CPU_BUSY_PCT:
        return {"busy": True, "why": f"the CPU is at {m['cpu']:.0f}%",
                "detail": f"background work waits above {CPU_BUSY_PCT:.0f}%"}
    if m.get("gpu", 0) >= GPU_BUSY_PCT:
        return {"busy": True, "why": f"the GPU is at {m['gpu']:.0f}%",
                "detail": f"background work waits above {GPU_BUSY_PCT:.0f}%"}
    # 6. and THIS FILE'S disk, which is the question the queue asks
    if disk:
        try:
            from . import gate
            if disk in gate.plex_disks():
                return {"busy": True, "why": f"a viewer is on {disk}",
                        "detail": "the transcode queue steers around this "
                                  "spindle for the same reason"}
            if disk in gate.busy_disks():
                return {"busy": True, "why": f"{disk} is busy",
                        "detail": "a rebuild, a balance or a copy has that "
                                  "disk - the other eleven are free"}
        except Exception:                                        # noqa: BLE001
            pass
    return {"busy": False, "why": "", "detail": "",
            "cpu": m.get("cpu", 0), "gpu": m.get("gpu", 0)}


# ------------------------------------------------------------- the runner ---
async def run(key: str, title: str, pending, do_one, label=None, *,
              empty_s: float = EMPTY_S, pause_s: float = PAUSE_S,
              system_name: str = "", disk_of=None, lanes: int = LANES) -> None:
    r"""Work through `pending()` for as long as the machine can spare it.

    NO BATCH, NO END. This is a loop that lives for the life of the process:
    it does one item, asks again, does the next. There is nothing to tune -
    the machine's own state is the throttle, and on an idle box that means it
    simply finishes.

    THE QUESTION IS ASKED BEFORE EVERY ITEM, not once per pass. A pass that
    lasts an hour and checks once at the start is a pass that ignores anybody
    who sits down to watch something in the other fifty-nine minutes.
    """
    d = state(key, title)
    lab = label or (lambda x: str(x))
    while True:
        try:
            b = await busy()
            if b["busy"]:
                d.update(running=False, paused=True,
                         paused_why=b["why"], now="")
                await asyncio.sleep(pause_s)
                continue
            d.update(paused=False, paused_why="")
            items = await asyncio.to_thread(pending)
            if not items:
                d.update(running=False, now="", total=0, done=0,
                         idle_why="nothing waiting",
                         next_look=time.time() + empty_s)
                await asyncio.sleep(empty_s)
                continue
            # THE FREE SPINDLES FIRST. A viewer on one disk used to mean the
            # runner stopped at the first file that lived there and waited
            # twenty seconds, over and over, while eleven other disks sat
            # idle with work on them. Sorting by whether the disk is spoken
            # for turns "paused" into "working on something else" - which is
            # what the transcode queue has always done, and the reason it
            # says "steering around them" rather than "held".
            if disk_of:
                try:
                    from . import gate
                    hot = set(gate.plex_disks()) | set(gate.busy_disks())
                    if hot:
                        items = sorted(
                            items, key=lambda x: (disk_of(x) or "") in hot)
                except Exception:                                # noqa: BLE001
                    pass
            d.update(running=True, idle_why="", total=len(items), done=0,
                     ok=0, failed=0, t0=time.time(), next_look=0.0,
                     lanes=max(1, int(lanes)), flight=[])

            queue = list(items)
            flight: dict = {}          # task -> {"disk","label","pct","t0"}
            halt = False

            def _note_item(it):
                return {"disk": (disk_of(it) if disk_of else ""),
                        "label": lab(it), "pct": 0.0, "t0": time.time()}

            def _show():
                """One place the panel reads from, however many are running."""
                live = [dict(v) for v in flight.values()]
                d["flight"] = [{"now": v["label"], "pct": v["pct"],
                                "disk": v["disk"]} for v in live]
                d["now"] = live[0]["label"] if live else ""
                d["item_pct"] = live[0]["pct"] if live else 0.0

            def _finish(task):
                v = flight.pop(task, None) or {}
                try:
                    res = task.result()
                    good = bool((res or {}).get("ok", True))
                except asyncio.CancelledError:
                    raise
                except Exception as e:                           # noqa: BLE001
                    good = False
                    d["last_error"] = f"{type(e).__name__}: {e}"
                d["done"] += 1
                d["ok" if good else "failed"] += 1
                took = max(0.001, time.time() - (v.get("t0") or time.time()))
                prev = d.get("secs_each") or 0.0
                d["secs_each"] = took if not prev else prev * .7 + took * .3

            while (queue or flight) and not halt:
                # ---- fill the free lanes ------------------------------------
                while len(flight) < max(1, int(lanes)) and queue and not halt:
                    taken = {v["disk"] for v in flight.values() if v["disk"]}
                    pick = None
                    for i, it in enumerate(queue):
                        dk = (disk_of(it) if disk_of else "")
                        # NEVER TWO ON ONE ARM. A second file on a spindle
                        # that is already being read is not a second lane, it
                        # is the same lane sharing itself.
                        if dk and dk in taken:
                            continue
                        pick = i
                        break
                    if pick is None:
                        break                      # only same-disk work left
                    it = queue.pop(pick)
                    dk = (disk_of(it) if disk_of else "")
                    b = await busy(dk)
                    # A BUSY DISK IS NOT A BUSY MACHINE. If the only thing in
                    # the way is this file's own spindle, step over it and
                    # take the next one - the sort above means there usually
                    # is a next one.
                    if b["busy"] and (b.get("why") or "").endswith(
                            dk or "\x00"):
                        d["skipped"] = (d.get("skipped") or 0) + 1
                        d["total"] = max(0, d["total"] - 1)
                        continue
                    if b["busy"]:
                        # PAUSED, NOT FAILED. Whatever is already in flight is
                        # allowed to finish - killing a half-written remux to
                        # be polite would leave more mess than it saves - but
                        # nothing new starts.
                        d.update(paused=True, paused_why=b["why"])
                        queue.insert(0, it)
                        halt = True
                        break

                    v = _note_item(it)

                    def _report(pct: float, _v=v) -> None:
                        """Called from the worker thread as the tool reports."""
                        try:
                            _v["pct"] = max(0.0, min(100.0, float(pct)))
                        except Exception:                        # noqa: BLE001
                            pass
                    task = asyncio.ensure_future(
                        asyncio.to_thread(do_one, it, _report))
                    flight[task] = v
                _show()
                if not flight:
                    break
                done_set, _ = await asyncio.wait(
                    list(flight.keys()), timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done_set:
                    _finish(task)
                _show()

            if halt:
                # Drain what was already running before reporting a pause.
                while flight:
                    done_set, _ = await asyncio.wait(
                        list(flight.keys()),
                        return_when=asyncio.FIRST_COMPLETED)
                    for task in done_set:
                        _finish(task)
                d.update(running=False, now="", item_pct=0.0, flight=[])
            elif not queue:
                # The list finished rather than being interrupted.
                d.update(running=False, now="", item_pct=0.0, flight=[],
                         last_run=time.time(), last_done=d["done"],
                         last_took=round(time.time() - d["t0"], 1),
                         runs=(d.get("runs") or 0) + 1)
                if d["done"]:
                    joblog.log(
                        f"{title}: {d['ok']} done"
                        + (f", {d['failed']} could not be" if d["failed"]
                           else "")
                        + f" in {round(time.time() - d['t0'])}s",
                        "warn" if d["failed"] else "info",
                        system=system_name or key)
            else:
                d.update(running=False, now="", item_pct=0.0, flight=[])
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # noqa: BLE001
            d["last_error"] = f"{type(e).__name__}: {e}"
            await asyncio.sleep(pause_s)


def progress(key: str) -> dict:
    r"""The shape every panel draws. Same keys for every system that adopts it."""
    d = dict(STATES.get(key) or {})
    if not d:
        return {"running": False, "paused": False, "total": 0, "done": 0}
    now = time.time()
    el = (now - d["t0"]) if (d.get("running") and d.get("t0")) else 0.0
    done, total = int(d.get("done") or 0), int(d.get("total") or 0)
    d["elapsed"] = round(el, 1)
    d["left"] = max(0, total - done)
    d["pct"] = round(min(100.0, (done / total * 100.0) if total else 0.0), 1)
    # RATE FROM THIS RUN, falling back to the smoothed per-item time so a
    # freshly started pass still has an estimate.
    rate = (done / el) if (el > 0.5 and done) else 0.0
    each = d.get("secs_each") or (1 / rate if rate else 0.0)
    d["rate"] = round(rate, 3)
    d["secs_each"] = round(each, 2)
    d["eta"] = round(d["left"] * each) if (d["left"] and each) else 0
    d["cpu_line"] = f"waits above {CPU_BUSY_PCT:.0f}% CPU or {GPU_BUSY_PCT:.0f}% GPU"
    # THE FILE IN FRONT OF IT, and how long it has been on it. Only while
    # running: a percentage left over from the last file would read as
    # progress that is not happening.
    if not d.get("running"):
        d["item_pct"] = 0.0
    d["item_elapsed"] = (round(time.time() - d["item_at"], 1)
                         if (d.get("running") and d.get("item_at")) else 0.0)
    return d
