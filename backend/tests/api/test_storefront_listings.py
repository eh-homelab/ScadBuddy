"""The back office's storefront listings (#5282, spec 2026-11-02 §5.3): the routes under
``/api/v1/storefront/listings``, and publish, which checks a listing against its commit's
schema and holds the commit's source snapshot and the listing's media."""

from __future__ import annotations

import asyncio
import base64
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.orders.storefront import listings as listings_module
from scadbuddy.orders.storefront.listings import HOLDER
from scadbuddy.store.content import ContentStore, sweep_content
from scadbuddy.store.refs import BlobRefs
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.test_media import PNG
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

LISTINGS = "/api/v1/storefront/listings"
BOOK = "/api/v1/orders/price-book"
SOURCE = (
    "width = 50; // [10:1:100]\n"
    "height = 5;\n"
    'colour = "#FF0000"; // color\n'
    'shape = "round"; // [round, square]\n'
    "// %%LISTING%%\n"
    "cube([width, width, height]);\n"
)


@pytest.fixture
def kek_file(tmp_path: Path) -> Path:
    path = tmp_path / "kek"
    path.write_text(base64.b64encode(os.urandom(32)).decode())
    return path


@pytest.fixture
def client(settings: Settings, s3: S3Settings, kek_file: Path) -> Iterator[TestClient]:
    """The app on an S3 store: the storefront needs one (plan "Global Constraints")."""
    on_s3 = settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "secret_key_file": kek_file,
        }
    )
    with TestClient(create_app(on_s3)) as test_client:
        yield test_client


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _content(client: TestClient) -> ContentStore:
    content = _state(client).store.content
    assert content is not None
    return content


@pytest.fixture
def template(client: TestClient) -> dict[str, Any]:
    """The demo template, with one image; its slug, head commit and media id."""
    created = client.post(
        "/api/v1/models", json={"name": "Demo", "source": SOURCE}, headers=press()
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    files = {"file": ("photo.png", PNG, "application/octet-stream")}
    added = client.post(f"/api/v1/models/{slug}/media", files=files, headers=press())
    assert added.status_code == 200, added.text
    model = client.get(f"/api/v1/models/{slug}").json()
    return {"slug": slug, "commit": model["version"], "media_id": model["media"][0]["id"]}


@pytest.fixture
def pla_red(client: TestClient) -> str:
    made = client.post(
        f"{BOOK}/filaments", json={"material": "PLA", "colour": "#FF0000", "cost_per_kg": "20"}
    )
    assert made.status_code in (200, 201), made.text
    filament_id: str = made.json()["id"]
    return filament_id


def listing_body(template: dict[str, Any], pla_red: str, **fields: Any) -> dict[str, Any]:
    return {
        "slug": template["slug"],
        "commit": template["commit"],
        "title": "Demo",
        "description": "A box <b>not bold</b>",
        "params": {"width": {"kind": "number", "min": 20, "max": 80, "step": 1}},
        "colours": [pla_red],
        "media_ids": [template["media_id"]],
        **fields,
    }


def create(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    made = client.post(LISTINGS, json=body)
    assert made.status_code == 201, made.text
    listing: dict[str, Any] = made.json()
    return listing


def publish(client: TestClient, listing_id: str) -> dict[str, Any]:
    published = client.post(f"{LISTINGS}/{listing_id}/publish")
    assert published.status_code == 200, published.text
    listing: dict[str, Any] = published.json()
    return listing


def holders(client: TestClient, key: str) -> list[tuple[str, str]]:
    with BlobRefs(_state(client).projection.pool).pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_kind, holder_id FROM blob_refs WHERE key = %s", (key,)
        ).fetchall()
    return [(row["holder_kind"], row["holder_id"]) for row in rows]


def test_publish_snapshots_the_schema_and_holds_the_source(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = create(client, listing_body(template, pla_red))
    published = publish(client, listing["id"])
    snapshot = published["snapshot"]
    assert published["published"] is True and published["published_at"] is not None
    assert (snapshot["template_name"], snapshot["pipeline"]) == ("Demo", False)
    assert [p["name"] for p in snapshot["schema"]["parameters"]] == [
        "width",
        "height",
        "colour",
        "shape",
    ]
    # Every parameter the listing does not name is fixed at its default (plan point 13),
    # in the snapshot; the row's `fixed` keeps only the shop's own locks.
    assert published["fixed"] == {}
    assert snapshot["fixed"] == {"height": 5, "colour": "#FF0000", "shape": "round"}
    assert holders(client, snapshot["snapshot_key"]) == [(HOLDER, listing["id"])]
    [media] = published["media"]
    assert media["blob_key"].startswith("listing-media-") and media["kind"] == "image"
    assert holders(client, media["blob_key"]) == [(HOLDER, listing["id"])]
    assert client.portal is not None
    stored = client.portal.call(_content(client).stat, media["blob_key"])
    assert stored is not None and stored.ref.kind == "media"


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        (
            {"params": {"width": {"kind": "number", "min": 0, "max": 500, "step": None}}},
            "listing_bounds_outside_schema",
        ),
        (
            {"params": {"width": {"kind": "text", "max_length": 10}}},
            "listing_bounds_outside_schema",
        ),
        (
            {"params": {"shape": {"kind": "choice", "choices": ["round", "hexagon"]}}},
            "listing_bounds_outside_schema",
        ),
        ({"params": {"depth": {"kind": "text", "max_length": 10}}}, "listing_param_unknown"),
        ({"fixed": {"nope": 1}}, "listing_param_unknown"),
        ({"fixed": {"width": 30}}, "listing_fixed_invalid"),
        ({"fixed": {"shape": "hexagon"}}, "listing_fixed_invalid"),
        ({"colours": ["no-such-filament"]}, "listing_colour_unknown"),
        (
            {"params": {"colour": {"kind": "choice", "choices": ["#00FF00"]}}},
            "listing_colour_unknown",
        ),
        ({"media_ids": ["nope"]}, "listing_media_unknown"),
        # A locked colour is a listing filament's, set or defaulted.
        ({"fixed": {"colour": "#00FF00"}}, "listing_colour_unknown"),
        ({"colours": []}, "listing_colour_unknown"),
    ],
)
def test_a_listing_its_commit_does_not_back_is_refused(
    client: TestClient,
    template: dict[str, Any],
    pla_red: str,
    fields: dict[str, Any],
    problem: str,
) -> None:
    listing = create(client, listing_body(template, pla_red, **fields))
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + problem
    assert client.get(LISTINGS).json()["items"][0]["published"] is False


