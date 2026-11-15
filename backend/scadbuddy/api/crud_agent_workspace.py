"""The assistant's workspace files behind CRUD routes (#5299 step 7): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`), area
``workspace``. Each write is one transaction (D2), its blob's hold and its events in it;
the SQL and the sealing are ``agent_data/workspace.py``. The API seals and opens every
file here with its own KEK and the sessions' payload keys (D5): the agent sends and gets
plaintext, and never a file key. The bytes travel as bytes, never base64 (D8). The agent's
``workspace/store.ts`` ``CrudSessionFiles`` calls these once ``…/areas`` lists the area,
and its own SQL until then (D9).

A workspace and a path travel in the query or the body, never in the route's path: a
principal's id may hold a ``/``. Each is checked against ``WORKSPACE_PATTERN`` and
``PATH_PATTERN``; a refusal is a problem whose ``reason`` is the agent's
``WorkspaceErrorCode`` (``invalid_path`` 422, ``not_found`` 404, ``conflict`` 409,
``too_large`` 413, ``quota`` 409, ``unavailable`` 503).

- ``GET …/workspace/status``: where a write's bytes go (``blobs``, ``postgres`` or
  ``refused`` with ``reason``), how many sealed files cannot be read from here, and why a
  sealed file cannot be read now (``sealed_unreadable``).
- ``GET …/workspace/files`` (``workspace``, ``prefix``), ``GET …/workspace/file``
  (``workspace``, ``path``), ``GET …/workspace/usage``, ``POST …/workspace/session-files``
  (at most ``SESSIONS_MAX`` sessions): the metadata.
- ``GET …/workspace/file-bytes`` (``workspace``, ``path``, ``expected_sha256``): the
  plaintext, streamed a segment at a time; 404, or 409 when the file is not at
  ``expected_sha256``. A file that changes while it streams ends the stream short: the
  agent hashes what it got and reads that as a conflict.
- ``POST …/workspace/file-writes``: ``multipart/form-data``, the part ``meta`` (JSON: the
  workspace, path, media type, sha256, size, source, what the file must be now, and the
  ``workspace.file`` events to log) and the part ``file``, at most ``FILE_BYTES_CEILING``
  bytes and the ``workspace_file_max_bytes`` setting.
- ``POST …/workspace/file-deletes``, ``POST …/workspace/copies`` (a fork's), ``POST
  …/workspace/blob-moves`` (one batch of files still in Postgres moved to blobs), ``POST
  …/workspace/sweeps`` (principals' expired files) and ``POST …/workspace/reconciles`` (the
  holds made to match the rows, one replica at a time: ``skipped: held`` while another
  holds the lock).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from scadbuddy.agent_data import workspace as ws
from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import SealError
from scadbuddy.crud.guard import crud_router
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope

router = crud_router("agent")
logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The most sessions one ``session-files`` read names (the picker's ``LIST_LIMIT_MAX``).
SESSIONS_MAX = 500
#: The largest ``meta`` part of a write.
META_BYTES_MAX = 32 * 1024
#: The largest ``source`` a file keeps, as JSON.
SOURCE_BYTES_MAX = 8 * 1024
#: What a write's multipart framing may add to its two parts.
FRAMING_BYTES_MAX = 16 * 1024
#: The most rows one ``blob-moves`` call moves.
MOVE_BATCH_MAX = 200
#: The header that names the streamed file's sha256 and size.
SHA256_HEADER = "X-ScadBuddy-Sha256"
SIZE_HEADER = "X-ScadBuddy-Size"

_STATUS: dict[str, int] = {
    "invalid_path": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "too_large": status.HTTP_413_CONTENT_TOO_LARGE,
    "quota": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}

Workspace = Annotated[str, Query(min_length=1, max_length=ws.WORKSPACE_MAX)]
FilePath = Annotated[str, Query(min_length=1, max_length=ws.PATH_MAX + 64)]
Sha = Annotated[str, Field(pattern=ws.SHA256_PATTERN)]


def _refusal(err: ws.WorkspaceRefusedError) -> ApiError:
    return ApiError(_STATUS[err.code], err.message, reason=err.code)


def _workspace(workspace: str) -> str:
    if not ws.valid_workspace(workspace):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "not a workspace", reason="invalid_path"
        )
    return workspace


def _path(path: str) -> str:
    try:
        return ws.check_path(path)
    except ws.WorkspaceRefusedError as err:
        raise _refusal(err) from None


async def _in_conn[T](state: Any, fn: Callable[[Any], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn:
            return fn(conn)

    try:
        return await asyncio.to_thread(run)
    except ws.WorkspaceRefusedError as err:
        raise _refusal(err) from None


def _keks(state: Any) -> ws.Keks:
    """The current and previous KEK, read once per process (``kek_at``)."""
    return ws.Keks(
        kek_at(state.settings.secret_key_file), kek_at(state.settings.secret_key_previous_file)
    )


def _content(state: Any) -> tuple[ContentStore | None, str]:
    """The content store (None on ``store_backend`` local) and the backend's name."""
    store = state.store
    return store.content, str(store.backend)


