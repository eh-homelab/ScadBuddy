"""#3630 — finding a library file by name in every folder, and the index it is read from."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.library_listing import (
    LibraryFolderView,
    list_library,
    search_library,
)
from scadbuddy.bambuddy.models import LibraryListRow
from scadbuddy.library_search.index import LibraryIndex
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"

FOLDERS = [
    LibraryFolderView(id=1, name="3D Name Keychains", parent_id=None, depth=0),
    LibraryFolderView(id=2, name="Work", parent_id=1, depth=1),
    LibraryFolderView(id=3, name="Toys & fidgets", parent_id=None, depth=0),
]


def row(file_id: int, filename: str, folder_id: int | None, file_type: str = "3mf") -> LibraryListRow:
    return LibraryListRow(id=file_id, filename=filename, file_type=file_type, folder_id=folder_id)


ROWS = [
    row(10, "Dinosaur_Keychain (ScadBuddy).3mf", 3),
    row(11, "Dinosaur_Keychain (ScadBuddy).gcode.3mf", 3, "gcode.3mf"),
    row(12, "plate_1.3mf", 2),
    row(13, "dragon-fidget.3mf", None),
]


def test_every_word_matches_the_name_or_the_folder_whatever_the_case_or_separator() -> None:
    found = search_library(FOLDERS, ROWS, "DINO keychain", show_all=False)

    assert [hit.id for hit in found.files] == [10]
    assert found.files[0].folder_path == ["Toys & fidgets"]
    # The sliced copy matches too, but only Advanced lists it.
    assert (found.total, found.hidden) == (1, 1)


def test_a_file_is_found_by_the_folders_it_is_in() -> None:
    found = search_library(FOLDERS, ROWS, "name keychains", show_all=False)

    assert [(hit.id, hit.folder_path) for hit in found.files] == [
        (12, ["3D Name Keychains", "Work"])
    ]


def test_a_match_in_the_name_comes_before_one_in_the_folder() -> None:
    found = search_library(FOLDERS, ROWS, "fidget", show_all=True)

    # dragon-fidget names it; the keychains are only in "Toys & fidgets".
    assert [hit.id for hit in found.files] == [13, 10, 11]
    assert found.files[2].sliced


def test_a_blank_query_finds_nothing_and_the_limit_cuts_the_list() -> None:
    assert search_library(FOLDERS, ROWS, "  _ ", show_all=True).files == []
    many = [row(100 + n, f"part-{n}.3mf", None) for n in range(150)]

    found = search_library(FOLDERS, many, "part", show_all=False, limit=100)

    assert (len(found.files), found.total) == (100, 150)


@respx.mock
async def test_the_listing_reads_the_tree_and_the_folder_at_once(bambuddy: BambuddyClient) -> None:
    """#3633: neither read waits on the other."""
    both = asyncio.Event()
    started = 0

    async def answer(json: object) -> httpx.Response:
        nonlocal started
        started += 1
        if started == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        return httpx.Response(200, json=json)

    respx.get(f"{API}/library/folders").mock(
        side_effect=lambda _: answer(recording("library-folders-nested.json"))
    )
    respx.get(f"{API}/library/files/").mock(
        side_effect=lambda _: answer(recording("library-files-folder.json"))
    )

    listing = await list_library(bambuddy, folder_id=4, show_all=True)

    assert listing.files and listing.folders


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@respx.mock
async def test_the_index_is_one_read_kept_fresh_then_answered_stale_while_it_is_read_again(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders-nested.json"))
    )
    files = respx.get(f"{API}/library/files/").mock(
        return_value=httpx.Response(200, json=recording("library-files-folder.json"))
    )

    @asynccontextmanager
    async def connect() -> AsyncIterator[BambuddyClient]:
        yield bambuddy

    clock = Clock()
    index = LibraryIndex(fresh=30, stale=600, clock=clock)

    first = await index.snapshot("bambuddy", connect)
    assert files.call_count == 1
    assert files.calls.last.request.url.params["include_root"] == "false"
    assert {folder.name for folder in first.folders} >= {"Supplies", "Storage"}

    clock.now += 10
    assert await index.snapshot("bambuddy", connect) is first
    assert files.call_count == 1

    # Stale: answered at once from what is kept, while one read replaces it.
    clock.now += 60
    assert await index.snapshot("bambuddy", connect) is first
    async with asyncio.timeout(5):
        while (await index.snapshot("bambuddy", connect)) is first:
            await asyncio.sleep(0.01)
    assert files.call_count == 2
    replaced = await index.snapshot("bambuddy", connect)
    assert replaced is not first

    # Fresh: a read that started after the request, even while one is kept.
    fresh = await index.snapshot("bambuddy", connect, fresh=True)
    assert fresh is not replaced
    assert files.call_count == 3

    # Past stale, a search waits for a new read.
    clock.now += 601
    await index.snapshot("bambuddy", connect)
    assert files.call_count == 4


async def test_a_failed_read_raises_to_the_search_that_waits_for_it() -> None:
    class Down(Exception):
        pass

    @asynccontextmanager
    async def connect() -> AsyncIterator[BambuddyClient]:
        raise Down
        yield  # pragma: no cover

    index = LibraryIndex()

    with pytest.raises(Down):
        await index.snapshot("bambuddy", connect)
