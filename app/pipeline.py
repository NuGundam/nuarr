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

# One place for the shape. Positions are a grid the page turns into pixels -
# column, row - so re-laying-out the diagram does not mean touching the labels.
_COLS = ["found", "read", "decided", "waiting", "working", "kept"]


def _n(cur, sql: str, args: tuple = ()) -> int:
    try:
        r = cur.execute(sql, args).fetchone()
        return int(r[0]) if r else 0
    except Exception:                                        # noqa: BLE001
        return 0


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


def graph() -> dict:
    """Nodes, edges, counts and labels. One pass over the database."""
    lab = _settings_labels()
    with cursor() as cur:
        live = _n(cur, "SELECT COUNT(*) FROM files "
                       "WHERE state NOT IN ('deleted','duplicate')")
        probed = _n(cur, "SELECT COUNT(*) FROM file_probes p JOIN files f "
                         "ON f.id=p.file_id "
                         "WHERE f.state NOT IN ('deleted','duplicate')")
        unprobed = max(0, live - probed)
        q_enc = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='queued' "
                        "AND pool IN ('encode','passthrough')")
        q_sub = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='queued' "
                        "AND pool='subocr'")
        r_enc = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='running' "
                        "AND pool='encode'")
        r_pass = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='running' "
                         "AND pool='passthrough'")
        r_sub = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='running' "
                        "AND pool='subocr'")
        # OUTSTANDING, NOT HISTORICAL. Counting every row that ever failed
        # answered a question nobody asked: of 33 such rows here, 4 were fixed
        # by a later pass and 14 belong to files that have since been deleted
        # or replaced. Reporting 33 as if 33 things were broken is the way a
        # number stops being believed. A file counts when it is still in the
        # library AND its most recent job failed or was blocked - i.e. nothing
        # has put it right since.
        bad_now = _n(cur, """
            SELECT COUNT(*) FROM files f
            WHERE f.state NOT IN ('deleted','duplicate')
              AND (SELECT j.state FROM jobs j
                    WHERE j.file_id = f.id
                    ORDER BY j.id DESC LIMIT 1) IN ('failed','blocked')""")
        bad_ever = _n(cur, "SELECT COUNT(*) FROM jobs "
                           "WHERE state IN ('failed','blocked')")
        # THESE COUNT FILES, NOT JOBS. A file rewritten three times is one
        # file, and putting 48,530 commits beside a library of 39,581 invites
        # the reader to compare two numbers that do not belong on the same
        # scale. The job totals are throughput and are kept for the edges,
        # where "how much has flowed down this path" is the right question.
        d_enc = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' "
                        "AND pool='encode'")
        d_pass = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' "
                         "AND pool='passthrough'")
        d_sub = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' "
                        "AND pool='subocr'")
        jobs_done = d_enc + d_pass + d_sub
        files_done = _n(cur, """
            SELECT COUNT(DISTINCT j.file_id) FROM jobs j JOIN files f
              ON f.id = j.file_id
             WHERE j.state='done'
               AND f.state NOT IN ('deleted','duplicate')""")
        # "Nothing to do" is a population too: files sitting there correct,
        # not the number of times we have concluded that.
        skipped = _n(cur, """
            SELECT COUNT(*) FROM files f
             WHERE f.state NOT IN ('deleted','duplicate')
               AND (SELECT j.state FROM jobs j WHERE j.file_id = f.id
                     ORDER BY j.id DESC LIMIT 1) = 'skipped'""")
        skipped_ever = _n(cur, "SELECT COUNT(*) FROM jobs "
                               "WHERE state='skipped'")
        # The subtitle branch, which is the part with a measurement behind it.
        shaped = _n(cur, "SELECT COUNT(DISTINCT file_id) FROM sub_shape")
        typeset = _n(cur, "SELECT COUNT(DISTINCT file_id) FROM sub_shape "
                          "WHERE typeset=1")
        dialogue = max(0, shaped - typeset)

    eng = lab.get("ocr_engine") or "the OCR"
    libs = lab.get("ocr_libraries") or []
    libtxt = (", ".join(libs) if 0 < len(libs) <= 3
              else (f"{len(libs)} libraries" if libs else "no libraries"))

    nodes = [
        dict(id="found", col=0, row=1, label="In the library",
             count=live, kind="source",
             note="every file the arrs have told nuarr about"),
        dict(id="probe", col=1, row=1, label="Probed",
             count=probed, kind="stage",
             note="ffprobe has read its streams; the answer is cached so a "
                  "rescan does not re-probe"),
        dict(id="unprobed", col=1, row=2, label="Not probed yet",
             count=unprobed, kind="idle",
             note="waiting for the next scan"),
        dict(id="plan", col=2, row=1, label="Planned",
             count=None, kind="stage",
             note="the rules turn the probe into a plan: which video, which "
                  "audio, which subtitles, and therefore which pool"),
        dict(id="nothing", col=2, row=2, label="Nothing to do",
             count=skipped, kind="idle",
             note=("files whose last look found nothing worth changing - they "
                   "already match the policy. "
                   f"{skipped_ever:,} such conclusions have been reached in "
                   "total, since a file is re-examined whenever the rules or "
                   "the file change.")),
        dict(id="gate", col=3, row=1, label="Job gate",
             count=q_enc + q_sub, kind="gate",
             note="holds work while someone is watching, while a disk is "
                  "busy, or while you have paused it"),
        dict(id="encode", col=4, row=0, label="Encode",
             count=r_enc, kind="pool", pool="encode",
             note="the picture is rebuilt on the graphics card"),
        dict(id="passthrough", col=4, row=1, label="Passthrough",
             count=r_pass, kind="pool", pool="passthrough",
             note="tracks and flags change, the picture is copied untouched"),
        dict(id="subocr", col=4, row=2, label="Subtitle OCR",
             count=r_sub, kind="pool", pool="subocr",
             note=f"picture subtitles read into text with {eng}, "
                  f"for {libtxt}"),
        dict(id="commit", col=5, row=1, label="Committed",
             count=files_done, kind="stage",
             note=("files that have been rewritten at least once and written "
                   "back over the original, paced so a viewer never feels it. "
                   f"{jobs_done:,} commits in total - a file is rewritten "
                   "again whenever the policy changes under it.")),
        dict(id="failed", col=5, row=2, label="Still not landed",
             count=bad_now, kind="bad",
             note=("files whose most recent attempt failed or was blocked, "
                   "and which nothing has put right since. Retried on a "
                   "backoff; blocked usually means the file could not be "
                   "opened exclusively, or its path is too long. "
                   f"{bad_ever} failures have been recorded in total - the "
                   "rest were fixed by a later pass or belong to files since "
                   "deleted or replaced.")),
    ]
    edges = [
        dict(a="found", b="probe", label="scan reads it", n=probed),
        dict(a="found", b="unprobed", label="not reached yet", n=unprobed,
             muted=True),
        dict(a="probe", b="plan", label="rules decide", n=probed),
        dict(a="plan", b="nothing", label="already correct", n=skipped,
             muted=True),
        dict(a="plan", b="gate", label="a change is needed", n=q_enc + q_sub),
        dict(a="gate", b="encode", label="picture must be rebuilt", n=r_enc),
        dict(a="gate", b="passthrough", label="picture can be copied",
             n=r_pass),
        dict(a="gate", b="subocr", label="picture subtitles to read",
             n=r_sub),
        dict(a="encode", b="commit", label="", n=d_enc),
        dict(a="passthrough", b="commit", label="", n=d_pass),
        dict(a="subocr", b="commit", label="", n=d_sub),
        dict(a="encode", b="failed", label="", n=bad_now, muted=True),
    ]
    # The subtitle decision, drawn as its own small graph under the main one -
    # it is the one branch with a measurement rather than a setting behind it.
    sub_nodes = [
        dict(id="s_img", col=0, row=0, label="Picture subtitles found",
             count=shaped, kind="source",
             note="PGS or VobSub tracks in a file the OCR sweep is about to "
                  "act on"),
        dict(id="s_meas", col=1, row=0, label="Bitmaps measured",
             count=shaped, kind="stage",
             note="heights read straight from the PGS headers - no OCR, no "
                  "decode"),
        dict(id="s_type", col=2, row=0, label="Typeset signs",
             count=typeset, kind="pool", pool="encode",
             note=lab.get("typeset", "")),
        dict(id="s_dial", col=2, row=1, label="Dialogue",
             count=dialogue, kind="pool", pool="subocr",
             note="ordinary lines at the bottom of the frame"),
        dict(id="s_burn", col=3, row=0, label="Burned into the picture",
             count=typeset, kind="stage",
             note="an encode; the separate track is dropped because it is "
                  "part of the picture now"),
        dict(id="s_ocr", col=3, row=1, label="Read into text",
             count=dialogue, kind="stage",
             note=f"{eng} writes an SRT, replacing any earlier OCR track "
                  f"rather than stacking a second one"),
    ]
    sub_edges = [
        dict(a="s_img", b="s_meas", label="before anything is queued",
             n=shaped),
        dict(a="s_meas", b="s_type", label=lab.get("typeset", ""), n=typeset),
        dict(a="s_meas", b="s_dial", label="anything else", n=dialogue),
        dict(a="s_type", b="s_burn", label="", n=typeset),
        dict(a="s_dial", b="s_ocr", label="", n=dialogue),
    ]
    return {"cols": _COLS, "nodes": nodes, "edges": edges,
            "sub": {"nodes": sub_nodes, "edges": sub_edges},
            "engine": eng, "libraries": libs, "at": time.time()}


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
            "SELECT id, title, season, episode, state, library, path "
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

    def finish():
        # The subtitle notes belong AFTER the journey, not spliced into the
        # middle of it - they explain a branch, and a reader meets the branch
        # once they have followed the trunk.
        out["path"] = path
        out["steps"] = [list(s) for s in steps + sub_steps]
        return out
    if probed:
        path.append("probe")
        steps.append(("probe", "ffprobe read its streams"))
    else:
        path.append("unprobed")
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
        if p not in ("encode", "passthrough", "subocr"):
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
