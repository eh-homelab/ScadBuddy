"""The models of ScadBuddy's filament inventory (#6261, epic #6260): one set per table
of `migrations/20261114T1300Z_filament_catalogs.sql` and `..T1301Z_filament_spools.sql`.

For a thing ``X`` there is ``XCreate`` (what a person sends to add one), ``XPatch`` (only
the fields sent change, by ``model_fields_set``; ``null`` clears a nullable column and is
refused for a NOT NULL one) and ``X`` (what a row reads back as, so ``X.model_validate(row)``
takes a psycopg ``dict_row``). Numeric columns come back from psycopg as ``Decimal`` and
are typed so. Every colour is an `Rgba`: ``#rrggbb`` is stored as ``#RRGGBBFF``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from scadbuddy.filament.colour import Rgba

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
SortOrder = Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
Idx = Annotated[int, Field(ge=0, le=1000)]
CaliIdx = Annotated[int, Field(ge=-1, le=1_000_000)]
Text = Annotated[str, Field(max_length=128)]
Note = Annotated[str, Field(max_length=2000)]
Temp = Annotated[int, Field(ge=0, le=1000)]
Density = Annotated[Decimal, Field(gt=0, lt=1000)]
Diameter = Annotated[Decimal, Field(gt=0, lt=100)]
Hours = Annotated[Decimal, Field(ge=0, lt=1000)]
Grams = Annotated[float, Field(ge=0, le=1_000_000)]
LabelWeight = Annotated[int, Field(gt=0, le=100_000)]
Percent = Annotated[int, Field(ge=0, le=100)]
Cost = Annotated[Decimal, Field(ge=0, lt=100_000_000)]
Colours = Annotated[list[Rgba], Field(max_length=16)]
#: How far past the server's clock a person's ``at`` may be (their clock may run ahead).
FUTURE_SKEW = timedelta(minutes=5)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Patch(_In):
    """Only the fields sent change. ``not_null`` names the fields whose column is NOT NULL:
    sending one as ``null`` is an error, not a clear."""

    not_null: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _no_null_for_required(self) -> Any:
        for field in self.model_fields_set & (self.not_null | {"archived"}):
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be cleared")
        return self


class _Row(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime


class _Catalog(_Row):
    name: str
    sort_order: int
    archived_at: datetime | None


# -- the plain catalogs: brand, category, effect, insert material --------------------


class _NamedCreate(_In):
    name: Name
    sort_order: SortOrder = 0


class _NamedPatch(_Patch):
    not_null = frozenset({"name", "sort_order"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    archived: bool | None = None


class BrandCreate(_NamedCreate): ...


class BrandPatch(_NamedPatch): ...


class Brand(_Catalog): ...


class CategoryCreate(_NamedCreate): ...


class CategoryPatch(_NamedPatch): ...


class Category(_Catalog): ...


class EffectCreate(_NamedCreate): ...


class EffectPatch(_NamedPatch): ...


class Effect(_Catalog): ...


class InsertMaterialCreate(_NamedCreate): ...


class InsertMaterialPatch(_NamedPatch): ...


class InsertMaterial(_Catalog): ...


# -- material and subtype -------------------------------------------------------------


class MaterialCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    family: Text | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    needs_enclosure: bool = False
    abrasive: bool = False
    hygroscopic: bool = False
    flexible: bool = False
    hardened_nozzle: bool = False
    note: Note | None = None


class MaterialPatch(_Patch):
    not_null = frozenset(
        {
            "name",
            "sort_order",
            "needs_enclosure",
            "abrasive",
            "hygroscopic",
            "flexible",
            "hardened_nozzle",
        }
    )
    name: Name | None = None
    sort_order: SortOrder | None = None
    family: Text | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    needs_enclosure: bool | None = None
    abrasive: bool | None = None
    hygroscopic: bool | None = None
    flexible: bool | None = None
    hardened_nozzle: bool | None = None
    note: Note | None = None
    archived: bool | None = None


class Material(_Catalog):
    family: str | None
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    drying_temp_c: int | None
    drying_hours: Decimal | None
    needs_enclosure: bool
    abrasive: bool
    hygroscopic: bool
    flexible: bool
    hardened_nozzle: bool
    note: str | None


class SubtypeCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    material_id: UUID | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None


class SubtypePatch(_Patch):
    not_null = frozenset({"name", "sort_order"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    material_id: UUID | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    archived: bool | None = None


class Subtype(_Catalog):
    material_id: UUID | None
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    drying_temp_c: int | None
    drying_hours: Decimal | None


# -- insert types, named colours ------------------------------------------------------


class InsertTypeCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    empty_weight_g: Annotated[int, Field(gt=0, le=100_000)]
    insert_material_id: UUID | None = None
    note: Note | None = None


class InsertTypePatch(_Patch):
    not_null = frozenset({"name", "sort_order", "empty_weight_g"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    empty_weight_g: Annotated[int, Field(gt=0, le=100_000)] | None = None
    insert_material_id: UUID | None = None
    note: Note | None = None
    archived: bool | None = None


class InsertType(_Catalog):
    empty_weight_g: int
    insert_material_id: UUID | None
    note: str | None


class ColourCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    manufacturer: Text | None = None
    rgba: Rgba
    material_id: UUID | None = None
    extra_colours: Colours | None = None
    effect_id: UUID | None = None


class ColourPatch(_Patch):
    not_null = frozenset({"name", "sort_order", "rgba"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    manufacturer: Text | None = None
    rgba: Rgba | None = None
    material_id: UUID | None = None
    extra_colours: Colours | None = None
    effect_id: UUID | None = None
    archived: bool | None = None


class Colour(_Catalog):
    manufacturer: str | None
    rgba: Rgba
    material_id: UUID | None
    extra_colours: list[Rgba] | None
    effect_id: UUID | None


# -- locations ------------------------------------------------------------------------


class LocationKindCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    dry_storage: bool = False
    ams: bool = False


class LocationKindPatch(_Patch):
    not_null = frozenset({"name", "sort_order", "dry_storage", "ams"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    dry_storage: bool | None = None
    ams: bool | None = None
    archived: bool | None = None


class LocationKind(_Catalog):
    dry_storage: bool
    ams: bool


class LocationCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    kind_id: UUID
    parent_id: UUID | None = None
    identifier: Text | None = None
    capacity: Annotated[int, Field(gt=0, le=100_000)] | None = None
    printer_id: UUID | None = None
    ams_id: Idx | None = None
    tray_id: Idx | None = None
    note: Note | None = None


class LocationPatch(_Patch):
    not_null = frozenset({"name", "sort_order", "kind_id"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    kind_id: UUID | None = None
    parent_id: UUID | None = None
    identifier: Text | None = None
    capacity: Annotated[int, Field(gt=0, le=100_000)] | None = None
    printer_id: UUID | None = None
    ams_id: Idx | None = None
    tray_id: Idx | None = None
    note: Note | None = None
    archived: bool | None = None


class Location(_Catalog):
    kind_id: UUID
    parent_id: UUID | None
    identifier: str | None
    capacity: int | None
    printer_id: UUID | None
    ams_id: int | None
    tray_id: int | None
    note: str | None


class LocationNode(Location):
    """A place in the tree: the unarchived spools in it, and the places under it."""

    spool_count: int
    children: list[LocationNode] = Field(default_factory=list)


class SpoolMove(_In):
    """Where a spool goes; ``null`` takes it out of every place."""

    location_id: UUID | None


# -- spool types ----------------------------------------------------------------------


class SpoolTypeCreate(_In):
    code: Name
    brand_id: UUID | None = None
    material_id: UUID
    subtype_id: UUID | None = None
    category_id: UUID | None = None
    effect_id: UUID | None = None
    colour_name: Text | None = None
    rgba: Rgba
    extra_colours: Colours | None = None
    diameter_mm: Diameter = Decimal("1.75")
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    label_weight_g: LabelWeight
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    note: Note | None = None


class SpoolTypePatch(_Patch):
    not_null = frozenset({"code", "material_id", "rgba", "diameter_mm", "label_weight_g"})
    code: Name | None = None
    brand_id: UUID | None = None
    material_id: UUID | None = None
    subtype_id: UUID | None = None
    category_id: UUID | None = None
    effect_id: UUID | None = None
    colour_name: Text | None = None
    rgba: Rgba | None = None
    extra_colours: Colours | None = None
    diameter_mm: Diameter | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    label_weight_g: LabelWeight | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    note: Note | None = None
    archived: bool | None = None


class SpoolType(_Row):
    code: str
    brand_id: UUID | None
    material_id: UUID
    subtype_id: UUID | None
    category_id: UUID | None
    effect_id: UUID | None
    colour_name: str | None
    rgba: Rgba
    extra_colours: list[Rgba] | None
    diameter_mm: Decimal
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    label_weight_g: int
    insert_type_id: UUID | None
    low_stock_pct: int | None
    cost_per_kg: Decimal | None
    filament_id: str | None
    note: str | None
    archived_at: datetime | None


class TypePresetCreate(_In):
    printer_model: Name
    nozzle_diameter: Name
    slicer_filament: Name
    slicer_filament_name: Text | None = None


class TypePresetPatch(_Patch):
    not_null = frozenset({"slicer_filament"})
    slicer_filament: Name | None = None
    slicer_filament_name: Text | None = None


class TypePreset(BaseModel):
    type_id: UUID
    printer_model: str
    nozzle_diameter: str
    slicer_filament: str
    slicer_filament_name: str | None


class TypeKValueCreate(_In):
    printer_id: UUID
    extruder: Idx
    nozzle_diameter: Name
    nozzle_type: Text | None = None
    k_value: Annotated[Decimal, Field(ge=0, lt=100)]
    name: Text | None = None
    cali_idx: CaliIdx | None = None
    setting_id: Text | None = None


class TypeKValuePatch(_Patch):
    not_null = frozenset({"k_value"})
    nozzle_type: Text | None = None
    k_value: Annotated[Decimal, Field(ge=0, lt=100)] | None = None
    name: Text | None = None
    cali_idx: CaliIdx | None = None
    setting_id: Text | None = None


class TypeKValue(BaseModel):
    type_id: UUID
    printer_id: UUID
    extruder: Idx
    nozzle_diameter: str
    nozzle_type: str | None
    k_value: Decimal
    name: str | None
    cali_idx: int | None
    setting_id: str | None


class SpoolTypeDetail(SpoolType):
    """A type with its slicer presets and pressure-advance values."""

    presets: list[TypePreset]
    k_values: list[TypeKValue]


class SpoolPresetCreate(TypePresetCreate): ...


class SpoolPresetPatch(TypePresetPatch): ...


class SpoolPreset(BaseModel):
    spool_id: UUID
    printer_model: str
    nozzle_diameter: str
    slicer_filament: str
    slicer_filament_name: str | None


class SpoolKValueCreate(TypeKValueCreate): ...


class SpoolKValuePatch(TypeKValuePatch): ...


class SpoolKValue(BaseModel):
    spool_id: UUID
    printer_id: UUID
    extruder: int
    nozzle_diameter: str
    nozzle_type: str | None
    k_value: Decimal
    name: str | None
    cali_idx: int | None
    setting_id: str | None


# -- spools ---------------------------------------------------------------------------


class EffectiveValues(BaseModel):
    """What a spool is, after its own overrides over its type's values: every overridable
    field of `filament_spools`. A field neither sets is ``None``."""

    brand_id: UUID | None = None
    material_id: UUID | None = None
    subtype_id: UUID | None = None
    category_id: UUID | None = None
    effect_id: UUID | None = None
    colour_name: str | None = None
    rgba: Rgba | None = None
    extra_colours: list[Rgba] | None = None
    diameter_mm: Decimal | None = None
    density_g_cm3: Decimal | None = None
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    bed_temp_min: int | None = None
    bed_temp_max: int | None = None
    label_weight_g: int | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: int | None = None
    cost_per_kg: Decimal | None = None


class SpoolCreate(_In):
    """A spool names a type, or, while it still needs one, its own material."""

    type_id: UUID | None = None
    brand_id: UUID | None = None
    material_id: UUID | None = None
    subtype_id: UUID | None = None
    category_id: UUID | None = None
    effect_id: UUID | None = None
    colour_name: Text | None = None
    rgba: Rgba | None = None
    extra_colours: Colours | None = None
    diameter_mm: Diameter | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    label_weight_g: LabelWeight | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    weight_locked: bool = False
    note: Note | None = None
    grams_left: Grams | None = None

    @model_validator(mode="after")
    def _typed(self) -> SpoolCreate:
        if self.type_id is None and self.material_id is None:
            raise ValueError("a spool needs a type_id or a material_id")
        return self


class SpoolPatch(_Patch):
    """``null`` on an override clears it, and the spool takes its type's value again."""

    not_null = frozenset({"weight_locked"})
    type_id: UUID | None = None
    brand_id: UUID | None = None
    material_id: UUID | None = None
    subtype_id: UUID | None = None
    category_id: UUID | None = None
    effect_id: UUID | None = None
    colour_name: Text | None = None
    rgba: Rgba | None = None
    extra_colours: Colours | None = None
    diameter_mm: Diameter | None = None
    density_g_cm3: Density | None = None
    nozzle_temp_min: Temp | None = None
    nozzle_temp_max: Temp | None = None
    bed_temp_min: Temp | None = None
    bed_temp_max: Temp | None = None
    label_weight_g: LabelWeight | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    weight_locked: bool | None = None
    note: Note | None = None
    archived: bool | None = None


