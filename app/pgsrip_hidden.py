r"""Run pgsrip with every process it spawns hidden. Windows-only concern.

WHY A WRAPPER. nuarr launches pgsrip with CREATE_NO_WINDOW, and that hides
pgsrip - but it does not govern what pgsrip launches. A STARTUPINFO does not
help either: it applies to the one process a CreateProcess call makes, not to
grandchildren (the 1.2.1 fix believed otherwise, and the consoles kept
coming). pgsrip runs mkvextract through a bare `check_output`, and pytesseract
runs tesseract - and because THIS process has no console, each console-
subsystem child gets a brand new one, which is the window flashing on the
desktop every time an OCR task starts.

The only place that can fix every descendant at once is inside pgsrip's own
process, before anything is imported: patch subprocess.Popen so every launch
carries CREATE_NO_WINDOW by default. Popen underlies run, call and
check_output, so one patch covers them all.
"""
import os
import runpy
import subprocess
import sys

if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_init = subprocess.Popen.__init__

    def _hidden_init(self, *args, **kwargs):
        kwargs["creationflags"] = (kwargs.get("creationflags") or 0) \
            | _CREATE_NO_WINDOW
        # The flag alone is not enough - some console-subsystem binaries
        # allocate a console regardless, and SW_HIDE is what stops the one
        # they take from being drawn. See paddle_worker.py.
        if not kwargs.get("startupinfo"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                  # SW_HIDE
            kwargs["startupinfo"] = si
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _hidden_init

# Everything after our own name is pgsrip's command line, untouched.
sys.argv = ["pgsrip"] + sys.argv[1:]
runpy.run_module("pgsrip", run_name="__main__")
