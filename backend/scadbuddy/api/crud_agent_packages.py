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
- ``POST …/{name}/delete`` the uninstall, with every access row and listing of the package;
- ``POST …/{name}/mirror`` the record of a commit pin's mirror (#7907): the sha256 of a zip of
  its files that the package holds (409 ``mirror-missing`` otherwise), set as
  ``mirror_sha256`` when the commit and content hash sent are the pin's, and as
  ``pending_mirror_sha256`` when they are the re-pin's (both when both match); 409
  ``pin-mismatch`` when neither is. It changes no pin, approval or switch.

A pin is a commit (``pin_kind`` ``commit``, a repository source) or bytes (``bytes``, #6935:
an archive or npm package, or a marketplace entry of either, pinned to the sha256 of the
file the agent fetched). A bytes pin is stored only while the package holds its mirror (409
``mirror-missing``); the agent sends the file first to area ``package-mirrors``. A commit
pin's files are sent there too, as a zip, once a fetch hashes to the pin (#7907):

- ``POST …/package-mirrors/{name}`` the raw bytes of a file for the package ``name``, at
  most `MAX_MIRROR_BYTES`, kept as a blob under their sha256 and held for ``name``
  (`agent_data/package_mirrors.py`); answers ``{bytes_sha256}``;
- ``GET …/package-mirrors/{name}/{sha256}`` the bytes, only while a row of ``name`` names that
  sha256 (``bytes_sha256``, ``pending_bytes_sha256``, ``mirror_sha256`` or
  ``pending_mirror_sha256``); else 404 ``mirror-not-found``, even while the blob is still in
  the store.

Every package write also releases the holds of mirrors no pin names, past their grace.

Area ``hosted-plugins`` (#6932): ``GET …/hosted-plugins/{name}`` a hosted plugin with its
latest version's row (``plugin`` null when there is none), and
``GET …/hosted-plugins/{name}/versions/{N}.zip`` that version's zip, as published (404
``hosted-version-not-found``).

