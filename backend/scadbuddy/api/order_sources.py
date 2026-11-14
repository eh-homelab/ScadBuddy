"""`/api/v1/orders/{order_id}/sources` (#6133 PR 4, spec 2026-11-13 §7, §8.1, §9.3, §11): a
storefront customer's files and links, as the back office's "Customer's models" panel works
on them. A person or the agent may call each route.

- ``GET …/sources/{id}/preview``: the check's stored preview, the bytes as they were
  stored (never decoded here), with `PREVIEW_HEADERS` (``image/png``, ``inline``,
  ``nosniff``). Never a presigned URL: S3 cannot add ``nosniff``.
- ``GET …/sources/{id}/download``: a presigned GET of a staged file's frozen copy, as an
  attachment, while the file waits in the staging area.
- ``POST …/sources/{id}/check``: "Check again" for a file whose check ``failed``; the check
  starts again on the intake worker (`intake.start_checks`).
- ``POST …/sources/{id}/decline``: a print file or link the shop does not take; a file's
  watcher then deletes its staged copy.
- ``POST …/sources/{id}/retry-import``: "Retry import" for a file whose import is stuck
  (`IMPORT_STUCK`): its watcher gets ``retry``.

No route takes an ``Idempotency-Key``: each repeats safely (a second decline, check or
retry finds the state the first left and writes nothing new but a signal, which a watcher
or a check absorbs). A refusal is the orders problem (``orders/problems.py`` ``refused``).
Nothing here fetches a customer's link.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Response, status

from scadbuddy.api.deps import StateDep
from scadbuddy.api.orders import _run, check_path
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import OrderItemSource, SourceDeclineInput, SourceDownload
from scadbuddy.orders.problems import refused
from scadbuddy.orders.storefront.intake import read_preview, start_checks
from scadbuddy.orders.storefront.staged_files import poke, staged_backend
from scadbuddy.storefront.wire import PREVIEW_HEADERS

router = APIRouter(prefix="/orders", tags=["orders"])

#: How long a download's presigned GET may start.
DOWNLOAD_TTL_MINUTES = 5
#: What a download's ``filename`` keeps: anything else becomes ``_``.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def _source(orders: OrdersComponent, order_id: str, source_id: str) -> OrderItemSource:
    if orders.store.order(order_id) is None:
        raise LifecycleError("order_not_found", f"no order {order_id}", not_found=True)
    found = orders.store.item_sources(order_id, source_id=source_id)
    if not found:
        raise LifecycleError("source_not_found", f"no source {source_id}", not_found=True)
    return found[0]


def _preview_key(orders: OrdersComponent, order_id: str, source_id: str) -> str | None:
    with orders.store.transaction() as conn:
        row = conn.execute(
            "SELECT preview_key FROM order_item_sources WHERE id = %s AND order_id = %s",
            (source_id, order_id),
        ).fetchone()
    return None if row is None else row["preview_key"]


@router.get(
    "/{order_id}/sources/{source_id}/preview",
    response_class=Response,
    summary="A customer file's preview",
    description=(
        "The PNG the file's check made, served as it was stored with `Content-Type: "
        "image/png`, `Content-Disposition: inline` and `X-Content-Type-Options: nosniff`. "
        "404 when the source has no preview."
    ),
    responses={status.HTTP_200_OK: {"content": {"image/png": {}}}},
)
async def source_preview(
    order_id: str, source_id: str, orders: OrdersDep, state: StateDep
) -> Response:
    check_path(order_id, source_id)
    key = await asyncio.to_thread(_preview_key, orders, order_id, source_id)
    data = await read_preview(state, key)
    if data is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"source {source_id!r} has no preview")
    return Response(content=data, headers=PREVIEW_HEADERS)


@router.get(
    "/{order_id}/sources/{source_id}/download",
    response_model=SourceDownload,
    summary="Download a customer's staged file",
    description=(
        "A presigned GET of the file's frozen copy in the staging area, as an attachment, "
        f"valid {DOWNLOAD_TTL_MINUTES} minutes. 409 `source_not_staged` once the file is "
        "imported or its staged copy is gone; 404 `source_not_found`."
    ),
)
async def source_download(
    order_id: str, source_id: str, orders: OrdersDep, state: StateDep
) -> SourceDownload:
    check_path(order_id, source_id)
    try:
        source = await asyncio.to_thread(_source, orders, order_id, source_id)
    except LifecycleError as error:
        raise refused(error) from None
    _upload, frozen_key = await asyncio.to_thread(orders.store.staged_key, source.id)
    backend = staged_backend(state)
    if source.kind != "file" or frozen_key is None or backend is None:
        raise refused(LifecycleError("source_not_staged", f"source {source_id} has no staged file"))
    name = _SAFE_NAME.sub("_", source.filename or "model") or "model"
    ttl = timedelta(minutes=DOWNLOAD_TTL_MINUTES)
    url = await backend.presign_staged_get(
        frozen_key,
        ttl,
        media_type="application/octet-stream",
        disposition=f'attachment; filename="{name}"',
    )
    return SourceDownload(url=url, expires_at=datetime.now(UTC) + ttl)


@router.post(
    "/{order_id}/sources/{source_id}/check",
    response_model=OrderItemSource,
    summary="Check a customer's file again",
    description=(
        "For a file whose check failed (every attempt timed out or crashed): the file "
        "waits for its check again, and the check starts on the intake worker. A file "
        "already being checked has its check started again (a running one is joined). "
        "409 `source_not_failed`, `source_not_staged`; 404 `source_not_found`."
    ),
)
async def source_check(
    order_id: str, source_id: str, orders: OrdersDep, state: StateDep
) -> OrderItemSource:
    check_path(order_id, source_id)
    by = current_actor()
    source = await _run(lambda: orders.lifecycle.recheck_source(order_id, source_id, by))
    await start_checks(state, [source.id])
    return source


@router.post(
    "/{order_id}/sources/{source_id}/decline",
    response_model=OrderItemSource,
    summary="Decline a customer's file or link",
    description=(
        "The shop does not print this file or link: it reads `declined` with the reason "
        "(the shop's; the customer never reads it), and a staged file is deleted by its "
        "watcher. A source already declined is returned as it is. 409 `source_not_print` "
        "for a thing to buy (use `POST …/purchases/{id}/decline`), `source_imported`, "
        "`order_not_submitted`; 404 `source_not_found`."
    ),
)
async def source_decline(
    order_id: str,
    source_id: str,
    body: SourceDeclineInput,
    orders: OrdersDep,
    state: StateDep,
) -> OrderItemSource:
    check_path(order_id, source_id)
    by = current_actor()
    source = await _run(
        lambda: orders.lifecycle.decline_source(order_id, source_id, body.reason, by)
    )
    upload, _key = await asyncio.to_thread(orders.store.staged_key, source.id)
    if upload is not None:
        await poke(state, [upload], "declined")
    return source


@router.post(
    "/{order_id}/sources/{source_id}/retry-import",
    response_model=OrderItemSource,
    summary="Retry a stuck import",
    description=(
        "For a file whose import has not finished an hour after the order was sent: its "
        "watcher is told to run the import again. 409 `import_not_stuck`; 404 "
        "`source_not_found`."
    ),
)
async def source_retry_import(
    order_id: str, source_id: str, orders: OrdersDep, state: StateDep
) -> OrderItemSource:
    check_path(order_id, source_id)
    by = current_actor()
    source = await _run(lambda: orders.lifecycle.retry_import(order_id, source_id, by))
    upload, _key = await asyncio.to_thread(orders.store.staged_key, source.id)
    if upload is not None:
        await poke(state, [upload], "retry")
    return source
