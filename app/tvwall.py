"""Loft TV wallpaper (Erik, 2026-11-19).

Projectivy on the loft TV loads its wallpaper from a URL. This renders that
picture: the nuarr-style home background with a "Now playing" card in the
free space top-right (whatever Plex is playing anywhere in the house, with
its backdrop, poster and progress), or, when nothing plays, one of the latest
additions to Plex (a different one every 10 minutes). If Plex can't be reached
it is just the plain background.
"""
from __future__ import annotations

import io
import json
import pathlib
import threading
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = pathlib.Path(__file__).parent / "assets"
W, H = 1920, 1080
BG, FG, DIM, ACC, LINE = (15, 18, 22), (230, 233, 239), (139, 149, 165), (88, 166, 255), (36, 42, 51)
# the free area right of the HDMI inputs and above the app row
BOX = (900, 0, W, 425)
_CACHE: dict = {"at": 0.0, "key": None, "jpg": b""}
_LOCK = threading.Lock()


def _plex() -> tuple[str, str]:
    from . import plexnotify
    return plexnotify._plex()


def _get_json(path: str) -> dict:
    url, token = _plex()
    req = urllib.request.Request(url + path, headers={"X-Plex-Token": token, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read() or b"{}")


def _get_img(path: str, w: int, h: int) -> Image.Image | None:
    if not path:
        return None
    url, token = _plex()
    from urllib.parse import quote
    q = f"/photo/:/transcode?width={w}&height={h}&minSize=1&upscale=1&url={quote(path)}"
    try:
        req = urllib.request.Request(url + q, headers={"X-Plex-Token": token})
        with urllib.request.urlopen(req, timeout=8) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception:                                            # noqa: BLE001
        return None


def _font(bold: bool, size: int):
    try:
        return ImageFont.truetype("segoeuib.ttf" if bold else "segoeui.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _fit(d: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if d.textlength(text, font=font) <= width:
        return text
    while text and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _hms(ms: int) -> str:
    s = int(ms or 0) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _pick() -> dict | None:
    """What to show: a playing Plex session first, else a recent addition."""
    try:
        sess = (_get_json("/status/sessions").get("MediaContainer") or {}).get("Metadata") or []
    except Exception:                                            # noqa: BLE001
        return None
    sess = [s for s in sess if s.get("type") in ("movie", "episode")]
    if sess:
        sess.sort(key=lambda s: ((s.get("Player") or {}).get("state") != "playing", -int(s.get("viewOffset") or 0)))
        s = sess[0]
        p = s.get("Player") or {}
        who = " · ".join(x for x in ((s.get("User") or {}).get("title"), p.get("title")) if x)
        return {"label": ("NOW PLAYING" if p.get("state") == "playing" else "PAUSED") + (f"  ·  {who}" if who else ""),
                "item": s, "progress": (int(s.get("viewOffset") or 0), int(s.get("duration") or 0))}
    try:
        rec = (_get_json("/library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=12")
               .get("MediaContainer") or {}).get("Metadata") or []
    except Exception:                                            # noqa: BLE001
        return None
    rec = [r for r in rec if r.get("art") or r.get("grandparentArt") or r.get("parentArt")]
    if not rec:
        return None
    r = rec[int(time.time() // 600) % len(rec)]
    return {"label": "NEW IN PLEX", "item": r, "progress": None}


def _text(it: dict) -> tuple[str, str]:
    t = it.get("type")
    if t == "episode":
        se = f"S{int(it.get('parentIndex') or 0):02d}E{int(it.get('index') or 0):02d}"
        return it.get("grandparentTitle") or it.get("title") or "", f"{se} · {it.get('title') or ''}"
    if t == "season":
        return it.get("parentTitle") or it.get("title") or "", it.get("title") or ""
    sub = " · ".join(str(x) for x in (it.get("year"), it.get("contentRating")) if x)
    return it.get("title") or "", sub


def render() -> bytes:
    with _LOCK:      # at most one Plex look every 20 s, however often the TV asks
        if _CACHE["jpg"] and time.time() - _CACHE["at"] < 20:
            return _CACHE["jpg"]
    base = Image.open(ASSETS / "tv_wallpaper.png").convert("RGB")
    pick = _pick()
    key = None
    if pick:
        it = pick["item"]
        key = (pick["label"], it.get("ratingKey"), (pick["progress"] or (0, 0))[0] // 30000)
    with _LOCK:
        if _CACHE["jpg"] and _CACHE["key"] == key and time.time() - _CACHE["at"] < 600:
            _CACHE["at"] = time.time()
            return _CACHE["jpg"]
    if pick:
        it = pick["item"]
        bw, bh = BOX[2] - BOX[0], BOX[3] - BOX[1]
        art = _get_img(it.get("grandparentArt") or it.get("parentArt") or it.get("art") or "", bw, bh)
        if art:
            # cover-crop, dim, and fade into the background on the left and bottom
            sc = max(bw / art.width, bh / art.height)
            art = art.resize((int(art.width * sc) + 1, int(art.height * sc) + 1))
            art = art.crop(((art.width - bw) // 2, (art.height - bh) // 3,
                            (art.width - bw) // 2 + bw, (art.height - bh) // 3 + bh))
            art = Image.blend(Image.new("RGB", art.size, BG), art, 0.42)
            mask = Image.new("L", (bw, bh), 0)
            md = ImageDraw.Draw(mask)
            for x in range(bw):
                a = min(1.0, x / 420)
                md.line((x, 0, x, bh), fill=int(255 * a))
            fade = Image.new("L", (bw, bh), 255)
            fd = ImageDraw.Draw(fade)
            fd.rectangle((0, 0, bw, 121), fill=0)          # keep the header bar as it is
            for y in range(bh - 140, bh):
                fd.line((0, y, bw, y), fill=int(255 * (bh - y) / 140))
            from PIL import ImageChops
            mask = ImageChops.multiply(mask, fade)
            base.paste(art, BOX[:2], mask)
        d = ImageDraw.Draw(base, "RGBA")
        # poster on the right
        px, py, pw, ph = 1640, 150, 180, 270
        poster = _get_img(it.get("grandparentThumb") or it.get("parentThumb") or it.get("thumb") or "", pw, ph)
        if poster:
            poster = poster.resize((pw, ph))
            m = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(m).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=12, fill=255)
            shadow = Image.new("RGBA", (pw + 40, ph + 40), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((20, 24, pw + 20, ph + 24), radius=14, fill=(0, 0, 0, 150))
            shadow = shadow.filter(ImageFilter.GaussianBlur(10))
            base.paste(shadow, (px - 20, py - 20), shadow)
            base.paste(poster, (px, py), m)
            d.rounded_rectangle((px, py, px + pw - 1, py + ph - 1), radius=12, outline=LINE + (255,), width=2)
        tx, tw = 1010 if poster else 1100, (px - 40 - 1010) if poster else 720
        title, sub = _text(it)
        f_lab, f_t, f_s, f_m = _font(True, 21), _font(True, 50), _font(False, 28), _font(False, 22)
        d.text((tx, 170), _fit(d, pick["label"], f_lab, tw), font=f_lab, fill=ACC)
        d.text((tx, 200), _fit(d, title, f_t, tw), font=f_t, fill=FG)
        if sub:
            d.text((tx, 268), _fit(d, sub, f_s, tw), font=f_s, fill=DIM)
        if pick["progress"] and pick["progress"][1]:
            off, dur = pick["progress"]
            y = 350
            d.rounded_rectangle((tx, y, tx + tw, y + 6), radius=3, fill=(255, 255, 255, 40))
            d.rounded_rectangle((tx, y, tx + max(6, int(tw * min(1, off / dur))), y + 6), radius=3, fill=ACC + (255,))
            d.text((tx, y + 16), _hms(off), font=f_m, fill=DIM)
            left = f"-{_hms(dur - off)}"
            d.text((tx + tw - d.textlength(left, font=f_m), y + 16), left, font=f_m, fill=DIM)
        else:
            summ = (it.get("summary") or it.get("parentSummary") or "").replace("\n", " ")
            if summ:
                # two lines of the summary
                words, lines, cur = summ.split(), [], ""
                for wd in words:
                    if d.textlength((cur + " " + wd).strip(), font=f_m) > tw:
                        lines.append(cur)
                        cur = wd
                        if len(lines) == 2:
                            break
                    else:
                        cur = (cur + " " + wd).strip()
                if len(lines) < 2 and cur:
                    lines.append(cur)
                if len(lines) == 2 and len(" ".join(lines)) < len(summ):
                    lines[1] = _fit(d, lines[1] + " …", f_m, tw)
                for i, ln in enumerate(lines[:2]):
                    d.text((tx, 322 + i * 30), ln, font=f_m, fill=DIM)
    out = io.BytesIO()
    base.save(out, "JPEG", quality=88)
    jpg = out.getvalue()
    with _LOCK:
        _CACHE.update(at=time.time(), key=key, jpg=jpg)
    return jpg
