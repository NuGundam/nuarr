r"""
nuarr - ffmpeg version check and installer

WHY
---
nuarr currently runs Tdarr's bundled binary:

    C:\Tdarr\Tdarr_Node\assets\app\ffmpeg\win32_x64\ffmpeg.exe

That is a dependency on software being retired, it pins whatever version Tdarr
shipped, and it made a diagnosis harder once already - a process list full of
"C:\Tdarr\...\ffmpeg.exe" reads as Tdarr transcoding when it is actually nuarr.
This gives nuarr its own copy under its own data directory and keeps it current.

SOURCE
------
gyan.dev, the build linked from ffmpeg.org's Windows download page. Chosen over
the alternatives because it publishes:
    /release-version                     plain text, e.g. "8.1.2"
    /ffmpeg-release-essentials.zip       the build
    /ffmpeg-release-essentials.zip.sha256  a checksum to verify it

The checksum is the deciding factor. Downloading an executable without one is
not something to automate.

SAFETY
------
  * check-only by default; installing is an explicit action
  * SHA-256 verified before anything is unpacked
  * the new binary must answer `-version` before it is adopted
  * the previous build is kept, so a bad release can be rolled back
  * nothing is ever written over the running binary in place
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

import httpx

from .config import DATA_DIR, NO_WINDOW, SETTINGS
from . import schedules
from .db import kv_get, kv_set
from . import joblog

BASE = "https://www.gyan.dev/ffmpeg/builds"
VERSION_URL = f"{BASE}/release-version"
ZIP_URL = f"{BASE}/ffmpeg-release-essentials.zip"
SHA_URL = f"{BASE}/ffmpeg-release-essentials.zip.sha256"
# FFmpeg's own changelog, tagged per release
CHANGELOG_URL = "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n{ver}/Changelog"

INSTALL_DIR = DATA_DIR / "ffmpeg"          # C:\ProgramData\nuarr\ffmpeg
BIN_DIR = INSTALL_DIR / "bin"
BACKUP_DIR = INSTALL_DIR / "previous"
_VER_RE = re.compile(r"ffmpeg version (\S+)")

# Live progress for the staging run. A 160 MB download followed by a checksum
# of the same 160 MB is a minute or more of apparent silence, and silence is
# indistinguishable from a hang - so report every phase, not just the download.
PROGRESS: dict = {
    "active": False, "phase": "", "version": "",
    "bytes": 0, "total": 0, "bps": 0.0, "eta_s": None,
    "started": 0.0, "finished": 0.0, "error": "", "ready": False,
}


def _prog(**kw) -> None:
    PROGRESS.update(kw)


def progress() -> dict:
    """Staging progress plus what is waiting to be applied."""
    p = dict(PROGRESS)
    p["staged"] = staged()
    p["pct"] = round(p["bytes"] / p["total"] * 100, 1) if p.get("total") else 0.0
    return p


def pinned_dir() -> str:
    """A build deliberately pinned by the user, or ''.

    Exists because "newest" is not always "works". Current gyan builds require
    NVENC API 13.1 (driver 610.00+); on an older driver they still run, but
    hevc_nvenc refuses to open and every re-encode fails while stream copies
    carry on regardless - so the fault hides until something needs the GPU.
    Pinning lets nuarr keep checking for updates while USING a build that this
    machine can actually encode with.
    """
    return (kv_get("ffmpeg.pinned_dir") or "").strip()


def set_pin(path: str | None) -> dict:
    """Pin (or unpin, with None/'') the directory ffmpeg is used from."""
    if not path:
        kv_set("ffmpeg.pinned_dir", "")
        return {"ok": True, "pinned": None}
    d = os.path.abspath(path)
    ff, fp = os.path.join(d, "ffmpeg.exe"), os.path.join(d, "ffprobe.exe")
    if not (os.path.exists(ff) and os.path.exists(fp)):
        return {"ok": False, "error": f"ffmpeg.exe and ffprobe.exe not found in {d}"}
    kv_set("ffmpeg.pinned_dir", d)
    return {"ok": True, "pinned": d, "version": local_version(ff)}


def installed_paths() -> tuple[str, str]:
    """The ffmpeg/ffprobe nuarr should use.

    Order: an explicit pin, then our own downloaded build, then the configured
    fallback. The pin comes first deliberately - it is the answer to "the newest
    build does not work on this box".
    """
    pin = pinned_dir()
    if pin:
        ff, fp = os.path.join(pin, "ffmpeg.exe"), os.path.join(pin, "ffprobe.exe")
        if os.path.exists(ff) and os.path.exists(fp):
            return ff, fp
    ff = BIN_DIR / "ffmpeg.exe"
    fp = BIN_DIR / "ffprobe.exe"
    if ff.exists() and fp.exists():
        return str(ff), str(fp)
    return SETTINGS.ffmpeg, SETTINGS.ffprobe


# --- NVENC self-test --------------------------------------------------------
# Cached because it spawns a process; refreshed whenever the binary changes.
NVENC: dict = {"checked_at": 0.0, "ok": None, "error": "", "exe": "",
               "driver": "", "cause": {}}


# --- what driver is available, not just what is installed -------------------
# upgrade_safety() can say "ffmpeg 9.x needs 610.00 and you have 596.72", but
# not whether a 610 driver EXISTS yet - which is the difference between "update
# your driver" and "wait, there is nothing to update to".
#
# NVIDIA's only usable endpoint here is the undocumented AjaxDriverService the
# driver-download page calls. Its product ids are not published and the
# enumeration functions it used to expose now 404, so the RTX A-series branch
# cannot be selected reliably - every id combination tried fell back to the
# consumer branch. Rather than guess and report a number for the wrong card,
# this reports WHICH BRANCH it actually got and lets the UI say so.
#
# That is still the useful half: NVENC requirements are expressed as driver
# VERSION numbers, and the consumer and professional branches share one
# numbering space, so "610.88 exists" is real evidence that the 610 series has
# shipped - even when the exact professional build cannot be confirmed here.
_NV_LOOKUP = ("https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/"
              "services/AjaxDriverService.php")
# Product ids read off NVIDIA's own driver picker (the selects are
# manualSearch-0/1/2): type 3 = "NVIDIA RTX PRO / RTX / Quadro",
# series 122 = "NVIDIA RTX Series", product 943 = "NVIDIA RTX A5000".
NV_PSID = 122
NV_PFID = 943
NV_OSID = 57                       # Windows 10/11 64-bit
NV_RESULTS_URL = "https://www.nvidia.com/en-us/drivers/"
_LATEST_DRV: dict = {"at": 0.0, "data": None}
_LATEST_TTL = 6 * 3600


def latest_driver(force: bool = False) -> dict:
    """Newest driver NVIDIA's lookup service reports for this card.

    WITH A STALENESS GUARD, because the answer is otherwise dangerously wrong.
    AjaxDriverService is the endpoint NVIDIA's own download page used to call,
    and for GeForce parts it is current - but for the professional line it is
    frozen: asked about the RTX A5000 it returns Release 470 (475.14, Jul 2024)
    while NVIDIA's site lists 596.86 and 610.88 for the same card.

    An earlier version of this queried the CONSUMER ids instead and reported
    610.88 as "available for your card". The number happened to match the
    A5000's New Feature Branch, so it looked right - it was luck. A driver
    figure older than the one already installed is proof the source is not
    authoritative, so it is reported as unreliable rather than used.
    """
    import urllib.parse

    if (not force and _LATEST_DRV["data"]
            and time.time() - _LATEST_DRV["at"] < _LATEST_TTL):
        return dict(_LATEST_DRV["data"])
    out: dict = {"ok": False, "version": "", "name": "", "released": "",
                 "stale": False, "error": "", "checked_at": time.time(),
                 "url": NV_RESULTS_URL}
    try:
        import httpx
        r = httpx.get(_NV_LOOKUP, params={
            "func": "DriverManualLookup",
            "psid": NV_PSID, "pfid": NV_PFID, "osID": NV_OSID,
            "languageCode": 1033, "numberOfResults": 1}, timeout=20)
        r.raise_for_status()
        ids = (r.json() or {}).get("IDS") or []
        if not ids:
            out["error"] = "no driver returned for this card"
        else:
            d = ids[0].get("downloadInfo") or {}
            out.update(ok=True,
                       version=str(d.get("Version") or ""),
                       name=urllib.parse.unquote(d.get("Name") or ""),
                       released=str(d.get("ReleaseDateTime") or ""))
            try:
                have = float(".".join((_driver_version() or "0").split(".")[:2]))
                got = float(".".join((out["version"] or "0").split(".")[:2]))
                if got and have and got < have:
                    out["stale"] = True
                    out["error"] = (f"NVIDIA's lookup service returns "
                                    f"{out['version']} for this card, older "
                                    f"than the {_driver_version()} already "
                                    f"installed - it is not current for the "
                                    f"professional driver line")
            except ValueError:
                pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    _LATEST_DRV.update(at=time.time(), data=out)
    return dict(out)


def driver_outlook() -> dict:
    """Installed vs available vs what each ffmpeg generation needs.

    Answers the question the ffmpeg tab could not: "if I update my driver,
    which ffmpeg builds become usable?"
    """
    have = _driver_version()
    latest = latest_driver()

    def as_f(v):
        try:
            return float(".".join((v or "0").split(".")[:2]))
        except ValueError:
            return 0.0

    # A stale lookup must not drive the verdict - see latest_driver().
    usable = latest.get("ok") and not latest.get("stale")
    have_f = as_f(have)
    new_f = as_f(latest.get("version")) if usable else 0.0
    # _NVENC_REQ is a list of (major, nvenc_api, min_driver) tuples, newest
    # first. Sorted ascending here so the table reads oldest-to-newest.
    gens = []
    for major, nvenc, need in sorted(_NVENC_REQ):
        need = float(need)
        gens.append({
            "ffmpeg": f"{major}.x", "nvenc": nvenc, "needs": need,
            "works_now": bool(have_f and have_f >= need),
            "works_after_update": bool(new_f and new_f >= need),
        })
    blocked = [g for g in gens if not g["works_now"]]
    unlocked = [g for g in blocked if g["works_after_update"]]
    return {
        "installed": have, "installed_f": have_f,
        "latest": latest,
        "generations": gens,
        "would_unlock": [g["ffmpeg"] for g in unlocked],
        # The one-line verdict.
        "lookup_usable": bool(usable),
        "verdict": (
            "up to date for every supported ffmpeg build" if not blocked else
            (f"updating to {latest.get('version')} would allow ffmpeg "
             + ", ".join(g["ffmpeg"] for g in unlocked)) if unlocked else
            # No usable feed: say what is REQUIRED rather than inventing a
            # conclusion about what is available.
            (f"ffmpeg {', '.join(g['ffmpeg'] for g in blocked)} needs driver "
             f"{min(g['needs'] for g in blocked):.2f}+ — check NVIDIA for a "
             f"build that meets it" if not usable else
             f"no published driver yet meets the "
             f"{min(g['needs'] for g in blocked):.2f} requirement")),
    }


_DRIVER: dict = {"at": 0.0, "v": ""}


def _driver_version(max_age_s: float = 3600.0) -> str:
    """The NVIDIA driver version, cached.

    This shells out to nvidia-smi, which takes the better part of a second, and
    it was being called on every upgrade_safety() - i.e. on every poll of the
    ffmpeg endpoints - to answer a question whose answer only changes when
    somebody installs a driver. Caching it is the difference between the header
    settling immediately and settling nine seconds later.
    """
    if _DRIVER["v"] and (time.time() - _DRIVER["at"]) < max_age_s:
        return _DRIVER["v"]
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=driver_version",
                            "--format=csv,noheader"], capture_output=True,
                           timeout=10, creationflags=NO_WINDOW)
        v = r.stdout.decode("utf-8", "replace").strip().splitlines()[0]
    except Exception:
        v = ""
    if v:
        _DRIVER.update(at=time.time(), v=v)
    return v


# WHY hevc_nvenc REFUSED TO OPEN, in words.
#
# The first failure here was a driver-too-old mismatch, and reporting its raw
# string happened to be readable. Most of the others are not: "Generic error in
# an external library" or "OpenEncodeSessionEx failed" tell you nothing about
# what to do. Each entry is (marker, short reason, what it means / what to do).
# The raw text is always kept alongside, so an unrecognised failure degrades to
# exactly what we had before rather than to nothing.
_NVENC_CAUSES: list[tuple[str, str, str]] = [
    ("required nvenc api", "driver too old",
     "this ffmpeg build needs a newer NVIDIA driver than the machine has - "
     "update the driver, or pin an older ffmpeg build"),
    ("minimum required nvidia driver", "driver too old",
     "this ffmpeg build needs a newer NVIDIA driver than the machine has"),
    ("cannot load nvcuda", "no NVIDIA driver",
     "the CUDA runtime is not present - the driver is missing, broken, or the "
     "GPU is not visible to this session"),
    ("no nvenc capable devices", "no NVENC hardware",
     "no GPU with an encoder was found; some cards and most laptop iGPUs have "
     "none"),
    ("nvenc not available", "NVENC unavailable",
     "the driver reports no encoder - check the card supports NVENC"),
    ("out of memory", "GPU out of memory",
     "the card had no free memory for an encode session; something else is "
     "using the GPU"),
    ("no free encoding sessions", "all NVENC sessions in use",
     "consumer cards cap concurrent encodes - lower the encode worker count, "
     "or something else (Plex?) is holding the sessions"),
    ("sessions per gpu", "NVENC session limit",
     "the per-GPU concurrent-encode limit was hit - lower encode workers"),
    ("frame dimensions", "test clip too small",
     "the self-test's own resolution was below the encoder minimum - this is a "
     "nuarr bug, not a GPU fault"),
    ("permission", "permission denied",
     "the account running nuarr cannot reach the GPU; a service running as "
     "SYSTEM or in session 0 often cannot"),
    ("unknown error", "driver rejected the session",
     "the driver refused the encode session without saying why - usually a "
     "driver that needs restarting or reinstalling"),
    ("no such file", "ffmpeg missing",
     "the ffmpeg binary itself could not be run"),
]


def classify_nvenc(err: str) -> dict:
    """Turn an NVENC failure into something actionable."""
    low = (err or "").lower()
    for marker, reason, meaning in _NVENC_CAUSES:
        if marker in low:
            return {"reason": reason, "meaning": meaning, "known": True}
    if not err:
        return {"reason": "unknown", "known": False,
                "meaning": "the encoder did not open and reported nothing"}
    return {"reason": "unrecognised failure", "known": False,
            "meaning": "nuarr does not have a specific explanation for this "
                       "one - the raw ffmpeg error is shown below"}


def nvenc_check(force: bool = False) -> dict:
    r"""Actually ENCODE one frame with hevc_nvenc and report whether it worked.

    nuarr assumed the GPU encoder was available because the binary existed and
    ran -version. It does not follow: hevc_nvenc opens only if the driver
    supports the API the build was compiled against, and when it does not, the
    failure appears as a per-job "ffmpeg exited -40" hours or days later - or
    never, if the queue happens to be all stream copies. On this box 192
    encodes succeeded, nuarr switched to its own ffmpeg build, and the next
    re-encode was attempted 30 hours later.

    Two seconds at startup turns that into a plain statement on the dashboard.
    """
    exe = installed_paths()[0]
    if (not force and NVENC["ok"] is not None and NVENC["exe"] == exe
            and time.time() - NVENC["checked_at"] < 3600):
        return dict(NVENC)
    err, ok = "", False
    try:
        r = subprocess.run(
            [exe, "-hide_banner", "-nostdin", "-v", "error",
             # 640x360, not something tiny. NVENC has a minimum frame size and
             # rejects anything under it with "Frame dimensions are less than
             # the minimum supported value" - which a 128x128 probe reported as
             # "NVENC unavailable" on a perfectly working encoder. A self-test
             # that cries wolf is worse than none.
             "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.1",
             "-c:v", "hevc_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=60, creationflags=NO_WINDOW)
        ok = r.returncode == 0
        if not ok:
            msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
            err = next((l for l in msg if "nvenc" in l.lower()), msg[0] if msg else
                       f"exit {r.returncode}")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    NVENC.update(checked_at=time.time(), ok=ok, error=err, exe=exe,
                 driver=_driver_version(),
                 cause=({} if ok else classify_nvenc(err)))
    return dict(NVENC)


# The NVENC API each ffmpeg generation is built against, and the NVIDIA driver
# that provides it. Used to answer "is it safe to take this update" BEFORE
# downloading 160 MB and breaking every encode.
#   ffmpeg 8.x/9.x -> NVENC 13.1 -> driver 610.00+
#   ffmpeg 7.x     -> NVENC 13.0 -> driver 570.00+
_NVENC_REQ = [(9, "13.1", 610.0), (8, "13.1", 610.0), (7, "13.0", 570.0)]


def upgrade_safety(latest: str) -> dict:
    """Would installing `latest` keep GPU encoding working on this driver?

    nuarr had no opinion on this, so the only way to find out was to install it
    and wait for the first re-encode to fail - which on a queue full of stream
    copies took thirty hours to notice.
    """
    drv = _driver_version()
    try:
        drv_f = float(re.match(r"(\d+(?:\.\d+)?)", drv).group(1)) if drv else 0.0
    except Exception:
        drv_f = 0.0
    major = 0
    m = re.match(r"(\d+)", (latest or "").lstrip("nv"))
    if m:
        major = int(m.group(1))
    req = next((r for r in _NVENC_REQ if r[0] == major), None)
    if not req or not drv_f:
        return {"known": False, "driver": drv, "latest": latest}
    _, api, need = req
    return {"known": True, "driver": drv, "latest": latest,
            "needs_driver": need, "nvenc_api": api, "safe": drv_f >= need,
            "why": (f"ffmpeg {major}.x needs NVENC {api} (driver {need:.2f}+); "
                    f"this machine has {drv}")}


def nvenc_startup_check() -> None:
    """Run the probe once at startup and say so, loudly if it failed."""
    res = nvenc_check(force=True)
    if res["ok"]:
        joblog.log(f"NVENC ready — hevc_nvenc opens on {os.path.basename(res['exe'])}"
                   + (f" (driver {res['driver']})" if res["driver"] else ""), "ok")
        return
    cause = res.get("cause") or {}
    joblog.log(f"NVENC IS NOT AVAILABLE ({cause.get('reason', 'unknown')}) — "
               "every re-encode will fail. Stream copies are unaffected.",
               "error")
    if cause.get("meaning"):
        joblog.log(f"  {cause['meaning']}", "error")
    joblog.log(f"  ffmpeg: {res['exe']}", "error")
    if res["driver"]:
        joblog.log(f"  driver: {res['driver']}", "error")
    if res["error"]:
        joblog.log(f"  ffmpeg said: {res['error']}", "error")


_VER_CACHE: dict[tuple, str] = {}


def local_version(exe: str | None = None) -> str:
    """Version string of a binary, or '' if it cannot be run.

    Cached on (path, mtime, size). This SPAWNS A PROCESS, which is not free -
    and check() calls it on every poll of the ffmpeg panel and the header. Run
    straight from a coroutine that was a subprocess launch on the event loop;
    py-spy caught the loop here. The binary only changes when the updater
    replaces it, and the mtime in the key notices when it does.
    """
    exe = exe or installed_paths()[0]
    try:
        st = os.stat(exe)
        key = (exe, st.st_mtime, st.st_size)
    except OSError:
        key = (exe, 0, 0)
    if key in _VER_CACHE:
        return _VER_CACHE[key]
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
        m = _VER_RE.search(r.stdout.decode("utf-8", "replace"))
        v = m.group(1) if m else ""
    except Exception:
        v = ""
    _VER_CACHE[key] = v
    return v


def _norm(v: str) -> tuple:
    """Comparable version tuple. '8.1.2' -> (8,1,2); junk sorts lowest."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:4]) or (0,)


