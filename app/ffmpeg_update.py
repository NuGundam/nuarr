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
import platform
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
               "driver": "", "cause": {}, "api": "", "required_api": "",
               "found_api": ""}
# ffmpeg names both versions when it refuses to open the encoder:
#   "Driver does not support the required nvenc API version. Required: 13.1
#    Found: 13.0"
# That is the single most authoritative line available - it is THIS build
# talking about THIS driver - so it is parsed out rather than paraphrased.
_NVENC_VER_RE = re.compile(
    r"Required:\s*(\d+\.\d+).*?Found:\s*(\d+\.\d+)", re.I | re.S)


# --- what driver is available, not just what is installed -------------------
# upgrade_safety() can say "ffmpeg 9.x needs 610.00 and you have 596.72", but
# not whether a 610 driver EXISTS yet - which is the difference between "update
# your driver" and "wait, there is nothing to update to".
#
# NVIDIA's only usable endpoint here is the undocumented AjaxDriverService the
# driver-download page calls. Two query parameters decide whether the answer is
# current or two years stale, and both were wrong here:
#
#   dch    0 (the default when omitted) asks for the LEGACY "Standard" package.
#          For the RTX A-series that line stopped at Release 470 - 475.14, July
#          2024 - which is why this used to report a driver older than the one
#          already installed. dch=1 asks for the DCH package NVIDIA has shipped
#          exclusively since, and returns R595 U8 (597.06, Aug 2026).
#
#   osID   the professional driver is published per Windows edition, and Server
#          is not Windows 10. osID 57 (Windows 10 64-bit) returns the desktop
#          package; Server 2022 is 134 and returns "NVIDIA RTX Server Driver".
#          Asking with the wrong one is how a Server box was told to install a
#          desktop driver. The ids are enumerated from getMenuArrays and are
#          listed in _NV_OSIDS below.
#
# The staleness guard is kept as a backstop: if the service ever returns a
# version older than the installed one again, that is reported rather than used.
_NV_LOOKUP = ("https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/"
              "services/AjaxDriverService.php")
# Product ids read off NVIDIA's own driver picker (the selects are
# manualSearch-0/1/2): type 3 = "NVIDIA RTX PRO / RTX / Quadro",
# series 122 = "NVIDIA RTX Series", product 943 = "NVIDIA RTX A5000".
NV_PSID = 122
NV_PFID = 943
NV_OSID = 57                       # fallback only; _nv_osids() picks the real one
NV_RESULTS_URL = "https://www.nvidia.com/en-us/drivers/"
# osID values NVIDIA lists for this card, keyed by how Windows identifies itself.
# (build number -> id for Server; ProductName carries the edition name.)
_NV_OSIDS = {
    "server 2025": 153, "server 2022": 134, "server 2019": 119,
    "server 2016": 74, "server 2012": 44,
    "11": 135, "10": 57,
}
_LATEST_DRV: dict = {"at": 0.0, "data": None}
_LATEST_TTL = 6 * 3600


