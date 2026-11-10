"""Where an archive's 3MF can be, for "Import print" (#4698).

A print ScadBuddy did not start has no link to a model. A person imports it: they choose
the model and the place to take the print's 3MF from. This module finds every such
place, says why one cannot be used, and reads the one chosen. The route is
`api/print_imports.py`; the link is `print_links.PrintLinkStore.record_import`.

The places (Bambuddy at maziggy/bambuddy@f9080d1, prod's 1.2.6b1):

* ``archive``: the sliced file Bambuddy keeps with the archive (``GET /archives/{id}``,
  ``routes/archives.py`` line 1625; read with ``/archives/{id}/download``, line 2259). An
  archive Bambuddy made without its 3MF carries ``extra_data.no_3mf_available``.
* ``archive_source``: the slicer project 3MF attached to the archive
  (``source_3mf_path``; read with ``/archives/{id}/source``, line 5159).
* ``library``: a Bambuddy library file with the archive's file name, or its stem. One
  read of every file (``GET /library/files/?include_root=false``, ``routes/library.py``
  lines 2133-2158, which has no hash), then ``GET /library/files/{id}`` (line 5160) for
  each candidate's ``file_hash``. A file with the same hash and another name is not found.
* ``printer``: the printer's storage, ``/cache`` and ``/``, over FTP
  (``GET /printers/{id}/files``, ``routes/printers.py`` line 1604; read with
  ``/printers/{id}/files/download``, line 1632; scope Control Printer). Only when the
  caller asks: it reaches the printer.

A source chosen for the import must be one :func:`find_sources` lists, available: no free
library file id and no free printer path is ever read.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import zipfile
from collections.abc import Sequence
from contextlib import aclosing
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self

from fastapi import status
from pydantic import BaseModel, Field, model_validator

from scadbuddy.bambuddy.client import BambuddyClient, Operation
from scadbuddy.bambuddy.errors import Scope
from scadbuddy.bambuddy.models import ArchiveDetail, LibraryListRow
from scadbuddy.bambuddy.print_links import ImportSource
from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

#: Where the printer keeps a job's file: Bambu printers write a started job's file to
#: ``/cache`` (the issue's ``cache/<job>.3mf``); a file sent to the card sits at its root.
PRINTER_DIRECTORIES = ("/cache", "/")
#: The most library files with the archive's name whose hash is read.
MAX_LIBRARY_CANDIDATES = 10
#: How many of those reads run at once.
LIBRARY_READS = 4

SLICED_SUFFIX = ".gcode.3mf"
_SUFFIXES = (SLICED_SUFFIX, ".3mf")
#: A printer path the import reads: absolute, no ``.`` or ``..`` segment, a 3MF. A
#: directory segment starts with a character other than a dot, or with one dot and then
#: another character, or with two dots and then more (no lookahead: the pattern is also
#: the OpenAPI ``pattern``, checked by pydantic-core's Rust regex).
_PRINTER_SEGMENT = r"(?:[^/\\.][^/\\]*|\.[^/\\.][^/\\]*|\.\.[^/\\]+)"
# The flag is inline, so the field's ``pattern`` (pydantic, OpenAPI) ignores case too.
_PRINTER_PATH = re.compile(rf"(?i)^/(?:{_PRINTER_SEGMENT}/)*[^/\\]+\.3mf$")

NOT_CHECKED = (
    "not checked: reading the printer's storage reaches the printer, so it is read only "
    "when asked (printer=true), and the assistant needs a person's approval for it"
)


class SourceChoice(BaseModel):
    """The source a person chose: its ``kind``, and for a library file its id, for the
    printer its path, as :func:`find_sources` listed them."""

    kind: ImportSource
    library_file_id: int | None = Field(default=None, ge=1)
    path: str | None = Field(default=None, max_length=1024, pattern=_PRINTER_PATH.pattern)

    @model_validator(mode="after")
    def _one_reference(self) -> Self:
        if (self.kind == "library") != (self.library_file_id is not None):
            raise ValueError("a library source, and only it, names its library_file_id")
        if (self.kind == "printer") != (self.path is not None):
            raise ValueError("a printer source, and only it, names its path")
        return self


class FoundSource(BaseModel):
    """One place the print's 3MF can be."""

    kind: ImportSource
    #: The file's name there; None for a source not found.
    name: str | None = None
    size: int | None = None
    #: When the file was made or written there, as that place reports it.
    modified_at: datetime | None = None
    available: bool
    #: Why it cannot be used, when it cannot.
    reason: str | None = None
    #: A sliced file has G-code and no meshes: it cannot make a new model.
    sliced: bool = False
    #: The SHA-256 Bambuddy reports for the file, when it does.
    sha256: str | None = None
    library_file_id: int | None = None
    path: str | None = None
    #: How a library file was matched: by the archive's hash, or by name only.
    matched_by: Literal["hash", "name"] | None = None
    #: The only available source: the page picks it, and the person still confirms it.
    preselected: bool = False

    def is_choice(self, choice: SourceChoice) -> bool:
        return (
            self.kind == choice.kind
            and (choice.kind != "library" or self.library_file_id == choice.library_file_id)
            and (choice.kind != "printer" or self.path == choice.path)
        )


