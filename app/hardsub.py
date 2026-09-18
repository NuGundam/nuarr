r"""nuarr - find the subtitles that are already in the picture.

THE FILES THAT SAY THEY HAVE NO SUBTITLES AND DO
------------------------------------------------
City Hunter (1987) is the case that started this. Japanese audio, Blu-ray rip,
and ffprobe reports zero subtitle streams on most of the season - which for a
Japanese show would make it unwatchable. It is not unwatchable: the English is
painted into the frames. E03 of the same season has a real subrip track, so
even within one folder the two kinds sit side by side and nothing distinguishes
them from the outside.

That matters for three separate reasons:

    BAZARR KEEPS DOWNLOADING. It asks "does this file have English subtitles",
    the answer is no, and it fetches one - forever, for every episode, because
    nothing it fetches ever changes the answer.

    THE LIBRARY LOOKS WRONG. Every count of "files with no subtitles" is
    inflated by files that have them, and any rule keyed on that count is
    reasoning about a number that is not true.

    AND IT CANNOT BE UNDONE. A burned-in subtitle is pixels. Knowing which
    files carry them is the difference between "this release is fine" and
    "this release can never be shown to somebody who does not want subtitles".

HOW IT IS DETECTED, AND WHY IT IS TWO STAGES
--------------------------------------------
Stage one is arithmetic and costs nothing: sample frames across the file,
count bright pixels low in the frame where a caption sits, and again in the
rest of the frame where a typeset sign would be. Twenty-four frames, a few
seconds per file, no OCR at all.

Stage two only runs on the frames stage one liked, and it is the one that
makes the answer trustworthy. A bright blob low in the frame is not a
subtitle - it is also a lens flare, a white shirt, or a lamp. So the best few
candidates are handed to Tesseract, and the file is only called hardsubbed if
real words come back.

WHY OCR IN ENGLISH IS THE RIGHT DISCRIMINATOR HERE
--------------------------------------------------
The hard case is telling a burned-in TRANSLATION from text that was always
part of the artwork - a shop sign, a newspaper, a title card. On this library
they are in different alphabets: the artwork of a 1987 Japanese show is
Japanese, and the fansub typesetting over it is English. Asking Tesseract for
Latin characters and requiring real words separates them almost for free. It
would separate them much less well on an English-language show, and this
module says so rather than pretending otherwise.

WHAT IT DOES NOT DO
-------------------
It does not read the subtitles. Transcribing them means OCR on every frame at
two frames a second - about ten minutes of GPU per episode against a few
seconds for this - and it is a separate decision for a separate day. What this
produces is a verdict and a cadence, which is all that is needed to stop
Bazarr, correct the counts, and decide whether transcription is worth it.
"""
from __future__ import annotations

import asyncio
import json
import threading
import os
import re
import subprocess
import time

from . import joblog
from .config import NO_WINDOW, SETTINGS, hidden_si
from .db import cursor

NONE, SIGNS, DIALOGUE, HYBRID = "none", "signs", "dialogue", "hybrid"

# How many frames one file is judged on. Twenty-four across a 24-minute episode
# is one a minute - dense enough that a dialogue track cannot hide, sparse
# enough that the whole thing is seek-bound rather than decode-bound.
SAMPLES = 24
# How many of them get shown to the OCR. Only the strongest candidates: the
# arithmetic is there to pick which frames are worth a second of Tesseract.
CONFIRM = 4
# The caption band. Measured against the City Hunter frames, where the yellow
# dialogue sits about four fifths of the way down.
LOW_BAND = "crop=iw:ih/3:0:ih*2/3"
HIGH_BAND = "crop=iw:ih*2/3:0:0"
# Counting is done on a small grey copy - text survives the downscale, noise
# does not.
W, H = 320, 60
BRIGHT = 210
# A caption is a few hundred bright pixels at this size. Fewer is noise; far
# more is a white wall or a flash frame, which is not text either.
MIN_PX, MAX_PX = 120, 6000
# What fraction of sampled frames must carry a caption before it is dialogue
# rather than the occasional sign. subocr's own thresholds make the same call
# on cue counts; this is the frame-sampling equivalent.
DIALOGUE_RATIO = 0.20
# Real words, not two stray glyphs.
MIN_CHARS = 6

# WHICH READER PRODUCED A VERDICT.
#
# Bumped whenever the reading rules change in a way that could alter an
# answer, so the sweep can go back for rows that predate the change. Without
# it a row written once is never revisited - _candidates only picks files with
# no row at all - and every improvement to the reader applies only to files
# nobody had read yet.
#
#   1  the original: a fixed MIN_PX caption floor
#   2  the floor is calibrated per file from frames the OCR read speech out
#      of, and one frame under the floor is read on purpose to find a thin
#      font. 3,691 rows said 'none' under rev 1, Velvet's among them.
#   3  that probe took the BRIGHTEST frame under the floor, so the floor was
#      derived from the top of the caption band and half of each file's
#      captions fell under it - Velvet S01E04 counted 4 of 24 on an episode
#      of solid dialogue.
#   4  and its guard meant it never ran at all. The reader now walks DOWN
#      from the dimmest confirmed caption, one probe at a time, until a
#      probe comes back as noise - which is where the caption band ends.
#   5  reading more words made the credit-roll veto too strict: any single
#      credit word voided the read, and "assistant" is one. It is a
#      fraction now, like the junk test beside it.
#   6  the OCR is whichever engine the install is set to. It was Tesseract,
#      hardcoded, while every library was set to PaddleOCR - and Paddle
#      refuses noise Tesseract invents, so the verdicts can differ.
READER_REV = 6

# BELOW THIS A BAND IS BLANK, whatever the font. MIN_PX is a guess about how
# many pixels a CAPTION makes; this is the far weaker claim that something is
# there at all, and it exists so the OCR has candidates to calibrate from on a
# file whose captions sit under MIN_PX. Measured on Velvet S01E47: blank bands
# read 0 and captions read 60-100, with one frame at 19 between them.
CAL_FLOOR_PX = 25
# Once the OCR has confirmed which frames really carry captions, the counting
# floor is set from the DIMMEST of them, with this much slack for a frame
# holding one short line rather than two long ones.
CAL_SLACK = 0.70

# WHAT THE FIRST SWEEP TAUGHT, IN ONE PASS OVER EIGHT FILES.
#
# The detector found burned-in TEXT correctly and then called all of it
# subtitles, which it is not. Three of the first three findings:
#
#   Big Bang Theory S11E10  aed, hernandez, produces, supervising, tara
#   Big Bang Theory S11E06  uce, wee
#   9-1-1 S03E15            later, months, paso, rie, texas, three
#
# An end-credit roll, OCR noise, and a location card - "EL PASO, TEXAS, THREE
# MONTHS LATER". All genuinely painted into the picture, none of them a
# subtitle. Sampling the middle 80% was supposed to dodge the credits and does
# not, because a 20-minute sitcom runs them over the last act.
#
# The signal that separates them is vocabulary, and it is a strong one.
# Dialogue is made of function words - you, what, but, that, this - and it is
# almost impossible to write three lines of speech without one. Credits are
# role nouns and proper nouns; location cards are place names and numbers.
# Neither contains "you".
_FUNCTION = {
    "the", "you", "your", "yours", "what", "who", "why", "how", "this",
    "that", "these", "those", "and", "but", "not", "for", "with", "are",
    "was", "were", "have", "has", "had", "will", "would", "can", "could",
    "should", "did", "does", "don", "didn", "isn", "just", "know", "think",
    "want", "need", "get", "got", "there", "here", "she", "her", "him",
    "his", "they", "them", "we", "our", "all", "out", "about", "from",
    "come", "going", "let", "like", "now", "one", "only", "some", "then",
    "well", "when", "where", "yes", "yeah", "look", "tell", "say", "said",
}
# The other half: words that only ever appear in a credit roll.
_CREDITS = {
    "produced", "producer", "producers", "produces", "production",
    "supervising", "executive", "directed", "director", "written", "writer",
    "starring", "casting", "editor", "edited", "cinematography", "music",
    "composed", "based", "created", "creator", "developed", "teleplay",
    "story", "screenplay", "copyright", "reserved", "rights", "studios",
    "entertainment", "productions", "presents", "associate", "coordinator",
    "assistant", "unit", "supervisor",
}
# How many DISTINCT frames must yield real speech. One frame of text is a
# title card; a subtitle track puts words on screen again and again.
MIN_TEXT_FRAMES = 2

# HOW MANY FRAMES ARE PULLED AT ONCE, and why raising the files-per-pass alone
# was not the answer.
#
# One file costs forty-eight ffmpeg seeks - twenty-four timestamps, two bands
# each - and they were running one after another at about 19 seconds a file.
# 4,526 files at 19s is a day of wall clock however high the per-pass cap goes,
# because the cap was never the bottleneck: the seeks were, in single file.
#
# They are also almost entirely WAITING. audit.py found the same thing sampling
# this pool and wrote it down: "the probes are seek-bound rather than
# CPU-bound, and eight lanes took a run from ~70s to ~9s here". Six lanes here
# for the same reason, and one lower because this reads two bands per
# timestamp rather than one file per probe.
LANES = 6

# THE PACE, AND WHAT IT YIELDS TO.
#
# Twenty files every fifteen minutes is eighty an hour, which put 4,554 files
# three and a half days away - a backlog nobody would watch drain. The limit
# was never about protecting the disks, though: it was a guess standing in for
# a gate. So the gate does the work now and the cap is raised to match.
#
# Every file is preceded by the same busy check the rule audit uses - is
# somebody watching Plex, are the workers full - and the pass STOPS the moment
# it goes true. A sweep that yields inside a second is allowed to be greedy
# while nothing else wants the disks.
# HOW MANY DISMISSALS IN ONE SERIES BEFORE THE SERIES IS THE ANSWER. Two, not
# one: a single false positive is a file, and the same false positive twice in
# the same show is a property of how that show was encoded.
SERIES_IGNORES = 2
# And how many separate dismissals a word has to appear in before it is taken
# as OCR noise rather than language. Also two, for the same reason - one bad
# read can produce any word at all.
GARBAGE_MIN = 2

PER_RUN = 90
CYCLE_S = 300
SETTLE_S = 600
# The name this job answers to on the Schedules page, so it is counted and
# timed alongside every other recurring pass rather than being a thing that
# happens invisibly.
SCHED_KEY = "hardsub"

# WHAT A LONG JOB OWES THE PERSON WATCHING IT.
#
# "looking..." and a filename is not progress, it is proof of life. This one
# has 4,564 files to get through at roughly ten seconds each, so the only
# honest thing to show is how fast it is actually going and what that means
# for the pile - which is a number nobody can work out from a spinner.
#
# t0/rate/eta are per RUN; backlog_eta is the whole queue at the pace the last
# runs actually achieved, which is the figure that decides whether the cadence
# needs changing.
STATE: dict = {"running": False, "done": 0, "total": 0, "now": "",
               "last_run": 0.0, "found": 0, "last_error": "",
               "t0": 0.0, "last_took": 0.0, "last_checked": 0,
               "last_found": 0, "runs": 0, "secs_each": 0.0}

_READY = False


