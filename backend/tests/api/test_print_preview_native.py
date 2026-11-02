"""The print dialog's background slice on the native slicer (plan
2026-10-16-native-slicer-rust-worker Task 12). The dialog's choices go through the run's
own path, as on Bambuddy; the plate is then sliced by `SliceWorkflow`, with the fake
slicer (`tests/support/fake_slicer.py`) in the Rust worker's place, and read back through
the same routes with ``?slicer=native``."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import time
from collections import OrderedDict
from collections.abc import Iterator
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy import preview as bambuddy_preview
from scadbuddy.bambuddy import toolpath
from scadbuddy.bambuddy.preview import PREVIEW_GRACE
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import PreviewSlice
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import _sweep_native_slices_logged, create_app
from scadbuddy.slicing import preview as native_preview
from scadbuddy.slicing.component import SLICING, Slicing
from scadbuddy.slicing.start import LOST_AFTER, STOPPED
from scadbuddy.slicing.store import HOLDER, NativeSlice, NativeSliceStore, SliceSubjectError
from scadbuddy.store.content import ContentStore, sweep_content
from tests.api.conftest import follow_operation
from tests.api.test_print_filaments import prepared
from tests.api.test_print_library import flow_copy_routes, library_file, one_color
from tests.api.test_print_run_choices import body, on_spool, run_routes
from tests.api.test_send import API, configure, upload_route
from tests.slicing.conftest import PROFILES
from tests.support.fake_slicer import INTERNAL, FakeSlicer, fake_slicer_worker
from tests.support.operations import press
from tests.support.sliced3mf import sliced_3mf

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

#: The copy the run uploads (``upload_route``), which the native slice reads back.
COPY = 41
INPUT = b"PK\x03\x04 the dialog's laid-out copy"
GCODE = b"M620 S0A\nM620 S1A\nM620 S0A\n"
CREATED = "2026-10-18T18:00:00.000000"
NATIVE_SLICES = Path(__file__).parents[1] / "fixtures" / "native_slices"
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
        copy_routes(router, COPY, INPUT)
        yield router


def copy_routes(router: respx.MockRouter, copy: int, payload: bytes) -> None:
    """The laid-out copy as the start reads it (its ``file_hash``, which names the slice,
    review M6) and as the slice downloads it."""
    router.get(f"{API}/library/files/{copy}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": copy,
                "filename": "demo-elan.3mf",
                "folder_id": 2,
                "file_hash": hashlib.sha256(payload).hexdigest(),
            },
        )
    )
    router.get(f"{API}/library/files/{copy}/download").mock(
        return_value=httpx.Response(200, content=payload)
    )


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


def test_a_native_preview_names_each_slots_side(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    """#4232: the side each slot was sliced for, from the sliced file, as the Bambuddy
    path answers it, so the dialog's side-mismatch warning fires on native too."""
    slicer.sliced = (NATIVE_SLICES / "h2d-two-filaments.gcode.3mf").read_bytes()
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)

    answer = settled(native_client, output_id, started["job_id"])

    assert answer["status"] == "completed", answer
    assert [(slot["slot_id"], slot["side"]) for slot in answer["slots"]] == [(1, "R"), (2, "L")]


def test_a_silk_profile_sets_the_silk_slots_nozzle_temperature_in_its_filament_file(
    native_client: TestClient, model: str, slicer: FakeSlicer, dialog: respx.MockRouter
) -> None:
    """#2799: on the native slicer a profile's filament settings go into the filament
    file of the slot whose spool it matches (spool 5 is Bambu's tri-colour silk), the
    channel measured to change the G-code (``slicing/presets.py``)."""
    dialog.get(f"{API}/slicer/preset-values").mock(
        return_value=httpx.Response(
            200, json={"resolved": True, "values": {"outer_wall_speed": ["200", "200"]}}
        )
    )
    output_id = prepared(native_client, model)
    started = native_client.post(
        f"/api/v1/print/outputs/{output_id}/preview-slice",
        json={**on_spool(5), "filament_profiles": ["silk-pla"]},
        headers=press(),
    )
    assert started.status_code == 202, started.text
    op = follow_operation(native_client, started.json(), timeout=60, poll=0.1)
    assert op["status"] == "succeeded", op

    answer = settled(native_client, output_id, op["result"]["job_id"])

    assert answer["status"] == "completed", answer
    _input, _machine, process, silk, *_ = slicer.downloads[0]
    # In the preset's shape: one entry per extruder variant, every one set alike.
    assert json.loads(silk)["nozzle_temperature"] == ["235"]
    # The test tree's preset leaves this one out, so it is written as given.
    assert json.loads(silk)["nozzle_temperature_initial_layer"] == "235"
    assert "nozzle_temperature" not in json.loads(process)
    assert json.loads(process)["outer_wall_speed"] == ["50", "50"]


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