def _stem(name: str) -> str:
    lower = PurePosixPath(name).name.lower()
    for suffix in _SUFFIXES:
        if lower.endswith(suffix):
            return lower[: -len(suffix)]
    return lower


def _names(archive: ArchiveDetail) -> set[str]:
    """The stems a file of this print can have: its archived file's, its print name."""
    return {_stem(name) for name in (archive.filename, archive.print_name) if name}


def _sliced(name: str | None) -> bool:
    return (name or "").lower().endswith(SLICED_SUFFIX)


def _archive_sources(archive: ArchiveDetail) -> list[FoundSource]:
    extra = archive.extra_data
    if extra.no_3mf_available or not archive.filename:
        sliced = FoundSource(
            kind="archive",
            available=False,
            reason=extra.no_3mf_reason
            or "Bambuddy archived this print without its 3MF: the printer did not serve it",
        )
    else:
        sliced = FoundSource(
            kind="archive",
            name=archive.filename,
            size=archive.file_size,
            modified_at=archive.created_at,
            available=True,
            sliced=True,
            sha256=archive.content_hash,
        )
    if archive.source_3mf_path:
        project = FoundSource(
            kind="archive_source",
            name=PurePosixPath(archive.source_3mf_path).name,
            available=True,
            sliced=_sliced(archive.source_3mf_path),
        )
    else:
        project = FoundSource(
            kind="archive_source",
            available=False,
            reason="no slicer project 3MF is attached to the archive",
        )
    return [sliced, project]


async def _library_sources(client: BambuddyClient, archive: ArchiveDetail) -> list[FoundSource]:
    try:
        rows = await client.library_listing(folder_id=None, every_folder=True)
    except (ApiError, ValueError) as error:
        detail = error.detail if isinstance(error, ApiError) else "an answer ScadBuddy cannot read"
        return [
            FoundSource(
                kind="library", available=False, reason=f"the library could not be read: {detail}"
            )
        ]
    names = _names(archive)
    candidates: list[LibraryListRow] = sorted(
        (
            row
            for row in rows
            if row.file_type in ("3mf", "gcode.3mf") and _stem(row.filename) in names
        ),
        key=lambda row: (row.created_at is None, row.created_at),
        reverse=True,
    )[:MAX_LIBRARY_CANDIDATES]
    if not candidates:
        return [
            FoundSource(
                kind="library",
                available=False,
                reason="no Bambuddy library file has this print's name",
            )
        ]
    gate = asyncio.Semaphore(LIBRARY_READS)

    async def hashed(row: LibraryListRow) -> str | None:
        async with gate:
            try:
                return (await client.library_file(row.id)).file_hash
            except (ApiError, ValueError):
                return None

    hashes = await asyncio.gather(*(hashed(row) for row in candidates))
    found = [
        FoundSource(
            kind="library",
            name=row.filename,
            size=row.file_size,
            modified_at=row.created_at,
            available=True,
            sliced=row.file_type == "gcode.3mf",
            sha256=file_hash,
            library_file_id=row.id,
            matched_by=(
                "hash" if file_hash is not None and file_hash == archive.content_hash else "name"
            ),
        )
        for row, file_hash in zip(candidates, hashes, strict=True)
    ]
    # A file with the printed bytes first.
    return sorted(found, key=lambda source: source.matched_by != "hash")


async def _printer_sources(client: BambuddyClient, archive: ArchiveDetail) -> list[FoundSource]:
    printer_id = archive.printer_id
    if printer_id is None:
        return [FoundSource(kind="printer", available=False, reason="no printer is known for it")]
    names = _names(archive)
    found: list[FoundSource] = []
    problems: list[str] = []
    for directory in PRINTER_DIRECTORIES:
        try:
            listing = await client.printer_files(printer_id, directory)
        except ApiError as error:
            if error.status in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
                return [FoundSource(kind="printer", available=False, reason=error.detail)]
            problems.append(f"{directory} could not be read: {error.detail}")
            continue
        except ValueError:
            problems.append(f"{directory} could not be read")
            continue
        if "printer_unavailable" in listing.warnings:
            return [
                FoundSource(
                    kind="printer",
                    available=False,
                    reason="the printer is offline or did not answer",
                )
            ]
        found.extend(
            FoundSource(
                kind="printer",
                name=file.name,
                size=file.size,
                modified_at=file.mtime,
                available=True,
                sliced=_sliced(file.name),
                path=file.path,
            )
            for file in listing.files
            if not file.is_directory
            and file.name.lower().endswith(".3mf")
            and _stem(file.name) in names
            and _PRINTER_PATH.fullmatch(file.path)
        )
    if found:
        return found
    reason = "; ".join(problems) or (
        f"the printer has no file named {archive.filename or archive.print_name} "
        "in /cache or at its root"
    )
    return [FoundSource(kind="printer", available=False, reason=reason)]


