"""A native library file's associations (#6395): ``GET``/``POST
/print/library/{file_id}/associations``, ``DELETE /print/library/associations/{id}`` and
``GET /models/{slug}/associations``. The rows are `print_library/associations.py`'s; this
adds the implied ones, read live and never stored: a model made from the file (its
``mesh_source``) has the file as ``source_of`` it, and an output's copy in the library
(``library_files.output_id``) is ``output_of`` the output's model.

Associations are ScadBuddy's own library's: on ``library_backend=bambuddy`` a read lists
none and a write is a 409.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``)."""

import asyncio
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response, status
from pydantic import BaseModel, Field, field_validator

from scadbuddy.api.deps import CatalogueDep, OutputsDep, SlugPath
from scadbuddy.api.library_print import _served_file_id
from scadbuddy.api.models import require_model_exists
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.paths import is_builtin
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError
from scadbuddy.library.outputs import OutputNotFoundError, OutputStore
from scadbuddy.orders.actor import current_actor
from scadbuddy.print_library.associations import (
    MAX_NOTE_CHARS,
    Association,
    Relation,
    TargetGoneError,
)
from scadbuddy.print_library.component import PrintLibraryDep
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["print"], dependencies=[Depends(_served_file_id)])

FileIdPath = Annotated[int, Path(ge=1)]

NOT_NATIVE_PROBLEM = "https://scadbuddy.dev/problems/library/associations-not-native"


class AssociationRequest(BaseModel):
    #: ``model:<slug>`` or ``library:<id>``.
    target: str = Field(
        description="What the file relates to: `model:<slug>` or `library:<file id>`",
        max_length=200,
    )
    relation: Relation = Field(
        description="How, from the file's side: the file is an `output_of` the target, a"
        " `variant_of` it, the `source_of` it, or `related_to` it"
    )
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        subject = PrintSubject.parse(value)
        if subject.kind == "output":
            raise ValueError("a target is `model:<slug>` or `library:<file id>`")
        # `isdecimal` passes `01000000005` and non-ASCII digits; the row keeps the id as
        # `int` writes it, so only that spelling is one.
        if subject.kind == "library" and subject.id != str(int(subject.id)):
            raise ValueError(f"write the file id as {int(subject.id)}")
        return value


class Associations(BaseModel):
    associations: list[Association]


async def _native(library: PrintLibraryDep) -> NativeLibrary | None:
    return await library.serving_native()


def _model_name(catalogue: Catalogue, slug: str) -> str | None:
    try:
        name = catalogue.read_raw_meta(slug).get("name")
    except (InvalidModelMetaError, OSError):
        return None
    return name if isinstance(name, str) and name else slug


def _mesh_source_file(catalogue: Catalogue, slug: str) -> int | None:
    """The native library file the model was made from, if it was."""
    try:
        source = catalogue.read_raw_meta(slug).get("mesh_source")
    except (InvalidModelMetaError, OSError):
        return None
    if not isinstance(source, dict) or source.get("kind") != "bambuddy_library":
        return None
    file_id = source.get("file_id")
    return file_id if isinstance(file_id, int) and is_native_id(file_id) else None


def _named(found: list[Association], catalogue: Catalogue) -> list[Association]:
    names: dict[str, str | None] = {}
    named = []
    for item in found:
        subject = PrintSubject.parse(item.target)
        if subject.slug is not None and item.target_name is None:
            if subject.slug not in names:
                names[subject.slug] = _model_name(catalogue, subject.slug)
            item = item.model_copy(update={"target_name": names[subject.slug]})
        named.append(item)
    return named


def _without_stored(implied: list[Association], stored: list[Association]) -> list[Association]:
    """An implied association a stored one already says is listed once, as the stored."""
    said = {(a.library_file_id, a.target, a.relation) for a in stored}
    return [a for a in implied if (a.library_file_id, a.target, a.relation) not in said]


def _implied_for_file(
    file: NativeFile, catalogue: Catalogue, outputs: OutputStore
) -> list[Association]:
    """The models made from ``file`` (mine only: a built-in is never made from a library
    file), and the model of the output it is a copy of. Reads every model's
    ``model.json``, as `Catalogue.library_pin_index` does."""
    implied: list[Association] = []
    if file.output_id is not None:
        try:
            _, slug = outputs.name_and_slug(file.output_id)
        except (OutputNotFoundError, OSError, ValueError):
            slug = None
        if slug is not None and catalogue.exists(slug):
            implied.append(
                Association(
                    id=None,
                    library_file_id=file.id,
                    library_file_name=file.name,
                    relation="output_of",
                    target=PrintSubject.model(slug).key,
                    implied_by="output",
                )
            )
    for slug in catalogue.slugs():
        if not is_builtin(slug) and _mesh_source_file(catalogue, slug) == file.id:
            implied.append(
                Association(
                    id=None,
                    library_file_id=file.id,
                    library_file_name=file.name,
                    relation="source_of",
                    target=PrintSubject.model(slug).key,
                    implied_by="mesh_source",
                )
            )
    return implied