class Spool(_Row):
    number: int
    type_id: UUID | None
    brand_id: UUID | None
    material_id: UUID | None
    subtype_id: UUID | None
    category_id: UUID | None
    effect_id: UUID | None
    colour_name: str | None
    rgba: Rgba | None
    extra_colours: list[Rgba] | None
    diameter_mm: Decimal | None
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    label_weight_g: int | None
    insert_type_id: UUID | None
    low_stock_pct: int | None
    cost_per_kg: Decimal | None
    location_id: UUID | None
    weight_locked: bool
    note: str | None
    grams_left: float | None
    last_weighed_g: float | None
    last_weighed_at: datetime | None
    last_dried_at: datetime | None
    last_dried_temp_c: int | None
    last_dried_hours: Decimal | None
    last_used_at: datetime | None
    archived_at: datetime | None
    #: The values the spool has after its overrides over its type's.
    effective: EffectiveValues = Field(default_factory=EffectiveValues)
    #: The names of the fields the spool overrides.
    overridden: list[str] = Field(default_factory=list)


# -- tags, bindings, the ledger -------------------------------------------------------

TagKind = Literal["bambu_mifare", "ntag_openspool", "ntag_opentag", "other"]
EventKind = Literal[
    "created",
    "loaded",
    "unloaded",
    "moved",
    "print_started",
    "print_ended",
    "weighed",
    "ams_reading",
    "dried",
    "edited",
    "tag_bound",
    "tag_unbound",
    "archived",
    "restored",
    "synced_from_bb",
]
EventSource = Literal["ams_remain", "scale", "print_estimate", "print_actual", "person", "bb"]


