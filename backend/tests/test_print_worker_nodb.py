"""The print worker holds no Postgres pool (#5298 Task 31): every store it reads or writes
goes through the API's CRUD routes, and it builds its stores with every way to open
Postgres refused. With a proof it exchanges that proof alone (`exchange_source`, no view
fallback), whether or not ``SCADBUDDY_DATABASE_URL`` is set. Only with no proof and a URL
set does it read its sealed credential from its view (`token_source`, until #5298 Task
34). With neither it refuses to start."""

from __future__ import annotations

import asyncio
import faulthandler
from pathlib import Path
from typing import Any, NoReturn

import httpx
import psycopg
import psycopg_pool
import pytest

from scadbuddy import worker as worker_module
from scadbuddy.auth.service_auth import ExchangeReader, ServiceCredentialConfigError
from scadbuddy.auth.service_credentials import TokenReader
from scadbuddy.bambuddy import operations as bambuddy_operations
from scadbuddy.core.settings import NoDatabaseSettings, Settings
from scadbuddy.crud.print import (
    RemoteHeldPresets,
    RemoteNativeSlices,
    RemoteNativeSpools,
    RemoteSpoolStore,
)
from scadbuddy.spools.spool_providers import native_provider
from scadbuddy.worker import PrintWorkerDeps, build_print_deps
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS
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


async def test_with_a_url_and_no_proof_it_reads_its_view_and_opens_no_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment with no proof yet: the credential's view is the URL's one use, read
    when the first call needs a token, never at build."""
    _no_database(monkeypatch)
    deps = await asyncio.to_thread(
        build_print_deps, _settings(tmp_path, database_url=UNUSED_DATABASE_URL)
    )
    try:
        assert isinstance(deps.crud._auth._reader, TokenReader)
    finally:
        await deps.aclose()


def test_with_neither_a_proof_nor_a_url_it_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ServiceCredentialConfigError, match="SCADBUDDY_SERVICE_TOKEN_FILE"):
        build_print_deps(_settings(tmp_path))
