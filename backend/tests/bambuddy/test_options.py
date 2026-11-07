"""Issue #88 — the print-option overlay and its scope resolver."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.models import QueueItemCreate
from scadbuddy.bambuddy.options import (
    BAMBUDDY_DEFAULTS,
    LAN_CALIBRATIONS,
    PrintOptions,
    lan_calibration_refusal,
    resolve,
)
from scadbuddy.printers.lan import project_file_command
from scadbuddy.printers.lan_models import ProjectFile
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
    ``PrintRunRequest.copies``.
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


# ── #5352: a print over the LAN takes the calibration value the LAN start sends ──────


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"flow_cali": "off"},
        {"bed_levelling": "auto"},
        {"nozzle_offset_cali": "auto"},
        {"flow_cali": "off", "bed_levelling": "auto", "nozzle_offset_cali": "auto"},
        # Not calibrations: the LAN start sends these itself.
        {"timelapse": True, "layer_inspect": False},
    ],
)
def test_what_the_lan_does_passes(options: dict[str, object]) -> None:
    assert lan_calibration_refusal(PrintOptions.model_validate(options)) is None


@pytest.mark.parametrize(
    ("name", "value", "named"),
    [
        ("flow_cali", "on", 'flow_cali "on"'),
        ("flow_cali", "auto", 'flow_cali "auto"'),
        ("bed_levelling", "on", 'bed_levelling "on"'),
        ("bed_levelling", "off", 'bed_levelling "off"'),
        ("nozzle_offset_cali", "on", 'nozzle_offset_cali "on"'),
        ("nozzle_offset_cali", "off", 'nozzle_offset_cali "off"'),
        ("vibration_cali", True, "vibration_cali true"),
        ("vibration_cali", False, "vibration_cali false"),
    ],
)
def test_what_the_lan_cannot_honour_is_refused(name: str, value: object, named: str) -> None:
    refusal = lan_calibration_refusal(PrintOptions.model_validate({name: value}))
    assert refusal is not None and refusal.startswith(f"A print over the LAN cannot set {named}:")
    # The control a person has: Settings > Printers' "How <name> prints" select, whose
    # `bambuddy` choice is labelled "Through Bambuddy" (frontend
    # `components/settings/Printers.tsx` `PATHS`); `run_path` takes it as set.
    assert refusal.endswith(
        'in Settings > Printers, set how the printer prints to "Through Bambuddy" (it must'
        " link a Bambuddy printer)."
    )


def test_the_lan_values_are_what_the_lan_start_sends() -> None:
    """`LAN_CALIBRATIONS` reads the start command: ``flow_cali`` false, and no field at
    all for the three left to the printer."""
    command = project_file_command(
        ProjectFile(
            url="ftp://x.3mf",
            param="p",
            subtask_name="s",
            use_ams=True,
            ams_mapping=[0],
            md5="0" * 32,
        ),
        "1",
    )["print"]
    assert command["flow_cali"] is False and LAN_CALIBRATIONS["flow_cali"] == "off"
    for left in ("bed_levelling", "bed_leveling", "nozzle_offset_cali", "vibration_cali"):
        assert left not in command
    assert LAN_CALIBRATIONS["bed_levelling"] == LAN_CALIBRATIONS["nozzle_offset_cali"] == "auto"
    assert LAN_CALIBRATIONS["vibration_cali"] is None
