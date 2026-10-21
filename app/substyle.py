r"""nuarr - how the Subtitle maker's English subtitles look, per kind of media.

WHY NOT PLAIN SRT EVERYWHERE. An SRT has no look of its own: the player
draws it in whatever it likes. That is right for a film, and it is what Plex
plays without effort on every client. Anime viewers expect the fansub look -
a heavy rounded sans, white with a thick black outline and a soft shadow, the
dialogue at the bottom and the on-screen text the reader saw (signs, title
cards) at the top in a style of its own - and that needs an ASS track.

So each kind of media - anime, animated, live action, read from the library's
name - gets a preset, chosen on the Subtitle maker panel:

    fansub     Trebuchet MS bold, thick outline, shadow   (Crunchyroll's classic)
    modern     Roboto Medium, outline, light shadow        (newer fansub groups)
    streaming  Arial, thin outline, light shadow           (the streaming look)
    plain      SRT - the player decides                    (plays everywhere)

THE FONT GOES INTO THE FILE. A TV has no Trebuchet MS; an ASS track naming a
font the player does not have falls back to whatever it does have, which is
the look this is here to avoid. So the font files the style names are attached
to the MKV beside the track (subembed does that for any .ass it takes in) -
which is what every fansub release does. The fonts are the ones already on
this machine; nothing is downloaded.

THE COST, SAID ONCE. Plex's TV apps cannot all draw ASS themselves; where one
cannot, Plex burns the subtitles into the picture, which is a transcode. That
is why live action defaults to plain SRT.
"""
from __future__ import annotations

import json
import os
import re

KV = "submake.styles"
GROUPS = [("anime", "Anime"), ("animated", "Animated"), ("live", "Live action")]
DEFAULTS = {"anime": "fansub", "animated": "modern", "live": "plain"}
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

# Each preset: the dialogue style and the sign style, at PlayRes 1920x1080.
# Colours are ASS &HAABBGGRR. The files are what gets attached.
PRESETS = {
    "fansub": {
        "word": "Fansub - Trebuchet MS bold, thick outline",
        "font": "Trebuchet MS", "bold": -1, "size": 66, "outline": 3.6, "shadow": 1.6,
        "primary": "&H00FFFFFF", "outline_c": "&H00000000", "back": "&H96000000",
        "sign_size": 54, "sign_outline": 3.0, "sign_primary": "&H00FFFFFF", "sign_outline_c": "&H00402000",
        "margin_v": 48,
        "files": ["trebucbd.ttf", "trebucbi.ttf", "trebuc.ttf", "trebucit.ttf"],
    },
    "modern": {
        "word": "Modern fansub - Roboto Medium, outline",
        "font": "Roboto Medium", "bold": 0, "size": 64, "outline": 3.0, "shadow": 1.2,
        "primary": "&H00FFFFFF", "outline_c": "&H00000000", "back": "&H96000000",
        "sign_size": 52, "sign_outline": 2.6, "sign_primary": "&H00FFFFFF", "sign_outline_c": "&H00402000",
        "margin_v": 46,
        "files": ["Roboto-Medium.ttf"],
    },
    "streaming": {
        "word": "Streaming - Arial, thin outline",
        "font": "Arial", "bold": 0, "size": 60, "outline": 2.0, "shadow": 0.8,
        "primary": "&H00FFFFFF", "outline_c": "&H00000000", "back": "&HA0000000",
        "sign_size": 50, "sign_outline": 1.8, "sign_primary": "&H00FFFFFF", "sign_outline_c": "&H00000000",
        "margin_v": 50,
        "files": ["arial.ttf", "arialbd.ttf", "ariali.ttf"],
    },
    "plain": {"word": "Plain SRT - the player decides", "files": []},
}


def group_of(library: str) -> str:
    low = (library or "").lower()
    if "anime" in low:
        return "anime"
    if "animat" in low or "cartoon" in low:
        return "animated"
    return "live"


def chosen() -> dict:
    from .db import kv_get
    out = dict(DEFAULTS)
    try:
        out.update({k: v for k, v in json.loads(kv_get(KV) or "{}").items()
                    if k in DEFAULTS and v in PRESETS})
    except Exception:                                            # noqa: BLE001
        pass
    return out


def set_chosen(group: str, preset: str) -> dict:
    from .db import kv_set
    if group not in DEFAULTS or preset not in PRESETS:
        raise ValueError("unknown group or style")
    c = chosen()
    c[group] = preset
    kv_set(KV, json.dumps(c))
    return c


def for_library(library: str) -> str:
    return chosen()[group_of(library)]


def font_paths(preset: str) -> list:
    return [p for p in (os.path.join(FONT_DIR, f) for f in PRESETS.get(preset, {}).get("files", []))
            if os.path.exists(p)]


def fonts_in_ass(ass_path: str) -> list:
    """The font files on this machine for the fonts an .ass names - for any
    .ass, so an embedded fansub sidecar carries its fonts too."""
    try:
        text = open(ass_path, encoding="utf-8-sig", errors="replace").read()
    except OSError:
        return []
    names = {m.group(1).strip().lower() for m in re.finditer(r"(?m)^Style:\s*[^,]*,\s*([^,]+),", text)}
    out = []
    for p in PRESETS.values():
        if p.get("font", "").lower() in names:
            out += font_paths_for(p)
    return list(dict.fromkeys(out))


