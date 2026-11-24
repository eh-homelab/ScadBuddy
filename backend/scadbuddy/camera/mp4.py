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
    """The byte length of the leading ``ftyp`` and ``moov`` boxes, and the sha256 of those bytes."""
    with path.open("rb") as handle:
        end = 0
        for expected in (b"ftyp", b"moov"):
            kind, size = _box(handle, end)
            if kind != expected:
                raise ValueError(f"expected a {expected.decode()} box, found {kind!r}")
            end += size
        handle.seek(0)
        data = handle.read(end)
    if len(data) != end:
        raise ValueError("the file ends inside the init segment")
    return end, hashlib.sha256(data).hexdigest()


def segment_start(path: Path) -> datetime:
    """The segment's start, from its file name, as UTC."""
    return datetime.strptime(path.stem, SEGMENT_NAME).replace(tzinfo=UTC)
