r"""What this machine is, and how many of each job it should be asked to run.

WHY THIS EXISTS. Every worker count in workers.LIMITS was a number measured
on ONE box - Erik's, twenty threads and an A5000 - and written down as the
default for everybody. That is the right number here and a guess anywhere
else: the same table tells a four-core NUC to run three decode checks at four
threads each and a 64-core server to run three, which is half the machine
asleep. Erik asked for "dynamic per cpu/gpu spec so the system is optimized
off the bat by default", and this is where that lives.

HOW A NUMBER IS ARRIVED AT. Two facts multiplied:

  what the box has        threads, memory, the card and its NVENC engines,
                          how many pool spindles the library sits on
  what one job costs      cores per job, MEASURED, in COST_CORES below

A pool that runs out of processor gets `budget / cores each`. A pool that
runs out of the card gets what the card saturates at. A pool that runs out of
spindles gets a share of the spindles. Nothing here is a vibe: every figure
in COST_CORES was taken by running that pool's real command and reading the
child's own kernel+user time out of Windows, and the ones that were wrong by
a factor of four when they were guesses (subread) are exactly why the table
is measured rather than reasoned.

THE BUDGET IS NOT THE WHOLE BOX. 80% of the threads, because nuarr is a guest
on a machine that also runs Plex, and a box driven to 100% by a subtitle
reader is a box that stutters during an episode. Erik's twenty threads become
a budget of sixteen.
"""
from __future__ import annotations

import threading
import time

# ---------------------------------------------------------------- the box --
_SPEC: dict = {"at": 0.0, "data": None}
_LOCK = threading.Lock()
TTL_S = 300.0

# WHAT ONE JOB OF EACH POOL COSTS THE PROCESSOR, in cores, measured on this
# box with idle priority and one job at a time (September 2026):
#
#   subread     9.1  24 frames through six ffmpeg processes, 1080p 10-bit
#               0.5  a track read instead - one stream out of the container
#   decode      3.2  five windows, four threads each
#   passthrough 1.1  while it converts a track to AAC; 0.2 copying only
#   subocr      1.0  the demux and the remux; the read itself is on the card
#   listen      0.5  five 30s windows pulled to PCM; Whisper is on the card
#   encode      0.5  feeding NVENC and collecting from it; NVDEC reads the
#                    source on the card, so the frames never come down
#   subs        0.3  mkvmerge, bound by the disk
#   probe       0.1  one ffprobe header read
#   audio       0.05 mkvpropedit, a seek and a few hundred bytes
#
# The pump uses the same table to decide what may start (jobs.cpu_has_room),
# so a number changed here changes both what is recommended and what is let
# through, and the two can never drift apart.
COST_CORES = {
    "encode": 0.5, "passthrough": 1.1, "subocr": 1.0, "subs": 0.3,
    "audio": 0.05, "decode": 3.2, "listen": 0.5, "subread": 9.1,
    "probe": 0.1,
}
# A subread is two different jobs wearing one name. The pump asks for the
# shape it is about to run; the recommender sizes for the expensive one.
COST_CORES_SUBREAD_TRACK = 0.5
# AND THE EXPENSIVE ONE IS PRICED BY THE LANE, because the lane count is no
# longer six everywhere (hardsub.lanes). Measured: 9.1 cores over 6 lanes,
# 5.1 over 3, 3.5 over 2 - so a lane is about 1.6 cores and the overhead of
# crowding shows up as the sixth lane costing more than the second.
LANE_CORES = 1.6

BUDGET_SHARE = 0.80                # of the threads, leaving Plex room
# AND NO ONE POOL TAKES MORE THAN THIS MUCH OF IT. The global budget in the
# pump stops the SUM from exceeding the box, but a ceiling per pool is what
# stops the cheapest, most numerous work - decode checks, subtitle reads -
# from filling that budget and leaving an encode queued behind it. Six
# tenths reproduces, on the box these figures came from, the three decode
# checks that were arrived at by hand and by measurement.
MAX_POOL_SHARE = 0.60


def _gpu_bits() -> dict:
    """Name and NVENC engine count, without spawning anything of our own."""
    out = {"name": "", "nvenc": 0, "vram_gb": 0.0}
    try:
        from . import system
        g = system._gpu() or {}
        out["name"] = str(g.get("name") or "")
        v = g.get("vram_total_mb") or 0
        out["vram_gb"] = round(float(v) / 1024.0, 1) if v else 0.0
    except Exception:                                        # noqa: BLE001
        pass
    if out["name"]:
        # ONE ENGINE UNLESS WE ARE TOLD OTHERWISE, and the driver does not
        # tell us: nvidia-smi reports sessions in flight, never how many NVENC
        # blocks the silicon has. A first version guessed from the card's name
        # and promptly said two for the A5000, which has one - and would have
        # doubled this box's encode count on the strength of a guess. So: one,
        # which is what almost every card in a media box has, and the figure
        # beside it (four 1080p streams an engine) is measured here rather
        # than assumed. A card with more can be turned up by hand; the page
        # says what the number was chosen from.
        out["nvenc"] = 1
    return out


