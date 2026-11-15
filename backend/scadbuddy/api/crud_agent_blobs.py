"""The assistant's session images, attachments and saved HTTP bodies behind CRUD routes
(#5299 step 7): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only
(`crud_router`). Each route is one method of the agent's store and runs what that method
ran over SQL, a transaction as one transaction (#5299 D2), so the TypeScript stores keep
their interfaces. The agent uses an area's routes once ``GET …/agent/areas`` lists it, and
its own SQL until then (D9). Bytes travel as bytes, never base64 (D8, #2479): a write is
``multipart/form-data`` (a ``meta`` JSON part and the byte parts), a read answers the bytes
as its body.

- area ``session-blobs`` (``agent/src/sessions/blobs.ts`` ``SessionBlobs``): ``POST
  …/session-blobs`` stores a session's images (one already there is left as it is), with
  ``tool_use_id`` as a durable tool call's images (``ai_session_tool_images``, replaced
  whole), and with ``fence`` only while the session's row still names that turn attempt
  (``turnHost.ts``'s hold, checked ``FOR SHARE`` in the same transaction); ``GET
  …/session-blobs`` one image by session and name, a fork's through its chain (an
  ancestor's only when an event the fork reads names it).
- area ``attachments`` (``attachments/store.ts`` ``AttachmentStore``): ``POST
  …/attachments`` stages an image and its preview for an owner under the owner's lock and
  caps; ``POST …/attachments/resolve`` the metadata of the live ones named, ``GET
  …/attachments/{id}/image`` and ``…/preview`` their bytes, ``POST
  …/attachments/deletes`` and ``…/sweeps``. Every read and delete names the owner: another
  owner's attachment reads as unknown.
- area ``http-bodies`` (``sessions/httpBodies.ts`` ``PgHttpBodies``): ``POST …/http-bodies``
  keeps a saved body and prunes all but the newest ``MAX_SAVED``; ``GET
  …/http-bodies/meta`` and ``GET …/http-bodies/bytes`` (``offset``, ``length``: a ranged
  read, never the whole body unless asked).

A name is the bytes' sha256 and the type's extension, checked here again from the bytes,
and every image's signature must be its media type's. An attachment id is a UUID, the only
id placed in a route's path; a session id and a name travel in the query or the body.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: An image's name: lowercase sha256 hex and its type's extension (``BLOB_NAME``).
BLOB_NAME = r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$"
EXTENSION = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
PREVIEW_TYPES = ("image/png", "image/jpeg", "image/webp")
#: One image's largest bytes (``sessions/images.ts`` ``IMAGE_BYTES_MAX``).
IMAGE_BYTES_MAX = 5 * 1024 * 1024 // 4 * 3
#: A preview's largest bytes (``PREVIEW_BYTES_MAX``).
PREVIEW_BYTES_MAX = 48 * 1024
#: The most images one ``session-blobs`` write stores: a tool result's
#: (``RESULT_IMAGES_MAX``, 8) or a turn's images and their previews (``IMAGES_MAX`` 4, twice).
IMAGES_PER_WRITE = 8
#: A tool call's id (``ai_session_tool_images.tool_use_id``).
TOOL_USE_ID_MAX = 256
#: The attachments policy (``attachments/store.ts``).
ATTACHMENT_TTL_MS = 60 * 60 * 1000
OWNER_ROWS_MAX = 24
OWNER_BYTES_MAX = 40 * 1024 * 1024
#: The most attachments one resolve or delete names (``IMAGES_MAX``).
ATTACHMENTS_PER_CALL = 4
#: A saved HTTP body's largest bytes and how many a session keeps
#: (``harness/httpRequest.ts`` ``SAVE_MAX_BYTES``, ``MAX_SAVED``).
HTTP_BODY_BYTES_MAX = 20 * 1024 * 1024
MAX_SAVED = 10
#: A saved body's URL (the one asked, at most 8192 characters, or a redirect's ``Location``)
#: and content type come from headers, which Node caps at 16 KiB by default
#: (https://nodejs.org/api/cli.html#--max-http-header-sizesize).
URL_MAX = 16 * 1024
CONTENT_TYPE_MAX = 16 * 1024
#: The largest ``meta`` part (a saved body's holds its URL and content type), and what
#: multipart framing may add.
META_BYTES_MAX = 48 * 1024
#: The largest offset a ranged read takes: Postgres's ``substring`` takes an ``int4``. The
#: agent's ``http_response_read`` takes the same bound (``agent/src/harness/httpRequest.ts``
#: ``HttpReadInput``).
OFFSET_MAX = 2**31 - 2
FRAMING_BYTES_MAX = 16 * 1024

OwnerKind = Literal["browser", "bearer", "oidc", "anonymous", "flow"]
Name = Annotated[str, Field(pattern=BLOB_NAME)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _unprocessable(detail: str) -> ApiError:
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def sniff(data: bytes) -> str | None:
    """The image type ``data``'s signature names (``sessions/images.ts`` ``sniff``)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_name(media_type: str, data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}.{EXTENSION[media_type]}"


