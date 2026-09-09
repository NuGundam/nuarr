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
import os
import re
import subprocess
import time

from . import joblog
from .config import NO_WINDOW, hidden_si
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

PER_RUN = 20
CYCLE_S = 900
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


def _tesseract() -> str:
    try:
        from . import subocr
        exe = os.path.join(subocr.tesseract_dir(), "tesseract.exe")
        return exe if os.path.exists(exe) else ""
    except Exception:                                            # noqa: BLE001
        return ""


_WORD = re.compile(r"[A-Za-z]{3,}")


def _read_text(path: str, t: float, band: str) -> str:
    """OCR one band of one frame. Empty string when nothing readable."""
    exe = _tesseract()
    if not exe:
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
              confirm: int = CONFIRM) -> dict:
    r"""Does this file carry burned-in text, and of what kind."""
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

    low, high, read = [], [], 0
    for t in marks:
        c = _bright(path, t, LOW_BAND)
        if c < 0:
            continue
        read += 1
        low.append((t, c))
        h = _bright(path, t, HIGH_BAND)
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
    for t, _c in sorted(low_hit, key=lambda x: -x[1])[:max(0, confirm)]:
        got = [w.lower() for w in _words(_read_text(path, t, LOW_BAND))
               if len(w) >= 3]
        # TWO REAL WORDS IN ONE FRAME, not two glyphs across four frames.
        # "uce, wee" cleared the old character-count bar and is not language.
        if len(got) >= 2:
            text_frames += 1
            words.extend(got)
        if text_frames >= MIN_TEXT_FRAMES and len(set(words)) >= 5:
            break
    uniq = sorted(set(words))
    speech = _FUNCTION & set(uniq)
    credits = _CREDITS & set(uniq)
    confirmed = (text_frames >= MIN_TEXT_FRAMES
                 and sum(len(w) for w in uniq) >= MIN_CHARS
                 and bool(speech) and not credits)

    if not confirmed:
        state = NONE
        if credits:
            why = (f"the words read are a credit roll "
                   f"({', '.join(sorted(credits)[:3])}), not speech")
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
    return {"ok": True, "file_id": int(file_id), "path": path, "state": state,
            "low_hits": len(low_hit), "high_hits": len(high_hit),
            "samples": read, "ratio": round(ratio, 3),
            "words": uniq[:12], "detail": detail,
            "has_track": bool((row.get("sub_langs") or "").strip())}


def _save(d: dict) -> None:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO hardsub(file_id,path,size,at,state,low_hits,"
                "high_hits,samples,words,detail) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, "
                "  size=excluded.size, at=excluded.at, state=excluded.state, "
                "  low_hits=excluded.low_hits, high_hits=excluded.high_hits, "
                "  samples=excluded.samples, words=excluded.words, "
                "  detail=excluded.detail",
                (int(d["file_id"]), d["path"], _size(d["path"]), time.time(),
                 d["state"], d["low_hits"], d["high_hits"], d["samples"],
                 ", ".join(d.get("words") or []), d["detail"][:400]))
    except Exception:                                            # noqa: BLE001
        pass


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
# default and forced are cleared so no client selects it on its own, and the
# track NAME says what it is, because a viewer who picks a blank subtitle
# deserves an explanation rather than a bug report.
MARK_NAME = "English (burned into the picture)"
_MARK_SRT = "1\r\n00:00:00,000 --> 00:00:01,000\r\n​\r\n\r\n"


