"""The content backends a non-local store runs on (spec 2026-10-16 §4), built by name."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from scadbuddy.core.config import StoreBackend
from scadbuddy.library.settings_store import RenderStoreSettings, StoreNotReadyError
from scadbuddy.store.bambuddy import BambuddyContentBackend, RenderSettingsSource
from scadbuddy.store.content import ContentBackend
from scadbuddy.store.index import Pool
from scadbuddy.store.s3 import S3ContentBackend

#: The recovery from a refused start: back to the local store (README, "Deploying").
RECOVER_LOCAL_SQL = "UPDATE settings SET value = '\"local\"' WHERE name = 'store_backend';"


class RemoteBackend(ContentBackend, Protocol):
    async def aclose(self) -> None: ...


@runtime_checkable
class UnrecordedSweeper(Protocol):
    """A backend whose uploads can outlive a process that died before their row was
    written (#701); the blob sweep deletes those older than its cutoff."""

    async def sweep_unrecorded(
        self, *, cutoff: datetime, recorded: Callable[[str], bool]
    ) -> int | list[str]: ...


def build_backend(
    backend: StoreBackend, current: RenderStoreSettings, source: RenderSettingsSource, pool: Pool
) -> RemoteBackend:
    """``backend`` by name, not ``current.store_backend``: a migration builds its target
    while the stored backend still names the old one."""
    if backend == "local":
        raise ValueError("the local store has no content backend")
    if backend == "bambuddy":
        # From any source, the environment's seed included: a Bambuddy store without a
        # URL or an inbox could store nothing, so the process does not start on it.
        if not current.bambuddy_url or current.library_folder_id is None:
            raise StoreNotReadyError(
                "store_backend is bambuddy, but the Bambuddy store needs a Bambuddy URL and a"
                " library folder (its inbox). To start on the local store, run"
                f" {RECOVER_LOCAL_SQL} in ScadBuddy's database, in the schema its"
                " SCADBUDDY_DATABASE_URL uses (or, when no store_backend is"
                " stored, set SCADBUDDY_STORE_BACKEND=local), then set both in Settings."
                ' See README, "Recovering an unready blob store".'
            )
        return BambuddyContentBackend(source.target, pool)
    if current.s3 is None or not current.s3.prefix:
        raise StoreNotReadyError(
            "store_backend is s3, but the S3 store needs an endpoint, a bucket, a prefix,"
            " an access key id and a secret access key. To start on the local store, run"
            f" {RECOVER_LOCAL_SQL} in ScadBuddy's database, in the schema its"
            " SCADBUDDY_DATABASE_URL uses (or, when no store_backend is"
            " stored, set SCADBUDDY_STORE_BACKEND=local), then set them in Settings."
            ' See README, "Recovering an unready blob store".'
        )
    return S3ContentBackend(current.s3)
