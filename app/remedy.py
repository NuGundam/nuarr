r"""nuarr - the two things you can do about a finding, in one place.

WHY THIS MODULE EXISTS
----------------------
Seven systems find things wrong: the rule audit, the arr gap check, the arr
sync check, the audio-title check, the Plex sync check, the subtitle-rules gap
and the errors tile. Every one of them grew its own answer to "and now what?",
and the answers were not the same shape:

    rule audit      requeue (mature: attempt counts, give-up, re-verify)
                    replace, but only ever for audio/language
    errors tile     requeue, replace - gated on refetch.classify()
    arr gap         index the file; no requeue, no replace
    arr sync        its own fixer
    audio title     requeue, its own endpoint
    Plex sync       requeue, through plexqueue
    subtitle gap    requeue, its own endpoint

Two consequences, both bad. A file whose only remedy is a different release
could be found by six of these and offered that remedy by one. And nothing
counted across systems: seven auto-mode sweeps, each politely capped at three
replacements a run, is twenty-one deleted files an hour and nobody's cap was
exceeded.

So this module owns the two verbs, the policy that decides which verb a finding
is allowed, and the ledger that both remedies write to. It does NOT own the
finding state - the audit's audit_heals rows keep tracking audit findings, and
each check keeps its own list. This is the record of what was DONE, by whom, to
what, and whether it worked. One place to look when a file keeps coming back.

THE TWO VERBS
-------------
    requeue   offer the file to the planner. Costs GPU time. Reversible in the
              sense that matters: the source file is still there afterwards.
    replace   blocklist the release and ask the arr for another one, which
              deletes the file on the way past. Costs an indexer grab and a
              file. Not reversible.

They are not alternatives on a spectrum. They answer different questions -
"the bytes are right but arranged wrong" versus "the bytes are wrong" - and
offering the second where the first applies is how a naming bug becomes a
library that deletes itself once per pass. refetch.py learned that the hard
way and its docstring is worth reading before editing POLICY below.
"""
from __future__ import annotations

import asyncio
import time

from . import joblog
from .config import SETTINGS
from .db import cursor

REQUEUE = "requeue"
REPLACE = "replace"
# THE THIRD VERB, WHICH THE FIRST DRAFT OF THIS MODULE DID NOT HAVE.
#
# Three of the seven checks do not find faults in files at all. arrsync finds
# that Sonarr's record disagrees with the file; plexsync finds that Plex's
# cached analysis is stale; audiotitle finds that a track's TITLE describes a
# codec the track no longer carries. Every one of those is a string in somebody
# else's database, and the fix is to write the correct string - a tenth of a
# second with mkvpropedit, or one API call.
#
# Offering those findings a requeue would send 39,000 files through the
# transcoder to correct a caption, which audiotitle's own docstring calls
# absurd and is right to. Offering them a replace would delete a perfectly good
# file because Sonarr's bookkeeping was out of date. So they get a verb of
# their own rather than a borrowed one, and the cards can say plainly that the
# other two do not apply and why.
REPAIR = "repair"
# THE FOURTH, AND THE NARROWEST. Nothing is wrong with anything: a release was
# rejected, the arr was asked for another, and none has come. Ask again. It
# touches no file and blocklists nothing - it spends one indexer search, which
# is why it is rate limited rather than free.
REASK = "reask"


# ---------------------------------------------------------------- policy ----
# WHAT EACH FINDING IS ALLOWED, AND WHAT AUTO MODE IS ALLOWED WITHOUT ASKING.
#
# Four columns per kind:
#   requeue       the planner can plausibly fix this
#   replace       a different release can plausibly fix this
#   auto_replace  ... and auto mode may do it unattended
#   why           what to tell somebody hovering the button
#
# auto_replace is deliberately a THIRD column rather than `replace and auto`.
# Plenty of findings are worth replacing when a person has looked at them and
# are not worth an unattended delete on the strength of a tag - audio/untagged
# is exactly that file: the words may be right and the label wrong, and no
# amount of re-downloading fixes a label.
#
# FAIL CLOSED. Anything not in this table gets requeue only. A finding kind
# nobody has thought about must never arrive with a delete button already
# attached to it - which is the same rule refetch.classify() enforces for
# error strings, for the same reason.
_P = {}


def _kind(name: str, requeue: bool, replace: bool, auto_replace: bool,
          why: str) -> None:
    _P[name] = {"requeue": requeue, "replace": replace,
                "auto_replace": auto_replace, "why": why}


# -- the bytes are wrong. Only a different release can help. ------------------
_kind("file/corrupt", False, True, True,
      "the file does not decode - no re-encode can rebuild bytes that are "
      "not there")
