"""A LAN print's read URL of a native slice's sliced 3MF, minted by the API (#5298 Task
32): ``POST …/print/transfer-urls`` answers the internal blob route's URL, signed under the
API's KEK, only for a key the content index holds live as a slicer's output (kind
``slice`` with `SLICED_OUTPUT_META`), whether or not its `native_slices` row still stands;
any other key (a slicer's input among them), and any other caller, is 403. The
print worker's `RemoteBlobs` asks the route, signs nothing, and reads a refusal as a file
no longer stored."""

from __future__ import annotations

import asyncio
import base64
import inspect
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI
from temporalio.testing import ActivityEnvironment

from scadbuddy.api import crud_areas, crud_print, crud_render, internal_blobs
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.models import Service
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.bambuddy import output_reader
from scadbuddy.bambuddy.output_reader import TRANSFER_URLS, BlobMissingAnswerError, RemoteBlobs
from scadbuddy.core.problems import ApiError, install_problem_handlers
from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.crud.client import CrudClient, CrudUnavailableError
from scadbuddy.slicing.component import SLICING
from scadbuddy.slicing.models import TransferUrl
from scadbuddy.slicing.transfer import (
    INTERNAL_BLOBS,
    READ_TTL,
    SLICE_KIND,
    SLICED_OUTPUT_META,
    STORE_UNAVAILABLE,
    TRANSFER_HEADER,
    TRANSFER_REFUSED,
    Transfers,
)
from scadbuddy.slicing.transfer_token import transfer_key, verify
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobKind, BlobRef, BlobStat
from scadbuddy.worker import PRINT_CRUD_AREAS
from scadbuddy.workflows.printing import HEARTBEAT, LAN_UPLOAD_TIMEOUT

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


