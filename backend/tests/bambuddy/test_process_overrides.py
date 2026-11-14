"""#2998: any Bambu Studio process setting through one generic ``process_overrides``.

The owner's decision (2026-11-14): no allowlist and no refusal of a key or a value. The
slicer judges each; its refusal comes back as a 422 naming the key."""

from __future__ import annotations

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.part_settings import PartSettings, overrides_of
from scadbuddy.bambuddy.print_run import PrintRunRequest, PrintRunResult
from scadbuddy.bambuddy.process_overrides import (
    named_key,
    normalised_overrides,
    override_warnings,
    scalar_text,
)
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import PrintRunError, run_key
from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.orders.printing import QUOTED_SETUP, setup_differences
from scadbuddy.orders.quote import SETUP_FIELDS, SliceQuoteRequest, print_setup
from scadbuddy.workflows.print_models import PrintRunInput, SourceSpec
from scadbuddy.workflows.problems import SETTING_REFUSED_PROBLEM, setting_refused


def request(**fields: object) -> PrintRunRequest:
    return PrintRunRequest.model_validate(
        {
            "filament_plan": FilamentPlan(slots=[]).model_dump(),
            "choices": PrintChoices(nozzles=[NozzleChoice(size="0.4")]).model_dump(),
            **fields,
        }
    )


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "1"),
        (False, "0"),
        (0, "0"),
        (3, "3"),
        (0.2, "0.2"),
        (5.0, "5"),
        (1e-05, "0.00001"),
        ("15%", "15%"),
        ("tree(auto)", "tree(auto)"),
    ],
)
def test_a_value_is_sent_as_a_bambu_config_stores_it(value: object, text: str) -> None:
    assert scalar_text(value) == text


def test_any_key_is_taken_and_kept_in_the_stored_shape() -> None:
    sent = request(
        process_overrides={
            "skirt_loops": 0,
            "enable_prime_tower": False,
            "outer_wall_speed": [150, 200.5],
            "not_a_bambu_key": "anything",
        }
    )

    assert sent.process_overrides == {
        "enable_prime_tower": "0",
        "not_a_bambu_key": "anything",
        "outer_wall_speed": ["150", "200.5"],
        "skirt_loops": "0",
    }
    assert list(sent.overrides()) == sorted(sent.overrides())


def test_none_set_is_none_sent_and_keys_as_before() -> None:
    """An empty map is no field, so a run's key is the one it had before #2998."""
    assert request(process_overrides={}).process_overrides is None
    assert run_key("o" * 32, request(process_overrides={})) == run_key("o" * 32, request())


def test_the_overrides_are_part_of_the_run_key() -> None:
    plain = run_key("o" * 32, request())
    one = run_key("o" * 32, request(process_overrides={"skirt_loops": 0}))
    same = run_key("o" * 32, request(process_overrides={"skirt_loops": "0"}))
    other = run_key("o" * 32, request(process_overrides={"skirt_loops": 1}))

    assert len({plain, one, other}) == 3
    # The stored shape keys: the number 0 and the string "0" are one setting.
    assert same == one


def test_the_overrides_cross_the_history() -> None:
    arg = PrintRunInput(
        subject="o" * 32,
        slug="demo",
        key="k",
        source=SourceSpec(kind="output", output_id="o" * 32),
        request=request(process_overrides={"skirt_loops": 2, "brim_type": "no_brim"}),
    )
    converter = pydantic_data_converter.payload_converter
    [payload] = converter.to_payloads([arg])
    [back] = converter.from_payloads([payload], [PrintRunInput])

    assert back.request.overrides() == {"brim_type": "no_brim", "skirt_loops": "2"}


def test_the_result_records_them_for_the_print_record() -> None:
    result = PrintRunResult(library_file_id=1, copies=1, process_overrides={"skirt_loops": "0"})

    assert PrintRunResult.model_validate(result.model_dump(mode="json")) == result


def test_normalised_overrides_of_none_is_none() -> None:
    assert normalised_overrides(None) is None
    assert normalised_overrides({}) is None


# ── warnings, never refusals ──────────────────────────────────────────────────────


def test_a_key_the_preset_lacks_is_a_warning() -> None:
    said = override_warnings({"no_such_key": "1", "wall_loops": "3"}, {"wall_loops": "2"})

    assert said is not None
    assert "no_such_key" in said and "wall_loops" not in said


def test_a_list_over_one_value_is_a_warning() -> None:
    said = override_warnings({"wall_loops": ["3", "3"]}, {"wall_loops": "2"})

    assert said is not None and "joined with commas" in said


