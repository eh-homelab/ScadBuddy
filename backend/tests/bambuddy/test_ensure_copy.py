"""#317: Generate's filing and a print started while it runs share one project copy.

The Customize page enables Print as soon as the output is saved, while the project file
is still being filed. Both then ask :func:`ensure_copy` for the same folder and target;
without a lock both miss the record and upload, leaving two files in the project folder.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.bambuddy import send
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.send import Target, ensure_copy
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.plate import DEFAULT_PLATE
from scadbuddy.render.projection import JobProjection

FOLDER = 9


class SlowBambuddy:
    """Uploads take a moment, so a second caller runs while the first is uploading."""

    def __init__(self) -> None:
        self.uploaded: list[str] = []

    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        return [
            LibraryFile(id=number, filename=name)
            for number, name in enumerate(self.uploaded, start=1)
        ]

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
        self.copies: dict[str, list[LibraryCopy]] = {}

    async def for_output(self, output_id: str) -> list[LibraryCopy]:
        return list(self.copies.get(output_id, []))

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        self.copies.setdefault(output_id, []).append(copy)

    async def recorded(self, library_file_ids: Iterable[int]) -> set[int]:
        ids = {copy.id for copies in self.copies.values() for copy in copies}
        return ids & set(library_file_ids)

    @asynccontextmanager
    async def copy_lock(self, key: str) -> AsyncIterator[None]:
        yield


def output(letter: str) -> OutputMeta:
    return OutputMeta(
        id=letter * 32,
        slug="demo",
        job_id="d" * 32,
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        colors=["#FF0000"],
    )


async def test_filing_and_a_print_at_once_upload_one_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    meta = output("c")

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


async def test_two_outputs_filed_into_one_folder_at_once_get_different_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two customizations with the same changed params name the same stem. The folder's
    listing and the upload that takes a name from it are one step per folder, or both
    see ``Demo.3mf`` free and both upload under it (#540 review)."""
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    uploads = MemoryUploads()

    def ensure(meta: OutputMeta) -> asyncio.Future[send.EnsuredCopy]:
        return asyncio.ensure_future(
            ensure_copy(
                cast(BambuddyClient, bambuddy),
                cast(OutputStore, None),
                cast(BambuddyUploadStore, uploads),
                meta,
                StoredSettings(library_folder_id=2),
                target=Target(DEFAULT_PLATE),
                folder_id=FOLDER,
                stem="Demo",
            )
        )

    await asyncio.gather(ensure(output("a")), ensure(output("b")))

    assert sorted(bambuddy.uploaded) == ["Demo (2).3mf", "Demo.3mf"]


class ListingFails(SlowBambuddy):
    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        raise ApiError(503, "Bambuddy is busy")


