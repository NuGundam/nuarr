r"""nuarr - pull a sidecar subtitle inside the file it belongs to.

WHY, AND WHAT IT COSTS TO LEAVE THEM OUT
----------------------------------------
A sidecar is a subtitle sitting next to the video as its own file. Plex reads
them and plays them, so nothing looks wrong - and three separate things go
wrong quietly:

    IT GETS ORPHANED. subocr's docstring already says it: "a sidecar is one arr
    rename away from being orphaned". An upgrade replaces the mkv, Sonarr
    renames the folder, and the .ass stays behind pointing at nothing. This
    library has 13,004 of them in Anime Shows alone.

    IT WINS THE PICKER. Plex prefers an external subtitle over an embedded one
    for the same language. So a file with a perfectly good embedded track can
    have the sidecar chosen for it.

    AND FOR ASS, IT BURNS. An external ASS has to be injected by the server,
    and injecting it means painting it onto the picture - which re-encodes the
    whole video to carry it. Measured here: hevc -> hevc, audio copied
    untouched, stream bitrate 4.6x the source, for one subtitle.

Embedding is a stream copy. mkvmerge writes a new container with the same
video, the same audio and the subtitle added; nothing is re-encoded and nothing
loses a generation.

THE THREE GUARDS, AND WHY EACH ONE IS THERE
-------------------------------------------
1. THE LANGUAGE RULES DECIDE FIRST. If the library would not keep a Spanish
   subtitle track, it must not gain one just because somebody dropped a
   .es.srt in the folder. The sidecar is put through the same policy the
   planner applies to embedded tracks, so the two cannot disagree.

2. NOT IF THAT LANGUAGE IS ALREADY INSIDE. Per language, not per file: a
   release with Japanese embedded subs still takes an English sidecar, because
   those are not the same subtitle. What this prevents is two English tracks
   where there was one.

3. THE SIDECAR IS RECYCLED, NEVER DELETED, AND ONLY AFTER THE RESULT IS READ
   BACK. mkvmerge exiting 0 is not proof; the new file is probed and the track
   has to be in it, with the right language, before anything is removed. If
   that check fails the original file is untouched and the sidecar is still
   where it was.

WHY IT IS OFF BY DEFAULT
------------------------
It rewrites files and removes others. Every other subtitle rule in this list
changes what a rebuild produces; this one starts a rebuild and reaches outside
the file to do it. That earns an explicit switch rather than a default.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

from . import joblog
from .config import NO_WINDOW, SETTINGS, hidden_si
from .db import cursor

# What counts as a subtitle sitting next to a video.
SIDECAR_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi")
# WHICH ONE WINS WHEN A FOLDER HAS TWO FOR ONE LANGUAGE. Found in the preview
# before this existed: Doom Patrol S01E05 has both an .ass and an .srt in
# English, and taking both would put two English tracks in the file - the exact
# duplication the embedded-language guard was written to prevent, arriving
# through the side door.
#
# SRT wins, and not by accident. The whole reason for this rule is that ASS
# forces a burn on most clients; embedding the ASS instead of the SRT would
# carry that problem inside the file rather than solving it. The ASS is left on
# disk rather than recycled, because it is the styled copy and nothing has
# replaced it.
_PREFER = {".srt": 0, ".vtt": 1, ".smi": 2, ".ass": 3, ".ssa": 4, ".sub": 5}
# Plex reads these from a subfolder too, as of server 1.41.
SUB_DIRS = ("subs", "subtitles")

# WHAT PER_RUN USED TO BE FOR, AND WHY IT IS GONE.
#
# Twelve files a pass, ten minutes apart, was a guess about a bad moment: each
# one is a stream-copy remux of a whole container, and a library of 13,000
# sidecars would otherwise saturate the pool for a day the first time this was
# switched on. It cost the good moments too - 446 files at twelve every ten
# minutes is six hours of an idle box doing nothing for nine minutes in ten -
# and it was never protection anyway, because twelve remuxes started the
# instant somebody presses play are still twelve.
#
# idle.py asks before EVERY file instead, so the throttle is the machine's own
# state rather than a number somebody picked. Busy means pause; free means
# keep going. PER_RUN survives only for the manual "run it now" button, where
# a person has already decided.
PER_RUN = 12
CYCLE_S = 600
# Never touch a file written in the last few minutes; it may still be landing.
SETTLE_S = 600

STATE: dict = {"running": False, "done": 0, "total": 0, "now": "",
               "last_run": 0.0, "embedded": 0, "recycled": 0, "last_error": ""}

_READY = False


def init() -> None:
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subembed_log(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                at       REAL NOT NULL,
                file_id  INTEGER,
                path     TEXT,
                sidecar  TEXT,
                lang     TEXT,
                ok       INTEGER NOT NULL DEFAULT 0,
                detail   TEXT
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_subembed_at "
                    "ON subembed_log(at)")
    _READY = True


def enabled(library: str = "") -> bool:
    """The per-library switch, off unless somebody turned it on."""
    try:
        from . import subocr
        return bool(subocr.sub_rules(library).get("embed_sidecars"))
    except Exception:                                            # noqa: BLE001
        return False


def rules_of(library: str = "", overrides: dict | None = None) -> dict:
    """This library's sidecar rules, with a proposed change laid over them."""
    try:
        from . import subocr
        d = dict(subocr.sub_rules(library))
    except Exception:                                            # noqa: BLE001
        d = {}
    if overrides:
        d.update(overrides)
    return d


def active(library: str = "", overrides: dict | None = None) -> bool:
    """Is there ANY sidecar work to do here?

    Recycling a redundant sidecar rewrites nothing, so it does not need the
    embed rule to be on and must not be gated behind it - that would make a
    switch that says it acts on its own into one that quietly does not.
    """
    d = rules_of(library, overrides)
    return bool(d.get("embed_sidecars")
                or d.get("sidecar_conflict") == "inside")


def _mkvmerge() -> str:
    p = str(getattr(SETTINGS, "mkvmerge", "") or "")
    if p and os.path.exists(p):
        return p
    # Sits beside mkvpropedit, which two other modules already resolve this way.
    near = str(getattr(SETTINGS, "mkvpropedit", "") or "")
    if near:
        guess = os.path.join(os.path.dirname(near), "mkvmerge.exe")
        if os.path.exists(guess):
            return guess
    guess = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
    return guess if os.path.exists(guess) else "mkvmerge"


