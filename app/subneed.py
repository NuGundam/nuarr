r"""nuarr - raws: films and episodes nobody in this house can follow.

WHAT THIS CHECK IS FOR
----------------------
Erik, on what he built it to do: "this system was intended to fix foreign
show/movies that get downloaded and only have their native lang - raws in
anime terms. If a user wants a sub lang because they don't understand the
native one and it doesn't have it, then nuarr is given the command to replace
it if in auto mode."

So the question is NOT "does this file have an English subtitle track". It is
"can anybody here understand this film", and it has two halves that have to be
asked in that order:

    is it spoken in a language you have?     -> then it is not a raw, whatever
                                                its subtitle tracks say
    no? then is there dialogue you can read?  -> a track, a file beside it, or
                                                the words burned into the
                                                picture. Signs and songs are
                                                not dialogue: they translate a
                                                shop front and leave the
                                                conversation alone.

A file that fails both is a raw. Nothing nuarr can do to those bytes produces
a subtitle - the planner cannot translate, the OCR has no picture to read and
the listener hears Japanese - so the only fix is a different release, which is
what the button does and what auto mode does without asking.

THE FILE THIS EXISTS FOR
------------------------
    The Villager of Level 999 (2026) - S01E08 - Incompatible
        [WEBDL-1080p][8bit][x264][AAC 2.0][JA]-ToonsHub.mkv

ffprobe on it, on disk, right now: one h264 video stream, one aac stream
tagged jpn. That is the whole file. No subtitle track, no sidecar beside it,
nothing burned into the picture. Every check nuarr has says this file is fine,
because every check nuarr has asks whether what is there is CORRECT - the
title matches the track, the audio matches the tag, the bytes decode. None of
them asks whether you can follow the story.

So the file sat at `done`, and the first person to find out was whoever
pressed play.

WHY THE VERDICT HAS FOUR VALUES AND NOT TWO
-------------------------------------------
This is the important part of the module and the reason it is written the way
it is.

`sub_facts` stores what each file carries, and `subscan._tracks_of()` builds
that list from the stored probe:

    r = cur.execute("SELECT json FROM file_probes WHERE file_id=?", ...)
    if not r:
        return out          # <- empty list, no error, no trace

A file nobody has probed produces an empty track list, which is written as
`n_tracks = 0` with `err = ''`. It is byte-for-byte the row a file with no
subtitles produces. Measured on Erik's library before this module existed:

    sub_facts rows                                       41,309
    ... with no probe behind them                        23,052

    Anime Shows, "no subtitle track and no sidecar"       11,840
    ... of those, ever probed                                153
    ... never looked inside                               11,687

So a two-valued check reading sub_facts would have reported 11,840 files as
missing their English subtitles, and been able to justify 153 of them. In auto
mode that is eleven and a half thousand good files deleted and re-downloaded
on the strength of a question nobody asked.

`unknown` is therefore its own verdict, and it is inert: it is counted, it is
shown, and it can never reach the replace button - not by hand and not by the
unattended pass. A file has to have been looked inside before nuarr is allowed
to have an opinion about what is missing from it.

WHAT COUNTS AS SATISFIED, AND WHY EACH ONE IS ON THE LIST
---------------------------------------------------------
Every entry here is a way of being wrong that a narrower check would have
been. The verdict is `ok` if ANY of these hold:

  a text or picture subtitle track tagged with the language
        the direct case
  a sidecar beside the file named for the language
        .eng.srt is a subtitle for this purpose; the player does not care
        which side of the container it is on
  an untagged track, when the library keeps untagged tracks
        Erik's subtitle policy has keep_untagged on, because "many releases
        leave subs unlabelled". An untagged track may well BE the English
        subtitles. nuarr cannot tell without reading it, and a check that
        cannot tell must not accuse
  dialogue burned into the picture
        hardsub state `dialogue` or `hybrid`. The words are on screen. Not
        `signs` - signs and songs are not dialogue, and a file with only
        those still needs subtitles
  audio already in that language
        an English dub needs no English subtitles. Judging it missing would
        blocklist every dual-audio release in the library

The order matters only for the sentence shown to a person; any one of them
ends the question.

WHAT IT DOES ABOUT IT
---------------------
Nothing, by itself. Findings go to remedy.py like every other check's, which
owns the verbs, the hourly cap shared across all seven checks, and the ledger.
The verb here is `replace` and only `replace`: a requeue offers the file to
the planner, and the planner cannot conjure a subtitle track that was never
in the release. Auto mode is opt-in, per the same rule the integrity sweep and
the rule audit follow, because the only thing this can do is irreversible.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import joblog
from .config import SETTINGS
from .db import cursor

# The three verdicts. `unknown` is not a failure to decide - it is the correct
# answer to "does this file carry English subtitles" when nobody has opened
# the file. See the module docstring.
OK, MISSING, UNKNOWN = "ok", "missing", "unknown"

# AND A FOURTH, BECAUSE "MISSING" WAS DOING TWO JOBS.
#
# Erik: "if the rule say it wants subs then follow the rule but it shouldn't
# delete the file, it should replace the file if there is no way for the user
# to understand".
#
# Those are two different findings and the check had one word for both. A
# Japanese film with no English subtitle cannot be followed at all: that is
# worth deleting a release over. An episode of The Big Bang Theory with no
# English subtitle track is spoken in English - the rule asks for the
# subtitle and the viewer can follow the programme without it, and replacing
# the file over that is a loud answer to a quiet question.
#
# `want` is the quiet one. It is listed, it is counted, and no button acts on
# it: findings() hands only `missing` to the shared remedy. Measured when
# this went in: 2,819 files, nearly all of them English-audio television.
WANT = "want"

KIND = "subs/missing-language"

# THE SAME LINE hardsub.py DRAWS FOR "A DIALOGUE CADENCE", and deliberately
# the same number rather than one of this module's own: it is being used to
# ask the same question, and two thresholds for one question drift apart.
# hardsub counts frames with something bright LOW in the picture - where
# subtitles live - and calls 20% of them a dialogue rhythm.
PICTURE_MARKS_RATIO = 0.20

# THE SPEECH BAND, AND DELIBERATELY THE SAME NUMBERS subtitletitle MEASURED.
# A track running between these is people talking; below it is a sign sheet.
# Imported lazily so a circular import cannot take the module down, with the
# measured values as the fallback.
try:                                                     # pragma: no cover
    from .subtitletitle import SPEECH_LO, SPEECH_HI
except Exception:                                        # noqa: BLE001
    SPEECH_LO, SPEECH_HI = 6.0, 40.0

# THE COMMONEST ENGLISH FUNCTION WORDS, and this is a test for ENGLISH rather
# than a test for text.
#
# The picture reader OCRs the frames it thinks carry subtitles and keeps the
# words. Asked whether ANY words came back, the answer is useless: on
# Japanese-audio anime the OCR reads compression artifacts and on-screen
# Japanese and returns 'cng, eae, ener, rel, senna' and 'corrs, dil, hbencb,
# wet'. Asked whether any ENGLISH came back, the answer is exact. Measured
# over the 23 accused files that had any words at all:
#
#     13 with a function word   every one Spanish-audio Velvet, English
#                               burned in - 'are, barbara, doing, here,
#                               what, you'
#     10 without one            every one Japanese-audio Conan or Naruto,
#                               pure OCR noise
#
# A perfect split, landing exactly on the audio language. Function words are
# what makes it work: they are short, ubiquitous and high-frequency, so a
# line of dialogue almost always has one and noise almost never does.
#
# It speaks for English only. For any other required language this list is
# silent and the frame counts decide, because an English stopword says
# nothing about whether Korean is on the screen - see _is_english().
_ENGLISH_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "from", "by", "as", "is", "are", "was", "were", "be",
    "been", "am", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "should", "not", "no", "yes", "i", "you", "he", "she",
    "it", "we", "they", "me", "him", "her", "us", "them", "my", "your", "his",
    "its", "our", "their", "this", "that", "these", "those", "what", "who",
    "why", "how", "when", "where", "which", "here", "there", "now", "then",
    "all", "just", "like", "get", "got", "go", "going", "know", "think",
    "want", "need", "let", "come", "take", "see", "look", "tell", "say",
    "said", "make", "really", "very", "please", "thank", "thanks", "sorry",
    "hello", "okay", "ok", "about", "because", "out", "up", "down", "over",
    "into", "than", "too", "also", "some", "any", "more", "one", "two",
    "don", "doesn", "isn", "won", "didn", "dont",
}


def _is_english(lang: str) -> bool:
    return str(lang or "").strip().lower()[:3] in ("eng", "en")


def _english_hits(words: str) -> list:
    """Which of the OCRed words are English function words."""
    out = []
    for w in str(words or "").split(","):
        w = w.strip().lower()
        if w and w in _ENGLISH_STOP:
            out.append(w)
    return out

# ---- THE LADDER, AND HOW SURE EACH RUNG IS ---------------------------------
#
# verdict() is a ladder of early returns: the first rung that matches answers
# the question and nothing under it ever runs. That is the right shape for the
# logic and a poor shape for a number, because the answer came back with no
# record of WHICH rung produced it - so every `ok` in the library read as
# equally settled. They are not, and the gap is not small:
#
#     36,586  a track tagged with the language        the container says so
#      2,744  the audio is already in that language   the probe says so
#        127  an untagged track that might be the one a hope, deliberately given
#
# Three sentences, one column, and only two of them are facts. So each rung
# carries a name, a base sureness, and the measurement that earned it.
#
# THE BASE DOES NOT DRIFT. A weight that moved on its own would let a file
# change its mind overnight with nothing about the file having changed. What
# moves is the counting beside it - how many files rest on this rung now, and
# how many of its answers it has since had to take back - and that is read as
# an argument about the weight rather than as the weight. See scoring().
#
# The unknown rungs make no claim at all, so they carry no percentage. What
# they carry instead is a LEAN: of the files this rung once held that have
# since been decided, the share that turned out fine. That figure is empty
# until files move, which is the honest state of it - and the 362 sitting on
# `marks` are queued for a re-read right now, so it will fill.
RULES: dict = {
    "unread": {
        "says": UNKNOWN, "short": "not looked inside",
        "line": "nobody has opened this file yet",
        "rests": "every check under this one reads a probe, and a missing "
                 "probe makes all of them return nothing - which is "
                 "indistinguishable from the file carrying nothing. This is "
                 "the rung that stops 11,687 unread files being reported as "
                 "faulty."},
    "noread": {
        "says": UNKNOWN, "short": "subtitles not read",
        "line": "the subtitle reader has not read this file",
        "rests": "the probe is there but the track list is not, so there is "
                 "nothing yet to check the language against."},
    "track": {
        "says": OK, "sure": 99, "short": "tagged track",
        "line": "a track tagged with the language",
        "rests": "the container's own tag, written by whoever made the "
                 "release. It is the strongest thing on this ladder and it "
                 "is still a claim: a mislabelled track would pass here and "
                 "nothing in this library has caught one."},
    "side": {
        "says": OK, "sure": 99, "short": "subtitle file beside it",
        "line": "a subtitle file sits beside the video",
        "rests": "a .srt or .ass named for the language, next to the file. "
                 "Same standing as a tagged track and the same caveat."},
    "untagged": {
        "says": OK, "sure": 70, "short": "untagged track",
        "line": "an untagged track, which this library keeps",
        "rests": "releases leave subtitles unlabelled, which is why the "
                 "library keeps untagged tracks at all. Reading one to find "
                 "out costs an extract and an OCR, and until somebody does, "
                 "\"there is no English track\" is a guess wearing a fact's "
                 "clothes. The benefit of the doubt, given on purpose - and "
                 "this is the one rung where the number says so."},
    "burned": {
        "says": OK, "sure": 92, "short": "burned into the picture",
        "line": "the dialogue is burned into the picture",
        "rests": "the picture reader's own verdict of dialogue or hybrid, "
                 "which had to clear its act line to be recorded. How that "
                 "score is built, and how often it has agreed with you, is "
                 "the panel above this one."},
    # `audio` was here, clearing any file whose audio carried the language.
    # It became `spoken` and moved to the bottom of the ladder, where it
    # decides how much a missing subtitle matters instead of deciding there
    # is not one. See WANT.
    "ocr_trans": {
        "says": OK, "sure": 88, "short": "OCRed off the screen",
        "line": "English was read off the picture and the audio is not "
                "English",
        "rests": "two English function words or more, OCRed from the frames. "
                 "Measured over the 23 accused files that had any words at "
                 "all: the 13 with a function word were every one "
                 "Spanish-audio Velvet with English burned in; the 10 "
                 "without were every one Japanese-audio OCR noise. Getting "
                 "here already means the language is not in the audio, so "
                 "English on the screen is English the audio does not have - "
                 "which is what a translation subtitle is."},
    "scored": {
        "says": OK, "sure": 90, "short": "the picture reader scores it burned-in",
        "line": "the picture reader's own score puts it past its act line",
        "rests": "\"What each file actually carries\" scores every picture "
                 "from the words it read, the function words among them and "
                 "how much of the runtime carried text, against the odds for "
                 "that kind of file. Past its act line it marks the file "
                 "itself. Measured when this went in: 62 files sat at "
                 "undecided here while that scorer had them at 85% or "
                 "better - the stored verdict was written before the scorer "
                 "was refined, and the mark had not caught up."},
    "show_burned": {
        "says": OK, "sure": 85, "short": "the rest of the show is burned-in",
        "line": "its picture shows marks nobody could read, and most of this "
                "show carries burned-in dialogue",
        "rests": "same release, same encoder, same burned-in subtitles. "
                 "Detective Conan was 61 of the 332 nobody could decide and "
                 "455 of its other 523 settled pictures read as burned-in "
                 "dialogue; City Hunter was 12 of them at 78%. A PROPORTION "
                 "and not an existence test, because one burned-in episode "
                 "out of 170 would otherwise clear all of Teenage Mutant "
                 "Ninja Turtles. Replayed over the 315 pictures a person "
                 "settled by hand it clears 291 and disagrees once."},
    "marks": {
        "says": UNKNOWN, "short": "the picture reader is unsure",
        "line": "the picture reader's own score sits between its two lines",
        "rests": "Erik: \"system 1 already does most of the work and is very "
                 "refined now\". So this check no longer keeps its own "
                 "opinion about marks the OCR could not read - it asks "
                 "\"What each file actually carries\" for its score and takes "
                 "the same three-way call that scorer makes for itself. Past "
                 "the act line the file is cleared; at or under the "
                 "throw-away line it is a raw; only in between does it wait, "
                 "and it waits for the same reason that scorer waits."},
    "noframes": {
        "says": UNKNOWN, "short": "no frames sampled",
        "line": "the picture reader has no frames for this file",
        "rests": "it looked and came back with nothing to count, so it has "
                 "not ruled anything out."},
    "nopic": {
        "says": UNKNOWN, "short": "picture not read",
        "line": "the picture reader has not looked at this file",
        "rests": "burned-in subtitles cannot be ruled out by a reader that "
                 "has not run."},
    "stale": {
        "says": UNKNOWN, "short": "waiting on a re-read",
        "line": "it was read by an earlier version of the picture reader",
        "rests": "the caption floor and the OCR engine have both changed, "
                 "and the stamp has moved three times. A reading this file "
                 "is already queued to have redone is not one to delete a "
                 "release over - measured when this went in, all 34 files on "
                 "the list rested on an older reader and none on the "
                 "current one."},
    "marker": {
        "says": OK, "sure": 97, "short": "nuarr's own marker",
        "line": "it carries the blank marker track nuarr writes",
        "rests": "\"English (burned into the picture)\" is not a subtitle "
                 "track, it is nuarr's own note that the dialogue is painted "
                 "into the frames - class 'marker' in the probe. Reading it "
                 "as a signs track would have put 809 files on a delete "
                 "list, every one of them already answered by nuarr itself."},
    "mislabelled": {
        "says": OK, "sure": 80, "short": "forced, but really dialogue",
        "line": "its only track claims to be forced and runs at the cadence "
                "of speech",
        "rests": "a forced flag over a full film's worth of lines is a full "
                 "track wearing the wrong flag, not a sign sheet - Aura Koga "
                 "Maryuin's Last War carries 1,691 cues over 83 minutes, "
                 "20.4 a minute, under a forced English track. The title "
                 "check corrects the label; this only declines to delete the "
                 "file over it."},
    "signs_unread": {
        "says": UNKNOWN, "short": "claims signs, unread",
        "line": "its only track in the language claims to be signs, and the "
                "container does not say how many lines it has",
        "rests": "a forced or signs-titled track is usually signs and "
                 "sometimes a full track wearing the wrong flag, and the cue "
                 "count is what tells them apart. Where the container reports "
                 "none the probe cannot say, so the question is left open and "
                 "the track is read rather than guessed at. Erik: hold and "
                 "read them."},
    "signs_only": {
        "says": MISSING, "sure": 88, "short": "a raw with signs on top",
        "line": "a raw whose only readable track is signs and songs",
        "rests": "signs and songs is not dialogue - it translates a shop "
                 "front and leaves the conversation untranslated. Reached "
                 "only when the cue rate positively says so: My Home Hero "
                 "S01E02 carries 22 cues over 24 minutes, The Castle of "
                 "Cagliostro 12 over 99."},
    "spoken": {
        "says": WANT, "sure": 95, "short": "not a raw - you speak it",
        "line": "it is spoken in the language, so it is not a raw",
        "rests": "the rule asked for a subtitle and this file has not got "
                 "one, so it is listed - but you can already understand it, "
                 "which is the whole thing the check is for. Nothing is "
                 "deleted over a film you can follow. Heard by Whisper where "
                 "it has listened, tagged where it has not, and the "
                 "metadata's original language last of all: Whisper outranks "
                 "the metadata because they disagree about 14,738 files here "
                 "and 14,196 of those are the same shape - the metadata says "
                 "Japanese and the audio is an English dub."},
    "missing": {
        "says": MISSING, "sure": 90, "short": "a raw",
        "line": "a raw - spoken in a language you have not got, with "
                "nothing to read",
        "rests": "every rung above had its say first. No dialogue track, "
                 "no file beside it, the picture reader's own score at or "
                 "under its throw-away line, and the audio in a language "
                 "this library was not asked to keep. Nothing nuarr can do "
                 "to these bytes produces a subtitle, so the only fix is a "
                 "different release - which is why this is the one rung with "
                 "a button."},
}

# THE ORDER THE LADDER IS ACTUALLY TRIED IN, written out rather than taken
# from the dict. The panel reads this to draw the rungs in the order they
# answer, and after the rewrite the dict's own order was the order the
# entries happened to be typed in - which put "the audio is in the language"
# above "it has no subtitle", the opposite of what the code does.
RULE_ORDER = (
    "unread", "noread",                       # nobody has looked
    "track", "side", "marker", "mislabelled",  # a subtitle, and a real one
    "untagged",                                # might be the one
    "burned", "scored", "ocr_trans",           # what the picture can clear
    "spoken",                                  # you can follow it anyway
    "signs_unread",                            # signs, or not? go and read it
    "nopic", "show_burned",                    # and the shields
    "marks", "noframes", "stale",
    "signs_only", "missing",                   # the two that accuse
)
assert set(RULE_ORDER) == set(RULES), "a rung is defined but never tried"


def rule_of(rule: str) -> dict:
    return RULES.get(str(rule or ""), {})


def sureness(rule: str) -> int:
    """The rung's own number, or 0 for the rungs that make no claim."""
    return int(RULES.get(str(rule or ""), {}).get("sure") or 0)


