r"""nuarr - where the opening and the ending are, when the file says so.

WHY THIS IS A MODULE AND NOT A COLUMN. Two readers want the same fact for
opposite reasons and neither of them is about chapters: the language listener
wants to NOT sample the theme song, and the picture reader wants to NOT sample
opening credits. A third caller will want it for skip logic. One place decides
what counts as an OP, or there will be three lists of words that disagree -
which is the mistake langkey.py exists to have already made once.

WHERE IT COMES FROM. The release group. Matroska carries named chapters and a
lot of anime releases label them exactly: Undead Unluck S01E06 ships

    Prologue · Part A · OP · Part B · Part C · Part D · ED · Preview

That is testimony, not inference - somebody who had the episode in front of
them wrote it down, to the frame. nuarr had never read it, because every
ffprobe call in the codebase was `-show_streams -show_format` and chapters
only come back when you ask for them.

COVERAGE, MEASURED, so nobody builds on a number they assumed: 180 files
sampled across every library, 63% carry chapters at all and 15% name an OP or
an ED. In Anime Shows it is 30%. Most of the rest are 'Chapter 1'...'Chapter
N', which say nothing. So this answers for a minority of the library and has
to say so - see spans(), which returns an empty list for "no chapters" and
None for "never looked". Those are different facts and collapsing them is the
bug this whole codebase keeps finding.

AND IT DOES NOT TRUST THEM BLINDLY. Plex's cloud markers - the other possible
source - had 3rd Rock from the Sun S01E02 marked with a 7.7-minute "intro" on
a 22-minute episode. A chapter list can be wrong the same way. An OP that
claims a third of the runtime is not an OP, and sane() drops it rather than
letting one bad marker blind a reader to a third of the file.
"""
from __future__ import annotations

import json
import os
import subprocess

from .config import NO_WINDOW
from .db import cursor

# WHAT COUNTS AS AN OPENING OR AN ENDING. Matched case-insensitively against
# the WHOLE chapter title after stripping, not as a substring - "Part A" must
# not match on "a", and a chapter actually called "Opening Statements" in a
# courtroom drama is not a theme song. Release groups are terse here; the long
# tail is not worth the false positives.
OPED_TITLES = {
    "op", "ed", "oped", "opening", "ending", "intro", "outro",
    "opening credits", "ending credits", "credits", "end credits",
    "preview", "next episode", "next", "nc op", "nc ed", "ncop", "nced",
    "opening theme", "ending theme", "theme", "title sequence",
}

# TITLES THAT MIGHT NOT MEAN THE THEME AT ALL.
#
# Sasaki and Peeps S1E6 is chaptered Intro 0-130s, OP 130-220s, ED 1330s. The
# OP is at 130 seconds, so that 'Intro' is the COLD OPEN - a scene, with
# dialogue in it. Other groups do use 'Intro' for the theme itself. The title
# alone cannot tell you which, and the cost of being wrong is not the same
# for both readers:
#
#   moving a listening window off a cold open costs nothing - the sample is
#   taken from elsewhere and the language is the same
#
#   subtracting a cold open from the subtitle line count removes REAL
#   dialogue from the number that decides dialogue-or-signs, and a signs
#   verdict is what gets a track dropped as a duplicate
#
# So callers who are about to act irreversibly ask with strict=True and get
# only the titles that can mean nothing but a theme.
MAYBE_NOT_A_THEME = {"intro", "outro", "credits", "next", "preview"}

# A SINGLE BLOCK MAY NOT CLAIM MORE THAN THIS MUCH OF THE FILE. An opening is
# ninety seconds; an ending is ninety seconds. Plex marked a 22-minute episode
# of 3rd Rock with a 7.7-minute "intro", and a reader that believed it would
# have refused to sample a third of the episode. 15% of a 24-minute episode is
# 3.6 minutes, which is generous for a 90-second theme and still catches the
# absurd ones.
MAX_SHARE = 0.15

# And all of them together may not claim more than this. Two themes and a
# preview is about 20%; anything past a third means the titles are being read
# wrong and the safe answer is to use none of them.
MAX_TOTAL_SHARE = 0.34


