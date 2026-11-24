"""Tasks (#7364): a typed question a flow asks a person, and the problems with an answer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.core.events import TASK_PLACE_CHAR, TASK_PLACE_MAX
from scadbuddy.core.json_values import unstorable
from scadbuddy.tasks.schema import prompt_schema_problems

TaskState = Literal["open", "answered", "cancelled", "expired"]


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
    row still reads when the rules tighten later."""

    @field_validator("title", "context", "place", "ui")
    @classmethod
    def _can_be_stored(cls, value: Any) -> Any:
        for bad in unstorable(value):
            raise ValueError(bad.message)
        return value

    @field_validator("schema_")
    @classmethod
    def _schema_fits_the_subset(cls, value: dict[str, Any]) -> dict[str, Any]:
        problems = prompt_schema_problems(value)
        if problems:
            raise ValueError("; ".join(problems))
        return value


class Task(_TaskFields):
    project_id: str | None
    state: TaskState
    answer: dict[str, Any] | None
    answered_by: str | None
    created_at: datetime
    updated_at: datetime


class AnswerProblem(BaseModel):
    #: The declared property (`email`), or an item of one (`tags/0`); empty for the root.
    field: str
    #: Never holds the rejected value: an answer may hold personal data.
    message: str
