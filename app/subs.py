r"""nuarr - the whole subtitle question, asked once.

WHY THIS EXISTS. The Subtitles page grew one panel at a time, and each panel
was right on its own. The sidecar sweep had a header, a count, a progress bar,
a switch per library and a list. The duplicate-track sweep had a header, a
count, a progress bar, a switch and a list. The burned-in/title check had a
header, a count, a progress bar, a mode and a list. Five vocabularies for
three things, and the same file appearing in three places with no way to see
that it was the same file. Tracking what each system did and did not do became
harder than the work any of them does.

subkind.py already made this move once, for two readers that were asking the
same question in the same words. This is the same move at the level of the
page: one place that says what every subtitle decision is, whether it is
switched on, how many files are waiting on it - and then ONE list, a row per
FILE, carrying everything that would happen to that file.

WHAT IT IS NOT. It is not a fourth engine. Nothing here reads a frame, opens a
container or rewrites anything. subembed still walks for sidecars, subdupe
still counts events, subkind still runs its two readers, and each still has
its own place in the shared idle runner with its own rank. What moves here is
the reporting - the counting, the naming, the ordering and the switchboard -
because that was the part that was written three times.

THE UNIT IS THE FILE, WHICH IS THE WHOLE POINT. "City Hunter S02E60" was in
the sidecar list because a subtitle sat beside it, in the duplicate list
because it had grown three English tracks, and in the title list because one
of them was labelled wrongly. Three rows in three panels, and nothing on the
page said they were one episode. Here it is one row, and what is wrong with
it reads as one sentence.

EVERY NUMBER COMES FROM THE SYSTEM THAT OWNS IT. There is no second count of
anything: the sidecar total is subembed.summary(), the duplicate total is the
length of subdupe's cached candidate list, the picture and title totals are
subkind.findings()'s own counts. A count computed twice is a count that will
eventually disagree with itself, and then neither copy can be trusted.
"""
from __future__ import annotations

import os
import time

# Every decision on this page, in the order a file meets them: what is sitting
# beside the file, what is already inside it, and what the inside actually
# says. The order is not cosmetic - the sidecar sweep can add a track that the
# duplicate sweep then has an opinion about, which is why it runs at a lower
# rank than subdupe in the shared runner.
SIDECAR = "sidecar"
DUPE = "dupe"
PICTURE = "picture"
TITLE = "title"

# How long a merged view is good for. The sources are all cached themselves -
# the walk for ten minutes, the duplicate candidates for ten, the findings off
# stored probes - so this is only about not re-merging twenty thousand rows on
# every poll of an open page.
_VIEW: dict = {"at": 0.0, "data": None, "running": False}
_VIEW_TTL = 20.0


def _name(path: str) -> str:
    """The part of the path a person would say out loud."""
    try:
        return os.path.basename(path or "") or (path or "")
    except Exception:                                            # noqa: BLE001
        return path or ""


def _side_word(t: dict) -> str:
    """One sidecar, named the way it appears on disk."""
    lang = (t.get("lang") or "?").strip()
    role = (t.get("role") or "").strip()
    ext = ""
    try:
        ext = os.path.splitext(t.get("sidecar") or "")[1].lstrip(".").lower()
    except Exception:                                            # noqa: BLE001
        pass
    bits = [lang]
    if role and role not in ("full", ""):
        bits.append(role)
    out = " ".join(bits)
    return f"{out} {ext}" if ext else out


# ------------------------------------------------------------ the switches --
def by_decision() -> dict:
    r"""How many queued files carry a step of each kind, and how many ask.

    THE COUNT IS NOW WHAT IS GOING TO HAPPEN, not what a sweep estimated it
    might find. Every one of these numbers used to come from a different
    system's own preview - a walk, a candidate list, a findings table - each
    computed at a different moment with a different notion of what counted.
    They are one query over one table now, so they add up.
    """
    from . import subqueue
    from .db import cursor
    import json as _json
    subqueue.init()
    n = {SIDECAR: 0, DUPE: 0, PICTURE: 0, TITLE: 0, ASK: 0}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT steps, asks FROM sub_queue "
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


