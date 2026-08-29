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


def tesseract_dir() -> str:
    """nuarr's own copy first, the system install second.

    DataDir\\tesseract is where the installer lands the bundled build and
    where a self-update can refresh it - the same arrangement as ffmpeg. The
    Program Files path is the dev box's hand-installed copy and any machine
    where someone installed Tesseract themselves.
    """
    from .config import DATA_DIR
    own = os.path.join(str(DATA_DIR), "tesseract")
    if os.path.exists(os.path.join(own, "tesseract.exe")):
        return own
    return TESSERACT_DIR


# ------------------------------------------------------- settings-driven ----
# Every threshold above is the MEASURED default; these accessors let the OCR
# settings page override them without touching the reasoning that set them.

def _s(key: str, default, library: str | None = None):
    r"""A setting, per library, falling back to the global default.

    PER LIBRARY BECAUSE THE ANSWER IS. An anime shelf is the case these
    thresholds were measured on - dense typeset signs, untitled tracks,
    dual-audio releases - and a live-action film library shares almost none
    of that. The overrides live in one dict keyed by library name so a
    library that has said nothing keeps following the global default rather
    than freezing a copy of it.
    """
    if library:
        per = getattr(SETTINGS, "subocr_libs", None) or {}
        lib = per.get(library) or {}
        if key in lib and lib[key] is not None:
            return lib[key]
    v = getattr(SETTINGS, key, None)
    return default if v is None else v


# ---------------------------------------------------- what each library HAS --
# Signs and forced switches must not be offered to a library that contains
# neither: an option that can never do anything reads as a decision, and the
# person is left wondering why it changes nothing. Counting them means
# walking the probes, which is the 188 MB parse the language page was
# rewritten to avoid - so it is done once and cached, off the request path.

_KINDS: dict = {"at": 0.0, "data": {}}
_KINDS_TTL = 900.0


def library_track_kinds(force: bool = False, blocking: bool = True) -> dict:
    """Per library: how many image, signs, forced and dialogue subs exist.

    STALE BEATS SLOW on the settings page. A cached answer a few minutes old
    is right for a decision about how to handle subtitles - the counts move
    when a scan imports files, not while somebody reads the page - so a warm
    cache is served immediately and refreshed on a thread behind it. Only the
    very first call on a cold start has nothing to serve.
    """
    now = time.time()
    if not force and _KINDS["data"]:
        if (now - _KINDS["at"]) < _KINDS_TTL:
            return dict(_KINDS["data"])
        # Stale: hand back what we have and refresh behind the request.
        if not blocking and not _KINDS.get("busy"):
            _KINDS["busy"] = True

            def _bg():
                try:
                    library_track_kinds(force=True)
                finally:
                    _KINDS["busy"] = False
            threading.Thread(target=_bg, name="subocr-kinds",
                             daemon=True).start()
            return dict(_KINDS["data"])
    out: dict = {}
    try:
        from .db import cursor
        with cursor() as cur:
            # THE JSON FILTER IS THE WHOLE OPTIMISATION. Parsing 39,000 probe
            # blobs to count picture subtitles took seconds and blocked the
            # page; only files that actually contain a PGS track can
            # contribute to any of these counters, and SQLite can decide that
            # with a substring test far cheaper than json.loads can. On this
            # library it takes the parse set from ~39,000 rows to ~6,600.
            # Every library still gets a row, so a library with no image subs
            # reports zeros rather than going missing.
            for lname, in cur.execute(
                    "SELECT DISTINCT library FROM files "
                    "WHERE library IS NOT NULL").fetchall():
                out[lname] = {"image": 0, "signs": 0, "forced": 0,
                              "dialogue": 0, "files": 0}
            rows = cur.execute(
                "SELECT f.library, p.json FROM file_probes p "
                "JOIN files f ON f.id=p.file_id "
                "WHERE f.library IS NOT NULL AND f.state NOT IN "
                "('deleted','duplicate') "
                "AND (p.json LIKE '%hdmv_pgs_subtitle%' "
                "     OR p.json LIKE '%pgssub%')").fetchall()
        for r in rows:
            slot = out.setdefault(r["library"], {"image": 0, "signs": 0,
                                                 "forced": 0, "dialogue": 0,
                                                 "files": 0})
            try:
                d = json.loads(r["json"])
            except Exception:                            # noqa: BLE001
                continue
            slot["files"] += 1
            subs = [s for s in (d.get("streams") or [])
                    if s.get("codec_type") == "subtitle"]
            mins = duration_min(d)
            for s in subs:
                if (s.get("codec_name") or "").lower() not in IMG_CODECS:
                    continue
                slot["image"] += 1
                if (s.get("disposition") or {}).get("forced"):
                    slot["forced"] += 1
                sg, _ = is_signs(s, mins)
                slot["signs" if sg else "dialogue"] += 1
    except Exception:                                    # noqa: BLE001
        pass
    _KINDS.update(at=now, data=out)
    return dict(out)


def signs_max_cpm(library: str | None = None) -> float:
    return float(_s("subocr_signs_max_cpm", SIGNS_MAX_CPM, library))


def dialogue_min_cues(library: str | None = None) -> int:
    return int(_s("subocr_dialogue_min_cues", DIALOGUE_MIN_CUES, library))


def enabled_for(library: str | None) -> bool:
    """Is conversion switched on for this library at all?"""
    return bool(_s("subocr_auto", True, library))


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


def is_signs(s: dict, minutes: float | None = None,
             library: str | None = None) -> tuple[bool, str]:
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
    if n_now is not None and (n_now / FRAMES_PER_CUE) >= dialogue_min_cues(library):
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
        if cpm < signs_max_cpm(library):
            return True, (f"{cpm:.1f} cues/min over {minutes:.0f} min - below "
                          f"the {signs_max_cpm(library)} dialogue floor")
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


