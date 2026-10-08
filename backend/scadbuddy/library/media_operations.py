"""A model's media writes as operations (#1054, spec 2026-10-01 §4.3, the ``library``
row): upload, caption, order, cover and delete. Each route keeps the refusals that read
only its request (the multipart shape, the sniffed type, the image cap, the caption);
the check makes the ones that read the volume or the database, and the run is the
route's former body.

An upload's file and poster arrive as claims (``operations/claims.py``) named by the
sha256 computed while they streamed; the run links each into a staging directory of
its own for ``Catalogue.add_media`` to move into ``media/``, so the claim stays for its
release.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api import media as media_api
from scadbuddy.api.models import require_model_exists
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import (
    MediaNotFoundError,
    MediaOrderError,
    MediaReadOnlyError,
    ModelNotFoundError,
    ModelRecord,
    TooManyMediaError,
)
from scadbuddy.library.media import MAX_MEDIA_ITEMS, MEDIA_UPLOAD_PREFIX, StagedMedia
from scadbuddy.library.operations import PIN_TIMEOUT, answered_as_routes
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind, to_thread_to_end

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState


def _too_many() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, f"a template holds at most {MAX_MEDIA_ITEMS} media items"
    )


def media_kinds(state: AppState) -> list[OperationKind]:
    """The media kinds, bound to this process's state; ``library/operations.py``
    exports them with the pins."""

    def _record(record: ModelRecord) -> dict[str, Any]:
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    def _changed(slug: str, record: ModelRecord) -> dict[str, Any]:
        emit(state.events, ModelEvent(kind="model.updated", slug=slug))
        return _record(record)

    async def exists_check(request: dict[str, Any]) -> dict[str, Any]:
        require_model_exists(state.catalogue, request["slug"])
        media_api.require_media_store(state.catalogue)
        return {}

    async def upload_check(request: dict[str, Any]) -> dict[str, Any]:
        await exists_check(request)
        listed = await asyncio.to_thread(state.catalogue.list_media, request["slug"])
        if len(listed) >= MAX_MEDIA_ITEMS:
            raise _too_many()
        return {}

    def _staged(name: str, staged: Path) -> Path:
        """``staged``, linked to the claim ``name``."""
        try:
            ClaimStore(state.paths.claims).link(name, staged)
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this request's upload is no longer held; send it again",
            ) from None
        return staged

    def _add(request: dict[str, Any]) -> ModelRecord:
        slug = request["slug"]
        # A directory, whose mtime is its own: a link shares the claim's, which the
        # staging sweep would take for an upload killed an hour ago while this run
        # waits on the model's lock (review 3e final M2).
        staging = state.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            upload = StagedMedia(
                path=_staged(request["file"], staging / "file"),
                kind=request["kind"],
                extension=request["extension"],
            )
            poster = None
            if request["poster"] is not None:
                poster = StagedMedia(
                    path=_staged(request["poster"], staging / "poster"),
                    kind="image",
                    extension=request["poster_extension"],
                )
            return state.catalogue.add_media(slug, upload, request["caption"], poster)
        except ModelNotFoundError:
            raise media_api.no_model(slug) from None
        except TooManyMediaError:
            raise _too_many() from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def upload_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        # A git commit, and a move of up to a gigabyte: a cancel waits for both to land
        # (review 3e final M3).
        record = await to_thread_to_end(partial(_add, request))
        return _changed(request["slug"], record)

    def _patch(slug: str, item_id: str, caption: str) -> ModelRecord:
        try:
            return state.catalogue.set_caption(slug, item_id, caption)
        except ModelNotFoundError:
            raise media_api.no_model(slug) from None
        except MediaNotFoundError:
            raise media_api.no_item(slug, item_id) from None
        except MediaReadOnlyError:
            raise media_api.read_only(slug, item_id) from None

    async def patch_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        record = await asyncio.to_thread(_patch, slug, request["item_id"], request["caption"])
        return _changed(slug, record)

    def _order(slug: str, ids: list[str]) -> ModelRecord:
        try:
            return state.catalogue.reorder(slug, ids)
        except ModelNotFoundError:
            raise media_api.no_model(slug) from None
        except MediaOrderError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    async def order_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        return _changed(slug, await asyncio.to_thread(_order, slug, request["ids"]))

    def _cover(slug: str, item_id: str | None) -> ModelRecord:
        try:
            return state.catalogue.set_cover(slug, item_id)
        except ModelNotFoundError:
            raise media_api.no_model(slug) from None
        except MediaNotFoundError:
            raise media_api.no_item(slug, item_id or "") from None
        except MediaOrderError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None

    async def cover_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        return _changed(slug, await asyncio.to_thread(_cover, slug, request["id"]))

    def _delete(slug: str, item_id: str) -> ModelRecord:
        try:
            return state.catalogue.remove_media(slug, item_id)
        except ModelNotFoundError:
            raise media_api.no_model(slug) from None
        except MediaNotFoundError:
            raise media_api.no_item(slug, item_id) from None
        except MediaReadOnlyError:
            raise media_api.read_only(slug, item_id) from None

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        return _changed(slug, await asyncio.to_thread(_delete, slug, request["item_id"]))

    def kind(name: str, check: Any, run: Any, **options: Any) -> OperationKind:
        return OperationKind(
            name,
            answered_as_routes(check),
            answered_as_routes(run),
            queue="library",
            where="the template's media",
            **options,
        )

    return [
        # Moves up to a gigabyte into `media/` and commits.
        kind("model_media_upload", upload_check, upload_run, run_timeout=PIN_TIMEOUT),
        kind("model_media_patch", exists_check, patch_run),
        kind("model_media_order", exists_check, order_run),
        kind("model_media_cover", exists_check, cover_run),
        kind("model_media_delete", exists_check, delete_run),
    ]
