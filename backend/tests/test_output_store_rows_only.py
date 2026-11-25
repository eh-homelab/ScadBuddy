"""`OutputStore` reads rows and blobs only (#7257, plan task B3): no folder lookup, no
file fallback. A lost blob is `OutputBytesLostError` (plan D9), and the asset sweep then
removes nothing."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library.assets import referenced_asset_ids
from scadbuddy.library.output_rows import output_blob_key
from scadbuddy.library.outputs import OutputNotFoundError
from tests.support.output_blobs import blob_output

pytestmark = pytest.mark.requires_postgres

UNKNOWN = "f" * 32


async def _lose(content: Any, key: str) -> None:
    found = await content.stat(key)
    assert found is not None
    await content.backend.remove(found.ref.backend_id)


async def test_get_of_id_without_row_is_not_found_and_reads_no_file(
    tmp_path: Path, pg_conninfo: str
) -> None:
    async with blob_output(tmp_path, pg_conninfo) as output:
        store = output.store
        # The share gone, and a plain file where the outputs root was: any read of it
        # would raise something other than `OutputNotFoundError`.
        outputs_root = store.paths.root / "outputs"
        shutil.rmtree(outputs_root)
        outputs_root.write_text("not a folder")
        with pytest.raises(OutputNotFoundError):
            store.get(UNKNOWN)
        with pytest.raises(OutputNotFoundError):
            store.name_and_slug(UNKNOWN)
        assert store.manifest(UNKNOWN) == []
        assert await store.model_3mf(UNKNOWN) is None
        assert await store.open_role(UNKNOWN, "model") is None
        # The saved output reads from its row and its blobs alone.
        assert store.get(output.meta.id).id == output.meta.id
        assert await store.model_3mf(output.meta.id)


async def test_ids_for_reads_rows(tmp_path: Path, pg_conninfo: str) -> None:
    async with blob_output(tmp_path, pg_conninfo) as output:
        store = output.store
        second = await store.save(output.job, name="second")
        shutil.rmtree(store.paths.root / "outputs")
        assert store.ids_for(output.meta.slug) == [output.meta.id, second.id]
        assert store.ids_for("other") == []


async def test_reading_nests(tmp_path: Path, pg_conninfo: str) -> None:
    """#7463: a nested `reading` of another output gives the outer one back on exit."""
    async with blob_output(tmp_path, pg_conninfo) as output:
        store = output.store
        second = await store.save(output.job, name="second")
        calls: list[str] = []
        get = store.rows.get

        def counted(output_id: str) -> Any:
            calls.append(output_id)
            return get(output_id)

        store.rows.get = counted  # type: ignore[method-assign]
        with store.reading(output.meta.id):
            with store.reading(second.id):
                assert store.get(second.id).name == "second"
            assert store.get(output.meta.id).name == "first"
            store.params(output.meta.id)
        assert calls == [output.meta.id, second.id]


async def test_model_3mf_of_lost_blob_raises(tmp_path: Path, pg_conninfo: str) -> None:
    from scadbuddy.library.outputs import OutputBytesLostError

    async with blob_output(tmp_path, pg_conninfo) as output:
        key = output_blob_key(output.meta.id, "model")
        await _lose(output.content, key)
        with pytest.raises(OutputBytesLostError) as raised:
            await output.store.model_3mf(output.meta.id)
        assert (raised.value.output_id, raised.value.role, raised.value.key) == (
            output.meta.id,
            "model",
            key,
        )


async def test_asset_sweep_skips_when_a_model_blob_is_lost(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """An output with no params has its 3MF scanned for asset ids; a lost model blob is
    a source that cannot be read, so the sweep removes nothing (`OSError`)."""
    async with blob_output(tmp_path, pg_conninfo) as output:
        store = output.store
        with output.pool.connection() as conn:
            conn.execute("UPDATE outputs SET params = '{}' WHERE output_id = %s", (output.meta.id,))
        await _lose(output.content, output_blob_key(output.meta.id, "model"))
        with pytest.raises(OSError):
            await referenced_asset_ids(store.paths, [], store)
