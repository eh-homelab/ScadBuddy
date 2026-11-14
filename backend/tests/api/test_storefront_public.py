"""The public site's listings on the CRUD side (#5282, spec 2026-11-02 §5.3, plan Task 6):
``GET /api/v1/internal/crud/storefront/listings`` and ``…/listings/{id}``, for the
``storefront`` credential only, the published listings only, on an S3 store only; and the
reference a listing builds from its snapshot, pinned to the back office's."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from scadbuddy.auth.service_credentials import ServiceCaller, ServiceCredentialStore
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.orders.models import Filament
from scadbuddy.orders.reference import resolve_reference
from scadbuddy.orders.storefront import listings as listings_module
from scadbuddy.orders.storefront.inputs import listing_reference, listing_values
from scadbuddy.orders.storefront.wire import Listing
from scadbuddy.storefront.wire import CLIENT_HEADER, STOREFRONT_PROBLEMS, VISITOR_HEADER
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    BOOK,
    LISTINGS,
    SOURCE,
    _state,
    client,
    create,
    kek_file,
    listing_body,
    pla_red,
    publish,
    template,
)
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

CRUD = "/api/v1/internal/crud/storefront"
#: A visitor id, as the edge mints them.
TOKEN_A = "A" * 21 + "_" + "b" * 20 + "-"


def bearer(api: TestClient, caller: ServiceCaller = "storefront") -> dict[str, str]:
    """An exchanged credential of ``caller``, as the edge holds."""
    pool = _state(api).settings_store.pool
    issued = ServiceCredentialStore(pool).issue(caller, "bootstrap", rate_limited=False)
    return {"Authorization": f"Bearer {issued.token}"}


class CrudCaller:
    """The edge's calls: its credential, a visitor id and the browser's address."""

    def __init__(self, api: TestClient, caller: ServiceCaller = "storefront") -> None:
        self.api = api
        self.headers = bearer(api, caller)

    def get(self, path: str, *, token: str = TOKEN_A) -> Any:
        headers = {**self.headers, VISITOR_HEADER: token, CLIENT_HEADER: "203.0.113.1"}
        return self.api.get(CRUD + path, headers=headers)


class _Heard(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def heard() -> Iterator[list[logging.LogRecord]]:
    """What `listings.py` logs at WARNING or above, heard on its own logger: the app's
    start replaces the root handlers that ``caplog`` hangs on."""
    logger = logging.getLogger(listings_module.__name__)
    handler = _Heard()
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def crud(client: TestClient) -> CrudCaller:  # noqa: F811
    return CrudCaller(client)


@pytest.fixture
def published(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
) -> dict[str, Any]:
    body = listing_body(
        template,
        pla_red,
        params={
            "width": {"kind": "number", "min": 20, "max": 80, "step": 1},
            "colour": {"kind": "choice", "choices": ["#FF0000"]},
        },
    )
    return publish(client, create(client, body)["id"])


@pytest.fixture
def unpublished(
    client: TestClient,  # noqa: F811
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
) -> dict[str, Any]:
    return create(client, listing_body(template, pla_red, title="Draft"))


def test_the_crud_side_lists_only_published_listings(
    crud: CrudCaller, published: dict[str, Any], unpublished: dict[str, Any]
) -> None:
    r = crud.get("/listings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [listing["id"] for listing in body["listings"]] == [published["id"]]
    assert body["requests_enabled"] is False
    refused = crud.get(f"/listings/{unpublished['id']}")
    assert refused.status_code == 404
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_not_found"
    unknown = crud.get("/listings/00000000-0000-0000-0000-000000000000")
    assert unknown.status_code == 404
    assert unknown.json() == {**refused.json(), "instance": unknown.json()["instance"]}


def test_the_public_list_is_cards_a_page_at_a_time(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
) -> None:
    """#6154: a card per listing, in the shop's order, ``limit`` at a time; the cursor
    reads the next page and the last page has none."""
    made = [
        publish(client, create(client, listing_body(template, pla_red, title=f"Box {n}"))["id"])
        for n in range(3)
    ]
    first = crud.get("/listings?limit=2")
    assert first.status_code == 200, first.text
    page = first.json()
    assert [c["id"] for c in page["listings"]] == [m["id"] for m in made[:2]]
    card = page["listings"][0]
    assert set(card) == {"id", "title", "description", "image", "available"}
    assert card["available"] is True
    assert card["image"]["kind"] == "image" and "Signature" in card["image"]["url"]
    assert page["next_cursor"]
    second = crud.get(f"/listings?limit=2&cursor={page['next_cursor']}").json()
    assert [c["id"] for c in second["listings"]] == [made[2]["id"]]
    assert second["next_cursor"] is None


def test_the_public_list_searches_titles_and_takes_wildcards_literally(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    template: dict[str, Any],  # noqa: F811
    pla_red: str,  # noqa: F811
) -> None:
    titles = ["Name Keychain", "Luggage tag", "100% Plant pot"]
    for title in titles:
        publish(client, create(client, listing_body(template, pla_red, title=title))["id"])
    found = crud.get("/listings?q=KEY").json()["listings"]
    assert [c["title"] for c in found] == ["Name Keychain"]
    assert [c["title"] for c in crud.get("/listings?q=0%25").json()["listings"]] == [
        "100% Plant pot"
    ]
    assert crud.get("/listings?q=_").json()["listings"] == []


@pytest.mark.parametrize(
    "cursor",
    [
        "x",
        "5b312c20325d",
        "5b2261222c202262225d",
        "5b747275652c20223030225d",
        "5b312c20226e6f742d616e2d6964225d",
    ],
)
def test_a_cursor_the_shop_never_gave_is_422(crud: CrudCaller, cursor: str) -> None:
    r = crud.get(f"/listings?cursor={cursor}")
    assert r.status_code == 422, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_cursor_invalid"


def test_a_public_listing_shows_what_a_visitor_may_choose(
    crud: CrudCaller,
    published: dict[str, Any],
    pla_red: str,  # noqa: F811
) -> None:
    r = crud.get(f"/listings/{published['id']}")
    assert r.status_code == 200, r.text
    listing = r.json()
    assert (listing["title"], listing["description"]) == ("Demo", "A box <b>not bold</b>")
    assert listing["updated_at"] == published["updated_at"]
    assert listing["params"] == {
        "width": {
            "kind": "number",
            "min": 20,
            "max": 80,
            "step": 1,
            "label": "width",
            "group": "Main",
            "colour": False,
        },
        "colour": {
            "kind": "choice",
            "choices": ["#FF0000"],
            "label": "colour",
            "group": "Main",
            "colour": True,
        },
    }
    assert listing["colours"] == [
        {"id": pla_red, "colour": "#FF0000", "colour_name": None, "material": "PLA"}
    ]
    # Nothing the shop keeps to itself: no price, snapshot or blob key.
    assert set(listing) == {
        "id",
        "title",
        "description",
        "params",
        "colours",
        "media",
        "updated_at",
    }
    [media] = listing["media"]
    assert media["kind"] == "image" and media["caption"] == ""
    # Presigned for an hour (moto's endpoint takes the query-string signature, whose
    # ``Expires`` is the end as a Unix time).
    query = parse_qs(urlsplit(media["url"]).query)
    assert "Signature" in query
    assert abs(int(query["Expires"][0]) - time.time() - 3600) < 60


def test_only_the_storefront_credential_reads_the_listings(
    client: TestClient,  # noqa: F811
    published: dict[str, Any],
) -> None:
    render = CrudCaller(client, "render").get("/listings")
    assert render.status_code == 403
    assert render.json()["reason"] == "wrong_caller"
    assert client.get(f"{CRUD}/listings").status_code == 401


def test_the_listings_need_the_edges_headers(
    client: TestClient,  # noqa: F811
    published: dict[str, Any],
) -> None:
    headers = bearer(client)
    r = client.get(f"{CRUD}/listings", headers={**headers, CLIENT_HEADER: "203.0.113.1"})
    assert r.status_code == 400 and r.json()["type"] == STOREFRONT_PROBLEMS + "visitor_id"
    r = client.get(
        f"{CRUD}/listings/{published['id']}", headers={**headers, VISITOR_HEADER: TOKEN_A}
    )
    assert r.status_code == 400 and r.json()["type"] == STOREFRONT_PROBLEMS + "store_client"


def test_the_crud_side_lists_the_area(crud: CrudCaller) -> None:
    assert "listings" in crud.get("/areas").json()["areas"]


def test_the_crud_side_refuses_a_store_that_is_not_s3(
    settings: Settings, heard: list[logging.LogRecord]
) -> None:
    """The operator's fault, not the visitor's: 503 with a plain text, and the operator's
    text in the log at most once a minute."""
    listings_module._needs_s3_warned = None
    with TestClient(create_app(settings)) as local:
        model = local.post(
            "/api/v1/models", json={"name": "Demo", "source": SOURCE}, headers=press()
        ).json()
        made = local.post(
            LISTINGS, json={"slug": model["slug"], "commit": model["version"], "title": "Demo"}
        ).json()
        caller = CrudCaller(local)
        for path in ("/listings", f"/listings/{made['id']}"):
            refused = caller.get(path)
            assert refused.status_code == 503, refused.text
            problem = refused.json()
            assert problem["type"] == STOREFRONT_PROBLEMS + "storefront_needs_s3"
            assert problem["detail"] == "The shop is not available right now."
            assert problem["reason"] == "not_configured"
    warned = [r for r in heard if "store_backend" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING


def test_the_reference_matches_the_back_offices(
    client: TestClient,  # noqa: F811
    published: dict[str, Any],
    pla_red: str,  # noqa: F811
) -> None:
    listing = Listing.model_validate(
        next(item for item in client.get(LISTINGS).json() if item["id"] == published["id"])
    )
    book = client.get(BOOK).json()
    filaments = {f["id"]: Filament.model_validate(f) for f in book["filaments"]}
    values = listing_values(listing, {"width": 40}, filaments)
    ours = listing_reference(listing, values, {0: pla_red}, filaments=filaments)
    state = _state(client)
    assert client.portal is not None
    theirs = client.portal.call(
        lambda: resolve_reference(
            state.catalogue,
            state.history,
            state.outputs,
            paths=state.paths,
            config=state.config,
            fetcher=None,
            slug=listing.slug,
            commit=listing.commit,
            inputs={"params": values},
            output_id=None,
        )
    )
    assert ours.reference == theirs.reference
    assert ours.reference.inputs["params"]["width"] == 40


def test_a_lost_medium_is_left_out_and_logged(
    client: TestClient,  # noqa: F811
    crud: CrudCaller,
    published: dict[str, Any],
    heard: list[logging.LogRecord],
) -> None:
    key = published["media"][0]["blob_key"]
    with _state(client).projection.pool.connection() as conn:
        conn.execute("DELETE FROM store_blobs WHERE key = %s", (key,))
    r = crud.get(f"/listings/{published['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["media"] == []
    [record] = heard
    assert record.levelno == logging.WARNING
    assert (record.listing_id, record.blob_key) == (published["id"], key)  # type: ignore[attr-defined]
    assert "Signature" not in record.getMessage() and "http" not in record.getMessage()


@pytest.mark.parametrize("listing_id", ["not-a-listing", "a%00b", "%2E%2E"])
def test_a_listing_id_that_is_not_one_is_404(crud: CrudCaller, listing_id: str) -> None:
    r = crud.get(f"/listings/{listing_id}")
    assert r.status_code == 404, r.text
    assert r.json()["type"] == STOREFRONT_PROBLEMS + "listing_not_found"
