"""Hosted plugins (#6932, spec 2026-11-21 §6.2 and §6.3): a Claude plugin written in
ScadBuddy as a draft of files, published as immutable numbered versions.

- ``POST /hosted-plugins`` creates one (409 ``hosted-plugin-exists``; 409
  ``package-name-taken`` when an installed or built-in package has the name, plan ruling
  R3); ``GET /hosted-plugins``, ``GET``, ``PATCH`` (``description``, ``category``,
  ``tags``) and ``DELETE /hosted-plugins/{name}`` list, read, change and delete it
  (with its versions and, once one was installed from it, its package);
- ``GET /{name}/files`` the draft; ``GET``, ``PUT`` (the raw bytes as the body,
  ``?executable=true`` for a script under ``skills/``) and ``DELETE /{name}/files/{path}``
  one file; ``POST /{name}/files/moves`` a rename; ``GET /{name}/draft-diff`` the draft
  against the latest version, with ``manifest_changed`` when the description differs;
- ``POST /{name}/publish`` the next version: the draft checked (422 ``draft-invalid`` with
  ``problems``), ``plugin.json`` generated, the zip built (`hosted_plugins/bundle.py`) and
  stored, and the version row and ``latest_version`` written in one transaction. A draft
  equal to the latest version, with the same description, is 409 ``nothing-to-publish``;
  one whose files or description changed while it was built, 409 ``draft-changed``.

Path rules and caps are `hosted_plugins/rules.py`'s. The routes are ``api`` routes
(`auth/routes.py`): a person, an access token with ``write`` for a write, or the agent's
service credential, which the MCP tools of Task 7 (#6938) call them with. Nothing here vets
the plugin or touches its package's pin: the agent's installer does (spec §6.3 step 5).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.api.deps import StateDep
from scadbuddy.auth.component import CallerDep
from scadbuddy.auth.models import Anonymous, Caller, Person, PersonalToken, Service
from scadbuddy.core.problems import ApiError
from scadbuddy.hosted_plugins import bundle, rules, store
from scadbuddy.hosted_plugins.blobs import blob_bytes
from scadbuddy.orders.attachments import keep_file

router = APIRouter(prefix="/hosted-plugins", tags=["hosted-plugins"])

#: The routes' reasons.
PLUGIN_EXISTS = "hosted-plugin-exists"
PLUGIN_NOT_FOUND = "hosted-plugin-not-found"
PACKAGE_NAME_TAKEN = "package-name-taken"
NAME_REFUSED = "name-refused"
PATH_REFUSED = "path-refused"
PATH_CLASH = "path-clash"
FILE_NOT_FOUND = "file-not-found"
FILE_EXISTS = "file-exists"
FILE_TOO_LARGE = "file-too-large"
TOO_MANY_FILES = "too-many-files"
DRAFT_TOO_LARGE = "draft-too-large"
DRAFT_INVALID = "draft-invalid"
DRAFT_CHANGED = "draft-changed"
NOTHING_TO_PUBLISH = "nothing-to-publish"

#: The agent's own cap on a package's tree (``agent/src/plugins/packages/hash.ts``
#: ``MAX_TOTAL_BYTES``), which the published tree, manifest included, must fit.
PACKAGE_MAX_TOTAL_BYTES = 20 * 1024 * 1024

Name = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
TAG = r"^[a-z0-9][a-z0-9-]{0,31}$"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HostedPluginCreate(_Body):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=1024)
    category: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    tags: list[Annotated[str, Field(pattern=TAG)]] = Field(default_factory=list, max_length=20)


class HostedPluginUpdate(_Body):
    """The fields a plugin may change after create, checked as ``HostedPluginCreate`` checks
    them. An absent field stays; ``category`` null clears it. The name never changes."""

    description: str | None = Field(default=None, max_length=1024)
    category: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    tags: list[Annotated[str, Field(pattern=TAG)]] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _only_category_clears(self) -> HostedPluginUpdate:
        for field in ("description", "tags"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class HostedPlugin(BaseModel):
    name: str
    description: str
    category: str | None
    tags: list[str]
    #: Null until the first publish.
    latest_version: int | None
    file_count: int
    total_bytes: int
    created_at: datetime
    updated_at: datetime


class HostedPluginFile(BaseModel):
    path: str
    sha256: str
    size: int
    executable: bool
    updated_at: datetime


class HostedPluginFiles(BaseModel):
    files: list[HostedPluginFile]
    total_bytes: int
    max_files: int = rules.MAX_FILES
    max_file_bytes: int = rules.MAX_FILE_BYTES
    max_total_bytes: int = rules.MAX_TOTAL_BYTES


class HostedPluginMove(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = Field(alias="from", min_length=1, max_length=rules.MAX_PATH_BYTES)
    target: str = Field(alias="to", min_length=1, max_length=rules.MAX_PATH_BYTES)


class HostedPluginDraftDiff(BaseModel):
    #: The version the draft is compared with; null before the first publish (every
    #: draft file is then added).
    base_version: int | None
    added: list[str]
    removed: list[str]
    changed: list[str]
    #: True when the generated ``.claude-plugin/plugin.json`` differs from the base's other
    #: than its version number (a ``PATCH`` changed the description). A draft with only this
    #: change still publishes. False before the first publish.
    manifest_changed: bool


class HostedPluginVersion(BaseModel):
    version: int
    zip_sha256: str
    content_hash: str
    files: dict[str, Any]
    published_at: datetime
    published_by: str


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _not_found(name: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND, f'no hosted plugin named "{name}"', reason=PLUGIN_NOT_FOUND
    )


def _file_not_found(path: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND, f"the draft has no file {path}", reason=FILE_NOT_FOUND
    )


def _plugin(conn: psycopg.Connection[Any], name: str, *, lock: bool = False) -> Any:
    row = store.plugin(conn, name, lock=lock)
    if row is None:
        raise _not_found(name)
    return row


def _draft_totals(conn: psycopg.Connection[Any], ids: list[int]) -> dict[int, tuple[int, int]]:
    """Each plugin's draft file count and bytes, in one query; a plugin with no file is absent."""
    rows = conn.execute(
        "SELECT plugin_id, count(*) AS n, coalesce(sum(size), 0) AS total"
        " FROM ai_hosted_plugin_files WHERE plugin_id = ANY(%s) GROUP BY plugin_id",
        (ids,),
    ).fetchall()
    return {r["plugin_id"]: (r["n"], r["total"]) for r in rows}


