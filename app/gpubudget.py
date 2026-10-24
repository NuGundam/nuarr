r"""GPU memory, budgeted: a big model loads only when there is room for it.

WHY. Everything nuarr runs on the card draws on one 24 GB pool, and none of
it asked first. On 2026-10-21 the card sat at 23.4 of 24 GB: four NVENC
encodes, PaddleOCR serving the burned-in check, an idle Whisper process
still holding its model, the household doorbell model in Ollama, and
Qwen-VL at 10.3 GB (Ollama's default 32k context). The Qwen judge then tried
to load its 7B model on top and sat at "loading Qwen" with nothing moving.

So before a heavy model starts, its owner asks `make_room(gb, who)`:

  1. if the free memory (less a safety margin) already covers it, go;
  2. otherwise clear what is idle, cheapest-to-reload first - Qwen-VL in
     Ollama (reloads in seconds), nuarr's Whisper process when no listen job
     is running, the PaddleOCR reader when it has not been asked for a while;
  3. wait for encodes and other work to give memory back, up to `timeout`;
  4. if there is still no room, say so - the caller waits for its next turn
     rather than loading into a full card.

Never touched: the household's own models in Ollama (the doorbell), Plex,
and anything nuarr did not start.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request

from . import joblog

MARGIN_GB = 1.0          # kept free for encodes starting and drivers
COST_GB = {              # measured on the A5000, model + working memory
    "qwen": 8.5,         # Qwen2.5-7B int8_float16, CTranslate2
    "whisper": 4.0,      # large-v3 float16
    "whisper_small": 1.2,  # the language identifier (audiolang)
    "paddle": 1.5,
    "qwenvl": 5.6,       # qwen3-vl:8b at num_ctx 2048
}
OLLAMA = "http://127.0.0.1:11434"
EVICTABLE_OLLAMA = ("qwen3-vl",)        # nuarr's own; the doorbell model is not ours

STATE: dict = {"waiting": {}, "freed": [], "last": {}}
_CACHE: dict = {"at": 0.0, "v": None}


def vram() -> tuple[float, float] | None:
    """(used GB, total GB) for the first GPU, cached two seconds."""
    if _CACHE["v"] is not None and time.time() - _CACHE["at"] < 2.0:
        return _CACHE["v"]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000).stdout.strip().splitlines()[0]
        used, total = (float(x) / 1024 for x in out.split(","))
        _CACHE.update(at=time.time(), v=(used, total))
        return used, total
    except Exception:                                    # noqa: BLE001
        return None


def free_gb() -> float | None:
    v = vram()
    return None if v is None else max(0.0, v[1] - v[0] - MARGIN_GB)


def _ollama_loaded() -> list:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("models") or []
    except Exception:                                    # noqa: BLE001
        return []


def _evict_ollama(who: str) -> float:
    gb = 0.0
    for m in _ollama_loaded():
        name = m.get("name") or ""
        if not name.startswith(EVICTABLE_OLLAMA) or who == "qwenvl":
            continue
        try:
            req = urllib.request.Request(OLLAMA + "/api/generate",
                                         data=json.dumps({"model": name, "keep_alive": 0}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
            gb += float(m.get("size_vram") or 0) / 1e9
            _note(f"unloaded {name} from Ollama ({gb:.1f} GB) for {who}")
        except Exception:                                # noqa: BLE001
            pass
    return gb


def _evict_whisper(who: str) -> bool:
    if who == "whisper":
        return False
    try:
        from . import jobs, audiolang
        if any((getattr(w, "pool", "") or "") == "listen" for w in list(jobs.RUNNING.values())):
            return False
        if getattr(audiolang, "_MODEL", None) is None:
            return False
        audiolang.unload()
        _note(f"closed the idle Whisper process for {who}")
        return True
    except Exception:                                    # noqa: BLE001
        return False


def _evict_paddle(who: str) -> bool:
    if who == "paddle":
        return False
    try:
        from . import hardsub
        o = hardsub._OCR
        if o.get("proc") is None or time.time() - float(o.get("at") or 0) < 30:
            return False
        hardsub._ocr_close()
        _note(f"closed the idle PaddleOCR reader for {who}")
        return True
    except Exception:                                    # noqa: BLE001
        return False


def _note(msg: str) -> None:
    STATE["freed"] = ([{"at": time.time(), "what": msg}] + STATE["freed"])[:20]
    joblog.log(f"GPU memory: {msg}", "info", system="gpu")


def make_room(need_gb: float, who: str, timeout: float = 600.0, on_wait=None) -> tuple[bool, str]:
    """Block until `need_gb` is free (clearing idle holders), or `timeout`."""
    t0, cleared = time.time(), False
    while True:
        f = free_gb()
        if f is None:
            return True, "no GPU reading - going ahead"
        if f >= need_gb:
            STATE["waiting"].pop(who, None)
            STATE["last"][who] = {"at": time.time(), "free": round(f, 1), "need": need_gb}
            return True, f"{f:.1f} GB free"
        if not cleared:
            cleared = True
            _evict_ollama(who)
            _evict_whisper(who)
            _evict_paddle(who)
            _CACHE["at"] = 0.0
            time.sleep(4)
            continue
        el = time.time() - t0
        STATE["waiting"][who] = {"since": t0, "need": need_gb, "free": round(f, 1)}
        if on_wait:
            try:
                on_wait(f, need_gb, el)
            except Exception:                            # noqa: BLE001
                pass
        if el >= timeout:
            STATE["waiting"].pop(who, None)
            return False, f"only {f:.1f} GB free of the {need_gb:g} GB it needs"
        time.sleep(10)
        # a second sweep later: something may have gone idle meanwhile
        if int(el) % 120 < 10:
            _evict_whisper(who)
            _evict_paddle(who)


def snapshot() -> dict:
    v = vram()
    return {"used": round(v[0], 1) if v else None, "total": round(v[1], 1) if v else None,
            "free": round(free_gb(), 1) if v else None, "margin": MARGIN_GB, "cost": COST_GB,
            "waiting": STATE["waiting"], "freed": STATE["freed"][:8]}
