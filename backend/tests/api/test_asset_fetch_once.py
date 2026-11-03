"""A one-off fetch from a host off the asset allowlist (#959): only with the grant of a
person's approval, for that host and URL, once."""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.settings import Settings
from scadbuddy.library.asset_fetch import DEFAULT_ASSET_FETCH_DOMAINS
from scadbuddy.library.fetch_grants import FETCH_GRANT_HEADER, fetch_grant_key, mint
from tests.conftest import MODEL_SLUG
from tests.support.operations import press

pytestmark = pytest.mark.usefixtures("fake_dns", "model")

URL = "https://icons.example.com/a.svg"
HOST = "icons.example.com"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2"><path d="M0 0H2V2Z"/></svg>'


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    return settings.model_copy(update={"secret_key_file": key})


def _grant(client: TestClient, host: str = HOST, url: str = URL) -> str:
    kek = getattr(client.app.state, STATE_ATTR).settings_store.kek  # type: ignore[attr-defined]
    return mint(fetch_grant_key(kek), host, url, now=datetime.now(UTC))


def _fetch(
    client: TestClient,
    url: str = URL,
    *,
    allow_host: str | None = HOST,
    grant: str | None = None,
    key: str | None = None,
) -> httpx.Response:
    headers = press() if key is None else {**press(), "Idempotency-Key": key}
    if grant is not None:
        headers[FETCH_GRANT_HEADER] = grant
    body: dict[str, object] = {"url": url}
    if allow_host is not None:
        body["allow_host"] = allow_host
    response: httpx.Response = client.post(
        f"/api/v1/models/{MODEL_SLUG}/assets/fetch", json=body, headers=headers
    )
    return response


@respx.mock
def test_an_approved_grant_fetches_once_and_leaves_the_allowlist_alone(
    client: TestClient,
) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=SVG))
    grant = _grant(client)

    assert _fetch(client, allow_host=None).status_code == 422
    first = _fetch(client, grant=grant, key="k" * 32)
    assert first.status_code == 201, first.text
    # A re-send of that fetch is the same fetch; another one with the grant is not.
    assert _fetch(client, grant=grant, key="k" * 32).status_code == 201
    spent = _fetch(client, grant=grant)
    assert spent.status_code == 403
    assert "already used" in spent.json()["detail"]
    assert route.call_count == 1
    assert client.get("/api/v1/settings").json()["asset_fetch_domains"] == list(
        DEFAULT_ASSET_FETCH_DOMAINS
    )


def test_the_flag_without_a_grant_is_refused_before_anything_is_fetched(
    client: TestClient,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, content=SVG))
        missing = _fetch(client)
        forged = _fetch(client, grant="e30.e30")
        other_url = _fetch(client, grant=_grant(client, url="https://icons.example.com/b.svg"))
        other_host = _fetch(client, grant=_grant(client, host="evil.example"))
        assert not route.called
    assert [r.status_code for r in (missing, forged, other_url, other_host)] == [403] * 4


@pytest.mark.parametrize(
    ("url", "allow_host"),
    [
        # Where parsers disagree on the host: refused outright.
        ("https://allowed.com\\@evil.com/a.svg", "allowed.com"),
        ("https://allowed.com@evil.com/a.svg", "allowed.com"),
        ("https://user:pw@allowed.com/a.svg", "allowed.com"),
        ("https://allowed.com/a\x01.svg", "allowed.com"),
        # Parsed alike, but the host is evil.com, not the one approved.
        ("https://evil.com#@allowed.com", "allowed.com"),
        ("https://sub.allowed.com/a.svg", "allowed.com"),
    ],
)
def test_the_host_must_be_the_one_this_route_reads_from_the_url(
    client: TestClient, url: str, allow_host: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, content=SVG))
        response = _fetch(client, url, allow_host=allow_host, grant=_grant(client, allow_host, url))
        assert not route.called
    assert response.status_code == 422, response.text
