r"""Tell Plex that one folder changed, and nothing more.

WHY THIS EXISTS. nuarr rewrites a file, tells Sonarr or Radarr to rescan it,
and stops there - so the arr's record is correct within seconds while Plex goes
on showing the streams the file had before. Erik hit it on Redline: the remux
had already dropped both picture subtitle tracks, ffprobe showed one SRT left,
and Plex was still offering "English (PGS)" twice in the subtitle menu.

Plex was not wrong. Nothing had asked it to look. On this server the settings
are FSEventLibraryUpdatesEnabled=True and ScheduledLibraryUpdateInterval=86400
- filesystem watching plus a daily sweep - but the library lives on a DrivePool
virtual volume, where directory-change notifications are unreliable, which
leaves "some time in the next 24 hours" as the honest answer.

A PARTIAL SCAN, NOT A LIBRARY SCAN. `/library/sections/{key}/refresh?path=...`
asks Plex to look at ONE directory. Anime Shows is thousands of folders; asking
Plex to walk all of them because one episode changed would trade a stale menu
for a scanner that never stops running, and on a library this size that is the
worse of the two.

AND THEN ANALYZE, BECAUSE THE SCAN IS NOT ENOUGH. Measured on Redline: after
the partial scan Plex had the new SIZE - 5,819,264,209 bytes, exactly right -
and was still listing both picture subtitle tracks in the menu. Scanning
updates the part; re-reading the streams inside it is a separate job, and
`PUT /library/metadata/{ratingKey}/analyze` is what does it. That call fixed
the menu in under six seconds. A refresh alone would have left the symptom Erik
reported and only shortened the wait for it to fix itself.

FINDING THE ITEM WITHOUT SEARCHING FOR IT. `/library/sections/{key}/all?file=`
narrows to the right neighbourhood in one request - no title matching, no fuzzy
results. It does NOT narrow to one file: on a show section it appears to filter
by directory and returns the whole season, so rating_key_for() matches on
Part.file itself. Episodes need `type=4` and movies `type=1` (without it, a show
section answers 500), which is why the section table below remembers what kind
of library each one is.

DEBOUNCED PER FOLDER, for the same reason the arr refresh is: a finishing
season lands eight episodes into one folder in a few minutes, and eight
identical partial scans of that folder is seven scans of nothing.

THIS MODULE NO LONGER DELIVERS ANYTHING. It used to be called straight from
the commit path on a daemon thread, which meant a busy or restarting Plex lost
the notification with nothing on disk to say it was owed. plexqueue.py owns
delivery now - a row per file, retried with backoff, verified against ffprobe
before it closes - and what is left here is the vocabulary both it and the
agreement check speak: where a path lives in Plex, how to ask, and what it
means for the two to agree.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.request

from . import joblog
from .config import SETTINGS

# Section key per library root, learned from Plex rather than configured here.
# Plex's own key numbering is the only correct answer and it differs per
# server; a mapping written into nuarr would be right on this machine and
# quietly wrong on anyone else's.
_SECTIONS: dict = {"at": 0.0, "map": [], "error": False}   # [(root, key, kind)]
_SECTIONS_TTL = 900.0

_SENT: dict[str, float] = {}
_DEBOUNCE_S = 90.0
_LOCK = threading.Lock()


def norm_path(p: str) -> str:
    r"""One spelling of a path, for comparing ours against Plex's.

    PLEX USES THE EXTENDED-LENGTH PREFIX AND NUARR DOES NOT. For any path over
    the 260-character limit - which on this library means the long anime titles
    - Plex stores and reports:

        \\?\P:\Anime Shows\From Overshadowed to Overpowered…\S01E11…mkv

    where nuarr has the same path without the leading \\?\. A plain string
    comparison therefore never matched for exactly those files, and the failure
    was silent in both directions: the catch-up queue reported "Plex has not
    indexed this path yet" for ever - 44 rows were stuck on it - and the
    agreement check skipped them as "Plex has it, nuarr does not manage it", so
    the one system that would have caught the other's blind spot shared it.

    Fourteen of three thousand sampled items carry the prefix. Small, and
    permanently wrong rather than occasionally.
    """
    q = (p or "").strip()
    if q.startswith("\\\\?\\UNC\\"):
        q = "\\\\" + q[8:]
    elif q.startswith("\\\\?\\"):
        q = q[4:]
    return q.replace("/", "\\").rstrip("\\").lower()


def _plex() -> tuple[str, str]:
    return (SETTINGS.plex_url or "").rstrip("/"), (SETTINGS.plex_token or "")


# ---------------------------------------------------------------------------
# WHAT "PLEX AGREES WITH THE FILE" MEANS, in one place, because two callers ask
# it: the notify queue, which will not call a notification delivered until Plex
# demonstrably re-read the file, and the agreement check, which asks it of the
# whole library.
#
# THE JOIN IS THE STREAM INDEX. Plex reports each stream's index within the
# file, which is ffprobe's index, so the two lists line up exactly rather than
# by guesswork about ordering. Removing a track changes the index set, which is
# precisely the change nuarr makes.
#
# Codec names differ between the two - ffprobe says subrip and hdmv_pgs_subtitle
# where Plex says srt and pgs - so they are mapped. An unmapped codec compares
# as "unknown on both sides" rather than as a disagreement: a check that invents
# mismatches is worse than one that misses them, because every invented one
# costs a re-analysis of a file that was already right.
_PLEX_CODEC = {
    "subrip": "srt", "hdmv_pgs_subtitle": "pgs", "dvd_subtitle": "vobsub",
    "ssa": "ass", "ass": "ass", "mov_text": "mov_text", "webvtt": "webvtt",
    "dts": "dca", "dca": "dca", "truehd": "truehd", "eac3": "eac3",
    "ac3": "ac3", "aac": "aac", "flac": "flac", "opus": "opus", "mp3": "mp3",
    "vorbis": "vorbis", "h264": "h264", "hevc": "hevc", "av1": "av1",
    "mpeg2video": "mpeg2video", "vc1": "vc1", "mpeg4": "mpeg4",
}
_TYPE_OF = {"video": 1, "audio": 2, "subtitle": 3}


_PLEX_NAMES = set(_PLEX_CODEC.values())


def _codec(name: str) -> str:
    """ffprobe's name for a codec, in Plex's spelling."""
    return _PLEX_CODEC.get((name or "").strip().lower(), "")


def _codec_plex(name: str) -> str:
    """Plex's own name, kept if it is one we recognise.

    THE MAP ONLY RAN ONE WAY at first, and both sides were passed through it -
    so Plex's "srt" was looked up in a table keyed by ffprobe's "subrip", came
    back empty, and every codec comparison silently became "unknown on both
    sides". Structural changes were still caught, because those show up as
    different stream counts, but a track that changed codec in place would have
    compared as identical.
    """
    n = (name or "").strip().lower()
    return n if n in _PLEX_NAMES else ""


def probe_sig(probe: dict) -> tuple:
    """(index, type, codec, default, forced) per stream, from an ffprobe dict."""
    out = []
    for s in (probe or {}).get("streams") or []:
        t = _TYPE_OF.get(s.get("codec_type") or "")
        if not t:
            continue
        disp = s.get("disposition") or {}
        # EMBEDDED COVER ART IS NOT A VIDEO TRACK. ffprobe reports a poster
        # inside an mkv as a second video stream; Plex does not list it at all,
        # so counting it reports every file with artwork as one stream out of
        # step. Accused (US) was the one that showed it.
        if disp.get("attached_pic"):
            continue
        out.append((int(s.get("index") or 0), t, _codec(s.get("codec_name")),
                    bool(disp.get("default")), bool(disp.get("forced"))))
    return tuple(sorted(out))


def plex_sig(part: dict) -> tuple:
    """The same shape, from a Plex Part.

    Two kinds of stream are listed here that are not streams in the file, and
    both had to be found the hard way:

    EXTERNAL SUBTITLES carry a `key` and live beside the file rather than in
    it. Counting them reports every sidecar as a stream the file has lost.

    CLOSED CAPTIONS carry `embeddedInVideo` and live INSIDE the video stream.
    ffprobe does not list them separately - it sets closedCaptions on the video
    stream instead - so Plex appears to have one extra subtitle, at index 0,
    colliding with the video. Clear and Present Danger was the file that showed
    it, and without this it is reported as a disagreement for ever, because no
    amount of re-analysis will make Plex stop mentioning them.
    """
    out = []
    for s in (part or {}).get("Stream") or []:
        t = int(s.get("streamType") or 0)
        if t not in (1, 2, 3) or s.get("key") or s.get("embeddedInVideo"):
            continue
        out.append((int(s.get("index") or 0), t, _codec_plex(s.get("codec")),
                    bool(s.get("default")), bool(s.get("forced"))))
    return tuple(sorted(out))


def sig_differs(a: tuple, b: tuple) -> bool:
    """Do these two describe different files? Unknown codecs never disagree."""
    if len(a) != len(b):
        return True
    for x, y in zip(a, b):
        if x[0] != y[0] or x[1] != y[1] or x[3] != y[3] or x[4] != y[4]:
            return True
        if x[2] and y[2] and x[2] != y[2]:
            return True
    return False


def describe(sig: tuple) -> str:
    """A signature in words, for a person reading one log line.

    Lives here rather than in either caller because both the queue and the
    agreement check have to say the same thing about the same file, and two
    phrasings of one fact is how a log stops being evidence.
    """
    names = {1: "video", 2: "audio", 3: "subtitle"}
    parts = []
    for _idx, t, codec, dflt, forced in sig:
        bit = f"{names.get(t, '?')} {codec or '?'}"
        if dflt:
            bit += " (default)"
        if forced:
            bit += " (forced)"
        parts.append(bit)
    return ", ".join(parts) or "nothing"


def summarise(sig: tuple) -> str:
    """The same thing in counts, for when the full list would be a paragraph.

    A file with eighty subtitle tracks - Plex genuinely believed one had - is
    not readable as a list, and the number is the point anyway.
    """
    n = {1: 0, 2: 0, 3: 0}
    for _idx, t, _c, _d, _f in sig:
        if t in n:
            n[t] += 1
    return (f"{n[1]} video, {n[2]} audio, {n[3]} subtitle"
            + ("s" if n[3] != 1 else ""))


def diff_words(got: tuple, want: tuple, limit: int = 3) -> str:
    r"""What actually differs between Plex's idea and the file, in words.

    WHY NOT JUST COUNT THE STREAMS. The first version of this said "Plex had
    1 video, 2 audio, 2 subtitles, the file has 1 video, 2 audio, 2 subtitles"
    - a line that reports a disagreement and then prints the same thing twice,
    which reads as a bug in the check rather than a fact about the file. That
    file was a real and total mismatch: Plex held truehd audio and picture
    subtitles from before nuarr transcoded it, and the file had eac3, aac and
    an OCR'd text track. Same shape, entirely different contents.

    So this compares position by position and names the first few differences.
    Counts are still used when the LENGTHS differ, because "eighty-three
    subtitle tracks against three" is the whole story and listing them is not.
    """
    kinds = {1: "video", 2: "audio", 3: "subtitle"}
    if len(got) != len(want):
        return (f"Plex had {summarise(got)}, the file has {summarise(want)}")
    bits = []
    for a, b in zip(got, want):
        if a == b:
            continue
        if len(bits) >= limit:
            bits.append("…")
            break
        what = kinds.get(a[1], "stream")
        if a[2] != b[2] and a[2] and b[2]:
            bits.append(f"{what} {a[0]}: Plex says {a[2]}, the file has {b[2]}")
        elif a[3] != b[3]:
            bits.append(f"{what} {a[0]}: Plex has it "
                        f"{'on' if a[3] else 'off'} by default, the file has it "
                        f"{'on' if b[3] else 'off'}")
        elif a[4] != b[4]:
            bits.append(f"{what} {a[0]}: forced flag differs")
        else:
            bits.append(f"{what} {a[0]} differs")
    return "; ".join(bits) or "no visible difference"


def part_of(rating_key: str) -> dict:
    """One item's first Part, streams included. {} when Plex will not say."""
    import json
    try:
        d = json.loads(_get(f"/library/metadata/{rating_key}", timeout=20))
        md = (d.get("MediaContainer") or {}).get("Metadata") or []
        return ((md[0].get("Media") or [{}])[0].get("Part") or [{}])[0]
    except Exception:                                        # noqa: BLE001
        return {}


