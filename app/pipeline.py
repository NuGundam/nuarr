r"""The journey a file takes, as a graph that describes itself.

WHY THIS IS NOT A PICTURE IN THE DOCS. A drawing of a pipeline is correct on
the day it is drawn. The thresholds move, a library gets its OCR switched off,
a pool is added - and the drawing goes on looking authoritative while quietly
describing a system that no longer exists. Every label here is read from the
same settings the running code reads, and every count is a query, so the
diagram is wrong only if nuarr is wrong.

The graph is built HERE rather than in the page for the same reason the rules
themselves live in Python: the branch labels are statements about behaviour,
and a statement about behaviour belongs next to the behaviour. The page draws
whatever it is handed and knows nothing about subtitles or codecs.
"""
from __future__ import annotations

import time

from .db import cursor

# READ FROM remedy, NOT RETYPED. The whole point of this module is that a
# number on the diagram cannot drift from the number the code uses.
try:
    from .remedy import AUTO_REQUEUE_PER_HOUR as REQ_CAP
    from .remedy import AUTO_REPLACE_PER_HOUR as REP_CAP
except Exception:                                            # noqa: BLE001
    REQ_CAP = REP_CAP = 0

# One place for the shape. Positions are a grid the page turns into pixels -
# column, row - so re-laying-out the diagram does not mean touching the labels.
_COLS = ["found", "read", "decided", "known", "waiting", "working", "kept"]

# THE ORDER THE SYSTEMS TAKE A FILE IN, read from precedence.py rather than
# retyped here - the whole point of this module. Each is (kind, label, note).
_FACT_WORDS = {
    "decode":  ("Does it decode?",
                "five windows spread head to tail are actually decoded. A "
                "truncated download passes ffprobe and fails halfway through "
                "the episode; every minute spent rewriting one is wasted."),
    "listen":  ("What language is it really?",
                "Whisper listens to five windows of each audio track. A tag "
                "that is WRONG is worse than one that is missing: the plan "
                "keeps and drops tracks by language, so a mislabelled track "
                "gets the file rewritten, then found out, then rewritten "
                "again."),
    "audio":   ("Audio tags corrected",
                "the corrected tags are written into the header - in place, "
                "in under a second, before anything reads them to build a "
                "plan."),
    "subread": ("What the subtitle tracks are",
                "which track is dialogue, which is signs, and whether there "
                "are words burned into the picture - so the plan can flag "
                "and keep the right ones rather than guessing from titles."),
}


def _n(cur, sql: str, args: tuple = ()) -> int:
    try:
        r = cur.execute(sql, args).fetchone()
        return int(r[0]) if r else 0
    except Exception:                                        # noqa: BLE001
        return 0


def _has(cur, table: str) -> bool:
    """Is this table here yet?

    Every check below was added after some installs were already running, and
    a table is only created the first time its module is used. A diagram that
    raises because nobody has switched on the OCR sweep would be a worse bug
    than the missing box it was trying to avoid.
    """
    try:
        return bool(cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone())
    except Exception:                                        # noqa: BLE001
        return False


# EVERY BOX ON THAT ROW COUNTS THE SAME KIND OF THING. The first draft had
# the audio check showing 3,590 - tracks LISTENED TO - beside neighbours
# showing 0 and 3, which were findings. Three numbers on one row answering two
# different questions is how a diagram teaches something false: it reads as
# "the audio check has three and a half thousand problems".
#
# So every count here is what the check is currently holding for a person, and
# the population it was drawn from goes in the note. For this one that means
# asking audiolang, which costs about a tenth of a second - memoised, because
# the diagram polls every eight.
_LIES = {"at": 0.0, "n": 0}
_LIES_TTL = 30.0


def _mislabelled() -> int:
    now = time.time()
    if now - _LIES["at"] < _LIES_TTL:
        return _LIES["n"]
    try:
        from . import audiolang
        _LIES.update(at=now, n=len(audiolang.mismatches(500)))
    except Exception:                                        # noqa: BLE001
        _LIES["at"] = now
    return _LIES["n"]


def _sidecars_cached() -> int | None:
    """Sidecars waiting, ONLY if somebody has already counted them.

    subembed.summary() walks every folder in the library - a minute of disk -
    and starts that walk in the background if its answer has gone stale.
    Opening a diagram must not set that going, and this poll runs every eight
    seconds, so this reads the cache and accepts None as an answer.
    """
    try:
        from . import subembed
        d = subembed._SUM.get("data")
        return int(d["files"]) if d else None
    except Exception:                                        # noqa: BLE001
        return None


