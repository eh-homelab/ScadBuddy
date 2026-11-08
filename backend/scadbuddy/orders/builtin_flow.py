"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2).

The orders component registers it at start as version 1 of an ordinary flow when no
flow of that name has ever had a version (`FlowDefinitions.register_new`). While the
newest version is still the built-in's own, a build with another script adds it as a new
version (#4673, `_upgraded`); once the shop edits it or deletes it, no build rewrites it.
A trigger pins its version, so each trigger of an older built-in version then moves to the
new one, its approved grant kept, only when the grant admits every call the new script
makes that its version did not; any other trigger stays, with :data:`NEWER_NOTICE`
(`move_triggers`). The built-in is ScadBuddy's own code in the image, and it never gets
more than the shop approved.
It runs on an `order.changed` trigger the shop makes, matched to `accepted`,
with a standing grant naming `queue_print`, `order_item_fulfil` and `order_attach` (an
agent call's grant only narrows the run's); nothing here makes that trigger or approves
that grant.

A print refused `requote_needed` (the accepted quote's sliced file is gone or changed)
asks a person and never re-quotes: on an accepted order a new quote is open only after a
revision after acceptance (`Lifecycle._revisable_after_acceptance`), a person's call,
so the price the customer accepted stands until they make it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from scadbuddy.flow_triggers.models import Trigger
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.flows.grants import GrantRule
from scadbuddy.flows.repository import FlowCommit
from scadbuddy.flows.typecheck import check_script

logger = logging.getLogger(__name__)

NAME = "order-fulfilment"
DISPLAY_NAME = "Order fulfilment"
DESCRIPTION = (
    "Prints each approved or failed line item of an accepted order from its accepted "
    "quote, asks a person when a print cannot start (the order on hold, or a stale "
    "quote to re-quote), waits for the prints to end, has an agent check them and "
    "record them, and writes the customer's message once the order is ready."
)
#: Who registered it: the build, never a person or an agent.
AUTHOR: dict[str, Any] = {"kind": "builtin", "name": NAME}
#: Shown on a trigger of an older built-in version that the upgrade did not move.
NEWER_NOTICE = (
    "A newer built-in flow exists: move this trigger to the newest version and approve its"
    " grant to use it."
)
#: A script's calls a grant can admit (`flows/grants.py` `HostFn`), and `agent(`, which
#: no rule admits: `queue_print(...)`, `arrange(...)`, `tool("<name>", ...)`.
_CALL = re.compile(r"""\b(queue_print|arrange|agent)\s*\(|\btool\(\s*["']([A-Za-z0-9_]+)["']""")

INPUTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notify": {
            "type": "boolean",
            "default": False,
            "description": "Write the customer's 'ready' message when the order is ready",
        },
        "settle_hours": {
            "type": "integer",
            "minimum": 1,
            "maximum": 168,
            "default": 48,
            "description": (
                "How long to wait for the prints to end before the check; past it the "
                "run ends with what is still printing, and no check"
            ),
        },
    },
    "additionalProperties": False,
}

