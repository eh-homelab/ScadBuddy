"""An output whose bytes are in the blob store, for the tests of each byte reader
(#7256): saved with its rows, then copied as the byte copy copies it."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import ByteWrite, OutputRows
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.jobs import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from tests.support.arrange import finished_job
from tests.support.saved_outputs import save_files
from tests.support.store import local_content, store_pool


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
        return self.store.directory(self.meta.id)


@contextlib.asynccontextmanager
async def blob_output(tmp_path: Path, pg_conninfo: str) -> AsyncIterator[BlobOutput]:
    """One saved output, its rows in Postgres and its bytes copied to a local content
    store. The NFS files stay; a test removes the ones it wants read from the blobs."""
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        with store_pool(pg_conninfo) as pool:
            content = local_content(tmp_path / "remote", pool)
            content.spool = tmp_path / "spool"
            try:
                paths, job, _ = await finished_job(tmp_path / "data")
                store = OutputStore(paths)
                store.rows = OutputRows(jobs.pool)
                store.bytes = OutputBytes(content)
                meta = save_files(store, job, name="first")
                blobs = await store.bytes.put_folder(store.directory(meta.id), meta.id, meta.slug)
                store.rows.write_byte_copy([ByteWrite(meta.id, blobs, "stamp", [])])
                yield BlobOutput(store, meta, content, jobs.pool, job)
            finally:
                await content.aclose()
    finally:
        jobs.close()