def _view(conn: psycopg.Connection[Any], row: Any) -> HostedPlugin:
    return _shown(row, _draft_totals(conn, [row["id"]]))


def _views(conn: psycopg.Connection[Any], rows: list[Any]) -> list[HostedPlugin]:
    totals = _draft_totals(conn, [r["id"] for r in rows])
    return [_shown(r, totals) for r in rows]


def _shown(row: Any, totals: dict[int, tuple[int, int]]) -> HostedPlugin:
    count, size = totals.get(row["id"], (0, 0))
    return HostedPlugin(
        name=row["name"],
        description=row["description"],
        category=row["category"],
        tags=list(row["tags"]),
        latest_version=row["latest_version"],
        file_count=count,
        total_bytes=size,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _checked(path: str, *, executable: bool = False) -> str:
    try:
        rules.check_path(path)
        rules.check_executable(path, executable=executable)
    except rules.PathRefusedError as err:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{path}: {err}", reason=PATH_REFUSED
        ) from None
    return path


def _no_clash(path: str, others: list[str]) -> None:
    why = rules.clash(path, others)
    if why is not None:
        raise ApiError(status.HTTP_409_CONFLICT, why, reason=PATH_CLASH)


def _by(caller: Caller) -> str:
    """Who published, for the version row."""
    if isinstance(caller, Person | PersonalToken):
        return caller.principal
    if isinstance(caller, Service):
        return f"service:{caller.caller}"
    assert isinstance(caller, Anonymous)
    return "anonymous"


def _file_list(rows: list[Any]) -> dict[str, dict[str, Any]]:
    by_path = {r["path"]: r for r in rows}
    return {
        p: {
            "sha256": by_path[p]["sha256"],
            "size": by_path[p]["size"],
            "executable": by_path[p]["executable"],
        }
        for p in sorted(by_path, key=rules.sort_key)
    }


def _without_manifest(files: dict[str, Any]) -> dict[str, Any]:
    return {p: v for p, v in files.items() if p != rules.MANIFEST_PATH}


def _manifest_changed(row: Any, base: Any | None) -> bool:
    """Whether the plugin's name or description differs from what ``base``'s plugin.json holds:
    the manifest rebuilt at the base's own version number has other bytes."""
    if base is None:
        return False
    rebuilt = bundle.plugin_json(row["name"], row["description"], base["version"])
    held: str = base["files"][rules.MANIFEST_PATH]["sha256"]
    return hashlib.sha256(rebuilt).hexdigest() != held


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a hosted plugin with an empty draft",
)
async def create_hosted_plugin(body: HostedPluginCreate, state: StateDep) -> HostedPlugin:
    if body.name in rules.BUILT_IN_NAMES:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f'"{body.name}" is a plugin built into ScadBuddy',
            reason=PACKAGE_NAME_TAKEN,
        )
    problem = rules.name_problem(body.name)
    if problem is not None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, problem, reason=NAME_REFUSED)

    def run(conn: psycopg.Connection[Any]) -> HostedPlugin:
        try:
            row = store.create(conn, body.name, body.description, body.category, body.tags)
        except store.PackageNameTakenError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'a plugin package named "{body.name}" is installed',
                reason=PACKAGE_NAME_TAKEN,
            ) from None
        except store.PluginExistsError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'a hosted plugin named "{body.name}" exists',
                reason=PLUGIN_EXISTS,
            ) from None
        return _view(conn, row)

    return await _in_tx(state, run)


