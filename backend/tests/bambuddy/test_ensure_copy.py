"""#317: Generate's filing and a print started while it runs share one project copy.

The Customize page enables Print as soon as the output is saved, while the project file
is still being filed. Both then ask :func:`ensure_copy` for the same folder and target;
without a lock both miss the record and upload, leaving two files in the project folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
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
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.plate import DEFAULT_PLATE, plate_for
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.provenance import PROVENANCE_KEY

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

    async def forget(self, output_id: str, library_file_id: int) -> None:
        self.copies[output_id] = [
            copy for copy in self.copies.get(output_id, []) if copy.id != library_file_id
        ]

    async def same_content(
        self, folder_id: int | None, target_key: str, content_key: str
    ) -> list[tuple[str, LibraryCopy]]:
        return [
            (owner, copy)
            for owner, copies in self.copies.items()
            for copy in copies
            if (copy.folder_id, copy.target_key, copy.content_key)
            == (folder_id, target_key, content_key)
        ]

    async def shared(self, library_file_ids: Iterable[int], output_id: str) -> set[int]:
        return {
            copy.id
            for owner, copies in self.copies.items()
            if owner != output_id
            for copy in copies
        } & set(library_file_ids)

    @asynccontextmanager
    async def copy_lock(self, key: str) -> AsyncIterator[None]:
        yield


async def _stored_3mf(store: object, meta: object) -> bytes:
    return b"3mf"


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
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
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
    see ``Demo.3mf`` free and both upload under it (#540 review). Their 3MFs differ:
    identical ones would share one copy (#2476)."""

    async def own_3mf(store: object, meta: OutputMeta) -> bytes:
        return meta.id.encode()

    monkeypatch.setattr(send, "read_3mf", own_3mf)
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
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
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
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
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
    # It was printed from, so a later copy for other spools leaves it be (#2476).
    uploads.copies[meta.id][0].sliced.append(SlicedCopy(id=99))
    filed = await ensure(Target(DEFAULT_PLATE, "0.2"))
    other = await ensure(Target(DEFAULT_PLATE, "0.2", colours=("#00ff00",)))

    assert (filed.created, filed.library_file_id) == (False, printed.library_file_id)
    assert other.created
    assert bambuddy.uploaded == ["Demo.3mf", "Demo (2).3mf"]


async def test_a_two_filament_copy_from_before_slot_hints_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy uploaded before #4013 names no painted slot, so Bambuddy slices a painted
    filament with another's preset; one of two filaments or more is uploaded again, and
    the new one is reused, by a print and by a filing in the model's own colours."""
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    uploads = MemoryUploads()
    meta = output("h").model_copy(update={"colors": ["#FF0000", "#00FF00"]})
    spools = Target(DEFAULT_PLATE, "0.2", colours=("#ff0000", "#00ff00"))
    assert spools.key.endswith(";slots")
    assert not Target(DEFAULT_PLATE, "0.2", colours=("#ff0000",)).key.endswith(";slots")
    bambuddy.uploaded.append("Demo.3mf")
    before = LibraryCopy(
        id=1,
        folder_id=FOLDER,
        target_key=spools.key.removesuffix(";slots"),
        sliced=[SlicedCopy(id=98)],
    )
    uploads.copies[meta.id] = [before]

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

    printed = await ensure(spools)
    again = await ensure(spools)
    filed = await ensure(Target(DEFAULT_PLATE, "0.2"))

    assert printed.created and printed.library_file_id != before.id
    assert (again.created, again.library_file_id) == (False, printed.library_file_id)
    assert (filed.created, filed.library_file_id) == (False, printed.library_file_id)


