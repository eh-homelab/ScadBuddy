"""`TriggerFires._decide` (#2484) with its store and flows faked: what an attempt does
once its fire is admitted, without Postgres or Temporal."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.flow_triggers import fire as fire_module
from scadbuddy.flow_triggers.fire import TriggerFires
from scadbuddy.flow_triggers.models import ScheduleTiming, Trigger


def trigger() -> Trigger:
    return Trigger.model_validate(
        {
            "id": str(uuid.uuid4()),
            "name": "nightly",
            "definition_id": str(uuid.uuid4()),
            "kind": "schedule",
            "schedule": ScheduleTiming(interval_s=3600),
            "overlap": "skip",
            "paused": False,
            "created_by": {"kind": "browser"},
            "created_at": datetime.now(UTC),
        }
    )


class FakeStore:
    def __init__(self, found: Trigger, refused: str | None) -> None:
        self.found = found
        self.refused = refused
        self.seeded = False

    async def get(self, trigger_id: str) -> Trigger:
        return self.found

    async def admit(self, *args: Any) -> None:
        return None

    async def begin_start(self, trigger_id: str, fire_key: str) -> str | None:
        return self.refused

    async def seed_run_grant(self, *args: Any) -> str:
        self.seeded = True
        return "grant"


async def test_an_attempt_refused_its_start_settles_with_the_reason_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3863 review: a fire past `ADMITTED_DEAD`, or whose trigger was deleted meanwhile,
    is settled `skipped` with the store's reason, and no grant is seeded nor run started."""
    started: list[str] = []

    async def no_problems(*args: Any) -> list[Any]:
        return []

    async def not_running(*args: Any) -> None:
        return None

    async def start(*args: Any, **kwargs: Any) -> None:
        started.append(kwargs["run_id"])

    monkeypatch.setattr(fire_module, "check_script", no_problems)
    monkeypatch.setattr(fire_module, "resolve_inputs", lambda schema, inputs: {})
    monkeypatch.setattr(fire_module, "execution_status", not_running)
    monkeypatch.setattr(fire_module, "start_execution", start)
    monkeypatch.setattr(fire_module, "approval_timeout", lambda *args: None)

    async def get_definition(definition_id: str) -> Any:
        return SimpleNamespace(script="", inputs_schema={}, deleted_at=None, archived_at=None)

    async def tool_tiers() -> dict[str, str]:
        return {}

    async def delete_grant(run_id: str) -> None:
        return None

    flows = SimpleNamespace(
        definitions=SimpleNamespace(get=get_definition),
        client=object(),
        agent_tools=SimpleNamespace(tool_tiers=tool_tiers),
        store=SimpleNamespace(delete_grant=delete_grant),
    )
    for reason in ("expired", "deleted"):
        found = trigger()
        store = FakeStore(found, reason)
        fires = TriggerFires(
            store,  # type: ignore[arg-type]
            lambda: flows,  # type: ignore[arg-type,return-value]
            client=object(),  # type: ignore[arg-type]
            task_queue="library",
        )
        assert await fires._decide(found.id, "fire-1", "run-1", None) == ("skipped", reason)
        assert not store.seeded
    assert started == []