def _settings_labels() -> dict:
    """The branch conditions, in the words of the current configuration."""
    out = {}
    try:
        from . import subocr
        out["typeset"] = (
            f"more than {subocr.TYPESET_SHARE:.0%} of cues taller than "
            f"{subocr.TYPESET_TALL_FRAC:.0%} of frame")
        out["ocr_engine"] = subocr.engine() or "tesseract"
        # ASK enabled_for() PER LIBRARY rather than reading the settings dict.
        # That function is what the sweep itself consults, and it folds in the
        # per-library override and the global switch together - reproducing
        # that here would be a second copy of the rule, free to disagree.
        libs = []
        with cursor() as cur:
            names = [r[0] for r in cur.execute(
                "SELECT DISTINCT library FROM files "
                "WHERE library IS NOT NULL AND library != '' "
                "  AND state NOT IN ('deleted','duplicate') ORDER BY library")]
        for name in names:
            try:
                if subocr.enabled_for(name):
                    libs.append(name)
            except Exception:                                # noqa: BLE001
                continue
        out["ocr_libraries"] = libs
    except Exception:                                        # noqa: BLE001
        out.setdefault("typeset", "tall bitmaps dominate the track")
        out.setdefault("ocr_engine", "")
        out.setdefault("ocr_libraries", [])
    return out


def _busy_pool(kind: str, rn: dict, qn: dict) -> int:
    """Outstanding jobs of one fact-finding kind. Its pool shares its name
    except for the two that do not, so the map is written down once."""
    pool = {"decode": "decode", "listen": "listen", "audio": "audio",
            "subread": "subread"}.get(kind, kind)
    return int(rn.get(pool, 0)) + int(qn.get(pool, 0))



FRESH_S = 24 * 3600.0
_SLOW = {"at": 0.0, "v": {}}
_SLOW_TTL = 300.0


def _slow() -> dict:
    """The historical counts, memoised five minutes.

    Together these cost 1.5-2.5 s - the DISTINCT over every done job alone
    is 1.2 s - and the page polled them every eight seconds, so the page was
    never faster than the slowest of them. They count things that change on
    the scale of hours; five minutes is nothing on that scale."""
    now = time.time()
    if now - _SLOW["at"] < _SLOW_TTL and _SLOW["v"]:
        return _SLOW["v"]
    with cursor() as cur:
        v = dict(
            probed=_n(cur, "SELECT COUNT(*) FROM file_probes p JOIN files f ON f.id=p.file_id "
                           "WHERE f.state NOT IN ('deleted','duplicate')"),
            bad_now=_n(cur, """
                SELECT COUNT(*) FROM files f
                WHERE f.state NOT IN ('deleted','duplicate')
                  AND (SELECT j.state FROM jobs j WHERE j.file_id = f.id
                        ORDER BY j.id DESC LIMIT 1) IN ('failed','blocked')"""),
            bad_ever=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state IN ('failed','blocked')"),
            d_enc=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' AND pool='encode'"),
            d_pass=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' AND pool='passthrough'"),
            d_sub=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' AND pool='subocr'"),
            d_subs=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' AND pool='subs'"),
            d_make=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' AND pool='submake'"),
            files_done=_n(cur, """
                SELECT COUNT(DISTINCT j.file_id) FROM jobs j JOIN files f ON f.id = j.file_id
                 WHERE j.state='done' AND f.state NOT IN ('deleted','duplicate')"""),
            skipped=_n(cur, """
                SELECT COUNT(*) FROM files f
                 WHERE f.state NOT IN ('deleted','duplicate')
                   AND (SELECT j.state FROM jobs j WHERE j.file_id = f.id
                         ORDER BY j.id DESC LIMIT 1) = 'skipped'"""),
            skipped_ever=_n(cur, "SELECT COUNT(*) FROM jobs WHERE state='skipped'"),
            ck_read=_n(cur, "SELECT COUNT(*) FROM integrity"),
            ck_burn_seen=_n(cur, "SELECT COUNT(*) FROM hardsub"),
            ck_marked=_n(cur, "SELECT COUNT(*) FROM hardsub WHERE marked=1"),
            ck_heard=_n(cur, "SELECT COUNT(*) FROM audio_lang"),
            shaped=_n(cur, "SELECT COUNT(DISTINCT file_id) FROM sub_shape"),
            typeset=_n(cur, "SELECT COUNT(DISTINCT file_id) FROM sub_shape WHERE typeset=1"),
        )
    _SLOW.update(at=now, v=v)
    return v