def _checked_image(what: str, media_type: str, data: bytes, cap: int) -> None:
    if not data:
        raise _unprocessable(f"{what} is empty")
    if len(data) > cap:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, f"{what} is larger than {cap} bytes")
    if sniff(data) != media_type:
        raise _unprocessable(f"{what} is not a {media_type} image")


async def _multipart(request: Request, parts: int, bytes_max: int) -> tuple[str, dict[str, bytes]]:
    """The ``meta`` part and up to ``parts`` byte parts, their sum at most ``bytes_max``."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "multipart/form-data":
        raise _unprocessable("a write comes as multipart/form-data")
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise ApiError(status.HTTP_411_LENGTH_REQUIRED, "a write names its Content-Length")
    if int(length) > bytes_max + META_BYTES_MAX + FRAMING_BYTES_MAX:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, f"a write is at most {bytes_max} bytes")
    try:
        form = await request.form(max_files=parts, max_fields=1, max_part_size=META_BYTES_MAX)
    except StarletteHTTPException as error:
        raise _unprocessable(f"the write's body was refused: {error.detail}") from None
    try:
        meta = form.get("meta")
        if not isinstance(meta, str):
            raise _unprocessable("a write has the part meta (JSON)")
        files: dict[str, bytes] = {}
        total = 0
        for key, value in form.multi_items():
            if key == "meta":
                continue
            if not isinstance(value, UploadFile) or key in files:
                raise _unprocessable(f"the part {key[:40]} is not one file")
            data = await value.read(bytes_max - total + 1)
            total += len(data)
            if total > bytes_max:
                raise ApiError(
                    status.HTTP_413_CONTENT_TOO_LARGE, f"a write is at most {bytes_max} bytes"
                )
            files[key] = data
        return meta, files
    finally:
        await form.close()


def _meta[M: BaseModel](model: type[M], raw: str) -> M:
    try:
        return model.model_validate_json(raw)
    except ValidationError as err:
        raise _unprocessable(f"meta: {err.errors(include_url=False)[0]['msg']}") from None


class Stored(BaseModel):
    """Whether the write ran: false only when its fence refused it."""

    stored: bool


# -- session blobs -------------------------------------------------------------------------


class Fence(_Body):
    """A turn placement's hold: the session's row still names this attempt and turn."""

    attempt: str = Field(min_length=1, max_length=500)
    turn_id: str = Field(min_length=1, max_length=500)


class BlobMeta(_Body):
    name: Name
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


class BlobsMeta(_Body):
    session_id: UUID
    #: Each image's part is ``image<index>``.
    images: list[BlobMeta] = Field(max_length=IMAGES_PER_WRITE)
    tool_use_id: str | None = Field(default=None, min_length=1, max_length=TOOL_USE_ID_MAX)
    fence: Fence | None = None


def _insert_blobs(
    conn: psycopg.Connection[Any], session: UUID, images: list[tuple[str, str, bytes]]
) -> None:
    for name, media_type, data in images:
        conn.execute(
            "INSERT INTO ai_session_blobs (session_id, name, media_type, data)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (session_id, name) DO NOTHING",
            (session, name, media_type, data),
        )


@router.post("/session-blobs", name="crud_agent_session_blobs_put")
async def session_blobs_put(request: Request, state: StateDep) -> Stored:
    raw, files = await _multipart(request, IMAGES_PER_WRITE, IMAGES_PER_WRITE * IMAGE_BYTES_MAX)
    meta = _meta(BlobsMeta, raw)
    if meta.tool_use_id is not None and meta.fence is not None:
        raise _unprocessable("a write names a tool_use_id or a fence, not both")
    if set(files) != {f"image{i}" for i in range(len(meta.images))}:
        raise _unprocessable("each image named in meta is the part image<index>")
    images: list[tuple[str, str, bytes]] = []
    for index, image in enumerate(meta.images):
        data = files[f"image{index}"]
        _checked_image(f"images[{index}]", image.media_type, data, IMAGE_BYTES_MAX)
        if image_name(image.media_type, data) != image.name:
            raise _unprocessable(f"images[{index}]: a name is its bytes' sha256 and type")
        images.append((image.name, image.media_type, data))
    fence, tool_use_id = meta.fence, meta.tool_use_id

    def run(conn: psycopg.Connection[Any]) -> bool:
        if fence is not None:
            held = conn.execute(
                "SELECT 1 FROM ai_sessions WHERE id = %s AND turn_attempt = %s"
                " AND durable_turn_id = %s FOR SHARE",
                (meta.session_id, fence.attempt, fence.turn_id),
            ).fetchone()
            if held is None:
                return False
        _insert_blobs(conn, meta.session_id, images)
        if tool_use_id is not None:
            conn.execute(
                "DELETE FROM ai_session_tool_images WHERE session_id = %s AND tool_use_id = %s",
                (meta.session_id, tool_use_id),
            )
            for ord_, (name, _, _) in enumerate(images):
                conn.execute(
                    "INSERT INTO ai_session_tool_images (session_id, tool_use_id, ord, name)"
                    " VALUES (%s, %s, %s, %s)",
                    (meta.session_id, tool_use_id, ord_, name),
                )
        return True

    try:
        return Stored(stored=await _in_tx(state, run))
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(status.HTTP_404_NOT_FOUND, "no such session") from None


#: The events a fork takes that can name an image (``blobs.ts`` ``IMAGE_EVENT_TYPES``).
IMAGE_EVENT_TYPES = ["tool.result", "user.turn"]


@router.get(
    "/session-blobs",
    name="crud_agent_session_blob",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}}, 404: {}},
)
async def session_blob(
    session_id: UUID,
    name: Annotated[str, Query(pattern=BLOB_NAME)],
    state: StateDep,
) -> Response:
    """The image, its media type the answer's; 404 for none. A fork's (#4212) is its own,
    else an ancestor's that an event it reads through its chain names (``eventLog.ts``
    ``forkChain``), so a fork reads no image of its parent's turns past the cut."""

    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "WITH RECURSIVE chain (id, parent, cut, bound, depth) AS ("
            " SELECT id, fork_parent_id, fork_event_cut, NULL::bigint, 0"
            " FROM ai_sessions WHERE id = %(s)s"
            " UNION ALL"
            " SELECT s.id, s.fork_parent_id, s.fork_event_cut, LEAST(c.bound, c.cut), c.depth + 1"
            " FROM ai_sessions s JOIN chain c ON s.id = c.parent WHERE c.cut IS NOT NULL)"
            " SELECT b.media_type, b.data FROM chain c"
            " JOIN ai_session_blobs b ON b.session_id = c.id AND b.name = %(n)s"
            " WHERE c.depth = 0 OR EXISTS ("
            " SELECT 1 FROM chain r JOIN ai_session_events e ON e.session_id = r.id"
            " WHERE r.depth > 0 AND e.seq <= r.bound AND strpos(e.event, %(n)s) > 0"
            " AND e.event::jsonb->>'type' = ANY(%(t)s))"
            " ORDER BY c.depth LIMIT 1",
            {"s": session_id, "n": name, "t": IMAGE_EVENT_TYPES},
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "no such image in the session")
    return Response(content=bytes(row["data"]), media_type=row["media_type"])


