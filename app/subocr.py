r"""Turn image subtitles into embedded text subtitles.

THE RULE
--------
Every English image subtitle that is not signs/songs gets OCR'd to SRT and
muxed in. The forced and default flags play no part in choosing; they are only
corrected afterwards.

WHY NOT THE TITLE ALONE - THE MEASUREMENT THAT DECIDED IT
---------------------------------------------------------
Titles cannot carry this decision. Across this library, English image subs by
title and cue count (NUMBER_OF_FRAMES, already in the probe):

    titled signs/songs        n=  300   median   65   p10  19   p90    181
    titled something else     n=3,677   median  664   p10 350   p90  2,085
    NO TITLE AT ALL           n=2,294   median  680   p10  78   p90  1,634

3,780 English image subs carry NO TITLE - the single largest group - so a
title regex is blind to most of the library. But the cue counts separate
cleanly: signs top out around 181, dialogue starts around 350. Hence the
threshold at 300, which sits in that gap, with the title regex kept as a
second way in for a dense track that is honestly labelled.

The bias is deliberate. Wrongly skipping a dialogue track leaves the file as
it is today - it still transcodes, and nothing is lost. Wrongly converting a
signs track embeds 65 lines of scattered captions as "English (OCR)" and
marks it default, which is actively worse than doing nothing. So borderline
cases skip.

WHY SRT AND NOT ASS
-------------------
ASS exists for positioning, styling and karaoke - exactly the signs category
excluded here. OCR output is plain timed text with no styling to preserve, so
ASS adds nothing and costs something: Plex hands SRT to clients as text for
client-side rendering, while complex ASS often forces server-side burn-in,
which is a transcode. ASS would partly recreate the problem this solves.

WHY EMBEDDED, AND WHY THE PGS SURVIVES
--------------------------------------
A sidecar is one arr rename away from being orphaned, so the SRT goes in the
file. The original PGS is KEPT but demoted to default=0/forced=0. Demoting is
what actually stops the transcode; deleting it would only save ~19 MB a file
(~105 GB across the library) and would be irreversible if OCR mangled a line.
"""
from __future__ import annotations

import json
import os
import time
import re
import shutil
import subprocess
import tempfile

from .config import NO_WINDOW, SETTINGS

# PGS ONLY, because pgsrip is PGS-rip. Including VOBSUB/DVB here was a bug
# caught on the first real batch: `-c:s copy` into a .sup container is invalid
# for them and ffmpeg refuses outright -
#     Not enough data, skipping 2672 bytes
#     Error submitting a packet to the muxer: Invalid data found
# and even had extraction worked, pgsrip cannot read VOBSUB. DVD subtitles
# need a different OCR tool entirely; they are out of scope rather than
# silently failing per file.
IMG_CODECS = {"hdmv_pgs_subtitle", "pgssub"}
# Recognised so they can be reported as skipped-with-a-reason rather than
# quietly ignored.
OTHER_IMG_CODECS = {"dvd_subtitle", "dvdsub", "dvb_subtitle"}
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
# LEARNED FROM THIS LIBRARY, not guessed. tools/sub_title_learn.py labels every
# image sub that has a cue count (sparse = signs, dense = dialogue) and scores
# title tokens by log-odds. On a 1,209-track holdout: precision 100%, recall
# 67%, and it showed the original hand-written pattern below caught only
# 276 of 565 titled signs tracks - 49%.
#
# What it found that the guess missed, with counts:
#
#     English (Foreign)        48      English (Forced)          27
#     English Forced [USBD]    26      English (Forced only)     17
#     English Subs Retail      23      Titles and Signs          15
#
# ONLY THE SEMANTIC ONES ARE ADOPTED. The learner also ranked `usbd`, `retail`,
# `official` and `vobsub` highly, but those are source and format markers that
# merely correlate with one distributor's habits - they say nothing about what
# a track CONTAINS, and encoding them would break on the next release group.
# `forced`, `foreign` and `titles` describe the content itself and travel.
#
# `forced` is also a correction to my own reasoning: I argued the forced flag
# was irrelevant to this decision. As a FLAG it largely is - but as a WORD in
# the title it scores signs=60 against dialogue=3, which means the old
# PowerShell's instinct about forced subs in anime was better founded than my
# dismissal of it.
SIGNS_RE = re.compile(
    r"sign|song|s\s*&\s*s|s\s*/\s*s|typeset|caption"
    r"|forced|foreign|\btitles\b", re.I)

