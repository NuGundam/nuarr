r"""Which video and audio codec settings apply, per library.

WHY THIS EXISTS
---------------
Every number here was already in the system - as a constant in rules.CONFIG,
surrounded by a comment explaining the measurement that produced it. That was
fine while there was one answer for the whole library, and it stopped being
fine the moment the right answer differed by content.

The clearest case is CQ. Drawn animation is large flat areas and smooth
gradients, which a quality-targeted encoder reads as "cheap" and hands very few
bits; in a dark scene those bits become visible banding, and motion turns the
banding into crawling blocks. That is why the space saver excludes animation
outright. Live action absorbs the same setting because film grain and detail
keep the encoder honest. One CQ cannot be correct for both, and until now there
was no way to say so without editing the source.

SHAPED LIKE langpolicy, ON PURPOSE
----------------------------------
Same storage, same per-library keying, same seeding from the kind a library
name implies, same "a removed library keeps its settings". Two settings pages
that do the same job in two different ways is a tax paid on every future
change, and the language page had already solved this.

DEFAULTS ARE THE OLD CONSTANTS, EXACTLY
---------------------------------------
Every default below is the value rules.CONFIG held before any of this was
configurable, so a fresh install and an untouched library behave precisely as
they did. Nothing here changes what nuarr does until someone changes it.

THE EXPLANATIONS ARE THE POINT
------------------------------
`what` on each field is not documentation bolted on afterwards - it is the
reason the default is the number it is, carried over from the comment that
justified it. A settings page full of sliders with no stated consequence
invites exactly the kind of guessing this system was built to replace.
"""
from __future__ import annotations

import json

from .db import cursor
from .langpolicy import KIND_LABELS, kind_for, libraries

_KEY = "codecpolicy.v1"

SIDES = ("video", "audio")

# Presets NVENC accepts, fastest to slowest. p5 is the default because it is
# the point where the curve flattens on this card: p6 and p7 cost noticeably
# more time for a fraction of a percent of size.
NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]

# EVERY FAMILY'S LADDER IN ONE LIST, because the encoder is a per-library
# choice now and the preset box has to be able to hold whatever that library
# picked. "p5" means nothing to libx265 and "medium" means nothing to NVENC,
# so a preset belonging to a different family is not passed through - the
# command builder substitutes that family's own default and carries on. Better
# a sane encode with a default preset than a failed one with a rejected flag.
ALL_PRESETS = (NVENC_PRESETS
               + ["ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow"]
               + ["speed", "balanced", "quality"])

