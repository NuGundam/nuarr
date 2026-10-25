"""Qwen-VL: the reader of last resort for subtitle pictures.

THE OCR ENGINES READ PIXELS AND QWEN READS TEXT, and neither can do the
other's job. PaddleOCR and Tesseract turn a subtitle bitmap into words; the
text model repairs words that came out wrong, but when a cue comes back empty
or as "lLlknow ldo." there is nothing for it to repair - it never sees the
picture. A VISION model does: it is shown the bitmap and asked what it says.

It is slow (a second or more per cue against Paddle's fifth of one) so it is
never the first reader. It is asked only for a cue the engine doubted - no
text, low confidence, or text that does not look like words - and at most
VL_MAX times per track, so a track the engine cannot read at all costs
minutes, not hours.

IT RUNS THROUGH OLLAMA, which already serves the household's doorbell model
on this card: no new runtime, no weights in nuarr's folder, one HTTP call per
picture. The top of this module is standard library only, because
paddle_worker.py (a bare script, not part of the package) imports it; the
nuarr-side helpers import the package lazily inside their functions.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request

OLLAMA = os.environ.get("NUARR_OLLAMA", "http://127.0.0.1:11434")
MODEL_DEFAULT = "qwen3-vl:8b-instruct"
VL_MAX_DEFAULT = 200           # cues per track it may be asked about
DOUBT_SCORE = 0.72             # mean Paddle confidence under this is a doubt
LOAD_GB = 6.6                  # free GPU memory needed to LOAD it (5.6 + the 1 GB margin gpubudget keeps)
KEEP_ALIVE = "3m"              # Ollama unloads it after this long idle; loading again costs about a second

PROMPT = ("This image is one subtitle cue cut from a video. Transcribe the text "
          "exactly as written, one line per line of text, keeping the original "
          "language and punctuation, and music-note symbols as ♪. Output the text "
          "only - no quotes, no notes. "
          "If there is no readable text, output exactly: NONE")

KV_ON = "qwenvl.on"
KV_MODEL = "qwenvl.model"
KV_STATS = "qwenvl.stats"


# ------------------------------------------------- worker side (stdlib) ----
def doubt(texts, scores=None) -> bool:
    """Should the vision model be asked about this cue?

    Three signs the engine struggled: it read nothing; it was not sure of what
    it read (Paddle's own per-line score); or what it read does not look like
    writing - mostly symbols, or capitals inside words, which is how Tesseract
    renders italics ("lLlknow ldo.").
    """
    lines = [t.strip() for t in (texts or []) if t and t.strip()]
    if not lines:
        return True
    if scores:
        s = [float(x) for x in scores if x is not None]
        if s and sum(s) / len(s) < DOUBT_SCORE:
            return True
    txt = " ".join(lines)
    # MUSIC NOTES ARE WRITING. A "♪♪" cue is a real subtitle, and it is the
    # kind the vision model gets wrong ("11") - measured on the first try.
    letters = sum(ch.isalnum() or ch in "♪♫" for ch in txt)
    if letters < 0.6 * max(1, len(txt.replace(" ", ""))):
        return True
    words = re.findall(r"[^\s]+", txt)
    odd = 0
    for w in words:
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) >= 3 and re.search(r"[a-z][A-Z]", core):
            odd += 1                      # a capital in the middle of a word
        elif len(core) == 1 and core not in "aAIi":
            odd += 1                      # a lone letter that is not a word
    return odd >= max(1, len(words) // 3)


def read_image(png: bytes, model: str = "", timeout: float = 90.0) -> str:
    """Ask the vision model what a subtitle picture says. '' when nothing."""
    body = json.dumps({
        "model": model or MODEL_DEFAULT, "prompt": PROMPT, "stream": False,
        "images": [base64.b64encode(png).decode("ascii")],
        "keep_alive": KEEP_ALIVE,
        # 2048 TOKENS OF CONTEXT, not Ollama's default 32k: one picture and a
        # one-line answer need a fraction of it, and the default held 10.3 GB
        # of the card where 2048 holds 5.5 GB - same answer, measured.
        "options": {"temperature": 0, "num_predict": 160, "num_ctx": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8")).get("response") or ""
    out = out.strip().strip("`").strip()
    if not out or out.upper().startswith("NONE"):
        return ""
    # The model sometimes wraps its answer in quotes or labels it.
    out = re.sub(r"^(text|subtitle)\s*:\s*", "", out, flags=re.I).strip('"“” ')
    return "\n".join(l.strip() for l in out.splitlines() if l.strip())


def png_bytes(pic) -> bytes:
    """A numpy image (grey or RGB) as PNG bytes, for read_image."""
    import io
    from PIL import Image
    im = Image.fromarray(pic)
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class Helper:
    """What a worker holds: the model name (empty = off), a budget, counts."""

    def __init__(self, model: str = "", vl_max: int = 0):
        self.model = model or ""
        self.max = int(vl_max or int(os.environ.get("NUARR_QWENVL_MAX") or VL_MAX_DEFAULT))
        self.asked = self.got = self.failed = 0
        self.secs = 0.0

    @classmethod
    def from_env(cls) -> "Helper":
        return cls(os.environ.get("NUARR_QWENVL", ""))

    def __bool__(self) -> bool:
        return bool(self.model) and self.asked < self.max

    def _room(self) -> bool:
        """Loaded already, or room on the card to load it. A fallback reader
        must never be the thing that crowds the card out - so it is skipped
        when it would have to load into less than LOAD_GB."""
        now = time.time()
        if now - getattr(self, "_room_at", 0.0) < 20:
            return self._room_ok
        ok = True
        try:
            with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=3) as r:
                loaded = any((m.get("name") or "").startswith(self.model.split(":")[0])
                             for m in json.loads(r.read().decode("utf-8")).get("models") or [])
            if not loaded:
                import subprocess
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                                      "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                     timeout=10, creationflags=0x08000000).stdout.split(",")
                free = (float(out[1]) - float(out[0])) / 1024
                ok = free >= LOAD_GB
        except Exception:                                # noqa: BLE001
            ok = True
        self._room_at, self._room_ok = now, ok
        if not ok:
            self.skipped = getattr(self, "skipped", 0) + 1
        return ok

    def ask(self, pic) -> list:
        """Lines the vision model read from a numpy image; [] if none/failed."""
        if not self or not self._room():
            return []
        self.asked += 1
        t0 = time.time()
        try:
            txt = read_image(png_bytes(pic), self.model)
        except Exception:                                # noqa: BLE001
            self.failed += 1
            txt = ""
        self.secs += time.time() - t0
        if txt:
            self.got += 1
        self._live(time.time() - t0)
        return txt.splitlines() if txt else []

    def _live(self, last: float) -> None:
        """WHAT IT IS DOING NOW, for nuarr's Processing panel - written to the
        file nuarr named, replaced whole so a reader never sees half of it."""
        p = os.environ.get("NUARR_QWENVL_LIVE", "")
        if not p:
            return
        if not getattr(self, "_t0", 0):
            self._t0 = time.time() - last
        try:
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "for": os.environ.get("NUARR_QWENVL_FOR", ""),
                           "asked": self.asked, "got": self.got, "failed": self.failed,
                           "secs": round(self.secs, 2), "last": round(last, 2), "max": self.max,
                           "at": time.time(), "started": self._t0, "model": self.model}, f)
            os.replace(p + ".tmp", p)
        except Exception:                                # noqa: BLE001
            pass

    def stat_line(self) -> str:
        return (f"VLSTAT asked={self.asked} got={self.got} failed={self.failed} "
                f"secs={self.secs:.1f}")


# --------------------------------------------------------- nuarr side ----
def on() -> bool:
    from .db import kv_get
    return str(kv_get(KV_ON) or "1") == "1"


def set_on(flag: bool) -> None:
    from .db import kv_set
    kv_set(KV_ON, "1" if flag else "0")


def model() -> str:
    from .db import kv_get
    return str(kv_get(KV_MODEL) or MODEL_DEFAULT)


def set_model(name: str) -> None:
    from .db import kv_set
    kv_set(KV_MODEL, (name or MODEL_DEFAULT).strip())


def _get(path: str, timeout: float = 4.0):
    with urllib.request.urlopen(OLLAMA + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_CACHE: dict = {"at": 0.0, "tags": None, "version": ""}


def _tags(force: bool = False) -> list | None:
    """Ollama's model list, cached a minute; None when Ollama is not there."""
    if not force and _CACHE["tags"] is not None and time.time() - _CACHE["at"] < 60:
        return _CACHE["tags"]
    try:
        _CACHE["tags"] = _get("/api/tags").get("models") or []
        try:
            _CACHE["version"] = _get("/api/version").get("version") or ""
        except Exception:                                # noqa: BLE001
            pass
    except Exception:                                    # noqa: BLE001
        _CACHE["tags"] = None
    _CACHE["at"] = time.time()
    return _CACHE["tags"]


def present(name: str = "") -> dict | None:
    name = name or model()
    for m in (_tags() or []):
        if m.get("name") == name or m.get("model") == name:
            return m
    return None


def env_model() -> str:
    """The model name a worker should use, or '' when it must not ask."""
    try:
        if on() and present():
            return model()
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def record(asked: int, got: int, secs: float, failed: int = 0) -> None:
    """Add a worker's VLSTAT line to the running totals."""
    from .db import kv_get, kv_set
    if not asked:
        return
    try:
        st = json.loads(kv_get(KV_STATS) or "{}")
    except Exception:                                    # noqa: BLE001
        st = {}
    st["asked"] = int(st.get("asked") or 0) + int(asked)
    st["got"] = int(st.get("got") or 0) + int(got)
    st["failed"] = int(st.get("failed") or 0) + int(failed)
    st["secs"] = float(st.get("secs") or 0) + float(secs)
    st["last"] = time.time()
    kv_set(KV_STATS, json.dumps(st))


def record_line(line: str) -> bool:
    """A worker's `VLSTAT asked=.. got=.. failed=.. secs=..` line, recorded."""
    m = re.match(r"VLSTAT asked=(\d+) got=(\d+) failed=(\d+) secs=([\d.]+)", line or "")
    if not m:
        return False
    record(int(m[1]), int(m[2]), float(m[4]), int(m[3]))
    return True


PULL: dict = {"busy": False, "status": "", "done": 0, "total": 0, "error": "", "at": 0.0}


def pull(name: str = "") -> dict:
    """Download the model through Ollama, in a thread; PULL shows progress."""
    import threading
    name = name or model()
    if PULL["busy"]:
        return {"started": False, "why": "already downloading"}
    PULL.update(busy=True, status="starting", done=0, total=0, error="", at=time.time())

    def _go():
        try:
            body = json.dumps({"model": name, "stream": True}).encode("utf-8")
            req = urllib.request.Request(OLLAMA + "/api/pull", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3600) as r:
                for raw in r:
                    try:
                        d = json.loads(raw.decode("utf-8"))
                    except Exception:                    # noqa: BLE001
                        continue
                    if d.get("error"):
                        PULL["error"] = str(d["error"])[:200]
                        break
                    PULL.update(status=str(d.get("status") or ""), done=int(d.get("completed") or 0),
                                total=int(d.get("total") or PULL["total"]))
        except Exception as e:                           # noqa: BLE001
            PULL["error"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            PULL["busy"] = False
            _tags(force=True)
    threading.Thread(target=_go, daemon=True, name="qwenvl-pull").start()
    return {"started": True}


def live_path() -> str:
    from .config import DATA_DIR
    return os.path.join(str(DATA_DIR), "qwenvl.live.json")


_PS: dict = {"at": 0.0, "m": None}
_FOR: dict = {"file": "", "at": 0.0, "last": ""}


def note_for(path: str) -> None:
    """The file a long-lived reader is working on right now (the picture
    check's served worker reads for many files in turn)."""
    _FOR.update(file=os.path.basename(path or ""), at=time.time())


def _nice(fname: str) -> str:
    """'City Hunter (1987) - S01E39 - Title [WEBDL...].mkv' -> 'City Hunter - S01E39'."""
    try:
        from .db import pretty_from_filename
        return pretty_from_filename(fname or "") or (fname or "")
    except Exception:                                    # noqa: BLE001
        stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", fname or "")
        return stem.split(" [")[0]


def _loaded() -> dict | None:
    """Ollama's own word on whether the model is in memory - asked at most
    every five seconds, because the panel polls several times a second."""
    now = time.time()
    if now - _PS["at"] > 5:
        _PS["at"] = now
        _PS["m"] = None
        try:
            for m in _get("/api/ps", timeout=1.5).get("models") or []:
                if (m.get("name") or "").startswith("qwen3-vl") or (m.get("model") or "").startswith("qwen3-vl"):
                    _PS["m"] = m
        except Exception:                                # noqa: BLE001
            pass
    return _PS["m"]


def as_running() -> list:
    """A card while the vision model is reading, or loaded and waiting to
    unload. Reading: the cues it has been asked about, its pace and the job it
    is reading for. Loaded and idle: a bar that drains to the moment Ollama
    lets it go."""
    try:
        live = json.load(open(live_path(), encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        live = {}
    m = _loaded()
    now = time.time()
    reading = bool(live) and now - float(live.get("at") or 0) < 30
    if not reading and not m:
        return []
    # WHAT IT IS READING FOR: the file the worker was started for, or the one
    # the picture check's served worker last turned to - and while it sits
    # loaded and idle, the file it read last.
    fname = (live.get("for") or "") if reading else ""
    if reading and not fname and now - _FOR["at"] < 600:
        fname = _FOR["file"]
    if reading and not fname:
        try:
            from . import jobs as _j
            for w in list(_j.RUNNING.values()):
                if getattr(w, "sub_ocr_active", False) or getattr(w, "pool", "") in ("subocr", "subread", "submake"):
                    fname = os.path.basename(w.job.path)
                    break
        except Exception:                                # noqa: BLE001
            pass
    from .db import kv_get, kv_set
    if fname and fname != _FOR["last"]:
        _FOR["last"] = fname
        kv_set("qwenvl.last_for", fname)     # survives a restart
    if not _FOR["last"]:
        _FOR["last"] = kv_get("qwenvl.last_for") or ""
    shown = fname or _FOR["last"]
    from . import outside as _out
    vram = int((m or {}).get("size_vram") or 0)
    if reading:
        asked, got = int(live.get("asked") or 0), int(live.get("got") or 0)
        mx = max(1, int(live.get("max") or VL_MAX_DEFAULT))
        avg = float(live.get("secs") or 0) / max(1, asked)
        stage = (f"reading doubted cues - {asked} asked, {got} read, {avg:.1f}s each"
                 + (f" · {vram / 1e9:.1f} GB on the card" if vram else ""))
        pct = min(1.0, asked / mx)
        if not _out._M.get("qwenvl") or _out._M["qwenvl"].get("pid") != int(live.get("pid") or 0):
            _out.start("qwenvl")
            _out.set_pid("qwenvl", int(live.get("pid") or 0))
        extra = _out.fields("qwenvl", pct)
        extra.pop("eta_s", None)
        el = round(now - float(live.get("started") or now))
        # the pace over the worker's own run, not since this card was first drawn
        extra.update(rate=round(asked / max(1.0, el) * 60, 1), rate_unit="cues/min")
    else:
        left = 0.0
        try:
            import datetime as _dt
            exp = (m or {}).get("expires_at") or ""
            left = (_dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        except Exception:                                # noqa: BLE001
            pass
        stage = (f"loaded, idle - unloads in {int(max(0, left))}s"
                 + (f" · {vram / 1e9:.1f} GB on the card" if vram else ""))
        pct = max(0.0, min(1.0, left / 180.0))
        extra = {"eta_s": round(max(0, left)), "eta_est": False}
        el = 0
    if not reading and shown:
        stage = f"last read for {_nice(shown)} · " + stage
    return [{"job_id": "qwenvl", "pool": "qwenvl", "kind": "qwenvl",
             "title": _nice(shown) if shown else "Qwen-VL reader",
             "file": shown or "(nothing read since nuarr started)",
             "stage": stage, "progress": pct, "elapsed_s": el, "eta_est": True,
             "plan": "the reader of last resort: a subtitle picture OCR doubted, read by a vision model",
             "why_pool": "GPU through Ollama - loaded on demand, let go after three idle minutes",
             "doing": {"tool": (live.get("model") or model()) if reading else model(), "hw": "GPU",
                       "why": "reading" if reading else "waiting to unload"},
             "disk": "", "dest_disk": "", "fps": 0.0, "speed": 0.0,
             "paused_for_viewer": False, "paused_for_load": False, "cancelled": False, "by_bytes": False,
             **extra}]


def info() -> dict:
    """Everything the Qwen page shows about the vision model."""
    from .db import kv_get
    tags = _tags()
    name = model()
    m = present(name) if tags is not None else None
    loaded = None
    if tags is not None:
        try:
            for p in _get("/api/ps").get("models") or []:
                if p.get("name") == name or p.get("model") == name:
                    loaded = {"vram": int(p.get("size_vram") or 0), "size": int(p.get("size") or 0)}
        except Exception:                                # noqa: BLE001
            pass
    try:
        st = json.loads(kv_get(KV_STATS) or "{}")
    except Exception:                                    # noqa: BLE001
        st = {}
    det = (m or {}).get("details") or {}
    others = [t.get("name") for t in (tags or []) if "vl" in (t.get("name") or "") and t.get("name") != name]
    return {"ollama": tags is not None, "ollama_version": _CACHE.get("version") or "", "url": OLLAMA,
            "model": name, "default": MODEL_DEFAULT, "present": bool(m), "size": int((m or {}).get("size") or 0),
            "quant": det.get("quantization_level") or "", "family": det.get("family") or "",
            "params": det.get("parameter_size") or "", "format": det.get("format") or "",
            "loaded": loaded, "on": on(), "stats": st, "pull": dict(PULL), "other_vl": others,
            "vl_max": VL_MAX_DEFAULT, "doubt_score": DOUBT_SCORE, "keep_alive": KEEP_ALIVE}