def init() -> None:
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hardsub(
                file_id   INTEGER PRIMARY KEY,
                path      TEXT,
                size      INTEGER,
                at        REAL,
                state     TEXT,
                low_hits  INTEGER,
                high_hits INTEGER,
                samples   INTEGER,
                words     TEXT,
                detail    TEXT,
                marked    INTEGER NOT NULL DEFAULT 0
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_hardsub_state "
                    "ON hardsub(state)")
        # WHOSE VERDICT IS THIS? The detector reads twenty-four frames and
        # guesses from what came back; a person watches the episode. When they
        # disagree the person is right, and the row has to remember that or
        # the next sweep quietly overwrites the correction with the same wrong
        # guess it made the first time.
        # See READER_REV. NULL means rev 1 - every row written before the
        # column existed came from the fixed-floor reader.
        try:
            cur.execute("ALTER TABLE hardsub ADD COLUMN rev INTEGER")
        except Exception:                                        # noqa: BLE001
            pass
        try:
            cur.execute("ALTER TABLE hardsub ADD COLUMN chosen TEXT")
        except Exception:                                        # noqa: BLE001
            pass
        # WHAT A DISMISSAL IS WORTH KEEPING. Not just "hide this row" - the
        # words that produced it, and which series it belongs to, because both
        # are the evidence for not making the same mistake again.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hardsub_ignored(
                file_id INTEGER PRIMARY KEY,
                path    TEXT,
                series  TEXT,
                state   TEXT,
                words   TEXT,
                at      REAL
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_hsig_series "
                    "ON hardsub_ignored(series)")
    _READY = True


def _ffmpeg() -> str:
    from .jobs import _ffmpeg_exe
    return _ffmpeg_exe()


def _bright(path: str, t: float, band: str) -> int:
    r"""Bright pixels in one band of one frame. -1 if it could not be read.

    Raw grey straight out of ffmpeg rather than a PNG on disk: the first
    version of this judged frames by the SIZE of the written PNG, which called
    a frame containing the word "Murder?" blank because a mostly-black image
    compresses small whatever is on it. Counting the pixels is the whole job;
    anything that only correlates with the pixels will eventually disagree
    with them.
    """
    cmd = [_ffmpeg(), "-hide_banner", "-v", "error", "-nostdin",
           "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"{band},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90,
                           creationflags=NO_WINDOW)
    except Exception:                                            # noqa: BLE001
        return -1
    b = r.stdout or b""
    if len(b) < W * H:
        return -1
    return sum(1 for v in b[:W * H] if v > BRIGHT)


def _bands(path: str, t: float) -> tuple:
    r"""Both bands of ONE frame, from ONE decode. (low, high), -1 on failure.

    THE FRAME GRAB IS THIS READER'S WHOLE COST, and it was being paid twice.
    _bright above takes a single band, so the sampler called it once for the
    caption band and once for the band above it - two ffmpeg processes, each
    seeking to the same timestamp and decoding the same picture, for two crops
    of that one picture. 24 frames, 48 decodes.

    Measured before this: of the CPU one file costs, 92% was ffmpeg grabbing
    frames and 8% was PaddleOCR reading them. Halving the decodes is most of
    what there is to win here.

    ONE OUTPUT STREAM, STACKED, on purpose: `split` feeds both crops, each is
    scaled and greyed exactly as it was alone, and vstack puts them in one
    rawvideo pipe - so this stays a plain subprocess.run with one stdout to
    read. Two pipes on Windows would be a second way to fail for a saving
    already banked by the single decode. Low band first, high band after it.
    """
    fc = (f"[0:v]split=2[a][b];"
          f"[a]{LOW_BAND},scale={W}:{H},format=gray[la];"
          f"[b]{HIGH_BAND},scale={W}:{H},format=gray[hb];"
          f"[la][hb]vstack=inputs=2[out]")
    cmd = [_ffmpeg(), "-hide_banner", "-v", "error", "-nostdin",
           "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-filter_complex", fc, "-map", "[out]",
           "-f", "rawvideo", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90,
                           creationflags=NO_WINDOW)
    except Exception:                                            # noqa: BLE001
        return (-1, -1)
    b = r.stdout or b""
    n = W * H
    if len(b) < n * 2:
        # A FAILURE HERE IS NOT A FACT ABOUT THE FILE. Fall back to the two
        # separate reads rather than recording a blank: this module treats an
        # unreadable frame as "nothing in the picture", which is a verdict.
        return (_bright(path, t, LOW_BAND), _bright(path, t, HIGH_BAND))
    return (sum(1 for v in b[:n] if v > BRIGHT),
            sum(1 for v in b[n:n * 2] if v > BRIGHT))


def _tesseract() -> str:
    try:
        from . import subocr
        exe = os.path.join(subocr.tesseract_dir(), "tesseract.exe")
        return exe if os.path.exists(exe) else ""
    except Exception:                                            # noqa: BLE001
        return ""


_WORD = re.compile(r"[A-Za-z]{3,}")


# ---- THE OCR, WHICHEVER ONE THE INSTALL IS SET TO ------------------------
#
# This module used to call tesseract.exe and nothing else, while the settings
# page offered "one choice for the whole install" and every library was set to
# PaddleOCR. Two engines in one system because one of them was hardcoded.
#
# THE WORKER IS KEPT ALIVE. Measured: Paddle reads a frame in 293 ms after 7.7
# seconds of model load, against Tesseract's 530 ms and no load at all. This
# reader takes four to seven frames of a file and moves on, and the walk down
# the caption band picks each frame from what the last one read - so the
# frames are not known in advance and cannot be batched. Spawning per frame
# would pay the load twenty-five times over the work. One process, opened on
# the first frame of a pass and closed when the reader goes quiet.
_OCR = {"proc": None, "engine": "", "device": "", "at": 0.0}
_OCR_LOCK = threading.Lock()
# Closed after this long unused, so the GPU goes back to Whisper and the
# encoders between passes. The same bargain audiolang.unload makes.
OCR_IDLE_S = 300.0


def ocr_engine() -> str:
    """Which OCR this install uses. The same answer subocr gives."""
    try:
        from . import subocr
        return subocr.engine("")
    except Exception:                                            # noqa: BLE001
        return "tesseract"


def _ocr_close() -> None:
    p = _OCR.get("proc")
    _OCR.update(proc=None, engine="", device="", at=0.0)
    if not p:
        return
    try:
        if p.stdin:
            p.stdin.write("QUIT\n")
            p.stdin.flush()
    except Exception:                                            # noqa: BLE001
        pass
    try:
        p.wait(timeout=5)
    except Exception:                                            # noqa: BLE001
        try:
            p.kill()
        except Exception:                                        # noqa: BLE001
            pass


def ocr_close() -> None:
    """Let the engine go. Called when the reader's pass ends."""
    with _OCR_LOCK:
        _ocr_close()


def _ocr_proc(engine: str):
    """The serving worker for this engine, started if it is not up."""
    import sys as _sys
    # A RUNNING WORKER IS ENGINE **AND** DEVICE. It holds one model loaded
    # onto one piece of silicon; switching the setting has to start a new one
    # or the change is invisible until the idle timer happens to close it.
    try:
        from . import subocr as _so
        want_dev = _so.device() if engine == "paddle" else "cpu"
    except Exception:                                            # noqa: BLE001
        want_dev = "cpu"
    p = _OCR.get("proc")
    if (p is not None and p.poll() is None
            and _OCR.get("engine") == engine
            and _OCR.get("device") == want_dev):
        return p
    _ocr_close()
    # WHICH SILICON, ASKED ONCE. This was the first of two copies of
    # "gpu if cuda else cpu"; both are subocr.device() now, which puts the
    # install's setting in front of the detection.
    dev = "cpu"
    if engine == "paddle":
        try:
            from . import subocr
            dev = subocr.device()
        except Exception:                                        # noqa: BLE001
            dev = "cpu"
    env = dict(os.environ)
    try:
        from . import subocr
        env["NUARR_TESSERACT_DIR"] = subocr.tesseract_dir()
    except Exception:                                            # noqa: BLE001
        pass
    try:
        p = subprocess.Popen(
            [_sys.executable,
             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "paddle_worker.py"),
             "--serve", "--engine", engine, "--device", dev],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env,
            creationflags=NO_WINDOW, startupinfo=hidden_si())
        # It says hello once the model is up - so the first read waits for the
        # load instead of timing out in the middle of it.
        line = p.stdout.readline()
        if not line or not json.loads(line).get("ready"):
            raise RuntimeError("worker did not come up")
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"picture reader: {engine} would not start ({e}) - "
                   f"falling back to Tesseract", "warn", system="hardsub")
        _OCR.update(proc=None, engine="", at=0.0)
        return None
    _OCR.update(proc=p, engine=engine, device=dev, at=time.time())
    return p