def board() -> list:
    """Every subtitle decision as one row, and what it is set to right now.

    A SETTING YOU HAVE TO OPEN A PANEL TO SEE IS A SETTING YOU DO NOT KNOW.
    Three of these four lived inside the panel they governed, so the only way
    to answer "is the duplicate sweep on?" was to scroll to it and read the
    switch. Here the answer is on the first screen, next to the number of
    files it is holding up.
    """
    rows = []
    # WHAT IS ACTUALLY QUEUED, which is the only count on this page that can
    # be checked against something. Each row below still says what its rule
    # IS; how many files are waiting on it is read off the queue.
    tally = by_decision()

    # 1. What is sitting beside the file.
    try:
        from . import subembed
        from .config import SETTINGS
        libs = {l.name: subembed.enabled(l.name)
                for l in (SETTINGS.libraries or [])}
        on = [k for k, v in libs.items() if v]
        sm = subembed.summary()
        # A ZERO THAT MEANS "NOT COUNTED YET" IS A LIE, and it is the first
        # thing on the page after a restart. The walk is a listdir per file
        # across forty thousand rows; until it finishes there is no total, so
        # the row says what it is doing instead of reporting none.
        w = dict(subembed._WALK or {})
        counting = bool(sm.get("pending") or (w.get("running")
                                              and not sm.get("files")))
        walk_word = ""
        if counting:
            done, tot = int(w.get("done") or 0), int(w.get("total") or 0)
            walk_word = (f"still walking the library — {done:,} of {tot:,}"
                         if tot else "still walking the library")
        rows.append({
            "key": SIDECAR,
            "name": "Subtitle files sitting beside the video",
            "does": "Takes a loose .srt or .ass into the file it belongs to, "
                    "as a stream copy, and recycles the loose copy.",
            "why": "A sidecar is one rename away from being orphaned, it "
                   "beats the embedded track in Plex's picker, and an ASS one "
                   "makes Plex re-encode the whole video to paint it on.",
            "on": bool(on),
            "setting": (f"on for {', '.join(sorted(on))}" if on
                        else "off in every library"),
            "per_library": libs,
            "waiting": tally[SIDECAR],
            "waiting_word": "queued files with a sidecar to take in or a "
                            "loose copy to recycle",
            "counting": counting,
            "detail": walk_word,
            "goto": "subembed", "panel": "sePanel",
            "toggle": "",
        })
    except Exception as e:                                       # noqa: BLE001
        rows.append(_broken(SIDECAR, "Subtitle files sitting beside the video",
                            e))

    # 2. What is already inside it, twice.
    try:
        from . import subdupe
        rows.append({
            "key": DUPE,
            "name": "The same subtitle inside the file twice",
            "does": "Keeps one track per language and kind - the one with the "
                    "most lines - and removes the rest. An empty track goes "
                    "whether or not it has a twin.",
            "why": "nuarr put most of these there: the sidecar sweep used to "
                   "read a stale probe, so a release shipping three English "
                   "sidecars got one per pass. 376 files, 407 extra tracks.",
            "on": bool(subdupe.enabled()),
            "setting": ("on" if subdupe.enabled()
                        else "off - it is the only sweep that takes a track "
                             "OUT, so it waits to be switched on"),
            "waiting": tally[DUPE],
            "waiting_word": "queued files carrying a twin or an empty track",
            "goto": "subdupe", "panel": "sdPanel",
            "toggle": subdupe.ENABLED_KEY,
        })
    except Exception as e:                                       # noqa: BLE001
        rows.append(_broken(DUPE, "The same subtitle inside the file twice", e))

    # 3 and 4. What the inside actually says. One system, two readers, and
    # one mode - so it is one switchboard row with two counts under it.
    try:
        from . import subkind
        f = _findings()
        c = f.get("counts") or {}
        mode = subkind.mode()
        rows.append({
            "key": PICTURE,
            "name": "What each file actually carries",
            "does": "Reads the picture of a file that claims no subtitles, "
                    "and reads the events of a track whose title looks wrong. "
                    "Marks the burned-in ones; corrects the titles that lie.",
            "why": "Bazarr asks 'does this have English subtitles', gets no, "
                   "and fetches one forever. A track called 'Signs only' "
                   "carrying eighteen lines a minute is dialogue.",
            "on": mode == "auto",
            "setting": (f"{mode} · sure at {subkind.mark_at()}%, "
                        f"dismissed at {subkind.dismiss_at()}%"),
            "waiting": tally[PICTURE] + tally[TITLE],
            "waiting_word": "queued files to mark or retitle",
            # Its panel is Subtitle User Input, and this row is its handle.
            # Named rather than left as "the detail", because the panel it
            # opens has a name and it is not a detail - it is the one thing on
            # the page that wants an answer.
            "detail_name": "Subtitle User Input",
            # READ IS NOT THE SAME AS OUTSTANDING, and putting the two
            # numbers side by side without saying so is how "600" ends up
            # looking like a backlog next to "1 waiting". Most of those 600
            # are answered; what is left is the count above.
            "detail": (f"read so far: {int(c.get('picture') or 0)} from the "
                       f"picture, {int(c.get('tracks') or 0)} from track "
                       f"titles · {int(c.get('band') or 0)} of them sit "
                       f"between the two lines and are yours to call"),
            "goto": "hardsub", "panel": "skPanel",
            "toggle": "",
        })
    except Exception as e:                                       # noqa: BLE001
        rows.append(_broken(PICTURE, "What each file actually carries", e))

    return rows


