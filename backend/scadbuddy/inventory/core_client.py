"""The API's client of scadbuddy-core's inventory (#6949 K1, design §6.1a). Go is the one
writer of the kind, item and stock tables, so the public routes only parse, then:

- read over Go's CRUD routes (`/api/v1/internal/crud/inventory/…`, caller `inventory`, a
  credential minted in this process), at ``SCADBUDDY_CORE_CRUD_URL`` (R-K1-4);
- write by starting Go's activity ``inventory.<Name>`` on the task queue
  ``SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY`` as a standalone activity whose id is the
  idempotency key (R-K1-24). The route waits for it up to ``wait`` seconds; then it answers
  202 with the operation to follow (`api/inventory_operations.py`), and the activity runs
  on. A start while the activity runs attaches (Use Existing); a start after it failed
  runs it again (Allow Duplicate Failed Only); a start after it succeeded reads its stored
  answer. Go's `crud_requests` row stops a second commit.

A problem Go answers passes through unchanged: its status, type, detail and ``code``, and
its other members; the API adds ``instance``. Go or Temporal out of reach is 503
``inventory_unavailable``; when a start fails, nothing was started, so the client may send
the same key again. Workflows that call Go (Task 16) run ``inventory.<Name>`` as a step
and do not use `InventoryCore.write`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from fastapi import status
from pydantic import BaseModel
from temporalio.client import ActivityFailureError, Client
from temporalio.common import ActivityIDConflictPolicy, ActivityIDReusePolicy, RetryPolicy
from temporalio.exceptions import ActivityAlreadyStartedError
from temporalio.service import RPCError

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.wire import PREFIX
from scadbuddy.inventory.problems import PROBLEMS
from scadbuddy.operations.store import OperationAccepted
from scadbuddy.orders.models import Actor

logger = logging.getLogger(__name__)

#: How long a public write waits for its activity before it answers 202 (R-K1-24).
WAIT = 15.0
#: How long a read retries Go out of reach before it answers 503. A person waits on it.
READ_BUDGET = 5.0
SCHEDULE_TO_CLOSE = timedelta(hours=24)
START_TO_CLOSE = timedelta(seconds=30)
RETRY_MAX_INTERVAL = timedelta(seconds=30)
#: The members of a problem that `ApiError` holds itself; Go's others are its extensions.
_OWN = frozenset({"type", "title", "status", "detail", "instance"})


class ActivityHandle(Protocol):
    async def result(self) -> Any: ...


class ActivityStarter(Protocol):
    """What `InventoryCore` uses of `temporalio.client.Client` (temporalio 1.36)."""

    async def start_activity(self, activity: str, arg: Any, **options: Any) -> Any: ...

    def get_activity_handle(self, activity_id: str) -> Any: ...


@dataclass(frozen=True)
class Done:
    """A write the activity answered: Go's public status (201 for a new row) and JSON."""

    body: Any
    status: int = status.HTTP_200_OK


def operation_key(by: Actor, idempotency_key: str | None) -> str:
    """The activity id: the request's ``Idempotency-Key`` scoped to who sent it, so two
    people's keys never meet; a request with none gets a key of its own."""
    if idempotency_key is None:
        return f"req:{uuid4()}"
    return f"{by.kind}:{by.principal or ''}:{idempotency_key}"


def unavailable(detail: str) -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail,
        type_=PROBLEMS + "inventory_unavailable",
        code="inventory_unavailable",
    )


def passed_through(code: int, problem: Mapping[str, Any]) -> ApiError:
    """Go's problem as the API answers it: every member kept, ``instance`` added."""
    return ApiError(
        int(problem.get("status") or code),
        str(problem.get("detail", "")),
        title=problem.get("title"),
        type_=str(problem.get("type", "about:blank")),
        **{k: v for k, v in problem.items() if k not in _OWN},
    )


class InventoryCore:
    def __init__(
        self,
        *,
        url: str | None,
        temporal: Client | ActivityStarter,
        queue: str,
        auth: ServiceAuth,
        transport: httpx.AsyncBaseTransport | None = None,
        wait: float = WAIT,
        read_budget: float = READ_BUDGET,
    ) -> None:
        self._url = url
        self._temporal = temporal
        self._queue = queue
        self._wait = wait
        self._client = (
            None
            if url is None
            else CrudClient(
                url,
                "inventory",
                auth,
                transport=transport,
                unavailable_budget=read_budget,
            )
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def read(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """Go's JSON for ``GET {path}`` (a route under the inventory's CRUD prefix), with
        the query ``params`` that are not None (a bool as ``true``/``false``)."""
        if self._client is None:
            raise unavailable("ScadBuddy's inventory service is not set up here.")
        query = {
            k: (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in (params or {}).items()
            if v is not None
        }
        target = f"{PREFIX}/inventory{path}" + (f"?{urlencode(query)}" if query else "")
        try:
            response = await self._client.request("GET", target)
        except CrudRefusedError as refused:
            raise passed_through(refused.status, refused.problem) from None
        except CrudUnavailableError as error:
            logger.warning("the inventory read %s failed: %s", path, error)
            raise unavailable("ScadBuddy's inventory service did not answer. Try again.") from None
        return response.json()

    async def write(
        self,
        name: str,
        *,
        key: str,
        by: Actor,
        subject: str,
        params: Mapping[str, str] | None = None,
        body: BaseModel | Mapping[str, Any] | None = None,
    ) -> Done | OperationAccepted:
        """Runs ``inventory.{name}`` with id ``key`` and answers it, or 202 when it takes
        longer than the wait. A model ``body`` goes as the fields the request set."""
        sent = (
            body.model_dump(mode="json", exclude_unset=True)
            if isinstance(body, BaseModel)
            else body
        )
        arg = {
            "key": key,
            "actor": by.model_dump(mode="json"),
            "params": dict(params or {}),
            "body": None if sent is None else dict(sent),
        }
        activity = f"inventory.{name}"
        repeated = False
        try:
            handle = await self._temporal.start_activity(
                activity,
                arg,
                id=key,
                task_queue=self._queue,
                schedule_to_close_timeout=SCHEDULE_TO_CLOSE,
                start_to_close_timeout=START_TO_CLOSE,
                id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=ActivityIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                retry_policy=RetryPolicy(maximum_interval=RETRY_MAX_INTERVAL),
                summary=f"{name} {subject}",
            )
        except ActivityAlreadyStartedError:
            # It succeeded before: its stored answer is the answer.
            handle = self._temporal.get_activity_handle(key)
            repeated = True
        except (RPCError, RuntimeError, OSError) as error:
            logger.warning("could not start %s: %s", activity, error)
            raise unavailable(
                "ScadBuddy's background service did not answer. Nothing was changed; try again."
            ) from None
        try:
            # Not shielded: the wait's end stops only this poll of the result; the
            # activity runs on in the worker, and the 202's follow reads its record.
            answer = await asyncio.wait_for(handle.result(), self._wait)
        except (TimeoutError, RPCError):
            return OperationAccepted(
                id=key,
                kind=activity,
                subject=subject,
                status="running",
                created_at=datetime.now(UTC),
                repeated=repeated,
            )
        except ActivityFailureError as failed:
            # A bad actor, or a run Temporal ended (its 24 h passed): no answer to give.
            raise ApiError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"the inventory activity failed: {failed.__cause__ or failed}",
                type_=PROBLEMS + "inventory_activity_failed",
                code="inventory_activity_failed",
            ) from None
        code = int(answer["status"])
        if code >= status.HTTP_400_BAD_REQUEST:
            raise passed_through(code, answer["body"] or {})
        return Done(answer["body"], code)
