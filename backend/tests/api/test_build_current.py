"""The api `client` fixture hands a test its client only once the in-process worker's
build serves the test's task queue: before that, a render waits unrouted and outlives
the submit's deadline (a 503 the test never expected)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.render.submit import SUBMIT_DEADLINE
from scadbuddy.workflows.client import make_current

#: Longer than the submit's deadline, as a loaded dev server's SetCurrentVersion was.
SLOW_SET_CURRENT = SUBMIT_DEADLINE + 3


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # A build no test made current before, so nothing routes this queue until it is.
    return settings.model_copy(update={"revision": f"slow-{uuid.uuid4().hex[:8]}"})


@pytest.fixture(autouse=True)
def _slow_set_current(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(SLOW_SET_CURRENT)
        await make_current(*args, **kwargs)

    monkeypatch.setattr("scadbuddy.worker.make_current", slow)


def test_the_first_render_is_accepted_when_the_build_was_slow_to_become_current(
    client: TestClient, model: str
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 202, response.json()