def mark_one(file_id: int) -> dict:
    """Give a hardsubbed file a blank English subtitle track."""
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

    srt = os.path.join(os.path.dirname(path), f".nuarr-mark-{int(time.time())}.srt")
    tmp = os.path.join(os.path.dirname(path), f".nuarr-mark-{int(time.time())}.mkv")
    try:
        with open(srt, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(_MARK_SRT)
        cmd = [_mkvmerge(), "-o", tmp, path,
               "--language", "0:eng",
               "--track-name", f"0:{MARK_NAME}",
               "--default-track", "0:no", "--forced-track", "0:no",
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
    joblog.log(f"marked {os.path.basename(path)} as carrying burned-in "
               f"subtitles - blank English track added so Bazarr stops "
               f"asking", "info")
    return {"ok": True, "path": path}


# ------------------------------------------------------------- the sweep ----
def untested() -> int:
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
                "  AND h.file_id IS NULL").fetchone()
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
            "SELECT f.id file_id, f.path, f.library FROM files f "
            "LEFT JOIN hardsub h ON h.file_id=f.id AND h.size=f.size "
            "WHERE f.state NOT IN ('deleted','duplicate') "
            "  AND COALESCE(f.sub_langs,'')='' "
            "  AND COALESCE(f.duration,0) > 120 "
            "  AND COALESCE(f.mtime,0) < ? "
            "  AND h.file_id IS NULL "
            "ORDER BY f.id LIMIT ?", (cutoff, int(limit)))]


async def sweep(limit: int = 0) -> dict:
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    todo = await asyncio.to_thread(_candidates, int(limit or PER_RUN))
    t0 = time.time()
    STATE.update(running=True, done=0, total=len(todo), now="", found=0,
                 t0=t0)
    done = found = 0
    try:
        for r in todo:
            STATE["now"] = os.path.basename(r["path"])[:60]
            STATE["done"] = done
            d = await asyncio.to_thread(probe_one, r["file_id"])
            done += 1
            if not d.get("ok"):
                continue
            await asyncio.to_thread(_save, d)
            if d["state"] != NONE:
                found += 1
                STATE["found"] = found
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
                   f"report no subtitles are carrying them in the picture",
                   "warn")
    return {"ok": True, "checked": done, "found": found,
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
           "done": STATE["done"], "total": STATE["total"],
           "last_run": STATE["last_run"], "untested": left,
           "elapsed": round(elapsed, 1), "rate": round(rate, 3),
           "eta": round(eta), "secs_each": round(each, 2),
           "last_took": round(STATE.get("last_took") or 0, 1),
           "last_checked": STATE.get("last_checked") or 0,
           "last_found": STATE.get("last_found") or 0,
           "runs": STATE.get("runs") or 0,
           "per_run": PER_RUN, "cycle_s": CYCLE_S,
           # THE ONE THAT ACTUALLY DECIDES ANYTHING. Not "how long is this
           # batch" but "how long until the library is answered", at the pace
           # this is really going and the cadence it really runs on.
           "backlog_eta": round(
               (left / max(1, PER_RUN)) * CYCLE_S
               + (left * each if each else 0)) if left else 0,
           "next_run": 0.0,
           "have_ocr": bool(_tesseract()), NONE: 0, SIGNS: 0,
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
            for r in cur.execute("SELECT state, COUNT(*) n FROM hardsub "
                                 "GROUP BY state"):
                out[r["state"] or "none"] = r["n"]
            r = cur.execute("SELECT COUNT(*) n FROM hardsub "
                            "WHERE marked=1").fetchone()
            out["marked"] = int((r["n"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        pass
    return out


def found(limit: int = 60) -> list:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT h.file_id, h.path, h.state, h.low_hits, h.samples, "
                "       h.words, h.detail, h.marked, f.library "
                "  FROM hardsub h JOIN files f ON f.id = h.file_id "
                " WHERE h.state != ? AND f.state NOT IN ('deleted','duplicate') "
                " ORDER BY h.at DESC LIMIT ?", (NONE, int(limit)))]
    except Exception:                                            # noqa: BLE001
        return []


async def watch() -> None:
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "Subtitles burned into the picture", "Subtitles",
            CYCLE_S,
            what=f"Samples {SAMPLES} frames of each file that reports having "
                 f"no subtitle track, counts the bright pixels low in the "
                 f"picture, and shows the best few to the OCR. {PER_RUN} "
                 f"files a pass.")
    except Exception:                                        # noqa: BLE001
        pass
    await asyncio.sleep(240)
    while True:
        try:
            await sweep()
        except Exception as e:                                   # noqa: BLE001
            STATE["last_error"] = f"{type(e).__name__}: {e}"
        await asyncio.sleep(CYCLE_S)
