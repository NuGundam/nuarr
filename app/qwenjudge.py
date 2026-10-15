r"""nuarr - Qwen reads the picture check's lines and says whether they are dialogue.

WHY. The burned-in subtitle check (hardsub.py) samples frames, OCRs the text
low in the picture, and scores what it read with fixed rules - word shapes,
function words, credit words, how often text was there. Where the rules
cannot decide, the file waits for a person between 30% and 85%. Measured
2026-10-14 on the answers on record: given only the sorted word list the
rules keep, Qwen agreed on 79% of dismissals and 47% of dialogue - no better.
Given the LINES, in order, it called all four test files right: a Cartoon
Network logo repeated in every frame and a title card were "not dialogue"
(100% and 85% sure), two episodes of real burned-in subtitles "dialogue"
(85% and 95%). So the reader now keeps its lines, and this judges them.

HOW. A child process (Qwen is 7 GB and native code - see submake_worker for
why that lives in a child) takes a batch of files whose lines have no verdict
yet, asks Qwen once per file, and writes hardsub_qwen. It runs on the heavy
lane so it cannot collide with the Subtitle maker or an install, when there is
something to judge. hardsub.score_of shows the verdict; it changes the score
only when the switch on the scoring panel is on.

`python -m app.qwenjudge --batch N` is the child.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

POLL_S = 10 * 60
BATCH = 200
LANE = "Qwen judge"
SYS = ("You judge OCR output from a video. A program took a few frames spread through a TV episode "
       "or film and read the text in the bottom third of each one. You get those reads in time order. "
       "OCR errors and garbled fragments are normal, and real subtitles are often short "
       "(\"Exactly!\", \"Hey, lunchtime.\"). Decide whether the video has BURNED-IN DIALOGUE "
       "SUBTITLES: different sentences of people talking at different times. The same logo or "
       "channel name in every frame, one title card, signs, song lyrics, credits, and reads that are "
       "only letter noise are NOT dialogue. Reply exactly: "
       "DIALOGUE CHANCE: a number 0-100 (100 = certainly burned-in dialogue, 0 = certainly "
       "not); WHY: one short sentence.")
# LEARNING FROM THE ANSWERS ON RECORD. A show keeps the same on-screen text
# from episode to episode - Beet the Vandel Buster's burned-in subtitles look
# alike in every episode, and so does a channel logo - and the misses were
# clustered by show (5 of 12 were Beet, 2 GaoGaiGar). So each file is judged
# with a tally of a person's answers on OTHER files of the same show in
# front of it. A file's own answer is never in its tally, which is also what keeps the measurement honest. When a show gets
# a new answer, its files are judged again with it.
EXAMPLES = 1          # 0 switches the tally off (measuring)
LEARN_NOTE = ("You may be told what a person decided about OTHER episodes of the same show; a show's "
              "on-screen text is usually the same kind in every episode, so weigh that heavily.")


def series_of(path: str) -> str:
    d = os.path.dirname(path or "")
    if re.match(r"(?i)^(season\s*\d+|specials|s\d+)$", os.path.basename(d)):
        d = os.path.dirname(d)
    return d.lower()


def pool(cur) -> dict:
    """{series: [(file_id, 'yes'|'no', lines)]} - the answers on record."""
    ign = {int(r["file_id"]) for r in cur.execute("SELECT file_id FROM hardsub_ignored")}
    out: dict = {}
    for r in cur.execute("SELECT file_id, path, chosen, lines FROM hardsub "
                         " WHERE COALESCE(lines,'') NOT IN ('','[]') "
                         "   AND (chosen IN ('dialogue','hybrid','none','signs') OR file_id IN "
                         "        (SELECT file_id FROM hardsub_ignored)) ORDER BY file_id"):
        fid = int(r["file_id"])
        side = "no" if (fid in ign or r["chosen"] in ("none", "signs")) else "yes"
        out.setdefault(series_of(r["path"]), []).append((fid, side, r["lines"]))
    return out


def pick(pl: dict, fid: int, path: str) -> list:
    """Every answer on the same show, never this file's own."""
    if EXAMPLES <= 0:
        return []
    return [x for x in pl.get(series_of(path), []) if x[0] != int(fid)]


