"""A native print's destination (#3818 tier A Task 5, plan
2026-10-18-hot-path-without-bambuddy D6): a native library folder, never a Bambuddy
project. The native flag reaches `check` through `PrintDeps.native_run` (Task 9 sets it
from `hot_path_native`); here a test sets it."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy import print_run
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.projects import PATH_SEPARATOR
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings, StoredSettings
from scadbuddy.main import create_app
from scadbuddy.print_library.copies import (
    NATIVE_PROJECT_REFUSAL,
    native_destination,
    require_native_folder,
)
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.remote import RemoteLibrary
from scadbuddy.printers.models import LanLink, PrinterCreate, PrinterPatch
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.store.bambuddy import folder_name
from scadbuddy.workflows import print_activities
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import REFUSED, CheckInput
from tests.api.test_library_native import _native, _native_settings
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.support.no_bambuddy import forbid_bambuddy
from tests.test_print_workflow import run_input

pytestmark = pytest.mark.requires_postgres

_REQUEST: dict[str, Any] = run_input().request.model_dump(mode="json")

BAMBUDDY = "http://bambuddy.test"


@pytest.fixture
def native_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    with TestClient(create_app(_native_settings(settings, s3))) as client:
        yield client


@pytest.fixture
def native(native_client: TestClient) -> NativeLibrary:
    return _native(native_client)


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    assert client.portal is not None

    async def run() -> Any:
        return await fn(*args, **kwargs)

    return client.portal.call(run)


def test_a_native_print_with_no_folder_is_filed_under_its_templates_title(
    native_client: TestClient, native: NativeLibrary
) -> None:
    first = _call(native_client, native_destination, native, folder_id=None, title="Name sign")
    again = _call(native_client, native_destination, native, folder_id=None, title="Name sign")

    assert first == again
    folder = _call(native_client, native.folder, first)
    assert (folder.name, folder.parent_id) == (folder_name("Name sign"), None)


def test_a_library_file_with_no_folder_is_filed_at_the_top_level(
    native_client: TestClient, native: NativeLibrary
) -> None:
    assert _call(native_client, native_destination, native, folder_id=None, title=None) is None


def test_a_native_folder_is_used_as_chosen(
    native_client: TestClient, native: NativeLibrary
) -> None:
    folder = _call(native_client, native.create_folder, "Gifts", None).id
    chosen = _call(native_client, native_destination, native, folder_id=folder, title="Sign")
    assert chosen == folder


@pytest.mark.parametrize("folder_id", [7, 1_999_999_999])
def test_a_folder_the_native_library_lacks_is_refused(
    native_client: TestClient, native: NativeLibrary, folder_id: int
) -> None:
    """A Bambuddy-range id, or a native one that is gone: 422, as Bambuddy's check is."""
    with pytest.raises(ApiError) as refused:
        _call(native_client, require_native_folder, native, folder_id)
    assert refused.value.status == 422


def test_the_print_worker_reads_folders_and_makes_the_template_folder(
    native_client: TestClient, native: NativeLibrary
) -> None:
    """Through `RemoteLibrary`, as the print worker's check and plan do."""
    nested_parent = _call(native_client, native.create_folder, "Gifts", None).id
    nested = _call(native_client, native.create_folder, "Birthdays", nested_parent).id

    async def remote_calls() -> tuple[Any, ...]:
        remote = RemoteLibrary(
            "http://scadbuddy",
            httpx.AsyncClient(transport=httpx.ASGITransport(app=native_client.app)),
        )
        try:
            await require_native_folder(remote, nested)
            made = await native_destination(remote, folder_id=None, title="Bracket")
            with pytest.raises(ApiError) as refused:
                await require_native_folder(remote, 7)
        finally:
            await remote.aclose()
        return made, refused.value

    assert native_client.portal is not None
    made, refused = native_client.portal.call(remote_calls)
    assert _call(native_client, native.folder, made).name == folder_name("Bracket")
    assert refused.status == 422


@dataclass
class Registry:
    """A registry with one row the LAN reaches, set to print natively, its report fresh:
    the row a native check decides on (`run_route`)."""

    printers: PrinterStore
    reports: PrinterReportStore
    row: str


