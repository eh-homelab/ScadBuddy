"""A spool's slicer presets and K values over its type's (#6262, spec §5.3): the spool's
own row for a key replaces the type's row of that key, and a key only the spool has is
kept. A key is a printer model and nozzle diameter for a preset, and a printer, extruder
and nozzle diameter for a K value. A preset row for any printer or nozzle (``*``) is
refused: the 0706Z migration made each one a ``migrated`` row per model and nozzle, as the
native API's write (`spools/native.py`) does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol, get_args
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.models import NozzleSize
from scadbuddy.filament import models as m

Conn = Connection[DictRow]

_PRESET_COLUMNS = ", ".join(m.SpoolPreset.model_fields)
_K_COLUMNS = ", ".join(m.SpoolKValue.model_fields)
_TYPE_PRESET_COLUMNS = ", ".join(m.TypePreset.model_fields)
_TYPE_K_COLUMNS = ", ".join(m.TypeKValue.model_fields)

#: The nozzle diameters a row for any nozzle becomes: the resolver's sizes.
NOZZLES: tuple[str, ...] = get_args(NozzleSize)


def drop_migrated(conn: Conn, spool_id: UUID) -> list[list[dict[str, Any]]] | None:
    """Delete the spool's ``migrated`` preset rows, so its type's presets show: what a
    spool that needed a type does when it is first given one (#6262 F27). The spool's own
    rows ``[before, after]``, for its ``edited`` event; ``None`` when it had none."""
    before = own_presets(conn, spool_id)
    gone = conn.execute(
        "DELETE FROM filament_spool_presets WHERE spool_id = %s AND migrated", (spool_id,)
    ).rowcount
    return [before, own_presets(conn, spool_id)] if gone else None


def own_presets(conn: Conn, spool_id: UUID) -> list[dict[str, Any]]:
    """The spool's own preset rows, by key."""
    return conn.execute(
        f"SELECT {_PRESET_COLUMNS} FROM filament_spool_presets WHERE spool_id = %s"
        " ORDER BY printer_model, nozzle_diameter",
        (spool_id,),
    ).fetchall()


def own_k_values(conn: Conn, spool_id: UUID) -> list[dict[str, Any]]:
    """The spool's own K-value rows, by key."""
    return conn.execute(
        f"SELECT {_K_COLUMNS} FROM filament_spool_k_values WHERE spool_id = %s"
        " ORDER BY printer_id, extruder, nozzle_diameter",
        (spool_id,),
    ).fetchall()


def _type_ids(conn: Conn, spool_ids: list[UUID]) -> dict[UUID, UUID | None]:
    rows = conn.execute(
        "SELECT id, type_id FROM filament_spools WHERE id = ANY(%s)", (spool_ids,)
    ).fetchall()
    return {row["id"]: row["type_id"] for row in rows}


def _type_rows(conn: Conn, sql: str, type_ids: Iterable[UUID | None]) -> dict[UUID, list[Any]]:
    """The rows ``sql`` (which takes the type ids as one array) gives, by ``type_id``."""
    wanted = list({t for t in type_ids if t is not None})
    found: dict[UUID, list[Any]] = {}
    if wanted:
        for row in conn.execute(sql, (wanted,)).fetchall():
            found.setdefault(row["type_id"], []).append(row)
    return found


def effective_presets(conn: Conn, spool_id: UUID) -> list[m.EffectivePreset]:
    """The type's presets, each replaced by the spool's row of the same key, then the
    spool's rows of keys the type lacks; by printer model and nozzle diameter."""
    return effective_presets_of(conn, [spool_id])[spool_id]


def effective_presets_of(
    conn: Conn, spool_ids: Iterable[UUID]
) -> dict[UUID, list[m.EffectivePreset]]:
    """`effective_presets` of each of ``spool_ids``, in three queries whatever their
    number (#6902); a spool that does not exist has none."""
    ids = list(dict.fromkeys(spool_ids))
    types = _type_ids(conn, ids)
    by_type = _type_rows(
        conn,
        f"SELECT {_TYPE_PRESET_COLUMNS} FROM filament_spool_type_presets WHERE type_id = ANY(%s)",
        types.values(),
    )
    own: dict[UUID, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"SELECT {_PRESET_COLUMNS}, migrated FROM filament_spool_presets WHERE spool_id = ANY(%s)",
        (ids,),
    ).fetchall():
        own.setdefault(row["spool_id"], []).append(row)
    out: dict[UUID, list[m.EffectivePreset]] = {}
    for spool_id in ids:
        type_id = types.get(spool_id)
        rows: dict[tuple[str, str], tuple[dict[str, Any], bool]] = {}
        for row in [] if type_id is None else by_type.get(type_id, []):
            rows[row["printer_model"], row["nozzle_diameter"]] = (row, False)
        for row in own.get(spool_id, []):
            rows[row["printer_model"], row["nozzle_diameter"]] = (row, True)
        out[spool_id] = [
            m.EffectivePreset(
                printer_model=row["printer_model"],
                nozzle_diameter=row["nozzle_diameter"],
                slicer_filament=row["slicer_filament"],
                slicer_filament_name=row["slicer_filament_name"],
                overridden=is_own,
                migrated=is_own and bool(row["migrated"]),
            )
            for _, (row, is_own) in sorted(rows.items())
        ]
    return out


def effective_k_values(conn: Conn, spool_id: UUID) -> list[m.EffectiveKValue]:
    """The type's K values, each replaced by the spool's row of the same key, then the
    spool's rows of keys the type lacks; by printer, extruder and nozzle diameter."""
    return effective_k_values_of(conn, [spool_id])[spool_id]


def effective_k_values_of(
    conn: Conn, spool_ids: Iterable[UUID]
) -> dict[UUID, list[m.EffectiveKValue]]:
    """`effective_k_values` of each of ``spool_ids``, in three queries whatever their
    number (#6902); a spool that does not exist has none."""
    ids = list(dict.fromkeys(spool_ids))
    types = _type_ids(conn, ids)
    by_type = _type_rows(
        conn,
        f"SELECT {_TYPE_K_COLUMNS} FROM filament_spool_type_k_values WHERE type_id = ANY(%s)",
        types.values(),
    )
    own: dict[UUID, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"SELECT {_K_COLUMNS} FROM filament_spool_k_values WHERE spool_id = ANY(%s)", (ids,)
    ).fetchall():
        own.setdefault(row["spool_id"], []).append(row)
    out: dict[UUID, list[m.EffectiveKValue]] = {}
    for spool_id in ids:
        type_id = types.get(spool_id)
        rows: dict[tuple[str, int, str], tuple[dict[str, Any], bool]] = {}
        for row in [] if type_id is None else by_type.get(type_id, []):
            rows[str(row["printer_id"]), row["extruder"], row["nozzle_diameter"]] = (row, False)
        for row in own.get(spool_id, []):
            rows[str(row["printer_id"]), row["extruder"], row["nozzle_diameter"]] = (row, True)
        out[spool_id] = [
            m.EffectiveKValue(
                printer_id=row["printer_id"],
                extruder=row["extruder"],
                nozzle_diameter=row["nozzle_diameter"],
                nozzle_type=row["nozzle_type"],
                k_value=row["k_value"],
                name=row["name"],
                cali_idx=row["cali_idx"],
                setting_id=row["setting_id"],
                overridden=is_own,
            )
            for _, (row, is_own) in sorted(rows.items())
        ]
    return out


def preset_for(
    presets: Sequence[m.EffectivePreset], printer_model: str, nozzle_diameter: str
) -> m.EffectivePreset | None:
    """The preset of exactly this printer model and nozzle diameter; ``None`` when there
    is none. A near key (0.4 for 0.2) is never taken."""
    for preset in presets:
        if preset.printer_model == printer_model and preset.nozzle_diameter == nozzle_diameter:
            return preset
    return None


def presets_by_number(conn: Conn, numbers: Sequence[int]) -> dict[int, list[m.EffectivePreset]]:
    """`effective_presets` of each SB spool among ``numbers``, by its number; a number no
    spool has is left out."""
    rows = conn.execute(
        "SELECT id, number FROM filament_spools WHERE number = ANY(%s)", (list(numbers),)
    ).fetchall()
    return {row["number"]: effective_presets(conn, row["id"]) for row in rows}


def bb_spools_by_number(conn: Conn, numbers: Sequence[int]) -> dict[int, int]:
    """The Bambuddy spool each SB spool among ``numbers`` is linked to
    (`filament_bb_links`), by its number; an unlinked one is left out."""
    rows = conn.execute(
        "SELECT s.number, l.bb_spool_id FROM filament_spools s"
        " JOIN filament_bb_links l ON l.spool_id = s.id WHERE s.number = ANY(%s)",
        (list(numbers),),
    ).fetchall()
    return {row["number"]: row["bb_spool_id"] for row in rows}


class SpoolPresetSource(Protocol):
    """What a print's plan reads of SB spools' presets, by spool number (#6262 F1,
    `bambuddy/print_run.py` `resolve_plates`): the API's from Postgres
    (:class:`PoolSpoolPresets`); the print worker's over its CRUD routes (Task 6)."""

    async def presets(self, numbers: Sequence[int]) -> dict[int, list[m.EffectivePreset]]: ...

    async def bb_spools(self, numbers: Sequence[int]) -> dict[int, int]: ...


class PoolSpoolPresets:
    """:class:`SpoolPresetSource` over a pool whose connections autocommit and return dict
    rows (the settings store's): each read in a thread."""

    def __init__(self, pool: ConnectionPool[Conn]) -> None:
        self._pool = pool

    async def presets(self, numbers: Sequence[int]) -> dict[int, list[m.EffectivePreset]]:
        return await asyncio.to_thread(self._read, presets_by_number, numbers)

    async def bb_spools(self, numbers: Sequence[int]) -> dict[int, int]:
        return await asyncio.to_thread(self._read, bb_spools_by_number, numbers)

    def _read[T](self, read: Callable[[Conn, Sequence[int]], T], numbers: Sequence[int]) -> T:
        with self._pool.connection() as conn:
            return read(conn, numbers)
