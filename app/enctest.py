r"""
nuarr - the encoder test bench

WHAT THIS IS
------------
One page's worth of truth about which encoders work on THIS machine with a
GIVEN ffmpeg build - the installed one, or the newest published one before it
is allowed anywhere near the queue.

WHY IT EXISTS, STATED AS THE RULE IT ENFORCES
---------------------------------------------
    Never predict what can be measured.

The ffmpeg tab used to reason about driver BRANCH numbers ("610.00+") to guess
whether a new build would keep encoding. Branch numbers belong to NVIDIA, they
mean different things per product line and per OS, and they change without
notice - they are not logic nuarr can control or rely on. What nuarr CAN
control is running each encoder for real and reading the result. So this
module encodes two seconds of colour bars through every encoder of every
family, with the exact binary in question, and reports supported /
not-supported with the failure text translated into plain words.

The one prediction left anywhere ("a future driver would unlock X") simply
does not appear on this page. If a build fails here today and someone updates
a driver tomorrow, the Re-test button measures the new truth in under a
minute.

DOWNLOAD, WITH RECOVERY
-----------------------
Testing the latest build means downloading ~160 MB first. Networks drop; a
download that dies at 92% and starts over from zero teaches people not to use
the button. So the fetch resumes: every retry asks the server for the bytes
it does not have yet (HTTP Range), verifies the SHA-256 of the assembled
whole, and the page shows each recovery rather than hiding it. The test build
lands in its own directory - it is NOT staged, NOT applied, and cannot be
picked up by the idle-swap loop. Adopting it is a separate, deliberate
action, and the Adopt endpoint refuses builds whose results say the encoders
in use would break.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from .config import NO_WINDOW, hidden_si
from .db import kv_get, kv_set
from . import joblog

# --------------------------------------------------------------- the matrix --

# Everything tested, in the order it is shown. Built from encoders.FAMILIES so
# a new family lands here automatically, but flattened per-encoder: "NVENC
# works" is not one fact, it is three - h264_nvenc, hevc_nvenc and av1_nvenc
# can and do fail independently (av1_nvenc needs Ada or newer silicon while
# the others run on anything Turing-era).
_CODECS = [("h264", "H.264"), ("hevc", "HEVC"), ("av1", "AV1")]


def _matrix() -> list[dict]:
    from . import encoders
    rows = []
    for fam in encoders.ORDER:
        spec = encoders.FAMILIES[fam]
        for key, label in _CODECS:
            enc = spec.get(key)
            if enc:
                rows.append({"family": fam, "family_label": spec["label"],
                             "codec": label, "encoder": enc})
    return rows


# ------------------------------------------------- plain-language failures --

# (marker in ffmpeg's stderr, short verdict, what it means for this machine).
# Checked in order; first hit wins. The raw text is always kept alongside so
# an unmatched failure still shows something true.
_CAUSES: list[tuple[str, str, str]] = [
    # First entry matches the reworded form _probe_one writes after pulling
    # the two API numbers out of ffmpeg's own message - classification runs
    # on the detail AFTER that rewrite, so the rewrite must classify too.
    ("needs nvenc", "driver too old for this build",
     "the build wants a newer NVENC API than the installed NVIDIA driver "
     "provides - the two versions are in the detail line"),
    ("required nvenc api", "driver too old for this build",
     "the build wants a newer NVENC API than the installed NVIDIA driver "
     "provides - the exact versions are in the detail line below"),
    ("minimum required nvidia driver", "driver too old for this build",
     "this build refuses drivers older than the version it names"),
    ("cannot load nvcuda", "no NVIDIA driver",
     "the NVIDIA driver is missing or broken, so CUDA cannot start"),
    ("no capable devices", "this GPU cannot encode this codec",
     "the card exists but lacks this encoder block - AV1 encoding on NVIDIA "
     "needs an RTX 40-series/Ada or newer card"),
    ("openencodesessionex failed", "GPU encoder refused to open",
     "an NVIDIA card exists but its encoder would not start - commonly the "
     "driver and build disagree, or every encode session is in use"),
    ("no device available for encoder", "hardware not present",
     "no GPU of this vendor is available to ffmpeg on this machine"),
    ("mfx", "no Intel QuickSync",
     "QuickSync needs an Intel iGPU or Arc card with its driver installed; "
     "this machine has none it can reach"),
    ("qsv", "no Intel QuickSync",
     "QuickSync needs an Intel iGPU or Arc card with its driver installed; "
     "this machine has none it can reach"),
    ("failed to create", "hardware not reachable",
     "the encoder exists in the build but its hardware could not be opened"),
    ("amf", "no AMD GPU",
     "AMF needs a Radeon card with the Adrenalin driver; this machine has "
     "none it can reach"),
    ("d3d11va", "no AMD GPU",
     "the Direct3D device AMF encodes through could not be created"),
    ("unknown encoder", "not in this build",
     "this ffmpeg build was compiled without this encoder"),
    ("timed out", "timed out",
     "the encoder did not finish a two-second test in time - the machine "
     "may be under heavy load; re-test when it is quieter"),
]

# ffmpeg names both API versions when NVENC refuses on version grounds;
# capture them so the detail line can say the two numbers that matter.
_API_RE = re.compile(r"Required:\s*(\d+\.\d+).*?Found:\s*(\d+\.\d+)",
                     re.I | re.S)


def _explain(err: str) -> tuple[str, str]:
    """(short verdict, plain meaning) for a raw ffmpeg failure line."""
    low = (err or "").lower()
    for marker, short, meaning in _CAUSES:
        if marker in low:
            return short, meaning
    return "failed", "an unrecognised error - the raw text below is all we know"


def _informative_line(raw: str) -> str:
    """The stderr line that names the CAUSE, not the one that reports the echo.

    ffmpeg fails in two voices at once: the encoder says why ("No capable
    devices found", "Error creating a MFX session") and then the muxer adds
    that nothing arrived ("Nothing was written into output file..."). The
    muxer line comes LAST, so taking the tail - the obvious implementation,
    and the first one here - reported the echo for every hardware failure and
    the page said "failed" twelve different unhelpful ways. Prefer the first
    line a cause marker matches; failing that, the first line that is not the
    muxer echo; the tail only when there is nothing else.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in lines:
        low = ln.lower()
        if any(marker in low for marker, _s, _m in _CAUSES):
            return ln
    for ln in lines:
        if "nothing was written" not in ln.lower():
            return ln
    return lines[-1]


# ------------------------------------------------------------------ state ---

# One shared, observable state. The page polls this; every phase writes it.
STATE: dict = {
    "active": False, "phase": "", "pct": 0.0,
    "target": "",                     # 'installed' | 'latest'
    "version": "", "exe": "",
    "download": {"bytes": 0, "total": 0, "bps": 0.0, "recoveries": 0,
                 "attempt": 0},
    "tests": [],                      # rows, updated live as each one runs
    "error": "", "started": 0.0, "finished": 0.0,
}

_LOCK = asyncio.Lock()

# Where the candidate build lives. Deliberately NOT the staging dir: staging
# is what apply_when_idle() adopts, and a build being tested must not be
# eligible for adoption as a side effect of being tested.
def _test_dir():
    from .ffmpeg_update import INSTALL_DIR
    return INSTALL_DIR / "testbuild"


def _st(**kw) -> None:
    STATE.update(kw)


def state() -> dict:
    """Live progress plus the last saved results for both builds."""
    out = dict(STATE)
    out["download"] = dict(STATE["download"])
    out["tests"] = [dict(t) for t in STATE["tests"]]
    try:
        out["results"] = json.loads(kv_get("enctest.results") or "{}")
    except Exception:
        out["results"] = {}
    return out


# ------------------------------------------------------------- the probing --

def _probe_one(exe: str, fam: str, encoder: str, timeout: float = 45.0) -> dict:
    """Encode two seconds of colour bars with ONE encoder. The whole truth.

    Same shape as encoders._try_family() and for the same reasons: lavfi so no
    source file can skew it, a real output file because some encoders will
    "succeed" into NUL without ever touching the hardware, a per-run temp name
    so two probes cannot eat each other's output, and one retry because this
    can run on a machine with seven encodes in flight and lose the race.
    """
    from . import encoders
    spec = encoders.FAMILIES[fam]
    # SVT-AV1 numbers its presets 0-13; the x264-style ladder that the rest of
    # the cpu family speaks is an "Invalid argument" to it.
    preset = "8" if encoder == "libsvtav1" else spec["default_preset"]
    last: dict = {"ok": False, "detail": "", "seconds": 0.0}
    for attempt in range(2):
        out = os.path.join(
            tempfile.gettempdir(),
            f"nuarr_enctest_{encoder}_{os.getpid()}_{int(time.time()*1000)}.mkv")
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=2",
               "-c:v", encoder, "-preset", preset]
        cmd += encoders._quality_args(fam, 28)
        cmd += ["-t", "2", out]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, creationflags=NO_WINDOW,
            startupinfo=hidden_si())
            ok = (p.returncode == 0 and os.path.exists(out)
                  and os.path.getsize(out) > 1024)
            raw = "" if ok else (p.stderr or "").strip()
            last = {"ok": ok, "detail": _informative_line(raw),
                    "seconds": round(time.time() - t0, 2)}
            if not ok:
                m = _API_RE.search(raw)
                if m:
                    last["detail"] = (f"needs NVENC {m.group(1)}, this "
                                      f"driver provides {m.group(2)}")
        except subprocess.TimeoutExpired:
            last = {"ok": False, "detail": "timed out", "seconds": timeout}
        except Exception as e:                            # noqa: BLE001
            last = {"ok": False, "detail": f"{type(e).__name__}: {e}",
                    "seconds": round(time.time() - t0, 2)}
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
        if last["ok"]:
            break
        if attempt == 0:
            time.sleep(1.5)
    if not last["ok"]:
        short, meaning = _explain(last["detail"])
        last["verdict"], last["meaning"] = short, meaning
    else:
        last["verdict"], last["meaning"] = "supported", ""
    return last


