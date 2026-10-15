r"""nuarr - make English subtitles for a file that has none, in a child process.

Run by submake.py as `python -m app.submake_worker --src ... --out ...`. It
prints one JSON object per line on stdout - {"stage", "pct", "note"} while it
works, {"done": true, ...} or {"error": ...} at the end - and writes the SRT to
--out. A child, for the same reason Whisper's language check is one: these are
native GPU libraries, and a crash or a stuck CUDA context must cost a process,
not the server. All three models are freed when it exits.

THE TWO HALVES
--------------
SPEECH. Whisper's own timestamps drift - measured on a 68-minute film, lines
up to 14 s early - so it is not asked for them. The voice-activity detector
finds where somebody is speaking, close pieces are joined into utterances, and
each utterance is translated on its own: the subtitle starts and ends exactly
where the voice does, by construction.

THE PICTURE. Some films speak in writing - Sumikko Gurashi's characters never
talk; every line of dialogue is text on screen and the voice is a narrator.
PaddleOCR reads frames twice a second in the audio's script; the same text on
consecutive frames is one event, OCR's near-misses of one line are merged,
lines that appear and leave together are one speech bubble, and a stretch of
dense text (the credit roll) is left out.

Text written for children is mostly hiragana, and a translation model trained
on ordinary sentences falls apart on it - NLLB turned あめがやまないと ("if the
rain doesn't stop") into "I have to stop". A language model reads it in
context, so the on-screen lines go to Qwen2.5-7B-Instruct in numbered batches
with the film's title.

On-screen lines are placed at the top ({\an8}) so they never sit on top of
the narration at the bottom.
"""
from __future__ import annotations

import argparse
import difflib
import gc
import json
import os
import re
import subprocess
import sys
import time

# HIDE EVERY GRANDCHILD'S CONSOLE. This child is started hidden, but what it
# starts (ffmpeg, ffprobe) gets a console of its own unless told otherwise -
# the console watcher caught ffmpeg windows flashing from the Subtitle maker
# and the Qwen jobs. Same patch as whisper_worker.py and paddle_worker.py.
try:
    import subprocess as _sp
    if os.name == "nt":
        _orig_popen = _sp.Popen.__init__

        def _hidden_popen(self, *a, **kw):
            kw["creationflags"] = (kw.get("creationflags") or 0) | 0x08000000
            if not kw.get("startupinfo"):
                si = _sp.STARTUPINFO()
                si.dwFlags |= _sp.STARTF_USESHOWWINDOW
                si.wShowWindow = 0                       # SW_HIDE
                kw["startupinfo"] = si
            return _orig_popen(self, *a, **kw)
        _sp.Popen.__init__ = _hidden_popen               # type: ignore[method-assign]
except Exception:                                        # noqa: BLE001
    pass

WHISPER_MODEL = "large-v3"
LLM_REPO = "jncraton/Qwen2.5-7B-Instruct-ct2-int8"
OCR_FPS = 2
UTT_GAP_S = 0.6
UTT_MAX_S = 12.0
HALLU = re.compile(r"thank(s| you) for (watching|your view)|translated by|subtitles? by|"
                   r"please subscribe|amara\.org|like and subscribe", re.I)
# ANY LANGUAGE, NOT A LIST OF THIRTEEN. The table this replaced named the
# languages it knew, cut every other code to its first two letters - Swedish
# "swe" became Swahili "sw", Indonesian "ind" became "in" - and gave nothing
# but Japanese, Korean and Chinese a reading of the picture. The plan is now
# worked out per file from three sources: nuarr's ISO 639 table (the codes the
# library's tracks carry), Whisper's own language list, and PaddleOCR's
# script families. A language that first appears in the library tomorrow is
# handled the same way with nothing to add here.
_P_LATIN = {"af", "az", "bs", "cs", "cy", "da", "de", "es", "et", "fr", "ga", "hr", "hu", "id",
            "is", "it", "ku", "la", "lt", "lv", "mi", "ms", "mt", "nl", "no", "oc", "pl", "pt",
            "ro", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi", "fi", "eu", "gl", "lb",
            "rm", "ca", "qu"}
