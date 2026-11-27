"""Task widgets, `expects` and context blocks (#8062)."""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.core.input_schema import schema_problems
from scadbuddy.tasks.models import BEFORE_WIDGETS, UNCHECKED_DEFAULTS, TaskOpen
from scadbuddy.tasks.schema import prompt_schema_problems
from scadbuddy.tasks.widgets import WIDGETS, context_problems, expects_entries, scan_fields


def _asks(prop: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": {"x": prop}}


ON_TYPE: list[dict[str, Any]] = [
    {"type": "string", "x-sb-widget": "scan"},
    {"type": "string", "x-sb-widget": "photo"},
    {"type": "string", "x-sb-widget": "file"},
    {"type": "string", "x-sb-widget": "entity", "kind": "library_file"},
    {"type": "string", "x-sb-widget": "entity", "kind": "location"},
    {"type": "string", "x-sb-widget": "entity", "kind": "spool"},
    {"type": "number", "x-sb-widget": "weight"},
    {"type": "integer", "x-sb-widget": "weight"},
    {"type": "boolean", "x-sb-widget": "confirm"},
]


def test_the_widgets() -> None:
    assert WIDGETS == ("scan", "photo", "file", "entity", "weight", "confirm")


@pytest.mark.parametrize("prop", ON_TYPE)
def test_widgets_pass_for_prompts(prop: dict[str, Any]) -> None:
    assert prompt_schema_problems(_asks(prop)) == []


def test_unknown_widget_passes_unlogged(caplog: pytest.LogCaptureFixture) -> None:
    """A workflow runs the check, so a log line would repeat on every replay."""
    with caplog.at_level(logging.WARNING):
        assert prompt_schema_problems(_asks({"type": "string", "x-sb-widget": "dial"})) == []
    assert caplog.text == ""


def test_widget_on_wrong_type_is_refused() -> None:
    problems = prompt_schema_problems(_asks({"type": "string", "x-sb-widget": "confirm"}))
    assert problems == ["x: 'x-sb-widget' confirm needs boolean"]


def test_entity_needs_a_known_kind() -> None:
    assert prompt_schema_problems(_asks({"type": "string", "x-sb-widget": "entity"}))
    bad = {"type": "string", "x-sb-widget": "entity", "kind": "order"}
    assert prompt_schema_problems(_asks(bad))
    assert prompt_schema_problems(_asks({"type": "string", "kind": "spool"}))


def test_other_x_sb_key_is_refused() -> None:
    problems = prompt_schema_problems(_asks({"type": "string", "x-sb-colour": "red"}))
    assert problems == ["x: 'x-sb-colour' is not supported for string"]


def _scan(**more: Any) -> dict[str, Any]:
    return _asks({"type": "string", "x-sb-widget": "scan", **more})


def test_expects_rules() -> None:
    plain = {"type": "string", "expects": {"kinds": ["spool"]}}
    assert prompt_schema_problems(_asks(plain))
    assert any("names no kind or code" in p for p in prompt_schema_problems(_scan(expects={})))
    assert prompt_schema_problems(_scan(expects={"kinds": ["order"]}))
    assert prompt_schema_problems(_scan(expects={"kinds": ["location", "spool"]})) == []
    assert prompt_schema_problems(_scan(expects={"codes": ["L-12"]})) == []
    assert prompt_schema_problems(_scan(expects={"codes": ["l-12", "S-3"]})) == []
    assert prompt_schema_problems(_scan(expects={"codes": ["X-1"]}))
    assert prompt_schema_problems(_scan(expects={"codes": [f"L-{n}" for n in range(1, 21)]})) == []
    assert prompt_schema_problems(_scan(expects={"codes": [f"L-{n}" for n in range(1, 22)]}))


def test_expects_refusal_never_quotes_a_value() -> None:
    problems = prompt_schema_problems(_scan(expects={"codes": ["ZZ-secret"]}))
    assert problems
    assert "secret" not in " ".join(problems)


def test_expects_entries() -> None:
    schema = _scan(expects={"kinds": ["location"], "codes": ["l-12"]})
    assert expects_entries(schema) == ["kind:location", "code:L-12"]
    assert scan_fields(schema) == {"x": {"kinds": ("location",), "codes": ("L-12",)}}
    assert scan_fields(_scan()) == {"x": {"kinds": (), "codes": ()}}
    assert expects_entries(_scan()) == []


def test_flow_inputs_still_refuse_widgets() -> None:
    for prop in (ON_TYPE[0], {"type": "string", "expects": {"kinds": ["spool"]}}):
        assert schema_problems(_asks(prop), formats=True)
        assert schema_problems(_asks(prop))


BLOCKS: list[dict[str, Any]] = [
    {"entity": "spool:abc"},
    {"text": "hello"},
    {"image": {"entity": "order:1", "attachment_id": "a1"}},
    {"table": {"columns": ["a", "b"], "rows": [["x", 1], [None, 2.5]]}},
    {"checklist": ["one", "two"]},
    {"expect": {"weight_g": 12.5, "tolerance_g": 0}},
]


def test_context_blocks() -> None:
    for block in BLOCKS:
        assert context_problems([block]) == []
    assert context_problems([{"text": 3}])
    assert context_problems([{"entity": "nokind:1"}])
    assert context_problems([{"expect": {"weight_g": 0, "tolerance_g": 0}}])
    assert context_problems([{"expect": {"weight_g": 1, "tolerance_g": -1}}])
    assert context_problems([{"checklist": ["x"] * 31}])
    assert context_problems([{"table": {"columns": ["a"], "rows": [["x"]] * 51}}])
    assert context_problems([{"table": {"columns": ["a"], "rows": [["x", "y"]]}}])
    assert context_problems([{"text": "x" * 4001}])
    assert context_problems([{"image": {"entity": "order:1"}}])


def test_other_context_shapes_are_stored() -> None:
    assert context_problems([{"note": "free"}, {"text": "a", "extra": 1}, {}]) == []


def _open(context: list[dict[str, Any]]) -> TaskOpen:
    return TaskOpen.model_validate(
        {
            "id": "t1",
            "run_id": "r1",
            "title": "T",
            "schema": {"type": "object", "properties": {}},
            "context": context,
        }
    )


def test_task_open_checks_the_context() -> None:
    assert _open([{"text": "ok"}, {"note": "free"}]).context
    with pytest.raises(ValidationError) as err:
        _open([{"text": 3}])
    assert "context[0] text" in str(err.value)


def _opened(rules: dict[str, Any], **fields: Any) -> TaskOpen:
    return TaskOpen.model_validate(
        {"id": "t", "run_id": "r", "title": "q", **fields}, context=rules
    )


def test_before_widgets_refuses_a_widget_schema() -> None:
    schema = _asks({"type": "string", "x-sb-widget": "scan", "expects": {"kinds": ["spool"]}})
    assert _opened(UNCHECKED_DEFAULTS, schema=schema).schema_ == schema
    with pytest.raises(ValidationError, match="'x-sb-widget' is not supported"):
        _opened(BEFORE_WIDGETS, schema=schema)


def test_before_widgets_keeps_a_context_item_as_it_is() -> None:
    context = [{"checklist": "not a list"}, {"entity": "SB-1042"}]
    with pytest.raises(ValidationError, match="context"):
        _opened(UNCHECKED_DEFAULTS, schema=_asks({"type": "string"}), context=context)
    assert (
        _opened(BEFORE_WIDGETS, schema=_asks({"type": "string"}), context=context).context
        == context
    )


@pytest.mark.parametrize(
    ("place", "stored"),
    [
        ("L-7", "L-7"),
        ("l7", "L-7"),
        ("L-07", "L-7"),
        (" l-7 ", "L-7"),
        ("S-3", "S-3"),
        ("Bench", "Bench"),
    ],
)
def test_a_place_that_is_a_location_code_is_stored_canonical(place: str, stored: str) -> None:
    assert (
        _opened(UNCHECKED_DEFAULTS, schema=_asks({"type": "string"}), place=place).place == stored
    )
