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
from collections.abc import Mapping, Sequence
from contextlib import aclosing
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from fastapi import Response, status
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.output_reader import LocalOutputs, invalid_meta, require
from scadbuddy.bambuddy.part_settings import dumped
from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.bambuddy.print_run import (
    PrintRunRequest,
    registry_printer,
    run_route,
    slice_route,
)
from scadbuddy.bambuddy.print_source import OutputSource
from scadbuddy.bambuddy.runs import PrintRun, run_key
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.gitrepo import GitError, RevisionNotFoundError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import declares_pipeline
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.library.operations import PIN_TIMEOUT
from scadbuddy.library.outputs import OutputNotFoundError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OPERATION_RUN, KindsBuild, OperationKind, to_thread_to_end
from scadbuddy.orders import attachments
from scadbuddy.orders.archives import copy_check, copy_in
from scadbuddy.orders.bambuddy import (
    ServedLibrary,
    download_sliced,
    quote_preview_job,
    start_library_quote_preview,
    start_quote_preview,
)
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import QUOTE_TIMEOUT, LifecycleError
from scadbuddy.orders.models import Actor, Reference
from scadbuddy.orders.printing import (
    ItemPrintRequest,
    print_check,
    print_item,
    quoted_path_refusal,
    sent_keys,
    sent_setup,
    test_print_check,
)
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.projects import item_project
from scadbuddy.orders.quote import (
    MAX_SLICED_BYTES,
    QuoteRun,
    SlicedPlate,
    SliceQuoteRequest,
    library_file_gone,
    operation_quote_id,
    quote_check,
    quote_run,
    support_check,
)
from scadbuddy.orders.spools import item_plan
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.hot_path import native_backends
from scadbuddy.render.job_models import Job
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.read_budget import budget_of
from scadbuddy.render.runner import OpenSCADError, cached_schema
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.slicing.component import NO_CONTENT_STORE, SLICING
from scadbuddy.store.cache import StaleBlobError
from scadbuddy.store.content_models import BlobMissingError

if TYPE_CHECKING:
    from scadbuddy.api.deps import PrintCommands
    from scadbuddy.core.components import Components, Core

logger = logging.getLogger(__name__)

ORDER_ITEM_QUOTE = "order_item_quote"
ORDER_ATTACHMENT_ADD = "order_attachment_add"
ORDER_ITEM_PRINT = "order_item_print"
ORDER_ATTEMPT_COPY = "order_attempt_copy"
#: Failures of a quote's run that another attempt would only repeat (#5353): a bug in
#: ScadBuddy's code (an assertion, a wrong type or key, a value it cannot read) or SQL the
#: database refuses as written. Not ``OSError``, ``httpx`` or ``psycopg.OperationalError``
#: (a volume, Bambuddy or the database briefly away), nor the worker's own stop: those are
#: what the next attempt is for. Each attempt renders and slices again, for up to
#: ``QUOTE_TIMEOUT``, so a bug fails the operation at once instead. Except
#: ``QUOTE_RETRYABLE``, which are ``LookupError``s too.
QUOTE_DETERMINISTIC: tuple[type[Exception], ...] = (
    AssertionError,
    AttributeError,
    LookupError,
    NotImplementedError,
    TypeError,
    ValueError,
    psycopg.DataError,
    psycopg.IntegrityError,
    psycopg.ProgrammingError,
)
#: ``LookupError``s that another attempt can get past (#5415): an output deleted, or a
#: blob swept, while the run read it. The next attempt's ``ensure_output`` renders and
#: saves the output again, and slices again.
QUOTE_RETRYABLE: tuple[type[Exception], ...] = (OutputNotFoundError, BlobMissingError)
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


