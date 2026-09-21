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


def _auto_eta() -> float:
    """How long until auto has worked through everything past the line."""
    from . import hardsub
    left = STATE.get("auto_queued") or 0
    if not left:
        return 0.0
    each = float((hardsub.MARK_STATE or {}).get("secs_each") or 0.0)
    if not each:
        try:
            each = float(hardsub.mark_progress().get("secs_each") or 0.0)
        except Exception:                                        # noqa: BLE001
            each = 0.0
    if each:
        # Plus the pause between batches, which is real time too.
        return left * each + (left / AUTO_MARKS_PER_PASS) * AUTO_TICK_S
    return (left / AUTO_MARKS_PER_PASS) * AUTO_TICK_S


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
        settled = bool(r.get("settled"))
        acked = bool(r.get("acked"))
        if settled:
            # You said what it carries and the title already agrees, so there
            # is nothing to correct - but the row stays until you press
            # something, and stays reachable after that.
            auto, auto_why = "none", ("you set this by hand, and the title "
                                      "already says so - nothing to correct")
        elif unread:
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
            # THE FLAG IS PART OF THE CORRECTION, so the row says so before
            # the button is pressed. A forced-flagged track carrying the same
            # cues as the full track beside it is the full track wearing a
            # flag that is not true of it, and renaming it alone leaves the
            # player still switching subtitles on by itself.
            "unforce": bool(r.get("unforce")),
            # NOTHING IS OFFERED UNTIL THE TRACK HAS BEEN READ. A row whose
            # events are still on the queue carried "retitle" as its action
            # because the title LOOKED regenerable, and the header counted
            # it as yours to answer - "12 yours" over a table of "not read
            # yet". The planner already refuses to act on an unread row; the
            # row now says so too.
            "action": ("" if unread else
                       "retitle" if rewritable else
                       ("leave" if (settled and not acked) else "")),
            "action_word": (("Correct it and clear the forced flag"
                             if r.get("unforce") else "Correct the title")
                            if rewritable else
                            ("Leave it as it is" if (settled and not acked)
                             else ("" if unread else "left alone"))),
            # DONE MEANS ANSWERED, and setting a kind is not an answer. Only
            # the button takes a row off the list.
            "done": acked, "done_word": "set by hand" if acked else "",
            "settled": settled, "acked": acked,
            # The title carries something nuarr did not write, and you have
            # overruled that caution by setting the kind yourself.
            "unsafe": bool(r.get("unsafe")),
            "detail": r.get("why") or "",
            "added": float(r.get("added") or 0.0),
            "found_at": 0.0,
        })
    return out


# ---- THE RAW CHECK'S OPEN QUESTIONS, AS ROWS HERE -------------------------
#
# Erik: "all undecided and not looked inside and waiting on re-read logic
# should be in system 1 so the user can decide what to do with them,
# especially the undecided, because right now the user can't do anything in
# nuarr about them."
#
# True. The raw check listed them under a count with no button, on the
# grounds that nuarr had no opinion - and "nuarr has no opinion" is exactly
# when a person's is worth the most. So every file the raw check holds at
# `unknown` is a row on this board: the ones that already have a picture row
# are tagged on it, the ones that do not get a row of their own, and either
# way the answer column offers the one thing a raw can be fixed by.
RAW = "raw"


def _raw_rows(existing: list) -> list:
    """Tag the picture rows the raw check is unsure about, and add rows for
    the files it has never been able to read at all."""
    from . import subneed, hardsub
    try:
        d = subneed.unknown_files("", 2000)
    except Exception:                                            # noqa: BLE001
        return []
    by_file = {int(r["file_id"]): r for r in existing
               if r.get("source") == PICTURE}
    out = []
    for u in (d.get("rows") or []):
        fid = int(u.get("file_id") or 0)
        why = str(u.get("why") or "")
        rule = str(u.get("rule") or u.get("kind") or "")
        hit = by_file.get(fid)
        if hit is not None:
            # ALREADY HERE AS A PICTURE ROW. It keeps its score, its picker
            # and its two buttons; it gains the raw check's reason and the
            # third button. A row that is answered with Mark it stops being
            # a raw by itself on the next pass.
            hit["raw"] = True
            hit["raw_why"] = why
            hit["raw_rule"] = rule
            continue
# THE RAW CHECK'S OWN SCORE FOR THIS FILE. Not the picture reader's -
        # that one answers "are there words in these frames", and the
        # question on this row is "is this a film nobody here can follow".
        # The picture is one term in it; see subneed.raw_score.
        score = int(u.get("sure") or 0)
        words, pst = "", ""
        try:
            from .db import cursor as _cur
            with _cur() as cur:
                h = cur.execute(
                    "SELECT h.state, h.words FROM hardsub h "
                    " WHERE h.file_id=?", (fid,)).fetchone()
            if h is not None:
                pst = str(h["state"] or "")
                words = str(h["words"] or "")
        except Exception:                                        # noqa: BLE001
            words, pst = "", ""
        out.append({
            "id": f"{fid}:{RAW}",
            "file_id": fid, "source": RAW,
            "source_word": "the raw check",
            "path": u.get("path") or "", "label": u.get("label") or "",
            "library": u.get("library") or "",
            "kind": pst, "chosen": False,
            "kinds": [{"id": k, "word": hardsub.KIND_WORDS[k]}
                      for k in hardsub.KINDS],
            "sure": score, "read": bool(pst), "unread": False,
            # A FILE NOTHING HAS READ STILL ARRIVED ON A DAY. The added column
            # drew a dash on every raw row because this was left at zero -
            # and "this whole show landed an hour ago" is exactly the context
            # somebody deciding about a raw wants.
            "added": float(u.get("first_seen") or 0.0),
            "evidence": words, "why": why,
            "auto": "ask", "auto_why": "nothing has read this file; yours to call",
            "action": "replace",
            "action_word": "Blocklist & re-download",
            "done": False, "done_word": "",
            "detail": why, "found_at": float(u.get("at") or 0.0),
            "raw": True, "raw_why": why, "raw_rule": rule,
        })
    return out