# DENSITY, NOT TOTAL. Cues per minute separates the two far better than an
# absolute count, because an absolute count cannot tell a 22-minute cartoon
# from a two-hour film:
#
#     signs-titled      n=  390   median  1.3 cues/min   p10 0.1   p90  3.7
#     dialogue-titled   n=2,308   median 13.7 cues/min   p10 9.6   p90 17.3
#
# A gap from 3.7 to 9.6 is far wider than the absolute version's 181 to 350.
# The fixed floor was skipping real dialogue on short-form content - caught on
# 'The Batman' S02E05, an 'English (SDH)' track with 266 display sets over a
# 22-minute episode, which the absolute rule called signs and which is plainly
# dialogue.
SIGNS_MAX_CPM = 6.0

# Kept only for files whose duration cannot be read, where density cannot be
# computed at all. Same distribution as before: signs p90=181, dialogue p10=350.
SIGNS_MAX_CUES = 300

# Above this many CUES (not display sets), a track is dialogue and no other
# signal gets a vote. Deliberately far above anything signs can reach: the
# heaviest typeset anime signs tracks in this library sit in the low hundreds,
# while the misread films start at ~540 and run to 2,081. 500 leaves a wide gap
# on both sides rather than splitting a difference.
DIALOGUE_MIN_CUES = 500

# Titles that positively mean dialogue, and therefore override a sparse count.
# Learned, with evidence: `sdh` scored 0 signs against 393 dialogue, `dialogue`
# 1 against 344, `translation` 0 against 87. A track that says it is SDH and
# looks thin is a short episode, not a signs track.
DIALOGUE_RE = re.compile(r"\bsdh\b|dialog|\bfull\b|translation|complete", re.I)

# NUMBER_OF_FRAMES IS NOT A CUE COUNT. It counts PGS display sets, and a
# subtitle needs two of them - one to draw it, one to clear it. Measured on
# two real files:
#
#     Blindspot  frames 2010 -> OCR 953  = 2.11x
#     Boruto     frames  770 -> OCR 377  = 2.04x
#
# Comparing an OCR cue count directly against frames therefore reads a clean
# pass as ~50% and rejects it. Boruto was rejected exactly that way - "49%,
# OCR mostly failed" - when it had in fact recovered about 98% of its real
# cues. Frames are halved before any comparison.
FRAMES_PER_CUE = 2.0

# Of the cues actually expected, how many must survive OCR. Applied after the
# halving above, so this is a real proportion rather than an artefact.
#
# 0.55, down from 0.7 - which was set on two data points and then rejected 22
# real files in the first 750, all clustered at ~68% with 900+ recovered cues
# apiece. A track that dense, which has also passed the gibberish and letter
# checks, is a usable subtitle; the frames-per-cue ratio simply is not exactly
# 2.0 on every mux. What this guard exists for is the catastrophic case,
# measured at 12% - and 55% separates that from the healthy 94-98% cluster
# with room on both sides.
MIN_RECOVERY = 0.55
MIN_CUES = 40

TESSERACT_DIR = r"C:\Program Files\Tesseract-OCR"


def _tags(s: dict) -> dict:
    return s.get("tags") or {}


def _title(s: dict) -> str:
    return str(_tags(s).get("title") or "").strip()


def _english(s: dict) -> bool:
    """English, or unlabelled.

    Untagged tracks count as English deliberately: 3,780 of these carry no
    title and many carry no language either, and excluding them would skip
    most of the files that need this.
    """
    l = str(_tags(s).get("language") or "").strip().lower()
    return l.startswith("en") or l in ("", "und")


def cues(s: dict) -> int | None:
    """Cue count from the container, without decoding anything."""
    t = _tags(s)
    for k in ("NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng"):
        try:
            return int(t[k])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def duration_min(probe: dict) -> float | None:
    """Runtime in minutes, or None. Needed to judge cue density."""
    try:
        d = float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return None
    return d / 60.0 if d > 0 else None


