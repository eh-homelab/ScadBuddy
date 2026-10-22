"""``order_item_quote`` as an operation kind (#4250, plan Ruling 1): the record, the 202
and its follow, the ``Idempotency-Key`` a re-sent request joins the running one by, and
the activity row, on the ``library`` queue, whose worker is in the API process and holds
the data volume the item's output and the quote's blobs live on.

The check is ``orders/quote.py`` ``quote_check`` (the rows alone, nothing written); the
run is ``quote_run`` over this process's services, read from the state when it runs
(``operations/kinds.py``). The slice is the print dialog's own background slice
(``bambuddy/preview.py`` ``start_preview_plates``, Ruling 2) on whichever slicer the
stack has; a slicer's refusal (no spool for a slot, a preset Bambuddy lacks) is the
run's failure, recorded with its text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.output_reader import LocalOutputs, require
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import OutputSource
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import KindsBuild, OperationKind
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import QUOTE_TIMEOUT
from scadbuddy.orders.models import Actor
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.quote import (
    MAX_SLICED_BYTES,
    QuoteRun,
    SlicedPlate,
    SliceQuoteRequest,
    quote_check,
    quote_run,
)
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.render.job_models import Job
from scadbuddy.slicing.component import NO_CONTENT_STORE, SLICING

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

ORDER_ITEM_QUOTE = "order_item_quote"


def _slice_failed(plate_id: int, failure: str | None) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"plate {plate_id}: the slice failed" + (f": {failure}" if failure else ""),
        type_=PROBLEMS + "slice_failed",
        plate=plate_id,
    )


def _too_big(plate_id: int) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        f"plate {plate_id}: the sliced file is too large to keep",
        type_=PROBLEMS + "sliced_plate",
        plate=plate_id,
    )


class PreviewSlicer:
    """``orders/quote.py``'s ``Slicer`` over the dialog's background slice: Bambuddy's,
    or the native slicer's on ``slicer_backend`` native, as ``start_preview`` picks."""

    def __init__(self, core: Core, components: Components) -> None:
        self.core = core
        self.components = components

    async def start(
        self, output_id: str, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        core = self.core
        settings = await asyncio.to_thread(core.settings_store.load)
        outputs = LocalOutputs(core.outputs, core.catalogue)
        meta = await require(outputs, output_id)
        naming = await outputs.naming(meta)
        source = OutputSource(
            store=outputs,
            meta=meta,
            uploads=core.uploads,
            settings=settings,
            # The quote's copy goes to the inbox, as the dialog's does.
            stem=None,
            print_settings=naming.print_settings,
        )
        slicing = self.components.get(SLICING)
        body = PrintRunRequest(
            printer_id=request.printer_id,
            filament_plan=request.filament_plan,
            choices=request.choices,
        )
        native_reads = await self.components.get(PRINT_LIBRARY).serving_native()
        async with client_for(settings) as client:
            return await start_preview_plates(
                client,
                source,
                settings,
                core.uploads,
                body,
                subject=subject,
                native=slicing.starter,
                native_slices=slicing.store,
                temporal=slicing.temporal(),
                native_reads=native_reads,
            )

    async def poll(self, started: PreviewStarted, subject: str) -> SlicedPlate | None:
        if started.slicer == "native":
            row = await self.components.get(SLICING).store.get(started.job_id, subject)
            if row is None:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND, f"there is no background slice {started.job_id}"
                )
            if row.status == "running":
                return None
            if row.status != "completed" or row.sliced_blob_key is None:
                raise _slice_failed(started.plate_id, row.failure or row.status)
            return SlicedPlate(
                plate_id=started.plate_id, slicer="native", blob_key=row.sliced_blob_key
            )
        settings = await asyncio.to_thread(self.core.settings_store.load)
        async with client_for(settings) as client:
            job = await preview_job(client, self.core.uploads, started.job_id, subject)
        if job.status == "failed":
            raise _slice_failed(started.plate_id, job.failure)
        if job.status != "completed":
            return None
        sliced = job.result.library_file_id if job.result else None
        if sliced is None:
            raise _slice_failed(started.plate_id, "Bambuddy names no sliced file")
        return SlicedPlate(plate_id=started.plate_id, slicer="bambuddy", file_id=sliced)

    async def fetch(self, sliced: SlicedPlate) -> bytes:
        """The sliced file's bytes (Ruling 3): Bambuddy's library download, or the
        native slice's blob through the content store the native slicer needs."""
        data = bytearray()
        if sliced.slicer == "native":
            content = self.core.store.content
            if content is None:
                raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NO_CONTENT_STORE)
            stat = await content.stat(sliced.blob_key or "")
            if stat is None:
                raise _slice_failed(sliced.plate_id, "the sliced file is gone")
            if stat.ref.size > MAX_SLICED_BYTES:
                raise _too_big(sliced.plate_id)
            async with aclosing(content.get(stat.ref)) as chunks:
                async for chunk in chunks:
                    data += chunk
            return bytes(data)
        assert sliced.file_id is not None
        settings = await asyncio.to_thread(self.core.settings_store.load)
        async with (
            client_for(settings) as client,
            aclosing(client.download_library_file(sliced.file_id)) as chunks,
        ):
            async for chunk in chunks:
                data += chunk
                if len(data) > MAX_SLICED_BYTES:
                    raise _too_big(sliced.plate_id)
        return bytes(data)


