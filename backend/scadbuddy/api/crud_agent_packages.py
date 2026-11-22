"""The agent's plugin packages behind its CRUD routes (#5299 step 5, area
``plugin-packages``): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential
only (`crud_router`).

Each route is one method of ``agent/src/plugins/packages/store.ts`` ``PackageStore`` and
runs what it ran over SQL, a transaction as one transaction (#5299 D2): the package row's
``FOR UPDATE``, the prune of the rows the current review no longer has
(`agent_data/packages.py` `prune_stale_rows`) and the warm-evict NOTIFY
(``scadbuddy_warm_evict``), so a warm Claude Code loads the packages of its time. Every
answer is the raw row (``PackageRow``): the agent's ``view`` and ``pin`` still shape it, and
its ``CrudPackageStore`` is the store over these routes. The agent still fetches, vets and
hashes a package; the API stores the pin it was given.

- ``GET …/plugin-packages`` every package, by name (``?enabled=true``: the enabled pins);
  ``GET …/plugin-packages/{name}`` one, or none;
- ``POST …/plugin-packages`` an install: stored unapproved and disabled, the rows an older
  replica's remove left forgotten; a name taken is 409 ``package-exists``;
- ``POST …/{name}/pending`` a re-pin under review, ``…/discard-pending`` its discard;
- ``POST …/{name}/approve`` the approval of exactly the commit and hash the review showed
  (409 ``pin-mismatch``); a review that lists refusals needs ``allow_refused`` (409
  ``refused-needs-allow``); a pending pin fetched from another place is approved disabled;
- ``POST …/{name}/enabled`` the switch (409 ``package-unapproved`` before an approval);
- ``POST …/{name}/delete`` the uninstall, with every access row and listing of the package.

A pin is a commit (``pin_kind`` ``commit``, a repository source) or bytes (``bytes``, #6935:
an archive or npm package, or a marketplace entry of either, pinned to the sha256 of the
file the agent fetched). A bytes pin is stored only while its mirror is held (409
``mirror-missing``); the agent sends the file first to area ``package-mirrors``:

- ``PUT …/package-mirrors/{sha256}`` the raw bytes, at most `MAX_MIRROR_BYTES`, named by
  their sha256 (422 otherwise), kept as a blob and held (`agent_data/package_mirrors.py`);
  204;
- ``GET …/package-mirrors/{sha256}`` the bytes, or 404 ``mirror-not-found``.

Every package write also releases the holds of mirrors no pin names, past their grace.

A package that is not there is 404 ``package-not-found``. Each refusal's text is the
agent's ``PluginError``'s, which the agent raises again with the route's status.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, Self

import psycopg
from fastapi import Path, Query, Request, status
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scadbuddy.agent_data import package_mirrors
from scadbuddy.agent_data.access import notify_warm_evict
from scadbuddy.agent_data.package_mirrors import MAX_MIRROR_BYTES
from scadbuddy.agent_data.packages import forget_package, prune_stale_rows
from scadbuddy.api.deps import StateDep
from scadbuddy.api.limits import RouteLimit
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: The table's CHECKs (``ai_plugin_packages``).
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
COMMIT_PATTERN = r"^([0-9a-f]{40}|[0-9a-f]{64})$"
HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
URL_MAX = 2048
TEXT_MAX = 1024
#: The largest JSON text of a pin's file list and of its review.
FILES_JSON_MAX = 16 * 1024 * 1024
REVIEW_JSON_MAX = 4 * 1024 * 1024

#: The route's reasons.
PACKAGE_EXISTS = "package-exists"
PACKAGE_NOT_FOUND = "package-not-found"
PACKAGE_PINNED = "package-pinned"
PACKAGE_NAME_DIFFERS = "package-name-differs"
PIN_MISMATCH = "pin-mismatch"
REFUSED_NEEDS_ALLOW = "refused-needs-allow"
PACKAGE_UNAPPROVED = "package-unapproved"
MIRROR_MISSING = "mirror-missing"
MIRROR_NOT_FOUND = "mirror-not-found"

Name = Annotated[str, Path(pattern=NAME_PATTERN)]
Sha256 = Annotated[str, Path(pattern=SHA256_PATTERN)]

#: Where a package comes from (the table's CHECK). ``hosted`` is the schema's only (#6934).
SourceKind = Literal["git", "marketplace", "marketplace-url", "archive", "npm", "hosted"]
#: The kinds an install here may name.
InstallKind = Literal["git", "marketplace", "marketplace-url", "archive", "npm"]
PinKind = Literal["commit", "bytes"]
#: The kinds that name a marketplace entry.
MARKETPLACE_KINDS = frozenset({"marketplace", "marketplace-url"})
#: The kinds whose ref names what was asked for (a branch, tag or commit), never empty.
REF_KINDS = frozenset({"git", "marketplace"})


def pin_fits(source_kind: str, pin_kind: str) -> bool:
    """The table's ``ai_plugin_packages_source_pin_check``: which pin a source can have."""
    match source_kind:
        case "git":
            return pin_kind == "commit"
        case "archive" | "npm":
            return pin_kind == "bytes"
        case "hosted":
            return pin_kind == "version"
        case _:
            return pin_kind in ("commit", "bytes")


