r"""Keep the arrs' language field agreeing with what is in the file.

THE MISMATCH, AND WHOSE FAULT IT IS. Sonarr records a file's languages from
the release it grabbed: a Netflix WEBDL of SmackDown ships seven audio tracks,
so the record says English, Spanish, Portuguese, French, Arabic, Hindi and
Italian. nuarr's audio policy then keeps the one track worth keeping and drops
the rest - and nothing tells the arr. The record goes on describing a file that
no longer exists in that form.

It is visible in the arr contradicting itself: `languages` lists seven while
its own `mediaInfo.audioLanguages` says `eng`. Measured across this library,
1,246 of 39,588 files - 3.1% - and every one of them in that direction, the arr
claiming languages the file does not have. That is nuarr's doing, so it is
nuarr's to put right.

GROUND TRUTH IS THE PROBE, not the arr's mediaInfo. Both usually agree, but one
of them is ffprobe reading the actual bytes and the other is a field the arr
populated at import; when they differ, the file wins.

THE SINGLE-FILE ENDPOINT SILENTLY IGNORES THIS. `PUT /episodefile/{id}` with a
languages array returns 200 and changes nothing - which is the worst kind of
API, because it looks like it worked. The bulk editor is the route that
actually applies it.
"""
from __future__ import annotations

import time

from .config import SETTINGS
from .db import cursor
from . import joblog

# Only ever narrows. A file that genuinely gained a language is not something
# this should be inventing, and an arr claiming FEWER languages than the file
# has is a different bug with a different fix.
_CACHE: dict = {"langs": {}, "at": 0.0}
_TTL = 3600.0


async def _lang_table(client) -> dict:
    r"""iso3 -> {id, name}, asked of the arr rather than written down here.

    Language ids are the arr's own and differ between installs and versions;
    a table baked in here would be right on this machine and quietly wrong on
    someone else's.
    """
    now = time.time()
    if _CACHE["langs"] and now - _CACHE["at"] < _TTL:
        return _CACHE["langs"]
    out = {}
    try:
        for l in (await client._get("/language")) or []:
            nm = (l.get("name") or "").strip()
            if nm:
                out[nm.lower()] = {"id": l.get("id"), "name": nm}
    except Exception:                                        # noqa: BLE001
        return _CACHE["langs"] or {}
    _CACHE.update(langs=out, at=now)
    return out


# ffprobe speaks ISO-639-2/B; the arrs speak English names. Only the ones that
# actually turn up in media - a complete table would be mostly dead weight.
_ISO2NAME = {
    "eng": "english", "spa": "spanish", "fre": "french", "fra": "french",
    "ger": "german", "deu": "german", "ita": "italian", "por": "portuguese",
    "ara": "arabic", "hin": "hindi", "jpn": "japanese", "kor": "korean",
    "chi": "chinese", "zho": "chinese", "dut": "dutch", "nld": "dutch",
    "pol": "polish", "rus": "russian", "tur": "turkish", "cze": "czech",
    "ces": "czech", "hun": "hungarian", "ind": "indonesian", "tam": "tamil",
    "tel": "telugu", "nor": "norwegian", "swe": "swedish", "dan": "danish",
    "fin": "finnish", "tha": "thai", "vie": "vietnamese", "gre": "greek",
    "ell": "greek", "heb": "hebrew", "rum": "romanian", "ron": "romanian",
    "ukr": "ukrainian", "bul": "bulgarian", "cat": "catalan",
    "hrv": "croatian", "slo": "slovak", "slk": "slovak", "slv": "slovenian",
    "srp": "serbian", "lit": "lithuanian", "lav": "latvian", "est": "estonian",
    "isl": "icelandic", "ice": "icelandic", "mal": "malayalam",
    "kan": "kannada", "ben": "bengali", "mar": "marathi", "pan": "punjabi",
    "fil": "filipino", "tgl": "tagalog", "may": "malay", "msa": "malay",
}