def _windows_edition() -> str:
    """ProductName from the registry, e.g. 'Windows Server 2022 Standard'.

    Read in-process with winreg - no PowerShell, no console window.
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            return str(winreg.QueryValueEx(k, "ProductName")[0] or "")
    except Exception:
        return ""


def _nv_osids() -> list:
    """osIDs to try, best guess first, then sensible fallbacks.

    Never returns an empty list: if the edition cannot be read, the desktop
    ids are still tried, which is what the old hard-coded behaviour did.
    """
    name = _windows_edition().lower()
    order = []
    if "server" in name:
        for key in ("server 2025", "server 2022", "server 2019",
                    "server 2016", "server 2012"):
            if key.split()[-1] in name:
                order.append(_NV_OSIDS[key])
                break
        else:
            # A Server edition this map does not name - build number decides.
            try:
                build = int(platform.win32_ver()[1].split(".")[-1])
            except Exception:
                build = 0
            order.append(_NV_OSIDS["server 2025"] if build >= 26100
                         else _NV_OSIDS["server 2022"])
        order += [_NV_OSIDS["server 2022"], _NV_OSIDS["11"]]
    else:
        order += [_NV_OSIDS["11" if "11" in name else "10"], _NV_OSIDS["10"]]
    seen, out = set(), []
    for i in order:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


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
                 "branch": "", "osid": 0, "edition": _windows_edition(),
                 "stale": False, "error": "", "checked_at": time.time(),
                 "url": NV_RESULTS_URL}
    try:
        import httpx
        d, used = {}, 0
        # dch=1 first (the current package); dch=0 only as a last resort, and
        # only for editions where the legacy line was ever published.
        for osid in _nv_osids():
            for dch in (1, 0):
                r = httpx.get(_NV_LOOKUP, params={
                    "func": "DriverManualLookup",
                    "psid": NV_PSID, "pfid": NV_PFID, "osID": osid,
                    "dch": dch, "languageCode": 1033,
                    "numberOfResults": 1}, timeout=20)
                r.raise_for_status()
                ids = (r.json() or {}).get("IDS") or []
                cand = (ids[0].get("downloadInfo") or {}) if ids else {}
                if str(cand.get("Success") or "") == "1" and cand.get("Version"):
                    d, used = cand, osid
                    break
            if d:
                break
        if not d:
            out["error"] = "no driver returned for this card"
        else:
            out.update(ok=True, osid=used,
                       version=str(d.get("Version") or ""),
                       name=urllib.parse.unquote(d.get("Name") or ""),
                       branch=urllib.parse.unquote(
                           d.get("DisplayVersion") or ""),
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

    "works_now" and "works_after_update" are NOT decided the same way, and the
    asymmetry is the point:

      works_now           measured. The driver on this machine is asked which
                          NVENC API it implements, which is the exact condition
                          ffmpeg tests. No inference, no table lookup.
      works_after_update  inferred. A driver that is not installed cannot be
                          asked anything, so this falls back to comparing
                          version numbers against _NVENC_REQ. It is a
                          prediction, and is labelled as one.
    """
    have = _driver_version()
    latest = latest_driver()
    api = nvenc_api()
    have_api = (api["major"], api["minor"]) if api.get("ok") else None

    def as_f(v):
        try:
            return float(".".join((v or "0").split(".")[:2]))
        except ValueError:
            return 0.0

    # A stale lookup must not drive the verdict - see latest_driver().
    usable = latest.get("ok") and not latest.get("stale")
    have_f = as_f(have)
    new_f = as_f(latest.get("version")) if usable else 0.0
    # _NVENC_REQ is a list of (major, (api_major, api_minor), min_driver)
    # tuples, newest first. Sorted ascending here so the table reads
    # oldest-to-newest.
    # Requirement numbers come from NVIDIA's own SDK Read Me when reachable,
    # and from the compiled-in table when not.
    try:
        reqs = sdk_driver_reqs()
    except Exception:
        reqs = _default_reqs()
    gens = []
    for major, nvenc, need in sorted(_NVENC_REQ):
        need = float(reqs.get(f"{nvenc[0]}.{nvenc[1]}", need))
        gens.append({
            "ffmpeg": f"{major}.x", "nvenc": f"{nvenc[0]}.{nvenc[1]}",
            "needs": need,
            # The driver's own answer when we have it; the version number only
            # as a fallback for machines where the DLL could not be read.
            "works_now": (have_api >= nvenc if have_api
                          else bool(have_f and have_f >= need)),
            "measured": bool(have_api),
            "works_after_update": bool(new_f and new_f >= need),
        })
    blocked = [g for g in gens if not g["works_now"]]
    unlocked = [g for g in blocked if g["works_after_update"]]
    return {
        "installed": have, "installed_f": have_f,
        "nvenc_api": api,
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
             # Not a fault of this card: the branch simply does not exist yet.
             f"ffmpeg {', '.join(g['ffmpeg'] for g in blocked)} needs NVENC "
             f"{blocked[0]['nvenc']}, which arrives with driver branch "
             f"R{int(min(g['needs'] for g in blocked))} — NVIDIA has not "
             f"released it for any GPU yet, so there is nothing to update to")),
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
    res = probe_nvenc(exe)
    NVENC.update(checked_at=time.time(), ok=res["ok"], error=res["error"],
                 exe=exe, driver=_driver_version(),
                 api=nvenc_api().get("version", ""),
                 required_api=res["required_api"],
                 found_api=res["found_api"],
                 cause=({} if res["ok"] else classify_nvenc(res["error"])))
    return dict(NVENC)