# Slow on purpose. Nothing here touches a disk - it reads sub_facts, hardsub
# and file_probes, all of which somebody else has already filled in - so the
# cost is a few table scans, and the thing it is looking for changes only when
# a file lands or a policy is edited. Both of those poke it directly.
POLL_S = 900.0

# How many files one pass judges. The whole library is ~40,000 rows and the
# query is indexed, but a pass that holds the write lock for a second is a
# pass that makes every other panel stutter, and there is no hurry.
BATCH = 5000

STATE: dict = {"running": False, "at": 0.0, "took": 0.0, "checked": 0,
               "ok": 0, "missing": 0, "unknown": 0, "want": 0,
               "err": "", "runs": 0,
               # A RUN YOU ASKED FOR, measured: how much of the library it
               # has judged so far and how much is left, for the bar.
               "done": 0, "total": 0, "t0": 0.0}

_READY = False


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sub_need(
                file_id    INTEGER NOT NULL,
                lang       TEXT    NOT NULL,
                library    TEXT    NOT NULL DEFAULT '',
                state      TEXT    NOT NULL DEFAULT '',
                why        TEXT    NOT NULL DEFAULT '',
                checked_at REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY (file_id, lang)
            )""")
        # The panel asks "how many are missing, and which" far more often than
        # it asks about one file.
        cur.execute("CREATE INDEX IF NOT EXISTS ix_sub_need_state "
                    "ON sub_need(state, library)")
        # WHICH RUNG ANSWERED, AND HOW SURE THAT RUNG IS. See RULES. The
        # verdict was being stored without either, so every `ok` in the
        # library looked equally certain - the 36,586 resting on a tag the
        # container wrote and the 127 resting on "this untagged track might
        # be the one" read exactly the same on the page.
        for col, decl in (("rule", "TEXT NOT NULL DEFAULT ''"),
                          ("sure", "INTEGER")):
            try:
                cur.execute(f"ALTER TABLE sub_need ADD COLUMN {col} {decl}")
            except Exception:                                    # noqa: BLE001
                pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sub_need_log(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id  INTEGER NOT NULL,
                lang     TEXT    NOT NULL DEFAULT '',
                at       REAL    NOT NULL DEFAULT 0,
                was      TEXT    NOT NULL DEFAULT '',
                now      TEXT    NOT NULL DEFAULT '',
                was_rule TEXT    NOT NULL DEFAULT '',
                now_rule TEXT    NOT NULL DEFAULT ''
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_sub_need_log_rule "
                    "ON sub_need_log(was_rule)")
        # A TRIGGER RATHER THAN A LINE IN THE SWEEP. The sweep writes 40,000
        # rows a pass and reading each one back first to see whether it moved
        # would double the query count for a fact SQLite already has in hand.
        # It also catches every other writer, and there are three.
        # THE EMPTY-RUNG GUARD IS NOT A DETAIL. Every row in the table
        # predates the rung column, so the first sweep after this lands
        # moves all 39,900 of them from '' to a name - and without the
        # guard that is 39,900 log rows saying the ladder changed its mind,
        # on the day it learned to say which rung it was standing on. A
        # rung has no history before it had a name.
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS tg_sub_need_moved
            AFTER UPDATE OF state, rule ON sub_need
            WHEN COALESCE(old.rule,'') <> ''
             AND (old.state <> new.state
                  OR COALESCE(old.rule,'') <> COALESCE(new.rule,''))
            BEGIN
              INSERT INTO sub_need_log(file_id, lang, at, was, now,
                                       was_rule, now_rule)
              VALUES(new.file_id, new.lang, new.checked_at, old.state,
                     new.state, COALESCE(old.rule,''),
                     COALESCE(new.rule,''));
            END""")
    _READY = True