async def test_a_folder_listing_that_fails_still_uploads_under_a_plain_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing only makes the name unique; it never fails the print (#540 review)."""
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = ListingFails()

    copy = await ensure_copy(
        cast(BambuddyClient, bambuddy),
        cast(OutputStore, None),
        cast(BambuddyUploadStore, MemoryUploads()),
        output("e"),
        StoredSettings(library_folder_id=2),
        target=Target(DEFAULT_PLATE),
        folder_id=FOLDER,
        stem="Demo",
    )

    assert (copy.created, bambuddy.uploaded) == (True, ["Demo.3mf"])


async def test_filing_after_a_print_in_the_models_own_colours_reuses_its_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A print whose spools were the model's colours records its copy under a coloured
    key; a filing with no spools must reuse it, not upload a duplicate (#540 review)."""
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    uploads = MemoryUploads()
    meta = output("f")

    async def ensure(target: Target) -> send.EnsuredCopy:
        return await ensure_copy(
            cast(BambuddyClient, bambuddy),
            cast(OutputStore, None),
            cast(BambuddyUploadStore, uploads),
            meta,
            StoredSettings(library_folder_id=2),
            target=target,
            folder_id=FOLDER,
            stem="Demo",
        )

    printed = await ensure(Target(DEFAULT_PLATE, "0.2", colours=("#ff0000",)))
    filed = await ensure(Target(DEFAULT_PLATE, "0.2"))
    other = await ensure(Target(DEFAULT_PLATE, "0.2", colours=("#00ff00",)))

    assert (filed.created, filed.library_file_id) == (False, printed.library_file_id)
    assert other.created
    assert bambuddy.uploaded == ["Demo.3mf", "Demo (2).3mf"]


@pytest.mark.requires_postgres
async def test_two_replicas_filing_and_printing_at_once_upload_one_copy(
    monkeypatch: pytest.MonkeyPatch, pg_conninfo: str, tmp_path: Path
) -> None:
    """The in-process lock is per replica; two replicas on one database share only the
    database, so the advisory lock is what makes them upload one copy (#540 review)."""
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    # Each replica has its own process lock: none is shared between the two calls.
    monkeypatch.setattr(send, "_copy_lock", lambda key: asyncio.Lock())
    bambuddy = SlowBambuddy()
    meta = output("g")
    replicas = [JobProjection(pg_conninfo, pool_size=2) for _name in ("one", "two")]
    for replica in replicas:
        replica.open()
    try:

        def ensure(replica: JobProjection) -> asyncio.Future[send.EnsuredCopy]:
            return asyncio.ensure_future(
                ensure_copy(
                    cast(BambuddyClient, bambuddy),
                    cast(OutputStore, None),
                    BambuddyUploadStore(replica.pool),
                    meta,
                    StoredSettings(library_folder_id=2),
                    target=Target(DEFAULT_PLATE),
                    folder_id=FOLDER,
                    stem="Demo",
                )
            )

        first, second = await asyncio.gather(*(ensure(replica) for replica in replicas))
    finally:
        for replica in replicas:
            replica.close()

    assert bambuddy.uploaded == ["Demo.3mf"]
    assert sorted((first.created, second.created)) == [False, True]
    assert first.library_file_id == second.library_file_id


class Hashing(SlowBambuddy):
    """Keeps what each upload carried, and reads it back with its sha256, as Bambuddy's
    ``FileResponse.file_hash``."""

    def __init__(self) -> None:
        super().__init__()
        self.contents: list[bytes] = []

    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        return [
            LibraryFile(id=number, filename=name, file_size=len(content))
            for number, (name, content) in enumerate(
                zip(self.uploaded, self.contents, strict=True), start=1
            )
        ]

    async def library_file(self, file_id: int) -> LibraryFile:
        return LibraryFile(
            id=file_id,
            filename=self.uploaded[file_id - 1],
            file_hash=hashlib.sha256(self.contents[file_id - 1]).hexdigest(),
        )

    async def upload_library_file(
        self, filename: str, content: bytes, *, folder_id: int | None = None
    ) -> LibraryFile:
        self.contents.append(content)
        return await super().upload_library_file(filename, content, folder_id=folder_id)


class DiesOnce(MemoryUploads):
    """The first record raises: the attempt died after Bambuddy stored the upload."""

    def __init__(self) -> None:
        super().__init__()
        self.died = False

    async def record(self, output_id: str, copy: LibraryCopy) -> None:
        if not self.died:
            self.died = True
            raise RuntimeError("the attempt died after the upload")
        await super().record(output_id, copy)


@pytest.mark.parametrize(("folder_id", "uploaded_as"), [(FOLDER, "Demo.3mf"), (None, "demo-h.3mf")])
async def test_a_retry_takes_the_upload_its_attempt_left_unrecorded(
    monkeypatch: pytest.MonkeyPatch, folder_id: int | None, uploaded_as: str
) -> None:
    """#1145, #1127: in a project's folder (a name made unique from the stem) and in the
    inbox, the retry finds the file by its bytes and records it rather than uploading it
    twice."""
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = Hashing()
    uploads = DiesOnce()
    meta = output("h").model_copy(update={"name": "h"})

    async def ensure() -> send.EnsuredCopy:
        return await ensure_copy(
            cast(BambuddyClient, bambuddy),
            cast(OutputStore, None),
            cast(BambuddyUploadStore, uploads),
            meta,
            StoredSettings(library_folder_id=2 if folder_id is None else None),
            target=Target(DEFAULT_PLATE),
            folder_id=folder_id,
            stem="Demo",
        )

    with pytest.raises(RuntimeError):
        await ensure()
    retried = await ensure()

    assert bambuddy.uploaded == [uploaded_as]
    assert (retried.library_file_id, retried.created) == (1, True)
    assert [copy.id for copy in await uploads.for_output(meta.id)] == [1]


async def test_a_file_of_the_same_name_and_size_with_other_bytes_is_not_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1145: only the same bytes are the copy an attempt left; an unrecorded file that
    merely shares the name and size (someone else's upload) is left alone."""
    monkeypatch.setattr(send, "_read_3mf", lambda store, meta: b"3mf")
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = Hashing()
    await bambuddy.upload_library_file("Demo.3mf", b"abc", folder_id=FOLDER)
    uploads = MemoryUploads()

    copy = await ensure_copy(
        cast(BambuddyClient, bambuddy),
        cast(OutputStore, None),
        cast(BambuddyUploadStore, uploads),
        output("i"),
        StoredSettings(library_folder_id=2),
        target=Target(DEFAULT_PLATE),
        folder_id=FOLDER,
        stem="Demo",
    )

    assert bambuddy.uploaded == ["Demo.3mf", "Demo (2).3mf"]
    assert (copy.library_file_id, copy.created) == (2, True)
