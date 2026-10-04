"""`python -m scadbuddy.worker --queue bambuddy` (#1060, spec 2026-10-01 §5.5): the
``scadbuddy-print`` worker, on Postgres and the API's internal routes only, versioned
and drained like the render worker."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from scadbuddy.core.settings import Settings
from scadbuddy.operations.store import Operation
from scadbuddy.worker import ApiUrlMissingError, build_print_deps, parse_queue, run_print_worker
from scadbuddy.workflows.client import PRINT_DEPLOYMENT_NAME, is_current
from scadbuddy.workflows.operation import OperationWorkflow
from scadbuddy.workflows.operation_models import (
    FinishOp,
    InsertOp,
    OperationInput,
    PreludeStep,
)
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.support.temporal import temporal_client
from tests.test_worker import _free_port, _healthy

NOW = datetime(2026, 10, 4, tzinfo=UTC)


def print_settings(pg_conninfo: str, tmp_path: Path, **fields: Any) -> Settings:
    return Settings(
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        # No volume: nothing may create it.
        data_dir=tmp_path / "no-volume",
        api_internal_url="http://127.0.0.1:9",
        **fields,
    )


def test_the_queue_flag_picks_the_worker() -> None:
    assert parse_queue([]) == "render"
    assert parse_queue(["--queue", "bambuddy"]) == "bambuddy"
    with pytest.raises(SystemExit):
        parse_queue(["--queue", "library"])


def test_the_print_worker_needs_the_apis_internal_url(tmp_path: Path) -> None:
    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        data_dir=tmp_path,
    )
    with pytest.raises(ApiUrlMissingError, match="SCADBUDDY_API_INTERNAL_URL"):
        build_print_deps(settings)


@pytest.mark.requires_postgres
async def test_the_print_worker_mounts_no_volume(pg_conninfo: str, tmp_path: Path) -> None:
    settings = print_settings(pg_conninfo, tmp_path)
    deps = await asyncio.to_thread(build_print_deps, settings)
    try:
        names = {getattr(fn, "__temporal_activity_definition").name for fn in deps.activities}
        assert {"print_check", "print_record", "op.send.run", "op.output_inbox_delete.run"} <= names
        # Only the Bambuddy kinds: a library kind needs the volume.
        assert "op.output_delete.run" not in names
    finally:
        await deps.aclose()
    assert not settings.data_dir.exists()


class Library:
    """The library side of an output delete: the record and an unversioned worker."""

    def __init__(self) -> None:
        self.finished: list[FinishOp] = []

    @activity.defn(name="op.output_delete.check")
    async def check(self, request: dict[str, Any]) -> dict[str, Any]:
        return {}

    @activity.defn(name="op_insert")
    async def insert(self, input: InsertOp) -> Operation:
        return Operation(
            id="op-1", kind="output_delete", subject="s", status="running", created_at=NOW
        )

    @activity.defn(name="op.output_delete.run")
    async def run(self, input: Any) -> dict[str, Any]:
        raise AssertionError("the delete ran after a failed prelude")

    @activity.defn(name="op_finish")
    async def finish(self, input: FinishOp) -> Operation:
        self.finished.append(input)
        return Operation(
            id="op-1",
            kind="output_delete",
            subject="s",
            status="failed",
            error=input.error,
            created_at=NOW,
        )


@pytest.mark.requires_postgres
@pytest.mark.requires_temporal
async def test_the_print_worker_is_current_serves_a_prelude_and_stops(
    pg_conninfo: str, tmp_path: Path
) -> None:
    """Made current as ``scadbuddy-print``, it serves the inbox step of a delete started
    on an unversioned (library) queue, and it stops on ``stop``."""
    queue = f"p-{uuid.uuid4().hex[:8]}"
    library_queue = f"l-{uuid.uuid4().hex[:8]}"
    build_id = f"test-{uuid.uuid4().hex[:8]}"
    settings = print_settings(
        pg_conninfo, tmp_path, temporal_task_queue_bambuddy=queue, revision=build_id
    )
    port = _free_port()
    library = Library()
    async with temporal_client() as client:
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_print_worker(settings, stop=stop, health_port=port, client=client)
        )
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                health = await _healthy(http, worker)
            assert health == {"ok": True, "build_id": build_id, "task_queue": queue}
            await _until_current(client, build_id)
            async with Worker(
                client,
                task_queue=library_queue,
                workflows=[OperationWorkflow],
                activities=[library.check, library.insert, library.run, library.finish],
            ):
                done = await _delete_with_prelude(client, library_queue, queue)
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=60)

    # The prelude ran on the print worker: the API it reads outputs through is not
    # there, so the step failed there and the delete never ran.
    assert done.status == "failed"
    [finish] = library.finished
    assert finish.error is not None


async def _until_current(client: Client, build_id: str) -> None:
    for _ in range(300):
        try:
            if await is_current(
                client,
                namespace=client.namespace,
                build_id=build_id,
                deployment_name=PRINT_DEPLOYMENT_NAME,
            ):
                return
        except Exception:
            pass
        await asyncio.sleep(0.2)
    raise AssertionError("the print worker never became current")


async def _delete_with_prelude(client: Client, library_queue: str, queue: str) -> Operation:
    key = uuid.uuid4().hex
    arg = OperationInput(
        kind="output_delete",
        subject="s",
        key=key,
        request={"output_id": "a" * 32, "delete_inbox_copies": True},
        prelude=PreludeStep(kind="output_inbox_delete", task_queue=queue),
    )
    handle = await client.start_workflow(
        "Operation",
        arg,
        id=f"op-output_delete-{key}",
        task_queue=library_queue,
        result_type=Operation,
    )
    done: Operation = await asyncio.wait_for(handle.result(), 120)
    return done
