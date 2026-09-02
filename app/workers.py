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
    # HOW MANY MAY BE ON THE CARD, as against how many files are in flight.
    # Only the OCR pass touches the GPU; the demux and the mux either side of
    # it are pool I/O. Capping the whole job at what the card can take left the
    # extra workers queued behind it and the disk idle through every read.
    # Turn subocr_workers up to keep the disk busy and leave this near what the
    # GPU actually saturates at - measured, two lanes reaches the floor.
    "subocr_gpu_lanes": (1, 6, 2),
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

def _encode_hint() -> str:
    """Named for the machine it is running on, not the one it was written on.

    This string shipped saying "the A5000" to every install, because that is
    the dev box's card - on a laptop with an RTX 3060, or a CPU-only server,
    it read as someone else's notes. Asked from the encoder probe at serve
    time (cached there), so it names whatever is actually doing the work.
    """
    try:
        from . import encoders
        fam = encoders.resolve("auto")[0]
        dev = encoders.devices()
        if fam == "nvenc":
            gpu = dev.get("gpu_name") or "the NVIDIA GPU"
            return (f"NVENC-bound. One encode engine on {gpu}; "
                    f"~4 saturates it at 1080p.")
        if fam == "cpu":
            cpu = dev.get("cpu_name") or "the CPU"
            return (f"CPU-bound - encoding runs on {cpu}. Every worker "
                    f"uses several cores; raise this cautiously.")
        label = (encoders.probe().get(fam) or {}).get("label", fam)
        return f"Bound by {label} - one hardware encode engine, ~4 at 1080p."
    except Exception:                                    # noqa: BLE001
        return "Bound by the hardware encoder; ~4 saturates one engine at 1080p."


def _subocr_hint() -> str:
    """What limits subtitle OCR depends on which engine is reading.

    Tesseract is CPU-bound and single-threaded per file, so more workers use
    more cores. PaddleOCR on a GPU is bound by the card instead, and running
    several at once mostly queues them behind each other - so the advice
    inverts, and the hint has to say which world this install is in.
    """
    try:
        from . import subocr
        libs = {l.name for l in (SETTINGS.libraries or [])}
        engines = {subocr.engine(n) for n in libs} or {"tesseract"}
        if engines == {"paddle"}:
            # CACHED ONLY. paddle_info() starts a Python and imports several
            # hundred megabytes of native code to answer honestly - fine on
            # the OCR page, wrong here: this hint is built on every /api/
            # workers poll, so a cold cache meant spawning that process every
            # few seconds behind the dashboard.
            cached = subocr._PADDLE_CACHE.get("data") or {}
            dev = "GPU" if cached.get("cuda") else "CPU"
            if dev == "GPU":
                # This used to end "more mostly queue behind each other",
                # which was true and is no longer: only the OCR pass takes a
                # GPU lane, so extra workers now demux and mux while the card
                # is busy instead of waiting on it. Turning this up is useful
                # again - subocr_gpu_lanes is what limits the card itself.
                return ("Subtitle OCR. Each file is demuxed off the pool, read "
                        "on the GPU, then muxed back - and only the middle "
                        "part touches the card. This is how many files are in "
                        "flight, so raise it to keep the disk busy; "
                        "subocr_gpu_lanes caps how many may be on the GPU at "
                        "once. The commit half is disk I/O, capped by "
                        "disk_wait_pct.")
            return ("Subtitle OCR, reading with PaddleOCR on the CPU. "
                    "Heavier per cue than Tesseract - scales with cores, but "
                    "each file costs far more. The commit half is disk I/O, "
                    "capped by disk_wait_pct.")
        if "paddle" in engines:
            return ("Subtitle OCR. Mixed engines across libraries - Tesseract "
                    "scales with cores, PaddleOCR is bound by whatever it runs "
                    "on. The commit half is disk I/O, capped by disk_wait_pct.")
    except Exception:                                    # noqa: BLE001
        pass
    return ("Subtitle OCR, reading with Tesseract. CPU-bound and "
            "single-threaded per file, so this scales with cores, not the "
            "GPU. The commit half is disk I/O and is separately capped by "
            "disk_wait_pct.")


# A PLAIN NAME AND A PLAIN SENTENCE. These read as variable names with the
# underscores taken out - "ffmpeg check h", "disk wait pct", "hold grace s" -
# which tells you nothing unless you already know what the setting does, and
# abbreviates the unit into a letter on top of that. LABELS gives each one a
# name a person would say out loud, with the unit spelled out; the hints below
# lead with the plain answer and keep the detail after it.
LABELS = {
    "encode_workers": "Encodes at once",
    "passthrough_workers": "Remuxes at once",
    "subocr_workers": "Subtitle reads at once",
    "subocr_gpu_lanes": "Subtitle reads on the GPU at once",
    "probe_workers": "File scans at once",
    "arr_concurrency": "Sonarr/Radarr calls at once",
    "hold_minutes": "Settle time (minutes)",
    "scan_every_min": "Scan the library every (minutes)",
    "ffmpeg_check_h": "Check for an ffmpeg update every (hours)",
    "control_poll_s": "Check for restart or shutdown every (seconds)",
    "disk_wait_pct": "Wait before a second job on the same disk (percent done)",
    "hold_grace_s": "Keep waiting after Plex stops (seconds)",
    "throttle_lead_pct": "How far ahead a paused Plex transcode must be (percent)",
    "gate_recheck_s": "Re-check while work is held (seconds)",
    "gate_cache_s": "Reuse the last check for (seconds)",
    "disk_busy_pct": "Steer work away from a disk busier than (percent)",
    "viewer_pause_lead_s": "Pause when a viewer's buffer drops below (seconds)",
    "viewer_share_pct": "Share a viewer's disk until it is busier than (percent)",
    "commit_retry_s": "Retry a blocked file swap every (seconds)",
    "rename_poll_s": "Retry blocked renames every (seconds)",
    "autoqueue_poll_s": "Look for new work every (seconds)",
    "missing_poll_s": "Look for missing files every (seconds)",
    "audit_every_h": "Run the rule check every (hours)",
}

