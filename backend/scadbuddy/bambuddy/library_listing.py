"""The Library page's listing (#313): Bambuddy's folder tree and one folder's files."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import Folder, LibraryListRow
from scadbuddy.bambuddy.print_source import is_sliced, printable

#: What the page lists without Advanced: unsliced 3MFs (spec 2026-09-28 §2).
DEFAULT_TYPES: frozenset[str] = frozenset({"3mf"})


class LibraryFolderView(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    #: How deep the folder sits, 0 at the top; the page indents by it.
    depth: int = 0
    file_count: int | None = None
    #: The Bambuddy project this folder is linked to: the project lists the files directly
    #: in it, so a move out of it takes a file out of the project (#2670).
    project_id: int | None = None
    project_name: str | None = None


class LibraryEntry(BaseModel):
    id: int
    filename: str
    file_type: str
    folder_id: int | None = None
    has_thumbnail: bool = False
    print_count: int = 0
    #: Whether the dialog prints it.
    printable: bool
    #: Sliced already (``gcode.3mf``, #2671): printed as it is, never arranged.
    sliced: bool = False
    #: Bytes, and when it was added (UTC): what tells two files of one name apart (#935).
    file_size: int | None = None
    created_at: datetime | None = None
    #: The output ScadBuddy uploaded this file as (#455), through which Arrange reads its
    #: objects (#1864); ``None`` for a file ScadBuddy did not make.
    output_id: str | None = None
    #: Indexed from an external folder: Bambuddy deletes it for good, never to its
    #: trash, so a delete of it cannot be undone (#2167).
    is_external: bool = False


class LibraryListing(BaseModel):
    folder_id: int | None = None
    all: bool = False
    folders: list[LibraryFolderView] = Field(default_factory=list)
    files: list[LibraryEntry] = Field(default_factory=list)
    #: This folder's files that only Advanced lists.
    hidden: int = 0


def _entry(row: LibraryListRow) -> LibraryEntry:
    return LibraryEntry(
        id=row.id,
        filename=row.filename,
        file_type=row.file_type,
        folder_id=row.folder_id,
        has_thumbnail=row.thumbnail_path is not None,
        print_count=row.print_count,
        printable=printable(row.file_type),
        sliced=is_sliced(row.file_type),
        file_size=row.file_size,
        created_at=_utc(row.created_at) if row.created_at is not None else None,
        is_external=row.is_external,
    )


def _utc(when: datetime) -> datetime:
    """Bambuddy's times are naive and are UTC (as in ``bambuddy.hardware``)."""
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def folder_views(folders: Iterable[Folder]) -> list[LibraryFolderView]:
    """Bambuddy's folder tree, flattened depth first, each folder with its depth."""
    return [
        LibraryFolderView(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            depth=depth,
            file_count=folder.file_count,
            project_id=folder.project_id,
            project_name=folder.project_name,
        )
        for top in folders
        for depth, folder in top.walk_with_depth()
    ]


async def list_library(
    client: BambuddyClient, *, folder_id: int | None, show_all: bool
) -> LibraryListing:
    """One read of the tree and one of the folder, however many files it holds. The two
    are read at once (#3633): each is most of a second on a library of hundreds of
    folders, and neither waits on the other."""
    tree, rows = await asyncio.gather(client.folders(), client.library_listing(folder_id=folder_id))
    here = [row for row in rows if row.folder_id == folder_id]
    shown = [row for row in here if show_all or row.file_type.lower() in DEFAULT_TYPES]
    return LibraryListing(
        folder_id=folder_id,
        all=show_all,
        folders=folder_views(tree),
        files=[_entry(row) for row in sorted(shown, key=lambda row: row.filename.casefold())],
        hidden=len(here) - len(shown),
    )


#: The most files one search answers; ``total`` says how many matched.
SEARCH_LIMIT = 100

#: What separates words in a file or folder name, besides spaces (``3D_Name_Keychains``).
_SEPARATORS = re.compile(r"[\s_\-.()\[\]]+")


class LibrarySearchHit(LibraryEntry):
    #: The folder's names from the top, empty for the top level: where the file is.
    folder_path: list[str] = Field(default_factory=list)


class LibrarySearch(BaseModel):
    query: str
    all: bool = False
    files: list[LibrarySearchHit] = Field(default_factory=list)
    #: How many files matched, of the kinds listed; ``files`` holds the first
    #: :data:`SEARCH_LIMIT`.
    total: int = 0
    #: Files that matched which only Advanced lists.
    hidden: int = 0


def _words(text: str) -> str:
    return _SEPARATORS.sub(" ", text.casefold()).strip()


def search_library(
    folders: Sequence[LibraryFolderView],
    rows: Iterable[LibraryListRow],
    query: str,
    *,
    show_all: bool,
    limit: int = SEARCH_LIMIT,
) -> LibrarySearch:
    """The files whose name, or the path of the folder they are in, holds every word of
    ``query``, whatever the case or separator (#3630): ``dino keychain`` finds
    ``Dinosaur_Keychain (ScadBuddy).3mf`` and every file under ``3D Name Keychains``.
    Files whose own name holds more of the words come first, then by name."""
    terms = _words(query).split()
    found = LibrarySearch(query=query, all=show_all)
    if not terms:
        return found
    by_id = {folder.id: folder for folder in folders}
    paths: dict[int | None, list[str]] = {None: []}

    def path(folder_id: int | None) -> list[str]:
        if folder_id not in paths:
            folder = by_id.get(folder_id) if folder_id is not None else None
            paths[folder_id] = [] if folder is None else [*path(folder.parent_id), folder.name]
        return paths[folder_id]

    matched: list[tuple[int, LibraryListRow]] = []
    for row in rows:
        name = _words(row.filename)
        where = _words(" ".join(path(row.folder_id)))
        if not all(term in name or term in where for term in terms):
            continue
        if not show_all and row.file_type.lower() not in DEFAULT_TYPES:
            found.hidden += 1
            continue
        matched.append((sum(term in name for term in terms), row))
    matched.sort(key=lambda hit: (-hit[0], hit[1].filename.casefold(), hit[1].id))
    found.total = len(matched)
    found.files = [
        LibrarySearchHit(**_entry(row).model_dump(), folder_path=path(row.folder_id))
        for _, row in matched[:limit]
    ]
    return found
