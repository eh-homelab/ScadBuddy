"""A storefront item prints its held copy (#6710 §10.2): the print's check answers
``restore`` when the library no longer serves the pinned bytes and the order still holds
them, and the restore puts them into the library's "Storefront" folder, points the item at
the new file with an order event, and copies the file's remembered choices. A retry of the
same claim uploads nothing, and the next print stands without a restore."""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.held_print import STOREFRONT_FOLDER, NativeShelf, restore_held
from scadbuddy.orders.models import LibraryFileRef, Order, OrderItem
from scadbuddy.orders.printing import print_check
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.store import OrderStore
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeLibraryStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_quote import FakeOutputs
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

PINNED = b"the listing's pinned bytes"
SHA256 = hashlib.sha256(PINNED).hexdigest()
COPY_KEY = f"listing-file-{SHA256}"


class Served:
    """The library that serves: each file's bytes and the hash its row states (None for a
    Bambuddy file that states none)."""

    def __init__(self, files: dict[int, tuple[bytes, str | None]] | None = None) -> None:
        self.files = dict(files or {})
        self.downloads: list[int] = []

    async def exists(self, file_id: int) -> bool:
        return file_id in self.files

    async def colours(self, file_id: int) -> dict[int, str]:
        return {}

    async def file(self, file_id: int) -> LibraryFile | None:
        if file_id not in self.files:
            return None
        _, stated = self.files[file_id]
        return LibraryFile(id=file_id, filename="bracket.3mf", file_hash=stated)

    async def download(self, file_id: int) -> AsyncGenerator[bytes]:
        self.downloads.append(file_id)
        payload, _ = self.files[file_id]
        yield payload


@pytest.fixture
async def content(tmp_path: Path, pool: PgPool) -> AsyncIterator[ContentStore]:
    store = local_content(tmp_path / "content", pool)
    try:
        yield store
    finally:
        await store.aclose()


@pytest.fixture
async def held(content: ContentStore) -> str:
    """The listing's held copy, as publish keeps it (§5.2)."""
    await content.put("media", PINNED, name="bracket.3mf", scope=BlobScope(), key=COPY_KEY)
    return COPY_KEY


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def storefront_item(
    shop: Shop, file_id: int = 89, library: str = "bambuddy", *, pinned: bool = True
) -> tuple[Order, OrderItem]:
    """An accepted order with one accepted library file item: a storefront line's (the
    pinned sha256 and the held copy) or, without ``pinned``, a back-office add's."""
    order = shop.order()
    ref = LibraryFileRef.model_validate(
        {
            "id": file_id,
            "filename": "bracket.3mf",
            "library": library,
            **({"sha256": SHA256, "copy_key": COPY_KEY} if pinned else {}),
        }
    )
    item = shop.lifecycle.add_library_item(order.id, BY, ref, quantity=2)
    shop.quoted(order, item, price=800)
    shop.lifecycle.accept_order(order.id, BY)
    return order, shop.get_item(item)


async def check(
    shop: Shop,
    order: Order,
    item: OrderItem,
    library: Served,
    content: ContentStore | None,
    tmp_path: Path,
) -> dict[str, Any]:
    blobs = LocalBlobStore(tmp_path / "blobs")
    return await print_check(
        shop.rows, FakeOutputs(), blobs, order.id, item.id, library=library, content=content
    )


def refused_gone(error: pytest.ExceptionInfo[ApiError]) -> None:
    assert (error.value.status, error.value.type) == (409, PROBLEMS + "library_file_gone")


# -- the check (§10.2 step 1) -----------------------------------------------------------------


async def test_a_storefront_item_with_its_pinned_file_stands_as_today(
    shop: Shop, content: ContentStore, held: str, tmp_path: Path
) -> None:
    order, item = storefront_item(shop)
    library = Served({89: (PINNED, SHA256)})
    checked = await check(shop, order, item, library, content, tmp_path)
    assert checked["library_file_id"] == 89
    assert "restore" not in checked
    # The row states the hash: nothing is downloaded.
    assert library.downloads == []


async def test_a_bambuddy_file_stating_no_hash_is_read_for_its_sha256(
    shop: Shop, content: ContentStore, held: str, tmp_path: Path
) -> None:
    order, item = storefront_item(shop)
    same = Served({89: (PINNED, None)})
    assert "restore" not in await check(shop, order, item, same, content, tmp_path)
    assert same.downloads == [89]
    other = Served({89: (b"other bytes", None)})
    assert (await check(shop, order, item, other, content, tmp_path))["restore"] is True