def is_signs(s: dict, minutes: float | None = None) -> tuple[bool, str]:
    """Signs/songs by title OR by being too sparse to be dialogue.

    An unknown cue count is treated as signs - i.e. skipped. A track that
    does not say how big it is cannot be shown to be dialogue, and the whole
    bias of this module is that skipping is the safe error.
    """
    # SIZE BEATS EVERY OTHER SIGNAL, and is checked before them.
    #
    # Both tests below are inferences about content; this one is close to a
    # measurement. A signs/songs track is a handful of captions - John Wick's
    # real one is 37 cues. Nothing carrying hundreds of cues is signs, whatever
    # its title says and whatever its density works out to.
    #
    # Both other tests were getting this wrong on the same films:
    #
    #   density - John Wick's dialogue track is 705 cues over 122 min = 5.8
    #             cues/min, under the 6.0 floor, because the film is quiet. So
    #             is Blade Runner 2049, at exactly 6.0. Sparse dialogue is
    #             still dialogue.
    #   title   - mkvmerge writes descriptive names like "Subtitle (PGS),
    #             English, 1601 captions", and SIGNS_RE matches "caption".
    #             The word is in there because the track is BIG.
    #
    # Consequence when it goes wrong: the track is skipped as signs, on the
    # premise that signs get burned into the picture instead - and for an HDR
    # file nothing is ever burned, so no text version exists at all. Plex then
    # paints the pictures on the CPU at playback, which is a full re-encode of
    # a 4K HDR film. That is the most expensive thing this system exists to
    # prevent, and it was reachable through a rounding error on cues per minute.
    n_now = cues(s)
    if n_now is not None and (n_now / FRAMES_PER_CUE) >= DIALOGUE_MIN_CUES:
        return False, (f"{int(n_now / FRAMES_PER_CUE)} cues - far too many to be "
                       f"signs, whatever the title says")
    if SIGNS_RE.search(_title(s)):
        return True, f"title {_title(s)!r} reads as signs/songs"
    if DIALOGUE_RE.search(_title(s)):
        # An explicit dialogue name beats a thin count. Checked AFTER the signs
        # patterns so a title carrying both words still reads as signs.
        return False, f"title {_title(s)!r} states dialogue"
    n = cues(s)
    if n is None:
        # UNKNOWN IS NOT SIGNS. Treating it as such skipped 3,634 tracks -
        # more than it converted. NUMBER_OF_FRAMES is an mkvmerge statistics
        # tag, so anything muxed by another tool simply has no cue count, and
        # that says nothing about what the track contains.
        #
        # These pass the cheap filter and are decided for real after OCR,
        # where the cue count is a fact rather than a missing tag. See
        # validate(): an unconfirmed track producing fewer than
        # SIGNS_MAX_CUES cues is rejected there instead. Signs tracks are
        # small, so the wasted OCR is seconds, not minutes.
        return False, "cue count unknown - will be confirmed after OCR"
    if minutes and minutes > 0:
        cpm = (n / FRAMES_PER_CUE) / minutes
        if cpm < SIGNS_MAX_CPM:
            return True, (f"{cpm:.1f} cues/min over {minutes:.0f} min - below "
                          f"the {SIGNS_MAX_CPM} dialogue floor")
        return False, f"{n} display sets, {cpm:.1f} cues/min"
    # No duration: density cannot be computed, so fall back to the absolute
    # floor. Weaker, and the reason the floor is kept at all.
    if n < SIGNS_MAX_CUES:
        return True, (f"only {n} display sets and no duration to judge "
                      f"density - below the {SIGNS_MAX_CUES} floor")
    return False, f"{n} display sets (no duration available)"


def _is_commentary(s: dict) -> bool:
    """A commentary track, by the same test the rules engine uses."""
    try:
        from . import rules
        if not rules.CONFIG.get("dropCommentarySubs", False):
            return False
        return bool(rules.CONFIG["commentaryPattern"].search(_title(s)))
    except Exception:
        return False


def _never_burned(probe: dict) -> bool:
    """Will the encode rules refuse to paint signs into this file, ever?

    Read from rules.CONFIG rather than duplicated, so the two cannot drift:
    the only thing worse than this check being absent is it disagreeing with
    the rule it is meant to mirror. Imported lazily - subocr is imported by
    the job path that rules also feeds, and a module-level import is a cycle.
    """
    try:
        from . import rules
        C = rules.CONFIG
    except Exception:
        return False
    if not C.get("burnEnabled", True):
        return True
    if not C.get("burnOnHDR", False):
        for s in (probe.get("streams") or []):
            if s.get("codec_type") != "video":
                continue
            tr = str(s.get("color_transfer") or "").lower()
            if tr in ("smpte2084", "arib-std-b67"):
                return True
    return False


