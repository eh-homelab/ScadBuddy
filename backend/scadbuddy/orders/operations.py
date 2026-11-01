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
import logging
import uuid
from collections.abc import Mapping
from contextlib import aclosing
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import Response, status
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.bambuddy.client import client_for, maybe_client
from scadbuddy.bambuddy.output_reader import LocalOutputs, require
from scadbuddy.bambuddy.preview import PreviewStarted, preview_job, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest, registry_printer, slice_route
from scadbuddy.bambuddy.print_source import OutputSource
from scadbuddy.bambuddy.runs import PrintRun, run_key
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.gitrepo import GitError, RevisionNotFoundError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import declares_pipeline
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.library.operations import PIN_TIMEOUT
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import KindsBuild, OperationKind, to_thread_to_end
from scadbuddy.orders import attachments
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import QUOTE_TIMEOUT, LifecycleError
from scadbuddy.orders.models import Actor, Reference
from scadbuddy.orders.printing import print_check, print_item
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.quote import (
    MAX_SLICED_BYTES,
    QuoteRun,
    SlicedPlate,
    SliceQuoteRequest,
    quote_check,
    quote_run,
)
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.printers.component import PRINTERS
from scadbuddy.render.job_models import Job
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.runner import OpenSCADError, cached_schema
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.slicing.component import NO_CONTENT_STORE, SLICING
from scadbuddy.store.cache import StaleBlobError

if TYPE_CHECKING:
    from scadbuddy.api.deps import PrintCommands
    from scadbuddy.core.components import Components, Core

logger = logging.getLogger(__name__)

ORDER_ITEM_QUOTE = "order_item_quote"
ORDER_ATTACHMENT_ADD = "order_attachment_add"
ORDER_ITEM_PRINT = "order_item_print"
#: One ``order_item_print`` run: the print run's accept, which answers within the command
#: deadline, and the attempt's row. Its slices and queueing are the print run's own.
PRINT_TIMEOUT = timedelta(minutes=5)


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
        body = PrintRunRequest(
            printer=request.printer,
            printer_id=request.printer_id,
            filament_plan=request.filament_plan,
            choices=request.choices,
        )
        registry = self.components.get(PRINTERS)
        # The run's own decision (plan 2026-10-18 D2), as the dialog's background slice
        # makes it (`slice_route`): a quote for a printer a run reaches over the LAN
        # builds no Bambuddy client (D3, #4551).
        route = await slice_route(
            settings,
            registry.store,
            registry.reports,
            printer=body.printer,
            printer_id=body.printer_id,
        )
        lan = route is not None and route.native
        native_reads = await self.components.get(PRINT_LIBRARY).serving_native()
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
            # A native run's copy goes into the native library (D6).
            native=native_reads if lan else None,
        )
        slicing = self.components.get(SLICING)
        async with maybe_client(settings, native=lan) as client:
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
                route=route,
                title=naming.title,
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


async def reference_schema(core: Core, reference: Reference) -> CustomizerSchema | None:
    """The schema of the reference's commit, for the quote's `output_matches` (#4774);
    None for a pipeline template, whose params need not be `model.scad`'s, or when it
    cannot be read: then the inputs are compared exactly, as before."""
    try:
        source = await resolve_source(
            reference.slug,
            reference.commit,
            paths=core.paths,
            history=core.history,
            fetcher=CheckoutFetcher(core.libraries, core.installs, core.checkouts),
        )
        if await asyncio.to_thread(declares_pipeline, source.scad.parent, reference.slug):
            return None
        return await cached_schema(
            source.scad, source.schema_cache, config=source.configure(core.config)
        )
    except (OSError, ValueError, GitError, RevisionNotFoundError, OpenSCADError):
        logger.warning("no schema for an item's reference", exc_info=True)
        return None


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
        # The quote reads the done job's outputs only, so a finished one will do (#4870).
        return await self.core.render.submit(
            slug,
            params,
            model_version=model_version,
            inputs=inputs,
            whole_inputs=whole_inputs,
            reuse_finished=True,
        )

    def read(self, job_id: str) -> Job:
        return self.core.render.store.read(job_id)


def print_body(request: Mapping[str, Any], copies: int) -> PrintRunRequest:
    """The print run's request for one ``order_item_print`` (its operation's
    ``request``): §5.4's quantity still to print, of the whole output (a quote is the
    whole output's price), keyed by the operation so a retry or a re-send repeats its
    run."""
    return PrintRunRequest.model_validate(
        {
            **request["request"],
            "copies": copies,
            "all_plates": True,
            "plate_ids": None,
            "request_id": request["request_id"],
        }
    )


async def existing_run(
    runs: PrintCommands, subject: PrintSubject, body: PrintRunRequest
) -> PrintRun | None:
    """The run ``body``'s start made, if it made one: its row by the run's key, as the
    print route finds a repeat (``api/printing.py`` ``accept_run``). A workflow still
    accepting with no row yet may still print, so it is a 503 for the retry, never a
    release."""
    key = run_key(subject.run_subject, body)
    found = await runs.store.find(key, has_request_id=True)
    if found is not None:
        return found
    try:
        described = await runs.client.get_workflow_handle(f"print-{key}").describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    if described.status == WorkflowExecutionStatus.RUNNING:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "this item's print is still being accepted; try again",
        )
    # Closed since the read above: it recorded its row, or it printed nothing.
    return await runs.store.find(key, has_request_id=True)


