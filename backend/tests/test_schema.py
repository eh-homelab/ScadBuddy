from __future__ import annotations

import json
from pathlib import Path

from scadbuddy.api.params import require_valid_preset_params
from scadbuddy.render.runner import format_scad_value
from scadbuddy.render.schema import (
    CustomizerSchema,
    build_schema,
    find_annotations,
    load_cached_schema,
    source_sha256,
    store_cached_schema,
)
from tests.conftest import load_fixture_param, load_fixture_source


def _schema(stem: str) -> CustomizerSchema:
    return build_schema(load_fixture_param(stem), load_fixture_source(stem))


def _types(schema: CustomizerSchema) -> dict[str, str]:
    return {p.name: p.type for p in schema.parameters}


def test_keychain_overlays_colour_and_font() -> None:
    schema = _schema("name_keychain")
    assert _types(schema) == {
        "name": "string",
        "text_colour": "color",
        "text_font": "font",
        "text_size": "slider",
        "text_depth": "slider",
        "base_colour": "color",
        "base_length": "integer",
        "base_width": "integer",
        "base_thickness": "integer",
    }
    assert schema.groups == ["Text", "Base"]
    assert schema.title == "name_keychain"


def test_keychain_parameter_details() -> None:
    schema = _schema("name_keychain")
    by_name = {p.name: p for p in schema.parameters}
    assert by_name["name"].max_length == 20
    assert by_name["name"].caption == "Text on the tag"
    assert by_name["text_size"].min == 4.0
    assert by_name["text_size"].max == 20.0
    assert by_name["text_size"].step == 0.5
    assert by_name["base_length"].initial == 60
    assert isinstance(by_name["base_length"].initial, int)
    assert by_name["text_colour"].initial == "#1f6feb"


def test_widgets_covers_every_type() -> None:
    schema = _schema("widgets")
    assert _types(schema) == {
        "quality": "slider",
        "copies": "integer",
        "wall": "number",
        "label": "string",
        "family": "select",
        "grid": "select",
        "lid": "boolean",
        "ramp": "slider",
        "body_colour": "color",
        "label_font": "font",
    }


def test_only_sliders_carry_a_step() -> None:
    # OpenSCAD 2026.09.23 started writing `step: 1` on every un-ranged number
    # (2026.01.19 omitted it). That is the customizer's default, not a fact about
    # the parameter, and NumberWidget would turn it into an HTML `step=1` on a
    # value like 1.2 — so it must not survive into the schema.
    schema = _schema("widgets")
    by_name = {p.name: p for p in schema.parameters}
    assert by_name["wall"].type == "number"
    assert by_name["wall"].step is None
    assert by_name["copies"].step is None
    assert by_name["quality"].step == 16.0


def test_implicit_step_does_not_change_type_detection() -> None:
    raw = {
        "title": "t",
        "parameters": [
            {"name": "whole", "type": "number", "initial": 3.0, "step": 1.0, "group": "G"},
            {"name": "frac", "type": "number", "initial": 1.2, "step": 1.0, "group": "G"},
            {"name": "bare", "type": "number", "initial": 3.0, "group": "G"},
        ],
    }
    by_name = {p.name: p for p in build_schema(raw, "").parameters}
    assert by_name["whole"].type == "integer"
    assert by_name["frac"].type == "number"
    assert by_name["bare"].type == "integer"
    assert all(p.step is None for p in by_name.values())


def test_global_group_is_not_a_tab() -> None:
    schema = _schema("widgets")
    assert schema.groups == ["Shapes", "Appearance"]
    assert next(p for p in schema.parameters if p.name == "quality").group == "Global"


def test_select_options_keep_label_and_value() -> None:
    schema = _schema("widgets")
    by_name = {p.name: p for p in schema.parameters}
    assert [(o.name, o.value) for o in by_name["family"].options] == [
        ("round", "round"),
        ("square", "square"),
        ("hex", "hex"),
    ]
    assert [o.value for o in by_name["grid"].options] == [1.0, 2.0, 4.0, 8.0]


def test_model_without_customizer_comments() -> None:
    schema = _schema("plain")
    assert _types(schema) == {
        "width": "integer",
        "height": "integer",
        "label": "string",
        "solid": "boolean",
    }
    assert schema.groups == ["Parameters"]
    assert all(p.caption is None for p in schema.parameters)


def test_hidden_group_is_excluded() -> None:
    raw = {
        "title": "t",
        "parameters": [
            {"name": "visible", "type": "number", "initial": 1.0, "group": "Shown"},
            {"name": "secret", "type": "number", "initial": 2.0, "group": "Hidden"},
        ],
    }
    schema = build_schema(raw, "")
    assert [p.name for p in schema.parameters] == ["visible"]
    assert schema.groups == ["Shown"]


