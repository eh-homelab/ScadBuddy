"""The storefront's listings (#5282, spec 2026-11-02 §5.3): what the public site offers,
kept by the back office's Storefront page.

``ListingStore`` keeps the rows. An edit names the ``updated_at`` the editor read, so a
second editor's stale save is refused (409 ``listing_changed``), and an edit of a
published listing unpublishes it: a change is not live until the shop publishes again.

``publish`` checks the listing against its commit's schema and captures what a visitor's
render needs without git (plan point 2): the schema, the template's name and library
pins, and its source snapshot. A pipeline template is refused for now (422
``listing_pipeline_unsupported``): its inputs need not be ``model.scad``'s parameters,
which are all a listing can bound. It copies each chosen media file into the content
store. The snapshot and the media are held in ``blob_refs`` under (:data:`HOLDER`,
listing id) while the listing is published; unpublishing, an edit and a delete drop that
hold in the same transaction as the row, and a failed publish drops every hold the row
does not name. Publishes of one listing never overlap (``begin_publish``), so a failing
one cannot drop the holds a running one took.

``public_listing`` is a published listing as the public site sees it (Task 6): its bounds
with each parameter's label and group, its filaments, and its media as presigned GETs
of `PUBLIC_MEDIA_TTL`. Only an S3 store presigns (`public_content`); on any other the
public routes answer 503, since the fault is the operator's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import psycopg
from fastapi import status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.gitrepo import GitError, RevisionNotFoundError
from scadbuddy.core.pg_pool import record_connection
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.wire import NOT_CONFIGURED
from scadbuddy.library.catalogue import MediaNotFoundError, ModelNotFoundError, declares_pipeline
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.orders.models import Filament
from scadbuddy.orders.reference import ReferenceError, TemplateDeletedError, resolve_reference
from scadbuddy.orders.storefront.wire import (
    ChoiceBound,
    Listing,
    ListingInput,
    ListingMedia,
    ListingSnapshot,
    NumberBound,
    ParamBound,
    PublicChoice,
    PublicFilament,
    PublicListing,
    PublicListingCard,
    PublicMedia,
    PublicNumber,
    PublicParam,
    PublicText,
    TextBound,
    shown_label,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.runner import OpenSCADError, cached_schema, format_scad_value
from scadbuddy.render.schema import CustomizerSchema, Parameter, ParamValue
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.refs import BlobRefs
from scadbuddy.store.s3 import S3ContentBackend
from scadbuddy.storefront.wire import LISTING_ID, STOREFRONT_PROBLEMS

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from scadbuddy.core.components import Core
    from scadbuddy.store.content import ContentStore
    from scadbuddy.store.snapshots import SnapshotStore

#: The ``blob_refs`` holder kind of a published listing's snapshot and media.
HOLDER: Final = "storefront_listing"
#: Seconds to wait for the publish lock's own connection.
PUBLISH_LOCK_CONNECT_TIMEOUT: Final = 5
#: The most publishes one process runs at once, each holding one connection of its own
#: for its lock; one more is refused (503 ``listing_publish_busy``).
PUBLISH_LOCKS_MAX: Final = 2
#: The advisory lock a create takes, so two creates never take the same position.
POSITION_LOCK: Final = f"{HOLDER}:position"
#: The publish locks' name in `core/pg_pool.py`'s registry and in ``pg_stat_activity``.
PUBLISH_LOCKS_NAME: Final = "scadbuddy-storefront-publish"
#: How long a listing media URL the public site gets works.
PUBLIC_MEDIA_TTL: Final = timedelta(hours=1)
#: The public routes log the operator's text of ``storefront_needs_s3`` at most this
#: often (seconds).
NEEDS_S3_WARN_EVERY: Final = 60.0
#: When the public routes last logged it (`time.monotonic`), or None.
_needs_s3_warned: float | None = None

_COLUMNS = (
    "id, slug, commit, title, description, position, published, params, fixed, colours,"
    " media_ids, snapshot, media, published_at, created_at, updated_at"
)
_NUMBERS = frozenset({"number", "integer", "slider"})

type Pool = ConnectionPool[Connection[DictRow]]


def problem(status_code: int, slug: str, detail: str, **extensions: Any) -> ApiError:
    """A refusal under ``https://scadbuddy.dev/problems/storefront/<slug>``."""
    return ApiError(status_code, detail, type_=STOREFRONT_PROBLEMS + slug, **extensions)