def _implied_for_model(
    slug: str, native: NativeLibrary, catalogue: Catalogue, outputs: OutputStore
) -> list[Association]:
    """The live file the model was made from, and every live copy of its outputs."""
    target = PrintSubject.model(slug).key
    implied: list[Association] = []
    source = _mesh_source_file(catalogue, slug)
    if source is not None:
        file = native.rows.file(source)
        if file is not None:
            implied.append(
                Association(
                    id=None,
                    library_file_id=file.id,
                    library_file_name=file.name,
                    relation="source_of",
                    target=target,
                    implied_by="mesh_source",
                )
            )
    output_ids = outputs.ids_for(slug)
    for file in native.rows.live_for_outputs(output_ids) if output_ids else []:
        implied.append(
            Association(
                id=None,
                library_file_id=file.id,
                library_file_name=file.name,
                relation="output_of",
                target=target,
                implied_by="output",
            )
        )
    return implied


def _require_native(native: NativeLibrary | None) -> NativeLibrary:
    if native is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "associations are kept for ScadBuddy's own library only (library_backend=native)",
            type_=NOT_NATIVE_PROBLEM,
        )
    return native


async def _live_file(native: NativeLibrary, file_id: int) -> NativeFile:
    file = await asyncio.to_thread(native.rows.file, file_id) if is_native_id(file_id) else None
    if file is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no library file {file_id}")
    return file


@router.get(
    "/print/library/{file_id}/associations",
    response_model=Associations,
    summary="A library file's associations, both ways",
)
async def get_file_associations(
    file_id: FileIdPath,
    library: PrintLibraryDep,
    catalogue: CatalogueDep,
    outputs: OutputsDep,
) -> Associations:
    """Every association of the file (#6395): its own (it relates to a target) first,
    then those of other files naming it as their target, and the implied ones (``id``
    null, ``implied_by`` set): a model made from it is listed as the file being the
    ``source_of`` it, and the model of the output the file is a copy of as ``output_of``.
    A file in the trash, or one the library does not have, is a 404; an association with
    a file in the trash is left out. On Bambuddy's library the list is empty for a Bambuddy
    file's id; an id in ScadBuddy's own range is a 404 there, as every file route's is."""
    native = await _native(library)
    if native is None:
        return Associations(associations=[])
    file = await _live_file(native, file_id)
    stored = await asyncio.to_thread(library.associations.for_file, file_id)
    implied = await asyncio.to_thread(_implied_for_file, file, catalogue, outputs)
    found = stored + _without_stored(implied, stored)
    return Associations(associations=await asyncio.to_thread(_named, found, catalogue))


@router.post(
    "/print/library/{file_id}/associations",
    response_model=Association,
    status_code=status.HTTP_201_CREATED,
    summary="Associate a library file with a model or another library file",
)
async def post_file_association(
    file_id: FileIdPath,
    body: AssociationRequest,
    response: Response,
    library: PrintLibraryDep,
    catalogue: CatalogueDep,
) -> Association:
    """Records that the file is ``relation`` the target, said from the file's side
    (#6395): ``output_of``, ``variant_of``, ``source_of`` or ``related_to`` a model
    (``model:<slug>``, which must exist) or another live file of ScadBuddy's own library
    (``library:<id>``). 201 with the new association; the same file, target and relation
    again is 200 with the one already there, its ``note`` replaced. A file in the trash,
    or one the library does not have, is a 404; a target that is not there, or the file
    itself, a 422. On Bambuddy's library, a 409 for a Bambuddy file's id; an id in
    ScadBuddy's own range is a 404 there, as every file route's is."""
    native = _require_native(await _native(library))
    await _live_file(native, file_id)
    target = PrintSubject.parse(body.target)
    if target.slug is not None:
        try:
            await asyncio.to_thread(require_model_exists, catalogue, target.slug)
        except ApiError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, error.detail) from None
    elif target.file_id == file_id:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "a file is not associated with itself"
        )
    elif target.file_id is not None and not is_native_id(target.file_id):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"no library file {target.file_id}")
    try:
        made, created = await asyncio.to_thread(
            library.associations.add,
            file_id,
            target,
            body.relation,
            body.note,
            current_actor(),
        )
    except TargetGoneError:
        if await asyncio.to_thread(native.rows.file, file_id) is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no library file {file_id}") from None
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"no library file {target.file_id}"
        ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    [named] = await asyncio.to_thread(_named, [made], catalogue)
    return named


@router.delete(
    "/print/library/associations/{association_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a library file's association",
)
async def delete_file_association(association_id: UUID, library: PrintLibraryDep) -> Response:
    """Removes a stored association (#6395). One that is not there, or names a file in
    the trash, is a 404. An implied one has no id and cannot be removed. On Bambuddy's
    library, a 409."""
    _require_native(await _native(library))
    if not await asyncio.to_thread(library.associations.remove, association_id):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no association {association_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/models/{slug}/associations",
    response_model=Associations,
    summary="The library files associated with a model",
)
async def get_model_associations(
    slug: SlugPath,
    library: PrintLibraryDep,
    catalogue: CatalogueDep,
    outputs: OutputsDep,
) -> Associations:
    """Every live library file associated with the model (#6395), each said from the
    file's side (``relation``: the file is an ``output_of`` the model, …), and the implied
    ones (``id`` null, ``implied_by`` set): the file the model was made from
    (``source_of``) and each library copy of its outputs (``output_of``). A model that
    does not exist is a 404. On Bambuddy's library the list is empty."""
    await asyncio.to_thread(require_model_exists, catalogue, slug)
    native = await _native(library)
    if native is None:
        return Associations(associations=[])
    stored = await asyncio.to_thread(library.associations.for_model, slug)
    implied = await asyncio.to_thread(_implied_for_model, slug, native, catalogue, outputs)
    found = stored + _without_stored(implied, stored)
    return Associations(associations=await asyncio.to_thread(_named, found, catalogue))