def probe_nvenc(exe: str) -> dict:
    """Try to open hevc_nvenc with a SPECIFIC ffmpeg binary.

    Takes the path as an argument rather than reading the installed one,
    because the most valuable moment to ask this question is BEFORE a
    downloaded build is adopted - see stage(). A build that cannot encode on
    this driver is exactly what the driver-number table was trying to predict,
    and the staged binary can simply be asked instead.
    """
    err, ok = "", False
    req_api = found_api = ""
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
            raw = r.stderr.decode("utf-8", "replace").strip()
            msg = raw.splitlines()
            err = next((l for l in msg if "nvenc" in l.lower()), msg[0] if msg else
                       f"exit {r.returncode}")
            m = _NVENC_VER_RE.search(raw)
            if m:
                req_api, found_api = m.group(1), m.group(2)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return {"ok": ok, "error": err, "exe": exe,
            "required_api": req_api, "found_api": found_api}


# The NVENC API each ffmpeg generation is built against, and the NVIDIA driver
# that provides it. Used to answer "is it safe to take this update" BEFORE
# downloading 160 MB and breaking every encode.
#   ffmpeg 8.x/9.x -> NVENC 13.1 -> driver 610.00+
#   ffmpeg 7.x     -> NVENC 13.0 -> driver 570.00+
# The API version is the REQUIREMENT; the driver number is only the vehicle
# that delivers it. See nvenc_api() for why that distinction is load-bearing.
_NVENC_REQ = [(9, (13, 1), 610.0), (8, (13, 1), 610.0), (7, (13, 0), 570.0)]

_NVENC_API: dict = {"at": 0.0, "data": None}
_NVENC_API_TTL = 3600.0

# NVIDIA states the requirement in each SDK's Read Me as a bare line:
#     Windows
#       * Driver version 610 and above
# Parsed from there rather than hardcoded, so a new SDK does not need a code
# change - the built-in _NVENC_REQ numbers stay as the offline fallback.
_SDK_README = ("https://docs.nvidia.com/video-technologies/video-codec-sdk/"
               "{ver}/read-me/index.html")
_SDK_DRV_RE = re.compile(r"Driver version\s+(\d+)\s+and above", re.I)
_SDK_REQ_TTL = 7 * 24 * 3600


def sdk_driver_reqs(force: bool = False) -> dict:
    """{'13.0': 570, '13.1': 610, ...} - the driver BRANCH each SDK needs.

    A NOTE ON WHAT THESE NUMBERS ARE, because it is easy to over-trust them.
    They are BRANCH numbers, not version numbers, and NVIDIA's branches do not
    number their releases after themselves:

        RTX Enterprise  R570 -> 572.16 .. 573.96
                        R580 -> 580.88 .. 582.78
                        R595 -> 595.71 .. 597.06
        Data Center     R580 -> 580.178.04 (Linux) but 582.78 (Windows)

    So "installed >= 610" is a reasonable reading of "branch R610 or later"
    WITHIN ONE PRODUCT LINE, and meaningless across lines. That is why this
    only ever feeds the advisory "a newer driver would unlock X" text, and
    never overrides nvenc_api(), which measures the real answer locally.
    """
    cached = kv_get("nvenc.sdk_reqs")
    stamp = float(kv_get("nvenc.sdk_reqs_at") or 0)
    if cached and not force and time.time() - stamp < _SDK_REQ_TTL:
        try:
            return {**_default_reqs(), **json.loads(cached)}
        except Exception:
            pass
    found: dict = {}
    # Ask about the SDKs referenced by _NVENC_REQ plus the next few, so a
    # newly published SDK is picked up without editing this file.
    want = {f"{a}.{b}" for _, (a, b), _ in _NVENC_REQ}
    want |= {"13.2", "14.0", "14.1"}
    for ver in sorted(want):
        try:
            r = httpx.get(_SDK_README.format(ver=ver), timeout=20,
                          follow_redirects=True)
            if r.status_code != 200:
                continue
            m = _SDK_DRV_RE.search(r.text)
            if m:
                found[ver] = int(m.group(1))
        except Exception:
            continue
    if found:
        kv_set("nvenc.sdk_reqs", json.dumps(found))
        kv_set("nvenc.sdk_reqs_at", str(time.time()))
    return {**_default_reqs(), **found}


