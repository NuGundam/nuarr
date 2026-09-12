r"""nuarr - what should happen to this file's audio languages, and why.

THE SAME SHAPE AS subplan, AND DELIBERATELY SO. The subtitle page settled into
four parts - facts, an instruction, a queue, a page - and each part answers one
question. This is the instruction for audio: it takes what audiolang MEASURED
and the lines you set, and turns the two into an ordered list of steps for one
file. It opens no file, runs no subprocess, and writes to nothing except the
memory of what you have taught it.

THERE IS NO audscan, AND THAT IS NOT AN OMISSION. subscan exists because the
subtitle facts were scattered across three systems' caches with three
lifetimes. Audio has no such problem: audiolang's `audio_lang` table already IS
the fact table - one row per track, what was heard, how sure, from which
windows, against which file size - written by the one reader that produces it.
Building a second table in front of it would be copying a good answer into a
worse one.

THE STEPS

    tag      a track's language tag is corrected to what was heard
    retitle  a track's TITLE is corrected to stop naming another language

Both are mkvpropedit writing a header - a fraction of a second, no rewrite, no
re-encode, nothing re-read. Worth saying out loud, because it is the whole
reason a lying tag is cheap to fix and expensive to leave: every player,
Sonarr, Radarr and Bazarr read that header, and none of them listen to the
audio.

WHAT IT REFUSES TO GUESS. A reading between the two lines is yours to call, and
so is one on a show you have already left alone enough times for audiolang to
hold it. Both become questions rather than steps, and your answer is remembered
against this file, then the show, then the release group - the subtitle page's
memory rule, for the subtitle page's reason: the next episode should not ask.

WHY THE TITLES ARE FETCHED IN BULK. audiolang.title_lies is per-file and falls
back to running mkvmerge when the stored probe has nothing - right for one file
you are about to edit, ruinous across four thousand. Planning a whole pass
reads the probes it already has in ONE query and believes what they say; the
queue re-plans the single file it is about to touch with the live check on.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from .db import cursor

# The order steps must run in. A title is corrected to agree with its tag, so
# it has to be written after the tag it is agreeing with.
ORDER = {"tag": 0, "retitle": 1}

_MEM_READY = False


# ------------------------------------------------------------- the memory --
def _mem_init() -> None:
    global _MEM_READY
    if _MEM_READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS aud_memory(
                scope    TEXT NOT NULL,     -- file | show | group
                skey     TEXT NOT NULL,
                question TEXT NOT NULL,
                answer   TEXT NOT NULL,
                at       REAL NOT NULL DEFAULT 0,
                used     INTEGER NOT NULL DEFAULT 0,
                note     TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(scope, skey, question))""")
    _MEM_READY = True


def show_key(file_id: int) -> str:
    """What counts as "the same show".

    audiolang.series_key already answers this and has since it shipped, so
    this calls it rather than inventing a second spelling of the same key - a
    show you have taught one thing about is the same show to both.
    """
    try:
        from . import audiolang
        with cursor() as cur:
            r = cur.execute(
                "SELECT arr_name, arr_parent_id, library, title FROM files "
                " WHERE id=?", (int(file_id),)).fetchone()
        if not r:
            return ""
        return audiolang.series_key(r["arr_name"], r["arr_parent_id"],
                                    r["library"], r["title"])
    except Exception:                                            # noqa: BLE001
        return ""


def group_key(path: str) -> str:
    """The release group - what follows the final "]" in the name."""
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    i = stem.rfind("]")
    tail = stem[i + 1:].strip() if i >= 0 else ""
    if not tail.startswith("-"):
        return ""
    tail = tail.lstrip("-").strip("[]{}() ")
    if not tail or len(tail) > 24 or tail.isdigit():
        return ""
    return tail.lower()


def recall(file_id: int, path: str, question: str) -> tuple:
    """What you said last time. This file, then the show, then the group."""
    _mem_init()
    for scope, key in (("file", str(int(file_id))),
                       ("show", show_key(file_id)),
                       ("group", group_key(path))):
        if not key:
            continue
        try:
            with cursor() as cur:
                r = cur.execute(
                    "SELECT answer FROM aud_memory "
                    " WHERE scope=? AND skey=? AND question=?",
                    (scope, key, question)).fetchone()
            if r:
                return str(r["answer"]), f"{scope} {key}"
        except Exception:                                        # noqa: BLE001
            pass
    return "", ""


