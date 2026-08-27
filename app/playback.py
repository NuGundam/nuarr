r"""
nuarr - what Plex actually did when somebody pressed play

WHY THIS EXISTS
---------------
Every rule in rules.py is a claim: "after this, Plex will direct-play the
file". Nothing checked the claim. The EAE bug - high-bitrate E-AC3 that Plex's
audio decoder rejects outright - lived in this library for months and was
found by accident, from an analysis pasted in from somewhere else. Local checks
could not find it because ffmpeg decodes those tracks perfectly; only Plex
disagreed, and nothing here was listening to Plex.

So: listen. Plex publishes, per session, a decision for every individual
stream - `copy`, `transcode`, or `burn` - alongside the source and target
codec. That is not a guess about why a transcode happened, it is Plex naming
the offending track itself:

    Stream(type=2, decision=transcode, codec=eac3 -> aac)   audio codec
    Stream(type=3, decision=burn,      codec=hdmv_pgs)      picture subtitle
    Stream(type=1, decision=transcode, codec=hevc -> h264)  video codec

Each observation is attributed back to the library file by path, so the
question stops being "playback feels bad sometimes" and becomes "these eleven
files made Plex work, and here is which track did it".

WHAT IT DOES NOT DO
-------------------
It does not act. A file that transcoded once for a client with a tiny codec
whitelist is not a defect, and quietly re-encoding the library to satisfy one
old TV would be exactly the wrong response. This records evidence and shows it;
deciding what deserves a rule is a human judgement, and the panel exists to
inform it.

Client capability is the confounder and is recorded with every event, because
"Chromecast could not do HEVC" and "every client refuses this audio" look
identical until you can see who was watching.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from . import joblog
from .config import SETTINGS
from . import schedules
from .db import cursor

POLL_S = 20.0            # a session lasts minutes; this is plenty
_SEEN: dict[str, float] = {}     # session_key -> last recorded, dedupes polls
_SEEN_MAX = 500

# Plex stream types, from the API.
_V, _A, _S = 1, 2, 3
_KIND = {_V: "video", _A: "audio", _S: "subtitle"}

STATS: dict = {"last_poll": 0.0, "events": 0, "last_error": ""}


_READY = False


def init() -> None:
    """Idempotent, and called from poll_once as well as from watch().

    watch() sleeps before its first init so it does not compete with startup,
    which left a window where the API's "Check now" ran against a table that
    did not exist yet: a live Roku session transcoding AAC audio - exactly the
    kind of event this module is for - was swallowed as an OperationalError
    and reported as "recorded: 0".
    """
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS playback_events(
                id          INTEGER PRIMARY KEY,
                at          REAL NOT NULL,
                file_id     INTEGER,
                path        TEXT,
                title       TEXT,
                user        TEXT,
                client      TEXT,
                product     TEXT,
                decision    TEXT,        -- direct play | copy | transcode
                stream_kind TEXT,        -- video | audio | subtitle
                stream_act  TEXT,        -- copy | transcode | burn
                src_codec   TEXT,
                dst_codec   TEXT,
                detail      TEXT
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_pb_at "
                    "ON playback_events(at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_pb_file "
                    "ON playback_events(file_id)")
        # ONE ROW PER STREAM PER SESSION, NOT PER POLL.
        #
        # The docstring said "one row per session" and the code did not do it:
        # the watcher polls every 20s and wrote three fresh rows every time it
        # saw the same transcode still running. A two-hour stream therefore
        # logged itself about 360 times, the detail log showed the same client
        # and the same reason over and over at five-minute intervals, and the
        # headline count and the offender ranking were both counting polls
        # rather than viewings.
        have = {r[1] for r in cur.execute("PRAGMA table_info(playback_events)")}
        for col, decl in (("session", "TEXT"),
                          ("last_at", "REAL"),
                          ("hits", "INTEGER")):
            if col not in have:
                cur.execute(f"ALTER TABLE playback_events ADD COLUMN {col} {decl}")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_pb_sess "
                    "ON playback_events(session, stream_kind)")
    _READY = True


def _sessions() -> list[dict] | None:
    url, token = SETTINGS.plex_url, SETTINGS.plex_token
    if not url or not token:
        return None
    try:
        r = httpx.get(f"{url.rstrip('/')}/status/sessions",
                      headers={"X-Plex-Token": token,
                               "Accept": "application/json"}, timeout=10)
        r.raise_for_status()
        mc = (r.json() or {}).get("MediaContainer") or {}
        return mc.get("Metadata") or []
    except Exception as e:
        STATS["last_error"] = f"{type(e).__name__}"
        return None


def _file_row(path: str) -> tuple[int | None, str]:
    """-> (file_id, episode label). The label is what a person recognises.

    "Rick and Morty" is a series, not a thing you can go and look at: two rows
    reading the same show name and different numbers is a panel that has not
    answered the question. nuarr already stores season and episode, so use the
    same label the rest of the dashboard uses.
    """
    if not path:
        return None, ""
    from .db import display_label
    with cursor() as cur:
        r = cur.execute("SELECT id, title, season, episode FROM files "
                        "WHERE path=? COLLATE NOCASE", (path,)).fetchone()
        if not r:
            # Plex may report a path through a different mount than the
            # scanner walked. Fall back to the filename, unique in practice.
            r = cur.execute("SELECT id, title, season, episode FROM files "
                            "WHERE path LIKE ? ORDER BY LENGTH(path) LIMIT 1",
                            ("%" + os.path.basename(path),)).fetchone()
        if not r:
            return None, ""
        return r["id"], display_label(r["title"], r["season"], r["episode"])


def _src_height(fid) -> str:
    """The file's real vertical resolution, from nuarr's stored probe.

    Plex will not tell us: on a transcoding session every Media/Part/Stream
    block describes the OUTPUT. Without this, "Plex shrank the picture" could
    only say what it shrank TO, which is the less useful half.
    """
    if not fid:
        return ""
    try:
        with cursor() as cur:
            r = cur.execute("SELECT json FROM file_probes WHERE file_id=?",
                            (fid,)).fetchone()
        if not r:
            return ""
        import json as _j
        for s in _j.loads(r["json"]).get("streams", []):
            if s.get("codec_type") == "video" and s.get("height"):
                return f"{int(s['height'])}p"
    except Exception:
        pass
    return ""


def _record(sess: dict) -> int:
    """Write one row per stream Plex had to work on. Returns rows written."""
    ts = sess.get("TranscodeSession") or {}
    player = sess.get("Player") or {}
    user = (sess.get("User") or {}).get("title") or ""

    path = ""
    streams: list[dict] = []
    for m in (sess.get("Media") or []):
        for p in (m.get("Part") or []):
            if p.get("file") and not path:
                path = p["file"]
            streams.extend(p.get("Stream") or [])
    if not path and sess.get("ratingKey"):
        from .gate import _plex_file_for
        path = _plex_file_for(str(sess["ratingKey"]))

    if not ts:
        decision = "direct play"
    elif any(str(ts.get(k) or "").lower() == "transcode"
             for k in ("videoDecision", "audioDecision", "subtitleDecision")):
        decision = "transcode"
    else:
        decision = "copy"

    # A clean direct play is the expected outcome and there are thousands of
    # them. Recording every one would bury the interesting rows in noise, so
    # only work Plex had to do is written down.
    if decision == "direct play":
        return 0

    fid, title = _file_row(path)
    now = time.time()

    # THE SESSION'S CEILING, WHICH IS USUALLY THE REAL ANSWER.
    #
    # Measured on a live Roku session: Session.bandwidth 1229 kbps, location
    # "wan", source h264 -> output h264 at 718x404. Nothing about the file was
    # rejected; the stream was capped and everything was rebuilt smaller. Until
    # this was read, the panel blamed the codec and the audio channels, both of
    # which were identical on either side.
    sj = sess.get("Session") or {}
    bw = sj.get("bandwidth")
    loc = str(sj.get("location") or "").lower()
    cap = ""
    if bw:
        try:
            mbps = float(bw) / 1000.0
            where = ("over the internet" if loc == "wan" else
                     "on the local network" if loc == "lan" else "")
            cap = f"the client is capped at about {mbps:.1f} Mbps"
            if where:
                cap += f" {where}"
        except Exception:
            cap = ""
    if loc == "wan" and not cap:
        cap = "the client is playing remotely, so Plex applies a quality limit"

    # Output size, off the video stream Plex is producing. The SOURCE size is
    # not in the session at all - Media/Part describes the target - so it comes
    # from nuarr's own probe of the file.
    dst_wh = ""
    for st in streams:
        if int(st.get("streamType") or 0) == 1 and st.get("height"):
            dst_wh = f"{st.get('height')}p"
            break
    src_wh = _src_height(fid)
    if src_wh and dst_wh and src_wh == dst_wh:
        src_wh = dst_wh = ""              # no resize; do not imply one

    rows = []
    for st in streams:
        act = str(st.get("decision") or "").lower()
        if act in ("", "copy"):
            continue                       # this stream was fine
        kind = _KIND.get(int(st.get("streamType") or 0), "?")
        # THE STREAM BLOCK DESCRIBES PLEX'S OUTPUT, NOT THE FILE.
        #
        # On a transcoding session Media/Part/Stream carries the TARGET: a live
        # Roku session showed Stream(codec=aac, channels=2) for a file whose
        # audio is E-AC3 5.1, and reading it as the source produced the
        # nonsense line "aac was not accepted by this client, sent as aac".
        # The real source is on the TranscodeSession, as sourceAudioCodec /
        # sourceVideoCodec. Blaming the wrong codec would send someone looking
        # for a rule to fix a track that was never the problem.
        src = str(ts.get(f"source{kind.capitalize()}Codec") or "")
        dst = str(ts.get(f"{kind}Codec") or "")
        if not src:                        # subtitles, or an older Plex
            src = str(st.get("codec") or "")
        src_ch = st.get("channels") if kind == "audio" else None
        dst_ch = ts.get("audioChannels") if kind == "audio" else None
        detail = ""
        if kind == "subtitle" and act == "burn":
            detail = ("Plex painted this subtitle into the video on the CPU — "
                      "that alone forces a full re-encode")
        elif kind == "subtitle":
            detail = f"this subtitle was not used ({act})"
        elif kind == "audio":
            if src and dst and src != dst:
                detail = f"the client would not take {src}, so Plex sent {dst}"
            elif src_ch and dst_ch and int(src_ch) != int(dst_ch):
                detail = (f"the codec was fine; the client asked for "
                          f"{dst_ch} channel audio")
            else:
                # SAME CODEC, SAME CHANNELS, AND STILL RE-ENCODED.
                # Then nothing about the track was unacceptable - it is being
                # shrunk to fit a bitrate ceiling, which is a property of the
                # session, not the file.
                detail = ("nothing was wrong with this audio — it was "
                          "re-encoded to fit the session's bitrate limit")
        elif kind == "video":
            if src and dst and src != dst:
                detail = (f"the client would not take {src}, so Plex "
                          f"re-encoded it to {dst}")
            else:
                # "the client would not take h264, so Plex re-encoded it to
                # h264" was the old line here, and it is self-contradictory on
                # its face. A same-codec video transcode is a RESIZE: the
                # client asked for a lower quality, or is remote and capped.
                # Blaming the codec sends you looking for a rule to fix a track
                # that was never the problem - the exact failure this panel was
                # built to prevent.
                detail = "the codec was fine — Plex shrank the picture"
                if src_wh and dst_wh:
                    detail += f" ({src_wh} → {dst_wh})"
                elif dst_wh:
                    detail += f" (down to {dst_wh})"
        # Why the ceiling exists, appended to whichever line applies. This is
        # the part that turns "Plex worked" into "and here is what to change".
        if cap and kind in ("video", "audio") and (not src or src == dst):
            detail += f", {cap}"
        rows.append((now, fid, path, title or os.path.basename(path), user,
                     player.get("title") or "", player.get("product") or "",
                     decision, kind, act, src, dst, detail))
    if not rows:
        # Transcoding, but every stream says copy: the container was remuxed.
        rows.append((now, fid, path, title or os.path.basename(path), user,
                     player.get("title") or "", player.get("product") or "",
                     decision, "container", "remux", "", "",
                     "the tracks were fine; only the container was rebuilt"))
    # Plex's own session id, which is stable for as long as the stream lives.
    # Where it is missing (older servers) fall back to who is watching what on
    # which device - coarser, because two separate plays of the same file on
    # the same device merge into one, but far better than counting polls.
    sid = str(sj.get("id") or "") or f"{user}|{player.get('title')}|{path}"

    written = 0
    with cursor() as cur:
        for r in rows:
            kind, act = r[8], r[9]
            hit = cur.execute(
                "SELECT id, hits FROM playback_events "
                " WHERE session=? AND stream_kind=? AND stream_act=? "
                " ORDER BY id DESC LIMIT 1", (sid, kind, act)).fetchone()
            if hit:
                # Same session, same stream, still being worked on. Move the
                # end marker and count the sighting; do not add a row.
                cur.execute(
                    "UPDATE playback_events SET last_at=?, hits=COALESCE(hits,1)+1,"
                    " detail=? WHERE id=?", (now, r[12], hit["id"]))
                continue
            cur.execute(
                "INSERT INTO playback_events(at,file_id,path,title,user,client,"
                "product,decision,stream_kind,stream_act,src_codec,dst_codec,"
                "detail,session,last_at,hits)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)", r + (sid, now))
            written += 1
    return written


async def poll_once() -> dict:
    if not _READY:
        await asyncio.to_thread(init)
    items = await asyncio.to_thread(_sessions)
    STATS["last_poll"] = time.time()
    if items is None:
        return {"ok": False, "why": "Plex unreachable"}
    written = 0
    for s in items:
        if not isinstance(s, dict):
            continue
        key = str(s.get("sessionKey") or "")
        # ONE ROW PER SESSION, NOT PER POLL. A film transcoding for two hours
        # is one fact; at 20s intervals it would otherwise become 360 copies
        # of that fact and drown everything else in the panel.
        if key and key in _SEEN:
            continue
        try:
            n = await asyncio.to_thread(_record, s)
        except Exception as e:
            joblog.log(f"playback watch: {type(e).__name__}: {e}", "warn")
            continue
        if key:
            _SEEN[key] = time.time()
        written += n
    # Forget sessions that have ended, so a re-watch is recorded again.
    live = {str(s.get("sessionKey") or "") for s in items if isinstance(s, dict)}
    for k in [k for k in _SEEN if k not in live]:
        _SEEN.pop(k, None)
    if len(_SEEN) > _SEEN_MAX:
        for k in sorted(_SEEN, key=_SEEN.get)[:len(_SEEN) - _SEEN_MAX]:
            _SEEN.pop(k, None)
    STATS["events"] += written
    return {"ok": True, "sessions": len(items), "recorded": written}


def offenders(days: int = 30, limit: int = 40) -> dict:
    """Files that made Plex work, worst first, with the cause named."""
    since = time.time() - days * 86400
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT file_id, path, title, "
            "       COUNT(*) n, "
            "       COUNT(DISTINCT client) clients, "
            "       MAX(at) last_at, "
            "       GROUP_CONCAT(DISTINCT stream_kind) kinds, "
            "       GROUP_CONCAT(DISTINCT src_codec) codecs "
            "  FROM playback_events WHERE at > ? AND stream_act != 'copy' "
            " GROUP BY COALESCE(path, title) "
            " ORDER BY n DESC, last_at DESC LIMIT ?", (since, limit))]
        by_cause = [dict(r) for r in cur.execute(
            "SELECT stream_kind, stream_act, src_codec, COUNT(*) n "
            "  FROM playback_events WHERE at > ? AND stream_act != 'copy' "
            " GROUP BY stream_kind, stream_act, src_codec "
            " ORDER BY n DESC LIMIT 12", (since,))]
        by_client = [dict(r) for r in cur.execute(
            "SELECT product, client, COUNT(*) n FROM playback_events "
            " WHERE at > ? AND stream_act != 'copy' "
            " GROUP BY product, client ORDER BY n DESC LIMIT 8", (since,))]
        total = cur.execute("SELECT COUNT(*) n FROM playback_events "
                            "WHERE at > ?", (since,)).fetchone()["n"]
    return {"days": days, "total": total, "files": rows,
            "by_cause": by_cause, "by_client": by_client,
            "watching": bool(SETTINGS.plex_url and SETTINGS.plex_token),
            "last_poll": STATS["last_poll"]}


def detail_for(path: str = "", file_id: int | None = None,
               days: int = 90, limit: int = 60) -> dict:
    """Every recorded session for ONE title, newest first.

    The summary row says "audio, eac3, 3 times" - useful for spotting a
    pattern, useless for judging it. This is the evidence behind that row:
    which client, on which night, what Plex sent instead, and why. A file that
    only ever struggles on one old device is a different problem from one that
    struggles everywhere, and the only way to tell them apart is to look.
    """
    since = time.time() - days * 86400
    where, params = ["at > ?"], [since]
    if file_id:
        where.append("file_id = ?")
        params.append(file_id)
    elif path:
        where.append("path = ?")
        params.append(path)
    else:
        return {"events": []}
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute(
            "SELECT at, COALESCE(last_at, at) last_at, COALESCE(hits,1) hits, "
            "       COALESCE(session,'') session, "
            "       user, client, product, decision, stream_kind, "
            "       stream_act, src_codec, dst_codec, detail, title, path "
            f"  FROM playback_events WHERE {' AND '.join(where)} "
            " ORDER BY at DESC LIMIT ?", tuple(params) + (limit,))]
    # Grouped here rather than in the browser: the panel should not have to
    # re-derive what a session was, and doing it once server-side keeps the
    # ordering stable while rows are being updated underneath it.
    order: list[str] = []
    groups: dict[str, dict] = {}
    for r in rows:
        # Rows written before sessions were recorded have no id. Falling back
        # to viewer+device+start-minute keeps them grouped roughly correctly
        # instead of scattering them as one-row sessions.
        key = r["session"] or f"{r['user']}|{r['client']}|{int(r['at'] // 60)}"
        g = groups.get(key)
        if not g:
            order.append(key)
            g = groups[key] = {
                "session": key, "legacy": not r["session"],
                "user": r["user"], "client": r["client"],
                "product": r["product"], "decision": r["decision"],
                "started": r["at"], "ended": r["last_at"], "streams": []}
        g["started"] = min(g["started"], r["at"])
        g["ended"] = max(g["ended"], r["last_at"])
        g["streams"].append(r)
    for g in groups.values():
        g["streams"].sort(key=lambda x: {"video": 0, "audio": 1,
                                         "subtitle": 2}.get(x["stream_kind"], 3))
    return {"sessions": [groups[k] for k in order], "events": rows,
            "title": rows[0]["title"] if rows else "",
            "path": rows[0]["path"] if rows else path}


def prune(days: int = 120) -> int:
    with cursor() as cur:
        cur.execute("DELETE FROM playback_events WHERE at < ?",
                    (time.time() - days * 86400,))
        return cur.rowcount


async def watch() -> None:
    await asyncio.sleep(90)
    try:
        await asyncio.to_thread(init)
    except Exception as e:
        joblog.log(f"playback table: {type(e).__name__}: {e}", "error")
        return
    while True:
        schedules.beat('playback')
        try:
            await poll_once()
        except Exception as e:
            STATS["last_error"] = f"{type(e).__name__}: {e}"
        await asyncio.sleep(POLL_S)
