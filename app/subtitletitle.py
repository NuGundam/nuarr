r"""Make the subtitle picker tell the truth about what it is offering.

THE PROBLEM, FOUND IN ONE FILE AND THEN IN SEVEN HUNDRED. BLUE LOCK (2022)
S02E11 offers a track called "Forced (Signs only)". A viewer reading that
picks it expecting a handful of captions over on-screen text. It carries 466
cues across 25 minutes and 38 seconds - eighteen a minute, which is the
cadence of people talking, not of signs. The same file carries an untitled
track with the identical 466 cues and 39,806 bytes, so the "signs" track is a
copy of the dialogue with a label that describes something else.

Plex shows exactly this string in its subtitle picker, and it is also what
Bazarr reads when deciding whether a file still needs subtitles. So a lie here
is a viewer choosing the wrong track and a downloader fetching something the
file already has.

WHY THIS IS ITS OWN THING AND NOT A RULE, and why it is a copy of audiotitle
rather than a branch of it: same shape, different measurement. A title is a
string in the container header - mkvpropedit rewrites it in place in about a
tenth of a second. The audit rules fix files by rebuilding them, which would
be absurd for a caption.

NOTHING IS RE-PROBED. Matroska carries a NUMBER_OF_FRAMES statistic per track,
which for a subtitle stream is the cue count, and a DURATION beside it. Both
are already in the probe nuarr stored when the file landed, so the whole
library is judged from the database without reading a byte off the pool.

WHERE THE LINES ARE, AND WHY THEY ARE NOT WHERE YOU WOULD GUESS
---------------------------------------------------------------
Measured over 13,038 subtitle tracks in this library that carry a cue count:

                        p5     p25    p50    p75     p95
    says signs only    0.1    0.5    1.9   64.9   894.6   cues/minute
    says full dialogue 10.4   14.6   18.6  22.7   150.7
    no title at all     9.7   14.0   17.7  21.1    27.3

Two things fall out of that. Human speech is a narrow band - the untitled
population, which is ordinary dialogue nobody felt the need to label, sits
between about 10 and 27 cues a minute, and the titled dialogue agrees. And the
signs population has a long, absurd tail: 894 cues a minute is fifteen a
second, which nobody reads. That tail is not mislabelled dialogue, it is ASS
TYPESETTING - an animated sign is one event per frame of animation, so a
genuine signs track can carry thousands of events and still be exactly what it
says.

So DENSE IS NOT THE TEST. The test is landing in the band where speech lives:
a track claiming to be sparse while running at a human talking cadence. Above
that band the density is evidence of typesetting, which is a reason to leave
the file alone rather than a reason to act - the same fail-closed rule the
refetch classifier follows.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

from .config import NO_WINDOW, SETTINGS
from .db import cursor
from . import joblog

EVERY_S = 24 * 3600.0

# THE BAND WHERE PEOPLE TALK. Both ends measured above, both deliberately
# inside the observed range rather than on its edge: the point is to catch the
# unarguable cases, not to maximise the count.
SPEECH_LO = 8.0          # below this a sparse claim is plausible
SPEECH_HI = 45.0         # above this the density is typesetting, not speech
# A track has to be long enough for a rate to mean anything. Two minutes of
# opening credits at four cues would read as 2 cues/min and say nothing.
MIN_DUR_S = 300.0
# And it needs enough cues that the rate is not one accident. A 24-minute
# episode at SPEECH_LO is 192 cues; well under that and it is not dialogue
# however the arithmetic lands.
MIN_CUES = 60

_CACHE: dict = {"at": 0.0, "data": None, "running": False,
                "done": 0, "total": 0, "now": "", "t0": 0.0, "t1": 0.0,
                "fixing": None, "failures": [], "last_fix": None}

# Words that promise a SPARSE track. Whole words only, so "songs" does not
# match inside another word and "cc" does not match inside "soccer".
_SPARSE = re.compile(
    r"\b(signs?|songs?|forced|s\s*[&+]\s*s|typeset(ting)?|karaoke)\b", re.I)
# Words that promise a FULL one. Used for the opposite finding and to tell a
# mixed title ("Dialogue + Signs") apart from a purely sparse claim.
_FULL = re.compile(
    r"\b(full|dialogu?e|sdh|cc|closed\s*caption\w*|subtitles?|translation|"
    r"dubtitles?)\b", re.I)

# What a title may contain and still be considered nuarr's to rewrite. Same
# conservative rule audiotitle follows: a title carrying any word not on this
# list - a fansub group, "Commentary", a person's name - is information nuarr
# did not put there and cannot regenerate, so it is reported and left alone.
_SAFE_WORDS = {
    "signs", "sign", "songs", "song", "forced", "full", "dialogue", "dialog",
    "sdh", "cc", "closed", "caption", "captions", "subtitle", "subtitles",
    "translation", "dubtitle", "dubtitles", "only", "and", "english",
    "eng", "japanese", "jpn", "spanish", "spa", "french", "fre", "fra",
    "german", "ger", "deu", "italian", "ita", "portuguese", "por", "korean",
    "kor", "chinese", "chi", "zho", "dutch", "nld", "s", "srt", "ass", "ssa",
    "pgs", "vobsub", "text", "typeset", "typesetting", "karaoke",
}
_TOKENS = re.compile(r"[a-z]+")
_DUR = re.compile(r"(\d+):(\d+):([\d.]+)")

_LANG_NAME = {
    "eng": "English", "jpn": "Japanese", "spa": "Spanish", "fre": "French",
    "fra": "French", "ger": "German", "deu": "German", "ita": "Italian",
    "por": "Portuguese", "kor": "Korean", "chi": "Chinese", "zho": "Chinese",
    "nld": "Dutch", "rus": "Russian", "ara": "Arabic", "pol": "Polish",
    "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish",
}


def _quiet_run(cmd, **kw):
    """subprocess.run that never flashes a console window. See config.py."""
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw.setdefault("startupinfo", si)
    kw.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kw)


def _mkvpropedit() -> str:
    p = getattr(SETTINGS, "mkvpropedit", "") or ""
    if p and os.path.exists(p):
        return p
    guess = r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
    return guess if os.path.exists(guess) else "mkvpropedit"


def mode() -> str:
    """THE PICTURE CHECK'S SWITCH, not a second one. Both readers answer one
    question now (see subkind.py), and one question has one switch."""
    from . import hardsub
    return hardsub.mode()


def sure_at() -> int:
    """The line auto will not act below - the picture check's mark line, since
    both readers now sit under one switch and one pair of lines."""
    from . import hardsub
    return hardsub.mark_at()


def _dur_s(t: str) -> float:
    m = _DUR.match(t or "")
    if not m:
        return 0.0
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def _stat(tags: dict, name: str):
    """A Matroska statistic, whatever language suffix it was written with.

    mkvmerge writes NUMBER_OF_FRAMES on some files and NUMBER_OF_FRAMES-eng on
    others, depending on the version and whether the track carries a language.
    Matching on the prefix is the difference between judging this library and
    judging a twelfth of it.
    """
    for k, v in (tags or {}).items():
        if k.upper().startswith(name):
            return v
    return None


def _rewritable(title: str) -> bool:
    """Is this title purely a role claim, with nothing nuarr cannot regenerate?"""
    words = _TOKENS.findall((title or "").lower())
    if not words:
        return False
    return all(w in _SAFE_WORDS for w in words)


def _rows_from_probe(path: str, probe: dict) -> list:
    r"""Subtitle tracks whose title contradicts their cue rate, one row each."""
    out = []
    s_i = 0
    for s in (probe.get("streams") or []):
        if s.get("codec_type") != "subtitle":
            continue
        s_i += 1                      # mkvpropedit numbers subtitle tracks from 1
        tags = s.get("tags") or {}
        old = (tags.get("title") or "").strip()
        if not old:
            continue                  # nothing claimed, nothing to contradict
        cues = _stat(tags, "NUMBER_OF_FRAMES")
        dur = _dur_s(str(_stat(tags, "DURATION") or ""))
        try:
            cues = int(cues)
        except (TypeError, ValueError):
            continue                  # no count stored - cannot be judged here
        if dur < MIN_DUR_S or cues < 1:
            continue
        cpm = cues / (dur / 60.0)
        sparse_claim = bool(_SPARSE.search(old))
        full_claim = bool(_FULL.search(old))
        lang = (tags.get("language") or "und").lower()
        name = _LANG_NAME.get(lang, "")

        # A MIXED CLAIM IS NOT A LIE. "Dialogue + Signs + Songs" says it has
        # both, and a dialogue cadence is exactly what it promised.
        if sparse_claim and full_claim:
            continue
        why = new = ""
        if sparse_claim and cues >= MIN_CUES and SPEECH_LO <= cpm <= SPEECH_HI:
            why = (f"says signs only, carries {cues:,} cues over "
                   f"{dur/60:.0f} minutes - {cpm:.0f} a minute, which is the "
                   f"cadence of people talking")
            new = name or "Dialogue"
        elif full_claim and not sparse_claim and cpm < 3.0 and cues < MIN_CUES:
            why = (f"says full dialogue, carries only {cues:,} cues over "
                   f"{dur/60:.0f} minutes - {cpm:.1f} a minute, which is "
                   f"signs rather than speech")
            new = (f"{name} (signs only)" if name else "Signs only")
        if not why:
            continue
        # HOW SURE, FROM THE ONE THING THAT IS MEASURED. This check has no
        # OCR to be unsure about - it has a cue rate - so the confidence is
        # simply how far inside the speech band the track sits. A track at 18
        # a minute is squarely where people talk; one at 8 or at 45 is on the
        # edge of the band and could be a long sparse track or a busy sign
        # sheet. Reported so the two panels read the same way round, and so a
        # borderline row can be recognised as borderline.
        mid = (SPEECH_LO + SPEECH_HI) / 2.0
        half = (SPEECH_HI - SPEECH_LO) / 2.0
        if sparse_claim:
            sure = 100.0 - (abs(cpm - mid) / half) * 45.0
        else:
            # The opposite finding: the further below 3 a minute, the surer.
            sure = 100.0 - (cpm / 3.0) * 40.0
        out.append({"track": s_i,
                    # mkvextract numbers tracks the way mkvmerge does, which
                    # for Matroska is ffprobe's stream index - not the s1/s2
                    # ordinal mkvpropedit wants. Both are carried because both
                    # are needed and they are not the same number.
                    "mkv_id": int(s.get("index") or 0),
                    "old": old, "new": new, "why": why,
                    "sure": int(max(0, min(100, round(sure)))),
                    "cues": cues, "cpm": round(cpm, 1),
                    "minutes": round(dur / 60.0, 1),
                    "codec": s.get("codec_name") or "",
                    "lang": lang,
                    # Reported either way; only the safe ones get a button.
                    "rewritable": _rewritable(old) and bool(new)
                    and new.lower() != old.lower()})
    return out


def _label(r) -> str:
    """'Detective Conan - S14E19', not 'Detective Conan'.

    Twenty-six rows sharing a series name is a list nobody can act on. The
    filename is in the row already and it is ninety characters of release
    tags; the four that identify the episode are the ones worth showing.
    """
    try:
        from .db import display_label
        return display_label(r["title"], r["season"], r["episode"])
    except Exception:                                            # noqa: BLE001
        return (r["title"] if "title" in r.keys() else "") or ""


# ---------------------------------------- what the cue rate cannot see ------
# THE CUE RATE IS A FIRST PASS, AND IT HAS A BLIND SPOT.
#
# By the Grace of the Gods S01E03 carries a track called "Signs & Songs" at 8.3
# cues a minute, which is inside the band where people talk, so the first pass
# called it mislabelled dialogue. It is not. Reading the actual events settles
# it in one look:
#
#     192 events across ELEVEN styles, named
#         neo sign · neo op enga · neo op roma · neo ed enga · neo ed roma
#     60% of them positioned with \pos or \move
#     and the first five are one "Episode 3" title card, animated frame by
#     frame with \clip - five events for one thing on screen
#
# That is a signs-and-karaoke track doing exactly what it says. What inflated
# its rate was the opening and ending songs: romaji and english lines, two
# events per lyric, which is a lot of cues and no dialogue at all.
#
# So the rate finds candidates and this confirms them, the same two-stage
# shape the hardsub check uses - bright pixels to find, OCR to confirm. The
# expensive half only ever runs on what the cheap half flagged.
#
# WHAT SEPARATES THEM, in order of how decisive it is:
#
#   THE GROUP SAID SO. Style names are written by whoever typeset the release,
#   and "sign", "op", "ed", "karaoke", "title" are not words that end up on a
#   dialogue style by accident. This is testimony, not inference.
#
#   IT IS POSITIONED. Dialogue sits where its style puts it, along the bottom.
#   A sign goes where the thing it labels is, which in ASS means \pos or
#   \move. Measured here: 60% on the signs track.
#
#   THERE ARE TOO MANY STYLES. A dialogue track needs one style, or two with
#   an alternate speaker. Eleven styles is a typesetter's palette.
POS_SHARE = 0.25         # a quarter positioned is already not dialogue
MANY_STYLES = 5
INSPECT_PER_SCAN = 80    # bounded: each is a demux of ~40 KB, not free
# THE WORDS THE TYPESETTER WRITES ON A STYLE THAT IS NOT DIALOGUE. Singular
# only - the plural is handled below - and no "opening"/"ending", because the
# test is for words that were written, not words that were nearly written.
_SIGN_WORDS = frozenset((
    "sign", "op", "ed", "oped", "karaoke", "kara", "title", "titlecard",
    "credit", "note", "caption", "typeset", "logo", "insert", "song",
    "lyric", "eyecatch", "romaji"))
# HOW A STYLE NAME BREAKS INTO WORDS. This used to be \b, which is a boundary
# between a word character and a non-word character - and a digit is a word
# character, so "OP1 - English" did not contain the word "op" while the same
# release's "1$OP" did. The test was reading punctuation rather than names.
#
# A style name is written by a person in a box that allows anything, so the
# separators are punctuation, spaces, underscores, digits running into
# letters, and CamelCase humps - including the acronym hump in "GTCredits".
_STYLE_SPLIT = re.compile(
    r"[^A-Za-z0-9]+"                     # - _ / # space
    r"|(?<=[a-z])(?=[A-Z])"              # SeriesTitle
    r"|(?<=[A-Z])(?=[A-Z][a-z])"         # GTCredits
    r"|(?<=[A-Za-z])(?=[0-9])"           # OP1, signs1
    r"|(?<=[0-9])(?=[A-Za-z])")          # 2Alt


# WHICH VERSION OF THIS READER PRODUCED A STORED COUNT.
#
# `plain` is "lines in non-sign styles", so it is only as good as the test
# above - and shape_of re-reads on a SIZE change, which never comes, because
# what changed is the reader and not the file. Bumped whenever the shape of
# the answer changes; shape_of re-reads anything behind it that has something
# to gain.
#
#   1  the \b style test - "OP1 - English" counted as dialogue
#   2  style names tokenised, so a numbered or glued theme style is a theme
SHAPE_REV = 2


def _style_is_sign(name: str) -> bool:
    r"""Did whoever typeset this release call the style a sign or a theme?

    Whole tokens, with an optional plural, so "Titles" and "signs1" match and
    "Editor", "Titan" and "Dialogue1" do not. Measured across every style
    name in the library when this replaced the \b test: 38 names and 127
    files newly recognised, nothing that matched before stopped matching,
    and no dialogue style caught.
    """
    for tok in _STYLE_SPLIT.split(name or ""):
        if not tok:
            continue
        t = tok.lower()
        if t in _SIGN_WORDS or (t.endswith("s") and t[:-1] in _SIGN_WORDS):
            return True
    return False
_EVENT = re.compile(r"^Dialogue:\s*(.*)$", re.M)
_POSITIONED = re.compile(r"\\(?:pos|move)\s*\(", re.I)


def _mkvextract() -> str:
    p = _mkvpropedit()
    guess = os.path.join(os.path.dirname(p), "mkvextract.exe")
    return guess if os.path.exists(guess) else "mkvextract"


def _inspect_init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subtitle_shape(
                file_id  INTEGER NOT NULL,
                track    INTEGER NOT NULL,
                size     INTEGER,
                at       REAL,
                events   INTEGER,
                styles   INTEGER,
                pos_pct  REAL,
                signish  INTEGER,
                detail   TEXT,
                PRIMARY KEY (file_id, track)
            )""")
        # plain: the count above. chosen: what a person said the track is,
        # which outranks the reading for good - the same column and the same
        # reason the hardsub table has one.
        # acked: you have seen what your choice did and taken the row off
        # the list. Setting a kind is not that - the row has to stay until a
        # button is pressed, or the picker becomes a way to lose rows.
        # plain_out: plain non-sign-style lines OUTSIDE the OP/ED, and
        # oped_s: how many seconds of theme that was measured against.
        #
        # BOTH NULLABLE, AND THE NULL MEANS SOMETHING. plain_out is NULL when
        # nuarr does not know where this file's theme is, which is most of the
        # library - only 15% of files name an OP or ED chapter. Writing the
        # unrestricted count into plain_out instead would say "no lyrics here"
        # about a file nobody checked, and this column decides whether a
        # subtitle track is kept.
        #
        # oped_s = -1 likewise separates "never looked for chapters" from
        # "looked, and this file names none" (0).
        for col, decl in (("plain", "INTEGER"), ("chosen", "TEXT"),
                          ("acked", "INTEGER"),
                          ("plain_out", "INTEGER"), ("oped_s", "REAL"),
                          # rev: which version of this reader counted it. See
                          # SHAPE_REV. NULL is "before there was one", which
                          # is every row that existed when this was added.
                          ("rev", "INTEGER")):
            try:
                cur.execute(f"ALTER TABLE subtitle_shape ADD COLUMN {col} {decl}")
            except Exception:                                    # noqa: BLE001
                pass