def select_targets(probe: dict, library: str | None = None) -> list[dict]:
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
        # FORCED IS NOT SIGNS, and lumping them together cost the forced
        # tracks their own decision. Typeset signs are ARTWORK - positioned,
        # styled, sometimes rotated to match a shop front - and OCR cannot
        # recover any of that, which is why they are burned in and never
        # converted. A forced track is ordinary dialogue: the lines spoken in
        # another language, plain text at the bottom of the screen, which
        # reads back perfectly. Same disposition bit, completely different
        # content.
        if SIGNS_RE.search(t):
            return "signs"
        if (s.get("disposition") or {}).get("forced"):
            return "forced"
        return "dialogue"

    have_text = {_role(s) for s in subs
                 if (s.get("codec_name") or "").lower() in TEXT_CODECS
                 and _english(s)}

    mins = duration_min(probe)
    # (_never_burned is kept for callers/diagnostics; signs are no longer
    # converted on the strength of it - see the signs branch below.)
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
        convert_all = bool(_s("subocr_all", False, library))
        # SDH is its own switch: some people want the plain track only.
        if role == "sdh" and not convert_all \
                and not bool(_s("subocr_sdh", True, library)):
            continue
        # FORCED TRACKS ARE THEIR OWN CHOICE. They are burned into the picture
        # when the video is rebuilt, which is the best outcome - but a file
        # that never gets rebuilt keeps its forced lines as pictures, and Plex
        # paints those on the CPU at playback. Converting gives it a text
        # version to reach for instead. Off by default: burning is still the
        # better answer whenever it happens.
        if role == "forced" and not convert_all \
                and not bool(_s("subocr_forced", False, library)):
            continue
        signs, why = is_signs(s, mins, library)
        # SIGNS ARE NEVER CONVERTED. Not by a switch, not by the override,
        # not ever.
        #
        # Typeset signs and song captions are POSITIONED ARTWORK, the same
        # kind of thing an ASS track carries: a caption angled across a shop
        # front, a translation pinned beside a text message, karaoke timed to
        # a lyric - each one placed at a specific point on the screen because
        # that is what makes it mean anything. OCR reads the words and throws
        # the placement away, so what comes back is a flat list of centred
        # subtitles: unreadable, and sitting over the very thing they were
        # describing.
        #
        # That is not a degraded version of the track, it is a wrong one - so
        # there is nothing for a switch to trade off. Two attempts got this
        # wrong before: first an "unburnable signs" option, then leaving them
        # inside the convert-everything override on the grounds that the
        # override says the classification stands down. It does - about which
        # ROLES to read, not about whether a picture can be turned into text
        # it never was. Burning them into the video is the only rendering
        # that works, and a signs track that cannot be burned is left exactly
        # as it is.
        if signs:
            continue
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
    tdir = tesseract_dir()
    if os.path.isdir(tdir):
        env["PATH"] = tdir + os.pathsep + env.get("PATH", "")
        td = os.path.join(tdir, "tessdata")
        if os.path.isdir(td):
            env.setdefault("TESSDATA_PREFIX", td)
    return env


# The caller's window into whichever child is currently doing the work, so the
# job row can show real R/W rates the way a transcode does. Thread-LOCAL, not
# module-global: four workers run run_one() concurrently on separate threads,
# and a shared hook would attribute one job's ffmpeg to another job's row.
import threading
_TLS = threading.local()


def _hidden() -> "subprocess.STARTUPINFO | None":
    r"""A STARTUPINFO that hides the window - for the GRANDCHILDREN.

    CREATE_NO_WINDOW keeps OUR child off the screen, and that has always
    been set. It does not govern what that child then spawns: pgsrip calls
    mkvextract through a bare `subprocess.check_output`, with no flags at
    all, so every OCR job briefly threw a console onto the desktop - which
    is the "cmd windows popping up when a task starts" nobody could place.
    A hidden STARTUPINFO is inherited, so anything further down the chain
    that asks for a window gets one it cannot show. Same technique
    pytesseract uses on itself, applied one level up.
    """
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _run(args: list[str], timeout: float = 3600) -> tuple[int, str]:
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace",
                         creationflags=NO_WINDOW, startupinfo=_hidden(),
                         env=_env())
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


def _ffmpeg() -> str:
    r"""THE SAME ffmpeg every other part of nuarr uses.

    Delegates to jobs rather than resolving its own path, so the pin, the
    rollback and the "Uses ffmpeg" table on the ffmpeg page all apply here
    too. SETTINGS.ffmpeg is the last resort and defaults to the bare name
    "ffmpeg", which only works when it happens to be on PATH - true for the
    scheduled task, false for anything else, which is exactly how the OCR
    self-test failed to find a single testable file.
    """
    try:
        from . import jobs
        ff = jobs._ffmpeg_exe()
        if ff:
            return ff
    except Exception:                                    # noqa: BLE001
        pass
    return getattr(SETTINGS, "ffmpeg", "") or "ffmpeg"


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
    # _ffmpeg(), not SETTINGS.ffmpeg - see its docstring.
    rc, err = _run([_ffmpeg(), "-y", "-v", "error", "-i", path,
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
                # NAMES ITSELF, like the Paddle path does. With two engines
                # in play "OCR 40%" no longer says which one is grinding, and
                # that is the first thing anyone looking at a slow card wants
                # to know. "(est)" stays: this bar is a guess from the size of
                # the .sup, because pgsrip reports nothing per cue.
                tick(base + span * frac,
                     f"OCR{who} — Tesseract "
                     f"~{int(min(99, frac * 100))}% (est)")

    th = threading.Thread(target=_ticker, daemon=True)
    th.start()
    try:
        # Through the wrapper, not `-m pgsrip` - see pgsrip_hidden.py. It is
        # what stops mkvextract and tesseract flashing consoles per cue.
        rc, err = _run([_python(),
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "pgsrip_hidden.py"),
                        "--language", "en", "--force", sup])
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
            on_progress=None, library: str | None = None) -> dict:
    """OCR only. Writes SRTs to the pending area and mNothing else.

    Split out from run_one so the expensive half can happen once, up front,
    and the resulting subtitles can be picked up by whichever job next
    rewrites the file - an encode, a passthrough, or failing those the
    subtitle job itself. Rewriting a file twice to do one thing was the waste
    worth removing.
    """
    res = run_one(path, probe, work_root, on_progress, mux=False,
                  library=library)
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
            on_progress=None, mux: bool = True, on_child=None,
            library: str | None = None) -> dict:
    """Convert every eligible track and produce a new file. Replaces nothing.

    The caller commits, so this inherits the Plex gate, the disk pacing and
    the DrivePool-aware move rather than reimplementing them badly.
    """
    targets = select_targets(probe, library)
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
                # WHICHEVER ENGINE THIS LIBRARY CHOSE. Tesseract goes through
                # pgsrip as it always has; PaddleOCR runs in its own process,
                # reads italics far better, and can keep each cue's position.
                if engine(library) == "paddle":
                    srt = ocr_paddle(sup, tick, base + span * 0.1,
                                     span * 0.8, who)
                else:
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


