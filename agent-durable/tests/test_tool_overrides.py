"""Settings → Tools & plugins access as the durable gate reads it (#2088, #2171): the
``ai_access`` rows of the registry sources, by the rules the shared vectors pin, in either
direction, and applied to the durable tools each turn."""

from __future__ import annotations

from session_support import MANIFEST
from temporalio.claude_agent_sdk import DurableTool

from scadbuddy_durable.session import tools
from scadbuddy_durable.session.access import AccessRow
from scadbuddy_durable.session.models import TurnTool

S = "builtin:scadbuddy"
BY = "browser:browser"


def test_lowered_and_raised_tiers_of_known_tools_are_kept() -> None:
    tiers, disabled = tools.sanitise_overrides(
        MANIFEST,
        [
            AccessRow(S, "render_preview", "outward", BY),  # read → outward: a raise
            AccessRow(S, "print_output", "read", BY),  # outward → read: a lowering
            AccessRow(S, "ask_user", "outward", BY),  # an answer tool is in no source
            AccessRow(S, "no_such_tool", "outward", BY),
            AccessRow(S, "wait_for_user", "bogus", BY),
            AccessRow("remote:hs", "render_preview", "off", BY),  # another source's row
        ],
    )
    assert tiers == {"render_preview": "outward", "print_output": "read"}
    assert disabled == []


def test_a_row_of_the_wrong_shape_overrides_nothing() -> None:
    for value in ("on", "none", ""):
        rows = [AccessRow(S, e.name, value, BY) for e in MANIFEST]
        assert tools.sanitise_overrides(MANIFEST, rows) == ({}, [])


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


def test_a_lowered_outward_tool_never_parks() -> None:
    """#2171: Settings may lower a tool. print_output at write runs without approval, and
    still at most once: an approval tool's effect is never retried."""
    by_name = _gates()
    tools.apply_overrides(by_name, MANIFEST, {"print_output": "write"}, set())
    assert not by_name["print_output"].needs_approval
    assert by_name["print_output"].retry_policy is not None
    assert by_name["print_output"].retry_policy.maximum_attempts == 1


def test_per_call_parks_every_tool_but_a_disabled_one() -> None:
    """A worker that asks tool_tier per call (#2171) parks every call, and the parked call
    asks: only a disabled tool goes straight to agent-tools, which refuses it."""
    by_name = _gates()
    tools.apply_overrides(
        by_name, MANIFEST, {"print_output": "read"}, {"render_preview"}, per_call=True
    )
    assert not by_name["render_preview"].needs_approval
    assert by_name["print_output"].needs_approval
    assert by_name["ask_user"].needs_approval
    assert by_name["wait_for_user"].needs_approval


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


def test_settings_still_raise_a_tool_whose_manifest_tier_is_not_a_tier() -> None:
    """Review of #3882: a manifest tier that is not a tier is said once at parse time, but
    kept as the manifest has it. Relabelled `outward` there, it outranked the override, so
    a Settings raise to `outward` was dropped and the tool ran ungated."""
    [odd] = tools.parse_manifest(
        [
            {
                "name": "odd_tool",
                "description": "odd",
                "input_schema": {"type": "object"},
                "tier": "admin",
            }
        ]
    )
    tiers, _ = tools.sanitise_overrides([odd], [AccessRow(S, "odd_tool", "outward", BY)])
    assert tiers == {"odd_tool": "outward"}
    by_name = {t.name: t for t in tools.durable_tools([odd])}
    tools.apply_overrides(by_name, [odd], tiers, set())
    assert by_name["odd_tool"].needs_approval
    assert by_name["odd_tool"].retry_policy is not None
    assert by_name["odd_tool"].retry_policy.maximum_attempts == 1


def test_an_outward_tool_lowered_below_outward_still_runs_at_most_once() -> None:
    """#2171, review I2: whether a retry is safe is the code's to say. An outward tool
    that asks for no approval (as ``confirm_action``) lowered to read or write runs
    without parking and is never retried, both from the turn's overrides and per call."""
    entry = tools.ManifestEntry("confirm_thing", "Confirms.", {"type": "object"}, "outward", None)
    for lowered in ("read", "write"):
        by_name = {t.name: t for t in tools.durable_tools([entry])}
        tools.apply_overrides(by_name, [entry], {"confirm_thing": lowered}, set())
        assert not by_name["confirm_thing"].needs_approval
        assert by_name["confirm_thing"].retry_policy is not None
        assert by_name["confirm_thing"].retry_policy.maximum_attempts == 1, lowered
        tools.gate_call(by_name["confirm_thing"], entry, lowered)
        assert by_name["confirm_thing"].retry_policy.maximum_attempts == 1, lowered


def test_a_manifest_tier_that_is_no_tier_runs_at_most_once() -> None:
    """Review m2: a code tier the worker does not know counts as outward for retries, as a
    flow counts an agent's missing ``declared``: whether a retry is safe is unknown."""
    [odd] = tools.parse_manifest(
        [
            {
                "name": "odd_tool",
                "description": "odd",
                "input_schema": {"type": "object"},
                "tier": "admin",
            }
        ]
    )
    by_name = {t.name: t for t in tools.durable_tools([odd])}
    assert by_name["odd_tool"].retry_policy is not None
    assert by_name["odd_tool"].retry_policy.maximum_attempts == 1
    tools.apply_overrides(by_name, [odd], {"odd_tool": "read"}, set())
    assert by_name["odd_tool"].retry_policy.maximum_attempts == 1
