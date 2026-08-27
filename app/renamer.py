"""
nuarr - rename with check, confirm, retry and verification

THE PROBLEM THIS SOLVES
-----------------------
Tdarr treats a rename as one shot. If the file is momentarily locked by Plex, or
DrivePool is relocating it, or the resulting path is one character over
MAX_PATH, it fails and moves on - leaving artifacts like:

    Turn A Gundam (1999) - S01E37 - ... -JosekiTurn A Gundam.mkv
                                        ^^^^^^^^^^^^^^^^^^^^ half-applied rename

which then needs a manual fix or a re-download.

nuarr splits a rename into four phases and refuses to skip any of them:

    CHECK    ask the arr what it wants to rename, resolve real paths, and
             pre-flight every one (length, collision, lock, existence)
    CONFIRM  produce a plan the user (or the UI) approves. Nothing touches disk
             until confirm=True. Blocked items are never silently attempted.
    APPLY    let the ARR do the rename - it owns the naming scheme and its own
             database - then WAIT for the command instead of firing and forgetting
    VERIFY   confirm the new path exists on disk AND the arr's record points at
             it. If not, retry with backoff; if the arr keeps failing, fall back
             to doing the move ourselves and tell the arr to rescan.

Nothing is ever deleted. The worst case is "blocked, needs attention", which is
reported - not a lost file.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
from dataclasses import dataclass, field

from . import fileops
from .arr import ArrClient
from .config import SETTINGS


@dataclass
class RenamePlan:
    arr_name: str
    kind: str                    # sonarr | radarr
    parent_id: int
    parent_title: str
    file_id: int
    existing_abs: str
    new_abs: str
    existing_rel: str
    new_rel: str
    issues: list[str] = field(default_factory=list)
    locked_by: list[str] = field(default_factory=list)
    corrupt_group: str | None = None
    good_group: str | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.issues)

    def describe(self) -> str:
        flag = "BLOCKED" if self.blocked else "ready"
        s = f"[{flag}] {self.parent_title} (fileId {self.file_id})\n"
        s += f"    from: {os.path.basename(self.existing_rel)}\n"
        s += f"    to  : {os.path.basename(self.new_rel)}"
        if self.issues:
            s += "\n    ! " + "\n    ! ".join(self.issues)
        return s


# ---------------------------------------------------------------- CHECK ----
async def plan_for_parent(client: ArrClient, parent_id: int,
                          parent_title: str = "", parent_path: str | None = None
                          ) -> list[RenamePlan]:
    """Build a pre-flighted rename plan for one series/movie."""
    rows = await client.rename_preview(parent_id)
    if not rows:
        return []

    base = parent_path
    if not base or not parent_title:
        ep = "/series/" if client.cfg.kind == "sonarr" else "/movie/"
        try:
            meta = await client._get(f"{ep}{parent_id}")
            base = base or meta.get("path")
            parent_title = parent_title or meta.get("title") or ""
        except Exception:
            pass
    if not base:
        return []

    id_key = "episodeFileId" if client.cfg.kind == "sonarr" else "movieFileId"
    plans: list[RenamePlan] = []

    for row in rows:
        ex_rel = row.get("existingPath") or ""
        nw_rel = row.get("newPath") or ""
        if not ex_rel or not nw_rel:
            continue
        ex_abs = os.path.join(base, ex_rel)
        nw_abs = os.path.join(base, nw_rel)

        p = RenamePlan(
            arr_name=client.cfg.name,
            kind=client.cfg.kind,
            parent_id=parent_id,
            parent_title=parent_title,
            file_id=row.get(id_key) or 0,
            existing_abs=ex_abs,
            new_abs=nw_abs,
            existing_rel=ex_rel,
            new_rel=nw_rel,
        )

        # --- pre-flight ----------------------------------------------------
        if not os.path.exists(ex_abs):
            p.issues.append(f"source not on disk: {ex_abs}")
        # Only a real limit blocks a rename. This machine has LongPathsEnabled,
        # and Sonarr renames 300+ character paths here without complaint - so
        # refusing them made nuarr the only thing that could not do the job.
        # An arr rename is a move: it writes no temp file, so no margin either.
        if fileops.path_too_long(nw_abs, SETTINGS.max_path_length):
            p.issues.append(
                f"destination path {len(nw_abs)} chars, over limit "
                f"{SETTINGS.max_path_length} - arr rename will fail"
            )
        if os.path.exists(nw_abs) and os.path.normcase(nw_abs) != os.path.normcase(ex_abs):
            p.issues.append(f"destination already exists: {nw_abs}")
        bad_group, good_group = _corrupt_group(nw_abs, parent_title)
        if bad_group:
            # THE CORRUPTION FEEDBACK LOOP.
            # Once a rename has been truncated, the series title ends up inside
            # the filename. The arr then re-parses that file, reads the junk as
            # the RELEASE GROUP, and bakes it into the next name - truncated at
            # a different point each pass. Observed live:
            #   on disk : ...[JA]-JosekiTurn A Gundam 1999.mkv
            #   proposed: ...[JA]-JosekiTurn.mkv          (group is really 'Joseki')
            # Renaming would "succeed" and leave the file still wrong, forever.
            # Refuse, and carry the correct group so it can be repaired.
            p.issues.append(
                f"proposed name embeds a corrupted release group "
                f"'{bad_group}'"
                + (f" - siblings agree the group is '{good_group}'" if good_group else "")
            )
            p.corrupt_group = bad_group
            p.good_group = good_group

        if os.path.exists(ex_abs) and fileops.is_locked(ex_abs):
            # not a blocker - we wait for locks - but surface who has it
            p.locked_by = fileops.who_locks(ex_abs)

        plans.append(p)

    return plans


async def plan_all(client: ArrClient, concurrency: int = 20,
                   limit: int | None = None) -> list[RenamePlan]:
    """Pre-flight the whole library for this arr.

    Uses bounded concurrency: a sequential pass over 1,081 series does not
    finish in a sane time, and the arr will happily serve 20 at once.
    """
    ep = "/series" if client.cfg.kind == "sonarr" else "/movie"
    items = await client._get(ep)
    if limit:
        items = items[:limit]

    sem = asyncio.Semaphore(concurrency)

    async def one(item):
        async with sem:
            try:
                return await plan_for_parent(
                    client, item["id"], item.get("title") or "", item.get("path")
                )
            except Exception:
                return []

    out: list[RenamePlan] = []
    for chunk in await asyncio.gather(*(one(i) for i in items)):
        out.extend(chunk)
    return out


# ------------------------------------------------------------ DIAGNOSE ----
_MEDIA_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}
_EP_TOKEN = re.compile(r"S(\d{1,3})E(\d{1,4})", re.I)


def _norm_ep(m) -> str | None:
    """'S01E37' and 'S1E37' must compare equal."""
    return f"{int(m.group(1))}x{int(m.group(2))}" if m else None


@dataclass
class Diagnosis:
    category: str        # orphan_record | duplicate | too_long | locked | unknown | ok
    explanation: str
    remedy: str
    auto_fixable: bool = False
    candidate: str | None = None      # the file we believe the record should point at


_GROUP_RE = re.compile(r"\]-([^\]\[/\\]+)$")
_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def _group_of(name: str) -> str | None:
    """The trailing release group of a scene-style filename."""
    m = _GROUP_RE.search(os.path.splitext(os.path.basename(name))[0])
    return m.group(1).strip() if m else None


def _normcat(s: str) -> str:
    """Lowercased, punctuation and spacing stripped - the shape a title takes
    once a template glues it into a filename token."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _cut_raw(grp: str, keep_norm_len: int) -> str | None:
    """The raw prefix of `grp` whose normalized length is keep_norm_len.

    Walks the raw string counting only the characters _normcat keeps, so the
    cut lands correctly even when the real group contains dots or dashes
    ('D-Z0N3'). Trailing joiner punctuation is stripped from the result.
    """
    if keep_norm_len <= 0:
        return None
    n = 0
    for i, ch in enumerate(grp):
        if ch.isalnum():
            n += 1
            if n == keep_norm_len:
                return grp[:i + 1].rstrip(" ._-") or None
    return None


