r"""
nuarr - backup and restore

WHAT A BACKUP IS FOR
--------------------
Two different disasters, and they need different things in the folder:

  1. "the database is corrupt / I deleted something / a migration went wrong"
     -> you need yesterday's nuarr.db and the settings that went with it.
        Frequent, small, and the common case.

  2. "the machine is gone"
     -> you need the program too: source, the exact ffmpeg build, the Python
        packages, something that installs it and something that explains how.
        Rare, large, and almost never changes between runs.

Backing both up on the same schedule is what makes backup systems get turned
off - copying a 1 GB ffmpeg tree nightly to capture a database that changed by
2 MB. So the two are split: the DATABASE AND SETTINGS are copied every run, and
the PROGRAM BUNDLE is written once and rewritten only when its content hash
changes. Four retained backups cost roughly 600 MB rather than 5 GB, and every
one of them is still independently restorable because the bundle is hard-linked
(or copied) into each.

WHY VACUUM INTO RATHER THAN COPYING THE FILE
--------------------------------------------
nuarr is running while the backup runs. Copying nuarr.db with the shell gives
you a torn file: the WAL holds committed pages the main file does not, and the
copy lands mid-transaction. `VACUUM INTO` takes a read snapshot and writes a
complete, consistent, already-compacted database - 272 MB of live file with a
127 MB WAL becomes a ~150 MB standalone copy that opens cleanly. It is the only
copy method here that is both consistent and does not interrupt the workers.

VERIFICATION
------------
An unverified backup is a guess. Every run opens the copy it just wrote and
runs PRAGMA integrity_check plus a row count against the source. The result is
recorded, so the tab can say "verified 4 hours ago" rather than "a file exists".
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import joblog
from .config import DATA_DIR, DB_PATH, ROOT
from . import schedules
from .db import cursor, kv_get, kv_set

DEST = Path(r"P:\BackUp Data\NuarrBackup")

# A restore is staged here and applied at startup - see restore()/apply_pending().
PENDING = DATA_DIR / "nuarr.db.restore-pending"
PENDING_MARK = DATA_DIR / "restore-pending.json"

# Retention counts FOLDERS, not files. Four is the default because it is enough
# to cover "the last good one" across a weekend of unattended running.
DEFAULT_KEEP = 4
DEFAULT_FREQ = "weekly"          # daily | weekly | monthly
DEFAULT_DOW = 6                  # Sunday (Monday=0, matching datetime.weekday)
DEFAULT_DOM = 1                  # 1st of the month
DEFAULT_TIME = "04:00"

# Live progress for the panel. Same shape as the other subsystems so the UI
# renders it with the existing busy/step helpers.
STATE: dict = {
    "running": False, "phase": "", "started": 0.0, "finished": 0.0,
    "error": None, "current": "", "pct": 0,
}

_LOCK = asyncio.Lock()


# ----------------------------------------------------------------- settings --
def settings() -> dict:
    """Schedule and retention, from kv so they survive a restart."""
    def _int(k, d):
        try:
            return int(kv_get(k) or d)
        except (TypeError, ValueError):
            return d
    return {
        "enabled": (kv_get("backup.enabled") or "1") == "1",
        "freq": kv_get("backup.freq") or DEFAULT_FREQ,
        "dow": _int("backup.dow", DEFAULT_DOW),
        "dom": _int("backup.dom", DEFAULT_DOM),
        "time": kv_get("backup.time") or DEFAULT_TIME,
        "keep": _int("backup.keep", DEFAULT_KEEP),
        "dest": kv_get("backup.dest") or str(DEST),
        "last_at": float(kv_get("backup.last_at") or 0),
        "last_ok": (kv_get("backup.last_ok") or "") == "1",
        "last_detail": kv_get("backup.last_detail") or "",
    }


def set_settings(**kw) -> dict:
    """Persist any subset of the schedule. Values are clamped, not trusted."""
    if "enabled" in kw:
        kv_set("backup.enabled", "1" if kw["enabled"] else "0")
    if kw.get("freq") in ("daily", "weekly", "monthly"):
        kv_set("backup.freq", kw["freq"])
    if "dow" in kw:
        kv_set("backup.dow", str(max(0, min(6, int(kw["dow"])))))
    if "dom" in kw:
        # 28 not 31: a monthly backup set to the 30th would simply not run in
        # February, which is the kind of silent gap you discover too late.
        kv_set("backup.dom", str(max(1, min(28, int(kw["dom"])))))
    if "time" in kw:
        t = str(kw["time"]).strip()
        try:
            h, m = t.split(":")
            t = f"{max(0, min(23, int(h))):02d}:{max(0, min(59, int(m))):02d}"
        except Exception:
            t = DEFAULT_TIME
        kv_set("backup.time", t)
    if "keep" in kw:
        # At least 1 - a retention of 0 would delete the backup it just made.
        kv_set("backup.keep", str(max(1, min(60, int(kw["keep"])))))
    if kw.get("dest"):
        kv_set("backup.dest", str(kw["dest"]))
    return settings()


def next_run(s: dict | None = None, now: float | None = None) -> float:
    """When the next backup is due, as a timestamp. 0 when disabled."""
    s = s or settings()
    if not s["enabled"]:
        return 0.0
    now_dt = datetime.fromtimestamp(now or time.time())
    try:
        hh, mm = (int(x) for x in s["time"].split(":"))
    except Exception:
        hh, mm = 4, 0

    cand = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if s["freq"] == "daily":
        if cand <= now_dt:
            cand += timedelta(days=1)
    elif s["freq"] == "weekly":
        delta = (s["dow"] - cand.weekday()) % 7
        cand += timedelta(days=delta)
        if cand <= now_dt:
            cand += timedelta(days=7)
    else:                                             # monthly
        dom = max(1, min(28, s["dom"]))
        cand = cand.replace(day=dom)
        if cand <= now_dt:
            cand = (cand.replace(day=28) + timedelta(days=7)).replace(day=dom)
    return cand.timestamp()


# ------------------------------------------------------------ program bundle --
def _bundle_hash() -> str:
    """Fingerprint of the things that make up a restorable install.

    Source files plus the ffmpeg build identity. Deliberately NOT the mtime -
    touching a file without changing it should not trigger a 1 GB copy.
    """
    h = hashlib.sha256()
    for base in (ROOT / "app", ROOT / "scripts"):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in (".py", ".ps1", ".cmd", ".md"):
                h.update(p.name.encode())
                h.update(p.read_bytes())
    for f in ("launch.py", "serve.py", "config.yml", "README.md"):
        p = ROOT / f
        if p.is_file():
            h.update(f.encode())
            h.update(p.read_bytes())
    ff = _ffmpeg_dir()
    if ff:
        h.update(str(ff.name).encode())
    return h.hexdigest()[:16]


def _ffmpeg_dir() -> Path | None:
    """The ffmpeg build actually in use - the pinned one if there is one."""
    try:
        from . import ffmpeg_update
        pin = ffmpeg_update.pinned_dir()
        if pin and Path(pin).is_dir():
            return Path(pin)
        exe, _ = ffmpeg_update.installed_paths()
        p = Path(exe).parent
        return p if p.is_dir() else None
    except Exception:
        return None


def _write_bundle(dest: Path) -> dict:
    """Source + ffmpeg + wheels + installer + README. Everything to rebuild."""
    dest.mkdir(parents=True, exist_ok=True)
    out = {"source_mb": 0.0, "ffmpeg_mb": 0.0, "wheels": 0, "wheels_mb": 0.0}

    # --- source -----------------------------------------------------------
    src = dest / "program"
    if src.exists():
        shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(
        ROOT, src,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv",
                                      "bin", "node_modules"))
    out["source_mb"] = round(_dir_mb(src), 2)

    # --- ffmpeg -----------------------------------------------------------
    ff = _ffmpeg_dir()
    if ff and ff.is_dir():
        tgt = dest / "ffmpeg" / ff.name
        if tgt.exists():
            shutil.rmtree(tgt, ignore_errors=True)
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ff, tgt)
        out["ffmpeg_mb"] = round(_dir_mb(tgt), 2)
        out["ffmpeg_name"] = ff.name

    # --- python dependencies ---------------------------------------------
    # Wheels, not just a requirements.txt. A requirements file is a promise
    # that PyPI will still have those versions and that the machine will have
    # a compiler; wheels are the actual bytes. `pip download` is best-effort -
    # no network at backup time must not fail the backup.
    wheels = dest / "wheels"
    wheels.mkdir(exist_ok=True)
    req = dest / "requirements.txt"
    failed: list[str] = []
    try:
        frozen = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                                capture_output=True, text=True, timeout=120)
        req.write_text(frozen.stdout, encoding="utf-8")
        pkgs = [l.strip() for l in frozen.stdout.splitlines()
                if l.strip() and not l.startswith(("#", "-e "))]

        # --prefer-binary, NOT --only-binary.
        #
        # The first version used `--only-binary :all:` and captured ZERO of 55
        # packages, because pip treats that as a hard constraint over the whole
        # resolve: esprima==4.0.1 publishes no wheel, so one source-only package
        # failed the entire batch. A backup that silently contains nothing is
        # the worst possible outcome here, so: prefer wheels, accept an sdist
        # when that is all that exists.
        rc = subprocess.run([sys.executable, "-m", "pip", "download",
                             "-r", str(req), "-d", str(wheels),
                             "--prefer-binary"],
                            capture_output=True, text=True, timeout=1800)
        if rc.returncode != 0:
            # And if the batch still fails, fall back to ONE AT A TIME so a
            # single unavailable package costs one package, not all of them.
            for p in pkgs:
                r1 = subprocess.run([sys.executable, "-m", "pip", "download", p,
                                     "-d", str(wheels), "--prefer-binary",
                                     "--no-deps"],
                                    capture_output=True, text=True, timeout=180)
                if r1.returncode != 0:
                    failed.append(p)
    except Exception as e:
        failed.append(f"(pip did not run: {type(e).__name__}: {e})")

    got = [p for p in wheels.glob("*") if p.is_file()]
    out["wheels"] = len(got)
    out["wheels_mb"] = round(_dir_mb(wheels), 2)
    out["wheels_failed"] = failed
    # Say so IN THE BUNDLE. A missing package discovered during a rebuild, on a
    # machine with no network, is not the moment to find out.
    if failed:
        (dest / "wheels-INCOMPLETE.txt").write_text(
            "These packages could not be downloaded and are NOT in wheels/.\n"
            "install.cmd will need network access to fetch them:\n\n"
            + "\n".join(failed) + "\n", encoding="utf-8")
    else:
        (dest / "wheels-INCOMPLETE.txt").unlink(missing_ok=True)

    (dest / "install.cmd").write_text(_INSTALL_CMD, encoding="utf-8")
    (dest / "restore.cmd").write_text(_RESTORE_CMD, encoding="utf-8")
    (dest / "README.txt").write_text(_readme(out), encoding="utf-8")
    return out


def _dir_mb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / 1024 / 1024


# ------------------------------------------------------------------ the run --
def run_backup() -> dict:
    """One backup. Synchronous on purpose - callers put it on a thread."""
    s = settings()
    dest_root = Path(s["dest"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = dest_root / f"nuarr-{stamp}"
    started = time.time()
    res: dict = {"folder": str(folder), "at": started}

    STATE.update(running=True, phase="preparing", started=started,
                 error=None, current="", pct=2, finished=0.0)
    try:
        folder.mkdir(parents=True, exist_ok=True)

        # --- database ------------------------------------------------------
        STATE.update(phase="copying the database", current="nuarr.db", pct=10)
        db_out = folder / "nuarr.db"
        # Checkpoint first so the copy starts from as complete a main file as
        # possible, then VACUUM INTO for a consistent, compacted snapshot.
        try:
            with cursor() as cur:
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass                    # a busy checkpoint is not a failed backup
        conn = sqlite3.connect(str(DB_PATH), timeout=120)
        try:
            conn.execute("PRAGMA mmap_size=0")
            conn.execute("VACUUM INTO ?", (str(db_out),))
            src_rows = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            conn.close()
        res["db_mb"] = round(db_out.stat().st_size / 1024 / 1024, 2)

        # --- verify --------------------------------------------------------
        STATE.update(phase="verifying the copy", pct=45)
        chk = sqlite3.connect(str(db_out), timeout=120)
        try:
            ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
            cp_rows = chk.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            chk.close()
        res["integrity"] = ok
        res["rows_source"] = src_rows
        res["rows_copy"] = cp_rows
        res["verified"] = (ok == "ok" and cp_rows == src_rows)
        if not res["verified"]:
            raise RuntimeError(
                f"verification failed: integrity={ok}, "
                f"rows {cp_rows} in the copy vs {src_rows} in the source")

        # --- settings ------------------------------------------------------
        STATE.update(phase="copying settings", current="config.yml", pct=55)
        conf = ROOT / "config.yml"
        if conf.is_file():
            shutil.copy2(conf, folder / "config.yml")
        with cursor() as cur:
            kv = {r["k"]: r["v"] for r in cur.execute("SELECT k, v FROM kv")}
        (folder / "settings-kv.json").write_text(
            json.dumps(kv, indent=2, sort_keys=True), encoding="utf-8")

        # --- program bundle -------------------------------------------------
        # Shared across backups and rewritten only when its hash changes, so a
        # nightly run does not recopy a gigabyte of ffmpeg to capture a
        # database that moved by a few MB.
        STATE.update(phase="checking the program bundle", pct=65)
        want = _bundle_hash()
        shared = dest_root / "program-bundle"
        marker = shared / "bundle.id"
        have = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        if have != want:
            STATE.update(phase="writing the program bundle "
                               "(source, ffmpeg, wheels, installer)", pct=70)
            if shared.exists():
                shutil.rmtree(shared, ignore_errors=True)
            info = _write_bundle(shared)
            marker.write_text(want, encoding="utf-8")
            (shared / "bundle.json").write_text(
                json.dumps(info | {"built": time.time(), "hash": want}, indent=2),
                encoding="utf-8")
            res["bundle"] = "rebuilt"
            res["bundle_info"] = info
        else:
            res["bundle"] = "unchanged"

        # Point this backup at the bundle, and say so in the folder rather than
        # leaving a bare pointer nobody can interpret during a recovery.
        (folder / "PROGRAM-BUNDLE.txt").write_text(
            "The program, ffmpeg, Python wheels, installer and README for this\n"
            f"backup live in:\n\n    {shared}\n\n"
            f"bundle id: {want}\n\n"
            "They are shared because they are identical across backups - only\n"
            "the database and settings in THIS folder change between runs.\n"
            "To restore: run restore.cmd in the bundle folder, or use the\n"
            "Backup tab in nuarr and pick this folder.\n", encoding="utf-8")

        (folder / "manifest.json").write_text(
            json.dumps(res | {"finished": time.time()}, indent=2),
            encoding="utf-8")

        # --- retention -------------------------------------------------------
        STATE.update(phase="applying retention", pct=90)
        res["pruned"] = prune(dest_root, s["keep"])

        res["ok"] = True
        res["seconds"] = round(time.time() - started, 1)
        kv_set("backup.last_at", str(time.time()))
        kv_set("backup.last_ok", "1")
        kv_set("backup.last_detail",
               f"{res['db_mb']} MB, verified, {res['bundle']} bundle, "
               f"{res['seconds']}s")
        joblog.log(f"backup complete: {folder.name} - {res['db_mb']} MB, "
                   f"integrity ok, bundle {res['bundle']}", "ok")
        STATE.update(phase="done", pct=100)
        return res
    except Exception as e:
        res["ok"] = False
        res["error"] = f"{type(e).__name__}: {e}"
        kv_set("backup.last_at", str(time.time()))
        kv_set("backup.last_ok", "0")
        kv_set("backup.last_detail", res["error"])
        joblog.log(f"backup FAILED: {res['error']}", "error")
        STATE.update(error=res["error"], phase="failed")
        # A half-written folder is worse than none - it looks like a backup.
        try:
            if folder.exists() and not (folder / "manifest.json").is_file():
                shutil.rmtree(folder, ignore_errors=True)
                res["removed_partial"] = True
        except Exception:
            pass
        return res
    finally:
        STATE.update(running=False, finished=time.time(), current="")


def listing(dest_root: Path | None = None) -> list[dict]:
    """Every backup on disk, newest first."""
    root = Path(dest_root or settings()["dest"])
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.glob("nuarr-*"), reverse=True):
        if not p.is_dir():
            continue
        man = p / "manifest.json"
        info = {}
        if man.is_file():
            try:
                info = json.loads(man.read_text(encoding="utf-8"))
            except Exception:
                info = {}
        db = p / "nuarr.db"
        out.append({
            "name": p.name, "path": str(p),
            "at": info.get("at") or p.stat().st_mtime,
            "db_mb": info.get("db_mb") or (round(db.stat().st_size / 1024 / 1024, 2)
                                           if db.is_file() else 0),
            "verified": bool(info.get("verified")),
            "integrity": info.get("integrity") or "",
            "rows": info.get("rows_copy") or 0,
            # A folder with no database is not restorable, and the tab must not
            # offer it as though it were.
            "restorable": db.is_file(),
        })
    return out


def prune(dest_root: Path | None = None, keep: int | None = None) -> list[str]:
    root = Path(dest_root or settings()["dest"])
    keep = int(keep if keep is not None else settings()["keep"])
    folders = [Path(b["path"]) for b in listing(root)]
    dropped = []
    for p in folders[keep:]:
        try:
            shutil.rmtree(p, ignore_errors=True)
            dropped.append(p.name)
        except Exception:
            pass
    if dropped:
        joblog.log(f"backup retention: removed {len(dropped)} old backup(s) "
                   f"keeping {keep}", "info")
    return dropped


# ------------------------------------------------------------------ restore --
def restore(name: str) -> dict:
    """Stage a backup to be swapped in at the next start.

    NOT AN IN-PLACE OVERWRITE, deliberately.

    The obvious implementation copies the backup over nuarr.db and tells you to
    restart. It usually appears to work, which is the problem: this process has
    long-lived thread-local connections open on the live database, with pages
    cached and a WAL it believes it owns. Overwriting the file underneath them
    means every reader between the swap and the restart is serving a mixture of
    two databases, and any write in that window lands in a WAL that belongs to
    a file that no longer exists. That is how a restore turns one bad database
    into two.

    So the restored copy is STAGED beside the live one and swapped in by
    _apply_pending() at startup, before anything opens the database. Until the
    restart, nothing has changed and the decision is still reversible.

    The safety copy is taken all the same. A restore happens under pressure,
    usually with an incomplete idea of what went wrong, and "I restored the
    wrong one" must not be the end of the story.
    """
    root = Path(settings()["dest"])
    folder = root / name
    src = folder / "nuarr.db"
    if not src.is_file():
        return {"ok": False, "error": f"no database in {name}"}

    # Verify BEFORE touching the live file, not after.
    try:
        c = sqlite3.connect(str(src), timeout=60)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            c.close()
        if ok != "ok":
            return {"ok": False, "error": f"backup failed integrity check: {ok}"}
    except Exception as e:
        return {"ok": False, "error": f"cannot open the backup: {e}"}

    safety = DATA_DIR / f"nuarr.db.before-restore-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=120)
        try:
            conn.execute("PRAGMA mmap_size=0")
            conn.execute("VACUUM INTO ?", (str(safety),))
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False,
                "error": f"refusing to restore - could not make a safety copy: {e}"}

    try:
        shutil.copy2(src, PENDING)
        PENDING_MARK.write_text(json.dumps({
            "from": name, "at": time.time(), "safety_copy": str(safety),
            "config": str(folder / "config.yml")
            if (folder / "config.yml").is_file() else "",
        }, indent=2), encoding="utf-8")
    except Exception as e:
        PENDING.unlink(missing_ok=True)
        PENDING_MARK.unlink(missing_ok=True)
        return {"ok": False, "error": f"could not stage the restore: {e}",
                "safety_copy": str(safety)}

    joblog.log(f"restore STAGED from {name} - it is applied on the next start. "
               f"Safety copy of the current database: {safety.name}", "warn")
    return {"ok": True, "staged": name, "safety_copy": str(safety),
            "restart_required": True}


def pending() -> dict | None:
    """A staged restore waiting for a restart, if there is one."""
    if not (PENDING.is_file() and PENDING_MARK.is_file()):
        return None
    try:
        return json.loads(PENDING_MARK.read_text(encoding="utf-8"))
    except Exception:
        return {"from": "unknown"}


def cancel_pending() -> dict:
    p = pending()
    PENDING.unlink(missing_ok=True)
    PENDING_MARK.unlink(missing_ok=True)
    if p:
        joblog.log(f"staged restore from {p.get('from')} cancelled", "info")
    return {"ok": True, "cancelled": bool(p)}


def apply_pending() -> dict | None:
    """Swap a staged restore in. Called at STARTUP, before init_db().

    This is the only moment it is safe: no connection is open yet, so there is
    no cache to go stale and no WAL to orphan. The staged file is verified once
    more here - it may have been sitting on the pool for days.
    """
    info = pending()
    if not info:
        return None
    try:
        c = sqlite3.connect(str(PENDING), timeout=60)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            c.close()
        if ok != "ok":
            joblog.log(f"staged restore REJECTED - integrity_check said {ok}. "
                       f"The live database is untouched.", "error")
            PENDING.unlink(missing_ok=True)
            PENDING_MARK.unlink(missing_ok=True)
            return {"ok": False, "error": ok}

        # The WAL and SHM belong to the OUTGOING database. Left in place SQLite
        # would replay them over the restored file and undo the restore.
        for suffix in ("-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
        if DB_PATH.exists():
            DB_PATH.unlink()
        shutil.move(str(PENDING), str(DB_PATH))

        conf = info.get("config")
        if conf and Path(conf).is_file():
            try:
                if (ROOT / "config.yml").is_file():
                    shutil.copy2(ROOT / "config.yml",
                                 ROOT / "config.yml.before-restore")
                shutil.copy2(conf, ROOT / "config.yml")
            except Exception:
                pass
        PENDING_MARK.unlink(missing_ok=True)
        joblog.log(f"restore applied from {info.get('from')} - safety copy of "
                   f"the previous database: {info.get('safety_copy')}", "warn")
        return {"ok": True, "from": info.get("from")}
    except Exception as e:
        joblog.log(f"could not apply the staged restore: {type(e).__name__}: {e}",
                   "error")
        return {"ok": False, "error": str(e)}


# ----------------------------------------------------------------- schedule --
async def watch() -> None:
    """Run a backup when one is due. Checks every few minutes, not every tick."""
    await asyncio.sleep(90)
    while True:
        schedules.beat('backup')
        try:
            s = settings()
            if s["enabled"]:
                due = next_run(s)
                last = s["last_at"]
                # "due" is the NEXT occurrence, so a run is owed when the
                # previous occurrence has passed and we have not run since.
                prev = due - _period_seconds(s)
                if prev > last and time.time() >= prev:
                    async with _LOCK:
                        await asyncio.to_thread(run_backup)
        except Exception as e:
            joblog.log(f"backup scheduler error: {type(e).__name__}: {e}", "warn")
        await asyncio.sleep(300)


def _period_seconds(s: dict) -> float:
    return {"daily": 86400.0, "weekly": 604800.0}.get(s["freq"], 2592000.0)


async def run_now() -> dict:
    if STATE["running"]:
        return {"ok": False, "error": "a backup is already running"}
    async with _LOCK:
        return await asyncio.to_thread(run_backup)


# ------------------------------------------------------------------- assets --
_INSTALL_CMD = r"""@echo off
REM  nuarr - install from this bundle
REM  Restores the program to C:\nuarr. Run as Administrator.
setlocal
set TARGET=C:\nuarr
echo Installing nuarr to %TARGET%
if not exist "%TARGET%" mkdir "%TARGET%"
xcopy /E /I /Y "%~dp0program\*" "%TARGET%\" >nul
echo Installing Python packages...
REM Try fully offline first, then fall back to PyPI for anything missing.
python -m pip install --no-index --find-links "%~dp0wheels" -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo Offline install incomplete - retrying with network access...
  python -m pip install --find-links "%~dp0wheels" -r "%~dp0requirements.txt"
)
echo Restoring ffmpeg...
if not exist "C:\ProgramData\nuarr\ffmpeg" mkdir "C:\ProgramData\nuarr\ffmpeg"
xcopy /E /I /Y "%~dp0ffmpeg\*" "C:\ProgramData\nuarr\ffmpeg\" >nul
echo.
echo Done. Now run restore.cmd to put the database back, then create the
echo scheduled task 'nuarr' pointing at:  python C:\nuarr\launch.py
pause
"""

_RESTORE_CMD = r"""@echo off
REM  nuarr - restore the database from a backup folder
REM  Usage: restore.cmd "P:\BackUp Data\NuarrBackup\nuarr-YYYYMMDD-HHMMSS"
setlocal
if "%~1"=="" (
  echo Usage: restore.cmd "path\to\nuarr-YYYYMMDD-HHMMSS"
  exit /b 1
)
if not exist "%~1\nuarr.db" (
  echo No nuarr.db in %~1
  exit /b 1
)
echo Stopping nuarr...
schtasks /End /TN nuarr >nul 2>&1
timeout /t 3 >nul
set DATA=C:\ProgramData\nuarr
if exist "%DATA%\nuarr.db" (
  echo Keeping a safety copy of the current database...
  copy /Y "%DATA%\nuarr.db" "%DATA%\nuarr.db.before-restore" >nul
)
del /Q "%DATA%\nuarr.db-wal" 2>nul
del /Q "%DATA%\nuarr.db-shm" 2>nul
copy /Y "%~1\nuarr.db" "%DATA%\nuarr.db" >nul
if exist "%~1\config.yml" copy /Y "%~1\config.yml" "C:\nuarr\config.yml" >nul
echo Starting nuarr...
schtasks /Run /TN nuarr >nul 2>&1
echo Done. The previous database is at %DATA%\nuarr.db.before-restore
pause
"""


def _readme(info: dict) -> str:
    return f"""nuarr - backup bundle