def test_a_colour_parameter_offers_the_listings_filaments(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    body = listing_body(
        template, pla_red, params={"colour": {"kind": "choice", "choices": ["#ff0000"]}}
    )
    assert publish(client, create(client, body)["id"])["published"] is True


async def _sweep(client: TestClient) -> list[str]:
    content = _content(client)
    with _state(client).projection.pool.connection() as conn:
        conn.execute("UPDATE store_blobs SET touched_at = now() - interval '1 day'")
    return await sweep_content(content, BlobRefs(_state(client).projection.pool), grace=60)


def test_the_held_snapshot_survives_the_blob_sweep(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    published = publish(client, create(client, listing_body(template, pla_red))["id"])
    key = published["snapshot"]["snapshot_key"]
    media_key = published["media"][0]["blob_key"]
    assert client.portal is not None
    swept = client.portal.call(_sweep, client)
    assert key not in swept and media_key not in swept
    assert client.portal.call(_content(client).stat, key) is not None
    # Unpublished, nothing holds them: the next sweep takes them.
    unpublished = client.post(f"{LISTINGS}/{published['id']}/unpublish")
    assert unpublished.status_code == 200, unpublished.text
    assert unpublished.json()["published"] is False
    assert holders(client, key) == []
    assert {key, media_key} <= set(client.portal.call(_sweep, client))


def test_the_listing_routes_round_trip(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    made = create(client, listing_body(template, pla_red))
    other = create(client, listing_body(template, pla_red, title="Other"))
    assert client.post(f"{LISTINGS}/{made['id']}/publish").json()["published"] is True
    assert client.delete(f"{LISTINGS}/{made['id']}").status_code == 409
    listed = client.get(LISTINGS).json()
    assert [listing["id"] for listing in listed["items"]] == [made["id"], other["id"]]
    assert (listed["total"], listed["places"], listed["limit"], listed["offset"]) == (2, 2, 50, 0)
    # The order is the one given, and survives a read.
    ordered = client.put(f"{LISTINGS}/order", json={"ids": [other["id"], made["id"]]})
    assert ordered.status_code == 200, ordered.text
    assert [row["id"] for row in client.get(LISTINGS).json()["items"]] == [other["id"], made["id"]]
    assert client.put(f"{LISTINGS}/order", json={"ids": [made["id"]]}).status_code == 409
    # One listing moves by its place alone.
    moved = client.post(f"{LISTINGS}/{made['id']}/move", json={"place": 1})
    assert moved.status_code == 200, moved.text
    assert [row["id"] for row in client.get(LISTINGS).json()["items"]] == [made["id"], other["id"]]
    assert client.post(f"{LISTINGS}/nope/move", json={"place": 1}).status_code == 404
    assert client.post(f"{LISTINGS}/{made['id']}/move", json={"place": 0}).status_code == 422
    # The list filters, and refuses a query it does not know.
    drafts = client.get(LISTINGS, params={"status": "draft", "q": "OTH"}).json()
    assert [row["id"] for row in drafts["items"]] == [other["id"]]
    assert client.get(LISTINGS, params={"limit": 201}).status_code == 422
    # An edit names the time it read the listing, and unpublishes it.
    current = client.get(f"{LISTINGS}/{made['id']}").json()
    assert client.get(f"{LISTINGS}/nope").status_code == 404
    edited = client.put(
        f"{LISTINGS}/{made['id']}",
        json={
            "listing": listing_body(template, pla_red, title="New"),
            "updated_at": current["updated_at"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["title"], edited.json()["published"]) == ("New", False)
    stale = client.put(
        f"{LISTINGS}/{made['id']}",
        json={"listing": listing_body(template, pla_red), "updated_at": current["updated_at"]},
    )
    assert stale.status_code == 409
    assert stale.json()["type"] == STOREFRONT_PROBLEMS + "listing_changed"
    assert client.delete(f"{LISTINGS}/{made['id']}").status_code == 204
    assert client.post(f"{LISTINGS}/{made['id']}/publish").status_code == 404
    assert client.delete(f"{LISTINGS}/{made['id']}").status_code == 404


def test_the_routes_refuse_a_listing_of_the_wrong_shape(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    body = listing_body(template, pla_red, commit="abc1234")
    assert client.post(LISTINGS, json=body).status_code == 422
    body = listing_body(template, pla_red, title="x" * 201)
    assert client.post(LISTINGS, json=body).status_code == 422


def test_a_choice_with_nothing_to_choose_says_so(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    """A colour parameter offered with every filament unchecked has no choice: the
    refusal names the parameter and says to choose a filament, not Pydantic's text."""
    params = {"colour": {"kind": "choice", "choices": []}}
    refused = client.post(LISTINGS, json=listing_body(template, pla_red, params=params))
    assert refused.status_code == 422, refused.text
    problem = refused.json()
    assert problem["type"] == STOREFRONT_PROBLEMS + "listing_choice_empty"
    assert problem["name"] == "colour"
    assert "choose at least one filament" in problem["detail"]
    made = create(client, listing_body(template, pla_red))
    refused = client.put(
        f"{LISTINGS}/{made['id']}",
        json={
            "listing": listing_body(template, pla_red, params=params),
            "updated_at": made["updated_at"],
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_choice_empty"


def test_a_commit_that_is_not_the_templates_is_refused(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    listing = create(client, listing_body(template, pla_red, commit="f" * 40))
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_reference"


def test_publish_needs_an_s3_store(settings: Settings) -> None:
    with TestClient(create_app(settings)) as local:
        model = local.post(
            "/api/v1/models", json={"name": "Demo", "source": SOURCE}, headers=press()
        ).json()
        made = local.post(
            LISTINGS, json={"slug": model["slug"], "commit": model["version"], "title": "Demo"}
        ).json()
        refused = local.post(f"{LISTINGS}/{made['id']}/publish")
    assert refused.status_code == 409
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "storefront_needs_s3"


def listing_holds(client: TestClient, listing_id: str) -> set[str]:
    with _state(client).projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (HOLDER, listing_id),
        ).fetchall()
    return {row["key"] for row in rows}


def test_a_republish_at_a_new_version_takes_its_defaults(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    """Publish at A, then at B where `height`'s default moved: B's default is the one a
    render uses, and `fixed` still holds only what the shop locked."""
    slug = template["slug"]
    body = listing_body(template, pla_red, fixed={"shape": "square"})
    listing = create(client, body)
    first = publish(client, listing["id"])
    assert first["snapshot"]["fixed"]["height"] == 5
    saved = client.put(
        f"/api/v1/models/{slug}/source",
        json={"source": SOURCE.replace("height = 5;", "height = 8;")},
        headers=press(),
    )
    assert saved.status_code == 200, saved.text
    commit = client.get(f"/api/v1/models/{slug}").json()["version"]
    assert commit != template["commit"]
    edited = client.put(
        f"{LISTINGS}/{listing['id']}",
        json={
            "listing": {**body, "commit": commit, "fixed": first["fixed"]},
            "updated_at": first["updated_at"],
        },
    )
    assert edited.status_code == 200, edited.text
    second = publish(client, listing["id"])
    assert second["fixed"] == {"shape": "square"}
    assert second["snapshot"]["fixed"] == {"height": 8, "colour": "#FF0000", "shape": "square"}


def test_a_hidden_colour_needs_no_listing_filament(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    """A `// color` parameter in the Hidden group has no row in the editor, and no
    listing filament need have its colour: the schema leaves it out (`build_schema`), so
    publish neither checks nor fixes it, and a render takes it from the source."""
    slug = template["slug"]
    hidden = SOURCE.replace(
        "// %%LISTING%%\n", '// %%LISTING%%\n/* [Hidden] */\ntrim = "#00FF00"; // color\n'
    )
    saved = client.put(f"/api/v1/models/{slug}/source", json={"source": hidden}, headers=press())
    assert saved.status_code == 200, saved.text
    commit = client.get(f"/api/v1/models/{slug}").json()["version"]
    listing = create(client, listing_body(template, pla_red, commit=commit))
    published = publish(client, listing["id"])
    assert "trim" not in published["snapshot"]["fixed"]


def test_a_failed_republish_leaves_only_the_published_holds(
    client: TestClient, template: dict[str, Any], pla_red: str
) -> None:
    slug = template["slug"]
    files = {"file": ("second.png", PNG + b"\x01", "application/octet-stream")}
    assert (
        client.post(f"/api/v1/models/{slug}/media", files=files, headers=press()).status_code == 200
    )
    first, second = (m["id"] for m in client.get(f"/api/v1/models/{slug}").json()["media"])
    body = listing_body(template, pla_red, media_ids=[first, second])
    listing = create(client, body)
    published = publish(client, listing["id"])
    held = listing_holds(client, listing["id"])
    assert held == {
        published["snapshot"]["snapshot_key"],
        *(m["blob_key"] for m in published["media"]),
    }
    # The first file changes (a new copy, a new key) and the second goes: the republish
    # copies the first, then fails on the second.
    _view, path = _state(client).catalogue.media_item(slug, first)
    path.write_bytes(PNG + b"\x02")
    assert client.delete(f"/api/v1/models/{slug}/media/{second}", headers=press()).status_code in (
        200,
        204,
    )
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_media_unknown"
    assert listing_holds(client, listing["id"]) == held


def test_a_pipeline_template_is_not_listed_yet(
    client: TestClient, template: dict[str, Any], pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(listings_module, "declares_pipeline", lambda directory, slug: True)
    listing = create(client, listing_body(template, pla_red))
    refused = client.post(f"{LISTINGS}/{listing['id']}/publish")
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == STOREFRONT_PROBLEMS + "listing_pipeline_unsupported"
    assert listing_holds(client, listing["id"]) == set()


def test_a_second_publish_of_a_listing_waits_for_none(
    client: TestClient, template: dict[str, Any], pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two publishes of one listing at once: the second is refused while the first runs
    (409 ``listing_publishing``) and touches no hold, so every blob the winner's row
    names is still held."""
    listing = create(client, listing_body(template, pla_red))
    copy = listings_module._copy_media
    started, release = threading.Event(), threading.Event()
    calls = 0

    async def held_up(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.to_thread(release.wait, 30)
        return await copy(*args, **kwargs)

    monkeypatch.setattr(listings_module, "_copy_media", held_up)
    answers: dict[str, Any] = {}
    first = threading.Thread(
        target=lambda: answers.update(first=client.post(f"{LISTINGS}/{listing['id']}/publish"))
    )
    first.start()
    try:
        assert started.wait(30)
        second = client.post(f"{LISTINGS}/{listing['id']}/publish")
    finally:
        release.set()
        first.join(60)
    assert second.status_code == 409, second.text
    assert second.json()["type"] == STOREFRONT_PROBLEMS + "listing_publishing"
    won = answers["first"]
    assert won.status_code == 200, won.text
    row = won.json()
    keys = {row["snapshot"]["snapshot_key"], *(m["blob_key"] for m in row["media"])}
    assert listing_holds(client, listing["id"]) == keys
