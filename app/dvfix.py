r"""
nuarr - finish the Dolby Vision strip that ffmpeg cannot finish

THE PROBLEM THIS EXISTS FOR
---------------------------
Dolby Vision is signalled in TWO places in an MKV, and removing it from one
does not remove it from the other:

    1. the BITSTREAM - RPU packets carried as HEVC NAL type 62
    2. the CONTAINER - a dvvC/dvcC record in the track's BlockAdditionMapping

nuarr's passthrough plan strips DV with an ffmpeg bitstream filter:

    -bsf:v filter_units=remove_types=62

That works. Measured on an untouched Profile 8 file: 722 RPU NALs before, zero
after. But a bitstream filter operates on the bitstream, which is the clue in
the name - the container record is copied through untouched, because ffmpeg's
Matroska muxer writes it from the stream's side data.

The result was the worst of both outcomes. 388 files had their DV layer removed
- irreversibly, short of re-downloading - while still advertising
`dv_profile=8, rpu_present_flag=1`. Plex reads the container, so it kept making
Dolby Vision decisions about files with no Dolby Vision in them, and the strip
achieved nothing it was for. Verified with ffprobe on the output of nuarr's own
command: RPU gone, dv_profile still 8.

HOW THIS FIXES IT
-----------------
EBML - Matroska's container format - defines a Void element (ID 0xEC) whose
entire purpose is "ignore these bytes". Any element can be neutralised by
overwriting it in place with a Void of exactly the same total length. The file
length does not change and nothing after the patched bytes moves, so this is a
~70 byte write to the header rather than a rewrite of a 5-19 GB file.

That matters twice over: on a fresh job it saves nothing much (we have just
written the file anyway), but it makes the SAME code usable to repair files
already on the pool without moving 2.95 TB through the commit path.

THE CRC
-------
The Tracks master element carries a CRC-32, and voiding a child without fixing
it leaves a file strict parsers reject. Matroska's CRC-32 covers the master
element's data AFTER the CRC-32 element itself, standard IEEE CRC-32, stored
little-endian.

This module recomputes it - but only after proving it can reproduce the
checksum ALREADY in the file. If it cannot, it does not understand the file's
layout and refuses to touch it rather than guessing. That check is the whole
safety story: a tool that can predict the existing CRC understands the element
boundaries it is about to edit.

WHAT IT REFUSES TO DO
---------------------
  * touch a file whose bitstream still HAS an RPU - removing the advertisement
    while real DV data is present is the opposite mistake, and would turn a
    working DV file into a broken one
  * touch a file whose existing CRC it cannot reproduce
  * touch anything that is not an MKV with a DV record

All three return a reason rather than raising, because this runs inside the
job pipeline and a file we decline to patch is not a failed job - it is a file
that did not need patching.
"""
from __future__ import annotations

import os
import zlib

# The Tracks header lives well inside the first MiB on every file produced by
# ffmpeg or mkvmerge. Reading more would be pointless I/O on a 19 GB file.
HEAD = 1 << 20

ID_SEGMENT = 0x18538067
ID_TRACKS = 0x1654AE6B
ID_TRACK_ENTRY = 0xAE
ID_BLOCK_ADD_MAPPING = 0x41E4
ID_BLOCK_ADD_ID_TYPE = 0x41E7
ID_CRC32 = 0xBF
ID_VOID = 0xEC


# ------------------------------------------------------------------ EBML ---
def _read_id(b: bytes, p: int) -> tuple[int, int]:
    """EBML element ID at p -> (id, bytes_consumed).

    The count of leading zero bits in the first byte gives the length; unlike
    a data size, the marker bit is KEPT as part of the id value.
    """
    first = b[p]
    n, mask = 1, 0x80
    while not (first & mask):
        mask >>= 1
        n += 1
        if n > 4:
            raise ValueError("bad EBML id")
    v = 0
    for i in range(n):
        v = (v << 8) | b[p + i]
    return v, n


