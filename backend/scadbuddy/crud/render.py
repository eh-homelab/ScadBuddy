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

`RemoteBlobRefs` and `RemoteGeometryIndex` are `store/refs.py` `BlobRefs.add` and
`store/geometry.py` `GeometryIndex` (get, put, forget) over the routes, with the same
names, signatures and answers. Every write is natural.
"""

from __future__ import annotations

import logging
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
from scadbuddy.store.geometry import GeometryRow

logger = logging.getLogger(__name__)

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
#: A template call's hold of the blob it emitted (`pipeline_activities._hold`): as long as
#: the SQL store's pool wait (30 s). The hold comes after the template ran, so a hold
#: that fails reruns the template.
HOLD_BUDGET = 30.0


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


class RemoteBlobRefs:
    """The render worker holds blobs for its jobs only: the route refuses any other
    holder kind (a 403 ``holder-kind``) and a holder that is no render job (a 404)."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def add(
        self, key: str, holder_kind: str, holder_id: str, *, budget: float | None = None
    ) -> None:
        """``budget``: how long the call tries; None bounds it as a `project` write, since
        `project` holds a done job's blobs inside its 10 s attempt. A job that is gone
        (pruned while it ran) is no refusal: the SQL write held a blob for any holder id,
        and the sweep dropped that orphan ref, so a ref for it holds nothing and the
        render goes on."""
        body = {"key": key, "holder_kind": holder_kind, "holder_id": holder_id}
        try:
            self._client.request(
                "POST",
                f"{BASE}/blob-refs",
                json=body,
                timeout=PROJECT_POOL_TIMEOUT,
                budget=PROJECT_BUDGET if budget is None else budget,
            )
        except CrudRefusedError as error:
            if error.status != 404:
                raise
            logger.warning(
                "no render job holds this blob: the job is gone",
                extra={"job_id": holder_id, "key": key},
            )


class RemoteGeometryIndex:
    """A hint, as `GeometryIndex` is: each call has the client's own budget, as the SQL
    store's waited the pool's default for a connection."""

    def __init__(self, client: CrudSyncClient) -> None:
        self._client = client

    def get(self, geometry_key: str) -> GeometryRow | None:
        response = self._client.request("GET", f"{BASE}/geometry/{_part(geometry_key)}")
        body = response.json()
        if body is None:
            return None
        return GeometryRow(body["piece_key"], list(body["colours"]), dict(body["main"]))

    def put(
        self, geometry_key: str, piece_key: str, colours: list[str], main: dict[str, Any]
    ) -> None:
        self._client.request(
            "PUT",
            f"{BASE}/geometry/{_part(geometry_key)}",
            json={"piece_key": piece_key, "colours": colours, "main": main},
        )

    def forget(self, geometry_key: str, piece_key: str) -> None:
        self._client.request(
            "DELETE",
            f"{BASE}/geometry/{_part(geometry_key)}?piece_key={_part(piece_key)}",
        )


def _part(value: str) -> str:
    return quote(value, safe="")
