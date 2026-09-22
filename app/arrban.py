r"""Releases nuarr has rejected, pushed back to the arrs as a custom format.

WHY A BLOCKLIST ENTRY IS NOT ENOUGH. Erik: "the blocklist only blocks that
one entry from that indexer and many indexers carry the same file". True.
When nuarr rejects a release - no English audio in a file that said DUAL,
no readable subtitle in a raw, a container that does not decode - refetch
blocklists the GRAB, which is one (indexer, release) pair. The same release
is on four other indexers under the same name, and the search that follows
the blocklist is free to take it from any of them. Nuarr then reads it
again, rejects it again, and the two of them can go round like that until
the retry cap stops it.

WHAT THIS DOES. Every rejection lands here as a ban on the RELEASE NAME,
and the bans are pushed into Sonarr and Radarr as custom formats scored
-10000 on the profiles you name - which is what a custom format is for:
it is judged against every candidate from every indexer, before the grab.
The name is matched loosely (dots, spaces and underscores interchangeable,
container extension ignored) because indexers differ in exactly that.

AND IT LEARNS. A release group that keeps producing rejected releases is
the problem, not the release, so after GROUP_STRIKES rejections from one
group the GROUP is banned - a ReleaseGroupSpecification, which catches
everything it puts out from then on. A person can ban a group outright,
un-ban one nuarr got wrong, or add a release by hand; the list on the
Custom arrs scripts card is the whole state and it is editable.

WHAT IT DOES NOT DO. It never deletes a format it did not make, never
touches a profile you have not named, and a ban you switch off is kept
(off) rather than forgotten, so "why is this back" has an answer.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from .db import cursor
from . import joblog

CF_RELEASES = "Nuarr: rejected releases"
CF_GROUPS = "Nuarr: rejected groups"
# A THIRD KIND, BECAUSE A RELEASE PROFILE HELD THREE. Erik's "must not
# contain" lists were not all group names: ".arj", "sub es-ES", "KOR DUB"
# and "/(XKsub)/i" are substrings and regexes, and a group specification
# would quietly match none of them. A term is matched wherever it appears
# in the name, which is exactly what a release profile did.
CF_TERMS = "Nuarr: banned terms"
SCORE = -10000
GROUP_STRIKES = 3           # rejections from one group before the group is banned
PER_FORMAT = 250            # release patterns per format before rolling over
SYNC_DEBOUNCE_S = 45.0

# EMPTY MEANS EVERY PROFILE. It used to mean every "Nu ..." and "Anime"
# one, which was this machine's profile names doing the job a setting
# should do - and on any other install it silently scored nothing. Erik:
# "can you just make it every Profile if not selected below".
DEFAULT_PROFILE_RULE = None

STATS: dict = {"last_run": 0.0, "last_result": "", "next_run": 0.0,
               "detail": [], "dirty": False, "dirty_at": 0.0}
_READY = False


def init() -> None:
    global _READY
    if _READY:
        return
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arr_bans(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                kind     TEXT NOT NULL,          -- release | group
                value    TEXT NOT NULL,          -- the name, as seen
                pattern  TEXT NOT NULL DEFAULT '',
                why      TEXT NOT NULL DEFAULT '',
                source   TEXT NOT NULL DEFAULT '',
                added_by TEXT NOT NULL DEFAULT 'nuarr',
                added_at REAL NOT NULL DEFAULT 0,
                hits     INTEGER NOT NULL DEFAULT 1,
                enabled  INTEGER NOT NULL DEFAULT 1,
                UNIQUE(kind, value)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS arr_ban_strikes(
                grp   TEXT PRIMARY KEY,
                n     INTEGER NOT NULL DEFAULT 0,
                last  REAL NOT NULL DEFAULT 0,
                why   TEXT NOT NULL DEFAULT ''
            )""")
    _READY = True


# ------------------------------------------------------------ patterns -----
# WHAT MAY BE ESCAPED, AND NOTHING ELSE. These patterns go to Sonarr, which
# compiles them with .NET, and .NET REJECTS an escape it does not recognise:
# "JAM_CLUB" escaped character by character becomes JAM\_CLUB, and \_ is not
# a legal escape there, so the whole custom format was refused with a 500 and
# 53 imported terms scored nothing. Python's re is happy with it, which is
# why it passed every test that did not talk to an arr.
_META = set(".^$*+?()[]{}|\\/")


def _esc(ch: str) -> str:
    return ("\\" + ch) if ch in _META else ch