def spans(data: dict | None, strict: bool = False) -> list | None:   # noqa: ARG001
    r"""[(start_s, end_s, title)] for the OP/ED blocks in a probe.

    None means NOBODY LOOKED - the probe predates chapters being asked for,
    so there is no evidence either way. [] means the file was read and names
    no opening or ending. Callers must tell those apart: the first is a gap
    in what nuarr knows and the second is a fact about the file.

    Every named block comes back; deciding which of the ambiguous ones are
    really themes needs the runtime, so it happens in sane().
    """
    if not data or "chapters" not in data:
        return None
    out = []
    for c in (data.get("chapters") or []):
        title = ((c.get("tags") or {}).get("title") or "").strip()
        low = title.lower()
        if low not in OPED_TITLES:
            continue
        try:
            a = float(c.get("start_time") or 0)
            b = float(c.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        if b > a >= 0:
            out.append((a, b, title))
    return out


# An ambiguous block starting within this much of the top is an opening
# rather than a scene; one starting after this much of the way through is an
# ending. Between the two it is neither, whatever it is called.
HEAD_S = 120.0
TAIL_SHARE = 0.80


def sane(blocks: list | None, duration: float, strict: bool = False) -> list:
    r"""Drop blocks too big to be a theme, and give up if too many are.

    strict=True also resolves the ambiguous titles - see MAYBE_NOT_A_THEME -
    by WHERE THEY SIT, because the word cannot do it and the position can:

        Sasaki and Peeps   Intro 0-130s, OP 130-220s
            something unambiguous claims to be the theme AFTER this Intro, so
            the Intro is the cold open before it. It keeps its dialogue.

        Farming Life       Intro 3-91s, Credits 1336-1432s of 1432s
            nothing else claims the theme, the Intro starts at the top and
            runs ninety seconds, the Credits are the last ninety-six seconds.
            Both are what they look like.

    Callers about to remove something irreversible ask strictly; a caller
    only moving a sample point does not need to.
    """
    if not blocks or duration <= 0:
        return []
    keep = [(a, b, t) for a, b, t in blocks
            if (b - a) <= duration * MAX_SHARE]
    if strict:
        sure = [(a, b, t) for a, b, t in keep
                if (t or "").strip().lower() not in MAYBE_NOT_A_THEME]
        out = []
        for a, b, t in keep:
            if (t or "").strip().lower() not in MAYBE_NOT_A_THEME:
                out.append((a, b, t))
                continue
            if a >= duration * TAIL_SHARE:
                out.append((a, b, t))            # an ending
                continue
            if a <= HEAD_S and not any(s0 > a for s0, _e, _tt in sure):
                out.append((a, b, t))            # an opening, nothing after it
                continue
            # Anywhere else, or with a real theme following it, this is a
            # scene and its lines are somebody's dialogue.
        keep = out
    if sum(b - a for a, b, _t in keep) > duration * MAX_TOTAL_SHARE:
        return []
    return keep


def in_any(blocks: list, start: float, end: float) -> bool:
    """Does [start, end) touch any block?"""
    return any(start < b and end > a for a, b, _t in blocks)


def read(path: str, timeout: float = 45.0) -> list | None:
    """Ask ffprobe for this one file's chapters. A header read, ~78 ms."""
    from . import jobs
    try:
        p = subprocess.run(
            [jobs._ffprobe_exe(), "-v", "error", "-print_format", "json",
             "-show_chapters", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=NO_WINDOW)
        return json.loads(p.stdout or "{}").get("chapters") or []
    except Exception:                                        # noqa: BLE001
        return None


def for_file(file_id: int, path: str = "", live: bool = False,
             strict: bool = False) -> list | None:
    r"""The OP/ED blocks for one file, from the stored probe where possible.

    live=True falls back to reading the file when the cached probe predates
    chapters. That is a disk seek, so only callers about to open the file
    anyway should ask for it - the listener already has the file open.
    """
    data = None
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if r and r["json"]:
            data = json.loads(r["json"])
    except Exception:                                        # noqa: BLE001
        data = None
    got = spans(data, strict)
    if got is not None:
        return got
    if not live or not path or not os.path.exists(path):
        return None                      # nobody looked, and not looking now
    ch = read(path)
    if ch is None:
        return None
    return spans({"chapters": ch}, strict)


def dodge(fracs, duration: float, blocks: list, secs: float = 30.0,
          taken=()) -> list:
    r"""Move sample points off the theme song, keeping their spread.

    WHY MOVE RATHER THAN DROP. The windows are spread across the file on
    purpose - five points that agree are evidence BECAUSE they are far apart.
    Dropping the one that lands on the opening would leave four, and the
    agreement rule would then be deciding on less. So each one slides to the
    nearest clear water instead, and the spread survives.

    MEASURED, on dual-audio anime with a named OP/ED: a routine window landing
    inside the theme refuses 21.9% of the time against 1.9% when every window
    misses it. Eleven and a half times. The mechanism is plain once seen - an
    opening is a Japanese song over an English dub, so the window disagrees
    with the rest and the verdict comes back "windows disagree: en, ja", which
    is what 3,124 of this library's 3,819 refusals say.

    AND NO TWO OF THEM LAND ON THE SAME SECOND. `taken` is the points already
    spoken for - the first look's, when the second look is being placed. Both
    passes push a blocked window to the first clear moment after the block,
    which is the SAME moment, and _judge counts windows: the same thirty
    seconds would have voted twice and been reported as two independent
    agreements. Measured on Undead Unluck S01E06, where first @0.10 and second
    @0.05 both came out at 0.1250.

    Returns fractions, in the same shape it was given, so the caller's loop
    does not change.
    """
    if not blocks or duration <= 0:
        return list(fracs)
    # Seconds, so "the same spot" means what it sounds like rather than
    # depending on how long the file is.
    used = [duration * float(t) for t in (taken or ())]

    def clear(x: float) -> bool:
        return (not in_any(blocks, x, x + secs)
                and all(abs(x - u) >= secs for u in used))

    out = []
    for f in fracs:
        a = duration * float(f)
        if clear(a):
            out.append(float(f))
            used.append(a)
            continue
        # Forward past the end of whatever it hit, then backward if that
        # runs off the end of the file. A window must still fit.
        moved = None
        for _ in range(len(blocks) + len(used) + 2):
            hit = [b for s0, b, _t in blocks if a < b and a + secs > s0]
            if hit:
                a = max(hit) + 1.0
                continue
            near = [u for u in used if abs(a - u) < secs]
            if near:
                a = max(near) + secs + 1.0
                continue
            break
        if a + secs <= duration and clear(a):
            moved = a
        if moved is None:
            a = duration * float(f)
            for _ in range(len(blocks) + len(used) + 2):
                hit = [s0 for s0, b, _t in blocks if a < b and a + secs > s0]
                if hit:
                    a = min(hit) - secs - 1.0
                    continue
                near = [u for u in used if abs(a - u) < secs]
                if near:
                    a = min(near) - secs - 1.0
                    continue
                break
            if a >= 0 and clear(a):
                moved = a
        # Nowhere clear: keep the original rather than inventing a point.
        # A file that is all theme song has no better answer than the one
        # it would have given anyway.
        out.append((moved / duration) if moved is not None else float(f))
        used.append(moved if moved is not None else duration * float(f))
    return out