async def test_a_gone_or_changed_file_answers_restore_never_changed(
    shop: Shop, content: ContentStore, held: str, tmp_path: Path
) -> None:
    order, item = storefront_item(shop)
    standing = await check(shop, order, item, Served({89: (PINNED, SHA256)}), content, tmp_path)
    gone = await check(shop, order, item, Served(), content, tmp_path)
    assert gone == {**standing, "restore": True}
    # Other bytes under the same id; the stated hash in either case.
    other = hashlib.sha256(b"other").hexdigest()
    for stated in (other, other.upper()):
        changed = await check(
            shop, order, item, Served({89: (b"other", stated)}), content, tmp_path
        )
        assert changed == {**standing, "restore": True}
    # The hash a Bambuddy row states in upper case is still the pinned one.
    upper = await check(
        shop, order, item, Served({89: (PINNED, SHA256.upper())}), content, tmp_path
    )
    assert "restore" not in upper


async def test_a_missing_held_copy_is_library_file_gone(
    shop: Shop, content: ContentStore, tmp_path: Path
) -> None:
    order, item = storefront_item(shop)
    for library in (Served(), Served({89: (b"other", None)})):
        with pytest.raises(ApiError) as refused:
            await check(shop, order, item, library, content, tmp_path)
        refused_gone(refused)
    # The pinned file still served needs no held copy.
    assert "restore" not in await check(
        shop, order, item, Served({89: (PINNED, SHA256)}), content, tmp_path
    )


async def test_a_back_office_item_keeps_todays_rule(
    shop: Shop, content: ContentStore, tmp_path: Path
) -> None:
    order, item = storefront_item(shop, pinned=False)
    library = Served({89: (b"whatever the library holds", "0" * 64)})
    checked = await check(shop, order, item, library, None, tmp_path)
    assert "restore" not in checked
    assert library.downloads == []
    with pytest.raises(ApiError) as refused:
        await check(shop, order, item, Served(), None, tmp_path)
    refused_gone(refused)


# -- the restore (§10.2 steps 2 to 5) --------------------------------------------------------


@pytest.fixture
def native(pool: PgPool, content: ContentStore, tmp_path: Path) -> NativeLibrary:
    spool = tmp_path / "spool"
    spool.mkdir()
    return NativeLibrary(NativeLibraryStore(pool), content, spool)


class CountingShelf(NativeShelf):
    """The native shelf, counting its uploads."""

    def __init__(self, native: NativeLibrary) -> None:
        super().__init__(native)
        self.uploads = 0

    async def upload(self, filename: str, data: BinaryIO, folder_id: int) -> int:
        self.uploads += 1
        return await super().upload(filename, data, folder_id)


class Choices:
    """The remembered choices' copy: what was asked, and a scope already holding some."""

    def __init__(self, held: set[int] | None = None) -> None:
        self.held = set(held or ())
        self.copied: list[tuple[int, int]] = []

    def copy_library_choices(self, from_id: int, to_id: int) -> bool:
        if to_id in self.held:
            return False
        self.held.add(to_id)
        self.copied.append((from_id, to_id))
        return True


GONE_ID = NATIVE_ID_BASE + 9_999_999


async def restore(
    shop: Shop,
    order: Order,
    item: OrderItem,
    content: ContentStore,
    shelf: NativeShelf,
    choices: Choices,
    checked_id: int = GONE_ID,
) -> int:
    return await restore_held(
        shop.lifecycle, content, shelf, choices, order.id, item.id, checked_id, BY
    )


async def test_the_restore_puts_the_held_copy_into_storefront_and_moves_the_item(
    shop: Shop, content: ContentStore, held: str, native: NativeLibrary, tmp_path: Path
) -> None:
    order, item = storefront_item(shop, GONE_ID, "native")
    shelf = CountingShelf(native)
    choices = Choices()
    new_id = await restore(shop, order, item, content, shelf, choices)
    assert shelf.uploads == 1
    made = await native.library_file(new_id)
    assert (made.filename, made.file_hash) == ("bracket.3mf", SHA256)
    folder = await native.folder(made.folder_id or 0)
    assert (folder.name, folder.parent_id) == (STOREFRONT_FOLDER, None)
    moved = shop.get_item(item)
    assert moved.library_file is not None
    assert moved.library_file.model_dump() == {
        "id": new_id,
        "filename": "bracket.3mf",
        "library": "native",
        "sha256": SHA256,
        "copy_key": COPY_KEY,
    }
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "item.library_file_restored"]
    assert event.detail == {
        "item_id": item.id,
        "from_library_file_id": GONE_ID,
        "library_file_id": new_id,
        "sha256": SHA256,
    }
    assert choices.copied == [(GONE_ID, new_id)]
    # The next print finds the file with the pinned sha256: no restore.
    served = Served({new_id: (PINNED, made.file_hash)})
    assert "restore" not in await check(shop, order, moved, served, content, tmp_path)


