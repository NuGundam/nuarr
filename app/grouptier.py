r"""Measured release groups -> custom formats the arrs score. The scorecard's
second stage (Erik, 2026-10-29).

WHAT IT WRITES. Three formats per arr, owned by nuarr and nobody else:
    Nuarr: measured group A      groups whose pictures measure cleanest here
    Nuarr: measured group B      the next band
    Nuarr: measured group poor   banding, blocking or blur at the bottom, or
                                 a group the incident log keeps naming
Each is one release-title regex over the group names in that band, scored
in every profile that has a cutoff score, scaled to that profile: A is 2 %
of the cutoff, B half of that, poor minus A. On the Anime profile (cutoff
15 400) that is +308 / +154 / -308: enough to pick between two releases in
the same TRaSH tier, never enough to climb a tier, cross a -10000 ban or
outweigh the English-audio format. The "Nu" profiles count in hundreds of
thousands, and the same 2 % stays a nudge there.

WHY NOT EDIT THE TRASH TIERS. arrguard rewrites those from the guides, so a
change there is undone at the next sync - and the tiers are other people's
reputation lists; these are this library's measurements.

WHEN A GROUP MOVES. Only with 6+ measured files across 2+ shows, and only
once its band has held for a week (STABLE_S) - a group with no band yet
takes its first one at once, so the first run is not an empty week. Bands
are per library (the Anime Shows median is not the Movies median); a group
that lands in two bands across libraries of one arr is left out of both.

Nothing reaches an arr until kv grouptier.armed is "1"; before that the
pass only computes and the Release groups page shows what it would do.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from . import joblog
from .arr import shared_client
from .config import SETTINGS
from .db import kv_get, kv_set

POLL_S = 24 * 3600
STABLE_S = 7 * 86400
MIN_MEASURED = 6
MIN_SHOWS = 2
A_AT, B_AT, POOR_AT = 75, 50, 25     # scorecard score bands (100 = cleanest)
POOR_ISSUES = 3
PCT_A = 0.02                         # of the profile's cutoff score
NAMES = {"A": "Nuarr: measured group A", "B": "Nuarr: measured group B",
         "poor": "Nuarr: measured group poor"}
KV_ARMED = "grouptier.armed"
KV_STATE = "grouptier.state"         # {kind: {grp: {"tier","cand","since"}}}
KV_LAST = "grouptier.last"
KV_LOG = "grouptier.log"
STATS: dict = {"running": False, "last_run": 0.0, "last_result": "", "next_run": 0.0}


def armed() -> bool:
    return (kv_get(KV_ARMED) or "") == "1"


def set_armed(on: bool) -> None:
    kv_set(KV_ARMED, "1" if on else "0")


def _kind_for(library: str) -> str:
    lib = next((l for l in SETTINGS.libraries if l.name == library), None)
    if lib is None:
        return ""
    return "radarr" if lib.kind == "movie" else "sonarr"


# WORDS THE ARR PARSER MISTAKES FOR A GROUP. "Dual-Audio" gives "Audio",
# "10-bit" gives "bit", a hand import gives "Manual" or "Unknown". In a
# release-title regex -(Audio) or -(bit) would match nearly every anime
# title and hand the whole indexer a B score, so these never get a band.
_JUNK = {"audio", "dual", "bit", "anime", "unknown", "manual", "web", "webdl", "webrip", "multi",
         "hevc", "x265", "x264", "aac", "flac", "dub", "dubbed", "sub", "subs", "raw", "raws",
         "eng", "jpn", "english", "japanese", "bluray", "remux", "hdr", "sdr", "dv", "uhd"}


def _junk(g: str) -> bool:
    from .relgroup import junk
    return junk(g) or (g or "").strip().lower() in _JUNK


def _band(r: dict) -> str:
    """A group's band from its scorecard row, "" when there is not enough."""
    if r.get("measured", 0) >= 3 and r.get("issues", 0) >= POOR_ISSUES:
        return "poor"
    if r.get("measured", 0) < MIN_MEASURED or r.get("shows", 0) < MIN_SHOWS or r.get("score") is None:
        return ""
    s = r["score"]
    if s >= A_AT and not r.get("issues"):
        return "A"
    if s >= B_AT:
        return "B"
    if s <= POOR_AT:
        return "poor"
    return ""


