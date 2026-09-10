r"""What subtitles does this file actually carry? One answer, from two readers.

WHY ONE SYSTEM. Two checks grew up beside each other on the Subtitles page.
"Subtitles already in the picture" sampled frames of files that claimed to
have no subtitle track and asked whether words were burned into the image.
"Does the subtitle title describe the subtitle?" read the events out of a
text track and asked whether its title told the truth. Different readers,
different tables, different panels - and the same question at the bottom of
both: is this dialogue, dialogue with signs over it, signs and songs, or
nothing? They ended up with the same four words, the same 0-100 score, the
same picker to overrule the reading, and the same manual/auto switch with the
same two lines. Two panels one above the other answering the same question in
the same vocabulary is one panel drawn twice.

So this is the one place the question is asked. Every subtitle SOURCE in a
file - the picture itself, and each text track - is a row: what it carries,
how sure, and what that means should be done:

    picture carries dialogue, no track       -> add the blank marker track
    track's title claims signs, carries speech -> rewrite the title
    anything else                            -> nothing; it is fine

THE READERS STAY WHERE THEY ARE. hardsub.py still samples frames and runs
OCR; subtitletitle.py still pulls events out of a track. They genuinely do
different work and each is a page of hard-won measurement. What moves here is
everything that was duplicated ON TOP of them: the schedule, the mode, the
lines, the picker, the batch, the panel, and the list. The two modules become
what they always were underneath - engines - and this is the system.

ONE SWITCH, ONE PAIR OF LINES. hardsub_mode, hardsub_mark_at and
hardsub_dismiss_at are the settings for the whole thing; the title check's
own copies are read through here so an existing config keeps meaning what it
meant. A single "sure enough" line for a picture and a different one for a
track would be two answers to one question.
"""
from __future__ import annotations

import asyncio
import time

from . import joblog

SCHED_KEY = "subkind"
CYCLE_S = 300.0

PICTURE = "picture"

STATE: dict = {"running": False, "phase": "", "t0": 0.0, "last_run": 0.0,
               "runs": 0, "last_took": 0.0, "last_error": "",
               # WHAT AUTO DID, AND WHAT IS LEFT FOR IT. A mode that acts on
               # its own has to be as legible as one that waits: how many it
               # took last pass, how many are still queued behind the per-pass
               # cap, and therefore how many passes - how long - until it has
               # worked through the standing list.
               "auto_marked": 0, "auto_dropped": 0, "auto_queued": 0,
               "auto_at": 0.0, "auto_runs": 0}


# ------------------------------------------------------------ the settings --
def mode() -> str:
    from . import hardsub
    return hardsub.mode()


def mark_at() -> int:
    from . import hardsub
    return hardsub.mark_at()


def dismiss_at() -> int:
    from . import hardsub
    return hardsub.dismiss_at()


def _next_run() -> float:
    """When this pass runs again. The scheduler's answer where it has one -
    it knows about the first run after boot, which last_run + cycle cannot."""
    try:
        from . import schedules
        for r in (schedules.snapshot() or {}).get("rows", []):
            if r.get("key") == SCHED_KEY and r.get("next_run"):
                return float(r["next_run"])
    except Exception:                                            # noqa: BLE001
        pass
    lr = STATE.get("last_run") or 0.0
    if lr:
        return lr + CYCLE_S
    # BEFORE THE FIRST PASS the scheduler has nothing to report, because
    # next_run is derived from a last_run that has not happened. The loop
    # knows when it will wake, so it says so.
    return float(STATE.get("due_at") or 0.0)


def _auto_of(score: int) -> tuple[str, str]:
    """The same three-way call the picture check has always made."""
    if score >= mark_at():
        return "act", f"{score}% is at or above the {mark_at()}% line"
    if score <= dismiss_at():
        return "dismiss", f"{score}% is at or below the {dismiss_at()}% line"
    return "ask", (f"{score}% sits between {dismiss_at()}% and "
                   f"{mark_at()}%, so this one is yours to call")


