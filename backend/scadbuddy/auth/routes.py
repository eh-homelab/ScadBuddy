"""Route classes and what each takes (#4975 phase 3, spec 2026-11-01 §5.3, §8.1).

Every mounted route falls in exactly one class, by its endpoint: the public ones, the
person-only ones and the internal modules are named here, and every other ``/api/v1``
route is ``api``. ``tests/auth/test_routes.py`` fails for a name here that matches no
route, and for an endpoint that refuses agents (``_browser_only``) that is not
``person``.

:func:`would_refuse` says why ``enforce`` would refuse a request, or ``None``. In
``observe`` that is only counted (``auth/check.py``); nothing here refuses.
"""

from __future__ import annotations

from typing import Literal

from fastapi.routing import APIRoute, APIWebSocketRoute

from scadbuddy.auth.models import Anonymous, Caller, Person, Service

RouteClass = Literal[
    "public",
    "api",
    "person",
    "internal.outputs",
    "internal.library",
    "internal.workspace",
    "transfer",
]
Reason = Literal["signed_out", "not_a_person", "no_credential", "no_request_header"]

#: The header every cookie-authenticated request carries (§5.3 layer 3).
REQUEST_HEADER = "x-scadbuddy-request"

#: Anyone: probes, Prometheus, and the routes that sign a person in.
PUBLIC_MODULES = frozenset({"scadbuddy.api.health", "scadbuddy.api.metrics"})
PUBLIC_ENDPOINTS = frozenset(
    {
        "scadbuddy.api.auth.get_session",
        "scadbuddy.api.auth.login",
        "scadbuddy.api.auth.callback",
        "scadbuddy.api.auth.break_glass",
    }
)
#: A person's own session only (§8.1): what an agent must never do for them.
PERSON_ENDPOINTS = frozenset(
    {
        # `_browser_only` today (`api/flows.py`): a flow's answers, approvals and grants.
        "scadbuddy.api.flows.answer_flow_run",
        "scadbuddy.api.flows.decide_flow_run",
        "scadbuddy.api.flows.get_flow_run_proposal",
        "scadbuddy.api.flows.approve_flow_grant",
        "scadbuddy.api.flows.decline_flow_grant",
        "scadbuddy.api.flow_triggers.approve_flow_trigger_grant",
        "scadbuddy.api.flow_triggers.decline_flow_trigger_grant",
        # Sign-out, the person's sessions and the login's configuration.
        "scadbuddy.api.auth.logout",
        "scadbuddy.api.auth.list_sessions",
        "scadbuddy.api.auth.revoke_session",
        "scadbuddy.api.auth.revoke_all_sessions",
        "scadbuddy.api.auth.get_login_config",
        "scadbuddy.api.auth.put_login_config",
        "scadbuddy.api.auth.test_login_config",
    }
)
#: The hidden internal routes, each the scope of one service credential (§7.1).
INTERNAL_MODULES: dict[str, RouteClass] = {
    "scadbuddy.api.internal": "internal.outputs",
    "scadbuddy.api.internal_library": "internal.library",
    "scadbuddy.api.internal_workspace": "internal.workspace",
    # The slicer's transfer token is that route's credential (C6), unchanged.
    "scadbuddy.api.internal_blobs": "transfer",
}
#: §5.3 (D11): GETs the browser makes without script (an ``<img>``, a ``<video>``, a
#: download link, a template UI's ``import()``), which can carry no header. They change
#: nothing, and pass on the cookie alone. Keyed by module and route path.
SCRIPTLESS_GETS = frozenset(
    {
        ("scadbuddy.api.models", "/models/{slug}/thumbnail"),
        ("scadbuddy.api.media", "/models/{slug}/media/{item_id}"),
        ("scadbuddy.api.media", "/models/{slug}/media/{item_id}/poster"),
        ("scadbuddy.api.media", "/models/{slug}/media/{item_id}/thumbnail"),
        ("scadbuddy.api.lsp", "/models/{slug}/images/{path:path}"),
        ("scadbuddy.api.assets", "/models/{slug}/assets/{asset_id}/content"),
        ("scadbuddy.api.assets", "/models/{slug}/samples/{name}"),
        ("scadbuddy.api.template_ui", "/models/{slug}/ui/{path:path}"),
        ("scadbuddy.api.template_ui", "/models/{slug}/versions/{commit}/ui/{path:path}"),
        ("scadbuddy.api.outputs", "/outputs/{output_id}/thumbnail"),
        ("scadbuddy.api.outputs", "/outputs/{output_id}/plates/{index}/thumbnail"),
        ("scadbuddy.api.outputs", "/outputs/{output_id}/model.3mf"),
        ("scadbuddy.api.outputs", "/outputs/{output_id}/files/{name}"),
        ("scadbuddy.api.library_print", "/print/library/{file_id}/thumbnail"),
        ("scadbuddy.api.library_print", "/print/library/{file_id}/plates/{index}/thumbnail"),
        ("scadbuddy.api.library_print", "/print/library/{file_id}/file"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/thumbnail"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/plates/{index}/thumbnail"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/photos/{filename}"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/timelapse"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/files/sliced"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/files/source"),
        ("scadbuddy.api.prints", "/prints/{archive_id}/attachments/{attachment_id}/file"),
        ("scadbuddy.api.print_history", "/prints/{archive_id}/timelapse/frames/{index}"),
        ("scadbuddy.api.printing", "/print/printers/{printer_id}/camera"),
        ("scadbuddy.api.printing", "/print/printers/{printer_id}/camera/stream"),
        (
            "scadbuddy.api.order_attachments",
            "/orders/{order_id}/attachments/{attachment_id}/content",
        ),
        (
            "scadbuddy.api.order_attachments",
            "/orders/{order_id}/attachments/{attachment_id}/thumbnail",
        ),
        ("scadbuddy.api.flows", "/workflow-runs/{run_id}/images/{name}"),
    }
)

Route = APIRoute | APIWebSocketRoute


def endpoint_name(route: Route) -> str:
    return f"{route.endpoint.__module__}.{route.endpoint.__qualname__}"


def route_class(route: Route) -> RouteClass:
    module = route.endpoint.__module__
    if module in PUBLIC_MODULES or endpoint_name(route) in PUBLIC_ENDPOINTS:
        return "public"
    if endpoint_name(route) in PERSON_ENDPOINTS:
        return "person"
    return INTERNAL_MODULES.get(module, "api")


def scriptless(route: Route) -> bool:
    return (
        isinstance(route, APIRoute)
        and "GET" in (route.methods or ())
        and (route.endpoint.__module__, route.path) in SCRIPTLESS_GETS
    )


def would_refuse(
    route_cls: RouteClass,
    caller: Caller,
    *,
    request_header: bool,
    header_needed: bool,
) -> Reason | None:
    """Why ``enforce`` would refuse this request, or ``None`` (§8.1, §5.3).
    ``header_needed`` is false for a socket and for a scriptless GET."""
    if route_cls in ("public", "transfer"):
        return None
    if route_cls.startswith("internal."):
        if isinstance(caller, Service) and route_cls in caller.scopes:
            return None
        return "no_credential"
    if isinstance(caller, Anonymous):
        return "signed_out"
    if route_cls == "person" and not (
        isinstance(caller, Person) and caller.session.kind in ("login", "break_glass")
    ):
        return "not_a_person"
    if isinstance(caller, Person) and header_needed and not request_header:
        return "no_request_header"
    return None
