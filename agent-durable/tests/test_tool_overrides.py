"""Settings → Plugins overrides as the durable gate reads them (#2088): sanitised as
builtInTools.ts does (TIGHTEN ONLY), and applied to the durable tools each turn."""

from __future__ import annotations

from session_support import MANIFEST
from temporalio.claude_agent_sdk import DurableTool

from scadbuddy_durable.session import tools
from scadbuddy_durable.session.models import TurnTool


def test_only_raises_of_known_tools_are_kept() -> None:
    tiers, disabled = tools.sanitise_overrides(
        MANIFEST,
        {
            "tool_tiers": {
                "render_preview": "outward",  # read → outward: kept
                "print_output": "read",  # a lowering: dropped
                "ask_user": "outward",  # an answer tool is in no Settings set
                "no_such_tool": "outward",
                "wait_for_user": "bogus",
            },
            "disabled_tools": ["render_preview", "no_such_tool", "ask_user", 3, "render_preview"],
        },
    )
    assert tiers == {"render_preview": "outward"}
    assert disabled == ["render_preview"]


def test_a_row_of_the_wrong_shape_overrides_nothing() -> None:
    shapes: list[object] = [None, "x", [], {"tool_tiers": [], "disabled_tools": {}}]
    for stored in shapes:
        assert tools.sanitise_overrides(MANIFEST, stored) == ({}, [])


def _gates() -> dict[str, DurableTool]:
    return {t.name: t for t in tools.durable_tools(MANIFEST)}


def test_a_raised_tool_parks_and_runs_once_and_is_reset_when_the_override_goes() -> None:
    by_name = _gates()
    assert not by_name["render_preview"].needs_approval
    tools.apply_overrides(by_name, MANIFEST, {"render_preview": "outward"}, set())
    assert by_name["render_preview"].needs_approval
    assert by_name["render_preview"].retry_policy is not None
    assert by_name["render_preview"].retry_policy.maximum_attempts == 1
    tools.apply_overrides(by_name, MANIFEST, {"render_preview": "write"}, set())
    assert not by_name["render_preview"].needs_approval
    assert by_name["render_preview"].retry_policy.maximum_attempts == 3
    # The manifest's own gates are untouched.
    assert by_name["print_output"].needs_approval
    assert by_name["ask_user"].needs_approval


def test_a_disabled_tool_never_parks() -> None:
    by_name = _gates()
    tools.apply_overrides(
        by_name, MANIFEST, {"render_preview": "outward"}, {"render_preview", "print_output"}
    )
    assert not by_name["render_preview"].needs_approval
    assert not by_name["print_output"].needs_approval
    # An outward call still runs at most once if it is ever let through.
    assert by_name["print_output"].retry_policy is not None
    assert by_name["print_output"].retry_policy.maximum_attempts == 1
    tools.apply_overrides(by_name, MANIFEST, {}, set())
    assert by_name["print_output"].needs_approval


def _turn_tool(name: str, may_park: bool = True) -> TurnTool:
    return TurnTool(name=name, description=name, input_schema={"type": "object"}, may_park=may_park)


def test_each_turn_replaces_the_last_turns_tools_and_never_a_manifest_tool() -> None:
    """#2320: what turn_tools listed is offered this turn, and nothing of last turn's."""
    offered = _gates()
    held = dict(offered)
    first = tools.offer_turn_tools(
        offered, held, set(), [_turn_tool("notes__post"), _turn_tool("hindsight__recall", False)]
    )
    assert first == {"notes__post", "hindsight__recall"}
    assert offered["notes__post"].needs_approval
    assert offered["notes__post"].retry_policy is not None
    assert offered["notes__post"].retry_policy.maximum_attempts == 1
    assert not offered["hindsight__recall"].needs_approval
    # A plugin removed since: gone from both tables, and a name without "__" or one the
    # manifest has is never taken.
    second = tools.offer_turn_tools(
        offered, held, first, [_turn_tool("render_preview"), _turn_tool("plain")]
    )
    assert second == set()
    assert "notes__post" not in offered and "notes__post" not in held
    assert offered["render_preview"] is held["render_preview"]
    assert not offered["render_preview"].needs_approval


def test_a_pinned_turn_tool_runs_on_its_replicas_queue_with_a_deadline() -> None:
    [pinned, anywhere] = tools.turn_tools(
        [
            TurnTool("scadbuddy_http__http_request", "", {}, True, "agent-tools.pod-a"),
            _turn_tool("notes__post"),
        ]
    )
    assert pinned.task_queue == "agent-tools.pod-a"
    assert pinned.schedule_to_close_timeout == tools.PINNED_DEADLINE
    assert anywhere.task_queue == "agent-tools"
    assert anywhere.schedule_to_close_timeout is None


def test_a_tool_the_sdk_refuses_is_left_out_not_raised() -> None:
    """A 54-character plugin tool name failed every workflow task of its session; it is
    now left out and the rest of the turn's tools are offered."""
    too_long = "plugin_playwright_playwright__browser_console_messages"
    offered = _gates()
    names = tools.offer_turn_tools(
        offered, dict(offered), set(), [_turn_tool(too_long), _turn_tool("notes__post")]
    )
    assert names == {"notes__post"}
    assert too_long not in offered
