"""A run grant's rules and bounds (#2481, spec 2026-10-11-flow-grants-and-git-design.md
§3.1, plan Task A1): what `admits` lets through and what `validate_spec` refuses."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.flows.grants import (
    MAX_EXPIRY,
    CallFacts,
    GrantRule,
    GrantSpec,
    RuleUse,
    admits,
    validate_spec,
)

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
TIERS = {"send_to_bambuddy": "outward", "get_model": "read", "save_preset": "write"}


def rule(**fields: Any) -> GrantRule:
    return GrantRule.model_validate({"fn": "queue_print", **fields})


def facts(**fields: Any) -> CallFacts:
    base: dict[str, Any] = {
        "fn": "queue_print",
        "args": {},
        "printer_id": 3,
        "models": ["coaster"],
        "prints": 2,
        "filament_g": 40.0,
    }
    return CallFacts.model_validate({**base, **fields})


def spec(*rules: dict[str, Any], expires_in: timedelta = timedelta(hours=24)) -> GrantSpec:
    return GrantSpec.model_validate({"expires_at": NOW + expires_in, "rules": list(rules)})


def test_an_unbounded_rule_admits_any_call_of_its_function() -> None:
    assert admits(rule(), facts(), RuleUse()) is None
    assert admits(rule(), facts(fn="arrange"), RuleUse()) is not None


def test_printers() -> None:
    assert admits(rule(printers=[3, 4]), facts(), RuleUse()) is None
    assert admits(rule(printers=[4]), facts(), RuleUse()) is not None
    # The configured printer could not be resolved: nothing to match.
    assert admits(rule(printers=[3]), facts(printer_id=None), RuleUse()) is not None


def test_models_match_every_slug() -> None:
    assert admits(rule(models=["coaster"]), facts(), RuleUse()) is None
    assert admits(rule(models=["coaster"]), facts(models=["coaster", "lid"]), RuleUse()) is not None
    arrange = GrantRule(fn="arrange", models=["coaster", "lid"])
    assert admits(arrange, facts(fn="arrange", models=["lid", "coaster"]), RuleUse()) is None


def test_a_library_file_matches_only_through_library_files() -> None:
    file_call = facts(models=[], library_file=12)
    assert admits(rule(library_files=[12]), file_call, RuleUse()) is None
    assert admits(rule(library_files=[13]), file_call, RuleUse()) is not None
    assert admits(rule(models=["coaster"]), file_call, RuleUse()) is not None
    # An output, not a library file, is never admitted by a library_files bound.
    assert admits(rule(library_files=[12]), facts(), RuleUse()) is not None


def test_max_prints_counts_what_was_used() -> None:
    assert admits(rule(max_prints=4), facts(prints=2), RuleUse(prints=2)) is None
    assert admits(rule(max_prints=4), facts(prints=2), RuleUse(prints=3)) is not None
    # An all-plates print of a library file is not counted: no print bound matches it.
    assert admits(rule(max_prints=4), facts(prints=None), RuleUse()) == "prints unknown"
    assert admits(rule(), facts(prints=None), RuleUse()) is None


def test_max_filament_refuses_unknown_grams() -> None:
    bounded = rule(max_filament_g=100)
    assert admits(bounded, facts(filament_g=40), RuleUse(filament_g=60)) is None
    assert admits(bounded, facts(filament_g=40), RuleUse(filament_g=61)) is not None
    assert admits(bounded, facts(filament_g=None), RuleUse()) == "filament unknown"
    # Without the bound, unknown grams are fine.
    assert admits(rule(), facts(filament_g=None), RuleUse()) is None


def test_max_calls() -> None:
    assert admits(rule(max_calls=2), facts(), RuleUse(calls=1)) is None
    assert admits(rule(max_calls=2), facts(), RuleUse(calls=2)) is not None


def test_a_tool_rule_names_its_tool_and_bounds_its_arguments() -> None:
    send = GrantRule.model_validate(
        {
            "fn": "tool",
            "tool": "send_to_bambuddy",
            "args": [{"path": "/printer_id", "in": [3, 4]}, {"path": "/opts/0", "in": ["a"]}],
        }
    )
    call = CallFacts(fn="tool", tool="send_to_bambuddy", args={"printer_id": 4, "opts": ["a", "b"]})
    assert admits(send, call, RuleUse()) is None
    assert admits(send, call.model_copy(update={"tool": "delete_model"}), RuleUse()) is not None
    wrong = call.model_copy(update={"args": {"printer_id": 5, "opts": ["a"]}})
    assert admits(send, wrong, RuleUse()) is not None
    missing = call.model_copy(update={"args": {"opts": ["a"]}})
    assert admits(send, missing, RuleUse()) is not None
    # JSON's types, not Python's: true is not 1.
    as_bool = call.model_copy(update={"args": {"printer_id": True, "opts": ["a"]}})
    one = GrantRule.model_validate(
        {"fn": "tool", "tool": "send_to_bambuddy", "args": [{"path": "/printer_id", "in": [1]}]}
    )
    assert admits(one, as_bool, RuleUse()) is not None


def test_a_valid_spec() -> None:
    good = spec(
        {"fn": "queue_print", "printers": [3], "max_prints": 12},
        {"fn": "arrange"},
        {"fn": "tool", "tool": "send_to_bambuddy", "args": [{"path": "/printer_id", "in": [3]}]},
    )
    assert validate_spec(good, tool_tiers=TIERS, now=NOW) == []


def test_validate_spec_refuses() -> None:
    def problems(*rules: dict[str, Any], expires_in: timedelta = timedelta(hours=1)) -> list[str]:
        return validate_spec(spec(*rules, expires_in=expires_in), tool_tiers=TIERS, now=NOW)

    assert problems({"fn": "tool"})  # names no tool
    assert problems({"fn": "tool", "tool": "get_model"})  # a read
    assert problems({"fn": "tool", "tool": "save_preset"})  # a write
    assert problems({"fn": "tool", "tool": "no_such_tool"})
    assert problems({"fn": "queue_print", "tool": "send_to_bambuddy"})
    assert problems({"fn": "arrange", "printers": [3]})  # printers bound only prints
    assert problems({"fn": "tool", "tool": "send_to_bambuddy", "models": ["x"]})
    assert problems({"fn": "queue_print", "args": [{"path": "/x", "in": [1]}]})
    assert problems(
        {"fn": "tool", "tool": "send_to_bambuddy", "args": [{"path": "no-slash", "in": [1]}]}
    )
    assert problems({"fn": "queue_print"}, expires_in=MAX_EXPIRY + timedelta(seconds=1))
    assert problems({"fn": "queue_print"}, expires_in=-timedelta(seconds=1))
    assert (
        validate_spec(spec({"fn": "queue_print"}, expires_in=MAX_EXPIRY), tool_tiers=TIERS, now=NOW)
        == []
    )


def test_the_model_refuses_negative_maxima_and_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        rule(max_prints=-1)
    with pytest.raises(ValidationError):
        rule(max_filament_g=-0.5)
    with pytest.raises(ValidationError):
        rule(printer=[3])
    with pytest.raises(ValidationError):
        GrantSpec.model_validate({"expires_at": NOW, "rules": [{"fn": "queue_print"}] * 33})
