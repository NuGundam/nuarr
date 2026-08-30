r"""Keep the arr-side configuration the way it was decided, automatically.

Two watch jobs, each with its own on/off toggle. Both take their profile
names from Settings -> Arrs rather than hard-coding them:

PROFILE GUARD (arrs.profile_guard)
    The 2160p quality profiles were split into ordered groups - resolution
    primary, custom-format score secondary - directly in Radarr/Sonarr,
    because that is what "a 2160p profile" should mean. A Profilarr profile
    sync rebuilds them flat. Rather than hoping nobody clicks Sync, this
    checks the live profiles on a timer and re-applies the split when it
    finds the merged shape, logging that it did.

TRASH ANIME SYNC (arrs.trash_anime)
    The anime custom formats came from the TRaSH guides and drift as the
    guides evolve - regexes fixed, release groups re-tiered. This fetches the
    current TRaSH anime CF definitions from their repo and updates the arr's
    EXISTING formats (matched by name) whose specifications changed. It never
    deletes and never adds formats to profiles on its own; new formats are
    reported for a human to adopt.

Both default OFF, run inside one watch loop, and publish STATS for the tab.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .arr import shared_client
from . import schedules
from .config import SETTINGS
from . import joblog

POLL_S = 6 * 3600            # guard cadence; TRaSH is further limited below
TRASH_MIN_GAP_S = 20 * 3600  # at most daily - it is someone else's bandwidth

# DEFAULTS, not the setting. Both lists are editable in Settings -> Arrs, and
# the stored value wins - see targets_2160() / anime_profiles(). They shipped
# hard-coded to this machine's profile names, which made a guard that is
# supposed to protect ANY install into one that silently did nothing on
# somebody else's.
DEFAULT_TARGET_2160 = {"radarr": ["Nu 2160p Quality", "Nu 2160p Animation"],
                       "sonarr": ["Nu 2160p Efficient", "Nu 2160p Animation"]}
# WHICH TRaSH FORMATS TO KEEP IN SYNC, per arr. Empty list = every anime
# format the guides publish that ALREADY exists in that arr, which is the
# original behaviour and stays the default.
#
# Note this is a FORMAT list, not a profile list. The old constant was named
# for profiles and keyed by arr, but the code only ever tested the KEY - the
# names inside were never read, so "which profiles" was never a thing this
# job could do. Editing formats is what it actually does, so that is what is
# editable.
DEFAULT_ANIME_FORMATS = {"sonarr": [], "radarr": []}


def _load_names(key: str, fallback: dict) -> dict:
    """{'radarr': [...], 'sonarr': [...]} from settings, or the default."""
    from .db import kv_get
    raw = kv_get(key)
    if not raw:
        return {k: list(v) for k, v in fallback.items()}
    try:
        got = json.loads(raw)
    except Exception:                                        # noqa: BLE001
        return {k: list(v) for k, v in fallback.items()}
    out = {}
    for kind in ("radarr", "sonarr"):
        vals = got.get(kind)
        # An empty list is a REAL choice - "do not guard this arr" - and must
        # not fall back to the default, or turning a guard off for one app
        # would be impossible.
        out[kind] = [str(x).strip() for x in vals if str(x).strip()] \
            if isinstance(vals, list) else list(fallback.get(kind, []))
    return out


def _save_names(key: str, value: dict) -> dict:
    from .db import kv_set
    clean = {k: [str(x).strip() for x in (value.get(k) or []) if str(x).strip()]
             for k in ("radarr", "sonarr")}
    kv_set(key, json.dumps(clean))
    return clean


def targets_2160() -> dict:
    return _load_names("arrs.split_profiles", DEFAULT_TARGET_2160)


def anime_formats() -> dict:
    return _load_names("arrs.anime_formats", DEFAULT_ANIME_FORMATS)


_TRASH_RAW = ("https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/"
              "docs/json/{kind}/cf/{fname}")
_TRASH_DIR = ("https://api.github.com/repos/TRaSH-Guides/Guides/contents/"
              "docs/json/{kind}/cf")

STATS: dict = {
    "guard": {"last_run": 0.0, "last_result": "", "fixed": 0, "next_run": 0.0},
    "trash": {"last_run": 0.0, "last_result": "", "updated": 0,
              "new_formats": [], "next_run": 0.0},
    "running": "",
}


def _is_2160(qitem: dict) -> bool:
    q = qitem.get("quality") or {}
    return (q.get("resolution") == 2160) or ("2160" in str(q.get("name", "")))


def _split_needed(profile: dict) -> bool:
    """True when the profile is back to one merged allowed group."""
    groups = [it for it in profile.get("items", [])
              if it.get("items") and it.get("allowed")]
    if len(groups) != 1:
        return False                       # already split (2) or unexpected
    g = groups[0]
    hi = [q for q in g["items"] if _is_2160(q)]
    lo = [q for q in g["items"] if not _is_2160(q)]
    return bool(hi and lo)


def _apply_split(profile: dict) -> dict:
    """Rebuild items as [below-2160 group, 2160 group]; cutoff on the top."""
    g = next(it for it in profile["items"]
             if it.get("items") and it.get("allowed"))
    hi = [q for q in g["items"] if _is_2160(q)]
    lo = [q for q in g["items"] if not _is_2160(q)]
    used = {it.get("id") for it in profile["items"] if it.get("id")}
    gid_hi = next(i for i in range(1000, 2000) if i not in used)
    items = []
    for it in profile["items"]:
        if it is g:
            items.append({"id": g["id"], "name": profile["name"] + " <2160p",
                          "allowed": True, "items": lo})
            items.append({"id": gid_hi, "name": profile["name"] + " 2160p",
                          "allowed": True, "items": hi})
        else:
            items.append(it)
    profile["items"] = items
    profile["cutoff"] = gid_hi
    return profile


# The English-audio SCORE GUARD lived here and is gone. Its job was to
# re-assert two Radarr format scores that a Profilarr sync would zero,
# because Profilarr did not know those formats existed. It now does:
# "Language: English" and "English Audio (Dual/Dubbed)" were created in
# Profilarr and scored +3000 on the five Nu profiles, so Profilarr pushes
# them itself and there is nothing left to restore. A guard that can only
# ever report "intact" is noise on the page.



async def run_guard() -> str:
    targets = targets_2160()
    fixed, ok, errs = [], [], []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in targets:
            continue
        c = shared_client(cfg)
        try:
            profs = await c._get("/qualityprofile")
        except Exception as e:
            errs.append(f"{cfg.name}: {type(e).__name__}")
            continue
        for p in profs:
            if p["name"] not in targets[cfg.kind]:
                continue
            if _split_needed(p):
                try:
                    await c._client.put(f"/qualityprofile/{p['id']}",
                                        json=_apply_split(p))
                    fixed.append(f"{cfg.name}/{p['name']}")
                    joblog.log(f"profile guard: re-split {cfg.name}/"
                               f"{p['name']} (a sync had flattened it)",
                               "warn")
                except Exception as e:
                    errs.append(f"{p['name']}: {type(e).__name__}")
            else:
                ok.append(p["name"])
    STATS["guard"].update(last_run=time.time(), fixed=len(fixed))
    msg = (f"re-split {', '.join(fixed)}" if fixed
           else f"{len(ok)} profiles intact")
    if errs:
        msg += f"; errors: {', '.join(errs)}"
    STATS["guard"]["last_result"] = msg
    return msg


async def _trash_fetch(kind: str) -> dict[str, dict]:
    """name -> TRaSH cf json, for the anime-tagged formats of one arr kind."""
    async with httpx.AsyncClient(timeout=30) as h:
        r = await h.get(_TRASH_DIR.format(kind=kind))
        r.raise_for_status()
        names = [e["name"] for e in r.json() if e["name"].endswith(".json")]
        out: dict[str, dict] = {}
        # anime formats are identifiable by filename; fetching all ~200 CFs
        # daily would be rude and pointless.
        for fname in names:
            if "anime" not in fname.lower():
                continue
            rr = await h.get(_TRASH_RAW.format(kind=kind, fname=fname))
            if rr.status_code == 200:
                cf = rr.json()
                out[cf.get("name", fname)] = cf
        return out


def _norm_specs(specs: list) -> list:
    r"""TRaSH's spec shape -> the arr's.

    The guides publish a specification's fields as an OBJECT:

        "fields": {"value": "\\b(Anime[ .-]?Heart)\\b"}

    Radarr and Sonarr want a LIST of named fields:

        "fields": [{"name": "value", "value": "\\b(Anime[ .-]?Heart)\\b"}]

    Posting the guides' shape straight through returns a 400 naming the exact
    conversion it could not do, which is how this was found. Normalising here
    rather than at each call site because the update path sends the same
    payload and would hit the same wall the first time a spec had to change.
    """
    out = []
    for s in specs or []:
        s = dict(s)
        f = s.get("fields")
        if isinstance(f, dict):
            s["fields"] = [{"name": k, "value": v} for k, v in f.items()]
        out.append(s)
    return out


async def add_formats(names: dict | None = None) -> str:
    """CREATE anime formats the guides publish but this arr does not have.

    The sync deliberately never invented formats: updating a rule you already
    chose to run is a different act from adding one you have never seen, and
    doing the second silently would mean the guides could change what your
    library downloads without anyone deciding to let them. So new ones were
    only ever LISTED - which is right, and also meant adding fifteen of them
    was fifteen trips through the arr's own UI.

    This adds them on request. Two ways in, both explicit:
      * the button, with the names ticked (names={"radarr": [...], ...})
      * the auto toggle, which passes names=None to mean "everything new"

    It creates the FORMAT only, never touches a quality profile, and never
    gives it a score. A format with no score changes nothing about what gets
    downloaded until somebody scores it - so this is reversible by ignoring
    it, which is the property that makes automating it defensible at all.
    """
    added, errs = [], []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in ("radarr", "sonarr"):
            continue
        want = None if names is None else set(names.get(cfg.kind) or [])
        if want is not None and not want:
            continue
        try:
            trash = await _trash_fetch(cfg.kind)
        except Exception as e:                               # noqa: BLE001
            errs.append(f"TRaSH fetch ({cfg.kind}): {type(e).__name__}")
            continue
        c = shared_client(cfg)
        try:
            mine = {f["name"] for f in await c._get("/customformat")}
        except Exception as e:                               # noqa: BLE001
            errs.append(f"{cfg.name}: {type(e).__name__}")
            continue
        for name, tcf in trash.items():
            if name in mine:
                continue                      # already there: not this job
            if want is not None and name not in want:
                continue
            body = {"name": name,
                    "includeCustomFormatWhenRenaming": False,
                    "specifications": _norm_specs(
                        tcf.get("specifications") or [])}
            try:
                await c._post("/customformat", body)
                added.append(f"{cfg.name}/{name}")
                joblog.log(f"TRaSH anime sync: added custom format {name!r} to "
                           f"{cfg.name} (no score - it does nothing until you "
                           f"give it one)", "info")
            except Exception as e:                           # noqa: BLE001
                errs.append(f"{name}: {type(e).__name__}")
    msg = (f"added {len(added)}: {', '.join(added[:6])}" if added
           else "nothing to add")
    if errs:
        msg += f"; errors: {', '.join(errs[:3])}"
    return msg


def _spec_key(cf: dict):
    """Comparable shape of a format's matching rules, order-insensitive."""
    out = []
    for s in cf.get("specifications", []):
        fields = s.get("fields")
        if isinstance(fields, list):
            fv = {f.get("name"): f.get("value") for f in fields}
        else:
            fv = fields or {}
        out.append((s.get("name"), s.get("implementation"),
                    bool(s.get("negate")), bool(s.get("required")),
                    json.dumps(fv, sort_keys=True, default=str)))
    return sorted(out)


