"""A flow script's check at registration and before each run (#1057, plan
2026-10-09-durable-phase-6-flows.md Task B2)."""

from scadbuddy.flows.typecheck import MAX_SCRIPT_BYTES, check_script


def _script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


async def test_a_clean_script_has_no_problems() -> None:
    assert await check_script(_script("await sleep(1)")) == []


async def test_a_type_error_is_reported_by_line() -> None:
    problems = await check_script(_script("await sleep('x')"))
    assert problems and problems[0].line == 3


async def test_a_syntax_error_is_a_problem() -> None:
    problems = await check_script(_script("await sleep(1"))
    assert problems and "invalid-syntax" in problems[0].message


async def test_an_unknown_host_function_is_a_problem() -> None:
    assert await check_script(_script("await render('box')"))


async def test_wait_for_human_needs_its_timeout() -> None:
    problems = await check_script(_script("await wait_for_human('swap spool?')"))
    assert [p.line for p in problems] == [3]


async def test_a_literal_wait_timeout_out_of_range() -> None:
    for timeout in ("timeout_s=5", "86401"):
        problems = await check_script(_script(f"await wait_for_human('swap spool', {timeout})"))
        assert [p.line for p in problems] == [3], timeout
        assert "10 to 86400" in problems[0].message


async def test_a_literal_wait_timeout_in_range() -> None:
    assert await check_script(_script("await wait_for_human('swap spool', timeout_s=60)")) == []


async def test_an_oversized_script() -> None:
    problems = await check_script("#" * (MAX_SCRIPT_BYTES + 1))
    assert [p.message for p in problems] == ["script is over 64 KiB"]