class TagCreate(_In):
    """A tag names itself by its UID, its tray UUID, or both."""

    kind: TagKind
    tag_uid: Text | None = None
    tray_uuid: Text | None = None
    raw_blob: str | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def _named(self) -> TagCreate:
        if self.tag_uid is None and self.tray_uuid is None:
            raise ValueError("a tag needs a tag_uid or a tray_uuid")
        return self


class TagPatch(_Patch):
    not_null = frozenset({"kind"})
    kind: TagKind | None = None
    tag_uid: Text | None = None
    tray_uuid: Text | None = None
    raw_blob: str | None = None
    note: Note | None = None


class Tag(BaseModel):
    id: UUID
    kind: TagKind
    tag_uid: str | None
    tray_uuid: str | None
    raw_blob: str | None
    note: str | None
    created_at: datetime


class TagBindingCreate(_In):
    tag_id: UUID
    spool_id: UUID
    #: Take the tag from the spool or tag that holds it now.
    override: bool = False
    reason: Text | None = None


class TagBindingPatch(_Patch):
    """Ending a binding: ``unbound`` sets ``unbound_at`` now."""

    not_null = frozenset({"unbound"})
    unbound: bool | None = None
    reason: Text | None = None


class TagBinding(BaseModel):
    id: UUID
    tag_id: UUID
    spool_id: UUID
    override: bool
    bound_at: datetime
    unbound_at: datetime | None
    reason: str | None