def _not_found(listing_id: str) -> ApiError:
    return problem(status.HTTP_404_NOT_FOUND, "listing_not_found", f"no listing {listing_id!r}")


def needs_s3() -> ApiError:
    """409 ``storefront_needs_s3``."""
    return problem(
        status.HTTP_409_CONFLICT,
        "storefront_needs_s3",
        "the storefront needs store_backend s3: set it in Settings first",
    )


def _changed(listing_id: str) -> ApiError:
    return problem(
        status.HTTP_409_CONFLICT,
        "listing_changed",
        f"listing {listing_id!r} changed since it was read; load it again",
    )


def _listing(row: DictRow) -> Listing:
    return Listing.model_validate(row)


def _drop_holds(conn: Connection[DictRow], listing_id: str, keep: Collection[str] = ()) -> None:
    conn.execute(
        "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s AND NOT (key = ANY(%s))",
        (HOLDER, listing_id, list(keep)),
    )


def _check_choices(body: ListingInput) -> None:
    """422 ``listing_choice_empty`` for a parameter offered with nothing to choose: a
    colour parameter whose listing has no filament checked, most often."""
    for name, bound in body.params.items():
        if isinstance(bound, ChoiceBound) and not bound.choices:
            raise problem(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "listing_choice_empty",
                f"{name!r} is offered with nothing to choose; for a colour parameter,"
                " choose at least one filament, or lock it",
                name=name,
            )


class _PublishLocks:
    """The publish locks' own connections: at most :data:`PUBLISH_LOCKS_MAX`, recorded in
    `core/pg_pool.py`'s registry as a pool of that size."""

    name = PUBLISH_LOCKS_NAME

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self._open = 0
        self._lock = threading.Lock()
        record_connection(self)

    @property
    def max_size(self) -> int:
        return PUBLISH_LOCKS_MAX

    def open_connections(self) -> int:
        return self._open

    def take(self) -> bool:
        with self._lock:
            if self._open >= PUBLISH_LOCKS_MAX:
                return False
            self._open += 1
            return True

    def give_back(self) -> None:
        with self._lock:
            self._open -= 1


