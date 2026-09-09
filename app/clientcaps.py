r"""What each Plex client will actually play, and whether this library suits it.

WHY THIS EXISTS
---------------
Every codec setting on the two codec pages is a bet about somebody else's
hardware. "Surround formats to copy: eac3" is not a fact about E-AC3, it is a
claim that every device on this server direct-plays E-AC3 - and until now
nothing on the page said whether that claim was true. The evidence existed:
playback.py has been writing down, per session, which stream each client
refused. It was one panel away from being usable and nobody had joined it up.

The joining is this module. It answers one question, per device and per
library: after nuarr is finished with a file, will that device play it
untouched?

THREE STATES, NOT TWO
---------------------
`yes` means observed playing here, or a well-established property of the
platform. `no` means observed being refused here, or a well-established
absence. `varies` is the honest answer for most codecs on most platforms and
it is not a cop-out: Plex for Windows plays E-AC3 through one audio device and
refuses it through another; a Roku's DTS support depends on the model; an
Android TV's decoder list depends on the SoC. A panel that flattened those to
yes/no would be lying in whichever direction it rounded.

OBSERVED BEATS ASSUMED, ALWAYS
------------------------------
The seeded profiles below exist so the panel says something useful on a fresh
install, before anyone has pressed play. The moment this server sees a device
play or refuse a codec, that observation replaces the assumption for that
device, and the panel says which of the two it is using. An assumption that
cannot be corrected by evidence is a bug; this one is corrected within one
session.

WHAT IT DOES NOT DO
-------------------
It does not change any setting. The panel is a mirror held up to the choices
on the page, not another thing making choices.
"""
from __future__ import annotations

import re
import time

from .db import cursor

# ---------------------------------------------------------------- storage ----
_READY = False


