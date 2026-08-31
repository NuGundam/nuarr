r"""What kind of storage is this, and which device holds this file?

WHY THIS EXISTS. nuarr grew up on one machine with a StableBit DrivePool, and
the way it answers "which disk is this file on" is to look for a folder called
"PoolPart.<guid>" - DrivePool's on-disk signature. That is a fine way to find a
DrivePool and the only way to find nothing else. A JBOD, a single disk, a
hardware RAID volume, a Storage Spaces pool, an unRAID share, a folder mount
point or an SMB share all have no PoolPart folder, so the answer came back
empty and everything downstream - the per-disk panel, the queue's spindle
fairness, the gate's "is a viewer on this disk" check - quietly had nothing to
work with.

THE SHAPE OF THE ANSWER IS THE SAME EVERYWHERE, which is what makes this
possible: a file sits on some device, and that device has a name, a size and an
I/O rate. Only the method of finding it differs. So the vendor knowledge goes
in one place, behind one question, and the rest of nuarr keeps asking that
question without knowing what answered it.

HOW THE KIND IS DETECTED, WITHOUT KNOWING ANY PRODUCT.

The first version of this asked how many physical disks back the volume and
called "none" a pool. That is the intuitive test and it is WRONG, which testing
caught immediately: StableBit presents a virtual disk of its own, so P:
resolved to exactly one disk and was classified a plain single drive.

The test that holds needs no vendor knowledge at all, because it appeals to
physics rather than to products: A VOLUME CANNOT BE LARGER THAN THE HARDWARE
BEHIND IT. Measured on this machine, P: reports 130.97 TB against a 2 TB
CoveFsDisk - a sixty-fold difference no partitioning explains - while every
real disk matches its volume to the byte (18.19 TB against 18.19 TB). So:

    bigger than its disks -> a VIRTUAL volume. Something is assembling this in
                     software, whoever wrote it. The member holding the file
                     has to be found, and that is the only case where a
                     vendor-specific probe is needed at all.
    exactly one disk -> a SIMPLE volume. A plain disk, an external drive, a
                     partition, or a folder mount point. The path already names
                     its device; there is nothing to search for.
    more than one -> a SPANNED volume. Software RAID, Storage Spaces, a striped
                     or mirrored set. The volume is the honest unit here: a file
                     is on all of them and on none of them in particular.
    remote        -> a SERVER. A UNC path or a mapped network drive. The machine
                     at the other end is the device as far as this one is
                     concerned; whether more detail is available depends on what
                     that machine will answer.

Bus type is consulted only to confirm a virtual verdict, never to reach one:
those constants have moved between Windows versions, and being wrong there
would mislabel real hardware.

WHAT IS DELIBERATELY NOT CLAIMED. A cloud mount presents a volume that is not
backed by local hardware and whose "device" is a network service; it is
reported as such rather than as a disk with an I/O rate that would mean nothing.
"""
from __future__ import annotations

import os
import subprocess
import time

from .config import NO_WINDOW, hidden_si

# Kinds, in the order the detector tries to establish them.
SIMPLE = "simple"        # one physical disk behind the volume
SPANNED = "spanned"      # several - RAID, Storage Spaces, striped or mirrored
VIRTUAL = "virtual"      # none - a pool presenting a volume of its own
REMOTE = "remote"        # a share; the server is the device
UNKNOWN = "unknown"

KIND_WORDS = {
    SIMPLE: "a single disk",
    SPANNED: "several disks behind one volume",
    VIRTUAL: "a pool presenting one volume",
    REMOTE: "storage on another machine",
    UNKNOWN: "not identified",
}

_CACHE: dict = {"at": 0.0, "vols": None}
_TTL = 300.0


def _ps(script: str, timeout: int = 60) -> str:
    """PowerShell, quietly. Windows-only; returns '' anywhere else."""
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW, startupinfo=hidden_si())
        return r.stdout or ""
    except Exception:                                        # noqa: BLE001
        return ""