def analyze(rating_key: str) -> bool:
    """Make Plex re-read this item's streams. The scan alone does not."""
    try:
        _get(f"/library/metadata/{rating_key}/analyze", timeout=20, method="PUT")
        return True
    except Exception:                                        # noqa: BLE001
        return False


def _get(path: str, timeout: float = 10.0, method: str = "GET") -> bytes:
    url, token = _plex()
    req = urllib.request.Request(
        url + path, method=method,
        headers={"X-Plex-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sections() -> list[tuple[str, str, int]]:
    """[(library root, section key, item type)], cached for fifteen minutes.

    The type is Plex's: 1 for a movie, 4 for an episode. It is carried here
    because the file lookup needs it - see the module docstring.
    """
    now = time.time()
    if _SECTIONS["map"] and now - _SECTIONS["at"] < _SECTIONS_TTL:
        return _SECTIONS["map"]
    import json
    out: list[tuple[str, str, int]] = []
    try:
        d = json.loads(_get("/library/sections"))
        for s in (d.get("MediaContainer") or {}).get("Directory") or []:
            kind = 4 if (s.get("type") or "") == "show" else 1
            for loc in s.get("Location") or []:
                p = (loc.get("path") or "").rstrip("\\/")
                if p:
                    out.append((p.lower(), str(s.get("key") or ""), kind))
    except Exception:                                        # noqa: BLE001
        # COULD NOT ASK IS NOT THE SAME AS THE ANSWER BEING NO. Without this
        # flag an unreachable Plex produced an empty section table, every path
        # looked like one Plex does not serve, and the queue closed each row as
        # delivered - losing exactly the notifications the queue exists to keep
        # through an outage. Caught by pointing this at a dead port.
        _SECTIONS["error"] = True
        return _SECTIONS["map"]                              # keep the last one
    # Longest root first: a nested library must win over the one that contains
    # it, or every title in it is refreshed against the wrong section.
    out.sort(key=lambda x: -len(x[0]))
    _SECTIONS.update(at=now, map=out, error=False)
    return out


def section_for(path: str) -> tuple[str, int]:
    """(section key, item type) for a path.

    ("", 0)  Plex serves libraries, and none of them contains this path.
    ("", -1) Plex could not be asked, so nothing is known either way.
    """
    p = (path or "").lower()
    got = _sections()
    for root, key, kind in got:
        if p.startswith(root):
            return key, kind
    if not got and _SECTIONS.get("error"):
        return "", -1
    return "", 0


def rating_key_for(file_path: str, key: str, kind: int) -> str:
    r"""The Plex item that owns this exact file, or "" if there is not one.

    `?file=` IS NOT AN EXACT MATCH, and taking the first result is how this
    check spent an evening comparing the wrong episode. Asked for
    `Demon Lord, Retry! - S01E06`, a show section returns all twenty-four
    episodes of that season - Plex appears to filter on the DIRECTORY - and
    Metadata[0] is episode one. So the queue read episode one's streams,
    compared them against episode six's file, found a disagreement it could
    never resolve, and asked Plex to re-analyse the wrong item. On a movie
    section it happened to work, because a movie folder holds one item, which
    is exactly the kind of luck that hides a bug in testing.

    The response carries each item's Part.file, so the match is made here,
    exactly, and a path Plex does not hold returns "" rather than a neighbour.
    """
    import json
    want = norm_path(file_path)
    if not want:
        return ""
    try:
        d = json.loads(_get(f"/library/sections/{key}/all?type={kind}&file="
                            + urllib.parse.quote(file_path), timeout=30))
        md = (d.get("MediaContainer") or {}).get("Metadata") or []
    except Exception:                                        # noqa: BLE001
        return ""
    for m in md:
        for media in m.get("Media") or []:
            for part in media.get("Part") or []:
                if norm_path(part.get("file")) == want:
                    return str(m.get("ratingKey") or "")
    return ""


def refresh_path(file_path: str, why: str = "", job_id: str = "") -> bool:
    """Scan the folder, then make Plex re-read this file's streams.

    Two steps because they are two different jobs inside Plex - see the module
    docstring. Never raises; every failure leaves Plex's own daily scan as the
    backstop it has always been.
    """
    import os
    url, token = _plex()
    if not url or not token or not file_path:
        return False
    folder = os.path.dirname(file_path)
    if not folder:
        return False
    key, kind = section_for(folder)
    if not key:
        # A path Plex does not serve is not an error - nuarr manages libraries
        # Plex may not have, and saying so once per commit would be noise.
        return False
    # THE FOLDER IS DEBOUNCED, THE FILE IS NOT. A finishing season lands eight
    # episodes in one folder in a few minutes and one scan covers them all -
    # but each of those files needs its own streams re-read, so the analyze
    # below runs every time.
    scan = False
    with _LOCK:
        now = time.time()
        if now - _SENT.get(folder.lower(), 0.0) >= _DEBOUNCE_S:
            scan = True
            _SENT[folder.lower()] = now
        # Bounded: one entry per folder touched, swept past the window.
        if len(_SENT) > 512:
            for k in [k for k, t in _SENT.items() if now - t > _DEBOUNCE_S]:
                _SENT.pop(k, None)
    if scan:
        try:
            _get(f"/library/sections/{key}/refresh?path="
                 + urllib.parse.quote(folder), timeout=15)
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"Plex would not take a refresh for this folder: "
                       f"{type(e).__name__}: {e}", "debug", job_id or None)

    # A file replaced in place is already indexed, so the lookup normally
    # answers at once. A renamed or brand-new one has to wait for the scan
    # above to reach it - hence the retry rather than a single attempt.
    rk = ""
    for wait in (0.0, 8.0, 20.0):
        if wait:
            time.sleep(wait)
        rk = rating_key_for(file_path, key, kind)
        if rk:
            break
    if not rk:
        joblog.log("Plex has not indexed this file yet - its own scan will "
                   "pick up the change", "debug", job_id or None)
        return False
    try:
        _get(f"/library/metadata/{rk}/analyze", timeout=20, method="PUT")
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"Plex would not re-read this file: {type(e).__name__}: {e}",
                   "debug", job_id or None)
        return False
    joblog.log(f"asked Plex to re-read {os.path.basename(file_path)}"
               + (f" - {why}" if why else ""), "debug", job_id or None)
    return True


