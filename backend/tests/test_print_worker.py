"""`python -m scadbuddy.worker --queue bambuddy` (#1060, spec 2026-10-01 §5.5): the
``scadbuddy-print`` worker, on Postgres and the API's internal routes only, versioned
like the render worker, its workflows AUTO_UPGRADE."""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import Worker

from scadbuddy import worker as worker_module
from scadbuddy.bambuddy import output_reader
from scadbuddy.core.settings import Settings
from scadbuddy.operations.store import Operation
from scadbuddy.worker import (
    ApiUrlMissingError,
    build_print_deps,
    parse_queue,
    run_print_worker,
)
from scadbuddy.workflows import client as client_module
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
from tests.test_worker import _healthy

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


def test_no_print_workflow_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every workflow the versioned print worker registers is AUTO_UPGRADE, by its own
    definition or by the worker's default (#2066): the Deployment rolls with
    ``Recreate``, so a run pinned to the old build would have no poller once it stops."""
    registered: list[type] = []
    defaults: list[VersioningBehavior] = []

    def record(*_: Any, workflows: list[type], deployment_config: Any, **__: Any) -> None:
        registered.extend(workflows)
        defaults.append(deployment_config.default_versioning_behavior)

    monkeypatch.setattr(client_module, "Worker", record)
    client_module.bambuddy_worker(None, "q", [], build_id="b")  # type: ignore[arg-type]

    assert defaults == [VersioningBehavior.AUTO_UPGRADE]
    assert registered
    for cls in registered:
        own = workflow._Definition.must_from_class(cls).versioning_behavior
        assert own in (VersioningBehavior.UNSPECIFIED, VersioningBehavior.AUTO_UPGRADE), cls


def test_the_print_worker_shares_the_apps_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2014: the client's tracing interceptor makes workflow spans in every worker's
    sandbox, so the print worker passes OpenTelemetry through as the render worker
    does. The follow worker runs no workflows."""
    runners: list[Any] = []

    def record(*_: Any, **kwargs: Any) -> None:
        runners.append(kwargs.get("workflow_runner"))

    monkeypatch.setattr(client_module, "Worker", record)
    client_module.bambuddy_worker(None, "q", [])  # type: ignore[arg-type]
    [runner] = runners
    assert "opentelemetry" in runner.restrictions.passthrough_modules


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
    pg_conninfo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    health_socket: socket.socket,
) -> None:
    """Made current as ``scadbuddy-print``, it serves the inbox step of a delete started
    on an unversioned (library) queue, and it stops on ``stop``."""
    # The API is never there: no wait for it to come back.
    monkeypatch.setattr(output_reader, "RESTART_DELAYS", ())
    queue = f"p-{uuid.uuid4().hex[:8]}"
    library_queue = f"l-{uuid.uuid4().hex[:8]}"
    build_id = f"test-{uuid.uuid4().hex[:8]}"
    settings = print_settings(
        pg_conninfo, tmp_path, temporal_task_queue_bambuddy=queue, revision=build_id
    )
    port = health_socket.getsockname()[1]
    library = Library()
    async with temporal_client() as client:
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_print_worker(settings, stop=stop, health_port=health_socket, client=client)
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


@pytest.mark.requires_postgres
async def test_the_print_worker_never_drains(
    pg_conninfo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2544: its workflows are AUTO_UPGRADE, so the next build takes them over: it
    stops without waiting for its runs (``drain_timeout=None``)."""
    served: dict[str, Any] = {}

    async def serve(_client: Any, workers: Any, _stop: Any, **kwargs: Any) -> None:
        served.update(kwargs, workers=workers)

    monkeypatch.setattr(worker_module, "_serve_versioned", serve)
    monkeypatch.setattr(worker_module, "bambuddy_worker", lambda *_a, **_k: "bambuddy")
    monkeypatch.setattr(worker_module, "follow_worker", lambda *_a, **_k: "follow")
    settings = print_settings(pg_conninfo, tmp_path, revision="b1")
    await run_print_worker(settings, health_port=None, client=cast(Client, object()))
    assert served["drain_timeout"] is None
    assert served["deployment_name"] == PRINT_DEPLOYMENT_NAME
    assert served["workers"] == ["bambuddy", "follow"]


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


@pytest.mark.requires_postgres
async def test_the_print_workers_stores_write_activity(pg_conninfo: str, tmp_path: Path) -> None:
    deps = await asyncio.to_thread(build_print_deps, print_settings(pg_conninfo, tmp_path))
    try:
        assert deps.print_runs.activity is not None
        assert deps.operations.activity is not None
    finally:
        await deps.aclose()
