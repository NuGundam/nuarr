r"""
nuarr - what language was this actually made in?

WHY IT HAS TO BE FETCHED
------------------------
Nothing in a media file says which of its audio tracks is the original. A probe
of a Korean drama with Korean and English audio sees two tracks and a language
tag on each; it cannot tell that the Korean one is the performance and the
English one is a dub, or - for a Hollywood film dubbed into Korean - that it is
the other way round.

The arrs already know, because their metadata providers say so: TMDB gives
Radarr `originalLanguage` per movie, TheTVDB gives Sonarr the same per series.
It is a property of the WORK, fixed when the title was added and refreshed with
its metadata, and it never changes with the file.

So this copies that one field into nuarr, keyed on the parent the file belongs
to. Two API calls per arr - the whole series list and the whole movie list -
which is why it runs on a slow timer rather than per file.

WHAT IT IS FOR
--------------
The audio policy currently decides "dual audio or English only" from the FOLDER
NAME: anything under an Anime library keeps Japanese + English, everything else
keeps English and falls back to the original only when no English exists. That
works for the anime libraries it was written for and quietly mishandles
everything else - a Korean film in Movies loses its Korean track, a Spanish
series keeps a French dub it should have dropped.

With the original language stored, the rule can say what it means: keep English
AND the language the thing was made in, drop everything else. See
rules.decide(), which takes orig_lang as an argument.
"""
from __future__ import annotations

import asyncio
import time

from . import joblog
from .config import SETTINGS
from . import schedules
from .db import cursor

POLL_S = 12 * 3600          # metadata this stable does not need chasing

STATS: dict = {"last_run": 0.0, "next_run": 0.0, "updated": 0,
               "parents": 0, "last_error": ""}


