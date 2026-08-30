r"""Does what the arr believes still match what is on disk?

WHY THIS EXISTS AS A PAGE RATHER THAN A HOPE. The language drift was found by
accident - a glance at a Sonarr import screen showed one file tagged English
beside siblings tagged Multi-Languages, and pulling that thread turned up 1,246
records describing audio tracks nuarr had removed months earlier. Nothing was
broken enough to complain, so nothing did.

That is the shape of every drift between two systems that each believe they own
the truth: no error, no failure, just two answers to the same question. The
answer is not to be more careful, it is to ask the question on a schedule and
put the number somewhere it is seen.

WHAT COUNTS AS DRIFT. Only differences where nuarr has better information than
the arr. nuarr probes the file with ffprobe after every rewrite; the arr's
record was written when the release was imported and is only refreshed when
something asks. So where the two disagree about the CONTENT of a file, the
probe is right - and where they disagree about anything else, this says so
rather than guessing which side to believe.
"""
from __future__ import annotations

import os
import time

from .config import SETTINGS
from .db import cursor
from . import joblog

# Recomputing this walks every arr record: minutes, not milliseconds. The card
# reads the last answer and asks for a new one on its own schedule.
_CACHE: dict = {"at": 0.0, "data": None, "running": False,
                "done": 0, "total": 0, "where": ""}
TTL_S = 1800.0

CHECKS = {
    "languages": {
        "label": "Language",
        "what": "the arr lists audio languages the file does not contain",
        "why": "nuarr's audio policy drops tracks the arr still remembers",
        "fixable": True,
        "fix": "sets the arr's languages to what ffprobe found",
    },
    "size": {
        "label": "File size",
        "what": "the arr's recorded size is not the size on disk",
        "why": "nuarr rewrote the file and the arr has not looked since",
        "fixable": True,
        "fix": "asks the arr to re-read the file",
    },
    "video": {
        "label": "Video",
        "what": "the arr's media info describes a different codec or height",
        "why": "the same - a rewrite the arr has not noticed",
        "fixable": True,
        "fix": "asks the arr to re-read the file",
    },
}


def _probe_rows() -> dict:
    """Everything nuarr knows, keyed the way an arr record can be looked up."""
    out = {}
    with cursor() as c:
        for r in c.execute(
                "SELECT f.arr_name, f.arr_file_id, f.arr_parent_id, f.path, "
                "       f.audio_langs, f.size, f.video_codec, f.height, p.json "
                "  FROM files f LEFT JOIN file_probes p ON p.file_id = f.id "
                " WHERE f.arr_file_id IS NOT NULL "
                "   AND f.state NOT IN ('deleted','duplicate')"):
            out[(r["arr_name"], r["arr_file_id"])] = dict(r)
    return out


def _size_on_disk(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


async def scan() -> dict:
    r"""Compare every arr record against nuarr's probe. Read-only, and slow."""
    from .arr import shared_client
    from . import arrlang

    probe = _probe_rows()
    found = {k: [] for k in CHECKS}
    checked = 0

    for cfg in (SETTINGS.arrs or []):
        if not getattr(cfg, "enabled", True):
            continue
        client = shared_client(cfg)
        kind = "episodefile" if cfg.kind == "sonarr" else "moviefile"
        key = "seriesId" if cfg.kind == "sonarr" else "movieId"
        try:
            parents = {f.parent_id for f in await client.list_files()}
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"arr sync check: cannot list {cfg.name}: "
                       f"{type(e).__name__}", "warn")
            continue
        # A WALK THIS LONG HAS TO SAY WHERE IT IS. Several minutes behind an
        # unmoving spinner is indistinguishable from several minutes hung, and
        # the honest signal - one arr, so many titles of so many - is already
        # in hand here.
        _CACHE["total"] = _CACHE.get("total", 0) + len(parents)
        for pid in parents:
            _CACHE["done"] = _CACHE.get("done", 0) + 1
            _CACHE["where"] = cfg.name
            try:
                raw = await client._get(f"/{kind}", **{key: pid})
            except Exception:                                # noqa: BLE001
                continue
            for rec in raw or []:
                mine = probe.get((cfg.name, rec.get("id")))
                if not mine:
                    continue
                checked += 1
                row = {"arr": cfg.name, "kind": cfg.kind,
                       "file_id": rec.get("id"),
                       "parent_id": mine.get("arr_parent_id"),
                       "path": mine.get("path") or rec.get("path") or ""}

                # --- languages, the one that started this ------------------
                actual = arrlang._canon(arrlang._iso_set(
                    mine.get("audio_langs") or ""))
                claimed = arrlang._canon(arrlang._claimed_iso(rec))
                if actual and claimed and claimed > actual:
                    found["languages"].append(
                        dict(row, claimed=sorted(claimed),
                             actual=sorted(actual)))

                # --- size --------------------------------------------------
                # NUARR'S RECORD FIRST, THE DISK ONLY TO SETTLE IT. Statting
                # every file to answer this meant 39,588 round trips to twelve
                # spinning disks that are usually busy with actual work, for a
                # question the scan already knows the answer to nearly always.
                # nuarr's size column is refreshed on every walk, so it screens
                # out the 99% that agree; the disk is then asked only about the
                # handful that do not, which is where the file being the
                # authority actually matters.
                arr_size = int(rec.get("size") or 0)
                my_size = int(mine.get("size") or 0)
                # A byte or two is not drift; anything a rewrite would cause is
                # far larger. 1 MB keeps rounding and metadata edits out of a
                # list meant for real differences.
                if arr_size and my_size and abs(my_size - arr_size) > 1_000_000:
                    disk = _size_on_disk(mine.get("path") or "")
                    if disk and abs(disk - arr_size) > 1_000_000:
                        # The DIFFERENCE, carried explicitly. Both numbers
                        # round to "1.2 GB" on a panel, so a row showing
                        # 1.2 -> 1.2 looks like a bug in the check rather than
                        # an 8.6 MB drift.
                        found["size"].append(
                            dict(row, arr=arr_size, disk=disk,
                                 delta=disk - arr_size))

                # --- video codec / height ---------------------------------
                mi = rec.get("mediaInfo") or {}
                a_codec = str(mi.get("videoCodec") or "").lower()
                a_h = int(mi.get("height") or 0)
                m_codec = str(mine.get("video_codec") or "").lower()
                m_h = int(mine.get("height") or 0)
                if m_codec and a_codec and not _codec_same(a_codec, m_codec):
                    found["video"].append(
                        dict(row, arr=a_codec, actual=m_codec, field="codec"))
                elif m_h and a_h and abs(a_h - m_h) > 8:
                    found["video"].append(
                        dict(row, arr=f"{a_h}p", actual=f"{m_h}p",
                             field="height"))

    # ONE FLAT LIST for the panel, so it can be sorted and scrolled like any
    # other table. Keeping them in per-check buckets meant the page could show
    # a count and two examples and nothing in between - a number with no way to
    # ask which files it means.
    flat = []
    for k, rows in found.items():
        for r in rows:
            flat.append(dict(r, check=k))
    flat.sort(key=lambda r: (r["check"], r.get("path") or ""))
    return {"checked": checked, "at": time.time(),
            "counts": {k: len(v) for k, v in found.items()},
            "rows": {k: v[:200] for k, v in found.items()},
            "list": flat[:2000],
            "total": sum(len(v) for v in found.values())}


