# ruff: noqa: F811  (the fixtures imported below are the tests' parameters)
"""A listing's pictures set on their own (#6213): ``PUT
/api/v1/storefront/listings/{id}/media``. A draft stays a draft; a published listing is
published again with the new pictures and stays live."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.orders.storefront.listings import HOLDER
from scadbuddy.orders.storefront.wire import LISTING_MEDIA_MAX
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.test_media import MP4, PNG
from tests.api.test_storefront_listings import (  # noqa: F401  (fixtures)
    BOOK,
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
    # The list's row carries every picture and the `updated_at` to set them with.
    [row] = client.get(LISTINGS, params={"slug": template["slug"]}).json()["items"]
    assert (row["media_ids"], row["updated_at"]) == (changed["media_ids"], changed["updated_at"])


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


def test_an_unknown_picture_is_named_and_too_many_are_refused(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = create(client, listing_body(template, pla_red))
    unknown = _set(client, listing, [template["media_id"], "0123456789ab"])
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["name"] == "0123456789ab"
    too_many = _set(client, listing, [f"{n:012x}" for n in range(LISTING_MEDIA_MAX + 1)])
    assert too_many.status_code == 422, too_many.text
    assert client.get(f"{LISTINGS}/{listing['id']}").json()["media_ids"] == listing["media_ids"]


def test_a_video_is_refused_the_storefront_shows_images_only(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    files = {"file": ("clip.mp4", MP4, "application/octet-stream")}
    added = client.post(f"/api/v1/models/{template['slug']}/media", files=files, headers=press())
    assert added.status_code == 200, added.text
    video = added.json()["media"][-1]
    assert video["kind"] == "video"
    listing = create(client, listing_body(template, pla_red))
    answer = _set(client, listing, [video["id"]])
    assert answer.status_code == 422, answer.text
    assert answer.json()["type"] == STOREFRONT_PROBLEMS + "listing_media_not_image"


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


def test_a_republish_the_stored_listing_fails_is_refused_and_the_last_publish_stays_live(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    # The media PUT republishes through `publish`, with POST …/publish's checks over the
    # stored row: here its one filament was archived after it went live.
    listing = publish(client, create(client, listing_body(template, pla_red))["id"])
    archived = client.post(f"{BOOK}/filaments/{pla_red}/archive")
    assert archived.status_code == 200, archived.text
    second = _second_image(client, template["slug"])

    answer = _set(client, listing, [second])
    assert answer.status_code == 422, answer.text
    assert answer.json()["type"] == STOREFRONT_PROBLEMS + "listing_colour_unknown"

    after = client.get(f"{LISTINGS}/{listing['id']}").json()
    # The new pictures are kept; the public site still shows the last publish.
    assert after["media_ids"] == [second]
    assert after["published"] is True
    assert after["snapshot"] == listing["snapshot"]
    assert after["media"] == listing["media"]
    assert holders(client, listing["snapshot"]["snapshot_key"]) == [(HOLDER, listing["id"])]
    assert holders(client, listing["media"][0]["blob_key"]) == [(HOLDER, listing["id"])]