SCRIPT = '''\
import asyncio
import json

FULFILMENT_REPORT = {
    "type": "object",
    "properties": {
        "fulfilled": {"type": "array", "items": {"type": "string"}},
        "failed": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["summary"],
}
#: What `order_item_print` prints: an approved item, or one whose attempt failed.
PRINTABLE = ["approved", "failed"]
#: How long a question to a person waits, in seconds. An unanswered one skips its item.
ASK_TIMEOUT_S = 86400
#: An item still on its way to the printer or on it.
IN_FLIGHT = ["queued", "printing"]
#: The first wait between reads of the order while its prints run, in seconds; each
#: wait doubles up to POLL_MAX_S. A run is one workflow with no continue-as-new, and
#: each read adds a timer and the whole order to its history, so the reads stay few: about
#: 170 at the longest settle_hours, not 2,016 (review of #5682).
POLL_S = 300
POLL_MAX_S = 3600


def data(text):
    """A tool's JSON answer, out of the untrusted-data envelope when it has one."""
    value = json.loads(text)
    if isinstance(value, dict) and "untrusted_data" in value:
        value = json.loads(value["untrusted_data"]["content"])
    return value


async def ask(question):
    """A person's answer, lower-cased; None when nobody answered in time, so one
    unanswered item never ends the run."""
    try:
        answer = await wait_for_human(question, ASK_TIMEOUT_S)
    except TimeoutError:
        return None
    return answer["answer"].strip().lower()


async def print_request(item):
    """The print dialog's body for `item`: its model's (or file's) remembered choices,
    the dialog's defaults otherwise. Its spools are filled from the item's filaments. A
    library file's choices come from the library that serves, Bambuddy's or ScadBuddy's
    own (`api/library_print.py` `get_library_choices`). An accepted quote that records its
    print setup (#4673) prints with it, so nothing is sent or read: other choices than the
    quote's are `requote_needed`."""
    accepted = [quote for quote in item.get("quotes") or [] if quote.get("accepted")]
    if accepted and accepted[0].get("choices") is not None:
        return {}
    if item.get("library_file") is not None:
        source = {"library_file_id": item["library_file"]["id"]}
    else:
        source = {"output_id": item["output_id"]}
    view = data(await tool("get_print_choices", source))
    remembered = view.get("model_choices") or {}
    choices = {
        "nozzles": remembered.get("nozzles") or view["default_nozzles"],
        "bed_type": view["bed_type"],
    }
    if remembered.get("tier"):
        choices["tier"] = remembered["tier"]
    if remembered.get("process_name"):
        choices["process_name"] = remembered["process_name"]
    request = {"filament_plan": {"slots": []}, "choices": choices}
    if view.get("printer_id") is not None:
        request["printer_id"] = view["printer_id"]
    elif remembered.get("printer"):
        # A LAN-only printer, kept by its registry id (#4971).
        request["printer"] = remembered["printer"]
    return request


async def current(order_id, item_id):
    """The item as it is now, or None when it is gone: a person's fix (a revision, a new
    quote) gives it a new output (review of #5682)."""
    detail = data(await tool("order_get", {"order_id": order_id}))
    for found in detail["items"]:
        if found["id"] == item_id:
            return found
    return None


async def print_item(order, item):
    """Print `item`, asking a person each time it does not start; whether it started.
    Each try after the first reads the item again."""
    source = {"order_id": order["id"], "order_item_id": item["id"]}
    first = True
    while True:
        if not first:
            item = await current(order["id"], item["id"])
            if item is None:
                return False
            if item["status"] in IN_FLIGHT:
                # A print that reached the queue anyway (`may_have_queued`): printed, so
                # the check waits for it, and never sent twice (review of #5682).
                return True
            if item["status"] not in PRINTABLE:
                return False
        first = False
        try:
            request = await print_request(item)
        except Exception as error:
            # No output (a manual quote), one gone since, or the print choices not read
            # (review of #5682): a person's to fix, never the run's end.
            question = (
                f"{item['title']}'s print choices could not be read ({error}). Its output "
                "may be gone or missing, or the error says what else went wrong. Fix it, "
                "then answer print, or skip."
            )
            if await ask(question) != "print":
                return False
            continue
        out = await queue_print(source, request)
        if out["status"] == "succeeded":
            return True
        if out["problem"] == "order_on_hold":
            # Spec 7.2: a hold pauses the flow's work until a person releases it.
            question = (
                f"Order {order['number']} is on hold, so {item['title']} did not print. "
                "Release the order, then answer print to print it, or skip."
            )
        elif out["problem"] == "requote_needed":
            # A re-quote of an accepted item is a person's call (a revision after
            # acceptance), never this flow's: the accepted price stands until then.
            question = (
                f"{item['title']} cannot print from its accepted quote: its sliced file "
                "is gone or changed. Revise and re-quote it, accept the new quote, then "
                "answer print, or skip."
            )
        else:
            reason = out["error"] or "no reason given"
            question = f"The print of {item['title']} did not go: {out['status']}, {reason}. "
            # The flow sends no spools: off the LAN the check fills each slot from the
            # item's filaments, but on the native (LAN) path the plan is the request's
            # (`orders/operations.py`), so the run refuses each slot ("no spool chosen").
            # Printing again would refuse the same way, so every failure names the way out
            # (review of #5682); no message text is matched.
            question += (
                "If it needs choices this flow does not make (such as its spools, on a LAN "
                "printer), print the item from the order's page, then answer skip. "
            )
            if out.get("may_have_queued"):
                question += (
                    "It may be on the printer's queue anyway: check the queue before you "
                    "answer print, or it may print twice. "
                )
            question += "Answer print to try again, or skip."
        if await ask(question) != "print":
            return False


async def settle(order_id, printed, hours):
    """The order once none of `printed` (item ids) is queued or printing: a print run
    ends once its plates are queued, not once they are printed. None past `hours`."""
    left = hours * 3600
    wait = POLL_S
    while True:
        detail = data(await tool("order_get", {"order_id": order_id}))
        busy = [i for i in detail["items"] if i["id"] in printed and i["status"] in IN_FLIGHT]
        if not busy:
            return detail
        if left <= 0:
            return None
        step = min(wait, left)
        await sleep(step)
        left -= step
        wait = min(wait * 2, POLL_MAX_S)


async def main():
    ev = await trigger()
    if ev is None or ev["kind"] != "order.changed":
        return "This flow runs on an order.changed trigger matched to accepted."
    settings = await inputs()
    order_id = ev["order_id"]
    detail = data(await tool("order_get", {"order_id": order_id}))
    order = detail["order"]
    printed = []
    skipped = []
    for item in detail["items"]:
        if item["status"] not in PRINTABLE or item["kind"] == "custom":
            continue
        if await print_item(order, item):
            printed.append(item["id"])
        else:
            skipped.append(item["title"])
    done = None
    if printed:
        settled = await settle(order_id, printed, settings["settle_hours"])
        if settled is None:
            return {
                "summary": (
                    f"Order {order['number']}'s prints did not end within "
                    f"{settings['settle_hours']} hours, so nothing was checked."
                ),
                "skipped": skipped,
                "message": None,
            }
        done = await agent(
            f"Check every printed item of order {order['number']} (id {order_id}): add "
            "the finished photo and the timelapse to each attempt, count the good ones as "
            "made, and tell me what failed.",
            ["print"],
            FULFILMENT_REPORT,
            # Both writes the prompt asks for, kept to this order (review of #5682).
            grant={"rules": [
                {"fn": "tool", "tool": "order_item_fulfil", "orders": [order_id]},
                {"fn": "tool", "tool": "order_attach", "orders": [order_id]},
            ]},
        )
    message = None
    if settings["notify"]:
        # Only an order the checks made ready, never one with prints failed or skipped.
        now = data(await tool("order_get", {"order_id": order_id}))
        if now["order"]["status"] == "ready":
            message = data(await tool("order_notify_customer", {
                "order_id": order_id, "template": "ready",
            }))
    return {"report": done, "skipped": skipped, "message": message}


asyncio.run(main())
'''


