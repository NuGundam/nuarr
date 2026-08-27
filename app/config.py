"""
nuarr - configuration

Runs NATIVELY on Windows, deliberately not in Docker. Measured on this box:
    pool read  native 105.8 MB/s   vs  Docker/WSL2 bind mount 21.7 MB/s
    ls 1 folder native instant     vs  Docker 4.2 s
The whole system is I/O bound on the pool, so the 5x penalty is disqualifying.
NVENC works either way; the filesystem is the reason we stay native.

Settings load from config.yml next to the project root, falling back to these
defaults. Nothing here is secret except the arr API keys, which are read from
the arr config.xml files directly so they are never duplicated.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NUARR_DATA", r"C:\ProgramData\nuarr"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --- console suppression ---------------------------------------------------
# CREATE_NO_WINDOW (0x08000000). EVERY child process nuarr spawns must pass
# this, or Windows gives it a console window of its own.
#
# Why this only started happening after the move to C:\nuarr: the server used
# to run under python.exe, which OWNS a console. A child console application
# inherits its parent's console and draws nothing. Under pythonw.exe there is
# no console to inherit, so Windows allocates a fresh one per child - and it
# is a visible top-level window that steals focus. nvidia-smi runs every few
# seconds for the system bar, which is why the flicker looked constant rather
# than tied to any job.
#
# DETACHED_PROCESS would also hide it, but it severs the pipes we read
# ffmpeg's -progress output from. CREATE_NO_WINDOW keeps stdout/stderr
# redirectable and only suppresses the window, which is exactly what we want.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DB_PATH = DATA_DIR / "nuarr.db"
LOG_PATH = DATA_DIR / "nuarr.log"


def _read_arr_key(config_xml: str) -> tuple[str | None, int | None]:
    """Pull ApiKey + Port straight from Sonarr/Radarr's config.xml.

    Avoids storing a second copy of the key, and means rotating it in the arr
    app is picked up automatically.
    """
    try:
        tree = ET.parse(config_xml)
        root = tree.getroot()
        key = root.findtext("ApiKey")
        port = root.findtext("Port")
        return key, int(port) if port else None
    except Exception:
        return None, None


def _tautulli_config_paths() -> list[str]:
    import glob as _glob
    return [p for p in ([
        r"C:\ProgramData\Tautulli\config.ini",
        r"C:\Tautulli\config.ini",
        os.path.expandvars(r"%APPDATA%\Tautulli\config.ini"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tautulli\config.ini"),
    ] + _glob.glob(r"C:\Program Files*\Tautulli\config.ini"))]


def _read_plex_conn() -> tuple[str | None, str | None]:
    r"""Plex's URL and token, taken from Tautulli's config.

    SAME REASONING AS THE TAUTULLI KEY: Tautulli already had to be told where
    Plex is and had to be given a token, and both are sitting in its config.
    Asking for them a second time creates a second copy of a secret that can
    drift out of sync with the first, and a token pasted into two places is one
    that gets rotated in one of them.

    Parsed line by line rather than with configparser. The keys live in [PMS],
    but the file also contains a nested "[[get_file_sizes_hold]]" section that
    configparser rejects outright even with strict=False on some versions - and
    a parser that throws takes the URL down with it.
    """
    for p in _tautulli_config_paths():
        try:
            if not os.path.isfile(p):
                continue
            vals: dict[str, str] = {}
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "=" not in line or line.lstrip().startswith(("#", ";", "[")):
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k in ("pms_url", "pms_ip", "pms_port", "pms_token",
                             "pms_ssl"):
                        vals[k] = v.strip().strip('"')
            token = vals.get("pms_token") or None
            url = vals.get("pms_url") or None
            if not url and vals.get("pms_ip"):
                scheme = "https" if vals.get("pms_ssl") == "1" else "http"
                url = f"{scheme}://{vals['pms_ip']}:{vals.get('pms_port', '32400')}"
            if url and token:
                return url, token
        except Exception:
            continue
    return None, None


def _read_tautulli_key() -> str | None:
    """Find Tautulli's API key without asking for it twice.

    Order matters:
      1. Tautulli's own config.ini, if we can find it - the real source.
      2. Tautulli-TdarrPause.ps1, the script nuarr is replacing. Migrating the
         value out of the old script is the point of consolidating, and it
         avoids a second copy of the same secret drifting out of sync.
    """
    import configparser
    import glob as _glob

    candidates = [
        r"C:\ProgramData\Tautulli\config.ini",
        r"C:\Tautulli\config.ini",
        os.path.expandvars(r"%APPDATA%\Tautulli\config.ini"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tautulli\config.ini"),
    ]
    candidates += _glob.glob(r"C:\Program Files*\Tautulli\config.ini")
    for p in candidates:
        try:
            if not os.path.isfile(p):
                continue
            cp = configparser.ConfigParser(strict=False)
            cp.read(p, encoding="utf-8")
            for sect in cp.sections():
                if cp.has_option(sect, "api_key"):
                    v = cp.get(sect, "api_key").strip().strip('"')
                    if v:
                        return v
        except Exception:
            continue

    ps = ROOT.parent / "Tautulli-TdarrPause.ps1"
    try:
        import re as _re
        m = _re.search(r'\$TautulliApiKey\s*=\s*"([0-9a-fA-F]{16,})"',
                       ps.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


@dataclass
class ArrConfig:
    name: str
    kind: str                      # "sonarr" | "radarr"
    url: str
    api_key: str | None
    enabled: bool = True

    @property
    def api(self) -> str:
        return self.url.rstrip("/") + "/api/v3"


@dataclass
class LibraryConfig:
    name: str
    path: str
    kind: str                      # "tv" | "movie"
    enabled: bool = True


@dataclass
class Settings:
    # --- media tooling -----------------------------------------------------
    ffmpeg: str = r"C:\Tdarr\Tdarr_Node\assets\app\ffmpeg\win32_x64\ffmpeg.exe"
    ffprobe: str = r"C:\Tdarr\Tdarr_Node\assets\app\ffmpeg\win32_x64\ffprobe.exe"
    mkvmerge: str = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
    mkvpropedit: str = r"C:\Program Files\MKVToolNix\mkvpropedit.exe"
    python: str = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"

    # transcode scratch space - NVMe, deliberately OFF the pool so a working
    # file is never subject to DrivePool balancing mid-job (that race produced
    # 104 failed encodes in one day under the old setup)
    cache_dir: str = r"E:\nuarr-cache"
    cache_min_free_gb: int = 100

    # --- workers -----------------------------------------------------------
    # The A5000 has ONE NVENC engine; measured saturation is ~4 concurrent
    # 1080p encodes. Pass-through/remux jobs do not touch NVENC, so they get a
    # separate pool and can run wider.
    encode_workers: int = 4
    passthrough_workers: int = 4
    probe_workers: int = 4

    # Where the PowerShell handler scripts live. Empty = look inside the
    # install (<root>\scripts), then fall back to the folder beside it, which
    # is where they sat when nuarr lived in the Claude directory.
    scripts_dir: str = ""

    # --- ffmpeg ------------------------------------------------------------
    # Prefer nuarr's own build when one is installed. SETTINGS.ffmpeg stays as
    # the FALLBACK (Tdarr's bundle) so a fresh install still works before the
    # first update is fetched - but once nuarr has its own copy under
    # ProgramData, everything must use it. Reading SETTINGS.ffmpeg directly is
    # what made the first install cosmetic: the binary was downloaded,
    # verified, installed and then ignored by every job.
    # --- exclusions --------------------------------------------------------
    # Paths nuarr must never touch, matched as a prefix and case-insensitively.
    # These are not necessarily outside the pool: an arr can have a root folder
    # here, in which case its files arrive through the API with no mtime and no
    # pool disk, and would sit in "Held" forever because mark_eligible() needs
    # an mtime. Excluding them is cleaner than repeatedly explaining them.
    exclude_paths: list[str] = field(default_factory=lambda: [
        r"P:\BackUp Data\Files\Video Files",
    ])

    # --- safety / coordination --------------------------------------------
    # New or changed files are not eligible until they have been quiet this
    # long. Lets Sonarr finish importing, upgrading, renaming and the OCR hook
    # finish before we spend GPU time on a file that is about to change.
    hold_minutes: int = 5                        # adjustable in the UI (Holds & timing)
    # MAX_PATH is 260 INCLUDING the terminating null, so 259 characters are
    # usable. 255 was over-cautious and produced a false positive: a 256-char
    # rename Sonarr could have performed was reported as "too long" and skipped.
    # The margin belongs on OUR commit path, not on the arr's rename - see
    # commit_path_margin below.
    max_path_length: int = 259
    # nuarr's own commit writes '.nuarr-new' and '.nuarr-bak' siblings, which
    # add ~10 characters to the longest name it touches. Reserve that here so
    # a file we can rename is never one we then fail to commit.
    commit_path_margin: int = 12
    pause_during_arr_rename: bool = True
    pause_during_pool_balance: bool = True

    # UPDATES. "owner/name" only - not a URL, because the API host and the
    # browse host differ and deriving both from one field is less to get wrong
    # than storing two and letting them disagree. Empty means nuarr never asks
    # GitHub anything, which is the state a fresh install ships in.
    update_repo: str = ""
    # Checking is automatic; INSTALLING IS ALWAYS A CLICK. Kept as a setting
    # anyway so the panel has something honest to show for "how do updates
    # work here", rather than the answer living only in a comment.
    update_auto_check: bool = True

    # Tautulli - used to hold jobs while Plex is busy. Key is read from
    # Tautulli's own config so it is not duplicated here.
    tautulli_url: str = "http://localhost:8181"
    tautulli_api_key: str | None = None

    # PLEX, DIRECTLY. Tautulli is a live passthrough to Plex for get_activity -
    # measured, it is not serving a cache - but it costs 2,177 ms per call
    # against 159 ms straight to Plex, and it is a second process that can die
    # independently of the thing we actually care about. When it does, the gate
    # goes blind and fails open, which silently removes viewer protection.
    #
    # Every field the gate uses (state, transcode decision, throttled, speed,
    # transcode progress, view progress, user, file path) is in Plex's own
    # /status/sessions response. Tautulli's extra 70-odd fields are history and
    # user metadata that nothing here reads.
    #
    # Tautulli stays as the FALLBACK, not as dead code: if Plex is unreachable
    # or the token is missing, the old path still answers.
    plex_url: str | None = None
    plex_token: str | None = None
    plex_direct: bool = True
    # Ask both and log any field they disagree on. Costs the Tautulli call, so
    # it is for proving the change, not for leaving on.
    plex_cross_check: bool = False

    # Files below this that the arrs do not know about are treated as EXTRAS,
    # not problems. Checked against the real library: the small "orphans" are
    # OP/ED sequences, AMVs, promos, bonus features and specials - content worth
    # keeping. Counting them as unmanaged junk buried the ~460 large orphans
    # that actually indicate failed imports.
    min_orphan_size_mb: int = 100

    # Run the repair/OCR handlers on each file as part of its job, before the
    # transcode. Off = they only run when queued by hand.

    # Use the Windows \\?\ extended-length form for paths past max_path_length
    # instead of refusing them. Lets the ~2,243 over-length files be processed.
    allow_long_paths: bool = True

    # --- scanning ----------------------------------------------------------
    scan_interval_minutes: int = 15

    # --- web ---------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8770

    arrs: list[ArrConfig] = field(default_factory=list)
    libraries: list[LibraryConfig] = field(default_factory=list)


def _default_arrs() -> list[ArrConfig]:
    out: list[ArrConfig] = []
    son_key, son_port = _read_arr_key(r"C:\ProgramData\Sonarr\config.xml")
    if son_key:
        out.append(ArrConfig("Sonarr", "sonarr", f"http://localhost:{son_port or 8989}", son_key))
    rad_key, rad_port = _read_arr_key(r"C:\ProgramData\Radarr\config.xml")
    if rad_key:
        out.append(ArrConfig("Radarr", "radarr", f"http://localhost:{rad_port or 7878}", rad_key))
    return out


def _default_libraries() -> list[LibraryConfig]:
    return [
        LibraryConfig("Anime Shows", r"P:\Anime Shows", "tv"),
        LibraryConfig("Animated Shows", r"P:\Animated Shows", "tv"),
        LibraryConfig("TV Shows", r"P:\TV Shows", "tv"),
        LibraryConfig("Anime Movies", r"P:\Anime Movies", "movie"),
        LibraryConfig("Animated Movies", r"P:\Animated Movies", "movie"),
        LibraryConfig("Movies", r"P:\Movies", "movie"),
    ]


def load_settings() -> Settings:
    s = Settings()
    cfg_file = ROOT / "config.yml"
    raw = {}
    if cfg_file.exists():
        try:
            # utf-8-sig, not utf-8. Any editor on Windows - Notepad, VS Code's
            # default, PowerShell's Set-Content -Encoding UTF8 - writes a BOM,
            # and yaml then either fails outright or hands back a first key
            # with an invisible ﻿ glued to the front. The except below
            # swallows that into raw={}, so the whole file silently stops
            # applying and the only symptom is a setting that will not take.
            # Cost an hour: plex_cross_check read False no matter what the file
            # said.
            raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            raw = {}

    for k, v in raw.items():
        if k in ("arrs", "libraries"):
            continue
        if hasattr(s, k):
            setattr(s, k, v)

    s.arrs = [ArrConfig(**a) for a in raw["arrs"]] if raw.get("arrs") else _default_arrs()
    s.libraries = (
        [LibraryConfig(**l) for l in raw["libraries"]] if raw.get("libraries") else _default_libraries()
    )
    if not s.tautulli_api_key:
        s.tautulli_api_key = _read_tautulli_key()
    if not (s.plex_url and s.plex_token):
        u, t = _read_plex_conn()
        s.plex_url = s.plex_url or u
        s.plex_token = s.plex_token or t
    Path(s.cache_dir).mkdir(parents=True, exist_ok=True)
    return s


SETTINGS = load_settings()
