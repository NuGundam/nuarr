r"""Which video encoders this machine can ACTUALLY use, and how to drive each.

TWO SEPARATE QUESTIONS, AND ONLY ONE OF THEM IS EASY.

`ffmpeg -encoders` lists what the BUILD supports, which on a Jellyfin build is
everything: NVENC, QuickSync, AMF and the CPU encoders are all compiled in on a
box with a single NVIDIA card and no Intel iGPU at all. Asking ffmpeg what it
knows about therefore answers the wrong question. The only honest test is to
hand each family two seconds of colour bars and see whether it produces a file.

The second question is how to DRIVE each one, and they share almost nothing:

    family   quality flag         preset scale        10-bit pixel format
    ------   ------------------   -----------------   -------------------
    nvenc    -rc vbr -cq N        p1..p7              p010le
    qsv      -global_quality N    veryfast..veryslow  p010le
    amf      -rc cqp -qp_i/-qp_p  speed/balanced/..   p010le
    cpu      -crf N               ultrafast..veryslow yuv420p10le

A CQ of 22 does not mean the same thing to libx265 as it does to NVENC either,
so the number is carried through unchanged rather than "translated" - the
setting is per library and per family, and the person tuning it can see what
they set.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

from .config import NO_WINDOW, hidden_si

# ---------------------------------------------------------------- families --

# preset ladders, fastest first, as each family spells them
NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]
CPU_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
               "medium", "slow", "slower", "veryslow"]
QSV_PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
AMF_PRESETS = ["speed", "balanced", "quality"]

FAMILIES: dict[str, dict] = {
    "nvenc": {
        "label": "NVIDIA NVENC",
        "hevc": "hevc_nvenc", "h264": "h264_nvenc", "av1": "av1_nvenc",
        "presets": NVENC_PRESETS, "default_preset": "p5",
        "hwaccel": "cuda",
        "pix10": "p010le",
        "note": "The GeForce/Quadro encoder block. Does not touch the CUDA "
                "cores, so an encode and a game or a Whisper listen can share "
                "the card.",
    },
    "qsv": {
        "label": "Intel QuickSync",
        "hevc": "hevc_qsv", "h264": "h264_qsv", "av1": "av1_qsv",
        "presets": QSV_PRESETS, "default_preset": "medium",
        "hwaccel": "qsv",
        "pix10": "p010le",
        "note": "The encoder built into Intel iGPUs and Arc cards. Very "
                "efficient per watt; needs an Intel GPU present and its "
                "driver installed.",
    },
    "amf": {
        "label": "AMD AMF",
        "hevc": "hevc_amf", "h264": "h264_amf", "av1": "av1_amf",
        "presets": AMF_PRESETS, "default_preset": "balanced",
        "hwaccel": "d3d11va",
        "pix10": "p010le",
        "note": "The encoder on Radeon cards. Needs an AMD GPU and the "
                "Adrenalin driver.",
    },
    "cpu": {
        "label": "CPU (x264 / x265)",
        "hevc": "libx265", "h264": "libx264", "av1": "libsvtav1",
        "presets": CPU_PRESETS, "default_preset": "medium",
        "hwaccel": "",
        "pix10": "yuv420p10le",
        "note": "Software encoding. Much slower and it will use every core, "
                "but it is the most efficient per bit and it works on any "
                "machine with no special hardware at all.",
    },
}

ORDER = ["nvenc", "qsv", "amf", "cpu"]      # preference when falling back


def family_of(encoder: str) -> str:
    """'hevc_nvenc' -> 'nvenc'. Empty for anything unrecognised."""
    e = (encoder or "").lower()
    for fam, spec in FAMILIES.items():
        if e in (spec["hevc"], spec["h264"], spec.get("av1")):
            return fam
    return ""


def encoder_for(family: str, target: str) -> str:
    """The encoder name a family uses for a target codec ('hevc'/'h264'/'av1')."""
    spec = FAMILIES.get(family) or FAMILIES["cpu"]
    return spec.get(target) or spec["hevc"]


# ------------------------------------------------------------- the probe ----

_CACHE: dict = {"at": 0.0, "result": {}, "ttl": 3600.0}
_TTL = 3600.0
# How long a "nothing works here" answer is trusted. Short, because that
# answer sends every job to the CPU and is the one most likely to be wrong -
# see probe().
_FAIL_TTL = 60.0


def _ff() -> str:
    from . import jobs
    try:
        return jobs._ffmpeg_exe()
    except Exception:                                    # noqa: BLE001
        return shutil.which("ffmpeg") or "ffmpeg"


def _try_family(fam: str, timeout: float = 25.0, attempts: int = 2) -> dict:
    r"""Encode two seconds of colour bars. Anything else is a guess.

    Uses lavfi rather than a real file so the test costs nothing and cannot be
    thrown off by a weird source. Writes to a temp file and deletes it - some
    encoders will happily "succeed" writing to NUL without initialising the
    hardware at all, which is exactly the false pass this is here to avoid.
    """
    spec = FAMILIES[fam]
    enc = spec["hevc"]
    t0 = time.time()
    last = {"ok": False, "detail": "failed", "seconds": 0.0}
    for attempt in range(max(1, attempts)):
        # A NAME PER RUN, NOT PER FAMILY. This used to be a fixed path -
        # nuarr_encprobe_cpu.mkv - so two probes running at once (the settings
        # page and a planner, or two browser tabs) shared one file: the first
        # finished, the second's `finally` deleted it, and the first then found
        # its own output missing and recorded a PASS as a failure.
        out = os.path.join(
            tempfile.gettempdir(),
            f"nuarr_encprobe_{fam}_{os.getpid()}_{int(time.time() * 1000)}.mkv")
        cmd = [_ff(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=2"]
        cmd += ["-c:v", enc, "-preset", spec["default_preset"]]
        cmd += _quality_args(fam, 28)
        cmd += ["-t", "2", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, creationflags=NO_WINDOW,
            startupinfo=hidden_si())
            ok = (p.returncode == 0 and os.path.exists(out)
                  and os.path.getsize(out) > 1024)
            err = "" if ok else (p.stderr or "").strip().splitlines()[-1:] or [""]
            last = {"ok": ok,
                    "detail": "" if ok else (err[0] if err else "failed"),
                    "seconds": round(time.time() - t0, 2)}
        except subprocess.TimeoutExpired:
            last = {"ok": False, "detail": "timed out", "seconds": timeout}
        except Exception as e:                           # noqa: BLE001
            last = {"ok": False, "detail": f"{type(e).__name__}: {e}",
                    "seconds": round(time.time() - t0, 2)}
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
        if last["ok"]:
            return last
        # ONE RETRY, BECAUSE THIS RUNS ON A BUSY MACHINE. The probe is a real
        # encode competing with every job in flight; on a box with seven
        # running it can lose the race and fail for reasons that have nothing
        # to do with the encoder. A single transient failure used to be
        # recorded as "this hardware does not work" and cached for an hour.
        if attempt + 1 < attempts:
            time.sleep(1.5)
    return last


def probe(force: bool = False) -> dict:
    """{family: {ok, detail, seconds, label, note}} - cached.

    A GOOD ANSWER KEEPS FOR AN HOUR; A TOTAL FAILURE KEEPS FOR A MINUTE.

    Encoders do not come and go, so caching a working answer for an hour is
    free. Caching "nothing works" for an hour is not: every job planned in
    that window is planned for the CPU, and on this machine that is the
    difference between 200 fps on the card and 10 fps on the processor. It
    happened - a probe under heavy load failed, and an hour of encodes were
    routed to software while the ffmpeg page went on correctly reporting that
    NVENC was fine.

    So a result with nothing usable is treated as provisional and re-checked
    a minute later, rather than believed until teatime.
    """
    now = time.time()
    ttl = float(_CACHE.get("ttl") or _TTL)
    if not force and _CACHE["result"] and now - _CACHE["at"] < ttl:
        return _CACHE["result"]
    out = {}
    for fam in ORDER:
        r = _try_family(fam)
        r["label"] = FAMILIES[fam]["label"]
        r["note"] = FAMILIES[fam]["note"]
        r["presets"] = FAMILIES[fam]["presets"]
        out[fam] = r
    any_ok = any(v.get("ok") for v in out.values())
    _CACHE["result"], _CACHE["at"] = out, now
    _CACHE["ttl"] = _TTL if any_ok else _FAIL_TTL
    return out


def usable() -> list[str]:
    """Families that actually produced a file, in preference order."""
    p = probe()
    return [f for f in ORDER if p.get(f, {}).get("ok")]


def resolve(want: str) -> tuple[str, str]:
    r"""Turn a requested family into one that works.

    Returns (family, why). `why` is empty when the request was honoured, and
    otherwise says what happened - the caller logs it rather than silently
    encoding with something the person did not choose.
    """
    have = usable()
    if not have:
        # Nothing probed clean. Rather than refuse to work, assume the CPU
        # encoder - it needs no hardware, so a probe failure is more likely to
        # be the probe's fault than a real absence.
        return "cpu", ("no encoder passed the probe; assuming CPU. "
                       "Check the ffmpeg build under Tools.")
    if want in ("", "auto"):
        return have[0], ""
    if want in have:
        return want, ""
    fallback = have[0]
    lbl = FAMILIES.get(want, {}).get("label", want)
    return fallback, (f"{lbl} is not usable on this machine - "
                      f"using {FAMILIES[fallback]['label']} instead")


# --------------------------------------------------- flags, per family ------

def _quality_args(fam: str, cq: int) -> list[str]:
    """The rate-control flags. Each family spells constant quality its own way."""
    cq = int(cq)
    if fam == "nvenc":
        return ["-rc", "vbr", "-cq", str(cq)]
    if fam == "qsv":
        # QSV has no -cq; -global_quality is its constant-quality control and
        # -look_ahead is what makes that behave sanely on it.
        return ["-global_quality", str(cq)]
    if fam == "amf":
        # AMF wants the quantiser given per frame type; using one value for
        # all three is the closest equivalent to a single CQ.
        return ["-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq), "-qp_b", str(cq)]
    return ["-crf", str(cq)]                    # cpu: libx264 / libx265


def video_args(family: str, target: str, cq: int, preset: str,
               ten_bit: bool = False) -> list[str]:
    r"""Everything after -c:v for one encode, for whichever family is in use.

    Kept in one place so the command builder does not grow a branch per family
    per call site - there are four call sites and four families, and the flags
    are the part that is easy to get subtly wrong.
    """
    spec = FAMILIES.get(family) or FAMILIES["cpu"]
    enc = encoder_for(family, target)
    # A preset from another family is meaningless here (p5 means nothing to
    # libx265) - fall back to this family's default rather than pass it on.
    if preset not in spec["presets"]:
        preset = spec["default_preset"]
    a = ["-c:v", enc, "-preset", preset] + _quality_args(family, cq)
    if ten_bit:
        a += ["-pix_fmt", spec["pix10"]]
        if target == "hevc":
            a += ["-profile:v", "main10"]
    return a


def _rational(v, scale: float) -> int | None:
    """ffprobe writes these as '34000/50000'. Return them in x265's units."""
    try:
        s = str(v)
        if "/" in s:
            a, b = s.split("/", 1)
            f = float(a) / float(b)
        else:
            f = float(s)
        return int(round(f * scale))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def hdr_args(vstream: dict, family: str) -> list[str]:
    r"""Carry HDR through a re-encode, instead of assuming it cannot survive.

    WHY THIS EXISTS. nuarr refused to burn subtitles into HDR video on the
    grounds that "re-encoding HDR loses the metadata". That is only true of an
    encode that says nothing about colour: ffmpeg tags the output from the
    DECODER's frames, and any filter in the chain - overlay, subtitles, a
    format conversion - can hand the encoder frames whose tags have been
    normalised away. The result is a picture whose pixels are still PQ but
    whose container says BT.709, which a player renders as washed-out grey.
    That is the failure everyone means by "it lost the HDR", and it is a
    missing flag, not a law of physics.

    So: the colour tags are stated EXPLICITLY on the output, and the two
    static HDR10 metadata blocks - mastering display and content light - are
    re-stated for libx265, which will not write them otherwise. NVENC takes
    them from frame side data itself once the tags are right.

    WHAT IS STILL LOST is the Dolby Vision RPU: it is a per-frame enhancement
    layer no encoder here can regenerate. nuarr already strips DV to HDR10 on
    copy for exactly that reason, and a DV file that gets burned comes out as
    the HDR10 base layer - which is what its own strip_dv rule would have
    produced anyway.
    """
    trc = (vstream.get("color_transfer") or "").lower()
    if trc not in ("smpte2084", "arib-std-b67"):
        return []
    a: list[str] = []
    for flag, key in (("-color_primaries", "color_primaries"),
                      ("-color_trc", "color_transfer"),
                      ("-colorspace", "color_space")):
        val = vstream.get(key)
        if val:
            a += [flag, str(val)]
    rng = vstream.get("color_range")
    if rng:
        a += ["-color_range", str(rng)]
    if family != "cpu":
        # NVENC/QSV/AMF write the HDR10 SEI from the frame's own side data;
        # the tags above are what they were missing.
        return a
    # libx265 needs it spelled out, in its own units: chromaticities in
    # 0.00002 steps, luminance in 0.0001 cd/m2.
    md = cll = ""
    for sd in (vstream.get("side_data_list") or []):
        t = (sd.get("side_data_type") or "").lower()
        if "mastering display" in t:
            g = (_rational(sd.get("green_x"), 50000),
                 _rational(sd.get("green_y"), 50000))
            b = (_rational(sd.get("blue_x"), 50000),
                 _rational(sd.get("blue_y"), 50000))
            r = (_rational(sd.get("red_x"), 50000),
                 _rational(sd.get("red_y"), 50000))
            w = (_rational(sd.get("white_point_x"), 50000),
                 _rational(sd.get("white_point_y"), 50000))
            lmax = _rational(sd.get("max_luminance"), 10000)
            lmin = _rational(sd.get("min_luminance"), 10000)
            if all(x is not None for x in g + b + r + w) \
                    and lmax is not None and lmin is not None:
                md = (f"G({g[0]},{g[1]})B({b[0]},{b[1]})R({r[0]},{r[1]})"
                      f"WP({w[0]},{w[1]})L({lmax},{lmin})")
        elif "content light" in t:
            try:
                cll = f"{int(sd['max_content'])},{int(sd['max_average'])}"
            except (KeyError, TypeError, ValueError):
                pass
    params = ["hdr-opt=1", "repeat-headers=1"]
    if md:
        params.append(f"master-display={md}")
    if cll:
        params.append(f"max-cll={cll}")
    a += ["-x265-params", ":".join(params)]
    return a


