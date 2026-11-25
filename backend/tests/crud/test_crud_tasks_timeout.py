"""The tasks route's answer to a body whose schema default's pattern ran out of the shared
budget (#7437): a 503 the client sends again. Its own module, with no Postgres mark: the
body is refused as it is read, so no store is reached and the test runs everywhere."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scadbuddy.core import input_schema
from scadbuddy.crud.wire import PATTERN_TIMEOUT
from tests.crud.test_crud_tasks import BASE, T1, TOKEN, _app, _wire


async def test_a_default_whose_pattern_times_out_is_503_to_send_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7437: the client tries a 5xx again; a real refusal is still 422. The body is refused
    as it is read, so no store is reached."""
    monkeypatch.setattr(input_schema, "PATTERNS_BUDGET_S", 0)
    slow: dict[str, Any] = {
        "type": "string",
        "pattern": "^(a|aa)+$",
        "maxLength": 64,
        "default": "a" * 60 + "!",
    }
    wrong = {"type": "string", "maxLength": 5, "default": 3}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(None)),
        base_url="http://api",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as http:
        timed_out = await http.post(
            BASE, json={**_wire(T1), "schema": {"type": "object", "properties": {"x": slow}}}
        )
        refused = await http.post(
            BASE,
            json={**_wire(T1), "schema": {"type": "object", "properties": {"x": slow, "y": wrong}}},
        )
    assert timed_out.status_code == 503, timed_out.text
    assert timed_out.headers["X-ScadBuddy-Crud"] == "1"
    assert timed_out.json()["reason"] == PATTERN_TIMEOUT
    assert slow["default"] not in timed_out.text
    assert refused.status_code == 422