def _ex_sig(ex: list) -> str:
    """What the prompt says, so a changed tally is judged again."""
    if not ex:
        return ""
    y = sum(1 for x in ex if x[1] == "yes")
    return f"{y}y{len(ex) - y}n"


def _ex_text(ex: list) -> str:
    # A TALLY, NOT THE LINES. Measured on the 149 answers on record, the
    # other episodes' own lines put in front of the file made Qwen worse -
    # 31 of 101 dialogue files against 76 without - because it compared the
    # texts ("does not match the pattern of the previous files") instead of
    # reading them. A sentence saying what a person decided does not invite that.
    if not ex:
        return ""
    y = sum(1 for x in ex if x[1] == "yes")
    n = len(ex) - y
    said = ("all had burned-in dialogue subtitles" if not n else
            "none had burned-in dialogue subtitles" if not y else
            f"{y} had burned-in dialogue subtitles and {n} did not")
    return (f"A person has already checked {len(ex)} other episode(s) of this same show: {said}. "
            f"OCR on this show may be just as fragmentary.\n\n")


# ONE NUMBER, NOT A VERDICT AND A CONFIDENCE. "VERDICT: NOT DIALOGUE; SURE: 0"
# came back meaning "0% dialogue", which read as a sure "not" became 100%.
STATE: dict = {"running": False, "last_run": 0.0, "next_run": 0.0, "last_result": "",
               "judged": 0, "error": "", "done": 0, "total": 0, "file": "", "started": 0.0}
_PROC = None


# THE SAME QUESTION FOR TEXT TRACKS. A track whose title contradicts its cue
# count ("Signs only" at the pace of people talking) is the third reader's
# finding; its lines are clean text, so Qwen reads a sample of them directly.
TRACK_SYS = ("You judge a subtitle track from a video. You get lines sampled through the episode, "
             "in order, and the track's title. Decide whether the track is the DIALOGUE (people "
             "talking), or SIGNS / SONGS only (on-screen text, song lyrics, karaoke, typesetting), "
             "or BOTH. Reply exactly: DIALOGUE CHANCE: a number 0-100; KIND: dialogue or signs or "
             "songs or both; WHY: one short sentence.")


