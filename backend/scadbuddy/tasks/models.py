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

from scadbuddy.core.events import TASK_PLACE_CHAR, TASK_PLACE_MAX
from scadbuddy.core.json_values import unstorable
from scadbuddy.tasks.schema import prompt_schema_problems

TaskState = Literal["open", "answered", "cancelled", "expired"]

#: A context or an answer, as JSON text, may be this many bytes.
MAX_JSON_BYTES = 16 * 1024
MAX_UI_CHARS = 200
MAX_BY_CHARS = 200
#: A `TaskOpen`'s validation context that leaves its schema's defaults unchecked: that
#: check runs patterns against a clock, which a workflow must not do.
UNCHECKED_DEFAULTS = {"defaults": False}


def json_fits(value: Any) -> bool:
    """Whether ``value``, as JSON text, is at most `MAX_JSON_BYTES`."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8", "replace")) <= MAX_JSON_BYTES


class _TaskFields(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    #: The harness call id.
    id: str
    run_id: str
    title: str = Field(min_length=1, max_length=500)
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


class TaskOpen(_TaskFields):
    """What a flow gives to open a task. Only this way in checks the schema, so a stored
    row still reads when the rules tighten later. Its context and its schema nest at most
    `MAX_DEPTH` deep, counted without recursion."""

    @field_validator("title", "context", "place", "ui")
    @classmethod
    def _can_be_stored(cls, value: Any) -> Any:
        for bad in unstorable(value):
            raise ValueError(bad.message)
        return value

    @field_validator("context")
    @classmethod
    def _context_is_small(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not json_fits(value):
            raise ValueError(f"the context is more than {MAX_JSON_BYTES} bytes of JSON")
        return value

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
        problems = prompt_schema_problems(value, defaults=defaults)
        if problems:
            raise ValueError("; ".join(problems))
        return value


def open_problems(error: ValidationError) -> str:
    """Why a `TaskOpen` was refused: the fields and the rules, never a value."""
    return "; ".join(
        f"{'.'.join(str(part) for part in one['loc'])}: {one['msg'].removeprefix('Value error, ')}"
        for one in error.errors(include_input=False)
    )


class TaskQuestion(_TaskFields):
    """A task without its answer, and without who gave it."""

    project_id: str | None
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
