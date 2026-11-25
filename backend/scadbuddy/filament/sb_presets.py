"""ScadBuddy's own filament presets (#7176, plan 2026-11-23-scadbuddy-filament-presets.md).

ScadBuddy is the record of a filament preset. A preset is a row of ``filament_presets``
whose current document is a blob (``blob_refs`` holder `HOLDER`, holder id
``<id>:<version>``, one hold per version); a changed document is a new version of the
same preset (`FilamentPresetStore.version_on`), so every binding that names it
(``sb-<id>``, `slicing/own_presets.py`) takes the change. ``source`` says who owns it:
``3dfp`` and ``manual`` are ScadBuddy's, and Bambuddy gets only a mirror; ``bambuddy``
(made in Bambuddy) is Bambuddy's, and each refresh takes a new version of it when
Bambuddy's ``updated_at`` moved (`refresh_bambuddy`).

`adopt_held` turns each binding that still names a Bambuddy local preset into an
``sb-`` ref (Decision 6): to the ScadBuddy preset with that Bambuddy id, else to a new one
made from Bambuddy's document. `import_presets` runs `refresh_bambuddy` and then
`adopt_held`, and nothing else imports Bambuddy's presets (#7244, the owner's decisions of
2026-11-23: ScadBuddy owns its presets, and Bambuddy is at most a mirror target or the
source of a one-time import). Every import reads Bambuddy itself (`LivePresets`: the
list, then each preset it needs), never a copy: no copy of Bambuddy's local presets is
kept, and a native slice never reads one. It runs in three places only:

- the Schedule's pull (`spools/activities.py` `filament_pull_activity`), after the spools,
  only while ``filament_sync.presets`` pulls (``bb_to_sb`` or ``two_way``) and the links
  are the configured Bambuddy's;
- a person's or the agent's one-time import (``POST /filament/presets/bambuddy-import``),
  whatever the direction is;
- the API's start (`adopt_at_start`): only `adopt_held`, only while a binding still names a
  Bambuddy local id, whatever the direction is. It is how the bindings made before #7244
  became ``sb-`` refs when the copy went; afterwards a binding that names a Bambuddy local
  id is an error the print dialog shows (`bambuddy/resolver.py`).

The bytes go to the blob store, which SQL cannot write, so the import is code that the
deployed API runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from collections.abc import Callable, Collection
from datetime import datetime
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from fastapi import status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.models import LocalPresetDetail
from scadbuddy.core.credentials import strip_credentials
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.rfid_push import acquire_lock
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.attachments import keep_file, read_blob
from scadbuddy.slicing.own_presets import OwnRow, facts, sb_id, sb_ref
from scadbuddy.store import BlobStore

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]
Source = Literal["3dfp", "bambuddy", "manual"]

#: The ``blob_refs`` holder of a preset's versions.
HOLDER: Final = "filament_preset"
#: The transaction lock every write of a preset row takes, so two adoptions of one
#: Bambuddy id make one preset.
LOCK: Final = "scadbuddy:filament:presets"
#: The binding tables, each with the column of its owner.
BINDINGS: Final = ("filament_spool_type_presets", "filament_spool_presets")


class PresetSource(Protocol):
    """Where an import reads Bambuddy's local presets (`LivePresets`; a test's is in
    memory)."""

    async def stamps(self) -> dict[int, str | None]:
        """Each preset's ``updated_at``, by id."""
        ...

    async def documents(self, ids: Collection[int]) -> dict[int, LocalPresetDetail]:
        """The presets of ``ids`` with their documents; an id it lacks is left out."""
        ...


class LivePresets:
    """Bambuddy's filament local presets read now, for a one-time import: one list, then
    ``GET /local-presets/{id}`` only for a listed id the import asks for. A preset gone
    since the list is left out."""

    def __init__(self, client: BambuddyClient) -> None:
        self._client = client
        self._listed: dict[int, str | None] | None = None

    async def stamps(self) -> dict[int, str | None]:
        if self._listed is None:
            listing = await self._client.local_presets()
            self._listed = {row.id: row.updated_at for row in listing.filament}
        return dict(self._listed)

    async def documents(self, ids: Collection[int]) -> dict[int, LocalPresetDetail]:
        listed = await self.stamps()
        found: dict[int, LocalPresetDetail] = {}
        for preset_id in sorted(set(ids) & listed.keys()):
            try:
                found[preset_id] = await self._client.local_preset(preset_id)
            except ApiError as error:
                if error.status != status.HTTP_404_NOT_FOUND:
                    raise
        return found


