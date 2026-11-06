"""The CRUD routers' guard (#5298, plan decisions D1, D2, D4).

A route under ``/api/v1/internal/crud/<caller>/`` is class ``crud.<caller>``
(`auth/routes.py`). ``require_crud`` refuses it unless the request carries a live service
credential of that caller whose scopes hold the class, in every ``SCADBUDDY_AUTH_MODE``:
these routes are new, so no client needs ``off`` or ``observe`` to let it through. A
missing or dead credential is 401 ``…/auth/signed-out``; any other is 403 ``…/auth/forbidden``.

Every response of a CRUD router, a refusal included, carries ``X-ScadBuddy-Crud: 1`` and
``Cache-Control: no-store``, so a client can tell an answer of these routes from a proxy's.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from scadbuddy.auth.check import FORBIDDEN, SIGNED_OUT
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import ApiError

#: The callers with CRUD routes; `auth/routes.py` `CRUD_CLASSES` has the same keys.
CrudCaller = Literal["projects", "render", "print"]
CRUD_HEADER = "X-ScadBuddy-Crud"
CRUD_HEADERS = {CRUD_HEADER: "1", "Cache-Control": "no-store"}
PREFIX = "/internal/crud"


def require_crud(caller: CrudCaller) -> Any:
    """A dependency: the request must be ``caller``'s service credential with ``crud.<caller>``."""
    route_class = f"crud.{caller}"

    async def check(request: Request) -> None:
        principal = caller_of(request.scope)
        if not isinstance(principal, Service):
            raise ApiError(
                401,
                "This route needs a service credential.",
                type_=SIGNED_OUT,
                headers={"WWW-Authenticate": 'Bearer realm="scadbuddy"'},
                reason="no_credential",
            )
        if principal.caller != caller:
            raise ApiError(
                403,
                "These routes belong to another service.",
                type_=FORBIDDEN,
                reason="wrong_caller",
            )
        if route_class not in principal.scopes:
            raise ApiError(
                403,
                "This credential's scope does not cover this route.",
                type_=FORBIDDEN,
                reason="out_of_scope",
            )

    check.__name__ = f"require_crud_{caller}"
    return Depends(check)


class _CrudRoute(APIRoute):
    """Puts the CRUD headers on every response, and on the problem of a refusal."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def crud_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except ApiError as error:
                error.headers.update(CRUD_HEADERS)
                raise
            except RequestValidationError as error:
                raise ApiError(
                    422,
                    "the request did not match the expected shape",
                    headers=CRUD_HEADERS,
                    errors=[
                        {"loc": [str(part) for part in e["loc"]], "msg": e["msg"]}
                        for e in error.errors()
                    ],
                ) from error
            except StarletteHTTPException as error:
                error.headers = {**(error.headers or {}), **CRUD_HEADERS}
                raise
            response.headers.update(CRUD_HEADERS)
            return response

        return crud_handler


def crud_router(caller: CrudCaller) -> APIRouter:
    """A router for ``/api/v1/internal/crud/<caller>``: hidden from the schema, guarded
    by :func:`require_crud`, answering with the CRUD headers."""
    return APIRouter(
        prefix=f"{PREFIX}/{caller}",
        include_in_schema=False,
        dependencies=[require_crud(caller)],
        route_class=_CrudRoute,
    )
