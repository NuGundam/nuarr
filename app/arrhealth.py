r"""What Sonarr and Radarr say is wrong with themselves, watched over time.

WHY A WATCHER AND NOT A LIVE FETCH. The Arrs page used to ask both servers the
moment it was opened, which is two API calls per arr and about 600 ms before
anything appeared - and it only ever told you the state at the instant you
happened to look. A health warning is not a fact about now; it is a fact with a
BEGINNING and usually an END, and the interesting parts are both edges:

    19:04  Sonarr: indexer NZBFinder unavailable for more than 6 hours
    21:37  Sonarr: that cleared

Neither line exists if the answer is only ever computed on demand. So this
polls on its own schedule, keeps the last answer for the page to read
instantly, and logs the transitions - which also means "when did that start?"
is answerable afterwards from the log filter, without anyone having been
watching at the time.

DEDUPED BY (arr, source, type), not by message text. Both arrs raise the same
IndexerLongTermStatusCheck for the same Prowlarr indexer, and the message
carries a running duration that changes on every poll - matching on the whole
string would report the same warning as newly appeared every few minutes.
"""

from __future__ import annotations

import asyncio
import json
import time

from . import joblog
from .config import SETTINGS

# Every five minutes. Health checks are two cheap calls per arr, and the things
# they report - an indexer down, a disk filling, a root folder missing - change
# on the timescale of minutes at best. Polling harder would add noise, not news.
POLL_S = 300.0

STATE: dict = {"arrs": [], "warnings": 0, "at": 0.0, "checked": 0}

# The set of (arr, source, type) seen on the previous pass, so a change can be
# named rather than just redrawn.
_SEEN: set[tuple] = set()

_LEVEL = {"error": "error", "warning": "warn", "notice": "info"}

# ---- SWITCHING A CHECK OFF -------------------------------------------------
#
# Not every health check is nuarr's business. AllowedHostsCheck fires on both
# arrs and will fire forever: on a LAN it is a deliberate configuration, not a
# fault, so there is nothing to fix and nothing to wait for. Left alone it sits
# in the Attention tile permanently, and a counter that never reaches zero is a
# counter nobody reads - which is how a real warning appearing underneath it
# goes unnoticed.
#
# SWITCHED OFF, NOT HIDDEN. A muted check is still fetched and still shown on
# its arr's card, greyed, with its switch beside it. What changes is that it
# stops COUNTING: out of STATE["warnings"], out of the tile, out of Needs
# attention, and out of the log when it appears or clears. Hiding the row would
# mean the only way to find what you had silenced was to remember you had.
#
# KEYED (arr, source, type) - the key the poller already dedupes on. Per-arr on
# purpose: Sonarr's indexer being down and Radarr's are two different facts,
# and silencing one must not silence the other.
_MUTE_KEY = "arrhealth.muted"
_MUTED: set[tuple[str, str, str]] | None = None


def mkey(arr: str, source: str, type_: str) -> tuple[str, str, str]:
    return (arr or "", source or "", type_ or "")


def muted() -> set[tuple[str, str, str]]:
    global _MUTED
    if _MUTED is None:
        from . import db
        try:
            _MUTED = {tuple(x[:3]) for x in
                      json.loads(db.kv_get(_MUTE_KEY) or "[]")
                      if isinstance(x, list) and len(x) == 3}
        except Exception:                                # noqa: BLE001
            _MUTED = set()
    return _MUTED


def set_muted(arr: str, source: str, type_: str, off: bool) -> int:
    """Switch one check off (off=True) or back on. Returns the new count."""
    from . import db
    global _MUTED
    key = mkey(arr, source, type_)
    m = set(muted())
    m.add(key) if off else m.discard(key)
    _MUTED = m
    db.kv_set(_MUTE_KEY, json.dumps(sorted(list(k) for k in m)))
    # THE COUNT MOVES NOW, not at the next poll five minutes from now. The
    # tile reads STATE["warnings"], so leaving that stale would make pressing
    # the switch look like it had done nothing at all.
    STATE["warnings"] = len(warnings_list(10_000))
    joblog.log(f"{arr}: {source or type_} switched "
               f"{'off' if off else 'back on'}"
               f" - {STATE['warnings']} warning(s) now counted",
               "info", system="arrhealth")
    return STATE["warnings"]


async def _one(cfg) -> dict:
    """One arr's reachability, version and health list."""
    from .arr import shared_client

    row = {"arr": cfg.name, "kind": cfg.kind, "ok": False,
           "version": None, "error": None, "health": []}
    c = shared_client(cfg)
    try:
        st = await c._get("/system/status")
        row["ok"] = True
        row["version"] = st.get("version")
    except Exception as e:                               # noqa: BLE001
        row["error"] = f"{type(e).__name__}"
        return row
    try:
        for h in (await c._get("/health") or []):
            kind = str(h.get("type") or "").lower()
            if kind == "ok":
                continue
            row["health"].append({
                "type": h.get("type"),
                "level": _LEVEL.get(kind, "warn"),
                "source": h.get("source"),
                "message": (h.get("message") or "")[:200],
                "url": h.get("wikiUrl") or "",
            })
    except Exception:                                    # noqa: BLE001
        pass
    return row


