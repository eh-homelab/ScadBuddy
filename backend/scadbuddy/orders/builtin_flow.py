"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2).

The orders component registers it at start as version 1 of an ordinary flow, only when
no flow of that name has ever had a version (`FlowDefinitions.register_new`): the shop
edits it into version 2 or deletes it, and a later build never rewrites it (a new
built-in is a new name). It runs on an `order.changed` trigger the shop makes, matched
to `accepted`, with a standing grant naming `queue_print` and `order_item_fulfil`;
nothing here makes that trigger or approves that grant.

A print refused `requote_needed` (the accepted quote's sliced file is gone or changed)
asks a person and never re-quotes: on an accepted order a new quote is open only after a
revision after acceptance (`Lifecycle._revisable_after_acceptance`), a person's call,
so the price the customer accepted stands until they make it.
"""

from __future__ import annotations

import logging
from typing import Any

from scadbuddy.flows.definitions import FlowDefinitions
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
#: How often the order is read while its prints run, in seconds.
POLL_S = 300


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
    the dialog's defaults otherwise. Its spools are filled from the item's filaments."""
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


async def print_item(order, item):
    """Print `item`, asking a person each time it does not start; whether it started."""
    source = {"order_id": order["id"], "order_item_id": item["id"]}
    while True:
        try:
            request = await print_request(item)
        except Exception as error:
            # No output (a manual quote), one gone since, or the print choices not read
            # (review of #5682): a person's to fix, never the run's end.
            question = (
                f"{item['title']}'s print choices could not be read ({error}): its output "
                "may be gone or missing. Fix it, then answer print, or skip."
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
                "is gone or changed. Revise and re-quote it, then answer print, or skip."
            )
        else:
            reason = out["error"] or "no reason given"
            question = f"The print of {item['title']} did not go: {out['status']}, {reason}. "
            if "no spool chosen" in reason:
                # The flow sends no spools: off the LAN the check fills each slot from the
                # item's filaments, but on the native (LAN) path the plan is the request's
                # (`orders/operations.py`), so the run refuses each slot. Printing again
                # would refuse the same way; a person chooses the spools on the order's
                # page (review of #5682).
                question += (
                    "This printer needs its spools chosen: print the item from the order's "
                    "page, then answer skip. "
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
    while True:
        detail = data(await tool("order_get", {"order_id": order_id}))
        busy = [i for i in detail["items"] if i["id"] in printed and i["status"] in IN_FLIGHT]
        if not busy:
            return detail
        if left <= 0:
            return None
        await sleep(POLL_S)
        left -= POLL_S


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
            grant={"rules": [{"fn": "tool", "tool": "order_item_fulfil", "orders": [order_id]}]},
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


async def ensure_builtin_flow(definitions: FlowDefinitions) -> bool:
    """Register version 1 of the built-in when the name never had a version; whether it
    did. A failure is logged: the app starts either way, and the next start tries again.
    Without the flows' repository there is nowhere to put it."""
    if not definitions.history.available:
        return False
    # The check a registration route applies (review of #5682): a build whose script no
    # longer type-checks registers nothing.
    if problems := await check_script(SCRIPT, INPUTS_SCHEMA, stored=True):
        logger.error(
            "the built-in %s flow does not type-check; not registered",
            NAME,
            extra={"problems": len(problems)},
        )
        return False
    try:
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
    return made is not None
