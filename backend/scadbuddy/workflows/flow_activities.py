"""A flow run's activities on the `projects` queue (plan 2026-10-09 Rulings 7 and 8):
its row, the `close` that ends its execution, and its prompts' tasks (#7364)."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.input_schema import PATTERN_TIMEOUT_S, PATTERNS_BUDGET_S, PatternsTimedOutError
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.wire import PATTERN_TIMEOUT
from scadbuddy.flows.store import FlowStore
from scadbuddy.tasks.models import Task, TaskOpen, open_problems
from scadbuddy.tasks.store import RunEndedError, RunNotFoundError
from scadbuddy.workflows.flow_models import FlowRecord, ProjectionWrite
from scadbuddy.workflows.flow_steps import FLOW_PROJECT

if TYPE_CHECKING:
    from scadbuddy.crud.projects import RemoteFlowStore

FLOW_RECORD = "flow_record"
FLOW_CLOSE = "flow_close"
FLOW_TASK_OPEN = "flow_task_open"
FLOW_TASK_ANSWERED = "flow_task_answered"
FLOW_TASK_CLOSE = "flow_task_close"
#: A prompt's task cannot be opened as asked: the message names the problem.
TASK_REFUSED = "TaskRefused"
#: A prompt's run takes no task: it ended, or it is gone.
TASK_RUN_OVER = "TaskRunOver"
#: The attempt at which a default's pattern that ran out of time is refused, not tried
#: again (#7437, #7688). With Temporal's default backoff the attempts before it span about
#: five minutes, long enough for a busy pod; a pattern that times out on every pod is the
#: script's.
PATTERN_TIMEOUT_ATTEMPTS = 10


class FlowActivities:
    def __init__(self, store: "FlowStore | RemoteFlowStore") -> None:
        self._store = store

    @activity.defn(name=FLOW_RECORD)
    async def record(self, record: FlowRecord) -> None:
        """Insert the run's row; a retry finds it there."""
        await self._store.insert_run(record)

    @activity.defn(name=FLOW_PROJECT)
    async def project(self, write: ProjectionWrite) -> None:
        """One of the run's own writes to its row (a local activity)."""
        await self._store.project(write)

    @activity.defn(name=FLOW_CLOSE)
    async def close(self, workflow_id: str) -> None:
        """The harness's public `close` Signal, sent to the run's own workflow as the
        harness's web app sends it: the execution then completes."""
        await activity.client().get_workflow_handle(workflow_id).signal("close")

    def all(self) -> list[Callable[..., Any]]:
        return [self.record, self.project, self.close]


class RunTasks(Protocol):
    """Where a run's tasks are written: `RemoteTasks`, or the API's own `TasksComponent`."""

    async def open(self, task: TaskOpen, *, workflow_run_id: str | None = None) -> Task: ...

    async def close(
        self,
        task_id: str,
        state: Literal["expired", "cancelled"],
        *,
        workflow_run_id: str | None = None,
    ) -> Task | None: ...

    async def mark_answered(
        self,
        task_id: str,
        answer: dict[str, Any],
        by: str | None,
        workflow_run_id: str,
        *,
        final: bool = False,
    ) -> Task: ...


def _execution() -> str:
    run_id = activity.info().workflow_run_id
    assert run_id is not None  # Only a workflow starts these.
    return run_id


def _refused(type_: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=type_, non_retryable=True)


def _refuse_past_attempts() -> None:
    """Refuse a default's pattern timeout once `PATTERN_TIMEOUT_ATTEMPTS` attempts met it."""
    if activity.info().attempt >= PATTERN_TIMEOUT_ATTEMPTS:
        raise _refused(
            TASK_REFUSED,
            "schema: a default's pattern took too long to check"
            f" (over {PATTERN_TIMEOUT_S}s, or {PATTERNS_BUDGET_S}s together)"
            f" on {PATTERN_TIMEOUT_ATTEMPTS} attempts",
        ) from None


class FlowTaskActivities:
    """What `prompt()` does to its task. Each write names the activity's own execution,
    never one from its input: a Reset gives a scheduled activity to the new execution with
    the input it had."""

    def __init__(self, tasks: RunTasks) -> None:
        self._tasks = tasks

    @activity.defn(name=FLOW_TASK_OPEN)
    async def open(self, task: dict[str, Any]) -> None:
        """Open the task, a `TaskOpen`'s JSON. A retry finds it there.

        Final: a task the model or the route refuses (422), a run that has ended, and a run
        that is not there. Every other refusal is tried again without limit (but see the
        pattern timeout below): a credential refused (401, 403), any other 4xx, a 5xx, a
        route that is missing during a roll, an API that is away. Those are ScadBuddy's to
        mend and not the script's, so they must not fail a run that may have worked for
        weeks. Nothing is lost while it waits: the
        question is not asked yet, and a cancel still ends the run.

        A default's pattern that ran out of time, here (`PatternsTimedOutError`) or in the
        API (its 503 ``pattern-timeout``), is tried again up to `PATTERN_TIMEOUT_ATTEMPTS`,
        then refused: a busy pod passes, but a pattern that times out on every pod is the
        script's (#7437, #7688)."""
        try:
            # Off the loop: the check of a default runs its pattern.
            checked = await asyncio.to_thread(TaskOpen.model_validate, task)
            await self._tasks.open(checked, workflow_run_id=_execution())
        except ValidationError as error:
            raise _refused(TASK_REFUSED, open_problems(error)) from None
        except RunEndedError:
            raise _refused(TASK_RUN_OVER, "the run has ended") from None
        except RunNotFoundError:
            raise _refused(TASK_RUN_OVER, "no such run") from None
        except CrudRefusedError as error:
            if error.status != 422:
                raise
            raise _refused(TASK_REFUSED, _said(error.problem)) from None
        except PatternsTimedOutError:
            _refuse_past_attempts()
            raise
        except CrudUnavailableError as error:
            if error.problem.get("reason") == PATTERN_TIMEOUT:
                _refuse_past_attempts()
            raise

    @activity.defn(name=FLOW_TASK_ANSWERED)
    async def answered(self, task_id: str, answer: dict[str, Any]) -> None:
        """The run's own mark, of the answer its script gets. A task the run's forgetting
        deleted is not an error."""
        try:
            await self._tasks.mark_answered(task_id, answer, None, _execution(), final=True)
        except KeyError:
            activity.logger.info("the task %s is gone", task_id)

    @activity.defn(name=FLOW_TASK_CLOSE)
    async def close(self, task_id: str, state: Literal["expired", "cancelled"]) -> None:
        """The run stopped waiting, with no answer. It may be closed or gone already."""
        await self._tasks.close(task_id, state, workflow_run_id=_execution())

    def all(self) -> list[Callable[..., Any]]:
        return [self.open, self.answered, self.close]


def _said(problem: dict[str, Any]) -> str:
    """A 422's own problems, else its detail."""
    errors = problem.get("errors")
    said = [str(e["msg"]) for e in errors if "msg" in e] if isinstance(errors, list) else []
    return "; ".join(said) or str(problem.get("detail", "refused"))
