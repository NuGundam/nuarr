r"""One place that knows every recurring job nuarr runs.

WHY THIS EXISTS
---------------
nuarr starts eighteen background loops at boot - the adopter, the healer, the
rule audit, the arr guards, backups, the ffmpeg update check, and so on. Each
one knew its own cadence and nothing knew all of them, so "what runs on this
box, and when does it next run" could only be answered by reading the source.
Profilarr shows exactly this as a single Background Jobs table, and it is the
right shape: one row per job, when it last ran, when it runs next.

HOW IT STAYS HONEST
-------------------
The alternative was to derive everything from the interval constants, which
would produce a confident table that drifts the moment a loop is slow, held, or
has thrown. Instead each loop calls beat() as it starts an iteration, so
last_run is an observation rather than a prediction. next_run is then the only
derived value, and it is presented as approximate because it is.

A job that has registered but never beaten shows as "waiting for its first
run" rather than being hidden or given a made-up timestamp. Most loops sleep
for a few minutes before their first pass so they do not compete with startup,
and inventing a last_run for them would misreport a healthy system.
"""
from __future__ import annotations

import contextvars
import threading
import time

_LOCK = threading.Lock()
REG: dict[str, dict] = {}
_ORDER: list[str] = []
UNKNOWN: dict[str, int] = {}     # beats for keys nobody registered - see beat()


def register(key: str, label: str, group: str, every_s: float,
             what: str = "", toggle: str | None = None) -> None:
    """Declare a recurring job. Safe to call twice; the last call wins."""
    with _LOCK:
        if key not in REG:
            _ORDER.append(key)
        REG.setdefault(key, {"last_run": 0.0, "last_result": "", "runs": 0,
                             "last_error": "", "last_error_at": 0.0})
        REG[key].update(label=label, group=group, every_s=float(every_s),
                        what=what, toggle=toggle)


# WHICH LOOP IS CURRENTLY RUNNING, per asyncio task.
#
# Every background loop already calls beat() at the top of each pass, and each
# loop is its own task - and an asyncio task runs in its own copy of the
# context. So setting this in beat() marks that loop, and only that loop, for
# as long as it lives. joblog.log() reads it, which is how seventeen existing
# loops became filterable in the log viewer without any of them being edited.
#
# contextvars also propagate through asyncio.to_thread, so the threaded halves
# of these loops stay attributed too.
_CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nuarr_system_job", default="")


def current() -> str:
    """The background loop running on this task, or '' outside one."""
    try:
        return _CURRENT.get()
    except Exception:                                    # noqa: BLE001
        return ""


def beat(key: str, result: str = "") -> None:
    """Called by a loop as it begins a pass. Never raises.

    Deliberately swallowing everything: this is instrumentation attached to
    eighteen long-running loops, and a bookkeeping bug must not be able to kill
    the adopter or the backup job. A missing row on a status page is a far
    smaller failure than a watcher that stopped.
    """
    try:
        # Mark this task as belonging to `key` before anything else, so even a
        # pass that fails immediately still has its log lines attributed.
        _CURRENT.set(key)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        with _LOCK:
            r = REG.get(key)
            if r is None:
                # A beat for a key nobody registered. This happened on the
                # first run - the loops beat 'playback' while the registry
                # said 'plex.playback' - and because beat() ignores unknown
                # keys the whole table just read "never" with no error
                # anywhere. Record it so the next mismatch announces itself
                # instead of looking like fifteen broken watchers.
                UNKNOWN[key] = UNKNOWN.get(key, 0) + 1
                return
            r["last_run"] = time.time()
            r["runs"] += 1
            if result:
                r["last_result"] = result
    except Exception:
        pass


def fail(key: str, err: str) -> None:
    try:
        with _LOCK:
            r = REG.get(key)
            if r is not None:
                r["last_error"] = err[:300]
                r["last_error_at"] = time.time()
    except Exception:
        pass