def init() -> None:
    """Remember the per-title answer, not just the per-file one.

    Files come and go - an upgrade deletes the old row and inserts a new one -
    and the new row starts with no language. Without somewhere to keep the
    TITLE's answer, that file would have none until the next 12-hour sweep,
    which is a long time to be wrong about something that never changes.
    """
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS parent_lang(
                arr_name  TEXT NOT NULL,
                parent_id INTEGER NOT NULL,
                lang      TEXT NOT NULL,
                at        REAL,
                PRIMARY KEY (arr_name, parent_id))""")


def fill_missing() -> int:
    """Give newly-arrived rows the language their title already has.

    Cheap enough to run after every scan: one indexed UPDATE ... FROM against
    a table with a few thousand rows, touching only files that have no answer
    yet.
    """
    try:
        with cursor() as cur:
            cur.execute(
                "UPDATE files SET orig_lang = ("
                "  SELECT p.lang FROM parent_lang p "
                "   WHERE p.arr_name = files.arr_name "
                "     AND p.parent_id = files.arr_parent_id) "
                "WHERE COALESCE(orig_lang,'') = '' "
                "  AND arr_parent_id IS NOT NULL "
                "  AND EXISTS (SELECT 1 FROM parent_lang p "
                "               WHERE p.arr_name = files.arr_name "
                "                 AND p.parent_id = files.arr_parent_id)")
            return cur.rowcount
    except Exception:
        return 0


async def sync() -> dict:
    """Copy originalLanguage from each arr onto the files we hold."""
    from .arr import shared_client
    init()

    total_parents = updated = 0
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
            lang = ((it.get("originalLanguage") or {}).get("name") or "").strip()
            if not lang or it.get("id") is None:
                continue
            rows.append((lang, cfg.name, int(it["id"])))
        total_parents += len(rows)
        if not rows:
            continue

        def _write() -> int:
            n = 0
            now = time.time()
            with cursor() as cur:
                cur.executemany(
                    "INSERT INTO parent_lang(arr_name,parent_id,lang,at) "
                    "VALUES(?,?,?,?) ON CONFLICT(arr_name,parent_id) "
                    "DO UPDATE SET lang=excluded.lang, at=excluded.at",
                    [(a, p, l, now) for l, a, p in rows])
                for lang, arr_name, pid in rows:
                    # Only touch rows whose value actually differs, so a sync
                    # that changes nothing costs no writes and the updated
                    # count means something.
                    cur.execute(
                        "UPDATE files SET orig_lang=? "
                        " WHERE arr_name=? AND arr_parent_id=? "
                        "   AND COALESCE(orig_lang,'') != ?",
                        (lang, arr_name, pid, lang))
                    n += cur.rowcount
            return n

        updated += await asyncio.to_thread(_write)

    STATS.update(last_run=time.time(), updated=updated, parents=total_parents,
                 last_error="; ".join(errs), next_run=time.time() + POLL_S)
    if updated:
        joblog.log(f"original language: {updated:,} file(s) tagged from "
                   f"{total_parents:,} titles", "ok")
    elif errs:
        joblog.log(f"original language sync: {'; '.join(errs)}", "warn")
    return {"updated": updated, "parents": total_parents, "errors": errs}


# Provider language NAME -> the ISO codes ffprobe actually puts in a track tag.
# The providers answer in English words ("Japanese"); media files carry ISO
# 639-2/B, sometimes 639-1, occasionally a regional variant. Both halves have to
# be spelled out or the comparison silently never matches - which would look
# exactly like "this film has no original-language track" and quietly delete it.
LANG_CODES: dict[str, set[str]] = {
    "english": {"eng", "en"},
    "japanese": {"jpn", "ja", "jp"},
    "chinese": {"chi", "zho", "zh", "cmn", "yue", "zh-cn", "zh-tw"},
    "korean": {"kor", "ko"},
    "spanish": {"spa", "es", "es-mx", "es-es"},
    "french": {"fre", "fra", "fr"},
    "german": {"ger", "deu", "de"},
    "italian": {"ita", "it"},
    "portuguese": {"por", "pt", "pt-br"},
    "russian": {"rus", "ru"},
    "norwegian": {"nor", "no", "nob", "nb", "nno", "nn"},
    "swedish": {"swe", "sv"},
    "danish": {"dan", "da"},
    "finnish": {"fin", "fi"},
    "dutch": {"dut", "nld", "nl"},
    "polish": {"pol", "pl"},
    "turkish": {"tur", "tr"},
    "hindi": {"hin", "hi"},
    "arabic": {"ara", "ar"},
    "thai": {"tha", "th"},
    "vietnamese": {"vie", "vi"},
    "indonesian": {"ind", "id"},
    "hebrew": {"heb", "he", "iw"},
    "czech": {"cze", "ces", "cs"},
    "hungarian": {"hun", "hu"},
    "greek": {"gre", "ell", "el"},
    "ukrainian": {"ukr", "uk"},
    "romanian": {"rum", "ron", "ro"},
    "tagalog": {"tgl", "tl", "fil"},
    "malay": {"may", "msa", "ms"},
    "icelandic": {"ice", "isl", "is"},
    "catalan": {"cat", "ca"},
    "persian": {"per", "fas", "fa"},
    "tamil": {"tam", "ta"},
    "telugu": {"tel", "te"},
}


def codes_for(name: str | None) -> set[str]:
    """ISO codes that count as `name`. Empty when the name is unknown.

    Empty is meaningful: it tells the caller it has no usable answer, so the
    caller must fall back rather than treat "no match" as "no original track".
    """
    return set(LANG_CODES.get((name or "").strip().lower(), ()))


async def for_file(file_id: int) -> str:
    r"""The original language for ONE file, fetched if we do not have it.

    Used at processing time. The stored value covers everything the sweep has
    seen; this covers the gap for a file that arrived since - ask the arr about
    the one title that matters, one small request, and remember the answer for
    the whole title so the next episode of the same series costs nothing.

    Returns "" when it cannot be determined. The caller must treat that as
    "unknown" and fall back, never as "no original language".
    """
    from .arr import shared_client
    with cursor() as cur:
        r = cur.execute("SELECT orig_lang, arr_name, arr_parent_id FROM files "
                        "WHERE id=?", (file_id,)).fetchone()
    if not r:
        return ""
    if (r["orig_lang"] or "").strip():
        return r["orig_lang"].strip()
    arr_name, pid = r["arr_name"], r["arr_parent_id"]
    if not arr_name or pid is None:
        return ""
    # The title may already be known even though this row is not.
    try:
        with cursor() as cur:
            p = cur.execute("SELECT lang FROM parent_lang WHERE arr_name=? "
                            "AND parent_id=?", (arr_name, pid)).fetchone()
        if p and p["lang"]:
            _remember(arr_name, pid, p["lang"])
            return p["lang"]
    except Exception:
        pass
    cfg = next((c for c in SETTINGS.arrs
                if c.name == arr_name and c.enabled and c.api_key), None)
    if not cfg:
        return ""
    path = f"/series/{pid}" if cfg.kind == "sonarr" else f"/movie/{pid}"
    try:
        item = await shared_client(cfg)._get(path)
        lang = ((item.get("originalLanguage") or {}).get("name") or "").strip()
    except Exception as e:
        joblog.log(f"could not fetch original language from {arr_name} "
                   f"for title {pid}: {type(e).__name__}", "debug")
        return ""
    if lang:
        _remember(arr_name, pid, lang)
    return lang


def _remember(arr_name: str, pid: int, lang: str) -> None:
    try:
        init()
        with cursor() as cur:
            cur.execute("INSERT INTO parent_lang(arr_name,parent_id,lang,at) "
                        "VALUES(?,?,?,?) ON CONFLICT(arr_name,parent_id) "
                        "DO UPDATE SET lang=excluded.lang, at=excluded.at",
                        (arr_name, pid, lang, time.time()))
            cur.execute("UPDATE files SET orig_lang=? WHERE arr_name=? "
                        "AND arr_parent_id=? AND COALESCE(orig_lang,'') != ?",
                        (lang, arr_name, pid, lang))
    except Exception:
        pass


def coverage() -> dict:
    """How much of the library has an original language, and which."""
    with cursor() as cur:
        total = cur.execute(
            "SELECT COUNT(*) n FROM files WHERE state != 'deleted'").fetchone()["n"]
        have = cur.execute(
            "SELECT COUNT(*) n FROM files WHERE state != 'deleted' "
            "AND COALESCE(orig_lang,'') != ''").fetchone()["n"]
        by = [dict(r) for r in cur.execute(
            "SELECT orig_lang lang, COUNT(*) n FROM files "
            " WHERE state != 'deleted' AND COALESCE(orig_lang,'') != '' "
            " GROUP BY 1 ORDER BY n DESC LIMIT 12")]
    return {"total": total, "tagged": have, "by_language": by, "stats": STATS}


async def watch() -> None:
    await asyncio.sleep(300)
    while True:
        schedules.beat('origlang')
        try:
            await sync()
        except Exception as e:
            STATS["last_error"] = f"{type(e).__name__}: {e}"
            joblog.log(f"original-language sync failed: "
                       f"{type(e).__name__}: {e}", "error")
        await asyncio.sleep(POLL_S)