def _ass_secs(t: str) -> float:
    """'0:01:28.50' -> 88.5. -1 when it is not a timestamp."""
    try:
        h, m, rest = t.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    except Exception:                                            # noqa: BLE001
        return -1.0


def _read_events(path: str, mkv_track_id: int, oped=None) -> dict | None:
    r"""Pull one text subtitle track out and describe its SHAPE.

    Never its words - this is about where the lines are and what the styles
    are called, which is the part the cue count could not see.
    """
    if not os.path.exists(path):
        return None
    # The cache, like every other scratch file - this one was already off the
    # pool, in the system TEMP, but there is no reason for it to be the one
    # exception to where nuarr puts working files.
    from . import fileops
    out = fileops.cache_temp(".txt", "shape")
    try:
        r = _quiet_run([_mkvextract(), "tracks", path,
                        f"{int(mkv_track_id)}:{out}"],
                       capture_output=True, text=True, timeout=180)
        if r.returncode >= 2 or not os.path.exists(out):
            return None
        # A PICTURE TRACK IS NOT A TRACK THIS CAN READ, and saying so is the
        # whole point. Read as text a .sup is a stack of bitmaps with no
        # "Dialogue:" and no "-->" in it, so this used to come back "0 events,
        # nothing readable" and write that down - and a stored zero is what
        # the duplicate sweep deletes a track for. Unreadable here means
        # nothing is written at all.
        with open(out, "rb") as fh:
            head = fh.read(4096)
        if head[:2] == b"PG" or b"\x00" in head:
            return None
        with open(out, encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    except Exception:                                            # noqa: BLE001
        return None
    finally:
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
    lines = _EVENT.findall(text)
    if not lines:
        # SRT and VTT have no styles and no positioning, so there is nothing
        # here to learn - and nothing to contradict the rate with either.
        # An SRT has no styles and no positioning - every cue is a plain
        # line, which is exactly what makes an SRT a dialogue format. The
        # cue count IS the plain count.
        srt_n = len(re.findall(r"^\d+\s*$", text, re.M))
        # An SRT carries no styles, so there is nothing for the theme to
        # hide in and nothing to restrict - but the column still has to
        # distinguish "no lyrics outside" from "nobody looked".
        return {"events": srt_n, "styles": 1 if srt_n else 0, "plain": srt_n,
                "plain_out": (srt_n if oped is not None else None),
                "oped_s": (sum(b - a for a, b, _t in (oped or []))
                           if oped is not None else -1.0),
                "pos_pct": 0.0, "signish": 0,
                "detail": (f"{srt_n} plain cues, no styling - a text format "
                           f"that can only be dialogue" if srt_n
                           else "nothing readable came back")}
    # THE LINE THAT SEPARATES THEM, MEASURED. Five tracks, read in full:
    #
    #   BLUE LOCK  'Forced (Signs only)'  466 events  Default=286 Italics=152
    #                                     394 plain lines in non-sign styles
    #   BLUE LOCK  'Dubtitle (SDH)'       615 events  Default=615
    #                                     615 plain lines
    #   Grace      'Signs & Songs'        192 events  neo sign / neo op / neo ed
    #                                     0 plain lines in non-sign styles
    #   Grace      'Dialogue'             486 events  neo default=294 + the same
    #                                     294 plain lines   signs/op/ed styles
    #
    # Dialogue is lines left where the style puts them, in a style called
    # Default or Main or Italics. Signs are lines placed by \pos in a style
    # called sign or op or ed. So the count that matters is PLAIN LINES IN
    # NON-SIGN STYLES, and a sign sheet has none of them however many events
    # it carries. Style COUNT does not separate them - BLUE LOCK's dialogue
    # runs across five styles (Default, Italics, Top, Flashback/Overlap) and
    # calling that "a typesetter's palette" was the mistake that cleared it.
    styles: dict = {}
    positioned = plain = 0
    # SEPARATELY, THE ONES OUTSIDE THE THEME. Song lyrics are plain lines in
    # a style often called nothing more suspicious than Default, so they pass
    # both halves of the test above and a track that is only an opening and an
    # ending reads as dialogue. The theme is the one place they can be
    # excluded from without excluding real speech - see chapters.py.
    plain_out = 0 if oped is not None else None
    in_oped = 0
    for ln in lines:
        parts = ln.split(",", 9)
        if len(parts) < 10:
            continue
        st = parts[3].strip()
        styles[st] = styles.get(st, 0) + 1
        theme = False
        if oped:
            a, b = _ass_secs(parts[1]), _ass_secs(parts[2])
            if a >= 0:
                if b < a:
                    b = a
                theme = any(a < e and b > s0 for s0, e, _t in oped)
                if theme:
                    in_oped += 1
        if _POSITIONED.search(parts[9]):
            positioned += 1
        elif not _style_is_sign(st):
            plain += 1
            if plain_out is not None and not theme:
                plain_out += 1
    n = max(1, len(lines))
    named = sorted(s for s in styles if _style_is_sign(s))
    top = max(styles.items(), key=lambda kv: kv[1]) if styles else ("", 0)
    share = positioned / n
    dstyles = len(styles) - len(named)
    bits = [("no plain dialogue lines" if not plain else
             f"{plain} plain dialogue line{'' if plain == 1 else 's'} in "
             f"{dstyles} style{'' if dstyles == 1 else 's'}")]
    if top[0]:
        bits.append(f"{top[0]!r} holds {top[1]/n*100:.0f}%")
    if named:
        bits.append("sign styles " + ", ".join(f"{s!r}" for s in named[:3]))
    if share:
        bits.append(f"{share*100:.0f}% positioned with \\pos or \\move")
    if oped is not None and plain_out is not None and plain_out != plain:
        bits.append(f"{plain - plain_out} of them inside the OP/ED")
    return {"events": len(lines), "styles": len(styles), "plain": plain,
            "plain_out": plain_out,
            "oped_s": (sum(b - a for a, b, _t in (oped or []))
                       if oped is not None else -1.0),
            "pos_pct": round(share * 100, 1), "signish": len(named),
            "detail": "; ".join(bits)}


def _duration_of(file_id: int) -> float:
    """Runtime in seconds, for judging whether a chapter is theme-sized."""
    try:
        with cursor() as cur:
            r = cur.execute("SELECT duration FROM files WHERE id=?",
                            (int(file_id),)).fetchone()
        return float((r and r["duration"]) or 0.0)
    except Exception:                                            # noqa: BLE001
        return 0.0


# What a shape row says when the track could not be read at all. See
# shape_of(): the row exists so the track stops being offered, and says why.
UNREADABLE = -1
UNREADABLE_WHY = "could not be read - not a text track, or mkvextract refused"


def shape_of(file_id: int, path: str, track: int, size: int,
             mkv_track_id: int) -> dict | None:
    """The cached shape of one track, reading it only when it is not known."""
    r = None
    try:
        _inspect_init()
        with cursor() as cur:
            r = cur.execute(
                "SELECT * FROM subtitle_shape WHERE file_id=? AND track=? "
                "  AND size=?", (int(file_id), int(track), int(size))).fetchone()
    except Exception:                                            # noqa: BLE001
        return None
    # WHERE THIS FILE'S THEME IS, if it says. None means nobody knows, which
    # is most of the library - only 15% of files name an OP or ED chapter -
    # and the reader is told None rather than an empty list so it can leave
    # plain_out NULL instead of claiming there are no lyrics.
    oped = None
    try:
        from . import chapters as _ch
        # strict: an ambiguous chapter title is only treated as a theme when
        # its POSITION says so. This number decides dialogue-or-signs and a
        # signs verdict is what drops a track, so a cold open chaptered
        # 'Intro' must not have its dialogue subtracted - see chapters.sane.
        found = _ch.for_file(int(file_id), path, live=True)
        if found is not None:
            oped = _ch.sane(found, _duration_of(file_id), strict=True)
    except Exception:                                            # noqa: BLE001
        oped = None
    try:
        # A row read before `plain` existed is a row that has to be read
        # again - it cannot answer the question that now decides everything.
        #
        # AND SO IS ONE READ BEFORE THE THEME WAS KNOWN, but only when the
        # theme is known NOW and this file actually has one. A file with no
        # OP/ED chapter gets the same answer either way, so re-reading it
        # would be an mkvextract for nothing - and this sweep is bounded at
        # eighty reads a scan, so spending them on files that cannot change
        # is spending them on nothing.
        if r and r["plain"] is not None:
            stale = bool(oped and r["plain_out"] is None)
            # AND A COUNT MADE BY AN EARLIER READER IS NOT A COUNT.
            #
            # `plain` means "lines in non-sign styles", so it is only as good
            # as _style_is_sign was on the day it was written - and the \b
            # test missed every numbered theme style, which is why eight
            # Undead Unluck episodes read as speech at 8.2 lines a minute.
            # Nothing else would ever redo them: the re-read above is keyed
            # on the file's size, and the file is fine. The reader changed.
            #
            # ONLY WHERE THERE IS SOMETHING TO GAIN. Each re-read is an
            # mkvextract, eighty a pass. A track already counting zero plain
            # lines cannot count fewer, and reclassifying a style can only
            # ever remove lines from that count - so of 1,219 stored shapes
            # this asks for 655 and leaves 560 alone. A kind set by hand
            # outranks the reader entirely and is never re-read.
            try:
                behind = int(r["rev"] or 0) < SHAPE_REV
            except Exception:                                    # noqa: BLE001
                behind = True
            if (behind and int(r["plain"] or 0) > 0
                    and not (r["chosen"] or "")):
                stale = True
            if not stale:
                return dict(r)
    except Exception:                                            # noqa: BLE001
        return None
    got = _read_events(path, mkv_track_id, oped)
    if got is None:
        # A TRACK THAT CANNOT BE READ IS AN ANSWER, AND IT HAS TO BE STORED.
        #
        # It was not. _read_events returns None for a missing file, an
        # mkvextract that refused, a picture track and any exception - and
        # this returned None too, writing nothing. The row therefore stayed
        # "not read yet" for ever: the scan listed it as a candidate, the
        # feeder queued a subread for it, the read failed again in under a
        # second, and the feeder queued it again a minute later. Measured on
        # this library: 2,339 subread jobs over 149 files in one day, one
        # Dororo episode 324 times in six hours, every one of them reporting
        # success.
        #
        # PLAIN = -1 IS THE SENTINEL and it is deliberate that it is not 0:
        # a stored zero means "no plain lines", which is what the duplicate
        # sweep deletes a track for. Minus one is a shape nothing can read as
        # a verdict, and scan() drops it from the candidates on sight.
        try:
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO subtitle_shape(file_id,track,size,at,events,"
                    "  styles,pos_pct,signish,detail,plain) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(file_id,track) DO UPDATE SET size=excluded.size,"
                    "  at=excluded.at, detail=excluded.detail, plain=excluded.plain",
                    (int(file_id), int(track), int(size), time.time(),
                     0, 0, 0.0, 0, UNREADABLE_WHY, UNREADABLE))
        except Exception:                                        # noqa: BLE001
            pass
        return None
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subtitle_shape(file_id,track,size,at,events,"
                "  styles,pos_pct,signish,detail,plain,plain_out,oped_s,rev) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id,track) DO UPDATE SET size=excluded.size, "
                "  at=excluded.at, events=excluded.events, "
                "  styles=excluded.styles, pos_pct=excluded.pos_pct, "
                "  signish=excluded.signish, detail=excluded.detail, "
                "  plain=excluded.plain, plain_out=excluded.plain_out, "
                "  oped_s=excluded.oped_s, rev=excluded.rev",
                (int(file_id), int(track), int(size), time.time(),
                 got["events"], got["styles"], got["pos_pct"], got["signish"],
                 got["detail"][:300], got.get("plain") or 0,
                 got.get("plain_out"), got.get("oped_s"), SHAPE_REV))
    except Exception:                                            # noqa: BLE001
        pass
    return got


def _shapes_for(file_ids) -> dict:
    """Every shape already known for these files, keyed (file_id, track)."""
    ids = sorted({int(i) for i in (file_ids or [])})
    out: dict = {}
    if not ids:
        return out
    try:
        _inspect_init()
        with cursor() as cur:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                qs = ",".join("?" * len(chunk))
                # ONLY SHAPES READ FROM THE FILE AS IT IS NOW. shape_of()
                # has always matched on size when reading one track; this is
                # the batch the PANEL is built from and it did not, so a file
                # rewritten since the read was described by the old shape.
                for r in cur.execute(
                        f"SELECT s.* FROM subtitle_shape s "
                        f"  JOIN files f ON f.id = s.file_id "
                        f" WHERE s.file_id IN ({qs}) "
                        f"   AND f.state NOT IN ('deleted','duplicate') "
                        # A kind you set by hand is a decision about the
                        # file, not a reading of its bytes, and correcting
                        # the title is itself what moved the size.
                        f"   AND (COALESCE(s.chosen,'') != '' "
                        f"        OR s.size IS NULL OR f.size IS NULL "
                        f"        OR s.size = f.size)", chunk):
                    out[(r["file_id"], r["track"])] = dict(r)
    except Exception:                                            # noqa: BLE001
        return {}
    return out


# THE READ IS THE JOB. The scan is a query and finishes before anyone notices;
# the read is a demux per candidate and is the part that takes time, yields to
# the gate, and needs a clock, a cadence and an estimate like every other
# sweep on this page. So this is what gets registered with the scheduler.
SCHED_KEY = "subtitletitle"
CYCLE_S = 1800.0          # every half hour, a bounded handful of reads
PER_RUN = 40
INSPECT_STATE: dict = {"running": False, "done": 0, "total": 0, "now": "",
                       "t0": 0.0, "cleared": 0, "last_run": 0.0,
                       "runs": 0, "secs_each": 0.0, "last_took": 0.0,
                       "last_read": 0, "last_cleared": 0, "yielded": "",
                       "last_error": ""}


async def _too_busy() -> bool:
    """The gate's opinion, not a second one. See audit._too_busy."""
    try:
        from .audit import _too_busy as busy
        return bool(await busy())
    except Exception:                                            # noqa: BLE001
        return False


# --------------------------------------------------------- onto the runner --
# FORTY READS AND THEN FIVE MINUTES ASLEEP. The gate check per file was
# already here and already right; the batch and the clock are what go.
#
# THE LIST IS REBUILT EACH CYCLE, WHICH THIS ALREADY RELIED ON. The scan is a
# query and costs under a second, and the runner calls pending() every time
# the list runs out - so refresh() belongs there rather than on a timer beside
# it.
KEY = "subtitletitle"
TITLE = "Subtitle titles against what the track carries"


def _pending() -> list:
    """Every flagged track that has not been read. Not a slice of them."""
    try:
        refresh()
    except Exception:                                            # noqa: BLE001
        pass
    d = _CACHE.get("data") or {}
    return [r for r in (d.get("rows") or [])
            if r.get("unread") and r.get("mkv_id")]


def _do_one(r: dict, report=None) -> dict:
    """Read one track's events and judge what it actually carries."""
    try:
        sh = shape_of(r["file_id"], r["path"], r["track"],
                      int(r.get("size") or 0), r["mkv_id"])
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    # A READ THAT FAILED IS NOT A VERDICT OF DIALOGUE. shape_of returns None
    # when the track cannot be read, and this used to fall through to
    # cleared=False - which the caller words as "the events say dialogue".
    # A failure reported as a finding is how the same track was read 324
    # times: the answer never changed, and neither did the question.
    if sh is None:
        return {"ok": False, "why": UNREADABLE_WHY, "unreadable": True}
    # LET THE PAGE SEE IT AS IT GOES. The scan is a query and costs under a
    # second; re-judging as each read lands is what turns "40 not read yet"
    # into a number that visibly falls while you watch.
    _CACHE["at"] = 0.0
    return {"ok": True, "cleared": bool(_is_really_signs(sh))}


def _after(d: dict) -> None:
    _CACHE["at"] = 0.0
    _beat(f"{d.get('done') or 0} read" if d.get("done")
          else "nothing left to read")


async def watch() -> None:
    import asyncio
    from . import idle
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "Subtitle titles", "Subtitles", CYCLE_S,
            what=("Compares each subtitle title against the cue count in "
                  "its stored probe, then reads the actual events of "
                  "anything that looks wrong - continuously while the box is "
                  "idle - because the count alone cannot tell karaoke from "
                  "dialogue."))
    except Exception:                                            # noqa: BLE001
        pass
    await asyncio.sleep(300)
    await idle.run(KEY, TITLE, _pending, _do_one,
                   label=lambda r: os.path.basename(r.get("path") or "")[:120],
                   disk_of=lambda r: r.get("pool_disk") or "",
                   note_of=lambda r: "reading the subtitle events",
                   system_name="Subtitle titles",
                   goto="/settings#subtitletitle", on_pass=_after)


