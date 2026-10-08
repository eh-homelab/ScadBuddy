"""Uploading and fetching a file for a `// file` parameter as operations (#1054, spec
2026-10-01 §4.3, the ``library`` row). The upload's bytes and the fetch's URL arrive as
claims (``operations/claims.py``): the URL's query may carry a token, so the request
holds only its shown form.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api import assets as assets_api
from scadbuddy.api.deps import DATABASE_REQUIRED_PROBLEM
from scadbuddy.api.models import require_model_exists
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import AssetStoreUnavailableError
from scadbuddy.library.operations import answered_as_routes
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState


def _store_answered[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """The upload store without its database as the 503 ``install_asset_handlers``
    answers it with, rather than the operation's unexpected 500."""

    @wraps(fn)
    async def answered(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except AssetStoreUnavailableError as error:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE, str(error), type_=DATABASE_REQUIRED_PROBLEM
            ) from None

    return answered


def asset_kinds(state: AppState) -> list[OperationKind]:
    """The asset kinds, bound to this process's state; ``library/operations.py``
    exports them with the pins."""

    async def _claimed(name: str) -> bytes:
        try:
            return await asyncio.to_thread(ClaimStore(state.paths.claims).get, name)
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this request's upload is no longer held; send it again",
            ) from None

    async def exists_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        return {}

    async def upload_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        data = await _claimed(request["file"])
        meta = await assets_api.store_asset(
            request["slug"], data, request["file_name"], assets=state.assets, state=state
        )
        dumped: dict[str, Any] = meta.model_dump(mode="json")
        return dumped

    async def fetch_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        url = (await _claimed(request["url"])).decode()
        fetched = await assets_api.fetch_run(request["slug"], url, state)
        dumped: dict[str, Any] = fetched.model_dump(mode="json")
        return dumped

    def kind(name: str, check: Any, run: Any) -> OperationKind:
        return OperationKind(
            name,
            answered_as_routes(_store_answered(check)),
            answered_as_routes(_store_answered(run)),
            queue="library",
            where="the template's uploaded files",
        )

    return [
        kind("asset_upload", exists_check, upload_run),
        kind("asset_fetch", exists_check, fetch_run),
    ]
