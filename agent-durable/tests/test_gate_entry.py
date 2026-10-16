from __future__ import annotations

from typing import Any

import pytest

from scadbuddy_durable.gate.entry import (
    PROMPT_MAX,
    MalformedAnswerInputError,
    PromptTooLargeError,
    build_entry,
)
from scadbuddy_durable.gate.validate import Principal, RespondRequest, validate_respond

ME = Principal("browser", "browser")
RID = "durable:00000000-0000-4000-8000-000000000001:run-1:toolu_1"


def _ask(question: str) -> dict[str, object]:
    return {
        "questions": [
            {
                "question": question,
                "header": "Colour",
                "multiSelect": False,
                "options": [
                    {"label": "Red", "description": ""},
                    {"label": "Blue", "description": ""},
                ],
            }
        ]
    }


def test_an_approval_carries_its_hash_and_never_its_input() -> None:
    built = build_entry(
        request_id=RID,
        kind="approval",
        tool="print_output",
        tool_input={"api_key": "sk-secret"},
        input_hash="a" * 64,
        requested_by=ME,
        session_owner=ME,
        session_creator=ME,
    )
    assert built.prompt == ""
    assert built.questions is None
    assert built.entry.input_hash == "a" * 64
    assert "sk-secret" not in repr(built)


def test_a_question_prompt_is_redacted_and_answerable() -> None:
    built = build_entry(
        request_id=RID,
        kind="answer",
        tool="ask_user",
        tool_input=_ask("Use key sk-secret-123?"),
        input_hash=None,
        requested_by=ME,
        session_owner=ME,
        session_creator=ME,
        secrets=["sk-secret-123"],
    )
    assert built.prompt == "Use key [redacted]?"
    valid = validate_respond(
        built.entry,
        RespondRequest(
            RID, {"kind": "answer", "answers": {"Use key [redacted]?": "Red"}}, ME, "browser"
        ),
    )
    assert valid.answers == ("Red",)


def test_an_attention_request_gets_the_default_replies() -> None:
    built = build_entry(
        request_id=RID,
        kind="answer",
        tool="wait_for_user",
        tool_input={"reason": "blocked", "message": "Need you", "on_timeout": "proceed"},
        input_hash=None,
        requested_by=ME,
        session_owner=ME,
        session_creator=ME,
    )
    assert built.entry.options == ("I'm here", "Carry on without me")
    assert built.attention == {"reason": "blocked", "on_timeout": "proceed"}


def test_a_prompt_over_16_kib_is_refused_without_parking() -> None:
    # Four questions at the 2000-character cap, in 3-byte characters: 24 KB.
    long = _ask("€" * 2000)
    long["questions"] = [
        {**long["questions"][0], "question": "€" * 1999 + str(n)}  # type: ignore[index]
        for n in range(4)
    ]
    assert len(str(long).encode()) > PROMPT_MAX
    with pytest.raises(PromptTooLargeError):
        _build("ask_user", long)


def _build(tool: str, tool_input: dict[str, Any]) -> Any:
    return build_entry(
        request_id=RID,
        kind="answer",
        tool=tool,
        tool_input=tool_input,
        input_hash=None,
        requested_by=ME,
        session_owner=ME,
        session_creator=ME,
    )


# Security review of 5b (sensitive-data-exposure): an answer tool's input is the
# model's, unchecked by the plugin, and the workflow that builds the entry holds no
# secret to redact it with. Anything outside the tool's declared shape is refused to
# the model without parking, so no unvalidated string reaches the projection, the
# card, or the events.
@pytest.mark.parametrize(
    "tool_input",
    [
        {"reason": "sk-live-secret", "message": "hi"},
        {"reason": "done", "message": "hi"},
        {"reason": "blocked", "message": "hi", "on_timeout": "stop"},
        {"reason": "blocked", "message": "hi", "on_timeout": "sk-live-secret"},
        {"reason": "blocked", "message": ""},
        {"reason": "blocked", "message": "x" * 2001},
        {"reason": "blocked", "message": "hi", "options": ["only one"]},
        {"reason": "blocked", "message": "hi", "options": ["a", "a"]},
        {"reason": "blocked", "message": "hi", "options": ["a", "b" * 201]},
        {"reason": "blocked", "message": "hi", "timeout_s": "soon"},
        {"reason": "blocked", "message": "hi", "extra": "sk-live-secret"},
        {"reason": "blocked"},
    ],
)
def test_a_malformed_attention_request_is_refused(tool_input: dict[str, Any]) -> None:
    with pytest.raises(MalformedAnswerInputError):
        _build("wait_for_user", tool_input)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda q: q.update(questions="sk-live-secret"),
        lambda q: q.update(questions=[]),
        lambda q: q.update(questions=[q["questions"][0]] * 5),
        lambda q: q["questions"][0].update(header="h" * 201),
        lambda q: q["questions"][0].update(question=""),
        lambda q: q["questions"][0].update(options=[{"label": "only"}]),
        lambda q: q["questions"][0]["options"][0].update(label=""),
        lambda q: q["questions"][0]["options"][0].update(label=["sk-live-secret"]),
        lambda q: q["questions"][0].update(multiSelect="yes"),
        lambda q: q.update(extra=1),
    ],
)
def test_a_malformed_question_is_refused(mutate: Any) -> None:
    tool_input = _ask("Which?")
    mutate(tool_input)
    with pytest.raises(MalformedAnswerInputError):
        _build("ask_user", tool_input)


def test_an_attention_card_names_its_reason_by_a_fixed_header() -> None:
    built = _build("wait_for_user", {"reason": "tab_disconnected", "message": "Reopen the tab"})
    assert built.questions[0]["header"] == "Tab disconnected"
    assert built.attention == {"reason": "tab_disconnected", "on_timeout": "proceed"}
