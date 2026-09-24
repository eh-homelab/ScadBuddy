"""Issue #88 — the print-option overlay and its scope resolver."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.models import QueueItemCreate
from scadbuddy.bambuddy.options import BAMBUDDY_DEFAULTS, PrintOptions, resolve
from tests.bambuddy.conftest import recording


def test_every_option_is_a_field_of_bambuddys_own_queue_item() -> None:
    """The whole point of #88: no ScadBuddy-invented option names."""
    assert set(PrintOptions.model_fields) <= set(QueueItemCreate.model_fields)


def test_the_recorded_defaults_match_bambuddys_own_queue_item_defaults() -> None:
    """``BAMBUDDY_DEFAULTS`` is only ever used to mark a value as non-default, so it
    drifting away from the model it describes would mislead the UI silently."""
    item = QueueItemCreate()
    for name, value in BAMBUDDY_DEFAULTS.queue_fields().items():
        assert getattr(item, name) == value, name
    # And the two it deliberately omits really are null on Bambuddy's side.
    assert item.project_id is None
    assert item.preheat_chamber_target_override is None


def test_an_unset_option_is_not_sent() -> None:
    assert PrintOptions(timelapse=False).queue_fields() == {"timelapse": False}
    assert PrintOptions().is_empty()
    # False is a value, not an absence — the bug this guards against is `if value:`.
    assert not PrintOptions(timelapse=False).is_empty()


def test_a_misspelled_option_is_refused_rather_than_remembered() -> None:
    with pytest.raises(ValidationError):
        PrintOptions.model_validate({"time_lapse": False})


@pytest.mark.parametrize(
    ("field", "value"),
    [("quantity", 0), ("quantity", 1001), ("preheat_chamber_target_override", 66)],
)
def test_the_bounds_are_enforced(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        PrintOptions.model_validate({field: value})


def test_the_measured_bound_comes_from_bambuddys_own_schema() -> None:
    """``preheat_chamber_target_override``'s 0-65 is Bambuddy's, not invented here.

    Read off the recorded ``PrintQueueItemCreate`` rather than written down, so a Bambuddy
    that widens it reds this test instead of ScadBuddy quietly 422ing a value the user's
    own instance would have taken. ``quantity`` deliberately has no counterpart here:
    Bambuddy declares it unbounded, and 1-1000 is ScadBuddy's own bound mirroring
    ``SendRequest.copies``.
    """
    schema = recording("openapi/scadbuddy-routes.json")
    field = schema["components"]["schemas"]["PrintQueueItemCreate"]["properties"][
        "preheat_chamber_target_override"
    ]
    bounded = next(branch for branch in field["anyOf"] if branch.get("type") == "integer")
    mirrored = PrintOptions.model_fields["preheat_chamber_target_override"].metadata

    assert bounded["minimum"] == 0
    assert bounded["maximum"] == 65
    assert [str(constraint) for constraint in mirrored] == ["Ge(ge=0)", "Le(le=65)"]
    assert (
        "minimum"
        not in schema["components"]["schemas"]["PrintQueueItemCreate"]["properties"]["quantity"]
    )


def test_later_scopes_win_field_by_field() -> None:
    resolved = resolve(
        PrintOptions(timelapse=True, layer_inspect=True),
        PrintOptions(timelapse=False),
        PrintOptions(quantity=2),
    )

    assert resolved.timelapse is False
    assert resolved.layer_inspect is True  # not cleared by the printer scope
    assert resolved.quantity == 2


def test_an_absent_scope_is_skipped() -> None:
    assert resolve(None, PrintOptions(use_ams=False), None).use_ams is False


def test_resolving_nothing_leaves_every_option_to_bambuddy() -> None:
    assert resolve(None, None).is_empty()


def test_quantity_alone_can_ride_a_pipeline_run_but_nothing_else_can() -> None:
    assert PrintOptions(quantity=3).beyond_pipeline() == ()
    assert PrintOptions(quantity=3, timelapse=False).beyond_pipeline() == ("timelapse",)
    assert PrintOptions().beyond_pipeline() == ()