#: For ``main.py``'s `BodySizeGate`: a mirror's PUT, whatever its content type, refused on
#: its Content-Length or, without one, as the body streams.
MIRROR_ROUTE_LIMIT = RouteLimit(
    "PUT",
    re.compile(r"^/api/v1/internal/crud/agent/package-mirrors/"),
    MAX_MIRROR_BYTES,
    "a plugin package's archive",
    "fixed, agent_data/package_mirrors.py MAX_MIRROR_BYTES",
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bounded(value: dict[str, Any], limit: int, what: str) -> dict[str, Any]:
    if len(json.dumps(value)) > limit:
        raise ValueError(f"{what} is larger than {limit} characters of JSON")
    return value


class PackageRow(BaseModel):
    """One ``ai_plugin_packages`` row, every column."""

    name: str
    source_kind: SourceKind
    source_url: str
    source_ref: str
    marketplace_entry: str | None
    fetch_url: str
    fetch_path: str
    pin_kind: Literal["commit", "bytes", "version"]
    commit_sha: str | None
    bytes_sha256: str | None
    version: int | None
    content_hash: str
    files: dict[str, Any]
    review: dict[str, Any]
    approved_at: datetime | None
    allow_refused: bool
    enabled: bool
    pending_ref: str | None
    pending_fetch_url: str | None
    pending_fetch_path: str | None
    pending_pin_kind: Literal["commit", "bytes", "version"] | None
    pending_commit_sha: str | None
    pending_bytes_sha256: str | None
    pending_version: int | None
    pending_content_hash: str | None
    pending_files: dict[str, Any] | None
    pending_review: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class Package(BaseModel):
    package: PackageRow | None


class Packages(BaseModel):
    packages: list[PackageRow]


class Done(BaseModel):
    done: bool


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _row(row: Any) -> PackageRow:
    return PackageRow.model_validate(dict(row))


def _not_found(name: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        f'no plugin package named "{name}"',
        reason=PACKAGE_NOT_FOUND,
    )


def _refused(review: dict[str, Any]) -> list[str]:
    """``store.ts`` ``withRefused``: a review stored before ``refused`` existed has none."""
    refused = review.get("refused")
    return list(refused) if isinstance(refused, list) else []


def _pin_id(row: Any, prefix: str = "") -> str | None:
    """The row's (or with ``pending_``, its re-pin's) commit or bytes sha256."""
    kind = row[f"{prefix}pin_kind"]
    if kind == "commit":
        return row[f"{prefix}commit_sha"]  # type: ignore[no-any-return]
    if kind == "bytes":
        return row[f"{prefix}bytes_sha256"]  # type: ignore[no-any-return]
    return None


def _mirror_held(conn: psycopg.Connection[Any], body: Pin) -> None:
    """A bytes pin is stored only while its mirror is held: a load reads that copy."""
    if body.bytes_sha256 is not None and not package_mirrors.held(conn, body.bytes_sha256):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"no mirror of the bytes {body.bytes_sha256} is kept; send them first",
            reason=MIRROR_MISSING,
        )


def _written(conn: psycopg.Connection[Any]) -> None:
    """What every package write ends with: the warm-evict NOTIFY, and the release of the
    mirrors no pin names any more."""
    package_mirrors.release_unnamed(conn)
    notify_warm_evict(conn)