# -- Task 12 review -----------------------------------------------------------------------


def test_a_restarted_failed_slice_reads_running_at_once(
    native_client: TestClient, model: str, slicer: FakeSlicer
) -> None:
    """Review I1: the dialog's first read after the start, with no wait, must not show
    the previous failure, or the dialog stops polling on it."""
    output_id = prepared(native_client, model)
    slicer.refuse_plate = True
    first = start(native_client, output_id)
    assert settled(native_client, output_id, first["job_id"])["status"] == "failed"
    slicer.refuse_plate = False

    again = start(native_client, output_id)
    read_at_once = read(native_client, output_id, again["job_id"], slicer="native").json()

    assert again["job_id"] == first["job_id"]
    assert (read_at_once["status"], read_at_once["failure"]) == ("running", None)
    assert settled(native_client, output_id, again["job_id"])["status"] == "completed"


def _row_without_execution(client: TestClient, subject: str, *, age: timedelta) -> NativeSlice:
    """A running row no execution writes (a start that never reached Temporal)."""
    store = _slicing(client).store
    assert client.portal is not None
    row: NativeSlice = client.portal.call(
        partial(
            store.start,
            workflow_id=f"slice-{COPY}-never-started",
            subject=subject,
            library_file_id=COPY,
            plate_id=1,
            preset_key="k",
        )
    )
    with store._pool.connection() as conn:
        conn.execute(
            "UPDATE native_slices SET started_at = now() - %s WHERE id = %s", (age, row.id)
        )
    return row


def test_a_read_of_a_fresh_row_with_no_execution_is_running(
    native_client: TestClient, model: str
) -> None:
    """Review M1: an execution Temporal has no record of reads as no progress, not a 500."""
    output_id = prepared(native_client, model)
    row = _row_without_execution(
        native_client, PrintSubject.output(output_id).run_subject, age=timedelta(0)
    )
    answer = read(native_client, output_id, row.id, slicer="native")
    assert answer.status_code == 200, answer.text
    assert (answer.json()["status"], answer.json()["progress"]) == ("running", None)


def test_a_read_of_a_lost_row_is_terminal(native_client: TestClient, model: str) -> None:
    """Review M1: with the sweeps off nothing else would stop it; the dialog would poll
    a running row for good."""
    output_id = prepared(native_client, model)
    row = _row_without_execution(
        native_client, PrintSubject.output(output_id).run_subject, age=LOST_AFTER * 2
    )
    answer = read(native_client, output_id, row.id, slicer="native")
    assert answer.status_code == 200, answer.text
    assert (answer.json()["status"], answer.json()["failure"]) == ("failed", STOPPED)


