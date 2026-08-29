r"""
nuarr - job queue and workers

WHAT THIS FIXES ABOUT TDARR
---------------------------
* Tdarr starts work regardless of what else the box is doing. Here every
  dispatch asks the gate first (Plex busy? balancer running? arr renaming?).
* Tdarr's progress is a percentage with no context. Here each worker reports
  the file, the plan, fps, speed, ETA and the live ffmpeg line.
* Tdarr commits by renaming over the original. Here the output is written to
  NVMe cache, verified, and only then swapped in through fileops - which waits
  for locks and can roll back.
* Cancelling in Tdarr can leave a .tmp behind. Here cancellation kills the
  process AND removes the partial file.

Worker counts come from workers.py and are re-read on every dispatch, so raising
or lowering them takes effect immediately - no restart, like Tdarr's +/- but
honest about the NVENC ceiling.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import psutil
import re
import shlex
import sqlite3
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import dvfix, fileops, gate, joblog, rules, workers  # noqa: F401  (rules used in snapshot)
from .config import NO_WINDOW, SETTINGS
from .db import cursor, log_event

# ---------------------------------------------------------------- state ----
RUNNING: dict[str, "Worker"] = {}

# Single background thread for best-effort stage bookkeeping - see
# Worker.set_stage. One worker, so transitions persist in the order they
# happened; daemon, so it never holds up shutdown.
_STAGE_DB = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage-db")
QUEUE: list["Job"] = []
_lock = asyncio.Lock()
_pump_task: asyncio.Task | None = None
PAUSED_REASON: str | None = None

# A SHRINK must save at least this fraction to be committed, otherwise the
# output is discarded and the original kept.
MIN_SAVING = 0.05

# A COMPATIBILITY conversion is allowed to grow - see the size gate in
# _transcode(). 1.75 rather than something tighter because the encoder is
# already capped at maxrateFactor (1.5) x the source bitrate, so a healthy
# conversion lands at or below ~150%; measured AV1 -> HEVC came in at 100-140%.
# This ceiling is not a size policy, it is a "the encode went wrong" detector.
MAX_GROWTH = 1.75

REFRESH_DEBOUNCE_S = 120.0
_LAST_REFRESH: dict[tuple[str, int], float] = {}
_refresh_lock = asyncio.Lock()

# ---------------------------------------------------------------- batch ----
# WHY THIS IS NOT "average seconds per file".
#
# The first version averaged wall-clock per finished job. It reported "8s per
# file" and a 2-minute ETA for 61 queued files while NVENC sat pinned at 99%,
# because a SKIPPED file finishes in well under a second and was averaged in
# with real encodes. The mean then lurched every time the mix of skips to
# encodes shifted - which is exactly the bouncing.
#
# Two files are also not equal units of work: a 22-minute episode and a 2-hour
# remux differ by ~6x. So measure THROUGHPUT as media-seconds processed per
# wall second, per pool, and size the remaining queue in media-seconds too.
# That is stable because it does not depend on how the queue is ordered.
BATCH: dict = {
    "started_at": None,
    "done": 0, "skipped": 0,
    "pools": {},            # pool -> {media_s, wall_s, rate, n}
    "media_done_s": 0.0,
    "eta_shown": None,
}
_EWMA_ALPHA = 0.25          # a new sample moves the rate by at most a quarter
_ETA_DAMP = 0.3             # the displayed ETA eases toward the computed one


def _pool_stat(pool: str) -> dict:
    return BATCH["pools"].setdefault(
        pool, {"media_s": 0.0, "wall_s": 0.0, "rate": None, "n": 0})


def _batch_note_finish(pool: str, started_at: float, media_s: float,
                       did_work: bool) -> None:
    """Fold one finished job into the batch throughput figures.

    `media_s` is the DURATION OF THE FILE processed, not how long it took.
    A skip counts toward completion but never toward the rate - it consumed no
    encoder time, and letting it into the average is what produced the nonsense
    estimate.
    """
    if BATCH["started_at"] is None:
        return
    BATCH["done"] += 1
    BATCH["media_done_s"] += max(0.0, media_s or 0.0)
    if not did_work:
        # No encoder ran, so this says nothing about throughput. The caller
        # decides whether it was a legitimate skip or a failure - conflating
        # the two hid 20 real failures inside a "skipped" count.
        return
    wall = max(0.001, time.time() - started_at)
    st = _pool_stat(pool or "encode")
    st["media_s"] += max(0.0, media_s or 0.0)
    st["wall_s"] += wall
    st["n"] += 1
    sample = max(0.0, media_s or 0.0) / wall      # media seconds per wall second
    if sample <= 0:
        return
    st["rate"] = sample if st["rate"] is None else (
        _EWMA_ALPHA * sample + (1 - _EWMA_ALPHA) * st["rate"])


def _batch_reset_if_idle(queued: int, running: int) -> None:
    if queued == 0 and running == 0:
        BATCH.update(started_at=None, done=0, skipped=0, failed=0, pools={},
                     media_done_s=0.0, eta_shown=None)
    elif BATCH["started_at"] is None:
        BATCH.update(started_at=time.time(), done=0, skipped=0, failed=0, pools={},
                     media_done_s=0.0, eta_shown=None)


def _queued_work() -> tuple[dict, float]:
    """Remaining media-seconds per pool, plus a fallback per-file duration.

    Files that have not been probed yet have no duration, so they are charged
    the average of what this library actually contains rather than treated as
    free - which would make the ETA collapse as soon as the queue is deep.
    """
    per_pool: dict[str, float] = {}
    with cursor() as cur:
        rows = cur.execute(
            "SELECT j.pool pool, "
            "       SUM(COALESCE(f.duration,0)) known_s, "
            "       SUM(CASE WHEN COALESCE(f.duration,0)<=0 THEN 1 ELSE 0 END) unknown "
            "FROM jobs j LEFT JOIN files f ON f.id=j.file_id "
            "WHERE j.state='queued' GROUP BY j.pool").fetchall()
        avg = cur.execute(
            "SELECT AVG(duration) d FROM files WHERE duration > 0").fetchone()
    typical = float((avg["d"] if avg else 0) or 0) or 1800.0     # 30 min default
    for r in rows:
        per_pool[r["pool"] or "encode"] = (float(r["known_s"] or 0)
                                           + float(r["unknown"] or 0) * typical)
    return per_pool, typical


def _overall(queued: int, workers: list["Worker"]) -> dict:
    running = len(workers)
    total = BATCH["done"] + running + queued
    if not total:
        return {"active": False}

    remaining, typical = _queued_work()

    # in-flight: only the part of each running file still left to do
    for w in workers:
        pool = w.pool or "encode"
        dur = w.duration or typical
        left = max(0.0, 1.0 - min(max(w.progress or 0.0, 0.0), 1.0)) * dur
        remaining[pool] = remaining.get(pool, 0.0) + left

    done_s = BATCH["media_done_s"] + sum(
        min(max(w.progress or 0.0, 0.0), 1.0) * (w.duration or typical)
        for w in workers)
    total_s = done_s + sum(remaining.values())
    fraction = (done_s / total_s) if total_s > 0 else 0.0
    # The media fraction under-reports when finished jobs contributed no
    # media_s (the subocr duration gap: 1,458 done rendered as "RUN 0.1%").
    # The count fraction is coarser but cannot be gaslit by a zero - take
    # whichever says more work has actually happened.
    fraction = max(fraction, BATCH["done"] / total if total else 0.0)

    # Pools drain in parallel, so the run ends when the SLOWEST one finishes.
    eta, per_pool_eta = None, {}
    for pool, left_s in remaining.items():
        rate = _pool_stat(pool)["rate"]
        if not rate or rate <= 0:
            continue
        per_pool_eta[pool] = left_s / (rate * max(1, _capacity(pool)))
    if per_pool_eta and len(per_pool_eta) == len([p for p, v in remaining.items() if v > 0]):
        eta = max(per_pool_eta.values())
    elif per_pool_eta:
        # some pool has no measured rate yet - show what we know, but it will
        # only rise as that pool reports, so flag it rather than pretend
        eta = max(per_pool_eta.values())
    elif workers and not queued:
        known = [w.eta_s for w in workers if w.eta_s]
        if known:
            eta = max(known)

    # STATISTICAL FALLBACK. "measuring throughput..." with 1,458 files done is
    # absurd: the batch's own history IS a throughput measurement. When no
    # pool has a media-seconds rate (a whole batch of jobs whose cost does not
    # track runtime, or a batch started before durations were recorded), fall
    # back to files-per-hour over the batch's own wall clock. Coarser than the
    # media rate - skips and monsters average together - but a real number
    # beats a shrug once enough files have finished to damp the noise.
    eta_basis = "media" if eta is not None else None
    if eta is None and BATCH["done"] >= 8 and BATCH["started_at"]:
        elapsed = time.time() - BATCH["started_at"]
        if elapsed > 60:
            files_per_s = BATCH["done"] / elapsed
            if files_per_s > 0:
                # running jobs count as half-done on average
                eta = (queued + running * 0.5) / files_per_s
                eta_basis = "files"

    # Ease the displayed value rather than snapping. One slow file should nudge
    # the estimate, not redraw it.
    if eta is not None:
        prev = BATCH["eta_shown"]
        BATCH["eta_shown"] = eta if prev is None else (
            _ETA_DAMP * eta + (1 - _ETA_DAMP) * prev)
        eta = BATCH["eta_shown"]

    rates = {p: round(s["rate"], 2) for p, s in BATCH["pools"].items()
             if s.get("rate")}
    return {"active": True, "done": BATCH["done"], "running": running,
            "queued": queued, "total": total,
            "skipped": BATCH.get("skipped", 0),
            "failed": BATCH.get("failed", 0),
            "fraction": round(min(1.0, max(0.0, fraction)), 4),
            "eta_s": round(eta) if eta is not None else None,
            "eta_basis": eta_basis,
            "remaining_media_s": round(sum(remaining.values())),
            "rates": rates,
            "started_at": BATCH["started_at"]}

_PROGRESS_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?(?:time=\s*(\d+:\d+:\d+\.\d+)).*?speed=\s*([\d.]+)x",
    re.S)
_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):([\d.]+)")


@dataclass
class Job:
    id: str
    file_id: int
    path: str
    title: str
    kind: str = "transcode"        # transcode | remux | ocr_forced | repair
    priority: int = 100
    plan: object | None = None
    disk: str = ""                 # pool disk holding the source, from _claim
    created_at: float = field(default_factory=time.time)


class NothingToDo(ValueError):
    """The probe says this file needs no work, so no job was created.

    Subclasses ValueError because every existing caller already catches that
    for "could not queue this one" and counts it as skipped - which is exactly
    what this is. They keep working unchanged; only the reporting improves.
    """


# How many files the enqueue path has decided against queueing, for the UI.
SKIPPED_EARLY: dict = {"n": 0, "last": ""}


def _human_bytes(n) -> str:
    """1.4 GB, not 1503238553. Used wherever a raw count would reach a human."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "— bytes"
    for unit, size in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20),
                       ("KB", 1 << 10)):
        if n >= size:
            v = n / size
            # One decimal below 100 keeps it precise without being noisy; above
            # that the fraction is meaningless next to a moving counter.
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{int(n)} B"


@dataclass
class Worker:
    job: Job
    pool: str                      # encode | passthrough
    started_at: float
    proc: object | None = None
    # True while this job is deliberately frozen because a viewer on
    # its spindle is short of buffer. Shown on the card so a stopped
    # progress bar reads as a decision rather than as a stall.
    paused_for_viewer: bool = False
    fps: float = 0.0
    speed: float = 0.0
    progress: float = 0.0          # 0..1
    eta_s: float | None = None
    duration: float = 0.0
    last_line: str = ""
    cancelled: bool = False
    # WHAT IS IT DOING RIGHT NOW.
    # ffmpeg reaching 100% is not the job finishing - the commit, verify, arr
    # refresh and rename all happen afterwards and can take longer than the
    # stream copy did. Without this the UI sat at "99.7% - ETA 0s" for minutes,
    # which reads as stuck rather than busy.
    stage: str = "starting"
    stage_at: float = 0.0
    _spd: float = 0.0                  # smoothed speed, for a stable ETA
    # DISK ACTIVITY. A stream copy is pure I/O, so bytes/second is the number
    # that explains its behaviour - "7.15x" means nothing on its own, but
    # "12 MB/s when this file normally moves at 200" says the pool is the
    # bottleneck. Tracked from ffmpeg's own total_size counter.
    out_bytes: int = 0
    est_out_bytes: int = 0
    src_bytes: int = 0                 # source size, for a shrink/grow read
    # WHICH POOL DISK the source lives on. With 12 letterless disks behind
    # DrivePool, an aggregate throughput figure cannot tell you whether four
    # jobs are spread across four spindles or all queued on one - and that is
    # the difference between a busy pool and a bottlenecked disk.
    disk: str = ""
    dest_disk: str = ""                # where DrivePool actually put the result
    # COMMIT PROGRESS. The commit is a full copy of the finished file onto the
    # pool - frequently the longest single step in a job - and it reported
    # nothing at all, so the card sat on "placing…" for minutes. These carry
    # the same three facts the encode already shows: where it is going, how
    # fast it is going there, and how long is left.
    commit_bytes: int = 0
    commit_total: int = 0
    commit_bps: float = 0.0
    commit_phase: str = ""             # staging | copying | verifying
    # SIDE OCR, running on a CPU thread beside ffmpeg. It has its own progress
    # and nothing was showing it: the row reported the encode while a second
    # piece of work ran invisibly, and only once ffmpeg exited did the wait
    # admit it existed. These two carry it for the whole job, so "why is this
    # one taking longer than the others" has an answer on the card.
    sub_ocr_frac: float = 0.0
    sub_ocr_stage: str = ""
    sub_ocr_active: bool = False       # holds one of the subocr pool's slots
    # SUBTITLE RESCUE - extracting the tracks ffmpeg could not read and turning
    # them into SRT. Separate from the OCR pair above on purpose: they are
    # different work with different failure modes, and a job can do both. Same
    # shape so the card can render them with the same strip.
    sub_fix_frac: float = 0.0
    sub_fix_stage: str = ""
    _cm_bytes: int = 0
    _cm_at: float = 0.0
    # ffmpeg's out_time does not advance on every file - see read_progress().
    # When it stalls, progress is derived from bytes written instead, and this
    # says so, because a byte-derived percentage on a stream copy is an
    # estimate rather than a measurement and the UI should not pretend
    # otherwise.
    by_bytes: bool = False
    _last_done: float = 0.0
    _stuck: int = 0
    write_bps: float = 0.0             # smoothed, bytes/sec written
    read_bps: float = 0.0              # from the OS, per process
    _last_bytes: int = 0
    _last_bytes_at: float = 0.0
    _last_io: tuple = ()

    def set_stage(self, name: str) -> None:
        """Record which phase this job is in. Persisted, but never on the loop.

        The comment here used to say "about six per job". The loop guard
        measured 104 writes in 22 seconds: callers re-assert the current stage
        from the progress path, so almost every one of those UPDATEs was
        rewriting a value that had not changed. Two fixes, in order of value:

          1. Skip the no-ops. stage_at now means "when this stage BEGAN", which
             is what it was always read as anyway.
          2. Do the write on a single background thread. This is best-effort
             bookkeeping - the original swallowed every exception rather than
             disturb a running job - so it has no business blocking the event
             loop. One thread, not a pool, because stage transitions have to
             land in the order they happened.
        """
        if self.stage == name:
            return
        self.stage = name
        self.stage_at = time.time()
        # Persist it. This is what lets a restart tell "was still encoding"
        # from "encode finished, was mid-commit" - the difference between
        # redoing an hour of work and finishing a copy.
        jid = self.job.id

        def _persist() -> None:
            try:
                with cursor() as cur:
                    cur.execute("UPDATE jobs SET stage=? WHERE job_id=?",
                                (name, jid))
            except Exception:
                pass                # never let bookkeeping break a running job

        try:
            _STAGE_DB.submit(_persist)
        except Exception:
            pass

    def _decay_io(self) -> None:
        """Let the rates fall to zero when nothing is moving any more.

        A rate is a claim about NOW. Leaving the last value standing turns it
        into a claim about the past that never expires.
        """
        self.read_bps *= 0.5
        self.write_bps *= 0.5
        if self.read_bps < 200_000:
            self.read_bps = 0.0
        if self.write_bps < 200_000:
            self.write_bps = 0.0

    def sample_io(self) -> None:
        """Read this job's own disk counters, both halves, every poll.

        ffmpeg reports what it WROTE; only the OS knows what it READ. On a
        stream copy the read side is the larger half - the source is pulled off
        a pool disk in full - so without this the picture is half missing.
        Called from the snapshot path, which is already once per poll.

        BOTH RATES COME FROM HERE NOW, and that is the fix for a real bug.
        write_bps used to be differenced from ffmpeg's own total_size inside
        the progress loop, which only updates when the byte count GROWS - so
        the moment ffmpeg finished and the job moved on to an inline subtitle
        OCR, the last value froze and stayed on screen. Encanto sat at
        "read 207.5 MB/s write 202.4 MB/s" on a spindle the disk counters
        reported as 1% busy.
        #
        # That was not only wrong on the card. gate._our_bps_by_key() subtracts
        # these figures from each disk's measured throughput to work out how
        # much of the load is NOT nuarr's - so a phantom 410 MB/s would mask
        # real external activity and stop the busy check ever firing on that
        # spindle. One source of truth, and it decays on its own because the
        # instantaneous rate genuinely goes to zero.
        """
        proc = self.proc
        pid = getattr(proc, "pid", None)
        if not pid:
            # ffmpeg has exited - whatever it was moving, it is not moving it
            # now. This is the OCR case: the process is gone and the job lives
            # on doing CPU work.
            self._decay_io()
            return
        try:
            import psutil
            io = psutil.Process(pid).io_counters()
            now = time.time()
            if self._last_io:
                t0, r0, w0 = self._last_io
                dt = now - t0
                if dt >= 0.5:
                    ir = max(0.0, (io.read_bytes - r0) / dt)
                    iw = max(0.0, (io.write_bytes - w0) / dt)
                    # Same smoothing as before, but applied EVERY sample rather
                    # than only on the samples that moved - which is what makes
                    # it fall as well as rise.
                    self.read_bps = 0.3 * ir + 0.7 * (self.read_bps or ir)
                    self.write_bps = 0.3 * iw + 0.7 * (self.write_bps or iw)
                    if self.read_bps < 200_000:
                        self.read_bps = 0.0
                    if self.write_bps < 200_000:
                        self.write_bps = 0.0
                    self._last_io = (now, io.read_bytes, io.write_bytes)
            else:
                self._last_io = (now, io.read_bytes, io.write_bytes)
        except Exception:
            # the process may have exited between poll and read - not an error,
            # but the rates are no longer true either
            self._decay_io()

    def why_pool(self) -> str:
        """Plain-English reason this job is in the pool it is in."""
        if self.pool == "subocr":
            return ("Reading picture subtitles into text. Background work — it "
                    "waits behind everything else and stays off any disk "
                    "someone is watching from.")
        if self.pool == "handler":
            return ("Repairing the file first, so it is not rebuilt and then "
                    "changed again underneath.")
        p = self.job.plan
        if self.pool == "encode":
            return ("Rebuilding the picture on the graphics card. This is the "
                    "slow kind of job, so only a few run at once.")
        bits = []
        if p is not None:
            if getattr(p, "strip_dv", False):
                bits.append("removing the Dolby Vision layer")
            ops = [o for o in getattr(p, "audio_ops", []) if o.get("to") != "copy"]
            if ops:
                bits.append(f"converting {len(ops)} audio track"
                            f"{'s' if len(ops) > 1 else ''}")
            if getattr(p, "keep_subs", None) is not None and p.clear_flags:
                bits.append("tidying subtitle settings")
        detail = ", ".join(bits) if bits else "changing tracks and settings only"
        return (f"The picture is untouched — {detail}. Copying at disk speed, "
                f"no quality lost.")

    def as_dict(self) -> dict:
        el = time.time() - self.started_at
        p = self.job.plan
        return {
            "job_id": self.job.id, "pool": self.pool, "title": self.job.title,
            "file": os.path.basename(self.job.path), "kind": self.job.kind,
            "path": self.job.path,
            "why_pool": self.why_pool(),
            "actions": [{"kind": a.kind, "what": a.what, "why": a.why,
                         "detail": a.detail} for a in getattr(p, "actions", [])],
            "plan": self.job.plan.summary() if self.job.plan else "",
            # WHICH SILICON IS DOING THIS ONE. The encoder is a per-library
            # choice that can silently fall back, so the card has to say what
            # is really running rather than leave it to be inferred from the
            # settings page - those two can legitimately differ.
            "venc": ({"family": (getattr(p, "venc", None) or {}).get("family"),
                      "encoder": (getattr(p, "venc", None) or {}).get("encoder"),
                      "preset": (getattr(p, "venc", None) or {}).get("preset"),
                      "cq": (getattr(p, "venc", None) or {}).get("cq"),
                      "why": (getattr(p, "venc", None) or {}).get("family_why")}
                     if getattr(p, "encode", False) else None),
            "fps": round(self.fps, 1), "speed": round(self.speed, 2),
            "progress": round(self.progress, 4),
            # A frozen progress bar with no explanation reads as a hang, and
            # this one is deliberate - so it has to say so on the card.
            "paused_for_viewer": bool(getattr(self, "paused_for_viewer", False)),
            # Only report an ETA while ffmpeg is actually running. Carrying the
            # last value into the commit/rename stages produced a stuck
            # "ETA 0s" that looked like a hang.
            "eta_s": (round(self.eta_s) if self.eta_s and self.stage == "encoding"
                      else None),
            "elapsed_s": round(el), "last_line": self.last_line,
            "cancelled": self.cancelled,
            "stage": self.stage,
            "stage_s": round(time.time() - self.stage_at) if self.stage_at else 0,
            "out_bytes": self.out_bytes,
            "est_out_bytes": self.est_out_bytes,
            "src_bytes": self.src_bytes,
            # Whether growing is the POINT of this job. Some conversions trade
            # size for compatibility on purpose - an unplayable 8 GB file that
            # becomes a playable 9 GB one is a success - and without this the
            # card can only warn about every increase equally.
            "grow_ok": bool(getattr(p, "grow_ok", False)),
            "disk": self.disk,
            "dest_disk": self.dest_disk,
            "write_bps": round(self.write_bps),
            "read_bps": round(self.read_bps),
            "io_bps": round(self.write_bps + self.read_bps),
            "commit_bytes": self.commit_bytes,
            "commit_total": self.commit_total,
            "commit_bps": round(self.commit_bps),
            "commit_phase": self.commit_phase,
            "sub_ocr_frac": round(self.sub_ocr_frac, 4),
            "sub_ocr_stage": self.sub_ocr_stage,
            # Whether an INLINE OCR is running on this job. The card needs to
            # distinguish "no disk rates because the phase is pure CPU" from
            # "no disk rates because something is wrong", and the pool alone
            # cannot say - this is a passthrough job doing OCR work, not a
            # member of the subocr pool.
            "sub_ocr_active": bool(self.sub_ocr_active),
            "sub_fix_frac": round(self.sub_fix_frac, 4),
            "sub_fix_stage": self.sub_fix_stage,
            "by_bytes": self.by_bytes,
            # Seconds left on the commit copy, from the smoothed rate. Computed
            # here rather than in the browser so it cannot disagree with the
            # numbers beside it.
            "commit_eta_s": (
                round((self.commit_total - self.commit_bytes) / self.commit_bps)
                if self.commit_bps > 0 and self.commit_total > self.commit_bytes
                else None),
        }


# ------------------------------------------------------------- helpers -----
def _hms(t: str) -> float:
    m = _TIME_RE.search(t)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def _ffmpeg_exe() -> str:
    """nuarr's own ffmpeg if installed, else the configured fallback.

    Resolved per call rather than cached: a staged build is applied while the
    server runs, and a cached path would keep launching the old binary until a
    restart - which is exactly the sort of silent staleness this module exists
    to remove.
    """
    try:
        from . import ffmpeg_update
        return ffmpeg_update.installed_paths()[0]
    except Exception:
        return SETTINGS.ffmpeg


