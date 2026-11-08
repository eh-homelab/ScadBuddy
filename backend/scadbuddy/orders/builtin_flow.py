"""The built-in `order-fulfilment` flow (#4252, orders spec §7.2).

The orders component registers it at start as version 1 of an ordinary flow, only when
no flow of that name has ever had a version (`FlowDefinitions.register_new`): the shop
edits it into version 2 or deletes it, and a later build never rewrites it (a new
built-in is a new name). It runs on an `order.changed` trigger the shop makes, matched
to `accepted`, with a standing grant naming `queue_print`, `order_item_accept_quote`
and `order_item_fulfil`; nothing here makes that trigger or approves that grant.
"""

from __future__ import annotations

import logging
from typing import Any

from scadbuddy.flows.definitions import FlowDefinitions

logger = logging.getLogger(__name__)

NAME = "order-fulfilment"
DISPLAY_NAME = "Order fulfilment"
DESCRIPTION = (
    "Prints each approved line item of an accepted order from its accepted quote, quotes "
    "a stale one again (accepting a rise under the shop's threshold, asking a person "
    "above it), has an agent check the prints and record them, and writes the "
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
        "auto_accept_quotes_under_minor": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": (
                "A new quote that rises less than this (minor units) over the accepted "
                "one is accepted without asking"
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


def data(text):
    """A tool's JSON answer, out of the untrusted-data envelope when it has one."""
    value = json.loads(text)
    if isinstance(value, dict) and "untrusted_data" in value:
        value = json.loads(value["untrusted_data"]["content"])
    return value


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


def accepted_total(item):
    for quote in item["quotes"]:
        if quote["accepted"]:
            return quote["total_minor"]
    return 0


async def requote(order, item, request, threshold):
    """A new slice quote for `item`, accepted when the rise is under `threshold` or a
    person says yes; whether it was."""
    filaments = {}
    for colour in item["colours"]:
        if colour.get("filament_id"):
            filaments[str(colour["slot"])] = colour["filament_id"]
    quote = data(await tool("order_item_quote", {
        "order_id": order["id"],
        "item_id": item["id"],
        "slice": {**request, "filaments": filaments},
    }))
    was = accepted_total(item)
    rise = quote["total_minor"] - was
    if rise >= threshold:
        answer = await wait_for_human(
            f"{item['title']} re-quoted at {quote['total_minor']} minor units "
            f"(accepted {was}). Accept and print? Answer yes to go on.",
            86400,
        )
        if answer["answer"].strip().lower() != "yes":
            return False
    await tool("order_item_accept_quote", {
        "order_id": order["id"], "item_id": item["id"], "quote_id": quote["id"],
    })
    return True


async def main():
    ev = await trigger()
    if ev is None or ev["kind"] != "order.changed":
        return "This flow runs on an order.changed trigger matched to accepted."
    settings = await inputs()
    order_id = ev["order_id"]
    detail = data(await tool("order_get", {"order_id": order_id}))
    order = detail["order"]
    for item in detail["items"]:
        if item["status"] != "approved" or item["kind"] == "custom":
            continue
        source = {"order_id": order_id, "order_item_id": item["id"]}
        request = await print_request(item)
        out = await queue_print(source, request)
        if out["problem"] == "order_on_hold":
            # §7.2: a hold pauses the flow's work; the person releases it, then it goes on.
            await wait_for_human(
                f"Order {order['number']} is on hold, so {item['title']} did not print. "
                "Release the order, then answer anything to print it.",
                86400,
            )
            out = await queue_print(source, request)
        if out["problem"] == "requote_needed":
            threshold = settings["auto_accept_quotes_under_minor"]
            if not await requote(order, item, request, threshold):
                continue
            out = await queue_print(source, request)
        if out["status"] != "succeeded":
            await wait_for_human(
                f"The print of {item['title']} did not go: {out['status']}, {out['error']}. "
                "Answer anything once you have looked at it.",
                86400,
            )
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
    return {"report": done, "message": message}


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
