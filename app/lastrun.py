"""When a periodic task last ran - kept across restarts.

A task that runs "every six hours" kept its clock in memory, so every start
of nuarr was also the start of every such task: the not-walked check, the arr
agreement check and the audio-language page each read all 1,152 series from
Sonarr within minutes of every start, and the rename plan asked for a preview
of each. On a day with many restarts Sonarr sat at three and a half cores for
it (2026-11-28). A task marks its run here; at start it waits out what is left
of its interval instead of beginning again.
"""
from __future__ import annotations

import time

from .db import kv_get, kv_set


def mark(key: str) -> None:
    try:
        kv_set("lastrun." + key, str(time.time()))
    except Exception:                                            # noqa: BLE001
        pass


def age(key: str) -> float:
    """Seconds since the task last ran; a very large number if it never has."""
    try:
        return max(0.0, time.time() - float(kv_get("lastrun." + key) or 0))
    except Exception:                                            # noqa: BLE001
        return 1e12


def first_wait(key: str, every_s: float, at_least: float) -> float:
    """How long a task should wait after a start before its first run."""
    return max(float(at_least), float(every_s) - age(key))