# ------------------------------------------------------------ the tool page --

def status() -> dict:
    """Everything the OCR settings page needs, without OCR'ing anything."""
    tdir = tesseract_dir()
    exe = os.path.join(tdir, "tesseract.exe")
    tver = ""
    if os.path.exists(exe):
        rc, out = _run([exe, "--version"], timeout=20)
        m = re.search(r"tesseract\s+v?([\w.\-]+)", out or "")
        tver = m.group(1) if m else ""
    pg = ""
    try:
        from importlib import metadata
        pg = metadata.version("pgsrip")
    except Exception:                                    # noqa: BLE001
        pass
    from .config import DATA_DIR
    kinds = library_track_kinds(blocking=False)
    return {
        "tesseract_dir": tdir if os.path.exists(exe) else "",
        "tesseract_version": tver,
        "tesseract_managed": tdir.startswith(str(DATA_DIR)),
        "pgsrip_version": pg,
        "ready": bool(tver and pg),
        "install": dict(_INSTALL),
        # The install-wide engine, for the OCR engines page.
        "engine": engine(),
        "every_h": int(_s("subocr_every_h", 6)),
        "batch": int(_s("subocr_batch", 20)),
        # Per library: the effective settings, plus what that library
        # actually CONTAINS, so the page can grey out a switch that could
        # never do anything there.
        "libraries": {
            l.name: {
                "auto": bool(_s("subocr_auto", True, l.name)),
                "sdh": bool(_s("subocr_sdh", True, l.name)),
                "forced": bool(_s("subocr_forced", False, l.name)),
                "all": bool(_s("subocr_all", False, l.name)),
                "remove_image": bool(_s("subocr_remove_image", False, l.name)),
                "engine": engine(l.name),
                "signs_max_cpm": signs_max_cpm(l.name),
                "dialogue_min_cues": dialogue_min_cues(l.name),
                "has": (kinds.get(l.name)
                        or {"image": 0, "signs": 0, "forced": 0,
                            "dialogue": 0, "files": 0}),
                # The subtitle RULES for this library, each on its own switch.
                "rules": sub_rules(l.name),
            } for l in (SETTINGS.libraries or [])
        },
        "rule_meta": RULE_META,
        # The install-wide engine, named for the per-library panel to show as
        # a fact rather than offer as a choice. The choice lives on the OCR
        # engines page; a second control for the same setting can only ever
        # disagree with it.
        "engine_label": ("PaddleOCR" if engine("") == "paddle"
                         else "Tesseract"),
    }


# ------------------------------------------------------- the subtitle rules --
# These were constants in rules.CONFIG - one answer for every library, only
# changeable by editing the file. They are decisions about how subtitles are
# handled, so they belong beside the languages, per library, with a plain
# description of what each one does.

RULE_META = [
    {"key": "burn", "label": "Burn signs and forced lines into the picture",
     "what": "The only way typeset signs and song captions render correctly - "
             "OCR cannot recover where they sit on screen. It needs the video "
             "to be rebuilt, so it happens when a rebuild is happening anyway.",
     "default": True},
    {"key": "burn_image_always",
     "label": "Rebuild the video just to burn in a picture track",
     "what": "Without this, a file that needs no other work keeps its picture "
             "subtitles - and Plex burns them in on the CPU at playback instead, "
             "which is a transcode, and an expensive one at 4K.",
     "default": True},
    {"key": "drop_covered",
     "label": "Drop a picture track the release already covers in text",
     "what": "When the release ships its own SRT or ASS doing the same job in "
             "the same language, the picture copy adds nothing - and a human "
             "typed that text, so it beats anything OCR would produce. "
             "Matched per role, so an SDH track is never dropped for a plain "
             "dialogue one.",
     "default": True},
    {"key": "clear_flags",
     "label": "Stop kept picture tracks switching themselves on",
     "what": "A picture track flagged default or forced makes Plex burn it in "
             "at playback. Clearing the flags leaves it selectable by hand "
             "but never automatic.",
     "default": True},
    {"key": "remove_burned",
     "label": "Remove a track once it is burned into the picture",
     "what": "It is part of the video now. With this off the track is kept but "
             "its default/forced flags are still cleared, so the same lines "
             "are never drawn twice on top of each other.",
     "default": True},
    {"key": "burn_lang_guard",
     "label": "Only burn in a track that is English or untagged",
     "what": "Stops a German or French forced track being burned permanently "
             "into a picture you wanted in English.",
     "default": True},
    {"key": "force_eng_sub",
     "label": "Turn on English subtitles when no English audio survives",
     "what": "For a subtitled release, so it plays with subtitles without "
             "anyone reaching for the menu. Always a text track, never a "
             "picture one, and never the signs track.",
     "default": True},
    {"key": "burn_hdr", "label": "Allow burning into HDR video",
     "what": "The colour tags and the HDR10 metadata are re-stated on the "
             "output, so the rebuild keeps its HDR. Dolby Vision's per-frame "
             "layer cannot survive any rebuild, so a DV file comes out as "
             "HDR10 - the same thing the Dolby Vision rule produces.",
     "default": True},
]

# Which rules.CONFIG constant each switch stands for.
_RULE_CFG = {
    "burn": "burnEnabled",
    "burn_image_always": "alwaysBurnImageSubs",
    "drop_covered": "dropRedundantImageSubs",
    "clear_flags": "neutralizeKeptImageSubFlags",
    "remove_burned": "removeBurnedSub",
    "burn_lang_guard": "burnLangGuard",
    "force_eng_sub": "forceEngSubWhenNoEngAudio",
    "burn_hdr": "burnOnHDR",
}


def sub_rules(library: str | None = None) -> dict:
    """Every subtitle rule switch for one library, resolved."""
    try:
        from . import rules
        C = rules.CONFIG
    except Exception:                                    # noqa: BLE001
        C = {}
    out = {}
    for m in RULE_META:
        base = C.get(_RULE_CFG[m["key"]], m["default"])
        out[m["key"]] = bool(_s("subrule_" + m["key"], base, library))
    return out


def sub_rule(key: str, library: str | None = None) -> bool:
    """One rule, for the planner. Falls back to the global constant."""
    try:
        from . import rules
        base = rules.CONFIG.get(_RULE_CFG.get(key, ""), True)
    except Exception:                                    # noqa: BLE001
        base = True
    return bool(_s("subrule_" + key, base, library))


