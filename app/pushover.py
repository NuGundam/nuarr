r"""Pushover: the arrs' imports, on the phone, with the detail the arrs leave out.

WHY NUARR SENDS THESE AND NOT THE ARRS. Sonarr and Radarr each have a
Pushover connection of their own, and what they send is the file name. By the
time one of their imports is done, nuarr knows more than they do: which disk
of the pool the file was placed on and how full that disk is, the old file's
spec next to the new one when it was an upgrade, the audio languages, the
release's indexer and score. The arrs' webhooks already bring all of that
through this process (webhooks.py) - this module is the last step of that
path, turning one handled import into one message.

ONE MESSAGE PER IMPORT, ONE DIGEST PER PACK. A season pack is twelve webhooks
in ten seconds. Twelve notifications is noise, so messages queue per arr and
series for a quiet window; a single arrival goes out as itself, several go
out as one digest listing every episode. The window also covers a small race:
the arr's webhook can land a moment before nuarr's own placement record for
that file is written, and the disk line is looked up at send time, not at
arrival.

THE KEYS ARE SETTINGS, NOT UI STATE. The app token and user key live in
config.yml (`pushover_token`, `pushover_user`) beside the Plex and Tautulli
credentials; the page shows whether they are set and never sends them back.
What to notify about, and how loud, is kv - togglable without a restart.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time

import httpx

from . import joblog
from .config import SETTINGS
from .db import kv_get, kv_set

API = "https://api.pushover.net/1"
KV_ON = "pushover.on"
KV_OPTS = "pushover.opts"
KV_RECENT = "pushover.recent"
RECENT_KEEP = 30
QUIET_S = 60.0            # a pack lands one file every few seconds - Sugar Apple had a 29 s gap after E01
MSG_LIMIT = 1024          # Pushover's message cap
DEFAULT_OPTS = {"imports": True, "upgrades": True, "digest": True,
                "priority": -1, "sound": "", "link": True}

STATS: dict = {"sent": 0, "failed": 0, "last_sent": 0.0, "last_error": "",
               "pending": 0, "limit": None, "remaining": None, "reset": None}
_PENDING: dict[tuple, dict] = {}       # (arr, series) -> {"at", "items": [...]}
# ONE IMPORT, TWO WEBHOOKS. Sonarr's nuarr connection has both On File Import
# and On Import Complete ticked (webhooks.py wants both), and both arrive as
# eventType "Download" carrying the same file - so My Home Hero S01E02, one
# file, went out as "My Home Hero (2 files)" listing E02 twice. A file seen
# in the last SEEN_S is the same import again, not a second one.
_SEEN: dict[tuple, float] = {}
SEEN_S = 600.0
_FLUSHER: asyncio.Task | None = None


# ------------------------------------------------------------ settings -----
def configured() -> bool:
    return bool(SETTINGS.pushover_token and SETTINGS.pushover_user)


def enabled() -> bool:
    return str(kv_get(KV_ON) or "0") == "1" and configured()


def set_enabled(on: bool) -> None:
    kv_set(KV_ON, "1" if on else "0")


def opts() -> dict:
    o = dict(DEFAULT_OPTS)
    try:
        o.update(json.loads(kv_get(KV_OPTS) or "{}"))
    except Exception:                                            # noqa: BLE001
        pass
    return o


def set_opts(changes: dict) -> dict:
    o = opts()
    for k in DEFAULT_OPTS:
        if k in changes:
            o[k] = int(changes[k]) if k == "priority" else (
                str(changes[k] or "") if k == "sound" else bool(changes[k]))
    o["priority"] = max(-2, min(1, int(o.get("priority", -1))))
    kv_set(KV_OPTS, json.dumps(o))
    return o


def _recent() -> list:
    try:
        return json.loads(kv_get(KV_RECENT) or "[]")
    except Exception:                                            # noqa: BLE001
        return []


def _remember(row: dict) -> None:
    # Each row keeps what was actually sent (title and body), so the page can
    # open a row to show it and send it again. The id is the send time in ns,
    # which two rows cannot share.
    row.setdefault("id", str(time.time_ns()))
    try:
        kv_set(KV_RECENT, json.dumps(([row] + _recent())[:RECENT_KEEP]))
    except Exception:                                            # noqa: BLE001
        pass


def _rid(r: dict) -> str:
    """Rows from before ids existed are addressed by their send time."""
    return str(r.get("id") or r.get("at"))


def forget(ids: list) -> int:
    """Drop rows from the list. Nothing on the phone changes."""
    ids = {str(i) for i in ids or []}
    rows = _recent()
    keep = [r for r in rows if _rid(r) not in ids]
    kv_set(KV_RECENT, json.dumps(keep))
    return len(rows) - len(keep)


async def resend(ids: list) -> dict:
    """Send these rows' messages again, exactly as they went out. A row from
    before the body was kept has nothing to resend and is reported as such."""
    ids = {str(i) for i in ids or []}
    sent = failed = skipped = 0
    for r in [r for r in _recent() if _rid(r) in ids]:
        if not r.get("msg"):
            skipped += 1
            continue
        ok, err = await send(r.get("ptitle") or r.get("title") or "nuarr", r["msg"],
                             url=_link())
        _remember({**{k: v for k, v in r.items() if k not in ("id", "at", "ok", "error")},
                   "at": time.time(), "ok": ok, "error": err, "resent": True})
        sent += int(ok)
        failed += int(not ok)
    return {"sent": sent, "failed": failed, "skipped": skipped}


# ------------------------------------------------------------ sending ------
async def send(title: str, message: str, *, priority: int | None = None,
               sound: str = "", url: str = "", url_title: str = "",
               token: str = "", user: str = "") -> tuple[bool, str]:
    """One message to Pushover. HTML is on; the caller escapes its own text."""
    token = token or SETTINGS.pushover_token or ""
    user = user or SETTINGS.pushover_user or ""
    if not (token and user):
        return False, "no token or user key"
    o = opts()
    data = {"token": token, "user": user, "title": title[:250],
            "message": message[:MSG_LIMIT], "html": 1,
            "priority": o["priority"] if priority is None else priority,
            "timestamp": int(time.time())}
    snd = sound or o.get("sound") or ""
    if snd:
        data["sound"] = snd
    if url:
        data["url"] = url
        data["url_title"] = url_title or "open nuarr"
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(API + "/messages.json", data=data)
        for k, h in (("limit", "X-Limit-App-Limit"), ("remaining", "X-Limit-App-Remaining"),
                     ("reset", "X-Limit-App-Reset")):
            if r.headers.get(h):
                STATS[k] = int(r.headers[h])
        body = r.json() if r.content else {}
        if r.status_code == 200 and body.get("status") == 1:
            STATS["sent"] += 1
            STATS["last_sent"] = time.time()
            STATS["last_error"] = ""
            return True, ""
        err = "; ".join(body.get("errors") or []) or f"HTTP {r.status_code}"
    except Exception as e:                                       # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:160]
    STATS["failed"] += 1
    STATS["last_error"] = err
    return False, err


async def test(token: str = "", user: str = "") -> tuple[bool, str]:
    """Validate a token/user pair with Pushover, then send one test message."""
    token = token or SETTINGS.pushover_token or ""
    user = user or SETTINGS.pushover_user or ""
    if not (token and user):
        return False, "both the app token and the user key are needed"
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(API + "/users/validate.json", data={"token": token, "user": user})
        b = r.json() if r.content else {}
        if b.get("status") != 1:
            return False, "; ".join(b.get("errors") or []) or f"Pushover answered {r.status_code}"
        devs = ", ".join(b.get("devices") or []) or "no devices"
    except Exception as e:                                       # noqa: BLE001
        return False, f"could not reach Pushover: {type(e).__name__}: {e}"[:160]
    ok, err = await send("nuarr", "<b>Test message.</b> Imports from Sonarr and Radarr "
                         "will arrive like this, with the disk they landed on.",
                         token=token, user=user, url=_link(), url_title="open nuarr")
    if not ok:
        return False, err
    _remember({"at": time.time(), "arr": "", "title": "test message", "detail": f"delivered to {devs}", "ok": True})
    return True, f"delivered to {devs}"


def _link() -> str:
    if not opts().get("link", True):
        return ""
    try:
        from .webhooks import default_base_url
        return default_base_url() or ""
    except Exception:                                            # noqa: BLE001
        return ""


# ------------------------------------------------------- the imports -------
def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


# COLOUR IN THE MESSAGE. Pushover's title is plain text, so the arr's colour
# goes on the first line of the body instead - the same Sonarr blue / Radarr
# amber the page uses. Each labelled line gets its own colour so the eye can
# find "Placed" without reading the four above it.
ARR_COL = {"sonarr": "#35c5f4", "radarr": "#ffc230"}
LABEL_COL = {"Upgrade": "#a371f7", "Release": "#f778ba",
             "Placed": "#56d4dd", "Client": "#db6d28"}
FROM_COL, TO_COL = "#f0883e", "#3fb950"     # the old file, the new one


def _lab(name: str) -> str:
    return f'<b><font color="{LABEL_COL.get(name, "#c9d1d9")}">{name}:</font></b> '


def _arr_tag(name: str, kind: str) -> str:
    c = ARR_COL.get((kind or "").lower())
    return f'<font color="{c}"><b>{_esc(name)}</b></font>' if c else f"<b>{_esc(name)}</b>"


def _plain(s: str) -> str:
    """The recent list is text, not HTML: tags off, entities back."""
    return html.unescape(re.sub(r"<[^>]+>", "", str(s or "")))


def _obj(x) -> dict:
    return x if isinstance(x, dict) else {}


# The naming format's audio tag: "[JA]", "[JA+EN]", "[EN+JA]".
_TAG = re.compile(r"\[([A-Z]{2}(?:\+[A-Z]{2})*)\]")
_TWO = {"EN": "eng", "JA": "jpn", "KO": "kor", "ZH": "zho", "ES": "spa", "FR": "fra",
        "DE": "deu", "IT": "ita", "PT": "por", "RU": "rus", "HI": "hin", "TH": "tha"}


def _file_langs(path: str) -> set:
    """Audio languages of a file as nuarr knows them: its own probe first,
    then the [JA+EN] tag in the name. Empty when neither says."""
    try:
        from .db import cursor
        with cursor() as cur:
            r = cur.execute("SELECT audio_langs FROM files WHERE path=? COLLATE NOCASE "
                            "ORDER BY id DESC LIMIT 1", (path,)).fetchone()
        if r and r["audio_langs"]:
            return {x.strip() for x in str(r["audio_langs"]).split(",") if x.strip()}
    except Exception:                                            # noqa: BLE001
        pass
    tags = _TAG.findall(os.path.basename(path or ""))
    if tags:
        return {_TWO.get(t, t.lower()) for t in tags[-1].split("+")}
    return set()


def _placed(name: str) -> dict:
    """nuarr's own placement record for this file, if its import went through
    the arr-import hook: disk label, fill %, free GB, seconds."""
    if not name:
        return {}
    try:
        from .arrimport import _recent_load
        for r in _recent_load():
            if (r.get("file") or "").lower() == name.lower():
                return r
    except Exception:                                            # noqa: BLE001
        pass
    return {}


def _release_line(body: dict) -> str:
    rel = _obj(body.get("release"))
    bits = []
    t = rel.get("releaseTitle") or ""
    if t:
        bits.append(_esc(t))
    grp = rel.get("releaseGroup")
    if grp and grp not in t:
        bits.append(_esc(grp))
    if rel.get("indexer"):
        bits.append(_esc(rel["indexer"]))
    sc = rel.get("customFormatScore")
    if sc is not None:
        try:
            bits.append(f"score {int(sc):+,}")
        except (TypeError, ValueError):
            pass
    return " · ".join(bits)


def build(cfg, body: dict, files: list, detail: str, upgrade: bool) -> dict | None:
    """One import -> the pieces of a message. Returns None when this kind of
    event is switched off."""
    from .webhooks import _describe, _media_label
    o = opts()
    if upgrade and not o.get("upgrades", True):
        return None
    if not upgrade and not o.get("imports", True):
        return None
    kind = cfg.kind
    label = _media_label(body, kind)
    # GAINED ENGLISH AUDIO. On a foreign show the reason an upgrade matters is
    # usually this - jpn-only becoming jpn+eng - and webhooks' detail already
    # says it ("audio jpn -> jpn+eng (gained eng)"). Lifted out so the title
    # and the digest can lead with it instead of burying it in a spec line.
    m = re.search(r"\(gained ([^)]+)\)", detail or "") if upgrade else None
    eng = bool(m and "eng" in m.group(1).split("+"))
    if upgrade and not m:
        # THE WEBHOOK OFTEN CARRIES NO LANGUAGES. KAIJU GIRL CARAMELISE S01E12
        # went [JA] -> [JA+EN] and the phone was never told: Sonarr's payload
        # had no audioLanguages on either file, so there was nothing to
        # compare. nuarr knows anyway - its own probe of the old file, and the
        # language tag the naming format writes into both names.
        old = [_obj(d).get("path") or "" for d in (body.get("deletedFiles") or [])
               if isinstance(d, dict)]
        new = [_obj(f).get("path") or "" for f in files]
        before = set().union(*[_file_langs(p) for p in old if p]) if old else set()
        after = set().union(*[_file_langs(p) for p in new if p]) if new else set()
        eng = bool(before and after and "eng" in after and "eng" not in before)
    series = (_obj(body.get("series")).get("title") if kind == "sonarr"
              else _obj(body.get("movie")).get("title")) or label
    sub = ""
    if kind == "sonarr":
        eps = [e for e in (body.get("episodes") or []) if isinstance(e, dict)]
        if eps and eps[0].get("title"):
            sub = eps[0]["title"]
    f = files[0] if files else {}
    spec = _describe(f) if f else ""
    path = _obj(f).get("path") or _obj(f).get("relativePath") or ""
    lines = []
    if sub:
        lines.append("<i>" + _esc(sub) + "</i>")
    if upgrade and detail and detail != "upgrade":
        # "From <old> → To <new>", the two words coloured, so which side is
        # which does not depend on reading the arrow. Anything after the new
        # file's spec (" · audio jpn -> jpn+eng ...") stays as it was.
        old, sep, new = detail.partition(" -> ")
        if sep:
            lines.append(_lab("Upgrade") + f'<font color="{FROM_COL}"><b>From</b></font> '
                         + _esc(old.strip()) + f' → <font color="{TO_COL}"><b>To</b></font> '
                         + _esc(new.strip().replace(" -> ", " → ")))
        else:
            lines.append(_lab("Upgrade") + _esc(detail))
    elif spec:
        lines.append(_esc(spec))
    rl = _release_line(body)
    if rl:
        lines.append(_lab("Release") + rl)
    pl = _placed(os.path.basename(path))
    if pl:
        s = _lab("Placed") + _esc(pl.get("placed"))
        if pl.get("pct") is not None:
            s += f" at {pl['pct']}%"
        if pl.get("free_gb") is not None:
            s += f" ({pl['free_gb']:,} GB free)"
        if pl.get("seconds") is not None:
            s += f" · {pl['seconds']} s"
        lines.append(s)
    dc = body.get("downloadClient")
    if dc:
        lines.append(_lab("Client") + _esc(dc))
    if eng:
        lines.insert(1 if sub else 0, "<b><font color=\"#2ea44f\">Now has English audio</font></b>")
    return {"arr": cfg.name, "kind": kind, "series": series, "label": label, "sub": sub,
            "spec": spec, "upgrade": upgrade, "lines": lines, "eng": eng,
            "ep": label[len(series):].strip() if label.startswith(series) else label,
            "at": time.time()}


def queue(item: dict | None) -> None:
    """Hold an import for the quiet window, then send it alone or as a digest."""
    global _FLUSHER
    if not item or not enabled():
        return
    now = time.time()
    for k in [k for k, t in _SEEN.items() if now - t > SEEN_S]:
        _SEEN.pop(k, None)
    # THE EPISODE IS THE KEY, NOT THE PAYLOAD. The two webhooks for one import
    # do not agree with each other: Tomb Raider King S01E12 arrived once as an
    # upgrade (On File Import) and once as a plain import (On Import Complete),
    # so a key that included the upgrade flag let both through as "2 files,
    # imported, 1 upgraded". When both turn up, the upgrade is the truer one -
    # it carries the old file - so it replaces a plain import still waiting.
    key = (item["arr"], item["series"].lower())
    seen = (item["arr"], item["label"].lower())
    if seen in _SEEN:
        if item["upgrade"]:
            for i, x in enumerate((_PENDING.get(key) or {}).get("items", [])):
                if x["label"].lower() == seen[1] and not x["upgrade"]:
                    _PENDING[key]["items"][i] = item
        return
    _SEEN[seen] = now
    p = _PENDING.setdefault(key, {"at": 0.0, "items": []})
    p["items"].append(item)
    p["at"] = time.time()
    STATS["pending"] = sum(len(v["items"]) for v in _PENDING.values())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _FLUSHER is None or _FLUSHER.done():
        _FLUSHER = loop.create_task(_flush_loop())


async def _flush_loop() -> None:
    while _PENDING:
        await asyncio.sleep(2.0)
        now = time.time()
        for key in [k for k, v in _PENDING.items() if now - v["at"] >= QUIET_S]:
            items = _PENDING.pop(key)["items"]
            STATS["pending"] = sum(len(v["items"]) for v in _PENDING.values())
            try:
                await _send_group(items)
            except Exception as e:                               # noqa: BLE001
                joblog.log(f"pushover: {type(e).__name__}: {e}", "error")


async def _send_group(items: list) -> None:
    arr = items[0]["arr"]
    if len(items) == 1 or not opts().get("digest", True):
        for it in items:
            title = (f"{arr} · {'upgraded' if it['upgrade'] else 'imported'} · {it['label']}"
                     + (" · English audio" if it.get("eng") else ""))
            msg = (_arr_tag(arr, it.get("kind", "")) + " · <b>" + _esc(it["label"]) + "</b>\n"
                   + "\n".join(it["lines"]))
            ok, err = await send(title, msg, url=_link())
            _remember({"at": time.time(), "arr": arr, "title": it["label"],
                       "detail": _plain(it["spec"] or (it["lines"][0] if it["lines"] else "")),
                       "eng": int(bool(it.get("eng"))), "ok": ok, "error": err,
                       "kind": "upgraded" if it["upgrade"] else "imported", "files": 1,
                       "ptitle": title, "msg": msg})
            if not ok:
                joblog.log(f"pushover: could not send {it['label']}: {err}", "warn")
        return
    # A PACK: one message, every episode on its own line, the shared parts once.
    items.sort(key=lambda x: x["ep"])
    series = items[0]["series"]
    ups = sum(1 for x in items if x["upgrade"])
    what = ("upgraded" if ups == len(items) else "imported" if not ups
            else f"imported, {ups} upgraded")
    engs = sum(1 for x in items if x.get("eng"))
    title = f"{arr} · {what} {len(items)} · {series}" + (
        f" · {engs} now English audio" if engs else "")
    head = (_arr_tag(arr, items[0].get("kind", "")) + " · <b>" + _esc(series) + "</b>  —  "
            + f"{len(items)} files")
    disks: dict = {}
    for x in items:
        for ln in x["lines"]:
            if ln.startswith(_lab("Placed")):
                d = html.unescape(ln[len(_lab("Placed")):].split(" at ")[0])
                disks[d] = disks.get(d, 0) + 1
    rows = []
    for x in items:
        r = _esc(x["ep"]) + ("  " + _esc(x["spec"]) if x["spec"] else "")
        if x["upgrade"]:
            r += "  ↑"
        if x.get("eng"):
            r += "  <font color=\"#2ea44f\">+eng audio</font>"
        rows.append(r)
    tail = []
    if disks:
        tail.append(_lab("Placed") + ", ".join(f"{_esc(k)} ×{v}" for k, v in sorted(disks.items())))
    rel = next((ln for x in items for ln in x["lines"] if ln.startswith(_lab("Release"))), "")
    if rel:
        tail.append(rel)
    msg = head + "\n" + "\n".join(rows)
    room = MSG_LIMIT - len("\n".join(tail)) - 4
    if len(msg) > room:
        msg = msg[:room].rsplit("\n", 1)[0] + "\n…"
    msg += "\n" + "\n".join(tail)
    ok, err = await send(title, msg, url=_link())
    _remember({"at": time.time(), "arr": arr, "title": f"{series} ({len(items)} files)",
               "detail": what, "eng": engs, "ok": ok, "error": err,
               "kind": "upgraded" if ups == len(items) else "imported", "files": len(items),
               "ptitle": title, "msg": msg})
    if not ok:
        joblog.log(f"pushover: could not send the {series} digest: {err}", "warn")


def snapshot() -> dict:
    return {"configured": configured(), "token_set": bool(SETTINGS.pushover_token),
            "user_set": bool(SETTINGS.pushover_user), "enabled": enabled(),
            "on": str(kv_get(KV_ON) or "0") == "1", "opts": opts(),
            "stats": dict(STATS), "recent": _recent(), "quiet_s": QUIET_S,
            "link": _link()}
