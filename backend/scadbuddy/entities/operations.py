"""``entity_attachment_add`` as an operation kind (#6950), on the ``library`` queue, as
``order_attachment_add`` is (``orders/operations.py``): the route claims the streamed
upload by its sha256 (``api/entity_attachments.py``), and the run keeps it as the blob
``attachment-<sha256>`` and writes the row with its hold in one transaction."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.entities.component import ENTITIES
from scadbuddy.entities.problems import entity_not_found, writable
from scadbuddy.entities.registry import EntityNotFound
from scadbuddy.library.operations import PIN_TIMEOUT
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import KindsBuild, OperationKind, to_thread_to_end
from scadbuddy.orders import attachments
from scadbuddy.orders.models import Actor
from scadbuddy.store.cache import StaleBlobError

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

ENTITY_ATTACHMENT_ADD = "entity_attachment_add"


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    async def check(request: dict[str, Any]) -> dict[str, Any]:
        """The owner's row is there (nothing written)."""
        ref = writable(request["entity"])
        entities = components.get(ENTITIES)

        def there() -> bool:
            with entities.opened().connection() as conn:
                return entities.store.exists(conn, ref)

        if not await asyncio.to_thread(there):
            raise entity_not_found(ref)
        return {}

    async def run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The claimed upload kept as its blob, then the row with its hold. A failure
        after the blob is kept leaves a blob no row holds, which the blob sweep takes
        once its grace is past."""
        ref = writable(request["entity"])
        entities = components.get(ENTITIES)
        store = entities.attachments
        claims = ClaimStore(core.paths.claims)
        try:
            key = await attachments.keep(core.store.blobs, claims, request["file"], request["file"])
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this request's upload is no longer held; send it again",
            ) from None
        except StaleBlobError:
            # As `order_attachment_add`'s: the same bytes are on the other backend of a
            # `/store/migrate`.
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "the stored copy of these bytes is not on this store's backend (a"
                " `/store/migrate` in progress?); send the file again once it has flipped"
                " `store_backend` and the API has restarted",
            ) from None

        def add() -> dict[str, Any]:
            with entities.opened().connection() as conn:
                added = store.add(
                    conn,
                    ref,
                    # Made here, not in the request, so a re-sent upload (the same
                    # Idempotency-Key) is the same request; the run has one attempt.
                    attachment_id=str(uuid.uuid4()),
                    kind=request["kind"],
                    role=request["role"],
                    filename=request["filename"],
                    media_type=request["media_type"],
                    size_bytes=request["size"],
                    sha256=request["file"],
                    blob_key=key,
                    caption=request["caption"],
                    by=Actor.model_validate(request["by"]),
                )
            dumped: dict[str, Any] = added.model_dump(mode="json")
            return dumped

        try:
            return await to_thread_to_end(add)
        except EntityNotFound:
            raise entity_not_found(ref) from None

    return [
        OperationKind(
            ENTITY_ATTACHMENT_ADD,
            check,
            run,
            # As `order_attachment_add`: a second attempt would only repeat the same
            # claimed bytes; the caller re-sends instead.
            run_attempts=1,
            queue="library",
            run_timeout=PIN_TIMEOUT,
            where="the entity's attachments",
        )
    ]


#: Registered with the operations component (`operations/component.py`).
OPERATION_KINDS: KindsBuild = _kinds
