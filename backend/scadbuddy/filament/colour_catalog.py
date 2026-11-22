"""The colour catalog's sources (#6907, plan `docs/superpowers/plans/2026-11-18-filament-bb-sync.md`
Decisions 206-222): Bambuddy's colour catalog (the pull, `bb_pull_apply.LocalBbPull.colours`),
filamentcolors.xyz and Bambu Studio's colour codes (`ColourSyncs`, run as the operation
``filament_colour_sync`` on the ``library`` queue, `filament/operations.py`).

Every source writes through `upsert_colours`, one transaction under one advisory lock:

- A row is one maker's colour of one material: its identity is the manufacturer, the name
  and the material as the source names it (``material_name``), compared case-blind, as
  Bambuddy's own filamentcolors sync compares them (``sync_from_filamentcolors``,
  ``backend/app/api/routes/inventory.py`` lines 1339-1434 at
  https://github.com/maziggy/bambuddy/tree/b7d894b57c0a373f66dc0de49680865b02b3aae5).
  Bambuddy's entry is found first by its link (``bb_colour_id``), then by identity.
- A row another source wrote is ``kept`` as it is (Decision 221): the first source
  to write a row keeps it; Bambuddy's entry still links to it.
- A row found that a person made or edited (``synced_at`` NULL: a person's write of a
  value the sync writes clears it, `catalog.CatalogStore.update`; a re-order or an archive
  does not) is ``kept`` as it is (Decisions 208,
  217); a Bambuddy entry still links to it. A sync's write sets ``updated_at`` and
  ``synced_at`` to the database's ``now()``, and it reads the row ``FOR UPDATE``, so an
  edit that commits while the sync runs waits for it or is seen by it, whatever the
  clocks say.
- An archived row counts and is ``kept`` (restored, it is synced again): a sync never
  brings back or changes a row a person archived, and never
  deletes a row (Decision 209).
- A source's material is resolved to the materials catalog by key (`catalog.lookup_key`):
  the whole name, else its first word ("PLA Basic" is PLA); an effect only by the whole
  name. Neither adds a catalog row.

filamentcolors.xyz's API is public (https://filamentcolors.xyz/api/; its README, "The API",
at https://github.com/itsthejoker/filamentcolors.xyz/tree/c660d80a5ebb5902c2ecc1cebd2b47a5960e9b88):
``/api/swatch/`` pages of at most 100 (``page_size``), newest first by default, and
``/api/version/`` whose ``db_last_modified`` changes with each new swatch, which the README
asks callers to check before reading again. A run reads the version first and reads no page
when it has not changed since the last finished run (Decision 211); it reads newest first,
so a swatch added while it runs moves the rest later and none is missed (Decision 212). A
failed read (no answer, a 429 or a 5xx after `TRIES`, any other status, another shape) ends
the run ``failed`` with the reason; what it wrote stays (Decision 213).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from psycopg import errors
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.models import BbColour
from scadbuddy.filament.catalog import Conn, lookup_key
from scadbuddy.filament.colour import canonical_colours, canonical_rgba
from scadbuddy.filament.colour_codes import ColourTableMissingError, colour_codes
from scadbuddy.filament.models import ColourSource

logger = logging.getLogger(__name__)

FILAMENTCOLORS_API = "https://filamentcolors.xyz/api"
#: The most the API serves a page (its README, "Searching and filtering swatches").
PAGE_SIZE = 100
#: A bound on one run's pages: 2,256 swatches are 23 pages (measured 2026-11-21).
MAX_PAGES = 200
#: Tries of one read when it may pass later (no answer, 429, 5xx).
TRIES = 3
#: The longest ``Retry-After`` a run waits for.
RETRY_AFTER_MAX = 30.0
REQUEST_TIMEOUT = 30.0
USER_AGENT = "ScadBuddy (+https://github.com/eh-homelab/ScadBuddy)"
#: Bambu Lab's colours (Bambu Studio's table names no maker).
BAMBU_LAB = "Bambu Lab"
#: filamentcolors' colour family for a translucent swatch (its README's table): such a
#: swatch's hex has no alpha, so it takes Bambu's translucent alpha (Decision 214).
TRANSLUCENT_FAMILY = "TRN"
TRANSLUCENT_ALPHA = "80"
#: A name, maker or material longer than the catalog takes is skipped.
TEXT_MAX = 128
#: A run still ``running`` after this is one its process lost; the next start ends it.
RUN_STALE = timedelta(minutes=30)
#: Finished runs older than this are deleted when a new one starts (each source keeps its
#: newest).
RUNS_KEPT = timedelta(days=30)

_LOCK = "scadbuddy:filament:colours"

SyncSource = Literal["filamentcolors", "bambu"]
SyncState = Literal["running", "done", "unchanged", "failed"]


@dataclass(frozen=True)
class ColourIn:
    """One colour as a source names it. ``rgba`` and ``extra_colours`` are ``#RRGGBBAA``."""

    manufacturer: str
    name: str
    rgba: str
    material_name: str | None = None
    extra_colours: list[str] | None = None
    effect_name: str | None = None
    bb_colour_id: int | None = None