def graph() -> dict:
    """Nodes, edges, counts and labels. The live counts every call, the
    historical ones from _slow()."""
    lab = _settings_labels()
    S = _slow()
    now = time.time()
    with cursor() as cur:
        live = _n(cur, "SELECT COUNT(*) FROM files "
                       "WHERE state NOT IN ('deleted','duplicate')")
        probed = S["probed"]
        unprobed = max(0, live - probed)
        live_by_pool: dict = {"queued": {}, "running": {}}
        try:
            for r in cur.execute(
                    "SELECT pool, state, COUNT(*) n FROM jobs "
                    " WHERE state IN ('queued','running') GROUP BY pool, state"):
                live_by_pool[r["state"]][r["pool"] or ""] = int(r["n"] or 0)
        except Exception:                                    # noqa: BLE001
            pass
        qn, rn = live_by_pool["queued"], live_by_pool["running"]

        def _busy(*pools) -> int:
            return sum(qn.get(p, 0) + rn.get(p, 0) for p in pools)

        q_all = sum(qn.values())
        r_enc, r_pass = _busy("encode"), _busy("passthrough")
        r_sub, r_subs, r_make = _busy("subocr"), _busy("subs"), _busy("submake")
        n_new = _n(cur, "SELECT COUNT(*) FROM files WHERE state='new'")
        n_elig = _n(cur, "SELECT COUNT(*) FROM files WHERE state='eligible'")
        n_again = _n(cur, "SELECT COUNT(*) FROM files WHERE state='eligible' "
                          "  AND requeued_at IS NOT NULL")
        n_first = max(0, n_elig - n_again)
        # THE FRESH BAND. A file that landed in the last day - a new import or
        # an upgrade, which is a new row - rides priority 20 on every job,
        # newest first, ahead of the disk balance, until it is watchable.
        n_fresh = _n(cur, "SELECT COUNT(*) FROM files WHERE state NOT IN "
                          "('deleted','duplicate') AND COALESCE(first_seen,0) > ?",
                     (now - FRESH_S,))
        n_fresh_jobs = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state IN "
                               "('queued','running') AND priority <= 20")
        n_watch = _n(cur, "SELECT COUNT(*) FROM files f WHERE f.state='done' "
                          "  AND COALESCE(f.first_seen,0) > ? AND NOT EXISTS "
                          "  (SELECT 1 FROM jobs j WHERE j.file_id=f.id "
                          "    AND j.state IN ('queued','running','deferred'))",
                     (now - FRESH_S,))
        # raws the maker is for, and batons waiting to go in
        n_raw = 0
        try:
            n_raw = _n(cur, "SELECT COUNT(DISTINCT file_id) FROM sub_need WHERE state='missing'")
        except Exception:                                    # noqa: BLE001
            pass
        # the standing checks
        ck_decode = _n(cur, "SELECT COUNT(*) FROM integrity WHERE verdict='corrupt'")
        ck_burn = _n(cur, "SELECT COUNT(*) FROM hardsub h "
                          "WHERE h.state NOT IN ('none','') AND h.marked=0 "
                          "  AND h.file_id NOT IN (SELECT file_id FROM hardsub_ignored)")
        ck_waiting = _n(cur, "SELECT COUNT(*) FROM audio_lang_queue")
        ck_rule = _n(cur, "SELECT COUNT(*) FROM audit_findings WHERE run_id="
                          "(SELECT MAX(run_id) FROM audit_findings)")
        ck_gap = _n(cur, "SELECT COUNT(*) FROM arrgap_seen")
        hour_ago = now - 3600.0
        acted = {r[0]: r[1] for r in cur.execute(
            "SELECT action, COUNT(*) FROM remedy_log WHERE ok=1 "
            "GROUP BY action")} if _has(cur, "remedy_log") else {}
        acted_h = {r[0]: r[1] for r in cur.execute(
            "SELECT action, COUNT(*) FROM remedy_log WHERE ok=1 AND at>=? "
            "GROUP BY action", (hour_ago,))} if _has(cur, "remedy_log") else {}

    shaped, typeset = S["shaped"], S["typeset"]
    dialogue = max(0, shaped - typeset)
    held: dict = {}
    try:
        from . import precedence
        held = dict((precedence.eligible_breakdown() or {}).get("by") or {})
    except Exception:                                        # noqa: BLE001
        pass

    eng = lab.get("ocr_engine") or "the OCR"
    libs = lab.get("ocr_libraries") or []
    libtxt = (", ".join(libs) if 0 < len(libs) <= 3
              else (f"{len(libs)} libraries" if libs else "no libraries"))
    sidecars = _sidecars_cached()
    ck_lie = _mislabelled()
    found_now = ck_decode + ck_lie + ck_burn + ck_rule + ck_gap
    n_req, n_rep = acted.get("requeue", 0), acted.get("replace", 0)
    n_fix, n_ask = acted.get("repair", 0), acted.get("reask", 0)

    def fact(k, col):
        lbl, note = _FACT_WORDS[k]
        pool = {"decode": "decode", "listen": "listen", "audio": "audio", "subread": "subread"}[k]
        return dict(id=f"f_{k}", col=col, row=5, label=lbl, kind="pool", pool=pool,
                    count=_busy(pool),
                    note=(note + (f" {held[k]:,} file(s) are held waiting for this "
                                  f"right now." if held.get(k) else "")))

    # ---- picture 1: top-down, two lanes ---------------------------------
    nodes = [
        dict(id="lane_a", col=0, row=0, label="Imported or upgraded", kind="lane",
             count=None, note="a file the arrs have just landed. An upgrade is "
                              "a new arr file id, so it is a new file to nuarr and "
                              "starts here, not in the right-hand lane."),
        dict(id="lane_b", col=2, row=0, label="Already in the library", kind="lane",
             count=None, note="a committed file is not finished, only correct for "
                              "now. These are the things that put it back on the "
                              "line - at the backlog's priority, behind anything "
                              "that landed today."),
        dict(id="found", col=0, row=1, label="It lands", count=n_new, kind="source",
             note=("an arr tells nuarr a file was imported, or the scan finds it. "
                   "The count is the settle hold: nuarr does not touch a file "
                   "while it might still be being written.")),
        dict(id="fresh", col=0, row=2, label="Fresh for 24 h", count=n_fresh, kind="source",
             note=(f"every job on a file that landed in the last day rides the fresh "
                   f"band: priority 20, below every feeder, newest arrival first, "
                   f"taken before the disk balance - through the facts, the rewrite, "
                   f"the OCR, the maker and the fixes, not just until the rewrite. "
                   f"{n_fresh_jobs:,} job(s) are riding it right now.")),
        dict(id="checks", col=2, row=1, label="Six standing checks", count=found_now,
             kind="warn" if found_now else "stage",
             note=(f"on their own clocks, over files committed weeks ago. Will not "
                   f"decode: {ck_decode:,} (of {S['ck_read']:,} checked). Audio tag is a "
                   f"lie: {ck_lie:,} (of {S['ck_heard']:,} tracks heard; {ck_waiting:,} "
                   f"waiting). Words in the frame: {ck_burn:,} (of {S['ck_burn_seen']:,} "
                   f"sampled, {S['ck_marked']:,} marked). Sidecars outside: "
                   f"{sidecars if sidecars is not None else 'not counted yet'}. Breaks a "
                   f"rule today: {ck_rule:,}. Missing from the arrs: {ck_gap:,}. All of "
                   f"them hand what they find to one remedy layer with one budget.")),
        dict(id="rule", col=3, row=3, label="A rule changed", count=n_again, kind="source",
             note=("change a codec, a language, a subtitle policy, and every file it "
                   "now judges differently goes straight back to Ready to plan. No "
                   "check, no remedy; the rule is the finding.")),
        dict(id="req", col=2, row=2, label="Plan it again", count=n_req, kind="pool", pool="passthrough",
             note=(f"the default remedy: back through the rules, queued if there is "
                   f"work. {acted_h.get('requeue', 0)} in the last hour of {REQ_CAP} "
                   f"allowed automatically.")),
        dict(id="fix", col=3, row=2, label="Fix in place", count=n_fix, kind="pool", pool="subocr",
             note=("the record was wrong, not the file: a title rewritten, a stale "
                   "Plex analysis re-read, something the arr already tracks indexed. "
                   "Nothing re-encoded.")),
        dict(id="rep", col=2, row=3, label="Blocklist and replace", count=n_rep, kind="bad",
             note=(f"the only irreversible verb: delete, blocklist, search again. "
                   f"Refused for anything a rebuild could fix. The replacement lands "
                   f"as a new file in the left lane. {acted_h.get('replace', 0)} in the "
                   f"last hour of {REP_CAP} allowed.")),
        dict(id="ask", col=3, row=1, label="Ask again", count=n_ask, kind="stage",
             note=("already blocklisted and still not replaced: one more indexer "
                   "search, six hours between asks about the same file.")),
        dict(id="elig", col=1, row=4, label="Ready to plan", count=n_elig, kind="stage",
             note=(f"both lanes meet here. {n_first:,} are here for the first time, "
                   f"{n_again:,} are back because a rule changed or somebody asked. "
                   f"A fresh file's jobs go in ahead of the backlog; a returning "
                   f"file's go in behind it.")),
        fact("decode", 0), fact("listen", 1), fact("audio", 2), fact("subread", 3),
        dict(id="plan", col=1, row=6, label="Planned", count=None, kind="stage",
             note=("the rules turn the probe and the four facts into a plan: which "
                   "video, which audio, which subtitles, and so which worker. "
                   f"{unprobed:,} files have not been probed yet; they are read the "
                   f"first time something needs them.")),
        dict(id="nothing", col=2, row=6, label="Nothing to do", count=S["skipped"], kind="idle",
             note=(f"the commonest outcome and not a failure: the file already matches "
                   f"the policy. {S['skipped_ever']:,} such conclusions in total. A fresh "
                   f"file that needs no rewrite still gets its subtitle question asked "
                   f"the same second.")),
        dict(id="gate", col=1, row=7, label="Job gate", count=q_all, kind="gate",
             note=("everything queued, in every pool. Held while someone is watching, "
                   "a disk is busy or a pool is paused; a fresh-band job is taken "
                   "first whichever disk it is on"
                   + (f". {sum(held.values()):,} file(s) are waiting on a fact" if sum(held.values()) else ""))),
        dict(id="encode", col=0, row=8, label="Encode", count=r_enc, kind="pool", pool="encode",
             note="the picture is rebuilt on the graphics card"),
        dict(id="passthrough", col=1, row=8, label="Passthrough", count=r_pass, kind="pool", pool="passthrough",
             note=("tracks and flags change, the picture is copied untouched. A copy "
                   "keeps its decode verdict, so the file is not decoded again.")),
        dict(id="failed", col=2, row=8, label="Still not landed", count=S["bad_now"], kind="bad",
             note=(f"most recent attempt failed or was blocked and nothing has put it "
                   f"right since. Retried on a backoff. {S['bad_ever']:,} failures ever; "
                   f"the rest were fixed later or belong to files since replaced.")),
        dict(id="commit", col=1, row=9, label="Committed", count=S["files_done"], kind="stage",
             note=(f"written back over the original, paced so a viewer never feels it. "
                   f"{S['d_enc'] + S['d_pass'] + S['d_sub'] + S['d_subs'] + S['d_make']:,} "
                   f"commits in total. The moment a fresh file's rewrite lands, the "
                   f"subtitle question is asked for it - no waiting for a sweep.")),
        dict(id="subocr", col=0, row=10, label="Subtitle OCR", count=r_sub, kind="pool", pool="subocr",
             note=(f"picture subtitles that survived the rewrite are read into text "
                   f"with {eng}, for {libtxt}. The text goes to the file through the "
                   f"subs-pending baton and is retried if the file is busy.")),
        dict(id="submake", col=1, row=10, label="Subtitle maker", count=r_make, kind="pool", pool="submake",
             note=(f"a raw - no English anywhere, judged by the raws check and the "
                   f"second opinion - gets English made from the audio and embedded "
                   f"as 'English (nuarr)'. Runs as a queue job, one job per file, "
                   f"never on a file something else is rewriting; the made text waits "
                   f"in the same baton as the OCR's and is retried. {n_raw:,} raw(s) "
                   f"known right now.")),
        dict(id="subs", col=2, row=10, label="Subtitle fixes", count=r_subs, kind="pool", pool="subs",
             note=("sidecars taken inside, duplicate tracks dropped, titles corrected, "
                   "on the final container. A subtitle-only rewrite re-keys the "
                   "verdicts, so nothing is listened to or decoded again.")),
        dict(id="watch", col=1, row=11, label="Watchable", count=n_watch, kind="stage",
             note=(f"fresh files with nothing left owed: rewritten, subtitled, done. "
                   f"The count is how many of today's arrivals are there now.")),
    ]
    edges = [
        dict(a="found", b="fresh", label="the hold passes", n=n_new),
        dict(a="fresh", b="elig", label="ahead of everything, newest first", n=n_fresh),
        dict(a="checks", b="req", label="a rebuild would fix it", n=n_req),
        dict(a="checks", b="fix", label="the record is wrong", n=n_fix),
        dict(a="checks", b="rep", label="only a new release", n=n_rep),
        dict(a="checks", b="ask", label="", n=n_ask),
        dict(a="rule", b="elig", label="straight back on the line", n=n_again),
        dict(a="req", b="elig", label="behind fresh", n=n_req),
        dict(a="rep", b="found", label="the replacement lands", n=n_rep),
        dict(a="elig", b="f_decode", label="the facts first", n=n_elig),
        dict(a="f_decode", b="f_listen", label="", n=_busy("listen")),
        dict(a="f_listen", b="f_audio", label="", n=_busy("audio")),
        dict(a="f_audio", b="f_subread", label="", n=_busy("subread")),
        dict(a="f_listen", b="plan", label="all four answered", n=probed),
        dict(a="plan", b="nothing", label="already correct", n=S["skipped"], muted=True),
        dict(a="plan", b="gate", label="a change is needed", n=q_all),
        dict(a="gate", b="encode", label="picture must be rebuilt", n=r_enc),
        dict(a="gate", b="passthrough", label="picture can be copied", n=r_pass),
        dict(a="encode", b="commit", label="", n=S["d_enc"]),
        dict(a="passthrough", b="commit", label="", n=S["d_pass"]),
        dict(a="passthrough", b="failed", label="", n=S["bad_now"], muted=True),
        dict(a="commit", b="subocr", label="picture subs survived", n=S["d_sub"]),
        dict(a="commit", b="submake", label="a raw: no English anywhere", n=S["d_make"]),
        dict(a="commit", b="subs", label="titles, duplicates, sidecars", n=S["d_subs"]),
        dict(a="subocr", b="watch", label="", n=S["d_sub"]),
        dict(a="submake", b="watch", label="", n=S["d_make"]),
        dict(a="subs", b="watch", label="", n=S["d_subs"]),
    ]
    # ---- picture 3: the subtitle decision ---------------------------------
    sub_nodes = [
        dict(id="s_img", col=0, row=0, label="Picture subtitles found", count=shaped, kind="source",
             note="PGS or VobSub tracks in a file the OCR sweep is about to act on"),
        dict(id="s_meas", col=1, row=0, label="Bitmaps measured", count=shaped, kind="stage",
             note="heights read straight from the PGS headers - no OCR, no decode"),
        dict(id="s_type", col=2, row=0, label="Typeset signs", count=typeset, kind="pool", pool="encode",
             note=lab.get("typeset", "")),
        dict(id="s_dial", col=2, row=1, label="Dialogue", count=dialogue, kind="pool", pool="subocr",
             note="ordinary lines at the bottom of the frame"),
        dict(id="s_burn", col=3, row=0, label="Burned into the picture", count=typeset, kind="stage",
             note="an encode; the separate track is dropped because it is part of the picture now"),
        dict(id="s_ocr", col=3, row=1, label="Read into text", count=dialogue, kind="stage",
             note=f"{eng} writes an SRT, replacing any earlier OCR track rather than stacking a second one"),
    ]
    sub_edges = [
        dict(a="s_img", b="s_meas", label="before anything is queued", n=shaped),
        dict(a="s_meas", b="s_type", label=lab.get("typeset", ""), n=typeset),
        dict(a="s_meas", b="s_dial", label="anything else", n=dialogue),
        dict(a="s_type", b="s_burn", label="", n=typeset),
        dict(a="s_dial", b="s_ocr", label="", n=dialogue),
    ]
    # ---- picture 2: the order the systems take a file in -----------------
    ord_nodes, ord_edges = [], []
    try:
        from . import precedence as _prec
        _chain = list(_prec.ORDER)
        _pool_of = dict(_prec.POOL_OF_KIND)
    except Exception:                                        # noqa: BLE001
        _chain = ["decode", "listen", "audio", "subread", "transcode", "sub_ocr", "submake", "subs"]
        _pool_of = {"decode": "decode", "listen": "listen", "audio": "audio", "subread": "subread",
                    "transcode": "encode", "sub_ocr": "subocr", "submake": "submake", "subs": "subs"}
    _SHORT = {"decode": "Does it decode?", "listen": "What language?",
              "audio": "Tags corrected", "subread": "What the subs are",
              "transcode": "The rewrite", "sub_ocr": "Subtitle OCR",
              "submake": "Subtitle maker", "subs": "Subtitle fixes"}
    _REST = {
        "transcode": ("The rewrite",
                      "the standardising pass: unwanted tracks dropped, audio converted, "
                      "Dolby Vision stripped. Everything above it exists so this runs once."),
        "sub_ocr": ("Subtitle OCR",
                    "the picture subtitles that SURVIVED the rewrite are read into text - "
                    "never one that was about to be dropped."),
        "submake": ("Subtitle maker",
                    "English made from the audio for a raw, embedded as its own track - "
                    "only once the facts say there is nothing to read and the rewrite is done."),
        "subs": ("Subtitle fixes",
                 "sidecars in, duplicates out, titles corrected - on the final container."),
    }
    for i, k in enumerate(_chain):
        lbl, note = (_FACT_WORDS.get(k) or _REST.get(k) or (k.replace("_", " "), ""))
        n_now = _busy_pool(_pool_of.get(k, k), rn, qn)
        ord_nodes.append(dict(
            id=f"o_{k}", col=i, row=0, label=_SHORT.get(k, lbl),
            count=n_now, kind="pool", pool=_pool_of.get(k, ""),
            note=(f"{i + 1} of {len(_chain)}. " + note
                  + (f" {held[k]:,} file(s) are held waiting for this right now." if held.get(k) else ""))))
        if i:
            prev = _chain[i - 1]
            ord_edges.append(dict(
                a=f"o_{prev}", b=f"o_{k}",
                label=("facts first" if k == "transcode" else "after it" if prev == "transcode" else ""),
                n=n_now))

    return {"cols": _COLS, "nodes": nodes, "edges": edges,
            "order": {"nodes": ord_nodes, "edges": ord_edges},
            "sub": {"nodes": sub_nodes, "edges": sub_edges},
            "engine": eng, "libraries": libs, "at": now}


