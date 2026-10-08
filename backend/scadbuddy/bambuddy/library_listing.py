"""The Library page's listing (#313): Bambuddy's folder tree and one folder's files."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import LibraryListRow
from scadbuddy.bambuddy.print_source import printable

#: What the page lists without Advanced: unsliced 3MFs (spec 2026-09-28 §2).
DEFAULT_TYPES: frozenset[str] = frozenset({"3mf"})


class LibraryFolderView(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    #: How deep the folder sits, 0 at the top; the page indents by it.
    depth: int = 0
    file_count: int | None = None


class LibraryEntry(BaseModel):
    id: int
    filename: str
    file_type: str
    folder_id: int | None = None
    has_thumbnail: bool = False
    print_count: int = 0
    #: Whether the dialog prints it; a sliced file is printed from Bambuddy directly.
    printable: bool
    #: Bytes, and when it was added (UTC): what tells two files of one name apart (#935).
    file_size: int | None = None
    created_at: datetime | None = None


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
        file_size=row.file_size,
        created_at=_utc(row.created_at) if row.created_at is not None else None,
    )


def _utc(when: datetime) -> datetime:
    """Bambuddy's times are naive and are UTC (as in ``bambuddy.hardware``)."""
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


async def list_library(
    client: BambuddyClient, *, folder_id: int | None, show_all: bool
) -> LibraryListing:
    """One read of the tree and one of the folder, however many files it holds."""
    folders = [
        LibraryFolderView(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            depth=depth,
            file_count=folder.file_count,
        )
        for top in await client.folders()
        for depth, folder in top.walk_with_depth()
    ]
    here = [
        row
        for row in await client.library_listing(folder_id=folder_id)
        if row.folder_id == folder_id
    ]
    shown = [row for row in here if show_all or row.file_type.lower() in DEFAULT_TYPES]
    return LibraryListing(
        folder_id=folder_id,
        all=show_all,
        folders=folders,
        files=[_entry(row) for row in sorted(shown, key=lambda row: row.filename.casefold())],
        hidden=len(here) - len(shown),
    )
