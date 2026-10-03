r"""Drive health: what every drive says about itself - SSD wear and HDD SMART.

WHY. Erik, 2026-10-02: StableBit Scanner showed the staging drive F: (a
Crucial P310 500 GB, every download lands on it) at 90% of its rated life,
270 TB written in 6,163 powered hours - about a terabyte a day. Nothing in
nuarr would have said so; it was found by opening Scanner. Then: "what about
the other disks" - the twelve SATA spindles under the pool, which wear out
differently and say so differently.

TWO KINDS OF DRIVE, TWO LOGS.

  NVMe   the SMART / Health log (log page 02h), through
         IOCTL_STORAGE_QUERY_PROPERTY (StorageDeviceProtocolSpecificProperty):
         byte 5 "Percentage Used" - the drive's own estimate of how much of
         its rated endurance is spent - and data written in 512,000-byte
         units. Some firmware says 0% long after real wear (the WD drives here
         at 74-198 TB), so the written total always sits beside it.
         Get-StorageReliabilityCounter's "Wear" was 0 for F:, so it is unused.

  SATA   the ATA SMART attribute table, through SMART_RCV_DRIVE_DATA. A hard
         disk has no endurance figure; what predicts its failure is the
         surface: 5 Reallocated Sectors (spots already swapped for spares),
         197 Current Pending (spots that could not be read and wait to be
         swapped) and 198 Offline Uncorrectable. 199 UDMA CRC counts errors
         on the cable, not the platter.

WHAT WARNS. Pending or uncorrectable sectors warn at once: data on those
spots is unreadable now. Reallocated sectors and CRC errors warn when they
RISE past what nuarr first saw - a disk that remapped one sector years ago
and has been steady since is not news, a disk remapping more this week is.
The counts are shown either way. An SSD warns at WARN_AT% used or on its own
critical flag / low spare / media errors.

IGNORE, THE WAY SCANNER DOES IT - BUT ONLY UNTIL IT GETS WORSE. An ignored
drive stays quiet (the Health row and the phone) until an SSD climbs another
STEP points or any drive reports a problem it did not have when it was
ignored; then it warns again on its own, with nothing to remember to undo.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import re
import struct
import time

from . import joblog
from .db import kv_get, kv_set

WARN_AT = 80            # SSD % used: the row turns amber, and the phone is told
STEP = 5                # then once more every 5 points
CACHE_S = 600.0
OLD_HOURS = 5 * 8766    # a spindle past five powered years gets an age note
KV_SAMPLES = "diskwear.samples"     # {key: [[t, written_bytes], ...]}
KV_TOLD = "diskwear.told"           # {key: {"step", "problems"}} the phone has heard
KV_IGNORED = "diskwear.ignored"     # {key: {"at", "pct", "problems"}}
KV_BASE = "diskwear.base"           # {key: {"realloc", "crc"}} first counts seen

_IOCTL_QUERY = 0x002D1400
_IOCTL_SMART_RCV = 0x0007C088
_IOCTL_VOL_EXTENTS = 0x00560000
_PROTO_PROP, _DEVICE_PROP = 50, 0
_NVME, _LOGPAGE, _HEALTH = 3, 2, 2

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                             wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                 ctypes.c_void_p, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.FindFirstVolumeW.restype = wt.HANDLE
_k32.FindFirstVolumeW.argtypes = [wt.LPWSTR, wt.DWORD]
_k32.FindNextVolumeW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD]
_k32.FindVolumeClose.argtypes = [wt.HANDLE]
_k32.GetVolumeInformationW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
_k32.GetVolumePathNamesForVolumeNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
                                                  ctypes.POINTER(wt.DWORD)]
_BAD = wt.HANDLE(-1).value

_CACHE: dict = {"at": 0.0, "drives": []}


# ------------------------------------------------------------ plumbing ----
def _kv(key: str) -> dict:
    try:
        return json.loads(kv_get(key) or "{}")
    except Exception:                                            # noqa: BLE001
        return {}


def _open(path: str, access: int = 0):
    h = _k32.CreateFileW(path, access, 3, None, 3, 0, None)
    return None if h in (_BAD, None) else h


def _ioctl(h, code: int, inbuf: bytes, outlen: int) -> bytes | None:
    buf = ctypes.create_string_buffer(inbuf + b"\0" * max(0, outlen - len(inbuf)))
    got = wt.DWORD(0)
    ok = _k32.DeviceIoControl(h, code, buf, len(buf), buf, len(buf), ctypes.byref(got), None)
    return buf.raw[:got.value] if ok else None


def _model(h) -> str:
    raw = _ioctl(h, _IOCTL_QUERY, struct.pack("<II", _DEVICE_PROP, 0), 1024)
    if not raw or len(raw) < 20:
        return ""
    prod = struct.unpack_from("<I", raw, 16)[0]
    if not prod or prod >= len(raw):
        return ""
    return raw[prod:].split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _nvme(h) -> dict | None:
    spsd = struct.pack("<IIIIIIIIII", _NVME, _LOGPAGE, _HEALTH, 0, 40, 512, 0, 0, 0, 0)
    raw = _ioctl(h, _IOCTL_QUERY, struct.pack("<II", _PROTO_PROP, 0) + spsd, 8 + 40 + 512)
    if not raw or len(raw) < 8 + 40:
        return None
    off = struct.unpack_from("<I", raw, 8 + 16)[0]
    log = raw[8 + off: 8 + off + 512]
    if len(log) < 192:
        return None
    le = lambda b: int.from_bytes(b, "little")
    return {"critical": log[0], "temp_c": le(log[1:3]) - 273, "spare": log[3],
            "spare_thr": log[4], "used_pct": log[5],
            "written": le(log[48:64]) * 512000, "read": le(log[32:48]) * 512000,
            "power_hours": le(log[128:144]), "unsafe": le(log[144:160]),
            "media_errors": le(log[160:176])}


def _smart(h) -> dict | None:
    """ATA SMART READ DATA: {attribute id: raw value}."""
    regs = bytes([0xD0, 1, 1, 0x4F, 0xC2, 0xA0, 0xB0, 0])
    inp = struct.pack("<I", 512) + regs + b"\0" * 4 + b"\0" * 16 + b"\0"
    raw = _ioctl(h, _IOCTL_SMART_RCV, inp, 16 + 512)
    if not raw or len(raw) < 16 + 362:
        return None
    data = raw[16:16 + 512]
    out = {}
    cur = {}
    for i in range(30):
        a = data[2 + i * 12: 2 + (i + 1) * 12]
        if a[0]:
            out[a[0]] = int.from_bytes(a[5:11], "little")
            cur[a[0]] = a[3]          # the normalised value, 100 = as new
    if out:
        out["cur"] = cur
    return out or None


def _volumes() -> dict:
    """{physical disk number: [{"letter", "label"}]} - every mounted volume,
    lettered or not (the pool's members carry labels, not letters)."""
    out: dict = {}
    name = ctypes.create_unicode_buffer(260)
    fh = _k32.FindFirstVolumeW(name, 260)
    if fh in (_BAD, None):
        return out
    try:
        while True:
            vol = name.value
            label = ctypes.create_unicode_buffer(261)
            _k32.GetVolumeInformationW(vol, label, 261, None, None, None, None, 0)
            paths = ctypes.create_unicode_buffer(1024)
            n = wt.DWORD(0)
            letter = ""
            if _k32.GetVolumePathNamesForVolumeNameW(vol, paths, 1024, ctypes.byref(n)):
                p = paths.value
                if len(p) == 3 and p[1] == ":":
                    letter = p[0]
            h = _open(vol.rstrip("\\"))
            if h:
                try:
                    raw = _ioctl(h, _IOCTL_VOL_EXTENTS, b"", 8 + 24 * 4)
                    if raw and len(raw) >= 8 + 24:
                        for i in range(struct.unpack_from("<I", raw, 0)[0]):
                            disk = struct.unpack_from("<I", raw, 8 + 24 * i)[0]
                            out.setdefault(disk, []).append({"letter": letter, "label": label.value})
                finally:
                    _k32.CloseHandle(h)
            if not _k32.FindNextVolumeW(fh, name, 260):
                break
    finally:
        _k32.FindVolumeClose(fh)
    return out


def _rate(key: str, written: int, power_hours: int) -> tuple[float, str]:
    """Bytes written per day: from nuarr's own samples once they span a day
    or more (recent use), otherwise the drive's lifetime average."""
    now = time.time()
    allk = _kv(KV_SAMPLES)
    s = [x for x in allk.get(key, []) if now - x[0] < 30 * 86400]
    if not s or now - s[-1][0] > 6 * 3600:
        s.append([now, written])
        allk[key] = s[-200:]
        try:
            kv_set(KV_SAMPLES, json.dumps(allk))
        except Exception:                                        # noqa: BLE001
            pass
    first = s[0]
    if now - first[0] >= 86400 and written >= first[1]:
        return (written - first[1]) / ((now - first[0]) / 86400), \
            (lambda n: "the last day" if n <= 1 else f"the last {n} days")(round((now - first[0]) / 86400))
    if power_hours:
        return written / (power_hours / 24), "lifetime average"
    return 0.0, ""


# ------------------------------------------------------------- reading ----
def _nat(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


def read(fresh: bool = False) -> list:
    """Every drive that answers - NVMe health log or ATA SMART - sorted by
    type (SSDs, then hard disks), then by name."""
    if not fresh and time.time() - _CACHE["at"] < CACHE_S:
        return _CACHE["drives"]
    vols = _volumes()
    ign_all = _kv(KV_IGNORED)
    base = _kv(KV_BASE)
    base_dirty = False
    drives = []
    for n in range(0, 40):
        h = _open(rf"\\.\PhysicalDrive{n}", 0xC0000000)
        if not h:
            continue
        try:
            hl = _nvme(h)
            sm = None if hl else _smart(h)
            # A REAL SPINDLE COUNTS ITS HOURS. DrivePool's virtual disk (P:,
            # CoveFsDisk) answers the SMART request with an empty table and was
            # listed as a seventeenth hard disk; no attribute 9, not a disk.
            if sm is not None and 9 not in sm:
                sm = None
            if not hl and not sm:
                continue                   # virtual disk (the pool), or no log
            model = _model(h)
        finally:
            _k32.CloseHandle(h)
        # AND BY NAME, BECAUSE UNDER THE SERVICE IT DOES ANSWER: run as nuarr,
        # the pool's virtual disk passed the SMART request through to one of
        # its members and showed up as "P: CoveFsDisk" with NU-DRIVE-11's
        # figures. Virtual disks are not drives.
        if re.match(r"(?i)covefs|virtual|msft|vhd", model or ""):
            continue
        key = f"{model}#{n}"
        vs = vols.get(n, [])
        letters = [v["letter"] for v in vs if v["letter"]]
        labels = [v["label"] for v in vs if v["label"]]
        name = (" ".join(f"{L}:" for L in letters) or (labels[0] if labels else f"disk {n}"))
        d = {"disk": n, "key": key, "model": model, "letters": letters,
             "labels": labels, "name": name}
        bad, notes = [], []
        if hl:
            per_day, basis = _rate(key, hl["written"], hl["power_hours"])
            used = hl["used_pct"]
            days_left = None
            if used and used < 100 and hl["written"] and per_day > 0:
                days_left = round((100 - used) * (hl["written"] / used) / per_day)
            if hl["critical"]:
                bad.append(f"critical warning flag {hl['critical']:#04x}")
            if hl["spare"] <= hl["spare_thr"]:
                bad.append(f"spare {hl['spare']}% at/below its {hl['spare_thr']}% threshold")
            if hl["media_errors"]:
                bad.append(f"{hl['media_errors']} media errors")
            raw_warn = bool(bad) or used >= WARN_AT
            d.update(type="ssd", bus="NVMe", **hl, per_day=per_day, rate_basis=basis,
                     days_left=days_left,
                     # what the drive's own percentage implies its whole
                     # endurance is: written / % used
                     implied_tbw=(hl["written"] / used * 100) if used else None)
        else:
            realloc, pending = sm.get(5, 0), sm.get(197, 0)
            uncorr, crc = sm.get(198, 0), sm.get(199, 0)
            hours = sm.get(9, 0)
            b = base.get(key)
            if b is None:
                base[key] = b = {"realloc": realloc, "crc": crc, "at": time.time()}
                base_dirty = True
            if pending:
                bad.append(f"{pending} pending sector(s) - unreadable now, waiting to be remapped")
            if uncorr:
                bad.append(f"{uncorr} uncorrectable sector(s)")
            if realloc > b.get("realloc", 0):
                bad.append(f"reallocated sectors rising: {b.get('realloc', 0)} -> {realloc}")
            elif realloc:
                notes.append(f"{realloc} reallocated sector(s), steady since nuarr started watching")
            if crc > b.get("crc", 0):
                bad.append(f"cable (CRC) errors rising: {b.get('crc', 0)} -> {crc} - reseat or replace the SATA cable")
            elif crc:
                notes.append(f"{crc} old cable (CRC) errors, not rising")
            age = f"{hours / 8766:.1f} years powered on" if hours >= OLD_HOURS else ""
            # HELIUM. Every spindle in this pool is helium-filled (attribute
            # 22, normalised: 100 is full). A sealed drive does not lose
            # helium in normal life, so any drop is a leak - and a drive
            # without its helium does not last.
            helium = (sm.get("cur") or {}).get(22)
            if helium is not None and helium < 100:
                bad.append(f"helium level {helium}% - the drive is leaking")
            raw_warn = bool(bad)
            d.update(type="hdd", realloc=realloc, pending=pending, uncorr=uncorr, crc=crc,
                     power_hours=hours, temp_c=(sm.get(194, 0) & 0xFF) or None,
                     starts=sm.get(4, 0), spin_retry=sm.get(10, 0), used_pct=0, written=0, age=age,
                     helium=helium, load_cycles=sm.get(193, 0),
                     bus="SATA", base_realloc=b.get("realloc", 0), base_crc=b.get("crc", 0),
                     base_at=b.get("at"))
        ig = ign_all.get(key)
        ignored = bool(ig and raw_warn and d.get("used_pct", 0) < int(ig.get("pct", 0)) + STEP
                       and set(bad) <= set(ig.get("problems") or []))
        d.update(problems=bad, notes=notes, raw_warn=raw_warn, ignored=ignored,
                 ignored_at=(ig or {}).get("at") if ignored else None,
                 rearm_at=(int(ig.get("pct", 0)) + STEP) if ignored and d["type"] == "ssd" else None,
                 warn=raw_warn and not ignored)
        drives.append(d)
    if base_dirty:
        try:
            kv_set(KV_BASE, json.dumps(base))
        except Exception:                                        # noqa: BLE001
            pass
    drives.sort(key=lambda x: (0 if x["type"] == "ssd" else 1, _nat(x["name"])))
    _CACHE.update(at=time.time(), drives=drives)
    return drives


def set_ignored(key: str, on: bool) -> bool:
    ig = _kv(KV_IGNORED)
    d = next((x for x in read() if x["key"] == key), None)
    if on:
        if not d:
            return False
        ig[key] = {"at": time.time(), "pct": d.get("used_pct", 0), "problems": d["problems"]}
    else:
        ig.pop(key, None)
    kv_set(KV_IGNORED, json.dumps(ig))
    _CACHE["at"] = 0.0
    return True


def snapshot() -> dict:
    """For the Disk page: every drive's full reading and its ignore state."""
    return {"drives": read(), "warn_at": WARN_AT, "step": STEP, "read_at": _CACHE["at"]}


def _name(d: dict) -> str:
    return f"{d['name']} {d['model']}".strip()


def _tb(b: float) -> str:
    return f"{b / 1e12:,.0f} TB" if b >= 1e13 else f"{b / 1e12:,.1f} TB"


def summary(drives: list | None = None) -> tuple[int, str]:
    """(warnings, one line) for the Health page: the drives that need a look,
    in full; the rest as a count."""
    drives = read() if drives is None else drives
    if not drives:
        return 0, "no drive answered"
    parts = []
    for d in drives:
        if not (d["warn"] or d["ignored"]):
            continue
        if d["type"] == "ssd":
            s = f"{_name(d)} {d['used_pct']}% used · {_tb(d['written'])} written"
            if d["warn"] and d.get("days_left") is not None:
                s += f" · ~{_tb(d['per_day'])}/day · about {d['days_left']} days to 100%"
        else:
            s = f"{_name(d)}"
        if d["warn"] and d["problems"]:
            s += " · " + "; ".join(d["problems"])
        if d["ignored"]:
            s += (f" · warning ignored until {d['rearm_at']}%" if d.get("rearm_at")
                  else " · warning ignored until something new")
        parts.append(s)
    ok_ssd = sum(1 for d in drives if d["type"] == "ssd" and not d["raw_warn"])
    ok_hdd = sum(1 for d in drives if d["type"] == "hdd" and not d["raw_warn"])
    watch = sum(1 for d in drives if d["notes"] and not d["raw_warn"])
    parts.append(f"{ok_ssd} SSD and {ok_hdd} hard disk(s) healthy"
                 + (f" ({watch} with something to keep an eye on - see the Disk page)" if watch else ""))
    return sum(1 for d in drives if d["warn"]), " | ".join(parts)


async def _tell_phone(drives: list) -> None:
    """Once per STEP points for an SSD, once per new set of problems for any
    drive - not every six hours for as long as a problem lasts."""
    try:
        from . import pushover
        if not pushover.enabled():
            return
        told = _kv(KV_TOLD)
        for d in drives:
            if not d["warn"]:
                continue
            step = max(WARN_AT, d["used_pct"] - d["used_pct"] % STEP) if d["type"] == "ssd" else 0
            prev = told.get(d["key"])
            if isinstance(prev, (int, float)):               # the old shape: just a step
                prev = {"step": prev, "problems": []}
            prev = prev or {"step": -1, "problems": []}
            if prev["step"] >= step and set(d["problems"]) <= set(prev["problems"]):
                continue
            esc = pushover._esc
            if d["type"] == "ssd":
                head = f"<b>{esc(_name(d))}</b> reports <b>{d['used_pct']}%</b> of its rated life used.\n"
                body = (f"{_tb(d['written'])} written"
                        + (f" · about {_tb(d['per_day'])} a day ({d['rate_basis']})" if d["per_day"] else "")
                        + (f"\nAt that pace: about <b>{d['days_left']} days</b> to 100%." if d["days_left"] is not None else ""))
                title = f"nuarr · SSD wear · {_name(d)} {d['used_pct']}%"
                line = f"{d['used_pct']}% used · {_tb(d['written'])} written"
            else:
                head = f"<b>{esc(_name(d))}</b> is reporting surface or cable trouble.\n"
                body = (f"reallocated {d['realloc']} · pending {d['pending']} · uncorrectable {d['uncorr']}"
                        f" · cable errors {d['crc']} · {d['power_hours'] / 8766:.1f} years powered on")
                title = f"nuarr · disk health · {_name(d)}"
                line = "; ".join(d["problems"])[:120]
            msg = head + body + ("\n<font color=\"#e0575b\">" + esc("; ".join(d["problems"])) + "</font>" if d["problems"] else "")
            ok, err = await pushover.send(title, msg, priority=0)
            pushover._remember({"at": time.time(), "arr": "", "title": f"Drive health · {_name(d)}",
                                "detail": line, "ok": ok, "error": err, "kind": "alert", "files": 0,
                                "ptitle": title, "msg": msg})
            if ok:
                told[d["key"]] = {"step": step, "problems": d["problems"]}
                kv_set(KV_TOLD, json.dumps(told))
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"drive health: could not notify: {type(e).__name__}: {e}", "warn")


async def watch() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            drives = await asyncio.to_thread(read, True)
            n, line = summary(drives)
            if n:
                joblog.log(f"drive health: {line}", "warn")
            await _tell_phone(drives)
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"drive health: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(6 * 3600)