class SpoolEventCreate(_In):
    spool_id: UUID
    kind: EventKind
    source: EventSource | None = None
    at: datetime | None = None
    printer_id: UUID | None = None
    ams_id: Idx | None = None
    tray_id: Idx | None = None
    grams_before: float | None = None
    grams_after: float | None = None
    delta_g: float | None = None
    ref_kind: Text | None = None
    ref_id: Text | None = None
    actor: Text | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ref(self) -> SpoolEventCreate:
        if self.ref_id is not None and self.ref_kind is None:
            raise ValueError("a ref_id needs a ref_kind")
        return self


class SpoolEvent(BaseModel):
    id: UUID
    seq: int
    spool_id: UUID
    at: datetime
    kind: EventKind
    source: EventSource | None
    printer_id: UUID | None
    ams_id: int | None
    tray_id: int | None
    grams_before: float | None
    grams_after: float | None
    delta_g: float | None
    ref_kind: str | None
    ref_id: str | None
    actor: str | None
    data: dict[str, Any]


class SpoolEventAdd(_In):
    """What a person records on a spool: a weighing (``grams`` is the filament left,
    without the empty spool) or a drying (its temperature and hours)."""

    kind: Literal["weighed", "dried"]
    source: Literal["person", "scale"] = "person"
    #: When it happened, with its offset; never later than now (`FUTURE_SKEW` allowed).
    at: AwareDatetime | None = None
    grams: Grams | None = None
    temp_c: Temp | None = None
    hours: Hours | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def _fits_kind(self) -> SpoolEventAdd:
        if self.at is not None and self.at > datetime.now(UTC) + FUTURE_SKEW:
            # A future reading would stay the anchor over every reading until then.
            raise ValueError("at is in the future")
        if self.kind == "weighed" and self.grams is None:
            raise ValueError("a weighing needs grams")
        if self.kind == "weighed" and (self.temp_c is not None or self.hours is not None):
            raise ValueError("a weighing has no temp_c or hours")
        if self.kind == "dried" and self.grams is not None:
            raise ValueError("a drying has no grams; record a weighing for that")
        return self


class SpoolArchive(_In):
    #: ``True`` takes the spool off the lists, ``False`` puts it back.
    archived: bool = True


class TagBind(_In):
    """The tag to put on a spool, by its UID, its tray UUID, or both. A tag not seen
    before is made with ``kind``."""

    tag_uid: Text | None = None
    tray_uuid: Text | None = None
    kind: TagKind = "other"
    #: Recorded on the binding: a person moved the tag on purpose.
    override: bool = False

    @model_validator(mode="after")
    def _named(self) -> TagBind:
        if self.tag_uid is None and self.tray_uuid is None:
            raise ValueError("a tag needs a tag_uid or a tray_uuid")
        return self


class BbLinkCreate(_In):
    """The link of a spool to its record in Bambuddy, for the sync."""

    spool_id: UUID
    bb_spool_id: Annotated[int, Field(ge=0, le=2_147_483_647)]
    bb_updated_at: datetime | None = None
    pushed_hash: Text | None = None


class BbLinkPatch(_Patch):
    bb_updated_at: datetime | None = None
    pushed_hash: Text | None = None


class BbLink(BaseModel):
    spool_id: UUID
    bb_spool_id: int
    bb_updated_at: datetime | None
    pushed_hash: str | None
    linked_at: datetime
