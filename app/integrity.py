r"""nuarr - is the file actually readable, or does it only look readable?

WHY A SEPARATE CHECK
--------------------
Corruption was the one failure nuarr could only find by tripping over it. A
truncated episode sat in the library looking perfect - right size, right name,
ffprobe answers every question about it - until a job picked it up months later
and failed with "moov atom not found", or until somebody pressed play and it
stopped forty minutes in. refetch.py knew exactly what to do about that error
string; nothing ever went looking for the files that would produce it.

ffprobe is not enough and never was. It reads the header, and the header of a
truncated file is intact - truncation removes the END. So this decodes, which
is the only test that can distinguish "the container describes 24 minutes" from
"there are 24 minutes of frames in it".

WHY IT DOES NOT DECODE THE WHOLE FILE
-------------------------------------
39,000 files at real decode speed is a week of GPU that nobody asked for. Two
bounded windows catch what actually goes wrong here:

    the head   header and stream damage, bad indices, wrong codec parameters
    the middle three short samples spread through the gap, because head plus
               tail is 45 seconds of a 22-minute episode and damage anywhere
               in the other 97% used to decode cleanly at both ends
    the tail   truncation - the single most common way a file in a media
               library is broken, because it is what an interrupted download,
               a full disk or a killed remux all leave behind. It catches it
               by COUNTING FRAMES, not by reading ffmpeg's complaints: a
               truncated file seeks past its own end, decodes nothing and
               exits 0, and that used to be recorded as health

A file that decodes at both ends and has an intact header is not proof of a
clean middle, and this module does not claim it is: the verdict is stored as
what it is, a sampled one. It is the difference between finding most of the
broken files tonight and finding all of them never.

FAIL CLOSED, LOUDLY
-------------------
The verdict here feeds remedy.py, where "file/corrupt" is one of four findings
auto mode may act on by DELETING the file and asking the arr for another. So a
noisy decoder warning must never be able to reach that. ffmpeg is run at -v
error, and even then the message has to match _FATAL before this says corrupt.
Anything else is recorded verbatim under an "ok" verdict, where it is visible
to a person and harmless to the library.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from . import joblog
from .config import NO_WINDOW, SETTINGS
from .db import cursor

# How many seconds to decode at each end.
HEAD_S = 20
TAIL_S = 25
# AND THE MIDDLE, WHICH USED TO BE NOBODY'S JOB. Head plus tail is 45 seconds
# of a 22-minute episode - 3.4% of it - so a file damaged anywhere in the
# other 97% decoded cleanly at both ends and was recorded as healthy.
# Measured on a test file with 64 KB of noise written over its halfway mark:
# passed as ok before, caught as corrupt by the 0m30s window after.
#
# WHAT IT COSTS, measured on thirty files taken at random off this shelf:
# 3.7s a file before, 8.7s after - 2.4x, for 6.8% of the running time looked
# at instead of 3.4%. All thirty kept the verdict they had, which is the
# number that mattered: the new rules fire on the synthetic damage and on
# nothing else.
MID_S = 15
MID_WINDOWS = 3
# How many decoder threads one check may use. See _decode.
DECODE_THREADS = 4
# HOW LONG ONE DECODE MAY TAKE BEFORE IT IS GIVEN UP ON.
#
# It was 240 s, and the check's own record says that is inside the real
# distribution rather than beyond it: of 39,876 stored verdicts the slowest
# ten run 200-254 s, the slowest of all 253.7 s. Over one day 643 decode jobs
# finished - 551 of them under ten seconds - with a tail of 17 between twenty
# and forty seconds, nine to eighty, two to 160, one to 239, and then
# SEVENTEEN sitting exactly on the 240 s wall. A cap landing in the middle of
# a tail turns contention into a verdict.
#
# And it IS contention, not the files. Measured by hand against the two worst
# repeat offenders while the queue ran: #55782, a 5.8 GB film that had timed
# out seven times, read its head in 0.1 s and decoded both windows in 1.1 s
# and 0.7 s; #56100 in 1.6 s and 2.0 s. Nothing is wrong with either. They
# were asked while 153 other jobs were on the same spindles.
#
# Ten minutes clears the measured tail four times over and still bounds a
# genuinely stuck read.
DECODE_BUDGET_S = 600
# HOW LONG A FILE THAT COULD NOT BE MEASURED WAITS BEFORE BEING ASKED AGAIN,
# and how many times before it is left alone. A timeout used to write nothing
# at all, so the candidate query - which offers any file with no row - handed
# the same file straight back on the next pass, forever: one film burned seven
# slots and twenty-eight minutes of pool time for no answer, ahead of files
# that had never been looked at once.
NOANSWER_BACKOFF = (1800, 7200, 21600, 86400)
MAX_NOANSWER = len(NOANSWER_BACKOFF)
# How many files one pass may test. Each costs two bounded decodes off a
# spinning pool disk; eight is roughly a minute of work and leaves the disks
# to whatever else wants them.
PER_RUN = 8
# The loop's own clock. The sweep only actually reads anything when the pool is
# idle, so a short cycle costs a `_too_busy()` call and nothing else.
CYCLE_S = 300
# Never test a file that was written in the last few minutes. A file still
# landing decodes badly for a reason that has nothing to do with its release.
SETTLE_S = 600

# Messages that mean the bytes are wrong. Deliberately a subset of what a
# decoder can complain about - the same list refetch.py keeps for error
# strings, for the same reason: a message nobody has classified must not
# arrive with a delete button attached.
_FATAL = [
    (r"moov atom not found", "the file is missing its index and is truncated"),
    (r"invalid data found", "the decoder rejected the stream"),
    (r"unexpected end of file|truncat", "the file ends early"),
    (r"error while decoding|decode(r)? (error|failed)", "decoding failed"),
    (r"corrupt|damaged", "the stream is corrupt"),
    (r"could not find codec parameters", "the stream parameters are unreadable"),
    (r"no such file or directory", "the file is not there"),
    (r"invalid nal unit|missing picture in access unit", "the video stream is broken"),
]

# NOTHING WAS DECODED, AND THAT IS NOT THE SAME AS DECODING CLEANLY.
#
# Erik: "looks like the decode pass 1 failed to check for these encode sub
# issues". It was worse than that - pass 1 had not read a single frame.
#
# 'Sentenced to Be a Hero' S01E01-E12 are Matroska files with 108 streams and a
# video stream ffmpeg cannot identify at all:
#
#     Stream #0:0: Video: none, none, 1920x1080, 23.98 fps   codec_tag [0][0][0][0]
#
# The container carries the picture's shape and not what the frames are. So the
# check's own command - map 0:v:0, decode twenty seconds - returned -22 with
#
#     [vist#0:0/none] Decoding requested, but no decoder found for: none
#
# and test_one stored verdict=ok with that sentence as its "note", because the
# message matched nothing in _FATAL and the return code was thrown away on the
# line that read `_ = rc`. The panel then said "decodes cleanly at both ends"
# over a file where no decode had happened, twice, and eighteen encode jobs
# went on to fail on the same twelve files.
#
# This is deliberately NOT _FATAL. The bytes are not known to be wrong - a
# player with the right decoder might be perfectly happy - so it must not
# arrive at remedy.py's auto-delete path. It is its own verdict: nuarr cannot
# read this, which is a fact about nuarr as much as about the file.
_NO_DECODER = re.compile(
    r"no decoder found for|decoder not found|"
    r"(video|audio):\s*none|unknown codec", re.I)

# SHORT is its own verdict and not CORRUPT, on purpose. CORRUPT maps to
# remedy.py's file/corrupt, which carries auto_replace=True - in auto mode
# that deletes the file and asks the arr for another. "A window came back
# empty" can also mean a seek landed badly on a pool disk that was spinning
# up, and a maybe must never hold a delete button. file/short is replaceable
# only by a person.
OK, CORRUPT, UNREADABLE, SHORT = "ok", "corrupt", "unreadable", "short"


def _no_decoder(rc: int, err: str) -> str:
    """Did this run decode anything at all? -> why not, or "".

    See the note above _NO_DECODER. Returns the sentence a person reads, and
    only for the case that is certain: ffmpeg said in words that it had no
    decoder. A non-zero exit with stderr nobody has classified is left alone
    here and reported as itself by test_one - fail closed on the delete path,
    loud everywhere else, which is this module's rule.
    """
    if _NO_DECODER.search(err or ""):
        return ("ffmpeg has no decoder for this file's video stream - the "
                "container describes the picture and not what the frames "
                "are, so nothing was decoded and nothing can re-encode it")
    _ = rc
    return ""
MISSING = "missing"          # was not on disk; not a fault in the bytes

STATE = {"running": False, "done": 0, "total": 0, "now": "", "last_run": 0.0,
         "last_error": "", "tested": 0, "found": 0,
         # How many the feeder handed to the main queue last pass, and how
         # many decode jobs are live on it now.
         "fed": 0, "on_queue": 0}

_READY = False


def init() -> None:
    r"""One row per file, keyed to the BYTES rather than the path.

    size and mtime are stored with the verdict so a file is re-tested when and
    only when it changes. Without that, the sweep either re-decodes a clean
    library forever or trusts a verdict about a file that has since been
    replaced by an upgrade - and the second one is how a check quietly stops
    being true.
    """
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS integrity(
                file_id INTEGER PRIMARY KEY,
                path    TEXT,
                size    INTEGER,
                mtime   REAL,
                at      REAL,
                verdict TEXT,
                detail  TEXT,
                secs    REAL
            )""")
        # HOW MANY TIMES THIS FILE HAS BEEN ASKED AND NOT ANSWERED. See
        # NOANSWER_BACKOFF: without it a timeout leaves no trace and the
        # feeder offers the same file again on the next pass.
        icols = {r["name"] for r in cur.execute("PRAGMA table_info(integrity)")}
        if "tries" not in icols:
            cur.execute("ALTER TABLE integrity ADD COLUMN tries INTEGER "
                        "DEFAULT 0")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_integrity_verdict "
                    "ON integrity(verdict)")
    _READY = True


