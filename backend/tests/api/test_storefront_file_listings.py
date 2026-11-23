"""Publishing a storefront listing of a library file (#6710, spec 2026-11-21 §5, §9.3):
the checks of §5.1 in order, the held copy and its pin, the slots, the previews, the
pictures (a plate's own and an upload), and the plate picture route. On the native library
over the S3 store, as a shop that sells from its own library runs it."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from collections.abc import AsyncGenerator, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import ModelPrintChoices, S3Settings
from scadbuddy.main import create_app
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.storefront import file_listings
from scadbuddy.orders.storefront.listings import (
    HOLDER,
    KEPT_HOLDER,
    file_copy_key,
    published_listing,
)
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.glb import SLOT_EXTRA
from scadbuddy.render.objects3mf import UnreadableObjectsError
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails
from scadbuddy.store.content import ContentStore
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS
from tests.api.test_media import PNG

pytestmark = pytest.mark.requires_postgres

LISTINGS = "/api/v1/storefront/listings"
BOOK = "/api/v1/orders/price-book"


@pytest.fixture
def client(settings: Settings, s3: S3Settings, tmp_path: Path) -> Iterator[TestClient]:
    kek = tmp_path / "kek"
    kek.write_text(base64.b64encode(os.urandom(32)).decode())
    on_s3 = settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "library_backend": "native",
            "secret_key_file": kek,
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


def _native(client: TestClient) -> NativeLibrary:
    native: NativeLibrary = _state(client).components.get(PRINT_LIBRARY).native
    return native


def two_colours(path: Path, top: str = "#1F6FEB") -> Path:
    shifted = np.eye(4)
    shifted[:3, 3] = (0, 0, 3)
    parts = [
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4))),
        ColourPart(2, "Color 2", top, trimesh.creation.box(extents=(2, 2, 2), transform=shifted)),
    ]
    write_bambu_3mf(parts, path, thumbnails=render_plate_thumbnails(parts), model_name="keychain")
    return path


def upload(client: TestClient, path: Path) -> NativeFile:
    assert client.portal is not None
    with path.open("rb") as data:
        made: NativeFile = client.portal.call(
            partial(_native(client).upload, path.name, data, None)
        )
    return made


@pytest.fixture
def keychain(client: TestClient, tmp_path: Path) -> NativeFile:
    return upload(client, two_colours(tmp_path / "keychain.3mf"))


@pytest.fixture
def pla_red(client: TestClient) -> str:
    made = client.post(
        f"{BOOK}/filaments", json={"material": "PLA", "colour": "#FF0000", "cost_per_kg": "20"}
    )
    assert made.status_code in (200, 201), made.text
    filament_id: str = made.json()["id"]
    return filament_id


def body(file: NativeFile, colours: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "source": "library_file",
        "library_file": {"id": file.id, "library": "native"},
        "title": "Keychain",
        "colours": colours,
        **fields,
    }


def create(client: TestClient, listing: dict[str, Any]) -> dict[str, Any]:
    made = client.post(LISTINGS, json=listing)
    assert made.status_code == 201, made.text
    found: dict[str, Any] = made.json()
    return found


def save(client: TestClient, listing: dict[str, Any], **fields: Any) -> dict[str, Any]:
    keys = ("source", "slug", "commit", "library_file", "title", "colours", "media_ids")
    whole = {k: listing[k] for k in keys} | fields
    saved = client.put(
        f"{LISTINGS}/{listing['id']}", json={"listing": whole, "updated_at": listing["updated_at"]}
    )
    assert saved.status_code == 200, saved.text
    found: dict[str, Any] = saved.json()
    return found


def publish(client: TestClient, listing_id: str) -> dict[str, Any]:
    done = client.post(f"{LISTINGS}/{listing_id}/publish")
    assert done.status_code == 200, done.text
    found: dict[str, Any] = done.json()
    return found


def refused(client: TestClient, listing_id: str, slug: str, status: int = 422) -> dict[str, Any]:
    answer = client.post(f"{LISTINGS}/{listing_id}/publish")
    assert answer.status_code == status, answer.text
    problem: dict[str, Any] = answer.json()
    assert problem["type"] == STOREFRONT_PROBLEMS + slug, problem
    return problem


def holders(client: TestClient, key: str) -> list[tuple[str, str]]:
    with _state(client).projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_kind, holder_id FROM blob_refs WHERE key = %s ORDER BY holder_kind",
            (key,),
        ).fetchall()
    return [(row["holder_kind"], row["holder_id"]) for row in rows]


def _glb_slots(data: bytes) -> list[Any]:
    length = struct.unpack("<I", data[12:16])[0]
    meshes = json.loads(data[20 : 20 + length])["meshes"]
    return sorted(mesh.get("extras", {}).get(SLOT_EXTRA) for mesh in meshes)


def _bytes(client: TestClient, key: str) -> bytes:
    assert client.portal is not None
    found: bytes | None = client.portal.call(file_listings.read_held, _content(client), key)
    assert found is not None
    return found


def _trash(client: TestClient, file: NativeFile) -> None:
    assert client.portal is not None
    client.portal.call(_native(client).trash, [file.id])


def test_publish_keeps_the_file_and_reads_its_slots_and_previews(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])

    snapshot = listing["snapshot"]
    assert snapshot["kind"] == "library_file"
    assert (snapshot["file_id"], snapshot["filename"], snapshot["file_type"]) == (
        keychain.id,
        "keychain.3mf",
        "3mf",
    )
    assert snapshot["sha256"] == keychain.sha256
    assert [(s["slot"], s["colour"], s["used"], s["support"]) for s in snapshot["slots"]] == [
        (0, "#FF6AC1", True, False),
        (1, "#1F6FEB", True, False),
    ]
    assert snapshot["plates"] == [1] and snapshot["remembered"] is None
    copy = f"listing-file-{keychain.sha256}"
    assert snapshot["copy_key"] == copy
    assert hashlib.sha256(_bytes(client, copy)).hexdigest() == keychain.sha256
    assert holders(client, copy) == [(KEPT_HOLDER, listing["id"])]
    assert listing["library_file"] == {
        "id": keychain.id,
        "library": "native",
        "repin": False,
        "pinned": keychain.sha256,
        "filename": "keychain.3mf",
    }
    [preview] = snapshot["preview_keys"]
    assert holders(client, preview) == [(HOLDER, listing["id"])]
    assert _glb_slots(_bytes(client, preview)) == [0, 1]


def test_publish_takes_the_remembered_choices_of_the_file(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    choices = ModelPrintChoices.model_validate({"print_sequence": "by object"})
    _state(client).settings_store.set_library_choices(keychain.id, choices)
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    assert listing["snapshot"]["remembered"] == choices.model_dump(mode="json")


def test_an_stl_has_one_used_slot(client: TestClient, tmp_path: Path, pla_red: str) -> None:
    stl = tmp_path / "cube.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(stl)
    listing = publish(client, create(client, body(upload(client, stl), [pla_red]))["id"])
    snapshot = listing["snapshot"]
    assert [(s["slot"], s["used"]) for s in snapshot["slots"]] == [(0, True)]
    assert snapshot["file_type"] == "stl" and len(snapshot["preview_keys"]) == 1


def test_an_unknown_file_is_refused_first(client: TestClient, pla_red: str) -> None:
    made = create(
        client,
        {
            "source": "library_file",
            "library_file": {"id": 2**31 - 7, "library": "native"},
            "title": "Gone",
            "colours": [],
        },
    )
    assert refused(client, made["id"], "listing_file_unknown")["name"] == "library_file"


def test_a_sliced_file_is_refused(client: TestClient, tmp_path: Path, pla_red: str) -> None:
    sliced = two_colours(tmp_path / "plate.gcode.3mf")
    made = create(client, body(upload(client, sliced), [pla_red]))
    problem = refused(client, made["id"], "listing_file_unprintable")
    assert (problem["name"], problem["reason"]) == ("library_file", "sliced")


def test_a_file_whose_bytes_are_not_its_stated_hash_is_refused(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def other(state: AppState, file_id: int) -> AsyncGenerator[bytes]:
        yield b"other bytes"

    monkeypatch.setattr(file_listings, "download", other)
    made = create(client, body(keychain, [pla_red]))
    assert refused(client, made["id"], "listing_file_changed", 409)["name"] == "library_file"


def test_a_listing_with_no_customer_colour_is_unprintable(
    client: TestClient, keychain: NativeFile
) -> None:
    made = create(client, body(keychain, []))
    assert refused(client, made["id"], "listing_file_slots_unprintable")["name"] == "colours"


def test_a_failed_publish_drops_its_holds(client: TestClient, keychain: NativeFile) -> None:
    made = create(client, body(keychain, []))
    refused(client, made["id"], "listing_file_slots_unprintable")
    assert holders(client, f"listing-file-{keychain.sha256}") == []


def test_a_file_with_no_readable_mesh_publishes_with_no_preview(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_: Any) -> bytes:
        raise UnreadableObjectsError("no mesh")

    monkeypatch.setattr(file_listings, "plate_preview", unreadable)
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    assert listing["snapshot"]["preview_keys"] == []


def test_a_publish_again_reads_the_held_copy_not_the_library(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    edited = save(client, listing, title="Keychain, edited")
    assert holders(client, f"listing-file-{keychain.sha256}") == [(KEPT_HOLDER, listing["id"])]

    async def no_library(*_: Any) -> None:
        raise AssertionError("the library is read")

    monkeypatch.setattr(file_listings, "served_file", no_library)
    _trash(client, keychain)
    again = publish(client, edited["id"])
    assert again["snapshot"]["sha256"] == keychain.sha256
    assert again["library_file"]["pinned"] == keychain.sha256


def test_use_the_librarys_version_pins_the_new_file(
    client: TestClient, keychain: NativeFile, pla_red: str, tmp_path: Path
) -> None:
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    other = upload(client, two_colours(tmp_path / "keychain-2.3mf", top="#00FF00"))
    moved = save(client, listing, library_file={"id": other.id, "library": "native"})
    assert moved["library_file"]["pinned"] is None
    assert holders(client, f"listing-file-{keychain.sha256}") == []
    again = publish(client, moved["id"])
    assert again["library_file"]["pinned"] == other.sha256
    assert holders(client, f"listing-file-{other.sha256}") == [(KEPT_HOLDER, listing["id"])]


def test_a_repin_refused_for_a_gone_file_keeps_the_old_version(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    asked = save(
        client, listing, library_file={"id": keychain.id, "library": "native", "repin": True}
    )
    _trash(client, keychain)
    refused(client, asked["id"], "listing_file_unknown")
    stood = client.get(f"{LISTINGS}/{asked['id']}").json()
    assert (stood["library_file"]["pinned"], stood["library_file"]["repin"]) == (
        keychain.sha256,
        True,
    )
    assert holders(client, f"listing-file-{keychain.sha256}") == [(KEPT_HOLDER, listing["id"])]
    kept = save(
        client, stood, library_file={"id": keychain.id, "library": "native", "repin": False}
    )
    assert publish(client, kept["id"])["snapshot"]["sha256"] == keychain.sha256


def test_plate_and_uploaded_pictures_publish_in_order(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    made = create(client, body(keychain, [pla_red]))
    sent = client.post(
        f"{LISTINGS}/{made['id']}/pictures", files={"file": ("p.png", PNG, "image/png")}
    )
    assert sent.status_code == 200, sent.text
    sha = hashlib.sha256(PNG).hexdigest()
    assert sent.json() == {"id": f"upload:{sha}"}
    assert holders(client, f"listing-media-{sha}") == [(KEPT_HOLDER, made["id"])]

    saved = save(client, made, media_ids=["plate:1", f"upload:{sha}"])
    listing = publish(client, saved["id"])
    media = listing["media"]
    assert [(m["media_id"], m["kind"], m["media_type"]) for m in media] == [
        ("plate:1", "image", "image/png"),
        (f"upload:{sha}", "image", "image/png"),
    ]
    for item in media:
        assert (HOLDER, listing["id"]) in holders(client, item["blob_key"])
    assert _bytes(client, media[0]["blob_key"]).startswith(b"\x89PNG")


def test_an_unknown_picture_is_refused(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    made = create(client, body(keychain, [pla_red], media_ids=["plate:3"]))
    assert refused(client, made["id"], "listing_media_unknown")["name"] == "plate:3"
    upload_id = "upload:" + "a" * 64
    later = save(client, client.get(f"{LISTINGS}/{made['id']}").json(), media_ids=[upload_id])
    assert refused(client, later["id"], "listing_media_unknown")["name"] == upload_id


def test_an_upload_that_is_not_an_image_is_refused(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    made = create(client, body(keychain, [pla_red]))
    sent = client.post(
        f"{LISTINGS}/{made['id']}/pictures", files={"file": ("p.txt", b"hello", "text/plain")}
    )
    assert sent.status_code == 415


def test_changing_the_pictures_republishes_a_file_listing(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    changed = client.put(
        f"{LISTINGS}/{listing['id']}/media",
        json={"media_ids": ["plate:1"], "updated_at": listing["updated_at"]},
    )
    assert changed.status_code == 200, changed.text
    found = changed.json()
    assert found["published"] and [m["media_id"] for m in found["media"]] == ["plate:1"]


def test_the_plate_picture_is_read_from_the_held_copy(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    made = create(client, body(keychain, [pla_red]))
    assert client.get(f"{LISTINGS}/{made['id']}/plates/1.png").status_code == 404
    listing = publish(client, made["id"])
    _trash(client, keychain)
    found = client.get(f"{LISTINGS}/{listing['id']}/plates/1.png")
    assert found.status_code == 200 and found.headers["content-type"] == "image/png"
    assert found.content.startswith(b"\x89PNG")
    assert client.get(f"{LISTINGS}/{listing['id']}/plates/2.png").status_code == 404


def test_a_file_past_the_download_cap_is_refused(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.1 row 3: a print cannot read a file past `MAX_DOWNLOAD_BYTES`."""
    monkeypatch.setattr(file_listings, "MAX_DOWNLOAD_BYTES", 64)
    made = create(client, body(keychain, [pla_red]))
    assert refused(client, made["id"], "listing_file_too_large")["name"] == "library_file"