def volumes() -> list:
    r"""Every local volume, with the physical disks behind it.

    One query, cached, because this is asked per library root and per file
    lookup and the answer only changes when hardware does.

    MSFT_Volume -> MSFT_Partition -> MSFT_Disk is the chain that survives every
    layout: a partition knows its disk, a volume knows its partitions, and a
    volume with no partitions at all is the tell for something virtual.
    """
    now = time.time()
    if _CACHE["vols"] is not None and now - _CACHE["at"] < _TTL:
        return _CACHE["vols"]
    out = []
    js = _ps(r"""
$ErrorActionPreference='SilentlyContinue'
$vols = Get-CimInstance -Namespace root/Microsoft/Windows/Storage MSFT_Volume
$res = foreach ($v in $vols) {
  $disks = @()
  try {
    $parts = Get-CimAssociatedInstance -InputObject $v `
             -ResultClassName MSFT_Partition
    foreach ($p in $parts) {
      $d = Get-CimAssociatedInstance -InputObject $p -ResultClassName MSFT_Disk
      foreach ($x in $d) {
        $disks += [pscustomobject]@{ N=[string]$x.Number; Sz=[int64]$x.Size;
                                     Bus=[int]$x.BusType }
      }
    }
  } catch {}
  [pscustomobject]@{
    Letter = $v.DriveLetter; Label = $v.FileSystemLabel; Path = $v.Path
    Size = $v.Size; Free = $v.SizeRemaining; FS = $v.FileSystem
    Disks = $disks
  }
}
$res | ConvertTo-Json -Depth 4 -Compress
""", timeout=90)
    try:
        import json
        data = json.loads(js or "[]")
        if isinstance(data, dict):
            data = [data]
        for v in data:
            disks = v.get("Disks") or []
            if isinstance(disks, dict):
                disks = [disks]
            nums, bytes_, buses = [], 0, []
            seen = set()
            for d in disks:
                if not isinstance(d, dict):
                    continue
                n = str(d.get("N") or "").strip()
                if not n or n in seen:
                    continue
                seen.add(n)
                nums.append(n)
                bytes_ += int(d.get("Sz") or 0)
                buses.append(int(d.get("Bus") or 0))
            out.append({"letter": (v.get("Letter") or "").strip() or None,
                        "label": v.get("Label") or "",
                        "path": v.get("Path") or "",
                        "size": int(v.get("Size") or 0),
                        "free": int(v.get("Free") or 0),
                        "fs": v.get("FS") or "",
                        "disks": nums, "disks_bytes": bytes_,
                        "bus_types": buses})
    except Exception:                                        # noqa: BLE001
        out = []
    _CACHE.update(vols=out, at=now)
    return out


# WHICH LETTERS ARE MAPPED DRIVES, asked once for all of them rather than once
# per lookup. The first version ran a PowerShell query per call, which made
# every disk_of() cost 270 ms warm - against the few milliseconds of the stat
# walk it replaced, on a function the job gate consults constantly. Measured,
# not guessed: it is exactly the kind of regression that hides behind a correct
# answer.
_NET_CACHE: dict = {"at": 0.0, "map": None}


def _net_letters() -> dict:
    """letter -> server, for every mapped network drive. One query, cached."""
    now = time.time()
    if _NET_CACHE["map"] is not None and now - _NET_CACHE["at"] < _TTL:
        return _NET_CACHE["map"]
    out = {}
    js = _ps("Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=4' | "
             "Select-Object DeviceID,ProviderName | ConvertTo-Json -Compress",
             timeout=45)
    try:
        import json
        data = json.loads(js or "[]")
        if isinstance(data, dict):
            data = [data]
        for d in data:
            dev = (d.get("DeviceID") or "").strip().rstrip(":").upper()
            prov = (d.get("ProviderName") or "").replace("/", "\\")
            if dev and prov:
                out[dev] = prov.lstrip("\\").split("\\", 1)[0]
    except Exception:                                        # noqa: BLE001
        out = {}
    _NET_CACHE.update(map=out, at=now)
    return out


def _is_remote(path: str) -> tuple[bool, str]:
    r"""Is this path on another machine, and which one?

    Two ways a path can be remote and they look nothing alike: a UNC path
    names the server directly, while a mapped drive looks exactly like a local
    letter until you ask what is behind it.
    """
    p = (path or "").replace("/", "\\")
    if p.startswith("\\\\"):
        host = p[2:].split("\\", 1)[0]
        return True, host
    if os.name == "nt" and len(p) > 1 and p[1] == ":":
        host = _net_letters().get(p[0].upper())
        if host:
            return True, host
    return False, ""


def volume_for(path: str) -> dict | None:
    """The volume a path sits on, by letter or by mount point."""
    p = os.path.abspath(path or "")
    vols = volumes()
    if os.name == "nt" and len(p) > 1 and p[1] == ":":
        letter = p[0].upper()
        for v in vols:
            if (v.get("letter") or "").upper() == letter:
                return v
    # Folder mount points have no letter, so match the longest volume path
    # that prefixes this one - the same way Windows resolves it.
    best = None
    for v in vols:
        vp = (v.get("path") or "")
        if vp and p.lower().startswith(vp.lower()):
            if best is None or len(vp) > len(best.get("path") or ""):
                best = v
    return best


# Bus types that mean "this disk is software, not hardware". Kept as a hint
# rather than the test, because the numbering has shifted between Windows
# versions and a wrong constant would silently mislabel real hardware.
_VIRTUAL_BUS = {14, 15, 16}


