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
import inspect
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
# And a ceiling on the whole box, nuarr's own work included. The line above
# deliberately ignores nuarr's share - otherwise a system paces itself against
# its own footprint - but "ignore our own load" cannot mean "there is always
# room". At this point there is not.
CPU_FULL_PCT = 96.0
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
    # WHOSE 92% IS IT.
    #
    # This read the whole box and compared it against 80, which is fine while
    # the only thing being paced is a remux that costs almost no CPU - and
    # falls apart the moment a system that DOES use the processor adopts this.
    # Caught within a minute of moving the decode check over: two ffmpeg
    # decodes put the box at 92%, the runner paused itself on its own load,
    # the load then vanished because it had paused, and it resumed - a system
    # oscillating against its own footprint and getting a fraction of the work
    # done it would have managed by ignoring the reading entirely.
    #
    # Same mistake as the disk column, with a different instrument: a program
    # that counts its own work as somebody else's will get out of its own way.
    # The question the gate exists to answer is "does anybody else need this
    # machine", so the figure is the box MINUS nuarr's own tree - and the
    # encoders are covered separately by the worker count above, the GPU by
    # its own line.
    #
    # A second rule survives underneath: whoever filled it, a box at 97% has
    # nothing to spare, and starting another decode there helps nobody.
    box = float(s.get("cpu_pct") or 0.0)
    mine = 0.0
    try:
        mine = float(((s.get("nuarr") or {}).get("cpu_pct")) or 0.0)
    except Exception:                                            # noqa: BLE001
        mine = 0.0
    return {"cpu": max(0.0, box - mine), "cpu_all": box, "cpu_mine": mine,
            "gpu": g}


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
        return {"busy": True,
                "why": f"something else has the CPU at {m['cpu']:.0f}%",
                # SHORT ENOUGH TO SURVIVE THE ROW IT IS SHOWN IN. The
                # running list clips a note at 140 characters, and a sentence
                # clipped mid-clause is worse than a shorter one.
                "detail": f"{m.get('cpu_all', 0):.0f}% in total, "
                          f"{m.get('cpu_mine', 0):.0f}% of it nuarr's own"}
    if m.get("cpu_all", 0) >= CPU_FULL_PCT:
        return {"busy": True,
                "why": f"the CPU is at {m['cpu_all']:.0f}%",
                "detail": "whoever filled it, there is nothing left to spare"}
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
              system_name: str = "", disk_of=None, lanes: int = LANES,
              goto: str = "", on_pass=None, note_of=None) -> None:
    r"""Work through `pending()` for as long as the machine can spare it.

    WHAT A CALLER MAY HAND OVER
        pending()          the list, cheap enough to call each cycle
        do_one(x, report)  sync or async - a coroutine is awaited, anything
                           else is run on a thread, because the systems that
                           adopt this are split about evenly and neither kind
                           should have to pretend to be the other
        label(x)           what to show while it happens
        disk_of(x)         which spindle, so busy disks can be stepped around
        note_of(x)         what it is doing to that file, for the disk panel
        on_pass(d)         called when the list runs out, for systems that
                           hand their findings somewhere when a pass ends

    NO BATCH, NO END. This is a loop that lives for the life of the process:
    it does one item, asks again, does the next. There is nothing to tune -
    the machine's own state is the throttle, and on an idle box that means it
    simply finishes.

    THE QUESTION IS ASKED BEFORE EVERY ITEM, not once per pass. A pass that
    lasts an hour and checks once at the start is a pass that ignores anybody
    who sits down to watch something in the other fifty-nine minutes.
    """
    d = state(key, title)
    if goto:
        d["goto"] = goto
    d["system_name"] = system_name or key
    lab = label or (lambda x: str(x))
    while True:
        try:
            b = await busy()
            if b["busy"]:
                d.update(running=False, paused=True,
                         paused_why=b["why"],
                         paused_detail=b.get("detail") or "", now="")
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
                     lanes=max(1, int(lanes)), flight=[], skipped=0,
                     skip_disks=[], paused_detail="")

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
                wk = v.get("task")
                if wk is not None:
                    wk.close()
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
                        # NAMED, NOT COUNTED. "3 skipped" is a number you can
                        # only shrug at; "stepped over NU-DRIVE-1" is the same
                        # fact and answers the next question with it.
                        d["skipped"] = (d.get("skipped") or 0) + 1
                        sk = d.get("skip_disks")
                        if not isinstance(sk, list):
                            sk = d["skip_disks"] = []
                        if dk and dk not in sk:
                            sk.append(dk)
                        d["total"] = max(0, d["total"] - 1)
                        continue
                    if b["busy"]:
                        d["paused_detail"] = b.get("detail") or ""
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
                    # ONE LEDGER SLOT PER ITEM, CLAIMED HERE SO NOBODY HAS TO.
                    #
                    # The disk panel splits every spindle into Nuarr and "not
                    # us", and a background system that does not appear in the
                    # first column appears in the second - which is the gate's
                    # own number. The sidecar sweep had to claim its slot by
                    # hand, and every system adopting this runner would have
                    # had to copy that. The runner already knows the file, the
                    # disk and when it started, so it claims it: a module only
                    # has to hand over the pid of whatever tool it spawns, and
                    # it finds the slot on the reporter it was given.
                    wk = claim(system_name or key, v["label"],
                               now=v["label"], disk=v["disk"],
                               note=((note_of(it) if note_of else "") or title))
                    v["task"] = wk
                    _report.task = wk

                    async def _go(_it=it, _rep=_report):
                        r = do_one(_it, _rep)
                        # A COROUTINE IS AWAITED WHERE IT IS. Half these
                        # systems are async because they shell out to ffmpeg
                        # and half are sync because they read the database;
                        # sending a coroutine to a thread would return the
                        # coroutine object and call it a result.
                        if inspect.isawaitable(r):
                            return await r
                        return r
                    if inspect.iscoroutinefunction(do_one):
                        task = asyncio.ensure_future(_go())
                    else:
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
                # WHAT HAPPENS WHEN THE LIST RUNS OUT. Several of these
                # systems do not merely find things, they hand what they found
                # to somebody - the remedy, an arr, the queue - and that step
                # belongs at the end of a pass rather than after every file.
                if on_pass is not None:
                    try:
                        r = on_pass(d)
                        if inspect.isawaitable(r):
                            await r
                    except Exception as e:                       # noqa: BLE001
                        d["last_error"] = f"{type(e).__name__}: {e}"
            else:
                d.update(running=False, now="", item_pct=0.0, flight=[])
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # noqa: BLE001
            d["last_error"] = f"{type(e).__name__}: {e}"
            await asyncio.sleep(pause_s)


