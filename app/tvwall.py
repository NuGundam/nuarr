"""Loft TV wallpaper (Erik, 2026-11-19).

Projectivy on the loft TV shows this as its home-screen wallpaper: the
nuarr-style background with the house's air readings from Home Assistant -
the same sources the HVAC card's rails use (temperature, RH, absolute
humidity, dew point, CO2, VOC, PM AQI, NOx, each with the fresh-air intake
value) and the outdoor line in the header.

Projectivy downloads a picture URL once and never again, so the TV plays it
as a slow live HLS stream instead (/tv/live.m3u8): every 10-second segment
is the current picture, encoded when the TV asks for it. Nothing is fetched
or encoded while the TV isn't on its home screen.

HA access: ha_url + ha_token in config.yml, set on /tv.
"""
from __future__ import annotations

import io
import json
import math
import pathlib
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont

ASSETS = pathlib.Path(__file__).parent / "assets"
W, H = 1920, 1080
BG, PANEL, LINE = (15, 18, 22), (23, 27, 33), (36, 42, 51)
FG, DIM, ACC = (230, 233, 239), (139, 149, 165), (88, 166, 255)
GREEN, LIME, AMBER, ORANGE, RED, BLUE = ((104, 211, 145), (163, 217, 119), (240, 192, 96),
                                         (246, 147, 91), (224, 87, 91), (116, 185, 232))
SEG = 10                      # seconds per HLS segment
FPS = 5

_IMG: dict = {"at": 0.0, "jpg": b"", "err": ""}
_SEGS: dict[int, bytes] = {}
_LOCK = threading.Lock()
_SEG_LOCK = threading.Lock()

IDS = [
    "sensor.house_temperture_sum", "sensor.house_humidity_sum", "sensor.house_ah_average",
    "sensor.house_dew_point", "sensor.airgradient_co2_index_sum", "sensor.nu_aqi_co2_sum",
    "sensor.airgradient_voc_index_sum", "sensor.nu_aqi_voc_index_sum", "sensor.pm2_5_sum",
    "sensor.house_nox_index", "sensor.house_temperature_trend", "sensor.house_humidity_trend",
    "sensor.house_ah_trend", "input_number.set_temperature",
    "sensor.fresh_air_aqi_temperature", "sensor.fresh_air_aqi_humidity",
    "sensor.fresh_air_aqi_absolute_humidity", "sensor.fresh_air_aqi_voc", "sensor.fresh_air_aqi_nox",
    "sensor.fresh_air_aqi_average_pm_aqi",
    "sensor.outdoor_temperature", "sensor.outdoor_humidity", "sensor.outdoor_forecast_high",
    "sensor.outdoor_forecast_low", "sensor.ecobee_forecast_high", "sensor.ecobee_forecast_low",
    "sun.sun",
]


# ------------------------------------------------------------------ HA ---
def ha_conf() -> tuple[str, str]:
    from .config import SETTINGS
    return (str(getattr(SETTINGS, "ha_url", "") or "").rstrip("/"),
            str(getattr(SETTINGS, "ha_token", "") or ""))