async def test_a_retry_of_the_claim_uploads_nothing(
    shop: Shop, content: ContentStore, held: str, native: NativeLibrary
) -> None:
    order, item = storefront_item(shop, GONE_ID, "native")
    shelf = CountingShelf(native)
    choices = Choices()
    first = await restore(shop, order, item, content, shelf, choices)
    # The same claim's retry reuses the check, so it names the old id: the item moved.
    assert await restore(shop, order, item, content, shelf, choices) == first
    assert shelf.uploads == 1
    events = [e for e in shop.rows.events(order.id) if e.kind == "item.library_file_restored"]
    assert len(events) == 1


async def test_a_retry_after_the_upload_finds_the_file_by_its_sha256(
    shop: Shop, content: ContentStore, held: str, native: NativeLibrary
) -> None:
    """A try that uploaded and stopped before it moved the item: its retry finds that
    file in "Storefront" and moves the item to it, uploading nothing."""
    order, item = storefront_item(shop, GONE_ID, "native")
    folder = await native.template_folder(STOREFRONT_FOLDER)
    other_folder = await native.create_folder("Elsewhere", None)
    shelf = CountingShelf(native)
    # Bytes of the same sha256 outside "Storefront" are not taken.
    elsewhere = await shelf.upload("bracket.3mf", _spooled(PINNED), other_folder.id)
    uploaded = await shelf.upload("bracket.3mf", _spooled(PINNED), folder.id)
    assert await restore(shop, order, item, content, shelf, Choices()) == uploaded
    assert shelf.uploads == 2
    assert uploaded != elsewhere


class RacingShelf(CountingShelf):
    """A shelf that yields between what it reads and what it writes, as Bambuddy's calls
    do: the folder looked for, then made when it was not there; an upload that takes a
    while. ``folder`` is the "Storefront" the library holds, shared by every restore."""

    def __init__(self, native: NativeLibrary) -> None:
        super().__init__(native)
        self.folder: int | None = None
        self.folders_made = 0

    async def storefront_folder(self) -> int:
        seen = self.folder
        await asyncio.sleep(0.05)
        if seen is not None:
            return seen
        self.folders_made += 1
        self.folder = (await self.native.create_folder(STOREFRONT_FOLDER, None)).id
        return self.folder

    async def upload(self, filename: str, data: BinaryIO, folder_id: int) -> int:
        await asyncio.sleep(0.05)
        return await super().upload(filename, data, folder_id)


async def test_racing_restores_make_one_folder_and_one_file(
    shop: Shop, content: ContentStore, held: str, native: NativeLibrary
) -> None:
    """Two prints of items with the same pinned bytes (other claims, other
    Idempotency-Keys) restore at once: one "Storefront" is made and one copy uploaded,
    and both items move to it."""
    first_order, first_item = storefront_item(shop, GONE_ID, "native")
    second_order, second_item = storefront_item(shop, GONE_ID, "native")
    shelf = RacingShelf(native)
    first, second = await asyncio.gather(
        restore(shop, first_order, first_item, content, shelf, Choices()),
        restore(shop, second_order, second_item, content, shelf, Choices()),
    )
    assert first == second
    assert (shelf.folders_made, shelf.uploads) == (1, 1)


async def test_the_choices_are_copied_to_an_empty_scope_only(
    shop: Shop, content: ContentStore, held: str, native: NativeLibrary
) -> None:
    order, item = storefront_item(shop, GONE_ID, "native")
    folder = await native.template_folder(STOREFRONT_FOLDER)
    shelf = CountingShelf(native)
    there = await shelf.upload("bracket.3mf", _spooled(PINNED), folder.id)
    choices = Choices(held={there})
    assert await restore(shop, order, item, content, shelf, choices) == there
    assert choices.copied == []


async def test_a_held_copy_lost_before_the_restore_is_library_file_gone(
    shop: Shop, content: ContentStore, native: NativeLibrary
) -> None:
    order, item = storefront_item(shop, GONE_ID, "native")
    with pytest.raises(ApiError) as refused:
        await restore(shop, order, item, content, CountingShelf(native), Choices())
    refused_gone(refused)
    assert shop.get_item(item).library_file == item.library_file


def _spooled(payload: bytes) -> BinaryIO:
    return io.BytesIO(payload)
