"""A file listing's held copy as the slice reads it (#6710 §7.3.1): `HeldFile` answers the
pinned row, plates and bytes, and `start_library_quote_preview` with it reads those bytes,
never the library's."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
import trimesh

from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders import bambuddy as orders_bambuddy
from scadbuddy.orders.storefront.held_file import HeldFile, PricedFile
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope
from tests.bambuddy.conftest import BASE_URL
from tests.conftest import PgPool
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

#: A Bambuddy-range id: the live file Bambuddy holds at it may be other bytes.
FILE_ID = 41


def _two_colours(tmp_path: Path) -> bytes:
    parts = [
        ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(10, 10, 4))),
        ColourPart(2, "Color 2", "#00FF00", trimesh.creation.box(extents=(4, 4, 8))),
    ]
    out = tmp_path / "keychain.3mf"
    write_bambu_3mf(parts, out, thumbnails=None, model_name="keychain")
    return out.read_bytes()


@pytest.fixture
async def content(tmp_path: Path, pool: PgPool) -> AsyncIterator[ContentStore]:
    store = local_content(tmp_path / "remote", pool)
    try:
        yield store
    finally:
        await store.aclose()


async def held_copy(content: ContentStore, payload: bytes) -> PricedFile:
    """The copy publish keeps (spec §5.2 step 1), and the file as the price names it."""
    sha256 = hashlib.sha256(payload).hexdigest()
    key = f"listing-file-{sha256}"
    await content.put("media", payload, name="keychain.3mf", scope=BlobScope(), key=key)
    return PricedFile(
        id=FILE_ID,
        library="bambuddy",
        filename="keychain.3mf",
        sha256=sha256,
        copy_key=key,
        file_type="3mf",
        size=len(payload),
        plates=[1],
        used=[0, 1],
    )


async def _bytes(held: HeldFile, file_id: int) -> bytes:
    async with aclosing(held.download_library_file(file_id)) as chunks:
        return b"".join([chunk async for chunk in chunks])


async def test_the_held_file_answers_the_pinned_row_plates_and_bytes(
    content: ContentStore, tmp_path: Path
) -> None:
    payload = _two_colours(tmp_path)
    file = await held_copy(content, payload)
    held = HeldFile(file, content)

    row = await held.library_file(FILE_ID)
    assert (row.id, row.filename, row.file_type, row.file_size) == (
        FILE_ID,
        "keychain.3mf",
        "3mf",
        len(payload),
    )
    assert row.file_hash == file.sha256
    assert [plate.index for plate in (await held.library_plates(FILE_ID)).plates] == [1]
    assert await _bytes(held, FILE_ID) == payload
    # Slot ids from 1, for a file whose bytes give none (an STL).
    needs = await held.filament_requirements(FILE_ID)
    assert [need.slot_id for need in needs.filaments] == [1, 2]
    # Another id is not this copy.
    with pytest.raises(ApiError) as other:
        await held.library_file(FILE_ID + 1)
    assert other.value.status == 404


async def test_a_held_copy_gone_from_the_store_is_a_404(
    content: ContentStore, tmp_path: Path
) -> None:
    file = await held_copy(content, _two_colours(tmp_path))
    await content.forget(file.copy_key)
    with pytest.raises(ApiError) as gone:
        await _bytes(HeldFile(file, content), FILE_ID)
    assert gone.value.status == 404


async def test_a_held_slice_reads_the_held_bytes_not_the_library(
    content: ContentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start_library_quote_preview` with ``held`` loads the source over the held copy
    (no `reads_for`: a native id with no native library would be its 404), reads the slots
    from its bytes, and gives the source no origin, so Bambuddy never slices the live file
    in place. Without ``held`` the library is read as today."""
    payload = _two_colours(tmp_path)
    file = await held_copy(content, payload)
    held = HeldFile(file, content)
    seen: dict[str, Any] = {}

    async def started(
        client: object, source: LibrarySource, *args: Any, **kwargs: Any
    ) -> list[Any]:
        seen["source"] = source
        seen["bytes"] = await source.fetched_3mf(client)  # type: ignore[arg-type]
        seen.update(kwargs)
        return []

    monkeypatch.setattr(orders_bambuddy, "start_preview_plates", started)
    settings = StoredSettings(bambuddy_url=BASE_URL, bambuddy_api_key="s3cret")
    request = PrintRunRequest.model_validate(
        {"printer_id": 1, "filament_plan": {"slots": []}, "choices": {"nozzles": [{"size": "0.4"}]}}
    )
    await orders_bambuddy.start_library_quote_preview(
        FILE_ID,
        settings,
        None,  # type: ignore[arg-type]
        request,
        lan=False,
        native_reads=None,
        held=held,
        subject="storefront_price:" + "a" * 64,
        slot_colours={1: "#123456"},
    )
    source = seen["source"]
    assert source.files is held and source.held
    assert (source.origin_id, source.version) == (None, file.sha256)
    assert seen["bytes"] == payload
    assert source.colours == ["#FF0000", "#00FF00"]
    assert seen["slot_colours"] == {1: "#123456"}
    assert "held" not in seen
