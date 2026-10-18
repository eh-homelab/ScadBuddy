"""`SliceWorkflow` around `slice_plate` (plan 2026-10-16-native-slicer-rust-worker
Task 11): `slice_prepare` on the `library` queue reads the laid-out copy and builds the
presets, the fake slicer (`tests/support/fake_slicer.py`) stands in for the Rust worker
on the slicer queue, and `slice_record` holds the sliced blob and finishes the row."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import suppress
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import CancelledError

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.print_library.native import NativeLibrary, blob_key
from scadbuddy.slicing.component import SLICING, Slicing
from scadbuddy.slicing.models import SliceProgress
from scadbuddy.slicing.presets import native_preset_key
from scadbuddy.slicing.profiles import ProfileError
from scadbuddy.slicing.start import (
    slice_progress,
    start_native_slice,
    stop_lost_native_slices,
    sweep_native_slices,
)
from scadbuddy.slicing.store import HOLDER, NativeSlice
from scadbuddy.store.content import ContentStore, sweep_content
from scadbuddy.store.content_models import BlobScope
from scadbuddy.workflows.slicing import SliceWorkflow
from scadbuddy.workflows.slicing_models import SliceWorkflowInput
from tests.api.test_send import API, configure
from tests.slicing.conftest import PROFILES
from tests.support.fake_slicer import INTERNAL, SLICED, FakeSlicer, fake_slicer_worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

COPY = 41
#: Another copy of the same bytes.
OTHER_COPY = 42
#: A copy of other bytes, which no earlier slice answers for.
OTHER_BYTES_COPY = 43
#: A copy Bambuddy no longer has.
GONE_COPY = 44
#: The laid-out copy's bytes, distinctive enough to find in a history.
INPUT = b"PK\x03\x04 the laid-out copy " + os.urandom(64).hex().encode()
PLAN = SlicePlan(
    printer_preset=PresetRef(source="standard", id="Test Printer 0.4 nozzle"),
    process_preset=PresetRef(source="standard", id="leaf"),
    filament_presets=[
        PresetRef(source="cloud", id="GF_PLA"),
        PresetRef(source="standard", id="Test PETG"),
    ],
    filament_colours=["#FF0000", "#00FF00"],
    bed_type="Textured PEI Plate",
)
SUBJECT = "out-1"


@pytest.fixture
def kek_file(tmp_path: Path) -> Path:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(os.urandom(32)).decode())
    return path


def _slice_settings(settings: Settings, kek_file: Path, **extra: Any) -> Settings:
    return settings.model_copy(
        update={
            "secret_key_file": kek_file,
            "api_internal_url": INTERNAL,
            "slicer_backend": "native",
            "slicer_profiles_dir": PROFILES,
            **extra,
        }
    )


def _s3(s3: S3Settings) -> dict[str, Any]:
    return {
        "store_backend": "s3",
        "store_s3_endpoint": s3.endpoint,
        "store_s3_bucket": s3.bucket,
        "store_s3_prefix": s3.prefix,
        "store_s3_access_key_id": s3.access_key_id,
        "store_s3_secret_access_key": s3.secret_access_key,
    }


@pytest.fixture
def bambuddy() -> Iterator[respx.MockRouter]:
    """Bambuddy serving both copies with the same bytes; everything on loopback (moto's
    S3, which the fake reaches by its presigned URLs) passes through."""
    with respx.mock(assert_all_called=False) as router:
        router.route(host="127.0.0.1").pass_through()
        for file_id in (COPY, OTHER_COPY):
            router.get(f"{API}/library/files/{file_id}/download").mock(
                return_value=httpx.Response(200, content=INPUT)
            )
        router.get(f"{API}/library/files/{OTHER_BYTES_COPY}/download").mock(
            return_value=httpx.Response(200, content=INPUT + b" changed")
        )
        yield router


def _open(app_settings: Settings, *, bambuddy: bool = True) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings)) as client:
        if bambuddy:
            configure(client)
        yield client


@pytest.fixture
def slice_client(
    settings: Settings, s3: S3Settings, kek_file: Path, bambuddy: respx.MockRouter
) -> Iterator[TestClient]:
    yield from _open(_slice_settings(settings, kek_file, **_s3(s3)))


@pytest.fixture
def slicer(slice_client: TestClient, settings: Settings) -> Iterator[FakeSlicer]:
    with fake_slicer_worker(slice_client, settings.temporal_task_queue_slicer) as fake:
        yield fake


def _state(client: TestClient) -> Any:
    return getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]


def _slicing(client: TestClient) -> Slicing:
    slicing: Slicing = _state(client).components.get(SLICING)
    return slicing


def _content(client: TestClient) -> ContentStore:
    content = _state(client).store.content
    assert isinstance(content, ContentStore)
    return content


def _call[T](client: TestClient, fn: Callable[[], Awaitable[T]]) -> T:
    assert client.portal is not None
    result: T = client.portal.call(fn)
    return result


def _start(
    client: TestClient,
    library_file_id: int = COPY,
    *,
    plan: SlicePlan = PLAN,
    temporal: Any = None,
    copy_sha256: str | None = None,
) -> NativeSlice:
    slicing = _slicing(client)
    starter = slicing.starter
    assert starter is not None

    async def start() -> NativeSlice:
        settings = _state(client).settings_store.load()
        args: dict[str, Any] = {
            "library_file_id": library_file_id,
            "plan": plan,
            "plate_id": 1,
            "subject": SUBJECT,
            "copy_sha256": copy_sha256,
        }
        target = temporal or slicing.temporal()
        if not settings.bambuddy_url:
            return await start_native_slice(target, slicing.store, starter, client=None, **args)
        async with client_for(settings) as bambuddy:
            return await start_native_slice(target, slicing.store, starter, client=bambuddy, **args)

    return _call(client, start)


def _finished(client: TestClient, row: NativeSlice) -> NativeSlice:
    slicing = _slicing(client)

    async def wait() -> NativeSlice:
        with suppress(WorkflowFailureError):  # a cancelled slice ends cancelled
            await slicing.temporal().get_workflow_handle(row.workflow_id).result()
        found = await slicing.store.get(row.id, SUBJECT)
        assert found is not None
        return found

    return _call(client, wait)


def _slice(
    client: TestClient, library_file_id: int = COPY, *, plan: SlicePlan = PLAN
) -> NativeSlice:
    return _finished(client, _start(client, library_file_id, plan=plan))


def _holds(client: TestClient, row: NativeSlice) -> set[str]:
    with _slicing(client).refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (HOLDER, str(row.id)),
        ).fetchall()
    return {r["key"] for r in rows}


def _history_activities(client: TestClient, row: NativeSlice) -> list[str]:
    async def names() -> list[str]:
        history = (
            await _slicing(client).temporal().get_workflow_handle(row.workflow_id).fetch_history()
        )
        return [
            e.activity_task_scheduled_event_attributes.activity_type.name
            for e in history.events
            if e.HasField("activity_task_scheduled_event_attributes")
        ]

    return _call(client, names)


def test_a_slice_completes_and_holds_its_blob(slice_client: TestClient, slicer: FakeSlicer) -> None:
    row = _slice(slice_client)

    assert row.status == "completed", row.failure
    assert row.failure is None and row.finished_at is not None
    assert row.warnings == ["a warning"]
    assert row.input_sha256 == hashlib.sha256(INPUT).hexdigest()
    content = _content(slice_client)
    assert row.sliced_blob_key is not None
    stat = _call(slice_client, partial(content.stat, row.sliced_blob_key))
    assert stat is not None
    assert _call(slice_client, partial(content.read, stat.ref)) == SLICED

    (downloaded,) = slicer.downloads
    assert downloaded[0] == INPUT
    machine, process, *filaments = (json.loads(doc) for doc in downloaded[1:])
    assert machine["name"] == "Test Printer 0.4 nozzle" and process["name"] == "leaf"
    assert process["curr_bed_type"] == "Textured PEI Plate" and len(filaments) == 2
    presets = {f"slice-{hashlib.sha256(doc).hexdigest()}" for doc in downloaded[1:]}
    assert _holds(slice_client, row) == {
        row.sliced_blob_key,
        f"slice-{row.input_sha256}",
        *presets,
    }


def test_a_held_blob_survives_the_blob_sweep(slice_client: TestClient, slicer: FakeSlicer) -> None:
    row = _slice(slice_client)
    assert row.status == "completed" and row.sliced_blob_key is not None
    content, refs = _content(slice_client), _slicing(slice_client).refs
    unheld = _call(
        slice_client,
        partial(content.put, "slice", b"nobody holds me", name="x", scope=BlobScope()),
    )
    time.sleep(0.01)

    removed = _call(slice_client, partial(sweep_content, content, refs, grace=0))

    assert f"slice-{unheld.sha256}" in removed
    assert _call(slice_client, partial(content.stat, row.sliced_blob_key)) is not None
    assert _call(slice_client, partial(content.stat, f"slice-{row.input_sha256}")) is not None


def test_a_validation_failure_fails_once_with_the_stderr_tail(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    slicer.refuse_plate = True
    row = _slice(slice_client)
    assert row.status == "failed"
    assert row.failure == "The slicer refused the plate: plate 99 is empty"
    assert row.sliced_blob_key is None
    assert _holds(slice_client, row) == set()
    assert slicer.calls == 1


def test_a_crash_retries_three_times(slice_client: TestClient, slicer: FakeSlicer) -> None:
    slicer.crash_times = 3
    row = _slice(slice_client)
    assert row.status == "completed", row.failure
    assert slicer.calls == 4

    slicer.calls, slicer.crash_times = 0, 4
    failed = _slice(slice_client, OTHER_BYTES_COPY)
    assert failed.status == "failed"
    assert failed.failure == "The slicer failed: segfault"
    assert _holds(slice_client, failed) == set()
    assert slicer.calls == 4


def test_an_expired_url_prepares_again_once(slice_client: TestClient, slicer: FakeSlicer) -> None:
    slicer.expire_times = 1
    row = _slice(slice_client)
    assert row.status == "completed", row.failure
    assert slicer.calls == 2
    names = _history_activities(slice_client, row)
    assert names.count("slice_prepare") == 2
    assert names.count("slice_plate") == 2


def test_another_error_type_hides_its_message(slice_client: TestClient, slicer: FakeSlicer) -> None:
    slicer.worker_errors = 4
    row = _slice(slice_client)
    assert row.status == "failed"
    assert row.failure == "The slicer worker failed (SlicerWorkerError)."
    assert slicer.calls == 4


def test_the_same_bytes_and_presets_reuse_the_slice(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    first = _slice(slice_client)
    assert first.status == "completed", first.failure
    calls = slicer.calls

    second = _slice(slice_client, OTHER_COPY)

    assert second.id != first.id and second.workflow_id != first.workflow_id
    assert second.status == "completed", second.failure
    assert second.sliced_blob_key == first.sliced_blob_key
    assert second.warnings == first.warnings
    assert slicer.calls == calls
    assert first.sliced_blob_key in _holds(slice_client, second)


def test_a_started_slice_starts_once(slice_client: TestClient, slicer: FakeSlicer) -> None:
    """The dialog and the run name one execution (Ruling C2); a finished one is its row."""
    slicer.hold = _call(slice_client, _event)
    first = _start(slice_client)
    again = _start(slice_client)
    assert again.id == first.id and again.workflow_id == first.workflow_id
    _set(slice_client, slicer)
    done = _finished(slice_client, first)
    assert done.status == "completed", done.failure

    after = _start(slice_client)
    assert (after.id, after.status) == (first.id, "completed")
    assert slicer.calls == 1


def _set(client: TestClient, slicer: FakeSlicer) -> None:
    assert client.portal is not None and slicer.hold is not None
    client.portal.call(slicer.hold.set)


async def _event() -> asyncio.Event:
    return asyncio.Event()


def test_progress_reads_the_slice_plate_heartbeat(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    slicer.hold = _call(slice_client, _event)
    row = _start(slice_client)
    temporal: Client = _slicing(slice_client).temporal()
    seen: list[SliceProgress | None] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        progress = _call(slice_client, partial(slice_progress, temporal, row.workflow_id))
        seen.append(progress)
        if progress is not None and progress.total_percent == 40:
            break
        time.sleep(0.2)
    assert seen[-1] == SliceProgress(
        plate_index=0, plate_count=1, plate_percent=40, total_percent=40, message="Slicing"
    )
    assert all(p is None or p.total_percent in (0, 40) for p in seen)

    _set(slice_client, slicer)
    assert _finished(slice_client, row).status == "completed"
    assert _call(slice_client, partial(slice_progress, temporal, row.workflow_id)) is None


def _payloads(history_json: str) -> list[bytes]:
    return [base64.b64decode(data) for data in re.findall(r'"data":\s*"([^"]*)"', history_json)]


def test_no_bytes_or_presets_in_the_history(slice_client: TestClient, slicer: FakeSlicer) -> None:
    row = _slice(slice_client)
    assert row.status == "completed", row.failure

    async def history() -> str:
        handle = _slicing(slice_client).temporal().get_workflow_handle(row.workflow_id)
        return (await handle.fetch_history()).to_json()

    payloads = _payloads(_call(slice_client, history))
    assert payloads, "the history carries payloads"
    encoded = base64.b64encode(INPUT)
    for payload in payloads:
        assert INPUT not in payload and encoded not in payload
        assert SLICED not in payload
        assert b"layer_height" not in payload


@pytest.fixture
def native_slice_client(
    settings: Settings, s3: S3Settings, kek_file: Path, bambuddy: respx.MockRouter
) -> Iterator[TestClient]:
    yield from _open(_slice_settings(settings, kek_file, **_s3(s3), library_backend="native"))


def test_native_library_input_moves_no_bytes(
    native_slice_client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    client = native_slice_client
    library = _state(client).components.get(PRINT_LIBRARY)
    native: NativeLibrary = library.native
    source = tmp_path / "copy.3mf"
    source.write_bytes(INPUT)
    with source.open("rb") as data:
        uploaded = _call(client, partial(native.upload, source.name, data, None))

    with fake_slicer_worker(client, settings.temporal_task_queue_slicer) as slicer:
        row = _slice(client, uploaded.id)

    assert row.status == "completed", row.failure
    assert slicer.downloads[0][0] == INPUT
    sha = hashlib.sha256(INPUT).hexdigest()
    assert row.input_sha256 == sha
    assert blob_key(uploaded.id) in _holds(client, row)
    assert _call(client, partial(_content(client).stat, f"slice-{sha}")) is None


def test_on_the_local_store_the_slice_fails_saying_why(
    settings: Settings, kek_file: Path, bambuddy: respx.MockRouter
) -> None:
    for client in _open(_slice_settings(settings, kek_file)):
        row = _slice(client)
        assert row.status == "failed"
        assert row.failure is not None and "store_backend" in row.failure


# -- fix round 1: the row's edges -------------------------------------------------------


def _wait_for_slicing(client: TestClient, row: NativeSlice) -> None:
    """Until the fake has beaten: `slice_prepare` has held everything by then."""
    temporal = _slicing(client).temporal()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _call(client, partial(slice_progress, temporal, row.workflow_id)) is not None:
            return
        time.sleep(0.2)
    pytest.fail("the slice never reached slice_plate")


def _terminate(client: TestClient, row: NativeSlice) -> None:
    async def terminate() -> None:
        handle = _slicing(client).temporal().get_workflow_handle(row.workflow_id)
        await handle.terminate("test")

    _call(client, terminate)


def _sweep_lost(client: TestClient) -> list[int]:
    """The lost-row stop (on the prune Schedule), then the TTL sweep: the ids deleted."""
    slicing = _slicing(client)
    _call(
        client,
        partial(stop_lost_native_slices, slicing.temporal(), slicing.store, timedelta(0)),
    )
    return _call(
        client, partial(sweep_native_slices, slicing.store, finished_older_than=timedelta(hours=1))
    )


def _row(client: TestClient, row: NativeSlice) -> NativeSlice:
    found = _call(client, partial(_slicing(client).store.get, row.id, SUBJECT))
    assert found is not None
    return found


class _Down:
    """A Temporal that refuses every start, as one blipping or the API stopping does."""

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("temporal is down")


def test_a_start_that_never_reached_temporal_is_swept_failed(
    slice_client: TestClient,
) -> None:
    with pytest.raises(RuntimeError, match="temporal is down"):
        _start(slice_client, temporal=_Down())
    with _slicing(slice_client).store._pool.connection() as conn:
        row = NativeSlice.model_validate(
            conn.execute("SELECT * FROM native_slices ORDER BY id DESC LIMIT 1").fetchone()
        )
    assert row.status == "running"
    _slicing(slice_client).refs.add("slice-orphan", HOLDER, str(row.id))

    assert _sweep_lost(slice_client) == []

    swept = _row(slice_client, row)
    assert (swept.status, swept.failure) == ("failed", "The slice stopped.")
    assert _holds(slice_client, row) == set()


def test_a_terminated_slice_is_swept_failed(slice_client: TestClient, slicer: FakeSlicer) -> None:
    slicer.hold = _call(slice_client, _event)
    row = _start(slice_client)
    _wait_for_slicing(slice_client, row)
    assert _holds(slice_client, row)
    _terminate(slice_client, row)

    _sweep_lost(slice_client)

    swept = _row(slice_client, row)
    assert (swept.status, swept.failure) == ("failed", "The slice stopped.")
    assert _holds(slice_client, row) == set()
    _set(slice_client, slicer)


def test_a_start_joining_a_closing_slice_is_healed_by_the_lost_row_stop(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    """The record has written `failed` and the execution is still closing: a start
    joins it (USE_EXISTING). The row reads running at once (Task 12 review I1), with
    nothing behind it once the execution closes, so the lost-row stop fails it."""
    slicer.hold = _call(slice_client, _event)
    row = _start(slice_client)
    _wait_for_slicing(slice_client, row)
    store = _slicing(slice_client).store
    _call(
        slice_client,
        partial(
            store.finish, row.id, status="failed", sliced_blob_key=None, failure="x", warnings=[]
        ),
    )

    joined = _start(slice_client)

    assert (joined.id, joined.status) == (row.id, "running")
    _terminate(slice_client, row)
    _sweep_lost(slice_client)
    assert _row(slice_client, row).status == "failed"
    assert _holds(slice_client, row) == set()
    _set(slice_client, slicer)


def test_a_cancelled_slice_is_recorded_cancelled(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    slicer.hold = _call(slice_client, _event)
    row = _start(slice_client)
    _wait_for_slicing(slice_client, row)

    async def cancel() -> BaseException | None:
        handle = _slicing(slice_client).temporal().get_workflow_handle(row.workflow_id)
        await handle.cancel()
        try:
            await handle.result()
        except WorkflowFailureError as error:
            return error.cause
        return None

    cause = _call(slice_client, cancel)

    assert isinstance(cause, CancelledError)
    done = _row(slice_client, row)
    assert (done.status, done.failure) == ("cancelled", "The slice was cancelled.")
    assert _holds(slice_client, row) == set()
    _set(slice_client, slicer)


def test_no_slicer_worker_fails_the_slice_saying_so(slice_client: TestClient) -> None:
    slicing = _slicing(slice_client)
    starter = slicing.starter
    assert starter is not None

    async def start() -> NativeSlice:
        row = await slicing.store.start(
            workflow_id="slice-unserved",
            subject=SUBJECT,
            library_file_id=COPY,
            plate_id=1,
            preset_key="k",
        )
        await slicing.temporal().start_workflow(
            SliceWorkflow.run,
            SliceWorkflowInput(
                slice_id=row.id,
                library_file_id=COPY,
                plan=PLAN,
                plate_id=1,
                # What the start keys PLAN by, or the drift check refuses it first.
                preset_key=native_preset_key(PLAN, 1, starter.profiles.version),
                slicer_queue=f"{starter.slicer_queue}-nobody",
                slicer_wait=timedelta(seconds=2),
            ),
            id=row.workflow_id,
            task_queue=starter.library_queue,
        )
        return row

    row = _finished(slice_client, _call(slice_client, start))

    assert row.status == "failed"
    assert row.failure is not None and row.failure.startswith("No slicer worker took the slice")
    assert _holds(slice_client, row) == set()


def test_a_second_refused_url_fails(slice_client: TestClient, slicer: FakeSlicer) -> None:
    slicer.expire_times = 2
    row = _slice(slice_client)
    assert row.status == "failed"
    assert row.failure == "The slicer worker failed (TransferRefused)."
    names = _history_activities(slice_client, row)
    assert names.count("slice_prepare") == 2 and names.count("slice_plate") == 2
    assert slicer.calls == 2


LOCAL_PLAN = PLAN.model_copy(update={"process_preset": PresetRef(source="local", id="4")})


def test_a_process_preset_changed_since_the_start_is_refused(
    slice_client: TestClient, bambuddy: respx.MockRouter, slicer: FakeSlicer
) -> None:
    bambuddy.get(f"{API}/slicer/presets").respond(200, json={})
    bambuddy.get(f"{API}/local-presets/").respond(200, json={})
    bambuddy.get(f"{API}/slicer/preset-values").mock(
        side_effect=[
            httpx.Response(200, json={"resolved": True, "values": {"layer_height": h}})
            for h in ("0.12", "0.20")
        ]
    )
    row = _slice(slice_client, plan=LOCAL_PLAN)
    assert row.status == "failed"
    assert row.failure == "The process preset changed while the slice started: slice again."
    assert slicer.calls == 0


def test_a_failed_slice_started_again_completes(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    slicer.refuse_plate = True
    assert _slice(slice_client).status == "failed"
    slicer.refuse_plate = False

    again = _slice(slice_client)

    assert (again.status, again.failure) == ("completed", None)
    assert slicer.calls == 2


def test_a_lost_put_fails_without_internal_keys(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    slicer.wrong_sha = True
    row = _slice(slice_client)
    assert (row.status, row.failure) == ("failed", "The sliced 3MF was lost; slice again.")


def test_a_gone_bambuddy_copy_says_so(
    slice_client: TestClient, bambuddy: respx.MockRouter, slicer: FakeSlicer
) -> None:
    bambuddy.get(f"{API}/library/files/{GONE_COPY}/download").respond(
        404, json={"detail": "File not found"}
    )
    row = _slice(slice_client, GONE_COPY)
    assert row.status == "failed" and row.failure is not None
    assert not row.failure.startswith("Preparing the slice failed")
    assert str(GONE_COPY) in row.failure


@pytest.fixture
def native_alone_client(
    settings: Settings, s3: S3Settings, kek_file: Path, bambuddy: respx.MockRouter
) -> Iterator[TestClient]:
    """The native library with no Bambuddy configured at all."""
    yield from _open(
        _slice_settings(settings, kek_file, **_s3(s3), library_backend="native"), bambuddy=False
    )


def _native_upload(client: TestClient, tmp_path: Path) -> int:
    native: NativeLibrary = _state(client).components.get(PRINT_LIBRARY).native
    source = tmp_path / "copy.3mf"
    source.write_bytes(INPUT)
    with source.open("rb") as data:
        uploaded = _call(client, partial(native.upload, source.name, data, None))
    return uploaded.id


def test_standard_presets_slice_with_no_bambuddy(
    native_alone_client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    client = native_alone_client
    file_id = _native_upload(client, tmp_path)
    with fake_slicer_worker(client, settings.temporal_task_queue_slicer) as slicer:
        row = _slice(client, file_id)
    assert row.status == "completed", row.failure
    assert slicer.calls == 1


def test_a_preset_that_needs_bambuddy_says_none_is_configured(
    native_alone_client: TestClient, tmp_path: Path
) -> None:
    file_id = _native_upload(native_alone_client, tmp_path)
    with pytest.raises(ProfileError, match="needs Bambuddy, and none is configured"):
        _start(native_alone_client, file_id, plan=LOCAL_PLAN)


# -- re-review: the cancel's edges and a swept row ---------------------------------------


def test_a_cancel_during_the_record_is_still_recorded_cancelled(
    slice_client: TestClient, slicer: FakeSlicer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review N3: the cancel lands while the slice's own record runs."""
    store = _slicing(slice_client).store
    finish = store.finish
    recording = threading.Event()
    release = threading.Event()

    async def held_finish(slice_id: int, **fields: Any) -> None:
        if fields["status"] == "completed":
            recording.set()
            await asyncio.to_thread(release.wait, 30)
        await finish(slice_id, **fields)

    monkeypatch.setattr(store, "finish", held_finish)
    row = _start(slice_client)
    assert recording.wait(60), "the record never ran"

    async def cancel() -> BaseException | None:
        handle = _slicing(slice_client).temporal().get_workflow_handle(row.workflow_id)
        await handle.cancel()
        release.set()
        try:
            await handle.result()
        except WorkflowFailureError as error:
            return error.cause
        return None

    assert isinstance(_call(slice_client, cancel), CancelledError)
    done = _row(slice_client, row)
    assert (done.status, done.failure) == ("cancelled", "The slice was cancelled.")
    assert _holds(slice_client, row) == set()


