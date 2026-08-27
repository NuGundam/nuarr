"""
nuarr - database

THE CENTRAL DESIGN DECISION
--------------------------
Tdarr keys its skiplist on the FILE NAME. That is why 8,800 files fell out of
"Success" back into the queue in a single day here: Sonarr renamed them, the
names no longer matched, and every one was reprocessed from scratch.

nuarr keys a file on its *arr identity instead:

    (arr_id, arr_file_id)      e.g. Sonarr episodeFileId 387565

Those IDs are stable across renames, moves between pool disks, folder
restructures and metadata refreshes. The path is stored as a mutable ATTRIBUTE
of the record, never as its identity. A rename therefore becomes an UPDATE, not
a new file, and processing state survives untouched.

Files the arrs do not know about (extras, orphans) fall back to a content
signature: size + a sampled hash. Cheap, and still rename-proof.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import traceback
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- files ----
CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- DURABLE IDENTITY. Either an arr file id, or a content signature when the
    -- arrs do not know the file. Never the path.
    arr_name        TEXT,             -- 'Sonarr' | 'Radarr' | NULL
    arr_file_id     INTEGER,          -- episodeFileId / movieFileId
    arr_parent_id   INTEGER,          -- seriesId / movieId
    content_sig     TEXT,             -- fallback identity: size:partialhash

    -- MUTABLE attributes. These change freely without affecting identity.
    path            TEXT NOT NULL,
    library         TEXT,
    title           TEXT,             -- series or movie title, for display
    season          INTEGER,
    episode         TEXT,             -- '4' or '4-5' for multi-episode files
    size            INTEGER,
    mtime           REAL,

    -- media properties from ffprobe
    video_codec     TEXT,
    audio_codecs    TEXT,
    height          INTEGER,
    duration        REAL,
    bitrate         INTEGER,
    probe_json      TEXT,
    probed_at       REAL,

    -- processing state
    state           TEXT NOT NULL DEFAULT 'new',
                    -- new | held | eligible | queued | running | done
                    -- | skipped | blocked | error
    state_reason    TEXT,
    processed_at    REAL,             -- when we last successfully processed it
    processed_sig   TEXT,             -- what the file looked like when we did
    attempts        INTEGER NOT NULL DEFAULT 0,

    -- placement
    pool_disk       TEXT,

    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    updated_at      REAL NOT NULL
);

-- identity uniqueness: one row per arr file, one per content signature
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_arr
    ON files(arr_name, arr_file_id) WHERE arr_file_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_sig
    ON files(content_sig) WHERE content_sig IS NOT NULL AND arr_file_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_files_path  ON files(path);
CREATE INDEX IF NOT EXISTS ix_files_state ON files(state);
CREATE INDEX IF NOT EXISTS ix_files_lib   ON files(library);
-- The drill panel's unfiltered view is ORDER BY size DESC LIMIT 500 over the
-- whole table, polled every few seconds. Without this it re-sorted 39k rows
-- per poll (~100-250 ms); with it the top N walk straight off the index.
CREATE INDEX IF NOT EXISTS ix_files_size  ON files(size DESC);
-- NOCASE, and that is the whole point.
--
-- The browser asks "everything under this folder" as `path LIKE 'P:\Lib\Show\%'`.
-- SQLite can turn a prefix LIKE into an index range scan, but only against an
-- index whose collation matches the comparison - and LIKE is case-INSENSITIVE
-- by default, so the plain BINARY ix_files_path above never qualified. Every
-- browse fell back to SCAN files: ~39,000 rows read to answer a question about
-- one folder. Warm that cost 77 ms and was invisible; contending with twelve
-- transcodes writing through the WAL it was measured at 14,207 ms for the
-- identical query.
--
-- With this index the plan becomes
--     SEARCH files USING INDEX ix_files_path_nc (path>? AND path<?)
-- which matters most for the case you actually click through - a show or season
-- folder, where the range is a few dozen rows instead of the whole table.
CREATE INDEX IF NOT EXISTS ix_files_path_nc ON files(path COLLATE NOCASE);

-- ----------------------------------------------------------------- jobs ----
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL is legitimate: library-wide handlers (pool_map, dv_scan) act on the
    -- whole pool, not one file. It was NOT NULL, which forced callers to pass
    -- 0 - a reference to a row that does not exist.
    file_id       INTEGER REFERENCES files(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,       -- probe|transcode|remux|subs_ocr|repair
    state         TEXT NOT NULL DEFAULT 'queued',
                  -- queued|running|done|failed|cancelled|skipped
    priority      INTEGER NOT NULL DEFAULT 100,
    worker        TEXT,
    plan_json     TEXT,                -- what we intend to do + why
    result_json   TEXT,
    error         TEXT,
    size_before   INTEGER,
    size_after    INTEGER,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state, priority, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_file  ON jobs(file_id);

-- -------------------------------------------------------------- history ----
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id    INTEGER REFERENCES files(id) ON DELETE CASCADE,
    event      TEXT NOT NULL,     -- renamed|moved_disk|processed|failed|...
    detail     TEXT,
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_history_file ON history(file_id, at DESC);
CREATE INDEX IF NOT EXISTS ix_history_at   ON history(at DESC);

-- ------------------------------------------------------- pool placement ----
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title_key TEXT NOT NULL,       -- 'library|title folder'
    disk      TEXT NOT NULL,
    files     INTEGER NOT NULL,
    bytes     INTEGER NOT NULL,
    at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pool_title ON pool_snapshots(title_key, at DESC);

-- ----------------------------------------------------------- raw probes ----
-- The full ffprobe output, OUT of the files table.
--
-- It lived in files.probe_json, where it had grown to 70.3 MB - 52% of a
-- 135 MB database - for data that is written once and READ NOWHERE. The fields
-- anything actually uses (video_codec, height, duration, bitrate,
-- audio_codecs) are extracted into real columns at probe time; rules.decide()
-- and handlers.suggest_kinds() take the LIVE probe dict, never this.
--
-- A blob that size does not just cost disk. SQLite spills oversized rows onto
-- overflow pages, so the 39,231-row files table was spread across far more
-- pages than its useful columns need, and every index-less scan of it paid for
-- that. Moving it to a side table keeps it available for diagnosing a specific
-- file while taking it off the hot path, and lets it age out on its own clock.
CREATE TABLE IF NOT EXISTS file_probes (
    file_id  INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    json     TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_probes_at ON file_probes(at);

-- ------------------------------------------------------------- settings ----
CREATE TABLE IF NOT EXISTS kv (
    k  TEXT PRIMARY KEY,
    v  TEXT
);
"""