def _iso_set(audio_langs: str) -> set:
    """The probe's audio_langs column into a set of ISO codes worth comparing."""
    out = set()
    for x in (audio_langs or "").split(","):
        x = x.strip().lower()
        # '-' is nuarr's marker for an untagged track. Untagged is not a
        # language, and a file with one is a file whose truth we do not know -
        # those are left alone rather than narrowed to whatever else is there.
        if not x or x == "-":
            continue
        out.add(x)
    return out


def _claimed_iso(rec: dict) -> set:
    out = set()
    for l in (rec.get("languages") or []):
        nm = (l.get("name") or "").strip().lower()
        if not nm or nm in ("unknown", "any", "original"):
            continue
        for iso, name in _ISO2NAME.items():
            if name == nm:
                out.add(iso)
                break
    return out


def _canon(isos: set) -> set:
    """Fold the b/t spellings so fre and fra are not two different languages."""
    return {n for n in (_ISO2NAME.get(i) for i in isos) if n}


async def scan(limit: int = 0) -> dict:
    r"""Every arr record whose languages disagree with the file. Read-only."""
    with cursor() as c:
        probe = {}
        for r in c.execute(
                "SELECT arr_name, arr_file_id, audio_langs, path FROM files "
                " WHERE arr_file_id IS NOT NULL AND audio_langs IS NOT NULL "
                "   AND state NOT IN ('deleted','duplicate')"):
            probe[(r["arr_name"], r["arr_file_id"])] = (r["audio_langs"],
                                                        r["path"])
    rows, checked = [], 0
    from .arr import shared_client
    for cfg in (SETTINGS.arrs or []):
        if not getattr(cfg, "enabled", True):
            continue
        client = shared_client(cfg)
        kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
        key = "seriesId" if cfg.kind == "sonarr" else "movieId"
        try:
            parents = {f.parent_id for f in await client.list_files()}
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"language check: could not list {cfg.name}: "
                       f"{type(e).__name__}", "warn")
            continue
        for pid in parents:
            try:
                raw = await client._get(f"/{kind}", **{key: pid})
            except Exception:                                # noqa: BLE001
                continue
            for rec in raw or []:
                hit = probe.get((cfg.name, rec.get("id")))
                if not hit:
                    continue
                checked += 1
                actual = _iso_set(hit[0])
                claimed = _claimed_iso(rec)
                if not actual or not claimed:
                    continue
                a, c2 = _canon(actual), _canon(claimed)
                # ONLY WHERE THE ARR CLAIMS MORE. The other direction means the
                # file has a language the arr never knew about, which is a
                # different situation and not one to "fix" by guessing.
                if a and c2 > a:
                    rows.append({"arr": cfg.name, "kind": cfg.kind,
                                 "file_id": rec.get("id"),
                                 "path": rec.get("path") or hit[1],
                                 "claimed": sorted(c2), "actual": sorted(a)})
                    if limit and len(rows) >= limit:
                        return {"checked": checked, "rows": rows,
                                "truncated": True}
    return {"checked": checked, "rows": rows, "truncated": False}


