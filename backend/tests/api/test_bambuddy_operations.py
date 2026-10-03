"""The Bambuddy writes as operations (#1053, spec 2026-10-01 §4.2, §4.3): a key makes a
retry answer the first outcome without a second effect; no key keeps today's
behaviour; a refusal writes no record."""

from __future__ import annotations

import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_actions import mock_enqueue
from tests.api.test_print_history import link, mock_archive
from tests.api.test_send import configure, make_output

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


@respx.mock
def test_reprint_with_a_key_twice_queues_once(client: TestClient, model: str) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    queue = mock_enqueue(51)

    headers = {"Idempotency-Key": "press-1"}
    first = client.post("/api/v1/prints/35/reprint", headers=headers)
    again = client.post("/api/v1/prints/35/reprint", headers=headers)

    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert queue.call_count == 1


@respx.mock
def test_reprint_without_a_key_queues_twice(client: TestClient, model: str) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    queue = mock_enqueue(51)

    assert client.post("/api/v1/prints/35/reprint").status_code == 201
    assert client.post("/api/v1/prints/35/reprint").status_code == 201
    assert queue.call_count == 2


@respx.mock
def test_a_reprint_refusal_writes_no_operation(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=None)
    queue = mock_enqueue()

    response = client.post("/api/v1/prints/35/reprint", headers={"Idempotency-Key": "p"})

    assert response.status_code == 409
    assert not queue.called
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == (0,)


@respx.mock
def test_a_reprint_is_recorded_as_an_operation(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    mock_enqueue(51)

    client.post("/api/v1/prints/35/reprint", headers={"Idempotency-Key": "p"})

    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT kind, subject, status, result FROM operations").fetchone()
    assert row is not None and row[:3] == ("reprint", "archive:35", "succeeded")
    assert row[3]["queue_item_id"] == 51
