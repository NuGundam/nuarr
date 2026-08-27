r"""
nuarr - job logging

Tdarr's logs were the one part of it worth copying: a per-job transcript you can
open and read. The parts worth improving:

  * they vanish when the job list is reset, so the record of WHY something
    failed disappears exactly when you want it
  * you cannot see a running job's log until it finishes
  * plugin decisions and ffmpeg output live in different places

So: every job gets a log that is written to disk immediately (survives a
restart), streamed live over HTTP while running, and holds the plan, the exact
ffmpeg command, and the raw output together in one file.

Layout:
    C:\ProgramData\nuarr\logs\jobs\<job_id>.log     one per job
    C:\ProgramData\nuarr\logs\nuarr.log             everything, rolling
"""
from __future__ import annotations

import heapq
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
JOB_LOG_DIR = LOG_DIR / "jobs"
JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)

MAIN_LOG = LOG_DIR / "nuarr.log"

LEVELS = ("debug", "info", "warn", "error", "ok")
_lock = threading.Lock()

# Live tail buffers, so the UI can follow a running job without reading the file
# on every poll.
_LIVE: dict[str, deque] = {}
_LIVE_MAX = 400

# Global recent lines for the "all activity" view
_RECENT: deque = deque(maxlen=600)


@dataclass
class LogLine:
    at: float
    level: str
    text: str
    job_id: str | None = None

    def as_dict(self) -> dict:
        return {"at": self.at, "level": self.level, "text": self.text,
                "job_id": self.job_id}

    def format(self) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.at))
        return f"{ts}  {self.level.upper():<5} {self.text}"


# WRITES GO TO A QUEUE, NOT TO DISK ON THE CALLER'S THREAD.
#
# log() used to open, append to and close TWO files per line - the job log and
# the main log - with _lock held for both. Every caller paid for that: the
# twelve workers logging progress, and the event loop, which calls log() from
# several coroutines. py-spy repeatedly caught MainThread inside _write.
#
# The lock made it worse than plain slow I/O. A worker thread taking 300 ms to
# append to a log on a spindle that ffmpeg is saturating held _lock for those
# 300 ms, so the event loop's next log() call blocked on the lock as well as on
# the disk. That is a scheduler-level stall caused entirely by bookkeeping.
#
# Now: callers do in-memory work only and hand the line to a queue. One daemon
# thread drains it, GROUPS BY FILE, and opens each file once per batch instead
# of once per line - which also removes most of the syscalls under load.
#
# Dropping is deliberate when the queue is full: losing log lines is strictly
# better than stalling a transcode or the web server to record them.
_WRITEQ: "queue.Queue[tuple | None]" = queue.Queue(maxsize=20000)
DROPPED = 0


def _writer_loop() -> None:
    global DROPPED
    while True:
        item = _WRITEQ.get()
        if item is None:
            return
        batch = [item]
        # Opportunistically take whatever else is already waiting.
        for _ in range(500):
            try:
                nxt = _WRITEQ.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                _WRITEQ.put(None)
                break
            batch.append(nxt)
        grouped: dict = {}
        for path, line in batch:
            grouped.setdefault(path, []).append(line)
        for path, lines in grouped.items():
            try:
                with open(path, "a", encoding="utf-8", errors="replace") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError:
                pass


_writer = threading.Thread(target=_writer_loop, name="joblog-writer", daemon=True)
_writer.start()


def _write(path, line: str) -> None:
    global DROPPED
    try:
        _WRITEQ.put_nowait((path, line))
    except queue.Full:
        DROPPED += 1


def flush(timeout: float = 5.0) -> None:
    """Block until queued lines are on disk. For shutdown and tests."""
    end = time.time() + timeout
    while not _WRITEQ.empty() and time.time() < end:
        time.sleep(0.02)


