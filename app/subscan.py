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

_READY = False

STATE: dict = {"running": False, "t0": 0.0, "done": 0, "total": 0,
               "last": "", "last_run": 0.0, "took": 0.0, "err": "",
               "written": 0}


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
                err       TEXT    NOT NULL DEFAULT ''
            )""")
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
def _tracks_of(file_id: int, cur) -> list:
    """What is inside, from the stored probe, plus any line count known."""
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
    try:
        for s in cur.execute("SELECT track, events, chosen FROM subtitle_shape "
                             " WHERE file_id=?", (int(file_id),)):
            i = int(s["track"])
            if 0 <= i < len(out):
                if s["events"] is not None:
                    out[i]["events"] = int(s["events"])
                if s["chosen"]:
                    out[i]["chosen"] = str(s["chosen"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _sides_of(path: str) -> list:
    """What is sitting beside it, named as far as the name can be read."""
    from . import subembed
    out: list = []
    for side in subembed.sidecars_for(path):
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
             "tracks": [], "sides": [], "picture": {}, "err": ""}
        try:
            d["tracks"] = _tracks_of(int(file_id), cur)
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
                "  tracks,sides,picture,n_tracks,n_sides,scanned_at,err) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path,"
                "  library=excluded.library, disk=excluded.disk,"
                "  mtime=excluded.mtime, size=excluded.size,"
                "  tracks=excluded.tracks, sides=excluded.sides,"
                "  picture=excluded.picture, n_tracks=excluded.n_tracks,"
                "  n_sides=excluded.n_sides, scanned_at=excluded.scanned_at,"
                "  err=excluded.err",
                (int(file_id), d["path"], d["library"], d["disk"], d["mtime"],
                 d["size"], json.dumps(d["tracks"]), json.dumps(d["sides"]),
                 json.dumps(d["picture"]), len(d["tracks"]), len(d["sides"]),
                 time.time(), d["err"]))
    except Exception as e:                                       # noqa: BLE001
        d["err"] = f"{type(e).__name__}: {e}"[:180]
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


def counts() -> dict:
    r"""How many files there are, how many have been read, how many are left.

    ONE QUERY, THREE NUMBERS. Asking separately would let them disagree - the
    library grows while the page is drawing - and "19,002 of 19,001" is the
    kind of thing that makes a person stop believing the whole panel.
    """
    init()
    cutoff = time.time() - MAX_AGE_S
    out = {"total": 0, "counted": 0, "left": 0, "fresh": 0, "errors": 0}
    try:
        with cursor() as cur:
            r = cur.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN s.file_id IS NOT NULL THEN 1 ELSE 0 END)
                           AS counted,
                       SUM(CASE WHEN s.file_id IS NOT NULL
                                 AND COALESCE(s.mtime,-1) = COALESCE(f.mtime,0)
                                 AND COALESCE(s.size,-1)  = COALESCE(f.size,0)
                                 AND COALESCE(s.scanned_at,0) >= ?
                                THEN 1 ELSE 0 END) AS fresh,
                       SUM(CASE WHEN COALESCE(s.err,'') != '' THEN 1 ELSE 0 END)
                           AS errors
                  FROM files f LEFT JOIN sub_facts s ON s.file_id = f.id
                 WHERE """ + _live_where(), (cutoff,)).fetchone()
        out["total"] = int(r["total"] or 0)
        out["counted"] = int(r["counted"] or 0)
        out["fresh"] = int(r["fresh"] or 0)
        out["errors"] = int(r["errors"] or 0)
        out["left"] = max(0, out["total"] - out["fresh"])
    except Exception:                                            # noqa: BLE001
        pass
    return out


def progress() -> dict:
    """The scan's own state, for the panel. Counted, left, rate, how long."""
    c = counts()
    st = dict(STATE)
    el = (time.time() - st["t0"]) if st["running"] and st["t0"] else 0.0
    rate = (st["done"] / el) if el > 0.5 else 0.0
    return {**c, "running": st["running"], "done_this_pass": st["done"],
            "of_this_pass": st["total"], "last": st["last"],
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
                 last="", err="", written=0)
    t0 = time.time()
    try:
        for r in rows:
            try:
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
            "SELECT * FROM sub_facts "
            " WHERE n_sides > 0 OR n_tracks > 1 "
            "    OR COALESCE(picture,'{}') NOT IN ('{}','') "
            " ORDER BY file_id LIMIT ?", (int(limit),))]


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