def order_kinds(core: Core, components: Components) -> list[OperationKind]:
    """The orders' kinds, bound to this process's state."""

    async def quote_check_kind(request: dict[str, Any]) -> dict[str, Any]:
        orders = components.get(ORDERS)
        body = SliceQuoteRequest.model_validate(request["request"])
        _, policy, _ = await asyncio.to_thread(orders.price_book.policy)
        slots = await asyncio.to_thread(
            quote_check, orders.store, policy, request["order_id"], request["item_id"], body
        )
        if body.printer is not None:
            # A registry printer the run would refuse (a disagreeing pair, a virtual
            # printer) is refused here, before the 202, as Arrange refuses it (#4551).
            # The run judges the pair again (`slice_route`): a row merged or deleted in
            # between fails the quote then, as it would fail a run.
            settings = await asyncio.to_thread(core.settings_store.load)
            await registry_printer(
                body.printer, body.printer_id, settings, components.get(PRINTERS).store
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
            schema_of=lambda reference: reference_schema(core, reference),
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

    async def item_print_check(request: dict[str, Any]) -> dict[str, Any]:
        """§5.4's refusals (``orders/printing.py`` ``print_check``), then the printer the
        run would refuse, as a quote's check refuses it."""
        orders = components.get(ORDERS)
        body = PrintRunRequest.model_validate(request["request"])
        checked = await print_check(
            orders.store,
            core.outputs,
            core.store.blobs,
            request["order_id"],
            request["item_id"],
            schema_of=lambda reference: reference_schema(core, reference),
        )
        if body.printer is not None:
            settings = await asyncio.to_thread(core.settings_store.load)
            await registry_printer(
                body.printer, body.printer_id, settings, components.get(PRINTERS).store
            )
        return checked

    async def item_print_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The item claimed, then its print started as the print route starts one
        (``accept_run``), then the run attached (``orders/printing.py`` ``print_item``)."""
        # Here, not at the top: the orders package imports nothing of `api/` as it loads
        # (`orders/__init__.py`); this run is the one place it reaches the print route.
        from scadbuddy.activity.component import ACTIVITY
        from scadbuddy.api.printing import accept_run
        from scadbuddy.workflows.print_models import SourceSpec

        output_id = checked["output_id"]

        subject = PrintSubject.output(output_id)

        def body(copies: int) -> PrintRunRequest:
            return print_body(request, copies)

        async def existing(copies: int) -> PrintRun | None:
            return await existing_run(core.print_runs, subject, body(copies))

        async def start(copies: int) -> PrintRun:
            run = await accept_run(
                core.print_runs,
                Response(),
                subject=subject,
                slug=checked["slug"],
                request=body(copies),
                source=SourceSpec(kind="output", output_id=output_id),
                activity=components.get(ACTIVITY),
            )
            # A follow URL (a `JSONResponse`) answers only a request that asked for one.
            assert isinstance(run, PrintRun)
            return run

        attempt = await print_item(
            components.get(ORDERS).lifecycle,
            start,
            existing,
            request["order_id"],
            request["item_id"],
            Actor.model_validate(request["by"]),
            request_id=request["request_id"],
            sliced_sha256=checked["sliced_sha256"],
            sliced_blob_key=checked["sliced_blob_key"],
        )
        dumped: dict[str, Any] = attempt.model_dump(mode="json")
        return dumped

    async def attachment_check(request: dict[str, Any]) -> dict[str, Any]:
        """The order, and the item when one is named, are there (nothing written)."""
        store = components.get(ORDERS).store
        order = await asyncio.to_thread(store.order, request["order_id"])
        if order is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {request['order_id']!r}")
        item_id = request["item_id"]
        if item_id is not None:
            item = await asyncio.to_thread(store.item, item_id)
            if item is None or item.order_id != order.id:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.number}"
                )
        return {}

    async def attachment_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The claimed upload kept as its blob, then the row with its hold (§6.2). A
        failure after the blob is kept leaves a blob no row holds, which the blob sweep
        takes once its grace is past."""
        orders = components.get(ORDERS)
        claims = ClaimStore(core.paths.claims)
        try:
            key = await attachments.keep(core.store.blobs, claims, request["file"], request["file"])
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this request's upload is no longer held; send it again",
            ) from None
        except StaleBlobError:
            # The same bytes are stored on the other backend of a `/store/migrate`
            # (`CachedBlobStore.claim`): the upload works once the store has moved.
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "the stored copy of these bytes is not on this store's backend (a"
                " `/store/migrate` in progress?); send the file again once it has flipped"
                " `store_backend` and the API has restarted",
            ) from None
        try:
            attachment = await to_thread_to_end(
                lambda: orders.lifecycle.add_attachment(
                    request["order_id"],
                    request["item_id"],
                    Actor.model_validate(request["by"]),
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
                )
            )
        except LifecycleError as error:
            raise refused(error) from None
        dumped: dict[str, Any] = attachment.model_dump(mode="json")
        return dumped

    return [
        OperationKind(
            ORDER_ATTACHMENT_ADD,
            attachment_check,
            attachment_run,
            # A second attempt after a partial failure would only repeat the same
            # claimed bytes: the caller re-sends instead.
            run_attempts=1,
            queue="library",
            # Moves (or copies) up to the upload limit into the blob store and publishes it.
            run_timeout=PIN_TIMEOUT,
            where="the order's attachments",
        ),
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
        ),
        OperationKind(
            ORDER_ITEM_PRINT,
            item_print_check,
            item_print_run,
            # A retry repeats the same print run (its `request_id` is the operation's),
            # so a start that found Temporal busy is tried again, never printed twice.
            run_attempts=3,
            queue="library",
            run_timeout=PRINT_TIMEOUT,
            where="the printer's queue and the item's attempts",
        ),
    ]


#: Registered with the operations component (`operations/component.py`).
OPERATION_KINDS: KindsBuild = order_kinds
