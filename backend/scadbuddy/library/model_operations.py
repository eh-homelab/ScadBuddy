"""A model's lifecycle as operations (#1054, spec 2026-10-01 §4.3, the ``library`` row):
create, import, patch, duplicate and delete. Each route keeps the refusals that read only
its request; the kind's check makes the ones that read the volume, and its run is the
route's former body.

A create's source, thumbnail and README arrive as claims (``operations/claims.py``),
never as workflow payloads.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api import library_pins, model_files
from scadbuddy.api import models as models_api
from scadbuddy.api import upstream as upstream_api
from scadbuddy.api import versions as versions_api
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
        body = models_api.UrlImport.model_validate(request)
        return _record(await models_api.import_url(body, state))

    async def patch_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.to_thread(models_api.require_model, state.catalogue, request["slug"])
        return {}

    async def patch_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        patch = ModelPatch.model_validate(request["patch"])
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

    async def source_put_check(request: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        models_api.require_model_exists(state.catalogue, slug)
        if request["base"] is not None:
            # The early stale refusal; `write_source` makes it again under the lock.
            current = await asyncio.to_thread(state.catalogue.version, slug)
            models_api._require_base(slug, request["base"], current)
        return {}

    async def source_put_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        source = await _claimed(request["source"])
        assert source is not None  # every save claims its source
        record = await models_api.save_source_run(
            request["slug"],
            source.decode(),
            message=request["message"],
            force=request["force"],
            merge_base=request["merge_base"],
            expected_version=request["base"],
            state=state,
        )
        return _record(record)

    async def source_patch_check(request: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        models_api.require_model_exists(state.catalogue, slug)
        current = await asyncio.to_thread(state.catalogue.version, slug)
        models_api._require_base(slug, request["base"], current)
        return {}

    async def source_patch_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        claimed = await _claimed(request["body"])
        assert claimed is not None  # every patch claims its body
        body = models_api.SourcePatch.model_validate_json(claimed)
        return _record(await models_api.patch_source_run(request["slug"], body, state))

    async def exists_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api.require_model_exists(state.catalogue, request["slug"])
        return {}

    async def _text(name: str) -> str:
        claimed = await _claimed(name)
        assert claimed is not None  # the route claims every text it sends
        return claimed.decode()

    async def thumbnail_put_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        png = await _claimed(request["png"])
        assert png is not None  # every set claims its image
        return _record(await models_api.thumbnail_put_run(request["slug"], png, state))

    async def thumbnail_delete_run(
        request: dict[str, Any], checked: dict[str, Any]
    ) -> dict[str, Any]:
        return _record(await models_api.thumbnail_delete_run(request["slug"], state))

    async def readme_put_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        content = await _text(request["content"])
        return _record(await models_api.readme_put_run(request["slug"], content, state))

    async def readme_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        return _record(await models_api.readme_delete_run(request["slug"], state))

    async def file_put_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        content = await _text(request["content"])
        record = await model_files.file_put_run(
            request["slug"], request["name"], content, request["message"], state
        )
        return _record(record)

    async def file_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        record = await model_files.file_delete_run(request["slug"], request["name"], state)
        return _record(record)

    async def restore_check(request: dict[str, Any]) -> dict[str, Any]:
        await asyncio.to_thread(
            versions_api.restore_check, request["slug"], request["commit"], state
        )
        return {}

    async def restore_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        version = await asyncio.to_thread(
            versions_api.restore_run, request["slug"], request["commit"], state
        )
        dumped: dict[str, Any] = version.model_dump(mode="json")
        return dumped

    async def merge_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        merged = await asyncio.to_thread(upstream_api.merge_run, request["slug"], state)
        dumped: dict[str, Any] = merged.model_dump(mode="json")
        return dumped

    async def dismiss_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        return _record(await asyncio.to_thread(upstream_api.dismiss_run, request["slug"], state))

    async def detach_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        return _record(await asyncio.to_thread(upstream_api.detach_run, request["slug"], state))

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
        # A save parse-checks the source and may clone the checkouts it includes.
        kind("model_source_put", source_put_check, source_put_run, run_timeout=PIN_TIMEOUT),
        kind("model_source_patch", source_patch_check, source_patch_run, run_timeout=PIN_TIMEOUT),
        kind("model_thumbnail_put", exists_check, thumbnail_put_run),
        kind("model_thumbnail_delete", exists_check, thumbnail_delete_run),
        kind("model_readme_put", exists_check, readme_put_run),
        kind("model_readme_delete", exists_check, readme_delete_run),
        kind("model_file_put", exists_check, file_put_run),
        kind("model_file_delete", exists_check, file_delete_run),
        kind("model_restore", restore_check, restore_run),
        kind("model_upstream_merge", exists_check, merge_run),
        kind("model_upstream_dismiss", exists_check, dismiss_run),
        kind("model_upstream_detach", exists_check, detach_run),
    ]
    return {each.name: each for each in kinds}