def test_the_library_preview_on_the_native_slicer(
    settings: Settings, s3: S3Settings, kek_file: Path, tmp_path: Path
) -> None:
    """Review M2: a library file's dialog, start to toolpath, on the native slicer."""
    profiles = dialog_profiles(tmp_path / "profiles")
    with respx.mock as router:
        router.route(host="127.0.0.1").pass_through()
        for client in _open(_settings(settings, s3, kek_file, profiles)):
            configure(client)
            one_color(89)
            flow_copy_routes()
            library_file(89)
            run_routes()
            copy_routes(router, 141, INPUT)
            request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}
            with fake_slicer_worker(client, settings.temporal_task_queue_slicer):
                posted = client.post(
                    "/api/v1/print/library/89/preview-slice", json=request, headers=press()
                )
                assert posted.status_code == 202, posted.text
                op = follow_operation(client, posted.json(), timeout=60, poll=0.1)
                assert op["status"] == "succeeded", op
                job_id = op["result"]["job_id"]
                assert op["result"]["slicer"] == "native"
                base = f"/api/v1/print/library/89/preview-slices/{job_id}"
                deadline = time.monotonic() + 60
                answer = client.get(base, params={"slicer": "native"}).json()
                while answer["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.2)
                    answer = client.get(base, params={"slicer": "native"}).json()
                assert (answer["status"], answer["slicer"]) == ("completed", "native"), answer
                toolpath_read = client.get(f"{base}/toolpath", params={"slicer": "native"})
                assert toolpath_read.status_code == 200, toolpath_read.text
                assert toolpath_read.content == toolpath.parse(GCODE).encode()
                bambuddy_read = client.get(base)
                assert bambuddy_read.status_code == 404, bambuddy_read.text


def test_a_preset_the_image_lacks_is_422(
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    model: str,
    dialog: respx.MockRouter,
) -> None:
    """Review M3: a plan naming a preset Bambu Studio's tree does not have is the
    plan's 422, not a 500."""
    profiles = dialog_profiles(tmp_path / "profiles")
    # A machine preset: Bambuddy serves only process preset values, so nothing else
    # can stand in for it.
    (profiles / "BBL" / "machine" / "Dialog GM042.json").unlink()
    vendor_file = profiles / "BBL.json"
    vendor = json.loads(vendor_file.read_text())
    vendor["machine_list"] = [e for e in vendor["machine_list"] if e["name"] != "Dialog GM042"]
    vendor_file.write_text(json.dumps(vendor))
    for client in _open(_settings(settings, s3, kek_file, profiles)):
        op = _post(client, prepared(client, model))
        assert (op["status"], op["error"]["status"]) == ("failed", 422), op
        assert "printer preset" in op["error"]["detail"]


def test_a_broken_profiles_tree_is_503(
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    model: str,
    dialog: respx.MockRouter,
) -> None:
    """Review N-b: a tree the image cannot read is the image's defect, a 503."""
    profiles = dialog_profiles(tmp_path / "profiles")
    (profiles / "BBL" / "process" / "Dialog GP243.json").write_text("{not json")
    for client in _open(_settings(settings, s3, kek_file, profiles)):
        op = _post(client, prepared(client, model))
        assert (op["status"], op["error"]["status"]) == ("failed", 503), op


def test_a_toolpath_past_its_bound_is_422(
    native_client: TestClient, model: str, slicer: FakeSlicer, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"
    monkeypatch.setattr(native_preview, "MAX_SLICED_BYTES", 10)
    # Another test's row of the same id and bytes may be cached in this process.
    monkeypatch.setattr(bambuddy_preview, "_TOOLPATHS", OrderedDict())
    path = f"/api/v1/print/outputs/{output_id}/preview-slices/{started['job_id']}/toolpath"
    answer = native_client.get(path, params={"slicer": "native"})
    assert answer.status_code == 422, answer.text
    assert "too large" in answer.json()["detail"]


def test_a_slice_with_no_gcode_has_no_toolpath(
    native_client: TestClient, model: str, slicer: FakeSlicer, monkeypatch: pytest.MonkeyPatch
) -> None:
    slicer.sliced = sliced_3mf(plates={1: (3600, [12.5, 3.0])}, gcode={})
    monkeypatch.setattr(bambuddy_preview, "_TOOLPATHS", OrderedDict())
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"
    path = f"/api/v1/print/outputs/{output_id}/preview-slices/{started['job_id']}/toolpath"
    answer = native_client.get(path, params={"slicer": "native"})
    assert answer.status_code == 422, answer.text
    assert "no G-code" in answer.json()["detail"]


def test_a_completed_read_never_holds_the_file_whole(
    native_client: TestClient, model: str, slicer: FakeSlicer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4: the sliced 3MF is streamed to a file and only its parts read."""
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"

    async def whole(self: ContentStore, ref: object) -> bytes:
        raise AssertionError("read whole")

    monkeypatch.setattr(ContentStore, "read", whole)
    monkeypatch.setattr(native_preview, "_READS", OrderedDict())
    monkeypatch.setattr(bambuddy_preview, "_TOOLPATHS", OrderedDict())
    answer = read(native_client, output_id, started["job_id"], slicer="native")
    assert answer.status_code == 200, answer.text
    assert answer.json()["print_time_seconds"] == 3600
    path = f"/api/v1/print/outputs/{output_id}/preview-slices/{started['job_id']}/toolpath"
    assert native_client.get(path, params={"slicer": "native"}).status_code == 200


def test_full_reads_are_capped(
    native_client: TestClient, model: str, slicer: FakeSlicer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M4: a read that cannot get a turn soon is told to come back."""
    output_id = prepared(native_client, model)
    started = start(native_client, output_id)
    assert settled(native_client, output_id, started["job_id"])["status"] == "completed"
    monkeypatch.setattr(native_preview, "_READS", OrderedDict())
    monkeypatch.setattr(native_preview, "_READING_SLICES", asyncio.Semaphore(0))
    monkeypatch.setattr(native_preview, "SLICE_READ_WAIT_SECONDS", 0.01)
    answer = read(native_client, output_id, started["job_id"], slicer="native")
    assert answer.status_code == 503, answer.text


def test_a_reused_copy_id_with_other_bytes_slices_again(
    native_client: TestClient, model: str, slicer: FakeSlicer, dialog: respx.MockRouter
) -> None:
    """Review M6: Bambuddy's SQLite library hands a deleted newest file's id out again.
    Copy 41 with other bytes is another copy, and is sliced as one."""
    output_id = prepared(native_client, model)
    first = start(native_client, output_id)
    assert settled(native_client, output_id, first["job_id"])["status"] == "completed"
    copy_routes(dialog, COPY, INPUT + b" other bytes")

    again = start(native_client, output_id)

    assert again["job_id"] != first["job_id"]
    assert settled(native_client, output_id, again["job_id"])["status"] == "completed"
    assert slicer.calls == 2
    assert slicer.downloads[1][0] == INPUT + b" other bytes"


def test_a_row_of_another_subject_is_409(
    native_client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M6: should two subjects ever name one slice, the start says so (409)."""

    async def refuse(self: object, **kwargs: object) -> NativeSlice:
        raise SliceSubjectError("slice x belongs to out-1, not out-2")

    monkeypatch.setattr(NativeSliceStore, "start", refuse)
    op = _post(native_client, prepared(native_client, model))
    assert (op["status"], op["error"]["status"]) == ("failed", 409), op


def test_a_native_row_on_the_local_store_is_503(
    settings: Settings, s3: S3Settings, kek_file: Path, tmp_path: Path, model: str
) -> None:
    """Review N-c: a leftover native row after a switch to ``store_backend=local`` is a
    named 503, not a 500."""
    profiles = dialog_profiles(tmp_path / "profiles")
    app_settings = _settings(settings, s3, kek_file, profiles, store_backend="local")
    for client in _open(app_settings):
        output_id = prepared(client, model)
        store = _slicing(client).store
        assert client.portal is not None
        row: NativeSlice = client.portal.call(
            partial(
                store.start,
                workflow_id="slice-41-local",
                subject=PrintSubject.output(output_id).run_subject,
                library_file_id=COPY,
                plate_id=1,
                preset_key="k",
            )
        )
        client.portal.call(
            partial(
                store.finish,
                row.id,
                status="completed",
                sliced_blob_key="slice-x",
                failure=None,
                warnings=[],
            )
        )
        answer = read(client, output_id, row.id, slicer="native")
        assert answer.status_code == 503, answer.text
        assert "store_backend" in answer.json()["detail"]
