"""A native run's laid-out per-print copy in ScadBuddy's own library (#3818 tier A Task 4,
plan 2026-10-18-hot-path-without-bambuddy): `ensure_native_copy`, and the one id space
each path of `output_bambuddy_uploads` reads (Review Focus 2)."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.dispatch import QueueOutcome, SlicePlan
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.bambuddy.send import Printable, Target, _laid_out_for, ensure_uploaded, target_for
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_library.copies import ensure_native_copy
from scadbuddy.print_library.ids import NATIVE_ID_BASE, is_native_id
from scadbuddy.print_library.native import NativeLibrary, blob_key
from scadbuddy.render.plate import plate_for
from scadbuddy.store.index import Pool
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.slicing_activities import SliceActivities
from scadbuddy.workflows.slicing_models import SliceWorkflowInput
from tests.print_library.conftest import upload
from tests.support.no_bambuddy import forbid_bambuddy

pytestmark = pytest.mark.requires_postgres

BAMBUDDY = "https://bambuddy.test"
KEY = "out-copy-1"
H2D = Target(plate_for("Bambu Lab H2D"), "0.4")
X1C = Target(plate_for("Bambu Lab X1 Carbon"), "0.4")


@pytest.fixture
def uploads(pool: Pool) -> BambuddyUploadStore:
    return BambuddyUploadStore(pool)


class _Fetches:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.count = 0

    def printable(self, key: str = KEY) -> Printable:
        async def fetch() -> bytes:
            self.count += 1
            return self.payload

        return Printable(
            key=key, colours=("#FF6AC1", "#1F6FEB"), inbox_name="bracket.3mf", fetch=fetch
        )


@pytest.fixture
def fetches(three_mf: Path) -> _Fetches:
    return _Fetches(three_mf.read_bytes())


async def test_a_native_copy_is_uploaded_and_reused(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    folder = (await native.create_folder("Bracket", None)).id

    first = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=folder
    )
    again = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=folder
    )

    assert first == again and is_native_id(first)
    assert fetches.count == 1
    assert [f.id for f in await native.library_listing(folder_id=folder)] == [first]
    [copy] = await uploads.for_output(KEY, native=True)
    assert (copy.id, copy.folder_id) == (first, folder)
    file = await native.library_file(first)
    assert file.filename == "bracket.3mf"
    laid_out = _laid_out_for(fetches.payload, H2D)
    assert file.file_hash == hashlib.sha256(laid_out).hexdigest()
    # A per-print copy, not the output's filed one: nothing names an output on it.
    row = await native._file(first)
    assert (row.output_id, row.filed) == (None, False)


async def test_another_target_uploads_another_copy(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    h2d = await ensure_native_copy(native, uploads, fetches.printable(), target=H2D, folder_id=None)
    x1c = await ensure_native_copy(native, uploads, fetches.printable(), target=X1C, folder_id=None)

    assert h2d != x1c
    assert {c.id for c in await uploads.for_output(KEY, native=True)} == {h2d, x1c}
    # Never a delete: the superseded target's copy stays in the library.
    assert {f.id for f in await native.library_listing(folder_id=None)} == {h2d, x1c}


async def test_a_trashed_native_copy_is_uploaded_again(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    first = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )
    await native.trash([first])

    second = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )

    assert second != first
    assert [c.id for c in await uploads.for_output(KEY, native=True)] == [second]


async def test_a_moved_native_copy_is_not_reused(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    elsewhere = (await native.create_folder("Elsewhere", None)).id
    first = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )
    await native.update_file(first, folder_id=elsewhere)

    second = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )

    assert second != first
    assert (await native.library_file(first)).folder_id == elsewhere


async def test_an_earlier_attempts_unrecorded_copy_is_taken(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    """An attempt that died after the upload and before the record (#1145): its retry
    takes that file rather than upload a second."""
    laid_out = _laid_out_for(fetches.payload, H2D)
    left = await native.upload("bracket.3mf", io.BytesIO(laid_out), None)
    # Same name, other bytes: never taken.
    await native.upload("bracket.3mf", io.BytesIO(fetches.payload), None)

    taken = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )

    assert taken == left.id
    assert len(await native.library_listing(folder_id=None)) == 2


async def test_a_recorded_file_is_never_taken_as_an_earlier_attempts(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    laid_out = _laid_out_for(fetches.payload, H2D)
    other = await native.upload("bracket.3mf", io.BytesIO(laid_out), None)
    await uploads.record("another-output", LibraryCopy(id=other.id, folder_id=None, target_key="x"))

    made = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )

    assert made != other.id


async def test_a_native_copy_is_never_looked_up_in_bambuddy(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    """Review Focus 2: one subject with a Bambuddy copy and a native one, for the same
    folder (the top level) and target. Each path reads only its own: the native one
    never asks Bambuddy (any request but the mocked one fails), and the Bambuddy one
    never sees the native id."""
    native_id = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )
    key = (await uploads.for_output(KEY, native=True))[0].target_key
    await uploads.record(KEY, LibraryCopy(id=42, folder_id=None, target_key=key))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        asked = router.get(f"{BAMBUDDY}/api/v1/library/files/42").respond(
            200, json={"id": 42, "filename": "bracket.3mf"}
        )

        again = await ensure_native_copy(
            native, uploads, fetches.printable(), target=H2D, folder_id=None
        )
        assert again == native_id
        assert not asked.called

        async with BambuddyClient(BambuddyConfig(base_url=BAMBUDDY, api_key="k")) as client:
            bambuddy_id = await ensure_uploaded(
                client, uploads, fetches.printable(), StoredSettings(), target=H2D
            )

        assert bambuddy_id == 42
        assert all(str(native_id) not in str(call.request.url) for call in router.calls)
    assert [c.id for c in await uploads.for_output(KEY)] == [42]
    assert [c.id for c in await uploads.for_output(KEY, native=True)] == [native_id]


async def test_target_for_with_a_model_reads_no_printers() -> None:
    target = await target_for(
        None, StoredSettings(), printer_id=3, nozzle_diameter="0.4", model="Bambu Lab H2D"
    )
    assert target.plate == plate_for("Bambu Lab H2D")
    assert target.nozzle_diameter == "0.4"


async def test_target_for_without_a_client_needs_the_model() -> None:
    """M6: a misuse an activity reaches is an `ApiError`, which the print activities
    raise as non-retryable, never a `TypeError` retried to the end of its policy."""
    with pytest.raises(ApiError) as raised:
        await target_for(None, StoredSettings(), printer_id=3)
    assert raised.value.status == 500


async def test_a_bambuddy_copy_without_a_client_is_refused_once(
    native: NativeLibrary, uploads: BambuddyUploadStore, three_mf: Path
) -> None:
    original = await upload(native, three_mf)
    source = await LibrarySource.load(
        None, original.id, uploads=uploads, settings=StoredSettings(), files=native
    )
    with pytest.raises(ApiError):
        await source.file_to_print(
            None, printer_id=3, nozzle_size="0.4", plan=FilamentPlan(), project_id=None
        )
    with pytest.raises(ApiError):
        await source.file_to_print(
            None,
            printer_id=3,
            nozzle_size="0.4",
            plan=FilamentPlan(),
            project_id=5,
            native_copy=True,
            printer_model="Bambu Lab H2D",
        )


async def test_a_native_run_lays_a_library_files_copy_out_natively(
    native: NativeLibrary, uploads: BambuddyUploadStore, three_mf: Path
) -> None:
    """`file_to_print(native_copy=True)`: no client at all, the copy in the native
    library, recorded under the run subject."""
    original = await upload(native, three_mf)
    folder = (await native.create_folder("Prints", None)).id
    source = await LibrarySource.load(
        None, original.id, uploads=uploads, settings=StoredSettings(), files=native, read_own=True
    )
    source.native = native

    printed = await source.file_to_print(
        None,
        printer_id=3,
        nozzle_size="0.4",
        plan=FilamentPlan(),
        project_id=None,
        folder_id=folder,
        native_copy=True,
        printer_model="Bambu Lab H2D",
    )

    assert printed.folder_id == folder and is_native_id(printed.id) and printed.id != original.id
    copy = await native.library_file(printed.id)
    assert (copy.filename, copy.folder_id) == ("bracket (ScadBuddy).3mf", folder)
    [recorded] = await uploads.for_output(source.subject.run_subject, native=True)
    assert recorded.id == printed.id


async def test_a_native_run_needs_the_native_library(
    native: NativeLibrary, uploads: BambuddyUploadStore, three_mf: Path
) -> None:
    original = await upload(native, three_mf)
    source = await LibrarySource.load(
        None, original.id, uploads=uploads, settings=StoredSettings(), files=native, read_own=True
    )

    with pytest.raises(Exception, match="ScadBuddy's own library"):
        await source.file_to_print(
            None,
            printer_id=3,
            nozzle_size="0.4",
            plan=FilamentPlan(),
            project_id=None,
            native_copy=True,
            printer_model="Bambu Lab H2D",
        )


async def test_the_native_slicer_reads_the_copy_without_a_copy(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches
) -> None:
    """AC5: a slice of the native copy reads its own ``library-file-<id>`` blob where it
    is, named by the copy's sha256 (`slice_prepare`'s id-range rule, `reads_for`): no
    ``slice`` blob is put for the input, and Bambuddy is not asked."""
    copy_id = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )
    plan = SlicePlan(
        printer_preset=PresetRef(source="standard", id="Bambu Lab H2D 0.4 nozzle"),
        process_preset=PresetRef(source="standard", id="0.20mm Standard @BBL H2D"),
        filament_presets=[PresetRef(source="standard", id="Bambu PLA Basic @BBL H2D")],
        filament_colours=["#FF6AC1"],
        bed_type="Textured PEI Plate",
    )
    input = SliceWorkflowInput(
        slice_id=1,
        library_file_id=copy_id,
        plan=plan,
        plate_id=1,
        preset_key="k",
        slicer_queue="slicer",
    )

    key, sha = await SliceActivities._input(
        cast(SliceActivities, None), native.content, input, native, None
    )

    assert key == blob_key(copy_id)
    assert sha == (await native.library_file(copy_id)).file_hash
    assert await native.content.stat(f"slice-{sha}") is None


# --- #4312 review --------------------------------------------------------------------------


async def _copy_with_slice(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches, key: str
) -> tuple[int, int, str]:
    """A native copy recorded under ``key``, with a native sliced file recorded on it as
    `print_sliced_upload` and `print_record` leave one: the copy, the slice, its sha256."""
    copy_id = await ensure_native_copy(
        native, uploads, fetches.printable(key), target=H2D, folder_id=None
    )
    sliced = await native.upload("bracket.plate1.sliced.3mf", io.BytesIO(b"gcode"), None)
    await uploads.record_sliced(key, copy_id, SlicedCopy(id=sliced.id, preset_key="k"))
    return copy_id, sliced.id, sliced.sha256


async def test_a_recorded_native_slice_is_reused_without_bambuddy(
    native: NativeLibrary,
    uploads: BambuddyUploadStore,
    fetches: _Fetches,
    three_mf: Path,
    spans: InMemorySpanExporter,
) -> None:
    """I2: `recorded_slice` reads a native slice in the native library, with no client."""
    original = await upload(native, three_mf)
    source = await LibrarySource.load(
        None, original.id, uploads=uploads, settings=StoredSettings(), files=native, native=native
    )
    key = source.subject.run_subject
    copy_id, sliced_id, sha = await _copy_with_slice(native, uploads, fetches, key)
    await uploads.record_slice_hash(key, sliced_id, sha)

    with forbid_bambuddy(spans, BAMBUDDY):
        found = await source.recorded_slice(None, copy_id, "k")

    assert found == sliced_id


async def test_a_native_slices_hash_is_kept_without_bambuddy(
    native: NativeLibrary,
    uploads: BambuddyUploadStore,
    fetches: _Fetches,
    spans: InMemorySpanExporter,
) -> None:
    """I2: `print_record`'s `_keep_slice_hash` reads a native sliced file's sha256 in the
    native library, never asking the Bambuddy that is configured."""
    copy_id, sliced_id, sha = await _copy_with_slice(native, uploads, fetches, KEY)
    unused: Any = None
    store: Any = SimpleNamespace(load=lambda: StoredSettings(bambuddy_url=BAMBUDDY))
    activities = PrintActivities(
        PrintDeps(
            settings_store=store,
            outputs=unused,
            uploads=uploads,
            prints=unused,
            store=unused,
            observer=unused,
            library=native,
        )
    )
    outcome = QueueOutcome(
        native_slice_id=3, sliced_library_file_id=sliced_id, preset_key="k", printer_id=1
    )

    with forbid_bambuddy(spans, BAMBUDDY):
        await activities._keep_slice_hash(PrintSubject.output(KEY), outcome)

    [slice_row] = await uploads.slices_of(KEY, copy_id, "k")
    assert slice_row.file_hash == sha


async def test_the_id_spaces_split_at_native_id_base(uploads: BambuddyUploadStore) -> None:
    """M8: the SQL's boundary is `is_native_id`'s."""
    for file_id in (NATIVE_ID_BASE - 1, NATIVE_ID_BASE):
        await uploads.record(KEY, LibraryCopy(id=file_id, folder_id=None, target_key="t"))

    assert [c.id for c in await uploads.for_output(KEY)] == [NATIVE_ID_BASE - 1]
    assert [c.id for c in await uploads.for_output(KEY, native=True)] == [NATIVE_ID_BASE]
    assert not is_native_id(NATIVE_ID_BASE - 1) and is_native_id(NATIVE_ID_BASE)


async def test_the_archive_window_is_bambuddys_copies_only(uploads: BambuddyUploadStore) -> None:
    """M1: `sent_between` scans Bambuddy's archives, so a native copy, older than every
    Bambuddy one, never widens the window."""
    await uploads.record(KEY, LibraryCopy(id=NATIVE_ID_BASE + 5, folder_id=None, target_key="t"))
    await uploads.record_sliced(KEY, NATIVE_ID_BASE + 5, SlicedCopy(id=NATIVE_ID_BASE + 6))
    after_native = datetime.now(UTC)
    await uploads.record(KEY, LibraryCopy(id=41, folder_id=None, target_key="t"))
    await uploads.record_sliced(KEY, 41, SlicedCopy(id=77))
    before_native_slice = datetime.now(UTC)
    await uploads.record_sliced(KEY, NATIVE_ID_BASE + 5, SlicedCopy(id=NATIVE_ID_BASE + 7))

    window = await uploads.sent_between(KEY)

    assert window is not None
    first, last = window
    assert first >= after_native
    assert last <= before_native_slice


@pytest.mark.parametrize("kind", ["filed", "named"])
async def test_an_outputs_or_persons_file_is_never_taken_as_an_earlier_attempts(
    native: NativeLibrary, uploads: BambuddyUploadStore, fetches: _Fetches, kind: str
) -> None:
    """M2: only an unrecorded per-print copy (no output named, not filed) is an earlier
    attempt's upload: never the output's filed copy, nor a file a person's copy made
    named as an output (``output_id`` kept)."""
    laid_out = _laid_out_for(fetches.payload, H2D)
    theirs = await native.upload(
        "bracket.3mf", io.BytesIO(laid_out), None, output_id="out-x", filed=kind == "filed"
    )

    made = await ensure_native_copy(
        native, uploads, fetches.printable(), target=H2D, folder_id=None
    )

    assert made != theirs.id