_INSTALL = {"state": "idle", "log": "", "error": ""}


# ------------------------------------------------------------- PaddleOCR ----
# A SECOND ENGINE, NOT A REPLACEMENT. Measured on this library - 55 real PGS
# cues from a dialogue track and an anime signs track:
#
#     engine              speed             disagreements   who was right
#     Tesseract           ~135 ms/cue       2 of 55         Paddle, both times
#     PaddleOCR (CPU)     ~2000 ms/cue      "
#     PaddleOCR (GPU)     ~200 ms/cue       "
#
# Both of Tesseract's misses were ITALICS - "lLlknow ldo." for "I know I do."
# - which is its known weakness and common in subtitles. So Paddle is the
# more accurate reader, and on a GPU it costs nothing for that; on CPU it is
# fifteen times slower, which is why Tesseract stays the default and remains
# the right answer on a machine with no card.

PADDLE_INSTALL = {"state": "idle", "mode": "", "log": "", "error": ""}
# The GPU build is not on PyPI; it comes from Paddle's own index. TWO
# SEPARATE pip RUNS, because `-i` REPLACES the index rather than adding to it -
# asking Paddle's server for `paddleocr` returns "from versions: none", which
# is what the first attempt did. So each package is fetched from the index
# that actually has it.
_PADDLE_GPU_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
_PADDLE_STEPS = {
    "gpu": [["paddlepaddle-gpu==3.3.1", "-i", _PADDLE_GPU_INDEX],
            ["paddleocr"]],
    "cpu": [["paddlepaddle"], ["paddleocr"]],
    "update": [["--upgrade", "paddleocr"]],
}


_PADDLE_CACHE: dict = {"at": 0.0, "data": None}
_PADDLE_TTL = 600.0


def paddle_invalidate() -> None:
    """Forget the cached answer - after an install, or on demand."""
    _PADDLE_CACHE["at"] = 0.0


def paddle_info(force: bool = False) -> dict:
    """Which Paddle is installed, and can it see the card.

    CACHED, because the honest way to answer costs seconds: it starts a
    Python, imports paddle and asks it about CUDA, and paddle is hundreds of
    megabytes of native code. That is the right way to ask - see below - but
    it is not something to do on every page load for an answer that only
    changes when somebody installs something. The install path clears this.
    """
    now = time.time()
    if not force and _PADDLE_CACHE["data"] is not None \
            and (now - _PADDLE_CACHE["at"]) < _PADDLE_TTL:
        d = dict(_PADDLE_CACHE["data"])
        d["install"] = dict(PADDLE_INSTALL)      # live, never cached
        return d
    import subprocess as _sp
    import sys as _sys
    out = {"installed": False, "paddleocr": "", "paddlepaddle": "",
           "cuda": False, "gpu_name": "", "install": dict(PADDLE_INSTALL)}
    code = (
        "import json\n"
        "d={}\n"
        "try:\n"
        "    import paddleocr; d['paddleocr']=getattr(paddleocr,'__version__','?')\n"
        "except Exception: pass\n"
        "try:\n"
        "    import paddle; d['paddlepaddle']=paddle.__version__\n"
        "    d['cuda']=bool(paddle.device.is_compiled_with_cuda())\n"
        "except Exception: pass\n"
        "print(json.dumps(d))\n")
    try:
        # ASKED IN A CHILD, ALWAYS. Importing paddle into the server process
        # just to learn whether paddle exists would pull hundreds of MB of
        # native code nuarr otherwise never touches - and would let a broken
        # wheel kill the web server on a settings page load.
        r = _sp.run([_sys.executable, "-c", code], capture_output=True,
                    text=True, timeout=120, creationflags=NO_WINDOW)
        lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
        if lines:
            d = json.loads(lines[-1])
            out.update(paddleocr=d.get("paddleocr", ""),
                       paddlepaddle=d.get("paddlepaddle", ""),
                       cuda=bool(d.get("cuda")))
            out["installed"] = bool(out["paddleocr"] and out["paddlepaddle"])
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import encoders
        out["gpu_name"] = encoders.devices().get("gpu_name", "")
    except Exception:                                    # noqa: BLE001
        pass
    _PADDLE_CACHE.update(at=now, data=dict(out))
    return out


def paddle_install_start(mode: str) -> dict:
    """Install PaddleOCR from the page - the Whisper pattern, second engine."""
    import threading as _t
    from . import heavy
    if mode not in _PADDLE_STEPS:
        return {"ok": False, "error": f"unknown mode {mode!r}"}
    if PADDLE_INSTALL["state"] == "installing":
        return {"ok": False, "error": "an install is already running"}
    # Shares one lane with the Whisper install and both engine tests - see
    # app/heavy.py. Two model-sized operations at once is what took the
    # server down on a small VM.
    got, holder = heavy.claim("PaddleOCR install")
    if not got:
        return {"ok": False,
                "error": f"{holder} is running — engine work is done one at "
                         "a time so a small machine is never asked to load "
                         "two models at once. Try again when it finishes."}
    PADDLE_INSTALL.update(state="installing", mode=mode, log="", error="")

    def _work():
        import subprocess as _sp
        import sys as _sys
        from collections import deque
        tail: deque = deque(maxlen=40)
        try:
            for step in _PADDLE_STEPS[mode]:
                cmd = [_sys.executable, "-m", "pip", "install",
                       "--prefer-binary", "--no-warn-script-location"] + step
                tail.append(f"$ pip install {' '.join(step)}")
                p = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                              text=True, creationflags=NO_WINDOW)
                for line in p.stdout:
                    if line.strip():
                        tail.append(line.rstrip())
                        PADDLE_INSTALL["log"] = "\n".join(tail)
                if p.wait() != 0:
                    raise RuntimeError(f"pip exited {p.returncode} on "
                                       f"{step[0]}")
            PADDLE_INSTALL.update(state="done")
            paddle_invalidate()          # the cached answer is now wrong
            from . import joblog as _jl
            _jl.log(f"PaddleOCR installed ({mode})", "ok")
        except Exception as e:                           # noqa: BLE001
            PADDLE_INSTALL.update(state="error",
                                  error=f"{type(e).__name__}: {str(e)[:160]}")
        finally:
            # Claimed in paddle_install_start(); released here so a failed
            # install cannot hold the lane shut.
            try:
                heavy.release("PaddleOCR install")
            except Exception:                            # noqa: BLE001
                pass
    _t.Thread(target=_work, name="paddle-install", daemon=True).start()
    return {"ok": True, "mode": mode}