def _ffprobe_exe() -> str:
    try:
        from . import ffmpeg_update
        return ffmpeg_update.installed_paths()[1]
    except Exception:
        return SETTINGS.ffprobe


async def probe(path: str) -> dict | None:
    """ffprobe one file. Cached into the DB so a rescan does not re-probe."""
    proc = await asyncio.create_subprocess_exec(
        _ffprobe_exe(), "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=NO_WINDOW)
    out, _ = await proc.communicate()
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception:
        return None


def cache_probe(file_id: int, data: dict) -> None:
    """Record a probe: useful fields on the row, raw output in a side table.

    The raw JSON used to be a column on `files`. At ~4.6 KB a row it had reached
    70 MB - over half the database - to store something nothing ever reads back;
    the decision logic in rules.decide() and handlers.suggest_kinds() runs off
    the live probe dict, not the stored copy. It is kept, separately, purely so
    a specific file can still be diagnosed after the fact, and it ages out on
    its own retention clock in maintenance.py.
    """
    v = next((s for s in data.get("streams", [])
              if s.get("codec_type") == "video"), {})
    fmt = data.get("format", {})
    now = time.time()
    with cursor() as cur:
        cur.execute(
            "UPDATE files SET probed_at=?, video_codec=?, height=?, "
            "duration=?, bitrate=?, audio_codecs=?, audio_langs=?, "
            "sub_langs=? WHERE id=?",
            (now, v.get("codec_name"),
             v.get("height"), float(fmt.get("duration") or 0) or None,
             int(fmt.get("bit_rate") or 0) or None,
             ",".join(sorted({s.get("codec_name", "") for s in data.get("streams", [])
                              if s.get("codec_type") == "audio"})),
             track_langs(data, "audio"), track_langs(data, "subtitle"),
             file_id))
        cur.execute(
            "INSERT INTO file_probes(file_id, json, at) VALUES(?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET json=excluded.json, at=excluded.at",
            (file_id, json.dumps(data, separators=(",", ":")), now))
    # A NEW PROBE MEANS A NEW FILE, AND TRACK NUMBERS MOVE WHEN TRACKS ARE
    # DROPPED. Any language verdict recorded against the old layout now points
    # at a different track, so it is deleted rather than left to rot: seven
    # TaleSpin episodes were reported as mislabelled because a "track 0 is
    # Chinese" result survived a remux that removed sixteen tracks.
    try:
        from . import audiolang
        audiolang.invalidate(file_id)
    except Exception:                                    # noqa: BLE001
        pass


# ------------------------------------------------------------ dispatch -----
def track_langs(d: dict, kind: str) -> str:
    r"""Language tag per track, IN TRACK ORDER, as one small string.

    Three screens wanted "which languages are in this library" and each
    answered it by SELECTing all 39,563 probe blobs and json.loads()-ing them -
    about 188 MB of JSON parsed per request, three times over. The answer is a
    few dozen bytes per file and only changes when the file is re-probed, so it
    belongs in a column, exactly as `audio_codecs` already is.

    Order is preserved and a blank tag is written as "-" rather than dropped:
    position IS the track number everywhere else in nuarr, and a missing tag is
    the single most important thing this string records.

    MODULE LEVEL, not nested in _store_probe, because three places re-probe a
    file and only one of them was updating the column. A header edit refreshed
    file_probes and left audio_langs saying "-" for a track that now had a
    tag - so the Audio language page kept reporting files it had just fixed as
    untagged. One writer, called from all of them.
    """
    out = []
    for s in d.get("streams", []):
        if s.get("codec_type") != kind:
            continue
        lg = ((s.get("tags") or {}).get("language") or "").strip().lower()[:3]
        out.append(lg if lg and lg not in ("und", "unk", "non") else "-")
    return ",".join(out)


def refresh_track_langs(file_id: int, data: dict) -> None:
    """Re-extract the language columns after a file has been re-probed."""
    try:
        with cursor() as cur:
            cur.execute("UPDATE files SET audio_langs=?, sub_langs=? WHERE id=?",
                        (track_langs(data, "audio"),
                         track_langs(data, "subtitle"), file_id))
    except Exception:                                    # noqa: BLE001
        pass


def _capacity(pool: str) -> int:
    w = workers.get()
    if pool == "encode":
        return w.encode_workers
    if pool == "subocr":
        # ITS OWN POOL, because its two phases pull in opposite directions:
        # OCR is single-threaded CPU with no disk at all, while the mux and
        # commit are pure pool I/O. Sharing the handler's cap meant the
        # CPU-bound half inherited a limit set for the I/O-bound one, and on a
        # 10-core box measured at 3% load that left nine cores idle while one
        # ground through 5,264 files.
        return max(1, getattr(w, "subocr_workers", 4))
    if pool == "handler":
        # Repairs are mostly single-threaded CPU/disk work and several of them
        # rewrite files in place, so run them narrowly.
        return max(1, min(2, w.passthrough_workers))
    return w.passthrough_workers


def _handlers_pending() -> bool:
    """Any handler work still queued or running?

    Kept because a database from before the handler subsystem was removed can
    still hold queued pool='handler' rows, and an encode must not start on a
    file one of them is about to rewrite. Those rows are retired the moment
    they reach a worker, so this drains to permanently False.
    """
    if any(w.pool == "handler" for w in RUNNING.values()):
        return True
    with cursor() as cur:
        return bool(cur.execute(
            "SELECT 1 FROM jobs WHERE state='queued' AND pool='handler' LIMIT 1"
        ).fetchone())


def _in_pool(pool: str) -> int:
    n = sum(1 for x in RUNNING.values() if x.pool == pool)
    if pool == "subocr":
        # OCR RUNNING INSIDE A TRANSCODE COUNTS AS OCR.
        #
        # The header read "SUBOCR 0/10" while a card below it showed an OCR at
        # 31%, because a side OCR runs on a thread owned by an encode or
        # passthrough worker and never joined the subocr pool. That was not
        # only a lying number: the cap was being exceeded. Four encode plus six
        # passthrough workers can each start one, so up to TEN tesseract
        # processes could run outside a pool whose limit is ten - twenty at
        # once on a box already sitting at 95% CPU. Counting them here makes
        # the display honest AND gives _side_ocr_allowed() something real to
        # check, so the number the user set is the number they get.
        n += sum(1 for x in RUNNING.values() if x.sub_ocr_active)
    return n


def _side_ocr_allowed() -> bool:
    """Is there room in the OCR budget for one more, started inline?

    Checked and acted on without awaiting in between, so two transcodes
    starting in the same tick cannot both see the last slot.
    """
    return _in_pool("subocr") < _capacity("subocr")


async def enqueue(file_id: int, path: str, title: str = "",
                  kind: str = "transcode", priority: int = 100,
                  source: str = "manual") -> Job:
    """Decide the plan now, then persist the job.

    THE QUEUE IS THE DATABASE. An earlier version kept it in a Python list,
    which meant a CLI enqueue built a queue inside a short-lived process and
    lost it on exit while the server's list stayed empty - six jobs queued and
    none ever ran. Persisting also means a restart resumes the queue.

    Planning at enqueue time (rather than at dispatch) has two payoffs: the pool
    is known up front so NVENC work is routed correctly instead of everything
    falling into passthrough, and the UI can show you what WILL happen before
    anything starts.
    """
    # Last line of defence. The scanner already filters exclusions, but a job
    # can also be raised from a webhook, a requeue or the CLI, and none of those
    # go through the scanner.
    from . import scanner as _sc
    if _sc.is_excluded(path):
        raise ValueError(f"path is excluded from nuarr: {path}")

    # ONE LIVE JOB PER FILE. A bulk queue overlapping an earlier one, or a
    # webhook arriving while a file is already queued, produced two rows for
    # the same file - and the second could only ever fail, because the first
    # holds the file open. Reuse the existing job instead of stacking another.
    if file_id:
        with cursor() as cur:
            dup = cur.execute(
                "SELECT job_id, state FROM jobs WHERE file_id=? "
                "AND state IN ('queued','running') LIMIT 1", (file_id,)).fetchone()
        if dup:
            raise ValueError(
                f"already {dup['state']} as job {dup['job_id']}: {path}")

    job_id = uuid.uuid4().hex[:12]
    # Always derive the display name from the DB so it carries the episode.
    # Callers pass a bare series title, which produced "Naruto Shippuden" with
    # no episode in the finished list - useless when 500 episodes share it.
    if file_id:
        from .db import display_label
        with cursor() as cur:
            row = cur.execute("SELECT title, season, episode FROM files WHERE id=?",
                              (file_id,)).fetchone()
        if row and row["title"]:
            title = display_label(row["title"], row["season"], row["episode"])
    title = title or os.path.basename(path)

    # PRE-FLIGHT BEFORE ANY WORK.
    # We commit by replacing the file in place, so if the existing path is
    # already past the Windows limit the commit can never succeed. Found this
    # the expensive way: a job ran a full remux and only then failed with
    # "target path too long (266 chars)". Checking here turns a wasted encode
    # into an instant, explained skip - which matters because ~2,243 files in
    # Anime Shows are over the limit.
    if fileops.path_too_long(path, SETTINGS.max_path_length) \
            and not SETTINGS.allow_long_paths:
        reason = (f"path is {len(path)} chars, over the {SETTINGS.max_path_length} "
                  f"limit - shorten the naming format in Profilarr first")
        with cursor() as cur:
            cur.execute(
                "INSERT INTO jobs(job_id,file_id,kind,state,priority,pool,path,"
                "title,error,created_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, file_id, kind, "blocked", priority, "none", path, title,
                 reason, time.time(), time.time()))
            cur.execute("UPDATE files SET state='blocked', state_reason=? WHERE id=?",
                        (reason, file_id))
        joblog.log(f"BLOCKED: {title} - {reason}", "warn", job_id)
        return Job(id=job_id, file_id=file_id, path=path, title=title, kind=kind)

    plan = None
    # Non-transcode work gets its own pool so it can be drained BEFORE any
    # encode starts, and a lower priority number so it sorts first.
    pool = ("passthrough" if kind == "transcode"
            else "subocr" if kind == "sub_ocr" else "handler")
    if kind != "transcode":
        priority = min(priority, 50)
    # ...EXCEPT subtitle OCR, which is explicitly background work. The clamp
    # above exists because repairs and pool maps are quick and want to jump the
    # queue; sub_ocr is 5,264 files and ~10 TB of remuxing and must never
    # elbow a transcode aside. It keeps whatever (high) priority it was given
    # so it sorts LAST within the handler pool.
    if kind == "sub_ocr":
        priority = max(priority, 90)
    if kind == "transcode":
        data = await probe(path)
        if data:
            cache_probe(file_id, data)
            anime = rules.is_anime(path)
            try:
                plan = rules.decide(data, anime=anime,
                                    filename=os.path.basename(path),
                                    size_bytes=os.path.getsize(path),
                                    orig_lang=await _orig_lang(file_id),
                                    file_id=file_id)
                pool = "encode" if plan.encode else "passthrough"
            except Exception as e:
                joblog.log(f"planning failed: {e}", "error", job_id)

            # NOTHING TO DO? THEN DO NOT QUEUE IT.
            #
            # The probe has just answered the only question a job for this file
            # would ask. Queueing it anyway meant the worker claimed it, ran
            # ffprobe a SECOND time, re-planned, reached the same conclusion and
            # finished as 'skipped'.
            #
            # Measured on this library: 19,258 of 21,517 completed jobs -
            # 89.5% - were exactly that. Each cost a probe here plus ~1.6 s of
            # dispatch and a second probe there, and left a job row behind. Of
            # the 707 queued at the time, 515 (72.8%) were no-ops waiting to
            # discover they had no work.
            #
            # Marking the file done here is the same thing _finish() does for a
            # skipped job, so the file leaves the eligible pool exactly as
            # before - which also stops auto-queue from re-probing it forever
            # on every pass.
            if plan is not None and (plan.skip_reason or not plan.needed):
                why = plan.skip_reason or "no work needed"
                with cursor() as cur:
                    cur.execute("UPDATE files SET state='done', "
                                "state_reason=?, processed_at=? WHERE id=?",
                                (why, time.time(), file_id))
                log_event(file_id, "skipped", plan.summary(), label=title)
                SKIPPED_EARLY["n"] = SKIPPED_EARLY.get("n", 0) + 1
                SKIPPED_EARLY["last"] = title or os.path.basename(path)
                joblog.log(f"no work needed, not queued: {title} - "
                           f"{plan.summary()}", "debug")
                raise NothingToDo(f"{why}: {path}")

    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO jobs(job_id,file_id,kind,state,priority,pool,path,"
                "title,plan_json,created_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, file_id, kind, "queued", priority, pool, path, title,
                 json.dumps(plan.to_dict()) if plan else None, time.time(),
                 source))
    except sqlite3.IntegrityError as e:
        # ux_jobs_live_file. Another caller queued this same file during the
        # seconds we spent probing and planning it - the check above passed and
        # was then overtaken. Report it exactly like the early duplicate check
        # so every caller keeps treating it as "already queued", not a failure.
        if "ux_jobs_live_file" in str(e) or "jobs.file_id" in str(e):
            raise ValueError(f"already queued by another pass: {path}") from None
        raise

    job = Job(id=job_id, file_id=file_id, path=path, title=title, kind=kind,
              priority=priority, plan=plan)
    joblog.log(f"queued [{pool}]: {title}"
               + (f" - {plan.summary()}" if plan else ""), "info", job_id)
    return job


# Why a worker slot is sitting idle, so "3/4 running" is explainable rather
# than looking like the dispatcher is stuck.
#
# WHAT IS STORED HERE IS THE *REASON*, NOT THE PROGRESS.
#
# `at` used to be a snapshot of the blocking job's percentage, taken at the
# instant a claim was refused and then never touched again - the entry is only
# rewritten if the dispatcher happens to try that disk once more. Note a wait
# while the job in the way has just started and the panel reads
#
#     WAITING NU-DRIVE-1 behind a job at 0% · starts at 85%
#
# for as long as that job runs, while the card two inches below it plainly
# shows 2.7% and climbing. Observed on NU-DRIVE-1 (said 0%, was 2.7%) and
# NU-DRIVE-0 (said 72%, was 74.8%) in the same frame.
#
# So the percentage is no longer stored. `need` is a threshold and does not
# move; `since` is when the wait began. The blocking job's progress is read
# live from RUNNING at the moment the snapshot is built - see _live_disk_waits.
DISK_WAIT: dict[str, dict] = {}


# WHICH POOLS ACTUALLY CONTEND FOR A SPINDLE.
#
# Module level, not nested inside _claim(). It was defined in there, and
# _live_disk_waits() below - which runs from the API snapshot, a completely
# different call path - referenced it and would have raised NameError the
# moment a wait existed, taking /api/jobs down with it. It read as working
# only because an empty DISK_WAIT never reaches the line.
#
# An encode is GPU-bound and barely touches the disk; a stream copy reads and
# writes the whole file at whatever the spindle allows. Only the second one
# makes a second job on the same disk crawl.
def _heavy(pool_name: str, plan_obj=None) -> bool:
    """Does this pool's work hammer a pool spindle?

    subocr belongs here as much as passthrough does. It reads the whole file
    to extract the subtitle, writes a full mkvmerge remux, and then commits it
    back - the same disk profile as a stream copy. Leaving it out meant the
    "one heavy job per spindle" rule did not apply to it, so several parallel
    OCR jobs could all land on the same disk and thrash it.
    """
    return pool_name in ("passthrough", "subocr")


def _note_disk_wait(disk: str, need_pct: float, why: str = "progress") -> None:
    """Record WHY this spindle is held, not how far along the job on it is.

    `why` separates the two very different holds that both landed here:
      progress - another heavy job is on this disk and has not reached the
                 threshold yet. A percentage is the right thing to show.
      viewer   - Plex is streaming from this disk. There is no threshold; it
                 clears when the viewer stops, and rendering that as
                 "starts at 100%" invited the reader to wait for a number that
                 was never going to arrive.
    """
    prev = DISK_WAIT.get(disk, {})
    DISK_WAIT[disk] = {"disk": disk, "need": round(need_pct, 1), "why": why,
                       "since": prev.get("since") or time.time()}


def _clear_disk_wait(disk: str) -> None:
    DISK_WAIT.pop(disk, None)


def _live_disk_waits(workers) -> list[dict]:
    """Waits, with the blocking job's progress read at snapshot time.

    Also self-correcting: a recorded wait is only reported if there is STILL a
    heavy job on that spindle short of the threshold. Previously an entry could
    outlive the condition - the dispatcher clears it on a successful claim, but
    nothing cleared it when the queue simply stopped offering that disk - and
    the panel kept advertising a wait that was over.
    """
    disks = {w.disk for w in workers}
    out = []
    for d in DISK_WAIT.values():
        disk = d["disk"]
        if disk not in disks:
            continue
        why = d.get("why") or "progress"
        if why == "viewer":
            # No job has to finish for this to clear - the viewer does. Report
            # it without a percentage rather than inventing one.
            if disk not in _plex_disks_safe():
                continue                  # viewer moved on; the hold is over
            out.append({"disk": disk, "why": why, "at": None,
                        "need": None, "since": d.get("since")})
            continue
        on = [w for w in RUNNING.values()
              if w.disk == disk and _heavy(w.pool)]
        if not on:
            continue                      # nothing is in the way any more
        at = min((w.progress or 0.0) for w in on) * 100.0
        need = d.get("need") or 0.0
        if need and at >= need:
            continue                      # threshold reached; the wait is over
        out.append({"disk": disk, "why": why, "at": round(at, 1), "need": need,
                    "since": d.get("since")})
    return out


# What a job costs the spindle it is on. A stream copy reads and writes a whole
# file at whatever the disk allows; an encode is GPU-bound - measured at ~4 MB/s
# aggregate against ~47 MB/s for passthrough - but still reads its source.
COPY_WEIGHT = 3
ENCODE_WEIGHT = 1


# ffmpeg's negative AVERROR codes, arriving as unsigned exit statuses.
#
# A failure read "ffmpeg exited 3199971767", which is a real answer rendered
# unusable. It is -1094995529 as a signed 32-bit int, and FFmpeg builds its
# error constants as FFERRTAG(a,b,c,d) = -MKTAG(...) - so that value spells
# 'INDA', AVERROR_INVALIDDATA. The number was carrying the reason the whole
# time; nobody could read it.
_AVERROR = {
    -1094995529: ("invalid data found while reading the source",
                  "the file is damaged, or has a stream ffmpeg cannot parse"),
    -1414092869: ("end of file reached unexpectedly",
                  "the source is truncated - the download or copy is incomplete"),
    -541478725:  ("end of file", "nothing left to read from the source"),
    -1163346256: ("stream not found", "the input has no stream matching the map"),
    -1179861752: ("decoder not found", "no decoder for a stream in this file"),
    -1128613112: ("encoder not found", "the requested encoder is unavailable"),
    -12:         ("out of memory", "the encode could not allocate memory"),
    -2:          ("no such file or directory", "the source moved or was deleted"),
    -13:         ("permission denied", "the file is locked or not readable"),
    -22:         ("invalid argument", "ffmpeg rejected the command nuarr built"),
    # AVERROR(ENOSYS). Almost always the muxer refusing a stream it has no
    # codec for - "Subtitle codec 0 is not supported" - which is a track
    # ffmpeg could not read on the way in, not anything wrong with the audio
    # the error appears to come from.
    -40:         ("a stream the output format cannot hold",
                  "usually a subtitle track ffmpeg could not read from the "
                  "source container"),
}


def _ffmpeg_exit(rc: int) -> str:
    """Turn an ffmpeg exit status into something a human can act on."""
    signed = rc - (1 << 32) if rc >= (1 << 31) else rc
    hit = _AVERROR.get(signed)
    if hit:
        return f"ffmpeg failed - {hit[0]} ({hit[1]}) [code {signed}]"
    # Unknown, but the FFERRTAG shape is still worth surfacing: four printable
    # characters means it IS an AVERROR, just not one listed above.
    tag = (-signed) & 0xFFFFFFFF
    chars = "".join(chr((tag >> (8 * i)) & 0xFF) for i in range(4))
    if signed < 0 and all(32 <= ord(c) < 127 for c in chars):
        return f"ffmpeg failed - AVERROR '{chars}' [code {signed}]"
    return f"ffmpeg exited {rc}"


def disk_load(running=None) -> dict[str, int]:
    """How contended each pool disk is right now, as a score per disk.

    Replaces a set of "busy" disks that counted passthrough jobs only. Two
    things were invisible to that:

      * an encode scored NOTHING, so a stream copy would start on the exact
        spindle an encode was already reading from - encode and passthrough
        both on NU-DRIVE-7 - and neither pool ever stepped around the other;
      * only the SOURCE disk counted, while the COMMIT writes a whole file back
        into the pool. That is the heaviest thing a job does and the scheduler
        could not see the disk it was landing on.

    A score rather than a flag because the right answer is an ordering, not a
    veto: an empty disk beats one carrying an encode, which beats one carrying
    a copy. Taken as a parameter so it can be tested without a live RUNNING.
    """
    running = RUNNING.values() if running is None else running
    load: dict[str, int] = {}

    def add(disk: str, n: int) -> None:
        if disk:
            load[disk] = load.get(disk, 0) + n

    for w in running:
        # By _heavy(), not by name. This said == "passthrough", so a subocr
        # job scored ENCODE_WEIGHT=1 - under the busy threshold of 3 - and its
        # spindle never counted as busy. Result, observed: two subocr jobs
        # grinding the same NU-DRIVE-11 while eleven disks sat idle. Not a
        # claim race - registration was already synchronous - the guard just
        # could not see the first job. _heavy() is the single definition of
        # "hammers its spindle" and already includes subocr.
        # A JOB IN THE OCR PHASE IS NOT ON THE DISK.
        #
        # _heavy() answers "does this POOL hammer a spindle", which is the
        # right question at claim time and the wrong one for a job already
        # running: an inline subtitle OCR has finished its copy, ffmpeg has
        # exited, and what is left is pure single-threaded CPU reading pictures
        # into text - measured at 1% busy on the spindle it was still scoring
        # as fully occupied. Holding a disk closed for the several minutes that
        # takes idles it for nothing.
        #
        # Detected from the MEASURED RATES, not from the stage string and not
        # from the process handle. Stages are free text and several of them
        # mean this; and w.proc is not None during the OCR either - it holds
        # the OCR's own process. What is unambiguous is that the job has
        # stopped moving bytes, which is the thing being decided about.
        # Reliable only because sample_io() now lets the rates fall; while
        # write_bps was frozen at its last ffmpeg value this test would have
        # been permanently false.
        ocr_only = (getattr(w, "sub_ocr_active", False)
                    and getattr(w, "stage", "") != "committing"
                    and (getattr(w, "read_bps", 0) or 0)
                        + (getattr(w, "write_bps", 0) or 0) < 5_000_000)
        add(getattr(w, "disk", ""),
            0 if ocr_only
            else (COPY_WEIGHT if _heavy(getattr(w, "pool", ""))
                  else ENCODE_WEIGHT))
        # The commit is a full-file copy INTO the pool, whatever the pool was.
        if getattr(w, "stage", "") == "committing":
            add(getattr(w, "dest_disk", "") or getattr(w, "disk", ""),
                COPY_WEIGHT)
    return load


