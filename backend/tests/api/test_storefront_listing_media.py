# ruff: noqa: F811  (the fixtures imported below are the tests' parameters)
"""A listing's pictures set on their own (#6213): ``PUT
/api/v1/storefront/listings/{id}/media``. A draft stays a draft; a published listing is
published again with the new pictures and stays live."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.storefront.listings import HOLDER
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.test_media import PNG
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    LISTINGS,
    client,
    create,
    holders,
    kek_file,
    listing_body,
    pla_red,
    publish,
    template,
)
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def _second_image(client: TestClient, slug: str) -> str:
    # Another file's bytes, so its blob is not the first image's.
    files = {"file": ("other.png", PNG + b"\0", "application/octet-stream")}
    added = client.post(f"/api/v1/models/{slug}/media", files=files, headers=press())
    assert added.status_code == 200, added.text
    media_id: str = added.json()["media"][-1]["id"]
    return media_id


def _set(client: TestClient, listing: dict[str, Any], media_ids: list[str]) -> Any:
    return client.put(
        f"{LISTINGS}/{listing['id']}/media",
        json={"media_ids": media_ids, "updated_at": listing["updated_at"]},
    )


def test_a_drafts_pictures_change_and_it_stays_a_draft(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = create(client, listing_body(template, pla_red))
    second = _second_image(client, template["slug"])
    answer = _set(client, listing, [second, template["media_id"]])
    assert answer.status_code == 200, answer.text
    changed = answer.json()
    assert changed["media_ids"] == [second, template["media_id"]]
    assert changed["published"] is False and changed["media"] == []
    # Nothing else moved.
    assert (changed["title"], changed["params"]) == (listing["title"], listing["params"])


def test_a_published_listing_is_published_again_with_the_new_pictures(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = publish(client, create(client, listing_body(template, pla_red))["id"])
    [old] = listing["media"]
    assert old["media_id"] == template["media_id"]
    second = _second_image(client, template["slug"])

    answer = _set(client, listing, [second, template["media_id"]])
    assert answer.status_code == 200, answer.text
    live = answer.json()
    assert live["published"] is True
    assert [m["media_id"] for m in live["media"]] == [second, template["media_id"]]
    assert all(holders(client, m["blob_key"]) == [(HOLDER, live["id"])] for m in live["media"])

    # And taken off again: the dropped picture's hold goes with it.
    removed = _set(client, live, [second])
    assert removed.status_code == 200, removed.text
    after = removed.json()
    assert after["published"] is True
    assert [m["media_id"] for m in after["media"]] == [second]
    assert holders(client, old["blob_key"]) == []


@pytest.mark.parametrize(
    ("ids", "problem"),
    [
        (["nope"], "listing_media_unknown"),
        (["{first}", "{first}"], "listing_media_duplicate"),
    ],
)
def test_pictures_the_template_does_not_back_are_refused(
    client: TestClient,
    template: dict[str, Any],
    pla_red: str,
    ids: list[str],
    problem: str,
) -> None:
    listing = create(client, listing_body(template, pla_red))
    answer = _set(client, listing, [i.format(first=template["media_id"]) for i in ids])
    assert answer.status_code == 422, answer.text
    assert answer.json()["type"] == STOREFRONT_PROBLEMS + problem
    assert client.get(f"{LISTINGS}/{listing['id']}").json()["media_ids"] == listing["media_ids"]


def test_a_stale_read_and_a_missing_listing_are_refused(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = create(client, listing_body(template, pla_red))
    assert _set(client, listing, []).status_code == 200
    stale = _set(client, listing, [template["media_id"]])
    assert stale.status_code == 409, stale.text
    assert stale.json()["type"] == STOREFRONT_PROBLEMS + "listing_changed"
    gone = _set(client, {**listing, "id": "00000000-0000-0000-0000-000000000000"}, [])
    assert gone.status_code == 404, gone.text
