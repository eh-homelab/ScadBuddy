"""A storefront item's print through the API (#6710 §10.2): with its file gone from the
native library, the print's run puts the held copy into "Storefront", moves the item and
starts the library run on the new id; the next print starts on that id with no restore."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import library_print
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.models import Actor, LibraryFileRef
from scadbuddy.orders.served_library import STOREFRONT_FOLDER
from scadbuddy.print_library.store import NativeFile
from scadbuddy.store.content_models import BlobScope
from tests.api.conftest import follow_operation
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import quote_and_accept
from tests.api.test_print_run_choices import body
from tests.api.test_storefront_file_listings import (  # noqa: F401
    _content,
    _native,
    _state,
    _trash,
    client,
    keychain,
)
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def _storefront_item(api: TestClient, file: NativeFile) -> tuple[str, str, str]:
    """An accepted order with one accepted storefront item of ``file``, its bytes held."""
    data = _bytes_of(api, file)
    sha256 = hashlib.sha256(data).hexdigest()
    key = f"listing-file-{sha256}"
    assert api.portal is not None
    api.portal.call(
        lambda: _content(api).put("media", data, name=file.name, scope=BlobScope(), key=key)
    )
    order = create_order(api, create_customer(api)["id"])
    ref = LibraryFileRef(
        id=file.id, filename=file.name, library="native", sha256=sha256, copy_key=key
    )
    lifecycle = _state(api).components.get(ORDERS).lifecycle
    item = lifecycle.add_library_item(order["id"], Actor(kind="browser"), ref, quantity=1)
    quote_and_accept(api, order["id"], item.id)
    assert api.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    return order["id"], item.id, sha256


def _bytes_of(api: TestClient, file: NativeFile) -> bytes:
    async def read() -> bytes:
        out = bytearray()
        async for chunk in _native(api).download_library_file(file.id):
            out += chunk
        return bytes(out)

    assert api.portal is not None
    data: bytes = api.portal.call(read)
    return data


def test_a_gone_file_prints_from_its_held_copy_on_a_new_id(
    client: TestClient,  # noqa: F811
    keychain: NativeFile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[int] = []

    async def start(file_id: int, *args: Any, **kwargs: Any) -> Any:
        started.append(file_id)
        raise ApiError(422, "refused for the test")

    monkeypatch.setattr(library_print, "start_library_run", start)
    order_id, item_id, sha256 = _storefront_item(client, keychain)
    _trash(client, keychain)
    path = f"/api/v1/orders/{order_id}/items/{item_id}"

    def print_once() -> None:
        """A print whose start the test refuses (after the restore, which runs first)."""
        answer = client.post(f"{path}/print", json=body(), headers=press())
        if answer.status_code == 202:
            op = follow_operation(client, answer.json(), timeout=60)
            assert op["status"] == "failed", op
        else:
            assert answer.status_code == 422, answer.text

    print_once()
    item = client.get(f"/api/v1/orders/{order_id}").json()["items"][0]
    new_id = item["library_file"]["id"]
    assert new_id != keychain.id
    assert started == [new_id]
    assert client.portal is not None
    made = client.portal.call(_native(client).library_file, new_id)
    assert made.file_hash == sha256
    folder = client.portal.call(_native(client).folder, made.folder_id or 0)
    assert folder.name == STOREFRONT_FOLDER
    # The next print stands on the new id: nothing more is uploaded.
    print_once()
    assert started == [new_id, new_id]
    listed = client.portal.call(lambda: _native(client).library_listing(folder_id=made.folder_id))
    assert [row.id for row in listed] == [new_id]
