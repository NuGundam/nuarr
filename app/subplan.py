r"""nuarr - what should happen to this file's subtitles, and why.

ONE INSTRUCTION PER FILE. The facts come from subscan - what is inside, what
is beside it, what is painted into the picture - and the rules come from the
Subtitle rules panel. This turns the two into an ordered list of steps, and
nothing else. It opens no file, runs no subprocess and writes to no table
except the memory of what you have taught it.

THAT PURITY IS THE WHOLE POINT, and it is what makes "change a rule and the
queue changes" possible at all. The three sweeps this replaces each re-applied
their rules inside the same pass that gathered their evidence, so a rule
change meant walking forty thousand folders again to find out what it meant.
Here the evidence is already in a table, planning a file is arithmetic on two
dicts, and re-planning the entire library after you move a switch takes a
second and touches no disk.

THE STEPS, IN THE ORDER THEY HAVE TO HAPPEN

    take     a subtitle file beside the video goes inside it
    dropdup  a track that is a second copy of another goes
    dropempty a track with nothing in it goes
    retitle  a track's title is corrected to what it actually carries
    mark     a blank marker says the words are painted into the picture
    recycle  a loose copy of something the file already has is binned

take, dropdup and dropempty are all ONE mkvmerge pass - that is the merge of
the sidecar and duplicate systems, and it is not cosmetic: it was two full
container copies of the same file, one after the other, for work that fits in
one. retitle is mkvpropedit and rewrites nothing. recycle touches no video at
all. So a file's whole instruction is at most one rewrite.

WHAT IT REFUSES TO GUESS. Three things are genuinely ambiguous and this says
so instead of picking:

    a sidecar that would replace one of SEVERAL identical tracks - which one?
    two tracks of the same language and kind with the same number of lines
    a picture or title reading that falls between the two sureness lines

Those become questions rather than steps, and an answer to one is remembered
against the show and against the release group, so the next episode does not
ask. That memory is consulted BEFORE the question is raised, which is what
makes the ask-me list shrink as you use it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from .db import cursor

# The order steps must run in. A drop that happens before its replacement is
# taken in leaves the file without that language for the length of a rewrite;
# a retitle before a remux is a title written onto a track that is about to be
# copied into a new container.
ORDER = {"take": 0, "dropdup": 0, "dropempty": 0, "retitle": 1, "mark": 2,
         "recycle": 3}

# Which sidecar format wins when a release ships the same subtitle twice.
PREFER = {".ass": 0, ".ssa": 1, ".srt": 2, ".sub": 3, ".vtt": 4}

_MEM_READY = False


# ------------------------------------------------------------- the memory --
def _mem_init() -> None:
    global _MEM_READY
    if _MEM_READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sub_memory(
                scope    TEXT NOT NULL,     -- show | group
                skey     TEXT NOT NULL,
                question TEXT NOT NULL,
                answer   TEXT NOT NULL,
                at       REAL NOT NULL DEFAULT 0,
                used     INTEGER NOT NULL DEFAULT 0,
                note     TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(scope, skey, question))""")
    _MEM_READY = True


def show_key(file_id: int) -> str:
    r"""What counts as "the same show" for the memory.

    The arr's own series id where there is one - it survives a rename, a
    re-import and a folder move, none of which a title does. Same key
    audiolang has used for its own memory since it shipped, so a person who
    has taught nuarr one thing about a show is taught it in the same units.
    """
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT arr_name, arr_parent_id, library, title FROM files "
                " WHERE id=?", (int(file_id),)).fetchone()
        if not r:
            return ""
        if r["arr_name"] and r["arr_parent_id"]:
            return f"{r['arr_name']}#{int(r['arr_parent_id'])}"
        return (f"~{(r['library'] or '').strip().lower()}"
                f"#{(r['title'] or '').strip().lower()}")
    except Exception:                                            # noqa: BLE001
        return ""


