"""Tasks (#7364): a typed question a flow asks a person, and the problems with an answer."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from scadbuddy.codes.parse import format_code, parse_code
from scadbuddy.codes.resolve import PREFIXES
from scadbuddy.core.events import TASK_PLACE_CHAR, TASK_PLACE_MAX
from scadbuddy.core.json_values import unstorable
from scadbuddy.tasks.schema import prompt_schema_problems
from scadbuddy.tasks.widgets import context_problems

#: The prefix of a location's code.
LOCATION_PREFIX = next(prefix for prefix, one in PREFIXES.items() if one.kind == "location")

TaskState = Literal["open", "answered", "cancelled", "expired"]

#: A context or an answer, as JSON text, may be this many bytes.
MAX_JSON_BYTES = 16 * 1024
MAX_UI_CHARS = 200
MAX_BY_CHARS = 200
#: A `TaskOpen`'s validation context that leaves its schema's defaults unchecked: that
#: check runs patterns against a clock, which a workflow must not do.
UNCHECKED_DEFAULTS = {"defaults": False}
#: As `UNCHECKED_DEFAULTS`, with the rules from before #8062: no widget keys in the
#: schema, and the context's blocks are not checked. A workflow replays an older
#: `prompt` call with these (`flow_tools.PROMPT_WIDGETS_PATCH`).
BEFORE_WIDGETS = {"defaults": False, "widgets": False}
#: The task of a `wait_for_human` under `flow_tools.HUMAN_PROMPT_PATCH` (#8062).
#: `validate.validate_answer` also refuses an answer with no character but spaces, as the
#: text answer's route does (#2379): a schema's `pattern` would need a `maxLength` of at
#: most 1024.
HUMAN_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "title": "Answer", "minLength": 1}},
    "required": ["answer"],
}


def folded_place(place: str) -> str:
    """A place that is a location's code (`l7`, `L-07`) as `L-7`, and any other as it is:
    a station lists its tasks by its own code (#8062)."""
    code = parse_code(place)
    if code is not None and code.prefix == LOCATION_PREFIX:
        return format_code(code.prefix, code.number)
    return place


def _widgets(info: ValidationInfo) -> bool:
    return bool((info.context or {}).get("widgets", True))


def json_fits(value: Any) -> bool:
    """Whether ``value``, as JSON text, is at most `MAX_JSON_BYTES`."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8", "replace")) <= MAX_JSON_BYTES


#: The longest task title.
TASK_TITLE_MAX = 500


class TaskFields(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    #: The harness call id.
    id: str
    run_id: str
    title: str = Field(min_length=1, max_length=TASK_TITLE_MAX)
    #: The closed JSON Schema subset of `core/input_schema.py`.
    schema_: dict[str, Any] = Field(alias="schema")
    context: list[dict[str, Any]] = []
    #: Where the task is shown. It becomes part of a realtime topic, so it holds no
    #: control character.
    place: str | None = Field(
        default=None, min_length=1, max_length=TASK_PLACE_MAX, pattern=rf"^{TASK_PLACE_CHAR}+$"
    )
    ui: str | None = None
    expires_at: datetime | None = None
    #: Who it is for, a principal string. It filters a list; it refuses no one's answer.
    #: No control character: the range of `TASK_PLACE_CHAR`, which matches the column's
    #: CHECK, so the store never refuses what this takes.
    assignee: str | None = Field(
        default=None, min_length=1, max_length=MAX_BY_CHARS, pattern=rf"^{TASK_PLACE_CHAR}+$"
    )


class TaskOpen(TaskFields):
    """What a flow gives to open a task. Only this way in checks the schema, so a stored
    row still reads when the rules tighten later. Its context and its schema nest at most
    `MAX_DEPTH` deep, counted without recursion."""

    @field_validator("title", "context", "place", "ui", "assignee")
    @classmethod
    def _can_be_stored(cls, value: Any) -> Any:
        for bad in unstorable(value):
            raise ValueError(bad.message)
        return value

    @field_validator("context")
    @classmethod
    def _context_is_small(
        cls, value: list[dict[str, Any]], info: ValidationInfo
    ) -> list[dict[str, Any]]:
        if not json_fits(value):
            raise ValueError(f"the context is more than {MAX_JSON_BYTES} bytes of JSON")
        if _widgets(info) and (problems := context_problems(value)):
            raise ValueError("; ".join(problems))
        return value

    @field_validator("place")
    @classmethod
    def _place_is_a_code(cls, value: str | None) -> str | None:
        return None if value is None else folded_place(value)

    @field_validator("ui")
    @classmethod
    def _ui_is_short(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_UI_CHARS:
            raise ValueError(f"the ui is more than {MAX_UI_CHARS} characters")
        return value

    @field_validator("schema_")
    @classmethod
    def _schema_fits_the_subset(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        defaults = (info.context or {}).get("defaults", True)
        widgets = _widgets(info)
        # A default's pattern that ran out of time (its own `PATTERN_TIMEOUT_S` or the shared
        # `PATTERNS_BUDGET_S`) raises `PatternsTimedOutError`, which pydantic passes on as it
        # is, not as a refusal: the caller tries again (#7437, #7688).
        # It drops the other fields' errors pydantic collected; the workflow refuses those
        # before the task reaches the activity (`_prompt_task`).
        problems = prompt_schema_problems(
            value, defaults=defaults, widgets=widgets, retry_timeouts=True
        )
        if problems:
            raise ValueError("; ".join(problems))
        return value


def open_problems(error: ValidationError) -> str:
    """Why a `TaskOpen` was refused: the fields and the rules, never a value."""
    return "; ".join(
        f"{'.'.join(str(part) for part in one['loc'])}: {one['msg'].removeprefix('Value error, ')}"
        for one in error.errors(include_input=False)
    )


class TaskQuestion(TaskFields):
    """A task without its answer, and without who gave it."""

    state: TaskState
    created_at: datetime
    updated_at: datetime


class Task(TaskQuestion):
    answer: dict[str, Any] | None
    answered_by: str | None


class AnswerProblem(BaseModel):
    #: The declared property (`email`), or an item of one (`tags/0`); empty for the root.
    field: str
    #: Never holds the rejected value: an answer may hold personal data.
    message: str
