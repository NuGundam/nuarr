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
    # Six, not four: 10,774 of them in a fortnight at a median of 7.4 seconds
    # and 0.57 GB, each one holding a single spindle for that time. With
    # twelve disks and the budget keeping one rewrite per spindle, six in
    # flight is six disks working and six idle - the CPU share (the audio
    # conversions on the way through) is small beside the I/O.
    "passthrough_workers": (0, 12, 6),
    # Subtitle OCR. Its own pool because the two halves pull opposite ways: the
    # OCR is single-threaded CPU with no disk at all, the mux and commit are
    # pure pool I/O. Sharing the handler cap of 2 left nine of ten cores idle
    # on a box measured at 3% load. 4 is a starting point, not a ceiling -
    # spindle contention on the commit is the real limit, and disk_wait_pct
    # already guards that.
    # Six. The card is capped separately (subocr_gpu_lanes), and the point of
    # a higher number here is that the two disk halves - the demux in and the
    # remux out - run while the card is busy with somebody else's read. The
    # longest job nuarr has after an encode: 57 s median over 1,333 of them.
    "subocr_workers": (0, 10, 6),
    # HOW MANY MAY BE ON THE CARD, as against how many files are in flight.
    # Only the OCR pass touches the GPU; the demux and the mux either side of
    # it are pool I/O. Capping the whole job at what the card can take left the
    # extra workers queued behind it and the disk idle through every read.
    # Turn subocr_workers up to keep the disk busy and leave this near what the
    # GPU actually saturates at - measured, two lanes reaches the floor.
    "subocr_gpu_lanes": (1, 6, 2),
    # THE THREE KINDS THAT JOINED THE QUEUE, EACH WITH ITS OWN POOL, each with
    # a knob here rather than a constant in jobs.py - because a pool whose
    # width cannot be seen or changed from this page is a pool that will be
    # asked about from this page. Erik asked for exactly that.
    #
    # Subtitle instructions: mostly instant (a title, a recycle) but the ones
    # that are not are a full container copy, the same spindle profile as a
    # remux. Two was what the background runner used before this moved, on a
    # box that then had one heavy job per spindle whatever the pool. With the
    # spindle budget (jobs.SPINDLE_WEIGHT) each of these still gets a disk to
    # itself and they simply land on different ones: twelve spindles, 5,412
    # jobs in a fortnight at a median of six seconds each, so four in flight
    # is four disks busy for six seconds, not four jobs fighting over one.
    "subs_workers": (0, 6, 4),
    # Audio tags: mkvpropedit writing a header, well under a second each -
    # measured at a 3.2 s median including everything around it, and rated
    # the cheapest work nuarr does. The only limit that matters is "not too
    # many seeks into the pool at once", and six of those is nothing.
    "audio_workers": (0, 6, 6),
    # Does it decode: a 4-thread software decode of 45 seconds of video and a
    # sequential read off one pool disk. Two of these at once measured 51-68%
    # CPU on a 20-thread box - THE PROCESSOR is what this one runs out of, not
    # the disk, and it is the only pool of which that is true. Three fits
    # (about twelve of twenty threads, leaving the encoder's feeder and the
    # commit copies their share); four does not. The busiest pool by far -
    # 15,211 jobs in a fortnight at four seconds each.
    "decode_workers": (0, 6, 3),
    # Whisper. One loaded model on one card; a second job shares the same
    # model and the same VRAM, which works but halves each. One is the honest
    # default; two is for a box with the card to spare.
    "listen_workers": (0, 3, 1),
    # The picture sampler and the track reader. Each is a whole-file read off
    # a spindle - frames from across the file, or a track out of the container.
    # Weight 2 under the spindle budget, so two of them may share a disk and
    # four across twelve disks is comfortable. Median 6.3 s.
    "subread_workers": (0, 6, 4),
    # ffprobe is cheap but still a pool read each - one seek per file, rated
    # disk 2 / cpu 1. Eight keeps a library walk moving without making the
    # pool sound like a scan.
    "probe_workers": (0, 16, 8),
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
    # A VIEWER WHO IS BUFFERING STOPS EVERYTHING. Not one pool, not one
    # spindle: every running job is frozen and no pool may start work,
    # for at least this long after the last stall and until the viewer's
    # buffer is back over its floor and has stayed there. The per-disk
    # yield is the everyday rule; this is the one for the moment it has
    # already failed. 0 turns it off.
    "buffer_hold_s": (0, 600, 60),
    # A VIEWER WHO HAS JUST PRESSED PLAY STOPS EVERYTHING, BRIEFLY. Nuarr
    # cannot know which spindle a new stream is about to read until Plex
    # names the file, and the player has no buffer at all in its first
    # seconds - the one moment a competing 150 MB/s read anywhere on the
    # pool is guaranteed to be felt. So every pool holds and every running
    # job is frozen until the disk is known and the viewer is over their
    # floor; then the per-disk rules take over and the rest of the pool
    # carries on. This is the most that wait may last. 0 turns it off.
    "new_viewer_hold_s": (0, 120, 30),
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
    # TWO DIFFERENT READS, TWO DIFFERENT NAMES. subocr turns pictures into
    # words; subread looks at what each track IS. Both were called "Subtitle
    # reads at once" - the second with "(kinds)" in brackets, which is not a
    # distinction anybody can hold - and the dashboard now shows these names
    # rather than its own short forms, so the collision was on two pages.
    "subocr_workers": "Subtitle OCR at once",
    "subocr_gpu_lanes": "Subtitle OCR on the GPU at once",
    "subs_workers": "Subtitle fixes at once",
    "audio_workers": "Audio tag fixes at once",
    "decode_workers": "Decode checks at once",
    "listen_workers": "Audio listens at once",
    "subread_workers": "Subtitle track reads at once",
    "probe_workers": "File scans at once",
    "arr_concurrency": "Sonarr/Radarr calls at once",
    "hold_minutes": "Settle time (minutes)",
    "scan_every_min": "Scan the library every (minutes)",
    "ffmpeg_check_h": "Check for an ffmpeg update every (hours)",
    "control_poll_s": "Check for restart or shutdown every (seconds)",
    "disk_wait_pct": "Wait before a second job on the same disk (percent done)",
    "hold_grace_s": "Keep waiting after Plex stops (seconds)",
    "buffer_hold_s": "Stop everything when a viewer buffers (seconds)",
    "new_viewer_hold_s": "Stop everything when a viewer starts (seconds, at most)",
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
                           "involved - there is no audio encoder on the card - "
                           "so the limit is how fast the pool disks can read "
                           "and write. A repack that also converts a track to "
                           "AAC spends about one core for the length of the "
                           "read; one that only copies spends nothing.",
    "subocr_workers": "",   # engine-dependent; see _subocr_hint()
    "subocr_gpu_lanes":
        "How many subtitle reads may use the graphics card at the same moment. "
        "The unpacking and repacking either side of the read are disk work and "
        "are not counted here, so workers above this number still help - they "
        "read and write while the card is busy. Measured on this box, two "
        "lanes already reach the card's floor; more only queues.",
    "subs_workers": "How many files may have their subtitles settled at once - "
                    "a sidecar taken in, a duplicate track removed, a title "
                    "corrected. Most are instant; the ones that rebuild the "
                    "container read and write a whole file, so this is disk "
                    "work like a remux and never two on one disk.",
    "audio_workers": "How many files may have a language tag corrected at "
                     "once. Each is one header write with mkvpropedit, a "
                     "fraction of a second - nothing is decoded or copied. "
                     "The limit is only how many seeks into the pool at once.",
    "decode_workers": "How many files may be checked for corruption at once. "
                      "Each decodes five windows - 20s at the head, three 15s "
                      "samples down the middle, 25s at the tail - on the CPU "
                      "with four threads, measured at three cores for ten "
                      "seconds, and reads off one pool disk, never two on the "
                      "same disk.",
    "listen_workers": "How many files may be listened to at once. Each runs "
                      "five 30-second windows per track through Whisper's "
                      "language identifier on the graphics card. One model "
                      "is loaded; a second job shares it and each runs at "
                      "half speed, so one is the honest default.",
    "subread_workers": "How many files may have their subtitles read at once "
                       "- frames sampled for burned-in words, or a track's "
                       "events read to judge its title. The frame sampler is "
                       "the most processor-hungry job here: six ffmpeg "
                       "processes pulling 24 frames, measured at nine cores "
                       "for sixteen seconds on a 1080p episode. Each is also "
                       "a whole-file read off one pool disk, never two on the "
                       "same disk.",
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
    "buffer_hold_s": "When any viewer's player reports buffering, every "
                     "running job is frozen and no pool starts anything - "
                     "on every disk, not just theirs - for at least this "
                     "long after the last stall, and until their buffer is "
                     "back over its floor and has stayed there for 20 s. "
                     "0 turns it off and leaves only the per-disk yield.",
    "new_viewer_hold_s": "When someone presses play - a new stream, or the "
                         "next episode - every running job is frozen and no "
                         "pool starts anything until Nuarr knows which disk "
                         "they are reading and their buffer is over its "
                         "floor. Then only that disk stays yielded and the "
                         "rest of the pool carries on. This is the longest "
                         "the wait may last if the disk or the buffer cannot "
                         "be read. 0 turns it off.",
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