def mode() -> str:
    """"manual" (default) or "auto"."""
    m = str(getattr(SETTINGS, "subneed_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


# ------------------------------------------------------------ the policy ----
def required() -> dict[str, list[str]]:
    """{library: [lang, ...]} - the subtitle languages a library REQUIRES.

    Requiring is not the same as keeping, which is why it is a separate list
    rather than a flag on `langs`. Keeping says "if this is here, do not throw
    it away"; requiring says "if this is not here, the file is wrong". A
    library can reasonably keep six languages and require one.
    """
    try:
        from . import langpolicy
        pol = langpolicy.load()
    except Exception:                                            # noqa: BLE001
        return {}
    out: dict[str, list[str]] = {}
    for lib, sides in (pol or {}).items():
        want = [str(x).strip().lower()[:3]
                for x in ((sides or {}).get("subs") or {}).get("require") or []]
        if want:
            out[str(lib)] = sorted(set(want))
    return out


def _keeps_untagged(lib: str) -> bool:
    try:
        from . import langpolicy
        return bool(((langpolicy.load().get(lib) or {}).get("subs")
                     or {}).get("keep_untagged", True))
    except Exception:                                            # noqa: BLE001
        return True


# ----------------------------------------------------------- the verdict ----
# WHAT THE METADATA CALLS THE ORIGINAL LANGUAGE, in the three-letter code
# the rest of nuarr speaks. The arrs store a name, not a code.
#
# IT IS THE LAST WORD AND NOT THE FIRST. It describes the SHOW, not this
# file: every dubbed anime here is "Japanese" by that measure, and 14,196 of
# them are English dubs. So it is consulted only where nothing has been
# listened to and nothing is tagged - which is 536 files, and for those it is
# better than nothing.
_ORIG3 = {
    "japanese": "jpn", "english": "eng", "chinese": "chi", "korean": "kor",
    "spanish": "spa", "french": "fre", "german": "ger", "italian": "ita",
    "portuguese": "por", "russian": "rus", "dutch": "dut", "swedish": "swe",
    "norwegian": "nor", "danish": "dan", "finnish": "fin", "polish": "pol",
    "turkish": "tur", "arabic": "ara", "hindi": "hin", "thai": "tha",
    "vietnamese": "vie", "indonesian": "ind", "hebrew": "heb", "czech": "cze",
    "hungarian": "hun", "greek": "gre", "ukrainian": "ukr", "romanian": "ron",
    "catalan": "cat", "tagalog": "tgl", "cantonese": "chi", "mandarin": "chi",
}


def orig_code(name: str) -> str:
    return _ORIG3.get(str(name or "").strip().lower(), "")


# ---- IS THIS TRACK ACTUALLY DIALOGUE ---------------------------------------
#
# Erik: "it can't just be S+S it has to be full dialogue if people are
# speaking". The check asked only whether a track carried the right language
# tag, and a signs-and-songs sheet carries it exactly as a full script does -
# so a Japanese film whose only English track translates the shop fronts
# counted as having English subtitles.
#
# The probe already knows. sub_facts stores a `class` per track, written by
# the subtitle scan: 33,463 full, 12,847 sdh, 9,828 forced, 845 marker across
# this library. No track has to be read for this.
_DIALOGUE_CLASS = {"full", "sdh", "cc", "dialogue"}
_SIGN_WORDS = ("sign", "song", "karaoke", "op/ed", "typeset", "credit")


def _is_signy(t: dict) -> bool:
    """Does this track SAY it is signs, by its title or its forced flag?"""
    ti = str(t.get("title") or "").lower()
    return any(w in ti for w in _SIGN_WORDS) or bool(t.get("forced"))


def _is_dialogue(t: dict) -> bool:
    """A full script, by the container's own account of it."""
    return (not _is_signy(t)
            and str(t.get("class") or "") in _DIALOGUE_CLASS)


def read_rate(file_id: int, ord_: int, minutes: float) -> float:
    r"""Lines a minute from the track that was actually READ, or 0.

    THE HEADER IS A CLAIM AND THE EVENTS ARE A FACT. subtitle_shape holds
    what the track reader pulled out with mkvextract - how many events, how
    many of them plain dialogue lines - and the raw check was not looking at
    it. Drug Store in Another World S01E10 was read while this was being
    written: 473 events, 467 of them plain lines over 24 minutes, and the
    file still sat at "its only eng track claims to be signs and the
    container does not say how many lines it has". The container still does
    not. Somebody went and counted.
    """
    if minutes <= 0:
        return 0.0
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT plain, events FROM subtitle_shape "
                " WHERE file_id=? AND track=?",
                (int(file_id), int(ord_) + 1)).fetchone()
    except Exception:                                            # noqa: BLE001
        return 0.0
    if not r:
        return 0.0
    # `plain` is the dialogue lines with the signs taken out, which is the
    # number this question is about; events is the fallback for a row stored
    # before that column existed.
    n = r["plain"]
    if n is None or int(n) < 0:
        n = r["events"]
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return 0.0
    return (n / minutes) if n > 0 else 0.0


def _speech_rate(t: dict, minutes: float) -> float:
    """Lines a minute, or 0 when the container does not say.

    -1 is what sub_facts stores for "no count reported", and it is common:
    12 of the 16 files that first reached the signs rung had it. Zero is
    returned for that rather than a number, because a rate nobody measured
    must not be compared against a threshold.
    """
    try:
        cues = int(t.get("cues") or t.get("events") or -1)
    except (TypeError, ValueError):
        cues = -1
    if cues <= 0 or minutes <= 0:
        return 0.0
    return cues / minutes


# ---- WHAT THE REST OF THE SHOW CARRIES -------------------------------------
#
# Erik: "the undecided should be checked again for dialog subs". 332 files
# sat at undecided, all of them the same shape - Japanese audio, no subtitle
# track at all, and a picture with subtitle-shaped marks the OCR could not
# read a word of. Re-reading them does not help: every one is already at the
# current reader revision, so they HAVE been read again and came back the
# same. The evidence that settles them is not in the file.
#
# It is in the other episodes. Detective Conan is 61 of the 332, and 455 of
# its other 523 settled pictures carry burned-in dialogue - same release,
# same encoder, same burned-in subtitles. City Hunter is 12 of them and 78%
# of that show reads burned-in. Meanwhile EyeShield 21 is 39 of them and 5%
# of that show does.
#
# A PROPORTION, NOT AN EXISTENCE TEST, and that distinction is the whole
# rule. "Any sibling read as burned-in" would clear all 169 Teenage Mutant
# Ninja Turtles episodes on the strength of one, and 39 EyeShield 21 on the
# strength of two out of forty-four. Shows here are strongly bimodal - 87%,
# 78%, or 0 to 8% - so anything from a quarter to three quarters picks out
# the same 74 files, and half is the honest middle of that.
#
# VALIDATED AGAINST THE ANSWERS. Replayed over the 315 pictures Erik settled
# by hand: the rule clears 291 of them and disagrees with him once.
#
# It can only CLEAR a file, never accuse one. The failure mode is leaving a
# raw alone, not deleting something good.
SHOW_BURNED_AT = 0.5        # of the show's settled pictures
SHOW_BURNED_MIN = 4         # settled pictures before the show is evidence
_SHOW_TTL = 300.0
_SHOWS: dict = {"at": 0.0, "map": {}}


def _show_of_path(p: str) -> str:
    parts = [x for x in str(p or "").replace("/", "\\").split("\\") if x]
    return parts[2] if len(parts) > 2 else (parts[-1] if parts else "")


def show_burn(cur) -> dict:
    """show folder -> (pictures reading burned-in, pictures settled at all).

    One query for the whole library rather than one per file: the sweep
    judges 40,000 files a pass and this table is 4,400 rows.
    """
    now = time.time()
    if _SHOWS["map"] and now - _SHOWS["at"] < _SHOW_TTL:
        return _SHOWS["map"]
    out: dict = {}
    try:
        for r in cur.execute(
                "SELECT f.path, COALESCE(NULLIF(h.chosen,''), h.state) v "
                "  FROM hardsub h JOIN files f ON f.id = h.file_id "
                " WHERE f.state NOT IN ('deleted','duplicate') "
                "   AND COALESCE(NULLIF(h.chosen,''), h.state) <> ''"):
            sh = _show_of_path(r["path"])
            if not sh:
                continue
            b, n = out.get(sh, (0, 0))
            out[sh] = (b + (1 if r["v"] in ("dialogue", "hybrid") else 0),
                       n + 1)
    except Exception:                                            # noqa: BLE001
        return _SHOWS["map"] or {}
    _SHOWS.update(at=now, map=out)
    return out


def show_burns_in(path: str, cur) -> tuple[bool, int, int]:
    """(is the rest of this show burned-in, how many, out of how many)."""
    sh = _show_of_path(path)
    if not sh:
        return False, 0, 0
    b, n = (show_burn(cur).get(sh) or (0, 0))
    return (n >= SHOW_BURNED_MIN and b >= n * SHOW_BURNED_AT), b, n


def _audio_langs(file_id: int, cur) -> set[str]:
    r"""What languages the audio is in - heard where possible, tagged where not.

    THE TAG IS A CLAIM, AND THIS PAGE WAS TAKING IT AS A FACT. A file tagged
    eng whose audio is really Japanese returned OK here - "the audio is
    already in eng" - and nuarr stopped asking whether it had English
    subtitles. The tag satisfied a requirement the file does not meet. That
    failure is the whole reason audiolang exists: S01E12 of "Pass the Monster
    Meat, Milady!" carries two Japanese tracks and calls one of them English.

    So a fresh, confident verdict from the listener outranks the tag for that
    track. Where nothing has been listened to the tag stands, because it is
    the only evidence there is.
    """
    out: set[str] = set()
    tags: list = []
    r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                    (int(file_id),)).fetchone()
    if r:
        try:
            streams = json.loads(r["json"]).get("streams") or []
            tags = [str((s.get("tags") or {}).get("language") or "").lower()[:3]
                    for s in streams if s.get("codec_type") == "audio"]
        except Exception:                                        # noqa: BLE001
            tags = []
    heard: dict = {}
    try:
        for a in cur.execute(
                "SELECT a.track, a.code FROM audio_lang a "
                "  JOIN files f ON f.id = a.file_id "
                " WHERE a.file_id = ? AND COALESCE(a.ok,0) = 1 "
                "   AND COALESCE(a.code,'') <> '' "
                "   AND a.size = COALESCE(f.size,0) "
                "   AND ABS(COALESCE(a.mtime,0) - COALESCE(f.mtime,0)) <= 1.0",
                (int(file_id),)):
            heard[int(a["track"])] = str(a["code"]).lower()[:3]
    except Exception:                                            # noqa: BLE001
        heard = {}
    for i, t in enumerate(tags):
        out.add(heard.get(i) or t)
    # A verdict for a track the probe did not list still counts.
    out.update(heard.values())
    return out


def _picture(file_id: int, cur, pic=None):
    if pic is not None:
        return pic
    try:
        return cur.execute(
            "SELECT state, chosen, low_hits, samples, words, rev FROM hardsub "
            " WHERE file_id=?", (int(file_id),)).fetchone()
    except Exception:                                            # noqa: BLE001
        return None


