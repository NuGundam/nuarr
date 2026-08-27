r"""
nuarr - launcher

WHY THIS EXISTS INSTEAD OF start-nuarr.cmd
------------------------------------------
The scheduled task used to run:

    cmd.exe /c "C:\nuarr\start-nuarr.cmd"

and cmd.exe is a CONSOLE application. Task Scheduler therefore gave it a
console window at logon - the black C:\Windows\System32\cmd.exe window, with
its conhost.exe, sitting on the desktop for as long as nuarr ran. Task Manager
showed the whole family: "Windows Command Processor (3)" = cmd.exe + Console
Window Host + pythonw.exe.

CREATE_NO_WINDOW does not help here. That flag applies to processes WE spawn;
this console is created by Task Scheduler for the launcher itself, before any
of our code runs.

The shell was only ever there to redirect stdout, because pythonw.exe has no
stdout of its own and uvicorn logs to it. Doing the redirect in Python removes
the reason for the shell:

    Execute:  pythonw.exe          <- GUI subsystem, never gets a console
    Argument: C:\nuarr\launch.py

No cmd.exe, no conhost.exe, no window.

LOGGING
-------
server.out grew without bound - 1.1 MB of uvicorn access lines and climbing,
since a shell '>>' redirect has no notion of rotation. uvicorn's logs now go
through a RotatingFileHandler (capped, with backups), while raw stdout/stderr
are pointed at a separate small file so that a traceback thrown outside the
logging system - the kind that kills the process at startup - still lands
somewhere instead of vanishing into pythonw's missing stdout.
"""
from __future__ import annotations

import os
import sys
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("NUARR_LOGS", r"C:\ProgramData\nuarr\logs")
os.makedirs(LOG_DIR, exist_ok=True)

SERVER_LOG = os.path.join(LOG_DIR, "server.log")     # uvicorn, rotated
STDERR_LOG = os.path.join(LOG_DIR, "server.out")     # bare tracebacks only

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

# --- give pythonw the stdout/stderr it does not have -----------------------
# Line buffered: a traceback that is followed by the process dying must already
# be on disk. This is the same lesson as the -u flag on the old command line;
# an unflushed buffer is why a fatal error once looked like "no error at all".
_raw = open(STDERR_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
sys.stdout = _raw
sys.stderr = _raw

# The old server.out was an append-only access log with no cap. It is now only
# used for stray output, so trim it once at startup if it arrived here large.
try:
    if os.path.getsize(STDERR_LOG) > MAX_BYTES:
        _raw.close()
        os.replace(STDERR_LOG, STDERR_LOG + ".1")
        _raw = open(STDERR_LOG, "a", buffering=1, encoding="utf-8",
                    errors="replace")
        sys.stdout = _raw
        sys.stderr = _raw
except OSError:
    pass

os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _log_config() -> dict:
    """uvicorn logging, but to a rotating file rather than a console."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "std": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "file": {
                "()": RotatingFileHandler,
                "filename": SERVER_LOG,
                "maxBytes": MAX_BYTES,
                "backupCount": BACKUPS,
                "encoding": "utf-8",
                "formatter": "std",
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["file"], "level": "INFO",
                               "propagate": False},
            "uvicorn.error":  {"handlers": ["file"], "level": "INFO",
                               "propagate": False},
            # the access log is the noisy one - every poll from the web UI
            "uvicorn.access": {"handlers": ["file"], "level": "WARNING",
                               "propagate": False},
            # httpx logs a line PER REQUEST at INFO, and nuarr polls Sonarr,
            # Radarr and the ffmpeg release feed continuously - that is a few
            # thousand lines an hour of "HTTP/1.1 200 OK" saying nothing. Left
            # at INFO it rotates the log out from under any real error.
            "httpx":     {"handlers": ["file"], "level": "WARNING",
                          "propagate": False},
            "httpcore":  {"handlers": ["file"], "level": "WARNING",
                          "propagate": False},
        },
        "root": {"handlers": ["file"], "level": "INFO"},
    }


def main() -> None:
    import uvicorn
    from app.config import SETTINGS

    uvicorn.run(
        "app.web:app",
        host=getattr(SETTINGS, "host", "0.0.0.0"),
        port=int(getattr(SETTINGS, "port", 8770) or 8770),
        log_config=_log_config(),
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        raise