# ------------------------------------- what nuarr is doing that is not a job --
#
# "WHAT IS NUARR DOING ON THIS SPINDLE" HAD ONE ANSWER AND IT WAS INCOMPLETE.
#
# The pool panel splits every disk three ways - Nuarr, viewer, system - and
# system means "not us", which is the half the gate steers around. Only the
# transcode queue could ever put anything in the Nuarr column, because that is
# the only place that kept per-job byte counters. So a sidecar remux, which
# reads a 40 GB file off NU-DRIVE-9 and writes it back, showed up as SYSTEM:
# the panel said "no jobs here" on the disk nuarr was busy rewriting, and
# blamed the traffic on something outside the program.
#
# That is not a display problem. "System" is the number the gate reacts to,
# and a system that mistakes its own work for somebody else's will steer away
# from a disk it is itself using, or hold the queue for a load that is its own.
# The scanner already had this fixed by hand - a disk being walked counts as
# ours - and every new background system would have needed the same patch.
#
# So there is one ledger. Anything doing real disk work outside the queue
# claims a slot here, hands over the pid of whatever tool it spawned, and is
# measured exactly the way a worker measures its ffmpeg: psutil io_counters,
# same smoothing, same floor. The panel, the gate and the disk report all read
# from it, so there is no way for them to disagree.
TASKS: dict = {}
_TASK_N = [0]
_SAMPLE_AT = [0.0]
SAMPLE_EVERY = 0.5
IO_FLOOR = 200_000.0          # under this it is noise, same as a worker's


