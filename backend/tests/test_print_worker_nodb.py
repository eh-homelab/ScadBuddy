"""The print worker holds no Postgres pool (#5298 Task 31) and no KEK (Task 33): every
store it reads or writes goes through the API's CRUD routes, and it builds its stores with
every way to open Postgres refused and never reads a key file. It exchanges its proof
alone (`exchange_source`, no view fallback) and ignores ``SCADBUDDY_DATABASE_URL`` with a
warning; with no proof it refuses to start. The API opens a virtual printer's sealed code
and says whether it can open access codes at all (area ``access-codes``,
`tests/crud/test_crud_access_codes.py` runs both through the routes)."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import faulthandler
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import httpx
import psycopg
import psycopg_pool
import pytest
import respx
from temporalio.testing import ActivityEnvironment

from scadbuddy import worker as worker_module
from scadbuddy.auth.exchange import BootstrapVerifier
from scadbuddy.auth.service_auth import ExchangeReader, ServiceCredentialConfigError
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.bambuddy import operations as bambuddy_operations
from scadbuddy.bambuddy import send
from scadbuddy.bambuddy.output_reader import OutputNaming, RemoteBlobs
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.settings import NoDatabaseSettings, Settings
from scadbuddy.crud import listener
from scadbuddy.crud.print import (
    RemoteHeldPresets,
    RemoteNativeSlices,
    RemoteNativeSpools,
    RemotePrinterStore,
    RemoteSpoolStore,
)
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.rack.usage import RackUsageStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.spools.spool_providers import native_provider
from scadbuddy.worker import PrintWorkerDeps, build_print_deps
from scadbuddy.workflows.print_models import (
    LAN_PRINT_ACTIVITY,
    LAN_UPLOAD_ACTIVITY,
    LanPrintInput,
    LanUploadInput,
    SlicedSource,
)
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import print_crud_app, static_print_crud
from tests.printers.conftest import KEK
from tests.slicing.conftest import PROFILES
from tests.support.fake_lan import FakeLan, needs_fake_lan, running_fake_lan
from tests.test_print_native_workflow import BLOB, SLICED, TRAYS, Blobs
from tests.test_render_worker_nodb import _ApiSide
from tests.test_render_worker_nodb import _no_database as _no_api_database


def _no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way the worker could open Postgres refused."""

    def refused(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("the print worker opened Postgres")

    monkeypatch.setattr(psycopg, "connect", refused)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(refused))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(refused))
    monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", refused)
    monkeypatch.setattr(psycopg_pool.AsyncConnectionPool, "__init__", refused)


def _settings(tmp_path: Path, **fields: Any) -> NoDatabaseSettings:
    return NoDatabaseSettings(
        data_dir=tmp_path / "no-volume",
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        api_internal_url="http://scadbuddy:8082",
        slicer_profiles_dir=PROFILES,
        **fields,
    )


def _proof(tmp_path: Path) -> Path:
    proof = tmp_path / "bootstrap"
    proof.write_text("p" * 40)
    return proof