async def check() -> dict:
    """Compare the installed build against the latest published one."""
    out: dict = {"checked_at": time.time()}
    cur_exe, _ = installed_paths()
    # to_thread even with the cache: the first call after an update still pays
    # a process spawn, and this runs on the loop.
    out["current"] = await asyncio.to_thread(local_version, cur_exe)
    out["current_path"] = cur_exe
    out["using_tdarr"] = "tdarr" in cur_exe.lower()
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(VERSION_URL)
            r.raise_for_status()
            out["latest"] = r.text.strip()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["update_available"] = bool(
        out["latest"] and _norm(out["latest"]) > _norm(out["current"]))
    kv_set("ffmpeg.last_check", json.dumps(
        {k: v for k, v in out.items() if k != "changelog"}, default=str))
    return out


async def changelog(version: str, limit: int = 120) -> str:
    """FFmpeg's own Changelog for a release tag.

    Read-only and separate from installing, so you can see what a release
    contains before deciding to take it.
    """
    ver = (version or "").strip().lstrip("nv")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(CHANGELOG_URL.format(ver=ver))
            if r.status_code != 200:
                return f"(no changelog published for {ver})"
            lines = r.text.splitlines()
    except Exception as e:
        return f"(changelog unavailable: {type(e).__name__}: {e})"
    # the file is newest-first; the current release's block runs until the
    # next "version X:" heading
    out: list[str] = []
    started = False
    for ln in lines:
        if re.match(r"^version ", ln):
            if started:
                break
            started = True
        if started:
            out.append(ln.rstrip())
        if len(out) >= limit:
            break
    return "\n".join(out) or "(changelog format not recognised)"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def stage(confirm: bool = False, force: bool = False) -> dict:
    """Download and VERIFY a build without touching the live one.

    Split from the swap deliberately. Downloading 160 MB and checksumming it
    is slow but completely inert - no running job is affected, because nothing
    the jobs use is written to. Only the final move matters, and that is
    seconds. So do the slow, risky part now and the disruptive part when it is
    safe.
    """
    info = await check()
    if info.get("error"):
        return {"ok": False, "error": info["error"]}
    if not confirm:
        return {"ok": True, "dry_run": True, "would_stage": info.get("latest"),
                "current": info.get("current"),
                "message": "call again with confirm=true to download"}
    # Do not re-download a version that is already live. Pressing the button
    # twice staged an identical build, which apply_when_idle would then swap
    # in - moving the working copy to 'previous' and installing the same thing.
    if not info.get("update_available") and not force:
        return {"ok": False, "noop": True, "already": info.get("current"),
                "error": f"{info.get('current')} is already installed - "
                         f"pass force=true to download it again"}

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    tmp_zip = str(INSTALL_DIR / "download.zip")
    staging = INSTALL_DIR / "staging"
    _prog(active=True, phase="starting", version=info.get("latest", ""),
          bytes=0, total=0, bps=0.0, eta_s=None, started=time.time(),
          finished=0.0, error="", ready=False)
    try:
        async with httpx.AsyncClient(timeout=900, follow_redirects=True) as c:
            _prog(phase="fetching checksum")
            want = (await c.get(SHA_URL)).text.strip().split()[0].lower()
            joblog.log(f"ffmpeg {info['latest']}: downloading (jobs unaffected)…",
                       "info")
            _prog(phase="downloading")
            t0 = time.time()
            done = 0
            with open(tmp_zip, "wb") as f:
                async with c.stream("GET", ZIP_URL) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    _prog(total=total)
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        f.write(chunk)
                        done += len(chunk)
                        el = time.time() - t0
                        bps = done / el if el > 0.5 else 0.0
                        _prog(bytes=done, bps=bps,
                              eta_s=((total - done) / bps) if bps and total else None)

        # Hashing 160 MB takes seconds and used to look like a stall right at
        # the point where the download bar had just hit 100%.
        _prog(phase="verifying checksum", eta_s=None, bps=0.0)
        got = await asyncio.to_thread(_sha256, tmp_zip)
        if got.lower() != want:
            os.remove(tmp_zip)
            _prog(active=False, phase="failed", error="SHA-256 mismatch",
                  finished=time.time())
            return {"ok": False, "error": "SHA-256 mismatch - download discarded",
                    "expected": want, "got": got}
        joblog.log(f"ffmpeg {info['latest']}: checksum verified", "ok")

        _prog(phase="unpacking")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_unpack, tmp_zip, str(staging))
        os.remove(tmp_zip)

        new_bin = _find_bin(str(staging))
        if not new_bin:
            _prog(active=False, phase="failed", error="no ffmpeg.exe in archive",
                  finished=time.time())
            return {"ok": False, "error": "no ffmpeg.exe inside the archive"}
        _prog(phase="testing the binary")
        ver = local_version(os.path.join(new_bin, "ffmpeg.exe"))
        if not ver:
            _prog(active=False, phase="failed", error="downloaded ffmpeg did not run",
                  finished=time.time())
            return {"ok": False, "error": "the downloaded ffmpeg did not run"}

        kv_set("ffmpeg.staged_version", ver)
        kv_set("ffmpeg.staged_bin", new_bin)
        _prog(active=False, phase="ready to apply", version=ver, ready=True,
              finished=time.time(), eta_s=None)
        joblog.log(f"ffmpeg {ver} staged and verified - will be applied when "
                   f"the queue is idle", "ok")
        return {"ok": True, "staged": ver, "bin": new_bin,
                "message": "verified and waiting; applies when no job is running"}
    except Exception as e:
        _prog(active=False, phase="failed", error=f"{type(e).__name__}: {e}",
              finished=time.time())
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def verify_install() -> dict:
    """Is the installed build actually usable?

    Checks the two things that matter and are cheap: both binaries exist, and
    both answer -version. A truncated download, a half-finished swap or an
    antivirus quarantine all show up here - and all of them otherwise surface
    as every job failing at once with an obscure error.
    """
    # Check nuarr's OWN install directly, not installed_paths().
    #
    # installed_paths() falls back to the configured binary the moment either
    # of ours is missing - good for keeping jobs alive, useless for a health
    # check: deleting ffprobe.exe made verify report "healthy", because it had
    # quietly switched to Tdarr's copy and verified THAT. A silent fallback
    # after we installed our own build is itself the fault worth reporting.
    own_ff, own_fp = str(BIN_DIR / "ffmpeg.exe"), str(BIN_DIR / "ffprobe.exe")
    have_own_dir = BIN_DIR.exists()
    ff, fp = (own_ff, own_fp) if have_own_dir else installed_paths()

    out = {"ffmpeg": ff, "ffprobe": fp,
           "own_build": have_own_dir,
           "ffmpeg_exists": os.path.exists(ff),
           "ffprobe_exists": os.path.exists(fp)}
    out["ffmpeg_version"] = local_version(ff) if out["ffmpeg_exists"] else ""
    if out["ffprobe_exists"]:
        try:
            r = subprocess.run([fp, "-version"], capture_output=True, timeout=15,
                               creationflags=NO_WINDOW)
            out["ffprobe_runs"] = r.returncode == 0
        except Exception:
            out["ffprobe_runs"] = False
    else:
        out["ffprobe_runs"] = False
    out["healthy"] = bool(out["ffmpeg_exists"] and out["ffprobe_exists"]
                          and out["ffmpeg_version"] and out["ffprobe_runs"])
    # If our install is broken, say WHAT jobs are actually running instead.
    if have_own_dir and not out["healthy"]:
        out["falling_back_to"] = SETTINGS.ffmpeg
    if not out["healthy"]:
        problems = []
        if not out["ffmpeg_exists"]:
            problems.append("ffmpeg.exe missing")
        elif not out["ffmpeg_version"]:
            problems.append("ffmpeg.exe will not run")
        if not out["ffprobe_exists"]:
            problems.append("ffprobe.exe missing")
        elif not out["ffprobe_runs"]:
            problems.append("ffprobe.exe will not run")
        out["problem"] = "; ".join(problems) or "unknown"
    return out


