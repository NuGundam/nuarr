r"""nuarr - every outside tool nuarr runs, its version, and whether a newer one exists.

WHY ONE LIST. Each tool grew its own check where it was first needed: ffmpeg
on its page, the three OCR engines on theirs, Whisper on its own, MKVToolNix
as a pill that said "103.0 available - installer at mkvtoolnix.download" and
nothing else, Ollama nowhere at all. So the honest answer to "is everything
current" was a walk round five pages. Erik, 2026-11-02: "make sure all tools
have an update and check for updates". This asks every one of them once a day
and the Updates page lists them together.

INSTALLING STAYS WHERE IT WAS SAFE TO BUILD. ffmpeg stages and verifies on
its own page, Tesseract / pgsrip / Whisper install from theirs - those buttons
are reused. MKVToolNix gets one here: the official installer, its SHA-256
checked against the release's own sums file and its Authenticode signature
checked, run silently, then the new mkvmerge asked its version. Two are
deliberately check-only: Ollama also serves the household's doorbell model,
so restarting it is a person's choice, and the NVIDIA driver is a system
install with its own reboot story.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time

from .config import NO_WINDOW, hidden_si

KV = "tools.updates"
MKV_DIR = r"C:\Program Files\MKVToolNix"
MKV_INSTALL: dict = {"state": "", "error": "", "version": "", "at": 0.0}
_LOCK = threading.Lock()


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _newer(cur: str, lat: str) -> bool:
    return bool(cur and lat and _ver(lat) > _ver(cur))


def _run(args: list, timeout: int = 30) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=NO_WINDOW, startupinfo=hidden_si())
        return (r.stdout or "") + (r.stderr or "")
    except Exception:                                            # noqa: BLE001
        return ""


def _get_json(url: str) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "nuarr",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------- MKVToolNix --
def mkv_installed() -> str:
    m = re.search(r"v(\d+(?:\.\d+)*)", _run([os.path.join(MKV_DIR, "mkvmerge.exe"), "--version"]))
    return m.group(1) if m else ""


def mkv_latest() -> str:
    import gzip
    import urllib.request
    req = urllib.request.Request("https://mkvtoolnix.download/latest-release.xml.gz",
                                 headers={"User-Agent": "nuarr"})
    with urllib.request.urlopen(req, timeout=20) as r:
        xml = gzip.decompress(r.read()).decode("utf-8", "replace")
    m = re.search(r"<latest-source>.*?<version>([\d.]+)</version>", xml, re.S)
    return m.group(1) if m else ""


def _mkv_busy() -> str:
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    for name in ("mkvmerge.exe", "mkvpropedit.exe", "mkvextract.exe", "mkvtoolnix-gui.exe"):
        if name in out.lower():
            return name
    return ""


def mkv_install_start() -> dict:
    """Download, verify and silently install the newest MKVToolNix."""
    with _LOCK:
        if MKV_INSTALL["state"] == "installing":
            return {"ok": False, "error": "already installing"}
        MKV_INSTALL.update(state="installing", error="", at=time.time())

    def _work():
        import urllib.request
        exe = ""
        try:
            v = mkv_latest()
            if not v:
                raise RuntimeError("could not read the latest version")
            MKV_INSTALL["version"] = v
            if _ver(v) <= _ver(mkv_installed()):
                MKV_INSTALL.update(state="done")
                return
            base = f"https://mkvtoolnix.download/windows/releases/{v}/"
            name = f"mkvtoolnix-64-bit-{v}-setup.exe"
            exe = os.path.join(tempfile.gettempdir(), name)
            req = urllib.request.Request(base + name, headers={"User-Agent": "nuarr"})
            h = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=120) as r, open(exe, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
                    f.write(b)
            # THE RELEASE'S OWN SUMS FILE, then the signature: a file that
            # matches neither is not run.
            req = urllib.request.Request(base + "sha256sums.txt", headers={"User-Agent": "nuarr"})
            with urllib.request.urlopen(req, timeout=20) as r:
                sums = r.read().decode("utf-8", "replace")
            want = next((ln.split()[0].lower() for ln in sums.splitlines()
                         if ln.strip().endswith(name)), "")
            if not want or want != h.hexdigest():
                raise RuntimeError("the installer does not match the release's SHA-256")
            sig = _run(["powershell", "-NoProfile", "-Command",
                        f"(Get-AuthenticodeSignature '{exe}').Status"]).strip()
            if sig != "Valid":
                raise RuntimeError(f"the installer's signature is {sig or 'unreadable'}")
            busy = _mkv_busy()
            if busy:
                raise RuntimeError(f"{busy} is running - try again when it has finished")
            p = subprocess.Popen([exe, "/S"], creationflags=NO_WINDOW, startupinfo=hidden_si())
            try:
                rc = p.wait(timeout=600)
            except subprocess.TimeoutExpired:
                _run(["taskkill", "/T", "/F", "/PID", str(p.pid)])
                raise RuntimeError("the installer hung and was stopped")
            if rc != 0:
                raise RuntimeError(f"the installer exited {rc}")
            got = mkv_installed()
            if _ver(got) != _ver(v):
                raise RuntimeError(f"installed, but mkvmerge reports {got or 'nothing'}")
            MKV_INSTALL.update(state="done")
            from . import joblog
            joblog.log(f"MKVToolNix {v} installed (checksum and signature verified)", "ok")
        except Exception as e:                                   # noqa: BLE001
            MKV_INSTALL.update(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            if exe:
                try:
                    os.remove(exe)
                except OSError:
                    pass
            try:
                check_all_sync()
            except Exception:                                    # noqa: BLE001
                pass

    threading.Thread(target=_work, name="mkv-update", daemon=True).start()
    return {"ok": True}


# --------------------------------------------------------------- Ollama --
def ollama_installed() -> str:
    try:
        from . import qwenvl
        import urllib.request
        with urllib.request.urlopen(qwenvl.OLLAMA.rstrip("/") + "/api/version", timeout=5) as r:
            return json.loads(r.read().decode("utf-8")).get("version") or ""
    except Exception:                                            # noqa: BLE001
        m = re.search(r"(\d+\.\d+\.\d+)", _run(["ollama", "--version"]))
        return m.group(1) if m else ""


def ollama_latest() -> str:
    return str(_get_json("https://api.github.com/repos/ollama/ollama/releases/latest")
               .get("tag_name") or "").lstrip("v")


# ------------------------------------------------------------ the check --
def _row(key, name, installed, latest, how, what, page="", error="") -> dict:
    return {"key": key, "name": name, "installed": installed or "", "latest": latest or "",
            "newer": _newer(installed, latest), "how": how, "what": what,
            "page": page, "error": error}


def check_all_sync() -> dict:
    """Every tool, asked now. Slow (pip, GitHub, four downloads of a version
    string) - run on a thread; the page reads the stored answer."""
    rows = []
    # nuarr itself
    try:
        from . import updates
        updates.check(force=True)
        s = updates.status()
        rows.append(_row("nuarr", "nuarr", s.get("current"), s.get("latest"), "here",
                         "this program", error=s.get("error") or ""))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("nuarr", "nuarr", "", "", "here", "this program", error=str(e)[:120]))
    # ffmpeg
    try:
        from . import ffmpeg_update
        f = asyncio.run(ffmpeg_update.check(force=True))
        cur = re.match(r"[\d.]+", f.get("current") or "")
        rows.append(_row("ffmpeg", "ffmpeg", cur.group(0) if cur else f.get("current"),
                         f.get("latest"), "page", "every encode, probe and cut", "ffmpeg",
                         error=f.get("error") or ""))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("ffmpeg", "ffmpeg", "", "", "page", "every encode, probe and cut", "ffmpeg",
                         error=str(e)[:120]))
    # MKVToolNix
    try:
        rows.append(_row("mkvtoolnix", "MKVToolNix", mkv_installed(), mkv_latest(), "install",
                         "remuxes, track flags, subtitle extracts", "mkv"))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("mkvtoolnix", "MKVToolNix", mkv_installed(), "", "install",
                         "remuxes, track flags, subtitle extracts", "mkv", error=str(e)[:120]))
    # the OCR engines
    try:
        from . import subocr
        o = subocr.updates_check()
        for k, name, what, how in (("tesseract", "Tesseract", "reads PGS pictures", "page"),
                                   ("pgsrip", "pgsrip", "drives Tesseract over a PGS track", "page"),
                                   ("paddleocr", "PaddleOCR", "the second OCR reader", "page")):
            x = o.get(k) or {}
            rows.append(_row(k, name, x.get("installed"), x.get("latest"), how, what, "ocr"))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("ocr", "OCR engines", "", "", "page", "", "ocr", error=str(e)[:120]))
    # Whisper
    try:
        from . import audiolang
        from importlib import metadata
        cur = metadata.version("faster-whisper")
        lat = audiolang.latest_version("faster-whisper")
        rows.append(_row("whisper", "faster-whisper", cur, lat.get("latest"), "page",
                         "hears the audio language and the speech for timing", "whisper",
                         error=lat.get("detail") or ""))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("whisper", "faster-whisper", "", "", "page", "", "whisper", error=str(e)[:120]))
    # Ollama
    try:
        rows.append(_row("ollama", "Ollama", ollama_installed(), ollama_latest(), "check",
                         "runs Qwen-VL - and the doorbell model, so updating it is your call"))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("ollama", "Ollama", ollama_installed(), "", "check", "runs Qwen-VL",
                         error=str(e)[:120]))
    # NVIDIA driver
    try:
        from . import ffmpeg_update
        d = ffmpeg_update.driver_outlook()
        lat = (d.get("latest") or {})
        rows.append(_row("driver", "NVIDIA driver", str(d.get("installed") or d.get("driver") or ""),
                         str(lat.get("version") or ""), "check",
                         "NVENC for every GPU encode - a system install, done by hand"))
    except Exception as e:                                       # noqa: BLE001
        rows.append(_row("driver", "NVIDIA driver", "", "", "check", "NVENC", error=str(e)[:120]))
    out = {"at": time.time(), "rows": rows}
    try:
        from .db import kv_set
        kv_set(KV, json.dumps(out))
    except Exception:                                            # noqa: BLE001
        pass
    return out


def state() -> dict:
    try:
        from .db import kv_get
        out = json.loads(kv_get(KV) or "{}")
    except Exception:                                            # noqa: BLE001
        out = {}
    out["mkv_install"] = dict(MKV_INSTALL)
    return out


async def watch() -> None:
    """Once a day, every tool - the Jobs page row."""
    from . import schedules
    schedules.register(
        "toolsupd", "Tool updates", "System", 86400,
        what="Asks once a day whether nuarr, ffmpeg, MKVToolNix, the OCR engines, "
             "Whisper, Ollama or the NVIDIA driver has a newer release. Lists them on "
             "the Updates page; installing is always your click there.")
    await asyncio.sleep(600)
    while True:
        try:
            if time.time() - float(state().get("at") or 0) >= 86400:
                r = await asyncio.to_thread(check_all_sync)
                avail = [x["name"] for x in r["rows"] if x.get("newer")]
                schedules.beat("toolsupd", ("updates: " + ", ".join(avail)) if avail
                               else "everything current")
            else:
                schedules.beat("toolsupd", "checked recently")
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(3600)