Area ``plugin-feeds`` (#6933): ``GET …/plugin-feeds/{feed}`` the feed's name and owner and
every package it lists (only the one ``?package=`` names, when given), each at its current
pin with whether that pin is approved, and for a hosted package the version that pin is
(``feed`` null for a slug no feed has). ``PUT`` and ``DELETE …/plugin-feeds/{feed}/packages/{name}``
add a package to the feed and remove it (#6936); a feed or package that is not there is 404
``feed-not-found`` or ``package-not-found``.

A hosted package (#6934, ``source_kind`` ``hosted``) is pinned to a published version
(``pin_kind`` ``version``): its install and re-pin are stored only when that version of the
hosted plugin of the same name has the content hash sent (409 ``hosted-version-mismatch``),
and every row answered carries ``hosted_latest_version``, the hosted plugin's latest
version, so the agent shows a version that no pin names ("version N not offered", spec
§6.3). A package of any other kind cannot take a hosted plugin's name (409
``hosted-name-taken``, the reverse of ruling R3).

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
from fastapi import Path, Query, Request, Response, status
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scadbuddy.agent_data import package_mirrors
from scadbuddy.agent_data.access import notify_warm_evict
from scadbuddy.agent_data.package_mirrors import MAX_MIRROR_BYTES
from scadbuddy.agent_data.packages import forget_package, prune_stale_rows
from scadbuddy.api.deps import StateDep
from scadbuddy.api.limits import RouteLimit
from scadbuddy.core.events import PluginPackagesChanged, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.hosted_plugins import store as hosted_store
from scadbuddy.hosted_plugins.blobs import blob_bytes

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
HOSTED_VERSION_NOT_FOUND = "hosted-version-not-found"
FEED_NOT_FOUND = "feed-not-found"
HOSTED_VERSION_MISMATCH = "hosted-version-mismatch"
HOSTED_NAME_TAKEN = "hosted-name-taken"

Name = Annotated[str, Path(pattern=NAME_PATTERN)]
Sha256 = Annotated[str, Path(pattern=SHA256_PATTERN)]

#: Where a package comes from (the table's CHECK).
SourceKind = Literal["git", "marketplace", "marketplace-url", "archive", "npm", "hosted"]
PinKind = Literal["commit", "bytes", "version"]
#: The column each pin kind names its pin by.
PIN_FIELD: dict[str, str] = {"commit": "commit_sha", "bytes": "bytes_sha256", "version": "version"}
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


#: For ``main.py``'s `BodySizeGate`: a mirror's upload, whatever its content type, refused on
#: its Content-Length or, without one, as the body streams.
MIRROR_ROUTE_LIMIT = RouteLimit(
    "POST",
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
    #: A commit pin's mirror (#7907): the sha256 of a zip of its files, and of its re-pin's.
    mirror_sha256: str | None = None
    pending_mirror_sha256: str | None = None
    created_at: datetime
    updated_at: datetime
    #: For a ``hosted`` package, its hosted plugin's ``latest_version`` (null otherwise, or
    #: before a publish): a version neither the pin nor the re-pin names is "not offered".
    hosted_latest_version: int | None = None


class Package(BaseModel):
    package: PackageRow | None


class Packages(BaseModel):
    packages: list[PackageRow]


class Done(BaseModel):
    done: bool


class Mirrored(BaseModel):
    bytes_sha256: str


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _rows(conn: psycopg.Connection[Any], rows: list[Any]) -> list[PackageRow]:
    """Rows as every route answers them, a hosted one with its plugin's latest version."""
    hosted = [r["name"] for r in rows if r["source_kind"] == "hosted"]
    latest: dict[str, int | None] = {}
    if hosted:
        latest = {
            r["name"]: r["latest_version"]
            for r in conn.execute(
                "SELECT name, latest_version FROM ai_hosted_plugins WHERE name = ANY(%s)",
                (hosted,),
            ).fetchall()
        }
    return [
        PackageRow.model_validate({**r, "hosted_latest_version": latest.get(r["name"])})
        for r in rows
    ]


def _row(conn: psycopg.Connection[Any], row: Any) -> PackageRow:
    return _rows(conn, [row])[0]


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
    """The row's (or with ``pending_``, its re-pin's) commit, bytes sha256 or version, as
    text."""
    field = PIN_FIELD.get(row[f"{prefix}pin_kind"])
    value = None if field is None else row[f"{prefix}{field}"]
    return None if value is None else str(value)


def _hosted_held(conn: psycopg.Connection[Any], name: str, body: Pin) -> None:
    """A version pin names a published version of the hosted plugin ``name`` whose content
    hash is the one sent: the agent hashed what that version's zip holds."""
    if body.version is None:
        return
    found = conn.execute(
        "SELECT 1 FROM ai_hosted_plugins h JOIN ai_hosted_plugin_versions v ON v.plugin_id = h.id"
        " WHERE h.name = %s AND v.version = %s AND v.content_hash = %s FOR KEY SHARE OF h",
        (name, body.version, body.content_hash),
    ).fetchone()
    if found is None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f'the hosted plugin "{name}" has no version {body.version}'
            f" with the content hash {body.content_hash}",
            reason=HOSTED_VERSION_MISMATCH,
        )


def _mirror_held(conn: psycopg.Connection[Any], name: str, body: Pin) -> None:
    """A bytes pin is stored only while the package's mirror is held: a load reads that
    copy."""
    if body.bytes_sha256 is not None and not package_mirrors.held(conn, name, body.bytes_sha256):
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


def _announced(state: Any, row: PackageRow) -> PackageRow:
    """``row``, once its write committed, told to Settings → Tools & plugins (#7460)."""
    emit(state.events, PluginPackagesChanged(name=row.name))
    return row


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
        return _rows(conn, rows)

    return Packages(packages=await _in_tx(state, run))


@router.get("/plugin-packages/{name}", name="crud_agent_plugin_package")
async def plugin_package(name: Name, state: StateDep) -> Package:
    def run(conn: psycopg.Connection[Any]) -> PackageRow | None:
        row = conn.execute("SELECT * FROM ai_plugin_packages WHERE name = %s", (name,)).fetchone()
        return None if row is None else _row(conn, row)

    return Package(package=await _in_tx(state, run))


class Pin(_Body):
    """A fetched, vetted and hashed commit, file or hosted version, as the agent prepared
    it."""

    fetch_url: str = Field(min_length=1, max_length=URL_MAX)
    fetch_path: str = Field(max_length=TEXT_MAX)
    pin_kind: PinKind = "commit"
    commit_sha: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    bytes_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    version: int | None = Field(default=None, ge=1)
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
        named = [k for k, f in PIN_FIELD.items() if getattr(self, f) is not None]
        if named != [self.pin_kind]:
            raise ValueError(f"a {self.pin_kind} pin names its {PIN_FIELD[self.pin_kind]} only")
        return self

    @property
    def pin_id(self) -> str:
        return str(getattr(self, PIN_FIELD[self.pin_kind]))


class Install(Pin):
    name: str = Field(pattern=NAME_PATTERN)
    source_kind: SourceKind
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
        hosted_store.lock_name(conn, body.name)
        if body.source_kind != "hosted" and hosted_store.plugin(conn, body.name) is not None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'"{body.name}" is the name of a hosted plugin; install that plugin from'
                " Hosted plugins, or a package of another name",
                reason=HOSTED_NAME_TAKEN,
            )
        _mirror_held(conn, body.name, body)
        _hosted_held(conn, body.name, body)
        row = conn.execute(
            "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref,"
            " marketplace_entry, fetch_url, fetch_path, pin_kind, commit_sha, bytes_sha256,"
            " version, content_hash, files, review)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
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
                body.version,
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
        return _row(conn, row)

    return _announced(state, await _in_tx(state, run))


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

    pin_id = body.pin_id

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
        _mirror_held(conn, name, body)
        _hosted_held(conn, name, body)
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
            " pending_bytes_sha256 = %s, pending_version = %s, pending_content_hash = %s,"
            " pending_files = %s, pending_review = %s, pending_mirror_sha256 = NULL,"
            " updated_at = now()"
            " WHERE name = %s RETURNING *",
            (
                body.ref,
                body.fetch_url,
                body.fetch_path,
                body.pin_kind,
                body.commit_sha,
                body.bytes_sha256,
                body.version,
                body.content_hash,
                Jsonb(body.files),
                Jsonb(body.review),
                name,
            ),
        ).fetchone()
        assert row is not None
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(conn, row)

    return _announced(state, await _in_tx(state, run))


@router.post("/plugin-packages/{name}/discard-pending", name="crud_agent_plugin_package_discard")
async def plugin_package_discard(name: Name, state: StateDep) -> PackageRow:
    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        row = conn.execute(
            "UPDATE ai_plugin_packages SET pending_ref = NULL, pending_fetch_url = NULL,"
            " pending_fetch_path = NULL, pending_pin_kind = NULL, pending_commit_sha = NULL,"
            " pending_bytes_sha256 = NULL, pending_version = NULL, pending_content_hash = NULL,"
            " pending_files = NULL, pending_review = NULL, pending_mirror_sha256 = NULL,"
            " updated_at = now()"
            " WHERE name = %s RETURNING *",
            (name,),
        ).fetchone()
        if row is None:
            raise _not_found(name)
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(conn, row)

    return _announced(state, await _in_tx(state, run))


class Approve(_Body):
    """The pin the review showed: its commit, for a bytes pin its bytes' sha256, or for a
    hosted package its version."""

    commit_sha: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    bytes_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    version: int | None = Field(default=None, ge=1)
    content_hash: str = Field(pattern=HASH_PATTERN)
    allow_refused: bool = False

    @model_validator(mode="after")
    def _one(self) -> Self:
        if len([f for f in PIN_FIELD.values() if getattr(self, f) is not None]) != 1:
            raise ValueError("name the pin by one of commit_sha, bytes_sha256 or version")
        return self

    @property
    def kind(self) -> str:
        return next(k for k, f in PIN_FIELD.items() if getattr(self, f) is not None)

    @property
    def pin_id(self) -> str:
        return str(getattr(self, PIN_FIELD[self.kind]))


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
                " fetch_path = pending_fetch_path, pin_kind = %s,"
                " commit_sha = pending_commit_sha, bytes_sha256 = pending_bytes_sha256,"
                " version = pending_version,"
                " content_hash = pending_content_hash, files = pending_files,"
                " review = pending_review, mirror_sha256 = pending_mirror_sha256,"
                " approved_at = now(), allow_refused = %s,"
                " pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL,"
                " pending_pin_kind = NULL, pending_commit_sha = NULL,"
                " pending_bytes_sha256 = NULL, pending_version = NULL,"
                " pending_content_hash = NULL, pending_mirror_sha256 = NULL,"
                " pending_files = NULL, pending_review = NULL, updated_at = now()"
                " WHERE name = %s RETURNING *",
                (moved, current["pending_pin_kind"], allow, name),
            ).fetchone()
            assert row is not None
            prune_stale_rows(conn, name, row["review"])
            _written(conn)
            return _row(conn, row)
        if current["approved_at"] is not None and current["allow_refused"] == allow:
            if prune_stale_rows(conn, name, current["review"]):
                notify_warm_evict(conn)
            return _row(conn, current)
        row = conn.execute(
            "UPDATE ai_plugin_packages SET approved_at = now(), allow_refused = %s,"
            " updated_at = now() WHERE name = %s RETURNING *",
            (allow, name),
        ).fetchone()
        assert row is not None
        prune_stale_rows(conn, name, row["review"])
        _written(conn)
        return _row(conn, row)

    return _announced(state, await _in_tx(state, run))


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
        return _row(conn, row)

    return _announced(state, await _in_tx(state, run))