def verdict(file_id: int, lang: str, cur, facts=None,
            untagged_ok: bool = True, pic=None,
            minutes: float = 0.0, path: str = "") -> tuple[str, str, str]:
    """Does this file carry `lang` subtitles? Returns (state, why, rule).

    `rule` names the rung that answered, so the row can say how sure that
    rung is and scoring() can count how often it has had to take an answer
    back. See RULES.

    `facts` is an optional pre-fetched sub_facts row, so a sweep over 40,000
    files does one query per file instead of two.
    """
    lang = (lang or "").lower()[:3]
    # A CUE COUNT MEANS NOTHING WITHOUT A RUNTIME. Fetched here when the
    # caller did not have it, which is the single-file path; the sweep
    # already has it on the row it selected.
    if not minutes or not path:
        try:
            _r = cur.execute("SELECT duration, path FROM files WHERE id=?",
                             (int(file_id),)).fetchone()
            if _r is not None:
                minutes = minutes or float(_r["duration"] or 0) / 60.0
                path = path or str(_r["path"] or "")
        except Exception:                                        # noqa: BLE001
            pass
    if facts is None:
        facts = cur.execute("SELECT * FROM sub_facts WHERE file_id=?",
                            (int(file_id),)).fetchone()

    # NOBODY HAS LOOKED INSIDE THIS FILE. Everything below reads a probe, and
    # a missing probe makes every one of those reads return "nothing" - which
    # is indistinguishable from the file genuinely carrying nothing. This is
    # the check that stops 11,687 unread files being reported as faulty; see
    # the module docstring before weakening it.
    has_probe = cur.execute("SELECT 1 FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
    if not has_probe:
        return UNKNOWN, "nuarr has not looked inside this file yet", "unread"
    if facts is None:
        return UNKNOWN, "the subtitle reader has not read this file yet", "noread"

    try:
        tracks = json.loads(facts["tracks"] or "[]")
    except Exception:                                            # noqa: BLE001
        tracks = []
    try:
        sides = json.loads(facts["sides"] or "[]")
    except Exception:                                            # noqa: BLE001
        sides = []

    # ---- IS THERE A SUBTITLE, AND IS IT DIALOGUE ---------------------------
    #
    # This asked only whether a track carried the right language tag. Erik:
    # "it can't just be S+S it has to be full dialogue if people are
    # speaking". A signs-and-songs sheet carries the tag exactly as a full
    # script does, so a Japanese film whose only English track translates the
    # shop fronts counted as subtitled and nobody ever looked again.
    mine = [t for t in tracks
            if str(t.get("lang") or "").lower()[:3] == lang]
    for t in mine:
        if _is_dialogue(t):
            return OK, f"carries a full {lang} dialogue track", "track"
    for s in sides:
        if str(s.get("lang") or "").lower()[:3] == lang:
            return OK, f"has a {lang} subtitle file beside it", "side"
    # NUARR'S OWN NOTE TO ITSELF. See the marker rung.
    for t in mine:
        if str(t.get("class") or "") == "marker":
            return OK, ("carries nuarr's marker track - the dialogue is "
                        "burned into the picture"), "marker"
    # A FORCED FLAG OVER A FULL FILM'S WORTH OF LINES. See the twin rule in
    # subtitletitle: the genuine forced tracks here carry a few dozen cues,
    # the mislabelled ones carry a whole script.
    for t in mine:
        rate = _speech_rate(t, minutes)
        if SPEECH_LO <= rate <= SPEECH_HI:
            return OK, (f"its {lang} track is flagged forced but carries "
                        f"{rate:.0f} lines a minute, which is dialogue "
                        f"wearing the wrong flag"), "mislabelled"
        # AND THE SAME QUESTION OF THE TRACK THAT WAS READ. The header says
        # nothing about how long this track is; the reader counted it.
        got = read_rate(file_id, int(t.get("ord") or 0), minutes)
        if SPEECH_LO <= got <= SPEECH_HI:
            return OK, (f"its {lang} track claims to be signs, and reading it "
                        f"found {got:.0f} dialogue lines a minute - a full "
                        f"script wearing a forced flag"), "mislabelled"

    # AN UNLABELLED TRACK MIGHT BE THE ONE. The library keeps untagged tracks
    # precisely because releases leave subtitles unlabelled; reading one to
    # find out costs an extract and an OCR, and until somebody does, "there is
    # no English track" is a guess wearing a fact's clothes.
    if untagged_ok:
        for t in tracks:
            if str(t.get("lang") or "").lower()[:3] in ("", "und", "un"):
                return OK, ("has an untagged subtitle track, which this "
                            "library keeps - it may be the one"), "untagged"

    # ---- THE PICTURE, AND THE THREE THINGS IT CAN SAY ----------------------
    #
    # Erik: "shows like City Hunter have burn-in subs so the system should
    # account for that too". They do, and it did not - because a `none`
    # verdict from the picture reader was being read as "there is nothing in
    # the picture", and that is not what it means.
    #
    # hardsub samples 24 frames, counts the ones with something bright low in
    # the picture, then OCRs them. If the OCR brings back no readable words
    # the verdict is NONE - correctly, from its point of view: it cannot show
    # you a caption it could not read. But the frame counts are still there,
    # and on City Hunter S01E20 they read:
    #
    #     state='none'   low_hits=16   samples=24   words=''
    #
    # Two thirds of sampled frames had subtitle-shaped marks in the subtitle
    # band and not one word came back - which is what an SDTV-era hardsub
    # looks like to an OCR trained on clean type. Treating that as "no
    # subtitles" put 126 of 146 files on a list offering to delete them.
    # Measured across the whole list when this was written:
    #
    #     146 missing
    #     126 ... the reader saw marks it could not read   <- these
    #      20 ... the picture really is clean
    #
    # So `none` is only evidence of a clean picture when the FRAME COUNTS
    # agree. Above the line, the picture question is unresolved and the file
    # goes to `unknown` with the rest of what nuarr does not know.
    # ---- WHAT THE PICTURE SAYS, AS SYSTEM 1 SCORES IT ---------------------
    #
    # Erik: "system 2 should change its logic and should be a placeholder
    # for system 1 - foreign media that comes up at or near 0%. System 1
    # already does most of the work and it is very refined now, so system 2
    # can be where foreign media gets manual/auto replaced."
    #
    # This check used to keep its own opinion about the picture: marks the
    # OCR could not read meant "undecided", whatever the scorer thought of
    # them. That rule was written for a cruder reader and it outlived it.
    # Measured on the 284 undecided files with a picture reading:
    #
    #     187  the scorer had at or under its throw-away line
    #      62  the scorer had at or ABOVE its act line - burned-in dialogue,
    #          waiting only for the mark to catch up
    #      31  the scorer was unsure
    #
    # Sixty-two files sat at "marks it could not read" while the other
    # panel was ready to mark them. Two opinions about one picture, and the
    # better-informed one was being ignored.
    #
    # So this takes the scorer's own three-way call - act, dismiss, ask -
    # and does exactly what it would do: past the act line the file is
    # cleared, at or under the throw-away line it is a raw, and only in
    # between does it wait.
    prow = _picture(file_id, cur, pic)
    pstate = ""
    words = ""
    call, score = "", 0
    if prow is not None:
        pstate = str((prow["chosen"] if "chosen" in prow.keys() else None)
                     or prow["state"] or "")
        try:
            if "words" in prow.keys():
                words = str(prow["words"] or "").strip()
        except Exception:                                    # noqa: BLE001
            words = ""
        # `signs` is deliberately not on this list. Signs and songs are not
        # dialogue, and a file carrying only those still needs subtitles.
        if pstate in ("dialogue", "hybrid"):
            return OK, "the dialogue is burned into the picture", "burned"
        chosen = str(prow["chosen"] if "chosen" in prow.keys() else "") or ""
        if chosen in ("none", "signs"):
            # A person looked and said there is nothing to read here. That
            # is the scorer's throw-away call, made by hand.
            call = "dismiss"
        else:
            try:
                from . import hardsub as _hs
                _v = _hs.verdict_for({
                    "state": str(prow["state"] or ""),
                    "low_hits": prow["low_hits"], "samples": prow["samples"],
                    "words": words, "path": path})
                call = str(_v.get("auto") or "")
                score = int(_v.get("score") or 0)
            except Exception:                                # noqa: BLE001
                call = ""
        if call == "mark":
            return OK, (f"the picture reader scores the frames at {score}% - "
                        f"burned-in dialogue it has not marked yet"), "scored"

    hits = _english_hits(words) if _is_english(lang) else []
    got = [w for w in (x.strip() for x in words.split(",")) if w]
    shown = ", ".join(got[:8]) + ("..." if len(got) > 8 else "")

    spoken = _audio_langs(file_id, cur)
    if not spoken:
        # NOTHING HEARD AND NOTHING TAGGED - 536 files here. The metadata's
        # original language describes the SHOW rather than this file, which
        # is why it is asked last, and last is better than nothing.
        try:
            _r = cur.execute("SELECT orig_lang FROM files WHERE id=?",
                             (int(file_id),)).fetchone()
            _oc = orig_code(_r["orig_lang"] if _r else "")
        except Exception:                                    # noqa: BLE001
            _oc = ""
        if _oc:
            spoken = {_oc}
    heard_it = lang in spoken
    said = "/".join(sorted(x for x in spoken
                           if x and x not in ("und", "un"))) or "not " + lang

    # HEARD ONE LANGUAGE, SAW ANOTHER - a translation subtitle. Velvet, The
    # New Empire: Spanish audio, and the picture reader returning "are,
    # barbara, doing, here, what, you". Two function words to decide.
    if len(hits) >= 2 and not heard_it:
        return OK, (f"the audio is {said} and the picture reader OCRed "
                    f"English off the screen - {shown} - so the English is "
                    f"burned in as a translation subtitle"), "ocr_trans"

    # ---- CAN ANYBODY FOLLOW THIS FILE WITHOUT A SUBTITLE? -----------------
    #
    # Erik: "if the rule say it wants subs then follow the rule but it
    # shouldn't delete the file, it should replace the file if there is no
    # way for the user to understand". Everything above has established that
    # there is no dialogue subtitle here. This decides how much that matters,
    # and it sits ABOVE every shield because a shield only protects against
    # an accusation and nothing below this line can accuse a file the viewer
    # can already follow.
    if heard_it:
        why = f"the audio is {said}, so nothing here is untranslatable"
        if mine:
            why = (f"its only {lang} track is signs and songs, but the audio "
                   f"is {said} - the conversation needs no translation")
        return WANT, why, "spoken"

    # ---- AN UNREAD TRACK IS A SECOND OF WORK, NOT A REASON TO PARK ---------
    #
    # This test used to sit at the bottom, under every picture shield, and
    # that put the two questions in the wrong order. Drug Store in Another
    # World is twelve identical files - one forced ASS track named
    # "English", no cue count in the header - and they came out three
    # different ways purely on what their PICTURES happened to score:
    #
    #     E10  picture 13/24, scorer said dismiss  -> signs_unread, read,
    #                                                 361 lines, cleared
    #     E04  picture 19/24, scorer said ask      -> marks, parked for ever
    #     E05  picture 14/24, scorer said ask      -> marks, parked for ever
    #
    # The `marks` rung is a shield against a false accusation, and it was
    # shielding the file from being LOOKED AT. Nothing could get past it to
    # notice there was an unread track, so unread_tracks never saw them, so
    # the reader never read them, so the picture stayed the only evidence -
    # and Erik pressed Read again on three of them and nothing happened.
    #
    # Reading the track costs one mkvextract. If it comes back as dialogue
    # the picture does not matter at all, and if it comes back as signs the
    # shields below are all still there. So it is asked first.
    if mine and not any(_speech_rate(t, minutes) > 0
                        or read_rate(file_id, int(t.get("ord") or 0), minutes)
                        > 0 for t in mine):
        return UNKNOWN, (
            f"its only {lang} track claims to be signs and the container "
            f"does not say how many lines it has, so it is queued to be "
            f"read rather than guessed at"), "signs_unread"

    # ---- FROM HERE ON A FILE CAN BE ACCUSED ---------------------------------
    if prow is None:
        return UNKNOWN, ("the picture reader has not looked at this file, so "
                         "burned-in subtitles cannot be ruled out"), "nopic"
    # THE REST OF THE SHOW OUTRANKS A DISMISSAL. The scorer reads Detective
    # Conan at 0% - an SDTV-era hardsub the OCR cannot transcribe - and 455
    # of its other episodes are settled as burned-in. Dropping this rung
    # while deferring to the scorer put 45 of those episodes on the raw
    # list in one dry run. See show_burns_in for the rule and its check
    # against the answers.
    yes, _b, _n = show_burns_in(path, cur)
    if yes:
        return OK, (
            f"the picture reader scores the frames at {score}% and could "
            f"read nothing - but {_b} of the {_n} episodes of this show it "
            f"has settled carry burned-in dialogue, so these are the same "
            f"subtitles it cannot transcribe"), "show_burned"
    if call == "ask":
        return UNKNOWN, (
            f"the picture reader scores the frames at {score}%, between its "
            f"throw-away line and its act line - it is not sure, so neither "
            f"is this"), "marks"
    n_s = int(prow["samples"] or 0)
    if not n_s:
        return UNKNOWN, ("the picture reader has no frames for this file, so "
                         "burned-in subtitles cannot be ruled out"), "noframes"

    # A READING THIS FILE IS ALREADY QUEUED TO HAVE REDONE IS NOT ONE TO
    # DELETE A RELEASE OVER. Measured when this went in: all 34 files on the
    # blocklist list rested on an older reader and none on the current one.
    try:
        from . import hardsub as _hs
        rev = 0
        if "rev" in prow.keys():
            rev = int(prow["rev"] or 0)
        if rev < int(_hs.READER_REV) and not (
                prow["chosen"] if "chosen" in prow.keys() else None):
            return UNKNOWN, (
                "the picture was read by an earlier version of the reader and "
                "is queued to be read again - the caption floor and the OCR "
                "engine have both changed since, so this is not an answer to "
                "delete a release over"), "stale"
    except Exception:                                            # noqa: BLE001
        pass

    # ---- NOTHING ANYWHERE, AND NOBODY CAN FOLLOW IT -----------------------
    pic = (f"the picture reader scores the frames at {score}%, under its "
           f"own throw-away line" if call == "dismiss" else
           "nothing readable in the picture")
    if mine:
        # Every track here has a rate by now - the unread ones were sent to
        # be read above, before any shield could hide them.
        rates = [r for r in
                 (_speech_rate(t, minutes)
                  or read_rate(file_id, int(t.get("ord") or 0), minutes)
                  for t in mine) if r > 0]
        rate = min(rates) if rates else 0.0
        return MISSING, (
            f"its only {lang} track is signs and songs, {rate:.1f} lines a "
            f"minute - the conversation is not translated; {pic}; and the "
            f"audio is {said}"), "signs_only"

    n = len(tracks) + len(sides)
    why = ("carries no subtitles at all" if not n else
           f"carries {n} subtitle(s), none of them {lang}")
    return MISSING, f"{why}; {pic}; and the audio is {said}", "missing"


# -------------------------------------------------------------- the sweep ---
def check_one(file_id: int, cur=None) -> dict:
    """Judge one file against its library's required languages."""
    init()
    close = cur is None
    ctx = cursor() if close else None
    cur = ctx.__enter__() if close else cur
    try:
        row = cur.execute("SELECT id, library, path, state, duration "
                          "  FROM files WHERE id=?",
                          (int(file_id),)).fetchone()
        if not row:
            return {"ok": False, "why": "no such file"}
        lib = row["library"] or ""
        want = required().get(lib) or []
        if not want or (row["state"] or "") in ("deleted", "duplicate"):
            cur.execute("DELETE FROM sub_need WHERE file_id=?", (int(file_id),))
            return {"ok": True, "checked": 0}
        facts = cur.execute("SELECT * FROM sub_facts WHERE file_id=?",
                            (int(file_id),)).fetchone()
        untagged = _keeps_untagged(lib)
        now = time.time()
        out = {}
        for lang in want:
            st, why, rule = verdict(file_id, lang, cur, facts, untagged,
                                    None,
                                    float(row["duration"] or 0) / 60.0,
                                    str(row["path"] or ""))
            sure = _sure_of(st, rule, file_id, lang, cur, facts, None,
                            str(row["path"] or ""),
                            float(row["duration"] or 0) / 60.0)
            out[lang] = st
            cur.execute(
                "INSERT INTO sub_need(file_id,lang,library,state,why,"
                "                     checked_at,rule,sure)"
                " VALUES(?,?,?,?,?,?,?,?) "
                " ON CONFLICT(file_id,lang) DO UPDATE SET "
                " library=excluded.library, state=excluded.state, "
                " why=excluded.why, checked_at=excluded.checked_at, "
                " rule=excluded.rule, sure=excluded.sure",
                (int(file_id), lang, lib, st, why, now, rule, sure))
        return {"ok": True, "checked": len(want), "states": out}
    finally:
        if close:
            ctx.__exit__(None, None, None)


def sweep(limit: int = BATCH) -> dict:
    """Judge everything in every library that requires a language."""
    init()
    want_by_lib = required()
    STATE.update(running=True, err="")
    t0 = time.time()
    n = 0
    tally = {OK: 0, MISSING: 0, UNKNOWN: 0, WANT: 0}
    try:
        with cursor() as cur:
            if not want_by_lib:
                # Nothing is required anywhere, so nothing can be missing.
                # Clearing rather than leaving the rows is what makes turning
                # the switch off actually turn it off.
                cur.execute("DELETE FROM sub_need")
                STATE.update(checked=0, ok=0, missing=0, unknown=0)
                return {"ok": True, "checked": 0, "why": "nothing is required"}
            # A library that no longer requires anything drops out here.
            cur.execute("DELETE FROM sub_need WHERE library NOT IN (%s)"
                        % ",".join("?" * len(want_by_lib)),
                        tuple(want_by_lib))
            for lib, want in want_by_lib.items():
                untagged = _keeps_untagged(lib)
                rows = cur.execute(
                    # f.duration IS NOT OPTIONAL either, for the same reason
                    # h.words and h.rev are not: a cue count means nothing
                    # without a runtime, and this hand-written column list is
                    # the only thing feeding it on the sweep path. Leaving it
                    # out would make every signs-or-dialogue test fall back
                    # to "cannot say" across the whole library while
                    # single-file calls worked perfectly.
                    "SELECT f.id, f.library, f.duration, f.path, "
                    "       s.tracks, s.sides, s.picture, "
                    # h.words IS NOT OPTIONAL. verdict() reads it to decide
                    # whether the OCR pulled English off the picture, and this
                    # hand-written column list is the only thing feeding it on
                    # the sweep path. Leaving it out made the whole check a
                    # no-op here while single-file calls worked perfectly -
                    # see the note on _ENGLISH_STOP.
                    # h.rev FOR THE SAME REASON h.words IS HERE. verdict()
                    # reads it to tell a current reading from one queued to be
                    # redone, and this hand-written list is the only thing
                    # feeding it on the sweep path. Leaving a column out makes
                    # the check a silent no-op here while single-file calls
                    # work perfectly - which is exactly how it went this
                    # morning.
                    "       h.state, h.chosen, h.low_hits, h.samples, h.words, "
                    "       h.rev, "
                    "       (SELECT MIN(n.checked_at) FROM sub_need n "
                    "         WHERE n.file_id=f.id) AS seen, "
                    "       (h.file_id IS NOT NULL) AS haspic "
                    "  FROM files f LEFT JOIN sub_facts s ON s.file_id=f.id "
                    "                LEFT JOIN hardsub h ON h.file_id=f.id "
                    " WHERE f.library=? AND f.state NOT IN ('deleted','duplicate') "
                    " ORDER BY seen IS NOT NULL, seen "
                    " LIMIT ?", (lib, int(limit))).fetchall()
                now = time.time()
                for r in rows:
                    # The joined row carries the picture columns, so the sweep
                    # does not make a second query per file to read them.
                    prow = r if r["haspic"] else None
                    for lang in want:
                        st, why, rule = verdict(
                            r["id"], lang, cur, r, untagged, prow,
                            float(r["duration"] or 0) / 60.0,
                            str(r["path"] or ""))
                        sure = _sure_of(
                            st, rule, r["id"], lang, cur, r, prow,
                            str(r["path"] or ""),
                            float(r["duration"] or 0) / 60.0)
                        tally[st] = tally.get(st, 0) + 1
                        cur.execute(
                            "INSERT INTO sub_need"
                            "(file_id,lang,library,state,why,checked_at,"
                            " rule,sure) "
                            "VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(file_id,lang) DO UPDATE SET "
                            " library=excluded.library, state=excluded.state,"
                            " why=excluded.why, checked_at=excluded.checked_at,"
                            " rule=excluded.rule, sure=excluded.sure",
                            (int(r["id"]), lang, lib, st, why, now, rule,
                             sure))
                        n += 1
    except Exception as e:                                       # noqa: BLE001
        STATE["err"] = f"{type(e).__name__}: {e}"
        joblog.log(f"missing-subtitle check: {STATE['err']}", "warn",
                   system="subneed")
    finally:
        # THE TALLY IS THIS PASS; THE COUNTS ARE THE LIBRARY. Reporting the
        # pass's own tally as the totals would make the numbers jump every
        # fifteen minutes by however much of the library that pass happened
        # to reach.
        whole = counts()
        STATE.update(running=False, at=time.time(), took=time.time() - t0,
                     checked=n, ok=whole[OK], missing=whole[MISSING],
                     unknown=whole[UNKNOWN], want=whole.get(WANT, 0),
                     runs=STATE["runs"] + 1)
    return {"ok": True, "checked": n, "pass": tally, **counts()}


# ------------------------------------------------------------- for the UI ---
def counts() -> dict:
    r"""How many files sit at each verdict - counted the way they are LISTED.

    THE HEADLINE AND THE LIST MUST BE THE SAME SET. This read sub_need on its
    own while missing() joined `files` and dropped the dead ones, so the panel
    said "43 waiting" over an empty list: 27 of those rows belonged to files
    marked deleted and 16 to files with no row left at all. A number you
    cannot reach the members of is worse than no number - it reads as work
    queued up and ignored.

    Same join, same filter, so every figure here describes a file that exists.
    """
    init()
    out = {OK: 0, MISSING: 0, UNKNOWN: 0, WANT: 0,
           "unknown_by": {"unread": 0, "stale": 0, "open": 0}}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT n.state AS st, COUNT(*) AS c FROM sub_need n "
                    "  JOIN files f ON f.id = n.file_id "
                    " WHERE f.state NOT IN ('deleted','duplicate') "
                    " GROUP BY n.state"):
                out[r["st"]] = int(r["c"])
            # WHAT "UNKNOWN" IS MADE OF. One number stood in for three, and
            # the panel called all of it "not looked inside" while most of it
            # was files this check has looked inside and declined to judge
            # on a verdict the picture reader has already decided to redo.
            # Same join as above so the three add up to the one; whether a
            # file is on the re-read list is asked of the reader's own `rev`
            # rather than of the reason sentence, because the stale guard
            # only fires in front of the accusation and an unknown file's
            # `why` says something else while it is nonetheless waiting.
            try:
                from . import hardsub as _hs
                rev = int(_hs.READER_REV)
            except Exception:                                    # noqa: BLE001
                rev = 0
            for r in cur.execute(
                    "SELECT CASE WHEN h.file_id IS NULL THEN 'unread' "
                    "            WHEN COALESCE(h.rev,1) < ? "
                    "                 AND COALESCE(h.chosen,'')='' THEN 'stale' "
                    "            ELSE 'open' END AS k, COUNT(*) AS c "
                    "  FROM sub_need n "
                    "  JOIN files f ON f.id = n.file_id "
                    "  LEFT JOIN hardsub h ON h.file_id = n.file_id "
                    " WHERE n.state = ? "
                    "   AND f.state NOT IN ('deleted','duplicate') "
                    " GROUP BY k", (rev, UNKNOWN)):
                out["unknown_by"][r["k"]] = int(r["c"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


# THE THREE BUCKETS, AND THE FILES IN THEM. The header has counted these
# since it was written and there was no way to reach them: "138 not looked
# inside · 380 undecided" over a panel that lists neither, so the only way to
# find out what they were was to ask the database by hand. Each is a real
# question about real files - 81 of the undecided are one show, Detective
# Conan, every one of them reading "marks low in the picture the OCR could
# not read" - and that is a fact worth being able to see rather than infer.

# ---- HOW SURE IS "BLOCKLIST AND RE-DOWNLOAD" -------------------------------
#
# Erik: "should have point system for blocklist & redownload". The rungs
# carry a sureness each, which is a fact about the RUNG - every file that
# reaches `missing` shows 90% whether it is a Japanese film with nothing
# inside it or a file nobody has ever opened. The button deletes five
# gigabytes. It should say how sure it is about THIS file.
#
# So the same shape the picture scorer has: every term is evidence already
# in hand, the points are added, and the multipliers are the refusals.
# Measured over the 269 files the check is accusing or cannot decide:
#
#   what is spoken   Whisper heard 259 of them, 219 at 0.90 or better,
#                    and the metadata agrees with the audio on 266. Two
#                    independent readings of the same question.
#   the tracks       211 carry no subtitle track at all; 46 carry a
#                    dialogue track in some other language; 12 carry only
#                    signs or forced.
#   the picture      180 score 0% and 47 have never been read.
#   the show         7 belong to a show that is mostly burned-in - which is
#                    the one thing here that argues the file is fine.
#
# THE TWO BIG TERMS ARE THE TWO HALVES OF THE QUESTION: is it foreign, and
# is there nothing to read. Everything else adjusts.
RAW_POINTS = {
    "heard": 45,        # Whisper heard a language this library does not keep
    "tagged": 30,       # only the audio tag says so
    "meta": 12,         # and the metadata's original language agrees
    "no_track": 30,     # no subtitle track at all
    "signs_only": 22,   # only signs or forced - nothing to read
    "other_lang": 18,   # tracks, but none in a language you asked for
    "pic_zero": 15,     # the picture reader read the frames and got nothing
    "pic_low": 8,       # it scored them under its own throw-away line
}
# And the refusals - things that do not lower the score, they void it.
RAW_VOID = {
    "unread_pic": 0.45,   # nobody has looked at the picture
    "unheard": 0.55,      # nobody has listened to the audio
    "show_burned": 0.15,  # the rest of the show carries burned-in dialogue
    "pic_band": 0.5,      # the picture reader is between its own lines
}


def raw_score(file_id: int, lang: str, cur, facts=None, pic=None,
              path: str = "", minutes: float = 0.0) -> dict:
    """How sure is this file a raw, 0-100, and the sentence explaining it."""
    lang = (lang or "").lower()[:3]
    if facts is None:
        facts = cur.execute("SELECT * FROM sub_facts WHERE file_id=?",
                            (int(file_id),)).fetchone()
    if not path or not minutes:
        try:
            _r = cur.execute("SELECT path, duration FROM files WHERE id=?",
                             (int(file_id),)).fetchone()
            if _r is not None:
                path = path or str(_r["path"] or "")
                minutes = minutes or float(_r["duration"] or 0) / 60.0
        except Exception:                                        # noqa: BLE001
            pass
    try:
        tracks = json.loads((facts["tracks"] if facts else None) or "[]")
    except Exception:                                            # noqa: BLE001
        tracks = []

    pts, why = 0.0, []

    # ---- is it foreign ----------------------------------------------------
    # THE SAME FRESHNESS TEST THE LADDER USES, and it has to be: this asked
    # only that the stored size match, while _audio_langs also requires the
    # mtime to - so a verdict taken before the file was replaced counted
    # here and not there. Six files came back at 0% "the audio is in eng"
    # while the ladder had them at `missing`, which is the check disagreeing
    # with itself in public.
    heard, tagged = set(), set()
    try:
        for a in cur.execute(
                "SELECT a.code FROM audio_lang a "
                "  JOIN files f ON f.id = a.file_id "
                " WHERE a.file_id=? AND COALESCE(a.ok,0)=1 "
                "   AND COALESCE(a.code,'')<>'' "
                "   AND a.size = COALESCE(f.size,0) "
                "   AND ABS(COALESCE(a.mtime,0) - COALESCE(f.mtime,0)) <= 1.0",
                (int(file_id),)):
            heard.add(str(a["code"]).lower()[:3])
    except Exception:                                            # noqa: BLE001
        heard = set()
    try:
        _r = cur.execute("SELECT audio_langs, orig_lang FROM files WHERE id=?",
                         (int(file_id),)).fetchone()
        tagged = {x.strip() for x in str((_r["audio_langs"] if _r else "")
                                         or "").split(",") if x.strip()}
        meta = orig_code(_r["orig_lang"] if _r else "")
    except Exception:                                            # noqa: BLE001
        tagged, meta = set(), ""

    # SPOKEN IN THE LANGUAGE IS NOT A RAW, AT ANY SCORE. Caught by the first
    # run of this over the library: a Scooby-Doo episode with no probe came
    # back at 30% reading "Whisper heard eng and nothing in eng", because the
    # term asked whether anything had been heard rather than whether what was
    # heard was the language. A film you can follow is not a raw however
    # little else is known about it, so it scores nothing at all.
    # AND THE LADDER'S OWN ANSWER TO "IS IT SPOKEN", not a second one built
    # from the tags. Tom and Jerry S1960E04 is tagged eng and Whisper heard
    # kor: _audio_langs prefers the verdict over the tag per track, which is
    # the whole reason it exists, and asking `lang in tagged` here let the
    # lying tag say "not a raw" about a file the ladder had accused. One
    # file, and one is enough - that is the check contradicting itself in
    # public over a five-gigabyte button.
    if lang in _audio_langs(file_id, cur):
        return {"score": 0, "picture": 0, "call": "",
                "why": f"the audio is in {lang} - this is not a raw, whatever "
                       f"else is missing from it"}
    if heard:
        pts += RAW_POINTS["heard"]
        why.append(f"Whisper heard {'/'.join(sorted(heard))} and nothing in "
                   f"{lang}")
    elif tagged:
        pts += RAW_POINTS["tagged"]
        why.append(f"the audio is tagged {'/'.join(sorted(tagged))}, though "
                   f"nobody has listened to it")
    if meta and meta != lang and (meta in heard or not heard):
        pts += RAW_POINTS["meta"]
        why.append(f"the metadata calls it {meta} too")

    # ---- is there nothing to read ----------------------------------------
    mine = [t for t in tracks
            if str(t.get("lang") or "").lower()[:3] == lang]
    if not tracks:
        pts += RAW_POINTS["no_track"]
        why.append("no subtitle track at all")
    elif not mine:
        pts += RAW_POINTS["other_lang"]
        why.append(f"{len(tracks)} subtitle track(s), none of them {lang}")
    else:
        pts += RAW_POINTS["signs_only"]
        why.append(f"its only {lang} track is signs or forced")

    # ---- what the picture says -------------------------------------------
    prow = _picture(file_id, cur, pic)
    call, score = "", 0
    if prow is None:
        pts *= RAW_VOID["unread_pic"]
        why.append("but nobody has looked at the picture")
    else:
        try:
            from . import hardsub as _hs
            _v = _hs.verdict_for({
                "state": str(prow["state"] or ""),
                "low_hits": prow["low_hits"], "samples": prow["samples"],
                "words": str((prow["words"] if "words" in prow.keys()
                              else "") or ""), "path": path})
            call = str(_v.get("auto") or "")
            score = int(_v.get("score") or 0)
        except Exception:                                        # noqa: BLE001
            call = ""
        if score == 0:
            pts += RAW_POINTS["pic_zero"]
            why.append("the picture reader read the frames and got nothing")
        elif call == "dismiss":
            pts += RAW_POINTS["pic_low"]
            why.append(f"the picture scores {score}%, under its throw-away line")
        elif call == "ask":
            pts *= RAW_VOID["pic_band"]
            why.append(f"but the picture scores {score}%, between the reader's "
                       f"own lines - it is not sure")

    if not heard and not tagged:
        pts *= RAW_VOID["unheard"]
        why.append("and nobody has listened to the audio")

    # ---- what the rest of the show says ----------------------------------
    try:
        yes, b, n = show_burns_in(path, cur)
    except Exception:                                            # noqa: BLE001
        yes, b, n = False, 0, 0
    if yes:
        pts *= RAW_VOID["show_burned"]
        why.append(f"and {b} of the {n} episodes of this show that have been "
                   f"settled carry burned-in dialogue")

    return {"score": int(max(0, min(100, round(pts)))),
            "why": "; ".join(why), "picture": score, "call": call}


def _sure_of(state: str, rule: str, file_id: int, lang: str, cur,
             facts=None, pic=None, path: str = "",
             minutes: float = 0.0) -> int:
    r"""The percentage stored on the row.

    A RUNG'S SURENESS IS A FACT ABOUT THE RUNG. Every file that reaches
    `missing` showed 90% - a Japanese film with nothing inside it and a file
    nobody has ever opened, the same number. The button behind that number
    deletes five gigabytes, so where the answer is "replace this" or "nobody
    can decide", the figure is this FILE's own score. See raw_score.

    Everywhere else the rung's own number still stands, because "it carries
    a tagged dialogue track" is as true of one file as of another.
    """
    if state in (MISSING, UNKNOWN):
        try:
            return int(raw_score(file_id, lang, cur, facts, pic,
                                 path, minutes).get("score") or 0)
        except Exception:                                        # noqa: BLE001
            return sureness(rule)
    return sureness(rule)


def raw_scoring() -> dict:
    """The terms, their weights and what each was measured on - for the
    panel, read out of this module so the two cannot drift."""
    P, V = RAW_POINTS, RAW_VOID
    return {
        "how": ("A raw is a film nobody here can follow, and this is how sure "
                "of that nuarr is about one file. The two big terms are the "
                "two halves of the question - is it foreign, and is there "
                "nothing to read - and the rest adjust. The multipliers are "
                "not doubts, they are refusals: evidence nobody has gathered "
                "yet cannot be counted as evidence that the file is bad."),
        "rows": [
            {"points": P["heard"], "what": "Whisper heard a language the "
             "library does not keep",
             "measured": "the listener's own verdict, and the strongest thing "
                         "here: of the 269 files this check is accusing or "
                         "cannot decide, it has heard 259, and 219 of those "
                         "at 0.90 confidence or better"},
            {"points": P["tagged"], "what": "only the audio TAG says it is "
             "foreign",
             "measured": "worth less than hearing it, because a tag is a "
                         "claim - a file tagged eng whose audio is really "
                         "Japanese is the failure the listener exists for"},
            {"points": P["meta"], "what": "and the metadata's original "
             "language agrees",
             "measured": "two independent readings of one question. They "
                         "agree on 266 of the 269; it is a small term "
                         "because the metadata describes the SHOW, and 14,196 "
                         "files here are English dubs of Japanese originals"},
            {"points": P["no_track"], "what": "no subtitle track at all",
             "measured": "211 of the 269. Nothing to read, and nothing nuarr "
                         "can do to the bytes that produces one"},
            {"points": P["signs_only"], "what": "only signs or forced tracks",
             "measured": "12 of the 269. Signs translate a shop front and "
                         "leave the conversation alone"},
            {"points": P["other_lang"], "what": "subtitle tracks, but none in "
             "a language you asked for",
             "measured": "46 of the 269 - a Spanish and a Portuguese track on "
                         "a Japanese film is no help to this house"},
            {"points": P["pic_zero"], "what": "the picture reader read the "
             "frames and got nothing",
             "measured": "180 of the 269 score 0%. It sampled 24 frames, ran "
                         "the OCR and recovered no words"},
            {"points": P["pic_low"], "what": "or scored them under its own "
             "throw-away line",
             "measured": "something was read and it was not language"},
        ],
        "voids": [
            {"mult": V["unread_pic"], "what": "nobody has looked at the "
             "picture",
             "measured": "47 of the 269. The dialogue may be painted into "
                         "every frame and no one has checked"},
            {"mult": V["unheard"], "what": "nobody has listened to the audio",
             "measured": "10 of the 269. What is spoken is the first half of "
                         "the question and it is unanswered"},
            {"mult": V["pic_band"], "what": "the picture reader is between "
             "its own two lines",
             "measured": "32 of the 269. When the scorer that reads pictures "
                         "says it is unsure, this one has no business being "
                         "sure"},
            {"mult": V["show_burned"], "what": "the rest of the show carries "
             "burned-in dialogue",
             "measured": "the strongest refusal there is. Detective Conan "
                         "scores 0% on the picture and 455 of its settled "
                         "episodes demonstrably carry burned-in subtitles the "
                         "OCR cannot transcribe"},
        ],
    }

# -------------------------------------------- how sure, and what has moved ---
_STATS_TTL = 120.0
_STATS: dict = {"at": 0.0, "data": None}


def _rule_stats() -> dict:
    r"""What each rung is carrying now, and what it has had to take back.

    THE PART THAT LEARNS. The base sureness in RULES is fixed; this is the
    counting beside it, and it is counted from the library rather than
    written down anywhere.

    `live` is how many verdicts rest on the rung today. `took_back` is how
    many of its answers have since been replaced by a different verdict -
    from the trigger on sub_need, which fires whenever a re-check moves a row.
    The two together read as "it has answered live+took_back times and taken
    back took_back of them", which is the only hit rate available here: there
    is no second opinion to check a verdict against, so the thing worth
    counting is how often the check disagrees with ITSELF once it knows more.

    For the rungs that make no claim, the interesting figure is the other
    one - of the files this rung held that have since been decided, the share
    that turned out fine. That is `became`, and an empty `became` means
    nothing has moved off the rung yet, which is a fact about the rung's age
    and not about its quality.
    """
    out = {}
    for k in RULES:
        out[k] = {"live": 0, "took_back": 0, "held": 0, "became": {}}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT COALESCE(n.rule,'') AS rule, COUNT(*) AS c "
                    "  FROM sub_need n JOIN files f ON f.id = n.file_id "
                    " WHERE f.state NOT IN ('deleted','duplicate') "
                    " GROUP BY COALESCE(n.rule,'')"):
                d = out.setdefault(r["rule"], {"live": 0, "took_back": 0,
                                               "held": 0, "became": {}})
                d["live"] = int(r["c"] or 0)
            for r in cur.execute(
                    "SELECT was_rule, was, now, COUNT(*) AS c "
                    "  FROM sub_need_log GROUP BY was_rule, was, now"):
                d = out.get(str(r["was_rule"] or ""))
                if d is None:
                    continue
                c = int(r["c"] or 0)
                if str(r["was"]) == str(r["now"]):
                    # Same verdict, different rung - a better reason for the
                    # same answer is not the check changing its mind.
                    d["held"] += c
                else:
                    d["took_back"] += c
                    d["became"][str(r["now"])] = \
                        d["became"].get(str(r["now"]), 0) + c
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _replaces() -> dict:
    """What you did about the one rung that accuses."""
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) AS asked, "
                "       SUM(CASE WHEN ok THEN 1 ELSE 0 END) AS went "
                "  FROM remedy_log WHERE kind=? AND action='replace'",
                (KIND,)).fetchone()
        return {"asked": int((r["asked"] if r else 0) or 0),
                "went": int((r["went"] if r else 0) or 0)}
    except Exception:                                            # noqa: BLE001
        return {"asked": 0, "went": 0}


