"""The print worker holds no Postgres pool (#5298 Task 31) and no KEK (Task 33): every
store it reads or writes goes through the API's CRUD routes, and it builds its stores with
every way to open Postgres refused and never reads a key file. It exchanges its proof
alone (`exchange_source`, no view fallback) and ignores ``SCADBUDDY_DATABASE_URL`` with a
warning; with no proof it refuses to start. The API opens a virtual printer's sealed code
and says whether it can open access codes at all (area ``access-codes``,
`tests/crud/test_crud_access_codes.py` runs both through the routes)."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
from pathlib import Path
from typing import Any, NoReturn

import httpx
import psycopg
import psycopg_pool
import pytest

from scadbuddy import worker as worker_module
from scadbuddy.auth.service_auth import ExchangeReader, ServiceCredentialConfigError
from scadbuddy.bambuddy import operations as bambuddy_operations
from scadbuddy.bambuddy.output_reader import RemoteBlobs
from scadbuddy.core.settings import NoDatabaseSettings, Settings
from scadbuddy.crud.print import (
    RemoteHeldPresets,
    RemoteNativeSlices,
    RemoteNativeSpools,
    RemotePrinterStore,
    RemoteSpoolStore,
)
from scadbuddy.spools.spool_providers import native_provider
from scadbuddy.worker import PrintWorkerDeps, build_print_deps
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import static_print_crud
from tests.slicing.conftest import PROFILES


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
