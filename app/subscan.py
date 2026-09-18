r"""nuarr - everything true about one file's subtitles, in one row.

WHY A FACT TABLE AND NOT THREE WALKS. Until now three systems each walked the
library for their own half of the same question. The sidecar sweep listed
every folder looking for loose .srt and .ass files. The duplicate sweep parsed
every stored probe looking for a language twice. The picture reader kept its
own table of what it had sampled. Three passes over forty thousand files, three
caches with three lifetimes, three progress bars that could not be added up -
and no single place you could point at and ask "what subtitles does this file
have?".

This is that place. One row per file, holding everything that is TRUE about
its subtitles and nothing about what should be done with them:

    tracks   - what is inside, from the stored probe: language, kind, title,
               and where the line count is already known, that too
    sides    - what is sitting beside it on disk: language, role, format,
               size, and whether the name can be read at all
    picture  - whether the words are painted into the frames, as the picture
               reader last measured it

WHAT IS TRUE AND WHAT SHOULD HAPPEN ARE DIFFERENT QUESTIONS, and keeping them
apart is the whole design. A fact is expensive - it costs a folder listing and
a probe parse - and it changes only when the file changes. An instruction is
cheap and changes every time you touch the Subtitle rules panel. Mixing them
is why a rule change used to mean re-walking the library: the rules were being
re-applied by the same pass that was gathering the evidence. Here the rules
change, subplan re-reads these rows, and no disk is touched at all.

COST, MEASURED. The probe parse is a database read. The picture verdict is a
database read. The folder listing is the only thing that touches a disk, and
it is the same listdir the sidecar sweep has always done - about three hundred
files a second once the directory entries are in the OS cache, which is where
they are after the first pass. A full scan of this library is a couple of
minutes, and after that only files that have changed are re-read.

WHEN A ROW GOES STALE. The file's size or mtime moved, the library rescanned
it, or the row is simply old - MAX_AGE_S, because a sidecar can appear beside
a file without the file itself changing in any way nuarr can see, and a
subtitle that shows up at midnight should not wait for the video to be
touched. Nothing else invalidates a row: the rules have no bearing on it,
which is the point.
"""
from __future__ import annotations

import json
import os
import time

import subprocess

from .config import SETTINGS
from .db import cursor

KEY = "subscan"
TITLE = "Reading what every file carries"

# A row older than this is re-read even if the file has not moved, because a
# sidecar can land next to an untouched video.
MAX_AGE_S = 6 * 3600.0
# How many files one pass will read before it lets go. The runner paces the
# passes; this only stops a single pass from holding the thread for an hour.
BATCH = 4000
# AND HOW MANY OF THOSE IT MAY OPEN. Reading a cached probe is a database
# read; fetching one is a process and a seek on a pool disk that may be spun
# down. This is the only part of the sweep that costs anything, so it is
# capped per pass - the rest of the batch still gets read from cache, and the
# next pass takes the next few. At this rate a library with twenty thousand
# unopened files clears in a few hours of idle time.
PROBE_PER_PASS = 150
PROBE_TIMEOUT_S = 60.0

_READY = False