async def _run_matrix(exe: str, pct_from: float, pct_to: float) -> list[dict]:
    """All encoders against one binary, streaming progress into STATE."""
    rows = _matrix()
    tests = [{**r, "status": "pending", "why": "", "meaning": "",
              "detail": "", "seconds": 0.0} for r in rows]
    _st(tests=tests)
    span = pct_to - pct_from
    for i, t in enumerate(tests):
        t["status"] = "testing"
        _st(phase=f"testing {t['encoder']}",
            pct=round(pct_from + span * i / len(tests), 1))
        r = await asyncio.to_thread(_probe_one, exe, t["family"], t["encoder"])
        t["status"] = "supported" if r["ok"] else "not supported"
        t["why"] = r["verdict"]
        t["meaning"] = r["meaning"]
        t["detail"] = r["detail"]
        t["seconds"] = r["seconds"]
    _st(pct=pct_to)
    return tests


# ------------------------------------------------- download, with recovery --

async def _fetch_zip(dest: str, pct_from: float, pct_to: float,
                     max_attempts: int = 8) -> str:
    """Download the release zip, resuming across drops. Returns its SHA-256.

    The recovery contract: any network failure waits briefly and retries FROM
    THE BYTES ALREADY ON DISK via an HTTP Range request (gyan.dev honours
    them). The count of recoveries is surfaced, not hidden - a flaky network
    is information. Only after the final byte does the checksum decide whether
    the assembled file is the published one; a corrupt resume cannot sneak
    through because SHA-256 covers the whole.
    """
    import hashlib
    import httpx
    from .ffmpeg_update import ZIP_URL, SHA_URL

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        _st(phase="fetching checksum")
        want = (await c.get(SHA_URL)).text.strip().split()[0].lower()

        span = pct_to - pct_from
        attempt = 0
        while True:
            attempt += 1
            have = os.path.getsize(dest) if os.path.exists(dest) else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            STATE["download"].update(attempt=attempt, bytes=have)
            _st(phase="downloading" if not have else
                f"resuming from {have // (1 << 20)} MB")
            t0, base = time.time(), have
            try:
                async with c.stream("GET", ZIP_URL, headers=headers) as r:
                    if have and r.status_code == 200:
                        # Server ignored the Range: it is sending the whole
                        # file, so start the local copy over to match.
                        have, base = 0, 0
                        open(dest, "wb").close()
                    r.raise_for_status()
                    total = have + int(r.headers.get("content-length") or 0)
                    STATE["download"].update(total=total)
                    with open(dest, "ab") as f:
                        async for chunk in r.aiter_bytes(1024 * 256):
                            f.write(chunk)
                            have += len(chunk)
                            el = time.time() - t0
                            bps = (have - base) / el if el > 0.5 else 0.0
                            STATE["download"].update(bytes=have, bps=bps)
                            if total:
                                _st(pct=round(
                                    pct_from + span * have / total, 1))
                break
            except (httpx.HTTPError, OSError) as e:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"download failed after {attempt} attempts: "
                        f"{type(e).__name__}: {e}") from e
                STATE["download"]["recoveries"] += 1
                _st(phase=f"connection lost - retrying "
                          f"({attempt}/{max_attempts})")
                await asyncio.sleep(min(3.0 * attempt, 15.0))

    _st(phase="verifying checksum")
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    got = h.hexdigest().lower()
    if got != want:
        os.remove(dest)
        raise RuntimeError("SHA-256 mismatch - download discarded")
    return got