# ---------------------------------------------------------------- the rows --
def _picture_rows(limit: int) -> list:
    """Every picture-source finding, in the merged shape."""
    from . import hardsub
    out = []
    for r in hardsub.found(limit):
        kind = r.get("state") or ""
        score = int(r.get("score") or 0)
        # THE READER'S OWN VERDICT, not a second one: the sweep and the
        # backlog act on verdict_for(), and the row must show what they do.
        auto = {"mark": "act"}.get(r.get("auto") or "", r.get("auto") or "ask")
        auto_why = r.get("auto_why") or ""
        marked = bool(r.get("marked"))
        out.append({
            "id": f"{r['file_id']}:{PICTURE}",
            "file_id": r["file_id"], "source": PICTURE,
            "source_word": "the picture",
            "path": r.get("path") or "", "label": r.get("label") or "",
            "library": r.get("library") or "",
            "kind": kind, "chosen": bool(r.get("chosen")),
            "kinds": r.get("kinds") or [],
            "sure": score, "read": True, "unread": False,
            "evidence": r.get("words") or "",
            "why": r.get("why") or "",
            "auto": auto, "auto_why": auto_why,
            "action": "" if marked else "mark",
            "action_word": "" if marked else "Mark it",
            "done": marked, "done_word": "marked" if marked else "",
            "detail": r.get("detail") or "",
            # WHEN THE FILE LANDED, not when it was looked at. A finding you
            # are deciding about is about a file, and "this arrived an hour
            # ago" is what tells you whether it is the batch you just grabbed.
            "added": float(r.get("first_seen") or 0.0),
            "found_at": float(r.get("at") or 0.0),
        })
    return out


def _track_rows(limit: int) -> list:
    """Every text-track finding, in the merged shape."""
    from . import subtitletitle as stt
    d = stt.cached()
    out = []
    for r in (d.get("rows") or [])[:limit]:
        unread = bool(r.get("unread"))
        score = 0 if unread else int(r.get("sure") or 0)
        rewritable = bool(r.get("rewritable"))
        if unread:
            auto, auto_why = "ask", "not read yet"
        elif not rewritable:
            # Nothing can act on a title that carries a group's name, however
            # sure the read - so the row does not pretend auto would.
            auto, auto_why = "none", ("left alone: the title carries a name "
                                      "nuarr did not write and cannot regenerate")
        else:
            auto, auto_why = _auto_of(score)
            if auto == "dismiss":
                # A track is never thrown away - there is nothing to throw. It
                # stays listed for a person; only the colour says "unsure".
                auto, auto_why = "ask", (f"{score}% is under the dismiss line; a "
                                         f"track is never thrown away, so this "
                                         f"one waits for you")
        out.append({
            "id": f"{r['file_id']}:track:{r['track']}",
            "file_id": r["file_id"], "source": f"track:{r['track']}",
            "track": r["track"],
            "source_word": f"track s:{r['track']}",
            "path": r.get("path") or "", "label": r.get("label") or "",
            "library": r.get("library") or "",
            "kind": r.get("kind") or "", "chosen": bool(r.get("chosen")),
            "kinds": r.get("kinds") or [],
            "sure": score, "read": not unread, "unread": unread,
            "evidence": f"{r.get('cues') or 0} cues · {r.get('cpm') or 0}/min",
            "plain_rate": r.get("rate") or 0,
            "shape": r.get("shape") or "",
            "why": r.get("kind_why") or r.get("why") or "",
            "auto": auto, "auto_why": auto_why,
            "title_old": r.get("old") or "", "title_new": r.get("new") or "",
            "action": "retitle" if rewritable else "",
            "action_word": ("Correct the title" if rewritable else
                            ("" if unread else "left alone")),
            "done": False, "done_word": "",
            "detail": r.get("why") or "",
            "added": float(r.get("added") or 0.0),
            "found_at": 0.0,
        })
    return out


