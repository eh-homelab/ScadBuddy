from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount

PROBLEM_MEDIA_TYPE = "application/problem+json"

logger = logging.getLogger(__name__)

_TITLES = {
    400: "Bad Request",
    413: "Content Too Large",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    # nginx's, not IANA's: the client hung up before it was answered.
    499: "Client Closed Request",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class ApiError(Exception):
    """A failure the client should see as an RFC 9457 problem document."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        title: str | None = None,
        type_: str = "about:blank",
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.title = title or _TITLES.get(status, "Error")
        self.type = type_
        #: Response headers, e.g. a 503's Retry-After.
        self.headers = dict(headers or {})
        self.extensions = extensions


def problem_response(
    request: Request,
    status: int,
    detail: str,
    *,
    title: str | None = None,
    type_: str = "about:blank",
    extensions: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": title or _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "instance": request.url.path,
    }
    body.update(extensions or {})
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


#: The methods a 405's ``Allow`` is drawn from.
_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def _allowed_methods(request: Request) -> str | None:
    """Every method some route on this path takes, for a 405's Allow (#1318).

    The router fills Allow from the first route whose path matches, so ``GET /settings``
    and ``PUT /settings``, being two routes, answer ``Allow: GET``. Each method is
    probed instead: FastAPI keeps an included router as one opaque route that says
    which methods match only when asked. A mount (the SPA's, at ``/``) matches every
    method on every path, so it is not asked.
    """
    allowed = {
        method
        for method in _METHODS
        for route in request.app.router.routes
        if not isinstance(route, Mount)
        and route.matches({**request.scope, "method": method})[0] is Match.FULL
    }
    return ", ".join(sorted(allowed)) or None


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_problem(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(
            request,
            exc.status,
            exc.detail,
            title=exc.title,
            type_=exc.type,
            extensions=exc.extensions,
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        headers = dict(exc.headers or {})
        if exc.status_code == 405 and (allow := _allowed_methods(request)):
            headers["Allow"] = allow
        return problem_response(request, exc.status_code, str(exc.detail), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(part) for part in error["loc"]], "msg": error["msg"]}
            for error in exc.errors()
        ]
        return problem_response(
            request,
            422,
            "the request did not match the expected shape",
            extensions={"errors": errors},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"path": request.url.path})
        if isinstance(exc, psycopg.Error):
            # The driver's text quotes the SQL and its bound values (#965).
            return problem_response(request, 500, type(exc).__name__)
        return problem_response(request, 500, f"{type(exc).__name__}: {exc}")