class ListingStore:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        conninfo = pool.conninfo
        self._publish_locks = _PublishLocks(conninfo if isinstance(conninfo, str) else "")

    def create(self, body: ListingInput) -> Listing:
        """A new listing, unpublished, after every other one; 422
        ``listing_choice_empty`` for a choice with nothing to choose."""
        _check_choices(body)
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (POSITION_LOCK,))
            row = conn.execute(
                "INSERT INTO storefront_listings"
                " (slug, commit, title, description, position, params, fixed, colours, media_ids)"
                " VALUES (%s, %s, %s, %s,"
                " (SELECT coalesce(max(position) + 1, 0) FROM storefront_listings),"
                f" %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (*self._fields(body),),
            ).fetchone()
        assert row is not None
        return _listing(row)

    @staticmethod
    def _fields(body: ListingInput) -> tuple[object, ...]:
        dumped = body.model_dump(mode="json")
        return (
            body.slug,
            body.commit,
            body.title,
            body.description,
            Jsonb(dumped["params"]),
            Jsonb(dumped["fixed"]),
            body.colours,
            body.media_ids,
        )

    def update(
        self, listing_id: str, body: ListingInput, *, expected_updated_at: datetime
    ) -> Listing:
        """The listing replaced by ``body``, and unpublished; 409 ``listing_changed``
        when it changed after ``expected_updated_at`` was read, 404 when it is gone, 422
        ``listing_choice_empty`` for a choice with nothing to choose."""
        _check_choices(body)
        slug, commit, title, description, params, fixed, colours, media_ids = self._fields(body)
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE storefront_listings SET slug = %s, commit = %s, title = %s,"
                " description = %s, params = %s, fixed = %s, colours = %s, media_ids = %s,"
                " published = false, snapshot = NULL, media = '[]', published_at = NULL,"
                " updated_at = clock_timestamp()"
                f" WHERE id = %s AND updated_at = %s RETURNING {_COLUMNS}",
                (
                    slug,
                    commit,
                    title,
                    description,
                    params,
                    fixed,
                    colours,
                    media_ids,
                    listing_id,
                    expected_updated_at,
                ),
            ).fetchone()
            if row is None:
                raise self._refusal(conn, listing_id)
            _drop_holds(conn, listing_id)
        return _listing(row)

    @staticmethod
    def _refusal(conn: Connection[DictRow], listing_id: str) -> ApiError:
        found = conn.execute(
            "SELECT 1 FROM storefront_listings WHERE id = %s", (listing_id,)
        ).fetchone()
        return _not_found(listing_id) if found is None else _changed(listing_id)

    def get(self, listing_id: str) -> Listing | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_listings WHERE id = %s", (listing_id,)
            ).fetchone()
        return None if row is None else _listing(row)

    def published(
        self, *, limit: int, after: tuple[int, str] | None = None, query: str | None = None
    ) -> list[Listing]:
        """At most ``limit`` published listings, in the shop's order (position, then id),
        after the listing at ``after`` (its position and id), whose title has ``query`` in
        it, any case (#6154). Keyset paging on ``storefront_listings_published_order``, so
        a later page costs what the first does."""
        where = ["published"]
        args: list[Any] = []
        if after is not None:
            where.append("(position, id) > (%s, %s)")
            args.extend(after)
        if query:
            where.append("title ILIKE %s ESCAPE '\\'")
            args.append("%" + _like_escaped(query) + "%")
        args.append(limit)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_listings WHERE {' AND '.join(where)}"
                " ORDER BY position, id LIMIT %s",
                args,
            ).fetchall()
        return [_listing(row) for row in rows]

    def at_commits(self, pairs: Collection[tuple[str, str]]) -> list[Listing]:
        """The listings at any of ``pairs`` (slug, commit), in one query; none for none."""
        if not pairs:
            return []
        slugs, commits = zip(*sorted(pairs), strict=True)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_listings"
                " WHERE (slug, commit) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
                " ORDER BY position, id",
                (list(slugs), list(commits)),
            ).fetchall()
        return [_listing(row) for row in rows]

    def all(self) -> list[Listing]:
        """Every listing, in the shop's order."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_listings ORDER BY position, id"
            ).fetchall()
        return [_listing(row) for row in rows]

    def reorder(self, ids: Sequence[str]) -> list[Listing]:
        """Every listing, in the order of ``ids``. ``ids`` must name each listing once:
        an order made from a list that has since gained or lost one is 409
        ``listing_changed``."""
        with self.pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "SELECT id FROM storefront_listings ORDER BY id FOR UPDATE"
            ).fetchall()
            if len(set(ids)) != len(ids) or {row["id"] for row in rows} != set(ids):
                raise problem(
                    status.HTTP_409_CONFLICT,
                    "listing_changed",
                    "the order must name every listing once; load the listings again",
                )
            conn.execute(
                "UPDATE storefront_listings SET position = given.position - 1"
                " FROM unnest(%s::text[]) WITH ORDINALITY AS given(id, position)"
                " WHERE storefront_listings.id = given.id",
                (list(ids),),
            )
        return self.all()

    def delete(self, listing_id: str) -> None:
        """Remove an unpublished listing; 409 ``listing_published`` while it is
        published, 404 when it is gone."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT published FROM storefront_listings WHERE id = %s FOR UPDATE",
                (listing_id,),
            ).fetchone()
            if row is None:
                raise _not_found(listing_id)
            if row["published"]:
                raise problem(
                    status.HTTP_409_CONFLICT,
                    "listing_published",
                    f"listing {listing_id!r} is published; unpublish it first",
                )
            conn.execute("DELETE FROM storefront_listings WHERE id = %s", (listing_id,))
            _drop_holds(conn, listing_id)

    def mark_published(
        self,
        listing_id: str,
        *,
        snapshot: ListingSnapshot,
        media: Sequence[ListingMedia],
        expected_updated_at: datetime,
    ) -> Listing:
        """The listing published with what ``publish`` captured (its effective values in
        ``snapshot.fixed``; the row's ``fixed`` is left as the shop set it). Holds of the listing
        on any blob other than the snapshot and ``media`` are dropped (a republish's
        old ones). 409 ``listing_changed`` when it changed after it was read."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE storefront_listings SET snapshot = %s, media = %s,"
                " published = true, published_at = clock_timestamp(),"
                " updated_at = clock_timestamp()"
                f" WHERE id = %s AND updated_at = %s RETURNING {_COLUMNS}",
                (
                    Jsonb(snapshot.model_dump(mode="json")),
                    Jsonb([item.model_dump(mode="json") for item in media]),
                    listing_id,
                    expected_updated_at,
                ),
            ).fetchone()
            if row is None:
                raise self._refusal(conn, listing_id)
            _drop_holds(conn, listing_id, [snapshot.snapshot_key, *(m.blob_key for m in media)])
        return _listing(row)

    def mark_unpublished(self, listing_id: str) -> Listing:
        """The listing taken off the public site, and its holds dropped; 404 when it is
        gone."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE storefront_listings SET published = false, snapshot = NULL,"
                " media = '[]', published_at = NULL, updated_at = clock_timestamp()"
                f" WHERE id = %s RETURNING {_COLUMNS}",
                (listing_id,),
            ).fetchone()
            if row is None:
                raise _not_found(listing_id)
            _drop_holds(conn, listing_id)
        return _listing(row)

    def release_unheld(self, listing_id: str) -> None:
        """Drop every hold of the listing on a blob its row does not name, under the row
        lock: after a failed publish, whose copies are held but never recorded. A
        published listing keeps its snapshot and media; an unpublished one keeps
        nothing (an edit's own drop may have run before the publish took its holds)."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT published, snapshot, media FROM storefront_listings"
                " WHERE id = %s FOR UPDATE",
                (listing_id,),
            ).fetchone()
            keep: list[str] = []
            if row is not None and row["published"]:
                keep = [row["snapshot"]["snapshot_key"], *(m["blob_key"] for m in row["media"])]
            _drop_holds(conn, listing_id, keep)

    def begin_publish(self, listing_id: str) -> Callable[[], None]:
        """Take the listing's publish lock, or 409 ``listing_publishing`` when another
        publish of it runs: publishes of one listing never overlap, so a failing one
        never drops the holds a running one took. The lock is a session advisory lock on
        a connection of its own, never the rows pool's: a publish's git, schema build and
        uploads take seconds, and publishes running at once would otherwise keep the
        pooled connections every other route needs. At most :data:`PUBLISH_LOCKS_MAX`
        such connections are open at once; one more publish is 503
        ``listing_publish_busy``. The returned function unlocks and closes it; closing
        the connection alone also ends the lock."""
        locks = self._publish_locks
        if not locks.take():
            raise problem(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "listing_publish_busy",
                f"{PUBLISH_LOCKS_MAX} listings are being published already;"
                " try again when one is done",
            )
        try:
            conninfo = self.pool.conninfo
            conn = psycopg.connect(
                conninfo() if callable(conninfo) else conninfo,
                autocommit=True,
                connect_timeout=PUBLISH_LOCK_CONNECT_TIMEOUT,
                application_name=PUBLISH_LOCKS_NAME,
            )
        except BaseException:
            locks.give_back()
            raise
        key = (f"{HOLDER}:{listing_id}",)
        try:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", key
            ).fetchone()
            if row is None or not row[0]:
                raise problem(
                    status.HTTP_409_CONFLICT,
                    "listing_publishing",
                    f"listing {listing_id!r} is being published; try again when it is done",
                )
        except BaseException:
            conn.close()
            locks.give_back()
            raise

        def end() -> None:
            try:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", key)
            finally:
                conn.close()
                locks.give_back()

        return end

    def filament_colours(self) -> dict[str, str]:
        """Each price-book filament not archived, by id: its colour (``#RRGGBB``)."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, colour FROM filaments WHERE archived_at IS NULL"
            ).fetchall()
        return {row["id"]: row["colour"] for row in rows}


