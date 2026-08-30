"""
nuarr - Sonarr / Radarr client

Two jobs:
  1. Give every media file a DURABLE identity (episodeFileId / movieFileId)
     so renames stop resetting processing state.
  2. Tell us when an arr is mid-rename, so we can hold off touching files that
     are about to move underneath us. That race is what produced the
     ENOENT / EBUSY / "501 file/download" failures in the old setup.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import httpx

from .config import ArrConfig

_EP_RE = re.compile(r"S(\d{1,3})E(\d{1,4})(?:-E?(\d{1,4}))?", re.I)


def _quality_name(f) -> str | None:
    """quality.quality.name, surviving any level arriving as a string.

    The obvious spelling - .get("quality", {}).get("name") - looks safe but is
    not: a dict .get returns the VALUE whenever the key exists, so a string
    quality passes the default straight through and the next .get raises
    "'str' object has no attribute 'get'". That single line failed every
    Sonarr Download webhook. Coerce at each level instead of defaulting.
    """
    if not isinstance(f, dict):
        return None
    q = f.get("quality")
    q = q.get("quality") if isinstance(q, dict) else None
    return q.get("name") if isinstance(q, dict) else None


def _episodes_from_relpath(rel: str) -> str | None:
    """Sonarr's /episodefile gives no episodes array, so read it off the name.

    Display only - identity is always the file id, never this.
    """
    m = _EP_RE.search(rel)
    if not m:
        return None
    return f"{int(m.group(2))}-{int(m.group(3))}" if m.group(3) else str(int(m.group(2)))


# slots=True: one of these exists per file the arrs know about - 39,229 live at
# once during a scan, and they are rebuilt from scratch every pass. Measured at
# 48 B for the object plus a 296 B __dict__; slots drops the dict entirely, for
# ~12 MB less churn per scan on this library.
@dataclass(slots=True)
class ArrFile:
    """One media file as the arr sees it. `file_id` is the durable identity."""
    arr_name: str
    file_id: int
    parent_id: int
    path: str
    title: str
    season: int | None
    episode: str | None
    size: int
    quality: str | None
    # The arrs return a mediaInfo block per file, and this was carrying all of
    # it: 39,000 nested dicts built on every scan, for a field NOTHING reads.
    # The intention was to reuse the arr's mediainfo instead of probing
    # ourselves; that was never wired up, and the probe path uses ffprobe
    # directly. Left in place as an explicit None so the shape is unchanged for
    # anything constructing an ArrFile, but no longer populated - see
    # _sonarr_files / _radarr_files.
    media_info: dict | None = field(default=None)


def _series_sig(s: dict) -> str:
    """A cheap fingerprint of one series' files, from the /series call.

    episodeFileCount catches adds and deletes; sizeOnDisk catches upgrades and
    re-encodes (including nuarr's own, which always change the byte count);
    path catches a series folder move or rename.

    What it deliberately does NOT catch is an EPISODE-level rename, which
    changes none of the three. That is why the skip list expires - see
    scanner.changed_series(). Renames also arrive over the webhook stream in
    real time, so the periodic full pass is a backstop, not the primary path.
    """
    st = s.get("statistics") or {}
    return (f"{st.get('episodeFileCount')}:{st.get('sizeOnDisk')}:"
            f"{s.get('path') or ''}")


class ArrClient:
    def __init__(self, cfg: ArrConfig, timeout: float = 60.0):
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.api,
            headers={"X-Api-Key": cfg.api_key or ""},
            timeout=timeout,
        )
        # Signatures from the caller's PREVIOUS fetch, {series_id: sig}. Any
        # series whose signature still matches is left out of the /episodefile
        # fan-out. Empty means "fetch everything", which is what every caller
        # except the scanner wants.
        #
        # The comparison has to happen in here, not in the caller: the current
        # signatures come from the /series call that this method makes, so the
        # caller cannot know what changed until the work has already started.
        self.prev_series_sig: dict[int, str] = {}
        self.last_series_sig: dict[int, str] = {}
        self.skipped_parents: set[int] = set()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params):
        r = await self._client.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, payload: dict):
        r = await self._client.post(path, json=payload)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _put(self, path: str, payload: dict):
        r = await self._client.put(path, json=payload)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _delete(self, path: str, **params):
        r = await self._client.delete(path, params=params or None)
        r.raise_for_status()
        return r.json() if r.content else {}

    # ------------------------------------------------- blocklist and search --
    # Everything below exists to answer one question for a single file: which
    # release produced it, and can that release be rejected and replaced?
    #
    # The chain is import -> grab, joined on downloadId, and it was verified
    # against this Sonarr before any of it was written: 766 of 766 imports for
    # a sample series traced back to their grab. The link from OUR row to the
    # arr's is data.fileId on the import record, which is the same episodeFile
    # id we already store in files.arr_file_id - so no path matching is needed
    # and renames cannot break it.

    async def _history_for_parent(self, parent_id: int, event_type: int) -> list[dict]:
        """Grab (1) or import (3) records for one series/movie.

        /history?episodeId= is NOT a working filter - it returned 0 records on
        v4.0.19 while the series endpoint returned 775 for the same show. The
        per-parent endpoints are the ones that actually filter, and they return
        a bare list rather than the paged {records:[...]} envelope.
        """
        if self.cfg.kind == "sonarr":
            data = await self._get("/history/series", seriesId=parent_id,
                                   eventType=event_type)
        else:
            data = await self._get("/history/movie", movieId=parent_id,
                                   eventType=event_type)
        return data if isinstance(data, list) else (data.get("records") or [])

    async def find_grab(self, parent_id: int, arr_file_id: int,
                        scene_name: str = "") -> dict | None:
        """The grab record whose download produced this file, or None.

        None is a COMMON answer, not a failure. Measured over a 20-file random
        sample of this library, only 4 resolved: most files carry no sceneName
        and appear in no surviving import record, because they predate the
        history or were imported by hand. Callers must treat "no grab" as the
        normal case and degrade to delete-and-search.

        data.fileId is the primary key but is NOT durable - the arr issues a
        new episodeFile id whenever a file is re-imported, which orphans the
        link. The 270 files re-imported during the DV rename are exactly that
        population, so sceneName is kept as a fallback. Honesty about it: in
        the 20-file sample it matched 3 and added zero that fileId had missed.
        It is retained because it costs one string compare and targets a case
        the sample did not happen to contain, not because it has been shown to
        pay for itself.
        """
        imports = await self._history_for_parent(parent_id, 3)
        mine = next((h for h in imports
                     if str((h.get("data") or {}).get("fileId") or "")
                     == str(arr_file_id)), None)
        grabs = await self._history_for_parent(parent_id, 1)
        if mine and mine.get("downloadId"):
            hit = next((g for g in grabs
                        if g.get("downloadId") == mine["downloadId"]), None)
            if hit:
                return hit
        if scene_name:
            want = re.sub(r"[^a-z0-9]", "", scene_name.lower())
            return next((g for g in grabs
                         if re.sub(r"[^a-z0-9]", "",
                                   (g.get("sourceTitle") or "").lower()) == want), None)
        return None

    async def file_scene_name(self, arr_file_id: int) -> str:
        ep = "/episodefile/" if self.cfg.kind == "sonarr" else "/moviefile/"
        try:
            return (await self._get(f"{ep}{arr_file_id}")).get("sceneName") or ""
        except Exception:
            return ""

    async def mark_failed(self, history_id: int) -> None:
        """Blocklist the release and search for a replacement.

        This is the single call behind the arr UI's "Blocklist and Search": it
        records the download as failed, adds the release to the blocklist so
        the same one is not grabbed again, and queues a search.
        """
        await self._post(f"/history/failed/{history_id}", {})

    async def delete_file(self, arr_file_id: int) -> None:
        ep = "/episodefile/" if self.cfg.kind == "sonarr" else "/moviefile/"
        await self._delete(f"{ep}{arr_file_id}")

    async def search_for(self, parent_id: int, episode_ids: list[int] | None = None):
        if self.cfg.kind == "sonarr":
            if episode_ids:
                return await self._post("/command", {"name": "EpisodeSearch",
                                                     "episodeIds": episode_ids})
            return await self._post("/command", {"name": "SeriesSearch",
                                                 "seriesId": parent_id})
        return await self._post("/command", {"name": "MoviesSearch",
                                             "movieIds": [parent_id]})

    # ------------------------------------------------------------- health --
    async def ping(self) -> dict:
        try:
            st = await self._get("/system/status")
            return {"ok": True, "version": st.get("version"), "name": st.get("instanceName")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -------------------------------------------------------------- files --
    async def list_files(self) -> list[ArrFile]:
        """Every media file the arr knows about, with its durable id."""
        if self.cfg.kind == "sonarr":
            return await self._sonarr_files()
        return await self._radarr_files()

    async def _sonarr_files(self) -> list[ArrFile]:
        """One /episodefile call per series, run concurrently.

        Sonarr does NOT honour repeated seriesId params - verified on v4.0.19:
        `?seriesId=2&seriesId=3` returns exactly the same 142 rows as
        `?seriesId=2` alone, silently dropping the rest. Batching that way
        loses almost the whole library (1,418 of ~37,000 files), so we fan out
        per series with bounded concurrency instead.
        """
        import asyncio

        series = await self._get("/series")
        by_id = {s["id"]: s for s in series}
        out: list[ArrFile] = []

        # 12 is not arbitrary and should not be raised. Measured against this
        # Sonarr over a 120-series sample: 12 -> 3.42 s, 24 -> 3.70 s,
        # 48 -> 4.19 s, 96 -> 3.88 s. Sonarr serialises these internally, so
        # extra concurrency only adds queuing and contention. The fan-out is
        # ~31 s for 1,085 series and that is a Sonarr limit, not a client one -
        # which is why the win had to come from ASKING FOR LESS (see
        # changed_series() and the skip list below), not from asking harder.
        sem = asyncio.Semaphore(12)

        # Current signatures, computed from the /series response we already
        # have. Anything matching the caller's previous run is left alone.
        self.last_series_sig = {sid: _series_sig(s) for sid, s in by_id.items()}
        prev = self.prev_series_sig or {}
        skip = {sid for sid, sig in self.last_series_sig.items()
                if prev.get(sid) == sig}
        self.skipped_parents = skip

        async def one(sid: int):
            async with sem:
                try:
                    return sid, await self._get("/episodefile", seriesId=sid)
                except Exception:
                    return sid, []

        ids_all = [i for i in by_id.keys() if i not in skip]
        for chunk_start in range(0, len(ids_all), 200):
            ids = ids_all[chunk_start:chunk_start + 200]
            for sid, files in await asyncio.gather(*(one(i) for i in ids)):
                s = by_id.get(sid, {})
                title = s.get("title") or ""
                for f in files:
                    out.append(ArrFile(
                        arr_name=self.cfg.name,
                        file_id=f["id"],
                        parent_id=sid,
                        path=f.get("path") or "",
                        title=title,
                        season=f.get("seasonNumber"),
                        episode=_episodes_from_relpath(f.get("relativePath") or ""),
                        size=f.get("size") or 0,
                        quality=_quality_name(f),
                    ))
        return out

    async def _radarr_files(self) -> list[ArrFile]:
        r"""Every movie file Radarr knows about.

        The /movie list embeds movieFile ONLY when the movie's hasFile flag is
        set, and that flag goes stale. Waterworld showed as Downloaded in the
        Radarr UI with a real file record (id 29815), while /movie reported
        hasFile=False and no movieFile - so nuarr classified a managed file as
        unmanaged. 86 of 2,116 movies were in that state.

        /moviefile?movieId=... is the authoritative source, so any movie the
        list left empty gets a second, batched look before we believe it.
        """
        movies = await self._get("/movie")
        gap = [m["id"] for m in movies
               if isinstance(m, dict) and m.get("id")
               and not isinstance(m.get("movieFile"), dict)]
        recovered: dict[int, dict] = {}
        for i in range(0, len(gap), 50):          # keep the query string sane
            chunk = gap[i:i + 50]
            try:
                q = "&".join(f"movieId={m}" for m in chunk)
                for f in await self._get(f"/moviefile?{q}"):
                    if isinstance(f, dict) and f.get("movieId"):
                        recovered[f["movieId"]] = f
            except Exception:
                continue                          # a bad chunk must not sink the scan

        out: list[ArrFile] = []
        for m in movies:
            if not isinstance(m, dict):
                continue
            mf = m.get("movieFile")
            if not isinstance(mf, dict) or not mf.get("id"):
                mf = recovered.get(m.get("id"))   # the second look
            if not isinstance(mf, dict) or not mf.get("id"):
                continue
            out.append(ArrFile(
                arr_name=self.cfg.name,
                file_id=mf["id"],
                parent_id=m["id"],
                path=mf.get("path") or "",
                title=m.get("title") or "",
                season=None,
                episode=None,
                size=mf.get("size") or 0,
                quality=_quality_name(mf),
            ))
        return out

    # ------------------------------------------------------ coordination ---
    async def busy(self) -> tuple[bool, str]:
        """Is this arr doing something that will move files underneath us?

        Returns (busy, reason). We treat rename/import/refresh commands as
        blocking, because every one of them can change a path mid-job.
        """
        try:
            cmds = await self._get("/command")
        except Exception as e:
            return False, f"command query failed: {e}"
        blocking = {"RenameFiles", "RenameSeries", "RenameMovie", "RenameMovieFolder",
                    "DownloadedEpisodesScan", "DownloadedMoviesScan", "ManualImport"}
        active = [
            c for c in cmds
            if c.get("status") in ("queued", "started") and c.get("name") in blocking
        ]
        if active:
            names = ", ".join(sorted({c["name"] for c in active}))
            return True, f"{self.cfg.name}: {names} in progress"
        return False, ""

    async def notify_file_changed(self, parent_id: int) -> None:
        """Tell the arr a file changed so it re-reads mediainfo.

        RESCAN, not Refresh. The two are not the same job:

            Refresh* = fetch metadata from TVDB/TMDB over the network, THEN
                       rescan the folder.
            Rescan*  = read the folder from disk and update file records.

        We changed a file on disk; the show's metadata did not change. Refresh
        made every commit depend on an external API and did strictly more work
        than the situation calls for.

        Scope is as narrow as these APIs allow:
          * Radarr - RescanMovie covers one movie folder, which holds exactly
            the one file we touched. Effectively per-file.
          * Sonarr - RescanSeries is the narrowest available. There is NO
            per-episode-file rescan command in the v3 API, so a series-level
            rescan is the floor, and the per-parent debounce keeps it cheap.
        """
        cmd = "RescanSeries" if self.cfg.kind == "sonarr" else "RescanMovie"
        key = "seriesId" if self.cfg.kind == "sonarr" else "movieId"
        await self._post("/command", {"name": cmd, key: parent_id})

    async def rename_files(self, parent_id: int, file_ids: list[int]) -> dict:
        """Ask the arr to apply ITS naming policy to specific files.

        Preferred over renaming ourselves: the arr owns naming, knows the
        format, and updates its own DB - so the durable id survives and our
        record simply picks up the new path on the next scan.

        Returns the command object so the caller can WAIT for it. Fire-and-
        forget is exactly how the old setup ended up with half-renamed files
        like '...-JosekiTurn A Gundam.mkv'.
        """
        key = "seriesId" if self.cfg.kind == "sonarr" else "movieId"
        return await self._post("/command", {"name": "RenameFiles", key: parent_id,
                                             "files": file_ids})

    async def rename_preview(self, parent_id: int) -> list[dict]:
        """What the arr WOULD rename for this series/movie.

        Paths in the response are RELATIVE to the series/movie folder - verified
        live against Sonarr v4 and Radarr v6. Callers must join them onto the
        parent's `path` before touching disk.
        """
        key = "seriesId" if self.cfg.kind == "sonarr" else "movieId"
        try:
            return await self._get("/rename", **{key: parent_id})
        except Exception:
            return []

    async def parent_title(self, parent_id: int) -> str | None:
        ep = "/series/" if self.cfg.kind == "sonarr" else "/movie/"
        try:
            return (await self._get(f"{ep}{parent_id}")).get("title")
        except Exception:
            return None

    async def parent_path(self, parent_id: int) -> str | None:
        ep = "/series/" if self.cfg.kind == "sonarr" else "/movie/"
        try:
            return (await self._get(f"{ep}{parent_id}")).get("path")
        except Exception:
            return None

    async def file_record(self, file_id: int) -> dict | None:
        """Current arr record for a file - used to confirm the path really moved."""
        ep = "/episodefile/" if self.cfg.kind == "sonarr" else "/moviefile/"
        try:
            return await self._get(f"{ep}{file_id}")
        except Exception:
            return None

    async def command_status(self, command_id: int) -> dict:
        try:
            return await self._get(f"/command/{command_id}")
        except Exception as e:
            return {"status": "unknown", "error": str(e)}

    async def wait_command(self, command_id: int, timeout_s: float = 600,
                           poll_s: float = 2.0) -> tuple[bool, str]:
        """Block until an arr command finishes. Returns (succeeded, status)."""
        import asyncio
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = await self.command_status(command_id)
            status = st.get("status", "unknown")
            if status in ("completed", "failed", "aborted", "cancelled"):
                return status == "completed", status
            await asyncio.sleep(poll_s)
        return False, "timeout"


async def gather_arr_files(
    arrs: list[ArrConfig],
    prev_sigs: dict[str, dict] | None = None,
) -> tuple[list[ArrFile], dict[str, dict]]:
    """Pull files from every enabled arr, plus a status map for the dashboard.

    `prev_sigs` maps an arr name to the {series_id: signature} map from the
    caller's last fetch. Series whose signature is unchanged are left out of
    the per-series fan-out, which is essentially the entire cost of this call.

    THE CALLER MUST FILL THE GAP. A skipped series' files are ABSENT from the
    returned list, and reconcile treats an absent arr file as deleted - so a
    caller that skips without restoring those records from its own store will
    mark thousands of healthy files missing. See scanner._reconstruct_skipped().
    """
    files: list[ArrFile] = []
    status: dict[str, dict] = {}
    for cfg in arrs:
        if not cfg.enabled or not cfg.api_key:
            continue
        c = ArrClient(cfg)
        try:
            st = await c.ping()
            status[cfg.name] = st
            if st.get("ok"):
                t0 = time.time()
                c.prev_series_sig = (prev_sigs or {}).get(cfg.name) or {}
                got = await c.list_files()
                files.extend(got)
                status[cfg.name]["files"] = len(got)
                status[cfg.name]["seconds"] = round(time.time() - t0, 1)
                status[cfg.name]["skipped_parents"] = sorted(c.skipped_parents)
                status[cfg.name]["series_sig"] = c.last_series_sig
        finally:
            await c.close()
    return files, status


# One long-lived client per arr, instead of one per call.
#
# httpx.AsyncClient() is not a cheap object: its constructor builds an SSL
# context, which loads the system certificate store SYNCHRONOUSLY. Fine once,
# expensive on a hot path - and check_arrs() built two of them (Sonarr and
# Radarr) every time the gate was evaluated, which is every dashboard poll plus
# every jobs.pump and jobs._kick. py-spy caught the event loop parked in
# ArrClient.__init__ in four of six samples.
#
# Blocking the loop is the real cost. While that constructor runs uvicorn
# cannot service anything, so an endpoint that does no work at all still waits.
# Reusing the client is also what httpx documents: connection pooling only
# helps if the pool outlives the request.
#
# THIS BELONGS AT MODULE LEVEL, AFTER THE CLASS. Defined immediately below
# close() it read as module scope but ENDED THE CLASS BODY, so every method
# after it - _get, _post, ping, busy - became a module function. The symptom
# was "'ArrClient' object has no attribute '_get'": both arrs unreachable and
# every rename retry failing. Keep new module-level helpers down here.
#
# Not closed deliberately - the process owns these for its lifetime.
_SHARED: dict[tuple, "ArrClient"] = {}


def shared_client(cfg) -> "ArrClient":
    key = (getattr(cfg, "name", ""), cfg.api, cfg.api_key)
    c = _SHARED.get(key)
    if c is None or c._client.is_closed:
        c = ArrClient(cfg)
        _SHARED[key] = c
    return c