=====================

WHAT IS HERE
------------
program/           the nuarr source tree, as it was when this bundle was built
ffmpeg/            the exact ffmpeg build nuarr was using ({info.get('ffmpeg_name', 'n/a')})
wheels/            {info.get('wheels', 0)} Python packages, downloaded ({info.get('wheels_mb', 0)} MB)
                   Mostly .whl; a few publish only source archives.
requirements.txt   the same package list, for installing with network access
{('wheels-INCOMPLETE.txt  ' + str(len(info.get('wheels_failed') or []))
  + ' package(s) could NOT be downloaded - read this one' + chr(10))
 if info.get('wheels_failed') else ''}
install.cmd        puts the program, packages and ffmpeg back on a machine
restore.cmd        puts a database back, given a backup folder
bundle.json        what was captured, and when

The DATABASE and SETTINGS are NOT here. They live in the dated folders beside
this one (nuarr-YYYYMMDD-HHMMSS), because they change on every run while this
bundle does not. Each dated folder is a complete restore point when paired
with this bundle.

RESTORING ONTO A WORKING MACHINE
--------------------------------
Use the Backup tab in nuarr: pick a backup, press Restore. nuarr takes a
safety copy of the current database first, so a restore can itself be undone.

RESTORING ONTO A BARE MACHINE
-----------------------------
1. Install Python 3.13 (or newer 3.x) and tick "Add python.exe to PATH".
2. Run install.cmd as Administrator. It copies the program to C:\\nuarr,
   installs the Python packages from wheels/ (no network needed), and puts
   ffmpeg back under C:\\ProgramData\\nuarr\\ffmpeg.
3. Run:  restore.cmd "path\\to\\nuarr-YYYYMMDD-HHMMSS"
4. Create a scheduled task named 'nuarr' that runs:
       python C:\\nuarr\\launch.py
   set to run whether the user is logged on or not, with highest privileges.
5. Browse to http://127.0.0.1:8770

WHAT WILL NOT COME BACK
-----------------------
* Your media. This backs up nuarr, not the library.
* The transcode cache (E:\\nuarr-cache) - scratch space, safe to lose.
* Sonarr/Radarr themselves. nuarr stores their URLs and API keys in the
  database, so once those services are running again nuarr will reconnect,
  but the arrs need their own backups.

VERIFYING A BACKUP YOURSELF
---------------------------
    python -c "import sqlite3;print(sqlite3.connect(r'nuarr.db').execute('PRAGMA integrity_check').fetchone()[0])"

It should print: ok
"""
