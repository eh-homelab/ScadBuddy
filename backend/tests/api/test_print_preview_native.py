"""The print dialog's background slice on the native slicer (plan
2026-10-16-native-slicer-rust-worker Task 12). The dialog's choices go through the run's
own path, as on Bambuddy; the plate is then sliced by `SliceWorkflow`, with the fake
slicer (`tests/support/fake_slicer.py`) in the Rust worker's place, and read back through
the same routes with ``?slicer=native``."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy import toolpath
from scadbuddy.bambuddy.preview import PREVIEW_GRACE
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import PreviewSlice
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import _sweep_native_slices_logged, create_app
from scadbuddy.slicing.component import SLICING, Slicing
from scadbuddy.slicing.store import HOLDER
from scadbuddy.store.content import ContentStore, sweep_content
from tests.api.conftest import follow_operation
from tests.api.test_print_filaments import prepared
from tests.api.test_print_run_choices import body, run_routes
from tests.api.test_send import API, upload_route
from tests.slicing.conftest import PROFILES
from tests.support.fake_slicer import INTERNAL, FakeSlicer, fake_slicer_worker
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

#: The copy the run uploads (``upload_route``), which the native slice reads back.
COPY = 41
INPUT = b"PK\x03\x04 the dialog's laid-out copy"
GCODE = b"M620 S0A\nM620 S1A\nM620 S0A\n"
CREATED = "2026-10-18T18:00:00.000000"
#: The presets the recorded H2C catalogue resolves the dialog's choices to (`body()`),
#: by their setting ids.
DIALOG_PRESETS = {
    "machine": ["GM042"],
    "process": ["GP243"],
    "filament": ["GFSG00_24", "GFSA05_21", "GFSA00", "GFSL99", "GFSG00"],
}
_LISTS = {"machine": "machine_list", "process": "process_list", "filament": "filament_list"}
_ROOTS = {
    "machine": "fdm_machine_root",
    "process": "fdm_process_root",
    "filament": "fdm_filament_root",
}


def dialog_profiles(root: Path) -> Path:
    """The test vendor tree, plus one preset per id the dialog's plan names."""
    shutil.copytree(PROFILES, root)
    vendor_file = root / "BBL.json"
    vendor = json.loads(vendor_file.read_text())
    for kind, ids in DIALOG_PRESETS.items():
        for setting_id in ids:
            name = f"Dialog {setting_id}"
            doc = {
                "type": kind,
                "name": name,
                "inherits": _ROOTS[kind],
                "from": "system",
                "instantiation": "true",
                "setting_id": setting_id,
            }
            (root / "BBL" / kind / f"{name}.json").write_text(json.dumps(doc))
            vendor.setdefault(_LISTS[kind], []).append(
                {"name": name, "sub_path": f"{kind}/{name}.json"}
            )
    vendor_file.write_text(json.dumps(vendor))
    return root


@pytest.fixture
def kek_file(tmp_path: Path) -> Path:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(os.urandom(32)).decode())
    return path


def _settings(
    settings: Settings, s3: S3Settings, kek_file: Path, profiles: Path, **extra: Any
) -> Settings:
    return settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "secret_key_file": kek_file,
            "api_internal_url": INTERNAL,
            "slicer_backend": "native",
            "slicer_profiles_dir": profiles,
            **extra,
        }
    )


@pytest.fixture
def dialog() -> Iterator[respx.MockRouter]:
    """Bambuddy as the run reads it, and the laid-out copy's download. Loopback (moto's
    S3, reached by the fake through presigned URLs) passes through."""
    with respx.mock as router:
        router.route(host="127.0.0.1").pass_through()
        upload_route(COPY)
        run_routes()
        router.get(f"{API}/library/files/{COPY}/download").mock(
            return_value=httpx.Response(200, content=INPUT)
        )
        yield router


def _open(app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings)) as client:
        yield client


@pytest.fixture
def native_client(
    model: str,
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    dialog: respx.MockRouter,
) -> Iterator[TestClient]:
    profiles = dialog_profiles(tmp_path / "profiles")
    # After `model`: the app commits the template it finds at its start, and on the S3
    # store a render needs that commit's snapshot.
    yield from _open(_settings(settings, s3, kek_file, profiles))


