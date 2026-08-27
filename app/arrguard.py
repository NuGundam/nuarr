r"""Keep the arr-side configuration the way it was decided, automatically.

Three watch jobs, each with its own on/off toggle:

PROFILE GUARD (arrs.profile_guard)
    The 2160p quality profiles were split into ordered groups - resolution
    primary, custom-format score secondary - directly in Radarr/Sonarr,
    because that is what "a 2160p profile" should mean. A Profilarr profile
    sync rebuilds them flat. Rather than hoping nobody clicks Sync, this
    checks the live profiles on a timer and re-applies the split when it
    finds the merged shape, logging that it did.

SCORE GUARD (arrs.score_guard)
    The two English-audio custom formats were scored +3000 directly in Radarr,
    because Radarr is where they were created. Profilarr owns per-profile
    format scores and pushes its own complete set on a sync, which sets
    anything it does not know about to 0. This re-asserts those scores.

    It is deliberately separate from the profile guard: same failure cause,
    but different blast radius. The profile guard rewrites the shape of a
    profile; this only touches two named scores. Anyone should be able to
    switch one off without losing the other.

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

TARGET_2160 = {"radarr": ["Nu 2160p Quality", "Nu 2160p Animation"],
               "sonarr": ["Nu 2160p Efficient", "Nu 2160p Animation"]}
ANIME_PROFILES = {"sonarr": ["Anime", "[Anime] Dual-Audio"],
                  "radarr": ["Anime"]}
_TRASH_RAW = ("https://raw.githubusercontent.com/TRaSH-Guides/Guides/master/"
              "docs/json/{kind}/cf/{fname}")
_TRASH_DIR = ("https://api.github.com/repos/TRaSH-Guides/Guides/contents/"
              "docs/json/{kind}/cf")

STATS: dict = {
    "guard": {"last_run": 0.0, "last_result": "", "fixed": 0, "next_run": 0.0},
    "score": {"last_run": 0.0, "last_result": "", "fixed": 0, "next_run": 0.0,
              "detail": []},
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


# Custom-format scores that are OURS, not Profilarr's, and which a profile
# sync therefore erases. Profilarr owns per-profile format scores - 1,721 of
# the user ops in its database are exactly that - and it pushes its own
# complete set when it syncs, so anything added directly in Radarr goes to 0.
# Nothing syncs automatically today (should_sync=0, trigger=manual), so this is
# a backstop against a button press, not a running battle.
GUARDED_SCORES = {
    "Language: English": 3000,
    "English Audio (Dual/Dubbed)": 3000,
}
SCORE_PROFILES = {
    "radarr": ["Nu 1080p Quality HDR", "Nu 2160p Quality", "Nu 2160p Animation",
               "Nu 1080p Quality HDR Animation", "Nu 1080p Remux Animation"],
}


async def run_score_guard() -> str:
    """Re-assert the English-audio scores if something reset them."""
    fixed, absent, errs, checked = [], [], [], 0
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in SCORE_PROFILES:
            continue
        c = shared_client(cfg)
        try:
            profs = await c._get("/qualityprofile")
        except Exception as e:
            errs.append(f"{cfg.name}: {type(e).__name__}")
            continue
        for p in profs:
            if p["name"] not in SCORE_PROFILES[cfg.kind]:
                continue
            checked += 1
            dirty = False
            for name, want in GUARDED_SCORES.items():
                item = next((x for x in p.get("formatItems", [])
                             if x.get("name") == name), None)
                if item is None:
                    absent.append(f"{p['name']}/{name}")
                    continue
                if item.get("score") == want:
                    continue
                item["score"] = want
                dirty = True
                fixed.append(f"{p['name']}/{name}")
            if not dirty:
                continue
            try:
                await c._client.put(f"/qualityprofile/{p['id']}", json=p)
                joblog.log(f"score guard: restored English-audio scores on "
                           f"{cfg.name}/{p['name']} — a sync had cleared them",
                           "warn")
            except Exception as e:
                errs.append(f"{p['name']}: {type(e).__name__}")
    # A profile that does not carry the format at all is not "intact" - it is
    # a gap this guard cannot close, because adding a format to a profile is a
    # bigger decision than restoring a number. Say so rather than counting it.
    msg = (f"restored {len(fixed)}: {', '.join(fixed[:4])}" if fixed
           else f"{checked} profile(s) intact, {len(GUARDED_SCORES)} score(s) "
                f"each")
    if absent:
        msg += f"; not on {len(absent)}: {', '.join(absent[:3])}"
    if errs:
        msg += f"; errors: {', '.join(errs[:3])}"
    STATS["score"].update(last_run=time.time(), fixed=len(fixed),
                          last_result=msg, detail=(fixed or absent)[:20])
    return msg


async def run_guard() -> str:
    fixed, ok, errs = [], [], []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in TARGET_2160:
            continue
        c = shared_client(cfg)
        try:
            profs = await c._get("/qualityprofile")
        except Exception as e:
            errs.append(f"{cfg.name}: {type(e).__name__}")
            continue
        for p in profs:
            if p["name"] not in TARGET_2160[cfg.kind]:
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
    updated, new_fmts, errs = [], [], []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in ANIME_PROFILES:
            continue
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
                new_fmts.append(f"{cfg.kind}: {name}")
                continue
            cur = mine[name]
            if _spec_key(cur) == _spec_key(tcf):
                continue
            body = dict(cur)
            body["specifications"] = tcf["specifications"]
            body["includeCustomFormatWhenRenaming"] = cur.get(
                "includeCustomFormatWhenRenaming", False)
            try:
                await c._client.put(f"/customformat/{cur['id']}", json=body)
                updated.append(f"{cfg.name}/{name}")
                joblog.log(f"TRaSH anime sync: updated custom format "
                           f"{name!r} on {cfg.name}", "info")
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__}")
    STATS["trash"].update(last_run=time.time(), updated=len(updated),
                          new_formats=sorted(set(new_fmts))[:20])
    msg = (f"updated {len(updated)}: {', '.join(updated[:6])}" if updated
           else "all anime formats match the guides")
    if new_fmts:
        msg += f"; {len(set(new_fmts))} new in the guides (not auto-added)"
    if errs:
        msg += f"; errors: {', '.join(errs[:4])}"
    STATS["trash"]["last_result"] = msg
    return msg


def ensure_defaults() -> None:
    """First sight of the new toggle: inherit the profile guard's setting.

    The score guard shipped inside the profile guard, so it was already
    running for anyone who had that on. Defaulting a split-out job to OFF
    would silently switch off protection somebody already had - a refactor
    should not change behaviour. After this one write the two are independent.
    """
    from .gate import kv_get, kv_set, get_toggle
    if kv_get("arrs.score_guard") is None:
        kv_set("arrs.score_guard",
               "1" if get_toggle("arrs.profile_guard") else "0")


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
            if get_toggle("arrs.score_guard"):
                STATS["running"] = "score guard"
                await run_score_guard()
            STATS["score"]["next_run"] = time.time() + POLL_S
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