class _Renders:
    """``RenderService`` as ``orders/quote.py`` ``ensure_output`` reads it."""

    def __init__(self, core: Core) -> None:
        self.core = core

    async def submit(
        self,
        slug: str,
        params: Mapping[str, Any],
        *,
        model_version: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        whole_inputs: bool = False,
    ) -> Job:
        return await self.core.render.submit(
            slug, params, model_version=model_version, inputs=inputs, whole_inputs=whole_inputs
        )

    def read(self, job_id: str) -> Job:
        return self.core.render.store.read(job_id)


def order_kinds(core: Core, components: Components) -> list[OperationKind]:
    """The orders' kinds, bound to this process's state."""

    async def quote_check_kind(request: dict[str, Any]) -> dict[str, Any]:
        orders = components.get(ORDERS)
        body = SliceQuoteRequest.model_validate(request["request"])
        _, policy, _ = await asyncio.to_thread(orders.price_book.policy)
        slots = await asyncio.to_thread(
            quote_check, orders.store, policy, request["order_id"], request["item_id"], body
        )
        return {"slots": {str(slot): filament for slot, filament in slots.items()}}

    async def quote_run_kind(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        # Read when it runs: the operations component is built after this one's kinds.
        from scadbuddy.operations.component import OPERATIONS

        orders = components.get(ORDERS)
        create = components.get(OPERATIONS).kinds["output_create"]

        async def create_output(body: dict[str, Any]) -> dict[str, Any]:
            return await create.run(body, {})

        run = QuoteRun(
            lifecycle=orders.lifecycle,
            book=orders.price_book,
            outputs=core.outputs,
            renders=_Renders(core),
            create_output=create_output,
            slicer=PreviewSlicer(core, components),
            blobs=core.store.blobs,
            refs=orders.refs,
        )
        quote = await quote_run(
            run,
            request["order_id"],
            request["item_id"],
            SliceQuoteRequest.model_validate(request["request"]),
            Actor.model_validate(request["by"]),
        )
        dumped: dict[str, Any] = quote.model_dump(mode="json")
        return dumped

    return [
        OperationKind(
            ORDER_ITEM_QUOTE,
            quote_check_kind,
            quote_run_kind,
            run_attempts=1,
            queue="library",
            run_timeout=QUOTE_TIMEOUT,
            where="the item's quotes",
            # The render and every plate's slice outlast the command deadline: the
            # caller follows the operation anyway.
            answers_running=True,
        )
    ]


#: Registered with the operations component (`operations/component.py`).
OPERATION_KINDS: KindsBuild = order_kinds
