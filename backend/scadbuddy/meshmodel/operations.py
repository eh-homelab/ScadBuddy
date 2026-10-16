"""Mesh models' operations (#2146, spec 2026-10-11 §6): ``mesh_model_create``,
``scene_edit`` and ``scene_eject``, on the ``library`` queue, whose worker runs in the
API process with the data volume and the Bambuddy client. The source's bytes arrive by
claim (an upload) or are read in the run (a library file, an output); a scene edit reads
the scene under the history's write lock (`Catalogue.update_files`). Every write names
only the scene's own files (:data:`SCENE_FILE`), never ``model.json`` or ``.git``."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from collections.abc import Callable, Coroutine, Mapping
from functools import partial, wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import status
from pydantic import TypeAdapter, ValidationError

from scadbuddy.api import models as models_api
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.print_source import SLICED_TYPE, LibrarySource, printable
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelExistsError, ModelMeta, UnsafeModelPathError
from scadbuddy.library.operations import PIN_TIMEOUT, answered_as_routes
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, apply_edits, removed_parameters
from scadbuddy.meshmodel.ingest import scene_from_objects
from scadbuddy.meshmodel.meshes import mesh_loader
from scadbuddy.meshmodel.scene import (
    MESHES_DIR,
    SCENE_NAME,
    Scene,
    SceneSource,
    dump_scene,
    exposed_values,
    load_scene,
    mesh_files,
    source_name,
    text_sha256,
)
from scadbuddy.meshmodel.shared import SceneEditError
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import (
    KindsBuild,
    OperationKind,
    to_thread_to_end,
    waiting_on_bambuddy,
)
from scadbuddy.render.bambu3mf import stl_3mf
from scadbuddy.render.objects3mf import UnreadableObjectsError, check_stl, read_objects
from scadbuddy.render.read_budget import ReadBudget, budget_of

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.core.components import Components, Core

EDITS: TypeAdapter[list[Edit]] = TypeAdapter(list[Edit])
SOURCE_NAME = "model.scad"
#: What a mesh model's writes may name: the scene, its compiled source, and the meshes
#: (each part's, and a painted part's codes beside it, `ingest.py`).
SCENE_FILE = re.compile(r"(?:scene\.json|model\.scad|meshes/[A-Za-z0-9_-]+\.(?:3mf|paint\.json))")
#: The compiled ``model.scad``'s cap. A scene at every other cap compiles to hundreds of
#: MB (200 objects of 64 parts and 100 ops); a real file's scene is far under it (the
#: MakerWorld fixtures', `tests/test_mesh_models_fixtures.py`).
MAX_SOURCE_BYTES = 8 * 1024 * 1024
#: A run: the source's download (at most ``MAX_DOWNLOAD_BYTES``) and read under the read
#: budget, a split's measuring, and the commit. A pin's, since the browser and the agent
#: follow a 202 for 15 minutes (`library/operations.py` ``PIN_TIMEOUT``).
RUN_TIMEOUT = PIN_TIMEOUT
HAND_EDITED = (
    "model.scad was edited by hand since the scene last wrote it; eject the scene "
    "(POST /models/{slug}/scene/eject, the agent's eject_scene) to keep editing it as source"
)


def scene_files[V](files: Mapping[str, V]) -> Mapping[str, V]:
    """``files``, or :class:`UnsafeModelPathError` naming those outside :data:`SCENE_FILE`:
    `Catalogue.update_files` writes any path inside the model, ``model.json`` included."""
    stray = sorted(name for name in files if not SCENE_FILE.fullmatch(name))
    if stray:
        raise UnsafeModelPathError(", ".join(stray))
    return files


def compiled(scene: Scene) -> tuple[Scene, str]:
    """The scene with the hash of its compiled ``model.scad``, and that text; refused past
    :data:`MAX_SOURCE_BYTES`."""
    text = compile_scene(scene)
    size = len(text.encode())
    if size > MAX_SOURCE_BYTES:
        raise SceneEditError(
            f"the scene compiles to {size:,} bytes of model.scad; at most "
            f"{MAX_SOURCE_BYTES:,} are allowed (fewer objects, ops or parts)"
        )
    return scene.model_copy(update={"generated_sha256": text_sha256(text)}), text


def hand_edited(directory: Path, scene: Scene) -> bool:
    """Whether ``model.scad`` is no longer what the scene compiled (spec §5): by its
    hash, never a fresh compile. A missing one (a manual edit, a restore) is not what
    the scene compiled either (#2631), so an edit is refused with the eject hint rather
    than failing as a 500."""
    try:
        source = (directory / SOURCE_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return text_sha256(source) != scene.generated_sha256


def has_scene(directory: Path, slug: str) -> None:
    """404 unless the model is a mesh model: it has a ``scene.json``, valid or not."""
    if not (directory / SCENE_NAME).is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} is not a mesh model (it has no scene)")


def read_scene(directory: Path, slug: str) -> Scene:
    """The model's scene; 404 for a model with none, and 422 for a ``scene.json`` that is
    not a valid scene (a manual edit, a restore of a broken one, #2631), naming eject:
    the scene cannot be edited, but ``model.scad`` can once it is plain source."""
    has_scene(directory, slug)
    try:
        return load_scene((directory / SCENE_NAME).read_text(encoding="utf-8"))
    except ValidationError as e:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{slug!r} has a scene.json that is not a valid scene ({e.error_count()} "
            f"errors); eject the scene (POST /models/{slug}/scene/eject, the agent's "
            "eject_scene) to keep the model as source, or restore an earlier version",
        ) from None


def unused_meshes(directory: Path, scene: Scene) -> list[str]:
    """The scene's own files under ``meshes/`` that no part of ``scene`` uses any more (a
    deleted object's): an edit removes them, so they leave the volume and every later
    revision, and ``restore_version`` brings them back with the scene that used them
    (#2632). Eject keeps them: the ejected ``model.scad`` imports the meshes."""
    meshes = directory / MESHES_DIR
    if not meshes.is_dir():
        return []
    used = mesh_files(scene)
    return sorted(
        f"{MESHES_DIR}/{path.name}"
        for path in meshes.iterdir()
        if path.is_file()
        and SCENE_FILE.fullmatch(f"{MESHES_DIR}/{path.name}")
        and f"{MESHES_DIR}/{path.name}" not in used
    )


def stl_as_3mf(data: bytes, name: str, budget: ReadBudget) -> bytes:
    """An untrusted STL as a 3MF: refused past the triangle budget before trimesh loads
    it whole (`check_stl`), and as unreadable when it holds no mesh."""
    check_stl(io.BytesIO(data), budget)
    try:
        return stl_3mf(data, model_name=Path(name).stem or "model")
    except ValueError:
        raise UnreadableObjectsError(f"{name} holds no mesh") from None


#: A kind's check or run.
Step = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


def _answered(fn: Step) -> Step:
    """The routes' answers (`answered_as_routes`), and an unreadable file, a refused
    edit or a stray path as a 422 rather than the operation's unexpected 500."""
    routed = answered_as_routes(fn)

    @wraps(fn)
    async def answered(*args: Any) -> dict[str, Any]:
        try:
            return await routed(*args)
        except SceneEditError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except UnreadableObjectsError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except UnsafeModelPathError as error:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"a mesh model writes only scene.json, model.scad and meshes/, not {error}",
            ) from None

    return answered


def mesh_kinds(state: AppState) -> list[OperationKind]:
    def _record(record: Any) -> dict[str, Any]:
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    async def _uploaded(source: dict[str, Any]) -> bytes:
        try:
            data = await asyncio.to_thread(ClaimStore(state.paths.claims).get, source["claim"])
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                # Not "send it again": a re-send with the same key replays this answer.
                "this request's upload is no longer held; upload the file again",
            ) from None
        return data

    async def source_bytes(
        request: dict[str, Any], checked: dict[str, Any], budget: ReadBudget
    ) -> tuple[bytes, SceneSource]:
        """The file as a 3MF, and where it came from (its own bytes' digest)."""
        source = request["source"]
        if source["kind"] == "upload":
            data, name = await _uploaded(source), source["name"]
        elif source["kind"] == "output":
            found = await state.outputs.model_3mf(source["output_id"])
            if found is None:
                raise ApiError(
                    status.HTTP_404_NOT_FOUND, f"output {source['output_id']!r} has no 3MF"
                )
            data, name = found, f"{source['output_id']}.3mf"
        else:
            # The check read the file's record; only its bytes are fetched here, as they
            # are, so an STL is budgeted below before anything loads it.
            filename, kind = checked["filename"], checked["file_type"]
            library = LibrarySource(
                file_id=source["file_id"], colours=[], plates=[], filename=filename, file_type=kind
            )
            settings = await asyncio.to_thread(state.settings_store.load)
            async with client_for(settings) as client:
                payload = await library.download(client)
            if payload is None:
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, f"{filename} is too large to read"
                )
            data, name = payload, filename
        found_at = SceneSource(
            kind=source["kind"],
            name=source_name(name),
            sha256=hashlib.sha256(data).hexdigest(),
            file_id=source.get("file_id"),
            output_id=source.get("output_id"),
        )
        if name.lower().endswith(".stl") or checked.get("file_type") == "stl":
            data = await asyncio.to_thread(stl_as_3mf, data, name, budget)
        return data, found_at

    async def create_check(request: dict[str, Any]) -> dict[str, Any]:
        models_api._require_new(state.catalogue, request["slug"])
        source = request["source"]
        if source["kind"] != "bambuddy_library":
            return {}
        settings = await asyncio.to_thread(state.settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            file = await client.library_file(source["file_id"])
        kind = (file.file_type or "").lower()
        if kind == SLICED_TYPE or not printable(kind):
            what = "sliced" if kind == SLICED_TYPE else f"a {kind or 'file of unknown type'}"
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{file.filename} is {what}; only an unsliced 3MF or an STL opens as a mesh model",
            )
        return {"filename": file.filename, "file_type": kind}

    async def create_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        # The operator's read budgets, as Arrange reads a library file (`api/outputs.py`).
        budget = budget_of(state.settings)
        data, source = await source_bytes(request, checked, budget)
        read = await asyncio.to_thread(read_objects, data, budget)
        if not read:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{source.name} holds no objects")
        scene, files = scene_from_objects(read, source)
        scene, text = compiled(scene)
        files[SCENE_NAME] = dump_scene(scene).encode()
        meta = ModelMeta(
            name=request["name"], description=request.get("description") or "", mesh_source=source
        )
        create = partial(
            state.catalogue.create, request["slug"], text, meta, files=scene_files(files)
        )

        def created(_: object) -> None:
            emit(state.events, ModelEvent(kind="model.created", slug=request["slug"]))

        try:
            # To its end: a commit that lands after the run's timeout is still announced.
            record = await to_thread_to_end(create, created)
        except ModelExistsError:
            raise ApiError(
                status.HTTP_409_CONFLICT, f"a model named {request['slug']!r} already exists"
            ) from None
        return _record(record)

    async def edit_check(request: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        models_api.require_model_exists(state.catalogue, slug)
        directory = state.paths.model_dir(slug)
        scene = await asyncio.to_thread(read_scene, directory, slug)
        if await asyncio.to_thread(hand_edited, directory, scene):
            raise ApiError(status.HTTP_409_CONFLICT, HAND_EDITED)
        return {}

    def trim_saved_presets(slug: str) -> None:
        """Drop from each saved preset the parameters the scene no longer exposes: the
        scene read and every preset rewritten under the model's preset lock (#2631), so a
        preset saved meanwhile is trimmed from what it holds then, never overwritten from
        an earlier read, and a later edit's trim sees that edit's scene."""
        directory = state.paths.model_dir(slug)
        state.presets.keep_only(slug, lambda: exposed_values(read_scene(directory, slug)))

    async def edit_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        edits = EDITS.validate_python(request["edits"])

        def compute(directory: Path) -> Mapping[str, bytes | None]:
            scene = read_scene(directory, slug)
            if hand_edited(directory, scene):
                raise ApiError(status.HTTP_409_CONFLICT, HAND_EDITED)
            after = apply_edits(scene, edits, mesh_loader(directory))
            removed = removed_parameters(scene, after)
            if removed:
                names = ", ".join(removed)
                own = [
                    p.name
                    for p in state.presets.template_presets(slug)
                    if set(removed) & set(p.params)
                ]
                if own:
                    raise ApiError(
                        status.HTTP_409_CONFLICT,
                        f"the model's own presets {', '.join(own)} set {names}; edit them first",
                    )
                saved = [
                    p for p in state.presets.saved_presets(slug) if set(removed) & set(p.params)
                ]
                if saved and not request["drop_from_presets"]:
                    raise ApiError(
                        status.HTTP_409_CONFLICT,
                        f"the saved presets {', '.join(p.name for p in saved)} set {names}; "
                        "send drop_from_presets to remove it from them",
                    )
            after, text = compiled(after)
            writes: dict[str, bytes | None] = {
                SCENE_NAME: dump_scene(after).encode(),
                SOURCE_NAME: text.encode(),
            }
            writes.update(dict.fromkeys(unused_meshes(directory, after)))
            return scene_files(writes)

        update = partial(state.catalogue.update_files, slug, compute, message=request["message"])
        record = await to_thread_to_end(
            update, lambda _: models_api.announce_source_change(state.events, slug)
        )
        # After the revision, and on every run whatever it removed: a saved preset naming a
        # parameter the scene no longer exposes cannot render, so trimming it is always the
        # repair. That makes a run that died between the commit and this loop (the revision
        # kept, the presets not trimmed) mended by the next edit (review of #2388).
        await asyncio.to_thread(trim_saved_presets, slug)
        return _record(record)

    async def eject_check(request: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]
        models_api.require_model_exists(state.catalogue, slug)
        # Only that it is a mesh model: an invalid scene.json is what eject is for (#2631).
        await asyncio.to_thread(has_scene, state.paths.model_dir(slug), slug)
        return {}

    async def eject_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        slug = request["slug"]

        def drop(directory: Path) -> Mapping[str, bytes | None]:
            return scene_files({SCENE_NAME: None})

        update = partial(state.catalogue.update_files, slug, drop, message="Eject the scene")
        record = await to_thread_to_end(
            update, lambda _: models_api.announce_source_change(state.events, slug)
        )
        return _record(record)

    def kind(name: str, check: Step, run: Step) -> OperationKind:
        return OperationKind(
            name, _answered(check), _answered(run), queue="library", run_timeout=RUN_TIMEOUT
        )

    return [
        kind("mesh_model_create", create_check, create_run),
        kind("scene_edit", edit_check, edit_run),
        kind("scene_eject", eject_check, eject_run),
    ]


def _kinds(core: Core, components: Components) -> list[OperationKind]:
    from scadbuddy.api.deps import AppState

    if not isinstance(core, AppState):
        raise TypeError(
            f"the mesh model kinds need the API's AppState, not a {type(core).__name__}"
        )
    return mesh_kinds(core)


OPERATION_KINDS: KindsBuild = _kinds
