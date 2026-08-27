"""
nuarr - Sonarr/Radarr webhook receiver

WHY
---
A full scan takes ~174 s and walks 39,000 files across 12 disks. Running it
often enough to notice an import is wasteful; running it rarely means nuarr is
minutes-to-hours stale, and stale state is what causes work on a file that has
already moved.

Webhooks close that gap: the arr tells us the moment something changes, and we
update exactly the affected rows, keyed on the durable arr file id. The periodic
scan stays as the reconciliation backstop for anything missed (a webhook lost
while nuarr was down, a DrivePool balancer move, an out-of-band edit).

TRUST MODEL
-----------
The payload is used only for IDS. Paths, sizes and quality are re-fetched from
the arr's API, because the payload is a snapshot from before the import finished
settling - and because trusting a body posted to an open HTTP port to tell us
where files live is not something worth doing.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request

from . import joblog, scanner
from .arr import ArrClient
from . import schedules
from .config import SETTINGS
from .db import cursor, kv_get, kv_set, log_event

router = APIRouter()

# last N events, for the dashboard
RECENT: list[dict] = []
MAX_RECENT = 60


def webhook_token() -> str:
    """Shared secret, generated once and kept in the DB.

    The server binds 0.0.0.0 so the port is reachable from the LAN. Without a
    token anything on the network could post fabricated file events and steer
    what nuarr does to real media.
    """
    tok = kv_get("webhook_token")
    if not tok:
        tok = secrets.token_urlsafe(24)
        kv_set("webhook_token", tok)
    return tok


def _note(arr: str, event: str, detail: str, ok: bool = True) -> None:
    RECENT.insert(0, {"arr": arr, "event": event, "detail": detail,
                      "ok": ok, "at": time.time()})
    del RECENT[MAX_RECENT:]


def _cfg(name: str):
    return next((c for c in SETTINGS.arrs if c.name.lower() == name.lower()), None)


def _sz(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 1024**3:.2f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


def _obj(x) -> dict:
    """A dict, whatever the arr actually sent.

    Every 'str' object has no attribute 'get' crash has been the same root
    cause: an arr field documented as an object arriving as a bare string.
    I fixed those one field at a time - deletedFiles, then movieFile - and each
    time another surfaced. Coercing at every access point ends the whack-a-mole.
    """
    return x if isinstance(x, dict) else {}


def _objs(x) -> list[dict]:
    """Only the dict members of a list field."""
    return [i for i in (x or []) if isinstance(i, dict)] if isinstance(x, list) else []


def _langs(raw) -> list[str]:
    r"""Audio languages as a short, de-duplicated, order-preserving list.

    Sonarr and Radarr report these slash-separated - 'jpn/eng', sometimes with
    'und' for an untagged track and repeats when several tracks share a
    language. Collapsing to unique codes in first-appearance order keeps the
    detail readable ('jpn+eng', not 'jpn/eng/eng/und') while preserving track
    order, which is what the audio policy is written against: original language
    first, English second.
    """
    if not raw or not isinstance(raw, str):
        return []
    out: list[str] = []
    for part in raw.replace(",", "/").split("/"):
        code = part.strip().lower()
        if not code or code in ("und", "unknown", "null"):
            continue
        if code not in out:
            out.append(code)
    return out[:4]          # a 10-language remux would swamp the row


def _describe(f) -> str:
    """'WEBDL-1080p x264 EAC3 5.1 · 2.14 GB' from an arr file object.

    Sonarr sends deletedFiles as a list of STRINGS on some events and a list of
    objects on others, which crashed every Download webhook with
    "'str' object has no attribute 'get'". Take whatever shape turns up.

    NOTE on the idiom that kept failing here:

        (f.get("quality") or {}).get("quality")

    This looks defensive and is not. `or {}` substitutes only for FALSY values -
    None, "", {}. A non-empty string is truthy, so a string quality passes
    straight through and the next .get() raises. Neither `or {}` nor
    .get(k, {}) can guard against a wrong TYPE; only isinstance can, which is
    what _obj() does. This exact line was the surviving crash.
    """
    if isinstance(f, str):
        return os.path.basename(f) or f
    if not isinstance(f, dict):
        return str(f)
    q = _obj(_obj(f.get("quality")).get("quality")).get("name")
    # a plain string quality is still useful information - show it
    if not q:
        raw = f.get("quality")
        raw = raw.get("quality") if isinstance(raw, dict) else raw
        q = raw if isinstance(raw, str) and raw else "?"
    mi = _obj(f.get("mediaInfo"))
    bits = [q]
    for k in ("videoCodec", "audioCodec"):
        v = mi.get(k)
        if v:
            bits.append(str(v))
    ch = mi.get("audioChannels")
    if ch:
        bits.append(str(ch))
    # AUDIO LANGUAGES. On an anime library this is often the whole point of an
    # upgrade - going from jpn-only to jpn+eng is the difference between a file
    # that works and one that does not, and the codec/size columns cannot show
    # it. Sonarr gives them slash-separated ('jpn/eng'); normalise to the same
    # ISO-639-2 codes the filenames use so the two read alike.
    langs = _langs(mi.get("audioLanguages"))
    if langs:
        bits.append("[" + "+".join(langs) + "]")
    return " ".join(bits) + " · " + _sz(f.get("size"))


def _media_label(body: dict, kind: str) -> str:
    """'The Boys S04E05' / 'Promare (2019)' - WHO the event happened to.

    The Live-arr-events panel showed 'upgrade: 1 file(s)' and 'file deleted'
    with no name attached, which reads as activity but answers nothing. Every
    payload already carries the series/movie object; this puts it in front.
    """
    if kind == "sonarr":
        t = _obj(body.get("series")).get("title") or ""
        eps = _objs(body.get("episodes"))
        if eps:
            try:
                e = eps[0]
                se = (f"S{int(e.get('seasonNumber') or 0):02d}"
                      f"E{int(e.get('episodeNumber') or 0):02d}")
                if len(eps) > 1:
                    se += f"-E{int(eps[-1].get('episodeNumber') or 0):02d}"
                return f"{t} {se}".strip()
            except (TypeError, ValueError):
                pass
        return t or "?"
    m = _obj(body.get("movie"))
    t = m.get("title") or "?"
    y = m.get("year")
    return f"{t} ({y})" if y else t


def _probe_langs(cur, file_id: int) -> list[str]:
    """Audio languages nuarr itself measured, from the stored probe.

    The OLD side of an upgrade comes from here rather than from the webhook,
    because a payload's deletedFiles entry carries no mediaInfo at all - which
    is why the language note never appeared. Our probe of the file we are
    about to lose is the last authoritative record of what it held.

    Takes the caller's cursor: opening a second one inside an open
    transaction is how this module would deadlock itself.
    """
    try:
        r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                        (file_id,)).fetchone()
        if not r or not r["json"]:
            return []
        import json as _json
        from . import rules
        out: list[str] = []
        for s in (_json.loads(r["json"]).get("streams") or []):
            if s.get("codec_type") != "audio":
                continue
            # _lang_guess, not the raw tag: dual-audio releases routinely ship
            # audio with NO language tag and the language in the track title
            # ("Eng", "Jap") - the same shape that made the dedupe rule delete
            # Japanese tracks. Reading the tag alone returns nothing here and
            # the note never appears, which is exactly what it did.
            lg = rules._lang_guess(s)
            if lg and lg != "und" and lg not in out:
                out.append(lg)
        return out
    except Exception:
        return []


def _lang_note(before: list[str], after: list[str]) -> str:
    """'audio jpn -> jpn+eng (gained eng)', or '' when nothing moved."""
    if not before or not after or before == after:
        return ""
    gained = [l for l in after if l not in before]
    lost = [l for l in before if l not in after]
    note = f"audio {'+'.join(before)} -> {'+'.join(after)}"
    if gained:
        note += f" (gained {'+'.join(gained)})"
    if lost:
        note += f" (lost {'+'.join(lost)})"
    return note


def _upgrade_detail(body: dict, new_files: list[dict]) -> str:
    """What actually changed in an upgrade, not just the word 'upgrade'.

    Recent activity showed every import as a bare 'upgrade', which tells you
    nothing about WHY the arr replaced the file. The payload already carries the
    old file(s) and the new one, so state the difference.
    """
    # A bare string here is one path, not a sequence of characters - slicing it
    # produced detail lines like "a / " from the first two letters.
    raw = body.get("deletedFiles") or []
    raw = [raw] if isinstance(raw, str) else raw
    old = [o for o in raw if o] if isinstance(raw, list) else []
    if not old or not new_files:
        return "upgrade"
    line = (" / ".join(_describe(o) for o in old[:2])
            + "  ->  " + " / ".join(_describe(n) for n in new_files[:2]))

    # Call the audio-language change out separately. Buried at the end of two
    # long spec strings it is easy to miss, and on an anime library it is often
    # the whole reason for the upgrade - jpn-only becoming jpn+eng is exactly
    # what the dual-audio policy is chasing.
    before, after = [], []
    for o in old[:2]:
        if isinstance(o, dict):
            before += [l for l in _langs(_obj(o.get("mediaInfo")).get("audioLanguages"))
                       if l not in before]
    for n in new_files[:2]:
        if isinstance(n, dict):
            after += [l for l in _langs(_obj(n.get("mediaInfo")).get("audioLanguages"))
                      if l not in after]
    note = _lang_note(before, after)
    if note:
        line += " · " + note
    return line


# --------------------------------------------------------------- upserts ----
async def _sync_file(cfg, file_id: int, parent_id: int | None, why: str,
                     client: "ArrClient | None" = None,
                     title_cache: dict | None = None,
                     event: str = "imported") -> None:
    """Re-read one file from the arr and write it to the DB by durable id.

    `client` and `title_cache` let a caller syncing many files under one parent
    share both. Without them this opened and tore down its own ArrClient - and
    re-asked the arr for the SAME series title - once per file. A 200-episode
    series rename therefore cost 200 HTTP clients and 200 identical title
    lookups on top of the work that actually had to happen.
    """
    own = client is None
    client = client or ArrClient(cfg)
    try:
        rec = _obj(await client.file_record(file_id))
        if not rec:
            return
        path = rec.get("path") or ""
        if not path:
            return
        # An arr can be told about a file nuarr is configured to ignore. Drop it
        # here or the webhook would re-insert rows the scanner just excluded.
        if scanner.is_excluded(path):
            joblog.log(f"webhook ignored (excluded path): {path}", "debug")
            return
        size = rec.get("size") or 0
        # Coerce by TYPE at each level. Neither .get(k, {}) nor `or {}` is a
        # guard here: the first only substitutes when the key is missing, the
        # second only when the value is falsy. A string quality defeats both.
        quality = _obj(_obj(rec.get("quality")).get("quality")).get("name")
        parent = parent_id or rec.get("seriesId") or rec.get("movieId")
        disk = scanner.disk_of(path)
        lib = scanner._library_of(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        now = time.time()

        title = None
        season = rec.get("seasonNumber")
        # Episode number was never captured here, so every webhook-imported file
        # showed as "Show - S02" with no episode - useless when 20 episodes of a
        # season import at once. Sonarr's episodefile has no episodes array, so
        # read it off the relative path exactly as the scanner does.
        from .arr import _episodes_from_relpath
        episode = _episodes_from_relpath(rec.get("relativePath") or path)
        if parent:
            if title_cache is not None and parent in title_cache:
                title = title_cache[parent]
            else:
                title = await client.parent_title(parent)
                if title_cache is not None:
                    title_cache[parent] = title

        # THE ARR'S OWN mediaInfo, not the webhook's. file_record() returns the
        # full episodefile/moviefile, which carries audioLanguages ('eng/jpn');
        # the webhook payload's file entries do not carry mediaInfo at all.
        new_langs = _langs(_obj(rec.get("mediaInfo")).get("audioLanguages"))

        with cursor() as cur:
            row = cur.execute(
                "SELECT id, path, size, pool_disk FROM files "
                "WHERE arr_name=? AND arr_file_id=?", (cfg.name, file_id)
            ).fetchone()
            # WHY THE UPGRADE HAPPENED, when the reason is language. On an
            # anime library "jpn -> jpn+eng" is frequently the entire point of
            # a grab, and the quality/size columns cannot show it: both sides
            # read WEBDL-1080p x264 AAC 2.0 · 1.37 GB and the row looked like
            # a pointless replacement of a file by itself.
            if row is not None and new_langs:
                lang_note = _lang_note(_probe_langs(cur, row["id"]), new_langs)
                if lang_note:
                    why = f"{why} · {lang_note}" if why else lang_note

            if row is None:
                # SELECT-then-INSERT is a race: a scan (or a second webhook for
                # the same file) can create this row in the gap between the two
                # statements. ON CONFLICT makes the write safe either way -
                # without it, whichever side loses raises
                # "UNIQUE constraint failed: files.arr_name, files.arr_file_id".
                cur.execute(
                    "INSERT INTO files(arr_name,arr_file_id,arr_parent_id,path,"
                    "library,title,season,episode,size,mtime,pool_disk,state,"
                    "state_reason,first_seen,last_seen,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(arr_name,arr_file_id) "
                    "WHERE arr_file_id IS NOT NULL DO UPDATE SET "
                    "arr_parent_id=excluded.arr_parent_id, path=excluded.path, "
                    "library=excluded.library, title=excluded.title, "
                    "season=excluded.season, episode=excluded.episode, "
                    "size=excluded.size, mtime=excluded.mtime, "
                    "pool_disk=excluded.pool_disk, last_seen=excluded.last_seen, "
                    "updated_at=excluded.updated_at",
                    (cfg.name, file_id, parent, path, lib, title, season, episode,
                     size, mtime, disk, "new", why, now, now, now))
                new_id = cur.lastrowid
                cur.execute("INSERT INTO history(file_id,event,detail,at) "
                            "VALUES(?,?,?,?)", (new_id, event, why, now))
                return

            # A rename is an UPDATE to a mutable column - the row keeps its
            # identity and every bit of processing state with it.
            if os.path.normcase(row["path"] or "") != os.path.normcase(path):
                cur.execute("INSERT INTO history(file_id,event,detail,at) "
                            "VALUES(?,?,?,?)",
                            (row["id"], "renamed", f"{row['path']} -> {path}", now))

            changed = row["size"] is not None and size and row["size"] != size
            state = "new" if changed else None
            if changed:
                cur.execute("INSERT INTO history(file_id,event,detail,at) "
                            "VALUES(?,?,?,?)",
                            (row["id"], "content_changed",
                             f"{row['size']} -> {size} bytes ({why})", now))

            if state:
                cur.execute(
                    "UPDATE files SET arr_parent_id=?,path=?,library=?,title=?,"
                    "season=?,episode=?,size=?,mtime=?,pool_disk=?,state=?,"
                    "state_reason=?,last_seen=?,updated_at=? WHERE id=?",
                    (parent, path, lib, title, season, episode, size, mtime, disk,
                     state, why, now, now, row["id"]))
            else:
                cur.execute(
                    "UPDATE files SET arr_parent_id=?,path=?,library=?,title=?,"
                    "season=?,episode=?,size=?,mtime=?,pool_disk=?,last_seen=?,"
                    "updated_at=? WHERE id=?",
                    (parent, path, lib, title, season, episode, size, mtime, disk,
                     now, now, row["id"]))
    finally:
        if own:
            await client.close()


async def _sync_parent(cfg, parent_id: int, why: str) -> int:
    """Resync every file under one series/movie - used after a Rename.

    ONE client for the whole parent, and one title lookup shared across its
    files. This used to close its client before the per-file loop and then let
    each _sync_file open another, so renaming a long-running series opened
    hundreds of connections in sequence and asked for the same title every time.
    """
    client = ArrClient(cfg)
    try:
        ep = "/episodefile" if cfg.kind == "sonarr" else "/moviefile"
        key = "seriesId" if cfg.kind == "sonarr" else "movieId"
        try:
            rows = await client._get(ep, **{key: parent_id})
        except Exception:
            return 0

        ids = [r["id"] for r in rows if r.get("id")]
        titles: dict = {}
        for fid in ids:
            await _sync_file(cfg, fid, parent_id, why,
                             client=client, title_cache=titles)
        return len(ids)
    finally:
        await client.close()


def _mark_deleted(cfg_name: str, file_id: int, reason: str) -> None:
    now = time.time()
    # AN UPGRADE IS NOT A DELETION, however the arr spells it. Every reason
    # that reaches here from an upgrade says so ("replaced by upgrade: ...",
    # deleteReason 'upgrade', 'EpisodeFileDeleteForUpgrade') - and recording
    # it as 'deleted' made the activity feed read like data loss when it was
    # the library getting better. The file row still goes to state='deleted'
    # (the bytes ARE gone); only the story told about it changes.
    ev = "upgraded" if "upgrade" in (reason or "").lower() else "deleted"
    with cursor() as cur:
        row = cur.execute("SELECT id FROM files WHERE arr_name=? AND arr_file_id=?",
                          (cfg_name, file_id)).fetchone()
        if not row:
            return
        # Keep the row. Deleting it would cascade its history away, and on an
        # upgrade we want the audit trail of what replaced what.
        cur.execute("UPDATE files SET state='deleted', state_reason=?, updated_at=? "
                    "WHERE id=?", (reason, now, row["id"]))
        cur.execute("INSERT INTO history(file_id,event,detail,at) VALUES(?,?,?,?)",
                    (row["id"], ev, reason, now))


# ---------------------------------------------------------------- routing ---
async def _handle(cfg, body: dict) -> str:
    ev = body.get("eventType") or ""
    kind = cfg.kind

    if ev in ("Test", "Health", "HealthIssue", "HealthRestored",
              "ApplicationUpdate", "Grab"):
        return f"{ev} acknowledged"

    if kind == "sonarr":
        pid = _obj(body.get("series")).get("id")

        if ev in ("Download", "ImportComplete"):
            files = _objs(body.get("episodeFiles"))
            ef = _obj(body.get("episodeFile"))
            if ef:
                files = files + [ef]
            up = (_upgrade_detail(body, files) if body.get("isUpgrade")
                  else f"import · {_describe(files[0])}" if files else "import")
            # The new file of an upgrade is an 'upgraded' event, not another
            # 'imported' - it is the side that carries the old -> new detail,
            # and the feed's pill should say what actually happened.
            evname = "upgraded" if body.get("isUpgrade") else "imported"
            for f in files:
                if f.get("id"):
                    await _sync_file(cfg, f["id"], pid, up, event=evname)
            for d in _objs(body.get("deletedFiles")):
                if d.get("id"):
                    _mark_deleted(cfg.name, d["id"],
                                  f"replaced by upgrade: {_describe(d)}")
            # The rich detail was always computed - it just went only into
            # per-file history while the panel got a bare count. Same string,
            # both places.
            extra = f" ({len(files)} files)" if len(files) > 1 else ""
            return f"{_media_label(body, 'sonarr')} — {up}{extra}"

        if ev == "Rename":
            # renamedEpisodeFiles carries plain strings on some Sonarr versions;
            # if none survive the dict filter, fall back to resyncing the series
            # rather than silently renaming nothing.
            renamed = _objs(body.get("renamedEpisodeFiles"))
            if renamed:
                for f in renamed:
                    if f.get("id"):
                        await _sync_file(cfg, f["id"], pid, "renamed")
                return f"{_media_label(body, 'sonarr')} — renamed {len(renamed)} file(s)"
            n = await _sync_parent(cfg, pid, "renamed") if pid else 0
            return f"{_media_label(body, 'sonarr')} — renamed, resynced {n} file(s)"

        if ev in ("EpisodeFileDelete", "EpisodeFileDeleteForUpgrade"):
            ef = _obj(body.get("episodeFile"))
            why = body.get("deleteReason") or ev
            if ef.get("id"):
                _mark_deleted(cfg.name, ef["id"], why)
            return (f"{_media_label(body, 'sonarr')} — deleted "
                    f"{_describe(ef)} ({why})")

        if ev == "SeriesDelete":
            if pid:
                with cursor() as cur:
                    cur.execute("UPDATE files SET state='deleted', "
                                "state_reason='series deleted', updated_at=? "
                                "WHERE arr_name=? AND arr_parent_id=?",
                                (time.time(), cfg.name, pid))
            return f"series deleted — {_media_label(body, 'sonarr')}"

    else:  # radarr
        pid = _obj(body.get("movie")).get("id")

        if ev == "Download":
            mf = _obj(body.get("movieFile"))
            up = (_upgrade_detail(body, [mf] if mf else []) if body.get("isUpgrade")
                  else f"import · {_describe(mf)}" if mf else "import")
            if mf.get("id"):
                await _sync_file(cfg, mf["id"], pid, up,
                                 event="upgraded" if body.get("isUpgrade")
                                 else "imported")
            for d in _objs(body.get("deletedFiles")):
                if d.get("id"):
                    _mark_deleted(cfg.name, d["id"],
                                  f"replaced by upgrade: {_describe(d)}")
            return f"{_media_label(body, 'radarr')} — {up}"

        if ev == "Rename":
            # This was the live crash: Radarr sends renamedMovieFiles as a list
            # of strings, so f.get("id") blew up before any rename was recorded.
            renamed = _objs(body.get("renamedMovieFiles"))
            if renamed:
                for f in renamed:
                    if f.get("id"):
                        await _sync_file(cfg, f["id"], pid, "renamed")
                return f"{_media_label(body, 'radarr')} — renamed {len(renamed)} file(s)"
            n = await _sync_parent(cfg, pid, "renamed") if pid else 0
            return f"{_media_label(body, 'radarr')} — renamed, resynced {n} file(s)"

        if ev in ("MovieFileDelete", "MovieFileDeleteForUpgrade"):
            mf = _obj(body.get("movieFile"))
            why = body.get("deleteReason") or ev
            if mf.get("id"):
                _mark_deleted(cfg.name, mf["id"], why)
            return (f"{_media_label(body, 'radarr')} — deleted "
                    f"{_describe(mf)} ({why})")

        if ev == "MovieDelete":
            if pid:
                with cursor() as cur:
                    cur.execute("UPDATE files SET state='deleted', "
                                "state_reason='movie deleted', updated_at=? "
                                "WHERE arr_name=? AND arr_parent_id=?",
                                (time.time(), cfg.name, pid))
            return f"movie deleted — {_media_label(body, 'radarr')}"

    return f"ignored {ev}"


@router.post("/api/webhook/{arr_name}")
async def receive(arr_name: str, request: Request, token: str = ""):
    cfg = _cfg(arr_name)
    if not cfg:
        raise HTTPException(404, f"unknown arr {arr_name}")
    if not secrets.compare_digest(token, webhook_token()):
        raise HTTPException(403, "bad or missing token")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected JSON")

    ev = body.get("eventType") or "?"

    # Answer immediately. The arr treats a slow webhook as a failure, and a
    # Rename event can mean resyncing hundreds of files.
    async def work():
        try:
            detail = await _handle(cfg, body)
            _note(cfg.name, ev, detail)
        except Exception as e:
            # A bare "AttributeError: 'str' object has no attribute 'get'" told
            # me nothing about WHICH field was the wrong shape. Log the
            # traceback and the payload so the next one is diagnosable.
            #
            # This block previously called joblog without importing it, so it
            # raised NameError inside the handler: the UI showed the failure
            # while nuarr.log stayed empty, and the diagnostics meant to explain
            # the bug were the reason there were none. Logging must never be
            # able to mask the error it is reporting, hence the inner guard.
            import traceback
            tb = traceback.format_exc()
            _note(cfg.name, ev, f"{type(e).__name__}: {e}", ok=False)
            try:
                joblog.log(f"webhook {cfg.name}/{ev} FAILED: "
                           f"{type(e).__name__}: {e}", "error")
                joblog.log("  payload keys: " + ", ".join(sorted(body.keys())),
                           "debug")
                # the offending value, not just the key name
                import json
                for k, v in body.items():
                    if not isinstance(v, (dict, list)):
                        continue
                    joblog.log(f"  {k}: {json.dumps(v, default=str)[:300]}", "debug")
                for ln in tb.strip().splitlines()[-8:]:
                    joblog.log("  " + ln, "debug")
            except Exception as log_err:
                print(f"[nuarr] webhook {ev} failed: {e}\n{tb}\n"
                      f"[nuarr] logging also failed: {log_err}", flush=True)

    asyncio.create_task(work())
    return {"accepted": True, "event": ev}


@router.get("/api/webhook/recent")
def recent():
    return {"rows": RECENT, "token_set": bool(kv_get("webhook_token"))}


# ----------------------------------------------------------- registration ---
async def register(cfg, base_url: str) -> dict:
    """Create or update the 'nuarr' Webhook notification inside the arr."""
    client = ArrClient(cfg)
    try:
        schema = await client._get("/notification/schema")
        wh = next((s for s in schema if s.get("implementation") == "Webhook"), None)
        if not wh:
            return {"ok": False, "error": "no Webhook implementation in schema"}

        payload = dict(wh)
        payload["name"] = "nuarr"
        # Enable every event this arr supports that tells us a FILE changed.
        wanted = ["onDownload", "onUpgrade", "onImportComplete", "onRename",
                  "onEpisodeFileDelete", "onEpisodeFileDeleteForUpgrade",
                  "onMovieFileDelete", "onMovieFileDeleteForUpgrade",
                  "onSeriesDelete", "onMovieDelete"]
        for k in wanted:
            if payload.get("supports" + k[2:]) is True or k in payload:
                if payload.get("supports" + k[2:]) is not False:
                    payload[k] = True
        payload["onGrab"] = False
        payload["onHealthIssue"] = False
        payload["onApplicationUpdate"] = False

        url = f"{base_url.rstrip('/')}/api/webhook/{cfg.name}?token={webhook_token()}"
        for f in payload.get("fields", []):
            if f.get("name") == "url":
                f["value"] = url
            elif f.get("name") == "method":
                f["value"] = f.get("value") or 1      # 1 = POST

        existing = await client._get("/notification")
        mine = next((n for n in existing if n.get("name") == "nuarr"), None)
        if mine:
            payload["id"] = mine["id"]
            r = await client._client.put(f"/notification/{mine['id']}", json=payload)
        else:
            r = await client._client.post("/notification", json=payload)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "error": r.text[:400]}
        return {"ok": True, "url": url, "updated": bool(mine)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await client.close()


# NOTE: this path must NOT live under /api/webhook/, because the receiver route
# is /api/webhook/{arr_name} and FastAPI would match "register" as an arr name -
# which it did, returning 404 "unknown arr register".
@router.post("/api/webhooks/register")
async def api_register(base_url: str = ""):
    base = base_url or default_base_url()
    kv_set("webhook_base_url", base)
    out = {}
    for cfg in SETTINGS.arrs:
        if cfg.enabled and cfg.api_key:
            out[cfg.name] = await register(cfg, base)
            STATUS[cfg.name] = {"ok": out[cfg.name].get("ok"), "at": time.time(),
                                "detail": out[cfg.name].get("error") or "registered",
                                "url": out[cfg.name].get("url")}
    return out


# ------------------------------------------------------- auto-registration ---
# Registration drifts: the arr's notification can be edited or deleted, the
# token is regenerated if the DB is rebuilt, and nuarr's own host/port can move.
# When that happens events stop arriving SILENTLY - the panel simply goes quiet,
# which looks identical to "nothing has imported lately". So verify on a timer
# and repair rather than trusting a one-time click.
WATCH_INTERVAL_S = 900.0        # 15 minutes
STATUS: dict[str, dict] = {}


def default_base_url() -> str:
    """How the arr should reach nuarr.

    Prefers whatever was used last, then an explicit setting, then the LAN IP.
    localhost is the last resort: it only works when the arr runs on this same
    machine, and silently fails when it does not.
    """
    saved = kv_get("webhook_base_url")
    if saved:
        return saved
    configured = getattr(SETTINGS, "base_url", "") or ""
    if configured:
        return configured
    port = getattr(SETTINGS, "port", 8770) or 8770
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no traffic sent, just picks the route
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{port}"
    except Exception:
        return f"http://localhost:{port}"


async def verify(cfg, base_url: str) -> dict:
    """Is our notification present, enabled, and pointed at the right URL?"""
    want_url = f"{base_url.rstrip('/')}/api/webhook/{cfg.name}?token={webhook_token()}"
    client = ArrClient(cfg)
    try:
        existing = await client._get("/notification")
        mine = next((n for n in existing
                     if isinstance(n, dict) and n.get("name") == "nuarr"), None)
        if not mine:
            return {"ok": False, "reason": "no nuarr notification in this arr",
                    "want_url": want_url}
        got = next((f.get("value") for f in (mine.get("fields") or [])
                    if isinstance(f, dict) and f.get("name") == "url"), None)
        if got != want_url:
            # Most often the token or the host changed. Report both so the log
            # says WHY it is being rewritten.
            return {"ok": False, "reason": "url/token mismatch",
                    "have_url": got, "want_url": want_url}
        # An import that changes a file must reach us; if onDownload is off the
        # notification exists but is useless.
        if mine.get("onDownload") is False:
            return {"ok": False, "reason": "onDownload disabled", "want_url": want_url}
        return {"ok": True, "url": got}
    except Exception as e:
        # Unreachable arr is NOT drift - do not rewrite config on a transient
        # network error, just report it.
        return {"ok": None, "reason": f"{type(e).__name__}: {e}"}
    finally:
        await client.close()


async def ensure_registered(force: bool = False) -> dict:
    """Verify every enabled arr and re-register the ones that have drifted."""
    base = default_base_url()
    out = {}
    for cfg in SETTINGS.arrs:
        if not (cfg.enabled and cfg.api_key):
            continue
        v = await verify(cfg, base)
        if v.get("ok") and not force:
            out[cfg.name] = {"ok": True, "action": "already correct"}
        elif v.get("ok") is None:
            # arr down - leave it alone and try again next cycle
            out[cfg.name] = {"ok": None, "action": "skipped", "detail": v["reason"]}
            joblog.log(f"webhook check: {cfg.name} unreachable - {v['reason']}",
                       "warn")
        else:
            joblog.log(f"webhook drift on {cfg.name}: {v.get('reason')} "
                       f"- re-registering", "warn")
            r = await register(cfg, base)
            out[cfg.name] = {"ok": r.get("ok"), "action": "re-registered",
                             "detail": r.get("error") or v.get("reason")}
            joblog.log(f"webhook re-register {cfg.name}: "
                       f"{'ok' if r.get('ok') else r.get('error')}",
                       "ok" if r.get("ok") else "error")
        STATUS[cfg.name] = {**out[cfg.name], "at": time.time(), "base_url": base}
    kv_set("webhook_base_url", base)
    return out


async def watch() -> None:
    """Background loop: register at startup, then re-check on a timer."""
    await asyncio.sleep(5)          # let the server finish binding first
    while True:
        schedules.beat('webhooks')
        try:
            await ensure_registered()
        except Exception as e:
            joblog.log(f"webhook watch failed: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(WATCH_INTERVAL_S)


@router.get("/api/webhooks/status")
def api_status():
    return {"arrs": STATUS, "base_url": default_base_url(),
            "interval_s": WATCH_INTERVAL_S}


@router.post("/api/webhooks/check")
async def api_check(force: bool = False):
    return await ensure_registered(force=force)