class _Content:
    """The content index's `stat` (what the route's allow rule reads) and `get` (what the
    internal GET route streams), over ``blobs``: key to kind and bytes. No
    `native_slices` row exists here: the rule follows the blob, not the row."""

    def __init__(
        self,
        blobs: dict[str, tuple[BlobKind, bytes]],
        *,
        meta: dict[str, Any] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.blobs = blobs
        self.meta = meta or {}
        self.failure = failure
        self.asked: list[str] = []

    async def stat(self, key: str) -> BlobStat | None:
        self.asked.append(key)
        if self.failure is not None:
            raise self.failure
        if key not in self.blobs:
            return None
        kind, data = self.blobs[key]
        ref = BlobRef(sha256="0" * 64, kind=kind, backend="s3", backend_id=key, size=len(data))
        return BlobStat(key=key, ref=ref, slug=None, meta=self.meta, touched_at=datetime.now(UTC))

    async def get(self, ref: BlobRef) -> AsyncIterator[bytes]:
        yield self.blobs[ref.backend_id][1]


def _live(*keys: str, kind: BlobKind = SLICE_KIND, marked: bool = True) -> _Content:
    """``keys`` stored live as ``kind``; ``marked``, as `Transfers.finish_put` marks a
    slicer's output (`SLICED_OUTPUT_META`), else as a slicer's input is stored."""
    meta = dict(SLICED_OUTPUT_META) if marked else {}
    return _Content({key: (kind, SLICED) for key in keys}, meta=meta)


def _app(
    tmp_path: Path, content: _Content, *, configured: bool = True, now: datetime | None = None
) -> FastAPI:
    """The print routes, the areas and the internal blob routes over ``content``, the
    API's KEK in a file; ``configured`` False is an API with no content store."""
    key_file = tmp_path / "kek"
    key_file.write_text(KEK_TEXT)
    transfers = Transfers(
        cast(ContentStore, content),
        cast(Any, None),
        internal_base_url=INTERNAL,
        secret=transfer_key(KEK),
        **({} if now is None else {"now": lambda: now}),
    )

    def mint() -> Transfers:
        if not configured:
            raise RuntimeError("no content store")
        return transfers

    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="off", secret_key_file=key_file),
        store=SimpleNamespace(content=content),
        components=SimpleNamespace(get=lambda key: {SLICING: SimpleNamespace(transfers=mint)}[key]),
    )
    app.include_router(crud_print.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    app.include_router(internal_blobs.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: None, credentials=lambda: _Credentials())  # type: ignore[arg-type,return-value]
    return app


class _Clock:
    """The CRUD client's clock, moved only by its own sleeps: the retry budget counts the
    backoffs, never how long a slow runner takes to answer a try (#6252)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _crud(app: FastAPI, caller: str = "print", clock: _Clock | None = None) -> CrudClient:
    return CrudClient(
        INTERNAL,
        "print",
        ServiceAuth(_Token(f"sbsvc_{caller}")),
        transport=httpx.ASGITransport(app=app),
        unavailable_budget=0.5,
        clock=time.monotonic if clock is None else clock,
        sleep=asyncio.sleep if clock is None else clock.sleep,
    )


async def _ask(app: FastAPI, key: str, caller: str = "print") -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=INTERNAL, transport=httpx.ASGITransport(app=app)
    ) as client:
        return await client.post(
            TRANSFER_URLS, json={"key": key}, headers={"authorization": f"Bearer sbsvc_{caller}"}
        )


def _blobs(app: FastAPI, caller: str = "print", clock: _Clock | None = None) -> RemoteBlobs:
    return RemoteBlobs(
        _crud(app, caller, clock),
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)),
    )


async def _read(blobs: RemoteBlobs, key: str) -> bytes:
    return b"".join([chunk async for chunk in blobs.stream(key)])


def test_the_area_is_served_and_awaited() -> None:
    assert "transfer-urls" in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's.
    assert CRUD_AREAS["print"][-2:] == ["transfer-urls", "access-codes"]


async def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    content = _live()
    response = await _ask(_app(tmp_path, content), KEY)
    assert response.status_code == 403
    assert response.json()["reason"] == TRANSFER_REFUSED
    assert TRANSFER_HEADER not in response.text and "url" not in response.json()
    assert content.asked == [KEY]


@pytest.mark.parametrize("kind", ["workspace", "piece", "media", "asset", "library"])
async def test_a_live_blob_of_another_kind_is_refused(tmp_path: Path, kind: BlobKind) -> None:
    key = f"{kind}-" + "c" * 64
    response = await _ask(_app(tmp_path, _live(key, kind=kind)), key)
    assert response.status_code == 403
    assert response.json()["reason"] == TRANSFER_REFUSED


@pytest.mark.parametrize("name", ["input.3mf", "machine.json", "process.json", "filament_1.json"])
async def test_a_slicers_input_of_kind_slice_is_refused(tmp_path: Path, name: str) -> None:
    """The slicer's laid-out copy and preset files are stored as kind ``slice`` with no
    mark (`workflows/slicing_activities.py`), so none of them gets a read URL."""
    key = "slice-" + name.encode().hex().ljust(64, "0")[:64]
    response = await _ask(_app(tmp_path, _live(key, marked=False)), key)
    assert response.status_code == 403
    assert response.json()["reason"] == TRANSFER_REFUSED


def test_the_render_worker_cannot_put_or_mark_a_slice_key() -> None:
    """The render worker's pod runs template Python, so its credential must not mark a
    slicer's input as an output, nor put one: `crud_render` refuses a ``slice-`` key for
    every index write (``put``, ``swap``, ``mark`` and both deletes call this check). The
    routes themselves need Postgres: `tests/crud/test_crud_blob_index.py`."""
    with pytest.raises(ApiError) as refused:
        crud_render._worker_writable("piece-" + "a" * 64, KEY)
    assert refused.value.status == 403
    assert refused.value.extensions["reason"] == crud_render.API_OWNED_KEY
    crud_render._worker_writable("piece-" + "a" * 64)


@pytest.mark.parametrize(
    "failure",
    [
        ClientError(
            {"Error": {"Code": "SlowDown", "Message": "s3://secret-bucket/slice-x said no"}},
            "HeadObject",
        ),
        httpx.ConnectError("bambuddy.lan:8000 secret refused"),
    ],
)
async def test_a_backend_that_fails_the_read_is_store_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    """An S3 error other than a missing object, or a Bambuddy that does not answer: a 503
    with a fixed reason, never the backend's text, and logged by the error's type only."""
    caplog.set_level("WARNING", logger=crud_print.__name__)
    content = _Content({KEY: (SLICE_KIND, SLICED)}, meta=dict(SLICED_OUTPUT_META), failure=failure)
    response = await _ask(_app(tmp_path, content), KEY)
    assert response.status_code == 503
    assert response.json()["reason"] == STORE_UNAVAILABLE
    assert "secret" not in response.text
    [record] = [r for r in caplog.records if r.name == crud_print.__name__]
    assert type(failure).__name__ in record.getMessage()
    assert "secret" not in record.getMessage() and record.exc_info is None


async def test_remote_blobs_retries_store_unavailable_as_a_passing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike ``not_configured``, the client retries it within its budget, then raises
    `CrudUnavailableError`, which the LAN upload's retry policy tries again. The budget
    runs on a fake clock that only the backoffs move (0.25 s, then the 0.25 s left), so
    the client makes three tries however long each one takes (#6252)."""
    monkeypatch.setattr(output_reader, "TRANSFER_URL_BUDGET", 0.5)
    content = _Content({}, failure=ClientError({"Error": {"Code": "SlowDown"}}, "HeadObject"))
    clock = _Clock()
    with pytest.raises(CrudUnavailableError) as raised:
        await _read(_blobs(_app(tmp_path, content), clock=clock), KEY)
    assert raised.value.problem["reason"] == STORE_UNAVAILABLE
    assert content.asked == [KEY] * 3
    assert clock.now == 0.5


def test_the_read_urls_wait_outlasts_a_slow_store_and_stays_bounded() -> None:
    """A slow Bambuddy ``stat`` still answers within a try (N8 of the Task 32 review: a
    5 s try timed every try out), and the whole wait stays far inside the upload's
    start-to-close timeout; the heartbeat, not the budget, keeps it under `HEARTBEAT`."""
    timeout = output_reader.TRANSFER_URL_TIMEOUT
    assert timeout.read is not None and timeout.read >= 20
    assert HEARTBEAT.total_seconds() > output_reader.TRANSFER_URL_HEARTBEAT
    phases = [timeout.pool, timeout.connect, timeout.write, timeout.read]
    worst = output_reader.TRANSFER_URL_BUDGET + sum(p or 0 for p in phases)
    assert worst < LAN_UPLOAD_TIMEOUT.total_seconds()


async def test_the_read_urls_wait_heartbeats_the_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the LAN upload the wait for the URL heartbeats first and while a slow API
    answers, so it never ends the attempt as a heartbeat timeout."""
    monkeypatch.setattr(output_reader, "TRANSFER_URL_HEARTBEAT", 0.05)
    content = _live(KEY)
    slow_stat = content.stat

    async def stat(key: str) -> BlobStat | None:
        await asyncio.sleep(0.3)
        return await slow_stat(key)

    content.stat = stat  # type: ignore[method-assign]
    beats: list[Any] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    assert await env.run(_read, _blobs(_app(tmp_path, content)), KEY) == SLICED
    assert len(beats) >= 3


async def test_remote_blobs_raises_another_callers_refusal_as_api_error(tmp_path: Path) -> None:
    """Only ``transfer-refused`` reads as a file no longer stored; a ``wrong_caller`` 403
    stays the API's problem."""
    content = _live(KEY)
    with pytest.raises(ApiError) as raised:
        await _read(_blobs(_app(tmp_path, content), "projects"), KEY)
    assert not isinstance(raised.value, BlobMissingAnswerError)
    assert raised.value.status == 403
    assert content.asked == []


async def test_another_callers_credential_is_refused(tmp_path: Path) -> None:
    content = _live(KEY)
    response = await _ask(_app(tmp_path, content), KEY, "projects")
    assert response.status_code == 403
    assert response.json()["reason"] == "wrong_caller"
    # Refused before the allow rule is asked.
    assert content.asked == []


async def test_a_live_slice_gets_the_internal_routes_url(tmp_path: Path) -> None:
    now = datetime(2026, 11, 13, 12, 0, 0, 500_000, tzinfo=UTC)
    response = await _ask(_app(tmp_path, _live(KEY), now=now), KEY)
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


async def test_a_swept_rows_live_slice_still_gets_a_url(tmp_path: Path) -> None:
    """A LAN run's later plate, past the slice sweep (6 h after the slice finished): no
    `native_slices` row names the key any more, but its blob is live in the content index
    until the content sweep takes it, and that is what the rule reads (review finding 1).
    The app has no slice store at all, so no row could answer."""
    assert await _read(_blobs(_app(tmp_path, _live(KEY))), KEY) == SLICED


async def test_the_url_reads_the_blob_through_the_internal_route(tmp_path: Path) -> None:
    app = _app(tmp_path, _live(KEY))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        minted = TransferUrl.model_validate((await _ask(app, KEY)).json())
        read = await client.get(minted.url, headers=minted.headers)
        assert read.status_code == 200 and read.content == SLICED
        assert urlsplit(minted.url).path == f"{INTERNAL_BLOBS}/{KEY}"
        # The token is for its one key: another blob's path refuses it.
        other = await client.get(f"{INTERNAL}{INTERNAL_BLOBS}/slice-other", headers=minted.headers)
        assert other.status_code == 403


async def test_an_api_with_no_content_store_is_not_configured(tmp_path: Path) -> None:
    response = await _ask(_app(tmp_path, _live(KEY), configured=False), KEY)
    assert response.status_code == 503
    assert response.json()["reason"] == "not_configured"


async def test_remote_blobs_reads_a_refusal_as_a_file_no_longer_stored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The LAN upload reports `BlobMissingAnswerError` in plain words (410, "no longer
    stored"); the key is only in a DEBUG line."""
    content = _live()
    caplog.set_level("DEBUG", logger=output_reader.__name__)
    with pytest.raises(BlobMissingAnswerError):
        await _read(_blobs(_app(tmp_path, content)), KEY)
    # Asked once: a refusal is not sent again.
    assert content.asked == [KEY]
    [record] = [r for r in caplog.records if r.name == output_reader.__name__]
    assert record.levelname == "DEBUG" and KEY in record.getMessage()


async def test_remote_blobs_raises_the_apis_not_configured(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as raised:
        await _read(_blobs(_app(tmp_path, _live(KEY), configured=False)), KEY)
    assert raised.value.status == 503


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