class FileInfo(BaseModel):
    path: str
    media_type: str
    size: int
    sha256: str
    source: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class SessionFileInfo(FileInfo):
    sealed: bool


class Files(BaseModel):
    files: list[FileInfo]


class OneFile(BaseModel):
    file: FileInfo | None


class Usage(BaseModel):
    files: int
    bytes: int


class BytesStatus(BaseModel):
    store: Literal["blobs", "postgres", "refused"]
    backend: str | None = None
    reason: str | None = None
    unreadable_files: int | None = None
    #: Why a sealed file cannot be read from here now; None when it can.
    sealed_unreadable: str | None = None


@router.get("/workspace/status", name="crud_agent_workspace_status")
async def workspace_status(state: StateDep) -> BytesStatus:
    content, backend = _content(state)
    keks = _keks(state)
    if content is None:
        out = BytesStatus(
            store="postgres",
            reason="the backend has no blob store (store_backend local)",
            sealed_unreadable="its bytes are in the blob store, and the backend now keeps files"
            " locally (store_backend local)",
        )
    elif keks.current is None:
        out = BytesStatus(
            store="refused",
            reason=f"the backend keeps files in its blob store (store_backend {backend}), and"
            " the API has no SCADBUDDY_SECRET_KEY_FILE to seal them with",
            sealed_unreadable="its bytes are sealed in the blob store, and the API has no"
            " SCADBUDDY_SECRET_KEY_FILE to open them",
        )
    else:
        return BytesStatus(store="blobs", backend=backend)
    unreadable = await _in_conn(state, ws.blob_rows)
    if unreadable:
        out.unreadable_files = unreadable
    return out


@router.get("/workspace/files", name="crud_agent_workspace_files")
async def workspace_files(
    state: StateDep,
    workspace: Workspace,
    prefix: Annotated[str | None, Query(max_length=ws.PATH_MAX + 64)] = None,
) -> Files:
    key = _workspace(workspace)
    rows = await _in_conn(state, lambda conn: ws.list_files(conn, key, prefix))
    return Files(files=[FileInfo.model_validate(r) for r in rows])


@router.get("/workspace/file", name="crud_agent_workspace_file")
async def workspace_file(state: StateDep, workspace: Workspace, path: FilePath) -> OneFile:
    key, name = _workspace(workspace), _path(path)
    row = await _in_conn(state, lambda conn: ws.stat(conn, key, name))
    return OneFile(file=None if row is None else FileInfo.model_validate(row))


@router.get("/workspace/usage", name="crud_agent_workspace_usage")
async def workspace_usage(state: StateDep, workspace: Workspace) -> Usage:
    key = _workspace(workspace)
    files, total = await _in_conn(state, lambda conn: ws.usage(conn, key))
    return Usage(files=files, bytes=total)


class SessionsAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_ids: list[Annotated[str, Field(pattern=rf"^{ws.UUID_PATTERN}$")]] = Field(
        max_length=SESSIONS_MAX
    )


class SessionsFiles(BaseModel):
    sessions: dict[str, list[SessionFileInfo]]


@router.post("/workspace/session-files", name="crud_agent_workspace_session_files")
async def workspace_session_files(body: SessionsAsk, state: StateDep) -> SessionsFiles:
    found = await _in_conn(state, lambda conn: ws.list_sessions(conn, body.session_ids))
    return SessionsFiles(
        sessions={
            sid: [SessionFileInfo.model_validate(r) for r in rows] for sid, rows in found.items()
        }
    )


# --- the bytes ---------------------------------------------------------------------------


async def _blob_stream(
    state: Any, workspace: str, path: str, stored: ws.Stored, recheck: bool
) -> AsyncIterator[bytes]:
    content, _backend = _content(state)
    if content is None:
        raise ws.WorkspaceRefusedError(
            "unavailable",
            f"{path} is in the blob store and cannot be read now: the backend keeps files"
            " locally (store_backend local)",
        )
    assert stored.blob_key is not None and stored.key_sealed is not None
    blob_key, sealed = stored.blob_key, stored.key_sealed
    file_key = await _in_conn(
        state,
        lambda conn: ws.unwrap_key(
            conn, workspace, path, blob_key, sealed, stored.key_kek_id, _keks(state)
        ),
    )
    stat = await content.stat(blob_key)
    if stat is None:
        raise ws.WorkspaceRefusedError(
            "unavailable", f"{path} is in the blob store and cannot be read now"
        )

    async def plain() -> AsyncIterator[bytes]:
        sent = 0
        async for part in ws.open_file(file_key, content.get(stat.ref), stored.sha256):
            # Read again between segments: a file replaced mid-send ends the stream short.
            if recheck and sent:
                now = await _in_conn(state, lambda conn: ws.sha_now(conn, workspace, path))
                if now != stored.sha256:
                    raise ws.WorkspaceRefusedError(
                        "conflict", f"{path} changed while it was being sent"
                    )
            sent += 1
            if part:
                yield part

    return plain()


def _postgres_stream(
    state: Any, workspace: str, path: str, stored: ws.Stored
) -> AsyncIterator[bytes]:
    async def slices() -> AsyncIterator[bytes]:
        offset = 0
        while True:
            row = await _in_conn(
                state,
                functools.partial(
                    ws.slice_of,
                    workspace=workspace,
                    path=path,
                    offset=offset,
                    length=ws.SEGMENT_BYTES,
                ),
            )
            # Gone, changed, or moved to a blob mid-send: the stream ends short.
            if row is None or row[2] != stored.sha256 or row[0] is None:
                raise ws.WorkspaceRefusedError(
                    "conflict", f"{path} changed while it was being sent"
                )
            part, size, _sha = row
            assert part is not None
            if part:
                yield part
            offset += len(part)
            if not part or offset >= size:
                return

    return slices()


@router.get("/workspace/file-bytes", name="crud_agent_workspace_file_bytes")
async def workspace_file_bytes(
    state: StateDep,
    workspace: Workspace,
    path: FilePath,
    expected_sha256: Annotated[str | None, Query(pattern=ws.SHA256_PATTERN)] = None,
) -> StreamingResponse:
    key, name = _workspace(workspace), _path(path)
    stored = await _in_conn(state, lambda conn: ws.stored(conn, key, name))
    if stored is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no file {name} in the workspace; workspace_list lists them",
            reason="not_found",
        )
    if expected_sha256 is not None and stored.sha256 != expected_sha256:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{name} changed: its sha256 is {stored.sha256}, not {expected_sha256}",
            reason="conflict",
        )
    try:
        if stored.blob_key is not None:
            parts = await _blob_stream(state, key, name, stored, recheck=True)
        else:
            parts = _postgres_stream(state, key, name, stored)
    except ws.WorkspaceRefusedError as err:
        raise _refusal(err) from None

    async def body() -> AsyncIterator[bytes]:
        try:
            async for part in parts:
                yield part
        except (ws.WorkspaceRefusedError, SealError) as err:
            # Past the status line: the stream ends short, which the agent reads as a conflict.
            logger.info("workspace file stream ended short", extra={"why": str(err)})
            raise

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={SHA256_HEADER: stored.sha256, SIZE_HEADER: str(stored.size)},
    )