def _clean_title(title: str) -> str:
    t = str(title or "").strip()
    t = re.sub(r"\.(mkv|mp4|avi|m4v|ts|webm)$", "", t, flags=re.I)
    return t


def release_pattern(title: str) -> str:
    """A .NET-safe regex that matches this release name however an indexer
    spells its separators. Anchored at the start; the end is left open so
    an indexer's suffix (".mkv", "[eztv]") does not let it through."""
    t = _clean_title(title)
    out = []
    for ch in t:
        if ch in " ._-":
            if out and out[-1] == "[ ._-]+":
                continue
            out.append("[ ._-]+")
        else:
            out.append(_esc(ch))
    return "^" + "".join(out) + r"(?![A-Za-z0-9])"


def group_pattern(group: str) -> str:
    g = str(group or "").strip()
    return "^" + "".join(_esc(c) for c in g) + "$"


def term_pattern(term: str) -> str:
    """Anywhere in the name. A release profile's own /regex/i syntax is kept
    as the regex it is; anything else is matched literally."""
    t = str(term or "").strip()
    m = re.match(r"^/(.*)/(i)?$", t)
    if m:
        return m.group(1)
    return "".join(_esc(c) for c in t)


def group_of(title: str) -> str:
    """The release group the arrs would read from this name, best effort."""
    t = _clean_title(title)
    m = re.search(r"-([A-Za-z0-9]+(?:\[[^\]]*\])?)\s*$", t)
    if m:
        return re.sub(r"\[.*\]$", "", m.group(1)).strip()
    m = re.match(r"^\[([^\]]+)\]", t)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------- the list ----
def rows(include_off: bool = True) -> list:
    init()
    with cursor() as cur:
        q = ("SELECT id, kind, value, pattern, why, source, added_by, "
             "       added_at, hits, enabled FROM arr_bans ")
        if not include_off:
            q += " WHERE enabled=1 "
        q += " ORDER BY kind, added_at DESC"
        return [dict(r) for r in cur.execute(q)]


def strikes() -> list:
    init()
    with cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT grp, n, last, why FROM arr_ban_strikes ORDER BY n DESC")]


def _mark_dirty() -> None:
    STATS["dirty"] = True
    STATS["dirty_at"] = time.time()


def add(kind: str, value: str, why: str = "", source: str = "",
        by: str = "you") -> dict:
    """Add or bump one ban. Returns the row and whether it is new."""
    init()
    k = str(kind).lower()
    kind = ("group" if k.startswith("g")
            else "term" if k.startswith("t") else "release")
    value = _clean_title(value) if kind == "release" else str(value).strip()
    if not value:
        return {"ok": False, "why": "nothing to ban"}
    pat = (release_pattern(value) if kind == "release"
           else group_pattern(value) if kind == "group"
           else term_pattern(value))
    now = time.time()
    with cursor() as cur:
        r = cur.execute("SELECT id, hits FROM arr_bans WHERE kind=? AND value=?",
                        (kind, value)).fetchone()
        if r:
            cur.execute("UPDATE arr_bans SET hits=hits+1, why=COALESCE(NULLIF(?,''),why) "
                        " WHERE id=?", (why, int(r["id"])))
            new = False
            bid = int(r["id"])
        else:
            cur.execute(
                "INSERT INTO arr_bans(kind,value,pattern,why,source,added_by,"
                " added_at,hits,enabled) VALUES(?,?,?,?,?,?,?,1,1)",
                (kind, value, pat, why or "", source or "", by, now))
            new = True
            bid = int(cur.lastrowid)
    _mark_dirty()
    return {"ok": True, "id": bid, "new": new, "kind": kind, "value": value}


def remove(ban_id: int) -> bool:
    init()
    with cursor() as cur:
        cur.execute("DELETE FROM arr_bans WHERE id=?", (int(ban_id),))
        n = cur.rowcount
    _mark_dirty()
    return bool(n)


def set_enabled(ban_id: int, on: bool) -> bool:
    init()
    with cursor() as cur:
        cur.execute("UPDATE arr_bans SET enabled=? WHERE id=?",
                    (1 if on else 0, int(ban_id)))
        n = cur.rowcount
    _mark_dirty()
    return bool(n)