async def fix_one(cfg, arr_file_id: int, file_id: int) -> bool:
    r"""Correct one file's languages, straight after the job that changed it.

    THE SWEEP IS THE BACKSTOP, NOT THE MECHANISM. Correcting drift once a day
    means a day of records describing tracks that are gone, and a sweep that
    grows with the library. The job that dropped the track knows which file and
    knows what is left; doing it here costs one API call on a rewrite that has
    already spent minutes, and the sweep then only ever finds what this missed.

    Quiet on every failure: an arr that will not take a metadata correction is
    not a reason to mark a finished, committed, verified rewrite as failed.
    """
    if not arr_file_id or not file_id:
        return False
    with cursor() as c:
        row = c.execute("SELECT audio_langs FROM files WHERE id=?",
                        (file_id,)).fetchone()
    actual = _canon(_iso_set(row["audio_langs"] if row else ""))
    if not actual:
        return False
    from .arr import shared_client
    client = shared_client(cfg)
    ep = cfg.kind == "sonarr"
    try:
        rec = await client._get(
            f"/{'episodefile' if ep else 'moviefile'}/{int(arr_file_id)}")
    except Exception:                                        # noqa: BLE001
        return False
    claimed = _canon(_claimed_iso(rec))
    # Only narrow, and only when there is something to narrow - the same rule
    # the sweep follows, for the same reason.
    if not claimed or not claimed > actual:
        return False
    table = await _lang_table(client)
    langs = [{"id": table[n]["id"], "name": table[n]["name"]}
             for n in sorted(actual) if n in table]
    if not langs:
        return False
    try:
        await client._put(
            "/episodefile/editor" if ep else "/moviefile/editor",
            {("episodeFileIds" if ep else "movieFileIds"): [int(arr_file_id)],
             "languages": langs})
    except Exception:                                        # noqa: BLE001
        return False
    joblog.log(f"corrected the arr's language field to "
               f"{', '.join(sorted(actual))} - it still listed "
               f"{', '.join(sorted(claimed))}", "info")
    return True


async def fix(rows: list, on_chunk=None) -> dict:
    r"""Set each record's languages to what the file actually contains.

    Batched through the arr's bulk editor, which is the only route that
    applies a language change - the per-file PUT accepts it and does nothing.

    on_chunk(file_ids, ok) is called after each batch is ACCEPTED OR REFUSED,
    so a caller can report progress and retire the corrected rows while the
    run is still going. Reported per batch rather than per file because the
    batch is the unit the arr actually confirms - claiming a file was
    corrected before the call that corrects it has returned would be a
    progress bar that lies.
    """
    from .arr import shared_client
    done, failed = 0, 0
    by_arr: dict = {}
    for r in rows:
        by_arr.setdefault((r["arr"], r["kind"]), []).append(r)
    for (arr_name, kind), group in by_arr.items():
        cfg = next((a for a in (SETTINGS.arrs or [])
                    if a.name == arr_name), None)
        if not cfg:
            continue
        client = shared_client(cfg)
        table = await _lang_table(client)
        ep = kind == "sonarr"
        path = "/episodefile/editor" if ep else "/moviefile/editor"
        idkey = "episodeFileIds" if ep else "movieFileIds"
        # One call per distinct language set, not per file: the editor takes a
        # list of ids, and 1,246 files collapse into a handful of shapes.
        buckets: dict = {}
        for r in group:
            buckets.setdefault(tuple(r["actual"]), []).append(r["file_id"])
        for want, ids in buckets.items():
            langs = []
            for name in want:
                ent = table.get(name)
                if ent:
                    langs.append({"id": ent["id"], "name": ent["name"]})
            if not langs:
                failed += len(ids)
                if on_chunk:
                    on_chunk(ids, False)
                continue
            for i in range(0, len(ids), 100):
                chunk = ids[i:i + 100]
                ok = True
                try:
                    await client._put(path, {idkey: chunk, "languages": langs})
                    done += len(chunk)
                except Exception as e:                       # noqa: BLE001
                    ok = False
                    failed += len(chunk)
                    joblog.log(f"language fix on {arr_name}: "
                               f"{type(e).__name__}: {str(e)[:120]}", "warn")
                if on_chunk:
                    # Never let a reporting callback take down a correction
                    # that has already been applied.
                    try:
                        on_chunk(chunk, ok)
                    except Exception:                        # noqa: BLE001
                        pass
    if done or failed:
        joblog.log(f"corrected the language on {done} arr record(s) to match "
                   f"the file" + (f", {failed} failed" if failed else ""),
                   "ok" if not failed else "warn")
    return {"fixed": done, "failed": failed}
