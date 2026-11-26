# ruff: noqa: F811  (a pytest fixture imported by name, then taken as an argument)
"""#7316: with ``SCADBUDDY_SPOOL_DIR`` set, every read of a library file spools under
it, whichever route reads the file: the root is resolved once, on the download cache
the store builds, so no caller passes it (or forgets to). The app here starts with a
spool dir and a node-local, private download cache, as prod's ``emptyDir`` is."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState, get_render
from scadbuddy.core.settings import Settings
from tests.api.test_arrange_api import Arranger, two_colour_3mf
from tests.api.test_order_library_items import (  # noqa: F401  (fixtures)
    FILE_ID,
    an_order,
    bambuddy,
)
from tests.api.test_order_print import answered
from tests.api.test_orders_lifecycle import add_item, quote_and_accept
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_library import library_3mf, library_file
from tests.api.test_print_run_choices import body, follow_run
from tests.api.test_send import API, configure
from tests.conftest import PgPool
from tests.support.arrange import saved_output

pytestmark = pytest.mark.requires_postgres

LIBRARY_SPOOL = "library-spool"


@pytest.fixture
def node(tmp_path: Path) -> Path:
    """The node-local volume: the spool and the download cache."""
    return tmp_path / "node"


@pytest.fixture
def settings(settings: Settings, node: Path) -> Iterator[Settings]:
    yield settings.model_copy(
        update={
            "spool_dir": node / "spool",
            "download_cache_dir": node / "downloads",
            "download_cache_private": True,
        }
    )


def _spooled_only_under(app: FastAPI, node: Path) -> None:
    state: AppState = getattr(app.state, STATE_ATTR)
    assert (node / "spool" / LIBRARY_SPOOL).is_dir()
    assert not (state.paths.cache / LIBRARY_SPOOL).exists()


@respx.mock
def test_arrange_spools_a_library_file_under_the_spool_dir(
    client: TestClient, app: FastAPI, tmp_path: Path, pg_pool: PgPool, node: Path
) -> None:
    configure(client)
    asyncio.run(saved_output(tmp_path, pg_pool))
    library_file(88, content=two_colour_3mf(tmp_path))
    app.dependency_overrides[get_render] = lambda: Arranger()

    arranged = client.post(
        "/api/v1/outputs/arrange", json={"objects": [{"library_file_id": 88}], "slug": "demo"}
    )

    assert arranged.status_code == 202, arranged.text
    _spooled_only_under(app, node)


@pytest.mark.usefixtures("recorded_sb_spools")
def test_an_order_items_library_print_spools_under_the_spool_dir(
    bambuddy: TestClient, app: FastAPI, node: Path
) -> None:
    respx.get(f"{API}/library/files", params={"folder_id": 801}).mock(
        return_value=httpx.Response(200, json=[])
    )
    # A hash Bambuddy states: the run then reads the file's slots from the file itself.
    content = library_3mf()
    library_file(FILE_ID, content=content, file_hash=hashlib.sha256(content).hexdigest())
    slice_routes()
    queue_route()
    order = an_order(bambuddy)
    item = add_item(bambuddy, order["id"], library_file_id=FILE_ID, quantity=1)
    quote_and_accept(bambuddy, order["id"], item["id"])
    assert bambuddy.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    path = f"/api/v1/orders/{order['id']}/items/{item['id']}"

    attempt = answered(
        bambuddy, bambuddy.post(f"{path}/print", json=body(), headers={"Idempotency-Key": "sp-1"})
    )

    assert follow_run(bambuddy, attempt["print_run_id"])["status"] == "succeeded"
    _spooled_only_under(app, node)


@respx.mock
def test_a_private_caches_hit_streams_the_entry_with_no_spooled_copy(
    client: TestClient, app: FastAPI, node: Path
) -> None:
    """#7316 review: the second ask is a hit, read in place from the private entry, so
    no copy is spooled for it, under the spool dir or the data volume."""
    configure(client)
    content = library_3mf()
    download = library_file(89, content=content, file_hash=hashlib.sha256(content).hexdigest())
    state: AppState = getattr(app.state, STATE_ATTR)

    first = client.get("/api/v1/print/library/89/file")
    second = client.get("/api/v1/print/library/89/file")

    assert first.content == second.content == content
    assert download.call_count == 1
    assert second.headers["content-length"] == str(len(content))
    assert not (node / "spool" / LIBRARY_SPOOL).exists()
    assert not (state.paths.cache / LIBRARY_SPOOL).exists()
