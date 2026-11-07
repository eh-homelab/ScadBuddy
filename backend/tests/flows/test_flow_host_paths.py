"""The routes the projects worker calls are exactly `auth/routes.py` `FLOW_HOST_ENDPOINTS`
(#5298): its credential holds `flow.host`, not `api`, so a call to any other public route
is refused in `enforce`.

The flow host functions (`workflows/flow_tools.py`) run here with their activities
(`workflows/flow_routes.py`) on a transport that logs each request; so do the grant
checks' reads (`workflows/flow_grants.py` `facts_of`) and the printer read that
`flow_decide` (`flows/operations.py`) makes through `flows/print_binding.py` `bind_call`.
"""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.routing import Match
from temporalio.testing import ActivityEnvironment

import scadbuddy.flows.operations
import scadbuddy.flows.print_binding
import scadbuddy.workflows.flow_grants
import scadbuddy.workflows.flow_tools
from scadbuddy.auth.routes import API_PREFIX, FLOW_HOST_ENDPOINTS, Route, endpoint_name
from scadbuddy.core.settings import Settings
from scadbuddy.flows.print_binding import PRINT_OUTPUT, api_lookup, bind_call
from scadbuddy.main import create_app
from scadbuddy.workflows import flow_tools
from scadbuddy.workflows.flow_grants import FlowGrants
from scadbuddy.workflows.flow_models import DecisionRead
from scadbuddy.workflows.flow_routes import (
    FLOW_ROUTE_FOLLOW,
    FLOW_ROUTE_SEND,
    FlowRoutes,
    RouteFollow,
    RouteSend,
)
from tests.auth.test_routes import _mounted
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

PRINTER = "6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab"
#: What the logging transport answers, by method and path.
ANSWERS: list[tuple[str, re.Pattern[str], int, dict[str, Any]]] = [
    ("POST", re.compile(r"/api/v1/models/[^/]+/render"), 202, {"job_id": "job1"}),
    ("GET", re.compile(r"/api/v1/jobs/[^/]+"), 200, {"id": "job1", "status": "done", "slug": "s"}),
    # A 202: the save is followed through its operation.
    ("POST", re.compile(r"/api/v1/models/[^/]+/outputs"), 202, {"id": "op1"}),
    (
        "GET",
        re.compile(r"/api/v1/operations/[^/]+"),
        200,
        {"status": "succeeded", "result": {"id": "o1"}},
    ),
    (
        "POST",
        re.compile(r"/api/v1/print/(outputs|library)/[^/]+/run"),
        202,
        {"id": "r1", "status": "queued"},
    ),
    ("GET", re.compile(r"/api/v1/print/runs/[^/]+"), 200, {"id": "r1", "status": "succeeded"}),
    ("POST", re.compile(r"/api/v1/outputs/arrange"), 202, {"id": "job1"}),
    ("GET", re.compile(r"/api/v1/outputs/[^/]+"), 200, {"slug": "s", "layout_plates": [{}]}),
    ("GET", re.compile(r"/api/v1/settings/print-options"), 200, {"printer_id": 1}),
    # A line item's print (#4252): the order read for its grant check, then the print.
    (
        "GET",
        re.compile(r"/api/v1/orders/[^/]+"),
        200,
        {"items": [{"id": "i1", "quantity": 1, "reference": {"slug": "s"}, "output_id": "o1"}]},
    ),
    (
        "POST",
        re.compile(r"/api/v1/orders/[^/]+/items/[^/]+/print"),
        200,
        {"print_run_id": "r1"},
    ),
    # Not in the registry: the read is made, and the call is bound with no printer.
    ("GET", re.compile(r"/api/v1/printers/[^/]+"), 404, {"detail": "no such printer"}),
]