# -- attachments ---------------------------------------------------------------------------


class Owner(_Body):
    kind: OwnerKind
    id: str = Field(min_length=1, max_length=1000)


class AttachmentMeta(_Body):
    owner: Owner
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    preview_media_type: Literal["image/png", "image/jpeg", "image/webp"]


class PreviewView(BaseModel):
    name: str
    media_type: str


class AttachmentView(BaseModel):
    id: UUID
    name: str
    media_type: str
    preview: PreviewView


def _lock_key(owner: Owner) -> str:
    return f"scadbuddy:ai_attachments:{owner.kind}:{owner.id}"


@router.post("/attachments", status_code=status.HTTP_201_CREATED, name="crud_agent_attachment_put")
async def attachment_put(request: Request, state: StateDep) -> AttachmentView:
    """Stages ``image`` and ``preview`` for the owner: 409 ``quota`` past the owner's caps,
    the count and the insert under one per-owner lock (``attachments/store.ts`` ``put``)."""
    raw, files = await _multipart(request, 2, IMAGE_BYTES_MAX + PREVIEW_BYTES_MAX)
    meta = _meta(AttachmentMeta, raw)
    if set(files) != {"image", "preview"}:
        raise _unprocessable("an attachment is the parts meta, image and preview")
    image, preview = files["image"], files["preview"]
    _checked_image("the image", meta.media_type, image, IMAGE_BYTES_MAX)
    _checked_image("the preview", meta.preview_media_type, preview, PREVIEW_BYTES_MAX)
    name = image_name(meta.media_type, image)
    attachment_id = uuid4()
    owner = meta.owner

    def run(conn: psycopg.Connection[Any]) -> bool:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_lock_key(owner),))
        conn.execute(
            "DELETE FROM ai_attachments WHERE owner_kind = %s AND owner_id = %s"
            " AND expires_at <= now()",
            (owner.kind, owner.id),
        )
        held = conn.execute(
            "SELECT count(*)::int AS n, coalesce(sum(octet_length(data)), 0)::bigint AS bytes"
            " FROM ai_attachments WHERE owner_kind = %s AND owner_id = %s",
            (owner.kind, owner.id),
        ).fetchone()
        if held is not None and (
            held["n"] + 1 > OWNER_ROWS_MAX or int(held["bytes"]) + len(image) > OWNER_BYTES_MAX
        ):
            return False
        conn.execute(
            "INSERT INTO ai_attachments (id, owner_kind, owner_id, name, media_type, data,"
            " preview_media_type, preview, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + %s::interval)",
            (
                attachment_id,
                owner.kind,
                owner.id,
                name,
                meta.media_type,
                image,
                meta.preview_media_type,
                preview,
                f"{ATTACHMENT_TTL_MS} milliseconds",
            ),
        )
        return True

    if not await _in_tx(state, run):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"too many images waiting to be sent: at most {OWNER_ROWS_MAX} images or"
            f" {OWNER_BYTES_MAX} bytes; send or remove some, or wait for them to expire",
            reason="quota",
        )
    return AttachmentView(
        id=attachment_id,
        name=name,
        media_type=meta.media_type,
        preview=PreviewView(
            name=image_name(meta.preview_media_type, preview), media_type=meta.preview_media_type
        ),
    )


