r"""nuarr - the whole audio-language question, asked once.

THE SUBTITLE PAGE'S SHAPE, BECAUSE IT IS THE SAME PROBLEM. That page grew one
panel at a time, each right on its own, until the same file appeared in three
places with nothing saying it was one file. It ended as facts -> instruction ->
queue -> page, with one switchboard and one list, and this is that page for
audio: aud_queue is what is going to happen, and this module says it in words.

WHAT IT IS NOT. It is not an engine. Nothing here opens a container, listens to
anything or writes a header. audiolang still does the listening on its own pass,
audplan decides, audqueue queues, and a worker in the main job queue carries it
out. What lives here is the reporting - the counting, the naming, the ordering
and the switchboard.

THE UNIT IS THE FILE. audiolang measures TRACKS, and rightly: a language is a
property of a track. But two lying tags in one container are one mkvpropedit
and one thing to look at, so the fold happens once, in audplan.by_file, and
every count on this page is a count of files.

EVERY NUMBER COMES FROM THE SYSTEM THAT OWNS IT. The waiting counts are one
query over aud_queue. The listening backlog is audiolang's own. The revision is
audplan's. There is no second count of anything here, because a count computed
twice is a count that will eventually disagree with itself - and then neither
copy can be trusted.
"""
from __future__ import annotations

import json as _json
import os
import time

# Every audio-language decision, in the order a file meets them: what the tag
# claims, what the title says, and which of the tracks the file is allowed to
# keep at all.
TAG = "tag"
TITLE = "title"
POLICY = "policy"
ASK = "ask"

# Which switchboard row a step belongs under, so one colour means one thing
# wherever it appears on the page.
_OF_STEP = {"tag": TAG, "retitle": TITLE}

# How long the merged view is good for. Everything under it is a database read,
# so this is only about not rebuilding the same answer on every poll of an open
# page.
_VIEW: dict = {"at": 0.0, "data": None, "running": False}
_VIEW_TTL = 15.0


def _word(s: dict) -> str:
    """One step, said the way a person would say it."""
    d = s.get("do")
    t = int(s.get("track") or 0) + 1
    if d == "tag":
        return (f"retag track {t}: {s.get('from') or '(none)'} → "
                f"{s.get('to') or ''}"
                + (f" ({s['sure']}% sure)" if s.get("sure") else ""))
    if d == "retitle":
        return (f"retitle track {t}: {s.get('from') or '(none)'} → "
                f"{s.get('to') or ''}")
    return str(d or "")


# ------------------------------------------------------------ the switches --
def by_decision() -> dict:
    r"""How many queued files carry a step of each kind, and how many ask.

    ONE QUERY OVER ONE TABLE, so the numbers add up. This is what is GOING TO
    HAPPEN rather than what some sweep estimated it might find - the distinction
    the subtitle page was rebuilt to make, and the reason its switchboard counts
    can be checked against its list.
    """
    from . import audqueue
    from .db import cursor
    audqueue.init()
    n = {TAG: 0, TITLE: 0, POLICY: 0, ASK: 0}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT steps, asks FROM aud_queue "
                                 " WHERE state != 'done'"):
                try:
                    steps = _json.loads(r["steps"] or "[]")
                except Exception:                                # noqa: BLE001
                    steps = []
                for k in {_OF_STEP.get(s.get("do")) for s in steps}:
                    if k:
                        n[k] += 1
                if (r["asks"] or "[]") != "[]":
                    n[ASK] += 1
    except Exception:                                            # noqa: BLE001
        pass
    return n


def needs_you() -> int:
    """How many questions are sitting in Audio User Input waiting on you.

    THE ROW'S COUNT HAS TO BE THE PANEL'S COUNT. Counted as QUESTIONS rather
    than files, because that is what you answer - a file with two uncertain
    tracks is two decisions, and saying "1 waiting" over two of them is the
    collapsed row giving you a reason not to open it.
    """
    from . import audqueue
    from .db import cursor
    audqueue.init()
    n = 0
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT asks FROM aud_queue "
                                 " WHERE asks != '[]'"):
                try:
                    n += len(_json.loads(r["asks"] or "[]"))
                except Exception:                                # noqa: BLE001
                    pass
    except Exception:                                            # noqa: BLE001
        pass
    return n


