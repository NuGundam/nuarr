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
    m = str(getattr(SETTINGS, "subtitletitle_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


def sure_at() -> int:
    """The line auto will not act below.

    AUTO THAT ACTS ON EVERYTHING IT FOUND IS NOT A MODE, IT IS A DARE. The
    findings run from 55% - a "Signs/Songs" track at 8.2 cues a minute, right
    on the lower edge of the speech band, which could genuinely be a dense
    sign sheet - up to the unarguable ones in the middle of the band. Only
    the second kind belongs to a machine.
    """
    try:
        return max(50, min(100, int(getattr(SETTINGS,
                                            "subtitletitle_sure_at", 70))))
    except Exception:                                            # noqa: BLE001
        return 70


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
        out.append({"track": s_i, "old": old, "new": new, "why": why,
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


def scan(limit: int = 0) -> dict:
    r"""Every contradicted subtitle title in the library, from stored probes."""
    rows, checked = [], 0
    _CACHE.update(running=True, done=0, total=0, now="", t0=time.time(), t1=0.0)
    try:
        with cursor() as cur:
            got = cur.execute(
                "SELECT f.id, f.path, f.title, f.library, f.season, "
                "       f.episode, p.json "
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
                           label=_label(r))
                rows.append(row)
    finally:
        _CACHE.update(running=False, done=checked, now="", t1=time.time())
    rows.sort(key=lambda r: (not r.get("rewritable"), r.get("sure", 0)))
    data = {"rows": rows, "checked": checked, "at": time.time(),
            "took": round(_CACHE["t1"] - _CACHE["t0"], 1),
            "fixable": sum(1 for r in rows if r["rewritable"])}
    _CACHE.update(at=time.time(), data=data)
    if mode() == "auto":
        # ONLY THE SURE ONES, AND ONLY THE SAFE ONES. Two filters, and they
        # are about different things: `rewritable` is whether a replacement
        # can be written without losing what a group put there, `sure` is
        # whether the finding is right at all.
        want = [r for r in rows
                if r.get("rewritable") and r.get("sure", 0) >= sure_at()]
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


def cached() -> dict:
    """The last scan, re-running it once a day or when it has never run."""
    d = _CACHE.get("data")
    if d and time.time() - _CACHE["at"] < EVERY_S:
        return d
    return scan()


def refresh() -> dict:
    _CACHE["at"] = 0.0
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
    d = dict(_CACHE)
    d.pop("data", None)
    return d
