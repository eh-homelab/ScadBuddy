"""The CRUD routers' guard (#5298, plan decisions D1, D2, D4).

A route under ``/api/v1/internal/crud/<caller>/`` is class ``crud.<caller>``
(`auth/routes.py`). ``require_crud`` refuses it unless the request carries a live service
credential of that caller whose scopes hold the class, in every ``SCADBUDDY_AUTH_MODE``:
these routes are new, so no client needs ``off`` or ``observe`` to let it through. A
missing or dead credential is 401 ``…/auth/signed-out``; any other is 403 ``…/auth/forbidden``.

A database error that passes (`psycopg.OperationalError`: out of reach, a cancelled
statement, a deadlock, a serialization failure) is a 503 with reason
``database-unavailable``; an exception the app has a handler for gets that handler's
answer; any other is a 500. Every response of a CRUD router, a refusal and an error's
included, carries ``X-ScadBuddy-Crud: 1`` and ``Cache-Control: no-store``, so a client
can tell an answer of these routes from a proxy's.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from scadbuddy.auth.check import FORBIDDEN, SIGNED_OUT
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Service
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

#: The reason of the 503 a CRUD route answers on a `psycopg.OperationalError`.
DATABASE_UNAVAILABLE = "database-unavailable"

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
            except psycopg.OperationalError as error:
                # The database out of reach (a pool timeout is one too), or a statement
                # it cancelled, a deadlock or a serialization failure: states that pass,
                # as the SQL accept treats them, which the client tells from a route's
                # own 500 by its reason.
                logger.warning("database unavailable", extra={"path": request.url.path})
                raise ApiError(
                    503,
                    "The database is out of reach.",
                    headers=CRUD_HEADERS,
                    reason=DATABASE_UNAVAILABLE,
                ) from error
            except Exception as error:
                registered = _registered_handler(request, error)
                if registered is not None:
                    # The app's own answer for it (a 409 for a library not installed),
                    # with the CRUD headers.
                    answer = registered(request, error)
                    response = await answer if inspect.isawaitable(answer) else answer
                else:
                    # Still this route's answer: without the headers a client would take
                    # the 500 for a missing route, an API out of reach (`crud/client.py`).
                    logger.exception("unhandled error", extra={"path": request.url.path})
                    raise ApiError(500, type(error).__name__, headers=CRUD_HEADERS) from error
            response.headers.update(CRUD_HEADERS)
            return response

        return crud_handler


def _registered_handler(request: Request, error: Exception) -> Callable[..., Any] | None:
    """The app's handler for ``error``'s type or a base of it, short of `Exception`, whose
    handler is the catch-all 500 this guard replaces."""
    handlers = request.app.exception_handlers
    for kind in type(error).__mro__:
        if kind is Exception:
            return None
        if kind in handlers:
            handler: Callable[..., Any] = handlers[kind]
            return handler
    return None


def crud_router(caller: CrudCaller) -> APIRouter:
    """A router for ``/api/v1/internal/crud/<caller>``: hidden from the schema, guarded
    by :func:`require_crud`, answering with the CRUD headers."""
    return APIRouter(
        prefix=f"{PREFIX}/{caller}",
        include_in_schema=False,
        dependencies=[require_crud(caller)],
        route_class=_CrudRoute,
    )