async def refresh() -> dict:
    """Ask both arrs now, update STATE, and log anything that changed."""
    global _SEEN
    cfgs = [c for c in SETTINGS.arrs if c.enabled and c.api_key]
    rows = list(await asyncio.gather(*[_one(c) for c in cfgs])) if cfgs else []

    now: set[tuple] = set()
    detail: dict[tuple, dict] = {}
    for r in rows:
        for h in r["health"]:
            key = (r["arr"], h.get("source") or "", h.get("type") or "")
            now.add(key)
            detail[key] = h

    # First pass after a restart establishes the baseline. Announcing eleven
    # pre-existing warnings as "new" every time nuarr restarts would train the
    # eye to skip them, which is the opposite of the point.
    if STATE["checked"]:
        _off = muted()
        for key in sorted(now - _SEEN):
            if key in _off:
                continue
            h = detail[key]
            joblog.log(f"{key[0]}: {h.get('source') or h.get('type')} — "
                       f"{h.get('message','')[:140]}",
                       "error" if h.get("level") == "error" else "warn",
                       system="arrhealth")
        for key in sorted(_SEEN - now):
            if key in _off:
                continue
            joblog.log(f"{key[0]}: {key[1] or key[2]} cleared", "ok",
                       system="arrhealth")
    _SEEN = now

    # THE COUNT IS THE LENGTH OF THE LIST THE PANEL SHOWS. It used to be
    # len(now) - a set keyed on (arr, source, type) - while the panel walked
    # a different structure, which is how the two came to disagree. arrs is
    # set first so warnings_list() reads this pass, not the last one.
    STATE.update(arrs=rows, at=time.time(), checked=STATE["checked"] + 1)
    STATE["warnings"] = len(warnings_list(10_000))
    return snapshot()


def warnings_list(limit: int = 50) -> list[dict]:
    r"""The warnings themselves, flattened, newest state first.

    WHY THIS EXISTS. The tile counted STATE["warnings"] and the Needs
    attention panel listed STATE["warning_list"] - a key this module has
    never set. So the tile read "2 - arr health 2" over a panel reading
    "nothing needs attention, 0 shown of 0", and there was no way to find out
    what the two were. Erik: "no info or link to issue".

    Everything needed was already fetched and thrown away: _one() reads each
    arr's /health and keeps the type, the source, the level, the message and
    Sonarr's own wikiUrl for it. This hands that over, and the tile counts
    the same list it returns - so the two cannot disagree again.
    """
    out: list[dict] = []
    off = muted()
    for r in STATE.get("arrs") or []:
        for h in r.get("health") or []:
            if mkey(r.get("arr"), h.get("source"), h.get("type")) in off:
                continue
            out.append({
                "arr": r.get("arr") or "",
                "kind": r.get("kind") or "",
                "type": h.get("type") or "",
                "level": h.get("level") or "warn",
                "source": h.get("source") or "",
                "message": h.get("message") or "",
                "url": h.get("url") or "",
            })
        # AN ARR THAT DOES NOT ANSWER IS THE LOUDEST WARNING OF ALL, and it
        # was never counted: _one() returns early on a connection failure, so
        # `health` is empty and the arr simply vanished from the reckoning.
        if r.get("error") and mkey(
                r.get("arr"), "nuarr", "Unreachable") not in off:
            out.append({
                "arr": r.get("arr") or "", "kind": r.get("kind") or "",
                "type": "Unreachable", "level": "error", "source": "nuarr",
                "message": f"nuarr could not reach this arr ({r['error']})",
                "url": "",
            })
    return out[:limit]


def snapshot() -> dict:
    d = dict(STATE)
    # THE CARDS SHOW EVERY CHECK, muted or not, so each row carries its own
    # state rather than the page rebuilding the key and guessing. Copied, not
    # annotated in place: STATE belongs to the poller and a view must not
    # write into it.
    off = muted()
    d["arrs"] = [dict(r, health=[
        dict(h, muted=mkey(r.get("arr"), h.get("source"), h.get("type")) in off)
        for h in (r.get("health") or [])],
        unreachable_muted=mkey(r.get("arr"), "nuarr", "Unreachable") in off)
        for r in (STATE.get("arrs") or [])]
    d["muted"] = [list(k) for k in sorted(off)]
    # The list rides with the count, so anything reading the snapshot gets
    # both and cannot pick one without the other.
    d["warning_list"] = warnings_list()
    d["age_s"] = round(time.time() - STATE["at"]) if STATE["at"] else None
    d["poll_s"] = POLL_S
    return d


async def watch() -> None:
    """Poll on a schedule, and never let a failure stop the loop."""
    from . import schedules

    schedules.register(
        "arrhealth", "Arr health", "Integrations", POLL_S,
        what="Asks Sonarr and Radarr what is wrong with themselves - indexers "
             "down, missing root folders, disk space - and records when each "
             "warning appears and when it clears.")
    await asyncio.sleep(45)          # let the arrs finish their own startup
    while True:
        try:
            schedules.beat("arrhealth")
            r = await refresh()
            if r["warnings"]:
                schedules.REG["arrhealth"]["last_result"] = (
                    f"{r['warnings']} warning(s)")
            else:
                schedules.REG["arrhealth"]["last_result"] = "all clear"
        except Exception as e:                           # noqa: BLE001
            joblog.log(f"arr health check: {type(e).__name__}: {e}", "warn",
                       system="arrhealth")
        await asyncio.sleep(POLL_S)
