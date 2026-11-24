import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import regex
from pydantic import ValidationError

from scadbuddy.core.input_schema import patterns_deadline, schema_problems
from scadbuddy.tasks.models import AnswerProblem, Task, TaskOpen
from scadbuddy.tasks.validate import prompt_schema_problems, validate_answer

EMAIL = {
    "type": "object",
    "required": ["email"],
    "properties": {
        "email": {"type": "string", "format": "email", "title": "Email"},
        "also_sms": {"type": "boolean", "default": False},
    },
}


BROKEN = [AnswerProblem(field="", message="this question's form is broken")]


def _asks(prop: Any, **others: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"x": prop, **others}}


def _nested(depth: int) -> Any:
    value: Any = []
    for _ in range(depth - 1):
        value = [value]
    return value


def test_a_valid_answer_has_no_problems() -> None:
    assert validate_answer(EMAIL, {"email": "jane@example.com"}) == []


def test_a_bad_email_names_its_field() -> None:
    [p] = validate_answer(EMAIL, {"email": "not-an-address"})
    assert p.field == "email" and "email" in p.message


def test_a_missing_required_field_is_a_root_problem_naming_it() -> None:
    [p] = validate_answer(EMAIL, {})
    assert p.field == "" and "email" in p.message


def test_a_nested_or_referencing_schema_is_refused_for_a_prompt() -> None:
    assert prompt_schema_problems({"type": "object", "properties": {"x": {"$ref": "#/a"}}})


def test_a_problem_message_never_holds_the_rejected_value() -> None:
    bad = "secret-not-an-address"
    choice = _asks({"type": "string", "enum": ["a", "b"]})
    short = _asks({"type": "string", "maxLength": 3})
    found = [
        validate_answer(EMAIL, {"email": bad}),
        validate_answer(EMAIL, {"email": 12345678}),
        validate_answer(choice, {"x": bad}),
        validate_answer(short, {"x": bad}),
        validate_answer(EMAIL, {"email": bad + "\x00"}),
    ]
    for problems in found:
        assert problems
        assert all(bad not in p.message and "12345678" not in p.message for p in problems)


def test_the_field_is_a_slash_joined_path() -> None:
    schema = _asks({"type": "array", "items": {"type": "integer"}})
    [p] = validate_answer(schema, {"x": [1, "x"]})
    assert p.field == "x/1"


def test_the_email_schema_is_a_prompt_schema() -> None:
    assert prompt_schema_problems(EMAIL) == []


def test_uri_is_refused_for_a_prompt_naming_it() -> None:
    schema = {"type": "object", "properties": {"u": {"type": "string", "format": "uri"}}}
    [problem] = prompt_schema_problems(schema)
    assert "uri" in problem and "email" in problem and "date-time" in problem


def test_a_bad_email_default_is_refused() -> None:
    prop = {"type": "string", "format": "email", "default": "nope"}
    problems = prompt_schema_problems({"type": "object", "properties": {"e": prop}})
    assert problems and "default" in problems[0]


def test_a_format_on_an_array_item_is_allowed_for_a_prompt() -> None:
    items = {"type": "string", "format": "date"}
    schema = {"type": "object", "properties": {"d": {"type": "array", "items": items}}}
    assert prompt_schema_problems(schema) == []


def test_format_is_still_refused_without_the_flag() -> None:
    assert any("format" in p for p in schema_problems(EMAIL))


def test_a_slow_pattern_is_refused_within_seconds() -> None:
    prop = {"type": "string", "pattern": "^(a|aa)+$", "maxLength": 1024}
    schema = {"type": "object", "properties": {"s": prop}}
    started = time.monotonic()
    problems = validate_answer(schema, {"s": "a" * 1000 + "b"})
    assert problems and time.monotonic() - started < 5


@pytest.mark.parametrize("answer", [["x"], "text", None, 3, {"email": ["x"]}, {"email": None}])
def test_a_wrong_shaped_answer_is_a_problem_not_an_exception(answer: Any) -> None:
    assert validate_answer(EMAIL, answer)


def test_an_extra_key_is_refused_even_without_additional_properties() -> None:
    secret = "secret-key-name"
    problems = validate_answer(EMAIL, {"email": "jane@example.com", secret: 1})
    assert problems
    assert all(secret not in p.field and secret not in p.message for p in problems)


def test_a_ref_schema_is_refused_when_a_task_opens() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "http://example.com/x"}}}
    with pytest.raises(ValidationError, match="type must be one of"):
        TaskOpen(id="t", run_id="r", title="x", schema=schema)


def test_a_stored_task_reads_without_the_subset_check() -> None:
    now = datetime.now(UTC)
    stored = {"type": "object", "properties": {"x": {"$ref": "#/a"}}}
    task = Task(
        id="t",
        run_id="r",
        title="x",
        schema=stored,
        project_id=None,
        state="open",
        answer=None,
        answered_by=None,
        created_at=now,
        updated_at=now,
    )
    assert task.schema_ == stored


