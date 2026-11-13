"""A LAN print's read URL of a native slice's sliced 3MF, minted by the API (#5298 Task
32): ``POST …/print/transfer-urls`` answers the internal blob route's URL, signed under the
API's KEK, only for a key a completed `native_slices` row names; any other key, and any
other caller, is 403. The print worker's `RemoteBlobs` asks the route and signs nothing."""

from __future__ import annotations

import base64
import inspect
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI

from scadbuddy.api import crud_areas, crud_print, internal_blobs
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.bambuddy import output_reader
from scadbuddy.bambuddy.output_reader import TRANSFER_URLS, BlobMissingAnswerError, RemoteBlobs
from scadbuddy.core.problems import ApiError, install_problem_handlers
from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient
from scadbuddy.crud.print import NATIVE_SLICES_API_ONLY, RemoteNativeSlices
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.slicing.component import SLICING
from scadbuddy.slicing.models import TransferUrl
from scadbuddy.slicing.store import NativeSliceStore
from scadbuddy.slicing.transfer import (
    INTERNAL_BLOBS,
    READ_TTL,
    TRANSFER_HEADER,
    TRANSFER_REFUSED,
    Transfers,
)
from scadbuddy.slicing.transfer_token import transfer_key, verify
from scadbuddy.store.content import ContentStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_print_crud

KEK_TEXT = base64.b64encode(b"\x32" * 32).decode()
KEK = kek_from_base64(KEK_TEXT)
INTERNAL = "http://api"
KEY = "slice-" + "a" * 64
SLICED = b"sliced 3mf bytes"


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


class _Slices:
    """`NativeSliceStore.names_blob` over the keys of completed slices."""

    def __init__(self, completed: set[str]) -> None:
        self.completed = completed
        self.asked: list[str] = []

    async def names_blob(self, key: str) -> bool:
        self.asked.append(key)
        return key in self.completed


class _Stat:
    def __init__(self, size: int) -> None:
        self.ref = SimpleNamespace(size=size)


class _Content:
    """The two calls the internal GET route makes of the content store."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs

    async def stat(self, key: str) -> _Stat | None:
        return _Stat(len(self.blobs[key])) if key in self.blobs else None

    async def get(self, ref: Any) -> AsyncIterator[bytes]:
        yield SLICED


def _app(
    tmp_path: Path,
    slices: _Slices,
    *,
    transfers: Transfers | None,
    blobs: dict[str, bytes] | None = None,
) -> FastAPI:
    """The print routes, the areas and the internal blob routes, the API's KEK in a file."""
    key_file = tmp_path / "kek"
    key_file.write_text(KEK_TEXT)

    def mint() -> Transfers:
        if transfers is None:
            raise RuntimeError("no content store")
        return transfers

    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="off", secret_key_file=key_file),
        store=SimpleNamespace(content=_Content(blobs or {})),
        components=SimpleNamespace(
            get=lambda key: {SLICING: SimpleNamespace(store=slices, transfers=mint)}[key]
        ),
    )
    app.include_router(crud_print.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    app.include_router(internal_blobs.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: None, credentials=lambda: _Credentials())  # type: ignore[arg-type,return-value]
    return app


def _transfers(now: datetime | None = None) -> Transfers:
    # `internal_get` reads neither the content store nor the puts.
    return Transfers(
        cast(ContentStore, None),
        cast(Any, None),
        internal_base_url=INTERNAL,
        secret=transfer_key(KEK),
        **({} if now is None else {"now": lambda: now}),
    )


def _crud(app: FastAPI, caller: str = "print") -> CrudClient:
    return CrudClient(
        INTERNAL,
        "print",
        ServiceAuth(_Token(f"sbsvc_{caller}")),
        transport=httpx.ASGITransport(app=app),
        unavailable_budget=0.5,
    )


async def _ask(app: FastAPI, key: str, caller: str = "print") -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=INTERNAL, transport=httpx.ASGITransport(app=app)
    ) as client:
        return await client.post(
            TRANSFER_URLS, json={"key": key}, headers={"authorization": f"Bearer sbsvc_{caller}"}
        )


