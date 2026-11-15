"""The check of a customer's staged file (#6133, spec 2026-11-13 §7, §7.1). It runs only in
the intake worker's child process (`child.py`), never in a process that holds a database
secret (#5295, #5296).

The file is judged by its content, never by its name or declared type:

- a 3MF starts ``PK\\x03\\x04``; a binary STL is ``84 + 50 * n`` bytes for the uint32 ``n``
  at offset 80; otherwise printable ASCII starting ``solid`` is an ASCII STL. The name's
  extension must agree.
- a 3MF's zip is read central directory first, before anything is inflated: at most
  `MAX_ENTRIES` entries, each declaring at most `MAX_ENTRY_BYTES` inflated and all at most
  `MAX_TOTAL_BYTES`, an entry over `RATIO_FLOOR` at most `MAX_RATIO` times its compressed
  size, stored or deflate only, nothing encrypted, no name that is absolute or holds
  ``..``, a backslash or a NUL, and ``[Content_Types].xml``, ``_rels/.rels`` and a model
  under ``3D/`` present. The read itself inflates through `zipfile`, whose reader stops
  at each entry's declared size (`ZipExtFile`), so no entry inflates past what the
  directory declared (`test_check.py` measures it).
- the model is read within the default read budget (`render/read_budget.py`), plate by
  plate (`render/objects3mf.py` `read_plate_parts`); an STL is checked against the
  triangle budget before it loads (`check_stl`) and read as a one-plate 3MF.

`check` answers the facts and an iso preview of plate 1 (`render/thumbnail.py`
`render_view`), or raises `Refused` with a reason for the shop.
"""

from __future__ import annotations

import hashlib
import io
import string
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

import numpy as np

from scadbuddy.intake_worker.wire import MAX_PLATES, FileFacts, PlateFacts
from scadbuddy.render.bambu3mf import stl_3mf
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.render.objects3mf import UnreadableObjectsError, check_stl, read_plate_parts
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_view

Kind = Literal["3mf", "stl"]

#: §7.1's limits: four times or more the largest of the measured MakerWorld fixtures on
#: each axis, while a zip bomb's ratio is in the hundreds to millions.
MAX_ENTRIES: Final = 512
MAX_ENTRY_BYTES: Final = 1 << 30
MAX_TOTAL_BYTES: Final = 2 << 30
RATIO_FLOOR: Final = 1 << 20
MAX_RATIO: Final = 200
PREVIEW_SIZE: Final = 512

_ZIP_MAGIC: Final = b"PK\x03\x04"
_STL_HEADER: Final = 84
_STL_TRIANGLE: Final = 50
#: How much of an ASCII STL's start is read to tell it from anything else.
_ASCII_PROBE: Final = 512
_PRINTABLE: Final = frozenset(string.printable.encode())
_METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_SUFFIXES: Final[dict[Kind, str]] = {"3mf": ".3mf", "stl": ".stl"}


class Refused(Exception):  # noqa: N818  (a verdict on the file, not an error)
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Checked:
    facts: FileFacts
    preview: bytes


def sniff(path: Path, filename: str) -> Kind:
    """The file's kind by its content; refused when its name's extension disagrees."""
    size = path.stat().st_size
    with path.open("rb") as file:
        head = file.read(max(_STL_HEADER, _ASCII_PROBE))
    kind: Kind
    if head.startswith(_ZIP_MAGIC):
        kind = "3mf"
    elif (
        size >= _STL_HEADER
        and size == _STL_HEADER + _STL_TRIANGLE * int.from_bytes(head[80:84], "little")
    ) or (head.lstrip().startswith(b"solid") and all(byte in _PRINTABLE for byte in head)):
        kind = "stl"
    else:
        raise Refused("The file is not a 3MF or an STL.")
    if PurePosixPath(filename.lower()).suffix != _SUFFIXES[kind]:
        raise Refused(
            f"The file is a {kind.upper()}, but its name does not end in {_SUFFIXES[kind]}."
        )
    return kind


