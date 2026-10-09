"""Mapping Bambuddy's HTTP failures onto RFC 9457 problem details.

Bambuddy answers a rejected call with FastAPI's ``{"detail": ...}`` body and
documents no 401/403 in its own OpenAPI, so the status code is the only reliable
signal. Each client call therefore declares the API-key scope it needs, and a
401/403 is reported as *that* scope being missing rather than as a bare "403".
"""

from __future__ import annotations

import os
import re
import socket
import ssl
from enum import StrEnum
from typing import Any

import httpx
from fastapi import status
from pydantic import BaseModel

from scadbuddy.core.problems import ApiError

#: Problem ``type`` URIs. The frontend branches on these, so they are contract.
SCOPE_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-scope"
NOT_FOUND_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-not-found"
CONFLICT_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-conflict"
REJECTED_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-rejected"
UNAVAILABLE_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-unavailable"
NOT_CONFIGURED_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-not-configured"
PLATE_FIT_PROBLEM = "https://scadbuddy.dev/problems/plate-does-not-fit"


class Scope(StrEnum):
    """Bambuddy's API-key scopes, spelled as its own settings UI spells them.

    The authoritative list is ``APIKeyCreate``'s ``can_*`` flags in Bambuddy's
    ``openapi.json`` — ``can_read_status``, ``can_manage_library``, ``can_queue``,
    ``can_manage_projects`` are the four ScadBuddy needs.
    """

    READ_STATUS = "Read Status"
    MANAGE_LIBRARY = "Manage Library"
    MANAGE_QUEUE = "Manage Queue"
    MANAGE_PROJECTS = "Manage Projects"
    #: ``can_manage_archives``: photo upload and delete, and pulling a timelapse off
    #: the printer (Bambuddy ``auth.py:194-196`` at v1.2.5.6).
    MANAGE_ARCHIVES = "Manage Archives"


def not_configured(detail: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, detail, type_=NOT_CONFIGURED_PROBLEM)