def _read_served(engine: str, img: str) -> str:
    """One image through the kept-alive worker. '' if it could not answer."""
    with _OCR_LOCK:
        if _OCR.get("at") and time.time() - _OCR["at"] > OCR_IDLE_S:
            _ocr_close()
        p = _ocr_proc(engine)
        if p is None:
            return ""
        try:
            p.stdin.write(img + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
            _OCR["at"] = time.time()
            if not line:
                raise RuntimeError("worker closed")
            d = json.loads(line)
            return str(d.get("text") or "") if d.get("ok") else ""
        except Exception:                                        # noqa: BLE001
            _ocr_close()
            return ""


def _read_text(path: str, t: float, band: str) -> str:
    """OCR one band of one frame. Empty string when nothing readable."""
    engine = ocr_engine()
    exe = _tesseract()
    if engine != "paddle" and not exe:
        return ""
    tmp = os.path.join(os.environ.get("TEMP") or ".",
                       f"nuarr-hs-{os.getpid()}-{int(t)}.png")
    try:
        # Upscaled and hard-thresholded: Tesseract is far better on big clean
        # black-on-white than on a subtitle sitting over artwork.
        subprocess.run(
            [_ffmpeg(), "-hide_banner", "-v", "error", "-nostdin",
             "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
             "-vf", f"{band},scale=1280:-1,format=gray,"
                    f"lut=y='if(gt(val,{BRIGHT}),0,255)'",
             "-y", tmp], capture_output=True, timeout=90,
            creationflags=NO_WINDOW, startupinfo=hidden_si())
        if not os.path.exists(tmp):
            return ""
        if engine == "paddle":
            # Measured identical to Tesseract on the thresholded image and on
            # the raw colour crop, so the threshold above is kept - it is what
            # the brightness numbers are counted from and changing it would
            # make them mean something different.
            got = _read_served("paddle", tmp)
            if got:
                return got.strip()
            if not exe:
                return ""
            # Paddle could not answer and Tesseract is here: read it rather
            # than record a blank, which this module treats as a fact.
        r = subprocess.run([exe, tmp, "stdout", "-l", "eng", "--psm", "6"],
                           capture_output=True, text=True, timeout=120,
                           creationflags=NO_WINDOW, startupinfo=hidden_si())
        return (r.stdout or "").strip()
    except Exception:                                            # noqa: BLE001
        return ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _words(text: str) -> list:
    return _WORD.findall(text or "")


# ------------------------------------------------------------- the verdict --
def probe_one(file_id: int, samples: int = SAMPLES,
              confirm: int = CONFIRM, on=None) -> dict:
    r"""Does this file carry burned-in text, and of what kind.

    `on(text, pct)` is called as it goes, if given. The work is in three
    parts and they cost very different amounts - 48 ffmpeg decodes, then a
    handful of OCR calls, then a short walk down under the brightness floor -
    so the percentage is divided between them in that proportion rather than
    evenly. See the card in Processing System.
    """
    def _say(text, pct):
        if on:
            try:
                on(text, float(pct))
            except Exception:                                    # noqa: BLE001
                pass
    with cursor() as cur:
        row = cur.execute("SELECT id, path, duration, size, sub_langs "
                          "FROM files WHERE id=?", (int(file_id),)).fetchone()
    if not row:
        return {"ok": False, "why": "no such file"}
    row = dict(row)
    path, dur = row["path"] or "", float(row["duration"] or 0)
    if not path or not os.path.exists(path):
        return {"ok": False, "why": "not on disk"}
    if dur < 120:
        return {"ok": False, "why": "too short to sample meaningfully"}

    # THE MIDDLE 80%, because the ends are where the noise lives: a title card,
    # a production logo and an end-credit roll are all text, none of them a
    # subtitle, and all of them at a predictable place in the file.
    t0, t1 = dur * 0.10, dur * 0.90
    step = (t1 - t0) / max(1, samples - 1)
    marks = [t0 + step * i for i in range(samples)]

    # ACROSS LANES, NOT ONE AFTER ANOTHER. Each _bright is a subprocess that
    # spends its life waiting on a spinning disk, so the GIL is free the whole
    # time and the pool has twelve disks to answer from.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    low, high, read = [], [], 0
    with ThreadPoolExecutor(max_workers=LANES) as ex:
        # ONE JOB PER FRAME, NOT ONE PER BAND. Both crops come out of a
        # single decode - see _bands. This was 48 ffmpeg processes for 24
        # frames, each pair seeking to the same timestamp for two crops of
        # the same picture.
        futs = {ex.submit(_bands, path, t): t for t in marks}
        got: dict = {}
        # AS THEY FINISH, NOT IN THE ORDER THEY WERE SUBMITTED. Twelve lanes
        # answer in whatever order the disks feel like; waiting on the first
        # while eleven others are already done makes a progress bar stand
        # still and then jump. Same results, same cost, honest movement.
        done_n, total_n = 0, max(1, len(futs))
        for f in as_completed(futs):
            t = futs[f]
            try:
                lo, hi = f.result()
            except Exception:                                # noqa: BLE001
                lo, hi = -1, -1
            got[(t, LOW_BAND)] = lo
            got[(t, HIGH_BAND)] = hi
            done_n += 1
            _say(f"measuring frame {done_n} of {samples}",
                 55.0 * done_n / total_n)
    for t in marks:
        c = got.get((t, LOW_BAND), -1)
        if c < 0:
            continue
        read += 1
        low.append((t, c))
        h = got.get((t, HIGH_BAND), -1)
        high.append((t, h if h >= 0 else 0))
    if not read:
        return {"ok": False, "why": "could not decode any frame"}

    def hits(pairs):
        return [(t, c) for t, c in pairs if MIN_PX <= c <= MAX_PX]

    low_hit, high_hit = hits(low), hits(high)
    ratio = len(low_hit) / max(1, read)

    # STAGE TWO. The arithmetic has said where to look; this says whether it
    # was text. Best candidates first - a frame with more bright pixels in the
    # caption band is a frame with more caption on it.
    words: list = []
    text_frames = 0
    # THE BRIGHTEST FRAMES, WHETHER OR NOT THEY CLEARED MIN_PX.
    #
    # MIN_PX is a claim about how many pixels a caption makes, and it was
    # measured on one show. Velvet S01E47's captions read 60-100 against a
    # floor of 120, so only 2 of 60 frames were ever offered to the OCR - and
    # the rejected ones held "hope you can understand and that have your
    # support". The candidates are topped up from everything that is not
    # blank, so the OCR gets to see what this file's captions look like.
    #
    # Same number of Tesseract calls, and no extra decoding: every frame here
    # has already been measured.
    cand = [(t, c) for t, c in sorted(low_hit, key=lambda x: -x[1])]
    if len(cand) < max(0, confirm):
        seen_t = {t for t, _c in cand}
        spare = sorted(((t, c) for t, c in low
                        if t not in seen_t and c >= CAL_FLOOR_PX
                        and c <= MAX_PX),
                       key=lambda x: -x[1])
        cand += spare[:max(0, confirm) - len(cand)]
    lit: list = []                       # brightness of frames that held words
    _shown = cand[:max(0, confirm)]
    _eng = "PaddleOCR" if ocr_engine() == "paddle" else "Tesseract"
    for _i, (t, _c) in enumerate(_shown, 1):
        _say(f"reading frame {_i} of {len(_shown)} \u00b7 {_eng}",
             55.0 + 33.0 * (_i - 1) / max(1, len(_shown)))
        got = [w.lower() for w in _words(_read_text(path, t, LOW_BAND))
               if len(w) >= 3]
        # TWO REAL WORDS IN ONE FRAME, not two glyphs across four frames.
        # "uce, wee" cleared the old character-count bar and is not language.
        if len(got) >= 2:
            text_frames += 1
            words.extend(got)
            if _FUNCTION & set(got):
                lit.append(_c)           # a caption, and this is its weight
        if text_frames >= MIN_TEXT_FRAMES and len(set(words)) >= 5:
            break
    # ONE LOOK UNDER THE FLOOR, WHEN NOTHING DIM HAS BEEN SEEN YET.
    #
    # The candidates above are the brightest frames, so on a file where a few
    # frames DID clear MIN_PX the budget fills with those and the dim ones are
    # never read. Velvet S01E03: four bright frames over a lit scene, a floor
    # derived from them of 430, no recount, and the fifteen dim caption frames
    # never shown to the OCR. The calibration could only ever find a dim font
    # on a file where nothing at all was bright, which is not what an episode
    # looks like - captions sit over whatever the scene happens to be.
    #
    # So if the OCR has not yet seen anything dimmer than the floor, and there
    # are frames between blank and the floor, the brightest of them is read.
    # One extra call, and it is the only way to answer the question the
    # pre-filter was guessing at: are the dim frames captions, or noise.
    if True:
        # FROM THE MIDDLE OF THE BAND, NOT THE TOP OF IT.
        #
        # Everything else handed to the OCR is the brightest thing available -
        # that is what the pre-filter is for - so a floor derived from those
        # samples lands near the top of the caption band and half of the
        # file's captions fall under it. Measured on Velvet, swept at forty
        # frames: captions run from about 25 to about 80 bright pixels, and
        # the floor was coming out at 52 and 56.
        #
        #     S01E26  captions 25-81   floor 52.5   counted  8 of 24
        #     S01E04  captions 25-80   floor 56.0   counted  4 of 24  -> signs
        #
        # on episodes that are wall-to-wall dialogue. So the two probes are
        # taken at the median and the lower quartile of the non-blank frames:
        # the floor then comes from a caption near the BOTTOM of what this
        # file produces, which is what "the dimmest thing still worth counting
        # as a caption" means.
        # A WALK DOWNWARDS, NOT A SINGLE LOOK.
        #
        # Every frame the OCR is shown is the brightest one available, so the
        # dimmest CONFIRMED caption is only ever the dimmest of the bright
        # ones - and the floor derived from it sits in the middle of the
        # caption band, excluding the other half. Velvet S01E26's captions run
        # 25 to 81 bright pixels and the floor came out at 52.5; S01E04's
        # came out at 56 and counted 4 frames of 24 on an episode that is
        # solid dialogue.
        #
        # So: take the frames dimmer than the dimmest caption so far, probe
        # the middle of THAT, and if it reads as speech the floor moves down
        # and the question is asked again. The walk stops the moment a probe
        # comes back as noise, which is where this file's captions end.
        _say("looking under the brightness floor", 88.0)
        for _step in range(3):
            edge = min(lit) if lit else MIN_PX
            below = sorted(((t, c) for t, c in low
                            if CAL_FLOOR_PX <= c < edge), key=lambda x: x[1])
            if not below:
                break
            t, c = below[len(below) // 2]
            got = [w.lower() for w in _words(_read_text(path, t, LOW_BAND))
                   if len(w) >= 3]
            if len(got) < 2 or not (_FUNCTION & set(got)):
                break                      # noise down here - the band ends
            text_frames += 1
            words.extend(got)
            lit.append(c)

    # AND NOW COUNT THE FRAMES AGAIN, AGAINST WHAT WAS ACTUALLY SEEN.
    #
    # `lit` holds the brightness of the frames the OCR read a line of speech
    # out of. Those are captions, measured rather than assumed, so the dimmest
    # of them is what a caption weighs in THIS file - and every frame at least
    # that bright is one too. Without this the ratio still says 1/24, because
    # the ratio is what decides dialogue from signs and it was reading a
    # threshold rather than the picture.
    #
    # Only ever downwards, and only on the evidence of speech: a file with
    # nothing on screen produces no lit frames, nothing is recalibrated, and
    # the answer is the one it always gave.
    calibrated = 0.0
    if lit:
        # NOT CONDITIONED ON HOW MANY FRAMES CLEARED THE OLD FLOOR. The first
        # version only recalibrated when low_hit had already fallen below the
        # dialogue ratio, and Velvet S01E03 sat exactly on the boundary - 4 of
        # 24, 16.7%, called "signs" while the calibration that would have
        # found the other fifteen never ran because 4 is not less than 4.
        #
        # The question is about the FONT, and the OCR has answered it by
        # reading one. How many captions there are is the next question, and
        # it cannot be asked until this one is settled.
        calibrated = max(float(CAL_FLOOR_PX), min(lit) * CAL_SLACK)
        if calibrated < MIN_PX:
            low_hit = [(t, c) for t, c in low
                       if calibrated <= c <= MAX_PX]
            high_hit = [(t, c) for t, c in high
                        if calibrated <= c <= MAX_PX]
            ratio = len(low_hit) / max(1, read)

    uniq = sorted(set(words))
    speech = _FUNCTION & set(uniq)
    credits = _CREDITS & set(uniq)
    # WHAT HAS ALREADY BEEN THROWN AWAY. Both halves: this exact file, and the
    # show it belongs to once two of its episodes have been dismissed.
    junk = garbage_words() & set(uniq)
    seen_before = (int(file_id) in ignored_ids()
                   or _series_of(path) in ignored_series())
    # A read that is mostly words somebody has already rejected is that same
    # read again. Judged as a fraction rather than a flat count, because a long
    # correct read will always pick up one or two.
    mostly_junk = bool(uniq) and len(junk) >= max(2, len(uniq) * 0.5)
    # A CREDIT ROLL IS MOSTLY CREDIT WORDS, not one of them.
    #
    # This was `bool(credits)` - any overlap at all - and it was safe while a
    # read was four frames and a handful of words. The reader walks down the
    # caption band now and brings back thirty or more, so the chance of one
    # ordinary word landing in the list rose with every frame the evidence
    # improved by. Velvet S01E10: 15 of 24 frames carrying captions, a page of
    # dialogue, and the whole read voided because somebody said "assistant".
    #
    # A third rather than mostly_junk's half: "produced directed written
    # starring" arrives together and densely, so a real roll clears this
    # easily while a line of speech that happens to mention a producer does
    # not.
    credit_roll = bool(uniq) and len(credits) >= max(2, len(uniq) * 0.34)
    confirmed = (text_frames >= MIN_TEXT_FRAMES
                 and sum(len(w) for w in uniq) >= MIN_CHARS
                 and bool(speech) and not credit_roll
                 and not seen_before and not mostly_junk)

    if not confirmed:
        state = NONE
        if seen_before:
            why = ("you have marked this one - or two others in this series - "
                   "as not hardsubbed, so it is left alone")
        elif mostly_junk:
            why = (f"most of what was read ({', '.join(sorted(junk)[:4])}) "
                   f"matches words from findings you threw away, so this is "
                   f"the same bad read again rather than dialogue")
        elif credit_roll:
            why = (f"{len(credits)} of the {len(uniq)} words read are credit-roll "
                   f"words ({', '.join(sorted(credits)[:3])}) - a roll, not "
                   f"speech")
        elif uniq and not speech:
            why = (f"words came back ({', '.join(uniq[:5])}) but none of them "
                   f"are the ones speech is made of - so a title card, a "
                   f"location card or a sign rather than dialogue")
        elif text_frames < MIN_TEXT_FRAMES:
            why = (f"only {text_frames} frame(s) held readable words - a "
                   f"subtitle track puts them on screen again and again")
        else:
            why = "no readable words came back - artwork, not a caption"
        detail = (f"{len(low_hit)}/{read} frames had something bright low in "
                  f"the picture; {why}")
    elif ratio >= DIALOGUE_RATIO and len(high_hit) > read * 0.5:
        state = HYBRID
        detail = (f"words in {len(low_hit)}/{read} sampled frames and bright "
                  f"text higher up in {len(high_hit)} - dialogue plus signs")
    elif ratio >= DIALOGUE_RATIO:
        state = DIALOGUE
        detail = (f"words in {len(low_hit)}/{read} sampled frames "
                  f"({ratio*100:.0f}% of the running time) - a dialogue "
                  f"cadence, not the occasional sign")
    else:
        state = SIGNS
        detail = (f"words in only {len(low_hit)}/{read} sampled frames - too "
                  f"sparse for dialogue, so signs or song captions")
    if calibrated:
        detail = (f"{detail}; the caption floor was set to {calibrated:.0f} "
                  f"bright pixels from frames the OCR read speech out of, "
                  f"rather than the default {MIN_PX} - a thinner font than "
                  f"the one that number was measured on")
    return {"ok": True, "file_id": int(file_id), "path": path, "state": state,
            "low_hits": len(low_hit), "high_hits": len(high_hit),
            "calibrated": round(calibrated, 1),
            "samples": read, "ratio": round(ratio, 3),
            "words": uniq[:12], "detail": detail,
            "has_track": bool((row.get("sub_langs") or "").strip())}


def _save(d: dict) -> None:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            # A HAND-SET KIND OUTRANKS THE DETECTOR, ALWAYS. Not just this
            # once - for good. Erik looked at Detective Conan S14E19, which
            # the sampler called "signs or songs" at 58%, and it is dialogue
            # with signs over the top. Twenty-four frames landing on the sparse
            # parts of an episode is exactly how that mistake happens, and it
            # would happen again on the next pass with the same twenty-four.
            row = cur.execute("SELECT chosen, state FROM hardsub "
                              " WHERE file_id=?",
                              (int(d["file_id"]),)).fetchone()
            was = (row["state"] if row else None)
            if row and (row["chosen"] or ""):
                d = dict(d, state=row["chosen"])
            cur.execute(
                "INSERT INTO hardsub(file_id,path,size,at,state,low_hits,"
                "high_hits,samples,words,detail,rev) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, "
                "  size=excluded.size, at=excluded.at, state=excluded.state, "
                "  low_hits=excluded.low_hits, high_hits=excluded.high_hits, "
                "  samples=excluded.samples, words=excluded.words, "
                "  detail=excluded.detail, rev=excluded.rev",
                (int(d["file_id"]), d["path"], _size(d["path"]), time.time(),
                 d["state"], d["low_hits"], d["high_hits"], d["samples"],
                 ", ".join(d.get("words") or []), d["detail"][:400],
                 READER_REV))
    except Exception:                                            # noqa: BLE001
        return
    # AND THE FACTS ROW IS WHERE EVERYONE ELSE LOOKS FOR IT.
    #
    # hardsub owns its table and nothing else reads it directly: the planner,
    # the queue and the blocklist list all go through sub_facts, which keeps
    # its own copy of the picture verdict and is refreshed by a library-wide
    # sweep. So a reading finished here was invisible downstream until that
    # sweep happened to come round - hours, typically.
    #
    # Measured on the eight files still not being marked after the requeue
    # hook went in: three had no sub_facts row at all, read at 20:28 and never
    # scanned, so interesting() could not offer them and replan never saw
    # them; five had a row written before the picture dict carried the
    # scorer's inputs, so they scored without the cadence term and fell under
    # the line. Both are the same thing - the reading was complete and nothing
    # downstream could see it.
    #
    # ONE PROBE-CACHE READ AND ONE LISTDIR, immediately after this reader has
    # spent seven seconds pulling twenty-four frames out of that same file.
    # The directory could not be warmer, and the alternative is nuarr knowing
    # something and behaving as though it does not.
    try:
        from . import subscan
        subscan.scan_one(int(d["file_id"]))
    except Exception:                                            # noqa: BLE001
        pass
    # A NEW READING IS A NEW FACT, AND THE QUEUE HAS TO BE TOLD.
    #
    # The plan for a file is re-made when subplan.revision() changes, and that
    # stamp is built from the RULES - the mode, the two lines, the language
    # policy. So moving a line re-plans the whole library and learning
    # something new about one file re-plans nothing.
    #
    # Measured: 22 files sat past the 85% line, in auto, with a verdict of
    # 'dialogue' at up to 100% sure, and not one of them had a mark step
    # queued. Their plans had been made while the picture still read 'none'.
    # The queue was not refusing - it had never been asked.
    #
    # ONLY WHEN THE ANSWER ACTUALLY MOVED. The re-read sweep will write three
    # thousand rows that say exactly what they said before, and re-planning
    # those would be three thousand plans to reach three thousand identical
    # conclusions.
    try:
        if str(was or "") != str(d.get("state") or ""):
            from . import subqueue
            subqueue.requeue(int(d["file_id"]))
    except Exception:                                            # noqa: BLE001
        pass


def _series_of(path: str) -> str:
    """The show folder - two up from the file, which is the season's parent."""
    try:
        return os.path.basename(os.path.dirname(os.path.dirname(path))) or ""
    except Exception:                                            # noqa: BLE001
        return ""


def ignore(file_id: int, undo: bool = False) -> dict:
    r"""Say this finding is wrong, and keep why.

    THE ROW IS THE SMALLEST PART OF WHAT THIS DOES. Dismissing one Dexter's
    Laboratory episode helps once; the reason it was wrong - an OCR read that
    produced `bagel, cong, egle, ene, fli, gol, wie` and got past the
    function-word filter on the strength of "yes" - is true of every episode of
    that show and of every show encoded like it. So the words are kept and the
    series is counted, and both feed back into the next verdict.
    """
    if not _READY:
        init()
    with cursor() as cur:
        r = cur.execute("SELECT path, state, words FROM hardsub "
                        "WHERE file_id=?", (int(file_id),)).fetchone()
        if undo:
            cur.execute("DELETE FROM hardsub_ignored WHERE file_id=?",
                        (int(file_id),))
            return {"ok": True, "why": "no longer ignored"}
        if not r:
            return {"ok": False, "why": "no verdict recorded for that file"}
        cur.execute(
            "INSERT INTO hardsub_ignored(file_id,path,series,state,words,at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET "
            "  words=excluded.words, at=excluded.at",
            (int(file_id), r["path"], _series_of(r["path"] or ""),
             r["state"], r["words"] or "", time.time()))
    # A dismissal must count at once - the dictionaries and the series list
    # are memoised, and the next score must see what was just learned.
    _GARB["at"] = 0.0
    _SERIES["at"] = 0.0
    n = _series_count(_series_of(r["path"] or ""))
    return {"ok": True, "series": _series_of(r["path"] or ""),
            "series_ignores": n,
            "why": ("ignored" if n < SERIES_IGNORES else
                    f"ignored - and that is {n} in this series, so the whole "
                    f"show is now left alone")}


def _series_count(series: str) -> int:
    if not series:
        return 0
    try:
        with cursor() as cur:
            r = cur.execute("SELECT COUNT(*) n FROM hardsub_ignored "
                            "WHERE series=?", (series,)).fetchone()
        return int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        return 0


_GARB: dict = {"at": 0.0, "words": set()}
_GARB_TTL = 120.0


def forget(file_ids) -> int:
    r"""Throw away what was read from bytes that no longer exist.

    THE VERDICT BELONGED TO THE OLD FILE. An upgrade replaces the release
    under the same row: different encode, different subtitle tracks, possibly
    not hardsubbed at all. Keeping the reading would answer a question about a
    file nobody has any more.

    The dismissals are NOT touched. Those are about words and about shows -
    "this read is OCR noise", "leave this series alone" - and neither belongs
    to one file's bytes.
    """
    ids = [int(i) for i in (file_ids or []) if i]
    if not ids:
        return 0
    try:
        if not _READY:
            init()
        with cursor() as cur:
            qs = ",".join("?" * len(ids))
            cur.execute(f"DELETE FROM hardsub WHERE file_id IN ({qs})", ids)
            return cur.rowcount or 0
    except Exception:                                            # noqa: BLE001
        return 0


def garbage_words() -> set:
    r"""Words that have only ever appeared in findings somebody threw away.

    A NEGATIVE DICTIONARY, BUILT FROM BEING WRONG. There is no English word
    list here to check against - so the next best thing is the list of words
    this check has produced and been told were not language. A word seen in two
    separate dismissals and never in a finding that was kept is, on this
    library, noise.
    """
    now = time.time()
    if now - _GARB["at"] < _GARB_TTL:
        return _GARB["words"]
    bad: dict = {}
    keep: set = set()
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT words FROM hardsub_ignored"):
                for w in (r["words"] or "").split(","):
                    w = w.strip().lower()
                    if w:
                        bad[w] = bad.get(w, 0) + 1
            for r in cur.execute(
                    "SELECT h.words FROM hardsub h "
                    " WHERE h.state != ? AND NOT EXISTS "
                    "   (SELECT 1 FROM hardsub_ignored i "
                    "     WHERE i.file_id = h.file_id)", (NONE,)):
                for w in (r["words"] or "").split(","):
                    w = w.strip().lower()
                    if w:
                        keep.add(w)
    except Exception:                                            # noqa: BLE001
        return _GARB["words"]
    got = {w for w, n in bad.items() if n >= GARBAGE_MIN and w not in keep}
    _GARB.update(at=now, words=got)
    return got


KINDS = (DIALOGUE, HYBRID, SIGNS, NONE)
KIND_WORDS = {DIALOGUE: "dialogue", HYBRID: "dialogue + signs",
              SIGNS: "signs or songs", NONE: "no subtitles in the picture"}


def set_kind(file_id: int, kind: str) -> dict:
    r"""Record what a person says this file actually carries.

    THE CORRECTION IS WORTH MORE THAN THE ROW IT FIXES. The detector samples
    twenty-four frames; a person has seen the episode. Storing the answer in
    its own column rather than overwriting `state` keeps both - what was
    guessed and what is true - which is the pair a future tuning pass would
    need, and stops the next sweep undoing the correction.
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "why": f"{kind!r} is not a kind"}
    if not _READY:
        init()
    try:
        with cursor() as cur:
            r = cur.execute("SELECT state FROM hardsub WHERE file_id=?",
                            (int(file_id),)).fetchone()
            if not r:
                return {"ok": False, "why": "no finding for that file"}
            was = r["state"] or ""
            cur.execute("UPDATE hardsub SET chosen=?, state=? WHERE file_id=?",
                        (kind, kind, int(file_id)))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": str(e)[:160]}
    if was and was != kind:
        joblog.log(f"burned-in subtitles: file {file_id} read as "
                   f"{KIND_WORDS.get(was, was)} and set by hand to "
                   f"{KIND_WORDS.get(kind, kind)}", "info")
    return {"ok": True, "was": was, "kind": kind,
            "why": (f"set to {KIND_WORDS.get(kind, kind)}"
                    + (f" (read as {KIND_WORDS.get(was, was)})"
                       if was and was != kind else ""))}


# ------------------------------------------------- how sure is it, and why --
# A VERDICT WITH NO CONFIDENCE ATTACHED CANNOT BE AUTOMATED.
#
# The check already decided yes-or-no. That is enough to make a list for a
# person and not nearly enough to act without one: "Beet S01E16 read `and,
# himself, per, sometimes, this`" and "Guardians S03E11 read `bee, birxsctied,
# eee, ene, eweee`" are the same verdict, and one of them is obviously right
# while the other is obviously noise. What separates them is not the verdict,
# it is how much of the read looks like language.
#
# So the same evidence is scored 0-100, and the number decides what happens:
# high enough and it is marked without asking, low enough and it is thrown
# away without asking, and the band in the middle - where the evidence really
# is ambiguous - is the only thing a person is shown. Every decision made in
# that band feeds the dictionaries below, so the band narrows with use.
#
# SCORED ON READ, NOT AT PROBE TIME. Every input is already in the row, so a
# word learned tonight re-scores a finding from March without re-reading a
# single frame. A score frozen into the table at probe time would be a number
# that stopped learning the moment it was written.
GOOD_MIN = 2                    # confirmations before a word counts as real
_GOOD: dict = {"at": 0.0, "words": set()}
_GOOD_TTL = 120.0

_VOWELS = set("aeiouy")
_RUN3 = re.compile(r"(.)\1\1")             # three of the same letter
_CONS5 = re.compile(r"[bcdfghjklmnpqrstvwxz]{5}")


def _word_ok(w: str) -> bool:
    r"""Does this read like a word, or like OCR falling over?

    NO DICTIONARY, ON PURPOSE. Shipping an English word list would answer this
    for English and be wrong about every other library on the shelf - and
    these files are mostly anime, where the correct reading is full of names
    no word list contains. What is being tested is SHAPE, and the shapes below
    were taken from the reads this check has actually produced:
        aonfoodlipurntil, weinaventspent, sendfalbrattysbustergto  -> too long
        eee, eweee, sss                                            -> a letter
                                                                      three times
        birxsctied, tengeki? no - brxsct                           -> five
                                                                      consonants
    Everything the shape test gets wrong, the learned dictionaries then fix,
    which is the right division of labour: rules for what is always true,
    evidence for what is true here.
    """
    w = (w or "").strip().lower()
    if not (2 <= len(w) <= 13):
        return False
    if not any(c in _VOWELS for c in w):
        return False
    if _RUN3.search(w):
        return False
    if _CONS5.search(w):
        return False
    return True


def good_words() -> set:
    r"""Words from findings somebody CONFIRMED. The mirror of garbage_words().

    The negative dictionary has been here since the ignore button; this is the
    other half, and it is what makes marking a file teach something rather
    than only doing something. Same bar as its opposite: a word has to turn up
    in GOOD_MIN separate confirmations before it counts, so one mark cannot
    enshrine one OCR slip.
    """
    now = time.time()
    if now - _GOOD["at"] < _GOOD_TTL:
        return _GOOD["words"]
    seen: dict = {}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT words FROM hardsub WHERE marked=1"):
                for w in (r["words"] or "").split(","):
                    w = w.strip().lower()
                    if w:
                        seen[w] = seen.get(w, 0) + 1
    except Exception:                                            # noqa: BLE001
        return _GOOD["words"]
    got = {w for w, n in seen.items() if n >= GOOD_MIN}
    _GOOD.update(at=now, words=got)
    return got


def score_of(row) -> dict:
    r"""How sure this finding is, 0-100, and the sentence explaining it.

    Every term is evidence that is already in the row:

      language shape   up to 60   what share of the read looks like words
      speech           up to 25   function words - the strongest single signal
                                  there is, because "and/this/have" is what
                                  dialogue is made of and a sign never is
      cadence          up to 15   how much of the running time had text low in
                                  the picture; a subtitle track is relentless
                                  and a title card is not

    and then the three things that are not doubts but refusals:

      a credit roll            x0.35
      a show already dismissed x0.25
      words from dismissals    down to x0.4 by how much of the read they are
    """
    words = [w.strip().lower() for w in
             str((row["words"] if "words" in row.keys() else "") or "").split(",")
             if w.strip()]
    if not words:
        return {"score": 0, "why": "nothing readable came back"}
    uniq = set(words)
    good = good_words()
    junk = garbage_words() & uniq
    shaped = {w for w in uniq if _word_ok(w) or w in good}
    good_share = len(shaped - junk) / len(uniq)
    junk_share = len(junk) / len(uniq)
    speech = _FUNCTION & uniq
    credits = _CREDITS & uniq
    try:
        samples = int(row["samples"] or 0)
        lows = int(row["low_hits"] or 0)
    except Exception:                                            # noqa: BLE001
        samples = lows = 0
    ratio = (lows / samples) if samples else 0.0

    s = good_share * 60.0
    s += min(25.0, len(speech) * 8.0)
    s += min(15.0, ratio * 30.0)
    bits = [f"{good_share*100:.0f}% of the read is word-shaped"]
    if speech:
        bits.append(f"{len(speech)} function word"
                    + ("" if len(speech) == 1 else "s")
                    + f" ({', '.join(sorted(speech)[:3])})")
    else:
        bits.append("no function words, so a card or a sign rather than speech")
    if ratio:
        bits.append(f"text low in the picture in {ratio*100:.0f}% of samples")
    if credits:
        s *= 0.35
        bits.append(f"reads like a credit roll ({', '.join(sorted(credits)[:2])})")
    try:
        if _series_of(str(row["path"] or "")) in ignored_series():
            s *= 0.25
            bits.append("this show has already been dismissed twice")
    except Exception:                                            # noqa: BLE001
        pass
    if junk_share:
        s *= max(0.4, 1.0 - 0.6 * junk_share)
        bits.append(f"{junk_share*100:.0f}% of it is words from findings you "
                    f"threw away")
    return {"score": int(max(0, min(100, round(s)))), "why": "; ".join(bits)}


# ------------------------------------------------------- auto, and how sure --
# TWO THRESHOLDS, NOT ONE. A single line would say "everything above this is
# hardsubbed and everything below is not", which is the claim the evidence
# cannot support - the whole reason a person is being asked is that the middle
# is genuinely undecidable from a dozen OCR'd words. So there is a band, and
# what lands in it is the only thing that reaches the list.
def mode() -> str:
    m = str(getattr(SETTINGS, "hardsub_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


def mark_at() -> int:
    try:
        return max(50, min(100, int(getattr(SETTINGS, "hardsub_mark_at", 85))))
    except Exception:                                            # noqa: BLE001
        return 85


def dismiss_at() -> int:
    try:
        v = int(getattr(SETTINGS, "hardsub_dismiss_at", 30))
    except Exception:                                            # noqa: BLE001
        v = 30
    # Never allowed to meet the other one. A band of zero width is a single
    # threshold wearing two names, and nothing would ever be asked about.
    return max(0, min(mark_at() - 10, v))


def verdict_for(row) -> dict:
    """What auto WOULD do with this row, whether or not auto is on."""
    d = score_of(row)
    s = d["score"]
    try:
        state = str(row["state"] or "") if "state" in row.keys() else ""
    except Exception:                                            # noqa: BLE001
        state = ""
    if s >= mark_at() and state == SIGNS:
        # SIGNS ARE NOT A REASON TO MARK. The marker track tells Bazarr and
        # Plex that this file's subtitles are in the picture and need no
        # other; a burned-in sign or song over a file that still wants a
        # dialogue track is the opposite of that. Sure or not, a signs-only
        # reading is the person's call.
        d["auto"] = "ask"
        d["auto_why"] = (f"{s}% is past the mark line, but the picture reads "
                         f"as signs or songs only - whether that deserves the "
                         f"marker track is your call")
    elif s >= mark_at():
        d["auto"] = "mark"
        d["auto_why"] = f"{s}% is at or above the {mark_at()}% mark line"
    elif s <= dismiss_at():
        d["auto"] = "dismiss"
        d["auto_why"] = f"{s}% is at or below the {dismiss_at()}% dismiss line"
    else:
        d["auto"] = "ask"
        d["auto_why"] = (f"{s}% sits between {dismiss_at()}% and {mark_at()}%, "
                         f"so this one is yours to call")
    return d


def ignored_ids() -> set:
    try:
        with cursor() as cur:
            return {r["file_id"] for r in
                    cur.execute("SELECT file_id FROM hardsub_ignored")}
    except Exception:                                            # noqa: BLE001
        return set()


_SERIES = {"at": 0.0, "set": set()}
_SERIES_TTL = 30.0


def ignored_series() -> set:
    """Shows dismissed often enough to leave alone. MEMOISED, because
    score_of() asks once per row and the list is scored 600 rows at a time on
    a two-second timer - that was 600 identical queries a poll."""
    now = time.time()
    if now - _SERIES["at"] < _SERIES_TTL:
        return _SERIES["set"]
    try:
        with cursor() as cur:
            got = {r["series"] for r in cur.execute(
                "SELECT series FROM hardsub_ignored WHERE series != '' "
                "GROUP BY series HAVING COUNT(*) >= ?", (SERIES_IGNORES,))}
    except Exception:                                            # noqa: BLE001
        return _SERIES["set"]
    _SERIES.update(at=now, set=got)
    return got


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# ------------------------------------------------------- the marker track ---
# A BLANK TRACK IS THE WHOLE ANSWER, AND IT NEEDS NO OCR.
#
# The point of marking these files is to stop Bazarr asking for a subtitle
# that is already there, and to stop Plex reporting the file as having none.
# Both of those questions are answered by the EXISTENCE of an English subtitle
# stream. Neither reads a word of it.
#
# So the track carries one cue, one second long, containing a zero-width space.
# Nothing is ever drawn over the words already painted into the picture -
# which is the failure a transcribed track would have, since the same lines
# would appear twice - and the transcription, which costs ten minutes of GPU an
# episode, is not needed for any of it.
#
# DEFAULT YES, FORCED NO - AND THE DIFFERENCE BETWEEN THEM IS MEASURED.
#
# Both flags were cleared at first, on the reasoning that nothing should pick a
# blank track. That was backwards. The words ARE in the picture; the viewer
# sees them whether or not a subtitle is selected, so English subtitles for
# this file are not optional, they are already on. What the flags decide is
# which track gets that slot - and leaving both off meant some OTHER track
# could take it and paint a second copy of the same lines over the first.
#
# So default=yes: this is the English subtitle for this file, and nothing else
# should be chosen over it. It draws a zero-width space, so "selected" costs
# the viewer nothing and the picture is unchanged.
#
# forced stays NO, and this is the part with a number behind it. `forced` is
# what makes Plex auto-select a track for accounts set to "Shown with foreign
# audio" - without the viewer asking for anything. This server has watched 4
# of 66 devices burn a plain SRT (BRAVIA BF1, Chrome, XboxOne, and the generic
# Smart TV app - all with Burn Subtitles set to Always rather than Automatic),
# and Plex for Android Mobile burns every subtitle type by design. Burning
# means re-encoding the whole video. So forced=yes would quietly transcode a
# 2160p episode, on five clients, to paint a track containing nothing, on a
# picture that already has the words.
#
# With forced off, those clients only transcode if somebody deliberately turns
# subtitles on - which is exactly what happens today, so nothing gets worse.
#
# The track NAME still says what it is, because a viewer who opens the picker
# and sees a blank subtitle selected deserves an explanation rather than a bug
# report.
MARK_NAME = "English (burned into the picture)"
_MARK_SRT = "1\r\n00:00:00,000 --> 00:00:01,000\r\n​\r\n\r\n"


def mark_one(file_id: int, kind: str = "") -> dict:
    """Give a hardsubbed file a blank English subtitle track.

    `kind` is what the person says it carries, set before the track is
    written so the row records the truth rather than the guess.
    """
    if kind:
        k = set_kind(int(file_id), kind)
        if not k.get("ok"):
            return k
    from . import fileops
    from .subembed import _mkvmerge, have_mkvmerge, _probe_langs, _lang_key
    with cursor() as cur:
        row = cur.execute("SELECT id, path, sub_langs FROM files WHERE id=?",
                          (int(file_id),)).fetchone()
    if not row:
        return {"ok": False, "why": "no such file"}
    row = dict(row)
    path = row["path"] or ""
    if not os.path.exists(path):
        return {"ok": False, "why": "not on disk"}
    if os.path.splitext(path)[1].lower() != ".mkv":
        return {"ok": False, "why": "only Matroska can carry the track"}
    # THE GUARD THAT MATTERS: never add a second English track. A file that
    # already has one does not need a marker - Bazarr is already satisfied.
    if any(_lang_key(x) == _lang_key("eng")
           for x in (row.get("sub_langs") or "").split(",") if x.strip()):
        return {"ok": False, "why": "this file already has an English "
                                    "subtitle track, so nothing would change"}
    if not have_mkvmerge():
        return {"ok": False, "why": "mkvmerge is not installed"}
    if fileops.is_locked(path):
        return {"ok": False, "why": "the file is in use"}

    # ONE DEFAULT SUBTITLE, OR NONE. The sweep only ever looks at files with
    # no subtitle track at all, so the marker is normally the only one there
    # is and claiming default is unambiguous. But this can be pressed on a row
    # the sweep found days ago, and a file can gain a track in between - at
    # which point setting default here would put TWO defaults in the subtitle
    # group and leave which one plays up to the client.
    #
    # Reading the existing track ids back to clear their flags would be the
    # thorough answer; declining the flag is the correct one. The marker's job
    # is to stop something being drawn over the picture, and a file that now
    # has a real subtitle track has a person's choice in it that this should
    # not overrule.
    others = [x for x in (row.get("sub_langs") or "").split(",") if x.strip()]
    make_default = "no" if others else "yes"

    # ON THE CACHE, NOT BESIDE THE SOURCE. See fileops.cache_temp - and this
    # one had the extra problem that the stray file it left in a season folder
    # was a .srt, which is exactly what Plex and Bazarr go looking for there.
    try:
        _need = os.path.getsize(path)
    except OSError:
        _need = 0
    ok_room, why_room = fileops.cache_room(_need)
    if not ok_room:
        return {"ok": False, "why": why_room}
    srt = fileops.cache_temp(".srt", "mark")
    tmp = fileops.cache_temp(".mkv", "mark")
    try:
        with open(srt, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(_MARK_SRT)
        cmd = [_mkvmerge(), "-o", tmp, path,
               "--language", "0:eng",
               "--track-name", f"0:{MARK_NAME}",
               "--default-track", f"0:{make_default}",
               "--forced-track", "0:no",
               srt]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                           creationflags=NO_WINDOW, startupinfo=hidden_si())
        if r.returncode >= 2 or not os.path.exists(tmp):
            why = (r.stderr or r.stdout or "mkvmerge failed").strip()[:300]
            return {"ok": False, "why": why}
        if _lang_key("eng") not in _probe_langs(tmp):
            return {"ok": False, "why": "the rebuilt file does not carry the "
                                        "marker - nothing was changed"}
        res = fileops.safe_replace(path, tmp)
        if not getattr(res, "ok", False):
            return {"ok": False,
                    "why": f"could not put the file in place: "
                           f"{getattr(res, 'why', '')}"}
    finally:
        for p in (srt, tmp):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
    try:
        with cursor() as cur:
            cur.execute("UPDATE hardsub SET marked=1 WHERE file_id=?",
                        (int(file_id),))
    except Exception:                                            # noqa: BLE001
        pass
    # A CONFIRMATION MUST COUNT AT ONCE, the same way a dismissal does.
    #
    # dismiss() has always reset the garbage dictionary and the ignored-series
    # list so the answer lands on every other finding immediately. Marking
    # feeds the OPPOSITE dictionary - good_words() - and nothing reset it, so
    # a confirmation sat in a two-minute cache while the panel went on scoring
    # every other row without it. The asymmetry was invisible and wrong: both
    # answers teach, so both have to take effect on the same breath.
    _GOOD["at"] = 0.0
    # AND TELL THE THINGS THAT CACHE WHAT IS IN THIS FILE. mkvmerge has just
    # stream-copied the whole container to add a track and safe_replace has
    # put it where the old one was, so every stream list anybody holds for
    # this path is now wrong. Plex is the one that matters here - a stale
    # stream list is exactly what makes a player offer a subtitle track that
    # is not there, or miss the one that is - and telling it is the POINT of
    # the marker rather than an afterthought to it. No rename: adding a
    # subtitle track cannot change what the arr calls the file.
    try:
        from . import notify
        notify.file_changed([int(file_id)],
                            why="nuarr added the burned-in marker track",
                            rename=False, system="hardsub")
    except Exception:                                            # noqa: BLE001
        pass
    joblog.log(f"marked {os.path.basename(path)} as carrying burned-in "
               f"subtitles - blank English track added"
               + (", set default so nothing is drawn over the words already "
                  "in the picture" if make_default == "yes"
                  else " (not set default - the file has other subtitle "
                       "tracks now)")
               + ", and Bazarr stops asking", "info")
    return {"ok": True, "path": path,
            "why": ("marked - default English, nothing drawn over the picture"
                    if make_default == "yes"
                    else "marked - left not-default, this file has other "
                         "subtitle tracks")}


# ------------------------------------------------------- doing a batch ------
# WHY A BATCH NEEDED WRITING RATHER THAN JUST WIRING UP THE EXISTING BUTTON.
#
# The findings arrive by SHOW, not by file. One release group encodes a whole
# series the same way, so a sweep that finds Beet the Vandel Buster S01E04
# finds forty-five of its siblings in the same pass. Answering those one row at
# a time is forty-six presses to say one thing, and the thing being said is
# identical every time.
#
# The two answers are not alike, though, and the code should not pretend they
# are:
#
#   NOT A SUBTITLE  is a verdict withdrawn. Nothing on disk changes, it is
#                   reversible from the ignored list, and it is one INSERT per
#                   file. So it happens inside the request and answers at once.
#
#   MARK IT         rewrites every one of those files. mkvmerge stream-copies
#                   the whole container to add one track - no re-encode, but
#                   minutes of disk per episode, and forty-six of them is an
#                   hour of the pool. So it runs behind the request, one file
#                   at a time, and asks the job gate before each one.
MARK_STATE: dict = {"running": False, "done": 0, "total": 0, "now": "",
                    "ok": 0, "failed": 0, "t0": 0.0, "finished": 0.0,
                    "yielded": "", "errors": []}


def ignore_many(file_ids) -> dict:
    """Withdraw a list of findings. Cheap enough to do in the request."""
    ids = []
    for x in (file_ids or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {"ok": False, "why": "nothing selected"}
    done = 0
    series: set = set()
    for fid in ids:
        r = ignore(fid)
        if r.get("ok"):
            done += 1
            if r.get("series"):
                series.add(r["series"])
    # THE LEARNING IS THE POINT, AND A BATCH IS WHERE IT PAYS. Two dismissals
    # in one series leave the whole show alone, so dismissing a season teaches
    # far more than the rows it clears - and saying so is what tells somebody
    # they will not be asked about that show again.
    quiet = sorted(s for s in series if _series_count(s) >= SERIES_IGNORES)
    why = f"dismissed {done} finding" + ("" if done == 1 else "s")
    if quiet:
        why += (f" - and {len(quiet)} show" + ("" if len(quiet) == 1 else "s")
                + " now left alone entirely")
    return {"ok": True, "done": done, "quiet": quiet, "why": why}


async def mark_many(file_ids, force: bool = False, kind: str = "") -> dict:
    """Add the marker track to a list of files, behind the request."""
    if MARK_STATE["running"]:
        return {"ok": False, "why": "a batch is already running"}
    ids = []
    for x in (file_ids or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {"ok": False, "why": "nothing selected"}
    MARK_STATE.update(running=True, done=0, total=len(ids), now="", ok=0,
                      failed=0, t0=time.time(), finished=0.0, yielded="",
                      errors=[])
    asyncio.get_running_loop().create_task(_mark_batch(ids, force, kind))
    return {"ok": True, "started": len(ids),
            "why": f"marking {len(ids)} file" + ("" if len(ids) == 1 else "s")}


async def _mark_batch(ids: list, force: bool, kind: str = "") -> None:
    ok = failed = 0
    try:
        for fid in ids:
            # THE GATE DECIDES, BEFORE EVERY FILE - the same rule the sweep
            # follows, and for the same reason: this runs for an hour, and
            # somebody pressing play in minute two should not wait it out.
            # What is already done stays done; the rest can be asked for again.
            if not force and await _too_busy():
                MARK_STATE["yielded"] = (
                    "stopped early - the pool is busy or somebody is "
                    "watching. What was marked stays marked; select the rest "
                    "again when it is quiet.")
                break
            try:
                with cursor() as cur:
                    r = cur.execute("SELECT path FROM files WHERE id=?",
                                    (fid,)).fetchone()
                MARK_STATE["now"] = os.path.basename(r["path"]) if r else str(fid)
            except Exception:                                    # noqa: BLE001
                MARK_STATE["now"] = str(fid)
            res = await asyncio.to_thread(mark_one, fid, kind)
            if res.get("ok"):
                ok += 1
            else:
                failed += 1
                # Kept per file rather than as one count, because "already has
                # an English track" and "the file is in use" want different
                # things done about them and a tally of 6 says neither.
                if len(MARK_STATE["errors"]) < 40:
                    MARK_STATE["errors"].append(
                        {"file_id": fid,
                         "name": MARK_STATE["now"],
                         "why": res.get("why") or "failed"})
            MARK_STATE.update(done=ok + failed, ok=ok, failed=failed)
    finally:
        MARK_STATE.update(running=False, now="", finished=time.time(),
                          done=ok + failed, ok=ok, failed=failed)
        if ok or failed:
            joblog.log(
                f"burned-in subtitles: marked {ok} file(s)"
                + (f", {failed} could not be marked" if failed else "")
                + (" - " + MARK_STATE["yielded"] if MARK_STATE["yielded"]
                   else ""),
                "warn" if failed else "ok")


def mark_progress() -> dict:
    d = dict(MARK_STATE)
    el = (time.time() - d["t0"]) if (d["running"] and d["t0"]) else 0.0
    rate = (d["done"] / el) if (el > 1 and d["done"]) else 0.0
    d["elapsed"] = round(el, 1)
    d["secs_each"] = round(1 / rate, 1) if rate else 0.0
    d["eta"] = round((d["total"] - d["done"]) / rate) if rate else 0
    # A finished batch is worth reading for a minute and then is noise.
    if not d["running"] and d["finished"] and time.time() - d["finished"] > 180:
        return {"running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
                "errors": [], "yielded": "", "elapsed": 0.0, "eta": 0,
                "secs_each": 0.0, "now": "", "finished": 0.0, "t0": 0.0}
    return d


def _auto_one(file_id: int) -> str:
    """Act on one finding if its score is past either line. -> what was done."""
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT file_id, path, state, low_hits, samples, words, marked "
                "  FROM hardsub WHERE file_id=?", (int(file_id),)).fetchone()
        if not r or r["marked"]:
            return ""
        d = verdict_for(r)
    except Exception:                                            # noqa: BLE001
        return ""
    if d["auto"] == "mark":
        res = mark_one(int(file_id))
        if res.get("ok"):
            joblog.log(f"burned-in subtitles: marked "
                       f"{os.path.basename(r['path'] or '')} on its own - "
                       f"{d['auto_why']} ({d['why']})", "info")
            return "mark"
        return ""
    if d["auto"] == "dismiss":
        res = ignore(int(file_id))
        if res.get("ok"):
            joblog.log(f"burned-in subtitles: threw away the finding for "
                       f"{os.path.basename(r['path'] or '')} - "
                       f"{d['auto_why']} ({d['why']})", "info")
            return "dismiss"
    return ""


# ------------------------------------------------------------- the sweep ----
def untested() -> int:
    r"""How many files the reader still has to look at.

    THE SAME PREDICATE _candidates USES, and it has to stay that way. This
    counted files with no row at all, which was the same question until
    _candidates learned to go back for rows written by an older reader. Then
    the panel said "every file looked at" over 3,280 files queued to be read
    again - a count of what had been TOUCHED being printed as a count of what
    was KNOWN, which is the fault the tile beside it was just fixed for.
    """
    if not _READY:
        init()
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) n FROM files f "
                "LEFT JOIN hardsub h ON h.file_id=f.id AND h.size=f.size "
                "WHERE f.state NOT IN ('deleted','duplicate') "
                "  AND COALESCE(f.sub_langs,'')='' "
                "  AND COALESCE(f.duration,0) > 120 "
                "  AND (h.file_id IS NULL "
                "       OR (COALESCE(h.rev,1) < ? "
                "           AND COALESCE(h.chosen,'') = '' "
                "           AND COALESCE(h.state,'') IN ('none','signs')))",
                (int(READER_REV),)).fetchone()
        return int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def _candidates(limit: int) -> list:
    r"""Files with no subtitle track that have not been looked at.

    ONLY THE ONES CLAIMING TO HAVE NONE. A file with a subtitle track already
    answers every question this module exists to answer - Bazarr is satisfied,
    the counts are right - so reading twenty-four frames of it would be work
    spent to learn nothing that changes anything.
    """
    if not _READY:
        init()
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT f.id file_id, f.path, f.library, f.pool_disk FROM files f "
            "LEFT JOIN hardsub h ON h.file_id=f.id AND h.size=f.size "
            "WHERE f.state NOT IN ('deleted','duplicate') "
            "  AND COALESCE(f.sub_langs,'')='' "
            "  AND COALESCE(f.duration,0) > 120 "
            "  AND COALESCE(f.mtime,0) < ? "
            # NEVER READ, OR READ BY A READER THAT HAS SINCE BEEN CORRECTED.
            #
            # A row used to be final: this picked files with no row at all, so
            # every improvement to the reader reached only files nobody had
            # got to yet. The caption floor is calibrated now and 3,691 rows
            # say 'none' on the old fixed one - Velvet among them, with full
            # English sentences in frames it never showed the OCR.
            #
            # Only the two verdicts a lower floor can change. 'dialogue' and
            # 'hybrid' are already the strongest answers available and
            # re-reading them would be a disk seek to learn nothing, which is
            # the same reason files with a subtitle track are left alone. A
            # verdict you set by hand is never revisited at all.
            "  AND (h.file_id IS NULL "
            "       OR (COALESCE(h.rev,1) < ? "
            "           AND COALESCE(h.chosen,'') = '' "
            "           AND COALESCE(h.state,'') IN ('none','signs'))) "
            # eligible first - see precedence.py
            "ORDER BY (f.state='eligible') DESC, f.id LIMIT ?",
            (cutoff, int(READER_REV), int(limit)))]


async def _too_busy() -> bool:
    """The gate's opinion, not a second one. See audit._too_busy."""
    try:
        from .audit import _too_busy as busy
        return bool(await busy())
    except Exception:                                        # noqa: BLE001
        return False


async def sweep(limit: int = 0, force: bool = False) -> dict:
    r"""One pass. `force` is the button: a person who pressed it has already
    decided the disks can spare it, so it does not yield."""
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    todo = await asyncio.to_thread(_candidates, int(limit or PER_RUN))
    t0 = time.time()
    STATE.update(running=True, done=0, total=len(todo), now="", found=0,
                 t0=t0, yielded="", auto_marked=0, auto_dropped=0)
    done = found = 0
    auto_marked = auto_dropped = 0
    try:
        for r in todo:
            # THE GATE DECIDES, BEFORE EVERY FILE. Not once at the start: a
            # pass of ninety files runs for half an hour, and somebody
            # pressing play in minute two should not wait out the other
            # eighty-eight. Checked per file because a file costs twenty
            # seconds and the check costs nothing.
            if not force and await _too_busy():
                STATE["yielded"] = ("stopped early - the pool is busy or "
                                    "somebody is watching")
                break
            STATE["now"] = os.path.basename(r["path"])
            STATE["done"] = done
            d = await asyncio.to_thread(probe_one, r["file_id"])
            done += 1
            if not d.get("ok"):
                continue
            await asyncio.to_thread(_save, d)
            if d["state"] != NONE:
                found += 1
                STATE["found"] = found
                # AUTO ACTS ONLY WHERE THE EVIDENCE IS NOT IN DOUBT, and the
                # asymmetry is deliberate: dismissing is a row disappearing
                # and can be undone from the ignored list, while marking
                # rewrites a file. Both ends are still gated on the same
                # score, but the one with consequences sits behind a
                # threshold a person set on purpose.
                if mode() == "auto":
                    acted = await asyncio.to_thread(_auto_one, r["file_id"])
                    if acted == "mark":
                        auto_marked += 1
                        STATE["auto_marked"] = auto_marked
                    elif acted == "dismiss":
                        auto_dropped += 1
                        STATE["auto_dropped"] = auto_dropped
    finally:
        took = max(0.001, time.time() - t0)
        # SECONDS PER FILE, SMOOTHED ACROSS RUNS. One run of twenty files is a
        # small sample and a single 4K episode on a sleeping disk skews it, so
        # the figure the backlog estimate uses is the average of what has
        # happened rather than the last run alone.
        prev = STATE.get("secs_each") or 0.0
        this = took / max(1, done)
        STATE.update(running=False, now="", last_run=time.time(), done=done,
                     t0=0.0, last_took=took, last_checked=done,
                     last_found=found, runs=STATE.get("runs", 0) + 1,
                     secs_each=(this if not prev else prev * 0.7 + this * 0.3))
        try:
            from . import schedules
            schedules.beat(SCHED_KEY,
                           f"{done} checked, {found} carrying subtitles"
                           if done else "nothing left to look at")
        except Exception:                                    # noqa: BLE001
            pass
    if found:
        joblog.log(f"burned-in subtitle check: {found} of {done} file(s) that "
                   f"report no subtitles are carrying them in the picture"
                   + (f" - {auto_marked} marked automatically" if auto_marked
                      else "")
                   + (f", {auto_dropped} dismissed as noise" if auto_dropped
                      else ""),
                   "warn")
    return {"ok": True, "checked": done, "found": found,
            "auto_marked": auto_marked, "auto_dropped": auto_dropped,
            "yielded": STATE.get("yielded") or "",
            "remaining": await asyncio.to_thread(untested)}


def stats() -> dict:
    if not _READY:
        init()
    left = untested()
    now = time.time()
    # THIS RUN: measured, not guessed. Before the first file lands there is no
    # rate to report and the panel says so rather than dividing by zero and
    # showing an ETA of infinity.
    elapsed = (now - STATE["t0"]) if (STATE["running"] and STATE["t0"]) else 0.0
    rate = (STATE["done"] / elapsed) if (elapsed > 0.5 and STATE["done"]) else 0.0
    eta = ((STATE["total"] - STATE["done"]) / rate) if rate else 0.0
    each = STATE.get("secs_each") or 0.0
    out = {"running": STATE["running"], "now": STATE["now"],
           "yielded": STATE.get("yielded") or "",
           "ignored": len(ignored_ids()),
           "ignored_series": sorted(ignored_series())[:20],
           "garbage": len(garbage_words()),
           "done": STATE["done"], "total": STATE["total"],
           "last_run": STATE["last_run"], "untested": left,
           "elapsed": round(elapsed, 1), "rate": round(rate, 3),
           "eta": round(eta), "secs_each": round(each, 2),
           "last_took": round(STATE.get("last_took") or 0, 1),
           "last_checked": STATE.get("last_checked") or 0,
           "last_found": STATE.get("last_found") or 0,
           "runs": STATE.get("runs") or 0,
           "per_run": PER_RUN, "cycle_s": CYCLE_S,
           # The two lines and which side of them auto is allowed to act on.
           "mode": mode(), "mark_at": mark_at(), "dismiss_at": dismiss_at(),
           "auto_marked": STATE.get("auto_marked") or 0,
           "auto_dropped": STATE.get("auto_dropped") or 0,
           "learned_good": len(good_words()),
           # THE ONE THAT ACTUALLY DECIDES ANYTHING. Not "how long is this
           # batch" but "how long until the library is answered", at the pace
           # this is really going and the cadence it really runs on.
           # AT THE PACE IT REALLY GOES. A pass that yields to the gate
           # gets through fewer than PER_RUN files, so the cadence alone
           # overstates it; the measured seconds-per-file is what actually
           # bounds the answer once the cap is high enough not to be the
           # limit.
           "backlog_eta": round(max(
               (left / max(1, PER_RUN)) * CYCLE_S,
               left * each if each else 0)) if left else 0,
           "next_run": 0.0,
           # The engine the INSTALL is set to, not whichever one is on disk.
           "have_ocr": bool(_tesseract()) or ocr_engine() == "paddle",
           "ocr_engine": ocr_engine(), NONE: 0, SIGNS: 0,
           DIALOGUE: 0, HYBRID: 0, "marked": 0}
    try:
        from . import schedules
        for r in (schedules.snapshot() or {}).get("rows", []):
            if r.get("key") == SCHED_KEY:
                out["next_run"] = r.get("next_run") or 0.0
                out["runs"] = r.get("runs") or out["runs"]
                out["last_result"] = r.get("last_result") or ""
                break
    except Exception:                                        # noqa: BLE001
        pass
    try:
        with cursor() as cur:
            # COUNTED THE SAME WAY THE LIST IS BUILT. These counted every
            # row in the table, including files that have been deleted and
            # files whose bytes have changed - so the headline disagreed with
            # the list underneath it, which is the one number a person checks
            # the other against.
            live = ("  FROM hardsub h JOIN files f ON f.id = h.file_id "
                    " WHERE f.state NOT IN ('deleted','duplicate') "
                    "   AND (h.marked = 1 OR h.chosen IS NOT NULL "
                    "        OR h.size IS NULL OR f.size IS NULL "
                    "        OR h.size = f.size) ")
            for r in cur.execute("SELECT h.state, COUNT(*) n " + live
                                 + " GROUP BY h.state"):
                out[r["state"] or "none"] = r["n"]
            r = cur.execute("SELECT COUNT(*) n " + live
                            + " AND h.marked=1").fetchone()
            out["marked"] = int((r["n"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        pass
    # AND WHAT THE RUNNER KNOWS BETTER. STATE belongs to the button now; the
    # sweep that runs all day is the shared runner's. See idle.merge_stats.
    try:
        from . import idle as _idle
        out = _idle.merge_stats(KEY, out)
    except Exception:                                            # noqa: BLE001
        pass
    return out


def marker() -> dict:
    r"""How far the marker track has got, and what it is waiting on.

    WHY THIS IS NOT stats(). stats() is about the READER - how much of the
    library has been sampled, how many frames a second, when the next pass
    runs. This is about the WRITER: the blank English track that tells Bazarr
    and Plex the words are already in the picture. They are two different jobs
    with two different backlogs, and the reader finishing says nothing about
    whether anything was written.

    Erik: "I don't see the progress for this like last run eta how many files
    done how many left when it runs next". There was none to see. Marking had
    live progress only WHILE a batch was running - MARK_STATE, which is empty
    the other 99% of the time - so between batches the answer to "how many are
    waiting" was nowhere on the page.

    Everything here is counted the way stats() counts, against live files
    whose bytes still match what was read, so this panel and that one cannot
    disagree about the same library.
    """
    if not _READY:
        init()
    out = {"marked": 0, "waiting": 0, "auto_ready": 0, "needs_you": 0,
           "signs_only": 0, "not_mkv": 0, "has_eng": 0, "dismissed": 0,
           "total_found": 0,
           # ROWS THIS PANEL'S CLAIM DOES NOT COVER. Its population is
           # state IN ('dialogue','hybrid','signs'), so a file reading 'none'
           # is invisible here - not waiting, not ineligible, absent. With
           # nothing waiting it said "every one marked", which was true of the
           # files it believed carry burned-in subtitles and silent about the
           # ones it wrongly believed do not. Velvet was in the second group.
           "unreviewed": 0, "reader_rev": READER_REV,
           "mode": mode(), "mark_at": mark_at(), "dismiss_at": dismiss_at(),
           "last_at": 0.0, "cycle_s": CYCLE_S,
           "reader_left": 0, "reader_running": False,
           "secs_each": 0.0, "eta": 0, "running": False}
    live = ("  FROM hardsub h JOIN files f ON f.id = h.file_id "
            " WHERE f.state NOT IN ('deleted','duplicate') "
            "   AND (h.marked = 1 OR h.chosen IS NOT NULL "
            "        OR h.size IS NULL OR f.size IS NULL "
            "        OR h.size = f.size) ")
    ignored = ignored_ids()
    try:
        with cursor() as cur:
            r = cur.execute("SELECT COUNT(*) n " + live + " AND h.marked=1"
                            ).fetchone()
            out["marked"] = int((r["n"] if r else 0) or 0)
            r = cur.execute("SELECT MAX(h.at) m " + live + " AND h.marked=1"
                            ).fetchone()
            out["last_at"] = float((r["m"] if r else 0) or 0)
            # Read by a reader that has since been corrected - see READER_REV.
            r = cur.execute(
                "SELECT COUNT(*) n " + live +
                "   AND COALESCE(h.rev,1) < ? "
                "   AND COALESCE(h.chosen,'') = '' "
                "   AND COALESCE(h.state,'') IN ('none','signs')",
                (int(READER_REV),)).fetchone()
            out["unreviewed"] = int((r["n"] if r else 0) or 0)

            # THE BACKLOG, FILE BY FILE, because "waiting" is four different
            # answers and a single number hides which one you are looking at.
            # A file the marker can never write to is not waiting on anything
            # and must not be counted as though a run would clear it.
            rows = cur.execute(
                "SELECT h.file_id, h.state, h.chosen, h.low_hits, h.samples, "
                "       h.words, h.marked, f.path, f.sub_langs " + live
                + " AND COALESCE(h.marked,0)=0 "
                "   AND COALESCE(h.chosen, h.state) IN "
                "       ('dialogue','hybrid','signs')").fetchall()
        for r in rows:
            out["total_found"] += 1
            if int(r["file_id"]) in ignored:
                out["dismissed"] += 1
                continue
            st = str(r["chosen"] or r["state"] or "")
            path = r["path"] or ""
            # Two hard stops, checked here because mark_one() checks them too
            # and a panel that promises what the writer will refuse is worse
            # than no panel.
            if os.path.splitext(path)[1].lower() != ".mkv":
                out["not_mkv"] += 1
                continue
            from .subembed import _lang_key
            if any(_lang_key(x) == _lang_key("eng")
                   for x in (r["sub_langs"] or "").split(",") if x.strip()):
                out["has_eng"] += 1
                continue
            if st == SIGNS:
                out["signs_only"] += 1
                continue
            out["waiting"] += 1
            try:
                v = verdict_for(r)
            except Exception:                                    # noqa: BLE001
                v = {"auto": "ask"}
            if v.get("auto") == "mark":
                out["auto_ready"] += 1
            else:
                out["needs_you"] += 1
    except Exception:                                            # noqa: BLE001
        pass

    # "WHEN DOES IT RUN NEXT" HAS NO ANSWER, AND SAYING SO IS THE ANSWER.
    #
    # The first draft read a next_run out of schedules under the key "hardsub".
    # There is no such row: hardsub.watch() - which registers it - is never
    # started. The picture reader runs through readers.watch() on the shared
    # IDLE runner, which has no clock at all. It works whenever the pool is
    # quiet and nobody is watching, and stops the moment either stops being
    # true.
    #
    # So the panel would have shown a countdown to a time that means nothing,
    # computed from a row that does not exist, and it would have read 0 -
    # "not scheduled" - which is true by accident and for the wrong reason.
    # What is actually worth knowing is whether it is working now and how much
    # of the library it has still to look at, because THAT is what decides
    # when the next marker appears.
    try:
        from . import idle as _idle
        p = _idle.progress(KEY) or {}
        out["reader_running"] = bool(p.get("running"))
        out["reader_paused"] = bool(p.get("paused"))
    except Exception:                                            # noqa: BLE001
        pass
    try:
        out["reader_left"] = untested()
    except Exception:                                            # noqa: BLE001
        pass

    # A LIVE BATCH OVERRIDES ALL OF IT. mark_progress() already measures
    # done/total/eta while mark_many is running; this just carries it through
    # so the panel has one place to read instead of two.
    p = mark_progress()
    if p.get("running"):
        out.update(running=True, batch=p)
        out["eta"] = int(p.get("eta") or 0)
        out["secs_each"] = float(p.get("secs_each") or 0)
    else:
        # Between batches the honest estimate is the measured cost of the last
        # one. Roughly six seconds a file - it is a remux of the container,
        # not a re-encode - but measured beats remembered.
        each = float(p.get("secs_each") or 0) or 6.0
        out["secs_each"] = each
        out["eta"] = int(out["auto_ready"] * each) if out["auto_ready"] else 0
    return out


def found(limit: int = 60) -> list:
    """The findings, each with how sure it is and what auto would do.

    SORTED BY DOUBT, NOT BY DATE. The list used to be newest-first, which is
    the right order for a log and the wrong one for a queue of decisions: the
    rows worth a person's attention are the ones nearest the middle of the
    band, and those arrived in no particular order. Least certain first puts
    the genuinely hard calls at the top and the ones auto would have handled
    at the bottom.
    """
    if not _READY:
        init()
    try:
        with cursor() as cur:
            rows = [dict(r) for r in cur.execute(
                "SELECT h.file_id, h.path, h.state, h.low_hits, h.samples, "
                "       h.words, h.detail, h.marked, h.chosen, h.at, "
                "       f.library, f.title, f.season, f.episode, f.first_seen "
                "  FROM hardsub h JOIN files f ON f.id = h.file_id "
                " WHERE h.state != ? AND f.state NOT IN ('deleted','duplicate') "
                # THE SAME JOIN THE CANDIDATE PICKER USES. _candidates() has
                # always matched on size, so a file whose bytes changed is
                # already queued to be read again - this stops the OLD answer
                # being shown as a live question in the meantime.
                "   AND (h.marked = 1 OR h.chosen IS NOT NULL "
                "        OR h.size IS NULL OR f.size IS NULL "
                "        OR h.size = f.size) "
                "   AND NOT EXISTS (SELECT 1 FROM hardsub_ignored i "
                "                    WHERE i.file_id = h.file_id) "
                " ORDER BY h.marked ASC, h.at DESC LIMIT ?",
                (NONE, int(limit)))]
            # THE MARKED ONES RIDE ALONG WHATEVER THE LIMIT. They are sorted
            # last on purpose, so at 300 findings and a limit of 300 they were
            # the ones the LIMIT cut - and the "show 7 already marked" link
            # had nothing to show. Few enough to always carry.
            have = {r["file_id"] for r in rows}
            rows += [dict(r) for r in cur.execute(
                "SELECT h.file_id, h.path, h.state, h.low_hits, h.samples, "
                "       h.words, h.detail, h.marked, h.chosen, h.at, "
                "       f.library, f.title, f.season, f.episode, f.first_seen "
                "  FROM hardsub h JOIN files f ON f.id = h.file_id "
                # No size test here at all: marked=1 IS the decision, and
                # the act of marking is what moved the size.
                " WHERE h.marked = 1 AND f.state NOT IN ('deleted','duplicate') "
                " ORDER BY h.at DESC LIMIT 200") if r["file_id"] not in have]
    except Exception:                                            # noqa: BLE001
        return []
    lo, hi = dismiss_at(), mark_at()
    mid = (lo + hi) / 2.0
    for r in rows:
        r.update(verdict_for(r))
        # THE EPISODE, NOT THE SHOW. Twenty-six rows all reading "Detective
        # Conan" is a list you cannot act on: the filename is in the row but
        # it is 90 characters of release tags, and the part that identifies
        # the file is four of them.
        try:
            from .db import display_label
            r["label"] = display_label(r.get("title"), r.get("season"),
                                       r.get("episode"))
        except Exception:                                        # noqa: BLE001
            r["label"] = r.get("title") or ""
        r["kinds"] = [{"id": k, "word": KIND_WORDS[k]} for k in KINDS]
    rows.sort(key=lambda r: (bool(r.get("marked")),
                             abs(r.get("score", 0) - mid)))
    return rows


# --------------------------------------------------------- onto the runner --
# NINETY FILES AND THEN ASLEEP. The per-file gate check was already right -
# see sweep() - so all that has to go is the batch and the clock. The shared
# runner asks the same question before every file, works two at a time on
# different spindles, and steps around a disk somebody is reading from instead
# of stopping the pass on it.
#
# sweep() stays exactly as it is: it is the "check some now" button.
KEY = "hardsub"
TITLE = "Subtitles burned into the picture"


def _pending() -> list:
    """Every file that reports no subtitle track and has not been sampled."""
    try:
        return _candidates(100000)
    except Exception:                                            # noqa: BLE001
        return []


def _do_one(r: dict, report=None) -> dict:
    """Sample one file's frames, save the verdict, and act if auto says so."""
    d = probe_one(r["file_id"], on=report)
    if not d.get("ok"):
        return {"ok": False, "why": d.get("why") or "could not sample it"}
    if report:
        try:
            report("saving the verdict", 97.0)
        except Exception:                                        # noqa: BLE001
            pass
    _save(d)
    if d["state"] != NONE and mode() == "auto":
        _auto_one(r["file_id"])
    return {"ok": True, "state": d["state"]}


def prune() -> int:
    r"""Forget readings about files that are not there any more.

    The same prune subneed and audiolang got, for the same reason. A
    replacement gets a NEW files row, so a reading against the old one can
    never be marked, listed or re-read - it can only be counted. Measured
    when this went in: 290 of them, three of which turned up on the "past the
    line and not marked" list and could not be acted on because
    requeue() answered "no such file". They inflate "pictures sampled" and
    "pictures carrying dialogue" on the panel by exactly their number.

    Called at the end of each reader pass, where the rows are made, so the
    table cannot fill up with them again.
    """
    try:
        with cursor() as cur:
            cur.execute(
                "DELETE FROM hardsub WHERE file_id IN ("
                "  SELECT h.file_id FROM hardsub h "
                "    LEFT JOIN files f ON f.id = h.file_id "
                "   WHERE f.id IS NULL "
                "      OR f.state IN ('deleted','duplicate'))")
            return int(cur.rowcount or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def _after(d: dict) -> None:
    # The pass is over, so the model can go and the GPU is Whisper's and the
    # encoders' again until the next one.
    try:
        ocr_close()
    except Exception:                                            # noqa: BLE001
        pass
    try:
        prune()
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import schedules
        schedules.beat(SCHED_KEY,
                       f"{d.get('ok') or 0} checked" if d.get("done")
                       else "nothing left to look at")
    except Exception:                                            # noqa: BLE001
        pass


async def watch() -> None:
    from . import idle
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "Subtitles burned into the picture", "Subtitles",
            CYCLE_S,
            what=f"Samples {SAMPLES} frames of each file that reports having "
                 f"no subtitle track, counts the bright pixels low in the "
                 f"picture, and shows the best few to the OCR. Works "
                 f"continuously while the box is idle rather than in batches.")
    except Exception:                                        # noqa: BLE001
        pass
    await asyncio.sleep(240)
    await idle.run(KEY, TITLE, _pending, _do_one,
                   label=lambda r: os.path.basename(r.get("path") or "")[:120],
                   disk_of=lambda r: r.get("pool_disk") or "",
                   note_of=lambda r: f"sampling {SAMPLES} frames",
                   system_name="Subtitles in the picture",
                   goto="/settings#hardsub", on_pass=_after)
