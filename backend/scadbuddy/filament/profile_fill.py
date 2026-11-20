"""A spool type's print values from its slicer filament profile (#6714; rulings in
`docs/superpowers/plans/2026-11-20-filament-type-from-profile.md`, "Decisions").

The profile is the preset of the type's slicer presets that :func:`choose_preset` picks
(the H2C 0.4 mm one, else any 0.4 mm one, else the first). It is read in the order
`orders/density.py` reads a density: a numeric id is a Bambuddy local preset, read while
Bambuddy is linked, over its ``inherits`` preset in the image's tree; any other id is the
tree's preset of that ``filament_id`` for the printer and nozzle (else its ``@base``), or
of that ``setting_id``, flattened through ``inherits``. A read that fails leaves the type
alone and says why.

:func:`profile_values` maps it onto the type: the nozzle range from
``nozzle_temperature_range_low/high``, the bed range from the H2C's plates
(:data:`PLATES`, values above 0), ``filament_density`` and ``filament_diameter``. The
type has no drying fields, so the profile's drying keys are not read. ``filament_cost``
is only a hint (:func:`cost_hint`), never written.

A fill writes only the type's empty fields (``diameter_mm`` is never empty). A refresh
answers what differs, with the values now, and writes nothing; an apply writes the
fields a person confirmed, once the profile still gives them. A type with no K values
takes the ``k_profiles`` of the Bambuddy spools its spools are linked to.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import LocalPresetDetail, Spool
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.density import DensityTable, density_text
from scadbuddy.slicing.profiles import ProfileError

#: The preset a type is filled from first: the owner's printer and its usual nozzle.
PRINTER_MODEL = "H2C"
NOZZLE = Decimal("0.4")

#: The H2C's plates: ``BBL/machine/Bambu Lab H2C.json`` in Bambu Studio 02.08.02.61 says
#: ``not_support_bed_type: "Cool Plate;Smooth PEI Plate / High Temp Plate"`` (read in the
#: API image on 2026-11-20), which leaves Cool Plate (SuperTack), Engineering Plate and
#: Textured PEI Plate.
PLATES = ("supertack_plate_temp", "eng_plate_temp", "textured_plate_temp")

Field = Literal[
    "nozzle_temp_min",
    "nozzle_temp_max",
    "bed_temp_min",
    "bed_temp_max",
    "density_g_cm3",
    "diameter_mm",
]
Value = int | Decimal


logger = logging.getLogger(__name__)


class ProfileSource(BaseModel):
    """The preset a type was filled from: the type's preset row and the profile read."""

    #: The type preset's ``slicer_filament``.
    preset_id: str
    #: The profile's name: the local preset's, or the tree's preset's.
    name: str
    origin: Literal["bambuddy_local", "system"]
    printer_model: str
    nozzle_diameter: str


@dataclass(frozen=True)
class Profile:
    values: Mapping[str, Any]
    source: ProfileSource


@dataclass(frozen=True)
class Unread:
    """Why no profile was read."""

    reason: str


class LocalPresets(Protocol):
    async def local_preset(self, preset_id: int) -> LocalPresetDetail: ...


