"""`POST /models/{slug}/assets/fetch`: an SVG or PNG off an allowlisted URL (#844)."""

from __future__ import annotations

from contextlib import ExitStack

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import IMPORT_CONCURRENCY, STATE_ATTR, ImportPermits
from scadbuddy.api.models import RESOLVER_RETRY_AFTER
from scadbuddy.library import url_import
from scadbuddy.library.asset_fetch import DEFAULT_ASSET_FETCH_DOMAINS
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import GitUnavailableError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.library.url_import import resolve_host as real_resolve_host
from tests.conftest import MODEL_SLUG
from tests.support.operations import press

ICON_URL = "https://openmoji.org/data/color/svg/1F984.svg"
UNICORN_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    b'<path d="M2 2 L22 2 L12 22 Z" onclick="steal()"/><script>alert(1)</script></svg>'
)

# respx only intercepts real transports, so the TestClient's own requests to the app
# pass straight through while the app's outbound fetch is mocked.
pytestmark = pytest.mark.usefixtures("fake_dns", "model")


def _fetch(client: TestClient, url: str, slug: str = MODEL_SLUG) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/models/{slug}/assets/fetch", json={"url": url}, headers=press()
    )
    return response


@respx.mock
def test_an_allowlisted_svg_is_stored_sanitised_like_an_upload(client: TestClient) -> None:
    respx.get(ICON_URL).mock(return_value=httpx.Response(200, content=UNICORN_SVG))

    response = _fetch(client, ICON_URL)

    assert response.status_code == 201, response.text
    asset = response.json()
    assert (asset["kind"], asset["name"]) == ("svg", "1F984.svg")
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
        for url in ("https://example.com/a.svg", "https://notopenmoji.org/a.svg"):
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
    response = _fetch(client, "http://openmoji.org/data/color/svg/1F984.svg")
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
    "domain",
    ["https://example.com", "example.com/path", "*.example.com", "localhost", "", "1.2.3.4"],
)
def test_an_allowlist_entry_that_is_not_a_domain_is_refused(
    client: TestClient, domain: str
) -> None:
    response = client.put("/api/v1/settings", json={"asset_fetch_domains": [domain]})
    assert response.status_code == 422, response.text


def test_a_fetch_over_the_shared_fetch_budget_is_a_503_with_retry_after(
    client: TestClient,
) -> None:
    """Imports and asset fetches share one budget per replica."""
    imports: ImportPermits = getattr(client.app.state, STATE_ATTR).imports  # type: ignore[attr-defined]
    with ExitStack() as held:
        for _ in range(IMPORT_CONCURRENCY):
            held.enter_context(imports.hold())
        oldest = next(iter(imports._taken))
        imports._taken[oldest] -= url_import.IMPORT_TIMEOUT - 10
        with respx.mock(assert_all_called=False) as mock:
            response = _fetch(client, ICON_URL)

    assert response.status_code == 503
    retry_after = response.json()["retry_after"]
    assert 5 <= retry_after <= 10
    assert response.headers["retry-after"] == str(retry_after)
    assert not mock.calls


def test_a_fetch_with_every_resolver_thread_busy_is_a_503_not_the_refusal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(url_import, "resolve_host", real_resolve_host)
    taken = 0
    while url_import._RESOLVER_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        with respx.mock(assert_all_called=False) as mock:
            response = _fetch(client, ICON_URL)
    finally:
        url_import._RESOLVER_SLOTS.release(taken)

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(RESOLVER_RETRY_AFTER)
    assert "resolver" in response.json()["detail"]
    assert not mock.calls


def test_a_stored_allowlist_is_normalised_on_load() -> None:
    """`host_allowed` compares against normalised entries, so a row written by hand
    (or by an older version) is normalised when read, and a bad entry dropped."""
    stored = StoredSettings.model_validate(
        {"asset_fetch_domains": ["Example.COM.", "not a domain", "example.com"]}
    )
    assert stored.allowed_asset_domains() == ("example.com",)


