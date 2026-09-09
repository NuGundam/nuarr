r"""nuarr - pull a sidecar subtitle inside the file it belongs to.

WHY, AND WHAT IT COSTS TO LEAVE THEM OUT
----------------------------------------
A sidecar is a subtitle sitting next to the video as its own file. Plex reads
them and plays them, so nothing looks wrong - and three separate things go
wrong quietly:

    IT GETS ORPHANED. subocr's docstring already says it: "a sidecar is one arr
    rename away from being orphaned". An upgrade replaces the mkv, Sonarr
    renames the folder, and the .ass stays behind pointing at nothing. This
    library has 13,004 of them in Anime Shows alone.

    IT WINS THE PICKER. Plex prefers an external subtitle over an embedded one
    for the same language. So a file with a perfectly good embedded track can
    have the sidecar chosen for it.

    AND FOR ASS, IT BURNS. An external ASS has to be injected by the server,
    and injecting it means painting it onto the picture - which re-encodes the
    whole video to carry it. Measured here: hevc -> hevc, audio copied
    untouched, stream bitrate 4.6x the source, for one subtitle.

Embedding is a stream copy. mkvmerge writes a new container with the same
video, the same audio and the subtitle added; nothing is re-encoded and nothing
loses a generation.

THE THREE GUARDS, AND WHY EACH ONE IS THERE
-------------------------------------------
1. THE LANGUAGE RULES DECIDE FIRST. If the library would not keep a Spanish
   subtitle track, it must not gain one just because somebody dropped a
   .es.srt in the folder. The sidecar is put through the same policy the
   planner applies to embedded tracks, so the two cannot disagree.

2. NOT IF THAT LANGUAGE IS ALREADY INSIDE. Per language, not per file: a
   release with Japanese embedded subs still takes an English sidecar, because
   those are not the same subtitle. What this prevents is two English tracks
   where there was one.

3. THE SIDECAR IS RECYCLED, NEVER DELETED, AND ONLY AFTER THE RESULT IS READ
   BACK. mkvmerge exiting 0 is not proof; the new file is probed and the track
   has to be in it, with the right language, before anything is removed. If
   that check fails the original file is untouched and the sidecar is still
   where it was.

WHY IT IS OFF BY DEFAULT
------------------------
It rewrites files and removes others. Every other subtitle rule in this list
changes what a rebuild produces; this one starts a rebuild and reaches outside
the file to do it. That earns an explicit switch rather than a default.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

from . import joblog
from .config import NO_WINDOW, SETTINGS, hidden_si
from .db import cursor

# What counts as a subtitle sitting next to a video.
SIDECAR_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi")
# WHICH ONE WINS WHEN A FOLDER HAS TWO FOR ONE LANGUAGE. Found in the preview
# before this existed: Doom Patrol S01E05 has both an .ass and an .srt in
# English, and taking both would put two English tracks in the file - the exact
# duplication the embedded-language guard was written to prevent, arriving
# through the side door.
#
# SRT wins, and not by accident. The whole reason for this rule is that ASS
# forces a burn on most clients; embedding the ASS instead of the SRT would
# carry that problem inside the file rather than solving it. The ASS is left on
# disk rather than recycled, because it is the styled copy and nothing has
# replaced it.
_PREFER = {".srt": 0, ".vtt": 1, ".smi": 2, ".ass": 3, ".ssa": 4, ".sub": 5}
# Plex reads these from a subfolder too, as of server 1.41.
SUB_DIRS = ("subs", "subtitles")

# How many files one pass may rewrite. Each is a stream-copy remux of a whole
# file - minutes of disk, not of GPU - and a library of 13,000 sidecars would
# otherwise saturate the pool for a day the first time this is switched on.
PER_RUN = 12
CYCLE_S = 600
# Never touch a file written in the last few minutes; it may still be landing.
SETTLE_S = 600

STATE: dict = {"running": False, "done": 0, "total": 0, "now": "",
               "last_run": 0.0, "embedded": 0, "recycled": 0, "last_error": ""}

_READY = False


def init() -> None:
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subembed_log(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                at       REAL NOT NULL,
                file_id  INTEGER,
                path     TEXT,
                sidecar  TEXT,
                lang     TEXT,
                ok       INTEGER NOT NULL DEFAULT 0,
                detail   TEXT
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_subembed_at "
                    "ON subembed_log(at)")
    _READY = True


def enabled(library: str = "") -> bool:
    """The per-library switch, off unless somebody turned it on."""
    try:
        from . import subocr
        return bool(subocr.sub_rules(library).get("embed_sidecars"))
    except Exception:                                            # noqa: BLE001
        return False


def _mkvmerge() -> str:
    p = str(getattr(SETTINGS, "mkvmerge", "") or "")
    if p and os.path.exists(p):
        return p
    # Sits beside mkvpropedit, which two other modules already resolve this way.
    near = str(getattr(SETTINGS, "mkvpropedit", "") or "")
    if near:
        guess = os.path.join(os.path.dirname(near), "mkvmerge.exe")
        if os.path.exists(guess):
            return guess
    guess = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
    return guess if os.path.exists(guess) else "mkvmerge"


def have_mkvmerge() -> bool:
    exe = _mkvmerge()
    return bool(exe) and (os.path.exists(exe) or exe == "mkvmerge")


# --------------------------------------------------------- reading the name --
# Plex's own convention, and the only thing a sidecar carries about itself:
#   Show - S01E02 [tags].en.srt          .en.forced.srt      .eng.sdh.ass
# The language is the last token that looks like one; forced/sdh/cc are roles.
_ROLE = {"forced", "sdh", "cc", "hi", "default"}
_LANG_RE = re.compile(r"^[a-z]{2,3}$")


def read_sidecar_name(video: str, side: str) -> dict:
    r"""-> {lang, role, ok, why} for a sidecar beside `video`.

    WHAT IS BETWEEN THE TWO NAMES IS THE METADATA. The sidecar's stem starts
    with the video's stem; whatever follows, split on dots, is the language and
    any role flags. A file whose extra part is not a language code at all -
    "Show - S01E02.track3.srt" - has no language to check against the policy,
    and a subtitle nuarr cannot name the language of is one it must not embed
    with a guess.
    """
    vstem = os.path.splitext(os.path.basename(video))[0]
    sstem = os.path.splitext(os.path.basename(side))[0]
    if not sstem.startswith(vstem):
        return {"lang": "", "role": "", "ok": False,
                "why": "the name does not match this video"}
    rest = sstem[len(vstem):].strip(". ")
    if not rest:
        # "Show - S01E02.srt" beside "Show - S01E02.mkv": no language stated.
        return {"lang": "", "role": "", "ok": False,
                "why": "no language in the filename, so there is nothing to "
                       "check against the language rules"}
    parts = [p.strip().lower() for p in rest.split(".") if p.strip()]
    roles = [p for p in parts if p in _ROLE]
    langs = [p for p in parts if p not in _ROLE and _LANG_RE.match(p)]
    if not langs:
        return {"lang": "", "role": ",".join(roles), "ok": False,
                "why": f"{rest!r} is not a language code"}
    return {"lang": langs[0][:3], "role": ",".join(roles), "ok": True,
            "why": ""}


def _lang_key(code: str) -> str:
    """Two- and three-letter spellings of one language, compared as one."""
    try:
        from . import langkey
        return langkey.key(code)                       # type: ignore[attr-defined]
    except Exception:                                            # noqa: BLE001
        return (code or "")[:2].lower()


def wanted(lang: str, library: str) -> tuple[bool, str]:
    """Would this library keep a subtitle in this language?"""
    from . import langpolicy
    pol = langpolicy.for_library(library, "subs") or {}
    keep = {_lang_key(x) for x in (pol.get("langs") or [])}
    k = _lang_key(lang)
    if k in keep:
        return True, ""
    if pol.get("keep_untagged") and k in ("un", "", "und"):
        return True, ""
    want = ", ".join(sorted(pol.get("langs") or [])) or "nothing"
    return False, (f"{library} keeps {want} subtitles, and this one is "
                   f"{lang or 'untagged'}")


def sidecars_for(video: str) -> list[str]:
    """Every subtitle file that belongs to this video, folder or Subs/."""
    out: list[str] = []
    d = os.path.dirname(video)
    stem = os.path.splitext(os.path.basename(video))[0]
    places = [d] + [os.path.join(d, s) for s in SUB_DIRS]
    for place in places:
        try:
            if not os.path.isdir(place):
                continue
            for f in os.listdir(place):
                if not f.lower().endswith(SIDECAR_EXT):
                    continue
                if not f.startswith(stem):
                    continue
                out.append(os.path.join(place, f))
        except OSError:
            continue
    return sorted(out)


def embedded_langs(file_id: int) -> set:
    """Subtitle languages already inside the file, from the stored probe."""
    got: set = set()
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (int(file_id),)).fetchone()
        if not r:
            return got
        for s in (json.loads(r["json"]).get("streams") or []):
            if s.get("codec_type") != "subtitle":
                continue
            got.add(_lang_key((s.get("tags") or {}).get("language") or "und"))
    except Exception:                                            # noqa: BLE001
        pass
    return got


# ------------------------------------------------------------- the verdict --
def plan_one(file_id: int, force: bool = False) -> dict:
    r"""What would happen to this file. Reads only; changes nothing.

    Returns {ok, path, library, take: [...], skip: [{sidecar, why}]}. The
    refusals are carried, not dropped - a panel that lists what it will do and
    silently omits what it will not is a panel that cannot be checked.
    """
    with cursor() as cur:
        row = cur.execute("SELECT id, path, library, state FROM files "
                          "WHERE id=?", (int(file_id),)).fetchone()
    if not row:
        return {"ok": False, "why": "no such file"}
    row = dict(row)
    path, lib = row["path"] or "", row["library"] or ""
    out = {"ok": True, "file_id": int(file_id), "path": path, "library": lib,
           "take": [], "skip": []}
    if (row.get("state") or "") in ("deleted", "duplicate"):
        return {**out, "ok": False, "why": "not a live file"}
    if not enabled(lib) and not force:
        return {**out, "ok": False,
                "why": f"the embed rule is off for {lib or 'this library'}"}
    have = embedded_langs(int(file_id))
    for side in sidecars_for(path):
        name = read_sidecar_name(path, side)
        if not name["ok"]:
            out["skip"].append({"sidecar": side, "why": name["why"]})
            continue
        ok, why = wanted(name["lang"], lib)
        if not ok:
            out["skip"].append({"sidecar": side, "why": why})
            continue
        # GUARD TWO, PER LANGUAGE. A file with Japanese subs inside still takes
        # an English sidecar; what this stops is a second English track.
        if _lang_key(name["lang"]) in have:
            out["skip"].append({
                "sidecar": side,
                "why": f"the file already has a {name['lang']} subtitle track "
                       f"inside it"})
            continue
        out["take"].append({"sidecar": side, "lang": name["lang"],
                            "role": name["role"],
                            "size": _size(side)})

    # ONE PER LANGUAGE AND ROLE, best format first. Role is part of the key
    # because a forced track and a full one are different subtitles, the same
    # way the drop-covered rule already treats SDH and dialogue as different.
    best: dict = {}
    for t in out["take"]:
        k = (_lang_key(t["lang"]), t.get("role") or "")
        rank = _PREFER.get(os.path.splitext(t["sidecar"])[1].lower(), 9)
        cur = best.get(k)
        if cur is None or rank < cur[0]:
            if cur is not None:
                out["skip"].append({
                    "sidecar": cur[1]["sidecar"],
                    "why": f"another {t['lang']} sidecar in a better format is "
                           f"being taken instead "
                           f"({os.path.splitext(t['sidecar'])[1]})"})
            best[k] = (rank, t)
        else:
            out["skip"].append({
                "sidecar": t["sidecar"],
                "why": f"another {t['lang']} sidecar in a better format is "
                       f"being taken instead "
                       f"({os.path.splitext(cur[1]['sidecar'])[1]})"})
    out["take"] = [t for _r, t in best.values()]
    return out


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# ------------------------------------------------------------- doing it -----
def _probe_langs(path: str) -> set:
    """Read the subtitle languages out of a file on disk, now."""
    from .jobs import _ffprobe_exe
    try:
        out = subprocess.run(
            [_ffprobe_exe(), "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "s", path],
            capture_output=True, text=True, timeout=120,
            creationflags=NO_WINDOW, startupinfo=hidden_si()).stdout
        got = set()
        for s in (json.loads(out or "{}").get("streams") or []):
            got.add(_lang_key((s.get("tags") or {}).get("language") or "und"))
        return got
    except Exception:                                            # noqa: BLE001
        return set()


def _note(file_id: int, path: str, side: str, lang: str, ok: bool,
          detail: str) -> None:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO subembed_log(at,file_id,path,sidecar,lang,ok,"
                "detail) VALUES(?,?,?,?,?,?,?)",
                (time.time(), int(file_id or 0), path, side, lang,
                 1 if ok else 0, (detail or "")[:500]))
    except Exception:                                            # noqa: BLE001
        pass


def embed_one(file_id: int) -> dict:
    r"""Remux this file's eligible sidecars into it, then recycle them.

    ONE mkvmerge FOR ALL OF THEM. Two sidecars means two subtitle tracks and
    one rewrite, not two rewrites - a second pass would copy a file that has
    just been copied, for no reason.
    """
    from . import fileops
    p = plan_one(int(file_id))
    if not p.get("ok") or not p.get("take"):
        return {**p, "embedded": 0}
    path = p["path"]
    if not os.path.exists(path):
        return {"ok": False, "why": "the file is not on disk"}
    if os.path.splitext(path)[1].lower() != ".mkv":
        return {"ok": False, "why": "only Matroska can carry these tracks; "
                                    "this file is not .mkv"}
    if fileops.is_locked(path):
        return {"ok": False, "why": "the file is in use"}
    if not have_mkvmerge():
        return {"ok": False, "why": "mkvmerge is not installed - see "
                                    "Settings, MKVToolNix"}
    tmp = os.path.join(os.path.dirname(path),
                       f".nuarr-embed-{int(time.time())}.mkv")
    cmd = [_mkvmerge(), "-o", tmp, path]
    for t in p["take"]:
        # The language goes on the TRACK, not just in the filename it came
        # from - a track tagged und is a track the planner will treat as
        # untagged forever after.
        cmd += ["--language", f"0:{t['lang']}"]
        if "forced" in (t.get("role") or ""):
            cmd += ["--forced-track", "0:yes"]
        cmd += [t["sidecar"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                           creationflags=NO_WINDOW, startupinfo=hidden_si())
    except Exception as e:                                       # noqa: BLE001
        fileops._quiet_remove(tmp)
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    # mkvmerge returns 1 for warnings and still writes a good file; 2 is a
    # real failure. Treated the way MKVToolNix documents it rather than as a
    # plain non-zero check, which would throw away every file with a warning.
    if r.returncode >= 2 or not os.path.exists(tmp):
        fileops._quiet_remove(tmp)
        why = (r.stderr or r.stdout or "mkvmerge failed").strip()[:300]
        _note(file_id, path, ";".join(t["sidecar"] for t in p["take"]), "",
              False, why)
        return {"ok": False, "why": why}

    # EXIT 0 IS NOT PROOF. Read the result back and require every language to
    # actually be in it before anything is removed or replaced.
    got = _probe_langs(tmp)
    missing = [t["lang"] for t in p["take"]
               if _lang_key(t["lang"]) not in got]
    if missing:
        fileops._quiet_remove(tmp)
        why = (f"the rebuilt file does not contain "
               f"{', '.join(missing)} - nothing was changed")
        _note(file_id, path, "", ",".join(missing), False, why)
        return {"ok": False, "why": why}

    res = fileops.safe_replace(path, tmp)
    if not getattr(res, "ok", False):
        fileops._quiet_remove(tmp)
        why = f"could not put the rebuilt file in place: {getattr(res, 'why', '')}"
        _note(file_id, path, "", "", False, why)
        return {"ok": False, "why": why}

    # AND ONLY NOW THE SIDECARS, one at a time, recycled rather than deleted.
    gone, kept = 0, []
    for t in p["take"]:
        rr = fileops.recycle(t["sidecar"])
        if getattr(rr, "ok", False):
            gone += 1
        else:
            kept.append(os.path.basename(t["sidecar"]))
        _note(file_id, path, t["sidecar"], t["lang"], True,
              f"embedded as {t['lang']}"
              + ("" if getattr(rr, "ok", False) else " (sidecar left in place)"))
    joblog.log(f"embedded {len(p['take'])} sidecar subtitle(s) into "
               f"{os.path.basename(path)} and recycled {gone}", "info")
    return {"ok": True, "embedded": len(p["take"]), "recycled": gone,
            "kept": kept, "path": path}


# ------------------------------------------------------------- the sweep ----
def candidates(limit: int = 200, force: bool = False) -> list[dict]:
    """Files with a sidecar worth taking.

    `force` widens it to every library rather than the enabled ones, which is
    what the preview needs: the question "what would this do" has to be
    answerable before the switch is thrown, not after.
    """
    if not _READY:
        init()
    libs = [l.name for l in (SETTINGS.libraries or [])
            if force or enabled(l.name)]
    if not libs:
        return []
    qs = ",".join("?" * len(libs))
    cutoff = time.time() - SETTLE_S
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            f"SELECT id, path, library FROM files "
            f" WHERE library IN ({qs}) "
            f"   AND state NOT IN ('deleted','duplicate') "
            f"   AND COALESCE(path,'') != '' "
            f"   AND COALESCE(mtime,0) < ? "
            f" ORDER BY id LIMIT 20000", libs + [cutoff])]
    out = []
    for r in rows:
        if len(out) >= limit:
            break
        if not sidecars_for(r["path"]):
            continue                       # cheap listdir, no probe, no policy
        p = plan_one(r["id"], force=force)
        if p.get("take"):
            out.append(p)
    return out


async def sweep(limit: int = 0) -> dict:
    if STATE["running"]:
        return {"ok": False, "why": "already running"}
    todo = await asyncio.to_thread(candidates, int(limit or PER_RUN))
    STATE.update(running=True, done=0, total=len(todo), now="",
                 embedded=0, recycled=0)
    done = emb = rec = 0
    try:
        for p in todo:
            STATE["now"] = os.path.basename(p["path"])[:60]
            STATE["done"] = done
            out = await asyncio.to_thread(embed_one, p["file_id"])
            done += 1
            if out.get("ok"):
                emb += int(out.get("embedded") or 0)
                rec += int(out.get("recycled") or 0)
                STATE.update(embedded=emb, recycled=rec)
    finally:
        STATE.update(running=False, now="", last_run=time.time(),
                     done=done, embedded=emb, recycled=rec)
    if emb:
        joblog.log(f"sidecar embed: {emb} subtitle(s) taken into "
                   f"{done} file(s), {rec} sidecar(s) recycled", "info")
    return {"ok": True, "files": done, "embedded": emb, "recycled": rec}


def stats() -> dict:
    if not _READY:
        init()
    libs = {l.name: enabled(l.name) for l in (SETTINGS.libraries or [])}
    out = {"libraries": libs, "on": any(libs.values()),
           "have_mkvmerge": have_mkvmerge(), "running": STATE["running"],
           "now": STATE["now"], "done": STATE["done"], "total": STATE["total"],
           "last_run": STATE["last_run"], "embedded": 0, "recycled": 0,
           "failed": 0}
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT SUM(ok) e, SUM(1-ok) f, COUNT(*) n "
                "FROM subembed_log").fetchone()
            out["embedded"] = int((r["e"] if r else 0) or 0)
            out["failed"] = int((r["f"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        pass
    return out


def preview(limit: int = 25, force: bool = True) -> dict:
    """What this would do, without doing any of it. Works while the rule is off."""
    got = candidates(int(limit), force=force)
    return {"ok": True, "files": [
        {"file_id": p["file_id"], "path": p["path"], "library": p["library"],
         "on": enabled(p["library"]),
         "take": p["take"], "skip": p["skip"][:4]} for p in got]}


async def watch() -> None:
    await asyncio.sleep(180)
    while True:
        try:
            if any(enabled(l.name) for l in (SETTINGS.libraries or [])):
                await sweep()
        except Exception as e:                                   # noqa: BLE001
            STATE["last_error"] = f"{type(e).__name__}: {e}"
        await asyncio.sleep(CYCLE_S)