def _claim(pool: str) -> Job | None:
    """Atomically take the next queued job for a pool.

    The guarded UPDATE (state must still be 'queued') means two dispatchers can
    never run the same job, even though only the server pumps today.
    """
    # HOW BUSY IS EACH SPINDLE, as a number rather than a yes/no.
    #
    # This used to be a set of "busy" disks built from passthrough jobs only,
    # on the reasoning that an encode is GPU-bound (~4 MB/s measured) while a
    # stream copy is pure I/O (~47 MB/s), so encodes can share a disk happily.
    # Two things were wrong with it in practice:
    #
    #   * an encode counted for NOTHING, so a passthrough would start on the
    #     exact spindle an encode was already reading from - visible as encode
    #     and passthrough both on NU-DRIVE-7 - and neither pool would ever step
    #     around the other;
    #   * only the SOURCE disk counted. During its commit a job writes a whole
    #     file back into the pool, which is the most disk-heavy thing it does,
    #     and that target disk was invisible to the scheduler entirely.
    #
    # A score fixes both without over-constraining. A disk with an encode on it
    # is worse than an empty one but better than a disk with a stream copy, so
    # work spreads out in the right order instead of being either forbidden or
    # ignored.
    load = disk_load()
    # Kept for the wait decision below, which is still about genuine
    # copy-on-copy contention rather than mere occupancy.
    busy = {d for d, n in load.items() if n >= COPY_WEIGHT}

    # _heavy is module level now - see the note beside DISK_WAIT.

    # Disks a Plex viewer is reading from are excluded in the QUERY, not by
    # rejecting the row after it is chosen. The selector only ever fetches one
    # candidate, so a post-hoc rejection would stall the whole pool whenever
    # that single row happened to sit on the viewer's disk, while thousands of
    # queued files on the other eleven disks waited for nothing.
    with cursor() as cur:
        # NEVER claim a second job for a file already being processed.
        # A duplicate row (bulk enqueue overlapping an earlier one) got claimed
        # while its sibling was mid-encode, hit the lock check against its own
        # ffmpeg, deferred, and was re-claimed seconds later - a loop of
        # "deferred - file is in use by FFmpeg (pid N)" that never resolved
        # because the holder was us.
        live_files = {w.job.file_id for w in RUNNING.values() if w.job.file_id}
        base = ("SELECT j.id,j.job_id,j.file_id,j.kind,j.priority,j.path,"
                "       j.title,j.plan_json, f.pool_disk AS pool_disk "
                "FROM jobs j LEFT JOIN files f ON f.id = j.file_id "
                "WHERE j.state='queued' AND j.pool=? ")
        if live_files:
            base += ("AND (j.file_id IS NULL OR j.file_id NOT IN ("
                     + ",".join(str(int(i)) for i in live_files) + ")) ")
        # PICK THE LEAST-LOADED SPINDLE, THEN QUEUE ORDER.
        #
        # The old shape was two queries: "anything not on a busy disk" and, if
        # that found nothing, "anything at all". A hard filter followed by a
        # blind fallback - so a disk was either forbidden or invisible, with no
        # way to express "this one is free, that one already has an encode on
        # it, take the free one". With consecutive episodes of a series living
        # in one folder and DrivePool keeping them together, a run of queued
        # files shares a spindle, and the fallback fired constantly.
        #
        # One query instead, ordered by how loaded each candidate's disk
        # already is. It is still only a preference - if everything left is on
        # a loaded disk it takes the best available rather than idling a worker
        # - but now the preference has gradations instead of a cliff.
        #
        # A Plex viewer's disk stays a hard exclusion in the WHERE clause: that
        # is a correctness rule, not a scheduling preference.
        # SPINDLES SOMETHING ELSE IS HAMMERING, treated exactly like a
        # viewer's disk: excluded in the query rather than rejected after the
        # fact. This is the generic replacement for the DrivePool balance
        # hold - and it is strictly better, because a balance (or a backup,
        # or a parity check) that touches four disks now steers work onto the
        # other eight instead of stopping the queue outright.
        #
        # Never exclude EVERYTHING: if every disk is busy the gate has already
        # said so and holds globally, and an empty candidate set here would
        # just idle the workers for no extra benefit.
        # A VIEWER IS NOT AUTOMATICALLY A VETO ANY MORE.
        #
        # Excluding a viewer's spindle outright is right when the disk is the
        # bottleneck and wasteful when it is not - and measured here it usually
        # is not. One direct play pulls about 1.1 MB/s off a disk that does
        # 150+, leaving it at 1% busy while eleven other spindles carry the
        # whole queue. That is not caution, it is a disk sitting idle for no
        # reason.
        #
        # So the test becomes head room rather than presence. Below
        # viewer_share_pct the disk is shared - and anything running there is
        # demoted to Very Low I/O by io_priority_watch within a second, so the
        # viewer's reads overtake nuarr's rather than queueing behind them.
        # Above it, the old veto applies unchanged.
        #
        # Deliberately measured busy% and not the viewer's bitrate: what
        # matters is whether a read has to WAIT, and on a mechanical disk that
        # is a question about seeks and queue depth, not about megabytes.
        try:
            share_pct = float(workers.get().viewer_share_pct)
        except Exception:
            share_pct = 25.0
        busy_now = {}
        try:
            busy_now = {e["disk"]: (e.get("busy") or 0)
                        for e in (gate.disk_report().get("disks") or [])}
        except Exception:
            busy_now = {}
        watched = {d for d in gate.plex_disks()
                   if share_pct <= 0 or busy_now.get(d, 100) >= share_pct}
        # SPINDLES SOMETHING ELSE IS HAMMERING, steered around exactly like a
        # viewer's disk. This is the generic half of the old DrivePool balance
        # hold, and it is strictly better than one: a balance - or a backup, a
        # parity check, another app - that touches four disks now pushes work
        # onto the other eight instead of stopping the queue outright.
        try:
            hot = set(gate.busy_disks())
        except Exception:
            hot = set()

        # MEASURED LOAD AS A TIE-BREAK, not just as a veto.
        #
        # `load` counts nuarr's OWN jobs per disk, which is the right first
        # question - do not stack two of our own streams on one spindle - but
        # it is blind to everything else. A disk at 60% from a backup and one
        # sitting idle looked identical, so the queue picked between them by
        # priority and creation order, which is to say arbitrarily.
        #
        # Ranked in tiers rather than added to anything: the exact percentage
        # is not worth ordering by, and a 3%-versus-6% difference should not
        # outrank a job's priority. Three bands is enough to say "prefer the
        # quiet one" without pretending to a precision the figure does not have.
        busy_rank: dict[str, int] = {}
        try:
            for e in (gate.disk_report().get("disks") or []):
                b = e.get("busy") or 0
                busy_rank[e["disk"]] = 0 if b < 25 else (1 if b < 60 else 2)
        except Exception:
            busy_rank = {}

        def _pick(excl: set[str]):
            params: list = [pool]
            where_extra = ""
            if excl:
                qs = ",".join("?" * len(excl))
                where_extra = (f"AND (f.pool_disk IS NULL OR "
                               f"f.pool_disk NOT IN ({qs})) ")
                params += list(excl)
            terms = []
            if load:
                cases = " ".join("WHEN ? THEN ?" for _ in load)
                terms.append(f"CASE f.pool_disk {cases} ELSE 0 END")
                for d, n in load.items():
                    params += [d, n]
            if busy_rank:
                cases = " ".join("WHEN ? THEN ?" for _ in busy_rank)
                terms.append(f"CASE f.pool_disk {cases} ELSE 0 END")
                for d, n in busy_rank.items():
                    params += [d, n]
            ordr = ", ".join(terms + ["j.priority", "j.created_at"])
            return cur.execute(base + where_extra + f"ORDER BY {ordr} LIMIT 1",
                               tuple(params)).fetchone()

        row = _pick(watched | hot)
        if row is None and hot:
            # Nothing left off the hot disks. Busy is a PREFERENCE, not a
            # veto: if every remaining candidate lives on a loaded spindle,
            # falling back beats idling, and the global hold in
            # gate.check_disk_activity already covers the case where the whole
            # array is pinned. Viewer disks stay excluded either way - those
            # are a real "do not touch".
            row = _pick(watched)
        if not row:
            return None

        # WAIT RATHER THAN THRASH.
        # Everything left is on a spindle already running disk-heavy work.
        # Starting now interleaves two streams across one disk and both crawl -
        # 63 MB/s shared versus 997 MB/s spread. Hold until the incumbent is
        # nearly done, so the disk stays saturated by ONE stream instead.
        cand_disk = (row["pool_disk"] if "pool_disk" in row.keys() else "") or ""

        # SAME SPINDLE AS A VIEWER.
        # A Plex transcode no longer holds passthrough globally (see
        # gate.check_plex), because a stream copy needs no GPU. It does still
        # need the DISK, though, and reading a file off the same spindle Plex
        # is pulling from is what turns a smooth stream into buffering. So the
        # hold is narrowed from "every passthrough job" to "passthrough jobs on
        # the one disk in use".
        # Same head-room test as the exclusion above, applied to whatever the
        # query actually returned. Kept as a second check because the disk can
        # get busier between the two, and because a job claimed on a viewer's
        # disk is the one case where being a second late genuinely shows.
        if cand_disk and cand_disk in gate.plex_disks() \
                and (share_pct <= 0 or busy_now.get(cand_disk, 100) >= share_pct):
            _note_disk_wait(cand_disk, 0.0, why="viewer")
            return None

        if cand_disk and cand_disk in busy and _heavy(pool):
            try:
                wait_pct = float(workers.get().disk_wait_pct)
            except Exception:
                wait_pct = 85.0
            if wait_pct > 0:
                # SAME EXCLUSION AS disk_load(). A job whose ffmpeg has exited
                # and is grinding through an OCR is not competing for this
                # spindle, so waiting for its overall percentage to reach 85 -
                # a percentage that now advances at OCR speed, not disk speed -
                # holds the disk closed for minutes over work that finished.
                on_disk = [w for w in RUNNING.values()
                           if w.disk == cand_disk and _heavy(w.pool)
                           and not (getattr(w, "sub_ocr_active", False)
                                    and w.stage != "committing"
                                    and (w.read_bps or 0) + (w.write_bps or 0)
                                        < 5_000_000)]
                # let it through once the slowest job there is close to done
                nearly = all((w.progress or 0.0) * 100.0 >= wait_pct
                             for w in on_disk)
                if on_disk and not nearly:
                    _note_disk_wait(cand_disk, wait_pct, why="progress")
                    return None
        cur.execute("UPDATE jobs SET state='running', worker=?, started_at=? "
                    "WHERE id=? AND state='queued'",
                    (pool, time.time(), row["id"]))
        if cur.rowcount != 1:
            return None                      # someone else took it
    # we are claiming it, so any hold on that disk is over
    _clear_disk_wait((row["pool_disk"] if "pool_disk" in row.keys() else "") or "")
    plan = None
    if row["plan_json"]:
        try:
            plan = rules.plan_from_dict(json.loads(row["plan_json"]))
        except Exception:
            plan = None
    return Job(id=row["job_id"], file_id=row["file_id"], path=row["path"] or "",
               title=row["title"] or "", kind=row["kind"],
               priority=row["priority"], plan=plan,
               # carried so the Worker knows its spindle from the moment it is
               # registered - the busy set above is read before _run() starts,
               # so setting it later would leave a window where a claimed job
               # is invisible to the next claim.
               disk=(row["pool_disk"] or "") if "pool_disk" in row.keys() else "")


def queue_depth() -> int:
    with cursor() as cur:
        return cur.execute(
            "SELECT COUNT(*) n FROM jobs WHERE state='queued'").fetchone()["n"]


# Stages at which the ENCODE IS FINISHED and only the handover remains. A job
# killed here has a complete output sitting in the cache; requeuing it throws
# that away and redoes the expensive half for nothing.
_DONE_ENCODING = ("committing", "arr refresh / rename", "done")


def _cache_output(job_id: str) -> str:
    return os.path.join(SETTINGS.cache_dir, f"{job_id}.mkv")


def _finished_output(job_id: str, expect_size: int) -> tuple[bool, str]:
    r"""Is the cached output the COMPLETE encode we recorded?

    Completeness is not re-derived here, it is REMEMBERED. The stage only ever
    becomes 'committing' after ffmpeg has exited 0 and the output has passed
    the existing size checks, so reaching that stage is itself the proof that
    the encode finished. All this has to confirm is that the file still on disk
    is the same one - hence the exact byte match against the size recorded at
    that moment.

    The obvious alternative - probe the file and compare durations - was tried
    and does not work: on this library ffprobe reports duration=0 even for a
    correctly finalised remux, because the DV sources it copies from carry the
    broken timestamps that also stall ffmpeg's out_time. A completeness test
    that returns 0 for good files would have rejected every resume and quietly
    made this whole feature a no-op.
    """
    out = _cache_output(job_id)
    if not os.path.exists(out):
        return False, "no cached output"
    try:
        have = os.path.getsize(out)
    except OSError as e:
        return False, f"cannot stat cached output: {e}"
    if not have:
        return False, "cached output is empty"
    if not expect_size:
        return False, "no recorded output size to check against"
    if have != expect_size:
        return False, (f"cached output is {_human_bytes(have)}, expected "
                       f"{_human_bytes(expect_size)} - not the finished encode")
    return True, _human_bytes(have)


def _requeue_all() -> int:
    with cursor() as cur:
        cur.execute("UPDATE jobs SET state='queued', worker=NULL, started_at=NULL "
                    "WHERE state='running'")
        return cur.rowcount


def recover_interrupted() -> int:
    """Anything left 'running' from a previous process was interrupted.

    Most of them are requeued: an interrupted encode changed nothing in the
    library, so redoing it is safe.

    But a job killed DURING THE COMMIT has already finished the expensive part.
    The encode is complete and sitting in the cache; all that was left was
    copying it into place. Requeuing that re-encoded a 60 GB remux from scratch
    to produce a file that already existed. Those are handed to the commit
    queue instead, which is the machinery that already exists for exactly this
    - a finished output waiting for a swap that could not happen yet.
    """
    from . import commitqueue
    # This runs from jobs.start(), which is BEFORE web.py calls
    # commitqueue.init() - so the table may not exist yet. It also has to run
    # before purge_stale_cache(), because that sweep only spares cache files
    # the commit queue knows about; enqueueing later would mean the output was
    # already deleted.
    try:
        commitqueue.init()
    except Exception:
        return _requeue_all()

    resumed = 0
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT job_id, file_id, path, stage, size_before, size_after "
            "FROM jobs WHERE state='running'")]

    for r in rows:
        jid, stage = r["job_id"], (r["stage"] or "")
        if stage not in _DONE_ENCODING or not jid or not r["path"]:
            continue
        ok, why = _finished_output(jid, r["size_after"] or 0)
        if not ok:
            joblog.log(f"interrupted mid-{stage}, but the cached output is not "
                       f"usable ({why}) - re-running the job", "warn", jid)
            continue
        if not os.path.exists(r["path"]):
            joblog.log("interrupted mid-commit and the original is gone - the "
                       "swap may already have happened; re-running the job",
                       "warn", jid)
            continue
        # retry_now: nothing is holding this file, it was simply interrupted.
        # The default back-off is for locked targets and would leave a finished
        # encode idle in the cache for five minutes.
        commitqueue.enqueue(jid, r["file_id"], r["path"], _cache_output(jid),
                            r["size_before"] or 0, r["size_after"] or 0,
                            "interrupted by a restart during the commit",
                            retry_now=True)
        with cursor() as cur:
            cur.execute("UPDATE jobs SET state='deferred', worker=NULL "
                        "WHERE job_id=?", (jid,))
        joblog.log(f"encode was already complete ({why}) - resuming at the "
                   f"commit instead of re-encoding", "ok", jid)
        resumed += 1

    with cursor() as cur:
        cur.execute("UPDATE jobs SET state='queued', worker=NULL, started_at=NULL "
                    "WHERE state='running'")
        requeued = cur.rowcount
    if resumed:
        joblog.log(f"{resumed} interrupted commit(s) resumed without "
                   f"re-encoding", "ok")
    return requeued


async def pump() -> None:
    """Dispatch loop. Runs forever; cheap when idle."""
    global PAUSED_REASON
    while True:
        try:
            # A pending restart/shutdown stops new work at once, so the queue
            # drains instead of refilling while we wait for idle - otherwise a
            # "wait for idle" stop would never arrive.
            from . import lifecycle
            if lifecycle.PENDING["action"]:
                PAUSED_REASON = (f"{lifecycle.PENDING['action']} pending - "
                                 f"finishing {len(RUNNING)} running job(s)")
                await asyncio.sleep(3)
                continue

            if queue_depth():
                st = await gate.status()
                PAUSED_REASON = None if st.open else st.why()
                # PER-POOL gating. This used to be a single `if st.open`, so a
                # Plex transcode - which only contends for the GPU - stopped
                # stream copies as well, and the queue sat idle through an
                # entire film for no reason. Each pool now asks whether anything
                # holds IT specifically.
                if any(st.open_for(p) for p in
                       ("handler", "encode", "passthrough", "subocr")):
                    async with _lock:
                        # HANDLERS FIRST. OCR, repairs and flag fixes must finish
                        # before a transcode touches the same library, otherwise
                        # we burn GPU time encoding a file that is about to be
                        # rewritten by a repair - and the repair then collides
                        # with the encode's commit.
                        blocked = _handlers_pending()
                        # subocr goes LAST on purpose. It is the only pool here
                        # that is pure background: 5,264 files of work that must
                        # never delay an encode or a repair. It is also exempt
                        # from the handler block, because unlike a repair it
                        # does not race a transcode - _sub_ocr hands off to one
                        # when both apply.
                        for pool in ("handler", "encode", "passthrough", "subocr"):
                            if pool not in ("handler", "subocr") and blocked:
                                continue
                            if not st.open_for(pool):
                                continue
                            while _in_pool(pool) < _capacity(pool):
                                job = _claim(pool)
                                if not job:
                                    break
                                # Register the worker HERE, synchronously.
                                # _run() used to add it to RUNNING itself, but
                                # create_task only schedules - the coroutine had
                                # not executed yet, so _in_pool() still read 0
                                # and this loop kept claiming. Result: 24
                                # concurrent jobs against a limit of 4.
                                RUNNING[job.id] = Worker(job=job, pool=pool,
                                                         started_at=time.time(),
                                                         disk=job.disk)
                                asyncio.create_task(_run(job, pool))
            else:
                PAUSED_REASON = None
        except Exception as e:
            joblog.log(f"dispatch error: {type(e).__name__}: {e}", "error")
        # Poll faster when there is work to dispatch, slower while held - the
        # re-check interval only needs to be long when nothing can start anyway.
        try:
            nap = float(workers.get().gate_recheck_s) if PAUSED_REASON else 3.0
        except Exception:
            nap = 3.0
        await asyncio.sleep(max(1.0, nap))


def reap_orphan_encoders() -> dict:
    r"""Kill ffmpeg processes left behind by a previous nuarr process.

    When the server dies or is restarted mid-encode, its ffmpeg children keep
    running: Windows does not reap them, and they hold a read handle on the
    SOURCE file for as long as they live. The restarted server then re-queues
    those same jobs, the lock check correctly reports the file is in use, and
    every one of them defers - forever, because the orphan never finishes into
    anything. Symptom is a log full of
    "deferred - file is in use by FFmpeg command-line tools (pid N)".

    Identified by output path, not by name: only processes writing into our own
    cache directory are ours. Anything else - Plex, Tdarr, a manual ffmpeg - is
    left strictly alone.
    """
    import psutil

    cache = os.path.normcase(os.path.abspath(SETTINGS.cache_dir))
    killed, skipped = [], 0
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "ppid", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() not in ("ffmpeg.exe", "ffmpeg"):
                continue
            cmd = p.info["cmdline"] or []
            if not any(cache in os.path.normcase(str(a)) for a in cmd):
                skipped += 1
                continue          # not ours - never touch it
            ppid = p.info["ppid"]
            if ppid == me or psutil.pid_exists(ppid):
                continue          # a live parent owns it; leave it running
            out = next((a for a in reversed(cmd)
                        if cache in os.path.normcase(str(a))), None)
            p.kill()
            killed.append(p.info["pid"])
            if out and os.path.exists(out):
                try:
                    os.remove(out)      # partial output, nothing will finish it
                except OSError:
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        joblog.log(f"reaped {len(killed)} orphaned encoder(s) from a previous "
                   f"run (pids {', '.join(map(str, killed))}) — they were "
                   f"holding source files locked", "warn")
    return {"killed": killed, "left_alone": skipped}


# Set once the slow disk recovery below has finished. pump() is gated behind it,
# so nothing can claim a job while a half-committed file is still on the pool.
RECOVERED = asyncio.Event()
# WHAT THE GATE IS DOING, not just that it is closed. This step blocks the job
# queue and, on a library this size, spends minutes walking disks without a
# word - which looks exactly like a hang. Everything the boot pill needs to
# show progress lives here and is updated from the walk itself.
RECOVERY: dict = {
    "state": "pending",     # pending | running | done
    "note": "",             # the one-line summary, once there is one
    "phase": "",            # commit sweep | cache sweep
    "root": "",             # library root currently being walked
    "root_i": 0, "roots": 0,
    "where": "",            # folder within that root
    "dirs": 0, "files": 0, "found": 0,
    "started": 0.0, "elapsed": 0.0,
    "fixed": [],            # names of files actually restored
}


async def _recover_then_pump() -> None:
    """The slow half of startup, off the critical path.

    It used to run INSIDE start(), which uvicorn awaits before it binds the
    port - so the dashboard was unreachable for the whole of it and a restart
    looked identical to a crash. Nothing here needs to block HTTP: it needs to
    block JOBS, which is a much smaller promise, kept by RECOVERED below.

    Two phases, for a reason worth stating plainly. Walking the library through
    the POOL was measured at 8.3 directories/second against 1,560 on a member
    volume - an aggregate has to merge each listing across every disk. At that
    rate the full walk took over an hour and blocked the queue for all of it,
    on every restart, to conclude nothing was wrong. So the read-only question
    "is there anything to fix?" is now answered against the disks in seconds,
    and the repair walk only runs when the answer is yes.
    """
    t0 = time.time()
    try:
        # A kill during the final swap leaves the real file missing with both
        # halves beside it. Put it back before anything else runs.
        try:
            roots = [l.path for l in SETTINGS.libraries if l.enabled]
            RECOVERY.update(state="running", phase="commit sweep",
                            roots=len(roots), started=t0)

            # Ordinals are resolved from the root path rather than counted in
            # the callback: os.walk visits a root many times, so incrementing
            # per call would race past the end of the list.
            order = {p: i + 1 for i, p in enumerate(roots)}

            def _prog(p: dict) -> None:
                root = p.get("root") or ""
                RECOVERY.update(
                    root=os.path.basename(root.rstrip("\\/")) or root,
                    root_i=order.get(root, 0),
                    where=p.get("where") or "",
                    dirs=p.get("dirs") or 0,
                    files=p.get("files") or 0,
                    found=p.get("found") or 0,
                    elapsed=p.get("elapsed") or 0.0)

            # FAST PROBE FIRST, on the disks rather than the aggregate view.
            # See fileops.probe_for_bak() for the measurement. Only meaningful
            # when the library actually sits on an aggregate: if a root already
            # resolves to one physical disk there is nothing faster to walk,
            # and the probe is skipped rather than duplicating the work.
            needed = True
            try:
                from . import diskload, scanner
                vols = sorted(set((scanner.media_roots() or {}).values()))
                # AN AGGREGATE IS A ROOT WHOSE DISK IS NOT ONE OF THE DISKS
                # THE FILES ARE ACTUALLY ON.
                #
                # The obvious test - "does the root resolve to a physical
                # disk?" - does not work: DrivePool publishes P: as a virtual
                # disk and Windows gives it a perf counter like any other, so
                # the root resolved to "16 P:" and the pool looked like plain
                # storage. Comparing the SETS is what distinguishes them.
                # P: is disk 16, its members are disks 0-11, and disjoint sets
                # mean the members are a different and faster place to look.
                # On a single disk or a plain set of drives the two overlap,
                # there is nothing faster to walk, and the probe is skipped.
                root_keys = {diskload.key_for_path(r) for r in roots if r}
                vol_keys = {diskload.key_for_path(v) for v in vols}
                root_keys.discard(None)
                vol_keys.discard(None)
                aggregate = bool(vol_keys) and not (root_keys & vol_keys)
                if aggregate and vols:
                    RECOVERY.update(phase="probe", roots=len(vols))

                    def _pprog(p: dict) -> None:
                        RECOVERY.update(dirs=p.get("dirs") or 0,
                                        files=p.get("files") or 0,
                                        elapsed=p.get("elapsed") or 0.0,
                                        where="", root="checking every disk")

                    # The library FOLDER NAMES, so the probe can ignore the
                    # rest of each disk. A member volume here carries 191,445
                    # directories against 39,445 media files; the difference
                    # is artwork and metadata, and nothing commits there.
                    lib_names = {os.path.basename(r.rstrip("\\/"))
                                 for r in roots if r}
                    needed = await asyncio.to_thread(
                        fileops.probe_for_bak, vols, lib_names, _pprog)
                    if not needed:
                        joblog.log(
                            f"pool recovery: nothing left mid-commit "
                            f"({RECOVERY['files']:,} files checked in "
                            f"{RECOVERY['elapsed']:.0f}s) — skipped the full "
                            f"pool walk", "ok")
            except Exception as e:
                joblog.log(f"recovery probe failed, doing full walk: {e}",
                           "debug")
                needed = True

            if not needed:
                # The probe reached the end of every disk and saw no .nuarr-bak
                # at all. There is nothing for the full walk to find, so it is
                # skipped outright - this is the normal path after a clean
                # shutdown, which is to say almost every start.
                RECOVERY["fixed"] = []
                RECOVERY["note"] = (
                    f"{RECOVERY['files']:,} files checked across "
                    f"{RECOVERY['roots']} disks in "
                    f"{RECOVERY['elapsed']:.0f}s — nothing left mid-commit")
            else:
                RECOVERY.update(phase="commit sweep", roots=len(roots),
                                dirs=0, files=0, elapsed=0.0)
                fixed = await asyncio.to_thread(
                    fileops.recover_interrupted_commits, roots, _prog)
                for f in fixed:
                    joblog.log(
                        f"recovered interrupted commit: {os.path.basename(f)}",
                        "warn")
                RECOVERY["fixed"] = [os.path.basename(f) for f in fixed[:20]]
                if fixed:
                    joblog.log(f"restored {len(fixed)} file(s) left mid-commit",
                               "ok")
                    RECOVERY["note"] = f"restored {len(fixed)} mid-commit file(s)"
                else:
                    # SAY SO WHEN THERE WAS NOTHING TO DO. A blank note after a
                    # long step reads as "no idea what happened"; the walk
                    # having found the library clean is a real result.
                    RECOVERY["note"] = (
                        f"{RECOVERY['files']:,} files across {len(roots)} "
                        f"librar{'y' if len(roots) == 1 else 'ies'} — nothing "
                        f"left mid-commit")
        except Exception as e:
            joblog.log(f"commit recovery failed: {e}", "error")
            RECOVERY["note"] = f"commit recovery failed: {e}"
        try:
            RECOVERY.update(phase="cache sweep", where="", root="")
            n, gb = purge_stale_cache()
            if n:
                joblog.log(f"cleared {n} stale cache file(s), freed {gb:.2f} GB",
                           "ok")
                RECOVERY["note"] += f"; cleared {n} stale cache file(s)"
        except Exception as e:
            joblog.log(f"cache sweep failed: {e}", "warn")
    finally:
        RECOVERY["elapsed"] = time.time() - t0
        RECOVERY["phase"] = ""
        # Even if recovery threw, release the gate - a permanently closed one
        # would silently stop the queue forever, which is far worse than
        # running with one unrepaired file.
        RECOVERY["state"] = "done"
        RECOVERED.set()
    await pump()