def _ffmpeg() -> str:
    from .jobs import _ffmpeg_exe
    return _ffmpeg_exe()


def mode() -> str:
    m = str(getattr(SETTINGS, "integrity_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


def _fatal(err: str) -> str:
    low = (err or "").lower()
    for pat, why in _FATAL:
        if re.search(pat, low):
            return why
    return ""


async def _decode(path: str, ss: float, dur: float,
                  on_pid=None) -> tuple[int, str, int]:
    """Decode a window. -> (returncode, stderr, frames).

    THE FRAME COUNT IS THE THIRD THING, and it is the one that catches
    truncation. `-progress pipe:1` writes `frame=N` to stdout in a form
    nothing has to parse English for; stderr is untouched, so every existing
    rule about what counts as fatal still reads exactly what it read before.

    -xerror stops at the first error rather than logging thousands of them,
    which is both faster and the difference between a 200-byte stderr and a
    200 MB one. Video only: an audio decode error is a real fault but a much
    noisier one, and the audio findings have their own check.
    """
    # A BOUNDED NUMBER OF THREADS, BECAUSE THIS RUNS BESIDE EVERYTHING NOW.
    # ffmpeg's default is one thread per core, and a 4K HEVC decode will take
    # all twenty of them - measured on the first queued pass: two of these at
    # once put the box at 99.8% CPU while somebody was watching Plex. On the
    # idle runner that never showed, because it ran one at a time and only
    # when the machine was quiet. The check does not need to be fast; it needs
    # to be a corner of the machine, so it gets four threads and the pool's
    # two-at-once is eight.
    # NOT ON THE CARD, DELIBERATELY - and this is the one place in nuarr
    # where that is a correctness rule rather than a performance one.
    # Measured: -hwaccel cuda is 84-93% cheaper on CPU here, and on a file
    # with noise written through its video the software decoder reports
    # "corrupt decoded frame" and exits non-zero while NVDEC reports nothing
    # and exits 0. A hardware decoder is built to keep playing through damage;
    # this check exists to notice damage. See build_ffmpeg for where the card
    # is the right answer.
    args = [_ffmpeg(), "-hide_banner", "-v", "error", "-xerror", "-nostdin",
            "-progress", "pipe:1", "-threads", str(DECODE_THREADS)]
    if ss > 0:
        args += ["-ss", f"{ss:.2f}"]
    args += ["-i", path, "-t", f"{dur:.2f}", "-map", "0:v:0?",
             "-f", "null", "-"]
    try:
        # NO_WINDOW, AND THE CONSOLE WATCHER IS WHY THIS COMMENT EXISTS.
        #
        # This spawn shipped without it and the first sweep put nine ffmpeg
        # console windows on the desktop - caught, named and dated by
        # consolewatch within the hour, which is the entire reason that watcher
        # was written. config.py states the rule plainly: every child process
        # nuarr spawns must pass this flag, because the server runs as a
        # service and a console-subsystem child with no console of its own gets
        # given a brand new visible one by Windows.
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        # WHOEVER IS COUNTING THE BYTES NEEDS THE PID. Without it a decode
        # reads twenty seconds off a pool disk and the panel files those bytes
        # under "system", which is the column that means "not nuarr" and the
        # number the gate steers by.
        if on_pid is not None:
            try:
                on_pid(proc.pid)
            except Exception:                                    # noqa: BLE001
                pass
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=DECODE_BUDGET_S)
        # THREE STATES, NOT TWO: a number, zero, or "the counter did not
        # answer". The last matters because the whole truncation rule rests
        # on this parse - if -progress ever stops emitting what it emits
        # today, a two-state count would silently read every window of every
        # file as empty and condemn the entire library in one sweep. An
        # unknown count is skipped instead, which costs one file's worth of
        # coverage rather than all of them.
        raw = out or b""
        seen = re.findall(rb"^frame=\s*(\d+)", raw, re.M)
        if seen:
            frames = int(seen[-1])
        elif b"progress=" in raw:
            frames = 0          # it reported, and it reported nothing
        else:
            frames = -1         # it did not report at all
        return (proc.returncode or 0,
                (err or b"").decode("utf-8", "replace"), frames)
    except asyncio.TimeoutError:
        # AND THE PROCESS IS KILLED, not abandoned. wait_for cancels the await;
        # it does not stop the ffmpeg on the other end of it, so every timeout
        # left a process still reading the spindle that made it time out - the
        # one thing guaranteed to make the next check time out too.
        try:
            proc.kill()
        except Exception:                                        # noqa: BLE001
            pass
        # A DECODE THAT NEVER FINISHES IS NOT A VERDICT. It is usually a disk
        # that went to sleep or a pool member being rebalanced under us, and
        # calling that corruption would delete a healthy file.
        return -1, "__timeout__", 0
    except Exception as e:                                       # noqa: BLE001
        return -1, f"__spawn__ {type(e).__name__}: {e}", 0


def say(detail: str) -> str:
    r"""An internal sentinel, in words a person reads.

    `_decode` marks "there is no verdict" with __timeout__ and __spawn__ so
    its callers can test for it without parsing English. Those markers were
    then handed straight to the job note, and the Activity feed showed
    "no verdict - __timeout__" - which tells the reader nothing except that
    something inside has leaked out. The markers stay; this is where they stop.
    """
    d = (detail or "").strip()
    if d == "__timeout__":
        return (f"the decode ran past {DECODE_BUDGET_S // 60} minutes without "
                "finishing - usually a spun-down pool disk, a member being "
                "rebalanced underneath it, or simply too much else reading the "
                "same spindle; not a fault in the file")
    if d.startswith("__spawn__"):
        return ("ffmpeg could not be started ("
                + (d[len("__spawn__"):].strip() or "no reason given") + ")")
    return d or "no verdict"


def windows(duration: float) -> list[tuple]:
    r"""Where to look, and what to call each place. -> [(ss, secs, label)]

    THE HEAD ALWAYS, because header and stream damage live there and it needs
    no duration to find. THE TAIL when the duration is known, because seeking
    to an unknown offset lands somewhere arbitrary and an arbitrary decode
    failure is evidence of nothing. THE MIDDLE because head plus tail is 45
    seconds of a 22-minute episode and everything between them was nobody's
    job - a test file with noise written over its halfway mark decoded
    cleanly at both ends and was recorded healthy.

    The middles are spaced inside the gap rather than across the whole file,
    so they never overlap the two windows that were already there, and they
    only appear on a file long enough to have a middle worth sampling.
    """
    w: list[tuple] = [(0.0, float(HEAD_S), f"the first {HEAD_S}s")]
    if not duration or duration <= (HEAD_S + TAIL_S + 5):
        return w
    lo = HEAD_S + 5.0
    hi = duration - TAIL_S - MID_S - 5.0
    if hi > lo:
        for i in range(MID_WINDOWS):
            ss = lo + (hi - lo) * ((i + 1) / (MID_WINDOWS + 1))
            w.append((ss, float(MID_S),
                      f"{MID_S}s at {int(ss // 60)}m{int(ss % 60):02d}s"))
    w.append((max(0.0, duration - TAIL_S), float(TAIL_S),
              f"the last {TAIL_S}s"))
    return w


async def test_one(file_id: int, path: str, duration: float = 0.0,
                   on_pid=None, on_stage=None) -> dict:
    """Every window in windows(). -> {verdict, detail, secs}"""
    t0 = time.time()
    wins = windows(duration)
    notes: list[str] = []
    empty: list[str] = []
    full = 0
    rc_any = 0
    for i, (ss, secs, label) in enumerate(wins):
        if on_stage:
            on_stage(label, 100.0 * i / max(1, len(wins)))
        rc, err, frames = await _decode(path, ss, secs, on_pid)
        if err.startswith("__"):
            # No verdict at all - a timeout or a failed spawn. Only the first
            # window is allowed to end the whole check that way; later ones
            # simply stop it, because what has already been decoded is still
            # worth recording.
            if i == 0:
                return {"verdict": "", "detail": err,
                        "secs": time.time() - t0}
            break
        # BEFORE _fatal, because "there is no decoder" is not "the bytes are
        # bad" and must never reach the path that deletes a file.
        why = _no_decoder(rc, err)
        if why:
            return {"verdict": UNREADABLE, "detail": why,
                    "secs": time.time() - t0}
        why = _fatal(err)
        if why:
            return {"verdict": CORRUPT,
                    "detail": f"{why} (in {label}): "
                              f"{err.strip().splitlines()[0][:200]}",
                    "secs": time.time() - t0}
        if frames > 0:
            full += 1
        elif frames == 0:
            empty.append(label)
        # frames < 0 is "the counter did not answer" - see _decode. Not
        # health, not damage, so it is not counted as either.
        if err.strip():
            notes.append(f"{label}: {err.strip().splitlines()[0][:120]}")
        rc_any = rc_any or rc

    # A WINDOW THAT DECODED NOTHING IS NOT A WINDOW THAT DECODED CLEANLY.
    #
    # This is how truncation was getting through. A file cut off at 66% of
    # its bytes still says 90.1s in its header, so the tail window seeks to
    # 65.1s - past where the picture stops at 56.6s - reads no frames, and
    # exits 0. ffmpeg says "File ended prematurely", which matches nothing in
    # _FATAL, so the check that exists to catch truncation returned ok.
    #
    # NOT CORRUPT. See the verdict list: CORRUPT carries auto_replace into
    # remedy.py and a seek landing badly on a pool disk that was spinning up
    # would then delete a healthy file. SHORT is put in front of a person.
    if empty and full:
        runs = (f"{int(duration // 60)}m{int(duration % 60):02d}s"
                if duration else "an unknown length")
        return {"verdict": SHORT,
                "detail": ("no picture came back from "
                           + ", ".join(empty[:3])
                           + f" - the file says it runs {runs} and decoded "
                             f"fine in {full} other window(s), so it ends or "
                             f"breaks before it claims to")[:600],
                "secs": time.time() - t0}
    if empty and not full:
        return {"verdict": SHORT,
                "detail": ("no picture came back from anywhere in this file - "
                           f"{len(empty)} window(s) tried and every one of "
                           "them decoded nothing")[:600],
                "secs": time.time() - t0}

    note = "; ".join(notes)[:300]
    # AND THE EXIT CODE IS PART OF THE ANSWER. This line used to read `_ = rc`
    # - the return code was read from the process and then thrown away, so an
    # ffmpeg that refused the command outright was recorded as an ok verdict
    # with its complaint filed as a "note". The verdict still fails open (see
    # the module docstring: an unclassified message must not arrive with a
    # delete button attached) but it no longer fails SILENT: a run that ended
    # badly says so in the sentence the panel prints.
    if rc_any:
        note = (f"ffmpeg exited {rc_any}" + (f" - {note}" if note else ""))[:300]
    secs_read = sum(w[1] for w in wins)
    return {"verdict": OK,
            "detail": note or (f"decoded {int(secs_read)}s across "
                               f"{len(wins)} windows - "
                               + ", ".join(w[2] for w in wins)),
            "clean": not rc_any,
            "secs": time.time() - t0}


def _candidates(limit: int) -> list[dict]:
    r"""Files whose bytes have never been decoded, or have changed since.

    Never-tested first, oldest verdict second. The join is on size and mtime
    rather than on the row's existence, so an upgraded file - same id, new
    bytes - comes back round as if it had never been seen, which is exactly
    what it is.
    """
    if not _READY:
        init()
    # THE FILE'S CLOCK, NOT NUARR'S BOOKKEEPING CLOCK.
    #
    # This read files.updated_at, which is when nuarr last touched the ROW -
    # and the boot recovery walk touches every row it looks at. Measured on
    # this box straight after a restart: 39,710 of 39,710 files had an
    # updated_at inside the settling window, so the candidate query returned
    # nothing at all and the sweep reported "tested 0" while looking perfectly
    # healthy. A filter meant to skip the handful of files still being written
    # was excluding the entire library.
    #
    # mtime is the filesystem's own answer to "when was this last written",
    # which is the question that was being asked.
    #
    # AND 'eligible' IS EXEMPT FROM IT, because it has already served a longer
    # one. A file becomes eligible by sitting out nuarr's own settle hold -
    # half an hour by default - so applying a second ten-minute guard to it
    # only delays the one population that is waiting to be processed.
    # Measured when Erik asked why eligible files were not moving: fifteen of
    # them were held on this check, and fourteen were excluded from its
    # candidate list for being under ten minutes old.
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT f.id file_id, f.path, f.size, f.duration, f.pool_disk, "
            "       i.at last_at, i.verdict last_verdict, "
            # WHICH PASS THIS IS. `i` is joined on size and therefore goes
            # null the moment the file is rewritten; `p` is the same row
            # without that condition, so it answers "has anything ever
            # decoded this file", which is the difference between a first
            # look and a re-check of what nuarr has just written. One row
            # per file (file_id is the primary key), so no fan-out.
            "       p.at prev_at, p.size prev_size, p.verdict prev_verdict "
            "  FROM files f "
            "  LEFT JOIN integrity i ON i.file_id = f.id "
            "                       AND i.size = f.size "
            "  LEFT JOIN integrity p ON p.file_id = f.id "
            " WHERE f.state NOT IN ('deleted','duplicate') "
            "   AND COALESCE(f.path,'') != '' "
            "   AND COALESCE(f.size,0) > 0 "
            "   AND (COALESCE(f.mtime, 0) < ? OR f.state='eligible') "
            # A FILE THAT COULD NOT BE ANSWERED WAITS BEFORE BEING ASKED
            # AGAIN, and after four goes is left alone. See NOANSWER_BACKOFF.
            "   AND (i.file_id IS NULL OR (i.verdict = '' AND "
            + noanswer_wait_sql("i") + ")) "
            " ORDER BY f.id LIMIT ?", (cutoff, time.time(), int(limit)))]


