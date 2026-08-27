"""
Filename quality ranking.

Used to decide which file in a duplicate pair survives. Weighted to match how
the Nu profiles score in Sonarr/Radarr, with one deliberate ordering choice:

    RESOLUTION OUTRANKS SOURCE.

A 2160p WEBDL beats a 1080p Remux here. That is the preference the 2160p
profiles express, and it is the ordering that gets 'The Boys S05E08' right -
the untracked 1080p file there is NEWER than the tracked 2160p HDR10 copy, so
any mtime- or recency-based rule would throw away the better file.

Size is only ever a tiebreaker. A bloated WEBRip should not beat a clean
Bluray encode, which is exactly what happens if you sort on bytes.
"""
from __future__ import annotations

import os
import re

# resolution dominates - see module docstring
RESOLUTION = [
    (re.compile(r"\b(2160p|4k|uhd)\b", re.I), 5000),
    (re.compile(r"\b1080[pi]\b", re.I), 3000),
    (re.compile(r"\b720p\b", re.I), 1500),
    (re.compile(r"\b(480[pi]|576[pi])\b", re.I), 500),
]

SOURCE = [
    (re.compile(r"\bremux\b", re.I), 800),
    (re.compile(r"\b(bluray|blu-ray|bdrip|brrip)\b", re.I), 600),
    (re.compile(r"\bweb-?dl\b", re.I), 400),
    (re.compile(r"\bweb-?rip\b", re.I), 250),
    (re.compile(r"\bhdtv\b", re.I), 100),
    (re.compile(r"\b(dvd|dvdrip)\b", re.I), 50),
]

HDR = [
    (re.compile(r"\bdv\s*hdr10\+?\b|\bdolby\s*vision\b", re.I), 70),
    (re.compile(r"\bdv\b", re.I), 60),
    (re.compile(r"\bhdr10\+", re.I), 50),
    (re.compile(r"\bhdr10\b", re.I), 40),
    (re.compile(r"\bhdr\b", re.I), 30),
]

AUDIO = [
    (re.compile(r"\btruehd\s*atmos\b", re.I), 90),
    (re.compile(r"\bdts[-\s]?x\b", re.I), 85),
    (re.compile(r"\btruehd\b", re.I), 80),
    (re.compile(r"\bdts[-\s]?hd(\s*ma)?\b", re.I), 70),
    (re.compile(r"\beac3\s*atmos\b", re.I), 55),
    (re.compile(r"\bdts\b", re.I), 50),
    (re.compile(r"\beac3\b|\bddp?\+", re.I), 40),
    (re.compile(r"\bac3\b|\bdd\b", re.I), 30),
    (re.compile(r"\bflac\b", re.I), 35),
    (re.compile(r"\baac\b", re.I), 20),
    (re.compile(r"\bmp3\b", re.I), 5),
]

_10BIT = re.compile(r"\b10-?bit\b", re.I)
_PROPER = re.compile(r"\b(proper|repack)\b", re.I)
_CHANNELS = re.compile(r"\b([0-9])\.([0-9])\b")


def _first(table, text: str) -> int:
    for rx, pts in table:
        if rx.search(text):
            return pts
    return 0


def score(path: str, size: int | None = None) -> int:
    """Quality score for a media filename. Higher is better."""
    t = os.path.basename(path)
    s = (_first(RESOLUTION, t) + _first(SOURCE, t)
         + _first(HDR, t) + _first(AUDIO, t))
    if _10BIT.search(t):
        s += 20
    if _PROPER.search(t):
        s += 15
    m = _CHANNELS.search(t)
    if m:  # 7.1 over 5.1 over 2.0
        s += int(m.group(1)) * 2
    return s


def explain(path: str) -> str:
    """Human-readable reason, so a deletion decision can be audited."""
    t = os.path.basename(path)
    bits = []
    for name, table in (("res", RESOLUTION), ("src", SOURCE),
                        ("hdr", HDR), ("aud", AUDIO)):
        for rx, _ in table:
            m = rx.search(t)
            if m:
                bits.append(m.group(0))
                break
    if _PROPER.search(t):
        bits.append("proper")
    return " ".join(bits) or "no tags"


def tier(path: str) -> tuple[int, int]:
    """(resolution, source) - the two tags that should bound file size."""
    t = os.path.basename(path)
    return _first(RESOLUTION, t), _first(SOURCE, t)


def mislabelled(winner: dict, loser: dict, ratio: float = 1.25) -> bool:
    """True if the size gap contradicts the tags, so the tags can't be trusted.

    Within one resolution+source tier, size tracks bitrate. A file claiming
    'Remux-2160p' at 6 GB next to another claiming the same at 51 GB means one
    of them is lying, and the ranker cannot tell which. Real case: Stardust,
    where a 6.22 GB 'Remux' outscored a 51.45 GB remux purely on an audio tag.

    Only compares within a tier - a WEBDL legitimately beats a much larger
    WEBRip, and that is not a mislabel.
    """
    if tier(winner["path"]) != tier(loser["path"]):
        return False
    ws, ls = winner.get("size") or 0, loser.get("size") or 0
    return ws > 0 and ls > ws * ratio


def better(a: dict, b: dict) -> dict:
    """Pick the better of two file rows, size breaking a tie."""
    sa, sb = score(a["path"]), score(b["path"])
    if sa != sb:
        return a if sa > sb else b
    return a if (a.get("size") or 0) >= (b.get("size") or 0) else b
