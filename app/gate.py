r"""
nuarr - the job gate

Replaces two scheduled PowerShell scripts with one always-on check:

    Tautulli-TdarrPause.ps1        pause the node while Plex is transcoding
    Pause-TdarrDuringBalance.ps1   pause the node while DrivePool is balancing

Both existed because Tdarr had no idea what else the box was doing. It would
start a GPU encode while Plex was already transcoding for a viewer, or move a
working file while the balancer was relocating it underneath - which is exactly
how .tmp files went missing mid-rename.

HOW IT BEHAVES
--------------
The gate never kills anything. It only decides whether a NEW job may start:

    open    -> dispatch freely
    holding -> in-flight jobs run to completion, nothing new begins

That is the same "pause the node" semantics as before, minus the node.

Each reason is independent and individually switchable, and every one reports a
human sentence, so the dashboard can say *why* nothing is running instead of
looking broken.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from .config import SETTINGS
from .db import kv_get, kv_set
# joblog was already being CALLED here - twice, in _cross_check - without ever
# being imported. Those calls only run when plex_cross_check is enabled, which
# it is not, so the NameError sat latent instead of being noticed.
from . import joblog

# Cache probe results briefly. The gate is consulted on every dispatch; without
# this, a queue draining 200 files would hammer Tautulli 200 times a second.
_CACHE: dict[str, tuple[float, "Reason"]] = {}
_CACHE_LOCKS: dict[str, threading.Lock] = {}

# Below this, the balancer is shuffling a rounding error and there is no reason
# to hold the queue. 1 GB moves in seconds on this pool.


def _tune(key: str) -> float:
    """Read a gate timing at call time, so edits apply without a restart.

    These were module constants (_TTL = 5.0, STALE_MINUTES = 15.0). Reading them
    live is the whole point: the right grace period depends on who is watching,
    and nobody wants to restart the server to change it.
    """
    from . import workers
    try:
        return float(getattr(workers.get(), key))
    except Exception:
        return float(workers.LIMITS[key][2])


# When the last stream stopped. A grace period keys off this so a viewer moving
# between episodes does not get a GPU-loaded server the instant they pause.
_LAST_BUSY: dict[str, float] = {}


# How each check is NAMED on the dashboard. The pills were rendering the raw
# internal key - "plex", "arrs", "drivepool" - which reads like a variable name
# rather than a label, and "arrs" in particular means nothing unless you already
# know the codebase.
LABELS = {
    "manual": "Manual",
    "plex": "Plex",
    "cache": "Cache",
    "arrs": "Sonarr / Radarr",
    "audiolang": "Audio language",
}


def _plural(n: int, one: str, many: str = "") -> str:
    """"1 session" / "2 sessions" - never "1 session(s)".

    The (s) form was everywhere in this panel. It is the written equivalent of
    not bothering, and on the line you read most often - "1 session(s)
    transcoding" - it is also the line most likely to be wrong in other ways.
    """
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def _names(sessions: list[dict]) -> list[str]:
    """Who is watching, de-duplicated, in a sensible order.

    Tautulli exposes the display name as friendly_name, but it is not always
    populated - a session from a managed user or a fresh client can arrive with
    it blank, which is how a viewer would silently vanish from the list while
    still being counted. Fall back through the other name fields rather than
    dropping the person.
    """
    out: list[str] = []
    for s in sessions:
        n = str(s.get("friendly_name") or s.get("user")
                or s.get("username") or "").strip()
        if n and n not in out:
            out.append(n)
    return out


def _who(sessions: list[dict], limit: int = 3) -> str:
    """A readable list of names: "a", "a and b", "a, b and 4 more".

    NOT USED BY THE GATE ROW ANY MORE, deliberately - the session cards below
    it name everybody, with a poster and a device, and the row repeating them
    was the duplication this panel was cleaned up to remove. Kept because it is
    the right helper the moment a name is needed somewhere without cards, such
    as a log line or a notification.
    """
    ns = _names(sessions)
    if not ns:
        return ""
    if len(ns) == 1:
        return ns[0]
    if len(ns) <= limit:
        return ", ".join(ns[:-1]) + f" and {ns[-1]}"
    return ", ".join(ns[:limit]) + f" and {len(ns) - limit} more"


@dataclass
class Reason:
    blocked: bool
    name: str
    detail: str = ""
    error: str | None = None
    # SUPPORTING LINES, kept apart from the headline.
    #
    # Everything used to be concatenated into detail with " - " and " · ", so
    # the Plex row was one 120-character run that had to be read end to end to
    # find the part you wanted. These render underneath, dimmer.
    extra: list[str] = field(default_factory=list)
    # WHAT HAS TO HAPPEN FOR THIS HOLD TO LIFT.
    #
    # The panel could say a thing was holding but never what would release it,
    # so the only way to find out was to read the source. Every blocking check
    # now has to answer that question.
    clears: str = ""
    # A LIMIT SHORT OF A HOLD.
    #
    # Plex holds encode outright but does not hold passthrough - it only bars it
    # from the spindles being watched. Saying "passthrough is still running" is
    # true and reads as "Plex does not affect passthrough", which is not; saying
    # it is held is plainly wrong while four copies are in flight. This is the
    # missing third state: running, but not everywhere.
    restricts: str = ""
    # Which worker pools this reason holds. None = all of them, which is the
    # right default for every check except Plex: a DrivePool balance or an arr
    # rename can move or rewrite a file under ANY job, while a Plex transcode
    # only competes for the GPU that encode jobs need.
    pools: tuple[str, ...] | None = None
    # IS SOMETHING ACTUALLY HAPPENING HERE, even though it is not holding?
    #
    # Every non-blocking check rendered as the same flat grey pill, so "Plex has
    # two people watching but neither is transcoding" looked identical to "Plex
    # is not configured". They are very different facts - one is a live
    # subsystem doing work, the other is silence - and the difference is exactly
    # what you want at a glance. The UI colours and animates on this.
    active: bool = False

    def holds(self, pool: str) -> bool:
        return self.blocked and (self.pools is None or pool in self.pools)

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name.title())

    @property
    def scope(self) -> str:
        """Which pools this reason holds, in words rather than a code."""
        if self.pools is None:
            return "all jobs"
        return " and ".join(self.pools) + " jobs"


@dataclass
class GateStatus:
    open: bool
    reasons: list[Reason] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def blocking(self) -> list[Reason]:
        return [r for r in self.reasons if r.blocked]

    def blocking_for(self, pool: str) -> list[Reason]:
        return [r for r in self.reasons if r.holds(pool)]

    def open_for(self, pool: str) -> bool:
        """Can this pool start work, even if another pool is held?"""
        return not self.blocking_for(pool)

    def why(self) -> str:
        b = self.blocking
        if not b:
            return "open - nothing is holding jobs back"
        out = []
        for r in b:
            scope = "" if r.pools is None else f" [{'/'.join(r.pools)} only]"
            out.append(f"{r.name}: {r.detail}{scope}")
        return "; ".join(out)

    # ALL THE POOLS THERE ARE. A hold is only meaningful against this list -
    # "Plex is holding encode" is half the story without "passthrough is still
    # running", which is the half that tells you work is still going out.
    # subocr is listed so a hold names it explicitly rather than silently
    # letting it through. It reads and rewrites pool files exactly as
    # passthrough does, so anything holding passthrough for disk reasons must
    # hold this too.
    POOLS = ("encode", "passthrough", "subocr")

    def headline(self) -> dict:
        """What the banner says: what is held, by whom, and what frees it.

        Deliberately NOT a repeat of the rows underneath. The banner used to
        print the blocking check's detail verbatim, so the Plex line appeared
        twice, word for word, a few pixels apart - the panel's most-read area
        spent on saying the same thing twice.
        """
        b = self.blocking
        if not b:
            return {"open": True,
                    "head": "Nothing is holding the queue",
                    "sub": "All checks are clear — jobs start as soon as the "
                           "queue has work",
                    "held": [], "running": list(self.POOLS)}

        held = [p for p in self.POOLS if self.blocking_for(p)]
        running = [p for p in self.POOLS if p not in held]
        who = " and ".join(dict.fromkeys(r.label for r in b))
        what = ("Everything is held" if len(held) == len(self.POOLS)
                else " and ".join(held).capitalize() + " jobs are held")
        head = f"{what} by {who}"
        # The good news half: a partial hold still gets work out of the door,
        # and that was invisible - the banner just said HOLDING.
        sub = ""
        if running:
            sub = " and ".join(running).capitalize() + " jobs are still running"
            # ...but say where they CANNOT go. Without this the line reads as
            # "Plex does not affect passthrough", when in fact it bars it from
            # every spindle being watched.
            limits = [r.restricts for r in b if r.restricts]
            if limits:
                sub += " — " + "; ".join(limits)
        return {"open": False, "head": head, "sub": sub,
                "held": held, "running": running}

    def as_dict(self) -> dict:
        return {
            "open": self.open,
            "why": self.why(),
            "headline": self.headline(),
            "checked_at": self.checked_at,
            "reasons": [
                {"name": r.name, "label": r.label, "blocked": r.blocked,
                 "detail": r.detail, "extra": list(r.extra),
                 "clears": r.clears, "scope": r.scope,
                 "pools": list(r.pools) if r.pools else None,
                 "error": r.error, "active": r.active}
                for r in self.reasons
            ],
        }


# ------------------------------------------------------------- toggles ----
DEFAULTS = {
    "gate.plex": "1",
    "gate.plex_transcodes_only": "1",   # matches OnlyTranscodes=$true
    # A Plex transcode competes for the GPU, which is what ENCODE jobs need.
    # Passthrough is a stream copy - no GPU at all - so holding it while someone
    # watches something was costing throughput for no benefit. With this on, a
    # Plex transcode holds encode only, and passthrough keeps running EXCEPT on
    # the pool disk Plex is reading from, where the two would fight for the same
    # spindle. That exception is enforced in jobs._claim() via plex_disks().
    "gate.plex_encode_only": "1",
    # A throttled Plex transcode has run ahead of the viewer and PARKED - the
    # encoder is idle (transcode_speed 0.0) until the buffer drains. Holding the
    # queue for it wastes exactly the resource it is not using. Only applies
    # when the session is also comfortably ahead; see _is_parked().
    "gate.plex_ignore_throttled": "1",
    # While ANY Plex client is playing - direct play included - drop the disk
    # priority of running encoders so a viewer's reads go first. Does not stop
    # or slow the queue by itself; it only decides who yields when both want
    # the same spindle at the same moment.
    "gate.plex_io_throttle": "1",
    # Generic disk-pressure check. Replaced a product-specific one that asked
    # product whether it was balancing; this asks the disks whether they are
    # busy, which is the question that actually matters and has an answer on
    # every storage setup.
    "gate.disk_busy": "1",
    "gate.arrs": "1",
    "gate.manual_pause": "0",
}

# Pool disks the current Plex sessions are streaming from. Read by the claim
# path so a passthrough job can avoid the one spindle a viewer is pulling from
# while still running freely on the other eleven.
_PLEX_DISKS: set[str] = set()
_PLEX_DISK_CACHE: dict[str, str | None] = {}     # file path -> disk label
# The live sessions themselves, for the dashboard. Deliberately a SEPARATE
# global from the counts the gate reasons about: this one exists to be looked
# at, and nothing in check_plex() reads it, so enriching it can never change a
# scheduling decision.
_PLEX_LIVE: list = []


def plex_live() -> list:
    return list(_PLEX_LIVE)


# How many sessions are PLAYING anything at all - direct play included. Drives
# the I/O-priority throttle, which is deliberately independent of whether the
# gate is holding: a direct play never holds the queue, but it still wants the
# disk queue more than a background remux does.
_PLEX_PLAYING: int = 0


def plex_playing() -> int:
    return _PLEX_PLAYING


def plex_disks() -> set[str]:
    return set(_PLEX_DISKS)


# HOW MUCH BUFFER THE THINNEST VIEWER ON EACH SPINDLE HAS, in seconds.
#
# Throttling treated every viewer the same: someone with four minutes banked
# got exactly the treatment of someone with twenty seconds, which is both too
# cautious in one case and not nearly enough in the other. The number that
# decides whether a read has to wait is the one already on the session cards,
# so this publishes it per disk for the pacing code to act on.
#
# MINIMUM, not average: two viewers on one spindle and the one about to stall
# is the only one that matters.
_PLEX_LEAD: dict[str, float] = {}


def viewer_lead(disk: str) -> float | None:
    """Seconds of buffer the thinnest viewer on `disk` has, or None.

    None means "no viewer there, or no measurement" - which callers must treat
    as "no reason to hold back", never as zero. Reading an absent measurement
    as a starving viewer would pause the queue whenever Plex went quiet.
    """
    if not disk:
        return None
    v = _PLEX_LEAD.get(disk)
    return None if v is None else float(v)


def viewer_leads() -> dict:
    return dict(_PLEX_LEAD)


# HOW MUCH BUFFER *THIS* VIEWER ACTUALLY NEEDS, in seconds, per spindle.
#
# One flat number of seconds was wrong in three separate ways, and all three
# showed up on the same screen:
#
#   * A STREAM BUFFERED TO THE END needs nothing. It will issue no further
#     reads no matter what nuarr does to the disk, so holding work for it buys
#     precisely zero and costs the queue.
#   * SECONDS ARE NOT DISK WORK. 60s of a 25.2 Mbps 4K remux is 189 MB that
#     has to come off the spindle; 60s of a 5.9 Mbps episode is 44 MB. The
#     same number of seconds is four times the reading, so the same number of
#     seconds is not the same amount of safety.
#   * PLEX PARKS ITS OWN ENCODER at TranscoderThrottleBuffer seconds ahead and
#     then stops reading. With that set to 60 and nuarr's floor also at 60,
#     every healthy transcode sits exactly on the line by construction - the
#     buffer is not "low", it is where Plex deliberately holds it.
#
# So the floor is computed per session and published per disk.
_PLEX_FLOOR: dict[str, float] = {}

# The bitrate at which the configured floor is taken at face value. Streams
# above it need proportionally more banked, streams below it less.
FLOOR_REF_KBPS = 10_000.0
# ...but not without limit. Unclamped, a 1.2 Mbps SD stream would be handed a
# 7 second floor and a 60 Mbps remux a 6 minute one, and neither is a number
# anyone asked for.
FLOOR_SCALE_MIN, FLOOR_SCALE_MAX = 0.5, 2.0
# How much of Plex's own throttle buffer nuarr is willing to demand. Asking
# for more than Plex will ever bank is asking for something that cannot
# happen, so the floor for a transcode is capped well under it.
THROTTLE_FRACTION = 0.6
# ...and the same argument applies to the CLIENT, which is the harder limit.
#
# Scaling the floor up with bitrate is right about the disk - 60s of a 25.2
# Mbps remux is 189 MB where 60s of a 5.9 Mbps episode is 44 MB - but it is
# only half the story, and on its own it backfires. Client buffers are capped
# in BYTES, not seconds, so the higher the bitrate the FEWER seconds a player
# can hold. Asking a 4K stream for 120s when its player never banks more than
# 60 is asking for something that cannot happen: the stream would read as
# starving for its entire runtime and nuarr would sit paused on that spindle
# for nothing. So the demand is capped at a fraction of what this client has
# actually been observed to manage.
CLIENT_CAP_FRACTION = 0.7
# A stream within this many seconds of having the rest of the file is treated
# as having all of it - the last couple of seconds are measurement slop.
FULLY_BUFFERED_SLOP_S = 3.0

# How close to the end of the current file a viewer must be before nuarr starts
# yielding the spindle their NEXT episode sits on. Five minutes: long enough
# that the disk is quiet well before the jump, short enough that a feature-
# length film does not hold two extra spindles down for an hour.
NEXT_EPISODE_WINDOW_S = 300.0


def viewer_floor(disk: str) -> float | None:
    """Seconds of buffer the governing viewer on `disk` needs, or None."""
    if not disk:
        return None
    v = _PLEX_FLOOR.get(disk)
    return None if v is None else float(v)


def viewer_floors() -> dict:
    return dict(_PLEX_FLOOR)


# Spindles where every viewer present has already fetched the rest of their
# file. Nothing on them will read again, so nuarr may work at full speed even
# though the disk technically has an audience.
_PLEX_DONE: set[str] = set()


def viewer_done(disk: str) -> bool:
    return bool(disk) and disk in _PLEX_DONE


# Plex's own preferences, cached. /:/prefs is 185 settings and does not change
# minute to minute, so this is read rarely and reused.
_PREFS: dict = {"at": 0.0, "vals": {}}
_PREFS_TTL = 600.0


def plex_prefs() -> dict:
    """Plex server settings as {id: value}, cached for _PREFS_TTL seconds."""
    now = time.time()
    if _PREFS["vals"] and now - _PREFS["at"] < _PREFS_TTL:
        return _PREFS["vals"]
    url, token = SETTINGS.plex_url, SETTINGS.plex_token
    if not (url and token):
        return _PREFS["vals"]
    try:
        import json as _json
        import urllib.request as _u
        req = _u.Request(url.rstrip("/") + "/:/prefs?X-Plex-Token=" + token,
                         headers={"Accept": "application/json"})
        with _u.urlopen(req, timeout=6) as r:                 # noqa: S310
            doc = _json.load(r)
        vals = {s["id"]: s.get("value")
                for s in doc.get("MediaContainer", {}).get("Setting", [])
                if s.get("id")}
        if vals:
            _PREFS["vals"], _PREFS["at"] = vals, now
    except Exception:                                        # noqa: BLE001
        _PREFS["at"] = now          # do not retry in a tight loop on failure
    return _PREFS["vals"]


def throttle_buffer_s() -> float | None:
    """Plex's TranscoderThrottleBuffer, or None if it could not be read."""
    try:
        v = plex_prefs().get("TranscoderThrottleBuffer")
        return float(v) if v not in (None, "") else None
    except Exception:                                        # noqa: BLE001
        return None