# ---------------------------------------------------------------- schema ----
# type:
#   bool    a checkbox
#   int     a number, with min/max/step
#   choice  one of `choices`
#   multi   any of `choices`, stored as a list
FIELDS: dict[str, list[dict]] = {
    "video": [
        # WHICH SILICON DOES THE WORK. Per library, like everything else here,
        # so a bulk rebuild of one library can sit on the CPU overnight while
        # another keeps the GPU. "Auto" means the best one that actually
        # passed the probe - see encoders.py for why being listed by ffmpeg is
        # not the same as being usable.
        {"key": "encoder_family", "label": "Encoder", "type": "choice",
         "choices": ["auto", "nvenc", "qsv", "amf", "cpu"], "default": "auto",
         "what": "Which encoder does the work for this library. auto picks "
                 "the best one this machine can actually run. nvenc is the "
                 "NVIDIA block, qsv is Intel QuickSync, amf is AMD, cpu is "
                 "software x264/x265 - slow and it will use every core, but "
                 "the most efficient per bit and it needs no special "
                 "hardware. A choice this machine cannot run falls back to "
                 "one it can, and says so in the log."},
        {"key": "h265_cq", "label": "HEVC quality (CQ)", "type": "int",
         "min": 1, "max": 51, "step": 1, "default": 22,
         "what": "Constant-quality target for the HEVC encoder. LOWER is "
                 "better quality and a bigger file; each step is roughly "
                 "10-15% of size. This is the setting to lower if drawn "
                 "animation shows banding in dark scenes — flat areas and "
                 "smooth gradients read as 'cheap' to a quality-targeted "
                 "encoder, so it hands them very few bits and motion turns "
                 "the banding into crawling blocks."},
        {"key": "h265_preset", "label": "HEVC preset", "type": "choice",
         "choices": ALL_PRESETS, "default": "p5",
         "what": "How hard the encoder works for the same quality target. "
                 "p1 is fastest, p7 slowest. p5 is where the curve flattens "
                 "on this card — p6 and p7 cost noticeably more time for a "
                 "fraction of a percent of size."},
        {"key": "h264_cq", "label": "H.264 quality (CQ)", "type": "int",
         "min": 1, "max": 51, "step": 1, "default": 20,
         "what": "Same meaning as the HEVC figure, on the older codec. Set "
                 "lower than the HEVC one because H.264 needs more bits for "
                 "the same picture. Only used for 8-bit SDR sources that are "
                 "not being routed to HEVC."},
        {"key": "h264_preset", "label": "H.264 preset", "type": "choice",
         "choices": ALL_PRESETS, "default": "p5",
         "what": "Speed against efficiency on the H.264 path. See the HEVC "
                 "preset."},
        {"key": "route_hevc", "label": "Send HEVC sources to HEVC",
         "type": "bool", "default": True,
         "what": "A file that is already HEVC is re-encoded as HEVC rather "
                 "than dropped to H.264. Turning this off means any HEVC "
                 "file that needs a rebuild comes back as an older, larger "
                 "codec."},
        {"key": "route_10bit", "label": "Send 10-bit sources to HEVC",
         "type": "bool", "default": True,
         "what": "h264_nvenc cannot encode 10-bit at all, so this is what "
                 "keeps a 10-bit source from being flattened to 8-bit. "
                 "Turning it off will visibly band anime."},
        {"key": "route_hdr", "label": "Send HDR sources to HEVC",
         "type": "bool", "default": True,
         "what": "HDR needs HEVC to carry its metadata. Off, an HDR file "
                 "that gets re-encoded loses the HDR."},
        {"key": "route_av1", "label": "Send AV1 sources to HEVC",
         "type": "bool", "default": True,
         "what": "Without this an 8-bit AV1 falls through to the H.264 "
                 "default — re-encoding to an OLDER codec, which is a size "
                 "and quality regression for no reason."},
        {"key": "convert_av1", "label": "Convert AV1 away", "type": "bool",
         "default": True,
         "what": "AV1 is treated as a source format to convert away from, "
                 "not a destination to leave alone. It direct-plays only on "
                 "clients that can decode it, and the ones that cannot fall "
                 "back to a CPU transcode — the exact thing this pipeline "
                 "exists to avoid. Turn this off once every client on the "
                 "server decodes AV1."},
        {"key": "maxrate_factor", "label": "Bitrate cap (x source)",
         "type": "int", "min": 0, "max": 500, "step": 5, "default": 150,
         "unit": "%",
         "what": "Ceiling on the encode bitrate, as a percentage of the "
                 "source. A quality-targeted encode of an already-efficient "
                 "source balloons — measured, a 1.7 Mbps source came out at "
                 "4.5 Mbps — wasting space and failing the size gate so the "
                 "work is thrown away. Bits above about 150% add nothing, "
                 "because the source is the quality ceiling anyway. 0 "
                 "removes the cap."},
        {"key": "maxrate_floor_mbps", "label": "Bitrate cap floor",
         "type": "int", "min": 0, "max": 100, "step": 1, "default": 2,
         "unit": "Mbps",
         "what": "The cap never drops below this, so a very low-bitrate "
                 "source is not squeezed to the point of falling apart."},
        {"key": "strip_dv", "label": "Remove Dolby Vision (keep HDR10)",
         "type": "bool", "default": True,
         "what": "Removes the Dolby Vision layer so the file direct-plays as "
                 "HDR10 on TVs without DV, which would otherwise have to "
                 "convert it. The picture is NOT re-encoded — the layer is "
                 "dropped losslessly with a stream copy, so nothing is lost "
                 "but the DV."},
        {"parent": "strip_dv", "key": "strip_dv_profiles", "label": "Dolby Vision profiles to strip",
         "type": "multi", "choices": ["5", "7", "8"], "default": ["7", "8"],
         "what": "Profiles 7 and 8 carry an HDR10 base layer, so removing "
                 "the DV leaves a complete picture. Profile 5 does NOT — its "
                 "colour is only correct after DV processing, and stripping "
                 "it produces a washed-out file. Leave 5 unticked unless you "
                 "know otherwise."},
        {"key": "shrink_enabled", "label": "Shrink oversized files",
         "type": "bool", "default": False,
         "what": "Re-encodes files whose bitrate is above the ceiling for "
                 "their resolution. OFF, and deliberately: with animation "
                 "excluded and the ceiling at a level that does not wreck "
                 "the picture, only 17 files in the whole library qualify. "
                 "That is not a library-scale saving and it is a destructive "
                 "rule to carry for it."},
        {"parent": "shrink_enabled", "key": "shrink_1080_mbps", "label": "Shrink ceiling — 1080p",
         "type": "int", "min": 0, "max": 200, "step": 1, "default": 22,
         "unit": "Mbps",
         "what": "Above this, a 1080p file is considered oversized. 22 is "
                 "measured, not guessed: a break-even sweep on this library "
                 "found 12.2 Mbps sources came out 34% LARGER, 20.2 Mbps "
                 "saved 5% for visible quality loss, and 22.5 Mbps saved 39% "
                 "at SSIM 0.964. 22 is the first point where the saving is "
                 "large and the quality cost is back under control."},
        {"parent": "shrink_enabled", "key": "shrink_2160_mbps", "label": "Shrink ceiling — 4K",
         "type": "int", "min": 0, "max": 400, "step": 1, "default": 40,
         "unit": "Mbps",
         "what": "Set deliberately high. There is no SDR 4K measurement in "
                 "this library at all, so this is parked well out of the way "
                 "rather than guessed at."},
        {"parent": "shrink_enabled", "key": "shrink_720_mbps", "label": "Shrink ceiling — 720p",
         "type": "int", "min": 0, "max": 100, "step": 1, "default": 12,
         "unit": "Mbps",
         "what": "Above this, a 720p file is considered oversized."},
        {"parent": "shrink_enabled", "key": "shrink_sd_mbps", "label": "Shrink ceiling — SD",
         "type": "int", "min": 0, "max": 100, "step": 1, "default": 6,
         "unit": "Mbps",
         "what": "Above this, an SD file is considered oversized."},
        {"parent": "shrink_enabled", "key": "shrink_include_hdr", "label": "Let the shrink touch HDR",
         "type": "bool", "default": False,
         "what": "Re-encoding HDR loses metadata and breaks Dolby Vision. "
                 "This only controls whether the SHRINK rule may pick an HDR "
                 "file for being large — a burn-in still re-encodes HDR "
                 "normally, which is a different path."},
        {"parent": "shrink_enabled", "key": "shrink_force_h265", "label": "Shrink converts to HEVC",
         "type": "bool", "default": False,
         "what": "Off, a shrink keeps the source codec (x264 stays x264). "
                 "Measured cost of that choice: H.264 to H.264 saves roughly "
                 "20 points less than H.264 to HEVC and holds a lower SSIM. "
                 "It is kept anyway because a codec change on 500 files is a "
                 "bigger risk to Sonarr and Radarr — they may decide the "
                 "file no longer matches the profile and re-download it — "
                 "than the extra space is worth."},
    ],
    "audio": [
        {"key": "surround_bitrate", "label": "Surround bitrate", "type": "int",
         "min": 96, "max": 1536, "step": 32, "default": 640, "unit": "kbps",
         "what": "The rate surround tracks are encoded to. 640 is the "
                 "standard E-AC3 5.1 rate and the one Plex decodes without "
                 "complaint."},
        {"key": "stereo_bitrate", "label": "Stereo bitrate", "type": "int",
         "min": 64, "max": 512, "step": 16, "default": 160, "unit": "kbps",
         "what": "The rate stereo and mono tracks are encoded to, as AAC."},
        {"key": "surround_max_channels", "label": "Channel ceiling",
         "type": "choice", "choices": ["2", "6", "8"], "default": "6",
         "what": "Anything above this is downmixed. 6 (5.1) because a 7.1 "
                 "track forces Plex to remix on the fly for a 5.1 or stereo "
                 "client, and it does that whether the eight channels "
                 "arrived as TrueHD or as E-AC3 — the channel count is what "
                 "costs, not the format."},
        {"key": "eac3_max_bitrate_k", "label": "E-AC3 re-encode threshold",
         "type": "int", "min": 0, "max": 2048, "step": 32, "default": 960,
         "unit": "kbps",
         "what": "E-AC3 above this is re-encoded down even though the codec "
                 "is already right. Being E-AC3 is not sufficient: Plex's "
                 "EAE decoder rejects high-bitrate E-AC3 outright — 'Cannot "
                 "group in blocks of 6!' looping thousands of times a second "
                 "— because frames at high rates carry fewer than the 6 "
                 "audio blocks EAE insists on. Verified five for five on "
                 "this library: three 1536k tracks failed, two 640k tracks "
                 "played. Set to 960 rather than 641 so 768k tracks, which "
                 "have not been seen to fail, are left alone until proven "
                 "guilty. 0 disables the check."},
        {"key": "copy_surround_if", "label": "Surround formats to copy",
         "type": "multi",
         "choices": ["eac3", "ac3", "aac", "dts", "truehd", "flac"],
         "default": ["eac3"],
         "what": "Surround tracks already in one of these formats are copied "
                 "untouched instead of re-encoded, which is always better "
                 "than any re-encode. Adding a format here is a claim that "
                 "every client on this server direct-plays it."},
        {"key": "copy_stereo_if", "label": "Stereo formats to copy",
         "type": "multi",
         "choices": ["aac", "ac3", "eac3", "mp3", "opus", "flac"],
         "default": ["aac"],
         "what": "Stereo tracks already in one of these formats are copied "
                 "untouched. AAC stereo is the one format every device plays "
                 "without help."},
        {"key": "dedupe_per_lang", "label": "Keep only the best track per language",
         "type": "bool", "default": True,
         "what": "Where a file carries two tracks in the same language, keep "
                 "one. Best means most channels, then already-compatible "
                 "(copied rather than re-encoded), then lossless, then "
                 "bitrate. Two UNTAGGED tracks are never deduplicated "
                 "against each other — 'und' is the absence of a language, "
                 "not a language, and the one time that fired in anger the "
                 "'spare' was the Japanese half of a dual-audio release."},
        {"key": "tag_untagged_audio",
         "label": "Name the language on untagged audio", "type": "bool",
         "default": True,
         "what": "A track with no language tag is not neutral — Sonarr and "
                 "Radarr fill in English for it, and so do most players. That "
                 "is where a Japanese episode ends up described as English "
                 "audio, and from there the language rules on the Subtitles "
                 "page are working from a false premise. Two independent ways "
                 "nuarr learns the real answer. LISTENING — when the Whisper "
                 "language identifier is installed (Settings → Whisper), three "
                 "30-second windows from the middle of the file are run "
                 "through it, all of which have to agree, and what was heard "
                 "is written. INFERENCE — needs nothing installed: one audio "
                 "track, no tag, and full dialogue subtitles means the "
                 "title's original language from TMDB/TheTVDB. A system "
                 "without Whisper is not missing the feature, it simply runs "
                 "on inference alone; installing Whisper later upgrades the "
                 "answer to what the audio actually contains, including the "
                 "raws with no subtitles that inference must skip. Either "
                 "way, only ever fills in a blank; a track that already "
                 "states a language is never overwritten, because a dub "
                 "correctly tagged English must stay that way. Metadata only "
                 "— the audio is stream-copied, so this never costs an "
                 "encode.",
         # THE SAME SETTING, DESCRIBED FOR THE MACHINE IT IS ON. Half the text
         # above is about a component that may not be installed, and on a
         # machine that cannot run it that half is not background reading, it
         # is instructions for something that will never happen. The feature
         # itself still works - inference needs nothing - so the setting stays
         # and only the explanation narrows.
         "what_no_whisper":
             "A track with no language tag is not neutral — Sonarr and "
             "Radarr fill in English for it, and so do most players. That is "
             "where a Japanese episode ends up described as English audio, "
             "and from there the language rules on the Subtitles page are "
             "working from a false premise. nuarr fills the blank by "
             "INFERENCE, which needs nothing installed: one audio track, no "
             "tag, and full dialogue subtitles means the title's original "
             "language from TMDB/TheTVDB. Only ever fills in a blank; a "
             "track that already states a language is never overwritten, "
             "because a dub correctly tagged English must stay that way. "
             "Metadata only — the audio is stream-copied, so this never "
             "costs an encode."},
        {"parent": "tag_untagged_audio", "key": "tag_untagged_min_conf",
         # Whisper-only: it grades how sure the LISTENER has to be. With no
         # listener there is nothing for it to govern, so it is not shown.
         "needs": "whisper",
         "label": "Minimum confidence to trust what was heard (%)",
         "type": "int", "default": 60, "min": 50, "max": 99,
         "what": "How sure the detector has to be before its answer is used. "
                 "All three sample windows must agree on the language before "
                 "confidence is even considered — disagreement is reported as "
                 "unknown rather than settled by majority vote, because two "
                 "windows out of three is the signature of a dual-language "
                 "track or a long musical stretch, and both deserve a human "
                 "look. Measured on this library: correct calls land at 0.94 "
                 "to 1.00, so anything below 0.60 is not a close call, it is "
                 "the detector telling you it has nothing."},
        {"key": "drop_commentary", "label": "Drop commentary tracks",
         "type": "bool", "default": True,
         "what": "Director commentary is not something you sit down to "
                 "watch, and it makes the file bigger. Matched on the track "
                 "title."},
    ],
}