def _queued_steps() -> dict:
    r"""{file_id: {"retitle": {track, ...}, "mark": set()}} for live queue rows.

    ONE QUERY OVER THE ROWS THAT ARE WAITING, not one per finding. The queue
    holds a few thousand rows at most and only the ones carrying a retitle or
    a mark matter here, so the JSON is only parsed for rows whose text
    contains one - a string test against a blob is far cheaper than decoding
    every instruction to find out it was about a sidecar.
    """
    import json as _json
    out: dict = {}
    try:
        from .db import cursor
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT file_id, steps FROM sub_queue "
                    " WHERE state IN ('queued','running','ask') "
                    "   AND (steps LIKE '%\"retitle\"%' OR steps LIKE '%\"mark\"%')"):
                try:
                    steps = _json.loads(r["steps"] or "[]")
                except Exception:                                # noqa: BLE001
                    continue
                got = out.setdefault(int(r["file_id"]), {})
                for s in steps:
                    d = s.get("do")
                    if d == "retitle":
                        got.setdefault("retitle", set()).add(int(s.get("ord") or 0))
                    elif d == "mark":
                        got.setdefault("mark", set())
    except Exception:                                            # noqa: BLE001
        pass
    return out


def queue() -> dict:
    """How many reads are on the main queue, for the panel's strip."""
    try:
        from . import readers
        return readers.queue_counts("subread")
    except Exception:                                            # noqa: BLE001
        return {"queued": 0, "running": 0, "now": ""}


def auto_from_queue() -> dict:
    r"""What the queue is doing with the findings past the line.

    A mark step is planned the moment a picture verdict scores past the mark
    line (subplan, rule 4) and runs as a job like any other rewrite. So "what
    has auto done and what is it about to do" is a question about sub_queue
    rows carrying a `mark` step, by state - not about a loop of its own.
    """
    from . import subqueue
    from .db import cursor
    out = {"queued": 0, "running": 0, "marked": 0, "failed": 0,
           "asking": 0, "at": 0.0, "last": "", "eta": 0.0}
    with cursor() as cur:
        for r in cur.execute(
                "SELECT state, COUNT(*) n, MAX(finished_at) at "
                "  FROM sub_queue "
                " WHERE steps LIKE '%\"do\": \"mark\"%' GROUP BY state"):
            st, n = str(r["state"] or ""), int(r["n"] or 0)
            if st == subqueue.QUEUED or st == subqueue.HELD:
                out["queued"] += n
            elif st == subqueue.RUNNING:
                out["running"] += n
            elif st == subqueue.DONE:
                out["marked"] += n
                out["at"] = max(out["at"], float(r["at"] or 0.0))
            elif st == subqueue.FAILED:
                out["failed"] += n
        r = cur.execute(
            "SELECT name FROM sub_queue "
            " WHERE steps LIKE '%\"do\": \"mark\"%' AND state=? "
            " ORDER BY finished_at DESC LIMIT 1", (subqueue.DONE,)).fetchone()
        out["last"] = str((r["name"] if r else "") or "")
        # AND THE ONES IT WILL NOT TOUCH: pictures in the band, waiting on
        # an answer. Counted here so the line can say where the rest went.
        r = cur.execute(
            "SELECT COUNT(*) n FROM sub_queue "
            " WHERE asks LIKE '%\"q\": \"picture\"%' AND state=?",
            (subqueue.ASK,)).fetchone()
        out["asking"] = int((r["n"] if r else 0) or 0)
    # How long the queued ones will take, from what a mark has cost here.
    try:
        from . import hardsub
        each = float((hardsub.MARK_STATE or {}).get("secs_each") or 0.0)
        if not each:
            each = float(hardsub.mark_progress().get("secs_each") or 0.0)
        if each and out["queued"]:
            out["eta"] = each * out["queued"]
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _safe(fn):
    """A section of a panel must not be able to take the panel down."""
    try:
        return fn() or {}
    except Exception:                                            # noqa: BLE001
        return {}


# ---------------------------------------------------- what the answers say --
#
# EVERY FIGURE IN THE PANEL USED TO BE FROZEN. The weights were measured once,
# by hand, over the library as it stood - and then written down, where they
# would go on saying "right 124 times out of 139" however many times somebody
# answered a row afterwards. Erik asked whether the panel updates with the
# answers it is given; this is what makes the honest half of that answer yes.
#
# What is recounted here: how often each signal agreed with a person, over
# every row a person has settled, and how often each kind of file really
# carries burned-in words. Both are queries over what is in the database now.
#
# What is NOT recounted, and the panel says so: the POINTS. A weight that
# moved on its own every time a row was answered would be a scorer nobody
# could reason about - a row could change its mind overnight with no change
# to the file or the evidence. The points stay put; the hit rate beside them
# tells you when one has drifted far enough to be worth changing by hand.
_STATS: dict = {"at": 0.0, "data": None}
_STATS_TTL = 120.0


