"""
nuarr - where a committed file lands

WHY
---
A commit copies the finished file from the cache back onto the pool, and
until now DrivePool chose the spindle: the copy was staged at its final
P:\\ path and the pool driver put it wherever its rules said. That is fine
when the balancer is running, and it is exactly the thing the balancer
then has to undo when it is not - every commit onto a full disk is bytes
the next balance pass moves again, at 1 MB/s while somebody is watching.

Erik: "fill the emptiest disk that is not busy on commit, and work with the
balancer on if possible, else default to the current setup".

HOW
---
DrivePool keeps no record of where a file is. The pool is a live overlay of
the hidden PoolPart.<guid> folders on each member disk, and a file's
location IS its record: put the file in a PoolPart at the same relative
path and the pool shows it there at once. Moving a file between PoolParts
is what the balancer itself does. So the commit copies into the chosen
disk's PoolPart directly, and the swap that follows is the same one
fileops.safe_replace has always done - original aside, new file in, verify,
backup gone - with the new file arriving on the chosen spindle instead of
the one DrivePool would have picked.

WHICH DISK. The emptiest member disk - by used percent when DrivePool's
own equaliser works by percent, by free bytes otherwise - that nobody is
watching from, that the load counters do not call busy, that has room for
the file with a margin, and that Prevent Drive Overfill (when on) would not
call full afterwards. If that is the disk the file is already on, nothing
changes. A duplicated file (two PoolParts hold it) is left to DrivePool:
placing one copy is a job for something that knows where the other is.

WITH THE BALANCER ON. Filling the emptiest disk is what DrivePool's
default placement and the Disk Space Equalizer / All In One already want,
so with those the two agree and nuarr proceeds. Ordered File Placement,
the SSD Optimizer and the Drive Usage Limiter want something else, and a
balancer with file placement rules may too; with any of those on, nuarr
steps back and lets DrivePool choose, as before. The switch is
drivepool.place on the DrivePool page; the page also says what the last
decision was and why.
"""
from __future__ import annotations

import os
import shutil
import time

from . import joblog

# Enabled balancer plugins that decide placement by something other than
# free space. With one of these on, DrivePool chooses.
CONFLICTS = ("Ordered File Placement", "SSD Optimizer", "Drive Usage Limiter")
# Room to leave on the chosen disk beyond the file itself.
MARGIN_BYTES = 20 * 2**30
MARGIN_FRACTION = 0.02

# The last decision, for the page: {at, file, chosen, from, why, placed}
LAST: dict = {"at": 0.0, "file": "", "chosen": "", "from": "", "why": "",
              "placed": False}
# THE LAST FEW, NOT JUST THE LAST. One line answers "what did it just do";
# four answer "is it doing the same thing every time" - the question that
# says whether the emptiest disk is soaking up every commit or the choice
# is spreading. Newest first.
RECENT: list = []
RECENT_MAX = 4
_USAGE: dict = {"at": 0.0, "data": {}}


def enabled() -> bool:
    try:
        from . import drivepool
        return drivepool.get_toggle("drivepool.enabled") and \
            drivepool.get_toggle("drivepool.place")
    except Exception:                                        # noqa: BLE001
        return False


def _usage(parts: dict) -> dict:
    """label -> (total, used, free) per member disk, held for 15 s."""
    now = time.time()
    if now - _USAGE["at"] < 15 and _USAGE["data"]:
        return _USAGE["data"]
    out = {}
    for label, part in parts.items():
        try:
            u = shutil.disk_usage(part)
            out[label] = (int(u.total), int(u.used), int(u.free))
        except OSError:
            pass
    _USAGE.update(at=now, data=out)
    return out


def _balancer_verdict() -> tuple[bool, str]:
    """(may nuarr place, why not)."""
    try:
        from . import drivepool
        b = drivepool.balancing()
        auto = b.get("auto")
        if auto in (None, 0):
            return True, ""                      # balancer off: our call
        on = {p["name"] for p in (drivepool.balance_info().get("plugins") or [])
              if p.get("on")}
        clash = sorted(on & set(CONFLICTS))
        if clash:
            return False, (f"DrivePool's balancer is on with {', '.join(clash)} "
                           f"- it places by its own order, so DrivePool chooses")
        return True, ""
    except Exception as e:                                   # noqa: BLE001
        return False, f"could not read DrivePool's balancer settings ({type(e).__name__})"