async def inspect_paced(limit: int = PER_RUN, force: bool = False) -> dict:
    """inspect_some(), one file at a time, asking the gate before each.

    THE GATE DECIDES, BEFORE EVERY FILE - not once at the start. A pass of
    forty reads runs for minutes, and somebody pressing play in minute one
    should not wait out the other thirty-nine. `force` is the button: a person
    who pressed it has already decided the disks can spare it.
    """
    import asyncio
    if INSPECT_STATE["running"]:
        return {"ok": False, "why": "already reading"}
    d = _CACHE.get("data") or {}
    rows = [r for r in (d.get("rows") or []) if r.get("unread")
            and r.get("mkv_id")][:max(1, int(limit))]
    if not rows:
        INSPECT_STATE["last_run"] = time.time()
        _beat("every candidate has been read")
        return {"ok": True, "read": 0, "cleared": 0}
    t0 = time.time()
    INSPECT_STATE.update(running=True, done=0, total=len(rows), now="",
                         t0=t0, cleared=0, yielded="")
    cleared = read = 0
    try:
        for r in rows:
            if not force and await _too_busy():
                INSPECT_STATE["yielded"] = ("stopped early - the pool is busy "
                                            "or somebody is watching")
                break
            INSPECT_STATE.update(done=read,
                                 now=os.path.basename(r.get("path") or ""))
            try:
                sh = await asyncio.to_thread(
                    shape_of, r["file_id"], r["path"], r["track"],
                    int(r.get("size") or 0), r["mkv_id"])
            except Exception:                                    # noqa: BLE001
                read += 1
                continue
            read += 1
            if _is_really_signs(sh):
                cleared += 1
                INSPECT_STATE["cleared"] = cleared
            # LET THE PAGE SEE IT AS IT GOES. The scan is a query and costs
            # under a second; re-judging every few reads is what turns "40 not
            # read yet" into a number that visibly falls while you watch.
            if read % 5 == 0:
                _CACHE["at"] = 0.0
    finally:
        took = max(0.001, time.time() - t0)
        prev = INSPECT_STATE.get("secs_each") or 0.0
        this = took / max(1, read)
        INSPECT_STATE.update(
            running=False, now="", done=read, last_run=time.time(),
            t0=0.0, last_took=took, last_read=read, last_cleared=cleared,
            runs=INSPECT_STATE.get("runs", 0) + 1,
            # Smoothed across passes, like every other sweep: one pass of
            # forty is a small sample and a single remux on a sleeping disk
            # skews it.
            secs_each=(this if not prev else prev * 0.7 + this * 0.3))
        _CACHE["at"] = 0.0
        _beat(f"{read} read, {cleared} cleared" if read
              else "nothing left to read")
    if cleared:
        joblog.log(f"subtitle titles: read {read} flagged track(s) and "
                   f"cleared {cleared} - signs, karaoke or typesetting rather "
                   f"than mislabelled dialogue", "info")
    return {"ok": True, "read": read, "cleared": cleared,
            "yielded": INSPECT_STATE.get("yielded") or ""}