_P_CYRILLIC = {"ru", "be", "uk", "bg", "mn", "kk", "ky", "tg", "mk", "tt", "ba"}
_P_ARABIC = {"ar", "fa", "ug", "ur", "ps", "sd"}
_P_DEVANAGARI = {"hi", "mr", "ne", "sa"}
_SCRIPT = {
    "japan": r"[\u3040-\u30ff\u4e00-\u9fff]",
    "korean": r"[\uac00-\ud7af]",
    "ch": r"[\u4e00-\u9fff]",
    "latin": r"[A-Za-z\u00c0-\u024f]{2,}",
    "cyrillic": r"[\u0400-\u04ff]",
    "arabic": r"[\u0600-\u06ff]",
    "devanagari": r"[\u0900-\u097f]",
    "th": r"[\u0e00-\u0e7f]",
    "el": r"[\u0370-\u03ff]",
}


def _iso_table() -> list:
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_iso639.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                            # noqa: BLE001
        return []


def lang_plan(code: str) -> dict:
    """What can be done for audio in this language.
    -> {code, a2, name, whisper, ocr, script}; whisper None means Whisper is
    left to detect it, ocr None means the picture is not read."""
    c = str(code or "").strip().lower()
    table = _iso_table()
    try:
        from app import langkey
        k = langkey.key(c)
    except Exception:                                            # noqa: BLE001
        k = c
    row = next((r for r in table if r.get("c") == k or r.get("c") == c), None) or {}
    a2 = row.get("a2") or (c if len(c) == 2 else "")
    if not a2:
        for r in table:                       # a 639-2/B code under another row
            if r.get("a2") and (r.get("c") == k):
                a2 = r["a2"]; break
    a2 = {"nb": "no", "nn": "no", "jp": "ja", "iw": "he", "in": "id"}.get(a2, a2)
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES as W
    except Exception:                                            # noqa: BLE001
        W = ()
    whisper = a2 if a2 in W else None
    if a2 == "ja":
        ocr = "japan"
    elif a2 == "ko":
        ocr = "korean"
    elif a2 == "zh":
        ocr = "ch"
    elif a2 in ("th", "el"):
        ocr = a2
    elif a2 in _P_LATIN or a2 in _P_CYRILLIC or a2 in _P_ARABIC or a2 in _P_DEVANAGARI:
        ocr = a2
    else:
        ocr = None                            # English needs no reading; others have no model
    fam = (ocr if ocr in ("japan", "korean", "ch", "th", "el") else
           "latin" if a2 in _P_LATIN else "cyrillic" if a2 in _P_CYRILLIC else
           "arabic" if a2 in _P_ARABIC else "devanagari" if a2 in _P_DEVANAGARI else "")
    return {"code": c, "a2": a2, "name": row.get("n") or c or "unknown", "whisper": whisper,
            "ocr": ocr, "script": _SCRIPT.get(fam, "")}


def say(**k):
    # ASCII on the wire: the pipe is the console code page under the service,
    # and a Japanese line in a test result raised UnicodeEncodeError there.
    print(json.dumps(k, ensure_ascii=True), flush=True)