async def test_a_copy_from_before_the_bed_was_stated_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4638: a copy laid out before the file stated its bed (``printable_area``) slices
    every plate past the first empty, and reusing it kept the quote failing after the
    fix. It is uploaded again, and the new copy is the one reused."""
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    uploads = MemoryUploads()
    meta = output("b").model_copy(update={"colors": ["#FF0000", "#00FF00"]})
    target = Target(plate_for("H2C"), "0.4", colours=("#ff0000", "#00ff00"))
    assert target.key.startswith("Bambu Lab H2C#bed@0.4")
    bambuddy.uploaded.append("Demo.3mf")
    # As #4638's copy 5394 was recorded: the same key, less the marker. Sliced from, so
    # it is kept beside the new copy rather than superseded.
    before = LibraryCopy(
        id=1,
        folder_id=FOLDER,
        target_key=target.key.replace("#bed", "", 1),
        sliced=[SlicedCopy(id=98)],
    )
    uploads.copies[meta.id] = [before]

    async def ensure() -> send.EnsuredCopy:
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

    printed = await ensure()
    again = await ensure()
    assert printed.created and printed.library_file_id != before.id
    assert (again.created, again.library_file_id) == (False, printed.library_file_id)


async def test_an_uncoloured_copy_from_before_slot_hints_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy Generate filed for a two-filament output before #4013, in the model's
    own colours, has no hint marker either: a print in those colours uploads a new one,
    rather than reusing one whose slice comes back swapped and is refused every time
    (claude-review on #4056). The new one is reused, and a one-filament copy keeps its
    key."""
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy = SlowBambuddy()
    uploads = MemoryUploads()
    meta = output("h").model_copy(update={"colors": ["#FF0000", "#00FF00"]})
    own = Target(DEFAULT_PLATE, "0.2", colours=("#ff0000", "#00ff00"))
    bambuddy.uploaded.append("Demo.3mf")
    before = LibraryCopy(
        id=1, folder_id=FOLDER, target_key=own.uncoloured_key, sliced=[SlicedCopy(id=98)]
    )
    uploads.copies[meta.id] = [before]

    async def ensure(target: Target, subject: OutputMeta = meta) -> send.EnsuredCopy:
        return await ensure_copy(
            cast(BambuddyClient, bambuddy),
            cast(OutputStore, None),
            cast(BambuddyUploadStore, uploads),
            subject,
            StoredSettings(library_folder_id=2),
            target=target,
            folder_id=FOLDER,
            stem="Demo",
        )

    printed = await ensure(own)
    filed = await ensure(Target(DEFAULT_PLATE, "0.2"))

    assert printed.created and printed.library_file_id != before.id
    assert (filed.created, filed.library_file_id) == (False, printed.library_file_id)
    single = output("s").model_copy(update={"colors": ["#FF0000"]})
    uploads.copies[single.id] = [LibraryCopy(id=1, folder_id=FOLDER, target_key=own.uncoloured_key)]
    kept = await ensure(Target(DEFAULT_PLATE, "0.2"), single)
    assert (kept.created, kept.library_file_id) == (False, 1)


@pytest.mark.requires_postgres
async def test_two_replicas_filing_and_printing_at_once_upload_one_copy(
    monkeypatch: pytest.MonkeyPatch, pg_conninfo: str, tmp_path: Path
) -> None:
    """The in-process lock is per replica; two replicas on one database share only the
    database, so the advisory lock is what makes them upload one copy (#540 review)."""
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
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
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
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
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
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


class Library(SlowBambuddy):
    """A folder's files by id, which deletes and renames change; each file's note."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[int] = []
        self.notes: dict[int, str] = {}
        self.renamed: dict[int, str] = {}

    def _name(self, file_id: int) -> str:
        return self.renamed.get(file_id, self.uploaded[file_id - 1])

    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        return [
            LibraryFile(id=number, filename=self._name(number))
            for number in range(1, len(self.uploaded) + 1)
            if number not in self.deleted
        ]

    async def library_file(self, file_id: int) -> LibraryFile:
        if file_id in self.deleted:
            raise ApiError(404, "gone")
        return LibraryFile(id=file_id, filename=self._name(file_id), notes=self.notes.get(file_id))

    async def delete_library_file(self, file_id: int) -> bool:
        self.deleted.append(file_id)
        return True

    async def rename_library_file(self, file_id: int, filename: str) -> LibraryFile:
        self.renamed[file_id] = filename
        return await self.library_file(file_id)

    async def annotate_library_file(self, file_id: int, notes: str) -> LibraryFile:
        self.notes[file_id] = notes
        return await self.library_file(file_id)


async def _file(
    bambuddy: Library, uploads: MemoryUploads, meta: OutputMeta, target: Target
) -> send.EnsuredCopy:
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


def _stamped(output_id: str) -> bytes:
    """A 3MF stamped as ``output_id``'s, the rest the same for every output."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?>\n<model unit="millimeter">\n'
            f' <metadata name="{PROVENANCE_KEY}">{{"output": "{output_id}"}}</metadata>\n'
            "<resources/></model>",
        )
        archive.writestr("Metadata/plate_1.png", b"cover")
    return buffer.getvalue()


