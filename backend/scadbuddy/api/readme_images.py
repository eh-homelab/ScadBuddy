"""``POST /models/{slug}/images``: an image for a template's README (#5475).

The rules are in ``library/readme_images.py``. The route checks the bytes, holds them
by claim check and runs the ``model_image_add`` operation on the ``library`` queue,
as a thumbnail set does, so a retry with the same ``Idempotency-Key`` writes once.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api.deps import AppState, CatalogueDep, PathsDep, SlugPath
from scadbuddy.api.lsp import MAX_MODEL_IMAGE_BYTES
from scadbuddy.api.models import require_mine, require_model_exists
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelNotFoundError, ModelRecord
from scadbuddy.library.readme_images import (
    IMAGES_DIR,
    MAX_README_IMAGES,
    ImagesFolderError,
    ReadmeImageError,
    TooManyReadmeImagesError,
    add_readme_image,
    image_type,
    stored_name,
)
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OperationsDep

router = APIRouter(tags=["models"])

#: The cap as people read it.
MAX_README_IMAGE_SIZE = f"{MAX_MODEL_IMAGE_BYTES / (1024 * 1024):g} MiB"


class ReadmeImage(BaseModel):
    path: str = Field(
        description=(
            "Where the image is stored, relative to the template: `images/<name>`. A README "
            "links it as `![alt](images/<name>)`."
        )
    )
    model: ModelRecord


@router.post(
    "/models/{slug}/images",
    response_model=ReadmeImage,
    summary="Add an image for a model's README",
    description=(
        "Stores an uploaded PNG, JPEG, WebP or GIF image in the template's `images/` "
        "folder, as one revision in its history. The type comes from the bytes; any "
        f"other file is a 422, and so is one over {MAX_README_IMAGE_SIZE}. The name is "
        "the upload's file name made safe: no folder, lower case, letters, digits, `-` "
        "and `_`, and the extension of the type. A name that is taken gets `-2`, `-3` "
        "and on: an image is never written over. A template holds at most "
        f"{MAX_README_IMAGES} images (409). A built-in template is read-only (403)."
    ),
    responses=OPERATION_RESPONSES,
)
async def add_model_image(
    slug: SlugPath,
    response: Response,
    ops: OperationsDep,
    paths: PathsDep,
    catalogue: CatalogueDep,
    file: Annotated[
        UploadFile,
        File(description=f"The image: PNG, JPEG, WebP or GIF, at most {MAX_README_IMAGE_SIZE}"),
    ],
    idempotency_key: IdempotencyKey = None,
) -> ReadmeImage | JSONResponse:
    require_mine(slug)
    require_model_exists(catalogue, slug)
    # One byte past the cap, so a large upload is never read whole.
    data = await file.read(MAX_MODEL_IMAGE_BYTES + 1)
    if len(data) > MAX_MODEL_IMAGE_BYTES:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the image is too large: an image is at most {MAX_MODEL_IMAGE_BYTES} bytes"
            f" ({MAX_README_IMAGE_SIZE})",
        )
    try:
        media_type = await asyncio.to_thread(image_type, data)
    except ReadmeImageError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    name = stored_name(file.filename, media_type)
    # By claim: up to 10 MiB, past a workflow payload's limit (#1054).
    claims = ClaimStore(paths.claims)
    claimed = await asyncio.to_thread(claims.hold, data)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["model_image_add"],
        subject=slug,
        request={"slug": slug, "name": name, "image": claimed.name},
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, [claimed]),
    )
    return operation_answer(result, ReadmeImage)


def image_add_check(slug: str, state: AppState) -> None:
    """The ``model_image_add`` operation's check: a template of mine that exists."""
    require_mine(slug)
    require_model_exists(state.catalogue, slug)


async def image_add_run(slug: str, name: str, data: bytes, state: AppState) -> ReadmeImage:
    """The ``model_image_add`` operation's run."""
    try:
        # `to_thread`: a git commit, from an `async def` handler.
        record, path = await asyncio.to_thread(add_readme_image, state.catalogue, slug, name, data)
    except ModelNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no model named {slug!r}") from None
    except TooManyReadmeImagesError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{slug!r} already holds {MAX_README_IMAGES} images, the most a template may;"
            f" remove one from its {IMAGES_DIR}/ folder first",
        ) from None
    except ImagesFolderError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{slug!r} has an {IMAGES_DIR!r} entry that is not a folder, so no image is added",
        ) from None
    emit(state.events, ModelEvent(kind="model.updated", slug=slug))
    return ReadmeImage(path=path, model=record)