def test_an_upload_past_the_picture_cap_is_refused(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_listings, "MAX_IMAGE_BYTES", len(PNG) - 1)
    made = create(client, body(keychain, [pla_red]))
    sent = client.post(
        f"{LISTINGS}/{made['id']}/pictures", files={"file": ("p.png", PNG, "image/png")}
    )
    assert sent.status_code == 413
    assert holders(client, f"listing-media-{hashlib.sha256(PNG).hexdigest()}") == []


def test_an_upload_to_a_listing_deleted_meanwhile_keeps_no_hold(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete between the upload's read of the listing and its hold leaves no hold that
    nothing would drop."""
    made = create(client, body(keychain, [pla_red]))
    content = _content(client)
    put = content.put

    async def put_then_delete(*args: Any, **kwargs: Any) -> Any:
        stored = await put(*args, **kwargs)
        with _state(client).projection.pool.connection() as conn:
            conn.execute("DELETE FROM storefront_listings WHERE id = %s", (made["id"],))
        return stored

    monkeypatch.setattr(content, "put", put_then_delete)
    sent = client.post(
        f"{LISTINGS}/{made['id']}/pictures", files={"file": ("p.png", PNG, "image/png")}
    )
    assert sent.status_code == 404
    assert holders(client, f"listing-media-{hashlib.sha256(PNG).hexdigest()}") == []


def test_a_published_plate_picture_is_served_from_its_copy(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plate picture publish copied is served from that copy, not the whole file."""
    listing = publish(
        client, create(client, body(keychain, [pla_red], media_ids=["plate:1"]))["id"]
    )

    def whole_file(payload: bytes, plate: int) -> bytes | None:
        raise AssertionError("the held file was read")

    monkeypatch.setattr(file_listings, "plate_image", whole_file)
    found = client.get(f"{LISTINGS}/{listing['id']}/plates/1.png")
    assert found.status_code == 200 and found.content.startswith(b"\x89PNG")


def test_a_plate_picture_whose_copy_is_gone_is_read_from_the_held_file(
    client: TestClient, keychain: NativeFile, pla_red: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = publish(
        client, create(client, body(keychain, [pla_red], media_ids=["plate:1"]))["id"]
    )
    copied = listing["media"][0]["blob_key"]
    read_held = file_listings.read_held
    keys: list[str] = []

    async def copy_gone(content: ContentStore, key: str) -> bytes | None:
        keys.append(key)
        return None if key == copied else await read_held(content, key)

    monkeypatch.setattr(file_listings, "read_held", copy_gone)
    found = client.get(f"{LISTINGS}/{listing['id']}/plates/1.png")
    assert found.status_code == 200 and found.content.startswith(b"\x89PNG")
    assert keys == [copied, file_copy_key(keychain.sha256)]


def test_a_listing_with_no_pin_has_no_plate_picture(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    """§9.3: 404 for a listing with no pin, whatever pictures its row still names."""
    listing = publish(
        client, create(client, body(keychain, [pla_red], media_ids=["plate:1"]))["id"]
    )
    with _state(client).projection.pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_listings SET library_file = library_file - 'pinned' WHERE id = %s",
            (listing["id"],),
        )
    assert client.get(f"{LISTINGS}/{listing['id']}/plates/1.png").status_code == 404


def test_a_delete_drops_the_kept_copy(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    assert client.post(f"{LISTINGS}/{listing['id']}/unpublish").status_code == 200
    assert holders(client, f"listing-file-{keychain.sha256}") == [(KEPT_HOLDER, listing["id"])]
    assert client.delete(f"{LISTINGS}/{listing['id']}").status_code == 204
    assert holders(client, f"listing-file-{keychain.sha256}") == []


def test_only_a_template_listing_takes_a_render(
    client: TestClient, keychain: NativeFile, pla_red: str
) -> None:
    """A file listing is on the public site (#6710 §12 PR 6), but a render, a variant and a
    prepare run take a template listing only."""
    listing = publish(client, create(client, body(keychain, [pla_red]))["id"])
    listings = _state(client).components.get(ORDERS).listings
    assert [found.id for found in listings.published(limit=10)] == [listing["id"]]
    with pytest.raises(ApiError, match="no such listing"):
        published_listing(listings, listing["id"])
    assert published_listing(listings, listing["id"], source="library_file").id == listing["id"]
    assert published_listing(listings, listing["id"], source=None).id == listing["id"]