async def ensure_builtin_flow(
    definitions: FlowDefinitions, triggers: TriggerStore | None = None
) -> bool:
    """Register version 1 of the built-in when the name never had a version, or this
    build's script as a new version when the newest is still an unedited built-in one
    with another script (#4673: an install that registered an older built-in); whether it
    wrote one. A person's version, or a deleted flow, stands. A trigger pins its version,
    so with ``triggers`` each trigger of an older built-in version then moves to the
    newest, or gets :data:`NEWER_NOTICE` (:func:`move_triggers`). A failure is logged: the
    app starts either way, and the next start tries again. Without the flows' repository
    there is nowhere to put it."""
    if not definitions.history.available:
        return False
    try:
        # The usual start: the name has versions, so nothing to write unless the newest
        # is an older built-in. `register_new`'s locked check stays the guard (review of
        # #5682).
        versions = await asyncio.to_thread(definitions.history.versions, NAME)
        if versions:
            upgraded = await _upgraded(definitions, versions[0])
            if triggers is not None:
                # Every start, so a start that stopped between the two still moves them.
                await move_triggers(definitions, triggers)
            if upgraded:
                return True
            logger.info("the built-in %s flow was not registered: the name has versions", NAME)
            return False
        # The check a registration route applies (review of #5682): a build whose script
        # no longer type-checks registers nothing.
        if problems := await check_script(SCRIPT, INPUTS_SCHEMA, stored=True):
            logger.error(
                "the built-in %s flow does not type-check; not registered",
                NAME,
                extra={"problems": len(problems)},
            )
            return False
        made = await definitions.register_new(
            NAME,
            SCRIPT,
            AUTHOR,
            inputs_schema=INPUTS_SCHEMA,
            message="Add the built-in order-fulfilment flow",
            display_name=DISPLAY_NAME,
            description=DESCRIPTION,
        )
    except Exception:
        logger.exception("could not register the built-in %s flow", NAME)
        return False
    if made is None:
        # The pre-check saw no version, but `register_new`'s locked check did: another
        # replica registered it first (review of #5682).
        logger.info(
            "the built-in %s flow was not registered: another replica registered it first", NAME
        )
        return False
    return True


