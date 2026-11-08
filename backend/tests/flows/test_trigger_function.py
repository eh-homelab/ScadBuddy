"""`trigger()` (#2666): the event that started a run, typed per kind, or None. It is
data the script reads, never authority: who the run acts as stays the trigger's record."""

import pytest

from scadbuddy.flows.host_interface import host_interface
from scadbuddy.flows.typecheck import check_script


def _script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


async def test_a_script_reads_its_event_by_kind() -> None:
    script = _script(
        "t = await trigger()",
        "if t is None:",
        "    return 'by hand or on a schedule'",
        "if t['kind'] == 'model.changed':",
        "    return t['slug'] + '@' + t['commit']",
        "if t['kind'] == 'render.finished':",
        "    return t['slug'] + ':' + t['job_id']",
        # The order kinds (#4252) are told apart too: what is left is a print's.
        "if t['kind'] == 'order.changed' or t['kind'] == 'order_item.changed':",
        "    return t['order_id']",
        "printer = t['printer'] or 'no printer'",
        "if t['kind'] == 'print.failed':",
        "    return 'failed ' + t['run_id'] + ' ' + t['output_id'] + ' ' + printer",
        "return 'finished ' + t['run_id'] + ' ' + printer",
    )
    assert await check_script(script) == []


@pytest.mark.parametrize(
    "lines",
    [
        # Not every run has an event.
        ["return t['slug']"],
        # A finished render names its job, not a commit.
        ["if t is not None and t['kind'] == 'render.finished':", "    return t['commit']"],
        # A print's event names its run, not a render's job (#2665).
        ["if t is not None and t['kind'] == 'print.failed':", "    return t['job_id']"],
        # Nor may a render's be read without telling it from a print's.
        ["if t is not None and t['kind'] != 'model.changed':", "    return t['job_id']"],
    ],
)
async def test_the_type_check_catches_misuse_of_the_event(lines: list[str]) -> None:
    problems = await check_script(_script("t = await trigger()", *lines))
    assert problems, lines


def test_the_editor_offers_trigger() -> None:
    names = [f.name for f in host_interface({}).functions]
    assert "trigger" in names


async def test_a_script_reads_an_order_event_by_kind() -> None:
    """#4252: the two order kinds, told apart by `kind` like the rest."""
    script = _script(
        "t = await trigger()",
        "if t is None:",
        "    return None",
        "if t['kind'] == 'order.changed':",
        "    return t['order_id'] + ' ' + t['status'] + ' ' + t['customer_id']",
        "if t['kind'] == 'order_item.changed':",
        "    return [t['order_id'], t['item_id'], t['status'], t['attempt']]",
        "return t['slug']",
    )
    assert await check_script(script) == []


@pytest.mark.parametrize(
    "lines",
    [
        # An order's event names no item.
        ["if t is not None and t['kind'] == 'order.changed':", "    return t['item_id']"],
        # An item's event names no customer.
        ["if t is not None and t['kind'] == 'order_item.changed':", "    return t['customer_id']"],
    ],
)
async def test_the_type_check_catches_misuse_of_an_order_event(lines: list[str]) -> None:
    problems = await check_script(_script("t = await trigger()", *lines))
    assert problems, lines