def init() -> None:
    """One row per (device, kind, codec, channels), counting both outcomes.

    BOTH COUNTERS ON ONE ROW, rather than a row per outcome, because the
    interesting devices are the ones that do both: the Bravia plays E-AC3 5.1
    and stumbles on E-AC3 2.0, and a schema that could not hold "played 9,
    refused 3" would have to pick one of those to believe.
    """
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS client_caps(
                product   TEXT NOT NULL,
                client    TEXT NOT NULL,
                kind      TEXT NOT NULL,     -- video | audio
                codec     TEXT NOT NULL,
                ch        INTEGER NOT NULL,  -- audio channels; 0 for video
                played    INTEGER NOT NULL DEFAULT 0,
                refused   INTEGER NOT NULL DEFAULT 0,
                first_at  REAL,
                last_at   REAL,
                PRIMARY KEY (product, client, kind, codec, ch)
            )""")
        # WHERE THE SIGHTING CAME FROM, best-first. Not decoration: the three
        # sources differ in how much they know. Tautulli's history carries the
        # exact channel count of the source track, a live sighting carries it
        # too, and the one-off read of nuarr's own old event rows does not -
        # those were written before any of this existed and can only say
        # "eac3 was refused", never "eac3 2.0 was". Knowing which is which is
        # what lets the precise rows replace the vague ones.
        have = {r[1] for r in cur.execute("PRAGMA table_info(client_caps)")}
        if "src" not in have:
            cur.execute("ALTER TABLE client_caps ADD COLUMN src TEXT")
    _READY = True


# Best last, so a later source overwrites an earlier one and never the reverse.
_SRC_RANK = {"": 0, "backfill": 1, "live": 2, "tautulli": 3}


def note(product: str, client: str, kind: str, codec: str,
         ch: int = 0, ok: bool = True, n: int = 1, at: float = 0.0,
         src: str = "live", cur=None) -> None:
    """Record one sighting. Cheap, and never allowed to break playback watching."""
    codec = (codec or "").strip().lower()
    if not codec or kind not in ("video", "audio"):
        return
    if not _READY:
        try:
            init()
        except Exception:                                    # noqa: BLE001
            return
    now = at or time.time()
    col = "played" if ok else "refused"

    def _go(c):
        c.execute(
            f"INSERT INTO client_caps(product,client,kind,codec,ch,{col},"
            f"                        first_at,last_at,src) VALUES(?,?,?,?,?,?,?,?,?) "
            f"ON CONFLICT(product,client,kind,codec,ch) DO UPDATE SET "
            f"  {col}={col}+excluded.{col}, "
            f"  last_at=MAX(COALESCE(last_at,0),excluded.last_at), "
            f"  first_at=MIN(COALESCE(first_at,excluded.first_at),excluded.first_at), "
            f"  src=CASE WHEN ?>? THEN excluded.src ELSE src END",
            ((product or "?").strip(), (client or "?").strip(),
             kind, codec, int(ch or 0), int(n), now, now, src,
             _SRC_RANK.get(src, 0), 0))
    try:
        if cur is not None:
            _go(cur)
        else:
            with cursor() as c:
                _go(c)
    except Exception:                                        # noqa: BLE001
        pass


def backfill() -> int:
    r"""Read the refusals already in playback_events. Once, on first run.

    THE EVIDENCE PREDATES THE TABLE. This server has months of recorded
    transcodes, every one of them a device naming a codec it would not take,
    and starting the capability table empty would throw all of it away and
    then ask the same questions again over the following weeks. The events
    only carry refusals - a direct play was never written down - so this
    seeds the `refused` side only, and the `played` side fills in from the
    first session after the upgrade.
    """
    from .db import kv_get, kv_set
    if (kv_get("clientcaps.backfilled") or "") == "1":
        return 0
    if not _READY:
        init()
    n = 0
    try:
        with cursor() as cur:
            # ONLY ROWS THAT AGREE WITH THEMSELVES. Until the fix above, a
            # repeat sighting could overwrite one row's detail with another
            # stream's sentence, leaving src_codec and detail describing
            # different tracks. Requiring the detail to name the same codec
            # the row blames throws those away rather than teaching this
            # table that an Xbox cannot play MP3 - which it can, and the row
            # claiming otherwise was really about E-AC3.
            #
            # It also, for free, excludes the other false positive: a track
            # re-encoded to fit a bandwidth cap has src != dst but says so in
            # its detail, and is not a statement about the decoder at all.
            rows = cur.execute(
                "SELECT product, client, stream_kind, src_codec, dst_codec, "
                "       COUNT(*) n, MIN(at) a0, MAX(COALESCE(last_at,at)) a1 "
                "  FROM playback_events "
                " WHERE stream_act='transcode' AND stream_kind IN ('video','audio') "
                "   AND COALESCE(src_codec,'') != '' "
                "   AND COALESCE(dst_codec,'') != '' AND src_codec != dst_codec "
                "   AND detail LIKE 'the client would not take ' || src_codec || '%' "
                " GROUP BY product, client, stream_kind, src_codec").fetchall()
            for r in rows:
                cur.execute(
                    "INSERT INTO client_caps(product,client,kind,codec,ch,"
                    "                        refused,first_at,last_at) "
                    "VALUES(?,?,?,?,0,?,?,?) "
                    "ON CONFLICT(product,client,kind,codec,ch) DO UPDATE SET "
                    "  refused=refused+excluded.refused",
                    ((r["product"] or "?"), (r["client"] or "?"),
                     r["stream_kind"], (r["src_codec"] or "").lower(),
                     r["n"], r["a0"], r["a1"]))
                cur.execute(
                    "UPDATE client_caps SET src=COALESCE(src,'backfill') "
                    " WHERE product=? AND client=? AND kind=? AND codec=? AND ch=0",
                    ((r["product"] or "?"), (r["client"] or "?"),
                     r["stream_kind"], (r["src_codec"] or "").lower()))
                n += 1
        kv_set("clientcaps.backfilled", "1")
    except Exception:                                        # noqa: BLE001
        return 0
    return n


# ------------------------------------------------------------- Tautulli ----
# WHY GO TO TAUTULLI AT ALL, when nuarr watches Plex itself.
#
# Because nuarr started watching in July and Tautulli started years ago, and
# because nuarr deliberately records only the sessions that went wrong. This
# server's Tautulli holds 51,689 plays, each one carrying the source audio
# codec, THE SOURCE CHANNEL COUNT, and a per-stream decision - which is
# precisely the evidence the panel wants and precisely the evidence nuarr's own
# event log cannot supply, because a direct play was never written down and a
# refusal never recorded how many channels the refused track had.
#
# That last one is not a detail. The laptop refused E-AC3 and asked for Opus,
# and the panel concluded the laptop could not play E-AC3 at all - so every
# library went red. The track it refused was E-AC3 2.0. Tautulli knew that all
# along.
TAUT: dict = {"running": False, "done": 0, "total": 0, "rows": 0, "learned": 0,
              "at": 0.0, "error": "", "devices": 0}

# HOW MANY ROWS PER DEVICE. The whole history is 50k sessions and one API call
# each, which is an hour of polling to learn something a few dozen rows per
# device already say. Capped per device AND per decision so a device that
# direct-plays everything still contributes its refusals, and one that
# transcodes everything still contributes its successes.
_TAUT_PER_DEVICE = 45
_TAUT_WALK = 8000


def _f(v) -> float:
    """Tautulli returns numbers as strings about half the time."""
    try:
        return float(v)
    except Exception:                                        # noqa: BLE001
        return 0.0


def enabled() -> bool:
    r"""Is nuarr allowed to talk to Tautulli at all?

    ON BY DEFAULT, because a configured connection that is silently ignored is
    worse than no connection. Off means every caller behaves as though Tautulli
    were not installed: the gate stops using it as a session fallback and the
    capability import stops running. The credentials stay put - turning an
    integration off should not make you find your API key again.
    """
    from .db import kv_get
    return (kv_get("tautulli.enabled") or "1") == "1"


def set_enabled(on: bool) -> bool:
    from .db import kv_set
    kv_set("tautulli.enabled", "1" if on else "0")
    return enabled()


def _taut_cfg() -> tuple[str, str]:
    """URL and key, or blanks when the integration is switched off."""
    from .config import SETTINGS
    if not enabled():
        return "", ""
    return ((SETTINGS.tautulli_url or "").rstrip("/"),
            SETTINGS.tautulli_api_key or "")


def import_tautulli(walk: int = _TAUT_WALK) -> dict:
    r"""Learn from Tautulli's history. Runs in a thread; reports through TAUT.

    INCREMENTAL, by row id. Tautulli's history ids only go up, so the highest
    one consumed is a watermark and a second run reads only what is new. That
    matters more than it sounds: without it, pressing the button twice would
    double every count in the table and quietly turn "played 3, refused 1"
    into a different verdict.
    """
    import httpx
    from .db import kv_get, kv_set
    base, key = _taut_cfg()
    if not base or not key:
        TAUT.update(error=("Tautulli is switched off" if not enabled()
                           else "Tautulli is not configured"),
                    at=time.time())
        return dict(TAUT)
    if TAUT["running"]:
        return dict(TAUT)
    if not _READY:
        init()
    TAUT.update(running=True, done=0, total=0, rows=0, learned=0, error="",
                devices=0, at=time.time())
    seen_max = int(kv_get("clientcaps.taut_row") or 0)
    high = seen_max
    learned = 0
    try:
        with httpx.Client(timeout=30.0) as cli:
            def api(cmd, **kw):
                r = cli.get(f"{base}/api/v2",
                            params={"apikey": key, "cmd": cmd, **kw})
                r.raise_for_status()
                return r.json()["response"]["data"]

            # 1. Walk the history newest-first and pick what to sample.
            picked: dict = {}
            start, walked = 0, 0
            while walked < walk:
                page = api("get_history", length=500, start=start)
                rows = page.get("data") or []
                if not rows:
                    break
                for r in rows:
                    rid = r.get("row_id") or r.get("id")
                    if not rid:
                        continue          # a live session; it has no history row yet
                    rid = int(rid)
                    high = max(high, rid)
                    if rid <= seen_max:
                        rows = []          # caught up with the last import
                        break
                    dev = ((r.get("product") or "?"), (r.get("player") or "?"))
                    dec = str(r.get("transcode_decision") or "").lower()
                    b = picked.setdefault(dev, {})
                    lst = b.setdefault(dec, [])
                    if len(lst) < _TAUT_PER_DEVICE:
                        lst.append((rid, float(r.get("date") or 0)))
                walked += len(page.get("data") or [])
                if not rows:
                    break
                start += 500
            todo = [(d, rid, at) for d, byd in picked.items()
                    for lst in byd.values() for rid, at in lst]
            TAUT.update(total=len(todo), rows=walked, devices=len(picked))

            # 2. Ask each sampled row what the streams actually were.
            with cursor() as cur:
                for (product, player), rid, at in todo:
                    TAUT["done"] += 1
                    try:
                        d = api("get_stream_data", row_id=rid)
                    except Exception:                        # noqa: BLE001
                        continue
                    if not d:
                        continue
                    # A CAPPED SESSION IS ABOUT BANDWIDTH, NOT DECODERS, and
                    # every refusal read out of one is a lie about the device.
                    # Proved on this server: the Bravia showed 25 "AAC 2.0
                    # refusals", and every single one was a session with
                    # quality_profile "1.5 Mbps 480p" - source 8.2 Mbps rebuilt
                    # at 1.5, video h264 to h264 downscaled, audio switched to
                    # Opus because Opus is simply better at low rates. That TV
                    # plays AAC perfectly. It was being asked not to.
                    #
                    # So a capped session contributes its SUCCESSES only: a
                    # stream that direct-played through a cap really was
                    # accepted, while a stream that was rebuilt tells us
                    # nothing about what the decoder would have taken. Under-
                    # recording is the right way to be wrong here; there are
                    # thousands of uncapped sessions to learn the rest from.
                    qp = str(d.get("quality_profile") or "").strip().lower()
                    br, sbr = _f(d.get("bitrate")), _f(d.get("stream_bitrate"))
                    capped = bool(qp and qp != "original") or bool(
                        br and sbr and sbr < br * 0.9)
                    for kind, cod, dec, ch in (
                            ("video", d.get("video_codec"),
                             d.get("video_decision"), 0),
                            ("audio", d.get("audio_codec"),
                             d.get("audio_decision"),
                             int(d.get("audio_channels") or 0))):
                        cod = str(cod or "").strip().lower()
                        dec = str(dec or "").strip().lower()
                        if not cod or not dec:
                            continue
                        # DIRECT PLAY AND COPY ARE BOTH "THE DECODER WAS FINE".
                        # A copy is a remux - the container changed and the
                        # stream did not - so the client took that codec.
                        if dec in ("direct play", "copy", "direct stream"):
                            note(product, player, kind, cod, ch, ok=True,
                                 at=at, src="tautulli", cur=cur)
                            learned += 1
                            TAUT["learned"] = learned
                        elif dec == "transcode" and not capped:
                            # A TRANSCODE TO THE SAME CODEC IS NOT A REFUSAL,
                            # same as in the live path: it is a resize or a
                            # bandwidth cap, and counting it here would teach
                            # this table that a device cannot play the codec
                            # it is at this moment playing.
                            out = str((d.get(f"stream_{kind}_codec")
                                       or "")).strip().lower()
                            if out and out != cod:
                                note(product, player, kind, cod, ch, ok=False,
                                     at=at, src="tautulli", cur=cur)
                                learned += 1
                                TAUT["learned"] = learned
        kv_set("clientcaps.taut_row", str(high))
        kv_set("clientcaps.taut_at", str(time.time()))
        # THE VAGUE ROWS GO NOW THAT THERE ARE PRECISE ONES. The one-off read
        # of nuarr's old events could only say "eac3 was refused" with no
        # channel count, and leaving those beside Tautulli's exact rows means
        # every audio answer falls back to "it depends" forever.
        if learned:
            try:
                with cursor() as cur:
                    cur.execute("DELETE FROM client_caps "
                                " WHERE src='backfill' AND kind='audio' AND ch=0")
            except Exception:                                # noqa: BLE001
                pass
        TAUT.update(learned=learned, error="")
    except Exception as e:                                   # noqa: BLE001
        TAUT.update(error=f"{type(e).__name__}: {e}")
    finally:
        TAUT.update(running=False, at=time.time())
    return dict(TAUT)


def taut_state() -> dict:
    from .config import SETTINGS
    base, key = _taut_cfg()
    from .db import kv_get
    last = float(kv_get("clientcaps.taut_at") or 0)
    return {**TAUT, "configured": bool(base and key), "url": base,
            "enabled": enabled(),
            # What is stored, regardless of the switch, so the page can offer
            # to turn it back ON rather than asking for the key again.
            "has_conn": bool((SETTINGS.tautulli_url or "")
                             and (SETTINGS.tautulli_api_key or "")),
            "imported_to": int(kv_get("clientcaps.taut_row") or 0),
            "auto": taut_auto(), "every_h": taut_every_h(),
            "last_ok": last,
            # No last run means no next one to name - it happens when the
            # watcher next comes round, and saying so beats printing 1970.
            "next_at": (last + taut_every_h() * 3600
                        if (taut_auto() and last) else 0.0)}


def taut_auto() -> bool:
    """Whether the recurring import is on. On by default, because the whole
    point of a capability table is that it does not go stale."""
    from .db import kv_get
    return (kv_get("clientcaps.auto") or "1") == "1"


def taut_every_h() -> float:
    from .db import kv_get
    try:
        return max(1.0, float(kv_get("clientcaps.every_h") or 6))
    except Exception:                                        # noqa: BLE001
        return 6.0


def set_taut_auto(on: bool, every_h: float | None = None) -> dict:
    from .db import kv_set
    kv_set("clientcaps.auto", "1" if on else "0")
    if every_h:
        kv_set("clientcaps.every_h", str(max(1.0, float(every_h))))
    return taut_state()


def stats() -> dict:
    """What the table knows, for the Tautulli page's own summary."""
    out = {"rows": 0, "devices": 0, "played": 0, "refused": 0,
           "by_src": {}, "newest": 0.0, "oldest": 0.0}
    try:
        if not _READY:
            init()
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) n, COUNT(DISTINCT product||'|'||client) d, "
                "       COALESCE(SUM(played),0) p, COALESCE(SUM(refused),0) x, "
                "       MAX(last_at) hi, MIN(first_at) lo FROM client_caps"
            ).fetchone()
            out.update(rows=r["n"], devices=r["d"], played=r["p"],
                       refused=r["x"], newest=r["hi"] or 0.0,
                       oldest=r["lo"] or 0.0)
            for s in cur.execute("SELECT COALESCE(src,'?') s, COUNT(*) n "
                                 "  FROM client_caps GROUP BY 1"):
                out["by_src"][s["s"]] = s["n"]
    except Exception:                                        # noqa: BLE001
        pass
    return out