class Logged:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        for method, path, status, body in ANSWERS:
            if request.method == method and path.fullmatch(request.url.path):
                return httpx.Response(status, json=body)
        return httpx.Response(599, json={"detail": f"unexpected {request.url.path}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(self.handle))


@pytest.fixture
def logged() -> Logged:
    return Logged()


@pytest.fixture
def host(logged: Logged, monkeypatch: pytest.MonkeyPatch) -> None:
    """`flow_tools` as a workflow runs it, its route activities run on `logged`."""
    routes = FlowRoutes(logged.client())
    activities: dict[str, Callable[[Any], Awaitable[Any]]] = {
        FLOW_ROUTE_SEND: routes.send,
        FLOW_ROUTE_FOLLOW: routes.follow,
    }

    async def execute_activity(name: str, arg: RouteSend | RouteFollow, **_: Any) -> Any:
        if isinstance(arg, RouteFollow):
            arg = arg.model_copy(update={"interval_s": 0.0})
        return await ActivityEnvironment().run(activities[name], arg)

    owner = SimpleNamespace(run_id="run1", route_author=None, mark_started=lambda _: None)
    monkeypatch.setattr(
        flow_tools,
        "workflow",
        SimpleNamespace(
            execute_activity=execute_activity,
            instance=lambda: owner,
            info=lambda: SimpleNamespace(run_id="wr1"),
            uuid4=uuid.uuid4,
        ),
    )

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    async def undecided(*_: Any, **__: Any) -> DecisionRead:
        return DecisionRead()

    monkeypatch.setattr(flow_tools, "project", nothing)
    monkeypatch.setattr(flow_tools, "_decided", undecided)


def _body(tool: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    """A workflow tool's own function: the harness's wrapper runs only in a turn."""
    body: Callable[..., Awaitable[Any]] = inspect.getclosurevars(tool).nonlocals["user_fn"]
    return body


async def _every_call(logged: Logged) -> None:
    await flow_tools._render("c1", "s", {})
    await flow_tools._save("c2", "s", "job1", "n")
    for source in (
        {"output_id": "o1"},
        {"file_id": 3},
        {"order_id": "ord1", "order_item_id": "i1"},
    ):
        await _body(flow_tools.approved_print)("c3", flow_tools._print_path(source), {})
    await _body(flow_tools.approved_arrange)("c4", {"objects": [{"output_id": "o1"}]})
    client = logged.client()
    grants = FlowGrants(None, client, b"k" * 32)  # type: ignore[arg-type]
    await grants.facts_of("arrange", None, {"request": {"objects": [{"output_id": "o1"}]}})
    for source in (
        {"output_id": "o1"},
        {"file_id": 3},
        {"order_id": "ord1", "order_item_id": "i1"},
    ):
        await grants.facts_of("queue_print", None, {"source": source, "request": {}})
    # `flow_decide`'s binding of a print on a registry printer (#4445).
    await bind_call(PRINT_OUTPUT, {"printer": PRINTER}, api_lookup(client))


def _app_routes() -> list[Route]:
    app = create_app(
        Settings(
            frontend_dir=Path("/nonexistent"),
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        )
    )
    return _mounted(app.routes)


def _endpoint(routes: list[Route], method: str, path: str) -> str:
    """The endpoint the API serves ``method path`` with."""
    scope = {"type": "http", "method": method, "path": path.removeprefix(API_PREFIX)}
    found = [r for r in routes if r.matches(scope)[0] == Match.FULL]
    assert len(found) == 1, (method, path, [endpoint_name(r) for r in found])
    return endpoint_name(found[0])


@pytest.mark.usefixtures("host")
async def test_the_projects_worker_calls_exactly_the_flow_host_routes(logged: Logged) -> None:
    await _every_call(logged)
    routes = _app_routes()
    called = {_endpoint(routes, method, path) for method, path in logged.calls}
    assert called - FLOW_HOST_ENDPOINTS == set(), "called, not flow.host"
    assert FLOW_HOST_ENDPOINTS - called == set(), "flow.host, never called"


#: The modules whose code calls the API with the projects worker's credential.
CALLERS = (
    scadbuddy.workflows.flow_tools,
    scadbuddy.workflows.flow_grants,
    scadbuddy.flows.print_binding,
    scadbuddy.flows.operations,
)
#: A path literal in either quote; an f-string's `{...}` may hold the other quote.
_PATH = re.compile(r"""f?(["'])(/api/v1/(?:\{[^}]*\}|(?!\1)[^{])*)\1""")
#: The method each path literal in those modules is sent with, read from its call site:
#: the source does not say it beside the literal. A new literal fails until it is here.
METHODS = {
    "/api/v1/models/{_segment(slug, 'slug')}/render": "POST",
    "/api/v1/jobs/{_segment(accepted['job_id'], 'job id')}": "GET",
    "/api/v1/models/{_segment(slug, 'slug')}/outputs": "POST",
    "/api/v1/operations/{_segment(answer['id'], 'operation id')}": "GET",
    "/api/v1/print/library/{int(file_id)}/run": "POST",
    "/api/v1/print/outputs/{_segment(output_id, 'output id')}/run": "POST",
    "/api/v1/print/runs/{_segment(run['id'], 'run id')}": "GET",
    "/api/v1/outputs/arrange": "POST",
    "/api/v1/jobs/{_segment(accepted['id'], 'job id')}": "GET",
    "/api/v1/outputs/{_segment(obj['output_id'])}": "GET",
    "/api/v1/settings/print-options": "GET",
    "/api/v1/outputs/{_segment(source['output_id'])}": "GET",
    "/api/v1/printers/{printer}": "GET",
    "/api/v1/orders/{order}/items/{_segment(item_id, 'item id')}/print": "POST",
    "/api/v1/operations/{_segment(attempt['id'], 'operation id')}": "GET",
    "/api/v1/print/runs/{_segment(run_id, 'run id')}": "GET",
    "/api/v1/orders/{_segment(order_id)}": "GET",
    "/api/v1/outputs/{_segment(item['output_id'])}": "GET",
}


def test_every_path_those_modules_name_is_a_flow_host_route() -> None:
    """A path added beside these without a call above still has to be `flow.host`, for
    the method it is sent with."""
    routes = _app_routes()
    named: set[str] = set()
    for module in CALLERS:
        text = Path(module.__file__ or "").read_text()
        found = [m.group(2) for m in _PATH.finditer(text)]
        # Every literal that starts with the prefix is read whole; a path built from a
        # bare prefix fails the comparison below.
        assert len(found) == len(re.findall(r"""["']/api/v1/""", text)), module.__name__
        named.update(found)
    assert named == set(METHODS)
    for path, method in METHODS.items():
        # Each `{...}` is one segment; the library's is a number.
        concrete = re.sub(r"\{[^}]*\}", "1", path)
        assert _endpoint(routes, method, concrete) in FLOW_HOST_ENDPOINTS, (method, path)