# --- writes ------------------------------------------------------------------------------


class WriteMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=ws.WORKSPACE_MAX)
    path: str = Field(min_length=1, max_length=ws.PATH_MAX + 64)
    media_type: str = Field(min_length=1, max_length=255)
    sha256: Sha
    size: int = Field(ge=0, le=ws.FILE_BYTES_CEILING)
    source: dict[str, str | int | float | bool | None] | None = None
    #: What the file must be now: anything (``any``), no file (``absent``), or at
    #: ``expected_sha256`` (``sha256``).
    expect: ws.Expect = "any"
    expected_sha256: Sha | None = None
    #: ``workspace.file`` events of the workspace's session, logged in the write's
    #: transaction, as JSON text.
    events: list[Annotated[str, Field(max_length=ws.EVENT_BYTES_MAX)]] = Field(
        default_factory=list, max_length=ws.EVENTS_MAX
    )


class Written(BaseModel):
    file: FileInfo
    #: The logged events' seqs, oldest first.
    seqs: list[int]


def _unprocessable(detail: str) -> ApiError:
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


async def _write_body(request: Request) -> tuple[WriteMeta, bytes]:
    if request.headers.get("content-type", "").split(";")[0].strip() != "multipart/form-data":
        raise _unprocessable("a write comes as multipart/form-data")
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise ApiError(status.HTTP_411_LENGTH_REQUIRED, "a write names its Content-Length")
    if int(length) > ws.FILE_BYTES_CEILING + META_BYTES_MAX + FRAMING_BYTES_MAX:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"a workspace file holds at most {ws.FILE_BYTES_CEILING} bytes",
            reason="too_large",
        )
    try:
        form = await request.form(max_files=1, max_fields=1, max_part_size=META_BYTES_MAX)
    except StarletteHTTPException as error:
        raise _unprocessable(f"the write's body was refused: {error.detail}") from None
    try:
        raw = form.get("meta")
        upload = form.get("file")
        if not isinstance(raw, str) or not isinstance(upload, UploadFile):
            raise _unprocessable("a write is the parts meta (JSON) and file")
        try:
            meta = WriteMeta.model_validate_json(raw)
        except ValidationError as err:
            raise _unprocessable(f"meta: {err.errors(include_url=False)[0]['msg']}") from None
        data = await upload.read(ws.FILE_BYTES_CEILING + 1)
        if len(data) > ws.FILE_BYTES_CEILING:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"a workspace file holds at most {ws.FILE_BYTES_CEILING} bytes",
                reason="too_large",
            )
        return meta, data
    finally:
        await form.close()


async def _place(
    state: Any, content: ContentStore, workspace: str, path: str, data: bytes, digest: str
) -> ws.Placed:
    """``data`` sealed under a new file key, stored as a blob, its key wrapped for the row.
    The blob is held in the row's own transaction; until then the store's grace keeps it."""
    keks = _keks(state)
    file_key = ws.new_file_key()
    sealed = ws.seal_file(file_key, data, digest)
    try:
        ref = await content.put(
            "workspace", sealed, name="file.sealed", scope=BlobScope(folder="work")
        )
        blob_key = f"workspace-{ref.sha256}"
        await content.touch(blob_key)
    except Exception as err:  # any store failure refuses the write
        logger.warning("workspace file not stored", extra={"error": type(err).__name__})
        raise ws.unavailable(
            f"the blob store could not take the file ({type(err).__name__})"
        ) from None
    key_sealed, kek_id = await _in_conn(
        state, lambda conn: ws.wrap_key(conn, workspace, path, blob_key, file_key, keks)
    )
    return ws.Placed(blob_key=blob_key, key_sealed=key_sealed, key_kek_id=kek_id)


