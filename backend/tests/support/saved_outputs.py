"""An output saved with its bytes in files only, for tests that do not need a blob
store (#7256)."""

from __future__ import annotations

from typing import Any

from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.job_models import Job


def save_files(store: OutputStore, job: Job, **kwargs: Any) -> OutputMeta:
    """Save the job's output with its bytes in files only, as `OutputStore.save` saves it
    with no blob store: `OutputStore.prepare`, then `OutputStore.commit` with no blobs.
    Blocking."""
    return store.commit(store.prepare(job, **kwargs), None)
