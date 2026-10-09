"""The print source seam (#313): what the run reads from an output or a library file,
and the one pipeline both go through (#1752)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar, cast
from xml.etree import ElementTree as ET

import httpx
import psycopg
import pytest
import respx
import trimesh

from scadbuddy.bambuddy import print_source, send
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import Folder, LibraryFile, Printer, SlotChoice, Spool
from scadbuddy.bambuddy.print_links import PrintSend
from scadbuddy.bambuddy.print_source import (
    UNKNOWN_COLOUR,
    LibrarySource,
    OutputSource,
    PrintSource,
    printable,
)
from scadbuddy.bambuddy.send import Target
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import ProjectTarget, SlicedCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import PlateSend
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.bambu3mf import (
    PROJECT_SETTINGS_NAME,
    ArchiveTooLargeError,
    PlateParts,
    layout_of,
    write_bambu_3mf,
    write_plates_3mf,
)
from scadbuddy.render.plate import DEFAULT_PLATE
from scadbuddy.render.split import ColourPart
from tests.bambuddy.conftest import BASE_URL, recording
from tests.bambuddy.test_ensure_copy import MemoryUploads

API = f"{BASE_URL}/api/v1"


class _Meta:
    id = "a" * 32
    slug = "name-keychain"
    colors: ClassVar[list[str]] = ["#FF0000", "#0000FF"]


def test_an_output_source_is_the_models_colors_and_slug(tmp_path: Path) -> None:
    unused: Any = object()
    meta: Any = _Meta()
    source: PrintSource = OutputSource(
        store=unused, uploads=unused, meta=meta, settings=StoredSettings()
    )

    assert source.colours == ["#FF0000", "#0000FF"]
    assert source.options_slug == "name-keychain"


def _file(file_id: int, file_type: str) -> None:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200, json={"id": file_id, "filename": f"f{file_id}.{file_type}", "file_type": file_type}
        )
    )


def test_only_an_unsliced_3mf_or_stl_is_printable() -> None:
    assert printable("3mf")
    assert not printable("gcode.3mf")
    assert printable("stl")
    assert not printable(None)
    assert printable("3MF")


@respx.mock
async def test_a_library_file_is_its_plates_and_its_filaments(bambuddy: BambuddyClient) -> None:
    _file(67, "3mf")
    respx.get(f"{API}/library/files/67/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-multi.json"))
    )
    respx.get(f"{API}/library/files/67/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )

    source: PrintSource = await LibrarySource.load(bambuddy, 67)

    assert await source.plate_ids(bambuddy) == [1, 2]
    assert source.colours == ["#0047BB", "#FF1493"]
    assert source.options_slug is None
    assert source.subject == PrintSubject.library(67)
    # Loading only reads: the file is downloaded when a print lays it out.
    assert {call.request.method for call in respx.calls} == {"GET"}
    assert not any(call.request.url.path.endswith("/download") for call in respx.calls)


@respx.mock
async def test_a_library_files_colors_drop_the_alpha_bambuddy_reads(
    bambuddy: BambuddyClient,
) -> None:
    """Bambuddy reads a file's colors as #RRGGBBAA; the dialog's swatches are #RRGGBB."""
    _file(67, "3mf")
    respx.get(f"{API}/library/files/67/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-multi.json"))
    )
    respx.get(f"{API}/library/files/67/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-rgba.json"))
    )

    source = await LibrarySource.load(bambuddy, 67)

    assert source.colours == ["#867A93"]


