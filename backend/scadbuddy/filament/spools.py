"""Spools and their tags (#6261, epic #6260, spec §5.3-5.4): the store over
`filament_spools`, `filament_tags` and `filament_tag_bindings`. Every change writes its
ledger event (`filament/ledger.py`) in the same transaction.

A spool reads back with its effective values (`types.effective`): its own override, else
its type's, else the subtype's and material's print defaults. A spool with prints in its
ledger is never deleted (`SpoolInUseError`); it is archived.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import JsonValue

from scadbuddy.core.settings import Settings
from scadbuddy.core.tracing import traced_transaction
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.required import checks_save, mark_incomplete, save_properties_on
from scadbuddy.filament import dry_hours, ledger, metrics
from scadbuddy.filament import models as m
from scadbuddy.filament import presets as p
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.empty_weight import EmptyWeight, effective_on
from scadbuddy.filament.insert_weights import check_on
from scadbuddy.filament.invoice_money import price_per_kg, unit_cost
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.rfid_push import enqueue_tag, link_origin
from scadbuddy.filament.types import (
    PresetUnknownError,
    TypeRowsDuplicateError,
    TypeTempError,
    effective,
    names_preset,
    overridden,
)
from scadbuddy.orders.models import Actor
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

Conn = Connection[DictRow]
Row = Mapping[str, Any]
_Spools = list[m.Spool]
_Events = list[m.SpoolEvent]
_TagRows = list[m.TagRow]
_Presets = list[m.EffectivePreset]
_KValues = list[m.EffectiveKValue]
_PresetRows = list[m.SpoolPresetCreate]
_KRows = list[m.SpoolKValueCreate]
_Ids = list[UUID]


#: A preset row's key and values, as a ``migrated`` row is matched by them.
_PresetValues = tuple[str, Any, str, str | None]
_MIGRATED_ROWS = (
    "SELECT printer_model, nozzle_diameter, slicer_filament, slicer_filament_name"
    " FROM filament_spool_presets WHERE spool_id = %s AND migrated"
)


def _preset_values(rows: list[DictRow]) -> set[_PresetValues]:
    """The key and values of each preset row; a row of NULLs (a LEFT JOIN's miss) is none."""
    return {
        (r["printer_model"], r["nozzle_diameter"], r["slicer_filament"], r["slicer_filament_name"])
        for r in rows
        if r["printer_model"] is not None
    }


def _migrated_rows(conn: Conn, spool_id: UUID) -> set[_PresetValues]:
    """The key and values of each of the spool's ``migrated`` preset rows."""
    return _preset_values(conn.execute(_MIGRATED_ROWS, (spool_id,)).fetchall())


#: The columns of a spool row: every field of `Spool` but those it is given here.
_SPOOL_FIELDS = tuple(
    f
    for f in m.Spool.model_fields
    if f
    not in {
        "effective",
        "overridden",
        "names",
        "low_stock",
        "hours_outside_dry",
        "needs_drying",
        "drift_notice",
        *m.COMPLETENESS,
    }
)
_SPOOL_COLUMNS = ", ".join(_SPOOL_FIELDS)
_TYPE_COLUMNS = ", ".join(f for f in m.SpoolType.model_fields if f not in m.COMPLETENESS)
_TAG_COLUMNS = ", ".join(m.Tag.model_fields)
_BINDING_COLUMNS = ", ".join(m.TagBinding.model_fields)
_TAG_COLUMNS_T = ", ".join(f"t.{f}" for f in m.Tag.model_fields)
#: A binding's columns, prefixed so they cannot clash with the tag's (``id``).
_BINDING_COLUMNS_B = ", ".join(f"b.{f} AS binding_{f}" for f in m.TagBinding.model_fields)
#: The events that keep a spool from being deleted: it printed.
_USED: tuple[m.EventKind, ...] = ("print_started", "print_ended")
#: What a create or a patch may write to a column of its own.
_WRITABLE = frozenset(m.SpoolPatch.model_fields) - {"archived", "properties"}


class SpoolInUseError(ValueError):
    """The spool has prints in its ledger: archive it instead."""

    def __init__(self) -> None:
        super().__init__("the spool has prints in its ledger; archive it instead")


class SpoolBambuddyLinkedError(ValueError):
    """The spool is linked to a Bambuddy spool: archive it instead."""

    def __init__(self) -> None:
        super().__init__(
            "the spool is linked to Bambuddy and the next pull would bring it back;"
            " archive it instead"
        )


class SpoolBambuddyCreatingError(SpoolBambuddyLinkedError):
    """The sync's create of the spool in Bambuddy is open (#6265 review M9): its intent
    exists, with or without an error row. A refusal, a timeout and a lost answer each
    leave an error row, so an error row does not prove that Bambuddy made no row; a row
    it made would come back from the pull as a new spool."""

    def __init__(self) -> None:
        ValueError.__init__(
            self,
            "the spool is being created in Bambuddy, which may already hold it, and the"
            " next pull would bring it back; archive it instead",
        )


class NoEmptyWeightError(ValueError):
    """A weighing sent as a reading alone on a spool with no empty weight known (#7682)."""

    def __init__(self) -> None:
        super().__init__("the spool has no empty weight to take off the reading")


class ReadingBelowEmptyError(ValueError):
    """A weighing sent as a reading alone that is under the spool's empty weight (#7682):
    the filament would be negative."""

    def __init__(self) -> None:
        super().__init__("the reading is below the spool's empty weight")


class NoBelowLabelError(ValueError):
    """A weighing sent with a ``resolution`` the insert weight check does not offer now
    (#7683): ``reason`` is the check's answer (``below_label`` when it offered others)."""

    def __init__(self, reason: m.NoOfferReason | None) -> None:
        super().__init__(f"no reading under the label weight to resolve: {reason}")
        self.reason: m.NoOfferReason | None = reason


class SpoolTypelessError(ValueError):
    """A spool would name neither a type nor a material."""

    def __init__(self) -> None:
        super().__init__("a spool needs a type_id or a material_id")


class TagUnnamedError(ValueError):
    """Neither key of the tag is left once cleaned (none, or all zeros)."""

    def __init__(self) -> None:
        super().__init__("the tag has no tag_uid or tray_uuid once cleaned")


class NoSpoolError(Exception):
    """The spool a tag is bound to does not exist; raised to roll back the bind."""


class ResetBeforeLinkError(ValueError):
    """A ``consumed_reset`` dated at or before the spool's Bambuddy link (#7149
    R-7149-12): the sync never sends such a reset (R-7149-11), so SB's counter and
    Bambuddy's would disagree."""

    def __init__(self, linked_at: datetime) -> None:
        super().__init__(
            "the reset predates the spool's Bambuddy link"
            f" (linked at {linked_at.isoformat()}); date it after the link"
        )
        self.linked_at = linked_at


class OverrideKindError(ValueError):
    """An override binding on a tag that is not a Bambu MIFARE tag: only Bambuddy's reader
    of those can be told to ignore them."""

    def __init__(self) -> None:
        super().__init__("only a bambu_mifare tag can be an override")


class SpoolCopySourceError(ValueError):
    """A bulk create's ``copy_of`` names no spool (#7150, R-7150-13)."""

    def __init__(self) -> None:
        super().__init__("copy_of names no spool")


class ContinuedFromNotArchivedError(ValueError):
    """``continued_from`` names a spool that is not archived (#7684)."""

    def __init__(self) -> None:
        super().__init__("continued_from names a spool that is not archived")


class SpoolBulkRefusedError(ValueError):
    """A bulk edit that one spool refuses (#7151, R-7151-1): the whole edit is refused,
    naming that spool by its number."""

    def __init__(self, number: int, cause: ValueError) -> None:
        super().__init__(f"spool #{number}: {cause}")


class SpoolTrayPlaceError(ValueError):
    """A new spool's place is a printer's tray (#7150, R-7150-2): a tray holds one spool,
    and the tray's own link (`PUT /printers/{id}/trays/{ams}/{tray}/spool`) puts it there."""

    def __init__(self) -> None:
        super().__init__(
            "a new spool cannot go straight into a printer's tray; make it, then link it to"
            " the tray"
        )


class TagKeysClashError(ValueError):
    """The tag's UID and its tray UUID name two different tags."""

    def __init__(self) -> None:
        super().__init__("the tag_uid and the tray_uuid name two different tags")


def _check_temps(values: m.EffectiveValues) -> None:
    for kind in ("nozzle", "bed"):
        low, high = getattr(values, f"{kind}_temp_min"), getattr(values, f"{kind}_temp_max")
        if low is not None and high is not None and low > high:
            raise TypeTempError(f"{kind}_temp_min is above {kind}_temp_max")


def _by_id(conn: Conn, table: str, columns: str, ids: Iterable[UUID | None]) -> dict[UUID, Row]:
    wanted = list({i for i in ids if i is not None})
    if not wanted:
        return {}
    rows = conn.execute(f"SELECT {columns} FROM {table} WHERE id = ANY(%s)", (wanted,)).fetchall()
    return {row["id"]: row for row in rows}


@dataclass(frozen=True)
class FilamentDefaults:
    """The Filament settings a spool read leans on, as one read of them: the percentage
    under which a spool with none of its own is low (``filament_low_stock_pct``), the cost
    per kg of a spool and type that name none (``filament_default_cost_per_kg``), and the
    drying temperature and hours when nothing in the catalog sets them
    (``filament_drying_temp_c``, ``filament_drying_hours``, #7156 R-7156-5), the drift
    at or over which a weighing is a notice (``filament_weighing_drift_notice_g``, #7682),
    and the most filament a finished spool has on it
    (``filament_weighing_finished_left_g``, #7684)."""

    low_stock_pct: int
    cost_per_kg: Decimal | None
    drying_temp_c: int | None = None
    drying_hours: Decimal | None = None
    drift_notice_g: int = int(Settings.model_fields["filament_weighing_drift_notice_g"].default)
    finished_left_g: int = int(Settings.model_fields["filament_weighing_finished_left_g"].default)


#: The settings' own defaults, for a read that shows neither ``low_stock`` nor a value (a
#: static read, a store a test builds with no reader).
_SETTING_DEFAULTS = FilamentDefaults(
    int(Settings.model_fields["filament_low_stock_pct"].default), None
)


def _setting_defaults() -> FilamentDefaults:
    return _SETTING_DEFAULTS


def _properties(
    conn: Conn,
    spool_id: UUID,
    properties: dict[str, JsonValue] | None,
    by: Actor | None,
    checked: bool,
) -> None:
    if properties or checked:
        save_properties_on(
            conn,
            EntityRef("spool", str(spool_id)),
            properties,
            by or Actor(kind="browser"),
            check=checked,
        )


def _hydrate(
    conn: Conn,
    rows: list[dict[str, Any]],
    defaults: FilamentDefaults = _SETTING_DEFAULTS,
    now: datetime | None = None,
    hours: bool = True,
) -> list[m.Spool]:
    """The spools of ``rows`` with their effective values. A spool's subtype and
    material are its own if it sets them, else its type's. A spool with no percentage of
    its own is low under ``defaults.low_stock_pct``, the setting ``filament_low_stock_pct``.
    ``now`` is the time the hours outside dry storage run to (`filament/dry_hours.py`);
    with none it is the database's. With ``hours`` false (a caller that does not return the
    spool) the hours and ``needs_drying`` stay at their defaults and cost no query. The
    hours are rounded to 0.1 h, the precision of the limit; ``needs_drying`` compares the
    unrounded hours. A place's CURRENT kind judges the whole window: no kind history is
    kept, so re-kinding a shelf to a dry box turns its past time there into dry time."""
    default_pct = defaults.low_stock_pct
    types = _by_id(conn, "filament_spool_types", _TYPE_COLUMNS, (r["type_id"] for r in rows))

    def level(row: Row, field: str) -> UUID | None:
        kind = types.get(row["type_id"])
        own: UUID | None = row[field]
        return own if own is not None or kind is None else kind[field]

    def pick(found: dict[UUID, Row], key: UUID | None) -> Row | None:
        return None if key is None else found.get(key)

    subtypes = _by_id(conn, "filament_subtypes", "*", (level(r, "subtype_id") for r in rows))
    materials = _by_id(conn, "filament_materials", "*", (level(r, "material_id") for r in rows))
    values = [
        effective(
            row,
            pick(types, row["type_id"]),
            pick(subtypes, level(row, "subtype_id")),
            pick(materials, level(row, "material_id")),
        )
        for row in rows
    ]
    brands = _by_id(conn, "filament_brands", "id, name", (v.brand_id for v in values))
    categories = _by_id(conn, "filament_categories", "id, name", (v.category_id for v in values))
    effects = _by_id(conn, "filament_effects", "id, name", (v.effect_id for v in values))
    inserts = _by_id(conn, "filament_insert_types", "id, name", (v.insert_type_id for v in values))
    places = _by_id(conn, "filament_locations", "id, name", (r["location_id"] for r in rows))

    def name(found: dict[UUID, Row], key: UUID | None) -> str | None:
        row = pick(found, key)
        return None if row is None else str(row["name"])

    outside = _hours_outside_dry(conn, rows, now) if hours else {}
    spools = []
    for row, own in zip(rows, values, strict=True):
        names = m.SpoolNames(
            brand=name(brands, own.brand_id),
            material=name(materials, own.material_id),
            subtype=name(subtypes, own.subtype_id),
            category=name(categories, own.category_id),
            effect=name(effects, own.effect_id),
            insert_type=name(inserts, own.insert_type_id),
            location=name(places, row["location_id"]),
        )
        spent = outside.get(row["id"], 0.0)
        material = pick(materials, own.material_id)
        spools.append(
            m.Spool.model_validate(
                row
                | {
                    "hours_outside_dry": round(spent, 1),
                    "needs_drying": material is not None
                    and dry_hours.needs_drying(
                        spent,
                        hygroscopic=material["hygroscopic"],
                        max_hours_outside_dry=None
                        if material["max_hours_outside_dry"] is None
                        else float(material["max_hours_outside_dry"]),
                    ),
                    "effective": own,
                    "overridden": overridden(row),
                    "names": names,
                    "low_stock": _low_stock(row["grams_left"], own, default_pct),
                    "drift_notice": row["last_drift_g"] is not None
                    and abs(row["last_drift_g"]) >= defaults.drift_notice_g,
                }
            )
        )
    mark_incomplete(conn, "spool", spools)
    return spools


def _hours_outside_dry(
    conn: Conn, rows: list[dict[str, Any]], now: datetime | None
) -> dict[UUID, float]:
    """Each spool's hours outside dry storage (`dry_hours.outside_dry`), from one query
    for the dry places and one for the ``moved`` events of all ``rows`` since each was
    last dried, else made."""
    if not rows:
        return {}
    if now is None:
        found = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert found is not None
        now = found["now"]
    dry = {
        r["id"]
        for r in conn.execute(
            "SELECT l.id FROM filament_locations l JOIN filament_location_kinds k"
            " ON k.id = l.kind_id WHERE k.dry_storage"
        ).fetchall()
    }
    moves: dict[UUID, list[dry_hours.Move]] = {}
    for e in conn.execute(
        "SELECT e.spool_id, e.at, e.data ->> 'from' AS origin, e.data ->> 'to' AS target"
        " FROM filament_spool_events e JOIN filament_spools s ON s.id = e.spool_id"
        " WHERE e.spool_id = ANY(%s) AND e.kind = 'moved'"
        " AND e.at > coalesce(s.last_dried_at, s.created_at) ORDER BY e.at, e.seq",
        ([r["id"] for r in rows],),
    ).fetchall():
        moves.setdefault(e["spool_id"], []).append(
            dry_hours.Move(
                e["at"],
                None if e["origin"] is None else UUID(e["origin"]),
                None if e["target"] is None else UUID(e["target"]),
            )
        )
    return {
        r["id"]: dry_hours.outside_dry(
            r["last_dried_at"] or r["created_at"],
            moves.get(r["id"], []),
            dry,
            r["location_id"],
            now,
        )
        for r in rows
    }


def _closing_inputs(conn: Conn, spool_id: UUID) -> Row | None:
    """What `ledger.closing` needs of the spool's row: its grams left, whether it was
    added full and its effective label weight; ``None`` for no such spool."""
    return conn.execute(
        "SELECT s.grams_left, s.added_full, coalesce(s.label_weight_g, t.label_weight_g)"
        " AS label_weight_g FROM filament_spools s"
        " LEFT JOIN filament_spool_types t ON t.id = s.type_id WHERE s.id = %s",
        (spool_id,),
    ).fetchone()


def _closing_on(
    conn: Conn,
    spool_id: UUID,
    reason: m.ArchiveReason | None,
    final_filament_g: float | None,
    *,
    grams_left: bool = True,
) -> dict[str, Any]:
    """The spool's closing now (`ledger.closing`, #7684): with ``final_filament_g`` the
    final weighing's filament, else, when ``grams_left``, on the spool's grams left."""
    spool = _closing_inputs(conn, spool_id)
    assert spool is not None
    return ledger.closing(
        ledger.events(conn, spool_id),
        label_weight_g=spool["label_weight_g"],
        added_full=spool["added_full"],
        final_filament_g=final_filament_g,
        reason=reason,
        grams_left=spool["grams_left"] if grams_left else None,
    )


def _final_filament(
    reading_g: float | None, grams: float | None, empty: EmptyWeight | None
) -> float | None:
    """A final weighing's filament (#7684), as `SpoolEventAdd`'s rule reads it: ``grams``
    when given, else the reading less the effective empty weight; ``None`` with no
    weighing, no empty weight known, or a reading under it."""
    if grams is not None:
        return grams
    if reading_g is None or empty is None or reading_g < empty.grams:
        return None
    return reading_g - empty.grams


def _closing_notices(
    reason: m.ArchiveReason | None, final_filament_g: float | None, finished_left_g: int
) -> list[m.ClosingNotice]:
    """What the archive dialog says when the numbers disagree with the reason (#7684):
    a finished spool with more than ``finished_left_g`` on it, or a spool archived for
    another reason that reads empty. Nothing is refused."""
    if final_filament_g is None or reason is None:
        return []
    if reason == "finished" and final_filament_g > finished_left_g:
        return [
            m.ClosingNotice(
                code="filament_left",
                text=f"{_grams_text(final_filament_g)} of filament are still on it.",
            )
        ]
    if reason != "finished" and final_filament_g <= finished_left_g:
        return [
            m.ClosingNotice(code="not_finished_but_empty", text="It reads empty; is it finished?")
        ]
    return []


def _grams_text(value: float) -> str:
    whole = round(value, 1)
    return f"{int(whole)} g" if whole == int(whole) else f"{whole} g"


def _low_stock(grams_left: float | None, values: m.EffectiveValues, default_pct: int) -> bool:
    label = values.label_weight_g
    if grams_left is None or not label:
        return False
    pct = default_pct if values.low_stock_pct is None else values.low_stock_pct
    return grams_left / label * 100 < pct


def _drying_defaults(
    conn: Conn, spool_id: UUID, defaults: FilamentDefaults = _SETTING_DEFAULTS
) -> m.DryingDefaults:
    """The spool's drying defaults (#6720, R-6714-10, R-6714-18): each of temperature and
    hours from its own override, else its type, else its subtype, else its material, the
    subtype and material being the spool's own override, else its type's, else the
    setting in ``defaults`` (#7156, R-7156-5). The values are its effective ones
    (`types.effective`) but for the setting; this names where each came from."""
    row = conn.execute(
        "SELECT s.drying_temp_c AS p_temp, s.drying_hours AS p_hours,"
        " t.drying_temp_c AS t_temp, t.drying_hours AS t_hours,"
        " st.drying_temp_c AS s_temp, st.drying_hours AS s_hours,"
        " mt.drying_temp_c AS m_temp, mt.drying_hours AS m_hours"
        " FROM filament_spools s"
        " LEFT JOIN filament_spool_types t ON t.id = s.type_id"
        " LEFT JOIN filament_subtypes st ON st.id = COALESCE(s.subtype_id, t.subtype_id)"
        " LEFT JOIN filament_materials mt ON mt.id = COALESCE(s.material_id, t.material_id)"
        " WHERE s.id = %s",
        (spool_id,),
    ).fetchone()
    found: dict[str, Any] = {}
    if row is None:
        return m.DryingDefaults()
    fields = (("temp_c", "temp_from", "temp"), ("hours", "hours_from", "hours"))
    setting = {"temp": defaults.drying_temp_c, "hours": defaults.drying_hours}
    for field, origin, column in fields:
        for level, prefix in (("spool", "p"), ("type", "t"), ("subtype", "s"), ("material", "m")):
            if row[f"{prefix}_{column}"] is not None:
                found[field], found[origin] = row[f"{prefix}_{column}"], level
                break
        else:
            if setting[column] is not None:
                found[field], found[origin] = setting[column], "setting"
    return m.DryingDefaults.model_validate(found)


class FilamentSpoolStore:
    """The spool and tag tables over a pool whose connections autocommit and return dict
    rows (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(
        self,
        pool: ConnectionPool[Conn],
        defaults: Callable[[], FilamentDefaults] = _setting_defaults,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """``defaults`` reads the Filament settings as they are now. It may take a
        connection of its own, so a method calls it before it takes one of the pool's,
        never inside. A store built without one uses the settings' defaults. ``now`` is the
        clock the hours outside dry storage run to; with none, the database's."""
        self._pool = pool
        self._defaults = defaults
        self._now = now

    @property
    def conninfo(self) -> str:
        """The database the pool serves, for a caller that needs a connection of its own
        (`filament/rfid_push.py`'s drain lock)."""
        conninfo = self._pool.conninfo
        return conninfo() if callable(conninfo) else conninfo

    def list(self, *, include_archived: bool = False, needs_type: bool | None = None) -> _Spools:
        """By number. ``needs_type`` keeps only the spools with no type (``True``) or only
        those with one (``False``)."""
        return self._list(self._defaults(), include_archived, needs_type)

    def _list(
        self,
        defaults: FilamentDefaults,
        include_archived: bool = False,
        needs_type: bool | None = None,
    ) -> _Spools:
        where = [] if include_archived else ["archived_at IS NULL"]
        if needs_type is not None:
            where.append("type_id IS NULL" if needs_type else "type_id IS NOT NULL")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_SPOOL_COLUMNS} FROM filament_spools{clause} ORDER BY number"
            ).fetchall()
            return _hydrate(conn, rows, defaults, self._clock())

    def _clock(self) -> datetime | None:
        return None if self._now is None else self._now()

    def defaults(self) -> FilamentDefaults:
        """The Filament settings as they are now, for a caller that reads a spool with
        `read` on its own connection: call it before taking that connection."""
        return self._defaults()

    def get(self, spool_id: UUID) -> m.Spool | None:
        defaults = self._defaults()
        with self._pool.connection() as conn:
            return self._spool(conn, spool_id, defaults, self._clock())

    def get_many(self, spool_ids: Sequence[UUID]) -> dict[UUID, m.Spool]:
        """Each spool among ``spool_ids`` that exists, by id, in a fixed number of queries
        (the labels PDF, #7157). As `read_many`, its hours outside dry storage are not read."""
        with self._pool.connection() as conn:
            return self.read_many(conn, spool_ids)

    def detail(self, spool_id: UUID) -> m.SpoolDetail | None:
        """The spool with its tags, effective presets and K values, the basis of its
        grams and whether it ever printed; ``None`` for no such spool."""
        defaults = self._defaults()
        with self._pool.connection() as conn, conn.transaction():
            # One snapshot, so the stored grams and the basis behind them agree.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            spool = self._spool(conn, spool_id, defaults, self._clock())
            if spool is None:
                return None
            history = ledger.events(conn, spool_id)
            tag_row = conn.execute(
                f"SELECT {_TAG_COLUMNS_T} FROM filament_tags t JOIN filament_tag_bindings b"
                " ON b.tag_id = t.id WHERE b.spool_id = %s AND b.unbound_at IS NULL",
                (spool_id,),
            ).fetchone()
            last = conn.execute(
                f"SELECT {_TAG_COLUMNS_T}, b.unbound_at, b.reason,"
                " (SELECT s.number FROM filament_tag_bindings o JOIN filament_spools s"
                "  ON s.id = o.spool_id WHERE o.tag_id = t.id AND o.unbound_at IS NULL)"
                " AS held_by FROM filament_tag_bindings b JOIN filament_tags t ON t.id = b.tag_id"
                " WHERE b.spool_id = %s AND b.unbound_at IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM filament_tag_bindings c"
                "  WHERE c.spool_id = b.spool_id"
                "  AND c.tag_id = b.tag_id AND c.unbound_at IS NULL)"
                " ORDER BY b.unbound_at DESC, b.bound_at DESC LIMIT 1",
                (spool_id,),
            ).fetchone()
            line = conn.execute(
                "SELECT i.id AS invoice_id, i.number, i.invoice_date, i.supplier_id,"
                " sup.name AS supplier_name, l.id AS line_id, l.position AS line_position,"
                " l.allocated_cost, l.quantity"
                " FROM filament_spools s JOIN filament_invoice_lines l ON l.id = s.invoice_line_id"
                " JOIN filament_invoices i ON i.id = l.invoice_id"
                " JOIN filament_suppliers sup ON sup.id = i.supplier_id WHERE s.id = %s",
                (spool_id,),
            ).fetchone()
            products = conn.execute(
                "SELECT p.supplier_id, sup.name AS supplier_name, p.sku, p.product_url,"
                " p.pack_size, t.label_weight_g, n.price AS newest_price"
                " FROM filament_supplier_products p"
                " JOIN filament_suppliers sup ON sup.id = p.supplier_id"
                " JOIN filament_spool_types t ON t.id = p.type_id"
                " LEFT JOIN LATERAL (SELECT pp.price FROM filament_price_points pp"
                "  WHERE pp.product_id = p.id"
                "  ORDER BY pp.on_date DESC, pp.created_at DESC, pp.id LIMIT 1) n ON true"
                " WHERE p.type_id = (SELECT type_id FROM filament_spools WHERE id = %s)"
                " AND p.archived_at IS NULL AND sup.archived_at IS NULL"
                " ORDER BY upper(sup.name), upper(p.sku), p.id",
                (spool_id,),
            ).fetchall()
            weighings = [e for e in history if e.kind == "weighed"]
            return m.SpoolDetail.model_validate(
                spool.model_dump()
                | {
                    "drying_defaults": _drying_defaults(conn, spool_id, defaults),
                    "last_weighing": weighings[-1] if weighings else None,
                    "invoice": None
                    if line is None
                    else m.SpoolInvoiceRef.model_validate(
                        {**line, "unit_cost": unit_cost(line["allocated_cost"], line["quantity"])}
                    ),
                    "type_suppliers": [
                        m.SpoolTypeSupplier(
                            supplier_id=r["supplier_id"],
                            supplier_name=r["supplier_name"],
                            sku=r["sku"],
                            product_url=r["product_url"],
                            newest_price=r["newest_price"],
                            price_per_kg=None
                            if r["newest_price"] is None
                            else price_per_kg(
                                r["newest_price"], r["pack_size"], r["label_weight_g"]
                            ),
                        )
                        for r in products
                    ],
                    "tag": None if tag_row is None else m.Tag.model_validate(tag_row),
                    "last_tag": None
                    if last is None
                    else m.LastTag(
                        tag=m.Tag.model_validate(last),
                        unbound_at=last["unbound_at"],
                        reason=last["reason"],
                        held_by=last["held_by"],
                    ),
                    "presets": p.effective_presets(conn, spool_id),
                    "k_values": p.effective_k_values(conn, spool_id),
                    "basis": ledger.basis(
                        history,
                        weight_locked=spool.weight_locked,
                        label_weight_g=spool.effective.label_weight_g,
                    ),
                    "in_use": any(e.kind in _USED for e in history),
                }
            )

    def summary(self) -> m.InventorySummary:
        """The unarchived spools in total; a spool's value is its grams left at its
        effective cost per kg, else ``filament_default_cost_per_kg``. A spool is loaded when
        its place is a printer's tray (``printer_id`` set, `filament/inventory.py`).
        ``consumed_g`` is the grams of every spool's plates, archived ones too."""
        defaults = self._defaults()
        default_cost = defaults.cost_per_kg
        spools = self._list(defaults)
        with self._pool.connection() as conn:
            trays = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM filament_locations WHERE printer_id IS NOT NULL"
                ).fetchall()
            }
            consumed = sum(
                plate.grams
                for found in ledger.plate_events(conn).values()
                for plate in metrics.plates(found)
            )
        value: Decimal | None = None
        stock: dict[str | None, tuple[int, float]] = {}
        for spool in spools:
            grams = spool.grams_left or 0.0
            count, total = stock.get(spool.names.material, (0, 0.0))
            stock[spool.names.material] = (count + 1, total + grams)
            cost = spool.effective.cost_per_kg
            if cost is None:
                cost = default_cost
            if cost is not None and spool.grams_left is not None:
                value = (value or Decimal(0)) + Decimal(str(grams)) / 1000 * cost
        return m.InventorySummary(
            spools=len(spools),
            grams_left=sum(s.grams_left or 0.0 for s in spools),
            value_left=None if value is None else value.quantize(Decimal("0.01")),
            by_material=[
                m.MaterialStock(material=name, spools=count, grams_left=total)
                for name, (count, total) in sorted(
                    stock.items(), key=lambda item: (item[0] is None, item[0] or "")
                )
            ],
            loaded=sum(1 for s in spools if s.location_id in trays),
            low_stock=sum(1 for s in spools if s.low_stock),
            needs_type=sum(1 for s in spools if s.type_id is None),
            consumed_g=consumed,
        )

    def tags(self, *, include_unbound: bool = False) -> _TagRows:
        """Every tag with its open binding and its spool's number, oldest first. A tag on
        no spool only with ``include_unbound``."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_TAG_COLUMNS_T}, {_BINDING_COLUMNS_B}, s.number AS spool_number"
                " FROM filament_tags t LEFT JOIN filament_tag_bindings b"
                " ON b.tag_id = t.id AND b.unbound_at IS NULL"
                " LEFT JOIN filament_spools s ON s.id = b.spool_id"
                f"{'' if include_unbound else ' WHERE b.id IS NOT NULL'}"
                " ORDER BY t.created_at, t.id"
            ).fetchall()
            return [self._tag_row(row) for row in rows]

    def tag(self, tag_id: UUID) -> m.TagDetail | None:
        """One tag with every binding it has had, newest first; ``None`` for no such tag."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_TAG_COLUMNS_T}, {_BINDING_COLUMNS_B}, s.number AS spool_number"
                " FROM filament_tags t LEFT JOIN filament_tag_bindings b"
                " ON b.tag_id = t.id AND b.unbound_at IS NULL"
                " LEFT JOIN filament_spools s ON s.id = b.spool_id WHERE t.id = %s",
                (tag_id,),
            ).fetchone()
            if row is None:
                return None
            history = conn.execute(
                f"SELECT {_BINDING_COLUMNS} FROM filament_tag_bindings WHERE tag_id = %s"
                " ORDER BY bound_at DESC, id",
                (tag_id,),
            ).fetchall()
        return m.TagDetail.model_validate(
            self._tag_row(row).model_dump()
            | {"history": [m.TagBinding.model_validate(h) for h in history]}
        )

    def ledger(
        self,
        *,
        spool_id: UUID | None = None,
        kind: m.EventKind | None = None,
        source: m.EventSource | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> m.EventPage:
        """One page of every spool's events, newest first (`ledger.page`)."""
        with self._pool.connection() as conn:
            return ledger.page(
                conn,
                spool_id=spool_id,
                kind=kind,
                source=source,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
            )

    def create(
        self, body: m.SpoolCreate, *, actor: str, by: Actor | None = None, checked: bool = False
    ) -> m.Spool:
        """A new spool and its ``created`` event; ``grams_left``, when sent, is that
        event's reading. The temperature ranges are checked on the effective values. Its
        ``properties`` are written with it, by ``by``; with ``checked`` (the spool routes,
        #7703 spec 6.3b) an incomplete spool is `MissingRequiredProperties` and nothing is
        written."""
        defaults = self._defaults()
        try:
            with self._pool.connection() as conn, conn.transaction():
                bare = body.model_copy(update={"properties": None})
                spool_id = self.create_on(conn, bare, actor=actor)
                _properties(conn, spool_id, body.properties, by, checked)
                made = self._spool(conn, spool_id, defaults, self._clock())
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        assert made is not None
        return made

    def create_on(
        self,
        conn: Conn,
        body: m.SpoolCreate,
        *,
        actor: str,
        ref_kind: str | None = None,
        ref_id: UUID | None = None,
        continued_from: UUID | None = None,
    ) -> UUID:
        """`create` on ``conn``, in the caller's transaction (`filament/csv_import.py`):
        the new spool's id. A row it names that does not exist is the driver's
        `ForeignKeyViolation`. ``ref_kind`` and ``ref_id`` go on the ``created`` event
        (an invoice line's, #6266); a retry of one pair books nothing twice. It writes no
        ``properties``: `create` and `bulk_on` do. ``continued_from`` (#7684) goes on the
        ``created`` event's ``data``; `bulk_on` has checked the source and links its
        ``archived`` event back."""
        if body.properties is not None:
            raise ValueError("create_on writes no properties")
        data = body.model_dump(exclude={"grams_left", "properties"})
        names = list(data)
        row = conn.execute(
            f"INSERT INTO filament_spools ({', '.join(names)})"
            f" VALUES ({', '.join('%s' for _ in names)}) RETURNING id",
            [data[name] for name in names],
        ).fetchone()
        assert row is not None
        spool_id: UUID = row["id"]
        self._check(conn, spool_id)
        ledger.record(
            conn,
            spool_id,
            "created",
            source="person" if body.grams_left is not None else None,
            absolute_g=body.grams_left,
            ref_kind=ref_kind,
            ref_id=None if ref_id is None else str(ref_id),
            actor=actor,
            data=None if continued_from is None else {"continued_from": str(continued_from)},
        )
        return spool_id

    def bulk_on(
        self,
        conn: Conn,
        body: m.SpoolBulkCreate,
        *,
        actor: str,
        by: Actor | None = None,
        checked: bool = False,
    ) -> _Ids:
        """``body.quantity`` spools of one draft, in the caller's transaction (#7150,
        R-7150-1, R-7150-2): each its own number, its ``created`` event (with
        ``grams_left`` as its reading), its move to ``location_id``, and its own presets
        and K values, each an ``edited`` event. Their ids in number order.
        `SpoolTrayPlaceError` for a printer's tray, `MoveTargetError` for a place that does
        not exist, is archived or is an AMS unit's; the driver's `ForeignKeyViolation` or
        `UniqueViolation` for a row the draft names that does not exist or two rows of
        one key. With ``copy_of`` a preset row that matches a ``migrated`` row of that
        spool is written ``migrated`` (R-7150-11, R-7150-13); `SpoolCopySourceError` when
        it names no spool. The source's ``migrated`` rows are read here, when the create
        runs, not when the draft was read: if the source's presets are re-set between the
        copy and the save, an unchanged copied row stops being ``migrated``. That is
        intended. Each spool gets the draft's ``properties``, by ``by``; with ``checked``
        (#7703) an incomplete spool is `MissingRequiredProperties`."""
        carried: set[_PresetValues] = set()
        continued: UUID | None = None
        if body.continued_from is not None:
            # #7684: the source is locked, so its archive state holds while the link is made.
            source = conn.execute(
                "SELECT archived_at FROM filament_spools WHERE id = %s FOR UPDATE",
                (body.continued_from,),
            ).fetchone()
            if source is None:
                raise SpoolCopySourceError()
            if source["archived_at"] is None:
                raise ContinuedFromNotArchivedError()
            continued = body.continued_from
        if body.copy_of is not None:
            # One read: the source, and its migrated rows (none: one row of NULLs).
            rows = conn.execute(
                "SELECT p.printer_model, p.nozzle_diameter, p.slicer_filament,"
                " p.slicer_filament_name FROM filament_spools s"
                " LEFT JOIN filament_spool_presets p ON p.spool_id = s.id AND p.migrated"
                " WHERE s.id = %s",
                (body.copy_of,),
            ).fetchall()
            if not rows:
                raise SpoolCopySourceError()
            carried = _preset_values(rows)
        if body.location_id is not None:
            place = conn.execute(
                "SELECT tray_id FROM filament_locations WHERE id = %s", (body.location_id,)
            ).fetchone()
            if place is not None and place["tray_id"] is not None:
                raise SpoolTrayPlaceError()
        made: _Ids = []
        bare = body.spool.model_copy(update={"properties": None})
        for _ in range(body.quantity):
            spool_id = self.create_on(conn, bare, actor=actor, continued_from=continued)
            _properties(conn, spool_id, body.spool.properties, by, checked)
            if continued is not None:
                conn.execute(
                    "UPDATE filament_spool_events SET data = data || %s WHERE id = ("
                    "SELECT id FROM filament_spool_events WHERE spool_id = %s AND kind = 'archived'"
                    " ORDER BY at DESC, seq DESC LIMIT 1)",
                    (Jsonb({"continued_as": str(spool_id)}), continued),
                )
            if body.location_id is not None:
                LocationStore.move_on(conn, spool_id, body.location_id, actor=actor)
            if body.presets:
                self.set_presets_on(conn, spool_id, body.presets, actor=actor, carried=carried)
            if body.k_values:
                self.set_k_values_on(conn, spool_id, body.k_values, actor=actor)
            made.append(spool_id)
        return made

    def copy(self, spool_id: UUID) -> m.SpoolDraft | None:
        """A new spool's draft from ``spool_id`` (#7150, R-7150-3): its type, its own
        overrides and note, its own presets and K values, and its place unless that is a
        printer's tray or archived. Not its number, tags, tray place, ledger (so no
        ``grams_left``), weight lock or ``added_full``, a fact of the source's own history
        (R-7150-10). ``copy_of`` names the source, so a bulk create marks the rows that
        match its ``migrated`` ones (R-7150-11, R-7150-13). ``None`` for no such spool.
        Writes nothing."""
        left_out = {"grams_left", "weight_locked", "added_full", "properties"}
        fields = [f for f in m.SpoolCreate.model_fields if f not in left_out]
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {', '.join(fields)}, location_id FROM filament_spools WHERE id = %s",
                (spool_id,),
            ).fetchone()
            if row is None:
                return None
            place = None
            if row["location_id"] is not None:
                kept = conn.execute(
                    "SELECT id FROM filament_locations WHERE id = %s AND tray_id IS NULL"
                    " AND archived_at IS NULL",
                    (row["location_id"],),
                ).fetchone()
                place = None if kept is None else kept["id"]
            presets = conn.execute(
                "SELECT printer_model, nozzle_diameter, slicer_filament, slicer_filament_name"
                " FROM filament_spool_presets WHERE spool_id = %s"
                " ORDER BY printer_model, nozzle_diameter",
                (spool_id,),
            ).fetchall()
            k_values = p.own_k_values(conn, spool_id)
        own = {f: row[f] for f in fields if row[f] is not None}
        return m.SpoolDraft(
            spool=m.SpoolCreate.model_validate(own),
            location_id=place,
            presets=[m.SpoolPresetCreate.model_validate(r) for r in presets],
            k_values=[
                m.SpoolKValueCreate.model_validate(
                    {f: r[f] for f in m.SpoolKValueCreate.model_fields}
                )
                for r in k_values
            ],
            copy_of=spool_id,
        )

    def update(
        self,
        spool_id: UUID,
        patch: m.SpoolPatch,
        *,
        actor: str,
        by: Actor | None = None,
        checked: bool = False,
    ) -> m.Spool | None:
        """The fields ``patch`` sends; ``None`` for no such spool. One ``edited`` event
        holds every field that changed, as ``[before, after]``; ``archived`` writes an
        ``archived`` or ``restored`` event instead. Archiving closes the spool's open tag
        binding (reason ``archived``, a ``tag_unbound`` event), as the native archive does
        (`spools/native.py`); restoring binds nothing back. A patch that would leave neither
        a type nor a material is `SpoolTypelessError`, and the temperature ranges are
        checked on the effective values after it. A spool's first type drops its
        ``migrated`` presets (`presets.drop_migrated`), in the same event as
        ``changes.presets``. Its ``properties`` are written with it, by ``by``; with
        ``checked`` (the spool route, #7703 spec 6.3b) a save that leaves the spool
        incomplete is `MissingRequiredProperties`, unless it sends only ``archived``."""
        defaults = self._defaults()
        try:
            with self._pool.connection() as conn, conn.transaction():
                if not self.update_on(conn, spool_id, patch, actor=actor):
                    return None
                _properties(
                    conn,
                    spool_id,
                    patch.properties,
                    by,
                    checked and checks_save(patch.model_fields_set),
                )
                return self._spool(conn, spool_id, defaults, self._clock())
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None

    def update_on(
        self,
        conn: Conn,
        spool_id: UUID,
        patch: m.SpoolPatch,
        *,
        actor: str,
        ref_kind: str | None = None,
        ref_id: UUID | None = None,
        at: datetime | None = None,
        invoice_line_id: UUID | None = None,
    ) -> bool:
        """`update` on ``conn``, in the caller's transaction (`filament/csv_import.py`);
        false for no such spool. A row it names that does not exist is the driver's
        `ForeignKeyViolation`. ``ref_kind`` and ``ref_id`` go on the ``edited`` event
        (an invoice line's, #6266); a retry of one pair books nothing twice. ``at`` is when
        an archive or restore happened (the pull's: Bambuddy's change time, #6265 review
        M3); with none it is booked now. An ``invoice_line_id`` (an invoice confirm's,
        #6269 Decision 12) that differs from the spool's is set in the same UPDATE and is
        one more change of that event."""
        return (
            self._update_on(
                conn,
                spool_id,
                patch,
                actor=actor,
                ref_kind=ref_kind,
                ref_id=ref_id,
                at=at,
                invoice_line_id=invoice_line_id,
            )
            is not None
        )

    def _update_on(
        self,
        conn: Conn,
        spool_id: UUID,
        patch: m.SpoolPatch,
        *,
        actor: str,
        ref_kind: str | None = None,
        ref_id: UUID | None = None,
        at: datetime | None = None,
        invoice_line_id: UUID | None = None,
    ) -> bool | None:
        """`update_on`'s body: ``None`` for no such spool, else whether a column changed
        (an archive or restore is not counted). An ``invoice_line_id`` that differs counts
        as changed; the bulk edit never passes one."""
        sent = [n for n in _WRITABLE if n in patch.model_fields_set]
        if patch.archived:
            # The tag before the spool, as every writer of bindings takes them.
            conn.execute(
                "SELECT t.id FROM filament_tags t JOIN filament_tag_bindings b"
                " ON b.tag_id = t.id WHERE b.spool_id = %s AND b.unbound_at IS NULL"
                " ORDER BY t.id FOR UPDATE OF t",
                (spool_id,),
            )
        current = conn.execute(
            f"SELECT {_SPOOL_COLUMNS}, invoice_line_id FROM filament_spools WHERE id = %s"
            " FOR UPDATE",
            (spool_id,),
        ).fetchone()
        if current is None:
            return None
        after = {n: getattr(patch, n) for n in sent}
        merged = current | after
        if merged["type_id"] is None and merged["material_id"] is None:
            raise SpoolTypelessError()
        changes = {
            n: [current[n], after[n]]
            for n in m.SpoolPatch.model_fields
            if n in after and current[n] != after[n]
        }
        if invoice_line_id is not None and current["invoice_line_id"] != invoice_line_id:
            after["invoice_line_id"] = invoice_line_id
            changes["invoice_line_id"] = [current["invoice_line_id"], invoice_line_id]
        if changes:
            columns = list(changes)
            conn.execute(
                f"UPDATE filament_spools SET {', '.join(f'{c} = %s' for c in columns)},"
                " updated_at = now() WHERE id = %s",
                [*(after[c] for c in columns), spool_id],
            )
            self._check(conn, spool_id)
            if current["type_id"] is None and merged["type_id"] is not None:
                dropped = p.drop_migrated(conn, spool_id)
                if dropped is not None:
                    changes["presets"] = dropped
            ledger.record(
                conn,
                spool_id,
                "edited",
                ref_kind=ref_kind,
                ref_id=None if ref_id is None else str(ref_id),
                actor=actor,
                data={"changes": changes},
            )
        if patch.archived is not None:
            closing = None
            if patch.archived and current["archived_at"] is None:
                # The pull's archive (``at`` is Bambuddy's time) records ``other`` (#7684).
                closing = _closing_on(conn, spool_id, "other" if at is not None else None, None)
            self._archive(conn, current, patch.archived, actor, at, data=closing)
        return bool(changes)

    def presets(self, spool_id: UUID) -> _Presets | None:
        """The spool's slicer presets, its own over its type's; ``None`` for no such
        spool."""
        with self._pool.connection() as conn:
            if self._spool(conn, spool_id, hours=False) is None:
                return None
            return p.effective_presets(conn, spool_id)

    def set_presets(self, spool_id: UUID, rows: _PresetRows, *, actor: str) -> _Presets | None:
        """Replace the spool's own presets with ``rows`` (``[]`` resets it to its type's);
        ``None`` for no such spool. One ``edited`` event holds the spool's own rows
        before and after, as ``changes.presets``. Two rows of one key are
        `TypeRowsDuplicateError`. A row sent back as a ``migrated`` row was (the same key
        and values) stays ``migrated``, so the spool's first type still drops it
        (`presets.drop_migrated`); a new or changed row is the person's."""
        try:
            with self._pool.connection() as conn, conn.transaction():
                if self._lock(conn, spool_id) is None:
                    return None
                self.set_presets_on(conn, spool_id, rows, actor=actor)
                return p.effective_presets(conn, spool_id)
        except errors.UniqueViolation:
            raise TypeRowsDuplicateError() from None
        except errors.ForeignKeyViolation as error:
            if not names_preset(error):
                raise
            raise PresetUnknownError() from None

    @classmethod
    def set_presets_on(
        cls,
        conn: Conn,
        spool_id: UUID,
        rows: Sequence[m.SpoolPresetCreate],
        *,
        actor: str,
        carried: set[_PresetValues] | None = None,
    ) -> None:
        """`set_presets` on ``conn``, in the caller's transaction, the spool locked (also a
        bulk create's new spools, #7150). A row in ``carried`` (a copy source's
        ``migrated`` rows, R-7150-13) is written ``migrated`` too. The driver's errors
        pass through."""
        table = "filament_spool_presets"
        before = p.own_presets(conn, spool_id)
        migrated = _migrated_rows(conn, spool_id) | (carried or set())
        conn.execute(f"DELETE FROM {table} WHERE spool_id = %s", (spool_id,))
        for row in rows:
            values: _PresetValues = (
                row.printer_model,
                row.nozzle_diameter,
                row.slicer_filament,
                row.slicer_filament_name,
            )
            conn.execute(
                f"INSERT INTO {table} (spool_id, printer_model, nozzle_diameter,"
                " slicer_filament, slicer_filament_name, migrated)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (spool_id, *values, values in migrated),
            )
        after = p.own_presets(conn, spool_id)
        cls._edited(conn, spool_id, "presets", before, after, actor)

    def k_values(self, spool_id: UUID) -> _KValues | None:
        """The spool's pressure-advance values, its own over its type's; ``None`` for no
        such spool."""
        with self._pool.connection() as conn:
            if self._spool(conn, spool_id, hours=False) is None:
                return None
            return p.effective_k_values(conn, spool_id)

    def set_k_values(self, spool_id: UUID, rows: _KRows, *, actor: str) -> _KValues | None:
        """`set_presets` for K values. A printer that does not exist is `CatalogRefError`."""
        try:
            with self._pool.connection() as conn, conn.transaction():
                if self._lock(conn, spool_id) is None:
                    return None
                self.set_k_values_on(conn, spool_id, rows, actor=actor)
                return p.effective_k_values(conn, spool_id)
        except errors.UniqueViolation:
            raise TypeRowsDuplicateError() from None
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None

    @classmethod
    def set_k_values_on(cls, conn: Conn, spool_id: UUID, rows: _KRows, *, actor: str) -> None:
        """`set_k_values` on ``conn``, in the caller's transaction, the spool locked (also a
        bulk create's new spools, #7150). The driver's errors pass through."""
        table = "filament_spool_k_values"
        before = p.own_k_values(conn, spool_id)
        conn.execute(f"DELETE FROM {table} WHERE spool_id = %s", (spool_id,))
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} (spool_id, printer_id, extruder, nozzle_diameter,"
                " nozzle_type, k_value, name, cali_idx, setting_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    spool_id,
                    row.printer_id,
                    row.extruder,
                    row.nozzle_diameter,
                    row.nozzle_type,
                    row.k_value,
                    row.name,
                    row.cali_idx,
                    row.setting_id,
                ),
            )
        after = p.own_k_values(conn, spool_id)
        cls._edited(conn, spool_id, "k_values", before, after, actor)

    def closing_preview(
        self,
        spool_id: UUID,
        reason: m.ArchiveReason | None,
        final_reading_g: float | None,
        final_grams: float | None,
    ) -> m.ClosingPreview | None:
        """What archiving the spool with these inputs would record (#7684), reading only;
        ``None`` for no such spool. The final filament is resolved as `archive` resolves
        it: ``final_grams`` is the filament; ``final_reading_g`` alone takes the spool's
        effective empty weight off, and ``None`` where that cannot be done (no empty
        weight, or a reading under it: the archive refuses those); neither closes on the
        spool's ``grams_left`` with no scale side. The notices follow the setting
        ``filament_weighing_finished_left_g``."""
        defaults = self._defaults()
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if _closing_inputs(conn, spool_id) is None:
                return None
            empty = effective_on(conn, spool_id)
            final = _final_filament(final_reading_g, final_grams, empty)
            # A reading that cannot be resolved closes on nothing, not on the ledger.
            weighed = final_reading_g is not None or final_grams is not None
            closed = _closing_on(conn, spool_id, reason, final, grams_left=not weighed)
        left = closed["final_filament_g"]
        return m.ClosingPreview(
            closing=m.Closing.model_validate(closed),
            final_filament_g=left,
            empty_g=None if empty is None else empty.grams,
            notices=_closing_notices(reason, left, defaults.finished_left_g),
        )

    def archive(
        self,
        spool_id: UUID,
        archived: bool,
        *,
        actor: str,
        reason: m.ArchiveReason | None = None,
        final_reading_g: float | None = None,
        final_grams: float | None = None,
    ) -> m.Spool | None:
        """Take a spool off the lists (``True``) or put it back; ``None`` for no such
        spool. Nothing is written when it is already so. An archive (#7684) first books the
        final weighing when one is sent, as `add_event` books one (`_weigh_on`:
        `NoEmptyWeightError`, `ReadingBelowEmptyError`), then the ``archived`` event with
        the closing (`ledger.closing`) as its ``data``, in one transaction."""
        if not archived:
            return self.update(spool_id, m.SpoolPatch(archived=False), actor=actor)
        defaults = self._defaults()
        with self._pool.connection() as conn, conn.transaction():
            # The tag before the spool, as every writer of bindings takes them.
            conn.execute(
                "SELECT t.id FROM filament_tags t JOIN filament_tag_bindings b"
                " ON b.tag_id = t.id WHERE b.spool_id = %s AND b.unbound_at IS NULL"
                " ORDER BY t.id FOR UPDATE OF t",
                (spool_id,),
            )
            current = conn.execute(
                f"SELECT {_SPOOL_COLUMNS} FROM filament_spools WHERE id = %s FOR UPDATE",
                (spool_id,),
            ).fetchone()
            if current is None:
                return None
            if current["archived_at"] is None:
                final: float | None = None
                if final_reading_g is not None or final_grams is not None:
                    weighing = m.SpoolEventAdd(
                        kind="weighed",
                        source="scale" if final_reading_g is not None else "person",
                        grams=final_grams,
                        reading_g=final_reading_g,
                    )
                    weighed = self._weigh_on(conn, spool_id, weighing, actor, {})
                    assert weighed is not None
                    final = weighed.grams_after
                closing = _closing_on(conn, spool_id, reason, final)
                self._archive(conn, current, True, actor, data=closing)
            return self._spool(conn, spool_id, defaults, self._clock())

    def delete(self, spool_id: UUID, *, refusing: Iterable[m.EventKind] = _USED) -> bool:
        """Remove a spool that never printed, with its events, tag bindings and Bambuddy
        sync error row; ``False`` for no such spool. `SpoolBambuddyLinkedError`
        when a Bambuddy link names it (the next pull would make it again), and
        `SpoolBambuddyCreatingError` while it has a create intent, with or without an
        error row: a timeout or a lost answer also leaves an error row, and the Bambuddy
        row such a create made would come back the same way. `SpoolInUseError`
        when its ledger holds an event of a kind in ``refusing`` (a ``print_started`` or
        ``print_ended`` one; the native
        API's delete refuses ``print_ended`` only, `spools/native.py`)."""
        with self._pool.connection() as conn, conn.transaction():
            found = conn.execute(
                "SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
            ).fetchone()
            if found is None:
                return False
            used = conn.execute(
                "SELECT 1 FROM filament_spool_events WHERE spool_id = %s"
                " AND kind = ANY(%s) LIMIT 1",
                (spool_id, list(refusing)),
            ).fetchone()
            if used is not None:
                raise SpoolInUseError()
            if conn.execute(
                "SELECT 1 FROM filament_bb_links WHERE spool_id = %s", (spool_id,)
            ).fetchone():
                raise SpoolBambuddyLinkedError()
            if conn.execute(
                "SELECT 1 FROM filament_bb_creates WHERE kind = 'spool' AND sb_id = %s",
                (spool_id,),
            ).fetchone():
                raise SpoolBambuddyCreatingError()
            for table in ("filament_spool_events", "filament_tag_bindings"):
                conn.execute(f"DELETE FROM {table} WHERE spool_id = %s", (spool_id,))
            # The Bambuddy sync's error row names the spool (#6265 R-T3-5); it has no
            # intent here (review M9).
            conn.execute(
                "DELETE FROM filament_bb_push_errors WHERE kind = 'spool' AND sb_id = %s",
                (spool_id,),
            )
            conn.execute("DELETE FROM filament_spools WHERE id = %s", (spool_id,))
        return True

    def bind_tag(
        self,
        spool_id: UUID,
        *,
        tag_uid: str | None = None,
        tray_uuid: str | None = None,
        kind: m.TagKind,
        override: bool = False,
        actor: str,
        marks_origin: bool = False,
    ) -> m.TagBinding | None:
        """Put a tag on a spool; ``None`` for no such spool. The tag is found by its
        cleaned UID or tray UUID (`spools.models.clean_tag_uid`, `clean_tray_uuid`), and
        takes a key it lacked; a tag not seen before is made with ``kind``. An open binding
        of the tag to another spool, and of the spool to another tag, is closed with
        ``reason='rebound'`` and a ``tag_unbound`` event on its spool; the new binding
        writes ``tag_bound``. A tag already on the spool and already carrying the same
        ``override`` is left as it is; one whose ``override`` differs is updated in place
        (no ``tag_unbound``/``tag_bound`` pair). ``override`` is valid only for a
        ``bambu_mifare`` tag (`OverrideKindError`) and locks the spool's weight, with one
        ``edited`` event when it was unlocked. Every tag whose binding this changes is
        queued for Bambuddy (`filament/rfid_push.py`), whatever the sync setting says.
        With ``marks_origin`` (a person's bind) a new binding sets the spool's
        ``data_origin`` from the tag's stored kind, not ``kind``, which names a new tag's
        only (#7149 R-7149-10: `rfid_push.link_origin`), in one ``edited`` event.

        Locks, in this order: the tag, then the new and the old spool by id. Every writer of
        bindings takes the tag first, so two binds in opposite directions wait rather than
        deadlock."""
        uid, tray = clean_tag_uid(tag_uid), clean_tray_uuid(tray_uuid)
        if uid is None and tray is None:
            raise TagUnnamedError()
        try:
            with self._pool.connection() as conn, conn.transaction():
                return self.bind_on(conn, spool_id, uid, tray, kind, override, actor, marks_origin)
        except NoSpoolError:
            return None

    @staticmethod
    def bind_on(
        conn: Conn,
        spool_id: UUID,
        uid: str | None,
        tray: str | None,
        kind: m.TagKind,
        override: bool | None,
        actor: str,
        marks_origin: bool = False,
    ) -> m.TagBinding:
        """`bind_tag` on ``conn``'s transaction, its keys already cleaned; `NoSpoolError`
        when the spool does not exist, and `OverrideKindError`, which the caller rolls back
        on. ``override=None`` keeps a held binding's flag as it is (what the Bambuddy pull
        and a tray link pass: they never change one); a new binding then takes False."""
        if (
            conn.execute("SELECT 1 FROM filament_spools WHERE id = %s", (spool_id,)).fetchone()
            is None
        ):
            raise NoSpoolError()
        tag_id, tag_kind = FilamentSpoolStore._tag(conn, uid, tray, kind)
        if override and tag_kind != "bambu_mifare":
            raise OverrideKindError()
        holder = conn.execute(
            "SELECT spool_id FROM filament_tag_bindings WHERE tag_id = %s AND unbound_at IS NULL",
            (tag_id,),
        ).fetchone()
        wanted = {spool_id} | ({holder["spool_id"]} if holder else set())
        locked = conn.execute(
            "SELECT id FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (list(wanted),),
        ).fetchall()
        if spool_id not in {row["id"] for row in locked}:
            # Deleted since the first look: roll back the tag this may have made.
            raise NoSpoolError()
        held = conn.execute(
            f"SELECT {_BINDING_COLUMNS} FROM filament_tag_bindings"
            " WHERE tag_id = %s AND spool_id = %s AND unbound_at IS NULL",
            (tag_id, spool_id),
        ).fetchone()
        if held is not None:
            if override is None or held["override"] == override:
                return m.TagBinding.model_validate(held)
            held = conn.execute(
                "UPDATE filament_tag_bindings SET override = %s WHERE id = %s"
                f" RETURNING {_BINDING_COLUMNS}",
                (override, held["id"]),
            ).fetchone()
            assert held is not None
            if override:
                FilamentSpoolStore._lock_weight(conn, spool_id, actor)
            enqueue_tag(conn, tag_id)
            return m.TagBinding.model_validate(held)
        override = bool(override)
        closed = conn.execute(
            "UPDATE filament_tag_bindings SET unbound_at = now(), reason = 'rebound'"
            " WHERE unbound_at IS NULL AND (tag_id = %s OR spool_id = %s)"
            " RETURNING tag_id, spool_id",
            (tag_id, spool_id),
        ).fetchall()
        for old in closed:
            enqueue_tag(conn, old["tag_id"])
            ledger.record(
                conn,
                old["spool_id"],
                "tag_unbound",
                actor=actor,
                data={"tag_id": old["tag_id"], "reason": "rebound"},
            )
        row = conn.execute(
            "INSERT INTO filament_tag_bindings (tag_id, spool_id, override)"
            f" VALUES (%s, %s, %s) RETURNING {_BINDING_COLUMNS}",
            (tag_id, spool_id, override),
        ).fetchone()
        assert row is not None
        ledger.record(
            conn,
            spool_id,
            "tag_bound",
            actor=actor,
            data={"tag_id": tag_id, "override": override},
        )
        if override:
            FilamentSpoolStore._lock_weight(conn, spool_id, actor)
        if marks_origin:
            FilamentSpoolStore.set_origin(conn, spool_id, actor, data_origin=link_origin(tag_kind))
        enqueue_tag(conn, tag_id)
        return m.TagBinding.model_validate(row)

    @staticmethod
    def set_origin(
        conn: Conn,
        spool_id: UUID,
        actor: str,
        *,
        data_origin: str,
        tag_encoded_at: datetime | None = None,
    ) -> None:
        """How the spool's tag came in, and when it was written when that is known (#7149
        R-7149-1, R-7149-3), with one ``edited`` event of what changed, on ``conn``'s
        transaction. Only ScadBuddy's own writers call it: a person's bind and a confirmed
        NTAG write."""
        current = conn.execute(
            "SELECT data_origin, tag_encoded_at FROM filament_spools WHERE id = %s FOR UPDATE",
            (spool_id,),
        ).fetchone()
        if current is None:
            return
        after: dict[str, Any] = {"data_origin": data_origin}
        if tag_encoded_at is not None:
            after["tag_encoded_at"] = tag_encoded_at
        changes = {c: [current[c], v] for c, v in after.items() if current[c] != v}
        if not changes:
            return
        conn.execute(
            f"UPDATE filament_spools SET {', '.join(f'{c} = %s' for c in changes)},"
            " updated_at = now() WHERE id = %s",
            [*(changes[c][1] for c in changes), spool_id],
        )
        ledger.record(conn, spool_id, "edited", actor=actor, data={"changes": changes})

    @staticmethod
    def _lock_weight(conn: Conn, spool_id: UUID, actor: str) -> None:
        """Lock the spool's weight, with one ``edited`` event, unless it is locked."""
        changed = conn.execute(
            "UPDATE filament_spools SET weight_locked = true, updated_at = now()"
            " WHERE id = %s AND NOT weight_locked RETURNING id",
            (spool_id,),
        ).fetchone()
        if changed is not None:
            ledger.record(
                conn,
                spool_id,
                "edited",
                actor=actor,
                data={"changes": {"weight_locked": [False, True]}},
            )

    @staticmethod
    def tag_on(conn: Conn, uid: str | None, tray: str | None, kind: m.TagKind) -> UUID:
        """The id of the tag either cleaned key names, locked ``FOR UPDATE`` and given the
        key it lacked, else a new tag of ``kind``, on ``conn``'s transaction; for a caller
        that takes the tag before its spools (`filament/inventory.py`). Two tags, one per
        key, are `TagKeysClashError`."""
        return FilamentSpoolStore._tag(conn, uid, tray, kind)[0]

    @property
    def pool(self) -> ConnectionPool[Conn]:
        """The pool the store runs on, for a writer that locks printer rows first
        (`filament/capture.py` `sync_ams_weights`)."""
        return self._pool

    @contextmanager
    def transaction(self) -> Iterator[Conn]:
        """A connection in a transaction, for a caller that writes a spool, its tags and
        its events as one (`filament/bambuddy_pull.py`). Its COMMIT is a span
        (`core/tracing.py` `traced_transaction`, #7389)."""
        with self._pool.connection() as conn, traced_transaction(conn):
            yield conn

    @staticmethod
    def read(
        conn: Conn,
        spool_id: UUID,
        *,
        check: bool = False,
        defaults: FilamentDefaults = _SETTING_DEFAULTS,
    ) -> m.Spool | None:
        """The spool with its effective values, on ``conn``; with ``check``, a backwards
        effective temperature range is `TypeTempError`. Its ``low_stock`` follows
        ``defaults``: a caller that returns the spool passes the settings' `defaults()`."""
        spool = FilamentSpoolStore._spool(conn, spool_id, defaults)
        if check and spool is not None:
            _check_temps(spool.effective)
        return spool

    @staticmethod
    def read_many(conn: Conn, spool_ids: Iterable[UUID]) -> dict[UUID, m.Spool]:
        """`read` of each spool among ``spool_ids`` that exists, by id, in a fixed number
        of queries (#6902: the Bambuddy sync's plan reads every spool). Its hours outside
        dry storage and ``needs_drying`` stay at their defaults and ``low_stock`` follows
        the settings' defaults, so a caller that returns a spool reads it with `read`."""
        rows = conn.execute(
            f"SELECT {_SPOOL_COLUMNS} FROM filament_spools WHERE id = ANY(%s)",
            (list(spool_ids),),
        ).fetchall()
        return {spool.id: spool for spool in _hydrate(conn, rows, hours=False)}

    def unbind_tag(self, tag_id: UUID, *, actor: str) -> m.TagBinding | None:
        """Take a tag off its spool, with a ``tag_unbound`` event; ``None`` when the tag
        is on no spool. Locks the tag, then the spool, as `bind_tag` does."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT 1 FROM filament_tags WHERE id = %s FOR UPDATE", (tag_id,))
            holder = conn.execute(
                "SELECT spool_id FROM filament_tag_bindings"
                " WHERE tag_id = %s AND unbound_at IS NULL",
                (tag_id,),
            ).fetchone()
            if holder is None:
                return None
            conn.execute(
                "SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (holder["spool_id"],)
            )
            row = conn.execute(
                "UPDATE filament_tag_bindings SET unbound_at = now(), reason = 'unbound'"
                f" WHERE tag_id = %s AND unbound_at IS NULL RETURNING {_BINDING_COLUMNS}",
                (tag_id,),
            ).fetchone()
            if row is None:
                return None
            ledger.record(
                conn,
                row["spool_id"],
                "tag_unbound",
                actor=actor,
                data={"tag_id": tag_id, "reason": "unbound"},
            )
            enqueue_tag(conn, tag_id)
        return m.TagBinding.model_validate(row)

    def open_binding(self, spool_id: UUID) -> m.TagBinding | None:
        """The spool's tag binding now, if it has one."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_BINDING_COLUMNS} FROM filament_tag_bindings"
                " WHERE spool_id = %s AND unbound_at IS NULL",
                (spool_id,),
            ).fetchone()
        return None if row is None else m.TagBinding.model_validate(row)

    def events(self, spool_id: UUID) -> _Events:
        """The spool's ledger, oldest first."""
        with self._pool.connection() as conn:
            return ledger.events(conn, spool_id)

    def add_event(
        self, spool_id: UUID, body: m.SpoolEventAdd, *, actor: str
    ) -> m.SpoolEvent | None:
        """A person's weighing, drying or consumed reset (#7149 R-7149-4); ``None`` for
        no such spool. A drying's ``temp_c`` or ``hours`` left out (not sent) is the
        spool's drying default (`_drying_defaults`, #6720; the settings last, #7156). A
        reset whose ``at`` is not after the spool's Bambuddy link is
        `ResetBeforeLinkError` (R-7149-12). A weighing with ``reading_g`` keeps its
        breakdown (`ledger.breakdown`, #7682); one with no ``grams`` takes the spool's
        effective empty weight off the reading, `NoEmptyWeightError` when there is
        none and `ReadingBelowEmptyError` when the reading is under it. One with a
        ``resolution`` (#7683) is checked again under the row locks
        (`insert_weights.check_on`), `NoBelowLabelError` unless the check offers that
        resolution now; ``short`` first writes ``added_full`` false (unless it is false
        already) and ``label_wrong`` the spool's own ``label_weight_g``, each with an
        ``edited`` event at the weighing's time, in the same transaction; it keeps
        ``expected_g`` and records no ``drift_g``, since the person explained the gap."""
        data: dict[str, Any] = {} if body.note is None else {"note": body.note}
        # Read before the store takes its connection (`FilamentDefaults`' rule).
        settings = self._defaults() if body.kind == "dried" else _SETTING_DEFAULTS
        with self._pool.connection() as conn, conn.transaction():
            if (
                conn.execute(
                    "SELECT 1 FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
                ).fetchone()
                is None
            ):
                return None
            if body.kind == "consumed_reset" and body.at is not None:
                # FOR SHARE: the link checked stays as read until commit (no delete or relink).
                link = conn.execute(
                    "SELECT linked_at FROM filament_bb_links WHERE spool_id = %s FOR SHARE",
                    (spool_id,),
                ).fetchone()
                if link is not None and body.at <= link["linked_at"]:
                    raise ResetBeforeLinkError(link["linked_at"])
            if body.kind == "dried":
                sent = body.model_fields_set
                defaults = (
                    m.DryingDefaults()
                    if {"temp_c", "hours"} <= sent
                    else _drying_defaults(conn, spool_id, settings)
                )
                temp = body.temp_c if "temp_c" in sent else defaults.temp_c
                hours = body.hours if "hours" in sent else defaults.hours
                data |= {"temp_c": temp, "hours": None if hours is None else float(hours)}
            if body.kind == "weighed":
                return self._weigh_on(conn, spool_id, body, actor, data)
            return ledger.record(
                conn, spool_id, body.kind, source="person", at=body.at, actor=actor, data=data
            )

    def _weigh_on(
        self, conn: Conn, spool_id: UUID, body: m.SpoolEventAdd, actor: str, data: dict[str, Any]
    ) -> m.SpoolEvent | None:
        """`add_event`'s weighing, under the spool's row lock: the breakdown of a
        ``reading_g`` (#7682), a ``resolution``'s side effect (#7683), then the event. An
        archive's final weighing goes through here too (#7684)."""
        grams = body.grams
        if body.reading_g is not None:
            empty = effective_on(conn, spool_id)
            spool = conn.execute(
                "SELECT s.weight_locked, s.added_full,"
                " s.label_weight_g AS own_label_weight_g,"
                " coalesce(s.label_weight_g, t.label_weight_g) AS label_weight_g"
                " FROM filament_spools s LEFT JOIN filament_spool_types t ON t.id = s.type_id"
                " WHERE s.id = %s",
                (spool_id,),
            ).fetchone()
            assert spool is not None
            if body.resolution is not None:
                grams = self._resolve_below_label(conn, spool_id, body, spool, actor, data)
            elif grams is None:
                if empty is None:
                    raise NoEmptyWeightError()
                grams = body.reading_g - empty.grams
                if grams < 0:
                    raise ReadingBelowEmptyError()
            expected = ledger.expected_before(
                ledger.events(conn, spool_id),
                body.at,
                weight_locked=spool["weight_locked"],
                label_weight_g=spool["label_weight_g"],
            )
            data |= ledger.breakdown(
                reading_g=body.reading_g,
                grams=grams,
                empty=empty,
                expected=expected,
                tared=body.resolution == "tared",
                resolved=body.resolution is not None,
            )
        return ledger.record(
            conn,
            spool_id,
            "weighed",
            source=body.source,
            absolute_g=grams,
            at=body.at,
            actor=actor,
            data=data,
        )

    @staticmethod
    def _resolve_below_label(
        conn: Conn,
        spool_id: UUID,
        body: m.SpoolEventAdd,
        spool: Row,
        actor: str,
        data: dict[str, Any],
    ) -> float:
        """The filament of a weighing with a ``resolution`` (#7683), its side effect
        booked: `NoBelowLabelError` unless the check offers that resolution now."""
        assert body.reading_g is not None and body.resolution is not None
        check = check_on(conn, spool_id, body.reading_g, lock=True)
        assert check is not None
        chosen = next((r for r in check.resolutions if r.kind == body.resolution), None)
        if check.reason != "below_label" or chosen is None:
            raise NoBelowLabelError(check.reason)
        changes: dict[str, list[Any]] | None = None
        if chosen.kind == "short" and spool["added_full"] is not False:
            conn.execute(
                "UPDATE filament_spools SET added_full = false, updated_at = now() WHERE id = %s",
                (spool_id,),
            )
            changes = {"added_full": [spool["added_full"], False]}
        elif chosen.kind == "label_wrong":
            conn.execute(
                "UPDATE filament_spools SET label_weight_g = %s, updated_at = now() WHERE id = %s",
                (chosen.new_label_weight_g, spool_id),
            )
            changes = {"label_weight_g": [spool["own_label_weight_g"], chosen.new_label_weight_g]}
        if changes is not None:
            ledger.record(
                conn,
                spool_id,
                "edited",
                source="person",
                actor=actor,
                at=body.at,
                data={"changes": changes},
            )
        data |= {"resolution": chosen.kind, "shortfall_g": check.shortfall_g}
        return chosen.filament_g

    def reset_consumed(self, spool_ids: Sequence[UUID], *, actor: str) -> m.ConsumedResetDone:
        """A person's ``consumed_reset`` on each spool named, in one transaction, the spools
        locked in id order (#7149 R-7149-4); the grams left stay. An id that names no
        spool is answered, not refused."""
        wanted = sorted(set(spool_ids))
        with self._pool.connection() as conn, conn.transaction():
            found = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    (wanted,),
                ).fetchall()
            }
            for spool_id in wanted:
                if spool_id in found:
                    ledger.record(
                        conn, spool_id, ledger.CONSUMED_RESET, source="person", actor=actor
                    )
        return m.ConsumedResetDone(
            reset=len(found), not_found=[i for i in dict.fromkeys(spool_ids) if i not in found]
        )

    def bulk_update(
        self, spool_ids: Sequence[UUID], patch: m.SpoolPatch, *, actor: str
    ) -> m.SpoolBulkUpdated:
        """``patch`` on each spool named, in one transaction, the spools locked in id
        order (#7151, R-7151-1): each its own ``edited`` event, as `update` writes it, and
        none for a spool already so, which is ``unchanged``. All or none: a spool the
        patch refuses is `SpoolBulkRefusedError` (its number), a row it names that does
        not exist `CatalogRefError`. An id that names no spool is answered, not
        refused."""
        wanted = sorted(set(spool_ids))
        changed: _Ids = []
        unchanged: _Ids = []
        try:
            with self._pool.connection() as conn, conn.transaction():
                rows = conn.execute(
                    "SELECT id, number FROM filament_spools WHERE id = ANY(%s)"
                    " ORDER BY id FOR UPDATE",
                    (wanted,),
                ).fetchall()
                for row in rows:
                    try:
                        did = self._update_on(conn, row["id"], patch, actor=actor)
                    except (SpoolTypelessError, TypeTempError) as exc:
                        raise SpoolBulkRefusedError(row["number"], exc) from None
                    (changed if did else unchanged).append(row["id"])
        except errors.ForeignKeyViolation:
            raise CatalogRefError() from None
        found = {row["id"] for row in rows}
        return m.SpoolBulkUpdated(
            changed=changed,
            unchanged=unchanged,
            not_found=[i for i in dict.fromkeys(spool_ids) if i not in found],
        )

    def bulk_archive(
        self, spool_ids: Sequence[UUID], archived: bool, *, actor: str
    ) -> m.SpoolsArchived:
        """Archive (``True``) or restore each spool named, in one transaction (#7151,
        R-7151-2): their open tags locked first, then the spools, each in id order, as
        every writer of bindings takes them. Each spool changed gets its own event and,
        archived, its binding closed, as `archive` does. A spool already so is
        ``unchanged``; an id that names no spool is answered in ``not_found``."""
        wanted = sorted(set(spool_ids))
        changed: _Ids = []
        unchanged: _Ids = []
        with self._pool.connection() as conn, conn.transaction():
            if archived:
                conn.execute(
                    "SELECT t.id FROM filament_tags t JOIN filament_tag_bindings b"
                    " ON b.tag_id = t.id WHERE b.spool_id = ANY(%s) AND b.unbound_at IS NULL"
                    " ORDER BY t.id FOR UPDATE OF t",
                    (wanted,),
                )
            rows = conn.execute(
                "SELECT id, archived_at FROM filament_spools WHERE id = ANY(%s)"
                " ORDER BY id FOR UPDATE",
                (wanted,),
            ).fetchall()
            for row in rows:
                if (row["archived_at"] is not None) == archived:
                    unchanged.append(row["id"])
                    continue
                self.update_on(conn, row["id"], m.SpoolPatch(archived=archived), actor=actor)
                changed.append(row["id"])
        found = {row["id"] for row in rows}
        return m.SpoolsArchived(
            changed=changed,
            unchanged=unchanged,
            not_found=[i for i in dict.fromkeys(spool_ids) if i not in found],
        )

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _spool(
        conn: Conn,
        spool_id: UUID,
        defaults: FilamentDefaults = _SETTING_DEFAULTS,
        now: datetime | None = None,
        hours: bool = True,
    ) -> m.Spool | None:
        row = conn.execute(
            f"SELECT {_SPOOL_COLUMNS} FROM filament_spools WHERE id = %s", (spool_id,)
        ).fetchone()
        return None if row is None else _hydrate(conn, [row], defaults, now, hours)[0]

    @staticmethod
    def tag_row_on(conn: Conn, tag_id: UUID) -> m.TagRow | None:
        """The tag with its open binding and its spool's number, on ``conn``."""
        row = conn.execute(
            f"SELECT {_TAG_COLUMNS_T}, {_BINDING_COLUMNS_B}, s.number AS spool_number"
            " FROM filament_tags t LEFT JOIN filament_tag_bindings b"
            " ON b.tag_id = t.id AND b.unbound_at IS NULL"
            " LEFT JOIN filament_spools s ON s.id = b.spool_id WHERE t.id = %s",
            (tag_id,),
        ).fetchone()
        return None if row is None else FilamentSpoolStore._tag_row(row)

    @staticmethod
    def _tag_row(row: Row) -> m.TagRow:
        binding = (
            None
            if row["binding_id"] is None
            else m.TagBinding.model_validate(
                {f: row[f"binding_{f}"] for f in m.TagBinding.model_fields}
            )
        )
        return m.TagRow.model_validate(
            {f: row[f] for f in m.Tag.model_fields}
            | {"binding": binding, "spool_number": row["spool_number"]}
        )

    @staticmethod
    def _lock(conn: Conn, spool_id: UUID) -> Row | None:
        return conn.execute(
            "SELECT id FROM filament_spools WHERE id = %s FOR UPDATE", (spool_id,)
        ).fetchone()

    @staticmethod
    def _edited(
        conn: Conn, spool_id: UUID, field: str, before: Any, after: Any, actor: str
    ) -> None:
        ledger.record(
            conn, spool_id, "edited", actor=actor, data={"changes": {field: [before, after]}}
        )

    def _check(self, conn: Conn, spool_id: UUID) -> None:
        spool = self._spool(conn, spool_id, hours=False)
        assert spool is not None
        _check_temps(spool.effective)

    @staticmethod
    def _archive(
        conn: Conn,
        current: Row,
        archived: bool,
        actor: str,
        at: datetime | None = None,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Archive or restore the locked row ``current``. ``data`` is the ``archived``
        event's closing (`ledger.closing`, #7684); a restore carries none."""
        if (current["archived_at"] is not None) == archived:
            return
        if archived:
            closed = conn.execute(
                "UPDATE filament_tag_bindings SET unbound_at = now(), reason = 'archived'"
                " WHERE spool_id = %s AND unbound_at IS NULL RETURNING tag_id",
                (current["id"],),
            ).fetchone()
            if closed is not None:
                ledger.record(
                    conn,
                    current["id"],
                    "tag_unbound",
                    actor=actor,
                    at=at,
                    data={"tag_id": closed["tag_id"], "reason": "archived"},
                )
                enqueue_tag(conn, closed["tag_id"])
        conn.execute(
            "UPDATE filament_spools SET archived_at = CASE WHEN %s THEN now() END,"
            " updated_at = now() WHERE id = %s",
            (archived, current["id"]),
        )
        if archived:
            # #6265 R-T3-5: an archived spool's sync error leaves the status. Its create
            # intent stays: a restore adopts the row a lost create made (Decision 10).
            conn.execute(
                "DELETE FROM filament_bb_push_errors WHERE kind = 'spool' AND sb_id = %s",
                (current["id"],),
            )
        ledger.record(
            conn,
            current["id"],
            "archived" if archived else "restored",
            actor=actor,
            at=at,
            data=data if archived else None,
        )

    @staticmethod
    def _tag(conn: Conn, uid: str | None, tray: str | None, kind: m.TagKind) -> tuple[UUID, str]:
        """The id and kind of the tag either key names, locked and given the key it lacked;
        else a new tag of ``kind``. A tag another transaction makes first is found on the
        second look."""
        while True:
            found = conn.execute(
                f"SELECT {_TAG_COLUMNS} FROM filament_tags"
                " WHERE tag_uid = %s OR tray_uuid = %s FOR UPDATE",
                (uid, tray),
            ).fetchall()
            if len(found) > 1:
                raise TagKeysClashError()
            if found:
                tag = found[0]
                if (uid is not None and tag["tag_uid"] is None) or (
                    tray is not None and tag["tray_uuid"] is None
                ):
                    conn.execute(
                        "UPDATE filament_tags SET tag_uid = coalesce(tag_uid, %s),"
                        " tray_uuid = coalesce(tray_uuid, %s) WHERE id = %s",
                        (uid, tray, tag["id"]),
                    )
                tag_id: UUID = tag["id"]
                return tag_id, tag["kind"]
            made = conn.execute(
                "INSERT INTO filament_tags (kind, tag_uid, tray_uuid) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING RETURNING id",
                (kind, uid, tray),
            ).fetchone()
            if made is not None:
                new_id: UUID = made["id"]
                return new_id, kind
