r"""What KIND of thing is this - anime, animation, or live action?

WHY THE FOLDER WAS NEVER THE RIGHT ANSWER
-----------------------------------------
Everything downstream of this question - which audio languages survive, whether
the shrink rule is allowed to touch a file, which codec settings apply - was
decided by asking whether a folder name started with "Anime". That works
exactly as far as the library is tidy, and it has three failure modes that are
not theoretical:

  A misfiled title.      An anime in P:\TV Shows gets the live-action audio
                         policy, which keeps English only. On a subtitled show
                         that means dropping the Japanese track and leaving the
                         file unwatchable.
  A mixed library.       "Movies" holds Akira and Aliens. The folder cannot
                         distinguish them, so one of the two is wrong whatever
                         it says.
  A new library.         "Cartoons" or "Foreign Film" starts as live action
                         with nobody told.

The arrs already know the answer. Sonarr and Radarr carry the metadata that
TheTVDB and TMDB publish for every title - genres, original language, and on
Sonarr a seriesType - and it is per TITLE, which is the level the question is
actually asked at. So ask them.

WHAT THE DATA LOOKS LIKE HERE, measured against this library
------------------------------------------------------------
    Sonarr   1,104 series   genre "Anime" on 705, "Animation" on 823
                            seriesType: standard 1,100, anime 3, daily 1
    Radarr   2,122 movies   genre "Animation" on 625, no "Anime" genre at all

Two things follow. TheTVDB tags an explicit "Anime" genre and it is well
populated, so on the TV side that alone is a strong signal. seriesType is NOT -
it is left at "standard" on essentially everything here, so it can only ever
confirm, never deny. And TMDB has no anime genre whatsoever, so for films the
only available test is animation plus a Japanese origin.

METADATA ADDS, IT NEVER TAKES AWAY - AND THAT IS NOT TIMIDITY
-------------------------------------------------------------
Checked against the whole library, the two methods agree on 98.1% of 39,470
files. The 738 that differ are what settled the design, because they go BOTH
ways:

    Don't Hug Me I'm Scared   in TV Shows, genre Animation
                              -> metadata is right, the folder is wrong
    Craig of the Creek        an American cartoon TheTVDB tags "Anime"
                              -> the folder is right, the metadata is wrong
    Beet the Vandel Buster    real anime carrying only "Animation"
                              -> the folder is right, the metadata is thin
    Chibi Devi!               real anime with NO animation genre at all
                              -> metadata says LIVE ACTION, which is a disaster

That last row is the whole argument. 127 files in Anime Shows have no animation
genre upstream, and trusting metadata outright would hand them the live-action
audio policy - English only - which drops the Japanese track from a subtitled
show and leaves it unwatchable. The folder was not merely a worse signal; on
those titles it was the only correct one.

So the two are COMBINED, and the combination is deliberately asymmetric:

    anime > animation > live, and the higher of the two wins.

The costs of being wrong are not symmetric either, which is what justifies it.
Calling live action "anime" keeps an audio track you did not need - a few MB.
Calling anime "live action" deletes the only track the show can be watched in.
One is a rounding error and the other is destructive, so the tie is broken
towards keeping more.

The practical effect is that metadata can PROMOTE a misfiled title - the
cartoon sitting in TV Shows starts being treated as animation - and can never
demote one. A library that is already tidy sees no change at all.

WHEN NOTHING IS KNOWN
---------------------
A file whose title nuarr cannot resolve - unmanaged, or an arr that has not
been reached yet - keeps the folder's answer. Silently reclassifying a whole
anime library because Sonarr was down during a sync is a far worse failure than
the one this replaces.
"""
from __future__ import annotations

import time

from .config import SETTINGS
from .db import cursor

KINDS = ("anime", "animation", "live")

# Genre names, lower-cased. TheTVDB and TMDB do not agree on spelling and both
# have changed theirs before, so match on a set rather than an exact string.
_ANIME_GENRES = {"anime"}
_ANIMATION_GENRES = {"animation", "animated"}

