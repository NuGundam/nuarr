r"""What is running right now, everywhere, in one list.

WHY THIS EXISTS. nuarr has grown fourteen things that do work on their own
clocks - a library scan, three job pools, six standing checks, two sweeps and
a couple of syncs - and every one of them reports itself beautifully on its
own page. Which is exactly the problem: to answer "is anything happening?" you
had to visit nine pages and know which of them to visit.

So this asks every one of them the same three questions - are you running,
what are you doing, and how far through - and hands back one list with a link
to the page that has the detail. A chip in the header opens it.

IT ASKS, IT DOES NOT KNOW. Every entry below reads a dict the module already
maintains for its own panel. Nothing here computes a state, nothing caches one,
and nothing is duplicated - so a system whose page says it is idle can never
show as busy here, because both are reading the same field.

CHEAP ENOUGH TO POLL. Every reader is a dict lookup or a memoised call that
already happens for the header; the one query in here is the job count, which
is indexed. Anything that would cost real work to answer is not asked - a
system that cannot say what it is doing without doing something is one this
list leaves out.
"""
from __future__ import annotations

import time


def _pct(done, total) -> float:
    try:
        done, total = float(done or 0), float(total or 0)
        return max(0.0, min(100.0, done / total * 100.0)) if total else 0.0
    except Exception:                                            # noqa: BLE001
        return 0.0


def _one(key, name, where, running, now="", done=0, total=0, note="",
         since=0.0) -> dict:
    return {"key": key, "name": name, "goto": where,
            "running": bool(running), "now": str(now or "")[:90],
            "done": int(done or 0), "total": int(total or 0),
            "pct": _pct(done, total), "note": str(note or "")[:140],
            "since": float(since or 0.0)}


def _jobs() -> list:
    """The three pools, from the same snapshot the worker cards read."""
    out = []
    try:
        from . import jobs
        snap = jobs.live_snapshot() or {}
    except Exception:                                            # noqa: BLE001
        return out
    rows = snap.get("running") or snap.get("jobs") or []
    by_pool: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        by_pool.setdefault(r.get("pool") or "job", []).append(r)
    label = {"encode": "Encoding", "passthrough": "Repacking",
             "subocr": "Reading picture subtitles"}
    for pool, rs in sorted(by_pool.items()):
        first = rs[0]
        out.append(_one(f"pool:{pool}", label.get(pool, pool.title()),
                        "/#running", True,
                        now=first.get("title") or first.get("path") or "",
                        done=0, total=0,
                        note=(f"{len(rs)} file{'' if len(rs) == 1 else 's'} "
                              f"in the {pool} pool")))
    return out


def _dict_state(mod_name, key, name, where, note="") -> list:
    """Read a module's own STATE-shaped dict without importing it eagerly."""
    try:
        mod = __import__(f"app.{mod_name}", fromlist=[mod_name])
    except Exception:                                            # noqa: BLE001
        return []
    st = (getattr(mod, "STATE", None) or getattr(mod, "STATS", None)
          or getattr(mod, "_CACHE", None) or getattr(mod, "PROGRESS", None))
    if not isinstance(st, dict):
        return []
    running = bool(st.get("running"))
    if not running:
        return []
    return [_one(key, name, where, True,
                 now=st.get("now") or st.get("current") or st.get("path") or "",
                 done=st.get("done") or st.get("checked") or 0,
                 total=st.get("total") or 0,
                 note=note,
                 since=st.get("t0") or st.get("started") or 0.0)]


# name, module, label, where its page is, and what it is doing while it runs.
#
# THE NOTE IS NOT DECORATION. Half these systems count files and half ask one
# long question - arrgap fetches every record from both arrs and has nothing to
# count until the answer comes back. A row reading "Missing from the arrs 0/0"
# says only that something is happening; the note says what, and the elapsed
# clock beside it is what turns a slow answer into a visibly stuck one.
_SIMPLE = [
    ("integrity", "integrity", "Does it decode?", "/settings#health",
     "decoding the first and last seconds of files"),
    ("audit", "audit", "Rule check", "/settings#rulecheck",
     "re-reading committed files against today's rules"),
    ("hardsub", "hardsub", "Subtitles in the picture", "/settings#subs",
     "sampling frames for words burned into the picture"),
    ("subembed", "subembed", "Sidecar subtitles", "/settings#subs",
     "walking folders for subtitles sitting outside their file"),
    ("subtitletitle", "subtitletitle", "Subtitle titles", "/settings#subs",
     "checking cue counts against what each title claims"),
    ("audiotitle", "audiotitle", "Audio titles", "/settings#alang",
     "checking track titles against the streams they describe"),
    ("arrgap", "arrgap", "Missing from the arrs", "/settings#arrs",
     "asking Sonarr and Radarr for everything they track"),
    ("plexsync", "plexsync", "Plex agreement", "/settings#plex",
     "comparing what Plex has against what nuarr has"),
    ("healer", "healer", "Missing-file check", "/#missing",
     "looking again for files an arr stopped reporting"),
]


