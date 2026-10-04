"""What crosses an ``Operation``'s history (#1053). No secret: each activity loads the
stored settings itself."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.operations.store import Operation

OPERATION_WORKFLOW = "Operation"
INSERT_ACTIVITY = "op_insert"
FINISH_ACTIVITY = "op_finish"


def check_activity(kind: str) -> str:
    return f"op.{kind}.check"


def run_activity(kind: str) -> str:
    return f"op.{kind}.run"


class OperationInput(BaseModel):
    kind: str
    subject: str
    #: ``operation_key(...)``; the workflow ID is ``op-<kind>-<key>``.
    key: str
    #: The route's request, as JSON: what the kind's check and run read.
    request: dict[str, Any]
    #: The kind's: 1 for an effect Bambuddy does not dedupe (§4.2).
    run_attempts: int = 1
    #: The kind's run timeout in seconds; the workflow's ``RUN_TIMEOUT`` when None.
    run_timeout_s: float | None = None
    #: Upsert the Scadbuddy* Search Attributes (``SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES``).
    search_attributes: bool = False


class OperationAnswer(BaseModel):
    """The ``accepted`` Update's answer: the finished operation, or the refusal."""

    operation: Operation | None = None
    refusal: PrintRunError | None = None
    repeated: bool = False


class InsertOp(BaseModel):
    input: OperationInput


class RunOp(BaseModel):
    request: dict[str, Any]
    #: What the check returned.
    checked: dict[str, Any]


class FinishOp(BaseModel):
    operation_id: str
    result: dict[str, Any] | None = None
    error: PrintRunError | None = None