def upstream_detail(response: httpx.Response) -> str | None:
    """Bambuddy's own message, when it sent a JSON body carrying one."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return repr(detail)
    return None


def upstream_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


class UpstreamAnswer(BaseModel):
    """What Bambuddy itself answered to a failed call (#1542): the status, the
    ``Retry-After`` it asked for and its own ``detail``; or, when it never answered,
    why not. Never the raw body: the URL is a setting, so a body passed through would
    let whoever sets it read any address this server can reach."""

    status: int | None = None
    retry_after: str | None = None
    detail: str | None = None
    error: str | None = None


def _answered(error: ApiError, answer: UpstreamAnswer) -> ApiError:
    error.upstream = answer
    return error


def map_response(response: httpx.Response, *, scope: Scope, what: str) -> ApiError:
    """Turn a failed Bambuddy response into the problem the browser should see, with
    Bambuddy's own answer kept on it (`UpstreamAnswer`).

    ``what`` names the operation in the first person plural of the UI ("upload the
    3MF"), so the detail reads as a sentence.
    """
    answer = UpstreamAnswer(
        status=response.status_code,
        retry_after=response.headers.get("Retry-After"),
        detail=upstream_detail(response),
    )
    return _answered(_mapped(response, scope=scope, what=what, answer=answer), answer)


def _mapped(
    response: httpx.Response, *, scope: Scope, what: str, answer: UpstreamAnswer
) -> ApiError:
    code = response.status_code
    detail = upstream_detail(response)
    suffix = f": {detail}" if detail else ""

    if code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        return ApiError(
            status.HTTP_409_CONFLICT,
            f"Bambuddy refused the API key when asked to {what}. "
            f"The key needs the {scope.value!r} scope{suffix}",
            title="Bambuddy API key scope",
            type_=SCOPE_PROBLEM,
            bambuddy_status=code,
            required_scope=scope.value,
        )
    if code == status.HTTP_404_NOT_FOUND:
        return ApiError(
            status.HTTP_404_NOT_FOUND,
            f"Bambuddy has no such resource when asked to {what}{suffix}",
            type_=NOT_FOUND_PROBLEM,
            bambuddy_status=code,
        )
    if code == status.HTTP_409_CONFLICT:
        # Bambuddy's own reason lives in the body; pass it through verbatim rather than
        # paraphrase it.
        return ApiError(
            status.HTTP_409_CONFLICT,
            f"Bambuddy reported a conflict when asked to {what}{suffix}",
            type_=CONFLICT_PROBLEM,
            bambuddy_status=code,
            bambuddy_body=upstream_body(response),
        )
    if code == status.HTTP_422_UNPROCESSABLE_CONTENT:
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Bambuddy rejected the request to {what}{suffix}",
            type_=REJECTED_PROBLEM,
            bambuddy_status=code,
            bambuddy_body=upstream_body(response),
        )
    if code == status.HTTP_429_TOO_MANY_REQUESTS:
        wait = f"; it asks to wait {answer.retry_after} s" if answer.retry_after else ""
        return ApiError(
            status.HTTP_502_BAD_GATEWAY,
            f"Bambuddy is limiting requests and refused to {what}{wait}{suffix}",
            type_=UNAVAILABLE_PROBLEM,
            bambuddy_status=code,
        )
    return ApiError(
        status.HTTP_502_BAD_GATEWAY,
        f"Bambuddy answered {code} when asked to {what}{suffix}",
        type_=UNAVAILABLE_PROBLEM,
        bambuddy_status=code,
    )


#: Bambuddy statuses a later attempt may not meet: a gateway's (#1144).
TRANSIENT_STATUSES = frozenset(
    {
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        status.HTTP_504_GATEWAY_TIMEOUT,
    }
)


def is_transient(error: ApiError) -> bool:
    """Whether ``error`` is Bambuddy not answering (:func:`map_transport`) or a gateway's
    502, 503 or 504 in front of it (:func:`map_response`): worth another attempt where
    the effect may run again (#1144). Any other answer would only come back."""
    if error.type != UNAVAILABLE_PROBLEM:
        return False
    upstream = error.extensions.get("bambuddy_status")
    return upstream is None or upstream in TRANSIENT_STATUSES


def map_transport(error: httpx.HTTPError, *, what: str) -> ApiError:
    code = (
        status.HTTP_504_GATEWAY_TIMEOUT
        if isinstance(error, httpx.TimeoutException)
        else status.HTTP_502_BAD_GATEWAY
    )
    return _answered(
        ApiError(
            code,
            f"could not reach Bambuddy to {what}: {type(error).__name__}",
            type_=UNAVAILABLE_PROBLEM,
        ),
        UpstreamAnswer(error=f"{type(error).__name__}: {_transport_reason(error)}"),
    )


#: What each transport failure means, in ScadBuddy's words; the first match wins.
_TRANSPORT_REASONS: tuple[tuple[type[httpx.HTTPError], str], ...] = (
    (httpx.ConnectTimeout, "timed out connecting"),
    (httpx.ReadTimeout, "timed out waiting for the answer"),
    (httpx.WriteTimeout, "timed out sending the request"),
    (httpx.PoolTimeout, "timed out waiting for a free connection"),
    (httpx.RemoteProtocolError, "the server did not answer in HTTP"),
    (httpx.ConnectError, "could not connect"),
    (httpx.ReadError, "the connection failed while reading the answer"),
    (httpx.WriteError, "the connection failed while sending the request"),
)

#: An OpenSSL reason code, such as CERTIFICATE_VERIFY_FAILED: a constant, never peer text.
_TLS_REASON = re.compile(r"[A-Z0-9_]{1,64}")


def _transport_reason(error: httpx.HTTPError) -> str:
    """Why the call failed, never in the exception's own words. For a peer that does
    not speak HTTP, h11 quotes the bytes it received in its message (``illegal status
    line: bytearray(b'...')``), and the URL is a setting: passing that text on would
    let whoever sets the URL read the first line of any port this server can reach.
    What the local socket or TLS layer said is kept, as a constant: the OS's text for
    the errno, or OpenSSL's reason code."""
    seen: set[int] = set()
    cause = error.__cause__ or error.__context__
    # A chain can loop back on itself, through a handler that re-raises.
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            reason = getattr(cause, "reason", None)
            reason = reason if isinstance(reason, str) else ""
            return f"TLS failed ({reason})" if _TLS_REASON.fullmatch(reason) else "TLS failed"
        if isinstance(cause, socket.gaierror):
            return "the host name could not be resolved"
        if isinstance(cause, OSError) and cause.errno:
            return os.strerror(cause.errno)
        cause = cause.__cause__ or cause.__context__
    for kind, reason in _TRANSPORT_REASONS:
        if isinstance(error, kind):
            return reason
    return "the call failed"
