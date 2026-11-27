"""#7169: `GET /codes/{code}` for a spool type."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.test_filament import _type

pytestmark = pytest.mark.requires_postgres


def test_a_type_resolves_by_a_lower_case_t_code(client: TestClient) -> None:
    made = _type(client)
    got = client.get(f"/api/v1/codes/t{made['number']}")
    assert got.status_code == 200, got.text
    assert got.json() == {
        "kind": "spool_type",
        "id": made["id"],
        "code": f"T-{made['number']}",
        "name": "PLA-RED",
        "archived": False,
    }