def board() -> list:
    r"""Every audio-language decision as one row, and what it is set to now.

    A SETTING YOU HAVE TO OPEN A PANEL TO SEE IS A SETTING YOU DO NOT KNOW.
    The mode and the two confidence lines lived inside the list they governed,
    so the only way to answer "will it correct a tag on its own?" was to scroll
    until you found the switch. Here it is on the first screen, beside the
    number of files waiting on it.
    """
    rows = []
    tally = by_decision()

    # 1. A tag that claims a language the track is not.
    try:
        from . import audiolang
        mode = audiolang.mode()
        rows.append({
            "key": TAG,
            "name": "A track tagged a language it is not",
            "does": "Corrects the language header to what was actually heard. "
                    "One mkvpropedit write — the audio and the video are never "
                    "touched and nothing is re-encoded.",
            "why": "A tag is the one thing about a file nuarr cannot take on "
                   "trust and cannot derive. Sonarr, Radarr, Bazarr and every "
                   "player read that header; none of them listen to the audio, "
                   "so a wrong tag looks exactly like a right one.",
            "on": mode == "auto",
            "setting": (f"{mode} · corrects at {audiolang.fix_at()}%, "
                        f"leaves alone at or below {audiolang.leave_at()}%"),
            "waiting": tally[TAG],
            "waiting_word": "queued files with a lying tag to correct",
            "needs_you": tally[ASK],
            "detail_name": "Audio User Input",
            "detail": (f"a reading between {audiolang.leave_at()}% and "
                       f"{audiolang.fix_at()}% is never acted on by itself — "
                       f"it is yours to call, and your answer is remembered "
                       f"for the rest of the show"),
            "goto": "audiolang", "panel": "alAskPanel",
            "toggle": "",
        })
    except Exception as e:                                       # noqa: BLE001
        rows.append(_broken(TAG, "A track tagged a language it is not", e))

    # 2. A title that names a different language from the tag.
    try:
        rows.append({
            "key": TITLE,
            "name": "A track title naming a different language",
            "does": "Replaces the language word in the track's title so it "
                    "agrees with the tag, and leaves the rest of the title "
                    "alone — “English E-AC3 5.1” becomes "
                    "“Japanese E-AC3 5.1”.",
            "why": "The tag and the title are two different fields and only "
                   "one of them was ever corrected, so a file can sit at "
                   "lang=jpn title=English forever: the rules read the tag and "
                   "are content, and the only person who sees the title is the "
                   "one choosing a track in a player.",
            "on": True,
            "setting": "always on — it rides along with the tag it agrees "
                       "with, in the same header write",
            "waiting": tally[TITLE],
            "waiting_word": "queued files whose title names another language",
            "goto": "audiolang", "panel": "",
            "toggle": "",
        })
    except Exception as e:                                       # noqa: BLE001
        rows.append(_broken(TITLE, "A track title naming a different "
                                   "language", e))

    # 3. Which languages a file is allowed to keep at all.
    try:
        from . import langpolicy
        from .config import SETTINGS
        keeps = {}
        for lib in (SETTINGS.libraries or []):
            name = getattr(lib, "name", "") or ""
            if not name:
                continue
            pol = langpolicy.for_library(name, "audio") or {}
            # `langs` is the key langpolicy normalises and stores; it is the
            # one the transcode plan reads, so it is the one to report.
            k = pol.get("langs") or []
            keeps[name] = {
                "langs": list(k) if isinstance(k, (list, tuple)) else [str(k)],
                "keep_original": bool(pol.get("keep_original")),
                "keep_untagged": bool(pol.get("keep_untagged"))}
        every = sorted({x for v in keeps.values() for x in v["langs"] if x})
        rows.append({
            "key": POLICY,
            "name": "Which spoken languages a file keeps",
            "does": "Decides which audio tracks survive when a file is next "
                    "rebuilt. Set per library.",
            "why": "The other two rows are about a file telling the truth "
                   "about itself; this one is about what you want kept. They "
                   "run in that order for a reason — dropping tracks by "
                   "language is only safe once the languages are right.",
            "on": bool(every),
            "setting": (", ".join(every) if every
                        else "nothing set — every track is kept"),
            "per_library": keeps,
            # NO COUNT HERE, AND THAT IS THE HONEST ANSWER. This rule is
            # enforced when a file is rebuilt, by the transcode plan, so the
            # files waiting on it are the transcode queue's - not this page's.
            # A number invented here would be a second opinion about somebody
            # else's queue.
            "waiting": 0,
            "waiting_word": "",
            "detail": "applied when the file is next rebuilt — see the "
                      "transcode queue, not this page",
            "goto": "langpolicy", "panel": "alpolCard",
            "toggle": "",
        })
    except Exception as e:                                       # noqa: BLE001
        rows.append(_broken(POLICY, "Which spoken languages a file keeps", e))

    return rows


def _broken(key: str, name: str, e: Exception) -> dict:
    """A decision that could not be read is still a decision, and says so."""
    return {"key": key, "name": name, "does": "", "why": "", "on": False,
            "setting": "could not be read", "waiting": 0,
            "waiting_word": str(e)[:120], "goto": "", "panel": "",
            "toggle": "", "error": str(e)[:200]}


