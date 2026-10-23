r"""nuarr - the second opinion: three engines settle what one could not.

WHY. The subtitle board (Subtitle User Input) lists every file the reading
is unsure about - "72% sure this picture carries burned-in dialogue", "58%
sure this track is signs" - and waits for a person. Each of those scores came
from ONE kind of evidence: twenty-four OCR'd frames, or the shape of a text
track. Nobody checked the one thing that settles it: does the text on the
screen match what is being SAID?

WHAT THIS DOES. For each unsure file it asks all three engines and compares:
  OCR / shape   what the board already read (the frames, the track's shape)
  Whisper       listens at the moments those lines appear and writes what is
                said there in English - if the screen says what the voices
                say, it is dialogue; if people talk and the screen says
                something else, it is a sign, a title, a credit
  Qwen          the judge's reading of the lines (hardsub_qwen /
                subtitle_qwen), taught by every answer on the same show
Each engine votes; the votes are weighted by how often that engine agreed
with the final verdict before (learnt, see engine_stats); agreement is the
confidence. At or above the sure line the file is ANSWERED - through the
same set_kind / record_answer paths a person uses, so every existing learner
(the Qwen judge's examples, the shape memory, the raw rules' hit rates) is
taught by it - and in auto mode the fix is carried out (the marker track, the
retitle). Below the line the evidence is attached to the row for a person.

For a raw ("nothing you can read") Whisper's language head answers the other
question: is English in fact spoken? A dub tagged as Japanese is not a raw.

It also LEARNS THE SHOW'S STYLE from every text track it reads - font,
size, placement, which style names are signs - so the Subtitle maker can match
what the show already looks like (sublearn.learn_style).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time

from .db import cursor, kv_get, kv_set
from . import joblog

LANE = "Second opinion"
KV_MODE = "verify.mode"            # off | advise | auto
KV_SURE = "verify.sure_at"         # answer at or above this confidence
KV_PER_HOUR = "verify.per_hour"
KV_ENGINES = "verify.engines"      # {engine: {agree, total}}
DEFAULT_SURE = 90
DEFAULT_PER_HOUR = 6
TICK_S = 300.0
LINES_MAX = 6                      # moments listened to per file
MATCH_AT = 0.34                    # a line and the speech there: this similar = the same words
BASE_WEIGHT = {"whisper": 0.9, "qwen": 0.75, "rules": 0.6}
_READY = False
_LOCK = threading.Lock()
STATE: dict = {"running": False, "file_id": 0, "label": "", "stage": "", "pct": 0.0, "started": 0.0,
               "last_run": 0.0, "last_result": "", "next_run": 0.0, "hour_count": 0, "hour_start": 0.0}
_STOP = {"flag": False}


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS verify_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, file_id INTEGER NOT NULL,
            source TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
            question TEXT NOT NULL DEFAULT '', was_sure INTEGER NOT NULL DEFAULT 0,
            evidence TEXT NOT NULL DEFAULT '{}', verdict TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
            confidence INTEGER NOT NULL DEFAULT 0, action TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
            secs REAL NOT NULL DEFAULT 0)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_verify_file ON verify_log(file_id, source)")
    _READY = True


# ---------------------------------------------------------- settings -----
def mode() -> str:
    m = str(kv_get(KV_MODE) or "auto")
    return m if m in ("off", "advise", "auto") else "auto"


def set_mode(m: str) -> str:
    m = m if m in ("off", "advise", "auto") else "auto"
    kv_set(KV_MODE, m)
    return m


def sure_at() -> int:
    try:
        return max(50, min(100, int(kv_get(KV_SURE) or DEFAULT_SURE)))
    except Exception:                                            # noqa: BLE001
        return DEFAULT_SURE


def per_hour() -> int:
    try:
        return max(1, min(60, int(kv_get(KV_PER_HOUR) or DEFAULT_PER_HOUR)))
    except Exception:                                            # noqa: BLE001
        return DEFAULT_PER_HOUR


def set_budget(sure: int | None = None, hourly: int | None = None) -> dict:
    if sure is not None:
        kv_set(KV_SURE, str(max(50, min(100, int(sure)))))
    if hourly is not None:
        kv_set(KV_PER_HOUR, str(max(1, min(60, int(hourly)))))
    return {"sure_at": sure_at(), "per_hour": per_hour()}


def engine_stats() -> dict:
    try:
        d = json.loads(kv_get(KV_ENGINES) or "{}")
    except Exception:                                            # noqa: BLE001
        d = {}
    out = {}
    for e, base in BASE_WEIGHT.items():
        s = d.get(e) or {}
        n, a = int(s.get("total") or 0), int(s.get("agree") or 0)
        # THE WEIGHT IS LEARNT: with ten verdicts behind it an engine is
        # trusted as often as it was right; before that, its starting weight
        rate = a / n if n else base
        w = base if n < 10 else max(0.3, min(0.98, rate))
        out[e] = {"agree": a, "total": n, "rate": round(rate, 3), "weight": round(w, 3)}
    return out


def _note_engines(votes: dict, verdict: str) -> None:
    try:
        d = json.loads(kv_get(KV_ENGINES) or "{}")
    except Exception:                                            # noqa: BLE001
        d = {}
    for e, v in votes.items():
        if v in ("", "undecided", None):
            continue
        s = d.setdefault(e, {"agree": 0, "total": 0})
        s["total"] += 1
        s["agree"] += int(v == verdict)
    kv_set(KV_ENGINES, json.dumps(d))


# ------------------------------------------------------------ picking -----
def _label(fid: int, label: str, path: str) -> str:
    """The row's name - never "(untitled)". A file read in its first minutes,
    before the arr's title reached it, came with that placeholder; by now the
    title is usually there, and the filename always is."""
    if label and not label.startswith("(untitled)"):
        return label
    try:
        from .db import display_label
        with cursor() as cur:
            r = cur.execute("SELECT title, season, episode, path FROM files WHERE id=?", (int(fid),)).fetchone()
        if r and (r["title"] or "").strip():
            return display_label(r["title"], r["season"], r["episode"], r["path"])
    except Exception:                                            # noqa: BLE001
        pass
    return os.path.splitext(os.path.basename(path))[0][:90] or label or "(untitled)"


def candidates(limit: int = 50) -> list:
    """The board's unsure rows, least sure first, not yet verified for this reading."""
    from . import subkind
    init()
    try:
        f = subkind.findings(limit=800, want_done=False, want_unread=False)
    except Exception:                                            # noqa: BLE001
        return []
    rows = [r for r in (f.get("rows") or []) if r.get("action") and not r.get("done")
            and (r.get("auto") == "ask" or r.get("source") == subkind.RAW)]
    with cursor() as cur:
        seen = {(int(r["file_id"]), r["source"]): float(r["at"]) for r in cur.execute(
            "SELECT file_id, source, MAX(at) at FROM verify_log GROUP BY file_id, source")}
    out = []
    for r in rows:
        k = (int(r["file_id"]), r["source"])
        if k in seen and time.time() - seen[k] < 14 * 86400:
            continue
        out.append(r)
    mid = (subkind.dismiss_at() + subkind.mark_at()) / 2.0
    out.sort(key=lambda r: abs(int(r.get("sure") or 50) - mid))
    return out[:limit]


