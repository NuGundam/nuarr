r"""nuarr - can this machine load the language model WITHOUT dying?

WHY A SEPARATE PROCESS
----------------------
faster-whisper runs on CTranslate2, which is native code compiled for a
baseline instruction set. On a CPU that does not have it - most often a VM
whose host does not pass AVX2 through - constructing the model does not raise
a Python exception. It executes an illegal instruction and the process is
terminated by Windows: exit code 0xC000001D, a WerFault.exe entry, and no
traceback anywhere, because there is no Python left to write one.

Nothing in the server can catch that. The only way to ask "will this work" and
still be alive to hear the answer is to ask in a child process. This script IS
that child: it loads the model exactly the way the server would and prints one
line. If it dies instead, the server reads the exit code and knows not to try
the same thing in its own address space.

Run:  python whisper_probe.py --device cpu --compute int8 --root <model dir>
Out:  OK <device> <compute>        (exit 0)
      FAIL <one line>              (exit 1  - an ordinary, catchable failure)
      <killed>                     (exit <0 - the case this exists for)
"""
from __future__ import annotations

import argparse
import os
import sys

# The console this child would otherwise flash, and every grandchild's.
try:
    import subprocess as _sp
    if os.name == "nt":
        _orig = _sp.Popen.__init__

        def _patched(self, *a, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000
            return _orig(self, *a, **kw)
        _sp.Popen.__init__ = _patched                     # type: ignore[method-assign]
except Exception:                                        # noqa: BLE001
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--root", default="")
    ap.add_argument("--size", default="small")
    args = ap.parse_args()
    try:
        from faster_whisper import WhisperModel
    except Exception as e:                               # noqa: BLE001
        print(f"FAIL not installed: {type(e).__name__}: {e}")
        return 1
    try:
        # The constructor is where the native code runs, so this is the whole
        # test. Nothing is transcribed: loading is what crashes.
        WhisperModel(args.size, device=args.device, compute_type=args.compute,
                     download_root=args.root or None)
    except Exception as e:                               # noqa: BLE001
        print(f"FAIL {type(e).__name__}: {str(e)[:200]}")
        return 1
    print(f"OK {args.device} {args.compute}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
