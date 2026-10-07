"""The print source seam (#313): what the run reads from an output or a library file."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.print_links import PrintSend
from scadbuddy.bambuddy.print_source import (
    UNKNOWN_COLOUR,
    LibrarySource,
    OutputSource,
    PrintSource,
    printable,
)
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import PlateSend
from scadbuddy.library.settings_store import StoredSettings
from tests.bambuddy.conftest import BASE_URL, recording

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
    assert (await source.file_to_read(bambuddy)).id == 67
    printed = await source.file_to_print(
        bambuddy,
        printer_id=1,
        nozzle_size="0.4",
        plan=None,  # type: ignore[arg-type]
        project_id=5,
    )
    assert (printed.id, printed.folder_id) == (67, None)
    # Nothing is uploaded, replated or recolored: every call was a read.
    assert {call.request.method for call in respx.calls} == {"GET"}


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


class _Outputs:
    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    def record_send(self, output_id: str, **fields: Any) -> None:
        self.sends.append({"output_id": output_id, **fields})


class _Uploads:
    async def record_sliced(self, *args: Any) -> None:
        return None


OUTCOME = QueueOutcome(
    slice_job_id=9, sliced_library_file_id=21, queue_item_ids=[51, 52], printer_id=2
)
SENT = [
    PrintSend(queue_item_id=51, plate_id=1, printer_id=2, project_id=7, slice_job_id=9),
    PrintSend(queue_item_id=52, plate_id=1, printer_id=2, project_id=7, slice_job_id=9),
]


async def test_an_output_print_records_its_send_by_subject() -> None:
    # #1750 (R1): both sources write one send record; an output's meta keeps its own too.
    sends, outputs = _Sends(), _Outputs()
    meta: Any = _Meta()
    source = OutputSource(
        store=outputs,  # type: ignore[arg-type]
        uploads=_Uploads(),  # type: ignore[arg-type]
        meta=meta,
        settings=StoredSettings(),
        sends=sends,  # type: ignore[arg-type]
    )

    sent = await source.record(11, 1, OUTCOME, 7, [])

    assert source.subject == PrintSubject.output(meta.id)
    assert sends.recorded == [(PrintSubject.output(meta.id), SENT)]
    assert [send["queue_item_id"] for send in outputs.sends] == [51, 52]
    assert sent == [
        PlateSend(plate_id=1, queue_item_id=51, slice_job_id=9),
        PlateSend(plate_id=1, queue_item_id=52, slice_job_id=9),
    ]


async def test_a_library_print_records_its_send_by_subject() -> None:
    # #1750 (R1, R2): a library file's print is recorded exactly as an output's is.
    sends = _Sends()
    source = LibrarySource(file_id=41, colours=["#FF0000"], plates=[1], sends=sends)  # type: ignore[arg-type]

    sent = await source.record(41, 1, OUTCOME, 7, [])

    assert source.subject == PrintSubject.library(41)
    assert sends.recorded == [(PrintSubject.library(41), SENT)]
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
    source = LibrarySource(file_id=41, colours=["#FF0000"], plates=[1], sends=sends)  # type: ignore[arg-type]

    sent = await source.record(41, 1, OUTCOME, None, [])

    assert [s.queue_item_id for s in sent] == [51, 52]
    assert "OperationalError" in caplog.text
    assert "connection refused" not in caplog.text
