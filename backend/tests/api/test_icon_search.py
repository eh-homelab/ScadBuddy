"""`GET /icons/search`: open icon catalogues searched through Iconify (#959)."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.library.icon_search import COLLECTIONS, SEARCH_URL

pytestmark = pytest.mark.usefixtures("fake_dns")

#: The shape of Iconify's answer, cut down from a real one (`/search?query=unicorn`,
#: measured 2026-11-03), plus a name Iconify never uses, which is dropped.
ANSWER = {
    "icons": ["mdi:unicorn", "openmoji:unicorn", "game-icons:unicorn", "mdi:../x", "nope:x"],
    "total": 5,
    "limit": 32,
    "start": 0,
    "collections": {
        "mdi": {
            "name": "Material Design Icons",
            "author": {
                "name": "Pictogrammers",
                "url": "https://github.com/Templarian/MaterialDesign",
            },
            "license": {
                "title": "Apache 2.0",
                "spdx": "Apache-2.0",
                "url": "https://github.com/Templarian/MaterialDesign/blob/master/LICENSE",
            },
        },
        "openmoji": {
            "name": "OpenMoji",
            "author": {"name": "OpenMoji", "url": "https://github.com/hfg-gmuend/openmoji"},
            "license": {
                "title": "CC BY-SA 4.0",
                "spdx": "CC-BY-SA-4.0",
                "url": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
        },
        "game-icons": {
            "name": "Game Icons",
            "author": {"name": "GameIcons", "url": "https://github.com/game-icons/icons"},
            "license": {"title": "CC BY 3.0", "spdx": "CC-BY-3.0"},
        },
    },
}


@respx.mock
def test_a_search_answers_each_icon_with_its_licence_and_a_fetchable_url(
    client: TestClient,
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=ANSWER))

    response = client.get("/api/v1/icons/search", params={"q": "unicorn", "limit": 2})

    assert response.status_code == 200, response.text
    sent = route.calls.last.request.url.params
    assert sent["query"] == "unicorn"
    assert sent["prefixes"] == ",".join(COLLECTIONS)
    # Iconify answers 400 below 32; the cut is ours.
    assert sent["limit"] == "32"
    icons = response.json()["icons"]
    assert icons == [
        {
            "id": "mdi:unicorn",
            "name": "unicorn",
            "collection": "mdi",
            "collection_name": "Material Design Icons",
            "author": "Pictogrammers",
            "licence": {
                "spdx": "Apache-2.0",
                "title": "Apache 2.0",
                "url": "https://github.com/Templarian/MaterialDesign/blob/master/LICENSE",
            },
            "url": "https://api.iconify.design/mdi/unicorn.svg",
            "allowlisted": True,
        },
        {
            "id": "openmoji:unicorn",
            "name": "unicorn",
            "collection": "openmoji",
            "collection_name": "OpenMoji",
            "author": "OpenMoji",
            "licence": {
                "spdx": "CC-BY-SA-4.0",
                "title": "CC BY-SA 4.0",
                "url": "https://creativecommons.org/licenses/by-sa/4.0/",
            },
            "url": "https://api.iconify.design/openmoji/unicorn.svg",
            "allowlisted": True,
        },
    ]


@respx.mock
def test_a_url_off_the_allowlist_says_so(client: TestClient) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=ANSWER))
    client.put("/api/v1/settings", json={"asset_fetch_domains": ["openmoji.org"]})

    icons = client.get("/api/v1/icons/search", params={"q": "unicorn"}).json()["icons"]

    assert [icon["allowlisted"] for icon in icons] == [False, False, False]
    # Every name that is not Iconify's shape is dropped, never put in a URL.
    assert [icon["id"] for icon in icons] == [
        "mdi:unicorn",
        "openmoji:unicorn",
        "game-icons:unicorn",
    ]


def test_a_catalogue_that_cannot_be_reached_is_a_502(client: TestClient) -> None:
    with respx.mock:
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
        response = client.get("/api/v1/icons/search", params={"q": "unicorn"})
    assert response.status_code == 502
    assert "api.iconify.design" in response.json()["detail"]


def test_a_redirect_is_not_followed(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(SEARCH_URL).mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/x"})
        )
        elsewhere = mock.get("https://evil.example/x").mock(return_value=httpx.Response(200))
        response = client.get("/api/v1/icons/search", params={"q": "unicorn"})
    assert response.status_code == 502
    assert not elsewhere.called


def test_an_empty_query_is_refused(client: TestClient) -> None:
    assert client.get("/api/v1/icons/search", params={"q": " "}).status_code == 422
