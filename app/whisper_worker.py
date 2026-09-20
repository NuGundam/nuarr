r"""nuarr - the language model, in a process that can be closed.

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
whisper_probe.py asks one question once and dies. This is the same idea kept
alive: it loads the model, then answers "what language is this audio" for as
long as the server keeps feeding it, and exits when the pipe closes.

The reason is memory, and it was measured rather than assumed. faster-whisper
runs on CTranslate2, which pulls in the CUDA runtime - cuBLAS, cuDNN, and a
cublasLt64_12.dll that alone maps 296 MB. Loaded into the server process,
that costs about 2.5 GB of private commit for the life of the process:

    private commit   3,055 MB        python objects alive   30 MB
    resident            533 MB       whisper state          none

Those numbers were taken with the model UNLOADED and nothing listening.
audiolang.unload() sets _MODEL to None at the end of every pass and gives the
VRAM back, and it cannot give that 2.5 GB back, because a CUDA context cannot
be unloaded from a process that is still running. The only thing that frees it
is the process ending - so the work moves into a process that can end.

paddle_worker.py already does this for OCR, for a related reason (a segfault
in a native kernel is not an exception anyone can catch), and this is
deliberately the same shape.

WHAT STAYS IN THE SERVER: everything that decides anything. The windows to
take, dodging the opening theme, the vote, the confidence floor, the second
look - all of it is in audiolang.py and untouched. This process is asked
exactly what the in-process model was asked, `detect_language` on one window
of PCM, and answers with exactly what it returned. The verdict logic cannot
drift from the worker because the worker has no verdict logic.

THE PROTOCOL, which is deliberately boring:

    stdout, once, when the model is up:   READY <device> <compute>
    stdout, once, if it cannot load:      FAIL <one line>            (exit 1)

    then, per window -
      stdin :  a line "DETECT <n>\n", followed by exactly n bytes of
               float32 little-endian PCM at 16 kHz, mono
      stdout:  one JSON line
               {"ok": true, "lang": "ja", "prob": 0.97,
                "probs": [["ja", 0.97], ["zh", 0.01], ...]}
               {"ok": false, "why": "..."}

    stdin closed -> exit 0.

Binary on stdin rather than a temp file per window: a window is 30 seconds of
16 kHz float32, which is 1.9 MB, and a pass takes eleven of them per file.
Writing and deleting that is work the pipe does for free.

Run:  python whisper_worker.py --device cuda --compute float16 --root <dir>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import site
import struct
import sys

# The console this child would otherwise flash, and every grandchild's. Same
# patch as whisper_probe.py and paddle_worker.py, and it goes in before the
# heavy imports for the same reason: it underlies run, call and check_output,
# so one patch covers whatever CTranslate2 decides to launch.
try:
    import subprocess as _sp
    if os.name == "nt":
        _orig = _sp.Popen.__init__

        def _patched(self, *a, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000
            if not kw.get("startupinfo"):
                si = _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0                       # SW_HIDE
                kw["startupinfo"] = si
            return _orig(self, *a, **kw)
        _sp.Popen.__init__ = _patched                    # type: ignore[method-assign]
except Exception:                                        # noqa: BLE001
    pass


def _add_cuda_dirs() -> None:
    r"""Put the pip-installed CUDA DLLs where CTranslate2 will find them.

    The same two steps audiolang._add_cuda_dirs does, and for the same
    reason: ctranslate2 resolves cublas64_12.dll by plain name, which
    searches PATH, and os.add_dll_directory alone is not enough - it fails
    at the first encode with "Library cublas64_12.dll is not found or cannot
    be loaded", long after the model reports itself loaded. Both, and both
    before faster_whisper is imported.

    The PATH-growth guard the server needs is not needed here. That bug was
    about a long-lived process prepending the same six directories on every
    reload until the environment passed 32,767 characters; this process does
    it once and then exits.
    """
    dirs: list[str] = []
    for s in site.getsitepackages():
        dirs += glob.glob(os.path.join(s, "nvidia", "*", "bin"))
    if not dirs:
        return
    os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass


def _read_exactly(stream, n: int) -> bytes:
    """A pipe read returns what it has, not what was asked for."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return bytes(buf)                # closed early
        buf += chunk
    return bytes(buf)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--root", default="")
    ap.add_argument("--size", default="small")
    args = ap.parse_args()

    if args.device == "cuda":
        _add_cuda_dirs()

    out = sys.stdout
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except Exception as e:                               # noqa: BLE001
        print(f"FAIL not installed: {type(e).__name__}: {e}", flush=True)
        return 1
    try:
        model = WhisperModel(args.size, device=args.device,
                             compute_type=args.compute,
                             download_root=args.root or None)
    except Exception as e:                               # noqa: BLE001
        print(f"FAIL {type(e).__name__}: {str(e)[:200]}", flush=True)
        return 1

    print(f"READY {args.device} {args.compute}", flush=True)

    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0                          # the server let go; so do we
        try:
            head = line.decode("ascii", "replace").strip()
        except Exception:                                # noqa: BLE001
            return 0
        if not head:
            continue
        if head == "BYE":
            return 0
        if not head.startswith("DETECT "):
            print(json.dumps({"ok": False, "why": f"bad request {head[:40]!r}"}),
                  flush=True)
            continue
        try:
            n = int(head.split(" ", 1)[1])
        except (IndexError, ValueError):
            print(json.dumps({"ok": False, "why": "bad length"}), flush=True)
            continue
        raw = _read_exactly(stdin, n)
        if len(raw) < n:
            return 0                          # truncated: the pipe is gone
        try:
            a = np.frombuffer(raw, dtype="<f4")
            # frombuffer gives a read-only view onto the bytes we just read;
            # CTranslate2 wants something it can own.
            a = np.array(a, dtype=np.float32, copy=True)
            lang, prob, all_probs = model.detect_language(a)
            # Normalised HERE to a list of pairs, because faster-whisper hands
            # back either a sorted list or a dict depending on version and
            # only one of those two survives JSON with its order intact.
            items = (list(all_probs.items()) if hasattr(all_probs, "items")
                     else list(all_probs))
            probs = [[str(k), float(v)] for k, v in items]
            probs.sort(key=lambda kv: -kv[1])
            print(json.dumps({"ok": True, "lang": str(lang),
                              "prob": float(prob), "probs": probs[:16]}),
                  flush=True)
        except Exception as e:                           # noqa: BLE001
            print(json.dumps({"ok": False,
                              "why": f"{type(e).__name__}: {str(e)[:120]}"}),
                  flush=True)


if __name__ == "__main__":
    sys.exit(main())