# WHICH POOL EACH KNOB WIDENS, so the settings row can wear the same coloured
# bubble the queue and the cards use for that pool - and a knob with no pool
# (probes, arr calls) wears none rather than a made-up one.
POOL_OF = {"encode_workers": "encode", "passthrough_workers": "passthrough",
           "subocr_workers": "subocr", "subocr_gpu_lanes": "subocr",
           "subs_workers": "subs", "audio_workers": "audio",
           "decode_workers": "decode", "listen_workers": "listen",
           "subread_workers": "subread"}

# Which tab each setting belongs to in the UI.
TIMING_KEYS = ("hold_minutes", "scan_every_min", "ffmpeg_check_h",
               "control_poll_s", "disk_wait_pct", "hold_grace_s",
               "buffer_hold_s", "new_viewer_hold_s",
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
    subs_workers: int
    audio_workers: int
    decode_workers: int
    listen_workers: int
    subread_workers: int
    probe_workers: int
    arr_concurrency: int
    hold_minutes: int
    scan_every_min: int
    ffmpeg_check_h: int
    control_poll_s: int
    disk_wait_pct: int
    hold_grace_s: int
    buffer_hold_s: int
    new_viewer_hold_s: int
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
        off = paused()
        return {
            k: {
                "value": getattr(self, k),
                "paused": POOL_OF.get(k, "") in off,
                "min": LIMITS[k][0],
                "max": LIMITS[k][1],
                # THE EFFECTIVE DEFAULT, not the one in the table. Three of
                # these keys also exist as config fields, and a config field
                # wins - so quoting LIMITS here told you "default 6" beside a
                # Reset button that would have set 4.
                "default": _default(k),
                "hint": (_encode_hint() if k == "encode_workers"
                         else _subocr_hint() if k == "subocr_workers"
                         else HINTS[k]),
                "label": LABELS.get(k, k.replace("_", " ")),
                "pool": POOL_OF.get(k, ""),
                "timing": k in TIMING_KEYS,
                # WHICH OF THE FOUR THINGS THIS ONE EATS. See COST.
                "cost": cost_of(k),
                # AND WHERE THE DEFAULT CAME FROM. "default 4" beside a Reset
                # button is a number with no argument behind it; this is the
                # argument - what about this box produced it - so a value you
                # moved can be compared with the one nuarr would choose.
                "sized": _sized(k),
            }
            for k in LIMITS if k not in HIDDEN_KEYS
        }


# A SWITCH PER POOL, beside the dial. Turning a pool's count to 0 pauses
# it, but that throws the number away - and "what was it set to before I
# stopped it" is exactly the question asked at 3 a.m. when a pool is
# flooding the server and needs to be stopped NOW and put back later.
# The switch keeps the count and takes the pool's capacity to zero for as
# long as it is off: running jobs finish, nothing new is claimed. Persisted
# like the counts, so a restart does not silently restart a pool somebody
# stopped on purpose.
# WHAT EACH WORKER ACTUALLY COSTS THE BOX.
#
# Every row on this page is "how many of these at once", and the honest way
# to answer that is to know which of the four things a worker eats: the
# processor, the card, memory, or the disks. They are not interchangeable -
# four encodes and four remuxes are the same number and completely different
# machines afterwards, because one is bound by a single NVENC engine and the
# other by twelve spindles. Nothing here is guessed: the ratings come from
# what the tool provably does (see what_runs() in jobs.py, which says the
# same thing per stage) and the figures beside them are medians over the
# jobs table.
#
# 0 nothing · 1 light · 2 moderate · 3 heavy. `lead` is the one that runs
# out first - the number this row is really limited by.
COST = {
    "encode_workers": {
        "cpu": 1, "gpu": 3, "ram": 1, "disk": 2, "lead": "gpu",
        "why": "NVENC does the encoding on its own engine - the card has one, "
               "which is why this is the number that matters. NVDEC reads the "
               "source on the same card and the frames never come down for "
               "39,759 of the 39,859 files that can be decoded there, so the "
               "processor only feeds and collects; a burn-in adds filter work "
               "on the SMs. The disk half is the commit at the end."},
    "passthrough_workers": {
        "cpu": 2, "gpu": 0, "ram": 1, "disk": 3, "lead": "disk",
        "why": "Reads a whole file and writes a whole file - the card is "
               "never involved, because there is no audio encoder on it. "
               "Copying the streams alone is nothing (2.4 cpu-seconds for a "
               "1.4 GB episode); the processor share is whatever audio the "
               "plan converts on the way through (TrueHD down to 5.1, EAC3 "
               "to AAC), and that is about one core for as long as the read "
               "takes - 95 cpu-seconds on that same episode through "
               "libfdk_aac, against 139 through ffmpeg's own AAC, which is "
               "why libfdk is the one that runs."},
    "subocr_workers": {
        "cpu": 2, "gpu": 2, "ram": 2, "disk": 3, "lead": "disk",
        "why": "Three parts with different appetites: demux the picture "
               "subtitles (disk), read them (the engine - CUDA on Paddle, "
               "the processor on Tesseract), then rebuild the container "
               "(disk again, a whole file each way). The longest job nuarr "
               "runs after an encode."},
    "subocr_gpu_lanes": {
        "cpu": 0, "gpu": 3, "ram": 2, "disk": 0, "lead": "gpu",
        "why": "Only the read itself - the card and the model it holds in "
               "video memory. The unpacking and repacking either side are "
               "disk work and are counted by the row above."},
    "subs_workers": {
        "cpu": 1, "gpu": 0, "ram": 1, "disk": 3, "lead": "disk",
        "why": "mkvmerge rebuilds the container: a whole file read and a "
               "whole file written, with almost nothing for the processor to "
               "do in between. Two of these on one spindle is the thing to "
               "avoid, not two on the box."},
    "audio_workers": {
        "cpu": 0, "gpu": 0, "ram": 0, "disk": 1, "lead": "disk",
        "why": "mkvpropedit rewrites the header in place - a seek and a few "
               "hundred bytes. Nothing is decoded, nothing is copied. The "
               "cheapest work nuarr does."},
    "decode_workers": {
        "cpu": 3, "gpu": 0, "ram": 1, "disk": 2, "lead": "cpu",
        "why": "ffmpeg decodes five windows - the first 20s, three 15s "
               "samples spaced down the middle, the last 25s, 90 seconds in "
               "all - on four threads, on the processor, deliberately: the "
               "point is to find out whether the bytes decode at all, and a "
               "hardware decoder is built to play through the damage this "
               "looks for. Measured at 31 cpu-seconds in 10, so about three "
               "cores per job while it runs."},
    "listen_workers": {
        "cpu": 1, "gpu": 2, "ram": 2, "disk": 1, "lead": "gpu",
        "why": "Whisper's language identifier over five thirty-second "
               "windows per track - eleven when those five disagree and it "
               "takes a second look. One model is loaded and shared, so the "
               "second job shares the card rather than doubling the memory; "
               "only a few minutes of audio is ever read off the disk, and "
               "pulling those five windows out is the whole processor share "
               "- one cpu-second a track, measured."},
    "subread_workers": {
        "cpu": 3, "gpu": 1, "ram": 1, "disk": 2, "lead": "cpu",
        "why": "THE HEAVIEST THING NUARR ASKS OF THE PROCESSOR, measured. A "
               "picture read grabs 24 frames through six ffmpeg processes at "
               "two threads each: on a 1080p 10-bit episode that is 145 "
               "cpu-seconds in 16 wall seconds - nine cores while it runs, "
               "where a decode check is three. The OCR is the small half and "
               "it is on the card; the frames are the big half and they are "
               "not (NVDEC loses on one frame per process - see hardsub). A "
               "track read instead walks one subtitle stream end to end, "
               "which is disk."},
    "probe_workers": {
        "cpu": 1, "gpu": 0, "ram": 0, "disk": 2, "lead": "disk",
        "why": "ffprobe reads a header. The cost is one seek per file across "
               "a spun-down pool disk, not the parsing."},
    "arr_concurrency": {
        "cpu": 0, "gpu": 0, "ram": 0, "disk": 0, "lead": "",
        "why": "Questions over the network to Sonarr and Radarr. The limit is "
               "their patience, not this machine's."},
}

# WHICH POOL EACH ROW'S JOBS ARE FILED UNDER, so the medians below can be
# looked up. Not POOL_OF: that maps to the pause switch and is empty for the
# rows that have no pool of their own.
_COST_POOL = {"encode_workers": "encode", "passthrough_workers": "passthrough",
              "subocr_workers": "subocr", "subocr_gpu_lanes": "subocr",
              "subs_workers": "subs", "audio_workers": "audio",
              "decode_workers": "decode", "listen_workers": "listen",
              "subread_workers": "subread"}

_MED: dict = {"at": 0.0, "data": {}}


def medians() -> dict:
    """Per pool, from the last fortnight of finished jobs: how many, how long
    the middle one took, and how big the middle file was.

    A rating says which resource; this says how much of it, and it is the
    figure that settles an argument - subtitle OCR reads for 57 seconds a
    file and an audio tag fix is done in three.
    """
    import time as _t
    if _MED["data"] and _t.time() - _MED["at"] < 600:
        return _MED["data"]
    out: dict = {}
    try:
        import statistics
        from .db import cursor as _cur
        since = _t.time() - 14 * 86400
        with _cur() as cur:
            rows = cur.execute(
                "SELECT COALESCE(pool,kind) p, started_at, finished_at, "
                "       size_before FROM jobs "
                " WHERE state='done' AND COALESCE(finished_at,0) > ?",
                (since,)).fetchall()
        agg: dict = {}
        for r in rows:
            st, fi = r["started_at"] or 0, r["finished_at"] or 0
            d = agg.setdefault(r["p"], {"n": 0, "secs": [], "gb": []})
            d["n"] += 1
            if st and fi and fi > st:
                d["secs"].append(fi - st)
            if r["size_before"]:
                d["gb"].append(r["size_before"] / 2 ** 30)
        for k, d in agg.items():
            out[k] = {"jobs": d["n"],
                      "secs": round(statistics.median(d["secs"]), 1) if d["secs"] else 0,
                      "gb": round(statistics.median(d["gb"]), 2) if d["gb"] else 0}
    except Exception:                                        # noqa: BLE001
        out = {}
    _MED.update(at=_t.time(), data=out)
    return out


def cost_of(key: str) -> dict:
    """The profile for one row, with the engine it actually uses folded in."""
    c = COST.get(key)
    if not c:
        return {}
    c = dict(c)
    # TWO OF THESE CHANGE SHAPE WITH THEIR ENGINE, and saying "GPU" on a box
    # whose OCR is Tesseract would be a fact about somebody else's machine.
    try:
        if key in ("subocr_workers", "subocr_gpu_lanes"):
            from . import subocr
            if (subocr.engine() or "").lower() != "paddle":
                c["gpu"] = 0
                c["cpu"] = 3 if key == "subocr_workers" else 3
                c["lead"] = "cpu"
                c["why"] = c["why"].replace(
                    "the engine - CUDA on Paddle, the processor on Tesseract",
                    "Tesseract, on the processor")
        if key == "listen_workers":
            from . import audiolang
            # THE DEVICE THE MODEL IS ACTUALLY LOADED ON - not the one the box
            # could offer. faster-whisper installed for the CPU is a CPU job
            # however many cards are in the machine, and _MODEL_DEV is what
            # the loader ended up with. info()'s answer is the fallback
            # before the first load. (progress() does not carry a device -
            # reading it there quietly rated a CUDA listener as CPU work.)
            dev = getattr(audiolang, "_MODEL_DEV", "") or \
                (audiolang.info() or {}).get("device") or ""
            if str(dev) != "cuda":
                c["gpu"] = 0
                c["cpu"] = 3
                c["lead"] = "cpu"
                c["why"] = c["why"].replace(
                    "shares the card", "shares the model")
    except Exception:                                        # noqa: BLE001
        pass
    m = medians().get(_COST_POOL.get(key, ""), {})
    if m:
        c["measured"] = m
    return c


PAUSABLE = ("encode", "passthrough", "subocr", "subs", "audio", "decode",
            "listen", "subread")


def paused() -> set:
    raw = kv_get("worker.paused") or ""
    return {p for p in raw.split(",") if p in PAUSABLE}


def set_paused(pool: str, on: bool) -> tuple[bool, str]:
    if pool not in PAUSABLE:
        return False, f"unknown pool '{pool}'"
    cur = paused()
    if on:
        cur.add(pool)
    else:
        cur.discard(pool)
    kv_set("worker.paused", ",".join(sorted(cur)))
    return True, (f"{pool} paused - running jobs finish, nothing new starts"
                  if on else f"{pool} running again")


def set_paused_all(on: bool) -> tuple[bool, str]:
    """Every pool at once - the master switch on the Concurrency page.

    One write rather than eight round trips, so the page cannot be caught
    half-paused, and so the log says it as one decision.
    """
    kv_set("worker.paused", ",".join(sorted(PAUSABLE)) if on else "")
    return True, ("every pool paused - running jobs finish, nothing new starts"
                  if on else "every pool running again")


def _sized(key: str) -> dict:
    """{n, why} - what this box works out for this row, and from what."""
    try:
        from . import boxspec
        n, why = boxspec.recommend_one(key)
        if n:
            lo, hi, _d = LIMITS[key]
            return {"n": max(lo, min(hi, int(n))), "why": why}
    except Exception:                                        # noqa: BLE001
        pass
    return {}


def _default(key: str) -> int:
    """The number for THIS box, or the one typed for the box these figures
    were measured on if the machine cannot be read.

    THE TABLE ABOVE IS ONE MACHINE'S ANSWER. Every figure in LIMITS was taken
    on twenty threads with an A5000 and twelve spindles, and handing the same
    number to a four-core NUC tells it to run three decode checks at four
    threads each. boxspec asks what this box actually is and divides by what
    each job is measured to cost; on the box the constants came from it
    returns the constants, which is the check that it is not inventing
    anything.
    """
    try:
        from . import boxspec
        n, _why = boxspec.recommend_one(key)
        if n:
            lo, hi, _d = LIMITS[key]
            return max(lo, min(hi, int(n)))
    except Exception:                                        # noqa: BLE001
        pass
    return int(getattr(SETTINGS, key, LIMITS[key][2]))


def _typed_default(key: str) -> int:
    """What the table says, before the box is taken into account."""
    return int(getattr(SETTINGS, key, LIMITS[key][2]))


# THE OLD RECOMMENDATIONS, AND WHY THEY ARE WRITTEN DOWN.
#
# Raising a default does nothing for a box that has already saved the old one,
# and every one of these was saved simply by existing. So a value that is
# still sitting on the number nuarr used to recommend is moved to the number
# it recommends now - once, recorded in the log, and only where the two are
# both untouched by hand. A value somebody chose is never overwritten: if it
# does not match the old default exactly, it stays.
_WAS_DEFAULT = {"passthrough_workers": 4, "subocr_workers": 4,
                "subs_workers": 2, "audio_workers": 4, "decode_workers": 2,
                "subread_workers": 2, "probe_workers": 4}
# Bumped once after the first run: two of the seven did not move, because
# their recommendation lives in config.py as well and that copy still said
# the old number. Re-running is safe - a key already on its new value no
# longer matches `was` and is left alone.
_MIGRATED_KEY = "worker.defaults.2026-09b"
# AND THE SAME RULE AGAIN, now that the recommendation is the box's rather
# than the table's. A saved value that still equals what the table typed is a
# value nobody chose - it was written by existing - so it moves onto what this
# machine works out for itself. Anything else is somebody's decision and is
# left exactly where it is. Once, logged, and safe to re-run: a key already on
# its new number no longer matches the typed default.
_SIZED_KEY = "worker.defaults.boxsized.1"


def _adopt_new_defaults() -> None:
    """Move untouched counts onto the new recommendations. Runs once."""
    if kv_get(_MIGRATED_KEY):
        return
    moved = []
    for key, was in _WAS_DEFAULT.items():
        now = _default(key)
        if now == was:
            continue
        raw = kv_get(f"worker.{key}")
        try:
            cur = int(raw) if raw is not None else was
        except (TypeError, ValueError):
            continue
        if cur != was:
            continue                       # somebody chose this; leave it
        kv_set(f"worker.{key}", str(now))
        moved.append(f"{LABELS.get(key, key)} {was} -> {now}")
    kv_set(_MIGRATED_KEY, "1")
    if moved:
        try:
            from . import joblog
            joblog.log("worker counts moved to the new recommendations: "
                       + "; ".join(moved), "info")
        except Exception:                                # noqa: BLE001
            pass


def _adopt_box_sizes() -> None:
    """Untouched counts move onto what this box works out. Runs once.

    AND NOT UNTIL THE BOX HAS BEEN SEEN. The first version ran at startup,
    when the pool walk has not happened and the disk count is zero; it read
    the minimum recommendation that produced and wrote it down. Waiting costs
    nothing - get() is called on every page poll, so this runs within seconds
    of the disks appearing - and it is the difference between sizing a machine
    and sizing the silence before one answers.
    """
    if kv_get(_SIZED_KEY):
        return
    try:
        from . import boxspec
        if not boxspec.spec().get("settled"):
            return
    except Exception:                                    # noqa: BLE001
        return
    moved = []
    for key in LIMITS:
        try:
            typed = _typed_default(key)
            now = _default(key)
        except Exception:                                # noqa: BLE001
            continue
        if now == typed:
            continue
        raw = kv_get(f"worker.{key}")
        if raw is None:
            # NOTHING SAVED, NOTHING TO MOVE. get() already resolves an unset
            # key through _default(), which is the box's answer - writing it
            # down would only freeze today's answer against a machine that
            # gains a disk tomorrow.
            continue
        try:
            if int(raw) != typed:
                continue                   # somebody chose this; leave it
        except (TypeError, ValueError):
            continue
        kv_set(f"worker.{key}", str(now))
        moved.append(f"{LABELS.get(key, key)} {typed} -> {now}")
    kv_set(_SIZED_KEY, "1")
    if moved:
        try:
            from . import boxspec, joblog
            joblog.log("worker counts sized for this box (" + boxspec.sentence()
                       + "): " + "; ".join(moved), "info")
        except Exception:                                # noqa: BLE001
            pass


def get() -> WorkerConfig:
    _adopt_new_defaults()
    _adopt_box_sizes()
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
