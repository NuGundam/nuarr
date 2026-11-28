"""Mend the untranslated lines in subtitle tracks the maker has already made.

Whisper, asked to translate, sometimes leaves a word of the source language
inside an English line ("we will now begin the new" + the Japanese word), and
now and then a whole line. New makes send such lines to the translator before
they are written (submake_worker). This does the same for the tracks made
before that: 69 of the first 820.

  1. read the track nuarr made, as it is in the file NOW - so anything you
     changed in it since is kept
  2. gather every line that still carries another script, from every file
  3. one run of the translator for all of them, on the heavy lane
  4. per file: the mended lines go back in their own cues (same times, same
     placement), the track is swapped in one mkvmerge pass by the same road a
     restyle takes, read back, and the file scored again

A line the translator still does not return in English keeps its English
words and loses the foreign ones, if at least three words are left; otherwise
the cue is removed. Nothing else in the track is touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

from .config import DATA_DIR

STATE: dict = {"running": False, "phase": "", "total": 0, "done": 0, "lines": 0, "mended": 0,
               "removed": 0, "failed": 0, "last": [], "error": "", "at": 0.0, "ended": 0.0}
_FOREIGN = re.compile("[Ѐ-ӿ؀-ۿ฀-๿぀-ヿ一-鿿가-힯]")
_FOREIGN_RUN = re.compile("[Ѐ-ӿ؀-ۿ฀-๿぀-ヿ一-鿿가-힯"
                          "　-〿＀-￯]+")
_TAGS = re.compile(r"\{[^}]*\}|<[^>]+>")
SIGN = "{" + chr(92) + "an8}"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def candidates() -> list:
    """Files whose made track was scored with untranslated lines (not dubs)."""
    from . import subquality
    from .db import kv_get
    try:
        rows = json.loads(kv_get(subquality.KV) or "[]")
    except Exception:                                            # noqa: BLE001
        rows = []
    return [r for r in rows if r.get("untranslated") and not r.get("dub")]


def current_text(file_id: int) -> tuple[str, str]:
    """(the made track as SRT, '') or ('', why not). Read from the file."""
    from . import submake, substyle
    from .config import quiet_run
    from .jobs import _ffmpeg_exe
    r = submake._row(int(file_id))
    if not r or not os.path.exists(r["path"]):
        return "", "the file is not on disk"
    if not r["path"].lower().endswith(".mkv"):
        return "", "not an mkv"
    j = submake._mkvj(r["path"])
    subs = [t for t in j.get("tracks", []) if t.get("type") == "subtitles"]
    for i, t in enumerate(subs):
        if (t.get("properties") or {}).get("track_name") != submake.TRACK_NAME:
            continue
        fmt = "srt" if "SubRip" in (t.get("codec") or "") else "ass"
        raw = quiet_run([_ffmpeg_exe(), "-v", "error", "-i", r["path"], "-map", f"0:s:{i}", "-f", fmt, "-"],
                        capture_output=True, timeout=300).stdout.decode("utf-8", "replace")
        text = raw if fmt == "srt" else substyle.ass_to_srt(raw)
        return (text, "") if text.strip() else ("", "the track could not be read")
    return "", "no track named " + submake.TRACK_NAME


def _plain(body: str) -> str:
    return " ".join(_TAGS.sub("", body).split())


def _salvage(text: str) -> str:
    """The English of a mixed line, when enough of it is left to be a line."""
    t = " ".join(_FOREIGN_RUN.sub(" ", text).split())
    return t if len(re.findall(r"[A-Za-z']{2,}", t)) >= 3 else ""


def _src_name(lang: str) -> str:
    return {"jpn": "Japanese", "ja": "Japanese", "kor": "Korean", "ko": "Korean", "zho": "Chinese",
            "chi": "Chinese", "zh": "Chinese", "rus": "Russian", "ru": "Russian", "tha": "Thai",
            "ara": "Arabic"}.get((lang or "").lower(), "Japanese")


async def run() -> None:
    from . import heavy, joblog, submake, subquality, sublearn, substyle
    if STATE["running"]:
        return
    STATE.update(running=True, phase="reading the tracks", total=0, done=0, lines=0, mended=0,
                 removed=0, failed=0, last=[], error="", at=time.time(), ended=0.0, gone=0)
    lane = "Subtitle line fix"
    held = False
    try:
        cand = candidates()
        STATE["total"] = len(cand)
        files = []                                   # {fid, label, text, cues, hits:[cue index], name}
        for c in cand:
            fid = int(c["file_id"])
            text, why = await asyncio.to_thread(current_text, fid)
            if not text and why.startswith("no track named"):
                # THE FILE HAS BEEN REPLACED SINCE - a better release with its
                # own subtitles (30 of the 69 on 2026-11-27). Nuarr's track
                # went with the old file; there is nothing to mend and the
                # old score is about a track that no longer exists.
                subquality.forget(fid)
                STATE["gone"] = STATE.get("gone", 0) + 1
                STATE["done"] += 1
                continue
            if not text:
                STATE["last"].append({"label": c.get("label"), "ok": False, "why": why})
                STATE["failed"] += 1
                STATE["done"] += 1
                continue
            cues = substyle.srt_cues(text)
            hits = [i for i, (_a, _z, b) in enumerate(cues) if _FOREIGN.search(_plain(b))]
            if not hits:
                # mended already (or by hand): the score follows the file
                try:
                    r = submake._row(fid)
                    subquality.record(fid, c.get("label") or "", subquality.score(text, float(r["duration"] or 0)))
                except Exception:                                # noqa: BLE001
                    pass
                STATE["done"] += 1
                continue
            r = submake._row(fid)
            files.append({"fid": fid, "label": c.get("label") or "", "text": text, "cues": cues, "hits": hits,
                          "name": _src_name(submake._lang_of(r)), "title": r["title"] or ""})
            STATE["lines"] += len(hits)
        if not files:
            STATE["phase"] = "nothing to mend"
            return
        # ONE RUN OF THE TRANSLATOR for every line of every file, grouped by
        # the language they are in. The lane is the maker's own, so this
        # waits for the file being made and the maker waits for this.
        STATE["phase"] = "waiting for the GPU lane"
        t0 = time.time()
        while True:
            got, _holder = heavy.claim(lane, ttl=3600)
            if got:
                held = True
                break
            if time.time() - t0 > 3 * 3600:
                STATE["error"] = "the GPU lane stayed busy for three hours - nothing was changed"
                return
            await asyncio.sleep(20)
        STATE["phase"] = "translating"
        groups: dict = {}
        for f in files:
            g = groups.setdefault(f["name"], {"name": f["name"], "lines": [], "at": []})
            for i in f["hits"]:
                g["lines"].append(_plain(f["cues"][i][2]))
                g["at"].append((f["fid"], i))
        spec = os.path.join(str(DATA_DIR), "subfixlines.json")
        with open(spec, "w", encoding="utf-8") as fh:
            json.dump({"groups": [{"name": g["name"], "lines": g["lines"]} for g in groups.values()]}, fh)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.submake_worker", "--src", "-", "--out", "-", "--data", str(DATA_DIR),
            "--ffmpeg", "-", "--retranslate", spec, cwd=root, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
        out, _ = await p.communicate()
        got_lines = None
        for ln in out.decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(ln)
            except Exception:                                    # noqa: BLE001
                continue
            if "retranslated" in d:
                got_lines = d["retranslated"]
            if d.get("error"):
                STATE["error"] = str(d["error"])[:200]
        heavy.release(lane)
        held = False
        try:
            os.remove(spec)
        except OSError:
            pass
        if got_lines is None:
            STATE["error"] = STATE["error"] or f"the translator stopped (exit {p.returncode}) - nothing was changed"
            return
        answer: dict = {}
        for g, res in zip(groups.values(), got_lines):
            for key, w in zip(g["at"], res):
                answer[key] = w
        # BACK INTO THE FILES, one at a time.
        STATE["phase"] = "putting the mended tracks back"
        for f in files:
            new, mended, removed = [], 0, 0
            for i, (a, z, body) in enumerate(f["cues"]):
                if i not in f["hits"]:
                    new.append((a, z, body))
                    continue
                w = (answer.get((f["fid"], i)) or "").strip()
                if not w or _FOREIGN.search(w):
                    w = _salvage(_plain(body))
                if not w:
                    removed += 1
                    continue
                mended += 1
                new.append((a, z, (SIGN if body.startswith(SIGN) else "") + w))
            text = "\n\n".join(f"{n}\n{a} --> {z}\n{b}" for n, (a, z, b) in enumerate(new, 1)) + "\n"
            try:
                res = await asyncio.to_thread(submake.restyle_one, f["fid"], "", text, False,
                                              submake.TRACK_NAME, True, True)
            except Exception as e:                               # noqa: BLE001
                res = {"ok": False, "why": f"{type(e).__name__}: {e}"[:160]}
            if res.get("ok"):
                STATE["mended"] += mended
                STATE["removed"] += removed
                try:
                    r = submake._row(f["fid"])
                    subquality.record(f["fid"], f["label"], subquality.score(text, float(r["duration"] or 0)))
                    # so the mend is not read later as something you changed
                    sublearn.remember_made(f["fid"], r["path"], text, "submake")
                except Exception:                                # noqa: BLE001
                    pass
                joblog.log(f"subtitle line fix: {f['label']} - {mended} line(s) translated"
                           + (f", {removed} removed" if removed else ""), "ok")
            else:
                STATE["failed"] += 1
                joblog.log(f"subtitle line fix: {f['label']} left as it was - {res.get('why')}", "warn")
            STATE["last"].append({"label": f["label"], "ok": bool(res.get("ok")), "mended": mended,
                                  "removed": removed, "why": res.get("why") or ""})
            STATE["done"] += 1
        STATE["phase"] = "done"
    except Exception as e:                                       # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        if held:
            heavy.release(lane)
        STATE.update(running=False, ended=time.time())


def status() -> dict:
    return dict(STATE) | {"last": STATE["last"][-80:], "waiting": len(candidates())}
