"""The storefront's listings as rows (#5282, spec 2026-11-02 §5.3): made, edited under the
time the editor read them, ordered, published and deleted. Publish itself, which reads the
template's commit and the content store, is in ``tests/api/test_storefront_listings.py``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from pydantic import TypeAdapter, ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.storefront.listings import HOLDER, ListingStore, checked_fixed
from scadbuddy.orders.storefront.wire import (
    Listing,
    ListingInput,
    ListingSnapshot,
    NumberBound,
    ParamBound,
)
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.store.refs import BlobRefs
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

HEAD = "a" * 40


def body(**fields: Any) -> ListingInput:
    return ListingInput.model_validate({"slug": "demo", "commit": HEAD, "title": "Demo", **fields})


SNAPSHOT = ListingSnapshot(
    schema=CustomizerSchema(),
    pipeline=False,
    template_name="Demo",
    libraries=[],
    snapshot_key=f"src-demo-{HEAD}",
)


@pytest.fixture
def store(pg_pool: PgPool) -> ListingStore:
    return ListingStore(pg_pool)


@pytest.fixture
def refs(pg_pool: PgPool) -> BlobRefs:
    return BlobRefs(pg_pool)


@pytest.fixture
def published(store: ListingStore, refs: BlobRefs) -> Listing:
    listing = store.create(body())
    refs.add(SNAPSHOT.snapshot_key, HOLDER, listing.id)
    return store.mark_published(
        listing.id,
        fixed={"height": 5},
        snapshot=SNAPSHOT,
        media=[],
        expected_updated_at=listing.updated_at,
    )


@contextmanager
def refused_as(slug: str, status: int = 409) -> Iterator[None]:
    """The block raises the storefront problem ``slug``, with ``status``."""
    with pytest.raises(ApiError) as refused:
        yield
    assert refused.value.type == STOREFRONT_PROBLEMS + slug, refused.value.type
    assert refused.value.status == status


def holders(pool: PgPool, key: str) -> list[tuple[str, str]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_kind, holder_id FROM blob_refs WHERE key = %s", (key,)
        ).fetchall()
    return [(row["holder_kind"], row["holder_id"]) for row in rows]


def test_a_new_listing_goes_last_and_unpublished(store: ListingStore) -> None:
    first = store.create(body(title="One"))
    second = store.create(
        body(title="Two", params={"width": {"kind": "number", "min": 20, "max": 80, "step": 1}})
    )
    assert (first.position, second.position) == (0, 1)
    assert not second.published and second.snapshot is None and second.published_at is None
    assert second.params == {"width": NumberBound(kind="number", min=20, max=80, step=1)}
    assert [listing.id for listing in store.all()] == [first.id, second.id]
    assert store.get(second.id) == second
    assert store.get("nope") is None


def test_only_published_listings_are_listed_as_published(
    store: ListingStore, published: Listing
) -> None:
    store.create(body(title="Draft"))
    assert [listing.id for listing in store.published()] == [published.id]
    assert published.published and published.published_at is not None
    assert published.snapshot == SNAPSHOT
    assert published.fixed == {"height": 5}


def test_editing_a_published_listing_unpublishes_it(
    store: ListingStore, published: Listing, pg_pool: PgPool
) -> None:
    edited = store.update(published.id, body(title="New"), expected_updated_at=published.updated_at)
    assert edited.published is False and edited.title == "New"
    assert edited.snapshot is None and edited.published_at is None and edited.media == []
    # Unpublished, it holds nothing the sweep must keep.
    assert holders(pg_pool, SNAPSHOT.snapshot_key) == []
    with refused_as("listing_changed"):
        store.update(published.id, body(), expected_updated_at=published.updated_at)


def test_an_edit_of_a_listing_that_is_gone_is_404(store: ListingStore) -> None:
    with refused_as("listing_not_found", 404):
        store.update("nope", body(), expected_updated_at=datetime.now(UTC))


def test_the_order_is_the_one_given(store: ListingStore) -> None:
    a, b, c = (store.create(body(title=t)) for t in "abc")
    assert [listing.id for listing in store.reorder([c.id, a.id, b.id])] == [c.id, a.id, b.id]
    assert [listing.position for listing in store.all()] == [0, 1, 2]
    # An order that leaves one out, or names one twice, was made from a stale list.
    for stale in ([c.id, a.id], [c.id, a.id, b.id, "nope"], [a.id, a.id, b.id]):
        with refused_as("listing_changed"):
            store.reorder(stale)


def test_a_published_listing_is_not_deleted(
    store: ListingStore, published: Listing, pg_pool: PgPool
) -> None:
    with refused_as("listing_published"):
        store.delete(published.id)
    store.mark_unpublished(published.id)
    assert holders(pg_pool, SNAPSHOT.snapshot_key) == []
    store.delete(published.id)
    assert store.get(published.id) is None
    with refused_as("listing_not_found", 404):
        store.delete(published.id)


def test_a_publish_against_an_edited_listing_is_refused(
    store: ListingStore, published: Listing
) -> None:
    edited = store.update(published.id, body(title="New"), expected_updated_at=published.updated_at)
    with refused_as("listing_changed"):
        store.mark_published(
            edited.id,
            fixed={},
            snapshot=SNAPSHOT,
            media=[],
            expected_updated_at=published.updated_at,
        )


def test_the_table_refuses_a_published_row_without_a_snapshot(
    store: ListingStore, pg_pool: PgPool
) -> None:
    listing = store.create(body())
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE storefront_listings SET published = true WHERE id = %s", (listing.id,))


@pytest.mark.parametrize(
    "bound",
    [
        {"kind": "text", "max_length": 201},
        {"kind": "choice", "choices": []},
        {"kind": "number", "min": 1},
        {"kind": "colour"},
    ],
)
def test_a_bound_of_the_wrong_shape_is_refused(bound: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ParamBound).validate_python(bound)


def test_customer_text_is_bounded() -> None:
    for fields in ({"title": ""}, {"title": "x" * 201}, {"description": "x" * 5001}):
        with pytest.raises(ValidationError):
            body(**fields)
    with pytest.raises(ValidationError):
        body(commit="abc")


# -- checked against the schema (publish's checks, without the template) ---------------

RED, BLUE = "f-red", "f-blue"
COLOURS = {RED: "#FF0000", BLUE: "#0000FF"}
SCHEMA = CustomizerSchema.model_validate(
    {
        "parameters": [
            {"name": "width", "type": "number", "initial": 50, "min": 10, "max": 100, "step": 0.5},
            {"name": "count", "type": "integer", "initial": 3, "min": 1, "max": 9, "step": 1},
            {"name": "colour", "type": "color", "initial": "#FF0000"},
            {"name": "font", "type": "font", "initial": "Liberation Sans"},
        ]
    }
)


def checked(**fields: Any) -> dict[str, Any]:
    return checked_fixed(SCHEMA, body(colours=[RED], **fields), COLOURS)


@pytest.mark.parametrize(
    "fields",
    [
        # Locked at a colour that is not a listing filament's.
        {"fixed": {"colour": "#00FF00"}},
        # Locked at the schema's default, which no listing filament has.
        {"colours": [BLUE]},
    ],
)
def test_a_locked_colour_must_be_a_listing_filament(fields: dict[str, Any]) -> None:
    with refused_as("listing_colour_unknown", 422):
        checked_fixed(SCHEMA, body(**{"colours": [RED], **fields}), COLOURS)


def test_a_locked_colour_of_a_listing_filament_is_kept() -> None:
    assert checked(fixed={"colour": "#ff0000"})["colour"] == "#ff0000"
    assert checked()["colour"] == "#FF0000"


@pytest.mark.parametrize(
    "bound",
    [
        # A step that is not a whole multiple of the schema's.
        {"width": {"kind": "number", "min": 10, "max": 20, "step": 0.75}},
        # An integer's bounds and step are whole.
        {"count": {"kind": "number", "min": 1.5, "max": 9, "step": 1}},
        {"count": {"kind": "number", "min": 1, "max": 9, "step": 1.5}},
        # A font is never offered, as a file is not.
        {"font": {"kind": "choice", "choices": ["Liberation Sans"]}},
    ],
)
def test_a_bound_outside_the_schema_is_refused(bound: dict[str, Any]) -> None:
    with refused_as("listing_bounds_outside_schema", 422):
        checked(params=bound)


def test_a_step_on_the_schemas_is_kept() -> None:
    fixed = checked(
        params={
            "width": {"kind": "number", "min": 10, "max": 20, "step": 1.5},
            "count": {"kind": "number", "min": 2, "max": 8, "step": 2},
        }
    )
    assert "width" not in fixed and "count" not in fixed


def test_a_failed_publish_keeps_only_the_published_holds(
    store: ListingStore, published: Listing, refs: BlobRefs, pg_pool: PgPool
) -> None:
    # What a republish took before it failed: a new copy of a media file.
    refs.add("listing-media-new", HOLDER, published.id)
    store.release_unheld(published.id)
    assert holders(pg_pool, "listing-media-new") == []
    assert holders(pg_pool, SNAPSHOT.snapshot_key) == [(HOLDER, published.id)]
    # Unpublished, it keeps nothing.
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_listings SET published = false WHERE id = %s", (published.id,)
        )
    store.release_unheld(published.id)
    assert holders(pg_pool, SNAPSHOT.snapshot_key) == []


def test_a_schema_step_of_zero_is_no_step() -> None:
    schema = CustomizerSchema.model_validate(
        {
            "parameters": [
                {"name": "width", "type": "number", "initial": 5, "min": 0, "max": 9, "step": 0}
            ]
        }
    )
    listing = body(params={"width": {"kind": "number", "min": 1, "max": 8, "step": 0.5}})
    assert checked_fixed(schema, listing, {}) == {}


def test_one_publish_of_a_listing_at_a_time(store: ListingStore) -> None:
    listing = store.create(body())
    other = store.create(body(title="Other"))
    end = store.begin_publish(listing.id)
    try:
        with refused_as("listing_publishing"):
            store.begin_publish(listing.id)
        # Another listing is not held up.
        store.begin_publish(other.id)()
    finally:
        end()
    store.begin_publish(listing.id)()