def consumers() -> dict:
    r"""Every component that shells out to ffmpeg/ffprobe, and what it resolves to.

    This exists because the first successful install was COSMETIC. The download
    verified, unpacked and installed correctly - and every job carried on
    launching Tdarr's 7.1.4, because jobs.py read SETTINGS.ffmpeg directly
    instead of the resolved path. Nothing in the UI contradicted the
    "installed 8.1.2" message.

    So resolve each consumer the way it does at runtime and compare against the
    expected binary. A mismatch here is the difference between an install that
    worked and one that only appeared to.
    """
    want_ff, want_fp = installed_paths()

    def entry(name: str, what: str, got: str, expect: str, why: str) -> dict:
        ok = bool(got) and os.path.normcase(os.path.abspath(got)) == \
             os.path.normcase(os.path.abspath(expect))
        return {"name": name, "tool": what, "path": got, "expected": expect,
                "ok": ok, "exists": bool(got) and os.path.exists(got),
                "why": why}

    rows: list[dict] = []
    try:
        from . import jobs
        rows.append(entry("Transcoder", "ffmpeg", jobs._ffmpeg_exe(), want_ff,
                          "every encode and remux"))
        rows.append(entry("Probe", "ffprobe", jobs._ffprobe_exe(), want_fp,
                          "stream inspection before planning"))
    except Exception as e:
        rows.append({"name": "Transcoder", "tool": "ffmpeg", "path": "",
                     "expected": want_ff, "ok": False, "exists": False,
                     "why": f"could not resolve: {e}"})
    # The "Handler scripts" rows are gone with the PowerShell subsystem they
    # described - nothing shells out to those scripts any more, so there is no
    # third resolution of ffmpeg left to disagree with the other two.
    try:
        # Language detection decodes short audio windows with ffmpeg and reads
        # durations with ffprobe. It is listed because it is a real consumer -
        # anything that runs the encoder binary belongs in the one table that
        # answers "which build is actually being used, and by what".
        from . import audiolang
        al_ff, al_fp = audiolang._ff()
        rows.append(entry("Audio language", "ffmpeg", al_ff, want_ff,
                          "decodes 30s windows for language detection"))
        rows.append(entry("Audio language", "ffprobe", al_fp, want_fp,
                          "reads duration to place the sample windows"))
    except Exception as e:                               # noqa: BLE001
        rows.append({"name": "Audio language", "tool": "-", "path": "",
                     "expected": want_ff, "ok": False, "exists": False,
                     "why": f"could not resolve: {e}"})

    return {"expected_ffmpeg": want_ff, "expected_ffprobe": want_fp,
            "own_build": BIN_DIR.exists(),
            "fallback_ffmpeg": SETTINGS.ffmpeg,
            "fallback_ffprobe": SETTINGS.ffprobe,
            "all_ok": all(r["ok"] and r["exists"] for r in rows),
            "consumers": rows}


