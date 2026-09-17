r"""nuarr - files that do not carry a subtitle language the library requires.

THE FILE THIS EXISTS FOR
------------------------
    The Villager of Level 999 (2026) - S01E08 - Incompatible
        [WEBDL-1080p][8bit][x264][AAC 2.0][JA]-ToonsHub.mkv

ffprobe on it, on disk, right now: one h264 video stream, one aac stream
tagged jpn. That is the whole file. No subtitle track, no sidecar beside it,
nothing burned into the picture. Anime Shows keeps English subtitles, and
there is no arrangement of these bytes that produces one - the planner cannot
translate, the OCR has no picture to read and the listener hears Japanese.
Every check nuarr has says this file is fine, because every check nuarr has
asks whether what is there is correct, and nothing was asking whether what is
required is there at all.

So the file sat at `done`, and the first person to find out was whoever
pressed play.

WHY THE VERDICT HAS THREE VALUES AND NOT TWO
--------------------------------------------
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

KIND = "subs/missing-language"

# THE SAME LINE hardsub.py DRAWS FOR "A DIALOGUE CADENCE", and deliberately
# the same number rather than one of this module's own: it is being used to
# ask the same question, and two thresholds for one question drift apart.
# hardsub counts frames with something bright LOW in the picture - where
# subtitles live - and calls 20% of them a dialogue rhythm.
PICTURE_MARKS_RATIO = 0.20

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
               "ok": 0, "missing": 0, "unknown": 0, "err": "", "runs": 0,
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
def _audio_langs(file_id: int, cur) -> set[str]:
    r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                    (int(file_id),)).fetchone()
    if not r:
        return set()
    try:
        streams = json.loads(r["json"]).get("streams") or []
    except Exception:                                            # noqa: BLE001
        return set()
    return {str((s.get("tags") or {}).get("language") or "").lower()[:3]
            for s in streams if s.get("codec_type") == "audio"}


def _picture(file_id: int, cur, pic=None):
    if pic is not None:
        return pic
    try:
        return cur.execute(
            "SELECT state, chosen, low_hits, samples, words FROM hardsub "
            " WHERE file_id=?", (int(file_id),)).fetchone()
    except Exception:                                            # noqa: BLE001
        return None


def verdict(file_id: int, lang: str, cur, facts=None,
            untagged_ok: bool = True, pic=None) -> tuple[str, str]:
    """Does this file carry `lang` subtitles? Returns (state, why).

    `facts` is an optional pre-fetched sub_facts row, so a sweep over 40,000
    files does one query per file instead of two.
    """
    lang = (lang or "").lower()[:3]
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
        return UNKNOWN, "nuarr has not looked inside this file yet"
    if facts is None:
        return UNKNOWN, "the subtitle reader has not read this file yet"

    try:
        tracks = json.loads(facts["tracks"] or "[]")
    except Exception:                                            # noqa: BLE001
        tracks = []
    try:
        sides = json.loads(facts["sides"] or "[]")
    except Exception:                                            # noqa: BLE001
        sides = []

    for t in tracks:
        if str(t.get("lang") or "").lower()[:3] == lang:
            return OK, f"carries a {lang} subtitle track"
    for s in sides:
        if str(s.get("lang") or "").lower()[:3] == lang:
            return OK, f"has a {lang} subtitle file beside it"

    # AN UNLABELLED TRACK MIGHT BE THE ONE. The library keeps untagged tracks
    # precisely because releases leave subtitles unlabelled; reading one to
    # find out costs an extract and an OCR, and until somebody does, "there is
    # no English track" is a guess wearing a fact's clothes.
    if untagged_ok:
        for t in tracks:
            if str(t.get("lang") or "").lower()[:3] in ("", "und", "un"):
                return OK, ("has an untagged subtitle track, which this "
                            "library keeps - it may be the one")

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
    prow = _picture(file_id, cur, pic)
    if prow is None:
        return UNKNOWN, ("the picture reader has not looked at this file, so "
                         "burned-in subtitles cannot be ruled out")
    pstate = str((prow["chosen"] if "chosen" in prow.keys() else None)
                 or prow["state"] or "")
    # `signs` is deliberately not on this list. Signs and songs are not
    # dialogue, and a file carrying only those still needs subtitles.
    if pstate in ("dialogue", "hybrid"):
        return OK, "the dialogue is burned into the picture"

    if lang in _audio_langs(file_id, cur):
        return OK, f"the audio is already in {lang}"

    n_s = int(prow["samples"] or 0)
    n_lo = int(prow["low_hits"] or 0)
    if n_s and (n_lo / n_s) >= PICTURE_MARKS_RATIO:
        return UNKNOWN, (
            f"the picture reader found marks low in the picture in "
            f"{n_lo} of {n_s} frames but could read none of them - burned-in "
            f"subtitles it cannot transcribe look exactly like this")
    if not n_s:
        return UNKNOWN, ("the picture reader has no frames for this file, so "
                         "burned-in subtitles cannot be ruled out")

    n = len(tracks) + len(sides)
    return MISSING, ("carries no subtitles at all, and nothing in the picture"
                     if not n
                     else f"carries {n} subtitle(s), none of them {lang}, "
                          f"and nothing in the picture")


# -------------------------------------------------------------- the sweep ---
def check_one(file_id: int, cur=None) -> dict:
    """Judge one file against its library's required languages."""
    init()
    close = cur is None
    ctx = cursor() if close else None
    cur = ctx.__enter__() if close else cur
    try:
        row = cur.execute("SELECT id, library, path, state FROM files "
                          " WHERE id=?", (int(file_id),)).fetchone()
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
            st, why = verdict(file_id, lang, cur, facts, untagged)
            out[lang] = st
            cur.execute(
                "INSERT INTO sub_need(file_id,lang,library,state,why,checked_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(file_id,lang) DO UPDATE SET "
                " library=excluded.library, state=excluded.state, "
                " why=excluded.why, checked_at=excluded.checked_at",
                (int(file_id), lang, lib, st, why, now))
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
    tally = {OK: 0, MISSING: 0, UNKNOWN: 0}
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
                    "SELECT f.id, f.library, s.tracks, s.sides, s.picture, "
                    "       h.state, h.chosen, h.low_hits, h.samples, "
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
                        st, why = verdict(r["id"], lang, cur, r, untagged, prow)
                        tally[st] = tally.get(st, 0) + 1
                        cur.execute(
                            "INSERT INTO sub_need"
                            "(file_id,lang,library,state,why,checked_at) "
                            "VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(file_id,lang) DO UPDATE SET "
                            " library=excluded.library, state=excluded.state,"
                            " why=excluded.why, checked_at=excluded.checked_at",
                            (int(r["id"]), lang, lib, st, why, now))
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
                     unknown=whole[UNKNOWN], runs=STATE["runs"] + 1)
    return {"ok": True, "checked": n, "pass": tally, **counts()}