def _beat(result: str) -> None:
    try:
        from . import schedules
        schedules.beat(SCHED_KEY, result)
    except Exception:                                            # noqa: BLE001
        pass


def inspect_some(limit: int = INSPECT_PER_SCAN) -> dict:
    r"""Read the events of candidates nobody has read yet.

    ON ITS OWN CLOCK, because a demux is disk and the page is a query. Each
    one is small - a subtitle track out of a 184 MB episode - but eighty of
    them is not something a page load should wait for.
    """
    if INSPECT_STATE["running"]:
        return {"ok": False, "why": "already reading"}
    d = _CACHE.get("data") or {}
    rows = [r for r in (d.get("rows") or []) if r.get("unread")
            and r.get("mkv_id")]
    if not rows:
        return {"ok": True, "read": 0, "cleared": 0,
                "why": "every candidate has been read"}
    rows = rows[:max(1, int(limit))]
    INSPECT_STATE.update(running=True, done=0, total=len(rows), now="",
                         t0=time.time(), cleared=0)
    cleared = 0
    try:
        for i, r in enumerate(rows, 1):
            INSPECT_STATE.update(done=i - 1,
                                 now=os.path.basename(r.get("path") or ""))
            try:
                sh = shape_of(r["file_id"], r["path"], r["track"],
                              int(r.get("size") or 0), r["mkv_id"])
            except Exception:                                    # noqa: BLE001
                continue
            if _is_really_signs(sh):
                cleared += 1
                INSPECT_STATE["cleared"] = cleared
    finally:
        INSPECT_STATE.update(running=False, now="", done=len(rows),
                             last_run=time.time())
        _CACHE["at"] = 0.0            # the next read re-judges with what we know
    if cleared:
        joblog.log(f"subtitle titles: read {len(rows)} flagged track(s) and "
                   f"cleared {cleared} - signs, karaoke or typesetting rather "
                   f"than mislabelled dialogue", "info")
    return {"ok": True, "read": len(rows), "cleared": cleared}


