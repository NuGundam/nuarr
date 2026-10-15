r"""nuarr - small Qwen jobs, each in a child process of its own.

    python -m app.qwentools --parse-names     JSON list of names on stdin
    python -m app.qwentools --fix-ocr IN OUT  an OCR'd SRT in, a corrected one out
    python -m app.qwentools --pick-episode    {name, episodes} on stdin

Both print one JSON object per line on stdout. See qwenlib for why Qwen only
ever runs in a child.

PARSE-NAMES. Release names the arrs give up on - "[Hakata Ramen] Pokemon
Movie 09 - Pokmon Ranger and the Temple of the Sea" came back "Unable to
parse release" in Radarr. Qwen reads such a name and says what it is; the
caller (arrqueue) only acts when that matches exactly one film or episode.

FIX-OCR. Subtitles converted from picture tracks carry OCR's character
errors - "l" for "I", "rn" for "m", a dropped apostrophe. Qwen corrects each
line in context, and a correction is kept only when it is SMALL: the same
number of words, each changed word a near-spelling of the one read. A line it
wants to rewrite rather than repair is left exactly as the OCR read it.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys

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


def _say(**k):
    print(json.dumps(k, ensure_ascii=True), flush=True)


def parse_names(names: list) -> list:
    from app import qwenlib
    sysm = ("You read video release names (torrent / usenet style). For each numbered name reply "
            "on the same numbered line with JSON only: {\"kind\": \"movie\" or \"episode\", "
            "\"title\": the show or film title in plain English words as it would appear on TMDB, "
            "\"year\": number or null, \"season\": number or null, \"episode\": number or null}. "
            "For an anime film numbered in a series (\"Pokemon Movie 09\") give the film's own title.")
    got = qwenlib.numbered(sysm, names, batch=10, max_per_line=80)
    out = []
    for n, g in zip(names, got):
        d = {}
        try:
            m = re.search(r"\{.*\}", g or "")
            d = json.loads(m.group(0)) if m else {}
        except Exception:                                        # noqa: BLE001
            d = {}
        out.append({"name": n, **{k: d.get(k) for k in ("kind", "title", "year", "season", "episode")}})
    return out


def pick_episode(name: str, episodes: list) -> dict:
    """Which of these episodes is this file? -> {"id": int|None, "said": str}.
    The caller checks the answer against the name before it acts on it."""
    from app import qwenlib
    sysm = ("You match a video file to its episode. You get the file name and a numbered list of "
            "episodes of the same show (season, episode, title). Specials, OVAs and movies are season "
            "0. Reply exactly: EPISODE: the SxxExx code of the one episode the file clearly is, or "
            "EPISODE: NONE if no episode clearly fits. Then WHY: one short sentence.")
    # CODES, NOT LIST NUMBERS. Asked for a list number, Qwen answered "18"
    # and explained it as "S00E01" - list position and episode number are
    # both small integers and it mixed them up.
    code = {f"S{int(e.get('s') or 0):02d}E{int(e.get('e') or 0):02d}": e for e in episodes}
    listing = "\n".join(f"{k} - {e.get('title') or ''}" for k, e in code.items())
    said = qwenlib.ask(sysm, f"File: {name}\n\nEpisodes:\n{listing}", max_len=60) or ""
    m = re.search(r"EPISODE:\s*S(\d+)\s*E(\d+)", said, re.I)
    e = code.get(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}") if m else None
    return {"id": e["id"] if e else None, "said": said.strip()[:200]}


_SWAP = [set("lI1|!"), set("0Oo"), set("5S"), set("8B")]


def _small(a: str, b: str) -> bool:
    """A REPAIR, NOT A REWRITE. Same number of words, and every word that
    changed is a near-spelling of the one the OCR read ("rnove"/"move",
    "Its"/"It's") or one of OCR's usual one-letter swaps ("l"/"I"). Measured:
    the looser rule - whole line three quarters the same - let "Me want go
    house now fast" become "I want to go home now fast"."""
    if not b or not qwenlib_english(b):
        return False
    wa, wb = a.split(), b.split()
    if len(wa) != len(wb):
        return False
    for x, y in zip(wa, wb):
        if x == y:
            continue
        cx, cy = x.strip(".,!?;:\"'"), y.strip(".,!?;:\"'")
        if len(cx) == len(cy) == 1 and any(cx in g and cy in g for g in _SWAP):
            continue
        if difflib.SequenceMatcher(None, x.lower(), y.lower()).ratio() < 0.6:
            return False
    return True


def qwenlib_english(t):
    from app import qwenlib
    return qwenlib.english(t)


def fix_ocr(src: str, dst: str) -> dict:
    from app import qwenlib
    text = open(src, encoding="utf-8", errors="replace").read()
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues = []
    for b in blocks:
        L = b.splitlines()
        k = next((i for i, x in enumerate(L) if "-->" in x), None)
        if k is None:
            continue
        cues.append((L[:k + 1], L[k + 1:]))
    lines = [" / ".join(x.strip() for x in body) for _h, body in cues]
    sysm = ("These English subtitle lines were read from images by OCR and may contain character "
            "errors (l for I, rn for m, 0 for O, missing apostrophes, joined or split words). For each "
            "numbered line reply with the corrected line on the same number. Fix only OCR errors; do "
            "not rephrase, translate or change meaning. Keep ' / ' where it appears. If a line is "
            "already right, repeat it exactly.")
    got = qwenlib.numbered(sysm, lines, batch=40, max_per_line=60)
    changed, kept_out = [], []
    for (head, body), old, new in zip(cues, lines, got):
        if new and new != old and _small(old, new):
            changed.append({"was": old, "now": new})
            body = [x.strip() for x in new.split(" / ")]
        kept_out.append("\n".join(head + body))
    tmp = dst + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n\n".join(kept_out) + "\n")
    os.replace(tmp, dst)
    return {"lines": len(lines), "changed": len(changed), "examples": changed[:12]}


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = sys.argv[1:]
    try:
        if args[:1] == ["--parse-names"]:
            names = json.loads(sys.stdin.read() or "[]")
            _say(parsed=parse_names(names))
        elif args[:1] == ["--pick-episode"]:
            d = json.loads(sys.stdin.read() or "{}")
            _say(picked=pick_episode(d.get("name") or "", d.get("episodes") or []))
        elif args[:1] == ["--fix-ocr"] and len(args) >= 3:
            _say(fixed=fix_ocr(args[1], args[2]))
        else:
            _say(error="usage: --parse-names | --fix-ocr IN OUT")
    except Exception as e:                                       # noqa: BLE001
        _say(error=f"{type(e).__name__}: {e}"[:300])


if __name__ == "__main__":
    main()