async def watch() -> None:
    r"""Keep the capability table current, on a timer.

    A CAPABILITY TABLE THAT IS NEVER TOPPED UP IS A TABLE THAT SLOWLY BECOMES
    A LIE. Devices get firmware, people buy new televisions, an app rewrites
    its decoder list - and the panel would go on quoting a year-old sighting
    with total confidence. The import is incremental by row id, so a run that
    finds nothing new costs one API call.
    """
    import asyncio
    from . import joblog, schedules
    schedules.register(
        "clientcaps", "Tautulli capability import", "Library",
        taut_every_h() * 3600,
        what="Reads Tautulli's newest play history and records which codecs, "
             "at which channel counts, each device played untouched or had "
             "transcoded. Feeds the 'Will each device play this?' panel on "
             "the codec pages. Incremental - a run with nothing new is one "
             "API call.",
        toggle="clientcaps.auto")
    from .db import kv_get, kv_set
    kv_set("clientcaps.auto", "1" if taut_auto() else "0")   # so the switch shows
    await asyncio.sleep(150)          # let the first scan and Plex settle
    while True:
        schedules.beat("clientcaps")
        try:
            base, key = _taut_cfg()
            last = float(kv_get("clientcaps.taut_at") or 0)
            due = time.time() - last >= taut_every_h() * 3600
            if taut_auto() and base and key and due and not TAUT["running"]:
                with joblog.section("Tautulli capability import"):
                    d = await asyncio.to_thread(import_tautulli)
                    if d.get("error"):
                        joblog.log(f"Tautulli import: {d['error']}", "warn")
                    elif d.get("learned"):
                        joblog.log(
                            f"Tautulli: read {d.get('done', 0)} session(s) and "
                            f"learned {d['learned']} fact(s) about what "
                            f"{d.get('devices', 0)} device(s) will play", "ok")
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"Tautulli capability import: "
                       f"{type(e).__name__}: {e}", "warn")
        await asyncio.sleep(900)


