"""Settings → Plugins overrides as the durable gate reads them (#2088): sanitised as
builtInTools.ts does (TIGHTEN ONLY), and applied to the durable tools each turn."""

from __future__ import annotations

from session_support import MANIFEST
from temporalio.claude_agent_sdk import DurableTool

from scadbuddy_durable.session import tools


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
    for stored in (None, "x", [], {"tool_tiers": [], "disabled_tools": {}}):
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