# What nuarr can actually do about each kind of failure. The point of naming
# these is that two of them have a real remedy and one does not, and offering a
# "Fix" button that cannot fix anything is worse than saying so plainly.
_CAUSES = [
    ("path_too_long", ("over the 255", "path is", "filename too long"),
     "The full path is longer than Windows allows, so the rewritten file "
     "cannot be written next to the original.",
     "nuarr cannot shorten it safely on its own - the name comes from the "
     "arr. Shorten that series' naming format in Sonarr or Radarr, let it "
     "rename, then Rescan here.",
     False),
    ("locked", ("database is locked", "being used by another process",
                "cannot access the file", "sharing violation"),
     "Something else held the file or the database at that moment.",
     "Nothing is wrong with the file - retrying is the whole fix.",
     True),
    ("bad_data", ("invalid data found", "moov atom", "corrupt"),
     "ffmpeg could not read the source. Usually a damaged download.",
     "Retrying will fail the same way. Replace the release, or blocklist it "
     "and let the arr fetch another.",
     False),
    ("space", ("no space left", "disk full"),
     "The cache or destination disk ran out of room.",
     "Free space, then retry.",
     True),
]


def classify(err: str) -> dict:
    """What went wrong, in words, and whether retrying could possibly help."""
    e = (err or "").lower()
    for key, marks, what, advice, retryable in _CAUSES:
        if any(m in e for m in marks):
            return {"cause": key, "what": what, "advice": advice,
                    "retryable": retryable}
    return {"cause": "other", "what": "",
            "advice": "No known pattern - retrying is worth one attempt.",
            "retryable": True}