def observed() -> dict:
    """{(product, client): {kind: {codec: {ch: (played, refused)}}}}"""
    out: dict = {}
    try:
        if not _READY:
            init()
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT product, client, kind, codec, ch, played, refused, "
                    "       last_at FROM client_caps"):
                d = out.setdefault((r["product"], r["client"]), {})
                d.setdefault(r["kind"], {}).setdefault(r["codec"], {})[r["ch"]] = \
                    (r["played"] or 0, r["refused"] or 0)
                d["last_at"] = max(d.get("last_at") or 0.0, r["last_at"] or 0.0)
    except Exception:                                        # noqa: BLE001
        pass
    return out


# ------------------------------------------------------------ known profiles --
# TYPICAL, NOT AUTHORITATIVE, and the panel says so wherever it uses one.
#
# Each entry is what the PLATFORM generally direct-plays, which is the most
# that can be known without watching the specific box: "Roku" covers a decade
# of models and "Android TV" covers every SoC anyone has shipped. So the
# honest value for most codecs is `varies`, and only the things that are true
# across the whole platform - a browser cannot do E-AC3, an Apple TV can - are
# stated flatly.
#
# `match` is tested against Plex's `product` string, lowercased.
Y, N, V = "yes", "no", "varies"

KNOWN: list[dict] = [
    {"id": "web", "label": "Plex Web (Chrome, Edge, Firefox)",
     "match": r"^plex web",
     "note": "A browser, so the list is whatever the browser will decode in "
             "its own media engine. No browser direct-plays E-AC3 or TrueHD, "
             "and HEVC only works in Edge and Safari.",
     "video": {"h264": Y, "hevc": V, "vp9": Y, "av1": V, "mpeg4": N, "vc1": N},
     "audio": {"aac": Y, "mp3": Y, "opus": Y, "flac": Y, "vorbis": Y,
               "ac3": V, "eac3": N, "dts": N, "truehd": N}},
    {"id": "windows", "label": "Plex for Windows / macOS (desktop app)",
     "match": r"^plex for (windows|mac)",
     "note": "Uses the OS audio path, so what it accepts depends on the "
             "output device: the same app takes E-AC3 to an HDMI receiver and "
             "refuses it to laptop speakers. This server has watched it "
             "refuse E-AC3 and ask for Opus instead.",
     "video": {"h264": Y, "hevc": Y, "vp9": Y, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "opus": Y, "flac": Y,
               "ac3": V, "eac3": V, "dts": V, "truehd": V}},
    {"id": "appletv", "label": "Apple TV",
     "match": r"apple ?tv",
     "note": "A fixed platform, so this list is unusually reliable. Dolby "
             "formats pass through to the receiver over HDMI.",
     "video": {"h264": Y, "hevc": Y, "vp9": N, "av1": V, "mpeg4": V, "vc1": N},
     "audio": {"aac": Y, "mp3": Y, "flac": Y, "ac3": Y, "eac3": Y,
               "opus": V, "dts": V, "truehd": V}},
    {"id": "roku", "label": "Roku",
     "match": r"roku",
     "note": "Spans ten years of hardware. 4K models decode HEVC; older "
             "sticks do not, and DTS depends on the model.",
     "video": {"h264": Y, "hevc": V, "vp9": V, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "ac3": Y, "eac3": Y,
               "flac": V, "opus": V, "dts": V, "truehd": V}},
    {"id": "androidtv", "label": "Android TV / Google TV / Sony Bravia",
     "match": r"android \(?tv|google tv|bravia|shield",
     "note": "The codec list comes from the SoC, so it differs between a "
             "Shield, a Bravia and a cheap stick. Sony sets are known to "
             "advertise E-AC3 and then stumble on stereo E-AC3, which "
             "restarts the audio transcode on every seek.",
     "video": {"h264": Y, "hevc": Y, "vp9": Y, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "ac3": Y, "eac3": V,
               "flac": V, "opus": V, "dts": V, "truehd": V}},
    {"id": "samsung", "label": "Samsung TV (Tizen)",
     "match": r"samsung|tizen",
     "note": "Built-in TV app. HEVC and the Dolby formats are usual; DTS was "
             "dropped from many 2018-and-later sets.",
     "video": {"h264": Y, "hevc": Y, "vp9": V, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "ac3": Y, "eac3": Y,
               "flac": V, "opus": V, "dts": N, "truehd": V}},
    {"id": "lg", "label": "LG TV (webOS)",
     "match": r"\blg\b|webos",
     "note": "Built-in TV app, much like the Samsung one.",
     "video": {"h264": Y, "hevc": Y, "vp9": Y, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "ac3": Y, "eac3": Y,
               "flac": V, "opus": V, "dts": V, "truehd": V}},
    {"id": "chromecast", "label": "Chromecast",
     "match": r"chromecast|cast",
     "note": "The dongle, not Google TV. No DTS or TrueHD at all.",
     "video": {"h264": Y, "hevc": V, "vp9": Y, "av1": N, "mpeg4": V, "vc1": N},
     "audio": {"aac": Y, "mp3": Y, "opus": Y, "flac": Y, "ac3": Y, "eac3": Y,
               "dts": N, "truehd": N}},
    {"id": "ios", "label": "Plex for iOS / iPadOS",
     "match": r"plex for (ios|ipad)",
     "note": "Apple's decoder set. Dolby formats decode, but to stereo unless "
             "an external device is attached.",
     "video": {"h264": Y, "hevc": Y, "vp9": N, "av1": N, "mpeg4": V, "vc1": N},
     "audio": {"aac": Y, "mp3": Y, "flac": Y, "ac3": Y, "eac3": Y,
               "opus": V, "dts": V, "truehd": N}},
    {"id": "android", "label": "Plex for Android (phone / tablet)",
     "match": r"plex for android(?! \(tv)",
     "note": "Whatever the phone's SoC exposes, which on anything recent is a "
             "wide list.",
     "video": {"h264": Y, "hevc": Y, "vp9": Y, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "flac": Y, "opus": Y, "ac3": V, "eac3": V,
               "dts": V, "truehd": N}},
    {"id": "xbox", "label": "Xbox",
     "match": r"xbox",
     "note": "Dolby formats pass through; DTS needs the paid DTS app on some "
             "generations.",
     "video": {"h264": Y, "hevc": Y, "vp9": Y, "av1": V, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "ac3": Y, "eac3": Y, "flac": Y,
               "opus": V, "dts": V, "truehd": V}},
    {"id": "playstation", "label": "PlayStation",
     "match": r"playstation|ps[45]",
     "note": "HEVC on PS5; the PS4 app is H.264 only.",
     "video": {"h264": Y, "hevc": V, "vp9": V, "av1": N, "mpeg4": V, "vc1": V},
     "audio": {"aac": Y, "mp3": Y, "ac3": Y, "eac3": Y,
               "flac": V, "opus": V, "dts": V, "truehd": V}},
]