_kind("audio/missing", False, True, True,
      "there is no audio stream at all - there is nothing to re-encode")
_kind("video/missing", False, True, True,
      "there is no video stream at all - there is nothing to re-encode")
_kind("audio/language", False, True, True,
      "the audio is in no language this library keeps, and no re-encode "
      "changes what language somebody is speaking")
_kind("job/content", False, True, True,
      "the job failed on the file's own contents")

# -- the bytes are right, the arrangement is wrong. The planner fixes it. -----
_kind("audio/channels", True, False, False,
      "the planner downmixes this")
_kind("audio/EAE", True, False, False,
      "the planner re-encodes this")
_kind("audio/commentary", True, False, False,
      "the planner drops commentary tracks")
_kind("audio/dedupe", True, False, False,
      "the planner drops the duplicate track")
_kind("audio/extra-language", True, False, False,
      "the planner drops the extra language")
_kind("video/codec", True, False, False,
      "the planner re-encodes this into a codec the library keeps")
_kind("container", True, False, False,
      "the planner remuxes this into Matroska")
_kind("video/DV", True, False, False,
      "the planner converts this")
_kind("video/10-bit", True, False, False,
      "the planner re-encodes this")
_kind("subs/language", True, False, False,
      "the planner drops the subtitle")
_kind("subs/order", True, False, False,
      "the planner reorders the subtitle tracks")
_kind("subs/flags", True, False, False,
      "the planner clears the flag")
_kind("subs/default", True, False, False,
      "the planner clears the default flag")
_kind("subs/gap", True, False, False,
      "the subtitle rules have work for this file")
_kind("container/name", True, False, False,
      "this is a rename, not stream work")

# -- a tag, not a fault. Replaceable by hand, never unattended. ---------------
_kind("audio/untagged", True, True, False,
      "the track carries no language tag. The words may be right and only "
      "the label wrong, so this is never replaced unattended")

# -- index and bookkeeping. Nothing about the file is wrong. ------------------
# NOT A REQUEUE, AND SAYING SO OUT LOUD BECAUSE IT LOOKED LIKE ONE. The first
# version offered index/not-walked a requeue, which asks the planner to plan a
# file by its nuarr row id - and the entire finding is that there IS no nuarr
# row. It could only ever have answered "no such file". Indexing is the arr gap
# check's own pass and belongs to it.
_kind("index/not-walked", False, False, False,
      "nuarr has no row for a file the arr tracks. There is nothing to "
      "requeue and nothing wrong with the file - it needs indexing, which is "
      "this check's own job")
_kind("index/wrote-off", False, False, False,
      "nuarr wrote this off, but the arr still tracks it and it is on disk. "
      "The verdict is what is wrong, not the file - this check withdraws it")
# REPLACE WAS THE WRONG VERB FOR THIS AND FAILED HONESTLY, WHICH IS HOW IT WAS
# FOUND. refetch exists to reject a release because the bytes are bad: it
# blocklists the grab, deletes the file and asks for another. This finding is
# what that leaves BEHIND - the release is already blocklisted, the row is
# already marked deleted, and the replacement has simply not arrived. Pointing
# refetch at it fed its classifier refetch's own output as if it were an error
# string, and it refused with "this error has not been seen before" - correctly,
# because it was not an error at all.
#
# What is actually wanted is the second half of refetch on its own: ask the arr
# to search again. That is `reask`, below.
_kind("index/rejected", False, False, False,
      "already blocklisted once and still waiting. The release is gone and the "
      "file is already written off - there is nothing left to reject, only "
      "another search to ask for")
_kind("arr/disagree", True, False, False,
      "nuarr and the arr hold different facts about this file")
_kind("plex/disagree", True, False, False,
      "Plex is out of date about this file")
_kind("audio/title", True, False, False,
      "the audio picker's label does not match the stream")

# -- job failures that are nobody's fault, or the wrong kind of fault. --------
_kind("job/transient", True, False, False,
      "something was busy for a moment. Nothing is wrong with the file")
_kind("job/policy", True, False, False,
      "the path or the settings blocked this, not the file. Replacing it "
      "would delete a good file and land the replacement at the same path")
_kind("job/unknown", True, False, False,
      "this error has not been seen before, so no remedy is assumed")

# -- names that used to mean two things. -------------------------------------
# Rows written before audit.check() split them: bare "video" was BOTH "no video
# stream" and "the codec is wrong", and only one of those may be replaced. An
# ambiguous name resolves to the safe half - requeue only - because the whole
# point of splitting them was that guessing costs a file.
_kind("video", True, False, False,
      "a finding from before this rule was split in two, so it is not "
      "possible to tell which half it was. Requeue is the safe reading")
