"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2): its script type-checks
against its inputs, and it is registered once, as version 1, and never over a shop's
edit or delete."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.core.events import OrderChanged, OrderItemChanged
from scadbuddy.flow_triggers.events import fire_event, trigger_event
from scadbuddy.flow_triggers.models import EventTiming, ScheduleTiming, Trigger
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.grants import GrantRule, GrantSpec
from scadbuddy.flows.reconcile import created_by
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.states import FlowStates
from scadbuddy.flows.store import FlowStore
from scadbuddy.flows.typecheck import check_script
from scadbuddy.orders import builtin_flow, component
from scadbuddy.orders.builtin_flow import (
    INPUTS_SCHEMA,
    NAME,
    NEWER_NOTICE,
    SCRIPT,
    ensure_builtin_flow,
    move_after_approval,
    move_triggers,
    script_calls,
    unread_tool_calls,
)
from scadbuddy.render.projection import JobProjection
from tests.test_flow_trigger_events import _Client, new_cart


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


#: A built-in version a running install registered before this build's script (#5682's
#: sent the model's remembered choices, which #4673 refuses against a quote's setup).
OLD_SCRIPT = SCRIPT.replace(
    '    if accepted and accepted[0].get("choices") is not None:\n        return {}\n', ""
)


async def _registered_old(definitions: FlowDefinitions) -> None:
    await definitions.start()
    assert OLD_SCRIPT != SCRIPT
    made = await definitions.register_new(
        NAME,
        OLD_SCRIPT,
        builtin_flow.AUTHOR,
        inputs_schema=INPUTS_SCHEMA,
        message="Add the built-in order-fulfilment flow",
        display_name=builtin_flow.DISPLAY_NAME,
        description=builtin_flow.DESCRIPTION,
    )
    assert made is not None


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_an_unedited_built_in_is_brought_up_to_this_builds_script(
    definitions: FlowDefinitions,
) -> None:
    """#5732 review: an install that registered an older built-in gets this build's as a
    new version, once, even from two replicas starting together; a person's own version
    (the test above) or a delete stands."""
    await _registered_old(definitions)
    results = await asyncio.gather(
        ensure_builtin_flow(definitions), ensure_builtin_flow(definitions)
    )
    assert sorted(results) == [False, True]
    newest, first = definitions.history.versions(NAME)
    assert definitions.history.script_at(NAME, newest.commit) == SCRIPT
    assert definitions.history.script_at(NAME, first.commit) == OLD_SCRIPT
    assert newest.builtin == NAME
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 2


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_an_edited_built_in_is_not_replaced(definitions: FlowDefinitions) -> None:
    await _registered_old(definitions)
    await definitions.register(NAME, "return 'mine'", {"kind": "browser"}, approval_timeout_s=None)
    assert await ensure_builtin_flow(definitions) is False
    newest, _ = definitions.history.versions(NAME)
    assert definitions.history.script_at(NAME, newest.commit) == "return 'mine'"


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_deleted_built_in_is_not_brought_back(definitions: FlowDefinitions) -> None:
    await _registered_old(definitions)
    await definitions.delete(NAME, {"kind": "browser"})
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 1


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_built_in_deleted_after_the_check_stays_deleted(
    definitions: FlowDefinitions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5732 security review: a person deletes the flow after `_upgraded` read it as
    not deleted; the upgrade's locked write refuses, so the delete stands."""
    await _registered_old(definitions)
    checked = check_script

    async def check_then_delete(*args: Any, **kwargs: Any) -> Any:
        problems = await checked(*args, **kwargs)
        await definitions.delete(NAME, {"kind": "browser"})
        return problems

    monkeypatch.setattr(builtin_flow, "check_script", check_then_delete)
    assert await ensure_builtin_flow(definitions) is False
    assert definitions.states.state(NAME).deleted_at is not None
    assert len(definitions.history.versions(NAME)) == 1


@pytest.fixture
def triggers(jobs: JobProjection) -> TriggerStore:
    return TriggerStore(jobs.pool)


#: What a shop approves for the built-in's trigger (its docstring): the print, and the
#: tools that record and attach.
BUILTIN_RULES = [
    GrantRule(fn="queue_print", orders="any"),
    GrantRule(fn="tool", tool="order_item_fulfil"),
    GrantRule(fn="tool", tool="order_attach"),
]


async def _approved_trigger(
    triggers: TriggerStore, definition_id: str, event: bool = False
) -> Trigger:
    """A trigger the shop made on ``definition_id`` (hourly, or with ``event`` on
    `order.changed`, as the built-in's docstring has it), its grant of `BUILTIN_RULES`
    approved as the browser approves one."""
    browser = {"kind": "browser"}
    timing: dict[str, Any] = (
        {"kind": "event", "event": EventTiming.model_validate({"kind": "order.changed"})}
        if event
        else {"kind": "schedule", "schedule": ScheduleTiming(interval_s=3600)}
    )
    trigger = await triggers.insert(
        Trigger.model_validate(
            {
                "id": str(uuid.uuid4()),
                "name": NAME,
                "definition_id": definition_id,
                **timing,
                "overlap": "skip",
                "paused": False,
                "created_by": browser,
                "created_at": datetime.now(UTC),
            }
        )
    )
    spec = GrantSpec(expires_at=datetime.now(UTC) + timedelta(days=6), rules=BUILTIN_RULES)
    seen = await asyncio.to_thread(triggers.live_hash, trigger)
    assert await triggers.attach_grant(trigger.id, spec, "approved", browser, seen)
    return trigger


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_trigger_on_the_older_built_in_runs_the_new_one(
    definitions: FlowDefinitions, triggers: TriggerStore
) -> None:
    """#5732 review: a trigger pins its version, so the upgrade moves the shop's trigger
    of the unedited built-in to the new version, keeping its approved grant: the new
    script calls nothing the old one did not."""
    await _registered_old(definitions)
    [old] = definitions.history.versions(NAME)
    trigger = await _approved_trigger(triggers, old.version_id)

    assert await ensure_builtin_flow(definitions, triggers) is True
    new = definitions.history.versions(NAME)[0]
    moved = await triggers.get(trigger.id)
    assert moved is not None
    assert moved.definition_id == new.version_id
    assert moved.notice is None
    grant = await triggers.current_grant(trigger.id)
    assert grant is not None and grant.in_force(datetime.now(UTC))
    # What its next fire runs: the pinned version's script (`FlowTriggerFire._decide`).
    pinned = await definitions.get(moved.definition_id)
    assert pinned is not None and pinned.script == SCRIPT
    # Started again, nothing moves twice.
    assert await ensure_builtin_flow(definitions, triggers) is False
    again = await triggers.get(trigger.id)
    assert again is not None and again.updated_at == moved.updated_at


def test_every_call_of_the_script_is_seen_by_the_widening_check() -> None:
    """#5732 review: `script_calls` reads `tool("<literal>", ...)` only, so a built-in
    that named a tool by a variable would slip past it. Each `tool(` call is a literal."""
    assert unread_tool_calls(SCRIPT) == 0
    assert {fn for fn, _ in script_calls(SCRIPT)} >= {"queue_print", "tool", "agent"}


def test_a_tool_mention_in_a_comment_or_a_string_is_no_call() -> None:
    """#5761 review: only a real call counts, so a mention never blocks the built-in."""
    mentioned = '# call tool(name) here\nx = "tool(name)"\ntool("order_get", {})\n'
    assert unread_tool_calls(mentioned) == 0
    assert unread_tool_calls('name = "order_get"\ntool(name, {})\n') == 1
    assert unread_tool_calls('tool(str("order_get"), {})\n') == 1


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_script_that_names_a_tool_by_a_variable_is_never_registered(
    definitions: FlowDefinitions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5732 review: the widening check reads `tool("<literal>", ...)` only, so a built-in
    whose script names a tool any other way writes no version: neither a first one nor
    an upgrade."""
    hidden = SCRIPT.replace('tool("order_get", ', 'tool(str("order_get"), ', 1)
    assert hidden != SCRIPT
    monkeypatch.setattr(builtin_flow, "SCRIPT", hidden)
    await definitions.start()
    assert await ensure_builtin_flow(definitions) is False
    assert definitions.history.versions(NAME) == []

    await _registered_old(definitions)
    assert await ensure_builtin_flow(definitions) is False
    assert len(definitions.history.versions(NAME)) == 1


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_trigger_on_a_persons_older_version_is_left_with_the_notice(
    definitions: FlowDefinitions, triggers: TriggerStore
) -> None:
    """#5732 review: a trigger of a person's version is never moved, even below a newer
    built-in one; it shows the notice, and a person moves it."""
    await _registered_old(definitions)
    persons = await definitions.register(
        NAME,
        OLD_SCRIPT + "\n# the shop's edit\n",
        {"kind": "browser"},
        approval_timeout_s=None,
        inputs_schema=INPUTS_SCHEMA,
    )
    [mine, _] = definitions.history.versions(NAME)
    assert mine.version_id == persons.id and mine.builtin is None
    trigger = await _approved_trigger(triggers, mine.version_id)
    # A built-in version above it (as a restore of the built-in's would leave).
    made = await definitions.upgrade_builtin(
        NAME,
        SCRIPT,
        builtin_flow.AUTHOR,
        head=mine.commit,
        inputs_schema=INPUTS_SCHEMA,
        message="Update the built-in order-fulfilment flow",
    )
    assert made is not None

    assert await move_triggers(definitions, triggers) == 0
    left = await triggers.get(trigger.id)
    assert left is not None
    assert left.definition_id == mine.version_id
    assert left.notice == NEWER_NOTICE


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_replicas_moving_together_leave_no_notice(
    definitions: FlowDefinitions, triggers: TriggerStore
) -> None:
    """#5732 review: a replica that finds the trigger moved by another (or by a person)
    meanwhile sets no notice on it, and a notice left on a trigger of the newest version
    is cleared."""
    await _registered_old(definitions)
    [old] = definitions.history.versions(NAME)
    trigger = await _approved_trigger(triggers, old.version_id)
    assert await ensure_builtin_flow(definitions) is True

    counts = await asyncio.gather(
        move_triggers(definitions, triggers), move_triggers(definitions, triggers)
    )
    assert sorted(counts) == [0, 1]
    moved = await triggers.get(trigger.id)
    assert moved is not None and moved.notice is None

    # A notice that outlived its reason (a start before this fix) goes at the next start.
    await triggers.set_notice(trigger.id, NEWER_NOTICE, moved.definition_id)
    await move_triggers(definitions, triggers)
    cleared = await triggers.get(trigger.id)
    assert cleared is not None and cleared.notice is None


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_grant_approved_later_moves_the_trigger_then(
    definitions: FlowDefinitions, triggers: TriggerStore
) -> None:
    """#5732 review: a trigger left behind for want of an approved grant moves once a
    person approves one, not only at the API's next start."""
    await _registered_old(definitions)
    [old] = definitions.history.versions(NAME)
    trigger = await _approved_trigger(triggers, old.version_id)
    await triggers.revoke_grant(trigger.id, "browser")
    assert await ensure_builtin_flow(definitions, triggers) is True
    left = await triggers.get(trigger.id)
    assert left is not None and left.notice == NEWER_NOTICE

    spec = GrantSpec(expires_at=datetime.now(UTC) + timedelta(days=6), rules=BUILTIN_RULES)
    seen = await asyncio.to_thread(triggers.live_hash, left)
    assert await triggers.attach_grant(trigger.id, spec, "approved", {"kind": "browser"}, seen)
    await move_after_approval(definitions, triggers, left)
    moved = await triggers.get(trigger.id)
    assert moved is not None
    assert moved.definition_id == definitions.history.versions(NAME)[0].version_id
    assert moved.notice is None


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_moved_trigger_still_never_fires_on_a_cart(
    definitions: FlowDefinitions, triggers: TriggerStore, jobs: JobProjection
) -> None:
    """#5282: a storefront visitor's cart starts none of the owner's flows, the built-in's
    moved trigger included (`flow_triggers/events.py` `trigger_event`)."""
    await _registered_old(definitions)
    [old] = definitions.history.versions(NAME)
    trigger = await _approved_trigger(triggers, old.version_id, event=True)
    assert await ensure_builtin_flow(definitions, triggers) is True
    moved = await triggers.get(trigger.id)
    assert moved is not None and moved.definition_id != old.version_id

    new_cart(jobs)
    client = _Client()
    for event in (
        OrderChanged(order_id="cart", status="draft", cart=True),
        OrderItemChanged(order_id="cart", item_id="ci", status="draft", cart=True),
    ):
        assert trigger_event(event) is None
        assert await fire_event(client, "library", triggers, event) == []  # type: ignore[arg-type]
    assert client.started == []


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_trigger_whose_grant_would_widen_is_not_moved(
    definitions: FlowDefinitions, triggers: TriggerStore
) -> None:
    """A new version that calls a tool the trigger's approved grant does not admit (here
    `order_notify_customer`, which the older script never called) leaves the trigger on
    its version, with a notice: a person approves the new one."""
    await definitions.start()
    older = OLD_SCRIPT.replace('tool("order_notify_customer"', 'tool("order_get"')
    assert older != OLD_SCRIPT
    made = await definitions.register_new(
        NAME,
        older,
        builtin_flow.AUTHOR,
        inputs_schema=INPUTS_SCHEMA,
        message="Add it",
        display_name=builtin_flow.DISPLAY_NAME,
        description=builtin_flow.DESCRIPTION,
    )
    assert made is not None
    [old] = definitions.history.versions(NAME)
    trigger = await _approved_trigger(triggers, old.version_id)

    assert await ensure_builtin_flow(definitions, triggers) is True
    left = await triggers.get(trigger.id)
    assert left is not None
    assert left.definition_id == old.version_id
    assert left.notice == NEWER_NOTICE
    grant = await triggers.current_grant(trigger.id)
    assert grant is not None and grant.in_force(datetime.now(UTC))
    # A person moves it: the notice goes, and the grant waits for their approval.
    new = definitions.history.versions(NAME)[0]
    edited = await triggers.update(
        trigger.id, {"definition_id": new.version_id}, {"kind": "browser"}, "browser"
    )
    assert edited is not None and edited.notice is None
    grant = await triggers.current_grant(trigger.id)
    assert grant is not None and grant.status == "requested"


@pytest.mark.requires_postgres
@pytest.mark.requires_git
async def test_a_trigger_with_no_approved_grant_is_not_moved(
    definitions: FlowDefinitions, triggers: TriggerStore
) -> None:
    await _registered_old(definitions)
    [old] = definitions.history.versions(NAME)
    trigger = await _approved_trigger(triggers, old.version_id)
    await triggers.revoke_grant(trigger.id, "browser")

    assert await ensure_builtin_flow(definitions, triggers) is True
    left = await triggers.get(trigger.id)
    assert left is not None
    assert left.definition_id == old.version_id
    assert left.notice == NEWER_NOTICE


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

    async def ensure(definitions: object, triggers: object = None) -> bool:
        calls.append(definitions)
        return True

    async def remove() -> None:
        return None

    monkeypatch.setattr(component, "ensure_builtin_flow", ensure)
    monkeypatch.setattr(component, "listen_for_prints", lambda *args: remove)
    orders = SimpleNamespace(
        flows=flows,
        core=SimpleNamespace(
            settings=SimpleNamespace(orders_builtin_flow=switch),
            events=None,
            projection=SimpleNamespace(pool=None),
        ),
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

    async def ensure(definitions: object, triggers: object = None) -> bool:
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
        core=SimpleNamespace(
            settings=SimpleNamespace(orders_builtin_flow=True),
            events=None,
            projection=SimpleNamespace(pool=None),
        ),
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

    def run(self, script: str = SCRIPT) -> Any:
        names = ("trigger", "inputs", "tool", "queue_print", "wait_for_human", "sleep", "agent")
        scope: dict[str, Any] = {name: getattr(self, name) for name in names}
        exec(script.replace("asyncio.run(main())", "RESULT = asyncio.run(main())"), scope)
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


def test_the_print_leaves_the_tower_supports_and_sequence_to_the_quote() -> None:
    """#4673: the flow's request names none of them, so `order_item_print` prints each
    item with its accepted quote's (`print_body`'s `setup`), what the quote priced."""
    host = _Host([_item("a")], [_OK], [])
    host.run()
    [request] = host.requests
    assert not {"prime_tower", "supports", "print_sequence"} & set(request)


def test_an_item_whose_quote_records_its_setup_prints_with_it() -> None:
    """#4673 PR 2: an accepted quote that records its print setup is printed with it, so
    the flow sends no choices of its own (the model's remembered ones would differ from
    the quote's and be `requote_needed`), and reads none."""
    item = {**_item("a"), "quotes": [{"accepted": True, "choices": {"choices": {}}}]}
    host = _Host([item], [_OK], [])
    host.run()
    assert host.requests == [{}]
    assert "get_print_choices" not in host.calls
    # Version 1's script (#5682) sent the model's remembered choices against the same
    # quote, which the print now refuses: the reason an unedited one is upgraded
    # (`test_an_unedited_built_in_is_brought_up_to_this_builds_script`).
    old = _Host([item], [_OK], [])
    old.run(OLD_SCRIPT)
    [request] = old.requests
    assert request["choices"] and "get_print_choices" in old.calls


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


async def test_a_registration_lost_to_another_replica_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #5682: the pre-check saw no version, but another replica registered it
    first (`register_new` answers None); the log tells that apart from the pre-check."""

    async def register_new(*args: object, **kwargs: object) -> None:
        return None

    history = SimpleNamespace(available=True, versions=lambda name: [])
    definitions = SimpleNamespace(history=history, register_new=register_new)
    with caplog.at_level("INFO", logger="scadbuddy.orders.builtin_flow"):
        made = await ensure_builtin_flow(cast(FlowDefinitions, definitions))
    assert made is False
    assert "another replica registered it first" in caplog.text
    assert "the name has versions" not in caplog.text