async def _drop_bytes(state: Any, shas: list[str]) -> None:
    """Deletes ``shas``' bytes once no file names them; a failure is logged, and the sweep
    deletes them later."""
    if not shas:
        return
    try:
        await _in_conn(state, lambda conn: ws.drop_unnamed_bytes(conn, shas))
    except Exception:  # the sweep tries again
        logger.warning("workspace files: unnamed bytes not deleted; the sweep deletes them")


@router.post("/workspace/file-writes", name="crud_agent_workspace_write")
async def workspace_write(request: Request, state: StateDep) -> Written:
    meta, data = await _write_body(request)
    key, name = _workspace(meta.workspace), _path(meta.path)
    digest = ws.sha256_hex(data)
    if digest != meta.sha256 or len(data) != meta.size:
        raise _unprocessable("the file is not the sha256 and size meta names")
    if meta.expect == "sha256" and meta.expected_sha256 is None:
        raise _unprocessable("expect sha256 names expected_sha256")
    if meta.source is not None and len(json.dumps(meta.source)) > SOURCE_BYTES_MAX:
        raise _unprocessable(f"a source is at most {SOURCE_BYTES_MAX} bytes of JSON")
    try:
        events = ws.check_events(key, meta.events)
    except ValueError as err:
        raise _unprocessable(f"events: {err}") from None
    file_max, workspace_max = await _in_conn(state, ws.limits)
    cap = min(file_max, ws.FILE_BYTES_CEILING)
    if len(data) > cap:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"{name} would be {len(data)} bytes; a workspace file holds at most {cap}",
            reason="too_large",
        )
    write = ws.Write(
        key,
        name,
        meta.media_type.strip(),
        digest,
        len(data),
        meta.source,
        meta.expect,
        meta.expected_sha256,
        events,
    )
    content, backend = _content(state)
    if content is None:
        placed = ws.Placed(data=data)
    else:
        if _keks(state).current is None:
            raise _refusal(
                ws.unavailable(
                    f"the backend keeps files in its blob store (store_backend {backend}),"
                    " and the API has no SCADBUDDY_SECRET_KEY_FILE to seal them with"
                )
            )
        # Checked before the upload too, so a refused write stores nothing.
        await _in_conn(
            state,
            lambda conn: ws.precheck(
                conn, key, name, len(data), workspace_max, meta.expect, meta.expected_sha256
            ),
        )
        try:
            placed = await _place(state, content, key, name, data, digest)
        except ws.WorkspaceRefusedError as err:
            raise _refusal(err) from None
    info, seqs, replaced = await _in_conn(
        state, lambda conn: ws.write_row(conn, write, placed, workspace_max)
    )
    await _drop_bytes(state, [replaced] if replaced else [])
    return Written(file=FileInfo.model_validate(info), seqs=seqs)


class DeleteAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=ws.WORKSPACE_MAX)
    path: str = Field(min_length=1, max_length=ws.PATH_MAX + 64)
    expected_sha256: Sha | None = None
    events: list[Annotated[str, Field(max_length=ws.EVENT_BYTES_MAX)]] = Field(
        default_factory=list, max_length=ws.EVENTS_MAX
    )


class Deleted(BaseModel):
    deleted: bool
    seqs: list[int]


