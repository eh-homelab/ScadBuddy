from __future__ import annotations

from pathlib import Path

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
    assert find_annotations(source) == {"a": "color", "b": "font", "c": "color"}


def test_schema_cache_round_trip(tmp_path: Path) -> None:
    source = load_fixture_source("plain")
    schema = build_schema(load_fixture_param("plain"), source)
    meta = tmp_path / "model.json"
    meta.write_text('{"name": "Plain"}', encoding="utf-8")

    store_cached_schema(meta, schema)
    assert load_cached_schema(meta, source_sha256(source)) == schema
    assert load_cached_schema(meta, source_sha256(source + "\n")) is None
    assert load_cached_schema(tmp_path / "missing.json", schema.source_sha256) is None
