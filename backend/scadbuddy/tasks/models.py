"""Tasks (#7364): a typed question a flow asks a person, and the problems with an answer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskState = Literal["open", "answered", "cancelled", "expired"]


class TaskOpen(BaseModel):
    """What a flow gives to open a task."""

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
        default=None, min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$"
    )
    ui: str | None = None
    expires_at: datetime | None = None


class Task(TaskOpen):
    project_id: str | None
    state: TaskState
    answer: dict[str, Any] | None
    answered_by: str | None
    created_at: datetime
    updated_at: datetime


class AnswerProblem(BaseModel):
    #: The JSON pointer path of the field, joined by `/`; empty for the root.
    field: str
    #: Never holds the rejected value: an answer may hold personal data.
    message: str
