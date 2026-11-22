"""A storefront listing of a library file, as rows (#6710, spec 2026-11-21 §4, §5.2, §5.4):
the source's shapes, the pin a save keeps or drops, and the holds of the held copy and the
uploaded pictures. Publish itself, which reads the library, is in
``tests/api/test_storefront_file_listings.py``."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.storefront import prepare
from scadbuddy.orders.storefront.file_listings import check_offered, support_locks
from scadbuddy.orders.storefront.listings import HOLDER, KEPT_HOLDER, ListingStore
from scadbuddy.orders.storefront.wire import (
    FileSlot,
    FileSnapshot,
    Listing,
    ListingInput,
    ListingMediaUpdate,
    ListingQuery,
    ListingSnapshot,
)
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.conftest import PgPool
from tests.orders.storefront.test_listings import HEAD, SNAPSHOT, holders

pytestmark = pytest.mark.requires_postgres

SHA = "1" * 64
OTHER = "2" * 64


def file_body(**fields: Any) -> ListingInput:
    return ListingInput.model_validate(
        {
            "source": "library_file",
            "library_file": {"id": 7, "library": "native"},
            "title": "Keychain",
            **fields,
        }
    )


def snapshot(sha: str = SHA, **fields: Any) -> FileSnapshot:
    return FileSnapshot(
        file_id=7,
        library="native",
        filename="keychain.3mf",
        file_type="3mf",
        sha256=sha,
        size=100,
        copy_key=f"listing-file-{sha}",
        slots=[FileSlot(slot=0, colour="#FF0000", material="PLA", used=True, support=False)],
        plates=[1],
        preview_keys=["listing-preview-" + "3" * 64],
        **fields,
    )


@pytest.fixture
def store(pg_pool: PgPool) -> ListingStore:
    return ListingStore(pg_pool)


def hold(pool: PgPool, key: str, kind: str, holder: str, age: timedelta | None = None) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id, created_at)"
            " VALUES (%s, %s, %s, now() - %s)",
            (key, kind, holder, age or timedelta(0)),
        )


def published(store: ListingStore, pool: PgPool, sha: str = SHA) -> Listing:
    listing = store.create(file_body())
    hold(pool, f"listing-file-{sha}", KEPT_HOLDER, listing.id)
    return store.mark_published(
        listing.id, snapshot=snapshot(sha), media=[], expected_updated_at=listing.updated_at
    )


@pytest.mark.parametrize(
    ("fields", "name"),
    [
        ({"source": "template", "commit": HEAD}, "slug"),
        ({"source": "template", "slug": "demo"}, "commit"),
        (
            {
                "source": "template",
                "slug": "demo",
                "commit": HEAD,
                "library_file": {"id": 1, "library": "native"},
            },
            "library_file",
        ),
        ({"source": "library_file"}, "library_file"),
        (
            {"source": "library_file", "library_file": {"id": 1, "library": "native"}, "slug": "x"},
            "slug",
        ),
        (
            {
                "source": "library_file",
                "library_file": {"id": 1, "library": "native"},
                "commit": HEAD,
            },
            "commit",
        ),
        (
            {
                "source": "library_file",
                "library_file": {"id": 1, "library": "native"},
                "params": {"w": {"kind": "number", "min": 1, "max": 2}},
            },
            "params",
        ),
        (
            {
                "source": "library_file",
                "library_file": {"id": 1, "library": "native"},
                "fixed": {"w": 1},
            },
            "fixed",
        ),
        (
            {
                "source": "library_file",
                "library_file": {"id": 1, "library": "native"},
                "media_ids": ["abc"],
            },
            "media_ids",
        ),
    ],
)
def test_a_body_that_does_not_fit_its_source_is_refused(
    store: ListingStore, fields: dict[str, Any], name: str
) -> None:
    body = ListingInput.model_validate({"title": "T", **fields})
    with pytest.raises(ApiError) as refused:
        store.create(body)
    assert refused.value.type == STOREFRONT_PROBLEMS + "listing_source_invalid"
    assert (refused.value.status, refused.value.extensions["name"]) == (422, name)


def test_a_body_with_no_source_saves_as_a_template_listing(store: ListingStore) -> None:
    made = store.create(ListingInput.model_validate({"slug": "demo", "commit": HEAD, "title": "D"}))
    assert (made.source, made.library_file) == ("template", None)


def test_a_stored_template_snapshot_with_no_kind_reads_as_a_template(
    store: ListingStore, pg_pool: PgPool
) -> None:
    made = store.create(ListingInput.model_validate({"slug": "demo", "commit": HEAD, "title": "D"}))
    stored = SNAPSHOT.model_dump(mode="json", by_alias=True)
    del stored["kind"]
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_listings SET snapshot = %s, published = true WHERE id = %s",
            (psycopg.types.json.Jsonb(stored), made.id),
        )
    found = store.get(made.id)
    assert found is not None and isinstance(found.snapshot, ListingSnapshot)
    assert found.template_snapshot == SNAPSHOT and found.file_snapshot is None


def test_the_table_refuses_a_row_that_does_not_fit_its_source(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO storefront_listings (source, slug, library_file, title, position)"
            " VALUES ('library_file', 'demo', '{}', 'T', 0)"
        )
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO storefront_listings (slug, title, position) VALUES ('demo', 'T', 0)"
        )


def test_a_pinned_value_that_is_not_lowercase_hex_is_refused() -> None:
    with pytest.raises(ValidationError):
        file_body(library_file={"id": 7, "library": "native", "pinned": "A" * 64})


def test_a_file_listing_takes_plate_and_upload_picture_ids(store: ListingStore) -> None:
    made = store.create(file_body(media_ids=["plate:1", f"upload:{SHA}"]))
    assert made.media_ids == ["plate:1", f"upload:{SHA}"]
    assert made.library_file is not None
    assert made.library_file.model_dump() == {
        "id": 7,
        "library": "native",
        "repin": False,
        "pinned": None,
        "filename": None,
    }
    update = ListingMediaUpdate.model_validate(
        {"media_ids": ["plate:2", f"upload:{SHA}"], "updated_at": made.updated_at}
    )
    assert update.media_ids == ["plate:2", f"upload:{SHA}"]


def test_publish_writes_the_pin_and_holds_only_the_pinned_copy(
    store: ListingStore, pg_pool: PgPool
) -> None:
    listing = store.create(file_body())
    hold(pg_pool, f"listing-file-{OTHER}", KEPT_HOLDER, listing.id)
    hold(pg_pool, f"listing-file-{SHA}", KEPT_HOLDER, listing.id)
    hold(pg_pool, f"listing-media-{OTHER}", KEPT_HOLDER, listing.id)
    hold(pg_pool, snapshot().preview_keys[0], HOLDER, listing.id)
    done = store.mark_published(
        listing.id, snapshot=snapshot(), media=[], expected_updated_at=listing.updated_at
    )
    assert done.library_file is not None
    assert (done.library_file.pinned, done.library_file.repin) == (SHA, False)
    assert done.library_file.filename == "keychain.3mf"
    assert done.file_snapshot == snapshot()
    assert holders(pg_pool, f"listing-file-{OTHER}") == []
    assert holders(pg_pool, f"listing-file-{SHA}") == [(KEPT_HOLDER, listing.id)]
    # A picture's kept hold follows its own rule: a publish drops none.
    assert holders(pg_pool, f"listing-media-{OTHER}") == [(KEPT_HOLDER, listing.id)]
    assert holders(pg_pool, snapshot().preview_keys[0]) == [(HOLDER, listing.id)]


def test_a_save_that_leaves_the_file_keeps_the_pin_and_the_request(
    store: ListingStore, pg_pool: PgPool
) -> None:
    listing = published(store, pg_pool)
    asked = store.update(
        listing.id,
        file_body(library_file={"id": 7, "library": "native", "repin": True}),
        expected_updated_at=listing.updated_at,
    )
    assert asked.library_file is not None
    assert (asked.library_file.pinned, asked.library_file.repin) == (SHA, True)
    assert not asked.published and asked.snapshot is None
    # A body that says nothing of the request, and one that sends back another pin, keep both.
    kept = store.update(
        listing.id,
        file_body(library_file={"id": 7, "library": "native", "pinned": OTHER}),
        expected_updated_at=asked.updated_at,
    )
    assert kept.library_file is not None
    assert (kept.library_file.pinned, kept.library_file.repin) == (SHA, True)
    withdrawn = store.update(
        listing.id,
        file_body(library_file={"id": 7, "library": "native", "repin": False}),
        expected_updated_at=kept.updated_at,
    )
    assert withdrawn.library_file is not None
    assert (withdrawn.library_file.pinned, withdrawn.library_file.repin) == (SHA, False)
    # The edit and its unpublish keep the copy.
    assert holders(pg_pool, f"listing-file-{SHA}") == [(KEPT_HOLDER, listing.id)]


def test_a_save_that_picks_another_file_drops_the_pin_and_its_copy(
    store: ListingStore, pg_pool: PgPool
) -> None:
    listing = published(store, pg_pool)
    moved = store.update(
        listing.id,
        file_body(library_file={"id": 8, "library": "native", "repin": True}),
        expected_updated_at=listing.updated_at,
    )
    assert moved.library_file is not None
    assert (moved.library_file.pinned, moved.library_file.repin) == (None, False)
    assert holders(pg_pool, f"listing-file-{SHA}") == []


def test_a_save_as_a_template_drops_the_files_copy(store: ListingStore, pg_pool: PgPool) -> None:
    listing = published(store, pg_pool)
    made = store.update(
        listing.id,
        ListingInput.model_validate({"slug": "demo", "commit": HEAD, "title": "D"}),
        expected_updated_at=listing.updated_at,
    )
    assert (made.source, made.library_file) == ("template", None)
    assert holders(pg_pool, f"listing-file-{SHA}") == []


def test_a_save_drops_an_uploaded_picture_it_does_not_name_after_an_hour(
    store: ListingStore, pg_pool: PgPool
) -> None:
    listing = store.create(file_body(media_ids=[f"upload:{SHA}"]))
    old, fresh = f"listing-media-{OTHER}", "listing-media-" + "4" * 64
    hold(pg_pool, f"listing-media-{SHA}", KEPT_HOLDER, listing.id, timedelta(hours=2))
    hold(pg_pool, old, KEPT_HOLDER, listing.id, timedelta(hours=2))
    hold(pg_pool, fresh, KEPT_HOLDER, listing.id)
    saved = store.update(
        listing.id, file_body(media_ids=[f"upload:{SHA}"]), expected_updated_at=listing.updated_at
    )
    assert holders(pg_pool, f"listing-media-{SHA}") == [(KEPT_HOLDER, listing.id)]
    assert holders(pg_pool, old) == []
    # Within the hour an upload is spared: another tab's save may not name it yet.
    assert holders(pg_pool, fresh) == [(KEPT_HOLDER, listing.id)]
    store.set_media(listing.id, [], expected_updated_at=saved.updated_at)
    assert holders(pg_pool, f"listing-media-{SHA}") == []
    assert holders(pg_pool, fresh) == [(KEPT_HOLDER, listing.id)]


def test_a_delete_drops_every_kept_hold(store: ListingStore, pg_pool: PgPool) -> None:
    listing = published(store, pg_pool)
    hold(pg_pool, f"listing-media-{OTHER}", KEPT_HOLDER, listing.id)
    unpublished = store.mark_unpublished(listing.id)
    assert holders(pg_pool, f"listing-file-{SHA}") == [(KEPT_HOLDER, listing.id)]
    assert unpublished.library_file is not None and unpublished.library_file.pinned == SHA
    store.delete(listing.id)
    assert holders(pg_pool, f"listing-file-{SHA}") == []
    assert holders(pg_pool, f"listing-media-{OTHER}") == []


def test_a_failed_publish_drops_a_new_copy_the_row_does_not_pin(
    store: ListingStore, pg_pool: PgPool
) -> None:
    listing = published(store, pg_pool)
    preview = snapshot().preview_keys[0]
    hold(pg_pool, preview, HOLDER, listing.id)
    hold(pg_pool, f"listing-file-{OTHER}", KEPT_HOLDER, listing.id)
    hold(pg_pool, "listing-preview-" + "5" * 64, HOLDER, listing.id)
    store.release_unheld(listing.id)
    assert holders(pg_pool, f"listing-file-{OTHER}") == []
    assert holders(pg_pool, "listing-preview-" + "5" * 64) == []
    assert holders(pg_pool, f"listing-file-{SHA}") == [(KEPT_HOLDER, listing.id)]
    assert holders(pg_pool, preview) == [(HOLDER, listing.id)]


def test_the_list_shows_a_file_listing(store: ListingStore, pg_pool: PgPool) -> None:
    published(store, pg_pool)
    store.create(ListingInput.model_validate({"slug": "demo", "commit": HEAD, "title": "Demo"}))
    page = store.page(ListingQuery(q="keychain.3"))
    assert [(row.source, row.filename, row.slug) for row in page.items] == [
        ("library_file", "keychain.3mf", None)
    ]
    assert store.page(ListingQuery(slug=["demo"])).total == 1


def _slot(slot: int, *, used: bool = True, material: str | None = "PLA") -> FileSlot:
    support = material is not None and "support" in material.casefold()
    return FileSlot(slot=slot, colour="#FF0000", material=material, used=used, support=support)


def test_a_file_with_no_slot_to_offer_is_refused() -> None:
    """§5.1 row 5: every slot unused or support."""
    with pytest.raises(ApiError) as refused:
        check_offered([_slot(0, used=False), _slot(1, material="PLA Support")])
    assert refused.value.type == STOREFRONT_PROBLEMS + "listing_file_no_parts"
    check_offered([_slot(0)])


def test_a_support_slot_takes_a_support_filament_of_its_material() -> None:
    """§5.1 row 8: the shop locks each support slot; none on offer is refused."""
    materials = {"red": "PLA", "pva": "PVA", "plas": "PLA Support"}
    slots = [_slot(0), _slot(1, material="PLA Support"), _slot(2, used=False, material="PVA")]
    assert support_locks(slots, ["red", "pva", "plas"], materials) == {1: "plas"}
    assert support_locks(slots, ["red", "pva"], materials) == {1: "pva"}
    with pytest.raises(ApiError) as refused:
        support_locks(slots, ["red"], materials)
    assert refused.value.type == STOREFRONT_PROBLEMS + "listing_file_support_unset"
    assert refused.value.extensions["slots"] == [1]


async def test_a_published_file_listing_starts_no_default_render(
    store: ListingStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6922's warm run renders a template's default; a file listing has none to render."""
    listing = published(store, pg_pool)

    def no_run(_: object) -> None:
        raise AssertionError("a run was started")

    monkeypatch.setattr(prepare, "preparer", no_run)
    state = SimpleNamespace(settings=SimpleNamespace(storefront_warm_prices=True))
    await prepare.warm_default(state, listing)  # type: ignore[arg-type]
