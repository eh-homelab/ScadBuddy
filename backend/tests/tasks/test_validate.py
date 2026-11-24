import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.core.input_schema import schema_problems
from scadbuddy.tasks.models import Task, TaskOpen
from scadbuddy.tasks.validate import prompt_schema_problems, validate_answer

EMAIL = {
    "type": "object",
    "required": ["email"],
    "properties": {
        "email": {"type": "string", "format": "email", "title": "Email"},
        "also_sms": {"type": "boolean", "default": False},
    },
}


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
    enum_schema = {"type": "object", "properties": {"c": {"enum": ["a", "b"]}}}
    short = {"type": "object", "properties": {"n": {"type": "string", "maxLength": 3}}}
    found = [
        validate_answer(EMAIL, {"email": bad}),
        validate_answer(EMAIL, {"email": 12345678}),
        validate_answer(enum_schema, {"c": bad}),
        validate_answer(short, {"n": bad}),
    ]
    for problems in found:
        assert problems
        assert all(bad not in p.message and "12345678" not in p.message for p in problems)


def test_the_field_is_a_slash_joined_path() -> None:
    inner = {"type": "object", "properties": {"b": {"type": "integer"}}}
    schema = {"type": "object", "properties": {"a": inner}}
    [p] = validate_answer(schema, {"a": {"b": "x"}})
    assert p.field == "a/b"


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
    assert [p.field for p in problems] == [""]


def test_a_file_ref_is_never_read() -> None:
    problems = validate_answer({"$ref": "file:///etc/hostname"}, {})
    assert [p.field for p in problems] == [""]


def test_an_over_long_value_is_refused_before_its_pattern_runs() -> None:
    prop = {"type": "string", "pattern": "^(a|aa)+$"}
    schema = {"type": "object", "properties": {"s": prop}}
    started = time.monotonic()
    [problem] = validate_answer(schema, {"s": "a" * 5000})
    assert problem.field == "s" and "1024" in problem.message
    assert "aaaa" not in problem.message
    assert time.monotonic() - started < 1