def decode_args(family: str, want_hw: bool) -> list[str]:
    r"""Input-side hardware decode flags.

    Only ever the family's OWN accelerator: feeding CUDA frames to an AMF
    encoder means a download and re-upload per frame, which is slower than
    decoding on the CPU in the first place.
    """
    if not want_hw:
        return []
    hw = (FAMILIES.get(family) or {}).get("hwaccel") or ""
    return ["-hwaccel", hw] if hw else []


_DEVICES: dict = {}


def devices() -> dict:
    """The actual hardware behind the family labels, asked once and kept.

    "NVENC" answers which door the frames go through; the QUESTION people ask
    at the concurrency page is what is behind the door - which card, or
    failing that which CPU. Cached forever because GPUs do not hot-swap.
    """
    if _DEVICES:
        return dict(_DEVICES)
    import platform
    import subprocess
    from .config import NO_WINDOW
    gpu = ""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name",
                            "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=10, creationflags=NO_WINDOW,
                startupinfo=hidden_si())
        if r.returncode == 0:
            gpu = (r.stdout or "").strip().splitlines()[0].strip()
    except Exception:                                    # noqa: BLE001
        pass
    cpu = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
                            ) as k:
            cpu = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:                                    # noqa: BLE001
        cpu = platform.processor() or ""
    _DEVICES.update(gpu_name=gpu, cpu_name=cpu)
    return dict(_DEVICES)


def info() -> dict:
    """For the settings page."""
    p = probe()
    fam, why = resolve("auto")
    return {
        "families": p,
        "usable": usable(),
        "auto": fam,
        "auto_why": why,
        "order": ORDER,
        **devices(),
    }