# Per-connection PRAGMAs. These do NOT persist in the database file - only
# journal_mode does. Leaving them in SCHEMA meant they applied once, during
# init_db(), and every runtime connection then ran with the defaults:
# synchronous=FULL (an fsync per write) and a 2 MB page cache that was thrown
# away again immediately. On a 40,000-row database polled every 2 seconds that
# is the difference between a warm index scan and a cold one.
_CONN_PRAGMAS = (
    "PRAGMA synchronous=NORMAL;"      # WAL makes FULL unnecessary for durability
    "PRAGMA foreign_keys=ON;"
    # PER CONNECTION, and connections are thread-local and long-lived, so this
    # is multiplied by the number of worker threads rather than paid once. An
    # earlier 64 MB here sent RSS from 172 MB to 624 MB and still climbing;
    # 8 MB replaced it and RSS still grew to 650 MB across four scans.
    #
    # It is 2 MB now because the private page cache is largely REDUNDANT here:
    # mmap_size below maps the entire 125 MB database, and those pages are
    # shared by the OS across every connection. Cache above that mostly buys a
    # second, private copy of pages the process already has mapped - paid once
    # per thread. 2 MB still covers the hot b-tree interior nodes, which are
    # what the cache is actually for.
    "PRAGMA cache_size=-2048;"        # 2 MB of pages, negative = KiB not pages
    "PRAGMA temp_store=MEMORY;"       # sorts/temp b-trees stay off the disk
    # mmap OFF. This reasoning used to read "the OS shares those pages rather
    # than duplicating them, so 128 MB comfortably covers the whole DB" - which
    # is true about physical pages and wrong about the consequence.
    #
    # Measured on the live process: 25 thread-local connections, 21 of them
    # holding a FULLY RESIDENT 128 MB view of a 275 MB database. Every one of
    # those views pulls its pages into this process's working set, and the
    # working set is what the machine budgets. RSS sat at 800-1,100 MB with a
    # Python heap of 20 MB and every internal cache empty.
    #
    # A controlled A/B on this database - same 25 connections, same queries,
    # only this pragma different:
    #     mmap 128 MB : RSS 121 MB then 401 MB   (varies with page residency)
    #     mmap off    : RSS 143 MB then 145 MB   (steady)
    # The mmap version is not reliably smaller and is far less predictable.
    #
    # The read benefit it was bought for is mostly redundant anyway: the OS
    # file cache already holds a 275 MB file that is read constantly, so the
    # pages are in RAM either way - mmap only removes a memcpy, and pays for it
    # with per-connection working set. Set to 0 rather than merely smaller so
    # the behaviour is one thing rather than twenty-five partial views.
    "PRAGMA mmap_size=0;"
    "PRAGMA busy_timeout=30000;"
)

