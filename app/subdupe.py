r"""nuarr - one subtitle track per language and kind, and no more.

WHY THIS EXISTS, SAID PLAINLY: nuarr put the extra tracks there.

The sidecar sweep rewrites a file to take a subtitle in, and for a long time
it did not re-read the file afterwards. Every guard that stops a SECOND copy
of a language going in reads the stored probe, so the record went on saying
"no English inside" after English had gone in - and a release that ships three
English sidecars (.en.ass, .1.en.ass, .2.en.ass, which sxales does) got one
per pass. Three passes, three identical tracks. Measured across the library
when it was found: 376 files, 407 extra tracks, 189 of them Detective Conan.

That hole is closed at the source. This is the repair, and it is also the
thing to have anyway - a file can arrive with two identical tracks without any
help from nuarr.

WHAT IT WILL AND WILL NOT REMOVE

A track is removed only when another of the SAME LANGUAGE AND THE SAME KIND is
still in the file afterwards. Kind is the same four-way distinction the rest of
the subtitle code uses - full, forced, SDH, marker - so an SDH track beside a
full one is not a duplicate of it, a signs track is not a duplicate of
dialogue, and the blank marker that says "the words are painted on" is never
mistaken for a subtitle. That is what keeps this away from the eighteen
thousand files whose probe shows eng twice because the release shipped a full
track and an SDH track.

It does not touch a language the library does not want. Removing those is a
different decision with different consequences - it deletes something that
came in the release - and it belongs to the rules, not here.

WHICH COPY SURVIVES. The one with the most lines in it. Two tracks that look
identical in the header are not necessarily identical inside: one can be
truncated, or empty, or a different sub. Keeping whichever came first is the
cheap answer and it is wrong exactly when it matters most, so every candidate
is extracted and its events counted before anything is dropped. On a tie, the
first one wins, because then it genuinely does not matter.

AND NOTHING IS DROPPED WITHOUT READING THE FILE BACK. The rebuilt copy must
carry one of every language and kind the original had before it replaces
anything - a rewrite that lost a track is a rewrite that gets thrown away.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time

from . import joblog
from .config import NO_WINDOW, SETTINGS, hidden_si
from .db import cursor

from .subembed import (_lang_key, _live_sub_tracks, _mkvmerge, _size,
                       have_mkvmerge)

KEY = "subdupe"
TITLE = "Duplicate subtitle tracks"

# Never look at a file that is still being written.
SETTLE_S = 600.0


def _groups(tracks: list) -> dict:
    """{(language, kind): [tracks]} - the groups with more than one are the work."""
    out: dict = {}
    for t in tracks:
        out.setdefault((t["lang"], t["class"]), []).append(t)
    return {k: v for k, v in out.items() if len(v) > 1}


def _probe_groups(file_id: int) -> dict:
    """The same question asked of the STORED probe, which is cheap enough to
    ask of forty thousand files. Only ever used to decide what is worth
    opening; the decision itself is taken against the file."""
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if not r:
            return {}
        from .subembed import _track_class
        seen: dict = {}
        for s in (json.loads(r["json"]).get("streams") or []):
            if s.get("codec_type") != "subtitle":
                continue
            tags = s.get("tags") or {}
            disp = s.get("disposition") or {}
            k = (_lang_key(tags.get("language") or "und"),
                 _track_class(tags.get("title") or "",
                              bool(disp.get("forced"))))
            seen[k] = seen.get(k, 0) + 1
        return {k: v for k, v in seen.items() if v > 1}
    except Exception:                                            # noqa: BLE001
        return {}


def candidates(limit: int = 100000) -> list[dict]:
    r"""Files whose stored probe shows two subtitle tracks of the same kind.

    THE CHEAP FILTER FIRST. sub_langs is a comma list on the row, so a
    repeated language is a string test rather than a JSON parse - and a file
    without one cannot possibly have a duplicate. Only the survivors are
    parsed, and only the ones that still look duplicated are opened.
    """
    cutoff = time.time() - SETTLE_S
    rows = []
    try:
        with cursor() as cur:
            rows = [dict(r) for r in cur.execute(
                "SELECT id, path, library, pool_disk, sub_langs FROM files "
                " WHERE state NOT IN ('deleted','duplicate') "
                "   AND COALESCE(path,'') != '' "
                "   AND COALESCE(sub_langs,'') LIKE '%,%' "
                "   AND COALESCE(mtime,0) < ? "
                " ORDER BY id", (cutoff,))]
    except Exception:                                            # noqa: BLE001
        return []
    out = []
    for r in rows:
        if len(out) >= limit:
            break
        langs = [x for x in (r.get("sub_langs") or "").split(",") if x.strip()]
        if len(langs) == len(set(langs)):
            continue                       # no language twice: nothing to do
        g = _probe_groups(int(r["id"]))
        if not g:
            continue                       # twice, but as different kinds
        out.append({"file_id": int(r["id"]), "path": r["path"],
                    "library": r.get("library") or "",
                    "pool_disk": r.get("pool_disk") or "",
                    "kinds": [f"{k[0]} {k[1]}" for k in g]})
    return out


def _events(path: str, track_id: int) -> int:
    """How many subtitle events this track actually carries.

    EXTRACTED AND COUNTED, NOT ESTIMATED. Two tracks with the same language
    and the same title can be a full subtitle and a truncated one, and the
    whole point of choosing is to keep the one with something in it.
    """
    d = tempfile.mkdtemp(prefix="nuarr-dupe-")
    out = os.path.join(d, f"t{track_id}.sub")
    try:
        exe = os.path.join(os.path.dirname(_mkvmerge()), "mkvextract.exe")
        if not os.path.exists(exe):
            exe = "mkvextract"
        subprocess.run([exe, path, "tracks", f"{track_id}:{out}"],
                       capture_output=True, text=True, timeout=600,
                       creationflags=NO_WINDOW, startupinfo=hidden_si())
        if not os.path.exists(out):
            return -1
        txt = ""
        with open(out, "r", encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        n = len(re.findall(r"^Dialogue:", txt, re.M))
        if not n:
            # SRT and friends: a cue is a line of two timestamps.
            n = len(re.findall(r"-->", txt))
        return n
    except Exception:                                            # noqa: BLE001
        return -1
    finally:
        try:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)
        except Exception:                                        # noqa: BLE001
            pass


def plan_one(file_id: int) -> dict:
    """Which tracks would go, and which would stay. Reads only."""
    with cursor() as cur:
        r = cur.execute("SELECT path FROM files WHERE id=?",
                        (int(file_id),)).fetchone()
    path = (r["path"] if r else "") or ""
    if not path or not os.path.exists(path):
        return {"ok": False, "why": "the file is not on disk"}
    live = _live_sub_tracks(path)
    g = _groups(live)
    if not g:
        return {"ok": True, "path": path, "drop": [], "keep": [],
                "why": "no two tracks of the same language and kind"}
    drop, keep = [], []
    for (lang, cls), tracks in sorted(g.items()):
        counts = [(_events(path, t["id"]), t) for t in tracks]
        # Most lines wins; a tie or an unreadable count keeps the first.
        counts.sort(key=lambda ct: (-(ct[0] if ct[0] >= 0 else -1),
                                    ct[1]["ord"]))
        keep.append({**counts[0][1], "events": counts[0][0],
                     "lang_kind": f"{lang} {cls}"})
        for n, t in counts[1:]:
            drop.append({**t, "events": n, "lang_kind": f"{lang} {cls}",
                         "why": f"a second {cls} {lang} track; keeping the "
                                f"one with {counts[0][0]} lines"})
    return {"ok": True, "path": path, "drop": drop, "keep": keep}


def fix_one(file_id: int, report=None) -> dict:
    r"""Rewrite the file without its duplicate tracks. One remux, verified.

    THE SAME DISCIPLINE THE SIDECAR SWEEP USES, for the same reasons: the
    working copy goes to the cache rather than beside the source, the rebuilt
    file is READ BACK before it replaces anything, and the probe is refreshed
    afterwards - because not refreshing it is the whole reason this module has
    any work to do.
    """
    from . import fileops, idle
    from .subembed import _run_reporting
    p = plan_one(int(file_id))
    if not p.get("ok"):
        return p
    drop, path = p.get("drop") or [], p["path"]
    if not drop:
        return {"ok": True, "removed": 0, "why": p.get("why") or "nothing to do"}
    if os.path.splitext(path)[1].lower() != ".mkv":
        return {"ok": False, "why": "only Matroska tracks can be removed this way"}
    if not have_mkvmerge():
        return {"ok": False, "why": "mkvmerge is not installed"}
    if fileops.is_locked(path):
        return {"ok": False, "why": "the file is in use"}
    ok_room, why_room = fileops.cache_room(_size(path))
    if not ok_room:
        return {"ok": False, "why": why_room}
    work = getattr(report, "task", None)
    mine = work is None
    if mine:
        work = idle.claim("Duplicate subtitles", os.path.basename(path),
                          now=os.path.basename(path), note="removing a copy")
    claim = fileops.cache_reserve(_size(path))
    claim.__enter__()
    tmp = fileops.cache_temp(".mkv", "dupe")
    try:
        ids = ",".join(str(t["id"]) for t in drop)
        cmd = [_mkvmerge(), "-o", tmp, "--subtitle-tracks", "!" + ids, path]
        try:
            r = _run_reporting(cmd, report,
                               on_pid=(work.set_pid if work is not None else None))
        except Exception as e:                                   # noqa: BLE001
            fileops._quiet_remove(tmp)
            return {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if r.returncode >= 2 or not os.path.exists(tmp):
            fileops._quiet_remove(tmp)
            return {"ok": False,
                    "why": (r.stderr or r.stdout or "mkvmerge failed").strip()[:300]}
        # READ IT BACK. Every language and kind that was in the original has to
        # still be there, exactly once. A rebuild that lost a subtitle is a
        # rebuild that gets deleted rather than committed.
        after = _groups(_live_sub_tracks(tmp))
        got = {(t["lang"], t["class"]) for t in _live_sub_tracks(tmp)}
        want = {(t["lang"], t["class"]) for t in (p.get("keep") or [])}
        if after or not want <= got:
            fileops._quiet_remove(tmp)
            lost = ", ".join(f"{l} {c}" for l, c in sorted(want - got))
            return {"ok": False,
                    "why": (f"the rebuilt file is missing {lost}"
                            if lost else
                            "the rebuilt file still has duplicates")}
        res = fileops.safe_replace(path, tmp)
        if not getattr(res, "ok", False):
            fileops._quiet_remove(tmp)
            return {"ok": False,
                    "why": f"could not put it in place: {getattr(res, 'why', '')}"}
        # AND THE RECORD IS REFRESHED, which is the lesson that created this
        # module in the first place.
        try:
            from . import jobs as _jobs
            from .subembed import _plain
            _r = _plain([_jobs._ffprobe_exe(), "-v", "quiet", "-print_format",
                         "json", "-show_streams", "-show_format", path], 180)
            _d = json.loads(_r[1] or "{}")
            if _d.get("streams"):
                _jobs.cache_probe(int(file_id), _d)
            else:
                raise ValueError("ffprobe said nothing")
        except Exception:                                        # noqa: BLE001
            try:
                with cursor() as cur:
                    cur.execute("DELETE FROM file_probes WHERE file_id=?",
                                (int(file_id),))
            except Exception:                                    # noqa: BLE001
                pass
        try:
            with cursor() as cur:
                cur.execute("UPDATE files SET size=?, mtime=?, updated_at=? "
                            " WHERE id=?",
                            (_size(path), os.path.getmtime(path), time.time(),
                             int(file_id)))
        except Exception:                                        # noqa: BLE001
            pass
        _note(int(file_id), path, drop)
        joblog.log(f"removed {len(drop)} duplicate subtitle track(s) from "
                   f"{os.path.basename(path)} - "
                   + "; ".join(t["why"] for t in drop), "info",
                   system="duplicate subtitles")
        try:
            from . import notify
            notify.file_changed([int(file_id)], "duplicate subtitle tracks "
                                                "removed", system="subdupe")
        except Exception:                                        # noqa: BLE001
            pass
        return {"ok": True, "removed": len(drop), "path": path}
    finally:
        if mine and work is not None:
            work.close()
        claim.__exit__(None, None, None)


def _note(file_id: int, path: str, drop: list) -> None:
    """A line per removal, so this is answerable after the fact."""
    try:
        with cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subdupe_log(
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    at      REAL NOT NULL,
                    file_id INTEGER,
                    path    TEXT,
                    detail  TEXT)""")
            for t in drop:
                cur.execute(
                    "INSERT INTO subdupe_log(at,file_id,path,detail) "
                    "VALUES(?,?,?,?)",
                    (time.time(), int(file_id or 0), path,
                     f"{t['lang_kind']} track {t['ord'] + 1} "
                     f"({t.get('events', -1)} lines) removed; "
                     f"{t.get('why') or ''}"[:400]))
    except Exception:                                            # noqa: BLE001
        pass


