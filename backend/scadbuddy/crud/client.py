"""The workers' client of the API's CRUD routes (#5298, plan decisions D4, D5).

A render, print or projects worker holds no database secret: it calls
``/api/v1/internal/crud/<caller>/…`` with its service credential (`ServiceAuth`).

- A response without ``X-ScadBuddy-Crud`` did not come from a CRUD route: the route is
  missing (an old API pod during a roll). It is tried again on a new connection, and a
  route still missing after ``unavailable_budget`` seconds is `CrudUnavailableError`. A
  5xx and a transport failure retry within the same budget.
- A 4xx with the header is `CrudRefusedError`, carrying the problem. Two refusals earn
  one retry first, once per call: a 401 (the credential died; `ServiceAuth` has dropped
  it) and a 403 ``out_of_scope`` (the credential's scopes are stale; `refresh` drops it).
- Every retry sends the same ``Idempotency-Key``; `activity_key` builds a key that is
  the same on every attempt of one Temporal activity.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
from temporalio import activity

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.core.metrics import Metrics
from scadbuddy.crud.guard import CRUD_HEADER, PREFIX, CrudCaller

#: How long a missing route, a 5xx or a transport failure is retried (D4).
UNAVAILABLE_BUDGET = 30.0
BACKOFF_FIRST = 0.25
BACKOFF_MAX = 2.0
IDEMPOTENCY_HEADER = "Idempotency-Key"
OUT_OF_SCOPE = "out_of_scope"

Outcome = Literal["ok", "refused", "route_missing", "unavailable", "transport", "reexchange"]


class CrudUnavailableError(Exception):
    """The route is missing, answers 5xx or cannot be reached, past the budget. Retryable."""


class CrudRefusedError(Exception):
    """The API refused the call with a 4xx problem of a CRUD route."""

    def __init__(self, status: int, problem: dict[str, Any]) -> None:
        super().__init__(f"{status}: {problem.get('detail', '')}")
        self.status = status
        self.problem = problem


def activity_key(ordinal: int) -> str:
    """The key of the ``ordinal``-th write of the running activity: the same on every
    retry of the activity (D5), so the API answers a repeated write once."""
    info = activity.info()
    material = f"{info.workflow_id}\0{info.activity_id}\0{ordinal}"
    return hashlib.sha256(material.encode()).hexdigest()


def _area(path: str) -> str:
    _, found, rest = path.partition(f"{PREFIX}/")
    parts = rest.split("/")
    return parts[1] if found and len(parts) > 1 and parts[1] else "other"


def _problem(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class _Call:
    """One request's retry state, shared by the async and the sync client."""

    def __init__(self, owner: _Base, path: str) -> None:
        self._owner = owner
        self._area = _area(path)
        self._deadline = owner._clock() + owner._budget
        self._attempt = 0
        self._reexchanged = False
        self.refresh = False
        self.last = "no attempt"

    def observe(self, outcome: Outcome, started: float) -> None:
        metrics = self._owner._metrics
        if metrics is not None:
            metrics.crud_client_seconds.labels(self._owner._caller, self._area, outcome).observe(
                time.perf_counter() - started
            )

    def judge(self, response: httpx.Response, started: float) -> Literal["done", "retry", "again"]:
        """``done``: return it; ``again``: send at once; ``retry``: after a backoff on a
        new connection. A refusal raises."""
        status = response.status_code
        if CRUD_HEADER not in response.headers:
            self.last = f"{status} without {CRUD_HEADER}: the route is missing"
            self.observe("route_missing", started)
            return "retry"
        if status < 400:
            self.observe("ok", started)
            return "done"
        if status >= 500:
            self.last = f"{status} from the API"
            self.observe("unavailable", started)
            return "retry"
        problem = _problem(response)
        stale = status == 401 or (status == 403 and problem.get("reason") == OUT_OF_SCOPE)
        if stale and not self._reexchanged:
            self._reexchanged = True
            self.refresh = status == 403
            self.observe("reexchange", started)
            return "again"
        self.observe("refused", started)
        raise CrudRefusedError(status, problem)

    def failed(self, error: Exception, started: float) -> None:
        self.last = f"{type(error).__name__}: {error}"
        self.observe("transport", started)

    def delay(self) -> float:
        """The wait before the next try; raises once the budget is spent."""
        remaining = self._deadline - self._owner._clock()
        if remaining <= 0:
            raise CrudUnavailableError(f"CRUD route unavailable: {self.last}")
        wait = float(min(self._owner._backoff * 2**self._attempt, BACKOFF_MAX, remaining))
        self._attempt += 1
        return wait


class _Base:
    def __init__(
        self,
        base_url: str,
        caller: CrudCaller,
        auth: ServiceAuth,
        *,
        timeout: float = 10.0,
        metrics: Metrics | None = None,
        unavailable_budget: float = UNAVAILABLE_BUDGET,
        backoff: float = BACKOFF_FIRST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._caller = caller
        self._auth = auth
        self._timeout = timeout
        self._metrics = metrics
        self._budget = unavailable_budget
        self._backoff = backoff
        self._clock = clock

    @staticmethod
    def _kwargs(json: Any, key: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if key is not None:
            kwargs["headers"] = {IDEMPOTENCY_HEADER: key}
        return kwargs


class CrudClient(_Base):
    def __init__(
        self,
        base_url: str,
        caller: CrudCaller,
        auth: ServiceAuth,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        **options: Any,
    ) -> None:
        super().__init__(base_url, caller, auth, **options)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                auth=self._auth,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._http

    async def aclose(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    async def request(
        self, method: str, path: str, *, json: Any = None, idempotency_key: str | None = None
    ) -> httpx.Response:
        call = _Call(self, path)
        kwargs = self._kwargs(json, idempotency_key)
        while True:
            started = time.perf_counter()
            try:
                response = await self._client().request(method, path, **kwargs)
            except httpx.TransportError as error:
                call.failed(error, started)
                await self.aclose()
                await asyncio.sleep(call.delay())
                continue
            action = call.judge(response, started)
            if action == "done":
                return response
            if call.refresh:
                call.refresh = False
                self._auth.refresh(response.request)
            if action == "retry":
                await self.aclose()
                await asyncio.sleep(call.delay())


class CrudSyncClient(_Base):
    """The same, blocking: for a store called through ``asyncio.to_thread``."""

    def __init__(
        self,
        base_url: str,
        caller: CrudCaller,
        auth: ServiceAuth,
        *,
        transport: httpx.BaseTransport | None = None,
        **options: Any,
    ) -> None:
        super().__init__(base_url, caller, auth, **options)
        self._transport = transport
        self._http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self._base_url,
                auth=self._auth,
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._http

    def close(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            http.close()

    def request(
        self, method: str, path: str, *, json: Any = None, idempotency_key: str | None = None
    ) -> httpx.Response:
        call = _Call(self, path)
        kwargs = self._kwargs(json, idempotency_key)
        while True:
            started = time.perf_counter()
            try:
                response = self._client().request(method, path, **kwargs)
            except httpx.TransportError as error:
                call.failed(error, started)
                self.close()
                time.sleep(call.delay())
                continue
            action = call.judge(response, started)
            if action == "done":
                return response
            if call.refresh:
                call.refresh = False
                self._auth.refresh(response.request)
            if action == "retry":
                self.close()
                time.sleep(call.delay())
