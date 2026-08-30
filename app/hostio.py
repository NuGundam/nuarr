r"""Disk activity for a pool nuarr reaches over the network.

THE PROBLEM. When the media lives on another machine, nuarr sees one thing: a
share. Windows' own counters describe the client's hardware, so the Files per
pool disk panel collapses from twelve spindles with viewer/nuarr/system splits
down to a single row that can say nothing about what is busy. The information
exists - it is just on the other end of the wire.

WHAT THIS DOES. Asks the host for its PhysicalDisk counters over CIM, and
labels them the way nuarr labels its own disks. The counters name disks by
index (0, 1, 2) because pool disks are letterless and have no drive letter to
report; MSFT_Partition knows which disk a volume sits on and MSFT_Volume knows
its label, so the join turns "disk 4 is 98% busy" into "NU-DRIVE-4 is 98%
busy" - the name every other part of nuarr already uses.

WHAT IT DELIBERATELY DOES NOT DO. It cannot say whose I/O it is. On the host,
nuarr knows which bytes are its own because it started them, and which are a
viewer because Plex told it; from outside, a busy spindle is just busy. The
panel says so rather than inventing a split, because a made-up attribution is
worse than an honest total.

THE PASSWORD NEVER TOUCHES A COMMAND LINE. Command lines are readable by any
process on the machine. The credential comes from the same config.yml entry
`net use` already uses, and is handed to PowerShell down stdin.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

from .config import NO_WINDOW, SETTINGS

# One sample is a round trip to another machine; the panel repaints far more
# often than that. TTL is generous because spindle load is not a millisecond
# quantity, and a stale-but-labelled number beats a blank column.
TTL_S = 6.0
TIMEOUT_S = 25.0

_LOCK = threading.Lock()
_CACHE: dict = {}          # server -> {"at": float, "disks": {...}, "why": str}
_INFLIGHT: set = set()

_PS = r"""
$ErrorActionPreference = 'Stop'
# SERVER AND USER COME THROUGH THE ENVIRONMENT, not as arguments: with
# powershell -Command <script>, a trailing -args is swallowed into the command
# string and $args arrives empty, which fails as "ComputerName is null" and
# looks exactly like an unreachable host. The password still comes down stdin,
# because environment blocks are readable and command lines doubly so.
$server = $env:NUARR_HOSTIO_SERVER
$user   = $env:NUARR_HOSTIO_USER
$pw = [Console]::In.ReadLine()
try {
  $cred = $null
  if ($user) {
    $sec  = ConvertTo-SecureString $pw -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential($user, $sec)
  }
  # WSMAN FIRST, DCOM WHEN IT REFUSES. WinRM will not carry NTLM to a machine
  # that is not domain-joined unless TrustedHosts names it or the transport is
  # HTTPS - the exact refusal a workgroup server gives, and not something worth
  # loosening a security setting to satisfy. CIM speaks DCOM as well, which
  # needs neither, so the modern transport is tried and the older one catches
  # the case it cannot serve. Which one answered is reported, because "it
  # works" and "it works over DCOM" are different facts about a network.
  $s = $null; $via = ''
  $mk = {
    param($opt, $name)
    try {
      $sess = if ($cred) {
        New-CimSession -ComputerName $server -Credential $cred -SessionOption $opt -OperationTimeoutSec 15 -ErrorAction Stop
      } else {
        New-CimSession -ComputerName $server -SessionOption $opt -OperationTimeoutSec 15 -ErrorAction Stop
      }
      $script:s = $sess; $script:via = $name
    } catch { }
  }
  & $mk (New-CimSessionOption -Protocol Wsman) 'WinRM'
  if (-not $s) { & $mk (New-CimSessionOption -Protocol Dcom) 'DCOM' }
  if (-not $s) { throw "could not open a CIM session over WinRM or DCOM" }
  $vols  = Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage `
             -ClassName MSFT_Volume | Where-Object { $_.FileSystemLabel }
  $parts = Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage `
             -ClassName MSFT_Partition
  $map = @{}
  foreach ($p in $parts) {
    foreach ($ap in @($p.AccessPaths)) {
      $v = $vols | Where-Object { $_.Path -eq $ap }
      if ($v) { $map[[string]$p.DiskNumber] = $v.FileSystemLabel }
    }
  }
  $perf = Get-CimInstance -CimSession $s -ClassName Win32_PerfFormattedData_PerfDisk_PhysicalDisk |
          Where-Object { $_.Name -ne '_Total' }
  $out = @{}
  foreach ($d in $perf) {
    $idx = ($d.Name -split '\s+')[0]
    $label = $map[$idx]
    if (-not $label) { continue }
    $out[$label] = @{
      busy  = [math]::Round(100 - [double]$d.PercentIdleTime, 1)
      read  = [double]$d.DiskReadBytesPersec
      write = [double]$d.DiskWriteBytesPersec
      queue = [double]$d.CurrentDiskQueueLength
    }
  }
  Remove-CimSession $s
  @{ ok = $true; via = $via; disks = $out } | ConvertTo-Json -Depth 5 -Compress
} catch {
  @{ ok = $false; why = $_.Exception.Message } | ConvertTo-Json -Compress
}
"""


def server_of(path: str) -> str:
    r"""The host in a UNC path. '' for a local one.

    \\192.168.0.176\P\Anime Movies -> 192.168.0.176
    """
    m = re.match(r"^\\\\([^\\/]+)", str(path or ""))
    return m.group(1) if m else ""


def servers() -> list:
    """Every host nuarr reaches a library through, deduplicated."""
    out = []
    try:
        for lib in (SETTINGS.libraries or []):
            p = lib.get("path") if isinstance(lib, dict) else getattr(lib, "path", None)
            s = server_of(p or "")
            if s and s not in out:
                out.append(s)
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _creds_for(server: str) -> tuple:
    r"""(user, password) for this host, from the entry `net use` already uses.

    A SECOND PLACE FOR SECRETS IS A SECOND PLACE TO LEAK THEM, so this reads
    the existing net_shares entry rather than adding a setting of its own. An
    empty user means "try as whoever the service runs as", which is what
    happens on a machine that is already domain-joined or has a session open.
    """
    try:
        for e in (SETTINGS.net_shares or []):
            if str(e.get("server", "")).strip().lower() == server.lower():
                return str(e.get("username") or ""), str(e.get("password") or "")
    except Exception:                                        # noqa: BLE001
        pass
    return "", ""


def _sample(server: str) -> dict:
    """One round trip. Blocking; callers use refresh() instead."""
    user, pwd = _creds_for(server)
    env = dict(os.environ)
    env["NUARR_HOSTIO_SERVER"] = server
    env["NUARR_HOSTIO_USER"] = user or ""
    try:
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
            env=env)
        out, err = p.communicate(input=(pwd or "") + "\n", timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except Exception:                                    # noqa: BLE001
            pass
        return {"ok": False, "why": f"timed out after {TIMEOUT_S:.0f}s"}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    line = ""
    for ln in (out or "").splitlines():
        if ln.strip().startswith("{"):
            line = ln.strip()
    if not line:
        # NEVER echo `err` wholesale - it can quote the command line back. The
        # first line is the message; the rest is a PowerShell stack trace that
        # helps nobody reading a settings page.
        why = (err or "").strip().splitlines()
        return {"ok": False, "why": (why[0][:200] if why else "no answer")}
    try:
        d = json.loads(line)
    except Exception:                                        # noqa: BLE001
        return {"ok": False, "why": "unreadable answer"}
    if not d.get("ok"):
        return {"ok": False, "why": str(d.get("why") or "")[:200]}
    disks = {}
    for label, v in (d.get("disks") or {}).items():
        try:
            disks[label] = {"busy": float(v.get("busy") or 0),
                            "read_bps": float(v.get("read") or 0),
                            "write_bps": float(v.get("write") or 0),
                            "queue": float(v.get("queue") or 0)}
        except Exception:                                    # noqa: BLE001
            continue
    return {"ok": True, "disks": disks, "via": str(d.get("via") or "")}


def refresh(server: str) -> None:
    """Sample in the background. One in flight per host, never two."""
    with _LOCK:
        if server in _INFLIGHT:
            return
        _INFLIGHT.add(server)

    def run():
        res = _sample(server)
        with _LOCK:
            _CACHE[server] = {"at": time.time(),
                              "disks": res.get("disks") or {},
                              "ok": bool(res.get("ok")),
                              "via": res.get("via") or "",
                              "why": res.get("why") or ""}
            _INFLIGHT.discard(server)

    threading.Thread(target=run, name=f"hostio-{server}", daemon=True).start()


def disks(server: str = "") -> dict:
    r"""Latest per-disk load for a host, refreshing behind the caller.

    NEVER BLOCKS THE PAGE. The first call returns nothing and starts a sample;
    the panel picks it up on its next repaint a second later. A settings page
    that stalls for a round trip to another machine is worse than one that
    fills in a moment late.
    """
    if not server:
        srv = servers()
        if not srv:
            return {}
        server = srv[0]
    with _LOCK:
        hit = _CACHE.get(server)
    if not hit or time.time() - hit["at"] > TTL_S:
        refresh(server)
    return dict(hit or {})


def state() -> dict:
    """Everything the UI needs, for every host a library lives on."""
    out = {"hosts": [], "local": True}
    for s in servers():
        d = disks(s)
        out["local"] = False
        user, _ = _creds_for(s)
        out["hosts"].append({
            "server": s,
            "have_creds": bool(user),
            "ok": bool(d.get("ok")),
            "via": d.get("via") or "",
            "why": d.get("why") or "",
            "age_s": round(time.time() - d["at"], 1) if d.get("at") else None,
            "disks": d.get("disks") or {},
        })
    return out