def _candidates_per_disk(per_disk: int) -> list[dict]:
    """The oldest N untested files on EACH spindle. Same filter as
    _candidates; the window is what makes the hand-over disk-diverse."""
    if not _READY:
        init()
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT file_id, path, size, duration, pool_disk, "
            "       prev_at, prev_size FROM ("
            "  SELECT f.id file_id, f.path, f.size, f.duration, f.pool_disk, "
            "         p.at prev_at, p.size prev_size, "
            # ELIGIBLE FIRST. precedence.py holds a transcode until this
            # verdict exists, so a file about to be processed must not queue
            # behind the never-checked back catalogue on its disk.
            "         ROW_NUMBER() OVER (PARTITION BY COALESCE(f.pool_disk,'') "
            "                            ORDER BY (f.state='eligible') DESC, f.id) rn "
            "    FROM files f "
            "    LEFT JOIN integrity i ON i.file_id = f.id AND i.size = f.size "
            "    LEFT JOIN integrity p ON p.file_id = f.id "
            "   WHERE f.state NOT IN ('deleted','duplicate') "
            "     AND COALESCE(f.path,'') != '' "
            "     AND COALESCE(f.size,0) > 0 "
            # See _candidates: eligible has already served a longer settle.
            "     AND (COALESCE(f.mtime, 0) < ? OR f.state='eligible') "
            # The same wait as _candidates - see noanswer_wait_sql.
            "     AND (i.file_id IS NULL OR (i.verdict = '' AND "
            + noanswer_wait_sql("i") + "))) "
            " WHERE rn <= ? ORDER BY pool_disk, rn",
            (cutoff, time.time(), int(per_disk)))]