@pytest.fixture
def registry(pg_pool: PgPool) -> Registry:
    printers = PrinterStore(pg_pool, KEK)
    reports = PrinterReportStore(pg_pool)
    row = printers.create(PrinterCreate(serial="01P00A0000000DD", name="Garage", model="H2C"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="cd" * 32
    )
    printers.patch(row.id, PrinterPatch(preferred_path="native"))
    reports.put(row.id, {"gcode_state": "IDLE"}, datetime.now(UTC))
    return Registry(printers, reports, row.id)


#: The hot path's stored backends (plan 2026-10-18 D2).
HOT_PATH: dict[str, Any] = {
    "slicer_backend": "native",
    "library_backend": "native",
    "store_backend": "s3",
}


def _activities(native: Any, registry: Registry, **settings: Any) -> PrintActivities:
    unused: Any = None
    store: Any = SimpleNamespace(
        load=lambda: StoredSettings(bambuddy_url=BAMBUDDY, **HOT_PATH, **settings)
    )
    return PrintActivities(
        PrintDeps(
            settings_store=store,
            outputs=unused,
            uploads=unused,
            prints=unused,
            store=unused,
            observer=unused,
            library=native,
            # The image's profiles a native check resolves against (D5); never read here.
            native=cast(Any, SimpleNamespace(profiles=unused)),
            printers=registry.printers,
            reports=registry.reports,
        )
    )


def _check(request: PrintRunRequest, **source: Any) -> CheckInput:
    run = run_input()
    run = run.model_copy(
        update={"request": request, "source": run.source.model_copy(update=source)}
    )
    return CheckInput(input=run, started_at=datetime.now(UTC))


async def test_a_project_on_a_native_run_is_refused_before_anything(registry: Registry) -> None:
    request = run_input().request.model_copy(
        update={"project_id": 5, "printer": registry.row, "printer_id": None}
    )
    with respx.mock(assert_all_called=False) as router:
        bambuddy = router.route(host="bambuddy.test").respond(500)
        with pytest.raises(ApplicationError) as raised:
            await ActivityEnvironment().run(_activities(object(), registry).check, _check(request))
    assert raised.value.type == REFUSED
    problem = raised.value.details[0]
    assert (problem.status, problem.detail) == (422, NATIVE_PROJECT_REFUSAL)
    assert not bambuddy.called


async def test_a_native_check_resolves_no_project_and_checks_the_native_folder(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    """``last_project_id`` is ignored, `resolve_project` (a Bambuddy read) is never
    called, and `prepare_run` is given the native library to check the folder in."""
    seen: dict[str, Any] = {}

    async def no_resolve(*args: Any) -> int | None:
        raise AssertionError("resolve_project read Bambuddy on a native run")

    async def stop(client: Any, source: Any, settings: Any, request: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ApiError(418, "stop here")

    async def source(*args: Any) -> Any:
        return object()

    monkeypatch.setattr(print_activities, "resolve_project", no_resolve)
    monkeypatch.setattr(print_activities, "prepare_run", stop)
    library = object()
    activities = _activities(library, registry, last_project_id=9)
    monkeypatch.setattr(activities, "_source", source)
    request = run_input().request.model_copy(
        update={"folder_id": 1_000_000_123, "printer": registry.row, "printer_id": None}
    )
    run = _check(request, kind="library", file_id=1_000_000_001, output_id=None)
    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(activities.check, run)

    assert seen["project"].project_id is None
    assert seen["native"] is library
    assert (seen["route"].row.id, seen["route"].native) == (registry.row, True)


async def test_prepare_run_on_native_checks_the_folder_natively(
    native_client: TestClient, native: NativeLibrary
) -> None:
    """`prepare_run(native=…)`: a Bambuddy-range folder is the 422, before any read of
    Bambuddy (the client is never touched) or of the source."""
    request = PrintRunRequest.model_validate({**_REQUEST, "folder_id": 7})
    with pytest.raises(ApiError) as refused:
        await print_run.prepare_run(
            None,  # never read: the folder is refused first
            None,  # type: ignore[arg-type]
            StoredSettings(),
            request,
            native=native,
        )
    assert refused.value.status == 422


@pytest.fixture
def configured_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    """The native library with a Bambuddy configured: what a native run must not ask."""
    with TestClient(create_app(_native_settings(settings, s3, bambuddy_url=BAMBUDDY))) as client:
        yield client


def test_the_picker_on_native_lists_native_folders_and_no_projects(
    configured_client: TestClient, spans: InMemorySpanExporter
) -> None:
    """#4312 review M3: with Bambuddy configured, inside the zero-call harness."""
    native = _native(configured_client)
    gifts = _call(configured_client, native.create_folder, "Gifts", None).id
    nested = _call(configured_client, native.create_folder, "Birthdays", gifts).id
    state = getattr(configured_client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    assert state.settings_store.load().bambuddy_url == BAMBUDDY
    with forbid_bambuddy(spans, BAMBUDDY):
        listed = configured_client.get("/api/v1/print/projects", params={"native": "true"})
        default = configured_client.get(
            "/api/v1/print/projects/default",
            params={"native": "true", "library_file_id": 1_000_000_001},
        )

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["projects"] == [] and body["last_project_id"] is None
    assert [(f["id"], f["path"]) for f in body["folders"]] == [
        (gifts, "Gifts"),
        (nested, f"Gifts{PATH_SEPARATOR}Birthdays"),
    ]
    assert default.status_code == 200, default.text
    assert default.json() == {"project_id": None, "reason": "none"}


def test_the_native_picker_on_the_bambuddy_library_is_a_conflict(client: TestClient) -> None:
    listed = client.get("/api/v1/print/projects", params={"native": "true"})
    assert listed.status_code == 409
