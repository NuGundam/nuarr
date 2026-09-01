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

import sys

import os
import subprocess
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

# CREATE_NO_WINDOW IS NOT ALWAYS ENOUGH ON ITS OWN, which is why the flicker
# came back the moment the subtitle OCR workers were turned up. Measured rather
# than assumed: sampling every running process at 4 Hz while OCR ran caught five
# nvidia-smi launches, each with a conhost.exe of its own as its child - one
# console window apiece. The flag says "do not give this process a console";
# some console-subsystem binaries allocate one anyway, and conhost is the proof.
#
# STARTF_USESHOWWINDOW with SW_HIDE closes that gap: it tells Windows how to
# show the window if one is created at all, so a console that gets allocated is
# never mapped to the screen. subocr passes both and has always been silent;
# the GPU poll passed only the flag and flashed. The pair belongs together, so
# it lives here as one call rather than as two things to remember separately.
def hidden_si():
    """STARTUPINFO that hides a window even if one gets created."""
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                       # SW_HIDE
    return si


def quiet_run(cmd, **kw):
    """subprocess.run with both halves of the console suppression applied."""
    kw.setdefault("creationflags", NO_WINDOW)
    if os.name == "nt":
        kw.setdefault("startupinfo", hidden_si())
    return subprocess.run(cmd, **kw)