def _default_reqs() -> dict:
    """The offline fallback: whatever is compiled into _NVENC_REQ."""
    return {f"{a}.{b}": float(need) for _, (a, b), need in _NVENC_REQ}


def _forget_gpu_caches() -> None:
    """Drop every cached answer about the GPU. Called when the driver moves."""
    _DRIVER.update(at=0.0, v="")
    _NVENC_API.update(at=0.0, data=None)
    _LATEST_DRV.update(at=0.0, data=None)
    NVENC.update(checked_at=0.0, ok=None)


async def watch_driver(interval_s: float = 300.0) -> None:
    """Notice a GPU driver change and react to it.

    A driver install swaps the encoder out from under a process that is already
    holding it open, and nuarr had no idea it had happened: every cached answer
    about the GPU - installed version, NVENC API, whether hevc_nvenc opens -
    has an hour-long TTL or longer, so the dashboard kept reporting the old
    driver, and any encode running at the time carried on talking to a driver
    that had been replaced.

    So: poll the version, and when it changes, forget everything cached about
    the GPU, re-measure the API, and hand any in-flight work back to the queue
    (see jobs.cancel_and_requeue - it spares jobs already past the encode).

    The poll costs one nvidia-smi (about a second, NO_WINDOW so nothing
    flashes) every five minutes, which is why the interval is minutes rather
    than seconds. It is deliberately NOT driven off the cached reader: the
    whole point is to look past the cache and see the real value.
    """
    from . import jobs

    await asyncio.sleep(60)
    while True:
        try:
            now = _driver_version(max_age_s=0.0)
            seen = kv_get("nvenc.driver_seen") or ""
            if now and now != seen:
                _forget_gpu_caches()
                api = nvenc_api(force=True)
                kv_set("nvenc.driver_seen", now)
                prev_api = kv_get("nvenc.api_seen") or ""
                kv_set("nvenc.api_seen", api.get("version") or "")
                if not seen:
                    # First run on this install - record the baseline quietly
                    # rather than announcing a change that did not happen.
                    joblog.log(f"GPU driver {now} (NVENC "
                               f"{api.get('version') or '?'})", "debug")
                else:
                    moved = (api.get("version") or "") != prev_api
                    joblog.log(
                        f"NVIDIA driver changed: {seen} -> {now}. NVENC API "
                        + (f"{prev_api} -> {api.get('version')}" if moved
                           else f"unchanged at {api.get('version') or '?'}"),
                        "warn")
                    if not moved and prev_api:
                        # Worth saying plainly: a driver update that does not
                        # move the API unlocks no new ffmpeg build.
                        joblog.log(
                            "the driver moved but the NVENC API did not, so "
                            "the same ffmpeg builds are supported as before",
                            "info")
                    # Re-probe the encoder before anything is requeued, so the
                    # replanned jobs are planned against the new reality.
                    res = nvenc_check(force=True)
                    if not res.get("ok"):
                        joblog.log(
                            f"GPU encoding is NOT working after the driver "
                            f"change: {res.get('error') or 'unknown'}",
                            "error")
                    try:
                        await jobs.cancel_and_requeue(
                            f"NVIDIA driver changed to {now}")
                    except Exception as e:                      # noqa: BLE001
                        joblog.log(f"driver-change requeue failed: "
                                   f"{type(e).__name__}: {e}", "error")
        except Exception as e:                                  # noqa: BLE001
            joblog.log(f"driver watch: {type(e).__name__}: {e}", "debug")
        await asyncio.sleep(interval_s)