STATE: dict = {"running": False, "t0": 0.0, "done": 0, "total": 0,
               "last": "", "last_run": 0.0, "took": 0.0, "err": "",
               "written": 0,
               # WHAT THE LAST FILE YIELDED, not just its name. A filename on
               # its own says the sweep is alive and nothing about what it
               # learned, which is the question a person watching it has.
               "last_found": {},
               # When the next pass is due, set by the loop that runs it -
               # subqueue._reader_and_feeder. It is gated on the box being
               # idle, so this is "not before", never a promise.
               "next_at": 0.0, "gated": False,
               # How many files this pass actually OPENED, as opposed to read
               # from cache. The number that has to move for "read inside" to
               # move - see _open_it.
               "opened": 0, "opened_total": 0}


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sub_facts(
                file_id   INTEGER PRIMARY KEY,
                path      TEXT    NOT NULL DEFAULT '',
                library   TEXT    NOT NULL DEFAULT '',
                disk      TEXT    NOT NULL DEFAULT '',
                mtime     REAL    NOT NULL DEFAULT 0,
                size      INTEGER NOT NULL DEFAULT 0,
                tracks    TEXT    NOT NULL DEFAULT '[]',
                sides     TEXT    NOT NULL DEFAULT '[]',
                picture   TEXT    NOT NULL DEFAULT '{}',
                n_tracks  INTEGER NOT NULL DEFAULT 0,
                n_sides   INTEGER NOT NULL DEFAULT 0,
                scanned_at REAL   NOT NULL DEFAULT 0,
                err       TEXT    NOT NULL DEFAULT '',
                -- DID ANYBODY ACTUALLY LOOK INSIDE? Without this, n_tracks=0
                -- means both "counted, there are none" and "no probe was
                -- cached, so the list came back empty" - and the planner
                -- cannot tell which. See the note on _tracks_of.
                probed    INTEGER NOT NULL DEFAULT 0
            )""")
        # The column arrived after the table; add it where it is missing.
        try:
            cur.execute("ALTER TABLE sub_facts ADD COLUMN "
                        "probed INTEGER NOT NULL DEFAULT 0")
        except Exception:                                        # noqa: BLE001
            pass                                  # already there
        # BACKFILL. A cached probe proves it was read. So does a non-empty
        # track list - that can only have come from a probe, even if the probe
        # itself has since been pruned. Everything else is honestly unknown
        # and the next sweep will settle it.
        try:
            cur.execute(
                "UPDATE sub_facts SET probed=1 "
                " WHERE probed=0 AND (n_tracks > 0 "
                "    OR file_id IN (SELECT file_id FROM file_probes))")
        except Exception:                                        # noqa: BLE001
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS ix_sub_facts_at "
                    "ON sub_facts(scanned_at)")
        # The planner asks "which files have something beside them or twice
        # inside them" far more often than it asks about one file, and a
        # table scan of forty thousand JSON blobs to answer it is the kind of
        # thing that makes a page feel slow for no reason anybody can see.
        cur.execute("CREATE INDEX IF NOT EXISTS ix_sub_facts_n "
                    "ON sub_facts(n_sides, n_tracks)")
    _READY = True


# --------------------------------------------------------------- the facts --
def _open_it(file_id: int, path: str) -> bool:
    r"""Fetch a probe for a file that has none, and cache it. True if it landed.

    THE ONLY PART OF THIS SWEEP THAT OPENS A FILE, and the reason the read
    count can move at all. Without it scan_one records "could not look inside"
    for every file whose probe is not cached, forever - the sweep visits them
    again next pass and learns the same nothing.

    Through jobs' own ffprobe and jobs.cache_probe so this writes exactly the
    probe a transcode would have written: one cache, one shape, one owner.
    Synchronous because scan() runs on a thread via jobs.in_work, where there
    is no loop to await jobs.probe() on.
    """
    try:
        from . import jobs
        if not path or not os.path.exists(path):
            return False
        r = subprocess.run(
            [jobs._ffprobe_exe(), "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, timeout=PROBE_TIMEOUT_S,
            creationflags=getattr(jobs, "NO_WINDOW", 0))
        if r.returncode != 0:
            return False
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        if not data.get("streams"):
            return False
        # changed=False: this file has not moved, its probe simply aged out.
        # cache_probe's invalidations exist for a file that was REWRITTEN, and
        # firing them here threw away OCR shape verdicts that were still true
        # - measured, sub_shape fell 195 -> 179 as this sweep ran.
        jobs.cache_probe(int(file_id), data, changed=False)
        return True
    except Exception:                                            # noqa: BLE001
        # A file that will not probe is not a failure of the sweep - it is
        # recorded as unread and offered again, which is what it is.
        return False


def _probed(file_id: int, cur) -> bool:
    """Has anybody actually looked inside this file?

    The one question that separates "it carries no subtitles" from "nuarr has
    never opened it". They were the same empty list until this existed.
    """
    return bool(cur.execute("SELECT 1 FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone())


def _tracks_of(file_id: int, cur, size: int | None = None) -> list:
    """What is inside, from the stored probe, plus any line count known.

    RETURNS [] FOR TWO DIFFERENT REASONS, which is why the caller records
    _probed() alongside this: no probe cached, or a probe that lists no
    subtitle streams. Only the second one means the file has no subtitles.
    """
    from .subembed import _lang_key, _track_class
    out: list = []
    r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                    (int(file_id),)).fetchone()
    if not r:
        return out
    try:
        streams = json.loads(r["json"]).get("streams") or []
    except Exception:                                            # noqa: BLE001
        return out
    n = 0
    for st in streams:
        if st.get("codec_type") != "subtitle":
            continue
        tags = st.get("tags") or {}
        disp = st.get("disposition") or {}
        title = (tags.get("title") or "").strip()
        out.append({
            "ord": n,
            "lang": _lang_key(tags.get("language") or "und"),
            "title": title,
            "codec": (st.get("codec_name") or "").lower(),
            "forced": bool(disp.get("forced")),
            "default": bool(disp.get("default")),
            "class": _track_class(title, bool(disp.get("forced"))),
            # NUMBER_OF_FRAMES on a subtitle stream is its cue count, and it
            # is already in the probe - so the commonest reason to open a file
            # is answered without opening it.
            "cues": int((tags.get("NUMBER_OF_FRAMES")
                         or tags.get("NUMBER_OF_FRAMES-eng") or 0) or 0),
            "events": -1,
        })
        n += 1
    # Where somebody has actually extracted and counted a track, that number
    # beats the header's - the header can be absent, and on a remuxed file it
    # can be left over from the container it came from.
    #
    # ONLY IF IT WAS COUNTED IN THIS FILE. shape_of() guards its own cache
    # with `WHERE size=?` because a rewrite invalidates a count; this read had
    # no such test, so counts taken before nuarr's own remux were still being
    # applied after it. Measured: 158 of 675 shape rows that still join to a
    # live file - 23% - record a size the file no longer has. Dropping a
    # duplicate track rewrites the container, which both shrinks the file and
    # RENUMBERS the tracks that remain, so a stale row either lands its count
    # on a different track than the one it was counted in, or points past the
    # end. The first is silent and feeds the sweep that decides which copy to
    # keep by comparing counts.
    #
    # Left at -1 when it does not match, which is the value meaning "nobody
    # has counted this" that every caller already handles.
    try:
        for s in cur.execute(
                "SELECT track, events, chosen, size FROM subtitle_shape "
                " WHERE file_id=?", (int(file_id),)):
            # ONE-BASED IN THE TABLE, zero-based in this list.
            #
            # subtitletitle._rows_from_probe increments before it stores -
            # "# mkvpropedit numbers subtitle tracks from 1" - because the
            # same number addresses mkvpropedit's s1/s2. Read as an index
            # into `out` it pointed one track too far: measured, 538 of the
            # rows with a live probe match the 1-based reading and none match
            # the 0-based one, and the table holds no track=0 at all. So
            # every count was landing on the NEXT track, and the last track's
            # count fell past the end and was dropped - which is why a row
            # saying track=2 on a two-subtitle file looked like nonsense.
            i = int(s["track"]) - 1
            if not (0 <= i < len(out)):
                continue
            fresh = (size is None
                     or int(s["size"] or 0) == int(size or 0))
            if fresh and s["events"] is not None:
                out[i]["events"] = int(s["events"])
            # `chosen` is YOUR answer about that track, not a measurement, so
            # it is not invalidated by the file changing size.
            if s["chosen"]:
                out[i]["chosen"] = str(s["chosen"])
    except Exception:                                            # noqa: BLE001
        pass
    # AND WHAT THE PICTURE READER MADE OF A PICTURE TRACK - the last fact that
    # lived outside this row. sub_shape answers "is this picture track typeset
    # signs, or dialogue", measured from glyph height while the OCR reader had
    # the track open.
    #
    # `rel` is ffmpeg's subtitle selector: subocr maps the stream with
    # `-map 0:s:{rel}`, so it counts subtitle streams from zero - the same
    # index as `ord` here. Checked against the data before being relied on -
    # 184 of 192 rows are rel=0, which a one-based counter cannot produce -
    # because the equivalent number in subtitle_shape turned out to be
    # one-based and had been read as zero-based for months.
    #
    # No freshness test, and that is not an oversight: subocr.forget_shapes()
    # DELETES these when a file is re-probed after a rewrite, so a row that is
    # here is a row about this version of the file. A second opinion about
    # freshness is how the bugs this week started.
    try:
        for s in cur.execute(
                "SELECT rel, typeset, median_h, tall_share "
                "  FROM sub_shape WHERE file_id=?", (int(file_id),)):
            i = int(s["rel"])
            if not (0 <= i < len(out)):
                continue
            out[i]["typeset"] = bool(s["typeset"])
            if s["median_h"] is not None:
                out[i]["median_h"] = float(s["median_h"])
            if s["tall_share"] is not None:
                out[i]["tall_share"] = float(s["tall_share"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _sides_of(path: str) -> list:
    """What is sitting beside it, named as far as the name can be read."""
    from . import subembed
    out: list = []
    for side in subembed.sidecars_for(path, fresh=True):
        name = subembed.read_sidecar_name(path, side)
        try:
            sz = os.path.getsize(side)
        except OSError:
            sz = 0
        out.append({
            "path": side,
            "name": os.path.basename(side),
            "ext": os.path.splitext(side)[1].lstrip(".").lower(),
            "lang": name.get("lang") or "",
            "role": name.get("role") or "",
            "ok": bool(name.get("ok")),
            "why": name.get("why") or "",
            "size": int(sz),
        })
    return out


def _picture_of(file_id: int, cur) -> dict:
    """What the picture reader last said about this file. A read, not a read."""
    try:
        r = cur.execute("SELECT state, chosen, marked, low_hits, high_hits, "
                        "       samples, words, at FROM hardsub WHERE file_id=?",
                        (int(file_id),)).fetchone()
    except Exception:                                            # noqa: BLE001
        return {}
    if not r:
        return {}
    lo = int(r["low_hits"] or 0)
    hi = int(r["high_hits"] or 0)
    n = int(r["samples"] or 0)
    return {"state": (r["chosen"] or r["state"] or ""),
            "by_hand": bool(r["chosen"]),
            "marked": bool(r["marked"]),
            "sure": int(round(100.0 * hi / n)) if n else 0,
            "words": (r["words"] or "")[:200],
            "at": float(r["at"] or 0.0)}


def scan_one(file_id: int, row=None) -> dict:
    """Read one file's subtitle facts and write the row. Returns the facts."""
    init()
    with cursor() as cur:
        if row is None:
            row = cur.execute(
                "SELECT id, path, library, pool_disk, mtime, size "
                "  FROM files WHERE id=?", (int(file_id),)).fetchone()
        if not row:
            return {}
        path = row["path"] or ""
        d = {"file_id": int(file_id), "path": path,
             "library": row["library"] or "", "disk": row["pool_disk"] or "",
             "mtime": float(row["mtime"] or 0.0), "size": int(row["size"] or 0),
             "tracks": [], "sides": [], "picture": {}, "err": "",
             "probed": 0}
        try:
            # ORDER MATTERS ONLY IN THAT BOTH ARE READ. An empty track list
            # from an unprobed file is not a fact about the file; `probed`
            # is what lets the planner tell that from a real absence.
            d["probed"] = 1 if _probed(int(file_id), cur) else 0
            d["tracks"] = _tracks_of(int(file_id), cur, d["size"])
            d["picture"] = _picture_of(int(file_id), cur)
        except Exception as e:                                   # noqa: BLE001
            d["err"] = f"{type(e).__name__}: {e}"[:180]
    # THE ONE PART THAT TOUCHES A DISK, and it is outside the cursor on
    # purpose: a listdir on a spun-down pool disk can take a second, and
    # holding a database connection across it would block every other reader
    # for that second.
    try:
        d["sides"] = _sides_of(path) if path else []
    except Exception as e:                                       # noqa: BLE001
        d["err"] = (d["err"] or f"{type(e).__name__}: {e}"[:180])
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO sub_facts(file_id,path,library,disk,mtime,size,"
                "  tracks,sides,picture,n_tracks,n_sides,scanned_at,err,"
                "  probed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path,"
                "  library=excluded.library, disk=excluded.disk,"
                "  mtime=excluded.mtime, size=excluded.size,"
                "  tracks=excluded.tracks, sides=excluded.sides,"
                "  picture=excluded.picture, n_tracks=excluded.n_tracks,"
                "  n_sides=excluded.n_sides, scanned_at=excluded.scanned_at,"
                "  err=excluded.err, probed=excluded.probed",
                (int(file_id), d["path"], d["library"], d["disk"], d["mtime"],
                 d["size"], json.dumps(d["tracks"]), json.dumps(d["sides"]),
                 json.dumps(d["picture"]), len(d["tracks"]), len(d["sides"]),
                 time.time(), d["err"], int(d["probed"])))
    except Exception as e:                                       # noqa: BLE001
        d["err"] = f"{type(e).__name__}: {e}"[:180]
    # WHAT THIS ONE CAME BACK WITH. Recorded for every file, not only inside a
    # batch, so "last read" on the panel is always the most recent thing that
    # actually happened rather than the last thing a sweep happened to touch.
    try:
        STATE["last_found"] = {
            "name": os.path.basename(path),
            "tracks": len(d["tracks"]), "sides": len(d["sides"]),
            "picture": (d["picture"] or {}).get("state") or "",
            "probed": int(d["probed"] or 0),
            "library": d["library"], "err": d["err"], "at": time.time()}
    except Exception:                                            # noqa: BLE001
        pass
    return d