def have_mkvmerge() -> bool:
    exe = _mkvmerge()
    return bool(exe) and (os.path.exists(exe) or exe == "mkvmerge")


# --------------------------------------------------------- reading the name --
# Plex's own convention, and the only thing a sidecar carries about itself:
#   Show - S01E02 [tags].en.srt          .en.forced.srt      .eng.sdh.ass
# The language is the last token that looks like one; forced/sdh/cc are roles.
_ROLE = {"forced", "sdh", "cc", "hi", "default"}
_LANG_RE = re.compile(r"^[a-z]{2,3}$")


def read_sidecar_name(video: str, side: str) -> dict:
    r"""-> {lang, role, ok, why} for a sidecar beside `video`.

    WHAT IS BETWEEN THE TWO NAMES IS THE METADATA. The sidecar's stem starts
    with the video's stem; whatever follows, split on dots, is the language and
    any role flags. A file whose extra part is not a language code at all -
    "Show - S01E02.track3.srt" - has no language to check against the policy,
    and a subtitle nuarr cannot name the language of is one it must not embed
    with a guess.
    """
    vstem = os.path.splitext(os.path.basename(video))[0]
    sstem = os.path.splitext(os.path.basename(side))[0]
    if not sstem.startswith(vstem):
        return {"lang": "", "role": "", "ok": False,
                "why": "the name does not match this video"}
    rest = sstem[len(vstem):].strip(". ")
    if not rest:
        # "Show - S01E02.srt" beside "Show - S01E02.mkv": no language stated.
        return {"lang": "", "role": "", "ok": False,
                "why": "no language in the filename, so there is nothing to "
                       "check against the language rules"}
    parts = [p.strip().lower() for p in rest.split(".") if p.strip()]
    roles = [p for p in parts if p in _ROLE]
    langs = [p for p in parts if p not in _ROLE and _LANG_RE.match(p)]
    if not langs:
        return {"lang": "", "role": ",".join(roles), "ok": False,
                "why": f"{rest!r} is not a language code"}
    return {"lang": langs[0][:3], "role": ",".join(roles), "ok": True,
            "why": ""}


def _lang_key(code: str) -> str:
    """Two- and three-letter spellings of one language, compared as one."""
    try:
        from . import langkey
        return langkey.key(code)                       # type: ignore[attr-defined]
    except Exception:                                            # noqa: BLE001
        return (code or "")[:2].lower()


def wanted(lang: str, library: str) -> tuple[bool, str]:
    """Would this library keep a subtitle in this language?"""
    from . import langpolicy
    pol = langpolicy.for_library(library, "subs") or {}
    keep = {_lang_key(x) for x in (pol.get("langs") or [])}
    k = _lang_key(lang)
    if k in keep:
        return True, ""
    if pol.get("keep_untagged") and k in ("un", "", "und"):
        return True, ""
    want = ", ".join(sorted(pol.get("langs") or [])) or "nothing"
    return False, (f"{library} keeps {want} subtitles, and this one is "
                   f"{lang or 'untagged'}")


def sidecars_for(video: str) -> list[str]:
    """Every subtitle file that belongs to this video, folder or Subs/."""
    out: list[str] = []
    d = os.path.dirname(video)
    stem = os.path.splitext(os.path.basename(video))[0]
    places = [d] + [os.path.join(d, s) for s in SUB_DIRS]
    for place in places:
        try:
            if not os.path.isdir(place):
                continue
            for f in os.listdir(place):
                if not f.lower().endswith(SIDECAR_EXT):
                    continue
                if not f.startswith(stem):
                    continue
                out.append(os.path.join(place, f))
        except OSError:
            continue
    return sorted(out)


def embedded_langs(file_id: int) -> set:
    """Subtitle languages already inside the file, from the stored probe."""
    got: set = set()
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if not r:
            return got
        for s in (json.loads(r["json"]).get("streams") or []):
            if s.get("codec_type") != "subtitle":
                continue
            got.add(_lang_key((s.get("tags") or {}).get("language") or "und"))
    except Exception:                                            # noqa: BLE001
        pass
    return got



# ------------------------------------------------- same language, and then --
# TWO SUBTITLES IN ONE LANGUAGE ARE NOT NECESSARILY THE SAME SUBTITLE.
#
# A file with "English [Signs]" inside it and a plain .en.srt beside it does
# not have that subtitle twice - it has a signs track and a dialogue track,
# which is the same distinction the drop-covered rule and the forced-flag rule
# already make. Deciding on language alone would either refuse a real addition
# or recycle a sidecar nothing had replaced, depending on which switch was on,
# and both are wrong in the same way.
#
# So everything below matches on language AND kind. Three kinds, because three
# is what a subtitle file's name and a track's header can actually tell apart.
def _role_class(role: str) -> str:
    """forced / sdh / full, from a sidecar's name."""
    r = (role or "").lower()
    if "forced" in r:
        return "forced"
    if "sdh" in r or "cc" in r or "hi" in r:
        return "sdh"
    return "full"


