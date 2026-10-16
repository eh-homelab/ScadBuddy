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
        "return t['slug'] + ':' + t['job_id']",
    )
    assert await check_script(script) == []


@pytest.mark.parametrize(
    "lines",
    [
        # Not every run has an event.
        ["return t['slug']"],
        # A finished render names its job, not a commit.
        ["if t is not None and t['kind'] == 'render.finished':", "    return t['commit']"],
    ],
)
async def test_the_type_check_catches_misuse_of_the_event(lines: list[str]) -> None:
    problems = await check_script(_script("t = await trigger()", *lines))
    assert problems, lines


def test_the_editor_offers_trigger() -> None:
    names = [f.name for f in host_interface({}).functions]
    assert "trigger" in names
