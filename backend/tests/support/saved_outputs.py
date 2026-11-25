"""An output saved with its bytes in files only, for tests that do not need a blob
store (#7256). And a saved output's 3MF rewritten in place, for tests that lay out its
plates after the save."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.library.output_rows import output_blob_key
from scadbuddy.library.outputs import MODEL_NAME, OutputMeta, OutputStore
from scadbuddy.render.job_models import Job


def save_files(store: OutputStore, job: Job, **kwargs: Any) -> OutputMeta:
    """Save the job's output with its bytes in files only, as `OutputStore.save` saves it
    with no blob store: `OutputStore.prepare`, then `OutputStore.commit` with no blobs.
    Blocking."""
    return store.commit(store.prepare(job, **kwargs), None)


def rewrite_model(client: TestClient, output_id: str, write: Callable[[Path], object]) -> None:
    """Let ``write`` rewrite the saved output's ``model.3mf`` in place. When the output's
    row names a model blob, the save put its bytes there and every reader reads them
    from it (#7256), so the new bytes go to that blob too, and what readers derived of
    the old bytes is dropped, as the byte copy does when the model key takes other bytes."""
    outputs: OutputStore = getattr(client.app.state, STATE_ATTR).outputs  # type: ignore[attr-defined]
    path = outputs.directory(output_id) / MODEL_NAME
    write(path)
    if outputs.model_sha(output_id) is None:
        return
    blobs, rows, portal = outputs.bytes, outputs.rows, client.portal
    assert blobs is not None and rows is not None and portal is not None
    slug = outputs.get(output_id).slug
    portal.call(lambda: blobs.put_file(output_blob_key(output_id, "model"), path, slug=slug))
    rows.drop_derived(output_id)
