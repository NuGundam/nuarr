r"""The services nurse: Prowlarr, its Cloudflare proxy, and Docker - watched,
and put right when they fall over.

WHY. On 2026-10-19 at 00:26 a Claude desktop update reinstalled its service and
restarted WSL; Docker Desktop's VM died with it and never came back. Byparr
(the FlareSolverr proxy Prowlarr sends Cloudflare-protected sites through)
lives in Docker, so from that minute every indexer that met a Cloudflare
challenge failed - "No connection could be made because the target machine
actively refused it" from 127.0.0.1:8192 - and both arrs raised
IndexerLongTermStatusCheck for 1337x. The Attention tile said "2" for two
days; nothing said WHY, and nothing could fix it, because the fault was two
layers below anything nuarr looked at.

The arrs' health checks report symptoms. This looks at the causes, in order:

    Prowlarr reachable?  ->  its proxies answering on their ports?  ->  if
    not: is the Docker engine up at all?  ->  if not: start Docker Desktop
    (a scheduled task in the interactive session, which is where it must
    run)  ->  if Docker is up but the container is not: `docker start` it
    ->  once the port answers: test the proxy and every indexer Prowlarr
    had marked down, so the arrs' warnings clear now rather than at their
    next scheduled look.

An indexer that keeps failing while the proxy is fine is a different matter -
a public tracker that changed its domain, went behind a new wall or died. It
cannot be fixed here, so after DISABLE_AFTER_DAYS of continuous failure it is
switched off in Prowlarr (the arrs stop warning, searches stop waiting on it)
and tested once a day; the day it answers again it is switched back on. Every
outage is remembered per indexer, so the card can say "down 4 times this
month" - the record that tells you which sources are worth keeping.

AUTO OR MANUAL, like every other correction system: in manual mode it finds
and names, and the buttons on the card do what auto would have done.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from . import joblog
from .db import kv_get, kv_set

POLL_S = 300.0
KV_MODE = "nurse.mode"
KV_URL = "nurse.prowlarr_url"
KV_KEY = "nurse.prowlarr_key"
KV_STATE = "nurse.state"
PROWLARR_DEFAULT = "http://localhost:9696"
PROWLARR_CONFIG = r"C:\ProgramData\Prowlarr\config.xml"
DOCKER_PIPE = r"\\.\pipe\dockerDesktopLinuxEngine"
DOCKER_TASK = "Docker Desktop"          # scheduled task, interactive session
DISABLE_AFTER_DAYS = 3.0
RETEST_EVERY_S = 86400.0
DOCKER_COOLDOWN_S = 3600.0              # one Docker start an hour, at most
CONTAINER_COOLDOWN_S = 1800.0
PORT_WAIT_S = 300.0                     # how long a started Docker gets

NO_WINDOW = 0x08000000

STATE: dict = {"running": False, "at": 0.0, "checked": 0, "findings": [], "actions": [],
               "prowlarr": {}, "proxies": [], "docker": None, "indexers": {}, "stage": ""}


# ------------------------------------------------------------ settings ----
def mode() -> str:
    return "manual" if str(kv_get(KV_MODE) or "auto") == "manual" else "auto"


def set_mode(m: str) -> None:
    kv_set(KV_MODE, "manual" if m == "manual" else "auto")


def prowlarr_url() -> str:
    return (kv_get(KV_URL) or PROWLARR_DEFAULT).rstrip("/")


def prowlarr_key() -> str:
    k = kv_get(KV_KEY) or ""
    if k:
        return k
    # A local Prowlarr keeps its key in its own config; read it rather than
    # asking a person to copy it across.
    try:
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(PROWLARR_CONFIG, encoding="utf-8").read())
        return m.group(1).strip() if m else ""
    except OSError:
        return ""


def set_prowlarr(url: str = "", key: str = "") -> None:
    if url is not None:
        kv_set(KV_URL, (url or "").strip().rstrip("/") or PROWLARR_DEFAULT)
    if key is not None:
        kv_set(KV_KEY, (key or "").strip())


def _memory() -> dict:
    try:
        return json.loads(kv_get(KV_STATE) or "{}") or {}
    except Exception:                                    # noqa: BLE001
        return {}


def _remember(d: dict) -> None:
    kv_set(KV_STATE, json.dumps(d))


# ------------------------------------------------------------- helpers ----
def _api(path: str, method: str = "GET", body=None, timeout: float = 30.0):
    req = urllib.request.Request(
        prowlarr_url() + "/api/v1" + path,
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
        headers={"X-Api-Key": prowlarr_key(), "Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _host_port(url: str) -> tuple[str, int]:
    m = re.match(r"https?://([^/:]+)(?::(\d+))?", url or "")
    if not m:
        return "", 0
    return m.group(1), int(m.group(2) or (443 if url.startswith("https") else 80))


def _local(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "host.docker.internal") or host == socket.gethostname().lower()


def docker_up() -> bool:
    return os.path.exists(DOCKER_PIPE)


def _run(args: list, timeout: float = 60.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                               # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _container_for_port(port: int) -> tuple[str, str]:
    """(container name, status) publishing this host port, or ('', '')."""
    rc, out = _run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"])
    if rc:
        return "", ""
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and re.search(rf":{port}->", parts[2]):
            return parts[0], parts[1]
    return "", ""


def _act(what: str, ok: bool = True) -> None:
    STATE["actions"] = ([{"at": time.time(), "what": what, "ok": ok}] + STATE["actions"])[:30]
    joblog.log(f"services nurse: {what}", "ok" if ok else "warn", system="nurse")


async def _notify(title: str, text: str) -> None:
    try:
        from . import pushover
        if pushover.configured() and pushover.enabled():
            await pushover.send(title, text, url=pushover._link() + "/settings#arrs", url_title="open nuarr")
    except Exception:                                    # noqa: BLE001
        pass


# ---------------------------------------------------------------- check ----
async def check(act: bool | None = None) -> dict:
    """One pass: look, and in auto mode put right what can be put right."""
    if STATE["running"]:
        return snapshot()
    STATE.update(running=True, stage="asking Prowlarr")
    act = (mode() == "auto") if act is None else act
    findings: list[dict] = []
    mem = _memory()
    mem.setdefault("indexers", {})
    mem.setdefault("cool", {})
    now = time.time()
    try:
        # 1. PROWLARR ITSELF
        try:
            st = await asyncio.to_thread(_api, "/system/status")
            health = await asyncio.to_thread(_api, "/health")
            STATE["prowlarr"] = {"ok": True, "version": st.get("version"), "url": prowlarr_url(),
                                 "health": [{"type": h.get("type"), "message": (h.get("message") or "")[:200]}
                                            for h in (health or [])]}
        except Exception as e:                           # noqa: BLE001
            STATE["prowlarr"] = {"ok": False, "error": f"{type(e).__name__}", "url": prowlarr_url(),
                                 "key": bool(prowlarr_key())}
            findings.append({"what": "Prowlarr does not answer", "detail": f"{prowlarr_url()} - {type(e).__name__}"
                             + ("" if prowlarr_key() else "; no API key - set it on the card"),
                             "fix": "", "level": "error"})
            return snapshot()
        # 2. THE PROXIES AND THEIR PORTS
        STATE["stage"] = "probing proxies"
        proxies = await asyncio.to_thread(_api, "/indexerproxy") or []
        prows = []
        down = []
        for p in proxies:
            host_url = next((f.get("value") for f in p.get("fields") or [] if f.get("name") == "host"), "") or ""
            host, port = _host_port(str(host_url))
            up = bool(host) and await asyncio.to_thread(_port_open, host, port)
            row = {"name": p.get("name"), "impl": p.get("implementation"), "host": host_url, "up": up,
                   "local": _local(host), "port": port}
            prows.append(row)
            if not up:
                down.append(row)
        STATE["proxies"] = prows
        # 3. WHY A PROXY IS DOWN, and the fix in order: Docker, then the container
        STATE["docker"] = await asyncio.to_thread(docker_up)
        fixed_proxy = False
        for row in down:
            if not row["local"]:
                findings.append({"what": f"proxy {row['name']} is not answering", "detail": f"{row['host']} - not on this machine, nothing to restart here",
                                 "fix": "", "level": "error"})
                continue
            if not STATE["docker"]:
                f = {"what": "Docker Desktop is not running", "detail": f"so {row['name']} at {row['host']} is down, and every "
                     "Cloudflare-protected indexer fails with it", "fix": "start Docker Desktop", "level": "error"}
                findings.append(f)
                if act and now - float(mem["cool"].get("docker") or 0) > DOCKER_COOLDOWN_S:
                    mem["cool"]["docker"] = now
                    _remember(mem)
                    STATE["stage"] = "starting Docker Desktop"
                    rc, out = await asyncio.to_thread(_run, ["schtasks", "/Run", "/TN", DOCKER_TASK])
                    if rc:
                        _act(f"could not start Docker Desktop: {out.strip()[:140]}", ok=False)
                        continue
                    _act("started Docker Desktop (the scheduled task in the signed-in session)")
                    if await _wait_port(row["host"], row["port"]):
                        f["fixed"] = True
                        fixed_proxy = True
                        STATE["docker"] = True
                    else:
                        _act(f"Docker Desktop started but {row['name']} did not answer within {int(PORT_WAIT_S / 60)} minutes", ok=False)
                elif act:
                    f["detail"] += " - started it less than an hour ago, waiting before trying again"
                continue
            name, status = await asyncio.to_thread(_container_for_port, row["port"])
            f = {"what": f"proxy {row['name']} is not answering", "detail": f"Docker is up; container {name or '?'} is {status or 'not found'}",
                 "fix": f"docker start {name}" if name else "", "level": "error"}
            findings.append(f)
            if act and name and now - float(mem["cool"].get("c:" + name) or 0) > CONTAINER_COOLDOWN_S:
                mem["cool"]["c:" + name] = now
                _remember(mem)
                STATE["stage"] = f"starting {name}"
                rc, out = await asyncio.to_thread(_run, ["docker", "start", name])
                if rc:
                    _act(f"docker start {name} failed: {out.strip()[:140]}", ok=False)
                    continue
                _act(f"started container {name}")
                if await _wait_port(row["host"], row["port"], 120):
                    f["fixed"] = True
                    fixed_proxy = True
        proxies_fine = not [r for r in STATE["proxies"] if not r["up"]] or fixed_proxy
        # 4. THE INDEXERS PROWLARR HAS MARKED DOWN
        STATE["stage"] = "reading indexer status"
        idx = await asyncio.to_thread(_api, "/indexer") or []
        by_id = {i["id"]: i for i in idx}
        status = await asyncio.to_thread(_api, "/indexerstatus") or []
        failing = {}
        for s in status:
            i = by_id.get(s.get("indexerId"))
            if not i:
                continue
            since = _ts(s.get("initialFailure")) or _ts(s.get("mostRecentFailure")) or now
            failing[i["name"]] = {"id": i["id"], "since": since, "till": s.get("disabledTill") or ""}
        # the memory: outages begin and end here
        for name, r in failing.items():
            m = mem["indexers"].setdefault(name, {"outages": 0, "down_s": 0.0, "first_seen": now})
            if not m.get("down_since"):
                m["down_since"] = r["since"]
                m["outages"] = int(m.get("outages") or 0) + 1
        for name, m in mem["indexers"].items():
            if m.get("down_since") and name not in failing and not m.get("off_by_nurse"):
                m["down_s"] = float(m.get("down_s") or 0) + max(0.0, now - float(m["down_since"]))
                m["last_ok"] = now
                m["down_since"] = 0
        # 5. AFTER A PROXY FIX, TEST WHAT WAS DOWN so the arrs clear now
        if fixed_proxy and failing:
            STATE["stage"] = "re-testing indexers"
            back = []
            for name, r in failing.items():
                ok = await asyncio.to_thread(_test_indexer, by_id[r["id"]])
                if ok:
                    back.append(name)
            if back:
                _act(f"after the proxy came back, {len(back)} indexer(s) answered again: {', '.join(back)}")
                await _retest_arrs(back)
                for name in back:
                    failing.pop(name, None)
        # 6. AN INDEXER THAT KEEPS FAILING WHILE THE PROXY IS FINE
        if proxies_fine:
            for name, r in failing.items():
                days = (now - r["since"]) / 86400
                m = mem["indexers"][name]
                if days >= DISABLE_AFTER_DAYS:
                    f = {"what": f"indexer {name} has failed for {days:.1f} days with the proxy fine",
                         "detail": "a tracker that moved, went behind a new wall or died - nothing here can fix it",
                         "fix": "switch it off in Prowlarr and test it daily", "level": "warn"}
                    findings.append(f)
                    if act and not m.get("off_by_nurse"):
                        if await asyncio.to_thread(_enable, by_id[r["id"]], False):
                            m["off_by_nurse"] = now
                            m["last_test"] = now
                            f["fixed"] = True
                            _act(f"switched {name} off in Prowlarr after {days:.1f} days of failure; it is tested daily and comes back by itself")
                            await _notify("nuarr switched an indexer off",
                                          f"<b>{name}</b> failed for {days:.1f} days while the proxy was fine. Off in Prowlarr; tested daily.")
                else:
                    findings.append({"what": f"indexer {name} is failing ({days * 24:.0f} h so far)",
                                     "detail": f"Prowlarr retries it until {r['till'][:16].replace('T', ' ')}; switched off by the nurse after {DISABLE_AFTER_DAYS:g} days",
                                     "fix": "", "level": "info"})
        # 7. THE ONES THE NURSE SWITCHED OFF: a test a day, back on when it passes
        for name, m in mem["indexers"].items():
            if not m.get("off_by_nurse"):
                continue
            i = next((x for x in idx if x["name"] == name), None)
            if i is None:
                m["off_by_nurse"] = 0
                continue
            if i.get("enable"):
                m["off_by_nurse"] = 0            # a person switched it back on
                continue
            if now - float(m.get("last_test") or 0) >= RETEST_EVERY_S:
                m["last_test"] = now
                STATE["stage"] = f"daily test of {name}"
                if await asyncio.to_thread(_test_indexer, i):
                    if await asyncio.to_thread(_enable, i, True):
                        m["off_by_nurse"] = 0
                        m["down_s"] = float(m.get("down_s") or 0) + max(0.0, now - float(m.get("down_since") or now))
                        m["down_since"] = 0
                        m["last_ok"] = now
                        _act(f"{name} answers again - switched back on in Prowlarr")
                        await _notify("nuarr switched an indexer back on", f"<b>{name}</b> answered its daily test.")
                        continue
            findings.append({"what": f"indexer {name} is switched off by the nurse", "detail":
                             f"since {time.strftime('%Y-%m-%d', time.localtime(float(m['off_by_nurse'])))} · tested daily",
                             "fix": "", "level": "info", "off": True, "name": name})
        STATE["indexers"] = mem["indexers"]
        _remember(mem)
        if fixed_proxy:
            await _notify("nuarr fixed the indexer proxy",
                          "\n".join(f"{f['what']} - {f['fix']}" for f in findings if f.get("fixed")) or "the proxy answers again")
    except Exception as e:                               # noqa: BLE001
        findings.append({"what": "the nurse could not finish", "detail": f"{type(e).__name__}: {e}"[:200], "fix": "", "level": "warn"})
        joblog.log(f"services nurse: {type(e).__name__}: {e}", "warn", system="nurse")
    finally:
        STATE.update(running=False, at=time.time(), checked=STATE["checked"] + 1, findings=findings, stage="")
    return snapshot()


async def _wait_port(host: str, port: int, limit: float = PORT_WAIT_S) -> bool:
    t0 = time.time()
    while time.time() - t0 < limit:
        STATE["stage"] = f"waiting for {host}:{port} ({int(time.time() - t0)}s)"
        if await asyncio.to_thread(_port_open, host, port):
            return True
        await asyncio.sleep(10)
    return False


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:                                    # noqa: BLE001
        return 0.0


def _test_indexer(i: dict) -> bool:
    try:
        _api("/indexer/test", "POST", i, timeout=120)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def _enable(i: dict, on: bool) -> bool:
    try:
        body = dict(i, enable=bool(on))
        _api(f"/indexer/{i['id']}", "PUT", body, timeout=60)
        return True
    except Exception as e:                               # noqa: BLE001
        joblog.log(f"services nurse: could not set {i.get('name')} {'on' if on else 'off'}: {type(e).__name__}", "warn", system="nurse")
        return False


async def _retest_arrs(names: list) -> None:
    """Ask Sonarr and Radarr to test their copies of these indexers, so their
    own 'unavailable' marks clear now instead of at the next RSS pass."""
    from . import arrqueue as q
    from .config import SETTINGS
    want = {n.lower() for n in names}
    for a in SETTINGS.arrs:
        if not a.enabled or a.kind not in ("sonarr", "radarr"):
            continue
        try:
            for i in await asyncio.to_thread(q._call, a, "/api/v3/indexer", "GET", None, 30) or []:
                base = re.sub(r"\s*\(prowlarr\)\s*$", "", i.get("name") or "", flags=re.I).lower()
                if base in want:
                    try:
                        await asyncio.to_thread(q._call, a, "/api/v3/indexer/test", "POST", i, 120)
                    except Exception:                    # noqa: BLE001
                        pass
            await asyncio.to_thread(q._call, a, "/api/v3/command", "POST", {"name": "CheckHealth"}, 30)
        except Exception:                                # noqa: BLE001
            pass


def indexer_set(name: str, on: bool) -> dict:
    """The card's own switch: on or off in Prowlarr, and the nurse's note."""
    idx = _api("/indexer") or []
    i = next((x for x in idx if x["name"] == name), None)
    if not i:
        return {"ok": False, "why": "no such indexer"}
    ok = _enable(i, on)
    mem = _memory()
    m = mem.setdefault("indexers", {}).setdefault(name, {})
    m["off_by_nurse"] = 0 if on else time.time()
    m["last_test"] = time.time()
    _remember(mem)
    _act(f"{name} switched {'on' if on else 'off'} in Prowlarr by hand")
    return {"ok": ok}


def snapshot() -> dict:
    d = {k: STATE[k] for k in ("running", "at", "checked", "findings", "actions", "prowlarr", "proxies", "docker", "stage")}
    d["mode"] = mode()
    d["prowlarr_url"] = prowlarr_url()
    d["has_key"] = bool(prowlarr_key())
    d["key_from_config"] = not (kv_get(KV_KEY) or "") and bool(prowlarr_key())
    d["indexers"] = STATE.get("indexers") or _memory().get("indexers") or {}
    d["open"] = len([f for f in STATE["findings"] if f.get("level") in ("error", "warn") and not f.get("fixed")])
    d["disable_after_days"] = DISABLE_AFTER_DAYS
    d["poll_s"] = POLL_S
    return d


async def watch() -> None:
    from . import schedules
    schedules.register(
        "svcnurse", "Services nurse", "Integrations", POLL_S,
        what="Looks below the arrs' health warnings: is Prowlarr up, do its proxies answer, is Docker "
             "running - and starts what is stopped. Switches off an indexer that has failed for days "
             "with the proxy fine, tests it daily and switches it back on.")
    await asyncio.sleep(60)
    while True:
        try:
            schedules.beat("svcnurse")
            r = await check()
            schedules.REG["svcnurse"]["last_result"] = (f"{r['open']} open" if r["open"] else "all up")
        except Exception as e:                           # noqa: BLE001
            joblog.log(f"services nurse: {type(e).__name__}: {e}", "warn", system="nurse")
        await asyncio.sleep(POLL_S)