def _trail_init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subneed_trail(
                day  TEXT PRIMARY KEY,
                at   REAL,
                json TEXT
            )""")


def _trail_write(d: dict) -> None:
    """One row a day, and only when something moved - as subkind does it."""
    try:
        _trail_init()
        day = time.strftime("%Y-%m-%d", time.localtime())
        # NAMED RUNGS ONLY. Rows written before the rung column existed
        # answer to '', and a trail row saying 39,890 files sat on a rung
        # with no name is a record of the migration, not of the library.
        small = {k: [v.get("live", 0), v.get("took_back", 0)]
                 for k, v in (d.get("rules") or {}).items()
                 if k in RULES and (v.get("live") or v.get("took_back"))}
        blob = json.dumps(small, sort_keys=True)
        with cursor() as cur:
            row = cur.execute("SELECT json FROM subneed_trail "
                              " ORDER BY day DESC LIMIT 1").fetchone()
            if row and str(row["json"] or "") == blob:
                return                       # nothing moved; nothing to say
            cur.execute(
                "INSERT INTO subneed_trail(day, at, json) VALUES(?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET at=excluded.at, "
                "  json=excluded.json", (day, time.time(), blob))
    except Exception:                                            # noqa: BLE001
        pass


def trail(limit: int = 90) -> list:
    """The dated rows, oldest first."""
    out = []
    try:
        _trail_init()
        with cursor() as cur:
            rows = cur.execute(
                "SELECT day, at, json FROM subneed_trail "
                " ORDER BY day DESC LIMIT ?", (int(limit),)).fetchall()
        for r in reversed(rows):
            try:
                out.append({"day": r["day"], "at": r["at"],
                            "rules": json.loads(r["json"] or "{}")})
            except Exception:                                    # noqa: BLE001
                pass
    except Exception:                                            # noqa: BLE001
        return []
    return out


def stats(fresh: bool = False) -> dict:
    """The recounted figures, held for two minutes, written to the trail."""
    init()
    now = time.time()
    if not fresh and _STATS["data"] and now - _STATS["at"] < _STATS_TTL:
        return _STATS["data"]
    d = {"rules": _rule_stats(), "replaces": _replaces(), "at": now}
    _STATS.update(at=now, data=d)
    _trail_write(d)
    return d


def scoring() -> dict:
    r"""Every rung of the ladder, how sure it is, and what it is carrying.

    THE SAME BARGAIN THE PANEL ABOVE MAKES. The weights are read out of
    RULES rather than typed into the page, so the table cannot say anything
    the code does not do; the counts are recounted from the library on the
    way in, so they move when it does.
    """
    st = stats()
    rules = st.get("rules") or {}
    hist = trail(400)
    first = (hist[0] if hist else {}) or {}
    first_rules = first.get("rules") or {}

    rows = []
    for rid in RULE_ORDER:
        R = RULES[rid]
        got = rules.get(rid) or {}
        live = int(got.get("live") or 0)
        back = int(got.get("took_back") or 0)
        d = {"id": rid, "says": R["says"], "short": R["short"],
             "line": R["line"], "rests": R["rests"],
             "sure": int(R.get("sure") or 0),
             "live": live, "took_back": back, "held": int(got.get("held") or 0),
             "became": got.get("became") or {}}
        if live + back:
            d["kept"] = round((live) * 100.0 / (live + back))
        was = first_rules.get(rid)
        if was and was != [live, back]:
            d["since"] = {"live": was[0], "took_back": was[1],
                          "day": first.get("day") or ""}
        rows.append(d)

    rep = st.get("replaces") or {}
    return {
        "rows": rows,
        "how": ("A RAW is a film nobody here can follow: spoken in a "
                "language you have not got, with no dialogue you can read. "
                "That is the only thing this check hunts, and it is the only "
                "thing it replaces a release over. The rungs are tried in "
                "this order and the first one that matches answers the "
                "question, so a file's sureness is the sureness of the rung "
                "that caught it - and the rungs that make no claim carry "
                "none, because \"open\" is a verdict about what nuarr knows "
                "rather than about the file."),
        "acts": ("Only the two raw rungs are wired to a button, and in "
                 "auto mode only those two blocklist the release and ask the "
                 "arr for another copy. A file you can already follow is "
                 "listed and left alone however loudly the rule asked for a "
                 "subtitle - deleting a film you understand is a loud answer "
                 "to a quiet question. An open question is never acted on "
                 "either, which is what keeps the files waiting on a re-read "
                 "off a list offering to delete them."),
        "replaced": rep,
        "counted": {"files": sum(int((v or {}).get("live") or 0)
                                 for k, v in rules.items() if k in RULES),
                    "moved": sum(int((v or {}).get("took_back") or 0)
                                 for k, v in rules.items() if k in RULES),
                    "at": st.get("at", 0.0)},
        "trail": [{"day": h.get("day"), "rules": h.get("rules") or {}}
                  for h in hist],
        "learns": ("Two things here are counted and not written down. How "
                   "many files each rung is carrying, which moves every time "
                   "the sweep runs; and how many of its answers it has since "
                   "taken back, which is recorded by a trigger on the verdict "
                   "table the moment a re-check moves a row. Nothing else "
                   "learns on its own - the sureness of a rung is fixed, "
                   "because a weight that drifted would let a file change its "
                   "mind overnight with nothing about the file having "
                   "changed. Read a rung that keeps taking answers back as an "
                   "argument that its weight is too high, and read an empty "
                   "\"taken back\" as what it is: nothing has moved off that "
                   "rung yet."),
    }


def _stream_index(file_id: int, ord_: int, cur_=None) -> int:
    """The ffprobe stream index of the ord_-th subtitle track, from the probe."""
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if not r:
            return 0
        d = json.loads(r["json"] or "{}")
    except Exception:                                            # noqa: BLE001
        return 0
    n = 0
    for st in (d.get("streams") or []):
        if st.get("codec_type") != "subtitle":
            continue
        if n == ord_:
            try:
                return int(st.get("index") or 0)
            except (TypeError, ValueError):
                return 0
        n += 1
    return 0


def unread_tracks(limit: int = 400) -> list:
    r"""Files whose only track in a required language has never been read.

    THE RUNG SAYS "QUEUED TO BE READ" AND NOTHING WAS QUEUING IT.
    signs_unread is reached when a file's only English track claims to be
    signs - forced, or titled for them - and the container reports no cue
    count, so the probe cannot tell a sign sheet from a full script. The
    honest answer is to read it, and the rung said so, and no reader ever
    picked those files up: the track reader's candidates are tracks whose
    TITLE CONTRADICTS THEIR CUE RATE, and a track with no cue rate
    contradicts nothing.

    Drug Store in Another World is the case. One ASS track, named "English",
    flagged forced and default by the release, no cue count in the header.
    The probe is not stale - it matches the file byte for byte - so
    re-probing it a hundred times would say the same thing. Reading it takes
    a second and settles it: 361 events, 357 of them plain dialogue lines,
    15.3 a minute, running through all ten slices of the runtime. A full
    script wearing a forced flag, and ten episodes of it sat on the raw list.

    Returned in the shape the track reader wants, so readers.py can feed
    them to the same subread pool as everything else.
    """
    init()
    out = []
    try:
        with cursor() as cur:
            rows = cur.execute(
                "SELECT n.file_id, n.lang, f.path, f.size, f.pool_disk, "
                "       s.tracks "
                "  FROM sub_need n JOIN files f ON f.id = n.file_id "
                "  LEFT JOIN sub_facts s ON s.file_id = n.file_id "
                " WHERE n.rule = 'signs_unread' "
                "   AND f.state NOT IN ('deleted','duplicate') "
                " LIMIT ?", (int(limit),)).fetchall()
    except Exception:                                            # noqa: BLE001
        return []
    for r in rows:
        try:
            tks = json.loads(r["tracks"] or "[]")
        except Exception:                                        # noqa: BLE001
            continue
        lang = str(r["lang"] or "").lower()[:3]
        for t in tks:
            if str(t.get("lang") or "").lower()[:3] != lang:
                continue
            # ALREADY READ ONCE IS NOT READ AGAIN. shape_of stores a row per
            # (file, track); without this the same ten episodes would be
            # pulled apart on every pass for ever.
            ord_ = int(t.get("ord") or 0)
            try:
                with cursor() as cur:
                    seen = cur.execute(
                        "SELECT 1 FROM subtitle_shape "
                        " WHERE file_id=? AND track=? AND size=?",
                        (int(r["file_id"]), ord_ + 1,
                         int(r["size"] or 0))).fetchone()
            except Exception:                                    # noqa: BLE001
                seen = None
            if seen:
                continue
            # THREE DIFFERENT NUMBERS FOR ONE TRACK, and they are not the
            # same: subtitle_shape.track is 1-based, sub_facts ord is
            # 0-based, and mkvextract wants the ffprobe STREAM INDEX - which
            # counts video and audio too. sub_facts does not store that one,
            # so it comes out of the probe. Getting this wrong extracted the
            # video stream as text once already; see subtitletitle.
            mkv_id = _stream_index(int(r["file_id"]), ord_, cur_=None)
            out.append({
                "file_id": int(r["file_id"]), "path": r["path"] or "",
                "pool_disk": r["pool_disk"] or "",
                "track": ord_ + 1, "mkv_id": mkv_id,
                "size": int(r["size"] or 0),
                "old": str(t.get("title") or ""),
                "why": "its only track in the language claims to be signs and "
                       "the container does not say how many lines it has",
            })
            break
    return out


UNKNOWN_KINDS = ("unread", "stale", "open")
UNKNOWN_WORDS = {"unread": "not looked inside",
                 "stale": "waiting on a re-read",
                 "open": "undecided"}


def unknown_files(kind: str = "", limit: int = 600) -> dict:
    """The files behind one of the unknown counts, newest first.

    Grouped as well as listed: 380 rows is not a list anybody reads, but
    "Detective Conan, 80 of them, all saying the same thing" is an answer.
    """
    init()
    kind = kind if kind in UNKNOWN_KINDS else ""
    try:
        from . import hardsub as _hs
        rev = int(_hs.READER_REV)
    except Exception:                                            # noqa: BLE001
        rev = 0
    rows, by_show = [], {}
    try:
        with cursor() as cur:
            q = ("SELECT CASE WHEN h.file_id IS NULL THEN 'unread' "
                 "            WHEN COALESCE(h.rev,1) < ? "
                 "                 AND COALESCE(h.chosen,'')='' THEN 'stale' "
                 "            ELSE 'open' END AS k, "
                 "       n.file_id, n.lang, n.why, n.checked_at, "
                 "       COALESCE(n.rule,'') AS rule, n.sure, f.first_seen, "
                 "       f.path, f.title, f.season, f.episode, f.library "
                 "  FROM sub_need n "
                 "  JOIN files f ON f.id = n.file_id "
                 "  LEFT JOIN hardsub h ON h.file_id = n.file_id "
                 " WHERE n.state = ? "
                 "   AND f.state NOT IN ('deleted','duplicate')")
            args: list = [rev, UNKNOWN]
            if kind:
                q += " AND (CASE WHEN h.file_id IS NULL THEN 'unread' " \
                     "           WHEN COALESCE(h.rev,1) < ? " \
                     "                AND COALESCE(h.chosen,'')='' THEN 'stale' " \
                     "           ELSE 'open' END) = ?"
                args += [rev, kind]
            q += " ORDER BY f.title, f.season, f.episode"
            for r in cur.execute(q, tuple(args)):
                show = _show_of(str(r["path"] or ""))
                g = by_show.setdefault(show, {"show": show, "n": 0,
                                              "library": r["library"] or "",
                                              "kind": r["k"], "why": ""})
                g["n"] += 1
                # THE WHOLE SENTENCE. It was cut at 160 characters, which
                # landed mid-word on every row - "burned-in subtitles it
                # cannot transcribe look exactly like th" - and the cut fell
                # exactly where the sentence was about to say what it meant.
                if not g["why"]:
                    g["why"] = str(r["why"] or "")
                g["rule"] = g.get("rule") or str(r["rule"] or "")
                g.setdefault("files", []).append({
                    "file_id": int(r["file_id"]),
                    "rule": str(r["rule"] or ""),
                    "label": _row_label(r),
                    "season": r["season"], "episode": r["episode"],
                    "lang": r["lang"],
                    "why": str(r["why"] or ""),
                    "at": float(r["checked_at"] or 0.0)})
                if len(rows) < max(1, int(limit)):
                    rows.append({
                        "file_id": int(r["file_id"]), "kind": r["k"],
                        "kind_word": UNKNOWN_WORDS.get(r["k"], r["k"]),
                        "lang": r["lang"], "show": show,
                        "library": r["library"] or "",
                        "label": _row_label(r), "path": str(r["path"] or ""),
                        "why": str(r["why"] or ""),
                        "rule": str(r["rule"] or ""),
                        "first_seen": float(r["first_seen"] or 0.0),
                        "sure": int(r["sure"] or 0),
                        "at": float(r["checked_at"] or 0.0)})
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:160],
                "rows": [], "shows": []}
    shows = sorted(by_show.values(), key=lambda g: -g["n"])
    return {"ok": True, "kind": kind, "word": UNKNOWN_WORDS.get(kind, "unknown"),
            "rows": rows, "shows": shows,
            "total": sum(g["n"] for g in shows), "shown": len(rows)}


def _row_label(r) -> str:
    """'Detective Conan - S16E08', not the whole release name."""
    try:
        from .db import display_label
        return display_label(r["title"], r["season"], r["episode"])
    except Exception:                                            # noqa: BLE001
        return str(r["title"] or "")


def _show_of(path: str) -> str:
    """'Detective Conan (1996) {tvdb-72454}' out of a pool path."""
    parts = [p for p in str(path or "").replace("/", "\\").split("\\") if p]
    return parts[2] if len(parts) > 2 else (parts[-1] if parts else "")


def prune() -> int:
    r"""Forget verdicts about files that are not there any more.

    A replacement gets a NEW files row, so a verdict against the old one is
    not waiting for anything: it can never be answered, acted on or listed.
    Left alone they pile up - 43 of them here, every single one dead - and
    they were the whole of what the panel was calling "waiting".

    Called from the sweep, where the verdicts are made, so the table cannot
    fill up with them again. What was actually DONE is in remedy_log, which is
    where answered_count() reads from, so nothing is lost by dropping these.
    """
    init()
    try:
        with cursor() as cur:
            cur.execute(
                "DELETE FROM sub_need WHERE file_id IN ("
                "  SELECT n.file_id FROM sub_need n "
                "    LEFT JOIN files f ON f.id = n.file_id "
                "   WHERE f.id IS NULL "
                "      OR f.state IN ('deleted','duplicate'))")
            return int(cur.rowcount or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def wanting(limit: int = 400, library: str = "") -> list[dict]:
    r"""The rule asks for a subtitle these have not got - and they can still
    be followed, so nothing is offered to delete them.

    Kept separate from missing() rather than filtered out of it, because the
    two lists mean different things to whoever is reading them and only one
    of them has a button. See WANT.
    """
    init()
    sql = ("SELECT n.file_id, n.lang, n.library, n.why, n.checked_at, "
           "       n.rule, n.sure, "
           "       f.path, f.title, f.season, f.episode, f.size, f.pool_disk, "
           "       f.first_seen "
           "  FROM sub_need n JOIN files f ON f.id=n.file_id "
           " WHERE n.state=? AND f.state NOT IN ('deleted','duplicate')"
           + (" AND n.library=?" if library else "") +
           " ORDER BY f.title, f.season, f.episode LIMIT ?")
    args = ((WANT, library, int(limit)) if library else (WANT, int(limit)))
    try:
        with cursor() as cur:
            out = [dict(r) for r in cur.execute(sql, args)]
    except Exception:                                            # noqa: BLE001
        return []
    for r in out:
        r["short"] = short_why(r.get("why") or "")
        r["rule_line"] = rule_of(r.get("rule") or "").get("line") or ""
    return out


def missing(limit: int = 200, library: str = "") -> list[dict]:
    """The files that are actually missing something, newest verdict first."""
    init()
    # first_seen is WHEN THE FILE ARRIVED, which is what an "added" column
    # means to a person deciding about it - "this landed an hour ago" says
    # it is the batch just grabbed. The first draft sent 0 and the column
    # drew a dash on every row. Erik: "fix date under ADDED column".
    sql = ("SELECT n.file_id, n.lang, n.library, n.why, n.checked_at, "
           "       n.rule, n.sure, "
           "       f.path, f.title, f.season, f.episode, f.size, f.pool_disk, "
           "       f.first_seen "
           "  FROM sub_need n JOIN files f ON f.id=n.file_id "
           # THE SAME LIVE FILTER EVERY OTHER LIST USES. answered() takes a
           # row off the moment the button is pressed; this catches the ones
           # auto replaced, and anything deleted by a path nobody thought of.
           " WHERE n.state='missing' AND f.state NOT IN ('deleted','duplicate')"
           + (" AND n.library=?" if library else "") +
           " ORDER BY f.title, f.season, f.episode LIMIT ?")
    args = ((library, int(limit)) if library else (int(limit),))
    try:
        with cursor() as cur:
            out = [dict(r) for r in cur.execute(sql, args)]
    except Exception:                                            # noqa: BLE001
        return []
    for r in out:
        r["short"] = short_why(r.get("why") or "")
        r["rule_line"] = rule_of(r.get("rule") or "").get("line") or ""
    return out


def short_why(why: str) -> str:
    """The sentence, as a phrase. Erik: "simplify title info".

    The long form is right for a tooltip and wrong for a column: "carries no
    subtitles at all, and nothing in the picture" is read once, and after
    that the eye wants a word it can run down.
    """
    w = (why or "").lower()
    if w.startswith("carries no subtitles at all"):
        return "no subtitles at all"
    if w.startswith("carries "):
        # "carries 2 subtitle(s), none of them eng, and nothing in the picture"
        try:
            n = int(w.split()[1])
        except (IndexError, ValueError):
            n = 0
        return f"{n} other-language track{'s' if n != 1 else ''}"
    return why[:40]


def answered(file_id: int) -> None:
    r"""The release was replaced. Take the question off the list.

    Erik: "I did a batch edit of all the Dragon Ball GT but it didn't
    disappear from the scroll box". The batch worked - five replaces in the
    remedy ledger, all ok - but the rows stayed, because a successful replace
    changes the FILE and this table still held the verdict about it. Measured
    after: 22 rows still saying `missing`, five of them for files whose row
    was already gone or marked deleted.

    Deleting the verdict is right rather than rewriting it: the question was
    "does this file carry eng", and there is no longer a file to ask it of.
    When the replacement lands, remedy.reconsider() judges it from scratch.
    """
    init()
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM sub_need WHERE file_id=?", (int(file_id),))
    except Exception:                                            # noqa: BLE001
        pass


def answered_list(limit: int = 200) -> list[dict]:
    r"""What this panel has already replaced. From the LEDGER, not this table.

    Erik: "can the panel have a link below allowing to see the answered one
    like on the top panel".

    The top panel can filter its own rows because answering one leaves the row
    behind with `done` set. This panel cannot: answered() DELETES the verdict,
    which is right - the file is gone, and a question about a file that no
    longer exists is not a question being kept, it is a stale row waiting to
    confuse somebody. See its docstring.

    So the answered list comes from where the answer actually lives:
    remedy_log, which records every replace with its kind, its source, whether
    it worked and what was on disk at the time. That record outlives the file,
    which is exactly the property this needs - and it is the same ledger the
    other six checks write to, so "what did I replace, and when" has one
    answer across all of them.
    """
    init()
    try:
        with cursor() as cur:
            rows = cur.execute(
                "SELECT r.at, r.file_id, r.ok, r.auto, r.detail, r.path, "
                "       f.title, f.season, f.episode, f.library "
                "  FROM remedy_log r LEFT JOIN files f ON f.id = r.file_id "
                " WHERE r.kind = ? AND r.action = 'replace' "
                " ORDER BY r.at DESC LIMIT ?", (KIND, int(limit))).fetchall()
    except Exception:                                            # noqa: BLE001
        return []
    out = []
    for r in rows:
        d = dict(r)
        # THE FILE IS USUALLY GONE, which is the point - so the name comes
        # from the path the ledger kept rather than from a row that may no
        # longer exist.
        import os
        d["name"] = (d.get("title") or
                     os.path.basename(d.get("path") or "") or
                     f"file {d['file_id']}")
        d["gone"] = d.get("title") is None
        out.append(d)
    return out


def answered_count() -> int:
    init()
    try:
        with cursor() as cur:
            r = cur.execute("SELECT COUNT(*) n FROM remedy_log "
                            " WHERE kind=? AND action='replace'",
                            (KIND,)).fetchone()
        return int((r["n"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def unknown_sample(limit: int = 20) -> list[dict]:
    """A few of the never-looked-at, so the count is not just a number."""
    init()
    try:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT n.file_id, n.lang, n.library, f.path, f.title "
                "  FROM sub_need n JOIN files f ON f.id=n.file_id "
                " WHERE n.state='unknown' LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def findings(limit: int = 50) -> list[dict]:
    r"""For remedy.auto(). ONLY `missing`.

    Never `unknown`, for the reason in the module docstring - a question
    nobody has answered is not a fault. And never `want` either: the rule
    asks for a subtitle those files have not got, and the audio is in the
    language, so there is nothing about them a viewer cannot follow. Deleting
    a release over that is a loud answer to a quiet question.

    Nothing here needs changing to enforce that - missing() selects
    state='missing' - but the rule is written down because `want` arrived
    after this function did, and the next person to widen this query needs to
    know which of the four states a button is allowed to touch.
    """

    return [{"file_id": int(r["file_id"]), "kind": KIND,
             "path": r.get("path") or "",
             "why": f"no {r['lang']} subtitles - {r['why']}"}
            for r in missing(limit)]


def snapshot() -> dict:
    c = counts()
    req = required()
    return {"counts": c, "required": req,
            "any_required": bool(req),
            "mode": mode(),
            "running": bool(STATE["running"]),
            "done": int(STATE.get("done") or 0),
            "total": int(STATE.get("total") or 0),
            "elapsed": (round(time.time() - STATE["t0"], 1)
                        if STATE["running"] and STATE.get("t0") else 0.0),
            "at": STATE["at"], "took": STATE["took"], "runs": STATE["runs"],
            "err": STATE["err"],
            "age_s": (round(time.time() - STATE["at"]) if STATE["at"] else None),
            "poll_s": POLL_S,
            "answered": answered_count(),
            # THE QUIET FINDING, COUNTED IN THE HEADER. It has no button and
            # it is not a fault, so it would be invisible without this - and
            # a check that silently decides 2,936 files do not matter is a
            # check nobody can argue with. See WANT.
            "want": int(c.get(WANT) or 0),
            # ITS SCHEDULE ROW, so the panel can say when the next pass is
            # rather than only offering a button. Erik: "make the check now a
            # job time". It is already registered - this is the panel finally
            # reading it.
            "next_run": _next_run()}


def _next_run() -> float:
    try:
        from . import schedules
        for r in (schedules.snapshot() or {}).get("rows", []):
            if r.get("key") == "subneed":
                return float(r.get("next_run") or 0.0)
    except Exception:                                            # noqa: BLE001
        pass
    return 0.0


# ------------------------------------------------------------ the loop ------
def _total() -> int:
    """How many files the required libraries hold - the bar's denominator."""
    libs = list(required())
    if not libs:
        return 0
    try:
        with cursor() as cur:
            return int(cur.execute(
                "SELECT COUNT(*) n FROM files WHERE library IN (%s) "
                "  AND state NOT IN ('deleted','duplicate')"
                % ",".join("?" * len(libs)), tuple(libs)).fetchone()["n"])
    except Exception:                                            # noqa: BLE001
        return 0