def remember(file_id: int, path: str, question: str, answer: str,
             scopes=("file", "show", "group"), note: str = "") -> dict:
    """Write down what you decided, so the next episode does not ask.

    "just this one" passes scopes=("file",). It has to mean something, or the
    file comes straight back on the next pass - which is exactly the bug the
    subtitle memory shipped with.
    """
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
                    "INSERT INTO aud_memory(scope,skey,question,answer,at,"
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
    _mem_init()
    try:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT scope, skey, question, answer, at, used, note "
                "  FROM aud_memory ORDER BY at DESC LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def unlearn(scope: str, skey: str, question: str) -> dict:
    _mem_init()
    with cursor() as cur:
        cur.execute("DELETE FROM aud_memory WHERE scope=? AND skey=? "
                    "  AND question=?", (scope, skey, question))
    return {"ok": True}


# ----------------------------------------------------------- the revision --
def revision() -> str:
    r"""A short hash of every setting that can change an instruction.

    subplan's argument, unchanged: there is no single place a rules change
    passes through, so asking what the settings ADD UP TO catches all of them -
    including ones added later - and cannot drift out of date, because it is
    computed rather than bumped.
    """
    from .config import SETTINGS
    bits = []
    try:
        from . import audiolang
        bits.append(f"mode={audiolang.mode()}|fix={audiolang.fix_at()}"
                    f"|leave={audiolang.leave_at()}")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import langpolicy
        for lib in (SETTINGS.libraries or []):
            name = getattr(lib, "name", "") or ""
            pol = langpolicy.for_library(name, "audio") or {}
            bits.append(f"{name}|"
                        f"{json.dumps(pol, sort_keys=True, default=str)}")
    except Exception:                                            # noqa: BLE001
        pass
    return hashlib.sha1("\n".join(bits).encode("utf-8",
                                               "replace")).hexdigest()[:12]


# --------------------------------------------------------- the title lies --
def _title_lies_bulk(file_ids: list) -> dict:
    r"""Every track whose TITLE names a language the track is not - in one go.

    THE SAME QUESTION audiolang.title_lies ASKS, ASKED OF THE PROBES WE HAVE.
    That function is right for one file: it falls back to running mkvmerge when
    the stored probe is missing or stale, because the file is about to be
    edited and a wrong answer would write a wrong title. Across a whole pass
    that fallback is thousands of processes, so this reads `file_probes` and
    believes it - and the queue re-checks live before it writes anything.
    """
    out: dict = {"lies": {}, "unread": set()}
    if not file_ids:
        return out
    try:
        from . import audiolang, langkey
    except Exception:                                            # noqa: BLE001
        return out
    names = getattr(audiolang, "_LANG_NAME", {}) or {}
    if not names:
        return out
    ids = [int(i) for i in file_ids]
    rows = []
    try:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = ",".join("?" * len(chunk))
            with cursor() as cur:
                rows += [dict(r) for r in cur.execute(
                    f"SELECT f.id file_id, f.audio_langs, p.json "
                    f"  FROM files f JOIN file_probes p ON p.file_id = f.id "
                    f" WHERE f.id IN ({q})", chunk)]
    except Exception:                                            # noqa: BLE001
        return out
    # WHAT IT HAS NO PROBE FOR IS NOT THE SAME AS WHAT IT FOUND NOTHING IN,
    # and reading the first as the second is how a lying title survives. Around
    # half the files in a mismatch list have no stored probe - they are named
    # BECAUSE something about them changed - so those are marked unread and the
    # queue looks at them live before it writes.
    out["unread"] = set(ids) - {int(r["file_id"]) for r in rows}
    for r in rows:
        try:
            streams = (json.loads(r["json"] or "{}").get("streams") or [])
        except Exception:                                        # noqa: BLE001
            out["unread"].add(int(r["file_id"]))
            continue
        auds = [s for s in streams if s.get("codec_type") == "audio"]
        codes = [c.strip() for c in (r["audio_langs"] or "").split(",")]
        found = []
        for i, code in enumerate(codes):
            if not code or code == "-" or i >= len(auds):
                continue
            title = str((auds[i].get("tags") or {}).get("title") or "").strip()
            if not title:
                continue
            for key, name in names.items():
                if not re.search(rf"\b{re.escape(name)}\b", title, re.I):
                    continue
                try:
                    if langkey.same(key, code):
                        break                  # the title agrees; move on
                except Exception:                                # noqa: BLE001
                    pass
                want_name = audiolang._name_of(code)
                if not want_name:
                    break                      # no name to put there
                want = re.sub(rf"\b{re.escape(name)}\b", want_name, title,
                              flags=re.IGNORECASE)
                if want and want != title:
                    found.append({"track": i, "title": title, "want": want,
                                  "names": name, "code": code})
                break
        if found:
            out["lies"][int(r["file_id"])] = found
    return out


