# ruff: noqa: F811  (pytest fixtures imported by name, then taken as arguments)
"""A storefront listing of a library file on the public site (#6710, spec 2026-11-21 §6,
§8, §12 PR 6): the CRUD side's list and listing show it with its parts and its preview
paths, and ``GET …/listings/{id}/preview/{plate}.glb`` streams the GLB publish held.

The public listing's template cases are ``test_storefront_public.py``; these need the
native library over S3 (``test_storefront_file_listings.py``'s ``client``), whose fixture
names would clash there."""

from __future__ import annotations

import pytest

from scadbuddy.print_library.store import NativeFile
from tests.api.test_storefront_file_listings import (  # noqa: F401  (fixtures)
    _bytes,
    body,
    client,
    create,
    keychain,
    pla_red,
    publish,
)
from tests.api.test_storefront_renders import CrudCaller, crud  # noqa: F401  (fixtures)

pytestmark = pytest.mark.requires_postgres


def test_a_file_listing_is_on_the_public_site(
    crud: CrudCaller, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red]))["id"])

    cards = crud.get("/listings")
    assert cards.status_code == 200, cards.text
    assert [(c["id"], c["available"]) for c in cards.json()["listings"]] == [(listing["id"], True)]

    shown = crud.get(f"/listings/{listing['id']}")
    assert shown.status_code == 200, shown.text
    public = shown.json()
    assert public["source"] == "library_file"
    assert public["params"] == {}
    assert public["slots"] == [
        {"slot": 0, "label": "Part 1", "default": pla_red},
        {"slot": 1, "label": "Part 2", "default": pla_red},
    ]
    assert public["previews"] == [f"/api/store/v1/listings/{listing['id']}/preview/1.glb"]
    # The page never shows the file's name, its colours or a material of its own.
    text = shown.text
    assert "keychain.3mf" not in text and "#FF6AC1" not in text and "#1F6FEB" not in text


def test_the_preview_route_streams_the_held_glb(
    crud: CrudCaller, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red]))["id"])
    [key] = listing["snapshot"]["preview_keys"]

    r = crud.get(f"/listings/{listing['id']}/preview/1.glb")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.content == _bytes(crud.api, key)


def test_a_plate_with_no_preview_is_404(
    crud: CrudCaller, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red]))["id"])
    r = crud.get(f"/listings/{listing['id']}/preview/2.glb")
    assert r.status_code == 404, r.text
    assert r.json()["type"].endswith("/preview_not_found")


def test_a_plate_number_below_one_is_422(
    crud: CrudCaller, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red]))["id"])
    r = crud.get(f"/listings/{listing['id']}/preview/0.glb")
    assert r.status_code == 422, r.text


def test_an_unpublished_file_listing_has_no_preview(
    crud: CrudCaller, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(crud.api, create(crud.api, body(keychain, [pla_red]))["id"])
    assert (
        crud.api.post(f"/api/v1/storefront/listings/{listing['id']}/unpublish").status_code == 200
    )
    r = crud.get(f"/listings/{listing['id']}/preview/1.glb")
    assert r.status_code == 404
    assert r.json()["type"].endswith("/listing_not_found")