def nvenc_api(force: bool = False) -> dict:
    """The NVENC API version THE DRIVER ACTUALLY EXPOSES. Measured, not inferred.

    This is the check that matters, and it was missing. ffmpeg does not refuse
    to open hevc_nvenc because a driver number is low - it refuses because the
    API version the build was compiled against is newer than the one the driver
    implements, and it says so in exactly those terms:

        Driver does not support the required nvenc API version.
        Required: 13.1 Found: 13.0

    Driver numbers were only ever a proxy for that, and a lossy one: NVIDIA
    ships several branches at once, so "596.86 < 610.00" is a guess about what
    596.86 implements, while the driver itself will simply tell you. It exports
    NvEncodeAPIGetMaxSupportedVersion from nvEncodeAPI64.dll, which returns the
    version packed as (major << 4) | minor - 208 means 13.0.

    Reading it costs one LoadLibrary of a DLL the driver installed, so a card
    with no NVIDIA driver fails cleanly at the load rather than misreporting.
    """
    if (not force and _NVENC_API["data"]
            and time.time() - _NVENC_API["at"] < _NVENC_API_TTL):
        return dict(_NVENC_API["data"])
    out = {"ok": False, "major": 0, "minor": 0, "version": "", "error": ""}
    try:
        import ctypes
        dll = ctypes.WinDLL("nvEncodeAPI64.dll")
        raw = ctypes.c_uint32(0)
        st = dll.NvEncodeAPIGetMaxSupportedVersion(ctypes.byref(raw))
        if st != 0:
            out["error"] = f"NvEncodeAPIGetMaxSupportedVersion returned {st}"
        else:
            major, minor = raw.value >> 4, raw.value & 0xF
            out.update(ok=True, major=major, minor=minor,
                       version=f"{major}.{minor}")
    except OSError:
        out["error"] = "no NVIDIA encoder driver on this machine"
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    _NVENC_API.update(at=time.time(), data=out)
    return dict(out)


