"""A file listing's held copy as the slice reads it (#6710, spec 2026-11-21 §7.3.1).

Publish keeps a copy of the listed library file in the content store,
``listing-file-<sha256>``, and that copy is the version the listing sells. Its price
slices that copy, never the live library file: :class:`HeldFile` is a ``LibraryReads``
(``print_library/reads.py``) over it, which ``orders/bambuddy.py``
``start_library_quote_preview`` gives ``LibrarySource.load`` in place of the library's own
reads. So the slice of a held copy goes through the back office's own pipeline, with only
the bytes' source changed. A file renamed, changed or deleted in the library after publish
changes neither the bytes nor the price.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING, Final, Literal

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.models import (
    FilamentRequirement,
    FilamentRequirements,
    LibraryFile,
    LibraryPlate,
    LibraryPlates,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.orders.models import ItemLibrary

if TYPE_CHECKING:
    from scadbuddy.store.content import ContentStore


class PricedFile(BaseModel):
    """A file listing's file as its price names it (#6710 §7.1): the listing's held copy
    of a library file, which the slice reads (:class:`HeldFile`)."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    library: ItemLibrary
    filename: str
    #: The pinned content: the held copy's bytes, and the price key's ``model``.
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The held copy the slice reads, ``listing-file-<sha256>`` (``FileSnapshot.copy_key``).
    copy_key: str = Field(min_length=1)
    file_type: Literal["3mf", "stl"]
    size: int = Field(ge=0)
    plates: list[int]
    #: The slots the slice must use (``FileSlot.used``), from 0.
    used: list[int]
    #: ``FileSnapshot.remembered``: the remembered choices of ``library:<id>`` when the
    #: listing was published, for a scope that holds none now (decision 4).
    remembered: ModelPrintChoices | None = None


#: The one text for a held copy gone from the store: the price's failure
#: (`slice_price.price_variant`) and the reads' 404 both say it.
FILE_GONE: Final = "the listing's copy of its file is missing"


def _not_found(file_id: int) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"library file {file_id} not found", file_id=file_id)


class HeldFile:
    """``LibraryReads`` over one held copy (#6710 §7.3.1): the listed file's row, plates and
    bytes as publish pinned them. Any other id is a 404, as a library answers a file it
    does not hold."""

    def __init__(self, file: PricedFile, content: ContentStore) -> None:
        self.file = file
        self.content = content

    def _check(self, file_id: int) -> None:
        if file_id != self.file.id:
            raise _not_found(file_id)

    async def library_file(self, file_id: int) -> LibraryFile:
        """The listed file, its ``file_hash`` the pinned sha256: what names the held
        source's copies (``Printable.version``)."""
        self._check(file_id)
        file = self.file
        return LibraryFile(
            id=file.id,
            filename=file.filename,
            file_type=file.file_type,
            file_size=file.size,
            file_hash=file.sha256,
        )

    async def library_plates(self, file_id: int) -> LibraryPlates:
        self._check(file_id)
        return LibraryPlates(
            file_id=file_id,
            plates=[LibraryPlate(index=plate) for plate in self.file.plates],
            is_multi_plate=len(self.file.plates) > 1,
        )

    async def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]:
        """The held copy's bytes, streamed from the content store. A copy gone from the
        store (only after a fault: publish holds it) is the 404 a missing file is."""
        self._check(file_id)
        stat = await self.content.stat(self.file.copy_key)
        if stat is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                FILE_GONE,
                file_id=file_id,
            )
        async with aclosing(self.content.get(stat.ref)) as chunks:
            async for chunk in chunks:
                yield chunk

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        """The used slots, by slot id (from 1), with no colour or type: reached only where
        the bytes give no slots (an STL, ``print_source.py`` ``_requirements``). The
        slice's colour for each comes from its ``slot_colours``."""
        self._check(file_id)
        return FilamentRequirements(
            file_id=file_id,
            filename=self.file.filename,
            plate_id=plate_id,
            filaments=[FilamentRequirement(slot_id=slot + 1) for slot in self.file.used],
        )
