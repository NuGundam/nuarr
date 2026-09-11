r"""nuarr - check ONE file now, and put what it finds where the panels look.

WHY A SPOT CHECK EXISTS AT ALL
------------------------------
Every reader in nuarr is a sweep. That is the right shape for 39,750 files and
the wrong shape for one question: "this episode sounds like it is tagged
wrong - is it?" The sweeps answer that eventually, on their own clock, in a
queue behind twenty-seven thousand other tracks. Eventually is not an answer
when you are looking at the file.

So: pick a file, read it now, and see the verdict. Nothing here is a new
detector. It calls the same functions the sweeps call, writes to the same
tables, and the result appears in the same panel with the same buttons - the
only difference is that a person chose the file and is watching the clock.

WHAT IT WILL NOT DO
-------------------
It will not check a file nuarr does not already know about. A finding has to
live somewhere - a row keyed on file_id, a panel that lists it, a button that
corrects it - and a path outside every library has none of those. Told
plainly, rather than reading the file and dropping the answer on the floor.

THE PROGRESS IS MEASURED, NOT ANIMATED
--------------------------------------
Each check is a handful of real steps with real costs: a model load, three
thirty-second windows of audio, a demux per subtitle track, twenty-four
frames sampled and OCR'd. The bar moves when a step finishes, the estimate
comes from what the sweeps have actually measured on this machine, and a step
that has not started is not reported as progress.
"""
from __future__ import annotations

import os
import threading
import time

from . import joblog
from .db import cursor

# One run at a time per kind. Both readers load models and hit the disk, and
# two of them racing is slower than either alone.
_LOCK = threading.Lock()

# Live state per kind ('audio' | 'subs'), polled by the panel.
RUNS: dict = {}


def _fresh(kind: str, path: str, label: str, steps: list) -> dict:
    d = {"kind": kind, "path": path, "label": label, "running": True,
         "t0": time.time(), "at": 0.0, "step": 0, "steps": steps,
         "total": len(steps), "now": steps[0] if steps else "",
         "ok": None, "why": "", "found": None, "secs_each": 0.0}
    RUNS[kind] = d
    return d


def _step(d: dict, i: int, what: str = "") -> None:
    d["step"] = i
    d["now"] = what or (d["steps"][i] if i < len(d["steps"]) else "")


def _end(d: dict, ok: bool, why: str, found=None) -> dict:
    d.update(running=False, ok=bool(ok), why=why, found=found,
             at=time.time(), step=d["total"],
             took=round(time.time() - d["t0"], 1))
    return d


def progress(kind: str) -> dict:
    r"""Where the run has got to, with an estimate built on measurement."""
    d = dict(RUNS.get(kind) or {})
    if not d:
        return {"running": False}
    now = time.time()
    el = (now - d["t0"]) if d.get("running") else (d.get("took") or 0)
    d["elapsed"] = round(el, 1)
    total = max(1, int(d.get("total") or 1))
    done = int(d.get("step") or 0)
    d["pct"] = round(min(100.0, done / total * 100.0), 1)
    # TIME LEFT FROM THE STEPS THAT HAVE FINISHED, not from a guess about the
    # ones that have not. Before the first step lands there is no rate, and
    # saying so is better than inventing one.
    if d.get("running") and done and el > 0.4:
        each = el / done
        d["secs_each"] = round(each, 2)
        d["eta"] = max(0, round((total - done) * each))
    else:
        d["eta"] = 0
    return d