class ColourCounts(BaseModel):
    """What one write did: rows added, rows a sync owned that it changed, rows it found
    as they were, rows a person made or edited or another source wrote (``kept``), and
    colours it could not take
    (no name, maker or colour, a name too long, a name a person's row of the same
    identity holds after a rename, or a second colour of one identity in one batch)."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    kept: int = 0
    skipped: int = 0

    def add(self, other: ColourCounts) -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


class ColourSync(BaseModel):
    """One run of a colour sync: its progress while it runs, its counts once it ended."""

    id: UUID
    source: SyncSource
    state: SyncState
    pages_read: int
    pages_total: int | None
    total: int | None
    added: int
    updated: int
    unchanged: int
    kept: int
    skipped: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class ColourSyncRequest(BaseModel, extra="forbid"):
    """``POST /filament/colour-syncs``: which source to read. ``force`` reads
    filamentcolors.xyz's pages even when its version has not changed since the last run."""

    source: SyncSource
    force: bool = False


class ColourSyncRunningError(Exception):
    """A run of that source is running."""


class ColourSourceError(Exception):
    """The source could not be read; the text says why (no Bambuddy, URL or body text)."""


def _text(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else None
    return text or None


def _material_id(conn: Conn, name: str | None, cache: dict[str, UUID | None]) -> UUID | None:
    if name is None:
        return None
    if name not in cache:
        found: UUID | None = None
        for candidate in dict.fromkeys([name, name.split()[0]]):
            keyed, found = lookup_key(conn, "materials", candidate)
            if keyed and found is not None:
                break
        cache[name] = found
    return cache[name]


def _effect_id(conn: Conn, name: str | None, cache: dict[str, UUID | None]) -> UUID | None:
    if name is None:
        return None
    if name not in cache:
        cache[name] = lookup_key(conn, "effects", name)[1]
    return cache[name]


_COLUMNS = "id, name, manufacturer, rgba, material_name, material_id, extra_colours, effect_id"


def upsert_colours(conn: Conn, rows: Iterable[ColourIn], source: ColourSource) -> ColourCounts:
    """Write ``rows`` as the module says, in the caller's transaction, under the catalog's
    advisory lock, stamped with the database's ``now()``. Each row is its own savepoint,
    so one the database refuses (a key another row holds, a CHECK, a bad value) is
    ``skipped``."""
    counts = ColourCounts()
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_LOCK,))
    stamp = conn.execute("SELECT now() AS now").fetchone()
    assert stamp is not None
    now: datetime = stamp["now"]
    materials: dict[str, UUID | None] = {}
    effects: dict[str, UUID | None] = {}
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        identity = (row.manufacturer.upper(), row.name.upper(), (row.material_name or "").upper())
        too_long = any(
            len(v or "") > TEXT_MAX for v in (row.manufacturer, row.name, row.material_name)
        )
        if identity in seen or too_long or not row.name or not row.manufacturer:
            counts.skipped += 1
            continue
        seen.add(identity)
        try:
            with conn.transaction():
                outcome = _one(conn, row, source, now, materials, effects)
        except (errors.IntegrityError, errors.DataError) as refused:
            logger.warning(
                "a %s colour was skipped", source, extra={"error": type(refused).__name__}
            )
            outcome = "skipped"
        setattr(counts, outcome, getattr(counts, outcome) + 1)
    return counts