# What the panel shows when a device matches nothing above. Deliberately
# conservative: the pair every Plex client on earth plays.
FALLBACK = {"id": "unknown", "label": "Unrecognised client",
            "note": "This device does not match any profile nuarr knows, so "
                    "only the universally safe formats are assumed. Play "
                    "something on it and the real answer replaces this.",
            "video": {"h264": Y, "hevc": V, "vp9": V, "av1": V},
            "audio": {"aac": Y, "mp3": Y, "ac3": V, "eac3": V, "dts": V,
                      "truehd": V, "flac": V, "opus": V}}


def profile_for(product: str, client: str = "") -> dict:
    """The seeded profile a device name implies."""
    hay = f"{product or ''} {client or ''}".lower()
    for k in KNOWN:
        if re.search(k["match"], hay):
            return k
    return FALLBACK


# ------------------------------------------------- what a library produces ---
def library_outputs(library: str) -> dict:
    r"""The formats a file in this library can carry once nuarr is done.

    NOT "what is in the library today" - that is a scan, and it answers a
    different question. This is what the SETTINGS on these two pages commit to
    producing, which is the thing the panel is checking a device against. A
    file nuarr never touches keeps whatever it arrived as, and the panel says
    so rather than pretending otherwise.
    """
    from . import codecpolicy
    v = codecpolicy.for_library(library, "video")
    a = codecpolicy.for_library(library, "audio")
    vids = ["h264"]
    if any(v.get(k) for k in ("route_hevc", "route_10bit", "route_hdr",
                              "route_av1")):
        vids.insert(0, "hevc")
    if not v.get("convert_av1", True):
        vids.append("av1")
    # Audio, as (codec, channels). The encoded targets first, then anything
    # the copy lists let through untouched.
    ceil = int(a.get("surround_max_channels") or 6)
    auds = [("eac3", ceil)] + [(c, ceil) for c in (a.get("copy_surround_if") or [])]
    auds += [("aac", 2)] + [(c, 2) for c in (a.get("copy_stereo_if") or [])]
    seen, uniq = set(), []
    for c, ch in auds:
        if (c, ch) not in seen:
            seen.add((c, ch))
            uniq.append((c, ch))
    return {"video": vids, "audio": uniq,
            "stereo_bitrate": a.get("stereo_bitrate"),
            "surround_bitrate": a.get("surround_bitrate")}