_kind("audio", True, True, False,
      "a finding from before this rule was named audio/missing. It almost "
      "certainly means the file has no sound, but 'almost' does not earn an "
      "unattended delete")

_FALLBACK = {"requeue": True, "replace": False, "auto_replace": False,
             "why": "an unrecognised finding - requeue is the only remedy "
                    "offered, because guessing the other one deletes files"}


# Findings whose remedy is neither of the file verbs: a wrong string somewhere,
# and the check that found it already owns the code that writes the right one.
# Held apart from _P rather than as a fourth column because it is a different
# kind of statement - not "what may be done to this file" but "this is not
# about the file".
_REPAIR_OF = {
    "arr/disagree": "arrsync",
    "plex/disagree": "plexsync",
    "audio/title": "audiotitle",
    # Indexing a file the arr tracks, and withdrawing a stale write-off, are
    # both the arr gap check's own pass. Same shape as the three above: the
    # file is fine, a record is wrong, and the check that found it owns the
    # code that puts it right.
    "index/not-walked": "arrgap",
    "index/wrote-off": "arrgap",
}


# Findings whose only sensible move is to ask the arr to look again.
_REASKABLE = {"index/rejected"}
# One search per file per this long. An indexer that is asked the same question
# every six hours is being used; one asked every thirty seconds is being
# abused, and the ban lands on the person who owns the account.
REASK_COOLDOWN_S = 6 * 3600


def policy(kind: str) -> dict:
    k = (kind or "").strip()
    out = dict(_P.get(k, _FALLBACK))
    out["repair"] = _REPAIR_OF.get(k, "")
    out["reask"] = k in _REASKABLE
    return out


def kinds() -> dict:
    """The whole table, for the settings page that explains itself."""
    return {k: policy(k) for k in sorted(_P)}


# --------------------------------------------------- the third verb's map ---
# system -> (what it is called on screen, the settings key that holds its
# auto/manual switch, the finding kind it produces). One row per check, so
# adding a check means adding a line here rather than teaching four call sites
# about it.
SYSTEMS = {
    "arrsync":    ("arr agreement check", "arrsync_mode", "arr/disagree"),
    "plexsync":   ("Plex agreement check", "plexsync_mode", "plex/disagree"),
    "audiotitle": ("audio picker check", "audiotitle_mode", "audio/title"),
    "arrgap":     ("not-walked check", "arrgap_mode", "index/not-walked"),
}


async def repair(system: str, source: str = "", auto: bool = False) -> dict:
    r"""Run a check's own in-place fixer, and write what it did to the ledger.

    THE POINT IS THE LEDGER, NOT THE DISPATCH. Each of these fixers was already
    reachable from its own card and worked perfectly; what was missing was that
    nothing counted them. Three checks correcting thousands of records were
    invisible next to a requeue of one file, so the only page that claims to
    say what nuarr has been doing to the library was telling less than half of
    it.
    """
    if system not in SYSTEMS:
        return {"ok": False, "why": f"no such check: {system}"}
    label, _key, kind = SYSTEMS[system]
    try:
        if system == "arrgap":
            from . import arrgap
            out = await arrgap.fix()
        elif system == "arrsync":
            from . import arrsync
            out = await arrsync.fix()
        elif system == "plexsync":
            import asyncio as _a
            from . import plexsync
            out = await _a.to_thread(plexsync.fix, 0, "")
        else:
            import asyncio as _a
            from . import audiotitle
            out = await _a.to_thread(audiotitle.fix)
    except Exception as e:                                       # noqa: BLE001
        _note(0, kind, REPAIR, source or system, auto, False,
              f"{type(e).__name__}: {e}")
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    out = out if isinstance(out, dict) else {"ok": True}
    if system == "arrgap":
        # arrgap.fix() answers with its whole cached report - the right thing
        # for the card that polls it and far too much to echo back here. The
        # number that matters is how many it actually indexed.
        from . import arrgap as _ag
        n = int((_ag._CACHE.get("last_fix") or {}).get("walked") or 0)
        out = {"ok": True, "walked": n}
    else:
        n = int(out.get("fixed") or out.get("done") or out.get("queued") or 0)
    # file_id 0 on purpose: this is one action over many records, and inventing
    # a file for it would put a lie in a table whose whole job is evidence.
    _note(0, kind, REPAIR, source or system, auto, True,
          f"{label}: corrected {n} record(s)" if n
          else f"{label}: nothing to correct")
    return {"ok": True, "fixed": n, **out}


