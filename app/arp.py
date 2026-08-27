r"""Keep the Programs and Features entry telling the truth.

WHY THIS RUNS AT EVERY BOOT AND NOT JUST AT INSTALL. The wizard writes the
Uninstall registry entry once, with the version it shipped. Then the in-app
updater replaces the program - and nothing ever told Windows: the entry kept
saying 1.0.3 while nuarr ran 1.0.4, and on one machine the uninstall itself
failed with "it may have already been uninstalled", which is Windows' way of
saying the command it was given no longer works. An entry that decays with
every update is worse than none - it teaches the owner that the uninstaller
is broken exactly once, at the moment they need it.

So nuarr maintains its own entry. At boot (and after every self-update, which
is a boot) it rewrites the uninstaller wrapper, refreshes the version and the
size, and re-creates anything missing. The bundle ships Nuarr-Uninstall.ps1
inside the program tree from 1.0.6 on, so an update refreshes the uninstall
logic itself too - the entry can only get truer over time, never staler.

Registry writes need admin; nuarr's scheduled task runs as SYSTEM, so this
works on any installed copy. A source checkout run by hand without rights
logs one line and moves on - it has no ARP entry to maintain anyway.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import joblog, version
from .config import DATA_DIR, ROOT

_RK = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\nuarr"


def _write_cmd(target: Path) -> Path:
    """The wrapper Programs and Features runs. Regenerated, never trusted.

    It copies the ps1 to TEMP and STARTS it detached before exiting, because a
    batch file is read incrementally and one that stayed alive inside the
    install folder would hold it open against its own deletion.
    """
    cmd = (
        "@echo off\r\n"
        "REM  nuarr uninstaller. Removes the program, task, shortcut and cache;\r\n"
        f"REM  KEEPS the database at {DATA_DIR} unless you pass -RemoveData.\r\n"
        "copy /Y \"%~dp0Nuarr-Uninstall.ps1\" \"%TEMP%\\Nuarr-Uninstall.ps1\" >nul\r\n"
        "start \"\" powershell -NoProfile -ExecutionPolicy Bypass -File "
        f"\"%TEMP%\\Nuarr-Uninstall.ps1\" -Target \"{target}\" "
        f"-DataDir \"{DATA_DIR}\" %*\r\n"
    )
    p = target / "Uninstall-nuarr.cmd"
    p.write_text(cmd, encoding="ascii")
    return p


def _dir_kb(*roots: Path) -> int:
    total = 0
    for r in roots:
        try:
            for f in r.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return int(total / 1024)


def ensure() -> None:
    """Bring the ARP entry up to date with what is actually installed."""
    if os.name != "nt":
        return
    target = Path(ROOT)
    ps1 = target / "Nuarr-Uninstall.ps1"
    if not ps1.is_file():
        # A source checkout, or an install too old to ship the uninstaller in
        # its program tree. Nothing to register a command for - and writing an
        # UninstallString that points at nothing is exactly the failure this
        # module exists to prevent.
        return
    try:
        import winreg
        cmd = _write_cmd(target)
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _RK, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
                                ) as k:
            def s(name: str, val: str) -> None:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)

            def d(name: str, val: int) -> None:
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, val)

            s("DisplayName", "nuarr")
            s("DisplayVersion", version.VERSION)
            s("Publisher", "NuGundam")
            s("InstallLocation", str(target))
            s("DisplayIcon", str(target / "assets" / "Nuarr.ico"))
            s("UninstallString", f'"{cmd}"')
            s("URLInfoAbout", "https://github.com/NuGundam/nuarr")
            d("NoModify", 1)
            d("NoRepair", 1)
            kb = _dir_kb(target, Path(DATA_DIR))
            if kb:
                d("EstimatedSize", kb)
        joblog.log(f"Programs and Features entry refreshed - "
                   f"nuarr {version.VERSION}", "info")
    except PermissionError:
        joblog.log("could not refresh the Programs and Features entry - "
                   "no admin rights (a source run; installed copies run "
                   "as SYSTEM and can)", "info")
    except Exception as e:                                   # noqa: BLE001
        joblog.log(f"Programs and Features refresh failed: "
                   f"{type(e).__name__}: {e}", "warn")