def _spindles() -> int:
    """How many disks the library lives on. ZERO means "not known yet".

    NOT ONE, WHICH IS WHAT IT USED TO SAY, and the difference cost a set of
    settings. The disk report is empty for the first minute or so after a
    restart - nothing has walked the pool yet - and a floor of one turned
    "I have not looked" into "this machine has a single disk". Every
    spindle-derived recommendation came out at its minimum, the migration
    below saw values still sitting on the typed defaults, and it wrote those
    minima down: six repacks became one, six OCR jobs became two, on a box
    with twelve disks. An unknown quantity has to be able to say so.
    """
    # THE POOL'S SHAPE, NOT THE POOL'S LOAD. The first version counted rows
    # in gate.disk_report(), which is built from sustained throughput samples
    # - so on a quiet box, or in the first minute after a restart, it is
    # empty, and an empty answer is indistinguishable from a one-disk
    # machine. media_roots() is the structural answer: which disks the
    # library is actually spread over, known as soon as the config is read
    # and true whether or not anything is moving.
    try:
        from . import scanner
        n = len(scanner.media_roots() or {})
        if n:
            return n
    except Exception:                                        # noqa: BLE001
        pass
    try:
        from . import gate
        return len((gate.disk_report() or {}).get("disks") or [])
    except Exception:                                        # noqa: BLE001
        return 0


def spec(refresh: bool = False) -> dict:
    """Threads, memory, card, spindles. Cached for five minutes."""
    now = time.time()
    with _LOCK:
        if not refresh and _SPEC["data"] and now - _SPEC["at"] < TTL_S:
            return _SPEC["data"]
    import psutil
    threads = int(psutil.cpu_count(logical=True) or 1)
    cores = int(psutil.cpu_count(logical=False) or threads)
    ram_gb = round(psutil.virtual_memory().total / 2 ** 30, 1)
    g = _gpu_bits()
    sp = _spindles()
    d = {"threads": threads, "cores": cores, "ram_gb": ram_gb,
         "gpu": g["name"], "nvenc": g["nvenc"], "vram_gb": g["vram_gb"],
         "spindles": sp,
         # HAVE WE ACTUALLY SEEN THE MACHINE? The processor and the card can
         # be read the instant the process starts; the pool cannot. Anything
         # that would write a number down waits for this to be true.
         "settled": sp > 0,
         "budget": max(1.0, round(threads * BUDGET_SHARE, 1)),
         "at": now}
    with _LOCK:
        # An unsettled answer is held for seconds, not minutes - the disks
        # normally appear within the first minute and the page should not
        # spend five of them describing a box nuarr could not see.
        _SPEC.update(at=(now if d["settled"] else now - TTL_S + 15.0), data=d)
    return d


def budget() -> float:
    """Cores nuarr may have in flight at once. The pump's ceiling."""
    try:
        return float(spec()["budget"])
    except Exception:                                        # noqa: BLE001
        return 4.0


# -------------------------------------------------------- the numbers ------
def _clamp(v, lo, hi):
    # A TENTH OF A THOUSANDTH OF SLACK, because 3.2 * 3 is 9.600000000000001
    # in binary and "how many 3.2s fit in 9.6" answered two. Every number on
    # this page comes out of a division like that one.
    return max(lo, min(hi, int(float(v) + 1e-6)))


def _disks(n: int) -> str:
    return f"{n} pool disk" + ("s" if n != 1 else "")


# WHAT SHARE OF THE BUDGET THE FRAME SAMPLER MAY HAVE. Its own number rather
# than MAX_POOL_SHARE because this one is split between the reads in flight
# instead of multiplied by them: two reads at three lanes each is the same
# six ffmpeg processes the old constant ran for ONE read, which is how a
# subtitle measurement came to be the heaviest thing on the box.
SUBREAD_SHARE = 0.35
LANES_MIN, LANES_MAX = 2, 6


def lanes(running: int | None = None) -> int:
    """How many frames to grab at once, for this box and this moment.

    WAS 6, FOR EVERYONE, FOREVER. Six lanes at two threads each measured nine
    cores on a twenty-thread box - and with four reads allowed at once that
    was thirty-six cores asked of twenty. The share is fixed and the reads in
    flight divide it, so a second read makes both narrower instead of making
    the box twice as oversubscribed. Floor of two because one lane at a time
    turns a sixteen-second read into a minute for no saving worth having
    (measured: two lanes cost 3.5 cores and 32s, six cost 9.1 and 16s).
    """
    if running is None:
        try:
            from . import jobs
            running = sum(1 for w in list(jobs.RUNNING.values())
                          if getattr(w, "pool", "") == "subread"
                          and _sampling(getattr(w, "stage", "")))
        except Exception:                                    # noqa: BLE001
            running = 1
    share = budget() * SUBREAD_SHARE
    return _clamp(share / max(1, int(running or 1)) / LANE_CORES,
                  LANES_MIN, LANES_MAX)