def _broken(key: str, name: str, e: Exception) -> dict:
    """A decision that could not be read is still a decision, and says so."""
    return {"key": key, "name": name, "does": "", "why": "", "on": False,
            "setting": "could not be read", "waiting": 0,
            "waiting_word": str(e)[:120], "goto": "", "panel": "",
            "toggle": "", "error": str(e)[:200]}


# -------------------------------------------------------------- the merge ---
def _findings() -> dict:
    from . import subkind
    try:
        return subkind.findings(600)
    except Exception:                                            # noqa: BLE001
        return {"rows": [], "counts": {}}


ASK = "ask"

# Which switchboard row a step belongs under, so one colour means one thing
# wherever it appears on the page.
_OF_STEP = {"take": SIDECAR, "recycle": SIDECAR, "dropdup": DUPE,
            "dropempty": DUPE, "retitle": TITLE, "mark": PICTURE}


def _word(s: dict) -> str:
    """One step, said the way a person would say it."""
    d = s.get("do")
    if d == "take":
        bits = [s.get("lang") or "?"]
        if s.get("cls") and s["cls"] != "full":
            bits.append(s["cls"])
        w = "take in " + " ".join(bits)
        if s.get("replaces"):
            w += f", replacing {len(s['replaces'])} inside"
        return w
    if d == "recycle":
        return f"recycle {s.get('name') or 'a loose copy'}"
    if d == "dropdup":
        if int(s.get("ord", -1)) < 0:
            n = len(s.get("weigh") or [])
            return f"weigh {n} {s.get('cls') or ''} {s.get('lang') or ''} " \
                   f"tracks and drop the thinner"
        return f"drop a second {s.get('cls') or ''} {s.get('lang') or ''} track"
    if d == "dropempty":
        return f"drop an empty {s.get('lang') or ''} track"
    if d == "retitle":
        return f"retitle track {int(s.get('ord') or 0)}: " \
               f"{s.get('from') or '(none)'} → {s.get('to') or ''}"
    if d == "mark":
        return f"mark the words burned into the picture ({s.get('sure')}% sure)"
    return str(d or "")