def upgrade_safety(latest: str) -> dict:
    """Would installing `latest` keep GPU encoding working on this driver?

    nuarr had no opinion on this, so the only way to find out was to install it
    and wait for the first re-encode to fail - which on a queue full of stream
    copies took thirty hours to notice.

    Decided on the NVENC API the driver reports, with the driver number used
    only when that cannot be read. Asking the driver what it implements beats
    inferring it from a version number against a table that goes out of date.
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
    if not req:
        return {"known": False, "driver": drv, "latest": latest}
    _, api, need = req
    have = nvenc_api()
    api_s = f"{api[0]}.{api[1]}"
    if have.get("ok"):
        safe = (have["major"], have["minor"]) >= api
        return {"known": True, "driver": drv, "latest": latest,
                "needs_driver": need, "nvenc_api": api_s,
                "have_api": have["version"], "measured": True, "safe": safe,
                "why": (f"ffmpeg {major}.x needs NVENC {api_s}; this driver "
                        f"({drv or '?'}) implements {have['version']}"
                        + ("" if safe else f" - driver {need:.2f}+ provides it"))}
    if not drv_f:
        return {"known": False, "driver": drv, "latest": latest,
                "why": have.get("error") or "NVENC API version unreadable"}
    return {"known": True, "driver": drv, "latest": latest,
            "needs_driver": need, "nvenc_api": api_s, "measured": False,
            "safe": drv_f >= need,
            "why": (f"ffmpeg {major}.x needs NVENC {api_s} (driver {need:.2f}+); "
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

        # ASK THE BUILD, DO NOT PREDICT IT.
        #
        # Whether a new ffmpeg can still use the GPU was previously inferred
        # from a table of driver numbers - and driver numbers are a poor proxy,
        # because NVIDIA's version numbering is per-branch and per-product-line
        # (the RTX Enterprise R570 branch ships as 572.16-573.96; the Data
        # Center R580 branch is 580.178.04 on Linux but 582.78 on Windows).
        # Comparing a raw number against a threshold only works by luck.
        #
        # The staged binary is sitting on disk and is the exact thing that
        # would run, so it is simply asked to open hevc_nvenc. If it cannot,
        # ffmpeg names both API versions itself and that is recorded verbatim.
        _prog(phase="checking GPU encoding")
        gpu = probe_nvenc(os.path.join(new_bin, "ffmpeg.exe"))
        kv_set("ffmpeg.staged_nvenc_ok", "1" if gpu["ok"] else "0")
        kv_set("ffmpeg.staged_nvenc_why", gpu["error"] or "")
        kv_set("ffmpeg.staged_nvenc_req", gpu["required_api"] or "")

        kv_set("ffmpeg.staged_version", ver)
        kv_set("ffmpeg.staged_bin", new_bin)
        if not gpu["ok"]:
            detail = (f"it needs NVENC {gpu['required_api']} and this driver "
                      f"provides {gpu['found_api']}"
                      if gpu["required_api"] else gpu["error"])
            _prog(active=False, phase="staged - GPU encoding would break",
                  version=ver, ready=True, finished=time.time(), eta_s=None)
            joblog.log(f"ffmpeg {ver} staged, but GPU ENCODING WOULD BREAK: "
                       f"{detail}. It has NOT been applied - every re-encode "
                       f"would fall back or fail.", "error")
            return {"ok": True, "staged": ver, "bin": new_bin,
                    "nvenc_ok": False, "nvenc_why": detail,
                    "requires_api": gpu["required_api"],
                    "have_api": gpu["found_api"] or nvenc_api().get("version"),
                    "message": f"downloaded and verified, but GPU encoding "
                               f"fails on this driver - {detail}"}
        _prog(active=False, phase="ready to apply", version=ver, ready=True,
              finished=time.time(), eta_s=None)
        joblog.log(f"ffmpeg {ver} staged and verified, GPU encoding confirmed "
                   f"working - will be applied when the queue is idle", "ok")
        return {"ok": True, "staged": ver, "bin": new_bin, "nvenc_ok": True,
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


def apply_staged(force: bool = False) -> dict:
    """Swap a staged build in. Fast, and only safe when nothing is running.

    A move of two directories - seconds - but it must not happen underneath a
    job that is about to spawn ffmpeg, so the caller checks for idle first.

    Refuses a build that FAILED the staged NVENC probe. stage() already tried
    to open hevc_nvenc with that exact binary; installing it anyway is choosing
    to break GPU encoding on a known-bad result. `force` is there because
    "CPU-only but newer" is a legitimate choice - just not a silent one.
    """
    if not force and kv_get("ffmpeg.staged_nvenc_ok") == "0":
        why = kv_get("ffmpeg.staged_nvenc_why") or "the GPU encoder would not open"
        return {"ok": False, "blocked": True,
                "error": f"not applied - GPU encoding fails with this build: "
                         f"{why}",
                "requires_api": kv_get("ffmpeg.staged_nvenc_req") or "",
                "message": "pass force=true to install it anyway (encodes "
                           "would fall back to CPU or fail)"}
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
                    elif res.get("blocked"):
                        # Known-bad for GPU encoding. Leave it staged so the
                        # UI can offer the choice, but never adopt it here -
                        # and do not log every 30s about it.
                        pass
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

    # Downloaded fine, but the staged binary could not open the GPU encoder on
    # this driver. Stop here rather than swapping it in - this is the failure
    # that previously took thirty hours to notice, and it is now known before
    # anything is adopted.
    if res.get("nvenc_ok") is False:
        return {"ok": True, "staged": res.get("staged"), "applied": False,
                "nvenc_ok": False, "nvenc_why": res.get("nvenc_why"),
                "requires_api": res.get("requires_api"),
                "have_api": res.get("have_api"),
                "message": res.get("message")}

    if jobs.RUNNING:
        return {"ok": True, "staged": res.get("staged") or res.get("version"),
                "applied": False, "running_jobs": len(jobs.RUNNING),
                "message": "downloaded and verified; it will be applied "
                           "automatically as soon as the queue is idle"}

    applied = apply_staged()
    if not applied.get("ok"):
        return {"ok": True, "staged": res.get("staged") or res.get("version"),
                "applied": False, "error": applied.get("error"),
                "blocked": applied.get("blocked", False),
                "message": (applied.get("message") if applied.get("blocked")
                            else "staged, but the swap failed - it will be "
                                 "retried when the queue is idle")}
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
