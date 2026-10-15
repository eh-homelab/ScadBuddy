"""Copying a zip entry as its compressed bytes stand, without inflating and deflating it.

A 3MF is mostly mesh objects that a rewrite of its settings or build items never
touches. `zipfile` has no public way to move such an entry from one archive to
another, and reading and writing it again costs seconds of CPU on a large file: 3.5 s
to stamp a 14 MB arranged 3MF in the API pod (#2755), 5.6 s of an 8.5 s re-placement
of an 86 MB-inflated one (#2942).
"""

from __future__ import annotations

import struct
import sys
import zipfile
from typing import BinaryIO

#: A local file header's fixed part: signature to extra-field length (APPNOTE 4.3.7).
_LOCAL_HEADER = struct.Struct("<4s2B4HL2L2H")
_LOCAL_SIGNATURE = b"PK\x03\x04"
#: General purpose flag bit 0: the entry is encrypted, so its bytes are not copied as is.
ENCRYPTED = 0x1

RAW_COPY_CHECKED = (3, 12)
"""The CPython minor whose `zipfile` internals :func:`copy_raw` was checked against
(#2910). On any other, callers read and write each entry again instead, and
``test_the_raw_copy_is_checked_on_this_python`` fails until it is checked again."""


def raw_copy_checked() -> bool:
    return sys.version_info[:2] == RAW_COPY_CHECKED


def copy_raw(
    source: BinaryIO,
    info: zipfile.ZipInfo,
    target: zipfile.ZipFile,
    *,
    date_time: tuple[int, int, int, int, int, int],
) -> None:
    """Append ``info``'s entry of ``source`` to ``target`` as its compressed bytes stand,
    dated ``date_time``.

    The entry keeps its compression, CRC and sizes, so a reader verifies it exactly as
    before. It writes through the `ZipFile`'s own file and bookkeeping, as `writestr`
    does: private state, which is why it runs only on :data:`RAW_COPY_CHECKED` (#2910)."""
    source.seek(info.header_offset)
    header = _LOCAL_HEADER.unpack(source.read(_LOCAL_HEADER.size))
    if header[0] != _LOCAL_SIGNATURE:
        raise zipfile.BadZipFile(f"no local header for {info.filename}")
    source.seek(info.header_offset + _LOCAL_HEADER.size + header[-2] + header[-1])
    data = source.read(info.compress_size)
    entry = zipfile.ZipInfo(info.filename, date_time=date_time)
    entry.compress_type = info.compress_type
    entry.CRC = info.CRC
    entry.compress_size = info.compress_size
    entry.file_size = info.file_size
    # What `ZipFile.open(mode="w")` gives an entry that states none, so a copied entry
    # reads back exactly as one written again would.
    entry.external_attr = 0o600 << 16
    fp = target.fp
    assert fp is not None
    entry.header_offset = fp.tell()
    fp.write(entry.FileHeader())
    fp.write(data)
    target.filelist.append(entry)
    target.NameToInfo[entry.filename] = entry
    target.start_dir = fp.tell()
    target._didModify = True  # type: ignore[attr-defined]  # what writestr sets