# ---------------------------------------------------------------- the pass --
def _live_where() -> str:
    return ("f.state NOT IN ('deleted','duplicate') "
            "AND COALESCE(f.path,'') != ''")


def _stale_sql(limit: int) -> tuple:
    r"""Files with no row, or a row that no longer describes them.

    NEVER SCANNED comes first, because a page that says "18,000 of 39,000" is
    only useful if the number is going up through material nobody has looked
    at yet. Re-reads of rows that are merely old come after that, oldest
    first.
    """
    cutoff = time.time() - MAX_AGE_S
    return ("""
        SELECT f.id, f.path, f.library, f.pool_disk, f.mtime, f.size,
               s.scanned_at AS sat
          FROM files f LEFT JOIN sub_facts s ON s.file_id = f.id
         WHERE """ + _live_where() + """
           AND (s.file_id IS NULL
                OR COALESCE(s.mtime,-1) != COALESCE(f.mtime,0)
                OR COALESCE(s.size,-1)  != COALESCE(f.size,0)
                -- NOBODY HAS OPENED IT YET. Without this a row saying "could
                -- not look inside" counts as finished the moment it is
                -- written, so the files the sweep most needs to get to were
                -- the only ones it never offered - and the opener had nothing
                -- to open. Self-pacing: scan_one stamps scanned_at whether
                -- the open worked or not, and the order below is
                -- oldest-first, so a file that will never probe goes to the
                -- back of the queue rather than holding the head of it.
                OR COALESCE(s.probed,0) = 0
                OR COALESCE(s.scanned_at,0) < ?)
         ORDER BY (s.file_id IS NOT NULL), COALESCE(s.scanned_at,0), f.id
         LIMIT ?""", (cutoff, int(limit)))