# WHICH PASS, AND WHAT IT IS FOR.
#
# Erik: "show the passes like for example decode pass 1 and pass 2 ... first
# pass to check the file can be read then second pass to check if the file is
# not corrupt after encode/passthrough".
#
# It is not a fixed two-step - most files are checked once and never again -
# but when a file IS checked twice the two checks are asking different
# questions, and the panel said "decode" both times with no way to tell which.
# The verdict row is keyed to the bytes, so a rewrite makes the file a new
# file and the second check is the one that says nuarr did not break it.
#
# Measured over seven days: 15,269 first looks, and 3,340 second passes - 2,676
# of them after a subtitle rewrite, 627 after a transcode, 37 after an OCR
# embed. So the second pass is named after whatever actually rewrote the file,
# not after the transcode it usually is not.
_AFTER_WORD = {"transcode": "the rewrite", "subs": "the subtitle rewrite",
               "sub_ocr": "the OCR embed", "audio": "the tag write"}


def _after_what(rows: list) -> None:
    """Annotate each row with its pass number and what rewrote it. One query."""
    second = [r for r in rows if (r.get("prev_at") or 0)
              and int(r.get("prev_size") or 0) != int(r.get("size") or 0)]
    # Identity, not equality: two rows for two files can compare equal on the
    # columns that matter and `r in second` would then mislabel both.
    ids = {id(r) for r in second}
    for r in rows:
        r["pass"] = 2 if id(r) in ids else 1
        r["after"] = ""
    if not second:
        return
    try:
        with cursor() as cur:
            for r in second:
                w = cur.execute(
                    "SELECT kind, pool FROM jobs "
                    " WHERE file_id=? AND state='done' AND finished_at > ? "
                    "   AND kind IN ('transcode','subs','sub_ocr','audio') "
                    " ORDER BY finished_at DESC LIMIT 1",
                    (int(r["file_id"]), float(r["prev_at"] or 0))).fetchone()
                if w:
                    # A transcode says which of its two shapes it was; the
                    # rest have one shape each and their kind is the word.
                    r["after"] = (("the " + (w["pool"] or "rewrite"))
                                  if w["kind"] == "transcode"
                                  else _AFTER_WORD.get(w["kind"], "the rewrite"))
                else:
                    # Nothing of nuarr's is recorded against it, so the bytes
                    # changed some other way - the arr replaced the file. Say
                    # that rather than blaming a rewrite that did not happen.
                    r["after"] = ""
                    r["replaced"] = True
    except Exception:                                            # noqa: BLE001
        pass