def engine(library: str | None = None) -> str:
    """Which OCR engine this library uses. Tesseract unless told otherwise."""
    e = str(_s("subocr_engine", "tesseract", library) or "tesseract").lower()
    return e if e in ("tesseract", "paddle") else "tesseract"


def ocr_paddle(sup: str, tick=None, base: float = 0.0, span: float = 1.0,
               who: str = "", ass: bool = False, device: str = "") -> str:
    r"""Read a .sup with PaddleOCR, in its own process.

    Returns the path it produced - .srt normally, .ass when `ass` is set,
    which is the mode that keeps each cue's position on screen.
    """
    import sys as _sys
    dev = device or ("gpu" if paddle_info().get("cuda") else "cpu")
    out = os.path.splitext(sup)[0] + (".ass" if ass else ".srt")
    args = [_sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "paddle_worker.py"),
            sup, "--out", out, "--device", dev, "--progress"]
    if ass:
        args.append("--ass")
    label = f"PaddleOCR · {dev.upper()}"
    if tick:
        tick(base, f"OCR{who} — {label}")
    # REAL PROGRESS, NOT AN ESTIMATE. The Tesseract path has to guess from the
    # size of the .sup because pgsrip reports nothing; this worker prints one
    # line per cue, so the bar can follow the actual work. Same bar, two very
    # different sources - see ocr().
    rc, err, tail = _run_progress(args, tick, base, span, who, label)
    if not os.path.exists(out):
        raise RuntimeError(f"PaddleOCR produced nothing (rc={rc}): "
                           f"{(err or tail).strip()[:300]}")
    if tick:
        tick(base + span, f"OCR{who} — {label} done")
    return out


def _run_progress(args: list[str], tick, base: float, span: float,
                  who: str, label: str) -> tuple[int, str, str]:
    """Run a child that prints `PROGRESS i/n`, moving the bar as it does."""
    from collections import deque
    tail: deque = deque(maxlen=25)
    p = subprocess.Popen(args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         errors="replace", creationflags=NO_WINDOW,
                         startupinfo=_hidden(), env=_env())
    hook = getattr(_TLS, "on_child", None)
    if hook:
        try:
            hook(p)
        except Exception:                                # noqa: BLE001
            pass
    try:
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("PROGRESS "):
                try:
                    i, n = line.split(" ", 1)[1].split("/")
                    frac = min(1.0, int(i) / max(1, int(n)))
                    if tick:
                        tick(base + span * frac,
                             f"OCR{who} — {label} {int(frac * 100)}% "
                             f"({i}/{n} cues)")
                except (ValueError, ZeroDivisionError):
                    pass
            else:
                tail.append(line)
        rc = p.wait()
    finally:
        if hook:
            try:
                hook(None)
            except Exception:                            # noqa: BLE001
                pass
    return rc, "", "\n".join(tail)


_TEST_SUP: dict = {"path": "", "cues": 0, "title": ""}


def _probe_a_few(limit: int = 25) -> dict | None:
    """Probe a few unprobed files looking for one picture subtitle track.

    Only reached when the probe cache holds nothing at all - a fresh install
    that has indexed files but not yet run any job. Bounded on purpose: this
    runs behind a button press, so it may cost a second or two and must not
    turn into a library-wide scan.
    """
    import asyncio as _aio
    from .db import cursor
    from .config import DATA_DIR
    from . import jobs as _jobs
    work = os.path.join(str(DATA_DIR), "ocrtest")
    os.makedirs(work, exist_ok=True)
    with cursor() as cur:
        rows = cur.execute(
            "SELECT f.id, f.path, f.title FROM files f "
            " LEFT JOIN file_probes p ON p.file_id = f.id "
            " WHERE f.state NOT IN ('deleted','duplicate') AND p.file_id IS NULL"
            " LIMIT ?", (limit,)).fetchall()
    for r in rows:
        if not os.path.exists(r["path"]):
            continue
        try:
            data = _aio.run(_jobs.probe(r["path"]))
        except Exception:                                # noqa: BLE001
            continue
        if not data:
            continue
        try:
            _jobs.cache_probe(r["id"], data)             # keep what we learned
        except Exception:                                # noqa: BLE001
            pass
        subs = [s for s in (data.get("streams") or [])
                if s.get("codec_type") == "subtitle"]
        for rel, s in enumerate(subs):
            if (s.get("codec_name") or "").lower() not in IMG_CODECS:
                continue
            try:
                sup = extract_sup(r["path"], rel, work, "sample")
            except Exception:                            # noqa: BLE001
                continue
            _TEST_SUP.update(path=sup, cues=0, title=r["title"] or "")
            return dict(_TEST_SUP)
    return None