# ------------------------------------------------------------- for the UI ---
def counts() -> dict:
    init()
    out = {OK: 0, MISSING: 0, UNKNOWN: 0}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT state, COUNT(*) n FROM sub_need "
                                 " GROUP BY state"):
                out[r["state"]] = int(r["n"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def missing(limit: int = 200, library: str = "") -> list[dict]:
    """The files that are actually missing something, newest verdict first."""
    init()
    # first_seen is WHEN THE FILE ARRIVED, which is what an "added" column
    # means to a person deciding about it - "this landed an hour ago" says
    # it is the batch just grabbed. The first draft sent 0 and the column
    # drew a dash on every row. Erik: "fix date under ADDED column".
    sql = ("SELECT n.file_id, n.lang, n.library, n.why, n.checked_at, "
           "       f.path, f.title, f.season, f.episode, f.size, f.pool_disk, "
           "       f.first_seen "
           "  FROM sub_need n JOIN files f ON f.id=n.file_id "
           " WHERE n.state='missing'"
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
    """For remedy.auto(). ONLY `missing` - never `unknown`; see the docstring."""
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
            "poll_s": POLL_S}


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


async def watch() -> None:
    from . import schedules
    schedules.register(
        "subneed", "Missing subtitle languages", "Subtitles", POLL_S,
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
                from . import remedy
                await remedy.auto(findings(50), "subneed", mode() == "auto")
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"missing-subtitle check: {type(e).__name__}: {e}",
                       "warn", system="subneed")
        await asyncio.sleep(POLL_S)