def test_a_remote_ref_is_never_fetched() -> None:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        schema = {"$ref": f"http://127.0.0.1:{server.server_port}/x"}
        problems = validate_answer(schema, {"a": 1})
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert hits == []
    assert problems == BROKEN


def test_a_file_ref_is_never_read(tmp_path: Path) -> None:
    # Read, the file would make "text" a type problem.
    target = tmp_path / "integer.json"
    target.write_text('{"type": "integer"}')
    assert validate_answer({"$ref": target.as_uri()}, "text") == BROKEN


def test_an_over_long_value_is_refused_before_its_pattern_runs() -> None:
    prop = {"type": "string", "pattern": "^(a|aa)+$"}
    schema = {"type": "object", "properties": {"s": prop}}
    started = time.monotonic()
    [problem] = validate_answer(schema, {"s": "a" * 5000})
    assert problem.field == "s" and "1024" in problem.message
    assert "aaaa" not in problem.message
    assert time.monotonic() - started < 1


HALVES = _asks({"type": "number", "multipleOf": 0.5})


def test_a_huge_integer_against_a_fractional_multiple_is_checked_exactly() -> None:
    assert validate_answer(HALVES, {"x": 10**400}) == []
    [problem] = validate_answer(_asks({"type": "number", "multipleOf": 0.7}), {"x": 10**400})
    assert problem.field == "x" and "multiple" in problem.message


def test_a_fraction_against_a_huge_multiple_is_checked() -> None:
    [problem] = validate_answer(_asks({"type": "number", "multipleOf": 10**400}), {"x": 1.5})
    assert problem.field == "x" and "multiple" in problem.message


@pytest.mark.parametrize("bad", [1e400, -1e400, float("nan")])
def test_a_number_that_is_not_finite_is_refused(bad: float) -> None:
    for schema in (HALVES, _asks({"type": "number"})):
        [problem] = validate_answer(schema, {"x": bad})
        assert problem.field == "x" and "finite" in problem.message


def test_an_integer_too_long_to_write_is_refused() -> None:
    [problem] = validate_answer(_asks({"type": "integer"}), {"x": 10**5000})
    assert problem.field == "x" and "too large" in problem.message


@pytest.mark.parametrize("depth", [900, 100_000])
def test_a_deeply_nested_answer_is_one_problem(depth: int) -> None:
    prop = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    problems = validate_answer(_asks(prop), {"x": [_nested(depth), _nested(depth)]})
    assert problems
    assert {p.field for p in problems} <= {"x", "x/0", "x/1"}
    assert all("nested" in p.message for p in problems)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": ["x"]},
        {"$ref": "#"},
        _asks({"type": ["string", "null"]}),
        _asks({"type": "array", "items": {"type": ["string"]}}),
        _asks({"type": "string", "maxLength": 5, "pattern": "a{99999999999999}"}),
        _asks({"type": "string", "maxLength": 5, "pattern": "(" * 4000 + ")" * 4000}),
        _asks({"type": "string", "enum": [_nested(900)]}),
        _asks({"type": "object", "properties": {}}),
        ["x"],
    ],
    ids=["list", "ref", "types", "item-types", "repeat", "parens", "deep", "object", "no-object"],
)
def test_a_stored_schema_outside_the_subset_is_one_problem(schema: Any) -> None:
    assert validate_answer(schema, {"x": "a"}) == BROKEN


def test_the_answer_is_checked_before_the_schema() -> None:
    [problem] = validate_answer({"$ref": "#"}, {"x": float("nan")})
    assert problem.field == "" and "finite" in problem.message


@pytest.mark.parametrize("bad", ["a\x00b", "\ud800", "a\udfffb"])
def test_text_the_store_cannot_write_is_refused_anywhere(bad: str) -> None:
    tags = {"type": "array", "items": {"type": "string"}}
    schema = _asks({"type": "string"}, tags=tags)
    answers: list[tuple[Any, str]] = [
        ({"x": bad}, "x"),
        ({"tags": ["a", bad]}, "tags/1"),
        ({"x": "a", bad: 1}, ""),
        ({"other": bad}, ""),
        ({"tags": [bad, {"a": 1}]}, "tags/0"),
        ({"x": {"secret_key": bad}}, "x"),
        (bad, ""),
    ]
    for answer, field in answers:
        problems = validate_answer(schema, answer)
        assert [p.field for p in problems if "stored" in p.message] == [field], answer
        assert all(bad not in p.message and bad not in p.field for p in problems)


def test_text_outside_the_basic_plane_is_stored() -> None:
    assert validate_answer(_asks({"type": "string"}), {"x": "\U0001f600 \ud7ff\ue000"}) == []


