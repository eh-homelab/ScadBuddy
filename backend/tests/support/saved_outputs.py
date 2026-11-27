"""A saved output's 3MF rewritten in place, for tests that lay out its plates after the
save."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.library.output_rows import OUTPUT_HOLDER, Role, output_blob_key, plate_cover_prefix
from scadbuddy.library.outputs import MODEL_NAME, OutputStore


async def rewrite_stored_model(
    outputs: OutputStore, output_id: str, write: Callable[[Path], object]
) -> None:
    """Let ``write`` rewrite the saved output's ``model.3mf``. Every reader reads it from
    its model blob (#7256, #7257), so the new bytes go to that blob, and what readers
    derived of the old bytes is dropped."""
    blobs = outputs.bytes
    assert blobs is not None
    data = await outputs.model_3mf(output_id)
    assert data is not None
    slug = outputs.get(output_id).slug
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / MODEL_NAME
        path.write_bytes(data)
        write(path)
        await blobs.put_file(output_blob_key(output_id, "model"), path, slug=slug)
    drop_derived(outputs, output_id)


def drop_derived(outputs: OutputStore, output_id: str) -> None:
    """Delete what readers derived of the output and release its plate cover's hold, in
    one transaction, for when the model key took other bytes. It locks the row first, so
    a derive write that read the old bytes either commits before this deletes, or finds
    the new sha256 and writes nothing (`output_rows._lock_for_derive`). Only tests move a
    saved output's model key (#7887)."""
    with outputs.rows._pool.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM outputs WHERE output_id = %s FOR UPDATE", (output_id,))
        conn.execute("DELETE FROM output_derived WHERE output_id = %s", (output_id,))
        conn.execute(
            "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s"
            " AND starts_with(key, %s)",
            (OUTPUT_HOLDER, output_id, plate_cover_prefix(output_id)),
        )


def _outputs(client: TestClient) -> OutputStore:
    outputs: OutputStore = getattr(client.app.state, STATE_ATTR).outputs  # type: ignore[attr-defined]
    return outputs


def rewrite_model(client: TestClient, output_id: str, write: Callable[[Path], object]) -> None:
    """`rewrite_stored_model` on the app's store, from a test with no loop."""
    portal = client.portal
    assert portal is not None
    portal.call(rewrite_stored_model, _outputs(client), output_id, write)


def lose_model(client: TestClient, output_id: str) -> None:
    """Remove the saved output's model blob behind the index's back: a blob the store
    has lost (#7257, plan D9)."""
    blobs, portal = _outputs(client).bytes, client.portal
    assert blobs is not None and portal is not None

    async def lose() -> None:
        found = await blobs.content.stat(output_blob_key(output_id, "model"))
        assert found is not None
        await blobs.content.backend.remove(found.ref.backend_id)

    portal.call(lose)


def stored_bytes(client: TestClient, output_id: str, role: Role = "model") -> bytes:
    """The saved output's bytes of ``role``, from its blob: a save writes no folder
    (#7257)."""
    blobs, portal = _outputs(client).bytes, client.portal
    assert blobs is not None and portal is not None
    data: bytes = portal.call(blobs.read, output_blob_key(output_id, role))
    return data