# ------------------------------------------ every requeue, wherever it is ---
# ONE HOOK RATHER THAN EIGHT CALL SITES. jobs.enqueue is the single door every
# requeue in the app goes through - the subtitle-rules sweep, the OCR sweep,
# the rule audit, the buttons - so noting it there is the only version of this
# that cannot drift. A check added next year lands in the ledger by existing.
#
# Sources already written by the code that called them are skipped, or the cap
# would count one requeue twice and spend half the budget on arithmetic.
_NOTED_ELSEWHERE = ("remedy", "rule audit")
_SOURCE_KIND = {
    "subtitle rules changed": ("subs/gap", "rulesgap_mode"),
    "auto": ("subs/gap", "rulesgap_mode"),
    "not-walked check": ("index/not-walked", "arrgap_mode"),
    "found by the not-walked check": ("index/not-walked", "arrgap_mode"),
    "integrity": ("file/corrupt", "integrity_mode"),
}


def note_enqueue(file_id: int, source: str, path: str = "") -> None:
    src = (source or "").strip()
    if not src or any(src.startswith(x) for x in _NOTED_ELSEWHERE):
        return
    got = _SOURCE_KIND.get(src)
    if not got:
        return
    kind, key = got
    # A BUTTON PRESS IS NOT AUTO SPEND. The hourly ceiling exists to bound what
    # nuarr does when nobody is watching; charging a person's own click against
    # it would mean the more you supervised it, the less it was allowed to do.
    auto = str(getattr(SETTINGS, key, "manual") or "manual").lower() == "auto"
    _note(int(file_id), kind, REQUEUE, src, auto, True,
          f"queued by the {src}", path or "")


# ------------------------------------------------------------- the caps -----
# ONE BUDGET, NOT SEVEN. Each system's own cap was reasonable in isolation and
# meaningless together, because nothing added them up. These are rolling-hour
# ceilings across every source, counted from the ledger below - so a rule edit
# that suddenly makes ten thousand files "wrong" costs at most this many
# actions per hour no matter how many checks notice it at once.
#
# Requeue is generous because its worst case is wasted GPU time. Replace is
# small because its worst case is deleted media.
AUTO_REQUEUE_PER_HOUR = 60
AUTO_REPLACE_PER_HOUR = 6

# How many times one file may be requeued for one finding before the loop is
# called a loop. Past this, the finding needs a person: the plan is not fixing
# what the check measures, and another attempt will not discover that.
MAX_REQUEUES = 3
# And a file that has been replaced twice is not being fixed by replacement.
MAX_REPLACES = 2

_READY = False


def init() -> None:
    """The ledger. Append-only on purpose - it is evidence, not state."""
    global _READY
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS remedy_log(
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                at      REAL    NOT NULL,
                file_id INTEGER NOT NULL,
                kind    TEXT    NOT NULL,
                action  TEXT    NOT NULL,
                source  TEXT,
                auto    INTEGER NOT NULL DEFAULT 0,
                ok      INTEGER NOT NULL DEFAULT 0,
                detail  TEXT,
                path    TEXT
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_remedy_at "
                    "ON remedy_log(at)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_remedy_file "
                    "ON remedy_log(file_id, kind)")
    _READY = True


def _note(file_id: int, kind: str, action: str, source: str, auto: bool,
          ok: bool, detail: str, path: str = "") -> None:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO remedy_log(at,file_id,kind,action,source,auto,"
                "ok,detail,path) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), int(file_id), kind, action, source or "",
                 1 if auto else 0, 1 if ok else 0, (detail or "")[:600],
                 path or ""))
    except Exception:                                            # noqa: BLE001
        pass


def attempts(file_id: int, kind: str = "") -> dict:
    """How often this has been tried on this file, and how it went."""
    if not _READY:
        init()
    q = ("SELECT action, ok, COUNT(*) n, MAX(at) last FROM remedy_log "
         "WHERE file_id=?")
    args: list = [int(file_id)]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    out = {REQUEUE: 0, REPLACE: 0, "last": 0.0}
    try:
        with cursor() as cur:
            for r in cur.execute(q + " GROUP BY action, ok", args):
                if r["ok"]:
                    out[r["action"]] = out.get(r["action"], 0) + r["n"]
                out["last"] = max(out["last"], r["last"] or 0.0)
    except Exception:                                            # noqa: BLE001
        pass
    return out


def _spent(action: str, since_s: float = 3600.0) -> int:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            r = cur.execute(
                "SELECT COUNT(*) n FROM remedy_log WHERE action=? AND auto=1 "
                "AND ok=1 AND at > ?",
                (action, time.time() - since_s)).fetchone()
        return int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        return 0


