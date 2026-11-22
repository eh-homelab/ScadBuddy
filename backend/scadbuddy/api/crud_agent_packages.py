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

Area ``hosted-plugins`` (#6932): ``GET …/hosted-plugins/{name}`` a hosted plugin with its
latest version's row (``plugin`` null when there is none), and
``GET …/hosted-plugins/{name}/versions/{N}.zip`` that version's zip, as published (404
``hosted-version-not-found``).

Area ``plugin-feeds`` (#6933): ``GET …/plugin-feeds/{feed}`` the feed's name and owner and
every package it lists, each at its current pin with whether that pin is approved, and for a
hosted package the version that pin is (``feed`` null for a slug no feed has).

A package that is not there is 404 ``package-not-found``. Each refusal's text is the
agent's ``PluginError``'s, which the agent raises again with the route's status.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Path, Query, Response, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.agent_data.access import notify_warm_evict
from scadbuddy.agent_data.packages import forget_package, prune_stale_rows
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.hosted_plugins import store as hosted_store
from scadbuddy.hosted_plugins.blobs import blob_bytes

router = crud_router("agent")

#: The table's CHECKs (``ai_plugin_packages``).
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
COMMIT_PATTERN = r"^([0-9a-f]{40}|[0-9a-f]{64})$"
HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
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
HOSTED_VERSION_NOT_FOUND = "hosted-version-not-found"

Name = Annotated[str, Path(pattern=NAME_PATTERN)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bounded(value: dict[str, Any], limit: int, what: str) -> dict[str, Any]:
    if len(json.dumps(value)) > limit:
        raise ValueError(f"{what} is larger than {limit} characters of JSON")
    return value


class PackageRow(BaseModel):
    """One ``ai_plugin_packages`` row, every column."""

    name: str
    source_kind: Literal["git", "marketplace"]
    source_url: str
    source_ref: str
    marketplace_entry: str | None
    fetch_url: str
    fetch_path: str
    commit_sha: str
    content_hash: str
    files: dict[str, Any]
    review: dict[str, Any]
    approved_at: datetime | None
    allow_refused: bool
    enabled: bool
    pending_ref: str | None
    pending_fetch_url: str | None
    pending_fetch_path: str | None
    pending_commit_sha: str | None
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
    """A fetched, vetted and hashed commit, as the agent prepared it."""

    fetch_url: str = Field(min_length=1, max_length=URL_MAX)
    fetch_path: str = Field(max_length=TEXT_MAX)
    commit_sha: str = Field(pattern=COMMIT_PATTERN)
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


class Install(Pin):
    name: str = Field(pattern=NAME_PATTERN)
    source_kind: Literal["git", "marketplace"]
    source_url: str = Field(min_length=1, max_length=URL_MAX)
    source_ref: str = Field(min_length=1, max_length=TEXT_MAX)
    marketplace_entry: str | None = Field(default=None, max_length=TEXT_MAX)


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
        row = conn.execute(
            "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref,"
            " marketplace_entry, fetch_url, fetch_path, commit_sha, content_hash, files, review)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (name) DO NOTHING RETURNING *",
            (
                body.name,
                body.source_kind,
                body.source_url,
                body.source_ref,
                body.marketplace_entry if body.source_kind == "marketplace" else None,
                body.fetch_url,
                body.fetch_path,
                body.commit_sha,
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
        notify_warm_evict(conn)
        return _row(row)

    return await _in_tx(state, run)


class Pending(Pin):
    ref: str = Field(min_length=1, max_length=TEXT_MAX)


@router.post("/plugin-packages/{name}/pending", name="crud_agent_plugin_package_pending")
async def plugin_package_pending(name: Name, body: Pending, state: StateDep) -> PackageRow:
    review_name = body.review.get("name")
    if review_name != name:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f'the new commit names the plugin "{review_name}", not "{name}"',
            reason=PACKAGE_NAME_DIFFERS,
        )

    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        current = _locked(conn, name)
        if (
            current["commit_sha"] == body.commit_sha
            and current["content_hash"] == body.content_hash
            and current["fetch_url"] == body.fetch_url
            and current["fetch_path"] == body.fetch_path
        ):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f'"{name}" is already pinned to {body.commit_sha}',
                reason=PACKAGE_PINNED,
            )
        row = conn.execute(
            "UPDATE ai_plugin_packages SET pending_ref = %s, pending_fetch_url = %s,"
            " pending_fetch_path = %s, pending_commit_sha = %s, pending_content_hash = %s,"
            " pending_files = %s, pending_review = %s, updated_at = now()"
            " WHERE name = %s RETURNING *",
            (
                body.ref,
                body.fetch_url,
                body.fetch_path,
                body.commit_sha,
                body.content_hash,
                Jsonb(body.files),
                Jsonb(body.review),
                name,
            ),
        ).fetchone()
        assert row is not None
        prune_stale_rows(conn, name, row["review"])
        notify_warm_evict(conn)
        return _row(row)

    return await _in_tx(state, run)


@router.post("/plugin-packages/{name}/discard-pending", name="crud_agent_plugin_package_discard")
async def plugin_package_discard(name: Name, state: StateDep) -> PackageRow:
    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        row = conn.execute(
            "UPDATE ai_plugin_packages SET pending_ref = NULL, pending_fetch_url = NULL,"
            " pending_fetch_path = NULL, pending_commit_sha = NULL, pending_content_hash = NULL,"
            " pending_files = NULL, pending_review = NULL, updated_at = now()"
            " WHERE name = %s RETURNING *",
            (name,),
        ).fetchone()
        if row is None:
            raise _not_found(name)
        prune_stale_rows(conn, name, row["review"])
        notify_warm_evict(conn)
        return _row(row)

    return await _in_tx(state, run)


class Approve(_Body):
    commit_sha: str = Field(pattern=COMMIT_PATTERN)
    content_hash: str = Field(pattern=HASH_PATTERN)
    allow_refused: bool = False


@router.post("/plugin-packages/{name}/approve", name="crud_agent_plugin_package_approve")
async def plugin_package_approve(name: Name, body: Approve, state: StateDep) -> PackageRow:
    def run(conn: psycopg.Connection[Any]) -> PackageRow:
        current = _locked(conn, name)
        is_pending = (
            current["pending_commit_sha"] == body.commit_sha
            and current["pending_content_hash"] == body.content_hash
        )
        is_current = (
            current["commit_sha"] == body.commit_sha
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
                " fetch_path = pending_fetch_path, commit_sha = pending_commit_sha,"
                " content_hash = pending_content_hash, files = pending_files,"
                " review = pending_review, approved_at = now(), allow_refused = %s,"
                " pending_ref = NULL, pending_fetch_url = NULL, pending_fetch_path = NULL,"
                " pending_commit_sha = NULL, pending_content_hash = NULL,"
                " pending_files = NULL, pending_review = NULL, updated_at = now()"
                " WHERE name = %s RETURNING *",
                (moved, allow, name),
            ).fetchone()
            assert row is not None
            prune_stale_rows(conn, name, row["review"])
            notify_warm_evict(conn)
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
        notify_warm_evict(conn)
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
        notify_warm_evict(conn)
        return _row(row)

    return await _in_tx(state, run)


@router.post("/plugin-packages/{name}/delete", name="crud_agent_plugin_package_delete")
async def plugin_package_delete(name: Name, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        gone = conn.execute("DELETE FROM ai_plugin_packages WHERE name = %s", (name,)).rowcount
        if gone == 0:
            return False
        forget_package(conn, name)
        notify_warm_evict(conn)
        return True

    return Done(done=await _in_tx(state, run))


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
# agent decides what an outside client is given: only an approved pin, and a hosted one by the
# version whose content hash is that pin's.

#: ``ai_plugin_feeds.slug``'s CHECK.
FEED_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class PluginFeed(BaseModel):
    slug: str
    #: The marketplace's ``name``.
    name: str
    #: The marketplace's ``owner.name``.
    owner: str


class FeedHostedVersion(BaseModel):
    """The hosted plugin's version whose content hash is the package's current pin."""

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
    #: Whether an admin approved the current pin; only an approved one is listed.
    approved: bool
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
       p.approved_at IS NOT NULL AS approved, p.review->>'description' AS description,
       v.version AS hosted_version, v.zip_sha256 AS hosted_zip_sha256
  FROM ai_plugin_feed_packages fp
  JOIN ai_plugin_packages p ON p.name = fp.package_name
  LEFT JOIN ai_hosted_plugins h ON p.source_kind = 'hosted' AND h.name = p.name
  LEFT JOIN ai_hosted_plugin_versions v ON v.plugin_id = h.id AND v.content_hash = p.content_hash
 WHERE fp.feed_id = %s
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
        approved=p["approved"],
        description=p["description"],
        hosted=hosted,
    )


@router.get("/plugin-feeds/{feed}", name="crud_agent_plugin_feed")
async def plugin_feed(
    feed: Annotated[str, Path(pattern=FEED_PATTERN)], state: StateDep
) -> PluginFeedAnswer:
    def run(conn: psycopg.Connection[Any]) -> PluginFeedAnswer:
        row = conn.execute(
            "SELECT id, slug, name, owner FROM ai_plugin_feeds WHERE slug = %s", (feed,)
        ).fetchone()
        if row is None:
            return PluginFeedAnswer(feed=None, packages=[])
        rows = conn.execute(_FEED_PACKAGES, (row["id"],)).fetchall()
        return PluginFeedAnswer(
            feed=PluginFeed(slug=row["slug"], name=row["name"], owner=row["owner"]),
            packages=[_feed_package(p) for p in rows],
        )

    return await _in_tx(state, run)