# --- yielding the disk to a viewer ------------------------------------------
# Avoiding the spindle a viewer is on solves the problem when there is
# somewhere else to go. Often there is not: measured here, all 15 queued files
# sat on the two disks already in use, so the queue either waits or shares.
#
# Sharing is fine IF nuarr goes second. Windows I/O priority does exactly that
# - a Very Low request yields to Normal ones at the storage stack rather than
# interleaving with them - so a background remux keeps whatever bandwidth the
# viewer is not using and gets out of the way the instant they need it. This is
# not a bandwidth cap; a job runs at full speed again the moment playback
# stops.
#
# Applied to the ffmpeg CHILDREN, not to nuarr: nuarr's own I/O is a few
# SQLite pages, while ffmpeg is the process moving gigabytes.
IO_THROTTLED: dict = {"on": False, "procs": 0, "changed_at": 0.0,
                      # Which spindles a viewer is on, whether a balance is
                      # running, and the sentence explaining it. The panel used
                      # to get a bare boolean and had to guess at the cause.
                      "disks": [], "reason": ""}


def _set_io_priority(watched: set | None = None,
                     everything: bool = False) -> int:
    """Demote the jobs that are actually in someone's way. Restore the rest.

    THIS USED TO BE ALL-OR-NOTHING, and that was wrong for the common case.
    One viewer occupies exactly ONE spindle. Windows arbitrates I/O priority
    per device, so demoting a job reading NU-DRIVE-7 does nothing for a viewer
    on NU-DRIVE-1 - it just makes that job slower for no one's benefit. With
    twelve disks and one stream, eleven of the twelve demotions were pure loss.

    Two different shapes of contention, so two rules:

      A VIEWER  - occupies one spindle. Demote only the jobs touching it.
                  "Touching" means SOURCE OR DESTINATION: a commit reads from
                  the cache and writes back into the pool, and DrivePool may
                  well place that write on the disk being watched, so keying on
                  the source alone would miss the half doing the writing.

      A BALANCE - reads and writes across every pool disk at once. There is no
                  "somewhere else" to be, so everything is demoted. A real
                  balance already blocks the gate, so this only reaches jobs
                  that were in flight when it started - and letting it finish
                  sooner is what reopens the queue.

    Returns the number of processes left at LOW priority.
    """
    try:
        import psutil
    except Exception:
        return 0
    watched = watched or set()
    lowered = 0
    for w in list(RUNNING.values()):
        p = getattr(w, "proc", None)
        pid = getattr(p, "pid", None)
        if not pid:
            continue
        touches = {getattr(w, "disk", "") or "",
                   getattr(w, "dest_disk", "") or ""} - {""}
        low = bool(everything or (touches & watched))
        # Record it on the worker so the card can state the truth per job
        # instead of the panel implying every job is throttled.
        try:
            w.io_low = low
        except Exception:
            pass
        want_io = psutil.IOPRIO_VERYLOW if low else psutil.IOPRIO_NORMAL
        # BELOW_NORMAL rather than IDLE: idle-class processes can be starved
        # outright by anything at all, and a stalled encode holding a cache file
        # open is worse than a slow one.
        want_cpu = (psutil.BELOW_NORMAL_PRIORITY_CLASS if low
                    else psutil.NORMAL_PRIORITY_CLASS)
        try:
            pr = psutil.Process(pid)
            # ffmpeg is the direct child here, but handlers run PowerShell which
            # spawns its own work - walk the tree so nothing is missed.
            for t in [pr] + pr.children(recursive=True):
                try:
                    t.ionice(want_io)
                    t.nice(want_cpu)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if low:
                lowered += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return lowered


# HOW LONG A DISK STAYS YIELDED AFTER THE VIEWER LEAVES IT.
#
# This is what stops the throttle flapping across an episode boundary. Plex
# closes one file and opens the next, and for a second or two in between there
# is no session on that disk at all - so the old loop restored full priority,
# let a remux spin up, and then demoted it again once the next episode
# registered. The viewer pays for that gap twice: once for the burst of
# competing I/O, and once for the seek storm when it is throttled back.
#
# Asymmetric on purpose: fast to yield, slow to take back. Being wrong about
# yielding costs a little background throughput; being wrong about taking it
# back costs somebody's playback.
IO_HOLD_S = 90.0
# Tick fast while anybody is watching, idle otherwise. The Plex read behind
# this is separately cached at ~2 s and costs a local round trip, so a 1 s
# loop adds nothing measurable - and it is the difference between reacting to
# a new disk in up to 7 s and reacting in under 3.
IO_FAST_S = 1.0
IO_IDLE_S = 5.0
# disk -> when a viewer was last on it
_IO_SEEN: dict[str, float] = {}


_SUSPENDED: dict[str, float] = {}      # job id -> when it was suspended
_SUSPENDED_PID: dict[str, int] = {}    # job id -> the pid we froze, for rescue
_CAPPED: set[str] = set()              # job ids that already served the max hold


def _suspend_for_viewers() -> None:
    r"""Freeze any encode reading a spindle whose viewer is short of buffer.

    RESUMING IS THE PART THAT MATTERS. A suspended process that is never woken
    is a hung job, so every path out of this function un-suspends: the buffer
    recovering, the viewer stopping, the setting being turned off, the job
    finishing, and the safety cap below. The state lives in _SUSPENDED rather
    than on the worker so a worker object that disappears cannot strand one.

    Windows has no SIGSTOP. psutil.suspend() uses NtSuspendProcess, which stops
    every thread in the process - ffmpeg included - and resume() reverses it.
    The process keeps its handles and its memory, so an encode picks up exactly
    where it stopped with no re-read and no lost work.
    """
    try:
        cap = float(fileops.PAUSE_CAP_S)
    except Exception:                                    # noqa: BLE001
        cap = 300.0
    now = time.time()
    live_ids = set()

    for w in list(RUNNING.values()):
        jid = getattr(getattr(w, "job", None), "id", "") or ""
        if not jid:
            continue
        # LIVENESS IS ABOUT THE JOB, NOT ABOUT THIS INSTANT'S PROCESS.
        #
        # This used to `continue` before the id was recorded whenever w.proc
        # was None, which it briefly is between ffmpeg invocations. The job
        # then looked finished to the cleanup at the bottom, which dropped its
        # _SUSPENDED entry - so the next tick saw an unsuspended job, called
        # suspend() again and logged "paused" again. The log filled with
        # repeated pauses that had no matching resume, and the bookkeeping
        # that is supposed to guarantee a wake-up had been thrown away.
        live_ids.add(jid)
        proc = getattr(w, "proc", None)
        if proc is None:
            continue                     # nothing to suspend or resume yet
        src = getattr(w, "disk", "") or ""
        dst = getattr(w, "dest_disk", "") or ""
        disks = {d for d in (src, dst) if d}

        want_stop = bool(disks) and _viewer_starving(disks)
        # NEVER hold one past the cap. A viewer stuck under the threshold for
        # minutes is a different problem, and a permanently frozen encode
        # holding a worker slot and cache space is not the answer to it.
        #
        # AND ONCE RELEASED, STAY RELEASED. Simply clearing want_stop for one
        # tick resumed the job and then re-suspended it on the next, because
        # the disk is still starving - a 300s sawtooth that let the encode run
        # for a single tick every five minutes, which is worse than either
        # honest answer. _CAPPED remembers that this job has already served
        # its maximum hold; it runs (at Very Low I/O) until the disk actually
        # recovers, which is what clears the flag.
        if want_stop and jid in _SUSPENDED and now - _SUSPENDED[jid] > cap:
            want_stop = False
            _CAPPED.add(jid)
        elif not want_stop:
            _CAPPED.discard(jid)
        elif jid in _CAPPED:
            want_stop = False

        if want_stop and jid not in _SUSPENDED:
            try:
                psutil.Process(proc.pid).suspend()
                _SUSPENDED[jid] = now
                _SUSPENDED_PID[jid] = proc.pid
                w.paused_for_viewer = True
                joblog.log("paused — a viewer on this spindle is low on buffer",
                           "info", jid)
            except Exception:                            # noqa: BLE001
                _SUSPENDED.pop(jid, None)
                _SUSPENDED_PID.pop(jid, None)
        elif not want_stop and jid in _SUSPENDED:
            try:
                psutil.Process(proc.pid).resume()
            except Exception:                            # noqa: BLE001
                pass
            held = now - _SUSPENDED.pop(jid, now)
            _SUSPENDED_PID.pop(jid, None)
            w.paused_for_viewer = False
            joblog.log(
                f"resumed after {held:.0f}s — "
                + ("held the maximum; running at Very Low I/O from here"
                   if jid in _CAPPED else "the viewer has buffer again"),
                "info", jid)

    # A job that ended while suspended cannot resume itself. Its process is
    # gone, so this only tidies the bookkeeping - but leaving the id behind
    # would make a later job reusing it look permanently paused.
    # WAKE IT BEFORE FORGETTING IT. Dropping the entry for a job that is no
    # longer in RUNNING is right, but only after making sure we are not
    # abandoning a frozen process: if the pid is still alive and still
    # suspended, nothing else in nuarr will ever resume it, and it would sit
    # holding its cache file and its handles until the box restarted.
    for jid in [j for j in _SUSPENDED if j not in live_ids]:
        _SUSPENDED.pop(jid, None)
        pid = _SUSPENDED_PID.pop(jid, None)
        if not pid:
            continue
        try:
            p = psutil.Process(pid)
            if p.status() == psutil.STATUS_STOPPED:
                p.resume()
                joblog.log(f"resumed a stranded process (pid {pid}) whose job "
                           f"had already left the running set", "warn", jid)
        except Exception:                                # noqa: BLE001
            pass                                         # already gone: fine
    _CAPPED.intersection_update(live_ids)


def suspended_jobs() -> dict:
    return dict(_SUSPENDED)


async def io_priority_watch() -> None:
    """Keep disk priority in step with who is competing for which spindle."""
    from . import gate
    while True:
        playing = False
        try:
            # KEEP THE RATES HONEST WITHOUT A BROWSER.
            #
            # sample_io() was only ever called from the snapshot the dashboard
            # polls, so with nothing open the figures froze at their last
            # value - and gate._our_bps_by_key() subtracts them from each
            # disk's measured throughput to decide what is NOT nuarr's load.
            # A stale 400 MB/s there would mask real external activity and stop
            # the busy check firing on that spindle, on exactly the unattended
            # server where nobody would notice. This loop already runs every
            # 1-5 s whatever else is happening.
            for w in list(RUNNING.values()):
                try:
                    w.sample_io()
                except Exception:
                    pass
            # A viewer demotes only the spindle they are on...
            watched = set()
            live: set = set()
            nxt: set = set()
            if get_toggle_safe("gate.plex_io_throttle") and gate.plex_playing():
                playing = True
                # ...AND ONLY IF THAT VIEWER STILL NEEDS THE SPINDLE.
                #
                # This was the last place still asking the old binary question:
                # "is anybody playing off this disk", full stop. Every other
                # mechanism learned to ask how MUCH buffer they have - the copy
                # ramp goes to full speed at VIEWER_FULL_SPEED_MULT, the gate
                # releases a parked transcode on the same line - but the I/O
                # demotion kept demoting on presence alone. So a viewer sitting
                # on four minutes against a thirty-second floor, who could not
                # be made to stutter by anything nuarr did, still pinned every
                # job on their spindle to Very Low I/O for the whole film.
                #
                # Same line as the ramp, deliberately: one threshold, so a job
                # cannot be told it is at full speed and be running at idle
                # priority at the same time.
                live = {d for d in _plex_disks_safe() if _viewer_needs_disk(d)}
                # ...plus the one the NEXT episode is on, so a rollover finds
                # it already yielded rather than racing to catch up. See
                # gate.plex_next_disks().
                try:
                    nxt = set(gate.plex_next_disks()) - live
                except Exception:
                    nxt = set()
                live = live | nxt
            now = time.time()
            for d in live:
                _IO_SEEN[d] = now
            # STICKY. A disk stays in the set for IO_HOLD_S after the last time
            # a viewer was on it, so the gap between two episodes never reads
            # as "nobody is watching".
            for d, t in list(_IO_SEEN.items()):
                if now - t > IO_HOLD_S:
                    _IO_SEEN.pop(d, None)
            watched = set(_IO_SEEN)
            # ...and so does anything ELSE that owns a spindle. This used to
            # ask DrivePool whether it was balancing and, if so, demote every
            # job on every disk. Two things were wrong with that: it only knew
            # about one product, and it was indiscriminate — a balance touching
            # three disks slowed jobs on the other nine for nobody's benefit.
            # Measuring instead covers a backup, a parity check, a rebuild or a
            # copy, and demotes exactly the spindles under pressure.
            busy = set()
            try:
                busy = set(gate.busy_disks())
            except Exception:
                pass
            watched |= busy

            want = bool(watched)
            # NAME THE REASONS SEPARATELY. They are all "yield the disk", but
            # they answer different questions when you are reading the log
            # afterwards trying to work out why a job ran slowly - and "a
            # viewer is on NU-DRIVE-7" would be a lie about a disk nobody has
            # touched for a minute.
            if watched:
                now_on = (live - nxt) & watched
                held = watched - live - busy
                bits = []
                if now_on:
                    bits.append(f"a viewer is on {', '.join(sorted(now_on))}")
                if nxt & watched:
                    bits.append(f"next episode is on {', '.join(sorted(nxt & watched))}")
                if held:
                    bits.append(f"still yielding {', '.join(sorted(held))}")
                if busy:
                    bits.append(f"other activity is on {', '.join(sorted(busy))}")
                reason = "; ".join(bits)
            else:
                reason = ""
            # Re-apply while ON even if the state has not flipped: jobs start
            # and finish constantly, and a worker that began after the throttle
            # engaged would otherwise run at full priority against the viewer.
            # It also has to re-run when the WATCHED SET changes - moving to the
            # next episode can move the viewer to a different spindle.
            # SUSPEND, not just deprioritise, when a viewer is running dry.
            #
            # Very Low I/O priority makes nuarr's reads yield per request; it
            # does not stop them being issued, and a 4-worker encode pulling
            # from the same spindle still costs seeks the viewer has to queue
            # behind. Below viewer_pause_lead_s the honest move is to stop the
            # process outright and give the disk back, then resume when the
            # buffer recovers - a suspended ffmpeg costs nothing but RAM.
            try:
                await asyncio.to_thread(_suspend_for_viewers)
            except Exception:                            # noqa: BLE001
                pass

            changed = (IO_THROTTLED["on"] != want
                       or IO_THROTTLED.get("disks") != sorted(watched))
            if want or changed:
                n = await asyncio.to_thread(_set_io_priority, watched, False)
                IO_THROTTLED["procs"] = n
                if changed:
                    IO_THROTTLED.update(on=want, disks=sorted(watched),
                                        reason=reason, changed_at=time.time())
                    joblog.log(
                        (f"disk priority lowered on {n} job(s) — {reason}"
                         if want else "disk priority restored — nothing is "
                                      "competing for the pool"),
                        "debug")
        except Exception as e:
            playing = False
            joblog.log(f"io priority: {type(e).__name__}: {e}", "debug")
        # Fast while somebody is watching OR while a disk is still inside its
        # hold - the hold has to expire promptly once it is genuinely over, or
        # the throttle would linger a whole idle tick past its own deadline.
        await asyncio.sleep(IO_FAST_S if (playing or _IO_SEEN) else IO_IDLE_S)


def get_toggle_safe(key: str) -> bool:
    from . import gate
    try:
        return gate.get_toggle(key)
    except Exception:
        return True


def _plex_disks_safe() -> set:
    from . import gate
    try:
        return gate.plex_disks()
    except Exception:
        return set()


def _disk_load_safe() -> dict:
    """Never let a counter problem break the 2 s poll the whole UI rides on."""
    from . import gate
    try:
        return gate.disk_report()
    except Exception as e:
        return {"disks": [], "error": f"{type(e).__name__}: {e}"}


def _plex_disk_detail_safe() -> dict:
    from . import gate
    try:
        return gate.plex_disk_detail()
    except Exception:
        return {}


async def start() -> None:
    global _pump_task
    if _pump_task is None or _pump_task.done():
        # BEFORE requeuing anything: kill encoders orphaned by the previous
        # process. recover_interrupted() is about to put those exact jobs back
        # in the queue, and if their old ffmpeg is still holding the source
        # file, every one of them will just defer on the lock check.
        try:
            await asyncio.to_thread(reap_orphan_encoders)
        except Exception as e:
            joblog.log(f"orphan reaper failed (continuing): "
                       f"{type(e).__name__}: {e}", "warn")
        n = recover_interrupted()
        if n:
            joblog.log(f"requeued {n} job(s) interrupted by a restart", "warn")
        RECOVERED.clear()
        RECOVERY.update(state="pending", note="")
        _pump_task = asyncio.create_task(_recover_then_pump())


# ----------------------------------------------------------------- run -----
async def _orig_lang(file_id: int | None) -> str:
    r"""The language this title was made in, for the audio policy.

    Stored value first, one small arr request if we have never seen the title,
    and "" on any failure - which rules.decide() reads as "unknown" and answers
    by falling back to the old folder-based logic. A metadata lookup must never
    be able to stop a file being processed.
    """
    if not file_id:
        return ""
    try:
        from . import origlang
        return await origlang.for_file(int(file_id))
    except Exception as e:
        joblog.log(f"original-language lookup failed, using the older audio "
                   f"rules for this file: {type(e).__name__}", "debug")
        return ""