class AttachmentIds(_Body):
    owner: Owner
    ids: list[UUID] = Field(min_length=1, max_length=ATTACHMENTS_PER_CALL)


class Resolved(BaseModel):
    id: UUID
    name: str
    media_type: str
    preview_media_type: str


class ResolvedList(BaseModel):
    """The live attachments of those named that are the owner's; the rest are not listed."""

    attachments: list[Resolved]


@router.post("/attachments/resolve", name="crud_agent_attachments_resolve")
async def attachments_resolve(body: AttachmentIds, state: StateDep) -> ResolvedList:
    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "SELECT id, name, media_type, preview_media_type FROM ai_attachments"
            " WHERE id = ANY(%s) AND owner_kind = %s AND owner_id = %s AND expires_at > now()",
            (body.ids, body.owner.kind, body.owner.id),
        ).fetchall()

    return ResolvedList(attachments=[Resolved(**r) for r in await _in_tx(state, run)])


async def _attachment_bytes(
    state: Any, attachment_id: UUID, kind: OwnerKind, owner_id: str, part: str
) -> Response:
    column, media = ("data", "media_type") if part == "image" else ("preview", "preview_media_type")

    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            f"SELECT {media} AS media_type, {column} AS data FROM ai_attachments"
            " WHERE id = %s AND owner_kind = %s AND owner_id = %s AND expires_at > now()",
            (attachment_id, kind, owner_id),
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "the attachment is unknown or has expired")
    return Response(content=bytes(row["data"]), media_type=row["media_type"])


OwnerIdQuery = Annotated[str, Query(min_length=1, max_length=1000)]
_BYTES: dict[int | str, dict[str, Any]] = {200: {"content": {"image/*": {}}}, 404: {}}


@router.get(
    "/attachments/{attachment_id}/image",
    name="crud_agent_attachment_image",
    response_class=Response,
    responses=_BYTES,
)
async def attachment_image(
    attachment_id: UUID, owner_kind: OwnerKind, owner_id: OwnerIdQuery, state: StateDep
) -> Response:
    return await _attachment_bytes(state, attachment_id, owner_kind, owner_id, "image")


@router.get(
    "/attachments/{attachment_id}/preview",
    name="crud_agent_attachment_preview",
    response_class=Response,
    responses=_BYTES,
)
async def attachment_preview(
    attachment_id: UUID, owner_kind: OwnerKind, owner_id: OwnerIdQuery, state: StateDep
) -> Response:
    return await _attachment_bytes(state, attachment_id, owner_kind, owner_id, "preview")


