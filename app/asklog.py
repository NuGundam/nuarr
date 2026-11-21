r"""nuarr - what it guessed, what you said, and what two matching answers teach.

WHY THIS EXISTS (2026-11-20 audit). The question systems kept your ANSWER
(sub_memory, aud_memory) and nothing else: not the guess nuarr had made, not
how sure it was, not when you answered. So "how often does the person agree
with the guess at 70 %?" - the number every act-by-itself line should be set
from - could not be computed, and the lines were set by feel.

Two things live here.

THE LOG. One row per answer a person gives: the system (sub / aud), the
question, the show and release group of the file, nuarr's guess and its sure %
as the question stood when it was answered, and the answer. stats() turns that
into agreement by sure-% bucket.

SHOW + RELEASE GROUP LEARNING. Asked again and again about the same show the
answers repeat (24 of 34 burned-in-picture answers matched the show's earlier
ones) - but what repeats is the RELEASE: one group hard-subs a show, another
ships it clean, so a show-wide rule would be wrong the day the arr upgrades to
a different group. LEARN_N matching answers for the same show AND the same
release group, with none against, are written into the memory the planners
already read, under the scope "showgroup" - narrower than the show, wider than
the file. One answer the other way removes it again. The memory panel lists the
row like any other, and Forget works on it.

NuSentry does the same with its health checks (learn.py: two "fine" on a
pattern and the model is no longer asked); this is that rule, for nuarr.
"""
from __future__ import annotations

import json
import time

from .db import cursor

