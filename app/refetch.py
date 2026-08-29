r"""Reject the release behind a bad file and ask the arr for another one.

WHY THIS IS DELIBERATELY HARD TO REACH
--------------------------------------
Re-downloading is the right fix for exactly one kind of error: the file's
contents are bad. It is the wrong fix, and a destructive one, for every other
kind - and nuarr's own history says the wrong kind is overwhelmingly what
happens. Every failure in 46,446 jobs, at the time this was written:

    blocked   15x   path is 268 chars, over the 255 limit
    failed     1x   ffprobe returned nothing

The 15 are a NAMING problem. The path comes from the naming format applied to
series metadata, not from the release, so a replacement lands at the same
268-character path and blocks again - having deleted a perfectly good file and
spent an indexer call to do it. A button that offered itself there would be a
loop that destroys media once per pass.

So classification fails CLOSED. Only reasons that positively implicate the
file's contents return "content"; anything unrecognised returns "unknown" and
is refused. A new error string nobody has seen before must never arrive with a
delete button already attached to it.
"""
from __future__ import annotations

import re
import time

from .arr import shared_client
from .config import SETTINGS
from .db import cursor
from . import joblog

# Reasons where the bytes on disk are the problem. Re-downloading can help.
_CONTENT = [
    (r"ffprobe returned nothing", "ffprobe could not read the file at all"),
    (r"invalid data found", "the decoder rejected the stream"),
    (r"moov atom not found", "the file is missing its index and is truncated"),
    (r"unexpected end of file|truncat", "the file ends early"),
    (r"corrupt|damaged", "the stream is corrupt"),
    (r"no such stream|no video stream|missing video", "the expected video stream is absent"),
    (r"decode(r)? (error|failed)", "decoding failed"),
    (r"non-monotonous dts|invalid timestamp", "the timestamps are broken"),
]

# MOMENTARY FAULTS. Nothing is wrong with the file, the library or the rules -
# something was busy for a second. These are the ones worth RETRYING rather
# than reporting, and they are checked before everything else.
#
# "database is locked" is why this category exists. It is SQLite saying two
# writers collided; it says nothing about the media file at all. The policy
# list below matches the bare word "locked", so it was being read as "the file
# is locked or unreadable - free it and requeue" - a diagnosis about the wrong
# object entirely, offering a remedy that could not work. One such error sat in
# the Errors tile for 24 days waiting for a human to free a file that was never
# held.
_TRANSIENT = [
    (r"database is locked|database table is locked|database is busy",
     "the database was busy for a moment - nothing is wrong with the file"),
    (r"\bdisk i/?o error\b", "a disk read faltered"),
    (r"temporarily unavailable|resource busy|try again",
     "something was busy for a moment"),
    (r"the network (path|name) .*not found|network path was not found|"
     r"semaphore timeout|the specified network name is no longer available",
     "the share dropped for a moment"),
]

# Reasons where the file is fine and a replacement provably cannot help. These
# are listed explicitly rather than left to the default so the UI can say what
# WOULD fix them instead of just refusing.
_POLICY = [
    (r"path is \d+ chars|too long", "shorten the naming format in Profilarr - "
                                    "a replacement lands at the same path"),
    # "locked" is qualified now: a FILE lock, not any sentence containing the
    # word. See _TRANSIENT above for the one that taught this lesson - but
    # note the first alternative here, because tightening this pattern the
    # obvious way dropped Windows' OWN wording for a file lock ("cannot access
    # the file because it is being used by another process"), which does not
    # contain "locked" at all and had been landing on "permission" by luck.
    (r"being used by another process|cannot access the file|"
     r"permission|access is denied|sharing violation|"
     r"file is (currently )?(in use|locked)|\block(ed)? by another",
     "the file is locked or unreadable - free it and requeue"),
    (r"disk full|no space", "free space on the destination and requeue"),
    (r"collision|already exists", "resolve the destination collision and requeue"),
    (r"cancelled|canceled|shutting down", "just requeue it"),
    (r"no work needed|skipped", "nothing was wrong with it"),
]


def classify(reason: str | None) -> tuple[str, str]:
    """-> ("transient"|"content"|"policy"|"unknown", human explanation).

    Order matters. TRANSIENT is checked first, because those strings contain
    words the other lists match on - "database is locked" is the whole reason
    the category exists - and a momentary fault must never be reported as a
    fault of the file.

    Policy is checked before content for the same reason at one remove:
    "ffprobe returned nothing because access is denied" is a lock, not a
    corrupt file, and the lock reading is the safe one to act on.
    """
    r = (reason or "").lower().strip()
    if not r:
        return "unknown", "no reason was recorded"
    for pat, why in _TRANSIENT:
        if re.search(pat, r):
            return "transient", why
    for pat, why in _POLICY:
        if re.search(pat, r):
            return "policy", why
    for pat, why in _CONTENT:
        if re.search(pat, r):
            return "content", why
    return "unknown", "this error has not been seen before - refusing to guess"


def _file_row(file_id: int) -> dict | None:
    with cursor() as cur:
        row = cur.execute(
            "SELECT id,path,title,size,state,state_reason,arr_file_id,"
            "arr_parent_id,library FROM files WHERE id=?", (file_id,)).fetchone()
    return dict(row) if row else None


def _arr_for(library: str):
    """Which arr owns this library.

    ArrConfig carries no library list, so the mapping goes through
    SETTINGS.libraries, which does record kind ("tv"/"movie"). Falling back to
    a guess from the library NAME would be wrong for "Anime Shows", which is
    tv but does not say so.
    """
    lib = next((l for l in SETTINGS.libraries if l.name == library), None)
    if lib is None:
        return None
    want = "radarr" if lib.kind == "movie" else "sonarr"
    return next((c for c in SETTINGS.arrs
                 if c.enabled and c.api_key and c.kind == want), None)


