"""How a made subtitle reads - measured from the text alone, the moment it is made.

Not "is the translation right": nothing here can know that without a second
subtitle to hold it against. These are the faults a made track shows on its
face, the ones Whisper and a translator are known for:

  repeats      the same line said twice in a row - Whisper looping on music
  untranslated a line that still carries the source language's script
  invented     the phrases Whisper makes up over silence ("thanks for watching")
  too fast     more than 25 characters a second - cannot be read in the time
  long         over 90 characters or 7 s on screen - a paragraph, not a subtitle
  flashes      under 0.7 s on screen

A file that is nearly silent (under three lines a minute) is noted, not
faulted: Primal and Shaun the Sheep have nothing to subtitle.

score() is pure. record() keeps the last thousand scores for the Subtitles
page and /api/submake/quality.
"""
from __future__ import annotations

import json
import re
import time

from .db import kv_get, kv_set

KV = "submake.quality"
KEEP = 1000
_BLOCK = re.compile(r"\r?\n\s*\r?\n")
_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_TAGS = re.compile(r"\{[^}]*\}|<[^>]+>")
_FOREIGN = re.compile("[Ѐ-ӿ؀-ۿ฀-๿぀-ヿ一-鿿가-힯]")
_INVENTED = re.compile(r"thank(s| you) for (watching|your view)|translated by|subtitles? by|"
                       r"please subscribe|amara\.org|like and subscribe|"
                       r"see you (in the|on the) next (video|episode)|don'?t forget to (like|subscribe)", re.I)
SIGN = "{" + chr(92) + "an8}"


def cues(srt_text: str) -> list:
    """[(start, end, text, is_sign)]"""
    out = []
    for b in _BLOCK.split((srt_text or "").strip()):
        m = _TIME.search(b)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s = g[0] * 3600 + g[1] * 60 + g[2] + int(str(g[3])[:3].ljust(3, "0")) / 1000
        e = g[4] * 3600 + g[5] * 60 + g[6] + int(str(g[7])[:3].ljust(3, "0")) / 1000
        body = b[m.end():].strip()
        sign = body.startswith(SIGN)
        body = " ".join(_TAGS.sub("", body).split())
        if body:
            out.append((s, e, body, sign))
    return out


def score(srt_text: str, duration: float) -> dict:
    cs = cues(srt_text)
    dia = [c for c in cs if not c[3]]
    n = len(dia)
    mins = max(1.0, float(duration or 0) / 60.0) if duration else max(1.0, (cs[-1][1] / 60.0) if cs else 1.0)
    d = max(1, n)
    m = {
        "lines": n, "signs": len(cs) - n,
        "per_min": round(n / mins, 1),
        "repeat": round(sum(1 for i in range(1, n) if dia[i][2].lower() == dia[i - 1][2].lower()) / d, 3),
        "untranslated": sum(1 for c in cs if _FOREIGN.search(c[2])),
        "invented": sum(1 for c in cs if _INVENTED.search(c[2])),
        "fast": round(sum(1 for s, e, t, _ in dia if len(t) / max(0.2, e - s) > 25) / d, 3),
        "long": round(sum(1 for s, e, t, _ in dia if len(t) > 90 or e - s > 7) / d, 3),
        "flash": round(sum(1 for s, e, t, _ in dia if e - s < 0.7) / d, 3),
    }
    flags = []
    if m["repeat"] > 0.05:
        flags.append(f"{m['repeat'] * 100:.0f} % of lines repeat the line before")
    if m["untranslated"]:
        flags.append(f"{m['untranslated']} line(s) still carry untranslated words")
    if m["invented"]:
        flags.append(f"{m['invented']} line(s) Whisper tends to invent over silence")
    if m["fast"] > 0.15:
        flags.append(f"{m['fast'] * 100:.0f} % of lines are too fast to read")
    if m["long"] > 0.15:
        flags.append(f"{m['long'] * 100:.0f} % of lines are too long")
    if m["flash"] > 0.10:
        flags.append(f"{m['flash'] * 100:.0f} % of lines flash by")
    note = "nearly silent - under three lines a minute" if n and m["per_min"] < 3 else ""
    grade = "good" if not flags else "poor" if (len(flags) >= 2 or m["repeat"] > 0.10) else "check"
    return {"grade": grade, "flags": flags, "note": note, **m}


def record(file_id: int, label: str, q: dict, dub: bool = False) -> None:
    try:
        rows = json.loads(kv_get(KV) or "[]")
    except Exception:                                            # noqa: BLE001
        rows = []
    rows = [r for r in rows if not (r.get("file_id") == int(file_id) and bool(r.get("dub")) == bool(dub))]
    rows.insert(0, {"at": time.time(), "file_id": int(file_id), "label": label, "dub": bool(dub), **q})
    kv_set(KV, json.dumps(rows[:KEEP]))


def backfill() -> int:
    """Score the tracks made before scoring existed, from the text sublearn
    kept of each (the newest thousand). Run once, when nothing is recorded."""
    from .db import cursor, display_label
    rows = []
    with cursor() as cur:
        cur.execute("SELECT o.file_id, o.text, o.made_at, f.duration, f.title, f.season, f.episode, f.path "
                    "FROM learn_orig o JOIN files f ON f.id = o.file_id WHERE o.tool='submake' "
                    "ORDER BY o.made_at DESC LIMIT ?", (KEEP,))
        for r in cur.fetchall():
            try:
                q = score(r["text"] or "", float(r["duration"] or 0))
                rows.append({"at": float(r["made_at"] or 0), "file_id": int(r["file_id"]),
                             "label": display_label(r["title"], r["season"], r["episode"], r["path"]),
                             "dub": False, "old": True, **q})
            except Exception:                                    # noqa: BLE001
                continue
    if rows:
        kv_set(KV, json.dumps(rows))
    return len(rows)


def summary() -> dict:
    try:
        rows = json.loads(kv_get(KV) or "[]")
    except Exception:                                            # noqa: BLE001
        rows = []
    if not rows:
        try:
            if backfill():
                rows = json.loads(kv_get(KV) or "[]")
        except Exception:                                        # noqa: BLE001
            pass
    kinds: dict = {}
    for r in rows:
        for f in r.get("flags") or []:
            k = " ".join(w for w in f.split() if not w[0].isdigit() and w != "%")
            kinds[k] = kinds.get(k, 0) + 1
    by = {"good": 0, "check": 0, "poor": 0}
    for r in rows:
        by[r.get("grade") or "good"] = by.get(r.get("grade") or "good", 0) + 1
    return {"n": len(rows), "by": by, "kinds": sorted(kinds.items(), key=lambda x: -x[1]),
            "silent": sum(1 for r in rows if r.get("note")),
            "flagged": sorted([r for r in rows if r.get("grade") != "good"],
                              key=lambda r: (r.get("grade") != "poor", -(r.get("at") or 0)))[:60]}