def group_key(path: str) -> str:
    r"""The release group, which is the other thing an answer generalises to.

    A group's habits are consistent across every show it touches: sxales
    ships .en.ass, .1.en.ass and .2.en.ass for one subtitle; another group
    always labels its CC track "Forced". Teaching nuarr that once and having
    it hold for every release from that group is the difference between
    answering a question and answering it nine hundred times.
    """
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    # AFTER THE LAST BRACKET, NOT AFTER THE LAST DASH. Episode titles are full
    # of dashes - "Height Unseen and Bygone Errors and Folly Calls" sits
    # between two of them - so splitting on the last dash in the whole name
    # returned most of the title and the group was never found. The group, by
    # convention, is what follows the final "]": "[EAC3 5.1]-EPSiLON".
    i = stem.rfind("]")
    tail = stem[i + 1:].strip() if i >= 0 else ""
    if not tail.startswith("-"):
        return ""
    tail = tail.lstrip("-").strip("[]{}() ")
    if not tail or len(tail) > 24 or tail.isdigit():
        return ""
    return tail.lower()


def recall(file_id: int, path: str, question: str) -> tuple:
    r"""What you said last time about this kind of question. Show, then group.

    THE SHOW OUTRANKS THE GROUP, and deliberately: the group's habit is a
    generalisation, the show is the thing you were actually looking at. When
    you have answered for both and they disagree, the narrower answer is the
    one you meant.
    """
    _mem_init()
    # THIS FILE FIRST, then the show, then the group. Narrowest wins, so
    # "just this one" is an exception you can make without unpicking what you
    # taught it about everything else.
    for scope, key in (("file", str(int(file_id))),
                       ("show", show_key(file_id)),
                       ("group", group_key(path))):
        if not key:
            continue
        try:
            with cursor() as cur:
                r = cur.execute(
                    "SELECT answer, note FROM sub_memory "
                    " WHERE scope=? AND skey=? AND question=?",
                    (scope, key, question)).fetchone()
            if r:
                return str(r["answer"]), f"{scope} {key}"
        except Exception:                                        # noqa: BLE001
            pass
    return "", ""


def remember(file_id: int, path: str, question: str, answer: str,
             scopes=("show", "group"), note: str = "") -> dict:
    """Write down what you decided, so the next episode does not ask."""
    _mem_init()
    wrote = []
    for scope in scopes:
        key = {"file": str(int(file_id)), "show": show_key(file_id),
               "group": group_key(path)}.get(scope, "")
        if not key:
            continue
        try:
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO sub_memory(scope,skey,question,answer,at,"
                    "  used,note) VALUES(?,?,?,?,?,0,?) "
                    "ON CONFLICT(scope,skey,question) DO UPDATE SET "
                    "  answer=excluded.answer, at=excluded.at, "
                    "  note=excluded.note",
                    (scope, key, question, str(answer), time.time(), note))
            wrote.append(f"{scope} {key}")
        except Exception:                                        # noqa: BLE001
            pass
    return {"ok": bool(wrote), "learned": wrote}


def memory(limit: int = 400) -> list:
    """Everything you have taught it, newest first."""
    _mem_init()
    try:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT scope, skey, question, answer, at, used, note "
                "  FROM sub_memory ORDER BY at DESC LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def unlearn(scope: str, skey: str, question: str) -> dict:
    _mem_init()
    with cursor() as cur:
        cur.execute("DELETE FROM sub_memory WHERE scope=? AND skey=? "
                    "  AND question=?", (scope, skey, question))
    return {"ok": True}