def _read_size(b: bytes, p: int) -> tuple[int, int]:
    """EBML data size at p -> (value, bytes_consumed). Marker bit cleared."""
    first = b[p]
    if first == 0:
        raise ValueError("bad EBML size")
    n, mask = 1, 0x80
    while not (first & mask):
        mask >>= 1
        n += 1
        if n > 8:
            raise ValueError("EBML size too long")
    v = first & (mask - 1)
    for i in range(1, n):
        v = (v << 8) | b[p + i]
    return v, n


def _children(b: bytes, start: int, end: int):
    """Iterate direct children: (id, offset, total_len, body_off, body_len).

    An element MUST still be yielded when its declared size runs past the end
    of the buffer we read - that is the normal case for the Segment, whose size
    is the whole 60 GB file while we hold only the first MiB. An earlier
    version returned instead, so the Segment was never yielded, Tracks was
    never found, and every large remux reported "not an MKV". Clamping is the
    caller's job; this only reports what is there.
    """
    p = start
    while p < end:
        try:
            eid, idl = _read_id(b, p)
            size, szl = _read_size(b, p + idl)
        except (ValueError, IndexError):
            return
        body = p + idl + szl
        yield eid, p, idl + szl + size, body, size
        nxt = body + size
        if nxt <= p:                  # zero-length element: would loop forever
            return
        p = nxt


def _find_tracks(b: bytes) -> tuple[int, int] | None:
    for eid, at, total, body, size in _children(b, 0, len(b)):
        if eid != ID_SEGMENT:
            continue
        # A Segment routinely declares a size larger than the bytes we read,
        # and may declare an unknown size; clamp to what we have.
        end = len(b) if body + size > len(b) else body + size
        for cid, cat, ctot, cbody, csize in _children(b, body, end):
            if cid == ID_TRACKS:
                return cbody, csize
    return None


def _find_dv_mapping(b: bytes, t_body: int, t_len: int) -> tuple[int, int] | None:
    """(offset, total_len) of the DV BlockAdditionMapping inside Tracks.

    Walked, not byte-searched. An earlier version scanned for the two-byte
    element id and matched constantly inside binary CodecPrivate data.
    """
    for cid, cat, ctot, cbody, csize in _children(b, t_body, t_body + t_len):
        if cid != ID_TRACK_ENTRY:
            continue
        for gid, gat, gtot, gbody, gsize in _children(b, cbody, cbody + csize):
            if gid != ID_BLOCK_ADD_MAPPING:
                continue
            payload = b[gbody:gbody + gsize]
            if (b"dvvC" in payload or b"dvcC" in payload
                    or ID_BLOCK_ADD_ID_TYPE.to_bytes(2, "big") in payload):
                return gat, gtot
    return None


def _crc_slot(b: bytes, t_body: int, t_len: int) -> tuple[int, int, int] | None:
    """(crc_value_offset, covered_start, covered_end) for the Tracks CRC-32.

    Per the spec CRC-32 is the FIRST child of the master element it protects,
    so if the first child is not one, there is no CRC to maintain.
    """
    for cid, cat, ctot, cbody, csize in _children(b, t_body, t_body + t_len):
        if cid == ID_CRC32:
            return cbody, cat + ctot, t_body + t_len
        return None
    return None


def _make_void(total: int) -> bytes:
    """A Void element occupying exactly `total` bytes."""
    body = total - 2
    if 0 <= body <= 0x7E:                     # 1-byte id + 1-byte size
        return bytes([ID_VOID, 0x80 | body]) + b"\x00" * body
    body = total - 5                          # 1-byte id + 4-byte size
    if body < 0:
        raise ValueError(f"element too small to void: {total}")
    return (bytes([ID_VOID]) + (0x10000000 | body).to_bytes(4, "big")
            + b"\x00" * body)


