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
        blocked = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='blocked'")
        failed = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='failed'")
        d_enc = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' "
                        "AND pool='encode'")
        d_pass = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' "
                         "AND pool='passthrough'")
        d_sub = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='done' "
                        "AND pool='subocr'")
        skipped = _n(cur, "SELECT COUNT(*) FROM jobs WHERE state='skipped'")
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
             note="already matches the policy - no rewrite would change it"),
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
             count=d_enc + d_pass + d_sub, kind="stage",
             note="written back over the original, paced so a viewer never "
                  "feels it"),
        dict(id="failed", col=5, row=2, label="Failed or blocked",
             count=failed + blocked, kind="bad",
             note="retried on a backoff; blocked means the file could not be "
                  "opened exclusively"),
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
        dict(a="encode", b="failed", label="", n=failed + blocked, muted=True),
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