def running() -> dict:
    """Every system doing work right now, and how far through it is."""
    out: list = []

    # THE SCAN, which reports differently from everything else because it is
    # the only one with named phases rather than a file count.
    try:
        from . import scanner
        p = scanner.PROGRESS or {}
        # PROGRESS keeps its last state after a pass ends, so "started" alone
        # left a finished scan sitting on the list reading 12 of 12 · done.
        # The phase is what says whether it is over.
        phase = str(p.get("phase") or "").lower()
        if (p.get("started") and not p.get("done_at")
                and phase not in ("done", "idle", "finished", "")):
            out.append(_one("scan", "Library scan", "/#scan", True,
                            now=p.get("library") or p.get("disk") or "",
                            done=p.get("disk_i") or 0, total=p.get("disks") or 0,
                            note=p.get("phase") or "",
                            since=p.get("started") or 0.0))
    except Exception:                                            # noqa: BLE001
        pass

    out += _jobs()

    for key, mod, name, where, note in _SIMPLE:
        try:
            out += _dict_state(mod, key, name, where, note)
        except Exception:                                        # noqa: BLE001
            continue

    # AUDIO LANGUAGE keeps a state WORD rather than a flag, because "listening"
    # and "writing" and "telling the arrs" are different enough to say out loud.
    try:
        from . import audiolang
        pr = audiolang.PROGRESS or {}
        if pr.get("state") and pr["state"] != "idle":
            out.append(_one("audiolang", "Audio language", "/settings#alang",
                            True, now=pr.get("current") or "",
                            done=pr.get("done") or 0, total=pr.get("total") or 0,
                            note=pr.get("state") or "",
                            since=pr.get("started_at") or 0.0))
    except Exception:                                            # noqa: BLE001
        pass

    # READING FLAGGED SUBTITLE TRACKS, which is disk rather than a query and
    # is the one part of that check somebody might be waiting on.
    try:
        from . import subtitletitle as _stt
        ins = _stt.INSPECT_STATE or {}
        if ins.get("running"):
            out.append(_one("subtitletitle:read", "Reading subtitle tracks",
                            "/settings#subs", True, now=ins.get("now") or "",
                            done=ins.get("done") or 0,
                            total=ins.get("total") or 0,
                            note="looking at where the lines actually are",
                            since=ins.get("t0") or 0.0))
    except Exception:                                            # noqa: BLE001
        pass

    # THE BATCH MARKER, which is a person's request rather than a schedule -
    # and the one thing here somebody is actively waiting on.
    try:
        from . import hardsub
        mk = hardsub.MARK_STATE or {}
        if mk.get("running"):
            out.append(_one("hardsub:mark", "Marking burned-in subtitles",
                            "/settings#subs", True, now=mk.get("now") or "",
                            done=mk.get("done") or 0, total=mk.get("total") or 0,
                            note="adding the blank marker track",
                            since=mk.get("t0") or 0.0))
    except Exception:                                            # noqa: BLE001
        pass

    # DRIVEPOOL is not nuarr working, it is nuarr WAITING - and that belongs on
    # this list precisely because "nothing is happening" and "something else is
    # happening to the disks" look identical from outside and mean opposite
    # things about whether to worry.
    try:
        from . import drivepool
        st = drivepool.STATE or {}
        if st.get("busy") or st.get("balancing") or st.get("duplicating"):
            what = ("balancing" if st.get("balancing")
                    else "duplicating" if st.get("duplicating") else "busy")
            out.append(_one("drivepool", "DrivePool", "/settings#drivepool",
                            True, now=what,
                            done=int(st.get("pct") or 0), total=100,
                            note="the pool is moving data; nuarr is holding "
                                 "renames and commits"))
    except Exception:                                            # noqa: BLE001
        pass

    out.sort(key=lambda r: (-(r["pct"] > 0), r["name"]))
    return {"running": out, "n": len(out), "at": time.time()}