# -- publish ---------------------------------------------------------------------------


def _unprocessable(slug: str, detail: str, name: str | None = None) -> ApiError:
    extensions = {} if name is None else {"name": name}
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, slug, detail, **extensions)


def _takes(parameter: Parameter, value: ParamValue) -> bool:
    try:
        format_scad_value(parameter, value)
    except ValueError:
        return False
    return True


def _whole_multiple(step: float, of: float) -> bool:
    """Whether ``step`` is a whole number of ``of``s, to a float's rounding. A schema
    step of 0 or less constrains nothing."""
    if of <= 0:
        return True
    times = step / of
    return times >= 1 - 1e-9 and abs(times - round(times)) < 1e-9


def _on_grid(offset: float, step: float) -> bool:
    """Whether ``offset`` is a whole number of ``step``s, to a float's rounding."""
    times = offset / step
    return abs(times - round(times)) < 1e-9


def _check_colour(name: str, value: ParamValue, colours: Collection[str]) -> None:
    """``value`` as the colour of one of the listing's filaments, or 422."""
    if not isinstance(value, str) or value.casefold() not in {c.casefold() for c in colours}:
        raise _unprocessable(
            "listing_colour_unknown",
            f"{name!r} is {value!r}, which is not the colour of a listing filament",
            name,
        )