@router.post("/plugin-packages/{name}/delete", name="crud_agent_plugin_package_delete")
async def plugin_package_delete(name: Name, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        gone = conn.execute("DELETE FROM ai_plugin_packages WHERE name = %s", (name,)).rowcount
        if gone == 0:
            return False
        forget_package(conn, name)
        _written(conn)
        return True

    done = await _in_tx(state, run)
    if done:
        emit(state.events, PluginPackagesChanged(name=name))
    return Done(done=done)


class MirrorRecord(_Body):
    """A commit pin's mirror (#7907): the pin it is of, and the sha256 of the zip of its
    files that the agent sent to ``POST …/package-mirrors/{name}``."""

    commit_sha: str = Field(pattern=COMMIT_PATTERN)
    content_hash: str = Field(pattern=HASH_PATTERN)
    bytes_sha256: str = Field(pattern=SHA256_PATTERN)


@router.post("/plugin-packages/{name}/mirror", name="crud_agent_plugin_package_mirror")
async def plugin_package_mirror(name: Name, body: MirrorRecord, state: StateDep) -> PackageRow:
    """Records ``body.bytes_sha256`` as the mirror of the commit pin it names: the pin's
    (``mirror_sha256``) and the re-pin's (``pending_mirror_sha256``) when each is that commit
    and content hash. Only a mirror the package holds is recorded. No pin, approval or switch
    changes, so nothing is announced and no warm Claude Code is evicted."""

    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        current = _locked(conn, name)
        if not package_mirrors.held(conn, name, body.bytes_sha256):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"no mirror of the bytes {body.bytes_sha256} is kept; send them first",
                reason=MIRROR_MISSING,
            )
        is_current = (
            current["pin_kind"] == "commit"
            and current["commit_sha"] == body.commit_sha
            and current["content_hash"] == body.content_hash
        )
        is_pending = (
            current["pending_pin_kind"] == "commit"
            and current["pending_commit_sha"] == body.commit_sha
            and current["pending_content_hash"] == body.content_hash
        )
        if not is_current and not is_pending:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'"{name}" is not pinned to {body.commit_sha} with that content hash,'
                " and its re-pin is not either",
                reason=PIN_MISMATCH,
            )
        row = conn.execute(
            "UPDATE ai_plugin_packages SET"
            " mirror_sha256 = CASE WHEN %s THEN %s ELSE mirror_sha256 END,"
            " pending_mirror_sha256 = CASE WHEN %s THEN %s ELSE pending_mirror_sha256 END"
            " WHERE name = %s RETURNING *",
            (is_current, body.bytes_sha256, is_pending, body.bytes_sha256, name),
        ).fetchone()
        assert row is not None
        package_mirrors.release_unnamed(conn)
        return _row(conn, row)

    return await _in_tx(state, run)


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