def select_targets(probe: dict) -> list[dict]:
    """Every English image sub worth converting, in stream order.

    Returns each stream annotated with `rel`, its index AMONG SUBTITLE
    STREAMS - which is what `ffmpeg -map 0:s:N` takes, and is NOT the same as
    stream.index. Passing the absolute index extracts the wrong track.
    """
    subs = [s for s in (probe.get("streams") or [])
            if s.get("codec_type") == "subtitle"]
    if not subs:
        return []

    # LOOP SAFETY, PER ROLE - not per file.
    #
    # This used to bail on the whole file the moment ANY English text sub
    # existed, which is right for "we already OCR'd the dialogue" and wrong for
    # everything else: a release shipping a plain English SRT beside an SDH PGS
    # left the SDH stranded as pictures forever, and SDH is the track you
    # actually want in a noisy room. Roles are converted independently, so each
    # one is compared only against text of its OWN role.
    def _role(s: dict) -> str:
        t = _title(s)
        if re.search(r"\bsdh\b|hearing.?impaired|\bcc\b", t, re.I):
            return "sdh"
        if SIGNS_RE.search(t) or (s.get("disposition") or {}).get("forced"):
            return "signs"
        return "dialogue"

    have_text = {_role(s) for s in subs
                 if (s.get("codec_name") or "").lower() in TEXT_CODECS
                 and _english(s)}

    mins = duration_min(probe)
    never_burned = _never_burned(probe)
    out = []
    for rel, s in enumerate(subs):
        if (s.get("codec_name") or "").lower() not in IMG_CODECS:
            continue
        if not _english(s):
            continue
        # NEVER OCR SOMETHING THE RULES ARE ABOUT TO DELETE.
        #
        # Commentary subtitles are dropped by the rules engine, the same way
        # commentary audio always has been. Reading one back costs a full OCR
        # pass - The Empire Strikes Back carries 4,162 and 3,740 display sets
        # of it - to produce a text track whose next act is to be removed.
        # Read from rules.CONFIG so the two rules cannot disagree about what
        # counts as commentary.
        if _is_commentary(s):
            continue
        role = _role(s)
        if role in have_text:
            continue                 # this role is already readable as text
        # Signs stay pictures: they are burned into the video by the encode
        # rules, and OCR of typeset signs is worthless anyway. SDH and full
        # dialogue are the ones worth reading back.
        #
        # UNLESS NOTHING IS EVER GOING TO BURN THEM. That sentence is a
        # premise, not a fact, and on an HDR file it is false: burnOnHDR is
        # off, deliberately, because painting subtitles into HDR means
        # re-encoding it and losing the metadata. So the signs track is skipped
        # here on the grounds that the encoder will handle it, and the encoder
        # never touches it. It stays pictures forever.
        #
        # Caught on John Wick: Chapter 2 - a 4K HDR film whose 'forced only'
        # PGS track Plex selects by name whatever its disposition says. With no
        # text equivalent to offer, Plex paints it on the CPU, which is a full
        # 2160p HDR re-encode. Converting it costs seconds of OCR on 37 cues.
        signs, why = is_signs(s, mins)
        if signs and not never_burned:
            continue
        if signs:
            why = (f"{why} - converted anyway because nothing will burn it "
                   f"into this file")
        t = dict(s)
        t["rel"] = rel
        t["cues"] = cues(s)
        t["minutes"] = mins
        t["why"] = why
        # Carried to validate(), which otherwise rejects a signs track for
        # being signs-sized - the exact thing that was asked for here.
        t["signs_ok"] = bool(signs)
        out.append(t)
    return out


def _env() -> dict:
    r"""PATH with Tesseract on it - the reason OCR never once worked.

    pgsrip finds tesseract through PATH, via pytesseract. Tesseract is
    installed at TESSERACT_DIR but has never been ON PATH here, so every
    attempt died with:

        <TesseractNotFoundError> tesseract is not installed or it's not in
        your PATH

    The PowerShell only checked that tesseract.exe EXISTS at -TesseractDir.
    It does. Existing and being findable are different things, and the check
    passed while the thing it checked for could not be reached.
    """
    env = dict(os.environ)
    if os.path.isdir(TESSERACT_DIR):
        env["PATH"] = TESSERACT_DIR + os.pathsep + env.get("PATH", "")
        td = os.path.join(TESSERACT_DIR, "tessdata")
        if os.path.isdir(td):
            env.setdefault("TESSDATA_PREFIX", td)
    return env


# The caller's window into whichever child is currently doing the work, so the
# job row can show real R/W rates the way a transcode does. Thread-LOCAL, not
# module-global: four workers run run_one() concurrently on separate threads,
# and a shared hook would attribute one job's ffmpeg to another job's row.
import threading
_TLS = threading.local()


def _run(args: list[str], timeout: float = 3600) -> tuple[int, str]:
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace",
                         creationflags=NO_WINDOW, env=_env())
    hook = getattr(_TLS, "on_child", None)
    if hook:
        try:
            hook(p)
        except Exception:
            pass
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return -1, f"timed out after {timeout}s"
    finally:
        if hook:
            try:
                hook(None)
            except Exception:
                pass
    return p.returncode, (err or "") + (out or "")


def _mkvmerge() -> str:
    return getattr(SETTINGS, "mkvmerge", None) or \
        r"C:\Program Files\MKVToolNix\mkvmerge.exe"


def mkv_track_ids(path: str) -> list[dict]:
    """mkvmerge's own view of the tracks.

    Asked for explicitly rather than assumed equal to ffprobe's stream
    indices. They usually agree, and "usually" is how the wrong track ends up
    flagged.
    """
    rc, out = _run([_mkvmerge(), "-J", path], timeout=120)
    try:
        return json.loads(out).get("tracks") or []
    except Exception:
        return []


