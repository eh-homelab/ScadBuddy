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
import time
from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from temporalio.client import Client

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.native import NativeLibrary, blob_key
from scadbuddy.slicing.component import SLICING, Slicing
from scadbuddy.slicing.models import SliceProgress
from scadbuddy.slicing.start import slice_progress, start_native_slice
from scadbuddy.slicing.store import HOLDER, NativeSlice
from scadbuddy.store.content import ContentStore, sweep_content
from scadbuddy.store.content_models import BlobScope
from tests.api.test_send import API, configure
from tests.slicing.conftest import PROFILES
from tests.support.fake_slicer import INTERNAL, SLICED, FakeSlicer, fake_slicer_worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

COPY = 41
#: Another copy of the same bytes.
OTHER_COPY = 42
#: A copy of other bytes, which no earlier slice answers for.
OTHER_BYTES_COPY = 43
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


def _open(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings)) as client:
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


def _start(client: TestClient, library_file_id: int = COPY) -> NativeSlice:
    slicing = _slicing(client)
    assert slicing.starter is not None and slicing.profiles is not None
    starter, profiles = slicing.starter, slicing.profiles

    async def start() -> NativeSlice:
        settings = _state(client).settings_store.load()
        async with client_for(settings) as bambuddy:
            return await start_native_slice(
                slicing.temporal(),
                slicing.store,
                starter,
                library_file_id=library_file_id,
                plan=PLAN,
                plate_id=1,
                subject=SUBJECT,
                profiles=profiles,
                client=bambuddy,
            )

    return _call(client, start)


def _finished(client: TestClient, row: NativeSlice) -> NativeSlice:
    slicing = _slicing(client)

    async def wait() -> NativeSlice:
        await slicing.temporal().get_workflow_handle(row.workflow_id).result()
        found = await slicing.store.get(row.id, SUBJECT)
        assert found is not None
        return found

    return _call(client, wait)


def _slice(client: TestClient, library_file_id: int = COPY) -> NativeSlice:
    return _finished(client, _start(client, library_file_id))


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
    assert slicer.calls == 4


def test_an_expired_url_prepares_again_once(slice_client: TestClient, slicer: FakeSlicer) -> None:
    slicer.expire_first = True
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