def findings(limit: int = 600) -> dict:
    """Everything, least certain first, with the counts the header needs."""
    from . import hardsub, subtitletitle as stt
    rows = _picture_rows(limit) + _track_rows(limit)
    lo, hi = dismiss_at(), mark_at()
    mid = (lo + hi) / 2.0
    # LEAST CERTAIN FIRST among the rows that can be answered; the unread
    # ones sit behind them because nothing can be pressed on an unread row,
    # and the done ones last because they are answered already.
    rows.sort(key=lambda r: (bool(r["done"]), bool(r["unread"]),
                             abs(r["sure"] - mid)))
    hs = hardsub.stats()
    sp = stt.progress()
    return {
        "rows": rows,
        "mode": mode(), "mark_at": mark_at(), "dismiss_at": dismiss_at(),
        "counts": {
            "picture": sum(1 for r in rows if r["source"] == PICTURE),
            "tracks": sum(1 for r in rows if r["source"] != PICTURE),
            "unread": sum(1 for r in rows if r["unread"]),
            "band": sum(1 for r in rows if r["auto"] == "ask"
                        and not r["unread"] and not r["done"]),
            "done": sum(1 for r in rows if r["done"]),
            "actionable": sum(1 for r in rows if r["action"] and not r["done"]),
        },
        "picture": hs,
        "tracks": sp,
        # The batch marker is where auto's marking actually happens, so its
        # progress is auto's progress and the panel reads it from here rather
        # than from a second endpoint.
        "marking": dict(hardsub.MARK_STATE or {}),
        "auto": {
            "marked": STATE.get("auto_marked") or 0,
            "dropped": STATE.get("auto_dropped") or 0,
            "queued": STATE.get("auto_queued") or 0,
            "at": STATE.get("auto_at") or 0.0,
            "per_pass": AUTO_MARKS_PER_PASS,
            "runs": STATE.get("auto_runs") or 0,
            # AUTO RUNS AT THE HEAD OF EVERY PASS, so the pass's clock is
            # auto's clock - one schedule, one next time, no second answer.
            "next_run": _next_run(),
            "cycle_s": CYCLE_S,
            # HOW LONG UNTIL AUTO IS DONE with what it can already see: the
            # queue over the per-pass cap, at the cadence the pass runs on.
            "eta": (((STATE.get("auto_queued") or 0) / AUTO_MARKS_PER_PASS)
                    * CYCLE_S) if STATE.get("auto_queued") else 0.0,
        },
        "state": {**STATE, "cycle_s": CYCLE_S, "next_run": _next_run()},
        "kinds": [{"id": k, "word": hardsub.KIND_WORDS[k]}
                  for k in hardsub.KINDS],
    }


# ------------------------------------------------------------- the actions --
def _split(source: str) -> tuple[bool, int]:
    if source == PICTURE:
        return True, 0
    try:
        return False, int(str(source).split(":", 1)[1])
    except Exception:                                            # noqa: BLE001
        return False, 0


def set_kind(file_id: int, source: str, kind: str) -> dict:
    from . import hardsub, subtitletitle as stt
    pic, track = _split(source)
    return (hardsub.set_kind(int(file_id), kind) if pic
            else stt.set_kind(int(file_id), track, kind))


def dismiss(file_id: int, source: str) -> dict:
    """"This is not what you said." For a picture that teaches the OCR filter;
    for a track it sets the kind to signs, which is what a wrong dialogue call
    almost always is."""
    from . import hardsub, subtitletitle as stt
    pic, track = _split(source)
    return (hardsub.ignore(int(file_id)) if pic
            else stt.set_kind(int(file_id), track, stt.SIGNS))


