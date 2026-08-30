r"""One canonical way to ask "are these two paths the same file?".

THE PROBLEM. nuarr and the arrs can be looking at the same file down different
roads. Sonarr says `P:\Anime Movies\...` because that is how the drive is
mapped where Sonarr runs; nuarr, on another machine, reaches the same bytes at
`\\192.168.0.176\P\Anime Movies\...`. Both are correct and they share not one
character of prefix, so a plain string compare says "no arr manages this
folder" about a folder an arr manages perfectly well.

That comparison is made in about a dozen places - adoption, rename matching,
orphan detection, the not-in-any-arr verdict. Fixing them one at a time would
mean twelve chances to fix eleven of them, so they all come here instead.

WHAT IS STORED, AND WHAT IS TRANSLATED. The database keeps the path nuarr can
actually open, because that is the one it has to hand to ffmpeg forty thousand
times, and rewriting every stored row to the arr's form would be a migration
in exchange for nothing. The translation happens at the boundary: when a path
is compared against, or sent to, an arr.

DERIVED, THEN SHOWN. The pairs are worked out by matching the tail of an arr
root against the tail of a root nuarr scanned, longest first. That is a guess,
and a guess that silently mis-files things is worse than no guess at all - so
describe() exists to put the working on the Arrs page, and an explicit pair in
settings always wins over a derived one.
"""
from __future__ import annotations

import os
import threading

_LOCK = threading.Lock()
_CACHE: dict = {"pairs": [], "at": 0.0, "derived": [], "explicit": []}


def _norm(p: str) -> str:
    """Compare-ready: no trailing separator, one separator kind, case-folded."""
    if not p:
        return ""
    p = str(p).replace("/", os.sep)
    # normpath would collapse the leading \\ of a UNC path to \, which is the
    # one thing about these paths that must survive.
    unc = p.startswith("\\\\")
    p = p.rstrip("\\/")
    if unc and not p.startswith("\\\\"):
        p = "\\" + p
    return os.path.normcase(p)


def _segments(p: str) -> list:
    """Path split into its parts, ignoring the root, for tail matching."""
    p = _norm(p)
    if p.startswith("\\\\"):
        # \\host\share\a\b -> host, share, a, b
        return [s for s in p[2:].split(os.sep) if s]
    return [s for s in p.replace(":", "").split(os.sep) if s]


def _tail_overlap(a: str, b: str) -> int:
    """How many trailing segments two paths share. 0 means unrelated."""
    sa, sb = _segments(a), _segments(b)
    n = 0
    while n < len(sa) and n < len(sb) and sa[-1 - n] == sb[-1 - n]:
        n += 1
    return n


def derive(arr_roots: list, local_roots: list) -> list:
    r"""Work out prefix pairs from two lists of roots.

    Pairs an arr root with the local root sharing the longest run of trailing
    segments - `P:\Anime Movies` against `\\host\P\Anime Movies` shares two
    ("p", "anime movies"), which is a better claim than any root sharing one.

    A tail of ONE SEGMENT IS NOT ENOUGH on its own when something else matches
    better, and never enough when the segment is the only thing there: two
    libraries can both end in "Movies" and mapping one onto the other would
    send files to the wrong arr. Ambiguity is reported rather than guessed at.

    ONE ROW PER LOCAL ROOT, driven from nuarr's side. The arr candidates are
    every ancestor folder seen in its file list, which is a long list mostly
    made of series folders; asking "what does the arr call THIS library of
    mine" gives one answer per library, which is both the useful question and
    the one worth showing.
    """
    out = []
    for l in local_roots or []:
        best, best_n, tie = None, 0, False
        for a in arr_roots or []:
            n = _tail_overlap(a, l)
            if n > best_n:
                best, best_n, tie = a, n, False
            elif n == best_n and n > 0 and _norm(a) != _norm(best or ""):
                tie = True
        if not best or best_n == 0:
            out.append({"arr": "", "local": l, "segments": 0,
                        "why": "no arr folder shares a trailing folder name "
                               "with this library",
                        "ok": False})
            continue
        if tie:
            out.append({"arr": "", "local": l, "segments": best_n,
                        "why": f"more than one arr folder matches on "
                               f"{best_n} trailing folder(s) - too ambiguous "
                               f"to choose",
                        "ok": False})
            continue
        if _norm(best) == _norm(l):
            out.append({"arr": best, "local": l, "segments": best_n,
                        "why": "identical - no translation needed",
                        "ok": True, "identity": True})
            continue
        out.append({"arr": best, "local": l, "segments": best_n,
                    "why": f"matched on {best_n} trailing folder(s)",
                    "ok": True, "identity": False})
    return out


