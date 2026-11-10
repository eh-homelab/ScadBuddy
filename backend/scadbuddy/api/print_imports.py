"""Import print (#4698): adopt a Bambuddy archive ScadBuddy did not start, and link it
to a model.

``GET /print-imports/{archive_id}/sources`` lists every place the print's 3MF can be
(`bambuddy/print_import.py`), with why one cannot be used, and suggests models by the
job's name. ``POST /print-imports/{archive_id}`` imports it from the source the person
chose, onto a model that exists or onto a new mesh model made from that 3MF. The link is
a ``print_links`` row with the subject ``model:<slug>`` (`PrintLinkStore.record_import`),
so every print route then takes the archive, as it takes a print ScadBuddy started.

ScadBuddy links its own prints lazily, so both routes first run the link passes for the
one archive (`linking.link_archive`): a print ScadBuddy made is linked to its run then,
and is never offered or taken as an import. A run's link found later still owns the
print (`print_links._OWNER_ORDER` puts an import last).

The printer is reached only when the request asks: ``printer=true`` on the sources, or a
``printer`` source on the import. These are a person's own calls from the page; the
assistant's tools pin both to a person's approval (`agent/src/tools/pinnedOutward.ts`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Self

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from scadbuddy.api.deps import CatalogueDep, PrintLinksDep, SettingsStoreDep, UploadsDep
from scadbuddy.api.mesh_models import MAX_DESCRIPTION_CHARS, MAX_NAME_CHARS, create_mesh_model
from scadbuddy.api.models import _slug_from_name, require_model_exists
from scadbuddy.api.operations import (
    KEY_REQUIRED_DETAIL,
    KEY_REQUIRED_PROBLEM,
    OPERATION_RESPONSES,
    IdempotencyKey,
)
from scadbuddy.api.prints import ArchiveIdPath
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.linking import link_archive
from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_import import (
    FoundSource,
    ModelSuggestion,
    SourceChoice,
    SourceFile,
    chosen,
    find_sources,
    read_source,
    suggested_slugs,
)
from scadbuddy.bambuddy.print_links import (
    IMPORT_LOCK_WAIT_S,
    ImportBusyError,
    LinkedPrint,
    PrintImport,
    PrintLinkStore,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import Operation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/print-imports", tags=["prints"])


class PrintOwner(BaseModel):
    """What a linked archive is a print of already."""

    subject: str
    slug: str | None = None
    output_id: str | None = None
    library_file_id: int | None = None


class PrintSources(BaseModel):
    archive_id: int
    #: The job's name in Bambuddy, and its file's.
    print_name: str | None
    filename: str | None
    #: Bambuddy's status: a print still ``printing`` can be imported too.
    status: str | None
    printer_id: int | None
    #: What the archive is a print of already, when a link names it; an import then
    #: answers only for the same model.
    linked: PrintOwner | None
    #: What its import read, when it was imported.
    imported: PrintImport | None
    sources: list[FoundSource]
    #: Models the job's name suggests; only a suggestion.
    suggested_models: list[ModelSuggestion]


class NewModel(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)


class ImportRequest(BaseModel):
    source: SourceChoice
    #: A model that exists; or ``new_model``, a mesh model made from the source's 3MF.
    slug: str | None = Field(default=None, pattern=MODEL_ID_PATTERN, max_length=MAX_MODEL_ID_LENGTH)
    new_model: NewModel | None = None

    @model_validator(mode="after")
    def _one_model(self) -> Self:
        if (self.slug is None) == (self.new_model is None):
            raise ValueError("name exactly one of slug and new_model")
        return self


class ImportedPrint(BaseModel):
    archive_id: int
    slug: str
    #: The print's record: ``GET /prints/{archive_id}``.
    print_url: str
    #: Whether the import made the model (``new_model``); a repeat answers the same.
    created_model: bool
    #: Whether this request made the import (201), not a repeat answering it (200).
    made_now: bool
    imported: PrintImport


def _owner(link: LinkedPrint) -> PrintOwner:
    return PrintOwner(
        subject=link.subject,
        slug=link.slug,
        output_id=link.output_id,
        library_file_id=link.library_file_id,
    )


async def _archive(client: BambuddyClient, archive_id: int) -> ArchiveDetail:
    try:
        return await client.archive(archive_id)
    except ApiError as error:
        if error.status == status.HTTP_404_NOT_FOUND:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"Bambuddy has no archive {archive_id}"
            ) from None
        raise


def _suggestions(catalogue: Catalogue, archive: ArchiveDetail) -> list[ModelSuggestion]:
    slugs = suggested_slugs(catalogue.slugs(), archive.print_name or archive.filename)
    if not slugs:
        return []
    records = {record.slug: record for record in catalogue.list_models(slugs)}
    return [
        ModelSuggestion(slug=slug, name=records[slug].name) for slug in slugs if slug in records
    ]


@router.get(
    "/{archive_id}/sources",
    response_model=PrintSources,
    summary="Where an archive's 3MF can be, to import the print",
    description=(
        "Every place the print's 3MF can be (#4698): the archive's sliced file, the "
        "archive's slicer project, Bambuddy library files with its name (each says "
        "whether its hash is the printed file's), and the printer's storage. A place "
        "that cannot be used says why (`reason`) and never hides the others; when only "
        "one can be used it is `preselected`. The printer is read only with "
        "`printer=true`, over FTP, and the key needs Control Printer. 404 when Bambuddy "
        "has no such archive."
    ),
)
async def get_sources(
    archive_id: ArchiveIdPath,
    links: PrintLinksDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    catalogue: CatalogueDep,
    printer: Annotated[
        bool, Query(description="Also read the printer's storage (it reaches the printer)")
    ] = False,
) -> PrintSources:
    async with client_for(store.load()) as client:
        archive = await _archive(client, archive_id)
        await link_archive(client, uploads, links, archive)
        linked = await links.linked(archive_id)
        sources = await find_sources(client, archive, printer=printer)
    return PrintSources(
        archive_id=archive_id,
        print_name=archive.print_name,
        filename=archive.filename,
        status=archive.status,
        printer_id=archive.printer_id,
        linked=_owner(linked) if linked is not None else None,
        # A run's link found after an import owns the print; the import is then no answer.
        imported=(
            await links.import_of(archive_id)
            if linked is not None and linked.slug is not None
            else None
        ),
        sources=sources,
        suggested_models=await asyncio.to_thread(_suggestions, catalogue, archive),
    )


async def _answer(
    links: PrintLinksDep,
    response: Response,
    slug: str,
    archive_id: int,
    *,
    new: bool,
) -> ImportedPrint:
    """201 for the import this request made, 200 for one made before; either way
    ``created_model`` is what the import recorded, so a repeat answers the same."""
    record = await links.import_of(archive_id)
    assert record is not None  # Written with the link it answers for.
    response.status_code = status.HTTP_201_CREATED if new else status.HTTP_200_OK
    return ImportedPrint(
        archive_id=archive_id,
        slug=slug,
        print_url=f"/api/v1/prints/{archive_id}",
        created_model=record.created_model,
        made_now=new,
        imported=record,
    )


def _refused(archive_id: int, owner: LinkedPrint, *, made: str | None = None) -> ApiError:
    of = (
        f"model {owner.slug!r}"
        if owner.slug is not None
        else f"output {owner.output_id}"
        if owner.output_id is not None
        else f"library file {owner.library_file_id}"
    )
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"archive {archive_id} is a print of {of} already"
        + (
            f"; the model {made!r} was made from its 3MF and stays, linked to no print"
            if made is not None
            else ""
        ),
        owner=_owner(owner).model_dump(),
    )


@asynccontextmanager
async def _one_import(links: PrintLinkStore, archive_id: int) -> AsyncIterator[None]:
    """`PrintLinkStore.import_lock`, its wait past `IMPORT_LOCK_WAIT_S` a 409 to send
    again."""
    try:
        async with links.import_lock(archive_id):
            yield
    except ImportBusyError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"another import of archive {archive_id} is still running: send the same"
            " request again shortly",
            headers={"Retry-After": str(int(IMPORT_LOCK_WAIT_S))},
        ) from None


@router.post(
    "/{archive_id}",
    response_model=ImportedPrint,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": ImportedPrint, "description": "Imported onto this model already"},
        **OPERATION_RESPONSES,
    },
    summary="Import a print ScadBuddy did not start, onto a model",
    description=(
        "Links the archive to a model (#4698): `slug`, a model that exists, or "
        "`new_model`, a mesh model made from the source's 3MF (a sliced file has no "
        "meshes, so that is a 422; it needs an `Idempotency-Key`, 428 without one, and "
        "answers 202 while the model is still being made: send the same request again). "
        "`source` must be one `GET …/sources` lists as available (422 when it is not "
        "listed, 409 when it cannot be used now, or while another import of the archive "
        "runs past 30 s: send it again). For the archive's and the library's "
        "files ScadBuddy records which file and its hash, and copies nothing; a printer "
        "file is read over FTP and kept as a Bambuddy library file in the inbox folder "
        "(one there with its name and SHA-256 already is used again), "
        "as the printer overwrites its cache. The same archive onto the same model again "
        "is 200 with the import made then; onto another model, or an archive "
        "ScadBuddy printed, is 409, naming the owner. A print still printing can be "
        "imported: its status comes from Bambuddy on each read."
    ),
)
async def import_print(
    archive_id: ArchiveIdPath,
    body: ImportRequest,
    response: Response,
    links: PrintLinksDep,
    uploads: UploadsDep,
    store: SettingsStoreDep,
    catalogue: CatalogueDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ImportedPrint | JSONResponse:
    if body.new_model is not None and idempotency_key is None:
        raise ApiError(
            status.HTTP_428_PRECONDITION_REQUIRED, KEY_REQUIRED_DETAIL, type_=KEY_REQUIRED_PROBLEM
        )
    if body.slug is not None:
        target = body.slug
    else:
        assert body.new_model is not None
        target = _slug_from_name(body.new_model.name)
    settings = store.load()
    # One import of an archive at a time, from the owner check to the record: two at once
    # never both make a model or keep a printer file (#4698 review).
    async with _one_import(links, archive_id), client_for(settings) as client:
        archive = await _archive(client, archive_id)
        # A print ScadBuddy made is linked to its run here, if no read linked it yet.
        await link_archive(client, uploads, links, archive)
        owner = await links.linked(archive_id)
        if owner is not None:
            # Nothing is read again: the same import answers what it made.
            if owner.slug == target:
                return await _answer(links, response, target, archive_id, new=False)
            raise _refused(archive_id, owner)
        if body.slug is not None:
            require_model_exists(catalogue, body.slug)
        sources = await find_sources(client, archive, printer=body.source.kind == "printer")
        source = chosen(sources, body.source)
        if body.new_model is not None and source.sliced:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{source.name} is a sliced file, which has no meshes to open as a model:"
                " choose the archive's slicer project or a library 3MF, or link the print"
                " to a model that exists",
            )
        if source.kind == "printer" and settings.library_folder_id is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "a printer file is kept in Bambuddy's library inbox folder:"
                " choose that folder in Settings first",
            )
        read: SourceFile | None = None
        if body.new_model is not None or source.kind == "printer":
            read = await read_source(client, archive, source)
        created = False
        if body.new_model is not None:
            assert read is not None
            made = await _new_model(ops, response, catalogue, idempotency_key, body.new_model, read)
            if isinstance(made, JSONResponse):
                return made
            target, created = made, True
        library_file_id = source.library_file_id
        kept: int | None = None
        if source.kind == "printer":
            assert read is not None and settings.library_folder_id is not None
            library_file_id, uploaded = await _keep_printer_file(
                client, read, settings.library_folder_id
            )
            kept = library_file_id if uploaded else None
        record = PrintImport(
            archive_id=archive_id,
            slug=target,
            source=source.kind,
            library_file_id=library_file_id,
            printer_id=archive.printer_id if source.kind == "printer" else None,
            printer_path=source.path,
            filename=read.name if read is not None else source.name or f"print-{archive_id}.3mf",
            size=len(read.data) if read is not None else source.size,
            sha256=read.sha256 if read is not None else source.sha256,
            created_model=created,
        )
        owner = await _record(links, client, record, archive, kept)
    if owner is not None:
        # Only a run's link can come between the check above and the record (imports of
        # one archive wait on `import_lock`), and a run's print is its own.
        if owner.slug == target:
            return await _answer(links, response, target, archive_id, new=False)
        raise _refused(archive_id, owner, made=target if created else None)
    return await _answer(links, response, target, archive_id, new=True)


async def _keep_printer_file(
    client: BambuddyClient, read: SourceFile, folder_id: int
) -> tuple[int, bool]:
    """The library copy of a printer file in the inbox folder, and whether this call
    uploaded it. A copy with the same name and SHA-256 there already (an earlier try of
    this import that was not recorded) is used again, so a retry uploads nothing."""
    for row in await client.library_files(folder_id):
        if row.filename != read.name or row.file_size not in (None, len(read.data)):
            continue
        if (await client.library_file(row.id)).file_hash == read.sha256:
            return row.id, False
    uploaded = await client.upload_library_file(read.name, read.data, folder_id=folder_id)
    return uploaded.id, True


async def _record(
    links: PrintLinkStore,
    client: BambuddyClient,
    record: PrintImport,
    archive: ArchiveDetail,
    kept: int | None,
) -> LinkedPrint | None:
    """`record_import`; a library copy this request uploaded (``kept``) that no import
    names after it is deleted, refused or failed alike."""
    try:
        owner = await links.record_import(
            record, printer_id=archive.printer_id, plate_id=archive.plate_id
        )
    except BaseException:
        if kept is not None:
            await _forget_copy(client, kept)
        raise
    if owner is not None and kept is not None:
        await _forget_copy(client, kept)
    return owner


async def _forget_copy(client: BambuddyClient, file_id: int) -> None:
    try:
        await client.delete_library_file(file_id)
    except ApiError:
        # It stays in the inbox; a retry finds it by its hash and uses it.
        logger.warning(
            "could not delete a printer file's library copy", extra={"library_file_id": file_id}
        )


def _source_changed(name: str, slug: str, first: Operation, made: bool) -> str:
    """The 409 of a new-model retry whose source changed since its first send (#5979):
    what that send's operation (``first``) did, and whether its model is there now."""
    changed = f"{name} changed since this request was first sent"
    again = (
        "send the request with a new Idempotency-Key to make the model from the file as it"
        " is now, or choose another source"
    )
    if first.status == "running":
        return (
            f"{changed}, and the model {slug!r} that request started is still being made"
            f" (operation {first.id}, `GET /api/v1/operations/{first.id}`): once it is done,"
            " import the print onto it with `slug`, or choose another source"
        )
    if first.status == "failed":
        return (
            f"{changed}, and that request made no model: its operation {first.id} failed; {again}"
        )
    if made:
        return (
            f"{changed}, so it is no longer the file the model {slug!r} was made from: that"
            " model stays; import the print onto it with `slug`, or choose another source"
        )
    return f"{changed}; that request made the model {slug!r}, which was deleted since; {again}"


async def _new_model(
    ops: OperationsDep,
    response: Response,
    catalogue: Catalogue,
    idempotency_key: str | None,
    new_model: NewModel,
    read: SourceFile,
) -> str | JSONResponse:
    """Make the mesh model from the bytes read, as an upload is made one
    (`create_mesh_model`); its slug, or the 202 while it is still being made."""

    async def claim(claims: ClaimStore) -> Held:
        return await asyncio.to_thread(claims.hold, read.data)

    made = await create_mesh_model(
        ops,
        response,
        catalogue,
        idempotency_key,
        name=new_model.name,
        description=new_model.description,
        source={"kind": "upload", "claim": read.sha256, "name": read.name},
        claim=claim,
        # A retry reads its source again, and a printer overwrites its files: a model the
        # first read made is no longer this file's, so it is named, never linked.
        changed=lambda first, made: _source_changed(
            read.name, _slug_from_name(new_model.name), first, made
        ),
    )
    if isinstance(made, JSONResponse):
        return made
    return made.slug