# One connection per thread, kept open. Opening a fresh connection for every
# query threw away the page cache each time and re-walked the WAL index;
# uvicorn runs sync endpoints on a bounded threadpool, so the number of these
# is bounded by that pool, not by request count.
_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(_CONN_PRAGMAS)
        _local.conn = conn
    return conn


# ------------------------------------------------- event-loop protection ----
#
# THE INVARIANT: no SQLite access on the asyncio event loop thread.
#
# Every query in here is "fast" in isolation - single-digit milliseconds against
# a warm 125 MB database. That is what made this so hard to see. Under twelve
# concurrent transcodes the pool disks are saturated, a read that misses the
# page cache waits behind 200 MB/s of copy traffic, and a write waits for the
# WAL lock behind whoever else is committing. Those same "fast" queries then
# take seconds - and because they were called straight from coroutines, uvicorn
# could not service ANY request while one was outstanding. An endpoint that
# touches no database at all was measured at 20 s for this reason.
#
# Patching call sites one at a time did not hold: there were 42 of them across
# seven modules, and nothing stopped the next one being added. So the rule is
# enforced here instead, at the single choke point every query passes through.
#
# LOG, DO NOT RAISE. Raising would turn a latency bug into an outage, and these
# paths include commit and rename - work that must not fail because of a
# diagnostic. Instead each offending call site is recorded once, with its
# location, and surfaced on /api/diag/pool. Remaining offenders report
# themselves rather than waiting to be guessed at.
ON_LOOP: dict[str, int] = {}
_on_loop_lock = threading.Lock()


def _note_loop_access() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return                      # worker thread - correct, nothing to do
    # We are on the loop thread. Record the caller, skipping this module.
    # Skip this module AND contextlib: cursor() is a @contextmanager, so the
    # innermost frames are always contextlib's __enter__ and attributing there
    # names every caller "contextlib.py:141", which is useless.
    try:
        st = traceback.extract_stack(limit=14)[:-2]
        where = next((f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                      for f in reversed(st)
                      if not f.filename.endswith(("db.py", "contextlib.py"))),
                     "unknown")
    except Exception:
        where = "unknown"
    with _on_loop_lock:
        ON_LOOP[where] = ON_LOOP.get(where, 0) + 1