def act(file_id: int, source: str, kind: str = "") -> dict:
    """Do the thing the finding calls for: mark a picture, retitle a track."""
    from . import hardsub, subtitletitle as stt
    pic, track = _split(source)
    if pic:
        return hardsub.mark_one(int(file_id), kind)
    if kind:
        stt.set_kind(int(file_id), track, kind)
        stt.refresh()
    rows = [r for r in (stt.cached().get("rows") or [])
            if int(r.get("file_id") or 0) == int(file_id)
            and int(r.get("track") or 0) == track and r.get("rewritable")]
    if not rows:
        return {"ok": False, "why": "nothing to rewrite on that track"}
    out = stt.fix(rows)
    return {"ok": bool(out.get("fixed")), "why":
            f"corrected {out.get('fixed') or 0}" if out.get("fixed")
            else (out.get("failures") or [{}])[0].get("why", "failed")}


async def act_many(items: list, kind: str = "", force: bool = False) -> dict:
    """items: [{file_id, source}] - pictures go to the batch marker, tracks
    are retitled in one mkvpropedit pass each."""
    from . import hardsub, subtitletitle as stt
    pics = [int(i["file_id"]) for i in items if i.get("source") == PICTURE]
    tracks = [(int(i["file_id"]), _split(i["source"])[1]) for i in items
              if i.get("source") != PICTURE]
    out: dict = {"ok": True, "pictures": 0, "tracks": 0, "why": ""}
    if pics:
        r = await hardsub.mark_many(pics, force=force, kind=kind)
        out["pictures"] = r.get("started") or 0
        if not r.get("ok"):
            out["why"] = r.get("why") or ""
    if tracks:
        want = {(f, t) for f, t in tracks}
        if kind:
            # THE KIND YOU PICKED IS WHAT THE TITLE SAYS. Set it first so the
            # rescan writes "dialogue + signs" or "dialogue" accordingly.
            for f, t in tracks:
                stt.set_kind(f, t, kind)
            await asyncio.to_thread(stt.refresh)
        rows = [r for r in (stt.cached().get("rows") or [])
                if (int(r.get("file_id") or 0), int(r.get("track") or 0)) in want
                and r.get("rewritable")]
        fixed = stt.fix(rows) if rows else {"fixed": 0}
        out["tracks"] = fixed.get("fixed") or 0
    bits = []
    if out["pictures"]:
        bits.append(f"marking {out['pictures']} picture"
                    + ("" if out["pictures"] == 1 else "s"))
    if out["tracks"]:
        bits.append(f"retitled {out['tracks']} track"
                    + ("" if out["tracks"] == 1 else "s"))
    out["why"] = out["why"] or ("; ".join(bits) or "nothing to do")
    return out


def dismiss_many(items: list) -> dict:
    from . import hardsub, subtitletitle as stt
    pics = [int(i["file_id"]) for i in items if i.get("source") == PICTURE]
    n = 0
    r = hardsub.ignore_many(pics) if pics else {"done": 0, "quiet": []}
    n += r.get("done") or 0
    for i in items:
        if i.get("source") == PICTURE:
            continue
        f, t = int(i["file_id"]), _split(i["source"])[1]
        if stt.set_kind(f, t, stt.SIGNS).get("ok"):
            n += 1
    why = f"dismissed {n} finding" + ("" if n == 1 else "s")
    if r.get("quiet"):
        why += (f" - and {len(r['quiet'])} show"
                + ("" if len(r["quiet"]) == 1 else "s")
                + " now left alone entirely")
    return {"ok": True, "done": n, "why": why}


# ------------------------------------------------------------- the schedule --
AUTO_MARKS_PER_PASS = 25


