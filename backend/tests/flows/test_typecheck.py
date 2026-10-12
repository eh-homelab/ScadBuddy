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


async def test_tool_still_answers_text_and_tool_result_its_images() -> None:
    """#2477: `tool()` keeps its `str`; `tool_result()` adds the images, as pointers."""
    assert (
        await check_script(
            _script(
                "text = await tool('get_model', {'slug': 'box'})",
                "print(text.upper())",
                "r = await tool_result('get_render_preview', {'job_id': 'j'})",
                "first = r['images'][0]",
                "print(r['text'], first['name'], first['mediaType'], first['bytes'])",
                "await agent_with_images('What is this?', [], None, r['images'])",
                "await ask_session_with_images('s', 'And this?', [first])",
                "return first",
            )
        )
        == []
    )


async def test_an_image_is_a_typed_pointer() -> None:
    problems = await check_script(
        _script(
            "r = await tool_result('get_render_preview', {'job_id': 'j'})",
            "await agent_with_images('x', [], None, ['not an image'])",
        )
    )
    assert [p.line for p in problems] == [4]
