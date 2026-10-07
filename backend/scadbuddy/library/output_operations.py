"""Saving, re-covering and deleting an output as operations (#1054, spec 2026-10-01
§4.3, the ``library`` row). Each route keeps the refusals that read only its request;
the check makes the ones that read the volume, the render's job or the blob store,
and the run is the route's former body.

An output delete with ``delete_inbox_copies`` deletes the Bambuddy inbox copies in its
run, before the files (plan 3e Ruling 4): §4.3 puts that part on ``bambuddy``, which is
acceptable while both workers run in the API process. #1060, which moves ``bambuddy``
out, splits it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

import psycopg
from fastapi import status

from scadbuddy.api import outputs as outputs_api
from scadbuddy.api.jobs import require_job
from scadbuddy.api.models import require_model_exists
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.send import delete_inbox_copies as remove_inbox_copies
from scadbuddy.bambuddy.uploads import DatabaseRequiredError
from scadbuddy.core.events import OutputEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.operations import answered_as_routes
from scadbuddy.library.outputs import hold_parts, release_parts, require_output
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.render.inputs import InputsDisagreeError, InputsError, normalize_inputs
from scadbuddy.render.job_models import Job, PipelineOutput
from scadbuddy.store.cache import materialize_result
from scadbuddy.workflows.models import ArrangeInputs

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)


def output_kinds(state: AppState) -> list[OperationKind]:
    """The output kinds, bound to this process's state; ``library/operations.py``
    exports them with the pins."""

    def _gone(job: Job) -> ApiError:
        return ApiError(status.HTTP_404_NOT_FOUND, f"the result of job {job.id!r} is gone")

    async def _done(slug: str, job_id: str) -> Job:
        """The job, done and of ``slug``."""
        job = await asyncio.to_thread(require_job, state.render, job_id)
        if job.slug != slug:
            raise ApiError(
                status.HTTP_409_CONFLICT, f"job {job.id!r} rendered {job.slug!r}, not {slug!r}"
            )
        if job.state != "done" or job.result is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"job {job.id!r} is {job.state}, so there is nothing to save",
            )
        return job

    async def _done_with(slug: str, job_id: str, index: int) -> tuple[Job, PipelineOutput | None]:
        """``_done``, and which of a pipeline job's outputs ``index`` names (§5.2): None
        for a job with one result."""
        job = await _done(slug, job_id)
        count = len(job.outputs) or 1
        if index >= count:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"job {job.id} has no output {index}"
            )
        return job, job.outputs[index] if job.outputs else None

    async def create_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        await _done_with(request["slug"], request["job_id"], request["index"])
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        index: int = request["index"]
        job, chosen = await _done_with(request["slug"], request["job_id"], index)
        inputs: dict[str, Any] | None = None
        if request["inputs"] is not None:
            rendered = f"inputs.params are not the parameters job {job.id} rendered"
            # One pass: the shape checks, then the typed comparison with what the job
            # rendered (12.0 is not 12, True is not 1), which skips a job with no params.
            try:
                inputs = normalize_inputs(request["inputs"], job.params).data
            except InputsDisagreeError:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, rendered) from None
            except InputsError as error:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
            # A template's own pipeline read the job's whole inputs (§3.4), so the output
            # records exactly those (§8.4), not only matching params.
            own_pipeline = chosen is not None and chosen.record.pipeline_version != "default"
            if own_pipeline and inputs != job.inputs:
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"inputs are not the ones job {job.id} rendered",
                )
            # The job with no params: nothing was compared above, so compare here.
            if inputs["params"] != job.params:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, rendered)
        result = chosen.result if chosen is not None else job.result
        assert result is not None  # `_done` refuses a job without one
        blobs = state.store.blobs
        # The copy reads the job's files, which on the bambuddy backend come through the
        # cache. Not in the check: a fetch from Bambuddy may take longer than its budget.
        await materialize_result(blobs, result)
        # A piece the store no longer has (aged out, or the Bambuddy store unreachable)
        # is not fetched, and the copy would fail with a server path in its message.
        files = (result.model_3mf, result.preview_glb)
        if not all((state.outputs.paths.root / name).is_file() for name in files):
            raise _gone(job)
        files_dir = None
        if chosen is not None and chosen.files_key is not None:
            if not await blobs.fetch(chosen.files_key):
                # Before `create`, so a refused save leaves nothing under outputs/.
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    "the job's extra files are no longer in the store; render again",
                )
            files_dir = blobs.dir_for(chosen.files_key) / "files"
        public_url = (await asyncio.to_thread(state.settings_store.load)).public_url
        # The Parts outlive the job that rendered them: Arrange reads them later (§7).
        # They are held before the write, so a failed hold leaves nothing saved to retry
        # over; inside the `try`, so a hold that fails part way is released too.
        output_id = uuid.uuid4().hex
        manifest = chosen.manifest if chosen is not None else []
        # An arranged output has no template inputs to reopen; it records its sources (§7).
        arranged = job.kind == "arrange"
        sources = ArrangeInputs.model_validate(job.inputs).sources if arranged else []
        try:
            await asyncio.to_thread(hold_parts, state.refs, output_id, manifest, job.slug)
            meta = await asyncio.to_thread(
                state.outputs.create,
                job,
                name=request["name"],
                public_url=public_url,
                inputs={} if arranged else inputs,
                index=index,
                files_dir=files_dir,
                arranged_from=sources,
                output_id=output_id,
            )
        except Exception as error:
            # Not on cancellation: the write's thread cannot be stopped and may still
            # finish, and an output written without its holds loses its Parts at the
            # next sweep.
            try:
                await asyncio.to_thread(release_parts, state.refs, output_id)
            except psycopg.Error:
                logger.exception("could not release a failed save's Parts", extra={"id": output_id})
            if isinstance(error, OSError):
                # Evicted or swept after the check above, or unreadable: the same answer,
                # not the copy's path. Only after the release, so this path leaks no holds.
                raise _gone(job) from None
            raise
        emit(state.events, OutputEvent(kind="output.created", output_id=meta.id, slug=meta.slug))
        # A new output has no uploads yet: no read to make.
        detail = outputs_api.detail(state.outputs, meta, [])
        dumped: dict[str, Any] = detail.model_dump(mode="json")
        return dumped

    async def output_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.to_thread(require_output, state.outputs, request["output_id"])
        return {}

    async def thumbnail_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        output_id = request["output_id"]
        try:
            png = await asyncio.to_thread(ClaimStore(state.paths.claims).get, request["png"])
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this request's upload is no longer held; send it again",
            ) from None
        await asyncio.to_thread(require_output, state.outputs, output_id)
        await asyncio.to_thread(state.outputs.write_thumbnail, output_id, png)
        return {}

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        output_id = request["output_id"]
        meta = await asyncio.to_thread(require_output, state.outputs, output_id)
        if request["delete_inbox_copies"] and await state.uploads.for_output(meta.id):
            settings = await asyncio.to_thread(state.settings_store.load)
            async with client_for(settings) as client:
                await remove_inbox_copies(client, state.uploads, meta, settings)
        await asyncio.to_thread(state.outputs.delete, output_id)
        # After the files: a failed delete keeps the output, and so must keep its records.
        # Best effort once the files are gone, as for a deleted model: the output is. Each
        # on its own, so a failed upload cleanup cannot leave links serving its archives.
        try:
            await state.uploads.delete_outputs([output_id])
        except (DatabaseRequiredError, psycopg.Error):
            logger.exception(
                "could not forget a deleted output's Bambuddy uploads", extra={"id": output_id}
            )
        try:
            await state.print_links.delete_outputs([output_id])
        except (DatabaseRequiredError, psycopg.Error):
            logger.exception(
                "could not forget a deleted output's print links", extra={"id": output_id}
            )
        # Its Parts go with it, or no sweep ever removes them (blob_refs, §7).
        try:
            await asyncio.to_thread(release_parts, state.refs, output_id)
        except psycopg.Error:
            logger.exception("could not release a deleted output's Parts", extra={"id": output_id})
        emit(state.events, OutputEvent(kind="output.deleted", output_id=meta.id, slug=meta.slug))
        return {}

    def kind(name: str, check: Any, run: Any, where: str) -> OperationKind:
        return OperationKind(
            name, answered_as_routes(check), answered_as_routes(run), queue="library", where=where
        )

    return [
        kind("output_create", create_check, create_run, "the template's outputs"),
        kind("output_thumbnail", output_check, thumbnail_run, "the output"),
        kind("output_delete", output_check, delete_run, "the output and Bambuddy's inbox folder"),
    ]
