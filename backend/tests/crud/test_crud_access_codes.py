"""The print worker holds no KEK (#5298 Task 33), so the API answers what its key once
did (area ``access-codes``): ``GET …/print/access-codes`` says whether the API can open an
access code, which gates the LAN path (`bambuddy/print_run.py` `_registry_path`), and
``POST …/print/access-codes/virtual-printer`` opens a virtual printer's sealed code
(`bambuddy/virtual_printers.py` `access_code_request`) for the worker's run. The code is
answered ``no-store`` to ``crud.print`` only and never logged. No Postgres: the routes
open nothing in the database."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
import uvicorn
from fastapi import Depends, FastAPI

from scadbuddy.api import crud_areas, crud_print
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.bambuddy.print_run import LAN_NEEDS_KEK, RunRefusalError, _registry_path
from scadbuddy.bambuddy.virtual_printers import (
    CODE_NEEDS_KEK,
    KEY_FIELD,
    SEALED_FIELD,
    access_code_request,
    seal_context,
    virtual_printer_kinds,
)
from scadbuddy.core.problems import ApiError, install_problem_handlers
from scadbuddy.core.secrets import Kek, SealError, kek_from_base64
from scadbuddy.crud.client import CrudRefusedError, CrudSyncClient, CrudUnavailableError
from scadbuddy.crud.print import (
    API_NEEDS_KEK,
    WATCH_CALL_BUDGET,
    WATCH_CALL_TIMEOUT,
    RemotePrinterStore,
)
from scadbuddy.crud.wire import CRUD_HEADERS
from scadbuddy.library.settings_store import PrintSettings
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import KekMissingError, PrinterStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.printers.conftest import KEK

INTERNAL = "http://api"
BAMBUDDY = "http://bambuddy.test"
CODE = "vpc0de12"
#: The KEK after a rotation; `KEK` is then the previous one.
ROTATED = kek_from_base64(base64.b64encode(b"\x11" * 32).decode())
OPEN = "/api/v1/internal/crud/print/access-codes/virtual-printer"


class _Credentials:
    def resolve(self, token: str) -> Service | None:
        return {
            "sbsvc_print": Service("print", frozenset({"crud.print"})),
            "sbsvc_projects": Service("projects", frozenset({"crud.projects"})),
        }.get(token)


class _Token:
    def __init__(self, token: str) -> None:
        self._token = token

    def token(self) -> str | None:
        return self._token

    def invalidate(self, token: str) -> None: ...


def _app(kek: Kek | None, previous: Kek | None = None) -> FastAPI:
    """The print routes and the areas over an API registry holding ``kek``. Its pool is
    never opened: neither route reads the database."""
    store = PrinterStore(cast(Any, None), kek, previous_kek=previous)
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="off"),
        components=SimpleNamespace(get=lambda key: {PRINTERS: SimpleNamespace(store=store)}[key]),
    )
    app.include_router(crud_print.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: None, credentials=lambda: _Credentials())  # type: ignore[arg-type,return-value]
    return app


@contextmanager
def _remote(app: FastAPI, caller: str = "print") -> Iterator[RemotePrinterStore]:
    """The worker's store over ``app``, served on a thread of its own, so a blocking call
    from the test's loop is answered."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, name="crud access codes", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    client = CrudSyncClient(
        f"http://127.0.0.1:{port}",
        "print",
        ServiceAuth(_Token(f"sbsvc_{caller}")),
        unavailable_budget=0.5,
    )
    try:
        yield RemotePrinterStore(client)
    finally:
        client.close()
        server.should_exit = True
        thread.join(30)


def _sealed(kek: Kek, *, vp_id: int | None = None, key: str = "k1") -> tuple[str, str]:
    fields = access_code_request(kek, CODE, vp_id=vp_id, idempotency_key=key)
    return fields[SEALED_FIELD], seal_context(vp_id=vp_id, idempotency_key=key)


# -- the area ---------------------------------------------------------------------------


def test_the_area_is_served_and_awaited() -> None:
    assert "access-codes" in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's.
    assert CRUD_AREAS["print"][-1] == "access-codes"


# -- whether the API opens access codes -------------------------------------------------


@pytest.mark.parametrize(("kek", "opens"), [(KEK, True), (None, False)])
async def test_the_worker_asks_whether_the_api_opens_access_codes(
    kek: Kek | None, opens: bool
) -> None:
    with _remote(_app(kek)) as remote:
        assert await asyncio.to_thread(remote.opens_access_codes) is opens