def _check_bound(parameter: Parameter, bound: ParamBound, colours: Collection[str]) -> None:
    """``bound`` as a subset of what the schema lets ``parameter`` take, or 422."""
    name = parameter.name
    outside = _unprocessable(
        "listing_bounds_outside_schema",
        f"the bounds of {name!r} are not inside what its schema allows",
        name,
    )
    if parameter.type == "color":
        if not isinstance(bound, ChoiceBound):
            raise outside
        for choice in bound.choices:
            _check_colour(name, choice, colours)
        return
    if isinstance(bound, NumberBound):
        if parameter.type not in _NUMBERS or bound.min > bound.max:
            raise outside
        if not (_takes(parameter, bound.min) and _takes(parameter, bound.max)):
            raise outside
        if parameter.type == "integer" and not all(
            float(n).is_integer() for n in (bound.min, bound.max, bound.step or 1)
        ):
            raise outside
        # On a schema with a step, the customer's values stay on its grid: a step that
        # is a whole number of the schema's, from a least value on the grid.
        if parameter.step is not None and parameter.step > 0:
            if bound.step is None or not _whole_multiple(bound.step, parameter.step):
                raise outside
            origin = parameter.min if parameter.min is not None else 0.0
            if not _on_grid(bound.min - origin, parameter.step):
                raise outside
        return
    if isinstance(bound, TextBound):
        if parameter.type != "string" or parameter.options:
            raise outside
        if parameter.max_length is not None and bound.max_length > parameter.max_length:
            raise outside
        return
    # A choice: a font or a template's own `file` samples are never offered to the public.
    if parameter.type in ("file", "font") or not all(_takes(parameter, c) for c in bound.choices):
        raise outside


def checked_fixed(
    schema: CustomizerSchema,
    body: ListingInput,
    colours: Mapping[str, str],
) -> dict[str, ParamValue]:
    """The listing checked against ``schema`` and the price book's ``colours`` (by
    filament id), and every value a render of it uses that the customer does not choose:
    its fixed values, and every parameter it does not name at the schema's default
    (plan point 13). Raises the 422 of the first fault.

    A colour a customer may pick, or one the shop locked or left at its default, must be
    a listing filament's (plan point 12). A parameter in the source's hidden group is
    never in ``schema`` (`render/schema.py` `build_schema` leaves it out), so it is
    neither checked nor fixed here: a render takes it from the source."""
    for filament_id in body.colours:
        if filament_id not in colours:
            raise _unprocessable(
                "listing_colour_unknown",
                f"{filament_id!r} is not a filament of the price book",
            )
    by_name = {parameter.name: parameter for parameter in schema.parameters}
    for name in [*body.params, *body.fixed]:
        if name not in by_name:
            raise _unprocessable(
                "listing_param_unknown", f"the template has no parameter {name!r}", name
            )
    offered = [colours[filament_id] for filament_id in body.colours]
    for name, bound in body.params.items():
        _check_bound(by_name[name], bound, offered)
    for name, value in body.fixed.items():
        if name in body.params:
            raise _unprocessable(
                "listing_fixed_invalid", f"{name!r} is both fixed and offered", name
            )
        if not _takes(by_name[name], value):
            raise _unprocessable("listing_fixed_invalid", f"{name!r} does not take {value!r}", name)
    fixed = dict(body.fixed)
    for parameter in schema.parameters:
        if parameter.name in body.params or parameter.name in fixed:
            continue
        if parameter.initial is not None and _takes(parameter, parameter.initial):
            fixed[parameter.name] = parameter.initial
    for name, value in fixed.items():
        if by_name[name].type == "color":
            _check_colour(name, value, offered)
    return fixed