async def _auto_backlog() -> dict:
    """Findings that were already on the list when auto was switched on.

    THE SWEEP ONLY JUDGES WHAT IT JUST READ. Flip the switch with 490 pictures
    already found under manual and nothing happened to any of them, because
    auto lived inside the sweep loop and those files were never swept again.
    So each pass in auto starts by going through the standing list: what is
    past the mark line is handed to the batch marker (gated, a bounded number
    a pass, because every mark rewrites a container), and what is under the
    dismiss line is thrown away the way the sweep would have.
    """
    from . import hardsub
    out = {"marked": 0, "dropped": 0, "queued": 0}
    if mode() != "auto":
        return out
    rows = await asyncio.to_thread(hardsub.found, 1000)
    to_mark = [r["file_id"] for r in rows
               if not r.get("marked") and r.get("auto") == "mark"]
    to_drop = [r["file_id"] for r in rows
               if not r.get("marked") and r.get("auto") == "dismiss"]
    if to_drop:
        d = await asyncio.to_thread(hardsub.ignore_many, to_drop)
        out["dropped"] = int(d.get("done") or 0)
    if to_mark and not hardsub.MARK_STATE.get("running"):
        m = await hardsub.mark_many(to_mark[:AUTO_MARKS_PER_PASS])
        out["marked"] = int(m.get("started") or 0)
    out["queued"] = max(0, len(to_mark) - out["marked"])
    # EVERY PASS, NOT ONLY THE ONES THAT DID SOMETHING. "has not acted yet"
    # and "ran a minute ago and found nothing to do" are different answers,
    # and only one of them means auto is working.
    STATE.update(auto_marked=out["marked"], auto_dropped=out["dropped"],
                 auto_queued=out["queued"], auto_at=time.time(),
                 auto_runs=(STATE.get("auto_runs") or 0) + 1)
    if out["marked"] or out["dropped"]:
        joblog.log(f"subtitle kinds: on its own, marking {out['marked']} "
                   f"file(s) past the {mark_at()}% line and dropping "
                   f"{out['dropped']} under the {dismiss_at()}% line"
                   + (f" - {len(to_mark) - out['marked']} more wait for the "
                      f"next pass" if len(to_mark) > out["marked"] else ""),
                   "info")
    return out


async def run(force: bool = False) -> dict:
    """One pass: the standing list, the picture reader, then the track reader.
    Both readers yield to the gate before every file."""
    from . import hardsub, subtitletitle as stt
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    t0 = time.time()
    STATE.update(running=True, phase="backlog", t0=t0, last_error="")
    got: dict = {}
    try:
        got["backlog"] = await _auto_backlog()
        STATE["phase"] = "picture"
        got["picture"] = await hardsub.sweep(force=force)
        STATE["phase"] = "tracks"
        await asyncio.to_thread(stt.refresh)
        got["tracks"] = await stt.inspect_paced(stt.PER_RUN, force=force)
        # RE-JUDGE WHAT WAS JUST READ, in this pass. The scan is where auto
        # corrects titles, so without this a track read now was corrected
        # five minutes later, and the page showed it as still wrong until then.
        await asyncio.to_thread(stt.refresh)
    except Exception as e:                                       # noqa: BLE001
        STATE["last_error"] = f"{type(e).__name__}: {e}"
    finally:
        STATE.update(running=False, phase="", last_run=time.time(),
                     runs=STATE.get("runs", 0) + 1,
                     last_took=time.time() - t0)
        try:
            from . import schedules
            p, t = got.get("picture") or {}, got.get("tracks") or {}
            schedules.beat(SCHED_KEY,
                           f"{p.get('checked', 0)} pictures sampled, "
                           f"{p.get('found', 0)} carrying subtitles; "
                           f"{t.get('read', 0)} tracks read, "
                           f"{t.get('cleared', 0)} cleared")
        except Exception:                                        # noqa: BLE001
            pass
    return {"ok": True, **got}


async def watch() -> None:
    """The one schedule both readers run on."""
    from . import hardsub, subtitletitle as stt
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "What subtitles does each file carry?", "Subtitles",
            CYCLE_S,
            what=(f"Samples frames of files that report no subtitle track "
                  f"({hardsub.PER_RUN} a pass) and reads the events of text "
                  f"tracks whose title looks wrong ({stt.PER_RUN} a pass). "
                  f"Both yield to the job gate before every file."))
    except Exception:                                            # noqa: BLE001
        pass
    STATE["due_at"] = time.time() + 240.0
    await asyncio.sleep(240)
    while True:
        try:
            await run()
        except Exception as e:                                   # noqa: BLE001
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            joblog.log(f"subtitle kind check: {STATE['last_error']}", "warn")
        await asyncio.sleep(CYCLE_S)