def test_a_cancel_from_the_worker_alone_is_a_failure(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    """Re-review N4: no cancel was asked of the workflow, so the execution and the row
    agree that the slice failed."""
    slicer.self_cancel = True
    row = _start(slice_client)

    async def outcome() -> str:
        handle = _slicing(slice_client).temporal().get_workflow_handle(row.workflow_id)
        await handle.result()
        status = (await handle.describe()).status
        assert status is not None
        return status.name

    assert _call(slice_client, outcome) == "COMPLETED"
    done = _row(slice_client, row)
    assert done.status == "failed"
    assert done.failure is not None and done.failure.startswith("The slicer worker failed")


def test_an_execution_whose_row_was_swept_takes_no_hold(slice_client: TestClient) -> None:
    """Re-review N2: the row is gone before the execution's first activity."""
    slicing = _slicing(slice_client)
    starter = slicing.starter
    assert starter is not None

    async def run() -> tuple[int, Any]:
        row = await slicing.store.start(
            workflow_id="slice-swept",
            subject=SUBJECT,
            library_file_id=COPY,
            plate_id=1,
            preset_key="k",
        )
        with slicing.store._pool.connection() as conn:
            conn.execute("DELETE FROM native_slices WHERE id = %s", (row.id,))
        handle = await slicing.temporal().start_workflow(
            SliceWorkflow.run,
            SliceWorkflowInput(
                slice_id=row.id,
                library_file_id=COPY,
                plan=PLAN,
                plate_id=1,
                preset_key=native_preset_key(PLAN, 1, starter.profiles.version),
                slicer_queue=starter.slicer_queue,
            ),
            id=row.workflow_id,
            task_queue=starter.library_queue,
        )
        return row.id, await handle.result()

    slice_id, result = _call(slice_client, run)

    assert result.status == "failed"
    assert result.failure == "The slice's record is gone: slice again."
    with slicing.refs.pool.connection() as conn:
        held = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (HOLDER, str(slice_id)),
        ).fetchone()
    assert held is not None and held["n"] == 0