BAD_VALUES: list[Any] = ["a\x00b", "\ud800", 1e400, float("nan")]


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_a_task_holds_only_what_the_store_can_write(bad: Any) -> None:
    good: dict[str, Any] = {"id": "t", "run_id": "r", "title": "x", "schema": EMAIL}
    places: list[dict[str, Any]] = [
        {"context": [{"type": "text", "text": bad}]},
        {"context": [{"deep": [{"deeper": [bad]}]}]},
        {"schema": _asks({"type": "number", "default": bad})},
        {"schema": _asks({"type": "string", "title": bad})},
        {"schema": _asks({"type": "number", "minimum": bad})},
    ]
    if isinstance(bad, str):
        places += [
            {"title": bad},
            {"ui": bad},
            {"place": bad},
            {"context": [{bad: 1}]},
            {"schema": {"type": "object", "description": bad}},
            {"schema": _asks({"type": "string", "enum": [bad]})},
        ]
    else:
        places.append({"schema": _asks({"type": "string", "enum": [bad]})})
    TaskOpen(**good)
    accepted = []
    for place in places:
        try:
            TaskOpen(**{**good, **place})
        except ValidationError:
            continue
        accepted.append(place)
    assert accepted == []


def test_a_deeply_nested_context_or_schema_is_a_validation_error() -> None:
    good: dict[str, Any] = {"id": "t", "run_id": "r", "title": "x", "schema": EMAIL}
    deep = _asks({"type": "string", "enum": [_nested(5000)]})
    for place in ({"context": [{"x": _nested(100_000)}]}, {"schema": deep}):
        with pytest.raises(ValidationError, match="nested"):
            TaskOpen(**{**good, **place})


@pytest.mark.parametrize(
    "items",
    [
        [{"a": i, "b": [i]} for i in range(2000)],
        [i if i % 2 else f"s{i}" for i in range(4000)],
        # Every one of these integers has the same hash.
        [(i + 1) * (2**61 - 1) for i in range(20_000)],
    ],
    ids=["objects", "mixed", "one-hash"],
)
def test_unique_items_take_no_quadratic_time(items: list[Any]) -> None:
    schema = _asks({"type": "array", "items": {"type": "integer"}, "uniqueItems": True})
    started = time.monotonic()
    validate_answer(schema, {"x": items})
    assert time.monotonic() - started < 2


def test_at_most_fifty_problems_are_returned() -> None:
    schema = _asks({"type": "array", "items": {"type": "integer"}})
    assert len(validate_answer(schema, {"x": ["a"] * 1000})) == 50
    assert len(validate_answer(schema, {"x": [float("nan")] * 1000})) == 50


def test_too_many_items_is_one_problem() -> None:
    schema = _asks({"type": "array", "items": {"type": "integer"}, "maxItems": 2})
    [problem] = validate_answer(schema, {"x": ["a"] * 1000})
    assert problem == AnswerProblem(field="x", message="has more than 2 items")


def test_a_date_time_needs_an_offset() -> None:
    schema = _asks({"type": "string", "format": "date-time"})
    assert validate_answer(schema, {"x": "2026-11-24T09:00:00Z"}) == []
    [problem] = validate_answer(schema, {"x": "2026-11-24T09:00:00"})
    assert problem == AnswerProblem(field="x", message="not a valid date-time")


def test_a_date_time_default_needs_an_offset() -> None:
    prop = {"type": "string", "format": "date-time", "default": "2026-11-24T09:00:00"}
    [problem] = prompt_schema_problems(_asks(prop))
    assert "default" in problem


def test_a_pattern_that_ran_out_of_time_says_so() -> None:
    prop = {"type": "string", "pattern": "^(a|aa)+$", "maxLength": 64}
    schema = _asks({"type": "array", "items": prop})
    problems = validate_answer(schema, {"x": ["a" * 60 + "!"] * 20})
    assert len(problems) == 20
    assert {p.message for p in problems} == {"could not be checked: its pattern took too long"}


def test_a_pattern_that_does_not_compile_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def search(*args: Any, **kwargs: Any) -> None:
        raise regex.error("no")

    prop = {"type": "string", "pattern": "^a$", "maxLength": 64}
    monkeypatch.setattr(regex, "search", search)
    [problem] = validate_answer(_asks(prop), {"x": "a"})
    assert problem.message == "could not be checked: its pattern does not compile"


def test_a_pattern_that_does_not_match_still_says_so() -> None:
    prop = {"type": "string", "pattern": "^a$", "maxLength": 64}
    [problem] = validate_answer(_asks(prop), {"x": "b"})
    assert problem.message == "does not match the required pattern"


def test_an_answer_does_not_run_its_questions_defaults_again() -> None:
    """Counted, not timed: with the budget spent, a default's pattern cannot run."""
    patterned = {"type": "string", "pattern": "^a$", "maxLength": 64, "default": "a"}
    schema = _asks(patterned, y={"type": "string"})
    token = patterns_deadline.set(time.monotonic() - 1)
    try:
        assert validate_answer(schema, {"y": "ok"}) == []
        with pytest.raises(ValidationError):
            TaskOpen(id="t", run_id="r", title="x", schema=schema)
    finally:
        patterns_deadline.reset(token)


def test_an_answer_still_checks_its_questions_structure() -> None:
    schema = _asks({"type": "string", "$ref": "#/a"})
    assert validate_answer(schema, {"x": "a"}) == BROKEN
