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

logger = logging.getLogger(__name__)

NAME = "order-fulfilment"
DISPLAY_NAME = "Order fulfilment"
DESCRIPTION = (
    "Prints each approved or failed line item of an accepted order from its accepted "
    "quote, asks a person when a print cannot start (the order on hold, or a stale "
    "quote to re-quote), has an agent check the prints and record them, and writes the "
    "customer's message when asked to."
)
#: Who registered it: the build, never a person or an agent.
AUTHOR: dict[str, Any] = {"kind": "builtin", "name": NAME}

INPUTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notify": {
            "type": "boolean",
            "default": False,
            "description": "Write the customer's 'ready' message when the order is done",
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
    except Exception:
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
    return request


async def print_item(order, item):
    """Print `item`, asking a person each time it does not start; whether it started."""
    source = {"order_id": order["id"], "order_item_id": item["id"]}
    request = await print_request(item)
    while True:
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
            question = (
                f"The print of {item['title']} did not go: {out['status']}, "
                f"{out['error']}. Answer print to try again, or skip."
            )
        if await ask(question) != "print":
            return False


async def main():
    ev = await trigger()
    if ev is None or ev["kind"] != "order.changed":
        return "This flow runs on an order.changed trigger matched to accepted."
    settings = await inputs()
    order_id = ev["order_id"]
    detail = data(await tool("order_get", {"order_id": order_id}))
    order = detail["order"]
    skipped = []
    for item in detail["items"]:
        if item["status"] not in PRINTABLE or item["kind"] == "custom":
            continue
        if not await print_item(order, item):
            skipped.append(item["title"])
    done = await agent(
        f"Check every printed item of order {order['number']} (id {order_id}): add the "
        "finished photo and the timelapse to each attempt, count the good ones as made, "
        "and tell me what failed.",
        ["print"],
        FULFILMENT_REPORT,
        grant={"rules": [{"fn": "tool", "tool": "order_item_fulfil", "orders": [order_id]}]},
    )
    message = None
    if settings["notify"]:
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
