"""Route classes and what each takes (#4975 phase 3, spec 2026-11-01 §5.3, §8.1).

Every mounted route falls in exactly one class, by its endpoint: the public ones, the
person-only ones, the service-token exchange, the flow host routes and the internal
modules are named here, and every other ``/api/v1`` route is ``api``.
``tests/auth/test_routes.py`` fails for a name here that matches no route, and for an
endpoint that refuses agents (``_browser_only``) that is not ``person``.

:func:`would_refuse` says why ``enforce`` refuses a request, or ``None``.
``auth/check.py`` refuses it in ``enforce`` and counts it in ``observe``.
"""

from __future__ import annotations

import re
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
    # The CRUD routes (#5298): one class per caller, `/internal/crud/<caller>/…`.
    "crud.projects",
    "crud.render",
    "crud.print",
    "crud.storefront",
    "crud.agent",
    # The projects worker's flow host routes (#5298): `api` routes it may call too.
    "flow.host",
]
Reason = Literal[
    "signed_out",
    "not_a_person",
    "no_credential",
    "out_of_scope",
    # Another caller's CRUD routes (#5298): no fresh credential clears it.
    "wrong_caller",
    "no_request_header",
    # The author headers' refusals (#4975 phase 5, `core/authorship.py` `AuthorRefusal`).
    "author_header",
    "wrong_author",
]

#: What a ``read`` access token may send (RFC 9110 §9.2.1's safe methods).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: The header every cookie-authenticated request carries (§5.3 layer 3).
REQUEST_HEADER = "x-scadbuddy-request"

#: Anyone: probes, Prometheus, the routes that sign a person in, and a payment webhook.
PUBLIC_MODULES = frozenset({"scadbuddy.api.health", "scadbuddy.api.metrics"})
PUBLIC_ENDPOINTS = frozenset(
    {
        "scadbuddy.api.auth.get_session",
        "scadbuddy.api.auth.login",
        "scadbuddy.api.auth.callback",
        "scadbuddy.api.auth.break_glass",
        # A delegation link's sign-in (#4975 phase 6): the one-time code is the credential.
        "scadbuddy.api.auth.delegated_sign_in",
        # A payment provider's event (#4957): its signature is the credential.
        "scadbuddy.api.payment_webhooks.receive_webhook",
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
        # The shop's payment provider accounts (#4957): settings writes, never an agent's.
        "scadbuddy.api.payments.connect_provider",
        "scadbuddy.api.payments.disconnect_provider",
        "scadbuddy.api.payments.default_provider",
        # The shop's bank account through Plaid (#4961): keys, link and unlink.
        "scadbuddy.api.bank_link.put_bank_keys",
        "scadbuddy.api.bank_link.delete_bank_keys",
        "scadbuddy.api.bank_link.create_bank_link_token",
        "scadbuddy.api.bank_link.create_bank_link",
        "scadbuddy.api.bank_link.delete_bank_link",
        # The storefront's SMTP password (#5283): a settings write, never an agent's.
        "scadbuddy.api.storefront.put_mail_password",
        "scadbuddy.api.storefront.delete_mail_password",
    }
)
#: The public routes the projects worker's flow host functions and grant checks call
#: (#5298): `workflows/flow_tools.py` through `workflows/flow_routes.py`,
#: `workflows/flow_grants.py` `facts_of`, and `flows/print_binding.py` `api_lookup` for the
#: `flow_decide` operation. They take everyone `api` takes, as `api`; the projects
#: credential holds `flow.host`, not `api`, and reaches only these.
#: `tests/flows/test_flow_host_paths.py` fails for a call to a route not listed here,
#: and for a listed one nothing calls.
FLOW_HOST_ENDPOINTS = frozenset(
    {
        "scadbuddy.api.jobs.render_model",
        "scadbuddy.api.jobs.get_job",
        "scadbuddy.api.outputs.create_output",
        "scadbuddy.api.outputs.get_output",
        "scadbuddy.api.outputs.arrange_outputs",
        "scadbuddy.api.operations.get_operation",
        "scadbuddy.api.printing.post_run",
        "scadbuddy.api.library_print.post_library_run",
        "scadbuddy.api.printing.get_run",
        "scadbuddy.api.settings.get_print_options",
        "scadbuddy.api.printers.get_printer",
        # A line item's print and the read its grant check makes (#4252, orders §7.2).
        "scadbuddy.api.orders.get_order",
        "scadbuddy.api.orders.print_item",
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

#: A CRUD route's path (#5298): the class is its caller's, whatever the module.
CRUD_PATH = re.compile(r"(?:/api/v1)?/internal/crud/([a-z]+)(?:/|$)")
CRUD_CLASSES: dict[str, RouteClass] = {
    "projects": "crud.projects",
    "render": "crud.render",
    "print": "crud.print",
    "storefront": "crud.storefront",
    "agent": "crud.agent",
}
CRUD_CALLERS = frozenset(CRUD_CLASSES)

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
    if endpoint_name(route) in FLOW_HOST_ENDPOINTS:
        return "flow.host"
    if (match := CRUD_PATH.match(route.path)) and match.group(1) in CRUD_CALLERS:
        return CRUD_CLASSES[match.group(1)]
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
    if route_cls.startswith("crud."):
        # A live service credential of another caller, or without the class, is a 403
        # as `crud/guard.py` answers it in `off`; only no live credential is a 401.
        if not isinstance(caller, Service):
            return "no_credential"
        if route_cls.startswith("crud.") and route_cls != f"crud.{caller.caller}":
            return "wrong_caller"
        return None if route_cls in caller.scopes else "out_of_scope"
    # A flow host route is an `api` route, and also the projects credential's (#5298).
    if route_cls == "flow.host" and isinstance(caller, Service) and "flow.host" in caller.scopes:
        return None
    if isinstance(caller, Anonymous):
        return "signed_out"
    # A credential whose scopes leave out the public routes (print, render, projects).
    if (
        route_cls in ("api", "flow.host")
        and isinstance(caller, Service)
        and "api" not in caller.scopes
    ):
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
