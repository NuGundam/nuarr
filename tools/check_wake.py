# -*- coding: utf-8 -*-
r"""A rule turned on mid-pass must not wait out the pass.

Run it:  python tools\check_wake.py

The runner reads pending() when its queue empties, which is right - re-reading
a forty-thousand-row walk between every file would cost more than the work.
The consequence nobody had asked about is that everything learned during a
pass is learned at the END of it, and a pass over a library this size is most
of a day. Turning a rule on for a second library was invisible until then; a
restart fixed it, because a restart re-reads the list.

Two cases here: a runner mid-pass, and a runner that has already found nothing
and gone to sleep. Both have to notice.
"""
import sys, os, asyncio
sys.stderr = sys.stdout
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import idle

FAILED = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: got {got!r}, want {want!r}")
    if not ok:
        FAILED.append(name)


async def main():
    async def open_gate(disk=""):
        return {"busy": False, "why": "", "detail": ""}
    idle.busy = open_gate
    idle.LANES_TOTAL = 2

    # ---- 1. mid-pass ------------------------------------------------------
    admitted = {"on": False}

    def pending():
        n = 40 if admitted["on"] else 6
        return [{"id": i, "disk": f"D{i % 4}"} for i in range(n)]

    async def slow(it, report=None):
        await asyncio.sleep(0.25)
        return {"ok": True}

    run = asyncio.ensure_future(
        idle.run("w1", "waker", pending, slow, label=lambda x: str(x["id"]),
                 disk_of=lambda x: x["disk"], system_name="waker",
                 empty_s=0.5, pause_s=0.2))
    await asyncio.sleep(0.9)
    d = idle.state("w1")
    print("1  a rule turned on while a pass is running")
    check("total before", d.get("total"), 6)
    admitted["on"] = True                     # the rule goes on
    idle.bump("w1")                           # the save rings the bell
    await asyncio.sleep(0.9)
    got = idle.state("w1").get("total")
    print(f"  total after the bell: {got}")
    check("noticed without finishing the pass", got > 6, True)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)

    # ---- 2. already asleep on an empty list -------------------------------
    idle.STATES.pop("w2", None)
    have = {"on": False}

    def pending2():
        return [{"id": i, "disk": f"E{i}"} for i in range(3)] if have["on"] else []

    run2 = asyncio.ensure_future(
        idle.run("w2", "sleeper", pending2, slow, label=lambda x: str(x["id"]),
                 disk_of=lambda x: x["disk"], system_name="sleeper",
                 empty_s=60.0, pause_s=0.2))
    await asyncio.sleep(0.6)
    print("2  a rule turned on after it had already found nothing")
    check("asleep with nothing to do", idle.state("w2").get("idle_why"),
          "nothing waiting")
    have["on"] = True
    idle.bump("w2")
    await asyncio.sleep(3.0)                  # far less than empty_s of 60
    got2 = idle.state("w2").get("total")
    print(f"  total after the bell: {got2}")
    check("woke instead of sleeping out its minute", (got2 or 0) > 0, True)
    run2.cancel()
    await asyncio.gather(run2, return_exceptions=True)

print("=== START ===")
asyncio.run(main())
print("=== END ===")
if FAILED:
    print("FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("both cases noticed")