def note_rejection(release: str, why: str = "", source: str = "",
                   group: str = "") -> dict:
    """What refetch calls the moment it has rejected a release.

    The release is banned by name; its group takes a strike, and at
    GROUP_STRIKES the group is banned too. Nothing here talks to an arr -
    the sync does that, debounced, so a batch of twenty rejections is one
    round of format updates rather than twenty.
    """
    init()
    out = {"release": None, "group": None, "group_banned": False}
    rel = _clean_title(release)
    if not rel:
        return out
    out["release"] = add("release", rel, why, source, by="nuarr")
    grp = (group or group_of(rel)).strip()
    if not grp or len(grp) < 2:
        return out
    out["group"] = grp
    now = time.time()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO arr_ban_strikes(grp,n,last,why) VALUES(?,1,?,?) "
            " ON CONFLICT(grp) DO UPDATE SET n=n+1, last=excluded.last, "
            " why=excluded.why", (grp, now, why or ""))
        n = int(cur.execute("SELECT n FROM arr_ban_strikes WHERE grp=?",
                            (grp,)).fetchone()["n"])
    if n >= GROUP_STRIKES:
        r = add("group", grp, f"{n} rejected releases - last: {why}",
                source, by="nuarr")
        out["group_banned"] = bool(r.get("new"))
        if r.get("new"):
            joblog.log(f"release group {grp!r} banned in the arrs after {n} "
                       f"rejected releases", "warn")
    return out


# --------------------------------------------------------------- profiles --
def profiles_for() -> dict:
    """{'radarr': [...names...], 'sonarr': [...]} - stored, or the rule."""
    from . import arrguard
    return arrguard._load_names("arrs.ban_profiles", {"radarr": [], "sonarr": []})


def _want_profile(name: str, listed: list) -> bool:
    """Named profiles only, or - with nothing named - all of them."""
    if listed:
        return name in listed
    if DEFAULT_PROFILE_RULE is None:
        return True
    return bool(DEFAULT_PROFILE_RULE.search(name or ""))


# ------------------------------------------------------------------ sync ----
def _chunks(xs: list, n: int) -> list:
    return [xs[i:i + n] for i in range(0, len(xs), n)] or [[]]