def _client_cap_s(client: str, br_kbps: float, device: str = "") -> float:
    r"""How many seconds this client can hold AT THIS BITRATE. 0 = not known.

    Prefers the byte figure, because that is the quantity a player's buffer is
    actually limited by: the seconds it converts to depend entirely on what is
    being streamed, and a cap learned on a 6 Mbps episode says almost nothing
    about a 25 Mbps remux except by way of the megabytes. Falls back to the
    raw seconds only when no bitrate is available to convert with.
    """
    if br_kbps > 0:
        # The peak lead is the better estimate - it is the buffer level, not a
        # lower bound on it - but only once it is big enough to be a ceiling
        # rather than a snapshot of it still filling.
        pk = _CLIENT_PEAK_BYTES.get(device or client, 0.0)
        if pk >= PEAK_MIN_BYTES:
            return pk * 8.0 / (float(br_kbps) * 1000.0)
        by = _CLIENT_CAP_BYTES.get(client, 0.0)
        if by >= PEAK_MIN_BYTES:
            return by * 8.0 / (float(br_kbps) * 1000.0)
        return 0.0                       # not enough evidence to cap anything
    return _CLIENT_CAP.get(client, 0.0)


def session_done(s: dict) -> bool:
    r"""Has this stream already got the rest of the file?

    Split out of session_floor because a floor of 0 has two very different
    causes - "needs nothing, it is finished fetching" and "could not be
    measured" - and the pacing code has to tell them apart. The first deserves
    full speed; the second deserves caution.
    """
    try:
        lead = float(s.get("lead_s"))
    except (TypeError, ValueError):
        return False
    dur = _num(s.get("duration_ms"), 0.0) or _num(s.get("duration"), 0.0)
    off = _num(s.get("offset_ms"), 0.0) or _num(s.get("view_offset"), 0.0)
    dur, off = dur / 1000.0, off / 1000.0
    return bool(dur) and lead >= max(0.0, dur - off) - FULLY_BUFFERED_SLOP_S


def session_floor(s: dict, base: float) -> float:
    """The buffer this one session actually needs. 0 means "never hold for it"."""
    if base <= 0:
        return 0.0
    try:
        lead = float(s.get("lead_s"))
    except (TypeError, ValueError):
        return 0.0                       # unmeasured is not starving
    # TWO SPELLINGS FOR EVERY FIELD. These dicts come from Plex directly on
    # one path and from Tautulli on the other, and the panel re-keys them
    # again on the way to the browser, so each name has to be tried. Reading
    # only one spelling silently yielded a duration of zero (no session ever
    # counted as buffered to the end) and a decision of "" (no transcode ever
    # hit the Plex cap) - both failing quietly toward "protect everything".
    dur = _num(s.get("duration_ms"), 0.0) or _num(s.get("duration"), 0.0)
    off = _num(s.get("offset_ms"), 0.0) or _num(s.get("view_offset"), 0.0)
    dur, off = dur / 1000.0, off / 1000.0
    # BUFFERED TO THE END - nothing left to read, so nothing to protect.
    if dur and lead >= max(0.0, dur - off) - FULLY_BUFFERED_SLOP_S:
        return 0.0
    floor = float(base)
    br = _num((s.get("detail") or {}).get("src_bitrate"), 0.0)
    if br > 0:
        floor *= min(FLOOR_SCALE_MAX,
                     max(FLOOR_SCALE_MIN, br / FLOOR_REF_KBPS))
    # WHAT THIS PLAYER HAS ACTUALLY MANAGED. Learned only from real coasts
    # (a client playing >= COAST_MIN_S without fetching has demonstrably
    # banked that much), so an absent entry means "not yet known" and imposes
    # nothing rather than guessing low and disabling the protection.
    cap = _client_cap_s(_client_of(s), br, _device_of(s))
    if cap > 0:
        floor = max(MIN_FLOOR_S, min(floor, cap * CLIENT_CAP_FRACTION))
    # The Plex cap is the last word: it is a statement about what the server
    # will ever bank, not a preference, so scaling must not push back past it.
    dec = (s.get("transcode_decision") or s.get("decision") or "")
    if dec == "transcode":
        tb = throttle_buffer_s()
        if tb and tb > 0:
            floor = min(floor, tb * THROTTLE_FRACTION)
    # A FLOOR TALLER THAN THE REST OF THE FILE CAN NEVER BE MET. With 34
    # seconds of episode left, demanding 49 banked - and 74 to resume - is a
    # sentence, not a threshold: Erik's card read "0s ahead - Nuarr paused
    # until 72s" through the entire final minute, waiting for a buffer the
    # credits made impossible. The most a client can ever hold is what has
    # not been played yet, so the floor shrinks with the file and reaches
    # zero as the end arrives - which also makes the buffered-to-the-end
    # test above a limit this line approaches rather than a separate cliff.
    if dur:
        floor = min(floor, max(0.0, dur - off - FULLY_BUFFERED_SLOP_S))
    # Whole seconds. A floor of 35.3 is false precision - the measurement it is
    # compared against is worth a second or two at best - and it propagates
    # into the axis labels as 105.89999999999999.
    return float(round(floor))


def floor_reason(s: dict, base: float) -> dict:
    r"""The working behind session_floor, for the card to show.

    A floor that is neither the setting nor the obvious scaling of it needs to
    account for itself, or it reads as a bug.
    """
    out = {"base": base, "wanted": base, "client_cap": None,
           "throttle_cap": None, "final": session_floor(s, base)}
    if base <= 0:
        return out
    br = _num((s.get("detail") or {}).get("src_bitrate"), 0.0)
    if br > 0:
        out["wanted"] = round(base * min(FLOOR_SCALE_MAX,
                                         max(FLOOR_SCALE_MIN,
                                             br / FLOOR_REF_KBPS)))
    cap = _client_cap_s(_client_of(s), br, _device_of(s))
    if cap > 0:
        out["client_cap"] = round(cap * CLIENT_CAP_FRACTION)
        out["client_seen"] = round(cap)
        by = max(_CLIENT_PEAK_BYTES.get(_device_of(s), 0.0),
                 _CLIENT_CAP_BYTES.get(_client_of(s), 0.0))
        if by > 0:
            out["client_mb"] = round(by / 1048576.0)
    out["min_floor"] = MIN_FLOOR_S
    dec = (s.get("transcode_decision") or s.get("decision") or "")
    if dec == "transcode":
        tb = throttle_buffer_s()
        if tb and tb > 0:
            out["throttle_cap"] = round(tb * THROTTLE_FRACTION)
    return out


# WHERE THE VIEWER IS ABOUT TO GO.
#
# Yielding the disk queue the moment a viewer appears is reactive, and on an
# episode rollover reactive is too late: Plex opens the next file and starts
# filling its buffer immediately, while nuarr is still running a remux at
# normal priority on that spindle because nothing has told it yet. Best case
# the throttle lands a couple of seconds in; worst case the viewer sees it.
#
# A binge is the most predictable thing on this server, so predict it. The next
# episode of the show being watched is one indexed query away, and if it is on
# a different disk that disk can be demoted BEFORE the switch rather than after
# - reaction time zero, because there is nothing to react to.
#
# Deliberately only ONE episode ahead. Two is speculation, and every disk added
# here is a disk nuarr yields on for somebody who may well stop after this one.
_PLEX_NEXT_DISKS: set[str] = set()
_NEXT_CACHE: dict[str, str | None] = {}


def plex_next_disks() -> set[str]:
    return set(_PLEX_NEXT_DISKS)


def plex_disk_detail() -> dict:
    """Per pool disk: who is watching from it and how much they are pulling.

    WHY A COMBINED RATE AND NOT JUST A COUNT.
    #
    # "watching" told you a spindle was spoken for and nothing about how badly.
    # One person direct-playing a 4 Mbps episode and three people pulling a
    # combined 90 Mbps off the same disk are the same word, and they are not
    # remotely the same situation - the first leaves plenty of head room for a
    # remux at low priority, the second is already most of what the disk can
    # do and anything else on it will be felt.
    #
    # Rate comes from Plex's own Session.bandwidth (kbps it has reserved for
    # the client), falling back to the file's bitrate, which is what a direct
    # play actually reads. Paused sessions are counted but contribute NOTHING
    # to the rate - a paused stream holds its place on the disk without
    # reading from it, and adding its bitrate would inflate the figure with
    # traffic that is not happening.
    """
    out: dict[str, dict] = {}
    for s in _PLEX_LIVE:
        d = s.get("disk") or ""
        if not d:
            continue
        e = out.setdefault(d, {"viewers": 0, "paused": 0, "kbps": 0.0,
                               "who": []})
        playing = str(s.get("state") or "").lower() == "playing"
        e["viewers"] += 1
        if not playing:
            e["paused"] += 1
        rate = _num(s.get("bandwidth"), 0.0) or _num(
            (s.get("detail") or {}).get("src_bitrate"), 0.0)
        if playing:
            e["kbps"] += rate
        e["who"].append({
            "user": s.get("user") or "",
            "title": s.get("title") or "",
            "state": "playing" if playing else "paused",
            "kbps": round(rate),
            "local": str(s.get("location") or "") == "lan",
        })
    return out


def _next_disk_for(path: str) -> str | None:
    """The pool disk holding the episode AFTER this one, if there is one.

    Memoised per path: a viewer sits on one episode for 20-40 minutes and this
    is consulted every couple of seconds, so the answer is looked up once and
    then read from a dict.
    """
    if not path:
        return None
    if path in _NEXT_CACHE:
        return _NEXT_CACHE[path]
    res: str | None = None
    try:
        from .db import cursor
        with cursor() as cur:
            r = cur.execute(
                "SELECT arr_name, arr_parent_id, season, episode FROM files "
                "WHERE path=? LIMIT 1", (path,)).fetchone()
            # Movies have no next episode, and neither does an untracked file.
            if (r and r["arr_parent_id"] is not None
                    and r["episode"] is not None and r["season"] is not None):
                # Next episode in the same season, else the first of the next
                # season - the order Plex itself plays them in.
                # CAST THE EPISODE. It is stored as TEXT while season is an
                # INTEGER, so a plain comparison orders episodes as strings:
                # '10' sorts before '9', and after episode 9 the "next" one
                # would come back as 10 from the wrong end of the season -
                # or as nothing at all. Only visible on shows with more than
                # nine episodes, which is most of them.
                ep = int(str(r["episode"]).strip() or 0)
                nxt = cur.execute(
                    "SELECT pool_disk FROM files "
                    " WHERE arr_name=? AND arr_parent_id=? "
                    "   AND state!='deleted' AND pool_disk IS NOT NULL "
                    "   AND season IS NOT NULL AND episode IS NOT NULL "
                    "   AND (season > ? OR (season = ? "
                    "        AND CAST(episode AS INTEGER) > ?)) "
                    " ORDER BY season, CAST(episode AS INTEGER) LIMIT 1",
                    (r["arr_name"], r["arr_parent_id"],
                     r["season"], r["season"], ep)).fetchone()
                if nxt:
                    res = nxt["pool_disk"]
    except Exception:
        res = None
    if len(_NEXT_CACHE) > 400:
        _NEXT_CACHE.clear()
    _NEXT_CACHE[path] = res
    return res


# Is the balancer actually MOVING data, as opposed to placing new files? Read by
# the I/O-priority watcher: a real balance reads and writes across every pool
# disk at once, so unlike a viewer - who occupies exactly one spindle - there is
# no such thing as "somewhere else" to be.



def _disk_for(path: str) -> str | None:
    """Pool disk holding a Plex session's file, memoised per path.

    disk_of() is ~12 stat calls, cheap individually, but this is consulted on
    every gate refresh for every session - and a viewer watching one episode
    for 40 minutes would repeat the same lookup 120 times.
    """
    if not path:
        return None
    if path in _PLEX_DISK_CACHE:
        return _PLEX_DISK_CACHE[path]
    try:
        from . import scanner
        d = scanner.disk_of(path)
    except Exception:
        d = None
    if len(_PLEX_DISK_CACHE) > 200:
        _PLEX_DISK_CACHE.clear()
    _PLEX_DISK_CACHE[path] = d
    return d


def get_toggle(key: str) -> bool:
    v = kv_get(key)
    if v is None:
        v = DEFAULTS.get(key, "0")
    return str(v) == "1"


def set_toggle(key: str, on: bool) -> None:
    kv_set(key, "1" if on else "0")


def toggles() -> dict:
    return {k: get_toggle(k) for k in DEFAULTS}