# -------------------------------------------------------------- the list ---
def by_show(limit: int = 14) -> list:
    """Where the waiting work is concentrated, by show.

    "463 of these are Detective Conan" is the kind of fact that turns a number
    into a decision, and it is one pass over a table that is already small.
    """
    from . import audqueue
    from .db import cursor
    audqueue.init()
    by: dict = {}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT path, library FROM aud_queue "
                                 " WHERE state != 'done'"):
                parts = (r["path"] or "").split("\\")
                show = parts[2] if len(parts) > 2 else (r["library"] or "?")
                by[show] = by.get(show, 0) + 1
    except Exception:                                            # noqa: BLE001
        return []
    return sorted(({"show": k, "n": v} for k, v in by.items()),
                  key=lambda x: -x["n"])[:int(limit)]


def queue_rows(limit: int = 400) -> dict:
    r"""The page's list, read straight off the queue.

    WHAT THIS SHOWS IS WHAT IS GOING TO RUN, rather than a preview of what some
    sweep might decide later. That is the whole reason the queue holds the
    instruction instead of holding file ids.
    """
    from . import audqueue
    from .db import cursor
    audqueue.init()
    out: list = []
    with_ask = 0
    try:
        with cursor() as cur:
            total = int(cur.execute(
                "SELECT COUNT(*) n FROM aud_queue "
                " WHERE state != 'done'").fetchone()["n"] or 0)
            rows = [dict(r) for r in cur.execute(
                "SELECT file_id, name, path, library, disk, series, state, "
                "       steps, asks, n, why, err, tries, unread "
                "  FROM aud_queue WHERE state != 'done' "
                " ORDER BY (asks != '[]') DESC, state, n DESC, library, name "
                " LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return {"total": 0, "ready": 0, "held": 0, "rows": [], "by_show": []}
    for r in rows:
        try:
            steps = _json.loads(r.get("steps") or "[]")
        except Exception:                                        # noqa: BLE001
            steps = []
        try:
            asks = _json.loads(r.get("asks") or "[]")
        except Exception:                                        # noqa: BLE001
            asks = []
        acts = [{"key": _OF_STEP.get(s.get("do"), TAG), "word": _word(s),
                 "why": s.get("why") or "", "on": True, "n": 1,
                 "track": s.get("track"), "sure": s.get("sure")}
                for s in steps]
        acts += [{"key": ASK, "word": a.get("asking") or "one to decide",
                  "why": a.get("why") or "", "on": False, "n": 1,
                  "q": a.get("q") or "", "track": a.get("track"),
                  "sure": a.get("sure"), "from": a.get("from"),
                  "to": a.get("to"), "options": a.get("options") or [],
                  "held": a.get("held"), "fake_dual": a.get("fake_dual")}
                 for a in asks]
        if asks:
            with_ask += 1
        out.append({"file_id": int(r["file_id"]), "path": r.get("path") or "",
                    "name": r.get("name") or "",
                    "library": r.get("library") or "",
                    "disk": r.get("disk") or "", "series": r.get("series") or "",
                    "state": r.get("state") or "", "acts": acts,
                    "n": len(acts), "why": r.get("why") or "",
                    "err": r.get("err") or "",
                    # A TRUTHFUL "NOT LOOKED AT". The titles were planned from
                    # a stored probe this file does not have, so the queue will
                    # check live before it writes. Saying nothing here would
                    # read as "the title is fine".
                    "titles_unread": bool(r.get("unread")),
                    "ready": not asks,
                    "keys": sorted({a["key"] for a in acts})})
    return {"total": total, "ready": total - with_ask, "held": with_ask,
            "by_show": by_show(), "rows": out}


# ------------------------------------------------------------- the reading --
def listening() -> dict:
    r"""How much of the library has been listened to, and how fast.

    THE FIRST THING A PERSON WANTS WHEN A PAGE SAYS A NUMBER: how much of the
    library has that number been measured over? On a library where thirty-eight
    thousand tracks have never been read, "6 files need a tag corrected" means
    something quite different from what it looks like, and this is the sentence
    that says so.
    """
    from . import audiolang
    out = {"left": 0, "done": 0, "total": 0, "pct": 0.0, "rate": 0.0,
           "eta": 0, "state": "", "current": "", "each": 0.0}
    try:
        left = int(audiolang.unverified_count() or 0)
    except Exception:                                            # noqa: BLE001
        left = 0
    try:
        p = audiolang.progress() or {}
    except Exception:                                            # noqa: BLE001
        p = {}
    try:
        from .db import cursor
        with cursor() as cur:
            total = int(cur.execute(
                "SELECT COUNT(*) n FROM audio_lang").fetchone()["n"] or 0)
    except Exception:                                            # noqa: BLE001
        total = 0
    whole = total + left
    out.update(left=left, done=total, total=whole,
               pct=round(100.0 * total / whole, 1) if whole else 0.0,
               state=str(p.get("state") or ""),
               current=str(p.get("current") or ""))
    # SECONDS PER TRACK, MEASURED RATHER THAN ASSUMED - and only reported once
    # there is a measurement. A rate invented from one file is not a rate.
    try:
        each = float(p.get("secs_each") or 0)
        if not each and p.get("done") and p.get("elapsed"):
            each = float(p["elapsed"]) / max(1, int(p["done"]))
        out["each"] = round(each, 2)
        out["rate"] = round(1.0 / each, 3) if each else 0.0
        out["eta"] = int(left * each) if each else 0
    except Exception:                                            # noqa: BLE001
        pass
    return out


def working() -> list:
    """What the audio systems have in flight, as the runner reports it."""
    try:
        from . import idle
    except Exception:                                            # noqa: BLE001
        return []
    mine = {"Audio languages", "audiolang", "Audio language listen"}
    try:
        return [t for t in idle.tasks() if (t.get("system") or "") in mine]
    except Exception:                                            # noqa: BLE001
        return []


def _audio_jobs() -> dict:
    r"""The audio jobs on the main queue: how many, and what is moving.

    READ FROM THE JOBS TABLE AND THE RUNNING WORKERS, not from anything this
    module keeps. There is one queue and one set of workers; a second count
    here would be a second opinion about the same rows, which is the failure
    this page is built to avoid.
    """
    out = {"running": 0, "queued": 0, "rows": []}
    try:
        from .db import cursor
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT state, COUNT(*) n FROM jobs "
                    " WHERE kind='audio' AND state IN ('queued','running') "
                    " GROUP BY state"):
                out[r["state"]] = int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            if getattr(w.job, "kind", "") != "audio":
                continue
            out["rows"].append({"title": w.job.title or "",
                                "disk": getattr(w, "disk", "") or "",
                                "progress": float(getattr(w, "progress", 0.0))})
    except Exception:                                            # noqa: BLE001
        pass
    return out


def overview(limit: int = 400, force: bool = False) -> dict:
    """Everything the page needs, in one answer.

    THE PAGE POLLS, so the half that is a merge is cached and the half that is
    live is not. What is in flight right now changes every second and is read
    fresh every time; the switchboard and the list are reused until they are
    fifteen seconds old.
    """
    now = time.time()
    cached = _VIEW["data"]
    if force or cached is None or now - _VIEW["at"] > _VIEW_TTL:
        if not _VIEW["running"]:
            _VIEW["running"] = True
            try:
                cached = {"board": board(), **queue_rows(limit)}
                _VIEW.update(at=time.time(), data=cached)
            except Exception as e:                               # noqa: BLE001
                cached = cached or {"board": [], "rows": [], "total": 0,
                                    "error": str(e)[:200]}
            finally:
                _VIEW["running"] = False
        else:
            cached = cached or {"board": [], "rows": [], "total": 0}
    d = dict(cached)
    d["working"] = working()
    d["listen"] = listening()
    try:
        from . import audplan, audqueue
        d["queue"] = (audqueue.stats() or {}).get("q") or {}
        d["rev"] = audplan.revision()
        d["replanning"] = bool(audqueue.STATE.get("replanning"))
        d["jobs"] = _audio_jobs()
    except Exception:                                            # noqa: BLE001
        d["queue"], d["jobs"] = {}, {}
    d["needs_you"] = needs_you()
    d["at"] = _VIEW["at"]
    d["age_s"] = round(now - (_VIEW["at"] or now), 1)
    # WHAT COULD NOT BE DONE belongs to the page, not to one panel of it.
    try:
        from . import audqueue
        d["failed"] = audqueue.snapshot(60).get("failed") or []
    except Exception:                                            # noqa: BLE001
        d["failed"] = []
    return d


def bump() -> None:
    """Forget the merge. Called when a switch moves, so the page catches up."""
    _VIEW.update(at=0.0)


def memory(limit: int = 400) -> list:
    """What you have taught it, newest first."""
    from . import audplan
    out = []
    for m in audplan.memory(limit):
        d = dict(m)
        d["what"] = {"tag": "correct the tag to what was heard",
                     "leave": "leave the tag alone"}.get(d.get("answer") or "",
                                                         d.get("answer") or "")
        out.append(d)
    return out


def _name(path: str) -> str:
    try:
        return os.path.basename(path or "") or (path or "")
    except Exception:                                            # noqa: BLE001
        return path or ""