async def _run(job: Job, pool: str) -> None:
    # the dispatcher already placed this worker in RUNNING so the pool count is
    # correct the instant the job is claimed
    w = RUNNING.get(job.id) or Worker(job=job, pool=pool, started_at=time.time())
    RUNNING[job.id] = w
    joblog.banner(job.id, f"START [{pool}] {job.title}")
    joblog.log(f"source: {job.path}", "debug", job.id)

    try:
        # Only transcodes exist now; the PowerShell handler kinds were removed
        # with their subsystem. A stale handler job left in the queue from
        # before that is caught below and retired, so it can never sit at the
        # head of the queue forever.
        if not os.path.exists(job.path):
            # A SOURCE THAT VANISHED IS NOT A FAILURE.
            #
            # Between enqueue and dispatch the arr can upgrade the file, rename
            # it, or DrivePool can move it - all normal operation, and all leave
            # this path pointing at nothing. Raising here recorded a hard
            # "failed" job whose entire error text was a file path, which then
            # sat in the panel forever looking like a real fault. Two of the
            # seven failures on this box were exactly that, and both files had
            # in fact been upgraded and processed successfully minutes later.
            #
            # Report it as skipped and say why. The scan and the missing-healer
            # already own deciding what the file's real state is.
            joblog.log(f"source is gone - the arr has replaced or moved it "
                       f"since this job was queued: {job.path}", "warn", job.id)
            _finish(job, "skipped", 0, 0)
            return

        # IS ANYONE USING THIS FILE RIGHT NOW?
        # The hold timer is a blunt guess - 30 hours of quiet does not prove a
        # file is free, and 30 hours of waiting on a file nobody has touched is
        # wasted. An actual lock check is the real answer: if Plex is streaming
        # it or the OCR hook has it open, defer and let something else run.
        # `needs_file` used to guard this: the pool-wide handlers took no file
        # at all, so locking a path they never opened was meaningless. Every
        # remaining job kind works on one file, so the path itself is the only
        # condition left.
        if job.path:
            if await asyncio.to_thread(fileops.is_locked, job.path):
                who = await asyncio.to_thread(fileops.who_locks, job.path)
                whom = ", ".join(who) if who else "another process"
                joblog.log(f"deferred - file is in use by {whom}", "warn", job.id)
                _defer(job, f"in use by {whom}")
                return

        # THE FIRST OF TWO DISPATCH SITES. Patching only the other one made
        # every sub_ocr job fail with "unknown handler 'sub_ocr'" - it never
        # reached the native branch because this one caught it first. Both are
        # guarded now; a third would be a reason to collapse them.
        if job.kind == "sub_ocr":
            data = await probe(job.path)
            if not data:
                joblog.log("could not probe the file", "error", job.id)
                _finish(job, "failed", 0, 0, "probe returned nothing")
                return
            # Duration feeds the batch throughput figures. This dispatch site
            # skipped it, so every subocr finish reported media_s=0, the rate
            # sample bailed at zero, the pool never earned a rate - and the
            # header said "measuring throughput..." with 1,458 files done.
            w.duration = float((data.get("format") or {}).get("duration") or 0)
            await _sub_ocr(w, data)
            return

        if job.kind != "transcode":
            # A JOB FROM BEFORE THE HANDLERS WERE REMOVED. Retired rather than
            # failed: nothing is wrong with the file, the kind of work simply
            # no longer exists. Failing it would put it in the Errors tile and
            # invite someone to retry something that cannot run.
            _retire_handler_job(job)
            return

        # _claim already carried the disk over; only look it up if it did not
        # (a job resumed from an older row, say).
        if not w.disk:
            try:
                with cursor() as cur:
                    r = cur.execute("SELECT pool_disk FROM files WHERE id=?",
                                    (job.file_id,)).fetchone()
                w.disk = (r["pool_disk"] if r else "") or ""
            except Exception:
                w.disk = ""

        w.set_stage("probing")
        data = await probe(job.path)
        if not data:
            raise RuntimeError("ffprobe returned nothing")
        cache_probe(job.file_id, data)
        w.duration = float((data.get("format") or {}).get("duration") or 0)
        joblog.log(f"probe ok: {len(data.get('streams', []))} streams, "
                   f"{w.duration/60:.1f} min", "ok", job.id)

        # A PLAN FOR BYTES THAT NO LONGER EXIST MUST NOT RUN.
        #
        # The plan is decided at enqueue and persisted, and a queued job can
        # wait hours. In that window an arr upgrade can land a DIFFERENT file
        # at the SAME path - the vanished-source check above never fires, and
        # the stored plan's track indices, burn target and audio ops all
        # describe the old release. Executing it against the new bytes maps
        # the wrong tracks at best. The fresh probe is already in hand, so a
        # size mismatch simply throws the stale plan away and re-decides.
        try:
            _cur_size = os.path.getsize(job.path)
        except OSError:
            _cur_size = 0
        _plan_size = int(getattr(job.plan, "src_size", 0) or 0) \
            if job.plan is not None else 0
        if job.plan is not None and _plan_size and _cur_size != _plan_size:
            joblog.log(f"source changed since this job was queued "
                       f"({_plan_size/2**20:.0f} MB -> {_cur_size/2**20:.0f} MB"
                       f" - an arr upgrade, most likely); discarding the "
                       f"stored plan and re-deciding from the live probe",
                       "warn", job.id)
            job.plan = None

        if job.plan is None:
            # rules.is_anime, not a second hand-rolled test - see its docstring.
            anime = rules.is_anime(job.path, job.title or "")
            job.plan = rules.decide(data, anime=anime,
                                    filename=os.path.basename(job.path),
                                    size_bytes=os.path.getsize(job.path),
                                    orig_lang=await _orig_lang(job.file_id),
                                    file_id=job.file_id)
        joblog.log("PIPELINE: probe -> plan -> encode -> commit -> "
                   "arr refresh -> rename", "info", job.id)

        # The pre-encode repair stage is gone with the PowerShell handlers it
        # ran. Its purpose - fix the source before planning from it - is now
        # served by the rules themselves: a broken container or an unplayable
        # track is a reason to rebuild, planned from what ffprobe actually
        # reports rather than patched beforehand by a separate script.
        if False:
            changed = await _run_inline_handlers(job, data)
            if changed:
                joblog.log("re-probing - a handler modified the file", "info", job.id)
                data = await probe(job.path) or data
                cache_probe(job.file_id, data)
                anime = "\\Anime" in job.path
                job.plan = rules.decide(data, anime=anime,
                                        filename=os.path.basename(job.path),
                                        size_bytes=os.path.getsize(job.path))

        # BACKFILL FACTS THE STORED PLAN PREDATES.
        #
        # A plan is persisted as JSON at enqueue and replayed here, so a job
        # queued before a field existed arrives without it. sub_codecs is
        # exactly that case: jobs already in the queue had no record of which
        # subtitles were mov_text, so build_ffmpeg fell back to `copy` and they
        # failed against Matroska all over again - the fix appeared not to work
        # when it was simply not present in those rows.
        #
        # The live probe is right here and knows the answer, so fill it in
        # rather than making the fix depend on when a job happened to be
        # queued.
        try:
            if job.plan is not None and not getattr(job.plan, "sub_codecs", None):
                job.plan.sub_codecs = [
                    (s.get("codec_name") or "").lower()
                    for s in (data.get("streams") or [])
                    if s.get("codec_type") == "subtitle"]
        except Exception:
            pass

        joblog.log_plan(job.id, job.plan)

        # Subtitle OCR is NATIVE, not a script, because it rewrites the file.
        # Everything below this line - the Plex gate, the paced commit, the
        # DrivePool-aware replace, the deferred-commit retry - exists to make
        # rewriting a library file safe, and 9.95 TB of remuxing has no
        # business going round it. Checked before the handler branch so it can
        # never fall through to the shell-out path.
        if job.kind == "sub_ocr":
            await _sub_ocr(w, data)
            return

        # see _retire_handler_job - the PowerShell kinds no longer exist
        if job.kind != "transcode":
            _retire_handler_job(job)
            return

        if job.plan.skip_reason or not job.plan.needed:
            joblog.log("nothing to do - file left untouched", "ok", job.id)
            _finish(job, "skipped", 0, 0)
            return

        await _transcode(w, data)

    except asyncio.CancelledError:
        joblog.log("cancelled", "warn", job.id)
        _finish(job, "cancelled", 0, 0)
    except Exception as e:
        joblog.log(f"FAILED: {type(e).__name__}: {e}", "error", job.id)
        _finish(job, "failed", 0, 0, str(e))
    finally:
        # Clear this job's scratch files - a cancel or crash used to leave a
        # part-written file on E:, which is how Tdarr accumulated 150 GB.
        # EXCEPT when a commit was deferred: that output is finished work being
        # held for retry, and purging it here would undo the whole point.
        deferred = False
        try:
            from . import commitqueue
            deferred = any(job.id == r["job_id"]
                           for r in commitqueue.stats()["rows"])
        except Exception:
            pass
        if not deferred:
            _purge_job_cache(job.id)
        el = time.time() - w.started_at
        joblog.banner(job.id, f"END   {job.title}  ({el:.0f}s)", "info")
        RUNNING.pop(job.id, None)
        joblog.release(job.id)


async def _current_path(job: Job) -> str | None:
    """Where does the arr say this file lives right now?

    Returns None if it cannot be determined, in which case the caller keeps the
    path it already has - a failed lookup must never redirect a commit.
    """
    from .arr import shared_client

    # Every step here was on the event loop: a SQLite read, an httpx client
    # construction (which builds an SSL context), and an existence check on the
    # pool. This runs once per commit, so with twelve workers it fired
    # constantly - py-spy caught the loop parked in it.
    def _row():
        with cursor() as cur:
            return cur.execute(
                "SELECT arr_name, arr_file_id FROM files WHERE id=?",
                (job.file_id,)).fetchone()

    row = await asyncio.to_thread(_row)
    if not row or not row["arr_name"] or not row["arr_file_id"]:
        return None
    cfg = next((c for c in SETTINGS.arrs if c.name == row["arr_name"]), None)
    if not cfg:
        return None
    # Shared and not closed - see arr.shared_client.
    client = shared_client(cfg)
    try:
        rec = await client.file_record(row["arr_file_id"])
        path = (rec or {}).get("path")
        # Only trust it if the file is actually there. If the arr is mid-rename
        # it can report a path that does not exist yet, and committing to that
        # would create a stray file.
        if not path:
            return None
        return path if await asyncio.to_thread(os.path.exists, path) else None
    except Exception as e:
        joblog.log(f"could not re-check the path with {cfg.name}: {e}",
                   "debug", job.id)
        return None


# HOW FAR THE BUFFER MUST CLIMB BACK before the disk is handed to nuarr again,
# as a multiple of the pause floor. One threshold for both directions is what
# made this flap: a viewer sitting at the floor crosses it several times a
# minute on measurement noise alone. Worse, the feedback loop is positive -
# pausing the encode is *what makes the buffer recover*, so the moment it ticks
# one second over the line nuarr resumes, the disk gets busy, and it falls
# straight back under. Resuming at 1.5x means the buffer has to have genuinely
# recovered, not merely touched the line.
VIEWER_RESUME_MULT = 1.5
# ...AND IT MUST STAY THERE. A single good sample is noise; two polls' worth of
# sustained recovery is a trend.
VIEWER_RECOVER_S = 20.0
# ...AND NOT BEFORE THIS. A floor is a floor, but a viewer who just ran dry is
# about to want a lot of disk, and a pause that lasts four seconds costs the
# spin-up without buying the buffer. Once stopped, stay stopped for a bit.
VIEWER_MIN_HOLD_S = 45.0

# per-disk pause state: disk -> {"since": t, "ok_since": t|None}
# Presence in this dict IS the paused state; absence is running.
_STARVE: dict[str, dict] = {}


def _disk_starving(disk: str, floor: float, now: float) -> bool:
    r"""Should nuarr keep off this spindle right now? Hysteretic and sticky.

    Two thresholds, not one. Falling below `floor` stops work; only climbing
    back to `floor * VIEWER_RESUME_MULT` starts it again, and even then only
    after the recovery has held for VIEWER_RECOVER_S and the pause has lasted
    at least VIEWER_MIN_HOLD_S. The gap between the two lines is what stops
    the oscillation - inside it the answer is simply "whatever it was".

    UNMEASURED IS NOT STARVING. gate.viewer_lead() answers None when nobody is
    watching that spindle or the lead could not be read, and reading None as
    zero would pause the queue every time Plex went quiet or a measurement
    slipped - the failure that looks exactly like nuarr having stopped working.
    A viewer who stops watching entirely releases the disk immediately; there
    is no one left to protect and no reason to serve out the hold.
    """
    from . import gate as _g
    lead = _g.viewer_lead(disk)
    st = _STARVE.get(disk)

    if lead is None:                       # nobody there, or unreadable
        _STARVE.pop(disk, None)
        return False

    if st is None:                         # running - does it need to stop?
        if lead < floor:
            _STARVE[disk] = {"since": now, "ok_since": None}
            return True
        return False

    # paused - every clause below is a reason to stay that way
    if lead < floor * VIEWER_RESUME_MULT:
        st["ok_since"] = None              # back under; recovery clock resets
        return True
    if st["ok_since"] is None:
        st["ok_since"] = now
    if now - st["since"] < VIEWER_MIN_HOLD_S:
        return True
    if now - st["ok_since"] < VIEWER_RECOVER_S:
        return True
    _STARVE.pop(disk, None)                # recovered, held, and served its time
    return False


def _viewer_starving(disks) -> bool:
    r"""Is any viewer on these spindles short of buffer?

    The threshold is a floor, not a target: above it nuarr still throttles to
    a quarter speed on a viewer's disk, it just does not stop. Below it, the
    disk is handed back entirely until the buffer recovers - see
    _disk_starving for what "recovers" has to mean before work resumes.
    """
    try:
        base = float(workers.get().viewer_pause_lead_s)
    except Exception:                                    # noqa: BLE001
        base = 0.0
    if base <= 0:
        _STARVE.clear()                                  # feature switched off
        return False
    now = time.time()
    hit = False
    try:
        from . import gate as _g
        for d in disks:
            # THE FLOOR IS PER DISK NOW, because it is per session: a stream
            # buffered to the end publishes none at all, a 4K remux publishes
            # a larger one than an SD episode, and a transcode's is capped
            # under Plex's own throttle buffer. Absent means nothing on that
            # spindle needs protecting.
            floor = _g.viewer_floor(d)
            if not floor or floor <= 0:
                _STARVE.pop(d, None)
                continue
            # not short-circuited: every disk's state machine needs the tick,
            # or a disk that is only ever asked about alongside a starving one
            # never advances its own recovery clock
            if _disk_starving(d, floor, now):
                hit = True
    except Exception:                                    # noqa: BLE001
        pass
    return hit


def starving_disks() -> dict:
    r"""What is paused, since when, and what it is waiting for - for the UI."""
    try:
        from . import gate as _g
        floors = _g.viewer_floors()
    except Exception:                                    # noqa: BLE001
        floors = {}
    now = time.time()
    out = {}
    for d, st in list(_STARVE.items()):
        floor = float(floors.get(d) or 0.0)
        held = now - st["since"]
        ok = st.get("ok_since")
        out[d] = {
            "since": st["since"],
            "held_s": round(held, 1),
            "resume_at_s": round(floor * VIEWER_RESUME_MULT, 1),
            # seconds still to serve on the two clocks, whichever bites last
            "hold_left_s": round(max(0.0, VIEWER_MIN_HOLD_S - held), 1),
            "recover_left_s": (None if ok is None
                               else round(max(0.0, VIEWER_RECOVER_S - (now - ok)), 1)),
        }
    return out


# AT THIS MULTIPLE OF THE FLOOR, STOP THROTTLING ALTOGETHER.
#
# Quarter speed was applied to anyone with a viewer on their spindle, flat,
# regardless of how far ahead that viewer was - so a stream sitting on 187s
# against a 46s floor was slowed exactly as hard as one on 47s. The second is
# a minute from trouble; the first could not stutter if nuarr tried.
VIEWER_FULL_SPEED_MULT = 3.0
# The hardest throttle, applied at the floor itself: sleep 3x the time the
# chunk took, i.e. roughly quarter speed.
VIEWER_THROTTLE_MAX = 3.0


def _viewer_needs_disk(d: str) -> bool:
    r"""Is the viewer on this spindle close enough to still need it yielded?

    The same test viewer_pace applies per disk, split out so the I/O-priority
    loop can ask it about ONE spindle without computing a pace it does not use.
    Unmeasured keeps the old caution: a viewer nuarr cannot cost is a viewer it
    does not get to gamble with.
    """
    try:
        from . import gate as _g
        if _g.viewer_done(d):
            return False              # has the rest of the file already
        fl, ld = _g.viewer_floor(d), _g.viewer_lead(d)
        if not fl or fl <= 0 or ld is None:
            return True
        return ld < fl * VIEWER_FULL_SPEED_MULT
    except Exception:                                    # noqa: BLE001
        return True


def viewer_pace(disks) -> float:
    r"""How hard to throttle a copy touching `disks`. -1 pauses, 0 is full speed.

    A RAMP, NOT A SWITCH - and deliberately continuous, because the last thing
    this code learned the hard way is that a threshold with work on one side
    and rest on the other oscillates. There is no state here to flip: as the
    buffer drains the sleep grows smoothly, and the loop settles at whatever
    speed keeps the viewer level instead of hunting between two extremes.

        lead >= 3x floor   full speed, nothing to protect against
        lead == 1x floor   quarter speed
        lead <  1x floor   paused (handled by _viewer_starving)
    """
    if _viewer_starving(disks):
        return -1.0
    try:
        from . import gate as _g
        worst = None
        for d in disks:
            if _g.viewer_done(d):
                continue          # everyone here has the rest of their file
            fl, ld = _g.viewer_floor(d), _g.viewer_lead(d)
            if not fl or fl <= 0 or ld is None:
                # A viewer nuarr cannot cost: keep the old caution rather than
                # inventing headroom that has not been measured.
                worst = 1.0 if worst is None else min(worst, 1.0)
                continue
            r = ld / fl
            worst = r if worst is None else min(worst, r)
        if worst is None:
            return 0.0            # nobody left to protect on these spindles
        if worst >= VIEWER_FULL_SPEED_MULT:
            return 0.0
        span = VIEWER_FULL_SPEED_MULT - 1.0
        frac = max(0.0, (VIEWER_FULL_SPEED_MULT - worst) / span)
        return round(VIEWER_THROTTLE_MAX * min(1.0, frac), 2)
    except Exception:                                    # noqa: BLE001
        return VIEWER_THROTTLE_MAX


def _commit_pace(w: Worker):
    r"""Shared throttle for any commit that copies a library file.

    Lifted out of _transcode so subtitle OCR gets the identical behaviour
    rather than a second, subtly different copy of it. The destination disk is
    unknown when the copy starts - DrivePool decides placement - so this is
    re-evaluated live per chunk rather than fixed up front.
    """
    def pace():
        try:
            if not get_toggle_safe("gate.plex_io_throttle"):
                return 0.0
            dest = getattr(w, "dest_disk", "") or ""
            src_disk = getattr(w, "disk", "") or ""
            watched = _plex_disks_safe()
            mine = ({dest, src_disk} - {""})
            hit = mine & watched if watched else set()
            if not hit:
                return 0.0
            # HOW THIN IS THE VIEWER'S BUFFER on the spindle being touched.
            # A quarter of a 150 MB/s commit is still 37 MB/s competing with a
            # viewer who has seconds in hand; below the floor the only useful
            # thing this copy can do is stop. -1 pauses; 0 means the viewer is
            # far enough ahead that there is nothing to protect them from.
            f = viewer_pace(hit)
            w.paused_for_viewer = (f < 0)
            return f
        except Exception:
            return 0.0
    return pace


def paced_disks() -> set[str]:
    r"""Spindles that currently have a copy on them the ramp can actually slow.

    A THROTTLE IS ONLY A FACT WHEN THERE IS WORK TO SLOW. The session card
    computed a percentage from the buffer level alone and printed it whenever
    the buffer was under the full-speed line - including on a spindle where
    nuarr had nothing running, and including where the only nuarr work was an
    encode, which this ramp does not govern at all (encodes are stopped and
    started outright by _suspend_for_viewers). "nuarr at 44% speed" was a
    statement about a mechanism that was not in play.

    Only the commit copy is paced, so only a worker in its commit phase counts.
    """
    out: set[str] = set()
    try:
        if not get_toggle_safe("gate.plex_io_throttle"):
            return out
        for w in list(RUNNING.values()):
            if not getattr(w, "commit_phase", ""):
                continue        # not copying yet; nothing is being held back
            for d in (getattr(w, "dest_disk", ""), getattr(w, "disk", "")):
                if d:
                    out.add(d)
    except Exception:                                    # noqa: BLE001
        return set()
    return out


def _commit_stage_cb(w: Worker):
    """The progress callback every commit hands to safe_replace.

    Shared for the same reason as _commit_pace: _sub_ocr grew its own
    single-argument version, safe_replace calls with four - so every call
    raised TypeError into a swallow, and subocr commits showed no phase, no
    destination disk and no copy progress while the transcode next to them
    showed all three. One callback, used by both, cannot drift like that.

    Resolving the destination HERE is the point: the staging file is already
    at its final pool location, so DrivePool has picked the spindle before the
    first byte is copied.
    """
    def _on_stage(phase: str, staged_path: str, copied: int, total: int) -> None:
        w.commit_phase = phase
        if total:
            w.commit_total = total
        if phase == "copying":
            now_t = time.time()
            w.commit_bytes = copied
            if w._cm_at and now_t > w._cm_at:
                inst = (copied - w._cm_bytes) / (now_t - w._cm_at)
                w.commit_bps = 0.3 * inst + 0.7 * (w.commit_bps or inst)
            w._cm_bytes, w._cm_at = copied, now_t
        if not w.dest_disk and staged_path:
            try:
                from . import scanner as _sc
                w.dest_disk = _sc.disk_of(staged_path) or ""
            except Exception:
                pass
    return _on_stage


async def _sub_ocr(w: Worker, probe_data: dict) -> None:
    r"""OCR image subtitles to SRT and mux them in, through the commit path.

    subocr produces a NEW file and returns it; the replace happens here, using
    the same paced, DrivePool-aware, retry-on-lock machinery as a transcode.
    That split is the point: the module that knows about subtitles knows
    nothing about when it is safe to touch the pool, and vice versa.
    """
    from . import subocr

    job = w.job
    os.makedirs(SETTINGS.cache_dir, exist_ok=True)
    w.set_stage("ocr")
    if not w.duration:            # belt for whichever dispatch path got here
        try:
            w.duration = float((probe_data.get("format") or {})
                               .get("duration") or 0)
        except (TypeError, ValueError):
            pass
    size_before = os.path.getsize(job.path) if os.path.exists(job.path) else 0
    # Report the source spindle the way a transcode does, so this appears in
    # the disk panel and counts toward the per-disk contention rules instead of
    # looking like work happening nowhere. Same lookup the transcode path uses;
    # the early dispatch site can reach here before that code runs.
    if not getattr(w, "disk", ""):
        try:
            with cursor() as cur:
                r = cur.execute("SELECT pool_disk FROM files WHERE id=?",
                                (job.file_id,)).fetchone()
            w.disk = (r["pool_disk"] if r else "") or ""
        except Exception:
            w.disk = ""

    # subocr runs on a worker thread, so progress arrives off the loop. Setting
    # the plain attributes is safe (set_stage already defers its own DB write);
    # what must NOT happen here is any awaiting or DB work from that thread.
    def _prog(frac: float, stage: str):
        w.progress = frac
        if stage:
            w.set_stage(stage)

    # ALREADY-PREPARED SUBTITLES FIRST. If a previous handoff produced SRTs
    # and the transcode they were meant for skipped, was cancelled, or was
    # dequeued, this job is the safety net: it embeds the prepared tracks
    # without paying the OCR again.
    pend = subocr.pending_for(job.file_id, SETTINGS.cache_dir)
    if pend:
        w.set_stage("muxing prepared subtitles")
        joblog.log(f"found {len(pend)} previously prepared subtitle track(s) "
                   f"- embedding without re-running the OCR", "info", job.id)

        def _mux_pend():
            import tempfile
            wk = tempfile.mkdtemp(prefix="subocr_", dir=SETTINGS.cache_dir)
            o = os.path.join(wk, "out.mkv")
            subocr.embed(job.path,
                         [(p["srt"], p.get("name") or "English (OCR)")
                          for p in pend], o)
            return wk, o
        try:
            wk, o = await asyncio.to_thread(_mux_pend)
            res = {"ok": True, "out": o, "work": wk, "tracks": len(pend),
                   "notes": []}
        except Exception as e:
            _finish(job, "failed", 0, 0,
                    f"embed of prepared subtitles failed: {e}")
            return
    else:
        # OCR FIRST, MUX LATER - and only if nobody else is going to rewrite
        # this file anyway. A transcode already produces a new file, so it can
        # carry the subtitles in for free; doing it here as well would rewrite
        # the same file twice for one outcome. Checked BEFORE the OCR so the
        # decision is made on the queue as it stands, not as it was several
        # minutes ago. The consume side lives in _transcode (search for
        # pending_for), and the pending-subs branch above is the safety net
        # for a transcode that never comes.
        handoff = False
        try:
            with cursor() as cur:
                handoff = bool(cur.execute(
                    "SELECT 1 FROM jobs WHERE file_id=? AND kind='transcode' "
                    "AND state IN ('queued','running','deferred') LIMIT 1",
                    (job.file_id,)).fetchone())
        except Exception:
            handoff = False

        if handoff:
            joblog.log("a transcode is already queued for this file - "
                       "preparing the subtitles for it to carry in, not "
                       "rewriting here", "info", job.id)
            res = await asyncio.to_thread(subocr.produce, job.path, probe_data,
                                          job.file_id, SETTINGS.cache_dir,
                                          _prog, _library_of_file(job.file_id))
            for n in res.get("notes", []):
                joblog.log(n, "info", job.id)
            if res.get("ok"):
                joblog.log(f"{res['tracks']} subtitle(s) prepared and waiting",
                           "ok", job.id)
                _finish(job, "done", 0, 0,
                        note=f"subtitle OCR: {res['tracks']} track(s) "
                             f"prepared for the queued transcode to carry in")
            else:
                joblog.log(f"nothing prepared: {res.get('why')}",
                           "warn", job.id)
                _finish(job, "skipped", 0, 0,
                        note=f"subtitle OCR skipped: {res.get('why')}"[:300])
            return

        res = await asyncio.to_thread(subocr.run_one, job.path, probe_data,
                                      SETTINGS.cache_dir, _prog, True,
                                      lambda p: setattr(w, "proc", p),
                                      _library_of_file(job.file_id))
        w.proc = None
    for n in res.get("notes", []):
        joblog.log(n, "info", job.id)
    if not res.get("ok"):
        # Not a failure worth alarming about: "no eligible track" and "the OCR
        # was too sparse to be dialogue" are both correct outcomes, and the
        # file is untouched either way.
        why = str(res.get("why") or "")
        # PERMANENT vs TRANSIENT decides whether a retry could ever help.
        # The pipeline is deterministic: the same bytes through the same OCR
        # produce the same verdict, so content-based rejections are marked
        # 'rejected' and never re-attempted. But a locked file, a timeout or a
        # tool crash says nothing about the content - marking those rejected
        # (as this originally did) permanently banned files whose only crime
        # was being open in Plex at the wrong moment.
        transient = any(t in why.lower() for t in
                        ("extraction failed", "timed out", "produced no srt",
                         "no usable python", "winerror", "sharing violation"))
        joblog.log(f"nothing embedded: {why}", "warn", job.id)
        # _finish FIRST: its skipped branch writes state_reason='no work
        # needed', which used to land AFTER the detailed reason below and
        # clobber it - every rejected file read "no work needed" in the drill
        # panel while the real reason sat only in the job log.
        _finish(job, "skipped", 0, 0,
                note=f"subtitle OCR skipped: {why}"[:300])
        if transient:
            joblog.log("transient cause - the file stays eligible for a "
                       "later subtitle pass", "info", job.id)
            return
        # REMEMBER content rejections. The cheap eligibility filter reads the
        # stored probe and cannot know the OCR will reject the result, so
        # without this the queue picks the same file every run and burns the
        # OCR again to reach the same answer. Observed on 'DC Showcase: Adam
        # Strange', chosen three times in a row.
        try:
            with cursor() as cur:
                cur.execute("UPDATE files SET subocr_state=?, state_reason=? "
                            "WHERE id=?",
                            ("rejected", f"subtitle OCR: {why}"[:400],
                             job.file_id))
        except Exception as e:
            joblog.log(f"could not record the skip: {type(e).__name__}: {e}",
                       "warn", job.id)
        return

    out = res["out"]
    size_after = os.path.getsize(out)
    joblog.log(f"embedded {res['tracks']} OCR subtitle track(s); "
               f"{size_before/2**20:.1f} MB -> {size_after/2**20:.1f} MB",
               "ok", job.id)

    w.progress = 0.92
    w.set_stage("committing")
    r = await asyncio.to_thread(fileops.safe_replace, job.path, out,
                                on_stage=_commit_stage_cb(w),
                                pace=_commit_pace(w))
    w.commit_phase = ""
    if not r.ok:
        # Same reasoning as a transcode: the expensive half is done, so the
        # output is kept and the swap retried rather than thrown away into the
        # same lock it just hit.
        from . import commitqueue
        commitqueue.enqueue(job.id, job.file_id, job.path, out,
                            size_before, size_after, r.detail)
        _finish(job, "deferred", size_before, size_after,
                f"commit deferred: {r.detail}")
        return
    await asyncio.to_thread(_rm, out)
    # The .sup and .srt live in a temp dir beside the output. Left behind they
    # refill E: exactly the way abandoned cache files did.
    try:
        import shutil
        shutil.rmtree(res.get("work") or "", ignore_errors=True)
    except Exception:
        pass
    # Whether these subtitles were freshly OCR'd or picked up from the pending
    # area, they are IN the file now - the pending copies are spent.
    try:
        subocr.clear_pending(job.file_id, SETTINGS.cache_dir)
    except Exception:
        pass
    _finish(job, "done", size_before, size_after,
            note=f"subtitle OCR: {res['tracks']} PGS track(s) -> SRT, "
                 f"embedded first; image subs kept, demoted",
            event="subtitled")


