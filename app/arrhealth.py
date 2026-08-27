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
        for key in sorted(now - _SEEN):
            h = detail[key]
            joblog.log(f"{key[0]}: {h.get('source') or h.get('type')} — "
                       f"{h.get('message','')[:140]}",
                       "error" if h.get("level") == "error" else "warn",
                       system="arrhealth")
        for key in sorted(_SEEN - now):
            joblog.log(f"{key[0]}: {key[1] or key[2]} cleared", "ok",
                       system="arrhealth")
    _SEEN = now

    STATE.update(arrs=rows, warnings=len(now), at=time.time(),
                 checked=STATE["checked"] + 1)
    return snapshot()


def snapshot() -> dict:
    d = dict(STATE)
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
