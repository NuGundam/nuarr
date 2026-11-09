r"""
make-nuarr-exe.py - build Nuarr.exe, the launcher the scheduled task runs

WHAT Nuarr.exe IS
-----------------
It is Python's own pythonw.exe - the windowless interpreter, a 100 KB shim
that loads python3xx.dll and calls Py_Main - under nuarr's name and icon, so
Task Manager, Resource Monitor and the firewall say "Nuarr" instead of one more
anonymous "Python". Nothing about how it runs changes:

    Nuarr.exe C:\nuarr\launch.py

It has to sit beside pythonw.exe, because that is where python3xx.dll is.
app/lifecycle.py prefers it over pythonw.exe when it restarts the server.

WHY THIS SCRIPT EXISTS
----------------------
The first Nuarr.exe was made by hand and its recipe was never written down.
It had its icon swapped and every other resource STRIPPED: no version block,
no description, no company, no signature. On 2026-11-08 Defender's
machine-learning model called exactly that - a tiny, unsigned, anonymous
launcher - Trojan:Win32/Bearfoos.A!ml, killed nuarr, quarantined the file and
deleted the scheduled task with it. This build does the opposite of
stripping: the result says plainly what it is.

WHAT IT DOES
------------
  1. copies pythonw.exe (never touches the original);
  2. removes the Python Software Foundation's Authenticode signature, which
     stops being valid the moment a resource changes - a broken signature is
     worse than none;
  3. replaces the icon with assets\Nuarr.ico;
  4. replaces the version block: "Nuarr" as product and description, nuarr's
     version from app\version.py, and an honest note that this is CPython's
     pythonw launcher (the PSF copyright line is kept);
  5. fixes the PE checksum;
  6. optionally signs it (--sign) with a code-signing certificate from the
     current user's store, creating a self-signed "Nuarr (self-signed)" one
     the first time. A self-signed signature only counts as VALID on a
     machine where that certificate has been put in Trusted Root and Trusted
     Publishers - that is a decision for the owner, so this script never does
     it; it prints the two commands instead.

Usage
-----
    python tools\make-nuarr-exe.py                      # -> tools\build\Nuarr.exe
    python tools\make-nuarr-exe.py --sign
    python tools\make-nuarr-exe.py --sign --install     # also copy beside pythonw.exe

--install refuses while a Nuarr.exe process is running (Windows locks the
file); stop nuarr first, or install and let the next restart pick it up via
--install-as Nuarr.new.exe and a rename.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import struct
import subprocess
import sys
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON = os.path.join(ROOT, "assets", "Nuarr.ico")
OUT_DIR = os.path.join(ROOT, "tools", "build")
CERT_SUBJECT = "CN=Nuarr (self-signed)"

RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16
LANG_EN_US = 0x0409
LOAD_LIBRARY_AS_DATAFILE = 0x2

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
imagehlp = ctypes.WinDLL("imagehlp", use_last_error=True)

k32.LoadLibraryExW.restype = wintypes.HMODULE
k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
k32.FreeLibrary.argtypes = [wintypes.HMODULE]
k32.BeginUpdateResourceW.restype = wintypes.HANDLE
k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
k32.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
ENUMNAME = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p)
ENUMLANG = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p)
k32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ENUMNAME, ctypes.c_void_p]
k32.EnumResourceLanguagesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                       ENUMLANG, ctypes.c_void_p]


def _nuarr_version() -> str:
    try:
        src = open(os.path.join(ROOT, "app", "version.py"), encoding="utf-8").read()
        m = re.search(r'^VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src, re.M)
        if m:
            return ".".join(m.groups())
    except OSError:
        pass
    return "0.0.0"


# --------------------------------------------------------------- signature

def strip_signature(path: str) -> bool:
    """Drop the Authenticode certificate table. It lives at the very end of
    the file, pointed at by data directory 4, so: zero the pointer, cut the
    tail. Returns True when there was one."""
    with open(path, "r+b") as f:
        b = f.read()
        pe = struct.unpack_from("<I", b, 0x3C)[0]
        magic = struct.unpack_from("<H", b, pe + 24)[0]
        dirs = pe + 24 + (112 if magic == 0x20B else 96)
        off, size = struct.unpack_from("<II", b, dirs + 8 * 4)
        if not off or not size:
            return False
        f.seek(dirs + 8 * 4)
        f.write(b"\0" * 8)
        if off + size >= len(b) - 8:            # the usual case: it is the tail
            f.truncate(off)
    return True


def fix_checksum(path: str) -> None:
    old, new = wintypes.DWORD(0), wintypes.DWORD(0)
    if imagehlp.MapFileAndCheckSumW(path, ctypes.byref(old), ctypes.byref(new)) != 0:
        return
    with open(path, "r+b") as f:
        b = f.read(0x40)
        pe = struct.unpack_from("<I", b, 0x3C)[0]
        f.seek(pe + 24 + 64)
        f.write(struct.pack("<I", new.value))


# --------------------------------------------------------------- resources

def _res_id(p):
    """A resource name is either a small integer or a pointer to a string."""
    v = p or 0
    return v if v >> 16 == 0 else ctypes.wstring_at(v)


def existing(path: str, rtype: int) -> list:
    """Every (name, language) of one resource type in the file."""
    h = k32.LoadLibraryExW(path, None, LOAD_LIBRARY_AS_DATAFILE)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    found: list = []
    try:
        names: list = []
        k32.EnumResourceNamesW(h, rtype, ENUMNAME(lambda m, t, n, l: names.append(_res_id(n)) or True), None)
        for n in names:
            langs: list = []
            arg = n if isinstance(n, int) else ctypes.c_wchar_p(n)
            k32.EnumResourceLanguagesW(h, rtype, arg,
                                       ENUMLANG(lambda m, t, nn, lang, l: langs.append(lang) or True), None)
            found += [(n, lang) for lang in langs]
    finally:
        k32.FreeLibrary(h)
    return found


def _name_arg(n):
    return n if isinstance(n, int) else ctypes.c_wchar_p(n)


def icon_resources(ico_path: str):
    """Split a .ico into the RT_ICON images and the RT_GROUP_ICON directory
    that points at them by id (1..n)."""
    b = open(ico_path, "rb").read()
    _, kind, count = struct.unpack_from("<HHH", b, 0)
    if kind != 1 or not count:
        raise ValueError(f"{ico_path} is not an icon file")
    images, group = [], struct.pack("<HHH", 0, 1, count)
    for i in range(count):
        w, h, colors, res, planes, bpp, size, off = struct.unpack_from("<BBBBHHII", b, 6 + 16 * i)
        images.append(b[off:off + size])
        group += struct.pack("<BBBBHHIH", w, h, colors, res, planes, bpp, size, i + 1)
    return images, group


def _node(key: str, value: bytes, vtype: int, children: bytes = b"", vlen: int | None = None) -> bytes:
    """One block of a version resource: header, key, padded value, children."""
    head = (key + "\0").encode("utf-16-le")
    body = b"\0" * ((-(6 + len(head))) % 4) + value
    body += b"\0" * ((-(6 + len(head) + len(body))) % 4) + children
    length = 6 + len(head) + len(body)
    return struct.pack("<HHH", length, len(value) if vlen is None else vlen, vtype) + head + body


def _pad(b: bytes) -> bytes:
    return b + b"\0" * ((-len(b)) % 4)


def version_resource(version: str, py_version: str) -> bytes:
    a, b_, c = (int(x) for x in version.split("."))
    ms, ls = (a << 16) | b_, (c << 16)
    fixed = struct.pack("<IIIIIIIIIIIII", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls,
                        0x3F, 0, 0x00040004, 1, 0, 0, 0)     # VOS_NT_WINDOWS32, VFT_APP
    strings = {
        "CompanyName": "Nuarr",
        "FileDescription": "Nuarr",
        "FileVersion": version,
        "InternalName": "Nuarr",
        "OriginalFilename": "Nuarr.exe",
        "ProductName": "Nuarr",
        "ProductVersion": version,
        "Comments": (f"nuarr's server process. This is CPython {py_version}'s pythonw.exe launcher "
                     "with nuarr's name and icon; it runs C:\\nuarr\\launch.py. "
                     "Built by tools\\make-nuarr-exe.py."),
        "LegalCopyright": ("Launcher: Copyright (c) Python Software Foundation. "
                           "nuarr: its own license, see the nuarr repository."),
    }
    entries = b""
    for k, v in strings.items():
        val = (v + "\0").encode("utf-16-le")
        entries += _pad(_node(k, val, 1, vlen=len(v) + 1))
    table = _node("040904B0", b"", 1, entries)
    sfi = _node("StringFileInfo", b"", 1, table)
    var = _node("Translation", struct.pack("<HH", LANG_EN_US, 0x04B0), 0)
    vfi = _node("VarFileInfo", b"", 1, var)
    return _node("VS_VERSION_INFO", fixed, 0, _pad(sfi) + _pad(vfi))


def rebrand(path: str, version: str, py_version: str) -> None:
    old = {t: existing(path, t) for t in (RT_ICON, RT_GROUP_ICON, RT_VERSION)}
    images, group = icon_resources(ICON)
    ver = version_resource(version, py_version)
    h = k32.BeginUpdateResourceW(path, False)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    ok = True
    try:
        for t, items in old.items():                       # out with Python's
            for name, lang in items:
                ok &= bool(k32.UpdateResourceW(h, t, _name_arg(name), lang, None, 0))
        for i, img in enumerate(images, 1):                # in with nuarr's
            ok &= bool(k32.UpdateResourceW(h, RT_ICON, i, LANG_EN_US, img, len(img)))
        ok &= bool(k32.UpdateResourceW(h, RT_GROUP_ICON, 1, LANG_EN_US, group, len(group)))
        ok &= bool(k32.UpdateResourceW(h, RT_VERSION, 1, LANG_EN_US, ver, len(ver)))
    finally:
        if not k32.EndUpdateResourceW(h, not ok):
            raise ctypes.WinError(ctypes.get_last_error())
    if not ok:
        raise RuntimeError("a resource update was refused; the file was left unchanged")


# ------------------------------------------------------------------ signing

_SIGN_PS = r"""
$ErrorActionPreference = 'Stop'
$c = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq '%(subject)s' -and $_.NotAfter -gt (Get-Date) } | Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $c) {
  $c = New-SelfSignedCertificate -Type CodeSigningCert -Subject '%(subject)s' -CertStoreLocation Cert:\CurrentUser\My -KeyUsage DigitalSignature -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 -NotAfter (Get-Date).AddYears(10)
  'created certificate ' + $c.Thumbprint
}
$r = Set-AuthenticodeSignature -FilePath '%(path)s' -Certificate $c -HashAlgorithm SHA256
'signed: ' + $r.Status + ' | ' + $r.StatusMessage
'thumbprint: ' + $c.Thumbprint
"""


def sign(path: str) -> str:
    ps = _SIGN_PS % {"subject": CERT_SUBJECT, "path": path}
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError("signing failed: " + out[-400:])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Nuarr.exe from pythonw.exe")
    ap.add_argument("--python", default=os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
                    help="the pythonw.exe to build from (default: this interpreter's)")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "Nuarr.exe"))
    ap.add_argument("--sign", action="store_true", help="sign with the self-signed Nuarr certificate")
    ap.add_argument("--install", action="store_true", help="copy the result beside pythonw.exe")
    ap.add_argument("--install-as", default="Nuarr.exe", help="file name to install under")
    a = ap.parse_args()

    if not os.path.exists(a.python):
        print(f"no pythonw.exe at {a.python}")
        return 2
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    shutil.copyfile(a.python, a.out)
    version = _nuarr_version()
    py_version = ".".join(str(x) for x in sys.version_info[:3])

    print(f"from      : {a.python} ({os.path.getsize(a.python):,} bytes)")
    print(f"signature : {'PSF signature removed' if strip_signature(a.out) else 'was not signed'}")
    rebrand(a.out, version, py_version)
    fix_checksum(a.out)
    print(f"resources : Nuarr icon + version block {version} (CPython {py_version} launcher)")
    if a.sign:
        print(sign(a.out))
        print("to make that signature count as valid on this PC (your decision, run elevated):")
        print(r"  $c = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | ? Subject -eq '%s' | select -First 1" % CERT_SUBJECT)
        print(r"  Export-Certificate -Cert $c -FilePath $env:TEMP\nuarr-sign.cer | Out-Null")
        print(r"  Import-Certificate -FilePath $env:TEMP\nuarr-sign.cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null")
        print(r"  Import-Certificate -FilePath $env:TEMP\nuarr-sign.cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null")
    print(f"built     : {a.out} ({os.path.getsize(a.out):,} bytes)")
    if a.install:
        dest = os.path.join(os.path.dirname(a.python), a.install_as)
        try:
            shutil.copyfile(a.out, dest)
            print(f"installed : {dest}")
        except PermissionError:
            print(f"NOT installed: {dest} is in use (nuarr is running from it). "
                  "Stop nuarr first, or use --install-as Nuarr.new.exe.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
