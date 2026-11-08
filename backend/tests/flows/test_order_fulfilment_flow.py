"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2): its script type-checks
against its inputs, and it is registered once, as version 1, and never over a shop's
edit or delete."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


class _Host:
    """The script's host functions, faked: `prints` answers each `queue_print` in turn,
    `answers` each question (an `Exception` is a question nobody answered)."""

    def __init__(
        self, items: list[dict[str, Any]], prints: list[dict[str, Any]], answers: list[Any]
    ) -> None:
        self.items = items
        self.prints = prints
        self.answers = answers
        self.tools: list[str] = []
        self.printed: list[str] = []
        self.questions: list[str] = []

    async def trigger(self) -> dict[str, Any]:
        return {"kind": "order.changed", "order_id": "o1"}

    async def inputs(self) -> dict[str, Any]:
        return {"notify": False}

    async def tool(self, name: str, args: dict[str, Any]) -> str:
        self.tools.append(name)
        if name == "order_get":
            return json.dumps({"order": {"id": "o1", "number": "ORD-1"}, "items": self.items})
        if name == "get_print_choices":
            return json.dumps({"default_nozzles": [], "bed_type": "pei"})
        raise AssertionError(f"unexpected tool {name}")

    async def queue_print(self, source: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        self.printed.append(source["order_item_id"])
        return self.prints.pop(0)

    async def wait_for_human(self, question: str, timeout: int) -> dict[str, Any]:
        self.questions.append(question)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"answer": answer}

    async def agent(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"summary": "ok"}

    def run(self) -> None:
        names = ("trigger", "inputs", "tool", "queue_print", "wait_for_human", "agent")
        exec(SCRIPT, {name: getattr(self, name) for name in names})


def _item(item_id: str, status: str = "approved") -> dict[str, Any]:
    return {"id": item_id, "title": item_id, "status": status, "kind": "model", "output_id": "o"}


_OK: dict[str, Any] = {"status": "succeeded", "problem": None, "error": None}


def _refused(problem: str) -> dict[str, Any]:
    return {"status": "refused", "problem": problem, "error": problem}


def test_a_stale_quote_asks_a_person_and_never_re_quotes() -> None:
    """Review of #5682: on an accepted order the API refuses a new quote for an item whose
    accepted quote stands (`quote_check`, `order_not_open`), so the flow asks a person."""
    host = _Host([_item("a"), _item("b")], [_refused("requote_needed"), _OK, _OK], ["print"])
    host.run()
    assert host.printed == ["a", "a", "b"]
    assert "order_item_quote" not in host.tools
    assert "re-quote" in host.questions[0]


def test_a_hold_asks_until_the_order_is_released() -> None:
    held = _refused("order_on_hold")
    host = _Host([_item("a")], [held, held, _OK], ["print", "print"])
    host.run()
    assert host.printed == ["a", "a", "a"]
    assert len(host.questions) == 2


def test_an_unanswered_or_skipped_item_never_ends_the_run() -> None:
    host = _Host(
        [_item("a"), _item("b"), _item("c", "failed"), _item("d", "fulfilled")],
        [_refused("requote_needed"), _refused("x"), _OK],
        [TimeoutError("nobody answered"), "skip"],
    )
    host.run()
    # A failed item prints again; a fulfilled one is not printed.
    assert host.printed == ["a", "b", "c"]
