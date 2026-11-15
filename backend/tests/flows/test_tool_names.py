"""A registration refuses a literal tool name the agent does not have (#6365)."""

from __future__ import annotations

from scadbuddy.flows.tool_names import unknown_tools

KNOWN = {"list_models", "send_to_bambuddy"}


def test_a_literal_name_the_agent_lacks_is_a_problem_on_its_line() -> None:
    script = "\n".join(
        [
            "async def main():",
            "    await tool('list_models', {})",
            "    await tool_result('resolve_makerworld_url', {'url': 'x'})",
            "    await tool(name='no_such_tool', args={})",
        ]
    )
    problems = unknown_tools(script, KNOWN)
    assert [(p.line, p.message.split(":")[0]) for p in problems] == [
        (3, "there is no tool 'resolve_makerworld_url'"),
        (4, "there is no tool 'no_such_tool'"),
    ]
    assert "not deployed yet" in problems[0].message


def test_names_it_cannot_check_are_left_to_the_run() -> None:
    script = "\n".join(
        [
            "async def main():",
            "    name = 'list_' + 'models'",
            "    await tool(name, {})",
            "    await tool('scadbuddy_http__http_request', {'method': 'GET'})",
            "    await tool('plugin_notes__send', {})",
            "    other.tool('not_a_host_function', {})",
        ]
    )
    assert unknown_tools(script, KNOWN) == []


def test_a_script_that_does_not_parse_has_no_problems_here() -> None:
    assert unknown_tools("def (:", KNOWN) == []