def _corrupt_group(new_abs: str, parent_title: str) -> tuple[str | None, str | None]:
    """Detect a release group polluted by the series/movie title.

    Returns (corrupt_group, clean_group). Three tiers of evidence, because the
    two real-world causes look different and the old single heuristic was
    wrong about both at once:

    TIER 1 - THE TEMPLATE ACCIDENT (certain; sibling unanimity CANNOT rescue).
    Profilarr's format dropdown makes it easy to paste a new format and leave
    a stray token behind, ending the template with
        ...{-Release Group}{Series Title}
    so every file the arr renames gets the FULL title glued to its group:
    '-GROUPSeries Title'. The old check leaned on "sibling unanimity means a
    real group" - and this accident renames the whole folder in one pass, so
    every sibling agrees on the same mangled group and unanimity is exactly
    what the accident produces. The full normalized title as a suffix of the
    group is not something a real group can be (a group merely NAMED like the
    title matches exactly, not as a strict suffix), so it condemns on its own
    and the clean group is recovered by cutting the title off - which makes
    single-file movie folders fixable too, where there are no siblings to ask.

    TIER 2 - THE TRUNCATION LOOP (strong; unanimity rescues). A previously
    truncated rename leaves a PARTIAL title in the name ('JosekiTurn' for
    'Turn A Gundam'); the arr re-parses the junk as the group and bakes it
    into the next name, cut at a different point each pass. Because the cut
    moves, the mangled groups differ between files - so a folder that AGREES
    on the suspect group is showing the signature of a real group ('kun' on
    "Yamada-kun at Lv999": thirteen episodes, all correct, all blocked by the
    old logic) and is left alone.

    TIER 3 - WORD OVERLAP (weak; needs an implausible group too). The old
    check flagged on ANY shared word >= 3 chars, so a group that legitimately
    shares one word with the title was condemned with only unanimity as an
    escape - an escape a movie folder with one file can never take. A shared
    word now only counts against a group too long to be plausible (real
    groups run short; only title contamination makes them long), and sibling
    unanimity still rescues.
    """
    grp = _group_of(new_abs)
    if not grp or not parent_title:
        return None, None

    grp_norm = _normcat(grp)
    title_norm = _normcat(parent_title)
    if len(title_norm) < 4:
        return None, None       # too little title to be evidence of anything

    # ---- tier 1: the full title glued onto the group ----------------------
    # Checked with and without trailing year digits, because the year token
    # rides along in some formats ('...JosekiTurn A Gundam 1999').
    for cand in (grp_norm, re.sub(r"\d{1,4}$", "", grp_norm)):
        if cand.endswith(title_norm) and len(cand) > len(title_norm):
            clean = _cut_raw(grp, len(cand) - len(title_norm))
            return grp, clean

    # From here on, evidence is fallible - collect the neighbours' verdict.
    folder = os.path.dirname(new_abs)
    counts: dict[str, int] = {}
    for f in _folder_media(folder):
        if os.path.normcase(f) == os.path.normcase(new_abs):
            continue
        g = _group_of(f)
        if g:
            counts[g] = counts.get(g, 0) + 1
    unanimous = counts.get(grp, 0) >= 2 and \
        counts.get(grp, 0) >= max(counts.values())

    title_words = {w.lower() for w in _WORD_RE.findall(parent_title)}
    title_words -= {"the", "and", "movie", "season", "part"}

    # ---- tier 2: a partial title (truncation cut) ending the group --------
    hit = False
    for k in range(len(title_norm), 3, -1):
        pre = title_norm[:k]
        if grp_norm.endswith(pre) and len(grp_norm) > k:
            hit = True
            break

    # ---- tier 3: shared words, only against an implausibly long group -----
    if not hit and len(grp_norm) >= 15:
        grp_words = {w.lower() for w in _WORD_RE.findall(grp)}
        hit = bool(title_words & grp_words) or \
            any(w in grp_norm for w in title_words if len(w) >= 4)
    if not hit:
        return None, None
    if unanimous:
        return None, None       # truncation is never unanimous; real groups are

    clean = None
    if counts:
        cand, n = max(counts.items(), key=lambda kv: kv[1])
        # only trust a consensus that is itself uncontaminated
        cand_words = {w.lower() for w in _WORD_RE.findall(cand)}
        if n >= 2 and not (title_words & cand_words):
            clean = cand
    return grp, clean