async def _transcode(w: Worker, probe_data: dict) -> None:
    job = w.job
    os.makedirs(SETTINGS.cache_dir, exist_ok=True)
    out = os.path.join(SETTINGS.cache_dir, f"{job.id}.mkv")
    size_before = os.path.getsize(job.path)
    w.src_bytes = size_before
    # Identity of the bytes this encode is about to read, checked again before
    # the commit. NTFS preserves mtime across a rename, so this pair survives
    # the arr renaming the file mid-encode (handled below) but changes the
    # moment an upgrade REPLACES it - which is the case that must not commit.
    try:
        _src_stamp = (size_before, os.path.getmtime(job.path))
    except OSError:
        _src_stamp = (size_before, 0.0)

    # SUBTITLE OCR RUNS ALONGSIDE THE ENCODE, not after it. If this file also
    # needs its PGS dialogue converted, produce the SRTs on a CPU thread while
    # ffmpeg owns the GPU and the disks - the two phases do not compete - and
    # the pickup below embeds them into the output before the single commit.
    # One pool rewrite carries both changes.
    #
    # This is what replaced the queued-handoff idea: enqueue() enforces one
    # job per file, so "a transcode and a sub_ocr both queued" cannot exist,
    # and a handoff keyed on that state was dead on arrival. Running the OCR
    # inside the transcode needs no queue coordination at all.
    _sub_task = None
    # The OCR reports its own progress, but nothing was listening: produce()
    # was called without a callback, so once the encode finished the row sat on
    # "waiting for subtitle OCR" with a stopwatch and no percentage - the same
    # "is it hung?" that the sub_ocr pool's own ticker was built to answer.
    # Stash the latest reading here; the wait below spends it.
    def _sub_tick(frac: float, stage: str) -> None:
        # Called from the OCR thread. Plain assignment only - no awaiting and
        # no DB work off the loop (the same rule _prog follows in _sub_ocr).
        w.sub_ocr_frac = frac
        if stage:
            w.sub_ocr_stage = stage

    try:
        from . import subocr as _so_pre
        if (not _so_pre.pending_for(job.file_id, SETTINGS.cache_dir)
                and _so_pre.select_targets(probe_data)):
            if not _side_ocr_allowed():
                # Not a failure and not worth queueing behind: the subtitle
                # backlog already owns this file and will reach it. Better a
                # second rewrite later than twenty tesseracts now.
                joblog.log("this file also needs subtitle OCR, but the OCR "
                           "budget is full - leaving it to the subtitle "
                           "queue rather than oversubscribing the CPU",
                           "info", job.id)
            else:
                joblog.log("this file also needs subtitle OCR - running it "
                           "alongside the encode so one rewrite carries both",
                           "info", job.id)
                w.sub_ocr_active = True      # claims a subocr slot from here
                _sub_task = asyncio.create_task(asyncio.to_thread(
                    _so_pre.produce, job.path, probe_data, job.file_id,
                    SETTINGS.cache_dir, _sub_tick,
                    _library_of_file(job.file_id)))
    except Exception as e:
        joblog.log(f"could not start the side OCR (continuing without): "
                   f"{type(e).__name__}: {e}", "warn", job.id)
        _sub_task = None

    cmd = build_ffmpeg(job.path, out, job.plan, w.duration, probe_data)
    joblog.log("ffmpeg " + " ".join(shlex.quote(c) for c in cmd[1:]), "debug", job.id)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
    w.proc = proc

    async def read_progress() -> None:
        """Parse ffmpeg -progress key=value output."""
        assert proc.stdout
        cur: dict[str, str] = {}
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            cur[k] = v
            if k != "progress":          # 'progress' terminates each block
                continue
            # ffmpeg reports elapsed output time three different ways and any
            # of them can be "N/A" depending on the input. Taking only the
            # first left one job sitting at 0% while its byte count climbed.
            done = 0.0
            for k in ("out_time_us", "out_time_ms"):
                v = cur.get(k, "")
                if v and v != "N/A":
                    try:
                        done = int(v) / 1_000_000   # both are microseconds
                        break
                    except ValueError:
                        pass
            if not done:
                done = _hms("time=" + cur.get("out_time", ""))
            try:
                w.fps = float(cur.get("fps") or 0)
            except ValueError:
                pass
            try:
                w.speed = float(str(cur.get("speed", "0")).rstrip("x") or 0)
            except ValueError:
                pass
            if w.stage != "encoding":
                w.set_stage("encoding")

            # --- IS out_time ACTUALLY MOVING? --------------------------------
            # On some files it is not, and everything derived from it is then a
            # lie. Measured on a Dolby Vision remux: total_size climbed from
            # 487 MB to 2.75 GB while out_time_us stayed pinned at 64000 - i.e.
            # 0.064 SECONDS of reported output for 2.75 GB written. The card
            # showed "0.0%", "ETA —" and "0.00x" for a job that was copying at
            # 139 MB/s and would finish fine.
            #
            # ffmpeg derives out_time from the muxed packet timestamps, and
            # this file's DV enhancement-layer BlockAdditions confuse that (the
            # matroska demuxer warns about exactly those blocks). It is not
            # something nuarr can fix in ffmpeg, so the job here is to notice
            # and stop trusting the number.
            if done > w._last_done + 0.001:
                w._last_done = done
                w._stuck = 0
            elif w.out_bytes:                 # only counts while bytes DO move
                w._stuck += 1
            cur_bytes = 0
            try:
                cur_bytes = int(cur.get("total_size") or 0)
            except (TypeError, ValueError):
                pass
            # Three consecutive blocks with bytes moving and the clock frozen is
            # not a slow section, it is a broken timestamp.
            timeline_ok = w._stuck < 3

            if w.duration and done and timeline_ok:
                w.progress = min(done / w.duration, 1.0)
                # Smooth the ETA. ffmpeg's instantaneous speed swings hard on a
                # stream copy - 169x one second, 4000x the next - so a raw
                # remaining/speed jumped between "3s" and "4m" every refresh.
                # A running average of recent speed gives a figure that settles.
                if w.speed > 0:
                    w._spd = (0.3 * w.speed + 0.7 * (w._spd or w.speed))
                    w.eta_s = max((w.duration - done) / max(w._spd, 0.01), 0)
            elif not timeline_ok and cur_bytes and w.src_bytes:
                # FALL BACK TO BYTES. For a stream copy the output is within a
                # percent or two of the input - this one only strips NAL 62 -
                # so bytes-written over source-size is an honest progress
                # figure, and far better than a permanent 0%. It is explicitly
                # NOT used for a real encode, where output size bears no fixed
                # relation to input.
                w.by_bytes = True
                w.progress = min(cur_bytes / w.src_bytes, 0.999)
                if w.write_bps > 0:
                    left = max(w.src_bytes - cur_bytes, 0)
                    w.eta_s = left / max(w.write_bps, 1.0)
                # The x-multiplier is derived from out_time too, so it reads
                # 0.00x while the copy runs flat out. Blank it rather than
                # print a number that is wrong.
                w.speed = 0.0
            # --- disk throughput -------------------------------------------
            # ffmpeg's total_size is bytes written so far. Differencing it over
            # wall time gives real write throughput, which for a copy job is
            # the only thing that matters.
            try:
                w.out_bytes = int(cur.get("total_size") or 0)
            except (TypeError, ValueError):
                w.out_bytes = w.out_bytes
            now_t = time.time()
            # out_bytes is kept for the SIZE readout; the write RATE is no
            # longer derived here. Differencing total_size only produced a
            # value when the count grew, so it could rise and never fall -
            # see sample_io(), which now owns both rates and lets them decay.
            if w._last_bytes_at is None or w.out_bytes > w._last_bytes:
                w._last_bytes, w._last_bytes_at = w.out_bytes, now_t
            # Projected final size, so "1.6 of ~3.4 GB" is possible.
            #
            # NOT WHILE PROGRESS IS BYTE-DERIVED. The projection is
            # out_bytes / progress, and the byte fallback above sets progress to
            # out_bytes / src_bytes - so the two cancel and the "estimate" comes
            # out as EXACTLY the source size, every time. The card then reported
            # "about the same size" for a file that finished 55% smaller, and
            # flickered between that and the true projection each time ffmpeg's
            # out_time started and stopped advancing.
            #
            # A circular estimate is worse than no estimate: this leaves the
            # last timeline-derived value standing, and shows nothing at all on
            # a file that never reports a timeline.
            if w.progress > 0.02 and w.out_bytes and not w.by_bytes:
                w.est_out_bytes = int(w.out_bytes / w.progress)

            # "67648098627 bytes" is not a number anybody can read at a glance,
            # and it sat right under the progress bar where the eye lands.
            # Format it here so every consumer - UI, log, API - gets it right.
            if w.by_bytes:
                # Don't print "0.0/91.4 min ... 0.00x" when neither figure is
                # real. Say what is actually known: bytes out of the source
                # size, and the write rate.
                w.last_line = (
                    f"{_human_bytes(cur_bytes)} of ~{_human_bytes(w.src_bytes)}  "
                    f"{w.fps:.0f} fps  "
                    f"{_human_bytes(w.write_bps)}/s  "
                    f"(ffmpeg reports no output timeline for this file)")
            else:
                w.last_line = (f"{done/60:.1f}/{w.duration/60:.1f} min  "
                               f"{w.fps:.0f} fps  {w.speed:.2f}x  "
                               f"{_human_bytes(cur.get('total_size'))}")
            cur.clear()

    # KEEP THE LAST LINES WHATEVER THEY SAY.
    #
    # The keyword filter below is right for the live log - a healthy encode
    # writes hundreds of harmless stderr lines - but it was also the only thing
    # retained, and the lines that actually explain a failure often match none
    # of those words. The WebVTT failure said:
    #
    #   [matroska] Subtitle codec 0 is not supported.
    #   [out#0/matroska] Could not write header ...: Function not implemented
    #
    # Neither contains "error", "invalid", "failed" or "unable", so the job
    # recorded a bare exit code and the reason had to be recovered by running
    # the command again by hand. The tail is kept unconditionally and attached
    # to the failure; on success it costs one small deque and is discarded.
    tail: collections.deque[str] = collections.deque(maxlen=12)

    async def read_errors() -> None:
        assert proc.stderr
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail.append(line)
            if any(k in line.lower()
                   for k in ("error", "invalid", "failed", "unable")):
                joblog.log(line, "error", job.id)

    await asyncio.gather(read_progress(), read_errors())
    rc = await proc.wait()
    if w.cancelled:
        await asyncio.to_thread(_rm, out)
        raise asyncio.CancelledError()
    if rc != 0:
        await asyncio.to_thread(_rm, out)
        for line in tail:
            joblog.log(line, "error", job.id)
        # The most specific line ffmpeg produced, next to nuarr's reading of
        # the exit code, so the stored error is self-explanatory.
        why = next((l for l in reversed(tail)
                    if "not supported" in l.lower()
                    or "could not" in l.lower()
                    or "no such" in l.lower()), "")
        raise RuntimeError(_ffmpeg_exit(rc) + (f" — {why}" if why else ""))

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        await asyncio.to_thread(_rm, out)
        raise RuntimeError("output missing or 0 bytes")

    # FINISH THE DOLBY VISION STRIP.
    #
    # -bsf:v filter_units=remove_types=62 removes the RPU from the BITSTREAM,
    # and it does work - 722 NALs to zero on a measured Profile 8 file. But DV
    # is signalled twice, and the second place is a dvvC record in the
    # container's BlockAdditionMapping, which ffmpeg's Matroska muxer writes
    # from the stream's side data no matter what the bitstream filter did.
    #
    # So every "stripped" file still reported dv_profile=8, rpu_present_flag=1.
    # Plex reads the container, so it kept treating them as Dolby Vision while
    # the DV layer was gone - the strip cost a full rewrite and changed nothing
    # that mattered. 388 files were made that way before this was caught.
    #
    # The patch is ~70 bytes of header written in place, so it costs nothing
    # next to the remux that has just finished. Failure is logged and ignored:
    # a file that keeps its DV record is exactly what we had before, and is not
    # worth failing a completed encode over.
    if getattr(job.plan, "strip_dv", False):
        try:
            changed, msg = await asyncio.to_thread(
                dvfix.strip_container_dv, out)
            joblog.log(msg, "ok" if changed else "warn", job.id)
        except Exception as e:
            joblog.log(f"container DV strip failed: {type(e).__name__}: {e}",
                       "warn", job.id)

    size_after = os.path.getsize(out)
    ratio = size_after / size_before
    joblog.log(f"encoded ok: {size_before/1024**2:.0f} MB -> {size_after/1024**2:.0f} MB "
               f"({ratio*100:.0f}%)", "ok", job.id)

    # SIZE GATE - but only where size is the point.
    #
    # This exists because "shrink" jobs that came out 30-50% LARGER were being
    # written over the original, and a shrink that grows the file is a failed
    # shrink. That reasoning is sound and stays.
    #
    # It does NOT apply to a compatibility conversion. Re-encoding AV1 to HEVC
    # makes the file bigger by design - AV1 is the more efficient codec, so the
    # same picture costs more bits in HEVC. Measured across 55 of them: none
    # shrank, median 101%, max 140%, every one discarded. The GPU time was
    # spent, the result thrown away, and the file left in the very codec the
    # conversion existed to remove. The gate was asking "did this save space"
    # when the question was "does this play".
    #
    # So a plan that is encoding for compatibility (plan.grow_ok) is judged
    # against a sanity ceiling instead. The bitrate is already capped at
    # maxrateFactor x source, so a healthy conversion lands near that; anything
    # far beyond it means something went wrong and is still discarded.
    grow_ok = bool(getattr(job.plan, "grow_ok", False))
    limit = MAX_GROWTH if grow_ok else (1.0 - MIN_SAVING)
    if job.plan.encode and ratio > limit:
        await asyncio.to_thread(_rm, out)
        if grow_ok:
            msg = (f"discarded: re-encode produced {ratio*100:.0f}% of the "
                   f"original ({size_before/1024**2:.0f} MB -> "
                   f"{size_after/1024**2:.0f} MB), past the "
                   f"{MAX_GROWTH*100:.0f}% ceiling for a compatibility "
                   f"conversion - the encode looks wrong, not merely larger")
            reason = "compatibility re-encode grew beyond the ceiling"
        else:
            msg = (f"discarded: re-encode produced {ratio*100:.0f}% of the "
                   f"original ({size_before/1024**2:.0f} MB -> "
                   f"{size_after/1024**2:.0f} MB); needs to save at least "
                   f"{MIN_SAVING*100:.0f}% to be worth keeping")
            reason = "re-encode would not shrink it"
        joblog.log(msg, "warn", job.id)
        with cursor() as cur:
            cur.execute("UPDATE files SET state='done', state_reason=? "
                        "WHERE id=?", (reason, job.file_id))
        _finish(job, "skipped", size_before, size_after, msg)
        return
    if grow_ok and ratio > 1.0:
        joblog.log(f"kept at {ratio*100:.0f}% of the original - this conversion "
                   f"is for playback compatibility, not to save space",
                   "info", job.id)

    # CARRY PREPARED SUBTITLES IN. If a subocr job produced SRTs for this file
    # (the handoff in _sub_ocr), embed them into the cache output NOW, between
    # encode and commit - a cache-local mkvmerge pass measured in seconds,
    # against the alternative of rewriting the whole file onto the pool a
    # second time. Text subs land first, image subs get demoted, exactly as
    # the standalone path does, because it IS the standalone path's embed.
    #
    # Deliberately NOT part of build_ffmpeg: splicing extra inputs into the
    # encode command means re-deriving map indices and dispositions inside the
    # most intricate command builder in the system, and a mistake there breaks
    # every job. Failure here is non-fatal - the pending subs stay on disk and
    # the subocr safety net embeds them on its next pass.
    # If the side OCR is still grinding (a movie-length track against a short
    # remux), wait for it - even ten minutes here is cheaper than the second
    # full pool rewrite it saves. A failure or timeout is non-fatal: the
    # subocr backlog remains the safety net for this file.
    if _sub_task is not None:
        # Poll the OCR's own reading into the stage line instead of waiting
        # blind. The encode is finished by now, so w.progress is free to carry
        # the OCR's fraction - which is the only work still happening.
        async def _mirror_ocr() -> None:
            while True:
                st = w.sub_ocr_stage or "working"
                w.set_stage(f"waiting for subtitle OCR - {st}")
                w.progress = w.sub_ocr_frac
                await asyncio.sleep(2)

        _mirror = asyncio.create_task(_mirror_ocr())
        try:
            r = await asyncio.wait_for(_sub_task, timeout=3600)
            if r.get("ok"):
                joblog.log(f"side OCR finished: {r['tracks']} track(s) "
                           f"prepared", "ok", job.id)
            else:
                joblog.log(f"side OCR produced nothing: {r.get('why')}",
                           "info", job.id)
        except Exception as e:
            joblog.log(f"side OCR did not finish ({type(e).__name__}); "
                       f"the subtitle queue will cover this file later",
                       "warn", job.id)
        finally:
            # An orphaned mirror would keep rewriting the stage of a job that
            # has moved on to committing.
            _mirror.cancel()
            w.sub_ocr_stage = ""       # the row stops claiming OCR is running
            w.sub_ocr_frac = 0.0
            # RELEASE THE SLOT HERE, in the finally, because every way out of
            # the wait above ends here - success, "produced nothing", timeout
            # and cancellation alike. A slot leaked on the failure path would
            # shrink the OCR budget for the life of the process, one file at a
            # time, and the only symptom would be side OCR quietly stopping.
            w.sub_ocr_active = False
    try:
        from . import subocr as _so
        _pend = _so.pending_for(job.file_id, SETTINGS.cache_dir)
    except Exception:
        _pend = []
    # Subtitles ffmpeg could not read out of the container join the same embed.
    # They were excluded from the encode command precisely so it would not die
    # on them; this is where they come back.
    if unreadable_subs(job.plan):
        w.set_stage("rescuing subtitles ffmpeg could not read")

        def _fix_progress(frac: float, stage: str) -> None:
            w.sub_fix_frac = frac
            w.sub_fix_stage = stage

        try:
            _pend = _pend + await asyncio.to_thread(
                rescue_subs, job.path, job.plan, SETTINGS.cache_dir, job.id,
                _fix_progress)
        except Exception as e:
            joblog.log(f"subtitle rescue step failed ({type(e).__name__}); "
                       f"continuing without those tracks", "warn", job.id)
        finally:
            # The strip disappears when the work does. Left set, a finished
            # rescue would keep a full bar on the card through the commit.
            w.sub_fix_stage = ""
            w.sub_fix_frac = 0.0
    _pend_used = False
    if _pend:
        w.set_stage("embedding subtitles")
        joblog.log(f"picking up {len(_pend)} prepared OCR subtitle track(s) "
                   f"before the commit", "info", job.id)

        def _mux_into_out():
            # Via a sibling temp name, then back onto the canonical cache
            # path: restart recovery and the deferred-commit queue both key on
            # <job_id>.mkv, and a differently-named output would be invisible
            # to them.
            tmp = out + ".subs"
            _so.embed(out, [(p["srt"], p.get("name") or "English (OCR)")
                            for p in _pend], tmp)
            os.remove(out)
            os.replace(tmp, out)
        try:
            await asyncio.to_thread(_mux_into_out)
            size_after = os.path.getsize(out)
            _pend_used = True
            joblog.log(f"subtitles embedded; output is now "
                       f"{size_after/2**20:.1f} MB", "ok", job.id)
        except Exception as e:
            joblog.log(f"could not embed the prepared subtitles - kept on "
                       f"disk for the next subtitle pass: "
                       f"{type(e).__name__}: {e}", "warn", job.id)

    # RE-RESOLVE THE TARGET BEFORE COMMITTING.
    # job.path was captured at enqueue, and an encode can run for an hour. If
    # Radarr or Sonarr renames the file in that window - a rename triggered by
    # an edited naming format, or by the post-commit rename of a sibling - the
    # path we started with no longer exists. Committing to it would resurrect
    # the old filename and leave the arr tracking a file nobody wrote to.
    # The whole point of keying on (arr_name, arr_file_id) is that the path is
    # a mutable attribute, so ask the arr where the file lives NOW.
    target = await _current_path(job)
    if target and os.path.normcase(target) != os.path.normcase(job.path):
        joblog.log(f"file was renamed during the encode -> committing to "
                   f"{os.path.basename(target)}", "warn", job.id)
        job.path = target

    # THE BYTES MUST STILL BE THE BYTES WE READ.
    #
    # The rename guard above covers a MOVED file, because the arr still tracks
    # its id. An UPGRADE is invisible to it: the old arr_file_id is deleted
    # with the old file, _current_path finds nothing, and the commit would
    # proceed against whatever now sits at the path - overwriting the better
    # release the arr just imported with a transcode of the file it replaced,
    # and then deleting the backup. Compare size+mtime against the stamp taken
    # when the encode started: unchanged across a rename, changed by any
    # replacement. On mismatch the output is worthless by definition - it was
    # made from bytes that are gone - so discard it and let the new file be
    # planned on its own merits.
    try:
        _now_stamp = (os.path.getsize(job.path), os.path.getmtime(job.path))
    except OSError:
        _now_stamp = None
    if _now_stamp != _src_stamp:
        joblog.log("the source was replaced while this encode ran (an arr "
                   "upgrade landed) - discarding the output rather than "
                   "overwriting the new file with a transcode of the old one",
                   "warn", job.id)
        await asyncio.to_thread(_rm, out)
        try:
            from . import subocr as _so_stale
            # Any subtitles OCR'd alongside came from the old bytes too.
            _so_stale.clear_pending(job.file_id, SETTINGS.cache_dir)
        except Exception:
            pass
        try:
            with cursor() as cur:
                cur.execute("UPDATE files SET state='eligible', "
                            "state_reason=NULL WHERE id=?", (job.file_id,))
        except Exception:
            pass
        _finish(job, "skipped", size_before, 0,
                "source replaced during the encode - output discarded, the "
                "new file will be planned fresh")
        return

    # Commit through fileops: waits for locks, verifies, can roll back. The
    # original is never removed until the replacement is verified in place.
    # Commit can outlast the encode on a big remux: it waits for locks, stages
    # across volumes and verifies. Name the stage so the panel stops reading
    # "100% - ETA 0s" while this is still working.
    # Record the VERIFIED output size before the commit starts. This is the
    # only moment we know for certain that the encode is complete - ffmpeg has
    # exited 0 and the size gate has passed - and a restart during the copy
    # that follows has no other way to tell a finished output from a partial
    # one. See _finished_output().
    try:
        with cursor() as cur:
            cur.execute("UPDATE jobs SET size_before=?, size_after=? "
                        "WHERE job_id=?", (size_before, size_after, job.id))
    except Exception:
        pass
    w.set_stage("committing")
    joblog.log("committing to the library...", "info", job.id)

    # Shared with _sub_ocr - see _commit_stage_cb for why one copy exists.
    _on_stage = _commit_stage_cb(w)

    def _pace():
        r"""How hard to hold the commit back, checked per 8 MB chunk.

        The destination disk is not known when the copy starts - DrivePool
        decides placement - so this is re-evaluated live rather than fixed up
        front. _on_stage fills in w.dest_disk within the first chunk or two.

        3.0 means "sleep three times as long as the chunk took", i.e. roughly a
        quarter speed. Enough to stay out of a viewer's way on a spindle they
        are streaming from, without turning a 20 GB commit into an overnight
        job. Returns 0 - full speed - whenever nothing is competing.
        """
        try:
            if not get_toggle_safe("gate.plex_io_throttle"):
                return 0.0
            dest = getattr(w, "dest_disk", "") or ""
            src_disk = getattr(w, "disk", "") or ""
            watched = _plex_disks_safe()
            mine = ({dest, src_disk} - {""})
            hit = mine & watched if watched else set()
            if not hit:
                return 0.0
            # HOW THIN IS THE VIEWER'S BUFFER on the spindle being touched.
            # A quarter of a 150 MB/s commit is still 37 MB/s competing with a
            # viewer who has seconds in hand; below the floor the only useful
            # thing this copy can do is stop. -1 pauses; 0 means the viewer is
            # far enough ahead that there is nothing to protect them from.
            f = viewer_pace(hit)
            w.paused_for_viewer = (f < 0)
            return f
        except Exception:
            return 0.0

    res = await asyncio.to_thread(fileops.safe_replace, job.path, out,
                                  on_stage=_on_stage, pace=_pace)
    w.commit_phase = ""
    if not res.ok:
        # DO NOT throw the encode away. safe_replace already exhausted its own
        # retries, which means a long-lived lock - Plex streaming it, a backup,
        # an AV scan. Deleting the output here meant re-encoding the file from
        # scratch later, straight back into the same lock. Keep it and retry
        # the swap on a timer; the expensive half is already done.
        from . import commitqueue
        commitqueue.enqueue(job.id, job.file_id, job.path, out,
                            size_before, size_after, res.detail)
        # Embedded subs travel WITH the deferred output, so the pending copies
        # are spent even though the commit has not landed yet.
        if _pend_used:
            try:
                from . import subocr as _so2
                _so2.clear_pending(job.file_id, SETTINGS.cache_dir)
            except Exception:
                pass
        _finish(job, "deferred", size_before, size_after,
                f"commit deferred: {res.detail}")
        return
    # THE OUTPUT IS ALWAYS MATROSKA, SO THE NAME MUST SAY SO.
    #
    # Not conditional on the plan carrying a "repackage" action: the cache file
    # is <job_id>.mkv for every job, passthrough included, so any source that
    # was not already .mkv now holds Matroska. Deriving it from the extension
    # cannot drift from what was actually written; a plan flag could.
    if os.path.splitext(job.path)[1].lower() != ".mkv":
        rr = await asyncio.to_thread(fileops.fix_container_extension, job.path)
        if rr.ok and rr.detail.lower().endswith(".mkv"):
            old, job.path = job.path, rr.detail
            with cursor() as cur:
                cur.execute("UPDATE files SET path=? WHERE id=?",
                            (job.path, job.file_id))
            joblog.log(f"renamed to match its container: "
                       f"{os.path.basename(old)} -> {os.path.basename(job.path)}",
                       "ok", job.id)
        elif not rr.ok:
            # Worth saying, not worth failing over: the file is correct, only
            # its name is wrong, which is exactly where it was a moment ago.
            joblog.log(f"could not rename to .mkv ({rr.detail}); the file is "
                       f"Matroska but keeps its {os.path.splitext(job.path)[1]} "
                       f"name", "warn", job.id)

    # The commit STAGES across volumes rather than moving, so the cache copy is
    # still there afterwards. Left alone it silently refills E: - which is the
    # same way Tdarr accumulated 150 GB of abandoned cache files.
    await asyncio.to_thread(_rm, out)
    if _pend_used:
        try:
            from . import subocr as _so2
            _so2.clear_pending(job.file_id, SETTINGS.cache_dir)
        except Exception:
            pass
    # WHERE DID IT LAND?
    # safe_replace moves the original aside and writes the new file, so
    # DrivePool re-places it - often on a different spindle than the source.
    # Two reasons to resolve that now rather than wait for a scan:
    #   * files.pool_disk would otherwise be stale, and disk-aware claiming
    #     reads exactly that column to decide what to run next;
    #   * "moved NU-DRIVE-1 -> NU-DRIVE-4" is worth seeing in the activity feed.
    try:
        from . import scanner as _sc
        dest = await asyncio.to_thread(_sc.disk_of, job.path) or ""
    except Exception:
        dest = ""
    w.dest_disk = dest
    moved = bool(dest and w.disk and dest != w.disk)
    if dest:
        with cursor() as cur:
            cur.execute("UPDATE files SET pool_disk=? WHERE id=?",
                        (dest, job.file_id))
    if moved:
        joblog.log(f"file moved disk: {w.disk} -> {dest}", "warn", job.id)
    elif dest:
        joblog.log(f"stayed on {dest}", "debug", job.id)

    joblog.log(f"committed: {res.detail}", "ok", job.id)
    _finish(job, "done", size_before, size_after)
    # Record it against the file so Recent activity can show the move.
    try:
        pct = ((size_after - size_before) / size_before * 100) if size_before else 0
        where = (f"moved {w.disk} -> {dest}" if moved
                 else (f"on {dest}" if dest else ""))
        log_event(job.file_id, "transcoded",
                  f"{job.plan.summary() if job.plan else 'processed'} · "
                  f"{pct:+.1f}% size" + (f" · {where}" if where else ""))
    except Exception:
        pass
    # An arr refresh waits on RefreshSeries, which on a large series is the
    # slowest thing in the whole job. It is still work, so keep it visible.
    w.set_stage("arr refresh / rename")
    await _post_commit(job)
    w.set_stage("done")