@router.post("/workspace/file-deletes", name="crud_agent_workspace_delete")
async def workspace_delete(body: DeleteAsk, state: StateDep) -> Deleted:
    key, name = _workspace(body.workspace), _path(body.path)
    try:
        events = ws.check_events(key, body.events)
    except ValueError as err:
        raise _unprocessable(f"events: {err}") from None
    deleted, seqs, named = await _in_conn(
        state, lambda conn: ws.delete_row(conn, key, name, body.expected_sha256, events)
    )
    await _drop_bytes(state, [named] if named else [])
    return Deleted(deleted=deleted, seqs=seqs)


class CopyAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_session: str = Field(pattern=rf"^{ws.UUID_PATTERN}$")
    to_session: str = Field(pattern=rf"^{ws.UUID_PATTERN}$")


class Copied(BaseModel):
    copied: int


@router.post("/workspace/copies", name="crud_agent_workspace_copy")
async def workspace_copy(body: CopyAsk, state: StateDep) -> Copied:
    if body.from_session == body.to_session:
        raise _unprocessable("a fork's workspace is another session's")
    keks = _keks(state)
    copied = await _in_conn(
        state, lambda conn: ws.copy(conn, body.from_session, body.to_session, keks)
    )
    return Copied(copied=copied)


class MoveAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch: int = Field(default=50, ge=1, le=MOVE_BATCH_MAX)


class Moved(BaseModel):
    moved: int
    #: The backend keeps no blobs (store_backend local): nothing moves.
    local: bool
    #: Rows were still in Postgres: ask again.
    more: bool


def _inline(
    conn: Any, *, workspace: str, path: str, digest: str
) -> tuple[bytes, str | None] | None:
    return ws.inline_bytes(conn, workspace, path, digest)


@router.post("/workspace/blob-moves", name="crud_agent_workspace_move_to_blobs")
async def workspace_move_to_blobs(body: MoveAsk, state: StateDep) -> Moved:
    content, backend = _content(state)
    if content is None:
        return Moved(moved=0, local=True, more=False)
    if _keks(state).current is None:
        raise _refusal(
            ws.unavailable(
                f"the backend keeps files in its blob store (store_backend {backend}), and"
                " the API has no SCADBUDDY_SECRET_KEY_FILE to seal them with"
            )
        )
    rows = await _in_conn(state, lambda conn: ws.kept_in_postgres(conn, body.batch))
    moved = 0
    for workspace, path, digest in rows:
        held = await _in_conn(
            state, functools.partial(_inline, workspace=workspace, path=path, digest=digest)
        )
        if held is None:
            continue
        data, named = held
        try:
            placed = await _place(state, content, workspace, path, data, digest)
        except ws.WorkspaceRefusedError as err:
            raise _refusal(err) from None
        moved += await _in_conn(
            state,
            functools.partial(
                ws.moved_row, workspace=workspace, path=path, digest=digest, placed=placed
            ),
        )
        await _drop_bytes(state, [named] if named else [])
    return Moved(moved=moved, local=False, more=bool(rows))


class SweepAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_ms: int = Field(default=ws.TTL_MS, ge=60_000, le=365 * 86_400_000)


class Swept(BaseModel):
    swept: int


@router.post("/workspace/sweeps", name="crud_agent_workspace_sweep")
async def workspace_sweep(body: SweepAsk, state: StateDep) -> Swept:
    return Swept(swept=await _in_conn(state, lambda conn: ws.sweep(conn, body.ttl_ms)))


class ReconcileAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grace_seconds: int = Field(default=3600, ge=0, le=31_536_000)


class Reconciled(BaseModel):
    released: int
    held: int
    #: ``held``: another reconcile holds the lock, and this one did nothing.
    skipped: Literal["held"] | None = None


@router.post("/workspace/reconciles", name="crud_agent_workspace_reconcile")
async def workspace_reconcile(body: ReconcileAsk, state: StateDep) -> Reconciled:
    done = await _in_conn(state, lambda conn: ws.reconcile(conn, body.grace_seconds))
    if done is None:
        return Reconciled(released=0, held=0, skipped="held")
    return Reconciled(released=done[0], held=done[1])
