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
from scadbuddy.library.outputs import require_output
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.render.inputs import InputsDisagreeError, InputsError, normalize_inputs
from scadbuddy.render.job_models import Job
from scadbuddy.store.cache import materialize_result

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

    async def _finished(slug: str, job_id: str) -> Job:
        """``_done``, with the job's files on this volume: the copy reads them, which
        on the bambuddy backend come through the cache. Not in the check: a fetch from
        Bambuddy may take longer than a check's budget."""
        job = await _done(slug, job_id)
        assert job.result is not None  # `_done` refuses a job without one
        await materialize_result(state.store.blobs, job.result)
        # A piece the store no longer has (aged out, or the Bambuddy store unreachable)
        # is not fetched, and the copy would fail with a server path in its message.
        files = (job.result.model_3mf, job.result.preview_glb)
        if not all((state.outputs.paths.root / name).is_file() for name in files):
            raise _gone(job)
        return job

    async def create_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        await _done(request["slug"], request["job_id"])
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        job = await _finished(request["slug"], request["job_id"])
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
            # The job with no params: nothing was compared above, so compare here.
            if inputs["params"] != job.params:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, rendered)
        public_url = (await asyncio.to_thread(state.settings_store.load)).public_url
        try:
            meta = await asyncio.to_thread(
                state.outputs.create,
                job,
                name=request["name"],
                public_url=public_url,
                inputs=inputs,
            )
        except OSError:
            # Evicted or swept after the check above, or unreadable: the same answer,
            # not the copy's path.
            raise _gone(job) from None
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
            settings = state.settings_store.load()
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