def _track_signal_stats() -> dict:
    """For every track signal: how often it agreed with the person, now."""
    from . import subtitletitle as stt
    from .db import cursor
    import json as _json
    hit: dict = {}

    def note(key, agreed):
        d = hit.setdefault(key, {"n": 0, "right": 0})
        d["n"] += 1
        d["right"] += 1 if agreed else 0

    facts: dict = {}
    try:
        with cursor() as cur:
            for r in cur.execute("SELECT file_id, tracks FROM sub_facts "
                                 " WHERE COALESCE(tracks,'') != ''"):
                try:
                    facts[int(r["file_id"])] = _json.loads(r["tracks"])
                except Exception:                            # noqa: BLE001
                    pass
            rows = cur.execute(
                "SELECT s.*, f.path, f.library, f.duration "
                "  FROM subtitle_shape s JOIN files f ON f.id=s.file_id "
                " WHERE COALESCE(s.chosen,'') != '' "
                "   AND f.state NOT IN ('deleted','duplicate')").fetchall()
    except Exception:                                        # noqa: BLE001
        return {}
    for r in rows:
        said_signs = str(r["chosen"] or "") == stt.SIGNS
        ordn = int(r["track"] or 1) - 1
        t = next((x for x in (facts.get(int(r["file_id"])) or [])
                  if int(x.get("ord", -1)) == ordn), {})
        mins = max(1.0, float(r["duration"] or 0) / 60.0)
        plain = r["plain"] or 0
        if r["plain_out"] is not None:
            mins = max(1.0, mins - float(r["oped_s"] or 0) / 60.0)
            plain = r["plain_out"]
        rate = plain / mins
        pos = float(r["pos_pct"] or 0.0)
        cover, gap = r["cover"], float(r["gap_s"] or 0.0)
        # Each signal is asked the same question: when you fired, was it a
        # sign sheet? A signal that never fires on these rows says nothing
        # and is left out rather than reported as 0 of 0.
        if pos >= 60.0:
            note("pos_high", said_signs)
        if 20.0 <= pos < 60.0:
            note("pos_some", said_signs)
        if pos <= 0.0:
            note("pos_none", not said_signs)
        if stt._style_is_sign(str(t.get("title") or "")):
            note("title", said_signs)
        if t.get("forced"):
            note("forced", said_signs)
        if int(r["signish"] or 0) > 0:
            note("styles", said_signs)
        if rate <= stt.SIGNS_MAX:
            note("rate_low", said_signs)
        elif rate >= stt.SPEECH_LO:
            note("rate_high", not said_signs)
        if cover is not None and int(cover or 0) > 0:
            if int(cover) <= stt.CLUSTERED_AT and gap >= stt.CLUSTERED_GAP_S:
                note("clustered", said_signs)
            elif int(cover) >= stt.SPREAD_AT and gap <= stt.SPREAD_GAP_S:
                note("spread", not said_signs)
    return hit


def _picture_base_rates() -> dict:
    """How often each kind of file really carries words, counted now."""
    from .db import cursor
    out: dict = {}
    try:
        with cursor() as cur:
            rows = cur.execute(
                "SELECT h.state, f.library, f.audio_langs FROM hardsub h "
                "  JOIN files f ON f.id=h.file_id "
                " WHERE f.state NOT IN ('deleted','duplicate')").fetchall()
    except Exception:                                        # noqa: BLE001
        return {}
    tally: dict = {}
    for r in rows:
        lib = str(r["library"] or "").lower()
        fam = ("anime" if "anime" in lib else
               "animated" if "animated" in lib else "live")
        carry = str(r["state"] or "") not in ("", "none")
        d = tally.setdefault(fam, [0, 0])
        d[0] += 1
        d[1] += 1 if carry else 0
        if fam != "anime":
            langs = {x.strip().lower()
                     for x in str(r["audio_langs"] or "").split(",") if x.strip()}
            if langs:
                key = ("eng_only" if not (langs - {"eng", "en", "und"})
                       else "not_eng")
                e = tally.setdefault(key, [0, 0])
                e[0] += 1
                e[1] += 1 if carry else 0
    for k, (n, c) in tally.items():
        out[k] = {"n": n, "carried": c, "rate": (c / n) if n else 0.0}
    all_n = sum(v[0] for k, v in tally.items()
                if k in ("anime", "animated", "live"))
    all_c = sum(v[1] for k, v in tally.items()
                if k in ("anime", "animated", "live"))
    out["_all"] = {"n": all_n, "carried": all_c,
                   "rate": (all_c / all_n) if all_n else 0.0}
    return out