def pending(limit: int = BATCH) -> list:
    """What still has to be read, oldest and never-read first."""
    init()
    sql, args = _stale_sql(limit)
    try:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(sql, args)]
    except Exception:                                            # noqa: BLE001
        return []


_COUNTS: dict = {"at": 0.0, "data": None}


def counts() -> dict:
    r"""How many files there are, how many have been read, how many are left.

    ONE QUERY, THREE NUMBERS. Asking separately would let them disagree - the
    library grows while the page is drawing - and "19,002 of 19,001" is the
    kind of thing that makes a person stop believing the whole panel.

    HELD FOR TEN SECONDS. The query joins forty thousand files to their facts
    and measured 1.5 s; the Subtitles page polls every five. The reader
    updates its own STATE per file, so the bar still moves - this is only the
    denominator, which changes when the library does.
    """
    now = time.time()
    if _COUNTS["data"] is not None and now - _COUNTS["at"] < 10:
        return dict(_COUNTS["data"])
    out = _counts_now()
    _COUNTS.update(at=now, data=dict(out))
    return out


def _counts_now() -> dict:
    init()
    cutoff = time.time() - MAX_AGE_S
    out = {"total": 0, "counted": 0, "left": 0, "fresh": 0, "errors": 0,
           # WHAT IT IS ESTABLISHING, not just how far it has got. These are
           # the four things scan_one reads for every file, and they come off
           # the SAME query as the totals above so a part can never exceed its
           # whole - see the note on counts().
           "found": {"tracks": 0, "sides": 0, "picture": 0, "unread": 0,
                     "bare": 0}}
    try:
        with cursor() as cur:
            r = cur.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN s.file_id IS NOT NULL THEN 1 ELSE 0 END)
                           AS counted,
                       -- FRESH MEANS THE SWEEP HAS NOTHING LEFT TO DO WITH
                       -- IT, which now includes having opened it - the same
                       -- test _stale_sql uses, or the panel would say "all
                       -- rows current" while the sweep chewed through
                       -- thousands of unopened files.
                       SUM(CASE WHEN s.file_id IS NOT NULL
                                 AND COALESCE(s.mtime,-1) = COALESCE(f.mtime,0)
                                 AND COALESCE(s.size,-1)  = COALESCE(f.size,0)
                                 AND COALESCE(s.probed,0) = 1
                                 AND COALESCE(s.scanned_at,0) >= ?
                                THEN 1 ELSE 0 END) AS fresh,
                       SUM(CASE WHEN COALESCE(s.err,'') != '' THEN 1 ELSE 0 END)
                           AS errors,
                       SUM(CASE WHEN COALESCE(s.n_tracks,0) > 0
                                THEN 1 ELSE 0 END) AS n_tracks,
                       SUM(CASE WHEN COALESCE(s.n_sides,0) > 0
                                THEN 1 ELSE 0 END) AS n_sides,
                       SUM(CASE WHEN COALESCE(s.picture,'{}') NOT IN ('{}','')
                                THEN 1 ELSE 0 END) AS n_pic,
                       -- Read, and carrying nothing at all. Only meaningful
                       -- BECAUSE `probed` exists: without it this number
                       -- silently included every file nobody had opened.
                       SUM(CASE WHEN s.file_id IS NOT NULL
                                 AND COALESCE(s.probed,0) = 1
                                 AND COALESCE(s.n_tracks,0) = 0
                                 AND COALESCE(s.n_sides,0) = 0
                                THEN 1 ELSE 0 END) AS n_bare,
                       SUM(CASE WHEN s.file_id IS NOT NULL
                                 AND COALESCE(s.probed,0) = 0
                                THEN 1 ELSE 0 END) AS n_unread
                  FROM files f LEFT JOIN sub_facts s ON s.file_id = f.id
                 WHERE """ + _live_where(), (cutoff,)).fetchone()
        out["total"] = int(r["total"] or 0)
        out["counted"] = int(r["counted"] or 0)
        out["fresh"] = int(r["fresh"] or 0)
        out["errors"] = int(r["errors"] or 0)
        out["left"] = max(0, out["total"] - out["fresh"])
        out["found"] = {"tracks": int(r["n_tracks"] or 0),
                        "sides": int(r["n_sides"] or 0),
                        "picture": int(r["n_pic"] or 0),
                        "bare": int(r["n_bare"] or 0),
                        "unread": int(r["n_unread"] or 0)}
        out["read_inside"] = max(0, out["counted"] - out["found"]["unread"])
        # PER LIBRARY, because one library sitting at 2% read is invisible in
        # a single figure for forty thousand files. Same live filter, same
        # pass, so these add up to the totals above.
        with cursor() as cur2:
            out["by_library"] = [
                {"library": (x["library"] or "?"),
                 "total": int(x["n"] or 0),
                 "read": int(x["rd"] or 0)}
                for x in cur2.execute(
                    "SELECT f.library AS library, COUNT(*) AS n,"
                    "       SUM(CASE WHEN COALESCE(s.probed,0)=1"
                    "                THEN 1 ELSE 0 END) AS rd"
                    "  FROM files f LEFT JOIN sub_facts s ON s.file_id = f.id"
                    " WHERE " + _live_where() +
                    " GROUP BY f.library ORDER BY n DESC")]
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _readers(out_left: int, running: bool) -> list:
    """One line per subtitle reader: what it reads, and how much is left."""
    rows = [{"key": "facts", "name": "the row for each file",
             "what": "the stored probe, the folder beside it, and what the "
                     "picture reader already decided - no disk beyond one "
                     "directory listing",
             "left": int(out_left), "running": bool(running)}]
    try:
        from . import hardsub
        rows.append({
            "key": "picture", "name": "the picture",
            "what": f"{hardsub.SAMPLES} frames sampled off the disk and the "
                    f"brightest shown to the OCR, for files that report no "
                    f"subtitle track - this is the one that finds words "
                    f"burned into the image",
            "left": int(hardsub.untested() or 0),
            "running": bool((hardsub.STATE or {}).get("running")),
            "goto": "/settings#hardsub"})
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import subtitletitle as _stt
        d = (_stt._CACHE.get("data") or {})
        rows.append({
            "key": "events", "name": "a track's events",
            "what": "the actual lines of a text track whose title contradicts "
                    "its cue count, demuxed and counted - the only way to "
                    "tell karaoke from dialogue",
            "left": int(d.get("unread") or 0),
            "running": bool((_stt.INSPECT_STATE or {}).get("running")),
            "goto": "/settings#subtitletitle"})
    except Exception:                                            # noqa: BLE001
        pass
    return rows


def progress() -> dict:
    """The scan's own state, for the panel. Counted, left, rate, how long."""
    c = counts()
    st = dict(STATE)
    el = (time.time() - st["t0"]) if st["running"] and st["t0"] else 0.0
    rate = (st["done"] / el) if el > 0.5 else 0.0
    return {**c, "running": st["running"], "done_this_pass": st["done"],
            "of_this_pass": st["total"], "last": st["last"],
            # What one file costs, so the panel can say what it is doing per
            # file rather than only per library.
            "reads": ["the probe for its subtitle tracks",
                      "the folder beside it for loose subtitle files",
                      "the picture reader's verdict",
                      "any line count already counted for a track"],
            # THE OTHER TWO READERS, so one place answers "is anything
            # still being read". They do different work on different clocks -
            # this one reads stored facts, the picture reader samples frames
            # off a disk, the events reader demuxes a track - and that is why
            # they are three passes rather than one. It is not a reason for
            # their PROGRESS to live in three panels: that is how the page
            # came to say "every file looked at" in one place while another
            # showed three thousand waiting.
            #
            # Each number comes from the reader that owns it. A count
            # computed twice is a count that will eventually disagree with
            # itself.
            "readers": _readers(out_left=max(0, int(c.get("left") or 0)),
                                running=bool(st["running"])),
            "batch": BATCH, "max_age_s": MAX_AGE_S,
            "last_found": dict(st.get("last_found") or {}),
            "opened": int(st.get("opened") or 0),
            "opened_total": int(st.get("opened_total") or 0),
            "probe_per_pass": PROBE_PER_PASS,
            "next_at": st.get("next_at") or 0.0,
            "gated": bool(st.get("gated")),
            "elapsed": round(el, 1), "rate": round(rate, 1),
            "eta": round(c["left"] / rate) if rate > 0.05 else 0,
            "last_run": st["last_run"], "took": round(st["took"], 1),
            "err": st["err"], "written": st["written"]}


