r"""nuarr - the readers, as jobs.

THREE READERS, ONE ARRANGEMENT. The Whisper listener (what language is this
track), the picture sampler (are there words burned into this file) and the
track reader (does this subtitle title describe what the track carries) each
ran on a schedule of their own - a pass, a sleep, a bar on their own page. Erik
asked for them under the main queue, beside everything else, on disks that are
not busy. So each becomes a job kind here:

    listen    one job per FILE, every unheard track in it, Whisper on the GPU
    subread   one job per picture sample, or per track read

WHY A FILE AND NOT A TRACK, FOR THE LISTENER. Whisper reads five windows per
track off the same container; two tracks in one file are one open and two
reads, and one card on the page rather than two saying the same name. The
sampler and the track reader are already one thing per row.

WHAT THIS MODULE DOES NOT DO. It does not know how to listen, sample or read -
audiolang, hardsub and subtitletitle still do all of that, unchanged, and their
"check some now" buttons still work as batches. What moved here is the
SCHEDULING: which file next, on which disk, how many at once, and where the
work shows up. The feeders deal untested rows round robin by spindle into the
jobs table - the lesson the subtitle queue learned when 3,302 of 5,300 files
sat on one disk - and the dispatcher does the rest, the same as for a
transcode: quietest disk first, never a viewer's, never two heavy reads on one
spindle.
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from . import joblog
from .db import cursor

# How many of each to keep on the main queue at once. The list is not the
# queue: there are fourteen thousand unheard tracks and putting all of them in
# the jobs table would make the queue panel a scrollbar.
DEPTH = {"listen": 60, "subread": 120}
FEED_S = 60.0

STATE: dict = {"listen": {"fed": 0, "on_queue": 0, "at": 0.0},
               "subread": {"fed": 0, "on_queue": 0, "at": 0.0}}
_SUBREAD_EMPTY: dict = {"at": 0.0}


# ------------------------------------------------------------ the helpers --
def _live_ids() -> set:
    try:
        with cursor() as cur:
            return {int(r["file_id"]) for r in cur.execute(
                "SELECT file_id FROM jobs WHERE state IN ('queued','running') "
                "  AND file_id IS NOT NULL")}
    except Exception:                                            # noqa: BLE001
        return set()


def _have(kind: str) -> int:
    with cursor() as cur:
        return int(cur.execute(
            "SELECT COUNT(*) n FROM jobs WHERE kind=? "
            "  AND state IN ('queued','running')", (kind,)).fetchone()["n"] or 0)


def _disks_of(ids: list) -> dict:
    out: dict = {}
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = ",".join("?" * len(chunk))
        with cursor() as cur:
            for r in cur.execute(
                    f"SELECT id, COALESCE(pool_disk,'') d FROM files "
                    f" WHERE id IN ({q})", chunk):
                out[int(r["id"])] = r["d"] or ""
    return out


def _deal(rows: list, room: int, disk_of) -> list:
    """Round robin by spindle, oldest first within each."""
    by: dict = {}
    for r in rows:
        by.setdefault(disk_of(r) or "?", []).append(r)
    out: list = []
    lanes = [iter(v) for _k, v in sorted(by.items())]
    while lanes and len(out) < room:
        alive = []
        for it in lanes:
            if len(out) >= room:
                alive.append(it)
                continue
            try:
                out.append(next(it))
                alive.append(it)
            except StopIteration:
                pass
        lanes = alive
    return out


# -------------------------------------------------------------- listening --
def _listen_pending(limit: int) -> list:
    r"""Unheard tracks folded into files, freshest first.

    THE SAME THREE POPULATIONS run_once used, in the same order: what just
    landed, what has no tag at all, then what has a tag nobody has verified.
    Folded by file, and the file carries its tracks in the plan so the worker
    does not have to ask again.
    """
    from . import audiolang
    # Tidy the jump queue first: rows whose every track has since been
    # judged by some other path stay in the table forever otherwise, and
    # each one is re-examined on every feeder pass.
    try:
        audiolang.queue_sync()
    except Exception:                                    # noqa: BLE001
        pass
    todo = list(audiolang.queued(limit))
    if len(todo) < limit:
        todo += audiolang.pending(limit - len(todo))
    if len(todo) < limit:
        todo += audiolang.unverified(limit - len(todo))
    files: dict = {}
    order: list = []
    for t in todo:
        fid = int(t.get("file_id") or 0)
        if not fid:
            continue
        if fid not in files:
            order.append(fid)
            files[fid] = {"file_id": fid, "path": t.get("path") or "",
                          "library": t.get("library") or "", "tracks": [],
                          "jumped": bool(t.get("jumped"))}
        files[fid]["tracks"].append({"track": int(t.get("track") or 0),
                                     "tagged": t.get("tagged") or ""})
    disks = _disks_of(order)
    for fid in order:
        files[fid]["disk"] = disks.get(fid, "")
    return [files[f] for f in order]


def _listen_plan(f: dict) -> str:
    n = len(f["tracks"])
    gaps = sum(1 for t in f["tracks"] if not t.get("tagged"))
    return json.dumps({
        "listen": True, "rewrite": False,
        "tracks": f["tracks"], "jumped": bool(f.get("jumped")),
        "summary": (f"listen to {n} track{'s' if n != 1 else ''}"
                    + (f" - {gaps} with no tag" if gaps else "")),
        "actions": [
            {"kind": "listen",
             "what": (f"listen to track {t['track'] + 1}"
                      + (" and write the tag it has none of" if not t.get("tagged")
                         else f" and check the tag ({t['tagged']})")),
             "why": "five 30-second windows through Whisper's language "
                    "identifier; the confident windows have to agree",
             "detail": ""} for t in f["tracks"]],
    })


async def topup_listen(depth: int | None = None) -> dict:
    from . import audiolang, jobs
    _ = asyncio
    depth = DEPTH["listen"] if depth is None else int(depth)
    try:
        if not audiolang.available():
            return {"ok": False, "why": "detection is not installed"}
        have = await asyncio.to_thread(_have, "listen")
        room = max(0, depth - have)
        # NOT FOR A HANDFUL OF SLOTS. The pending query walks three
        # populations and measured fifteen seconds on this library; paying
        # that every minute to refill two slots would be most of what the
        # feeder does. It waits until a quarter of the depth has drained.
        if room < max(1, depth // 4):
            return {"ok": True, "made": 0, "on_queue": have}
        files = await jobs.in_work(_listen_pending, max(room * 6, 300))
        live = await asyncio.to_thread(_live_ids)
        files = [f for f in files if f["file_id"] not in live]
        rows = _deal(files, room, lambda f: f.get("disk"))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    # ONE TRANSACTION, OFF THE LOOP - see jobs.enqueue_many.
    r = await jobs.in_work(jobs.enqueue_many,
                           [{**f, "plan_json": _listen_plan(f)} for f in rows],
                           "listen", 80, "audio language")
    made = int(r.get("made") or 0)
    STATE["listen"].update(fed=made, on_queue=have + made, at=time.time())
    return {"ok": True, "made": made, "on_queue": have + made}


def listen_one(file_id: int, path: str, tracks: list, jumped: bool,
               on_stage=None) -> dict:
    r"""Listen to every unheard track in one file, and fill the blanks.

    run_once's loop body for one file, with the bookkeeping it did per pass
    done per file instead: an untagged track that came back confident gets its
    tag written now, the probe refreshed and the arrs told, rather than at the
    end of a batch that no longer exists. A tagged track that disagrees is only
    RECORDED - correcting it is audqueue's decision, made from these facts.
    """
    from . import audiolang
    heard = refused = 0
    tags: dict = {}
    n = len(tracks)
    for i, t in enumerate(tracks):
        tr = int(t.get("track") or 0)
        if on_stage:
            on_stage(f"listening to track {tr + 1}" + (f" of {n}" if n > 1 else ""),
                     (i / max(1, n)) * 100.0)
        try:
            d = audiolang.check(int(file_id), path, tr)
        except Exception as e:                                   # noqa: BLE001
            return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
        if d.get("ok") and d.get("code"):
            heard += 1
            if not t.get("tagged"):
                tags[tr] = d["code"]
        else:
            refused += 1
    applied = ""
    if tags and audiolang.can_fast_path(path):
        if on_stage:
            on_stage("writing the tag it had none of", 95.0)
        ok, why = audiolang.apply_and_restamp(int(file_id), path, tags)
        if ok:
            audiolang._reprobe_quiet(int(file_id), path)
            try:
                audiolang.notify_arrs([int(file_id)])
            except Exception:                                    # noqa: BLE001
                pass
            applied = ", ".join(f"a:{k}={v}" for k, v in sorted(tags.items()))
        else:
            applied = f"could not write the tag: {why}"
    if jumped:
        try:
            audiolang.unqueue({int(file_id)})
        except Exception:                                        # noqa: BLE001
            pass
    try:
        audiolang.pending_invalidate()
    except Exception:                                            # noqa: BLE001
        pass
    return {"ok": True, "heard": heard, "refused": refused, "tags": tags,
            "why": (f"heard {heard} of {n}"
                    + (f", {refused} refused" if refused else "")
                    + (f" - tagged {applied}" if tags and applied
                       and not applied.startswith("could") else "")
                    + (f" - {applied}" if applied.startswith("could") else ""))}


# ------------------------------------------------------- subtitle readers --
def _subread_pending(limit: int) -> list:
    from . import hardsub, subtitletitle as stt
    out = []
    try:
        for r in hardsub._pending()[:limit]:
            out.append({"reader": "picture", "file_id": int(r["file_id"]),
                        "path": r.get("path") or "",
                        "disk": r.get("pool_disk") or "", "row": dict(r)})
    except Exception:                                            # noqa: BLE001
        pass
    try:
        for r in stt._pending()[:limit]:
            out.append({"reader": "track", "file_id": int(r["file_id"]),
                        "path": r.get("path") or "",
                        "disk": r.get("pool_disk") or "", "row": dict(r)})
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _subread_plan(r: dict) -> str:
    from . import hardsub
    if r["reader"] == "picture":
        return json.dumps({
            "subread": "picture", "rewrite": False, "row": r["row"],
            "summary": "sample the picture for burned-in subtitles",
            "actions": [{"kind": "subread",
                         "what": f"sample {hardsub.SAMPLES} frames and show the "
                                 f"bright text low in the picture to the OCR",
                         "why": "the file reports no subtitle track, and words "
                                "burned into the image are still subtitles",
                         "detail": ""}]})
    tr = int(r["row"].get("track") or 0)
    return json.dumps({
        "subread": "track", "rewrite": False, "row": r["row"],
        "summary": f"read subtitle track {tr + 1}'s events",
        "actions": [{"kind": "subread",
                     "what": f"read the events of subtitle track {tr + 1} and "
                             f"judge what it carries",
                     "why": "its title contradicts its cue rate, and only the "
                            "events can settle which is lying",
                     "detail": ""}]})


async def topup_subread(depth: int | None = None) -> dict:
    from . import jobs
    depth = DEPTH["subread"] if depth is None else int(depth)
    try:
        have = await asyncio.to_thread(_have, "subread")
        room = max(0, depth - have)
        if room < max(1, depth // 4):
            return {"ok": True, "made": 0, "on_queue": have}
        # NOT EVERY MINUTE FOR NOTHING. The two readers' pending lists cost
        # 3.6 s together and are almost always empty on a library that has
        # been read; an empty answer is believed for ten minutes.
        if _SUBREAD_EMPTY["at"] and time.time() - _SUBREAD_EMPTY["at"] < 600:
            return {"ok": True, "made": 0, "on_queue": have}
        rows = await jobs.in_work(_subread_pending, max(room * 8, 400))
        live = await asyncio.to_thread(_live_ids)
        rows = [r for r in rows if r["file_id"] not in live]
        _SUBREAD_EMPTY["at"] = time.time() if not rows else 0.0
        rows = _deal(rows, room, lambda r: r.get("disk"))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    r = await jobs.in_work(jobs.enqueue_many,
                           [{**x, "plan_json": _subread_plan(x)} for x in rows],
                           "subread", 80, "subtitle kinds")
    made = int(r.get("made") or 0)
    STATE["subread"].update(fed=made, on_queue=have + made, at=time.time())
    return {"ok": True, "made": made, "on_queue": have + made}


def subread_one(reader: str, row: dict, on_stage=None) -> dict:
    from . import hardsub, subtitletitle as stt
    if reader == "picture":
        if on_stage:
            on_stage(f"sampling {hardsub.SAMPLES} frames", 0.0)
        r = hardsub._do_one(row)
        if r.get("ok"):
            r["why"] = {"none": "nothing in the picture",
                        "signs": "signs or songs in the picture",
                        "dialogue": "DIALOGUE burned into the picture",
                        "hybrid": "dialogue and signs in the picture"}.get(
                str(r.get("state") or ""), f"read as {r.get('state')}")
        return r
    if on_stage:
        on_stage("reading the subtitle events", 0.0)
    r = stt._do_one(row)
    if r.get("ok"):
        r["why"] = ("signs after all - cleared" if r.get("cleared")
                    else "the events say dialogue - it stays on the list")
    return r


# --------------------------------------------------------------- feeding --
async def watch() -> None:
    """Keep both queues topped up. Nothing else."""
    await asyncio.sleep(150)
    while True:
        try:
            await topup_listen()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            await topup_subread()
        except Exception:                                        # noqa: BLE001
            pass
        # THE MODEL GIVES ITS VRAM BACK WHEN THERE IS NOTHING TO HEAR. run_once
        # unloaded at the end of every pass; there are no passes now, so the
        # feeder does it when the listen queue has drained - the GPU is for
        # encoding the rest of the time.
        try:
            from . import audiolang
            if not await asyncio.to_thread(_have, "listen") \
                    and getattr(audiolang, "_MODEL", None) is not None:
                await asyncio.to_thread(audiolang.unload)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(FEED_S)


def queue_counts(kind: str) -> dict:
    """How many of this kind are queued and running, for the pages' strips."""
    out = {"queued": 0, "running": 0, "now": ""}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT state, COUNT(*) n FROM jobs WHERE kind=? "
                    "  AND state IN ('queued','running') GROUP BY state",
                    (kind,)):
                out[r["state"]] = int(r["n"] or 0)
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            if getattr(w.job, "kind", "") == kind:
                out["now"] = os.path.basename(w.job.path or "")[:90]
                break
    except Exception:                                            # noqa: BLE001
        pass
    return out


_ = joblog
