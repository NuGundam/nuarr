r"""nuarr - Qwen in a child process: load once, ask many times.

ONLY EVER IMPORTED BY A CHILD PROCESS (submake_worker, qwenjudge, qwentools).
Qwen2.5-7B is about 8 GB on the card and native code; the server never loads
it, for the same reason Whisper and PaddleOCR live in children. Everything
that asks Qwen something goes through here, so the prompt format, the
English-only check and the model path are written once.
"""
from __future__ import annotations

import os
import re
import sys

LLM_REPO = "jncraton/Qwen2.5-7B-Instruct-ct2-int8"
_G = None
_TOK = None
NOT_ENGLISH = re.compile(r"[Ͱ-ϿЀ-ӿ؀-ۿऀ-ॿ฀-๿"
                         r"぀-ヿ一-鿿가-힯]")


def model_dir() -> str:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import DATA_DIR
    return os.path.join(str(DATA_DIR), "llm", "qwen2.5-7b-instruct-ct2-int8")


def load():
    """The generator and tokenizer, loaded once per process. Raises when the
    model is not downloaded - the Qwen page has the Download button."""
    global _G, _TOK
    if _G is not None:
        return _G, _TOK
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import audiolang
    audiolang._add_cuda_dirs()
    import ctranslate2
    import tokenizers
    d = model_dir()
    if not os.path.exists(os.path.join(d, "model.bin")):
        raise RuntimeError("Qwen is not downloaded - see Settings, Qwen")
    _TOK = tokenizers.Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
    try:
        _G = ctranslate2.Generator(d, device="cuda", compute_type="int8_float16")
    except Exception:                                            # noqa: BLE001
        _G = ctranslate2.Generator(d, device="cpu", compute_type="int8")
    return _G, _TOK


def ask_many(system: str, users: list, max_len: int = 200) -> list:
    """One answer per user message, batched eight at a time."""
    g, tok = load()
    out = []
    for i in range(0, len(users), 8):
        part = users[i:i + 8]
        prompts = [tok.encode(f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{u}"
                              f"<|im_end|>\n<|im_start|>assistant\n").tokens for u in part]
        rs = g.generate_batch(prompts, max_length=max_len, sampling_topk=1,
                              include_prompt_in_result=False, end_token="<|im_end|>")
        out += [tok.decode(r.sequences_ids[0]).strip() for r in rs]
    return out


def ask(system: str, user: str, max_len: int = 200) -> str:
    return ask_many(system, [user], max_len)[0]


def numbered(system: str, lines: list, context: str = "", batch: int = 40,
             max_per_line: int = 40, check=None, on_batch=None) -> list:
    """Send lines as a numbered list and read back the same numbering.
    -> one answer (or None) per line. `check(answer)` can reject an answer,
    which is then asked again on its own once."""
    out = [None] * len(lines)
    for i in range(0, len(lines), batch):
        part = lines[i:i + batch]
        body = "\n".join(f"{n + 1}. {t}" for n, t in enumerate(part))
        reply = ask(system, (context + "\n\n" if context else "") + body,
                    max_len=max_per_line * len(part) + 64)
        for ln in reply.splitlines():
            m = re.match(r"\s*(\d+)[.)]\s*(.*)", ln)
            if m and 1 <= int(m.group(1)) <= len(part):
                out[i + int(m.group(1)) - 1] = m.group(2).strip()
        if on_batch:
            on_batch(min(len(lines), i + batch), len(lines))
    if check:
        for k, t in enumerate(out):
            if t is not None and check(t):
                continue
            again = ask(system + " Answer for this single line only, with no number.",
                        lines[k], max_len=max_per_line + 16).strip().splitlines()
            a = again[0].strip() if again else ""
            out[k] = a if a and check(a) else None
    return out


def english(t: str) -> bool:
    return bool(t) and bool(re.search(r"[A-Za-z]", t)) and not NOT_ENGLISH.search(t)
