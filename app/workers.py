"""
nuarr - runtime worker configuration

Tdarr lets you raise and lower worker counts while it runs; that is genuinely
useful, because the right number depends on what else the box is doing. This is
the same idea, with the limits written down instead of guessed.

Values live in the kv table, so a change survives a restart and takes effect on
the next job dispatch - no restart, no config file edit.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import SETTINGS
from .db import kv_get, kv_set

# Hard ceilings, with the reasoning attached.
LIMITS = {
    # The A5000 has ONE NVENC engine. Measured saturation on this box is about
    # 4 concurrent 1080p encodes; past that, throughput stops improving and
    # per-job latency climbs. 8 is allowed for 720p/x264 work, but it is not a
    # free win and the UI says so.
    "encode_workers": (0, 8, 4),
    # Remux/passthrough never touches NVENC - it is pure disk I/O on the pool,
    # so the ceiling is spindle contention, not the GPU.
    "passthrough_workers": (0, 12, 4),
    # Subtitle OCR. Its own pool because the two halves pull opposite ways: the
    # OCR is single-threaded CPU with no disk at all, the mux and commit are
    # pure pool I/O. Sharing the handler cap of 2 left nine of ten cores idle
    # on a box measured at 3% load. 4 is a starting point, not a ceiling -
    # spindle contention on the commit is the real limit, and disk_wait_pct
    # already guards that.
    "subocr_workers": (0, 10, 4),
    # ffprobe is cheap but still a pool read each.
    "probe_workers": (0, 16, 4),
    # Health/rename checks against the arrs.
    "arr_concurrency": (1, 30, 20),

    # ---- file settling ----
    # MINUTES a file must sit untouched before it becomes eligible - the
    # "Held (new) - still settling" tile. It exists so we never burn GPU time on
    # a file Sonarr is still importing, upgrading or renaming.
    #
    # Was 1800 (30 h, inherited from Tdarr). 5 minutes is long enough for a copy
    # to finish and the file to stop changing, and the risk of catching a later
    # rename is now much lower: the commit re-resolves the path through the arr,
    # so a rename mid-encode retargets instead of writing to a stale path.
    "hold_minutes": (0, 10080, 5),

    # MINUTES between automatic library scans. Nothing becomes eligible without
    # a scan - mark_eligible() only runs as part of one - so with no schedule a
    # settled file waits until someone presses Rescan. 0 disables auto-scanning.
    "scan_every_min": (0, 10080, 180),

    # HOURS between ffmpeg update checks. One HTTP GET of a version string, so
    # it is nearly free - but there is no point asking often either, since
    # releases are weeks apart. 0 turns the check off.
    "ffmpeg_check_h": (0, 720, 24),

    # SECONDS between server-control status polls while nothing is pending.
    # This was a hardcoded 5 s, which asked "is a stop pending?" twelve times a
    # minute to hear "no" - the answer only changes when you press a button or
    # the last job finishes. While a stop IS pending it switches to a 3 s tick
    # regardless of this, because then the countdown actually matters.
    "control_poll_s": (5, 600, 60),

    # PERCENT complete a disk-heavy job must reach before a SECOND disk-heavy
    # job is allowed to start on the same spindle.
    #
    # Measured on this pool: four stream copies sharing NU-DRIVE-1 managed
    # 63 MB/s between them; spread across four spindles the same work ran at
    # 997 MB/s. That is seek thrashing, not a bandwidth ceiling - so when the
    # only work left is on a busy disk it is faster to WAIT than to pile on.
    # Waiting until the incumbent is nearly finished keeps the disk saturated
    # without interleaving two streams across it.
    # 0 disables the hold entirely (old behaviour: start immediately).
    "disk_wait_pct": (0, 100, 85),

    # ---- gate hold timings (seconds unless noted) ----
    # How long to keep holding AFTER the last Plex stream stops. Without a grace
    # period the queue restarts the instant someone pauses or an episode ends,
    # so a viewer flicking between episodes gets a GPU-loaded server every time.
    "hold_grace_s": (0, 1800, 120),
    # PERCENTAGE POINTS a throttled Plex transcode must already be ahead of the
    # viewer before nuarr stops holding the queue for it. Plex transcodes ahead
    # and then parks with the encoder idle; that idle time is free GPU, but only
    # if there is enough buffer that it will not wake up mid-encode. 15 points
    # is roughly several minutes of playback on a typical episode.
    "throttle_lead_pct": (0, 90, 15),
    # How often the gate re-polls Plex/DrivePool while held. Longer means less
    # API chatter but a slower resume once the coast is clear.
    "gate_recheck_s": (1, 300, 3),
    # How long a gate probe result is reused. Measured: Tautulli takes ~2.4 s to
    # answer get_activity on this box. At the old 5 s TTL the cache expired
    # between polls, so almost every dispatch tick and every dashboard refresh
    # paid that cost. 20 s is far shorter than the 120 s grace period, so it
    # cannot meaningfully delay a hold.
    "gate_cache_s": (1, 120, 20),

    # PERCENT busy a physical disk must be, sustained, before nuarr steers new
    # work away from it. Storage-agnostic: it measures the disks rather than
    # asking one product whether it is doing one thing, so it covers a
    # DrivePool balance, a SnapRAID sync, a Storage Spaces rebuild, a backup
    # or another app hammering the same spindles. Load nuarr is causing itself
    # is subtracted first. 0 turns the check off.
    "disk_busy_pct": (0, 100, 85),

    # PERCENT busy a viewer's own spindle may already be before nuarr refuses
    # to share it.
    #
    # A viewer's disk used to be an outright veto - nothing started there at
    # all. That is correct when the disk is the bottleneck and wasteful when it
    # is not, and measured here it usually is not: one direct play pulls about
    # 1.1 MB/s off a disk that does 150+, leaving the spindle at 1% busy and
    # eleven others carrying the entire queue. Refusing to touch it is not
    # caution, it is idle hardware.
    #
    # So the veto becomes a threshold. Below this, work may run on a viewer's
    # disk - always at Very Low I/O priority, so the viewer's reads overtake
    # it - and above it, the old behaviour applies. 25 is deliberately well
    # under the point where seeks start to bite: the number that matters is
    # not what the disk can do flat out, it is how much head room is left
    # before a read has to queue behind something.
    #
    # 0 restores the veto exactly.
    "viewer_share_pct": (0, 100, 25),

    # SECONDS of buffer a viewer must have before nuarr will merely throttle
    # rather than stop. Below this, work touching that spindle is paused
    # outright - commits stop writing and a running encode is suspended - and
    # resumes within half a second of the buffer recovering.
    #
    # A quarter of a 150 MB/s commit is still 37 MB/s of competing writes, and
    # that is fine against a viewer holding four minutes and useless against
    # one holding twenty seconds. Throttling treats both the same; this does
    # not. 0 turns the pause off and leaves the old throttle-only behaviour.
    "viewer_pause_lead_s": (0, 600, 60),

    # ---- background sweeps (previously hardcoded module constants) ----
    # SECONDS between commit-queue retries: finished encodes whose file swap
    # hit a lock (usually Plex playing the file) wait here and retry.
    "commit_retry_s": (30, 1800, 120),
    # SECONDS between rename-queue sweeps (blocked arr renames re-checked).
    "rename_poll_s": (5, 600, 20),
    # SECONDS between autoqueue passes looking for eligible work to enqueue.
    "autoqueue_poll_s": (5, 600, 20),
    # SECONDS between missing-file healer sweeps.
    "missing_poll_s": (30, 3600, 120),
    # HOURS between rule-check audit runs.
    "audit_every_h": (1, 168, 24),
}

HINTS = {
    "encode_workers": "NVENC-bound. One engine on the A5000; ~4 saturates it at 1080p.",
    "passthrough_workers": "Remux/copy only, no GPU. Limited by pool disk I/O.",
    "subocr_workers": ("Subtitle OCR. CPU-bound and single-threaded per file, "
                       "so this scales with cores, not the GPU. The commit "
                       "half is disk I/O and is separately capped by "
                       "disk_wait_pct."),
    "probe_workers": "ffprobe scans. Cheap CPU, one pool read each.",
    "arr_concurrency": "Parallel Sonarr/Radarr API calls during scans.",
    "hold_minutes": "MINUTES a file must sit untouched before it can be "
                    "processed — the 'Held (new)' tile. Lower it to start "
                    "sooner; too low and you may transcode a file Sonarr is "
                    "still importing or renaming.",
    "scan_every_min": "MINUTES between automatic library scans. Files only "
                      "become eligible during a scan. 0 turns auto-scan off.",
    "ffmpeg_check_h": "HOURS between ffmpeg update checks. Reports only — it "
                      "never installs on its own. 0 disables the check.",
    "control_poll_s": "SECONDS between restart/shutdown status checks when "
                      "idle. Drops to 3s automatically while a stop is pending.",
    "disk_wait_pct": "How far a disk-heavy job must get before another starts "
                     "on the SAME pool disk. Stops two stream copies thrashing "
                     "one spindle. 0 = start immediately.",
    "hold_grace_s": "Keep holding this long after the last Plex stream stops. "
                    "0 resumes immediately.",
    "throttle_lead_pct": "PERCENTAGE POINTS a throttled Plex transcode must be "
                         "ahead of the viewer before it stops holding encodes. "
                         "Throttled means Plex has buffered ahead and parked "
                         "the encoder, so the GPU is free. Lower reclaims more "
                         "idle time; higher is more cautious. 0 = ignore any "
                         "throttled session.",
    "gate_recheck_s": "SECONDS between re-checks of Plex and the disks while "
                      "jobs are held. Shorter means the queue resumes sooner "
                      "after the coast clears; longer means less API chatter "
                      "while someone is watching.",
    "gate_cache_s": "SECONDS a gate probe result is reused before asking Plex "
                    "or the disks again. Saves repeated API calls when several "
                    "things check the gate in the same moment; far shorter "
                    "than the grace period, so it cannot delay a hold.",
    "disk_busy_pct": "PERCENT busy a physical disk must be, sustained, before "
                     "new work is steered away from it. It measures the disks "
                     "rather than asking one product what it is doing, so it "
                     "covers a pool balance, a SnapRAID sync, a Storage Spaces "
                     "rebuild, a backup or another app entirely. Load nuarr is "
                     "causing itself is subtracted first. 0 turns it off.",
    "viewer_pause_lead_s": "SECONDS of buffer a viewer must have before nuarr "
                           "will merely slow down rather than STOP. Below this, "
                           "commits onto that spindle pause and any encode "
                           "reading it is suspended, resuming within half a "
                           "second of the buffer recovering. Throttling treats "
                           "a viewer with four minutes banked the same as one "
                           "with twenty seconds; this does not. 0 disables the "
                           "pause and leaves throttling alone.",
    "viewer_share_pct": "PERCENT busy a viewer's own spindle may already be "
                        "before nuarr refuses to share it. One direct play "
                        "pulls about 1.1 MB/s off a disk that does 150+, so an "
                        "outright veto left eleven disks carrying the queue "
                        "and one sitting idle. Work below this threshold runs "
                        "at Very Low I/O priority so the viewer's reads "
                        "overtake it. 0 restores the old veto exactly.",
    "commit_retry_s": "SECONDS between commit-queue retries. A finished encode "
                      "whose file swap hit a lock — usually Plex playing it — "
                      "waits here and tries again.",
    "rename_poll_s": "SECONDS between rename-queue sweeps, re-checking arr "
                     "renames that were blocked.",
    "autoqueue_poll_s": "SECONDS between auto-queue passes looking for "
                        "eligible work to enqueue.",
    "missing_poll_s": "SECONDS between missing-file healer sweeps.",
    "audit_every_h": "HOURS between rule-check audit runs.",
}

# NOT SHOWN ON THE SETTINGS PAGE, but still live.
#
# hold_minutes was the whole settling rule: "has nothing written to this file
# for N minutes", a proxy for "is anybody using it". mark_eligible() now
# answers that question directly - the file must be openable exclusively, and
# have stayed that way for LOCK_QUIET_S - so the minutes are just a cheap SQL
# pre-filter in front of the real test. Leaving a knob on the page implied it
# still decided something, and turning it up would only delay a check that is
# already correct. The value stays in LIMITS so the dataclass, the default and
# every reader keep working.
HIDDEN_KEYS = ("hold_minutes",)

# Which tab each setting belongs to in the UI.
TIMING_KEYS = ("hold_minutes", "scan_every_min", "ffmpeg_check_h",
               "control_poll_s", "disk_wait_pct", "hold_grace_s",
               "throttle_lead_pct",
               "gate_recheck_s", "gate_cache_s",
               "disk_busy_pct", "viewer_share_pct", "viewer_pause_lead_s",
               "commit_retry_s", "rename_poll_s", "autoqueue_poll_s",
               "missing_poll_s", "audit_every_h")


@dataclass
class WorkerConfig:
    encode_workers: int
    passthrough_workers: int
    subocr_workers: int
    probe_workers: int
    arr_concurrency: int
    hold_minutes: int
    scan_every_min: int
    ffmpeg_check_h: int
    control_poll_s: int
    disk_wait_pct: int
    hold_grace_s: int
    throttle_lead_pct: int
    gate_recheck_s: int
    gate_cache_s: int
    disk_busy_pct: int
    viewer_share_pct: int
    viewer_pause_lead_s: int
    commit_retry_s: int
    rename_poll_s: int
    autoqueue_poll_s: int
    missing_poll_s: int
    audit_every_h: int

    def as_dict(self) -> dict:
        return {
            k: {
                "value": getattr(self, k),
                "min": LIMITS[k][0],
                "max": LIMITS[k][1],
                "default": LIMITS[k][2],
                "hint": HINTS[k],
                "timing": k in TIMING_KEYS,
            }
            for k in LIMITS if k not in HIDDEN_KEYS
        }


def _default(key: str) -> int:
    return int(getattr(SETTINGS, key, LIMITS[key][2]))


def get() -> WorkerConfig:
    vals = {}
    for key in LIMITS:
        raw = kv_get(f"worker.{key}")
        try:
            vals[key] = int(raw) if raw is not None else _default(key)
        except (TypeError, ValueError):
            vals[key] = _default(key)
    return WorkerConfig(**vals)


def set_one(key: str, value: int) -> tuple[bool, str, int]:
    """Clamp and persist one worker count. Returns (changed, message, applied)."""
    if key not in LIMITS:
        return False, f"unknown setting '{key}'", 0
    lo, hi, _ = LIMITS[key]
    try:
        v = int(value)
    except (TypeError, ValueError):
        return False, "value must be a whole number", 0

    applied = max(lo, min(hi, v))
    kv_set(f"worker.{key}", str(applied))

    if applied != v:
        return True, f"{key} clamped to {applied} (allowed {lo}-{hi})", applied
    note = ""
    if key == "encode_workers" and applied > 4:
        note = " - above 4 the single NVENC engine is the bottleneck, not a speedup"
    elif key == "encode_workers" and applied == 0:
        note = " - encoding paused"
    elif key == "hold_minutes":
        # Say what the change actually does to the backlog, since the number is
        # in minutes but people think in hours.
        note = (f" ({applied/60:.1f} h) - files older than this become eligible "
                f"on the next scan")
        if applied == 0:
            note = " - no settling period; a file can be picked up mid-import"
    elif key == "scan_every_min":
        note = (f" ({applied/60:.1f} h)" if applied >= 60 else "")
        if applied == 0:
            note = " - auto-scan OFF; nothing becomes eligible until you rescan"
    elif key == "hold_grace_s" and applied == 0:
        note = " - jobs resume the moment a stream stops"
    elif key == "gate_recheck_s" and applied > 60:
        note = f" - jobs may sit idle up to {applied}s after Plex frees up"
    return True, f"{key} set to {applied}{note}", applied


def tune(key: str) -> float:
    """One timing value, read live so an edit applies without a restart.

    The background loops (commit queue, rename queue, autoqueue, healer,
    audit) read this each time round instead of a module constant - which is
    the entire point of putting these on the settings page.
    """
    try:
        return float(getattr(get(), key))
    except Exception:
        return float(LIMITS[key][2])


def reset() -> dict:
    for key in LIMITS:
        kv_set(f"worker.{key}", str(_default(key)))
    return get().as_dict()
