"""The MakerWorld import as an operation (#6119, spec 2026-10-01 §4.3).

It runs on the ``library`` worker, in the API process, which holds ScadBuddy's own
library and Bambuddy's key; nothing that runs untrusted code calls Bambuddy (#5295). Its
download can take minutes, so the route answers 202 with the operation past the command
deadline, and a caller follows ``GET /operations/{id}``.

The effect may run again: Bambuddy dedupes an import by the model and profile's source URL
(``routes/makerworld.py``, maziggy/bambuddy v1.3.2b1-daily.20261112), and the copy into the
native library skips bytes it already holds.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.bambuddy.client import MAKERWORLD_IMPORT_TIMEOUT, BambuddyConfig, client_for
from scadbuddy.core.problems import ApiError
from scadbuddy.makerworld.imports import FOLDER_ON_BAMBUDDY, import_model
from scadbuddy.makerworld.models import MakerWorldImportRequest
from scadbuddy.operations.kinds import KindsBuild, OperationKind
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.copy_store import LibraryCopyStore

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

KIND = "makerworld_import"
#: Bambuddy's download, then the copy into the native library. With the run's
#: ``CANCEL_GRACE`` it stays under the 15 minutes a browser follows a 202 (#1063).
IMPORT_TIMEOUT = timedelta(seconds=MAKERWORLD_IMPORT_TIMEOUT) + timedelta(minutes=3)
#: A transient Bambuddy failure is tried again: neither step repeats its effect.
IMPORT_ATTEMPTS = 3


def subject(request: MakerWorldImportRequest) -> str:
    return f"makerworld:{request.model_id}:{request.profile_id or 'default'}"


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    from scadbuddy.api.deps import AppState

    if not isinstance(core, AppState):
        raise TypeError(
            "the MakerWorld import runs on the API's library worker, which needs the API's"
            f" AppState, not a {type(core).__name__}"
        )
    state = core
    library = components.get(PRINT_LIBRARY)

    async def check(request: dict[str, Any]) -> dict[str, Any]:
        body = MakerWorldImportRequest.model_validate(request)
        BambuddyConfig.from_settings(await asyncio.to_thread(state.settings_store.load))
        native = await library.serving_native()
        if body.folder_id is not None:
            if native is None:
                raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, FOLDER_ON_BAMBUDDY)
            await native.folder(body.folder_id)
        return {}

    async def run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        body = MakerWorldImportRequest.model_validate(request)
        settings = await asyncio.to_thread(state.settings_store.load)
        native = await library.serving_native()
        async with client_for(settings) as client:
            imported = await import_model(
                body,
                client=client,
                native=native,
                locks=lambda: LibraryCopyStore(state.settings_store.pool).locks(),
            )
        dumped: dict[str, Any] = imported.model_dump(mode="json")
        return dumped

    return [
        OperationKind(
            KIND,
            check,
            run,
            run_attempts=IMPORT_ATTEMPTS,
            queue="library",
            run_timeout=IMPORT_TIMEOUT,
            where="Bambuddy's library and ScadBuddy's library",
        )
    ]


OPERATION_KINDS: KindsBuild = _kinds