async def repair(confirm: bool = False) -> dict:
    """Re-download the current release and overwrite the install.

    Distinct from an update: it deliberately ignores the version check, because
    the reason to run it is that the files on disk are wrong, not old. Same
    download and SHA-256 verification as any other install - a repair that
    skipped verification would just re-break it differently.

    The swap still waits for an idle queue. A broken ffmpeg means jobs are
    failing anyway, and overwriting the binary underneath a job that happens
    to be working is not an improvement.
    """
    health = verify_install()
    if not confirm:
        return {"ok": True, "dry_run": True, "health": health,
                "message": "call again with confirm=true to re-download and "
                           "overwrite the current build"}
    joblog.log(f"ffmpeg repair requested - "
               f"{'install looks healthy' if health['healthy'] else health.get('problem')}",
               "warn")
    res = await stage(confirm=True, force=True)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "health": health}
    return {"ok": True, "staged": res.get("staged"), "health": health,
            "message": "verified; overwrites the current build when the queue "
                       "is idle"}


def staged() -> dict:
    """What is waiting to be applied, if anything."""
    return {"version": kv_get("ffmpeg.staged_version") or "",
            "bin": kv_get("ffmpeg.staged_bin") or ""}


def apply_staged() -> dict:
    """Swap a staged build in. Fast, and only safe when nothing is running.

    A move of two directories - seconds - but it must not happen underneath a
    job that is about to spawn ffmpeg, so the caller checks for idle first.
    """
    st = staged()
    new_bin = st["bin"]
    if not new_bin or not os.path.isdir(new_bin):
        # A no-op, not a fault: pressing Apply with nothing downloaded has
        # nothing to do. Flagged so the UI can say so quietly instead of
        # painting a red "failed:" for a button that simply had no work.
        return {"ok": False, "noop": True,
                "error": "nothing staged to apply"}
    try:
        if BIN_DIR.exists():
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
            shutil.move(str(BIN_DIR), str(BACKUP_DIR))
        BIN_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(new_bin, str(BIN_DIR))
        shutil.rmtree(INSTALL_DIR / "staging", ignore_errors=True)
        ver = local_version(str(BIN_DIR / "ffmpeg.exe"))
        kv_set("ffmpeg.installed_version", ver)
        kv_set("ffmpeg.installed_at", str(time.time()))
        kv_set("ffmpeg.staged_version", "")
        kv_set("ffmpeg.staged_bin", "")
        _prog(active=False, phase="applied", version=ver, ready=False,
              finished=time.time())
        joblog.log(f"ffmpeg {ver} is now live at {BIN_DIR}", "ok")
        return {"ok": True, "installed": ver, "path": str(BIN_DIR)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def apply_when_idle() -> None:
    """Wait for the queue to drain, then swap the staged build in.

    Runs forever, cheaply. Jobs keep using the old binary until they finish -
    a spawned process holds its own executable, so nothing in flight is
    disturbed either way; this just avoids a new job starting mid-swap.
    """
    from . import jobs

    while True:
        try:
            if staged()["bin"]:
                if not jobs.RUNNING:
                    res = apply_staged()
                    if res.get("ok"):
                        joblog.log("ffmpeg swap complete while the queue was "
                                   "idle - no job was interrupted", "ok")
                    elif res.get("noop"):
                        # nothing staged - the pointer was stale, not broken.
                        # Logging this as an error put a red line in the log
                        # every time the loop raced its own cleanup.
                        kv_set("ffmpeg.staged_bin", "")
                    else:
                        joblog.log(f"ffmpeg swap failed: {res.get('error')}",
                                   "error")
                        kv_set("ffmpeg.staged_bin", "")   # do not loop on it
        except Exception as e:
            joblog.log(f"ffmpeg apply loop: {type(e).__name__}: {e}", "debug")
        await asyncio.sleep(30)


async def install(confirm: bool = False) -> dict:
    """Download and adopt the latest build - SAFELY.

    This used to do its own download and then move BIN_DIR aside immediately,
    with no check for running jobs. That is the exact hazard the stage/apply
    split was built to remove, and leaving this endpoint routed meant a single
    POST could yank the binary directory out from under a live encode. On
    Windows the running image is locked, so the move raises mid-way and can
    leave the install half-swapped - worse than either outcome.

    It now delegates: stage() downloads and verifies into staging (jobs
    unaffected), then the swap happens only when the queue is idle, either
    right here or by apply_when_idle() shortly after.
    """
    from . import jobs

    res = await stage(confirm=confirm)
    if not confirm or not res.get("ok"):
        return res

    if jobs.RUNNING:
        return {"ok": True, "staged": res.get("staged") or res.get("version"),
                "applied": False, "running_jobs": len(jobs.RUNNING),
                "message": "downloaded and verified; it will be applied "
                           "automatically as soon as the queue is idle"}

    applied = apply_staged()
    if not applied.get("ok"):
        return {"ok": True, "staged": res.get("staged") or res.get("version"),
                "applied": False, "error": applied.get("error"),
                "message": "staged, but the swap failed - it will be retried "
                           "when the queue is idle"}
    return {"ok": True, "applied": True, "installed": applied.get("installed"),
            "path": applied.get("path"), "previous_kept": str(BACKUP_DIR)}


def _unpack(zip_path: str, dest: str) -> None:
    with zipfile.ZipFile(zip_path) as z:
        for m in z.namelist():
            # refuse path traversal from a crafted archive
            if os.path.isabs(m) or ".." in m.replace("\\", "/").split("/"):
                raise ValueError(f"unsafe path in archive: {m}")
        z.extractall(dest)


def _find_bin(root: str) -> str | None:
    """The bin/ folder holding ffmpeg.exe, wherever the archive nested it."""
    for dirpath, _dirs, files in os.walk(root):
        if "ffmpeg.exe" in files and "ffprobe.exe" in files:
            return dirpath
    return None


def rollback(force: bool = False) -> dict:
    r"""Put the previous build back.

    Two things this did not do before, both of which could leave nuarr with no
    working transcoder at all:

    1. It ran regardless of what was in flight. Moving BIN_DIR while ffmpeg is
       executing from it hits a sharing violation on Windows.
    2. It had no error handling around a TWO-STEP move. If step one (BIN_DIR ->
       broken) succeeded and step two (backup -> BIN_DIR) failed, BIN_DIR was
       simply gone - every subsequent job would fail to spawn, and the only
       copy of the good build was sitting in a folder called "broken".

    Now: refuse while jobs run (force to override), and if the second move
    fails, put the first one back before reporting.
    """
    from . import jobs

    if not BACKUP_DIR.exists():
        return {"ok": False, "noop": True,
                "error": "no previous build kept to roll back to"}
    if jobs.RUNNING and not force:
        return {"ok": False, "noop": True, "error": "jobs are running",
                "running_jobs": len(jobs.RUNNING),
                "message": "wait for the queue to drain, or pass force=true"}

    broken = INSTALL_DIR / "broken"
    shutil.rmtree(broken, ignore_errors=True)
    moved_aside = False
    try:
        if BIN_DIR.exists():
            shutil.move(str(BIN_DIR), str(broken))
            moved_aside = True
        shutil.move(str(BACKUP_DIR), str(BIN_DIR))
    except Exception as e:
        if moved_aside and not BIN_DIR.exists():
            try:
                shutil.move(str(broken), str(BIN_DIR))     # undo step one
            except Exception:
                joblog.log(f"ffmpeg rollback failed AND could not restore "
                           f"{BIN_DIR} - the build is at {broken}", "error")
                return {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "current_build_at": str(broken),
                        "message": "manual recovery needed"}
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "message": "nothing changed"}

    ver = local_version(str(BIN_DIR / "ffmpeg.exe"))
    kv_set("ffmpeg.installed_version", ver)
    joblog.log(f"ffmpeg rolled back to {ver}", "warn")
    return {"ok": True, "version": ver}


async def watch() -> None:
    """Check on a timer and say so. NEVER installs on its own.

    Auto-installing a transcoder underneath running jobs is not a decision to
    make quietly - a regression would silently affect every file processed
    afterwards. So this reports, and installing stays a deliberate action.

    Interval is read live from settings each cycle, so changing it in the UI
    takes effect without a restart.
    """
    await asyncio.sleep(120)
    while True:
        schedules.beat('ffmpeg')
        try:
            from . import workers
            interval_h = float(workers.get().ffmpeg_check_h)
        except Exception:
            interval_h = 24.0
        if interval_h <= 0:
            await asyncio.sleep(3600)     # disabled - re-read hourly
            continue
        try:
            info = await check()
            if info.get("update_available"):
                joblog.log(f"ffmpeg update available: {info['current']} -> "
                           f"{info['latest']} (Settings -> ffmpeg to install)",
                           "warn")
            elif info.get("using_tdarr"):
                joblog.log("ffmpeg is still Tdarr's bundled build - install "
                           "nuarr's own copy to drop that dependency", "warn")
        except Exception as e:
            joblog.log(f"ffmpeg check failed: {type(e).__name__}: {e}", "debug")
        await asyncio.sleep(interval_h * 3600)
