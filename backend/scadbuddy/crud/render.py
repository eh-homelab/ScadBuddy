"""The render worker's stores over the CRUD routes (#5298). The routes are
`api/crud_render.py`.

`RemoteRenderSettingsSource` is `store/bambuddy.py` `RenderSettingsSource` read from the
API instead of the database: the same 30 s re-read, and a re-read that fails keeps the
last good settings.

`RemoteJobProjection` is the part of `render/projection.py` `JobProjection` the worker's
activities call (accept, claims, `project`), with the same names, signatures and
answers. Every write but the finish is natural (plan D5): sent again after a lost
answer, the route answers as it did the first time. The finish sends an
``Idempotency-Key`` of its own, so a resend replays whether this call settled the row.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient
from scadbuddy.crud.guard import PREFIX
from scadbuddy.library.settings_store import RenderStoreSettings
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError, StepInfo
from scadbuddy.render.projection import PROJECT_POOL_TIMEOUT
from scadbuddy.store.bambuddy import StoreSettingsSource

BASE = f"/api/v1{PREFIX}/render"
#: `api/crud_render.py` `QUEUE_FULL`.
QUEUE_FULL = "queue-full"
#: How long a `project` write tries again after a failed try, inside its 10 s attempt
#: (`pipelines.PROJECT_TIMEOUT`). Each wait of a try (connect, send, read) lasts at most
#: `PROJECT_POOL_TIMEOUT` (5 s), as the SQL store's pool wait does; a credential exchange
#: is outside these bounds.
PROJECT_BUDGET = 4.0
#: An accept's bounds. Each wait of a try (connect, send, read) lasts at most
#: `ACCEPT_TIMEOUT` (accepts took 0.03-0.4 s on prod) and no try starts past
#: `ACCEPT_BUDGET`, so `ACCEPT_RETRY`'s three attempts with their backoff (1 s, 2 s)
#: usually end within the route's 10 s deadline. A slow connect followed by a slow read
#: can take a try past that; the request is then answered still-accepting at the
#: deadline, and the run's last attempt decides. A read timeout on the last attempt
#: passes (`pipelines.ACCEPT_RETRY`), so a slow API is never answered render-unstartable.
ACCEPT_TIMEOUT = 2.0
ACCEPT_BUDGET = 1.0


class RemoteRenderSettingsSource(StoreSettingsSource):
    def __init__(
        self,
        client: CrudClient,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(ttl=ttl, clock=clock)
        self._client = client

    async def _read(self) -> RenderStoreSettings:
        response = await self._client.request("GET", f"{BASE}/settings")
        return RenderStoreSettings.model_validate(response.json())


class RemoteJobProjection:
    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def close(self) -> None:
        self._client.close()

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        budget: float | None = None,
        timeout: float = PROJECT_POOL_TIMEOUT,
        key: str | None = None,
    ) -> httpx.Response:
        """``budget``: an accept's or a `project` write's call, ended inside its attempt,
        each of its waits bounded by ``timeout``."""
        return self._client.request(
            method,
            f"{BASE}{path}",
            json=json,
            idempotency_key=key,
            timeout=None if budget is None else timeout,
            budget=budget,
        )

    def accept(
        self,
        job: Job,
        key: str,
        *,
        workflow_id: str,
        run_id: str,
        max_pending: int = 0,
        supersedes: str | None = None,
    ) -> Job:
        body = {
            "job": job.model_dump(mode="json"),
            "key": key,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "max_pending": max_pending,
            "supersedes": supersedes,
        }
        try:
            response = self._send(
                "POST", "/jobs/accept", json=body, budget=ACCEPT_BUDGET, timeout=ACCEPT_TIMEOUT
            )
        except CrudRefusedError as error:
            if error.status == 409 and error.problem.get("reason") == QUEUE_FULL:
                raise QueueFullError(int(error.problem["depth"])) from error
            raise
        return Job.model_validate(response.json())

    def set_claims(self, job_id: str, claims: int) -> None:
        self._send(
            "PUT", f"/jobs/{_part(job_id)}/claims", json={"claims": claims}, budget=PROJECT_BUDGET
        )

    def mark_started(self, job_id: str) -> Job | None:
        response = self._send("POST", f"/jobs/{_part(job_id)}/started", budget=PROJECT_BUDGET)
        body = response.json()
        return None if body is None else Job.model_validate(body)

    def set_steps(self, job_id: str, steps: list[StepInfo]) -> None:
        self._send(
            "PUT",
            f"/jobs/{_part(job_id)}/steps",
            json=[step.model_dump(mode="json") for step in steps],
            budget=PROJECT_BUDGET,
        )

    def finish(self, job: Job) -> bool:
        """As `JobProjection.finish`: whether this call changed the row (the route's
        ``moved``), so a retried activity's call, which finds it settled, answers False
        and its settle is counted once."""
        response = self._send(
            "POST",
            f"/jobs/{_part(job.id)}/finish",
            json=job.model_dump(mode="json"),
            budget=PROJECT_BUDGET,
            key=uuid.uuid4().hex,
        )
        return bool(response.json()["moved"])

    def read(self, job_id: str, *, pool_timeout: float | None = None) -> Job:
        """``pool_timeout``: a `project` activity's read, bounded as its writes are."""
        try:
            response = self._send(
                "GET",
                f"/jobs/{_part(job_id)}",
                budget=None if pool_timeout is None else PROJECT_BUDGET,
            )
        except CrudRefusedError as error:
            if error.status == 404:
                raise JobNotFoundError(job_id) from error
            raise
        return Job.model_validate(response.json())


def _part(value: str) -> str:
    return quote(value, safe="")
