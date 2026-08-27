r"""Is there a newer nuarr, and what changed in it.

DELIBERATELY INERT UNTIL CONFIGURED. There is no repo baked in, so on a fresh
install this reports "not configured" and does nothing else - no requests, no
badge, no error banner. A checker that shouts about a network failure it was
never asked to attempt is noise, and noise in a status panel is how people stop
reading status panels.

NOTIFY, DO NOT APPLY. Checking is automatic; installing is a click. nuarr is
usually mid-encode or mid-commit, and a self-update that swaps files under a
running ffmpeg would leave a half-written file in the library - which is the
one outcome the whole system exists to prevent.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import joblog, version

# GitHub's unauthenticated limit is 60 requests an hour per IP, shared with
# anything else on this network. A check every 6 hours costs 4 of them a day
# and still finds a release the same day it lands.
CHECK_EVERY_S = 6 * 3600.0
# Long enough for a slow morning, short enough that a hung request cannot stall
# the settings page behind it.
TIMEOUT_S = 12.0

_STATE: dict = {
    "checked_at": 0.0,      # when the last attempt finished, success or not
    "ok": False,            # did the last attempt actually reach GitHub
    "error": "",
    "latest": "",           # newest release tag that parses as a version
    "latest_url": "",
    "latest_notes": "",
    "latest_at": "",
    "previous": "",         # the one before it, so "what did I skip" is answerable
    "previous_url": "",
    "releases": [],         # [{version, url, notes, published}] newest first
}
_LOCK = threading.Lock()


def _repo() -> str:
    """owner/name from settings, falling back to the built-in default."""
    try:
        from .config import SETTINGS
        r = (getattr(SETTINGS, "update_repo", "") or "").strip()
    except Exception:                                        # noqa: BLE001
        r = ""
    return r or version.DEFAULT_REPO


def configured() -> bool:
    return bool(_repo())


def _fetch(repo: str) -> list[dict]:
    r"""Releases newest-first. Raises on anything that is not a clean answer.

    Asking for releases rather than tags: a tag is just a commit somebody
    labelled, while a release is a deliberate statement that this is meant to
    be installed, and it carries the notes that make the update panel worth
    reading. Repos that only tag will report nothing here, which is correct -
    nothing has been offered.
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=10"
    req = urllib.request.Request(url, headers={
        # GitHub rejects requests with no User-Agent outright.
        "User-Agent": f"nuarr/{version.VERSION}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for rel in data if isinstance(data, list) else []:
        if rel.get("draft"):
            continue        # not published, not offerable
        tag = str(rel.get("tag_name") or rel.get("name") or "")
        if version.parse(tag) is None:
            continue        # not a version; see version.is_newer for why
        out.append({
            "version": tag.lstrip("vV"),
            "url": rel.get("html_url") or "",
            "notes": (rel.get("body") or "").strip(),
            "published": (rel.get("published_at") or "")[:10],
            "prerelease": bool(rel.get("prerelease")),
        })
    # Sorted by PARSED version, not by publish date: a patch to an older line
    # can be published after a newer minor, and date order would then offer it
    # as an upgrade.
    out.sort(key=lambda r: version.parse(r["version"]) or (0, 0, 0), reverse=True)
    return out


def check(force: bool = False) -> dict:
    """Refresh if due (or forced) and return the current state."""
    repo = _repo()
    if not repo:
        with _LOCK:
            _STATE.update(ok=False, error="", checked_at=time.time())
            return dict(_STATE)
    now = time.time()
    with _LOCK:
        fresh = _STATE["checked_at"] and (now - _STATE["checked_at"]) < CHECK_EVERY_S
        if fresh and not force:
            return dict(_STATE)
    try:
        rels = _fetch(repo)
    except urllib.error.HTTPError as e:
        # 404 is the interesting one: it means the repo name is wrong, or it is
        # private, and saying "not found" is far more useful than "HTTP error".
        msg = ("repository not found - check the owner/name"
               if e.code == 404 else f"GitHub returned {e.code}")
        with _LOCK:
            _STATE.update(ok=False, error=msg, checked_at=time.time())
            return dict(_STATE)
    except Exception as e:                                   # noqa: BLE001
        with _LOCK:
            _STATE.update(ok=False, error=f"{type(e).__name__}: {e}",
                          checked_at=time.time())
            return dict(_STATE)

    # STABLE RELEASES DECIDE THE BADGE. Pre-releases are still listed, so you
    # can see one exists and install it deliberately, but they must not light
    # up the header on a machine that only wants stable builds.
    stable = [r for r in rels if not r["prerelease"]]
    top = stable[0] if stable else {}
    prev = stable[1] if len(stable) > 1 else {}
    with _LOCK:
        _STATE.update(
            checked_at=time.time(), ok=True, error="",
            latest=top.get("version", ""), latest_url=top.get("url", ""),
            latest_notes=top.get("notes", ""), latest_at=top.get("published", ""),
            previous=prev.get("version", ""), previous_url=prev.get("url", ""),
            releases=rels,
        )
        return dict(_STATE)


def status() -> dict:
    """What the UI shows. Never makes a request; check() does that."""
    with _LOCK:
        s = dict(_STATE)
    latest = s.get("latest") or ""
    return {
        "current": version.VERSION,
        "build_date": version.BUILD_DATE,
        "repo": _repo(),
        "configured": bool(_repo()),
        "checked_at": s["checked_at"],
        "ok": s["ok"],
        "error": s["error"],
        "latest": latest,
        "latest_url": s.get("latest_url", ""),
        "latest_notes": s.get("latest_notes", ""),
        "latest_at": s.get("latest_at", ""),
        "previous": s.get("previous", ""),
        "previous_url": s.get("previous_url", ""),
        "update_available": version.is_newer(latest),
        "releases": s.get("releases", []),
    }


async def watch() -> None:
    """Background loop. Silent when there is no repo to ask about."""
    import asyncio
    while True:
        try:
            if configured():
                before = _STATE.get("latest", "")
                st = await asyncio.to_thread(check)
                after = st.get("latest", "")
                # Logged only on a CHANGE. A line every six hours saying the
                # version is the same one it was six hours ago is the kind of
                # entry that pushes real events off the end of the log.
                if after and after != before and version.is_newer(after):
                    joblog.log(f"update available: {after} "
                               f"(running {version.VERSION})", "info")
        except Exception:                                    # noqa: BLE001
            pass
        await asyncio.sleep(900)     # re-evaluate every 15 min; check() gates