def extract_sup(path: str, rel: int, work: str, tag: str) -> str:
    """Copy the image sub out to a .sup. No decode, no remux.

    The filename carries the language because pgsrip reads it from there
    rather than from an argument.
    """
    sup = os.path.join(work, f"{tag}.en.sup")
    rc, err = _run([SETTINGS.ffmpeg, "-y", "-v", "error", "-i", path,
                    "-map", f"0:s:{rel}", "-c:s", "copy", sup])
    if rc != 0 or not os.path.exists(sup) or os.path.getsize(sup) < 1024:
        raise RuntimeError(f"sup extraction failed: {err.strip()[:200]}")
    return sup


def _python() -> str:
    r"""An ABSOLUTE python, never a bare name.

    Carried from the PowerShell, where it was learned expensively: a service
    account's PATH is not yours, and the per-user Python under C:\Users may be
    unreadable to it.
    """
    for c in (r"C:\ProgramData\ForcedSub\Python\python.exe",
              r"C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe",
              r"C:\Python313\python.exe"):
        if os.path.exists(c):
            return c
    found = shutil.which("python") or shutil.which("py")
    if not found:
        raise RuntimeError("no usable python.exe for pgsrip")
    return found


# OCR throughput, measured on this library rather than assumed: an 80.6 MB
# .sup took ~800s and ~20 MB ones took ~80s, so ~0.1 MB of PGS per second.
# Used only to move the progress bar - never to time anything out.
OCR_MB_PER_S = 0.10


def ocr(sup: str, tick=None, base: float = 0.0, span: float = 1.0,
        who: str = "") -> str:
    """Run pgsrip, estimating progress from sup size while it works.

    pgsrip emits nothing machine-readable per cue, so the bar used to sit
    frozen for the whole OCR - 13 minutes at 16.3% on Kizumonogatari, which
    reads exactly like a hang and got reported as one. The estimate is
    labelled as such and capped at 95% of its span so it can never claim
    completion the subprocess has not delivered.
    """
    import threading

    srt = os.path.splitext(sup)[0] + ".srt"
    expect_s = max(10.0, os.path.getsize(sup) / 1048576 / OCR_MB_PER_S)
    stop = threading.Event()

    def _ticker():
        # Every 2s, not 5s. The dashboard interpolates between samples using
        # the rate it measured from the last two, and that rate decays to zero
        # within about three seconds of silence - so at a 5s cadence the bar
        # crept, stalled for two seconds, then jumped when the next sample
        # landed. This is a timestamp and a division; the cost of doing it
        # more often is nil.
        t0 = time.time()
        while not stop.wait(2.0):
            if tick:
                frac = min(0.95, (time.time() - t0) / expect_s)
                tick(base + span * frac,
                     f"OCR{who} ~{int(min(99, frac * 100))}% (est)")

    th = threading.Thread(target=_ticker, daemon=True)
    th.start()
    try:
        rc, err = _run([_python(), "-m", "pgsrip", "--language", "en",
                        "--force", sup])
    finally:
        stop.set()
        th.join(timeout=1)
    if not os.path.exists(srt):
        raise RuntimeError(f"OCR produced no srt (rc={rc}): {err.strip()[:300]}")
    return srt


def clean(text: str) -> str:
    """Repair only the OCR mistakes that are unambiguous.

    Tesseract reads capital I as ! constantly on subtitle fonts. Just the
    clear shapes are touched - a bare "!" as a word, and "!" leading an
    otherwise lower-case word - because real exclamation marks are common in
    dialogue and rewriting those would be worse than the error.
    """
    text = re.sub(r"(?<![\w!])!(?=[a-z']{1,})", "I", text)
    text = re.sub(r"(?<![\w!])!(?![\w!])", "I", text)
    return text