def _verdict(prof: dict, obs: dict, kind: str, codec: str, ch: int) -> dict:
    """One codec against one device. Observation first, profile second."""
    o = ((obs.get(kind) or {}).get(codec) or {})
    # THE EXACT CHANNEL COUNT IS THE STRONGER EVIDENCE, because that is where
    # the interesting failures live - a device that plays E-AC3 5.1 and balks
    # at E-AC3 2.0 is invisible to a codec-only reading, and that is the exact
    # case this server hit on the Bravia.
    # THE EXACT CHANNEL COUNT IS THE ONLY DEFINITIVE EVIDENCE, and this is the
    # whole reason the column exists. Erik's laptop refused E-AC3 and asked for
    # Opus - but the track it refused was E-AC3 2.0, and the panel read that as
    # "this device cannot play E-AC3" and marked every 5.1 library red. Stereo
    # and 5.1 E-AC3 are different questions on the same decoder: a Bravia plays
    # 5.1 and stumbles on 2.0, a laptop does the reverse. So a sighting at one
    # channel count never settles another - it only ever softens the platform's
    # answer to "it depends", which is the truth.
    hit = o.get(ch)
    if hit:
        played, refused = hit
        at = f" at {_chan(ch)}" if ch else ""
        if refused and played:
            return {"state": V,
                    "why": f"played {played}×, transcoded {refused}×{at} here",
                    "src": "seen here"}
        if refused:
            return {"state": N, "why": f"transcoded {refused}×{at} here",
                    "src": "seen here"}
        return {"state": Y, "why": f"played untouched {played}×{at} here",
                "src": "seen here"}
    if o:
        played = sum(p for p, _ in o.values())
        refused = sum(r for _, r in o.values())
        where = ", ".join(
            (_chan(c) if c else "channel count not recorded")
            for c in sorted(o) )
        if played or refused:
            bits = []
            if played:
                bits.append(f"played {played}×")
            if refused:
                bits.append(f"transcoded {refused}×")
            return {"state": V,
                    "why": (", ".join(bits) + f" here, but only at {where}"
                            + (f" — never at {_chan(ch)}" if ch else "")),
                    "src": "seen here, other channel counts"}
    st = (prof.get(kind) or {}).get(codec, V)
    return {"state": st, "why": {Y: "this platform normally plays it",
                                 N: "this platform does not decode it",
                                 V: "depends on the model or the output "
                                    "device"}[st],
            "src": "typical for the platform"}