# ------------------------------------------------------------------ api ----
def inspect(path: str) -> dict:
    """What is here, without changing anything.

    Returns {found, offset, length, crc_ok, reason}. `found` False with a
    reason is the normal outcome for most files and is not an error.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD)
    except OSError as e:
        return {"found": False, "reason": f"cannot read: {e}"}

    tr = _find_tracks(head)
    if not tr:
        return {"found": False, "reason": "no Tracks element (not an MKV?)"}
    t_body, t_len = tr

    hit = _find_dv_mapping(head, t_body, t_len)
    if not hit:
        return {"found": False, "reason": "no Dolby Vision record"}
    at, total = hit

    out = {"found": True, "offset": at, "length": total,
           "crc_at": None, "crc_ok": None, "reason": ""}
    slot = _crc_slot(head, t_body, t_len)
    if slot:
        crc_at, cov_a, cov_b = slot
        stored = int.from_bytes(head[crc_at:crc_at + 4], "little")
        calc = zlib.crc32(head[cov_a:cov_b]) & 0xFFFFFFFF
        out["crc_at"] = crc_at
        out["crc_ok"] = (stored == calc)
        if stored != calc:
            out["reason"] = (f"existing CRC does not verify "
                             f"(stored {stored:08X}, calc {calc:08X})")
    return out


def strip_container_dv(path: str) -> tuple[bool, str]:
    """Void the DV record in place and fix the CRC. -> (changed, message).

    Does NOT check the bitstream. Callers on the job path know they have just
    run the RPU filter; callers repairing existing files must check first (see
    tools/dv_untag.py, which does).
    """
    info = inspect(path)
    if not info["found"]:
        return False, info["reason"]
    if info["crc_ok"] is False:
        return False, info["reason"]

    at, total = info["offset"], info["length"]
    try:
        with open(path, "rb") as f:
            head = bytearray(f.read(HEAD))
    except OSError as e:
        return False, f"cannot read: {e}"

    head[at:at + total] = _make_void(total)

    crc_bytes = None
    if info["crc_at"] is not None:
        tr = _find_tracks(bytes(head))
        if not tr:
            return False, "Tracks vanished after voiding (should be impossible)"
        slot = _crc_slot(bytes(head), *tr)
        if slot:
            crc_at, cov_a, cov_b = slot
            new = zlib.crc32(bytes(head[cov_a:cov_b])) & 0xFFFFFFFF
            crc_bytes = new.to_bytes(4, "little")
            head[crc_at:crc_at + 4] = crc_bytes

    try:
        with open(path, "r+b") as f:
            f.seek(at)
            f.write(bytes(head[at:at + total]))
            if crc_bytes is not None:
                f.seek(info["crc_at"])
                f.write(crc_bytes)
    except OSError as e:
        return False, f"write failed: {e}"

    # Re-read and confirm, rather than trusting the write.
    after = inspect(path)
    if after["found"]:
        return False, "record still present after patching"
    wrote = total + (4 if crc_bytes else 0)
    return True, (f"container Dolby Vision record removed "
                  f"({wrote} bytes rewritten in place"
                  + (", CRC-32 recomputed" if crc_bytes else "") + ")")


def has_rpu(path: str, ffmpeg: str, seconds: int = 20) -> bool | None:
    """Is real DV data still in the bitstream? (HEVC NAL type 62)

    None means "could not tell", which callers must treat as "do not touch".
    """
    import subprocess
    import tempfile
    from .config import NO_WINDOW

    tmp = os.path.join(tempfile.gettempdir(),
                       f"dvrpu_{os.getpid()}_{abs(hash(path)) % 99999}.hevc")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-t", str(seconds), "-i", path,
             "-map", "0:v:0", "-c:v", "copy", "-f", "hevc", tmp],
            capture_output=True, timeout=900, creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(tmp):
            return None
        data = open(tmp, "rb").read()
        i = 0
        while True:
            j = data.find(b"\x00\x00\x01", i)
            if j < 0 or j + 4 >= len(data):
                return False
            if ((data[j + 3] >> 1) & 0x3F) == 62:
                return True
            i = j + 3
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
