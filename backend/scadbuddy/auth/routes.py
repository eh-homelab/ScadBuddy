"""Route classes and what each takes (#4975 phase 3, spec 2026-11-01 §5.3, §8.1).

Every mounted route falls in exactly one class, by its endpoint: the public ones, the
person-only ones, the service-token exchange and the internal modules are named here,
and every other ``/api/v1`` route is ``api``. ``tests/auth/test_routes.py`` fails for a
name here that matches no route, and for an endpoint that refuses agents
(``_browser_only``) that is not ``person``.

:func:`would_refuse` says why ``enforce`` refuses a request, or ``None``.
``auth/check.py`` refuses it in ``enforce`` and counts it in ``observe``.
"""

from __future__ import annotations

from typing import Literal

from fastapi.routing import APIRoute, APIWebSocketRoute

from scadbuddy.auth.models import Anonymous, Caller, Person, PersonalToken, Service

RouteClass = Literal[
    "public",
    "api",
    "person",
    "internal.outputs",
    "internal.library",
    "internal.workspace",
    "transfer",
    "exchange",
]
Reason = Literal[
    "signed_out",
    "not_a_person",
    "no_credential",
    "out_of_scope",
    "no_request_header",
    # The author headers' refusals (#4975 phase 5, `core/authorship.py` `AuthorRefusal`).
    "author_header",
    "wrong_author",
]

#: What a ``read`` access token may send (RFC 9110 §9.2.1's safe methods).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
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
        # A delegation link's sign-in (#4975 phase 6): the one-time code is the credential.
        "scadbuddy.api.auth.delegated_sign_in",
    }
)
#: The service-token exchange (#5494, spec 2026-11-05 §6.2): the proof in its body is
#: its credential, so it takes no cookie and no service credential.
EXCHANGE_ENDPOINTS = frozenset({"scadbuddy.api.service_token.exchange_service_token"})
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
        # A person's access tokens (#4975 phase 6): minted, listed and revoked by them.
        "scadbuddy.api.access_tokens.list_access_tokens",
        "scadbuddy.api.access_tokens.create_access_token",
        "scadbuddy.api.access_tokens.revoke_access_token",
        # The services' credentials (#4975 phase 4).
        "scadbuddy.api.service_credentials.list_service_credentials",
        "scadbuddy.api.service_credentials.rotate_service_credential",
        "scadbuddy.api.service_credentials.revoke_service_credential",
        # Revocation by pod and suspension of a caller (#5494, spec 2026-11-05 §6.5).
        "scadbuddy.api.service_credentials.revoke_pod_service_credentials",
        "scadbuddy.api.service_credentials.list_service_callers",
        "scadbuddy.api.service_credentials.suspend_service_caller",
        "scadbuddy.api.service_credentials.resume_service_caller",
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
    if endpoint_name(route) in EXCHANGE_ENDPOINTS:
        return "exchange"
    if endpoint_name(route) in PERSON_ENDPOINTS:
        return "person"
    return INTERNAL_MODULES.get(module, "api")


#: The prefix every listed path sits under once mounted (`main.API_PREFIX`).
API_PREFIX = "/api/v1"


def scriptless(route: Route) -> bool:
    """Whether ``route`` is a listed GET. The matched route's path is its own router's
    (FastAPI >= 0.140 keeps included routers nested, as `core/metrics.py` notes), but a
    path with the prefix is taken too, so a FastAPI that flattens again changes nothing."""
    if not isinstance(route, APIRoute) or "GET" not in (route.methods or ()):
        return False
    path = (
        route.path.removeprefix(API_PREFIX)
        if route.path.startswith(API_PREFIX + "/")
        else route.path
    )
    return (route.endpoint.__module__, path) in SCRIPTLESS_GETS


def would_refuse(
    route_cls: RouteClass,
    caller: Caller,
    *,
    request_header: bool,
    header_needed: bool,
    method: str = "GET",
) -> Reason | None:
    """Why ``enforce`` would refuse this request, or ``None`` (§8.1, §5.3).
    ``header_needed`` is false for a socket and for a scriptless GET."""
    if route_cls in ("public", "transfer", "exchange"):
        return None
    if route_cls.startswith("internal."):
        if isinstance(caller, Service) and route_cls in caller.scopes:
            return None
        return "no_credential"
    if isinstance(caller, Anonymous):
        return "signed_out"
    # A credential whose scopes leave out the public routes (the print worker's).
    if route_cls == "api" and isinstance(caller, Service) and "api" not in caller.scopes:
        return "out_of_scope"
    # A person's access token (§7.5): the api routes only, and writes with `write` only.
    if isinstance(caller, PersonalToken):
        if route_cls == "person":
            return "not_a_person"
        if caller.scope != "write" and method.upper() not in SAFE_METHODS:
            return "out_of_scope"
        return None
    if route_cls == "person" and not (
        isinstance(caller, Person) and caller.session.kind in ("login", "break_glass")
    ):
        return "not_a_person"
    if isinstance(caller, Person) and header_needed and not request_header:
        return "no_request_header"
    return None
