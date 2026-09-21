r"""Does the audio carry the language this library asks for?

WHAT THIS IS FOR. Erik found an EyeShield 21 release scoring +4000 in Sonarr
as "Anime Dual Audio" whose two audio tracks are Portuguese and Japanese.
There is no English in it at all. The release NAME says DUAL, and Sonarr
believed the name, because at the moment it has to decide whether to grab
something a name is all it has.

Sonarr cannot be taught its way out of that. A custom format is judged
against releases and against files with the same rules, and a release's
languages are guessed from its title - so "this has no English" is true of
every dual-audio release ever offered, and a rule banning them would ban the
good ones too. Measured: parsing a perfectly good "Blue Lock S01E02 1080p
WEB-DL DUAL" the language specification for Japanese matches, because Sonarr
attaches the SERIES' original language to the release. There is no wording
of that rule that separates the liar from the honest release.

nuarr can, because it comes after. The file is here, its tracks have been
probed and in most cases listened to, and "there is no English in this file"
is then a measurement rather than a reading of somebody's filename.

WHAT IT ACCUSES, AND WHAT IT LEAVES ALONE. Not every file without English.
A library of subtitled Japanese shows is not a fault, and Erik has said so
twice - a hard dual-audio requirement was tried and reverted. What is a
fault is a file that CLAIMS to carry the language and does not:

  - it carries two or more audio languages - a genuine dual - and the one
    this library requires is not among them, or
  - its name claims a dub or dual audio, and the audio does not back it up.

A Japanese-only release, honestly named, is never touched by this.

AND THE SWITCH IS THE LIBRARY'S OWN. The required list lives in langpolicy
beside the subtitle one, per library, so what counts as a fault here follows
what that library asks for rather than a constant compiled into this file.
Anime Shows requiring eng means an anime claiming a dub had better have one;
a library requiring nothing is never judged at all.
"""
from __future__ import annotations

import json
import os
import re
import time

from .db import cursor

KIND = "audio/missing-language"

OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"

# What a release says about itself. The same shapes the arrs look for, kept
# here because this check has to recognise the CLAIM before it can call the
# claim a lie.
_CLAIMS = re.compile(
    r"dual[ ._-]?audio|[([]dual[])]|\bdual\b|\bdub(bed)?\b|"
    r"(funi|eng(lish)?)[ ._-]?dub|\bdublado\b|\bdoblado\b|\bdoppiato\b|"
    r"\b(JA|ZH|KO)\s*\+\s*[A-Z]{2}\b|\b[A-Z]{2}\s*\+\s*(JA|ZH|KO)\b",
    re.I)

RULES = {
    "unheard":  {"sure": 0,  "why": "nothing has listened to this file and it "
                                    "carries no usable audio tags"},
    "has_it":   {"sure": 95, "why": "the audio carries the language"},
    "tagged":   {"sure": 70, "why": "a tag says the audio carries the "
                                    "language, and nobody has listened"},
    "single":   {"sure": 90, "why": "one audio language, honestly named - not "
                                    "a fault, just not a dub"},
    "dual_no":  {"sure": 92, "why": "it carries two or more audio languages "
                                    "and the required one is not among them"},
    "claim_no": {"sure": 85, "why": "the name claims a dub or dual audio and "
                                    "the audio does not carry it"},
}
RULE_ORDER = ["unheard", "has_it", "tagged", "dual_no", "claim_no", "single"]
assert set(RULE_ORDER) == set(RULES)

_READY = False
STATE: dict = {"running": False, "checked": 0, "ok": 0, "missing": 0,
               "unknown": 0, "at": 0.0, "err": ""}
BATCH = 5000


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS aud_need(
                file_id    INTEGER NOT NULL,
                lang       TEXT    NOT NULL,
                library    TEXT    NOT NULL DEFAULT '',
                state      TEXT    NOT NULL DEFAULT '',
                why        TEXT    NOT NULL DEFAULT '',
                rule       TEXT    NOT NULL DEFAULT '',
                sure       INTEGER,
                checked_at REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY (file_id, lang)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_aud_need_state "
                    "ON aud_need(state, library)")
    _READY = True


def required() -> dict:
    """library -> the audio languages it requires. Read every time, cheap:
    langpolicy holds the text in a cache of its own."""
    from . import langpolicy
    out: dict = {}
    for name, sides in (langpolicy.load() or {}).items():
        want = [str(x).lower()[:3]
                for x in ((sides.get("audio") or {}).get("require") or [])]
        if want:
            out[name] = sorted(set(want))
    return out


