r"""Should THIS nuarr be changing files, or only watching them?

WHERE THIS CAME FROM. Erik runs a second nuarr on a sandbox VM that reaches
the main machine's pool over a share. Comparing the two side by side showed
the sandbox had quietly queued twelve jobs - five transcodes and seven
subtitle OCRs - against files the host's nuarr had already processed and
marked done. Processing state lives in each machine's own database, so a
second nuarr pointed at the same library sees fresh files and will happily
redo all of them, over SMB, into a pool another program is actively managing.

Two nuarrs writing one library is not a configuration anyone means to run.
But a second nuarr WATCHING that library is genuinely useful - every panel,
the viewer system, the comparisons - and it is exactly what the sandbox is
for. So the machine decides which kind it is:

  auto      observe when the library lives on another machine AND that machine
            is running nuarr (it answers on its own port). Full otherwise.
            This is the default, and it is deliberately conservative: a share
            with no nuarr behind it - a NAS, say - still gets processed,
            because there is nobody else to defer to.
  observe   never queue work, regardless of what is detected.
  full      queue work even on another nuarr's pool. This is Erik's "real
            mode": the sandbox exists to test the whole system, and sometimes
            the whole system includes the queue. The card that announces
            observer mode says loudly when this override is on, because two
            machines processing one library is safe only while a person is
            watching it happen.

WHAT OBSERVING BLOCKS, AND WHAT IT DOES NOT. The one choke point is the
dispatch loop: jobs are still discovered, still counted, still listed - the
queue fills and the panels describe it - but nothing is ever claimed, so
nothing runs. Blocking discovery too would have made every panel lie about
what WOULD happen; an observer that cannot tell you what it would do is not
observing much.
"""
from __future__ import annotations

import time

from .config import SETTINGS

# The peer probe is cached: it is one HTTP round trip, and the answer changes
# when someone installs or removes nuarr on the file server, which is to say
# almost never. Ten minutes keeps a wrong answer from outliving a reinstall by
# much while costing one request per interval.
_PROBE: dict = {"at": 0.0, "peer": False, "server": ""}
_PROBE_TTL = 600.0


def mode() -> str:
    """"auto", "observe" or "full". Anything unrecognised is auto."""
    m = str(getattr(SETTINGS, "observer_mode", "auto") or "auto").lower()
    return m if m in ("auto", "observe", "full") else "auto"


def _peer_nuarr() -> tuple[bool, str]:
    """Is the machine holding the library running nuarr itself?"""
    now = time.time()
    if now - _PROBE["at"] < _PROBE_TTL:
        return _PROBE["peer"], _PROBE["server"]
    peer, server = False, ""
    try:
        from . import hostio
        for srv in hostio.servers():
            try:
                import urllib.request
                with urllib.request.urlopen(
                        f"http://{srv}:8770/api/version", timeout=5) as r:
                    if r.status == 200:
                        peer, server = True, srv
                        break
            except Exception:                                # noqa: BLE001
                continue
    except Exception:                                        # noqa: BLE001
        pass
    _PROBE.update(at=now, peer=peer, server=server)
    return peer, server


def observing() -> bool:
    m = mode()
    if m == "observe":
        return True
    if m == "full":
        return False
    peer, _ = _peer_nuarr()
    return peer


def state() -> dict:
    """Everything the page needs to explain itself."""
    m = mode()
    peer, server = _peer_nuarr()
    obs = observing()
    if m == "observe":
        why = "set to observe - this machine never changes files"
    elif m == "full":
        why = (f"REAL MODE on another nuarr's pool ({server}) - both machines "
               f"can process the same files" if peer else
               "full - the library is this machine's own")
    elif obs:
        why = (f"the library lives on {server}, which runs its own nuarr - "
               f"that machine owns the processing, this one watches")
    else:
        try:
            from . import hostio
            remote = bool(hostio.servers())
        except Exception:                                    # noqa: BLE001
            remote = False
        # A NAS is not a peer. A share with no nuarr behind it has nobody to
        # defer to, and saying "this machine's own" about it would be wrong in
        # a way that matters to anyone debugging why auto chose to process.
        why = ("the library is on a network share with no nuarr behind it - "
               "nobody else will process it, so this machine does"
               if remote else
               "the library is this machine's own, so it processes normally")
    return {"mode": m, "observing": obs, "peer_nuarr": peer,
            "server": server, "why": why}