HINTS = {
    "encode_workers": "",   # built live by _encode_hint() - see as_dict()
    "passthrough_workers": "How many files may be repacked at once. No GPU "
                           "involved - the limit is how fast the pool disks "
                           "can read and write.",
    "subocr_workers": "",   # engine-dependent; see _subocr_hint()
    "subocr_gpu_lanes":
        "How many subtitle reads may use the graphics card at the same moment. "
        "The unpacking and repacking either side of the read are disk work and "
        "are not counted here, so workers above this number still help - they "
        "read and write while the card is busy. Measured on this box, two "
        "lanes already reach the card's floor; more only queues.",
    "probe_workers": "How many files may be inspected at once. Cheap on the "
                     "processor, one disk read each.",
    "arr_concurrency": "How many questions Nuarr may ask Sonarr and Radarr at "
                       "the same time during a scan.",
    "hold_minutes": "How long a file must sit untouched before Nuarr will "
                    "touch it - the 'Held (new)' tile. Lower it to start "
                    "sooner; too low and you may convert a file Sonarr is "
                    "still importing or renaming.",
    "scan_every_min": "How often Nuarr looks through the library for new "
                      "files. Nothing becomes eligible for work except during "
                      "a scan. 0 turns automatic scanning off.",
    "ffmpeg_check_h": "How often Nuarr checks whether a newer ffmpeg exists. "
                      "It only reports - it never installs on its own. "
                      "0 disables the check.",
    "control_poll_s": "How often Nuarr checks whether you have asked it to "
                      "restart or stop, while it is idle. Speeds up to every "
                      "3 seconds on its own once a stop is pending.",
    "disk_wait_pct": "How far one disk-heavy job must get before a second may "
                     "start on the SAME pool disk. Stops two big copies "
                     "fighting over one drive. 0 starts them together.",
    "hold_grace_s": "How long Nuarr keeps waiting after the last Plex stream "
                    "ends, in case someone is between episodes. 0 resumes "
                    "immediately.",
    "throttle_lead_pct": "How far ahead of the viewer a paused Plex conversion "
                         "must be before Nuarr stops waiting for it. Plex "
                         "buffers ahead and then parks its encoder, which "
                         "frees the graphics card. Lower reclaims more idle "
                         "time; higher is more cautious. 0 ignores these "
                         "sessions entirely.",
    "gate_recheck_s": "How often Nuarr re-checks Plex and the disks while work "
                      "is held. Shorter resumes the queue sooner once the "
                      "coast is clear; longer means less chatter at Plex while "
                      "someone is watching.",
    "gate_cache_s": "How long one check of Plex and the disks is reused before "
                    "asking again. Saves repeat questions when several things "
                    "check at the same moment. Far shorter than the waiting "
                    "period, so it cannot delay a hold.",
    "disk_busy_pct": "How busy a physical disk must be, and stay, before Nuarr "
                     "steers new work away from it. It watches the disks "
                     "themselves rather than asking any one program, so it "
                     "covers a pool balance, a SnapRAID sync, a Storage Spaces "
                     "rebuild, a backup, or another app entirely. Load Nuarr "
                     "is causing itself is subtracted first. 0 turns it off.",
    "viewer_pause_lead_s": "How little buffer a viewer must have left before "
                           "Nuarr stops rather than merely slows down. Below "
                           "this, writes to that disk pause and any conversion "
                           "reading it is suspended, resuming within half a "
                           "second of the buffer recovering. Slowing down "
                           "treats a viewer with four minutes banked the same "
                           "as one with twenty seconds; this does not. "
                           "0 disables the pause and leaves slowing alone.",
    "viewer_share_pct": "How busy a viewer's own disk may already be before "
                        "Nuarr refuses to share it at all. One direct play "
                        "pulls about 1.1 MB/s off a disk that can do 150+, so "
                        "refusing outright left eleven disks carrying the "
                        "queue and one sitting idle. Work under this threshold "
                        "runs at the lowest disk priority, so the viewer's "
                        "reads always overtake it. 0 restores the old refusal.",
    "commit_retry_s": "How often Nuarr retries putting a finished file back "
                      "when the swap was blocked - usually because Plex is "
                      "playing it.",
    "rename_poll_s": "How often Nuarr retries renames that Sonarr or Radarr "
                     "refused earlier.",
    "autoqueue_poll_s": "How often Nuarr looks for eligible files to queue.",
    "missing_poll_s": "How often Nuarr looks for files that have gone missing "
                      "and tries to put them right.",
    "audit_every_h": "How often the rule check re-reads the library to confirm "
                     "it still matches its own rules.",
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
# audit_every_h joined it when the rule check moved to a fixed ten-minute
# cycle with a busy check in front of it. The value stays in LIMITS so the
# dataclass and every reader keep working; what changed is that nothing reads
# it any more, and a knob that decides nothing is worse than no knob.
HIDDEN_KEYS = ("hold_minutes", "audit_every_h")

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
    subocr_gpu_lanes: int
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
                "hint": (_encode_hint() if k == "encode_workers"
                         else _subocr_hint() if k == "subocr_workers"
                         else HINTS[k]),
                "label": LABELS.get(k, k.replace("_", " ")),
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
