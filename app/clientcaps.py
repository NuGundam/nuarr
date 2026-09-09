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
    _READY = True


def note(product: str, client: str, kind: str, codec: str,
         ch: int = 0, ok: bool = True) -> None:
    """Record one sighting. Cheap, and never allowed to break playback watching."""
    codec = (codec or "").strip().lower()
    if not codec or kind not in ("video", "audio"):
        return
    if not _READY:
        try:
            init()
        except Exception:                                    # noqa: BLE001
            return
    now = time.time()
    col = "played" if ok else "refused"
    try:
        with cursor() as cur:
            cur.execute(
                f"INSERT INTO client_caps(product,client,kind,codec,ch,{col},"
                f"                        first_at,last_at) VALUES(?,?,?,?,?,1,?,?) "
                f"ON CONFLICT(product,client,kind,codec,ch) DO UPDATE SET "
                f"  {col}={col}+1, last_at=excluded.last_at",
                ((product or "?").strip(), (client or "?").strip(),
                 kind, codec, int(ch or 0), now, now))
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
                n += 1
        kv_set("clientcaps.backfilled", "1")
    except Exception:                                        # noqa: BLE001
        return 0
    return n


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
    hit = o.get(ch)
    src = "seen here"
    if hit is None and o:
        played = sum(p for p, _ in o.values())
        refused = sum(r for _, r in o.values())
        hit = (played, refused)
        # 0 is the channel count of a sighting recorded before nuarr kept
        # them - the backfilled history. Saying "other channel counts" about
        # those would be inventing a distinction the row never made.
        src = ("seen here, channel count not recorded" if set(o) == {0}
               else "seen here, at other channel counts")
    if hit:
        played, refused = hit
        if refused and played:
            return {"state": V, "why": f"played {played}×, transcoded "
                                       f"{refused}× on this server",
                    "src": src}
        if refused:
            return {"state": N, "why": f"transcoded {refused}× on this server",
                    "src": src}
        return {"state": Y, "why": f"played untouched {played}× here",
                "src": src}
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