FIELD_BY_KEY = {side: {f["key"]: f for f in fields}
                for side, fields in FIELDS.items()}

# Per-kind starting points, applied on top of the field defaults above.
# EMPTY ON PURPOSE for now: rules.CONFIG had one answer for the whole library,
# so seeding anything different here would silently change what nuarr does the
# first time this module loads. The hook exists because the obvious first use
# of this page is a lower CQ for anime, and when that is measured it belongs
# here as the new starting point rather than as something everyone has to
# discover and set by hand.
KIND_SEEDS: dict[str, dict] = {
    "anime": {},
    "animation": {},
    "live": {},
}


def defaults_for(library: str = "") -> dict:
    """The starting point for a library: field defaults plus any kind seed."""
    out: dict = {}
    for side, fields in FIELDS.items():
        out[side] = {f["key"]: json.loads(json.dumps(f["default"]))
                     for f in fields}
    seed = KIND_SEEDS.get(kind_for(library=library) if library else "live", {})
    for side, vals in (seed or {}).items():
        if side in out:
            out[side].update(json.loads(json.dumps(vals)))
    return out


def _coerce(field: dict, value):
    """One submitted value, forced into the shape the planner can rely on.

    Everything that reads these settings is downstream of a user typing into a
    box. A CQ of "" or a channel ceiling of "six" must not reach ffmpeg, and
    must not raise either - an unusable value falls back to the default, which
    is always safe by construction.
    """
    t = field["type"]
    try:
        if t == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if t == "int":
            n = int(round(float(value)))
            lo, hi = field.get("min"), field.get("max")
            if lo is not None:
                n = max(lo, n)
            if hi is not None:
                n = min(hi, n)
            return n
        if t == "choice":
            s = str(value).strip()
            return s if s in field["choices"] else field["default"]
        if t == "multi":
            got = value if isinstance(value, list) else [value]
            keep = [str(x).strip() for x in got
                    if str(x).strip() in field["choices"]]
            # Order by the declared choices so a stored list is stable and two
            # equivalent selections compare equal.
            return [c for c in field["choices"] if c in keep]
    except (TypeError, ValueError):
        pass
    return json.loads(json.dumps(field["default"]))


