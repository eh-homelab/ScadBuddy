from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
        **extensions: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.title = title or _TITLES.get(status, "Error")
        self.type = type_
        self.extensions = extensions


def problem_response(
    request: Request,
    status: int,
    detail: str,
    *,
    title: str | None = None,
    type_: str = "about:blank",
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": title or _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "instance": request.url.path,
    }
    body.update(extensions or {})
    return JSONResponse(body, status_code=status, media_type=PROBLEM_MEDIA_TYPE)


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
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(request, exc.status_code, str(exc.detail))

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
        return problem_response(request, 500, f"{type(exc).__name__}: {exc}")