def _locked(conn: psycopg.Connection[Any], name: str) -> Any:
    row = conn.execute(
        "SELECT * FROM ai_plugin_packages WHERE name = %s FOR UPDATE", (name,)
    ).fetchone()
    if row is None:
        raise _not_found(name)
    return row


@router.get("/plugin-packages", name="crud_agent_plugin_packages")
async def plugin_packages(state: StateDep, enabled: Annotated[bool, Query()] = False) -> Packages:
    where = " WHERE enabled" if enabled else ""

    def run(conn: psycopg.Connection[Any]) -> list[PackageRow]:
        rows = conn.execute(f"SELECT * FROM ai_plugin_packages{where} ORDER BY name").fetchall()
        return [_row(r) for r in rows]

    return Packages(packages=await _in_tx(state, run))


@router.get("/plugin-packages/{name}", name="crud_agent_plugin_package")
async def plugin_package(name: Name, state: StateDep) -> Package:
    def run(conn: psycopg.Connection[Any]) -> PackageRow | None:
        row = conn.execute("SELECT * FROM ai_plugin_packages WHERE name = %s", (name,)).fetchone()
        return None if row is None else _row(row)

    return Package(package=await _in_tx(state, run))


class Pin(_Body):
    """A fetched, vetted and hashed commit or file, as the agent prepared it."""

    fetch_url: str = Field(min_length=1, max_length=URL_MAX)
    fetch_path: str = Field(max_length=TEXT_MAX)
    pin_kind: PinKind = "commit"
    commit_sha: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    bytes_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    content_hash: str = Field(pattern=HASH_PATTERN)
    files: dict[str, Any]
    review: dict[str, Any]

    @field_validator("files")
    @classmethod
    def _files(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded(value, FILES_JSON_MAX, "files")

    @field_validator("review")
    @classmethod
    def _review(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded(value, REVIEW_JSON_MAX, "review")

    @model_validator(mode="after")
    def _one_pin(self) -> Self:
        commit = self.pin_kind == "commit"
        if commit != (self.commit_sha is not None) or commit == (self.bytes_sha256 is not None):
            field = "commit_sha" if commit else "bytes_sha256"
            raise ValueError(f"a {self.pin_kind} pin names its {field} only")
        return self


class Install(Pin):
    name: str = Field(pattern=NAME_PATTERN)
    source_kind: InstallKind
    source_url: str = Field(min_length=1, max_length=URL_MAX)
    #: The ref asked for: a branch, tag or commit for a repository; an npm version or
    #: dist-tag (empty for a tarball link); an archive's expected ``sha256:`` or empty.
    source_ref: str = Field(max_length=TEXT_MAX)
    marketplace_entry: str | None = Field(default=None, max_length=TEXT_MAX)

    @model_validator(mode="after")
    def _fits(self) -> Self:
        if not pin_fits(self.source_kind, self.pin_kind):
            raise ValueError(f"a {self.source_kind} source cannot have a {self.pin_kind} pin")
        if self.source_kind in REF_KINDS and not self.source_ref:
            raise ValueError(f"a {self.source_kind} source names its ref")
        return self


@router.post(
    "/plugin-packages",
    name="crud_agent_plugin_package_install",
    status_code=status.HTTP_201_CREATED,
)
async def plugin_package_install(body: Install, state: StateDep) -> PackageRow:
    if body.review.get("name") != body.name:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a package's name is its review's name",
            reason=PACKAGE_NAME_DIFFERS,
        )

    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        _mirror_held(conn, body)
        row = conn.execute(
            "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref,"
            " marketplace_entry, fetch_url, fetch_path, pin_kind, commit_sha, bytes_sha256,"
            " content_hash, files, review)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (name) DO NOTHING RETURNING *",
            (
                body.name,
                body.source_kind,
                body.source_url,
                body.source_ref,
                body.marketplace_entry if body.source_kind in MARKETPLACE_KINDS else None,
                body.fetch_url,
                body.fetch_path,
                body.pin_kind,
                body.commit_sha,
                body.bytes_sha256,
                body.content_hash,
                Jsonb(body.files),
                Jsonb(body.review),
            ),
        ).fetchone()
        if row is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'a plugin package named "{body.name}" is already installed; re-pin it instead',
                reason=PACKAGE_EXISTS,
            )
        # What an older replica's remove left: a re-install starts at the defaults.
        forget_package(conn, body.name)
        _written(conn)
        return _row(row)

    return await _in_tx(state, run)