def _norm(got: dict, into: dict, side: str) -> None:
    for key, val in (got or {}).items():
        f = FIELD_BY_KEY[side].get(key)
        if f is not None:
            into[key] = _coerce(f, val)


def _stored() -> dict:
    try:
        with cursor() as cur:
            r = cur.execute("SELECT v FROM kv WHERE k=?", (_KEY,)).fetchone()
        if r:
            return json.loads(r["v"]) or {}
    except Exception:
        pass
    return {}


def _write(pol: dict) -> None:
    with cursor() as cur:
        cur.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (_KEY, json.dumps(pol)))


def load() -> dict:
    """Settings for every CURRENT library, filled in from the defaults."""
    raw = _stored()
    pol: dict = {}
    for name in libraries():
        base = defaults_for(name)
        got = raw.get(name) or {}
        for side in SIDES:
            _norm(got.get(side) or {}, base[side], side)
        pol[name] = base
    return pol


def normalise(pol: dict, base: dict | None = None) -> dict:
    """Apply submitted settings on top of a base WITHOUT storing them."""
    out = json.loads(json.dumps(base if base is not None else load()))
    for name, sides in (pol or {}).items():
        target = out.get(name) or defaults_for(name)
        for side in SIDES:
            _norm((sides or {}).get(side) or {}, target[side], side)
        out[name] = target
    return out