async def _upgraded(definitions: FlowDefinitions, newest: FlowCommit) -> bool:
    """This build's script as a new version over ``newest`` when it is the built-in's own
    (`BUILTIN_TRAILER`), not deleted, and another script; whether it wrote one. Checked
    again under the history's write lock (``expect_head``), so replicas starting together
    write it once."""
    if newest.builtin != NAME:
        return False
    state = await asyncio.to_thread(definitions.states.state, NAME)
    if state.deleted_at is not None:
        return False
    script = await asyncio.to_thread(definitions.history.script_at, NAME, newest.commit)
    if script == SCRIPT:
        return False
    if problems := await check_script(SCRIPT, INPUTS_SCHEMA, stored=True):
        logger.error(
            "the built-in %s flow does not type-check; not upgraded",
            NAME,
            extra={"problems": len(problems)},
        )
        return False
    made = await definitions.upgrade_builtin(
        NAME,
        SCRIPT,
        AUTHOR,
        head=newest.commit,
        inputs_schema=INPUTS_SCHEMA,
        message="Update the built-in order-fulfilment flow",
    )
    return made is not None


def script_calls(script: str) -> set[tuple[str, str | None]]:
    """The grant-relevant calls ``script`` makes, as ``(fn, tool)``: ``tool`` only for a
    ``tool("<name>", ...)`` call."""
    return {
        ("tool", match[2]) if match[2] else (match[1], None) for match in _CALL.finditer(script)
    }


def _admitted(call: tuple[str, str | None], rules: list[GrantRule]) -> bool:
    fn, tool = call
    return any(rule.fn == fn and (fn != "tool" or rule.tool in (None, tool)) for rule in rules)


async def move_triggers(definitions: FlowDefinitions, triggers: TriggerStore) -> int:
    """Move each trigger of the built-in's older, unedited versions to its newest (#4673),
    keeping the approved standing grant, when all of these hold: the newest version is
    the built-in's own; the trigger runs a version the built-in registered; its grant is
    approved and in force; and each call the new script makes that its version's did not
    is one the grant admits by function and tool (`agent(` never is). The bounds the rules
    set still apply to every call. A trigger left behind gets :data:`NEWER_NOTICE`, only
    while it still runs the version read (another replica or a person may have moved it
    meanwhile); a trigger on the newest loses any notice. A person moves one, which
    returns its grant to `requested`. Every trigger of the flow is the shop's: a build
    makes none. How many moved."""
    versions = await asyncio.to_thread(definitions.history.versions, NAME)
    if not versions or versions[0].builtin != NAME:
        return 0
    newest = versions[0]
    builtin = {v.version_id: v for v in versions if v.builtin == NAME}
    calls = script_calls(
        await asyncio.to_thread(definitions.history.script_at, NAME, newest.commit)
    )
    moved = 0
    for trigger in await triggers.list_triggers(name=NAME, limit=None):
        if trigger.definition_id == newest.version_id:
            if trigger.notice is not None:
                await triggers.set_notice(trigger.id, None, newest.version_id)
            continue
        if await _move(definitions, triggers, trigger, builtin, newest, calls):
            moved += 1
        else:
            await triggers.set_notice(trigger.id, NEWER_NOTICE, trigger.definition_id)
    if moved:
        logger.info("moved %d trigger(s) to the built-in %s flow's newest version", moved, NAME)
    return moved


async def move_after_approval(
    definitions: FlowDefinitions, triggers: TriggerStore, trigger: Trigger
) -> None:
    """After a person approves ``trigger``'s grant: when it is the built-in flow's, move
    its triggers now (:func:`move_triggers`), not only at the API's next start (#5732
    review). A failure is logged; the approval stands either way."""
    if trigger.name != NAME or not definitions.history.available:
        return
    try:
        await move_triggers(definitions, triggers)
    except Exception:
        logger.exception("could not move the built-in %s flow's triggers", NAME)


async def _move(
    definitions: FlowDefinitions,
    triggers: TriggerStore,
    trigger: Trigger,
    builtin: dict[str, FlowCommit],
    newest: FlowCommit,
    calls: set[tuple[str, str | None]],
) -> bool:
    old = builtin.get(trigger.definition_id)
    if old is None:
        return False  # a person's version
    grant = await triggers.current_grant(trigger.id)
    if grant is None or not grant.in_force(datetime.now(UTC)) or grant.approval_id is None:
        return False
    before = script_calls(await asyncio.to_thread(definitions.history.script_at, NAME, old.commit))
    if not all(_admitted(call, grant.spec.rules) for call in calls - before):
        return False
    made = await triggers.move_builtin(
        trigger.id, trigger.definition_id, newest.version_id, grant.approval_id, AUTHOR
    )
    return made is not None