def scan(limit: int = BATCH, on_each=None) -> dict:
    r"""Read everything that is stale, writing a row per file as it goes.

    ONE FILE AT A TIME AND VISIBLE THE WHOLE WAY. This is the pass the page's
    "counted / left" bar is drawn from, so it updates STATE per file rather
    than per batch - a bar that moves once a minute is a bar nobody trusts.
    """
    init()
    if STATE["running"]:
        return {"ok": False, "why": "already reading"}
    rows = pending(limit)
    STATE.update(running=True, t0=time.time(), done=0, total=len(rows),
                 last="", err="", written=0, opened=0)
    t0 = time.time()
    budget = PROBE_PER_PASS
    try:
        for r in rows:
            try:
                # OPEN IT IF NOBODY EVER HAS. Checked before the read so the
                # row this pass writes reflects the probe this pass fetched,
                # rather than recording "could not look" and picking it up on
                # some later pass.
                if budget > 0:
                    with cursor() as _c:
                        seen = _probed(int(r["id"]), _c)
                    if not seen and _open_it(int(r["id"]), r["path"] or ""):
                        budget -= 1
                        STATE["opened"] += 1
                        STATE["opened_total"] = int(
                            STATE.get("opened_total") or 0) + 1
                scan_one(int(r["id"]), r)
                STATE["written"] += 1
            except Exception as e:                               # noqa: BLE001
                STATE["err"] = f"{type(e).__name__}: {e}"[:180]
            STATE["done"] += 1
            STATE["last"] = os.path.basename(r["path"] or "")
            if on_each is not None:
                try:
                    on_each(STATE["done"], STATE["total"])
                except Exception:                                # noqa: BLE001
                    pass
    finally:
        STATE.update(running=False, last_run=time.time(),
                     took=time.time() - t0)
    return {"ok": True, "read": STATE["written"], "took": STATE["took"]}


