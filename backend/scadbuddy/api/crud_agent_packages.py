"""The agent's plugin package files behind CRUD routes (#5299 step 5): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

Areas ``plugin-packages``, ``plugin-feeds`` and the hosted plugin's row (``hosted-plugins``)
are Go's (``tools/go/core/internal/packages``, #7667). These routes stay here: they read or
write the blob store.

A bytes pin (#6935: an archive or npm package, or a marketplace entry of either, pinned to the
sha256 of the file the agent fetched) is stored only while the package holds its mirror; the
agent sends the file first to area ``package-mirrors``. A commit pin's files are sent there
too, as a zip, once a fetch hashes to the pin, and recorded by Go's
``POST …/plugin-packages/{name}/mirror`` (#7907):

- ``POST …/package-mirrors/{name}`` the raw bytes of a file for the package ``name``, at
  most `MAX_MIRROR_BYTES`, kept as a blob under their sha256 and held for ``name``
  (`agent_data/package_mirrors.py`); answers ``{bytes_sha256}``;
- ``GET …/package-mirrors/{name}/{sha256}`` the bytes, only while a row of ``name`` names that
  sha256 (``bytes_sha256``, ``pending_bytes_sha256``, ``mirror_sha256`` or
  ``pending_mirror_sha256``); else 404 ``mirror-not-found``, even while the blob is still in
  the store.

Area ``hosted-plugins`` (#6932): ``GET …/hosted-plugins/{name}/versions/{N}.zip`` that
version's zip, as published (404 ``hosted-version-not-found``).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
from fastapi import Path, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scadbuddy.agent_data import package_mirrors
from scadbuddy.agent_data.package_mirrors import MAX_MIRROR_BYTES
from scadbuddy.api.deps import StateDep
from scadbuddy.api.limits import RouteLimit
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.hosted_plugins import store as hosted_store
from scadbuddy.hosted_plugins.blobs import blob_bytes

router = crud_router("agent")

#: The table's CHECKs (``ai_plugin_packages``).
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

#: The route's reasons.
MIRROR_NOT_FOUND = "mirror-not-found"
HOSTED_VERSION_NOT_FOUND = "hosted-version-not-found"

Name = Annotated[str, Path(pattern=NAME_PATTERN)]
Sha256 = Annotated[str, Path(pattern=SHA256_PATTERN)]


#: For ``main.py``'s `BodySizeGate`: a mirror's upload, whatever its content type, refused on
#: its Content-Length or, without one, as the body streams.
MIRROR_ROUTE_LIMIT = RouteLimit(
    "POST",
    re.compile(r"^/api/v1/internal/crud/agent/package-mirrors/"),
    MAX_MIRROR_BYTES,
    "a plugin package's archive",
    "fixed, agent_data/package_mirrors.py MAX_MIRROR_BYTES",
)


class Mirrored(BaseModel):
    bytes_sha256: str


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


@router.post("/package-mirrors/{name}", name="crud_agent_package_mirror_post")
async def package_mirror_post(name: Name, request: Request, state: StateDep) -> Mirrored:
    """The raw bytes of a file the agent fetched for the package ``name``, kept under
    their sha256, which the answer names, and held for ``name`` (`package_mirrors.hold`):
    only a pin of ``name`` may then name that sha256, and a load reads them back only once
    one does (`package_mirror`)."""
    too_large = ApiError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"a plugin package's archive is at most {MAX_MIRROR_BYTES} bytes",
    )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_MIRROR_BYTES:
        raise too_large
    with tempfile.TemporaryFile() as spool:
        digest = hashlib.sha256()
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_MIRROR_BYTES:
                raise too_large
            digest.update(chunk)
            await asyncio.to_thread(spool.write, chunk)
        sha256 = digest.hexdigest()
        # The blob is published before the hold's transaction. If that transaction fails, the
        # blob has no hold, and the blob sweep removes it once its grace runs out.
        await package_mirrors.keep(state.store.blobs, spool, sha256)

    def run(conn: psycopg.Connection[Any]) -> None:
        package_mirrors.hold(conn, name, sha256)
        package_mirrors.release_unnamed(conn)

    await _in_tx(state, run)
    return Mirrored(bytes_sha256=sha256)


@router.get(
    "/package-mirrors/{name}/{sha256}",
    name="crud_agent_package_mirror",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def package_mirror(name: Name, sha256: Sha256, state: StateDep) -> FileResponse:
    """The kept bytes of ``sha256``, only while the package ``name``'s pin or pending re-pin
    names them (its bytes, or a commit pin's recorded mirror): a hold released, or another
    package's file, is not served."""
    not_found = ApiError(
        status.HTTP_404_NOT_FOUND,
        f"no mirror of the bytes {sha256} is kept for {name}",
        reason=MIRROR_NOT_FOUND,
    )

    def named(conn: psycopg.Connection[Any]) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM ai_plugin_packages WHERE name = %s"
                " AND %s IN (bytes_sha256, pending_bytes_sha256, mirror_sha256,"
                " pending_mirror_sha256)",
                (name, sha256),
            ).fetchone()
            is not None
        )

    if not await _in_tx(state, named):
        raise not_found
    path = await package_mirrors.path_of(state.store.blobs, sha256)
    if path is None:
        raise not_found
    return FileResponse(path, media_type="application/octet-stream")


@router.get(
    "/hosted-plugins/{name}/versions/{version}.zip",
    name="crud_agent_hosted_plugin_zip",
    response_class=Response,
)
async def hosted_plugin_zip(
    name: Name, version: Annotated[int, Path(ge=1)], state: StateDep
) -> Response:
    """The version's zip, as published; its sha256 and content hash in the headers."""

    def run(conn: psycopg.Connection[Any]) -> Any:
        row = hosted_store.plugin(conn, name)
        found = None if row is None else hosted_store.version(conn, row["id"], version)
        if found is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f'the hosted plugin "{name}" has no version {version}',
                reason=HOSTED_VERSION_NOT_FOUND,
            )
        return found

    found = await _in_tx(state, run)
    data = await blob_bytes(state, found["zip_sha256"])
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "X-ScadBuddy-Zip-Sha256": found["zip_sha256"],
            "X-ScadBuddy-Content-Hash": found["content_hash"],
        },
    )
