"""Forgetting a durable subject, the deletes of keys nothing forgets, and a flow run's images
behind CRUD routes (#5299 step 6): under ``/api/v1/internal/crud/agent/``, for the ``agent``
credential only (`crud_router`), area ``forget``. Each route is one transaction (D2), or one
per subject (the sweep); the SQL is ``agent_data/forget.py`` and ``agent_data/flow_blobs.py``.
The agent's stores (``agent/src/sessions/forget.ts`` ``CrudSubjectKeys``,
``agent/src/temporal/flowBlobs.ts`` ``CrudFlowBlobs``) call these once ``…/areas`` lists the
area, and their own SQL until then (D9).

- ``POST …/forgotten-subjects/{subject}``: forgetSubject's key transaction, under the
  subject's lock: the tombstone, a flow run's images and the key. A session with forks is
  409 ``forks-take-ranges``: the agent copies its forks' ranges and forgets it in its own
  transaction until its sessions move here (#5299 step 9).
- ``POST …/payload-keys/{subject}/drop``: a browser call's key (``bridge-<uuid>`` only),
  deleted once its caller has the answer; no tombstone.
- ``POST …/payload-key-sweeps``: the keys of sessions whose row is gone and of browser calls
  long over, deleted (the agent's hourly sweep).
- ``POST …/flow-blobs/{subject}``: a flow run's images kept, ``multipart/form-data``: the
  part ``images`` a JSON list of ``{name, media_type, size}`` and, in the same order, one
  ``blob`` part per image, its bytes sealed by the agent under the run's key (D8: bytes in
  the body, never base64). Answers the names the run holds; 410 ``subject-forgotten`` for a
  forgotten run.
- ``GET …/flow-blobs/{subject}/{name}``: one image's sealed bytes
  (``application/octet-stream``), its type in ``X-ScadBuddy-Media-Type``; 404
  ``no-such-image``.

The flow run page reads and opens the same rows (``flows/images.py``), and ``DELETE
/workflow-runs/{id}`` forgets a run with the same statements (``flows/forget.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from scadbuddy.agent_data import flow_blobs, forget
from scadbuddy.agent_data.payload_keys import SUBJECT_PATTERN
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows.agent_reads import Pool

router = crud_router("agent")

T = TypeVar("T")

#: A session with forks: they take their ranges of its transcript before it goes.
FORKS_TAKE_RANGES = "forks-take-ranges"
#: The run was forgotten: none of its images is kept.
SUBJECT_FORGOTTEN = "subject-forgotten"
#: No such image of the run.
NO_SUCH_IMAGE = "no-such-image"
#: The header that names a stored image's type.
MEDIA_TYPE_HEADER = "X-ScadBuddy-Media-Type"

BRIDGE_SUBJECT = r"^bridge-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: The most images one put carries (a call keeps at most 8, the agent's ``RESULT_IMAGES_MAX``).
PUT_IMAGES_MAX = 16
#: The most sealed bytes one image may be, and one put altogether.
IMAGE_BYTES_MAX = 32 * 1024 * 1024
PUT_BYTES_MAX = 64 * 1024 * 1024


def _pool(state: Any) -> Pool:
    pool: Pool = state.settings_store.pool
    return pool


async def _in_conn[T](state: Any, fn: Callable[[Any], T]) -> T:
    def run() -> T:
        with _pool(state).connection() as conn:
            return fn(conn)

    return await asyncio.to_thread(run)


# --- forget -----------------------------------------------------------------------------


class Forgotten(BaseModel):
    #: Whether the subject had a key, now deleted.
    key: bool


@router.post("/forgotten-subjects/{subject}", name="crud_agent_forget_subject")
async def forget_subject(
    subject: Annotated[str, Path(pattern=SUBJECT_PATTERN)], state: StateDep
) -> Forgotten:
    if subject.startswith("bridge-"):
        raise ApiError(422, "a browser call's key is dropped, not forgotten")
    try:
        key = await _in_conn(state, lambda conn: forget.forget(conn, subject))
    except forget.ForksTakeRangesError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{subject} has forks, which take their ranges of its transcript first.",
            reason=FORKS_TAKE_RANGES,
        ) from None
    return Forgotten(key=key)


class Dropped(BaseModel):
    #: Whether there was a key.
    dropped: bool


@router.post("/payload-keys/{subject}/drop", name="crud_agent_drop_payload_key")
async def drop_payload_key(
    subject: Annotated[str, Path(pattern=BRIDGE_SUBJECT)], state: StateDep
) -> Dropped:
    return Dropped(
        dropped=await _in_conn(state, lambda conn: forget.drop_bridge_key(conn, subject))
    )


class SweepAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orphan_min_age_s: int = Field(default=forget.ORPHAN_KEY_MIN_AGE_S, ge=60, le=30 * 86400)
    bridge_max_age_s: int = Field(default=forget.BRIDGE_KEY_MAX_AGE_S, ge=60, le=30 * 86400)


class Swept(BaseModel):
    orphaned: int
    bridge: int


@router.post("/payload-key-sweeps", name="crud_agent_sweep_payload_keys")
async def sweep_payload_keys(body: SweepAsk, state: StateDep) -> Swept:
    orphaned, bridge = await _in_conn(
        state,
        lambda conn: forget.sweep(
            conn, orphan_min_age_s=body.orphan_min_age_s, bridge_max_age_s=body.bridge_max_age_s
        ),
    )
    return Swept(orphaned=orphaned, bridge=bridge)


# --- flow images ------------------------------------------------------------------------


class ImageMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=flow_blobs.BLOB_NAME.pattern)
    media_type: str
    size: int = Field(gt=0, le=IMAGE_BYTES_MAX)


_METAS = TypeAdapter(list[ImageMeta])


class Kept(BaseModel):
    #: The names the run holds, of those sent.
    names: list[str]


def _refused(detail: str) -> ApiError:
    return ApiError(422, detail)


async def _sealed_images(request: Request) -> list[flow_blobs.SealedImage]:
    if request.headers.get("content-type", "").split(";")[0].strip() != "multipart/form-data":
        raise _refused("the images come as multipart/form-data")
    try:
        form = await request.form(max_files=PUT_IMAGES_MAX, max_fields=1)
    except StarletteHTTPException as error:
        # Starlette's 400 for a body past the part limits, or one it cannot parse.
        raise _refused(f"the images' body was refused: {error.detail}") from None
    try:
        raw = form.get("images")
        if not isinstance(raw, str):
            raise _refused("the part images is the JSON list of the images")
        try:
            metas = _METAS.validate_python(json.loads(raw))
        except (ValueError, ValidationError):
            raise _refused("the part images is not a list of {name, media_type, size}") from None
        blobs = form.getlist("blob")
        if not 1 <= len(metas) <= PUT_IMAGES_MAX or len(blobs) != len(metas):
            raise _refused(f"1 to {PUT_IMAGES_MAX} images, each with one blob part")
        out: list[flow_blobs.SealedImage] = []
        total = 0
        for meta, blob in zip(metas, blobs, strict=True):
            if flow_blobs.EXTENSIONS.get(meta.media_type) != meta.name.rsplit(".", 1)[1]:
                raise _refused(f"{meta.name} is not named for {meta.media_type}")
            if not isinstance(blob, UploadFile):
                raise _refused("each blob part is a file")
            data = await blob.read(IMAGE_BYTES_MAX + 1)
            total += len(data)
            if not data or len(data) > IMAGE_BYTES_MAX or total > PUT_BYTES_MAX:
                raise _refused(f"an image is 1 to {IMAGE_BYTES_MAX} bytes, a put {PUT_BYTES_MAX}")
            out.append(flow_blobs.SealedImage(meta.name, meta.media_type, meta.size, data))
        return out
    finally:
        await form.close()


@router.post("/flow-blobs/{subject}", name="crud_agent_flow_blobs_put")
async def put_flow_blobs(
    subject: Annotated[str, Path(pattern=flow_blobs.FLOW_SUBJECT_PATTERN)],
    request: Request,
    state: StateDep,
) -> Kept:
    images = await _sealed_images(request)
    names = await _in_conn(state, lambda conn: flow_blobs.put(conn, subject, images))
    if names is None:
        raise ApiError(
            status.HTTP_410_GONE,
            f"{subject} was forgotten; its images are not kept.",
            reason=SUBJECT_FORGOTTEN,
        )
    return Kept(names=names)


@router.get(
    "/flow-blobs/{subject}/{name}",
    name="crud_agent_flow_blob",
    response_class=Response,
    responses={status.HTTP_200_OK: {"content": {"application/octet-stream": {}}}},
)
async def get_flow_blob(
    subject: Annotated[str, Path(pattern=flow_blobs.FLOW_SUBJECT_PATTERN)],
    name: Annotated[str, Path(pattern=flow_blobs.BLOB_NAME.pattern)],
    state: StateDep,
) -> Response:
    found = await _in_conn(state, lambda conn: flow_blobs.get(conn, subject, name))
    if found is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"{subject} has no image {name}.", reason=NO_SUCH_IMAGE
        )
    media_type, data = found
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={MEDIA_TYPE_HEADER: media_type},
    )
