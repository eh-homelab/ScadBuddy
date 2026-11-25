"""An output whose bytes are in the blob store, for the tests of each byte reader
(#7256, #7257): saved through `OutputStore.save`, its rows in Postgres and its bytes in a
local content store."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import OutputBlobs
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.jobs import Job
from scadbuddy.store.content import BlobScope, ContentStore
from tests.support.arrange import finished_job
from tests.support.row_outputs import output_store
from tests.support.store import store_pool


async def put_folder_at_keys(
    blobs: OutputBytes, directory: Path, output_id: str, slug: str
) -> OutputBlobs:
    """`OutputBytes.put_folder`, then each of the output's keys names its object: for a
    test that names the keys in the row with `OutputRows.write_byte_copy`. A save's keys
    take their objects in the row's transaction instead (`OutputRows.insert`)."""
    got = await blobs.put_folder(directory, output_id, slug)
    for item in got.staged:
        await blobs.content.link(item.key, item.ref, scope=BlobScope(slug=slug))
    await blobs.drop_staged(got)
    return dataclasses.replace(got, staged=())


@dataclass
class BlobOutput:
    store: OutputStore
    meta: OutputMeta
    content: ContentStore
    #: The rows' pool, for a test that writes a row by hand.
    pool: ConnectionPool[Connection[DictRow]]
    #: The finished render job, for a test that saves another output of it.
    job: Job

    @property
    def directory(self) -> Path:
        """The folder the save still writes (until #7257's task B4), which no reader
        reads: a test removes a file there to show it reads the blob."""
        return self.store.paths.output_dir(self.meta.slug, self.meta.id)


@contextlib.asynccontextmanager
async def blob_output(tmp_path: Path, pg_conninfo: str) -> AsyncIterator[BlobOutput]:
    """One saved output, its rows in Postgres and its bytes in a local content store."""
    with store_pool(pg_conninfo) as pool:
        paths, job, _ = await finished_job(tmp_path / "data")
        store = output_store(tmp_path, pool, paths=paths)
        assert store.bytes is not None
        content = store.bytes.content
        try:
            meta = await store.save(job, name="first")
            yield BlobOutput(store, meta, content, pool, job)
        finally:
            await content.aclose()