def ts(t):
    t = max(0.0, t); h, r = divmod(t, 3600); m, s = divmod(r, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:
        s, ms = int(s) + 1, 0
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def sim(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


# ------------------------------------------------- already burned in ------
# A FILE THAT ALREADY SPEAKS ENGLISH ON SCREEN gets nothing. Case Closed OVA 09
# carried the fansubber's English burned into the picture, nuarr's picture
# check had called it "none", and a second English track went on top of the
# first - two sets of subtitles, one of them early. So this asks the picture
# itself before any listening: 24 frames across the file, the bottom of each
# read in English. Subtitles are only up while someone talks, so a fifth of
# them carrying a sentence (OVA 09: 5 of 16) already means it is done.
def burned_english(src, ffmpeg, duration, frames=24):
    import numpy as np
    from paddleocr import PaddleOCR
    kw = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
              use_textline_orientation=False, lang="en", device="gpu")
    try:
        ocr = PaddleOCR(enable_mkldnn=False, **kw)
    except TypeError:
        ocr = PaddleOCR(**kw)
    W, H = 1280, 720
    hits = seen = 0
    for k in range(frames):
        t = duration * (0.08 + 0.84 * k / max(1, frames - 1))
        raw = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                              "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True).stdout
        if len(raw) < W * H * 3:
            continue
        seen += 1
        pic = np.frombuffer(raw[:W * H * 3], np.uint8).reshape(H, W, 3)[int(H * 0.62):, :, ::-1].copy()
        words = 0
        for r in ocr.predict(pic):
            for txt, sc in zip(r.get("rec_texts") or [], r.get("rec_scores") or []):
                if sc >= 0.85:
                    words += len(re.findall(r"[A-Za-z]{2,}", txt))
        hits += words >= 3
    del ocr
    gc.collect()
    return hits, seen


# ------------------------------------------------------------ speech ------
def speech(src, wlang, model_dir, ffmpeg):
    import numpy as np
    from faster_whisper import WhisperModel
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    say(stage="reading the audio", pct=2)
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", src, "-map", "0:a:0", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
    audio = np.frombuffer(raw, np.float32)
    if not len(audio):
        return []
    chunks = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=250,
                                                     speech_pad_ms=60, threshold=0.5))
    utt = []
    for c in chunks:
        s, e = c["start"] / 16000, c["end"] / 16000
        if utt and s - utt[-1][1] < UTT_GAP_S and e - utt[-1][0] < UTT_MAX_S:
            utt[-1][1] = e
        else:
            utt.append([s, e])
    say(stage=f"loading Whisper {WHISPER_MODEL}", pct=6, note=f"{len(utt)} stretches of speech")
    try:
        m = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16", download_root=model_dir)
    except Exception:                                            # noqa: BLE001
        m = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=model_dir)
    task = "transcribe" if wlang == "en" else "translate"
    out = []
    for i, (s, e) in enumerate(utt):
        a, b = int(max(0, s - 0.15) * 16000), int((e + 0.15) * 16000)
        segs, _ = m.transcribe(audio[a:b], language=wlang, task=task, beam_size=5,
                               condition_on_previous_text=False, vad_filter=False)
        segs = list(segs)
        text = " ".join(x.text.strip() for x in segs).strip()
        if (not text or HALLU.search(text)
                or max((x.no_speech_prob for x in segs), default=1) > 0.85
                or min((x.avg_logprob for x in segs), default=-9) < -1.3):
            continue
        out.append([s, e, text])
        if i % 10 == 0:
            say(stage="listening", pct=8 + 37 * (i + 1) / max(1, len(utt)),
                note=f"{i + 1} of {len(utt)} stretches")
    del m
    gc.collect()
    return out


