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

import os
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
    # A full cache is a condition of the MACHINE, not the file, and it
    # clears by itself: finishing jobs release their working copies and
    # the housekeeping sweep removes anything left behind. Sixty-one
    # files sat in Errors with "refusing to guess" for exactly this.
    (r"free on the cache", "the cache was full - it is swept and the "
                           "file retried once there is room"),
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


# NOT A VIDEO FILE AT ALL. A disc image, an archive, an installer - the arr
# imported it because the release said it was a movie, and no amount of
# retrying will make ffmpeg read a .iso. The error text is useless here
# ("ffmpeg exited 4294967262"), so the extension is what classifies it, and
# it is checked before every other list: nothing in the reason can make a
# disc image fine.
NOT_MEDIA = {
    ".iso", ".img", ".bin", ".cue", ".nrg", ".mdf", ".mds", ".dmg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".exe", ".msi", ".apk", ".dll", ".lnk", ".url", ".html", ".htm",
    ".txt", ".nfo", ".srr", ".sfv", ".par2", ".torrent",
}


def not_media(path: str | None) -> str | None:
    """The reason a path is not a playable video, or None if it might be."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in NOT_MEDIA:
        what = ("a disc image" if ext in (".iso", ".img", ".bin", ".cue", ".nrg",
                                          ".mdf", ".mds", ".dmg")
                else "an archive" if ext in (".zip", ".rar", ".7z", ".tar",
                                             ".gz", ".bz2", ".xz")
                else "not a video file")
        return (f"this is {what} ({ext}), not a video file - only a different "
                f"release can fix it")
    return None


def classify(reason: str | None, path: str | None = None) -> tuple[str, str]:
    """-> ("transient"|"content"|"policy"|"unknown", human explanation).

    `path` lets the extension speak first: a .iso is "content" whatever the
    error text says, because the bytes on disk are the wrong kind of bytes.

    Order matters. TRANSIENT is checked first, because those strings contain
    words the other lists match on - "database is locked" is the whole reason
    the category exists - and a momentary fault must never be reported as a
    fault of the file.

    Policy is checked before content for the same reason at one remove:
    "ffprobe returned nothing because access is denied" is a lock, not a
    corrupt file, and the lock reading is the safe one to act on.
    """
    nm = not_media(path)
    if nm:
        return "content", nm
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


async def plan(file_id: int, reason: str = "") -> dict:
    """What WOULD happen. Never changes anything.

    The UI calls this before showing a confirmation, so every refusal reason
    has to come back as data rather than an exception.
    """
    row = _file_row(file_id)
    if not row:
        return {"ok": False, "why": "no such file"}
    # A CALLER MAY BRING ITS OWN REASON. classify() reads state_reason, which
    # is how the pipeline records a FAILURE - and a file the rule check flags
    # has not failed anything: it imported cleanly, plays fine, and is simply
    # the wrong release. Its state_reason is empty, so classify returned
    # "unrecognised error" and the panel was told re-downloading cannot fix a
    # file for which re-downloading is the only fix. The audit passes its rule
    # here instead; nothing else about the flow changes.
    if reason:
        kind, why = "content", reason
    else:
        kind, why = classify(row.get("state_reason"), row.get("path"))
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


# WHY THE QUESTION MENTIONS THE ORDER. Somebody reading the confirmation is
# deciding whether to destroy 5 GB; the fact that the delete has to happen
# before the search is the reason the button works at all, and it is not
# guessable from the outside.
def order_note(arr: str = "") -> str:
    return (f"The file is deleted first: {arr or 'the arr'} scores every "
            f"candidate against whatever is still on disk, so leaving it there "
            f"makes the replacement search reject its own results.")


async def run(file_id: int, delete_file: bool = True,
              reason: str = "") -> dict:
    r"""Delete the file, blocklist the release, search for a replacement.

    Re-plans rather than trusting whatever the caller was shown: the panel may
    have been open for a while, and the file may have been fixed or removed in
    the meantime.

    THE FILE GOES FIRST, AND THAT ORDER IS THE WHOLE POINT.
    ------------------------------------------------------
    This used to blocklist and stop. Blocklisting tells the arr "not that
    release again"; it does not tell it "and the thing on disk is rubbish". So
    the automatic search that markFailed queues ran while the corrupt file was
    still imported, and the arr judged every candidate against it - quality
    profile cutoff, and in Erik's setup a custom-format score. A 2160p HULU
    WEB-DL scores high whether or not it decodes, so the replacement search
    found nothing "better" and quietly declined to grab anything.

    From the outside that is indistinguishable from success: the release IS
    blocklisted, the search DID run, and nothing arrives. Four episodes of
    Reasonable Doubt sat like that.

    The arr cannot score a file that is not there, so deleting first is what
    makes the search able to accept anything at all. It also makes the
    confirmation honest - it always said "5.24 GB is deleted", and on this
    path nothing was.

    A DELETE THAT FAILS STOPS THE FLOW, deliberately. If the file is still
    there the search cannot succeed, so spending an indexer call on it would
    burn the request and report a lie. Only a 404 passes, and a 404 means the
    record is already gone - the state the delete was aiming for.
    """
    p = await plan(file_id, reason=reason)
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
        # ONE: THE FILE. Removes it from disk and from the arr's database, so
        # the episode has nothing for a replacement to be measured against.
        # This is the step that has to come first; see the docstring.
        if delete_file and row.get("arr_file_id"):
            try:
                await client.delete_file(int(row["arr_file_id"]))
                did.append("deleted the file")
            except Exception as e:
                if not _is_404(e):
                    raise
                did.append("the arr no longer tracks this file (already "
                           "replaced or removed) - nothing to delete")

        # TWO: THE RELEASE. markFailed blocklists it AND queues the arr's own
        # search, which is why the search below is a fallback rather than a
        # second request - the arr's search is aimed at the right episode,
        # while search_for() with no episode ids is a whole-series sweep.
        searched = False
        if p.get("grab_id"):
            try:
                await client.mark_failed(int(p["grab_id"]))
                did.append(f"blocklisted {p.get('release')!r} and asked "
                           f"{cfg.name} to search again")
                searched = True
            except Exception as e:
                if not _is_404(e):
                    raise
                did.append("the grab is no longer in history (the arr has "
                           "moved on) - cannot blocklist it, searching anyway")

        # THREE: THE SEARCH, when nothing above asked for one. Either there
        # was no grab to blocklist, or the grab had aged out of history.
        if not searched:
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
    # A DELETED FILE IS A CHANGED FOLDER. The arr removes the release and goes
    # looking for another; Plex, told nothing, keeps offering an item whose
    # file is gone until its own scan notices. The replacement announces itself
    # later through the ordinary commit path.
    try:
        from . import plexqueue
        plexqueue.enqueue(file_id, row.get("path") or "",
                          why="the release was rejected and deleted")
    except Exception:                                        # noqa: BLE001
        pass
    return {"ok": True, "did": did, "title": row.get("title"),
            "release": p.get("release")}
