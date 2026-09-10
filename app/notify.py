r"""One place that says: this file on disk is not what it was.

WHY THIS IS A MODULE AND NOT A HABIT.

Six systems in nuarr change a file without going through the transcode
pipeline. The pipeline tells everybody - it queues a Plex scan and a rename
the moment a job commits - but the six shortcuts each grew their own idea of
whose business it was, and the answers did not agree:

    rebuild (jobs)         Plex, arr rescan, rename        everything
    audio language         arr rescan, rename              Plex only if the
                                                           rename happened
    hardsub marker track   nothing                         and it REMUXES the
                                                           whole container
    subtitle titles        nothing
    audio titles           nothing

The marker track is the one that shows why it matters. Its entire purpose is
to tell Bazarr and Plex "this file is handled" - and it was rewriting the
container with mkvmerge, replacing the file, and then telling neither. Plex
went on serving a stream list for a file that no longer had those streams,
which is exactly the fault that makes a player offer a subtitle track that is
not there.

So there is one function, and everything that writes to a file calls it.

WHAT EACH OF THE THREE IS FOR, because they are not interchangeable:

  Plex    caches the stream list AND the track languages in its own metadata.
          A header edit is invisible to it until its own scan comes round,
          which on this server can be a day.
  the arr caches a file's audio languages too, and shows them in its UI. A
          rescan makes it re-read the file; a REFRESH would re-fetch the
          show's metadata, which is not what changed.
  rename  only when the change can move the file: Sonarr and Radarr naming
          formats can carry the audio languages - "[JA]" on this library - so
          a file imported while its track read English may now be named
          wrongly. A subtitle title cannot do that, so it does not ask.

DEBOUNCED PER PARENT, NOT PER FILE. A batch that corrects 400 tracks across 20
series must not fire 400 rescans. Plex is queued per file because the queue
itself coalesces and knows about paths; the arr is told once per series per
two minutes.
"""
from __future__ import annotations

import time

from . import joblog
from .db import cursor

# One rescan per series per this many seconds, however many files changed.
ARR_DEBOUNCE_S = 120.0
_ARR_TOLD: dict[tuple, float] = {}


def _prune(now: float) -> None:
    """Drop entries that can no longer say anything but yes-go-ahead.

    This map answers one question - "did we tell this arr about this series in
    the last two minutes" - so an entry older than the window is garbage.
    Nothing pruned it in its first home, so it held one entry per series ever
    touched for as long as the process lived: small, but unbounded in the only
    direction that matters.
    """
    if len(_ARR_TOLD) <= 256:
        return
    for k in [k for k, t in _ARR_TOLD.items() if now - t > ARR_DEBOUNCE_S]:
        _ARR_TOLD.pop(k, None)


def file_changed(file_ids, why: str = "nuarr changed this file", *,
                 plex: bool = True, arrs: bool = True, rename: bool = False,
                 system: str = "") -> dict:
    r"""Tell whoever caches something about these files that it is stale.

    Everything here is best-effort and none of it blocks the caller: Plex and
    the rename go onto queues that already know how to retry, and the arr call
    is scheduled on the loop if there is one. A file that has just been
    corrected must not be un-corrected because Sonarr was down.
    """
    ids = [int(i) for i in (file_ids or []) if i]
    out = {"plex": 0, "arrs": 0, "renames": 0}
    if not ids:
        return out
    try:
        with cursor() as cur:
            ph = ",".join("?" * len(ids))
            rows = [dict(r) for r in cur.execute(
                f"SELECT id, path, arr_name, arr_parent_id FROM files "
                f" WHERE id IN ({ph})", tuple(ids))]
    except Exception:                                            # noqa: BLE001
        return out

    # ---- Plex, per file. Idempotent, and a no-op when no Plex is configured.
    if plex:
        try:
            from . import plexqueue
            for f in rows:
                if f.get("path"):
                    plexqueue.enqueue(int(f["id"]), f["path"], why=why)
                    out["plex"] += 1
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"could not queue a Plex scan: {str(e)[:90]}",
                       "warn", system=system or "notify")

    # ---- the rename queue, per file. It waits for the rescan, backs off,
    # retries, and refuses to act on a file the arr cannot see. Doing any of
    # that here would be a second, worse copy of it.
    if rename:
        try:
            from . import renamequeue
            for f in rows:
                if f.get("arr_name") and f.get("arr_parent_id"):
                    renamequeue.enqueue(int(f["id"]), f["arr_name"],
                                        f["arr_parent_id"], f["path"] or "",
                                        why=why)
                    out["renames"] += 1
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"could not queue a rename: {str(e)[:90]}",
                       "warn", system=system or "notify")

    # ---- the arrs, per series, debounced.
    if not arrs:
        return out
    now = time.time()
    _prune(now)
    todo = []
    for f in rows:
        if not (f.get("arr_name") and f.get("arr_parent_id")):
            continue
        key = (f["arr_name"], f["arr_parent_id"])
        if now - _ARR_TOLD.get(key, 0.0) < ARR_DEBOUNCE_S:
            continue
        _ARR_TOLD[key] = now
        todo.append(key)
    if not todo:
        return out

    async def _go() -> int:
        from .arr import ArrClient
        from .config import SETTINGS
        n = 0
        for name, pid in todo:
            cfg = next((c for c in SETTINGS.arrs if c.name == name), None)
            if not cfg:
                continue
            client = ArrClient(cfg)
            try:
                # A RESCAN, not a refresh: the file on disk changed, the
                # show's metadata did not.
                await client.notify_file_changed(pid)
                n += 1
            except Exception as e:                               # noqa: BLE001
                joblog.log(f"could not tell {name} the file changed: "
                           f"{str(e)[:90]}", "warn", system=system or "notify")
            finally:
                try:
                    await client.close()
                except Exception:                                # noqa: BLE001
                    pass
        return n

    try:
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            out["arrs"] = asyncio.run(_go())   # plain thread: own loop is fine
            return out
        # Already on the event loop - schedule it and do not block the caller.
        asyncio.create_task(_go())
        out["arrs"] = len(todo)
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"arr notify failed: {str(e)[:90]}",
                   "warn", system=system or "notify")
    return out
