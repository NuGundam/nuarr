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

# When the verdict table was last swept, and what went. See watch().
_PRUNE: dict = {"at": 0.0, "last": {}}
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


def _policy_of(ids: list) -> dict:
    """library, audio tags and original language for a batch of files."""
    out: dict = {}
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = ",".join("?" * len(chunk))
        with cursor() as cur:
            for r in cur.execute(
                    f"SELECT id, COALESCE(library,'') lib, "
                    f"       COALESCE(audio_langs,'') langs, "
                    f"       COALESCE(orig_lang,'') orig "
                    f"  FROM files WHERE id IN ({q})", chunk):
                out[int(r["id"])] = (r["lib"], r["langs"], r["orig"])
    return out


def _codes_from_probe(file_id: int) -> list:
    """Every audio track's language tag, in track order, out of the probe."""
    if not file_id:
        return []
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if not r:
            return []
        d = json.loads(r["json"] or "{}")
    except Exception:                                        # noqa: BLE001
        return []
    out = []
    for st in (d.get("streams") or []):
        if st.get("codec_type") != "audio":
            continue
        out.append(str((st.get("tags") or {}).get("language") or "-").strip())
    return out


def _split_spare(f: dict, lib: str, langs: str, orig: str) -> None:
    r"""Set aside the tracks nothing is going to keep.

    Erik: "whisper should not waste time on listening to tracks that will be
    removed". The Strongest Sage S01E07 is the case: six audio tracks, Whisper
    heard all six, and ninety seconds later the rewrite said "remove audio 0
    (rus); remove audio 1 (rus); remove audio 2 (ger); remove audio 3 (por)".
    Four tracks read to decide nothing.

    WHY IT IS SAFE TO SKIP THEM, AND THE EXACT CONDITION. The listener exists
    because a tag can LIE - Children of the Sea carried a second Japanese track
    wearing "eng", and the rewrite dropped the wrong one. So a track cannot be
    skipped merely for being unwanted: if its "rus" is a lie, the English may
    be in it.

    What makes it safe is that the file ALSO claims a track in every language
    the policy keeps, and those tracks are the ones being listened to right
    now. If they hold up, the spares carry nothing anyone wants, whatever they
    really are. If they do NOT hold up - a wanted language claimed by a tag and
    not confirmed by the audio - the spares are heard after all, in the same
    job, before it finishes. See listen_one().

    And the cases where nothing is skipped at all, because nothing is dropped:
      * one audio track - always kept;
      * keep_original set and the original language unknown - rules keeps
        every track rather than guessing;
      * no track in any kept language - the never-go-silent guard in
        rules.decide() keeps the whole file.
    """
    from . import langkey, langpolicy, origlang
    codes = [c.strip() for c in (langs or "").split(",")] if langs else []
    if len(codes) < 2:
        # THE FILE'S OWN PROBE, WHEN THE COLUMN IS NOT FILLED IN YET.
        #
        # This read files.audio_langs and gave up when it was empty, which
        # is exactly the state a file is in when it has just landed - and a
        # file that has just landed is the commonest thing in this queue.
        # A Minecraft Movie was planned in that window: 40 tracks, one of
        # them English, and the plan set aside none of them. Re-running the
        # same split against the same file an hour later, with the column
        # populated, set aside 40 of 44.
        #
        # The plan is written once and the job carries it for its whole
        # life, so "the column will be filled in by the next pass" does not
        # help the file being planned right now. The probe has the same
        # answer and is there from the moment the file is scanned.
        codes = _codes_from_probe(int(f.get("file_id") or 0))
    if len(codes) < 2:
        return
    pol = langpolicy.for_library(lib, "audio")
    keep = list(pol.get("langs") or [])
    if pol.get("keep_original"):
        oc = origlang.codes_for(orig)
        if not oc:
            return
        keep += list(oc)
    if not keep:
        return
    want = langkey.expand(keep)
    wanted_at = {i for i, c in enumerate(codes)
                 if c and c != "-" and (langkey.key(c) in want
                                        or c.lower() in want)}
    if not wanted_at:
        return
    spare, keepers = [], []
    for t in f.get("tracks") or []:
        i = int(t.get("track") or 0)
        # THE TRACK'S OWN TAG, WITH THE INDEX ONLY AS A FALLBACK.
        #
        # This asked whether the track's INDEX was in wanted_at, and a track
        # whose index fell outside the code list - `0 <= i < len(codes)` -
        # was kept by default. That guard was written for a short or missing
        # audio_langs, and it silently turned into "listen to it" for every
        # track past the end. On the doubled Minecraft plan that was half of
        # them. The tag is carried on the row already and says the same
        # thing without needing the two lists to line up.
        tag = (t.get("tagged") or "").strip()
        if not tag and 0 <= i < len(codes):
            tag = codes[i] if codes[i] != "-" else ""
        # An UNTAGGED track is never spare: it has no claim to disbelieve, and
        # its language is exactly what nobody knows yet.
        if tag and not (langkey.key(tag) in want or tag.lower() in want):
            spare.append(t)
        else:
            keepers.append(t)
    if spare and keepers:
        f["tracks"], f["spare"] = keepers, spare


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
    # A TRACK CAN BE IN TWO OF THE THREE POPULATIONS AT ONCE, and nothing
    # noticed. queued() is "somebody asked for this file now"; unverified()
    # is "this tag has never been checked"; a jumped file whose tags are also
    # unverified is in both, and the two lists were simply concatenated.
    #
    # Measured on A Minecraft Movie, 40 audio tracks: queued returned 22,
    # pending 2, unverified 20 - 44 entries for 22 distinct tracks. The job
    # that came out said "listen to 80 tracks", every one of the 40 twice,
    # and Whisper listened to all of them: ten 30-second windows per track
    # instead of five, for forty tracks, on a file with one English track.
    #
    # First wins, which keeps the priority the three queries were ordered in:
    # what was asked for, then what has no tag, then what is merely unchecked.
    seen_tracks: set = set()
    for t in todo:
        fid = int(t.get("file_id") or 0)
        if not fid:
            continue
        key = (fid, int(t.get("track") or 0))
        if key in seen_tracks:
            continue
        seen_tracks.add(key)
        if fid not in files:
            order.append(fid)
            files[fid] = {"file_id": fid, "path": t.get("path") or "",
                          "library": t.get("library") or "", "tracks": [],
                          "spare": [],
                          "jumped": bool(t.get("jumped")),
                          "blocking": False}
        # Any track of a blocking file makes the file blocking.
        if t.get("blocking"):
            files[fid]["blocking"] = True
        files[fid]["tracks"].append({"track": int(t.get("track") or 0),
                                     "tagged": t.get("tagged") or ""})
    disks = _disks_of(order)
    pols = _policy_of(order)
    for fid in order:
        files[fid]["disk"] = disks.get(fid, "")
        # THE TRACKS THE REWRITE IS ABOUT TO DELETE DO NOT NEED HEARING FIRST.
        # Never raises: a policy lookup failing means the file is listened to
        # in full, which is where it was a moment ago.
        try:
            _lib, _lg, _or = pols.get(fid, ("", "", ""))
            _split_spare(files[fid], _lib, _lg, _or)
        except Exception:                                        # noqa: BLE001
            pass
    return [files[f] for f in order]