def sweep_all() -> dict:
    r"""Judge EVERY file, not one batch, and say how far along it is.

    Erik: "fix the scan now ... and show scanning animation". The button
    called sweep() once, which judges BATCH rows and returns - a fifth of the
    library - and reported nothing while it did it. So Check now looked like
    it had done something small or nothing at all, and there was no way to
    tell which.

    This runs batches back to back until every file has been judged in THIS
    run, keeps done/total current so a bar can move, and refuses to overlap
    itself. It is still cheap - stored facts only, no disk - so the whole
    library is a few seconds; but a few seconds with nothing moving reads as
    broken, and a few seconds with a bar reads as working.
    """
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    total = _total()
    # Clear the dead verdicts BEFORE judging, so the counts this run produces
    # are about files that exist - see prune().
    prune()
    STATE.update(running=True, done=0, total=total, t0=time.time(), err="")
    started = time.time()
    n = 0
    try:
        # sweep() picks the least-recently-judged rows first, so running it
        # until it has covered `total` rows covers the library exactly once.
        while n < total:
            r = sweep(BATCH)
            got = int(r.get("checked") or 0)
            if not got:
                break
            n += got
            STATE["done"] = min(n, total)
            # sweep() flips running off when it returns; this run is not over.
            STATE["running"] = True
    finally:
        STATE.update(running=False, done=min(n, total))
    return {"ok": True, "checked": n, "total": total,
            "took": round(time.time() - started, 1), **counts()}