async def _post_commit(job: Job) -> None:
    """Hand the file off to the arr WITHOUT holding a worker slot.

    This used to refresh the arr and wait for the command to finish, then plan
    and apply the rename - all inline. Measured, that parked every worker in
    "updating Sonarr/Radarr" for ~2 minutes while the encodes themselves took
    seconds, because:

      * the arrs execute commands SERIALLY, so four finishing workers queue
        behind each other;
      * _refresh_lock then serialises them a second time on our side;
      * wait_command polls for up to 300 s.

    A worker slot is the scarce resource - it gates GPU throughput - and none
    of that waiting needs one. rename_queue already does exactly this work on
    its own loop, with a FORCED refresh (no debounce) and backoff, so the
    correct move is to record the intent and return immediately.

    The refresh is still fired here, just not awaited, so a single file that
    finishes alone still updates promptly.
    """
    from . import renamequeue

    with cursor() as cur:
        row = cur.execute("SELECT arr_name, arr_parent_id FROM files WHERE id=?",
                          (job.file_id,)).fetchone()
    if not row or not row["arr_name"] or not row["arr_parent_id"]:
        joblog.log("no arr record for this file - skipping refresh/rename",
                   "debug", job.id)
        return

    cfg = next((c for c in SETTINGS.arrs if c.name == row["arr_name"]), None)
    if not cfg:
        return

    # Fire the refresh and move on. Debounced per parent so a finishing season
    # does not trigger one full RefreshSeries per episode - the storm that
    # drove 6,249 folder scans a day here before.
    pkey = (cfg.name, row["arr_parent_id"])
    async with _refresh_lock:
        now = time.time()
        # Same dead-entry sweep as audiolang's _ARR_TOLD: past the debounce
        # window an entry cannot change any answer, so it is pure residue.
        # Done under the lock that already serialises this map.
        if len(_LAST_REFRESH) > 256:
            for k in [k for k, t in _LAST_REFRESH.items()
                      if now - t > REFRESH_DEBOUNCE_S]:
                _LAST_REFRESH.pop(k, None)
        age = now - _LAST_REFRESH.get(pkey, 0.0)
        due = age >= REFRESH_DEBOUNCE_S
        if due:
            _LAST_REFRESH[pkey] = time.time()

    if due:
        async def _kick() -> None:
            # Imported here, not at module scope: arr pulls in config which
            # comes back through this module. Without it the whole coroutine
            # died on NameError and the arr was never told the file changed -
            # silently, because a stray task exception only reaches the log.
            from .arr import ArrClient
            client = ArrClient(cfg)
            try:
                # notify_file_changed sends Rescan*, not Refresh* - the file
                # changed on disk, the show's metadata did not.
                await client.notify_file_changed(row["arr_parent_id"])
            except Exception as e:
                joblog.log(f"refresh kick failed (rename queue will retry): {e}",
                           "debug", job.id)
            finally:
                await client.close()
        asyncio.create_task(_kick())
        joblog.log(f"asked {cfg.name} to re-read this file; rename handled by "
                   f"the retry queue", "info", job.id)
    else:
        joblog.log(f"{cfg.name} refreshed this title {age:.0f}s ago - rename "
                   f"queued", "debug", job.id)

    # The queue owns the rename from here: it forces its own refresh before
    # planning, so it can never ask against stale mediainfo.
    renamequeue.enqueue(job.file_id, cfg.name, row["arr_parent_id"],
                        job.path, "post-transcode")


async def _run_inline_handlers(job: Job, data: dict) -> bool:
    """No longer runs anything. Kept as the single call site it always was.

    This used to pick which of the PowerShell handlers a file qualified for
    and run each in turn after a transcode. Every one of those handlers was
    audited and removed: none had ever run, and the conditions they fixed all
    measure zero across the library - subocr.py and the strip_dv rule do that
    work inline now. Returning False means "nothing changed the file", which
    is exactly true.
    """
    return False


def _retire_handler_job(job) -> None:
    """Close out a queued job whose handler kind no longer exists.

    Retired, not failed. The file is fine; the KIND of work was removed. A
    failure would land it in the Errors tile and invite a retry of something
    that cannot run.
    """
    joblog.log(f"'{job.kind}' was a PowerShell handler; that subsystem was "
               f"removed after an audit found none had ever run and every "
               f"condition they fixed now measures zero. Retiring this job - "
               f"the file is untouched.", "warn", job.id)
    _finish(job, "done", 0, 0, None)


def _defer(job: Job, why: str) -> None:
    """Put a job back on the queue because the file is busy right now.

    Deliberately NOT a failure: nothing is wrong with the file, someone is just
    watching it. It goes to the back of its pool so other work proceeds, and is
    retried on a later pass.
    """
    with cursor() as cur:
        cur.execute("UPDATE jobs SET state='queued', worker=NULL, started_at=NULL, "
                    "error=?, priority=priority+5 WHERE job_id=?", (why, job.id))


def _purge_job_cache(job_id: str) -> int:
    """Remove every scratch file belonging to one job."""
    import glob as _g
    n = 0
    for f in _g.glob(os.path.join(SETTINGS.cache_dir, f"{job_id}*")):
        try:
            os.remove(f)
            n += 1
        except OSError:
            pass
    return n


def purge_stale_cache(max_age_h: float = 6.0) -> tuple[int, float]:
    """Sweep cache files that belong to no running job.

    Called at start-up. Anything left behind by a kill or a power cut is dead
    weight - the job that owned it is gone and will never come back for it.
    """
    import glob as _g
    live = set(RUNNING.keys())
    # A deferred commit is holding a finished encode in the cache waiting for
    # the target to unlock. Purging it would destroy the very output the retry
    # exists to preserve - and this runs at STARTUP, exactly when a pending
    # commit from before the restart is most likely to be sitting there.
    try:
        from . import commitqueue
        commitqueue.init()
        keep = commitqueue.pending_cache_paths()
    except Exception:
        keep = set()
    freed = 0.0
    n = 0
    cutoff = time.time() - max_age_h * 3600
    for f in _g.glob(os.path.join(SETTINGS.cache_dir, "*")):
        stem = os.path.splitext(os.path.basename(f))[0].split(".")[0]
        if stem in live or os.path.normcase(f) in keep:
            continue
        try:
            st = os.stat(f)
            if st.st_mtime > cutoff and stem in live:
                continue
            freed += st.st_size
            os.remove(f)
            n += 1
        except OSError:
            pass
    return n, freed / 1024 ** 3


def _rm(p: str) -> None:
    """Delete a staged file. Callers in _transcode MUST await this in a thread.

    The staged output is a full copy of the media - routinely tens of GB - and
    os.remove() on a file that size, on a cache disk the encoders are hammering,
    is not instant. Called bare from a coroutine it stopped the web server dead
    for the duration; py-spy caught the loop inside it.
    """
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


# HOW EACH JOB ACTUALLY ENDED, remembered in memory for the fast poll.
#
# The worker cards are driven by live_snapshot(), which deliberately carries no
# Finished list - that is the whole reason it is fast. But the panel leaves a
# ghost card behind when a job disappears, and it stamped that ghost from the
# Finished list it did not have. With nothing to read, every ghost fell back to
# a green "done", including jobs that were deferred, skipped, cancelled or
# failed.
#
# "deferred" is the one that matters: the encode finished, the swap could not
# happen, and the file is still sitting in the cache waiting for the commit
# queue. Calling that "done" says the file has been placed when it has not.
#
# A dict of the last few outcomes costs nothing and travels with the fast poll.
FATE: dict[str, str] = {}
_FATE_MAX = 60


def _remember_fate(job_id: str, state: str) -> None:
    FATE[job_id] = state
    if len(FATE) > _FATE_MAX:
        for k in list(FATE)[:len(FATE) - _FATE_MAX]:
            FATE.pop(k, None)


# THE STAGE A FINISHED JOB SHOULD BE LEFT SITTING IN.
#
# _transcode walks itself all the way to set_stage("done"), so its cards settle
# on "done" and the ghost the panel leaves behind agrees with the stamp. No
# other job kind does. _sub_ocr's last set_stage is "committing" (it has its own
# commit block and returns straight after _finish), so every OCR card froze on
# "committing - placing..." while the ghost stamped a green "done" over the top
# of it - two different claims about the same job in the same card. Measured
# over the last 400 finished jobs: 8 of 8 subocr rows ended stage='committing'.
#
# Setting it here rather than at each call site means the guarantee holds for
# every kind and every exit path, including the ones that return early
# (skipped, rejected, deferred) and any added later. _transcode still sets
# "arr refresh / rename" AFTER this and settles on "done" itself; that is a
# real stage with real waiting behind it, so it is allowed to override.
_END_STAGE = {
    "done": "done",
    "failed": "failed",
    "skipped": "skipped",
    "deferred": "waiting to commit",
    "cancelled": "cancelled",
}


def _finish_stage(job_id: str, state: str) -> None:
    try:
        w = RUNNING.get(job_id)
        if w is not None:
            w.set_stage(_END_STAGE.get(state, state))
    except Exception:
        pass                    # bookkeeping must never fail a job


def _finish(job: Job, state: str, before: int, after: int,
            error: str | None = None, note: str = "",
            event: str = "") -> None:
    _remember_fate(job.id, state)
    _finish_stage(job.id, state)
    # Count every outcome toward completion, but only jobs that actually ran an
    # encoder contribute to the RATE. 'skipped' means nothing was transcoded, so
    # its near-zero wall time says nothing about how fast the queue drains.
    # Telemetry must never be able to fail a job. An earlier version read
    # job.pool - which does not exist, pool lives on the Worker - and because
    # this runs inside the job's try block it turned 20 healthy files into
    # "'Job' object has no attribute 'pool'" failures. Guard it.
    try:
        w = RUNNING.get(job.id)
        if state != "cancelled":
            did_work = state == "done" and bool(before or after)
            _batch_note_finish(w.pool if w else "encode",
                               w.started_at if w else time.time(),
                               (w.duration if w else 0.0) or 0.0, did_work)
            if state == "failed":
                BATCH["failed"] = BATCH.get("failed", 0) + 1
            elif state == "skipped":
                BATCH["skipped"] = BATCH.get("skipped", 0) + 1
    except Exception as e:
        joblog.log(f"batch telemetry error (ignored): {type(e).__name__}: {e}",
                   "debug", job.id)
    with cursor() as cur:
        # The note is persisted on the JOB ROW, not only sent to history. The
        # Finished panel derives its detail line from plan_json, which a
        # plan-less job does not have - so subocr entries rendered blank while
        # the history row quietly carried the text. result_json was an unused
        # column (zero writers, verified by grep before adopting it).
        cur.execute(
            "UPDATE jobs SET state=?, finished_at=?, size_before=?, size_after=?, "
            "error=?, result_json=COALESCE(?, result_json) WHERE job_id=?",
            (state, time.time(), before or None, after or None, error,
             json.dumps({"summary": note}) if note else None, job.id))
        if state == "done":
            cur.execute("UPDATE files SET state='done', processed_at=? WHERE id=?",
                        (time.time(), job.file_id))
        elif state == "failed":
            cur.execute("UPDATE files SET state='error', state_reason=? WHERE id=?",
                        (error, job.file_id))
        elif state == "skipped":
            cur.execute("UPDATE files SET state='done', state_reason='no work needed' "
                        "WHERE id=?", (job.file_id,))
    # `note` gives plan-less jobs a real detail line. A transcode's Finished
    # entry says what it did ("remux (stream copy): track 0: aac -> E-AC3");
    # a sub_ocr job has no plan, so its entry said nothing at all and the
    # Recent-activity row was just a bare "done".
    #
    # `event` lets a job kind name its own history event the way transcodes
    # already do with 'transcoded' - a wall of generic 'done' rows cannot be
    # filtered by process, and the events dropdown stocks itself from what
    # exists here.
    log_event(job.file_id, event or state,
              note or (job.plan.summary() if job.plan else ""),
              label=job.title)


async def cancel(job_id: str) -> bool:
    w = RUNNING.get(job_id)
    if not w:
        with cursor() as cur:
            cur.execute("UPDATE jobs SET state='cancelled', finished_at=? "
                        "WHERE job_id=? AND state='queued'", (time.time(), job_id))
            if cur.rowcount:
                joblog.log("removed from queue", "warn", job_id)
                return True
        return False
    w.cancelled = True
    joblog.log("cancel requested - killing ffmpeg", "warn", job_id)
    try:
        if w.proc:
            w.proc.kill()
    except Exception:
        pass
    return True


# --------------------------------------------- subtitles ffmpeg cannot read ---
# A SUBTITLE STREAM WITH NO CODEC NAME AT ALL.
#
# Not the same problem as mov_text below, though it produces the identical
# error. mov_text is a codec ffmpeg reads perfectly and Matroska cannot store,
# so converting it to srt is enough. These tracks are ones ffmpeg's MATROSKA
# DEMUXER does not map on the way IN: ffprobe reports codec_name null and
# codec_tag [0][0][0][0], and the muxer then refuses with
#
#   [matroska] Subtitle codec 0 is not supported.
#   [out#0/matroska] Could not write header: Function not implemented
#
# which exits -40 and kills the whole graph before a frame is written.
#
# Found on this library as S_TEXT/WEBVTT, which mkvmerge reads happily and
# ffmpeg does not. Two files, four tracks - but one of them was the ONLY
# English subtitle on a Cantonese film, so dropping the track is a real loss
# and rescue is worth the extra step. mkvextract writes the track out as a
# standalone .vtt, and ffmpeg reads THAT without complaint; it is only the
# in-container mapping that is missing.
def unreadable_subs(plan) -> set[int]:
    """Kept subtitle indices whose codec ffmpeg did not recognise."""
    codecs = getattr(plan, "sub_codecs", None) or []
    return {i for i in getattr(plan, "keep_subs", []) or []
            if i != getattr(plan, "burn_index", None)
            and i < len(codecs) and not (codecs[i] or "").strip()}


def _mkvextract_exe() -> str:
    for p in (r"C:\Program Files\MKVToolNix\mkvextract.exe",
              r"C:\Program Files (x86)\MKVToolNix\mkvextract.exe"):
        if os.path.exists(p):
            return p
    return "mkvextract"


def _mkvextract_progress(cmd: list[str], on_frac) -> None:
    """Run mkvextract, reporting its own percentage as it goes.

    mkvextract writes "Progress: 42%" lines, so the bar on the card is the
    tool's real position rather than a spinner pretending to be one. It has to
    read the whole container to find the track, so on a 4 GB source this is
    seconds of apparently-nothing otherwise.
    """
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", creationflags=NO_WINDOW,
                         bufsize=1)
    tail: collections.deque[str] = collections.deque(maxlen=8)
    try:
        for line in p.stdout or ():
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            m = re.search(r"Progress:\s*(\d+)%", line)
            if m and on_frac:
                on_frac(int(m.group(1)) / 100.0)
    finally:
        rc = p.wait()
    if rc != 0:
        raise RuntimeError(f"mkvextract exited {rc}: {' | '.join(tail)[:200]}")


def rescue_subs(src: str, plan, workdir: str, job_id: str = "",
                on_progress=None) -> list[dict]:
    """Pull out the tracks ffmpeg cannot read and convert them to SRT.

    Returns [{"srt": path, "name": label}] shaped for subocr.embed(), so these
    ride the SAME post-encode mkvmerge pass the OCR subtitles use rather than
    being spliced into the encode command. That is a deliberate reuse: the
    comment on that embed explains why extra ffmpeg inputs are not welcome in
    build_ffmpeg, and the reasoning applies here unchanged.

    Never raises. A rescue that fails costs the subtitle track; a rescue that
    throws would cost the whole job, and the job is the more valuable of the
    two.
    """
    want = sorted(unreadable_subs(plan))
    if not want:
        return []
    # The plan records sub_codecs but not languages, and a rescued track with
    # no language tag is one Plex will not offer to anybody. mkvmerge is being
    # asked for the track table anyway, and it has both.
    table = _mkv_sub_tracks(src)
    out: list[dict] = []

    # Progress is reported across ALL the tracks being rescued, not per track,
    # so a file with three of them shows one bar going to 100% once rather than
    # three bars each racing to 100%.
    def _report(done: int, frac: float, stage: str) -> None:
        if on_progress:
            on_progress((done + max(0.0, min(1.0, frac))) / len(want), stage)

    for n, i in enumerate(want):
        stem = os.path.join(workdir, f"{job_id or 'rescue'}.s{i}")
        vtt, srt = stem + ".vtt", stem + ".srt"
        info = table[i] if i < len(table) else {}
        lang = info.get("lang") or "und"
        label = info.get("name") or f"Subtitles ({lang})"
        try:
            # mkvextract counts ALL tracks together, not subtitles separately,
            # so the subtitle-relative index has to be resolved against the
            # file's own track table rather than assumed.
            tid = info.get("id")
            if tid is None:
                raise RuntimeError("no matching subtitle track in the container")
            _report(n, 0.0, f"extracting the {lang} subtitle track")
            _mkvextract_progress(
                [_mkvextract_exe(), src, "tracks", f"{tid}:{vtt}"],
                lambda f, _n=n: _report(_n, f * 0.9,
                                        f"extracting the {lang} subtitle track"))
            # The conversion is a text pass over a file measured in kilobytes -
            # it finishes faster than a poll, so it gets the last tenth of the
            # bar rather than a percentage of its own.
            _report(n, 0.9, f"converting the {lang} subtitle to SRT")
            subprocess.run([_ffmpeg_exe(), "-hide_banner", "-y", "-nostdin",
                            "-i", vtt, "-c:s", "srt", srt],
                           capture_output=True, timeout=600,
                           creationflags=NO_WINDOW, check=True)
            _report(n, 1.0, f"converting the {lang} subtitle to SRT")
            if os.path.getsize(srt) > 0:
                out.append({"srt": srt, "name": label, "lang": lang})
                joblog.log(f"rescued subtitle {i} ({lang}) — ffmpeg cannot read "
                           f"it from the container, so it was extracted and "
                           f"converted to SRT", "ok", job_id)
        except Exception as e:
            joblog.log(f"could not rescue subtitle {i} ({lang}): "
                       f"{type(e).__name__}: {e}. The file will be processed "
                       f"without that track rather than failing.", "warn", job_id)
        finally:
            if os.path.exists(vtt):
                try:
                    os.remove(vtt)
                except OSError:
                    pass
    return out