# -- a virtual printer's sealed code ----------------------------------------------------


async def test_the_api_opens_a_sealed_code_for_the_worker(caplog: pytest.LogCaptureFixture) -> None:
    sealed, context = _sealed(KEK)
    app = _app(KEK)
    with (
        _remote(app) as remote,
        caplog.at_level(logging.INFO, logger="scadbuddy.api.crud_print"),
    ):
        opened = await asyncio.to_thread(remote.open_virtual_access_code, sealed, context)
    assert opened == CODE
    assert "released a virtual printer's access code to service credential" in caplog.text
    assert CODE not in caplog.text
    async with httpx.AsyncClient(base_url=INTERNAL, transport=httpx.ASGITransport(app)) as client:
        response = await client.post(
            OPEN,
            json={"sealed": sealed, "context": context},
            headers={"authorization": "Bearer sbsvc_print"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == CRUD_HEADERS["Cache-Control"] == "no-store"


async def test_a_code_sealed_before_a_rotation_opens_under_the_previous_key() -> None:
    sealed, context = _sealed(KEK)
    with _remote(_app(ROTATED, previous=KEK)) as remote:
        assert await asyncio.to_thread(remote.open_virtual_access_code, sealed, context) == CODE


async def test_an_api_with_no_kek_opens_nothing() -> None:
    sealed, context = _sealed(KEK)
    with _remote(_app(None)) as remote, pytest.raises(KekMissingError) as missing:
        await asyncio.to_thread(remote.open_virtual_access_code, sealed, context)
    assert str(missing.value) == API_NEEDS_KEK


@pytest.mark.parametrize(
    "ask",
    [
        # Bound to another VP: the seal's context is its AAD.
        lambda sealed, _: (sealed, seal_context(vp_id=4, idempotency_key="k1")),
        # Sealed under a key the API does not hold.
        lambda _, context: (_sealed(ROTATED)[0], context),
        # Not a seal at all.
        lambda _, context: ("zz", context),
    ],
)
async def test_a_value_that_does_not_open_is_a_seal_error(ask: Any) -> None:
    sealed, context = ask(*_sealed(KEK))
    with _remote(_app(KEK)) as remote, pytest.raises(SealError) as refused:
        await asyncio.to_thread(remote.open_virtual_access_code, sealed, context)
    assert CODE not in str(refused.value)


@pytest.mark.parametrize("context", ["", "printer:1", "vp-access-code:vp:x", "x" * 600])
async def test_a_context_not_of_a_virtual_printer_is_refused(context: str) -> None:
    sealed, _ = _sealed(KEK)
    with _remote(_app(KEK)) as remote, pytest.raises(CrudRefusedError) as refused:
        await asyncio.to_thread(remote.open_virtual_access_code, sealed, context)
    assert refused.value.status == 422


async def test_another_caller_is_refused() -> None:
    sealed, context = _sealed(KEK)
    async with httpx.AsyncClient(
        base_url=INTERNAL, transport=httpx.ASGITransport(_app(KEK))
    ) as client:
        for method, path, body in [
            ("POST", OPEN, {"sealed": sealed, "context": context}),
            ("GET", "/api/v1/internal/crud/print/access-codes", None),
        ]:
            response = await client.request(
                method, path, json=body, headers={"authorization": "Bearer sbsvc_projects"}
            )
            assert response.status_code == 403
            assert CODE not in response.text


# -- the worker's kinds and its LAN gate, through the routes ----------------------------


class _Settings:
    def load(self, timeout: float | None = None) -> PrintSettings:
        return PrintSettings(bambuddy_url=BAMBUDDY, bambuddy_api_key="k")

    def plate_clear_required(self) -> bool:
        return False


def _vp(vp_id: int) -> dict[str, Any]:
    return {
        "id": vp_id,
        "name": "X",
        "enabled": True,
        "mode": "immediate",
        "model": "O1C",
        "access_code_set": True,
        "serial": "01P00A000000009",
        "target_printer_id": None,
    }


@respx.mock
async def test_a_virtual_printer_create_sends_the_code_the_api_opened() -> None:
    """The kind's check asks the API whether it opens codes, and its run sends Bambuddy
    the code the API opened: the worker holds no key."""
    # The worker's own calls reach the API's routes, served on this host.
    respx.route(host="127.0.0.1").pass_through()
    created = respx.post(f"{BAMBUDDY}/api/v1/virtual-printers").mock(
        return_value=httpx.Response(200, json=_vp(7))
    )
    # The follow's sync after the write is best effort; nothing listed keeps it short.
    respx.get(f"{BAMBUDDY}/api/v1/virtual-printers").mock(return_value=httpx.Response(500, json={}))
    request = {
        "fields": {"name": "X"},
        **access_code_request(KEK, CODE, vp_id=None, idempotency_key="k1"),
    }
    assert request[KEY_FIELD] == "k1"
    with _remote(_app(KEK)) as remote:
        kinds = {
            kind.name: kind
            for kind in virtual_printer_kinds(settings_store=_Settings(), printers=remote)
        }
        create = kinds["bambuddy_vp_create"]
        checked = await create.check(request)
        await create.run(request, checked)

    assert json.loads(created.calls.last.request.read())["access_code"] == CODE


async def test_a_sealed_code_with_no_kek_on_the_api_is_refused_before_bambuddy() -> None:
    request = {
        "fields": {"name": "X"},
        **access_code_request(KEK, CODE, vp_id=None, idempotency_key="k1"),
    }
    with _remote(_app(None)) as remote:
        kinds = {
            kind.name: kind
            for kind in virtual_printer_kinds(settings_store=_Settings(), printers=remote)
        }
        with pytest.raises(ApiError) as refused:
            await kinds["bambuddy_vp_create"].check(request)
    assert (refused.value.status, refused.value.detail) == (503, CODE_NEEDS_KEK)


class _Reports:
    """A fresh LAN report for every printer."""

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime]:
        return {"gcode_state": "IDLE"}, datetime.now(UTC)


def _row(preferred: str) -> PrinterRow:
    return PrinterRow(
        id="00000000-0000-4000-8000-000000000001",
        serial="01P00A000000001",
        name="Bench",
        bambuddy_printer_id=1,
        lan_host="192.168.1.50",
        has_access_code=True,
        preferred_path=preferred,  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def test_with_no_kek_on_the_api_an_auto_printer_goes_through_bambuddy() -> None:
    with _remote(_app(None)) as remote:
        path, status = await _registry_path(_row("auto"), remote, _Reports())  # type: ignore[arg-type]
    assert (path, status) == ("bambuddy", None)


async def test_with_no_kek_on_the_api_a_native_printer_is_refused() -> None:
    with _remote(_app(None)) as remote, pytest.raises(ApiError) as refused:
        await _registry_path(_row("native"), remote, _Reports())  # type: ignore[arg-type]
    assert (refused.value.status, refused.value.detail) == (503, LAN_NEEDS_KEK)


async def test_with_a_kek_on_the_api_the_lan_path_is_open() -> None:
    """The worker holds no key, yet the run takes the LAN: the API opens the code."""
    with _remote(_app(KEK)) as remote:
        path, _ = await _registry_path(_row("auto"), remote, _Reports())  # type: ignore[arg-type]
        # With no report the native row passes the gate and is refused for the report.
        with pytest.raises(RunRefusalError):
            await _registry_path(_row("native"), remote, None)
    assert path == "native"


@pytest.mark.parametrize("preferred", ["auto", "native"])
async def test_an_api_out_of_reach_is_retried_never_read_as_no_key(preferred: str) -> None:
    """The gate's read failing is the client's retryable error, as the report's read
    beside it is: neither a fallback to Bambuddy nor `LAN_NEEDS_KEK`. It gives up within
    the watch's short bound, not the client's own budget."""
    asked: list[httpx.Request] = []

    def down(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(502, headers=CRUD_HEADERS)

    client = CrudSyncClient(
        INTERNAL, "print", ServiceAuth(_Token("sbsvc_print")), transport=httpx.MockTransport(down)
    )
    try:
        started = time.monotonic()
        with pytest.raises(CrudUnavailableError):
            await _registry_path(_row(preferred), RemotePrinterStore(client), _Reports())  # type: ignore[arg-type]
        assert time.monotonic() - started < WATCH_CALL_BUDGET + WATCH_CALL_TIMEOUT + 2
    finally:
        client.close()
    assert asked
    assert all(request.url.path.endswith("/print/access-codes") for request in asked)