def save(pol: dict) -> dict:
    """Store settings. Libraries not mentioned keep whatever they had."""
    merged = _stored()
    for name, sides in (pol or {}).items():
        base = json.loads(json.dumps(merged.get(name) or defaults_for(name)))
        for side in SIDES:
            _norm((sides or {}).get(side) or {}, base[side], side)
        merged[name] = base
    _write(merged)
    return load()


def reset(library: str, side: str = "", key: str = "") -> dict:
    """Put one field, one side, or a whole library back to its default.

    Per-field reset is the reason the defaults live in the schema rather than
    in a comment. Changing a number is easy; remembering what it was before is
    not, and 'what did this used to be' is the question you have when an encode
    starts looking wrong.
    """
    merged = _stored()
    base = defaults_for(library)
    cur_val = json.loads(json.dumps(merged.get(library) or base))
    if key and side:
        f = FIELD_BY_KEY.get(side, {}).get(key)
        if f is not None:
            cur_val.setdefault(side, {})[key] = json.loads(
                json.dumps(base[side][key]))
    elif side:
        cur_val[side] = json.loads(json.dumps(base[side]))
    else:
        cur_val = base
    merged[library] = cur_val
    _write(merged)
    return load()


_CACHE: dict = {"at": 0.0, "pol": None}