def _test_sample() -> dict:
    """A real .sup from the library, cached, for the engine tests.

    A REAL TRACK, not a synthetic image: the whole question these tests
    answer is "how does this engine do on MY subtitles", and a rendered
    sample of Arial would flatter both engines equally and tell nobody
    anything.
    """
    if _TEST_SUP["path"] and os.path.exists(_TEST_SUP["path"]):
        return dict(_TEST_SUP)
    from .db import cursor
    from .config import DATA_DIR
    work = os.path.join(str(DATA_DIR), "ocrtest")
    os.makedirs(work, exist_ok=True)
    with cursor() as cur:
        rows = cur.execute(
            "SELECT f.path, f.title, p.json FROM file_probes p "
            "JOIN files f ON f.id=p.file_id "
            "WHERE f.state NOT IN ('deleted','duplicate') "
            "AND (p.json LIKE '%hdmv_pgs_subtitle%' OR p.json LIKE '%pgssub%') "
            "LIMIT 400").fetchall()
    # WHY IT FAILED, NOT JUST THAT IT DID. "no reachable file to test with"
    # was one message for three unrelated situations: nothing has been probed
    # yet, nothing here HAS a picture subtitle, or the files are known but
    # cannot be opened - which is what a library on a network share looks like
    # to a service running as SYSTEM, since that account reaches the network
    # as the machine, not as you. Counting each case separately turns a dead
    # end into an instruction.
    missing = 0
    seen = 0
    extract_err = ""
    for r in rows:
        seen += 1
        if not os.path.exists(r["path"]):
            missing += 1
            continue
        try:
            d = json.loads(r["json"])
        except Exception:                                # noqa: BLE001
            continue
        subs = [s for s in (d.get("streams") or [])
                if s.get("codec_type") == "subtitle"]
        for rel, s in enumerate(subs):
            if (s.get("codec_name") or "").lower() not in IMG_CODECS:
                continue
            try:
                sup = extract_sup(r["path"], rel, work, "sample")
            except Exception as e:                       # noqa: BLE001
                extract_err = f"{type(e).__name__}: {str(e)[:90]}"
                continue
            _TEST_SUP.update(path=sup, cues=0, title=r["title"] or "")
            return dict(_TEST_SUP)
    if not seen:
        # NOTHING PROBED YET IS THE NORMAL STATE OF A NEW INSTALL, and it is
        # not something the person can fix by scanning: a scan indexes paths,
        # only a job reads a file's streams. So the test button could not
        # work until unrelated work happened to run - on a fresh machine,
        # possibly for hours. Probe a handful here instead: ffprobe on a few
        # files costs a second and is exactly what the button is asking for.
        sample = _probe_a_few()
        if sample:
            return sample
        why = ("no file here has a picture subtitle track — "
               "probed a sample and found none")
    elif missing == seen:
        why = (f"all {seen} candidate file(s) are indexed but cannot be "
               "opened from here. If this library is a network share, note "
               "that nuarr runs as a service: SYSTEM reaches the network as "
               "the machine account, not as you, so a share that works in "
               "Explorer can still be unreadable to it. A local path, or a "
               "share that grants the computer account access, fixes it")
    elif extract_err:
        why = f"found a picture subtitle track but could not extract it — {extract_err}"
    else:
        why = (f"{seen} file(s) checked, none carried a picture subtitle "
               "track to test with")
    return {"path": "", "cues": 0, "title": "", "why": why,
            "candidates": seen, "unreachable": missing}


def measurements() -> dict:
    """Every engine test that has been run here, newest per engine+device.

    THE TABLE ON THE PAGE IS THIS, not a constant. It shipped as a set of
    numbers measured on the machine nuarr was written on, which is exactly
    the kind of claim that goes stale and cannot be checked - a different
    card, a different library, a newer wheel and it is quietly a lie. Running
    a test overwrites its row, so the page reports what THIS machine did.
    """
    try:
        from .db import kv_get
        return json.loads(kv_get("subocr.measurements") or "{}")
    except Exception:                                    # noqa: BLE001
        return {}


def _record_measurement(row: dict) -> None:
    try:
        from .db import kv_get, kv_set
        cur = json.loads(kv_get("subocr.measurements") or "{}")
        key = (f"{row['engine']}:{row['device']}" if row["engine"] == "paddle"
               else row["engine"])
        cur[key] = {"per_cue_ms": row.get("per_cue_ms"),
                    "cues": row.get("cues"), "elapsed": row.get("elapsed"),
                    "title": row.get("title", ""), "at": time.time(),
                    "lines": (row.get("lines") or [])[:6]}
        kv_set("subocr.measurements", json.dumps(cur))
    except Exception:                                    # noqa: BLE001
        pass


def engine_test(which: str = "tesseract", device: str = "cpu",
                limit: int = 12) -> dict:
    """Read a few real cues with one engine and report what came back.

    The codec page's test-encode, for OCR: same images, one engine, honest
    numbers. Nothing is written to the library and nothing is saved.
    """
    import sys as _sys
    t0 = time.time()
    samp = _test_sample()
    if not samp["path"]:
        return {"ok": False,
                "error": samp.get("why") or "no file with a picture subtitle "
                                            "track to test with"}
    out = os.path.join(os.path.dirname(samp["path"]),
                       f"test_{which}_{device}.srt")
    args = [_sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "paddle_worker.py"),
            samp["path"], "--out", out, "--engine", which,
            "--device", device, "--limit", str(limit)]
    env_dir = tesseract_dir()
    old = os.environ.get("NUARR_TESSERACT_DIR")
    os.environ["NUARR_TESSERACT_DIR"] = env_dir
    try:
        rc, err = _run(args, timeout=1800)
    finally:
        if old is None:
            os.environ.pop("NUARR_TESSERACT_DIR", None)
        else:
            os.environ["NUARR_TESSERACT_DIR"] = old
    el = time.time() - t0
    if not os.path.exists(out):
        return {"ok": False, "engine": which, "device": device,
                "elapsed": round(el, 1),
                "error": (err or "").strip()[-300:] or f"exit {rc}"}
    lines = []
    for block in open(out, encoding="utf-8").read().split("\n\n"):
        rows = [x for x in block.splitlines() if x.strip()]
        if len(rows) >= 3:
            lines.append(" ".join(rows[2:]))
    try:
        os.remove(out)
    except OSError:
        pass
    row = {"ok": True, "engine": which, "device": device,
           "title": samp["title"], "cues": len(lines),
           "elapsed": round(el, 1),
           "per_cue_ms": int(el * 1000 / max(1, len(lines))),
           "lines": lines[:8]}
    _record_measurement(row)
    return row


# --------------------------------------------------------- engine updates --
# ONE CHECKER FOR ALL THREE PIECES, one daily job row, and every one of them
# installable by nuarr itself:
#   pgsrip, paddleocr - pip packages, updated in place.
#   Tesseract         - UB Mannheim's Windows build is an NSIS installer,
#                       which takes /S (silent) and /D= (target dir). Run
#                       into DataDir\tesseract - nuarr's managed location,
#                       which tesseract_dir() already prefers - it needs no
#                       interaction and touches no system install.

TESS_INSTALL = {"state": "idle", "error": "", "version": ""}


def _pip_latest(pkg: str) -> str:
    import sys as _sys
    try:
        r = subprocess.run([_sys.executable, "-m", "pip", "index", "versions",
                            pkg], capture_output=True, text=True, timeout=90,
                           creationflags=NO_WINDOW)
        m = re.search(r"LATEST:\s*([0-9][\w.\-]*)",
                      (r.stdout or "") + (r.stderr or ""))
        return m.group(1) if m else ""
    except Exception:                                    # noqa: BLE001
        return ""