class Pending(Pin):
    ref: str = Field(max_length=TEXT_MAX)


@router.post("/plugin-packages/{name}/pending", name="crud_agent_plugin_package_pending")
async def plugin_package_pending(name: Name, body: Pending, state: StateDep) -> PackageRow:
    review_name = body.review.get("name")
    if review_name != name:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f'the new commit names the plugin "{review_name}", not "{name}"',
            reason=PACKAGE_NAME_DIFFERS,
        )

    pin_id = body.commit_sha or body.bytes_sha256

    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        current = _locked(conn, name)
        kind = current["source_kind"]
        if not pin_fits(kind, body.pin_kind):
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"a {kind} source cannot have a {body.pin_kind} pin",
            )
        if kind in REF_KINDS and not body.ref:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"a {kind} source names its ref")
        _mirror_held(conn, body)
        if (
            current["pin_kind"] == body.pin_kind
            and _pin_id(current) == pin_id
            and current["content_hash"] == body.content_hash
            and current["fetch_url"] == body.fetch_url
            and current["fetch_path"] == body.fetch_path
        ):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'"{name}" is already pinned to {pin_id}',
                reason=PACKAGE_PINNED,
            )
        row = conn.execute(
            "UPDATE ai_plugin_packages SET pending_ref = %s, pending_fetch_url = %s,"
            " pending_fetch_path = %s, pending_pin_kind = %s, pending_commit_sha = %s,"
            " pending_bytes_sha256 = %s, pending_version = NULL, pending_content_hash = %s,"
            " pending_files = %s, pending_review = %s, updated_at = now()"
            " WHERE name = %s RETURNING *",
            (
                body.ref,
                body.fetch_url,
                body.fetch_path,
                body.pin_kind,
                body.commit_sha,
                body.bytes_sha256,
                body.content_hash,
                Jsonb(body.files),
                Jsonb(body.review),
                name,
            ),
        ).fetchone()
        assert row is not None
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(row)

    return await _in_tx(state, run)


@router.post("/plugin-packages/{name}/discard-pending", name="crud_agent_plugin_package_discard")
async def plugin_package_discard(name: Name, state: StateDep) -> PackageRow:
    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        row = conn.execute(
            "UPDATE ai_plugin_packages SET pending_ref = NULL, pending_fetch_url = NULL,"
            " pending_fetch_path = NULL, pending_pin_kind = NULL, pending_commit_sha = NULL,"
            " pending_bytes_sha256 = NULL, pending_version = NULL, pending_content_hash = NULL,"
            " pending_files = NULL, pending_review = NULL, updated_at = now()"
            " WHERE name = %s RETURNING *",
            (name,),
        ).fetchone()
        if row is None:
            raise _not_found(name)
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(row)

    return await _in_tx(state, run)


class Approve(_Body):
    """The pin the review showed: its commit, or for a bytes pin its bytes' sha256."""

    commit_sha: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    bytes_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    content_hash: str = Field(pattern=HASH_PATTERN)
    allow_refused: bool = False

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.commit_sha is None) == (self.bytes_sha256 is None):
            raise ValueError("name the pin by commit_sha or by bytes_sha256, not both")
        return self

    @property
    def kind(self) -> str:
        return "commit" if self.commit_sha is not None else "bytes"

    @property
    def pin_id(self) -> str:
        return self.commit_sha or self.bytes_sha256 or ""


