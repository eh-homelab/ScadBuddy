"""#317: Generate's filing and a print started while it runs share one project copy.

The Customize page enables Print as soon as the output is saved, while the project file
is still being filed. Both then ask :func:`ensure_copy` for the same folder and target;
without a lock both miss the record and upload, leaving two files in the project folder.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from scadbuddy.bambuddy import send
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.send import Target, ensure_copy
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.plate import DEFAULT_PLATE

FOLDER = 9


class SlowBambuddy:
    """Uploads take a moment, so a second caller runs while the first is uploading."""

    def __init__(self) -> None:
        self.uploaded: list[str] = []

    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        return []

    async def library_file(self, file_id: int) -> LibraryFile:
        return LibraryFile(id=file_id, filename=self.uploaded[file_id - 1])

    async def upload_library_file(
        self, filename: str, content: bytes, *, folder_id: int | None = None
    ) -> LibraryFile:
        await asyncio.sleep(0.05)
        self.uploaded.append(filename)
        return LibraryFile(id=len(self.uploaded), filename=filename)


class MemoryUploads:
    def __init__(self) -> None:
        self.copies: list[LibraryCopy] = []

    async def for_output(self, output_id: str) -> list[LibraryCopy]:
        return list(self.copies)

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        self.copies.append(copy)


async def test_filing_and_a_print_at_once_upload_one_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    meta = OutputMeta(
        id="c" * 32,
        slug="demo",
        job_id="d" * 32,
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        colors=["#FF0000"],
    )

    def ensure(target: Target) -> asyncio.Future[send.EnsuredCopy]:
        return asyncio.ensure_future(
            ensure_copy(
                cast(BambuddyClient, bambuddy),
                cast(OutputStore, None),
                cast(BambuddyUploadStore, uploads),
                meta,
                StoredSettings(library_folder_id=2),
                target=target,
                folder_id=FOLDER,
                stem="Demo",
            )
        )

    uploads = MemoryUploads()
    # Generate files the model's own colours; the print chose a spool of the same colour.
    filed, printed = await asyncio.gather(
        ensure(Target(DEFAULT_PLATE)), ensure(Target(DEFAULT_PLATE, colours=("#ff0000",)))
    )

    assert bambuddy.uploaded == ["Demo.3mf"]
    assert (filed.created, printed.created) == (True, False)
    assert printed.library_file_id == filed.library_file_id