def _sampling(stage: str) -> bool:
    """Is this worker in the half of a subread that grabs frames?"""
    st = (stage or "").lower()
    return "sampl" in st or "frame" in st or "floor" in st


def subread_cores(picture: bool = True, running: int | None = None) -> float:
    """What one subtitle read is about to cost the processor."""
    if not picture:
        return COST_CORES_SUBREAD_TRACK
    return round(lanes(running) * LANE_CORES, 2)


def recommend() -> dict:
    """{setting: (count, why)} for this box. Every count is from the spec.

    EMPTY UNTIL THE POOL HAS BEEN SEEN. Half of these are divisions by the
    spindle count, and a recommendation made before the disks are known is
    not a smaller recommendation - it is a wrong one. Callers fall back to
    the typed defaults, which is what they used before any of this existed.
    """
    s = spec()
    if not s.get("settled"):
        return {}
    t, b, sp = s["threads"], s["budget"], s["spindles"]
    out: dict = {}

    # THE CARD, NOT THE BOX. One NVENC engine saturates at about four 1080p
    # encodes; the processor barely appears (0.5 a job), so threads are not
    # what runs out here.
    out["encode_workers"] = (
        _clamp(4 * max(1, s["nvenc"]), 1, 8),
        f"{s['nvenc'] or 1} encode engine on {s['gpu'] or 'this card'}; "
        f"about four 1080p streams each, measured")

    # SPINDLES. A repack reads a whole file and writes a whole one, and the
    # budget keeps one of those per disk - so half the disks, which leaves the
    # other half free for everything else and for whoever is watching.
    out["passthrough_workers"] = (
        _clamp(sp / 2, 1, 12),
        _disks(sp) + ", one whole-file rewrite per disk, half of them at once")

    # SPINDLES AGAIN, and the same shape of job.
    out["subs_workers"] = (
        _clamp(sp / 3, 1, 6),
        _disks(sp) + "; mkvmerge is a whole file in and a whole file out")

    # The demux and the remux are disk; the read is on the card and capped
    # separately below, so this one is sized to keep the disks busy while the
    # card works through somebody else's read.
    out["subocr_workers"] = (
        _clamp(max(2, sp / 2), 1, 10),
        _disks(sp) + "; the unpacking and repacking are disk work either side "
                     "of the card")

    out["subocr_gpu_lanes"] = (
        2 if s["gpu"] else 1,
        "measured on this card: two reads reach its floor, more only queue"
        if s["gpu"] else "no card found, so the OCR is on the processor")

    # THE PROCESSOR, for the two that actually run out of it. Each gets at
    # most six tenths of the budget, so neither can fill it alone.
    share = b * MAX_POOL_SHARE
    for key, cost in (("decode_workers", COST_CORES["decode"]),
                      ("subread_workers", subread_cores(True, 1))):
        n = _clamp(share / cost, 1, 6)
        out[key] = (n, f"{cost:g} cores a job, measured, against this pool's "
                       f"share of the budget ({round(share, 1):g} of {b:g} "
                       f"cores; {t} threads less a fifth for Plex)")

    # ONE MODEL, SHARED. A second listen does not load a second model - it
    # shares the first and each runs at half speed - so a bigger card buys
    # nothing here and the honest recommendation is one whatever the spec.
    out["listen_workers"] = (
        1, (f"one Whisper model on {s['vram_gb']:g} GB of card memory, shared "
            f"- a second job halves both rather than adding anything"
            if s["gpu"] else "no card, so Whisper runs on the processor"))

    # A HEADER WRITE AND A HEADER READ. Neither is in anyone's way; the only
    # question is how many seeks into the pool at once.
    out["audio_workers"] = (_clamp(sp / 2, 1, 6),
                            "one header write each, " + _disks(sp)
                            + " to spread over")
    out["probe_workers"] = (_clamp(sp, 2, 16),
                            "one seek a file, " + _disks(sp)
                            + " to spread over")
    out["arr_concurrency"] = (20, "the arrs' patience, not this machine's")
    return out


def recommend_one(key: str) -> tuple[int, str]:
    try:
        return recommend().get(key) or (0, "")
    except Exception:                                        # noqa: BLE001
        return (0, "")


def sentence() -> str:
    """One line for the top of the Concurrency page."""
    s = spec()
    bits = [f"{s['threads']} threads", f"{s['ram_gb']:g} GB"]
    if s["gpu"]:
        bits.append(f"{s['gpu']} ({s['nvenc']} encode engine"
                    + ("s" if s["nvenc"] > 1 else "") + ")")
    bits.append(f"{s['spindles']} pool disk"
                + ("s" if s["spindles"] != 1 else ""))
    return ", ".join(bits)
