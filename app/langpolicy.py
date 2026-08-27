r"""Which audio and subtitle languages survive, per library.

WHY PER LIBRARY AND NOT PER "KIND"
----------------------------------
The first version of this had three fixed buckets - anime, animation, live
action - which was already better than the boolean it replaced, but it was
still a fixed list in the source. Add a "Documentaries" or "Foreign Film"
library and it would silently fall into "live action" with nobody told; remove
one and a dead entry would sit in the settings forever.

The libraries are the thing that actually exists. They are configured, they
have folders on the pool, files carry the name in a column, and the scanner
already walks them. So the policy is keyed on the library, the settings page
draws one section per library it finds, and a library added tomorrow appears
on its own with sensible defaults rather than needing a code change.

WHAT A NEW LIBRARY GETS
-----------------------
Not "nothing", and not "keep everything". It is seeded from the KIND its
folder name implies - anything starting Anime is anime, Animated/Animation is
animation, everything else is live action - which is exactly the rule that
used to be hardcoded, now demoted to a first guess that can be edited. A new
"Anime Docs" library therefore starts with dual audio rather than
English-only, which is almost certainly what was wanted.

A REMOVED LIBRARY KEEPS ITS SETTINGS
------------------------------------
Its entry stays in storage but stops being shown. Detaching a library to move
a disk and reattaching it should not silently reset a policy that was chosen
deliberately - and an invisible stored row costs nothing.

KEEP-ORIGINAL IS A FLAG, NOT A LANGUAGE
---------------------------------------
"The language it was made in" differs per title - Japanese for one show,
Korean for the next - so it cannot be an entry in a list of codes. It is
resolved per file from the TMDB/TheTVDB original language that origlang
stores. When that lookup fails there is nothing for it to act on, and
rules.decide() keeps every track rather than guessing.
"""
from __future__ import annotations

import json

from .db import cursor

# Still used - to SEED a library nobody has configured yet, and nothing else.
KINDS = ("anime", "animation", "live")
KIND_LABELS = {"anime": "Anime", "animation": "Animation",
               "live": "Live action"}

_KEY = "langpolicy.v2"
_KEY_V1 = "langpolicy.v1"

# The per-kind starting points. Straight from rules.CONFIG as it stood before
# any of this was configurable, so a fresh install behaves as it always did.
KIND_DEFAULTS: dict = {
    "anime": {
        "audio": {"keep_original": True, "langs": ["jpn", "eng", "und"]},
        "subs":  {"keep_original": False, "langs": ["eng"], "keep_untagged": True},
    },
    "animation": {
        "audio": {"keep_original": True, "langs": ["eng"]},
        "subs":  {"keep_original": False, "langs": ["eng"], "keep_untagged": True},
    },
    "live": {
        "audio": {"keep_original": True, "langs": ["eng"]},
        "subs":  {"keep_original": False, "langs": ["eng"], "keep_untagged": True},
    },
}


def kind_for(path: str = "", library: str = "") -> str:
    """The kind a library NAME implies. Only ever a default, never a rule.

    Folder-based, and folders only - never the filename. A plain substring
    test on the whole path matches "P:\\Movies\\Anime Club (2020).mkv", a
    live-action film, and would hand it the dual-audio policy.
    """
    def _k(name: str) -> str | None:
        s = (name or "").strip().lower()
        if not s:
            return None
        if s.startswith("anime"):
            return "anime"
        if s.startswith("animated") or s.startswith("animation"):
            return "animation"
        return None

    k = _k(library)
    if k:
        return k
    if library:
        return "live"
    parts = [s for s in (path or "").replace("/", "\\").split("\\") if s]
    for seg in parts[:-1]:
        k = _k(seg)
        if k:
            return k
    # NOT the metadata verdict, on purpose. This function's job is to seed a
    # LIBRARY's defaults, and a library is a folder - asking what one file in
    # it happens to be would make the default depend on whichever file was
    # looked at first. contentkind.for_file() combines the two per FILE, which
    # is the level the question is actually meaningful at.
    return "live"


def libraries() -> list[str]:
    """Every library nuarr is configured for, in configured order."""
    try:
        from .config import SETTINGS
        names = [l.name for l in SETTINGS.libraries if getattr(l, "enabled", True)]
        if names:
            return names
    except Exception:
        pass
    # Fall back to what the database has actually seen, so the settings page
    # still works if the config cannot be read for any reason.
    try:
        with cursor() as cur:
            return [r["library"] for r in cur.execute(
                "SELECT DISTINCT library FROM files WHERE library IS NOT NULL "
                "ORDER BY library")]
    except Exception:
        return []


def _seed(name: str) -> dict:
    return json.loads(json.dumps(KIND_DEFAULTS[kind_for(library=name)]))