def init_tracks() -> None:
    from .db import cursor
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subtitle_qwen(
                file_id    INTEGER,
                track      INTEGER,
                size       INTEGER,
                dialogue_p INTEGER,
                kind       TEXT,
                why        TEXT,
                at         REAL,
                PRIMARY KEY (file_id, track)
            )""")
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(subtitle_qwen)")}
        if "ex" not in cols:
            cur.execute("ALTER TABLE subtitle_qwen ADD COLUMN ex TEXT NOT NULL DEFAULT ''")


# THE SAME TALLY FOR TRACKS: the kinds a person set on other tracks of the
# same show, never this track's own.
def track_pool(rows: list) -> dict:
    out: dict = {}
    for r in rows:
        if r.get("chosen") and r.get("kind"):
            out.setdefault(series_of(r.get("path") or ""), []).append(
                ((int(r["file_id"]), int(r["track"])), str(r["kind"])))
    return out


def track_tally(pl: dict, key: tuple, path: str) -> dict:
    if EXAMPLES <= 0:
        return {}
    t: dict = {}
    for k, kind in pl.get(series_of(path), []):
        if k != key and k[0] != key[0]:
            t[kind] = t.get(kind, 0) + 1
    return t


def _tally_sig(t: dict) -> str:
    return ",".join(f"{k}{v}" for k, v in sorted(t.items()))


def _tally_text(t: dict) -> str:
    if not t:
        return ""
    words = {"dialogue": "dialogue", "hybrid": "dialogue + signs", "signs": "signs / songs only",
             "none": "not subtitles"}
    return (f"A person already set the kind of {sum(t.values())} similar track(s) on other episodes of "
            f"this show: " + ", ".join(f"{v} {words.get(k, k)}" for k, v in sorted(t.items())) + ".\n")


def track_verdicts() -> dict:
    """{(file_id, track): {"p", "kind", "why", "size"}}"""
    from .db import cursor
    init_tracks()
    out = {}
    with cursor() as cur:
        for r in cur.execute("SELECT * FROM subtitle_qwen"):
            out[(int(r["file_id"]), int(r["track"]))] = {"p": int(r["dialogue_p"] or 0),
                                                         "kind": r["kind"] or "", "why": r["why"] or "",
                                                         "size": int(r["size"] or 0), "ex": r["ex"] or ""}
    return out


def pending_tracks() -> list:
    from . import subtitletitle as stt
    have = track_verdicts()
    rows = stt.cached().get("rows") or []
    pl = track_pool(rows)
    out = []
    for r in rows:
        if r.get("unread") or not r.get("mkv_id") and r.get("mkv_id") != 0:
            continue
        k = (int(r["file_id"]), int(r["track"]))
        v = have.get(k)
        tally = track_tally(pl, k, r.get("path") or "")
        if v and v["size"] == int(r.get("size") or 0) and v.get("ex", "") == _tally_sig(tally):
            continue
        out.append({"file_id": k[0], "track": k[1], "mkv_id": int(r["mkv_id"]), "path": r.get("path"),
                    "size": int(r.get("size") or 0), "title": r.get("old") or "",
                    "label": r.get("label") or "", "tally": tally})
    return out


# ----------------------------------------------------------------- server --
def init_learn() -> None:
    from .db import cursor
    with cursor() as cur:
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(hardsub_qwen)")}
        if cols and "ex" not in cols:
            cur.execute("ALTER TABLE hardsub_qwen ADD COLUMN ex TEXT NOT NULL DEFAULT ''")


def pending(limit: int = BATCH) -> list:
    """Files with lines and no verdict, a verdict older than the lines, or a
    verdict given with other examples than the show's answers now offer."""
    from .db import cursor
    init_learn()
    out = []
    with cursor() as cur:
        pl = pool(cur)
        for r in cur.execute(
                "SELECT h.file_id, h.path, h.at, q.at qat, q.ex FROM hardsub h "
                "  LEFT JOIN hardsub_qwen q ON q.file_id = h.file_id "
                " WHERE COALESCE(h.lines,'') NOT IN ('', '[]') ORDER BY h.at DESC").fetchall():
            if r["qat"] is None or r["qat"] < r["at"] or \
                    (r["ex"] or "") != _ex_sig(pick(pl, r["file_id"], r["path"])):
                out.append(int(r["file_id"]))
                if len(out) >= limit:
                    break
    return out


async def run() -> str:
    from . import heavy, joblog
    from .config import NO_WINDOW
    todo = pending()
    tracks = pending_tracks()[:BATCH]
    if not todo and not tracks:
        return "nothing to judge"
    got, holder = heavy.claim(LANE, ttl=1800)
    if not got:
        return f"waiting - {holder} has the GPU lane"
    global _PROC
    STATE.update(running=True, error="", done=0, total=min(len(todo), BATCH), file="",
                 started=time.time())
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..",
                          "qwen_tracks.json")
        from .config import DATA_DIR
        tf = os.path.join(str(DATA_DIR), "qwen_tracks.json")
        with open(tf, "w", encoding="utf-8") as f:
            json.dump(tracks, f)
        STATE["total"] = min(len(todo), BATCH) + len(tracks)
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.qwenjudge", "--batch", str(BATCH), "--tracks", tf, cwd=root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=NO_WINDOW)
        _PROC = p
        n, err = 0, ""
        # AS IT GOES, so the Processing System card and the reader row move
        # while it works instead of jumping when the batch ends.
        while True:
            ln = await p.stdout.readline()
            if not ln:
                break
            try:
                d = json.loads(ln.decode("utf-8", "replace"))
            except Exception:                                    # noqa: BLE001
                continue
            if "progress" in d:
                STATE.update(done=int(d["progress"]), total=int(d.get("total") or STATE["total"]),
                             file=str(d.get("file") or ""))
            n = int(d.get("judged", n))
            err = d.get("error", err)
        await p.wait()
        _PROC = None
        if n:
            STATE["secs_each"] = (time.time() - STATE["started"]) / n
        res = f"judged {n} file(s)" + (f" - {err}" if err else "")
        STATE.update(judged=STATE["judged"] + n, error=err)
        if n:
            joblog.log(f"qwen judge: {res}", "info", system="hardsub")
        try:
            from . import hardsub
            hardsub._QW["at"] = 0.0          # the scorer re-reads the verdicts
        except Exception:                                        # noqa: BLE001
            pass
        return res
    finally:
        STATE.update(running=False, last_run=time.time())
        heavy.release(LANE)