# AND THE FOURTH KIND, WHICH IS NOT A SUBTITLE AT ALL.
#
# The hardsub check writes a blank English track titled "English (burned into
# the picture)" into files whose subtitles are painted on. It carries one empty
# cue. Its whole job is to tell Bazarr and Plex there is nothing to fetch.
#
# The survey caught what that would have done here. On 198 files - Detective
# Conan, mostly - the marker made the file look like it already had English
# subtitles, so the recycle rule would have thrown away a REAL English sidecar
# to preserve a placeholder that contains one blank line. That is the worst
# outcome this feature can produce, and it would have looked like it worked.
#
# So a marker is its own kind and NOTHING may happen to it. It is not a twin,
# so nothing is recycled against it - and it is not a placeholder to be
# upgraded either, because the words it is describing are still painted on the
# picture. A file wearing one is left alone in that language entirely.
def _is_marker(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return False
    try:
        from . import hardsub as _h
        if t == _h.MARK_NAME.strip().lower():
            return True
    except Exception:                                            # noqa: BLE001
        pass
    return "burned into the picture" in t


def _track_class(title: str, forced: bool) -> str:
    """The four kinds an embedded track can be, from its title and flags."""
    if _is_marker(title):
        return "marker"
    try:
        from . import rules as _r
        if _r.SDH_TITLE_RE.search(title or ""):
            return "sdh"
        if forced or _r.is_signs_title(title or ""):
            return "forced"
    except Exception:                                            # noqa: BLE001
        if forced:
            return "forced"
    return "full"


def _markers(tracks: list, lang: str) -> list:
    """Placeholder tracks in this language - never twins, only replaceable."""
    k = _lang_key(lang)
    return [t for t in tracks if t["lang"] == k and t["class"] == "marker"]


def _probe_sub_tracks(file_id: int) -> list:
    """Subtitle tracks from the STORED probe: language, kind, ordinal.

    Cheap on purpose - this runs inside the library walk, and a subprocess per
    file across forty thousand of them is not a walk, it is an outage. The
    authoritative read happens once, at the moment of the rewrite, where being
    wrong costs something.
    """
    out: list = []
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if not r:
            return out
        n = 0
        for st in (json.loads(r["json"]).get("streams") or []):
            if st.get("codec_type") != "subtitle":
                continue
            tags = st.get("tags") or {}
            disp = st.get("disposition") or {}
            out.append({
                "ord": n,
                "lang": _lang_key(tags.get("language") or "und"),
                "title": (tags.get("title") or "").strip(),
                "class": _track_class(tags.get("title") or "",
                                      bool(disp.get("forced"))),
            })
            n += 1
    except Exception:                                            # noqa: BLE001
        return out
    return out


def _live_sub_tracks(path: str) -> list:
    """The same list, read from the FILE - and with mkvmerge's own track ids.

    mkvmerge numbers its inputs its own way, so an ffprobe stream index cannot
    be handed to --subtitle-tracks; asking mkvmerge itself is the only way to
    name a track to mkvmerge. It also means the decision to drop a track is
    taken against the file as it is now rather than against a probe written
    before somebody else edited it.
    """
    out: list = []
    try:
        r = subprocess.run([_mkvmerge(), "-J", path], capture_output=True,
                           text=True, timeout=180, creationflags=NO_WINDOW,
                           startupinfo=hidden_si())
        d = json.loads(r.stdout or "{}")
    except Exception:                                            # noqa: BLE001
        return out
    n = 0
    for t in (d.get("tracks") or []):
        if t.get("type") != "subtitles":
            continue
        p = t.get("properties") or {}
        out.append({
            "id": t.get("id"),
            "ord": n,
            "lang": _lang_key(p.get("language") or "und"),
            "title": (p.get("track_name") or "").strip(),
            "class": _track_class(p.get("track_name") or "",
                                  bool(p.get("forced_track"))),
        })
        n += 1
    return out


def _same_kind(tracks: list, lang: str, role: str) -> list:
    """The tracks a sidecar of this language and kind would be a second copy of."""
    k, cls = _lang_key(lang), _role_class(role)
    return [t for t in tracks if t["lang"] == k and t["class"] == cls]


# ------------------------------------------------------------- the verdict --
def plan_one(file_id: int, force: bool = False,
             overrides: dict | None = None) -> dict:
    r"""What would happen to this file. Reads only; changes nothing.

    Returns {ok, path, library, take: [...], skip: [{sidecar, why}]}. The
    refusals are carried, not dropped - a panel that lists what it will do and
    silently omits what it will not is a panel that cannot be checked.
    """
    with cursor() as cur:
        row = cur.execute("SELECT id, path, library, state FROM files "
                          "WHERE id=?", (int(file_id),)).fetchone()
    if not row:
        return {"ok": False, "why": "no such file"}
    row = dict(row)
    path, lib = row["path"] or "", row["library"] or ""
    out = {"ok": True, "file_id": int(file_id), "path": path, "library": lib,
           "take": [], "skip": []}
    if (row.get("state") or "") in ("deleted", "duplicate"):
        return {**out, "ok": False, "why": "not a live file"}
    _sr = rules_of(lib, overrides)
    if not active(lib, overrides) and not force:
        return {**out, "ok": False,
                "why": f"the sidecar rules are off for {lib or 'this library'}"}
    have = embedded_langs(int(file_id))
    # The two answers to "that language is already inside", both off by
    # default and both the library's to give.
    take_them = bool(_sr.get("embed_sidecars")) or force
    # leave / inside / sidecar - what happens when the sidecar is a genuine
    # second copy of a track that is already there.
    conflict = str(_sr.get("sidecar_conflict") or "leave")
    # "KEEP THE LOOSE COPY" IS A WAY OF TAKING IT IN, so with the rule above
    # off it is not a third answer - it is the first one wearing a different
    # label. Said here rather than left to be inferred from an empty result:
    # the library refuses the sidecar either way, and the reason a person is
    # given should be the real one.
    if conflict == "sidecar" and not take_them:
        conflict = "leave"
        _sr = dict(_sr, sidecar_conflict="leave", _demoted=True)
    beats = (conflict == "sidecar") and take_them
    tidy = (conflict == "inside")
    # ALWAYS READ, NOT ONLY WHEN THERE IS A CONFLICT RULE. Telling a duplicate
    # from an addition needs the tracks whatever the answer is going to be: a
    # .en.sdh.srt beside a full English track is not a second copy of it, and
    # refusing it on the language alone - which is what the old guard did - is
    # the case this list exists to stop being wrong about.
    inside = _probe_sub_tracks(int(file_id)) if take_them or tidy else []
    out["drop"] = []
    for side in sidecars_for(path):
        name = read_sidecar_name(path, side)
        if not name["ok"]:
            out["skip"].append({"sidecar": side, "why": name["why"]})
            continue
        ok, why = wanted(name["lang"], lib)
        if not ok:
            out["skip"].append({"sidecar": side, "why": why})
            continue
        # GUARD TWO, PER LANGUAGE. A file with Japanese subs inside still takes
        # an English sidecar; what this stops is a second English track.
        if _lang_key(name["lang"]) in have:
            twins = _same_kind(inside, name["lang"], name["role"])
            cls = _role_class(name["role"])
            # THE WORDS ARE ALREADY ON THE SCREEN.
            #
            # A marker track means this file's subtitles are PAINTED INTO THE
            # PICTURE. An earlier version of this treated the marker as a
            # placeholder worth replacing with a real subtitle, which is
            # exactly wrong twice over: the file would play the sidecar on top
            # of the burned-in words - two sets of English at once - and
            # dropping the marker would tell Bazarr and Plex there is nothing
            # burned in, so the next pass would fetch another one.
            #
            # The marker stops being true only when the words come out of the
            # picture, and taking them out means inpainting the video frame by
            # frame and re-encoding it. Nothing here does that. Until
            # something does, a sidecar in the burned-in language is left
            # exactly where it is - not embedded, not recycled.
            if _markers(inside, name["lang"]):
                out["skip"].append({
                    "sidecar": side,
                    "why": f"this file's {name['lang']} subtitles are burned "
                           f"into the picture, so a second copy inside it "
                           f"would play on top of them - and nothing can take "
                           f"them out of the picture without re-encoding it"})
                continue
            # NOT A DUPLICATE AT ALL, so no answer is needed. The language is
            # inside; a subtitle of this KIND is not. Full dialogue against
            # SDH, or against a signs track - different subtitles, and the
            # conflict setting has nothing to say about them.
            if not twins and not beats:
                if not take_them:
                    out["skip"].append({
                        "sidecar": side, "why": _off_why(_sr)})
                    continue
                out["take"].append({
                    "sidecar": side, "lang": name["lang"],
                    "role": name["role"], "size": _size(side),
                    "replaces": [],
                    "note": f"the file has {name['lang']} subtitles but none "
                            f"of them are {cls}"})
                continue
            if beats:
                # NOTHING OF THIS KIND IS ACTUALLY INSIDE. The language is,
                # but as a different sort of subtitle - "English [Signs]"
                # against a plain .en.srt. That is not a duplicate, it is the
                # gap the sidecar fills, so it is taken and nothing is
                # dropped. Only reachable with this switch on, because it
                # widens what the file is allowed to gain.
                if not twins:
                    out["take"].append({
                        "sidecar": side, "lang": name["lang"],
                        "role": name["role"], "size": _size(side),
                        "replaces": [],
                        "note": f"the file has {name['lang']} subtitles but "
                                f"none of them are {cls}"})
                    continue
                if len(twins) == 1:
                    out["take"].append({
                        "sidecar": side, "lang": name["lang"],
                        "role": name["role"], "size": _size(side),
                        "replaces": [twins[0]["ord"]],
                        "note": f"replaces the {cls} {name['lang']} track "
                                f"already inside"
                                + (f" ({twins[0]['title']})"
                                   if twins[0]["title"] else "")})
                    continue
                # AMBIGUOUS IS NOT A REASON TO PICK ONE. Two full English
                # tracks and one English sidecar: whichever is thrown away is
                # a guess, and a guess here is a subtitle nobody can get back.
                out["skip"].append({
                    "sidecar": side,
                    "why": f"{len(twins)} {cls} {name['lang']} tracks are "
                           f"already inside - which one this would replace "
                           f"is not obvious, so nothing was changed"})
                continue
            if tidy and twins:
                out["drop"].append({
                    "sidecar": side, "lang": name["lang"],
                    "role": name["role"], "size": _size(side),
                    "why": f"the file already carries a {cls} "
                           f"{name['lang']} subtitle track"})
                continue
            out["skip"].append({
                "sidecar": side,
                "why": f"the file already has a {name['lang']} subtitle track "
                       f"inside it"})
            continue
        if not take_them:
            out["skip"].append({"sidecar": side, "why": _off_why(_sr)})
            continue
        out["take"].append({"sidecar": side, "lang": name["lang"],
                            "role": name["role"],
                            "size": _size(side)})

    # ONE PER LANGUAGE AND ROLE, best format first. Role is part of the key
    # because a forced track and a full one are different subtitles, the same
    # way the drop-covered rule already treats SDH and dialogue as different.
    best: dict = {}
    for t in out["take"]:
        k = (_lang_key(t["lang"]), t.get("role") or "")
        rank = _PREFER.get(os.path.splitext(t["sidecar"])[1].lower(), 9)
        cur = best.get(k)
        if cur is None or rank < cur[0]:
            if cur is not None:
                out["skip"].append({
                    "sidecar": cur[1]["sidecar"],
                    "why": f"another {t['lang']} sidecar in a better format is "
                           f"being taken instead "
                           f"({os.path.splitext(t['sidecar'])[1]})"})
            best[k] = (rank, t)
        else:
            out["skip"].append({
                "sidecar": t["sidecar"],
                "why": f"another {t['lang']} sidecar in a better format is "
                       f"being taken instead "
                       f"({os.path.splitext(cur[1]['sidecar'])[1]})"})
    out["take"] = [t for _r, t in best.values()]
    return out


def _off_why(sr: dict) -> str:
    """Why a sidecar was refused when this library does not take them in."""
    if sr.get("_demoted"):
        return ("this library is set to keep the loose copy, but the rule "
                "that takes subtitle files inside is off - so nothing can be "
                "taken in and the sidecar is left where it is")
    return ("this library only recycles sidecars it already has; taking new "
            "ones in is a separate rule")


def _recycle_drops(file_id: int, path: str, drops: list) -> dict:
    r"""Recycle sidecars the file already carries. Nothing is rewritten.

    READ THE FILE, NOT THE RECORD. The plan decided from the stored probe,
    which is fast and is the right trade for a walk of forty thousand files -
    but it is also written before an edit rather than after, and a stale probe
    is how a subtitle would get thrown away because nuarr THOUGHT the track
    was inside. So every drop is checked against the container one more time,
    now, and a file that has changed its mind keeps its sidecar.
    """
    from . import fileops
    out = {"dropped": 0, "kept": []}
    if not drops:
        return out
    live = _live_sub_tracks(path)
    if not live:
        out["kept"] = [os.path.basename(d["sidecar"]) for d in drops]
        return out
    for d in drops:
        if not _same_kind(live, d["lang"], d["role"]):
            out["kept"].append(os.path.basename(d["sidecar"]))
            _note(file_id, path, d["sidecar"], d["lang"], False,
                  "the track it duplicates is not in the file after all - "
                  "the sidecar was left alone")
            continue
        rr = fileops.recycle(d["sidecar"])
        if getattr(rr, "ok", False):
            out["dropped"] += 1
            _note(file_id, path, d["sidecar"], d["lang"], True,
                  d.get("why") or "already inside the file - recycled")
        else:
            out["kept"].append(os.path.basename(d["sidecar"]))
            _note(file_id, path, d["sidecar"], d["lang"], False,
                  f"could not recycle it: {getattr(rr, 'why', '')}"[:200])
    return out


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# ------------------------------------------------------------- doing it -----
class _Ran:
    """What subprocess.run would have given back, for the streaming version."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.returncode, self.stdout, self.stderr = code, out, err


_PROG_RE = re.compile(r"(?:#GUI#progress\s+|Progress:\s*)(\d{1,3})\s*%")


def _run_reporting(cmd: list, report=None, timeout: int = 3600) -> _Ran:
    r"""Run mkvmerge, calling `report(pct)` as it announces its progress.

    STREAMED, NOT CAPTURED. subprocess.run() hands back everything at the end,
    which is exactly too late to draw a bar with. The output is small and
    line-based, so reading it as it arrives costs nothing and turns a minute
    of apparent silence into a moving number.
    """
    if report is None:
        return _Ran(*_plain(cmd, timeout))
    import subprocess as _sp
    p = _sp.Popen([cmd[0], "--gui-mode"] + list(cmd[1:]),
                  stdout=_sp.PIPE, stderr=_sp.PIPE, text=True,
                  encoding="utf-8", errors="replace", bufsize=1,
                  creationflags=NO_WINDOW, startupinfo=hidden_si())
    out: list = []
    t0 = time.time()
    try:
        for line in p.stdout:                     # type: ignore[union-attr]
            out.append(line)
            m = _PROG_RE.search(line)
            if m:
                try:
                    report(float(m.group(1)))
                except Exception:                                # noqa: BLE001
                    pass
            if time.time() - t0 > timeout:
                p.kill()
                break
    finally:
        try:
            err = p.stderr.read() if p.stderr else ""            # type: ignore
        except Exception:                                        # noqa: BLE001
            err = ""
        p.wait()
    return _Ran(p.returncode, "".join(out), err)


def _plain(cmd: list, timeout: int) -> tuple:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       creationflags=NO_WINDOW, startupinfo=hidden_si())
    return r.returncode, r.stdout or "", r.stderr or ""


def _probe_langs(path: str) -> set:
    """Read the subtitle languages out of a file on disk, now."""
    from .jobs import _ffprobe_exe
    try:
        out = subprocess.run(
            [_ffprobe_exe(), "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "s", path],
            capture_output=True, text=True, timeout=120,
            creationflags=NO_WINDOW, startupinfo=hidden_si()).stdout
        got = set()
        for s in (json.loads(out or "{}").get("streams") or []):
            got.add(_lang_key((s.get("tags") or {}).get("language") or "und"))
        return got
    except Exception:                                            # noqa: BLE001
        return set()


def _note(file_id: int, path: str, side: str, lang: str, ok: bool,
          detail: str) -> None:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subembed_log(at,file_id,path,sidecar,lang,ok,"
                "detail) VALUES(?,?,?,?,?,?,?)",
                (time.time(), int(file_id or 0), path, side, lang,
                 1 if ok else 0, (detail or "")[:500]))
    except Exception:                                            # noqa: BLE001
        pass


def embed_one(file_id: int, report=None) -> dict:
    r"""Remux this file's eligible sidecars into it, then recycle them.

    ONE mkvmerge FOR ALL OF THEM. Two sidecars means two subtitle tracks and
    one rewrite, not two rewrites - a second pass would copy a file that has
    just been copied, for no reason.
    """
    from . import fileops
    p = plan_one(int(file_id))
    if not p.get("ok"):
        return {**p, "embedded": 0}
    takes, drops = (p.get("take") or []), (p.get("drop") or [])
    if not takes and not drops:
        return {**p, "embedded": 0}
    path = p["path"]
    if not os.path.exists(path):
        return {"ok": False, "why": "the file is not on disk"}
    # A DROP TOUCHES NO FILE, so it is not gated on any of what follows - not
    # the container, not mkvmerge, not the lock. It only ever removes a loose
    # copy of something the file already has.
    if not takes:
        d = _recycle_drops(int(file_id), path, drops)
        if d["dropped"]:
            joblog.log(f"recycled {d['dropped']} sidecar(s) already inside "
                       f"{os.path.basename(path)}", "info")
        return {"ok": True, "embedded": 0, "recycled": d["dropped"],
                "dropped": d["dropped"], "kept": d["kept"], "path": path}
    if os.path.splitext(path)[1].lower() != ".mkv":
        return {"ok": False, "why": "only Matroska can carry these tracks; "
                                    "this file is not .mkv"}
    if fileops.is_locked(path):
        return {"ok": False, "why": "the file is in use"}
    if not have_mkvmerge():
        return {"ok": False, "why": "mkvmerge is not installed - see "
                                    "Settings, MKVToolNix"}
    # ON THE CACHE, NOT BESIDE THE SOURCE. See fileops.cache_temp.
    ok_room, why_room = fileops.cache_room(_size(path))
    if not ok_room:
        return {"ok": False, "why": why_room}
    tmp = fileops.cache_temp(".mkv", "embed")
    cmd = [_mkvmerge(), "-o", tmp]
    # WHAT THE SIDECAR REPLACES, NAMED IN MKVMERGE'S OWN NUMBERS. The plan
    # counted subtitle tracks in order; mkvmerge numbers every track in the
    # file in its own way, so the ordinal has to be translated against the
    # container as it is right now. If the file has changed since the plan was
    # made, this comes out different and the replace is abandoned rather than
    # aimed at whatever happens to be in that position.
    want_drop = [o for t in takes for o in (t.get("replaces") or [])]
    if want_drop:
        live = _live_sub_tracks(path)
        by_ord = {t["ord"]: t for t in live}
        ids, lost = [], []
        for t in takes:
            for o in (t.get("replaces") or []):
                hit = by_ord.get(o)
                want_cls = _role_class(t.get("role") or "")
                if hit is None or hit["lang"] != _lang_key(t["lang"]) \
                        or hit["class"] != want_cls:
                    lost.append(t["lang"])
                else:
                    ids.append(str(hit["id"]))
        if lost or len(ids) != len(want_drop):
            why = ("the track this would have replaced is not where the plan "
                   "said it was - the file has changed since it was looked at, "
                   "so nothing was done")
            _note(file_id, path, ";".join(t["sidecar"] for t in takes), "",
                  False, why)
            return {"ok": False, "why": why}
        # !ids means "everything except these", which is how mkvmerge spells
        # a removal: the container is copied without them.
        cmd += ["--subtitle-tracks", "!" + ",".join(ids)]
    cmd += [path]
    for t in takes:
        # The language goes on the TRACK, not just in the filename it came
        # from - a track tagged und is a track the planner will treat as
        # untagged forever after.
        cmd += ["--language", f"0:{t['lang']}"]
        if "forced" in (t.get("role") or ""):
            cmd += ["--forced-track", "0:yes"]
        cmd += [t["sidecar"]]
    # READ ITS PROGRESS AS IT GOES, rather than waiting sixty seconds for an
    # exit code. mkvmerge prints "Progress: 37%" as it copies; with --gui-mode
    # it prints "#GUI#progress 37%" on a line of its own, which is the same
    # number without the carriage-return games. Nothing about the result
    # changes - the exit code and the read-back below still decide - this only
    # gives the panel something true to draw while it waits.
    try:
        r = _run_reporting(cmd, report)
    except Exception as e:                                       # noqa: BLE001
        fileops._quiet_remove(tmp)
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    # mkvmerge returns 1 for warnings and still writes a good file; 2 is a
    # real failure. Treated the way MKVToolNix documents it rather than as a
    # plain non-zero check, which would throw away every file with a warning.
    if r.returncode >= 2 or not os.path.exists(tmp):
        fileops._quiet_remove(tmp)
        why = (r.stderr or r.stdout or "mkvmerge failed").strip()[:300]
        _note(file_id, path, ";".join(t["sidecar"] for t in takes), "",
              False, why)
        return {"ok": False, "why": why}

    # EXIT 0 IS NOT PROOF. Read the result back and require every language to
    # actually be in it before anything is removed or replaced.
    got = _probe_langs(tmp)
    missing = [t["lang"] for t in takes
               if _lang_key(t["lang"]) not in got]
    if missing:
        fileops._quiet_remove(tmp)
        why = (f"the rebuilt file does not contain "
               f"{', '.join(missing)} - nothing was changed")
        _note(file_id, path, "", ",".join(missing), False, why)
        return {"ok": False, "why": why}

    res = fileops.safe_replace(path, tmp)
    if not getattr(res, "ok", False):
        fileops._quiet_remove(tmp)
        why = f"could not put the rebuilt file in place: {getattr(res, 'why', '')}"
        _note(file_id, path, "", "", False, why)
        return {"ok": False, "why": why}

    # AND ONLY NOW THE SIDECARS, one at a time, recycled rather than deleted.
    gone, kept = 0, []
    for t in takes:
        rr = fileops.recycle(t["sidecar"])
        if getattr(rr, "ok", False):
            gone += 1
        else:
            kept.append(os.path.basename(t["sidecar"]))
        _note(file_id, path, t["sidecar"], t["lang"], True,
              f"embedded as {t['lang']}"
              + ("" if getattr(rr, "ok", False) else " (sidecar left in place)"))
    # The redundant ones go in the same visit - the file is already settled
    # and re-probing it is cheaper than coming back for them.
    dd = _recycle_drops(int(file_id), path, drops)
    gone += dd["dropped"]
    kept += dd["kept"]
    replaced = sum(len(t.get("replaces") or []) for t in takes)
    joblog.log(f"embedded {len(takes)} sidecar subtitle(s) into "
               f"{os.path.basename(path)}"
               + (f", replacing {replaced} track(s) already inside"
                  if replaced else "")
               + f" and recycled {gone}", "info")
    return {"ok": True, "embedded": len(takes), "recycled": gone,
            "replaced": replaced, "dropped": dd["dropped"],
            "kept": kept, "path": path}


# ------------------------------------------------------------- the sweep ----
def candidates(limit: int = 200, force: bool = False,
               on_progress=None) -> list[dict]:
    """Files with a sidecar worth taking.

    `force` widens it to every library rather than the enabled ones, which is
    what the preview needs: the question "what would this do" has to be
    answerable before the switch is thrown, not after.
    """
    if not _READY:
        init()
    libs = [l.name for l in (SETTINGS.libraries or [])
            if force or active(l.name)]
    if not libs:
        return []
    qs = ",".join("?" * len(libs))
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            f"SELECT id, path, library, pool_disk FROM files "
            f" WHERE library IN ({qs}) "
            f"   AND state NOT IN ('deleted','duplicate') "
            f"   AND COALESCE(path,'') != '' "
            # NO CAP. This said LIMIT 20000 back when that was the whole
            # library; it is 39,753 now, so the walk stopped at id 25,979 and
            # everything imported after that was invisible - not "not yet
            # done", INVISIBLE, because the sweep reads the same list. The
            # newest half of the library would never have had a sidecar taken.
            # The caller's own limit bounds the RESULT; this bounded the
            # search, which is not the same thing and was never meant to.
            f"   AND COALESCE(mtime,0) < ? "
            f" ORDER BY id", libs + [cutoff])]
    out = []
    total = len(rows)
    for i, r in enumerate(rows, 1):
        if len(out) >= limit:
            break
        # WHERE THE MINUTE GOES, REPORTED WHILE IT GOES. One listdir per file
        # across twenty thousand of them is a minute nobody can be asked to
        # stare at a spinner through - and the loop knows exactly how far it
        # is, so there is no reason to guess.
        if on_progress is not None and (i % 25 == 0 or i == total):
            try:
                on_progress(i, total, len(out))
            except Exception:                                # noqa: BLE001
                pass
        if not sidecars_for(r["path"]):
            continue                       # cheap listdir, no probe, no policy
        p = plan_one(r["id"], force=force)
        if p.get("take") or p.get("drop"):
            # WHICH SPINDLE IT LIVES ON, carried with the plan. The runner
            # asks the gate per disk before every file - a viewer on one is a
            # reason to work on another, not a reason to stop - and plan_one
            # has no reason to know about disks.
            p["pool_disk"] = r.get("pool_disk") or ""
            out.append(p)
    return out


async def sweep(limit: int = 0) -> dict:
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    todo = await asyncio.to_thread(candidates, int(limit or PER_RUN))
    STATE.update(running=True, done=0, total=len(todo), now="",
                 embedded=0, recycled=0)
    done = emb = rec = 0
    try:
        for p in todo:
            STATE["now"] = os.path.basename(p["path"])[:60]
            STATE["done"] = done
            out = await asyncio.to_thread(embed_one, p["file_id"])
            done += 1
            if out.get("ok"):
                emb += int(out.get("embedded") or 0)
                rec += int(out.get("recycled") or 0)
                STATE.update(embedded=emb, recycled=rec)
    finally:
        STATE.update(running=False, now="", last_run=time.time(),
                     done=done, embedded=emb, recycled=rec)
    if emb:
        joblog.log(f"sidecar embed: {emb} subtitle(s) taken into "
                   f"{done} file(s), {rec} sidecar(s) recycled", "info")
    return {"ok": True, "files": done, "embedded": emb, "recycled": rec}


def stats() -> dict:
    if not _READY:
        init()
    libs = {l.name: enabled(l.name) for l in (SETTINGS.libraries or [])}
    out = {"libraries": libs, "on": any(libs.values()),
           "have_mkvmerge": have_mkvmerge(), "running": STATE["running"],
           "now": STATE["now"], "done": STATE["done"], "total": STATE["total"],
           "last_run": STATE["last_run"], "embedded": 0, "recycled": 0,
           "failed": 0}
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT SUM(ok) e, SUM(1-ok) f, COUNT(*) n "
                "FROM subembed_log").fetchone()
            out["embedded"] = int((r["e"] if r else 0) or 0)
            out["failed"] = int((r["f"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        pass
    return out


# THE WHOLE LIBRARY, COUNTED ONCE AND REMEMBERED.
#
# A panel showing twelve of something and no total is a panel that cannot be
# acted on: twelve out of what? The full walk is one listdir per file across
# 20,000 rows - seconds, not minutes, because the folders are already in the
# OS cache after the first pass - but it is far too slow to do on every poll of
# a settings page. So it is computed on demand and kept.
_SUM: dict = {"at": 0.0, "data": None}
_SUM_TTL = 600.0
# ONE WALK, TWO READERS. The count was cached and the ROWS were not, so opening
# the panel re-walked twenty thousand folders on every poll - sixty seconds a
# time, which is why the skeleton never cleared. Both come off the same cached
# list now; the walk happens once and everything else is a slice of it.
_WALK: dict = {"at": 0.0, "rows": None, "took": 0.0, "running": False,
               "done": 0, "total": 0, "found": 0, "t0": 0.0}
_WALK_LOCK = None


def _walk_now(force: bool) -> None:
    """The walk itself, on a thread, updating _WALK as it goes."""
    t0 = time.time()
    _WALK.update(running=True, done=0, total=0, found=0, t0=t0)

    def tick(done, total, found):
        _WALK.update(done=done, total=total, found=found)
    try:
        rows = candidates(limit=100000, force=True, on_progress=tick)
        _WALK.update(at=time.time(), rows=rows,
                     took=round(time.time() - t0, 1), found=len(rows))
    except Exception:                                            # noqa: BLE001
        pass
    finally:
        _WALK["running"] = False


def walk(force_refresh: bool = False) -> list:
    r"""Every file with a sidecar worth taking.

    NEVER BLOCKS THE REQUEST. The first walk is a minute of listdir and the
    panel that wants it polls every couple of seconds; holding the connection
    open for that is how the skeleton ends up looking like a hang. So the walk
    happens on a thread, the last good answer is served in the meantime, and
    walk_state() carries how far it has got so the panel can draw it.
    """
    global _WALK_LOCK
    now = time.time()
    fresh = (_WALK["rows"] is not None and now - _WALK["at"] < _SUM_TTL)
    if fresh and not force_refresh:
        return _WALK["rows"]
    if not _WALK["running"]:
        import threading
        if _WALK_LOCK is None:
            _WALK_LOCK = threading.Lock()
        with _WALK_LOCK:
            if not _WALK["running"]:
                _WALK["running"] = True     # claimed before the thread starts
                threading.Thread(target=_walk_now, args=(force_refresh,),
                                 daemon=True).start()
    return _WALK["rows"] or []


def walk_state() -> dict:
    """How far the walk has got, and how long the rest of it will take."""
    d = dict(_WALK)
    d.pop("rows", None)
    el = (time.time() - _WALK["t0"]) if (_WALK["running"] and _WALK["t0"]) else 0
    rate = (_WALK["done"] / el) if (el > 0.5 and _WALK["done"]) else 0.0
    d["elapsed"] = round(el, 1)
    d["rate"] = round(rate, 1)
    d["eta"] = round((_WALK["total"] - _WALK["done"]) / rate) if rate else 0
    d["have_rows"] = _WALK["rows"] is not None
    return d


def summary(force_refresh: bool = False) -> dict:
    """How many files across the library have a sidecar worth taking."""
    now = time.time()
    if (not force_refresh and _SUM["data"] is not None
            and now - _SUM["at"] < _SUM_TTL):
        return dict(_SUM["data"])
    got = walk(force_refresh)
    if not got and _WALK["running"]:
        # Nothing to count yet. Say so rather than reporting a zero that reads
        # as "no sidecars anywhere".
        d = dict(_SUM["data"] or {"files": 0, "subs": 0, "by_library": {}})
        d["pending"] = True
        return d
    by_lib: dict = {}
    files = subs = drops = reps = 0
    for p in got:
        lib = p.get("library") or "?"
        e = by_lib.setdefault(lib, {"files": 0, "subs": 0, "drops": 0,
                                    "replaces": 0, "on": enabled(lib)})
        e["files"] += 1
        e["subs"] += len(p.get("take") or [])
        e["drops"] += len(p.get("drop") or [])
        e["replaces"] += sum(len(t.get("replaces") or [])
                             for t in (p.get("take") or []))
        files += 1
        subs += len(p.get("take") or [])
        drops += len(p.get("drop") or [])
        reps += sum(len(t.get("replaces") or []) for t in (p.get("take") or []))
    d = {"files": files, "subs": subs, "drops": drops, "replaces": reps,
         "by_library": by_lib, "at": now, "took": _WALK.get("took") or 0.0}
    _SUM.update(at=now, data=d)
    return dict(d)


def failures(limit: int = 100) -> list[dict]:
    r"""What could not be taken in, and why - from the log, not from memory.

    THE PANEL SAID "2 failed" AND OFFERED NOWHERE TO LOOK. A count with no
    list is a count you can only worry about: it cannot be judged, retried or
    dismissed. Every attempt has always been written to subembed_log with its
    reason; nothing read them back.

    ONE ROW PER FILE, the most recent attempt. A file that failed twice and
    then succeeded is not a failure, and the log holds all three.
    """
    if not _READY:
        init()
    out: list[dict] = []
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT l.file_id, l.path, l.sidecar, l.lang, l.detail, "
                    "       l.at, f.title, f.season, f.episode, f.library "
                    "  FROM subembed_log l "
                    "  LEFT JOIN files f ON f.id = l.file_id "
                    " WHERE l.id IN (SELECT MAX(id) FROM subembed_log "
                    "                 GROUP BY file_id) "
                    "   AND l.ok = 0 "
                    " ORDER BY l.at DESC LIMIT ?", (int(limit),)):
                d = dict(r)
                try:
                    from .db import display_label
                    d["label"] = (display_label(d.get("title"),
                                                d.get("season"),
                                                d.get("episode"))
                                  or os.path.basename(d.get("path") or ""))
                except Exception:                                # noqa: BLE001
                    d["label"] = os.path.basename(d.get("path") or "")
                d["sidecar_name"] = os.path.basename(d.get("sidecar") or "")
                out.append(d)
    except Exception:                                            # noqa: BLE001
        return out
    return out


def retry(file_ids) -> dict:
    r"""Put failures back in the queue by forgetting they failed.

    Nothing is re-run here. The sweep picks its work from what is on disk, so
    a file only stays out of it because the log says its last attempt failed -
    clearing that row is the whole of "try again", and it happens on the
    runner's own schedule under the gate rather than right now on a request.
    """
    ids = [int(i) for i in (file_ids or []) if i]
    if not ids:
        return {"ok": False, "why": "nothing chosen"}
    try:
        if not _READY:
            init()
        with cursor() as cur:
            qs = ",".join("?" * len(ids))
            cur.execute(f"DELETE FROM subembed_log "
                        f" WHERE file_id IN ({qs}) AND ok = 0", ids)
            n = cur.rowcount or 0
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    return {"ok": True, "cleared": n,
            "why": f"{len(ids)} file(s) will be tried again on the next pass"}


def preview_counts(library: str, overrides: dict) -> dict:
    r"""What these rules WOULD do to one library. Plans, changes nothing.

    THE OTHER PREVIEW CANNOT ANSWER THIS. Ticking a subtitle rule shows what
    the planner would decide differently, by planning every file both ways -
    and the planner has never heard of sidecars, so all three sidecar rules
    came back "nothing changes, safe to apply" while in fact hundreds of files
    were about to be rewritten. A confirmation that is confidently wrong is
    worse than none at all.
    """
    return preview_pair(library, overrides, None)["after"]


def preview_pair(library: str, after: dict, before: dict | None) -> dict:
    r"""Both sides of a proposed change, in ONE walk of the library.

    The listdir is what this costs - the planning either side of it is
    arithmetic on what the listdir already found. Doing the two states as two
    calls walked Anime Shows twice for no reason, which on 22,905 files is the
    difference between a pause and a page that looks broken.
    """
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT id, path FROM files "
            " WHERE library=? AND state NOT IN ('deleted','duplicate') "
            "   AND COALESCE(path,'') != '' ORDER BY id", (library,))]
    tot = {"after": {"files": 0, "subs": 0, "drops": 0, "replaces": 0},
           "before": {"files": 0, "subs": 0, "drops": 0, "replaces": 0}}
    listed: list = []
    for r in rows:
        if not sidecars_for(r["path"]):
            continue
        for side, ov in (("after", after), ("before", before)):
            if ov is None:
                continue
            p = plan_one(r["id"], overrides=ov)
            if not p.get("ok"):
                continue
            t, d = (p.get("take") or []), (p.get("drop") or [])
            if not t and not d:
                continue
            rp = sum(len(x.get("replaces") or []) for x in t)
            e = tot[side]
            e["files"] += 1
            e["subs"] += len(t)
            e["drops"] += len(d)
            e["replaces"] += rp
            if side == "after" and len(listed) < 200:
                bits = []
                if len(t) - rp:
                    bits.append(f"takes in {len(t) - rp} subtitle(s)")
                if rp:
                    bits.append(f"replaces {rp} track(s) already inside")
                if d:
                    bits.append(f"recycles {len(d)} loose copy(s)")
                listed.append({"id": r["id"],
                               "label": os.path.basename(r["path"] or ""),
                               "change": ", ".join(bits)})
    tot["after"]["listed"] = listed
    tot["before"]["listed"] = []
    return tot


def preview(limit: int = 25, force: bool = True) -> dict:
    """What this would do, without doing any of it. Works while the rule is off.

    A SLICE OF THE CACHED WALK, not a walk of its own. `force` is kept for the
    signature's sake and is what the walk already does - there is no cheaper
    partial version, because deciding whether a file qualifies means listing
    its folder either way.
    """
    _ = force
    got = walk()[:max(0, int(limit))]
    return {"ok": True, "files": [
        {"file_id": p["file_id"], "path": p["path"], "library": p["library"],
         "on": enabled(p["library"]),
         "take": p["take"], "drop": p.get("drop") or [],
         "skip": p["skip"][:4]} for p in got]}


KEY = "subembed"
TITLE = "Subtitle files beside the video"


def _pending() -> list:
    """Everything waiting, not a slice of it. The runner does the pacing."""
    if not any(active(l.name) for l in (SETTINGS.libraries or [])):
        return []
    try:
        return candidates(limit=100000)
    except Exception:                                            # noqa: BLE001
        return []


def _do_one(p: dict, report=None) -> dict:
    return embed_one(int(p["file_id"]), report=report)


async def watch() -> None:
    r"""Work through the sidecars for as long as the box can spare it.

    No batch and no cycle of its own: idle.run() asks before every file and
    pauses the moment anything more important wants the machine.
    """
    from . import idle
    await asyncio.sleep(180)
    await idle.run(KEY, TITLE, _pending, _do_one,
                   label=lambda p: os.path.basename(p.get("path") or "")[:70],
                   disk_of=lambda p: p.get("pool_disk") or "",
                   system_name="sidecar subtitles")