# --------------------------------------------------------------- checks ----
def _cached(name: str, fn) -> Reason:
    """Cache a check result. Now runs on worker threads, so it must be locked.

    A per-name lock also collapses a thundering herd: the dispatcher and the
    dashboard can ask at the same moment, and without this both would make
    their own 10 s Tautulli call instead of one sharing the answer.
    """
    hit = _CACHE.get(name)
    now = time.time()
    ttl = _plex_ttl() if name == "plex" else _tune("gate_cache_s")
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _CACHE_LOCKS.setdefault(name, threading.Lock()):
        hit = _CACHE.get(name)              # another thread may have filled it
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        r = fn()
        _CACHE[name] = (time.time(), r)
        return r


def _plex_ttl() -> float:
    """The Plex check re-runs far more often than the others, on purpose.

    gate_cache_s exists to ration a ~2.4 s Tautulli call. Reading Plex directly
    costs a local round trip and the session list behind it is separately cached
    for 1.5 s, so re-deciding is nearly free - and the Plex row is the one row
    on the panel that a person actually watches change. Holding it for 20 s made
    it visibly lag the session cards immediately below it.

    Never LONGER than the configured value, so lowering gate_cache_s still
    lowers this.
    """
    conf = _tune("gate_cache_s")
    return min(conf, 2.0) if SETTINGS.plex_direct else conf


def _num(v, default: float = 0.0) -> float:
    """Tautulli returns numbers as strings about half the time."""
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


# A VERDICT WITH A TWO-MINUTE CONSEQUENCE CANNOT TURN ON ONE POLL.
#
# Measured live and it is the whole of the bug: throttled=1, speed=0.0, lead
# 225-242s, floor 30s - every input to the parked test rock steady across a
# minute of sampling - and yet the verdict flipped to "not parked" on 2 polls
# out of 14. Each flip re-stamps _LAST_BUSY, which restarts the hold_grace_s
# countdown from the top, so the banner sat there cycling 1.9 -> 1.5 -> 1.9 min
# remaining and the gate never actually opened. The reset is one sample cheap;
# the countdown is 120 seconds expensive. That asymmetry alone guarantees the
# clock can never reach zero, whatever the noisy input happens to be - a poll
# that lands on a cache boundary, a Plex response that omits maxOffsetAvailable,
# a field arriving late.
#
# So the verdict has to HOLD before it changes. Symmetric, because the failure
# is symmetric: parking too eagerly would release the GPU under a viewer who is
# about to want it back.
_PARK: dict[str, list] = {}
PARK_DWELL_S = 20.0


def _park_settled(key: str, seen: bool, now: float) -> bool:
    """`seen` debounced: the new value must persist PARK_DWELL_S to take."""
    st = _PARK.get(key)
    if st is None:
        _PARK[key] = [seen, 0.0, seen]
        return seen
    verdict, since, pend = st
    if seen == verdict:                  # agrees with the standing verdict
        st[1], st[2] = 0.0, seen
        return verdict
    if seen != pend or not since:        # a new disagreement: start its clock
        st[1], st[2] = now, seen
        return verdict
    if now - since >= PARK_DWELL_S:      # it has held long enough to be real
        st[0], st[1] = seen, 0.0
        return seen
    return verdict


def _is_parked(s: dict, lead_needed: float, base: float = 0.0) -> bool:
    """Is this transcode throttled AND far enough ahead to be safely ignored?

    All three have to hold:
      * Plex says it is throttled
      * its encoder really has stopped (speed ~0, not just slow)
      * it has buffered comfortably past where the viewer is

    The lead check is what keeps this safe. A session throttled with only a few
    percent in hand is about to resume and will want the GPU straight back, so
    releasing the queue for it would just cause contention a moment later.

    MEASURED IN SECONDS, BECAUSE THAT IS WHAT A BUFFER IS.
    #
    The lead test used to be PERCENTAGE POINTS OF THE FILE, and the setting's
    own comment gave the game away by explaining the value in minutes: "15
    points is roughly several minutes of playback on a typical episode". It is
    not a fixed anything - 15% of a 22-minute episode is 3.3 min and 15% of a
    three-hour film is 27 - so the guarantee it bought swung by a factor of
    eight depending on what was being watched, while the risk it protects
    against (a spindle busy for a few seconds) never changed at all.

    Caught live and it read as a plain contradiction on one screen: a session
    with 227s banked against its own 30s floor - 7.6x clear, the card calling it
    parked, the encoder at speed 0.0 - was still holding the whole encode queue,
    because 227s happened to be 5.1% of a 70-minute film and 5.1 < 15.

    So the same per-session floor that governs everything else governs this too,
    at the multiple where nuarr already stops throttling for a viewer entirely.
    The percentage survives only as the fallback for a session whose buffer
    could not be measured, where a crude number beats none.
    """
    if int(_num(s.get("transcode_throttled"))) != 1:
        return False
    # speed is transcode rate as a multiple of realtime; 0.0 means stopped.
    # Allow a hair above zero for a sample caught mid-wind-down.
    if _num(s.get("transcode_speed"), 99.0) > 0.05:
        return False
    floor = session_floor(s, base) if base > 0 else 0.0
    try:
        lead = float(s.get("lead_s"))
    except (TypeError, ValueError):
        lead = None
    if lead is not None and base > 0:
        # Buffered to the end - floor 0 - is the most parked a stream can be.
        if floor <= 0:
            return True
        try:
            from . import jobs as _j
            mult = float(_j.VIEWER_FULL_SPEED_MULT)
        except Exception:                                    # noqa: BLE001
            mult = 3.0
        return lead >= floor * mult
    ahead = _num(s.get("transcode_progress")) - _num(s.get("progress_percent"))
    return ahead >= lead_needed


def _plex_sessions_direct() -> list[dict] | None:
    r"""Ask Plex itself, shaped exactly like Tautulli's get_activity.

    THE POINT OF THE SHAPE. check_plex() below is carefully tuned - the parked
    transcode rule, the paused handling, the per-spindle disk set - and none of
    that should have to change to swap where the data comes from. So this
    returns Tautulli's field names, and everything downstream is untouched.

    The mapping, field by field:

        Tautulli                 Plex /status/sessions
        friendly_name       <-   User.title
        state               <-   Player.state
        transcode_decision  <-   TranscodeSession.videoDecision, or the
                                 absence of a TranscodeSession entirely
        transcode_throttled <-   TranscodeSession.throttled   (bool -> 0/1)
        transcode_speed     <-   TranscodeSession.speed
        transcode_progress  <-   TranscodeSession.progress
        progress_percent    <-   viewOffset / duration * 100  (computed)
        file                <-   Media[0].Part[0].file

    Returns None - not [] - when Plex cannot be reached, because "no sessions"
    and "no answer" must not be confused: the first means nobody is watching,
    the second means we do not know, and only one of those is safe to act on.
    """
    url, token = SETTINGS.plex_url, SETTINGS.plex_token
    if not url or not token:
        return None
    try:
        r = httpx.get(f"{url.rstrip('/')}/status/sessions",
                      headers={"X-Plex-Token": token,
                               "Accept": "application/json"}, timeout=10)
        r.raise_for_status()
        body = r.json()
        mc = body.get("MediaContainer") if isinstance(body, dict) else None
        items = (mc or {}).get("Metadata") or []
    except Exception:
        return None

    out: list[dict] = []
    for s in items:
        if not isinstance(s, dict):
            continue
        ts = s.get("TranscodeSession") or {}
        player = s.get("Player") or {}
        user = s.get("User") or {}
        # THE SOURCE PATH IS NOT IN A TRANSCODING SESSION.
        #
        # For a direct play, Media[0].Part[0].file is the file on disk. For a
        # transcode, Plex returns the transcode TARGET instead - decision
        # 'transcode', and no `file` key at all. Verified on two live
        # transcodes: both had exactly one Media, one Part, and file absent.
        #
        # That matters more than it looks. _PLEX_DISKS is built from these
        # paths and is what keeps jobs off the spindle a viewer is reading
        # from - so an empty path during a transcode removes the protection
        # precisely when the disk is busiest. Tautulli fills it in because it
        # resolves the item separately, and so must we.
        path = ""
        for m in (s.get("Media") or []):
            for p in (m.get("Part") or []):
                if p.get("file"):
                    path = p["file"]
                    break
            if path:
                break
        if not path and s.get("ratingKey"):
            path = _plex_file_for(str(s["ratingKey"]))

        dur = _num(s.get("duration"))
        off = _num(s.get("viewOffset"))
        pct = (off / dur * 100.0) if dur > 0 else 0.0

        # THE SELECTED STREAMS, for the card's expanded view. Plex composes
        # displayTitle strings that already read well ("1080p (HEVC Main 10)",
        # "English (EAC3 5.1)") - carrying those beats rebuilding them from
        # codec fields. `selected` marks what this viewer is actually using;
        # the video stream has no selected flag worth trusting, so first wins.
        vstream = astream = sstream = None
        for m in (s.get("Media") or []):
            for p in (m.get("Part") or []):
                for st in (p.get("Stream") or []):
                    t = st.get("streamType")
                    if t == 1 and vstream is None:
                        vstream = st
                    elif t == 2 and (st.get("selected") or astream is None):
                        astream = st
                    elif t == 3 and st.get("selected"):
                        sstream = st
        media0 = (s.get("Media") or [{}])[0] or {}

        # ANY STREAM TRANSCODING MAKES THIS A TRANSCODE, which is what Tautulli
        # reports and therefore what the gate has always been tuned against.
        #
        # The first version of this looked only at videoDecision, on the
        # reasoning that an audio-only transcode leaves the GPU alone. That
        # reasoning is not wrong, but applying it here was: it silently changed
        # what the gate does, inside a change that was only supposed to move
        # where the data comes from. Caught on a live session - Lioness,
        # videoDecision='copy' with audioDecision='transcode' - which Tautulli
        # called a transcode and held the queue for, and which the new path
        # called a direct play and let straight through.
        #
        # If holding encode jobs for an audio-only transcode is wrong, that is
        # a real question worth answering on its own. It is not something to
        # change as a side effect of swapping a data source.
        if not ts:
            decision = "direct play"
        elif any(str(ts.get(k) or "").lower() == "transcode"
                 for k in ("videoDecision", "audioDecision",
                           "subtitleDecision")):
            decision = "transcode"
        else:
            decision = "copy"          # direct stream: remuxed, not re-encoded

        out.append({
            "session_key": str(s.get("sessionKey") or ""),
            "friendly_name": user.get("title") or "",
            "user": user.get("title") or "",
            "state": str(player.get("state") or "playing").lower(),
            "transcode_decision": decision,
            # Plex sends a JSON bool; Tautulli sends "0"/"1" and _num() reads
            # either, but normalise so the two sources compare cleanly.
            "transcode_throttled": 1 if ts.get("throttled") in (True, 1, "1") else 0,
            "transcode_speed": ts.get("speed", ""),
            "transcode_progress": ts.get("progress", 0),
            "progress_percent": round(pct, 1),
            "file": path,
            # ---- for the panel only ------------------------------------
            # Nothing below this line is read by check_plex(). It is carried
            # so the dashboard can show WHO is watching WHAT rather than a
            # bare count, and it is kept separate from the fields the gate
            # reasons about so that adding to it can never change a decision.
            "title": s.get("title") or "",
            "show": s.get("grandparentTitle") or "",
            "season": s.get("parentIndex"),
            "episode": s.get("index"),
            "year": s.get("year"),
            "kind": s.get("type") or "",
            # Raw position and length, so the browser can run its own clock
            # between polls instead of stepping the bar once per refresh.
            "duration_ms": int(dur),
            "offset_ms": int(off),
            # HOW MUCH ENCODED VIDEO IS SITTING AHEAD OF THE VIEWER, in seconds.
            #
            # This is the number that predicts a stutter. Plex encodes ahead and
            # parks; if the lead is shrinking towards zero while the stream is
            # playing, the encoder is losing the race and the viewer is about to
            # buffer. By the time Player.state says "buffering" it has already
            # happened. maxOffsetAvailable is in seconds; viewOffset is in ms.
            "lead_s": (round(_num(ts.get("maxOffsetAvailable")) - off / 1000.0, 1)
                       if ts.get("maxOffsetAvailable") is not None else None),
            "speed": _num(ts.get("speed")) if ts else None,
            "throttled": 1 if ts.get("throttled") in (True, 1, "1") else 0,
            # grandparentThumb is the SHOW poster; thumb on an episode is the
            # episode still, which is a worse thing to look at in a list.
            "thumb": (s.get("grandparentThumb") or s.get("thumb")
                      or s.get("parentThumb") or ""),
            "player": player.get("title") or "",
            "product": player.get("product") or "",
            # matches statistics/bandwidth accountID - the client-buffer
            # estimator's join key
            "account_id": str(user.get("id") or ""),
            "location": str((s.get("Session") or {}).get("location") or "").lower(),
            "bandwidth": (s.get("Session") or {}).get("bandwidth"),
            "video": f"{ts.get('sourceVideoCodec') or ''}"
                     f"{' -> ' + str(ts.get('videoCodec')) if ts.get('videoCodec') else ''}",
            "audio": f"{ts.get('sourceAudioCodec') or ''}"
                     f"{' -> ' + str(ts.get('audioCodec')) if ts.get('audioCodec') else ''}",
            # Everything the expanded card shows, in one bag so adding to it
            # can never collide with a field the gate reasons about.
            "detail": {
                "video_decision": str(ts.get("videoDecision") or "").lower(),
                "audio_decision": str(ts.get("audioDecision") or "").lower(),
                "sub_decision": str(ts.get("subtitleDecision") or "").lower(),
                "src_container": media0.get("container") or "",
                "dst_container": ts.get("container") or "",
                "src_res": str(media0.get("videoResolution") or ""),
                "src_bitrate": media0.get("bitrate"),
                "video_title": (vstream or {}).get("displayTitle") or "",
                "audio_title": (astream or {}).get("displayTitle") or "",
                "audio_ch": (astream or {}).get("channels"),
                "sub_title": (sstream or {}).get("displayTitle") or "",
                "sub_burn": bool((sstream or {}).get("burn"))
                            or str(ts.get("subtitleDecision") or "") in
                               ("burn", "transcode"),
                "hw": bool(ts.get("transcodeHwRequested")),
                "hw_full": bool(ts.get("transcodeHwFullPipeline")),
                # how much of the FILE the encoder has finished, distinct from
                # how much the viewer has watched
                "enc_progress": _num(ts.get("progress")),
            },
        })
    _estimate_client_leads(out)
    return out


# ------------------------------------------- client-side buffer estimate ----
# HOW FAR AHEAD HAS THE CLIENT FETCHED, for streams Plex tells us nothing
# about. A transcode reports maxOffsetAvailable - the encoder's lead - but a
# direct play is just an HTTP download of the file, and Plex exposes no "the
# client has byte N" anywhere. What it DOES expose is /statistics/bandwidth:
# real measured bytes delivered per account in ~6 s buckets (verified live:
# accountID matches Session.User.id, and the byte counts track the stream).
#
# So the estimate is an integral. From the moment a session is first observed:
#
#     delivered_s = bytes_sent * 8 / bitrate        (file kbps, from Media[0])
#     played_s    = viewOffset_now - viewOffset_then
#     buffered    = delivered_s - played_s           (clamped to [0, remaining])
#
# A client pulling a 9.4 Mbps file at 19 Mbps is stacking up ~1 s of video per
# second watched; one that has gone quiet is draining what it stacked. This is
# a LOWER BOUND, not a measurement: whatever the client buffered before we
# started watching the counters is invisible, so the figure starts at zero and
# earns its way up. The UI labels it as an estimate for exactly that reason.
#
# Honest limits, so this never lies with confidence:
#   * two sessions on one account are indistinguishable in the statistics -
#     no estimate rather than a wrong one;
#   * a seek voids the integral (clients flush and refetch) - detected as an
#     offset jump and the observation restarts;
#   * account traffic that is not this stream (posters, metadata) pollutes the
#     numerator by a few KB against MBs of video - accepted.
_XFER: dict[str, dict] = {}
_BW_CACHE: dict = {"at": 0.0, "rows": []}

