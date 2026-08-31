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

from .config import NO_WINDOW, SETTINGS, hidden_si

# One sample is a round trip to another machine; the panel repaints far more
# often than that. TTL is generous because spindle load is not a millisecond
# quantity, and a stale-but-labelled number beats a blank column.
# How often the far side takes a reading. A spindle's load is a real-time
# quantity and the panel repaints every second; sampling slower than that shows
# you where the array was, not where it is.
EVERY_S = 1.0
# How long one sampler lives before the reader starts a fresh one. Long enough
# that restarts are rare, short enough that a credential change, a host reboot
# or a moved library is picked up without anyone restarting nuarr.
LIFE_S = 300.0
TTL_S = 6.0
# A DCOM handshake to a busy server is not always quick, and 25s was being hit:
# the panel then replaced twelve live rows with "timed out", which is a worse
# answer than slightly old numbers. Longer ceiling, and a failure now keeps the
# last good sample rather than erasing it - see refresh().
TIMEOUT_S = 45.0
# How long a kept sample stays worth showing once it stops refreshing. Past
# this it is history, not telemetry.
STALE_S = 120.0

_LOCK = threading.Lock()
_CACHE: dict = {}          # server -> {"at": float, "disks": {...}, "why": str}
_INFLIGHT: set = set()