def test_find_annotations() -> None:
    source = "\n".join(
        [
            "a = 1; // color",
            "b = 2;//font",
            'c = "x";   //  color  ',
            "d = 3; // colorful",
            "e = 4; // not a color",
            "// f = 5; // color",
        ]
    )
    assert {name: a.kind for name, a in find_annotations(source).items()} == {
        "a": "color",
        "b": "font",
        "c": "color",
    }


def test_schema_cache_round_trip(tmp_path: Path) -> None:
    source = load_fixture_source("plain")
    schema = build_schema(load_fixture_param("plain"), source)
    meta = tmp_path / "model.json"
    meta.write_text('{"name": "Plain"}', encoding="utf-8")

    store_cached_schema(meta, schema)
    assert load_cached_schema(meta, source_sha256(source)) == schema
    assert load_cached_schema(meta, source_sha256(source + "\n")) is None
    assert load_cached_schema(tmp_path / "missing.json", schema.source_sha256) is None


def test_colour_defaults_named_in_css_are_served_as_hex() -> None:
    """#187: the frontend has no CSS name table, so the schema resolves the name."""
    raw = {
        "parameters": [
            {"name": "named", "type": "string", "initial": "Red", "group": "G"},
            {"name": "hex", "type": "string", "initial": "#abc", "group": "G"},
            {"name": "odd", "type": "string", "initial": "#12345", "group": "G"},
            {"name": "text", "type": "string", "initial": "red", "group": "G"},
        ],
    }
    source = 'named = "Red"; // color\nhex = "#abc"; // color\nodd = "#12345"; // color\n'
    by_name = {p.name: p.initial for p in build_schema(raw, source).parameters}
    assert by_name == {"named": "#FF0000", "hex": "#abc", "odd": "#12345", "text": "red"}


def test_schema_cache_written_by_an_older_derivation_misses(tmp_path: Path) -> None:
    source = load_fixture_source("plain")
    schema = build_schema(load_fixture_param("plain"), source)
    cache = tmp_path / "schema.json"
    store_cached_schema(cache, schema)
    body = json.loads(cache.read_text(encoding="utf-8"))
    del body["version"]
    cache.write_text(json.dumps(body), encoding="utf-8")

    assert load_cached_schema(cache, source_sha256(source)) is None


def test_schema_cache_is_keyed_on_the_library_path_too(tmp_path: Path) -> None:
    """#93: the same source can derive a different schema against another pin, and
    a pin's checkout directory is named by its commit."""
    source = load_fixture_source("plain")
    schema = build_schema(load_fixture_param("plain"), source)
    cache = tmp_path / "schema.json"
    pinned = (tmp_path / "libraries" / "BOSL2" / "abc123",)

    store_cached_schema(cache, schema, library_path=pinned)

    assert load_cached_schema(cache, source_sha256(source), library_path=pinned) == schema
    assert load_cached_schema(cache, source_sha256(source)) is None
    repinned = (tmp_path / "libraries" / "BOSL2" / "def456",)
    assert load_cached_schema(cache, source_sha256(source), library_path=repinned) is None


# ── #204: `// file` parameters ────────────────────────────────────────────────

# `openscad -o x.param` on 2026.09.23 for the source below: every one of these is a
# plain string to OpenSCAD, caption kept, and the trailing `// file:svg,png` breaks
# nothing.
FILE_SOURCE = """\
/* [Overlay] */
// Picture to overlay
overlay_file = ""; // file:svg,png
any_file = ""; // file
mask = "star.png"; // file: png
model_file = ""; // file:stl
note = ""; // files
depth = 2; // file:svg
"""


def _file_param_json() -> dict[str, object]:
    string = {"type": "string", "group": "Overlay"}
    return {
        "parameters": [
            {**string, "name": "overlay_file", "initial": "", "caption": "Picture to overlay"},
            {**string, "name": "any_file", "initial": ""},
            {**string, "name": "mask", "initial": "star.png"},
            {**string, "name": "model_file", "initial": ""},
            {**string, "name": "note", "initial": ""},
            {"name": "depth", "type": "number", "initial": 2.0, "group": "Overlay"},
        ]
    }


def test_a_file_annotation_types_a_string_parameter_as_file() -> None:
    schema = build_schema(_file_param_json(), FILE_SOURCE)
    by_name = {p.name: p for p in schema.parameters}

    assert _types(schema) == {
        "overlay_file": "file",
        "any_file": "file",
        "mask": "file",
        # Names only a kind ScadBuddy cannot store: left a text field.
        "model_file": "string",
        # `// files` is not the annotation.
        "note": "string",
        # Only a string can be a file.
        "depth": "integer",
    }
    assert by_name["overlay_file"].accept == ["svg", "png"]
    assert by_name["overlay_file"].caption == "Picture to overlay"
    assert by_name["any_file"].accept == ["svg", "png"]
    assert by_name["mask"].accept == ["png"]
    assert by_name["mask"].initial == "star.png"
    assert by_name["model_file"].accept == []
    assert by_name["depth"].accept == []