# -- Task 12 review: the copy named by its bytes, a native id where none serves ---------


def test_a_copy_whose_bytes_changed_since_the_start_is_refused(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    """Task 12 review M6: a reused Bambuddy id with other bytes than the start read is
    another copy: the slice is refused, never made of the wrong file."""
    row = _finished(slice_client, _start(slice_client, copy_sha256="0" * 64))
    assert (row.status, row.failure) == (
        "failed",
        "The copy changed while the slice started: slice again.",
    )
    assert slicer.calls == 0 and _holds(slice_client, row) == set()


def test_the_copys_own_bytes_slice(slice_client: TestClient, slicer: FakeSlicer) -> None:
    row = _slice_with(slice_client, hashlib.sha256(INPUT).hexdigest())
    assert row.status == "completed", row.failure


def _slice_with(client: TestClient, sha: str) -> NativeSlice:
    return _finished(client, _start(client, copy_sha256=sha))


def test_a_native_id_where_the_native_library_does_not_serve_is_refused(
    slice_client: TestClient, slicer: FakeSlicer
) -> None:
    """Task 12 review M3: on ``library_backend=bambuddy`` a native id is refused by
    name, never downloaded from Bambuddy."""
    row = _slice(slice_client, NATIVE_ID_BASE + 5)
    assert row.status == "failed"
    assert row.failure == (
        f"library file {NATIVE_ID_BASE + 5} is in ScadBuddy's own library, which does not"
        " serve here"
    )
    assert slicer.calls == 0