def test_nothing_is_said_when_the_values_are_unread_or_the_keys_known() -> None:
    assert override_warnings({"x": "1"}, None) is None
    assert override_warnings({"wall_loops": "3"}, {"wall_loops": "2"}) is None
    assert override_warnings(None, {"wall_loops": "2"}) is None


# ── the slicer's refusal names the key ────────────────────────────────────────────


def failed(detail: str) -> PrintRunError:
    return PrintRunError(
        status=502,
        title="Bad Gateway",
        detail=detail,
        extensions={"slice_job_id": 7},
    )


def test_a_failure_naming_an_override_is_a_422_naming_the_key() -> None:
    refused = setting_refused(
        failed("Bambuddy failed to slice the plate: invalid value for skirt_loops: -3"),
        {"skirt_loops": "-3", "wall_loops": "2"},
    )

    assert refused is not None
    assert refused.status == 422
    assert refused.type == SETTING_REFUSED_PROBLEM
    assert refused.extensions == {"slice_job_id": 7, "key": "skirt_loops"}
    assert "skirt_loops" in refused.detail


def test_a_failure_naming_no_override_stays_as_it_was() -> None:
    assert setting_refused(failed("the plate is empty"), {"skirt_loops": "0"}) is None
    assert setting_refused(failed("skirt_loops: bad"), None) is None


def test_a_key_is_named_only_as_a_whole_word() -> None:
    assert named_key("bad wall_loops_extra value", {"wall_loops": "1"}) is None
    # The longest key wins where one key holds another.
    assert named_key("bad top_shell_layers", {"top_shell_layers": "1", "layers": "2"}) == (
        "top_shell_layers"
    )


# ── remembered with the other choices, recorded by a quote ────────────────────────


def test_remembered_choices_keep_them_and_drop_none() -> None:
    kept = ModelPrintChoices.model_validate({"process_overrides": {"skirt_loops": 0}})
    empty = ModelPrintChoices.model_validate({"process_overrides": {}})

    assert kept.model_dump()["process_overrides"] == {"skirt_loops": "0"}
    assert "process_overrides" not in empty.model_dump()
    assert empty == ModelPrintChoices()


def test_a_quote_records_them_in_its_print_setup() -> None:
    quote = SliceQuoteRequest.model_validate(
        {
            "filament_plan": FilamentPlan(slots=[]).model_dump(),
            "choices": PrintChoices(nozzles=[NozzleChoice(size="0.4")]).model_dump(),
            "process_overrides": {"skirt_loops": 0},
        }
    )

    assert "process_overrides" in SETUP_FIELDS and "process_overrides" in QUOTED_SETUP
    assert print_setup(quote)["process_overrides"] == {"skirt_loops": "0"}


def test_a_print_with_other_overrides_than_its_quote_differs() -> None:
    recorded = {"process_overrides": {"skirt_loops": "0"}}

    assert setup_differences({"process_overrides": {"skirt_loops": "1"}}, recorded) == [
        "process_overrides"
    ]
    assert setup_differences({"process_overrides": {"skirt_loops": "0"}}, recorded) == []
    # One not sent is the quote's.
    assert setup_differences({}, recorded) == []


# ── part settings take any key too ────────────────────────────────────────────────


def test_part_settings_take_any_key_and_value() -> None:
    settings = PartSettings.model_validate(
        {
            "top_shell_layers": 5000,
            "sparse_infill_density": 15,
            "sparse_infill_pattern": "a pattern bambu may not know",
            "outer_wall_speed": [100, 120],
            "ironing_flow": 0.15,
            "enable_support": True,
        }
    )

    assert settings.metadata() == (
        ("enable_support", "1"),
        ("ironing_flow", "0.15"),
        ("outer_wall_speed", "100,120"),
        ("sparse_infill_density", "15%"),
        ("sparse_infill_pattern", "a pattern bambu may not know"),
        ("top_shell_layers", "5000"),
    )


def test_an_old_requests_part_settings_key_as_before() -> None:
    """The six named fields stay, so a request from before #2998 dumps as it did."""
    sent = request(part_settings={"1": {"top_shell_layers": 5}})

    assert sent.model_dump(mode="json")["part_settings"] == {
        "1": {
            "top_shell_layers": 5,
            "bottom_shell_layers": None,
            "wall_loops": None,
            "sparse_infill_density": None,
            "sparse_infill_pattern": None,
            "ironing_type": None,
        }
    }
    assert overrides_of(sent.part_settings) == ((1, (("top_shell_layers", "5"),)),)
