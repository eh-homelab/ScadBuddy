"""The library write calls the mount needs (#2686): partial updates, 0 clears."""

import json

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import FileUpdate, FolderUpdate
from scadbuddy.core.problems import ApiError

from .conftest import BASE_URL

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_update_library_file_sends_only_the_fields_given(bambuddy: BambuddyClient) -> None:
    route = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "b.3mf", "folder_id": 3})
    )
    found = await bambuddy.update_library_file(7, FileUpdate(filename="b.3mf"))
    assert json.loads(route.calls.last.request.content) == {"filename": "b.3mf"}
    assert found.filename == "b.3mf"


@respx.mock
async def test_moving_a_file_to_the_top_level_sends_zero(bambuddy: BambuddyClient) -> None:
    route = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "a.3mf"})
    )
    await bambuddy.update_library_file(7, FileUpdate(folder_id=0))
    assert json.loads(route.calls.last.request.content) == {"folder_id": 0}


@respx.mock
async def test_update_folder_and_delete_folder(bambuddy: BambuddyClient) -> None:
    put = respx.put(f"{API}/library/folders/4").mock(
        return_value=httpx.Response(200, json={"id": 4, "name": "New", "parent_id": None})
    )
    gone = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))
    folder = await bambuddy.update_folder(4, FolderUpdate(name="New"))
    await bambuddy.delete_folder(4)
    assert json.loads(put.calls.last.request.content) == {"name": "New"}
    assert folder.name == "New"
    assert gone.called


@pytest.mark.parametrize("status", [403, 404])
@respx.mock
async def test_delete_folder_maps_errors(bambuddy: BambuddyClient, status: int) -> None:
    respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(ApiError) as raised:
        await bambuddy.delete_folder(4)
    assert raised.value.status in {404, 409, 502}