# ------------------------------------------------------------ orchestration --

def _save_results(tag: str, version: str, exe: str, tests: list[dict]) -> None:
    try:
        allr = json.loads(kv_get("enctest.results") or "{}")
    except Exception:
        allr = {}
    allr[tag] = {"version": version, "exe": exe, "at": time.time(),
                 "tests": tests}
    kv_set("enctest.results", json.dumps(allr))


async def run(target: str = "installed") -> dict:
    """Test the installed build, or download-and-test the latest one."""
    from . import ffmpeg_update as fu

    if _LOCK.locked():
        return {"ok": False, "error": "a test run is already in progress"}
    async with _LOCK:
        _st(active=True, phase="starting", pct=0.0, target=target,
            version="", exe="", error="", started=time.time(), finished=0.0,
            tests=[])
        STATE["download"].update(bytes=0, total=0, bps=0.0, recoveries=0,
                                 attempt=0)
        try:
            if target == "latest":
                # BASELINE FIRST, AUTOMATICALLY. Adoption is a comparison
                # ("does anything that works today break?"), and a comparison
                # needs both sides. Asking the user to press a separate
                # "test installed" button first was a workflow with a
                # mandatory step disguised as an optional one.
                try:
                    have_base = bool((json.loads(
                        kv_get("enctest.results") or "{}")
                        .get("installed") or {}).get("tests"))
                except Exception:
                    have_base = False
                off = 0.0                # pct consumed by the baseline pass
                if not have_base:
                    exe0 = fu.installed_paths()[0]
                    _st(version=fu.local_version(exe0), exe=exe0,
                        phase="first, testing the installed build")
                    base = await _run_matrix(exe0, 0.0, 20.0)
                    _save_results("installed", fu.local_version(exe0),
                                  exe0, base)
                    off = 20.0
                info = await fu.check()
                if info.get("error"):
                    raise RuntimeError(info["error"])
                latest = info.get("latest") or ""
                tdir = _test_dir()
                exe = _find_test_exe()
                if exe and fu.local_version(exe) == latest:
                    # The candidate is already on disk and is the right
                    # version - no need to move 160 MB again to re-ask it.
                    _st(phase="using the already-downloaded build", pct=60.0)
                else:
                    shutil.rmtree(tdir, ignore_errors=True)
                    tdir.mkdir(parents=True, exist_ok=True)
                    zip_path = str(tdir / "download.zip")
                    await _fetch_zip(zip_path, off + 2.0, 55.0)
                    _st(phase="unpacking", pct=57.0)
                    await asyncio.to_thread(fu._unpack, zip_path, str(tdir))
                    os.remove(zip_path)
                    exe = _find_test_exe()
                if not exe:
                    raise RuntimeError("no ffmpeg.exe inside the archive")
                ver = fu.local_version(exe)
                if not ver:
                    raise RuntimeError("the downloaded ffmpeg did not run")
                _st(version=ver, exe=exe)
                tests = await _run_matrix(exe, 60.0, 100.0)
                _save_results("latest", ver, exe, tests)
            else:
                exe = fu.installed_paths()[0]
                ver = fu.local_version(exe)
                _st(version=ver, exe=exe)
                tests = await _run_matrix(exe, 0.0, 100.0)
                _save_results("installed", ver, exe, tests)
            ok_n = sum(1 for t in tests if t["status"] == "supported")
            _st(active=False, phase="done", pct=100.0, finished=time.time())
            joblog.log(f"encoder test bench: ffmpeg {STATE['version']} - "
                       f"{ok_n}/{len(tests)} encoders supported", "info")
            return {"ok": True, "version": STATE["version"],
                    "supported": ok_n, "total": len(tests)}
        except Exception as e:                            # noqa: BLE001
            _st(active=False, phase="failed", error=f"{e}",
                finished=time.time())
            return {"ok": False, "error": f"{e}"}


