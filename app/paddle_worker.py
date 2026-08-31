r"""Read a PGS subtitle track with PaddleOCR. Runs as its own process.

WHY A SEPARATE PROCESS, AND NOT AN IMPORT.

PaddleOCR drags in paddlepaddle, paddlex, opencv and its own numpy
expectations - hundreds of megabytes of native code that pins versions
faster-whisper and ctranslate2 also care about. nuarr's server process holds
the database, the job queue and every scheduled loop; putting that dependency
graph inside it means one bad wheel takes the whole application down, and a
segfault in a native OCR kernel is not an exception anyone can catch.

So it is a script. nuarr runs it the same way it runs pgsrip - arguments in,
an SRT out, a non-zero exit code if something went wrong - and the only
contract between them is the file on disk. That also means Paddle can be
installed, upgraded or removed without restarting nuarr.

WHAT IT DOES THAT pgsrip DOES NOT: it keeps the POSITION. Every PGS display
set says where on screen its bitmap belongs, and that survives here - so a
sign at the top of the frame can be written as ASS with a \pos tag instead of
being flattened into a centred subtitle at the bottom. See --ass.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess as _sp
import sys

# HIDE EVERY DESCENDANT. This process runs without a console, so any
# console-subsystem child anything in here spawns would get a brand new one
# on the desktop. Patch Popen before the heavy imports - it underlies run,
# call and check_output, so one patch covers whatever paddle or pytesseract
# decide to launch. See pgsrip_hidden.py for the full story.
#
# BOTH HALVES, NOT JUST THE FLAG. This patched creationflags only, and that was
# not enough: sampling every process at 4 Hz while OCR ran caught nvidia-smi
# and ffmpeg launches from inside these workers, each with a conhost.exe of its
# own - one console window apiece. CREATE_NO_WINDOW asks for no console; some
# console-subsystem binaries take one anyway. STARTF_USESHOWWINDOW with SW_HIDE
# says how to show a window if one is created, so a console that does get
# allocated is never mapped to the screen.
#
# It scaled with the worker count, which is what made it visible: one OCR
# worker flickered rarely enough to miss, six flickered constantly.
if os.name == "nt":
    _orig_popen_init = _sp.Popen.__init__

    def _hidden_popen_init(self, *a, **k):
        k["creationflags"] = (k.get("creationflags") or 0) | 0x08000000
        if not k.get("startupinfo"):
            si = _sp.STARTUPINFO()
            si.dwFlags |= _sp.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                  # SW_HIDE
            k["startupinfo"] = si
        _orig_popen_init(self, *a, **k)

    _sp.Popen.__init__ = _hidden_popen_init


def _die(msg: str) -> None:
    print(f"paddle-worker: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _ass_ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h}:{m:02}:{s:02}.{ms // 10:02}"


def read_sup(path: str):
    """Decode the .sup into (image, start_ms, end_ms, x, y, video_w, video_h).

    Uses pgsrip's own PGS parser, so nuarr has exactly one implementation of
    "what is in this subtitle track" and the two engines cannot disagree
    about how many cues a file contains.
    """
    import numpy as np
    from pgsrip.media_path import MediaPath
    from pgsrip.pgs import PgsImage, PgsReader

    data = open(path, "rb").read()
    mp = MediaPath(path)
    out = []
    pending = None
    for ds in PgsReader.decode(data, mp):
        if not ds.is_valid():
            continue
        # pgsrip hands the timestamp back as a SubRipTime, not a number -
        # it carries .ordinal (milliseconds) rather than being an int.
        raw_ts = getattr(ds.pcs, "presentation_timestamp", 0) or 0
        start = int(getattr(raw_ts, "ordinal", raw_ts) or 0)
        if ds.ods_segments and ds.pds_segments:
            # A new bitmap: close the previous cue, open this one.
            try:
                ods = ds.ods_segments[0]
                raw = getattr(ods, "img_data", None) or getattr(ods, "data", None)
                arr = np.array(PgsImage(raw, ds.pds_segments[0].palettes).data)
                if arr.size == 0:
                    continue
                if arr.dtype != np.uint8:
                    arr = ((arr * 255) if arr.max() <= 1 else arr).clip(0, 255) \
                        .astype("uint8")
            except Exception:                            # noqa: BLE001
                continue
            if pending:
                pending[2] = start
                out.append(pending)
            w = ds.wds
            pending = [arr, start, start,
                       getattr(w, "x_offset", 0) or 0,
                       getattr(w, "y_offset", 0) or 0,
                       getattr(ds.pcs, "width", 1920) or 1920,
                       getattr(ds.pcs, "height", 1080) or 1080]
        elif pending:
            # An empty display set is the CLEAR - the cue ends here.
            pending[2] = start
            out.append(pending)
            pending = None
    if pending:
        pending[2] = pending[1] + 3000
        out.append(pending)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sup")
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--ass", action="store_true",
                    help="write positioned ASS instead of flat SRT")
    ap.add_argument("--an8", action="store_true",
                    help="SRT, but tag top and middle cues with {\\an8}/{\\an5} "
                         "so signs keep their place without leaving SRT")
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--engine", default="paddle",
                    choices=("paddle", "tesseract"))
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N cues - used by the settings-page test")
    a = ap.parse_args()

    import numpy as np

    cues = read_sup(a.sup)
    if not cues:
        _die("no cues decoded from the sup")
    if a.limit:
        cues = cues[:a.limit]

    # ONE HARNESS, TWO ENGINES. Tesseract normally runs through pgsrip, which
    # does its own decoding - but a comparison is only worth reading if both
    # engines saw exactly the same pictures, so the test path drives both from
    # the decode above. Each `read` takes an image and returns text lines.
    if a.engine == "paddle":
        try:
            from paddleocr import PaddleOCR
        except Exception as e:                           # noqa: BLE001
            _die(f"paddleocr is not installed: {e}")
        # enable_mkldnn=False: paddlepaddle 3.3.x raises
        # "ConvertPirAttribute2RuntimeAttribute not support" from its oneDNN
        # kernel on this class of model. Disabling it costs CPU speed and is
        # the difference between working and not working at all.
        kw = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
                  use_textline_orientation=False, lang=a.lang, device=a.device)
        try:
            ocr = PaddleOCR(enable_mkldnn=False, **kw)
        except TypeError:
            ocr = PaddleOCR(**kw)

        def read(pic):
            # THREE CHANNELS, ALWAYS. The PGS decoder hands back a single
            # greyscale plane; Paddle's detector expects an H x W x 3 image
            # and quietly finds nothing at all in a 2-D array rather than
            # complaining about it.
            if pic.ndim == 2:
                pic = np.stack([pic] * 3, axis=-1)
            if pic.shape[2] == 4:
                pic = pic[:, :, :3]
            texts, ys = [], []
            for r in ocr.predict(pic):
                got = list(r.get("rec_texts") or [])
                texts += got
                # WHERE THE WORDS ARE, not where the bitmap is. A PGS display
                # set gives the position of the whole BITMAP, and for a sign
                # that bitmap is often most of the frame with the text in one
                # corner - measured on Detective Conan, the same top sign read
                # 0.12 of frame height from its text box while its bitmap
                # centre wandered from 0.12 to 0.27 as the bitmap grew. The
                # text box is the honest answer; the bitmap box is a canvas.
                polys = r.get("rec_polys")
                if polys is None:
                    polys = r.get("dt_polys")
                if polys is not None and len(polys):
                    for p in polys:
                        arr = np.asarray(p, dtype=float)
                        if arr.ndim == 2 and arr.shape[1] >= 2:
                            ys.append(float(arr[:, 1].mean()))
            # Mean AND spread. One bitmap often carries two blocks that belong
            # in different places - a sign across the top and the dialogue
            # under it, or karaoke romaji with its translation. Averaging those
            # lands in the middle and moves BOTH somewhere neither belongs; on
            # a real episode that was 324 of 681 cues. The caller uses the
            # spread to refuse to guess.
            if not ys:
                return texts, None, None
            return texts, sum(ys) / len(ys), (max(ys) - min(ys))
    else:
        try:
            import pytesseract
            from PIL import Image
        except Exception as e:                           # noqa: BLE001
            _die(f"pytesseract is not available: {e}")
        tdir = os.environ.get("NUARR_TESSERACT_DIR", "")
        if tdir and os.path.isdir(tdir):
            os.environ["PATH"] = tdir + os.pathsep + os.environ.get("PATH", "")

        def read(pic):
            txt = pytesseract.image_to_string(
                Image.fromarray(pic).convert("L"), lang="eng", config="--psm 6")
            # No box from this path, so callers fall back to the bitmap.
            return [l for l in txt.splitlines() if l.strip()], None, None

    rows = []
    n = len(cues)
    for i, (img, start, end, x, y, vw, vh) in enumerate(cues):
        try:
            texts, ty, spread = read(img)
        except Exception:                                # noqa: BLE001
            continue
        txt = "\n".join(t.strip() for t in texts if t and t.strip())
        if not txt:
            continue
        # Text centre in FRAME coordinates: the bitmap's offset plus where the
        # words sat inside it. Falls back to the bitmap's own centre when the
        # engine gave no boxes (Tesseract).
        cy = y + (ty if ty is not None else img.shape[0] / 2)
        rows.append({"start": start, "end": max(end, start + 500), "text": txt,
                     "x": x, "y": y, "vw": vw, "vh": vh, "cy": cy,
                     "spread": spread,
                     "h": int(img.shape[0]), "w": int(img.shape[1])})
        # EVERY CUE, NOT EVERY TENTH: this is the only true progress signal
        # either engine emits, and nuarr's bar reads it directly. The old
        # size-based estimate existed purely because pgsrip says nothing.
        if a.progress:
            print(f"PROGRESS {i + 1}/{n}", flush=True)

    if not rows:
        _die("nothing was read from any cue")

    if a.ass:
        vw = rows[0]["vw"]
        vh = rows[0]["vh"]
        head = ("[Script Info]\nScriptType: v4.00+\n"
                f"PlayResX: {vw}\nPlayResY: {vh}\n"
                "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, "
                "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
                "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
                "MarginV, Encoding\n"
                f"Style: Default,Arial,{max(20, vh // 20)},&H00FFFFFF,"
                "&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,"
                "5,10,10,10,1\n\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
                "MarginR, MarginV, Effect, Text\n")
        body = []
        for r in rows:
            # Alignment 5 means "centred on this point". Horizontally the
            # bitmap centre is the best available guess; vertically we now
            # know where the WORDS were - see the note in read().
            cx = int(r["x"] + r["w"] / 2)
            cy = int(r["cy"])
            t = r["text"].replace("\n", r"\N")
            body.append(f"Dialogue: 0,{_ass_ts(r['start'])},{_ass_ts(r['end'])},"
                        f"Default,,0,0,0,,{{\\pos({cx},{cy})}}{t}")
        open(a.out, "w", encoding="utf-8").write(head + "\n".join(body) + "\n")
    else:
        # POSITION IN PLAIN SRT, via the alignment tags libass understands.
        #
        # SRT has no positioning of its own, but {\an8} and {\an5} pass
        # straight through ffmpeg's subrip decoder into the ASS it hands the
        # renderer - verified, the tag survives verbatim - and a player that
        # does not understand them simply shows the text where it always did.
        # So a sign at the top stays at the top without giving up direct play.
        #
        # Three bands, measured against where cues actually land. On a real
        # episode the bottom dialogue clusters at 0.89-0.93 of frame height,
        # top signs at 0.10-0.12, and mid-screen karaoke near 0.50, so the
        # cuts sit in the empty space between those groups rather than on a
        # round number.
        parts = []
        for i, r in enumerate(rows, 1):
            tag = ""
            if a.an8:
                vh = r.get("vh") or 1
                frac = r["cy"] / vh
                spread = r.get("spread")
                # ONE TAG CANNOT DESCRIBE TWO PLACES. When a cue's text is
                # spread down the frame it is two blocks that belong apart -
                # a sign and the dialogue beneath it - and the average is a
                # position neither of them wants. Leave those exactly as they
                # render today rather than moving both somewhere wrong.
                split = spread is not None and spread > 0.25 * vh
                if not split and frac < 0.35:
                    tag = r"{\an8}"          # top, and only text at the top
                # Everything else - ordinary dialogue, and any cue we cannot
                # describe with a single anchor - is left un-tagged.
            parts.append(f"{i}\n{_ts(r['start'])} --> {_ts(r['end'])}\n"
                         f"{tag}{r['text']}\n")
        open(a.out, "w", encoding="utf-8").write("\n".join(parts))
    print(f"OK {len(rows)} cues -> {os.path.basename(a.out)}")


if __name__ == "__main__":
    main()
