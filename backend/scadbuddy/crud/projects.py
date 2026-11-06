"""The projects worker's `FlowStore` over the CRUD routes (#5298): `RemoteFlowStore` has
`flows/store.py` `FlowStore`'s method names, signatures and answers, so its callers do
not change, and holds no database secret. The routes are `api/crud_projects.py`.

Every write it sends is natural (plan D5): the client may send it again after a lost
answer, and the route answers as it did the first time.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import quote

import httpx

from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.guard import PREFIX
from scadbuddy.flows.grants import ConsumeRequest, Grant, GrantUse
from scadbuddy.flows.models import Decision, Proposal, Run
from scadbuddy.flows.store import (
    RESET_CALL_S,
    HopMovedError,
    ResetBeforeFloorError,
    ResetSupersededError,
)
from scadbuddy.workflows.flow_models import FlowRecord, FlowWaiting, Hop, HopRecord, ProjectionWrite

BASE = f"/api/v1{PREFIX}/projects"
#: `api/crud_projects.py` `HOP_MOVED`, `RESET_SUPERSEDED`, `RESET_BEFORE_FLOOR`.
HOP_MOVED = "hop_moved"
RESET_SUPERSEDED = "reset-superseded"
RESET_BEFORE_FLOOR = "reset-before-floor"
#: A Reset's request: the route holds the run's lock for up to `RESET_CALL_S` while
#: Temporal answers, then writes the row.
RESET_REQUEST_S = RESET_CALL_S + 10.0


def _part(value: str) -> str:
    return quote(value, safe="")


class RemoteFlowStore:
    def __init__(self, client: CrudClient) -> None:
        self._client = client

    async def _send(
        self, method: str, path: str, *, json: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        return await self._client.request(method, f"{BASE}{path}", json=json, timeout=timeout)

    async def _found(self, path: str) -> Any:
        """The answer's body; None for a 404 of the route (the row is missing)."""
        try:
            response = await self._send("GET", path)
        except CrudRefusedError as error:
            if error.status == 404:
                return None
            raise
        return response.json()

    # -- runs -------------------------------------------------------------------

    async def get_run(self, run_id: str) -> Run | None:
        body = await self._found(f"/runs/{_part(run_id)}")
        return None if body is None else Run.model_validate(body)

    async def reset_floor(self, run_id: str) -> int | None:
        response = await self._send("GET", f"/runs/{_part(run_id)}/reset-floor")
        floor: int | None = response.json()["floor"]
        return floor

    async def insert_run(self, record: FlowRecord) -> Run:
        response = await self._send("POST", "/runs", json=record.model_dump(mode="json"))
        return Run.model_validate(response.json())

    async def project(self, write: ProjectionWrite) -> Run | None:
        response = await self._send(
            "POST", f"/runs/{_part(write.run_id)}/project", json=write.model_dump(mode="json")
        )
        body = response.json()
        return None if body is None else Run.model_validate(body)

    async def run_talking_to(self, session_id: str, call_id: str) -> Run | None:
        response = await self._send(
            "GET", f"/runs/talking-to?session_id={_part(session_id)}&call_id={_part(call_id)}"
        )
        body = response.json()
        return None if body is None else Run.model_validate(body)

    async def reset_run(
        self,
        run_id: str,
        *,
        replaced: str,
        point: int,
        request_id: str,
        reset: Callable[[], Coroutine[Any, Any, str]],
        responder: str | None = None,
    ) -> Run | None:
        """`FlowStore.reset_run`, but the API makes Temporal's Reset under the row's lock
        (D6), with what `reset` would send: `reset` itself is not called. One request
        waits out the Reset (`RESET_REQUEST_S`), rather than retrying while it holds the
        lock."""
        body = {
            "replaced": replaced,
            "point": point,
            "request_id": request_id,
            "responder": responder,
        }
        try:
            response = await self._send(
                "POST", f"/runs/{_part(run_id)}/reset", json=body, timeout=RESET_REQUEST_S
            )
        except CrudRefusedError as error:
            reason = error.problem.get("reason")
            if error.status == 409 and reason == RESET_SUPERSEDED:
                raise ResetSupersededError(Run.model_validate(error.problem["run"])) from error
            if error.status == 422 and reason == RESET_BEFORE_FLOOR:
                raise ResetBeforeFloorError(int(error.problem["floor"])) from error
            raise
        moved = response.json()
        return None if moved is None else Run.model_validate(moved)

    # -- decisions ---------------------------------------------------------------

    async def record_decision(self, decision: Decision, *, at_hop: Hop | None = None) -> bool:
        try:
            response = await self._send(
                "POST",
                "/decisions",
                json={"decision": decision.model_dump(mode="json"), "at_hop": at_hop},
            )
        except CrudRefusedError as error:
            if error.status == 409 and error.problem.get("reason") == HOP_MOVED:
                raise HopMovedError(decision.request_id) from error
            raise
        return bool(response.json()["recorded"])

    async def get_decision(self, request_id: str) -> Decision | None:
        body = await self._found(f"/decisions/{_part(request_id)}")
        return None if body is None else Decision.model_validate(body)

    async def delete_decision(self, request_id: str) -> None:
        await self._send("DELETE", f"/decisions/{_part(request_id)}")

    async def record_grant_decision(self, decision: Decision) -> bool:
        response = await self._send(
            "POST", "/decisions/grant", json=decision.model_dump(mode="json")
        )
        return bool(response.json()["recorded"])

    async def withdraw_grant_decision(self, request_id: str) -> None:
        await self._send("DELETE", f"/decisions/{_part(request_id)}/grant")

    # -- grants (#2481) -----------------------------------------------------------

    async def get_grant(self, run_id: str) -> Grant | None:
        response = await self._send("GET", f"/grants/{_part(run_id)}")
        body = response.json()
        return None if body is None else Grant.model_validate(body)

    async def grant_use(self, request_id: str) -> GrantUse | None:
        response = await self._send("GET", f"/grant-uses/{_part(request_id)}")
        body = response.json()
        return None if body is None else GrantUse.model_validate(body)

    async def consume(self, run_id: str, request: ConsumeRequest) -> GrantUse | str:
        """The API chooses the rule and spends it under the grant's row lock."""
        response = await self._send(
            "POST",
            f"/grants/{_part(run_id)}/consume",
            json=request.model_dump(mode="json", by_alias=True),
        )
        body = response.json()
        if "use" in body:
            return GrantUse.model_validate(body["use"])
        return str(body["outside"])

    # -- escalation (#2482) -------------------------------------------------------

    async def move_hop(
        self,
        run_id: str,
        call_id: str,
        record: HopRecord,
        *,
        proposal: Proposal | None = None,
    ) -> FlowWaiting | None:
        body = {
            "call_id": call_id,
            "record": record.model_dump(mode="json"),
            "proposal": None if proposal is None else proposal.model_dump(mode="json"),
        }
        response = await self._send("POST", f"/runs/{_part(run_id)}/hops", json=body)
        moved = response.json()
        return None if moved is None else FlowWaiting.model_validate(moved)

    async def get_proposal(self, request_id: str) -> Proposal | None:
        body = await self._found(f"/proposals/{_part(request_id)}")
        return None if body is None else Proposal.model_validate(body)

    async def parent_tiers(self, session: str, principal: str) -> list[str] | None:
        response = await self._send(
            "GET", f"/sessions/{_part(session)}/tiers?principal={_part(principal)}"
        )
        tiers: list[str] | None = response.json()["tiers"]
        return tiers

    async def approval_expiry_s(self) -> int:
        response = await self._send("GET", "/approval-expiry")
        return int(response.json()["seconds"])
