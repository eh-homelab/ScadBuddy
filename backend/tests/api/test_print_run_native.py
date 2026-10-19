"""`PrintRunWorkflow` on the native slicer (plan 2026-10-16-native-slicer-rust-worker
Task 13, Rulings C1, C2): ``slice_run`` on the ``library`` queue starts the shared
`SliceWorkflow`, or joins the dialog's, and waits for it; ``print_sliced_upload`` puts
the sliced file beside the copy in Bambuddy's library, which ``print_enqueue`` queues.
The fake slicer (`tests/support/fake_slicer.py`) stands in for the Rust worker."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory

from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.workflows.slicing_activities import WAITING
from tests.api.conftest import follow_operation
from tests.api.test_print_filaments import prepared
from tests.api.test_print_preview_native import (
    COPY,
    INPUT,
    _open,
    _settings,
    _slicing,
    _state,
    copy_routes,
    dialog_profiles,
)
from tests.api.test_print_preview_native import kek_file as kek_file  # the fixture, shared
from tests.api.test_print_run_choices import allow_reprints, body, follow_run, run_routes
from tests.api.test_send import API, upload_route
from tests.bambuddy.conftest import recording
from tests.support.fake_slicer import FakeSlicer, fake_slicer_worker
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

HISTORIES = Path(__file__).parents[1] / "fixtures" / "print_run_histories"
#: The first sliced file Bambuddy's fake library answers an upload of a ``.sliced.3mf``
#: with; each later one is the next id.
SLICED = 77
#: The colours `body()`'s plan slices slots 1 and 2 in.
PLAN_COLOURS = ["#688197", "#0047BB"]


class FakeLibrary:
    """Bambuddy's library and queue for the run: the copy (41) and each sliced upload,
    which it then serves as a library file."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.sliced_names: list[str] = []
        self.next_id = SLICED
        self.gone: set[int] = set()
        #: Answer the next ``POST /queue/`` as Bambuddy does a deleted library file.
        self.refuse_next_queue = False
        self.queued: list[int] = []
        #: Each sliced upload stored: its id, name, size and sha256.
        self.stored: dict[int, tuple[str, int, str]] = {}
        #: Store the next sliced upload, then drop its answer (review I1).
        self.drop_next_answer = False
        # Before `upload_route`'s empty listing, which this then wins over.
        router.route(method="GET", path__regex=r"/api/v1/library/files/?$").mock(
            side_effect=self._listing
        )
        upload_route(COPY)
        router.post(f"{API}/library/files").mock(side_effect=self._upload)
        router.route(method="GET", path__regex=r"/api/v1/library/files/\d+$").mock(
            side_effect=self._file
        )
        router.post(f"{API}/queue/").mock(side_effect=self._queue)
        # Before `run_routes`, whose requirements route this then wins over; a pattern of
        # its own (the sliced ids), or respx would merge the two into theirs.
        router.route(
            method="GET", path__regex=r"/api/v1/library/files/(?:7[7-9]|8\d)/filament-requirements"
        ).mock(side_effect=self._requirements)

    def _requirements(self, request: httpx.Request) -> httpx.Response:
        """A sliced upload's filaments in the colours the plan sent (#4013's check); the
        copy's as recorded."""
        file_id = int(request.url.path.split("/")[-2])
        answer = recording("filament-requirements.json")
        for filament in answer["filaments"]:
            if 1 <= filament["slot_id"] <= len(PLAN_COLOURS):
                filament["color"] = PLAN_COLOURS[filament["slot_id"] - 1]
        return httpx.Response(200, json={**answer, "file_id": file_id})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        name = request.content.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
        if not name.endswith(".sliced.3mf"):
            return httpx.Response(
                200, json={"id": COPY, "filename": name, "file_type": "3mf", "file_size": 9}
            )
        file_id = self.next_id
        self.next_id += 1
        self.sliced_names.append(name)
        payload = _uploaded_bytes(request.content)
        self.stored[file_id] = (name, len(payload), _sha(payload))
        if self.drop_next_answer:
            self.drop_next_answer = False
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return httpx.Response(
            200, json={"id": file_id, "filename": name, "file_type": "3mf", "file_size": 9}
        )

    def _file(self, request: httpx.Request) -> httpx.Response:
        file_id = int(request.url.path.rsplit("/", 1)[1])
        if file_id == COPY:
            return httpx.Response(
                200,
                json={
                    "id": COPY,
                    "filename": "demo-elan.3mf",
                    "folder_id": 2,
                    "file_hash": _sha(INPUT),
                },
            )
        if file_id in self.gone or file_id >= self.next_id:
            return httpx.Response(404, json={"detail": "File not found"})
        name, size, sha = self.stored.get(file_id, (f"sliced-{file_id}.3mf", 9, f"{file_id:064x}"))
        return httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": name,
                "file_type": "gcode.3mf",
                "file_size": size,
                "folder_id": None,
                "file_hash": sha,
            },
        )

    def _listing(self, request: httpx.Request) -> httpx.Response:
        """The library's root, where the run files its sliced uploads: those stored and
        not gone."""
        if "folder_id" in request.url.params:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "id": file_id,
                    "filename": name,
                    "file_type": "gcode.3mf",
                    "file_size": size,
                    "folder_id": None,
                }
                for file_id, (name, size, _) in self.stored.items()
                if file_id not in self.gone
            ],
        )

    def _queue(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        if self.refuse_next_queue:
            self.refuse_next_queue = False
            self.gone.add(sent["library_file_id"])
            return httpx.Response(400, json={"detail": "Library file not found"})
        self.queued.append(sent["library_file_id"])
        return httpx.Response(
            200,
            json={
                "id": 50 + len(self.queued),
                "printer_id": 1,
                "library_file_id": sent["library_file_id"],
                "position": 1,
                "status": "queued",
                "plate_id": 1,
            },
        )


def _uploaded_bytes(multipart: bytes) -> bytes:
    """The file part's bytes of one upload's multipart body."""
    boundary = multipart.split(b"\r\n", 1)[0]
    part = multipart.split(b'filename="', 1)[1].split(b"\r\n\r\n", 1)[1]
    return part.rsplit(b"\r\n" + boundary, 1)[0]


def _sha(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def library() -> Iterator[FakeLibrary]:
    with respx.mock as router:
        router.route(host="127.0.0.1").pass_through()
        fake = FakeLibrary(router)
        run_routes()
        copy_routes(router, COPY, INPUT)
        # `copy_routes` named the copy's own read; the library's answers every id.
        router.route(method="GET", path__regex=r"/api/v1/library/files/\d+$").mock(
            side_effect=fake._file
        )
        yield fake


def _app(
    settings: Settings, s3: S3Settings, kek_file: Path, tmp_path: Path, **extra: Any
) -> Iterator[TestClient]:
    profiles = dialog_profiles(tmp_path / "profiles")
    yield from _open(_settings(settings, s3, kek_file, profiles, **extra))


@pytest.fixture
def native_client(
    model: str,
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    library: FakeLibrary,
) -> Iterator[TestClient]:
    yield from _app(settings, s3, kek_file, tmp_path)


@pytest.fixture
def slicer(native_client: TestClient, settings: Settings) -> Iterator[FakeSlicer]:
    with fake_slicer_worker(native_client, settings.temporal_task_queue_slicer) as fake:
        yield fake


def _post_run(client: TestClient, output_id: str) -> str:
    started = client.post(f"/api/v1/print/outputs/{output_id}/run", json=body())
    assert started.status_code in (200, 202), started.text
    run_id: str = started.json()["id"]
    return run_id


def _run(client: TestClient, output_id: str) -> dict[str, Any]:
    return follow_run(client, _post_run(client, output_id), timeout=120)


def _history(client: TestClient, run_id: str) -> WorkflowHistory:
    with _slicing(client).store._pool.connection() as conn:
        row = conn.execute("SELECT workflow_id FROM print_runs WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    temporal = _slicing(client).temporal()

    async def fetch() -> WorkflowHistory:
        return await temporal.get_workflow_handle(row["workflow_id"]).fetch_history()

    assert client.portal is not None
    history: WorkflowHistory = client.portal.call(fetch)
    return history


def _waiting_on_the_slice(client: TestClient, run_id: str) -> bool:
    """Whether the run's pending ``slice_run`` has beaten `WAITING`: it joined the slice."""
    with _slicing(client).store._pool.connection() as conn:
        row = conn.execute("SELECT workflow_id FROM print_runs WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    temporal = _slicing(client).temporal()

    async def details() -> list[bytes]:
        described = await temporal.get_workflow_handle(row["workflow_id"]).describe()
        return [
            payload.data
            for pending in described.raw_description.pending_activities
            if pending.activity_type.name == "slice_run"
            for payload in pending.heartbeat_details.payloads
        ]

    assert client.portal is not None
    return json.dumps(WAITING).encode() in client.portal.call(details)


def _activities(history: WorkflowHistory) -> list[str]:
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


def _inputs(history: WorkflowHistory, activity: str) -> list[Any]:
    return [
        json.loads(event.activity_task_scheduled_event_attributes.input.payloads[0].data)
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and event.activity_task_scheduled_event_attributes.activity_type.name == activity
    ]


def _markers(history: WorkflowHistory) -> bytes:
    """Every marker's details, as bytes, to look a patch id up in."""
    found = b""
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_MARKER_RECORDED:
            continue
        for payloads in event.marker_recorded_event_attributes.details.values():
            for payload in payloads.payloads:
                found += payload.data
    return found


#: Read at import: the tests' own environment drops ``SCADBUDDY_*`` variables.
RECORD = os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1"


def _record(history: WorkflowHistory, name: str) -> None:
    if RECORD:
        (HISTORIES / f"{name}.json").write_text(history.to_json())


def _native_rows(client: TestClient) -> list[dict[str, Any]]:
    with _slicing(client).store._pool.connection() as conn:
        return list(conn.execute("SELECT * FROM native_slices ORDER BY id").fetchall())


def test_a_native_run_slices_uploads_and_queues(
    native_client: TestClient, model: str, slicer: FakeSlicer, library: FakeLibrary
) -> None:
    output_id = prepared(native_client, model)
    run_id = _post_run(native_client, output_id)
    run = follow_run(native_client, run_id, timeout=120)

    assert run["status"] == "succeeded", run
    assert library.sliced_names == ["demo-elan.plate1.sliced.3mf"]
    assert library.queued == [SLICED]
    (row,) = _native_rows(native_client)
    assert row["status"] == "completed"
    with _slicing(native_client).store._pool.connection() as conn:
        recorded = conn.execute(
            "SELECT sliced_library_file_id, sliced_blob_key FROM output_bambuddy_slices"
        ).fetchall()
    assert [(r["sliced_library_file_id"], r["sliced_blob_key"]) for r in recorded] == [
        (SLICED, row["sliced_blob_key"])
    ]
    history = _history(native_client, run_id)
    names = _activities(history)
    assert "slice_run" in names and "print_sliced_upload" in names
    assert "print_slice_start" not in names and "print_slice_wait" not in names
    (record,) = _inputs(history, "print_record")
    outcome = record["outcome"]
    assert outcome["slice_job_id"] is None
    assert outcome["native_slice_id"] == row["id"]
    assert outcome["sliced_blob_key"] == row["sliced_blob_key"]
    assert slicer.calls == 1
    _record(history, "native_sliced")


def test_a_refused_plate_fails_the_run_with_its_stderr(
    native_client: TestClient, model: str, slicer: FakeSlicer, library: FakeLibrary
) -> None:
    slicer.refuse_plate = True
    output_id = prepared(native_client, model)
    run_id = _post_run(native_client, output_id)
    run = follow_run(native_client, run_id, timeout=120)

    assert run["status"] == "failed", run
    assert run["error"]["status"] == 422, run
    assert run["error"]["title"] == "The slice failed"
    assert "plate 99 is empty" in run["error"]["detail"]
    assert library.queued == []
    history = _history(native_client, run_id)
    assert "print_slice_start" not in _activities(history)
    _record(history, "native_slicer_refused")


def test_the_dialog_and_the_run_share_one_slice(
    native_client: TestClient, model: str, slicer: FakeSlicer, library: FakeLibrary
) -> None:
    """Review Focus 6, Ruling C2: one execution, one `slice_plate`, one row."""
    output_id = prepared(native_client, model)
    assert native_client.portal is not None

    async def event() -> Any:
        import asyncio

        return asyncio.Event()

    slicer.hold = native_client.portal.call(event)
    posted = native_client.post(
        f"/api/v1/print/outputs/{output_id}/preview-slice", json=body(), headers=press()
    )
    assert posted.status_code == 202, posted.text
    op = follow_operation(native_client, posted.json(), timeout=60, poll=0.1)
    assert op["status"] == "succeeded", op
    run_id = _post_run(native_client, output_id)
    deadline = time.monotonic() + 60
    while slicer.calls == 0:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    # Review M5: the run's `slice_run` waits on the running slice before it may end, so
    # the run joins the execution rather than finding the row completed.
    while not _waiting_on_the_slice(native_client, run_id):
        assert time.monotonic() < deadline, "the run never joined the running slice"
        time.sleep(0.2)
    (row,) = _native_rows(native_client)
    assert row["status"] == "running"
    native_client.portal.call(slicer.hold.set)

    run = follow_run(native_client, run_id, timeout=120)

    assert run["status"] == "succeeded", run
    assert slicer.calls == 1
    (row,) = _native_rows(native_client)
    assert row["id"] == op["result"]["job_id"]
    (record,) = _inputs(_history(native_client, run_id), "print_record")
    assert record["outcome"]["native_slice_id"] == row["id"]


def test_a_lost_upload_answer_is_taken_on_the_retry(
    native_client: TestClient, model: str, slicer: FakeSlicer, library: FakeLibrary
) -> None:
    """Review I1: Bambuddy stored the sliced file and its answer was lost; the retry
    finds it by its bytes and queues it, leaving no spare file."""
    library.drop_next_answer = True
    output_id = prepared(native_client, model)

    run = _run(native_client, output_id)

    assert run["status"] == "succeeded", run
    assert library.sliced_names == ["demo-elan.plate1.sliced.3mf"]
    assert list(library.stored) == [SLICED]
    assert library.queued == [SLICED]


def test_a_reprint_reuses_the_native_upload(
    native_client: TestClient, model: str, slicer: FakeSlicer, library: FakeLibrary
) -> None:
    allow_reprints(native_client)
    output_id = prepared(native_client, model)
    assert _run(native_client, output_id)["status"] == "succeeded"

    run_id = _post_run(native_client, output_id)
    again = follow_run(native_client, run_id, timeout=120)

    assert again["status"] == "succeeded", again
    assert library.queued == [SLICED, SLICED]
    assert library.sliced_names == ["demo-elan.plate1.sliced.3mf"]
    assert slicer.calls == 1
    assert "slice_run" not in _activities(_history(native_client, run_id))


def test_a_gone_native_upload_is_resliced(
    native_client: TestClient, model: str, slicer: FakeSlicer, library: FakeLibrary
) -> None:
    """`REUSED_GONE_PATCH`: the reused upload is gone as it is queued, so the plate is
    sliced again; the row is completed, so nothing is sliced, only uploaded again."""
    allow_reprints(native_client)
    output_id = prepared(native_client, model)
    assert _run(native_client, output_id)["status"] == "succeeded"
    library.refuse_next_queue = True

    again = _run(native_client, output_id)

    assert again["status"] == "succeeded", again
    assert library.queued == [SLICED, SLICED + 1]
    assert len(library.sliced_names) == 2
    assert slicer.calls == 1


def test_bambuddy_runs_are_unchanged(
    model: str,
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    library: FakeLibrary,
) -> None:
    library.router.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
    )
    library.router.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 9,
                "status": "completed",
                "result": {"library_file_id": SLICED, "filament_used_g": 12.0},
            },
        )
    )
    library.next_id = SLICED + 1  # Bambuddy's own slice is file 77
    for client in _app(settings, s3, kek_file, tmp_path, slicer_backend="bambuddy"):
        output_id = prepared(client, model)
        run_id = _post_run(client, output_id)
        run = follow_run(client, run_id, timeout=120)
        assert run["status"] == "succeeded", run
        history = _history(client, run_id)
        names = _activities(history)
        assert "print_slice_start" in names and "slice_run" not in names
        assert b"native-slicer" not in _markers(history)
        assert base64.b64encode(b"native-slicer") not in _markers(history)
        assert library.queued == [SLICED]


def test_native_library_run(
    model: str,
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    library: FakeLibrary,
) -> None:
    """The combination: the native library and the native slicer. An output's run still
    lays its copy out in Bambuddy's library, which its queue reads, so the sliced file
    goes there too."""
    for client in _app(settings, s3, kek_file, tmp_path, library_backend="native"):
        with fake_slicer_worker(client, settings.temporal_task_queue_slicer) as fake:
            output_id = prepared(client, model)
            run = _run(client, output_id)
        assert run["status"] == "succeeded", run
        assert fake.calls == 1
        assert library.queued == [SLICED]
        assert _state(client) is not None
