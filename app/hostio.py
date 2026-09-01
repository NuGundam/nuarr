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
  # WHICH VOLUMES ARE REAL DEVICES - decided by physics, not by lettering.
  #
  # This used to select letterless volumes, on the reasoning that a pooled disk
  # has no drive letter while the pool, the system disk and ordinary volumes
  # do. True on a StableBit host and useless anywhere else: a server sharing a
  # plain JBOD, a RAID volume or a pair of lettered data drives has no
  # letterless volumes at all, so the panel would have shown nothing.
  #
  # The general test is the one storage.py uses locally: A VOLUME CANNOT BE
  # LARGER THAN THE HARDWARE BEHIND IT. A pool reports far more space than the
  # disk it presents itself through; a real volume matches its disks. So take
  # every volume that is NOT bigger than its backing hardware - that is the set
  # of things with actual spindles, whether they carry a letter or not.
  $diskSize = @{}
  foreach ($d in (Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage -ClassName MSFT_Disk)) {
    $diskSize[[string]$d.Number] = [double]$d.Size
  }
  $allVols = Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage `
               -ClassName MSFT_Volume |
             Where-Object { [double]$_.Size -gt 0 }
  $allParts = Get-CimInstance -CimSession $s -Namespace root/Microsoft/Windows/Storage `
                -ClassName MSFT_Partition
  # volume access path -> the disks under it, so each volume can be measured
  # against its own hardware rather than against a guess.
  $backing = @{}
  foreach ($p in $allParts) {
    foreach ($ap in @($p.AccessPaths)) {
      if ($ap) {
        if (-not $backing.ContainsKey($ap)) { $backing[$ap] = @{} }
        $backing[$ap][[string]$p.DiskNumber] = $true
      }
    }
  }
  # WHICH LETTERED VOLUMES ARE ACTUALLY SERVING MEDIA. Selecting every real
  # volume was too broad: it added the host's transcode drive, staging drive
  # and general storage to a panel that exists to show where the LIBRARY lives.
  # Those are real disks doing real work and none of them hold a file Plex or
  # an arr will ever ask for.
  #
  # A shared path is the honest test for "this machine serves media from here",
  # and it is what makes the rule work on setups that are not pools: a server
  # sharing a plain D: and E: has no letterless volumes at all, and those two
  # are exactly what should appear.
  # THE SHARES NUARR'S LIBRARIES ACTUALLY COME THROUGH, passed in by the
  # caller - not "anything shared", which was the first attempt and is wrong
  # twice over. Every Windows machine publishes admin shares (C$, D$, P$), so
  # "is it shared" was true of every lettered volume including the OS drive;
  # and a machine can share plenty that has no media on it. The libraries name
  # the shares they use, so those are the only ones that count.
  $wantShares = @{}
  foreach ($n in ($env:NUARR_HOSTIO_SHARES -split '\|')) {
    if ($n) { $wantShares[$n.Trim().ToUpper()] = $true }
  }
  $sharedRoots = @{}
  foreach ($sh in (Get-CimInstance -CimSession $s -ClassName Win32_Share |
                   Where-Object { $_.Path -and $_.Path -match '^[A-Za-z]:' -and
                                  $_.Name -notmatch '\$$' })) {
    if ($wantShares.Count -eq 0 -or $wantShares.ContainsKey($sh.Name.ToUpper())) {
      $sharedRoots[$sh.Path.Substring(0,2).ToUpper()] = $true
    }
  }
  $vols = @()
  foreach ($v in $allVols) {
    $sum = 0.0
    if ($v.Path -and $backing.ContainsKey($v.Path)) {
      foreach ($dn in $backing[$v.Path].Keys) { $sum += [double]$diskSize[$dn] }
    }
    # Virtual when the volume claims more than its disks hold. Also skipped
    # when nothing backs it at all, which is the same situation reported a
    # different way.
    if (-not ($sum -gt 0 -and [double]$v.Size -le ($sum * 1.05))) { continue }
    if ($v.DriveLetter) {
      # Lettered: only if this machine shares it. A pool's members carry no
      # letter and are kept regardless, which is what makes the members show
      # while the pool volume itself does not.
      $dl = ([string]$v.DriveLetter).ToUpper() + ':'
      if (-not $sharedRoots.ContainsKey($dl)) { continue }
    }
    $vols += $v
  }
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