# ------------------------------------------------------------ the revision --
def revision() -> str:
    r"""A short hash of every setting that can change an instruction.

    WHY A HASH AND NOT A VERSION NUMBER. There is no single place a rule
    change passes through - a library's languages, the sidecar switches, the
    two sureness lines and the duplicate toggle all live in different panels
    written at different times. Asking what they ADD UP TO catches all of them
    including the ones added later, and it cannot drift out of date, because
    it is computed from the settings rather than bumped by whoever remembers.
    """
    from . import subocr
    from .config import SETTINGS
    bits = []
    try:
        from . import langpolicy
        for l in (SETTINGS.libraries or []):
            r = subocr.sub_rules(l.name)
            pol = langpolicy.for_library(l.name, "subs") or {}
            bits.append(f"{l.name}|{json.dumps(r, sort_keys=True)}"
                        f"|{json.dumps(pol, sort_keys=True, default=str)}")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import hardsub, subdupe
        bits.append(f"mode={hardsub.mode()}|mark={hardsub.mark_at()}"
                    f"|dis={hardsub.dismiss_at()}|dupe={subdupe.enabled()}")
    except Exception:                                            # noqa: BLE001
        pass
    return hashlib.sha1("\n".join(bits).encode("utf-8",
                                               "replace")).hexdigest()[:12]


# -------------------------------------------------------------- the context --
def context() -> dict:
    r"""Everything the planner needs that is the same for every file.

    LOADED ONCE PER PASS, NOT ONCE PER FILE. Planning six thousand files with
    a per-file settings lookup measured four seconds of pure dictionary
    rebuilding; the rules do not change while a pass is running, so they are
    read once and handed down.
    """
    from . import subocr
    from .config import SETTINGS
    ctx = {"rev": revision(), "rules": {}, "langs": {}, "titles": {},
           "mark_at": 85, "dismiss_at": 30, "mode": "manual", "dupe": False}
    try:
        from . import langpolicy
        for l in (SETTINGS.libraries or []):
            ctx["rules"][l.name] = subocr.sub_rules(l.name)
            ctx["langs"][l.name] = langpolicy.for_library(l.name, "subs") or {}
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import hardsub, subdupe
        ctx.update(mark_at=hardsub.mark_at(), dismiss_at=hardsub.dismiss_at(),
                   mode=hardsub.mode(), dupe=bool(subdupe.enabled()))
    except Exception:                                            # noqa: BLE001
        pass
    # The title reader's verdicts, indexed by file, so a per-file lookup is a
    # dict hit rather than a query.
    try:
        from . import subtitletitle as stt
        for r in (stt.cached().get("rows") or []):
            ctx["titles"].setdefault(int(r["file_id"]), []).append(r)
    except Exception:                                            # noqa: BLE001
        pass
    return ctx


def _wants(lang: str, library: str, ctx: dict) -> tuple:
    """Would this library keep a subtitle in this language? From the context."""
    from .subembed import _lang_key
    pol = ctx["langs"].get(library) or {}
    keep = {_lang_key(x) for x in (pol.get("langs") or [])}
    k = _lang_key(lang)
    if k in keep:
        return True, ""
    if pol.get("keep_untagged") and k in ("un", "", "und"):
        return True, ""
    want = ", ".join(sorted(pol.get("langs") or [])) or "nothing"
    return False, (f"{library or 'this library'} keeps {want} subtitles, and "
                   f"this one is {lang or 'untagged'}")