async def _commit_facts(core: Core, slug: str, commit: str) -> ListingSnapshot:
    """The commit's schema, pipeline flag, name and library pins, without the snapshot
    key; 422 ``listing_reference`` for a slug or commit that is not a revision of a
    template, 422 ``listing_pipeline_unsupported`` for a pipeline template."""
    fetcher = CheckoutFetcher(core.libraries, core.installs, core.checkouts)
    try:
        resolved = await resolve_reference(
            core.catalogue,
            core.history,
            core.outputs,
            paths=core.paths,
            config=core.config,
            fetcher=fetcher,
            slug=slug,
            commit=commit,
            inputs=None,
            output_id=None,
        )
        source = await resolve_source(
            slug, commit, paths=core.paths, history=core.history, fetcher=fetcher
        )
    except (ReferenceError, TemplateDeletedError) as error:
        raise _unprocessable("listing_reference", str(error)) from None
    except (GitError, RevisionNotFoundError) as error:
        raise _unprocessable("listing_reference", f"{slug}@{commit}: {error}") from None
    try:
        schema = await cached_schema(
            source.scad, source.schema_cache, config=source.configure(core.config)
        )
    except (OSError, OpenSCADError) as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"the schema could not be built: {error}"
        ) from None
    directory = source.scad.parent
    if await asyncio.to_thread(declares_pipeline, directory, slug):
        # A pipeline's inputs need not be `model.scad`'s parameters, which is all a
        # listing can bound yet.
        raise _unprocessable(
            "listing_pipeline_unsupported",
            f"{slug!r} is a pipeline template; the storefront cannot list one yet",
        )
    reference = resolved.reference
    return ListingSnapshot(
        schema=schema,
        pipeline=False,
        template_name=reference.template_name,
        libraries=list(reference.libraries),
        snapshot_key="",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _copy_media(
    core: Core, refs: BlobRefs, listing: Listing, title: str
) -> list[ListingMedia]:
    """Each chosen media file stored as ``listing-media-<sha256>`` and held."""
    content = core.store.content
    assert content is not None
    copied: list[ListingMedia] = []
    for media_id in listing.media_ids:
        try:
            view, path = await asyncio.to_thread(core.catalogue.media_item, listing.slug, media_id)
        except (MediaNotFoundError, ModelNotFoundError):
            raise _unprocessable(
                "listing_media_unknown", f"the template has no media {media_id!r}"
            ) from None
        key = f"listing-media-{await asyncio.to_thread(_sha256, path)}"
        data = await asyncio.to_thread(path.open, "rb")
        try:
            await content.put(
                "media",
                data,
                name=view.file,
                scope=BlobScope(slug=listing.slug, title=title),
                key=key,
            )
        finally:
            await asyncio.to_thread(data.close)
        await asyncio.to_thread(refs.add, key, HOLDER, listing.id)
        copied.append(
            ListingMedia(
                blob_key=key, kind=view.kind, media_type=view.content_type, caption=view.caption
            )
        )
    return copied


async def publish(core: Core, store: ListingStore, listing_id: str) -> Listing:
    """The listing checked against its commit and published (see the module's notes).
    409 ``storefront_needs_s3`` unless the store is S3; 422 ``listing_param_unknown``,
    ``listing_bounds_outside_schema``, ``listing_fixed_invalid``,
    ``listing_colour_unknown``, ``listing_media_unknown``, ``listing_reference`` or
    ``listing_pipeline_unsupported`` for a listing its commit or the price book does not
    back; 409 ``listing_changed`` when it
    was edited while it was being published; 409 ``listing_publishing`` while another
    publish of it runs (it is refused, not queued); 503 ``listing_publish_busy`` while
    :data:`PUBLISH_LOCKS_MAX` other publishes run."""
    listing = await asyncio.to_thread(store.get, listing_id)
    if listing is None:
        raise _not_found(listing_id)
    snapshots = core.store.snapshots
    # The public site serves listing media as presigned GETs, and a visitor's render
    # reads the snapshot from the content store (plan).
    if core.store.backend != "s3" or snapshots is None:
        raise needs_s3()
    end = await asyncio.to_thread(store.begin_publish, listing.id)
    try:
        return await _publish(core, store, listing, snapshots)
    finally:
        await asyncio.to_thread(end)


async def _publish(
    core: Core, store: ListingStore, listing: Listing, snapshots: SnapshotStore
) -> Listing:
    facts = await _commit_facts(core, listing.slug, listing.commit)
    colours = await asyncio.to_thread(store.filament_colours)
    fixed = checked_fixed(facts.schema_, listing, colours)
    refs = BlobRefs(core.projection.pool)
    try:
        key = await snapshots.ensure(listing.slug, listing.commit)
        await asyncio.to_thread(refs.add, key, HOLDER, listing.id)
        media = await _copy_media(core, refs, listing, facts.template_name)
        return await asyncio.to_thread(
            store.mark_published,
            listing.id,
            snapshot=facts.model_copy(update={"snapshot_key": key, "fixed": fixed}),
            media=media,
            expected_updated_at=listing.updated_at,
        )
    except BaseException:
        await asyncio.to_thread(store.release_unheld, listing.id)
        raise


async def unpublish(store: ListingStore, listing_id: str) -> Listing:
    """The listing off the public site; its hold on the snapshot and the media goes with
    it, so the sweep may take them once nothing else holds them."""
    return await asyncio.to_thread(store.mark_unpublished, listing_id)


# -- the public site's view (#5282 Task 6) ----------------------------------------------


def public_content(core: Core) -> ContentStore:
    """The S3 content store the public site's media is presigned on. On any other the
    fault is the operator's, not the visitor's: 503 ``storefront_needs_s3`` with a
    plain text and reason ``not_configured`` (the edge's client answers it at once),
    and the operator's text logged at most once a minute."""
    global _needs_s3_warned
    content = core.store.content
    if (
        core.store.backend == "s3"
        and content is not None
        and isinstance(content.backend, S3ContentBackend)
    ):
        return content
    now = time.monotonic()
    if _needs_s3_warned is None or now - _needs_s3_warned >= NEEDS_S3_WARN_EVERY:
        _needs_s3_warned = now
        logger.warning("the public storefront is off: set store_backend to s3 in Settings")
    raise problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "storefront_needs_s3",
        "The shop is not available right now.",
        reason=NOT_CONFIGURED,
    )