_PS = r"""
$ErrorActionPreference = 'Stop'
# HOW OFTEN TO SAMPLE, AND FOR HOW LONG. Set by the caller; a run that lives
# forever would outlive a settings change and a run that lives for one reading
# pays the handshake every time.
$every = [double]($env:NUARR_HOSTIO_EVERY); if (-not $every) { $every = 1.0 }
$until = [double]($env:NUARR_HOSTIO_LIFE);  if (-not $until) { $until = 300.0 }
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
  # Capacity comes from the same call that gives the label, so the table can
  # be the same table: size, used and free per spindle, not just "busy".
  # WHICH VOLUMES ARE POOL MEMBERS. Not "the ones called NU-DRIVE" - that is
  # one person's labelling and would show nothing on anyone else's machine.
  # A pooled disk is mounted WITHOUT a drive letter; the pool itself, the
  # system disk and any ordinary volume all have one. So: letterless, and with
  # a size. On this host that is exactly the twelve, and excludes C, D, E, F
  # and the P: pool volume without naming any of them.
  $vols  = Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage `
             -ClassName MSFT_Volume |
           Where-Object { -not $_.DriveLetter -and [double]$_.Size -gt 0 }
  $cap = @{}
  foreach ($v in $vols) {
    $cap[$v.Path] = @{ size  = [double]$v.Size
                       free  = [double]$v.SizeRemaining
                       label = $v.FileSystemLabel }
  }
  # DATA PARTITIONS ONLY. Letterless is the right test for "pooled", but it
  # also catches the EFI system partition and the reserved/recovery ones -
  # every disk has them, they are letterless and unlabelled, and they would
  # have appeared as "Disk 12" at 85% busy holding no space. Windows already
  # marks them: IsSystem for EFI, IsHidden for reserved. A size threshold would
  # have worked here and broken on a small pool disk somewhere else.
  $parts = Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage `
             -ClassName MSFT_Partition |
           Where-Object { -not $_.IsSystem -and -not $_.IsBoot -and -not $_.IsHidden }
  # disk number -> the name to show. The volume's own label when it has one,
  # otherwise "Disk N" from the number Windows gives it, so an unlabelled pool
  # still lists twelve distinguishable disks rather than twelve blanks.
  $map = @{}; $capByDisk = @{}
  foreach ($p in $parts) {
    foreach ($ap in @($p.AccessPaths)) {
      # A partition can report a null access path - a recovery partition, or
      # one Windows has not surfaced. ContainsKey($null) throws rather than
      # returning false, which failed the whole sample over a disk nobody
      # asked about.
      if ($ap -and $cap.ContainsKey($ap)) {
        $c = $cap[$ap]
        $name = if ($c.label) { $c.label } else { "Disk $($p.DiskNumber)" }
        $map[[string]$p.DiskNumber] = $name
        $capByDisk[[string]$p.DiskNumber] = $c
      }
    }
  }
  # SAMPLE IN A LOOP DOWN ONE SESSION. Spawning PowerShell and shaking hands
  # with the host for every reading cost a second or more before a number was
  # even asked for, which is why the panel moved in five-second steps. The
  # session, the volume list and the disk map are all worked out once; only the
  # counters are re-read, so a reading costs about as much as the wire does.
  #
  # Capacity is not re-read every second either - free space does not move at
  # that speed, and it is two more queries per tick to say so.
  $t0 = [Diagnostics.Stopwatch]::StartNew()
  while ($t0.Elapsed.TotalSeconds -lt $until) {
    $perf = Get-CimInstance -CimSession $s -ClassName Win32_PerfFormattedData_PerfDisk_PhysicalDisk |
            Where-Object { $_.Name -ne '_Total' }
    $out = @{}
    foreach ($d in $perf) {
      $idx = ($d.Name -split '\s+')[0]
      $label = $map[$idx]
      if (-not $label) { continue }
      $c = $capByDisk[$idx]
      $out[$label] = @{
        busy  = [math]::Round(100 - [double]$d.PercentIdleTime, 1)
        read  = [double]$d.DiskReadBytesPersec
        write = [double]$d.DiskWriteBytesPersec
        queue = [double]$d.CurrentDiskQueueLength
        size  = if ($c) { $c.size } else { 0 }
        free  = if ($c) { $c.free } else { 0 }
      }
    }
    # One JSON object per line, flushed, so the reader sees each tick as it
    # happens rather than when the pipe buffer decides.
    $line = @{ ok = $true; via = $via; disks = $out } | ConvertTo-Json -Depth 5 -Compress
    [Console]::Out.WriteLine($line)
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds ([int]($every * 1000))
  }
  Remove-CimSession $s
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


def _parse(line: str) -> dict:
    """One JSON tick from the sampler into the shape the panel wants."""
    try:
        d = json.loads(line)
    except Exception:                                        # noqa: BLE001
        return {}
    if not d.get("ok"):
        return {"ok": False, "why": str(d.get("why") or "")[:200]}
    disks = {}
    for label, v in (d.get("disks") or {}).items():
        try:
            disks[label] = {"busy": float(v.get("busy") or 0),
                            "read_bps": float(v.get("read") or 0),
                            "write_bps": float(v.get("write") or 0),
                            "queue": float(v.get("queue") or 0),
                            "size": float(v.get("size") or 0),
                            "free": float(v.get("free") or 0)}
        except Exception:                                    # noqa: BLE001
            continue
    return {"ok": True, "disks": disks, "via": str(d.get("via") or "")}


def _stream(server: str) -> None:
    r"""Keep one sampler alive and take its readings as they arrive.

    The old shape was a process per reading: spawn PowerShell, authenticate,
    enumerate volumes, map disks, read counters, exit - a second or more of
    setup to produce one number, which is why the panel moved in five-second
    steps and every step was already stale. Now the setup happens once and the
    loop on the far side only re-reads counters, so a tick costs about what the
    wire costs.

    Runs for LIFE_S and is restarted by the reader. A sampler that lived
    forever would outlive a credential change, a reboot of the host, or the
    library being pointed somewhere else.
    """
    user, pwd = _creds_for(server)
    env = dict(os.environ)
    env["NUARR_HOSTIO_SERVER"] = server
    env["NUARR_HOSTIO_USER"] = user or ""
    env["NUARR_HOSTIO_EVERY"] = str(EVERY_S)
    env["NUARR_HOSTIO_LIFE"] = str(LIFE_S)
    p = None
    try:
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
            env=env,
            startupinfo=hidden_si())
        p.stdin.write((pwd or "") + "\n")
        p.stdin.flush()
        started = time.time()
        got = False
        for line in p.stdout:
            line = (line or "").strip()
            if not line.startswith("{"):
                continue
            res = _parse(line)
            if not res:
                continue
            got = True
            _store(server, res)
        # The stream ended. If it never produced a reading, say why - the
        # first stderr line, never the whole thing, which can quote the
        # command back.
        if not got:
            err = ""
            try:
                err = (p.stderr.read() or "").strip().splitlines()
                err = err[0][:200] if err else ""
            except Exception:                                # noqa: BLE001
                pass
            if not err and time.time() - started >= TIMEOUT_S:
                err = f"no answer within {TIMEOUT_S:.0f}s"
            _store(server, {"ok": False, "why": err or "no answer"})
    except Exception as e:                                   # noqa: BLE001
        _store(server, {"ok": False, "why": f"{type(e).__name__}: {e}"})
    finally:
        try:
            if p:
                p.kill()
        except Exception:                                    # noqa: BLE001
            pass
        with _LOCK:
            _INFLIGHT.discard(server)


def _sample(server: str) -> dict:
    """One reading, synchronously. Kept for tests and one-off checks."""
    user, pwd = _creds_for(server)
    env = dict(os.environ)
    env["NUARR_HOSTIO_SERVER"] = server
    env["NUARR_HOSTIO_USER"] = user or ""
    env["NUARR_HOSTIO_EVERY"] = "0"
    env["NUARR_HOSTIO_LIFE"] = "0.001"      # one pass through the loop
    try:
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW,
            env=env,
            startupinfo=hidden_si())
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
        why = (err or "").strip().splitlines()
        return {"ok": False, "why": (why[0][:200] if why else "no answer")}
    return _parse(line) or {"ok": False, "why": "unreadable answer"}


def _store(server: str, res: dict) -> None:
    """Take one reading into the cache, keeping the last good one on failure."""
    now = time.time()
    with _LOCK:
        prev = _CACHE.get(server) or {}
        if res.get("ok"):
            _CACHE[server] = {"at": now, "good_at": now,
                              "disks": res.get("disks") or {},
                              "ok": True, "via": res.get("via") or "",
                              "why": ""}
        else:
            # KEEP THE LAST GOOD READING. One slow handshake used to wipe
            # twelve live rows and leave "timed out" in their place, which says
            # less than numbers a few seconds old would have. The row ages
            # visibly and disappears on its own once it is history.
            good_at = prev.get("good_at") or 0
            keep = bool(prev.get("disks")) and (now - good_at) < STALE_S
            _CACHE[server] = {
                "at": now, "good_at": good_at,
                "disks": prev.get("disks") if keep else {},
                "ok": bool(keep), "stale": bool(keep),
                "via": prev.get("via") or "",
                "why": res.get("why") or ""}


def refresh(server: str) -> None:
    """Make sure a sampler is running for this host. One, never two."""
    with _LOCK:
        if server in _INFLIGHT:
            return
        _INFLIGHT.add(server)
    threading.Thread(target=_stream, args=(server,),
                     name=f"hostio-{server}", daemon=True).start()


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
        running = server in _INFLIGHT
    # The sampler pushes readings in on its own, so the only thing to check is
    # that one is alive - not whether the last number is old.
    if not running:
        refresh(server)
    return dict(hit or {})


def infer_viewers(disks: dict) -> dict:
    r"""Guess which spindle each viewer is reading from. A GUESS, and labelled.

    WHY IT CANNOT BE KNOWN. DrivePool presents a virtual volume and reports its
    own serial through the share: the same file opened via P:\, via the share
    and via its PoolPart copy gives 092120B4, 092120B4 and 68F9900E. Two of
    those are the pool. So nothing the filesystem returns over a share names
    the disk - it is hidden by design, not by permission.

    WHAT CAN BE SAID. Plex reports what each session is playing and at what
    bitrate. A stream at 15,784 kbps is about 2.0 MB/s off a disk, and if one
    spindle is reading at about that rate while that stream plays, it is very
    probably the one. That is inference, so it is reported as inference - the
    same footing as the panel's "looks like data moving" pairing.

    Deliberately conservative: a disk must be reading within a wide band of the
    expected rate, and one disk is claimed per session, best match first. Plex
    reads ahead in bursts, so the band is generous upward and tight downward -
    a disk reading far LESS than the stream needs cannot be feeding it.
    """
    out = {}
    try:
        from . import gate
        live = [s for s in (gate.plex_live() or [])
                if float(s.get("kbps") or 0) > 0]
    except Exception:                                        # noqa: BLE001
        return out
    if not live or not disks:
        return out
    cands = {k: float(v.get("read_bps") or 0) for k, v in disks.items()}
    taken = set()
    # Biggest stream first: it has the strongest signal and the most to lose
    # from being assigned a disk that a smaller one explains better.
    for s in sorted(live, key=lambda x: -float(x.get("kbps") or 0)):
        want = float(s["kbps"]) * 1000.0 / 8.0          # kbps -> bytes/sec
        best, best_err = None, None
        for label, rd in cands.items():
            if label in taken or rd <= 0:
                continue
            if rd < want * 0.6 or rd > want * 8.0:
                continue
            err = abs(rd - want) / want
            if best_err is None or err < best_err:
                best, best_err = label, err
        if best:
            taken.add(best)
            out[best] = {"kbps": float(s["kbps"]),
                         "bps": cands[best],
                         "user": str(s.get("user") or ""),
                         "title": str(s.get("title") or ""),
                         "err": round(best_err, 3),
                         "inferred": True}
    return out


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
            "stale": bool(d.get("stale")),
            "age_s": (round(time.time() - d["good_at"], 1)
                      if d.get("good_at") else None),
            "disks": d.get("disks") or {},
            "viewers": infer_viewers(d.get("disks") or {}),
        })
    return out