def _toggle_on(name: str | None) -> bool | None:
    """Is this job switched on? None means "no switch" or "cannot tell".

    nuarr stores on/off flags two ways: gate.* keys have entries in
    gate.DEFAULTS and are read with get_toggle(), while others - autoqueue.
    enabled among them - are plain kv rows with no default. get_toggle() on the
    second kind returns its fallback of "0", so the first version of this
    reported auto-queue as OFF while it was demonstrably running and filling
    the queue. A status page that says a running job is off is worse than one
    that says nothing, so an unknown key now answers None rather than False.
    """
    if not name:
        return None
    try:
        from .gate import kv_get, DEFAULTS
        raw = kv_get(name)
        if raw is not None:
            return str(raw) == "1"
        if name in DEFAULTS:
            return str(DEFAULTS[name]) == "1"
        return None
    except Exception:
        return None


def snapshot() -> dict:
    now = time.time()
    rows = []
    with _LOCK:
        for key in _ORDER:
            r = REG.get(key)
            if not r:
                continue
            on = _toggle_on(r.get("toggle"))
            last = r["last_run"] or 0.0
            nxt = (last + r["every_s"]) if last else None
            # A next_run in the past means the loop is inside a pass right now,
            # or is held by its gate. Reporting a negative countdown would read
            # as a fault, so it is flattened to "due".
            rows.append({
                "key": key, "label": r["label"], "group": r["group"],
                "what": r.get("what", ""), "every_s": r["every_s"],
                "last_run": last or None, "next_run": nxt,
                "overdue": bool(nxt and nxt < now - 5),
                "runs": r["runs"], "last_result": r["last_result"],
                "last_error": r["last_error"] or "",
                "last_error_at": r["last_error_at"] or None,
                "enabled": on,
                "status": ("off" if on is False else
                           "waiting" if not last else
                           "error" if r["last_error_at"] > last else "ok"),
            })
    return {"now": now, "rows": rows, "unknown": dict(UNKNOWN)}


# ---------------------------------------------------------------- first look --
_FIRST: set = set()
_LOOP = None


def bind_loop(loop) -> None:
    """Remember the app's event loop, so first_look never invents one."""
    global _LOOP
    _LOOP = loop


def first_look(name: str, run, when: bool = True) -> bool:
    r"""Start a check's first pass, once, the first time anybody asks for it.

    WHY THIS EXISTS
    ---------------
    Every "does X still agree?" check keeps its answer in memory and has a loop
    behind it that refreshes it. A restart empties the memory, and the loops
    deliberately wait a minute or three before their first sweep so that boot
    is not a stampede. In that window the health page shows a row saying "not
    checked yet" next to another row showing a real number, and the difference
    is not about the two systems at all - it is about which one happened to
    have somebody call it. Erik hit exactly this on Plex and asked why the
    subtitle row beside it was right; the subtitle check starts itself.

    So: any check whose answer is empty starts its own first pass when the page
    asks for it. Once per process - the loop owns every pass after this one -
    and never blocking, because the caller is rendering a page.

    ON NOT INVENTING AN EVENT LOOP
    ------------------------------
    The first version of this ran async scans with asyncio.run() on a private
    thread, which looked harmless and took the whole app down. arr.py keeps one
    shared httpx.AsyncClient; a private loop touched it, then closed, and the
    client stayed bound to a loop that no longer existed, so every later arr
    call on the real loop raised and the process stopped answering. An async
    check therefore goes to the app's own loop or it does not run at all.
    """
    if not when or name in _FIRST:
        return False

    started = False
    try:
        r = run()
    except Exception as e:                                       # noqa: BLE001
        from . import joblog
        joblog.log(f"first {name} check: {type(e).__name__}: {e}", "warn")
        return False

    if hasattr(r, "__await__"):                     # a coroutine: the app's loop
        import asyncio
        loop = _LOOP
        if loop is None or loop.is_closed():
            r.close()                               # never on a loop of our own
            return False
        _FIRST.add(name)
        asyncio.run_coroutine_threadsafe(_guard(name, r), loop)
        started = True
    else:
        _FIRST.add(name)                            # a plain call, already done
        started = True

    if started:
        from . import joblog
        joblog.log(f"{name}: nothing measured since the restart - checking "
                   "now, because the page asked", "debug")
    return started


async def _guard(name: str, coro) -> None:
    try:
        await coro
    except Exception as e:                                       # noqa: BLE001
        from . import joblog
        joblog.log(f"first {name} check: {type(e).__name__}: {e}", "warn")