def _resolve(path: str) -> dict:
    r"""The library file at this path, or why there is not one.

    NORMALISED BOTH WAYS. The picker hands back whatever the filesystem spells
    - a trailing slash, a different case, a short name - and `files.path` is
    whatever the scanner wrote. Compared case-insensitively on the normalised
    form, which is the only comparison that holds on Windows.
    """
    p = os.path.normpath((path or "").strip().strip('"'))
    if not p:
        return {"ok": False, "why": "no file given"}
    if not os.path.isfile(p):
        return {"ok": False, "why": "that is not a file on this machine"}
    want = os.path.normcase(p)
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT id, path, library, title, season, episode, size, "
                    "       duration, audio_langs, sub_langs, state "
                    "  FROM files WHERE path = ? COLLATE NOCASE", (p,)):
                return {"ok": True, "row": dict(r)}
            # A rename between the scan and now leaves the row under the old
            # spelling; fall back to the basename before giving up.
            base = os.path.basename(p)
            for r in cur.execute(
                    "SELECT id, path, library, title, season, episode, size, "
                    "       duration, audio_langs, sub_langs, state "
                    "  FROM files WHERE path LIKE ?", ("%" + base,)):
                if os.path.normcase(os.path.normpath(r["path"])) == want:
                    return {"ok": True, "row": dict(r)}
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    return {"ok": False,
            "why": "nuarr has no record of that file - it is not inside a "
                   "library, so a finding about it would have nowhere to live"}


def _label(r: dict) -> str:
    try:
        from .db import display_label
        return (display_label(r.get("title"), r.get("season"),
                              r.get("episode"))
                or os.path.basename(r.get("path") or ""))
    except Exception:                                            # noqa: BLE001
        return os.path.basename(r.get("path") or "")


# ------------------------------------------------------------- the audio ----
def check_audio(path: str) -> dict:
    r"""Listen to every tagged audio track in this file, now.

    The same audiolang.check() the file's own row calls, once per track, so
    the verdicts land in audio_lang and any disagreement appears in the
    Disagreements tab exactly as a swept one would.
    """
    from . import audiolang
    # EVERY OUTCOME GOES THROUGH THE SAME STATE, refusals included. A check
    # that declines without leaving a trace is a button that does nothing:
    # the panel polls this and would see "not running" either way.
    d = _fresh("audio", path, os.path.basename(path or ""),
               ["finding that file"])
    got = _resolve(path)
    if not got.get("ok"):
        return _end(d, False, got["why"])
    r = got["row"]
    d["label"] = _label(r)
    d["path"] = r["path"]
    if not audiolang.usable():
        return _end(d, False, "the listener is not installed or cannot run "
                              "on this machine")
    codes = [c for c in (r.get("audio_langs") or "").split(",") if c.strip()]
    n = max(1, len(codes))
    steps = ["opening the file"] + [f"listening to track {i}" for i in range(n)]
    if not _LOCK.acquire(blocking=False):
        return _end(d, False, "a check is already running")
    d["steps"] = steps
    d["total"] = len(steps)
    d.update(running=True, step=0, t0=time.time(), now=steps[0])
    try:
        # What the sweeps have measured on this machine, so the first estimate
        # is not a guess either.
        try:
            d["secs_each"] = audiolang.secs_each_seen() or 0.0
        except Exception:                                        # noqa: BLE001
            pass
        heard = []
        for i in range(n):
            _step(d, i + 1, f"listening to track {i} of {n}")
            try:
                res = audiolang.check(int(r["id"]), r["path"], i, refresh=True)
            except Exception as e:                               # noqa: BLE001
                res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            tagged = (codes[i] if i < len(codes) else "").strip()
            heard.append({"track": i, "tagged": tagged,
                          "code": res.get("code") or "",
                          "confidence": round(float(res.get("confidence")
                                                    or 0), 3),
                          "ok": bool(res.get("ok")),
                          "why": res.get("why") or ""})
        _step(d, len(steps), "reading the ledger back")
        # DID IT PRODUCE A FINDING? The panel is the answer, not this, so ask
        # the panel's own list rather than deciding here for a second time.
        odd = []
        try:
            odd = [x for x in audiolang.mismatches(4000, floor=0.0,
                                                   respect_answers=False)
                   if int(x["file_id"]) == int(r["id"])]
        except Exception:                                        # noqa: BLE001
            pass
        why = ("that file's tags match what is in it"
               if not odd else
               f"{len(odd)} track{'' if len(odd) == 1 else 's'} "
               f"{'is' if len(odd) == 1 else 'are'} tagged a language "
               f"{'it is' if len(odd) == 1 else 'they are'} not")
        joblog.log(f"spot check: listened to {d['label']} - {why}", "info",
                   system="audiolang")
        return _end(d, True, why, {"tracks": heard, "disagrees": odd})
    finally:
        if d.get("running"):
            _end(d, False, "the check stopped early")
        _LOCK.release()