def test_the_print_queue_reads_settings_with_no_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Settings] = []

    async def run(settings: Settings, queue: str) -> None:
        started.append(settings)

    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    monkeypatch.setenv("SCADBUDDY_TEMPORAL_ADDRESS", UNUSED_TEMPORAL_ADDRESS)
    monkeypatch.setattr("sys.argv", ["scadbuddy.worker", "--queue", "bambuddy"])
    monkeypatch.setattr(worker_module, "_main", run)
    monkeypatch.setattr(worker_module, "configure_tracing", lambda *a, **k: None)
    monkeypatch.setattr(worker_module, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(faulthandler, "register", lambda *a, **k: None)
    worker_module.main()
    [settings] = started
    assert isinstance(settings, NoDatabaseSettings) and settings.database_url == ""


async def test_it_builds_with_no_database_and_holds_no_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_database(monkeypatch)
    settings = _settings(tmp_path, service_bootstrap_file=_proof(tmp_path))
    deps = await asyncio.to_thread(build_print_deps, settings)
    try:
        assert "database" not in {field for field in PrintWorkerDeps.__dataclass_fields__}
        reader = deps.crud._auth._reader
        assert isinstance(reader, ExchangeReader) and reader._fallback is None
        [check] = [
            fn
            for fn in deps.activities
            if getattr(fn, "__temporal_activity_definition").name == "print_check"
        ]
        print_deps = check.__self__.d  # type: ignore[attr-defined]
        assert isinstance(print_deps.spools.store, RemoteSpoolStore)
        assert isinstance(native_provider(print_deps.spools).store, RemoteNativeSpools)
        assert print_deps.native is not None
        assert isinstance(print_deps.native.profiles.held, RemoteHeldPresets)
        [lan] = [
            fn
            for fn in deps.activities
            if getattr(fn, "__temporal_activity_definition").name == "lan_print"
        ]
        assert isinstance(lan.__self__.d.native_spools, RemoteNativeSpools)  # type: ignore[attr-defined]
        # With no KEK, a LAN print still reads its sliced file: the API mints the URL
        # (#5298 Task 32).
        assert settings.secret_key_file is None
        blobs = lan.__self__.d.blobs  # type: ignore[attr-defined]
        assert isinstance(blobs, RemoteBlobs) and blobs._crud is deps.crud
    finally:
        await deps.aclose()
    assert not settings.data_dir.exists()


async def test_a_native_slice_starts_through_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background slice's kind is given the remote slice store."""
    built: list[dict[str, Any]] = []
    original = bambuddy_operations.bambuddy_kinds_over

    def recording(**kwargs: Any) -> Any:
        built.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(worker_module, "bambuddy_kinds_over", recording)
    _no_database(monkeypatch)
    deps = await asyncio.to_thread(
        build_print_deps, _settings(tmp_path, service_bootstrap_file=_proof(tmp_path))
    )
    try:
        [kwargs] = built
        assert isinstance(kwargs["native_slices"], RemoteNativeSlices)
    finally:
        await deps.aclose()


async def test_with_a_proof_and_a_url_it_only_exchanges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL is ignored once a proof is set: an exchange answered 404 gives no token,
    never a read of the view."""
    _no_database(monkeypatch)
    connects: list[object] = []
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: connects.append(a))
    settings = _settings(
        tmp_path, service_bootstrap_file=_proof(tmp_path), database_url=UNUSED_DATABASE_URL
    )
    deps = await asyncio.to_thread(build_print_deps, settings)
    try:
        reader = deps.crud._auth._reader
        assert isinstance(reader, ExchangeReader) and reader._fallback is None
        reader._transport = httpx.MockTransport(lambda request: httpx.Response(404))
        assert await asyncio.to_thread(reader.token) is None
        assert connects == []
    finally:
        await deps.aclose()


async def test_it_never_reads_a_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both key files set, as an old Deployment may still mount them: the worker reads
    neither. Its store asks the API whether it opens access codes and opens none itself."""
    _no_database(monkeypatch)
    key_files = {tmp_path / "kek", tmp_path / "kek-previous"}
    for path in key_files:
        path.write_text("a" * 44)
    read: list[Path] = []
    read_bytes = Path.read_bytes

    def refused(self: Path) -> bytes:
        if self in key_files:
            read.append(self)
            raise AssertionError("the print worker read a key file")
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", refused)
    settings = _settings(
        tmp_path,
        service_bootstrap_file=_proof(tmp_path),
        secret_key_file=tmp_path / "kek",
        secret_key_previous_file=tmp_path / "kek-previous",
    )
    deps = await asyncio.to_thread(build_print_deps, settings)
    try:
        assert "kek" not in PrintWorkerDeps.__dataclass_fields__
        assert isinstance(deps.printers, RemotePrinterStore)
        assert not hasattr(deps.printers, "kek")
        assert not hasattr(deps.printers, "previous_kek")
    finally:
        await deps.aclose()
    assert read == []


async def test_it_ignores_a_database_url_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _no_database(monkeypatch)
    settings = _settings(
        tmp_path, service_bootstrap_file=_proof(tmp_path), database_url=UNUSED_DATABASE_URL
    )
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.WARNING, logger="scadbuddy.worker"):
        await worker_module.run_print_worker(
            settings, stop=stop, health_port=None, crud=static_print_crud()
        )
    assert "ignoring SCADBUDDY_DATABASE_URL" in caplog.text


@pytest.mark.parametrize("database_url", ["", UNUSED_DATABASE_URL])
def test_with_no_proof_it_refuses_to_start(tmp_path: Path, database_url: str) -> None:
    """No view to fall back to: a database URL does not stand in for a proof."""
    with pytest.raises(ServiceCredentialConfigError, match="SCADBUDDY_SERVICE_TOKEN_FILE"):
        build_print_deps(_settings(tmp_path, database_url=database_url))


# -- a print end to end, on a worker with no database and no KEK -------------------------
#
# As the render worker's (#5298 Task 20): the API's print routes over Postgres with the
# exchange of a bootstrap secret for ``print``; the worker built by `build_print_deps`
# with no database URL and no key file, every way to open Postgres refused outside the
# API's own requests. Only what the CRUD routes do not serve is faked: the printer (the
# fake LAN printer), Bambuddy (respx), the sliced file's bytes and the output's files.