async def run_trash() -> str:
    only = anime_formats()
    updated, new_fmts, errs = [], [], []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in only:
            continue
        keep = set(only[cfg.kind])          # empty = no filter, sync them all
        try:
            trash = await _trash_fetch(cfg.kind)
        except Exception as e:
            errs.append(f"TRaSH fetch ({cfg.kind}): {type(e).__name__}")
            continue
        c = shared_client(cfg)
        try:
            mine = {f["name"]: f for f in await c._get("/customformat")}
        except Exception as e:
            errs.append(f"{cfg.name}: {type(e).__name__}")
            continue
        for name, tcf in trash.items():
            if name not in mine:
                # DISCOVERY IS NOT FILTERED, on purpose. The keep-list says
                # which formats to keep UPDATED; applying it here as well
                # meant that naming any formats silently switched off the
                # "new in the guides" report - a filter quietly disabling a
                # different feature. You still get told what exists; whether
                # to add it stays your call.
                new_fmts.append(f"{cfg.kind}: {name}")
                continue
            if keep and name not in keep:
                continue                      # not one you asked to track
            cur = mine[name]
            if _spec_key(cur) == _spec_key(tcf):
                continue
            body = dict(cur)
            body["specifications"] = _norm_specs(tcf["specifications"])
            body["includeCustomFormatWhenRenaming"] = cur.get(
                "includeCustomFormatWhenRenaming", False)
            try:
                await c._client.put(f"/customformat/{cur['id']}", json=body)
                updated.append(f"{cfg.name}/{name}")
                joblog.log(f"TRaSH anime sync: updated custom format "
                           f"{name!r} on {cfg.name}", "info")
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__}")
    # AUTO-ADD, if it has been switched on. Off by default and separate from
    # the sync's own toggle: keeping an existing format current is a much
    # smaller decision than letting the guides add formats to your arr.
    auto_msg = ""
    if new_fmts:
        from .gate import get_toggle
        if get_toggle("arrs.trash_autoadd"):
            try:
                auto_msg = "; auto-add: " + await add_formats(None)
                new_fmts = []               # they exist now, not "new"
            except Exception as e:                           # noqa: BLE001
                auto_msg = f"; auto-add failed: {type(e).__name__}"
    STATS["trash"].update(last_run=time.time(), updated=len(updated),
                          new_formats=sorted(set(new_fmts))[:20])
    msg = (f"updated {len(updated)}: {', '.join(updated[:6])}" if updated
           else "all anime formats match the guides")
    if new_fmts:
        msg += f"; {len(set(new_fmts))} new in the guides (not auto-added)"
    msg += auto_msg
    if errs:
        msg += f"; errors: {', '.join(errs[:4])}"
    STATS["trash"]["last_result"] = msg
    return msg


