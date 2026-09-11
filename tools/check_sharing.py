# -*- coding: utf-8 -*-
r"""Four background systems must behave as one pool, not four.

Run it:  python tools\check_sharing.py

Two runners are started on overlapping disks with the gate stubbed open, and
the ledger is watched. The two things that must never happen:

  * two systems reading the same spindle at the same moment
  * more files in flight, across every system, than the shared budget

Both were possible until the exclusion moved out of each runner's own lane
list and into the ledger every system already writes to.
"""
import sys, os, asyncio, time
sys.stderr = sys.stdout
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import idle

DISKS = ["NU-DRIVE-1", "NU-DRIVE-2", "NU-DRIVE-3"]
worst_per_disk = {}
worst_total = 0
FAILED = []


async def main():
    global worst_total
    # The gate says yes to everything; this test is about the systems, not it.
    async def open_gate(disk=""):
        return {"busy": False, "why": "", "detail": ""}
    idle.busy = open_gate
    idle.LANES_TOTAL = 3

    def pending_for(tag):
        def p():
            return [{"id": f"{tag}{i}", "disk": DISKS[i % len(DISKS)]}
                    for i in range(12)]
        return p

    async def do_one(it, report=None):
        # While this is held, look at what the whole of nuarr is doing.
        global worst_total
        for _ in range(6):
            seen = {}
            for t in list(idle.TASKS.values()):
                seen.setdefault(t.disk, []).append(t.system)
            for dk, who in seen.items():
                if len(who) > worst_per_disk.get(dk, 0):
                    worst_per_disk[dk] = len(who)
            worst_total = max(worst_total, len(idle.TASKS))
            await asyncio.sleep(0.05)
        return {"ok": True}

    runs = [asyncio.ensure_future(
                idle.run(f"t{n}", f"test system {n}", pending_for(f"s{n}-"),
                         do_one, label=lambda x: str(x["id"]),
                         disk_of=lambda x: x["disk"],
                         system_name=f"test system {n}", empty_s=0.2,
                         pause_s=0.2))
            for n in (1, 2, 3)]
    await asyncio.sleep(6)
    for r in runs:
        r.cancel()
    await asyncio.gather(*runs, return_exceptions=True)

asyncio.run(main())

print("=== START ===")
print("most systems seen on one spindle at once:")
for dk in sorted(worst_per_disk):
    ok = worst_per_disk[dk] <= 1
    print(("  ok   " if ok else "  FAIL ") + f"{dk}: {worst_per_disk[dk]}")
    if not ok:
        FAILED.append(f"{dk} had {worst_per_disk[dk]} readers")
print(f"most files in flight across every system: {worst_total} "
      f"(budget {idle.LANES_TOTAL})")
if worst_total > idle.LANES_TOTAL:
    print("  FAIL over budget")
    FAILED.append(f"{worst_total} in flight against a budget of {idle.LANES_TOTAL}")
else:
    print("  ok   within budget")
print("=== END ===")
if FAILED:
    print("FAILED: " + "; ".join(FAILED))
    sys.exit(1)
print("both rules held")
