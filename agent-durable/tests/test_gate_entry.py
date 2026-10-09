from __future__ import annotations

import pytest

from scadbuddy_durable.gate.entry import PROMPT_MAX, PromptTooLargeError, build_entry
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
    with pytest.raises(PromptTooLargeError):
        build_entry(
            request_id=RID,
            kind="answer",
            tool="wait_for_user",
            tool_input={"reason": "blocked", "message": "é" * (PROMPT_MAX // 2 + 1)},
            input_hash=None,
            requested_by=ME,
            session_owner=ME,
            session_creator=ME,
        )
