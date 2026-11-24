"""Reads of MediaMTX's fragmented MP4 segments."""

from __future__ import annotations

import hashlib
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

#: MediaMTX's ``recordPath`` name, ``%Y-%m-%d_%H-%M-%S-%f``, in UTC (the recorder runs with TZ=UTC).
SEGMENT_NAME = "%Y-%m-%d_%H-%M-%S-%f"


def _box(handle: BinaryIO, offset: int) -> tuple[bytes, int]:
    handle.seek(offset)
    head = handle.read(8)
    if len(head) < 8:
        raise ValueError("the file ends inside a box header")
    size, kind = struct.unpack(">I4s", head)
    if size == 1:
        wide = handle.read(8)
        if len(wide) < 8:
            raise ValueError("the file ends inside a box header")
        (size,) = struct.unpack(">Q", wide)
    elif size == 0:
        raise ValueError("a box that runs to the end of the file has no init segment")
    if size < 8:
        raise ValueError("a box is smaller than its header")
    return kind, size


def init_span(path: Path) -> tuple[int, str]:
    """The byte length of the leading ``ftyp`` and ``moov`` boxes, and the sha256 of the
    codec setup they hold: ``ftyp`` and each child of ``moov`` but ``udta``.

    MediaMTX writes a ``moov/udta/mtxi`` box in each segment's init, with the stream id and
    the segment's number, DTS and NTP time (measured 2026-11-23 on v1.21.4, segments of one
    ffmpeg stream). With it, no two segments share an init digest, and each one would start
    a discontinuity."""
    with path.open("rb") as handle:
        end = 0
        spans: list[tuple[int, int]] = []
        for expected in (b"ftyp", b"moov"):
            kind, size = _box(handle, end)
            if kind != expected:
                raise ValueError(f"expected a {expected.decode()} box, found {kind!r}")
            spans.append((end, size))
            end += size
        (_, ftyp), (moov, moov_size) = spans
        kept = [(0, ftyp)]
        child = moov + 8
        while child < moov + moov_size:
            kind, size = _box(handle, child)
            if child + size > moov + moov_size:
                raise ValueError("a box runs past the end of moov")
            if kind != b"udta":
                kept.append((child, size))
            child += size
        handle.seek(0)
        data = handle.read(end)
    if len(data) != end:
        raise ValueError("the file ends inside the init segment")
    digest = hashlib.sha256()
    for at, size in kept:
        digest.update(data[at : at + size])
    return end, digest.hexdigest()


def segment_start(path: Path) -> datetime:
    """The segment's start, from its file name, as UTC."""
    return datetime.strptime(path.stem, SEGMENT_NAME).replace(tzinfo=UTC)