# ----------------------------------------------------------- helpers -----
def _norm_words(t: str) -> list:
    t = re.sub(r"\{[^}]*\}|<[^>]+>", " ", str(t or ""))
    return [w for w in re.findall(r"[a-z0-9']+", t.lower()) if len(w) > 1]


_STOP_WORDS = set("the a an and or but of to in on at is it i you he she we they are was were be been this that "
                  "for with as by from not no yes so do did does have has had will would can could my your his her "
                  "our their me him them us what who why how when where which there here just".split())


def _match(a: str, b: str) -> float:
    """How much two lines share - content words, order-free, plus a ratio."""
    wa, wb = _norm_words(a), _norm_words(b)
    if len(wa) < 2 or len(wb) < 2:
        return 0.0
    ca = [w for w in wa if w not in _STOP_WORDS] or wa
    cb = [w for w in wb if w not in _STOP_WORDS] or wb
    inter = len(set(ca) & set(cb))
    overlap = inter / max(2, min(len(set(ca)), len(set(cb))))
    ratio = difflib.SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio()
    return max(overlap, ratio)


def _sentence_like(t: str) -> bool:
    w = _norm_words(t)
    return len(w) >= 3 and sum(1 for x in w if x.isalpha()) >= 3


def _audio_lang(file_id: int) -> str:
    with cursor() as cur:
        r = cur.execute("SELECT audio_langs FROM files WHERE id=?", (int(file_id),)).fetchone()
    s = str((r["audio_langs"] if r else "") or "")
    m = re.search(r"[a-z]{3}", s.lower())
    return m.group(0) if m else "und"