def _mkv_sub_tracks(src: str) -> list[dict]:
    """Subtitle tracks in ffmpeg's order, with the container id, lang and name.

    ffprobe's `s:N` index and mkvextract's track id are different numbers -
    mkvextract counts video and audio in the same sequence - so extracting
    "track 2" because ffmpeg called it subtitle 2 would pull out the wrong
    stream, or an audio track.
    """
    try:
        pr = subprocess.run([_mkvextract_exe().replace("mkvextract", "mkvmerge"),
                             "-J", src], capture_output=True, text=True,
                            timeout=120, creationflags=NO_WINDOW, errors="replace")
        tracks = json.loads(pr.stdout).get("tracks") or []
    except Exception:
        return []
    out = []
    for t in tracks:
        if t.get("type") != "subtitles":
            continue
        p = t.get("properties") or {}
        out.append({"id": t.get("id"), "lang": p.get("language") or "",
                    "name": p.get("track_name") or ""})
    return out


def _library_of_file(file_id: int) -> str | None:
    """Which library a job's file belongs to.

    The subtitle settings are per library and the job row does not carry the
    name, so it is looked up once per OCR job - a single indexed read against
    a decision that then governs the whole pass.
    """
    if not file_id:
        return None
    try:
        with cursor() as cur:
            r = cur.execute("SELECT library FROM files WHERE id=?",
                            (file_id,)).fetchone()
        return (r["library"] if r else None) or None
    except Exception:                                    # noqa: BLE001
        return None


# ---------------------------------------------------------------- ffmpeg ---
def build_ffmpeg(src: str, dst: str, plan, duration: float,
                 probe: dict | None = None) -> list[str]:
    """Translate a plan into an ffmpeg command.

    Deliberately explicit rather than clever: the exact command is written to
    the job log, so when an encode looks wrong you can copy the line and run it
    by hand.
    """
    C = rules.CONFIG
    # -progress writes key=value lines terminated by NEWLINE to stdout.
    # The default stats line ends with a CARRIAGE RETURN, so readline() blocks
    # until the process exits - which is why progress sat at 0% for minutes on
    # a long remux. -nostats silences the unparseable version.
    a: list[str] = [_ffmpeg_exe(), "-hide_banner", "-y", "-nostdin",
                    "-nostats", "-progress", "pipe:1"]

    # Hardware DECODE only makes sense when something is being encoded. On a
    # stream copy there is no decode step at all, so -hwaccel cuda just
    # initialises a CUDA context per process, costs startup time, and holds
    # driver resources the encode pool could be using. Copy jobs were carrying
    # it for no benefit.
    if plan.encode and (C["hwDecode"] if "hwDecode" in C else True):
        # THE DECODER MUST MATCH THE ENCODER'S FAMILY. Handing CUDA frames to
        # an AMF or QuickSync encoder forces a download and re-upload of every
        # frame, which is slower than never accelerating the decode at all -
        # and a CPU encode wants no hardware decode surface in the first place.
        _fam = (getattr(plan, "venc", None) or {}).get("family") or "nvenc"
        try:
            from . import encoders as _enc
            a += _enc.decode_args(_fam, True)
        except Exception:                                # noqa: BLE001
            a += ["-hwaccel", "cuda"]
    a += ["-i", src]

    if plan.encode:
        # THE PLAN'S OWN SETTINGS, not a fresh lookup. Codec settings are per
        # library now, and this function has a source path and a plan - it does
        # not know which library the file belongs to. decide() stamped what it
        # decided with, so the command cannot disagree with the plan that was
        # shown in the queue. A plan stored before venc existed has none, and
        # falls back to the global constants so old queued jobs still run.
        tgt = dict(C["videoTargets"][plan.target or "h264"])
        tgt.update(getattr(plan, "venc", None) or {})
        # BURN THE SIGNS/FORCED TRACK INTO THE PICTURE.
        # This was planned but never emitted: the chosen subtitle was simply
        # dropped from the output, so a "burn" plan silently DELETED the forced
        # track instead of painting it in. Image subs go through overlay (with
        # scale2ref so a 1080p PGS over a 720p encode fits); text subs use the
        # subtitles filter.
        burn = getattr(plan, "burn_index", None)
        # KEEP 10-BIT ALL THE WAY THROUGH THE CHAIN.
        #
        # Asking the encoder for p010le is not enough on its own. If a filter
        # has already converted to 8-bit, the encoder just pads what it was
        # handed and writes a 10-bit file carrying 8-bit information - the
        # banding is already baked in and the tag is a lie. The depth has to
        # survive the FILTERS, which is where it was being lost: overlay
        # defaults to 8-bit, and burning a signs/songs track is routine on
        # anime, which is exactly where the library showed it.
        ten = bool(getattr(plan, "ten_bit", False))
        if burn is not None:
            if getattr(plan, "burn_image", False):
                ov = "overlay=format=yuv420p10" if ten else "overlay"
                fc = (f"[0:s:{burn}][0:v:0]scale2ref[sp][vp];[vp][sp]{ov}[v]"
                      if C["scalePgsToVideo"] else
                      f"[0:v:0][0:s:{burn}]{ov}[v]")
                a += ["-filter_complex", fc, "-map", "[v]"]
            else:
                esc_src = src.replace("\\", "/").replace(":", "\\:")
                vf = f"subtitles='{esc_src}':si={burn}"
                if ten:
                    # libass composites in 8-bit RGB regardless, so this cannot
                    # make the SUBTITLE 10-bit - it puts the picture back into
                    # 10-bit before the encoder so the video itself is
                    # quantised at 10-bit precision rather than 8.
                    vf += ",format=yuv420p10le"
                a += ["-map", "0:v:0", "-vf", vf]
        else:
            a += ["-map", "0:v:0"]
        # THE QUALITY FLAGS ARE NOT PORTABLE. -rc vbr -cq is NVENC's spelling;
        # libx265 wants -crf, QuickSync -global_quality, AMF -rc cqp with a
        # quantiser per frame type. And 10-bit is p010le on the hardware
        # encoders but yuv420p10le in software. encoders.video_args owns that
        # table so this builder does not grow a branch per family.
        _fam = tgt.get("family") or "nvenc"
        _codec = "hevc" if (plan.target or "h264") == "h265" else "h264"
        try:
            from . import encoders as _enc
            a += _enc.video_args(_fam, _codec, int(tgt["cq"]),
                                 str(tgt["preset"]), ten)
        except Exception:                                # noqa: BLE001
            a += ["-c:v", tgt["encoder"], "-preset", tgt["preset"],
                  "-rc", "vbr", "-cq", str(tgt["cq"])]
            if ten:
                a += ["-pix_fmt", "p010le", "-profile:v", "main10"]
        # HDR SURVIVES THE ENCODE - it just has to be told to. Without these
        # the output carries PQ pixels under BT.709 tags and every player
        # renders it washed out, which is what "re-encoding loses the HDR"
        # always actually meant. See encoders.hdr_args.
        try:
            from . import encoders as _enc2
            vs = next((s for s in (probe or {}).get("streams", [])
                       if s.get("codec_type") == "video"), None)
            if vs:
                a += _enc2.hdr_args(vs, _fam)
        except Exception:                                # noqa: BLE001
            pass
        # CAP THE BITRATE AGAINST THE SOURCE.
        # The plugin does this and I had omitted it: a CQ encode of an already
        # efficient source balloons, because NVENC needs more bits than x264 for
        # the same CQ. The source is the quality ceiling anyway - bits above
        # ~1.5x add nothing. Without this, "shrink" jobs came out 30-50% LARGER.
        src_mbps = getattr(plan, "source_mbps", 0) or 0
        mf = float(tgt.get("maxrate_factor", C["maxrateFactor"]) or 0)
        mfl = float(tgt.get("maxrate_floor_mbps", C["maxrateFloorMbps"]) or 0)
        if mf > 0 and src_mbps > 0:
            cap = max(src_mbps * mf, mfl)
            a += ["-maxrate:v", f"{cap:.1f}M", "-bufsize", f"{cap * 2:.1f}M"]
        # NVENC-ONLY EXTRAS. -b_ref_mode, -spatial-aq and -rc-lookahead are
        # NVENC option names; libx265 rejects them outright and the encode
        # dies before the first frame. They only ever meant "tune NVENC", so
        # they only ever go to NVENC.
        extra = tgt.get("extra") or C["nvencExtra"].get(plan.target or "h264", "")
        if extra and _fam == "nvenc":
            a += shlex.split(extra)
    else:
        a += ["-map", "0:v:0", "-c:v", "copy"]
        # Remove the Dolby Vision RPU NAL so it direct-plays as HDR10. This is
        # noqa: the DV comment continues below
        # a bitstream filter on a COPIED stream - no re-encode, no quality loss.
        # Without it the plan claimed to strip DV while the command was a plain
        # copy, so an 85 GB file was rewritten to achieve nothing.
        if getattr(plan, "strip_dv", False):
            a += ["-bsf:v", f"filter_units=remove_types={C['dvRemoveNalTypes']}"]
            if C["dvRetagHvc1"]:
                a += ["-tag:v", "hvc1"]

    # Audio: honour the per-track decisions instead of blanket -c:a copy.
    ops = {o["idx"]: o for o in getattr(plan, "audio_ops", [])}
    lang_tags = getattr(plan, "audio_lang_tags", None) or {}
    for out_i, src_i in enumerate(plan.keep_audio):
        a += ["-map", f"0:a:{src_i}?"]
        # NAME THE LANGUAGE ON A TRACK THAT HAS NONE. Container metadata, so it
        # rides along with whatever this job was doing anyway - a stream copy
        # stays a stream copy. Keyed on the SOURCE index because that is what
        # the planner saw; the output index is whatever it lands at here.
        if src_i in lang_tags:
            a += [f"-metadata:s:a:{out_i}", f"language={lang_tags[src_i]}"]
        op = ops.get(src_i, {"to": "copy"})
        if op["to"] == "copy":
            a += [f"-c:a:{out_i}", "copy"]
        else:
            a += [f"-c:a:{out_i}", "eac3" if op["to"] == "eac3" else "aac",
                  f"-b:a:{out_i}", f"{op.get('br', 640)}k"]
            if op.get("ch"):
                # -ac:a:N, NOT -ac:N. This one character destroyed surround
                # audio, silently, on every re-encode that kept a 5.1 track.
                #
                # ffmpeg stream specifiers come in two forms: `a:N` means "the
                # Nth AUDIO stream", while a bare `N` means "output stream N",
                # counting the video. -c:a: and -b:a: above use the first form.
                # -ac used the second, so the indices were off by one stream:
                #
                #   -ac:0 6   -> output stream 0 = the VIDEO, silently ignored
                #   -ac:1 2   -> output stream 1 = the FIRST AUDIO track
                #
                # so the 5.1 track received the stereo setting intended for the
                # track after it, and the last track received nothing. Verified
                # on S01E09: planned "truehd 6ch -> E-AC3", ffprobe of the
                # committed file reported `eac3, 2, stereo` at 640 kbps - the
                # bitrate for 5.1 (correctly applied, because -b:a: has the
                # qualifier) spent on a downmixed stereo track, which is the
                # tell that the two option families disagreed.
                #
                # ffmpeg reports none of this as an error: an unused option on
                # a video stream is not a warning, and a downmix is a legal
                # request. The only visible trace was Sonarr renaming the file
                # from [TrueHD 5.1] to [EAC3 2.0] afterwards.
                a += [f"-ac:a:{out_i}", str(op["ch"])]

    out_sub = 0
    for i in plan.keep_subs:
        if i == plan.burn_index:
            continue                       # it is painted into the picture now
        if i in unreadable_subs(plan):
            continue                       # ffmpeg cannot read it; rescued later
        a += ["-map", f"0:s:{i}?"]
        # DISPOSITION FLAGS. Also planned but never emitted before.
        # A default/forced IMAGE sub makes Plex burn it on the CPU at playback,
        # which is the transcode we are trying to avoid - so clear those. And
        # when no English audio survives, the chosen dialogue sub has to be
        # flagged default or Plex shows nothing over the Japanese track.
        if i in getattr(plan, "clear_flags", []):
            a += [f"-disposition:s:{out_sub}", "0"]
        elif i == getattr(plan, "default_sub", None):
            a += [f"-disposition:s:{out_sub}", "default"]
        elif i == getattr(plan, "forced_sub", None):
            # forced+default together: 'forced' is what the track IS, 'default'
            # is what makes Plex show it without being asked. Either alone
            # leaves signs and foreign lines waiting for someone to go and
            # select them, which defeats the point of a forced track.
            a += [f"-disposition:s:{out_sub}", "forced+default"]
        else:
            # EVERY kept subtitle gets its flags stated EXPLICITLY, not merely
            # copied. The Jellyfin ffmpeg build was caught inventing default=1
            # on the first subtitle of a remux regardless of -default_mode
            # (verified: source subs carried no default, output did) - which
            # planted the exact "sub auto-shows over English audio" violation
            # on the pipeline's own output, and a second full pass then ran
            # only to clear it. Asserting the source's flags per stream is the
            # one thing the muxer cannot override. Plans stored before
            # sub_disps existed fall through to the old copy behaviour.
            disps = getattr(plan, "sub_disps", None) or []
            if i < len(disps) and disps[i]:
                a += [f"-disposition:s:{out_sub}", disps[i]]
        out_sub += 1

    # SUBTITLE CODEC: copy, EXCEPT the ones Matroska cannot hold.
    #
    # mov_text is an MP4-only format. Copying it into MKV fails outright with
    # "Subtitle codec 94213 is not supported", and because that kills the muxer
    # the whole filter graph collapses - which surfaced as a completely
    # unrelated-looking AUDIO error:
    #
    #   [af#0:1] Error sending frames to consumers: Function not implemented
    #   Task finished with error code: -40
    #
    # so the job read as an audio-conversion fault when the audio was fine.
    # Every mp4 source with embedded soft subs hits this; the plan for these is
    # "remux to mkv", so it is a guaranteed failure for that whole class.
    #
    # Both formats are plain timed text, so srt carries the same content. Only
    # the text formats are converted - image subs (PGS, VobSub) copy fine into
    # Matroska and must NOT be sent through a text encoder.
    _MP4_ONLY_TEXT = {"mov_text", "text", "tx3g"}
    codecs = getattr(plan, "sub_codecs", None) or []
    kept = [i for i in plan.keep_subs
            if i != plan.burn_index and i not in unreadable_subs(plan)]
    if not kept:
        a += ["-c:s", "copy"]
    else:
        for out_pos, i in enumerate(kept):
            name = codecs[i] if i < len(codecs) else ""
            # srt for the MP4-only text formats, copy for everything else.
            # Image subs (PGS, VobSub) go into Matroska untouched and must NOT
            # be handed to a text encoder.
            a += [f"-c:s:{out_pos}", "srt" if name in _MP4_ONLY_TEXT else "copy"]

    # Cap runtime so a runaway far-future frame cannot produce the 1193-hour
    # duration corruption seen on PGS-burn re-encodes.
    if plan.encode and C["trimToRealDuration"] and duration:
        a += ["-t", str(round(duration + C["trimBufferSec"], 3))]

    a += ["-map_metadata", "0", "-map_chapters", "0", dst]
    return a


def _io_by_disk(workers: list["Worker"]) -> list[dict]:
    """Group live throughput by pool disk, busiest first."""
    agg: dict[str, dict] = {}
    for w in workers:
        d = w.disk or "?"
        e = agg.setdefault(d, {"disk": d, "jobs": 0, "read_bps": 0.0,
                               "write_bps": 0.0})
        e["jobs"] += 1
        e["read_bps"] += w.read_bps
        e["write_bps"] += w.write_bps
    out = []
    for e in agg.values():
        e["total_bps"] = round(e["read_bps"] + e["write_bps"])
        e["read_bps"] = round(e["read_bps"])
        e["write_bps"] = round(e["write_bps"])
        out.append(e)
    return sorted(out, key=lambda x: -x["total_bps"])


def live_snapshot() -> dict:
    """Just the moving parts, cheap enough to poll several times a second.

    snapshot() carries the Finished list - sixty rows, each with its plan
    unpacked into an actions array - plus the queue preview. None of that
    changes between two frames of a progress bar, and dragging it along was
    what kept the worker cards on a two-second heartbeat: the bar stepped
    rather than moved, and a job that finished mid-interval sat at a stale
    percentage until the next poll.

    This returns the running workers and the few counters drawn beside them.
    One COUNT against an indexed column is the whole database cost.
    """
    workers = list(RUNNING.values())
    for w in workers:
        w.sample_io()
    with cursor() as cur:
        depth = cur.execute(
            "SELECT COUNT(*) n FROM jobs WHERE state='queued'").fetchone()["n"]
    return {
        "running": [w.as_dict() for w in workers],
        "queued": depth,
        "overall": _overall(depth, workers),
        "io": {
            "read_bps": round(sum(w.read_bps for w in workers)),
            "write_bps": round(sum(w.write_bps for w in workers)),
            "total_bps": round(sum(w.read_bps + w.write_bps for w in workers)),
            "by_disk": _io_by_disk(workers),
        },
        "disk_waits": _live_disk_waits(workers),
        "io_throttled": bool(IO_THROTTLED.get("on")),
        "io_reason": IO_THROTTLED.get("reason") or "",
        "io_low_jobs": [w.job.id for w in workers
                        if getattr(w, "io_low", False)],
        "plex_disks": sorted(_plex_disks_safe()),
        # Who is on each of those disks and what they are pulling, so the
        # panel can say how heavily a spindle is spoken for rather than just
        # that it is. See gate.plex_disk_detail().
        "plex_disk_detail": _plex_disk_detail_safe(),
        # PHYSICAL disk load, which is a different question from the "io" block
        # above. That one is nuarr's own per-job byte counters; this is what the
        # spindle itself is doing, including everything nuarr has no visibility
        # of - a viewer, a backup, a rebuild, another app. Carried on the 2 s
        # poll rather than its own, because the panel showing it repaints here.
        # A background ticker already keeps the samples fresh, so this only
        # reads them.
        "disk_load": _disk_load_safe(),
        "paused_reason": PAUSED_REASON,
        "capacity": {"encode": _capacity("encode"),
                     "passthrough": _capacity("passthrough"),
                     "subocr": _capacity("subocr")},
        "in_use": {"encode": _in_pool("encode"),
                   "passthrough": _in_pool("passthrough"),
                   "subocr": _in_pool("subocr")},
        "subocr_inline": sum(1 for w in workers if w.sub_ocr_active),
        # How the recently-finished jobs ended, so a ghost card can say what
        # actually happened instead of assuming success. See FATE.
        "fate": dict(FATE),
        "live": True,          # tells the painter there is no recent/queue here
    }


def snapshot(recent_limit: int = 60) -> dict:
    """Live queue state.

    `recent_limit` is a parameter because this is polled every 2 s and the
    finished list was fixed at 300 rows - 160 KB per poll, most of it a history
    that had not changed. The Finished panel asks for the long list on demand.
    """
    with cursor() as cur:
        upcoming = [dict(r) for r in cur.execute(
            "SELECT job_id,title,kind,pool FROM jobs WHERE state='queued' "
            "ORDER BY priority, created_at LIMIT 25")]
        depth = cur.execute(
            "SELECT COUNT(*) n FROM jobs WHERE state='queued'").fetchone()["n"]
        # 'deferred' belongs in this list: the encode is over and the swap is
        # queued, but excluding the state made the job VANISH from the feed
        # for however long the commit waited - finished work with no row
        # anywhere. And the files join carries the CURRENT size, because a
        # skipped or cancelled job deliberately records no before/after
        # (nothing was transcoded) yet the file still has a size worth
        # showing instead of a dash.
        recent_done = [dict(r) for r in cur.execute(
            "SELECT j.job_id,j.title,j.state,j.size_before,j.size_after,"
            "j.error,j.finished_at,j.path,j.kind,j.pool,j.plan_json,"
            "j.result_json,f.size AS file_size FROM jobs j "
            "LEFT JOIN files f ON f.id=j.file_id "
            "WHERE j.state IN ('done','failed','skipped','cancelled',"
            "'blocked','deferred') "
            "ORDER BY j.finished_at DESC LIMIT ?", (recent_limit,))]
    for r in recent_done:
        # unpack the plan so the UI can explain WHY, not just what
        try:
            p = json.loads(r.pop("plan_json") or "{}")
            r["summary"] = rules.plan_from_dict(p).summary() if p else ""
            r["actions"] = p.get("actions") or []
        except Exception:
            r["summary"], r["actions"] = "", []
        # Plan-less jobs (subocr) carry their outcome in result_json instead.
        if not r["summary"]:
            try:
                r["summary"] = (json.loads(r.get("result_json") or "{}")
                                .get("summary") or "")
            except Exception:
                pass
        r.pop("result_json", None)
    workers = list(RUNNING.values())
    for w in workers:                  # refresh per-process read counters
        w.sample_io()
    _batch_reset_if_idle(depth, len(workers))
    return {
        "running": [w.as_dict() for w in workers],
        "queued": depth,
        "queue": upcoming,
        "recent": recent_done,
        "overall": _overall(depth, workers),
        # Pool-wide disk load. The per-job rates only make sense against the
        # total: four copies at 12 MB/s each is a saturated pool, not four slow
        # jobs, and that distinction decides whether adding workers helps.
        "io": {
            "read_bps": round(sum(w.read_bps for w in workers)),
            "write_bps": round(sum(w.write_bps for w in workers)),
            "total_bps": round(sum(w.read_bps + w.write_bps for w in workers)),
            # PER DISK. Two jobs at 8 MB/s each on the same spindle is a
            # contended disk; the same two on different spindles is a pool
            # nowhere near its limit. The totals cannot distinguish them.
            "by_disk": _io_by_disk(workers),
        },
        # Spindles where a queued job is deliberately held back. Built live so
        # the percentage matches the worker card for the job it names.
        "disk_waits": _live_disk_waits(workers),
        # Whether encoders are currently yielding the disk to a viewer, and
        # which spindles a viewer is on. Both belong here rather than only in
        # the gate: the gate answers "may jobs start", this answers "why is
        # this running job slower than usual", which is a different question
        # asked from a different panel.
        "io_throttled": bool(IO_THROTTLED.get("on")),
        "io_reason": IO_THROTTLED.get("reason") or "",
        "io_low_jobs": [w.job.id for w in RUNNING.values()
                        if getattr(w, "io_low", False)],
        "plex_disks": sorted(_plex_disks_safe()),
        # Who is on each of those disks and what they are pulling, so the
        # panel can say how heavily a spindle is spoken for rather than just
        # that it is. See gate.plex_disk_detail().
        "plex_disk_detail": _plex_disk_detail_safe(),
        # ALSO HERE, not just in live_snapshot(). The fast poll only runs while
        # there is something to watch; an idle dashboard is driven entirely by
        # this one, which is exactly when the disks are most worth showing -
        # nothing of nuarr's is running, so anything the panel reports is
        # somebody else's load. Sending it from only one of the two left the
        # column reading "idle" on twelve genuinely busy disks.
        "disk_load": _disk_load_safe(),
        "paused_reason": PAUSED_REASON,
        "capacity": {"encode": _capacity("encode"),
                     "passthrough": _capacity("passthrough"),
                     "subocr": _capacity("subocr")},
        "in_use": {"encode": _in_pool("encode"),
                   "passthrough": _in_pool("passthrough"),
                   "subocr": _in_pool("subocr")},
        # How much of the subocr figure above is running INSIDE a transcode
        # rather than as a job of its own. Same budget, different home, and the
        # header says so instead of leaving you to wonder why the count moves
        # with no subocr card on screen.
        "subocr_inline": sum(1 for w in RUNNING.values() if w.sub_ocr_active),
    }