def validate(srt_path: str, expected: int | None,
             minutes: float | None = None,
             signs_ok: bool = False) -> tuple[bool, str, int]:
    """Plausible dialogue, or a failed pass?

    `signs_ok` says the caller ASKED for a signs track - see select_targets on
    a file nothing will ever burn. Every "too few cues" rule below exists to
    catch a signs track sneaking through as dialogue, and applying them to a
    track deliberately chosen for being signs rejects exactly what was wanted:
    John Wick's forced track OCR'd cleanly to 37 cues and was thrown away for
    having 37 cues. The OCR-quality checks still apply - the letters and junk
    tests below are about whether Tesseract worked, which is a real question
    either way.
    """
    try:
        with open(srt_path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError as e:
        return False, f"unreadable: {e}", 0
    got = len(re.findall(r"^\s*\d+\s*$", txt, re.M))
    # `expected` arrives as container FRAMES; halve it to get cues. See
    # FRAMES_PER_CUE - skipping this step rejects healthy conversions.
    want = int(expected / FRAMES_PER_CUE) if expected else None
    if got < (5 if signs_ok else MIN_CUES):
        return False, f"only {got} cues recovered", got
    if want is None and not signs_ok:
        # The deferred half of is_signs(): the container never said how many
        # cues this track had, and now that it has been read, it can be judged
        # for real. Rejected here rather than embedded as "English (OCR)" and
        # marked default, which is worse than leaving the file alone.
        if minutes and minutes > 0:
            cpm = got / minutes
            if cpm < SIGNS_MAX_CPM:
                return False, (f"{cpm:.1f} cues/min - below the "
                               f"{SIGNS_MAX_CPM} dialogue floor, "
                               f"treating as signs/songs"), got
        elif got < SIGNS_MAX_CUES / FRAMES_PER_CUE:
            return False, (f"{got} cues and no duration - below the "
                           f"{int(SIGNS_MAX_CUES/FRAMES_PER_CUE)} floor, "
                           f"treating as signs/songs"), got
    if want and got < want * MIN_RECOVERY:
        return False, (f"recovered {got} of about {want} cues "
                       f"({got/want:.0%}) - OCR mostly failed"), got
    body = re.sub(r"^\s*\d+\s*$|^.*-->.*$", "", txt, flags=re.M)
    if sum(c.isalpha() for c in body) < 200:
        return False, "almost no letters - OCR failed", got
    junk = sum(1 for c in body if not (c.isalnum() or c.isspace()
               or c in ".,!?'\"-:;()[]<>/&%#$*+=…—’‘“”"))
    if body and junk / len(body) > 0.15:
        return False, f"{junk/len(body):.0%} unrecognised characters", got
    return True, f"{got} cues", got


def embed(src: str, subs: list[tuple[str, str]], dst: str) -> None:
    """Mux the SRTs in, demote every image sub, and put text subs FIRST.

    One pass, not a merge followed by an mkvpropedit: a second tool touching
    the file is a second chance to leave it half-done.

    ORDER MATTERS AS MUCH AS THE FLAGS. Plex lists subtitles in physical track
    order, so leaving the PGS ahead of the SRT means the obvious pick - the
    first one - is the image track that forces a transcode. Demoting it stops
    Plex choosing it automatically but does nothing about a person choosing it
    by hand. --track-order puts the text tracks above the image ones so the
    top entry is the cheap one.

    This is why the SRT is not simply appended: mkvmerge writes tracks in
    command-line order by default, which would put every new track last.
    """
    src_tracks = mkv_track_ids(src)

    def is_image(t):
        c = str((t.get("properties") or {}).get("codec_id") or "").upper()
        return "PGS" in c or "VOBSUB" in c or "DVBSUB" in c

    args = [_mkvmerge(), "--quiet", "-o", dst]
    for t in src_tracks:
        tid = t["id"]
        p = t.get("properties") or {}
        if t.get("type") == "subtitles" and is_image(t):
            # Demoted, not dropped: this is what stops Plex choosing the image
            # track and transcoding, while keeping the original as a fallback.
            args += ["--default-track-flag", f"{tid}:no",
                     "--forced-display-flag", f"{tid}:no"]
        else:
            # EVERY OTHER TRACK KEEPS THE FLAGS IT ARRIVED WITH, stated
            # explicitly rather than left to mkvmerge's defaults.
            #
            # This was a real bug, not a precaution. A converted file came back
            # with its audio default flag CLEARED - three untouched files in
            # the same library all carried default=1, the converted one did
            # not. Nothing here ever set an audio flag; it was lost to the
            # muxer's own idea of what the defaults should be.
            #
            # Harmless on a single-audio file, because Plex picks the only
            # track regardless. NOT harmless on a multi-audio file: losing the
            # default silently changes which LANGUAGE is chosen, and that is
            # the kind of fault nobody notices until weeks later on a file
            # they were not watching.
            args += ["--default-track-flag",
                     f"{tid}:{'yes' if p.get('default_track') else 'no'}",
                     "--forced-display-flag",
                     f"{tid}:{'yes' if p.get('forced_track') else 'no'}"]
    args.append(src)
    for i, (srt, name) in enumerate(subs):
        args += ["--language", "0:eng", "--track-name", f"0:{name}",
                 "--forced-display-flag", "0:no",
                 # NOT DEFAULT. This was default=yes on the first track, and it
                 # was a real regression: a default subtitle is ALWAYS ON, so
                 # Plex burns it into the video on any transcode, forcing a
                 # full video re-encode on top of whatever else it was doing.
                 # Observed on two files - the 2160p HEVC one failed to start
                 # and the 1080p h264 one stuttered, severity tracking the
                 # re-encode cost exactly. Direct play was fine either way,
                 # because the client renders text subs itself.
                 #
                 # The goal was never "always show subtitles". It was "if a
                 # subtitle IS shown, make it text so it does not force a
                 # transcode". Demoting the PGS already delivers that; making
                 # the SRT default manufactured a worse transcode than the one
                 # being removed.
                 "--default-track-flag", "0:no",
                 srt]

    # Physical order: video, audio, NEW TEXT SUBS, then the image subs they
    # replace, then anything else. mkvmerge wants FILEID:TRACKID pairs; the
    # source is file 0 and each SRT is its own input, numbered from 1 in the
    # order they were appended above.
    order = []
    order += [f"0:{t['id']}" for t in src_tracks if t.get("type") == "video"]
    order += [f"0:{t['id']}" for t in src_tracks if t.get("type") == "audio"]
    order += [f"{i + 1}:0" for i in range(len(subs))]
    order += [f"0:{t['id']}" for t in src_tracks
              if t.get("type") == "subtitles"]
    seen = set(order)
    order += [f"0:{t['id']}" for t in src_tracks
              if f"0:{t['id']}" not in seen and t.get("type") not in
              ("video", "audio", "subtitles")]
    if order:
        args += ["--track-order", ",".join(order)]

    rc, err = _run([a for a in args])
    # mkvmerge returns 1 for warnings, which are routine (timestamp rounding).
    if rc > 1 or not os.path.exists(dst):
        raise RuntimeError(f"mkvmerge failed (rc={rc}): {err.strip()[:300]}")


def pending_dir(file_id: int, root: str | None = None) -> str:
    """Where prepared SRTs wait for whoever rewrites the file next."""
    base = root or getattr(SETTINGS, "cache_dir", ".")
    return os.path.join(base, "subs-pending", str(file_id))


def pending_for(file_id: int, root: str | None = None) -> list[dict]:
    """SRTs already OCR'd for this file and not yet muxed in.

    Read by the transcode path so an encode that is rewriting the file anyway
    can carry the subtitles in with it, instead of the file being rewritten a
    second time purely to add them.
    """
    d = pending_dir(file_id, root)
    man = os.path.join(d, "manifest.json")
    if not os.path.exists(man):
        return []
    try:
        with open(man, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return []
    out = []
    for it in items:
        if os.path.exists(it.get("srt") or ""):
            out.append(it)
    return out


def clear_pending(file_id: int, root: str | None = None) -> None:
    shutil.rmtree(pending_dir(file_id, root), ignore_errors=True)


def ffmpeg_sub_args(src_probe: dict, pend: list[dict],
                    first_input: int = 1) -> tuple[list[str], list[str]]:
    r"""ffmpeg inputs and mapping so an ENCODE can carry the SRTs itself.

    Returns (inputs, maps). The ordering rule matches the mkvmerge path: text
    subtitles are mapped BEFORE the image ones, because Plex lists subtitles in
    physical order and the first entry is what a person picks by hand. Image
    subs are carried through but stripped of default/forced.

    Kept here rather than in jobs.py so both rewrite paths - mkvmerge and
    ffmpeg - derive the same order and the same flags from one place. Two
    implementations of "which subtitle goes first" would drift.
    """
    inputs: list[str] = []
    maps: list[str] = []
    for i, it in enumerate(pend):
        inputs += ["-i", it["srt"]]
        maps += ["-map", f"{first_input + i}:0"]
    subs = [s for s in (src_probe.get("streams") or [])
            if s.get("codec_type") == "subtitle"]
    for rel, _s in enumerate(subs):
        maps += ["-map", f"0:s:{rel}"]
    # Disposition is 1-based over the OUTPUT subtitle streams: the new text
    # tracks come first, then the originals.
    for i in range(len(pend)):
        maps += [f"-disposition:s:{i}", "0"]
    for j in range(len(subs)):
        maps += [f"-disposition:s:{len(pend) + j}", "0"]
    for i, it in enumerate(pend):
        maps += [f"-metadata:s:s:{i}", f"title={it.get('name') or 'English (OCR)'}",
                 f"-metadata:s:s:{i}", "language=eng"]
    return inputs, maps


def produce(path: str, probe: dict, file_id: int, work_root: str | None = None,
            on_progress=None) -> dict:
    """OCR only. Writes SRTs to the pending area and mNothing else.

    Split out from run_one so the expensive half can happen once, up front,
    and the resulting subtitles can be picked up by whichever job next
    rewrites the file - an encode, a passthrough, or failing those the
    subtitle job itself. Rewriting a file twice to do one thing was the waste
    worth removing.
    """
    res = run_one(path, probe, work_root, on_progress, mux=False)
    if not res.get("ok"):
        return res
    d = pending_dir(file_id, work_root)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    items = []
    for srt, name in res["made"]:
        dst = os.path.join(d, os.path.basename(srt))
        shutil.copy2(srt, dst)
        items.append({"srt": dst, "name": name})
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(items, f)
    shutil.rmtree(res.get("work") or "", ignore_errors=True)
    return {"ok": True, "pending": items, "tracks": len(items),
            "notes": res.get("notes", [])}


def run_one(path: str, probe: dict, work_root: str | None = None,
            on_progress=None, mux: bool = True, on_child=None) -> dict:
    """Convert every eligible track and produce a new file. Replaces nothing.

    The caller commits, so this inherits the Plex gate, the disk pacing and
    the DrivePool-aware move rather than reimplementing them badly.
    """
    targets = select_targets(probe)
    if not targets:
        return {"ok": False, "why": "no English non-signs image sub to convert"}
    _TLS.on_child = on_child          # this thread's children only; see _TLS
    work = tempfile.mkdtemp(prefix="subocr_",
                            dir=work_root or getattr(SETTINGS, "cache_dir", None))
    made: list[tuple[str, str]] = []
    notes: list[str] = []

    # Progress is reported in coarse phases rather than not at all. OCR has no
    # usable inner percentage - pgsrip emits nothing per cue - so the honest
    # unit is "track N of M", and the bar moves once per track. A four-minute
    # job that never moves reads as a hang, which is the actual complaint.
    n = len(targets) or 1

    def _who(t: dict) -> str:
        """Which track this is, in words, for the progress line.

        "extracting track 1/2" is true and unhelpful on a file with two image
        subs - the reader wants to know WHICH, the same way the audio and
        subtitle plan lines now name their language. Prefers the track's own
        title, since a release that bothered to label one ('SDH', 'Signs')
        said something the language code cannot.
        """
        tags = t.get("tags") or {}
        title = (tags.get("title") or "").strip()
        lang = (tags.get("language") or "").strip().lower()
        if title:
            return f" ({title[:24]})"
        return f" ({lang})" if lang and lang != "und" else " (untagged)"

    def tick(frac: float, stage: str = ""):
        if on_progress:
            try:
                on_progress(max(0.0, min(1.0, frac)), stage)
            except Exception:
                pass

    tick(0.02, "extracting")
    try:
        for i, t in enumerate(targets):
            tag = f"t{t['rel']}"
            base = 0.05 + 0.75 * (i / n)
            span = 0.75 / n
            who = _who(t)
            try:
                tick(base, f"extracting track {i+1}/{n}{who}")
                sup = extract_sup(path, t["rel"], work, tag)
                tick(base + span * 0.1, f"OCR track {i+1}/{n}{who}")
                # ocr() moves the bar itself while it grinds - see its ticker.
                srt = ocr(sup, tick, base + span * 0.1, span * 0.8, who)
                tick(base + span * 0.9, f"OCR track {i+1}/{n}{who} done")
            except Exception as e:
                notes.append(f"rel {t['rel']}: {e}")
                continue
            with open(srt, encoding="utf-8", errors="replace") as f:
                txt = clean(f.read())
            with open(srt, "w", encoding="utf-8") as f:
                f.write(txt)
            ok, why, got = validate(srt, t.get("cues"), t.get("minutes"),
                                    signs_ok=bool(t.get("signs_ok")))
            if not ok:
                notes.append(f"rel {t['rel']} rejected: {why}")
                continue
            label = _title(t) or "English"
            made.append((srt, f"{label} (OCR)"))
            notes.append(f"rel {t['rel']} -> {why}")
        if not made:
            shutil.rmtree(work, ignore_errors=True)
            return {"ok": False, "why": "; ".join(notes) or "nothing converted"}
        if not mux:
            # Caller only wanted the subtitles. Whoever rewrites the file next
            # will carry them in, so no mux and no commit happens here.
            return {"ok": True, "made": made, "work": work, "notes": notes,
                    "tracks": len(made)}
        tick(0.82, "muxing")
        out = os.path.join(work, "out.mkv")
        embed(path, made, out)
        tick(0.90, "muxed")
        return {"ok": True, "out": out, "work": work, "notes": notes,
                "tracks": len(made),
                "size_before": os.path.getsize(path),
                "size_after": os.path.getsize(out)}
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        return {"ok": False, "why": f"{type(e).__name__}: {e}", "notes": notes}