def _folder_media(folder: str) -> list[str]:
    try:
        return [os.path.join(folder, f) for f in os.listdir(folder)
                if os.path.splitext(f)[1].lower() in _MEDIA_EXT]
    except OSError:
        return []


def diagnose(plan: RenamePlan) -> Diagnosis:
    """Explain WHY a rename is blocked and what would clear it.

    'Blocked' on its own just moves the manual work around. Every category here
    was observed on this library, so each one names the actual remedy.
    """
    if not plan.issues:
        return Diagnosis("ok", "no blockers", "apply the rename", True)

    joined = " ".join(plan.issues)

    # --- the arr's record points at a file that is not there ---------------
    if "source not on disk" in joined:
        folder = os.path.dirname(plan.existing_abs)
        want = os.path.basename(plan.new_abs)
        cands = _folder_media(folder)
        # Finding the real file must key on the EPISODE TOKEN, not a shared
        # prefix: every episode in a season folder starts with the same series
        # title, so prefix scoring happily returns the wrong episode (it picked
        # S01E30 for an S01E37 rename). The SxxExx token is the identity here;
        # a fuzzy ratio only breaks ties.
        best, best_score = None, 0.0
        tok = _EP_TOKEN.search(want)
        if tok:
            exact = [c for c in cands
                     if _norm_ep(_EP_TOKEN.search(os.path.basename(c)))
                     == _norm_ep(tok)]
            if len(exact) == 1:
                best, best_score = exact[0], 1.0
            elif exact:
                cands = exact
        if best is None:
            for c in cands:
                score = difflib.SequenceMatcher(
                    None, os.path.basename(c).lower(), want.lower()
                ).ratio()
                if score > best_score:
                    best, best_score = c, score
        if best and best_score >= 0.6:
            return Diagnosis(
                "orphan_record",
                f"the arr's database points at a filename that no longer exists; "
                f"the episode IS on disk as '{os.path.basename(best)}' - a previous "
                f"rename applied to the file but was not recorded (or was truncated)",
                "RefreshSeries/RefreshMovie so the arr re-links to the real file, "
                "then re-plan the rename",
                auto_fixable=True,
                candidate=best,
            )
        return Diagnosis(
            "orphan_record",
            "the arr's database points at a file that is not on disk, and no "
            "close match was found in the folder",
            "rescan the folder; if still missing the file was lost and needs "
            "re-downloading",
            auto_fixable=False,
        )

    # --- destination occupied ---------------------------------------------
    if "already exists" in joined:
        detail = ""
        auto = False
        try:
            if os.path.exists(plan.existing_abs) and os.path.exists(plan.new_abs):
                s_old = os.path.getsize(plan.existing_abs)
                s_new = os.path.getsize(plan.new_abs)
                delta = abs(s_old - s_new)
                if s_old == s_new and (fileops.quick_sig(plan.existing_abs)
                                       == fileops.quick_sig(plan.new_abs)):
                    detail = (f"both are {s_old/1024**3:.2f} GB with a matching "
                              f"signature - the SAME file twice")
                    auto = True
                elif delta <= max(65536, s_old // 10000):
                    # Observed: a 3.12 GB pair differing by 38 bytes. That is a
                    # container/tag rewrite (mkvpropedit, a title or language
                    # edit), not a second encode. Calling that "different files"
                    # sends you off to compare two identical movies by hand.
                    detail = (f"{s_old/1024**3:.2f} GB vs {s_new/1024**3:.2f} GB - "
                              f"only {delta} bytes apart, so this is almost certainly "
                              f"the same content re-tagged/remuxed, wasting "
                              f"{s_new/1024**3:.2f} GB")
                else:
                    detail = (f"existing {s_old/1024**3:.2f} GB vs destination "
                              f"{s_new/1024**3:.2f} GB ({delta/1024**2:.0f} MB apart) "
                              f"- genuinely different files")
        except OSError as e:
            detail = f"could not compare: {e}"
        return Diagnosis(
            "duplicate",
            f"a file already sits at the target name. {detail}",
            "confirm which copy to keep; nuarr will not delete media on its own. "
            "Once one is removed, re-run the rename.",
            auto_fixable=False,
        )

    # --- corrupted release group ------------------------------------------
    if "corrupted release group" in joined:
        if plan.good_group:
            fixed = os.path.basename(plan.new_abs).replace(
                f"-{plan.corrupt_group}", f"-{plan.good_group}")
            return Diagnosis(
                "corrupt_parse",
                f"the title got written into the filename - either a truncated "
                f"rename, or a naming format with a stray title token after the "
                f"release group (Profilarr's dropdown makes that an easy paste "
                f"accident). The arr now reads the release group as "
                f"'{plan.corrupt_group}' instead of '{plan.good_group}'. "
                f"Applying this rename would 'succeed' and still leave the "
                f"file wrong.",
                f"rename the file to the sibling-consistent name '{fixed}', then "
                f"rescan so the arr re-parses it correctly",
                auto_fixable=True,
                candidate=os.path.join(os.path.dirname(plan.new_abs), fixed),
            )
        return Diagnosis(
            "corrupt_parse",
            f"the release group '{plan.corrupt_group}' contains the title, so the "
            f"filename was mangled by an earlier truncated rename. No sibling "
            f"consensus was available to infer the correct group.",
            "correct the filename by hand (or from another episode of the same "
            "release), then rescan",
            auto_fixable=False,
        )

    # --- path length -------------------------------------------------------
    if "over limit" in joined:
        return Diagnosis(
            "too_long",
            f"the name the arr wants is {len(plan.new_abs)} characters, past the "
            f"Windows limit. The arr will start this rename and fail partway, "
            f"which is what leaves mangled names behind.",
            "shorten the naming format in Profilarr (drop optional tokens) or "
            "shorten the folder name; nuarr refuses to attempt it meanwhile",
            auto_fixable=False,
        )

    return Diagnosis("unknown", joined, "manual review", False)


async def repair_orphan(client: ArrClient, plan: RenamePlan,
                        confirm: bool = False) -> fileops.OpResult:
    """Fix an orphaned arr record by making the arr rescan the folder.

    Safe by construction: this only asks the arr to re-read the folder. No file
    is moved, renamed or deleted.
    """
    d = diagnose(plan)
    if d.category != "orphan_record" or not d.auto_fixable:
        return fileops.OpResult(False, "repair", f"not an auto-fixable orphan: {d.category}")
    if not confirm:
        return fileops.OpResult(True, "repair",
                                f"DRY RUN - would rescan {plan.parent_title} to re-link "
                                f"{os.path.basename(d.candidate or '')}")
    try:
        await client.notify_file_changed(plan.parent_id)
        return fileops.OpResult(True, "repair",
                                f"rescan requested for {plan.parent_title}; "
                                "re-plan once it completes")
    except Exception as e:
        return fileops.OpResult(False, "repair", f"rescan failed: {e}")


# ------------------------------------------------------- APPLY + VERIFY ----
async def _verify(client: ArrClient, plan: RenamePlan) -> tuple[bool, str]:
    """Did the rename actually land - on disk AND in the arr's record?"""
    if not os.path.exists(plan.new_abs):
        return False, "new path not on disk"
    if os.path.getsize(plan.new_abs) == 0:
        return False, "new path is 0 bytes"
    rec = await client.file_record(plan.file_id)
    if rec:
        recorded = rec.get("path") or ""
        if os.path.normcase(recorded) != os.path.normcase(plan.new_abs):
            return False, f"arr still records {recorded}"
    return True, "verified on disk and in arr"


async def apply_rename(client: ArrClient, plan: RenamePlan, *, confirm: bool = False,
                       attempts: int = 4, base_delay: float = 5.0,
                       lock_timeout: float = 600,
                       allow_fallback: bool = True) -> fileops.OpResult:
    """Execute one planned rename, waiting and retrying rather than giving up.

    confirm=False is a DRY RUN and never touches disk.
    """
    if plan.blocked:
        return fileops.OpResult(False, "rename", "blocked in pre-flight: " +
                                "; ".join(plan.issues), locked_by=plan.locked_by)
    if not confirm:
        return fileops.OpResult(True, "rename", "DRY RUN - would rename "
                                f"{plan.existing_rel} -> {plan.new_rel}")

    waited = 0.0
    holders: list[str] = []

    for attempt in range(1, attempts + 1):
        # someone may have already fixed it between planning and now
        ok, why = await _verify(client, plan)
        if ok:
            return fileops.OpResult(True, "rename", why, attempt, waited, holders)

        # 1. never fight the arr - if it is mid-rename/scan, wait it out
        busy, reason = await client.busy()
        if busy:
            await asyncio.sleep(base_delay)
            waited += base_delay
            continue

        # 2. wait for the file to be free BEFORE asking the arr to move it.
        #    This is the step Tdarr skips, and the reason it fails on files
        #    Plex happens to be scanning.
        if os.path.exists(plan.existing_abs):
            unlocked, w, holders = await asyncio.to_thread(
                fileops.wait_for_unlock, plan.existing_abs, lock_timeout
            )
            waited += w
            if not unlocked:
                if attempt == attempts:
                    return fileops.OpResult(False, "rename",
                                            "file stayed locked", attempt, waited, holders)
                continue

        # 3. ask the ARR to rename, then WAIT for the command to finish
        try:
            cmd = await client.rename_files(plan.parent_id, [plan.file_id])
            cid = cmd.get("id")
            if cid:
                await client.wait_command(cid, timeout_s=lock_timeout)
        except Exception as e:
            if attempt == attempts and not allow_fallback:
                return fileops.OpResult(False, "rename", f"arr command failed: {e}",
                                        attempt, waited, holders)

        # 4. verify
        ok, why = await _verify(client, plan)
        if ok:
            return fileops.OpResult(True, "rename", why, attempt, waited, holders)

        if attempt < attempts:
            delay = base_delay * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
            waited += delay

    # 5. the arr gave up. Do the move ourselves - safely - then tell it.
    if allow_fallback and os.path.exists(plan.existing_abs):
        res = await asyncio.to_thread(
            fileops.safe_rename, plan.existing_abs, plan.new_abs
        )
        res.attempts += attempts
        res.waited_s += waited
        if res.ok:
            try:
                await client.notify_file_changed(plan.parent_id)
                res.detail += " (renamed by nuarr, arr notified to rescan)"
            except Exception as e:
                res.detail += f" (renamed by nuarr, arr notify failed: {e})"
        return res

    return fileops.OpResult(False, "rename", "arr rename did not take effect",
                            attempts, waited, holders)


async def repair_corrupt_name(client: ArrClient, plan: RenamePlan,
                              confirm: bool = False) -> fileops.OpResult:
    """Break the corruption loop by writing the sibling-consistent name.

    Uses the same lock-wait/verify machinery as any other move, so a file Plex
    happens to be reading is waited for rather than abandoned.
    """
    d = diagnose(plan)
    if d.category != "corrupt_parse" or not d.auto_fixable or not d.candidate:
        return fileops.OpResult(False, "repair", f"not auto-fixable: {d.category}")

    src = plan.existing_abs
    dst = d.candidate
    if not os.path.exists(src):
        return fileops.OpResult(False, "repair", f"source missing: {src}")
    if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
        return fileops.OpResult(False, "repair", f"target already exists: {dst}")
    if not confirm:
        return fileops.OpResult(True, "repair",
                                f"DRY RUN - would rename to {os.path.basename(dst)}")

    res = await asyncio.to_thread(fileops.safe_rename, src, dst)
    if res.ok:
        try:
            await client.notify_file_changed(plan.parent_id)
            res.detail += " (arr notified to re-parse)"
        except Exception as e:
            res.detail += f" (arr notify failed: {e})"
    return res


async def apply_many(client: ArrClient, plans: list[RenamePlan], *,
                     confirm: bool = False, concurrency: int = 3
                     ) -> list[tuple[RenamePlan, fileops.OpResult]]:
    """Apply a set of renames.

    Deliberately low concurrency: renames are metadata operations on the same
    pool, and running many at once is how you provoke the balancer races this
    whole module exists to survive.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(p: RenamePlan):
        async with sem:
            return p, await apply_rename(client, p, confirm=confirm)

    return list(await asyncio.gather(*(one(p) for p in plans)))


def summarise(results: list[tuple[RenamePlan, fileops.OpResult]]) -> str:
    ok = [r for _, r in results if r.ok]
    bad = [(p, r) for p, r in results if not r.ok]
    lines = [f"renamed/verified: {len(ok)}    needs attention: {len(bad)}"]
    for p, r in bad:
        lines.append(f"  - {p.parent_title} [{p.file_id}]: {r.detail}")
        if r.locked_by:
            lines.append(f"      locked by: {', '.join(r.locked_by)}")
    return "\n".join(lines)
