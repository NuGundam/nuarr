r"""Make the audio picker tell the truth about what it is offering.

THE PROBLEM, FOUND IN A LIBRARY THAT LOOKED FINE. A release group writes the
track title, and it describes the source: "English FLAC 5.1", "TrueHD / 5.1 /
2076 kbps". nuarr then re-encodes the stream and the title outlives the thing
it described. Overlord season 1 offers "TrueHD / 5.1 / 2076 kbps" for a 640k
E-AC3 track. Season 3 offers "English FLAC 5.1" for a stereo AAC track with no
centre channel at all - the title promises surround the file does not contain,
and that one was wrong before nuarr ever touched it.

Plex shows exactly this string in its audio picker. So the one place a person
goes to choose between tracks is describing formats that are not there.

WHY THIS IS ITS OWN THING AND NOT A RULE. The audit rules fix files by running
a job, which means re-reading and rewriting the file. A title is a string in
the container header: mkvpropedit rewrites it in place in about a tenth of a
second, with the file's bytes otherwise untouched. Sending 39,000 files through
the transcoder to correct a caption would be absurd, so this does the small
thing that is actually required.

NOTHING IS RE-PROBED. nuarr already stores an ffprobe result for every file, so
the whole library can be checked from the database without reading a single
byte off the pool - the check costs a query, not an hour of disk.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from .config import SETTINGS
from .db import cursor
from . import joblog
from .rules import audio_title, _title_is_only_format

_CACHE: dict = {"at": 0.0, "data": None, "running": False,
                "done": 0, "total": 0,
                "fixing": None, "failures": []}


def _mkvpropedit() -> str:
    p = getattr(SETTINGS, "mkvpropedit", "") or ""
    if p and os.path.exists(p):
        return p
    guess = r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
    return guess if os.path.exists(guess) else "mkvpropedit"


# CONTRADICTION, NOT DISAGREEMENT WITH MY PREFERRED WORDING. The first pass of
# this flagged 43,716 tracks, and most were nothing: '"English AAC 2.0"' differs
# from 'English AAC 2.0' only by quote marks, and "Surround" on a 5.1 track is
# vague but perfectly true. Rewriting 29,772 files to restyle titles that were
# already correct is churn, not repair - and it would have buried the few
# hundred that genuinely lie among thousands that do not.
#
# So a title is only stale when it NAMES a codec the stream is not, or NAMES a
# layout the stream does not have. Anything vaguer than that is left alone.
_CODEC_CLAIMS = [
    ("truehd", ("truehd", "true-hd", "thd")),
    ("dts",    ("dts",)),
    ("flac",   ("flac",)),
    ("eac3",   ("eac3", "e-ac3", "dd+", "ddp", "ddplus",
                "dolby digital plus")),
    ("ac3",    ("ac3", "ac-3", "dolby digital")),
    ("aac",    ("aac",)),
    ("opus",   ("opus",)),
    ("mp3",    ("mp3",)),
    ("pcm",    ("pcm", "lpcm")),
]
_LAYOUT_CLAIMS = [(8, ("7.1",)), (7, ("6.1",)), (6, ("5.1",)),
                  (2, ("2.0", "stereo")), (1, ("1.0", "mono"))]


def _claimed(title: str):
    """(codec, channels) the title asserts, either as None when it asserts none."""
    t = " " + (title or "").lower().replace("_", " ") + " "
    codec = None
    for fam, words in _CODEC_CLAIMS:          # ordered: dd+ before dd
        if any(w in t for w in words):
            codec = fam
            break
    ch = None
    for n, words in _LAYOUT_CLAIMS:
        if any(w in t for w in words):
            ch = n
            break
    return codec, ch


def _codec_family(name: str) -> str:
    n = (name or "").lower()
    return {"eac3": "eac3", "ac3": "ac3", "aac": "aac", "flac": "flac",
            "truehd": "truehd", "opus": "opus", "mp3": "mp3",
            "pcm_s16le": "pcm", "pcm_s24le": "pcm"}.get(
                n, "dts" if n.startswith("dts") else n)


def _rows_from_probe(path: str, probe: dict) -> list:
    r"""Audio tracks whose title contradicts the stream, one row each.

    Two filters, both deliberately narrow. The title must say nothing except
    format - anything carrying a word nuarr cannot regenerate ("Commentary", a
    fansub group) is information and is left alone. And it must actually be
    WRONG about the codec or the channel count, not merely differently phrased.
    """
    out = []
    a_i = 0
    for s in (probe.get("streams") or []):
        if s.get("codec_type") != "audio":
            continue
        a_i += 1                       # mkvpropedit numbers tracks from 1
        tags = s.get("tags") or {}
        old = (tags.get("title") or "").strip()
        if not old or not _title_is_only_format(old):
            continue
        real_codec = _codec_family(s.get("codec_name") or "")
        real_ch = int(s.get("channels") or 0)
        c_claim, ch_claim = _claimed(old)
        wrong = []
        if c_claim and real_codec and c_claim != real_codec:
            wrong.append(f"says {c_claim.upper()}, is {real_codec.upper()}")
        if ch_claim and real_ch and ch_claim != real_ch:
            wrong.append(f"says {ch_claim}ch, is {real_ch}ch")
        if not wrong:
            continue
        want = audio_title(tags.get("language") or "",
                           s.get("codec_name") or "", real_ch or 2)
        if not want or want == old:
            continue
        out.append({"track": a_i, "old": old, "new": want, "why": "; ".join(wrong),
                    "codec": s.get("codec_name"), "ch": real_ch,
                    "lang": tags.get("language") or "und"})
    return out


def scan(limit: int = 0) -> dict:
    r"""Every stale audio title in the library, from stored probes alone."""
    rows, checked = [], 0
    _CACHE.update(running=True, done=0, total=0)
    try:
        with cursor() as c:
            todo = list(c.execute(
                "SELECT f.id, f.path, p.json FROM files f "
                "  JOIN file_probes p ON p.file_id = f.id "
                " WHERE f.state NOT IN ('deleted','duplicate')"))
        _CACHE["total"] = len(todo)
        for n, r in enumerate(todo, 1):
            _CACHE["done"] = n
            # .mkv only: mkvpropedit is a Matroska tool, and an mp4 would need
            # a full remux to change one string - not worth it for a caption.
            if not (r["path"] or "").lower().endswith(".mkv"):
                continue
            checked += 1
            try:
                probe = json.loads(r["json"] or "{}")
            except Exception:                                # noqa: BLE001
                continue
            hits = _rows_from_probe(r["path"], probe)
            for h in hits:
                rows.append(dict(h, file_id=r["id"], path=r["path"]))
            if limit and len(rows) >= limit:
                break
    finally:
        _CACHE["running"] = False
    files = len({r["file_id"] for r in rows})
    return {"checked": checked, "at": time.time(), "rows": rows[:4000],
            "total": len(rows), "files": files}


def _fix_file(path: str, edits: list) -> tuple[bool, str]:
    """One mkvpropedit call per file, however many tracks it corrects."""
    cmd = [_mkvpropedit(), path]
    for e in edits:
        cmd += ["--edit", f"track:a{e['track']}", "--set", f"name={e['new']}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:140]
    return True, ""


def fix(rows: list | None = None) -> dict:
    r"""Correct the titles in place. No re-encode, no rewrite, no data touched.

    The stored probe is updated to match, because leaving it stale would make
    the very next scan report the same files all over again.
    """
    data = rows if rows is not None else (_CACHE.get("data") or {}).get("rows")
    if not data:
        return {"fixed": 0, "files": 0, "failed": 0}
    by_file: dict = {}
    for r in data:
        by_file.setdefault((r["file_id"], r["path"]), []).append(r)
    out = {"fixed": 0, "files": 0, "failed": 0}
    failures: list = []
    _CACHE["fixing"] = {"running": True, "done": 0, "total": len(by_file),
                        "fixed": 0, "failed": 0, "where": ""}
    prog = _CACHE["fixing"]
    try:
        for (fid, path), edits in by_file.items():
            prog["where"] = os.path.basename(path)[:60]
            if not os.path.exists(path):
                out["failed"] += len(edits)
                prog["failed"] += len(edits)
                failures.append({"path": path, "why": "the file is not there"})
                prog["done"] += 1
                continue
            ok, err = _fix_file(path, edits)
            if ok:
                out["fixed"] += len(edits)
                out["files"] += 1
                prog["fixed"] += len(edits)
                _restamp(fid, path, edits)
            else:
                out["failed"] += len(edits)
                prog["failed"] += len(edits)
                failures.append({"path": path, "why": err or "mkvpropedit failed"})
            prog["done"] += 1
    finally:
        prog["running"] = False
        _CACHE["failures"] = failures[:200]
    if out["fixed"]:
        joblog.log(f"audio titles: corrected {out['fixed']} track title(s) "
                   f"across {out['files']} file(s) - the picker now names the "
                   f"format that is actually there", "ok")
    if out["failed"]:
        joblog.log(f"audio titles: {out['failed']} could not be corrected",
                   "warn")
    return out


def _restamp(file_id: int, path: str, edits: list) -> None:
    r"""Keep nuarr's own records true after an edit.

    Two things go stale the moment mkvpropedit writes a longer string into the
    header, and both cause visible nonsense if left:

      - the stored PROBE still holds the old title, so the very next scan
        reports the same files all over again and Put right never finishes;
      - the recorded SIZE is now short by however much the header grew - about
        20 KB a file here. Left alone, correcting 1,799 titles would invent
        1,799 size disagreements for the arr agreement check to find, and it
        would be right to find them. Manufacturing work for another part of
        the system to clean up is not a fix.

    The arrs' own records go stale too, and that is deliberately left to the
    agreement check: asking an arr to re-read is its job, it batches per
    series rather than per file, and it already runs on a schedule.
    """
    try:
        with cursor() as c:
            row = c.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (file_id,)).fetchone()
            if row:
                probe = json.loads(row["json"] or "{}")
                want = {e["track"]: e["new"] for e in edits}
                a_i = 0
                for s in (probe.get("streams") or []):
                    if s.get("codec_type") != "audio":
                        continue
                    a_i += 1
                    if a_i in want:
                        s.setdefault("tags", {})["title"] = want[a_i]
                c.execute("UPDATE file_probes SET json=? WHERE file_id=?",
                          (json.dumps(probe), file_id))
            try:
                now = os.path.getsize(path)
            except OSError:
                now = 0
            if now:
                c.execute("UPDATE files SET size=? WHERE id=?", (now, file_id))
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"audio titles: record update failed: {type(e).__name__}",
                   "warn")


def cached() -> dict:
    d = _CACHE.get("data")
    return {"have": bool(d), "running": bool(_CACHE.get("running")),
            "age_s": (round(time.time() - _CACHE["at"], 1)
                      if _CACHE.get("at") else None),
            "progress": {"done": _CACHE.get("done", 0),
                         "total": _CACHE.get("total", 0)},
            "fixing": _CACHE.get("fixing"),
            "failures": _CACHE.get("failures") or [],
            **(d or {"checked": 0, "rows": [], "total": 0, "files": 0})}


def refresh() -> dict:
    if _CACHE.get("running"):
        return cached()
    try:
        _CACHE.update(data=scan(), at=time.time())
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"audio title check: {type(e).__name__}: {e}", "warn")
    return cached()
