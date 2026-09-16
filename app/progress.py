r"""nuarr - what has and has not been done to each file, in one place.

WHY
---
Six stores answered "is this still valid" and every one of them answered it
the same wrong way - by asking whether the BYTES had changed:

    integrity       file_id + size, mtime          39,859 rows
    audio_lang      file_id, track + size, mtime   57,929 rows
    sub_facts       file_id + size, mtime          40,904 rows
    hardsub         file_id + size                  4,298 rows
    subtitle_shape  file_id, track + size             680 rows
    file_probes     file_id + at                   18,248 rows

Size and mtime change for two completely different reasons - somebody replaced
the file, or NUARR edited it - and those two demand opposite answers. Treating
them as one is what sent 2,145 tracks back through Whisper to be told the same
thing again, and what re-decoded files nuarr had just written. Each store was
fixed on its own, one bug at a time, which is six places to get it right and
six places to get it wrong again.

And nothing could answer the plain question. files.state holds two values
across the whole library - 39,859 'done' and 356 'deleted' - so "what has been
checked on this file, and what is still owed" had no answer anywhere.

Erik: "can we add DB entry per file to track the state of the file progress so
we know what has and has not been checked or done to each, and it be track by
not the name/size parts that change but a different file marker".

THE MARKER
----------
files.id already is one, and it already has the right meaning: it survives
nuarr's own rewrites - 6,534 of them in a week, every one keeping its id - and
an arr replacement makes a NEW row (Always a Catch! S01E12 went #35634
deleted -> #55794). Nothing about it is derived from the name or the size.

What was missing is a way to say WHICH VERSION of those bytes a check was made
against, without going back to size and mtime. That is `files.rev`: a counter
nuarr owns and bumps once, in fileops.REPLACED, every time it replaces a file -
the one door a transcode, an OCR embed, a sidecar merge and a deferred commit
all pass through.

  a stage is current  <=>  its rev == the file's rev

A rewrite bumps the rev, so every stage goes stale at once - and then the
commit says explicitly which ones the rewrite could not have invalidated and
carries those forward. That is the honest version of the rule: "the audio
behind a kept track is the same audio, so the listen stands; the bytes are
new, so the decode check does not."

`uid` is reserved and unused. The marker written INTO the container comes
later; when it does, it lands in this column and this table does not change
shape.

WHAT THIS IS NOT
----------------
Not a replacement for the six stores - they keep their detail, their verdicts
and their own tables. This is the index over them: one row per file saying
what has been done, at which rev, and the one line it found. Read it to decide
whether to do the work; read the store to see what the work said.
"""
from __future__ import annotations

import time

from .db import cursor

# THE STAGES A FILE GOES THROUGH, in the order precedence.py takes them.
# 'plan' is the moment the four facts become a decision (the 'checked' row);
# 'rewrite' is the commit that acts on it.
STAGES = ("decode", "listen", "audio", "subread",
          "plan", "rewrite", "subocr", "subs")

# WHAT SURVIVES NUARR'S OWN REWRITE.
#
# The question each stage answers, and whether a remux can change that answer:
#   listen   what language is this track  - no. A kept track is the same audio,
#            copied or re-encoded, and Whisper says the same word about it.
#   audio    is the tag right             - no. The rewrite wrote the corrected
#            tags itself, from this verdict.
#   decode   do these bytes decode        - YES, and that is the whole point of
#            the second pass: it is the check that says nuarr did not break it.
#   subread  what is in each subtitle     - YES. The tracks moved.
#   the rest are about the current container by definition.
SURVIVES_REWRITE = ("listen", "audio")

_READY = False