def pass_words(r: dict) -> tuple:
    """(short label, the sentence) for one candidate row."""
    if int(r.get("pass") or 1) < 2:
        return ("pass 1",
                "nothing has decoded these bytes yet - this is the check that "
                "says the file can be played at all")
    if r.get("replaced"):
        return ("pass 1",
                "the file at this name was replaced since the last check, so "
                "these are new bytes and this is a first look at them")
    after = r.get("after") or "the rewrite"
    return ("pass 2",
            f"{after} has rewritten this file since the last check, so these "
            f"are different bytes - this is the one that says what nuarr "
            f"wrote is not corrupt")


def noanswer_wait_sql(alias: str = "i") -> str:
    r"""The "has it waited long enough to be asked again" test, as SQL.

    ONE LADDER, BOTH FEEDERS. _candidates and _candidates_per_disk each decide
    what to offer, and a file that could not be measured has to look the same
    to both of them or the disk-diverse feeder simply undoes the wait. Takes
    one bound parameter - the current time - immediately after the settle
    cutoff.
    """
    ladder = " ".join(f"WHEN {n} THEN {v}"
                      for n, v in enumerate(NOANSWER_BACKOFF, start=1))
    return (f"COALESCE({alias}.tries,0) < {MAX_NOANSWER} AND ? > {alias}.at + "
            f"CASE COALESCE({alias}.tries,0) {ladder} "
            f"ELSE {NOANSWER_BACKOFF[-1]} END")


def _write(file_id: int, path: str, size: int, mtime: float, out: dict) -> None:
    # A VERDICT RESETS THE COUNT; NO ANSWER ADDS TO IT. An answer of any kind
    # means the file could be read after all, so whatever went wrong before is
    # no longer true of it.
    tries = 0 if out.get("verdict") else int(out.get("tries") or 1)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO integrity(file_id,path,size,mtime,at,verdict,detail,"
            "secs,tries) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, "
            "  size=excluded.size, mtime=excluded.mtime, at=excluded.at, "
            "  verdict=excluded.verdict, detail=excluded.detail, "
            "  secs=excluded.secs, tries=excluded.tries",
            (int(file_id), path, int(size or 0), float(mtime or 0), time.time(),
             out.get("verdict") or "", (out.get("detail") or "")[:600],
             float(out.get("secs") or 0), tries))


async def sweep(limit: int = 0, force: bool = False) -> dict:
    """One bounded pass. Returns what it did."""
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    if not _READY:
        init()
    if not force:
        try:
            from .audit import _too_busy
            if await _too_busy():
                return {"ok": False, "why": "the pool is busy"}
        except Exception:                                        # noqa: BLE001
            pass
    rows = await asyncio.to_thread(_candidates, int(limit or PER_RUN))
    STATE.update(running=True, done=0, total=len(rows), now="", found=0)
    tested = found = 0
    try:
        for r in rows:
            STATE["now"] = os.path.basename(r["path"] or "")[:60]
            STATE["done"] = tested
            try:
                st = os.stat(r["path"])
            except OSError:
                # GONE IS SOMEBODY ELSE'S FINDING. The missing-from-disk check
                # owns that, and writing a verdict here would give one fact two
                # owners that can disagree.
                continue
            out = await test_one(int(r["file_id"]), r["path"],
                                 float(r.get("duration") or 0))
            if not out.get("verdict"):
                # timeout or spawn failure - no verdict, try again another day
                continue
            await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                                    st.st_size, st.st_mtime, out)
            tested += 1
            if out["verdict"] == CORRUPT:
                found += 1
                joblog.log(f"integrity: {os.path.basename(r['path'])} - "
                           f"{out['detail']}", "error")
    finally:
        STATE.update(running=False, now="", last_run=time.time(),
                     tested=tested, found=found)
    if found:
        joblog.log(f"integrity sweep: {found} of {tested} file(s) failed to "
                   f"decode", "error")
    return {"ok": True, "tested": tested, "found": found,
            "remaining": await asyncio.to_thread(untested)}


