r"""nuarr - what the subtitle tools learn, per show.

WHY. Whisper, Qwen and the OCR readers are fixed models: nuarr never retrains
them, and the same input gives the same mistakes every time - a character's
name spelt three ways across a season, "rn" read for "m" on every episode of
a Blu-ray. What CAN improve is what they are told and what is done with what
they say. Three things are kept here, per show:

  NAMES. Character and place names, gathered from English subtitles already
  in the show's folder (beside the files or inside them), from your edits,
  and from you. Whisper is prompted with them and Qwen is told to spell
  them that way - the commonest error a made subtitle has is a misspelt name.

  CORRECTIONS. Wrong -> right, applied to text as it is made: OCR's misreads
  after a picture track is read, and Whisper's or Qwen's output in the
  Subtitle maker. They come from Qwen's OCR repair (adopted once the same
  fix has been seen twice - one sighting can be Qwen's own mistake), from
  your edits (adopted at once - you are the authority), and from you.

  YOUR EDITS. Every subtitle the maker writes is kept as it was written. When
  the file on disk later differs - fixed in Subtitle Edit, say - the changed
  words become corrections, and capitalised ones become names.

A "show" is the series folder (or a film's own folder), the same key the Qwen
judge already groups its examples by. The key "*" means every show.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import time

from .db import cursor

ALL = "*"
NAMES_MAX = 60                 # names handed to a model per show
NAMES_REBUILD_S = 30 * 86400   # rebuild a show's names from its subtitles this often
QWEN_ADOPT_N = 2               # a Qwen OCR repair is used once seen this many times
EMBED_FILES = 3                # files read for embedded English tracks per build (each read is the whole file)
TEXT_CODECS = ("subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text")
_READY = False
_LOCK = threading.Lock()
STATE: dict = {"last_scan": 0.0, "last_scan_result": "", "scanning": False, "building": ""}

# capitalised words that are not names
_NOT_NAMES = set("""I I'm I'll I've I'd OK Okay Mr Mrs Ms Dr Sir Miss Mister Lord Lady God Oh Ah Eh Hey Yes No
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December English Japanese Chinese Korean Spanish French German Christmas
The A An And But Or So If When What Why How Who Where Which This That These Those It He She They We You
Mom Dad Mommy Daddy Mother Father Brother Sister Grandma Grandpa Captain Sensei Senpai Boss Chief
Please Thank Thanks Sorry Wait Come Look Let Stop Don't Can't Won't Is Are Was Were Do Does Did""".split())


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS learn_shows(
            show TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '',
            names_built_at REAL NOT NULL DEFAULT 0, names_from TEXT NOT NULL DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS learn_names(
            show TEXT NOT NULL, name TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT '', on_ INTEGER NOT NULL DEFAULT 1, at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(show, name))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS learn_fixes(
            show TEXT NOT NULL, wrong TEXT NOT NULL, right TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'any',
            n INTEGER NOT NULL DEFAULT 1, applied INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT '',
            on_ INTEGER NOT NULL DEFAULT 1, at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(show, wrong, scope))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS learn_orig(
            file_id INTEGER PRIMARY KEY, path TEXT NOT NULL, show TEXT NOT NULL, tool TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '', made_at REAL NOT NULL DEFAULT 0, checked_at REAL NOT NULL DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS learn_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
            show TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""")
    _READY = True


# ------------------------------------------------------------- shows -----
def show_of(path: str) -> str:
    """The series folder (or a film's folder), lower-cased - qwenjudge's key."""
    from .qwenjudge import series_of
    return series_of(path or "")


def _label_of(show: str) -> str:
    return os.path.basename(show.rstrip("\\/")) or show


def _real_label(cur, show: str) -> str:
    """The folder name as it is written - show keys are lower-cased."""
    r = cur.execute("SELECT path FROM files WHERE lower(path) LIKE ? LIMIT 1", (show + "%",)).fetchone()
    if r:
        d = os.path.dirname(r["path"])
        if re.match(r"(?i)^(season\s*\d+|specials|s\d+)$", os.path.basename(d)):
            d = os.path.dirname(d)
        return os.path.basename(d)
    return _label_of(show)


def _touch_show(cur, show: str, path_hint: str = "") -> None:
    label = os.path.basename(os.path.dirname(path_hint)) if path_hint else ""
    if label and re.match(r"(?i)^(season\s*\d+|specials|s\d+)$", label):
        label = os.path.basename(os.path.dirname(os.path.dirname(path_hint)))
    cur.execute("INSERT INTO learn_shows(show, label) VALUES(?,?) ON CONFLICT(show) DO UPDATE SET "
                "label=CASE WHEN excluded.label!='' THEN excluded.label ELSE learn_shows.label END",
                (show, label or _label_of(show)))


def _event(cur, kind: str, show: str, detail: str) -> None:
    cur.execute("INSERT INTO learn_events(at, kind, show, detail) VALUES(?,?,?,?)",
                (time.time(), kind, show, detail[:300]))
    cur.execute("DELETE FROM learn_events WHERE id < (SELECT MAX(id) - 400 FROM learn_events)")


# ------------------------------------------------------------- names -----
def names(show: str, limit: int = NAMES_MAX) -> list:
    init()
    with cursor() as cur:
        rows = cur.execute("SELECT name FROM learn_names WHERE show IN (?,?) AND on_=1 "
                           "ORDER BY n DESC, name LIMIT ?", (show, ALL, int(limit))).fetchall()
    return [r["name"] for r in rows]


def _clean_sub_text(text: str) -> list:
    """Dialogue lines out of SRT or ASS text."""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        if s.startswith("Dialogue:"):
            s = s.split(",", 9)[-1]
        elif s.startswith(("[", "Style:", "Format:", "ScriptType", "PlayRes", "Title:", "WrapStyle",
                           "ScaledBorder", "YCbCr", "Collisions", "Comment:")):
            continue
        s = re.sub(r"\{[^}]*\}", "", s).replace("\\N", " ").replace("\\n", " ")
        s = re.sub(r"<[^>]+>", "", s).strip()
        if s:
            out.append(s)
    return out


def _proper_nouns(docs: list) -> list:
    """[(name, count)] - words written capitalised in the MIDDLE of sentences
    across the documents and (almost) never in lower case."""
    capmid: dict = {}
    eps: dict = {}
    lower: dict = {}
    for i, lines in enumerate(docs):
        for ln in lines:
            for sent in re.split(r"(?<=[.!?…])\s+|^\s*-\s*", ln):
                toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", sent)
                for k, w in enumerate(toks):
                    w = re.sub(r"'s$", "", w)
                    if len(w) < 3:
                        continue
                    if w[0].islower():
                        lower[w.lower()] = lower.get(w.lower(), 0) + 1
                    elif k > 0 and not w.isupper():
                        capmid[w] = capmid.get(w, 0) + 1
                        eps.setdefault(w, set()).add(i)
    need_eps = 2 if len(docs) >= 2 else 1
    need_n = 3 if len(docs) >= 2 else 4
    out = []
    for w, n in capmid.items():
        if n < need_n or len(eps[w]) < need_eps or w in _NOT_NAMES:
            continue
        if lower.get(w.lower(), 0) > 0.1 * n:
            continue
        out.append((w, n))
    out.sort(key=lambda x: -x[1])
    return out


def _english_name(fname: str) -> bool:
    low = fname.lower()
    return bool(re.search(r"\.(en|eng|english)(\.[a-z]+)*\.(srt|ass|ssa)$", low))


def build_names(show: str, force: bool = False) -> dict:
    """Gather the show's names from the English subtitles in its folder."""
    init()
    with cursor() as cur:
        r = cur.execute("SELECT names_built_at FROM learn_shows WHERE show=?", (show,)).fetchone()
    if r and not force and time.time() - float(r["names_built_at"] or 0) < NAMES_REBUILD_S:
        return {"skipped": "built recently"}
    STATE["building"] = show
    try:
        from .config import SETTINGS
        docs, used = [], []
        root = show
        # the real-case folder: show keys are lower-cased
        with cursor() as cur:
            fr = cur.execute("SELECT id, path FROM files WHERE lower(path) LIKE ? ORDER BY path LIMIT 400",
                             (show.replace("%", "") + "%",)).fetchall()
        paths = [x["path"] for x in fr]
        if paths:
            root = os.path.dirname(paths[0])
            if re.match(r"(?i)^(season\s*\d+|specials|s\d+)$", os.path.basename(root)):
                root = os.path.dirname(root)
        # 1. sidecars, anywhere under the folder
        for dp, ds, fs in os.walk(root):
            if dp.count(os.sep) - root.count(os.sep) > 2:
                continue
            for f in fs:
                if _english_name(f) and len(docs) < 40:
                    try:
                        with open(os.path.join(dp, f), encoding="utf-8-sig", errors="replace") as h:
                            docs.append(_clean_sub_text(h.read()))
                        used.append(f)
                    except OSError:
                        pass
        # 2. English text tracks inside a few files
        if len(docs) < 6 and fr:
            ff = getattr(SETTINGS, "ffmpeg", "ffmpeg")
            try:
                from .jobs import _ffmpeg_exe
                ff = _ffmpeg_exe()
            except Exception:                                    # noqa: BLE001
                pass
            ids = [int(x["id"]) for x in fr]
            with cursor() as cur:
                facts = cur.execute(f"SELECT file_id, path, tracks FROM sub_facts WHERE file_id IN "
                                    f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []
            got = 0
            for f in facts:
                if got >= EMBED_FILES:
                    break
                try:
                    tr = json.loads(f["tracks"] or "[]")
                except Exception:                                # noqa: BLE001
                    continue
                t = next((t for t in tr if str(t.get("lang") or "")[:2] == "en"
                          and str(t.get("codec") or "") in TEXT_CODECS and t.get("class") != "signs"), None)
                if not t or not os.path.exists(f["path"]):
                    continue
                try:
                    out = subprocess.run([ff, "-v", "error", "-i", f["path"], "-map", f"0:s:{int(t['ord'])}",
                                          "-f", "srt", "-"], capture_output=True, timeout=240,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                    docs.append(_clean_sub_text(out.decode("utf-8", "replace")))
                    used.append(os.path.basename(f["path"]))
                    got += 1
                except Exception:                                # noqa: BLE001
                    continue
        found = _proper_nouns([d for d in docs if d])[:NAMES_MAX * 2]
        now = time.time()
        with cursor() as cur:
            _touch_show(cur, show, paths[0] if paths else "")
            cur.execute("DELETE FROM learn_names WHERE show=? AND source='subtitles'", (show,))
            for w, n in found:
                cur.execute("INSERT INTO learn_names(show, name, n, source, at) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(show, name) DO UPDATE SET n=MAX(learn_names.n, excluded.n)",
                            (show, w, int(n), "subtitles", now))
            cur.execute("UPDATE learn_shows SET names_built_at=?, names_from=? WHERE show=?",
                        (now, f"{len(used)} subtitle file(s)", show))
            if found:
                _event(cur, "names", show, f"{len(found)} names from {len(used)} English subtitle file(s): "
                                           + ", ".join(w for w, _n in found[:8]))
        return {"names": len(found), "from": len(used)}
    finally:
        STATE["building"] = ""


def set_name(show: str, name: str, action: str) -> bool:
    """add / on / off / remove one name ('*' = every show)."""
    init()
    name = str(name or "").strip()
    if not name:
        return False
    with cursor() as cur:
        if action == "add":
            _touch_show(cur, show)
            cur.execute("INSERT INTO learn_names(show, name, n, source, on_, at) VALUES(?,?,?,?,1,?) "
                        "ON CONFLICT(show, name) DO UPDATE SET on_=1, source='you', n=MAX(learn_names.n, 99)",
                        (show, name, 99, "you", time.time()))
            _event(cur, "names", show, f"you added the name {name}")
        elif action in ("on", "off"):
            cur.execute("UPDATE learn_names SET on_=? WHERE show=? AND name=?", (1 if action == "on" else 0, show, name))
        elif action == "remove":
            cur.execute("DELETE FROM learn_names WHERE show=? AND name=?", (show, name))
        else:
            return False
    return True


# ------------------------------------------------------- corrections -----
_PUNCT = ".,!?;:\"'()[]{}…-"


def fixes(show: str, scope: str) -> list:
    """[(wrong, right)] in force for this show and scope ('ocr' / 'whisper' /
    'qwen'); 'any' and '*' apply everywhere. A Qwen-learnt fix waits for its
    second sighting."""
    init()
    with cursor() as cur:
        rows = cur.execute(
            "SELECT wrong, right FROM learn_fixes WHERE show IN (?,?) AND scope IN (?, 'any') AND on_=1 "
            "AND (source!='qwen' OR n>=?) ORDER BY length(wrong) DESC",
            (show, ALL, scope, QWEN_ADOPT_N)).fetchall()
    return [(r["wrong"], r["right"]) for r in rows]


def apply(text: str, pairs: list) -> tuple:
    """-> (text, {"wrong->right": times}). Whole words only."""
    hits: dict = {}
    for w, r in pairs:
        pat = r"(?<![\w'])" + re.escape(w) + r"(?![\w'])"
        text, n = re.subn(pat, r.replace("\\", "\\\\"), text)
        if n:
            hits[f"{w}→{r}"] = n
    return text, hits


def note_applied(show: str, hits: dict) -> None:
    """Count what a run applied (hits from apply())."""
    if not hits:
        return
    init()
    with cursor() as cur:
        for k, n in hits.items():
            w, _sep, r = k.partition("→")
            cur.execute("UPDATE learn_fixes SET applied=applied+? WHERE show IN (?,?) AND wrong=? AND right=?",
                        (int(n), show, ALL, w, r))


def _pairs_of(was: str, now: str) -> list:
    """Small word-level replacements between two versions of a line."""
    a, b = was.split(), now.split()
    out = []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace" or not (1 <= i2 - i1 <= 3 and 1 <= j2 - j1 <= 3):
            continue
        w = " ".join(a[i1:i2]).strip(_PUNCT)
        r = " ".join(b[j1:j2]).strip(_PUNCT)
        if not w or not r or w == r or len(w) > 40 or len(r) > 40:
            continue
        if w.lower() != r.lower() and difflib.SequenceMatcher(None, w.lower(), r.lower()).ratio() < 0.45:
            continue            # a rewrite, not a correction
        out.append((w, r))
    return out


def learn_pairs(show: str, pairs: list, source: str, scope: str, path_hint: str = "") -> int:
    """Lines as they were and as they are now -> corrections (and names)."""
    init()
    n_new = 0
    now = time.time()
    with cursor() as cur:
        _touch_show(cur, show, path_hint)
        for was, nowtxt in pairs:
            for w, r in _pairs_of(was or "", nowtxt or ""):
                cur.execute("INSERT INTO learn_fixes(show, wrong, right, scope, n, source, at) VALUES(?,?,?,?,1,?,?) "
                            "ON CONFLICT(show, wrong, scope) DO UPDATE SET n=learn_fixes.n+1, right=excluded.right, "
                            "at=excluded.at, source=CASE WHEN excluded.source='edit' THEN 'edit' ELSE learn_fixes.source END",
                            (show, w, r, scope, source, now))
                n_new += 1
                if source == "edit":
                    for tok in re.findall(r"[A-Z][a-z][A-Za-z'\-]+", r):
                        if tok not in _NOT_NAMES:
                            cur.execute("INSERT INTO learn_names(show, name, n, source, at) VALUES(?,?,?,?,?) "
                                        "ON CONFLICT(show, name) DO UPDATE SET n=learn_names.n+3, on_=1",
                                        (show, tok, 5, "edit", now))
        if n_new:
            what = {"qwen": "Qwen's OCR repair", "edit": "your edit", "you": "you"}.get(source, source)
            _event(cur, "fixes", show, f"{n_new} correction(s) learnt from {what}")
    return n_new


def set_fix(show: str, wrong: str, right: str, scope: str, action: str) -> bool:
    init()
    wrong, right = str(wrong or "").strip(), str(right or "").strip()
    scope = scope if scope in ("any", "ocr", "whisper", "qwen") else "any"
    if not wrong:
        return False
    with cursor() as cur:
        if action == "add":
            if not right or right == wrong:
                return False
            _touch_show(cur, show)
            cur.execute("INSERT INTO learn_fixes(show, wrong, right, scope, n, source, on_, at) VALUES(?,?,?,?,9,'you',1,?) "
                        "ON CONFLICT(show, wrong, scope) DO UPDATE SET right=excluded.right, source='you', on_=1",
                        (show, wrong, right, scope, time.time()))
            _event(cur, "fixes", show, f"you added {wrong} → {right}")
        elif action in ("on", "off"):
            cur.execute("UPDATE learn_fixes SET on_=? WHERE show=? AND wrong=? AND scope=?",
                        (1 if action == "on" else 0, show, wrong, scope))
        elif action == "delete":
            cur.execute("DELETE FROM learn_fixes WHERE show=? AND wrong=? AND scope=?", (show, wrong, scope))
        else:
            return False
    return True


# ------------------------------------------------------------ edits -----
def remember_made(file_id: int, path: str, srt_text: str, tool: str) -> None:
    """Keep a subtitle as nuarr wrote it, to see later what you changed."""
    init()
    show = show_of(path)
    with cursor() as cur:
        _touch_show(cur, show, path)
        cur.execute("INSERT INTO learn_orig(file_id, path, show, tool, text, made_at, checked_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(file_id) DO UPDATE SET path=excluded.path, show=excluded.show, tool=excluded.tool, "
                    "text=excluded.text, made_at=excluded.made_at, checked_at=excluded.checked_at",
                    (int(file_id), path, show, tool, srt_text or "", time.time(), time.time()))


def _cues(srt_text: str) -> dict:
    """{start seconds (0.1 s): text} from SRT."""
    out = {}
    for blk in re.split(r"\r?\n\s*\r?\n", (srt_text or "").strip()):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->", blk)
        if not m:
            continue
        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)[:3].ljust(3, "0")) / 1000
        body = blk.split("-->", 1)[1].split("\n", 1)[1] if "\n" in blk.split("-->", 1)[1] else ""
        body = re.sub(r"\{[^}]*\}", "", body)
        body = re.sub(r"<[^>]+>", "", body)
        out[round(t, 1)] = " ".join(body.split())
    return out


def _current(row) -> tuple:
    """(text now, where) - the sidecar if there is one, else nuarr's track inside the file."""
    from . import substyle
    from .submake import TRACK_NAME, _sidecar
    path = row["path"]
    for ext in (".srt", ".ass"):
        side = _sidecar(path, ext)
        if os.path.exists(side):
            if os.path.getmtime(side) <= float(row["checked_at"] or 0):
                return None, "unchanged"
            t = open(side, encoding="utf-8-sig", errors="replace").read()
            return (t if ext == ".srt" else substyle.ass_to_srt(t)), "sidecar"
    if not os.path.exists(path) or os.path.getmtime(path) <= float(row["checked_at"] or 0):
        return None, "unchanged"
    from .jobs import _ffmpeg_exe
    ff = _ffmpeg_exe()
    fp = os.path.join(os.path.dirname(ff), "ffprobe.exe")
    try:
        st = json.loads(subprocess.run([fp, "-v", "error", "-select_streams", "s", "-show_entries",
                                        "stream=index:stream_tags=title", "-of", "json", path],
                                       capture_output=True, text=True, timeout=60,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout)["streams"]
    except Exception:                                            # noqa: BLE001
        return None, "unreadable"
    k = next((i for i, s in enumerate(st) if ((s.get("tags") or {}).get("title") or "") == TRACK_NAME), None)
    if k is None:
        return None, "nuarr's track is gone"
    out = subprocess.run([ff, "-v", "error", "-i", path, "-map", f"0:s:{k}", "-f", "srt", "-"],
                         capture_output=True, timeout=600,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return out.decode("utf-8", "replace"), "inside the file"


def scan_edits() -> dict:
    """Compare every subtitle nuarr made with the one on disk now."""
    init()
    if not _LOCK.acquire(blocking=False):
        return {"busy": True}
    STATE["scanning"] = True
    looked = changed = learnt = 0
    try:
        with cursor() as cur:
            rows = cur.execute("SELECT * FROM learn_orig").fetchall()
        for row in rows:
            looked += 1
            try:
                now_text, where = _current(row)
            except Exception:                                    # noqa: BLE001
                continue
            if now_text is None:
                continue
            a, b = _cues(row["text"]), _cues(now_text)
            both = [t for t in a if t in b]
            diff = [(a[t], b[t]) for t in both if a[t] != b[t]]
            remade = len(both) < 0.6 * max(1, len(a)) or len(diff) > 0.4 * max(1, len(both))
            n = 0
            if diff and not remade:
                changed += 1
                n = learn_pairs(row["show"], diff, "edit", "any", row["path"])
                learnt += n
                if n:
                    with cursor() as cur:
                        _event(cur, "edit", row["show"],
                               f"your edit of {os.path.basename(row['path'])[:70]} ({where}): {len(diff)} line(s) changed, "
                               f"{n} correction(s)")
            with cursor() as cur:
                cur.execute("UPDATE learn_orig SET text=?, checked_at=? WHERE file_id=?",
                            (now_text, time.time(), int(row["file_id"])))
        res = f"{looked} made subtitle(s) looked at, {changed} edited, {learnt} correction(s) learnt"
        STATE.update(last_scan=time.time(), last_scan_result=res)
        return {"looked": looked, "edited": changed, "learnt": learnt}
    finally:
        STATE["scanning"] = False
        _LOCK.release()


async def watch() -> None:
    """Look for your edits once a day."""
    import asyncio
    await asyncio.sleep(900)
    while True:
        try:
            await asyncio.to_thread(scan_edits)
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(86400)


# --------------------------------------------------------- the panels -----
def snapshot(kind: str = "") -> dict:
    init()
    with cursor() as cur:
        shows = [dict(r) for r in cur.execute(
            "SELECT s.show, s.label, s.names_built_at, s.names_from, "
            " (SELECT COUNT(*) FROM learn_names n WHERE n.show=s.show AND n.on_=1) names_on, "
            " (SELECT COUNT(*) FROM learn_names n WHERE n.show=s.show) names_all, "
            " (SELECT COUNT(*) FROM learn_fixes f WHERE f.show=s.show AND f.on_=1 "
            "    AND (f.source!='qwen' OR f.n>=?)) fixes_on, "
            " (SELECT COUNT(*) FROM learn_fixes f WHERE f.show=s.show) fixes_all, "
            " (SELECT COALESCE(SUM(applied),0) FROM learn_fixes f WHERE f.show=s.show) applied, "
            " (SELECT COUNT(*) FROM learn_orig o WHERE o.show=s.show) made "
            "FROM learn_shows s WHERE s.show != ? ORDER BY names_on + fixes_on DESC, s.label",
            (QWEN_ADOPT_N, ALL)).fetchall()]
        scope_sql = {"ocr": "('ocr','any')", "whisper": "('whisper','any')", "qwen": "('qwen','ocr','any')"}.get(kind)
        fx = [dict(r) for r in cur.execute(
            "SELECT f.*, COALESCE(s.label, f.show) label FROM learn_fixes f LEFT JOIN learn_shows s ON s.show=f.show "
            + (f"WHERE f.scope IN {scope_sql} " if scope_sql else "")
            + "ORDER BY f.at DESC LIMIT 400").fetchall()]
        ev = [dict(r) for r in cur.execute(
            "SELECT e.*, COALESCE(s.label, e.show) label FROM learn_events e LEFT JOIN learn_shows s ON s.show=e.show "
            "ORDER BY e.id DESC LIMIT 40").fetchall()]
        made = cur.execute("SELECT COUNT(*) n FROM learn_orig").fetchone()["n"]
    for f in fx:
        f["active"] = bool(f["on_"]) and (f["source"] != "qwen" or f["n"] >= QWEN_ADOPT_N)
    out = {"shows": shows, "fixes": fx, "events": ev, "made_kept": made, "qwen_adopt_n": QWEN_ADOPT_N,
           "names_max": NAMES_MAX, **STATE}
    if kind == "qwen":
        try:
            from . import qwenjudge
            with cursor() as cur:
                pl = qwenjudge.pool(cur)
                rows = sorted([{"show": k, "yes": sum(1 for x in v if x[1] == "yes"),
                                "no": sum(1 for x in v if x[1] == "no")} for k, v in pl.items()],
                              key=lambda x: -(x["yes"] + x["no"]))[:60]
                for x in rows:
                    x["label"] = _real_label(cur, x["show"])
            out["answers"] = rows
        except Exception:                                        # noqa: BLE001
            out["answers"] = []
    return out


def show_detail(show: str) -> dict:
    init()
    with cursor() as cur:
        nm = [dict(r) for r in cur.execute("SELECT * FROM learn_names WHERE show=? ORDER BY on_ DESC, n DESC, name",
                                           (show,)).fetchall()]
        fx = [dict(r) for r in cur.execute("SELECT * FROM learn_fixes WHERE show=? ORDER BY at DESC", (show,)).fetchall()]
    for f in fx:
        f["active"] = bool(f["on_"]) and (f["source"] != "qwen" or f["n"] >= QWEN_ADOPT_N)
    return {"show": show, "label": _label_of(show), "names": nm, "fixes": fx}
