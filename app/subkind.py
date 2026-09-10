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
               "runs": 0, "last_took": 0.0, "last_error": ""}


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
        auto, auto_why = _auto_of(score)
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
        auto, auto_why = ("ask", "not read yet") if unread else _auto_of(score)
        rewritable = bool(r.get("rewritable"))
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
            "evidence": (f"{r.get('cues') or 0} cues · {r.get('cpm') or 0}/min"
                         + (f" · {r.get('shape')}" if r.get("shape") else "")),
            "why": r.get("kind_why") or r.get("why") or "",
            "auto": auto, "auto_why": auto_why,
            "title_old": r.get("old") or "", "title_new": r.get("new") or "",
            "action": "retitle" if rewritable else "",
            "action_word": ("Correct the title" if rewritable else
                            ("" if unread else "left alone")),
            "done": False, "done_word": "",
            "detail": r.get("why") or "",
        })
    return out


def findings(limit: int = 300) -> dict:
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
        "state": {**STATE, "cycle_s": CYCLE_S,
                  "next_run": (STATE["last_run"] + CYCLE_S)
                              if STATE.get("last_run") else 0.0},
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
async def run(force: bool = False) -> dict:
    """One pass: the picture reader, then the track reader. Both yield."""
    from . import hardsub, subtitletitle as stt
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    t0 = time.time()
    STATE.update(running=True, phase="picture", t0=t0, last_error="")
    got: dict = {}
    try:
        got["picture"] = await hardsub.sweep(force=force)
        STATE["phase"] = "tracks"
        await asyncio.to_thread(stt.refresh)
        got["tracks"] = await stt.inspect_paced(stt.PER_RUN, force=force)
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
    await asyncio.sleep(240)
    while True:
        try:
            await run()
        except Exception as e:                                   # noqa: BLE001
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            joblog.log(f"subtitle kind check: {STATE['last_error']}", "warn")
        await asyncio.sleep(CYCLE_S)
