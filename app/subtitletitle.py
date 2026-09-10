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
_STYLE_SIGN = re.compile(
    r"\b(sign|signs|op|ed|oped|karaoke|kara|title|credit|credits|note|"
    r"caption|typeset|logo|insert)\b", re.I)
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
        for col, decl in (("plain", "INTEGER"), ("chosen", "TEXT")):
            try:
                cur.execute(f"ALTER TABLE subtitle_shape ADD COLUMN {col} {decl}")
            except Exception:                                    # noqa: BLE001
                pass


def _read_events(path: str, mkv_track_id: int) -> dict | None:
    r"""Pull one text subtitle track out and describe its SHAPE.

    Never its words - this is about where the lines are and what the styles
    are called, which is the part the cue count could not see.
    """
    if not os.path.exists(path):
        return None
    out = os.path.join(os.environ.get("TEMP") or ".",
                       f".nuarr-shape-{int(time.time()*1000)}.txt")
    try:
        r = _quiet_run([_mkvextract(), "tracks", path,
                        f"{int(mkv_track_id)}:{out}"],
                       capture_output=True, text=True, timeout=180)
        if r.returncode >= 2 or not os.path.exists(out):
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
        return {"events": srt_n, "styles": 1 if srt_n else 0, "plain": srt_n,
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
    for ln in lines:
        parts = ln.split(",", 9)
        if len(parts) < 10:
            continue
        st = parts[3].strip()
        styles[st] = styles.get(st, 0) + 1
        if _POSITIONED.search(parts[9]):
            positioned += 1
        elif not _STYLE_SIGN.search(st or ""):
            plain += 1
    n = max(1, len(lines))
    named = sorted(s for s in styles if _STYLE_SIGN.search(s or ""))
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
    return {"events": len(lines), "styles": len(styles), "plain": plain,
            "pos_pct": round(share * 100, 1), "signish": len(named),
            "detail": "; ".join(bits)}


def shape_of(file_id: int, path: str, track: int, size: int,
             mkv_track_id: int) -> dict | None:
    """The cached shape of one track, reading it only when it is not known."""
    try:
        _inspect_init()
        with cursor() as cur:
            r = cur.execute(
                "SELECT * FROM subtitle_shape WHERE file_id=? AND track=? "
                "  AND size=?", (int(file_id), int(track), int(size))).fetchone()
        # A row read before `plain` existed is a row that has to be read
        # again - it cannot answer the question that now decides everything.
        if r and r["plain"] is not None:
            return dict(r)
    except Exception:                                            # noqa: BLE001
        return None
    got = _read_events(path, mkv_track_id)
    if got is None:
        return None
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subtitle_shape(file_id,track,size,at,events,"
                "  styles,pos_pct,signish,detail,plain) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id,track) DO UPDATE SET size=excluded.size, "
                "  at=excluded.at, events=excluded.events, "
                "  styles=excluded.styles, pos_pct=excluded.pos_pct, "
                "  signish=excluded.signish, detail=excluded.detail, "
                "  plain=excluded.plain",
                (int(file_id), int(track), int(size), time.time(),
                 got["events"], got["styles"], got["pos_pct"], got["signish"],
                 got["detail"][:300], got.get("plain") or 0))
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
                for r in cur.execute(
                        f"SELECT * FROM subtitle_shape "
                        f" WHERE file_id IN ({qs})", chunk):
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


async def watch() -> None:
    """Re-scan and read a handful, on a schedule, yielding to the gate."""
    import asyncio
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "Subtitle titles", "Subtitles", CYCLE_S,
            what=(f"Compares each subtitle title against the cue count in "
                  f"its stored probe, then reads the actual events of "
                  f"anything that looks wrong - {PER_RUN} a pass - because "
                  f"the count alone cannot tell karaoke from dialogue."))
    except Exception:                                            # noqa: BLE001
        pass
    await asyncio.sleep(300)
    while True:
        try:
            await asyncio.to_thread(refresh)
            await inspect_paced(PER_RUN)
        except Exception as e:                                   # noqa: BLE001
            INSPECT_STATE["last_error"] = f"{type(e).__name__}: {e}"
        await asyncio.sleep(CYCLE_S)


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
    plain = int(sh.get("plain") or 0)
    mins = max(1.0, float(minutes or 0))
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


def set_kind(file_id: int, track: int, kind: str) -> dict:
    """Record what a person says this track carries. Outranks the reading."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "why": f"{kind!r} is not a kind"}
    try:
        _inspect_init()
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subtitle_shape(file_id,track,size,at,chosen) "
                "VALUES(?,?,0,?,?) ON CONFLICT(file_id,track) DO UPDATE SET "
                "  chosen=excluded.chosen",
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
            dropped.append(r)
            continue
        if v["kind"] == HYBRID:
            # The honest title for a track carrying both is neither word alone.
            base = _LANG_NAME.get((r.get("lang") or "").lower(), "")
            r["new"] = f"{base} (dialogue + signs)" if base else "Dialogue + Signs"
        r["rewritable"] = (_rewritable(r.get("old") or "")
                           and bool(r.get("new"))
                           and (r["new"] or "").lower() != (r.get("old") or "").lower())
    if dropped:
        gone = {id(r) for r in dropped}
        rows = [r for r in rows if id(r) not in gone]
    _CACHE["cleared"] = [
        {"label": r.get("label") or "", "old": r.get("old") or "",
         "why": r.get("shape") or ""} for r in dropped[:40]]
    looked = len(rows) + len(dropped) - unread

    rows.sort(key=lambda r: (not r.get("rewritable"), r.get("sure", 0)))
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
