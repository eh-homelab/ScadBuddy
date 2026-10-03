"""The manual verify script's cleanup (#684, #690): it must not leave test files in a
user's library."""

from __future__ import annotations

import httpx
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.store.verify_bambuddy import _cleanup
from tests.bambuddy.conftest import BASE_URL

API = f"{BASE_URL}/api/v1"


@respx.mock
async def test_one_failed_delete_does_not_keep_the_rest(bambuddy: BambuddyClient) -> None:
    first = respx.delete(f"{API}/library/files/1").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    second = respx.delete(f"{API}/library/files/2").mock(return_value=httpx.Response(204))
    failed = await _cleanup(bambuddy, [1, 2])
    assert first.called and second.called
    assert len(failed) == 1 and failed[0].startswith("1:")


@respx.mock
async def test_an_id_a_dedupe_returned_twice_is_deleted_once(bambuddy: BambuddyClient) -> None:
    """Bambuddy may answer a re-upload with the existing file's id."""
    route = respx.delete(f"{API}/library/files/2").mock(return_value=httpx.Response(204))
    assert await _cleanup(bambuddy, [2, 2]) == []
    assert route.call_count == 1