def stats() -> dict:
    out = {"files": 0, "removed": 0, "last_run": 0.0}
    try:
        with cursor() as cur:
            r = cur.execute("SELECT COUNT(*) n, COUNT(DISTINCT file_id) f, "
                            "       MAX(at) a FROM subdupe_log").fetchone()
            out["removed"] = int((r["n"] if r else 0) or 0)
            out["files"] = int((r["f"] if r else 0) or 0)
            out["last_run"] = float((r["a"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from . import idle
        out = idle.merge_stats(KEY, out)
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ------------------------------------------------------------ the runner ----
ENABLED_KEY = "subdupe.enabled"


def enabled() -> bool:
    r"""Off until somebody turns it on, and that is not timidity.

    Everything else nuarr does in the background either adds something or
    moves something; this is the only sweep that takes a track OUT of a file
    that had it. The repair it exists for is real and measured, but a person
    should decide to run it rather than discover it has run - especially since
    two tracks that look identical in the header can differ inside, which is
    exactly what City Hunter turned out to be: 398 lines, 397, and 360.
    """
    try:
        from . import gate
        return gate.get_toggle(ENABLED_KEY)
    except Exception:                                            # noqa: BLE001
        return False


def _pending() -> list:
    if not enabled():
        return []
    try:
        return candidates(100000)
    except Exception:                                            # noqa: BLE001
        return []


def _do_one(p: dict, report=None) -> dict:
    return fix_one(int(p["file_id"]), report=report)


async def watch() -> None:
    """Work through the duplicates for as long as the box can spare it."""
    from . import idle
    await asyncio.sleep(360)
    await idle.run(KEY, TITLE, _pending, _do_one,
                   label=lambda p: os.path.basename(p.get("path") or "")[:120],
                   disk_of=lambda p: p.get("pool_disk") or "",
                   note_of=lambda p: "removing a duplicate subtitle",
                   system_name="Duplicate subtitles",
                   goto="/settings#lang")