# THE INTEGRAL MEASURES GROWTH, NOT LEVEL - so the anchor decides everything.
#
# delivered_s - played_s is the amount the client has fetched BEYOND what it
# played since we started counting. That equals the buffer only if the buffer
# was empty when counting began. Join a session already in flight and the
# answer is a floor that never catches up: a client sitting on a full buffer
# fetches at exactly 1x playback, so growth is zero and the estimate freezes.
# Measured live: a paused Skeleton Knight session reported lead=24.0s, frozen,
# while the client actually held 199s.
#
# A session key that appears for the first time IS a playback that just began,
# and playback begins with an empty buffer - including a resume, which starts
# empty at a saved offset. The one exception is nuarr restarting mid-stream, so
# whatever is present on the very first poll is explicitly untrusted.
_FIRST_POLL_DONE = False

# Seconds without a single delivered byte before the client is deemed to have
# stopped fetching. A paused client that has stopped fetching is FULL, which is
# the one moment its buffer level is knowable without an anchor.
SATURATED_AFTER_S = 25.0

# A coast has to be long enough to be a buffer rather than a gap between two
# range requests. Below this it says nothing about capacity.
COAST_MIN_S = 20.0

# A COAST THAT BARELY CLEARS THE THRESHOLD MEASURES THE THRESHOLD, NOT THE
# CLIENT. Caught live and it had poisoned everything downstream: every single
# capacity this had ever learned was 0.3 min - Plex for Windows 20s, Plex for
# Android (TV) 20s, hours apart, at 11.8 and 24.7 Mbps. Not a coincidence and
# not a real property of two different players; it is COAST_MIN_S being read
# back out of its own filter. Plex clients do not fill and then fall silent for
# minutes - they trickle, topping up with small range requests continuously - so
# nearly every coast ends a hair past the cutoff. Accepting those made the
# learned figure a restatement of the cutoff, and because the ratchet only
# raises on a LONGER coast, 20s then stuck forever.
#
# Requiring real headroom over the cutoff means a coast has to be long enough
# that its length carries information the cutoff did not already supply.
COAST_TEACH_MIN_S = COAST_MIN_S * 2.0

# What each kind of client turned out to buffer, learned from anchored
# sessions: product/device -> seconds. Lets an untrusted mid-stream join still
# report something true once the same client has been measured properly once.
_CLIENT_CAP: dict[str, float] = {}
# The same observation in bytes, which is the form that survives a change of
# bitrate. See the learn site for why seconds alone were not enough.
_CLIENT_CAP_BYTES: dict[str, float] = {}

# THE HIGH-WATER MARK OF THE BUFFER ITSELF, per client, in bytes.
#
# A coast is a LOWER bound - "played this far without fetching" proves the
# client held at least that much and says nothing about the ceiling - so using
# one to cap nuarr's demand under-reads the client badly. Straight after a
# restart the only coast on record was 20s of a 5.9 Mbps episode, i.e. 14 MB,
# which set a 4K film's floor to 3 seconds: not protection, just a number.
# The peak LEAD actually observed is the right estimate of capacity, because
# it is the buffer level itself rather than a floor on it.
_CLIENT_PEAK_BYTES: dict[str, float] = {}

# WHAT WAS LEARNED SURVIVES A RESTART, because a restart is when it is needed
# most. Everything above is measured patiently - a coast takes minutes to
# prove itself - and it all lived in memory, so every nuarr restart (and this
# machine restarts on every update) threw the education away. The next poll
# then found sessions "already running when nuarr started": unanchored, no
# capacity on record, so the card fell back to "measuring client buffer" and
# the full scaled floor - and the queue got paused on behalf of a client that
# had demonstrated three minutes of headroom an hour earlier. Erik's laptop
# hit exactly this: a healthy direct play read "0s ahead - Nuarr paused"
# because the restart had erased everything the client had ever proven.
#
# Written through the kv table on every learn event - they are rare, a few per
# session at most - and read back once at import. The maps stay authoritative
# in memory; the kv copy is only the memory of them.
def _caps_save() -> None:
    try:
        from .db import kv_set
        kv_set("plex_client_caps", json.dumps({
            "cap": _CLIENT_CAP, "cap_bytes": _CLIENT_CAP_BYTES,
            "peak_bytes": _CLIENT_PEAK_BYTES}))
    except Exception:                                        # noqa: BLE001
        pass


def _caps_load() -> None:
    try:
        from .db import kv_get
        d = json.loads(kv_get("plex_client_caps") or "{}")
        for src, dst in (("cap", _CLIENT_CAP),
                         ("cap_bytes", _CLIENT_CAP_BYTES),
                         ("peak_bytes", _CLIENT_PEAK_BYTES)):
            for k, v in (d.get(src) or {}).items():
                try:
                    dst[k] = max(dst.get(k, 0.0), float(v))
                except (TypeError, ValueError):
                    continue
        if _CLIENT_CAP or _CLIENT_PEAK_BYTES:
            joblog.log(f"plex: remembered buffer capacities for "
                       f"{len(set(_CLIENT_CAP) | set(_CLIENT_PEAK_BYTES))} "
                       f"client(s) from before the restart", "debug")
    except Exception:                                        # noqa: BLE001
        pass


# The last peak per client that reached the kv copy, so a 2% wobble does not
# cost a write per poll. Not persisted itself - it is bookkeeping about
# persistence.
_PEAK_SAVED: dict[str, float] = {}

_caps_load()

# Below this the peak is still filling and says nothing about the ceiling.
PEAK_MIN_BYTES = 32 * 1024 * 1024
# And no cap may reduce the floor below this, whatever it thinks it has seen.
# A floor of 3s is indistinguishable from the feature being switched off.
MIN_FLOOR_S = 20.0


def _bw_stats() -> list[dict]:
    now = time.time()
    if now - _BW_CACHE["at"] < 5:
        return _BW_CACHE["rows"]
    url, token = SETTINGS.plex_url, SETTINGS.plex_token
    try:
        r = httpx.get(f"{url.rstrip('/')}/statistics/bandwidth",
                      params={"timespan": 6},
                      headers={"X-Plex-Token": token,
                               "Accept": "application/json"}, timeout=6)
        r.raise_for_status()
        rows = ((r.json().get("MediaContainer") or {})
                .get("StatisticsBandwidth") or [])
        _BW_CACHE.update(at=now, rows=rows)
    except Exception:
        _BW_CACHE["at"] = now          # do not hammer a failing endpoint
    return _BW_CACHE["rows"]


def _client_of(s: dict) -> str:
    return (s.get("product") or s.get("player") or "client").strip()


def _device_of(s: dict) -> str:
    r"""Product AND machine, for facts that are about the hardware.

    _client_of deliberately generalises across devices - "Plex for Windows"
    behaves like "Plex for Windows" - which is right for learning how a player
    fetches. It is wrong for how much it can HOLD: a 4K living-room box and a
    laptop are both "Plex for Windows" and have nothing in common memory-wise,
    and pooling them hands one device's budget to the other.
    """
    return (f"{(s.get('product') or '').strip()}|"
            f"{(s.get('player') or '').strip()}") or "client"


# ---------------------------------------------- disk-read high-water mark ---
# THE PRIMARY SOURCE for a direct play's buffer: the server's own file
# position. Plex reads the file to serve the client's range requests, so its
# handle offset IS the furthest byte served - the exact "buffered to" line the
# player draws. Verified live against a paused client showing 14:54 buffered:
# the handle read 15.15 min (1.4% VBR interpolation error) where the byte
# integral said 30 s. Everything below this block is the fallback for when no
# handle is visible (PMS opens some streams per-request, or nuarr lacks the
# privilege to look).
_HW: dict[str, dict] = {}          # session_key -> {bytes, size, last_off}
_HP_CACHE: dict = {"at": 0.0, "res": {}}
_HP_TTL = 2.0


def _disk_offsets(paths: dict[str, int]) -> dict[str, int]:
    now = time.time()
    if now - _HP_CACHE["at"] < _HP_TTL:
        return _HP_CACHE["res"]
    try:
        from . import handlepeek
        res = handlepeek.read_offsets(paths)
    except Exception:
        res = {}
    _HP_CACHE.update(at=now, res=res)
    return res