async def watch() -> None:
    await asyncio.sleep(240)
    while True:
        if paused():
            STATE["last_result"] = "paused on the Concurrency page"
            STATE["next_run"] = time.time() + POLL_S
            await asyncio.sleep(POLL_S)
            continue
        try:
            STATE["last_result"] = await run()
        except Exception as e:                                   # noqa: BLE001
            STATE["error"] = f"{type(e).__name__}: {e}"
        STATE["next_run"] = time.time() + POLL_S
        await asyncio.sleep(POLL_S)


def agreement() -> dict:
    """How often Qwen agreed with the answers on record. A file dismissed as
    not dialogue, or set to 'none' by hand, counts as no; one set to
    dialogue or dialogue + signs by hand counts as yes."""
    from .db import cursor
    out = {"no": {"n": 0, "agree": 0}, "yes": {"n": 0, "agree": 0}, "wrong": []}
    try:
        with cursor() as cur:
            ign = {int(r["file_id"]) for r in cur.execute("SELECT file_id FROM hardsub_ignored")}
            for r in cur.execute("SELECT h.file_id, h.path, h.chosen, q.dialogue_p, q.verdict "
                                 "  FROM hardsub h JOIN hardsub_qwen q ON q.file_id = h.file_id"):
                fid = int(r["file_id"])
                if fid in ign or r["chosen"] == "none":
                    side = "no"
                elif r["chosen"] in ("dialogue", "hybrid"):
                    side = "yes"
                else:
                    continue
                said_yes = int(r["dialogue_p"] or 0) >= 50
                ok = said_yes == (side == "yes")
                out[side]["n"] += 1
                out[side]["agree"] += int(ok)
                if not ok and len(out["wrong"]) < 12:
                    out["wrong"].append({"file": os.path.basename(r["path"] or ""), "truth": side,
                                         "p": int(r["dialogue_p"] or 0)})
            # TRACKS: a kind set by hand is the answer.
            try:
                from . import subtitletitle as stt
                tv = track_verdicts()
                tr = {"yes": {"n": 0, "agree": 0}, "no": {"n": 0, "agree": 0}}
                for r in (stt.cached().get("rows") or []):
                    if not r.get("chosen"):
                        continue
                    v = tv.get((int(r["file_id"]), int(r["track"])))
                    if not v:
                        continue
                    side = "no" if r.get("kind") in ("signs", "none") else "yes"
                    tr[side]["n"] += 1
                    tr[side]["agree"] += int((v["p"] >= 50) == (side == "yes"))
                out["tracks"] = tr
                out["tracks_judged"] = len(tv)
                out["tracks_waiting"] = len(pending_tracks())
            except Exception:                                    # noqa: BLE001
                pass
            out["judged"] = cur.execute("SELECT COUNT(*) n FROM hardsub_qwen").fetchone()["n"]
            out["waiting"] = cur.execute(
                "SELECT COUNT(*) n FROM hardsub h LEFT JOIN hardsub_qwen q ON q.file_id=h.file_id "
                " WHERE COALESCE(h.lines,'') NOT IN ('','[]') AND (q.file_id IS NULL OR q.at < h.at)"
            ).fetchone()["n"]
            out["with_lines"] = cur.execute(
                "SELECT COUNT(*) n FROM hardsub WHERE COALESCE(lines,'') NOT IN ('','[]')").fetchone()["n"]
    except Exception as e:                                       # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


