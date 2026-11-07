"""#5626: a template's slicer settings per colour part, ``part_print_settings`` in its
model.json, named by colour parameter and read as one output's slots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scadbuddy.bambuddy.part_settings import PartSettings, by_colour, laid_under
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError, meta_from_raw
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore


def _catalogue(tmp_path: Path, meta: dict[str, object]) -> Catalogue:
    paths = DataPaths(tmp_path)
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube(1);\n", encoding="utf-8")
    paths.model_meta("demo").write_text(json.dumps(meta), encoding="utf-8")
    return Catalogue(paths, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def test_settings_are_read_by_colour_parameter() -> None:
    meta = meta_from_raw(
        {"part_print_settings": {"letter_color": {"top_shell_layers": 8, "ironing_type": "top"}}},
        "demo",
    )

    assert meta.part_print_settings == {
        "letter_color": {"top_shell_layers": 8, "ironing_type": "top"}
    }
    # A plain dump is the model.json shape: no serializer (an operation's stored result
    # is sent as it is, tests/api/test_operations.py).
    assert meta.model_dump()["part_print_settings"] == meta.part_print_settings


def test_none_is_empty_and_a_parameter_that_sets_nothing_is_dropped() -> None:
    assert meta_from_raw({}, "demo").part_print_settings == {}
    assert meta_from_raw({"part_print_settings": None}, "demo").part_print_settings == {}
    assert meta_from_raw({"part_print_settings": {"a": {}}}, "demo").part_print_settings == {}


@pytest.mark.parametrize(
    "settings",
    [
        {"top_shell_layers": 1001},
        {"wall_loops": -1},
        {"sparse_infill_density": 101},
        {"sparse_infill_pattern": "spiral"},
        {"ironing_type": "all"},
        {"layer_height": 0.2},
    ],
)
def test_a_key_or_value_out_of_range_is_refused(settings: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        meta_from_raw({"part_print_settings": {"letter_color": settings}}, "demo")


@pytest.mark.parametrize("name", ["2", "letter-color", "", "a b"])
def test_a_part_is_named_by_an_identifier_not_a_slot(name: str) -> None:
    with pytest.raises(ValidationError):
        meta_from_raw({"part_print_settings": {name: {"wall_loops": 3}}}, "demo")


def test_loading_a_template_with_a_bad_value_says_which(tmp_path: Path) -> None:
    catalogue = _catalogue(
        tmp_path, {"name": "Demo", "part_print_settings": {"base_color": {"wall_loops": 2000}}}
    )

    with pytest.raises(InvalidModelMetaError, match=r"part_print_settings.*base_color\.wall_loops"):
        catalogue.part_print_settings("demo")
    with pytest.raises(InvalidModelMetaError, match="part_print_settings"):
        catalogue.print_settings("demo")


def test_the_catalogue_answers_them_and_the_record_states_only_what_is_set(
    tmp_path: Path,
) -> None:
    catalogue = _catalogue(
        tmp_path, {"name": "Demo", "part_print_settings": {"base_color": {"wall_loops": 3}}}
    )

    assert catalogue.part_print_settings("demo") == {"base_color": PartSettings(wall_loops=3)}
    record = catalogue.record("demo").model_dump(mode="json")
    assert record["part_print_settings"] == {"base_color": {"wall_loops": 3}}


def test_a_template_that_is_gone_has_none(tmp_path: Path) -> None:
    assert _catalogue(tmp_path, {"name": "Demo"}).part_print_settings("gone") == {}


def test_each_parameter_is_the_slot_of_its_colour_in_the_output() -> None:
    declared = {"letter_color": PartSettings(top_shell_layers=8)}

    # A CSS name and its hex are one colour, as OpenSCAD renders them.
    assert by_colour(declared, {"letter_color": "purple"}, ["#FFFFFF", "#800080"]) == {
        2: PartSettings(top_shell_layers=8)
    }
    # The letters' colour moved to slot 1: the settings follow it.
    assert by_colour(declared, {"letter_color": "#800080"}, ["#800080", "#FFFFFF"]) == {
        1: PartSettings(top_shell_layers=8)
    }


def test_a_parameter_with_no_value_or_no_slot_sets_nothing() -> None:
    declared = {"letter_color": PartSettings(top_shell_layers=8)}

    assert by_colour(declared, {}, ["#FFFFFF"]) is None
    assert by_colour(declared, {"letter_color": "#123456"}, ["#FFFFFF"]) is None
    assert by_colour(declared, {"letter_color": 3}, ["#FFFFFF"]) is None


def test_two_parameters_of_one_colour_share_its_slot_and_the_first_wins() -> None:
    declared = {
        "letter_color": PartSettings(top_shell_layers=8),
        "border_color": PartSettings(top_shell_layers=4, wall_loops=3),
    }
    params = {"letter_color": "#800080", "border_color": "#800080"}

    assert by_colour(declared, params, ["#FFFFFF", "#800080"]) == {
        2: PartSettings(top_shell_layers=8, wall_loops=3)
    }


def test_the_request_wins_per_key_over_the_template() -> None:
    template = {2: PartSettings(top_shell_layers=8, wall_loops=3)}
    request = {2: PartSettings(top_shell_layers=4), 1: PartSettings(ironing_type="top")}

    assert laid_under(template, request) == {
        1: PartSettings(ironing_type="top"),
        2: PartSettings(top_shell_layers=4, wall_loops=3),
    }
    assert laid_under(template, None) == template
    assert laid_under(None, None) is None