BAMBUDDY = "https://bambuddy.test"
BAMBUDDY_API = f"{BAMBUDDY}/api/v1"


class _Api:
    def __init__(self, url: str, secret: Path) -> None:
        self.url = url
        self.secret = secret


@asynccontextmanager
async def _api(store: SettingsStore, tmp_path: Path, **parts: Any) -> AsyncIterator[_Api]:
    """The API's side, made before the database is refused: the print routes over
    ``store`` and ``parts`` (`print_crud_app`'s), with the exchange of a bootstrap secret
    for ``print``."""
    secrets_dir = tmp_path / "bootstrap-secrets"
    secrets_dir.mkdir()
    secret = secrets_dir / "print"
    secret.write_text(secrets.token_urlsafe(32))
    app = print_crud_app(
        store,
        ServiceCredentialStore(store.pool),
        bootstrap=BootstrapVerifier.from_dir(secrets_dir),
        **parts,
    )
    app.add_middleware(_ApiSide)
    await listener.start(app, 0)
    try:
        yield _Api(f"http://127.0.0.1:{listener.crud_listener_port(app)}", secret)
    finally:
        await listener.stop(app)


def _worker_settings(tmp_path: Path, api: _Api) -> NoDatabaseSettings:
    cfg = NoDatabaseSettings(
        data_dir=tmp_path / "no-volume",
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        api_internal_url=api.url,
        slicer_profiles_dir=PROFILES,
        service_bootstrap_file=api.secret,
    )
    assert cfg.database_url == "" and cfg.secret_key_file is None
    return cfg


@pytest.fixture
def api_store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    """The API's settings, with its KEK: the Bambuddy key and a printer's access code
    are sealed and opened there, never on the worker."""
    key = tmp_path / "api.key"
    key.write_text(base64.b64encode(b"\x5a" * 32).decode())
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path / "api-data",
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            secret_key_file=key,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def fake_lan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeLan]:
    async with running_fake_lan(tmp_path, monkeypatch) as lan:
        yield lan


def _activity(deps: PrintWorkerDeps, name: str) -> Any:
    [found] = [
        fn for fn in deps.activities if getattr(fn, "__temporal_activity_definition").name == name
    ]
    return found


def _needs_fake_lan(test: Callable[..., Any]) -> Callable[..., Any]:
    for mark in needs_fake_lan:
        test = mark(test)
    return test


@pytest.mark.requires_postgres
@_needs_fake_lan
async def test_a_native_lan_print_runs_on_a_worker_with_no_database_and_no_kek(
    tmp_path: Path,
    api_store: SettingsStore,
    pg_conninfo: str,
    fake_lan: FakeLan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plate is uploaded to the printer and started: the registry row and its access
    code (opened by the API) go through the CRUD routes."""
    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCADBUDDY_SECRET_KEY_FILE", raising=False)
    printers = PrinterStore(api_store.pool, KEK)
    row = printers.create(PrinterCreate(serial=fake_lan.serial, name="Fake H2C", model="H2C"))
    printers.link_lan(
        row.id,
        LanLink(host=fake_lan.host, access_code=fake_lan.access_code),
        cert_sha256=fake_lan.cert_sha256,
    )
    rack = RackUsageStore(pg_conninfo)
    blobs = Blobs()
    stem = "scadbuddy-nodb0000-p1"
    try:
        async with _api(api_store, tmp_path, printers=printers, rack=rack) as api:
            cfg = _worker_settings(tmp_path, api)
            with pytest.MonkeyPatch.context() as refused:
                tried = _no_api_database(refused)
                deps = await asyncio.to_thread(build_print_deps, cfg)
                try:
                    upload = _activity(deps, LAN_UPLOAD_ACTIVITY)
                    start = _activity(deps, LAN_PRINT_ACTIVITY)
                    lan = upload.__self__
                    assert start.__self__ is lan
                    assert isinstance(lan.d.printers, RemotePrinterStore)
                    # The sliced file's bytes: its read URL's route is
                    # `tests/crud/test_crud_transfer_urls.py`'s.
                    lan.d = dataclasses.replace(lan.d, blobs=blobs, start_wait=10.0)
                    env = ActivityEnvironment()
                    uploaded = await env.run(
                        upload,
                        LanUploadInput(
                            run_id="run-nodb",
                            printer=row.id,
                            sliced=SlicedSource(blob_key=BLOB),
                            remote_name=f"{stem}.3mf",
                            plate_id=1,
                        ),
                    )
                    started = await env.run(
                        start,
                        LanPrintInput(
                            printer=row.id,
                            uploaded=uploaded,
                            plate_id=1,
                            subtask_name=stem,
                            plan=TRAYS,
                        ),
                    )
                finally:
                    await deps.aclose()
    finally:
        rack.close()
    assert tried == []
    assert blobs.read == [BLOB]
    assert started.subtask_name == stem and started.run_id == "run-nodb"
    [received] = fake_lan.received()["ftps"]
    assert received["name"] == f"/{stem}.3mf" and received["size"] == len(SLICED)
    [sent] = fake_lan.project_files()
    assert sent["subtask_name"] == stem and sent["md5"] == received["md5"]


class _Outputs:
    """The output's record and files, which the API's internal outputs routes serve
    (`api/internal.py`, not a CRUD route): `RemoteOutputs`' place."""

    def __init__(self, meta: OutputMeta) -> None:
        self.meta = meta

    async def get(self, output_id: str) -> OutputMeta:
        assert output_id == self.meta.id
        return self.meta

    async def model_3mf(self, output_id: str) -> bytes | None:
        return b"3mf"

    async def naming(self, meta: OutputMeta) -> OutputNaming:
        return OutputNaming(stem="Demo")

    async def print_settings(self, meta: OutputMeta) -> tuple[dict[str, str], str | None]:
        return {}, None

    async def part_settings(self, meta: OutputMeta) -> None:
        return None

    async def aclose(self) -> None:
        pass


async def _the_3mf(store: object, meta: object) -> bytes:
    return b"3mf"


def _bambuddy_routes() -> respx.Route:
    """Bambuddy's library: an empty inbox folder, the upload, and the read that finds
    the copy (as `tests/api/test_send.py`'s `upload_route`)."""
    respx.get(f"{BAMBUDDY_API}/printers/").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BAMBUDDY_API}/library/files").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BAMBUDDY_API}/library/files/41").mock(
        return_value=httpx.Response(200, json={"id": 41, "filename": "demo.3mf", "folder_id": 2})
    )
    return respx.post(f"{BAMBUDDY_API}/library/files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 41,
                "filename": "demo.3mf",
                "file_type": "3mf",
                "file_size": 3,
                "thumbnail_path": None,
            },
        )
    )