async def act_now(limit: int = 50) -> dict:
    r"""Hand what is missing to the shared remedy, now.

    Erik enabled auto, pressed Check now, and asked when the files would be
    fixed. The honest answer at the time was "not because of anything you just
    did": Check now ran sweep_all(), which JUDGES files and nothing else. Only
    the fifteen-minute loop below called remedy.auto(), so the scan he asked
    for could not act however many findings it produced.

    Judging and acting are still separate - a sweep in manual mode must touch
    nothing - but "check now" in auto mode plainly means both, so the button
    calls this after the sweep and reports what came of it.

    The cap is remedy's and is shared across every check. Being refused by it
    is a normal outcome, not a failure, and it is returned rather than
    swallowed so the panel can say so.
    """
    if mode() != "auto":
        return {"ok": True, "why": "manual mode - nothing acted on",
                "replaced": 0}
    from . import remedy
    got = findings(limit)
    if not got:
        return {"ok": True, "why": "nothing to act on", "replaced": 0}
    r = await remedy.auto(got, "subneed", True)
    # Anything replaced has had its question answered, so it leaves the list
    # here the same way the buttons do.
    return {"ok": True, **r}


async def watch() -> None:
    from . import schedules
    schedules.register(
        "subneed", "Raws - nothing here you can read", "Subtitles", POLL_S,
        what="Asks, for every file, whether it carries the subtitle languages "
             "its library requires - and says so when a file carries none and "
             "no re-encode could produce one. Reads stored facts only; opens "
             "nothing.",
        toggle="subneed_mode")
    await asyncio.sleep(90)
    while True:
        try:
            schedules.beat("subneed")
            r = await asyncio.to_thread(sweep)
            schedules.REG["subneed"]["last_result"] = (
                f"{r.get('missing', 0)} missing, {r.get('unknown', 0)} unread"
                if r.get("checked") else (r.get("why") or "nothing required"))
            if r.get("missing"):
                await act_now(50)
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"missing-subtitle check: {type(e).__name__}: {e}",
                       "warn", system="subneed")
        await asyncio.sleep(POLL_S)