def _norm(got: dict, into: dict) -> None:
    if isinstance(got.get("langs"), list):
        # Normalised on the way in, so a policy saved as "ENG " cannot quietly
        # fail to match a probe reporting "eng".
        into["langs"] = sorted({str(x).strip().lower()[:3]
                                for x in got["langs"] if str(x).strip()})
    for flag in ("keep_original", "keep_untagged"):
        if flag in got and flag in into:
            into[flag] = bool(got[flag])


def _stored() -> dict:
    try:
        with cursor() as cur:
            r = cur.execute("SELECT v FROM kv WHERE k=?", (_KEY,)).fetchone()
        if r:
            return json.loads(r["v"]) or {}
    except Exception:
        pass
    return {}


def _migrate_v1() -> dict:
    """Carry a three-kind policy across to per-library, once.

    Without this, everyone who set a policy under the old shape would have it
    silently reset to defaults by an upgrade - which is the same class of
    failure as a Profilarr sync flattening a profile, and just as invisible.
    """
    try:
        with cursor() as cur:
            r = cur.execute("SELECT v FROM kv WHERE k=?", (_KEY_V1,)).fetchone()
        if not r:
            return {}
        old = json.loads(r["v"]) or {}
    except Exception:
        return {}
    out: dict = {}
    for name in libraries():
        k = kind_for(library=name)
        if k in old:
            out[name] = json.loads(json.dumps(old[k]))
    return out


def load() -> dict:
    """Policy for every CURRENT library, filled in from the seeds."""
    raw = _stored()
    if not raw:
        raw = _migrate_v1()
        if raw:
            _write(raw)
    pol: dict = {}
    for name in libraries():
        base = _seed(name)
        got = raw.get(name) or {}
        for side in ("audio", "subs"):
            _norm(got.get(side) or {}, base[side])
        pol[name] = base
    return pol


def _write(pol: dict) -> None:
    with cursor() as cur:
        cur.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (_KEY, json.dumps(pol)))


def normalise(pol: dict, base: dict | None = None) -> dict:
    """Apply a submitted policy on top of a base WITHOUT storing it.

    Exists so the preview can be of the exact thing save() would write - same
    lowercasing, same three-letter truncation, same "a library not mentioned
    keeps what it had". A preview computed from raw form input would differ
    from the stored result in ways nobody could see.
    """
    out = json.loads(json.dumps(base if base is not None else load()))
    for name, sides in (pol or {}).items():
        target = out.get(name) or _seed(name)
        for side in ("audio", "subs"):
            _norm((sides or {}).get(side) or {}, target[side])
        out[name] = target
    return out


def save(pol: dict) -> dict:
    """Store a policy. Libraries not mentioned keep whatever they had."""
    merged = _stored() or _migrate_v1()
    for name, sides in (pol or {}).items():
        base = json.loads(json.dumps(merged.get(name) or _seed(name)))
        for side in ("audio", "subs"):
            _norm((sides or {}).get(side) or {}, base[side])
        merged[name] = base
    _write(merged)
    return load()


def for_library(name: str, side: str) -> dict:
    pol = load()
    if name in pol:
        return pol[name][side]
    return KIND_DEFAULTS[kind_for(library=name)][side]


def iso_languages() -> list[dict]:
    """The picker's list: every ISO 639-1 language, plus the 639-2/B spellings
    ffprobe actually emits (fre, ger, chi, dut...).

    Baked into a JSON file rather than computed from pycountry at runtime. The
    list does not change, and a settings page that cannot open because an
    optional dependency went missing is a worse trade than 20 KB on disk.
    """
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_iso639.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return [{"c": c, "a2": "", "n": c} for c in
                ("und", "eng", "jpn", "kor", "chi", "fre", "ger", "spa", "rus")]


def library_languages() -> dict:
    """Codes actually present, per library and side, with counts.

    Per library now rather than library-wide: the point of a per-library policy
    is deciding what THIS library contains, and a Japanese count driven by the
    anime shelf is noise when you are looking at the Movies section.
    """
    out: dict = {}
    try:
        # READS TWO SMALL COLUMNS, not 39,563 probe blobs. This used to join
        # file_probes and json.loads() every row - about 188 MB of JSON parsed
        # to produce a few hundred counters, on every open of the Subtitles
        # page. The tags are extracted at probe time now (jobs._store_probe).
        with cursor() as cur:
            rows = cur.execute(
                "SELECT library, audio_langs, sub_langs FROM files "
                " WHERE library IS NOT NULL AND state!='deleted'").fetchall()
        for r in rows:
            slot = out.setdefault(r["library"], {"audio": {}, "subs": {}})
            for side, col in (("audio", "audio_langs"), ("subs", "sub_langs")):
                raw = r[col]
                if not raw:
                    continue
                for lg in raw.split(","):
                    # "-" is a track with no tag at all. It is reported as
                    # "und" here because that is what the page and the policy
                    # already call it, and because a blank tag is NOT the
                    # absence of a language - players read it as English.
                    lg = "und" if lg == "-" else lg
                    if lg:
                        slot[side][lg] = slot[side].get(lg, 0) + 1
    except Exception:
        pass
    return out