def _nozzle(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def canonical_nozzle(value: str) -> str:
    """A nozzle diameter as the presets and printer names spell it: ``0.40`` is ``0.4``
    (R-6714-d); text that is no number stays as it is."""
    number = _nozzle(value.strip())
    if number is None or not number.is_finite():
        return value
    return format(number.normalize(), "f")


def choose_preset(presets: Sequence[m.TypePreset]) -> m.TypePreset | None:
    """The type's H2C 0.4 mm preset, else any 0.4 mm one, else the first (R-6714-1)."""
    at_04 = [p for p in presets if _nozzle(p.nozzle_diameter) == NOZZLE]
    h2c = [p for p in at_04 if p.printer_model.strip().upper() == PRINTER_MODEL]
    return next(iter(h2c or at_04 or presets), None)


def _text(value: Any) -> str | None:
    """A profile value as one text: a list's first item; empty and ``nil`` are none."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if value == "" or value.lower() == "nil" else value


def _number(value: Any) -> Decimal | None:
    text = _text(value)
    if text is None:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _temp(value: Any) -> int | None:
    """A temperature a type holds (0 to 1000); 0 is none (a plate the filament is not
    for, or a range left unset)."""
    number = _number(value)
    if number is None or not Decimal(0) < number <= Decimal(1000):
        return None
    return int(number)


def profile_values(flat: Mapping[str, Any]) -> dict[Field, Value]:
    """The type's fields a profile gives (R-6714-3)."""
    found: dict[Field, Value] = {}
    names: tuple[tuple[Field, str], ...] = (
        ("nozzle_temp_min", "nozzle_temperature_range_low"),
        ("nozzle_temp_max", "nozzle_temperature_range_high"),
    )
    for name, key in names:
        if (temp := _temp(flat.get(key))) is not None:
            found[name] = temp
    plates = [t for key in PLATES if (t := _temp(flat.get(key))) is not None]
    if plates:
        found["bed_temp_min"], found["bed_temp_max"] = min(plates), max(plates)
    density = density_text(_text(flat.get("filament_density")))
    if density is not None:
        found["density_g_cm3"] = Decimal(density)
    diameter = _number(flat.get("filament_diameter"))
    if diameter is not None and Decimal(0) < diameter < Decimal(100):
        found["diameter_mm"] = diameter
    return found


def cost_hint(flat: Mapping[str, Any]) -> str | None:
    """The profile's ``filament_cost``, as text, when it is above 0."""
    cost = _number(flat.get("filament_cost"))
    return None if cost is None or cost <= 0 else format(cost.normalize(), "f")


def _over_parent(setting: Mapping[str, Any], table: DensityTable | None) -> dict[str, Any]:
    """A local preset's own setting over its ``inherits`` preset in the tree, when the
    tree has it. Blocking."""
    parent = setting.get("inherits")
    profiles = None if table is None else table.profiles
    base: dict[str, Any] = {}
    if profiles is not None and isinstance(parent, str) and profiles.find("filament", parent):
        try:
            base = dict(profiles.filament(parent))
        except ProfileError:
            base = {}
    return base | dict(setting)


async def read_profile(
    preset: m.TypePreset, table: DensityTable | None, local: LocalPresets | None
) -> Profile | Unread:
    """The profile ``preset`` names, in `orders/density.py`'s order (R-6714-2)."""
    preset_id = preset.slicer_filament.strip()
    source = {
        "preset_id": preset_id,
        "printer_model": preset.printer_model,
        "nozzle_diameter": preset.nozzle_diameter,
    }
    if preset_id.isdigit():
        if local is None:
            return Unread(
                f"preset {preset_id} is a Bambuddy local preset, and no Bambuddy is linked"
            )
        try:
            detail = await local.local_preset(int(preset_id))
        except ApiError as error:
            return Unread(f"Bambuddy did not give local preset {preset_id}: {error.detail}")
        except ValidationError:
            return Unread(f"Bambuddy answered local preset {preset_id} in another shape")
        values = await asyncio.to_thread(_over_parent, detail.setting, table)
        return Profile(values, ProfileSource(name=detail.name, origin="bambuddy_local", **source))
    profiles = None if table is None else table.profiles
    if table is None or profiles is None:
        return Unread(f"this image has no slicer profiles to read preset {preset_id} from")
    printer = f"Bambu Lab {preset.printer_model} {canonical_nozzle(preset.nozzle_diameter)} nozzle"
    name = table.preset_name(preset_id, printer)
    if name is None:
        return Unread(f"Bambu Studio {profiles.version} has no filament preset {preset_id}")
    try:
        flat = await asyncio.to_thread(profiles.filament, name)
    except ProfileError as error:
        return Unread(str(error))
    return Profile(flat, ProfileSource(name=name, origin="system", **source))


# -- the fill -------------------------------------------------------------------------


class TypeFill(BaseModel):
    """What a fill did, or a refresh would do, to one type (R-6714-4)."""

    type_id: UUID
    code: str
    #: The fields filled (or, on a refresh, the profile's values that differ).
    filled: dict[Field, Value] = {}
    #: On a refresh or an apply: each of those fields' value before.
    previous: dict[Field, Value | None] = {}
    #: A field the profile gives but that was not filled, and why.
    skipped: dict[Field, str] = {}
    source: ProfileSource | None = None
    #: Why no profile was read; ``None`` when one was.
    reason: str | None = None
    #: The profile's ``filament_cost``: a hint, never written.
    cost_hint: str | None = None
    #: The K values imported from Bambuddy's spools (on a refresh, that would be).
    k_values: list[m.TypeKValue] = []
    #: Why the K values could not be read.
    k_reason: str | None = None
    #: Whether anything was written.
    written: bool = False


class FillRequest(BaseModel):
    """`refresh`: answer the profile's values that differ, set or not, and write nothing.
    `apply`: write these fields, as a refresh showed them; `k_values`, with it, the K
    values too."""

    refresh: bool = False
    apply: dict[Field, Value] | None = None
    #: With ``apply``: also write the K values a refresh showed, while the type has none.
    k_values: bool = False


class BulkFillRequest(BaseModel):
    refresh: bool = False


class ProfileChangedError(ValueError):
    """The profile no longer gives a value a person confirmed."""


_PAIRS: tuple[tuple[Field, Field], ...] = (
    ("nozzle_temp_min", "nozzle_temp_max"),
    ("bed_temp_min", "bed_temp_max"),
)


def _ranges(current: Mapping[str, Any], wanted: dict[Field, Value]) -> dict[Field, str]:
    """Drop from ``wanted`` a pair it would leave with a minimum above its maximum; the
    fields dropped, and why."""
    skipped: dict[Field, str] = {}
    for low_key, high_key in _PAIRS:
        low = wanted.get(low_key, current.get(low_key))
        high = wanted.get(high_key, current.get(high_key))
        if low is not None and high is not None and low > high:
            for key in (low_key, high_key):
                if wanted.pop(key, None) is not None:
                    skipped[key] = f"{low_key} {low} would be above {high_key} {high}"
    return skipped


class _Reads:
    """One fill's Bambuddy reads, each made at most once."""

    def __init__(self, client: BambuddyClient | None) -> None:
        self.client = client
        self._presets: dict[int, LocalPresetDetail | ApiError | ValidationError] = {}
        self._spools: dict[int, Spool] | Unread | None = None

    async def local_preset(self, preset_id: int) -> LocalPresetDetail:
        assert self.client is not None
        if preset_id not in self._presets:
            try:
                self._presets[preset_id] = await self.client.local_preset(preset_id)
            except (ApiError, ValidationError) as error:
                self._presets[preset_id] = error
        found = self._presets[preset_id]
        if isinstance(found, Exception):
            raise found
        return found

    @property
    def local(self) -> LocalPresets | None:
        return None if self.client is None else self

    async def spools(self) -> dict[int, Spool] | Unread:
        """Bambuddy's spools by id; a row of another shape is left out."""
        if self._spools is None:
            if self.client is None:
                self._spools = Unread("no Bambuddy is linked")
            else:
                try:
                    rows = await self.client.spool_rows(include_archived=True)
                except ApiError as error:
                    self._spools = Unread(f"Bambuddy did not list its spools: {error.detail}")
                else:
                    found: dict[int, Spool] = {}
                    for row in rows:
                        try:
                            spool = Spool.model_validate(row)
                        except ValidationError:
                            continue
                        found[spool.id] = spool
                    self._spools = found
        return self._spools


def _rows(type_id: UUID, rows: list[m.TypeKValueCreate]) -> list[m.TypeKValue]:
    return [m.TypeKValue(type_id=type_id, **one.model_dump()) for one in rows]


@asynccontextmanager
async def _bambuddy(settings: StoredSettings) -> AsyncIterator[BambuddyClient | None]:
    """A client while Bambuddy is linked, else ``None``."""
    try:
        config = BambuddyConfig.from_settings(settings)
    except ApiError:
        yield None
        return
    async with BambuddyClient(config) as client:
        yield client


async def fill_quietly(
    filler: TypeFiller,
    load: Callable[[], StoredSettings],
    type_ids: Sequence[UUID],
    *,
    actor: str,
) -> None:
    """``filler.fill`` after a write has committed: a failure is logged with its error
    label and never fails the request that wrote (R-6714-b)."""
    try:
        await filler.fill(await asyncio.to_thread(load), list(type_ids), actor=actor)
    except Exception as error:
        logger.warning(
            "could not fill the types from their slicer profiles",
            extra={"error": type(error).__name__},
        )


@dataclass
class TypeFiller:
    """Fills spool types from their profiles over the type store, with the image's tree
    (``table``, built once per process and blocking: it runs in a thread)."""

    types: SpoolTypeStore
    table: Callable[[], DensityTable | None]

    async def fill(
        self,
        settings: StoredSettings,
        type_ids: Sequence[UUID] | None,
        *,
        actor: str,
        refresh: bool = False,
    ) -> list[TypeFill]:
        """Fill (or, with ``refresh``, compare) each type of ``type_ids`` that exists, or
        every unarchived type for ``None``. A write fans out to the type's spools as
        ``actor`` (R-T9-4)."""
        if type_ids is None:
            rows = await asyncio.to_thread(self.types.list)
        else:
            found = [await asyncio.to_thread(self.types.get, i) for i in type_ids]
            rows = [row for row in found if row is not None]
        table = await asyncio.to_thread(self.table)
        async with _bambuddy(settings) as client:
            reads = _Reads(client)
            return [
                await self._one(row, table, reads, actor=actor, refresh=refresh) for row in rows
            ]

    async def apply(
        self,
        settings: StoredSettings,
        type_id: UUID,
        values: Mapping[Field, Value],
        *,
        actor: str,
        k_values: bool = False,
    ) -> TypeFill | None:
        """Write ``values`` over the type's, once its profile still gives each of them
        (`ProfileChangedError` when it does not), and with ``k_values`` the K values a
        refresh showed, while the type still has none. ``None`` for no such type."""
        row = await asyncio.to_thread(self.types.get, type_id)
        if row is None:
            return None
        table = await asyncio.to_thread(self.table)
        rows: list[m.TypeKValueCreate] = []
        async with _bambuddy(settings) as client:
            reads = _Reads(client)
            result, profile = await self._read(row, table, reads)
            if values:
                if profile is None:
                    raise ProfileChangedError(f"the profile could not be read: {result.reason}")
                given = profile_values(profile.values)
                for name, value in values.items():
                    if given.get(name) != value:
                        raise ProfileChangedError(
                            f"the profile now gives {name} {given.get(name)}, not {value}:"
                            " refresh again"
                        )
            if k_values:
                rows, result.k_reason = await self._k_values(type_id, reads)
        if values:
            current = row.model_dump()
            result.previous = {name: current[name] for name in values}
            patch = m.SpoolTypePatch.model_validate(dict(values))
            await asyncio.to_thread(self.types.update, type_id, patch, actor=actor)
            result.filled = dict(values)
            result.written = True
        await self._write_k(result, rows, actor)
        return result

    async def _write_k(self, result: TypeFill, rows: list[m.TypeKValueCreate], actor: str) -> None:
        """Write ``rows`` as the type's K values while it has none, into ``result``."""
        if not rows:
            return
        saved = await asyncio.to_thread(
            self.types.fill_k_values, result.type_id, _rows(result.type_id, rows), actor=actor
        )
        if saved is not None:
            result.k_values = saved
            result.written = True

    async def _read(
        self, row: m.SpoolType, table: DensityTable | None, reads: _Reads
    ) -> tuple[TypeFill, Profile | None]:
        result = TypeFill(type_id=row.id, code=row.code)
        chosen = choose_preset(await asyncio.to_thread(self.types.presets, row.id))
        if chosen is None:
            result.reason = "the type has no slicer preset"
            return result, None
        read = await read_profile(chosen, table, reads.local)
        if isinstance(read, Unread):
            result.reason = read.reason
            return result, None
        result.source = read.source
        result.cost_hint = cost_hint(read.values)
        return result, read

    async def _one(
        self,
        row: m.SpoolType,
        table: DensityTable | None,
        reads: _Reads,
        *,
        actor: str,
        refresh: bool,
    ) -> TypeFill:
        result, profile = await self._read(row, table, reads)
        if profile is not None:
            current = row.model_dump()
            given = profile_values(profile.values)
            wanted: dict[Field, Value] = {
                name: value
                for name, value in given.items()
                if (current[name] != value if refresh else current[name] is None)
            }
            result.skipped = _ranges(current, wanted)
            if refresh:
                result.filled = wanted
                result.previous = {name: current[name] for name in wanted}
            elif wanted:
                written = await asyncio.to_thread(
                    self.types.fill_empty,
                    row.id,
                    {str(k): v for k, v in wanted.items()},
                    actor=actor,
                )
                result.filled = {
                    name: value for name, value in wanted.items() if name in (written or {})
                }
                result.written = bool(written)
        rows, result.k_reason = await self._k_values(row.id, reads)
        if refresh:
            result.k_values = _rows(row.id, rows)
        else:
            await self._write_k(result, rows, actor)
        return result

    async def _k_values(
        self, type_id: UUID, reads: _Reads
    ) -> tuple[list[m.TypeKValueCreate], str | None]:
        """The K values the type's linked Bambuddy spools have, when the type has none
        (R-6714-5): one per printer, extruder and nozzle (the table's key), the newest
        spool's first; a K profile of a printer no ScadBuddy printer links is left out."""
        if await asyncio.to_thread(self.types.k_values, type_id):
            return [], None
        linked = await asyncio.to_thread(self.types.linked_bb_spools, type_id)
        if not linked:
            return [], None
        spools = await reads.spools()
        if isinstance(spools, Unread):
            return [], spools.reason
        printers = await asyncio.to_thread(self.types.printers_by_bambuddy_id)
        rows: list[m.TypeKValueCreate] = []
        keys: set[tuple[UUID, int, Decimal | str]] = set()
        for bb_id in linked:
            spool = spools.get(bb_id)
            for k in [] if spool is None else spool.k_profiles:
                printer = None if k.printer_id is None else printers.get(k.printer_id)
                if printer is None or k.k_value is None or k.extruder is None:
                    continue
                nozzle = (k.nozzle_diameter or "").strip()
                key = (printer, k.extruder, _nozzle(nozzle) or nozzle)
                if key in keys:
                    continue
                try:
                    one = m.TypeKValueCreate(
                        printer_id=printer,
                        extruder=k.extruder,
                        nozzle_diameter=nozzle,
                        nozzle_type=k.nozzle_type,
                        k_value=Decimal(str(k.k_value)),
                        name=k.name,
                        cali_idx=k.cali_idx,
                        setting_id=k.setting_id,
                    )
                except ValidationError:
                    continue
                keys.add(key)
                rows.append(one)
        return rows, None