@respx.mock
async def test_a_file_with_no_plates_or_filaments_is_one_plate_one_filament(
    bambuddy: BambuddyClient,
) -> None:
    _file(70, "3mf")
    respx.get(f"{API}/library/files/70/plates").mock(
        return_value=httpx.Response(
            200, json={**recording("library-plates-stl.json"), "file_id": 70}
        )
    )
    respx.get(f"{API}/library/files/70/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )

    source = await LibrarySource.load(bambuddy, 70)

    assert await source.plate_ids(bambuddy) == [1]
    assert source.colours == [UNKNOWN_COLOUR]
    assert len(source.colours) == 1


@respx.mock
@pytest.mark.parametrize("file_type", ["gcode.3mf"])
async def test_a_file_the_dialog_cannot_print_is_a_422(
    bambuddy: BambuddyClient, file_type: str
) -> None:
    _file(104, file_type)

    with pytest.raises(ApiError) as refused:
        await LibrarySource.load(bambuddy, 104)

    assert refused.value.status == 422
    assert f"f104.{file_type}" in refused.value.detail


@respx.mock
async def test_an_uppercase_sliced_type_still_gets_the_sliced_message(
    bambuddy: BambuddyClient,
) -> None:
    _file(105, "GCODE.3MF")

    with pytest.raises(ApiError) as refused:
        await LibrarySource.load(bambuddy, 105)

    assert refused.value.status == 422
    assert "is sliced already" in refused.value.detail


@respx.mock
async def test_an_stl_is_one_plate_of_one_filament(bambuddy: BambuddyClient) -> None:
    _file(46, "stl")
    respx.get(f"{API}/library/files/46/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-stl.json"))
    )
    respx.get(f"{API}/library/files/46/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )

    source = await LibrarySource.load(bambuddy, 46)

    assert await source.plate_ids(bambuddy) == [1]
    assert len(source.colours) == 1


@respx.mock
async def test_a_file_deleted_in_bambuddy_is_a_404(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )

    with pytest.raises(ApiError) as missing:
        await LibrarySource.load(bambuddy, 89)

    assert missing.value.status == 404


class _Sends:
    """Records `record_sends` as the link store would take it."""

    available = True

    def __init__(self, error: Exception | None = None) -> None:
        self.recorded: list[tuple[PrintSubject, list[PrintSend]]] = []
        self.error = error

    async def record_sends(self, subject: PrintSubject, sends: list[PrintSend]) -> None:
        if self.error is not None:
            raise self.error
        self.recorded.append((subject, list(sends)))


class _Prints:
    """`OutputPrintStore`: where an output's own last print is kept (#1060)."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    def record(self, output_id: str, **fields: Any) -> None:
        self.sends.append({"output_id": output_id, **fields})


class _Uploads:
    def __init__(self) -> None:
        self.sliced: list[tuple[Any, ...]] = []

    async def record_sliced(self, *args: Any) -> None:
        self.sliced.append(args)


OUTCOME = QueueOutcome(
    slice_job_id=9, sliced_library_file_id=21, queue_item_ids=[51, 52], printer_id=2
)
SENT = [
    PrintSend(queue_item_id=51, plate_id=1, printer_id=2, project_id=7, slice_job_id=9),
    PrintSend(queue_item_id=52, plate_id=1, printer_id=2, project_id=7, slice_job_id=9),
]


async def test_an_output_print_records_its_send_by_subject() -> None:
    # #1750 (R1): both sources write one send record; an output's meta keeps its own too.
    sends, prints = _Sends(), _Prints()
    meta: Any = _Meta()
    source = OutputSource(
        store=None,  # type: ignore[arg-type]
        uploads=_Uploads(),  # type: ignore[arg-type]
        meta=meta,
        settings=StoredSettings(),
        prints=prints,  # type: ignore[arg-type]
        sends=sends,  # type: ignore[arg-type]
    )

    sent = await source.record(11, 1, OUTCOME, 7, [])

    assert source.subject == PrintSubject.output(meta.id)
    assert sends.recorded == [(PrintSubject.output(meta.id), SENT)]
    assert [send["queue_item_id"] for send in prints.sends] == [51, 52]
    assert sent == [
        PlateSend(plate_id=1, queue_item_id=51, slice_job_id=9),
        PlateSend(plate_id=1, queue_item_id=52, slice_job_id=9),
    ]


async def test_a_library_print_records_its_send_by_subject() -> None:
    # #1750 (R1, R2): a library file's print is recorded exactly as an output's is.
    sends, uploads = _Sends(), _Uploads()
    source = LibrarySource(41, ["#FF0000"], [1], uploads=uploads, sends=sends)  # type: ignore[arg-type]

    sent = await source.record(141, 1, OUTCOME, 7, [])

    assert source.subject == PrintSubject.library(41)
    assert sends.recorded == [(PrintSubject.library(41), SENT)]
    # The slice is recorded against the copy it was sliced from, as an output's (#1752).
    assert [args[:2] for args in uploads.sliced] == [("library:41", 141)]
    assert sent == [
        PlateSend(plate_id=1, queue_item_id=51, slice_job_id=9),
        PlateSend(plate_id=1, queue_item_id=52, slice_job_id=9),
    ]


async def test_a_send_record_that_fails_does_not_fail_the_queued_plate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The plate is on Bambuddy's queue: failing the run over its record would tell the
    # user it was not (#976).
    sends = _Sends(psycopg.OperationalError("connection refused"))
    source = LibrarySource(41, ["#FF0000"], [1], uploads=_Uploads(), sends=sends)  # type: ignore[arg-type]

    sent = await source.record(41, 1, OUTCOME, None, [])

    assert [s.queue_item_id for s in sent] == [51, 52]
    assert "OperationalError" in caplog.text
    assert "connection refused" not in caplog.text


# --- #1752: one pipeline; a library file differs only in how its 3MF is obtained -------

FOLDER, INBOX, ORIGIN = 30, 2, 41
SPOOL_RGBA = "00C000FF"
HIGH_FLOW = ["High Flow", "High Flow"]
STATS = ["High Flow#1", "High Flow#0"]


class _Bambuddy:
    """What the pipeline reads of Bambuddy, in memory: the file to download, the
    printer it is laid out for, the spool chosen, the project's folder, and each folder's
    files, which uploads add to."""

    def __init__(self, payload: bytes, *, listed: list[LibraryFile] | None = None) -> None:
        self.payload = payload
        self.downloads = 0
        self.files: list[LibraryFile] = list(listed or [])
        self.uploaded: list[tuple[str, bytes, int | None]] = []

    async def download_library_file(self, file_id: int) -> AsyncIterator[bytes]:
        assert file_id == ORIGIN
        self.downloads += 1
        yield self.payload

    async def printers(self) -> list[Printer]:
        return [Printer(id=1, name="H2C", model="H2C")]

    async def spools(self) -> list[Spool]:
        return [Spool(id=9, material="PLA", rgba=SPOOL_RGBA)]

    async def folders_by_project(self, project_id: int) -> list[Folder]:
        return [Folder(id=FOLDER, name="Project", project_id=project_id)]

    async def library_files(self, folder_id: int) -> list[LibraryFile]:
        return [file for file in self.files if file.folder_id == folder_id]

    async def library_file(self, file_id: int) -> LibraryFile:
        return next(file for file in self.files if file.id == file_id)

    async def upload_library_file(
        self, filename: str, content: bytes, *, folder_id: int | None = None
    ) -> LibraryFile:
        self.uploaded.append((filename, content, folder_id))
        uploaded = LibraryFile(
            id=100 + len(self.uploaded),
            filename=filename,
            folder_id=folder_id,
            file_size=len(content),
            file_hash=hashlib.sha256(content).hexdigest(),
        )
        self.files.append(uploaded)
        return uploaded


class _Copies(MemoryUploads):
    """The uploads store in memory: copies, slices and project targets."""

    def __init__(self) -> None:
        super().__init__()
        self.sliced: list[tuple[str, int, SlicedCopy]] = []
        self.targets: dict[int, ProjectTarget] = {}

    async def forget(self, output_id: str, library_file_id: int) -> None:
        self.copies[output_id] = [
            copy for copy in self.copies.get(output_id, []) if copy.id != library_file_id
        ]

    async def record_sliced(self, output_id: str, library_file_id: int, sliced: SlicedCopy) -> None:
        self.sliced.append((output_id, library_file_id, sliced))

    async def remember_project_target(self, project_id: int, target: ProjectTarget) -> None:
        self.targets[project_id] = target


def _scadbuddy_3mf(tmp_path: Path) -> bytes:
    out = tmp_path / "critter.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4)))],
        out,
        thumbnails=None,
        model_name="critter",
    )
    return out.read_bytes()


def _authors_3mf(tmp_path: Path) -> bytes:
    """A two-plate project its author saved in Bambu Studio: their placement, their
    plates, a real printer preset."""
    tray = ColourPart(1, "Tray", "#FF6AC1", trimesh.creation.box(extents=(30, 30, 4)))
    lid = ColourPart(2, "Lid", "#1F6FEB", trimesh.creation.box(extents=(12, 12, 2)))
    out = tmp_path / "maze.3mf"
    write_plates_3mf(
        [PlateParts((tray,), (1,)), PlateParts((lid,), (2,))],
        ["#FF6AC1", "#1F6FEB"],
        out,
        thumbnails=None,
        model_name="maze",
    )
    with zipfile.ZipFile(out) as archive:
        entries = [(name, archive.read(name)) for name in archive.namelist()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if name == PROJECT_SETTINGS_NAME:
                settings = json.loads(data)
                settings["printer_settings_id"] = "Bambu Lab H2C 0.4 nozzle"
                data = json.dumps(settings).encode()
            archive.writestr(name, data)
    return buffer.getvalue()


def _sliced_3mf() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr(PROJECT_SETTINGS_NAME, '{"filament_colour": ["#FFFFFF"]}')
        archive.writestr("Metadata/plate_1.gcode", "; sliced\n")
    return buffer.getvalue()


def _library(
    uploads: _Copies,
    *,
    filename: str = "critter.3mf",
    file_type: str = "3mf",
    colours: list[str] | None = None,
) -> LibrarySource:
    return LibrarySource(
        ORIGIN,
        colours or ["#FF6AC1"],
        [1],
        filename=filename,
        file_type=file_type,
        uploads=uploads,  # type: ignore[arg-type]
        settings=StoredSettings(library_folder_id=INBOX),
        stem=filename.rsplit(".", 1)[0],
    )


async def _print(
    source: LibrarySource, bambuddy: _Bambuddy, *, project_id: int | None = None
) -> print_source.PrintFile:
    return await source.file_to_print(
        cast(BambuddyClient, bambuddy),
        printer_id=1,
        nozzle_size="0.4",
        plan=FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=9)]),
        project_id=project_id,
        nozzle_stats=STATS,
        nozzle_volume_type=HIGH_FLOW,
    )


def _settings(payload: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings: dict[str, Any] = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    return settings


def _transform(payload: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        item = ET.fromstring(archive.read("3D/3dmodel.model")).find(".//{*}item")
    assert item is not None
    return item.get("transform")


async def test_a_scadbuddy_3mf_in_the_library_is_printed_as_an_output_is(tmp_path: Path) -> None:
    """B1-B4, R3: replated for the printer, its nozzle side, flow and spool colours
    stated, uploaded into the project's folder, its slice and project recorded."""
    payload = _scadbuddy_3mf(tmp_path)
    bambuddy, uploads = _Bambuddy(payload), _Copies()
    source = _library(uploads)
    client = cast(BambuddyClient, bambuddy)

    printed = await _print(source, bambuddy, project_id=5)
    sent = await source.record(printed.id, 1, OUTCOME, 5, [])
    await source.remember_project(5, printer_id=1, nozzle_size="0.4")

    [(filename, copy, folder)] = bambuddy.uploaded
    assert (printed.id, printed.folder_id, folder, filename) == (101, FOLDER, FOLDER, "critter.3mf")
    settings = _settings(copy)
    assert settings["nozzle_diameter"] == ["0.4"]
    assert settings["nozzle_volume_type"] == HIGH_FLOW
    assert settings["extruder_nozzle_stats"] == STATS
    assert settings["filament_colour"] == ["#00C000"]
    assert _transform(copy) != _transform(payload)  # re-placed on the H2C's plate
    assert await source.states_nozzles(client)
    assert [copy.id for copy in uploads.copies["library:41"]] == [101]
    assert uploads.sliced == [("library:41", 101, SlicedCopy(id=21))]
    assert [plate.queue_item_id for plate in sent] == [51, 52]
    assert uploads.targets == {5: ProjectTarget(printer_id=1, nozzle_diameter="0.4")}
    assert bambuddy.downloads == 1  # read once for the judgement and the copy

    # Printed again the same way, the copy is reused: nothing new is uploaded.
    again = _library(uploads)
    assert (await _print(again, bambuddy, project_id=5)).id == 101
    assert len(bambuddy.uploaded) == 1
    # The filament step reads that copy, showing the file's own colours (B7).
    read = await again.file_to_read(client)
    assert (read.id, read.own_colours) == (101, ["#FF6AC1"])


async def test_an_authors_multi_plate_layout_is_kept(tmp_path: Path) -> None:
    """The riskiest part of #1752: a file someone laid out keeps every plate and every
    placement; only its settings state the nozzles and the spools' colours."""
    payload = _authors_3mf(tmp_path)
    bambuddy, uploads = _Bambuddy(payload), _Copies()
    source = _library(uploads, filename="maze.3mf", colours=["#FF6AC1", "#1F6FEB"])

    printed = await _print(source, bambuddy)

    [(filename, copy, folder)] = bambuddy.uploaded
    assert (printed.id, folder, filename) == (101, INBOX, "maze (ScadBuddy).3mf")
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as before,
        zipfile.ZipFile(io.BytesIO(copy)) as after,
    ):
        assert after.namelist() == before.namelist()
        for name in before.namelist():
            if name != PROJECT_SETTINGS_NAME:
                assert after.read(name) == before.read(name), name
    settings = _settings(copy)
    assert settings["nozzle_volume_type"] == HIGH_FLOW
    assert settings["extruder_nozzle_stats"] == STATS
    # Slot 1 takes the spool; slot 2 has none chosen and keeps the file's own.
    assert settings["filament_colour"] == ["#00C000", "#1F6FEB"]
    assert settings["printer_settings_id"] == "Bambu Lab H2C 0.4 nozzle"


