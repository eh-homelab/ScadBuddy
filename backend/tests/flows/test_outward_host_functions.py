"""A flow's `render`, `save_output`, `queue_print` and `arrange` (#1057, plan
2026-10-09-durable-phase-6-flows.md Task C3, Ruling 5): each calls ScadBuddy's own
route, here a fake of the routes' shapes; `queue_print` and `arrange` wait for a person's
approval first, and are denied when the run's approval timeout passes."""

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig

from scadbuddy.flows.store import FlowStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_activities import FlowActivities
from scadbuddy.workflows.flow_routes import FlowRoutes, route_key
from scadbuddy.workflows.flow_tools import outward_activity_id
from scadbuddy.workflows.flows_client import connect_flows
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow
from scadbuddy.workflows.projects_worker import projects_worker
from tests.flows.flows_support import Flows, Keys, script
from tests.flows.test_project_replay import HISTORIES

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

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


async def _worker(
    temporal_address: str, jobs: JobProjection, api: httpx.AsyncClient | None
) -> AsyncIterator[Outward]:
    client = await connect_flows(temporal_address, "default", Keys())
    store = FlowStore(jobs.pool)
    queue = f"projects-{uuid.uuid4().hex[:8]}"
    routes = FlowRoutes(api)
    async with projects_worker(client, queue, [*FlowActivities(store).all(), *routes.all()]):
        yield Outward(client, queue, store)


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
async def outward(
    temporal_address: str, jobs: JobProjection, api: FakeApi
) -> AsyncIterator[Outward]:
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        async for flows in _worker(temporal_address, jobs, client):
            yield flows


async def test_render_follows_its_job_until_it_settles(outward: Outward, api: FakeApi) -> None:
    run_id = await outward.run(script("r = await render('box', {'width': 20})", "return r"))
    run = await outward.finished(run_id)
    assert run.status == "succeeded", run.result
    assert "job-box" in (run.result or "") and "'done'" in (run.result or "")
    [(body, key)] = api.to("/api/v1/models/box/render")
    assert body == {"params": {"width": 20}}
    assert key is not None and re.fullmatch(r"[0-9a-f]{32}", key)
    assert [(s.fn, s.outward, s.status) for s in run.steps] == [("render", False, "succeeded")]


async def test_a_refusal_reaches_the_script_in_the_routes_words(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "try:",
            "    await render('missing', {})",
            "except Exception as e:",
            "    return str(e)",
        )
    )
    run = await outward.finished(run_id)
    assert "no model 'missing'" in (run.result or "")
    assert [(s.fn, s.status, s.error) for s in run.steps] == [
        ("render", "failed", "RouteRefusedError")
    ]


async def test_save_output_follows_an_operation(outward: Outward) -> None:
    run_id = await outward.run(
        script(
            "a = await save_output('box', 'j1', 'A')",
            "b = await save_output('slow', 'j2', 'B')",
            "return [a, b]",
        )
    )
    run = await outward.finished(run_id)
    assert run.result == "result: ['out-j1', 'o-9']"


async def test_print_waits_for_approval_then_runs_once_under_its_key(
    outward: Outward, api: FakeApi
) -> None:
    api.accepting = 1
    run_id = await outward.run(
        script(f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})", "return o['status']")
    )
    call_id = await outward.approval(run_id)
    assert api.sent == []
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"
    assert run.waiting_on == []
    sends = api.to("/api/v1/print/outputs/o1/run")
    # Re-sent once past `command-still-accepting`, with the same key and request id.
    assert len(sends) == 2 and sends[0] == sends[1]
    body, key = sends[0]
    expected = route_key(run_id, run.workflow_run_id, call_id)
    assert (body["request_id"], key) == (expected, expected)
    assert list(api.runs) == [f"run-{expected}"]
    history = await outward.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_id
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    # The one thing a Reset preview can read of an outward send (Ruling 14).
    assert scheduled.count(outward_activity_id("queue_print", call_id)) == 1
    assert [(s.fn, s.outward, s.status) for s in run.steps] == [("queue_print", True, "succeeded")]


async def test_a_print_refusal_fails_its_step(outward: Outward) -> None:
    run_id = await outward.run(
        script(
            "try:",
            "    await queue_print({'output_id': 'o1'}, {'choices': {}})",
            "except Exception as e:",
            "    return str(e)",
        )
    )
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert "filament_plan: Field required" in (run.result or "")
    assert [(s.fn, s.status) for s in run.steps] == [("queue_print", "failed")]


async def test_a_denied_print_sends_nothing(outward: Outward, api: FakeApi) -> None:
    run_id = await outward.run(
        script(
            "try:",
            f"    await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "except Exception as e:",
            "    return str(e)",
        )
    )
    await outward.decide(run_id, approved=False)
    run = await outward.finished(run_id)
    assert (run.result or "").removeprefix("result: ").strip("'\"").startswith("ToolApprovalDenied")
    assert api.sent == []
    assert run.waiting_on == []


async def test_an_undecided_print_is_denied_at_the_runs_timeout(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "try:",
            f"    await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "except Exception as e:",
            "    return str(e)",
        ),
        approval_timeout_s=2,
    )
    await outward.approval(run_id)
    run = await outward.finished(run_id)
    assert "timed out" in (run.result or "")
    assert api.sent == []
    assert run.waiting_on == []
    status = await AgentClient(outward.client, f"flow-{run_id}").get_status()
    assert status.pending_approvals == []


async def test_an_approval_inside_the_timeout_is_not_undone(outward: Outward, api: FakeApi) -> None:
    run_id = await outward.run(
        script(
            f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "await sleep(3)",
            "return o['status']",
        ),
        approval_timeout_s=2,
    )
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"


async def test_arrange_waits_for_approval_and_saves_the_result(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "a = await arrange({'objects': [{'output_id': 'o1'}], 'name': 'Plates'})",
            "return [a['status'], a['output_id']]",
        )
    )
    call_id = await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: ['done', 'out-arr-1']"
    [(saved, save_key)] = api.to("/api/v1/models/box/outputs")
    assert saved == {"job_id": "arr-1", "name": "Plates"}
    [(_, arrange_key)] = api.to("/api/v1/outputs/arrange")
    assert arrange_key == route_key(run_id, run.workflow_run_id, call_id)
    assert save_key == route_key(run_id, run.workflow_run_id, call_id + ":save")


async def test_without_an_api_url_the_call_names_the_setting(
    temporal_address: str, jobs: JobProjection
) -> None:
    async for flows in _worker(temporal_address, jobs, None):
        run_id = await flows.run(
            script(
                "try:", "    await render('box', {})", "except Exception as e:", "    return str(e)"
            )
        )
        run = await flows.finished(run_id)
        assert "SCADBUDDY_API_INTERNAL_URL" in (run.result or "")


async def test_printed(outward: Outward, api: FakeApi) -> None:
    """Records `project_workflow_histories/printed.json` (test_project_replay.py): a
    render, its output saved, and a print approved inside its approval timeout."""
    run_id = await outward.run(
        script(
            "r = await render('box', {'width': 20})",
            "o = await save_output('box', r['job_id'], 'Box')",
            "p = await queue_print({'output_id': o}, " + repr(PLAN) + ")",
            "return p['status']",
        ),
        approval_timeout_s=600,
    )
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"
    if os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1":
        handle = outward.client.get_workflow_handle(f"flow-{run_id}")
        (HISTORIES / "printed.json").write_text((await handle.fetch_history()).to_json())
