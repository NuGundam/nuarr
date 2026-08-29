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
# ONCE PER DAY. It was six hours, which bought nothing - releases land at
# most daily and each check is a network call to GitHub that shows up in
# nobody's favour. Daily also makes the Jobs page entry honest: one row, one
# cadence, matching what actually happens. "Check now" on the Updates page
# still forces one on demand.
CHECK_EVERY_S = 24 * 3600.0
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
    """owner/name from settings, falling back to the built-in default.

    "off" is an ANSWER, not an absence. With a real default repo, an empty
    setting now means "use the official one" - which removed the old way of
    opting out (leave it blank). The explicit word restores it: someone who
    wrote "off" has said no to update checks, and no default gets to talk
    over that.
    """
    try:
        from .config import SETTINGS
        r = (getattr(SETTINGS, "update_repo", "") or "").strip()
    except Exception:                                        # noqa: BLE001
        r = ""
    if r.lower() in ("off", "none", "disabled"):
        return ""
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
        # The installer asset, if one is attached - the thing self-update
        # downloads. Name-matched rather than "first asset" so a stray
        # checksum file or zip cannot be mistaken for the installer.
        asset = next((a for a in (rel.get("assets") or [])
                      if str(a.get("name", "")).lower().startswith("nuarr-setup")
                      and str(a.get("name", "")).lower().endswith(".exe")), None)
        out.append({
            "version": tag.lstrip("vV"),
            "url": rel.get("html_url") or "",
            "notes": (rel.get("body") or "").strip(),
            "published": (rel.get("published_at") or "")[:10],
            "prerelease": bool(rel.get("prerelease")),
            "asset_url": (asset or {}).get("browser_download_url", ""),
            "asset_size": int((asset or {}).get("size") or 0),
            "asset_name": (asset or {}).get("name", ""),
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
        # A FAILURE IS NOT WORTH CACHING FOR SIX HOURS. The first check after
        # a restart - which is exactly when the page reloads to greet the new
        # version - can lose the race with the network stack coming back up.
        # Caching that loss for the full interval meant the Updates page said
        # "Could not check" until a human pressed Check now, every single
        # update. A success keeps the long interval; a failure goes stale in
        # a minute, so the next page load simply tries again.
        ttl = CHECK_EVERY_S if _STATE["ok"] else 60
        fresh = _STATE["checked_at"] and (now - _STATE["checked_at"]) < ttl
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
        "mode": _mode(),
        "apply": apply_status(),
    }


# ------------------------------------------------------- self-update ----
# DOWNLOAD AND VERIFY ARE AUTOMATIC (in auto mode, while idle); INSTALLING IS
# STILL A DECISION. The staged build sits verified on disk and the header says
# "ready to install"; the swap itself happens when the person says so - from
# the power menu - or never. The one exception nuarr makes for itself is the
# same one it makes for ffmpeg updates: nothing is ever written over the
# running install in place. The swap is a staged copy, applied by a helper
# process AFTER this one has exited, so a failed update leaves either the old
# install or the new one - never a mixture.

APPLY_DIR_NAME = "update"
IDLE_STAGE_S = 600.0          # auto mode: this long with no jobs and no
                              # viewers before the download starts
_APPLY = {
    "state": "idle",          # idle|downloading|verifying|ready|applying|error
    "progress": 0.0,          # download fraction 0..1
    "staged_version": "",
    "error": "",
}
_APPLY_LOCK = threading.Lock()
_IDLE_SINCE = [0.0]


def _data_dir():
    from .config import DATA_DIR
    return DATA_DIR


def _install_root() -> str:
    """The folder holding app/ - what gets replaced on apply."""
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def apply_status() -> dict:
    with _APPLY_LOCK:
        return dict(_APPLY)


def _set(state=None, **kw):
    with _APPLY_LOCK:
        if state is not None:
            _APPLY["state"] = state
        _APPLY.update(kw)


def stage(force: bool = False) -> dict:
    r"""Download the latest release's installer, verify it, unpack the new
    program tree into DATA_DIR\update\staged. Synchronous - run in a thread.

    The download is the INSTALLER exe, not a bare zip, because that is the
    one artifact every release is guaranteed to carry. The installer is
    [stub][zip][offset:8]["NUARRSFX":8], a format this codebase defines, so
    nuarr can lift the zip straight out of it - the update path and the
    fresh-install path ship one file between them, which is one file whose
    integrity matters instead of two.
    """
    import os
    import shutil
    import zipfile
    # FRESH METADATA, ALWAYS. The daily check's cache can be hours old, and a
    # release asset can be replaced in that window - which happened: the cached
    # size described the old exe, the URL served the new one, and the byte
    # count refused a perfectly good download 527 bytes away from its stale
    # expectation. The size check only means something when the size and the
    # download come from the same moment.
    st = check(force=True)
    latest = st.get("latest") or ""
    if not latest or not version.is_newer(latest):
        return {"ok": False, "error": "no newer release to stage"}
    rel = next((r for r in st.get("releases", [])
                if r.get("version") == latest), None)
    if not rel or not rel.get("asset_url"):
        return {"ok": False, "error": "the release has no installer attached"}
    with _APPLY_LOCK:
        if _APPLY["state"] in ("downloading", "verifying", "applying"):
            return {"ok": False, "error": f"already {_APPLY['state']}"}
        if _APPLY["state"] == "ready" and \
                _APPLY["staged_version"] == latest and not force:
            return {"ok": True, "already": True}
        _APPLY.update(state="downloading", progress=0.0, error="",
                      staged_version="")
    base = _data_dir() / APPLY_DIR_NAME
    exe_path = base / rel["asset_name"]
    staged = base / "staged"
    try:
        shutil.rmtree(staged, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        # ---- download, with progress the UI can show -----------------------
        req = urllib.request.Request(rel["asset_url"], headers={
            "User-Agent": f"nuarr/{version.VERSION}"})
        want = int(rel.get("asset_size") or 0)
        got = 0
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(exe_path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if want:
                    _set(progress=round(got / want, 3))
        # SIZE IS THE FIRST TRUTH TEST. The packager this replaced shipped a
        # silently truncated payload once; a byte count against what the API
        # promised catches that class outright.
        if want and got != want:
            raise RuntimeError(f"download is {got:,} bytes, "
                               f"the release says {want:,}")
        _set(state="verifying", progress=1.0)
        # ---- lift the zip out of the installer -----------------------------
        with open(exe_path, "rb") as f:
            f.seek(-16, 2)
            tail = f.read(16)
            if tail[8:] != b"NUARRSFX":
                raise RuntimeError("installer payload marker missing")
            zip_start = int.from_bytes(tail[:8], "little")
            f.seek(0, 2)
            zip_len = f.tell() - 16 - zip_start
            f.seek(zip_start)
            zpath = base / "payload.zip"
            with open(zpath, "wb") as z:
                left = zip_len
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        raise RuntimeError("payload ended early")
                    z.write(chunk)
                    left -= len(chunk)
        # ---- unpack ONLY the program tree ----------------------------------
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist()
                     if n.replace("\\", "/").startswith("program/")]
            if not names:
                raise RuntimeError("no program/ tree in the payload")
            z.extractall(staged, members=names)
        os.remove(zpath)
        os.remove(exe_path)
        # ---- the bundle's config TEMPLATE must never reach the install -----
        # program\config.yml in a release is the placeholder the installer
        # writes real values into. Left in the staged tree, the apply robocopy
        # would lay it over the user's actual config - Plex token, libraries,
        # everything - which the first sandbox run of this feature did.
        # Removed here AND excluded in the helper's robocopy: either alone is
        # one edit away from the worst bug this feature can have.
        (staged / "program" / "config.yml").unlink(missing_ok=True)
        # ---- the staged tree must SAY it is the version we asked for -------
        vfile = staged / "program" / "app" / "version.py"
        staged_ver = ""
        for line in vfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION"):
                staged_ver = line.split('"')[1]
                break
        if staged_ver != latest:
            raise RuntimeError(f"staged tree is {staged_ver!r}, "
                               f"expected {latest!r}")
        _set(state="ready", staged_version=latest)
        joblog.log(f"update {latest} downloaded and verified - "
                   f"ready to install", "ok")
        return {"ok": True, "staged": latest}
    except Exception as e:                                   # noqa: BLE001
        shutil.rmtree(staged, ignore_errors=True)
        try:
            os.remove(exe_path)
        except OSError:
            pass
        _set(state="error", error=f"{type(e).__name__}: {e}")
        joblog.log(f"update stage failed: {e}", "warn")
        return {"ok": False, "error": str(e)}


def apply_staged() -> dict:
    r"""Swap the staged build in, by dying correctly.

    A running process cannot safely replace its own program folder, so the
    swap is done by a detached PowerShell helper: it stops the scheduled
    task (which takes this process down), robocopies the staged tree over
    the install, and starts the task again. Either every file copies and
    the new version boots, or robocopy fails loudly and the OLD install is
    still there to restart - the staged tree is never half-applied over a
    running copy.
    """
    import os
    import subprocess
    st = apply_status()
    if st["state"] != "ready":
        return {"ok": False, "error": f"nothing staged (state={st['state']})"}
    base = _data_dir() / APPLY_DIR_NAME
    src = base / "staged" / "program"
    if not (src / "app" / "version.py").exists():
        _set(state="error", error="staged tree has gone missing")
        return {"ok": False, "error": "staged tree has gone missing"}
    root = _install_root()
    # NUARR_TASK: 'nuarr' on a real install. The sandbox harness sets it to
    # 'none' so the helper relaunches the copy it just updated directly
    # instead of poking the production scheduled task from a test.
    task = os.environ.get("NUARR_TASK", "nuarr")
    pid = os.getpid()
    py = os.environ.get("NUARR_PYTHON", "") or "python"
    data_env = os.environ.get("NUARR_DATA", "")
    helper = base / "apply-update.ps1"
    # Environment the relaunched copy needs when there is no scheduled task to
    # carry it (sandbox / source runs): without NUARR_DATA the new instance
    # would come up pointed at the DEFAULT data directory - somebody else's
    # database.
    relaunch = (
        f"schtasks /Run /TN {task} 2>&1 | Out-Null" if task != "none" else
        ("$env:NUARR_TASK = 'none'\n"
         + (f"$env:NUARR_DATA = '{data_env}'\n" if data_env else "")
         + f'Start-Process "{py}" -ArgumentList '
           f'"{os.path.join(root, "launch.py")}" '
           f'-WorkingDirectory "{root}" -WindowStyle Hidden'))
    helper.write_text(f"""
$ErrorActionPreference = 'Continue'
Start-Transcript -Path "{base / 'apply.log'}" -Force | Out-Null
Start-Sleep -Seconds 1
{f"schtasks /End /TN {task} 2>&1 | Out-Null" if task != "none" else ""}
# wait for the server process to be gone, up to 30s
for ($i=0; $i -lt 60; $i++) {{
  if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ break }}
  Start-Sleep -Milliseconds 500
}}
robocopy "{src}" "{root}" /E /XF config.yml /NFL /NDL /NJH /NJS /NP /R:2 /W:2
if ($LASTEXITCODE -ge 8) {{
  Write-Output "robocopy failed with $LASTEXITCODE - old install left in place"
}} else {{
  Remove-Item "{base / 'staged'}" -Recurse -Force -ErrorAction SilentlyContinue
}}
{relaunch}
Stop-Transcript | Out-Null
""", encoding="utf-8")
    _set(state="applying")
    joblog.log(f"installing update {st['staged_version']} - nuarr will "
               f"restart", "warn")
    # THE HELPER MUST NOT BE OUR CHILD. A plain Popen makes it one, and a
    # child dies with its parent's process tree: on a real install
    # `schtasks /End` kills the task's whole tree - helper included, half a
    # second before it was going to do the swap - and in the sandbox os._exit
    # takes it down the same way. Found exactly so: staged tree verified,
    # server exited cleanly, nothing swapped, no log. Win32_Process.Create
    # parents the helper to the WMI service instead, outside our tree and
    # outside the scheduled task's job, so it survives our death - which is
    # the entire point of its existence.
    hcmd = (f'powershell -NoProfile -ExecutionPolicy Bypass '
            f'-WindowStyle Hidden -File "{helper}"')
    spawn = (f"(Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
             f"-Arguments @{{CommandLine='{hcmd}'}}).ReturnValue")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", spawn],
            creationflags=0x08000000, capture_output=True, text=True,
            timeout=60)
        if r.stdout.strip().splitlines()[-1:] != ["0"]:
            raise RuntimeError(f"WMI create returned {r.stdout.strip()!r} "
                               f"{r.stderr.strip()!r}")
    except Exception as e:                                   # noqa: BLE001
        _set(state="ready", error="")
        joblog.log(f"could not launch the update helper: {e}", "warn")
        return {"ok": False, "error": f"could not launch the update helper: {e}"}
    if task == "none":
        # No task to /End us - exit under our own power once the response has
        # had a moment to flush.
        threading.Timer(2.0, lambda: os._exit(0)).start()
    return {"ok": True, "installing": st["staged_version"]}


def _system_idle() -> bool:
    """No jobs running and nobody watching - the same bar ffmpeg updates use."""
    try:
        from . import jobs
        if jobs.RUNNING:
            return False
    except Exception:                                        # noqa: BLE001
        return False
    try:
        from . import gate
        if gate.plex_playing():
            return False
    except Exception:                                        # noqa: BLE001
        pass
    return True


def _mode() -> str:
    try:
        from .config import SETTINGS
        m = (getattr(SETTINGS, "update_mode", "") or "").strip().lower()
    except Exception:                                        # noqa: BLE001
        m = ""
    return m if m in ("auto", "manual") else "manual"


async def watch() -> None:
    """Background loop. Silent when there is no repo to ask about."""
    import asyncio

    from . import schedules
    # ON THE JOBS PAGE LIKE EVERY OTHER RECURRING JOB. This loop ran from the
    # start, but nothing declared it, so "when does nuarr phone GitHub" could
    # only be answered by reading the source - the exact gap the schedules
    # registry exists to close. beat() fires only when a check actually goes
    # to the network, so last-run is an observation, not the 60-second tick.
    schedules.register(
        "updates", "Update check", "System", CHECK_EVERY_S,
        what="Asks GitHub once a day whether a newer nuarr release exists. "
             "Never installs on its own: auto mode only downloads and "
             "verifies during a quiet stretch, and installing is always "
             "your click - from the power menu or Settings → Updates.")
    while True:
        try:
            if configured():
                before = _STATE.get("latest", "")
                fetched_before = _STATE.get("checked_at") or 0.0
                st = await asyncio.to_thread(check)
                if (st.get("checked_at") or 0.0) > fetched_before:
                    schedules.beat("updates",
                                   ("newest: " + st["latest"]) if st.get("latest")
                                   else "no releases found")
                after = st.get("latest", "")
                # Logged only on a CHANGE. A line every six hours saying the
                # version is the same one it was six hours ago is the kind of
                # entry that pushes real events off the end of the log.
                if after and after != before and version.is_newer(after):
                    joblog.log(f"update available: {after} "
                               f"(running {version.VERSION})", "info")
                # AUTO MODE STAGES WHILE IDLE - it does not install. The
                # download and verify are the safe, interruptible part;
                # doing them during a quiet stretch means the day someone
                # clicks Install, the answer is seconds, not a 200 MB wait.
                # Idle must HOLD for IDLE_STAGE_S before the download starts,
                # so a gap between two encodes does not trigger it.
                if _mode() == "auto" and st.get("update_available"):
                    ap = apply_status()
                    if ap["state"] in ("idle", "error") or (
                            ap["state"] == "ready"
                            and ap["staged_version"] != st.get("latest")):
                        if _system_idle():
                            if not _IDLE_SINCE[0]:
                                _IDLE_SINCE[0] = time.time()
                            elif time.time() - _IDLE_SINCE[0] >= IDLE_STAGE_S:
                                await asyncio.to_thread(stage)
                                _IDLE_SINCE[0] = 0.0
                        else:
                            _IDLE_SINCE[0] = 0.0
        except Exception:                                    # noqa: BLE001
            pass
        await asyncio.sleep(60)      # a minute of granularity for the idle
                                     # clock; check() still gates the network