def _langs(file_id: int, cur) -> tuple[set, set, bool]:
    """(what the audio really is, what only a tag claims, has anyone listened).

    Heard beats tagged per track, exactly as the subtitle check does it - see
    subneed._audio_langs, which this borrows rather than copies badly.
    """
    from . import subneed
    heard_any = False
    try:
        r = cur.execute("SELECT COUNT(*) n FROM audio_lang a JOIN files f "
                        "  ON f.id=a.file_id "
                        " WHERE a.file_id=? AND COALESCE(a.ok,0)=1 "
                        "   AND a.size = COALESCE(f.size,0)",
                        (int(file_id),)).fetchone()
        heard_any = bool(r and int(r["n"] or 0))
    except Exception:                                            # noqa: BLE001
        heard_any = False
    langs = {x for x in subneed._audio_langs(file_id, cur) if x}
    real = {x for x in langs if x not in ("und", "", "zxx")}
    return real, langs, heard_any


def verdict(file_id: int, lang: str, cur, path: str = "") -> tuple:
    """(state, why, rule) for one file against one required audio language."""
    lang = (lang or "").lower()[:3]
    real, all_langs, heard_any = _langs(file_id, cur)
    name = os.path.basename(path or "")
    claims = bool(_CLAIMS.search(name))
    if not real and not heard_any:
        return UNKNOWN, ("nothing has listened to this file and its audio "
                         "carries no language tag"), "unheard"
    if lang in real:
        return OK, (f"the audio carries {lang}" if heard_any
                    else f"the audio is tagged {lang}"), (
            "has_it" if heard_any else "tagged")
    others = ", ".join(sorted(real)) or "nothing named"
    if len(real) >= 2:
        return MISSING, (f"it carries two audio languages - {others} - and "
                         f"{lang} is not one of them, so whatever the release "
                         f"called itself it is not a {lang} dual audio"), \
            "dual_no"
    if claims:
        return MISSING, (f"the release name claims a dub or dual audio and "
                         f"the audio is {others}; there is no {lang} in it"), \
            "claim_no"
    return OK, (f"one audio language ({others}) and the name claims nothing "
                f"else - not a {lang} dub, but not a lie either"), "single"


def check_one(file_id: int, cur=None) -> dict:
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
            cur.execute("DELETE FROM aud_need WHERE file_id=?", (int(file_id),))
            return {"ok": True, "checked": 0}
        now = time.time()
        out = {}
        for lang in want:
            st, why, rule = verdict(file_id, lang, cur, str(row["path"] or ""))
            out[lang] = st
            cur.execute(
                "INSERT INTO aud_need(file_id,lang,library,state,why,rule,"
                "                     sure,checked_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(file_id,lang) DO UPDATE SET "
                " library=excluded.library, state=excluded.state, "
                " why=excluded.why, rule=excluded.rule, sure=excluded.sure, "
                " checked_at=excluded.checked_at",
                (int(file_id), lang, lib, st, why, rule,
                 int(RULES[rule]["sure"]), now))
        return {"ok": True, "checked": len(want), "states": out}
    finally:
        if close:
            ctx.__exit__(None, None, None)


def sweep(limit: int = BATCH) -> dict:
    """Judge every file in every library that requires an audio language."""
    init()
    want_by_lib = required()
    STATE.update(running=True, err="")
    t0 = time.time()
    n = 0
    tally = {OK: 0, MISSING: 0, UNKNOWN: 0}
    try:
        with cursor() as cur:
            if not want_by_lib:
                # Turning the switch off has to actually turn it off, so the
                # rows go with it rather than lingering as accusations nobody
                # is making any more.
                cur.execute("DELETE FROM aud_need")
                STATE.update(checked=0, ok=0, missing=0, unknown=0)
                return {"ok": True, "checked": 0, "missing": 0, "unknown": 0}
            qs = ",".join("?" * len(want_by_lib))
            # LEAST RECENTLY JUDGED FIRST. Without the ordering a batch is
            # whatever SQLite hands back, which is the same five thousand
            # rows every pass - measured: 5,000 of the 23,008 files in Anime
            # Shows judged, the other 18,008 never looked at, including the
            # EyeShield episodes this check was written for.
            rows = cur.execute(
                f"SELECT f.id, f.library, f.path FROM files f "
                f"  LEFT JOIN aud_need n ON n.file_id = f.id "
                f" WHERE f.library IN ({qs}) "
                f"   AND f.state NOT IN ('deleted','duplicate') "
                f" ORDER BY COALESCE(n.checked_at, 0) ASC "
                f" LIMIT ?", list(want_by_lib) + [int(limit)]).fetchall()
            cur.execute(f"DELETE FROM aud_need WHERE library NOT IN ({qs})",
                        list(want_by_lib))
            now = time.time()
            for r in rows:
                for lang in want_by_lib.get(r["library"]) or []:
                    st, why, rule = verdict(int(r["id"]), lang, cur,
                                            str(r["path"] or ""))
                    tally[st] = tally.get(st, 0) + 1
                    cur.execute(
                        "INSERT INTO aud_need(file_id,lang,library,state,why,"
                        " rule,sure,checked_at) VALUES(?,?,?,?,?,?,?,?) "
                        " ON CONFLICT(file_id,lang) DO UPDATE SET "
                        " library=excluded.library, state=excluded.state, "
                        " why=excluded.why, rule=excluded.rule, "
                        " sure=excluded.sure, checked_at=excluded.checked_at",
                        (int(r["id"]), lang, r["library"], st, why, rule,
                         int(RULES[rule]["sure"]), now))
                n += 1
    except Exception as e:                                       # noqa: BLE001
        STATE.update(running=False, err=f"{type(e).__name__}: {e}")
        return {"ok": False, "why": STATE["err"]}
    STATE.update(running=False, checked=n, ok=tally[OK],
                 missing=tally[MISSING], unknown=tally[UNKNOWN],
                 at=time.time())
    return {"ok": True, "checked": n, "missing": tally[MISSING],
            "unknown": tally[UNKNOWN], "ms": int((time.time() - t0) * 1000)}


