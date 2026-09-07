r"""Retitle nuarr's OCR'd subtitle tracks in place, across the library.

Files written before the clean-title change carry text tracks named like
"English [Full] (PGS) (OCR)". This walks nuarr's own probe records for such
tracks and, with mkvpropedit - a header edit, no rewrite, a second per file -
sets the title to the clean label and adds the NUARR_OCR tag the readers now
look for.

    python tools\retitle_ocr_tracks.py            dry run: count and list
    python tools\retitle_ocr_tracks.py --apply    do it
    python tools\retitle_ocr_tracks.py --apply --log C:\path\to\log.txt

Only tracks nuarr made (the "(OCR...)" suffix) are touched. Picture tracks
keep their names. A file that is locked or mid-move is skipped and reported;
run again later for those.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = sys.stdout

from app import subocr                                  # noqa: E402
from app.config import DB_PATH                          # noqa: E402
import sqlite3                                          # noqa: E402

APPLY = "--apply" in sys.argv
LOG = None
if "--log" in sys.argv:
    LOG = open(sys.argv[sys.argv.index("--log") + 1], "a", encoding="utf-8")


def say(msg: str) -> None:
    print(msg, flush=True)
    if LOG:
        LOG.write(msg + "\n"); LOG.flush()


def mkvpropedit() -> str:
    mk = subocr._mkvmerge()
    return os.path.join(os.path.dirname(mk), "mkvpropedit.exe")


def candidates() -> list[tuple[int, str]]:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    out = []
    for fid, path, pj in con.execute(
            "SELECT f.id, f.path, p.json FROM files f JOIN file_probes p ON p.file_id=f.id "
            "WHERE f.state NOT IN ('deleted') AND p.json LIKE '%(OCR%'"):
        try:
            streams = (json.loads(pj) or {}).get("streams") or []
        except Exception:                                    # noqa: BLE001
            continue
        hit = any(s.get("codec_type") == "subtitle"
                  and (s.get("codec_name") or "").lower() not in subocr.IMG_CODECS
                  and subocr._OCR_TITLE.search(str((s.get("tags") or {}).get("title") or ""))
                  for s in streams)
        if hit:
            out.append((fid, path))
    return out


def fix_file(path: str, xml: str) -> tuple[int, str]:
    """-> (tracks retitled, note)."""
    mk = subocr._mkvmerge()
    r = subprocess.run([mk, "-J", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=120, creationflags=0x08000000)
    if not (r.stdout or "").strip():
        raise RuntimeError(f"mkvmerge -J gave nothing (rc {r.returncode}): {(r.stderr or '').strip()[:200]}")
    j = json.loads(r.stdout)
    args = [mkvpropedit(), path]
    n = 0
    names = []
    for t in j.get("tracks") or []:
        if not subocr.is_ocr_track(t):
            continue
        p = t.get("properties") or {}
        uid = p.get("uid")
        old = str(p.get("track_name") or "")
        new = subocr.clean_label(old)
        if uid is None:
            continue
        args += ["--edit", f"track:={uid}", "--set", f"name={new}",
                 "--tags", f"track:={uid}:{xml}"]
        names.append(f"{old!r} -> {new!r}")
        n += 1
    if not n:
        return 0, "no OCR track by mkvmerge's reading"
    if not APPLY:
        return n, "; ".join(names)
    r = subprocess.run(args, capture_output=True, text=True, timeout=300,
                       creationflags=0x08000000)
    if r.returncode not in (0, 1):          # 1 = warnings, still written
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return n, "; ".join(names)


def main() -> None:
    cands = candidates()
    say(f"{'APPLY' if APPLY else 'DRY RUN'}: {len(cands)} file(s) carry an OCR track with the old title")
    d = tempfile.mkdtemp(prefix="nuarr_retitle_")
    xml = os.path.join(d, "tags.xml")
    open(xml, "w", encoding="utf-8").write(subocr._OCR_TAG_XML)
    done = skipped = failed = tracks = 0
    busy: set[str] = set()
    t0 = time.time()
    def busy_paths() -> set[str]:
        """Files nuarr has a job on right now - not touched in flight."""
        try:
            import urllib.request
            j = json.load(urllib.request.urlopen("http://localhost:8770/api/jobs/live", timeout=5))
            return {str(w.get("path") or "").lower() for w in j.get("running") or []}
        except Exception:                                    # noqa: BLE001
            return set()

    for i, (fid, path) in enumerate(cands, 1):
        if not os.path.exists(path):
            skipped += 1
            continue
        # LEAVE ALONE anything being worked on or freshly written: a job of
        # nuarr's, or a file whose bytes changed in the last ten minutes
        # (an import landing, a mover mid-copy). It is picked up next run.
        if i % 25 == 1:
            busy = busy_paths()
        if path.lower() in busy or time.time() - os.path.getmtime(path) < 600:
            skipped += 1
            say(f"  skip (in use) {os.path.basename(path)[:70]}")
            continue
        try:
            n, note = fix_file(path, xml)
        except Exception as e:                               # noqa: BLE001
            failed += 1
            say(f"  FAIL {os.path.basename(path)[:70]}: {type(e).__name__}: {e}")
            continue
        if n:
            done += 1; tracks += n
            if not APPLY or i <= 20 or i % 100 == 0:
                say(f"  [{i}/{len(cands)}] {os.path.basename(path)[:70]}: {note}")
        else:
            skipped += 1
    say(f"{'done' if APPLY else 'would do'}: {done} file(s), {tracks} track(s); "
        f"{skipped} skipped, {failed} failed, {time.time()-t0:.0f}s")
    say("END")


if __name__ == "__main__":
    main()