def _estimate_client_leads(sessions: list[dict]) -> None:
    global _FIRST_POLL_DONE
    live_keys = set()
    per_acct: dict[str, int] = {}
    for s in sessions:
        a = s.get("account_id") or ""
        if a:
            per_acct[a] = per_acct.get(a, 0) + 1
    stats = None
    now = time.time()
    first_poll = not _FIRST_POLL_DONE
    _FIRST_POLL_DONE = True

    # One handle sweep covers every candidate file this refresh.
    want: dict[str, str] = {}
    for s in sessions:
        if s.get("lead_s") is None and s.get("session_key") and s.get("file"):
            want[(s["file"] or "").lower()] = s["session_key"]
    offs = _disk_offsets({p: 0 for p in want}) if want else {}

    for s in sessions:
        k = s.get("session_key") or ""
        live_keys.add(k)
        if s.get("lead_s") is not None:
            continue                    # the encoder's real lead beats any estimate

        # ---- the server's file position for this stream --------------------
        # A strong source but NOT an unimpeachable one: PMS opens the same
        # file with other handles too - credits detection, thumbnails,
        # loudness analysis - and those sweep the whole file in seconds.
        # Caught live: a fresh episode read "9.8 min buffered" because an
        # analysis pass had parked a handle at the end of the file while the
        # client actually held 2 minutes. So the value is STASHED here and
        # cross-checked against the byte integral below before it is believed.
        hw_lead = None
        f = (s.get("file") or "").lower()
        dur_s = (s.get("duration_ms") or 0) / 1000.0
        off_s = (s.get("offset_ms") or 0) / 1000.0
        if f and dur_s:
            hw = _HW.setdefault(k, {"bytes": 0, "size": 0, "last_off": off_s})
            if not hw["size"]:
                try:
                    hw["size"] = os.path.getsize(s["file"])
                except OSError:
                    hw["size"] = 0
            # A seek backwards flushes the client's buffer and the refetch
            # starts near the new position - a high-water from before the
            # seek would report the old buffer as still standing.
            if off_s < hw["last_off"] - 8:
                hw["bytes"] = 0
            hw["last_off"] = off_s
            got = offs.get(f)
            if got and got > hw["bytes"]:
                hw["bytes"] = got
            if hw["bytes"] and hw["size"]:
                to_s = hw["bytes"] / hw["size"] * dur_s
                # A HANDLE BEHIND THE PLAYHEAD IS NO EVIDENCE, NOT ZERO BUFFER.
                #
                # This used to read max(to_s, off_s) and then subtract off_s,
                # so any handle at or behind the playback position collapsed to
                # exactly 0.0 - and since the disk figure WINS OUTRIGHT further
                # down, that zero then overwrote a perfectly good byte-integral
                # reading. Live symptom: a healthy 11.8 Mbps LAN direct play,
                # 62% through, reported "0s buffered (measured)" with
                # lead_disk=1 and was paused against a 71s floor.
                #
                # Behind the playhead is exactly what a direct play looks like:
                # the client pulls ranges and Plex's handle is left wherever the
                # last one ended, or is closed and reopened, or gets rewound -
                # and a backward seek zeroes the high-water here by design. None
                # of that says the client is empty; it says this instrument is
                # not tracking this client. So the reading is DISCARDED and the
                # integral keeps its answer.
                if to_s > off_s:
                    hw_lead = to_s - off_s
        acct = s.get("account_id") or ""
        br = (s.get("detail") or {}).get("src_bitrate")
        if not (k and acct and br) or per_acct.get(acct) != 1:
            # The integral cannot run here, so the disk figure has nothing to
            # be checked against - better an unverified measurement than none.
            if hw_lead is not None:
                lead = min(hw_lead, max(0.0, dur_s - off_s)) if dur_s else hw_lead
                s["lead_s"], s["lead_est"] = round(lead, 1), 1
                s["lead_disk"] = 1
            continue
        if stats is None:
            stats = _bw_stats()
        mine = [b for b in stats if str(b.get("accountID")) == acct]
        off_s = (s.get("offset_ms") or 0) / 1000.0
        st = _XFER.get(k)
        # A seek voids the integral - and RE-ANCHORS it, because a client
        # flushes and refetches from the new position, so the buffer really is
        # empty at that instant. Forward tolerance is generous: a background
        # tab or a slow poll makes played time legitimately jump.
        seek = st is not None and (off_s < st["last_off"] - 8
                                   or off_s > st["last_off"] + 300)
        if st is None or seek:
            _XFER[k] = {
                "off0": off_s, "bytes": 0.0, "last_off": off_s,
                "last_at": max((b.get("at") or 0 for b in mine),
                               default=int(now)),
                # A seek is always a clean anchor. A newly-seen session is one
                # too, UNLESS it was already running when nuarr started.
                "anchored": bool(seek or not first_poll),
                "idle_since": 0.0, "peak": 0.0,
                # Seconds of buffer PROVEN to have existed by watching the
                # client play without fetching - see the coast rule below.
                "coast_off": 0.0,
                # The lowest the growth integral has ever been - the reference
                # point for the tightest growth-only bound; see below.
                "min_raw": 0.0,
            }
            s["lead_s"], s["lead_est"] = 0.0, 1
            s["lead_anchored"] = 1 if _XFER[k]["anchored"] else 0
            continue
        new = [b for b in mine if (b.get("at") or 0) > st["last_at"]]
        if new:
            st["bytes"] += sum(b.get("bytes") or 0 for b in new)
            st["last_at"] = max(b["at"] for b in new)
            st["idle_since"] = 0.0
        elif not st["idle_since"]:
            st["idle_since"] = now
        st["last_off"] = off_s
        delivered_s = st["bytes"] * 8.0 / 1000.0 / float(br)
        played_s = off_s - st["off0"]
        dur_s = (s.get("duration_ms") or 0) / 1000.0
        raw = delivered_s - played_s

        # PLAYING WITHOUT FETCHING MEASURES THE BUFFER - AT THE END, NOT DURING.
        #
        # This is the measurement a resume hands us. A client coming off a
        # pause plays out of its buffer and fetches nothing until it needs a
        # top-up, so every second it plays without receiving a byte is a second
        # it must already have held. The tempting move is to treat the coast so
        # far as a floor on the CURRENT buffer, and that is wrong: the coast
        # proves what the buffer HELD, while the thing being displayed is what
        # is LEFT, and those move in opposite directions. Tried it - it reported
        # zero through a 199-second coast, which is a true lower bound and
        # useless.
        #
        # What the coast genuinely yields is the client's CAPACITY, readable at
        # the moment the coast ends: it played `coasted` seconds and only then
        # needed more, so it was holding about that much when it started. Learn
        # that, and every later session on the same client - including one
        # joined mid-stream, which can never measure itself forward - can be
        # reported properly as capacity minus what has been played out of it.
        client = _client_of(s)
        playing = str(s.get("state") or "").lower() == "playing"
        coasted = 0.0
        if playing and not new:
            if not st["coast_off"]:
                st["coast_off"] = off_s
            coasted = off_s - st["coast_off"]
        elif st["coast_off"]:
            # The coast just ended - the client asked for more data, so it had
            # run its buffer down to its refill mark. Take the length of that
            # coast as this client's measured fill.
            ran = st["last_off"] - st["coast_off"]
            if ran >= COAST_TEACH_MIN_S and ran > _CLIENT_CAP.get(client, 0.0):
                _CLIENT_CAP[client] = ran
                _caps_save()
                joblog.log(f"plex: learned {client} buffers about "
                           f"{ran/60:.1f} min ahead (played that far without "
                           f"fetching)", "debug")
            # ...AND IN BYTES, which is the number that actually transfers.
            #
            # A player's buffer is a memory budget, so what it holds is a fixed
            # number of MEGABYTES and a wildly varying number of seconds: the
            # same client that coasts 90s on a 6 Mbps episode manages barely 20
            # on a 25 Mbps remux. Storing only the seconds meant the figure
            # learned from a cheap stream was applied unchanged to an expensive
            # one, which is how a 4K film ends up with a floor it can never
            # reach. Recorded at the bitrate it was measured at, so it can be
            # converted back at whatever the next stream costs.
            if ran >= COAST_TEACH_MIN_S and br > 0:
                by = ran * float(br) * 1000.0 / 8.0
                if by > _CLIENT_CAP_BYTES.get(client, 0.0):
                    _CLIENT_CAP_BYTES[client] = by
                    _caps_save()
                    joblog.log(f"plex: {client} held about {by/1048576:.0f} MB "
                               f"({ran:.0f}s at {float(br)/1000:.1f} Mbps)",
                               "debug")
            st["coast_off"] = 0.0

        cap = _CLIENT_CAP.get(client, 0.0)
        # THE TIGHTEST GROWTH-ONLY BOUND, and the fix for a floor that SHRANK.
        #
        # raw = delivered - played since observation began, and on a session
        # joined mid-stream that number falls one second per second while the
        # client coasts on a buffer it filled before we were watching - so the
        # card read "22s fetched ahead" and DECREASED against a client sitting
        # on three minutes. A lower bound that weakens as you watch it is worse
        # than none.
        #
        # The bound worth reporting: the buffer was >= 0 at EVERY past moment,
        # not just at the start. So for any past time t, buffer(now) >=
        # delivered-played since t, and the best choice of t is wherever that
        # difference bottomed out: buffer(now) >= raw - min(raw ever seen).
        # During a coast, raw and its minimum fall together and the bound
        # holds still instead of eroding; every refill burst lifts it. It
        # converges upward toward the truth instead of drifting away from it.
        #
        # Anchored sessions keep plain raw: their integral started at a known
        # empty, so raw IS the buffer and needs no reference shift.
        st["min_raw"] = min(st.get("min_raw", 0.0), raw)
        if st["anchored"]:
            lead = max(0.0, raw)
        else:
            lead = max(0.0, raw - st["min_raw"])
        if coasted and cap:
            # Mid-coast with a known capacity: what is left is what it started
            # with, minus what it has played out. This is the case that fixes a
            # session nuarr joined late.
            #
            # A COAST THAT OUTLASTS THE CAPACITY DISPROVES THE CAPACITY - it
            # does not empty the buffer. Read naively this subtraction goes
            # negative and clamps to zero, and the card then says "0s buffered"
            # about a client that is at that very moment playing smoothly
            # without asking for a single byte. That is self-contradictory:
            # playing without fetching is only possible OUT OF a buffer, so an
            # ongoing coast is positive proof the buffer is not empty. Live
            # symptom: a healthy 11.8 Mbps LAN direct play sat at 0s and was
            # paused against a 71s floor it could never satisfy.
            #
            # So the longer coast wins and re-teaches the client, immediately
            # rather than at the end - waiting for the coast to finish leaves
            # the number wrong for exactly as long as the evidence is strongest.
            # Re-teach first, then decide. Note the subtraction is only
            # meaningful while the coast is still INSIDE the known capacity;
            # once it is outside, cap - coasted is not a small number, it is a
            # wrong one, and the growth integral computed above - which never
            # claims more than it can prove - is the better answer.
            if coasted > cap:
                cap = _CLIENT_CAP[client] = coasted
                if br > 0:
                    _CLIENT_CAP_BYTES[client] = max(
                        _CLIENT_CAP_BYTES.get(client, 0.0),
                        coasted * float(br) * 1000.0 / 8.0)
                _caps_save()
                s["lead_outlasted"] = 1
            else:
                lead = max(0.0, cap - coasted)
                s["lead_proven"] = 1
        st["peak"] = max(st["peak"], lead)

        # THE CLIENT HAS STOPPED FETCHING, so its buffer is as full as it
        # intends to get. This is the one moment a level is knowable without
        # an anchor - and if the anchor IS trustworthy, it is also the moment
        # this client's capacity becomes a measured fact worth remembering.
        idle = st["idle_since"] and (now - st["idle_since"]) >= SATURATED_AFTER_S
        # CAPACITY IS LEARNED FROM TIME, NEVER FROM BYTES. This used to also
        # learn from the byte integral when an anchored session went idle -
        # and the byte integral OVERSHOOTS on fresh starts, because players
        # probe with overlapping range requests and re-fetches that all count
        # as delivered bytes without being distinct video. Measured on a fresh
        # episode: 5.0 min reported against ~2 min actually buffered. A coast
        # is measured in seconds of playback and cannot be inflated that way,
        # so it is the only teacher - and once known, it CAPS the byte-based
        # figure, since a client never holds more than it is willing to...
        #
        # ...but only when the capacity is itself worth believing. A cap
        # learned from a barely-
        # qualifying coast is a restatement of COAST_MIN_S (see
        # COAST_TEACH_MIN_S), and clamping every reading to it made the buffer
        # unable to exceed 20s while the floors it was compared against were 71s
        # and 120s. That is not a tight measurement, it is a guaranteed
        # starvation verdict: the pause could never clear, because the number
        # being tested was pinned below the threshold by construction. A ceiling
        # has to come from evidence stronger than the thing it overrules.
        if cap >= COAST_TEACH_MIN_S:
            lead = min(lead, cap)
        if not st["anchored"] and not s.get("lead_proven"):
            # Nothing measured forward here can be better than a floor - but a
            # capacity measured on THIS client (from an anchored fill, or from
            # a coast) is a real number, and beats a floor we know is wrong.
            if idle and cap > lead:
                lead = cap
                s["lead_learned"] = 1
            else:
                s["lead_floor"] = 1
        # ---- reconcile the two sources -------------------------------------
        # The disk position is precise about WHERE reading reached but blind
        # to WHO read (analysis handles sweep the file); the byte integral
        # knows exactly how much crossed the wire to the client but not where
        # it lies in the file. Physics joins them: the client cannot hold more
        # video than was delivered, so on an anchored session the integral
        # (plus grace for probe overlap) is a hard ceiling on the disk figure.
        # A learned client capacity caps it the same way. A disk value inside
        # those bounds is the best number we have and wins outright.
        if hw_lead is not None:
            ceil = None
            if st["anchored"]:
                ceil = max(0.0, raw) + 30.0
            if cap >= COAST_TEACH_MIN_S:      # same believability test as above
                c2 = cap + 30.0
                ceil = min(ceil, c2) if ceil is not None else c2
            if ceil is not None:
                hw_lead = min(hw_lead, ceil)
            lead = hw_lead
            for t in ("lead_floor", "lead_proven", "lead_learned"):
                s.pop(t, None)
            s["lead_disk"] = 1
        if dur_s:
            lead = min(lead, max(0.0, dur_s - off_s))
        s["lead_s"], s["lead_est"] = round(lead, 1), 1
        s["lead_anchored"] = 1 if st["anchored"] else 0
        s["lead_full"] = 1 if idle else 0
    for k in list(_XFER):
        if k not in live_keys:
            del _XFER[k]
    for k in list(_HW):
        if k not in live_keys:
            del _HW[k]


# ratingKey -> file path. A transcoding session does not carry its source path,
# so it has to be looked up - and the answer cannot change for a given key, so
# it is worth remembering. Bounded because a long-running server would
# otherwise accumulate one entry per item ever streamed.
_FILE_BY_KEY: dict[str, str] = {}


def _plex_file_for(rating_key: str) -> str:
    """The file on disk behind a Plex item, via /library/metadata."""
    if rating_key in _FILE_BY_KEY:
        return _FILE_BY_KEY[rating_key]
    url, token = SETTINGS.plex_url, SETTINGS.plex_token
    path = ""
    try:
        r = httpx.get(f"{url.rstrip('/')}/library/metadata/{rating_key}",
                      headers={"X-Plex-Token": token,
                               "Accept": "application/json"}, timeout=8)
        r.raise_for_status()
        items = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        for it in items:
            for m in (it.get("Media") or []):
                for p in (m.get("Part") or []):
                    if p.get("file"):
                        path = p["file"]
                        break
                if path:
                    break
            if path:
                break
    except Exception:
        return ""                      # unknown, not "no file" - do not cache
    if len(_FILE_BY_KEY) > 500:
        _FILE_BY_KEY.clear()
    _FILE_BY_KEY[rating_key] = path
    return path


def _sessions() -> tuple[list[dict] | None, str]:
    """Live sessions and where they came from: 'plex' or 'tautulli'."""
    if SETTINGS.plex_direct:
        s = _plex_sessions_direct()
        if s is not None:
            if SETTINGS.plex_cross_check:
                _cross_check(s)
            return s, "plex"
    return _tautulli_sessions(), "tautulli"


# ------------------------------------------------- one fetch, two readers ----
# THE GATE ROW AND THE SESSION CARDS MUST DESCRIBE THE SAME INSTANT.
#
# They did not. check_plex() called _sessions() behind a 20 s cache; the panel
# called _plex_sessions_direct() behind its own 1.5 s one. Two fetches, two
# clocks, and a row that could still say "2 direct plays" while the cards below
# it had already moved on - which is exactly what it looked like.
#
# Now both read this. The TTL depends on where the data comes from, because the
# two sources cost wildly different amounts: Plex answers /status/sessions
# locally in tens of milliseconds, while Tautulli's get_activity takes ~2.4 s on
# this box and is the entire reason gate_cache_s is 20 s. So a direct source is
# refreshed often enough to feel live, and a Tautulli source keeps the old
# behaviour rather than being polled to death on a dashboard's behalf.
_SESS_FAST_TTL = 1.5
_SESS_CACHE: tuple[float, list | None, str] = (0.0, None, "")
_SESS_LOCK = threading.Lock()


def _sessions_shared() -> tuple[list[dict] | None, str]:
    def ttl(src: str) -> float:
        return _SESS_FAST_TTL if src == "plex" else _tune("gate_cache_s")

    at, rows, src = _SESS_CACHE
    now = time.time()
    if src and now - at < ttl(src):
        return rows, src
    with _SESS_LOCK:
        at, rows, src = _SESS_CACHE
        if src and time.time() - at < ttl(src):
            return rows, src
        return _refresh_sessions()


def _refresh_sessions() -> tuple[list[dict] | None, str]:
    global _SESS_CACHE
    rows, src = _sessions()
    # A failed fetch is not cached. "We could not reach Plex" must not be held
    # for a second and a half - the next caller should try again.
    if rows is not None:
        _SESS_CACHE = (time.time(), rows, src)
    return rows, src


# --------------------------------------------------- live panel feed ----
# READ-ONLY. This never writes _PLEX_LIVE, _PLEX_DISKS or _LAST_BUSY, so
# nothing a viewer does to the dashboard can change a gate decision.
def _peer_disk(rows: list[dict]) -> list[dict]:
    r"""Fill in the disk from the host, for a machine that cannot see the files.

    WHY THE CARD HAD NO DRIVE ON THE SANDBOX. _disk_for() resolves a path
    through the storage layer, and over a share there is nothing to resolve:
    DrivePool answers with the pool's own serial, so every session came back
    with disk="" and the playback card simply had nothing to show. The host has
    the pool attached and resolves all of them exactly - so ask it, rather than
    leaving the field blank on one machine and full on the other.

    Costs nothing where it is not needed: hostio.servers() is empty when the
    storage is local, and this returns the rows untouched.
    """
    try:
        from . import hostio
        srvs = hostio.servers()
        if not srvs:
            return rows
        peer: dict = {}
        for srv in srvs:
            peer.update(hostio.peer_sessions(srv))
        if not peer:
            return rows
        out = []
        for s in rows:
            if not (s.get("disk") or "").strip():
                # BOTH SPELLINGS. These rows carry session_key; the endpoint
                # that publishes them renames it to key. Reading only one of
                # the two matched nothing at all - see hostio.session_key().
                told = peer.get(hostio.session_key(s))
                if told:
                    s = dict(s, disk=told, disk_from="host")
            out.append(s)
        return out
    except Exception:                                        # noqa: BLE001
        return rows


def panel_sessions() -> list[dict]:
    """Near-live sessions for the dashboard. Never influences the gate.

    Reads the SAME cache check_plex() reads, so the row above the cards and the
    cards themselves can never describe two different moments.
    """
    if not (SETTINGS.plex_direct and SETTINGS.plex_url and SETTINGS.plex_token):
        return _peer_disk(plex_live())
    rows, _src = _sessions_shared()
    if rows is None:
        # Cannot reach Plex. The gate's last known list is a better answer than
        # an empty one, which would read as "everybody stopped".
        return _peer_disk(plex_live())
    return _peer_disk(
        [dict(s, disk=_disk_for(s.get("file") or "") or "") for s in rows])


def _tautulli_sessions() -> list[dict] | None:
    url = SETTINGS.tautulli_url
    key = SETTINGS.tautulli_api_key
    if not url or not key:
        return None
    try:
        r = httpx.get(f"{url.rstrip('/')}/api/v2",
                      params={"apikey": key, "cmd": "get_activity"}, timeout=10)
        r.raise_for_status()
        # Same shape trap as the arr webhooks: .get(k, {}) returns the VALUE
        # whenever the key exists, so a Tautulli error response - which puts a
        # STRING in data - would crash here.
        j = r.json()
        d = j.get("response") if isinstance(j, dict) else None
        d = d.get("data") if isinstance(d, dict) else None
        sessions = d.get("sessions") if isinstance(d, dict) else None
        return [s for s in (sessions or []) if isinstance(s, dict)]
    except Exception:
        return None


# Fields that decide something. Anything else differing between the two
# sources is noise not worth a log line.
_CHECKED_FIELDS = ("state", "transcode_decision", "transcode_throttled",
                   "transcode_speed", "transcode_progress", "file")