def untested() -> int:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) n FROM files f "
                "LEFT JOIN integrity i ON i.file_id=f.id AND i.size=f.size "
                "WHERE f.state NOT IN ('deleted','duplicate') "
                "  AND COALESCE(f.size,0) > 0 AND i.file_id IS NULL"
            ).fetchone()
        return int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def findings(limit: int = 200) -> list[dict]:
    """The ones that failed, in remedy.py's shape.

    THREE VERDICTS, THREE KINDS. 'corrupt' means the bytes are wrong and
    only a different release can help, which is why auto mode may act on it.
    'unreadable' means ffmpeg has no decoder for the stream - the file may be
    perfectly good to something else - so it is reported and never replaced
    without a person saying so. Until this, unreadable was a constant this
    module defined and nothing ever produced or read.

    'short' is the newest and the same shape as unreadable: a window of the
    file decoded nothing where frames were expected. Usually an interrupted
    download that ends before its header says it does; occasionally a disk
    that was not ready. A person decides.

    EVERY VERDICT THAT IS NOT OK BELONGS IN THIS LIST. The WHERE clause below
    is the only thing standing between a verdict and the panel, and a new
    verdict that is written but never selected is written to nobody.
    """
    if not _READY:
        init()
    kinds = {CORRUPT: "file/corrupt", UNREADABLE: "file/unreadable",
             SHORT: "file/short"}
    try:
        with cursor() as cur:
            return [{"file_id": r["file_id"],
                     "kind": kinds.get(r["verdict"], "file/corrupt"),
                     "path": r["path"], "why": r["detail"], "at": r["at"]}
                    for r in cur.execute(
                        "SELECT i.file_id, i.path, i.detail, i.at, i.verdict "
                        "  FROM integrity i JOIN files f ON f.id = i.file_id "
                        " WHERE i.verdict IN (?, ?, ?) "
                        "   AND f.state NOT IN ('deleted','duplicate') "
                        " ORDER BY i.at DESC LIMIT ?",
                        (CORRUPT, UNREADABLE, SHORT, int(limit)))]
    except Exception:                                            # noqa: BLE001
        return []


def stats() -> dict:
    if not _READY:
        init()
    # WHERE THE WORK ACTUALLY IS. STATE belongs to sweep(), which is now the
    # manual button; the sweep that runs all day is the shared runner's, and a
    # panel reading the wrong dict is a panel that says "idle" while the disks
    # are going - which is exactly how the sidecar system came to be missing
    # from the running list for months.
    # THE WORK IS ON THE MAIN QUEUE NOW, so that is where "is it running" is
    # read from: decode jobs queued and running, and the live workers' files.
    # The shared runner's strip is gone with the runner.
    q = {"queued": 0, "running": 0}
    now_word = ""
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT state, COUNT(*) n FROM jobs "
                    " WHERE kind='decode' AND state IN ('queued','running') "
                    " GROUP BY state"):
                q[r["state"]] = int(r["n"] or 0)
        from . import jobs as _jobs
        for w in list(_jobs.RUNNING.values()):
            if getattr(w.job, "kind", "") == "decode":
                now_word = os.path.basename(w.job.path or "")[:60]
                break
    except Exception:                                            # noqa: BLE001
        pass
    _live = bool(q["running"]) or STATE["running"]
    out = {"ok": 0, "corrupt": 0, "untested": untested(), "mode": mode(),
           "last_run": STATE["last_run"],
           "running": _live,
           "now": (now_word or STATE["now"]) if _live else "",
           "done": STATE["done"], "total": STATE["total"],
           "paused": False, "paused_why": "",
           "queue": q, "on_queue": q["queued"] + q["running"],
           "fed": STATE.get("fed") or 0,
           "idle": {},
           "head_s": HEAD_S, "tail_s": TAIL_S, "per_run": PER_RUN,
           # WHAT IT READS, FOR A FILE OF TYPICAL LENGTH. The panel used to
           # spell out the head and the tail because those were all there
           # were; it cannot spell out windows whose number depends on the
           # file, so it is given the shape of a middling one to describe.
           "mid_s": MID_S, "mid_windows": MID_WINDOWS,
           "windows_example": [
               {"at": round(ss), "secs": round(secs), "label": label}
               for ss, secs, label in windows(22 * 60.0)]}
    try:
        with cursor() as cur:
            # LIVE FILES ONLY - the same join findings() uses. A verdict
            # outlives its file: the remedy replaces a corrupt release, the
            # old row leaves the files table, and the integrity row stayed
            # and was still counted. The card read "4 that will not decode"
            # over a list of none, because the four had all been dealt with.
            for r in cur.execute(
                    "SELECT i.verdict, COUNT(*) n FROM integrity i "
                    "  JOIN files f ON f.id = i.file_id "
                    " WHERE f.state NOT IN ('deleted','duplicate') "
                    " GROUP BY i.verdict"):
                out[r["verdict"] or "none"] = r["n"]
    except Exception:                                            # noqa: BLE001
        pass
    return out


# --------------------------------------------------------- onto the runner --
#
# EIGHT FILES AND THEN FIVE MINUTES ASLEEP, on a box that is idle most of the
# night. The batch was never protection - eight bounded decodes started the
# instant somebody presses play are still eight - and it was a real cost:
# 39,710 files at eight a pass, five minutes apart, is eleven days of
# wall-clock to walk the library once, nearly all of it spent waiting.
#
# The shared runner asks the gate before every single file instead, works at
# whatever rate the machine can spare, and steps around a spindle somebody is
# reading from rather than stopping on it. Same question, asked properly.
#
# sweep() is left exactly as it was: it is the "test some now" button, and a
# button is a batch by definition.
KEY = "integrity"
TITLE = "Does it decode?"


def _pending() -> list:
    """Every file whose bytes have never been decoded. Not a slice of them."""
    if not _READY:
        init()
    try:
        return _candidates(100000)
    except Exception:                                            # noqa: BLE001
        return []