class Task:
    """One piece of background work, measured the way a job is measured."""

    __slots__ = ("id", "system", "title", "now", "disk", "dest_disk", "pid",
                 "read_bps", "write_bps", "since", "note", "pct",
                 "_last", "_moved")

    def __init__(self, system, title, now="", disk="", dest_disk="", note=""):
        _TASK_N[0] += 1
        self.id = _TASK_N[0]
        self.system, self.title = str(system or ""), str(title or "")
        self.now, self.note = str(now or ""), str(note or "")
        self.disk, self.dest_disk = str(disk or ""), str(dest_disk or "")
        self.pid = 0
        self.pct = 0.0
        self.read_bps = self.write_bps = 0.0
        self.since = time.time()
        self._last = None
        self._moved = None

    # The tool is spawned after the slot is claimed, so the pid arrives late.
    def set_pid(self, pid) -> None:
        self.pid = int(pid or 0)
        self._last = None

    def sample(self) -> None:
        if not self.pid:
            return
        try:
            import psutil
            io = psutil.Process(self.pid).io_counters()
        except Exception:                                        # noqa: BLE001
            # The tool has exited. Whatever it was moving, it is not moving it
            # now - and a rate left standing is a rate that lies.
            self.read_bps = self.write_bps = 0.0
            self.pid = 0
            return
        now = time.time()
        if self._last:
            t0, r0, w0 = self._last
            dt = now - t0
            if dt < 0.5:
                return
            ir = max(0.0, (io.read_bytes - r0) / dt)
            iw = max(0.0, (io.write_bytes - w0) / dt)
            self.read_bps = 0.3 * ir + 0.7 * (self.read_bps or ir)
            self.write_bps = 0.3 * iw + 0.7 * (self.write_bps or iw)
            if self.read_bps < IO_FLOOR:
                self.read_bps = 0.0
            if self.write_bps < IO_FLOOR:
                self.write_bps = 0.0
        self._last = (now, io.read_bytes, io.write_bytes)

    def moved(self, copied: int, note: str = "", pct: float = -1.0) -> None:
        r"""Bytes written by hand, for the phases that spawn no tool.

        THE SECOND HALF OF A REWRITE HAS NO CHILD PROCESS. mkvmerge builds the
        new file on the cache, and then nuarr itself copies it back onto the
        pool - tens of gigabytes, moved by this process, with no pid of its
        own to point psutil at. Measuring only the tool would have fixed half
        the mislabelling and left the other half reading "system", which is
        the half that actually lands on the media disk.

        The copy already reports its progress for the commit bar; the same
        callback carries the byte count, so the rate is a subtraction rather
        than a new measurement.
        """
        now = time.time()
        if note:
            self.note = note
        if pct >= 0:
            self.pct = max(0.0, min(100.0, float(pct)))
        if self._moved:
            t0, b0 = self._moved
            dt = now - t0
            if dt < 0.5:
                return
            rate = max(0.0, (int(copied) - b0) / dt)
            self.write_bps = 0.3 * rate + 0.7 * (self.write_bps or rate)
            if self.write_bps < IO_FLOOR:
                self.write_bps = 0.0
        self._moved = (now, int(copied))

    def close(self) -> None:
        TASKS.pop(self.id, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def claim(system, title, now="", disk="", dest_disk="", note="") -> Task:
    """Register a piece of background work. Close it when it ends."""
    t = Task(system, title, now, disk, dest_disk, note)
    TASKS[t.id] = t
    return t


def _sample_all() -> list:
    """Every live task, refreshed at most twice a second."""
    live = list(TASKS.values())
    now = time.time()
    if now - _SAMPLE_AT[0] >= SAMPLE_EVERY:
        _SAMPLE_AT[0] = now
        for t in live:
            try:
                t.sample()
            except Exception:                                    # noqa: BLE001
                pass
    return live


def tasks() -> list[dict]:
    """The same shape a worker reports itself in, so one renderer draws both."""
    out = []
    for t in _sample_all():
        out.append({
            "job_id": f"bg{t.id}", "background": True,
            "system": t.system, "title": t.title or t.now, "file": t.now,
            "pool": "background", "stage": t.note or t.system,
            "stage_s": round(time.time() - t.since),
            "elapsed_s": round(time.time() - t.since),
            "progress": round(max(0.0, min(1.0, (t.pct or 0.0) / 100.0)), 3),
            "paused_for_viewer": False, "paused_for_load": False,
            "disk": t.disk, "dest_disk": t.dest_disk,
            "read_bps": round(t.read_bps), "write_bps": round(t.write_bps),
            "plan": t.note or "",
        })
    return out


def by_disk() -> dict:
    """{pool disk: {jobs, read_bps, write_bps, jobs_detail}} for background work."""
    out: dict = {}

    def row(lbl):
        return out.setdefault(lbl, {"disk": lbl, "jobs": 0, "read_bps": 0.0,
                                    "write_bps": 0.0, "jobs_detail": []})
    for d in tasks():
        src, dst = d["disk"], d["dest_disk"]
        # THE READ AND THE WRITE ARE ON DIFFERENT SPINDLES FOR MOST OF THIS.
        # A remux reads the file off the pool and writes the new one to the
        # cache, which is deliberately not a pool disk - so during that phase
        # there is a read to claim here and a write that belongs to a drive
        # this panel does not draw. Claiming it anyway would credit nuarr with
        # bytes it did not put on that spindle, and the number this subtracts
        # from is the one the gate steers by. An empty dest means "somewhere
        # that is not the pool", and the write is simply not claimed.
        if src:
            e = row(src)
            e["read_bps"] += d["read_bps"]
            e["jobs"] += 1
            e["jobs_detail"].append(d)
            if dst == src:
                e["write_bps"] += d["write_bps"]
        if dst and dst != src:
            row(dst)["write_bps"] += d["write_bps"]
    return out


def bps_by_label() -> dict:
    """Total bytes a second this is putting on each pool disk."""
    out: dict = {}
    for d in tasks():
        rate = (d["read_bps"] or 0) + (d["write_bps"] or 0)
        if rate <= 0:
            continue
        for lbl in (d["disk"], d["dest_disk"]):
            if lbl:
                out[lbl] = out.get(lbl, 0.0) + rate
    return out


def rw_by_label() -> dict:
    """(read, write) per pool disk, split the way the transfer detector needs."""
    out: dict = {}
    for d in tasks():
        r, w = float(d["read_bps"] or 0), float(d["write_bps"] or 0)
        src, dst = d["disk"], (d["dest_disk"] or d["disk"])
        if src:
            a, b = out.get(src, (0.0, 0.0))
            out[src] = (a + r, b + (w if dst == src else 0.0))
        if dst and dst != src:
            a, b = out.get(dst, (0.0, 0.0))
            out[dst] = (a, b + w)
    return out


def merge_stats(key: str, out: dict, *, live_keys=()) -> dict:
    r"""Overlay the runner's own progress onto a module's stats dict.

    ONE LINE PER PANEL, INSTEAD OF ONE BUG PER PANEL.
    
    Every system that moves onto this runner leaves its old STATE dict behind
    as a husk - true only while its manual button runs - and every panel
    reading that husk then says "idle" while the disks are going. That is not
    hypothetical: it is exactly what happened to the sidecar sweep, which was
    missing from the running list for months because the list read a dict
    nothing wrote to any more.
    
    So the conversion is mechanical. A module keeps its own stats() for the
    things only it knows - how many are left, what it found, where its
    thresholds are - and hands the dict through here for the things the runner
    knows better: whether it is working, on what, how far through, how fast,
    and what it is waiting for.
    
    The module's own values survive when the runner has nothing to say, so a
    manual run still reports itself.
    """
    try:
        p = progress(key)
    except Exception:                                            # noqa: BLE001
        return out
    live = bool(p.get("running") or p.get("paused"))
    if live or not any(out.get(k) for k in ("running",) + tuple(live_keys)):
        out["running"] = live or bool(out.get("running"))
    if live:
        out["now"] = p.get("now") or out.get("now") or ""
        out["done"] = p.get("done") or 0
        out["total"] = p.get("total") or 0
        out["elapsed"] = p.get("elapsed") or 0.0
        out["rate"] = p.get("rate") or 0.0
        out["eta"] = p.get("eta") or 0
    # These are true whether or not it happens to be working this second.
    out["paused"] = bool(p.get("paused"))
    out["paused_why"] = p.get("paused_why") or ""
    out["paused_detail"] = p.get("paused_detail") or ""
    out["lanes"] = p.get("lanes") or 1
    out["flight"] = p.get("flight") or []
    out["skipped"] = p.get("skipped") or 0
    out["skip_disks"] = p.get("skip_disks") or []
    out["idle_why"] = p.get("idle_why") or ""
    out["next_look"] = p.get("next_look") or 0.0
    out["cpu_line"] = p.get("cpu_line") or ""
    for k in ("secs_each", "last_run", "last_took", "runs"):
        v = p.get(k)
        if v:
            out[k] = v
    if p.get("last_done"):
        out.setdefault("last_checked", p["last_done"])
    if p.get("last_error"):
        out["last_error"] = p["last_error"]
    return out


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
    d["cpu_line"] = (f"waits above {CPU_BUSY_PCT:.0f}% CPU from anything else"
                     f" (or {CPU_FULL_PCT:.0f}% in total) or "
                     f"{GPU_BUSY_PCT:.0f}% GPU")
    # THE FILE IN FRONT OF IT, and how long it has been on it. Only while
    # running: a percentage left over from the last file would read as
    # progress that is not happening.
    if not d.get("running"):
        d["item_pct"] = 0.0
    d["item_elapsed"] = (round(time.time() - d["item_at"], 1)
                         if (d.get("running") and d.get("item_at")) else 0.0)
    return d
