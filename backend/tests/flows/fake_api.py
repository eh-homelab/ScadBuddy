"""A fake of the routes a flow's `render`, `save_output`, `queue_print` and `arrange`
call (plan 2026-10-09-durable-phase-6-flows.md Ruling 5), and a `projects` worker whose
route activities reach it through `httpx.ASGITransport`."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig

from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flow_routes import FlowRoutes
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow
from scadbuddy.workflows.projects_worker import projects_worker
from tests.flows.flows_support import Flows, Keys

PROBLEM = "application/problem+json"
STILL_ACCEPTING = "https://scadbuddy.dev/problems/command-still-accepting"
PLAN: dict[str, Any] = {"filament_plan": {"slots": []}, "choices": {}}


def problem(status: int, detail: str, type_: str = "about:blank") -> JSONResponse:
    return JSONResponse(
        {"type": type_, "title": "x", "status": status, "detail": detail},
        status_code=status,
        media_type=PROBLEM,
    )


class FakeApi:
    """The routes' shapes, recording every command it was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.polls: dict[str, int] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        #: Answer the next print `command-still-accepting` this many times.
        self.accepting = 0
        #: Refuse every print, as a printer that is offline.
        self.refuse_prints = False
        self.app = Starlette(
            routes=[
                Route("/api/v1/models/{slug}/render", self.render, methods=["POST"]),
                Route("/api/v1/jobs/{job_id}", self.job),
                Route("/api/v1/models/{slug}/outputs", self.save, methods=["POST"]),
                Route("/api/v1/operations/{op}", self.operation),
                Route("/api/v1/print/outputs/{output_id}/run", self.print, methods=["POST"]),
                Route("/api/v1/print/runs/{run_id}", self.run),
                Route("/api/v1/outputs/arrange", self.arrange, methods=["POST"]),
            ]
        )

    async def _record(self, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = await request.json()
        self.sent.append((request.url.path, body, request.headers.get("Idempotency-Key")))
        return body

    async def render(self, request: Request) -> JSONResponse:
        await self._record(request)
        slug = request.path_params["slug"]
        if slug == "missing":
            return problem(404, "no model 'missing'")
        return JSONResponse({"job_id": f"job-{slug}", "status_url": "x"}, status_code=202)

    async def job(self, request: Request) -> JSONResponse:
        job_id = request.path_params["job_id"]
        self.polls[job_id] = self.polls.get(job_id, 0) + 1
        status = "running" if self.polls[job_id] < 2 else "done"
        return JSONResponse({"id": job_id, "slug": "box", "status": status, "error": None})

    async def save(self, request: Request) -> JSONResponse:
        body = await self._record(request)
        if request.path_params["slug"] == "slow":
            return JSONResponse(
                {"id": "op-1", "kind": "output_create", "subject": "slow", "status": "running"},
                status_code=202,
            )
        return JSONResponse({"id": f"out-{body['job_id']}"}, status_code=201)

    async def operation(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {"id": "op-1", "kind": "output_create", "status": "succeeded", "result": {"id": "o-9"}}
        )

    async def print(self, request: Request) -> JSONResponse:
        body = await self._record(request)
        if self.accepting > 0:
            self.accepting -= 1
            return problem(503, "still accepting", STILL_ACCEPTING)
        if self.refuse_prints:
            return problem(422, "the printer is offline")
        if "filament_plan" not in body:
            return problem(422, "filament_plan: Field required")
        run_id = f"run-{body['request_id']}"
        if run_id in self.runs:
            return JSONResponse({**self.runs[run_id], "repeated": True})
        self.runs[run_id] = {"id": run_id, "status": "running", "may_have_queued": False}
        return JSONResponse(self.runs[run_id], status_code=202)

    async def run(self, request: Request) -> JSONResponse:
        return JSONResponse({**self.runs[request.path_params["run_id"]], "status": "succeeded"})

    async def arrange(self, request: Request) -> JSONResponse:
        await self._record(request)
        return JSONResponse({"id": "arr-1", "slug": "box", "status": "pending"}, status_code=202)

    def to(self, path: str) -> list[tuple[dict[str, Any], str | None]]:
        return [(body, key) for p, body, key in self.sent if p == path]


class Outward(Flows):
    async def run(self, body: str, *, approval_timeout_s: int = 0) -> str:
        definition = await self.store.create_definition("t", body, {"kind": "browser"})
        run_id = str(uuid.uuid4())
        await AgentClient(self.client, f"flow-{run_id}").start_and_submit_message(
            "execute",
            RunFlow(script=body).model_dump(),
            workflow_name=PROJECT_WORKFLOW,
            task_queue=self.queue,
            start_config=AgentConfig(),
            start_data=FlowStart(
                run_id=run_id,
                definition_id=definition.id,
                version=1,
                name="t",
                started_by={"kind": "browser"},
                approval_timeout_s=approval_timeout_s,
            ),
        )
        return run_id

    async def approval(self, run_id: str) -> str:
        """The parked call's id, once the row and the harness both show it."""
        run = await self.row(run_id, lambda r: any(w.kind == "approval" for w in r.waiting_on))
        call_id = run.waiting_on[0].call_id
        client = AgentClient(self.client, f"flow-{run_id}")
        for _ in range(100):
            if any(p.tool_id == call_id for p in await client.get_pending_approvals()):
                return call_id
            await asyncio.sleep(0.1)
        raise AssertionError("the harness never parked the call")

    async def decide(self, run_id: str, approved: bool) -> str:
        call_id = await self.approval(run_id)
        await AgentClient(self.client, f"flow-{run_id}").approve_tool(call_id, approved=approved)
        return call_id


async def outward_worker(
    temporal_address: str, jobs: JobProjection, api: httpx.AsyncClient | None
) -> AsyncIterator[Outward]:
    client = await connect_flows(temporal_address, "default", Keys())
    store = FlowStore(jobs.pool)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    routes = FlowRoutes(api)
    async with projects_worker(client, queue, [*FlowActivities(store).all(), *routes.all()]):
        yield Outward(client, queue, store)