def _disks(file_ids: list) -> dict:
    """Which spindle each file lives on. One query, because the queue deals
    rows round-robin by disk and doing that needs every row's disk at once."""
    out: dict = {}
    ids = [int(i) for i in (file_ids or [])]
    if not ids:
        return out
    try:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = ",".join("?" * len(chunk))
            with cursor() as cur:
                for r in cur.execute(
                        f"SELECT id, COALESCE(pool_disk,'') d FROM files "
                        f" WHERE id IN ({q})", chunk):
                    out[int(r["id"])] = r["d"] or ""
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ------------------------------------------------------------- the context --
def context(limit: int = 6000, titles: bool = True) -> dict:
    r"""Everything the planner needs that is the same for every file.

    READ ONCE PER PASS. The mismatch list, the title probes and the disks are
    each one query for the whole pass; asking per file turned a pass into
    twelve thousand round trips the first time subplan tried it.
    """
    from . import audiolang
    ctx = {"rev": revision(), "mode": "manual", "fix_at": 95, "leave_at": 60,
           "rows": [], "titles": {}, "untitled": set(), "disks": {}}
    try:
        ctx.update(mode=audiolang.mode(), fix_at=audiolang.fix_at(),
                   leave_at=audiolang.leave_at())
    except Exception:                                            # noqa: BLE001
        pass
    try:
        # respect_answers stays ON. A show you have retired should not come
        # back as work merely because the list is being built for a new reason.
        ctx["rows"] = audiolang.mismatches(int(limit))
    except Exception:                                            # noqa: BLE001
        ctx["rows"] = []
    ids = sorted({int(r.get("file_id") or 0)
                  for r in ctx["rows"] if r.get("file_id")})
    ctx["disks"] = _disks(ids)
    if titles:
        t = _title_lies_bulk(ids)
        ctx["titles"] = t.get("lies") or {}
        ctx["untitled"] = t.get("unread") or set()
    else:
        ctx["untitled"] = set(ids)
    return ctx


def by_file(ctx: dict | None = None) -> list:
    r"""Every file with something to say about its audio, one row each.

    audiolang.mismatches returns a row per TRACK. The queue and the page are
    both about FILES - two lying tags in one container are one mkvpropedit and
    one row, not two - so the tracks are folded here, once, rather than in
    every caller afterwards.
    """
    ctx = ctx if ctx is not None else context()
    disks = ctx.get("disks") or {}
    files: dict = {}
    order: list = []
    for r in (ctx.get("rows") or []):
        fid = int(r.get("file_id") or 0)
        if not fid:
            continue
        if fid not in files:
            order.append(fid)
            files[fid] = {"file_id": fid, "path": r.get("path") or "",
                          "library": r.get("library") or "",
                          "title": r.get("title") or "",
                          "series": r.get("series") or "",
                          "disk": disks.get(fid, ""), "tracks": []}
        files[fid]["tracks"].append(r)
    return [files[f] for f in order]