async def test_an_stl_is_wrapped_and_laid_out_like_a_render(tmp_path: Path) -> None:
    stl = trimesh.creation.box(extents=(20, 10, 5)).export(file_type="stl")
    bambuddy, uploads = _Bambuddy(stl), _Copies()
    source = _library(uploads, filename="bracket.stl", file_type="stl", colours=[UNKNOWN_COLOUR])

    await _print(source, bambuddy)

    [(filename, copy, _)] = bambuddy.uploaded
    assert filename == "bracket (ScadBuddy).3mf"
    assert layout_of(copy) == "scadbuddy"
    settings = _settings(copy)
    assert (settings["nozzle_diameter"], settings["filament_colour"]) == (["0.4"], ["#00C000"])
    assert await source.states_nozzles(cast(BambuddyClient, bambuddy))


async def test_a_file_nothing_can_be_stated_into_prints_as_it_is() -> None:
    """A sliced 3MF (or another slicer's) has nothing to lay out: in the inbox the file
    itself is sliced, and the run takes it as Standard. Into a project it is copied, as
    it is, so the project's page has it (#79)."""
    bambuddy, uploads = _Bambuddy(_sliced_3mf()), _Copies()
    source = _library(uploads)
    client = cast(BambuddyClient, bambuddy)

    assert not await source.states_nozzles(client)
    assert (await _print(source, bambuddy)).id == ORIGIN
    assert bambuddy.uploaded == []

    filed = await _print(_library(uploads), bambuddy, project_id=5)
    [(_, copy, folder)] = bambuddy.uploaded
    assert (filed.id, folder, copy) == (101, FOLDER, _sliced_3mf())


