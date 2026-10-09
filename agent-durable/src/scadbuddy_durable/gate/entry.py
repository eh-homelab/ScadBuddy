"""A parked durable call as the gate keeps it (spec 2026-10-01 §6.6, "Reads").

``build_entry`` is what DurableSession runs when the plugin's ``pending_approvals()``
gains a call: the workflow state the validator reads, and what ``open_input`` writes.
An ``approval`` carries its summary and input hash, never its input, and no prompt.
An ``answer`` carries its prompt (the question, or the attention message) on purpose,
with the turn's secrets redacted as the classic QuestionService redacts them, and at
most PROMPT_MAX bytes: a longer one is refused to the model as malformed, without
parking.

The plugin hands the workflow the model's arguments unchecked, and the workflow holds
no secret to redact them with (it never holds the credential). So an ``answer`` call's
input is checked against the tool's declared shape (agent/src/tools/answerTools.ts)
before any of it is kept: every string bounded, ``reason`` and ``on_timeout`` from
their sets. Anything else is refused to the model, never parked, and a refusal names
the field, never its value (security review of 5b, plan 5c Ruling 9).
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

# agent/src/harness/questions.ts and attention.ts: the declared shapes.
QUESTIONS_MAX = 4
OPTIONS_MIN = 2
OPTIONS_MAX = 4
QUESTION_TEXT_MAX = 2_000
LABEL_MAX = 200
HEADER_MAX = 200
PREVIEW_MAX = 20_000
MESSAGE_MAX = 2_000
TIMEOUT_MIN_S = 10
TIMEOUT_MAX_S = 86_400
DEFAULT_TIMEOUT_S = 300
# wait_for_user's reasons (5b Ruling 12), each with its card header (attention.ts HEADERS).
WAIT_HEADERS = {
    "tab_disconnected": "Tab disconnected",
    "question": "Question",
    "blocked": "Blocked",
}
# Only `proceed` until a durable turn's stop is offered (spec §6.6 Timeouts).
ON_TIMEOUT = ("proceed",)
_WAIT_KEYS = frozenset({"reason", "message", "options", "timeout_s", "on_timeout"})


class MalformedAnswerInputError(ValueError):
    """The call's input is not the tool's declared shape: refused, never parked."""


class PromptTooLargeError(MalformedAnswerInputError):
    """The prompt is over PROMPT_MAX bytes: the call is refused, never parked."""


@dataclass(frozen=True)
class BuiltEntry:
    entry: GateEntry
    prompt: str
    # The card the panel shows (question.asked's `questions`), redacted; None for an approval.
    questions: list[dict[str, Any]] | None
    # An attention request's {reason, on_timeout}; None otherwise.
    attention: dict[str, Any] | None
    # An attention request's own timer, in seconds; None otherwise.
    timeout_s: int | None = None


def redact(text: str, secrets: Sequence[str]) -> str:
    """agent/src/secrets.ts ``redact``: every secret of 4 characters or more, replaced."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text


def _text(value: Any, field: str, *, low: int, high: int, trim: bool = False) -> str:
    if not isinstance(value, str):
        raise MalformedAnswerInputError(f"{field} must be a string")
    text = value.strip() if trim else value
    if not low <= len(text) <= high:
        raise MalformedAnswerInputError(f"{field} must be {low} to {high} characters")
    return text


def _check_question(q: Any, n: int) -> dict[str, Any]:
    where = f"questions[{n}]"
    if not isinstance(q, dict):
        raise MalformedAnswerInputError(f"{where} must be an object")
    question = _text(q.get("question"), f"{where}.question", low=1, high=QUESTION_TEXT_MAX)
    header = _text(q.get("header"), f"{where}.header", low=0, high=HEADER_MAX)
    multi = q.get("multiSelect")
    if not isinstance(multi, bool):
        raise MalformedAnswerInputError(f"{where}.multiSelect must be true or false")
    options = q.get("options")
    if not isinstance(options, list) or not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
        raise MalformedAnswerInputError(f"{where}.options must be {OPTIONS_MIN} to {OPTIONS_MAX}")
    checked: list[dict[str, Any]] = []
    for m, o in enumerate(options):
        at = f"{where}.options[{m}]"
        if not isinstance(o, dict):
            raise MalformedAnswerInputError(f"{at} must be an object")
        option: dict[str, Any] = {
            "label": _text(o.get("label"), f"{at}.label", low=1, high=LABEL_MAX),
            "description": _text(
                o.get("description"), f"{at}.description", low=0, high=QUESTION_TEXT_MAX
            ),
        }
        if "preview" in o:
            option["preview"] = _text(o["preview"], f"{at}.preview", low=0, high=PREVIEW_MAX)
        if multi and "," in option["label"]:
            raise MalformedAnswerInputError(f"{at}.label of a multiSelect question has a comma")
        checked.append(option)
    if len({o["label"] for o in checked}) != len(checked):
        raise MalformedAnswerInputError(f"{where}.options need their own labels")
    return {"question": question, "header": header, "multiSelect": multi, "options": checked}