# ---------------------------------------------------------------- the plan --
def plan(row: dict, ctx: dict | None = None, live_titles: bool = False) -> dict:
    r"""One file's instruction, from what was heard and where your lines are.

    live_titles is for the queue, which is about to write to this one file and
    would rather pay for one mkvmerge than write a title from a stale probe.
    Everything that only DISPLAYS the plan leaves it off.
    """
    from . import audiolang
    ctx = ctx if ctx is not None else context()
    fid = int(row.get("file_id") or 0)
    path = row.get("path") or ""
    steps: list = []
    asks: list = []

    said, whose = recall(fid, path, "tag")
    for t in (row.get("tracks") or []):
        v = audiolang.verdict_for(t)
        sure = int(v.get("sure") or 0)
        heard = t.get("heard") or ""
        tagged = (t.get("tagged") or "").strip() or "untagged"
        track = int(t.get("track") or 0)
        if said == "leave":
            continue                       # you told it to leave these alone
        if v.get("auto") == "act" or said == "tag":
            steps.append({
                "do": "tag", "track": track, "from": tagged, "to": heard,
                "sure": sure,
                "why": (f"you said correct these ({whose})" if said == "tag"
                        else v.get("why") or "")})
        elif v.get("auto") == "ask":
            asks.append({
                "q": "tag", "track": track, "from": tagged, "to": heard,
                "sure": sure,
                "asking": (f"Track {track + 1} is tagged {tagged}, and "
                           f"{sure}% of what was heard is {heard}."),
                "why": v.get("why") or "",
                "held": bool(t.get("held")),
                "fake_dual": bool(t.get("fake_dual")),
                "options": [
                    {"v": "tag", "label": f"Yes, it is {heard}",
                     "what": "the tag is corrected - one header write, "
                             "nothing re-encoded"},
                    {"v": "leave", "label": "Leave the tag alone",
                     "what": "and stop asking about this show"}]})
        # auto == "leave" is below the lower line: not acted on, not asked.

    # A TITLE THAT NAMES ANOTHER LANGUAGE. The tag and the title are two
    # fields and only one of them was ever corrected, so a file can sit at
    # lang=jpn title="English E-AC3 5.1" forever. Same header write, so it
    # rides along rather than becoming a second visit.
    unread = fid in (ctx.get("untitled") or ())
    try:
        lies = (audiolang.title_lies(fid) if live_titles
                else (ctx.get("titles") or {}).get(fid) or [])
        for t in (lies or []):
            steps.append({"do": "retitle", "track": int(t.get("track") or 0),
                          "from": t.get("title") or "",
                          "to": t.get("want") or "",
                          "why": f"the title still says {t.get('names') or ''}"
                                 f" and the track is tagged "
                                 f"{t.get('code') or ''}"})
    except Exception:                                            # noqa: BLE001
        pass

    steps.sort(key=lambda s: (ORDER.get(s["do"], 9), s.get("track") or 0))
    return {"file_id": fid, "path": path, "name": os.path.basename(path),
            "library": row.get("library") or "",
            "disk": row.get("disk") or (ctx.get("disks") or {}).get(fid, ""),
            "series": row.get("series") or "",
            "steps": steps, "asks": asks,
            # A TRUTHFUL "I HAVE NOT LOOKED". The page can say the titles are
            # unchecked rather than implying they are clean, and the queue
            # knows to re-plan this one with the live check before it writes.
            "titles_unread": bool(unread and not live_titles),
            "n": len(steps), "n_asks": len(asks),
            "rev": ctx.get("rev") or "", "why": _sentence(steps, asks)}


def plan_all(ctx: dict | None = None) -> list:
    """The whole pass, planned. One context, read once, handed to every file."""
    ctx = ctx if ctx is not None else context()
    return [plan(f, ctx) for f in by_file(ctx)]


def _sentence(steps: list, asks: list) -> str:
    """The instruction as one line, for a table cell."""
    if not steps and not asks:
        return "nothing to do"
    n: dict = {}
    for s in steps:
        n[s["do"]] = n.get(s["do"], 0) + 1
    words = {"tag": "correct a language tag",
             "retitle": "correct a track title"}
    bits = []
    for k in ("tag", "retitle"):
        if k in n:
            bits.append(words[k] if n[k] == 1 else f"{words[k]} ×{n[k]}")
    if asks:
        bits.append(f"{len(asks)} to ask about")
    return "; ".join(bits)