def _cross_check(plex_rows: list[dict]) -> None:
    """Ask Tautulli the same question and report any field that disagrees.

    For proving the swap on a REAL transcode. throttled, speed and progress
    only exist when a TranscodeSession does, so a direct play cannot exercise
    them - and those three are exactly what the parked-transcode rule reads.
    """
    other = _tautulli_sessions()
    if other is None:
        return
    by_key = {str(s.get("session_key")): s for s in other}
    for p in plex_rows:
        t = by_key.get(p["session_key"])
        if not t:
            joblog.log(f"plex cross-check: session {p['session_key']} "
                       f"({p['friendly_name']}) is not in Tautulli's list",
                       "warn")
            continue
        diff = []
        for f in _CHECKED_FIELDS:
            a, b = str(p.get(f, "")).strip().lower(), str(t.get(f, "")).strip().lower()
            # progress figures drift by a fraction of a percent between two
            # calls a moment apart; that is not a disagreement.
            if f == "transcode_progress":
                try:
                    if abs(float(a or 0) - float(b or 0)) < 2.0:
                        continue
                except ValueError:
                    pass
            if a != b:
                diff.append(f"{f}: plex={a!r} tautulli={b!r}")
        if diff:
            joblog.log(f"plex cross-check DIFFERS for {p['friendly_name']}: "
                       + "; ".join(diff), "warn")


def check_plex() -> Reason:
    """Is anyone watching - and does that matter right now?

    Sessions come from Plex directly where possible, falling back to Tautulli.
    Everything below this line is source-agnostic: _sessions() returns
    Tautulli's field names either way, so the rules that decide what holds the
    queue never had to learn where the data came from.

    With transcodes_only off, ANY playback holds jobs, which is the safer
    setting on a busy server since even a direct play competes for pool reads.
    """
    # Any path that returns WITHOUT reading sessions has to clear the derived
    # state as well. Otherwise turning the check off, or the source going away,
    # leaves the last known disks pinned as "a viewer is here" forever - the
    # queue would keep avoiding a spindle nobody is watching, and the I/O
    # throttle would stay on indefinitely.
    global _PLEX_DISKS, _PLEX_PLAYING, _PLEX_LIVE

    if not get_toggle("gate.plex"):
        _PLEX_DISKS, _PLEX_PLAYING, _PLEX_LIVE = set(), 0, []
        return Reason(False, "plex", "check disabled")
    if not (SETTINGS.plex_url and SETTINGS.plex_token) \
            and not (SETTINGS.tautulli_url and SETTINGS.tautulli_api_key):
        _PLEX_DISKS, _PLEX_PLAYING, _PLEX_LIVE = set(), 0, []
        return Reason(False, "plex", "not configured")

    sessions, source = _sessions_shared()
    if sessions is None:
        # NEITHER SOURCE ANSWERED. Not the same as "nobody is watching" - we
        # simply do not know, and the derived state has to be cleared rather
        # than left pinned. Reported as an error so the panel says so instead
        # of quietly showing an open gate.
        _PLEX_DISKS, _PLEX_PLAYING, _PLEX_LIVE = set(), 0, []
        return Reason(False, "plex", "",
                      error="neither Plex nor Tautulli answered")

    # A PAUSED session is not using the GPU or the pool.
    # transcode_decision stays "transcode" while a stream is paused, so counting
    # it held the queue for as long as someone left a show paused - potentially
    # all night. Only a session that is actually PLAYING competes with us.
    #
    # BUFFERING COUNTS AS PLAYING. Plex has three player states, not two, and
    # this split used to be `state == "playing"` versus everything else - which
    # filed a buffering viewer under "paused" and let the queue run. That is
    # backwards. A buffering viewer is the one MOST in need of the pool and the
    # GPU: they are trying to play and failing. Whatever the cause, the last
    # thing that should happen at that moment is nuarr starting an encode.
    active, paused_sessions = [], []
    for s in sessions:
        st = str(s.get("state") or "playing").lower()
        (active if st in ("playing", "buffering") else paused_sessions).append(s)
    paused = len(paused_sessions)
    buffering = [s for s in active if str(s.get("state") or "").lower() == "buffering"]

    if get_toggle("gate.plex_transcodes_only"):
        busy = [s for s in active
                if (s.get("transcode_decision") or "").lower() == "transcode"]
        what = "transcoding"
    else:
        busy = active
        what = "playing"

    # THROTTLED TRANSCODES ARE NOT USING THE GPU.
    #
    # Plex transcodes ahead of the viewer and then STOPS until the buffer drains
    # - Tautulli reports transcode_throttled=1 and transcode_speed=0.0 for that
    # state. Measured on this server: throttled=1, speed 0.0, transcode_progress
    # 69% against a viewer at 28%. Zero GPU in use, and nuarr's encode queue was
    # held the whole time anyway. Over an evening's viewing that is hours of
    # idle GPU.
    #
    # A throttled session still gets to hold if its lead is thin, because it is
    # about to wake up and want the encoder back. THROTTLE_LEAD_PCT is the
    # margin: how far ahead of the viewer the transcoder must already be before
    # we treat it as genuinely parked. The gate re-evaluates every few seconds,
    # so the moment it un-throttles the hold returns.
    #
    # A BUFFERING SESSION IS NEVER PARKED, whatever its numbers say. "Parked"
    # means the viewer is comfortably ahead and the encoder has stopped by
    # choice. A viewer who is buffering is by definition not comfortably ahead,
    # and letting the exemption fire there would release the GPU at the worst
    # possible moment.
    # Hoisted above the parked test, which now needs it: the same base floor the
    # per-spindle pass below uses, so one setting decides both and the banner
    # cannot claim a session is holding by one measure while the card next to it
    # calls the same session clear by another.
    try:
        from . import workers as _wk
        _base = float(_wk.get().viewer_pause_lead_s)
    except Exception:                                        # noqa: BLE001
        _base = 0.0

    idle_throttled = []
    if busy and get_toggle("gate.plex_ignore_throttled"):
        lead_needed = _tune("throttle_lead_pct")
        _now = time.time()
        still = []
        for s in busy:
            # Buffering is judged instantly and never debounced - it is the one
            # state where reacting late is the expensive mistake.
            if str(s.get("state") or "").lower() == "buffering":
                still.append(s)
                continue
            key = str(s.get("session_key") or s.get("key") or "")
            parked = _is_parked(s, lead_needed, _base)
            if key:
                parked = _park_settled(key, parked, _now)
            (idle_throttled if parked else still).append(s)
        busy = still
    # Forget sessions that have ended, so a key reused later starts clean.
    _live_keys = {str(s.get("session_key") or s.get("key") or "")
                  for s in sessions}
    for _k in [k for k in _PARK if k not in _live_keys]:
        _PARK.pop(_k, None)

    # WHICH SPINDLES ARE UNDER A VIEWER - all of them, not just transcodes.
    #
    # Two different resources, two different rules:
    #
    #   GPU  - only a live transcode competes, so only those hold the encode
    #          pool. That is the `busy` list above.
    #   DISK - EVERY playing session reads from the pool, and a 4K remux
    #          direct-playing pulls 60-100 Mbps sustained off ONE spindle with
    #          no transcode involved at all. Those sessions were invisible
    #          here, so nuarr would happily start a stream copy on the exact
    #          disk someone was watching from - the one case most likely to
    #          make playback stutter, and the one it was not protecting.
    #
    # Parked transcodes stay in the set too. Releasing the GPU for them is well
    # evidenced (speed 0.0); whether Plex still touches that spindle while
    # parked is less certain, and the cost of being wrong is asymmetric -
    # keeping one disk clear only steers passthrough elsewhere, while guessing
    # wrong makes a viewer stutter.
    _PLEX_DISKS = {d for d in (_disk_for(s.get("file") or "")
                               for s in active) if d}
    # ...and how much buffer the thinnest viewer on each of them has. Built
    # from the SAME `active` list in the same pass, so the lead can never
    # describe a different instant from the disk set it is keyed on.
    global _PLEX_LEAD, _PLEX_FLOOR
    # _base was computed above the parked test - same value, one source.
    # THE GOVERNING SESSION, not the thinnest one.
    #
    # Each session now has its own floor, so "fewest seconds banked" no longer
    # identifies the one at risk: 40s on a 4K remux that needs 120 is in far
    # more trouble than 30s on an SD stream that needs 30. Ranking by the
    # SHORTFALL against each session's own floor picks the right one, and
    # keeping that session's lead and floor together stops the pair being
    # read off two different streams.
    # HIGH-WATER MARKS FIRST, and from every session - including the paused
    # ones, which `active` deliberately excludes. A paused client is holding
    # its buffer, not spending it, so its lead is one of the better readings
    # of what that device can manage; skipping it meant a player that was
    # paused when nuarr restarted never contributed a ceiling at all, and its
    # films kept the unreachable scaled-up floor.
    for s in (active + paused_sessions):
        try:
            _ld = float(s.get("lead_s"))
        except (TypeError, ValueError):
            continue
        _br = _num((s.get("detail") or {}).get("src_bitrate"), 0.0)
        if _br > 0 and _ld > 0:
            _cl = _device_of(s)
            _by = _ld * _br * 1000.0 / 8.0
            if _by > _CLIENT_PEAK_BYTES.get(_cl, 0.0):
                _CLIENT_PEAK_BYTES[_cl] = _by
                # Every reading can nudge this; persist only meaningful jumps,
                # or the kv write would run on most polls for nothing.
                if _by > _PEAK_SAVED.get(_cl, 0.0) * 1.25:
                    _PEAK_SAVED[_cl] = _by
                    _caps_save()

    worst: dict[str, tuple[float, float, float]] = {}
    for s in active:
        d = _disk_for(s.get("file") or "")
        if not d:
            continue
        try:
            lead = float(s.get("lead_s"))
        except (TypeError, ValueError):
            continue                     # unmeasured is not "starving"
        # A parked transcode has banked everything it needs; its lead is a
        # statement about the encoder, not about how close the viewer is to
        # running dry, and treating it as thin would pause work for nothing.
        if s.get("lead_full"):
            continue
        floor = session_floor(s, _base)
        # Published on the session itself so the card can show the number that
        # actually governs it rather than the global setting.
        s["floor_s"] = floor
        if floor <= 0:
            continue                     # buffered to the end, or unmeasured
        deficit = floor - lead
        cur = worst.get(d)
        if cur is None or deficit > cur[0]:
            worst[d] = (deficit, lead, floor)
    _PLEX_LEAD = {d: v[1] for d, v in worst.items()}
    _PLEX_FLOOR = {d: v[2] for d, v in worst.items()}
    # A disk counts as finished only if it has viewers and EVERY one of them
    # is done fetching. One stream still reading is enough to keep the whole
    # spindle under the normal rules.
    global _PLEX_DONE
    _done, _busy = set(), set()
    for s in active:
        d = _disk_for(s.get("file") or "")
        if not d:
            continue
        (_done if session_done(s) else _busy).add(d)
    _PLEX_DONE = _done - _busy
    # The disk the NEXT episode sits on, so a rollover finds it already
    # yielded. Only for sessions that are actually playing - a paused stream
    # is not about to advance.
    # ...AND ONLY WHEN THE ROLLOVER IS ACTUALLY NEAR.
    #
    # Pre-yielding is worth a little waste in the last few minutes of an
    # episode, when the jump is imminent and catching up afterwards would be
    # visible. It is not worth anything at minute three of a seventy-minute
    # film - and that is what it was doing: two extra spindles pinned to Very
    # Low I/O for the entire runtime, on behalf of a rollover an hour away that
    # may never happen at all if the viewer stops. Seen live with a viewer 328s
    # ahead of a 30s floor, holding NU-DRIVE-1 and NU-DRIVE-2 down for nothing.
    global _PLEX_NEXT_DISKS
    _soon = []
    for s in active:
        dur = _num(s.get("duration_ms"), 0.0) / 1000.0
        off = _num(s.get("offset_ms"), 0.0) / 1000.0
        # No duration means no idea how close the end is; keep the old
        # behaviour rather than guessing the rollover is far away.
        if not dur or (dur - off) <= NEXT_EPISODE_WINDOW_S:
            _soon.append(s)
    _PLEX_NEXT_DISKS = {d for d in (_next_disk_for(s.get("file") or "")
                                    for s in _soon) if d} - _PLEX_DISKS
    # Same list the rules above worked from, plus the disk each one is on, so
    # the panel and the scheduler can never disagree about who is watching.
    #
    # PAUSED SESSIONS ARE INCLUDED HERE and nowhere else. The gate deliberately
    # ignores them - a paused stream uses neither the GPU nor the pool, and
    # counting it held the queue for as long as somebody left a show paused.
    # But "paused" is exactly what a person looking at this panel wants to see:
    # the alternative is a viewer silently vanishing from the list the moment
    # they hit space, which reads as a bug.
    _PLEX_LIVE = [dict(s, disk=_disk_for(s.get("file") or "") or "")
                  for s in (active + paused_sessions)]
    # Anyone watching anything, for the I/O-priority throttle. Separate from
    # the hold decision on purpose: this does not stop work, it just makes
    # nuarr yield the disk queue while a viewer needs it.
    _PLEX_PLAYING = len(active)

    # SUPPORTING LINES, built once and shared by every branch below. These used
    # to be glued onto the end of one detail string with " · " separators, which
    # is how the row grew to a 120-character run.
    # SUPPORTING LINES SAY WHAT THE CARDS CANNOT.
    #
    # Every one of these used to name its viewers - "2 direct plays —
    # <user> and <user>", "1 session paused — <user>". That was
    # right when the row was all there was. It is not any more: the cards
    # immediately below name every viewer, their play method, their disk and
    # their state, with a poster attached. Repeating it here left the reader
    # comparing two lists of the same names.
    #
    # So the row keeps only the part the cards genuinely cannot express - the
    # RULE being applied and its consequence for the queue - and states the
    # count without the roll call. The whole direct-plays line is gone: it was
    # a list of names plus a fact ("still reading the pool") that the spindle
    # line below states properly.
    extra: list[str] = []
    if buffering:
        extra.append(f"{_plural(len(buffering), 'session')} buffering — counted "
                     "as playing, because a stalling viewer is exactly who the "
                     "hold is for")
    if paused:
        extra.append(f"{_plural(paused, 'session')} paused — paused streams "
                     "never hold the queue")
    if idle_throttled:
        extra.append(f"{_plural(len(idle_throttled), 'transcode')} buffered "
                     "ahead — encoder stopped and the viewer is well past "
                     "the buffer they need, so not holding")

    if not busy:
        # GRACE PERIOD. Resuming the instant a stream stops means someone moving
        # between episodes, or pausing to get a drink, restarts four encodes and
        # gets a loaded server when they hit play again. Keep holding for a
        # configurable while after the last playing session disappears.
        grace = _tune("hold_grace_s")
        last = _LAST_BUSY.get("plex", 0.0)
        since = time.time() - last
        if grace > 0 and last and since < grace:
            left = grace - since
            # "AFTER PLAYBACK STOPPED" IS NOT ALWAYS WHY WE GOT HERE.
            #
            # This branch means "nothing is holding any more", and there are two
            # roads to it: everyone stopped, or the sessions that were holding
            # parked their encoders and stepped out of the way. Since the parked
            # test started measuring in seconds it takes the second road often -
            # and the banner then announced playback had stopped while two cards
            # underneath it showed people happily watching.
            why_now = ("after the encoders parked" if active
                       else "after playback stopped")
            return Reason(
                True, "plex",
                f"Grace period {why_now} — "
                f"{left/60:.1f} min remaining",
                extra=extra + ["Held briefly on purpose so moving between "
                               "episodes does not restart the encoders"],
                clears=(f"Automatically in {left/60:.1f} min, unless "
                        + ("a viewer's buffer runs down again" if active
                           else "somebody starts watching again")))
            # NOTE: the old message called these "idle sessions" and counted
            # every session including ones that were direct-playing at the
            # time. They were not idle and the number was misleading.

        if not sessions:
            return Reason(False, "plex", "Nobody is watching")
        # "NONE TRANSCODING" WHEN ONE PLAINLY IS.
        #
        # `busy` is empty by the time we reach here, but that is the list AFTER
        # the parked exemption removed things from it - so a session with a live
        # transcode that has simply stopped for a while was being reported as no
        # transcode at all, directly above a card labelled "transcoding". The
        # exemption is the interesting fact; say that instead of denying the
        # transcode exists.
        if active and idle_throttled:
            detail = (f"{_plural(len(active), 'session')} playing, "
                      f"{len(idle_throttled)} transcoding but parked")
        elif active:
            detail = f"{_plural(len(active), 'session')} playing, none {what}"
        else:
            detail = f"{_plural(len(sessions), 'session')} open, none playing"
        # People ARE watching, we are just not holding for them. That is a live
        # subsystem, not silence, and the pill should not look like "Plex is not
        # configured".
        return Reason(False, "plex", detail, extra=extra, active=bool(sessions))

    _LAST_BUSY["plex"] = time.time()
    # No " — <username>" here either. The headline answers "how much is
    # holding the queue"; the card below answers "who", better.
    detail = f"{_plural(len(busy), 'session')} {what}"

    grace = _tune("hold_grace_s")
    # NAME THE NUMBER THE HOLD IS WAITING ON.
    #
    # This used to promise it clears "when Plex buffers ahead and parks the
    # encoder" - a condition the session card beside it was already reporting as
    # MET, in the words "parked (far enough ahead, not struggling)". Two panels
    # on one screen, one saying the thing had happened and the other still
    # waiting for it. The card was right about parked; the gate wanted parked
    # AND a buffer margin, and never said so.
    #
    # So the margin is stated, per session, in the same seconds the cards use.
    need = []
    for s in busy:
        try:
            _fl = session_floor(s, _base)
            _ld = float(s.get("lead_s"))
        except (TypeError, ValueError):
            continue
        if _fl > 0:
            try:
                from . import jobs as _j
                _m = float(_j.VIEWER_FULL_SPEED_MULT)
            except Exception:                                # noqa: BLE001
                _m = 3.0
            need.append((round(_fl * _m), round(_ld)))
    if need:
        want, have = need[0]
        margin = (f"when its encoder stops with over {want}s banked "
                  f"(it has {have}s)" if len(need) == 1
                  else f"when their encoders stop with enough banked "
                       f"(needs {want}s, has {have}s)")
    else:
        margin = "when Plex buffers ahead and parks the encoder"
    clears = ("When they stop watching, or " + margin
              + (f", then a {grace/60:.0f} min grace period" if grace else ""))

    if get_toggle("gate.plex_encode_only"):
        if _PLEX_DISKS:
            disks = ", ".join(sorted(_PLEX_DISKS))
            many = len(_PLEX_DISKS) > 1
            # SAY WHAT THE RULE ACTUALLY IS, in the tense it applies to.
            #
            # This used to read "passthrough jobs avoid those spindles", which
            # was wrong twice over and visibly so - the disk panel two inches
            # below would show two nuarr jobs on the very spindle the banner
            # said was avoided.
            #
            #   * It governs what a NEW job may CLAIM. Work already running is
            #     never cancelled; it finishes, at Very Low I/O priority so the
            #     viewer's reads overtake it.
            #   * It is not a blanket veto any more. Below viewer_share_pct of
            #     head room the disk is shared deliberately, because one direct
            #     play at ~1 MB/s does not justify idling a 150 MB/s spindle.
            #
            # A banner that states a rule the screen disproves teaches you to
            # stop believing the banner, which is worse than saying less.
            try:
                from . import workers as _w
                share = int(_w.get().viewer_share_pct)
            except Exception:                            # noqa: BLE001
                share = 0
            share_note = (f", or share {'them' if many else 'it'} while under "
                          f"{share}% busy" if share else "")
            extra.append(
                f"Reading {disks} — new passthrough jobs prefer the other "
                f"spindles{share_note}. Anything already running there "
                f"finishes, throttled to Very Low I/O so the viewer reads "
                f"first.")
            # NAME THE ONE THAT IS ACTUALLY AT RISK. "A viewer is on this
            # disk" is not the interesting fact; "and they have 41 seconds
            # left" is, because that is the number that decides whether nuarr
            # merely slows down or stops.
            try:
                from . import workers as _wk
                floor = float(_wk.get().viewer_pause_lead_s)
            except Exception:                            # noqa: BLE001
                floor = 0.0
            # ASK WHAT IS ACTUALLY PAUSED, do not re-derive it from the lead.
            # The pause is hysteretic - it releases at 1.5x the floor, not at
            # the floor - so "v < floor" and "nuarr has stopped" are no longer
            # the same set. Recomputing it here would put the banner back to
            # describing a rule the scheduler does not follow.
            try:
                from . import jobs as _jb
                held = {d: st for d, st in _jb.starving_disks().items()
                        if d in _PLEX_DISKS}
                mult = float(_jb.VIEWER_RESUME_MULT)
            except Exception:                            # noqa: BLE001
                held, mult = {}, 1.5
            # A disk only enters `held` once a running job has asked about it,
            # so the two sets are not the same: a viewer can be thin on a
            # spindle nuarr simply has no work on. Both are worth saying, and
            # they say different things.
            thin = sorted(d for d, v in _PLEX_LEAD.items()
                          if floor and v < floor and d not in held)
            if thin:
                bits = ", ".join(f"{d} ({_PLEX_LEAD[d]:.0f}s)" for d in thin)
                extra.append(
                    f"Low on buffer: {bits} — under the {floor:.0f}s floor. "
                    f"nuarr has nothing running on "
                    f"{'those spindles' if len(thin) > 1 else 'that spindle'} "
                    f"right now, and will not start anything until the buffer "
                    f"is back over {floor * mult:.0f}s")
            if held:
                hb = ", ".join(
                    f"{d} ({_PLEX_LEAD[d]:.0f}s)" if d in _PLEX_LEAD else d
                    for d in sorted(held))
                back = max(st["resume_at_s"] for st in held.values())
                wait = max(max(st["hold_left_s"], st["recover_left_s"] or 0.0)
                           for st in held.values())
                extra.append(
                    f"Paused for the viewer: {hb} — work is stopped on "
                    f"{'those spindles' if len(held) > 1 else 'that spindle'} "
                    f"and resumes once the buffer is back over {back:.0f}s and "
                    f"has held there"
                    + (f" — about {wait:.0f}s away" if wait > 0 else ""))
            return Reason(True, "plex", detail, extra=extra, clears=clears,
                          restricts=f"new ones avoid {disks} while "
                                    f"{'they are' if many else 'it is'} busy",
                          pools=("encode",))
        # No disk resolved (path outside the pool, or Tautulli withheld it).
        # Fall back to holding passthrough too rather than assuming the
        # viewer is on a disk we are not touching.
        extra.append("Could not tell which pool disk they are reading, so "
                     "passthrough is held as well rather than guessing")
        return Reason(True, "plex", detail, extra=extra, clears=clears)

    return Reason(True, "plex", detail, extra=extra, clears=clears)