async def arun(fn, *args, **kwargs):
    """Run a function that touches the database, off the event loop.

    The one-line fix for any coroutine that needs a query: wrap the body in a
    local def and await this. Thread-local connections make it safe - each
    worker thread gets its own, so there is no shared cursor to race on.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


@contextmanager
def cursor():
    _note_loop_access()
    conn = connect()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with cursor() as cur:
        cur.executescript(SCHEMA)
        # --- migrations -----------------------------------------------------
        # The queue lives in this table, not in process memory. A job needs a
        # stable public id (for logs and cancel) and a pool, so dispatch can
        # route without re-probing.
        have = {r["name"] for r in cur.execute("PRAGMA table_info(files)")}
        # Missing-file self-healing. A file the arr tracks but that is not on
        # disk is usually NOT a real loss - it is an arr whose record is a few
        # minutes stale after a rename, an upgrade, or a DrivePool move. These
        # columns let the healer re-check a few times before the file is
        # reported to a human, so the Missing tile counts real problems.
        # requeued_at marks a file the user deliberately sent back through, so
        # the next enqueue puts it at the FRONT. Without it a requeue dropped
        # the file into a 2,000-file queue ordered by size and it might not run
        # for hours - which is not what "requeue this" means when you have just
        # fixed the thing that broke it.
        # adopt_* is the same idea for the OTHER direction. A file with no
        # arr_file_id is "unmanaged", and like a missing file that is usually
        # staleness rather than a fact: the arr imported it after our last
        # scan, or never imported it because the import failed once. The
        # adopter re-checks a few times before the file is reported as an
        # orphan a human has to deal with. Separate columns from heal_* because
        # a file can be neither, and mixing the two counters would let one
        # sweep exhaust the other's attempts.
        for name, ddl in (("heal_attempts", "INTEGER DEFAULT 0"),
                          ("heal_last_at", "REAL"),
                          ("heal_state", "TEXT"),
                          ("adopt_attempts", "INTEGER DEFAULT 0"),
                          ("adopt_last_at", "REAL"),
                          ("adopt_state", "TEXT"),
                          # Set to 'rejected' when subtitle OCR ran and the
                          # result was not usable, so the queue stops choosing
                          # a file it has already proved it cannot convert.
                          ("subocr_state", "TEXT"),
                          # The language the title was MADE in, from TMDB via
                          # Radarr or TheTVDB via Sonarr. It is a property of
                          # the work, not of the file - nothing in the media
                          # carries it - so it has to be fetched and stored.
                          # The audio rules need it to tell "the original
                          # track, worth keeping" from "a dub of something
                          # else": a Korean drama's Korean audio and a French
                          # dub of it look identical to a probe.
                          ("orig_lang", "TEXT"),
                          # Language tag per track, in track order, "-" for a
                          # blank. Extracted at probe time for the same reason
                          # audio_codecs is: three screens were answering
                          # "which languages does this library contain" by
                          # parsing all 39,563 probe blobs on every request.
                          ("audio_langs", "TEXT"),
                          ("sub_langs", "TEXT"),
                          ("requeued_at", "REAL")):
            if name not in have:
                cur.execute(f"ALTER TABLE files ADD COLUMN {name} {ddl}")
                have.add(name)
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(jobs)")}
        # `stage` is how far a job got, persisted. It only existed in memory on
        # the Worker, so a restart could not tell an encode that had barely
        # started from one that had FINISHED and was mid-commit - and requeued
        # both, throwing away a completed 60 GB encode to redo it from scratch.
        # `source` is WHO asked for this job - manual | auto | requeue. With one
        # undifferentiated queue there was no way to answer "why is this here",
        # and no way to clear the automatic backlog without also throwing away
        # the handful of files you queued deliberately.
        for name, ddl in (("job_id", "TEXT"), ("pool", "TEXT"),
                          ("path", "TEXT"), ("title", "TEXT"),
                          ("stage", "TEXT"),
                          ("source", "TEXT DEFAULT 'manual'")):
            if name not in cols:
                cur.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        # jobs.file_id must allow NULL.
        #
        # It was declared NOT NULL REFERENCES files(id), assuming every job
        # belongs to one file. Library-wide handlers - pool_map, dv_scan - have
        # no file, so the caller passed 0 instead. That is an invalid reference,
        # and it inserted silently only because foreign_keys was off on every
        # connection except the one init_db used. With the pragma applied to all
        # connections the truth surfaced: 0 fails the FK, NULL fails NOT NULL.
        #
        # SQLite cannot drop NOT NULL in place, so rebuild the table once.
        jrow = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                           "AND name='jobs'").fetchone()
        if jrow and "file_id       INTEGER NOT NULL" in (jrow["sql"] or ""):
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("""
                CREATE TABLE jobs_new (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
                    kind        TEXT NOT NULL,
                    state       TEXT NOT NULL DEFAULT 'queued',
                    priority    INTEGER NOT NULL DEFAULT 100,
                    worker      TEXT,
                    plan_json   TEXT,
                    result_json TEXT,
                    error       TEXT,
                    size_before INTEGER,
                    size_after  INTEGER,
                    created_at  REAL NOT NULL,
                    started_at  REAL,
                    finished_at REAL,
                    job_id      TEXT,
                    pool        TEXT,
                    path        TEXT,
                    title       TEXT)""")
            cur.execute("""
                INSERT INTO jobs_new (id,file_id,kind,state,priority,worker,
                    plan_json,result_json,error,size_before,size_after,
                    created_at,started_at,finished_at,job_id,pool,path,title)
                SELECT id,
                       CASE WHEN file_id=0 THEN NULL ELSE file_id END,
                       kind,state,priority,worker,plan_json,result_json,
                       error,size_before,size_after,created_at,started_at,
                       finished_at,job_id,pool,path,title
                FROM jobs""")
            cur.execute("DROP TABLE jobs")
            cur.execute("ALTER TABLE jobs_new RENAME TO jobs")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_jobs_state "
                        "ON jobs(state, priority, created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_jobs_file ON jobs(file_id)")
            cur.execute("PRAGMA foreign_keys=ON")

        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_jobid "
                    "ON jobs(job_id) WHERE job_id IS NOT NULL")

        # ONE LIVE JOB PER FILE, enforced by the database.
        #
        # enqueue() already checked for an existing queued/running job, but the
        # check and the INSERT are seconds apart - the file is probed and
        # planned in between - so two callers could both pass the check and
        # both insert. Measured: the auto-queue timer overlapping its own
        # "Top up now" button queued 18 files twice, every one of which would
        # have been encoded a second time for nothing.
        #
        # Existing duplicates have to go before the index can exist. The OLDEST
        # row per file is kept: if one of them is already running, it is that
        # one, and deleting it would orphan a live encode.
        dups = cur.execute(
            "SELECT COUNT(*) n FROM (SELECT file_id FROM jobs "
            "WHERE file_id IS NOT NULL AND state IN ('queued','running') "
            "GROUP BY file_id HAVING COUNT(*) > 1)").fetchone()["n"]
        if dups:
            cur.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT j.id FROM jobs j"
                "  JOIN (SELECT file_id, MIN(id) keep FROM jobs"
                "        WHERE file_id IS NOT NULL"
                "          AND state IN ('queued','running')"
                "        GROUP BY file_id) k"
                "    ON k.file_id = j.file_id"
                "  WHERE j.state IN ('queued','running') AND j.id <> k.keep)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_live_file "
                    "ON jobs(file_id) WHERE file_id IS NOT NULL "
                    "AND state IN ('queued','running')")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_jobs_claim "
                    "ON jobs(state, pool, priority, created_at)")
        # History rows for pool-wide work (pool_map, repairs) have no file to
        # join a title from, so they rendered as "(untitled)". Carry the name
        # on the event itself.
        hcols = {r["name"] for r in cur.execute("PRAGMA table_info(history)")}
        if "label" not in hcols:
            cur.execute("ALTER TABLE history ADD COLUMN label TEXT")

        # --- files.probe_json -> file_probes ---------------------------------
        # One-way migration, run once. The blob is moved rather than dropped so
        # nothing is lost, then the column is emptied to release the pages.
        #
        # SQLite cannot DROP COLUMN on a table with partial indexes on older
        # builds, and rebuilding a 39,231-row table under a live queue is a far
        # bigger risk than leaving one always-NULL column in place. So the
        # column stays and simply stops being written; VACUUM reclaims the
        # space. Anything reading it (nothing does) still sees NULL, not junk.
        if "probe_json" in have:
            n = cur.execute("SELECT COUNT(*) c FROM files "
                            "WHERE probe_json IS NOT NULL").fetchone()["c"]
            if n:
                cur.execute(
                    "INSERT OR REPLACE INTO file_probes(file_id, json, at) "
                    "SELECT id, probe_json, COALESCE(probed_at, updated_at) "
                    "FROM files WHERE probe_json IS NOT NULL")
                cur.execute("UPDATE files SET probe_json=NULL "
                            "WHERE probe_json IS NOT NULL")
                kv_set("migration.probe_json_moved", str(n))
        _ = have


_EP_RE = None


def pretty_from_filename(name: str) -> str:
    r"""A release filename reduced to the thing a person would say out loud.

        Leverage - Redemption (2021) - S03E08 - The Cooling Off the Mark Job
            [WEBRip-1080p][10bit][x265][EAC3 5.1]-NTb.mkv
        -> Leverage - Redemption - S03E08

        Panda Plan The Magical Tribe (2026) {tmdb-1421552} [WEBDL-2160p]
            [HDR10][EAC3 5.1][x265]-Kitsune.mkv
        -> Panda Plan The Magical Tribe

    Everything after the season/episode marker is the episode title and the
    encoder's boasting, and neither identifies the file better than SxxExx
    already does. In a 300-pixel cell they push the part you actually read off
    the end.

    SPLIT ON THE EPISODE MARKER, not on " - ". Plenty of shows have a hyphen in
    their own name - "Leverage - Redemption" is exactly that - so splitting on
    the separator truncates the title instead of the noise.

    The year is dropped for episodes, where SxxExx already identifies the file,
    and KEPT for films, where it is part of how you tell two of them apart.
    """
    global _EP_RE
    if _EP_RE is None:
        import re as _re
        _EP_RE = _re.compile(r"\bS\d{1,3}E\d{1,4}(?:[-E]+\d{1,4})*\b", _re.I)

    import os as _os
    import re as _re
    n = _os.path.basename(name or "")
    n = _re.sub(r"\.(mkv|mp4|avi|m4v|ts|mov|wmv)$", "", n, flags=_re.I)

    m = _EP_RE.search(n)
    if m:
        head = n[:m.start()]
        head = _re.sub(r"\s*\((?:19|20)\d{2}\)\s*", " ", head)     # (2021)
        head = _re.sub(r"\s*\{[^}]*\}\s*", " ", head)              # {tvdb-123}
        head = _re.sub(r"[\s\-]+$", "", head).strip()
        return f"{head} - {m.group(0).upper()}" if head else m.group(0).upper()

    # A film: cut at the first bracketed tag or id, keep the year.
    cut = len(n)
    for ch in ("[", "{"):
        i = n.find(ch)
        if i > 0:
            cut = min(cut, i)
    return _re.sub(r"\s+", " ", n[:cut]).strip(" -") or n


def display_label(title: str | None, season=None, episode=None) -> str:
    """'Sword Art Online - S02E13' rather than just 'Sword Art Online'.

    A series title on its own is useless in a log when 200 episodes share it -
    you cannot tell which file a line refers to.
    """
    t = (title or "").strip() or "(untitled)"
    if season is None and not episode:
        return t
    # Pad each numeric part so it sorts and reads like the filenames do:
    # S01E06, and S01E04-E05 for a multi-episode file.
    raw = str(episode or "").strip()
    parts = [p for p in raw.split("-") if p]
    ep = "-E".join(f"{int(p):02d}" if p.isdigit() else p for p in parts) if parts else ""
    if season is not None and ep:
        return f"{t} - S{int(season):02d}E{ep}"
    if season is not None:
        return f"{t} - S{int(season):02d}"
    return f"{t} - E{ep}"


# The settings table, held in memory.
#
# The loop guard was expected to indict the job pipeline. It indicted the
# settings reads instead: of 987 database calls made on the event loop in one
# minute, 721 were kv lookups -
#
#     workers.py:175    get()          406
#     gate.py:205       get_toggle()   241
#     ffmpeg_update.py  pinned_dir()    74
#
# workers.get() alone issues one SELECT PER KEY and is called from _capacity()
# on every dispatch decision. None of these values change unless somebody edits
# a setting, so this was hundreds of queries a minute re-reading constants.
#
# The right fix is therefore not "move it to a thread" - it is to stop doing
# the work. The whole table is a handful of rows; load it once and serve from
# memory. This process is the only writer, so the cache cannot go stale behind
# our back, and kv_set keeps it coherent.
_KV: dict[str, str] | None = None
_kv_lock = threading.Lock()


def _kv_load() -> dict:
    with cursor() as cur:
        rows = cur.execute("SELECT k, v FROM kv").fetchall()
    return {r["k"]: r["v"] for r in rows}


def kv_get(key: str, default: str | None = None) -> str | None:
    global _KV
    if _KV is None:
        with _kv_lock:
            if _KV is None:
                _KV = _kv_load()
    return _KV.get(key, default)


def kv_set(key: str, value: str) -> None:
    global _KV
    with cursor() as cur:
        cur.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )
    with _kv_lock:
        if _KV is None:
            _KV = {}
        _KV[key] = value


def kv_invalidate() -> None:
    """Drop the settings cache. For anything that writes kv behind kv_set."""
    global _KV
    with _kv_lock:
        _KV = None


def log_event(file_id: int | None, event: str, detail: str = "",
              label: str | None = None) -> None:
    import time
    with cursor() as cur:
        cur.execute(
            "INSERT INTO history(file_id,event,detail,at,label) VALUES(?,?,?,?,?)",
            (file_id or None, event, detail, time.time(), label),
        )


if __name__ == "__main__":
    init_db()
    print(f"initialised {DB_PATH}")