def budget() -> dict:
    """What auto mode has left this hour, for the panel."""
    rq, rp = _spent(REQUEUE), _spent(REPLACE)
    return {"requeue": {"spent": rq, "cap": AUTO_REQUEUE_PER_HOUR,
                        "left": max(0, AUTO_REQUEUE_PER_HOUR - rq)},
            "replace": {"spent": rp, "cap": AUTO_REPLACE_PER_HOUR,
                        "left": max(0, AUTO_REPLACE_PER_HOUR - rp)}}


# ------------------------------------------------------------- the offers ---
def offers(kind: str, file_id: int | None = None) -> dict:
    r"""What the UI should show for this finding, and why anything is refused.

    Returns both remedies always, each with `on` and a `why`. A greyed button
    that explains itself is worth more than an absent one: the absent one reads
    as an oversight, and somebody goes looking for the feature.
    """
    p = policy(kind)
    a = attempts(int(file_id), kind) if file_id else {REQUEUE: 0, REPLACE: 0}
    out = {"kind": kind, "why": p["why"],
           # The two verbs that are not about the file's bytes. Reported here
           # as well as in kinds(), so one call answers "what can I do with
           # this row" completely rather than nearly.
           REPAIR: {"on": bool(p["repair"]), "system": p["repair"],
                    "why": p["why"]},
           REASK: {"on": bool(p["reask"]), "why": p["why"],
                   "tried": a.get(REASK, 0)},
           REQUEUE: {"on": p["requeue"], "why": p["why"],
                     "tried": a.get(REQUEUE, 0)},
           REPLACE: {"on": p["replace"], "why": p["why"],
                     "tried": a.get(REPLACE, 0),
                     "auto": p["auto_replace"]}}
    if not p["requeue"]:
        out[REQUEUE]["why"] = ("no rule the planner has would change this - "
                               + p["why"])
    elif a.get(REQUEUE, 0) >= MAX_REQUEUES:
        out[REQUEUE]["on"] = False
        out[REQUEUE]["why"] = (
            f"requeued {a[REQUEUE]}x already and still found. The plan is not "
            f"fixing what the check measures, so another attempt will not "
            f"either")
    if not p["replace"]:
        out[REPLACE]["why"] = ("the file's contents are not the problem - "
                               + p["why"])
    elif a.get(REPLACE, 0) >= MAX_REPLACES:
        out[REPLACE]["on"] = False
        out[REPLACE]["why"] = (
            f"already replaced {a[REPLACE]}x and still found. Another "
            f"release is not the answer here")
    return out


# ------------------------------------------------------------- the verbs ----
async def requeue(file_id: int, kind: str, source: str = "", why: str = "",
                  auto: bool = False) -> dict:
    """Offer the file to the planner.

    READS THE FILE BEFORE DECIDING, by handing the decision to jobs.enqueue,
    which probes. A finding can be minutes or weeks old and the file may have
    been dealt with in between; NothingToDo is the planner saying exactly that
    and is recorded as such rather than as a failure.
    """
    from . import jobs
    p = policy(kind)
    if not p["requeue"]:
        return {"ok": False, "why": f"requeue is not a remedy for {kind}: "
                                    f"{p['why']}"}
    a = attempts(file_id, kind)
    if a.get(REQUEUE, 0) >= MAX_REQUEUES:
        return {"ok": False, "why": f"already requeued {a[REQUEUE]}x for "
                                    f"{kind} and still found"}
    if auto and _spent(REQUEUE) >= AUTO_REQUEUE_PER_HOUR:
        return {"ok": False, "why": "auto mode has spent this hour's requeue "
                                    "budget", "capped": True}
    with cursor() as cur:
        row = cur.execute("SELECT id,path,title,state FROM files WHERE id=?",
                          (int(file_id),)).fetchone()
    if not row:
        # "no such file" WAS TRUE AND USELESS. It was answered about a file
        # sitting on the pool, because the caller had handed over the ARR's
        # file id and this table is keyed by nuarr's. Two id spaces, one field
        # name. Say which one was expected, so the next person to hit it is
        # reading about the bug rather than hunting for a missing file.
        return {"ok": False,
                "why": f"nuarr has no row with id {int(file_id)}. This wants "
                       f"nuarr's own file id, not the arr's - if this came "
                       f"from a card that lists arr records, it is sending "
                       f"the wrong one"}
    row = dict(row)
    try:
        await jobs.enqueue(int(file_id), row["path"], row.get("title") or "",
                           source=source or "remedy", priority=60)
    except jobs.NothingToDo:
        _note(file_id, kind, REQUEUE, source, auto, False,
              "the planner has no work for this file", row["path"])
        return {"ok": False, "nothing_to_do": True,
                "why": "the planner re-read the file and has no work for it"}
    except ValueError:
        return {"ok": True, "already": True,
                "why": "a job for this file is already queued"}
    except Exception as e:                                       # noqa: BLE001
        _note(file_id, kind, REQUEUE, source, auto, False,
              f"{type(e).__name__}: {e}", row["path"])
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    _note(file_id, kind, REQUEUE, source, auto, True,
          why or f"queued to fix {kind}", row["path"])
    try:
        await jobs.start()
    except Exception:                                            # noqa: BLE001
        pass
    return {"ok": True, "why": f"queued to fix {kind}"}