async def test_the_file_itself_is_never_taken_for_its_own_copy(tmp_path: Path) -> None:
    """The file printed sits in the project's folder already, under the name its copy
    would take and with the bytes it would have: it is still copied, never recorded as a
    copy of itself, so nothing ScadBuddy supersedes could ever delete it."""
    payload = _sliced_3mf()
    origin = LibraryFile(
        id=ORIGIN,
        filename="critter.3mf",
        folder_id=FOLDER,
        file_size=len(payload),
        file_hash=hashlib.sha256(payload).hexdigest(),
    )
    bambuddy, uploads = _Bambuddy(payload, listed=[origin]), _Copies()

    printed = await _print(_library(uploads), bambuddy, project_id=5)

    assert printed.id == 101
    assert ORIGIN not in {copy.id for copy in uploads.copies["library:41"]}


@respx.mock
async def test_a_library_file_over_the_download_cap_prints_as_it_is(
    bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library file is untrusted: one past the cap is never held whole, nor laid out.
    It prints as it is, and is warned of as Standard."""
    payload = _sliced_3mf()
    monkeypatch.setattr(print_source, "MAX_DOWNLOAD_BYTES", len(payload) - 1)
    download = respx.get(f"{API}/library/files/41/download").mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.get(f"{API}/projects/5/folders").mock(return_value=httpx.Response(200, json=[]))
    source = _library(_Copies())

    assert not await source.states_nozzles(bambuddy)
    printed = await source.file_to_print(
        bambuddy,
        printer_id=1,
        nozzle_size="0.4",
        plan=FilamentPlan(),
        project_id=None,
        nozzle_volume_type=HIGH_FLOW,
    )
    assert printed.id == ORIGIN
    assert download.call_count == 1  # read once, not again for the print


def test_a_rewrite_past_the_cap_falls_back_to_the_file_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should the rewrite still find the archive too large, the print goes on with the
    file as it is rather than a 500."""

    def too_large(*_: object, **__: object) -> bytes:
        raise ArchiveTooLargeError("the 3MF inflates past the cap")

    monkeypatch.setattr(send, "state_nozzles", too_large)
    payload = _authors_3mf(tmp_path)

    laid_out = send._laid_out_for(
        payload, Target(DEFAULT_PLATE, "0.4", nozzle_volume_type=tuple(HIGH_FLOW))
    )

    assert laid_out == payload


async def test_a_library_files_filament_step_reads_the_file_itself_until_a_copy_exists() -> None:
    """B7: read the way an output's is, from a recorded copy when one exists; before
    any, from the file itself, which Bambuddy has already, so nothing is uploaded."""
    bambuddy, uploads = _Bambuddy(_sliced_3mf()), _Copies()
    source = _library(uploads)

    read = await source.file_to_read(cast(BambuddyClient, bambuddy))
    already = await source.file_read_already(cast(BambuddyClient, bambuddy))

    assert (read.id, read.own_colours) == (ORIGIN, None)
    assert already == read
    assert bambuddy.uploaded == [] and bambuddy.downloads == 0