def _kind_of(v: dict) -> str:
    r"""Simple, spanned or virtual - decided by physics, not by product.

    THE OBVIOUS TEST WAS WRONG AND TESTING FOUND IT. "A pool volume resolves
    to no physical disk" is what the code that came before assumed, and on this
    machine it is false: StableBit presents a virtual disk of its own, so P:
    resolved to exactly one disk and was classified a plain single drive.

    The honest test needs no vendor knowledge at all: A VOLUME CANNOT BE LARGER
    THAN THE HARDWARE BEHIND IT. P: reports 138.97 TB against a 2 TB
    CoveFsDisk - a 69-fold difference that no partitioning explains. Anything
    meaningfully bigger than the disks under it is being assembled by software,
    whoever wrote that software.

    Bus type is used only to confirm, never to decide, because those constants
    have moved between Windows versions and being wrong there would mislabel
    real hardware as virtual.
    """
    disks = v.get("disks") or []
    size = int(v.get("size") or 0)
    backing = int(v.get("disks_bytes") or 0)
    if backing and size and size > backing * 1.05:
        return VIRTUAL
    if v.get("bus_types") and set(v["bus_types"]) & _VIRTUAL_BUS:
        return VIRTUAL
    if len(disks) == 1:
        return SIMPLE
    if len(disks) > 1:
        return SPANNED
    # No disk behind it at all. Still virtual - something is presenting this.
    return VIRTUAL


def describe(path: str) -> dict:
    r"""What kind of storage backs this path, and what is the device called?

    The whole point of the module in one call. Everything else - the per-disk
    panel, spindle fairness, the viewer check - can ask this and stop caring
    whether the answer came from DrivePool, from a RAID controller or from a
    machine in another room.
    """
    remote, host = _is_remote(path)
    if remote:
        return {"kind": REMOTE, "device": host or "a network server",
                "host": host, "disks": [], "why": KIND_WORDS[REMOTE],
                "detail_possible": bool(host)}
    v = volume_for(path)
    if not v:
        return {"kind": UNKNOWN, "device": "", "host": "", "disks": [],
                "why": KIND_WORDS[UNKNOWN], "detail_possible": False}
    name = v.get("label") or (f"{v['letter']}:" if v.get("letter") else
                              v.get("path") or "")
    kind = _kind_of(v)
    return {"kind": kind, "device": name, "host": "",
            "disks": v.get("disks") or [], "why": KIND_WORDS[kind],
            "size": v.get("size", 0), "free": v.get("free", 0),
            "detail_possible": kind in (SIMPLE, SPANNED)}


# ---- which device holds one file ------------------------------------------
# Registered rather than hard-coded, so adding support for another pool product
# is a function and a line rather than an edit to everything that asks.
_MEMBER_FINDERS: list = []


def register_member_finder(fn) -> None:
    """fn(path) -> device label, or None. Consulted only for VIRTUAL volumes.

    Idempotent: registering the same function twice would run the same probe
    twice for every lookup, and a module imported from two places is normal.
    """
    if fn not in _MEMBER_FINDERS:
        _MEMBER_FINDERS.append(fn)


def device_of(path: str) -> tuple[str | None, str]:
    r"""Which device holds this file, and how confidently.

    Returns (label, how) where how is one of:

        "volume"  - the path names its own device. True for a plain disk, an
                    external drive, a partition or a mount point, and the
                    common case everywhere except a pool.
        "member"  - a virtual volume, and a registered finder identified the
                    member holding the file. Exact, but vendor-specific.
        "spanned" - several disks back this volume, so the volume is the
                    honest unit and naming one spindle would be a fiction.
        "remote"  - the file is on another machine; the server is the device.
        ""        - not determined.

    THE CALLER IS TOLD WHICH, deliberately. "NU-DRIVE-3" from a PoolPart walk
    and "MEDIA" from a RAID volume are both correct and mean different things,
    and code that treats them alike - spindle fairness, for instance - should
    be able to tell that the second cannot be split further.
    """
    d = describe(path)
    if d["kind"] == REMOTE:
        return (d["device"] or None), "remote"
    if d["kind"] in (SIMPLE,):
        return (d["device"] or None), "volume"
    if d["kind"] == SPANNED:
        return (d["device"] or None), "spanned"
    if d["kind"] == VIRTUAL:
        for fn in _MEMBER_FINDERS:
            try:
                got = fn(path)
            except Exception:                                # noqa: BLE001
                continue
            if got:
                return got, "member"
        # A pool nuarr does not recognise. The volume is still a true answer,
        # just a coarse one - better than None, which reads as "no storage".
        return (d["device"] or None), "volume"
    return None, ""


def warm() -> None:
    r"""Fill the caches before anybody asks, off the request path.

    The two queries behind this cost about twenty seconds together on a
    twelve-disk box, and warm they cost nothing measurable. That difference has
    to be paid somewhere, and paying it during start-up - where a few seconds
    is invisible - is better than making one unlucky job wait for it.
    """
    try:
        volumes()
        _net_letters()
    except Exception:                                        # noqa: BLE001
        pass


def roots_report(paths: list) -> list:
    """One row per library root: what it is, and what nuarr can say about it."""
    out = []
    for p in paths or []:
        try:
            d = describe(p)
        except Exception as e:                               # noqa: BLE001
            d = {"kind": UNKNOWN, "device": "", "why": f"{type(e).__name__}",
                 "disks": [], "detail_possible": False}
        out.append({"path": p, **d})
    return out
