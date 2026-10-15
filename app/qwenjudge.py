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
# ONE NUMBER, NOT A VERDICT AND A CONFIDENCE. "VERDICT: NOT DIALOGUE; SURE: 0"
# came back meaning "0% dialogue", which read as a sure "not" became 100%.
STATE: dict = {"running": False, "last_run": 0.0, "next_run": 0.0, "last_result": "",
               "judged": 0, "error": "", "done": 0, "total": 0, "file": "", "started": 0.0}
_PROC = None


# ----------------------------------------------------------------- server --
def pending(limit: int = BATCH) -> list:
    from .db import cursor
    with cursor() as cur:
        return [int(r["file_id"]) for r in cur.execute(
            "SELECT h.file_id FROM hardsub h LEFT JOIN hardsub_qwen q ON q.file_id = h.file_id "
            " WHERE COALESCE(h.lines,'') NOT IN ('', '[]') "
            "   AND (q.file_id IS NULL OR q.at < h.at) "
            " ORDER BY h.at DESC LIMIT ?", (int(limit),))]


async def run() -> str:
    from . import heavy, joblog
    from .config import NO_WINDOW
    todo = pending()
    if not todo:
        return "nothing to judge"
    got, holder = heavy.claim(LANE, ttl=1800)
    if not got:
        return f"waiting - {holder} has the GPU lane"
    global _PROC
    STATE.update(running=True, error="", done=0, total=min(len(todo), BATCH), file="",
                 started=time.time())
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.qwenjudge", "--batch", str(BATCH), cwd=root,
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
    ids = list(_ONLY) or pending(limit)
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
        prompts = []
        for r in part:
            try:
                L = json.loads(r["lines"] or "[]")
            except Exception:                                    # noqa: BLE001
                L = []
            body = "\n".join(f"{x['t']}s: {x['text']}" for x in L)[:3000]
            prompts.append(tok.encode(
                f"<|im_start|>system\n{SYS}<|im_end|>\n<|im_start|>user\nFile: "
                f"{os.path.basename(r['path'] or '')}\n{len(L)} of {r['samples'] or len(L)} sampled "
                f"frames had text:\n{body}<|im_end|>\n<|im_start|>assistant\n").tokens)
        res = g.generate_batch(prompts, max_length=80, sampling_topk=1,
                               include_prompt_in_result=False, end_token="<|im_end|>")
        with cursor() as cur:
            for r, out in zip(part, res):
                t = tok.decode(out.sequences_ids[0]).strip()
                c = re.search(r"CHANCE:\s*(\d{1,3})", t, re.I)
                w = re.search(r"WHY:\s*(.+)", t, re.I | re.S)
                if not c:
                    continue
                p = max(0, min(100, int(c.group(1))))
                verdict = "dialogue" if p >= 50 else "not dialogue"
                cur.execute(
                    "INSERT INTO hardsub_qwen(file_id,path,size,verdict,dialogue_p,why,at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, "
                    "size=excluded.size, verdict=excluded.verdict, dialogue_p=excluded.dialogue_p, "
                    "why=excluded.why, at=excluded.at",
                    (int(r["file_id"]), r["path"], r["size"], verdict, p,
                     (w.group(1).strip().splitlines()[0] if w else "")[:200], time.time()))
                done += 1
    print(json.dumps({"judged": done}), flush=True)


_ONLY: list = []

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--ids", default="", help="judge exactly these file ids (testing)")
    a = ap.parse_args()
    _ONLY = [int(x) for x in a.ids.split(",") if x.strip()]
    try:
        _judge(a.batch)
    except Exception as e:                                       # noqa: BLE001
        print(json.dumps({"judged": 0, "error": f"{type(e).__name__}: {e}"[:300]}), flush=True)