KV_PAUSED = "qwenjudge.paused"


def paused() -> bool:
    from .db import kv_get
    return str(kv_get(KV_PAUSED) or "0") == "1"


def set_paused(on: bool) -> None:
    from .db import kv_set
    kv_set(KV_PAUSED, "1" if on else "0")


def cost() -> dict:
    n = 0
    try:
        from .db import cursor
        with cursor() as cur:
            n = cur.execute("SELECT COUNT(*) n FROM hardsub_qwen").fetchone()["n"]
    except Exception:                                            # noqa: BLE001
        pass
    per = STATE.get("secs_each")
    return {"cpu": 1, "gpu": 3, "ram": 2, "disk": 1, "lead": "gpu",
            "why": ("ONE MODEL, LOADED ONCE PER RUN. Qwen2.5-7B (about 8 GB of video memory) reads "
                    "eight pictures' lines at a time. Loading it is most of a short run; after that a "
                    "picture is a fraction of a second. Nothing is decoded and no pool disk is read - "
                    "the lines are already in the database."),
            "measured": ({"jobs": n, "secs": round(per, 1)} if per else None)}


def stop() -> bool:
    p = _PROC
    if p is None or p.returncode is not None:
        return False
    try:
        import subprocess
        from .config import NO_WINDOW
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW)
    except Exception:                                            # noqa: BLE001
        return False
    return True


def as_running() -> list:
    """The judge as a worker card for the job feed, while it runs."""
    if not STATE.get("running"):
        return []
    el = time.time() - float(STATE.get("started") or time.time())
    tot, done = int(STATE.get("total") or 0), int(STATE.get("done") or 0)
    pct = (done / tot) if tot else 0.0
    return [{
        "job_id": "qwenjudge", "pool": "qwen", "kind": "qwen",
        "title": f"Qwen judge - {done} of {tot} pictures", "file": STATE.get("file") or "",
        "path": "", "stage": "reading the lines" if done else "loading Qwen",
        "progress": pct, "elapsed_s": round(el),
        "eta_s": round(el / pct - el) if 0.05 < pct < 1 else None, "eta_est": True,
        "plan": "read each picture's lines in order and say how likely they are to be dialogue",
        "why_pool": ("The Qwen judge: for the burned-in subtitle check. It reads the lines the "
                     "picture check kept, with a language model, and stores the chance they are "
                     "dialogue - which the sure % shows, and counts once switched on."),
        "doing": {"tool": "Qwen2.5-7B (CTranslate2)", "hw": "GPU", "why": "judging"},
        "actions": [{"kind": "subtitle", "what": "judge burned-in words",
                     "why": "the rules could not always tell dialogue from logos and title cards",
                     "detail": ""}],
        "venc": None, "fps": 0.0, "speed": 0.0, "paused_for_viewer": False,
        "paused_for_load": False, "pace_factor": 0.0, "pace_why": "", "cancelled": False,
        "by_bytes": False, "disk": "", "dest_disk": "",
    }]


def waiting() -> int:
    try:
        return len(pending(100000))
    except Exception:                                            # noqa: BLE001
        return 0


def snapshot() -> dict:
    from . import hardsub
    return {"state": dict(STATE), "on": hardsub.qwen_on(), "weight": hardsub.QWEN_WEIGHT,
            "agreement": agreement(), "poll_s": POLL_S}