def _total() -> int:
    want = required()
    if not want:
        return 0
    try:
        with cursor() as cur:
            qs = ",".join("?" * len(want))
            r = cur.execute(
                f"SELECT COUNT(*) n FROM files WHERE library IN ({qs}) "
                f"  AND state NOT IN ('deleted','duplicate')",
                list(want)).fetchone()
        return int((r["n"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def sweep_all() -> dict:
    """Every file, not one batch - the twin of subneed.sweep_all.

    A rule changing is the moment the whole answer changes, so the switch
    waits for the whole answer rather than a fifth of it.
    """
    total = _total()
    n = 0
    t0 = time.time()
    tally = {MISSING: 0, UNKNOWN: 0}
    while n < total:
        r = sweep(BATCH)
        got = int(r.get("checked") or 0)
        if not got:
            break
        n += got
        tally[MISSING] = int(r.get("missing") or 0) + tally[MISSING]
        tally[UNKNOWN] = int(r.get("unknown") or 0) + tally[UNKNOWN]
    c = counts()
    return {"ok": True, "checked": n, "total": total,
            "missing": c.get(MISSING, 0), "unknown": c.get(UNKNOWN, 0),
            "ms": int((time.time() - t0) * 1000)}


def counts() -> dict:
    init()
    out = {OK: 0, MISSING: 0, UNKNOWN: 0}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT state, COUNT(*) n FROM aud_need "
                                 " GROUP BY state"):
                out[str(r["state"])] = int(r["n"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def accused(limit: int = 500, library: str = "") -> list:
    """The files this check says are lying about their audio."""
    init()
    sql = ("SELECT n.file_id, n.lang, n.library, n.why, n.rule, n.sure, "
           "       f.path, f.title, f.season, f.episode, f.size, "
           "       f.audio_langs, f.first_seen "
           "  FROM aud_need n JOIN files f ON f.id = n.file_id "
           " WHERE n.state = ? AND f.state NOT IN ('deleted','duplicate') ")
    args: list = [MISSING]
    if library:
        sql += " AND n.library = ? "
        args.append(library)
    sql += " ORDER BY n.sure DESC, f.path LIMIT ?"
    args.append(int(limit))
    out = []
    try:
        with cursor() as cur:
            for r in cur.execute(sql, args):
                out.append({
                    "file_id": int(r["file_id"]), "lang": r["lang"],
                    "library": r["library"], "why": r["why"],
                    "rule": r["rule"], "sure": int(r["sure"] or 0),
                    "path": r["path"], "title": r["title"],
                    "season": r["season"], "episode": r["episode"],
                    "size": int(r["size"] or 0),
                    "audio": r["audio_langs"] or "",
                    "added": r["first_seen"],
                })
    except Exception:                                            # noqa: BLE001
        return []
    return out


def by_library() -> dict:
    """library -> {required, missing, unknown, ok} for the page."""
    init()
    out: dict = {}
    for lib, want in required().items():
        out[lib] = {"require": want, OK: 0, MISSING: 0, UNKNOWN: 0}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT library, state, COUNT(*) n "
                                 "  FROM aud_need GROUP BY library, state"):
                d = out.setdefault(str(r["library"]),
                                   {"require": [], OK: 0, MISSING: 0,
                                    UNKNOWN: 0})
                d[str(r["state"])] = int(r["n"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def answered(file_id: int) -> None:
    """Replaced. The question goes with the file - see subneed.answered."""
    init()
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM aud_need WHERE file_id=?", (int(file_id),))
    except Exception:                                            # noqa: BLE001
        pass


def snapshot() -> dict:
    c = counts()
    return {"state": dict(STATE), "counts": c,
            "libraries": by_library(),
            "required": required(),
            "rules": {k: RULES[k] for k in RULE_ORDER}}