# THE SAME FOUR WORDS THE HARDSUB CHECK USES, because they are answering the
# same question about a different source: what kind of subtitle is this?
DIALOGUE, HYBRID, SIGNS, NONE = "dialogue", "hybrid", "signs", "none"
KINDS = (DIALOGUE, HYBRID, SIGNS, NONE)
KIND_WORDS = {DIALOGUE: "dialogue", HYBRID: "dialogue + signs",
              SIGNS: "signs or songs", NONE: "empty"}
# Below this many plain dialogue lines a minute, a "signs" title is telling
# the truth. Measured: every genuine signs track read so far sits at zero.
SIGNS_MAX = 2.0


def kind_of(sh: dict, minutes: float) -> dict:
    r"""What this track carries, and how sure, from its shape.

    THE DIALOGUE RATE IS THE WHOLE TEST. Plain lines in non-sign styles per
    minute: signs tracks have none, dialogue tracks run at the speech band,
    and a track carrying both is dialogue with sign styles beside it. The
    score is how far inside the speech band that rate sits - the same idea
    the cue rate used, on the number that actually means something.
    """
    if not sh:
        return {"kind": "", "rate": 0.0, "score": 0, "why": "not read yet"}
    if sh.get("chosen"):
        return {"kind": sh["chosen"], "rate": 0.0, "score": 100,
                "why": f"set by hand to {KIND_WORDS.get(sh['chosen'], sh['chosen'])}",
                "chosen": True}
    # THE LYRICS DO NOT VOTE, WHERE NUARR KNOWS WHERE THEY ARE.
    #
    # A song subtitle carries no \pos and sits in a style called Default or
    # Romaji, so it is "a plain line in a non-sign style" by both halves of
    # this test - and a track that is only an opening and an ending therefore
    # read as dialogue. Restricting the count to lines outside the theme, over
    # the runtime outside the theme, removes them from both sides of the rate.
    # Measured: Gundam 00's 'Signs/Titles/OP & ED Karaoke' track has 37 events
    # of which 73% are in the theme, against 347 in the dialogue track of
    # which 8% are.
    #
    # ONLY WHERE IT IS KNOWN. plain_out is NULL for the ~85% of files that
    # name no OP/ED chapter, and the fallback is the number this always used.
    # A zero read out of a NULL would say "no dialogue outside the theme"
    # about a file whose theme was never found, in the one place that decides
    # whether a subtitle track is kept.
    plain = int(sh.get("plain") or 0)
    mins = max(1.0, float(minutes or 0))
    restricted = sh.get("plain_out")
    if restricted is not None:
        oped_s = float(sh.get("oped_s") or 0.0)
        if oped_s > 0:
            mins = max(1.0, mins - oped_s / 60.0)
        plain = int(restricted)
    rate = plain / mins
    if not sh.get("events"):
        return {"kind": NONE, "rate": 0.0, "score": 100,
                "why": "no events at all"}
    if rate <= SIGNS_MAX:
        # Sure in proportion to how empty of dialogue it is.
        score = int(round(100 - (rate / SIGNS_MAX) * 40))
        return {"kind": SIGNS, "rate": round(rate, 1), "score": score,
                "why": (f"{plain} plain dialogue line{'' if plain == 1 else 's'} "
                        f"over {mins:.0f} minutes - a sign sheet")}
    mid = (SPEECH_LO + SPEECH_HI) / 2.0
    half = (SPEECH_HI - SPEECH_LO) / 2.0
    if rate < SPEECH_LO:
        # Between "no dialogue" and "people talking": unsure, and says so.
        score = int(round(30 + (rate - SIGNS_MAX) / (SPEECH_LO - SIGNS_MAX) * 30))
        return {"kind": DIALOGUE if not sh.get("signish") else HYBRID,
                "rate": round(rate, 1), "score": score,
                "why": (f"{rate:.1f} plain dialogue lines a minute - too many "
                        f"for a sign sheet, too few for a whole episode's "
                        f"speech; a partial or sparse dialogue track")}
    score = int(round(100 - (abs(rate - mid) / half) * 40))
    kind = HYBRID if sh.get("signish") else DIALOGUE
    return {"kind": kind, "rate": round(rate, 1),
            "score": int(max(0, min(100, score))),
            "why": (f"{rate:.1f} plain dialogue lines a minute, the cadence "
                    f"of people talking"
                    + (" - with sign styles beside them" if kind == HYBRID
                       else ""))}