def _explicit() -> list:
    """Pairs written down by hand, which always beat a derived one."""
    try:
        from .config import SETTINGS
        raw = getattr(SETTINGS, "arr_path_map", None) or []
    except Exception:                                        # noqa: BLE001
        return []
    out = []
    for row in raw:
        try:
            if isinstance(row, dict):
                a, l = row.get("arr"), row.get("local")
            else:
                a, l = row[0], row[1]
            if a and l:
                out.append({"arr": str(a), "local": str(l), "segments": -1,
                            "why": "set by hand", "ok": True,
                            "identity": _norm(a) == _norm(l)})
        except Exception:                                    # noqa: BLE001
            continue
    return out


def pairs(refresh: bool = False) -> list:
    """Every mapping in force, explicit first. Cached; cheap to call."""
    import time
    with _LOCK:
        if not refresh and _CACHE["pairs"] and time.time() - _CACHE["at"] < 300:
            return list(_CACHE["pairs"])
    ex = _explicit()
    der = []
    try:
        der = derive(_arr_roots(), _local_roots())
    except Exception:                                        # noqa: BLE001
        der = []
    seen = {_norm(p["arr"]) for p in ex}
    merged = ex + [d for d in der if _norm(d["arr"]) not in seen]
    with _LOCK:
        _CACHE.update(pairs=merged, derived=der, explicit=ex, at=time.time())
    return list(merged)


_KV_KEY = "pathmap.arr_roots"


def learn(arr_paths, local_roots=None) -> list:
    r"""Record the roots the arrs are using, from paths they just reported.

    CALLED WHERE THE TWO WORLDS MEET. The scanner already holds the arr's file
    list and nuarr's own paths in the same function; nowhere else has both, and
    re-fetching the arr list purely to learn a prefix would be a second round
    trip for something that just went past.

    EVERY ANCESTOR, NOT A FIXED DEPTH. A movie sits two levels under its
    library root and an episode three - root/series/season/file - so walking up
    a fixed two produced `P:\TV Shows` for films and `P:\TV Shows\Chucky
    (2021)` for episodes, and the second matches no local root at all. Emitting
    the whole chain costs a handful of strings and lets derive() pick the one
    that actually lines up, whatever the library's shape.

    KEPT TO THE ONES THAT COULD MATCH. Walking every ancestor of 39,000 files
    produced 5,527 candidates - a third of a megabyte of series names rewritten
    into the key-value store on every scan. Capping by depth does not help: a
    series folder `P:\TV Shows\9-1-1 (2018)` and a UNC root
    `\\host\P\TV Shows` are both three segments deep. What DOES separate them
    is the thing we are looking for anyway - whether the folder shares a
    trailing name with one of nuarr's own library roots. Filtering on that
    here leaves a handful instead of thousands, and cannot lose a real root,
    because a root with no shared tail is one derive() would reject regardless.
    """
    locals_ = local_roots if local_roots is not None else _local_roots()
    roots = set()
    for p in arr_paths or []:
        p = str(p or "")
        if not p:
            continue
        d = os.path.dirname(p)
        for _ in range(5):
            if not d or d == os.path.dirname(d):
                break
            if len(_segments(d)) < 2:  # a drive or a bare share is not a root
                break
            if any(_tail_overlap(d, l) > 0 for l in locals_):
                roots.add(d)
            d = os.path.dirname(d)
    if not roots:
        return []
    keep = sorted(roots)
    try:
        from .db import cursor
        with cursor() as cur:
            cur.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (_KV_KEY, "|".join(keep)))
    except Exception:                                        # noqa: BLE001
        pass
    with _LOCK:
        _CACHE["pairs"] = []                    # force a re-derive next ask
    return keep