async def find_sources(
    client: BambuddyClient, archive: ArchiveDetail, *, printer: bool
) -> list[FoundSource]:
    """Every place the print's 3MF can be. One that cannot be read says why and never
    stops the others. The printer is read only with ``printer``."""
    sources = _archive_sources(archive) + await _library_sources(client, archive)
    if printer:
        sources += await _printer_sources(client, archive)
    else:
        sources.append(FoundSource(kind="printer", available=False, reason=NOT_CHECKED))
    available = [source for source in sources if source.available]
    if len(available) == 1:
        available[0].preselected = True
    return sources


def chosen(sources: Sequence[FoundSource], choice: SourceChoice) -> FoundSource:
    """The listed source ``choice`` names: 422 when the listing has none, 409 when it
    cannot be used now."""
    for source in sources:
        if source.is_choice(choice):
            if not source.available:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    f"the {choice.kind} source cannot be used: {source.reason}",
                )
            return source
    raise ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"the {choice.kind} source named is not one this print's sources list",
    )


class SourceFile(BaseModel):
    name: str
    data: bytes
    sha256: str


async def _read(
    client: BambuddyClient,
    path: str,
    *,
    operation: Operation,
    what: str,
    params: dict[str, str] | None = None,
    scope: Scope = Scope.READ_STATUS,
) -> bytes:
    data = bytearray()
    async with client.stream(
        path, operation=operation, what=what, params=params, pass_416=False, scope=scope
    ) as response:
        async for chunk in response.aiter_bytes():
            data += chunk
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise ApiError(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"the file is larger than {MAX_DOWNLOAD_BYTES} bytes",
                )
    return bytes(data)


async def read_source(
    client: BambuddyClient, archive: ArchiveDetail, source: FoundSource
) -> SourceFile:
    """The bytes of a listed, available source: a 3MF (a ZIP), at most
    `MAX_DOWNLOAD_BYTES`. The printer's is read only for a printer source."""
    name = source.name or archive.filename or f"print-{archive.id}.3mf"
    if source.kind == "archive":
        data = await _read(
            client,
            f"/archives/{archive.id}/download",
            operation="media.download",
            what="read the archive's 3MF",
        )
    elif source.kind == "archive_source":
        data = await _read(
            client,
            f"/archives/{archive.id}/source",
            operation="media.source",
            what="read the archive's slicer project",
        )
    elif source.kind == "library":
        assert source.library_file_id is not None
        buffer = bytearray()
        async with aclosing(client.download_library_file(source.library_file_id)) as chunks:
            async for chunk in chunks:
                buffer += chunk
                if len(buffer) > MAX_DOWNLOAD_BYTES:
                    raise ApiError(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        f"the file is larger than {MAX_DOWNLOAD_BYTES} bytes",
                    )
        data = bytes(buffer)
    else:
        assert archive.printer_id is not None and source.path is not None
        data = await _read(
            client,
            f"/printers/{archive.printer_id}/files/download",
            operation="printers.files.download",
            what=f"read {source.path} from the printer",
            params={"path": source.path},
            scope=Scope.CONTROL_PRINTER,
        )
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name} is not a 3MF")
    return SourceFile(name=name, data=data, sha256=hashlib.sha256(data).hexdigest())


class ModelSuggestion(BaseModel):
    slug: str
    name: str


_TOKENS = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKENS.findall(text.lower()))


def suggested_slugs(slugs: Sequence[str], job: str | None, *, limit: int = 5) -> list[str]:
    """The models a job's name suggests (``basketball_trophy_elan`` suggests
    ``basketball-trophy``): every word of the slug is a word of the job's name, or every
    word of the name is one of the slug's; most shared words first. Only a suggestion:
    the person chooses."""
    if not job:
        return []
    words = _tokens(_stem(job))
    if not words:
        return []
    scored: list[tuple[int, str]] = []
    for slug in slugs:
        own = _tokens(slug.removeprefix("builtin:"))
        if own and (own <= words or words <= own):
            scored.append((len(own & words), slug))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [slug for _, slug in scored[:limit]]