class Deleted(BaseModel):
    deleted: int


@router.post("/attachments/deletes", name="crud_agent_attachments_delete")
async def attachments_delete(body: AttachmentIds, state: StateDep) -> Deleted:
    def run(conn: psycopg.Connection[Any]) -> int:
        return conn.execute(
            "DELETE FROM ai_attachments WHERE id = ANY(%s) AND owner_kind = %s AND owner_id = %s",
            (body.ids, body.owner.kind, body.owner.id),
        ).rowcount

    return Deleted(deleted=await _in_tx(state, run))


@router.post("/attachments/sweeps", name="crud_agent_attachments_sweep")
async def attachments_sweep(state: StateDep) -> Deleted:
    def run(conn: psycopg.Connection[Any]) -> int:
        return conn.execute("DELETE FROM ai_attachments WHERE expires_at <= now()").rowcount

    return Deleted(deleted=await _in_tx(state, run))


# -- saved HTTP bodies ---------------------------------------------------------------------


class BodyMeta(_Body):
    url: str = Field(max_length=URL_MAX)
    content_type: str | None = Field(max_length=CONTENT_TYPE_MAX)
    size_bytes: int = Field(ge=0)
    complete: bool


class BodySave(BodyMeta):
    session_id: UUID
    id: UUID


@router.post("/http-bodies", name="crud_agent_http_body_save")
async def http_body_save(request: Request, state: StateDep) -> Stored:
    """Keeps a saved body (the part ``body``) and prunes all but the session's newest
    ``MAX_SAVED``, in one transaction (``httpBodies.ts`` ``save``)."""
    raw, files = await _multipart(request, 1, HTTP_BODY_BYTES_MAX)
    meta = _meta(BodySave, raw)
    if set(files) != {"body"}:
        raise _unprocessable("a saved body is the parts meta and body")
    data = files["body"]

    def run(conn: psycopg.Connection[Any]) -> bool:
        conn.execute(
            "INSERT INTO ai_session_http_bodies"
            " (session_id, id, url, content_type, size_bytes, complete, data)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                meta.session_id,
                meta.id,
                meta.url,
                meta.content_type,
                meta.size_bytes,
                meta.complete,
                data,
            ),
        )
        conn.execute(
            "DELETE FROM ai_session_http_bodies WHERE session_id = %(s)s AND id NOT IN ("
            " SELECT id FROM ai_session_http_bodies WHERE session_id = %(s)s"
            " ORDER BY created_at DESC LIMIT %(n)s)",
            {"s": meta.session_id, "n": MAX_SAVED},
        )
        return True

    try:
        return Stored(stored=await _in_tx(state, run))
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(status.HTTP_404_NOT_FOUND, "no such session") from None
    except psycopg.errors.UniqueViolation:
        raise ApiError(status.HTTP_409_CONFLICT, "that body is saved already") from None


class SavedMeta(BaseModel):
    url: str
    content_type: str | None
    size_bytes: int
    complete: bool
    created_at: datetime


@router.get("/http-bodies/meta", name="crud_agent_http_body_meta")
async def http_body_meta(session_id: UUID, id: UUID, state: StateDep) -> SavedMeta:
    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "SELECT url, content_type, size_bytes, complete, created_at"
            " FROM ai_session_http_bodies WHERE session_id = %s AND id = %s",
            (session_id, id),
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "no such saved body")
    return SavedMeta(**row)


@router.get(
    "/http-bodies/bytes",
    name="crud_agent_http_body_bytes",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}, 404: {}},
)
async def http_body_bytes(
    session_id: UUID,
    id: UUID,
    state: StateDep,
    # Any offset `http_response_read` asks: past the end reads nothing, as over SQL.
    offset: Annotated[int, Query(ge=0, le=OFFSET_MAX)] = 0,
    length: Annotated[int, Query(ge=0, le=HTTP_BODY_BYTES_MAX)] = HTTP_BODY_BYTES_MAX,
) -> Response:
    """Up to ``length`` bytes from ``offset``, fewer at the end, none past it; 404 for no
    such body."""

    def run(conn: psycopg.Connection[Any]) -> Any:
        # substring() is 1-based; past the end it is empty.
        return conn.execute(
            "SELECT substring(data FROM %s FOR %s) AS part FROM ai_session_http_bodies"
            " WHERE session_id = %s AND id = %s",
            (offset + 1, length, session_id, id),
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "no such saved body")
    return Response(content=bytes(row["part"]), media_type="application/octet-stream")