def _bad_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return name.startswith("/") or "\\" in name or "\x00" in name or ".." in parts


def check_zip(path: Path) -> None:
    """§7's central-directory checks, before any entry is inflated."""
    try:
        with zipfile.ZipFile(path) as zipped:
            entries = zipped.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise Refused("The 3MF is not a readable zip archive.") from None
    if len(entries) > MAX_ENTRIES:
        raise Refused(f"The 3MF holds {len(entries):,} files, more than {MAX_ENTRIES}.")
    total = 0
    names = set()
    for entry in entries:
        if _bad_name(entry.filename):
            raise Refused("The 3MF holds a file with an unsafe name.")
        if entry.flag_bits & 0x1:
            raise Refused("The 3MF holds an encrypted file.")
        if entry.compress_type not in _METHODS:
            raise Refused("The 3MF uses a compression other than deflate.")
        if entry.file_size > MAX_ENTRY_BYTES:
            raise Refused("The 3MF holds a file that inflates past 1 GiB.")
        if entry.file_size > RATIO_FLOOR and entry.file_size > MAX_RATIO * entry.compress_size:
            raise Refused("The 3MF holds a file compressed past what a model can be.")
        total += entry.file_size
        names.add(entry.filename)
    if total > MAX_TOTAL_BYTES:
        raise Refused("The 3MF inflates past 2 GiB in all.")
    if "[Content_Types].xml" not in names or "_rels/.rels" not in names:
        raise Refused("The 3MF lacks its content types or its relationships.")
    if not any(
        name.lower().startswith("3d/") and name.lower().endswith(".model") for name in names
    ):
        raise Refused("The 3MF holds no model.")


def _plate_facts(index: int, parts: Sequence[ColourPart]) -> PlateFacts:
    bounds = np.array([part.mesh.bounds for part in parts])
    low = bounds[:, 0, :].min(axis=0)
    high = bounds[:, 1, :].max(axis=0)
    size = tuple(round(float(axis), 3) for axis in high - low)
    return PlateFacts(
        index=index,
        triangles=sum(len(part.mesh.faces) for part in parts),
        size_mm=(size[0], size[1], size[2]),
        colours=list(dict.fromkeys(part.colour for part in parts)),
    )


def _read(payload: bytes | io.BufferedReader, kind: Kind, budget: ReadBudget) -> Checked:
    first = read_plate_parts(payload, 1, budget)
    if first.plates > MAX_PLATES:
        raise Refused(f"The file lays out {first.plates} plates, more than {MAX_PLATES}.")
    plates = [_plate_facts(1, first.parts)]
    for index in range(2, first.plates + 1):
        if not isinstance(payload, bytes):
            payload.seek(0)
        plates.append(_plate_facts(index, read_plate_parts(payload, index, budget).parts))
    facts = FileFacts(
        format=kind, plates=plates, triangles=sum(plate.triangles for plate in plates)
    )
    return Checked(facts, render_view(first.parts, "iso", PREVIEW_SIZE))


def check(path: Path, filename: str, budget: ReadBudget | None = None) -> Checked:
    """The file's facts and preview, or `Refused`."""
    spend = budget or ReadBudget()
    kind = sniff(path, filename)
    try:
        if kind == "3mf":
            check_zip(path)
            with path.open("rb") as file:
                return _read(file, kind, spend)
        with path.open("rb") as file:
            check_stl(file, spend)
        data = path.read_bytes()
        try:
            converted = stl_3mf(data, model_name=Path(filename).stem or "model")
        except ValueError:
            raise Refused("The STL holds no triangles.") from None
        return _read(converted, kind, spend)
    except (UnreadableObjectsError, NoSuchPlateError, zipfile.BadZipFile) as error:
        raise Refused(f"The file cannot be read: {error}"[:500]) from None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()
