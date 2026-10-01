"""`POST /models/{slug}/assets/fetch`: an SVG or PNG off an allowlisted URL (#844)."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.library.asset_fetch import DEFAULT_ASSET_FETCH_DOMAINS
from tests.conftest import MODEL_SLUG

ICON_URL = "https://www.svgrepo.com/download/12345/unicorn.svg"
UNICORN_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    b'<path d="M2 2 L22 2 L12 22 Z" onclick="steal()"/><script>alert(1)</script></svg>'
)

# respx only intercepts real transports, so the TestClient's own requests to the app
# pass straight through while the app's outbound fetch is mocked.
pytestmark = pytest.mark.usefixtures("fake_dns", "model")


def _fetch(client: TestClient, url: str, slug: str = MODEL_SLUG) -> httpx.Response:
    response: httpx.Response = client.post(f"/api/v1/models/{slug}/assets/fetch", json={"url": url})
    return response


@respx.mock
def test_an_allowlisted_svg_is_stored_sanitised_like_an_upload(client: TestClient) -> None:
    respx.get(ICON_URL).mock(return_value=httpx.Response(200, content=UNICORN_SVG))

    response = _fetch(client, ICON_URL)

    assert response.status_code == 201, response.text
    asset = response.json()
    assert (asset["kind"], asset["name"]) == ("svg", "unicorn.svg")
    assert asset["source_url"] == ICON_URL
    # The id is a stored upload's: the same metadata and bytes routes serve it.
    meta = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{asset['id']}").json()
    assert meta["id"] == asset["id"]
    content = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{asset['id']}/content").content
    assert b"<path" in content
    assert b"script" not in content and b"onclick" not in content


@respx.mock
def test_a_subdomain_of_an_allowlisted_domain_is_allowed(client: TestClient) -> None:
    url = "https://cdn.game-icons.net/icons/unicorn.svg"
    respx.get(url).mock(return_value=httpx.Response(200, content=UNICORN_SVG))

    assert _fetch(client, url).status_code == 201


def test_a_host_off_the_allowlist_is_refused_before_anything_is_fetched(
    client: TestClient,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r".*").mock(return_value=httpx.Response(200))
        # A suffix match on the name, not on the label, would let this one through.
        for url in ("https://example.com/a.svg", "https://notsvgrepo.com/a.svg"):
            response = _fetch(client, url)
            assert response.status_code == 422, url
            assert "allowlist" in response.json()["detail"]
        assert not route.called


def test_a_redirect_off_the_allowlist_is_refused(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(ICON_URL).mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/x.svg"})
        )
        elsewhere = mock.get("https://evil.example/x.svg").mock(
            return_value=httpx.Response(200, content=UNICORN_SVG)
        )

        response = _fetch(client, ICON_URL)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "evil.example" in detail and "allowlist" in detail
        assert not elsewhere.called


def test_a_plain_http_url_is_refused(client: TestClient) -> None:
    response = _fetch(client, "http://www.svgrepo.com/download/1/a.svg")
    assert response.status_code == 422
    assert "https" in response.json()["detail"]


@respx.mock
def test_content_that_is_not_svg_or_png_is_refused(client: TestClient) -> None:
    # Named .svg, but sniffed: the name is not trusted.
    respx.get(ICON_URL).mock(return_value=httpx.Response(200, content=b"GIF89a...."))

    response = _fetch(client, ICON_URL)

    assert response.status_code == 422
    assert "only SVG and PNG" in response.json()["detail"]


def test_a_fetch_needs_a_model(client: TestClient) -> None:
    assert _fetch(client, ICON_URL, slug="nope").status_code == 404


def test_the_allowlist_starts_on_the_defaults_and_the_user_can_replace_it(
    client: TestClient,
) -> None:
    assert client.get("/api/v1/settings").json()["asset_fetch_domains"] == list(
        DEFAULT_ASSET_FETCH_DOMAINS
    )

    saved = client.put(
        "/api/v1/settings", json={"asset_fetch_domains": ["Example.COM.", "icons.example.org"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["asset_fetch_domains"] == ["example.com", "icons.example.org"]

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/a.svg").mock(
            return_value=httpx.Response(200, content=UNICORN_SVG)
        )
        assert _fetch(client, "https://example.com/a.svg").status_code == 201
        assert _fetch(client, ICON_URL).status_code == 422

    # null puts the defaults back; an empty list allows nothing.
    reset = client.put("/api/v1/settings", json={"asset_fetch_domains": None})
    assert reset.json()["asset_fetch_domains"] == list(DEFAULT_ASSET_FETCH_DOMAINS)
    empty = client.put("/api/v1/settings", json={"asset_fetch_domains": []})
    assert empty.json()["asset_fetch_domains"] == []


@pytest.mark.parametrize(
    "domain", ["https://example.com", "example.com/path", "*.example.com", "localhost", ""]
)
def test_an_allowlist_entry_that_is_not_a_domain_is_refused(
    client: TestClient, domain: str
) -> None:
    response = client.put("/api/v1/settings", json={"asset_fetch_domains": [domain]})
    assert response.status_code == 422, response.text
