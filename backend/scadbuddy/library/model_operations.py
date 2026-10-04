"""A model's lifecycle as operations (#1054, spec 2026-10-01 §4.3, the ``library`` row):
create, import, patch, duplicate and delete. Each route keeps the refusals that read only
its request; the kind's check makes the ones that read the volume, and its run is the
route's former body.

A create's source, thumbnail and README, a patch's presets and an import's URL arrive
as claims (``operations/claims.py``), never as workflow payloads.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api import library_pins
from scadbuddy.api import models as models_api
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelMeta, ModelPatch, ModelRecord
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.library.operations import PIN_TIMEOUT, answered_as_routes
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState


def model_kinds(state: AppState) -> dict[str, OperationKind]:
    """The model kinds, bound to this process's state."""

    def _record(record: ModelRecord) -> dict[str, Any]:
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    async def _claimed(name: str | None) -> bytes | None:
        if name is None:
            return None
        try:
            return await asyncio.to_thread(ClaimStore(state.paths.claims).get, name)
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this request's upload is no longer held; send it again",
            ) from None

    async def create_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api._require_new(state.catalogue, request["slug"])
        return {}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        source = await _claimed(request["source"])
        assert source is not None  # every create claims its source
        readme = await _claimed(request["readme"])
        async with library_pins.pinned_at_create(
            request["libraries"],
            ModelMeta.model_validate(request["meta"]),
            libraries=state.libraries,
            installs=state.installs,
            checkouts=state.checkouts,
        ) as meta:
            record = await models_api._create(
                state.catalogue,
                state.config,
                state.checks,
                state.events,
                slug=request["slug"],
                source=source.decode(),
                meta=meta,
                force=request["force"],
                thumbnail=await _claimed(request["thumbnail"]),
                readme=None if readme is None else readme.decode(),
                fetcher=CheckoutFetcher(state.libraries, state.installs, state.checkouts)
                if request["fetch"]
                else None,
            )
        return _record(record)

    async def import_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api._require_import_permit(state.imports)
        return {}

    async def import_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        url = await _claimed(request["url"])
        assert url is not None  # every import claims its URL
        body = models_api.UrlImport.model_validate({**request, "url": url.decode()})
        return _record(await models_api.import_url(body, state))

    async def patch_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.to_thread(models_api.require_model, state.catalogue, request["slug"])
        return {}

    async def patch_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        fields = request["patch"]
        presets = await _claimed(request["presets"])
        if presets is not None:
            fields = {**fields, "presets": json.loads(presets)}
        patch = ModelPatch.model_validate(fields)
        return _record(await models_api.patch_template(request["slug"], patch, state))

    async def duplicate_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api.require_model_exists(state.catalogue, request["slug"])
        models_api._require_new(state.catalogue, models_api._slug_from_name(request["name"]))
        return {}

    async def duplicate_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        record = await asyncio.to_thread(
            models_api.duplicate_template, request["slug"], request["name"], state
        )
        return _record(record)

    async def delete_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.to_thread(
            models_api.refuse_delete,
            request["slug"],
            state.catalogue,
            state.render,
            request["force"],
        )
        return {}

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        await models_api.delete_template(request["slug"], request["force"], state)
        return {}

    def kind(name: str, check: Any, run: Any, **options: Any) -> OperationKind:
        return OperationKind(
            name, answered_as_routes(check), answered_as_routes(run), queue="library", **options
        )

    kinds = [
        # A create may clone the libraries it names, as a pin does.
        kind("model_create", create_check, create_run, run_timeout=PIN_TIMEOUT),
        kind("model_import", import_check, import_run),
        kind("model_patch", patch_check, patch_run),
        kind("model_duplicate", duplicate_check, duplicate_run),
        kind("model_delete", delete_check, delete_run),
    ]
    return {each.name: each for each in kinds}