async def sync(force: bool = False) -> str:
    r"""Make the arrs' formats match the list, and score them on the profiles.

    Formats this owns are found BY NAME - CF_RELEASES, CF_RELEASES " 2", ...
    and CF_GROUPS. Each is rebuilt from the list; ones no longer needed are
    deleted (they are nuarr's own). A format with no patterns would match
    nothing, so the list being empty leaves one placeholder spec that can
    never match rather than a format the arr rejects as invalid.
    """
    from .arr import shared_client
    from .config import SETTINGS
    init()
    try:
        restamp()
    except Exception:                                            # noqa: BLE001
        pass
    live = [r for r in rows() if r["enabled"]]
    rel = [r for r in live if r["kind"] == "release"]
    grp = [r for r in live if r["kind"] == "group"]
    trm = [r for r in live if r["kind"] == "term"]
    names = profiles_for()
    detail: list = []
    STATS["dirty"] = False
    STATS["last_run"] = time.time()
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in ("radarr", "sonarr"):
            continue
        c = shared_client(cfg)
        try:
            have = {f["name"]: f for f in await c._get("/customformat")}
            wanted: dict = {}
            for i, chunk in enumerate(_chunks(rel, PER_FORMAT)):
                nm = CF_RELEASES if i == 0 else f"{CF_RELEASES} {i + 1}"
                specs = [{"name": r["value"][:80],
                          "implementation": "ReleaseTitleSpecification",
                          "negate": False, "required": False,
                          "fields": [{"name": "value", "value": r["pattern"]}]}
                         for r in chunk] or [{
                    "name": "nothing rejected yet",
                    "implementation": "ReleaseTitleSpecification",
                    "negate": False, "required": True,
                    "fields": [{"name": "value", "value": r"^\b$"}]}]
                wanted[nm] = specs
            wanted[CF_TERMS] = [{
                "name": r["value"][:80],
                "implementation": "ReleaseTitleSpecification",
                "negate": False, "required": False,
                "fields": [{"name": "value", "value": r["pattern"]}]}
                for r in trm] or [{
                "name": "no term banned yet",
                "implementation": "ReleaseTitleSpecification",
                "negate": False, "required": True,
                "fields": [{"name": "value", "value": r"^\b$"}]}]
            wanted[CF_GROUPS] = [{
                "name": r["value"][:80],
                "implementation": "ReleaseGroupSpecification",
                "negate": False, "required": False,
                "fields": [{"name": "value", "value": r["pattern"]}]}
                for r in grp] or [{
                "name": "no group banned yet",
                "implementation": "ReleaseGroupSpecification",
                "negate": False, "required": True,
                "fields": [{"name": "value", "value": r"^\b$"}]}]
            ids: dict = {}
            for nm, specs in wanted.items():
                body = {"name": nm, "includeCustomFormatWhenRenaming": False,
                        "specifications": specs}
                # ONE FORMAT FAILING MUST NOT COST THE SCORING. The first live
                # sync got a 500 from Sonarr on a create that had in fact
                # succeeded, and the exception skipped the profile step, so
                # the format existed and did nothing. Re-read after a failure
                # - it may be there - and carry on with what is.
                try:
                    if nm in have:
                        body["id"] = have[nm]["id"]
                        r = await c._put(f"/customformat/{have[nm]['id']}", body)
                    else:
                        r = await c._post("/customformat", body)
                    ids[nm] = int(r["id"])
                except Exception as e:                           # noqa: BLE001
                    again = {f["name"]: f for f in await c._get("/customformat")}
                    if nm in again:
                        ids[nm] = int(again[nm]["id"])
                        have[nm] = again[nm]
                    else:
                        detail.append(f"{cfg.name}: {nm}: "
                                      f"{type(e).__name__}: {e}")
            # formats of ours that are no longer needed (a list that shrank)
            for nm, f in have.items():
                if (nm.startswith(CF_RELEASES) or nm in (CF_GROUPS, CF_TERMS)) \
                        and nm not in wanted:
                    await c._delete(f"/customformat/{f['id']}")
            # and the score, on every profile named - EACH ONE ON ITS OWN.
            # A profile the arr refuses to save must not cost the others:
            # Radarr would not accept its own "HD-All" and "Animation"
            # profiles at all ("Minimum Custom Format Score can never be
            # satisfied" - both ask for 1 point and have nothing positive to
            # score), and that one 400 aborted the whole app's pass, so
            # nothing in Radarr was scored.
            scored, refused = [], []
            for pr in await c._get("/qualityprofile"):
                if not _want_profile(pr.get("name") or "", names.get(cfg.kind) or []):
                    continue
                try:
                    full = await c._get(f"/qualityprofile/{pr['id']}")
                    items = full.get("formatItems") or []
                    seen = {it.get("format") for it in items}
                    changed = False
                    for it in items:
                        if it.get("format") in ids.values() \
                                and it.get("score") != SCORE:
                            it["score"] = SCORE
                            changed = True
                    for nm, fid in ids.items():
                        if fid not in seen:
                            items.append({"format": fid, "name": nm,
                                          "score": SCORE})
                            changed = True
                    if changed:
                        full["formatItems"] = items
                        await c._put(f"/qualityprofile/{pr['id']}", full)
                    scored.append(pr["name"])
                except Exception as e:                           # noqa: BLE001
                    # THE REASON IS IN THE BODY, NOT THE MESSAGE. httpx's
                    # text is "400 Bad Request for url ..."; the arr's own
                    # sentence is in the response, and that is the one a
                    # person can act on.
                    why = str(e)
                    try:
                        body = e.response.text                   # type: ignore[attr-defined]
                        if "never be satisfied" in body:
                            why = ("minimum custom format score can never "
                                   "be met - fix the profile in Radarr")
                        else:
                            m = re.search(r'"errorMessage":\s*"([^"]+)"', body)
                            if m:
                                why = m.group(1)
                    except Exception:                            # noqa: BLE001
                        pass
                    refused.append(f"{pr.get('name')} - {why[:90]}")
            detail.append(f"{cfg.name}: {len(rel)} release(s), {len(grp)} "
                          f"group(s), {len(trm)} term(s) scored on "
                          f"{len(scored)} profile(s)"
                          + (f"; could not save {len(refused)}: "
                             + "; ".join(refused) if refused else ""))
        except Exception as e:                                   # noqa: BLE001
            detail.append(f"{cfg.name}: {type(e).__name__}: {e}")
    STATS["detail"] = detail
    STATS["last_result"] = "; ".join(detail)
    joblog.log("arr release bans synced - " + STATS["last_result"], "info")
    return STATS["last_result"]


async def sync_soon() -> None:
    """Debounced: a batch of rejections becomes one sync."""
    await asyncio.sleep(SYNC_DEBOUNCE_S)
    if STATS["dirty"]:
        try:
            await sync()
        except Exception as e:                                   # noqa: BLE001
            joblog.log(f"arr release bans: {type(e).__name__}: {e}", "error")