async def plan(file_id: int) -> dict:
    """What WOULD happen. Never changes anything.

    The UI calls this before showing a confirmation, so every refusal reason
    has to come back as data rather than an exception.
    """
    row = _file_row(file_id)
    if not row:
        return {"ok": False, "why": "no such file"}
    kind, why = classify(row.get("state_reason"))
    out = {"ok": False, "file_id": file_id, "title": row.get("title"),
           "path": row.get("path"), "size": row.get("size"),
           "state": row.get("state"), "reason": row.get("state_reason"),
           "kind": kind, "explain": why}
    if kind != "content":
        out["why"] = ("re-downloading cannot fix this" if kind == "policy"
                      else "unrecognised error")
        out["remedy"] = why
        return out
    if not row.get("arr_file_id") or not row.get("arr_parent_id"):
        out["why"] = "no arr record for this file, so there is no release to reject"
        return out
    cfg = _arr_for(row.get("library") or "")
    if not cfg:
        out["why"] = "no enabled arr covers this library"
        return out
    client = shared_client(cfg)
    try:
        scene = await client.file_scene_name(int(row["arr_file_id"]))
        grab = await client.find_grab(int(row["arr_parent_id"]),
                                      int(row["arr_file_id"]), scene)
    except Exception as e:
        out["why"] = f"could not read {cfg.name} history: {type(e).__name__}: {e}"
        return out
    if not grab:
        # The common case, not an error: roughly four files in five have no
        # surviving grab. Say plainly what is lost by proceeding anyway, because
        # a search with nothing blocklisted can hand back the same bad release -
        # and if it is the only one indexed, will.
        out.update(
            arr=cfg.name, can_search=True, degraded=True,
            why=("no grab record survives for this file, so there is nothing "
                 "to blocklist"),
            warning=("the file can be deleted and re-searched, but with no "
                     "blocklist entry the arr may grab the SAME release again "
                     "- and will, if it is the only one indexed"))
        return out
    data = grab.get("data") or {}
    # A season pack rejection is not a per-episode action. Blocklisting it
    # affects every episode that came from the same download, which is a much
    # bigger consequence than the button implies - so it is surfaced, loudly.
    pack = str(data.get("releaseType") or "").lower() == "seasonpack"
    out.update(ok=True, arr=cfg.name, grab_id=grab.get("id"),
               release=grab.get("sourceTitle"), indexer=data.get("indexer"),
               release_type=data.get("releaseType"), season_pack=pack,
               can_search=True)
    if pack:
        out["warning"] = ("this came from a SEASON PACK - blocklisting it "
                          "rejects the release for every episode in that pack, "
                          "not just this file")
    return out


async def run(file_id: int, delete_file: bool = True) -> dict:
    """Blocklist the release and search for a replacement. Destructive.

    Re-plans rather than trusting whatever the caller was shown: the panel may
    have been open for a while, and the file may have been fixed or removed in
    the meantime.
    """
    p = await plan(file_id)
    if not p.get("ok") and not p.get("can_search"):
        return p
    row = _file_row(file_id)
    cfg = _arr_for(row.get("library") or "")
    client = shared_client(cfg)
    did: list[str] = []

    # A 404 FROM THE ARR IS STALE BOOKKEEPING, NOT A FAILURE.
    #
    # The ids this flow holds - history grab id, episodefile/moviefile id -
    # are snapshots, and the arr is free to invalidate them at any time: an
    # upgrade deletes the old file record, history gets trimmed, a re-import
    # issues a new id. Observed live: DELETE /episodefile/401518 -> 404,
    # which aborted the whole flow BEFORE the search ran, so the user got a
    # raw HTTPStatusError and no replacement was ever requested. But a 404 on
    # a delete means the record is already gone - the exact state the delete
    # was trying to reach - and a 404 on the history mark just means that
    # route is unavailable, not that the file cannot be re-searched. Note it,
    # fall through, and always reach the search.
    def _is_404(e: Exception) -> bool:
        import httpx
        return (isinstance(e, httpx.HTTPStatusError)
                and e.response is not None
                and e.response.status_code == 404)

    try:
        blocklisted = False
        if p.get("grab_id"):
            try:
                await client.mark_failed(int(p["grab_id"]))
                did.append(f"blocklisted {p.get('release')!r} and asked "
                           f"{cfg.name} to search again")
                blocklisted = True
            except Exception as e:
                if not _is_404(e):
                    raise
                did.append("the grab is no longer in history (the arr has "
                           "moved on) - cannot blocklist it, searching anyway")
        if not blocklisted:
            if delete_file and row.get("arr_file_id"):
                try:
                    await client.delete_file(int(row["arr_file_id"]))
                    did.append("deleted the file record")
                except Exception as e:
                    if not _is_404(e):
                        raise
                    did.append("the arr no longer tracks this file (already "
                               "replaced or removed) - nothing to delete; the "
                               "old file on disk will be superseded by the "
                               "import or flagged by the duplicate sweep")
            await client.search_for(int(row["arr_parent_id"]))
            did.append(f"asked {cfg.name} to search for a replacement")
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}", "did": did}
    with cursor() as cur:
        cur.execute("UPDATE files SET state='deleted', state_reason=?, "
                    "updated_at=? WHERE id=?",
                    (f"rejected and re-searched: {'; '.join(did)}",
                     time.time(), file_id))
    joblog.log(f"REFETCH {row.get('title')}: {'; '.join(did)}", "warn")
    return {"ok": True, "did": did, "title": row.get("title"),
            "release": p.get("release")}