def queue_rows(limit: int = 400) -> dict:
    r"""The page's list, read straight off the queue.

    IT USED TO BE A MERGE OF THREE SYSTEMS' OPINIONS and it is now one table
    lookup, because the merge moved into the queue where it belongs. The
    difference matters beyond speed: what this list shows is now exactly what
    is going to run, rather than three separate previews of what three
    separate sweeps might each decide to do later.
    """
    from . import subqueue
    subqueue.init()
    out: list = []
    with_ask = 0
    try:
        from .db import cursor
        with cursor() as cur:
            total = int(cur.execute(
                "SELECT COUNT(*) n FROM sub_queue "
                " WHERE state != 'done'").fetchone()["n"] or 0)
            rows = [dict(r) for r in cur.execute(
                "SELECT file_id, name, path, library, disk, state, steps, "
                "       asks, n, why, err, tries "
                "  FROM sub_queue WHERE state != 'done' "
                " ORDER BY (asks != '[]') DESC, state, n DESC, library, name "
                " LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return {"total": 0, "ready": 0, "held": 0, "rows": []}
    import json as _json
    for r in rows:
        try:
            steps = _json.loads(r.get("steps") or "[]")
        except Exception:                                        # noqa: BLE001
            steps = []
        try:
            asks = _json.loads(r.get("asks") or "[]")
        except Exception:                                        # noqa: BLE001
            asks = []
        acts = [{"key": _OF_STEP.get(s.get("do"), DUPE), "word": _word(s),
                 "why": s.get("why") or "", "on": True, "n": 1}
                for s in steps]
        acts += [{"key": ASK, "word": a.get("asking") or "one to decide",
                  "why": "", "on": False, "n": 1, "q": a.get("q") or ""}
                 for a in asks]
        if asks:
            with_ask += 1
        out.append({"file_id": int(r["file_id"]), "path": r.get("path") or "",
                    "name": r.get("name") or "", "library": r.get("library") or "",
                    "disk": r.get("disk") or "", "state": r.get("state") or "",
                    "acts": acts, "n": len(acts), "why": r.get("why") or "",
                    "err": r.get("err") or "",
                    "ready": not asks, "keys": sorted({a["key"] for a in acts})})
    return {"total": total, "ready": total - with_ask, "held": with_ask,
            "rows": out}


def _sidecar_acts(on_map: dict) -> dict:
    """{file_id: act} for every file with a sidecar worth taking."""
    from . import subembed
    out: dict = {}
    try:
        rows = subembed.walk()
    except Exception:                                            # noqa: BLE001
        return out
    for p in rows:
        take = p.get("take") or []
        drop = p.get("drop") or []
        if not take and not drop:
            continue
        bits = []
        if take:
            bits.append("take in " + ", ".join(_side_word(t) for t in take[:3])
                        + ("…" if len(take) > 3 else ""))
        reps = sum(len(t.get("replaces") or []) for t in take)
        if reps:
            bits.append(f"replacing {reps} track{'' if reps == 1 else 's'} "
                        f"already inside")
        if drop:
            bits.append(f"recycle {len(drop)} loose "
                        f"cop{'y' if len(drop) == 1 else 'ies'}")
        lib = p.get("library") or ""
        out[int(p["file_id"])] = {
            "file_id": int(p["file_id"]), "path": p.get("path") or "",
            "library": lib, "disk": p.get("pool_disk") or "",
            "act": {"key": SIDECAR, "word": "; ".join(bits),
                    "why": "; ".join(
                        [t.get("note") or "" for t in take if t.get("note")]
                        + [d.get("why") or "" for d in drop])[:300],
                    "on": bool(on_map.get(lib)),
                    "n": len(take) + len(drop)},
        }
    return out


def _dupe_acts(on: bool) -> dict:
    from . import subdupe
    out: dict = {}
    try:
        rows = subdupe.cached()
    except Exception:                                            # noqa: BLE001
        return out
    for r in rows:
        kinds = r.get("kinds") or []
        # THE PLAN IS NOT RUN HERE. Naming which copy survives means
        # extracting and counting every candidate track, which is a minute a
        # file - so the row says what is duplicated and leaves which-one-goes
        # to the sweep, which reads the file it is about to rewrite anyway.
        word = ("an empty track" if kinds == ["an empty track"]
                else "two of: " + ", ".join(kinds[:3])
                     + ("…" if len(kinds) > 3 else ""))
        out[int(r["file_id"])] = {
            "file_id": int(r["file_id"]), "path": r.get("path") or "",
            "library": r.get("library") or "", "disk": r.get("pool_disk") or "",
            # NO PER-ROW REASON, BECAUSE IT IS THE SAME REASON EVERY TIME.
            # "one per language and kind survives" under all 2,944 rows is
            # 2,944 copies of a sentence that belongs to the rule, not to the
            # file. It is on the switchboard row above, said once.
            "act": {"key": DUPE, "word": word, "why": "",
                    "on": bool(on), "n": max(1, len(kinds))},
        }
    return out


def _kind_acts() -> dict:
    """{file_id: [acts]} from the picture and title readers."""
    out: dict = {}
    f = _findings()
    for r in (f.get("rows") or []):
        if r.get("done") or not r.get("action"):
            continue
        fid = int(r.get("file_id") or 0)
        if not fid:
            continue
        is_pic = (r.get("source") == "picture")
        sure = int(r.get("sure") or 0)
        if is_pic:
            word = f"mark the words burned into the picture ({sure}% sure)"
            why = r.get("why") or ""
        else:
            word = (f"correct {r.get('source_word') or 'a title'}: "
                    f"{r.get('title_old') or '(none)'} → "
                    f"{r.get('title_new') or ''} ({sure}% sure)")
            why = r.get("why") or ""
        e = out.setdefault(fid, {"file_id": fid, "path": r.get("path") or "",
                                 "library": r.get("library") or "",
                                 "disk": "", "acts": []})
        e["acts"].append({"key": PICTURE if is_pic else TITLE, "word": word,
                          "why": why, "sure": sure,
                          "on": (r.get("auto") == "act"), "n": 1})
    return out


def rows(limit: int = 400) -> dict:
    """One row per file, carrying every subtitle thing that would happen.

    ORDERED BY WHAT IS ACTUALLY GOING TO HAPPEN. A file whose decisions are
    all switched on is work in progress; a file waiting on a switch is a
    question. Those are different things to look at, so the first sort key is
    which of the two it is, and only then how much is wrong with it.
    """
    t0 = time.time()
    try:
        from . import subembed
        from .config import SETTINGS
        on_map = {l.name: subembed.enabled(l.name)
                  for l in (SETTINGS.libraries or [])}
    except Exception:                                            # noqa: BLE001
        on_map = {}
    try:
        from . import subdupe
        dup_on = subdupe.enabled()
    except Exception:                                            # noqa: BLE001
        dup_on = False

    merged: dict = {}

    def slot(d):
        fid = int(d["file_id"])
        e = merged.setdefault(fid, {
            "file_id": fid, "path": d.get("path") or "",
            "name": _name(d.get("path") or ""),
            "library": d.get("library") or "", "disk": d.get("disk") or "",
            "acts": []})
        # Whichever source knew the spindle wins; the readers do not carry it.
        if not e["disk"] and d.get("disk"):
            e["disk"] = d["disk"]
        if not e["path"] and d.get("path"):
            e["path"] = d["path"]
            e["name"] = _name(d["path"])
        if not e["library"] and d.get("library"):
            e["library"] = d["library"]
        return e

    for d in _sidecar_acts(on_map).values():
        slot(d)["acts"].append(d["act"])
    for d in _dupe_acts(dup_on).values():
        slot(d)["acts"].append(d["act"])
    for d in _kind_acts().values():
        e = slot(d)
        e["acts"].extend(d["acts"])

    out = list(merged.values())
    for e in out:
        e["n"] = sum(int(a.get("n") or 1) for a in e["acts"])
        e["ready"] = any(a.get("on") for a in e["acts"])
        e["waiting"] = any(not a.get("on") for a in e["acts"])
        e["keys"] = sorted({a["key"] for a in e["acts"]})
    out.sort(key=lambda e: (not e["ready"], -len(e["keys"]), -e["n"],
                            e["library"], e["name"]))
    # COUNTED OVER EVERYTHING, NOT OVER THE PAGE. The header says how many
    # nuarr will get to by itself; counting that inside the first four hundred
    # rows would make the number depend on how many rows were asked for.
    ready = sum(1 for e in out if e["ready"])
    return {"total": len(out), "ready": ready, "held": len(out) - ready,
            "rows": out[:max(0, int(limit))],
            "took": round(time.time() - t0, 3)}


def working() -> list:
    """What the four systems have in flight, as the runner reports it."""
    try:
        from . import idle
    except Exception:                                            # noqa: BLE001
        return []
    mine = {"Subtitles", "Sidecar subtitles", "Duplicate subtitles",
            "Subtitles in the picture", "Subtitle titles"}
    try:
        return [t for t in idle.tasks() if (t.get("system") or "") in mine]
    except Exception:                                            # noqa: BLE001
        return []


def _subtitle_jobs() -> dict:
    r"""The subtitle jobs on the main queue: how many, and what is moving.

    READ FROM THE JOBS TABLE AND THE RUNNING WORKERS, not from anything this
    module keeps. There is one queue and one set of workers; a second count
    kept here would be a second opinion about the same rows, which is the
    failure this whole page has been built to avoid.
    """
    out = {"running": 0, "queued": 0, "rows": []}
    try:
        from .db import cursor
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT state, COUNT(*) n FROM jobs "
                    " WHERE kind='subs' AND state IN ('queued','running') "
                    " GROUP BY state"):
                out[r["state"]] = int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            if getattr(w.job, "kind", "") != "subs":
                continue
            out["rows"].append({"title": w.job.title or "",
                                "disk": getattr(w, "disk", "") or "",
                                "progress": float(getattr(w, "progress", 0.0))})
    except Exception:                                            # noqa: BLE001
        pass
    return out


def overview(limit: int = 400, force: bool = False) -> dict:
    """Everything the page needs, in one answer, at most once every 20s.

    THE PAGE POLLS AND THE MERGE IS NOT FREE, so the expensive half is cached
    and the live half is not. What is in flight right now and what the idle
    runner is doing change every second and are read fresh every time; the
    list of what WOULD happen is a merge over twenty thousand walked rows and
    is reused until it is twenty seconds old.
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
    # THE SCAN'S OWN PROGRESS, which is the first thing a person wants when a
    # page says a number: how much of the library has that number been
    # measured over? Counted, left, and how fast it is going.
    try:
        from . import subplan, subqueue, subscan
        d["scan"] = subscan.progress()
        d["queue"] = (subqueue.stats() or {}).get("q") or {}
        d["rev"] = subplan.revision()
        d["replanning"] = bool(subqueue.STATE.get("replanning"))
        # WHAT THE MAIN QUEUE HAS OF OURS. The work runs over there now, so
        # this page reports it rather than owning it - how many subtitle jobs
        # are running, how many are waiting behind them, and enough of the
        # live ones to show something moving.
        d["jobs"] = _subtitle_jobs()
    except Exception:                                            # noqa: BLE001
        d["scan"], d["queue"] = {}, {}
    # THE RUNNER'S OWN STRIP, SHIPPED WITH THE PAGE. It used to be fetched
    # per panel, which meant the switchboard could only show a progress bar
    # for a system whose panel somebody had already opened - and the whole
    # point of the switchboard is that you do not have to open anything.
    d["idle"] = {}
    try:
        from . import idle
        for k in ("subqueue", "subscan", "subembed", "subdupe", "hardsub",
                  "subtitletitle"):
            try:
                d["idle"][k] = idle.progress(k)
            except Exception:                                    # noqa: BLE001
                pass
    except Exception:                                            # noqa: BLE001
        pass
    d["at"] = _VIEW["at"]
    d["age_s"] = round(now - (_VIEW["at"] or now), 1)
    # WHAT COULD NOT BE DONE belongs to the page, not to one panel of it. Only
    # the sidecar sweep can fail in a way that leaves something to look at -
    # the other two either act or leave the file alone - so this is its list,
    # named for the page rather than for the system.
    try:
        from . import subembed
        f = subembed.failures(200)
        d["failed"] = [x for x in f if not x.get("gone")]
        d["failed_gone"] = sum(1 for x in f if x.get("gone"))
    except Exception:                                            # noqa: BLE001
        d["failed"], d["failed_gone"] = [], 0
    return d


def bump() -> None:
    """Forget the merge. Called when a switch moves, so the page catches up."""
    _VIEW.update(at=0.0)