async def reask(file_id: int, kind: str, source: str = "",
                auto: bool = False) -> dict:
    r"""Ask the arr to search for this one again. Touches nothing.

    No blocklist, no delete, no job - one command to Sonarr or Radarr saying
    "look again for this episode". The whole cost is an indexer search, and the
    whole risk is asking for it too often, so that is the only thing guarded
    here.
    """
    from .arr import shared_client
    from .refetch import _arr_for, _file_row
    if not policy(kind)["reask"]:
        return {"ok": False, "why": f"asking again is not a remedy for {kind}"}
    row = _file_row(int(file_id))
    if not row:
        return {"ok": False,
                "why": f"nuarr has no row with id {int(file_id)} - this wants "
                       f"nuarr's own file id, not the arr's"}
    if not row.get("arr_parent_id"):
        return {"ok": False, "why": "no series or film id on this row, so "
                                    "there is nothing to search for"}
    a = attempts(file_id, kind)
    if a.get("last") and (time.time() - a["last"]) < REASK_COOLDOWN_S:
        left = int((REASK_COOLDOWN_S - (time.time() - a["last"])) / 60)
        return {"ok": False, "why": f"already asked within the last "
                                    f"{REASK_COOLDOWN_S // 3600}h - {left}m "
                                    f"before it is worth asking again"}
    cfg = _arr_for(row.get("library") or "")
    if cfg is None:
        return {"ok": False, "why": f"no arr is configured for the library "
                                    f"{row.get('library') or '?'}"}
    try:
        await shared_client(cfg).search_for(int(row["arr_parent_id"]))
    except Exception as e:                                       # noqa: BLE001
        _note(file_id, kind, REASK, source, auto, False,
              f"{type(e).__name__}: {e}", row.get("path") or "")
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    _note(file_id, kind, REASK, source, auto, True,
          f"asked {cfg.name} to search again", row.get("path") or "")
    joblog.log(f"{source or 'remedy'}: asked {cfg.name} to search again for "
               f"{row.get('title') or row.get('path') or file_id}", "info")
    return {"ok": True, "why": f"asked {cfg.name} to search again"}


async def replace(file_id: int, kind: str, source: str = "", why: str = "",
                  auto: bool = False) -> dict:
    """Blocklist the release and ask the arr for another one. Destructive.

    Everything about HOW is refetch's - finding the grab, blocklisting it,
    falling back to delete-and-search, treating a 404 as stale bookkeeping.
    This decides only WHETHER, which is the part refetch cannot know.
    """
    from . import refetch
    p = policy(kind)
    if not p["replace"]:
        return {"ok": False, "why": f"replacing is not a remedy for {kind}: "
                                    f"{p['why']}"}
    if auto and not p["auto_replace"]:
        return {"ok": False, "why": f"{kind} is replaceable, but never "
                                    f"unattended: {p['why']}"}
    from .refetch import _file_row
    if not _file_row(int(file_id)):
        # THE SAME TWO-ID-SPACES TRAP THE REQUEUE PATH HAD. refetch answers
        # "no such file" from three frames down, which reads as a missing file
        # rather than a caller handing over the arr's id.
        return {"ok": False,
                "why": f"nuarr has no row with id {int(file_id)}. This wants "
                       f"nuarr's own file id, not the arr's"}
    a = attempts(file_id, kind)
    if a.get(REPLACE, 0) >= MAX_REPLACES:
        return {"ok": False, "why": f"already replaced {a[REPLACE]}x for "
                                    f"{kind} and still found"}
    if auto and _spent(REPLACE) >= AUTO_REPLACE_PER_HOUR:
        return {"ok": False, "why": "auto mode has spent this hour's "
                                    "replacement budget", "capped": True}
    try:
        out = await refetch.run(int(file_id),
                                reason=why or policy(kind)["why"])
    except Exception as e:                                       # noqa: BLE001
        _note(file_id, kind, REPLACE, source, auto, False,
              f"{type(e).__name__}: {e}")
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    _note(file_id, kind, REPLACE, source, auto, bool(out.get("ok")),
          "; ".join(out.get("did") or []) or out.get("why") or "",
          out.get("path") or "")
    if out.get("ok"):
        joblog.log(f"{source or 'remedy'}: replaced a release - {kind}: "
                   f"{policy(kind)['why']}", "warn")
    return out


