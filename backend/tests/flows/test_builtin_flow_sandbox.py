"""The built-in order flow's `send_refusal` (#5660, review of #5919) in the sandbox a flow
script really runs in: Monty, not CPython. A string method Monty lacks fails here, not in a
customer's run."""

from __future__ import annotations

import ast
from typing import Any

import pydantic_monty

from scadbuddy.orders.builtin_flow import SCRIPT


def _send_refusal_source() -> str:
    """The script's `send_refusal` function, as the script defines it."""
    tree = ast.parse(SCRIPT)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "send_refusal":
            return ast.get_source_segment(SCRIPT, node) or ""
    raise AssertionError("SCRIPT defines no send_refusal")


def _in_monty(code: str) -> Any:
    """`code`'s last expression, run in Monty's sandbox as the flows harness runs a script."""
    with pydantic_monty.Monty() as pool, pool.checkout() as session:
        return session.feed_run(code)


def test_send_refusal_runs_in_the_real_sandbox() -> None:
    """Review of #5919: the script runs in Monty, not CPython, so `send_refusal`'s string
    methods are checked there: one missing fails here, not in a customer's run."""
    cases = {
        "ToolFailedError: already_notified: told": "already_notified: told",
        "ToolFailedError: record the message to order o1's customer failed (HTTP 409): told": (
            "record the message to order o1's customer failed (HTTP 409): told"
        ),
        "ToolFailedError: order_notify_customer_send was not run: no worker": (
            "order_notify_customer_send was not run: no worker"
        ),
        "ToolApprovalDenied: timed out": "the send was not approved: timed out",
        "ToolFailedError: order_notify_customer_send may have run: no heartbeat": None,
        "ToolFailedError: record the message failed (HTTP 503)": None,
        "TimeoutError: no answer": None,
    }
    code = (
        _send_refusal_source()
        + "\nout = []\nfor text in "
        + repr(list(cases))
        + ":\n    out.append(send_refusal(Exception(text)))\nout\n"
    )
    assert _in_monty(code) == list(cases.values())