# Hosted plugins (#6932, spec 2026-11-21 §6.5, area ``hosted-plugins``): the agent's reads of
# what the API's ``/api/v1/hosted-plugins`` routes publish. The agent installs and re-pins a
# hosted package from its latest version, and fetches a version's zip to materialise it.


class HostedVersionRow(BaseModel):
    """One ``ai_hosted_plugin_versions`` row: the pin is (``version``, ``content_hash``)."""

    version: int
    zip_sha256: str
    content_hash: str
    files: dict[str, Any]
    published_at: datetime
    published_by: str


class HostedPluginRow(BaseModel):
    name: str
    description: str
    category: str | None
    tags: list[str]
    latest_version: int | None
    #: The latest version's row; null before the first publish.
    latest: HostedVersionRow | None


class HostedPluginAnswer(BaseModel):
    plugin: HostedPluginRow | None


@router.get("/hosted-plugins/{name}", name="crud_agent_hosted_plugin")
async def hosted_plugin(name: Name, state: StateDep) -> HostedPluginAnswer:
    def run(conn: psycopg.Connection[Any]) -> HostedPluginRow | None:
        row = hosted_store.plugin(conn, name)
        if row is None:
            return None
        latest = hosted_store.latest(conn, row)
        return HostedPluginRow(
            name=row["name"],
            description=row["description"],
            category=row["category"],
            tags=list(row["tags"]),
            latest_version=row["latest_version"],
            latest=None if latest is None else HostedVersionRow.model_validate(dict(latest)),
        )

    return HostedPluginAnswer(plugin=await _in_tx(state, run))


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