def _whisper(path: str, windows: list, lang3: str, tick=None) -> list:
    """[{t, text, speech, lang}] - the worker listens at each moment."""
    from .config import DATA_DIR, NO_WINDOW
    from .jobs import _ffmpeg_exe
    spec = os.path.join(str(DATA_DIR), "verify.windows.json")
    with open(spec, "w", encoding="utf-8") as f:
        json.dump({"windows": windows}, f)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-m", "app.submake_worker", "--src", path, "--out", spec + ".out", "--lang", lang3,
           "--data", str(DATA_DIR), "--ffmpeg", _ffmpeg_exe(), "--verify", spec]
    p = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    heard, err = [], ""
    for ln in p.stdout:
        try:
            d = json.loads(ln)
        except Exception:                                        # noqa: BLE001
            continue
        if "stage" in d and tick:
            tick(d["stage"], float(d.get("pct") or 0))
        if "heard" in d:
            heard = d["heard"]
        if "error" in d:
            err = d["error"]
    p.wait()
    if err:
        raise RuntimeError(err)
    return heard


def _extract_track(path: str, ordinal: int, fmt: str = "srt") -> str:
    from .jobs import _ffmpeg_exe
    from .config import NO_WINDOW
    r = subprocess.run([_ffmpeg_exe(), "-v", "error", "-i", path, "-map", f"0:s:{int(ordinal)}", "-f", fmt, "-"],
                       capture_output=True, timeout=300, creationflags=NO_WINDOW)
    return r.stdout.decode("utf-8", "replace")


def _srt_cues(text: str) -> list:
    out = []
    for blk in re.split(r"\r?\n\s*\r?\n", (text or "").strip()):
        m = re.search(r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)", blk)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = blk.split("\n", 2)[2] if blk.count("\n") >= 2 else ""
        body = re.sub(r"\{[^}]*\}|<[^>]+>", "", body).replace("\n", " ").strip()
        if body:
            out.append((a, b, body))
    return out


