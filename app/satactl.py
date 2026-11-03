r"""
nuarr - the storage controllers: which drive hangs off which port, and
what the port has been doing to it

WHY. Twelve spindles sit on two ASMedia ASM1166 cards and the onboard Intel
SATA, and when a port misbehaves the symptom lands on the DRIVE: a write
that took 29.8 s, an IO retried, a "Reset to device" in the System log. The
drive's own SMART table knows nothing of it. Erik, 2026-11-01: "add my sata
controllers here and track info like faults speed issues etc".

WHAT IS READ, AND WHAT IS DELIBERATELY NOT
  * cfgmgr32 for the controllers themselves: name, PCI slot, driver, PnP
    problem code, PCIe link (current and maximum lanes and generation).
    Milliseconds, no PowerShell.
  * WMI Win32_DiskDrive for disk -> storport port (SCSIPort N is the
    "RaidPortN" the event log names) and the port number on the controller
    (SCSIBus); the disk's parent devnode says which controller.
  * The System event log for the faults: storahci/stornvme 129 (reset to
    device), disk 153 (IO retried), 7 (bad block), 11 (controller error),
    51 (paging error), 157 (surprise removal). 30 days, 0.2 s.
  * MSFT_StorageReliabilityCounter for the driver's own per-disk figures:
    uncorrected read/write errors and the worst read/write/flush wait. A
    worst write of ~30,000 ms IS the storport timeout that ends in a reset.
  * IDENTIFY DEVICE through the legacy SMART ioctl, once per disk, for the
    negotiated SATA link speed (word 77) against what the drive supports
    (word 76). Tested 2026-11-01: no reset followed.

  NOT: SMART WRITE LOG (the SCT path) or READ LOG EXT (device statistics,
  SATA Phy counters). Both wedged a port on the ASM1166 for 30 s and
  storahci reset it - 116 resets and 32 retried IOs in one afternoon of
  trying, none in the 29 days before. The drive's Phy error counters would
  be the ideal figures and they are not worth a reset each to read.

HOW IT IS SERVED. snapshot() never blocks the page: it returns the last
answer and a thread builds the next when it is older than its budget
(inventory 10 min, reliability 5 min, events 1 min).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import html
import re
import struct
import subprocess
import threading
import time
import uuid

from .config import NO_WINDOW, hidden_si

INVENTORY_S, RELIABILITY_S, EVENTS_S = 600.0, 300.0, 60.0
DAYS = 30

_LOCK = threading.Lock()
_STATE: dict = {"inv": None, "inv_at": 0.0, "rel": {}, "rel_at": 0.0,
                "ev": [], "ev_at": 0.0, "link": {}, "out": None, "out_at": 0.0,
                "busy": False, "why": ""}

# ---------------------------------------------------------------- cfgmgr32 --
try:
    _cm = ctypes.WinDLL("cfgmgr32")
except OSError:                                              # not Windows
    _cm = None

_CR_SUCCESS = 0
_FILTER_CLASS, _FILTER_PRESENT = 0x200, 0x100
_GUID_HDC = "{4d36e96a-e325-11ce-bfc1-08002be10318}"
_GUID_SCSI = "{4d36e97b-e325-11ce-bfc1-08002be10318}"


class _KEY(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wt.DWORD)]


def _key(g: str, pid: int) -> _KEY:
    k = _KEY()
    ctypes.memmove(k.fmtid, uuid.UUID(g).bytes_le, 16)
    k.pid = pid
    return k


_K_NAME = _key("b725f130-47ef-101a-a5f1-02608c9eebac", 10)
_K_LOC = _key("a45c254e-df1c-4efd-8020-67d146a850e0", 15)
_K_DRVVER = _key("a8b865dd-2e3d-4094-ad97-e593a70c75d6", 3)
_K_PROBLEM = _key("4340a6c5-93fa-4706-972c-7b648008a5a7", 3)
_K_LINK = {k: _key("3ab22e31-8264-4b4e-9af5-a8d2d8e33e62", pid)
           for k, pid in (("gen", 9), ("lanes", 10), ("gen_max", 11), ("lanes_max", 12))}


def _devnode(inst: str):
    dn = wt.DWORD(0)
    if _cm.CM_Locate_DevNodeW(ctypes.byref(dn), inst, 0) != _CR_SUCCESS:
        return None
    return dn.value


def _prop(dn, k: _KEY):
    typ, size = wt.DWORD(0), wt.DWORD(0)
    _cm.CM_Get_DevNode_PropertyW(dn, ctypes.byref(k), ctypes.byref(typ), None, ctypes.byref(size), 0)
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if _cm.CM_Get_DevNode_PropertyW(dn, ctypes.byref(k), ctypes.byref(typ), buf, ctypes.byref(size), 0) != _CR_SUCCESS:
        return None
    t = typ.value
    if t == 0x12:
        return buf.raw.decode("utf-16-le").rstrip("\0")
    if t in (0x07, 0x08):
        return struct.unpack("<I", buf.raw[:4])[0]
    if t == 0x03:
        return buf.raw[0]
    return None


def _id_list(class_guid: str) -> list:
    size = wt.ULONG(0)
    _cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), class_guid, _FILTER_CLASS | _FILTER_PRESENT)
    buf = ctypes.create_unicode_buffer(max(2, size.value))
    _cm.CM_Get_Device_ID_ListW(class_guid, buf, size.value, _FILTER_CLASS | _FILTER_PRESENT)
    return [s for s in buf[:size.value].split("\0") if s]


def _parent(inst: str) -> str:
    dn = _devnode(inst)
    if dn is None:
        return ""
    p = wt.DWORD(0)
    if _cm.CM_Get_Parent(ctypes.byref(p), dn, 0) != _CR_SUCCESS:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    _cm.CM_Get_Device_IDW(p.value, buf, 512, 0)
    return buf.value


# WHAT THE PCI IDS MEAN, for the handful that are here; anything else keeps
# the driver's own name with the ids beside it.
_KNOWN = {
    ("1B21", "1166"): ("ASMedia ASM1166", "SATA", 6),
    ("1B21", "0612"): ("ASMedia ASM1061/1062", "SATA", 2),
    ("1B21", "1064"): ("ASMedia ASM1064", "SATA", 4),
    ("8086", "A282"): ("Intel 200-series PCH SATA (onboard)", "SATA", 6),
    ("8086", "A352"): ("Intel 300-series PCH SATA (onboard)", "SATA", 6),
    ("1000", "0097"): ("LSI SAS3008 HBA", "SAS", 8),
    ("15B7", "5002"): ("WD Black SN750 NVMe", "NVMe", 1),
    ("C0A9", "5427"): ("Crucial P310 NVMe", "NVMe", 1),
    ("15B7", "5006"): ("WD Red SN700 NVMe", "NVMe", 1),
}
_LANE_MBPS = {1: 250, 2: 500, 3: 985, 4: 1969, 5: 3938}


def _controllers() -> list:
    out = []
    for inst in _id_list(_GUID_HDC) + _id_list(_GUID_SCSI):
        if not inst.upper().startswith("PCI\\"):
            continue                       # Storage Spaces, VHD, the pool's enumerator
        dn = _devnode(inst)
        if dn is None:
            continue
        m = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", inst.upper())
        ven, dev = (m.group(1), m.group(2)) if m else ("", "")
        known = _KNOWN.get((ven, dev))
        name = _prop(dn, _K_NAME) or ""
        bus = ("NVMe" if "NVM" in name.upper() else "SATA" if "AHCI" in name.upper() or "SATA" in name.upper()
               else "SAS" if "SAS" in name.upper() else "")
        link = {k: _prop(dn, kk) for k, kk in _K_LINK.items()}
        gen, lanes = link.get("gen") or 0, link.get("lanes") or 0
        out.append({
            "id": inst, "name": name, "model": known[0] if known else f"{name} ({ven}:{dev})",
            "bus": known[1] if known else bus, "ports": known[2] if known else None,
            "slot": _prop(dn, _K_LOC) or "", "driver": _prop(dn, _K_DRVVER) or "",
            "problem": int(_prop(dn, _K_PROBLEM) or 0),
            "pcie": {"gen": gen, "lanes": lanes, "gen_max": link.get("gen_max") or 0,
                     "lanes_max": link.get("lanes_max") or 0,
                     "mbps": (_LANE_MBPS.get(gen, 0) * lanes) if gen and lanes else 0,
                     "onboard": not gen},          # the PCH's SATA has no PCIe link of its own
            "disks": [],
        })
    return out


def _disks() -> list:
    """disk number -> storport port, port on the controller, model, parent."""
    r = subprocess.run(["wmic", "diskdrive", "get", "Index,SCSIPort,SCSIBus,Model,PNPDeviceID,Size", "/format:csv"],
                       capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW, startupinfo=hidden_si())
    rows = [ln.split(",") for ln in r.stdout.splitlines() if ln.strip()]
    if not rows:
        return []
    hdr, out = rows[0], []
    for row in rows[1:]:
        d = dict(zip(hdr, row))
        try:
            n = int(d.get("Index") or -1)
        except ValueError:
            continue
        pnp = html.unescape(d.get("PNPDeviceID") or "").replace("\\\\", "\\")
        out.append({"n": n, "raidport": int(d.get("SCSIPort") or -1), "port": int(d.get("SCSIBus") or -1),
                    "model": (d.get("Model") or "").strip(), "parent": _parent(pnp) if pnp else ""})
    return out


def _inventory() -> dict:
    ctls = _controllers()
    by_id = {c["id"].upper(): c for c in ctls}
    for d in _disks():
        c = by_id.get((d["parent"] or "").upper())
        if c is None:
            continue
        c["disks"].append(d)
        c.setdefault("raidports", set()).add(d["raidport"])
    for c in ctls:
        c["raidports"] = sorted(c.get("raidports") or [])
        c["disks"].sort(key=lambda x: x["port"])
        # AN NVMe DRIVE IS ITS OWN CONTROLLER, so one missing from _KNOWN is
        # named after the drive rather than Windows' inbox driver: "Standard
        # NVM Express Controller (15B7:5006)" was the WD Red SN700 F: drive.
        if c["bus"] == "NVMe" and c["model"].startswith(c["name"] + " (") and len(c["disks"]) == 1:
            dm = (c["disks"][0].get("model") or "").strip()
            if dm:
                c["model"] = f"{dm} NVMe" if "NVME" not in dm.upper() else dm
    # SATA first, by slot; NVMe after.
    ctls.sort(key=lambda c: (0 if c["bus"] == "SATA" else 1 if c["bus"] == "SAS" else 2, c["slot"]))
    return {"controllers": ctls}


# ------------------------------------------------------------- the faults --
_EVENT_IDS = (129, 153, 7, 11, 51, 157, 154)
_WHAT = {129: "reset to device", 153: "IO retried", 7: "bad block", 11: "controller error",
         51: "paging error", 157: "surprise removal", 154: "IO error"}


def _events(days: int = DAYS) -> list:
    """[{t, provider, id, raidport|None, disk|None}] newest first."""
    try:
        import win32evtlog
    except ImportError:
        return []
    q = ("*[System[(Provider[@Name='storahci'] or Provider[@Name='stornvme'] or Provider[@Name='disk'] "
         "or Provider[@Name='storport']) and (" + " or ".join(f"EventID={i}" for i in _EVENT_IDS) + ")"
         f" and TimeCreated[timediff(@SystemTime) <= {days * 86400000}]]]")
    h = win32evtlog.EvtQuery("System", win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection, q)
    out = []
    while True:
        evs = win32evtlog.EvtNext(h, 256)
        if not evs:
            break
        for e in evs:
            x = win32evtlog.EvtRender(e, win32evtlog.EvtRenderEventXml)
            m = re.search(r"SystemTime='([^']+)'", x)
            prov = re.search(r"Provider Name='([^']+)'", x)
            eid = re.search(r"<EventID[^>]*>(\d+)</EventID>", x)
            data = re.findall(r"<Data[^>]*>([^<]*)</Data>", x)
            blob = " ".join(data)
            rp = re.search(r"RaidPort(\d+)", blob)
            dk = re.search(r"Harddisk(\d+)", blob)
            if not dk and eid and eid.group(1) == "153" and len(data) > 2 and data[2].isdigit():
                dk = re.match(r"(\d+)", data[2])
            try:
                t = time.mktime(time.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone \
                    if m else 0.0
                # the log stamps UTC; strptime gave a naive local - correct for DST too
                t = _utc_to_local(m.group(1)[:19]) if m else 0.0
            except Exception:                                # noqa: BLE001
                t = 0.0
            out.append({"t": t, "provider": prov.group(1) if prov else "", "id": int(eid.group(1)) if eid else 0,
                        "raidport": int(rp.group(1)) if rp else None, "disk": int(dk.group(1)) if dk else None})
    return out


def _utc_to_local(s: str) -> float:
    import calendar
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")))


# ----------------------------------------------- the driver's own figures --
def _reliability() -> dict:
    """disk number -> {rd_unc, wr_unc, rd_max_ms, wr_max_ms, flush_max_ms}"""
    out: dict = {}
    try:
        import pythoncom
        import win32com.client as w
    except ImportError:
        return out
    def num(v):
        # WMI hands uint64 over COM as a STRING; None stays None
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None

    def _query() -> None:
        # Its own frame, so every COM object is released before CoUninitialize
        # (released after it, pythoncom logs "Win32 exception releasing IUnknown").
        s = w.GetObject("winmgmts:root/Microsoft/Windows/Storage")
        for pd in s.ExecQuery("SELECT DeviceId,ObjectId FROM MSFT_PhysicalDisk"):
            oid = str(pd.ObjectId).replace("\\", "\\\\").replace('"', '\\"')
            for rc in s.ExecQuery('ASSOCIATORS OF {MSFT_PhysicalDisk.ObjectId="%s"} '
                                  'WHERE ResultClass=MSFT_StorageReliabilityCounter' % oid):
                try:
                    n = int(pd.DeviceId)
                except (TypeError, ValueError):
                    continue
                out[n] = {"rd_unc": num(rc.ReadErrorsUncorrected), "wr_unc": num(rc.WriteErrorsUncorrected),
                          "rd_max_ms": num(rc.ReadLatencyMax), "wr_max_ms": num(rc.WriteLatencyMax),
                          "flush_max_ms": num(rc.FlushLatencyMax)}

    pythoncom.CoInitialize()
    try:
        _query()
    except Exception:                                        # noqa: BLE001
        pass
    finally:
        pythoncom.CoUninitialize()
    return out


# ---------------------------------------------------------- the link speed --
_GEN = {1: 1.5, 2: 3.0, 3: 6.0}


def _link_speed(n: int) -> dict:
    """IDENTIFY DEVICE through the legacy SMART ioctl - the one path proven
    not to upset the ASM1166. {gbps, max_gbps} or {}."""
    from . import diskwear as dw
    h = dw._open(rf"\\.\PhysicalDrive{n}", 0xC0000000)
    if not h:
        return {}
    try:
        regs = bytes([0, 1, 1, 0, 0, 0xA0, 0xEC, 0])
        raw = dw._ioctl(h, dw._IOCTL_SMART_RCV, struct.pack("<I", 512) + regs + b"\0" * 21, 16 + 512)
    finally:
        dw._k32.CloseHandle(h)
    if not raw or len(raw) < 16 + 512:
        return {}
    w76, w77 = struct.unpack_from("<HH", raw, 16 + 76 * 2)
    cur = _GEN.get((w77 >> 1) & 7)
    sup = [_GEN[g] for g in (1, 2, 3) if w76 & (1 << g)]
    if not cur and not sup:
        return {}
    return {"gbps": cur, "max_gbps": max(sup) if sup else None}


# ---------------------------------------------------------------- assembly --
def _build() -> dict:
    now = time.time()
    if _STATE["inv"] is None or now - _STATE["inv_at"] > INVENTORY_S:
        _STATE["inv"], _STATE["inv_at"] = _inventory(), now
    if now - _STATE["rel_at"] > RELIABILITY_S:
        _STATE["rel"], _STATE["rel_at"] = _reliability(), now
    if now - _STATE["ev_at"] > EVENTS_S:
        _STATE["ev"], _STATE["ev_at"] = _events(), now
    inv, rel, ev = _STATE["inv"], _STATE["rel"], _STATE["ev"]
    try:
        from . import diskwear as dw
        drives = {d.get("disk"): d for d in dw.read()}
    except Exception:                                        # noqa: BLE001
        drives = {}
    day, week = now - 86400, now - 7 * 86400
    ctls = []
    for c in inv["controllers"]:
        rps = set(c["raidports"])
        dnums = {d["n"] for d in c["disks"]}
        mine = [e for e in ev if (e["raidport"] in rps) or (e["disk"] in dnums)]
        resets = [e for e in mine if e["id"] == 129]
        retried = [e for e in mine if e["id"] == 153]
        other = [e for e in mine if e["id"] not in (129, 153)]
        ports = []
        for d in c["disks"]:
            n = d["n"]
            if c["bus"] == "SATA" and n not in _STATE["link"]:
                try:
                    _STATE["link"][n] = _link_speed(n)
                except Exception:                            # noqa: BLE001
                    _STATE["link"][n] = {}
            lk = _STATE["link"].get(n) or {}
            dr = drives.get(n) or {}
            r = rel.get(n) or {}
            ports.append({
                "port": d["port"], "disk": n, "model": d["model"],
                "label": dr.get("name") or f"disk {n}", "type": dr.get("type") or "",
                "warn_drive": bool(dr.get("warn")),
                "link_gbps": lk.get("gbps"), "link_max_gbps": lk.get("max_gbps"),
                "link_down": bool(lk.get("gbps") and lk.get("max_gbps") and lk["gbps"] < lk["max_gbps"]),
                "crc": dr.get("crc"), "timeouts": dr.get("timeouts"),
                "wr_max_ms": r.get("wr_max_ms"), "rd_max_ms": r.get("rd_max_ms"),
                "flush_max_ms": r.get("flush_max_ms"),
                "rd_unc": r.get("rd_unc"), "wr_unc": r.get("wr_unc"),
                "timed_out": bool((r.get("wr_max_ms") or 0) >= 25000 or (r.get("rd_max_ms") or 0) >= 25000),
                "retried_24h": sum(1 for e in retried if e["disk"] == n and e["t"] >= day),
                "retried_30d": sum(1 for e in retried if e["disk"] == n),
                "last_retried": max([e["t"] for e in retried if e["disk"] == n] or [0]),
            })
        p = c["pcie"]
        ctls.append({
            "id": c["id"], "model": c["model"], "name": c["name"], "bus": c["bus"], "slot": c["slot"],
            "driver": c["driver"], "problem": c["problem"], "ports_total": c["ports"],
            "raidports": c["raidports"], "pcie": p,
            # FEWER LANES THAN THE CARD HAS is a fault (a bad slot, a bent pin);
            # a lower generation is usually the slot it is in - a gen4 drive on
            # a gen3 board - and is only noted.
            "pcie_down": bool(p["lanes"] and p["lanes_max"] and p["lanes"] < p["lanes_max"]),
            "pcie_slower": bool(p["gen"] and p["gen_max"] and p["gen"] < p["gen_max"]),
            "resets_24h": sum(1 for e in resets if e["t"] >= day),
            "resets_7d": sum(1 for e in resets if e["t"] >= week),
            "resets_30d": len(resets), "last_reset": max([e["t"] for e in resets] or [0]),
            "retried_24h": sum(1 for e in retried if e["t"] >= day), "retried_30d": len(retried),
            "other_30d": [{"what": _WHAT.get(e["id"], str(e["id"])), "t": e["t"], "disk": e["disk"]} for e in other[:8]],
            "ports": ports,
        })
    # resets the log names by port that no controller here claims (a port with no disk, or NVMe)
    claimed = {rp for c in inv["controllers"] for rp in c["raidports"]}
    stray = [e for e in ev if e["id"] == 129 and e["raidport"] not in claimed]
    return {"at": now, "controllers": ctls, "days": DAYS,
            "stray_resets_30d": len(stray), "events_30d": len(ev)}


def _refresh() -> None:
    try:
        out = _build()
        _STATE.update(out=out, out_at=time.time(), why="")
    except Exception as e:                                   # noqa: BLE001
        _STATE["why"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        _STATE["busy"] = False


def snapshot() -> dict:
    """Never waits. The last answer, and a thread builds the next when it
    is older than a minute (the event window is the freshest part)."""
    if _cm is None:
        return {"why": "not Windows", "controllers": []}
    if (time.time() - _STATE["out_at"] > EVENTS_S) and not _STATE["busy"]:
        with _LOCK:
            if not _STATE["busy"]:
                _STATE["busy"] = True
                threading.Thread(target=_refresh, daemon=True, name="satactl").start()
    out = _STATE["out"]
    if out is None:
        return {"building": True, "controllers": [], "why": _STATE["why"]}
    return dict(out, why=_STATE["why"], stale_s=round(time.time() - _STATE["out_at"]))


def summary() -> tuple:
    """(n, line) for the Health page: what needs a person at the controllers.
    Resets in the last 24 h, a PnP problem code, a card running below its
    PCIe link, or a SATA link negotiated below what the drive supports."""
    s = snapshot()
    ctls = s.get("controllers") or []
    if not ctls:
        return 0, ("storage controllers not read yet" if s.get("building") else (s.get("why") or "no controllers found"))
    bits, n = [], 0
    for c in ctls:
        if c["resets_24h"]:
            n += c["resets_24h"]
            bits.append(f"{c['model']}: {c['resets_24h']} port reset(s) in 24 h, last "
                        f"{time.strftime('%H:%M', time.localtime(c['last_reset']))}")
        if c["problem"]:
            n += 1
            bits.append(f"{c['model']}: PnP problem code {c['problem']}")
        if c["pcie_down"]:
            n += 1
            bits.append(f"{c['model']}: PCIe x{c['pcie']['lanes']} of the x{c['pcie']['lanes_max']} it has")
        for p in c["ports"]:
            if p["link_down"]:
                n += 1
                bits.append(f"{p['label']}: SATA {p['link_gbps']} Gb/s, drive supports {p['link_max_gbps']}")
    if not bits:
        sata = [c for c in ctls if c["bus"] == "SATA"]
        drives = sum(len(c["ports"]) for c in ctls)
        return 0, (f"{len(ctls)} controllers, {drives} drives, every SATA link at full speed, "
                   f"no port reset in 24 h" + (f" ({sum(c['resets_30d'] for c in sata)} in 30 d)" if any(c["resets_30d"] for c in sata) else ""))
    return n, "; ".join(bits)[:300]