def refresh_async(file_path: str, why: str = "", job_id: str = "") -> None:
    """The commit path's entry point: never blocks, never raises."""
    try:
        threading.Thread(target=refresh_path, args=(file_path, why, job_id),
                         name="plex-refresh", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        pass


def moved_async(old_path: str, new_path: str, why: str = "",
                job_id: str = "") -> None:
    r"""A file that changed NAME, not just contents.

    BOTH ENDS, OR PLEX IS WRONG IN TWO DIRECTIONS. A rename leaves Plex holding
    a path that no longer exists and knowing nothing about the one that does -
    so the item plays back "file not found" until a scan reaches it, and the new
    file is invisible until a scan reaches that. Scanning only the destination
    fixes half of it and leaves a ghost behind.

    The arrs do the renaming here, not nuarr, which is exactly why this has to
    be said out loud: nothing in the rename path ever told Plex anything.
    """
    import os

    def _both() -> None:
        old_dir = os.path.dirname(old_path or "")
        new_dir = os.path.dirname(new_path or "")
        if old_dir and old_dir.lower() != new_dir.lower():
            key, _kind = section_for(old_dir)
            if key:
                try:
                    _get(f"/library/sections/{key}/refresh?path="
                         + urllib.parse.quote(old_dir), timeout=15)
                except Exception:                            # noqa: BLE001
                    pass                 # the destination scan still matters
        refresh_path(new_path, why, job_id)

    try:
        threading.Thread(target=_both, name="plex-moved", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        pass