def preview_request(request: SliceQuoteRequest) -> PrintRunRequest:
    """The print run's request a quote's background slice prepares (Ruling 2): the
    dialog's choices, the route, the settings per slot (#5571) and the tower, supports
    and sequence (#4673), so the quote slices what the print will."""
    return PrintRunRequest(
        printer=request.printer,
        printer_id=request.printer_id,
        filament_plan=request.filament_plan,
        choices=request.choices,
        path=request.path,
        part_settings=request.part_settings,
        prime_tower=request.prime_tower,
        supports=request.supports,
        print_sequence=request.print_sequence,
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
        return await self._start(request, subject, output_id=output_id)

    async def start_library(
        self, file_id: int, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        """A library file item's plates (#5544), read as the dialog's background slice
        of a library file reads it (``bambuddy/operations.py`` ``preview_run``)."""
        return await self._start(request, subject, file_id=file_id)

    async def _start(
        self,
        request: SliceQuoteRequest,
        subject: str,
        *,
        output_id: str | None = None,
        file_id: int | None = None,
    ) -> list[PreviewStarted]:
        core = self.core
        settings = await asyncio.to_thread(core.settings_store.load)
        body = preview_request(request)
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
            asked=body.path,
        )
        lan = route is not None and route.native
        native_reads = await self.components.get(PRINT_LIBRARY).serving_native()
        slicing = self.components.get(SLICING)
        if file_id is not None:
            return await start_library_quote_preview(
                file_id,
                settings,
                core.uploads,
                body,
                lan=lan,
                subject=subject,
                native=slicing.starter,
                native_slices=slicing.store,
                temporal=slicing.temporal(),
                native_reads=native_reads,
                route=route,
            )
        assert output_id is not None
        outputs = LocalOutputs(core.outputs, core.catalogue)
        meta = await require(outputs, output_id)
        naming = await outputs.naming(meta)
        # The run's refusal (#5649): never a price for a print the run refuses.
        if naming.invalid_meta is not None:
            raise invalid_meta(naming.invalid_meta)
        source = OutputSource(
            store=outputs,
            meta=meta,
            uploads=core.uploads,
            settings=settings,
            # The quote's copy goes to the inbox, as the dialog's does.
            stem=None,
            print_settings=naming.print_settings,
            part_settings=naming.part_settings,
            # A native run's copy goes into the native library (D6).
            native=native_reads if lan else None,
        )
        return await start_quote_preview(
            source,
            settings,
            core.uploads,
            body,
            lan=lan,
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
        job = await quote_preview_job(settings, self.core.uploads, started.job_id, subject)
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
        found = await download_sliced(settings, sliced.file_id, MAX_SLICED_BYTES)
        if found is None:
            raise _too_big(sliced.plate_id)
        return found


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


def filled(sent: Mapping[str, Any], setup: Mapping[str, Any] | None) -> dict[str, Any]:
    """An item print's request (its operation's ``request``, dumped with
    ``exclude_unset``) with each part of the quote's print setup it left out (#4673,
    ``printing.quoted_setup``; a null one stays out, as the request's default). A null
    ``choices`` or ``filament_plan`` sent is left out (``printing.sent_keys``)."""
    kept = sent_keys(sent)
    quoted = {
        key: value for key, value in (setup or {}).items() if key not in kept and value is not None
    }
    return {**quoted, **{key: value for key, value in sent.items() if key in kept}}


def print_body(
    request: Mapping[str, Any],
    copies: int,
    project_id: int | None = None,
    filament_plan: Mapping[str, Any] | None = None,
    part_settings: Mapping[str, Any] | None = None,
    setup: Mapping[str, Any] | None = None,
) -> PrintRunRequest:
    """The print run's request for one ``order_item_print`` (its operation's
    ``request``): §5.4's quantity still to print, of the whole output (a quote is the
    whole output's price), keyed by the operation so a retry or a re-send repeats its
    run. ``project_id`` is the item's Bambuddy project (§8.1), which files the print in
    place of any project or folder the request named; ``filament_plan`` the check's
    filled plan (§5.4 "Spools"), in place of the request's. ``part_settings`` are the
    quote's settings per slot (#5571), which a request that sends none prints with, so
    the print slices what the quote priced; ``setup`` the quote's print setup and spools
    (#4673, ``printing.quoted_setup``), each taken when the request sent none
    (:func:`filled`)."""
    body: dict[str, Any] = {
        **filled(request["request"], setup),
        "copies": copies,
        "all_plates": True,
        "plate_ids": None,
        "request_id": request["request_id"],
    }
    if filament_plan is not None:
        body["filament_plan"] = filament_plan
    if project_id is not None:
        body = {**body, "project_id": project_id, "folder_id": None}
    run = PrintRunRequest.model_validate(body)
    # One that sets nothing (`{}`, #5627) is none sent too: it prints the quote's. The
    # field's validator (`_part_settings_set`) already makes it `None`.
    if run.part_settings is None and part_settings is not None:
        # Validated as the request's would be, so the run keys them alike.
        run = PrintRunRequest.model_validate({**body, "part_settings": part_settings})
    return run


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


async def claimed_run(
    runs: PrintCommands,
    subject: PrintSubject,
    request: Mapping[str, Any],
    copies: int,
    projects: Sequence[int | None],
    filament_plan: Mapping[str, Any] | None = None,
    part_settings: Mapping[str, Any] | None = None,
    setup: Mapping[str, Any] | None = None,
) -> PrintRun | None:
    """The run an ``order_item_print`` request made, keyed with any of ``projects``
    (:func:`existing_run` for each, in order), the check's ``filament_plan`` and the
    quote's ``part_settings`` and ``setup``."""
    for project_id in projects:
        body = print_body(request, copies, project_id, filament_plan, part_settings, setup)
        run = await existing_run(runs, subject, body)
        if run is not None:
            return run
    return None


def order_kinds(core: Core, components: Components) -> list[OperationKind]:
    """The orders' kinds, bound to this process's state."""

    def served_library() -> ServedLibrary:
        """The library that serves, for a library file item (#5544)."""
        return ServedLibrary(core.settings_store, components.get(PRINT_LIBRARY).serving_native)

    async def library_file_stands(item_id: str) -> None:
        """409 ``library_file_gone`` for a library file item whose file is gone, in a
        check, before the 202."""
        item = await asyncio.to_thread(components.get(ORDERS).store.item, item_id)
        file = None if item is None else item.library_file
        if item is not None and file is not None and not await served_library().exists(file.id):
            raise library_file_gone(item.id, file)

    async def quote_check_kind(request: dict[str, Any]) -> dict[str, Any]:
        orders = components.get(ORDERS)
        body = SliceQuoteRequest.model_validate(request["request"])
        _, policy, _ = await asyncio.to_thread(orders.price_book.policy)
        # The printer's own machine rate (#4625), stored: the book prices nothing only
        # when that rate is 0 too.
        rate = await asyncio.to_thread(
            orders.machine_rates.check_rate, body.printer, body.printer_id
        )
        if rate is not None:
            policy = policy.model_copy(update={"machine_rate_per_hour": rate})
        slots = await asyncio.to_thread(
            lambda: quote_check(
                orders.store,
                policy,
                request["order_id"],
                request["item_id"],
                body,
                expected_revision_id=request.get("revision_id"),
            )
        )
        await asyncio.to_thread(support_check, orders.store, request["item_id"], body)
        await library_file_stands(request["item_id"])
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
            library=served_library(),
            machine_rate=orders.machine_rates.for_quote,
        )
        try:
            quote = await quote_run(
                run,
                request["order_id"],
                request["item_id"],
                SliceQuoteRequest.model_validate(request["request"]),
                Actor.model_validate(request["by"]),
                quote_id=operation_quote_id(OPERATION_RUN.get()),
                expected_revision_id=request.get("revision_id"),
            )
        except QUOTE_RETRYABLE:
            raise
        except QUOTE_DETERMINISTIC as error:
            # Recorded as the unexpected failure, as before; only not tried again.
            raise ApplicationError(
                type(error).__name__, type=type(error).__name__, non_retryable=True
            ) from error
        dumped: dict[str, Any] = quote.model_dump(mode="json")
        return dumped

    async def item_print_check(request: dict[str, Any]) -> dict[str, Any]:
        """§5.4's refusals (``orders/printing.py`` ``print_check``; a test print's,
        ``test_print_check``, when the request names its ``revision_id``), then the
        printer the run would refuse, as a quote's check refuses it."""
        orders = components.get(ORDERS)
        sent = request["request"]
        # Validated once (#5720 review): choices and spools may be the quote's (#4673).
        asked = ItemPrintRequest.model_validate(sent)
        setup = sent_setup(asked, sent_keys(sent))
        revision_id = request.get("revision_id")
        if revision_id is not None:
            checked = await test_print_check(
                orders.lifecycle,
                core.outputs,
                core.store.blobs,
                request["order_id"],
                request["item_id"],
                revision_id,
                schema_of=lambda reference: reference_schema(core, reference),
                by=Actor.model_validate(request["by"]),
                part_settings=dumped(asked.part_settings),
                setup=setup,
            )
        else:
            checked = await print_check(
                orders.store,
                core.outputs,
                core.store.blobs,
                request["order_id"],
                request["item_id"],
                schema_of=lambda reference: reference_schema(core, reference),
                library=served_library(),
                part_settings=dumped(asked.part_settings),
                setup=setup,
            )
        # The request as it prints: what it left out is the quote's (`_same_setup` passed).
        body = PrintRunRequest.model_validate(filled(sent, checked.get("setup")))
        # Read once for the printer and the route (#5732 review).
        settings = await asyncio.to_thread(core.settings_store.load)
        if body.printer is not None:
            await registry_printer(
                body.printer, body.printer_id, settings, components.get(PRINTERS).store
            )
        native = await native_run(body, settings)
        if body.path == "native" and "path" not in sent_keys(sent) and not native:
            raise quoted_path_refusal(request["item_id"])
        # §5.4 "Spools" (#4251): each slot's spool from the item's filaments, in the check
        # so a refusal writes nothing, and recorded with it so a retry keys its run as
        # the first try did. On the native path the plan is the request's.
        if not native:
            printer_id = body.printer_id
            if printer_id is None and body.printer is not None:
                row = await asyncio.to_thread(components.get(PRINTERS).store.get, body.printer)
                printer_id = None if row is None else row.bambuddy_printer_id
            plan = await item_plan(
                orders.store,
                orders.integrations,
                request["item_id"],
                body.filament_plan,
                printer_id,
            )
            if plan is not None:
                checked["filament_plan"] = plan.model_dump(mode="json")
        return checked

    async def native_run(body: PrintRunRequest, settings: StoredSettings) -> bool:
        """Whether ``body``'s print takes the native path (no Bambuddy), on the stored
        ``settings`` the check read. A printer the route refuses is not native here: the
        run refuses it itself, in its own words."""
        if not native_backends(settings):
            return False
        printers = components.get(PRINTERS)
        try:
            route = await run_route(
                settings,
                printers.store,
                printers.reports,
                printer=body.printer,
                printer_id=body.printer_id,
                asked=body.path,
            )
        except ApiError:
            return False
        return route.native

    async def item_project_of(request: dict[str, Any], body: PrintRunRequest) -> int | None:
        """The item's Bambuddy project (§8.1), ensured before anything is claimed, so a
        retry's run is keyed as its first try's; None without Bambuddy, and for a print
        on the native path (``body``'s, the quote's setup filled in), which files under
        no project. Bambuddy failing to make it is a 503 the operation tries again."""
        orders = components.get(ORDERS)
        if not orders.integrations.linked:
            return None
        settings = await asyncio.to_thread(core.settings_store.load)
        if native_backends(settings):
            printers = components.get(PRINTERS)
            try:
                route = await run_route(
                    settings,
                    printers.store,
                    printers.reports,
                    printer=body.printer,
                    printer_id=body.printer_id,
                    asked=body.path,
                )
            except ApiError:
                # The run refuses it itself, with its own words.
                return None
            if route.native:
                return None
        try:
            return await item_project(
                orders.store, orders.integrations, request["order_id"], request["item_id"]
            )
        except Exception as error:
            logger.warning("could not make the item's Bambuddy project", exc_info=True)
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Bambuddy could not make the item's project; the print is tried again",
                # Transient (`is_transient`), or the operation would never try it again.
                type_=UNAVAILABLE_PROBLEM,
            ) from error

    async def item_print_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The item claimed, then its print started as the print route starts one
        (``accept_run``), then the run attached (``orders/printing.py`` ``print_item``);
        a test print of the request's ``revision_id`` when it names one."""
        # Here, not at the top: the orders package imports nothing of `api/` as it loads
        # (`orders/__init__.py`); this run is the one place it reaches the print route.
        from scadbuddy.activity.component import ACTIVITY
        from scadbuddy.api.library_print import start_library_run
        from scadbuddy.api.printing import accept_run
        from scadbuddy.workflows.print_models import SourceSpec

        # A library file item (#5544) prints its file, as `POST /print/library/{id}/run`.
        file_id: int | None = checked.get("library_file_id")
        output_id: str | None = checked.get("output_id")
        if file_id is not None:
            subject = PrintSubject.library(file_id)
        else:
            assert output_id is not None
            subject = PrintSubject.output(output_id)
        # The check's filled plan (§5.4 "Spools"); one checked before it has none.
        plan = checked.get("filament_plan")
        # The quote's settings per slot (#5571); one checked before #5571 has none.
        quoted_settings = checked.get("part_settings")
        # The quote's print setup and spools (#4673); one checked before has less or none.
        setup = checked.get("setup")
        project_id = await item_project_of(
            request, PrintRunRequest.model_validate(filled(request["request"], setup))
        )
        store = components.get(ORDERS).store

        async def keyed() -> tuple[bool, list[int | None]]:
            """Whether the claim recorded its project, and the projects its run may be
            keyed with (#5263). A claim recorded the one it was made with, which a retry
            keeps even once a deleted project is made again. One from before recorded
            none: its run was keyed with no project (before #5231) or with the item's."""
            recorded = await asyncio.to_thread(store.claim_project, request["request_id"])
            if recorded is not None and recorded[0]:
                return True, [recorded[1]]
            return False, list(dict.fromkeys([project_id, None]))

        async def existing(copies: int) -> PrintRun | None:
            _, projects = await keyed()
            return await claimed_run(
                core.print_runs, subject, request, copies, projects, plan, quoted_settings, setup
            )

        async def start(copies: int) -> PrintRun:
            recorded, projects = await keyed()
            if not recorded:
                # A claim from before #5263, retried: its run may be under either key.
                found = await claimed_run(
                    core.print_runs,
                    subject,
                    request,
                    copies,
                    projects,
                    plan,
                    quoted_settings,
                    setup,
                )
                if found is not None:
                    return found
            if file_id is not None:
                # The library print route's own start (#5544): its subject, its slug and
                # the file's filament slots, read as that route reads them (#2550).
                started = await start_library_run(
                    file_id,
                    print_body(request, copies, projects[0], plan, quoted_settings, setup),
                    runs=core.print_runs,
                    activity=components.get(ACTIVITY),
                    store=core.settings_store,
                    paths=core.paths,
                    budget=budget_of(core.settings),
                    downloads=core.store.downloads,
                    native=await components.get(PRINT_LIBRARY).serving_native(),
                    # #5602: as the output's run below.
                    api=True if request.get("api") else None,
                )
                assert isinstance(started, PrintRun)
                return started
            run = await accept_run(
                core.print_runs,
                Response(),
                subject=subject,
                slug=checked["slug"],
                request=print_body(request, copies, projects[0], plan, quoted_settings, setup),
                source=SourceSpec(kind="output", output_id=output_id),
                activity=components.get(ACTIVITY),
                # #5602: the route's `asked_by_api`; else the run's author tells.
                api=True if request.get("api") else None,
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
            project_id=project_id,
            revision_id=request.get("revision_id"),
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
                    status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.label}"
                )
        # A request recorded before attempts had files names none (#4251).
        fulfilment_id = request.get("fulfilment_id")
        if fulfilment_id is not None:
            attempt = await asyncio.to_thread(store.fulfilment, fulfilment_id)
            if attempt is None or attempt.order_item_id != item_id:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND, f"no attempt {fulfilment_id!r} of that item"
                )
        revision_id = request.get("revision_id")
        if revision_id is not None:
            revision = await asyncio.to_thread(store.revision, revision_id)
            if revision is None or revision.item_id != item_id:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND, f"no revision {revision_id!r} of that item"
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
                    fulfilment_id=request.get("fulfilment_id"),
                    revision_id=request.get("revision_id"),
                )
            )
        except LifecycleError as error:
            raise refused(error) from None
        dumped: dict[str, Any] = attachment.model_dump(mode="json")
        return dumped

    async def copy_check_kind(request: dict[str, Any]) -> dict[str, Any]:
        """§8.2's copy refused from the rows alone (nothing written, Bambuddy not asked)."""
        orders = components.get(ORDERS)
        await copy_check(
            orders.store,
            orders.integrations,
            request["order_id"],
            request["item_id"],
            request["fulfilment_id"],
            request["archive_id"],
        )
        return {}

    async def copy_run_kind(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The archive's file pulled once into the claim store, kept as its blob and
        recorded as the attempt's attachment, listed under its item
        (``orders/archives.py`` ``copy_in``)."""
        orders = components.get(ORDERS)
        try:
            attachment = await copy_in(
                store=orders.store,
                lifecycle=orders.lifecycle,
                integrations=orders.integrations,
                blobs=core.store.blobs,
                claims=ClaimStore(core.paths.claims),
                order_id=request["order_id"],
                item_id=request["item_id"],
                fulfilment_id=request["fulfilment_id"],
                archive_id=request["archive_id"],
                what=request["what"],
                name=request.get("name"),
                by=Actor.model_validate(request["by"]),
                limit=core.settings.media_upload_max_bytes,
            )
        except StaleBlobError:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "the stored copy of these bytes is not on this store's backend (a"
                " `/store/migrate` in progress?); copy it again once the store has moved",
            ) from None
        except LifecycleError as error:
            raise refused(error) from None
        dumped: dict[str, Any] = attachment.model_dump(mode="json")
        return dumped

    return [
        OperationKind(
            ORDER_ATTEMPT_COPY,
            copy_check_kind,
            copy_run_kind,
            # A retry pulls the same bytes again and finds the attachment it made.
            run_attempts=2,
            queue="library",
            run_timeout=PIN_TIMEOUT,
            where="the item's attachments",
        ),
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
            # A worker that stops mid-run (a rollout: the API pod holds the `library`
            # worker) fails its attempt as a retryable `WorkerShutdown`, or misses its
            # heartbeat; the next attempt runs on the new pod (#5353). Every attempt
            # records under the operation's quote id, so one that finds it recorded
            # answers with it, and a transient Bambuddy failure is tried again too; a
            # bug (`QUOTE_DETERMINISTIC`) is not.
            run_attempts=3,
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
