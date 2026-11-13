"""MakerWorld through Bambuddy's own MakerWorld routes, called server-side (#6119, #1254).

Bambuddy resolves a MakerWorld link and downloads a profile's 3MF with its Bambu Cloud
token (maziggy/bambuddy ``backend/app/api/routes/makerworld.py`` and
``backend/app/schemas/makerworld.py`` at v1.3.2b1-daily.20261112). ScadBuddy calls them
with its own Bambuddy key, so the assistant, a flow and the browser never reach Bambuddy
or hold its key (#5295). The import is an operation on the ``library`` worker
(`makerworld/operations.py`), which also files the file in ScadBuddy's own library on
``library_backend`` native.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.core.problems import ApiError
from scadbuddy.makerworld.models import (
    MakerWorldImported,
    MakerWorldImportRequest,
    MakerWorldResolved,
    MakerWorldResolveRequest,
    MakerWorldStatus,
)
from scadbuddy.makerworld.operations import KIND, subject
from scadbuddy.operations.component import OperationsDep
from scadbuddy.print_library.component import PrintLibraryDep
from scadbuddy.print_library.native import NativeLibrary

router = APIRouter(prefix="/makerworld", tags=["makerworld"])

#: The Bambuddy files of a model whose bytes a resolve looks up in the native library: a
#: model imported once per profile has a few.
MAX_MATCHED = 20


@router.get(
    "/status",
    response_model=MakerWorldStatus,
    summary="Whether Bambuddy can import from MakerWorld",
    description=(
        "Bambuddy's MakerWorld status for ScadBuddy's API key: `can_download` is false"
        " without a Bambu Cloud sign-in in Bambuddy, or, while Bambuddy's auth is on, for a"
        " key without cloud access. 409 when no Bambuddy is configured."
    ),
)
async def get_makerworld_status(store: SettingsStoreDep) -> MakerWorldStatus:
    async with client_for(await asyncio.to_thread(store.load)) as client:
        found = await client.makerworld_status()
    return MakerWorldStatus(
        has_cloud_token=found.has_cloud_token,
        can_download=found.can_download,
        sign_in_expired=found.sign_in_expired,
    )


@router.post(
    "/resolve",
    response_model=MakerWorldResolved,
    summary="Resolve a MakerWorld link",
    description=(
        "The design and its print profiles (`instances`, each `profileId` an import's"
        " `profile_id`) for a MakerWorld link, read by Bambuddy, and the library files an"
        " earlier import of the model made. Downloads nothing. 400 for a link Bambuddy"
        " does not take; 409 when no Bambuddy is configured."
    ),
)
async def post_makerworld_resolve(
    body: MakerWorldResolveRequest, store: SettingsStoreDep, library: PrintLibraryDep
) -> MakerWorldResolved:
    native = await library.serving_native()
    async with client_for(await asyncio.to_thread(store.load)) as client:
        resolved = await client.makerworld_resolve(body.url)
        bambuddy_ids = resolved.already_imported_library_ids
        library_ids = (
            bambuddy_ids if native is None else await _native_copies(client, native, bambuddy_ids)
        )
    return MakerWorldResolved(
        model_id=resolved.model_id,
        profile_id=resolved.profile_id,
        design=resolved.design,
        instances=resolved.instances,
        already_imported_bambuddy_ids=bambuddy_ids,
        already_imported_library_ids=library_ids,
        library="bambuddy" if native is None else "native",
    )


async def _native_copies(
    client: BambuddyClient, native: NativeLibrary, bambuddy_ids: list[int]
) -> list[int]:
    """The live native files holding the bytes of these Bambuddy files, by SHA-256 (the
    match a ``bb_to_sb`` copy makes). A Bambuddy file gone since its import is left out."""
    digests: list[str] = []
    for file_id in bambuddy_ids[:MAX_MATCHED]:
        try:
            found = await client.library_file(file_id)
        except ApiError as error:
            if error.status == status.HTTP_404_NOT_FOUND:
                continue
            raise
        if found.file_hash is not None:
            digests.append(found.file_hash)
    rows = await asyncio.to_thread(native.rows.files_by_sha256, digests)
    return [row.id for row in rows]


@router.post(
    "/import",
    response_model=MakerWorldImported,
    summary="Import a MakerWorld model into the library",
    description=(
        "Bambuddy downloads the profile's 3MF from MakerWorld into its library"
        " (`bambuddy_folder_id`, else its `MakerWorld` folder). On `library_backend`"
        " native the file is then copied into ScadBuddy's own library (`folder_id`, else a"
        " `MakerWorld` folder at its top level), skipped when that library already holds"
        " the same bytes; `library_file_id` is the file the order and print routes take."
        " A repeat downloads nothing: Bambuddy reuses its earlier import of the profile."
        " The download can take minutes: past the command deadline the answer is 202 with"
        " the operation to follow. 409 when no Bambuddy is configured; 422 for a"
        " `folder_id` on `library_backend` bambuddy."
    ),
    responses=OPERATION_RESPONSES,
)
async def post_makerworld_import(
    body: MakerWorldImportRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> MakerWorldImported | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[KIND],
        subject=subject(body),
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, MakerWorldImported)