# HIDING A CHILD DOES NOT HIDE ITS CHILDREN, and that gap is what put a
# console window on Erik's desktop. CREATE_NO_WINDOW and SW_HIDE apply to the
# process being started; a console-less child that then spawns a
# console-subsystem program gets Windows to allocate a BRAND NEW console for
# it, and that one is visible.
#
# Caught by nuarr's own console watcher: "where ccache", parented to a Nuarr.exe
# that was itself parented to Nuarr.exe. That is paddle - subprocess.check_output
# (['where','ccache']) in paddle/utils/cpp_extension/extension_utils.py - run
# from the short -c probe that asks whether paddleocr is installed. The probe was
# started hidden and correctly so; nothing was passed on to what IT started.
#
# The three worker SCRIPTS each patch Popen at the top of themselves. A -c
# string has no top to put it in, so it gets this. Kept here rather than inline
# so the four copies cannot drift apart.
CHILD_HIDE_PREAMBLE = (
    "import os as _o, subprocess as _s\n"
    "if _o.name=='nt':\n"
    "    _op=_s.Popen.__init__\n"
    "    def _hp(self,*a,**k):\n"
    "        k['creationflags']=(k.get('creationflags') or 0)|0x08000000\n"
    "        if not k.get('startupinfo'):\n"
    "            _i=_s.STARTUPINFO()\n"
    "            _i.dwFlags|=_s.STARTF_USESHOWWINDOW\n"
    "            _i.wShowWindow=0\n"
    "            k['startupinfo']=_i\n"
    "        _op(self,*a,**k)\n"
    "    _s.Popen.__init__=_hp\n"
)

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
    # Bare names, resolved via PATH - the LAST resort, not the plan. The
    # working resolution order lives in ffmpeg_update.paths(): the build nuarr
    # installed to ProgramData\nuarr\ffmpeg\bin wins whenever it exists, so
    # these only fire on a machine where that folder is missing. They used to
    # point at the dev box's Tdarr install, which put "C:\Tdarr\...\ffmpeg.exe
    # (missing)" in every fresh install's error log - a path from a machine
    # the reader has never seen, presented as if it were their problem.
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
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

    # NETWORK SHARES nuarr connects to as itself. The service runs as SYSTEM,
    # which cannot see drive letters mapped in anyone's login session, so
    # UNC access has to be authenticated BY the service with credentials it
    # keeps. Same file and same threat model as the arr keys above.
    # Entries: {server, username, password}.
    net_shares: list = field(default_factory=list)

    # UPDATES. "owner/name" only - not a URL, because the API host and the
    # browse host differ and deriving both from one field is less to get wrong
    # than storing two and letting them disagree. Empty means nuarr never asks
    # GitHub anything, which is the state a fresh install ships in.
    update_repo: str = ""
    # Checking is automatic; INSTALLING IS ALWAYS A CLICK. Kept as a setting
    # anyway so the panel has something honest to show for "how do updates
    # work here", rather than the answer living only in a comment.
    update_auto_check: bool = True
    # "manual": nuarr tells you an update exists and waits.
    # "auto":   nuarr also downloads and VERIFIES it during a quiet stretch
    #           (no jobs, no viewers, for ten unbroken minutes), so Install
    #           is instant when you choose it. Installing is a click in both
    #           modes - the swap restarts nuarr, and when that happens is
    #           never the software's call.
    update_mode: str = "manual"

    # DO NUARR AND THE ARRS STILL AGREE - how the answer gets acted on.
    # "manual": the drift is found and listed, and Put right waits for you.
    #           The Attention tile carries the count, because a list nobody
    #           is told about is a list nobody reads.
    # "auto":   the same corrections are applied as they are found, and the
    #           tile stays quiet about them - being told about work that is
    #           already being done is how a tile stops being believed.
    # Either way, anything nuarr TRIED and could not fix raises attention:
    # that is the case where the mode is irrelevant, because no amount of
    # waiting will clear it.
    arrsync_mode: str = "manual"
    arrsync_every_h: int = 12            # hours between agreement checks

    # DOES THE AUDIO PICKER TELL THE TRUTH - the same bargain as above, for the
    # same reason. A title that names a codec the file does not have is drift
    # like any other, it is found by a check nobody remembers to run, and the
    # question of whether to fix it automatically is the user's, not nuarr's.
    audiotitle_mode: str = "manual"
    audiotitle_every_h: int = 24         # hours between title checks

    # FILES THE ARRS MANAGE THAT NUARR HAS NEVER WALKED - the third of the same
    # bargain, and the one where "auto" is least alarming: indexing a file the
    # arrs already track changes nothing on disk, it only stops nuarr being
    # ignorant of it. It still defaults to manual, because the point of these
    # three switches is that the choice is the user's and a safe-looking
    # default is still a default somebody did not pick.
    #
    # Six hours rather than twelve: this looks for files that arrived MINUTES
    # ago, so a long interval would mostly measure how long ago the last scan
    # ran. It costs two list calls and no disk walk.
    arrgap_mode: str = "manual"
    arrgap_every_h: int = 6              # hours between not-walked checks

    # OBSERVER MODE - whether this machine may CHANGE files, or only watch.
    # "auto" observes when the library lives on another machine that is itself
    # running nuarr, and processes normally everywhere else; "observe" and
    # "full" force it either way. Full on another nuarr's pool is the sandbox's
    # real-mode test switch, and the UI says loudly when it is on - two
    # machines processing one library is safe only while somebody watches.
    observer_mode: str = "auto"

    # SUBTITLE OCR - the switches behind Settings -> Subtitle OCR. Defaults
    # match the measured thresholds in subocr.py; the page can move them.
    subocr_auto: bool = True             # queue conversions on a schedule
    subocr_every_h: int = 6              # hours between sweeps
    # 0 = HAND THE WHOLE BACKLOG TO THE QUEUE, which is the default now. The
    # old 20-per-sweep throttled the wrong stage: the queue already paces this
    # with its worker count, the gate stops it when someone is watching, and
    # disk_wait_pct holds it off the spindles - none of which care how many
    # items are waiting. Twenty every six hours is eighty a day against a
    # backlog that grows whenever new files land, so it could lose ground while
    # every real throttle sat idle. Set a number here to cap it anyway.
    subocr_batch: int = 0                # 0 = every eligible file
    # Per-library overrides, keyed by library name; anything absent follows
    # the defaults above. See subocr._s().
    subocr_libs: dict = field(default_factory=dict)
    # Image subs whose text version now exists: demote them (default, and
    # reversible) or drop them from the file entirely.
    subocr_remove_image: bool = False
    # Which OCR engine reads the pictures: "tesseract" (bundled, fast on any
    # CPU) or "paddle" (more accurate on italics, wants a GPU).
    # HOW MANY MAY BE ON THE CARD AT ONCE, which is a different question from
    # how many jobs may be in flight. A subtitle OCR job is three phases -
    # demux the picture track off the pool, read it on the GPU, mux the text
    # back - and only the middle one touches the card. Capping the whole job
    # at "what the GPU can take" meant the extra workers queued behind the
    # card doing nothing, while the disk sat idle through every OCR pass.
    #
    # So the JOB cap (subocr_workers) governs how many files are being worked
    # on, and this governs how many of those may be on the GPU at any moment.
    # Raise subocr_workers to fill the disk; leave this near what the card
    # actually saturates at.
    subocr_gpu_lanes: int = 2
    subocr_engine: str = "tesseract"
    subocr_sdh: bool = True              # convert SDH image subs too
    subocr_all: bool = False             # override: OCR every kept PGS track
    subocr_signs_unburned: bool = True   # convert signs when nothing burns them
    subocr_signs_max_cpm: float = 6.0    # below this density = signs
    subocr_dialogue_min_cues: int = 500  # above this = dialogue, no debate

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
    # EMPTY, deliberately. This used to return one specific person's six
    # P:\ folders - reasonable when this file WAS that person's config, wrong
    # from the moment the code ran anywhere else: a fresh install with an
    # empty library list booted up showing somebody else's shelves, all
    # marked (missing). No libraries is a true statement about a new machine;
    # the UI has a Libraries page for adding real ones.
    return []


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
        except Exception as e:                           # noqa: BLE001
            # LOUD, not silent. This swallow used to be wordless, and the cost
            # was a debugging session that started three symptoms downstream:
            # the installer once wrote invalid YAML for an empty library list,
            # the parse failed HERE, raw stayed {}, every setting fell back to
            # its dev-box default, and the process then died creating a cache
            # directory on a drive letter that machine did not have. The
            # traceback pointed at mkdir; the actual fault was on this line,
            # invisible. A config file that exists but does not apply must
            # say so somewhere a person will look.
            print(f"nuarr: config.yml exists but could not be parsed - "
                  f"IGNORING IT and using defaults. {type(e).__name__}: {e}",
                  file=sys.stderr)
            raw = {}

    for k, v in raw.items():
        if k in ("arrs", "libraries"):
            continue
        if hasattr(s, k):
            setattr(s, k, v)

    # AN EXPLICIT EMPTY LIST IS AN ANSWER, NOT AN ABSENCE. `raw.get("arrs")`
    # is falsy for [] as well as for a missing key, so a config that honestly
    # said `libraries: []` was overridden by the defaults - which at the time
    # were one particular machine's six library folders. Auto-detection is
    # for a config that never mentions the key; a key that says "none" means
    # none.
    # (`or []` because a bare `libraries:` key parses as None, and iterating
    # None would trade the old wrong answer for a crash.)
    s.arrs = ([ArrConfig(**a) for a in (raw["arrs"] or [])]
              if "arrs" in raw else _default_arrs())
    s.libraries = ([LibraryConfig(**l) for l in (raw["libraries"] or [])]
                   if "libraries" in raw else _default_libraries())
    if not s.tautulli_api_key:
        s.tautulli_api_key = _read_tautulli_key()
    if not (s.plex_url and s.plex_token):
        u, t = _read_plex_conn()
        s.plex_url = s.plex_url or u
        s.plex_token = s.plex_token or t
    # A CACHE THAT CANNOT BE CREATED IS A WARNING, NOT A DEATH SENTENCE.
    # This mkdir used to be bare, and it is the line that decided whether the
    # whole application booted: a cache_dir pointing at a drive letter the
    # machine does not have (which is exactly what a default from another
    # machine looks like) threw FileNotFoundError during import, before the
    # web server, before the logs page, before anything that could have told
    # the user what to fix. The settings page already knows how to say "that
    # folder does not exist yet" - it just needs the process alive to say it.
    try:
        Path(s.cache_dir).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"nuarr: cache_dir {s.cache_dir!r} cannot be created ({e}) - "
              f"starting anyway; set a working cache folder in Settings",
              file=sys.stderr)
    return s


SETTINGS = load_settings()
