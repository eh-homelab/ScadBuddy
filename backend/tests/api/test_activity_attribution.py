"""Who started a render or a print: the route records it on the activity row."""

import asyncio

from fastapi.testclient import TestClient

from scadbuddy.activity.index import ActivityIndex


def test_coalesced_render_keeps_first_principal(
    client: TestClient, activity_index: ActivityIndex, model: str
) -> None:
    body = {"params": {"width": 12}}
    first = client.post(
        f"/api/v1/models/{model}/render",
        json=body,
        headers={"X-ScadBuddy-Agent-Author": "token:t1"},
    )
    second = client.post(f"/api/v1/models/{model}/render", json=body)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    got = asyncio.run(activity_index.get(f"render:{first.json()['job_id']}"))
    assert got is not None and got.principal == "token:t1"


def test_a_browser_render_is_attributed_to_the_browser(
    client: TestClient, activity_index: ActivityIndex, model: str
) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 13}})
    assert response.status_code == 202
    got = asyncio.run(activity_index.get(f"render:{response.json()['job_id']}"))
    assert got is not None and got.principal == "browser"


def test_the_activity_component_ingests_an_agent_operation_event(client: TestClient) -> None:
    """The bus listener runs under the app: an AgentOperationEvent reaches the index."""
    import time
    from unittest.mock import AsyncMock

    from scadbuddy.activity.component import ACTIVITY
    from scadbuddy.api.deps import STATE_ATTR
    from scadbuddy.core.events import AgentOperationEvent

    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    ingest = AsyncMock()
    state.components.get(ACTIVITY).index.ingest_agent_operation = ingest
    state.events.publish(AgentOperationEvent(operation_id="x1"))
    deadline = time.monotonic() + 5
    while not ingest.await_args_list and time.monotonic() < deadline:
        time.sleep(0.05)
    ingest.assert_awaited_with("x1")


def test_a_failing_attribution_does_not_fail_the_render(client: TestClient, model: str) -> None:
    from unittest.mock import AsyncMock

    from scadbuddy.activity.component import ACTIVITY
    from scadbuddy.api.deps import STATE_ATTR

    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    state.components.get(ACTIVITY).index.attribute = AsyncMock(side_effect=RuntimeError("pool"))
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 14}})
    assert response.status_code == 202


def test_a_failing_ingest_is_counted_not_lost(client: TestClient) -> None:
    import time
    from unittest.mock import AsyncMock

    from scadbuddy.activity.component import ACTIVITY
    from scadbuddy.api.deps import STATE_ATTR
    from scadbuddy.core.events import AgentOperationEvent

    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    index = state.components.get(ACTIVITY).index
    index.ingest_agent_operation = AsyncMock(side_effect=RuntimeError("boom"))
    before = index.metrics.activity_write_failures._value.get()
    state.events.publish(AgentOperationEvent(operation_id="x2"))
    deadline = time.monotonic() + 5
    while index.metrics.activity_write_failures._value.get() == before:
        assert time.monotonic() < deadline
        time.sleep(0.05)