def matrix(side: str = "video") -> dict:
    r"""Every device against every library, for one side of the pipeline.

    Devices this server has actually seen come first and are marked; the
    seeded profiles fill in the rest so the panel is useful before anyone has
    pressed play.
    """
    from .langpolicy import libraries
    side = "video" if side != "audio" else "audio"
    try:
        backfill()
    except Exception:                                        # noqa: BLE001
        pass
    obs = observed()
    libs = libraries()
    outs = {L: library_outputs(L) for L in libs}

    devices = []
    seen_ids = set()
    for (product, client), o in sorted(
            obs.items(), key=lambda kv: -(kv[1].get("last_at") or 0)):
        prof = profile_for(product, client)
        seen_ids.add(prof["id"])
        devices.append({"product": product, "client": client,
                        "label": f"{product}" + (f" · {client}" if client and
                                                 client != product else ""),
                        "profile": prof["label"], "note": prof["note"],
                        "seen": True,
                        "last_at": o.get("last_at") or 0.0,
                        "_prof": prof, "_obs": o})
    for k in KNOWN:
        if k["id"] in seen_ids:
            continue
        devices.append({"product": k["label"], "client": "",
                        "label": k["label"], "profile": k["label"],
                        "note": k["note"], "seen": False, "last_at": 0.0,
                        "_prof": k, "_obs": {}})

    rows = []
    for d in devices:
        prof, o = d.pop("_prof"), d.pop("_obs")
        # WHAT THIS DEVICE PLAYS, FULL STOP - not just the formats these
        # libraries happen to produce today. The panel's job is to help decide
        # what to produce, and you cannot decide that from a list narrowed to
        # what you already decided. Everything observed on the device, plus
        # everything its platform profile claims, both sides of the pipeline.
        known = {"video": [], "audio": []}
        for kind in ("video", "audio"):
            seen = []
            for codec, chans in (o.get(kind) or {}).items():
                for ch, (played, refused) in chans.items():
                    vd = _verdict(prof, o, kind, codec, ch)
                    seen.append({"codec": codec, "ch": ch, "played": played,
                                 "refused": refused, **vd})
            # Anything the platform claims that has never been seen here, so a
            # device with two sightings still shows a usable list.
            for codec in (prof.get(kind) or {}):
                if not any(x["codec"] == codec for x in seen):
                    vd = _verdict(prof, {}, kind, codec, 0)
                    seen.append({"codec": codec, "ch": 0, "played": 0,
                                 "refused": 0, **vd})
            order = {Y: 0, V: 1, N: 2}
            seen.sort(key=lambda x: (order.get(x["state"], 3),
                                     -(x["played"] + x["refused"]),
                                     x["codec"], x["ch"]))
            known[kind] = seen
        d["known"] = known
        cells = {}
        for L in libs:
            out = outs[L]
            items = ([(c, 0) for c in out["video"]] if side == "video"
                     else out["audio"])
            per = []
            for codec, ch in items:
                vd = _verdict(prof, o, side, codec, ch)
                per.append({"codec": codec, "ch": ch, **vd})
            bad = [p for p in per if p["state"] == N]
            iffy = [p for p in per if p["state"] == V]
            if bad:
                state, txt = "bad", "transcodes " + ", ".join(
                    _fmt(p) for p in bad)
            elif iffy:
                state, txt = "warn", "may transcode " + ", ".join(
                    _fmt(p) for p in iffy)
            else:
                state, txt = "ok", "direct play"
            cells[L] = {"state": state, "text": txt, "codecs": per}
        rows.append({**d, "cells": cells})

    return {"side": side, "libraries": libs,
            "outputs": {L: {"video": outs[L]["video"],
                            "audio": [f"{c} {_chan(ch)}"
                                      for c, ch in outs[L]["audio"]]}
                        for L in libs},
            "devices": rows,
            "seen_any": any(r["seen"] for r in rows)}


def _chan(ch: int) -> str:
    return {2: "2.0", 6: "5.1", 8: "7.1"}.get(int(ch or 0), f"{ch}ch")


def _fmt(p: dict) -> str:
    return p["codec"] + (f" {_chan(p['ch'])}" if p["ch"] else "")