def _public_param(
    name: str, bound: ParamBound, by_name: Mapping[str, Parameter], colours: Collection[str]
) -> PublicParam:
    """``bound`` with what the page shows. A colour's choices keep only ``colours`` (the
    listing filaments the price book still has, casefolded)."""
    parameter = by_name.get(name)
    shown = {
        "label": shown_label(name, bound.label, parameter.caption if parameter else None),
        "group": parameter.group if parameter is not None else "",
        "colour": parameter is not None and parameter.type == "color",
    }
    fields = {**bound.model_dump(), **shown}
    if isinstance(bound, TextBound) and parameter is not None:
        initial = parameter.initial
        fields["placeholder"] = initial if isinstance(initial, str) and initial.strip() else None
    if isinstance(bound, ChoiceBound) and shown["colour"]:
        fields["choices"] = [
            c for c in bound.choices if isinstance(c, str) and c.casefold() in colours
        ]
    if isinstance(bound, NumberBound):
        return PublicNumber.model_validate(fields)
    if isinstance(bound, ChoiceBound):
        return PublicChoice.model_validate(fields)
    return PublicText.model_validate(fields)


async def _media_url(content: ContentStore, listing_id: str, key: str) -> str | None:
    """A presigned GET of the held blob ``key``, or None (logged, never with a URL) when
    the index lost it. It reads the index only: the listing holds the blob, so no HEAD
    is needed."""
    stat = await asyncio.to_thread(content.index.get, key)
    if stat is None or stat.ref.backend != content.name:
        logger.warning(
            "listing %s names media %s, which the blob index does not have; left out",
            listing_id,
            key,
            extra={"listing_id": listing_id, "blob_key": key},
        )
        return None
    backend = content.backend
    assert isinstance(backend, S3ContentBackend)
    return await backend.presign_get(stat.ref.backend_id, PUBLIC_MEDIA_TTL)


