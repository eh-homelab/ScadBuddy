"""Claim checks held in the blob store, for a library upload (#7373).

`claims.py` keeps a claim as a file under ``cache/claims`` on the data volume, which
every API replica mounts over NFS. A library upload's body was spooled there, linked in
as its claim, read back by the run on whichever replica took it, hashed again and put
in the blob store. Here the route puts the bytes in the blob store once, as the
``library`` blob `upload_key` names, while the request is received: the body is
spooled to the node-local `ContentStore.spool` and hashed as it streams, so the put
reads it once and hashes nothing. The run reads the blob on any replica.

The row's ``touched_at`` stands in for a claim file's inode: a release removes the
row only while it is still the put this request made (`ContentStore.delete_if_stale`),
and only when that put created it and no running operation names the digest
(`api/operations.py` `_release`), so bytes another request put since stay for its run.
What a crash leaves is removed by `sweep_upload_blobs` once nothing has put it for
`CLAIM_MAX_AGE`, on the claims sweep's cadence and on every backend. The native
library's own orphan sweep (`print_library/sweep.py`) leaves these keys to it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scadbuddy.operations.claims import CLAIM_MAX_AGE
from scadbuddy.store.content import BlobRef, BlobScope, ContentStore
from scadbuddy.store.content_models import BlobKind, RefusedDeleteError

logger = logging.getLogger(__name__)

#: The index key of an upload's blob is this and its sha256.
UPLOAD_KEY_PREFIX = "library-upload-"
UPLOAD_KIND: BlobKind = "library"


def upload_key(digest: str) -> str:
    return f"{UPLOAD_KEY_PREFIX}{digest}"


@dataclass(frozen=True)
class HeldBlob:
    """What one put left: the claim's name (the sha256), the ``touched_at`` its put
    wrote, and whether that put created the row (none held those bytes before)."""

    name: str
    touched_at: datetime
    created: bool


class BlobClaims:
    def __init__(self, content: ContentStore) -> None:
        self.content = content

    async def hold_file(self, path: Path, digest: str, size: int, *, name: str) -> HeldBlob:
        """Put the file at ``path``, whose sha256 and size the caller hashed as it
        streamed, as the upload blob of ``digest``. `StoreFullError` propagates."""
        key = upload_key(digest)
        before = await asyncio.to_thread(self.content.index.get, key)
        with path.open("rb") as data:
            await self.content.put(
                UPLOAD_KIND, data, name=name, scope=BlobScope(), key=key, digest=(digest, size)
            )
        stat = await asyncio.to_thread(self.content.index.get, key)
        if stat is None:
            raise LookupError(f"upload {digest} was removed as it was put")
        return HeldBlob(digest, stat.touched_at, created=before is None)

    async def ref(self, digest: str) -> BlobRef:
        """The upload blob of ``digest``; `LookupError` when it is gone."""
        stat = await self.content.stat(upload_key(digest))
        if stat is None:
            raise LookupError(f"upload {digest} is gone")
        return stat.ref

    async def release(self, held: HeldBlob) -> None:
        """Remove the row ``held`` created, unless a put has touched it since. The bytes
        stay while another row names them (a native library file of the same bytes)."""
        if not held.created:
            return
        try:
            await self.content.delete_if_stale(upload_key(held.name), held.touched_at)
        except RefusedDeleteError:
            logger.exception("the backend refused to delete an upload's blob")


async def sweep_upload_blobs(
    content: ContentStore, max_age: timedelta = CLAIM_MAX_AGE, *, now: datetime | None = None
) -> int:
    """Remove the upload blobs nothing has put for ``max_age``; how many went."""
    cutoff = (now or datetime.now(UTC)) - max_age
    removed = 0
    stats = await asyncio.to_thread(content.index.stats, [UPLOAD_KIND], backend=content.name)
    for stat in stats:
        if not stat.key.startswith(UPLOAD_KEY_PREFIX) or stat.touched_at > cutoff:
            continue
        try:
            if await content.delete_if_stale(stat.key, cutoff):
                removed += 1
        except RefusedDeleteError:
            logger.exception("the backend refused to delete an upload's blob")
    return removed