def init() -> None:
    """One row per file. Created on demand; never raises into a caller."""
    global _READY
    if _READY:
        return
    cols = ", ".join(f"{s}_rev INTEGER, {s}_at REAL, {s}_note TEXT"
                     for s in STAGES)
    with cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS file_progress(
                file_id    INTEGER PRIMARY KEY,
                uid        TEXT,
                updated_at REAL,
                {cols}
            )""")
        # Reading "what is owed across the library" is a scan of this table
        # against files.rev; the index is for the per-file reads that the
        # feeders do on every pass.
        cur.execute("CREATE INDEX IF NOT EXISTS ix_progress_updated "
                    "ON file_progress(updated_at)")
    _READY = True


def rev_of(file_id: int) -> int:
    """The file's current generation. 0 for a file nuarr has never rewritten."""
    try:
        with cursor() as cur:
            r = cur.execute("SELECT COALESCE(rev, 0) v FROM files WHERE id=?",
                            (int(file_id),)).fetchone()
        return int(r["v"]) if r else 0
    except Exception:                                        # noqa: BLE001
        return 0


def note(file_id: int, stage: str, note_text: str = "",
         rev: int | None = None) -> None:
    """Record that `stage` has been done to this file, at this rev.

    Called by the stage itself, at the moment it has an answer. `rev` is read
    from the file unless the caller has it - the commit does, because it has
    just bumped it.
    """
    if not file_id or stage not in STAGES:
        return
    try:
        init()
        r = rev_of(file_id) if rev is None else int(rev)
        with cursor() as cur:
            cur.execute(
                f"INSERT INTO file_progress(file_id, updated_at, "
                f"  {stage}_rev, {stage}_at, {stage}_note) VALUES(?,?,?,?,?) "
                f"ON CONFLICT(file_id) DO UPDATE SET updated_at=excluded.updated_at, "
                f"  {stage}_rev=excluded.{stage}_rev, "
                f"  {stage}_at=excluded.{stage}_at, "
                f"  {stage}_note=excluded.{stage}_note",
                (int(file_id), time.time(), r, time.time(),
                 (note_text or "")[:400]))
    except Exception:                                        # noqa: BLE001
        pass


def carry(file_id: int, stages, to_rev: int) -> int:
    """Move stages onto a new rev without redoing them.

    The commit's own statement about what it did not invalidate. Only touches
    a stage that HAS been done - carrying a blank forward would claim a check
    that never happened.
    """
    done = 0
    try:
        init()
        with cursor() as cur:
            for s in stages:
                if s not in STAGES:
                    continue
                cur.execute(
                    f"UPDATE file_progress SET {s}_rev=?, updated_at=? "
                    f" WHERE file_id=? AND {s}_at IS NOT NULL",
                    (int(to_rev), time.time(), int(file_id)))
                done += cur.rowcount if cur.rowcount > 0 else 0
    except Exception:                                        # noqa: BLE001
        return done
    return done


def of(file_id: int) -> dict:
    """Everything known about one file's progress, with the rev applied.

    -> {rev, uid, stages: {name: {done, at, rev, note, current}}, owed: [...]}
    A stage that has never run is `done: False`; one that ran against older
    bytes is `done: True, current: False` - a different thing, and the reason
    the panel can say "checked, but not since the rewrite".
    """
    out = {"rev": 0, "uid": "", "stages": {}, "owed": list(STAGES)}
    try:
        init()
        with cursor() as cur:
            f = cur.execute("SELECT COALESCE(rev,0) rev FROM files WHERE id=?",
                            (int(file_id),)).fetchone()
            if not f:
                return out
            out["rev"] = int(f["rev"])
            r = cur.execute("SELECT * FROM file_progress WHERE file_id=?",
                            (int(file_id),)).fetchone()
    except Exception:                                        # noqa: BLE001
        return out
    owed = []
    for s in STAGES:
        at = (r[f"{s}_at"] if r else None)
        rv = (r[f"{s}_rev"] if r else None)
        cur_ok = at is not None and int(rv or 0) == out["rev"]
        out["stages"][s] = {"done": at is not None,
                            "at": float(at or 0), "rev": int(rv or 0),
                            "note": (r[f"{s}_note"] if r else "") or "",
                            "current": cur_ok}
        if not cur_ok:
            owed.append(s)
    out["uid"] = (r["uid"] if r else "") or ""
    out["owed"] = owed
    return out


def owed(file_id: int) -> list:
    """Just the stages that are not current. The cheap question."""
    return of(file_id).get("owed") or []


