r"""
nuarr - does the library actually match the rules?

WHY THIS RUNS ON A TIMER
------------------------
rule_audit.py was a script I ran by hand once. On that single run it found two
real planner bugs: unwanted subtitle tracks that were excluded but never
removed (658 files kept foreign-language subs), and no-op actions that marked
9,220 files as needing a full rewrite to achieve nothing. Both had been true
for a long time and neither was visible anywhere.

A check that only runs when somebody remembers is not a guarantee. This is the
same set of checks on a nightly timer, with the results kept so a regression
shows up as a change rather than as a number nobody has a baseline for.

WHAT IT CHECKS, AND WHY IT READS THE FILE
-----------------------------------------
decide() answers "is there work outstanding", which is necessary but not
sufficient: it knows nothing about the ORDER tracks ended up in or the
dispositions they carry, and both of those are things the rules exist to fix.
So the sample is probed from disk and the invariants are checked against the
actual bytes.

Two different verdicts, deliberately kept apart:

    VIOLATION - the file contradicts a rule. Either a rule is not doing what it
                claims, or something wrote the file after nuarr did.
    PENDING   - the file predates a rule and has not been reprocessed yet.
                Normal on a library mid-backlog, and not a defect.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

from . import joblog, rules
from .config import SETTINGS, hidden_si
from . import schedules
from .db import cursor

POLL_S = 24 * 3600            # fallback only; the live value is audit_every_h
PER_BUCKET = 6
# How many files may be read at once. Measured: the sample lands across twelve
# pool disks, the probes are seek-bound rather than CPU-bound, and eight lanes
# took a run from ~70s to ~9s here. Higher stops helping and starts competing
# with whatever else is reading the pool - which the gate would then have to
# steer around.
PROBE_LANES = 8


def _every_hours() -> float:
    from . import workers
    try:
        return round(workers.tune("audit_every_h"), 1)
    except Exception:
        return round(POLL_S / 3600)

# Each bucket is a distinct rule surface. A uniform random sample of 39,000
# files is mostly 1080p anime episodes and would say nothing about 4K HDR,
# Dolby Vision, dual audio or PGS - which is where the interesting rules are.
BUCKETS = {
    "4K HDR":           "f.height >= 1700 AND p.json LIKE '%smpte2084%'",
    "4K SDR":           "f.height >= 1700 AND p.json NOT LIKE '%smpte2084%'",
    "1080p h264":       "f.height BETWEEN 900 AND 1200 AND f.video_codec='h264'",
    "1080p HEVC":       "f.height BETWEEN 900 AND 1200 AND f.video_codec='hevc'",
    "10-bit anime":     "p.json LIKE '%yuv420p10le%' AND f.path LIKE '%Anime%'",
    "dual-audio anime": "f.path LIKE '%Anime%' AND f.audio_codecs LIKE '%,%'",
    "has PGS subs":     "p.json LIKE '%hdmv_pgs_subtitle%'",
    "was Dolby Vision": "f.path LIKE '%DV%' OR p.json LIKE '%dv_profile%'",
    "SD / old TV":      "f.height < 800",
    "movies":           "f.library LIKE '%Movie%'",
}

# live_checked / heal_* exist so the panel can SHOW the check working. Both a
# run (ffprobe x60 off spinning disks) and a heal (ffprobe per flagged file)
# take real time, and a panel that only repaints when they finish reads as
# frozen for exactly that long - which got reported as "clicking locks the
# panel" when the click was merely waiting.
STATS: dict = {"last_run": 0.0, "next_run": 0.0, "running": False,
               "checked": 0, "violations": 0, "pending": 0, "last_error": "",
               "healed": 0, "unfixable": 0, "heal_error": "",
               "live_checked": 0, "heal_running": False, "heal_done": 0,
               "heal_total": 0, "heal_current": ""}

# ---------------------------------------------------------------- healing ---
# WHY THE AUDIT REQUEUES RATHER THAN JUST REPORTING
#
# A finding is a file that contradicts a rule. For most rules the fix already
# exists - the planner would rewrite the file correctly if it were asked - and
# the only reason the file is still wrong is that nobody asked. Leaving that in
# a panel for a human to click is busywork: the same six rows, every night,
# until somebody notices.
#
# So: after a run, every violating file is offered to the planner. That is the
# authoritative test, because it is the same code the queue uses. Two answers,
# and they mean completely different things:
#
#   the planner has work  -> the rules CAN fix this. Queue it. The finding
#                            should be gone by the next run.
#   NothingToDo           -> the rules CANNOT fix this. The file breaks a rule
#                            the planner does not act on, which is a GAP IN THE
#                            RULES, not a backlog item. Retrying it forever
#                            would be a loop that never converges.
#
# The second answer is the valuable one and it is why this is worth building.
# subs/order is the live example: the audit checks that text dialogue sits
# ahead of picture subtitles, and nothing in rules.py reorders subtitle tracks,
# so those files were re-reported on every run and no button would ever clear
# them. Naming that as unfixable turns a nagging row into a decision.
#
# LOOP GUARDS, because a self-healer that can requeue is a self-healer that can
# thrash a 57 TB library:
#   * MAX_HEAL_ATTEMPTS - a file the planner claimed to fix, twice, and which
#     is still flagged, is not going to be fixed by a third try. Give up and
#     say so, loudly, because that combination means the planner is reporting
#     work it does not actually perform.
#   * MAX_PER_RUN - bounded like the sample itself. A rule change that suddenly
#     invalidates thousands of files must not enqueue thousands of rewrites in
#     one unattended night.
#   * 'unfixable' and 'gave-up' are terminal. Nothing re-attempts them until a
#     human presses the button, by which time they have presumably changed a
#     rule - which is the actual fix.
HEAL_ENABLED = True
MAX_HEAL_ATTEMPTS = 2
MAX_PER_RUN = 20

# Terminal states never re-attempted automatically.
_TERMINAL = ("unfixable", "gave-up")

# Set once init() has run, so latest() can create its tables on first read
# without paying for it on every 60-second poll thereafter.
_READY = False


def init() -> None:
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_runs(
                id        INTEGER PRIMARY KEY,
                at        REAL NOT NULL,
                checked   INTEGER, violations INTEGER, pending INTEGER,
                by_rule   TEXT)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_findings(
                id      INTEGER PRIMARY KEY,
                run_id  INTEGER, at REAL, file_id INTEGER,
                bucket  TEXT, path TEXT, rule TEXT, detail TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_af_run "
                    "ON audit_findings(run_id)")
        # found/want split the old prose detail into the two halves every
        # violation actually consists of. Older rows keep detail only and the
        # panel falls back to it.
        for col in ("found", "want"):
            try:
                cur.execute(f"ALTER TABLE audit_findings ADD COLUMN {col} TEXT")
            except Exception:
                pass                                  # already migrated
        # One row per file the audit has ever tried to heal. Keyed on file_id
        # rather than on the finding, because the question "has this file been
        # through the healer" outlives any single run and has to survive the
        # 60-day pruning of findings above.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_heals(
                file_id   INTEGER PRIMARY KEY,
                rule      TEXT,
                attempts  INTEGER NOT NULL DEFAULT 0,
                first_at  REAL, last_at REAL,
                state     TEXT,
                detail    TEXT,
                path      TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_ah_state "
                    "ON audit_heals(state)")


def _ffprobe() -> str:
    """The SAME ffprobe the jobs use.

    The first version guessed at a SETTINGS.ffmpeg_dir that does not exist,
    fell back to bare "ffprobe" on PATH, found nothing, and every probe
    returned None - so the run reported "0 files checked" and looked like a
    clean result. An audit that silently measures nothing is worse than no
    audit, and it must read through exactly the binary the pipeline uses or it
    is not auditing the pipeline.
    """
    from .jobs import _ffprobe_exe
    return _ffprobe_exe()


def probe(path: str) -> dict | None:
    try:
        out = subprocess.run(
            [_ffprobe(), "-v", "error", "-show_streams", "-show_format",
             "-of", "json", path],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=hidden_si()).stdout
        return json.loads(out) if out.strip() else None
    except Exception:
        return None


def _t(s: dict) -> str:
    return (s.get("tags") or {}).get("title") or ""


def _lang(s: dict) -> str:
    return ((s.get("tags") or {}).get("language") or "").lower()


def _disp(s: dict, f: str) -> bool:
    return bool((s.get("disposition") or {}).get(f))


# Language NAME -> ISO 639-2, for the one place the two have to meet: the arrs
# report originalLanguage as a word ("Japanese"), and every track tag in a file
# is a code ("jpn"). Only the languages that actually appear as an original
# language in this library, plus the obvious neighbours - a name that is not
# here simply means the check does not fire, which is the safe direction.
_ISO3 = {
    "english": "eng", "japanese": "jpn", "korean": "kor", "chinese": "zho",
    "mandarin": "zho", "cantonese": "zho", "french": "fra", "german": "deu",
    "spanish": "spa", "italian": "ita", "portuguese": "por", "russian": "rus",
    "dutch": "nld", "swedish": "swe", "norwegian": "nor", "danish": "dan",
    "finnish": "fin", "polish": "pol", "czech": "ces", "hungarian": "hun",
    "turkish": "tur", "arabic": "ara", "hebrew": "heb", "hindi": "hin",
    "thai": "tha", "vietnamese": "vie", "indonesian": "ind", "greek": "ell",
    "ukrainian": "ukr", "romanian": "ron", "bulgarian": "bul",
    "croatian": "hrv", "serbian": "srp", "slovak": "slk", "catalan": "cat",
    "icelandic": "isl", "tamil": "tam", "telugu": "tel", "malayalam": "mal",
}


# ONE LANGUAGE, TWO SPELLINGS. ISO 639-2 has a bibliographic set and a
# terminological set, and ffprobe emits whichever the muxer wrote: chi/zho,
# fre/fra, ger/deu, dut/nld and the rest are the SAME LANGUAGE. The first live
# run of the language check proved why this matters - it flagged Ne Zha 2, a
# Chinese film in a library that keeps zho, because the file said chi. A check
# that cannot spell its own subject reports a correct file as wrong, and in
# auto mode would have deleted it.
_ISO_ALIAS = {
    "chi": "zho", "cze": "ces", "dut": "nld", "fre": "fra", "ger": "deu",
    "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri", "may": "msa",
    "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym",
    "arm": "hye", "baq": "eus", "bur": "mya", "geo": "kat",
}


def _lang_key(code: str) -> str:
    """One canonical spelling per language, so chi and zho compare equal."""
    c = str(code or "").strip().lower()[:3]
    return _ISO_ALIAS.get(c, c)


def check(pr: dict, anime: bool, library: str = "",
          path: str = "") -> list[tuple[str, str, str]]:
    """Invariants the rules promise, checked against the real streams.

    Each finding is (rule, found, want): WHAT THE FILE HAS and WHAT THE RULES
    SAY IT SHOULD BE, as two short values rather than one prose sentence. The
    old single-string details buried the comparison in the middle of a clause -
    "picture sub 0 sits before the text dialogue track 1" makes the reader
    reconstruct both halves themselves. Every violation IS a comparison; the
    data should carry it as one.
    """
    bad: list[tuple[str, str, str]] = []

    def flag(rule: str, found: str, want: str) -> None:
        bad.append((rule, found, want))

    C = rules.CONFIG
    # THE AUDIT AND THE PLANNER MUST READ THE SAME SETTINGS.
    #
    # Codec settings are per library now, and this used to read the globals.
    # That is precisely the failure this audit already caught once: an eac3 7.1
    # track was flagged by audit/channels while decide() reported the file
    # correct, because the two were reading the same config to opposite
    # conclusions - and the Requeue button answered "nothing to do", which
    # looked like the audit was broken rather than the planner. Leaving the
    # globals here would have recreated it the first time anyone raised the
    # channel ceiling for one library.
    try:
        from . import codecpolicy
        AP = codecpolicy.for_library(library, "audio")
        VP = codecpolicy.for_library(library, "video")
    except Exception:
        AP = VP = {}
    max_ch = int(AP.get("surround_max_channels", C["surroundMaxChannels"]))
    eac3_max = int(AP.get("eac3_max_bitrate_k", C["eac3MaxBitrateK"]))
    dv_profiles = [int(x) for x in VP.get("strip_dv_profiles",
                                          C["stripDVProfiles"])] \
        if VP.get("strip_dv", C["stripDV"]) else []
    streams = pr.get("streams") or []
    fmt = pr.get("format") or {}
    vid = [s for s in streams if s.get("codec_type") == "video"
           and not _disp(s, "attached_pic")]
    aud = [s for s in streams if s.get("codec_type") == "audio"]
    sub = [s for s in streams if s.get("codec_type") == "subtitle"]

    if "matroska" not in (fmt.get("format_name") or ""):
        flag("container", f"{fmt.get('format_name') or 'unknown'} container",
             "Matroska (.mkv)")
    else:
        # THE NAME HAS TO AGREE WITH THE CONTAINER.
        #
        # This check did not exist, and 148 files were Matroska wearing .mp4,
        # .m4v or .avi - written by nuarr's own commit, which replaced the
        # original path and so kept the original extension. The audit read
        # format_name, correctly saw "matroska", and passed every one of them.
        # A check that is right about the content and silent about the name
        # will never find a file whose content and name disagree.
        name = fmt.get("filename") or ""
        ext = os.path.splitext(name)[1].lower()
        if name and ext and ext != ".mkv":
            flag("container/name", f"Matroska content named {ext}",
                 "named .mkv")
    if not vid:
        flag("video", "no video stream", "one video stream")
        return bad

    v = vid[0]
    if (v.get("codec_name") or "").lower() not in ("h264", "hevc", "h265", "av1"):
        flag("video", f"{v.get('codec_name') or 'unknown'} video",
             "h264, HEVC or AV1")
    for sd in v.get("side_data_list") or []:
        if "dv_profile" in sd and int(sd["dv_profile"]) in dv_profiles:
            flag("video/DV", f"Dolby Vision profile {sd['dv_profile']}",
                 "plain HDR10, DV layer removed")
    if "10" in (v.get("profile") or "") and "10" not in (v.get("pix_fmt") or ""):
        flag("video/10-bit", f"{v.get('profile')} at {v.get('pix_fmt')}",
             "10-bit pixels kept 10-bit")

    if not aud:
        flag("audio", "no audio stream", "at least one audio track")
    langs: dict[str, int] = {}
    for a in aud:
        ac = (a.get("codec_name") or "").lower()
        ch = int(a.get("channels") or 0)
        br_k = int(a.get("bit_rate") or 0) // 1000
        if ac == "eac3" and ch > 2 and eac3_max > 0 and br_k > eac3_max:
            flag("audio/EAE", f"E-AC3 {ch}ch at {br_k}k",
                 f"{eac3_max}k or less (Plex's decoder limit)")
        if ch > max_ch:
            flag("audio/channels", f"{ac} {_chname(ch)}",
                 f"{_chname(max_ch)} or less")
        if AP.get("drop_commentary", True) and C["commentaryPattern"].search(_t(a)):
            flag("audio/commentary", f"commentary track: {_t(a)[:36]!r}",
                 "removed")
        # Same language guesser as the planner (tag first, then the track
        # title), and the same refusal: two tracks that BOTH lack a language
        # are not evidence of a duplicate, they are a dual-audio release with
        # lazy tagging - flagging them here would re-open the exact DAIMA trap
        # the planner just closed.
        lg = rules._lang_guess(a) or "und"
        langs[lg] = langs.get(lg, 0) + 1
    if C["dedupeAudioPerLang"]:
        for lg, n in langs.items():
            if n > 1 and lg != "und":
                flag("audio/dedupe", f"{n} audio tracks for {lg!r}",
                     "one per language")

    img = [i for i, s in enumerate(sub)
           if (s.get("codec_name") or "").lower() in rules.IMAGE_SUB_CODECS]
    txt = [i for i, s in enumerate(sub)
           if (s.get("codec_name") or "").lower() in rules.TEXT_SUB_CODECS]
    for i, s in enumerate(sub):
        lg = _lang(s)
        if lg and lg not in C["keepSubLangs"] and lg not in ("und",):
            flag("subs/language", f"{lg} subtitle", "English only")
    # Order only matters for the DIALOGUE track: Plex picks forced tracks by
    # flag, not position, so a forced line appended after the images is fine.
    dial = [i for i in txt if not (_disp(sub[i], "forced")
                                   or rules.SIGNS_TITLE_RE.search(_t(sub[i])))]
    if dial and img and max(dial) > min(img):
        flag("subs/order", "picture subtitle before the text dialogue",
             "text dialogue listed first")

    # ---- AUDIO WITH NO LANGUAGE TAG AT ALL ----------------------------------
    #
    # Observed on The Oblivious Saint S01E03: one audio track, no language tag
    # on it, full English dialogue subtitles, and a show TheTVDB records as
    # Japanese. Sonarr's mediainfo reported that audio as English, because
    # unlabelled audio defaults to English - and from there the file looks like
    # an English dub to everything downstream. The file was not lying; it was
    # silent, and something else invented an answer.
    #
    # THE RULE THIS REPLACED WAS WRONG, and the library said so. The first
    # version also flagged audio TAGGED with a language that disagreed with the
    # title's original, on the argument that nobody subtitles dialogue in the
    # language it is already spoken in. That premise is false for English dubs
    # of foreign films, and the sweep proved it: 573 hits, and the ones checked
    # by hand were genuine dubs whose own audio titles said so -
    #
    #     REC (2007)            audio title "Dub / DTS-HD Master Audio"
    #     Drunken Master (1978) audio title "Offical English Dub 2.0 Audio"
    #     Rumble in the Bronx   subtitle    "English (SDH DUB)"
    #
    # English subtitles ship with English dubs routinely. So that branch is
    # gone, and what remains claims only what is certain: THE TAG IS MISSING.
    # A missing tag is a fact about the file rather than an inference about the
    # speech, and it is worth fixing whatever the language turns out to be.
    #
    # The guards that remain:
    #   * ONE audio track only. On a dual-audio release the untagged track is
    #     ambiguous - it could be either half - so there is nothing to suggest.
    #   * Full DIALOGUE subtitles must exist. Signs-only and SDH are both
    #     normal over matching audio and are excluded; ordinary dialogue subs
    #     are why the original language is the likely answer.
    #   * The title's original language must be known and not English, or
    #     there is nothing useful to suggest.
    if len(aud) == 1 and library:
        try:
            from . import contentkind
            orig = ""
            with cursor() as cur:
                r = cur.execute(
                    "SELECT k.lang FROM parent_kind k JOIN files f "
                    "  ON f.arr_name=k.arr_name AND f.arr_parent_id=k.parent_id "
                    " WHERE f.path=? LIMIT 1", (path,)).fetchone() if path else None
                orig = (r["lang"] or "") if r else ""
        except Exception:
            orig = ""
        claimed = rules._lang_guess(aud[0]) or ""
        orig3 = _ISO3.get((orig or "").strip().lower(), "")

        def _dialogue_subs(want: set[str] | None) -> list[str]:
            """Full-dialogue text subtitle languages, signs and SDH excluded."""
            out = []
            for i, s in enumerate(sub):
                if i not in txt:
                    continue
                if _disp(s, "forced") or rules.SIGNS_TITLE_RE.search(_t(s)):
                    continue
                if re.search(r"\bSDH\b|hearing", _t(s), re.I):
                    continue
                lg = _lang(s) or "und"
                if want is None or lg in want:
                    out.append(lg)
            return out

        if orig3 and not claimed and orig3 != "eng":
                # WHAT IS CLAIMED HERE IS ONLY THAT THE TAG IS MISSING, which
                # is certain, rather than what the language is, which is not.
                # An English dub of a Japanese show with an untagged track
                # looks identical from the metadata, and telling somebody to
                # tag that one "jpn" would be worse than the missing tag.
                # The original language is offered as the likely answer, and
                # the presence of full dialogue subtitles is the reason to
                # believe it: nobody subtitles dialogue they can already
                # understand.
                #
                # Subtitle LANGUAGE is not required, because on these releases
                # the subtitle is routinely untagged too - it was on this very
                # file. Its existence is the signal, not its label.
                dial = _dialogue_subs(None)
                if dial:
                    flag("audio/untagged",
                         f"single audio track with no language tag, on a "
                         f"{orig} title carrying full dialogue subtitles",
                         f"any language tag — probably {orig3}. Untagged "
                         f"audio is guessed as English by the arrs and by "
                         f"players, which is where a wrong label comes from")
    # ---- AUDIO IN A LANGUAGE THIS LIBRARY DOES NOT KEEP ---------------------
    #
    # THE CHECK THAT WAS MISSING. Every other rule here asks whether the file
    # was BUILT correctly - codec, channels, bitrate, track order. None asked
    # the prior question: is this the right file at all? A Portuguese-audio
    # release of an English-language show is perfectly well formed and entirely
    # wrong, and nothing in the audit noticed, because the language policy is
    # applied by the planner when it rewrites a file and never checked against
    # what is actually on disk.
    #
    # Found live on this library: The Big Bang Theory S02E20, playing to a
    # viewer, its only audio track Portuguese, filename ending [PT]-ZNM.mkv.
    #
    # TWO DIFFERENT SEVERITIES, and they must not be conflated:
    #
    #   audio/language      the file carries NO audio in any language the
    #                       library keeps. Nothing can be re-encoded to fix
    #                       this - the words are the wrong words - so it is
    #                       reported as replaceable, and it is the only rule
    #                       here for which fetching a different release is the
    #                       correct remedy rather than a destructive mistake.
    #   audio/extra-language
    #                       kept-language audio exists AND there are extra
    #                       tracks the policy would drop. That is ordinary
    #                       cleanup: the planner removes them on the next pass.
    try:
        from . import langpolicy
        pol = langpolicy.for_library(library, "audio") if library else {}
    except Exception:                                        # noqa: BLE001
        pol = {}
    keep = {_lang_key(c) for c in (pol.get("langs") or [])}
    if keep and aud:
        # "Keep the original language" is per title, so the policy set alone is
        # not the whole answer - a Japanese show whose library keeps English
        # plus the original is not violating anything by being Japanese.
        if pol.get("keep_original"):
            try:
                r = None
                if path:
                    with cursor() as cur:
                        r = cur.execute(
                            "SELECT k.lang FROM parent_kind k JOIN files f "
                            "  ON f.arr_name=k.arr_name "
                            " AND f.arr_parent_id=k.parent_id "
                            " WHERE f.path=? LIMIT 1", (path,)).fetchone()
                o = (r["lang"] or "") if r else ""
                if o:
                    keep.add(_lang_key(_ISO3.get(o.strip().lower(),
                                                 o.strip().lower())))
            except Exception:                                # noqa: BLE001
                pass
        have = [_lang_key(rules._lang_guess(a) or "und") for a in aud]
        # UNTAGGED IS NOT A VIOLATION HERE. An untagged track has its own rule
        # above; treating it as a foreign language would flag every lazily
        # tagged dual-audio release in the library as the wrong release.
        named = [g for g in have if g and g != "und"]
        kept_here = [g for g in have if g in keep or g == "und"]
        if named and not kept_here:
            flag("audio/language",
                 f"only {', '.join(sorted(set(named)))} audio",
                 f"{library} keeps {', '.join(sorted(keep))} — no track here "
                 f"is in a language this library wants, which no amount of "
                 f"re-encoding can change")
        elif len(aud) > len(kept_here) and kept_here:
            drop = [g for g in named if g not in keep]
            if drop:
                flag("audio/extra-language",
                     f"{len(drop)} extra audio track(s): "
                     f"{', '.join(sorted(set(drop)))}",
                     f"only {', '.join(sorted(keep))} kept")

    for i in img:
        if _disp(sub[i], "default") or _disp(sub[i], "forced"):
            flag("subs/flags", f"picture sub {i} set to auto-show",
                 "off — Plex would burn it on every play")
    eng_audio = any(_lang(a)[:2] == "en" for a in aud)
    if C.get("autoShowForcedOnly") and eng_audio:
        for i, s in enumerate(sub):
            if not _disp(s, "default"):
                continue
            if _disp(s, "forced") or rules.SIGNS_TITLE_RE.search(_t(s)):
                continue          # correct: this is the one that should show
            flag("subs/default",
                 f"{_t(s)[:22] or 'sub ' + str(i)!r} auto-shows over English "
                 f"audio", "only forced signs auto-show")
    return bad


# THE ONLY RULE FETCHING A DIFFERENT RELEASE CAN FIX.
#
# Every other finding here is a build fault - wrong codec, too many channels,
# tracks in the wrong order - and the planner rewrites the file. Replacing it
# would destroy a good copy to fetch another one with the same fault, since
# the fault is usually a property of how nuarr encoded it rather than of the
# release. "No audio in a language this library keeps" is different in kind:
# the words are the wrong words, no re-encode can change that, and the only
# real remedy is a different release.
#
# Kept as a tuple rather than a flag on the finding so that adding a rule here
# is a deliberate, visible act - this is the list that decides what auto mode
# is allowed to delete.
REPLACEABLE_RULES = ("audio/language",)

# Smaller than MAX_PER_RUN on purpose: a requeue costs GPU time, this costs
# a file and an indexer grab.
MAX_REPLACE_PER_RUN = 3


def replaceable(rule: str) -> bool:
    return any(r in (rule or "") for r in REPLACEABLE_RULES)


def mode() -> str:
    r""""manual" or "auto".

    MANUAL IS THE DEFAULT AND HAS TO BE. Auto mode blocklists the release and
    asks the arr to fetch another one, which deletes the file on the way past -
    the only irreversible thing the audit can do, on the strength of a rule
    reading a track's language tag. That is a switch a person turns on
    knowingly, having looked at what it would have replaced.
    """
    m = str(getattr(SETTINGS, "audit_mode", "manual") or "manual").lower()
    return m if m in ("manual", "auto") else "manual"


async def replace_one(file_id: int, why: str = "") -> dict:
    """Blocklist this file's release and ask the arr for a different one.

    Goes through refetch, which already knows how to find the grab, blocklist
    it, fall back to delete-and-search when no grab survives, and treat a 404
    as stale bookkeeping rather than a failure. Nothing about that flow is
    re-implemented here; this only decides that it is the right thing to run,
    which is the part the audit knows and refetch does not.
    """
    from . import refetch
    try:
        out = await refetch.run(
            int(file_id),
            reason=why or "the audio is in no language this library keeps, "
                          "which no re-encode can change")
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    if out.get("ok"):
        reason = why or "audio in no language this library keeps"
        joblog.log(f"rule audit: replaced a release - {reason}", "warn")
        await asyncio.to_thread(_note_replaced, int(file_id), out)
    return out


def _note_replaced(file_id: int, out: dict) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO audit_heals(file_id,rule,attempts,first_at,last_at,"
            "state,detail,path) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET state=excluded.state, "
            "detail=excluded.detail, last_at=excluded.last_at",
            (int(file_id), "audio/language", 1, time.time(), time.time(),
             "replaced", "; ".join(out.get("did") or []) or "release replaced",
             out.get("path") or ""))


async def auto_replace(viol_by_file: dict) -> int:
    """In auto mode, replace the releases nothing else can fix.

    BOUNDED THE SAME WAY THE HEALER IS. A language policy edited by mistake
    could make every file in a library a violation, and an unattended sweep
    that acts on that would empty a shelf overnight. MAX_REPLACE_PER_RUN is
    deliberately smaller than MAX_PER_RUN: a requeue costs GPU time, this costs
    a file and an indexer grab.
    """
    if mode() != "auto":
        return 0
    done = 0
    for fid, v in list(viol_by_file.items()):
        if done >= MAX_REPLACE_PER_RUN:
            break
        if not any(replaceable(r) for r in (v.get("rules") or [])):
            continue
        out = await replace_one(int(fid), "audio in no language this library keeps")
        if out.get("ok"):
            done += 1
    if done:
        joblog.log(f"rule audit (auto mode): {done} release(s) blocklisted and "
                   f"re-searched — the audio was in no language their library "
                   f"keeps", "warn")
    return done


def _chname(ch: int) -> str:
    """8ch means nothing to most readers; 7.1 does."""
    return {8: "7.1", 7: "6.1", 6: "5.1", 2: "stereo", 1: "mono"}.get(
        int(ch), f"{ch}ch")


# EVERY DATABASE CALL IN THE HEALER RUNS IN A THREAD.
#
# db.py states the invariant outright: no SQLite on the asyncio event loop
# thread, because under load a "fast" query waits behind 200 MB/s of commit
# traffic and uvicorn cannot service ANY request while one is outstanding - it
# measured a database-free endpoint at 20 s for exactly this reason. The first
# version of heal() was an async def that opened cursor() directly, once per
# flagged file, up to twenty times a run. That is the documented way to freeze
# the whole UI, and a freeze in the server survives a browser refresh, which is
# precisely what it looked like from the panel.
#
# So the reads happen in one threaded call up front, the writes in one threaded
# call at the end, and the loop only ever awaits.
def _heal_rows() -> dict[int, dict]:
    with cursor() as cur:
        return {r["file_id"]: dict(r) for r in
                cur.execute("SELECT * FROM audit_heals")}


def _write_heals(recs: list[tuple]) -> None:
    if not recs:
        return
    with cursor() as cur:
        cur.executemany(
            "INSERT INTO audit_heals(file_id,rule,attempts,first_at,"
            "last_at,state,detail,path) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET rule=excluded.rule, "
            "attempts=excluded.attempts, last_at=excluded.last_at, "
            "state=excluded.state, detail=excluded.detail, "
            "path=excluded.path", recs)


def _still_broken(path: str, library: str = "") -> list[str] | None:
    """Re-read the file and return the rules it STILL breaks, or None if clean.

    THE PLANNER HAVING NO WORK IS NOT PROOF THAT NO RULE APPLIES.
    #
    It has two completely different causes and the first version treated them
    as one: either nothing in the rules addresses the finding, or the file has
    already been repaired since the finding was recorded. Both surface as
    NothingToDo, so a file that autoqueue had fixed minutes earlier was labelled
    "no rule fixes this" - observed on The Jungle Book, which the activity feed
    showed being downmixed to 5.1 at 08:45 while the panel called it unfixable.

    A finding is a claim about bytes on disk, so the way to tell the two apart
    is to go and read the bytes again. Clean now means healed; still broken with
    no plan means the rules genuinely have a gap.
    """
    if not path or not os.path.exists(path):
        return None                      # gone is not broken
    pr = probe(path)
    if not pr:
        return []                        # unreadable: no verdict either way
    try:
        return list(dict.fromkeys(
            rule for rule, _found, _want
            in check(pr, rules.is_anime(path), library, path)))
    except Exception:
        return []


def _mark_fixed(clean_ids: list[int]) -> int:
    """Files that were flagged before and came back clean this run.

    This is the only evidence that healing WORKED. Without it the panel can say
    "queued" forever and never say "and it worked", which is the difference
    between a self-healer and a button that fires into the dark. Only files
    this run actually read from disk count - absence from a random sample is
    not evidence of anything.
    """
    if not clean_ids:
        return 0
    qs = ",".join("?" * len(clean_ids))
    with cursor() as cur:
        cur.execute(f"UPDATE audit_heals SET state='fixed', last_at=?, "
                    f"detail='re-checked after the requeue and it now matches "
                    f"every rule' WHERE state NOT IN ('fixed','unfixable') "
                    f"AND file_id IN ({qs})",
                    (time.time(), *clean_ids))
        return cur.rowcount or 0


async def heal(viol: list[dict]) -> dict:
    """Offer every violating file to the planner; queue the ones it can fix.

    `viol` is [{file_id, path, rules:[...]}] from the run that just finished.
    Returns counts for the panel. Never raises into the audit loop - a healer
    that can break the check it rides on is worse than no healer.
    """
    from . import jobs
    STATS.update(heal_running=True, heal_total=len(viol), heal_done=0,
                 heal_current="")
    prior = await asyncio.to_thread(_heal_rows)
    # A FINDING'S PATH IS A SNAPSHOT; THE FILE'S PATH IS THE TRUTH.
    #
    # Fixing a file routinely renames it - Sonarr and Radarr rebuild the name
    # from the new mediainfo, so downmixing Snow White moved it from
    # [EAC3 7.1] to [EAC3 5.1] the moment the job committed. The finding still
    # named the old path, _still_broken found nothing on disk there and returned
    # "gone, no verdict", and the file fell through to being queued a second
    # time to fix something already fixed. Resolve the current path first, in
    # one query, and every check below is about the file rather than about a
    # name it used to have.
    def _paths(ids: list[int]) -> dict[int, tuple[str, str]]:
        if not ids:
            return {}
        qs = ",".join("?" * len(ids))
        with cursor() as cur:
            # The LIBRARY comes along for the ride, because the re-check below
            # has to apply the same per-library codec settings the planner did.
            # Re-checking a file against another library's ceiling is how a
            # finding becomes permanently "unfixable".
            return {r["id"]: (r["path"], r["library"] or "")
                    for r in cur.execute(
                        f"SELECT id, path, library FROM files "
                        f"WHERE id IN ({qs})", ids)}
    live = await asyncio.to_thread(_paths, [v["file_id"] for v in viol])
    for v in viol:
        got = live.get(v["file_id"])
        if got:
            v["path"] = got[0] or v["path"]
            v["library"] = got[1]
    queued = unfixable = gave_up = skipped = fixed = 0
    recs: list[tuple] = []
    now = time.time()
    for _hn, v in enumerate(viol):
        fid = v["file_id"]
        STATS["heal_done"] = _hn
        STATS["heal_current"] = os.path.basename(v["path"] or "")[:60]
        rule = ", ".join(sorted(set(v["rules"])))
        p = prior.get(fid) or {}
        state = (p.get("state") or "")
        attempts = int(p.get("attempts") or 0)
        if state in _TERMINAL:
            skipped += 1
            continue
        if attempts >= MAX_HEAL_ATTEMPTS:
            # Requeued the limit and still flagged. The planner said it had
            # work, ran it, and the invariant did not change - so either the
            # plan does not do what the audit measures, or something rewrites
            # the file afterwards. Both need a person, not another attempt.
            recs.append((fid, rule, attempts, p.get("first_at") or now, now,
                         "gave-up",
                         f"requeued {attempts}x and still breaking {rule} - the "
                         f"plan is not fixing what the audit measures",
                         v["path"]))
            joblog.log(f"rule audit: gave up healing {os.path.basename(v['path'])} "
                       f"- {rule} survived {attempts} requeues", "error")
            gave_up += 1
            continue
        if queued >= MAX_PER_RUN:
            skipped += 1
            continue

        # READ THE FILE BEFORE DECIDING ANYTHING ABOUT IT.
        #
        # A finding can be minutes or weeks old, and in between autoqueue, a
        # manual requeue or an arr upgrade may have dealt with it. Asking the
        # planner first gets that wrong in both directions, because the planner
        # works from the STORED probe:
        #
        #   * stale probe still showing the fault -> it plans a job, the job
        #     re-probes, finds nothing to do and skips. Both Snow White and The
        #     Jungle Book were already eac3/6ch on disk and were queued anyway.
        #   * no plan, for any reason -> recorded as "no rule fixes this", which
        #     is how The Jungle Book was called unfixable eleven minutes after
        #     the activity feed showed it being downmixed.
        #
        # One ffprobe, capped at MAX_PER_RUN per run and run in a thread, settles
        # it. This is the same instinct the audit itself is built on: the check
        # reads real streams off the pool rather than trusting the database, so
        # the thing acting on the check must too.
        broken = await asyncio.to_thread(_still_broken, v["path"],
                                         v.get("library") or "")
        if broken is not None and not broken:
            recs.append((fid, rule, attempts, p.get("first_at") or now, now,
                         "fixed",
                         "re-read from disk and it now matches every rule",
                         v["path"]))
            fixed += 1
            continue

        label = os.path.splitext(os.path.basename(v["path"] or ""))[0]
        try:
            await jobs.enqueue(fid, v["path"], label, source="rule audit",
                               priority=60)
            new_state, detail = "queued", f"queued to fix {rule}"
            attempts += 1
            queued += 1
        except jobs.NothingToDo:
            # THE ONE GAP THE PLANNER CANNOT CLOSE BY DESIGN. container/name
            # means Matroska content wearing another extension - nuarr's own
            # commit writes over the original path and so keeps its name. The
            # streams are already right, so decide() has nothing to do, and the
            # finding sat as "no rule fixes this" forever. The remedy is a
            # rename, which is not stream work and never will be.
            done, why = await _fix_container_name(v, rule, broken)
            if done:
                new_state, detail = "fixed", why
                fixed += 1
            elif why:
                # A collision, and worth naming: telling someone "no rule fixes
                # this" when the actual situation is two copies of one episode
                # sends them looking at the rules instead of at their disk.
                new_state, detail = "unfixable", why
                unfixable += 1
            else:
                new_state = "unfixable"
                detail = (f"still breaking {', '.join(broken or [rule])} when "
                          f"re-read from disk, and the planner has no work for "
                          f"it - the check is right and the rules have a gap")
                unfixable += 1
        except ValueError:
            # already queued or running - the fix is already on its way
            new_state, detail = "queued", "a job for this file is already queued"
            skipped += 1
        except Exception as e:
            new_state = "error"
            detail = f"{type(e).__name__}: {e}"

        recs.append((fid, rule, attempts, p.get("first_at") or now, now,
                     new_state, detail, v["path"]))

    STATS.update(heal_running=False, heal_done=len(viol), heal_current="")
    await asyncio.to_thread(_write_heals, recs)
    if queued:
        try:
            await jobs.start()
        except Exception:
            pass
    if queued or unfixable or gave_up or fixed:
        joblog.log(f"rule audit healing: {queued} requeued, {fixed} already "
                   f"fixed, {unfixable} that no rule can fix, {gave_up} given "
                   f"up on", "warn" if (unfixable or gave_up) else "ok")
    STATS.update(healed=queued, unfixable=unfixable)
    return {"queued": queued, "unfixable": unfixable, "gave_up": gave_up,
            "skipped": skipped, "fixed": fixed}


# ------------------------------------------------------- close the loop ----
# THE PANEL MUST NOT LAG THE QUEUE. A heal row sits at 'queued' from the
# moment the healer enqueues the job, but nothing flipped it to 'fixed' until
# the next nightly run happened to re-sample the file - so the timeline kept a
# run red and the scoreboard said "4 queued to fix" for hours after the
# Transcoding panel had watched all four jobs finish. Two panels, one fact,
# disagreeing for up to a day.
#
# This watcher closes the loop: whenever a 'queued' heal row's file has a
# finished job newer than the row itself (and nothing further queued or
# running), the file is re-read from disk and the verdict recorded - the same
# read-the-bytes test every other verdict here rests on. The query returns
# nothing at all in the common case, so the 30 s cadence costs a single cheap
# SELECT; the ffprobes only happen in the moments after a healed job lands.
REVERIFY_S = 30


async def _fix_container_name(v: dict, rule: str, broken) -> tuple:
    r"""Rename Matroska content to .mkv. -> (fixed, why).

    ONLY WHEN THE NAME IS FREE. The obvious case is a lone file that nuarr
    remuxed in place: the content became Matroska, the path did not change, and
    so the extension stayed whatever it was. Renaming it is the whole fix, and
    the arr is told so its record follows.

    The interesting case is when `foo.mkv` already exists beside `foo.mp4`.
    That is not a naming problem at all - it is two copies of one episode, and
    which to keep is a judgement about 6 GB of disk that nothing here should
    make on its own. So it is reported AS that, rather than as "no rule fixes
    this", which is true and sends the reader to look at the rules when the
    answer is on their disk.

    Returns ("", "") when this is not a container/name finding at all, so the
    caller falls through to the general message.
    """
    if rule != "container/name" and "container/name" not in (broken or ()):
        return False, ""
    src = v.get("path") or ""
    if not src or not os.path.exists(src):
        return False, ""
    stem, ext = os.path.splitext(src)
    if ext.lower() == ".mkv":
        return False, ""
    dst = stem + ".mkv"
    if os.path.exists(dst):
        try:
            a, b = os.path.getsize(src), os.path.getsize(dst)
        except OSError:
            a = b = 0
        near = a and b and abs(a - b) < max(a, b) * 0.01
        return False, (
            f"a file with the correct name already sits beside this one"
            f"{' and is within 1% of the same size' if near else ''} - "
            f"{os.path.basename(dst)}. This is two copies of one episode "
            f"rather than a naming mistake, so nothing renames it: keeping "
            f"one and removing the other is a decision about "
            f"{max(a, b) / 1e9:.1f} GB")
    try:
        from . import fileops
        r = fileops.safe_rename(src, dst)
        if not getattr(r, "ok", False):
            # OpResult carries `detail`, not `error` - reading the wrong field
            # would have reported every failure as the literal word "None".
            return False, (f"could not rename to .mkv: "
                           f"{getattr(r, 'detail', '') or 'rename failed'}"[:180])
    except Exception as e:                                   # noqa: BLE001
        return False, f"could not rename to .mkv: {type(e).__name__}: {e}"[:180]
    fid = v.get("file_id")
    try:
        with cursor() as cur:
            cur.execute("UPDATE files SET path=?, updated_at=? WHERE id=?",
                        (dst, time.time(), fid))
    except Exception:                                        # noqa: BLE001
        pass
    # The arr still believes the old name. Its own rename pass is what puts
    # that right, and it is already the thing that owns naming.
    try:
        from . import renamequeue
        with cursor() as cur:
            row = cur.execute("SELECT arr_name, arr_parent_id FROM files "
                              "WHERE id=?", (fid,)).fetchone()
        if row and row["arr_name"]:
            renamequeue.enqueue(fid, row["arr_name"], row["arr_parent_id"],
                                dst, why="renamed to .mkv to match its content")
    except Exception:                                        # noqa: BLE001
        pass
    joblog.log(f"renamed to match its content: {os.path.basename(dst)} "
               f"(was {ext})", "ok")
    return True, "renamed to .mkv - the content was already Matroska"


def retire_gone() -> int:
    r"""Retire findings whose file no longer exists. Returns how many.

    A finding is a statement about a FILE, and the arrs replace files. A DVD
    rip of Swat Kats S01E01 was flagged for its container, then Sonarr
    upgraded the episode: the old file was deleted, a 1080p release took its
    place under a new id, and the finding was left pointing at a row that no
    longer exists.

    Nothing could ever clear it. _reverify_queued() joins audit_heals to
    files, so a finding whose file row is gone is not merely unfixable - it is
    invisible to the re-check, and sits at 'queued' forever. It kept the
    Attention tile at 1 for a file that had not existed for hours, which is
    how a number stops being believed.

    Marked 'gone' rather than deleted: the run that found it is a record and
    keeps it. It simply stops counting as outstanding work, because it is not.

    Deliberately decided from nuarr's OWN bookkeeping - the files row is
    absent, or says deleted - and not from a disk check. A pool disk that
    blinks out for a moment must not be able to retire real findings.
    """
    # RESOLVED IS NOT THE SAME AS VANISHED, and reporting one as the other
    # loses the only interesting part. A container/name finding says "Matroska
    # content named .mp4"; delete that file and keep the .mkv beside it and the
    # rule is satisfied - the library is now correct BECAUSE of what the check
    # said. Calling that "gone" reads as the finding having escaped rather than
    # been answered. So the ones whose complaint no longer applies are marked
    # fixed, and only the genuinely disappeared are marked gone.
    now = time.time()
    fixed_now = 0
    # 'gone' IS RECONSIDERED, not just the queued ones. Rows retired before
    # this distinction existed are sitting at 'gone' with the wrong story, and
    # a rule that only ever looks forward leaves them lying there. There are a
    # handful of these and the test is one os.path.exists, so it is re-asked
    # every sweep rather than fixed once by hand and forgotten.
    with cursor() as cur:
        stale = [dict(r) for r in cur.execute(
            "SELECT file_id, rule, path FROM audit_heals "
            " WHERE state != 'fixed' "
            "   AND file_id NOT IN (SELECT id FROM files "
            "                        WHERE state != 'deleted')")]
    for s in stale:
        if s.get("rule") != "container/name":
            continue
        p = s.get("path") or ""
        stem, ext = os.path.splitext(p)
        # The complaint was the extension. If the file is gone and a correctly
        # named one now sits where it was, the complaint was met.
        if ext.lower() != ".mkv" and stem and os.path.exists(stem + ".mkv"):
            with cursor() as cur:
                cur.execute(
                    "UPDATE audit_heals SET state='fixed', last_at=?, "
                    "       detail='the misnamed copy is gone and the .mkv "
                    "beside it remains - the rule is satisfied' "
                    " WHERE file_id=? AND rule=?",
                    (now, s["file_id"], s["rule"]))
            fixed_now += 1
    with cursor() as cur:
        cur.execute(
            "UPDATE audit_heals SET state='gone', "
            "       detail='the file was replaced or removed - this finding "
            "was about a file that no longer exists', "
            "       last_at=? "
            " WHERE state NOT IN ('fixed','gone') "
            "   AND file_id NOT IN (SELECT id FROM files "
            "                        WHERE state != 'deleted')", (now,))
        n = cur.rowcount or 0
    if fixed_now:
        joblog.log(f"{fixed_now} finding(s) turned out to have been answered "
                   f"rather than lost - the file the check complained about is "
                   f"gone and a correctly named one is in its place", "ok")
    return n + fixed_now


async def _reverify_queued() -> int:
    def _due() -> list[dict]:
        with cursor() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT h.file_id, h.rule, h.last_at, f.path, f.library "
                "FROM audit_heals h JOIN files f ON f.id=h.file_id "
                "WHERE h.state='queued' "
                # COALESCE, because a done job CAN carry a NULL finished_at
                # (observed on Captain America Brave New World: state='done',
                # finished_at NULL) and NULL > x is NULL in SQL - the row
                # silently fails the test and the heal sits at 'queued'
                # forever. created_at is a fair stand-in: it is later than the
                # heal's own stamp for any job the healer queued. >= rather
                # than >, since enqueue and the heal stamp land in the same
                # second; the re-probe bumps last_at past it either way.
                "AND EXISTS (SELECT 1 FROM jobs j WHERE j.file_id=h.file_id "
                "  AND j.state IN ('done','skipped','failed') "
                "  AND COALESCE(j.finished_at, j.created_at) >= h.last_at) "
                "AND NOT EXISTS (SELECT 1 FROM jobs j2 WHERE j2.file_id=h.file_id "
                "  AND j2.state IN ('queued','running'))")]

    def _upd(fid: int, state: str, detail: str) -> None:
        with cursor() as cur:
            cur.execute("UPDATE audit_heals SET state=?, detail=?, last_at=? "
                        "WHERE file_id=?", (state, detail, time.time(), fid))

    rows = await asyncio.to_thread(_due)
    fixed = 0
    for r in rows[:6]:                   # bounded; the next sweep gets the rest
        broken = await asyncio.to_thread(_still_broken, r["path"],
                                         r["library"] or "")
        if broken is not None and not broken:
            await asyncio.to_thread(
                _upd, r["file_id"], "fixed",
                "its queued job finished and the file now matches every rule")
            joblog.log(f"rule audit: {os.path.basename(r['path'])} re-checked "
                       f"after its job - clean now", "ok")
            fixed += 1
        else:
            # The job ran and the fault is still there, or the file is gone.
            # Keep the row 'queued' but stamp last_at so this exact job is not
            # re-probed every sweep; the nightly heal owns the attempt count
            # and the gave-up verdict.
            await asyncio.to_thread(
                _upd, r["file_id"], "queued",
                f"a job finished but {', '.join(broken or [r['rule']])} still "
                f"fails - the nightly check will retry or give up")
    return fixed


async def reverify_watch() -> None:
    await asyncio.sleep(120)             # boot scan first; this is bookkeeping
    while True:
        try:
            # Retire first: a finding about a file that has been replaced is
            # not work, and re-verifying it is impossible anyway - the join
            # that drives the re-check cannot see it.
            n = await asyncio.to_thread(retire_gone)
            if n:
                joblog.log(f"rule check: {n} finding(s) retired - their files "
                           f"were replaced or removed", "info")
            await _reverify_queued()
        except Exception as e:
            joblog.log(f"audit reverify: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(REVERIFY_S)


async def heal_file(file_id: int) -> dict:
    """One file, on demand - what the panel's button does.

    Routed through heal() rather than straight to jobs.enqueue so that a press
    of the button REACHES THE SAME VERDICT AND WRITES IT DOWN. The button used
    to call the generic queue endpoint, which returned "nothing to do" as a
    line of text and recorded nothing: the row stayed red, kept its button, and
    said the same thing again the next time it was pressed. That answer is the
    unfixable verdict - it deserves to be stored like one.

    A manual press deliberately ignores the attempt cap and the terminal
    states. Those exist to stop the nightly loop thrashing; a person pressing
    the button has decided otherwise, and is usually doing it because they just
    changed a rule.
    """
    def _look() -> tuple[str, str]:
        with cursor() as cur:
            row = cur.execute("SELECT path, rule FROM audit_findings "
                              "WHERE file_id=? ORDER BY at DESC LIMIT 1",
                              (file_id,)).fetchone()
            rule = row["rule"] if row else "manual"
            # files.path first - the finding's copy goes stale the moment an
            # arr renames the file, which fixing it usually causes.
            f = cur.execute("SELECT path FROM files WHERE id=?",
                            (file_id,)).fetchone()
            path = (f["path"] if f else "") or (row["path"] if row else "")
        return path, rule

    path, rule = await asyncio.to_thread(_look)
    if not path:
        return {"ok": False, "why": "no such file"}
    await asyncio.to_thread(clear_heal, file_id)
    try:
        out = await heal([{"file_id": file_id, "path": path, "rules": [rule]}])
    finally:
        # A raise must not leave heal_running stuck True - the panel polls
        # fast while it is set and would spin forever on a dead heal.
        STATS.update(heal_running=False, heal_current="")
    return {"ok": True, **out}


async def run_once(per_bucket: int = PER_BUCKET) -> dict:
    STATS.update(running=True, last_error="", live_checked=0)
    started = time.time()
    checked = viol_files = pend_files = 0
    findings: list[tuple] = []
    by_rule: dict[str, int] = {}
    # Per-file, for the healer: what this run found, and who came back clean.
    viol_by_file: dict[int, dict] = {}
    clean_ids: list[int] = []
    try:
        for name, pred in BUCKETS.items():
            # Off the loop, like every other query here. ORDER BY RANDOM() is a
            # full scan of the join by definition - it cannot use an index - so
            # this is one of the slowest reads in the app, run ten times a pass.
            def _sample(pred=pred) -> list[dict]:
                with cursor() as cur:
                    return [dict(r) for r in cur.execute(
                        "SELECT f.id, f.path, f.size, f.library FROM files f "
                        "JOIN file_probes p ON p.file_id = f.id "
                        f"WHERE f.state='done' AND ({pred}) "
                        "ORDER BY RANDOM() LIMIT ?", (per_bucket,))]
            rows = await asyncio.to_thread(_sample)
            # SIXTY FFPROBES, ONE AT A TIME, OFF SPINNING DISKS was the whole
            # of the wait. Each is ~0.6-1.5s depending on which spindle wakes
            # up, and nothing in the loop depended on the previous answer - the
            # run was serial because it was written as a for-loop, not because
            # it had to be. Eight at a time, which is where the pool stops
            # rewarding more: the probes are seek-bound and land on twelve
            # different disks, so they overlap almost perfectly.
            live = [r for r in rows if r.get("path") and os.path.exists(r["path"])]
            sem = asyncio.Semaphore(PROBE_LANES)

            async def _one(r):
                async with sem:
                    return r, await asyncio.to_thread(probe, r["path"])
            for r, pr in await asyncio.gather(*[_one(r) for r in live]):
                path = r["path"]
                if not pr:
                    continue
                checked += 1
                anime = rules.is_anime(path)
                bad = check(pr, anime, r.get("library") or "", path)
                STATS["live_checked"] = checked
                if bad:
                    viol_files += 1
                    for rule, found, want in bad:
                        by_rule[rule] = by_rule.get(rule, 0) + 1
                        findings.append((started, r["id"], name, path, rule,
                                         f"{found} — should be {want}",
                                         found, want))
                    viol_by_file[r["id"]] = {
                        "file_id": r["id"], "path": path,
                        "rules": [rule for rule, _f, _w in bad]}
                else:
                    clean_ids.append(r["id"])
                try:
                    pl = rules.decide(pr, anime=anime, filename=path,
                                      size_bytes=r["size"] or 0)
                    if pl.needed:
                        pend_files += 1
                except Exception:
                    pass
        def _persist() -> None:
            with cursor() as cur:
                cur.execute("INSERT INTO audit_runs(at,checked,violations,"
                            "pending,by_rule) VALUES(?,?,?,?,?)",
                            (started, checked, viol_files, pend_files,
                             json.dumps(by_rule)))
                run_id = cur.lastrowid
                if findings:
                    cur.executemany(
                        "INSERT INTO audit_findings(run_id,at,file_id,bucket,"
                        "path,rule,detail,found,want) VALUES(?,?,?,?,?,?,?,?,?)",
                        [(run_id,) + f for f in findings])
                # Evidence, not state: two months is long enough to see a trend.
                cur.execute("DELETE FROM audit_findings WHERE at < ?",
                            (time.time() - 60 * 86400,))
                cur.execute("DELETE FROM audit_runs WHERE at < ?",
                            (time.time() - 60 * 86400,))
        await asyncio.to_thread(_persist)
        # "0 checked" is not a pass, it is a broken audit. Say so loudly.
        if not checked:
            joblog.log("rule audit read no files — the sample was empty or "
                       "ffprobe could not run; this is NOT a clean result",
                       "error")
        lvl = "error" if not checked else ("warn" if viol_files else "ok")
        joblog.log(f"rule audit: {checked} files checked, {viol_files} with "
                   f"violations, {pend_files} awaiting reprocessing", lvl)
        STATS.update(last_run=started, checked=checked, violations=viol_files,
                     pending=pend_files)
        # HEALING RUNS AFTER THE RESULT IS PERSISTED, NEVER BEFORE.
        #
        # The run row and its findings are the record of what was true at this
        # moment. If the healer were allowed to run first it would queue jobs
        # that could rewrite files before their finding was written down, and a
        # later reader could not tell whether a clean file had been healed or
        # had never been broken. Evidence first, then act on it.
        fixed = await asyncio.to_thread(_mark_fixed, clean_ids)
        healed = {"queued": 0, "unfixable": 0, "gave_up": 0, "skipped": 0}
        if HEAL_ENABLED and viol_by_file:
            try:
                healed = await heal(list(viol_by_file.values()))
            except Exception as e:
                STATS.update(heal_error=f"{type(e).__name__}: {e}",
                             heal_running=False, heal_current="")
                joblog.log(f"rule audit healing failed: {type(e).__name__}: {e}",
                           "error")
        # REPLACEMENT RUNS LAST, and only in auto mode. It is the one action
        # here that cannot be undone, so it happens after the evidence is
        # written and after the healer has had its go - a file the planner can
        # fix must never be replaced instead.
        replaced = 0
        try:
            replaced = await auto_replace(viol_by_file)
        except Exception as e:                               # noqa: BLE001
            joblog.log(f"rule audit auto-replace failed: {type(e).__name__}: "
                       f"{e}", "error")
        return {"ok": True, "checked": checked, "violations": viol_files,
                "pending": pend_files, "by_rule": by_rule,
                "fixed_since_last": fixed, "replaced": replaced, **healed}
    except Exception as e:
        STATS["last_error"] = f"{type(e).__name__}: {e}"
        joblog.log(f"rule audit failed: {type(e).__name__}: {e}", "error")
        return {"ok": False, "why": str(e)}
    finally:
        from . import workers
        STATS.update(running=False,
                     next_run=time.time() + workers.tune("audit_every_h") * 3600)


# What the panel tells a reader this check actually covers. Kept next to the
# checks themselves so the two cannot drift - a description of an audit that
# has quietly stopped testing something is worse than no description.
COVERS = [
    ("Container", "the file is MKV, which Plex plays without repackaging"),
    ("Video", "a codec Plex plays; Dolby Vision layer removed; 10-bit still "
              "10-bit and not quietly flattened by a filter"),
    ("Audio", "no E-AC3 above the rate Plex refuses to decode, nothing above "
              "5.1, no commentary left behind, one track per language"),
    ("Subtitles", "English only; picture subtitles never set to auto-show; "
                  "text dialogue ahead of pictures; with English audio only a "
                  "forced signs track shows itself"),
]


def latest(run_id: int = 0) -> dict:
    """The named run (or the newest), plus every run's shape and heal state.

    run_id exists because the panel used to be able to show exactly one thing:
    the most recent run's findings. A check whose whole purpose is to make a
    regression visible has to let you look at the run before, or "it was fine
    last week" is a claim nobody can test. The history rows now carry their id
    and their by_rule breakdown so any of them can be opened.
    """
    # init() previously only ran from watch(), ten minutes after startup, or
    # when somebody pressed Run now. So the panel's own poll could reach a
    # table that did not exist yet and render its error instead of its content
    # - which is exactly what audit_heals did on the first restart after it was
    # added. Creating the tables is idempotent and costs nothing worth counting;
    # a panel that cannot read its own store is not a cost worth taking.
    global _READY
    if not _READY:
        init()
        _READY = True
    with cursor() as cur:
        run = None
        if run_id:
            run = cur.execute("SELECT * FROM audit_runs WHERE id=?",
                              (run_id,)).fetchone()
        if run is None:
            run = cur.execute("SELECT * FROM audit_runs ORDER BY at DESC "
                              "LIMIT 1").fetchone()
        latest_id = cur.execute("SELECT id FROM audit_runs ORDER BY at DESC "
                                "LIMIT 1").fetchone()
        hist = [dict(r) for r in cur.execute(
            "SELECT id, at, checked, violations, pending, by_rule "
            "FROM audit_runs ORDER BY at DESC LIMIT 30")]
        # A RUN THAT FOUND PROBLEMS SINCE FIXED IS NOT THE SAME AS A RUN WHOSE
        # PROBLEMS STILL STAND. The timeline painted both the same red forever,
        # so the panel could never show the healer winning - a fully-resolved
        # run and an ignored one were indistinguishable at a glance. One
        # grouped query per page load answers it: of the files each run
        # flagged, how many carry a 'fixed' heal verdict now.
        res = {r["run_id"]: (r["flagged"], r["fixedn"]) for r in cur.execute(
            "SELECT f.run_id, COUNT(DISTINCT f.file_id) flagged, "
            "COUNT(DISTINCT CASE WHEN h.state='fixed' THEN f.file_id END) fixedn "
            "FROM audit_findings f "
            "LEFT JOIN audit_heals h ON h.file_id=f.file_id "
            "GROUP BY f.run_id")}
        for h in hist:
            h["flagged_files"], h["fixed_files"] = res.get(h["id"], (0, 0))
        rows = []
        if run:
            rows = [dict(r) for r in cur.execute(
                "SELECT file_id, bucket, path, rule, detail, found, want "
                "FROM audit_findings WHERE run_id=? ORDER BY rule LIMIT 60",
                (run["id"],))]
        # Heal state, joined onto the findings by the panel. Selected whole
        # rather than per-finding because the interesting rows are the ones
        # NOT in this run: a file healed last week is the evidence the healer
        # works, and it is by definition absent from today's findings.
        heals = [dict(r) for r in cur.execute(
            "SELECT file_id, rule, attempts, first_at, last_at, state, detail, "
            "path FROM audit_heals ORDER BY last_at DESC LIMIT 120")]
        tally = {r["state"]: r["n"] for r in cur.execute(
            "SELECT state, COUNT(*) n FROM audit_heals GROUP BY state")}
    return {"run": dict(run) if run else None,
            "run_id": run["id"] if run else None,
            "latest_id": latest_id["id"] if latest_id else None,
            "history": hist, "findings": rows, "stats": STATS,
            "heals": heals, "heal_tally": tally,
            "heal": {"enabled": HEAL_ENABLED, "max_attempts": MAX_HEAL_ATTEMPTS,
                     "max_per_run": MAX_PER_RUN},
            "covers": [{"area": a, "what": w} for a, w in COVERS],
            "buckets": list(BUCKETS), "per_bucket": PER_BUCKET,
            "mode": mode(), "replaceable_rules": list(REPLACEABLE_RULES),
            "max_replace_per_run": MAX_REPLACE_PER_RUN,
            "every_hours": _every_hours()}


def clear_heal(file_id: int) -> bool:
    """Forget a terminal heal verdict so the file is eligible again.

    The counterpart to 'unfixable' and 'gave-up' being permanent. Those states
    mean "no rule fixes this, stop asking" - which is the correct answer right
    up until somebody changes a rule. This is how you tell the healer that
    happened, and it is deliberately manual: nothing else in the system knows
    that a rule edit was meant to address a particular finding.
    """
    with cursor() as cur:
        cur.execute("DELETE FROM audit_heals WHERE file_id=?", (file_id,))
        return bool(cur.rowcount)


async def watch() -> None:
    # Well after startup: the first scan and the arr fetch matter more, and a
    # nightly job has no reason to compete with them.
    # Announce the first run before sleeping, so the panel can say when it is
    # due rather than showing a blank where a time should be.
    STATS["next_run"] = time.time() + 600
    await asyncio.sleep(600)
    try:
        await asyncio.to_thread(init)
    except Exception as e:
        joblog.log(f"audit tables: {type(e).__name__}: {e}", "error")
        return
    while True:
        schedules.beat('audit')
        try:
            await run_once()
        except Exception as e:
            joblog.log(f"audit loop: {type(e).__name__}: {e}", "error")
        # Re-read each cycle AND nap in slices, so shortening a 24 h cadence
        # on the settings page does not have to wait out a sleep started
        # under the old value.
        from . import workers
        end = time.time() + workers.tune("audit_every_h") * 3600
        while time.time() < end:
            await asyncio.sleep(min(300, max(1, end - time.time())))
            end = min(end, time.time()
                      + workers.tune("audit_every_h") * 3600)