def _check_ask(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    if set(tool_input) != {"questions"}:
        raise MalformedAnswerInputError("ask_user takes only `questions`")
    questions = tool_input["questions"]
    if not isinstance(questions, list) or not 1 <= len(questions) <= QUESTIONS_MAX:
        raise MalformedAnswerInputError(f"questions must be 1 to {QUESTIONS_MAX} questions")
    checked = [_check_question(q, n) for n, q in enumerate(questions)]
    if len({q["question"] for q in checked}) != len(checked):
        raise MalformedAnswerInputError("each question must be different")
    return checked


def _check_wait(tool_input: dict[str, Any]) -> dict[str, Any]:
    if not set(tool_input) <= _WAIT_KEYS:
        raise MalformedAnswerInputError(f"wait_for_user takes only {', '.join(sorted(_WAIT_KEYS))}")
    reason = tool_input.get("reason")
    if not isinstance(reason, str) or reason not in WAIT_HEADERS:
        raise MalformedAnswerInputError(f"reason must be one of {', '.join(WAIT_HEADERS)}")
    message = _text(tool_input.get("message"), "message", low=1, high=MESSAGE_MAX, trim=True)
    options: tuple[str, ...] = DEFAULT_REPLIES
    if tool_input.get("options") is not None:
        raw = tool_input["options"]
        if not isinstance(raw, list) or not 2 <= len(raw) <= 4:
            raise MalformedAnswerInputError("options must be 2 to 4 replies")
        options = tuple(
            _text(o, f"options[{n}]", low=1, high=LABEL_MAX, trim=True) for n, o in enumerate(raw)
        )
        if len(set(options)) != len(options):
            raise MalformedAnswerInputError("each option must be different")
    timeout = tool_input.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not TIMEOUT_MIN_S <= timeout <= TIMEOUT_MAX_S
    ):
        raise MalformedAnswerInputError(
            f"timeout_s must be a whole number, {TIMEOUT_MIN_S} to {TIMEOUT_MAX_S}"
        )
    on_timeout = tool_input.get("on_timeout", "proceed")
    if not isinstance(on_timeout, str) or on_timeout not in ON_TIMEOUT:
        raise MalformedAnswerInputError(f"on_timeout must be one of {', '.join(ON_TIMEOUT)}")
    return {
        "reason": reason,
        "message": message,
        "options": options,
        "timeout_s": timeout,
        "on_timeout": on_timeout,
    }


def _card_question(q: dict[str, Any], secrets: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question": redact(q["question"], secrets),
        "header": redact(q["header"], secrets),
        "multiSelect": q["multiSelect"],
        "options": [],
    }
    for o in q["options"]:
        option = {
            "label": redact(o["label"], secrets),
            "description": redact(o["description"], secrets),
        }
        if "preview" in o:
            option["preview"] = redact(o["preview"], secrets)
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
    if not isinstance(tool_input, dict):
        raise MalformedAnswerInputError(f"{tool} takes an object")
    timeout_s: int | None = None
    if tool == ASK_USER:
        card = [_card_question(q, secrets) for q in _check_ask(tool_input)]
        prompt = "\n".join(q["question"] for q in card)
        questions = tuple(Question(q["question"], q["multiSelect"]) for q in card)
        entry = entry_of(questions=questions)
        attention = None
    elif tool == WAIT_FOR_USER:
        wait = _check_wait(tool_input)
        prompt = redact(wait["message"], secrets)
        options = tuple(redact(o, secrets) for o in wait["options"])
        card = [
            {
                "question": prompt,
                "header": WAIT_HEADERS[wait["reason"]],
                "multiSelect": False,
                "options": [{"label": o, "description": ""} for o in options],
            }
        ]
        entry = entry_of(options=options)
        attention = {"reason": wait["reason"], "on_timeout": wait["on_timeout"]}
        timeout_s = wait["timeout_s"]
    else:
        raise ValueError(f"{tool} is not an answer tool")
    if len(prompt.encode("utf-8")) > PROMPT_MAX:
        raise PromptTooLargeError(
            f"the {tool} prompt is over {PROMPT_MAX} bytes; ask a shorter question"
        )
    return BuiltEntry(entry, prompt, card, attention, timeout_s)
