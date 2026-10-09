"""A parked durable call as the gate keeps it (spec 2026-10-01 §6.6, "Reads").

``build_entry`` is what DurableSession runs when the plugin's ``pending_approvals()``
gains a call: the workflow state the validator reads, and what ``open_input`` writes.
An ``approval`` carries its summary and input hash, never its input, and no prompt.
An ``answer`` carries its prompt (the question, or the attention message) on purpose,
with the turn's secrets redacted as the classic QuestionService redacts them, and at
most PROMPT_MAX bytes: a longer one is refused to the model as malformed, without
parking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from scadbuddy_durable.gate.validate import GateEntry, Principal, Question

PROMPT_MAX = 16 * 1024
ASK_USER = "ask_user"
WAIT_FOR_USER = "wait_for_user"
# harness/attention.ts DEFAULT_REPLIES: an attention request's options when it names none.
DEFAULT_REPLIES = ("I'm here", "Carry on without me")
REDACTED = "[redacted]"


class PromptTooLargeError(ValueError):
    """The prompt is over PROMPT_MAX bytes: the call is refused, never parked."""


@dataclass(frozen=True)
class BuiltEntry:
    entry: GateEntry
    prompt: str
    # The card the panel shows (question.asked's `questions`), redacted; None for an approval.
    questions: list[dict[str, Any]] | None
    # An attention request's {reason, on_timeout}; None otherwise.
    attention: dict[str, Any] | None


def redact(text: str, secrets: Sequence[str]) -> str:
    """agent/src/secrets.ts ``redact``: every secret of 4 characters or more, replaced."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text


def _card_question(q: dict[str, Any], secrets: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question": redact(str(q["question"]), secrets),
        "header": redact(str(q.get("header", "")), secrets),
        "multiSelect": bool(q.get("multiSelect", False)),
        "options": [],
    }
    for o in q.get("options", []):
        option = {
            "label": redact(str(o["label"]), secrets),
            "description": redact(str(o.get("description", "")), secrets),
        }
        if "preview" in o:
            option["preview"] = redact(str(o["preview"]), secrets)
        out["options"].append(option)
    return out


def build_entry(
    *,
    request_id: str,
    kind: Literal["approval", "answer"],
    tool: str,
    tool_input: dict[str, Any],
    input_hash: str | None,
    requested_by: Principal,
    session_owner: Principal,
    session_creator: Principal,
    secrets: Sequence[str] = (),
) -> BuiltEntry:
    def entry_of(
        questions: tuple[Question, ...] | None = None, options: tuple[str, ...] | None = None
    ) -> GateEntry:
        return GateEntry(
            id=request_id,
            kind=kind,
            state="pending",
            input_hash=input_hash if kind == "approval" else None,
            requested_by=requested_by,
            session_owner=session_owner,
            session_creator=session_creator,
            questions=questions,
            options=options,
        )

    if kind == "approval":
        return BuiltEntry(entry_of(), "", None, None)
    if tool == ASK_USER:
        card = [_card_question(q, secrets) for q in tool_input["questions"]]
        prompt = "\n".join(q["question"] for q in card)
        questions = tuple(Question(q["question"], q["multiSelect"]) for q in card)
        entry = entry_of(questions=questions)
        attention = None
    elif tool == WAIT_FOR_USER:
        prompt = redact(str(tool_input["message"]), secrets)
        options = tuple(redact(o, secrets) for o in tool_input.get("options") or DEFAULT_REPLIES)
        card = [
            {
                "question": prompt,
                "header": str(tool_input["reason"]),
                "multiSelect": False,
                "options": [{"label": o, "description": ""} for o in options],
            }
        ]
        entry = entry_of(options=options)
        attention = {
            "reason": tool_input["reason"],
            "on_timeout": tool_input.get("on_timeout", "proceed"),
        }
    else:
        raise ValueError(f"{tool} is not an answer tool")
    if len(prompt.encode("utf-8")) > PROMPT_MAX:
        raise PromptTooLargeError(
            f"the {tool} prompt is over {PROMPT_MAX} bytes; ask a shorter question"
        )
    return BuiltEntry(entry, prompt, card, attention)