def log(text: str, level: str = "info", job_id: str | None = None,
        system: str | None = None) -> LogLine:
    """Record one line. Safe to call from worker threads.

    `system` names the BACKGROUND LOOP a line came from, so the log viewer can
    show "everything the audio language check has ever said" without grepping.
    It is almost never passed by hand: schedules.beat() marks the running loop
    and it is picked up automatically below, which is why this works on the
    seventeen existing loops without touching any of them.
    """
    if system is None and not job_id:
        try:
            from . import schedules
            system = schedules.current() or None
        except Exception:                                # noqa: BLE001
            system = None
    ln = LogLine(time.time(), level if level in LEVELS else "info", text, job_id)
    formatted = ln.format()
    with _lock:
        _RECENT.append(ln)
        if job_id:
            buf = _LIVE.setdefault(job_id, deque(maxlen=_LIVE_MAX))
            buf.append(ln)
            _write(JOB_LOG_DIR / f"{job_id}.log", formatted)
        # One suffix or the other, never both: a line belongs to a job OR to a
        # background loop, and the parser splits on whichever it finds.
        suffix = (f"  [job {job_id}]" if job_id
                  else f"  [sys {system}]" if system else "")
        _write(MAIN_LOG, formatted + suffix)
    return ln


def job_lines(job_id: str, since: float = 0.0, limit: int = 400) -> list[dict]:
    """Live tail for a job. Falls back to the file once the buffer is dropped."""
    with _lock:
        buf = _LIVE.get(job_id)
        if buf:
            return [l.as_dict() for l in buf if l.at > since][-limit:]
    p = JOB_LOG_DIR / f"{job_id}.log"
    if not p.exists():
        return []
    try:
        tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    # PARSE the level back out. Stamping every line "info" meant a FINISHED job's
    # log rendered in one flat colour - exactly the jobs you most want to read,
    # because the live buffer is dropped once a job ends.
    out = []
    for t in tail:
        r = _parse(t)
        r["job_id"] = job_id
        out.append(r)
    return out


_LINE_RE = None