async def _do_one(r: dict, report=None) -> dict:
    """Decode both ends of one file and write the verdict."""
    try:
        st = os.stat(r["path"])
    except OSError:
        # GONE IS SOMEBODY ELSE'S FINDING. The missing-from-disk check owns
        # that, and writing a verdict here would give one fact two owners that
        # can disagree.
        return {"ok": True, "skipped": True}
    work = getattr(report, "task", None)

    def _stage(which, pct):
        if work is not None:
            work.note = ("decoding the first "
                         f"{HEAD_S}s" if which == "head"
                         else f"decoding the last {TAIL_S}s")
        if report is not None:
            try:
                report(pct)
            except Exception:                                    # noqa: BLE001
                pass
    out = await test_one(int(r["file_id"]), r["path"],
                         float(r.get("duration") or 0),
                         on_pid=(work.set_pid if work is not None else None),
                         on_stage=_stage)
    if not out.get("verdict"):
        # timeout or spawn failure - no verdict, try again another day
        return {"ok": False, "why": say(out.get("detail"))}
    await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                            st.st_size, st.st_mtime, out)
    if out["verdict"] == CORRUPT:
        joblog.log(f"integrity: {os.path.basename(r['path'])} - "
                   f"{out['detail']}", "error")
    return {"ok": True, "verdict": out["verdict"]}


async def _after(_d: dict) -> None:
    """What a finished pass is for: handing the findings to the remedy."""
    from . import remedy
    got = findings(50)
    if got:
        await remedy.auto(got, "integrity", mode() == "auto")


# ------------------------------------------------------- onto the queue --
#
# AND THEN OFF THE RUNNER AGAIN, ONTO THE MAIN QUEUE. The shared runner was the
# right answer to "eight files then five minutes asleep": it asked the gate per
# file and stepped around a viewer's disk. But it was still a second scheduler
# with a second idea of what the machine could spare, running beside the queue
# that every other piece of file work goes through - and "what is nuarr doing
# to my files" had two answers again. Erik asked for it under the main queue,
# queued across disks that are not busy.
#
# So it is a job kind now. The feeder deals untested files to the jobs table
# round robin by spindle - the lesson the subtitle queue learned when 3,302 of
# 5,300 files sat on one disk and eleven others idled - and the dispatcher does
# the rest: it already prefers the quietest disk, refuses a viewer's, and steers
# around one something else is hammering, for every pool alike. The candidate
# list does not need a table of its own - `integrity` LEFT JOIN `files` on size
# IS the queue, and a file leaves it the moment its verdict is written.
QUEUE_DEPTH = 120


def _to_hand_over(depth: int) -> tuple:
    """How much room the queue has for decode jobs, and which files fill it."""
    with cursor() as cur:
        have = int(cur.execute(
            "SELECT COUNT(*) n FROM jobs "
            " WHERE kind='decode' AND state IN ('queued','running')"
        ).fetchone()["n"] or 0)
    room = max(0, int(depth) - have)
    if not room:
        return have, []
    # A SLICE PER DISK, NOT A SLICE OF THE TABLE. _candidates orders by file
    # id, and file ids were handed out as the library was walked - disk by
    # disk. Measured on the first run: the first 2,400 untested files were all
    # on NU-DRIVE-1, so a round-robin over them dealt 120 files to one disk,
    # which is the exact failure it exists to prevent. The window takes the
    # oldest few dozen from EVERY disk that has any.
    rows = _candidates_per_disk(max(4, room))
    # A FILE THE PROCESSING SYSTEM IS HELD ON COMES FIRST. The window above is
    # already eligible-first, which is the right population; this is the one
    # file in it that something is actually standing still behind.
    try:
        from . import precedence as _prec
        rows = _prec.wanted_first("decode", rows)
    except Exception:                                            # noqa: BLE001
        pass
    # Skip what is already on the jobs table for any reason - enqueue would
    # refuse it anyway, but refusing two hundred rows a pass is noise.
    try:
        with cursor() as cur:
            live = {int(r["file_id"]) for r in cur.execute(
                "SELECT file_id FROM jobs WHERE state IN ('queued','running') "
                "  AND file_id IS NOT NULL")}
    except Exception:                                            # noqa: BLE001
        live = set()
    rows = [r for r in rows if int(r["file_id"]) not in live]

    def _deal(rs, n):
        """Round robin by spindle, oldest first within each."""
        by_disk: dict = {}
        for r in rs:
            by_disk.setdefault(r.get("pool_disk") or "?", []).append(r)
        out: list = []
        lanes = [iter(v) for _k, v in sorted(by_disk.items())]
        while lanes and len(out) < n:
            alive = []
            for it in lanes:
                if len(out) >= n:
                    alive.append(it)
                    continue
                try:
                    out.append(next(it))
                    alive.append(it)
                except StopIteration:
                    pass
            lanes = alive
        return out

    # THE BLOCKED ONES ARE DEALT THEIR OWN ROOM FIRST. Being first in the
    # candidate list is not enough: a round robin over twelve spindles can
    # spend the whole allowance on files nobody is waiting for before it
    # reaches them.
    try:
        from . import precedence as _prec
        want = _prec.wanted("decode")
    except Exception:                                            # noqa: BLE001
        want = set()
    front = _deal([r for r in rows if int(r["file_id"]) in want], room)
    rest = _deal([r for r in rows if int(r["file_id"]) not in want],
                 max(0, room - len(front)))
    for r in front:
        r["_blocking"] = True
    out = front + rest
    # Which pass each of these is, worked out once for the handful being
    # queued rather than for the whole candidate list.
    try:
        _after_what(out)
    except Exception:                                            # noqa: BLE001
        pass
    return have, out


def window_why(label: str) -> str:
    """What each window is there to catch, for the card's action list."""
    if label.startswith("the first"):
        return ("header and stream damage, bad indices, wrong codec "
                "parameters all show here")
    if label.startswith("the last"):
        return ("truncation - the commonest way a library file is broken - "
                "shows here as a window that decodes nothing")
    return "damage in the middle used to be invisible to this check"


