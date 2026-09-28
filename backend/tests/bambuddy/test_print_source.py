"""The print source seam (#313): what the run reads from an output or a library file."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.print_source import (
    UNKNOWN_COLOUR,
    LibrarySource,
    OutputSource,
    PrintSource,
    printable,
)
from scadbuddy.core.problems import ApiError
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


def test_only_an_unsliced_3mf_is_printable() -> None:
    assert printable("3mf")
    assert not printable("gcode.3mf")
    assert not printable("stl")
    assert not printable(None)


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
    assert await source.used_slots(bambuddy, [1, 2]) == {1, 2}
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
async def test_a_plates_used_slots_are_the_ones_bambuddy_marks_used(
    bambuddy: BambuddyClient,
) -> None:
    _file(67, "3mf")
    respx.get(f"{API}/library/files/67/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-multi.json"))
    )
    both = recording("filament-requirements.json")
    second_unused = {
        **both,
        "plate_id": 1,
        "filaments": [both["filaments"][0], {**both["filaments"][1], "used_in_plate": False}],
    }
    respx.get(f"{API}/library/files/67/filament-requirements", params={"plate_id": 1}).mock(
        return_value=httpx.Response(200, json=second_unused)
    )
    respx.get(f"{API}/library/files/67/filament-requirements").mock(
        return_value=httpx.Response(200, json=both)
    )

    source = await LibrarySource.load(bambuddy, 67)

    assert await source.used_slots(bambuddy, [1]) == {1}
    assert await source.used_slots(bambuddy, [1, 2]) == {1, 2}


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
    assert await source.used_slots(bambuddy, [1]) == {1}


@respx.mock
@pytest.mark.parametrize("file_type", ["gcode.3mf", "stl"])
async def test_a_file_the_dialog_cannot_print_is_a_422(
    bambuddy: BambuddyClient, file_type: str
) -> None:
    _file(104, file_type)

    with pytest.raises(ApiError) as refused:
        await LibrarySource.load(bambuddy, 104)

    assert refused.value.status == 422
    assert f"f104.{file_type}" in refused.value.detail


@respx.mock
async def test_a_file_deleted_in_bambuddy_is_a_404(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )

    with pytest.raises(ApiError) as missing:
        await LibrarySource.load(bambuddy, 89)

    assert missing.value.status == 404