def _one(
    conn: Conn,
    row: ColourIn,
    source: ColourSource,
    now: datetime,
    materials: dict[str, UUID | None],
    effects: dict[str, UUID | None],
) -> Literal["added", "updated", "unchanged", "kept", "skipped"]:
    found = None
    if row.bb_colour_id is not None:
        found = conn.execute(
            f"SELECT {_COLUMNS}, source, synced_at, updated_at, archived_at, bb_colour_id"
            " FROM filament_colours"
            " WHERE bb_colour_id = %s FOR UPDATE",
            (row.bb_colour_id,),
        ).fetchone()
    if found is None:
        found = conn.execute(
            f"SELECT {_COLUMNS}, source, synced_at, updated_at, archived_at, bb_colour_id"
            " FROM filament_colours"
            " WHERE coalesce(upper(manufacturer), '') = upper(%s) AND upper(name) = upper(%s)"
            " AND coalesce(upper(material_name), '') = upper(%s)"
            " ORDER BY archived_at IS NOT NULL, created_at LIMIT 1 FOR UPDATE",
            (row.manufacturer, row.name, row.material_name or ""),
        ).fetchone()
    values = {
        "name": row.name,
        "manufacturer": row.manufacturer,
        "rgba": row.rgba,
        "material_name": row.material_name,
        "material_id": _material_id(conn, row.material_name, materials),
        "extra_colours": row.extra_colours,
        "effect_id": _effect_id(conn, row.effect_name, effects),
    }
    if found is None:
        names = list(values)
        conn.execute(
            f"INSERT INTO filament_colours ({', '.join(names)}, source, synced_at, updated_at,"
            f" bb_colour_id) VALUES ({', '.join('%s' for _ in names)}, %s, %s, %s, %s)",
            [*values.values(), source, now, now, row.bb_colour_id],
        )
        return "added"
    if row.bb_colour_id is not None and found["bb_colour_id"] is None:
        # A link changes no value, so it is no edit (`updated_at` and `synced_at` stay).
        conn.execute(
            "UPDATE filament_colours SET bb_colour_id = %s WHERE id = %s",
            (row.bb_colour_id, found["id"]),
        )
    if found["synced_at"] is None or found["archived_at"] is not None:
        # The person's row, or one they archived (left as it is until restored).
        return "kept"
    if found["source"] != source:
        # Another source's colour: the first source to write a row keeps it (Decision
        # 221), so a Bambuddy entry never drops a filamentcolors swatch's alpha.
        return "kept"
    if all(found[name] == value for name, value in values.items()):
        return "unchanged"
    sets = ", ".join(f"{name} = %s" for name in values)
    conn.execute(
        f"UPDATE filament_colours SET {sets}, source = %s, synced_at = %s, updated_at = %s"
        " WHERE id = %s",
        [*values.values(), source, now, now, found["id"]],
    )
    return "updated"


# -- the sources' rows ------------------------------------------------------------------


def bambuddy_rows(entries: Sequence[BbColour]) -> tuple[list[ColourIn], int]:
    """Bambuddy's colour catalog as rows, and how many entries have no colour."""
    rows: list[ColourIn] = []
    bad = 0
    for entry in entries:
        rgba = canonical_rgba(entry.hex_color)
        name, maker = _text(entry.color_name), _text(entry.manufacturer)
        if rgba is None or name is None or maker is None:
            bad += 1
            continue
        rows.append(
            ColourIn(
                manufacturer=maker,
                name=name,
                rgba=rgba,
                material_name=_text(entry.material),
                extra_colours=canonical_colours(entry.extra_colors),
                effect_name=_text(entry.effect_type),
                bb_colour_id=entry.id,
            )
        )
    return rows, bad