def _like_escaped(text: str) -> str:
    """``text`` with LIKE's wildcards and its escape character taken literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def listing_cursor(listing: Listing) -> str:
    """The public list's cursor after ``listing``: its position and id, as hex of JSON."""
    return json.dumps([listing.position, listing.id], separators=(",", ":")).encode().hex()


def cursor_after(cursor: str) -> tuple[int, str]:
    """The position and id a `listing_cursor` names; 422 ``listing_cursor_invalid`` for
    any other text."""
    try:
        position, listing_id = json.loads(bytes.fromhex(cursor))
    except (UnicodeDecodeError, ValueError, TypeError):
        position = listing_id = None
    if (
        not isinstance(position, int)
        or isinstance(position, bool)
        # ``position`` is an ``integer`` column: a cursor past it was never given out.
        or not -(2**31) <= position < 2**31
        or not isinstance(listing_id, str)
        or not LISTING_ID.fullmatch(listing_id)
    ):
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "listing_cursor_invalid",
            "This page of designs is not one the shop gave out.",
        )
    return position, listing_id


async def public_card(
    content: ContentStore, listing: Listing, filaments: Mapping[str, Filament]
) -> PublicListingCard:
    """``listing`` as a card of the public list (#6154): its first picture as the one
    presigned GET, and whether every colour it offers still has a filament."""
    assert listing.snapshot is not None
    colours = {filaments[i].colour.casefold() for i in listing.colours if i in filaments}
    by_name = {p.name: p for p in listing.snapshot.schema_.parameters}
    # As the page's `colourGone` (`frontend/src/storefront/params.ts`): a colour with no
    # filament left cannot be ordered.
    available = True
    for name, bound in listing.params.items():
        shown = _public_param(name, bound, by_name, colours)
        if isinstance(shown, PublicChoice) and shown.colour and not shown.choices:
            available = False
            break
    image = None
    for item in listing.media:
        if item.kind != "image":
            continue
        url = await _media_url(content, listing.id, item.blob_key)
        if url is not None:
            image = PublicMedia(url=url, kind=item.kind, caption=item.caption)
            break
    return PublicListingCard(
        id=listing.id,
        title=listing.title,
        description=listing.description,
        image=image,
        available=available,
    )


async def public_listing(
    content: ContentStore, listing: Listing, filaments: Mapping[str, Filament]
) -> PublicListing:
    """The published ``listing`` as the public site shows it: each bound with its
    schema label and group, the listing's filaments the price book still has
    (``filaments``, by id), and each media file as a presigned GET of
    `PUBLIC_MEDIA_TTL`. A media file the index lost is left out, and logged."""
    assert listing.snapshot is not None
    by_name = {p.name: p for p in listing.snapshot.schema_.parameters}
    offered = [filaments[i] for i in listing.colours if i in filaments]
    colours = {f.colour.casefold() for f in offered}
    media: list[PublicMedia] = []
    for item in listing.media:
        url = await _media_url(content, listing.id, item.blob_key)
        if url is not None:
            media.append(PublicMedia(url=url, kind=item.kind, caption=item.caption))
    return PublicListing(
        id=listing.id,
        title=listing.title,
        description=listing.description,
        params={
            name: _public_param(name, b, by_name, colours) for name, b in listing.params.items()
        },
        colours=[
            PublicFilament(id=f.id, colour=f.colour, colour_name=f.colour_name, material=f.material)
            for f in offered
        ],
        media=media,
        updated_at=listing.updated_at,
    )


def published_listing(store: ListingStore, listing_id: str) -> Listing:
    """The listing, when it is published; 404 ``listing_not_found`` for an unpublished
    or unknown one alike, so a visitor cannot tell them apart, and for an id that is not
    one, before any lookup."""
    listing = store.get(listing_id) if LISTING_ID.fullmatch(listing_id) else None
    if listing is None or not listing.published or listing.snapshot is None:
        raise problem(status.HTTP_404_NOT_FOUND, "listing_not_found", "There is no such listing.")
    return listing