@pytest.fixture
def slicer(native_client: TestClient, settings: Settings) -> Iterator[FakeSlicer]:
    with fake_slicer_worker(native_client, settings.temporal_task_queue_slicer) as fake:
        yield fake


def _state(client: TestClient) -> Any:
    return getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]


def _slicing(client: TestClient) -> Slicing:
    slicing: Slicing = _state(client).components.get(SLICING)
    return slicing


def _post(client: TestClient, output_id: str) -> dict[str, Any]:
    started = client.post(
        f"/api/v1/print/outputs/{output_id}/preview-slice", json=body(), headers=press()
    )
    assert started.status_code == 202, started.text
    op: dict[str, Any] = follow_operation(client, started.json(), timeout=60, poll=0.1)
    return op


def start(client: TestClient, output_id: str) -> dict[str, Any]:
    op = _post(client, output_id)
    assert op["status"] == "succeeded", op
    result: dict[str, Any] = op["result"]
    return result


def read(client: TestClient, output_id: str, job_id: int, **params: str) -> httpx.Response:
    response: httpx.Response = client.get(
        f"/api/v1/print/outputs/{output_id}/preview-slices/{job_id}", params=params
    )
    return response


def settled(client: TestClient, output_id: str, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        response = read(client, output_id, job_id, slicer="native")
        assert response.status_code == 200, response.text
        answer: dict[str, Any] = response.json()
        if answer["status"] != "running" or time.monotonic() > deadline:
            return answer
        time.sleep(0.2)


def _event(client: TestClient) -> asyncio.Event:
    async def make() -> asyncio.Event:
        return asyncio.Event()

    assert client.portal is not None
    event: asyncio.Event = client.portal.call(make)
    return event


def _release(client: TestClient, slicer: FakeSlicer) -> None:
    assert client.portal is not None and slicer.hold is not None
    client.portal.call(slicer.hold.set)


def test_a_native_preview_completes(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    assert started["slicer"] == "native"

    answer = settled(native_client, output_id, started["job_id"])

    assert answer["status"] == "completed", answer
    assert answer["slicer"] == "native"
    assert answer["print_time_seconds"] == 3600
    assert answer["filament_used_g"] == 15.5
    assert answer["filament_changes"] == 2
    assert answer["warnings"] == ["a warning"]
    assert answer["slots"] and all(slot["side"] is None for slot in answer["slots"])
    assert slicer.downloads[0][0] == INPUT


def test_the_same_choices_answer_the_same_job(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    output_id = prepared(native_client, model)
    first = start(native_client, output_id)
    assert settled(native_client, output_id, first["job_id"])["status"] == "completed"

    again = start(native_client, output_id)

    assert again["job_id"] == first["job_id"]
    assert slicer.calls == 1


def test_progress_while_running(native_client: TestClient, model: str, slicer: FakeSlicer) -> None:
    output_id = prepared(native_client, model)
    slicer.hold = _event(native_client)
    started = start(native_client, output_id)
    deadline = time.monotonic() + 60
    answer: dict[str, Any] = {}
    while time.monotonic() < deadline:
        answer = read(native_client, output_id, started["job_id"], slicer="native").json()
        if answer.get("progress") == 40:
            break
        assert answer["status"] == "running"
        time.sleep(0.2)
    assert (answer["status"], answer["progress"]) == ("running", 40)
    _release(native_client, slicer)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"


def test_a_refused_plate_fails_with_its_stderr(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    slicer.refuse_plate = True
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    answer = settled(native_client, output_id, started["job_id"])
    assert answer["status"] == "failed"
    assert "plate 99 is empty" in answer["failure"]


def test_another_subject_reads_404(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    other = native_client.get(
        f"/api/v1/print/library/{COPY}/preview-slices/{started['job_id']}",
        params={"slicer": "native"},
    )
    assert other.status_code == 404, other.text
    toolpath_read = native_client.get(
        f"/api/v1/print/library/{COPY}/preview-slices/{started['job_id']}/toolpath",
        params={"slicer": "native"},
    )
    assert toolpath_read.status_code == 404, toolpath_read.text


def test_the_toolpath_of_a_native_slice(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    output_id = prepared(native_client, model)
    slicer.hold = _event(native_client)
    started = start(native_client, output_id)
    path = f"/api/v1/print/outputs/{output_id}/preview-slices/{started['job_id']}/toolpath"

    running = native_client.get(path, params={"slicer": "native"})
    assert running.status_code == 409, running.text

    _release(native_client, slicer)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"
    done = native_client.get(path, params={"slicer": "native"})
    assert done.status_code == 200, done.text
    assert done.content == toolpath.parse(GCODE).encode()


def test_a_native_id_never_reads_a_bambuddy_preview(
    native_client: TestClient, model: str, slicer: FakeSlicer, dialog: respx.MockRouter
) -> None:
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    job_id = started["job_id"]
    assert settled(native_client, output_id, job_id)["status"] == "completed"
    uploads = _state(native_client).uploads
    assert native_client.portal is not None
    native_client.portal.call(
        uploads.record_preview,
        PreviewSlice(
            job_id=job_id,
            subject=PrintSubject.output(output_id).run_subject,
            library_file_id=COPY,
            preset_key="bambuddy",
            plate_id=1,
            job_created=CREATED,
        ),
    )
    dialog.get(f"{API}/slice-jobs/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": job_id,
                "status": "running",
                "kind": "library_file",
                "source_id": COPY,
                "created_at": CREATED,
            },
        )
    )

    native = read(native_client, output_id, job_id, slicer="native").json()
    bambuddy = read(native_client, output_id, job_id).json()

    assert (native["status"], native["slicer"]) == ("completed", "native")
    assert (bambuddy["status"], bambuddy["slicer"]) == ("running", "bambuddy")


def test_native_without_profiles_is_503(
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    model: str,
    dialog: respx.MockRouter,
) -> None:
    empty = tmp_path / "no-profiles"
    empty.mkdir()
    for client in _open(_settings(settings, s3, kek_file, empty)):
        output_id = prepared(client, model)
        op = _post(client, output_id)
        assert op["status"] == "failed", op
        assert op["error"]["status"] == 503, op
        assert "the native slicer is not set up here" in op["error"]["detail"]


def test_the_sweep_removes_a_settled_slice_and_its_holds(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"
    slicing = _slicing(native_client)
    with slicing.store._pool.connection() as conn:
        row = conn.execute(
            "UPDATE native_slices SET finished_at = now() - %s WHERE id = %s"
            " RETURNING sliced_blob_key",
            (PREVIEW_GRACE * 2, started["job_id"]),
        ).fetchone()
    assert row is not None
    key = row["sliced_blob_key"]
    state = _state(native_client)
    content = state.store.content
    assert isinstance(content, ContentStore)
    assert native_client.portal is not None

    native_client.portal.call(_sweep_native_slices_logged, state)
    time.sleep(0.01)
    native_client.portal.call(partial(sweep_content, content, slicing.refs, grace=0))

    assert read(native_client, output_id, started["job_id"], slicer="native").status_code == 404
    with slicing.refs.pool.connection() as conn:
        held = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (HOLDER, str(started["job_id"])),
        ).fetchone()
    assert held is not None and held["n"] == 0
    assert native_client.portal.call(content.stat, key) is None


def test_the_bambuddy_preview_is_unchanged(
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    model: str,
    dialog: respx.MockRouter,
) -> None:
    dialog.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        return_value=httpx.Response(200, json={"job_id": 9, "status": "pending"})
    )
    dialog.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": 9,
                "status": "running",
                "kind": "library_file",
                "source_id": COPY,
                "created_at": CREATED,
            },
        )
    )
    profiles = dialog_profiles(tmp_path / "profiles")
    app_settings = _settings(settings, s3, kek_file, profiles, slicer_backend="bambuddy")
    for client in _open(app_settings):
        output_id = prepared(client, model)
        started = start(client, output_id)
        assert (started["job_id"], started["slicer"]) == (9, "bambuddy")
        answer = read(client, output_id, 9).json()
        assert (answer["status"], answer["slicer"]) == ("running", "bambuddy")


def test_native_library_with_native_slicer(
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    model: str,
    dialog: respx.MockRouter,
) -> None:
    profiles = dialog_profiles(tmp_path / "profiles")
    app_settings = _settings(settings, s3, kek_file, profiles, library_backend="native")
    for client in _open(app_settings):
        with fake_slicer_worker(client, settings.temporal_task_queue_slicer) as fake:
            output_id = prepared(client, model)
            started = start(client, output_id)
            assert started["slicer"] == "native"
            answer = settled(client, output_id, started["job_id"])
        assert answer["status"] == "completed", answer
        assert fake.calls == 1