# -------------------------------------------------------------- the reader --
def _row(r) -> dict:
    d = dict(r)
    for k in ("tracks", "sides", "picture"):
        try:
            d[k] = json.loads(d.get(k) or ("{}" if k == "picture" else "[]"))
        except Exception:                                        # noqa: BLE001
            d[k] = {} if k == "picture" else []
    return d


def facts(file_id: int, read_now: bool = False) -> dict:
    """One file's facts. read_now forces a fresh read rather than the row."""
    init()
    if read_now:
        return scan_one(int(file_id))
    with cursor() as cur:
        r = cur.execute("SELECT * FROM sub_facts WHERE file_id=?",
                        (int(file_id),)).fetchone()
    return _row(r) if r else scan_one(int(file_id))


def interesting(limit: int = 100000) -> list:
    r"""Every file that could possibly need something doing to it.

    THE CHEAP FILTER, AND IT IS THE WHOLE POINT OF THE TABLE. A file with
    nothing beside it and one subtitle track inside it cannot have a duplicate,
    cannot have a sidecar taken in, and has no title worth doubting that the
    reader has not already looked at. Forty thousand files come down to a few
    thousand on two indexed integers, and the planner only ever thinks about
    those.
    """
    init()
    with cursor() as cur:
        return [_row(r) for r in cur.execute(
            # AND THE FILE HAS TO STILL EXIST. Measured before this join: of
            # 11,537 rows offered, 297 had no `files` row at all and 162 were
            # deleted or duplicates; 68 of those still had work planned, and
            # 67 of THOSE were not on disk either. Every one became a job that
            # reached a worker, found nothing, and reported "the file is not
            # there - the arr replaced or moved it". A row about a file that
            # has gone is not a file that could possibly need something doing
            # to it, which is what this function claims to return.
            "SELECT s.* FROM sub_facts s "
            "  JOIN files f ON f.id = s.file_id "
            " WHERE f.state NOT IN ('deleted','duplicate') "
            "   AND COALESCE(f.path,'') != '' "
            "   AND (s.n_sides > 0 OR s.n_tracks > 1 "
            "    OR COALESCE(s.picture,'{}') NOT IN ('{}','')) "
            " ORDER BY s.file_id LIMIT ?", (int(limit),))]


def forget(file_id: int) -> None:
    """Drop the row so the next pass reads this file again."""
    init()
    try:
        with cursor() as cur:
            cur.execute("DELETE FROM sub_facts WHERE file_id=?",
                        (int(file_id),))
    except Exception:                                            # noqa: BLE001
        pass


def libraries_on() -> list:
    """Which libraries this page is about at all."""
    return [l.name for l in (SETTINGS.libraries or [])]