# Original languages that make an animated title anime rather than a cartoon.
# Japanese is the definition; Chinese and Korean are included because donghua
# and aeni sit in the same libraries here and want the same dual-audio policy -
# the reason the policy exists is "the original language is not English and you
# want to keep it", which is equally true of all three.
_ANIME_LANGS = {"japanese", "chinese", "mandarin", "cantonese", "korean",
                "ja", "jpn", "zh", "zho", "chi", "ko", "kor"}


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS parent_kind(
                arr_name  TEXT NOT NULL,
                parent_id INTEGER NOT NULL,
                kind      TEXT NOT NULL,
                why       TEXT,
                at        REAL,
                PRIMARY KEY (arr_name, parent_id))""")
        # THE EVIDENCE, NOT JUST THE VERDICT.
        #
        # `why` is one phrase - "genre Anime" - which is enough to audit the
        # decision but not enough to disagree with it. Seeing the actual genre
        # list is what lets somebody say "that is wrong, TheTVDB has mistagged
        # this", which is the whole reason to show metadata on a title at all.
        # Added rather than baked in, so an existing database upgrades in place.
        for col, decl in (("genres", "TEXT"), ("lang", "TEXT"),
                          ("stype", "TEXT")):
            try:
                cur.execute(f"ALTER TABLE parent_kind ADD COLUMN {col} {decl}")
            except Exception:
                pass                     # already there


def classify(item: dict) -> tuple[str, str]:
    """(kind, why) for one Sonarr series or Radarr movie.

    `why` is kept and shown, because a classification nobody can check is one
    nobody can correct. "genre Anime" and "Animation, original language
    Japanese" are both auditable claims; "anime" on its own is not.
    """
    genres = {str(g).strip().lower() for g in (item.get("genres") or [])}
    lang = str(((item.get("originalLanguage") or {}).get("name")
                or item.get("originalLanguage") or "")).strip().lower()
    stype = str(item.get("seriesType") or "").strip().lower()

    if genres & _ANIME_GENRES:
        return "anime", "genre Anime"
    # Only ever confirms. Measured here, seriesType is "standard" on 1,100 of
    # 1,104 series, so treating a missing "anime" as evidence of NOT anime
    # would reclassify the entire library.
    if stype == "anime":
        return "anime", "Sonarr seriesType anime"
    animated = bool(genres & _ANIMATION_GENRES)
    if animated and lang in _ANIME_LANGS:
        return "anime", f"animation, original language {lang.title()}"
    if animated:
        return "animation", "genre Animation"
    # A non-animated Japanese title is live action - a J-drama, not anime -
    # and must not be caught by the language test above.
    return "live", "no animation genre"


async def sync() -> dict:
    """Read genres and original language from every arr, store the verdict."""
    from .arr import shared_client
    init()
    counts = {k: 0 for k in KINDS}
    total = 0
    errs: list[str] = []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or not cfg.api_key:
            continue
        c = shared_client(cfg)
        path = "/series" if cfg.kind == "sonarr" else "/movie"
        try:
            items = await c._get(path)
        except Exception as e:
            errs.append(f"{cfg.name}: {type(e).__name__}")
            continue
        rows = []
        for it in items or []:
            if it.get("id") is None:
                continue
            kind, why = classify(it)
            counts[kind] += 1
            total += 1
            genres = ", ".join(str(g) for g in (it.get("genres") or []))
            lang = str(((it.get("originalLanguage") or {}).get("name")
                        or "")).strip()
            stype = str(it.get("seriesType") or "").strip()
            rows.append((cfg.name, int(it["id"]), kind, why,
                         genres, lang, stype))
        if not rows:
            continue
        now = time.time()
        with cursor() as cur:
            cur.executemany(
                "INSERT INTO parent_kind"
                "(arr_name,parent_id,kind,why,genres,lang,stype,at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(arr_name,parent_id) "
                "DO UPDATE SET kind=excluded.kind, why=excluded.why, "
                "              genres=excluded.genres, lang=excluded.lang, "
                "              stype=excluded.stype, at=excluded.at",
                [(a, p, k, w, g, l, st, now)
                 for a, p, k, w, g, l, st in rows])
    return {"titles": total, "counts": counts, "errors": errs}


# Read on the hot path - decide() runs per file during a scan - so the whole
# table is held in memory and refreshed on a timer. It is a few thousand short
# rows and it changes only when an arr's metadata does.
_CACHE: dict = {"at": 0.0, "map": None}
_TTL = 60.0


def _map() -> dict[tuple[str, int], str]:
    now = time.time()
    if _CACHE["map"] is not None and now - _CACHE["at"] < _TTL:
        return _CACHE["map"]
    m: dict[tuple[str, int], str] = {}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT arr_name, parent_id, kind FROM parent_kind"):
                m[(r["arr_name"], int(r["parent_id"]))] = r["kind"]
    except Exception:
        m = _CACHE["map"] or {}
    _CACHE.update(at=now, map=m)
    return m


def invalidate() -> None:
    _CACHE.update(at=0.0, map=None)
    _PATHS.update(at=0.0, map=None)


def for_parent(arr_name: str, parent_id) -> str | None:
    if not arr_name or parent_id is None:
        return None
    try:
        return _map().get((arr_name, int(parent_id)))
    except (TypeError, ValueError):
        return None


# anime beats animation beats live. See the asymmetry note at the top: this
# ordering IS the safety property, not a presentation choice.
_RANK = {"live": 0, "animation": 1, "anime": 2}


def combine(folder_kind: str, meta_kind: str | None) -> str:
    """The higher of what the folder says and what the metadata says."""
    a = _RANK.get(folder_kind or "live", 0)
    b = _RANK.get(meta_kind or "", -1)
    best = folder_kind if a >= b else meta_kind
    return best if best in KINDS else "live"


# path -> kind, built from one join and refreshed on the same timer as the
# parent map. rules.is_anime() is handed a path and nothing else - it is called
# from the executor, from enqueue, and from the audit - so the lookup has to
# work from that alone, and it has to be a dict read rather than a query
# because decide() runs tens of thousands of times in a scan.
_PATHS: dict = {"at": 0.0, "map": None}


def _path_map() -> dict[str, str]:
    now = time.time()
    if _PATHS["map"] is not None and now - _PATHS["at"] < _TTL:
        return _PATHS["map"]
    m: dict[str, str] = {}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT f.path, k.kind FROM files f "
                    "JOIN parent_kind k ON k.arr_name=f.arr_name "
                    "  AND k.parent_id=f.arr_parent_id "
                    "WHERE f.state!='deleted' AND f.path IS NOT NULL"):
                m[(r["path"] or "").lower()] = r["kind"]
    except Exception:
        m = _PATHS["map"] or {}
    _PATHS.update(at=now, map=m)
    return m


def by_path(path: str) -> str | None:
    """What the METADATA says about the title this file belongs to.

    Deliberately does not consult the folder - callers combine the two
    themselves, and a function that quietly did both would make it impossible
    to tell which signal produced an answer.
    """
    if not path:
        return None
    return _path_map().get(path.lower())


def for_file(path: str = "", library: str = "",
             arr_name: str = "", parent_id=None) -> str:
    """The kind to apply to one file. The single answer everything should use.

    Takes the arr identifiers when the caller has them and falls back to the
    folder when it does not, so a call site that only has a path still gets the
    old behaviour rather than an exception or a wrong default.
    """
    from .langpolicy import kind_for
    folder = kind_for(path=path, library=library)
    return combine(folder, for_parent(arr_name, parent_id))


def detail(arr_name: str, parent_id) -> dict | None:
    """Everything known about one title, for the folder view.

    A separate query rather than part of the cached map: this runs once when
    somebody opens a folder, not per file during a scan, and it returns the
    evidence rather than just the verdict.
    """
    if not arr_name or parent_id is None:
        return None
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT kind, why, genres, lang, stype, at FROM parent_kind "
                "WHERE arr_name=? AND parent_id=?",
                (arr_name, int(parent_id))).fetchone()
    except Exception:
        return None
    if not r:
        return None
    return {"kind": r["kind"], "why": r["why"],
            "genres": [g.strip() for g in (r["genres"] or "").split(",")
                       if g.strip()],
            "lang": r["lang"] or "", "stype": r["stype"] or "",
            "at": r["at"] or 0.0}


def coverage() -> dict:
    """How much of the library has a metadata verdict, for the settings page."""
    out = {"titles": 0, "counts": {k: 0 for k in KINDS}, "files": 0,
           "files_matched": 0, "at": 0.0}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT kind, COUNT(*) n, MAX(at) a FROM parent_kind "
                    "GROUP BY kind"):
                out["counts"][r["kind"]] = r["n"]
                out["titles"] += r["n"]
                out["at"] = max(out["at"], r["a"] or 0.0)
            out["files"] = cur.execute(
                "SELECT COUNT(*) n FROM files WHERE state!='deleted'"
            ).fetchone()["n"]
            out["files_matched"] = cur.execute(
                "SELECT COUNT(*) n FROM files f JOIN parent_kind k "
                "  ON k.arr_name=f.arr_name AND k.parent_id=f.arr_parent_id "
                "WHERE f.state!='deleted'").fetchone()["n"]
    except Exception:
        pass
    return out


async def watch() -> None:
    """Re-read the arrs on the same cadence origlang uses.

    Genres do not change often, but they DO change - a title gets retagged
    upstream, or a new series is added between full syncs - and a wrong kind is
    silent: the file simply gets the other library's audio policy.
    """
    import asyncio
    from . import joblog, schedules
    await asyncio.sleep(90)          # let the first scan and origlang settle
    while True:
        try:
            r = await sync()
            invalidate()
            if r.get("titles"):
                c = r["counts"]
                joblog.log(
                    f"content kinds: {r['titles']:,} titles — "
                    f"{c['anime']:,} anime, {c['animation']:,} animation, "
                    f"{c['live']:,} live action", "debug")
            schedules.beat("contentkind")
        except Exception as e:
            joblog.log(f"content kind sync: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(12 * 3600)