def _by_percent() -> bool:
    try:
        from . import drivepool
        eq = (drivepool.balancers() or {}).get("equalizer") or {}
        if eq.get("by_free") or eq.get("by_used"):
            return False
    except Exception:                                        # noqa: BLE001
        pass
    return True


def _fill_ceiling() -> float:
    """Prevent Drive Overfill's fill line as a fraction, or 1.0."""
    try:
        from . import drivepool
        of = (drivepool.balancers() or {}).get("overfill") or {}
        f = float(of.get("fill") or 0)
        return f if 0.3 <= f <= 1.0 else 1.0
    except Exception:                                        # noqa: BLE001
        return 1.0


def choose(target: str, size: int, pool_root: str = "P:\\") -> tuple:
    """(poolpart_root, label, why) for the disk `target` should land on,
    or (None, "", why) to let DrivePool choose. Cheap: a dozen stats."""
    from . import scanner
    why = ""
    if not enabled():
        return None, "", "placement is off"
    ok, why = _balancer_verdict()
    if not ok:
        _note(target, "", "", why, False)
        return None, "", why
    parts = scanner.pool_disks() or {}
    if len(parts) < 2:
        return None, "", "fewer than two pool disks"
    p = scanner.strip_extended_prefix(target)
    try:
        rel = os.path.relpath(p, pool_root)
    except ValueError:
        return None, "", "not a pool path"
    if rel.startswith(".."):
        return None, "", "not a pool path"
    holders = [l for l, part in parts.items()
               if os.path.exists(os.path.join(part, rel))]
    if len(holders) > 1:
        why = f"duplicated on {', '.join(sorted(holders))} - DrivePool keeps the copies"
        _note(target, "", "", why, False)
        return None, "", why
    src = holders[0] if holders else ""
    try:
        from . import gate
        avoid = set(gate.plex_disks()) | set(gate.busy_disks())
    except Exception:                                        # noqa: BLE001
        avoid = set()
    use = _usage(parts)
    ceiling = _fill_ceiling()
    need = int(size) + max(MARGIN_BYTES, 0)
    best, best_key, skipped = "", None, []
    for label, (total, used, free) in use.items():
        if label in avoid:
            skipped.append(f"{label} busy")
            continue
        if free < need + int(total * MARGIN_FRACTION):
            skipped.append(f"{label} full")
            continue
        if total and (used + size) / total > ceiling:
            skipped.append(f"{label} over the fill line")
            continue
        key = ((used / total) if (total and _by_percent()) else -free)
        if best_key is None or key < best_key:
            best, best_key = label, key
    if not best:
        why = "no disk has room and is free of viewers and load" + \
            (f" ({'; '.join(skipped[:6])})" if skipped else "")
        _note(target, "", src, why, False)
        return None, "", why
    if best == src:
        why = f"already on the emptiest free disk, {src}"
        _note(target, src, src, why, False)
        return None, "", why
    t, u, f = use[best]
    why = (f"emptiest disk not in use: {best} at {u / t * 100:.0f}% "
           f"({f / 2**30:.0f} GB free)" + (f", from {src}" if src else ""))
    _note(target, best, src, why, True)
    return parts[best], best, why


def _note(target: str, chosen: str, src: str, why: str, placed: bool) -> None:
    LAST.update(at=time.time(), file=os.path.basename(target or ""),
                chosen=chosen, **{"from": src}, why=why, placed=placed)
    RECENT.insert(0, dict(LAST))
    del RECENT[RECENT_MAX:]


def landed(target: str, label: str) -> None:
    """The file is on `label` now: say so in the files table and the log."""
    try:
        from .db import cursor
        with cursor() as cur:
            cur.execute("UPDATE files SET pool_disk=? WHERE path=?", (label, target))
    except Exception:                                        # noqa: BLE001
        pass
    try:
        joblog.log(f"placed on {label}: {os.path.basename(target)}", "info")
    except Exception:                                        # noqa: BLE001
        pass


def status() -> dict:
    ok, why = _balancer_verdict() if enabled() else (False, "placement is off")
    return {"enabled": enabled(), "may_place": ok, "why": why,
            "by": "percent used" if _by_percent() else "free space",
            "ceiling": _fill_ceiling(), "last": dict(LAST),
            "recent": [dict(r) for r in RECENT]}


def register() -> None:
    """Hand fileops the chooser, so every commit path gets it."""
    from . import fileops
    fileops.PLACER = choose
    fileops.PLACED = landed