def _trail_init() -> None:
    from .db import cursor
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subkind_trail(
                day   TEXT PRIMARY KEY,
                at    REAL,
                json  TEXT
            )""")


def _trail_write(d: dict) -> None:
    r"""One row a day, and only when something moved.

    A HISTORY OF A NUMBER IS WORTH MORE THAN THE NUMBER. "Positioning is
    right 89% of the time" is a fact about today; "it was 89% in September
    and is 71% now" is a fact about the library changing under the scorer,
    and it is the one worth acting on. Keyed by day so answering thirty rows
    in an evening writes one row, not thirty.
    """
    import json as _json
    try:
        _trail_init()
        day = time.strftime("%Y-%m-%d", time.localtime())
        small = {"signals": {k: [v["right"], v["n"]]
                             for k, v in (d.get("signals") or {}).items()},
                 "rates": {k: [v["carried"], v["n"]]
                           for k, v in (d.get("rates") or {}).items()}}
        blob = _json.dumps(small, sort_keys=True)
        from .db import cursor
        with cursor() as cur:
            row = cur.execute("SELECT json FROM subkind_trail "
                              " ORDER BY day DESC LIMIT 1").fetchone()
            if row and str(row["json"] or "") == blob:
                return                       # nothing moved; nothing to say
            cur.execute(
                "INSERT INTO subkind_trail(day, at, json) VALUES(?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET at=excluded.at, "
                "  json=excluded.json", (day, time.time(), blob))
    except Exception:                                        # noqa: BLE001
        pass


def trail(limit: int = 60) -> list:
    """The dated rows, oldest first."""
    import json as _json
    out = []
    try:
        _trail_init()
        from .db import cursor
        with cursor() as cur:
            rows = cur.execute(
                "SELECT day, at, json FROM subkind_trail "
                " ORDER BY day DESC LIMIT ?", (int(limit),)).fetchall()
        for r in reversed(rows):
            try:
                out.append({"day": r["day"], "at": r["at"],
                            **_json.loads(r["json"] or "{}")})
            except Exception:                                # noqa: BLE001
                pass
    except Exception:                                        # noqa: BLE001
        return []
    return out


def stats(fresh: bool = False) -> dict:
    """The recounted figures, held for two minutes, and written to the trail."""
    now = time.time()
    if not fresh and _STATS["data"] and now - _STATS["at"] < _STATS_TTL:
        return _STATS["data"]
    d = {"signals": _track_signal_stats(), "rates": _picture_base_rates(),
         "at": now}
    _STATS.update(at=now, data=d)
    _trail_write(d)
    return d


def scoring() -> dict:
    r"""Every signal the two readers score with, what it is worth, and the
    measurement behind it.

    WHY THIS IS BUILT HERE AND NOT WRITTEN INTO THE PAGE. The numbers move -
    the weights came from counting this library and will be re-counted - and
    a table typed into the HTML would start drifting from the scorer the day
    after it was written. These are read from the modules that use them, so
    the page cannot say anything the code does not do.
    """
    from . import hardsub as hs, subtitletitle as stt
    sp, dp = stt.SIGN_POINTS, stt.DIALOGUE_POINTS

    # THE LIVE HALF. live[key] is how often this signal agreed with a person
    # over every row a person has settled, recounted on the way in; `since`
    # is the same figure the first day the trail has, so a signal drifting
    # away from the weight it was given shows it rather than hiding it.
    st = stats()
    live = st.get("signals") or {}
    hist = trail(400)
    first = (hist[0] if hist else {}) or {}
    first_sig = first.get("signals") or {}

    def _row(pts, what, measured, key=""):
        d = {"points": pts, "what": what, "measured": measured}
        got = live.get(key) if key else None
        if got and got.get("n"):
            d["now"] = {"right": got["right"], "n": got["n"],
                        "pct": round(got["right"] * 100.0 / got["n"])}
            was = first_sig.get(key)
            if was and was[1] and was != [got["right"], got["n"]]:
                d["since"] = {"right": was[0], "n": was[1],
                              "pct": round(was[0] * 100.0 / was[1]),
                              "day": first.get("day") or ""}
        return d

    track = [
        _row(dp["spread"], "its lines run through most of the runtime",
             f"{stt.SPREAD_AT} of {stt.COVER_SLICES} slices or more with no "
             f"gap past {stt.SPREAD_GAP_S/60:.0f} min. Dialogue tracks "
             f"measured 10 of 10 and a 1.1 min gap; sign sheets 3 of 10 and "
             f"18.8 min", "spread"),
        _row(sp["clustered"], "its lines sit in one block",
             f"{stt.CLUSTERED_AT} slices or fewer and silent for "
             f"{stt.CLUSTERED_GAP_S/60:.0f} min at a stretch - an opening, an "
             f"ending and a few signs", "clustered"),
        _row(dp["twin"], "it is the same track as the one beside it",
             "a forced flag over the same cue count as the full track in the "
             "same file. The genuine forced tracks here carry 1 to 117 cues; "
             "the mislabelled ones carried 925, 1,110 and 1,639", "twin"),
        _row(sp["pos_high"], "most of it is placed on screen",
             "60% or more of its events carry \\pos or \\move. Of the tracks "
             "a person had to answer, positioning was right 124 times out of "
             "139; above 80% it was signs 56 times out of 56", "pos_high"),
        _row(sp["pos_some"], "some of it is placed on screen",
             "20% or more of its events", "pos_some"),
        _row(dp["pos_none"], "none of it is placed on screen",
             "nothing positioned at all, so nothing in it is a sign",
             "pos_none"),
        _row(sp["title"], "the title says signs",
             "sign, op, ed, karaoke, title, credit and the rest, as whole "
             "words. Right 120 times out of 139 - but silenced on this panel, "
             "where the title is the claim being tested", "title"),
        _row(sp["forced"], "the header flags it forced",
             "right 120 times out of 139. It means different things by "
             "family: 8,287 forced ASS tracks in anime are signs and songs, "
             "781 forced SRT in live action are foreign dialogue", "forced"),
        _row(sp["styles"], "its styles are named for signs or themes", "",
             "styles"),
        _row(sp["rate_low"], "two plain dialogue lines a minute or fewer",
             f"the line under which a signs title is telling the truth "
             f"({stt.SIGNS_MAX:g}/min)", "rate_low"),
        _row(dp["rate_high"], "it runs at the cadence of people talking",
             f"{stt.SPEECH_LO:g} to {stt.SPEECH_HI:g} lines a minute, both "
             f"ends measured. Overruled when the lines are clustered: fifteen "
             f"a minute and twenty-one minutes of silence cannot both be "
             f"speech", "rate_high"),
        _row(dp["unique"], "no sibling episode carries these lines",
             "written for this episode, which a karaoke script never is"),
        _row(dp["live"], "live action",
             "10% of live-action tracks read are sign sheets, against 84% of "
             "anime"),
    ]
    picture = [
        _row(60, "how much of the read looks like language",
             "the shape test, scaled - no dictionary, because the right "
             "reading of an anime frame is full of names no word list has"),
        _row(25, "function words were read",
             "the/and/you is what speech is made of and a sign never is. "
             "Present in 98% of the files that really carry words and 11% of "
             "those that do not"),
        _row(15, "how much of the running time carried text low in the frame",
             "a subtitle track is relentless; a title card is not"),
        _row(-65, "it reads like a credit roll",
             "a third of the words or more are roll words - not one word, "
             "which nearly threw away a Velvet episode over 'assistant'"),
        _row(-65, "nothing read is longer than four letters",
             "what the OCR returns for texture. Files that really carry words "
             "have a five-letter word 43% of the time; those called none, "
             "never"),
    ]
    # THE PRIORS, AS THEY STAND NOW. hs.BASE_RATE is what the scorer
    # multiplies by and is fixed; `now` beside it is the same question asked
    # of the pictures read since, so the two can be compared.
    rates = st.get("rates") or {}
    priors = []
    for k, v in hs.BASE_RATE.items():
        got = rates.get(k) or {}
        p = {"what": k, "rate": v[0], "said": v[1]}
        if got.get("n"):
            p["now"] = {"rate": round(got["rate"], 4),
                        "carried": got["carried"], "n": got["n"]}
        priors.append(p)
    lang = {k: rates.get(k) for k in ("eng_only", "not_eng")
            if rates.get(k, {}).get("n")}
    return {
        "track": {"rows": track, "signs_at": stt.SIGNS_AT,
                  "dialogue_at": -30,
                  "how": "Every signal votes and the points are added. Past "
                         f"{stt.SIGNS_AT} it is a sign sheet, past -30 it is "
                         "dialogue, and the further past, the higher the "
                         "percentage. Between the two the older rate rule "
                         "decides, which is what it was doing alone before "
                         "any of this."},
        "picture": {"rows": picture, "mark_at": hs.mark_at(),
                    "dismiss_at": hs.dismiss_at(),
                    "how": "The first three are added, the last two multiply "
                           "what is left, and then the file's own odds below "
                           "scale it. Past the act line nuarr marks the file "
                           "itself; under the throw-away line it drops the "
                           "finding; between them it asks."},
        # AND THE THIRD BUTTON ON THIS BOARD. Erik: "should have point system
        # for blocklist & redownload". The two scorers above decide what is
        # inside a file; this one decides whether anybody can follow it, and
        # it is the one with a five-gigabyte button behind it. Read out of
        # subneed so the panel and the scorer cannot drift.
        "raw": _safe(lambda: __import__(
            "app.subneed", fromlist=["subneed"]).raw_scoring()),
        "priors": priors,
        "prior_how": ("How often this kind of file carries burned-in words at "
                      "all, counted over 3,941 pictures sampled here. The "
                      "multiplier is that rate over the library's own "
                      f"({hs.BASE_RATE_ALL*100:.0f}%), held between "
                      f"{hs.PRIOR_FLOOR:g} and {hs.PRIOR_CEIL:g} so a rare "
                      "kind cannot veto a clear reading."),
        "counted": {"answers": sum(v.get("n", 0) for v in
                                   (st.get("signals") or {}).values()),
                    "pictures": (rates.get("_all") or {}).get("n", 0),
                    "at": st.get("at", 0.0)},
        "trail": [{"day": h.get("day"), "signals": h.get("signals") or {},
                   "rates": h.get("rates") or {}} for h in hist],
        "live_language": lang,
        "learns": ("The hit rates and the odds are counted fresh from the "
                   "rows you have settled and the pictures read so far - "
                   "answer one and they move. The POINTS do not: a weight "
                   "that drifted on its own would let a row change its mind "
                   "overnight with nothing about the file having changed. "
                   "Read the percentages as an argument about the weights "
                   "rather than as the weights themselves - and read them "
                   "knowing WHICH rows they are counted over. A row reaches "
                   "you because the signals disagreed, so a signal that "
                   "looks poor here is one that tends to be on the losing "
                   "side of an argument, which is not the same as being "
                   "wrong about the library. Two other things do learn on "
                   "their own and are not listed: the words confirmed and "
                   "thrown away by marking and dismissing, which feed the "
                   "picture reader, and the shows dismissed often enough to "
                   "be left alone."),
        "language": ("Outside anime the question is not what shelf the file "
                     "sits on but whether anyone in it speaks a language the "
                     "audience has no track for. English audio only: 21 of "
                     "2,160 carried burned-in words. A non-English track: 51 "
                     "of 66."),
    }


def findings(limit: int = 600, want_done: bool = True,
             want_unread: bool = True) -> dict:
    r"""Everything, least certain first, with the counts the header needs.

    WHAT IT SENDS AND WHAT IT COUNTS ARE TWO DIFFERENT SETS, and separating
    them is the whole performance story of this panel.

    Measured before the split: 683 rows, 800 KB of JSON, 0.9 to 1.8 seconds an
    endpoint call - to draw a panel whose default view shows SEVEN of them.
    655 were findings already marked and 21 not read yet; both are hidden
    unless you press the footer link that asks for them, and both were being
    serialised, shipped and parsed every fifteen seconds regardless.

    The counts still come from everything, because the header's whole job is
    to say how many are hidden - "also show the 655 already marked" cannot be
    written without counting 655. That costs two queries and about 150 ms of
    Python; what it does not cost any more is three quarters of a megabyte on
    the wire.
    """
    from . import hardsub, subtitletitle as stt
    rows = _picture_rows(limit) + _track_rows(limit)
    rows += _raw_rows(rows)
    lo, hi = dismiss_at(), mark_at()
    mid = (lo + hi) / 2.0
    # LEAST CERTAIN FIRST among the rows that can be answered; the unread
    # ones sit behind them because nothing can be pressed on an unread row,
    # and the done ones last because they are answered already.
    # A FILE NOTHING HAS READ IS THE LEAST CERTAIN OF ALL. The raw-only rows
    # carry no score, and sorting them by distance from the middle put them
    # at the bottom of the board under hundreds of rows that at least had a
    # reading. They are the ones a person has to decide, so they go first.
    rows.sort(key=lambda r: (bool(r["done"]), bool(r["unread"]),
                             r.get("source") != RAW,
                             abs(r["sure"] - mid)))
    # A QUESTION YOU HAVE ALREADY ANSWERED IS NOT A QUESTION.
    #
    # This is the bug Erik found by answering three Moonrise episodes and
    # watching them come straight back. Everything about the answer worked:
    # the memory was written against the show and the release group, the file
    # was re-planned, and the retitle went on the queue. What did not happen
    # was the READING changing - and the reading is what this list is made of.
    # The title says "English[Signs]" until the job actually rewrites it, and
    # that job is behind five thousand others, so the row kept asking.
    #
    # The row is answered the moment the work is queued, so that is what it
    # says. No new column: the queue already knows, and reading it here means
    # the row goes back to asking by itself if the job fails and the step
    # leaves the queue - which is exactly when it SHOULD ask again.
    _queued = _queued_steps()
    for r in rows:
        if r["done"] or not r.get("action"):
            continue
        want = "mark" if r["source"] == PICTURE else "retitle"
        hit = _queued.get(int(r.get("file_id") or 0)) or {}
        ords = hit.get(want)
        if ords is None:
            continue
        if want == "retitle" and ords and int(r.get("track") or -1) not in ords:
            continue
        r["done"] = True
        r["done_word"] = "on the queue"
        r["action"] = ""
        r["action_word"] = ""
        r["queued"] = True

    hs = hardsub.stats()
    sp = stt.progress()
    # COUNTED OVER ALL OF THEM, SENT AS THE FEW. The filter is applied after
    # the counts below have been taken from the whole set, which is why the
    # header can still say how many are being left out.
    shown = [r for r in rows
             if (want_done or not r["done"])
             and (want_unread or not r["unread"])]
    return {
        "rows": shown,
        # So the panel can say "showing 7 of 683" honestly rather than
        # inferring it from a list it was only given part of.
        "shown": len(shown), "found": len(rows),
        "filtered": {"done": not want_done, "unread": not want_unread},
        "mode": mode(), "mark_at": mark_at(), "dismiss_at": dismiss_at(),
        "queue": queue(),
        "counts": {
            "picture": sum(1 for r in rows if r["source"] == PICTURE),
            "tracks": sum(1 for r in rows if r["source"] not in (PICTURE, RAW)),
            # The raw check's open questions on this board - tagged onto a
            # picture row or standing on their own.
            "raw": sum(1 for r in rows if r.get("raw") and not r["done"]),
            # OF THOSE, THE ONES NOTHING HAS READ. Hidden behind a footer
            # link like the answered ones: a row with no reading behind it
            # is a decision made on the filename alone, and it should be
            # asked for rather than filling the board by default.
            "noread": sum(1 for r in rows
                          if r.get("source") == RAW and not r.get("read")),
            "unread": sum(1 for r in rows if r["unread"]),
            "band": sum(1 for r in rows if r["auto"] == "ask"
                        and not r["unread"] and not r["done"]),
            "done": sum(1 for r in rows if r["done"]),
            # Of the answered ones, how many are answered but not yet carried
            # out. "already marked" and "waiting for the queue to get to it"
            # are different states and the footer should not call them one.
            "queued": sum(1 for r in rows if r.get("queued")),
            "settled": sum(1 for r in rows if r.get("acked")),
            "actionable": sum(1 for r in rows if r["action"] and not r["done"]
                              and not r["unread"]),
        },
        "picture": hs,
        "tracks": sp,
        # The batch marker is where auto's marking actually happens, so its
        # progress is auto's progress and the panel reads it from here rather
        # than from a second endpoint.
        "marking": dict(hardsub.MARK_STATE or {}),
        # THE STANDING BACKLOG, which `marking` above is not. MARK_STATE is a
        # batch: it exists while one runs and is empty the rest of the time,
        # so between batches the panel could say nothing at all about how many
        # files are still owed a marker track. These two are counts of the
        # library and are always true.
        "marker": _safe(lambda: hardsub.marker()),
        # WHAT AUTO IS, NOW: the queue carrying mark steps. The auto_* fields
        # in STATE belong to watch_auto(), which register_only() never starts
        # because the queue does this job - so a line built from them said
        # "has not run yet · nothing waiting past the line" on an evening the
        # queue marked eighteen files and held four more.
        "auto": _safe(auto_from_queue),
        "state": {**STATE, "cycle_s": CYCLE_S, "next_run": _next_run()},
        "kinds": [{"id": k, "word": hardsub.KIND_WORDS[k]}
                  for k in hardsub.KINDS],
    }


# ------------------------------------------------------------- the actions --
def _rescan() -> None:
    """Re-read the track findings now, waiting for the answer."""
    from . import subtitletitle as stt
    try:
        stt.refresh()
    except Exception:                                            # noqa: BLE001
        pass


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
    if pic:
        return hardsub.set_kind(int(file_id), kind)
    out = stt.set_kind(int(file_id), track, kind)
    # TRUE BEFORE IT RETURNS - see dismiss(). The page repaints the moment
    # this replies, and a choice that has not been re-read yet comes back
    # showing the kind you just replaced.
    _rescan()
    return out


def dismiss(file_id: int, source: str) -> dict:
    """"This is not what you said." For a picture that teaches the OCR filter;
    for a track it sets the kind to signs, which is what a wrong dialogue call
    almost always is."""
    from . import hardsub, subtitletitle as stt
    pic, track = _split(source)
    if pic:
        return hardsub.ignore(int(file_id))
    out = stt.set_kind(int(file_id), track, stt.SIGNS)
    # THE ANSWER MUST BE TRUE BY THE TIME THIS RETURNS. cached() serves the
    # last scan and rescans behind the request, which is right for a page
    # load and wrong for the moment after a write: the page would come back
    # showing the row you just answered, and drop it ten seconds later when
    # the background scan landed. A person's click is worth the scan.
    _rescan()
    return out


def act(file_id: int, source: str, kind: str = "") -> dict:
    """Do the thing the finding calls for: mark a picture, retitle a track,
    or take a settled row off the list."""
    from . import hardsub, subtitletitle as stt
    pic, track = _split(source)
    if pic:
        return hardsub.mark_one(int(file_id), kind)
    if kind == "__leave__":
        out = stt.ack(int(file_id), track, True)
        _rescan()
        return out
    if kind:
        stt.set_kind(int(file_id), track, kind)
        stt.refresh()
    # RE-READ BEFORE ACTING, not only after. The row on the page can be a
    # minute old; the title may already have been corrected by you, by auto,
    # or by the batch, and pressing again should say so rather than failing.
    _rescan()
    here = [r for r in (stt.cached().get("rows") or [])
            if int(r.get("file_id") or 0) == int(file_id)
            and int(r.get("track") or 0) == track]
    rows = [r for r in here if r.get("rewritable")]
    if not rows:
        if not here:
            return {"ok": True, "gone": True,
                    "why": "that title already agrees with the track - "
                           "nothing left to correct"}
        r0 = here[0]
        if r0.get("settled"):
            return {"ok": True, "gone": True,
                    "why": f"you set this to "
                           f"{stt.KIND_WORDS.get(r0.get('kind'), r0.get('kind'))}"
                           f", and the title already says so"}
        return {"ok": False, "why": (r0.get("why") or "")
                or "the title carries a name nuarr did not write"}
    out = stt.fix(rows)
    _rescan()                       # see dismiss(): true before it returns
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
                if (int(r.get("file_id") or 0), int(r.get("track") or 0)) in want]
        # A SETTLED ROW IS ANSWERED, NOT CORRECTED - and the batch could not
        # say so.
        #
        # A track you set by hand whose title already agrees has nothing to
        # write. The row is still there because your choice has to stay
        # reachable, and the only thing it wants is acknowledging. This
        # filtered on `rewritable`, which is False for exactly those rows, so
        # they never reached fix(), fix() reported nothing, and all of them
        # came back on the next paint - Hunter x Hunter S02, seven of them,
        # acted on and unchanged.
        #
        # The single-row button has always known this: act() answers
        # "__leave__" by acking. The batch is the same answer given seven
        # times, so it gives the same answer.
        acked = 0
        for r in rows:
            if r.get("settled") and not r.get("acked"):
                if stt.ack(int(r["file_id"]), int(r["track"]), True).get("ok"):
                    acked += 1
        todo = [r for r in rows if r.get("rewritable")]
        fixed = stt.fix(todo) if todo else {"fixed": 0}
        out["tracks"] = fixed.get("fixed") or 0
        out["acked"] = acked
        await asyncio.to_thread(_rescan)
    bits = []
    if out["pictures"]:
        bits.append(f"marking {out['pictures']} picture"
                    + ("" if out["pictures"] == 1 else "s"))
    if out["tracks"]:
        bits.append(f"retitled {out['tracks']} track"
                    + ("" if out["tracks"] == 1 else "s"))
    if out.get("acked"):
        bits.append(f"took {out['acked']} settled row"
                    + ("" if out["acked"] == 1 else "s") + " off the list")
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
    if any(i.get("source") != PICTURE for i in items):
        _rescan()
    why = f"dismissed {n} finding" + ("" if n == 1 else "s")
    if r.get("quiet"):
        why += (f" - and {len(r['quiet'])} show"
                + ("" if len(r["quiet"]) == 1 else "s")
                + " now left alone entirely")
    return {"ok": True, "done": n, "why": why}


# ------------------------------------------------------------- the schedule --
AUTO_MARKS_PER_PASS = 25
# HOW OFTEN AUTO LOOKS FOR WORK. Not the reading cadence - the two are
# different jobs. Reading a pass of ninety pictures takes half an hour, and
# tying acting to it meant a batch of twenty-five finished in four minutes and
# then nothing happened for twenty-six, with 359 findings sitting there
# already scored and already past the line. Auto has its own clock: whenever
# the marker is free and something is waiting, it takes the next batch.
AUTO_TICK_S = 45.0


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
    STATE.update(running=True, phase="picture", t0=t0, last_error="")
    got: dict = {}
    try:
        # Auto has its own loop now (watch_auto); the pass just reads.
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


async def watch_auto() -> None:
    """Auto's own loop: take the next batch whenever the marker is free.

    SEPARATE FROM THE READERS ON PURPOSE. Reading is disk-bound and paced at
    ninety pictures every five minutes; acting on what has already been read
    and scored is a different job with a different rhythm, and hanging it off
    the reading pass made it stall for the length of a pass. This one is idle
    unless there is something past the line and nothing already running.
    """
    from . import hardsub
    await asyncio.sleep(90)
    while True:
        try:
            if mode() == "auto" and not (hardsub.MARK_STATE or {}).get("running"):
                await _auto_backlog()
            STATE["auto_due"] = time.time() + AUTO_TICK_S
        except Exception as e:                                   # noqa: BLE001
            STATE["last_error"] = f"auto: {type(e).__name__}: {e}"
        await asyncio.sleep(AUTO_TICK_S)


async def register_only() -> None:
    """The schedule entry, without the runner. The readers are jobs now."""
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "What subtitles does each file carry?", "Subtitles",
            CYCLE_S,
            what=("Samples frames of files that report no subtitle track and "
                  "reads the events of text tracks whose title looks wrong. "
                  "Both run as jobs on the main queue now, dealt across the "
                  "pool disks with everything else."))
    except Exception:                                            # noqa: BLE001
        pass
    STATE["due_at"] = 0.0
    # AND THE ROW REPORTS ITSELF, instead of saying "never" forever.
    #
    # schedules.snapshot() derives last_run, next_run and status from beat(),
    # which every clock-driven loop calls at the top of a pass. This check has
    # no pass of its own any more - its two readers are `subread` jobs on the
    # main queue - so nothing ever called it, and the row read
    #
    #     subkind   Subtitles   every 300s   last run: never
    #
    # over readers that had between them filled 4,346 hardsub rows and 683
    # subtitle_shape rows. That is what a broken check looks like.
    await _heartbeat()


async def _heartbeat() -> None:
    """Report the subread feeder on the schedule row that names it."""
    from .db import cursor
    try:
        from . import schedules
    except Exception:                                            # noqa: BLE001
        return
    while True:
        await asyncio.sleep(CYCLE_S)
        try:
            since = time.time() - CYCLE_S
            with cursor() as cur:
                done = int(cur.execute(
                    "SELECT COUNT(*) c FROM jobs WHERE kind='subread' "
                    "  AND state='done' AND COALESCE(finished_at,0) > ?",
                    (since,)).fetchone()["c"] or 0)
                left = int(cur.execute(
                    "SELECT COUNT(*) c FROM jobs WHERE kind='subread' "
                    "  AND state IN ('queued','running')").fetchone()["c"] or 0)
            said = (f"{done} read this pass" if done else "idle - nothing read")
            if left:
                said += f", {left} waiting"
            # AND THE DISMISSALS, which are the one thing auto does that is
            # not a rewrite and therefore has no queue step of its own. See
            # hardsub.auto_dismiss: marking a file belongs on the queue with
            # every other rewrite, but recording that a finding is not worth
            # acting on is a single row and nothing to schedule around.
            try:
                from . import hardsub as _hs
                drop = await asyncio.to_thread(_hs.auto_dismiss, 50)
                if drop.get("dropped"):
                    said += f", {drop['dropped']} dismissed"
            except Exception:                                    # noqa: BLE001
                pass
            schedules.beat(SCHED_KEY, said)
            STATE["last_run"] = time.time()
        except Exception as e:                                   # noqa: BLE001
            STATE["last_error"] = f"heartbeat: {type(e).__name__}: {e}"


async def watch() -> None:
    r"""Start both readers on the shared runner and get out of the way.

    ONE SCHEDULE WAS THE RIGHT IDEA AND A CLOCK WAS THE WRONG MECHANISM.
    These two answer the same question - what does this file carry - so they
    belong on one page and under one heading, and that has not changed. What
    has changed is that a batch and a sleep are no longer how either of them
    is paced: they take turns here, ninety pictures then forty tracks and then
    five minutes of nothing, on a box that is idle most of the night. Worse,
    they took turns SEQUENTIALLY, so the track reader waited out the picture
    sampler even when the two would have used different disks.
    
    Each runs on its own now, asking the gate before every file, two at a time
    across different spindles, stepping around a disk somebody is reading
    from. run() stays exactly as it was: it is the "check some now" button,
    and a button is a batch by definition.
    """
    from . import hardsub, subtitletitle as stt
    try:
        from . import schedules
        schedules.register(
            SCHED_KEY, "What subtitles does each file carry?", "Subtitles",
            CYCLE_S,
            what=("Samples frames of files that report no subtitle track and "
                  "reads the events of text tracks whose title looks wrong. "
                  "Both work continuously while the box is idle, and both "
                  "yield to the job gate before every file."))
    except Exception:                                            # noqa: BLE001
        pass
    STATE["due_at"] = 0.0
    # NOTE: web.py does not call this - it calls register_only(), and the
    # readers run as `subread` jobs on the main queue. The heartbeat that
    # keeps this check's schedule row honest lives there for that reason.
    await asyncio.gather(hardsub.watch(), stt.watch())