def _find_test_exe() -> str:
    """ffmpeg.exe inside the test-build directory, or ''."""
    tdir = _test_dir()
    if not tdir.is_dir():
        return ""
    for dirpath, _d, files in os.walk(str(tdir)):
        if "ffmpeg.exe" in files and "ffprobe.exe" in files:
            return os.path.join(dirpath, "ffmpeg.exe")
    return ""


async def adopt() -> dict:
    """Promote the TESTED build into staging - the 'save' after the test.

    This is the page's whole contract: nothing is saved until it has been
    measured, and the measurement is enforced here, not just displayed.
    Refused outright if any encoder that works on the INSTALLED build stops
    working on the candidate - that is the regression the thirty-hour failure
    was made of. Encoders that never worked (no such hardware) do not count
    against it.
    """
    from . import ffmpeg_update as fu

    exe = _find_test_exe()
    if not exe:
        return {"ok": False, "error": "no tested build on disk - run "
                                      "'Test latest' first"}
    try:
        allr = json.loads(kv_get("enctest.results") or "{}")
    except Exception:
        allr = {}
    cand = allr.get("latest") or {}
    ver = fu.local_version(exe)
    if not cand or cand.get("version") != ver:
        return {"ok": False, "error": "the build on disk has not been tested "
                                      "- run 'Test latest' first"}
    inst = allr.get("installed") or {}
    if not inst.get("tests"):
        # No baseline means no comparison, and "no comparison" must not decay
        # into "no objection". One extra minute of measuring beats adopting
        # a regression unexamined.
        return {"ok": False, "error": "test the installed build first, so "
                                      "there is a baseline to compare the "
                                      "new build against"}
    inst_ok = {t["encoder"] for t in inst.get("tests", [])
               if t.get("status") == "supported"}
    cand_ok = {t["encoder"] for t in cand.get("tests", [])
               if t.get("status") == "supported"}
    lost = sorted(inst_ok - cand_ok)
    if lost:
        why = {t["encoder"]: t.get("why") or "failed"
               for t in cand.get("tests", []) if t["encoder"] in lost}
        return {"ok": False, "blocked": True, "lost": lost,
                "error": "not adopted - these encoders work today but fail "
                         "on the new build: "
                         + ", ".join(f"{e} ({why.get(e)})" for e in lost)}

    bin_dir = os.path.dirname(exe)
    kv_set("ffmpeg.staged_version", ver)
    kv_set("ffmpeg.staged_bin", bin_dir)
    # The bench just proved something STRONGER than the nvenc-only guard in
    # apply_staged(): no encoder that works today regresses. Marking this "0"
    # on a machine where NVENC never worked (CPU-only box) would make the
    # guard refuse a build the bench had passed - the guard is for builds
    # that BYPASSED the bench, not ones it approved.
    kv_set("ffmpeg.staged_nvenc_ok", "1")
    joblog.log(f"ffmpeg {ver} passed the encoder bench and is staged - "
               f"applies when the queue is idle", "ok")
    return {"ok": True, "staged": ver,
            "message": "staged; applies automatically when no job is running"}