def swatch_row(swatch: Any) -> ColourIn | None:
    """One filamentcolors.xyz swatch as a row; ``None`` when it lacks a maker, a name or a
    colour. A swatch of the translucent family takes alpha ``80`` (Decision 214)."""
    if not isinstance(swatch, dict):
        return None
    maker = swatch.get("manufacturer")
    kind = swatch.get("filament_type")
    name = _text(swatch.get("color_name"))
    maker_name = _text(maker.get("name")) if isinstance(maker, dict) else None
    rgba = canonical_rgba(_text(swatch.get("hex_color")))
    if name is None or maker_name is None or rgba is None:
        return None
    families = {swatch.get("color_parent"), swatch.get("alt_color_parent")}
    if TRANSLUCENT_FAMILY in families and rgba.endswith("FF"):
        rgba = rgba[:7] + TRANSLUCENT_ALPHA
    return ColourIn(
        manufacturer=maker_name,
        name=name,
        rgba=rgba,
        material_name=_text(kind.get("name")) if isinstance(kind, dict) else None,
    )


def bambu_rows(profiles_dir: Path) -> list[ColourIn]:
    """Bambu Studio's colour codes as Bambu Lab's colours: the first colour of a code is
    its colour, the rest its extra colours, alpha as the table gives it (``80`` for a
    translucent one); the material is the code's ``fila_type``. `ColourTableMissingError`
    when the image has no table."""
    return [
        ColourIn(
            manufacturer=BAMBU_LAB,
            name=code.name,
            rgba=code.rgba[0],
            material_name=_text(code.fila_type),
            extra_colours=code.rgba[1:] or None,
        )
        for code in colour_codes(profiles_dir).all()
        if code.rgba
    ]


# -- the runs ---------------------------------------------------------------------------


Sleep = Callable[[float], Awaitable[None]]


def _why(stopped: BaseException) -> str:
    """A run's error for an exception it did not expect: a cancel (the operation's timeout
    or a worker's shutdown, delivered through its heartbeat between pages) interrupted it."""
    return INTERRUPTED if isinstance(stopped, asyncio.CancelledError) else "internal error"


INTERRUPTED = "the run was interrupted"


def _end_stale(conn: Conn, source: SyncSource | None = None) -> None:
    """End ``failed`` every run (of ``source``, or of any) still ``running`` past
    `RUN_STALE`."""
    conn.execute(
        "UPDATE filament_colour_syncs SET state = 'failed', finished_at = now(), error = %s"
        " WHERE state = 'running' AND started_at <= now() - %s"
        " AND (%s::text IS NULL OR source = %s)",
        (INTERRUPTED, RUN_STALE, source, source),
    )