def shares_on(server: str) -> list:
    r"""The share names this machine's libraries come through, for one server.

    The remote sampler uses these to decide which of the host's lettered
    volumes are serving media. Without them it would have to guess, and the two
    obvious guesses are both wrong: "anything shared" catches the admin shares
    every Windows box publishes, and "anything with a letter" catches the OS.
    """
    out = []
    try:
        for lib in (SETTINGS.libraries or []):
            p = (lib.get("path") if isinstance(lib, dict)
                 else getattr(lib, "path", None)) or ""
            if server_of(p) != server:
                continue
            q = p.replace("/", "\\")
            if q.startswith("\\\\"):
                bits = q[2:].split("\\")
                if len(bits) > 1 and bits[1] and bits[1] not in out:
                    out.append(bits[1])
    except Exception:                                        # noqa: BLE001
        pass
    return out


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
    env["NUARR_HOSTIO_SHARES"] = "|".join(shares_on(server))
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
    env["NUARR_HOSTIO_SHARES"] = "|".join(shares_on(server))
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


# FILE COUNTS PER DISK, ASKED RATHER THAN WALKED.
#
# The panel's FILES column is blank on a machine that reaches the pool over a
# share, and it cannot be filled the obvious way: counting means walking, and
# walking one of these disks locally reached 114,894 entries in 25 seconds
# without finishing. Over SMB that is the same walk the remote scan is already
# stuck doing - it is why the header said 252 files.
#
# But the machine with the disks attached has already done that work. If it is
# also running nuarr it will answer with a count per disk in one request, and
# the numbers are the same numbers by construction because they come from the
# same scan the panel would have been showing locally.
#
# Entirely optional. No nuarr at the other end, or a different port, and the
# column stays as blank as it is today - nothing else changes and nothing
# waits on it.
_PEER: dict = {}          # server -> {"at": float, "counts": {disk: n}}
_PEER_TTL = 300.0


_PEER_SESS: dict = {}
_PEER_SESS_TTL = 8.0


def peer_sessions(server: str) -> dict:
    r"""session key -> disk, from the nuarr that can actually see the files.

    THE SAME ARGUMENT AS THE FILE COUNTS, APPLIED TO A HARDER QUESTION. Over a
    share nothing reports which spindle holds a file, so this machine can only
    correlate bitrates against read rates - and that is a guess with two failure
    modes seen on Erik's box within one evening:

      A PAUSED STREAM READS NOTHING, so there is nothing to correlate and it is
      never placed at all. Measured: the host had erikh11 paused on NU-DRIVE-6;
      the sandbox reported "playing, but nothing here says which disk".

      A BUSY DISK LOOKS LIKE A VIEWER. A rebalance reading 1.3 MB/s off
      NU-DRIVE-1 is indistinguishable, by rate alone, from someone watching at
      1.3 MB/s - so the sandbox put a viewer on NU-DRIVE-1 while the host,
      reading the file's own location, said NU-DRIVE-6.

    The host resolves every session exactly, through the storage layer, and it
    is already answering /api/plex/sessions. One request replaces the whole
    correlation with a fact. Silent and absent when there is no nuarr at the
    other end - the guess is still there as the fallback.

    Eight seconds, because unlike a file count this changes when somebody
    presses pause.
    """
    now = time.time()
    ent = _PEER_SESS.get(server)
    if ent and now - ent["at"] < _PEER_SESS_TTL:
        return ent["map"]
    out: dict = {}
    try:
        import urllib.request
        url = f"http://{server}:8770/api/plex/sessions"
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.load(r)
        rows = d.get("sessions") if isinstance(d, dict) else d
        for s in (rows or []):
            k = str(s.get("key") or "").strip()
            disk = str(s.get("disk") or "").strip()
            if k and disk:
                out[k] = disk
    except Exception:                                        # noqa: BLE001
        out = {}
    _PEER_SESS[server] = {"at": now, "map": out}
    return out