# ---------------------------------------------------------------- the plan --
def plan(f: dict, ctx: dict | None = None) -> dict:
    r"""One file's whole instruction, from its facts and the current rules."""
    from .subembed import _lang_key, _role_class
    ctx = ctx or context()
    fid = int(f.get("file_id") or 0)
    path = f.get("path") or ""
    lib = f.get("library") or ""
    rules = ctx["rules"].get(lib) or {}
    tracks = list(f.get("tracks") or [])
    sides = list(f.get("sides") or [])
    pic = dict(f.get("picture") or {})
    steps: list = []
    asks: list = []
    skips: list = []

    embed_on = bool(rules.get("embed_sidecars"))
    conflict = str(rules.get("sidecar_conflict") or "leave")
    if conflict == "sidecar" and not embed_on:
        # Keeping the loose copy is a way of taking it in; with the rule above
        # off it is not a third answer, it is the first one relabelled.
        conflict = "leave"

    def kin(lang, cls):
        k = _lang_key(lang)
        return [t for t in tracks if t["lang"] == k and t["class"] == cls]

    def markers(lang):
        k = _lang_key(lang)
        return [t for t in tracks if t["lang"] == k and t["class"] == "marker"]

    # ---- 1. what is sitting beside it -------------------------------------
    # Best format first, so a release shipping .ass and .srt of one subtitle
    # does not get both.
    best: dict = {}
    for s in sides:
        if not s.get("ok"):
            skips.append({"what": s.get("name"), "why": s.get("why") or
                          "the name does not say what language it is"})
            continue
        if int(s.get("size") or 0) <= 0:
            steps.append({"do": "recycle", "side": s["path"],
                          "name": s.get("name"),
                          "why": "the subtitle file is empty - nothing was "
                                 "ever written to it"})
            continue
        k = (_lang_key(s["lang"]), _role_class(s.get("role") or ""))
        rank = PREFER.get("." + (s.get("ext") or ""), 9)
        cur = best.get(k)
        if cur is None or rank < cur[0]:
            if cur is not None:
                skips.append({"what": cur[1].get("name"),
                              "why": f"another {cur[1]['lang']} subtitle in a "
                                     f"better format is being taken instead"})
            best[k] = (rank, s)
        else:
            skips.append({"what": s.get("name"),
                          "why": f"another {s['lang']} subtitle in a better "
                                 f"format is being taken instead"})

    for (lang, cls), (_r, s) in sorted(best.items()):
        ok, why = _wants(lang, lib, ctx)
        if not ok:
            skips.append({"what": s.get("name"), "why": why})
            continue
        # A FILE WHOSE WORDS ARE PAINTED ON IS LEFT ALONE IN THAT LANGUAGE.
        # A second copy inside it would play on top of the words already on
        # the screen. This is the case the survey caught before it did harm.
        if markers(lang) or (pic.get("marked") and _lang_key(lang) == "eng"):
            skips.append({"what": s.get("name"),
                          "why": "this file's words are painted into the "
                                 "picture in that language"})
            continue
        twins = kin(lang, cls)
        if not twins:
            if embed_on:
                steps.append({"do": "take", "side": s["path"],
                              "name": s.get("name"), "lang": lang, "role":
                              s.get("role") or "", "cls": cls, "replaces": [],
                              "size": s.get("size") or 0,
                              "why": f"nothing inside is {cls} {lang}"})
            else:
                skips.append({"what": s.get("name"),
                              "why": "taking subtitle files inside is off for "
                                     "this library"})
            continue
        if conflict == "inside":
            steps.append({"do": "recycle", "side": s["path"],
                          "name": s.get("name"),
                          "why": f"the file already carries a {cls} {lang} "
                                 f"track"})
            continue
        if conflict == "sidecar":
            if len(twins) == 1:
                steps.append({"do": "take", "side": s["path"],
                              "name": s.get("name"), "lang": lang,
                              "role": s.get("role") or "", "cls": cls,
                              "replaces": [twins[0]["ord"]],
                              "size": s.get("size") or 0,
                              "why": f"replaces the {cls} {lang} track "
                                     f"already inside"})
                continue
            # AMBIGUOUS, AND THE MEMORY IS ASKED BEFORE YOU ARE.
            said, whose = recall(fid, path, "sidecar_twin")
            if said == "leave":
                skips.append({"what": s.get("name"),
                              "why": f"you said leave these alone ({whose})"})
                continue
            if said == "recycle":
                steps.append({"do": "recycle", "side": s["path"],
                              "name": s.get("name"),
                              "why": f"you said keep what is inside ({whose})"})
                continue
            asks.append({
                "q": "sidecar_twin", "side": s["path"], "name": s.get("name"),
                "lang": lang, "cls": cls,
                "ords": [t["ord"] for t in twins],
                "asking": (f"{s.get('name')} is another {cls} {lang} subtitle, "
                           f"and the file already has {len(twins)} of them. "
                           f"Which should win?"),
                "options": [
                    {"v": "recycle", "label": "Keep what is inside",
                     "what": "The loose copy is binned; the file is untouched."},
                    {"v": "leave", "label": "Leave both alone",
                     "what": "Nothing happens, and it stops being asked."},
                ]})
            continue
        skips.append({"what": s.get("name"),
                      "why": f"the file already has a {cls} {lang} track "
                             f"inside it"})

    # ---- 2. what is inside it twice, and what is inside it empty ----------
    if ctx.get("dupe"):
        groups: dict = {}
        for t in tracks:
            if t["class"] == "marker":
                continue
            groups.setdefault((t["lang"], t["class"]), []).append(t)
        taken = {(_lang_key(s["lang"]), s["cls"]) for s in steps
                 if s["do"] == "take"}
        for (lang, cls), ts in sorted(groups.items()):
            if len(ts) < 2:
                continue
            if (lang, cls) in taken:
                # A sidecar is going in as this language and kind in the same
                # pass; what is duplicated will be different afterwards, so
                # the duplicate question is asked of the file that results,
                # not of this one.
                continue
            # MEASURED, NOT MERELY STATED. NUMBER_OF_FRAMES in the header is
            # a good hint and a bad witness: on a remuxed file it can be left
            # over from the container it came from, and two tracks can carry
            # the same header figure and different content. So a decision is
            # taken from the header only when it separates them; a tie in the
            # header is not a question for a person, it is a measurement
            # nobody has taken yet.
            known = [t for t in ts if int(t.get("events", -1)) >= 0]
            if len(known) == len(ts):
                best_n = max(_lines(t) for t in ts)
                winners = [t for t in ts if _lines(t) == best_n]
                if len(winners) > 1 and best_n > 0:
                    said, whose = recall(fid, path, "twin_tie")
                    if said == "first":
                        winners = winners[:1]
                    else:
                        asks.append({
                            "q": "twin_tie", "lang": lang, "cls": cls,
                            "ords": [t["ord"] for t in ts],
                            "asking": (f"Two {cls} {lang} tracks with the same "
                                       f"{best_n} lines. Which one stays?"),
                            "options": ([{"v": f"ord{t['ord']}",
                                          "label": (f"track {t['ord'] + 1}"
                                                    + (f" — {t['title']}"
                                                       if t.get('title')
                                                       else "")),
                                          "what": f"{_lines(t)} lines"}
                                         for t in ts]
                                        + [{"v": "first",
                                            "label": "Always keep the first",
                                            "what": "and stop asking"}])})
                        continue
                keep = winners[0]
                for t in ts:
                    if t["ord"] == keep["ord"]:
                        continue
                    steps.append({"do": "dropdup", "ord": t["ord"],
                                  "lang": lang, "cls": cls,
                                  "lines": _lines(t),
                                  "why": f"a second {cls} {lang} track; "
                                         f"keeping the one with {best_n} "
                                         f"lines"})
            else:
                # NOT KNOWN YET, AND NOT A QUESTION FOR A PERSON. Which copy
                # is fuller is measurable - extract and count - so the worker
                # is told to weigh them rather than you being asked to guess.
                steps.append({"do": "dropdup", "ord": -1, "lang": lang,
                              "cls": cls, "weigh": [t["ord"] for t in ts],
                              "why": f"{len(ts)} {cls} {lang} tracks - the "
                                     f"one with the most lines stays, and "
                                     f"they are counted before anything goes"})
        for t in tracks:
            if t["class"] == "marker" or _lines(t) != 0:
                continue
            if any(s.get("ord") == t["ord"] for s in steps
                   if s["do"] == "dropdup"):
                continue
            steps.append({"do": "dropempty", "ord": t["ord"],
                          "lang": t["lang"], "cls": t["class"],
                          "why": "there is nothing in this track - no lines "
                                 "at all"})

    # ---- 3. a title that does not describe the track ----------------------
    for r in (ctx["titles"].get(fid) or []):
        if not r.get("rewritable") or r.get("acked"):
            continue
        sure = int(r.get("sure") or 0)
        if r.get("unread"):
            continue
        if sure >= ctx["mark_at"]:
            steps.append({"do": "retitle", "ord": int(r.get("track") or 0),
                          "from": r.get("old") or "", "to": r.get("new") or "",
                          "sure": sure,
                          "why": r.get("kind_why") or r.get("why") or ""})
        elif sure > ctx["dismiss_at"]:
            said, whose = recall(fid, path, "title")
            if said == "retitle":
                steps.append({"do": "retitle", "ord": int(r.get("track") or 0),
                              "from": r.get("old") or "",
                              "to": r.get("new") or "", "sure": sure,
                              "why": f"you said correct these ({whose})"})
            elif said == "leave":
                skips.append({"what": f"track {int(r.get('track') or 0) + 1}",
                              "why": f"you said leave these titles ({whose})"})
            else:
                asks.append({
                    "q": "title", "ord": int(r.get("track") or 0),
                    "from": r.get("old") or "", "to": r.get("new") or "",
                    "sure": sure,
                    "asking": (f"Track {int(r.get('track') or 0) + 1} is "
                               f"called {r.get('old') or '(nothing)'}, and it "
                               f"reads like {r.get('new') or 'something else'}"
                               f" — {sure}% sure."),
                    "options": [
                        {"v": "retitle", "label": "Correct it",
                         "what": r.get("new") or ""},
                        {"v": "leave", "label": "Leave the title alone",
                         "what": "and stop asking about this show"}]})

    # ---- 4. words painted into the picture --------------------------------
    if pic and not pic.get("marked"):
        state = str(pic.get("state") or "")
        sure = int(pic.get("sure") or 0)
        if state and state not in ("none", "clean"):
            if pic.get("by_hand") or sure >= ctx["mark_at"]:
                steps.append({"do": "mark", "kind": state, "sure": sure,
                              "why": ("you said so" if pic.get("by_hand")
                                      else f"{sure}% of the sampled frames "
                                           f"carry words")})
            elif sure > ctx["dismiss_at"]:
                said, whose = recall(fid, path, "picture")
                if said == "mark":
                    steps.append({"do": "mark", "kind": state, "sure": sure,
                                  "why": f"you said mark these ({whose})"})
                elif said == "leave":
                    skips.append({"what": "the picture",
                                  "why": f"you said leave these ({whose})"})
                else:
                    asks.append({
                        "q": "picture", "kind": state, "sure": sure,
                        "asking": (f"{sure}% of the sampled frames look like "
                                   f"they carry words painted into the "
                                   f"picture."),
                        "words": pic.get("words") or "",
                        "options": [
                            {"v": "mark", "label": "Yes, they are burned in",
                             "what": "a blank marker track tells Bazarr and "
                                     "Plex there is nothing to fetch"},
                            {"v": "leave", "label": "No, leave it",
                             "what": "and stop asking about this show"}]})

    steps.sort(key=lambda s: ORDER.get(s["do"], 9))
    rewrite = any(s["do"] in ("take", "dropdup", "dropempty") for s in steps)
    return {"file_id": fid, "path": path, "library": lib,
            "disk": f.get("disk") or "", "name": os.path.basename(path),
            "steps": steps, "asks": asks, "skips": skips,
            "n": len(steps), "rewrite": rewrite, "rev": ctx["rev"],
            "why": _sentence(steps, asks)}


def _lines(t: dict) -> int:
    """How many lines this track carries: counted if known, else the header's."""
    n = int(t.get("events", -1))
    if n >= 0:
        return n
    return int(t.get("cues") or -1)


def _sentence(steps: list, asks: list) -> str:
    """The instruction as one line, for a table cell."""
    if not steps and not asks:
        return "nothing to do"
    bits = []
    n = {}
    for s in steps:
        n[s["do"]] = n.get(s["do"], 0) + 1
    words = {"take": "take in", "dropdup": "drop a duplicate track",
             "dropempty": "drop an empty track", "retitle": "correct a title",
             "mark": "mark the burned-in words", "recycle": "recycle a loose copy"}
    for k, v in n.items():
        w = words.get(k, k)
        bits.append(f"{w}" if v == 1 else f"{w} ×{v}")
    if asks:
        bits.append(f"{len(asks)} to ask about")
    return "; ".join(bits)