# ----------------------------------------------------------- picture ------
def picture(src, plang, script, ffmpeg, duration):
    import numpy as np
    from paddleocr import PaddleOCR
    say(stage="loading the OCR", pct=46)
    kw = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
              use_textline_orientation=False, lang=plang, device="gpu")
    try:
        ocr = PaddleOCR(enable_mkldnn=False, **kw)
    except TypeError:
        ocr = PaddleOCR(**kw)
    W, H = 1280, 720
    rx = re.compile(script)
    p = subprocess.Popen([ffmpeg, "-v", "error", "-i", src, "-vf", f"fps={OCR_FPS},scale={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    frames = []
    total = max(1, int(duration * OCR_FPS))
    while True:
        buf = p.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            break
        pic = np.frombuffer(buf, np.uint8).reshape(H, W, 3)[:, :, ::-1].copy()
        got = []
        for r in ocr.predict(pic):
            texts, scores = r.get("rec_texts") or [], r.get("rec_scores") or []
            polys = r.get("rec_polys")
            if polys is None:
                polys = r.get("dt_polys")
            for i, t in enumerate(texts):
                t = t.strip()
                if (i < len(scores) and float(scores[i]) >= 0.85 and len(t) >= 2 and rx.search(t)):
                    y = (float(np.asarray(polys[i])[:, 1].mean()) / H
                         if polys is not None and i < len(polys) else 0.5)
                    got.append((t, y))
        frames.append(got)
        if len(frames) % 240 == 0:
            say(stage="reading the picture", pct=47 + 30 * len(frames) / total,
                note=f"{len(frames) // OCR_FPS // 60} of {int(duration // 60)} min")
    del ocr
    gc.collect()
    step = 1.0 / OCR_FPS
    # credits: a stretch where most frames carry five or more lines
    dense = [len(f) >= 5 for f in frames]
    credits = [False] * len(frames)
    win = int(20 * OCR_FPS)
    for i in range(0, max(0, len(frames) - win)):
        if sum(dense[i:i + win]) >= win * 0.5:
            for j in range(i, i + win):
                credits[j] = True
    # one event per text while it stays on screen
    open_ev, ev = {}, []
    for k, got in enumerate(frames):
        if credits[k]:
            continue
        seen = set()
        for t, y in got:
            seen.add(t)
            e = open_ev.get(t)
            if e and k - e["last"] <= 2:
                e["last"] = k; e["n"] += 1
            else:
                if e:
                    ev.append(e)
                open_ev[t] = {"first": k, "last": k, "text": t, "y": y, "n": 1}
        for t in [t for t, e in open_ev.items() if t not in seen and k - e["last"] > 2]:
            ev.append(open_ev.pop(t))
    ev += open_ev.values()
    ev.sort(key=lambda e: e["first"])
    # OCR near-misses of one line (だいじょうぶ / だいじょうぷ) are one line:
    # the version seen on most frames wins
    merged = []
    for e in ev:
        hit = next((m for m in merged if e["first"] <= m["last"] + 2 and sim(e["text"], m["text"]) >= 0.6
                    and abs(e["y"] - m["y"]) < 0.12), None)
        if hit:
            if e["n"] > hit["n"]:
                hit["text"] = e["text"]
            hit["n"] += e["n"]; hit["last"] = max(hit["last"], e["last"]); hit["first"] = min(hit["first"], e["first"])
        else:
            merged.append(dict(e))
    merged = [m for m in merged if m["n"] >= 2 or len(m["text"]) >= 4]
    # lines that come and go together, one above the other, are one bubble
    bubbles = []
    for m in sorted(merged, key=lambda m: (m["first"], m["y"])):
        b = next((b for b in bubbles if abs(b["first"] - m["first"]) <= 1 and abs(b["last"] - m["last"]) <= 1
                  and abs(b["ys"][-1] - m["y"]) <= 0.25), None)
        if b:
            b["lines"].append(m["text"]); b["ys"].append(m["y"]); b["last"] = max(b["last"], m["last"])
        else:
            bubbles.append({"first": m["first"], "last": m["last"], "lines": [m["text"]], "ys": [m["y"]]})
    # A LABEL IS NOT DIALOGUE. Text printed on a character or a prop (the
    # desiccant packet's 乾燥剤 in Sumikko) comes back every time it is on
    # screen, and each return was a fresh subtitle. Speech is rarely the
    # same three times in two minutes; a label always is. Such a run is
    # shown once, at its first appearance.
    bubbles.sort(key=lambda b: b["first"])
    keep = []
    for i, b in enumerate(bubbles):
        t = "".join(b["lines"])
        near = [o for o in bubbles if abs(o["first"] - b["first"]) <= 120 * OCR_FPS
                and sim("".join(o["lines"]), t) >= 0.6]
        if len(near) >= 3 and any(o["first"] < b["first"] for o in near):
            continue
        keep.append(b)
    bubbles = keep
    joiner = " " if plang == "korean" else ""
    return [[b["first"] * step, (b["last"] + 1) * step + 0.25, joiner.join(b["lines"])] for b in bubbles]


# -------------------------------------------------------- translation ------
def translate(lines, title, llm_dir, src_name="Japanese"):
    import ctranslate2
    import tokenizers
    from huggingface_hub import snapshot_download
    say(stage="loading the translator", pct=78, note="Qwen2.5-7B-Instruct")
    if not os.path.exists(os.path.join(llm_dir, "model.bin")):
        snapshot_download(LLM_REPO, local_dir=llm_dir)
    tok = tokenizers.Tokenizer.from_file(os.path.join(llm_dir, "tokenizer.json"))
    try:
        g = ctranslate2.Generator(llm_dir, device="cuda", compute_type="int8_float16")
    except Exception:                                            # noqa: BLE001
        g = ctranslate2.Generator(llm_dir, device="cpu", compute_type="int8")
    # ENGLISH, SAID TWICE. "into natural English subtitles" alone let Qwen
    # answer in Chinese when the film title gave it little to go on - the
    # test on the Qwen page came back 天上的国家？ for くものうえのくに？.
    kana = ("Children's films write mostly in hiragana; read it in context. "
            if src_name.lower().startswith("japanese") else "")
    sysm = (f"You are a {src_name}-to-English subtitle translator. Translate each numbered "
            f"{src_name} line into natural ENGLISH. {kana}Keep each line short. Reply in English "
            f"only - never in {src_name} or any other language - with exactly the same numbered "
            f"lines and nothing else.")
    out = [None] * len(lines)
    B = 40
    for i in range(0, len(lines), B):
        part = lines[i:i + B]
        body = "\n".join(f"{n + 1}. {t}" for n, t in enumerate(part))
        prompt = (f"<|im_start|>system\n{sysm}<|im_end|>\n<|im_start|>user\nFilm: {title}\n\n"
                  f"{body}<|im_end|>\n<|im_start|>assistant\n")
        r = g.generate_batch([tok.encode(prompt).tokens], max_length=40 * len(part) + 64,
                             sampling_topk=1, include_prompt_in_result=False, end_token="<|im_end|>")
        for ln in tok.decode(r[0].sequences_ids[0]).splitlines():
            m = re.match(r"\s*(\d+)[.)]\s*(.+)", ln)
            if m and 1 <= int(m.group(1)) <= len(part):
                out[i + int(m.group(1)) - 1] = m.group(2).strip()
        say(stage="translating the on-screen text", pct=78 + 17 * min(1, (i + B) / len(lines)),
            note=f"{min(len(lines), i + B)} of {len(lines)} lines")
    # A LINE THAT DID NOT COME BACK IN ENGLISH is asked again on its own, more
    # bluntly; if it still is not English it is dropped rather than shown.
    def english(t):
        # Latin letters, and none of the scripts English is not written in.
        return (bool(t) and bool(re.search(r"[A-Za-z]", t)) and not re.search(
            r"[\u0370-\u03ff\u0400-\u04ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f"
            r"\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]", t))
    for k, t in enumerate(out):
        if english(t):
            continue
        prompt = (f"<|im_start|>system\nTranslate the {src_name} text into English. Reply with the "
                  "English translation only.<|im_end|>\n<|im_start|>user\n"
                  f"{lines[k]}<|im_end|>\n<|im_start|>assistant\n")
        r = g.generate_batch([tok.encode(prompt).tokens], max_length=64, sampling_topk=1,
                             include_prompt_in_result=False, end_token="<|im_end|>")
        t2 = tok.decode(r[0].sequences_ids[0]).strip().splitlines()
        out[k] = t2[0].strip() if t2 and english(t2[0]) else None
    del g
    gc.collect()
    return out


# ------------------------------------------------- from a subtitle track --
# THE TEXT IS ALREADY THERE. A file whose only subtitles are Spanish, or
# Chinese, over Japanese audio is a raw to an English speaker - but its
# subtitles are timed by a person, and clean text. Translating them keeps every
# cue's timing exactly and starts from what the subtitler wrote rather than
# from what Whisper heard, so it is the maker's first choice whenever a
# text track in another language exists.
def _srt_parse(text):
    cues = []
    for b in re.split(r"\r?\n\s*\r?\n", text.strip()):
        L = [x for x in b.splitlines() if x.strip()]
        k = next((i for i, x in enumerate(L) if "-->" in x), None)
        if k is None:
            continue
        a, z = [x.strip() for x in L[k].split("-->")[:2]]
        body = " ".join(re.sub(r"<[^>]+>|\{[^}]*\}", "", x).strip() for x in L[k + 1:]).strip()
        if body:
            cues.append([a, z.split(" ")[0], body])
    return cues


def from_subs(src, spec, src_name, ffmpeg, out):
    from app import qwenlib
    say(stage="reading the subtitle track", pct=3)
    if spec.startswith("stream:"):
        cmd = [ffmpeg, "-v", "error", "-i", src, "-map", "0:" + spec.split(":", 1)[1], "-f", "srt", "-"]
    else:
        cmd = [ffmpeg, "-v", "error", "-i", spec.split(":", 1)[1], "-f", "srt", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout.decode("utf-8", "replace")
    cues = _srt_parse(raw)
    if not cues:
        say(error="the subtitle track could not be read")
        return
    say(stage="loading the translator", pct=10, note=f"{len(cues)} lines")
    sysm = (f"You are a {src_name}-to-English subtitle translator. Translate each numbered "
            f"{src_name} subtitle line into natural, short ENGLISH subtitles. Keep names as they are. "
            f"Reply in English only, with exactly the same numbered lines and nothing else.")
    words = qwenlib.numbered(sysm, [c[2] for c in cues], batch=40, check=qwenlib.english,
                             on_batch=lambda d, t: say(stage="translating the subtitles",
                                                       pct=12 + 83 * d / t, note=f"{d} of {t} lines"))
    keep = [(a, z, w) for (a, z, _t), w in zip(cues, words) if w]
    say(stage="writing", pct=96)
    tmp = out + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n\n".join(f"{i + 1}\n{a} --> {z}\n{w}" for i, (a, z, w) in enumerate(keep)) + "\n")
    os.replace(tmp, out)
    say(done=True, narration=0, onscreen=0, translated=len(keep), dropped=len(cues) - len(keep))


# ---------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lang", default="jpn")
    ap.add_argument("--title", default="")
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--data", required=True)
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--force", action="store_true", help="make them even if English is burned in")
    ap.add_argument("--burned-only", action="store_true", help="only answer the burned-in check")
    ap.add_argument("--llm-test", action="store_true", help="translate a few sample lines")
    ap.add_argument("--llm-fetch", action="store_true", help="download the translation model")
    ap.add_argument("--from-subs", default="", help="stream:N or file:path - translate this track")
    ap.add_argument("--sub-lang", default="", help="the language of --from-subs")
    a = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import audiolang
    audiolang._add_cuda_dirs()
    if a.from_subs:
        try:
            from_subs(a.src, a.from_subs, lang_plan(a.sub_lang)["name"], a.ffmpeg, a.out)
        except Exception as e:                                   # noqa: BLE001
            say(error=f"{type(e).__name__}: {e}"[:300])
        return
    if a.llm_test or a.llm_fetch:
        llm_dir = os.path.join(a.data, "llm", "qwen2.5-7b-instruct-ct2-int8")
        try:
            if a.llm_fetch:
                from huggingface_hub import snapshot_download
                snapshot_download(LLM_REPO, local_dir=llm_dir)
                say(llm={"fetched": True})
                return
            # Hiragana on purpose: it is what an ordinary translator fails on.
            src = ["くものうえのくに？", "あめがやまないと", "くにがなくなっちゃう？",
                   "みずのしんでんにいこう！", "だまっていてごめんなさい……"]
            t0 = time.time()
            got = translate(src, "a children's anime film", llm_dir)
            say(llm={"pairs": [[x, y or ""] for x, y in zip(src, got)],
                     "secs": round(time.time() - t0, 1)})
        except Exception as e:                                   # noqa: BLE001
            say(error=f"{type(e).__name__}: {e}"[:300])
        return
    plan = lang_plan(a.lang)
    wlang, plang, script = plan["whisper"], plan["ocr"] or "", plan["script"]
    t0 = time.time()
    try:
        if a.burned_only:
            hits, seen = burned_english(a.src, a.ffmpeg, a.duration)
            say(burned=[hits, seen])
            return
        if not a.force and a.duration > 60:
            say(stage="checking for subtitles already in the picture", pct=1)
            # IN A PROCESS OF ITS OWN. PaddleOCR loads its own CUDA libraries,
            # and once they are in this process Whisper's engine cannot load
            # ("WinError 127 - the specified procedure could not be found").
            # The picture is read again later, after Whisper is done with the
            # card - so only this early look needs a separate process.
            r = subprocess.run([sys.executable, "-m", "app.submake_worker", "--src", a.src,
                                "--out", a.out, "--lang", a.lang, "--duration", str(a.duration),
                                "--data", a.data, "--ffmpeg", a.ffmpeg, "--burned-only"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            hits = seen = 0
            for ln in (r.stdout or "").splitlines():
                try:
                    d = json.loads(ln)
                except Exception:                                # noqa: BLE001
                    continue
                if "burned" in d:
                    hits, seen = d["burned"]
            if seen and hits >= max(4, seen * 0.2):
                say(error=f"English is already burned into the picture ({hits} of {seen} sampled "
                          f"frames carry it) - nothing made. Use 'make anyway' to override.")
                return
        talk = speech(a.src, wlang, os.path.join(a.data, "whisper"), a.ffmpeg)
        shown, words = [], []
        if plang and not a.no_ocr:
            shown = picture(a.src, plang, script, a.ffmpeg, a.duration or (talk[-1][1] if talk else 3600))
            if shown:
                words = translate([s[2] for s in shown], a.title,
                                  os.path.join(a.data, "llm", "qwen2.5-7b-instruct-ct2-int8"),
                                  plan["name"])
        say(stage="writing", pct=96)
        bot = []
        for s, e, t in talk:
            # ONE STRETCH OF SPEECH, SEVERAL SENTENCES. An utterance can run
            # to 12 s, and three sentences in one cue is a paragraph on the
            # screen. Long ones are split at sentence ends and the stretch's
            # time shared out by length, so each piece shows while it is said.
            parts = [x.strip() for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()]
            if len(t) <= 84 or len(parts) < 2:
                parts = [t]
            total = sum(len(x) for x in parts)
            at = s
            for x in parts:
                span = (e - s) * len(x) / total
                bot.append([at, max(at + span + 0.3, at + min(8.0, 1.2 + 0.065 * len(x))), x])
                at += span
        bot.sort(key=lambda x: x[0])
        for i in range(len(bot) - 1):
            bot[i][1] = max(min(bot[i][1], bot[i + 1][0] - 0.05), bot[i][0] + 0.8)
        top = [[s, max(e, s + 1.5), "{\\an8}" + w] for (s, e, _j), w in zip(shown, words) if w]
        ev = sorted(bot + top, key=lambda x: x[0])
        if not ev:
            say(error="nothing was spoken or written that could be turned into a subtitle")
            return
        tmp = a.out + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n\n".join(f"{i + 1}\n{ts(s)} --> {ts(e)}\n{t}" for i, (s, e, t) in enumerate(ev)) + "\n")
        os.replace(tmp, a.out)
        say(done=True, narration=len(bot), onscreen=len(top), seconds=round(time.time() - t0))
    except Exception as e:                                       # noqa: BLE001
        say(error=f"{type(e).__name__}: {e}"[:300])


if __name__ == "__main__":
    main()