class ColourSyncs:
    """The colour catalog's sync runs over a pool whose connections autocommit and return
    dict rows. Its methods block: `run` calls them in threads."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        # The shared `scadbuddy-rows` pool (`rows/component.py`), opened on first use.
        pool.open(wait=False)
        self._pool = pool

    def upsert(self, rows: Sequence[ColourIn], source: ColourSource) -> ColourCounts:
        with self._pool.connection() as conn, conn.transaction():
            return upsert_colours(conn, rows, source)

    def latest(self) -> list[ColourSync]:
        """Each source's newest run. A run still ``running`` past `RUN_STALE` ends
        ``failed`` first: its process died (a restart), and nothing else would end it."""
        with self._pool.connection() as conn, conn.transaction():
            _end_stale(conn)
            rows = conn.execute(
                "SELECT DISTINCT ON (source) * FROM filament_colour_syncs"
                " ORDER BY source, started_at DESC"
            ).fetchall()
        return [ColourSync.model_validate(row) for row in rows]

    def get(self, sync_id: UUID) -> ColourSync:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM filament_colour_syncs WHERE id = %s", (sync_id,)
            ).fetchone()
        assert row is not None
        return ColourSync.model_validate(row)

    def running(self, source: SyncSource) -> bool:
        """Whether a run of ``source`` is running and not stale."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM filament_colour_syncs WHERE source = %s AND state = 'running'"
                " AND started_at > now() - %s",
                (source, RUN_STALE),
            ).fetchone()
        return row is not None

    def start(self, source: SyncSource, operation_id: str | None = None) -> UUID:
        """A new run of ``source`` for the operation run ``operation_id``; first a stale
        run, and a run the same operation left (an attempt that died, now retried), end
        ``failed``, and finished runs past `RUNS_KEPT` but the newest are deleted.
        `ColourSyncRunningError` while another runs."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{_LOCK}:{source}",))
            _end_stale(conn, source)
            if operation_id is not None:
                conn.execute(
                    "UPDATE filament_colour_syncs SET state = 'failed', finished_at = now(),"
                    " error = %s WHERE source = %s AND state = 'running' AND operation_id = %s",
                    (INTERRUPTED, source, operation_id),
                )
            if conn.execute(
                "SELECT 1 FROM filament_colour_syncs WHERE source = %s AND state = 'running'",
                (source,),
            ).fetchone():
                raise ColourSyncRunningError(source)
            conn.execute(
                "DELETE FROM filament_colour_syncs WHERE source = %s AND started_at < now() - %s"
                " AND id <> (SELECT id FROM filament_colour_syncs WHERE source = %s"
                " ORDER BY started_at DESC LIMIT 1)",
                (source, RUNS_KEPT, source),
            )
            row = conn.execute(
                "INSERT INTO filament_colour_syncs (source, operation_id) VALUES (%s, %s)"
                " RETURNING id",
                (source, operation_id),
            ).fetchone()
        assert row is not None
        made: UUID = row["id"]
        return made

    def last_modified(self, source: SyncSource) -> int | None:
        """filamentcolors.xyz's ``db_last_modified`` at the newest run that read every page."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT db_last_modified FROM filament_colour_syncs WHERE source = %s"
                " AND state IN ('done', 'unchanged') ORDER BY started_at DESC LIMIT 1",
                (source,),
            ).fetchone()
        return None if row is None else row["db_last_modified"]

    def progress(
        self,
        sync_id: UUID,
        counts: ColourCounts,
        *,
        pages_read: int | None = None,
        pages_total: int | None = None,
        total: int | None = None,
    ) -> None:
        """Add ``counts`` to the run's and set its page counts."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE filament_colour_syncs SET added = added + %s, updated = updated + %s,"
                " unchanged = unchanged + %s, kept = kept + %s, skipped = skipped + %s,"
                " pages_read = coalesce(%s, pages_read), pages_total = coalesce(%s, pages_total),"
                " total = coalesce(%s, total) WHERE id = %s",
                (
                    counts.added,
                    counts.updated,
                    counts.unchanged,
                    counts.kept,
                    counts.skipped,
                    pages_read,
                    pages_total,
                    total,
                    sync_id,
                ),
            )

    def finish(
        self,
        sync_id: UUID,
        state: SyncState,
        *,
        error: str | None = None,
        db_last_modified: int | None = None,
    ) -> ColourSync:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE filament_colour_syncs SET state = %s, error = %s, db_last_modified = %s,"
                " finished_at = now() WHERE id = %s",
                (state, error, db_last_modified, sync_id),
            )
        return self.get(sync_id)


async def _read_json(
    http: httpx.AsyncClient, url: str, params: dict[str, Any] | None, sleep: Sleep
) -> Any:
    """One GET of filamentcolors.xyz as JSON; a read that may pass later is tried again,
    `TRIES` in all. `ColourSourceError` names why it failed."""
    for attempt in range(1, TRIES + 1):
        wait = float(2**attempt)
        try:
            response = await http.get(url, params=params)
        except httpx.HTTPError as error:
            reason = f"filamentcolors.xyz did not answer ({type(error).__name__})"
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    raise ColourSourceError("filamentcolors.xyz answered with no JSON") from None
            reason = f"filamentcolors.xyz answered {response.status_code}"
            if response.status_code != 429 and response.status_code < 500:
                raise ColourSourceError(reason)
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                wait = min(float(retry_after), RETRY_AFTER_MAX)
        if attempt == TRIES:
            raise ColourSourceError(reason)
        await sleep(wait)
    raise AssertionError("unreachable: the last try returns or raises")


async def run_filamentcolors(
    syncs: ColourSyncs,
    sync_id: UUID,
    http: httpx.AsyncClient,
    *,
    force: bool = False,
    sleep: Sleep = asyncio.sleep,
) -> ColourSync:
    """Read filamentcolors.xyz's swatches into the catalog, a page at a time, each page its
    own write and progress step; as the module says."""
    try:
        version = await _read_json(http, f"{FILAMENTCOLORS_API}/version/", None, sleep)
        modified = version.get("db_last_modified") if isinstance(version, dict) else None
        modified = modified if isinstance(modified, int) else None
        if (
            not force
            and modified is not None
            and modified == await asyncio.to_thread(syncs.last_modified, "filamentcolors")
        ):
            return await asyncio.to_thread(
                syncs.finish, sync_id, "unchanged", db_last_modified=modified
            )
        for page in range(1, MAX_PAGES + 1):
            data = await _read_json(
                http,
                f"{FILAMENTCOLORS_API}/swatch/",
                {"page": page, "page_size": PAGE_SIZE},
                sleep,
            )
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ColourSourceError("filamentcolors.xyz answered in another shape")
            count = data.get("count")
            total = count if isinstance(count, int) else None
            rows = [swatch_row(one) for one in results]
            counts = await asyncio.to_thread(
                syncs.upsert, [row for row in rows if row is not None], "filamentcolors"
            )
            counts.skipped += sum(row is None for row in rows)
            await asyncio.to_thread(
                syncs.progress,
                sync_id,
                counts,
                pages_read=page,
                pages_total=None if total is None else max(1, math.ceil(total / PAGE_SIZE)),
                total=total,
            )
            if not data.get("next") or not results:
                break
        else:
            # Pages are left: the run did not read every swatch, so it records no version.
            raise ColourSourceError(
                f"filamentcolors.xyz has more than {MAX_PAGES} pages; the rest were not read"
            )
        return await asyncio.to_thread(syncs.finish, sync_id, "done", db_last_modified=modified)
    except ColourSourceError as error:
        logger.warning("the filamentcolors.xyz sync failed: %s", error)
        return await asyncio.to_thread(syncs.finish, sync_id, "failed", error=str(error))
    except BaseException as stopped:
        await asyncio.shield(
            asyncio.to_thread(syncs.finish, sync_id, "failed", error=_why(stopped))
        )
        raise


async def run_bambu(syncs: ColourSyncs, sync_id: UUID, profiles_dir: Path) -> ColourSync:
    """Bambu Studio's colour codes into the catalog as Bambu Lab's colours, in one write."""
    try:
        rows = await asyncio.to_thread(bambu_rows, profiles_dir)
        counts = await asyncio.to_thread(syncs.upsert, rows, "bambu")
        await asyncio.to_thread(
            syncs.progress, sync_id, counts, pages_read=1, pages_total=1, total=len(rows)
        )
        return await asyncio.to_thread(syncs.finish, sync_id, "done")
    except ColourTableMissingError as error:
        return await asyncio.to_thread(syncs.finish, sync_id, "failed", error=str(error))
    except BaseException as stopped:
        await asyncio.shield(
            asyncio.to_thread(syncs.finish, sync_id, "failed", error=_why(stopped))
        )
        raise


def http_client() -> httpx.AsyncClient:
    """The client a run reads filamentcolors.xyz with."""
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
    )
