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


def _sniffio_stand_in() -> None:
    r"""A sniffio for an interpreter that has none - before httpcore is imported.

    httpcore asks `import sniffio` on EVERY request it makes (Sonarr, Radarr,
    Plex, Tautulli...), and with sniffio not installed that is a failed import
    each time: a fresh search of every sys.path folder, because Python does not
    remember a miss. Cheap on an idle machine; under a library scan the stall
    watch caught it holding the event loop - the whole server - for 0.5-2.3 s
    per request. This answers what sniffio would: "asyncio" inside a running
    loop, AsyncLibraryNotFoundError outside one. nuarr runs on asyncio alone.
    A real sniffio, if one is ever installed, is used instead.
    """
    try:
        import sniffio  # noqa: F401
        return
    except ImportError:
        pass
    import asyncio
    import contextvars
    import types

    class AsyncLibraryNotFoundError(RuntimeError):
        pass

    # anyio sets this around the code it runs (set_current_async_library), as
    # it does with the real sniffio; an answer set here wins, as it does there.
    cvar = contextvars.ContextVar("current_async_library_cvar", default=None)

    def current_async_library() -> str:
        v = cvar.get()
        if v is not None:
            return v
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raise AsyncLibraryNotFoundError(
                "unknown async library, or not in async context") from None
        return "asyncio"

    m = types.ModuleType("sniffio")
    m.current_async_library = current_async_library
    m.current_async_library_cvar = cvar
    m.AsyncLibraryNotFoundError = AsyncLibraryNotFoundError
    m.__nuarr_stand_in__ = True
    sys.modules["sniffio"] = m


_sniffio_stand_in()


# Windows errors that belong to ONE connection, not to the listening socket.
_CONN_ERRORS = {64, 121, 1236, 10053, 10054, 995}   # netname deleted, timeout, aborted, reset, op aborted


def _accept_keeps_listening() -> None:
    r"""One client hanging up must not close the server's listening socket.

    asyncio's proactor accept loop (BaseProactorEventLoop._start_serving)
    treats ANY OSError while accepting as fatal for the socket: it reports
    "Accept failed on a socket" and closes it. On Windows a client that drops
    its connection mid-handshake raises WinError 64 ("The specified network
    name is no longer available") there - and at 20:57 on 2026-10-18 that is
    exactly what took nuarr's 0.0.0.0:8770 listener down. localhost (the ::1
    socket) kept answering, every other device on the network got "refused".

    Same loop as the standard library's, except a per-connection error is
    logged and accepting carries on; anything else still closes the socket.
    """
    import asyncio
    import logging
    from asyncio import exceptions, proactor_events, trsock
    base = proactor_events.BaseProactorEventLoop
    log = logging.getLogger("uvicorn.error")

    def _start_serving(self, protocol_factory, sock, sslcontext=None, server=None, backlog=100,
                       ssl_handshake_timeout=None, ssl_shutdown_timeout=None):
        def loop(f=None):
            try:
                if f is not None:
                    try:
                        conn, addr = f.result()
                    except OSError as exc:
                        if getattr(exc, "winerror", None) in _CONN_ERRORS or isinstance(
                                exc, (ConnectionResetError, ConnectionAbortedError)):
                            log.warning("a client dropped its connection while it was being accepted "
                                        "(%s) - still listening", exc)
                            conn = None
                        else:
                            raise
                    if conn is not None:
                        protocol = protocol_factory()
                        if sslcontext is not None:
                            self._make_ssl_transport(
                                conn, protocol, sslcontext, server_side=True,
                                extra={"peername": addr}, server=server,
                                ssl_handshake_timeout=ssl_handshake_timeout,
                                ssl_shutdown_timeout=ssl_shutdown_timeout)
                        else:
                            self._make_socket_transport(conn, protocol, extra={"peername": addr},
                                                        server=server)
                if self.is_closed():
                    return
                f = self._proactor.accept(sock)
            except OSError as exc:
                if sock.fileno() != -1:
                    self.call_exception_handler({"message": "Accept failed on a socket",
                                                 "exception": exc, "socket": trsock.TransportSocket(sock)})
                    sock.close()
            except exceptions.CancelledError:
                sock.close()
            else:
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)
        self.call_soon(loop)

    base._start_serving = _start_serving


if sys.platform == "win32":
    try:
        _accept_keeps_listening()
    except Exception:                                    # noqa: BLE001
        pass


def _listener_watchdog(port: int) -> None:
    r"""The safety net: if the IPv4 listener is gone anyway, start over.

    Every minute, connect to 127.0.0.1 - which only the 0.0.0.0 socket serves.
    A busy server still completes the connect (the kernel queues it), so only
    a REFUSED connect means the socket itself is closed. Three in a row and
    the process exits non-zero; Task Scheduler restarts nuarr a minute later
    (RestartCount 3, RestartInterval 1 min on the nuarr task)."""
    import socket
    import threading
    import time as _t

    def watch():
        _t.sleep(120)
        refused = 0
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=10):
                    refused = 0
            except ConnectionRefusedError:
                refused += 1
                print(f"listener watchdog: 127.0.0.1:{port} refused ({refused}/3)", flush=True)
                if refused >= 3:
                    print("listener watchdog: the network listener is gone - exiting so Task "
                          "Scheduler restarts nuarr", flush=True)
                    os._exit(1)
            except Exception:                            # noqa: BLE001
                refused = 0                              # slow or busy is not gone
            _t.sleep(60)
    threading.Thread(target=watch, daemon=True, name="listener-watchdog").start()


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


def _loopback6(port: int):
    """A listener on the IPv6 loopback beside the IPv4 one, or None.

    LOCALHOST IS ::1 FIRST ON WINDOWS. With only 0.0.0.0 bound, every request
    to http://localhost:8770 tried ::1, was refused, and fell back to
    127.0.0.1 - about 200 ms added to each one, and a page making thirty
    requests a second paid it thirty times. ::1 only, not "::": this makes
    localhost answer at once without opening nuarr on any IPv6 address the
    network can reach.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind(("::1", port))
        s.listen(2048)
        s.set_inheritable(True)
        return s
    except OSError:
        return None


def main() -> None:
    import uvicorn
    from app.config import SETTINGS

    host = getattr(SETTINGS, "host", "0.0.0.0")
    port = int(getattr(SETTINGS, "port", 8770) or 8770)
    config = uvicorn.Config("app.web:app", host=host, port=port,
                            log_config=_log_config())
    six = _loopback6(port) if host in ("0.0.0.0", "127.0.0.1") else None
    if host in ("0.0.0.0", "127.0.0.1"):
        _listener_watchdog(port)
    if six is None:
        uvicorn.Server(config).run()
        return
    four = config.bind_socket()
    uvicorn.Server(config).run(sockets=[four, six])


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        traceback.print_exc()
        raise