@router.get("", summary="List the hosted plugins")
async def list_hosted_plugins(state: StateDep) -> list[HostedPlugin]:
    return await _in_tx(state, lambda conn: _views(conn, store.plugins(conn)))


@router.get("/{name}", summary="Read a hosted plugin")
async def get_hosted_plugin(name: Name, state: StateDep) -> HostedPlugin:
    return await _in_tx(state, lambda conn: _view(conn, _plugin(conn, name)))


@router.patch("/{name}", summary="Change a hosted plugin's description, category or tags")
async def update_hosted_plugin(
    name: Name, body: HostedPluginUpdate, state: StateDep
) -> HostedPlugin:
    def run(conn: psycopg.Connection[Any]) -> HostedPlugin:
        row = _plugin(conn, name, lock=True)
        row = store.update(conn, row, body.model_dump(include=body.model_fields_set))
        return _view(conn, row)

    return await _in_tx(state, run)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a hosted plugin, its versions and its package",
)
async def delete_hosted_plugin(name: Name, state: StateDep) -> None:
    def run(conn: psycopg.Connection[Any]) -> None:
        store.delete(conn, _plugin(conn, name, lock=True))

    await _in_tx(state, run)


@router.get("/{name}/files", summary="List a hosted plugin's draft")
async def list_hosted_plugin_files(name: Name, state: StateDep) -> HostedPluginFiles:
    def run(conn: psycopg.Connection[Any]) -> HostedPluginFiles:
        rows = store.draft(conn, _plugin(conn, name)["id"])
        rows.sort(key=lambda r: rules.sort_key(r["path"]))
        return HostedPluginFiles(
            files=[HostedPluginFile.model_validate(dict(r)) for r in rows],
            total_bytes=sum(r["size"] for r in rows),
        )

    return await _in_tx(state, run)


@router.get(
    "/{name}/files/{path:path}",
    summary="Read one draft file's bytes",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def get_hosted_plugin_file(name: Name, path: str, state: StateDep) -> Response:
    def run(conn: psycopg.Connection[Any]) -> Any:
        row = store.draft_file(conn, _plugin(conn, name)["id"], path)
        if row is None:
            raise _file_not_found(path)
        return row

    row = await _in_tx(state, run)
    data = await blob_bytes(state, row["sha256"])
    # Never served as itself: an HTML or SVG file in a draft does not run on this origin.
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "X-ScadBuddy-Executable": "true" if row["executable"] else "false",
        },
    )


async def _read_body(request: Request) -> bytes:
    too_large = ApiError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"a file is at most {rules.MAX_FILE_BYTES} bytes",
        reason=FILE_TOO_LARGE,
    )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > rules.MAX_FILE_BYTES:
        raise too_large
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > rules.MAX_FILE_BYTES:
            raise too_large
    return bytes(data)


@router.put(
    "/{name}/files/{path:path}",
    summary="Write one draft file (the raw bytes as the body)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def put_hosted_plugin_file(
    name: Name,
    path: str,
    request: Request,
    state: StateDep,
    executable: Annotated[bool, Query()] = False,
) -> HostedPluginFile:
    _checked(path, executable=executable)
    await _in_tx(state, lambda conn: _plugin(conn, name))
    data = await _read_body(request)
    sha256 = hashlib.sha256(data).hexdigest()
    # Stored before the row names it; until the row's hold commits, the store's grace keeps it.
    await keep_file(state.store.blobs, io.BytesIO(data), sha256)

    def run(conn: psycopg.Connection[Any]) -> HostedPluginFile:
        row = _plugin(conn, name, lock=True)
        rows = store.draft(conn, row["id"])
        others = [r["path"] for r in rows if r["path"] != path]
        _no_clash(path, others)
        if len(others) == len(rows) and len(rows) >= rules.MAX_FILES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"a draft holds at most {rules.MAX_FILES} files",
                reason=TOO_MANY_FILES,
            )
        total = sum(r["size"] for r in rows if r["path"] != path) + len(data)
        if total > rules.MAX_TOTAL_BYTES:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"a draft holds at most {rules.MAX_TOTAL_BYTES} bytes",
                reason=DRAFT_TOO_LARGE,
            )
        written = store.write_file(conn, row["id"], path, sha256, len(data), executable)
        return HostedPluginFile.model_validate(dict(written))

    return await _in_tx(state, run)