@respx.mock
def test_a_url_that_grows_when_encoded_is_still_answered(client: TestClient) -> None:
    """The request caps the URL as given; the parsed one percent-encodes each
    non-ASCII character, so the answer's `source_url` must not hold it to that cap."""
    url = "https://openmoji.org/" + "é" * 1000 + ".svg"
    respx.get(url__startswith="https://openmoji.org/").mock(
        return_value=httpx.Response(200, content=UNICORN_SVG)
    )

    response = _fetch(client, url)

    assert response.status_code == 201, response.text
    assert len(response.json()["source_url"]) > 2048


@respx.mock
def test_a_fetched_asset_keeps_its_source_licence_and_view_box(client: TestClient) -> None:
    """#959: the asset itself records where it came from and under what licence, so
    its metadata route says so long after the fetch's answer."""
    respx.get(url__startswith=ICON_URL).mock(return_value=httpx.Response(200, content=UNICORN_SVG))
    licence = {
        "spdx": "CC-BY-SA-4.0",
        "name": "CC BY-SA 4.0",
        "url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "author": "OpenMoji",
    }

    response = client.post(
        f"/api/v1/models/{MODEL_SLUG}/assets/fetch",
        json={"url": ICON_URL + "?token=secret", "licence": licence},
        headers=press(),
    )

    assert response.status_code == 201, response.text
    meta = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{response.json()['id']}").json()
    # Never the query, which may carry a token.
    assert meta["source_url"] == ICON_URL
    assert meta["licence"] == licence
    assert (meta["view_box"], meta["path_count"]) == ([0, 0, 24, 24], 1)


@respx.mock
@pytest.mark.requires_git
def test_a_fetch_from_a_known_catalogue_captures_its_licence_and_credits_it_once(
    client: TestClient,
) -> None:
    """#959: OpenMoji's licence without the caller naming it, and one README line."""
    respx.get(ICON_URL).mock(return_value=httpx.Response(200, content=UNICORN_SVG))

    first = _fetch(client, ICON_URL)

    assert first.status_code == 201, first.text
    assert first.json()["credited"] is True
    assert first.json()["licence"]["spdx"] == "CC-BY-SA-4.0"
    stored = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{first.json()['id']}").json()
    assert stored["licence"]["spdx"] == "CC-BY-SA-4.0"
    readme = client.get(f"/api/v1/models/{MODEL_SLUG}/readme").text
    assert "## Credits" in readme
    assert f"- `1F984.svg` from <{ICON_URL}>, by OpenMoji, under [CC-BY-SA-4.0]" in readme
    history = client.get(f"/api/v1/models/{MODEL_SLUG}/versions").json()
    assert history[0]["message"] == "Credit 1F984.svg in the README"

    again = _fetch(client, ICON_URL)
    assert again.status_code == 201, again.text
    assert again.json()["credited"] is True
    assert client.get(f"/api/v1/models/{MODEL_SLUG}/readme").text == readme


@respx.mock
def test_a_fetch_from_elsewhere_is_credited_with_no_licence_stated(client: TestClient) -> None:
    url = "https://icons.example.org/a.svg"
    client.put("/api/v1/settings", json={"asset_fetch_domains": ["example.org"]})
    respx.get(url).mock(return_value=httpx.Response(200, content=UNICORN_SVG))

    response = _fetch(client, url)

    assert response.status_code == 201, response.text
    assert response.json()["licence"] is None
    readme = client.get(f"/api/v1/models/{MODEL_SLUG}/readme").text
    assert f"- `a.svg` from <{url}>, licence not stated" in readme


@respx.mock
def test_a_fetch_is_stored_and_answered_when_the_readme_cannot_be_credited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#959: with no model history the credit is skipped, never a 500 after the store."""

    def unavailable(*args: object, **kwargs: object) -> None:
        raise GitUnavailableError("git is not available")

    monkeypatch.setattr(Catalogue, "write_readme", unavailable)
    respx.get(ICON_URL).mock(return_value=httpx.Response(200, content=UNICORN_SVG))

    response = _fetch(client, ICON_URL)

    assert response.status_code == 201, response.text
    assert response.json()["credited"] is False
    asset = client.get(f"/api/v1/models/{MODEL_SLUG}/assets/{response.json()['id']}")
    assert asset.status_code == 200
