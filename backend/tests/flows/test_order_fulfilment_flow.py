"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2): its script type-checks
against its inputs, and it is registered once, as version 1, and never over a shop's
edit or delete."""

from __future__ import annotations

from pathlib import Path

import pytest

from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.states import FlowStates
from scadbuddy.flows.store import FlowStore
from scadbuddy.flows.typecheck import check_script
from scadbuddy.orders.builtin_flow import (
    INPUTS_SCHEMA,
    NAME,
    SCRIPT,
    ensure_builtin_flow,
)
from scadbuddy.render.projection import JobProjection


async def test_the_script_type_checks_against_its_inputs() -> None:
    assert await check_script(SCRIPT, INPUTS_SCHEMA) == []
    assert await check_script(SCRIPT, INPUTS_SCHEMA, stored=True) == []


@pytest.fixture
def definitions(tmp_path: Path, jobs: JobProjection) -> FlowDefinitions:
    store = FlowStore(jobs.pool)
    return FlowDefinitions(FlowHistory(tmp_path / "flows"), store, FlowStates(jobs.pool))


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_it_is_registered_once_and_never_over_an_edit_or_a_delete(
    definitions: FlowDefinitions,
) -> None:
    await definitions.start()
    assert await ensure_builtin_flow(definitions) is True
    [first] = definitions.history.versions(NAME)
    assert definitions.history.script_at(NAME, first.commit) == SCRIPT
    assert definitions.history.inputs_at(NAME, first.commit) == INPUTS_SCHEMA
    # A second start, or a second replica, registers nothing.
    assert await ensure_builtin_flow(definitions) is False
    # The shop's version 2 stands.
    await definitions.register(NAME, "return 'mine'", {"kind": "browser"}, approval_timeout_s=None)
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 2
    # Deleted, it stays deleted.
    await definitions.delete(NAME, {"kind": "browser"})
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 2