# --------------------------------------------------------- the auto pass ----
async def auto(findings: list, source: str, enabled: bool) -> dict:
    r"""The shared unattended pass. Every check calls this and none implement it.

    `findings` is [{file_id, kind, path?, why?}]. Order matters and is the
    caller's: it knows which of its findings are freshest, and fresh is what a
    person notices.

    REQUEUE FIRST, ALWAYS. A finding that both verbs claim gets the reversible
    one until it has been requeued its limit, and only then the destructive
    one. That ordering is the whole safety argument: the only files auto mode
    can delete are ones the planner has already been given, and failed, three
    times - or ones where the planner was never a candidate because the bytes
    themselves are wrong.
    """
    out = {"requeued": 0, "replaced": 0, "skipped": 0, "capped": False,
           "why": ""}
    if not enabled:
        out["why"] = "manual mode"
        return out
    for f in findings:
        kind = f.get("kind") or ""
        fid = int(f.get("file_id") or 0)
        if not fid:
            out["skipped"] += 1
            continue
        p = policy(kind)
        a = attempts(fid, kind)
        did = None
        if p["requeue"] and a.get(REQUEUE, 0) < MAX_REQUEUES:
            did = await requeue(fid, kind, source=source,
                                why=f.get("why") or "", auto=True)
            if did.get("ok"):
                out["requeued"] += 1
                continue
            if did.get("capped"):
                out["capped"] = True
                break
        if p["replace"] and p["auto_replace"]:
            did = await replace(fid, kind, source=source,
                                why=f.get("why") or "", auto=True)
            if did.get("ok"):
                out["replaced"] += 1
                continue
            if did.get("capped"):
                out["capped"] = True
                break
        out["skipped"] += 1
    if out["requeued"] or out["replaced"]:
        joblog.log(f"{source} (auto mode): {out['requeued']} requeued, "
                   f"{out['replaced']} release(s) replaced",
                   "warn" if out["replaced"] else "ok")
    return out