async def test_an_identical_output_filed_again_takes_the_copy_already_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2476: Generate twice with the same parameters saves two outputs whose 3MFs
    differ only in the stamp naming each. The second filing takes the first one's copy
    rather than adding ``Demo (2).3mf``, and records it as its own."""

    async def stamped(store: object, meta: OutputMeta) -> bytes:
        return _stamped(meta.id)

    monkeypatch.setattr(send, "read_3mf", stamped)
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    bambuddy, uploads = Library(), MemoryUploads()

    first = await _file(bambuddy, uploads, output("h"), Target(DEFAULT_PLATE))
    second = await _file(bambuddy, uploads, output("i"), Target(DEFAULT_PLATE))

    assert bambuddy.uploaded == ["Demo.3mf"]
    assert (second.created, second.library_file_id, second.filename) == (
        False,
        first.library_file_id,
        "Demo.3mf",
    )
    assert [copy.id for copy in uploads.copies["i" * 32]] == [first.library_file_id]
    # On another plate it is another file.
    other = await _file(bambuddy, uploads, output("i"), Target(DEFAULT_PLATE, "0.2"))
    assert other.created


async def test_a_print_for_another_printer_replaces_the_copy_nothing_printed_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2476: Generate files a copy laid out for its guess at the printer. A print on
    another one puts its own copy beside it; rather than leave two files (and the
    print's slice) with no way to tell which printed, the guess goes, and the print's
    copy takes its name and its "Edit in ScadBuddy" note."""
    monkeypatch.setattr(send, "read_3mf", _stored_3mf)
    monkeypatch.setattr(
        send, "_laid_out_for", lambda payload, target: payload + target.key.encode()
    )
    bambuddy, uploads = Library(), MemoryUploads()
    meta = output("j")

    filed = await _file(bambuddy, uploads, meta, Target(DEFAULT_PLATE))
    bambuddy.notes[filed.library_file_id] = "Edit in ScadBuddy: https://sb/edit/j"
    printed = await _file(bambuddy, uploads, meta, Target(DEFAULT_PLATE, "0.2"))

    assert bambuddy.uploaded == ["Demo.3mf", "Demo (2).3mf"]
    assert bambuddy.deleted == [filed.library_file_id]
    assert (printed.created, printed.filename) == (True, "Demo.3mf")
    assert bambuddy.notes[printed.library_file_id] == "Edit in ScadBuddy: https://sb/edit/j"
    assert [copy.id for copy in uploads.copies[meta.id]] == [printed.library_file_id]


async def test_a_copy_printed_from_or_shared_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy sliced from is the project's record of what it printed (#316), and one an
    identical output shares is that output's file too: both stay."""

    async def stamped(store: object, meta: OutputMeta) -> bytes:
        return _stamped(meta.id)

    monkeypatch.setattr(send, "read_3mf", stamped)
    monkeypatch.setattr(
        send, "_laid_out_for", lambda payload, target: payload + target.key.encode()
    )
    bambuddy, uploads = Library(), MemoryUploads()
    printed, shared = output("k"), output("l")

    sliced = await _file(bambuddy, uploads, printed, Target(DEFAULT_PLATE))
    uploads.copies[printed.id][0].sliced.append(SlicedCopy(id=99))
    await _file(bambuddy, uploads, printed, Target(DEFAULT_PLATE, "0.2"))
    assert bambuddy.deleted == []

    # `shared` files the same 3MF, so takes the copy `printed` has for the 0.2 plate.
    await _file(bambuddy, uploads, shared, Target(DEFAULT_PLATE, "0.2"))
    await _file(bambuddy, uploads, shared, Target(DEFAULT_PLATE, "0.6"))
    assert bambuddy.deleted == []
    assert sliced.library_file_id in {copy.id for copy in uploads.copies[printed.id]}


def test_every_layout_a_slice_depends_on_keys_its_own_copy() -> None:
    """#3437: a recorded slice is looked up by its copy's id and the preset key, so the
    copy stands in for the layout. Each part of the layout a slice depends on (the
    nozzle, the sides it may use, their flow, the filament map) keys a copy of its own,
    so a slice of one layout is never reused for another."""
    from scadbuddy.render.bambu3mf import FilamentMap

    base = Target(DEFAULT_PLATE, "0.4")
    layouts = [
        base,
        Target(DEFAULT_PLATE, "0.6"),
        Target(DEFAULT_PLATE, "0.4", nozzle_stats=("1", "0")),
        Target(DEFAULT_PLATE, "0.4", nozzle_volume_type=("1", "0")),
        Target(DEFAULT_PLATE, "0.4", filament_map=FilamentMap(("1", "2"), ("0", "0"))),
        Target(DEFAULT_PLATE, "0.4", filament_map=FilamentMap(("2", "1"), ("0", "0"))),
    ]
    assert len({layout.key for layout in layouts}) == len(layouts)
    # The same layout is the same copy, so its slice is reused.
    assert Target(DEFAULT_PLATE, "0.4").key == base.key
