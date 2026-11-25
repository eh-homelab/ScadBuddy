"""A hosted plugin's bytes from the blob store (spec 2026-11-21 §6.1), for the routes
(`api/hosted_plugins.py`) and the agent's CRUD read (`api/crud_agent_packages.py`)."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.attachments import blob_key, read_blob

#: The reason of a 500 when the store lost a blob a row names, or holds other bytes.
BLOB_MISSING = "blob-missing"


async def blob_bytes(state: Any, sha256: str) -> bytes:
    data = await read_blob(state.store.blobs, blob_key(sha256))
    if data is None:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "the store has lost a file's bytes",
            reason=BLOB_MISSING,
        )
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "a file's bytes in the store do not match their sha256",
            reason=BLOB_MISSING,
        )
    return data