def test_find_annotations_reads_the_accepted_kinds() -> None:
    annotations = find_annotations('a = ""; // file:SVG, .png\nb = ""; // file : svg\n')
    assert annotations["a"].kind == "file"
    assert annotations["a"].accept == ("svg", "png")
    assert annotations["b"].accept == ("svg",)


def test_a_cache_entry_from_before_file_parameters_is_rederived(tmp_path: Path) -> None:
    """An entry written before #204 typed `// file` as a string for the same source."""
    schema = build_schema(_file_param_json(), FILE_SOURCE)
    cache = tmp_path / "schema.json"
    store_cached_schema(cache, schema)
    assert load_cached_schema(cache, source_sha256(FILE_SOURCE)) == schema

    body = json.loads(cache.read_text(encoding="utf-8"))
    del body["format"]
    cache.write_text(json.dumps(body), encoding="utf-8")
    assert load_cached_schema(cache, source_sha256(FILE_SOURCE)) is None


def test_a_retired_value_is_kept_on_its_select_only() -> None:
    """`// retired name = value` (#432): accepted by a render, never offered."""
    source = (
        'kind = "auto"; // [auto, png_threshold]\n'
        "count = 2; // [1, 2]\n"
        'label = "x";\n'
        '// retired kind = "image_threshold"\n'
        "// retired count = 3\n"
        '// retired label = "y"\n'
    )
    param_json = {
        "parameters": [
            {
                "name": "kind",
                "type": "string",
                "initial": "auto",
                "options": [
                    {"name": "auto", "value": "auto"},
                    {"name": "png_threshold", "value": "png_threshold"},
                ],
            },
            {
                "name": "count",
                "type": "number",
                "initial": 2,
                "options": [{"name": "1", "value": 1}, {"name": "2", "value": 2}],
            },
            {"name": "label", "type": "string", "initial": "x"},
        ]
    }
    by_name = {p.name: p for p in build_schema(param_json, source).parameters}
    assert by_name["kind"].retired == ["image_threshold"]
    assert by_name["count"].retired == [3]
    assert by_name["label"].retired == []
    assert [o.value for o in by_name["kind"].options] == ["auto", "png_threshold"]


#: The battery crate's `cell = "AA"; // [AAA, AA, C, D, 9V, 18650, CR2032]` (#356):
#: OpenSCAD's export types the bare 18650 as a number, the rest as text.
MIXED_PARAM = {
    "parameters": [
        {
            "name": "cell",
            "type": "string",
            "initial": "AA",
            "options": [
                {"name": "AAA", "value": "AAA"},
                {"name": "AA", "value": "AA"},
                {"name": "9V", "value": "9V"},
                {"name": "18650", "value": 18650.0},
                {"name": "Half", "value": 1.5},
            ],
        },
    ]
}
MIXED_SOURCE = 'cell = "AA"; // [AAA, AA, 9V, 18650, 1.5:Half]\n// retired cell = 14500\n'


def test_a_select_mixing_text_and_numbers_serves_every_option_as_text() -> None:
    """#356: the template compares `cell` against strings, so a number option is
    offered as the text OpenSCAD's comment wrote, never as `18650.0`."""
    (cell,) = build_schema(MIXED_PARAM, MIXED_SOURCE).parameters
    assert cell.type == "select"
    assert [(o.name, o.value) for o in cell.options] == [
        ("AAA", "AAA"),
        ("AA", "AA"),
        ("9V", "9V"),
        ("18650", "18650"),
        ("Half", "1.5"),
    ]
    assert cell.retired == ["14500"]


def test_a_mixed_select_number_option_renders_and_saves_as_a_preset() -> None:
    """#356: the text value round-trips through a render and a preset."""
    schema = build_schema(MIXED_PARAM, MIXED_SOURCE)
    (cell,) = schema.parameters
    assert format_scad_value(cell, "18650") == '"18650"'
    assert format_scad_value(cell, "AA") == '"AA"'
    require_valid_preset_params(schema, {"cell": "18650"})
    require_valid_preset_params(schema, {"cell": "14500"})


def test_an_all_number_select_keeps_its_numbers() -> None:
    raw = {
        "parameters": [
            {
                "name": "grid",
                "type": "number",
                "initial": 2,
                "options": [{"name": "1", "value": 1.0}, {"name": "2", "value": 2.0}],
            }
        ]
    }
    (grid,) = build_schema(raw, "grid = 2; // [1, 2]\n").parameters
    assert [o.value for o in grid.options] == [1.0, 2.0]
    assert format_scad_value(grid, 2) == "2.0"