def not_landed() -> list:
    r"""Files whose most recent attempt failed and nothing has since fixed.

    The same definition the flow page counts, so the list and the number can
    never disagree - one query, one meaning of "still wrong".
    """
    out = []
    with cursor() as cur:
        rows = [dict(r) for r in cur.execute("""
            SELECT f.id, f.title, f.season, f.episode, f.library, f.path,
                   f.size, f.state,
                   j.id AS job_id, j.state AS jstate, j.error, j.kind,
                   j.pool, j.finished_at, j.created_at
              FROM files f
              JOIN jobs j ON j.id = (SELECT j2.id FROM jobs j2
                                      WHERE j2.file_id = f.id
                                      ORDER BY j2.id DESC LIMIT 1)
             WHERE f.state NOT IN ('deleted','duplicate')
               AND j.state IN ('failed','blocked')
             ORDER BY f.title, f.season, f.episode""")]
    try:
        from .subocr import ep_label
    except Exception:                                        # noqa: BLE001
        def ep_label(a, b):
            return ""
    # RE-TEST THE CONDITION, DO NOT TRUST THE OLD MESSAGE. An error is a
    # record of what was true when it was written, and settings move under it.
    # Every one of the nine path-length blocks here was recorded against a
    # 255-char limit that is now 259 with long paths enabled - none of them
    # would be blocked today, and telling someone to go and shorten their
    # naming format would have sent them off to fix a problem that no longer
    # exists. Where the original condition can be re-evaluated cheaply, it is.
    try:
        from . import fileops
        from .config import SETTINGS
    except Exception:                                        # noqa: BLE001
        fileops = SETTINGS = None
    for r in rows:
        r["ep"] = ep_label(r.pop("season", None), r.pop("episode", None))
        r.update(classify(r.get("error")))
        r["attempts"] = 0
        r["path_len"] = len(r.get("path") or "")
        r["stale"] = False
        if r["cause"] == "path_too_long" and fileops and SETTINGS:
            try:
                still = (fileops.path_too_long(r["path"],
                                               SETTINGS.max_path_length)
                         and not SETTINGS.allow_long_paths)
            except Exception:                                # noqa: BLE001
                still = True
            if not still:
                r["stale"] = True
                r["retryable"] = True
                r["what"] = ("The path was too long for the limit in force "
                             "when this was recorded.")
                r["advice"] = (
                    f"That limit has since moved to "
                    f"{SETTINGS.max_path_length}"
                    + (" with long paths enabled" if SETTINGS.allow_long_paths
                       else "")
                    + f", and this path is {r['path_len']} chars - it would "
                      "not be blocked today. Retrying should simply work.")
        out.append(r)
    if out:
        ids = [r["id"] for r in out]
        marks = ",".join("?" * len(ids))
        with cursor() as cur:
            for a in cur.execute(
                    f"SELECT file_id, COUNT(*) n FROM jobs "
                    f"WHERE file_id IN ({marks}) "
                    f"  AND state IN ('failed','blocked') "
                    f"GROUP BY file_id", ids):
                d = dict(a)
                for r in out:
                    if r["id"] == d["file_id"]:
                        r["attempts"] = d["n"]
            # WHETHER ANYTHING IS COMING FOR IT. A row that says only "blocked"
            # reads as abandoned, and someone will press a button that the
            # retry ladder was going to press for them in eight minutes.
            try:
                for a in cur.execute(
                        f"SELECT file_id, attempts, next_at, state "
                        f"FROM error_retry WHERE file_id IN ({marks})", ids):
                    d = dict(a)
                    for r in out:
                        if r["id"] == d["file_id"]:
                            r["retry_state"] = d["state"]
                            r["retry_attempts"] = d["attempts"] or 0
                            r["retry_in_s"] = max(
                                0, (d["next_at"] or 0) - time.time())
            except Exception:                                # noqa: BLE001
                pass
    try:
        from .errorretry import MAX_ATTEMPTS
    except Exception:                                        # noqa: BLE001
        MAX_ATTEMPTS = 0
    for r in out:
        r.setdefault("retry_state", "")
        r.setdefault("retry_attempts", 0)
        r.setdefault("retry_in_s", 0)
        r["retry_max"] = MAX_ATTEMPTS
    return out