# DrivePool reports ~0.9999 when it considers the pool balanced.


# IS THE NUMBER ACTUALLY MOVING?
#
# Neither BytesToBalance nor the store file's mtime answers "is DrivePool
# doing something". The byte count sits at a fixed residual the balancer has
# decided to tolerate (measured here: 196.8 GB, unchanged across reads), and
# the service rewrites its store on a timer whether or not it moved a byte -
# so freshness proves nothing either.
#
# What a pool genuinely placing or relocating data looks like is a byte count
# that CHANGES. So remember the last value and when it last differed: constant
# for BYTES_IDLE_S means idle, whatever the raw number says.
BYTES_IDLE_S = 90.0
_BYTES_SEEN: dict = {"value": None, "changed_at": 0.0}


# ------------------------------------------------- generic disk pressure ---
# Replaces the DrivePool-specific balance check. See diskload.py for why
# measuring the effect beats naming one product's cause.
_DISK_LABEL_KEY: dict[str, str] = {}
_DISK_LABEL_AT = 0.0
_BUSY_DISKS: set[str] = set()


def _label_keys() -> dict[str, str]:
    """pool-disk label -> physical-disk counter key.

    Pool-aware where a pool exists and harmless where one does not: a plain
    setup has no labels, so this is empty and the scoped avoidance below
    simply never fires, while the global check still watches the disks the
    library and cache actually live on.
    """
    global _DISK_LABEL_AT
    now = time.time()
    if _DISK_LABEL_KEY and now - _DISK_LABEL_AT < 300:
        return _DISK_LABEL_KEY
    try:
        from . import scanner, diskload
        m = {}
        for label, path in (scanner.media_roots() or {}).items():
            k = diskload.key_for_path(path)
            if k:
                m[label] = k
        if m:
            _DISK_LABEL_KEY.clear()
            _DISK_LABEL_KEY.update(m)
            _DISK_LABEL_AT = now
    except Exception:
        pass
    return _DISK_LABEL_KEY


def _our_bps_by_key() -> dict[str, float]:
    """Throughput nuarr's own workers are putting on each physical disk.

    Subtracting this is what stops the check pausing on its own work - see
    the feedback trap in diskload.py.
    """
    out: dict[str, float] = {}
    try:
        from . import jobs
        keys = _label_keys()
        for w in list(jobs.RUNNING.values()):
            rate = float(getattr(w, "read_bps", 0) or 0) + \
                float(getattr(w, "write_bps", 0) or 0)
            if rate <= 0:
                continue
            # A job reads its source and writes its output, often on two
            # different spindles; both are ours.
            for lbl in (getattr(w, "disk", ""), getattr(w, "dest_disk", "")):
                k = keys.get(lbl or "")
                if k:
                    out[k] = out.get(k, 0.0) + rate
    except Exception:
        pass
    return out


def _busy_now() -> tuple[list[dict], dict]:
    """(disks under sustained EXTERNAL load, all rows keyed by counter)."""
    from . import diskload
    rows = diskload.sustained()
    if not rows:
        return [], {}
    thresh = _tune("disk_busy_pct")
    ours = _our_bps_by_key()
    keys = _label_keys()
    key_label = {v: k for k, v in keys.items()}
    # Only disks that carry OUR data are relevant - a busy system disk or a
    # download drive is somebody else's business and must not hold the queue.
    watch = set(keys.values())
    for p in (getattr(SETTINGS, "cache_dir", "") or "",):
        k = diskload.key_for_path(p) if p else None
        if k:
            watch.add(k)
    hot = []
    for k, d in rows.items():
        if watch and k not in watch:
            continue
        mine = ours.get(k, 0.0)
        ext = max(0.0, d["bps"] - mine)
        # Busy AND not busy because of us. The share test matters more than
        # the absolute: a disk at 100% that is 95% our own encode is a disk
        # doing exactly what we asked it to.
        if d["busy"] >= thresh and (d["bps"] <= 0 or ext >= d["bps"] * 0.35):
            hot.append({"key": k, "label": key_label.get(k, k),
                        "busy": round(d["busy"], 1),
                        "ext_bps": ext, "bps": d["bps"], "mine_bps": mine})
    return hot, rows


def busy_disks() -> set[str]:
    """Pool-disk labels to steer new work away from, like plex_disks().

    Scoped avoidance rather than a global hold: one spindle being hammered by
    a rebuild is a reason to work on the other eleven, not a reason to stop.
    The old DrivePool check held EVERYTHING during a balance, which is why a
    pool that is 99% balanced could idle the whole queue.
    """
    return set(_BUSY_DISKS)


def disk_report() -> dict:
    """Per-managed-disk load, for the pool panel.

    Only disks the library actually lives on. The counters expose every disk on
    the box - the OS disk, a download drive, whatever else is plugged in - and
    none of those are nuarr's business or worth a row in a panel about the
    media pool. The cache disk is included when it is separate, because a
    saturated cache disk does stall the queue.

    Splits each disk's throughput into OURS and OTHER. That distinction is the
    whole point: a disk at 100% because we are encoding on it is working as
    intended, and a disk at 100% because something else has it is the one to
    steer around, and the panel should never make those look the same.
    """
    from . import diskload
    rows = diskload.sustained()
    # Rates from the newest tick, steering from the median - see
    # diskload.latest() for why the panel and the gate stopped sharing one
    # number.
    now = diskload.latest()
    thresh = _tune("disk_busy_pct")
    if not rows:
        return {"disks": [], "thresh": thresh, "window_s": diskload.WINDOW_S,
                "error": diskload.status().get("error", ""),
                "enabled": bool(get_toggle("gate.disk_busy"))}
    ours = _our_bps_by_key()
    rw = _our_rw_by_key()
    keys = _label_keys()
    out = []
    for label, k in keys.items():
        d = rows.get(k)
        if not d:
            continue
        mine = ours.get(k, 0.0)
        live = now.get(k) or d
        ext = max(0.0, live["bps"] - mine)
        busy = d["busy"]
        mr, mw = rw.get(k, (0.0, 0.0))
        out.append({
            "disk": label,
            "busy": round(busy, 1),
            "bps": live["bps"],
            "mine_bps": mine,
            "ext_bps": ext,
            # The external half, split. Which way a disk's traffic is going is
            # what says whether it is being read FROM or written TO, and that
            # is the difference between "something is copying off this disk"
            # and "something is filling it".
            "ext_read_bps": max(0.0, (live.get("read_bps") or 0) - mr),
            "ext_write_bps": max(0.0, (live.get("write_bps") or 0) - mw),
            "queue": round(d["queue"], 1),
            # HOT means "someone else has this disk", which is the only kind of
            # busy that changes what nuarr does. Same test the gate applies, so
            # a disk flagged here is exactly a disk being steered around.
            # Judged on the MEDIAN on purpose - hot is a steering decision,
            # and a one-tick burst must not paint a disk as contended.
            "hot": bool(thresh > 0 and busy >= thresh
                        and (d["bps"] <= 0
                             or max(0.0, d["bps"] - mine) >= d["bps"] * 0.35)),
            "samples": d.get("samples", 0),
        })
    out.sort(key=lambda d: d["disk"])
    try:
        moves = transfers()
    except Exception:
        moves = []
    return {"disks": out, "thresh": thresh, "window_s": diskload.WINDOW_S,
            "moves": moves,
            "error": "", "enabled": bool(get_toggle("gate.disk_busy"))}


def _our_rw_by_key() -> dict[str, tuple[float, float]]:
    """(read, write) nuarr's own workers are putting on each physical disk.

    Split, unlike _our_bps_by_key(), because the transfer detector needs to
    subtract the right HALF from each side - a commit writing to a disk must
    not be mistaken for somebody else's copy landing there.
    """
    out: dict[str, tuple[float, float]] = {}
    try:
        from . import jobs
        keys = _label_keys()
        for w in list(jobs.RUNNING.values()):
            r = float(getattr(w, "read_bps", 0) or 0)
            wr = float(getattr(w, "write_bps", 0) or 0)
            src = keys.get(getattr(w, "disk", "") or "")
            dst = keys.get(getattr(w, "dest_disk", "") or "") or src
            if src:
                a, b = out.get(src, (0.0, 0.0))
                out[src] = (a + r, b + (wr if dst == src else 0.0))
            if dst and dst != src:
                a, b = out.get(dst, (0.0, 0.0))
                out[dst] = (a, b + wr)
    except Exception:
        pass
    return out