@pytest.mark.requires_postgres
async def test_a_bambuddy_send_runs_on_a_worker_with_no_database_and_no_kek(
    tmp_path: Path, api_store: SettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The send uploads the output to Bambuddy's library with the key the API released,
    under the API's copy lease, and the API records the copy."""
    monkeypatch.delenv("SCADBUDDY_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCADBUDDY_SECRET_KEY_FILE", raising=False)
    api_store.save(
        SettingsPatch(bambuddy_url=BAMBUDDY, bambuddy_api_key="s3cret", library_folder_id=2)
    )
    meta = OutputMeta(
        id="e" * 32,
        slug="demo",
        job_id="d" * 32,
        created_at=datetime(2026, 11, 13, tzinfo=UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        colors=["#FF0000"],
    )
    built: list[list[OperationKind]] = []
    original = bambuddy_operations.bambuddy_kinds_over

    def recording(**kwargs: Any) -> list[OperationKind]:
        kinds = original(**kwargs)
        built.append(kinds)
        return kinds

    monkeypatch.setattr(worker_module, "bambuddy_kinds_over", recording)
    monkeypatch.setattr(worker_module, "RemoteOutputs", lambda *a, **k: _Outputs(meta))
    # The output's bytes and their layout are the send's own tests' (`test_ensure_copy`).
    monkeypatch.setattr(send, "read_3mf", _the_3mf)
    monkeypatch.setattr(send, "_laid_out_for", lambda payload, target: payload)
    async with _api(api_store, tmp_path) as api:
        cfg = _worker_settings(tmp_path, api)
        with pytest.MonkeyPatch.context() as refused, respx.mock(assert_all_called=False):
            # The worker's calls to the API reach it; Bambuddy is respx's.
            respx.route(host="127.0.0.1").pass_through()
            uploaded = _bambuddy_routes()
            tried = _no_api_database(refused)
            deps = await asyncio.to_thread(build_print_deps, cfg)
            try:
                [kinds] = built
                [kind] = [kind for kind in kinds if kind.name == "send"]
                request = {"output_id": meta.id}
                checked = await kind.check(request)
                answer = await kind.run(request, checked)
            finally:
                await deps.aclose()
    assert tried == []
    assert answer["library_file_id"] == 41 and answer["created"] is True
    sent = uploaded.calls.last.request
    assert sent.url.params["folder_id"] == "2" and sent.headers["X-API-Key"] == "s3cret"
    copies = await BambuddyUploadStore(api_store.pool).for_output(meta.id)
    assert [(copy.id, copy.folder_id) for copy in copies] == [(41, 2)]