def _job_plan(r: dict) -> str:
    """The instruction in the shape the queue panel reads plans in."""
    import json
    dur = float(r.get("duration") or 0)
    lbl, why = pass_words(r)
    # THE SAME WINDOWS THE CHECK WILL READ, from the function that decides
    # them. This said "the first 20s and the last 25s" for a check that has
    # read five windows since the middle was added.
    wins = windows(dur)
    secs = int(sum(w[1] for w in wins))
    what = (f"decode {secs}s in {len(wins)} windows" if len(wins) > 1
            else f"decode the first {HEAD_S}s")
    return json.dumps({
        "decode": True, "rewrite": False,
        # Carried so the worker card, the queue row, the activity pill and the
        # history line can all say the same thing without working it out again.
        # FROM THE LABEL, not from the raw count: a file the arr replaced has a
        # previous verdict and is still a first look, and the number has to
        # agree with the sentence beside it.
        "pass": 2 if lbl == "pass 2" else 1,
        "pass_label": lbl,
        "pass_why": why,
        "after": r.get("after") or "",
        "summary": f"{lbl} · {what}",
        "actions": [
            {"kind": "decode", "what": f"{lbl}: {what}", "why": why,
             "detail": ""}]
        + [{"kind": "decode", "what": f"decode {label} to null",
            "why": window_why(label), "detail": ""}
           for _ss, _sec, label in wins],
    })


async def topup(depth: int = QUEUE_DEPTH) -> dict:
    """Hand untested files to the main queue, spread across every spindle."""
    from . import jobs
    if not _READY:
        init()
    try:
        have, rows = await jobs.in_work(_to_hand_over, depth)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    # ONE TRANSACTION, OFF THE LOOP - see jobs.enqueue_many. Two of them: a
    # file the processing system is standing still behind goes in at the
    # promotion priority, ahead of the backlog nobody is waiting on. Queued at
    # 90 with the rest, it would have sat behind eighty-seven older rows and
    # the transcode would have waited for all of them.
    made = skipped = 0
    front = [x for x in rows if x.get("_blocking")]
    rest = [x for x in rows if not x.get("_blocking")]
    if front:
        from . import precedence as _prec
        rf = await jobs.in_work(
            jobs.enqueue_many,
            [{**x, "plan_json": _job_plan(x)} for x in front],
            "decode", _prec.PROMOTE_TO, "does it decode?")
        made += int(rf.get("made") or 0)
        skipped += int(rf.get("skipped") or 0)
    if rest:
        r = await jobs.in_work(jobs.enqueue_many,
                               [{**x, "plan_json": _job_plan(x)} for x in rest],
                               "decode", 90, "does it decode?")
        made += int(r.get("made") or 0)
        skipped += int(r.get("skipped") or 0)
    STATE["on_queue"] = have + made
    return {"ok": True, "made": made, "skipped": skipped,
            "on_queue": have + made}


async def job_one(r: dict, on_pid=None, on_stage=None) -> dict:
    r"""One file, inside a job. -> {ok, verdict|why, skipped}

    The runner's _do_one with the ledger removed: a job IS a ledger entry, so
    the pid and the stage go to the worker's card rather than to a second
    entry on the disk panel. The verdict is written here, and a corrupt one
    goes straight to the remedy rather than waiting for a pass to end - there
    are no passes any more.
    """
    try:
        st = os.stat(r["path"])
    except OSError:
        # WRITE THE ABSENCE DOWN, or the feeder picks the same file again on
        # its next pass - a file Sonarr had replaced was queued, skipped and
        # re-queued once a minute for as long as anyone looked. The row is
        # keyed to the size the files table holds, so a file that comes
        # back at a different size is a different file and is checked.
        await asyncio.to_thread(
            _write, int(r["file_id"]), r["path"], int(r.get("size") or 0),
            0.0, {"verdict": MISSING,
                  "detail": "not on disk when the check came round"})
        return {"ok": True, "skipped": True,
                "why": "not on disk - the missing-file check owns that"}

    def _stage(which, pct):
        if on_stage is not None:
            try:
                on_stage(f"decoding the first {HEAD_S}s" if which == "head"
                         else f"decoding the last {TAIL_S}s", pct)
            except Exception:                                    # noqa: BLE001
                pass
    out = await test_one(int(r["file_id"]), r["path"],
                         float(r.get("duration") or 0),
                         on_pid=on_pid, on_stage=_stage)
    if not out.get("verdict"):
        # NO ANSWER IS ALSO SOMETHING TO RECORD. It is not a verdict about the
        # file and must never read as one - the row keeps verdict '' - but the
        # ATTEMPT is a fact, and writing it down is what stops the feeder
        # handing the same file back every pass for the rest of the week.
        n = 1
        try:
            with cursor() as cur:
                row = cur.execute("SELECT tries FROM integrity WHERE file_id=?",
                                  (int(r["file_id"]),)).fetchone()
            n = int((row["tries"] if row else 0) or 0) + 1
        except Exception:                                        # noqa: BLE001
            pass
        out["tries"] = n
        await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                                st.st_size, st.st_mtime, out)
        if n >= MAX_NOANSWER:
            joblog.log(f"integrity: giving up on "
                       f"{os.path.basename(r['path'])} after {n} attempts "
                       f"with no answer - {say(out.get('detail'))}", "warn")
        return {"ok": False, "why": say(out.get("detail")),
                "tries": n, "gave_up": n >= MAX_NOANSWER}
    await asyncio.to_thread(_write, int(r["file_id"]), r["path"],
                            st.st_size, st.st_mtime, out)
    # BOTH FAILING VERDICTS GO TO THE REMEDY, and the remedy decides what may
    # be done about each: file/corrupt may be replaced by a sweep, and
    # file/unreadable may only be replaced by a person. Routing just the
    # corrupt ones here meant an unreadable file was written down and never
    # mentioned again.
    if out["verdict"] in (CORRUPT, UNREADABLE):
        joblog.log(f"integrity: {os.path.basename(r['path'])} - "
                   f"{out['detail']}",
                   "error" if out["verdict"] == CORRUPT else "warn")
        try:
            from . import remedy
            await remedy.auto(findings(50), "integrity", mode() == "auto")
        except Exception:                                        # noqa: BLE001
            pass
    return {"ok": True, "verdict": out["verdict"],
            "detail": out.get("detail") or "",
            # WHETHER FFMPEG ITSELF ENDED WELL, carried out to the job so the
            # note can stop claiming a clean decode after a bad exit.
            "clean": bool(out.get("clean", True)),
            "secs": out.get("secs") or 0}


FEED_S = 60.0


async def watch() -> None:
    """Keep the main queue topped up with untested files. Nothing else."""
    await asyncio.sleep(120)
    while True:
        try:
            d = await topup()
            STATE["fed"] = int(d.get("made") or 0)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(FEED_S)