# STALE, NOT ABSENT. A backfilled stage whose store no longer describes the
# file on disk is recorded as done at rev -1: it HAPPENED, and it is not
# current. Writing nothing would say it never ran, which is a different claim
# and the one that sends work round again.
STALE_REV = -1


def backfill(force: bool = False) -> dict:
    r"""Seed the ledger from what the six stores already know.

    39,859 files have been decoded, listened to and read for years; starting
    the ledger empty would claim none of that happened and hand every feeder
    the whole library at once. So the first fill is a read of the stores, with
    each stage marked current only where its own freshness test still passes
    against the file on disk - the same test that store uses today.

    Idempotent and cheap to re-run: INSERT ... ON CONFLICT DO NOTHING, so a
    stage already recorded by a real run is never overwritten by a guess.
    """
    init()
    out = {"files": 0, "stages": {}}
    try:
        with cursor() as cur:
            if not force and cur.execute(
                    "SELECT 1 FROM file_progress LIMIT 1").fetchone():
                return {"skipped": "already filled"}
            # A FORCED REFILL STARTS EMPTY. The updates below are
            # WHERE EXISTS, so a stage that no longer has a source keeps
            # whatever a previous fill put there - which is how a first draft
            # reading the wrong table left 4,532 files claiming a subtitle
            # pass they had never had. Re-deriving means re-deriving.
            if force:
                cur.execute("DELETE FROM file_progress")
            now = time.time()
            cur.execute("INSERT OR IGNORE INTO file_progress(file_id, updated_at) "
                        "SELECT id, ? FROM files WHERE state!='deleted'", (now,))
            out["files"] = cur.rowcount

            # decode <- integrity. Its own rule: the verdict describes these
            # bytes when the size it was taken at is the size that is there.
            cur.execute("""
                UPDATE file_progress SET
                  decode_at  = (SELECT i.at FROM integrity i WHERE i.file_id=file_progress.file_id),
                  decode_note= (SELECT COALESCE(i.detail, i.verdict) FROM integrity i
                                 WHERE i.file_id=file_progress.file_id),
                  decode_rev = (SELECT CASE WHEN CAST(i.size AS INTEGER)=CAST(f.size AS INTEGER)
                                            THEN 0 ELSE ? END
                                  FROM integrity i JOIN files f ON f.id=i.file_id
                                 WHERE i.file_id=file_progress.file_id)
                 WHERE EXISTS (SELECT 1 FROM integrity i WHERE i.file_id=file_progress.file_id)
            """, (STALE_REV,))
            out["stages"]["decode"] = cur.rowcount

            # listen <- audio_lang, the newest verdict on the file. Current
            # only when every track's verdict still matches the file.
            cur.execute("""
                UPDATE file_progress SET
                  listen_at  = (SELECT MAX(a.checked_at) FROM audio_lang a
                                 WHERE a.file_id=file_progress.file_id),
                  listen_note= (SELECT 'heard ' || COUNT(*) || ' track(s): ' ||
                                       GROUP_CONCAT(COALESCE(NULLIF(a.code,''),'?'))
                                  FROM audio_lang a WHERE a.file_id=file_progress.file_id),
                  listen_rev = (SELECT CASE WHEN MIN(CASE WHEN CAST(a.size AS INTEGER)=CAST(f.size AS INTEGER)
                                                          THEN 1 ELSE 0 END)=1
                                            THEN 0 ELSE ? END
                                  FROM audio_lang a JOIN files f ON f.id=a.file_id
                                 WHERE a.file_id=file_progress.file_id)
                 WHERE EXISTS (SELECT 1 FROM audio_lang a WHERE a.file_id=file_progress.file_id)
            """, (STALE_REV,))
            out["stages"]["listen"] = cur.rowcount

            # subread <- sub_facts, the scan that says what subtitles a file has.
            cur.execute("""
                UPDATE file_progress SET
                  subread_at  = (SELECT s.scanned_at FROM sub_facts s
                                  WHERE s.file_id=file_progress.file_id),
                  subread_note= (SELECT COALESCE(s.n_tracks,0) || ' track(s), ' ||
                                        COALESCE(s.n_sides,0) || ' sidecar(s)'
                                   FROM sub_facts s WHERE s.file_id=file_progress.file_id),
                  subread_rev = (SELECT CASE WHEN CAST(s.size AS INTEGER)=CAST(f.size AS INTEGER)
                                             THEN 0 ELSE ? END
                                   FROM sub_facts s JOIN files f ON f.id=s.file_id
                                  WHERE s.file_id=file_progress.file_id)
                 WHERE EXISTS (SELECT 1 FROM sub_facts s WHERE s.file_id=file_progress.file_id)
            """, (STALE_REV,))
            out["stages"]["subread"] = cur.rowcount

            # THE REST COME FROM THE JOBS TABLE, not from history event
            # names. These four are events rather than measurements - a
            # rewrite, a tag write, an OCR embed - so the record of them IS
            # the record that they happened. But history only started naming
            # them after their kind recently: 106,496 rows predate that and
            # still read 'done', which is why a first draft of this found 0
            # files that had ever had subtitle OCR while 7,119 sub_ocr jobs
            # sat in the jobs table. jobs.kind has been right all along.
            for stage, kind in (("audio",   "audio"),
                                ("rewrite", "transcode"),
                                ("subocr",  "sub_ocr"),
                                ("subs",    "subs")):
                cur.execute(f"""
                    UPDATE file_progress SET
                      {stage}_at   = (SELECT MAX(j.finished_at) FROM jobs j
                                       WHERE j.file_id=file_progress.file_id
                                         AND j.kind=? AND j.state='done'),
                      {stage}_note = (SELECT COALESCE(
                                        json_extract(j.result_json,'$.summary'), '')
                                       FROM jobs j
                                       WHERE j.file_id=file_progress.file_id
                                         AND j.kind=? AND j.state='done'
                                       ORDER BY j.finished_at DESC LIMIT 1),
                      {stage}_rev  = 0
                     WHERE EXISTS (SELECT 1 FROM jobs j
                                    WHERE j.file_id=file_progress.file_id
                                      AND j.kind=? AND j.state='done')
                """, (kind, kind, kind))
                out["stages"][stage] = cur.rowcount

            # 'plan' is the only one with no job of its own - it is the moment
            # the four facts became a decision, which the 'checked' row marks.
            # Files that were planned before that row existed are left blank
            # rather than guessed at; the next pass over them writes it.
            cur.execute("""
                UPDATE file_progress SET
                  plan_at   = (SELECT MAX(h.at) FROM history h
                                WHERE h.file_id=file_progress.file_id
                                  AND h.event='checked'),
                  plan_note = (SELECT h.detail FROM history h
                                WHERE h.file_id=file_progress.file_id
                                  AND h.event='checked'
                                ORDER BY h.at DESC LIMIT 1),
                  plan_rev  = 0
                 WHERE EXISTS (SELECT 1 FROM history h
                                WHERE h.file_id=file_progress.file_id
                                  AND h.event='checked')
            """)
            out["stages"]["plan"] = cur.rowcount
    except Exception as e:                                   # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def summary() -> dict:
    """Library-wide: how many files are current for each stage."""
    out = {"files": 0, "stages": {}}
    try:
        init()
        sel = ", ".join(
            f"SUM(CASE WHEN p.{s}_at IS NOT NULL "
            f"          AND COALESCE(p.{s}_rev,-1)=COALESCE(f.rev,0) "
            f"     THEN 1 ELSE 0 END) {s}_ok, "
            f"SUM(CASE WHEN p.{s}_at IS NOT NULL THEN 1 ELSE 0 END) {s}_any"
            for s in STAGES)
        with cursor() as cur:
            r = cur.execute(
                f"SELECT COUNT(*) n, {sel} FROM files f "
                f"  LEFT JOIN file_progress p ON p.file_id=f.id "
                f" WHERE f.state!='deleted'").fetchone()
        out["files"] = int(r["n"] or 0)
        for s in STAGES:
            out["stages"][s] = {"current": int(r[f"{s}_ok"] or 0),
                                "ever": int(r[f"{s}_any"] or 0)}
    except Exception:                                        # noqa: BLE001
        pass
    return out