class FilamentPreset(BaseModel):
    """A ScadBuddy filament preset, without its document."""

    id: int
    name: str
    source: Source
    type_id: UUID | None
    printer_model: str | None
    nozzle_diameter: str | None
    tdfp_id: int | None
    fetch_run_id: UUID | None
    version: int
    blob_key: str
    sha256: str
    filament_type: str | None
    filament_colour: str | None
    compatible_printers: list[str]
    bb_preset_id: int | None
    #: The Bambuddy ``bb_preset_id`` belongs to (`bb_sync_nudge.instance_for`); ``None``
    #: for a row from before it was kept, taken for the configured one's.
    bb_instance: str | None
    #: Bambuddy's ``updated_at`` of ``bb_preset_id``, as it sent it: for a ``bambuddy``
    #: preset when its document was last taken (`refresh_bambuddy`); for a ``3dfp`` or
    #: ``manual`` one, of the mirror's last write (`preset_mirror._mirrored`; ``None`` for a
    #: preset sent before the mirror kept it, until its next mirror write), which the links'
    #: reset compares to prove the same Bambuddy (#7343).
    bb_updated_at: str | None
    mirrored_sha256: str | None
    #: Why the mirror's last send failed, or, after a successful send, a note that is no
    #: failure: the untracked copy a lost create left in Bambuddy (`preset_mirror._mirrored`).
    mirror_error: str | None
    mirror_error_at: datetime | None
    created_at: datetime
    updated_at: datetime


_COLUMNS = ", ".join(FilamentPreset.model_fields)


def document_bytes(doc: dict[str, Any]) -> bytes:
    """A document as the store keeps it: JSON, indented as a profile fetch writes it."""
    return json.dumps(doc, indent=4, ensure_ascii=False).encode()


async def keep_document(blobs: BlobStore, body: bytes) -> tuple[str, str]:
    """``body`` kept as a blob; its key and sha256. Held by no row yet: the caller's
    write holds it (`create_on`, `version_on`) before the sweep's grace ends."""
    sha256 = hashlib.sha256(body).hexdigest()
    return await keep_file(blobs, io.BytesIO(body), sha256), sha256


def _hold(conn: Conn, key: str, preset_id: int, version: int) -> None:
    conn.execute(
        "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (key, HOLDER, f"{preset_id}:{version}"),
    )


def lock_presets_on(conn: Conn) -> None:
    """`LOCK` for ``conn``'s transaction: every write of a preset row takes it."""
    _lock(conn)


def _lock(conn: Conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK,))


def create_on(
    conn: Conn,
    *,
    name: str,
    source: Source,
    key: str,
    sha256: str,
    doc: dict[str, Any],
    made_by: str,
    type_id: UUID | None = None,
    printer_model: str | None = None,
    nozzle_diameter: str | None = None,
    tdfp_id: int | None = None,
    fetch_run_id: UUID | None = None,
    bb_preset_id: int | None = None,
    bb_instance: str | None = None,
    bb_updated_at: str | None = None,
    mirrored_sha256: str | None = None,
) -> int:
    """A new preset at version 1, its blob ``key`` held; its id. On ``conn``, in the
    caller's transaction."""
    _lock(conn)
    filament_type, colour, printers = facts(doc)
    row = conn.execute(
        "INSERT INTO filament_presets (name, source, type_id, printer_model, nozzle_diameter,"
        " tdfp_id, fetch_run_id, blob_key, sha256, filament_type, filament_colour,"
        " compatible_printers, bb_preset_id, bb_instance, bb_updated_at, mirrored_sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING id",
        (
            name,
            source,
            type_id,
            printer_model,
            nozzle_diameter,
            tdfp_id,
            fetch_run_id,
            key,
            sha256,
            filament_type,
            colour,
            printers,
            bb_preset_id,
            bb_instance,
            bb_updated_at,
            mirrored_sha256,
        ),
    ).fetchone()
    assert row is not None
    preset_id: int = row["id"]
    conn.execute(
        "INSERT INTO filament_preset_versions (preset_id, version, blob_key, sha256,"
        " fetch_run_id, bb_updated_at, made_by) VALUES (%s, 1, %s, %s, %s, %s, %s)",
        (preset_id, key, sha256, fetch_run_id, bb_updated_at, made_by),
    )
    _hold(conn, key, preset_id, 1)
    return preset_id