def _arr_roots() -> list:
    """Root folders learned from what the arrs last reported."""
    try:
        from .db import cursor
        with cursor() as cur:
            r = cur.execute("SELECT v FROM kv WHERE k=?", (_KV_KEY,)).fetchone()
        if r and r[0]:
            return [x for x in str(r[0]).split("|") if x]
    except Exception:                                        # noqa: BLE001
        pass
    return []


def _local_roots() -> list:
    """The library roots nuarr itself scanned."""
    try:
        from .config import SETTINGS
        libs = SETTINGS.libraries or []
        out = []
        for l in libs:
            p = l.get("path") if isinstance(l, dict) else getattr(l, "path", None)
            if p:
                out.append(str(p))
        return out
    except Exception:                                        # noqa: BLE001
        return []


def to_local(path: str) -> str:
    """An arr's path, rewritten as one nuarr can open. Unchanged if no pair."""
    if not path:
        return path
    n = _norm(path)
    best, best_len = None, -1
    for p in pairs():
        if not p.get("ok") or p.get("identity"):
            continue
        a = _norm(p["arr"])
        if a and (n == a or n.startswith(a + os.sep)) and len(a) > best_len:
            best, best_len = p, len(a)
    if not best:
        return path
    rest = path[len(best["arr"].rstrip("\\/")):].lstrip("\\/")
    return os.path.join(best["local"].rstrip("\\/"), rest) if rest \
        else best["local"]


def to_arr(path: str) -> str:
    """A local path, rewritten as the arr would report it."""
    if not path:
        return path
    n = _norm(path)
    best, best_len = None, -1
    for p in pairs():
        if not p.get("ok") or p.get("identity"):
            continue
        l = _norm(p["local"])
        if l and (n == l or n.startswith(l + os.sep)) and len(l) > best_len:
            best, best_len = p, len(l)
    if not best:
        return path
    rest = path[len(best["local"].rstrip("\\/")):].lstrip("\\/")
    return os.path.join(best["arr"].rstrip("\\/"), rest) if rest \
        else best["arr"]


def same(a: str, b: str) -> bool:
    """Do these two paths name the same file, whichever road they came by?"""
    if not a or not b:
        return False
    if _norm(a) == _norm(b):
        return True
    return _norm(to_local(a)) == _norm(to_local(b))


def under(child: str, parent: str) -> bool:
    """Is `child` inside `parent`, allowing for the two speaking differently?"""
    if not child or not parent:
        return False
    c, p = _norm(child), _norm(parent)
    if c == p or c.startswith(p + os.sep):
        return True
    c2, p2 = _norm(to_local(child)), _norm(to_local(parent))
    return c2 == p2 or c2.startswith(p2 + os.sep)


def describe() -> dict:
    r"""Everything the Arrs page needs to show what was worked out, and why.

    `learned` MATTERS MORE THAN IT LOOKS. Before the first scan there are no
    arr folders to match against, so every library comes back unmatched - which
    is true and reads exactly like six broken libraries. "Not asked yet" and
    "asked, and the answer is no" deserve different words on the page.
    """
    roots = _arr_roots()
    ps = pairs(refresh=True)
    return {
        "learned": bool(roots),
        "pairs": ps if roots else [],
        "arr_roots": roots,
        "local_roots": _local_roots(),
        "unmatched": [p for p in ps if not p.get("ok")] if roots else [],
        "translating": [p for p in ps
                        if p.get("ok") and not p.get("identity")] if roots
                       else [],
    }
