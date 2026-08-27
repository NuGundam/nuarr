r"""
nuarr - restart and shutdown

Stopping a media transcoder is not like stopping a web app: a kill mid-commit
is how a file ends up with a .nuarr-bak and .nuarr-new beside it and no real
file, which happened here once already and is why
fileops.recover_interrupted_commits() exists.

So both actions here are DELIBERATE and staged:

  1. stop taking new work (the dispatcher pauses immediately);
  2. optionally wait for in-flight jobs to reach a safe point;
  3. only then exit.

A running ffmpeg is not itself dangerous - it writes to the cache, and an
abandoned cache file is swept at startup. The dangerous window is the commit,
which is seconds long. Waiting for idle avoids it entirely.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

from . import joblog
from .config import NO_WINDOW
from . import schedules

# Set when a stop has been requested, so the UI can show it and the dispatcher
# can refuse new work.
PENDING: dict = {"action": "", "since": 0.0, "wait_for_idle": False,
                 "force": False}


def request(action: str, wait_for_idle: bool = True, force: bool = False) -> dict:
    PENDING.update(action=action, since=time.time(),
                   wait_for_idle=wait_for_idle, force=force)
    joblog.log(f"{action} requested"
               + (" - waiting for running jobs to finish" if wait_for_idle
                  else " - stopping without waiting"), "warn")
    return status()


def cancel() -> dict:
    if PENDING["action"]:
        joblog.log(f"{PENDING['action']} cancelled", "ok")
    PENDING.update(action="", since=0.0, wait_for_idle=False, force=False)
    return status()


def status() -> dict:
    from . import jobs
    return {"action": PENDING["action"], "since": PENDING["since"],
            "wait_for_idle": PENDING["wait_for_idle"],
            "running_jobs": len(jobs.RUNNING),
            "waiting_on": [w.job.title for w in jobs.RUNNING.values()][:8]}


def _spawn_replacement() -> None:
    r"""Start a fresh server that waits for this one to release the port.

    Windows will not let the new process bind 8770 until this one has exited,
    and this one cannot wait for that after it has exited - so the child does
    the waiting: it polls the port, then starts the server. DETACHED_PROCESS
    keeps it alive once the parent is gone.

    The replacement goes through launch.py under pythonw, NOT through uvicorn
    directly and NOT through a .cmd. Two reasons, both learned the hard way:

      * launch.py is what gives the process usable stdout/stderr and rotating
        logs. pythonw.exe has no stdout of its own, so a directly-spawned
        uvicorn inherits nothing and its output - including a startup
        traceback - is discarded. Restarting used to silently cost the log
        until the next logon.

      * a .cmd launcher means cmd.exe, which is a CONSOLE application and gets
        a visible black window. That is the window that sat on the desktop the
        whole time nuarr was running.
    """
    port = int(getattr(__import__("app.config", fromlist=["SETTINGS"]).SETTINGS,
                       "port", 8770) or 8770)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.join(root, "launch.py")
    # Prefer Nuarr.exe so a restarted server keeps its identity and icon in Task
    # Manager rather than reverting to a generic "Python" (built by
    # tools/make-nuarr-exe.py). Then pythonw, never python: python.exe is a
    # CONSOLE application, so restarting through it would pop the very window
    # the launcher exists to avoid.
    pyw = sys.executable
    base = os.path.basename(pyw).lower()
    if base != "nuarr.exe":                      # NTFS compare is case-insensitive
        for name in ("Nuarr.exe", "pythonw.exe"):
            cand = os.path.join(os.path.dirname(pyw), name)
            if os.path.exists(cand):
                pyw = cand
                break

    if os.path.exists(launcher):
        start = ("subprocess.Popen([r'%s', r'%s'], cwd=r'%s', "
                 "creationflags=0x08000000|0x00000008)\n"
                 % (pyw, launcher, root))
    else:
        # no launcher (dev checkout): fall back to uvicorn, but keep the log
        start = (
            "import os\n"
            "os.makedirs(r'C:\\ProgramData\\nuarr\\logs', exist_ok=True)\n"
            "f=open(r'C:\\ProgramData\\nuarr\\logs\\server.out','ab')\n"
            "subprocess.Popen([r'%s','-u','-m','uvicorn','app.web:app',"
            "'--host','0.0.0.0','--port','%d'], cwd=r'%s',"
            "stdout=f, stderr=f, creationflags=0x08000000)\n"
            % (pyw, port, root)
        )

    waiter = (
        "import socket,subprocess,sys,time\n"
        f"for _ in range(120):\n"
        f"    s=socket.socket()\n"
        f"    try:\n"
        f"        s.bind(('127.0.0.1',{port})); s.close(); break\n"
        f"    except OSError:\n"
        f"        s.close(); time.sleep(1)\n"
    ) + start

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | \
                NO_WINDOW
    subprocess.Popen([sys.executable, "-c", waiter], cwd=root,
                     creationflags=flags, close_fds=True)


async def watch() -> None:
    """Carry out a pending restart/shutdown once it is safe."""
    from . import jobs

    while True:
        schedules.beat('lifecycle')
        await asyncio.sleep(3)
        act = PENDING["action"]
        if not act:
            continue
        running = len(jobs.RUNNING)
        if running and PENDING["wait_for_idle"] and not PENDING["force"]:
            continue                       # still working - keep waiting
        if running:
            joblog.log(f"{act} proceeding with {running} job(s) still running "
                       f"- they will be requeued on start", "warn")
        joblog.log(f"{act} now", "warn")
        await asyncio.sleep(0.5)           # let the log line flush to disk
        if act == "restart":
            _spawn_replacement()
        # os._exit skips atexit handlers and any half-finished teardown; the
        # queue is in SQLite and interrupted jobs are recovered on start, so a
        # clean exit buys nothing and a hung teardown would leave the port bound.
        os._exit(0)