@router.post("/plugin-packages/{name}/approve", name="crud_agent_plugin_package_approve")
async def plugin_package_approve(name: Name, body: Approve, state: StateDep) -> PackageRow:
    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        current = _locked(conn, name)
        is_pending = (
            current["pending_pin_kind"] == body.kind
            and _pin_id(current, "pending_") == body.pin_id
            and current["pending_content_hash"] == body.content_hash
        )
        is_current = (
            current["pin_kind"] == body.kind
            and _pin_id(current) == body.pin_id
            and current["content_hash"] == body.content_hash
        )
        if not is_pending and not is_current:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "the commit and content hash do not match the pin under review;"
                " reload the review and approve what it shows",
                reason=PIN_MISMATCH,
            )
        # Only what the admin was shown is allowed: a pin with nothing refused is stored
        # without the flag, so a later rule still refuses it.
        refused = _refused(current["pending_review"] if is_pending else current["review"])
        if refused and not body.allow_refused:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"the review lists what the rules refuse ({len(refused)});"
                " approve with allow_refused to load the package as it is",
                reason=REFUSED_NEEDS_ALLOW,
            )
        allow = body.allow_refused and bool(refused)
        if is_pending:
            # A new commit from the same place keeps `enabled`; one fetched from another
            # repository or path is a new source to trust, approved disabled.
            moved = (
                current["pending_fetch_url"] != current["fetch_url"]
                or current["pending_fetch_path"] != current["fetch_path"]
            )
            row = conn.execute(
                "UPDATE ai_plugin_packages SET enabled = enabled AND NOT %s,"
                " source_ref = pending_ref, fetch_url = pending_fetch_url,"
                " fetch_path = pending_fetch_path, pin_kind = pending_pin_kind,"
                " commit_sha = pending_commit_sha, bytes_sha256 = pending_bytes_sha256,"
                " version = pending_version,"
                " content_hash = pending_content_hash, files = pending_files,"
                " review = pending_review, approved_at = now(), allow_refused = %s,"
                " pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL,"
                " pending_pin_kind = NULL, pending_commit_sha = NULL,"
                " pending_bytes_sha256 = NULL, pending_version = NULL,"
                " pending_content_hash = NULL,"
                " pending_files = NULL, pending_review = NULL, updated_at = now()"
                " WHERE name = %s RETURNING *",
                (moved, allow, name),
            ).fetchone()
            assert row is not None
            prune_stale_rows(conn, name, row["review"])
            _written(conn)
            return _row(row)
        if current["approved_at"] is not None and current["allow_refused"] == allow:
            if prune_stale_rows(conn, name, current["review"]):
                notify_warm_evict(conn)
            return _row(current)
        row = conn.execute(
            "UPDATE ai_plugin_packages SET approved_at = now(), allow_refused = %s,"
            " updated_at = now() WHERE name = %s RETURNING *",
            (allow, name),
        ).fetchone()
        assert row is not None
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(row)

    return await _in_tx(state, run)


class Enabled(_Body):
    enabled: bool


@router.post("/plugin-packages/{name}/enabled", name="crud_agent_plugin_package_enabled")
async def plugin_package_enabled(name: Name, body: Enabled, state: StateDep) -> PackageRow:
    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        current = _locked(conn, name)
        if body.enabled and current["approved_at"] is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                'approve the pin before enabling the package (issue #297, "Review before enable")',
                reason=PACKAGE_UNAPPROVED,
            )
        row = conn.execute(
            "UPDATE ai_plugin_packages SET enabled = %s, updated_at = now()"
            " WHERE name = %s RETURNING *",
            (body.enabled, name),
        ).fetchone()
        assert row is not None
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(row)

    return await _in_tx(state, run)


@router.post("/plugin-packages/{name}/delete", name="crud_agent_plugin_package_delete")
async def plugin_package_delete(name: Name, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        gone = conn.execute("DELETE FROM ai_plugin_packages WHERE name = %s", (name,)).rowcount
        if gone == 0:
            return False
        forget_package(conn, name)
        _written(conn)
        return True

    return Done(done=await _in_tx(state, run))


@router.put(
    "/package-mirrors/{sha256}",
    name="crud_agent_package_mirror_put",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def package_mirror_put(sha256: Sha256, request: Request, state: StateDep) -> None:
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
        if digest.hexdigest() != sha256:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "a mirror's name is its bytes' sha256"
            )
        await package_mirrors.keep(state.store.blobs, spool, sha256)

    def run(conn: psycopg.Connection[Any]) -> None:
        package_mirrors.hold(conn, sha256)
        package_mirrors.release_unnamed(conn)

    await _in_tx(state, run)


@router.get(
    "/package-mirrors/{sha256}",
    name="crud_agent_package_mirror",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def package_mirror(sha256: Sha256, state: StateDep) -> FileResponse:
    path = await package_mirrors.path_of(state.store.blobs, sha256)
    if path is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no mirror of the bytes {sha256} is kept",
            reason=MIRROR_NOT_FOUND,
        )
    return FileResponse(path, media_type="application/octet-stream")