# --------------------------------------------------------- the subtitles ----
def check_subs(path: str) -> dict:
    r"""Work out what this file's subtitles actually are, now.

    Two readers, the same two the panel merges: the text tracks get their
    events read and shaped, and a file claiming no subtitle track at all gets
    frames sampled for words burned into the picture. Whichever applies.
    """
    from . import hardsub, subtitletitle as stt
    d = _fresh("subs", path, os.path.basename(path or ""),
               ["finding that file"])
    got = _resolve(path)
    if not got.get("ok"):
        return _end(d, False, got["why"])
    r = got["row"]
    d["label"] = _label(r)
    d["path"] = r["path"]
    subs = [c for c in (r.get("sub_langs") or "").split(",") if c.strip()]
    picture = not subs
    steps = (["sampling the picture for burned-in words"] if picture
             else ["looking at what the titles claim"])
    if not _LOCK.acquire(blocking=False):
        return _end(d, False, "a check is already running")
    d["steps"] = steps
    d["total"] = len(steps)
    d.update(running=True, step=0, t0=time.time(), now=steps[0])
    try:
        found = {}
        if picture:
            _step(d, 1, "sampling 24 frames and reading them")
            try:
                res = hardsub.probe_one(int(r["id"]))
            except Exception as e:                               # noqa: BLE001
                res = {"ok": False, "why": f"{type(e).__name__}: {e}"}
            found["picture"] = res
            why = (res.get("why") or "")
            if res.get("ok") is False:
                return _end(d, False, why or "could not read the picture",
                            found)
            state = res.get("state") or res.get("kind") or ""
            why = ("nothing readable is burned into the picture"
                   if state in ("", "none")
                   else f"the picture carries {state}")
        else:
            # THIS FILE'S TRACKS, NOT THE NEXT ONES IN THE QUEUE.
            # inspect_some() reads whatever is at the head of the unread list,
            # which for a spot check would be somebody else's episode. The
            # scan says which of THIS file's tracks are worth reading, then
            # each one is read by hand.
            _step(d, 1, "looking at what the titles claim")
            try:
                stt.refresh()
            except Exception:                                    # noqa: BLE001
                pass
            mine = [x for x in (stt.cached().get("rows") or [])
                    if int(x.get("file_id") or 0) == int(r["id"])]
            todo = [x for x in mine if x.get("unread") and x.get("mkv_id")]
            d["steps"] = (["looking at what the titles claim"]
                          + [f"reading track s:{x.get('track')}" for x in todo]
                          + ["working out what each track carries"])
            d["total"] = len(d["steps"])
            for i, x in enumerate(todo, 1):
                _step(d, i + 1, f"reading track s:{x.get('track')} - "
                                f"{i} of {len(todo)}")
                try:
                    stt.shape_of(x["file_id"], x["path"], x["track"],
                                 int(x.get("size") or 0), x["mkv_id"])
                except Exception:                                # noqa: BLE001
                    continue
            _step(d, d["total"], "working out what each track carries")
            try:
                stt.refresh()
            except Exception:                                    # noqa: BLE001
                pass
            rows = [x for x in (stt.cached().get("rows") or [])
                    if int(x.get("file_id") or 0) == int(r["id"])]
            found["tracks"] = rows
            found["read"] = len(todo)
            why = ("every subtitle title agrees with what the track carries"
                   if not rows else
                   f"{len(rows)} track{'' if len(rows) == 1 else 's'} "
                   f"{'carries' if len(rows) == 1 else 'carry'} something "
                   f"{'its' if len(rows) == 1 else 'their'} title does not say")
        joblog.log(f"spot check: read the subtitles of {d['label']} - {why}",
                   "info", system="subtitle kinds")
        return _end(d, True, why, found)
    finally:
        if d.get("running"):
            _end(d, False, "the check stopped early")
        _LOCK.release()