def peer_counts(server: str) -> dict:
    """disk -> file count, from a nuarr running on the machine that has them."""
    now = time.time()
    ent = _PEER.get(server)
    if ent and now - ent["at"] < _PEER_TTL:
        return ent["counts"]
    counts: dict = {}
    try:
        import urllib.request
        url = f"http://{server}:8770/api/summary"
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.load(r)
        for row in (d.get("disks") or []):
            lbl = row.get("pool_disk") or row.get("disk")
            if lbl and row.get("n") is not None:
                counts[lbl] = int(row["n"] or 0)
    except Exception:                                        # noqa: BLE001
        # Silent: a host that is not running nuarr is the normal case, not a
        # fault, and logging it every five minutes would be noise.
        counts = {}
    _PEER[server] = {"at": now, "counts": counts}
    return counts


# ONE ANSWER PER SESSION, HELD UNTIL THE SESSION ENDS.
#
# WHY A CORRECT ANSWER KEPT DISAPPEARING. The rate match is a photograph of one
# instant, and Plex does not read continuously - it pulls thirty seconds of
# video and goes quiet. Sampled during a lull the disk reads nothing, the match
# finds nothing, and a viewer that was placed a second ago becomes "playing,
# but nothing here says which disk". Measured on the sandbox with two streams
# running: one sample had every disk under 200 KB/s and both viewers unplaced;
# six seconds later NU-DRIVE-7 was reading 3.14 MB/s and NU-DRIVE-1 2.09 MB/s,
# which is exactly the two streams. The evidence comes and goes; the fact does
# not.
#
# A PAUSED STREAM IS THE SAME PROBLEM MADE PERMANENT. It reads nothing at all,
# so there is no rate to match for as long as it stays paused - which is
# precisely when you most want the row to say who is holding that disk. Without
# a memory, "paused" is indistinguishable from "gone".
#
# So the first placement is kept and reused for the life of the Plex session,
# and re-derived only when the session's FILE changes - the session key
# survives moving to the next episode, and the next episode can be on another
# spindle.
_VIEWER_LOCK: dict = {}
_LOCK_MAX_ERR = 0.5      # a loose guess is not worth freezing for an hour


def _session_id(s: dict) -> str:
    """Plex's own session key, which lasts exactly as long as the session."""
    k = str(s.get("key") or "").strip()
    if k:
        return k
    # No key is not a reason to give up on continuity: user plus file is stable
    # for the same reasons and only collides if one person plays one file twice.
    return f"{s.get('user') or ''}|{s.get('file') or s.get('title') or ''}"


def viewer_locks() -> dict:
    """What is currently pinned, for anything that wants to explain itself."""
    return {k: dict(v) for k, v in _VIEWER_LOCK.items()}


