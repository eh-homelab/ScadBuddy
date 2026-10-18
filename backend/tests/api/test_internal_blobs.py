"""The internal blob routes the slicer worker downloads its input and uploads its output
through, and the transfer URLs the API mints for them (plan
2026-10-16-native-slicer-rust-worker Task 10). Every request is made with
``follow_redirects=False``: the worker follows none, and treats a 3xx as refused."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy.preview import MAX_SLICED_BYTES
from scadbuddy.core.secrets import load_kek
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.slicing.models import TransferUrl
from scadbuddy.slicing.transfer import (
    INTERNAL_BLOBS,
    PENDING_PREFIX,
    TRANSFER_HEADER,
    TRANSFER_TTL,
    PendingPut,
    TransferPuts,
    TransferRecordError,
    Transfers,
    TransferUnavailableError,
)
from scadbuddy.slicing.transfer_token import TokenError, TransferToken, sign, transfer_key, verify
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobMissingError, BlobScope
from scadbuddy.store.index import Pool
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

INTERNAL = "http://scadbuddy"
SCOPE = BlobScope(folder="work")


@pytest.fixture
def kek_file(tmp_path: Path) -> Path:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(os.urandom(32)).decode())
    return path


@pytest.fixture
def blob_client(settings: Settings, s3: S3Settings, kek_file: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            settings.model_copy(
                update={
                    "store_backend": "s3",
                    "store_s3_endpoint": s3.endpoint,
                    "store_s3_bucket": s3.bucket,
                    "store_s3_prefix": s3.prefix,
                    "store_s3_access_key_id": s3.access_key_id,
                    "store_s3_secret_access_key": s3.secret_access_key,
                    "secret_key_file": kek_file,
                    "api_internal_url": INTERNAL,
                }
            )
        )
    ) as client:
        yield client


def _state(client: TestClient) -> Any:
    return getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]


def _content(client: TestClient) -> ContentStore:
    content = _state(client).store.content
    assert isinstance(content, ContentStore)
    return content


def _secret(client: TestClient) -> bytes:
    return transfer_key(load_kek(_state(client).settings.secret_key_file))


def _transfers(client: TestClient, now: Callable[[], datetime] | None = None) -> Transfers:
    state = _state(client)
    return Transfers(
        _content(client),
        TransferPuts(state.projection.pool),
        internal_base_url=INTERNAL,
        secret=_secret(client),
        **({} if now is None else {"now": now}),
    )


def _put(client: TestClient, data: bytes) -> str:
    assert client.portal is not None
    ref = client.portal.call(partial(_content(client).put, "slice", data, name="x", scope=SCOPE))
    return f"slice-{ref.sha256}"


def _signed(client: TestClient, method: str, key: str) -> dict[str, str]:
    token = TransferToken(
        method=method,  # type: ignore[arg-type]
        blob_key=key,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    return {TRANSFER_HEADER: sign(_secret(client), token)}


def _path(url: TransferUrl) -> str:
    assert url.url.startswith(f"{INTERNAL}{INTERNAL_BLOBS}/")
    return urlsplit(url.url).path


def _get(client: TestClient, path: str, headers: dict[str, str]) -> httpx.Response:
    response: httpx.Response = client.get(path, headers=headers, follow_redirects=False)
    return response


def _upload(client: TestClient, path: str, headers: dict[str, str], body: bytes) -> httpx.Response:
    response: httpx.Response = client.put(
        path, headers=headers, content=body, follow_redirects=False
    )
    return response


def _finish(client: TestClient, put: PendingPut, data: bytes) -> str:
    assert client.portal is not None
    key: str = client.portal.call(
        partial(
            _transfers(client).finish_put,
            put,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
    )
    return key


def test_a_minted_get_reads_the_blob(blob_client: TestClient) -> None:
    key = _put(blob_client, b"hello")
    url = _transfers(blob_client).internal_get(key)
    assert url.expires_at > datetime.now(UTC) + TRANSFER_TTL - timedelta(minutes=1)
    response = _get(blob_client, _path(url), url.headers)
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-length"] == "5"
    assert response.headers["content-type"] == "application/octet-stream"


def test_get_without_the_header_is_403(blob_client: TestClient) -> None:
    key = _put(blob_client, b"hello")
    assert _get(blob_client, f"{INTERNAL_BLOBS}/{key}", {}).status_code == 403
    garbage = {TRANSFER_HEADER: "not-a-token"}
    assert _get(blob_client, f"{INTERNAL_BLOBS}/{key}", garbage).status_code == 403


def test_a_get_token_cannot_put(blob_client: TestClient) -> None:
    put = _transfers(blob_client).mint_put()
    path = urlsplit(put.url.url).path
    response = _upload(blob_client, path, _signed(blob_client, "GET", put.pending_key), b"x")
    assert response.status_code == 403


def test_a_put_token_cannot_get(blob_client: TestClient) -> None:
    key = _put(blob_client, b"hello")
    headers = _signed(blob_client, "PUT", key)
    assert _get(blob_client, f"{INTERNAL_BLOBS}/{key}", headers).status_code == 403


def test_a_token_for_key_a_cannot_read_key_b(blob_client: TestClient) -> None:
    a = _transfers(blob_client).internal_get(_put(blob_client, b"aaa"))
    b = _put(blob_client, b"bbb")
    response = _get(blob_client, f"{INTERNAL_BLOBS}/{b}", a.headers)
    assert response.status_code == 403
    assert b"bbb" not in response.content


def test_a_missing_key_is_404_only_after_the_token(blob_client: TestClient) -> None:
    missing = "slice-" + "c" * 64
    url = _transfers(blob_client).internal_get(missing)
    assert _get(blob_client, _path(url), url.headers).status_code == 404
    good = url.headers[TRANSFER_HEADER]
    bad = {TRANSFER_HEADER: good[:-4] + ("AAA=" if good[-4:] != "AAA=" else "BBB=")}
    assert _get(blob_client, _path(url), bad).status_code == 403


def test_mint_get_of_a_missing_key_raises(blob_client: TestClient) -> None:
    assert blob_client.portal is not None
    with pytest.raises(BlobMissingError):
        blob_client.portal.call(_transfers(blob_client).mint_get, "slice-" + "d" * 64)


def test_an_expired_url_is_403(blob_client: TestClient) -> None:
    key = _put(blob_client, b"hello")
    then = datetime.now(UTC) - TRANSFER_TTL - timedelta(seconds=1)
    url = _transfers(blob_client, now=lambda: then).internal_get(key)
    assert _get(blob_client, _path(url), url.headers).status_code == 403
    put = _transfers(blob_client, now=lambda: then).mint_put()
    assert _upload(blob_client, _path(put.url), put.url.headers, b"x").status_code == 403


def test_put_then_finish_put(blob_client: TestClient) -> None:
    put = _transfers(blob_client).mint_put()
    assert put.pending_key.startswith(PENDING_PREFIX)
    assert _path(put.url) == f"{INTERNAL_BLOBS}/{put.pending_key}"
    response = _upload(blob_client, _path(put.url), put.url.headers, b"hello")
    assert response.status_code == 204
    key = _finish(blob_client, put, b"hello")
    assert key == f"slice-{hashlib.sha256(b'hello').hexdigest()}"
    content = _content(blob_client)
    assert blob_client.portal is not None
    stat = blob_client.portal.call(content.stat, key)
    assert stat is not None
    assert blob_client.portal.call(content.read, stat.ref) == b"hello"


def test_a_second_put_to_the_same_pending_key_upserts(blob_client: TestClient) -> None:
    put = _transfers(blob_client).mint_put()
    for body in (b"one", b"two"):
        assert _upload(blob_client, _path(put.url), put.url.headers, body).status_code == 204
    assert _finish(blob_client, put, b"two") == f"slice-{hashlib.sha256(b'two').hexdigest()}"
    with pytest.raises(TransferRecordError):
        _finish(blob_client, put, b"one")


def test_finish_put_refuses_a_wrong_digest_or_nothing_put(blob_client: TestClient) -> None:
    nothing = _transfers(blob_client).mint_put()
    with pytest.raises(TransferRecordError):
        _finish(blob_client, nothing, b"hello")
    put = _transfers(blob_client).mint_put()
    assert _upload(blob_client, _path(put.url), put.url.headers, b"hello").status_code == 204
    assert blob_client.portal is not None
    with pytest.raises(TransferRecordError):
        blob_client.portal.call(
            partial(
                _transfers(blob_client).finish_put,
                put,
                sha256=hashlib.sha256(b"hello").hexdigest(),
                size=4,
            )
        )
    with pytest.raises(TransferRecordError):
        _finish(blob_client, put, b"hellp")


def test_a_put_token_for_another_pending_key_is_403(blob_client: TestClient) -> None:
    a = _transfers(blob_client).mint_put()
    b = _transfers(blob_client).mint_put()
    assert _upload(blob_client, _path(b.url), a.url.headers, b"x").status_code == 403


def test_put_to_a_non_pending_key_is_404(blob_client: TestClient) -> None:
    key = "slice-abc"
    headers = _signed(blob_client, "PUT", key)
    response = _upload(blob_client, f"{INTERNAL_BLOBS}/{key}", headers, b"x")
    assert response.status_code == 404


def test_mint_without_internal_url_or_kek_says_which(blob_client: TestClient) -> None:
    content = _content(blob_client)
    puts = TransferPuts(_state(blob_client).projection.pool)
    secret = _secret(blob_client)
    with pytest.raises(TransferUnavailableError, match="SCADBUDDY_API_INTERNAL_URL"):
        Transfers(content, puts, internal_base_url=None, secret=secret).mint_put()
    with pytest.raises(TransferUnavailableError, match="SCADBUDDY_SECRET_KEY_FILE"):
        Transfers(content, puts, internal_base_url=INTERNAL, secret=None).mint_put()


def test_s3_presigned_get_reads_the_object(blob_client: TestClient) -> None:
    key = _put(blob_client, b"hello")
    assert blob_client.portal is not None
    url = blob_client.portal.call(_transfers(blob_client).mint_get, key)
    assert url.headers == {}
    assert not url.url.startswith(INTERNAL)
    response = httpx.get(url.url, follow_redirects=False)
    assert response.status_code == 200
    assert response.content == b"hello"


def test_mint_get_off_s3_is_the_internal_route(tmp_path: Path, pool: Pool) -> None:
    content = local_content(tmp_path / "blobs", pool)
    secret = bytes(32)
    transfers = Transfers(content, TransferPuts(pool), internal_base_url=INTERNAL, secret=secret)

    async def mint() -> TransferUrl:
        ref = await content.put("slice", b"hello", name="x", scope=SCOPE)
        return await transfers.mint_get(f"slice-{ref.sha256}")

    url = asyncio.run(mint())
    assert url.url.startswith(f"{INTERNAL}{INTERNAL_BLOBS}/slice-")
    assert set(url.headers) == {TRANSFER_HEADER}


def test_without_a_content_store_both_routes_are_404(client: TestClient) -> None:
    key = "slice-" + "e" * 64
    assert _get(client, f"{INTERNAL_BLOBS}/{key}", {}).status_code == 404
    pending = f"{PENDING_PREFIX}{'0' * 32}"
    assert _upload(client, f"{INTERNAL_BLOBS}/{pending}", {}, b"x").status_code == 404


def test_transfer_puts_prune(pool: Pool) -> None:
    puts = TransferPuts(pool)
    puts.record("pending-old", key="slice-a", sha256="a", size=1)
    puts.record("pending-new", key="slice-b", sha256="b", size=2)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE transfer_puts SET created_at = now() - interval '2 days'"
            " WHERE pending_key = 'pending-old'"
        )
    assert puts.prune(timedelta(days=1)) == 1
    assert puts.get("pending-old") is None
    kept = puts.get("pending-new")
    assert kept is not None
    assert (kept.key, kept.sha256, kept.size) == ("slice-b", "b", 2)


def test_a_minted_url_expires_with_its_token(tmp_path: Path, pool: Pool) -> None:
    """The token carries whole seconds, so the URL's ``expires_at`` is floored to match:
    the worker never sends a request the route refuses as expired."""
    at = datetime(2026, 10, 16, 12, 0, 0, 654321, tzinfo=UTC)
    secret = bytes(32)
    transfers = Transfers(
        local_content(tmp_path / "blobs", pool),
        TransferPuts(pool),
        internal_base_url=INTERNAL,
        secret=secret,
        now=lambda: at,
    )
    put = transfers.mint_put()
    for method, key, url in (
        ("GET", "slice-abc", transfers.internal_get("slice-abc")),
        ("PUT", put.pending_key, put.url),
    ):
        assert url.expires_at == datetime(2026, 10, 16, 14, 0, 0, tzinfo=UTC)
        token = url.headers[TRANSFER_HEADER]
        last = url.expires_at - timedelta(microseconds=1)
        assert verify(secret, token, method=method, blob_key=key, now=last)
        with pytest.raises(TokenError):
            verify(secret, token, method=method, blob_key=key, now=url.expires_at)


def test_a_pending_put_never_prints_its_token(blob_client: TestClient) -> None:
    put = _transfers(blob_client).mint_put()
    token = put.url.headers[TRANSFER_HEADER]
    for text in (repr(put), str(put)):
        assert token not in text
        assert put.pending_key in text
    with pytest.raises(ValidationError) as caught:
        PendingPut.model_validate({"url": {**put.url.model_dump(mode="json"), "expires_at": "x"}})
    assert token not in str(caught.value)


def test_a_put_declaring_more_than_a_sliced_3mf_is_413(blob_client: TestClient) -> None:
    put = _transfers(blob_client).mint_put()
    headers = {**put.url.headers, "Content-Length": str(MAX_SLICED_BYTES + 1)}
    response = _upload(blob_client, _path(put.url), headers, b"x")
    assert response.status_code == 413
    assert TransferPuts(_state(blob_client).projection.pool).get(put.pending_key) is None


def test_a_streamed_put_past_a_sliced_3mf_is_413(blob_client: TestClient) -> None:
    """No Content-Length and no Content-Type: the body is counted as it arrives."""
    put = _transfers(blob_client).mint_put()

    def body() -> Iterator[bytes]:
        yield bytes(MAX_SLICED_BYTES + 1)

    response: httpx.Response = blob_client.put(
        _path(put.url), headers=put.url.headers, content=body(), follow_redirects=False
    )
    assert "content-length" not in response.request.headers
    assert "content-type" not in response.request.headers
    assert response.status_code == 413
    assert TransferPuts(_state(blob_client).projection.pool).get(put.pending_key) is None


def test_mint_get_reads_through_the_route_off_s3(blob_client: TestClient, tmp_path: Path) -> None:
    """Off S3 `mint_get` is the internal route's URL, and the route serves it."""
    state = _state(blob_client)
    s3 = state.store.content
    local = local_content(tmp_path / "local", state.projection.pool)
    state.store.content = local
    try:
        assert blob_client.portal is not None
        ref = blob_client.portal.call(partial(local.put, "slice", b"local", name="x", scope=SCOPE))
        transfers = Transfers(
            local,
            TransferPuts(state.projection.pool),
            internal_base_url=INTERNAL,
            secret=_secret(blob_client),
        )
        url = blob_client.portal.call(transfers.mint_get, f"slice-{ref.sha256}")
        response = _get(blob_client, _path(url), url.headers)
        assert response.status_code == 200
        assert response.content == b"local"
    finally:
        state.store.content = s3


def test_finish_put_refuses_a_key_no_longer_stored(blob_client: TestClient) -> None:
    put = _transfers(blob_client).mint_put()
    assert _upload(blob_client, _path(put.url), put.url.headers, b"gone").status_code == 204
    assert blob_client.portal is not None
    blob_client.portal.call(
        _content(blob_client).delete, f"slice-{hashlib.sha256(b'gone').hexdigest()}"
    )
    with pytest.raises(TransferRecordError, match="no longer stored"):
        _finish(blob_client, put, b"gone")
