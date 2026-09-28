"""`BambuddyUploadStore` (#455): an output's uploads to Bambuddy's file library, and the
slices Bambuddy made of them, in Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from scadbuddy.bambuddy.uploads import (
    BambuddyUploadStore,
    DatabaseRequiredError,
    LibraryCopy,
    SlicedCopy,
)
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.pg_store import MIGRATIONS, PostgresJobStore

OUTPUT = "a" * 32
OTHER = "b" * 32


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data")


@pytest.fixture
def uploads(pg_conninfo: str, paths: DataPaths) -> Iterator[BambuddyUploadStore]:
    jobs = PostgresJobStore(pg_conninfo, paths, pool_size=2)
    jobs.open()
    try:
        yield BambuddyUploadStore(jobs.pool)
    finally:
        jobs.close()


def copy(file_id: int, folder_id: int | None = 2, target_key: str = "H2D") -> LibraryCopy:
    return LibraryCopy(id=file_id, folder_id=folder_id, target_key=target_key)


@pytest.mark.requires_postgres
def test_the_tables_are_created_on_a_fresh_database_and_reopening_changes_nothing(
    pg_conninfo: str, paths: DataPaths
) -> None:
    for _ in range(2):
        store = PostgresJobStore(pg_conninfo, paths, pool_size=2)
        store.open()
        store.close()
    with psycopg.connect(pg_conninfo) as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM scadbuddy_migrations ORDER BY id")]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema()"
            )
        }
    assert ids == [migration.id for migration in MIGRATIONS]
    assert "20260928T0720Z_output_bambuddy_uploads" in ids
    assert {"output_bambuddy_uploads", "output_bambuddy_slices"} <= tables


@pytest.mark.requires_postgres
async def test_uploads_list_in_upload_order_per_output(uploads: BambuddyUploadStore) -> None:
    await uploads.record(OUTPUT, copy(11, folder_id=9))
    await uploads.record(OUTPUT, copy(12, folder_id=None, target_key="A1"))
    await uploads.record(OTHER, copy(13))

    assert await uploads.for_output(OUTPUT) == [copy(11, folder_id=9), copy(12, None, "A1")]
    assert await uploads.for_output(OTHER) == [copy(13)]
    assert await uploads.for_output("c" * 32) == []


@pytest.mark.requires_postgres
async def test_recording_the_same_file_again_replaces_it(uploads: BambuddyUploadStore) -> None:
    await uploads.record(OUTPUT, copy(11, folder_id=2))
    await uploads.record(OUTPUT, copy(11, folder_id=9, target_key="A1"))

    assert await uploads.for_output(OUTPUT) == [copy(11, folder_id=9, target_key="A1")]


@pytest.mark.requires_postgres
async def test_slices_belong_to_their_source_and_record_once(uploads: BambuddyUploadStore) -> None:
    await uploads.record(OUTPUT, copy(11))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21, preset_key="1"))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21, preset_key="1"))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=22))

    [only] = await uploads.for_output(OUTPUT)
    assert only.sliced == [SlicedCopy(id=21, preset_key="1"), SlicedCopy(id=22)]


@pytest.mark.requires_postgres
async def test_a_slice_of_a_file_no_longer_recorded_is_dropped(
    uploads: BambuddyUploadStore,
) -> None:
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21))

    assert await uploads.for_output(OUTPUT) == []


@pytest.mark.requires_postgres
async def test_forgetting_a_file_forgets_its_slices(uploads: BambuddyUploadStore) -> None:
    await uploads.record(OUTPUT, copy(11))
    await uploads.record(OUTPUT, copy(12))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21))

    await uploads.forget(OUTPUT, 11)
    await uploads.record(OUTPUT, copy(11))

    assert await uploads.for_output(OUTPUT) == [copy(12), copy(11)]


@pytest.mark.requires_postgres
async def test_for_outputs_answers_every_output_in_one_read(uploads: BambuddyUploadStore) -> None:
    await uploads.record(OUTPUT, copy(11))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21))
    await uploads.record(OTHER, copy(12))

    listed = await uploads.for_outputs([OUTPUT, OTHER, "c" * 32])

    assert listed == {
        OUTPUT: [copy(11).model_copy(update={"sliced": [SlicedCopy(id=21)]})],
        OTHER: [copy(12)],
        "c" * 32: [],
    }


@pytest.mark.requires_postgres
async def test_deleting_an_output_deletes_only_its_rows(
    uploads: BambuddyUploadStore, pg_conninfo: str
) -> None:
    await uploads.record(OUTPUT, copy(11))
    await uploads.record_sliced(OUTPUT, 11, SlicedCopy(id=21))
    await uploads.record(OTHER, copy(12))

    await uploads.delete_outputs([OUTPUT])

    assert await uploads.for_output(OUTPUT) == []
    assert await uploads.for_output(OTHER) == [copy(12)]
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT count(*) FROM output_bambuddy_slices").fetchone()
    assert row is not None and row[0] == 0


async def test_without_a_database_every_use_says_so() -> None:
    uploads = BambuddyUploadStore(None)
    with pytest.raises(DatabaseRequiredError, match="SCADBUDDY_DATABASE_URL"):
        await uploads.for_output(OUTPUT)
    with pytest.raises(DatabaseRequiredError):
        await uploads.record(OUTPUT, copy(11))


@pytest.mark.requires_postgres
async def test_delete_outputs_takes_several_at_once(uploads: BambuddyUploadStore) -> None:
    await uploads.record(OUTPUT, copy(11))
    await uploads.record(OTHER, copy(12))
    await uploads.record("c" * 32, copy(13))

    await uploads.delete_outputs([OUTPUT, OTHER])

    assert await uploads.for_outputs([OUTPUT, OTHER, "c" * 32]) == {
        OUTPUT: [],
        OTHER: [],
        "c" * 32: [copy(13)],
    }