def version_on(
    conn: Conn,
    preset_id: int,
    *,
    name: str,
    key: str,
    sha256: str,
    doc: dict[str, Any],
    made_by: str,
    fetch_run_id: UUID | None = None,
    bb_updated_at: str | None = None,
) -> int | None:
    """``doc`` (kept as blob ``key``) as the preset's next version; that version, or
    ``None`` when the preset holds this document already (its ``bb_updated_at`` is still
    moved to the one given, so a refresh does not read it again). On ``conn``, in the
    caller's transaction, under the preset's row lock."""
    row = conn.execute(
        "SELECT version, sha256 FROM filament_presets WHERE id = %s FOR UPDATE", (preset_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no filament preset {preset_id}")
    if row["sha256"] == sha256:
        if bb_updated_at is not None:
            conn.execute(
                "UPDATE filament_presets SET bb_updated_at = %s WHERE id = %s",
                (bb_updated_at, preset_id),
            )
        return None
    version: int = row["version"] + 1
    filament_type, colour, printers = facts(doc)
    conn.execute(
        "UPDATE filament_presets SET name = %s, version = %s, blob_key = %s, sha256 = %s,"
        " filament_type = %s, filament_colour = %s, compatible_printers = %s,"
        " fetch_run_id = COALESCE(%s, fetch_run_id),"
        " bb_updated_at = COALESCE(%s, bb_updated_at), updated_at = now() WHERE id = %s",
        (
            name,
            version,
            key,
            sha256,
            filament_type,
            colour,
            printers,
            fetch_run_id,
            bb_updated_at,
            preset_id,
        ),
    )
    conn.execute(
        "INSERT INTO filament_preset_versions (preset_id, version, blob_key, sha256,"
        " fetch_run_id, bb_updated_at, made_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (preset_id, version, key, sha256, fetch_run_id, bb_updated_at, made_by),
    )
    _hold(conn, key, preset_id, version)
    return version


def rebind_on(conn: Conn, bb_preset_id: int, preset_id: int) -> int:
    """Every binding that names Bambuddy local preset ``bb_preset_id`` names ScadBuddy
    preset ``preset_id`` instead; how many rows changed. The name each row shows stays."""
    changed = 0
    for table in BINDINGS:
        changed += conn.execute(
            f"UPDATE {table} SET slicer_filament = %s WHERE slicer_filament = %s",
            (sb_ref(preset_id), str(bb_preset_id)),
        ).rowcount
    return changed


def by_bb_id_on(
    conn: Conn, bb_preset_ids: Collection[int], instance: str | None = None
) -> dict[int, int]:
    """The ScadBuddy preset that is each of ``bb_preset_ids`` (`FilamentPreset.bb_preset_id`),
    by Bambuddy id; one with none is left out. With ``instance``, only a preset of that
    Bambuddy (or of an unrecorded one) counts: an id of another Bambuddy names another
    preset."""
    if not bb_preset_ids:
        return {}
    rows = conn.execute(
        "SELECT id, bb_preset_id FROM filament_presets WHERE bb_preset_id = ANY(%s)"
        " AND (%s::text IS NULL OR bb_instance IS NULL OR bb_instance = %s)",
        (sorted(set(bb_preset_ids)), instance, instance),
    ).fetchall()
    return {row["bb_preset_id"]: row["id"] for row in rows}


def record_instance_on(conn: Conn, instance: str | None) -> None:
    """A preset's Bambuddy id is a Bambuddy link (#7176, `bambuddy_pull.ANY_LINK`): when the
    filament sync's state row records no Bambuddy yet, ``instance``, the one the id was
    written for, becomes the links' (Decision 12). So after a URL change the mirror waits
    and a person can reset the links (`bb_sync_results.reset_links`). Run in the
    transaction that wrote the id; ``None`` records nothing."""
    if instance is not None:
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = %s WHERE instance IS NULL", (instance,)
        )


class FilamentPresetStore:
    """``filament_presets`` and their documents. ``blobs`` is asked at each read, as the
    app's blob store is set after its components are built. Reads in a thread."""

    def __init__(self, pool: ConnectionPool[Conn], blobs: Callable[[], BlobStore]) -> None:
        self._pool = pool
        self._blobs = blobs

    def get(self, preset_id: int) -> FilamentPreset | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_presets WHERE id = %s", (preset_id,)
            ).fetchone()
        return None if row is None else FilamentPreset.model_validate(row)

    def fetched(self, type_id: UUID, printer_model: str, nozzle: str) -> FilamentPreset | None:
        """The type's ``3dfp`` preset of one key (one per key, Decision 3)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM filament_presets WHERE source = '3dfp'"
                " AND type_id = %s AND printer_model = %s AND nozzle_diameter = %s",
                (type_id, printer_model, nozzle),
            ).fetchone()
        return None if row is None else FilamentPreset.model_validate(row)

    def all(self) -> list[FilamentPreset]:
        with self._pool.connection() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM filament_presets ORDER BY id").fetchall()
        return [FilamentPreset.model_validate(row) for row in rows]

    async def rows(self) -> list[OwnRow]:
        return [
            OwnRow(
                id=p.id,
                name=p.name,
                sha256=p.sha256,
                filament_type=p.filament_type,
                filament_colour=p.filament_colour,
                compatible_printers=list(p.compatible_printers),
                bb_preset_id=p.bb_preset_id,
            )
            for p in await asyncio.to_thread(self.all)
        ]

    async def documents(self, ids: Collection[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        wanted = sorted(set(ids))

        def keys() -> dict[int, str]:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT id, blob_key FROM filament_presets WHERE id = ANY(%s)", (wanted,)
                ).fetchall()
            return {row["id"]: row["blob_key"] for row in rows}

        out: dict[int, dict[str, Any]] = {}
        for preset_id, key in (await asyncio.to_thread(keys)).items():
            doc = await self.read(key)
            if doc is not None:
                out[preset_id] = doc
        return out

    async def read(self, key: str) -> dict[str, Any] | None:
        """The document kept as blob ``key``, without print-host credential keys;
        ``None`` when the store lost it (logged)."""
        data = await read_blob(self._blobs(), key)
        if data is None:
            logger.warning("a filament preset's blob is missing", extra={"key": key})
            return None
        doc = json.loads(data)
        return strip_credentials(doc) if isinstance(doc, dict) else {}


# -- adoption and the refresh (the pull's side, plan Decisions 5 to 7) -------------------


def _bound_bb_ids(conn: Conn) -> set[int]:
    """The Bambuddy local preset ids the binding tables still name."""
    ids: set[int] = set()
    for table in BINDINGS:
        for row in conn.execute(
            f"SELECT DISTINCT slicer_filament FROM {table}"
            " WHERE slicer_filament ~ '^[0-9]{1,18}$'"
        ).fetchall():
            ids.add(int(row["slicer_filament"]))
    return ids


def _fetched_key(conn: Conn, bb_preset_id: int) -> dict[str, Any] | None:
    """The type binding that a profile fetch of that type bound to ``bb_preset_id`` at
    the binding's own key, with the run, when the key has no ``3dfp`` preset yet: what
    makes an adopted preset ``3dfp`` (Decision 6)."""
    return conn.execute(
        "SELECT b.type_id, b.printer_model, b.nozzle_diameter, f.id AS run_id, f.tdfp_id"
        " FROM filament_spool_type_presets b"
        " JOIN filament_profile_fetches f ON f.type_id = b.type_id,"
        " jsonb_each(f.files) AS m(model, nozzles), jsonb_each(m.nozzles) AS n(nozzle, entry)"
        " WHERE b.slicer_filament = %s AND m.model = b.printer_model"
        " AND n.nozzle = b.nozzle_diameter AND n.entry ->> 'preset_id' = b.slicer_filament"
        " AND NOT EXISTS (SELECT 1 FROM filament_presets p WHERE p.source = '3dfp'"
        " AND p.type_id = b.type_id AND p.printer_model = b.printer_model"
        " AND p.nozzle_diameter = b.nozzle_diameter)"
        " ORDER BY f.started_at DESC, f.id LIMIT 1",
        (str(bb_preset_id),),
    ).fetchone()


def _adopt_one(
    pool: ConnectionPool[Conn],
    detail: LocalPresetDetail,
    doc: dict[str, Any],
    key: str,
    sha256: str,
    instance: str | None = None,
) -> bool:
    """Kept preset ``detail`` (its document ``doc``, kept as blob ``key``) made a
    ScadBuddy preset, and its bindings moved to it; false when another adoption made it
    first, whose preset the bindings then name."""
    with pool.connection() as conn, conn.transaction():
        _lock(conn)
        again = by_bb_id_on(conn, [detail.id], instance)
        if again:
            rebind_on(conn, detail.id, again[detail.id])
            # Blob ``key`` stays unheld on purpose: the blob sweep reclaims it once its
            # grace ends (`keep_document`).
            return False
        fetched = _fetched_key(conn, detail.id)
        preset_id = create_on(
            conn,
            name=detail.name,
            source="3dfp" if fetched is not None else "bambuddy",
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="adopt",
            type_id=None if fetched is None else fetched["type_id"],
            printer_model=None if fetched is None else fetched["printer_model"],
            nozzle_diameter=None if fetched is None else fetched["nozzle_diameter"],
            tdfp_id=None if fetched is None else fetched["tdfp_id"],
            fetch_run_id=None if fetched is None else fetched["run_id"],
            bb_preset_id=detail.id,
            bb_instance=instance,
            bb_updated_at=detail.updated_at,
            # The document came from Bambuddy: its mirror holds it already.
            mirrored_sha256=sha256,
        )
        record_instance_on(conn, instance)
        rebind_on(conn, detail.id, preset_id)
        return True


def _take_version(
    pool: ConnectionPool[Conn],
    preset_id: int,
    detail: LocalPresetDetail,
    doc: dict[str, Any],
    key: str,
    sha256: str,
) -> bool:
    """Bambuddy's edit of ``bambuddy`` preset ``preset_id`` as its next version; whether
    the document changed."""
    with pool.connection() as conn, conn.transaction():
        version = version_on(
            conn,
            preset_id,
            name=detail.name,
            key=key,
            sha256=sha256,
            doc=doc,
            made_by="bambuddy",
            bb_updated_at=detail.updated_at,
        )
        if version is not None:
            # Bambuddy holds this document: it is its own.
            conn.execute(
                "UPDATE filament_presets SET mirrored_sha256 = %s WHERE id = %s",
                (sha256, preset_id),
            )
        return version is not None


async def adopt_held(
    pool: ConnectionPool[Conn],
    blobs: BlobStore,
    held: PresetSource,
    instance: str | None = None,
) -> int:
    """Every binding that names a Bambuddy local preset ``held`` holds as a filament
    preset names a ScadBuddy preset instead (Decision 6); how many presets it made. One
    whose preset ``held`` lacks keeps its Bambuddy id, which the native slicer refuses
    (#7244). A preset already made for that id is reused."""

    def bound() -> tuple[set[int], dict[int, int]]:
        with pool.connection() as conn:
            ids = _bound_bb_ids(conn)
            return ids, by_bb_id_on(conn, ids, instance)

    ids, known = await asyncio.to_thread(bound)
    if not ids:
        return 0
    if known:

        def rebind_known() -> None:
            with pool.connection() as conn, conn.transaction():
                for bb_id, preset_id in known.items():
                    rebind_on(conn, bb_id, preset_id)

        await asyncio.to_thread(rebind_known)
    wanted = ids - known.keys()
    docs = await held.documents(wanted)
    made = 0
    for bb_id in sorted(wanted):
        detail = docs.get(bb_id)
        if detail is None or detail.preset_type != "filament" or not detail.setting:
            continue
        doc = dict(detail.setting)
        key, sha256 = await keep_document(blobs, document_bytes(doc))

        try:
            made += await asyncio.to_thread(_adopt_one, pool, detail, doc, key, sha256, instance)
        except Exception as error:
            # One preset ScadBuddy cannot hold (a name past the column's CHECK) never stops
            # the ids after it; its bindings keep the Bambuddy id, and the next refresh
            # tries it again.
            logger.warning(
                "a Bambuddy local preset was not adopted",
                extra={"bb_preset_id": bb_id, "error": type(error).__name__},
            )
    if made:
        logger.info("adopted Bambuddy local presets as ScadBuddy presets", extra={"count": made})
    return made


async def refresh_bambuddy(
    pool: ConnectionPool[Conn],
    blobs: BlobStore,
    held: PresetSource,
    instance: str | None = None,
) -> int:
    """A new version of each ``bambuddy`` preset whose Bambuddy ``updated_at`` in
    ``held`` moved since its document was taken (Decision 4); how many changed. A preset
    gone from Bambuddy stays as it was: ScadBuddy holds it now. With ``instance``, only
    a preset of that Bambuddy (or of an unrecorded one) is refreshed: ``held`` is the
    configured Bambuddy's, whose same id is another preset after a URL change."""

    def ours() -> list[dict[str, Any]]:
        with pool.connection() as conn:
            return conn.execute(
                "SELECT id, bb_preset_id, bb_updated_at, sha256 FROM filament_presets"
                " WHERE source = 'bambuddy' AND bb_preset_id IS NOT NULL"
                " AND (%(instance)s::text IS NULL OR bb_instance IS NULL"
                " OR bb_instance = %(instance)s)",
                {"instance": instance},
            ).fetchall()

    presets = await asyncio.to_thread(ours)
    if not presets:
        return 0
    stamps = await held.stamps()
    moved = [
        p
        for p in presets
        if p["bb_preset_id"] in stamps
        and (stamps[p["bb_preset_id"]] is None or stamps[p["bb_preset_id"]] != p["bb_updated_at"])
    ]
    docs = await held.documents([p["bb_preset_id"] for p in moved])
    changed = 0
    for p in moved:
        detail = docs.get(p["bb_preset_id"])
        if detail is None or not detail.setting:
            continue
        doc = dict(detail.setting)
        body = document_bytes(doc)
        if detail.updated_at is None and hashlib.sha256(body).hexdigest() == p["sha256"]:
            # With no stamp, every refresh reads it: an unchanged document writes no blob.
            continue
        key, sha256 = await keep_document(blobs, body)

        changed += await asyncio.to_thread(_take_version, pool, p["id"], detail, doc, key, sha256)
    return changed


class PresetImport(BaseModel):
    """What one import of Bambuddy's presets did (`import_presets`)."""

    #: Bambuddy local presets made ScadBuddy presets (`adopt_held`).
    adopted: int
    #: ``bambuddy`` presets that took a new version (`refresh_bambuddy`).
    updated: int
    #: Bambuddy local ids that bindings still name after the import (`bound_bb_ids`), for
    #: any cause: for example an id Bambuddy no longer holds, a detail that is no filament
    #: preset, or a preset whose adopt failed (#7244 review, #7435).
    unresolved: int = 0


async def import_presets(
    pool: ConnectionPool[Conn],
    blobs: BlobStore,
    held: PresetSource,
    instance: str | None = None,
) -> PresetImport:
    """`refresh_bambuddy`, then `adopt_held`, from ``held`` (the module's docstring says
    who calls it), their presets marked as ``instance``'s, the Bambuddy ``held`` reads.
    Each step runs even when the other failed; a failure is logged by its type and raised
    once both ran."""
    counts: dict[str, int] = {"updated": 0, "adopted": 0}
    failed: Exception | None = None
    for name, step in (("updated", refresh_bambuddy), ("adopted", adopt_held)):
        try:
            counts[name] = await step(pool, blobs, held, instance)
        except Exception as error:
            logger.warning(
                "a step of the Bambuddy preset import failed",
                extra={"step": step.__name__, "error": type(error).__name__},
            )
            failed = failed or error
    if failed is not None:
        raise failed
    left = await asyncio.to_thread(bound_bb_ids, pool)
    return PresetImport(**counts, unresolved=len(left))


async def import_from_bambuddy(
    pool: ConnectionPool[Conn], blobs: BlobStore, settings: StoredSettings
) -> PresetImport:
    """`import_presets` from the configured Bambuddy, read now (`LivePresets`)."""
    async with client_for(settings) as client:
        instance = instance_for(settings.bambuddy_url) if settings.bambuddy_url else None
        return await import_presets(pool, blobs, LivePresets(client), instance)


def bound_bb_ids(pool: ConnectionPool[Conn]) -> set[int]:
    """The Bambuddy local ids a spool or type binding still names."""
    with pool.connection() as conn:
        return _bound_bb_ids(conn)


#: How long the start's adoption waits before it tries again after a failure.
ADOPT_RETRY_S: Final = 300.0


def links_elsewhere_on(pool: ConnectionPool[Conn], configured: str) -> bool:
    """Whether the Bambuddy links' recorded instance is another than ``configured``
    (#6845 R-6845-5, Decision 12): `bambuddy_pull.links_elsewhere` on a pool."""
    with pool.connection() as conn:
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    return state is not None and state["instance"] not in (None, configured)


#: The session advisory lock one replica holds while it runs `adopt_at_start`'s step, so
#: the replicas of one start do not each read Bambuddy's presets (#7244 review).
ADOPT_LOCK = "scadbuddy:filament:adopt_at_start"


async def adopt_at_start(
    pool: ConnectionPool[Conn],
    blobs: Callable[[], BlobStore],
    load: Callable[[], StoredSettings],
    retry_s: float = ADOPT_RETRY_S,
) -> None:
    """The API's start step (`slicing/component.py`): while a binding names a Bambuddy local
    id, `adopt_held` from the configured Bambuddy, whatever ``filament_sync.presets`` says
    (#7244: the bindings made before ScadBuddy stopped reading Bambuddy's local presets
    become ``sb-`` refs). Nothing bound, no Bambuddy call; links recorded for another
    Bambuddy than the configured one, no call either (`links_elsewhere_on`), since a local id
    means something only on the Bambuddy that issued it. A failed try is logged and tried
    again every ``retry_s`` until one ends; ids Bambuddy no longer holds are logged and left,
    each an error the print dialog shows. One replica does it: each try takes the session
    try-lock `ADOPT_LOCK`; a replica that finds it held tries again every ``retry_s`` while
    bindings still name Bambuddy ids, so it takes over from a holder that ended first.
    Never raises."""
    while True:
        held = None
        try:
            ids = await asyncio.to_thread(bound_bb_ids, pool)
            if not ids:
                return
            conninfo = pool.conninfo if isinstance(pool.conninfo, str) else pool.conninfo()
            held = await acquire_lock(conninfo, ADOPT_LOCK)
            if held is None:
                # Another replica adopts; try again later, so a holder that ends before it
                # is done leaves the step to this one (#7244 review).
                logger.info("another replica adopts the bound Bambuddy local presets")
                await asyncio.sleep(retry_s)
                continue
            settings = await asyncio.to_thread(load)
            if not settings.bambuddy_url:
                logger.warning(
                    "bindings name Bambuddy local presets and no Bambuddy is configured;"
                    " the native slicer refuses them",
                    extra={"count": len(ids)},
                )
                return
            if await asyncio.to_thread(
                links_elsewhere_on, pool, instance_for(settings.bambuddy_url)
            ):
                # A Bambuddy local id means something only on the Bambuddy that issued it:
                # another one's preset of the same id is another filament (#7244 review).
                logger.warning(
                    "bindings name Bambuddy local presets of another Bambuddy than the"
                    " configured one; left, and the native slicer refuses them until the"
                    " links are reset and the presets imported",
                    extra={"count": len(ids)},
                )
                return
            async with client_for(settings) as client:
                made = await adopt_held(
                    pool, blobs(), LivePresets(client), instance_for(settings.bambuddy_url)
                )
            left = await asyncio.to_thread(bound_bb_ids, pool)
            logger.info(
                "adopted the Bambuddy local presets bindings named",
                extra={"made": made, "left": len(left)},
            )
            if left:
                logger.warning(
                    "bindings still name Bambuddy local presets Bambuddy does not hold;"
                    " the native slicer refuses them",
                    extra={"bb_preset_ids": sorted(left)},
                )
            return
        except Exception as error:
            logger.warning(
                "could not adopt the bound Bambuddy local presets; trying again later",
                extra={"error": type(error).__name__},
            )
        finally:
            if held is not None:
                await asyncio.to_thread(held.close)
        await asyncio.sleep(retry_s)


def bambuddy_refs_on(conn: Conn, refs: Collection[str]) -> dict[str, str | None]:
    """The ref Bambuddy knows for each ``sb-`` ref among ``refs``: the preset's Bambuddy
    id as text, or ``None`` when it has none (no mirror) or its id is of another Bambuddy
    than the one the filament sync's links name (`filament_bb_sync_state`, which a reset of
    the links moves, #6773). Other refs are left out."""
    ids = {i for ref in refs if (i := sb_id(ref)) is not None}
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT p.id, CASE WHEN p.bb_instance IS NULL OR s.instance IS NULL"
        " OR p.bb_instance = s.instance THEN p.bb_preset_id END AS bb_preset_id"
        " FROM filament_presets p"
        " LEFT JOIN (SELECT instance FROM filament_bb_sync_state LIMIT 1) s ON true"
        " WHERE p.id = ANY(%s)",
        (sorted(ids),),
    ).fetchall()
    found = {row["id"]: row["bb_preset_id"] for row in rows}
    return {
        ref: (None if found.get(i) is None else str(found[i]))
        for ref in refs
        if (i := sb_id(ref)) is not None
    }