def _blobs(app: FastAPI) -> RemoteBlobs:
    return RemoteBlobs(_crud(app), client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))


async def _read(blobs: RemoteBlobs, key: str) -> bytes:
    return b"".join([chunk async for chunk in blobs.stream(key)])


# -- with no database --------------------------------------------------------------------


def test_the_area_is_served_and_awaited() -> None:
    assert "transfer-urls" in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's.
    assert CRUD_AREAS["print"][-1] == "transfer-urls"


def test_the_allow_rule_is_the_apis_alone() -> None:
    assert "names_blob" in NATIVE_SLICES_API_ONLY
    assert "names_blob" in vars(RemoteNativeSlices)


async def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    slices = _Slices(set())
    response = await _ask(_app(tmp_path, slices, transfers=_transfers()), KEY)
    assert response.status_code == 403
    assert response.json()["reason"] == TRANSFER_REFUSED
    assert TRANSFER_HEADER not in response.text and "url" not in response.json()
    assert slices.asked == [KEY]


async def test_another_callers_credential_is_refused(tmp_path: Path) -> None:
    slices = _Slices({KEY})
    response = await _ask(_app(tmp_path, slices, transfers=_transfers()), KEY, "projects")
    assert response.status_code == 403
    assert response.json()["reason"] == "wrong_caller"
    # Refused before the allow rule is asked.
    assert slices.asked == []


async def test_a_known_slices_key_gets_the_internal_routes_url(tmp_path: Path) -> None:
    now = datetime(2026, 11, 13, 12, 0, 0, 500_000, tzinfo=UTC)
    app = _app(tmp_path, _Slices({KEY}), transfers=_transfers(now))
    response = await _ask(app, KEY)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    url = TransferUrl.model_validate(response.json())
    assert url.url == f"{INTERNAL}{INTERNAL_BLOBS}/{KEY}"
    assert url.expires_at == (now + READ_TTL).replace(microsecond=0)
    token = verify(
        transfer_key(KEK),
        url.headers[TRANSFER_HEADER],
        method="GET",
        blob_key=KEY,
        now=now,
    )
    assert token.expires_at == url.expires_at


async def test_the_url_reads_the_blob_through_the_internal_route(tmp_path: Path) -> None:
    app = _app(tmp_path, _Slices({KEY}), transfers=_transfers(), blobs={KEY: SLICED})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        minted = TransferUrl.model_validate((await _ask(app, KEY)).json())
        read = await client.get(minted.url, headers=minted.headers)
        assert read.status_code == 200 and read.content == SLICED
        assert urlsplit(minted.url).path == f"{INTERNAL_BLOBS}/{KEY}"
        # The token is for its one key: another blob's path refuses it.
        other = await client.get(f"{INTERNAL}{INTERNAL_BLOBS}/slice-other", headers=minted.headers)
        assert other.status_code == 403


async def test_an_api_with_no_content_store_is_not_configured(tmp_path: Path) -> None:
    response = await _ask(_app(tmp_path, _Slices({KEY}), transfers=None), KEY)
    assert response.status_code == 503
    assert response.json()["reason"] == "not_configured"


async def test_remote_blobs_reads_through_the_minted_url(tmp_path: Path) -> None:
    app = _app(tmp_path, _Slices({KEY}), transfers=_transfers(), blobs={KEY: SLICED})
    assert await _read(_blobs(app), KEY) == SLICED


async def test_remote_blobs_raises_a_refusal_it_does_not_retry(tmp_path: Path) -> None:
    slices = _Slices(set())
    with pytest.raises(ApiError) as raised:
        await _read(_blobs(_app(tmp_path, slices, transfers=_transfers())), KEY)
    assert raised.value.status == 403
    assert raised.value.extensions["reason"] == TRANSFER_REFUSED
    assert KEY in raised.value.detail
    # Asked once: a refusal is not sent again.
    assert slices.asked == [KEY]


