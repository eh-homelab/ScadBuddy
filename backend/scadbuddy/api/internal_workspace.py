"""The routes the agent stores a session's workspace files through (#4789, spec
2026-10-13 §14): their bytes, sealed by the agent under the session's own data key, are
blobs of the content store (`workspace-<sha256 of the sealed bytes>`), not `bytea` in
`ai_session_files`. The agent holds no store credential and mints no token: it asks
here for a `TransferUrl` (`slicing/transfer.py`) to PUT a file to (always the internal
blob route, `api/internal_blobs.py`), says what it PUT, and asks for one to GET a file
back (S3's presigned URL on `store_backend` s3). The agent writes each file's holder
`(ai_session_file, <workspace>/<path>)` in `blob_refs` itself, in the transaction that
writes its row (a trigger on `ai_session_files`), so the blob sweep keeps a blob while a
row names it.

Without a content store (`store_backend` local) every route is a 404, and the agent keeps
the file's bytes in Postgres as before. With one but without what a `TransferUrl` needs
(`SCADBUDDY_API_INTERNAL_URL`, the KEK) a route is a 503 saying which: the agent then
refuses the write, never keeping the bytes in plaintext. Not a client API: hidden from
the schema, and internal by path only, like `api/internal.py`. `POST /uploads` is a write
path (a token for one internal PUT of up to `MAX_SLICED_BYTES`, with no workflow or row
behind it): README "Deploying" lists it with the internal routes that the guard coming
with API auth must cover."""

from __future__ import annotations

import re

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.slicing.component import SlicingDep
from scadbuddy.slicing.models import TransferUrl
from scadbuddy.slicing.transfer import (
    WORKSPACE_PENDING_PREFIX,
    TransferRecordError,
    Transfers,
    TransferUnavailableError,
)
from scadbuddy.store.content_models import BlobMissingError

INTERNAL_WORKSPACE = "/api/v1/internal/workspace-blobs"
#: The prefix of every key a workspace file is stored under.
WORKSPACE_KEY_PREFIX = "workspace-"

router = APIRouter(prefix=INTERNAL_WORKSPACE.removeprefix("/api/v1"), include_in_schema=False)

_PENDING_KEY = re.compile(rf"^{WORKSPACE_PENDING_PREFIX}[0-9a-f]{{32}}$")
_BLOB_KEY = re.compile(rf"^{WORKSPACE_KEY_PREFIX}[0-9a-f]{{64}}$")


class FinishedPut(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class StoredKey(BaseModel):
    key: str


class WorkspaceUpload(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    url: TransferUrl
    pending_key: str


def _transfers(slicing: SlicingDep) -> Transfers:
    try:
        transfers = slicing.transfers()
    except RuntimeError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "workspace files are kept in Postgres: store_backend local has no blob store",
        ) from None
    return transfers


def _unavailable(err: TransferUnavailableError) -> ApiError:
    # `err` names what the slicer's URLs need; workspace files' URLs need the same.
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"workspace files cannot reach the blob store: {err}".replace(
            "the native slicer needs", "the transfer URLs need"
        ),
    )


def _missing(key: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"blob {key} not found", key=key)


#: A write path for whoever reaches the API, internal by path only (README "Deploying"):
#: covered by the same cluster-internal guard as the other internal routes once the API has auth.
@router.post("/uploads", response_model=WorkspaceUpload)
async def mint_upload(slicing: SlicingDep) -> WorkspaceUpload:
    try:
        put = _transfers(slicing).mint_put(WORKSPACE_PENDING_PREFIX)
    except TransferUnavailableError as err:
        raise _unavailable(err) from None
    return WorkspaceUpload(url=put.url, pending_key=put.pending_key)


@router.post("/uploads/{pending_key}/finish", response_model=StoredKey)
async def finish_upload(pending_key: str, body: FinishedPut, slicing: SlicingDep) -> StoredKey:
    transfers = _transfers(slicing)
    # Never a slice's pending key: this route answers only for workspace files.
    if not _PENDING_KEY.match(pending_key):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no upload {pending_key}")
    try:
        key = await transfers.finish_pending(pending_key, sha256=body.sha256, size=body.size)
    except TransferRecordError as err:
        raise ApiError(status.HTTP_409_CONFLICT, str(err)) from None
    # The PUT's time is the blob's grace; the agent writes its holder next.
    await transfers.content.touch(key)
    return StoredKey(key=key)


@router.get("/{key}/url", response_model=TransferUrl)
async def mint_download(key: str, slicing: SlicingDep) -> TransferUrl:
    transfers = _transfers(slicing)
    if not _BLOB_KEY.match(key):
        raise _missing(key)
    try:
        return await transfers.mint_get(key)
    except BlobMissingError:
        raise _missing(key) from None
    except TransferUnavailableError as err:
        raise _unavailable(err) from None


__all__ = ["INTERNAL_WORKSPACE", "WORKSPACE_KEY_PREFIX", "router"]