# Marketplace feeds (#6933, spec 2026-11-21 §6.4, area ``plugin-feeds``): the packages a feed
# lists, for the agent, which serves ``marketplace.json`` and the version zips from them. The
# agent decides what an outside client is given: only an approved pin, and a hosted one at the
# version that pin names (#6934: the pin holds its ``version``), and only while that version's
# content hash is still the pin's.

#: ``ai_plugin_feeds.slug``'s CHECK.
FEED_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class PluginFeed(BaseModel):
    slug: str
    #: The marketplace's ``name``.
    name: str
    #: The marketplace's ``owner.name``.
    owner: str


class FeedHostedVersion(BaseModel):
    """The hosted plugin's version the package's current pin names."""

    version: int
    zip_sha256: str


class FeedPackage(BaseModel):
    """A package the feed names, at its current pin (never its pending one)."""

    name: str
    source_kind: str
    fetch_url: str | None
    fetch_path: str | None
    commit_sha: str | None
    content_hash: str
    #: ``commit``, ``bytes`` or ``version``: what the feed lists the package by (#6936).
    pin_kind: str
    #: A bytes pin's file, which the agent serves from its mirror (#6936).
    bytes_sha256: str | None
    #: Whether an admin approved the current pin; only an approved one is listed.
    approved: bool
    #: Approved with its refused items allowed (hooks, MCP servers): every client runs them.
    allow_refused: bool
    #: The approved review's manifest description.
    description: str | None
    #: For a ``hosted`` package, the version its pin is; null otherwise, or when none is.
    hosted: FeedHostedVersion | None


class PluginFeedAnswer(BaseModel):
    #: Null when no feed has the slug.
    feed: PluginFeed | None
    packages: list[FeedPackage]


_FEED_PACKAGES = """
SELECT p.name, p.source_kind, p.fetch_url, p.fetch_path, p.commit_sha, p.content_hash,
       p.pin_kind, p.bytes_sha256, p.allow_refused,
       p.approved_at IS NOT NULL AS approved, p.review->>'description' AS description,
       v.version AS hosted_version, v.zip_sha256 AS hosted_zip_sha256
  FROM ai_plugin_feed_packages fp
  JOIN ai_plugin_packages p ON p.name = fp.package_name
  LEFT JOIN ai_hosted_plugins h ON p.source_kind = 'hosted' AND h.name = p.name
  LEFT JOIN ai_hosted_plugin_versions v
    ON v.plugin_id = h.id AND v.version = p.version AND v.content_hash = p.content_hash
 WHERE fp.feed_id = %s AND (%s::text IS NULL OR p.name = %s)
 ORDER BY p.name
"""


