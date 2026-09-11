# -*- coding: utf-8 -*-
r"""Seven things the direct-play buffer estimate must get right.

Run it:  python tools\check_buffer.py

NO SERVER, NO PLEX, NO DISK. Every instrument is stubbed, so each case is a
fixed arrangement of facts with one correct answer - which is the only way to
test a thing whose whole job is to be careful about evidence. Each case here
is a bug that was actually shipped; if one of them fails again, it has come
back.
"""
import sys, time, json
sys.stderr = sys.stdout
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import gate as g
FAILED = []


def check(name, got, want, tol=None):
    ok = (abs(float(got) - float(want)) <= tol) if tol is not None else (got == want)
    print(("  ok   " if ok else "  FAIL ") + name + f"  got {got!r}, want {want!r}"
          + (f" +/-{tol}" if tol is not None else ""))
    if not ok:
        FAILED.append(name)


print("=== START ===")

def sess(key, off_ms, file, br=5000, state="playing", dur_ms=1_400_000,
         acct="77", product="Plex for Windows", player="TestBox"):
    return {"session_key": key, "offset_ms": off_ms, "duration_ms": dur_ms,
            "file": file, "state": state, "account_id": acct,
            "product": product, "player": player, "lead_s": None,
            "detail": {"src_bitrate": br}}

def reset():
    g._XFER.clear(); g._HW.clear(); g._SLEADS.clear()
    g._FIRST_POLL_DONE = True
    g._STARTED_AT = time.time() - 9999      # past the join grace
    g._CLIENT_CAP.clear(); g._CLIENT_CAP_BYTES.clear(); g._CLIENT_PEAK_BYTES.clear()

OFFS = {}
BW = []
g._disk_offsets = lambda paths: {k: v for k, v in OFFS.items() if k in paths}
g._bw_stats = lambda: BW
g._sleads_save = lambda live: None
g._caps_save = lambda: None
g.os.path.getsize = lambda p: 1_000_000_000      # 1 GB, 1400 s -> 714 KB/s

# ---------------------------------------------------------------- 1. restore
reset()
F = r"\\?\P:\Anime\Long Name.mkv"
g._SLEADS["S1"] = {"file": g._hp_key(F), "off": 300.0, "lead": 200.0,
                   "at": time.time() - 30}
s = sess("S1", 340_000, F)                  # played 40s since the save
g._estimate_client_leads([s])
print("1  what the last run believed, less what has been played since")
check("restored lead", s.get("lead_s"), 160.0, 0.6)
check("restored flag", s.get("lead_restored"), 1)

# ---------------------------------------------------- 2. long-path handle key
reset()
OFFS = {g._hp_key(F): 500_000_000}          # handle at 50% -> 700s
now = int(time.time())
BW = [{"accountID": "77", "at": now - 2, "bytes": 250_000_000}]
s = sess("S2", 290_000, F); g._estimate_client_leads([s])     # anchor
BW = [{"accountID": "77", "at": now - 1, "bytes": 250_000_000}]
g._XFER["S2"]["last_poll"] = time.time() - 2
s = sess("S2", 300_000, F)                  # playhead at 300s
g._estimate_client_leads([s])
print("2  a \\\\?\\ path still matches the handle Plex holds")
check("long-path lead", s.get("lead_s"), 400.0, 5.0)
check("long-path used the disk", s.get("lead_disk"), 1)

# --------------------------------------------- 3. analysis sweep is rejected
reset()
OFFS = {g._hp_key(F): 60_000_000}           # plausible: 84s in
now = int(time.time())
BW = [{"accountID": "77", "at": now - 2, "bytes": 300_000_000}]
s = sess("S3", 28_000, F); g._estimate_client_leads([s])
BW = [{"accountID": "77", "at": now - 1, "bytes": 300_000_000}]
g._XFER["S3"]["last_poll"] = time.time() - 2
s = sess("S3", 30_000, F)
g._estimate_client_leads([s])
first = s.get("lead_s")
OFFS = {g._hp_key(F): 990_000_000}          # a sweep parks at the end
g._XFER["S3"]["last_poll"] = time.time() - 2
s = sess("S3", 32_000, F)
g._estimate_client_leads([s])
print("3  an analysis handle parked at the end is not believed")
check("sweep ignored", bool(s.get("lead_s") < first + 20), True)

# ------------------------------------------------ 4. two viewers, one file
reset()
OFFS = {g._hp_key(F): 500_000_000}
a = sess("S4a", 300_000, F, acct="1")
b = sess("S4b", 100_000, F, acct="2")
g._estimate_client_leads([a, b])
print("4  two viewers on one file share one handle, so neither may use it")
check("no handle for viewer A", a.get("lead_disk"), None)
check("no handle for viewer B", b.get("lead_disk"), None)

# ---------------------------------------------------------- 5. forward seek
reset()
OFFS = {}
BW = [{"accountID": "77", "at": int(time.time()) - 1, "bytes": 0}]
s = sess("S5", 100_000, F); g._estimate_client_leads([s])
st = g._XFER["S5"]; st["last_poll"] = time.time() - 2
s = sess("S5", 195_000, F)                  # +95s in 2s = an intro skip
g._estimate_client_leads([s])
print("5  a jump the clock cannot explain is a seek")
check("skip re-anchored", g._XFER["S5"]["off0"], 195.0, 0.1)
st = g._XFER["S5"]; st["last_poll"] = time.time() - 2
s = sess("S5", 199_000, F)                  # +4s in 2s = normal
g._estimate_client_leads([s])
check("normal playback kept its anchor", g._XFER["S5"]["off0"], 195.0, 0.1)

# ------------------------------------- 6. the cap is bytes at THIS bitrate
reset()
g._CLIENT_CAP["Plex for Windows"] = 281.0             # learned at 4 Mbps
g._CLIENT_CAP_BYTES["Plex for Windows"] = 139_130_125
g._CLIENT_PEAK_BYTES["Plex for Windows|TestBox"] = 441_802_250
s = sess("S6", 0, F, br=4633)
print("6  capacity is bytes, converted at THIS stream's bitrate")
check("generous ceiling", g._cap_ceiling_s(s, 4633), 762.9, 1.0)
check("conservative claim", g._cap_claim_s(s, 4633), 240.2, 1.0)

# -------------------------------------- 7. a pause must not teach a capacity
reset()
BW = [{"accountID": "77", "at": int(time.time()) - 1, "bytes": 10_000_000}]
s = sess("S7", 100_000, F); g._estimate_client_leads([s])
BW = []                                      # nothing delivered -> a coast
for i in range(1, 4):
    s = sess("S7", 100_000 + i * 30_000, F)
    g._XFER["S7"]["last_poll"] = time.time() - 30
    g._estimate_client_leads([s])
s = sess("S7", 190_000, F, state="paused")   # the viewer pauses mid-coast
g._XFER["S7"]["last_poll"] = time.time() - 1
g._estimate_client_leads([s])
print("7  pressing pause mid-coast teaches nothing about capacity")
check("pause taught nothing", g._CLIENT_CAP.get("Plex for Windows"), None)

print("=== END ===")
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all seven pass")