def _load_state() -> dict:
    try:
        return json.loads(kv_get(KV_STATE) or "{}")
    except Exception:                                            # noqa: BLE001
        return {}


def compute(now: float | None = None, commit: bool = False) -> dict:
    """Bands per library, then per arr, with the week's hold applied.
    commit=True records the candidates' first-seen times."""
    from .groupscore import scorecard
    now = now or time.time()
    state = _load_state()
    libs: dict = {}
    want: dict = {}                     # kind -> grp -> set(bands)
    for l in SETTINGS.libraries:
        kind = _kind_for(l.name)
        if not kind:
            continue
        try:
            rows = scorecard(l.name)["groups"]
        except Exception as e:                                   # noqa: BLE001
            libs[l.name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        bands = {r["grp"]: b for r in rows if not _junk(r["grp"]) and (b := _band(r))}
        libs[l.name] = {"kind": kind, "bands": bands,
                        "eligible": sum(1 for r in rows if r.get("measured", 0) >= MIN_MEASURED and r.get("shows", 0) >= MIN_SHOWS)}
        for g, b in bands.items():
            want.setdefault(kind, {}).setdefault(g, set()).add(b)
    out_tiers: dict = {}
    pending: list = []
    for kind in ("sonarr", "radarr"):
        st = state.setdefault(kind, {})
        cands = {g: (bs.pop() if len(bs) == 1 else "") for g, bs in want.get(kind, {}).items()}
        tiers: dict = {}
        for g in set(cands) | set(st):
            if _junk(g):                      # out at once, no week's hold for a word
                st.pop(g, None)
                continue
            cand = cands.get(g, "")
            rec = st.get(g) or {"tier": "", "cand": cand, "since": now}
            if rec.get("cand") != cand:
                rec = {"tier": rec.get("tier", ""), "cand": cand, "since": now}
            cur = rec.get("tier", "")
            if cand != cur:
                if not cur or now - float(rec.get("since") or now) >= STABLE_S:
                    rec["tier"] = cand            # first band at once; moves after a week
                else:
                    pending.append({"kind": kind, "grp": g, "from": cur or "-", "to": cand or "-",
                                    "due": float(rec["since"]) + STABLE_S})
            st[g] = rec
            if rec["tier"]:
                tiers[g] = rec["tier"]
        for g in [g for g, r in st.items() if not r.get("tier") and not r.get("cand")]:
            del st[g]
        out_tiers[kind] = tiers
    if commit:
        kv_set(KV_STATE, json.dumps(state))
    return {"libraries": libs, "tiers": out_tiers, "pending": sorted(pending, key=lambda p: p["due"])}


def _regex(groups: list) -> str:
    alts = "|".join(sorted(re.escape(g) for g in groups))
    return rf"\[({alts})\]|-({alts})\b"


async def _apply_arr(cfg, tiers: dict, dry: bool) -> list:
    """Make the three formats say these groups and score them in every
    profile with a cutoff. Returns the lines of what changed."""
    c = shared_client(cfg)
    lines: list = []
    mine = {f["name"]: f for f in await c._get("/customformat")}
    ids: dict = {}
    for band, name in NAMES.items():
        groups = sorted(g for g, b in tiers.items() if b == band)
        spec = [{"name": "measured here", "implementation": "ReleaseTitleSpecification",
                 "negate": False, "required": True,
                 "fields": [{"name": "value", "value": _regex(groups) if groups else r"(?!x)x"}]}]
        cur = mine.get(name)
        if cur is None:
            lines.append(f"{cfg.name}: create {name!r} with {len(groups)} group(s)")
            ids[band] = -1                   # dry: still list the profile scores below
            if not dry:
                made = await c._post("/customformat", {"name": name, "includeCustomFormatWhenRenaming": False,
                                                       "specifications": spec})
                ids[band] = made["id"]
            continue
        ids[band] = cur["id"]
        have = ((cur.get("specifications") or [{}])[0].get("fields") or [{}])[0].get("value")
        if have != spec[0]["fields"][0]["value"]:
            lines.append(f"{cfg.name}: {name!r} -> {len(groups)} group(s): {', '.join(groups[:8])}{'...' if len(groups) > 8 else ''}")
            if not dry:
                body = dict(cur)
                body["specifications"] = spec
                await c._client.put(f"/customformat/{cur['id']}", json=body)
    for p in await c._get("/qualityprofile"):
        cutoff = int(p.get("cutoffFormatScore") or 0)
        if cutoff <= 0:
            continue
        a = max(1, round(cutoff * PCT_A))
        want = {"A": a, "B": a // 2, "poor": -a}
        changed = []
        items = p.get("formatItems") or []
        for band, fid in ids.items():
            it = next((x for x in items if x.get("format") == fid), None)
            if it is None:
                if dry:
                    changed.append(f"{band} {want[band]:+}")
                continue
            if int(it.get("score") or 0) != want[band]:
                it["score"] = want[band]
                changed.append(f"{band} {want[band]:+}")
        if changed:
            lines.append(f"{cfg.name}: profile {p['name']!r}: {', '.join(changed)}")
            if not dry:
                await c._client.put(f"/qualityprofile/{p['id']}", json=p)
    return lines


async def run(force: bool = False) -> dict:
    """One pass: compute, and when armed, write. Returns the preview either way."""
    STATS["running"] = True
    try:
        live = armed()
        plan = await asyncio.to_thread(compute, None, True)
        lines: list = []
        for cfg in SETTINGS.arrs:
            if not cfg.enabled or cfg.kind not in ("sonarr", "radarr"):
                continue
            try:
                lines += await _apply_arr(cfg, plan["tiers"].get(cfg.kind, {}), dry=not live)
            except Exception as e:                               # noqa: BLE001
                lines.append(f"{cfg.name}: {type(e).__name__}: {e}"[:200])
        plan["would"] = lines
        plan["armed"] = live
        res = (f"{'wrote' if live else 'would write'} {len(lines)} change(s)" if lines
               else "formats already match the measurements")
        STATS["last_run"] = time.time()
        STATS["last_result"] = res
        kv_set(KV_LAST, json.dumps({"at": STATS["last_run"], "result": res, "lines": lines[:40],
                                    "counts": {k: {b: sum(1 for x in t.values() if x == b) for b in NAMES}
                                               for k, t in plan["tiers"].items()}}))
        if live and lines:
            try:
                log = json.loads(kv_get(KV_LOG) or "[]")
            except Exception:                                    # noqa: BLE001
                log = []
            log = [{"at": time.time(), "lines": lines}] + log
            kv_set(KV_LOG, json.dumps(log[:30]))
            joblog.log("measured groups: " + res, "ok")
            for ln in lines:
                joblog.log("  " + ln, "info")
        return plan
    finally:
        STATS["running"] = False


def snapshot() -> dict:
    try:
        last = json.loads(kv_get(KV_LAST) or "{}")
    except Exception:                                            # noqa: BLE001
        last = {}
    try:
        log = json.loads(kv_get(KV_LOG) or "[]")
    except Exception:                                            # noqa: BLE001
        log = []
    state = _load_state()
    tiers = {k: {g: r["tier"] for g, r in st.items() if r.get("tier")} for k, st in state.items()}
    pending = [{"kind": k, "grp": g, "from": r.get("tier") or "-", "to": r.get("cand") or "-",
                "due": float(r.get("since") or 0) + STABLE_S}
               for k, st in state.items() for g, r in st.items() if r.get("cand") != r.get("tier")]
    return {"armed": armed(), "tiers": tiers, "pending": sorted(pending, key=lambda p: p["due"]),
            "last": last, "log": log[:10], "names": NAMES, "pct_a": PCT_A, "stable_d": STABLE_S // 86400,
            "min_measured": MIN_MEASURED, "min_shows": MIN_SHOWS, **STATS}


async def watch() -> None:
    STATS["next_run"] = time.time() + 900
    await asyncio.sleep(900)
    while True:
        try:
            await run()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"measured groups: {type(e).__name__}: {e}", "error")
        STATS["next_run"] = time.time() + POLL_S
        await asyncio.sleep(POLL_S)