LEARN_N = 2
SCOPE = "showgroup"
_READY = False
_MEM = {"sub": "sub_memory", "aud": "aud_memory"}
_QUEUE = {"sub": "sub_queue", "aud": "aud_queue"}


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ask_log(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                at       REAL NOT NULL,
                system   TEXT NOT NULL,          -- sub | aud
                file_id  INTEGER,
                show     TEXT NOT NULL DEFAULT '',
                grp      TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL,
                guess    TEXT NOT NULL DEFAULT '',
                sure     INTEGER,                -- 0-100 as shown, NULL = not recorded
                answer   TEXT NOT NULL,
                by       TEXT NOT NULL DEFAULT 'you')""")
        cur.execute("CREATE INDEX IF NOT EXISTS ask_log_sg ON ask_log(system, show, grp, question)")
    _READY = True


def _keys(system: str, file_id: int, path: str) -> tuple:
    """(show key, release group) in the units the two memories already use."""
    try:
        if system == "aud":
            from . import audplan as m
        else:
            from . import subplan as m
        return (m.show_key(int(file_id)) or ""), (m.group_key(path or "") or "")
    except Exception:                                            # noqa: BLE001
        return "", ""


def sg_key(system: str, file_id: int, path: str) -> str:
    show, grp = _keys(system, file_id, path)
    return f"{show}|{grp}" if show and grp else ""


def _guess(system: str, file_id: int, question: str) -> tuple:
    """(guess, sure) from the open question, read before the answer removes it.

    Picture: the score IS the guess - at or over 50 it leans "mark". Audio tag:
    the question is only asked when what was heard disagrees with the tag, so
    the guess is "tag". A title question leans "retitle" for the same reason.
    """
    try:
        with cursor() as cur:
            r = cur.execute(f"SELECT asks FROM {_QUEUE[system]} WHERE file_id=?", (int(file_id),)).fetchone()
        asks = [a for a in json.loads((r["asks"] if r else "") or "[]") if a.get("q") == question]
    except Exception:                                            # noqa: BLE001
        asks = []
    if not asks:
        return "", None
    sures = [int(a["sure"]) for a in asks if a.get("sure") is not None]
    sure = max(sures) if sures else None
    if question == "picture":
        return ("" if sure is None else "mark" if sure >= 50 else "leave"), sure
    if question == "tag":
        return "tag", sure
    if question == "title":
        return "retitle", sure
    return "", sure


def note(system: str, file_id: int, path: str, question: str, answer: str, by: str = "you") -> dict:
    """A person answered. Log it, then see what the show + group now teaches.
    Call BEFORE the queue row is re-planned, so the open question is still there."""
    try:
        init()
        show, grp = _keys(system, file_id, path)
        guess, sure = _guess(system, file_id, question)
        with cursor() as cur:
            cur.execute("INSERT INTO ask_log(at,system,file_id,show,grp,question,guess,sure,answer,by) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), system, int(file_id), show, grp, question, guess, sure, str(answer), by))
        return promote(system, show, grp, question)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:160]}


def promote(system: str, show: str, grp: str, question: str) -> dict:
    """Write, refresh or remove the show + group memory row for this question."""
    if not (show and grp):
        return {"ok": True, "learned": ""}
    init()
    mem, key = _MEM[system], f"{show}|{grp}"
    with cursor() as cur:
        answers = [str(r["answer"]) for r in cur.execute(
            "SELECT answer FROM ask_log WHERE system=? AND show=? AND grp=? AND question=? ORDER BY at",
            (system, show, grp, question))]
        if len(answers) >= LEARN_N and len(set(answers)) == 1:
            cur.execute(
                f"INSERT INTO {mem}(scope,skey,question,answer,at,used,note) VALUES(?,?,?,?,?,0,?) "
                "ON CONFLICT(scope,skey,question) DO UPDATE SET answer=excluded.answer, at=excluded.at, "
                "note=excluded.note",
                (SCOPE, key, question, answers[0], time.time(),
                 f"learned: you answered this {len(answers)} times for this show from {grp}, always the same"))
            return {"ok": True, "learned": f"{SCOPE} {key}", "answer": answers[0], "n": len(answers)}
        if len(set(answers)) > 1:
            cur.execute(f"DELETE FROM {mem} WHERE scope=? AND skey=? AND question=?", (SCOPE, key, question))
    return {"ok": True, "learned": ""}


def recall(system: str, file_id: int, path: str, question: str) -> tuple:
    """The learned show + group answer, as (answer, whose) - or ("", "")."""
    key = sg_key(system, file_id, path)
    if not key:
        return "", ""
    try:
        with cursor() as cur:
            r = cur.execute(f"SELECT answer FROM {_MEM[system]} WHERE scope=? AND skey=? AND question=?",
                            (SCOPE, key, question)).fetchone()
        if r:
            return str(r["answer"]), f"this show from {key.split('|', 1)[1]} - learned from your answers"
    except Exception:                                            # noqa: BLE001
        pass
    return "", ""


def backfill() -> dict:
    """Once: the file-scope answers already in the two memories become log rows
    (no guess - it was never recorded), so learning starts from your history."""
    from .db import kv_get, kv_set
    init()
    if kv_get("asklog.backfilled"):
        return {"ok": True, "added": 0}
    added, seen = 0, set()
    for system, mem in _MEM.items():
        try:
            with cursor() as cur:
                rows = [dict(r) for r in cur.execute(
                    f"SELECT m.skey, m.question, m.answer, m.at, f.path FROM {mem} m "
                    "  JOIN files f ON f.id = CAST(m.skey AS INTEGER) WHERE m.scope='file'")]
        except Exception:                                        # noqa: BLE001
            rows = []
        for r in rows:
            show, grp = _keys(system, int(r["skey"]), r["path"] or "")
            with cursor() as cur:
                cur.execute("INSERT INTO ask_log(at,system,file_id,show,grp,question,guess,sure,answer,by) "
                            "VALUES(?,?,?,?,?,?,'',NULL,?,'you (before the log)')",
                            (float(r["at"] or 0), system, int(r["skey"]), show, grp, r["question"], r["answer"]))
            added += 1
            seen.add((system, show, grp, r["question"]))
    learned = sum(1 for k in seen if promote(*k).get("learned"))
    kv_set("asklog.backfilled", str(time.time()))
    return {"ok": True, "added": added, "learned": learned}


def stats() -> dict:
    """Agreement with the guess, by system, question and sure-% bucket, and the
    show + group rows learned so far."""
    init()
    out: dict = {"total": 0, "by": [], "learned": [], "learn_n": LEARN_N}
    with cursor() as cur:
        out["total"] = int(cur.execute("SELECT COUNT(*) n FROM ask_log").fetchone()["n"] or 0)
        rows = [dict(r) for r in cur.execute(
            "SELECT system, question, guess, sure, answer FROM ask_log WHERE guess != '' AND sure IS NOT NULL")]
        for system, mem in _MEM.items():
            try:
                out["learned"] += [dict(r, system=system) for r in cur.execute(
                    f"SELECT skey, question, answer, at, note FROM {mem} WHERE scope=? ORDER BY at DESC", (SCOPE,))]
            except Exception:                                    # noqa: BLE001
                pass
    agg: dict = {}
    for r in rows:
        b = min(90, int(r["sure"]) // 10 * 10)
        k = (r["system"], r["question"], b)
        a = agg.setdefault(k, [0, 0])
        a[0] += 1
        a[1] += int(r["guess"] == r["answer"])
    out["by"] = [{"system": k[0], "question": k[1], "sure": f"{k[2]}-{k[2] + 9}%", "asked": v[0], "agreed": v[1]}
                 for k, v in sorted(agg.items())]
    return out