# How lopsided a disk has to be before it counts as a source or a destination,
# and how much traffic is worth reporting at all.
_XFER_MIN_BPS = 8_000_000       # 8 MB/s - below this it is housekeeping noise
_XFER_RATIO = 3.0               # read must be this many times the write, or v.v.


def _our_moves() -> list[dict]:
    r"""Files NUARR is moving between disks right now.

    A commit is a move whether or not anybody intended it to be. nuarr reads
    the source off one spindle, writes the finished file to the cache, and then
    copies it back into the pool - and the pool is free to place it anywhere.
    When that placement lands on a different disk, the file has changed drives,
    and that is exactly the fact the transfer line exists to surface.

    NOT VISIBLE TO THE COUNTER-BASED DETECTOR BELOW, for two reasons. The read
    half comes off the cache volume, not the source disk, so there is no pool
    disk showing a matching read - the pattern is a write with no reader. And
    nuarr's own throughput is deliberately subtracted from the external figures,
    which is right for spotting somebody else's copy and wrong here: this is
    our copy, and hiding it would leave a disk visibly filling with nothing
    named as the cause.

    So it is reported separately and labelled as ours. Same line, different
    colour: the reader wants to know a file is changing disks, and then
    immediately wants to know whether that is nuarr doing its job or something
    else moving data around underneath it.
    """
    out: list[dict] = []
    try:
        from . import jobs
        for w in list(jobs.RUNNING.values()):
            src = getattr(w, "disk", "") or ""
            dst = getattr(w, "dest_disk", "") or ""
            # Only a move if it is landing somewhere ELSE. A commit that writes
            # back to the disk it came from is a replacement, not a transfer,
            # and calling it one would put a permanent arrow on every job.
            if not src or not dst or src == dst:
                continue
            if getattr(w, "stage", "") != "committing":
                continue
            rate = float(getattr(w, "commit_bps", 0) or 0) \
                or float(getattr(w, "write_bps", 0) or 0)
            # The title lives on the JOB, not on the worker - getattr(w,
            # "title") silently returned "" and the tooltip said nothing about
            # which file was moving, which is the only detail worth having here.
            try:
                what = w.job.title or ""
            except Exception:
                what = ""
            out.append({"from": src, "to": dst, "bps": rate,
                        "read_bps": 0.0, "write_bps": rate,
                        "mine": True, "what": what})
    except Exception:
        pass
    return out


def transfers() -> list[dict]:
    r"""Data being MOVED between pool disks, inferred from the counters alone.

    NO PRODUCT IS ASKED. The old DrivePool check read a JSON file StableBit
    happens to write; this works the same on a SnapRAID sync, a Storage Spaces
    rebuild, an unRAID mover, robocopy, or somebody dragging a folder in
    Explorer - because it does not care what is doing the moving, only that
    bytes are leaving one spindle and arriving on another.

    THE SIGNAL IS THE READ/WRITE SPLIT. A single disk at 95% says nothing about
    direction. A disk reading 186 MB/s and writing almost nothing, at the same
    moment as another writing 117 MB/s and reading almost nothing, is a copy
    from the first to the second - and that pair is exactly what a balance
    looks like from underneath.

    nuarr's own I/O is subtracted from each half first, so a commit writing a
    finished encode back into the pool is not reported as an external move.

    PAIRED BY SIZE, NOT PROVEN. Two sources and two destinations cannot be
    matched with certainty from rates alone, so they are paired largest-to-
    largest and the result is described as an inference. It is right in the
    common case - one thing moving at a time - and the panel says "looks like"
    rather than claiming to know.
    """
    from . import diskload
    out_mine = _our_moves()
    rows = diskload.sustained()
    if not rows:
        return out_mine
    ours = _our_rw_by_key()
    keys = _label_keys()
    label_of = {v: k for k, v in keys.items()}
    srcs: list[tuple[str, float]] = []
    dsts: list[tuple[str, float]] = []
    for k, d in rows.items():
        if k not in label_of:
            continue                     # not a disk holding our media
        mr, mw = ours.get(k, (0.0, 0.0))
        r = max(0.0, (d.get("read_bps") or 0) - mr)
        w = max(0.0, (d.get("write_bps") or 0) - mw)
        if r >= _XFER_MIN_BPS and r >= w * _XFER_RATIO:
            srcs.append((label_of[k], r))
        elif w >= _XFER_MIN_BPS and w >= r * _XFER_RATIO:
            dsts.append((label_of[k], w))
    # A disk nuarr is already committing onto is accounted for - do not also
    # report it as an unexplained arrival.
    mine_dst = {m["to"] for m in out_mine}
    dsts = [d for d in dsts if d[0] not in mine_dst]
    if not srcs or not dsts:
        return out_mine
    srcs.sort(key=lambda x: -x[1])
    dsts.sort(key=lambda x: -x[1])
    out = []
    # ONE SOURCE FEEDING SEVERAL DISKS IS THE NORMAL SHAPE OF A BALANCE, and
    # pairing largest-to-largest got it wrong: with one reader and two writers
    # it produced "NU-DRIVE-1 -> NU-DRIVE-10" and then an orphan arrow into
    # NU-DRIVE-3 from nowhere. When there is exactly one disk on a side, every
    # disk on the other side is talking to it - no matching needed, and no
    # guess involved.
    if len(srcs) == 1 and dsts:
        s, sb = srcs[0]
        for t, tb in dsts:
            out.append({"from": s, "to": t, "bps": tb,
                        "read_bps": sb, "write_bps": tb, "mine": False})
        return out_mine + out
    if len(dsts) == 1 and srcs:
        t, tb = dsts[0]
        for s, sb in srcs:
            out.append({"from": s, "to": t, "bps": sb,
                        "read_bps": sb, "write_bps": tb, "mine": False})
        return out_mine + out
    for i in range(min(len(srcs), len(dsts))):
        s, sb = srcs[i]
        t, tb = dsts[i]
        out.append({"from": s, "to": t,
                    # The smaller of the two: bytes cannot arrive faster than
                    # they leave, and the difference is other traffic on
                    # whichever disk is busier.
                    "bps": min(sb, tb),
                    "read_bps": sb, "write_bps": tb, "mine": False})
    # Anything left over is moving somewhere this pool cannot see - off to
    # another volume, or in from one. Worth saying rather than dropping.
    n = len(out)
    for s, sb in srcs[n:]:
        out.append({"from": s, "to": "", "bps": sb,
                    "read_bps": sb, "write_bps": 0.0, "mine": False})
    for t, tb in dsts[n:]:
        out.append({"from": "", "to": t, "bps": tb,
                    "read_bps": 0.0, "write_bps": tb, "mine": False})
    # OURS FIRST. When both are happening, "nuarr is moving this file" is the
    # half the reader can act on.
    return out_mine + out


def check_disk_activity() -> Reason:
    """Are the disks too busy - with somebody else's work - to add more?"""
    global _BUSY_DISKS
    if not get_toggle("gate.disk_busy"):
        _BUSY_DISKS = set()
        return Reason(False, "disks", "check disabled")
    thresh = _tune("disk_busy_pct")
    if thresh <= 0:
        _BUSY_DISKS = set()
        return Reason(False, "disks", "not watching disk load")
    try:
        hot, rows = _busy_now()
    except Exception as e:
        _BUSY_DISKS = set()
        return Reason(False, "disks", "", error=f"{type(e).__name__}: {e}")
    if not rows:
        # Counters unavailable - fail OPEN. A check that cannot measure must
        # never hold work, or an unsupported setup stalls forever.
        _BUSY_DISKS = set()
        return Reason(False, "disks", "disk counters unavailable")

    labelled = [h for h in hot if h["label"] != h["key"]]
    _BUSY_DISKS = {h["label"] for h in labelled}
    watched = len(_label_keys()) or len(rows)
    if not hot:
        return Reason(False, "disks",
                      f"All quiet (busiest {max((r['busy'] for r in rows.values()),
                                                 default=0):.0f}%)")
    names = ", ".join(h["label"] for h in sorted(hot, key=lambda x: -x["busy"])[:4])
    mb = sum(h["ext_bps"] for h in hot) / 1e6
    # EVERY relevant disk busy is the only case worth a full stop; anything
    # less is handled by steering work to the quiet ones.
    if labelled and len(labelled) >= watched:
        return Reason(
            True, "disks",
            f"Every pool disk busy — {mb:.0f} MB/s from something else",
            extra=[f"Disks at or above {thresh:.0f}% with load that is not "
                   f"nuarr's own: {names}",
                   "Starting a job now would interleave with whatever is "
                   "already using the spindles and both would crawl."],
            clears="When the other activity drops off",
            active=True)
    return Reason(
        False, "disks",
        f"{len(hot)} of {watched} disk(s) busy elsewhere — steering around them",
        extra=[f"Busy: {names} ({mb:.0f} MB/s not from nuarr). New jobs go to "
               f"the quiet spindles instead of waiting."],
        active=True)


async def check_arrs() -> Reason:
    """Is Sonarr or Radarr mid-rename / import / scan?

    Touching a file while an arr is renaming it is the original sin behind the
    EBUSY and ENOENT failures.
    """
    if not get_toggle("gate.arrs"):
        return Reason(False, "arrs", "check disabled")
    from .arr import shared_client

    busy_msgs = []
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or not cfg.api_key:
            continue
        # Shared, and NOT closed here. Building and tearing down a client per
        # poll meant paying an SSL-context construction on the event loop every
        # couple of seconds, and throwing away the connection pool each time.
        c = shared_client(cfg)
        try:
            busy, why = await c.busy()
            if busy:
                busy_msgs.append(why)
        except Exception:
            pass
    if busy_msgs:
        return Reason(
            True, "arrs", "; ".join(busy_msgs),
            extra=["Renaming or importing rewrites paths underneath us — the "
                   "original cause of the EBUSY and missing-file failures"],
            clears="When the rename, import or scan finishes")
    return Reason(False, "arrs", "Idle — no rename, import or scan running")


def check_manual() -> Reason:
    if get_toggle("gate.manual_pause"):
        return Reason(True, "manual", "Paused from the dashboard",
                      extra=["Running jobs are finishing; nothing new starts"],
                      # Names the Resume button in the Job gate heading, not
                      # the settings page. The gate settings moved to
                      # /settings, and pointing someone at another page to
                      # undo a pause when the control is right above them
                      # would be the wrong instruction as well as a slower one.
                      clears="When you press Resume, in the Job gate heading")
    return Reason(False, "manual", "Not paused")


def check_audiolang() -> Reason:
    r"""Is a listening pass running, and how far through is it.

    NEVER BLOCKS. Language detection takes the GPU for a second or two per
    track, which is worth showing next to the encoder - but it is not a reason
    to hold anything, and dressing it up as one would train the eye to ignore
    the colour that means "the queue is actually stopped".

    It belongs on this panel for the same reason Plex sessions do: it is a
    subsystem competing for the same hardware, and "the encoder feels slow" is
    much easier to explain when you can see what else is using the card.
    """
    try:
        from . import audiolang
        if not audiolang.available():
            return Reason(False, "audiolang", "Audio language ID not installed")
        p = audiolang.progress()
    except Exception as e:                               # noqa: BLE001
        return Reason(False, "audiolang", "Audio language", error=str(e)[:120])

    state = p.get("state") or "idle"
    if state in ("idle", "error"):
        if state == "error" and p.get("error"):
            return Reason(False, "audiolang", "Audio language check",
                          error=str(p["error"])[:120])
        try:
            waiting = audiolang.pending_count()
        except Exception:                                # noqa: BLE001
            waiting = 0
        done = int(p.get("applied") or 0)
        if waiting:
            # A NUMBER, not a list. This panel answers "is anything wrong and
            # is anything moving"; which files they are is a question for the
            # Audio language page, and it is named here so there is somewhere
            # to go for the answer.
            return Reason(
                False, "audiolang",
                f"{_plural(waiting, 'track')} waiting to be listened to",
                extra=["Runs within 30 minutes, or now from "
                       "Settings → Audio language"])
        note = (f"last pass named {done} file(s)" if done
                else "nothing waiting")
        return Reason(False, "audiolang", f"Audio language check idle — {note}")

    total = int(p.get("total") or 0)
    dn = int(p.get("done") or 0)
    pct = f"{dn * 100 // total}%" if total else "starting"
    extra = []
    if p.get("current"):
        extra.append(str(p["current"])[:90])
    found, refused = int(p.get("found") or 0), int(p.get("refused") or 0)
    if found or refused:
        extra.append(f"{found} identified, {refused} too unclear to call")
    return Reason(False, "audiolang",
                  f"Listening to audio: {dn} of {total} tracks ({pct})",
                  extra=extra, active=True)


def check_cache_space() -> Reason:
    """Refuse to start work we cannot finish."""
    from . import fileops
    free = fileops.free_space_gb(SETTINGS.cache_dir)
    need = SETTINGS.cache_min_free_gb
    if free < need:
        return Reason(
            True, "cache",
            f"Only {free:.0f} GB free on the transcode cache",
            extra=[f"Every encode writes its output here before the commit "
                   f"copies it back to the pool; the floor is {need:.0f} GB"],
            clears=f"When {need - free:.0f} GB more is freed on "
                   f"{SETTINGS.cache_dir} — finishing jobs release their "
                   "temporary files as they commit")
    return Reason(False, "cache", f"{free:.0f} GB free",
                  extra=[f"Floor is {need:.0f} GB"] if free < need * 2 else [])


# ---------------------------------------------------------------- status ---
async def status() -> GateStatus:
    """Gate state, WITHOUT blocking the event loop.

    check_plex() makes a synchronous httpx call with a 10 s timeout, and
    check_cache_space() does file and disk I/O. Calling them
    straight from this coroutine ran all of that ON the loop, so every dispatch
    tick - once every 3 s - froze the whole server for as long as Tautulli took
    to answer. Symptoms were everywhere but looked unrelated: /api/jobs taking
    2-9 s, and the dashboard's 2 s poll returning stale, out-of-order snapshots
    so worker cards appeared to start and stop at random.
    """
    plex, disks, cache = await asyncio.gather(
        asyncio.to_thread(_cached, "plex", check_plex),
        asyncio.to_thread(_cached, "disks", check_disk_activity),
        asyncio.to_thread(_cached, "cache", check_cache_space),
    )
    # PLEX LAST, so it sits directly above the session cards it is describing.
    # The row says "2 sessions playing" and the cards say which two; separating
    # them with Cache and the arrs meant reading the summary, two unrelated
    # lines, and only then the detail.
    #
    # There used to be a DrivePool row here as well, asking one product whether
    # it was balancing. `disks` replaced it: it MEASURES per-spindle activity,
    # so it covers a balance along with a backup, a SnapRAID sync, a Storage
    # Spaces rebuild or a plain file copy - none of which the old check could
    # see - and it steers around the busy disks instead of holding everything.
    reasons = [
        check_manual(),
        disks, cache,
        await check_arrs(),
        # Between the arrs and Plex: like Plex it is a live subsystem sharing
        # the hardware, and unlike the checks above it can never hold anything.
        check_audiolang(),
        plex,
    ]
    return GateStatus(open=not any(r.blocked for r in reasons),
                      reasons=reasons, checked_at=time.time())


async def is_open() -> bool:
    return (await status()).open
