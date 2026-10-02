r"""SSD wear: what each NVMe drive says about its own endurance.

WHY. Erik, 2026-10-02: StableBit Scanner showed the staging drive F: (a
Crucial P310 500 GB, every download lands on it) at 90% of its rated life,
270 TB written in 6,163 powered hours - about a terabyte a day. Nothing in
nuarr would have said so; it was found by opening Scanner. This reads the
same figure from the drive and puts it on the Health page, with how fast it
is being used and roughly how long is left, and tells the phone once per
five points past the warning line.

WHERE THE NUMBER COMES FROM. Every NVMe drive keeps a SMART / Health log
(log page 02h): byte 5 is "Percentage Used", the drive's own estimate of how
much of its rated endurance is spent; bytes 48-63 count data written in units
of 512,000 bytes. Windows hands that page to an administrator through
IOCTL_STORAGE_QUERY_PROPERTY (StorageDeviceProtocolSpecificProperty), which
is what Scanner reads. Get-StorageReliabilityCounter's "Wear" was 0 for this
drive, so it is not used.

Some firmware reports 0% long after real wear (the WD drives here say 0% at
74-198 TB written). The written total is always shown beside it, so a 0% next
to a large number reads as "the drive does not say", not as "new".
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import string
import struct
import time

from . import joblog
from .db import kv_get, kv_set

WARN_AT = 80            # % used: the row turns amber, and the phone is told
STEP = 5                # then once more every 5 points
CACHE_S = 600.0
KV_SAMPLES = "diskwear.samples"     # {serial-ish key: [[t, written_bytes], ...]}
KV_TOLD = "diskwear.told"           # {key: last % step the phone heard about}

_IOCTL_QUERY = 0x002D1400
_IOCTL_DEVNUM = 0x002D1080
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
_BAD = wt.HANDLE(-1).value

_CACHE: dict = {"at": 0.0, "drives": []}


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


def _health(h) -> dict | None:
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


def _letters() -> dict:
    """{physical disk number: ['F', ...]} for the lettered volumes."""
    out: dict = {}
    for L in string.ascii_uppercase:
        h = _open(rf"\\.\{L}:")
        if not h:
            continue
        try:
            raw = _ioctl(h, _IOCTL_DEVNUM, b"", 12)
            if raw and len(raw) >= 8:
                out.setdefault(struct.unpack_from("<I", raw, 4)[0], []).append(L)
        finally:
            _k32.CloseHandle(h)
    return out


def _rate(key: str, written: int, power_hours: int) -> tuple[float, str]:
    """Bytes written per day: from nuarr's own samples once they span a day
    or more (recent use), otherwise the drive's lifetime average."""
    now = time.time()
    try:
        allk = json.loads(kv_get(KV_SAMPLES) or "{}")
    except Exception:                                            # noqa: BLE001
        allk = {}
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
            f"last {round((now - first[0]) / 86400)} days"
    if power_hours:
        return written / (power_hours / 24), "lifetime average"
    return 0.0, ""


def read(fresh: bool = False) -> list:
    """Every NVMe drive: model, letters, wear, written, rate, days left."""
    if not fresh and time.time() - _CACHE["at"] < CACHE_S:
        return _CACHE["drives"]
    letters = _letters()
    drives = []
    for n in range(0, 32):
        h = _open(rf"\\.\PhysicalDrive{n}", 0xC0000000)
        if not h:
            continue
        try:
            hl = _health(h)
            if not hl:
                continue                       # not NVMe, or no health log
            model = _model(h)
        finally:
            _k32.CloseHandle(h)
        key = f"{model}#{n}"
        per_day, basis = _rate(key, hl["written"], hl["power_hours"])
        used = hl["used_pct"]
        days_left = None
        if used and used < 100 and hl["written"] and per_day > 0:
            per_pct = hl["written"] / used
            days_left = round((100 - used) * per_pct / per_day)
        bad = []
        if hl["critical"]:
            bad.append(f"critical warning flag {hl['critical']:#04x}")
        if hl["spare"] <= hl["spare_thr"]:
            bad.append(f"spare {hl['spare']}% at/below its {hl['spare_thr']}% threshold")
        if hl["media_errors"]:
            bad.append(f"{hl['media_errors']} media errors")
        drives.append({"disk": n, "model": model, "letters": letters.get(n, []),
                       **hl, "per_day": per_day, "rate_basis": basis,
                       "days_left": days_left, "problems": bad,
                       "warn": bool(bad) or used >= WARN_AT})
    _CACHE.update(at=time.time(), drives=drives)
    return drives


def _name(d: dict) -> str:
    return (",".join(f"{L}:" for L in d["letters"]) + " " if d["letters"] else "") + \
        (d["model"] or f"disk {d['disk']}")


def _tb(b: float) -> str:
    return f"{b / 1e12:,.0f} TB" if b >= 1e13 else f"{b / 1e12:,.1f} TB"


def summary(drives: list | None = None) -> tuple[int, str]:
    """(warnings, one line) for the Health page."""
    drives = read() if drives is None else drives
    if not drives:
        return 0, "no NVMe drives answered"
    worst = sorted(drives, key=lambda d: (not d["warn"], -d["used_pct"]))
    parts = []
    for d in worst:
        s = f"{_name(d)} {d['used_pct']}% used · {_tb(d['written'])} written"
        if d["warn"]:
            if d["per_day"]:
                s += f" · ~{_tb(d['per_day'])}/day"
            if d["days_left"] is not None:
                s += f" · about {d['days_left']} days to 100%"
            if d["problems"]:
                s += " · " + "; ".join(d["problems"])
        parts.append(s)
    return sum(1 for d in drives if d["warn"]), " | ".join(parts)


async def _tell_phone(drives: list) -> None:
    try:
        from . import pushover
        if not pushover.enabled():
            return
        told = json.loads(kv_get(KV_TOLD) or "{}")
        for d in drives:
            if not d["warn"]:
                continue
            key = f"{d['model']}#{d['disk']}"
            step = max(WARN_AT, d["used_pct"] - d["used_pct"] % STEP)
            if told.get(key, 0) >= step and not d["problems"]:
                continue
            msg = (f"<b>{pushover._esc(_name(d))}</b> reports <b>{d['used_pct']}%</b> of its rated life used.\n"
                   f"{_tb(d['written'])} written"
                   + (f" · about {_tb(d['per_day'])} a day ({d['rate_basis']})" if d["per_day"] else "")
                   + (f"\nAt that pace: about <b>{d['days_left']} days</b> to 100%." if d["days_left"] is not None else "")
                   + ("\n<font color=\"#e0575b\">" + pushover._esc("; ".join(d["problems"])) + "</font>" if d["problems"] else ""))
            title = f"nuarr · SSD wear · {_name(d)} {d['used_pct']}%"
            ok, err = await pushover.send(title, msg, priority=0)
            pushover._remember({"at": time.time(), "arr": "", "title": f"SSD wear · {_name(d)}",
                                "detail": f"{d['used_pct']}% used · {_tb(d['written'])} written",
                                "ok": ok, "error": err, "kind": "alert", "files": 0,
                                "ptitle": title, "msg": msg})
            if ok:
                told[key] = step
                kv_set(KV_TOLD, json.dumps(told))
    except Exception as e:                                       # noqa: BLE001
        joblog.log(f"ssd wear: could not notify: {type(e).__name__}: {e}", "warn")


async def watch() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            drives = await asyncio.to_thread(read, True)
            n, line = summary(drives)
            if n:
                joblog.log(f"ssd wear: {line}", "warn")
            await _tell_phone(drives)
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"ssd wear: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(6 * 3600)