def route(file_id: int) -> dict:
    r"""The path ONE file actually took, as node ids to light up.

    Read from what happened to it rather than from what the rules would say
    now - the point of tracing a real file is to see the route it took, which
    a settings change since then does not alter.
    """
    out = {"file_id": file_id, "title": "", "path": [], "sub_path": [],
           "steps": [], "found": False}
    with cursor() as cur:
        f = cur.execute(
            "SELECT id, title, season, episode, state, library, path, first_seen "
            "FROM files WHERE id=?", (int(file_id),)).fetchone()
        if not f:
            return out
        f = dict(f)
        out["found"] = True
        try:
            from .subocr import ep_label
            ep = ep_label(f.get("season"), f.get("episode"))
        except Exception:                                    # noqa: BLE001
            ep = ""
        out["title"] = f"{f['title']}{' - ' + ep if ep else ''}"
        out["library"] = f.get("library") or ""
        probed = cur.execute("SELECT 1 FROM file_probes WHERE file_id=?",
                             (int(file_id),)).fetchone() is not None
        jobs = [dict(r) for r in cur.execute(
            "SELECT kind, state, pool, error, created_at, finished_at "
            "FROM jobs WHERE file_id=? ORDER BY id", (int(file_id),))]
        shapes = [dict(r) for r in cur.execute(
            "SELECT rel, typeset, median_h, tall_share FROM sub_shape "
            "WHERE file_id=? ORDER BY rel", (int(file_id),))]

    # THE SUBTITLE BRANCH IS COMPUTED FIRST because the main path has several
    # early returns and this used to sit after all of them. A typeset file
    # whose OCR correctly skipped leaves every job in state 'skipped', which
    # takes the "nothing to do" exit - so the one case where the subtitle
    # branch is the whole story was the one case that returned without it.
    sub_path, sub_steps = _sub_route(shapes, jobs)
    out["sub_path"] = sub_path

    path = ["found"]
    steps = [("found", "the arrs told nuarr about this file")]
    fresh = float(f.get("first_seen") or 0) > time.time() - FRESH_S
    if fresh:
        path.append("fresh")
        steps.append(("fresh", "landed in the last day - every job on it rides the fresh band"))
    path.append("elig")
    done_kinds = {j["kind"] for j in jobs if j["state"] == "done"}
    for k in ("decode", "listen", "audio", "subread"):
        if k in done_kinds:
            path.append(f"f_{k}")
    if done_kinds & {"decode", "listen", "audio", "subread"}:
        steps.append(("facts", ", ".join(k for k in ("decode", "listen", "audio", "subread")
                                         if k in done_kinds) + " answered"))

    def finish():
        # The subtitle notes belong AFTER the journey, not spliced into the
        # middle of it - they explain a branch, and a reader meets the branch
        # once they have followed the trunk.
        out["path"] = path
        out["steps"] = [list(s) for s in steps + sub_steps]
        return out
    if probed:
        steps.append(("probe", "ffprobe read its streams"))
    else:
        steps.append(("unprobed", "not probed yet"))
        return finish()

    path.append("plan")
    steps.append(("plan", "the rules turned that probe into a plan"))
    if not jobs:
        path.append("nothing")
        steps.append(("nothing", "no work was ever queued for it"))
        return finish()

    last = jobs[-1]
    if all(j["state"] == "skipped" for j in jobs):
        path.append("nothing")
        steps.append(("nothing", f"{len(jobs)} job(s), all skipped - "
                                 f"{last.get('error') or 'nothing to change'}"))
        return finish()

    path.append("gate")
    steps.append(("gate", "queued, and released by the gate"))
    # IN THE ORDER THEY HAPPENED. Walking a fixed encode/passthrough/subocr
    # list drew Batman Beyond as encode-then-passthrough when the passthrough
    # ran first - a sequence diagram that reports the wrong sequence is worse
    # than one that reports none.
    seen, counts = [], {}
    for j in jobs:
        p = j["pool"]
        if p not in ("encode", "passthrough"):
            continue
        counts[p] = counts.get(p, 0) + 1
        if p not in seen:
            seen.append(p)
    for p in seen:
        path.append(p)
        n = counts[p]
        steps.append((p, f"{n} {p} job{'s' if n != 1 else ''}"))
    if any(j["state"] == "done" for j in jobs):
        path.append("commit")
        steps.append(("commit", "written back over the original"))
    if any(j["state"] in ("failed", "blocked") for j in jobs):
        path.append("failed")
        steps.append(("failed", last.get("error") or "did not land"))
    _AFTER = {"subocr": ("subocr", "picture subtitles read into text"),
              "submake": ("submake", "English made from the audio and embedded"),
              "subs": ("subs", "subtitle tracks settled")}
    for p, (nid, txt) in _AFTER.items():
        n = sum(1 for j in jobs if j["pool"] == p and j["state"] == "done")
        if n:
            path.append(nid)
            steps.append((nid, f"{txt} ({n} job{'s' if n != 1 else ''})"))
    if fresh and (f.get("state") == "done") and jobs[-1]["state"] in ("done", "skipped"):
        path.append("watch")
        steps.append(("watch", "watchable - nothing left owed"))

    return finish()


