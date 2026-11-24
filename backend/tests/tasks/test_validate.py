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