def forget(file_ids) -> int:
    """Drop every shape read from bytes that are no longer there."""
    ids = [int(i) for i in (file_ids or []) if i]
    if not ids:
        return 0
    try:
        _inspect_init()
        with cursor() as cur:
            qs = ",".join("?" * len(ids))
            cur.execute(f"DELETE FROM subtitle_shape "
                        f" WHERE file_id IN ({qs})", ids)
            n = cur.rowcount or 0
        _CACHE["at"] = 0.0                    # the next read re-scans
        return n
    except Exception:                                            # noqa: BLE001
        return 0


def ack(file_id: int, track: int, on: bool = True) -> dict:
    """Take a settled row off the list, or put it back."""
    try:
        _inspect_init()
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subtitle_shape(file_id,track,size,at,acked) "
                "VALUES(?,?,0,?,?) ON CONFLICT(file_id,track) DO UPDATE SET "
                "  acked=excluded.acked",
                (int(file_id), int(track), time.time(), 1 if on else 0))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": str(e)[:160]}
    _CACHE["at"] = 0.0
    return {"ok": True, "why": "left as it is" if on else "back on the list"}


def set_kind(file_id: int, track: int, kind: str) -> dict:
    """Record what a person says this track carries. Outranks the reading."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "why": f"{kind!r} is not a kind"}
    try:
        _inspect_init()
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subtitle_shape(file_id,track,size,at,chosen,acked) "
                "VALUES(?,?,0,?,?,0) ON CONFLICT(file_id,track) DO UPDATE SET "
                "  chosen=excluded.chosen, acked=0",
                (int(file_id), int(track), time.time(), kind))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": str(e)[:160]}
    _CACHE["at"] = 0.0
    return {"ok": True, "kind": kind,
            "why": f"set to {KIND_WORDS.get(kind, kind)}"}


def _is_really_signs(sh: dict) -> bool:
    """Kept for the callers that still ask the yes/no form."""
    return bool(sh) and kind_of(sh, 24.0).get("kind") == SIGNS


def scan(limit: int = 0) -> dict:
    r"""Every contradicted subtitle title in the library, from stored probes."""
    rows, checked = [], 0
    _CACHE.update(running=True, done=0, total=0, now="", t0=time.time(), t1=0.0)
    try:
        with cursor() as cur:
            got = cur.execute(
                "SELECT f.id, f.path, f.title, f.library, f.season, "
                "       f.episode, f.size, f.first_seen, p.json "
                "  FROM files f JOIN file_probes p ON p.file_id = f.id "
                " WHERE f.state NOT IN ('deleted','duplicate') "
                + (" LIMIT ?" if limit else ""),
                ((int(limit),) if limit else ())).fetchall()
        _CACHE["total"] = len(got)
        for i, r in enumerate(got, 1):
            checked += 1
            if i % 200 == 0:
                _CACHE.update(done=i, now=os.path.basename(r["path"] or ""))
            try:
                probe = json.loads(r["json"] or "{}")
            except Exception:                                # noqa: BLE001
                continue
            for row in _rows_from_probe(r["path"] or "", probe):
                row.update(file_id=r["id"], path=r["path"],
                           title=r["title"] or "", library=r["library"] or "",
                           size=r["size"] or 0, label=_label(r),
                           added=r["first_seen"] or 0.0)
                rows.append(row)
    finally:
        _CACHE.update(running=False, done=checked, now="", t1=time.time())
    # ---- the second pass, consulted but never RUN from here -------------
    # SCANNING MUST STAY A QUERY. The first version read the events inline and
    # turned a 0.9-second endpoint into one that did eighty demuxes before it
    # answered - a page that took minutes to load, which is a worse fault than
    # the false positive it was fixing. So the shapes are looked up here and
    # produced by inspect_some() on its own clock; a candidate nobody has read
    # yet stays listed, marked as unread rather than silently trusted.
    # A CANDIDATE IS NOT A FINDING UNTIL SOMEBODY HAS READ IT.
    #
    # This is not caution, it is what the first eighty reads showed: 45 of 45
    # candidates cleared, and so did BLUE LOCK S02E11 - the file this check was
    # built on. Its "Forced (Signs only)" track has 466 events in 5 styles with
    # 15% of them positioned, while the "Dubtitle (SDH)" track beside it has
    # 615 events in ONE style with none positioned. That is the difference
    # between a sign sheet and dialogue, and the cue rate cannot see it: an
    # opening song is two events per lyric and an animated title card is one
    # event per frame, so a signs track reaches a talking cadence without a
    # word of dialogue in it.
    #
    # So the rate now produces CANDIDATES, and nothing is offered as
    # correctable until its events have been read. Leaving them actionable
    # would have renamed 82 correctly-labelled signs tracks to "English".
    dropped, unread = [], 0
    known = _shapes_for([r["file_id"] for r in rows])
    for r in list(rows):
        sh = known.get((r["file_id"], r["track"]))
        # A row read before `plain` existed is unread for this purpose.
        if sh is None or (sh.get("plain") is None and not sh.get("chosen")):
            unread += 1
            r["unread"] = True
            r["rewritable"] = False        # nothing to press until it is read
            r["kind"] = ""
            r["kinds"] = [{"id": k, "word": KIND_WORDS[k]} for k in KINDS]
            continue
        # Read, and unreadable - not a candidate, and not asked again.
        if (sh.get("plain") or 0) == UNREADABLE:
            r["shape"] = sh.get("detail") or UNREADABLE_WHY
            dropped.append(r)
            continue
        v = kind_of(sh, r.get("minutes") or 24.0)
        r["read"] = True
        r["kind"] = v["kind"]
        r["chosen"] = bool(v.get("chosen"))
        r["rate"] = v["rate"]
        r["kinds"] = [{"id": k, "word": KIND_WORDS[k]} for k in KINDS]
        r["shape"] = sh.get("detail") or ""
        r["kind_why"] = v["why"]
        # THE SCORE IS THE VERDICT'S, and the sure column shows it: how
        # certain the read is about what the track carries, in the same
        # 0-100 the other panel uses and coloured the same way.
        r["sure"] = int(v["score"])
        # A signs title over a signs track is not a finding. Anything else the
        # read settled - dialogue or hybrid under a signs claim - is one, and
        # only the safe titles among those can be rewritten.
        if v["kind"] in (SIGNS, NONE):
            # A CHOICE YOU MADE IS NOT A ROW THAT NEVER EXISTED. The reader
            # deciding a track is signs means there was never a finding, and
            # the row goes. YOU deciding it is a different thing: the row
            # leaves the list of questions but the choice has to stay
            # reachable, or setting a kind by hand is a one-way door with no
            # handle on the far side. Kept, flagged, hidden behind a count.
            if v.get("chosen"):
                r["settled"] = True
                r["acked"] = bool(sh.get("acked"))
                r["rewritable"] = False
                continue
            dropped.append(r)
            continue
        if v["kind"] == HYBRID:
            # The honest title for a track carrying both is neither word alone.
            base = _LANG_NAME.get((r.get("lang") or "").lower(), "")
            r["new"] = f"{base} (dialogue + signs)" if base else "Dialogue + Signs"
        # SAFE, OR SAID BY YOU. The safe-title rule exists because nuarr
        # cannot regenerate what a fansub group wrote into a title, so a
        # DETECTOR'S opinion is never allowed to overwrite one. A kind you set
        # by hand is not the detector's opinion - you have looked and made the
        # call - so the correction is offered, with exactly what it would
        # become written in the row for you to approve or not.
        r["rewritable"] = ((_rewritable(r.get("old") or "") or v.get("chosen"))
                           and bool(r.get("new"))
                           and (r["new"] or "").lower() != (r.get("old") or "").lower())
        r["unsafe"] = bool(v.get("chosen")) and not _rewritable(r.get("old") or "")
    if dropped:
        gone = {id(r) for r in dropped}
        rows = [r for r in rows if id(r) not in gone]
    _CACHE["cleared"] = [
        {"label": r.get("label") or "", "old": r.get("old") or "",
         "why": r.get("shape") or ""} for r in dropped[:40]]
    looked = len(rows) + len(dropped) - unread

    rows.sort(key=lambda r: (bool(r.get("settled")),
                             not r.get("rewritable"), r.get("sure", 0)))
    data = {"rows": rows, "checked": checked, "at": time.time(),
            "cleared": len(dropped), "inspected": looked, "unread": unread,
            "cleared_rows": _CACHE["cleared"],
            "took": round(_CACHE["t1"] - _CACHE["t0"], 1),
            "fixable": sum(1 for r in rows if r["rewritable"])}
    _CACHE.update(at=time.time(), data=data)
    if mode() == "auto":
        # ONLY THE SURE ONES, AND ONLY THE SAFE ONES. Two filters, and they
        # are about different things: `rewritable` is whether a replacement
        # can be written without losing what a group put there, `sure` is
        # whether the finding is right at all.
        # `rewritable` is False for anything unread, so this cannot reach a
        # candidate nobody has looked at - which is the whole point.
        want = [r for r in rows
                if r.get("rewritable") and r.get("read")
                and r.get("sure", 0) >= sure_at()]
        if want:
            out = fix(want)
            data["auto_fixed"] = out.get("fixed") or 0
            joblog.log(f"subtitle titles: corrected {out.get('fixed') or 0} "
                       f"title(s) on its own - at or above the {sure_at()}% "
                       f"line", "info")
            # fix() invalidates the cache; re-read so the page sees the result
            # rather than the findings that have just been corrected.
            _CACHE.update(at=time.time(), data=data)
    return data


def _fix_file(path: str, edits: list) -> tuple[bool, str]:
    """One mkvpropedit call per file, however many tracks it corrects."""
    if not os.path.exists(path):
        return False, "not on disk"
    cmd = [_mkvpropedit(), path]
    for e in edits:
        cmd += ["--edit", f"track:s{int(e['track'])}",
                "--set", f"name={e['new']}"]
    try:
        r = _quiet_run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:                                   # noqa: BLE001
        return False, str(e)[:160]
    if r.returncode >= 2:
        return False, (r.stderr or r.stdout or "mkvpropedit failed").strip()[:200]
    return True, ", ".join(f"s{e['track']}: {e['old']!r} -> {e['new']!r}"
                           for e in edits)


def fix(rows: list | None = None) -> dict:
    r"""Correct the titles that are safe to correct.

    ONLY THE REWRITABLE ONES, EVER. A title carrying a fansub group's name is
    reported and never touched - the finding is still true and worth seeing,
    but nuarr cannot write a replacement that keeps what the group put there.
    """
    if rows is None:
        rows = (cached().get("rows") or [])
    todo: dict = {}
    for r in rows:
        if not r.get("rewritable"):
            continue
        todo.setdefault(r["path"], []).append(r)
    ok = failed = 0
    fails: list = []
    _CACHE.update(fixing={"done": 0, "total": len(todo)}, failures=[])
    for i, (path, edits) in enumerate(todo.items(), 1):
        _CACHE["fixing"] = {"done": i - 1, "total": len(todo),
                            "now": os.path.basename(path)}
        good, why = _fix_file(path, edits)
        if good:
            ok += len(edits)
            _restamp(edits[0]["file_id"], path, edits)
            # The title a viewer reads has changed. Plex caches it; so does
            # the arr. Neither was ever told.
            try:
                from . import notify
                notify.file_changed([int(edits[0]["file_id"])],
                                    why="nuarr corrected a subtitle track title",
                                    rename=False, system="subtitle titles")
            except Exception:                                    # noqa: BLE001
                pass
        else:
            failed += len(edits)
            if len(fails) < 30:
                fails.append({"path": path, "why": why})
    _CACHE.update(fixing=None, failures=fails,
                  last_fix={"at": time.time(), "ok": ok, "failed": failed})
    if ok or failed:
        joblog.log(f"subtitle titles: corrected {ok} track(s)"
                   + (f", {failed} could not be written" if failed else ""),
                   "warn" if failed else "ok")
    _CACHE["at"] = 0.0                 # the next read re-scans
    return {"ok": True, "fixed": ok, "failed": failed, "failures": fails}


def _restamp(file_id: int, path: str, edits: list) -> None:
    """Put the new titles into the stored probe, so the next scan agrees.

    Without this the finding survives its own correction: the file on disk is
    right, the probe nuarr reads is stale, and the row comes back on the next
    pass looking exactly as it did.
    """
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
            if not r:
                return
            probe = json.loads(r["json"] or "{}")
            s_i = 0
            for s in (probe.get("streams") or []):
                if s.get("codec_type") != "subtitle":
                    continue
                s_i += 1
                for e in edits:
                    if int(e["track"]) == s_i:
                        s.setdefault("tags", {})["title"] = e["new"]
            cur.execute("UPDATE file_probes SET json=? WHERE file_id=?",
                        (json.dumps(probe), int(file_id)))
    except Exception:                                        # noqa: BLE001
        pass


_RESCAN = threading.Lock()


def _rescan_behind() -> None:
    try:
        scan()
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        _RESCAN.release()


def cached() -> dict:
    """The last scan - served as it stands, and re-run BEHIND the request.

    THE PAGE MUST NEVER PAY FOR THE SCAN. A scan parses 39,000 stored probes
    and takes two seconds; the read pass invalidates the cache every few
    files; and the panel polls every couple of seconds while a pass runs.
    Put together, that was a 2-second endpoint on a 1.5-second timer. So a
    stale answer is served at once and one rescan starts behind it - the same
    rule the audio-language and arr-language panels follow.
    """
    d = _CACHE.get("data")
    if d and time.time() - _CACHE["at"] < EVERY_S:
        return d
    if d:
        if _RESCAN.acquire(blocking=False):
            threading.Thread(target=_rescan_behind, daemon=True,
                             name="subtitletitle-rescan").start()
        return d
    with _RESCAN:
        d = _CACHE.get("data")
        return d if d else scan()


def refresh() -> dict:
    """A scan now, waited for - the scheduled pass and the buttons."""
    _CACHE["at"] = 0.0
    with _RESCAN:
        return scan()


def attention() -> dict | None:
    """What the Attention tile should say, or nothing.

    Silent in auto, like the audio-title check: a finding that is already
    being handled is not a thing that needs somebody.
    """
    if mode() == "auto":
        return None
    try:
        rows = (cached().get("rows") or [])
    except Exception:                                        # noqa: BLE001
        return None
    if not rows:
        return None
    return {"what": "subtitle titles", "n": len(rows),
            "note": "a subtitle track is labelled as something it is not",
            "goto": "/settings#subs"}


def progress() -> dict:
    """The read pass, measured. Same fields the hardsub panel draws from."""
    st = INSPECT_STATE
    now = time.time()
    el = (now - st["t0"]) if (st["running"] and st["t0"]) else 0.0
    rate = (st["done"] / el) if (el > 0.5 and st["done"]) else 0.0
    eta = ((st["total"] - st["done"]) / rate) if rate else 0.0
    each = st.get("secs_each") or 0.0
    d = _CACHE.get("data") or {}
    unread = int(d.get("unread") or 0)
    out = {"running": st["running"], "now": st["now"], "done": st["done"],
           "total": st["total"], "elapsed": round(el, 1),
           "rate": round(rate, 3), "eta": round(eta),
           "secs_each": round(each, 2), "cleared": st.get("cleared") or 0,
           "last_run": st.get("last_run") or 0.0,
           "last_took": round(st.get("last_took") or 0, 1),
           "last_read": st.get("last_read") or 0,
           "last_cleared": st.get("last_cleared") or 0,
           "runs": st.get("runs") or 0, "yielded": st.get("yielded") or "",
           "last_error": st.get("last_error") or "",
           "per_run": PER_RUN, "cycle_s": CYCLE_S, "unread": unread,
           # How long until every candidate has been read, at the pace the
           # last passes really achieved and the cadence they really run on.
           "backlog_eta": round(max((unread / max(1, PER_RUN)) * CYCLE_S,
                                    unread * each if each else 0))
                          if unread else 0,
           "next_run": 0.0}
    # AND WHAT THE RUNNER KNOWS BETTER. INSPECT_STATE is the manual button's
    # now; the reading that runs all day is the shared runner's, and a panel
    # reading the wrong dict says "idle" while the disks are going.
    try:
        from . import idle as _idle
        out = _idle.merge_stats(KEY, out)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import schedules
        for r in (schedules.snapshot() or {}).get("rows", []):
            if r.get("key") == SCHED_KEY:
                out["next_run"] = r.get("next_run") or 0.0
                out["runs"] = r.get("runs") or out["runs"]
                break
    except Exception:                                            # noqa: BLE001
        pass
    return out