def _sub_route(shapes: list, jobs: list) -> tuple:
    """The picture-subtitle branch for one file: nodes to light, and why."""
    sub_path, steps = [], []
    if not shapes:
        if any(j["kind"] == "sub_ocr" for j in jobs):
            # EXPLAIN THE BLANK. A file that has been through the subtitle path
            # and shows no measurement looks like the measurement never ran. It
            # did - and then the file was rewritten, which moves track numbers
            # and so deletes the verdicts on purpose (subocr.forget_shapes).
            # Saying nothing invites the conclusion that the check is broken.
            steps.append(("s_meas", "no current measurement - the verdicts are "
                                    "cleared whenever the file is rewritten, "
                                    "because track numbers move"))
        return sub_path, steps
    sub_path = ["s_img", "s_meas"]
    ts = [s for s in shapes if s["typeset"]]
    if ts:
        sub_path.append("s_type")
        steps.append(("s_type", "track %s: %d%% of cues are tall bitmaps - "
                                "signs, so they get burned in"
                      % (ts[0]["rel"], round((ts[0]["tall_share"] or 0) * 100))))
        if any(j["pool"] == "encode" and j["state"] == "done" for j in jobs):
            sub_path.append("s_burn")
    if len(ts) < len(shapes):
        d = [s for s in shapes if not s["typeset"]][0]
        sub_path.append("s_dial")
        steps.append(("s_dial", "track %s: %d%% tall, ordinary dialogue"
                      % (d["rel"], round((d["tall_share"] or 0) * 100))))
        if any(j["kind"] == "sub_ocr" and j["state"] == "done" for j in jobs):
            sub_path.append("s_ocr")
    return sub_path, steps