def _spread(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def _combine(votes: dict, strength: dict | None = None) -> tuple:
    """-> (verdict, confidence 0-100). Votes are 'dialogue' / 'not' / 'undecided';
    strength (0-1) says how one-sided an engine's own evidence was."""
    st = engine_stats()
    strength = strength or {}
    tally: dict = {}
    total = 0.0
    for e, v in votes.items():
        if v in ("dialogue", "not"):
            w = st.get(e, {}).get("weight", BASE_WEIGHT.get(e, 0.5)) * (0.5 + 0.5 * float(strength.get(e, 1.0)))
            tally[v] = tally.get(v, 0.0) + w
            total += w
    if not tally or total <= 0:
        return "", 0
    best = max(tally, key=tally.get)
    voters = sum(1 for v in votes.values() if v in ("dialogue", "not"))
    conf = tally[best] / total
    # ONE VOICE IS NOT A VERDICT - unless it is Whisper and the lines matched
    # (or missed) almost every time: six of six is a verdict on its own. Any
    # other lone engine caps at 80.
    if voters < 2:
        lone = next(e for e, v in votes.items() if v in ("dialogue", "not"))
        cap = 0.92 if (lone == "whisper" and float(strength.get("whisper", 0)) >= 0.83) else 0.80
        conf = min(conf, cap)
    return best, int(round(100 * conf))


# ------------------------------------------------------------ the work ----
def verify_one(row: dict, act: bool | None = None, dry: bool = False) -> dict:
    """One board row -> evidence from every engine, a verdict, and what was done.
    dry: gather and judge, answer nothing, teach nothing - for checking the method."""
    from . import subkind, hardsub, subtitletitle as stt, sublearn
    init()
    fid, source = int(row["file_id"]), str(row.get("source") or "")
    path = row.get("path") or ""
    label = _label(fid, row.get("label") or "", path)
    t0 = time.time()
    act = mode() == "auto" if act is None else act
    STATE.update(running=True, file_id=fid, label=label, stage="starting", pct=0.0, started=t0)

    def tick(stage, pct=0.0):
        STATE.update(stage=stage, pct=float(pct))

    ev: dict = {"rules": {"sure": int(row.get("sure") or 0), "why": (row.get("why") or "")[:300]}}
    votes: dict = {}
    strength: dict = {}
    verdict, kind, conf, action, note, question = "", "", 0, "", "", ""
    try:
        if source == subkind.PICTURE:
            question = "burned-in dialogue in the picture?"
            lines = [l for l in (row.get("lines") or []) if _sentence_like(l.get("text", ""))]
            if len(lines) < 3:
                tick("reading more frames", 5)
                hardsub.probe_one(fid, samples=48, on=lambda t, p: tick(t, 5 + p * 0.3))
                with cursor() as cur:
                    r = cur.execute("SELECT lines FROM hardsub WHERE file_id=?", (fid,)).fetchone()
                lines = [l for l in json.loads((r["lines"] if r else "") or "[]") if _sentence_like(l.get("text", ""))]
            lines = _spread(sorted(lines, key=lambda l: l["t"]), LINES_MAX)
            ev["ocr"] = {"lines": len(lines), "sample": [l["text"][:60] for l in lines[:3]]}
            if len(lines) >= 3:
                heard = _whisper(path, [{"t": l["t"], "before": 5, "len": 14} for l in lines], _audio_lang(fid),
                                 tick=lambda s, p: tick(s, 35 + p * 0.5))
                pairs = [(l["text"], h["text"], _match(l["text"], h["text"]), h.get("speech")) for l, h in zip(lines, heard)]
                hits = sum(1 for p in pairs if p[2] >= MATCH_AT)
                spoken = sum(1 for p in pairs if p[3])
                ev["whisper"] = {"compared": len(pairs), "matched": hits, "spoken": spoken,
                                 "pairs": [{"screen": a[:70], "heard": b[:70], "match": round(m, 2)} for a, b, m, _s in pairs]}
                votes["whisper"] = ("dialogue" if hits / len(pairs) >= 0.5 else
                                    "not" if hits / len(pairs) <= 0.17 and spoken >= max(2, len(pairs) // 2) else "undecided")
                strength["whisper"] = (hits / len(pairs)) if votes["whisper"] == "dialogue" else (1 - hits / len(pairs))
            else:
                # THE TEXT IS TOO GARBLED TO MATCH (old SDTV anime: EyeShield 21,
                # Fighting Spirit) but the MOMENTS are still known. Burned-in
                # dialogue sits where people speak; a title card or a credit
                # does not need a voice under it. Listen at the moments text
                # was seen and vote on whether speech is there - a weaker vote.
                with cursor() as cur:
                    r = cur.execute("SELECT lines FROM hardsub WHERE file_id=?", (fid,)).fetchone()
                allp = [l for l in json.loads((r["lines"] if r else "") or "[]") if l.get("t")]
                allp = _spread(sorted(allp, key=lambda l: l["t"]), LINES_MAX)
                if len(allp) >= 3:
                    heard = _whisper(path, [{"t": l["t"], "before": 3, "len": 9} for l in allp], _audio_lang(fid),
                                     tick=lambda s, p: tick(s, 35 + p * 0.5))
                    spoken = sum(1 for h in heard if h.get("speech") and len(_norm_words(h.get("text", ""))) >= 3)
                    pairs = [(l["text"], h.get("text", ""), _match(l["text"], h.get("text", ""))) for l, h in zip(allp, heard)]
                    hits = sum(1 for p in pairs if p[2] >= MATCH_AT)
                    share = spoken / len(heard)
                    ev["whisper"] = {"compared": len(pairs), "matched": hits, "listened": len(heard), "spoken": spoken,
                                     "note": f"the lines were barely readable; {hits} still match what is said, and speech was "
                                             f"heard at {spoken} of {len(heard)} moments text was on screen",
                                     "pairs": [{"screen": a[:70], "heard": b[:70], "match": round(m, 2)} for a, b, m in pairs]}
                    # WORDS THAT MATCH ARE THE REAL VOTE; coincidence is the weaker one
                    if hits / len(pairs) >= 0.5:
                        votes["whisper"], strength["whisper"] = "dialogue", hits / len(pairs)
                    else:
                        votes["whisper"] = "dialogue" if share >= 0.8 else "not" if share <= 0.34 else "undecided"
                        strength["whisper"] = 0.5
                else:
                    ev["whisper"] = {"compared": 0, "note": "too few lines to listen at"}
            q = hardsub.qwen_of({"file_id": fid, "path": path})
            if q:
                ev["qwen"] = {"p": q["p"], "why": (q.get("why") or "")[:160]}
                votes["qwen"] = "dialogue" if q["p"] >= 70 else "not" if q["p"] <= 30 else "undecided"
            s = ev["rules"]["sure"]
            votes["rules"] = "dialogue" if s >= subkind.mark_at() else "not" if s <= subkind.dismiss_at() else "undecided"
            verdict, conf = _combine(votes, strength)
            if verdict == "dialogue":
                kind = hardsub.DIALOGUE
            elif verdict == "not":
                with cursor() as cur:
                    r = cur.execute("SELECT low_hits, samples FROM hardsub WHERE file_id=?", (fid,)).fetchone()
                frac = (int(r["low_hits"] or 0) / max(1, int(r["samples"] or 1))) if r else 0
                kind = hardsub.SIGNS if frac >= 0.15 else hardsub.NONE
        elif source.startswith("track:"):
            track = int(row.get("track") or 0)
            question = "is this text track dialogue, or signs & songs?"
            tick("reading the track", 5)
            # THE BOARD COUNTS SUBTITLE TRACKS FROM 1 (mkvpropedit s1, s2...); ffmpeg from 0
            srt = _extract_track(path, track - 1, "srt")
            cues = [c for c in _srt_cues(srt) if _sentence_like(c[2])]
            dur = cues[-1][1] if cues else 0
            mid = [c for c in cues if dur and 0.1 * dur <= c[0] <= 0.9 * dur] or cues
            pick = _spread(mid, LINES_MAX)
            ev["shape"] = {"cues": len(cues), "sentences": len(mid), "kind_read": row.get("kind") or ""}
            try:
                ass = _extract_track(path, track - 1, "ass")
                st_ = sublearn.learn_style(sublearn.show_of(path), ass, path)
                if st_:
                    ev["style"] = st_
            except Exception:                                    # noqa: BLE001
                pass
            if len(pick) >= 3:
                heard = _whisper(path, [{"t": c[0], "before": 1.5, "len": max(8.0, c[1] - c[0] + 5)} for c in pick],
                                 _audio_lang(fid), tick=lambda s, p: tick(s, 25 + p * 0.6))
                pairs = [(c[2], h["text"], _match(c[2], h["text"]), h.get("speech")) for c, h in zip(pick, heard)]
                hits = sum(1 for p in pairs if p[2] >= MATCH_AT)
                spoken = sum(1 for p in pairs if p[3])
                ev["whisper"] = {"compared": len(pairs), "matched": hits, "spoken": spoken,
                                 "pairs": [{"screen": a[:70], "heard": b[:70], "match": round(m, 2)} for a, b, m, _s in pairs]}
                votes["whisper"] = ("dialogue" if hits / len(pairs) >= 0.5 else
                                    "not" if hits / len(pairs) <= 0.17 and spoken >= max(2, len(pairs) // 2) else "undecided")
                strength["whisper"] = (hits / len(pairs)) if votes["whisper"] == "dialogue" else (1 - hits / len(pairs))
            else:
                if not cues:
                    ev["whisper"] = {"compared": 0, "note": "the track could not be read - the file has changed since the board read it"}
                    note = "stale row"
                else:
                    ev["whisper"] = {"compared": 0, "note": f"only {len(pick)} sentence-like cue(s) among {len(cues)} - a sign sheet"}
                    if len(mid) <= 2:
                        votes["whisper"] = "not"
                        strength["whisper"] = 1.0
            try:
                from . import qwenjudge
                q = qwenjudge.track_verdicts().get((fid, track))
            except Exception:                                    # noqa: BLE001
                q = None
            if q:
                ev["qwen"] = {"p": q["p"], "why": (q.get("why") or "")[:160]}
                votes["qwen"] = "dialogue" if q["p"] >= 70 else "not" if q["p"] <= 30 else "undecided"
            k = (row.get("kind") or "").lower()
            s = ev["rules"]["sure"]
            votes["rules"] = (("dialogue" if k in ("dialogue", "hybrid") else "not") if s >= stt.sure_at()
                              else "undecided")
            verdict, conf = _combine(votes, strength)
            if verdict == "dialogue":
                kind = stt.HYBRID if (k == "hybrid" or re.search(r"\bpositioned\b|\bsigns? beside\b", row.get("why") or "")) else stt.DIALOGUE
            elif verdict == "not":
                kind = stt.SIGNS
        elif source == subkind.RAW:
            question = "is English spoken (a mis-tagged dub), or is it a raw?"
            from . import audiolang
            found = []
            with cursor() as cur:
                r = cur.execute("SELECT audio_langs FROM files WHERE id=?", (fid,)).fetchone()
            n_tracks = max(1, len([x for x in re.split(r"[,\s]+", str((r["audio_langs"] if r else "") or "")) if x]))
            for i in range(min(n_tracks, 4)):
                tick(f"listening to audio track {i + 1}", 10 + 70 * i / max(1, n_tracks))
                d = audiolang.detect(path, track=i, file_id=fid)
                found.append({"track": i, "code": d.get("code") or "", "confidence": round(float(d.get("confidence") or 0), 2)})
            ev["whisper"] = {"audio": found}
            eng = [f for f in found if f["code"] == "eng" and f["confidence"] >= 0.75]
            if eng:
                verdict, kind, conf = "english", "english audio", int(round(100 * max(f["confidence"] for f in eng)))
                votes["whisper"] = "english"
            elif found and all(f["code"] and f["code"] != "eng" and f["confidence"] >= 0.75 for f in found):
                verdict, kind, conf = "raw", "raw", int(round(100 * min(f["confidence"] for f in found)))
                votes["whisper"] = "raw"
        else:
            note = f"nothing to check for {source}"
        # ---------------------------------------------------- the answer --
        sure = sure_at()
        if dry:
            action = f"dry run: would {'answer' if verdict and conf >= sure else 'leave it'}"
        elif verdict and conf >= sure:
            if source == subkind.PICTURE:
                subkind.set_kind(fid, source, kind)
                action = f"answered: {hardsub.KIND_WORDS.get(kind, kind)}"
                if act and kind in (hardsub.DIALOGUE, hardsub.HYBRID):
                    r = subkind.act(fid, source, kind)
                    action += " · marked" if r.get("ok") else f" · mark failed: {r.get('why', '')[:60]}"
            elif source.startswith("track:"):
                subkind.set_kind(fid, source, kind)
                action = f"answered: {stt.KIND_WORDS.get(kind, kind)}"
                if act:
                    r = subkind.act(fid, source, kind)
                    action += (" · retitled" if r.get("ok") and not r.get("gone") else
                               " · title already right" if r.get("gone") else f" · retitle failed: {r.get('why', '')[:60]}")
            elif source == subkind.RAW:
                from . import subneed
                if verdict == "english":
                    subneed.record_answer(fid, "not_raw", "second opinion", str(row.get("rule") or ""), path=path)
                    action = "answered: English is spoken - not a raw"
                    if act:
                        try:
                            from . import audqueue
                            audqueue.requeue(fid)
                            action += " · audio language queued to retag"
                        except Exception:                        # noqa: BLE001
                            pass
                else:
                    subneed.record_answer(fid, "raw", "second opinion", str(row.get("rule") or ""), path=path)
                    action = "answered: a raw - the Subtitle maker takes it"
                # the answer settles the raw check's row now (see subneed.check_one),
                # so the board drops it and a raw goes on to the Subtitle maker
                try:
                    subneed.check_one(fid)
                except Exception:                                # noqa: BLE001
                    pass
            _note_engines(votes, "english" if verdict == "english" else "raw" if verdict == "raw" else verdict)
        elif verdict:
            action = f"left for you ({conf}% - the line is {sure}%)"
        else:
            action = "left for you (the engines could not agree)"
            note = "undecided"
    except Exception as e:                                       # noqa: BLE001
        action, note = "failed", f"{type(e).__name__}: {e}"[:200]
    secs = round(time.time() - t0, 1)
    ev["votes"] = votes
    with cursor() as cur:
        cur.execute("INSERT INTO verify_log(at, file_id, source, path, label, question, was_sure, evidence, verdict, kind, "
                    "confidence, action, note, secs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), fid, source, path, label, question, int(row.get("sure") or 0), json.dumps(ev)[:6000],
                     verdict, kind, int(conf), action, note, secs))
        cur.execute("DELETE FROM verify_log WHERE id < (SELECT MAX(id) - 2000 FROM verify_log)")
    STATE.update(running=False, file_id=0, stage="", pct=0.0, last_run=time.time(),
                 last_result=f"{label}: {action}")
    joblog.log(f"second opinion: {label} - {question} -> {kind or verdict or 'undecided'} ({conf}%) - {action}",
               "ok" if action.startswith("answered") else "info")
    return {"file_id": fid, "source": source, "verdict": verdict, "kind": kind, "confidence": conf,
            "action": action, "note": note, "evidence": ev}


def run_batch(n: int) -> dict:
    from . import heavy
    if not _LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        got, holder = heavy.claim(LANE, ttl=1800)
        if not got:
            return {"skipped": f"{holder} has the GPU lane"}
        from . import gpubudget as _gb
        _ok, _why = _gb.make_room(_gb.COST_GB["whisper"], "verify", 300)
        if not _ok:
            heavy.release(LANE)
            return {"skipped": f"GPU memory - {_why}"}
        try:
            done = []
            for r in candidates(n):
                if _STOP["flag"]:
                    break
                done.append(verify_one(r))
            return {"done": len(done), "answered": sum(1 for d in done if d["action"].startswith("answered")),
                    "results": done}
        finally:
            heavy.release(LANE)
    finally:
        _STOP["flag"] = False
        _LOCK.release()


def stop() -> bool:
    _STOP["flag"] = True
    return True


def paused() -> bool:
    """The Concurrency page's switch - separate from the mode, which says what
    an answer may do; this only says whether it runs now."""
    return str(kv_get("verify.paused") or "0") == "1"


def set_paused(on: bool) -> None:
    kv_set("verify.paused", "1" if on else "0")


async def watch() -> None:
    import asyncio
    await asyncio.sleep(600)
    while True:
        STATE["next_run"] = time.time() + TICK_S
        try:
            if mode() != "off" and not paused():
                now = time.time()
                if now - STATE["hour_start"] > 3600:
                    STATE.update(hour_start=now, hour_count=0)
                room = per_hour() - STATE["hour_count"]
                if room > 0:
                    n = max(1, min(room, per_hour() * TICK_S / 3600 + 0.999).__int__())
                    r = await asyncio.to_thread(run_batch, n)
                    STATE["hour_count"] += int(r.get("done") or 0)
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"second opinion: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(TICK_S)


def as_running() -> list:
    if not STATE.get("running"):
        return []
    el = time.time() - float(STATE.get("started") or time.time())
    return [{"job_id": f"verify-{STATE['file_id']}", "pool": "verify", "kind": "verify", "title": STATE["label"],
             "file": STATE["label"], "stage": STATE["stage"], "progress": STATE["pct"], "elapsed_s": round(el),
             "eta_s": None, "eta_est": True, "plan": "the second opinion: Whisper listens where the lines are, "
             "and the three engines' votes decide", "why_pool": "GPU - the heavy lane"}]


def snapshot() -> dict:
    init()
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute("SELECT * FROM verify_log ORDER BY id DESC LIMIT 40")]
        tot = cur.execute("SELECT COUNT(*) n, SUM(CASE WHEN action LIKE 'answered%' THEN 1 ELSE 0 END) a, "
                          "SUM(CASE WHEN action LIKE 'left%' THEN 1 ELSE 0 END) l FROM verify_log").fetchone()
    for r in rows:
        if (r.get("label") or "").startswith("(untitled)") or not r.get("label"):
            r["label"] = _label(r["file_id"], "", r["path"])
        try:
            r["evidence"] = json.loads(r["evidence"] or "{}")
        except Exception:                                        # noqa: BLE001
            r["evidence"] = {}
    try:
        waiting = len(candidates(400))
    except Exception:                                            # noqa: BLE001
        waiting = -1
    # SPEED AND ETA, measured: seconds per file over the last twenty, files
    # in the last hour, and what is left at that pace - but never faster than
    # the hourly budget allows.
    with cursor() as cur:
        last = [float(r["secs"] or 0) for r in cur.execute(
            "SELECT secs FROM verify_log WHERE secs > 0 ORDER BY id DESC LIMIT 20")]
        hour_n = cur.execute("SELECT COUNT(*) n FROM verify_log WHERE at > ?", (time.time() - 3600,)).fetchone()["n"]
    avg = (sum(last) / len(last)) if last else 0.0
    eta = 0.0
    if waiting > 0:
        by_pace = waiting * (avg or 30.0)
        by_budget = waiting / max(1, per_hour()) * 3600 if mode() != "off" else 0.0
        eta = max(by_pace, by_budget)
    el = (time.time() - float(STATE["started"] or time.time())) if STATE.get("running") else 0.0
    return {"mode": mode(), "sure_at": sure_at(), "per_hour": per_hour(), "engines": engine_stats(),
            "recent": rows, "total": int(tot["n"] or 0), "answered": int(tot["a"] or 0), "left": int(tot["l"] or 0),
            "waiting": waiting, "avg_secs": round(avg, 1), "hour_n": int(hour_n or 0), "eta_s": round(eta),
            "elapsed_s": round(el), "file_eta_s": (round(max(0.0, avg - el)) if avg and STATE.get("running") else None),
            **{k: STATE[k] for k in ("running", "label", "stage", "pct", "last_run", "last_result", "next_run")}}