def _parse(line: str) -> dict:
    """Turn a written log line back into a row.

    Needed because the in-memory ring buffer only holds what THIS process wrote.
    A CLI run, or anything before the last restart, exists only in the file - and
    a log viewer that forgets everything on restart is not a log viewer.
    """
    global _LINE_RE
    if _LINE_RE is None:
        import re
        # Fixed-width level field so the message keeps its own indentation -
        # a greedy \s+ swallowed the leading spaces that show handler output
        # nested under its job.
        _LINE_RE = re.compile(
            r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (\w+)\s{0,2}(.*)$")
    m = _LINE_RE.match(line)
    if not m:
        return {"at": 0, "level": "info", "text": line, "job_id": None}
    ts, lvl, text = m.group(1), m.group(2), m.group(3)
    try:
        at = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        at = 0
    job = None
    system = None
    if text.endswith("]") and "  [job " in text:
        text, _, tail = text.rpartition("  [job ")
        job = tail.rstrip("]")
    elif text.endswith("]") and "  [sys " in text:
        text, _, tail = text.rpartition("  [sys ")
        system = tail.rstrip("]")
    return {"at": at, "level": lvl.lower(), "text": text, "job_id": job,
            "system": system}


def _main_lines() -> list[str]:
    """The WHOLE main log. Expensive - prefer _tail_lines().

    Kept for the level-filter fallback only, where a caller genuinely needs to
    search history rather than read the end of it.
    """
    if not MAIN_LOG.exists():
        return []
    try:
        return MAIN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _tail_lines(n: int) -> list[str]:
    r"""The last `n` lines, read by seeking from the end.

    THE LOG VIEWER WAS READING THE ENTIRE 70 MB FILE EVERY THREE SECONDS.

    recent() called _main_lines(), which read the whole of nuarr.log into one
    string, split it into 585,218 lines, and then _parse()d every one of them -
    a regex match plus a time.strptime per line - sorted the result, and
    returned 50 rows. The dashboard polls it on a 3 second timer.

    Measured: 6,008 ms per call, 585,218 lines parsed to display 50. That is
    ~585k regex + strptime calls every three seconds, and each pass allocates a
    70 MB string, 585k line strings and 585k dicts - which is the churn that
    kept this process at an 800 MB floor with every one of its own caches
    empty.

    Reading backwards costs the last few KB instead. The file only grows at the
    end, so the newest lines - the only ones a log tail wants - are always
    there.
    """
    if n <= 0 or not MAIN_LOG.exists():
        return []
    try:
        size = MAIN_LOG.stat().st_size
    except OSError:
        return []
    data = b""
    block = 1 << 16
    try:
        with open(MAIN_LOG, "rb") as f:
            pos = size
            # One extra newline of slack: the first line in the window is
            # usually partial, and dropping it is cheaper than getting it wrong.
            while pos > 0 and data.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                block = min(block * 2, 1 << 22)
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if len(lines) > n:
        lines = lines[-n:]
    return lines


# Total line count, maintained incrementally. The viewer needs it to know when
# it has reached the end of history; counting newlines in the bytes APPENDED
# since last time costs nothing, while re-reading 70 MB to count them costs as
# much as the bug this replaced.
_COUNT: dict = {"size": 0, "lines": 0}

# Full-scan results for a sparse level filter, keyed level -> (file_size, rows).
# Invalidated by the file growing, which is the only way it changes.
_FILTER_CACHE: dict = {}


def _total_lines() -> int:
    try:
        size = MAIN_LOG.stat().st_size
    except OSError:
        return 0
    with _lock:
        prev_size, prev = _COUNT["size"], _COUNT["lines"]
    if size == prev_size:
        return prev
    if size < prev_size:          # rotated or truncated - start over
        prev_size, prev = 0, 0
    n = prev
    try:
        with open(MAIN_LOG, "rb") as f:
            f.seek(prev_size)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
    except OSError:
        return prev
    with _lock:
        _COUNT.update(size=size, lines=n)
    return n


def _tail_main(limit: int) -> list[dict]:
    return [_parse(l) for l in _main_lines()[-limit:]]


def recent(level: str | None = None, limit: int = 200,
           offset: int = 0) -> tuple[list[dict], int]:
    """A page of history, newest first.

    `offset` counts backwards from the newest line, so the UI can keep scrolling
    into older history instead of being stuck with the last screenful. The file
    is the source of truth here - the in-memory ring only covers this process, so
    on its own it would hide everything logged before the last restart.

    Returns (rows, total_available) so the viewer knows when it has hit the end.

    READS THE TAIL, NOT THE FILE. See _tail_lines() for what this used to cost.
    Only as many lines as the page actually needs are parsed. With a level
    filter the window is widened progressively, because the matching lines may
    be sparse - and only if that still comes up short does it fall back to
    reading everything, which keeps "show me every error" correct without
    making the common unfiltered poll pay for it.
    """
    def key(r):
        # Dedupe on WHOLE seconds and stripped text. The in-memory copy carries
        # a fractional timestamp and its original indentation, while the same
        # line read back from the file has second resolution and leading spaces
        # eaten by the parser - so an exact key never matched and every line
        # this process logged showed up twice.
        return (int(r["at"] or 0), (r["text"] or "").strip())

    with _lock:
        mem = [r.as_dict() for r in _RECENT]

    need = offset + limit
    # Margin for the dedupe against the in-memory ring and for a partial first
    # line. Without a filter the answer is always in the newest `need` lines.
    windows = [need + len(mem) + 64]
    if level:
        windows += [need * 40 + 2000, need * 400 + 20000]

    rows: list[dict] = []
    full = False
    for w in windows:
        lines = _tail_lines(w)
        if len(lines) < w:            # the whole file fits in this window
            full = True
        rows = [_parse(l) for l in lines]
        seen = {key(r) for r in rows}
        rows += [r for r in mem if key(r) not in seen]
        rows.sort(key=lambda r: r["at"])
        if level:
            rows = [r for r in rows if r["level"] == level]
        if len(rows) >= need or full:
            break
    else:
        if level and not full:        # sparse matches - read the lot, ONCE
            # CACHED BY LEVEL AND FILE SIZE. A rare level - 'error' on a healthy
            # server - is sparse enough that no reasonable window finds a
            # pageful, so this path reads all 585k lines. That is acceptable
            # once; it is not acceptable every 3 seconds because someone left
            # the filter set. Only the MATCHING rows are kept, which for the
            # levels that trigger this is a few hundred, not the whole file.
            try:
                size = MAIN_LOG.stat().st_size
            except OSError:
                size = -1
            with _lock:
                hit = _FILTER_CACHE.get(level)
            if hit and hit[0] == size:
                rows = list(hit[1])
            else:
                scanned = [_parse(l) for l in _main_lines()]
                scanned = [r for r in scanned if r["level"] == level]
                with _lock:
                    _FILTER_CACHE[level] = (size, scanned)
                    # Bounded: one entry per level, and there are five levels.
                    if len(_FILTER_CACHE) > 8:
                        _FILTER_CACHE.clear()
                        _FILTER_CACHE[level] = (size, scanned)
                rows = list(scanned)
            seen = {key(r) for r in rows}
            rows += [r for r in mem
                     if r["level"] == level and key(r) not in seen]
            rows.sort(key=lambda r: r["at"])
            full = True

    # `total` is what the viewer uses to decide whether more history exists.
    # Counted from the file incrementally rather than derived from the parsed
    # page, which now deliberately holds only a window of it.
    total = len(rows) if (full or level) else max(_total_lines(), len(rows))
    newest_first = rows[::-1]
    return newest_first[offset:offset + limit], total


def release(job_id: str) -> None:
    """Drop the in-memory buffer once a job is done; the file remains."""
    with _lock:
        _LIVE.pop(job_id, None)


def list_job_logs(limit: int = 100) -> list[dict]:
    r"""The most recent job logs. Reads the directory ONCE, keeps `limit`.

    This used to be:

        sorted(JOB_LOG_DIR.glob("*.log"),
               key=lambda x: x.stat().st_mtime, reverse=True)[:limit]

    which looks harmless and is three separate mistakes at this scale. There
    is one log file per job and 32,009 of them:

      * glob() built 32,009 WindowsPath objects - and a WindowsPath is not a
        string, it carries a segment list and several strings each. Measured on
        an idle, PAUSED server: 160,116 live WindowsPath objects and 164,046
        lists, with every one of nuarr's own caches empty. That was the biggest
        single thing in a 790 MB process.
      * the key function called .stat() on every one - 32,009 syscalls against
        ProgramData - to then discard all but 100.
      * sorted() ordered all 32,009 to take the top 100.

    scandir yields entries whose stat is already populated from the directory
    read, and nlargest keeps only `limit` in hand instead of ordering the lot.

    Measured, same directory, same result: 1,346 ms -> 69 ms, and 32,009 Path
    objects -> none.
    """
    out: list[dict] = []
    try:
        with os.scandir(JOB_LOG_DIR) as it:
            entries = [e for e in it if e.name.endswith(".log")]
        for e in heapq.nlargest(limit, entries, key=lambda x: x.stat().st_mtime):
            st = e.stat()
            out.append({"job_id": e.name[:-4], "bytes": st.st_size,
                        "modified": st.st_mtime})
    except OSError:
        pass
    return out


def read_job_log(job_id: str) -> str:
    p = JOB_LOG_DIR / f"{job_id}.log"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def prune(days: int = 30) -> int:
    """Delete job logs older than N days. Text only - never touches media."""
    cutoff = time.time() - days * 86400
    n = 0
    # scandir for the same reason as list_job_logs: 32,009 files is a lot of
    # Path objects to build in order to delete a handful of them.
    try:
        with os.scandir(JOB_LOG_DIR) as it:
            stale = [e.path for e in it
                     if e.name.endswith(".log") and e.stat().st_mtime < cutoff]
    except OSError:
        return 0
    for path in stale:
        try:
            os.remove(path)
            n += 1
        except OSError:
            pass
    return n


def banner(job_id: str, text: str, level: str = "info") -> None:
    """A hard visual boundary in the log.

    Without these, a log with four workers running is an interleaved stream with
    no way to see where one file's work begins and another's ends - especially
    in the raw text file, which has no colour to fall back on.
    """
    # Plain ASCII on purpose. A box-drawing character looks nicer in the browser
    # but renders as mojibake in a console or a non-UTF8 editor, and the raw
    # .log file has to stay readable wherever you open it.
    bar = "=" * 72
    log(bar, level, job_id)
    log(text, level, job_id)
    log(bar, level, job_id)


class section:
    r"""A start/end block for a SUBSYSTEM run, as opposed to one job.

    Job work already had banner(); everything else - the rename queue, commit
    retries, missing healing, the failure tidy - wrote loose lines into the same
    stream. With four workers logging simultaneously those lines scatter, and
    there is no way to see where one subsystem's pass began, what it did, or
    whether it finished. This gives each pass a labelled boundary:

        ---- rename queue -------------------------------------------------
          retried 3, 1 still pending
        ---- rename queue: 3 retried in 4.1s ------------------------------

    Silent by default when nothing happened. A pass that checks and finds no
    work should leave no trace, or the log fills with heartbeat noise and the
    banners stop being findable - which is the problem they exist to solve.

    Usage:
        with joblog.section("rename queue") as s:
            ...
            s.note("retried 3")          # a line inside the block
            s.result = "3 retried"       # summary on the closing bar
            s.keep()                     # force the block to be written
    """
    WIDTH = 72

    def __init__(self, name: str, level: str = "info"):
        self.name = name
        self.level = level
        self.result: str | None = None
        self._t0 = 0.0
        self._lines: list[tuple[str, str]] = []
        self._keep = False

    def note(self, text: str, level: str = "info") -> None:
        self._lines.append((text, level))
        self._keep = True

    def keep(self) -> None:
        self._keep = True

    def __enter__(self) -> "section":
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        took = time.time() - self._t0
        failed = exc_type is not None
        if not (self._keep or failed):
            return False                      # nothing happened; stay quiet
        head = f"---- {self.name} "
        log(head + "-" * max(0, self.WIDTH - len(head)), self.level)
        for text, lvl in self._lines:
            log("  " + text, lvl)
        if failed:
            log(f"  FAILED: {exc_type.__name__}: {exc}", "error")
        tail_txt = self.result or ("failed" if failed else "done")
        tail = f"---- {self.name}: {tail_txt} in {took:.1f}s "
        log(tail + "-" * max(0, self.WIDTH - len(tail)),
            "error" if failed else self.level)
        return False                          # never swallow the exception


def log_plan(job_id: str, plan) -> None:
    """Write the decision BEFORE any work starts.

    This is the bit Tdarr never showed clearly: not just what it did, but why it
    decided to. When an encode turns out wrong, this block is the evidence.
    """
    log(f"PLAN: {plan.summary()}", "info", job_id)
    if plan.skip_reason:
        log(f"  skipped: {plan.skip_reason}", "warn", job_id)
        return
    for a in plan.actions:
        log(f"  [{a.kind}] {a.what}", "info", job_id)
        log(f"        why: {a.why}", "debug", job_id)
        if a.detail:
            log(f"        detail: {a.detail}", "debug", job_id)
    # Notes explain things we deliberately did NOT do - just as important when
    # you are trying to work out why a file came out the way it did.
    for n in getattr(plan, "notes", []):
        log(f"  [note] {n}", "debug", job_id)