@router.delete(
    "/{name}/files/{path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one draft file",
)
async def delete_hosted_plugin_file(name: Name, path: str, state: StateDep) -> None:
    def run(conn: psycopg.Connection[Any]) -> None:
        if not store.delete_file(conn, _plugin(conn, name, lock=True)["id"], path):
            raise _file_not_found(path)

    await _in_tx(state, run)


@router.post("/{name}/files/moves", summary="Rename one draft file")
async def move_hosted_plugin_file(
    name: Name, body: HostedPluginMove, state: StateDep
) -> HostedPluginFile:
    _checked(body.target)

    def run(conn: psycopg.Connection[Any]) -> HostedPluginFile:
        row = _plugin(conn, name, lock=True)
        rows = {r["path"]: r for r in store.draft(conn, row["id"])}
        moved = rows.get(body.source)
        if moved is None:
            raise _file_not_found(body.source)
        if body.target == body.source:
            return HostedPluginFile.model_validate(dict(moved))
        _checked(body.target, executable=moved["executable"])
        if body.target in rows:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"the draft already has {body.target}",
                reason=FILE_EXISTS,
            )
        _no_clash(body.target, [p for p in rows if p != body.source])
        return HostedPluginFile.model_validate(
            dict(store.move_file(conn, row["id"], body.source, body.target))
        )

    return await _in_tx(state, run)


@router.get("/{name}/draft-diff", summary="The draft against the latest version")
async def hosted_plugin_draft_diff(name: Name, state: StateDep) -> HostedPluginDraftDiff:
    def run(conn: psycopg.Connection[Any]) -> HostedPluginDraftDiff:
        row = _plugin(conn, name)
        base = store.latest(conn, row)
        before = {} if base is None else _without_manifest(base["files"])
        diff = bundle.diff_files(before, _file_list(store.draft(conn, row["id"])))
        return HostedPluginDraftDiff(
            base_version=None if base is None else base["version"],
            manifest_changed=_manifest_changed(row, base),
            **diff,
        )

    return await _in_tx(state, run)


@router.post("/{name}/publish", summary="Publish the draft as the next version")
async def publish_hosted_plugin(
    name: Name, state: StateDep, caller: CallerDep
) -> HostedPluginVersion:
    def read(conn: psycopg.Connection[Any]) -> tuple[Any, list[Any], Any | None]:
        row = _plugin(conn, name)
        return row, store.draft(conn, row["id"]), store.latest(conn, row)

    row, rows, base = await _in_tx(state, read)
    draft = {r["path"]: (await blob_bytes(state, r["sha256"]), r["executable"]) for r in rows}
    found = bundle.problems(draft)
    number = (row["latest_version"] or 0) + 1
    tree = bundle.published_tree(
        draft, name=row["name"], description=row["description"], version=number
    )
    if sum(len(data) for data, _ in tree.values()) > PACKAGE_MAX_TOTAL_BYTES:
        found.append(
            f"the plugin with its plugin.json is larger than {PACKAGE_MAX_TOTAL_BYTES} bytes"
        )
    if found:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "the draft cannot be published as it is",
            reason=DRAFT_INVALID,
            problems=found,
        )
    listed = bundle.file_list(tree)
    if (
        base is not None
        and _without_manifest(base["files"]) == _without_manifest(listed)
        and not _manifest_changed(row, base)
    ):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"the draft is the same as version {base['version']}",
            reason=NOTHING_TO_PUBLISH,
        )
    zipped = await asyncio.to_thread(bundle.build_zip, tree)
    zip_sha256 = hashlib.sha256(zipped).hexdigest()
    await keep_file(state.store.blobs, io.BytesIO(zipped), zip_sha256)
    read_draft = _file_list(rows)
    by = _by(caller)

    def write(conn: psycopg.Connection[Any]) -> HostedPluginVersion:
        current = _plugin(conn, name, lock=True)
        if (
            current["id"] != row["id"]
            or current["latest_version"] != row["latest_version"]
            or current["description"] != row["description"]
            or _file_list(store.draft(conn, row["id"])) != read_draft
        ):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "the draft, its description or its versions changed while it was published; "
                "publish again",
                reason=DRAFT_CHANGED,
            )
        version = store.publish(
            conn,
            row["id"],
            number,
            zip_sha256,
            bundle.content_hash(listed),
            listed,
            by,
        )
        return HostedPluginVersion.model_validate(dict(version))

    return await _in_tx(state, write)