def kick() -> None:
    """Schedule a sync from sync code, if there is a loop to schedule on."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(sync_soon())


# ------------------------------------------ taking over a release profile --
def _looks_like_group(term: str) -> bool:
    """One token, no spaces, not an extension, not a regex - a group name."""
    t = str(term or "").strip()
    if not t or t.startswith("/") or t.startswith(".") or " " in t:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{1,}", t))


async def import_release_profiles(delete_after: bool = False,
                                  as_terms: bool = True) -> dict:
    r"""Take the must-not-contain lists out of the arrs' release profiles.

    WHY MOVE THEM AT ALL. A release profile is scoped by TAG - "Anime
    Minimum" only guards series tagged animemin - and it is invisible to
    the other arr. Erik keeps one list of things he does not want; keeping
    it in five places, per app, is how a group ends up banned for anime and
    not for television. As custom formats they are one list, scored on every
    profile, in both apps.

    EVERYTHING COMES ACROSS AS A TERM by default, and that is deliberate.
    A release profile matches its text ANYWHERE in the name; a group
    specification matches the parsed release group exactly. Importing
    "Hindi" as a group would quietly stop catching the releases it catches
    today. as_terms=False asks for the tighter reading where a term looks
    like a bare group name.
    """
    from .arr import shared_client
    from .config import SETTINGS
    init()
    out = {"added": 0, "already": 0, "profiles": [], "deleted": []}
    for cfg in SETTINGS.arrs:
        if not cfg.enabled or cfg.kind not in ("radarr", "sonarr"):
            continue
        c = shared_client(cfg)
        try:
            rps = await c._get("/releaseprofile")
        except Exception as e:                                   # noqa: BLE001
            out["profiles"].append(f"{cfg.name}: {type(e).__name__}: {e}")
            continue
        for rp in rps:
            ignored = [str(x) for x in (rp.get("ignored") or []) if str(x).strip()]
            if not ignored:
                continue
            name = rp.get("name") or f"profile {rp.get('id')}"
            for term in ignored:
                kind = ("group" if (not as_terms and _looks_like_group(term))
                        else "term")
                r = add(kind, term,
                        f"from {cfg.name} release profile {name!r}",
                        f"{cfg.kind}:releaseprofile", by="you")
                if r.get("new"):
                    out["added"] += 1
                else:
                    out["already"] += 1
            out["profiles"].append(f"{cfg.name}: {name} ({len(ignored)})")
            if delete_after:
                # A RELEASE PROFILE WITH NOTHING IN IT IS NOT VALID, so an
                # emptied one is deleted rather than saved blank. Anything it
                # also required or preferred is left alone - only the
                # must-not-contain list moved.
                try:
                    if rp.get("required") or rp.get("preferred"):
                        rp["ignored"] = []
                        await c._put(f"/releaseprofile/{rp['id']}", rp)
                        out["deleted"].append(f"{cfg.name}: {name} (emptied)")
                    else:
                        await c._delete(f"/releaseprofile/{rp['id']}")
                        out["deleted"].append(f"{cfg.name}: {name} (removed)")
                except Exception as e:                           # noqa: BLE001
                    out["profiles"].append(
                        f"{cfg.name}: could not clear {name}: "
                        f"{type(e).__name__}: {e}")
    if out["added"]:
        joblog.log(f"release profiles taken over: {out['added']} term(s) "
                   f"banned, {out['already']} already on the list", "ok")
    return out


def restamp() -> int:
    """Rewrite every stored pattern with the current rules.

    Needed once, after the escaping fix above: rows saved before it hold
    patterns .NET will not compile, and a single bad one costs the whole
    custom format.
    """
    init()
    n = 0
    with cursor() as cur:
        for r in cur.execute("SELECT id, kind, value, pattern "
                             "  FROM arr_bans").fetchall():
            want = (release_pattern(r["value"]) if r["kind"] == "release"
                    else group_pattern(r["value"]) if r["kind"] == "group"
                    else term_pattern(r["value"]))
            if want != r["pattern"]:
                cur.execute("UPDATE arr_bans SET pattern=? WHERE id=?",
                            (want, int(r["id"])))
                n += 1
    if n:
        _mark_dirty()
    return n


def snapshot() -> dict:
    init()
    rs = rows()
    return {"stats": dict(STATS), "rows": rs, "strikes": strikes(),
            "profiles": profiles_for(),
            "counts": {"release": sum(1 for r in rs if r["kind"] == "release"
                                      and r["enabled"]),
                       "group": sum(1 for r in rs if r["kind"] == "group"
                                    and r["enabled"]),
                       "term": sum(1 for r in rs if r["kind"] == "term"
                                   and r["enabled"]),
                       "off": sum(1 for r in rs if not r["enabled"])},
            "group_strikes": GROUP_STRIKES, "score": SCORE}