def _listen_plan(f: dict) -> str:
    n = len(f["tracks"])
    gaps = sum(1 for t in f["tracks"] if not t.get("tagged"))
    sp = list(f.get("spare") or [])
    return json.dumps({
        "listen": True, "rewrite": False,
        "tracks": f["tracks"], "spare": sp, "jumped": bool(f.get("jumped")),
        # Recorded on the job so the queue row can say why it is at the front
        # even though nobody lifted it - it was born there. See api_queue.
        "blocking": bool(f.get("blocking")),
        "summary": (f"listen to {n} track{'s' if n != 1 else ''}"
                    + (f" - {gaps} with no tag" if gaps else "")
                    + (f", {len(sp)} left for the rewrite to delete"
                       if sp else "")),
        "actions": [
            {"kind": "listen",
             "what": (f"listen to track {t['track'] + 1}"
                      + (" and write the tag it has none of" if not t.get("tagged")
                         else f" and check the tag ({t['tagged']})")),
             "why": "five 30-second windows through Whisper's language "
                    "identifier; the confident windows have to agree",
             "detail": ""} for t in f["tracks"]]
        + [{"kind": "listen",
            "what": f"skip track {t['track'] + 1} ({t.get('tagged') or 'untagged'})",
            "why": "this library keeps none of that language and the rewrite "
                   "will drop the track; heard only if a language it does keep "
                   "turns out not to be here",
            "detail": ""} for t in sp],
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
        #
        # THE DEADBAND IS FOR A QUIET QUEUE, NOT A BLOCKED ONE. With sixty
        # slots it will not refill until fifteen are free, and a transcode
        # held on a file with no listen job waits out that whole drain -
        # measured: room 13 against a threshold of 15, so the feeder was
        # doing nothing at all while fifteen files stood still behind it.
        urgent = await asyncio.to_thread(audiolang.blocking_count)
        if room < max(1, depth // 4) and not urgent:
            return {"ok": True, "made": 0, "on_queue": have}
        if not room:
            room = max(1, min(urgent, 8))   # make space for the blocked ones
        files = await jobs.in_work(_listen_pending, max(room * 6, 300))
        live = await asyncio.to_thread(_live_ids)
        files = [f for f in files if f["file_id"] not in live]
        # THE BLOCKING ONES ARE DEALT FIRST AND KEEP THEIR OWN ROOM, so a
        # round robin over twelve spindles cannot spend the whole allowance
        # on files nobody is waiting for.
        stuck = [f for f in files if f.get("blocking")]
        other = [f for f in files if not f.get("blocking")]
        front = _deal(stuck, room, lambda f: f.get("disk"))
        rows = _deal(other, max(0, room - len(front)), lambda f: f.get("disk"))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    made = 0
    # ONE TRANSACTION, OFF THE LOOP - see jobs.enqueue_many. Two of them here:
    # the blocked files go in at the promotion priority, ahead of everything
    # the feeders queue, which is the entire point of noticing them.
    if front:
        from . import precedence as _prec
        rf = await jobs.in_work(
            jobs.enqueue_many,
            [{**f, "plan_json": _listen_plan(f)} for f in front],
            "listen", _prec.PROMOTE_TO, "audio language")
        made += int(rf.get("made") or 0)
    if rows:
        r = await jobs.in_work(jobs.enqueue_many,
                               [{**f, "plan_json": _listen_plan(f)} for f in rows],
                               "listen", 80, "audio language")
        made += int(r.get("made") or 0)
    STATE["listen"].update(fed=made, on_queue=have + made, at=time.time())
    return {"ok": True, "made": made, "on_queue": have + made}


def replan_listen(file_id: int, tracks: list, spare: list) -> tuple:
    r"""Judge the plan again, with today's facts, just before running it.

    A PLAN IS WRITTEN ONCE AND CARRIED FOR THE JOB'S WHOLE LIFE, and that is
    the right design until the facts it was made from arrive late. A file is
    queued for listening the moment it lands; files.audio_langs is filled in
    by the probe that follows; and a plan made in that window sets nothing
    aside because it cannot yet see what languages the file has.

    A Minecraft Movie is the case that found it. Planned at 09:46 with the
    column still empty: 40 audio tracks, one of them English, none set
    aside - and every track listed TWICE, because the three queues it was
    folded from overlap. Eighty listens, ten 30-second windows apiece, about
    an hour of Whisper on a file where the answer was one English track and
    two untagged ones. Re-judged here with the probe in hand: one track to
    listen to, eighteen set aside.

    So the plan is re-made at the start of the job rather than trusted. It
    can only ever shrink the work: the same split, the same policy, the same
    never-go-silent guards, and anything it sets aside is still heard later
    in this same job if the tags it kept turn out to be lies - see
    listen_one.
    """
    tracks = list(tracks or [])
    spare = list(spare or [])
    if not tracks:
        return tracks, spare
    # THE SAME TRACK TWICE IS ALWAYS WRONG, whatever the policy says.
    seen: set = set()
    deduped = []
    for t in tracks:
        k = int(t.get("track") or 0)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(t)
    f = {"file_id": int(file_id), "tracks": deduped, "spare": []}
    try:
        pol = _policy_of([int(file_id)]).get(int(file_id))
        if pol:
            _split_spare(f, pol[0], pol[1], pol[2])
    except Exception:                                        # noqa: BLE001
        # A re-judgement that fails leaves the plan exactly as it was, which
        # is where it was a moment ago.
        return deduped, spare
    # Whatever it set aside joins what the plan already had, so the late
    # hearing in listen_one still covers all of it.
    return f["tracks"], spare + [t for t in (f.get("spare") or [])]


def listen_one(file_id: int, path: str, tracks: list, jumped: bool,
               on_stage=None, spare: list | None = None) -> dict:
    r"""Listen to every unheard track in one file, and fill the blanks.

    run_once's loop body for one file, with the bookkeeping it did per pass
    done per file instead: an untagged track that came back confident gets its
    tag written now, the probe refreshed and the arrs told, rather than at the
    end of a batch that no longer exists. A tagged track that disagrees is only
    RECORDED - correcting it is audqueue's decision, made from these facts.
    """
    from . import audiolang, langkey
    heard = refused = 0
    tags: dict = {}
    got: dict = {}
    n = len(tracks)
    failed: dict = {}

    def _hear(rows: list, base: int, total: int) -> bool:
        nonlocal heard, refused
        for i, t in enumerate(rows):
            tr = int(t.get("track") or 0)
            _label = (f"listening to track {tr + 1}"
                      + (f" of {total}" if total > 1 else ""))
            if on_stage:
                on_stage(_label, ((base + i) / max(1, total)) * 100.0)

            # THE WINDOWS INSIDE THE TRACK MOVE THE BAR. Each track owns an
            # equal share; the model's tick says how far through that share
            # it is, so a one-track file no longer sits at 0% for the whole
            # listen and then jumps to done.
            # NEVER BACKWARDS. When the model takes its second look the
            # planned count jumps from five windows to eleven, and 5/5 would
            # become 5/11 - the bar sliding back on a job that is further
            # along than it was. Watched live: 25% then 11%. The share only
            # ever rises.
            _high = [0.0]

            def _tick(done, planned, _i=i):
                if on_stage and planned:
                    _high[0] = max(_high[0], min(1.0, done / planned))
                    on_stage(_label, ((base + _i + _high[0])
                                      / max(1, total)) * 100.0)
            try:
                d = audiolang.check(int(file_id), path, tr, tick=_tick)
            except Exception as e:                               # noqa: BLE001
                failed["why"] = f"{type(e).__name__}: {e}"[:200]
                return False
            got[tr] = d
            if d.get("ok") and d.get("code"):
                heard += 1
                if not t.get("tagged"):
                    tags[tr] = d["code"]
            else:
                refused += 1
        return True

    if not _hear(tracks, 0, n):
        return {"ok": False, "why": failed.get("why") or "listen failed"}

    # THE SPARES, BUT ONLY IF THE TAGS DID NOT HOLD UP.
    #
    # _split_spare set aside the tracks in languages this library keeps none
    # of, on the strength of the file ALSO claiming a track in a language it
    # does keep. That claim has now been tested. If every kept language the
    # tags promised is actually in the audio, the spares carry nothing anyone
    # wants and the job is done - four tracks of Whisper saved on The
    # Strongest Sage S01E07 alone.
    #
    # If one of them was a lie, the promise is void: the English this file
    # says it has is not where it said it was, and it may be sitting in a
    # track labelled Russian. So they are heard after all - here, in the same
    # job, before the rewrite is unheld and acts on any of it.
    late = 0
    if spare:
        claimed = {(t.get("tagged") or "").strip()
                   for t in tracks if (t.get("tagged") or "").strip()}
        confirmed = {str((got.get(int(t.get("track") or 0)) or {}).get("code")
                         or "") for t in tracks}
        confirmed.discard("")
        lost = sorted(c for c in claimed
                      if not any(langkey.same(c, h) for h in confirmed))
        if lost:
            late = len(spare)
            if not _hear(spare, n, n + late):
                return {"ok": False, "why": failed.get("why") or "listen failed"}
            n += late
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
                    + (f" - {late} more after a tag did not hold up" if late
                       else (f" - {len(spare)} skipped, the rewrite drops them"
                             if spare else ""))
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
    # AND THE TRACKS THE RAW CHECK CANNOT JUDGE. Its signs_unread rung says
    # the track "is queued to be read rather than guessed at" and nothing
    # was queuing it: the title check's candidates are tracks whose title
    # contradicts their cue rate, and these have no cue rate to contradict.
    # Ten Drug Store in Another World episodes sat on the raw list because
    # of it, every one carrying a full English script under a forced flag.
    try:
        from . import subneed as _sn
        have = {int(x["file_id"]) for x in out}
        for r in _sn.unread_tracks(limit):
            if int(r["file_id"]) in have:
                continue
            out.append({"reader": "track", "file_id": int(r["file_id"]),
                        "path": r.get("path") or "",
                        "disk": r.get("pool_disk") or "", "row": dict(r)})
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _subread_plan(r: dict) -> str:
    from . import hardsub
    if r["reader"] == "picture":
        # THE CARD NAMES THE ENGINE. The OCR is one setting for the whole
        # install now, so "the OCR" on a job card is a name withheld.
        try:
            from . import subocr as _so
            eng = _so.engine_name(hardsub.ocr_engine())
        except Exception:                                        # noqa: BLE001
            eng = "the OCR"
        return json.dumps({
            "subread": "picture", "rewrite": False, "row": r["row"],
            "summary": f"sample the picture for burned-in subtitles · {eng}",
            "actions": [{"kind": "subread",
                         "what": f"sample {hardsub.SAMPLES} frames and show the "
                                 f"bright text low in the picture to {eng}",
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
                     "why": (r["row"].get("why")
                             or "its title contradicts its cue rate, and only "
                                "the events can settle which is lying"),
                     "detail": ""}]})


async def feed_subread_now(depth: int | None = None) -> dict:
    """The button. Top the queue up this second, whatever the empty cache
    says - a person who pressed it has just changed something, or wants to
    see the readers move, and "nothing was pending ten minutes ago" is not an
    answer to that. The reads then run as subread jobs on the workers, where
    they show as cards, count on the disk panel, and stop when the queue is
    paused - which is why this replaced the in-process readers the button
    used to run."""
    _SUBREAD_EMPTY["at"] = 0.0
    return await topup_subread(depth)


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
        # Files a transcode is held on get their own room, then the round
        # robin as before - and they go in at the promotion priority, which is
        # the half that actually makes them run first.
        try:
            from . import precedence as _prec
            want = _prec.wanted("subread")
        except Exception:                                        # noqa: BLE001
            want = set()
        stuck = [r for r in rows if int(r.get("file_id") or 0) in want]
        other = [r for r in rows if int(r.get("file_id") or 0) not in want]
        front = _deal(stuck, room, lambda r: r.get("disk"))
        rows = _deal(other, max(0, room - len(front)), lambda r: r.get("disk"))
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    made = 0
    if front:
        from . import precedence as _prec2
        rf = await jobs.in_work(
            jobs.enqueue_many,
            [{**x, "plan_json": _subread_plan(x)} for x in front],
            "subread", _prec2.PROMOTE_TO, "subtitle kinds")
        made += int(rf.get("made") or 0)
    r = await jobs.in_work(jobs.enqueue_many,
                           [{**x, "plan_json": _subread_plan(x)} for x in rows],
                           "subread", 80, "subtitle kinds")
    made += int(r.get("made") or 0)
    STATE["subread"].update(fed=made, on_queue=have + made, at=time.time())
    return {"ok": True, "made": made, "on_queue": have + made}


def subread_one(reader: str, row: dict, on_stage=None) -> dict:
    r"""Read one file, saying where it has got to as it goes.

    on_stage(text, pct) is the job card's bar. It used to be called once with
    0.0 and then nothing for the next eighteen seconds, which the card drew as
    an indeterminate sweep and a row of dashes - it had nothing else to go on.
    Both readers report their real stages now; see hardsub.probe_one.
    """
    from . import hardsub, subtitletitle as stt, subneed as _sn
    told = _sn.FOUND.pop(int(row.get("file_id") or 0), "")
    if told and on_stage:
        on_stage(told, 0.0)
    if reader == "picture":
        if on_stage:
            on_stage(f"sampling {hardsub.SAMPLES} frames", 0.0)
        r = hardsub._do_one(row, report=on_stage)
        if r.get("ok"):
            r["why"] = {"none": "nothing in the picture",
                        "signs": "signs or songs in the picture",
                        "dialogue": "DIALOGUE burned into the picture",
                        "hybrid": "dialogue and signs in the picture"}.get(
                str(r.get("state") or ""), f"read as {r.get('state')}")
            if told:
                r["why"] = told + "; " + r["why"]
            # THE PICTURE CLOSES ITS LOOP TOO. A track-less file sits on the
            # raw check's nopic rung - "nothing has read the picture yet" -
            # and the moment the picture is read that sentence is false, but
            # the verdict kept saying it until the next sweep. After Read
            # again on Outcast's Restaurant the 24 frames landed in a minute
            # and the row still said nopic 39% a minute later.
            try:
                from . import subneed as _sn
                _sn.check_one(int(row.get("file_id") or 0))
            except Exception:                                    # noqa: BLE001
                pass
        return r
    if on_stage:
        on_stage("pulling the track out", 0.0)
    r = stt._do_one(row, report=on_stage)
    if r.get("ok"):
        r["why"] = ("signs after all - cleared" if r.get("cleared")
                    else "the events say dialogue - it stays on the list")
        if told:
            r["why"] = told + "; " + r["why"]
        # AND TELL THE RAW CHECK, WHICH ASKED FOR THIS READ.
        #
        # Its signs_unread rung is the reason half these reads happen: a
        # forced track whose container reports no cue count is a sign sheet
        # and a full script at the same time, and only the events separate
        # them. The events land here - and the verdict went on saying "the
        # container does not say how many lines it has" until the next
        # sweep came round, which on a 40,000-file library is minutes. Three
        # Drug Store episodes sat at that sentence with the answer already
        # in subtitle_shape.
        #
        # Re-judging one file is a handful of reads and it closes the loop
        # where it was opened.
        try:
            from . import subneed as _sn
            _sn.check_one(int(row.get("file_id") or 0))
        except Exception:                                        # noqa: BLE001
            pass
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
        # AND THE VERDICT TABLE IS TIDIED HERE, NOT BY A BUTTON.
        #
        # audio_lang had 1,801 rows whose file_id is not in `files` at all and
        # 719 for files marked deleted or duplicate. forget() and invalidate()
        # are per-file and are called from paths that KNOW a file changed;
        # nothing answered for the rows nobody was told about. Because the
        # listening bar counted the table's rows as library read, all 2,520
        # were reported as tracks heard.
        #
        # Hourly, off the feeder that is already awake, and only when nothing
        # is listening - two DELETEs against an index, but there is no reason
        # for them to land in the middle of a pass.
        try:
            from . import audiolang as _al_prune
            if time.time() - _PRUNE["at"] > 3600:
                _PRUNE["at"] = time.time()
                _PRUNE["last"] = await asyncio.to_thread(_al_prune.prune)
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
