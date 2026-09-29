"""The two reads a library print adds (#313): the folder's files and a file's plates."""

from __future__ import annotations

import httpx
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_the_root_lists_without_a_folder(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/library/files/").mock(
        return_value=httpx.Response(200, json=recording("library-files-root.json"))
    )

    rows = await bambuddy.library_listing(folder_id=None)

    assert "folder_id" not in route.calls.last.request.url.params
    assert rows and all(row.folder_id is None for row in rows)
    assert {"3mf", "gcode.3mf"} <= {row.file_type for row in rows}


@respx.mock
async def test_a_folder_is_asked_for_by_id(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/library/files/").mock(
        return_value=httpx.Response(200, json=recording("library-files-folder.json"))
    )

    rows = await bambuddy.library_listing(folder_id=4)

    assert route.calls.last.request.url.params["folder_id"] == "4"
    assert {"3mf", "gcode.3mf", "stl"} <= {row.file_type for row in rows}


@respx.mock
async def test_plates_read_the_undeclared_shape(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/files/67/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-multi.json"))
    )
    respx.get(f"{API}/library/files/46/plates").mock(
        return_value=httpx.Response(200, json=recording("library-plates-stl.json"))
    )

    multi = await bambuddy.library_plates(67)
    stl = await bambuddy.library_plates(46)

    assert [plate.index for plate in multi.plates] == [1, 2]
    assert multi.is_multi_plate
    assert stl.plates == []