def font_paths_for(p: dict) -> list:
    return [x for x in (os.path.join(FONT_DIR, f) for f in p.get("files", [])) if os.path.exists(x)]


def preset_of_ass(ass_path: str) -> str:
    try:
        text = open(ass_path, encoding="utf-8-sig", errors="replace").read(4000)
    except OSError:
        return ""
    m = re.search(r"(?m)^; nuarr style: (\w+)", text)
    return m.group(1) if m else ""


# ------------------------------------------------------------ SRT -> ASS --
def _ts(t: str) -> str:
    """'00:01:02,345' -> '0:01:02.34'"""
    h, m, rest = t.strip().replace(".", ",").split(":")
    s, ms = rest.split(",")
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{int(ms) // 10:02d}"


def srt_cues(text: str) -> list:
    """[(start, end, body)] with SRT tags still in the body."""
    out = []
    for b in re.split(r"\r?\n\s*\r?\n", (text or "").strip()):
        L = b.splitlines()
        k = next((i for i, x in enumerate(L) if "-->" in x), None)
        if k is None:
            continue
        a, _, z = L[k].partition("-->")
        body = "\n".join(L[k + 1:]).strip()
        if body:
            out.append((a.strip().split(" ")[0], z.strip().split(" ")[0], body))
    return out


def to_ass(srt_text: str, preset: str, title: str = "", override: dict | None = None) -> str:
    p = dict(PRESETS[preset])
    # THE SHOW'S OWN LOOK, when sublearn has read enough of its tracks: font,
    # size and placement follow the fansub's dialogue style; the preset keeps
    # supplying what the track did not say.
    if override:
        for k in ("font", "size", "align", "margin_v", "bold", "outline"):
            if override.get(k) not in (None, ""):
                p[k] = override[k]
        if override.get("primary"):
            p["primary"] = override["primary"]
        if override.get("outline_c"):
            p["outline_c"] = override["outline_c"]
    align = int(p.get("align") or 2)
    head = [
        "[Script Info]",
        f"; nuarr style: {preset}",
        f"Title: {title or 'English'}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{p['font']},{p['size']},{p['primary']},&H000000FF,{p['outline_c']},{p['back']},"
        f"{p['bold']},0,0,0,100,100,0,0,1,{p['outline']},{p['shadow']},{align},120,120,{p['margin_v']},1",
        f"Style: Sign,{p['font']},{p['sign_size']},{p['sign_primary']},&H000000FF,{p['sign_outline_c']},"
        f"{p['back']},{p['bold']},0,0,0,100,100,0,0,1,{p['sign_outline']},{p['shadow']},8,120,120,"
        f"{p['margin_v']},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev = []
    for a, z, body in srt_cues(srt_text):
        style = "Default"
        if body.startswith("{\\an8}"):
            style, body = "Sign", body[len("{\\an8}"):]
        body = (body.replace("<i>", "{\\i1}").replace("</i>", "{\\i0}")
                .replace("<b>", "{\\b1}").replace("</b>", "{\\b0}"))
        body = re.sub(r"</?[a-zA-Z][^>]*>", "", body).replace("\r", "").replace("\n", "\\N")
        try:
            ev.append(f"Dialogue: {1 if style == 'Sign' else 0},{_ts(a)},{_ts(z)},{style},,0,0,0,,{body}")
        except Exception:                                        # noqa: BLE001
            continue
    return "\n".join(head + ev) + "\n"


def ass_to_srt(ass_text: str) -> str:
    """Back to SRT, keeping what nuarr's own ASS carries: the Sign style as
    {\\an8}, italics as <i>. Used when a group is switched back to plain."""
    def srt_ts(t):
        h, m, s = t.split(":")
        sec, cs = (s.split(".") + ["0"])[:2]
        return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{int(cs.ljust(2, '0')[:2]) * 10:03d}"
    fmt, out = None, []
    for ln in (ass_text or "").splitlines():
        if ln.startswith("Format:") and fmt is None and "Text" in ln and "Start" in ln:
            fmt = [x.strip() for x in ln[7:].split(",")]
        if not ln.startswith("Dialogue:") or not fmt:
            continue
        parts = ln[9:].split(",", len(fmt) - 1)
        d = dict(zip(fmt, [x.strip() if k != len(fmt) - 1 else x for k, x in enumerate(parts)]))
        body = d.get("Text", "")
        body = body.replace("{\\i1}", "<i>").replace("{\\i0}", "</i>").replace("\\N", "\n").replace("\\n", "\n")
        body = re.sub(r"\{[^}]*\}", "", body).strip()
        if not body:
            continue
        if d.get("Style", "").lower().startswith("sign"):
            body = "{\\an8}" + body
        try:
            out.append((srt_ts(d["Start"]), srt_ts(d["End"]), body))
        except Exception:                                        # noqa: BLE001
            continue
    out.sort()
    return "\n\n".join(f"{i}\n{a} --> {z}\n{b}" for i, (a, z, b) in enumerate(out, 1)) + "\n"


def snapshot() -> dict:
    return {"groups": [{"id": g, "word": w, "chosen": chosen()[g], "default": DEFAULTS[g]} for g, w in GROUPS],
            "presets": [{"id": k, "word": v["word"], "fonts": [os.path.basename(x) for x in font_paths(k)],
                         "missing": [f for f in v.get("files", []) if not os.path.exists(os.path.join(FONT_DIR, f))]}
                        for k, v in PRESETS.items()]}