def _feed_package(p: Any) -> FeedPackage:
    hosted = (
        None
        if p["hosted_version"] is None
        else FeedHostedVersion(version=p["hosted_version"], zip_sha256=p["hosted_zip_sha256"])
    )
    return FeedPackage(
        name=p["name"],
        source_kind=p["source_kind"],
        fetch_url=p["fetch_url"],
        fetch_path=p["fetch_path"],
        commit_sha=p["commit_sha"],
        content_hash=p["content_hash"],
        pin_kind=p["pin_kind"],
        bytes_sha256=p["bytes_sha256"],
        approved=p["approved"],
        allow_refused=p["allow_refused"],
        description=p["description"],
        hosted=hosted,
    )


@router.get("/plugin-feeds/{feed}", name="crud_agent_plugin_feed")
async def plugin_feed(
    feed: Annotated[str, Path(pattern=FEED_PATTERN)],
    state: StateDep,
    package: Annotated[str | None, Query(max_length=64)] = None,
) -> PluginFeedAnswer:
    """The feed and its packages; with ``package``, only that one (a zip request's read)."""

    def run(conn: psycopg.Connection[Any]) -> PluginFeedAnswer:
        row = conn.execute(
            "SELECT id, slug, name, owner FROM ai_plugin_feeds WHERE slug = %s", (feed,)
        ).fetchone()
        if row is None:
            return PluginFeedAnswer(feed=None, packages=[])
        rows = conn.execute(_FEED_PACKAGES, (row["id"], package, package)).fetchall()
        return PluginFeedAnswer(
            feed=PluginFeed(slug=row["slug"], name=row["name"], owner=row["owner"]),
            packages=[_feed_package(p) for p in rows],
        )

    return await _in_tx(state, run)


def _feed_id(conn: psycopg.Connection[Any], feed: str) -> int:
    row = conn.execute("SELECT id FROM ai_plugin_feeds WHERE slug = %s", (feed,)).fetchone()
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f'no plugin feed "{feed}"', reason=FEED_NOT_FOUND)
    return int(row["id"])


@router.put("/plugin-feeds/{feed}/packages/{name}", name="crud_agent_plugin_feed_add")
async def plugin_feed_add(
    feed: Annotated[str, Path(pattern=FEED_PATTERN)], name: Name, state: StateDep
) -> Done:
    """Lists the package ``name`` in the feed (#6936, spec §6.7); ``done`` is false when it
    already was. The feed lists it only at an approved pin, which the agent checks."""

    def run(conn: psycopg.Connection[Any]) -> bool:
        feed_id = _feed_id(conn, feed)
        if (
            conn.execute(
                "SELECT 1 FROM ai_plugin_packages WHERE name = %s FOR KEY SHARE", (name,)
            ).fetchone()
            is None
        ):
            raise _not_found(name)
        added = conn.execute(
            "INSERT INTO ai_plugin_feed_packages (feed_id, package_name) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (feed_id, name),
        ).rowcount
        return added == 1

    return Done(done=await _in_tx(state, run))


@router.delete("/plugin-feeds/{feed}/packages/{name}", name="crud_agent_plugin_feed_remove")
async def plugin_feed_remove(
    feed: Annotated[str, Path(pattern=FEED_PATTERN)], name: Name, state: StateDep
) -> Done:
    """Takes the package ``name`` out of the feed (#6936); ``done`` is false when it was not
    in it."""

    def run(conn: psycopg.Connection[Any]) -> bool:
        feed_id = _feed_id(conn, feed)
        removed = conn.execute(
            "DELETE FROM ai_plugin_feed_packages WHERE feed_id = %s AND package_name = %s",
            (feed_id, name),
        ).rowcount
        return removed == 1

    return Done(done=await _in_tx(state, run))