def ha_test(url: str, token: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200, "ok"
    except urllib.error.HTTPError as e:
        return False, "HA refused the token" if e.code in (401, 403) else f"HA answered {e.code}"
    except Exception as e:                                       # noqa: BLE001
        return False, f"can't reach HA ({type(e).__name__})"


def _states() -> dict[str, str]:
    url, token = ha_conf()
    if not (url and token):
        raise RuntimeError("no Home Assistant token set (nuarr /tv)")
    tpl = ("{% set ns = namespace(d={}) %}{% for i in " + json.dumps(IDS) + " %}"
           "{% set ns.d = dict(ns.d, **{i: states(i)}) %}{% endfor %}{{ ns.d | tojson }}")
    req = urllib.request.Request(url + "/api/template", data=json.dumps({"template": tpl}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def _n(s: dict, k: str) -> float:
    try:
        return float(s.get(k))
    except (TypeError, ValueError):
        return float("nan")


def _ok(v: float) -> bool:
    return v == v


def _hi(a: float, b: float) -> float:
    return b if not _ok(a) else a if not _ok(b) else max(a, b)


def pm_aqi(c: float) -> float:
    """US EPA PM2.5 AQI, 2024 breakpoints (the card's pmAqiOf)."""
    if not _ok(c):
        return c
    for lo, hi, alo, ahi in ((0, 9.0, 0, 50), (9.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
                             (55.5, 125.4, 151, 200), (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 500)):
        if c <= hi:
            return alo + (ahi - alo) * (max(c, lo) - lo) / (hi - lo)
    return 500.0


def _dew_f(t_f: float, rh: float) -> float:
    if not (_ok(t_f) and _ok(rh)) or rh <= 0:
        return float("nan")
    t = (t_f - 32) / 1.8
    g = math.log(rh / 100) + 17.62 * t / (243.12 + t)
    return (243.12 * g / (17.62 - g)) * 1.8 + 32


def _ah(t_f: float, rh: float) -> float:
    if not (_ok(t_f) and _ok(rh)):
        return float("nan")
    t = (t_f - 32) / 1.8
    return 6.112 * math.exp(17.67 * t / (t + 243.5)) * rh * 2.1674 / (273.15 + t)


# five-tier scales, the card's (CO2 600/800/1000/1400 · VOC 150/250/350/450 · PM AQI 25/50/100/150)
def _tier5(v: float, cuts) -> tuple[str, tuple]:
    if not _ok(v):
        return "", DIM
    for cut, lab, col in zip(cuts, ("Excellent", "Good", "Moderate", "Poor"), (GREEN, LIME, AMBER, ORANGE)):
        if v < cut:
            return lab, col
    return "Bad", RED


def _dew_tier(dp: float) -> tuple[str, tuple]:
    if not _ok(dp):
        return "", DIM
    for cut, lab, col in ((40, "Very Dry", AMBER), (50, "Dry", LIME), (55, "Pleasant", GREEN),
                          (60, "Comfortable", GREEN), (65, "Sticky", AMBER)):
        if dp < cut:
            return lab, col
    return "Muggy", RED


def _rh_tier(rh: float) -> tuple[str, tuple]:
    if not _ok(rh):
        return "", DIM
    if rh < 30:
        return "Dry", AMBER
    if rh < 40:
        return "OK", LIME
    if rh <= 60:
        return "Ideal", GREEN
    return "Humid", AMBER if rh <= 70 else RED


def _ah_tier(ah: float) -> tuple[str, tuple]:
    if not _ok(ah):
        return "", DIM
    if ah < 7:
        return "Dry", AMBER
    if ah <= 11:
        return "OK", GREEN
    return "Humid", AMBER if ah <= 13 else RED


def _temp_tier(t: float, sp: float) -> tuple[str, tuple]:
    if not (_ok(t) and _ok(sp)):
        return "", DIM
    if t < sp - 1.0:
        return "Below band", BLUE
    if t > sp + 1.0:
        return "Above band", ORANGE
    return "In band", GREEN


def _arrow(trend) -> str:
    t = str(trend or "").lower()
    return "↑" if "rising" in t else "↓" if "falling" in t else "→" if "stable" in t else ""


# -------------------------------------------------------------- drawing ---
def _font(kind: str, size: int):
    name = {"b": "segoeuib.ttf", "sb": "seguisb.ttf", "r": "segoeui.ttf", "sym": "seguisym.ttf"}[kind]
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def _fmt(v: float, dp: int) -> str:
    return "–" if not _ok(v) else f"{v:.{dp}f}"


def _render_house(s: dict) -> Image.Image:
    img = Image.open(ASSETS / "tv_wallpaper.png").convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    sp = _n(s, "input_number.set_temperature")
    hT, hRH, hAH, hDP = (_n(s, "sensor.house_temperture_sum"), _n(s, "sensor.house_humidity_sum"),
                         _n(s, "sensor.house_ah_average"), _n(s, "sensor.house_dew_point"))
    iT, iRH, iAH = (_n(s, "sensor.fresh_air_aqi_temperature"), _n(s, "sensor.fresh_air_aqi_humidity"),
                    _n(s, "sensor.fresh_air_aqi_absolute_humidity"))
    co2 = _hi(_n(s, "sensor.airgradient_co2_index_sum"), _n(s, "sensor.nu_aqi_co2_sum"))
    voc = _hi(_n(s, "sensor.airgradient_voc_index_sum"), _n(s, "sensor.nu_aqi_voc_index_sum"))
    pm = pm_aqi(_n(s, "sensor.pm2_5_sum"))
    nox = _n(s, "sensor.house_nox_index")
    tiles = [
        ("TEMPERATURE", "°F", _fmt(hT, 1), _temp_tier(hT, sp), _arrow(s.get("sensor.house_temperature_trend")),
         f"intake {_fmt(iT, 1)}°" + (f" · set {_fmt(sp, 0)}°" if _ok(sp) else "")),
        ("HUMIDITY", "%", _fmt(hRH, 0), _rh_tier(hRH), _arrow(s.get("sensor.house_humidity_trend")),
         f"intake {_fmt(iRH, 0)} %"),
        ("ABS HUMIDITY", "g/m³", _fmt(hAH, 1), _ah_tier(hAH), _arrow(s.get("sensor.house_ah_trend")),
         f"intake {_fmt(iAH, 1)}"),
        ("DEW POINT", "°F", _fmt(hDP, 1), _dew_tier(hDP), "", f"intake {_fmt(_dew_f(iT, iRH), 1)}°"),
        ("CO₂", "ppm", _fmt(co2, 0), _tier5(co2, (600, 800, 1000, 1400)), "", ""),
        ("VOC", "index", _fmt(voc, 0), _tier5(voc, (150, 250, 350, 450)), "",
         f"intake {_fmt(_n(s, 'sensor.fresh_air_aqi_voc'), 0)}"),
        ("PM AQI", "EPA", _fmt(pm, 0), _tier5(pm, (25, 50, 100, 150)), "",
         f"intake {_fmt(_n(s, 'sensor.fresh_air_aqi_average_pm_aqi'), 0)}"),
        ("NOx", "index", _fmt(nox, 0), _tier5(nox, (2, 20, 100, 200)), "",
         f"intake {_fmt(_n(s, 'sensor.fresh_air_aqi_nox'), 0)}"),
    ]
    # ---- tiles in the free band between the header and the app rows
    x0, y0, gap, th = 96, 152, 14, 196
    tw = (W - 2 * x0 - gap * (len(tiles) - 1)) // len(tiles)
    f_lab, f_unit, f_val, f_pill, f_sub, f_arr = (_font("sb", 15), _font("r", 14), _font("sb", 46),
                                                  _font("sb", 14), _font("r", 16), _font("sym", 28))
    for i, (lab, unit, val, (tier, col), arr, sub) in enumerate(tiles):
        x = x0 + i * (tw + gap)
        d.rounded_rectangle((x, y0, x + tw, y0 + th), radius=12, fill=PANEL + (170,), outline=LINE + (200,), width=1)
        d.rounded_rectangle((x + 16, y0 + th - 4, x + tw - 16, y0 + th - 2), radius=1, fill=col + (130,))
        d.text((x + 16, y0 + 13), lab, font=f_lab, fill=DIM)
        d.text((x + 16, y0 + 32), unit, font=f_unit, fill=DIM + (150,))
        d.text((x + 15, y0 + 58), val, font=f_val, fill=col if tier else FG)
        if arr:
            d.text((x + 22 + d.textlength(val, font=f_val), y0 + 72), arr, font=f_arr, fill=DIM)
        if tier:
            pw = d.textlength(tier, font=f_pill) + 18
            d.rounded_rectangle((x + 15, y0 + 126, x + 15 + pw, y0 + 149), radius=11,
                                fill=col + (32,), outline=col + (110,), width=1)
            d.text((x + 24, y0 + 128), tier, font=f_pill, fill=col)
        if sub:
            sub_t = sub
            while sub_t and d.textlength(sub_t, font=f_sub) > tw - 30:
                sub_t = sub_t[:-1]
            d.text((x + 16, y0 + 160), sub_t, font=f_sub, fill=DIM)
    # ---- outdoor line in the header (the card's OUTDOOR pill)
    oT, oRH = _n(s, "sensor.outdoor_temperature"), _n(s, "sensor.outdoor_humidity")
    hi, lo = _n(s, "sensor.outdoor_forecast_high"), _n(s, "sensor.outdoor_forecast_low")
    hi = hi if _ok(hi) else _n(s, "sensor.ecobee_forecast_high")
    lo = lo if _ok(lo) else _n(s, "sensor.ecobee_forecast_low")
    night = str(s.get("sun.sun")) == "below_horizon"
    parts = [("☾  " if night else "☀  ", _font("sym", 20), (240, 220, 160) if night else AMBER),
             ("OUTDOOR   ", _font("sb", 18), FG), (f"{_fmt(oT, 1)}°F", _font("sb", 22), BLUE),
             ("   ·   AH  ", _font("sb", 18), DIM), (f"{_fmt(_ah(oT, oRH), 1)} g/m³", _font("sb", 20), BLUE)]
    if _ok(hi) or _ok(lo):
        parts.append((f"   ·   H {_fmt(hi, 0)}°  /  L {_fmt(lo, 0)}°", _font("sb", 18), DIM))
    width = sum(d.textlength(t, font=f) for t, f, _ in parts)
    px, py = 410, 41
    d.rounded_rectangle((px - 18, py - 6, px + width + 18, py + 38), radius=22,
                        fill=(255, 255, 255, 10), outline=LINE + (200,), width=1)
    x = px
    for t, f, c in parts:
        bb = d.textbbox((0, 0), t, font=f)
        d.text((x, py + 16 - (bb[1] + bb[3]) / 2), t, font=f, fill=c)
        x += d.textlength(t, font=f)
    # "updated" under the tiles, so a stale picture is obvious
    up, f_up = time.strftime("updated %H:%M").replace(" 0", " "), _font("r", 14)
    d.text((W - x0 - d.textlength(up, font=f_up), y0 + th + 6), up, font=f_up, fill=DIM + (130,))
    return img


def _render_plain(msg: str) -> Image.Image:
    img = Image.open(ASSETS / "tv_wallpaper.png").convert("RGB")
    if msg:
        ImageDraw.Draw(img, "RGBA").text((420, 50), msg, font=_font("r", 24), fill=DIM)
    return img


def render() -> bytes:
    """The current picture as JPEG; HA is asked at most every 20 s."""
    with _LOCK:
        if _IMG["jpg"] and time.time() - _IMG["at"] < 20:
            return _IMG["jpg"]
    try:
        img, err = _render_house(_states()), ""
    except Exception as e:                                       # noqa: BLE001
        err = str(e) if isinstance(e, RuntimeError) else f"Home Assistant unavailable ({type(e).__name__})"
        img = _render_plain(err)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    with _LOCK:
        _IMG.update(at=time.time(), jpg=out.getvalue(), err=err)
        return _IMG["jpg"]


# ---------------------------------------------------------------- wave ---
# THE PS3 WAVE (Erik, 2026-11-25): the blue XMB wave instead of the readings.
# A 16.3 s loop, upscaled once to 4K60 HEVC 10-bit and cut into four segments
# kept in DATA_DIR/tvwave (w0..w3.ts). The same live playlist the TV already
# plays hands them out over and over, with a discontinuity where the loop
# starts again - so nothing is encoded while the TV is on, and the TV's
# wallpaper setting does not change. kv tvwall.mode: "wave" (default while the
# segments exist) or "air".
WAVE_D = (4.0, 4.0, 4.0, 4.3)
WAVE_L = sum(WAVE_D)


def _wave_dir() -> pathlib.Path:
    from .config import DATA_DIR
    return pathlib.Path(str(DATA_DIR)) / "tvwave"


def mode() -> str:
    from .db import kv_get
    m = str(kv_get("tvwall.mode") or "wave")
    return "wave" if m == "wave" and all((_wave_dir() / f"w{i}.ts").exists() for i in range(len(WAVE_D))) else "air"


def set_mode(m: str) -> str:
    from .db import kv_set
    kv_set("tvwall.mode", "air" if m == "air" else "wave")
    return mode()


def _wave_playlist() -> str:
    t = time.time()
    k, rem, acc = len(WAVE_D) * int(t // WAVE_L), t % WAVE_L, 0.0
    for d in WAVE_D[:-1]:
        acc += d
        if rem >= acc:
            k += 1
    first = k - 1
    n = len(WAVE_D)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{math.ceil(max(WAVE_D))}",
             f"#EXT-X-MEDIA-SEQUENCE:{first}", f"#EXT-X-DISCONTINUITY-SEQUENCE:{(first + n - 1) // n}"]
    for j in range(first, first + 4):
        if j % n == 0:
            lines.append("#EXT-X-DISCONTINUITY")
        lines += [f"#EXTINF:{WAVE_D[j % n]:.3f},", f"seg/{j}.ts"]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- HLS ---
def playlist() -> str:
    if mode() == "wave":
        return _wave_playlist()
    n = int(time.time() // SEG)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEG}", f"#EXT-X-MEDIA-SEQUENCE:{n - 2}"]
    for k in range(n - 2, n + 1):
        lines += [f"#EXTINF:{SEG}.000,", f"seg/{k}.ts"]
    return "\n".join(lines) + "\n"


def segment(n: int) -> bytes:
    """Segment n = the current picture, SEG seconds at FPS, with timestamps
    continuous across segments (wrapping with MPEG-TS's 33-bit clock)."""
    if mode() == "wave":
        return (_wave_dir() / f"w{n % len(WAVE_D)}.ts").read_bytes()
    with _SEG_LOCK:
        if n in _SEGS:
            return _SEGS[n]
        from .config import NO_WINDOW
        from .jobs import _ffmpeg_exe
        jpg = render()
        start = ((n * SEG * 90000) % (1 << 33)) / 90000.0
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "f.jpg"
            src.write_bytes(jpg)
            cmd = [_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS),
                   "-i", str(src), "-t", str(SEG), "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
                   "-crf", "20", "-pix_fmt", "yuv420p", "-g", str(SEG * FPS), "-keyint_min", str(SEG * FPS),
                   "-sc_threshold", "0", "-an", "-output_ts_offset", f"{start:.3f}", "-muxdelay", "0",
                   "-f", "mpegts", "-"]
            r = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError("encode failed: " + r.stderr.decode("utf-8", "replace")[-300:])
        _SEGS[n] = r.stdout
        for k in sorted(_SEGS)[:-6]:
            _SEGS.pop(k, None)
        return r.stdout
