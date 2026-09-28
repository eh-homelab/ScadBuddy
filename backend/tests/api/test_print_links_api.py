"""Issue #306 through the app: a progress poll links the print's archive, the media
proxy then serves it, and deleting the output forgets the link."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.print_links import PrintLink
from tests.api.test_send import API, configure, make_output
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres


def state(client: TestClient) -> AppState:
    app_state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return app_state


@respx.mock
def test_a_progress_poll_links_the_archive_and_the_proxy_serves_it(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    state(client).outputs.record_send(output_id, queue_item_id=34, print_route="slice_queue")
    respx.get(f"{API}/queue/34").mock(
        return_value=httpx.Response(
            200, json={**recording("queue-item.json"), "id": 34, "archive_id": 18}
        )
    )
    respx.get(f"{API}/archives/18/thumbnail").mock(
        return_value=httpx.Response(200, content=b"png", headers={"Content-Type": "image/png"})
    )

    assert client.get("/api/v1/prints/18/thumbnail").status_code == 404
    assert client.get(f"/api/v1/print/outputs/{output_id}/progress").status_code == 200
    response = client.get("/api/v1/prints/18/thumbnail")

    assert response.status_code == 200
    assert response.content == b"png"


def test_deleting_an_output_forgets_its_links(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    links = state(client).print_links
    asyncio.run(links.record(output_id, PrintLink(archive_id=18, matched_by="queue_item")))

    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204

    assert asyncio.run(links.output_for(18)) is None
