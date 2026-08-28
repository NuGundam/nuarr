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
import sys


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
    ap.add_argument("--progress", action="store_true")
    a = ap.parse_args()

    try:
        from paddleocr import PaddleOCR
    except Exception as e:                               # noqa: BLE001
        _die(f"paddleocr is not installed: {e}")

    cues = read_sup(a.sup)
    if not cues:
        _die("no cues decoded from the sup")

    # enable_mkldnn=False: paddlepaddle 3.3.x raises
    # "ConvertPirAttribute2RuntimeAttribute not support" from its oneDNN
    # kernel on this class of model. Disabling it costs CPU speed and is the
    # difference between working and not.
    kw = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
              use_textline_orientation=False, lang=a.lang, device=a.device)
    try:
        ocr = PaddleOCR(enable_mkldnn=False, **kw)
    except TypeError:
        ocr = PaddleOCR(**kw)

    import numpy as np
    rows = []
    n = len(cues)
    for i, (img, start, end, x, y, vw, vh) in enumerate(cues):
        try:
            # THREE CHANNELS, ALWAYS. The PGS decoder hands back a single
            # greyscale plane; Paddle's detector expects an H x W x 3 image
            # and quietly finds nothing at all in a 2-D array rather than
            # complaining about it.
            # `pic`, not `a` - `a` is the argument namespace, and shadowing
            # it here turned `a.progress` into an attribute lookup on a numpy
            # array a few lines later.
            pic = img
            if pic.ndim == 2:
                pic = np.stack([pic] * 3, axis=-1)
            res = ocr.predict(pic)
        except Exception:                                # noqa: BLE001
            continue
        texts, boxes = [], []
        for r in res:
            texts += list(r.get("rec_texts") or [])
            boxes += [list(b) for b in (r.get("rec_polys") or [])]
        txt = "\n".join(t.strip() for t in texts if t and t.strip())
        if not txt:
            continue
        rows.append({"start": start, "end": max(end, start + 500), "text": txt,
                     "x": x, "y": y, "vw": vw, "vh": vh,
                     "h": int(img.shape[0]), "w": int(img.shape[1])})
        if a.progress and (i % 10 == 0):
            print(f"PROGRESS {i}/{n}", flush=True)

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
            # The centre of where the bitmap sat, which is where the words
            # were - alignment 5 means "centred on this point".
            cx = int(r["x"] + r["w"] / 2)
            cy = int(r["y"] + r["h"] / 2)
            t = r["text"].replace("\n", r"\N")
            body.append(f"Dialogue: 0,{_ass_ts(r['start'])},{_ass_ts(r['end'])},"
                        f"Default,,0,0,0,,{{\\pos({cx},{cy})}}{t}")
        open(a.out, "w", encoding="utf-8").write(head + "\n".join(body) + "\n")
    else:
        parts = []
        for i, r in enumerate(rows, 1):
            parts.append(f"{i}\n{_ts(r['start'])} --> {_ts(r['end'])}\n"
                         f"{r['text']}\n")
        open(a.out, "w", encoding="utf-8").write("\n".join(parts))
    print(f"OK {len(rows)} cues -> {os.path.basename(a.out)}")


if __name__ == "__main__":
    main()
