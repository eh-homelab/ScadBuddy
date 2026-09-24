"""Mapping Bambuddy's HTTP failures onto RFC 9457 problem details.

Bambuddy answers a rejected call with FastAPI's ``{"detail": ...}`` body and
documents no 401/403 in its own OpenAPI, so the status code is the only reliable
signal. Each client call therefore declares the API-key scope it needs, and a
401/403 is reported as *that* scope being missing rather than as a bare "403".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import httpx
from fastapi import status

from scadbuddy.core.problems import ApiError

#: Problem ``type`` URIs. The frontend branches on these, so they are contract.
SCOPE_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-scope"
NOT_FOUND_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-not-found"
ELIGIBILITY_PROBLEM = "https://scadbuddy.dev/problems/pipeline-ineligible"
REJECTED_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-rejected"
UNAVAILABLE_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-unavailable"
NOT_CONFIGURED_PROBLEM = "https://scadbuddy.dev/problems/bambuddy-not-configured"
PLATE_FIT_PROBLEM = "https://scadbuddy.dev/problems/plate-does-not-fit"


class Scope(StrEnum):
    """Bambuddy's API-key scopes, spelled as its own settings UI spells them.

    The authoritative list is ``APIKeyCreate``'s ``can_*`` flags in Bambuddy's
    ``openapi.json`` — ``can_read_status``, ``can_manage_library``, ``can_queue``,
    ``can_manage_projects`` are the four ScadBuddy needs. Which flag guards
    ``/slicer-pipelines/`` could **not** be verified: the homelab instance runs with
    authentication disabled, so every call succeeds whatever the key says. Those
    methods declare ``MANAGE_QUEUE`` because running a pipeline queues prints; if a
    key with that scope still 403s there, this is the line to correct.
    """

    READ_STATUS = "Read Status"
    MANAGE_LIBRARY = "Manage Library"
    MANAGE_QUEUE = "Manage Queue"
    MANAGE_PROJECTS = "Manage Projects"


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


def map_response(response: httpx.Response, *, scope: Scope, what: str) -> ApiError:
    """Turn a failed Bambuddy response into the problem the browser should see.

    ``what`` names the operation in the first person plural of the UI ("upload the
    3MF"), so the detail reads as a sentence.
    """
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
        # The pipeline-eligibility report lives in the body; pass it through verbatim
        # so the UI can list the blocking issues rather than paraphrase them.
        return ApiError(
            status.HTTP_409_CONFLICT,
            f"Bambuddy reported a conflict when asked to {what}{suffix}",
            type_=ELIGIBILITY_PROBLEM,
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
    return ApiError(
        status.HTTP_502_BAD_GATEWAY,
        f"Bambuddy answered {code} when asked to {what}{suffix}",
        type_=UNAVAILABLE_PROBLEM,
        bambuddy_status=code,
    )


def map_transport(error: httpx.HTTPError, *, what: str) -> ApiError:
    code = (
        status.HTTP_504_GATEWAY_TIMEOUT
        if isinstance(error, httpx.TimeoutException)
        else status.HTTP_502_BAD_GATEWAY
    )
    return ApiError(
        code,
        f"could not reach Bambuddy to {what}: {type(error).__name__}",
        type_=UNAVAILABLE_PROBLEM,
    )