def for_library(name: str, side: str) -> dict:
    """The settings the planner should apply to a file in this library.

    Cached for a second. decide() is called for every file in a scan - tens of
    thousands of times - and each call would otherwise be a SQLite read on
    whatever thread the scan is using. The invariant that matters is that a
    saved change is picked up promptly, and one second is well inside 'before
    the next job starts'.
    """
    import time
    now = time.time()
    pol = _CACHE.get("pol")
    if pol is None or now - _CACHE["at"] > 1.0:
        try:
            pol = load()
        except Exception:
            pol = {}
        _CACHE.update(at=now, pol=pol)
    got = (pol or {}).get(name)
    if got and side in got:
        return got[side]
    return defaults_for(name)[side]


def invalidate() -> None:
    _CACHE.update(at=0.0, pol=None)


def describe() -> dict:
    """Everything the settings page needs to draw itself.

    The page is generated from this rather than hand-written, so a field added
    here appears there with its explanation, its range and its reset control
    already wired. The two cannot drift.
    """
    libs = libraries()
    # WHAT THIS MACHINE CAN ACTUALLY DO, shipped with the schema so the page
    # can narrow the preset list to the chosen encoder instead of offering
    # every family's ladder at once. Without it the box would happily let
    # someone pick "veryslow" for NVENC, which NVENC has never heard of.
    enc = {}
    try:
        from . import encoders as _enc
        enc = {
            "families": {f: {"label": s["label"],
                             "presets": s["presets"],
                             "default_preset": s["default_preset"],
                             "note": s["note"]}
                         for f, s in _enc.FAMILIES.items()},
            "usable": _enc.usable(),
            "auto": _enc.resolve("auto")[0],
            "order": _enc.ORDER,
        }
    except Exception:                                    # noqa: BLE001
        pass
    # THE SCHEMA, NARROWED TO THIS MACHINE. A field marked `needs` describes a
    # component that may not be here; showing it anyway means a control that
    # governs nothing, under an explanation of a thing that cannot happen.
    # Done here rather than in the page because the page is generated from
    # this - one filter, and every renderer agrees.
    whisper_ok = True
    try:
        from . import audiolang as _al
        whisper_ok = _al.usable()
    except Exception:                                    # noqa: BLE001
        pass
    # FIELDS is {"video": [...], "audio": [...]}, not a flat list - filtering
    # it as one cost a 500 on the whole codec page until the traceback said so.
    fields: dict = {}
    for side, items in FIELDS.items():
        kept = []
        for f in items:
            if f.get("needs") == "whisper" and not whisper_ok:
                continue
            g = dict(f)
            if not whisper_ok and g.get("what_no_whisper"):
                g["what"] = g["what_no_whisper"]
            g.pop("what_no_whisper", None)
            kept.append(g)
        fields[side] = kept
    return {
        "fields": fields,
        "whisper_ok": whisper_ok,
        "policy": load(),
        "defaults": {n: defaults_for(n) for n in libs},
        "encoders": enc,
        "libraries": [{"name": n,
                       "kind": kind_for(library=n),
                       "kind_label": KIND_LABELS[kind_for(library=n)]}
                      for n in libs],
    }