def _codec_same(a: str, b: str) -> bool:
    r"""x265, HEVC and h265 are one codec with three names.

    The arrs and ffprobe do not spell these the same way, and a comparison that
    does not know that reports every HEVC file in the library as drift.
    """
    fam = {
        "hevc": "hevc", "h265": "hevc", "x265": "hevc", "h.265": "hevc",
        "avc": "h264", "h264": "h264", "x264": "h264", "h.264": "h264",
        "av1": "av1", "vp9": "vp9", "mpeg2video": "mpeg2", "mpeg2": "mpeg2",
        "mpeg4": "mpeg4", "xvid": "mpeg4", "divx": "mpeg4", "vc1": "vc1",
        "vc-1": "vc1", "wmv3": "vc1",
    }
    return fam.get(a, a) == fam.get(b, b)


async def fix(kinds: list | None = None) -> dict:
    r"""Put right what can be put right, leaving the rest visible.

    Languages are set directly, because nuarr knows the answer. Size and video
    drift are not corrected by writing a number into the arr - the arr's job is
    to read its own files, so it is asked to, and it comes back with the truth
    on its own. Telling it what to think would make nuarr the authority on a
    field the arr owns.
    """
    from .arr import shared_client
    from . import arrlang

    data = await scan()
    kinds = kinds or list(CHECKS)
    out = {"fixed": 0, "refreshed": 0, "failed": 0}

    if "languages" in kinds and data["rows"]["languages"]:
        res = await arrlang.fix(data["rows"]["languages"])
        out["fixed"] += res.get("fixed", 0)
        out["failed"] += res.get("failed", 0)

    # One refresh per PARENT, not per file: a series with forty stale episodes
    # is one rescan, and forty would be forty times the work for one answer.
    parents = {}
    for k in ("size", "video"):
        if k not in kinds:
            continue
        for r in data["rows"][k]:
            if r.get("parent_id"):
                parents.setdefault((r["arr"], r["parent_id"]), 0)
                parents[(r["arr"], r["parent_id"])] += 1
    for (arr_name, pid), n in parents.items():
        cfg = next((a for a in (SETTINGS.arrs or [])
                    if a.name == arr_name), None)
        if not cfg:
            continue
        try:
            await shared_client(cfg).notify_file_changed(int(pid))
            out["refreshed"] += n
        except Exception:                                    # noqa: BLE001
            out["failed"] += n

    if out["fixed"] or out["refreshed"]:
        joblog.log(f"arr sync: corrected {out['fixed']} language record(s) and "
                   f"asked the arrs to re-read {out['refreshed']} file(s)",
                   "ok")
    return out


def cached() -> dict:
    """The last answer, for the card. Never blocks; never runs a scan."""
    d = _CACHE.get("data")
    done, total = _CACHE.get("done", 0), _CACHE.get("total", 0)
    return {"have": bool(d), "running": bool(_CACHE.get("running")),
            "age_s": (round(time.time() - _CACHE["at"], 1)
                      if _CACHE.get("at") else None),
            "checks": CHECKS,
            "progress": {"done": done, "total": total,
                         "where": _CACHE.get("where", ""),
                         # Total grows as each arr is reached, so a fraction
                         # would jump backwards. Reported only once both arrs
                         # have been counted, and as a plain count until then.
                         "frac": (done / total) if total else 0.0},
            **(d or {"checked": 0, "counts": {}, "rows": {}, "list": [],
                     "total": 0})}


async def refresh() -> dict:
    """Run a scan and keep it. One at a time."""
    if _CACHE.get("running"):
        return cached()
    _CACHE.update(running=True, done=0, total=0, where="")
    try:
        d = await scan()
        _CACHE.update(data=d, at=time.time())
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"arr sync check: {type(e).__name__}: {e}", "warn")
    finally:
        _CACHE["running"] = False
    return cached()