def infer_viewers(disks: dict) -> dict:
    r"""Which spindle is each viewer reading from, and how many of them.

    WHY IT CANNOT SIMPLY BE READ. DrivePool presents a virtual volume and
    reports its own serial through the share: the same file opened via P:\, via
    the share and via its PoolPart copy gives 092120B4, 092120B4 and 68F9900E.
    Two of those are the pool. Nothing the filesystem returns over a share
    names the disk - it is hidden by design, not by permission.

    SO ASK FIRST, GUESS SECOND. Where nuarr can resolve a session's own file to
    a device it does, and that is a fact. What is left over is matched by rate:
    a stream at 15,784 kbps is about 2.0 MB/s off a disk, and a spindle reading
    about that much while that stream plays is very probably the one. That half
    is labelled inference, on the same footing as the panel's "looks like data
    moving" pairing.

    A DISK CAN HAVE MORE THAN ONE VIEWER, AND THIS DENIED IT TWICE OVER. Erik
    had two people watching and saw one. Both were real, both were on
    NU-DRIVE-1, and the old code could not represent that: it claimed one disk
    per session, so the second viewer found NU-DRIVE-1 already taken and was
    dropped - and even had it matched, the row was a single dict per disk, so
    it would have overwritten the first. Two people on one spindle is not an
    exotic case in a twelve-disk pool; it is roughly a one-in-twelve coincidence
    per pair, and it happens constantly on a box where a season lives together.

    Each disk now carries a count, a paused count, the summed rate and who is
    on it - the same shape the local path already published as
    plex_disk_detail, so the panel sees one shape wherever the pool is.

    A PAUSED STREAM IS NEVER RATE-MATCHED. It holds its place on the disk and
    reads nothing, so it has no rate to match and would only steal a disk from
    a stream that does. It is placed when its file names a device, and
    otherwise counted but not located.
    """
    out: dict = {}

    def _rate(s: dict) -> float:
        r"""A session's read rate in kbps, from the fields that carry it.

        THIS IS WHERE THE FUNCTION WAS BROKEN, from the day it was written.
        It filtered on s["kbps"], and no session has ever had a "kbps" key -
        that name belongs to the AGGREGATED per-disk figure, not to a session.
        Every session therefore failed the filter, the list was always empty,
        and infer_viewers() returned {} on every machine, always. It looked
        like a sandbox problem and was a typo with a plausible name.

        The real fields are the ones _disk_rates() has always used: Plex's
        reserved bandwidth, falling back to the file's own bitrate, which is
        what a direct play actually reads off the disk.
        """
        try:
            v = float(s.get("bandwidth") or 0)
            if v > 0:
                return v
            return float((s.get("detail") or {}).get("src_bitrate") or 0)
        except Exception:                                    # noqa: BLE001
            return 0.0

    def _paused(s: dict) -> bool:
        return str(s.get("state") or "").strip().lower() == "paused"

    def _place(label: str, s: dict, exact: bool, why: str,
               err: float | None = None) -> dict:
        """Add one viewer to a disk, creating the row if it is the first."""
        d = out.get(label)
        if d is None:
            d = out[label] = {
                "viewers": 0, "paused": 0, "kbps": 0.0,
                "bps": float((disks.get(label) or {}).get("read_bps") or 0),
                "who": [], "user": "", "title": "",
                "inferred": not exact, "exact": exact, "why": why}
        k = float(s.get("kbps") or 0)
        d["viewers"] += 1
        d["paused"] += 1 if _paused(s) else 0
        d["kbps"] += k
        d["who"].append({"user": str(s.get("user") or ""),
                         "title": str(s.get("title") or ""),
                         "state": "paused" if _paused(s) else "playing",
                         "kbps": k})
        if not exact:
            d["inferred"], d["exact"] = True, False
        if err is not None:
            d["err"] = round(err, 3)
        # THE LOUDEST STREAM NAMES THE ROW. A disk with one viewer then reads
        # exactly as it always did, so nothing downstream had to change to keep
        # working; a disk with two says so through the count.
        top = max(d["who"], key=lambda w: w["kbps"])
        d["user"], d["title"] = top["user"], top["title"]
        return d

    try:
        from . import gate
        sessions = list(gate.plex_live() or [])
    except Exception:                                        # noqa: BLE001
        return out
    if not sessions or not disks:
        return out
    live = [dict(s, kbps=_rate(s)) for s in sessions]
    cands = {k: float(v.get("read_bps") or 0) for k, v in disks.items()}
    # THE LOCK LASTS AS LONG AS THE SESSION AND NOT ONE PASS LONGER. Plex stops
    # reporting a session the moment it ends, so the set of live keys IS the
    # lifetime - no timeout to tune, and no chance of a finished stream holding
    # a disk on the panel.
    ids = {_session_id(s) for s in sessions}
    for gone in [k for k in _VIEWER_LOCK if k not in ids]:
        _VIEWER_LOCK.pop(gone, None)

    def _remember(sess: dict, label: str, how: str, err=None) -> None:
        _VIEWER_LOCK[_session_id(sess)] = {
            "disk": label, "file": (sess.get("file") or "").strip(),
            "how": how, "err": err, "at": time.time()}

    def _locked(sess: dict):
        """The disk this session was placed on, if that answer still applies."""
        ent = _VIEWER_LOCK.get(_session_id(sess))
        if not ent:
            return None
        # THE SESSION SURVIVES THE FILE CHANGING, and the file is what decides
        # the disk. Playing the next episode keeps the same session key and can
        # land on any of twelve spindles, so a lock that ignored the path would
        # confidently pin the wrong one for the rest of the evening.
        if (sess.get("file") or "").strip() != ent.get("file", ""):
            return None
        return ent if ent.get("disk") in disks else None
    # How much of each disk's read rate is already spoken for. This replaces
    # the old all-or-nothing "taken" set: a disk reading 5.6 MB/s while feeding
    # a 2.2 MB/s stream still has 3.4 MB/s that something else is doing, and
    # that remainder is exactly what the next stream should be matched against.
    spent: dict = {}

    # ASK BEFORE GUESSING. The docstring above explains why the disk cannot be
    # read back through a share, and that is still true - but it is an argument
    # about the FILESYSTEM, and the filesystem is not the only thing that knows.
    # Where nuarr can resolve the session's own path to a device, that is the
    # answer, and correlating bitrates to arrive at it anyway would be choosing
    # a guess over a fact.
    # WHAT THE HOST KNOWS, BEFORE ANYTHING IS GUESSED. On a machine with the
    # pool attached this is empty and costs nothing; over a share it is the
    # difference between a fact and a correlation - see peer_sessions().
    peer: dict = {}
    try:
        for srv in servers():
            peer.update(peer_sessions(srv))
    except Exception:                                        # noqa: BLE001
        peer = {}

    for s in list(live):
        # THE GATE HAS ALREADY DONE THIS, and doing it again would be slower
        # and no more correct: gate.plex_live() resolves each session's file to
        # a device through the same storage layer and caches it per path, so
        # the answer is sitting on the session already. Falling back to
        # resolving it here covers a session the gate could not place.
        label, how = (s.get("disk") or "").strip(), "member"
        if not label:
            told = peer.get(str(s.get("key") or "").strip())
            if told:
                label, how = told, "volume"
        if not label:
            f = (s.get("file") or "").strip()
            if not f:
                continue
            try:
                from . import storage
                label, how = storage.device_of(f)
            except Exception:                                # noqa: BLE001
                label, how = None, ""
        if label and label in disks and how in ("member", "volume"):
            _place(label, s, True,
                   "the file's own location says so"
                   if (s.get("disk") or "").strip()
                   else "the host resolved it from the file itself")
            # A FACT OUTRANKS A LOCK AND REPLACES IT. If the storage layer can
            # name the device, that answer is better than whatever was inferred
            # earlier, so it is written back rather than merely used.
            _remember(s, label, "exact")
            if not _paused(s):
                spent[label] = spent.get(label, 0.0) + \
                    float(s.get("kbps") or 0) * 1000.0 / 8.0
            live.remove(s)

    # ---- then anything already placed, before guessing again ----------------
    for s in list(live):
        ent = _locked(s)
        if not ent:
            continue
        label = ent["disk"]
        d = _place(label, s, False,
                   "held from when this session was first placed"
                   + (f" ({ent['how']})" if ent.get("how") else ""))
        d["held"] = True
        # A PAUSED STREAM SPENDS NOTHING. It holds its place on the disk and
        # reads nothing at all, so counting its bitrate against the disk's rate
        # would starve a stream that really is reading.
        if not _paused(s):
            spent[label] = spent.get(label, 0.0) + \
                float(s.get("kbps") or 0) * 1000.0 / 8.0
        live.remove(s)

    # ONE STREAM AND ONE BUSY DISK NEEDS NO ARITHMETIC. Measured on the
    # sandbox: a single session playing, NU-DRIVE-0 reading 9.98 MB/s and the
    # other eleven at zero. There is nothing to correlate - the only thing
    # reading is feeding the only thing watching - yet the rate-matching below
    # would refuse it outright if the session reported no bitrate, which is
    # exactly the case that produced no viewers at all.
    if len(live) == 1 and float(live[0].get("kbps") or 0) <= 0 \
            and not _paused(live[0]):
        # ONLY WHEN THERE IS NOTHING BETTER TO GO ON. The first version of this
        # skipped the rate check entirely, and comparing the two machines
        # showed what that costs: the host resolved a stream to NU-DRIVE-4 from
        # the file itself, while the sandbox confidently reported NU-DRIVE-1 -
        # which was reading 16.7 MB/s because nuarr's own OCR was working on
        # it. The stream was 1,971 kbps, about 0.25 MB/s. A sixty-eight-fold
        # mismatch, claimed as an answer.
        #
        # So the shortcut is now the last resort it should always have been: it
        # fires only when the session reports no bitrate at all, because with a
        # bitrate the band below is strictly better information. A confident
        # wrong answer is worse than none, and this produced one.
        busy = [(k, v) for k, v in cands.items()
                if v - spent.get(k, 0.0) > 400_000]
        if len(busy) == 1:
            label, rd = busy[0]
            d = _place(label, live[0], False,
                       "the only disk reading, and the only stream")
            d["only"] = True
            _remember(live[0], label, "only one reading")
            return out

    # Biggest stream first: it has the strongest signal and the most to lose
    # from being assigned a disk that a smaller one explains better.
    for s in sorted(live, key=lambda x: -float(x.get("kbps") or 0)):
        if _paused(s):
            continue                       # holds its place, reads nothing
        if float(s.get("kbps") or 0) <= 0:
            continue                       # nothing to match a rate against
        want = float(s["kbps"]) * 1000.0 / 8.0          # kbps -> bytes/sec
        best, best_err = None, None
        for label, rd in cands.items():
            left = rd - spent.get(label, 0.0)
            if left <= 0:
                continue
            # Plex reads ahead in bursts, so the band is generous upward and
            # tight downward - a disk with less rate left than the stream needs
            # cannot be the one feeding it.
            if left < want * 0.6 or left > want * 8.0:
                continue
            err = abs(left - want) / want
            if best_err is None or err < best_err:
                best, best_err = label, err
        if best:
            spent[best] = spent.get(best, 0.0) + want
            _place(best, s, False, "its rate matches what this disk is reading",
                   best_err)
            # ONLY A CLOSE MATCH IS WORTH KEEPING. Anything looser is a
            # best-of-a-bad-lot answer that happened to be the least wrong in
            # one sample; freezing that for the length of a film would turn a
            # shrug into a claim.
            if best_err is not None and best_err <= _LOCK_MAX_ERR:
                _remember(s, best, "rate", round(best_err, 3))

    # A VIEWER NUARR CANNOT PLACE IS STILL A VIEWER. Dropping it silently is
    # how two people watching came to be reported as one; the panel would
    # rather say "somewhere on the pool" than quietly lose someone.
    lost = [s for s in live
            if not any(w.get("user") == str(s.get("user") or "")
                       and w.get("title") == str(s.get("title") or "")
                       for d in out.values() for w in d["who"])]
    if lost:
        out["_unplaced"] = {
            "viewers": len(lost),
            "paused": sum(1 for s in lost if _paused(s)),
            "kbps": sum(float(s.get("kbps") or 0) for s in lost),
            "bps": 0.0, "inferred": True, "exact": False,
            "user": str(lost[0].get("user") or ""),
            "title": str(lost[0].get("title") or ""),
            "why": "playing, but nothing here says which disk",
            "who": [{"user": str(s.get("user") or ""),
                     "title": str(s.get("title") or ""),
                     "state": "paused" if _paused(s) else "playing",
                     "kbps": float(s.get("kbps") or 0)} for s in lost]}
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
            # Blank unless the host is also running nuarr - see peer_counts().
            "file_counts": peer_counts(s),
        })
    return out