# ------------------------------------------------------------------ child --
def _judge(limit: int) -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import audiolang
    audiolang._add_cuda_dirs()
    from app.db import cursor
    from app.submake import LLM_DIR
    import ctranslate2
    import tokenizers
    if not os.path.exists(os.path.join(LLM_DIR, "model.bin")):
        print(json.dumps({"judged": 0, "error": "Qwen is not downloaded - see the Qwen page"}), flush=True)
        return
    tok = tokenizers.Tokenizer.from_file(os.path.join(LLM_DIR, "tokenizer.json"))
    try:
        g = ctranslate2.Generator(LLM_DIR, device="cuda", compute_type="int8_float16")
    except Exception:                                            # noqa: BLE001
        g = ctranslate2.Generator(LLM_DIR, device="cpu", compute_type="int8")
    init_learn()
    ids = list(_ONLY) or pending(limit)
    with cursor() as cur:
        pl = pool(cur)
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            f"SELECT file_id, path, size, samples, lines FROM hardsub WHERE file_id IN "
            f"({','.join('?' * len(ids))})", ids)] if ids else []
    done = 0
    print(json.dumps({"progress": 0, "total": len(rows)}), flush=True)
    for i in range(0, len(rows), 8):
        part = rows[i:i + 8]
        print(json.dumps({"progress": i, "total": len(rows),
                          "file": os.path.basename(part[0]["path"] or "")}), flush=True)
        prompts, exs = [], []
        for r in part:
            ex = pick(pl, r["file_id"], r["path"])
            exs.append(_ex_sig(ex))
            try:
                L = json.loads(r["lines"] or "[]")
            except Exception:                                    # noqa: BLE001
                L = []
            body = "\n".join(f"{x['t']}s: {x['text']}" for x in L)[:3000]
            prompts.append(tok.encode(
                f"<|im_start|>system\n{SYS} {LEARN_NOTE if ex else ''}<|im_end|>\n<|im_start|>user\n"
                f"{_ex_text(ex)}File: "
                f"{os.path.basename(r['path'] or '')}\n{len(L)} of {r['samples'] or len(L)} sampled "
                f"frames had text:\n{body}<|im_end|>\n<|im_start|>assistant\n").tokens)
        res = g.generate_batch(prompts, max_length=80, sampling_topk=1,
                               include_prompt_in_result=False, end_token="<|im_end|>")
        with cursor() as cur:
            for r, out, sig in zip(part, res, exs):
                t = tok.decode(out.sequences_ids[0]).strip()
                c = re.search(r"CHANCE:\s*(\d{1,3})", t, re.I)
                w = re.search(r"WHY:\s*(.+)", t, re.I | re.S)
                if not c:
                    continue
                p = max(0, min(100, int(c.group(1))))
                verdict = "dialogue" if p >= 50 else "not dialogue"
                if _DRY:
                    print(json.dumps({"id": int(r["file_id"]), "p": p, "ex": sig, "why": t[:300]}), flush=True)
                    done += 1
                    continue
                cur.execute(
                    "INSERT INTO hardsub_qwen(file_id,path,size,verdict,dialogue_p,why,at,ex) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, "
                    "size=excluded.size, verdict=excluded.verdict, dialogue_p=excluded.dialogue_p, "
                    "why=excluded.why, at=excluded.at, ex=excluded.ex",
                    (int(r["file_id"]), r["path"], r["size"], verdict, p,
                     (w.group(1).strip().splitlines()[0] if w else "")[:200], time.time(), sig))
                done += 1
    print(json.dumps({"judged": done}), flush=True)


