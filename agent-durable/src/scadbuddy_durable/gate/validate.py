"""The tool-call gate's validator (spec 2026-10-01 §6.6, "Responses").

A port of agent/src/gate/validate.ts, check for check and in the same order; the
shared vectors run against both. It reads nothing but its arguments (no I/O, no
clock), so DurableSession's ``respond`` Update can run it as its validator, beside
the plugin's ``validate_decision``. Who the caller is (``role``) is the route's to
decide; this checks only whether the kind accepts that role.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

# The agent's harness/questions.ts bounds, which the vectors file carries.
QUESTIONS_MAX = 4
QUESTION_TEXT_MAX = 2_000
ANSWER_MAX = 20_000
# The response cap, the classic route's (plan 5b ruling 1): the largest valid answer
# at its worst JSON encoding.
RESPONSE_MAX = QUESTIONS_MAX * (QUESTION_TEXT_MAX + ANSWER_MAX) * 6 + 1024

Role = Literal["browser", "grant", "owner"]
RefusalCode = Literal[
    "stale",
    "resolved",
    "resolving",
    "forbidden",
    "self",
    "malformed",
    "too_large",
    "input_mismatch",
]
State = Literal["pending", "resolving", "resolved"]


@dataclass(frozen=True)
class Principal:
    kind: str
    id: str


@dataclass(frozen=True)
class Question:
    question: str
    multi_select: bool


@dataclass(frozen=True)
class GateEntry:
    """What the workflow holds of a parked call."""

    id: str
    kind: Literal["approval", "answer"]
    state: State
    input_hash: str | None
    requested_by: Principal | None
    session_owner: Principal | None
    session_creator: Principal | None
    questions: tuple[Question, ...] | None = None
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RespondRequest:
    request_id: str
    response: Any
    responder: Principal
    role: Role


@dataclass(frozen=True)
class ValidResponse:
    decision: Literal["approve", "deny"] | None = None
    answers: tuple[str, ...] = field(default_factory=tuple)


class RespondRefused(Exception):  # noqa: N818 - the name the spec gives the refusal
    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(message)
        self.code: RefusalCode = code


_ALLOWED: dict[str, tuple[Role, ...]] = {"approval": ("browser", "grant"), "answer": ("browser",)}
_HEX64 = frozenset("0123456789abcdef")


# What JavaScript's String.prototype.trim removes (WhiteSpace and LineTerminator,
# ECMA-262), which str.strip() does not match: it keeps U+FEFF and strips U+001C-U+001F.
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _js_trim(text: str) -> str:
    return text.strip(_JS_WHITESPACE)


def utf16_length(text: str) -> int:
    """A JavaScript string's length, which the agent's bounds count."""
    return len(text.encode("utf-16-le")) // 2


def response_bytes(response: Any) -> int:
    """The UTF-8 length of the response as JSON.stringify writes it."""
    return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _is_answer(value: Any) -> bool:
    return isinstance(value, str) and 1 <= utf16_length(value) <= ANSWER_MAX


def _malformed(message: str) -> RespondRefused:
    return RespondRefused("malformed", message)


def _same(a: Principal | None, b: Principal) -> bool:
    return a is not None and a.kind == b.kind and a.id == b.id


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_respond(entry: GateEntry | None, request: RespondRequest) -> ValidResponse:
    rid = request.request_id
    if entry is None or entry.id != rid:
        raise RespondRefused("stale", f"no pending input {rid}: it is stale or was never asked")
    if entry.state == "resolved":
        raise RespondRefused("resolved", f"{rid} is no longer waiting for a response")
    if entry.state == "resolving":
        raise RespondRefused("resolving", f"{rid} is being resolved; it can no longer be answered")
    if response_bytes(request.response) > RESPONSE_MAX:
        raise RespondRefused("too_large", f"a response may be at most {RESPONSE_MAX} bytes")
    if request.role not in _ALLOWED[entry.kind]:
        raise RespondRefused(
            "forbidden",
            "only the user in the ScadBuddy panel answers the agent's questions"
            if entry.kind == "answer"
            else "outward actions need a human approval in the ScadBuddy UI, or another agent"
            " with a per-token approval grant",
        )
    if entry.kind == "approval" and request.role != "browser":
        own = (entry.requested_by, entry.session_owner, entry.session_creator)
        if any(_same(o, request.responder) for o in own):
            raise RespondRefused(
                "self",
                "an approval grant is for approving another agent's outward actions, never your"
                " own; ask the user in the ScadBuddy UI",
            )
    body = request.response
    if not isinstance(body, dict) or body.get("kind") not in ("approval", "answer"):
        raise _malformed('a response is {"kind": "approval", …} or {"kind": "answer", …}')
    if entry.kind == "approval":
        return _approval(entry, rid, body)
    return _answer(entry, rid, body)


def _approval(entry: GateEntry, rid: str, body: dict[str, Any]) -> ValidResponse:
    if body["kind"] != "approval":
        raise _malformed(
            f'{rid} is an approval: respond with {{"kind": "approval", "decision": …}}'
        )
    if not set(body) <= {"kind", "decision", "input_hash"}:
        raise _malformed('an approval takes only "decision" and "input_hash"')
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise _malformed('"decision" must be "approve" or "deny"')
    if "input_hash" in body:
        given = body["input_hash"]
        if not isinstance(given, str) or len(given) != 64 or not set(given) <= _HEX64:
            raise _malformed('"input_hash" must be 64 lowercase hex characters')
        if given != entry.input_hash:
            raise RespondRefused(
                "input_mismatch",
                f"{rid} is for a different input than the one you were shown; the call needs a"
                " new approval",
            )
    return ValidResponse(decision="approve" if decision == "approve" else "deny")


def _answer(entry: GateEntry, rid: str, body: dict[str, Any]) -> ValidResponse:
    if body["kind"] != "answer":
        raise _malformed(f'{rid} asks for an answer: respond with {{"kind": "answer", …}}')
    if not set(body) <= {"kind", "answers", "choice", "text"}:
        raise _malformed('an answer takes only "answers", "choice" or "text"')
    if entry.options is not None:
        choice, text = body.get("choice"), body.get("text")
        if "answers" in body or (("choice" in body) == ("text" in body)):
            raise _malformed(
                f'{rid} is an attention request: respond with exactly one of "choice" or "text"'
            )
        if "choice" in body:
            if not _is_answer(choice) or choice not in entry.options:
                raise _malformed(
                    f'"choice" must be one of {_dumps(list(entry.options))}; use "text" for your'
                    " own words"
                )
            return ValidResponse(answers=(choice,))
        if not isinstance(text, str) or not _is_answer(text) or not _js_trim(text):
            raise _malformed(f'"text" must be 1 to {ANSWER_MAX} characters, not only spaces')
        return ValidResponse(answers=(text,))
    asked = entry.questions or ()
    given = body.get("answers")
    if not isinstance(given, dict) or "choice" in body or "text" in body:
        raise _malformed(
            f'{rid} is a question: respond with "answers", one per question, keyed by its text'
        )
    if len(given) != len(asked) or not all(q.question in given for q in asked):
        raise _malformed(
            '"answers" must answer exactly these questions: ' + _dumps([q.question for q in asked])
        )
    answers: list[str] = []
    for q in asked:
        a = given[q.question]
        if isinstance(a, str):
            if not _is_answer(a):
                raise _malformed(f"each answer must be 1 to {ANSWER_MAX} characters")
            answers.append(a)
            continue
        if not isinstance(a, list) or not a or not all(_is_answer(p) for p in a):
            raise _malformed(
                f"{_dumps(q.question)} takes an answer: a string, or for a multi-select a list"
                " of picks"
            )
        if not q.multi_select:
            raise _malformed(f"{_dumps(q.question)} takes one answer: send a string, not a list")
        if any(", " in p for p in a):
            raise _malformed(
                'a pick must not contain ", ", which joins a multi-select\'s picks; send the'
                " answer as one string"
            )
        answers.append(", ".join(a))
    if any(utf16_length(a) > ANSWER_MAX for a in answers):
        raise _malformed(
            f"each answer must be at most {ANSWER_MAX} characters, a multi-select's picks joined"
            ' with ", "'
        )
    if any(not _js_trim(a) for a in answers):
        raise _malformed("an answer must not be only spaces")
    return ValidResponse(answers=tuple(answers))