def mode_of(system: str) -> str:
    """"manual" or "auto" for any of the checks, by its settings key."""
    m = str(getattr(SETTINGS, f"{system}_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


# ------------------------------------------------ acting while it matters ---
# WAITING FOR THE NEXT SWEEP IS NOT A REMEDY.
#
# Every check here runs on a timer measured in hours: the rule audit nightly,
# the arr gap check six-hourly, the integrity sweep on its own slow clock. That
# is the right cadence for LOOKING - reading 39,000 files more often would cost
# more than it finds. It is the wrong cadence for ACTING. A file that arrived
# broken at seven in the evening was found at three the following morning and
# replaced some time after that, and in between it sat in the library being
# offered to whoever pressed play.
#
# So the moment a file is probed - a job just rewrote it, or the change watcher
# saw it appear and the rescan read it - it is checked against the rules there
# and then, and anything worth acting on is written here. The drain below picks
# it up within a minute. The sweeps still run; they are the safety net rather
# than the mechanism.
#
# WHY A TABLE AND NOT A SET IN MEMORY. A restart between "found" and "acted" is
# not rare - it is what happens every time nuarr updates itself - and an
# in-memory queue turns that into a finding that was seen once and never again
# until the next full sweep hours later. Which is the delay this exists to
# remove.
FRESH_SETTLE_S = 120      # let the file finish being whatever it is becoming
FRESH_CYCLE_S = 45


def _fresh_init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS remedy_fresh(
                file_id INTEGER NOT NULL,
                kind    TEXT    NOT NULL,
                at      REAL    NOT NULL,
                why     TEXT,
                PRIMARY KEY (file_id, kind)
            )""")


def reconsider(file_id: int, probe: dict | None = None) -> int:
    r"""A new probe landed. Is there anything here worth acting on now?

    Called from jobs.store_probe, on the same breath as the subtitle-rules
    reconsider - which is the established place for "this file just became a
    different file". Cheap on purpose: it reads a probe that is already in
    hand and writes at most a row or two. Anything expensive belongs in the
    sweeps.

    Returns how many findings were parked.
    """
    if not probe:
        return 0
    try:
        from . import audit, rules
        with cursor() as cur:
            r = cur.execute("SELECT path, library, state FROM files "
                            "WHERE id=?", (int(file_id),)).fetchone()
        if not r or (r["state"] or "") in ("deleted", "duplicate"):
            return 0
        path, lib = r["path"] or "", r["library"] or ""
        found = audit.check(probe, rules.is_anime(path), lib, path)
    except Exception:                                            # noqa: BLE001
        return 0
    now, n = time.time(), 0
    try:
        _fresh_init()
        with cursor() as cur:
            # ONLY WHAT A REMEDY EXISTS FOR. The rule check reports everything
            # it finds because reporting is free; this parks only findings one
            # of the two verbs can actually answer, so the drain never walks a
            # list of things it will decline one at a time.
            for rule, got, want in found:
                p = policy(rule)
                if not (p["requeue"] or p["replace"]):
                    continue
                cur.execute(
                    "INSERT INTO remedy_fresh(file_id,kind,at,why) "
                    "VALUES(?,?,?,?) ON CONFLICT(file_id,kind) DO UPDATE SET "
                    "at=excluded.at, why=excluded.why",
                    (int(file_id), rule, now, f"{got} - should be {want}"))
                n += 1
    except Exception:                                            # noqa: BLE001
        return 0
    return n


def _fresh_take(limit: int = 25) -> list[dict]:
    """Findings that have sat long enough to be real. Removed as they are read.

    Taken rather than read: a finding that is acted on and still true will be
    parked again by the next probe, and one that was acted on and fixed should
    not come round a second time. Leaving rows behind for the drain to re-skip
    every 45 seconds is how a queue becomes a busy loop.
    """
    try:
        _fresh_init()
        with cursor() as cur:
            rows = [dict(r) for r in cur.execute(
                "SELECT file_id, kind, at, why FROM remedy_fresh "
                "WHERE at < ? ORDER BY at LIMIT ?",
                (time.time() - FRESH_SETTLE_S, int(limit)))]
            for r in rows:
                cur.execute("DELETE FROM remedy_fresh WHERE file_id=? AND "
                            "kind=?", (r["file_id"], r["kind"]))
        return rows
    except Exception:                                            # noqa: BLE001
        return []


def fresh_pending() -> int:
    try:
        _fresh_init()
        with cursor() as cur:
            r = cur.execute("SELECT COUNT(*) n FROM remedy_fresh").fetchone()
        return int(r["n"] or 0)
    except Exception:                                            # noqa: BLE001
        return 0


async def watch() -> None:
    r"""Drain the fresh findings on a short clock.

    GOVERNED BY THE RULE CHECK'S OWN SWITCH, not a new one. Everything parked
    here is a rule-check finding read from a rule-check probe; giving it a
    second auto/manual switch would mean a person could turn the rule check to
    manual and still have nuarr deleting files on its behalf. In manual mode
    the findings are still parked and still drained - they are simply drained
    into the list rather than into an action.
    """
    from . import audit
    await asyncio.sleep(90)
    while True:
        try:
            rows = await asyncio.to_thread(_fresh_take, 25)
            if rows:
                await auto(rows, "settling check", audit.mode() == "auto")
        except Exception:                                        # noqa: BLE001
            pass
        await asyncio.sleep(FRESH_CYCLE_S)


# ------------------------------------------------------------- the ledger ---
def recent(limit: int = 80) -> list[dict]:
    if not _READY:
        init()
    try:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT l.*, f.title FROM remedy_log l "
                "LEFT JOIN files f ON f.id = l.file_id "
                "ORDER BY l.at DESC LIMIT ?", (int(limit),))]
    except Exception:                                            # noqa: BLE001
        return []


def stats() -> dict:
    if not _READY:
        init()
    out = {"budget": budget(), "day": {REQUEUE: 0, REPLACE: 0},
           "loops": 0, "last_at": 0.0}
    try:
        with cursor() as cur:
            for r in cur.execute(
                    "SELECT action, COUNT(*) n FROM remedy_log "
                    "WHERE ok=1 AND at > ? GROUP BY action",
                    (time.time() - 86400,)):
                out["day"][r["action"]] = r["n"]
            r = cur.execute("SELECT MAX(at) m FROM remedy_log").fetchone()
            out["last_at"] = float((r["m"] if r else 0) or 0)
            # A LOOP IS A FILE THIS HAS GIVEN UP ON. Worth a number of its own:
            # it is the only figure here that means "a person is needed".
            r = cur.execute(
                "SELECT COUNT(*) n FROM (SELECT file_id, kind, COUNT(*) c "
                "  FROM remedy_log WHERE action=? AND ok=1 "
                "  GROUP BY file_id, kind HAVING c >= ?)",
                (REQUEUE, MAX_REQUEUES)).fetchone()
            out["loops"] = int((r["n"] if r else 0) or 0)
    except Exception:                                            # noqa: BLE001
        pass
    return out