async def test_remote_blobs_raises_the_apis_not_configured(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as raised:
        await _read(_blobs(_app(tmp_path, _Slices({KEY}), transfers=None)), KEY)
    assert raised.value.status == 503


async def test_remote_blobs_reads_a_missing_blob_as_gone(tmp_path: Path) -> None:
    app = _app(tmp_path, _Slices({KEY}), transfers=_transfers(), blobs={})
    with pytest.raises(BlobMissingAnswerError):
        await _read(_blobs(app), KEY)


async def test_remote_blobs_signs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker holds no key: `RemoteBlobs` takes none, imports no signer, and never
    derives one. Its one read is the URL the route answered, with that URL's header."""
    assert list(inspect.signature(RemoteBlobs).parameters) == ["crud", "client"]
    for name in ("sign", "transfer_key", "Kek"):
        assert not hasattr(output_reader, name), name

    def derived(*_: object) -> bytes:
        raise AssertionError("the worker derived a transfer key")

    monkeypatch.setattr("scadbuddy.slicing.transfer_token.transfer_key", derived)
    minted = {
        "url": "http://elsewhere/read/me",
        "headers": {"X-Read": "t"},
        "expires_at": "2026-11-13T12:10:00Z",
    }
    asked: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if request.url.path == TRANSFER_URLS:
            return httpx.Response(200, json=minted, headers={"X-ScadBuddy-Crud": "1"})
        return httpx.Response(200, content=SLICED)

    transport = httpx.MockTransport(answer)
    blobs = RemoteBlobs(
        CrudClient(INTERNAL, "print", ServiceAuth(_Token("sbsvc_print")), transport=transport),
        client=httpx.AsyncClient(transport=transport),
    )
    assert await _read(blobs, KEY) == SLICED
    ask, read = asked
    assert ask.method == "POST" and ask.read() == b'{"key":"' + KEY.encode() + b'"}'
    assert str(read.url) == minted["url"] and read.headers["X-Read"] == "t"
    # The service credential stays with the API: the read URL never gets it.
    assert "authorization" not in read.headers


# -- over the routes, on the API's own slices --------------------------------------------


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.requires_postgres
async def test_only_a_completed_slices_key_is_named(store: SettingsStore) -> None:
    slices = NativeSliceStore(store.pool)
    row = await slices.start(
        workflow_id="slice-5298-transfer",
        subject="output:a",
        library_file_id=5298,
        plate_id=1,
        preset_key="k",
    )
    # Running: no sliced file yet.
    assert not await slices.names_blob(KEY)
    await slices.finish(row.id, status="completed", sliced_blob_key=KEY, failure=None, warnings=[])
    assert await slices.names_blob(KEY)
    assert not await slices.names_blob("slice-" + "b" * 64)
    # An execution that runs it again clears its file first.
    await slices.running(row.id)
    assert not await slices.names_blob(KEY)


@pytest.mark.requires_postgres
async def test_the_route_answers_on_the_apis_slices(store: SettingsStore) -> None:
    slices = NativeSliceStore(store.pool)
    row = await slices.start(
        workflow_id="slice-5298-route",
        subject="output:a",
        library_file_id=5298,
        plate_id=1,
        preset_key="k",
    )
    await slices.finish(row.id, status="completed", sliced_blob_key=KEY, failure=None, warnings=[])
    async with served_print_crud(store, transfers=_transfers()) as crud:
        answered = await crud.request("POST", TRANSFER_URLS, json={"key": KEY})
        url = TransferUrl.model_validate(answered.json())
        assert url.url == f"{INTERNAL}{INTERNAL_BLOBS}/{KEY}"
        blobs = RemoteBlobs(crud)
        with pytest.raises(ApiError) as raised:
            await blobs._url("slice-" + "b" * 64)
        assert raised.value.status == 403
        await blobs.aclose()
    assert url.expires_at - datetime.now(UTC) <= READ_TTL + timedelta(seconds=1)