def _judge_tracks(tf: str, offset: int) -> None:
    import subprocess as sp
    from app.db import cursor
    from app.jobs import _ffmpeg_exe
    from app import qwenlib
    try:
        tracks = json.load(open(tf, encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return
    init_tracks()
    users, keep = [], []
    for i, t in enumerate(tracks):
        print(json.dumps({"progress": offset + i, "total": offset + len(tracks),
                          "file": os.path.basename(t["path"] or "")}), flush=True)
        try:
            raw = sp.run([_ffmpeg_exe(), "-v", "error", "-i", t["path"], "-map", f"0:{t['mkv_id']}",
                          "-f", "srt", "-"], capture_output=True, timeout=180).stdout.decode("utf-8", "replace")
        except Exception:                                        # noqa: BLE001
            continue
        lines = []
        for b in re.split(r"\r?\n\s*\r?\n", raw.strip()):
            L = [x for x in b.splitlines() if x.strip()]
            k = next((j for j, x in enumerate(L) if "-->" in x), None)
            if k is not None:
                body = " ".join(re.sub(r"<[^>]+>|\{[^}]*\}", "", x) for x in L[k + 1:]).strip()
                if body:
                    lines.append(body[:120])
        if not lines:
            continue
        step = max(1, len(lines) // 40)
        sample = lines[::step][:40]
        users.append(_tally_text(t.get("tally") or {}) +
                     f"Track title: {t['title'] or '(none)'}\nFrom: {t['label']}\n"
                     f"{len(lines)} lines in all; a sample in order:\n" + "\n".join(sample))
        keep.append(t)
    answers = qwenlib.ask_many(TRACK_SYS + " " + LEARN_NOTE, users, max_len=80) if users else []
    with cursor() as cur:
        for t, a in zip(keep, answers):
            c = re.search(r"CHANCE:\s*(\d{1,3})", a, re.I)
            if not c:
                continue
            k = re.search(r"KIND:\s*(\w+)", a, re.I)
            w = re.search(r"WHY:\s*(.+)", a, re.I | re.S)
            # THE KIND IS THE ANSWER; THE NUMBER FOLLOWS IT. Measured on 30
            # tracks set by hand: the kind matched 27, while the chance it
            # gave beside it often did not match its own kind ("signs", 70%).
            kind = (k.group(1).lower() if k else "")
            p = max(0, min(100, int(c.group(1))))
            p = (max(p, 75) if kind == "dialogue" else 45 if kind == "both"
                 else min(p, 25) if kind in ("signs", "songs") else p)
            if _DRY:
                print(json.dumps({"key": [t["file_id"], t["track"]], "p": p, "kind": kind}), flush=True)
                continue
            cur.execute("INSERT INTO subtitle_qwen(file_id,track,size,dialogue_p,kind,why,at,ex) "
                        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(file_id,track) DO UPDATE SET "
                        "size=excluded.size, dialogue_p=excluded.dialogue_p, kind=excluded.kind, "
                        "why=excluded.why, at=excluded.at, ex=excluded.ex",
                        (t["file_id"], t["track"], t["size"], p, kind,
                         (w.group(1).strip().splitlines()[0] if w else "")[:200], time.time(),
                         _tally_sig(t.get("tally") or {})))
    print(json.dumps({"tracks_judged": len(answers)}), flush=True)


_ONLY: list = []
_DRY = False

if __name__ == "__main__":

    # HIDE EVERY GRANDCHILD'S CONSOLE. This child is started hidden, but what it
    # starts (ffmpeg, ffprobe) gets a console of its own unless told otherwise -
    # the console watcher caught ffmpeg windows flashing from the Subtitle maker
    # and the Qwen jobs. Same patch as whisper_worker.py and paddle_worker.py.
    try:
        import subprocess as _sp
        if os.name == "nt":
            _orig_popen = _sp.Popen.__init__

            def _hidden_popen(self, *a, **kw):
                kw["creationflags"] = (kw.get("creationflags") or 0) | 0x08000000
                if not kw.get("startupinfo"):
                    si = _sp.STARTUPINFO()
                    si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0                       # SW_HIDE
                    kw["startupinfo"] = si
                return _orig_popen(self, *a, **kw)
            _sp.Popen.__init__ = _hidden_popen               # type: ignore[method-assign]
    except Exception:                                        # noqa: BLE001
        pass
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--ids", default="", help="judge exactly these file ids (testing)")
    ap.add_argument("--tracks", default="", help="a JSON file of text tracks to judge as well")
    ap.add_argument("--dry", action="store_true", help="print verdicts, write nothing (measuring)")
    ap.add_argument("--no-learn", action="store_true", help="judge without the show's answers (measuring)")
    a = ap.parse_args()
    _ONLY = [int(x) for x in a.ids.split(",") if x.strip()]
    _DRY = a.dry
    if a.no_learn:
        EXAMPLES = 0
    try:
        _judge(a.batch)
        if a.tracks:
            _judge_tracks(a.tracks, 0)
    except Exception as e:                                       # noqa: BLE001
        print(json.dumps({"judged": 0, "error": f"{type(e).__name__}: {e}"[:300]}), flush=True)