def ensure_defaults() -> None:
    """Tidy up after the score guard, which no longer exists.

    Its toggle is deleted rather than left behind: a stored setting for a job
    that is gone is a thing that reads as broken next time somebody greps for
    it. The two English-audio formats it protected now live in Profilarr with
    their +3000 scores, so Profilarr pushes them itself.
    """
    from .db import cursor, kv_get
    if kv_get("arrs.score_guard") is None:
        return
    # DELETE the row - kv_set(key, None) would store a NULL, which is still a
    # setting, just an unreadable one.
    with cursor() as cur:
        cur.execute("DELETE FROM kv WHERE k=?", ("arrs.score_guard",))
    try:
        from . import db as _db
        with _db._kv_lock:                       # keep the in-memory cache true
            if _db._KV is not None:
                _db._KV.pop("arrs.score_guard", None)
    except Exception:                                        # noqa: BLE001
        pass
    joblog.log("the English-audio score guard has been retired - Profilarr now "
               "owns those two formats and their scores", "info")


async def watch() -> None:
    from .gate import get_toggle
    await asyncio.sleep(240)             # never compete with startup
    try:
        ensure_defaults()
    except Exception:
        pass
    while True:
        schedules.beat('arrguard')
        try:
            if get_toggle("arrs.profile_guard"):
                STATS["running"] = "profile guard"
                await run_guard()
            STATS["guard"]["next_run"] = time.time() + POLL_S
            if get_toggle("arrs.trash_anime") and \
                    time.time() - STATS["trash"]["last_run"] > TRASH_MIN_GAP_S:
                STATS["running"] = "TRaSH anime sync"
                await run_trash()
            STATS["trash"]["next_run"] = max(
                STATS["trash"]["last_run"] + TRASH_MIN_GAP_S,
                time.time() + POLL_S)
        except Exception as e:
            joblog.log(f"arr guard loop: {type(e).__name__}: {e}", "error")
        finally:
            STATS["running"] = ""
        await asyncio.sleep(POLL_S)
