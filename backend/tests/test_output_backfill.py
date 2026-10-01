"""#902: an output saved before manifests gets one by re-rendering it."""

from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.library.backfill import attach_backfills, choose_output
from scadbuddy.library.outputs import OUTPUT_HOLDER, OutputStore
from scadbuddy.render.job_models import Job, JobNotFoundError
from scadbuddy.store.refs import BlobRefs
from tests.support.arrange import finished_job
from tests.support.store import store_pool


def _jobs(*jobs: Job):  # type: ignore[no-untyped-def]
    by_id = {job.id: job for job in jobs}

    def read(job_id: str) -> Job:
        if job_id not in by_id:
            raise JobNotFoundError(job_id)
        return by_id[job_id]

    return read


def _holders(refs: BlobRefs, key: str) -> list[tuple[str, str]]:
    with refs._pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_kind, holder_id FROM blob_refs WHERE key = %s ORDER BY 1, 2", (key,)
        ).fetchall()
    return [(row["holder_kind"], row["holder_id"]) for row in rows]


async def _legacy_output(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An output saved before manifests, and the done re-render of it."""
    paths, job, written = await finished_job(tmp_path, job_id="rerender")
    store = OutputStore(paths)
    old = store.create(job.model_copy(update={"outputs": [], "id": "old"}), name="mine")
    assert store.manifest(old.id) == []
    return store, old, job, written


@pytest.mark.requires_postgres
async def test_a_finished_rerender_gives_the_output_its_manifest_and_holds(
    tmp_path: Path, pg_conninfo: str
) -> None:
    store, old, job, written = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        assert attach_backfills(store, refs, _jobs(job)) == 1
        key = written.manifest[0].part
        assert _holders(refs, key) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest
    assert store.backfill(old.id) is None
    kept = store.get(old.id)
    assert (kept.id, kept.name, kept.job_id) == (old.id, "mine", old.job_id)
    assert store.ids_for("demo") == [old.id]  # nothing duplicated


@pytest.mark.requires_postgres
async def test_attaching_again_after_a_crash_changes_nothing(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """A crash after the hold, before the manifest: the next pass finishes the job, and
    one after that finds nothing to do."""
    store, old, job, written = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    key = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        refs.add(key, OUTPUT_HOLDER, old.id)  # the hold the crashed pass took
        assert attach_backfills(store, refs, _jobs(job)) == 1
        assert attach_backfills(store, refs, _jobs(job)) == 0
        assert _holders(refs, key) == [(OUTPUT_HOLDER, old.id)]
    assert store.manifest(old.id) == written.manifest


@pytest.mark.requires_postgres
async def test_an_unfinished_rerender_is_left_for_the_next_pass(
    tmp_path: Path, pg_conninfo: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    running = job.model_copy(update={"state": "running", "result": None, "outputs": []})
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), _jobs(running)) == 0
    state = store.backfill(old.id)
    assert state is not None and state.job_id == job.id and state.error is None
    assert store.manifest(old.id) == []


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    ("failed", "message"),
    [
        ({"state": "failed", "error": "openscad exited with 1"}, "openscad exited with 1"),
        ({"state": "cancelled"}, "the re-render was cancelled"),
        (None, "the re-render is gone"),
    ],
)
async def test_a_rerender_that_did_not_finish_says_why_and_is_not_retried(
    tmp_path: Path, pg_conninfo: str, failed: dict[str, str] | None, message: str
) -> None:
    store, old, job, _ = await _legacy_output(tmp_path)
    store.start_backfill(old.id, job.id)
    jobs = (
        _jobs(job.model_copy(update={**failed, "result": None, "outputs": []}))
        if failed
        else _jobs()
    )
    with store_pool(pg_conninfo) as pool:
        assert attach_backfills(store, BlobRefs(pool), jobs) == 0
        assert attach_backfills(store, BlobRefs(pool), jobs) == 0
    state = store.backfill(old.id)
    assert state is not None and state.error is not None and message in state.error
    assert store.manifest(old.id) == []


async def test_the_output_is_matched_by_its_recorded_parts(tmp_path: Path) -> None:
    """A pipeline job writes several outputs; the one with the same Parts is this one."""
    _, job, written = await finished_job(tmp_path, job_id="j1")
    _, _, other = await finished_job(tmp_path / "b", width=20, job_id="j2")
    both = job.model_copy(update={"outputs": [other, written]})
    assert choose_output(both, written.record) == written
    assert choose_output(both, other.record) == other
    assert choose_output(both, None) is None  # no record: cannot tell which
    assert choose_output(job, None) == written  # one output: that one