def _tess_latest() -> tuple[str, str]:
    """Newest UB Mannheim build THAT HAS AN INSTALLER: (version, url).

    Releases, not tags. The first version of this read the tag list, which
    named 5.5.3 - and the download 404'd, because the Mannheim mirror the
    URL pointed at stops in 2024 and newer builds ship as GitHub release
    assets instead. A version nothing can install is not an update, so only
    releases carrying a w64 setup exe count, and the exe's own URL is what
    gets used - never a guessed one.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/UB-Mannheim/tesseract/releases"
            "?per_page=15",
            headers={"User-Agent": "nuarr",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            rels = json.loads(r.read().decode("utf-8"))
        best_v, best_url = "", ""
        for rel in rels or []:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            for a in rel.get("assets") or []:
                name = str(a.get("name") or "")
                m = re.match(r"tesseract-ocr-w64-setup-v?"
                             r"(\d+\.\d+\.\d+(?:\.\d+)?)\.exe$", name)
                if m and (not best_v
                          or m.group(1).split(".") > best_v.split(".")):
                    best_v = m.group(1)
                    best_url = a.get("browser_download_url") or ""
        return best_v, best_url
    except Exception:                                    # noqa: BLE001
        return "", ""


def updates_check() -> dict:
    """Ask upstream about all three pieces, remember the answer."""
    st = status()
    pi = paddle_info()
    out = {"at": time.time()}
    cur_t = st.get("tesseract_version") or ""
    lat_t, url_t = _tess_latest()
    out["tesseract"] = {"installed": cur_t, "latest": lat_t, "url": url_t,
                        "newer": bool(cur_t and lat_t
                                      and lat_t.split(".") > cur_t.split("."))}
    for pkg, cur in (("pgsrip", st.get("pgsrip_version") or ""),
                     ("paddleocr", pi.get("paddleocr") or "")):
        lat = _pip_latest(pkg) if cur else ""
        out[pkg] = {"installed": cur, "latest": lat,
                    "newer": bool(cur and lat and lat != cur)}
    try:
        from .db import kv_set
        kv_set("subocr.updates", json.dumps(out))
    except Exception:                                    # noqa: BLE001
        pass
    return out


def updates_state() -> dict:
    try:
        from .db import kv_get
        return json.loads(kv_get("subocr.updates") or "{}")
    except Exception:                                    # noqa: BLE001
        return {}


def tesseract_update_start(target_version: str = "") -> dict:
    """Download the newest UB Mannheim build and install it silently.

    Into nuarr's OWN folder, never over a system install: /D lands it in
    DataDir\\tesseract, which the resolver already prefers - so a hand-
    installed copy in Program Files is left exactly as its owner left it.
    """
    import threading as _t
    if TESS_INSTALL["state"] == "installing":
        return {"ok": False, "error": "already installing"}
    # REFUSED WHEN A SYSTEM INSTALL EXISTS, and this is a measurement, not
    # caution: the NSIS installer, on finding a registered copy, runs that
    # copy's UNINSTALLER first - and under /S it sat on an invisible prompt
    # for ten minutes on the very first test, one keypress away from removing
    # a Tesseract nuarr does not own. So the silent path is only taken when
    # the machine has no registered install to trip over; otherwise the
    # answer is a link and an explanation, which is at least always true.
    if os.path.exists(os.path.join(TESSERACT_DIR, "tesseract.exe")):
        return {"ok": False,
                "error": "a system Tesseract is installed at "
                         f"{TESSERACT_DIR} - its installer would try to "
                         "remove that copy first, which nuarr will not do. "
                         "Update it yourself from the link, or uninstall it "
                         "and press Update again for a nuarr-managed copy."}
    v, url = _tess_latest()
    if not v or not url:
        return {"ok": False, "error": "no installable build found upstream"}
    TESS_INSTALL.update(state="installing", error="", version=v)

    def _work():
        import urllib.request
        from .config import DATA_DIR
        exe = os.path.join(tempfile.gettempdir(), f"tess-{v}.exe")
        p = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nuarr"})
            with urllib.request.urlopen(req, timeout=120) as r, \
                    open(exe, "wb") as f:
                shutil.copyfileobj(r, f)
            dst = os.path.join(str(DATA_DIR), "tesseract")
            # NSIS: /S silent, /D last and UNQUOTED (its own rule; the path
            # has no spaces because DATA_DIR does not). Popen rather than
            # run, so a hang can be killed with its whole tree.
            p = subprocess.Popen([exe, "/S", f"/D={dst}"],
                                 creationflags=NO_WINDOW)
            try:
                rc = p.wait(timeout=600)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True, creationflags=NO_WINDOW)
                raise RuntimeError("the installer hung and was stopped - "
                                   "nothing was changed")
            if rc != 0:
                raise RuntimeError(f"installer exited {rc}")
            got = os.path.join(dst, "tesseract.exe")
            if not os.path.exists(got):
                raise RuntimeError("installer finished but tesseract.exe "
                                   "is not there")
            TESS_INSTALL.update(state="done")
            from . import joblog as _jl
            _jl.log(f"Tesseract {v} installed to nuarr's folder", "ok")
            updates_check()          # the stored answer just changed
        except Exception as e:                           # noqa: BLE001
            TESS_INSTALL.update(state="error",
                                error=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            try:
                os.remove(exe)
            except OSError:
                pass
    _t.Thread(target=_work, name="tess-update", daemon=True).start()
    return {"ok": True, "version": v}


_CHANGELOG_REPOS = {
    "tesseract": "UB-Mannheim/tesseract",
    "pgsrip": "ratoaq2/pgsrip",
    "paddleocr": "PaddlePaddle/PaddleOCR",
}


def changelog(which: str, since: str = "") -> dict:
    """What changed between the installed version and now, from upstream.

    Straight from each project's GitHub releases: title, date and body per
    release newer than `since`, capped so a project that writes essays does
    not flood the card. Answering "should I press Update" is the whole job.
    """
    import urllib.request
    repo = _CHANGELOG_REPOS.get(which)
    if not repo:
        return {"ok": False, "error": "unknown component"}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases?per_page=10",
            headers={"User-Agent": "nuarr",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            rels = json.loads(r.read().decode("utf-8"))
        out = []
        cur = [int(x) for x in re.findall(r"\d+", since or "")][:4]
        for rel in rels or []:
            if rel.get("draft"):
                continue
            tag = str(rel.get("tag_name") or "")
            ver = [int(x) for x in re.findall(r"\d+", tag)][:4]
            if cur and ver and ver <= cur:
                continue                 # already running this one
            body = (rel.get("body") or "").strip()
            if len(body) > 1200:
                body = body[:1200] + "…"
            out.append({"tag": tag, "name": rel.get("name") or tag,
                        "date": (rel.get("published_at") or "")[:10],
                        "body": body,
                        "url": rel.get("html_url") or ""})
            if len(out) >= 5:
                break
        return {"ok": True, "which": which, "since": since, "releases": out,
                "url": f"https://github.com/{repo}/releases"}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def updates_watch() -> None:
    """Once a day, ask upstream about all three - the Jobs page row."""
    import asyncio

    from . import schedules
    schedules.register(
        "ocrupd", "OCR engine updates", "System", 86400,
        what="Asks once a day whether newer Tesseract, pgsrip or PaddleOCR "
             "builds exist. Reports on the OCR engines page; installing is "
             "always your click there.")
    await asyncio.sleep(420)
    while True:
        try:
            st = updates_state()
            if time.time() - float(st.get("at") or 0) >= 86400:
                r = await asyncio.to_thread(updates_check)
                avail = [k for k in ("tesseract", "pgsrip", "paddleocr")
                         if (r.get(k) or {}).get("newer")]
                schedules.beat("ocrupd",
                               ("updates: " + ", ".join(avail)) if avail
                               else "everything current")
            else:
                schedules.beat("ocrupd", "checked recently")
        except Exception:                                # noqa: BLE001
            pass
        await asyncio.sleep(3600)


def pip_update_start() -> dict:
    """Update pgsrip from the page - the Whisper pattern, for the OCR rip."""
    import threading as _t
    if _INSTALL["state"] == "installing":
        return {"ok": False, "error": "already installing"}
    _INSTALL.update(state="installing", log="", error="")

    def _work():
        import sys
        from collections import deque
        tail: deque = deque(maxlen=30)
        try:
            p = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "--upgrade",
                 "--prefer-binary", "--no-warn-script-location", "pgsrip"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=NO_WINDOW)
            for line in p.stdout:
                if line.strip():
                    tail.append(line.rstrip())
                    _INSTALL["log"] = "\n".join(tail)
            if p.wait() != 0:
                raise RuntimeError(f"pip exited {p.returncode}")
            import importlib
            importlib.invalidate_caches()
            _INSTALL.update(state="done")
        except Exception as e:                           # noqa: BLE001
            _INSTALL.update(state="error",
                            error=f"{type(e).__name__}: {str(e)[:140]}")
    _t.Thread(target=_work, name="pgsrip-update", daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------- the sweep --
# WHY THIS EXISTS: OCR used to run only when a person pressed the queue
# button, and 2,335 files sat convertible for weeks - including the episode
# whose signs got burned in while its dialogue PGS stayed pictures. The rules
# knew what to do; nothing ever asked them to do it.

_SWEEP = {"last": 0.0, "queued_total": 0}


def sweep_pick(limit: int) -> list[dict]:
    """The next `limit` convertible files, respecting every switch."""
    from .db import cursor
    picked: list[dict] = []
    with cursor() as cur:
        rows = cur.execute(
            "SELECT p.file_id, p.json, f.path, f.title, f.library "
            "FROM file_probes p JOIN files f ON f.id=p.file_id "
            "WHERE f.state NOT IN ('deleted','duplicate') "
            "AND f.arr_file_id IS NOT NULL "
            "AND COALESCE(f.subocr_state,'') != 'rejected' "
            "AND f.id NOT IN (SELECT file_id FROM jobs WHERE file_id IS "
            "NOT NULL AND state IN ('queued','running'))")
        for r in rows:
            if len(picked) >= limit:
                break
            if not enabled_for(r["library"]):
                continue
            try:
                d = json.loads(r["json"])
            except Exception:                            # noqa: BLE001
                continue
            if select_targets(d, r["library"]):
                picked.append({"file_id": r["file_id"], "path": r["path"],
                               "title": r["title"] or ""})
    return picked


async def watch() -> None:
    """Queue OCR work on a schedule, like every other recurring job.

    QUEUES, never converts inline: the jobs system owns pacing, the gate owns
    viewers, and an OCR pass through it behaves exactly like one queued by
    hand. The batch is deliberately modest per pass - this rewrites library
    files, and 'slow and continuous' beats 'all 2,335 at once' on a system
    whose whole point is not being noticed.
    """
    import asyncio

    from . import joblog as _log, jobs, schedules
    schedules.register(
        "subocr", "Subtitle OCR", "Library",
        max(1, int(_s("subocr_every_h", 6))) * 3600,
        what="Converts image subtitles (PGS) to embedded SRT by OCR, a batch "
             "at a time, so Plex can hand clients text instead of burning "
             "pictures into the video. Which tracks qualify - dialogue, SDH, "
             "signs & songs - is set per library on the Subtitle OCR page.")
    # Warm the per-library track counts while nobody is waiting, so the
    # Subtitles page opens instantly instead of paying for the walk itself.
    await asyncio.sleep(45)
    try:
        await asyncio.to_thread(library_track_kinds, True)
    except Exception:                                    # noqa: BLE001
        pass
    await asyncio.sleep(195)             # let the first scan settle
    while True:
        try:
            if bool(_s("subocr_auto", True)):
                every = max(1, int(_s("subocr_every_h", 6))) * 3600
                if time.time() - _SWEEP["last"] >= every:
                    picked = await asyncio.to_thread(
                        sweep_pick, int(_s("subocr_batch", 20)))
                    n = 0
                    for p in picked:
                        try:
                            j = await jobs.enqueue(p["file_id"], p["path"],
                                                   p["title"], kind="sub_ocr",
                                                   priority=90)
                            if j:
                                n += 1
                        except Exception:                # noqa: BLE001
                            continue
                    _SWEEP["last"] = time.time()
                    _SWEEP["queued_total"] += n
                    schedules.beat("subocr",
                                   f"queued {n} file(s)" if n
                                   else "nothing to convert")
                    if n:
                        _log.log(f"subtitle OCR sweep queued {n} file(s)",
                                 "info")
        except Exception as e:                           # noqa: BLE001
            joblog_mod = None
            try:
                from . import joblog as joblog_mod
                joblog_mod.log(f"subtitle OCR sweep: {type(e).__name__}: {e}",
                               "warn")
            except Exception:                            # noqa: BLE001
                pass
        await asyncio.sleep(600)
