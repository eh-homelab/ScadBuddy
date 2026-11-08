"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2): its script type-checks
against its inputs, and it is registered once, as version 1, and never over a shop's
edit or delete."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.reconcile import created_by
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.states import FlowStates
from scadbuddy.flows.store import FlowStore
from scadbuddy.flows.typecheck import check_script
from scadbuddy.orders import builtin_flow, component
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
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await definitions.start()
    assert await ensure_builtin_flow(definitions) is True
    [first] = definitions.history.versions(NAME)
    assert definitions.history.script_at(NAME, first.commit) == SCRIPT
    assert definitions.history.inputs_at(NAME, first.commit) == INPUTS_SCHEMA
    # Its authorship is in the commit too (review of #5682): git, reconcile and the row agree.
    assert first.builtin == NAME
    [listed] = await definitions.versions(NAME, limit=1)
    assert listed.created_by == {"kind": "builtin", "name": NAME}
    assert created_by(first.agent, first.builtin) == {"kind": "builtin", "name": NAME}

    # A second start, or a second replica, registers nothing, and says so in the log;
    # it reads the versions first, so it never type-checks the script again (review of
    # #5682).
    async def no_check(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("type-checked although the name has versions")

    monkeypatch.setattr(builtin_flow, "check_script", no_check)
    with caplog.at_level("INFO", logger="scadbuddy.orders.builtin_flow"):
        assert await ensure_builtin_flow(definitions) is False
    assert "not registered: the name has versions" in caplog.text
    # The shop's version 2 stands.
    await definitions.register(NAME, "return 'mine'", {"kind": "browser"}, approval_timeout_s=None)
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 2
    # Deleted, it stays deleted.
    await definitions.delete(NAME, {"kind": "browser"})
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 2


@pytest.mark.parametrize(
    ("switch", "flows", "registered"),
    [(True, "defs", True), (False, "defs", False), (True, None, False)],
)
async def test_the_component_registers_it_only_when_switched_on_with_flows(
    monkeypatch: pytest.MonkeyPatch, switch: bool, flows: str | None, registered: bool
) -> None:
    """Review of #5682: `_run` skips the built-in with `orders_builtin_flow` off, or
    without flows."""
    calls: list[object] = []

    async def ensure(definitions: object) -> bool:
        calls.append(definitions)
        return True

    async def remove() -> None:
        return None

    monkeypatch.setattr(component, "ensure_builtin_flow", ensure)
    monkeypatch.setattr(component, "listen_for_prints", lambda *args: remove)
    orders = SimpleNamespace(
        flows=flows,
        core=SimpleNamespace(settings=SimpleNamespace(orders_builtin_flow=switch), events=None),
        lifecycle=None,
        integrations=None,
    )
    async with component._run(cast(component.OrdersComponent, orders)):
        await asyncio.sleep(0)
    assert calls == (["defs"] if registered else [])


async def test_a_slow_registration_never_holds_up_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #5682: the registration runs beside the start, so a slow flows repository
    delays nothing, and it is cancelled when the component stops."""
    listening: list[bool] = []
    cancelled = asyncio.Event()

    async def ensure(definitions: object) -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return True

    async def remove() -> None:
        return None

    def listen(*args: object) -> Any:
        listening.append(True)
        return remove

    monkeypatch.setattr(component, "ensure_builtin_flow", ensure)
    monkeypatch.setattr(component, "listen_for_prints", listen)
    orders = SimpleNamespace(
        flows="defs",
        core=SimpleNamespace(settings=SimpleNamespace(orders_builtin_flow=True), events=None),
        lifecycle=None,
        integrations=None,
    )
    async with asyncio.timeout(5):
        async with component._run(cast(component.OrdersComponent, orders)):
            await asyncio.sleep(0)
            assert listening == [True]
    assert cancelled.is_set()


class _Host:
    """The script's host functions, faked: `prints` answers each `queue_print` in turn,
    `answers` each question (an `Exception` is a question nobody answered), and `later`
    each `order_get` after the first, as (items, order status); the last one stands."""

    def __init__(
        self,
        items: list[dict[str, Any]],
        prints: list[dict[str, Any]],
        answers: list[Any],
        *,
        later: list[tuple[list[dict[str, Any]], str]] | None = None,
        notify: bool = False,
        settle_hours: int = 48,
        choices: dict[str, Any] | None = None,
    ) -> None:
        self.items = items
        self.prints = prints
        self.answers = answers
        self.later = later or [(items, "in_production")]
        self.settings = {"notify": notify, "settle_hours": settle_hours}
        self.choices = choices or {"default_nozzles": [], "bed_type": "pei"}
        self.requests: list[dict[str, Any]] = []
        self.reads = 0
        self.calls: list[str] = []
        self.printed: list[str] = []
        self.slept: list[float] = []
        self.questions: list[str] = []
        self.sources: list[dict[str, Any]] = []
        self.grants: list[Any] = []

    async def trigger(self) -> dict[str, Any]:
        return {"kind": "order.changed", "order_id": "o1"}

    async def inputs(self) -> dict[str, Any]:
        return self.settings

    async def tool(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append(name)
        if name == "order_get":
            self.reads += 1
            items, status = (self.items, "accepted")
            if self.reads > 1:
                items, status = self.later[min(self.reads - 2, len(self.later) - 1)]
            order = {"id": "o1", "number": "ORD-1", "status": status}
            return json.dumps({"order": order, "items": items})
        if name == "get_print_choices":
            self.sources.append(args)
            if args.get("library_file_id") is not None:
                return json.dumps(self.choices)
            if args.get("output_id") is None:
                # The tool's own failure (`ToolFailedError` in a real run).
                raise RuntimeError("no such output")
            return json.dumps(self.choices)
        if name == "order_notify_customer":
            return json.dumps({"text": "Order ORD-1 is ready.", "sent": False})
        raise AssertionError(f"unexpected tool {name}")

    async def queue_print(self, source: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        self.printed.append(source["order_item_id"])
        self.requests.append(request)
        return self.prints.pop(0)

    async def wait_for_human(self, question: str, timeout: int) -> dict[str, Any]:
        self.questions.append(question)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"answer": answer}

    async def sleep(self, seconds: float) -> None:
        self.calls.append("sleep")
        self.slept.append(seconds)

    async def agent(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("agent")
        self.grants.append(kwargs.get("grant"))
        return {"summary": "ok"}

    def run(self) -> Any:
        names = ("trigger", "inputs", "tool", "queue_print", "wait_for_human", "sleep", "agent")
        scope: dict[str, Any] = {name: getattr(self, name) for name in names}
        exec(SCRIPT.replace("asyncio.run(main())", "RESULT = asyncio.run(main())"), scope)
        return scope["RESULT"]


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
    assert "order_item_quote" not in host.calls
    assert "re-quote" in host.questions[0]
    # An early "print" before the new quote is accepted would pass the item over.
    assert "accept the new quote, then answer print" in host.questions[0]


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
    result = host.run()
    # A failed item prints again; a fulfilled one is not printed.
    assert host.printed == ["a", "b", "c"]
    assert result["skipped"] == ["a", "b"]


def test_a_failed_print_with_no_error_names_no_none_and_warns_of_a_queued_copy() -> None:
    failed = {"status": "failed", "problem": None, "error": None, "may_have_queued": True}
    host = _Host([_item("a")], [failed], ["skip"])
    host.run()
    assert "None" not in host.questions[0] and "no reason given" in host.questions[0]
    assert "check the queue before you answer print" in host.questions[0]


def test_the_check_waits_for_the_prints_to_end() -> None:
    """Review of #5682: a print run ends once its plates are queued, so the agent's check
    and the "ready" message wait until no printed item is queued or printing."""
    host = _Host(
        [_item("a"), _item("b")],
        [_OK, _OK],
        [],
        later=[
            ([_item("a", "printing"), _item("b", "queued")], "in_production"),
            ([_item("a", "printed"), _item("b", "printing")], "in_production"),
            ([_item("a", "printed"), _item("b", "printed")], "in_production"),
            ([_item("a", "fulfilled"), _item("b", "fulfilled")], "ready"),
        ],
        notify=True,
    )
    result = host.run()
    assert host.calls.count("sleep") == 2
    assert host.calls.index("agent") > max(i for i, c in enumerate(host.calls) if c == "sleep")
    assert host.calls[-1] == "order_notify_customer"
    assert result["message"] == {"text": "Order ORD-1 is ready.", "sent": False}


def test_prints_still_running_at_the_deadline_end_the_run_without_a_check() -> None:
    printing = ([_item("a", "printing")], "in_production")
    host = _Host([_item("a")], [_OK], [], later=[printing], notify=True, settle_hours=1)
    result = host.run()
    assert "agent" not in host.calls and "order_notify_customer" not in host.calls
    assert "did not end within 1 hours" in result["summary"]
    # The polls back off (300, 600, 1200 s), and the last waits only to the deadline.
    assert host.slept == [300, 600, 1200, 1500]


def test_the_longest_wait_keeps_the_run_history_bounded() -> None:
    """Review of #5682: a flow run is one workflow with no continue-as-new, and each poll
    adds a timer and a whole order read to its history. At the schema's maximum the polls
    must stay few: backed off to one an hour, about 170, never 2,016."""
    printing = ([_item("a", "printing")], "in_production")
    hours = INPUTS_SCHEMA["properties"]["settle_hours"]["maximum"]
    host = _Host([_item("a")], [_OK], [], later=[printing], settle_hours=hours)
    host.run()
    assert sum(host.slept) == hours * 3600
    assert max(host.slept) == 3600
    assert len(host.slept) <= 175


def test_no_ready_message_for_an_order_that_is_not_ready() -> None:
    host = _Host([_item("a")], [_OK], [], notify=True)
    result = host.run()
    assert "agent" in host.calls and "order_notify_customer" not in host.calls
    assert result["message"] is None


def test_print_choices_that_cannot_be_read_ask_a_person_and_the_next_item_prints() -> None:
    """Review of #5682: an item with no output (a manual quote) or one gone since makes
    `get_print_choices` fail; the flow asks, and the items after it still print."""
    missing = {**_item("a"), "output_id": None}
    host = _Host([missing, _item("b")], [_OK], ["skip"])
    result = host.run()
    assert host.printed == ["b"] and result["skipped"] == ["a"]
    assert "could not be read" in host.questions[0]
    # The error may be other than a gone output (review of #5682): never claimed as one.
    assert "or the error says what else went wrong" in host.questions[0]


def test_a_lan_only_printer_the_model_remembers_is_named() -> None:
    choices = {"default_nozzles": [], "bed_type": "pei", "model_choices": {"printer": "p-lan"}}
    host = _Host([_item("a")], [_OK], [], choices=choices)
    host.run()
    assert host.requests[0]["printer"] == "p-lan" and "printer_id" not in host.requests[0]


def test_only_a_timeout_counts_as_nobody_answering() -> None:
    host = _Host([_item("a")], [_refused("x")], [ValueError("bad timeout")])
    with pytest.raises(ValueError, match="bad timeout"):
        host.run()


def test_a_lan_print_with_no_spools_chosen_says_to_print_it_from_the_page() -> None:
    """Review of #5682: on the native (LAN) path the flow's empty plan is the run's, so
    the run refuses each slot; printing again would refuse the same way."""
    refused = {"status": "failed", "problem": None, "error": "Slot 1 has no spool chosen."}
    host = _Host([_item("a")], [refused], ["skip"])
    host.run()
    assert "print the item from the order's page, then answer skip" in host.questions[0]


def test_a_fixed_item_is_read_again_before_it_prints_again() -> None:
    """Review of #5682: a person's fix gives the item a new output; the retry prints
    from it, never from the item the run first read."""
    missing = {**_item("a"), "output_id": None}
    fixed = {**_item("a"), "output_id": "new"}
    host = _Host([missing], [_OK], ["print"], later=[([fixed], "in_production")])
    host.run()
    assert host.printed == ["a"]
    assert host.calls[:3] == ["order_get", "get_print_choices", "order_get"]


def test_an_item_no_longer_printable_after_the_question_is_passed_over() -> None:
    held = _refused("order_on_hold")
    gone = ([_item("a", "cancelled")], "in_production")
    host = _Host([_item("a")], [held], ["print"], later=[gone])
    result = host.run()
    assert host.printed == ["a"] and result["skipped"] == ["a"]


def test_a_print_that_queued_anyway_is_waited_for_and_never_sent_twice() -> None:
    """Review of #5682: a failed run with `may_have_queued` can leave the item queued; the
    re-read counts it as printed, so the check waits for it."""
    failed = {"status": "failed", "problem": None, "error": "lost", "may_have_queued": True}
    host = _Host(
        [_item("a")],
        [failed],
        ["print"],
        later=[([_item("a", "queued")], "in_production"), ([_item("a", "printed")], "ready")],
    )
    result = host.run()
    assert host.printed == ["a"]
    assert result["skipped"] == []
    assert "agent" in host.calls


def test_the_agent_may_attach_the_photo_and_timelapse_to_this_order_only() -> None:
    """Review of #5682: the prompt asks the agent to attach each attempt's photo and
    timelapse, so the grant names `order_attach` too, both rules kept to the order."""
    host = _Host([_item("a")], [_OK], [])
    host.run()
    [grant] = host.grants
    assert {rule["tool"] for rule in grant["rules"]} == {"order_item_fulfil", "order_attach"}
    assert all(rule["orders"] == ["o1"] for rule in grant["rules"])


def test_a_library_file_item_reads_its_choices_by_its_file() -> None:
    """Review of #5682: a `library_file` item's choices come from the library that
    serves, Bambuddy's or ScadBuddy's own (`get_library_choices`)."""
    item = {**_item("a"), "kind": "library_file", "output_id": None, "library_file": {"id": 7}}
    host = _Host([item], [_OK], [])
    host.run()
    assert host.sources == [{"library_file_id": 7}]
    assert host.printed == ["a"]
