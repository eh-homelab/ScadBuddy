"""The models of ScadBuddy's filament inventory (#6261, epic #6260): one set per table
of `migrations/20261114T1300Z_filament_catalogs.sql` and `..T1301Z_filament_spools.sql`.

For a thing ``X`` there is ``XCreate`` (what a person sends to add one), ``XPatch`` (only
the fields sent change, by ``model_fields_set``; ``null`` clears a nullable column and is
refused for a NOT NULL one) and ``X`` (what a row reads back as, so ``X.model_validate(row)``
takes a psycopg ``dict_row``). Numeric columns come back from psycopg as ``Decimal`` and
are typed so. Every colour is an `Rgba`: ``#rrggbb`` is stored as ``#RRGGBBFF``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Final, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from scadbuddy.filament.colour import Rgba
from scadbuddy.filament.label_templates import LabelTemplate, LabelTemplateChoice
from scadbuddy.slicing.own_presets import sb_id

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


def _not_any(value: str) -> str:
    if value == "*":
        raise ValueError("name one printer model and nozzle diameter, not *")
    return value


#: A preset's printer model or nozzle diameter: one, never ``*`` for any (#6262; the
#: 0706Z migration's CHECK refuses it too).
PresetKey = Annotated[Name, AfterValidator(_not_any)]
SortOrder = Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
Idx = Annotated[int, Field(ge=0, le=1000)]
CaliIdx = Annotated[int, Field(ge=-1, le=1_000_000)]
Text = Annotated[str, Field(max_length=128)]
Note = Annotated[str, Field(max_length=2000)]
Temp = Annotated[int, Field(ge=0, le=1000)]
Density = Annotated[Decimal, Field(gt=0, lt=1000)]
Diameter = Annotated[Decimal, Field(gt=0, lt=100)]
Hours = Annotated[Decimal, Field(ge=0, lt=1000)]
#: numeric(6,1): 99999.9 is the largest the column holds.
MaxHours = Annotated[Decimal, Field(ge=0, le=Decimal("99999.9"))]
Grams = Annotated[float, Field(ge=0, le=1_000_000)]
LabelWeight = Annotated[int, Field(gt=0, le=100_000)]
Percent = Annotated[int, Field(ge=0, le=100)]
Days = Annotated[int, Field(ge=0, le=365)]
Cost = Annotated[Decimal, Field(ge=0, lt=100_000_000)]
TdfpId = Annotated[int, Field(gt=0, lt=2**31)]
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


class _Aliased(_Catalog):
    """A row of a catalog with aliases (#6706): its other names, canonical name aside,
    each a key no other name or alias of the catalog has (`filament/catalog.py`)."""

    aliases: list[str] = Field(default_factory=list)


class AliasesPut(_In):
    """The whole alias list of a row: trimmed, none empty (#6706, R-6706-6)."""

    aliases: Annotated[list[Name], Field(max_length=64)]


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


class Brand(_Aliased): ...


class CategoryCreate(_NamedCreate): ...


class CategoryPatch(_NamedPatch): ...


class Category(_Aliased): ...


class EffectCreate(_NamedCreate): ...


class EffectPatch(_NamedPatch): ...


class Effect(_Aliased): ...


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
    max_hours_outside_dry: MaxHours | None = None
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
    max_hours_outside_dry: MaxHours | None = None
    needs_enclosure: bool | None = None
    abrasive: bool | None = None
    hygroscopic: bool | None = None
    flexible: bool | None = None
    hardened_nozzle: bool | None = None
    note: Note | None = None
    archived: bool | None = None


class Material(_Aliased):
    family: str | None
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    drying_temp_c: int | None
    drying_hours: Decimal | None
    max_hours_outside_dry: Decimal | None
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


class Subtype(_Aliased):
    material_id: UUID | None
    density_g_cm3: Decimal | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    bed_temp_min: int | None
    bed_temp_max: int | None
    drying_temp_c: int | None
    drying_hours: Decimal | None


# -- insert types, named colours ------------------------------------------------------


#: Where an insert's empty weight came from (#7280 R-7280-3): ``catalog`` (Bambuddy's pull,
#: and every row from before the column), ``person``, ``placeholder``, ``derived`` (the mean
#: of new spools' weighings, `filament/insert_weights.py`) or ``weighed_empty``.
InsertWeightBasis = Literal["catalog", "person", "placeholder", "derived", "weighed_empty"]
#: The bases a person's catalog write may send (D6); no other is taken from a body.
PersonWeightBasis = Literal["person", "placeholder"]


class InsertTypeCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    empty_weight_g: Annotated[int, Field(gt=0, le=100_000)]
    #: ``placeholder`` marks a weight that nobody measured (R-7280-4).
    empty_weight_basis: PersonWeightBasis = "person"
    insert_material_id: UUID | None = None
    note: Note | None = None


class InsertTypePatch(_Patch):
    not_null = frozenset({"name", "sort_order", "empty_weight_g", "empty_weight_basis"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    #: Sent without ``empty_weight_basis``, it sets the basis to ``person`` (D6).
    empty_weight_g: Annotated[int, Field(gt=0, le=100_000)] | None = None
    empty_weight_basis: PersonWeightBasis | None = None
    insert_material_id: UUID | None = None
    note: Note | None = None
    archived: bool | None = None


class InsertType(_Catalog):
    empty_weight_g: int
    insert_material_id: UUID | None
    note: str | None
    #: Bambuddy's spool catalog entry this type is (the pull and the sync's results set it, #6265).
    bb_catalog_id: int | None = None
    empty_weight_basis: InsertWeightBasis = "catalog"
    #: The basis is ``placeholder`` or the note begins "Placeholder weight" (R-7280-3).
    placeholder: bool = False
    #: How many new spools' readings the derived weight is the mean of (R-7280-2).
    derived_count: int = 0


#: Why a weighing offers no derived insert weight (#7280 R-7280-1, D2). ``below_label``
#: (#7683): a new spool read under its label weight, and `InsertWeightCheck` names the
#: shortfall and the resolutions.
NoOfferReason = Literal[
    "archived",
    "no_insert",
    "no_label_weight",
    "used",
    "not_full",
    "below_label",
    "not_above_label",
    "derived_too_heavy",
]

#: How a new spool's reading under its label weight is read (#7683, spec §5.3): the
#: scale was tared on the empty spool, the spool is short, or the label weight is wrong.
BelowLabelResolutionKind = Literal["tared", "short", "label_wrong"]


class BelowLabelResolution(BaseModel):
    """One reading of a new spool's shortfall, with the filament it means."""

    kind: BelowLabelResolutionKind
    filament_g: float
    #: For ``label_wrong``: ``filament_g`` rounded half up, at least 1.
    new_label_weight_g: int | None = None


class InsertWeightOffer(BaseModel):
    """What accepting a new spool's weighing would make its insert's empty weight."""

    insert_type_id: UUID
    insert_name: str
    spool_id: UUID
    reading_g: float
    #: The spool's own label weight, else its type's (D1).
    label_weight_g: int
    #: ``reading_g - label_weight_g``, not rounded.
    derived_g: float
    current_empty_weight_g: int
    current_basis: InsertWeightBasis
    placeholder: bool
    #: The mean of the insert's readings with this one, rounded half up (D4).
    new_empty_weight_g: int
    #: The readings that mean uses, this one included.
    sample_count: int
    #: Only for a placeholder or an earlier derived weight (R-7280-4).
    checked_by_default: bool


class InsertWeightCheck(BaseModel):
    """A reading checked against its spool's insert: an ``offer``, else the ``reason``."""

    offer: InsertWeightOffer | None
    reason: NoOfferReason | None
    insert_type_id: UUID | None
    #: The insert's empty weight now; ``None`` without an insert.
    empty_weight_g: int | None
    #: ``reading_g - empty_weight_g``; ``None`` without an insert.
    filament_g: float | None
    #: For ``below_label`` (#7683): ``label_weight_g - reading_g``, the resolutions in the
    #: order ``tared``, ``short``, ``label_wrong`` (the last two only when the reading is
    #: at least the empty weight) and the one chosen by default.
    shortfall_g: float | None = None
    resolutions: list[BelowLabelResolution] = []
    default: BelowLabelResolutionKind | None = None


class InsertWeightAccept(_In):
    """A new spool's weighing: the filament, the empty spool and its insert."""

    reading_g: Annotated[float, Field(gt=0, le=100_000)]


class InsertWeightAccepted(BaseModel):
    insert_type: InsertType
    empty_weight_g: int
    sample_count: int
    #: ``reading_g - empty_weight_g`` with the new weight.
    filament_g: float


class ColourCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    manufacturer: Text | None = None
    rgba: Rgba
    material_id: UUID | None = None
    #: The material as the source names it ("PLA Basic"); with the maker and the name it
    #: is the row's identity (#6907).
    material_name: Text | None = None
    extra_colours: Colours | None = None
    effect_id: UUID | None = None


class ColourPatch(_Patch):
    not_null = frozenset({"name", "sort_order", "rgba"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    manufacturer: Text | None = None
    rgba: Rgba | None = None
    material_id: UUID | None = None
    material_name: Text | None = None
    extra_colours: Colours | None = None
    effect_id: UUID | None = None
    archived: bool | None = None


#: Where a colour catalog row came from (#6907): a sync's source, else ``None``, a person's.
ColourSource = Literal["bambuddy", "filamentcolors", "bambu"]


class Colour(_Catalog):
    manufacturer: str | None
    rgba: Rgba
    material_id: UUID | None
    material_name: str | None = None
    extra_colours: list[Rgba] | None
    effect_id: UUID | None
    #: The sync that made or last wrote the row; ``None`` for a person's own row. A
    #: person's edit keeps it, as where the values came from (Decision 222);
    #: ``synced_at`` ``None`` says the row was edited since.
    source: ColourSource | None = None
    #: That sync write's time. A person's write of a value a sync writes clears it, and no
    #: sync writes a row whose ``synced_at`` is ``None`` (#6907, Decision 217).
    synced_at: datetime | None = None
    #: Bambuddy's colour catalog entry the row is linked to (`GET /inventory/colors`).
    bb_colour_id: int | None = None


# -- locations ------------------------------------------------------------------------


class LocationKindCreate(_In):
    name: Name
    sort_order: SortOrder = 0
    dry_storage: bool = False
    ams: bool = False
    printer: bool = False


class LocationKindPatch(_Patch):
    not_null = frozenset({"name", "sort_order", "dry_storage", "ams", "printer"})
    name: Name | None = None
    sort_order: SortOrder | None = None
    dry_storage: bool | None = None
    ams: bool | None = None
    printer: bool | None = None
    archived: bool | None = None


class LocationKind(_Catalog):
    dry_storage: bool
    ams: bool
    printer: bool


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
    number: int
    printer_root: bool
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


class PathStep(BaseModel):
    """A place on the way to another, or under it: enough to name it and link to it."""

    id: UUID
    name: str
    code: str


class PlacedSpool(BaseModel):
    """A spool in a place, as its page lists it: effective material and colour."""

    id: UUID
    number: int
    code: str
    material: str | None
    colour_rgba: Rgba | None
    grams_left: float | None


class LocationDetail(Location):
    """A place's own page (#6955): its code and kind, the places above it (root first),
    the unarchived places under it and the unarchived spools in it."""

    code: str
    kind_name: str
    path: list[PathStep]
    children: list[PathStep]
    spools: list[PlacedSpool]


class PrinterLabelsRequest(_In):
    #: ``None`` means every printer.
    printer_ids: list[UUID] | None = None


class PrinterLabel(BaseModel):
    """One label: a printer's place (depth 0), an AMS unit or the external holder (1) or a
    slot (2)."""

    location_id: UUID
    code: str
    name: str
    depth: int


class PrinterLabels(BaseModel):
    base_url: str | None
    labels: list[PrinterLabel]
    #: The names of the printers with no report that lists an AMS unit or an external
    #: holder: their slots get labels only once a spool has been loaded in them.
    no_report: list[str] = []


#: The most labels one PDF draws, as the Labels page (`labelSheet.ts` `MAX_LABELS`).
MAX_LABELS = 100


class LabelsPdfRequest(_In):
    """#7157: the labels of these spools, then these places, in the order sent."""

    spool_ids: list[UUID] = Field(default_factory=list, max_length=MAX_LABELS)
    location_ids: list[UUID] = Field(default_factory=list, max_length=MAX_LABELS)
    #: ``None``: the stored setting `filament_label_template`.
    template: LabelTemplateChoice | None = None
    #: A one-label template: one label a page (``roll``) or packed on Letter paper
    #: (``sheet``). A sheet template prints on its own grid either way.
    layout: Literal["roll", "sheet"] = "roll"

    @model_validator(mode="after")
    def _one_to_max(self) -> LabelsPdfRequest:
        count = len(self.spool_ids) + len(self.location_ids)
        if not 1 <= count <= MAX_LABELS:
            raise ValueError(f"name 1 to {MAX_LABELS} spools and places")
        return self


class CustomLabelBounds(BaseModel):
    min_width_mm: float
    min_height_mm: float
    max_width_mm: float
    max_height_mm: float


class LabelTemplates(BaseModel):
    """#7157: the label templates, and a custom label's bounds."""

    templates: list[LabelTemplate]
    custom: CustomLabelBounds


Reading = Annotated[float, Field(allow_inf_nan=False)]


class SensorIn(_In):
    location_id: UUID
    source: Literal["home_assistant", "ams"]
    kind: Literal["humidity", "temperature"]
    entity_id: Text | None = None
    printer_id: UUID | None = None
    ams_id: Idx | None = None
    name: Name
    unit: Annotated[str, Field(max_length=16)] | None = None
    alert_above: Reading | None = None
    alert_below: Reading | None = None
    notify: bool = False

    @model_validator(mode="after")
    def _source_fields(self) -> SensorIn:
        if self.source == "home_assistant":
            if not self.entity_id:
                raise ValueError("entity_id is required for a home_assistant sensor")
            if self.printer_id is not None or self.ams_id is not None:
                raise ValueError("printer_id and ams_id are for an ams sensor only")
        else:
            if self.printer_id is None or self.ams_id is None:
                raise ValueError("printer_id and ams_id are required for an ams sensor")
            if self.entity_id is not None:
                raise ValueError("entity_id is for a home_assistant sensor only")
        _band(self.alert_below, self.alert_above)
        return self


def _band(below: float | None, above: float | None) -> None:
    if below is not None and above is not None and below >= above:
        raise ValueError("alert_below must be under alert_above")


class SensorPatch(_Patch):
    """A sensor's source, kind and where it reads from stay as made."""

    not_null = frozenset({"name", "notify"})
    location_id: UUID | None = None
    name: Name | None = None
    unit: Annotated[str, Field(max_length=16)] | None = None
    alert_above: Reading | None = None
    alert_below: Reading | None = None
    notify: bool | None = None

    @model_validator(mode="after")
    def _location_set(self) -> SensorPatch:
        if "location_id" in self.model_fields_set and self.location_id is None:
            raise ValueError("location_id cannot be cleared")
        _band(self.alert_below, self.alert_above)
        return self


#: Why a send failed, as the print worker records it (`filament/sensor_notify.py`
#: `failure_of`): Bambuddy refused the key (401, ``unauthorized``), the key lacks "Send
#: notifications" (403, ``scope``), Bambuddy has no app-message route (404, older than
#: 1.3.1, ``not_found``), it refused it for its rate limit, could not be reached, refused it
#: for another reason, took it into no channel, Bambuddy is not configured, or the send
#: broke in ScadBuddy.
SendError = Literal[
    "unauthorized",
    "scope",
    "not_found",
    "rate_limited",
    "unavailable",
    "rejected",
    "no_channel",
    "not_configured",
    "internal",
    "interrupted",
]

#: Why a notification was not delivered: a send's `SendError`, or what a claim decided
#: (`filament/sensors.py` `claim_on`): the sensor's ``notify`` was turned off or it was
#: archived before the send (``cancelled``), a newer change of the sensor was queued
#: (``superseded``), or a send was cut off before its end was recorded (``interrupted``:
#: it may have reached Bambuddy, and it is never sent again).
NotificationError = Literal[
    "unauthorized",
    "scope",
    "not_found",
    "rate_limited",
    "unavailable",
    "rejected",
    "no_channel",
    "not_configured",
    "internal",
    "cancelled",
    "superseded",
    "interrupted",
]

#: The most rows one claim takes: Bambuddy takes 20 app messages a minute per key.
CLAIM_MAX = 20


class NotificationClaim(_In):
    """``POST …/print/filament/sensor-notifications/claim``'s body (#6646)."""

    limit: int = Field(ge=1, le=CLAIM_MAX)


class ClaimedNotification(BaseModel):
    """A row a claim marked ``sending``: what to send (made when it was queued)."""

    id: UUID
    title: str
    message: str


class NotificationEnd(_In):
    """How a send ended: ``channels`` (how many Bambuddy channels took it) when Bambuddy
    answered, else ``error``. An answer of 0 channels is ended ``no_channel``."""

    channels: int | None = Field(default=None, ge=0)
    error: SendError | None = None
    #: Hand a claimed row back to ``queued``: the pass ran out of time before it tried it.
    requeue: Literal[True] | None = None

    @model_validator(mode="after")
    def _one(self) -> NotificationEnd:
        if self.requeue:
            if self.error is not None or self.channels is not None:
                raise ValueError("a row handed back has neither channels nor error")
            return self
        if self.error is None and not self.channels:
            raise ValueError("a sent message needs channels above 0; else give error")
        if self.error not in (None, "no_channel") and self.channels is not None:
            raise ValueError("a failed send has no channels")
        return self


class SensorNotification(BaseModel):
    """A notification a sensor queued when it started (`started`) or stopped (`stopped`)
    alerting (#6646). ``read_at`` and ``value`` are the reading that changed it; ``state``
    goes ``queued``, ``sending``, then ``sent`` or ``failed`` (a row a claim skipped goes
    from ``queued`` to ``failed``); ``sent_at`` is when it was ended; ``channels`` how many
    of Bambuddy's channels took it."""

    kind: Literal["started", "stopped"]
    value: float
    read_at: datetime
    channel: Literal["bambuddy"]
    state: Literal["queued", "sending", "sent", "failed"]
    channels: int | None
    error: NotificationError | None
    queued_at: datetime
    sent_at: datetime | None


class Sensor(BaseModel):
    id: UUID
    location_id: UUID
    source: Literal["home_assistant", "ams"]
    kind: Literal["humidity", "temperature"]
    entity_id: str | None
    printer_id: UUID | None
    ams_id: int | None
    name: str
    unit: str | None
    alert_above: float | None
    alert_below: float | None
    notify: bool
    last_value: float | None
    last_at: datetime | None
    reachable: bool
    created_at: datetime
    archived_at: datetime | None
    alerting: bool = False
    #: The newest notification this sensor queued, whatever its state (#6646).
    last_notification: SensorNotification | None = None

    @model_validator(mode="after")
    def _alerting(self) -> Sensor:
        v = self.last_value
        self.alerting = v is not None and (
            (self.alert_above is not None and v > self.alert_above)
            or (self.alert_below is not None and v < self.alert_below)
        )
        return self


class SensorReading(BaseModel):
    at: datetime
    value: float


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
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    label_weight_g: LabelWeight
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    lead_time_days: Days | None = None
    safety_margin_days: Days | None = None
    tdfp_id: TdfpId | None = None
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
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    label_weight_g: LabelWeight | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    lead_time_days: Days | None = None
    safety_margin_days: Days | None = None
    tdfp_id: TdfpId | None = None
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
    drying_temp_c: int | None
    drying_hours: Decimal | None
    label_weight_g: int
    insert_type_id: UUID | None
    low_stock_pct: int | None
    cost_per_kg: Decimal | None
    lead_time_days: int | None
    safety_margin_days: int | None
    filament_id: str | None
    tdfp_id: int | None
    note: str | None
    archived_at: datetime | None


class TypePresetCreate(_In):
    printer_model: PresetKey
    nozzle_diameter: PresetKey
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


class ShownKValue(TypeKValueCreate):
    """A K value row a fill's refresh showed, sent back as ``shown_k_values`` to compare.
    It takes the refresh's ``type_id`` and ``source`` too, so a client may echo the rows as
    it got them, and drops both (#6859, R-6714-22): only the server sets a row's source,
    and the comparison and the write never read them."""

    type_id: Annotated[UUID | None, Field(exclude=True)] = None
    source: Annotated[str | None, Field(exclude=True)] = None


class TypeKValuePatch(_Patch):
    not_null = frozenset({"k_value"})
    nozzle_type: Text | None = None
    k_value: Annotated[Decimal, Field(ge=0, lt=100)] | None = None
    name: Text | None = None
    cali_idx: CaliIdx | None = None
    setting_id: Text | None = None


#: Who set a type's K value (#6856, R-6714-19): a person (an edit, and every row made
#: before the column), the profile fill from its linked Bambuddy spools, a printer's
#: calibration, or the 3DFP profile fetch (#6625).
KSource = Literal["person", "profile", "printer_calibration", "profile_fetch"]


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
    #: Who set it; a write never takes it from a body (R-6714-19).
    source: KSource = "person"


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


class EffectivePreset(BaseModel):
    """A slicer preset a spool is printed with: its own row for the key, else its type's.
    ``overridden`` is true for a row of the spool's own. ``migrated`` is true for an own
    row the presets-per-nozzle migration made and no person has changed: the spool's
    first type drops it (`presets.drop_migrated`)."""

    printer_model: str
    nozzle_diameter: str
    slicer_filament: str
    slicer_filament_name: str | None
    overridden: bool
    migrated: bool = False
    #: For a ScadBuddy preset (``slicer_filament`` ``sb-<id>``, #7176): its Bambuddy
    #: local preset id (its mirror), or ``None`` while Bambuddy has none. Unset for any
    #: other ref, which Bambuddy knows as it is (`for_bambuddy`).
    bambuddy_id: str | None = None

    def for_bambuddy(self) -> str | None:
        """The ref Bambuddy knows this preset by: the mirror's id for a ScadBuddy preset
        (``None`` without one), else ``slicer_filament`` as it is."""
        return self.bambuddy_id if sb_id(self.slicer_filament) is not None else self.slicer_filament


class EffectiveKValue(BaseModel):
    """A pressure-advance value a spool is printed with: its own row for the key, else
    its type's. ``overridden`` is true for a row of the spool's own."""

    printer_id: UUID
    extruder: int
    nozzle_diameter: str
    nozzle_type: str | None
    k_value: Decimal
    name: str | None
    cali_idx: int | None
    setting_id: str | None
    overridden: bool


# -- type proposals (#6701, spec §8.1) --------------------------------------------------


class TypeProposal(BaseModel):
    """A type proposed for the open spools that need one and share its key: brand,
    material, subtype, RGBA, label weight and the ``material_number`` of their last
    Bambuddy reading (none when it is longer than 128 characters; spools with none fold into
    the one group of their other fields that has one). ``code`` is the proposed name;
    ``type_id`` names a type that matches the key now (the confirm uses it, and makes none).
    The names are the catalog rows'."""

    code: str
    type_id: UUID | None
    brand_id: UUID | None
    material_id: UUID
    subtype_id: UUID | None
    rgba: Rgba
    label_weight_g: int
    material_number: Text | None
    brand: str | None
    material: str
    subtype: str | None
    colour_name: str | None
    spool_ids: list[UUID]
    numbers: list[int]
    count: int


ProposalField = Literal["material", "rgba", "label_weight_g"]


class UngroupedSpool(BaseModel):
    """An open spool that needs a type but lacks a value a proposal needs."""

    spool_id: UUID
    number: int
    missing: list[ProposalField]


class TypeProposals(BaseModel):
    groups: list[TypeProposal]
    ungrouped: list[UngroupedSpool]


class TypeProposalPick(_In):
    """One proposal a person confirms: its key, the spools it named and the code to give
    the type (the proposed one, or the person's)."""

    brand_id: UUID | None = None
    material_id: UUID
    subtype_id: UUID | None = None
    rgba: Rgba
    label_weight_g: LabelWeight
    material_number: Text | None = None
    code: Name
    spool_ids: list[UUID] = Field(min_length=1, max_length=5000)


class TypeProposalConfirm(_In):
    groups: list[TypeProposalPick] = Field(min_length=1, max_length=500)


SkipReason = Literal["not_found", "archived", "typed", "changed"]


class SkippedSpool(BaseModel):
    """A spool a confirm left alone: gone, archived, typed meanwhile, or no longer of the
    proposal's key."""

    spool_id: UUID
    reason: SkipReason


class ConfirmedProposal(BaseModel):
    """What a confirm did with one proposal: the type it used (``None`` when no spool was
    left to type), ``made`` when this group made it, ``new`` when this request made it (by
    this group or an earlier one: ``new`` and not ``made`` means the group joined another
    group's new type, and its own code was not used), the spools it typed and those it
    skipped."""

    code: str
    type_id: UUID | None
    made: bool
    new: bool
    typed: list[UUID]
    skipped: list[SkippedSpool]


class TypeProposalResult(BaseModel):
    groups: list[ConfirmedProposal]


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
    drying_temp_c: int | None = None
    drying_hours: Decimal | None = None
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
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    label_weight_g: LabelWeight | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    weight_locked: bool = False
    note: Note | None = None
    grams_left: Grams | None = None
    #: Whether the spool was added full (#7149 R-7149-2); ``None`` is not known.
    added_full: bool | None = None

    @model_validator(mode="after")
    def _typed(self) -> SpoolCreate:
        if self.type_id is None and self.material_id is None:
            raise ValueError("a spool needs a type_id or a material_id")
        return self


class SpoolDraft(_In):
    """A new spool as the spool form holds it (#7150, R-7150-2): its fields, the place it
    goes to (not a printer's tray) and its own presets and K values. What
    ``GET /filament/spools/{id}/copy`` answers (R-7150-3), with ``copy_of`` its source.
    A client never marks a preset row ``migrated`` (R-7150-13): with ``copy_of``, a row
    whose key and values match a ``migrated`` row of that spool is written ``migrated``,
    so the copy's first type drops it as it would the source's (R-7150-11); any other row
    is the person's."""

    spool: SpoolCreate
    location_id: UUID | None = None
    presets: list[SpoolPresetCreate] = Field(default_factory=list)
    k_values: list[SpoolKValueCreate] = Field(default_factory=list)
    copy_of: UUID | None = None


#: The most spools one bulk create makes (R-7150-1).
BULK_MAX: Final = 100


class SpoolBulkCreate(SpoolDraft):
    """``quantity`` identical spools, each with its own number (#7150, R-7150-1)."""

    quantity: Annotated[int, Field(ge=1, le=BULK_MAX)] = 1


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
    drying_temp_c: Temp | None = None
    drying_hours: Hours | None = None
    label_weight_g: LabelWeight | None = None
    insert_type_id: UUID | None = None
    low_stock_pct: Percent | None = None
    cost_per_kg: Cost | None = None
    weight_locked: bool | None = None
    note: Note | None = None
    added_full: bool | None = None
    archived: bool | None = None


class SpoolNames(BaseModel):
    """The names behind a spool's effective ids, and its place's name."""

    brand: str | None = None
    material: str | None = None
    subtype: str | None = None
    category: str | None = None
    effect: str | None = None
    insert_type: str | None = None
    location: str | None = None


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
    drying_temp_c: int | None = None
    drying_hours: Decimal | None = None
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
    #: The newest weighing's drift, ``data.drift_g`` (#7682); null when it has no breakdown.
    last_drift_g: float | None = None
    archived_at: datetime | None
    #: The invoice line the spool was bought on, if any. An invoice line may fill a spool
    #: with none, costed or not; one with a line is refused (#6700).
    invoice_line_id: UUID | None = None
    #: How the spool's data came in, in Bambuddy's words (``rfid_auto``, ``nfc_link``,
    #: ``opentag3d`` ...) or ScadBuddy's ``openspool``, whether the pull or ScadBuddy wrote
    #: it (#7149 R-7149-1). Read only: the pull, a person's tag bind and a checked tag write
    #: from the phone page (#7273) set it.
    data_origin: str | None = None
    #: Whether the spool was added full; ``None`` is not known (R-7149-2).
    added_full: bool | None = None
    #: When its tag was written (Bambuddy's ``encode_time``, R-7149-3). Read only.
    tag_encoded_at: datetime | None = None
    #: The values the spool has after its overrides over its type's.
    effective: EffectiveValues = Field(default_factory=EffectiveValues)
    #: The names of the fields the spool overrides.
    overridden: list[str] = Field(default_factory=list)
    #: The text of the effective catalog rows and of the spool's place.
    names: SpoolNames = Field(default_factory=lambda: SpoolNames())
    #: Server-computed (F3): the grams left are under the spool's effective low-stock
    #: percentage of its label weight, else the default's. False when either is unknown.
    low_stock: bool = False
    #: Server-computed (#6268): the hours since the spool was last dried (else made) that
    #: its place was not dry storage, and whether that passes its material's limit.
    hours_outside_dry: float = 0.0
    needs_drying: bool = False
    #: Server-computed (#7682): ``last_drift_g`` is at or over the setting
    #: ``filament_weighing_drift_notice_g``, either way.
    drift_notice: bool = False


# -- the trays (`filament/inventory.py`) ----------------------------------------------

#: How a tray's spool was found: its tag's binding, or a person's link.
TrayFoundBy = Literal["tag", "link"]


class TrayHolder(BaseModel):
    """One reported tray and the SB spool in it, with how it was found."""

    ams_id: int
    tray_id: int
    spool: Spool | None
    found_by: TrayFoundBy | None


class TrayView(BaseModel):
    """A printer's reported trays, and the unarchived spools no printer holds. Here, not
    in `filament/inventory.py`, so the print dialog's modules (`bambuddy/trays.py`) read
    it without importing the inventory, which imports them (#6262 Task 5)."""

    trays: list[TrayHolder]
    shelf: list[Spool]


class TrayPlace(BaseModel):
    """The tray an SB spool is in (#6262 Task 7, `FilamentInventory.rows`): its
    registry printer, that printer's Bambuddy id and name, and the tray. ``printer_id``
    is ``None`` only for a Bambuddy assignment on a printer the registry has no row for
    yet; ``bambuddy_printer_id`` is ``None`` for a printer reached over the LAN alone."""

    printer_id: str | None
    bambuddy_printer_id: int | None
    printer_name: str | None
    ams_id: int
    tray_id: int


class FilamentInventorySpool(BaseModel):
    """An SB spool, the tray it is in, and the Bambuddy spool it is linked to
    (`filament_bb_links`), as the Bambuddy path's print picker (#6262 Task 7) and
    ``GET /filament/inventory`` (Task 9) read it. Named apart from Bambuddy's inventory
    spool (plan F16)."""

    spool: Spool
    loaded: TrayPlace | None = None
    bb_spool_id: int | None = None
    #: The spool's effective presets (`presets.effective_presets`), so its row names the
    #: preset it slices with on the chosen nozzle (#6262 Task 7 review R2).
    presets: list[EffectivePreset] = Field(default_factory=list)


class TrayMismatch(BaseModel):
    """What the printer reports in an untagged tray whose placed spool does not agree
    with it, where Bambuddy assigns no spool that does (#6713, `FilamentInventory.view`):
    the tray's ``tray_type`` and its ``#RRGGBBAA``. Nothing moves; a person checks."""

    reported_type: str
    reported_rgba: str | None = None


class InventoryTray(BaseModel):
    """One filled tray a registry printer's latest report shows (#6262 Task 9): its label,
    the material and ``#RRGGBBAA`` it reports, and the SB spool it holds
    (`FilamentInventory.view`), with how that spool was found, and ``mismatch`` when that
    spool, placed in an untagged tray, does not agree with what the tray reports (#6713)."""

    ams_id: int
    tray_id: int
    #: "AMS-A slot 1", as Bambu's own UI counts.
    label: str
    material: str | None = None
    rgba: str | None = None
    spool_id: UUID | None = None
    spool_number: int | None = None
    found_by: TrayFoundBy | None = None
    mismatch: TrayMismatch | None = None


class PrinterTrays(BaseModel):
    """A registry printer's reported trays, as its latest stored report shows them."""

    printer_id: str
    printer_name: str
    trays: list[InventoryTray]


class FilamentInventoryView(BaseModel):
    """ScadBuddy's spools, each with the tray it is in, and every registry printer's
    reported trays with the SB spool each holds (`GET /filament/inventory`, #6262 Task 9).
    Named apart from Bambuddy's ``InventoryView`` (plan F16)."""

    spools: list[FilamentInventorySpool]
    trays: list[PrinterTrays]


class SameFilamentPlace(BaseModel):
    """Where one spool of a filament is loaded."""

    spool_id: UUID
    spool_number: int
    loaded: TrayPlace


class SameFilamentStock(BaseModel):
    """Every unarchived SB spool of one filament: the same effective material, subtype,
    brand and ``#RRGGBBAA`` (the colour's name for a spool with none). An opaque and a
    translucent spool of one hue are two filaments (#6316)."""

    material: str | None = None
    subtype: str | None = None
    brand: str | None = None
    colour_name: str | None = None
    rgba: str | None = None
    spool_count: int
    spool_numbers: list[int]
    #: The spools' grams left (their ledgers), summed.
    grams_left: float
    #: Where the filament's spools are loaded, one row per loaded spool.
    loaded: list[SameFilamentPlace]


class FilamentSpoolSummary(BaseModel):
    """What a filament hover card shows for one SB spool (#785, #6262 Task 9): its stock,
    tray and price, and the stock of every spool of the same filament. Prices are the
    spool's effective ``cost_per_kg``; every price and cost is ``None`` without one,
    never a guess."""

    spool: FilamentInventorySpool
    cost_per_kg: Decimal | None = None
    #: ``cost_per_kg`` / 1000, to six places.
    price_per_g: float | None = None
    #: What the grams left on this spool are worth, to four places.
    value_left: float | None = None
    #: ``grams`` x ``price_per_g``, when the caller asked about ``grams`` (a plate's use).
    grams: float | None = None
    grams_cost: float | None = None
    filament: SameFilamentStock


class HeldSpool(BaseModel):
    """What a tray's picker row and its low-filament warnings read of the SB spool in it
    (`bambuddy/trays.py` `loaded_trays`): its id and number, its effective brand and
    colour names, ``rgba`` and label weight, the ledger's grams, and its material and
    subtype names for the rack's Glow and abrasive tests only (#6876). Nothing else, so
    the background slice's request stays small (#6262, ``MAX_REQUEST_BYTES``)."""

    id: UUID
    number: int
    needs_type: bool
    brand: str | None = None
    material: str | None = None
    subtype: str | None = None
    colour_name: str | None = None
    rgba: str | None = None
    grams_left: float | None = None
    label_weight_g: int | None = None

    @classmethod
    def of(cls, spool: Spool) -> HeldSpool:
        effective = spool.effective
        return cls(
            id=spool.id,
            number=spool.number,
            needs_type=spool.type_id is None,
            brand=spool.names.brand,
            material=spool.names.material,
            subtype=spool.names.subtype,
            colour_name=effective.colour_name,
            rgba=effective.rgba,
            grams_left=spool.grams_left,
            label_weight_g=effective.label_weight_g,
        )


class HeldTray(BaseModel):
    """One reported tray and the SB spool in it, as :class:`HeldSpool`."""

    ams_id: int
    tray_id: int
    spool: HeldSpool


class SbSpools(BaseModel):
    """What a native run's plan reads of ScadBuddy's own spools, read on the API and
    carried where no database is (#6262): the dialog's background slice runs on the print
    worker (`bambuddy/operations.py` `preview_run`), so its route reads these first and
    sends them in the operation's request, out of its key (``unkeyed``: the API's read,
    not the client's request). ``trays`` are the trays that hold one; ``spool_presets``
    the held spools' own, by SB number. It is a held view itself (:data:`HeldTrays`,
    with no shelf: a plan's choices are the trays) and a `filament/presets.py`
    `SpoolPresetSource`; a native plan has no Bambuddy client, so it names no Bambuddy
    spool."""

    trays: list[HeldTray] = Field(default_factory=list)
    spool_presets: dict[int, list[EffectivePreset]] = Field(default_factory=dict)

    @property
    def shelf(self) -> list[Spool]:
        return []

    @classmethod
    def of(
        cls,
        view: TrayView,
        spool_presets: dict[int, list[EffectivePreset]],
        printer_model: str,
    ) -> SbSpools:
        """``view``'s held trays, and only ``printer_model``'s preset rows: the plan reads
        no other (`resolver.PRINTER_MODEL`), so other printers' rows never grow the
        request."""
        return cls(
            trays=[
                HeldTray(ams_id=tray.ams_id, tray_id=tray.tray_id, spool=HeldSpool.of(tray.spool))
                for tray in view.trays
                if tray.spool is not None
            ],
            spool_presets={
                number: [row for row in rows if row.printer_model == printer_model]
                for number, rows in spool_presets.items()
            },
        )

    async def presets(self, numbers: Sequence[int]) -> dict[int, list[EffectivePreset]]:
        rows = self.spool_presets
        return {number: rows[number] for number in numbers if number in rows}

    async def bb_spools(self, numbers: Sequence[int]) -> dict[int, int]:
        return {}


#: The SB spool in each tray as the print dialog's modules read it: the inventory's
#: full view, or the background slice's :class:`SbSpools`.
HeldTrays = TrayView | SbSpools


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
    "consumed_reset",
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
    last_seen_at: datetime | None = None
    last_seen_source: str | None = None


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
    without the empty spool; ``reading_g`` the scale's reading with it, from which the
    store takes the spool's effective empty weight when ``grams`` is left out, and keeps
    the breakdown and the drift in the event's ``data``, #7682), a drying (its temperature
    and hours) or a reset of its consumed counter (``consumed_reset``, #7149 R-7149-4: no
    grams, the grams left stay). A drying that leaves out ``temp_c`` or ``hours`` takes
    the spool's `DryingDefaults` for it; ``null`` records none (#6720). A new spool's
    weighing under its label weight sends ``resolution`` with ``reading_g`` alone (#7683):
    what the check `GET …/insert-weight-offer` offered for it."""

    kind: Literal["weighed", "dried", "consumed_reset"]
    source: Literal["person", "scale"] = "person"
    #: When it happened, with its offset; never later than now (`FUTURE_SKEW` allowed).
    at: AwareDatetime | None = None
    grams: Grams | None = None
    #: The scale reading with the spool, a weighing only.
    reading_g: Grams | None = None
    #: How a reading under a new spool's label weight is read (#7683).
    resolution: BelowLabelResolutionKind | None = None
    temp_c: Temp | None = None
    hours: Hours | None = None
    note: Note | None = None

    @model_validator(mode="after")
    def _fits_kind(self) -> SpoolEventAdd:
        if self.at is not None and self.at > datetime.now(UTC) + FUTURE_SKEW:
            # A future reading would stay the anchor over every reading until then.
            raise ValueError("at is in the future")
        if self.reading_g is not None and self.kind != "weighed":
            raise ValueError("only a weighing has reading_g")
        if self.resolution is not None and self.reading_g is None:
            raise ValueError("a resolution needs reading_g")
        if self.resolution is not None and self.grams is not None:
            raise ValueError("a resolution takes reading_g alone")
        if self.reading_g is not None and self.grams is not None and self.reading_g < self.grams:
            raise ValueError("reading_g is below grams")
        if self.kind == "weighed" and self.grams is None and self.reading_g is None:
            raise ValueError("a weighing needs grams")
        if self.kind == "weighed" and (self.temp_c is not None or self.hours is not None):
            raise ValueError("a weighing has no temp_c or hours")
        if self.kind == "dried" and self.grams is not None:
            raise ValueError("a drying has no grams; record a weighing for that")
        if self.kind == "consumed_reset" and (
            self.grams is not None or self.temp_c is not None or self.hours is not None
        ):
            raise ValueError("a consumed reset has no grams, temp_c or hours")
        return self


class ConsumedReset(_In):
    """The spools whose consumed counter to start again (#7149 R-7149-4): an explicit list,
    never "all", as Bambuddy's bulk reset takes (a slip would wipe every counter)."""

    spool_ids: list[UUID] = Field(min_length=1, max_length=500)


class ConsumedResetDone(BaseModel):
    """How many spools took a ``consumed_reset`` event, and the ids that name no spool."""

    reset: int
    not_found: list[UUID]


#: The most spools one bulk edit, archive or restore names (#7151), as `ConsumedReset`.
BULK_IDS_MAX: Final = 500


class SpoolIds(_In):
    """The spools a bulk edit, archive or restore acts on (#7151, R-7151-1, R-7151-2): an
    explicit list."""

    spool_ids: list[UUID] = Field(min_length=1, max_length=BULK_IDS_MAX)


class SpoolBulkUpdate(SpoolIds):
    """A bulk edit (#7151, R-7151-1): ``patch`` on each spool named, all or none. The
    patch is a PATCH's, less ``archived`` (the archive routes do that), and sets at least
    one field."""

    patch: SpoolPatch

    @model_validator(mode="after")
    def _fields(self) -> SpoolBulkUpdate:
        if "archived" in self.patch.model_fields_set:
            raise ValueError("a bulk edit does not archive; use bulk-archive or bulk-restore")
        if not self.patch.model_fields_set:
            raise ValueError("the patch sets no field")
        return self


class SpoolBulkUpdated(BaseModel):
    """A bulk edit's answer, each list in id order: the spools it changed, those already
    so, and the ids that name none."""

    changed: list[UUID]
    unchanged: list[UUID]
    not_found: list[UUID]


class SpoolsArchived(BaseModel):
    """A bulk archive's or restore's answer (#7151, R-7151-2), each list in id order:
    the spools it archived or restored, those already so, and the ids that name none."""

    changed: list[UUID]
    unchanged: list[UUID]
    not_found: list[UUID]


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


# -- reads for the Filament section (#6262) ----------------------------------------


class GramsBasis(BaseModel):
    """What a spool's ``grams_left`` is made of: the reading it anchors on and the
    deltas taken off it, oldest first."""

    anchor: SpoolEvent | None
    deltas: list[SpoolEvent]


class LastTag(BaseModel):
    """The tag a spool had before, how its binding ended, and the number of the spool
    that holds the tag now."""

    tag: Tag
    unbound_at: datetime
    reason: str | None
    held_by: int | None


class SpoolInvoiceRef(BaseModel):
    """The invoice line a spool was bought on (#6266); ``unit_cost`` is the line's
    ``allocated_cost`` per spool."""

    invoice_id: UUID
    number: str | None
    invoice_date: date
    supplier_id: UUID
    supplier_name: str
    line_id: UUID
    line_position: int
    unit_cost: Decimal


class SpoolTypeSupplier(BaseModel):
    """A supplier that sells the spool's type: one unarchived product and its newest price."""

    supplier_id: UUID
    supplier_name: str
    sku: str
    product_url: str | None
    newest_price: Decimal | None
    price_per_kg: Decimal | None


DryingFrom = Literal["spool", "type", "subtype", "material", "setting"]


class DryingDefaults(BaseModel):
    """What a drying of the spool records when it does not say (#6720, R-6714-10,
    R-6714-18): each field from the spool's own override, else its type, else its subtype,
    else its material, else the setting ``filament_drying_temp_c`` or
    ``filament_drying_hours`` (#7156); ``*_from`` names which. ``None`` when none of them
    sets it."""

    temp_c: int | None = None
    hours: Decimal | None = None
    temp_from: DryingFrom | None = None
    hours_from: DryingFrom | None = None


class SpoolDetail(Spool):
    """A spool with what its page shows: its tags, effective presets and K values, the
    basis of its grams, whether it ever printed and its drying defaults."""

    drying_defaults: DryingDefaults = Field(default_factory=DryingDefaults)

    #: The tag bound to it now.
    tag: Tag | None = None
    last_tag: LastTag | None = None
    presets: list[EffectivePreset] = Field(default_factory=list)
    k_values: list[EffectiveKValue] = Field(default_factory=list)
    basis: GramsBasis
    #: Any ``print_started`` or ``print_ended`` event: the spool is archived, not deleted.
    in_use: bool = False
    #: The invoice line it was bought on; ``None`` for a spool no invoice made.
    invoice: SpoolInvoiceRef | None = None
    #: The unarchived products of unarchived suppliers for its type, by supplier name.
    type_suppliers: list[SpoolTypeSupplier] = Field(default_factory=list)
    #: The newest ``weighed`` event; its ``data`` holds the breakdown when it has one (#7682).
    last_weighing: SpoolEvent | None = None


class TagRow(Tag):
    binding: TagBinding | None = None
    spool_number: int | None = None


class TagDetail(TagRow):
    #: Every binding of the tag, newest first.
    history: list[TagBinding] = Field(default_factory=list)


#: NTAG216's usable memory, the most one tag's records can hold.
MAX_RECORD_BYTES: Final = 888
MAX_RECORDS: Final = 16
TagSource = Literal["phone", "agent", "ams"]


class NdefRecord(_In):
    """One NDEF record as a reader gives it: its type and its payload in hex."""

    record_type: Annotated[
        str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9:._/+-]+$")
    ]
    media_type: Annotated[str, Field(max_length=127)] | None = None
    id: Annotated[str, Field(max_length=255)] | None = None
    encoding: Annotated[str, Field(max_length=32)] | None = None
    lang: Annotated[str, Field(max_length=35)] | None = None
    data_hex: Annotated[str, Field(max_length=2 * MAX_RECORD_BYTES, pattern=r"^([0-9a-fA-F]{2})*$")]

    @field_validator("data_hex")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()

    @field_validator("media_type", "id", "encoding", "lang")
    @classmethod
    def _utf8(cls, value: str | None) -> str | None:
        return _utf8(value)


def _utf8(value: str | None) -> str | None:
    """``value`` when UTF-8 can hold it. JSON can carry a lone UTF-16 surrogate
    (``"\\ud800"``), which no UTF-8 holds, so storing or hashing it would fail later."""
    if value is not None:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("the text holds a lone UTF-16 surrogate") from None
    return value


def _check_fits(records: Sequence[NdefRecord]) -> None:
    if sum(len(r.data_hex) // 2 for r in records) > MAX_RECORD_BYTES:
        raise ValueError(f"the records hold more than {MAX_RECORD_BYTES} bytes")


class _FitsOneTag(_In):
    """A body whose NDEF ``records`` must fit one tag."""

    records: Annotated[list[NdefRecord], Field(max_length=MAX_RECORDS)]

    @model_validator(mode="after")
    def _fits(self) -> Self:
        _check_fits(self.records)
        return self


class TagReadIn(_FitsOneTag):
    """What a phone reads from a tag: its UID and its NDEF records."""

    uid: Annotated[str, Field(max_length=64)] | None = None
    tag_type: Annotated[str, Field(max_length=32)] | None = None
    #: Bind the tag to this spool as part of the read.
    bind_spool_id: UUID | None = None


BackupReason = Literal["spool_write", "agent_write", "restore"]


class TagBackupIn(_FitsOneTag):
    """What a tag held just before a write (#7273 G7). An empty ``uid`` is allowed here so
    that it reaches the store and answers 422 ``no_uid``, as ``"::"`` does."""

    uid: Annotated[str, Field(max_length=64)]
    reason: BackupReason
    job_id: UUID | None = None

    @model_validator(mode="after")
    def _job(self) -> TagBackupIn:
        if (self.reason == "agent_write") != (self.job_id is not None):
            raise ValueError("`job_id` goes with the reason `agent_write`, and only with it")
        return self


class TagBackup(BaseModel):
    id: UUID
    tag_id: UUID
    uid: str
    records: list[NdefRecord]
    reason: BackupReason
    job_id: UUID | None
    actor: str
    at: datetime


class TagBackupMade(BaseModel):
    backup_id: UUID


class TagRecordsIn(_FitsOneTag):
    """The tag as a phone just read it, and the backup it took of it (#7273 G2, G7)."""

    uid: Annotated[str, Field(max_length=64)]
    backup_id: UUID


class TagRecords(BaseModel):
    """The records to write. ``format`` is ``None`` for a restore of records that are in
    neither format."""

    format: Literal["openspool", "opentag3d"] | None
    records: list[NdefRecord]
    warnings: list[str] = Field(default_factory=list)


class TagWrittenIn(_In):
    """A write the phone made: what it sent and what the tag held after (#7273 R4)."""

    uid: Annotated[str, Field(max_length=64)]
    backup_id: UUID
    sent: Annotated[list[NdefRecord], Field(max_length=MAX_RECORDS)]
    read_back: Annotated[list[NdefRecord], Field(max_length=MAX_RECORDS)]


NfcJobKind = Literal["read", "write"]
#: ``cancelled``: the agent stopped waiting for it (#7273 M1).
NfcJobState = Literal["pending", "armed", "done", "failed", "expired", "cancelled"]


class NfcJobIn(_In):
    """A tag read or write an agent asks for (#7273 G4). The phone page runs it when a person
    arms it. A ``write`` needs the ``records`` to write; a ``read`` takes none."""

    kind: NfcJobKind
    records: Annotated[list[NdefRecord], Field(min_length=1, max_length=MAX_RECORDS)] | None = None
    #: How long the job waits for a person, in seconds (#7273 R7).
    ttl_s: Annotated[int, Field(ge=1, le=600)] = 600

    @model_validator(mode="after")
    def _records(self) -> Self:
        if (self.kind == "write") != (self.records is not None):
            raise ValueError("a `write` job needs `records`, and a `read` job takes none")
        _check_fits(self.records or [])
        return self


class NfcJob(BaseModel):
    id: UUID
    kind: NfcJobKind
    records: list[NdefRecord] | None
    created_by: str
    state: NfcJobState
    result: dict[str, Any] | None
    created_at: datetime
    expires_at: datetime
    armed_by: str | None
    #: Whether the job's ``agent_write`` backup exists: the write has begun, so the job
    #: is never cancelled or expired; it ends with its result, or ``failed`` with the code
    #: ``no_read_back`` once the backup is too old to check one (#7419 review).
    write_begun: bool


def records_sha256(records: Sequence[NdefRecord]) -> str:
    """The SHA-256, as lower-case hex, of the canonical JSON of ``records`` (`NfcJobClaimIn`)."""
    canonical = json.dumps(
        [r.model_dump(mode="json") for r in records],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NfcJobClaimIn(_In):
    """The phone page arms a job (#7273 R22). For a ``write`` job, ``records_sha256`` is
    required: the SHA-256 (hex) of the records the page showed the person, so that the
    person's tap approves exactly those records. A ``read`` job takes none.

    The hashed bytes are the UTF-8 of the job's ``records`` array exactly as
    ``GET /nfc-jobs/{job_id}`` returns it, written as canonical JSON: the array in its own
    order; in each record object, every key it has (``data_hex``, ``encoding``, ``id``,
    ``lang``, ``media_type``, ``record_type``, with ``null`` for an absent value) in sorted
    order; no spaces or newlines (separators ``,`` and ``:``); strings as ``JSON.stringify``
    writes them (non-ASCII characters as themselves, not ``\\u`` escapes). One record
    ``{"record_type": "mime", "data_hex": "7b7d"}`` is hashed as
    ``[{"data_hex":"7b7d","encoding":null,"id":null,"lang":null,"media_type":null,"record_type":"mime"}]``.
    In a browser: ``JSON.stringify`` of the records with each object's keys sorted, then
    ``crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))``. The server's side
    is `records_sha256`."""

    records_sha256: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")] | None = None

    @field_validator("records_sha256")
    @classmethod
    def _lower(cls, value: str | None) -> str | None:
        return None if value is None else value.lower()


class NfcJobResultIn(_In):
    """What the phone page got from the tag for an armed job. For a write, ``records`` is the
    read-back and ``backup_id`` the `agent_write` backup it took first. An ``error`` fails the
    job, and nothing else is read."""

    uid: Annotated[str, Field(max_length=64)] | None = None
    records: Annotated[list[NdefRecord], Field(max_length=MAX_RECORDS)] = Field(
        default_factory=list
    )
    backup_id: UUID | None = None
    error: Annotated[str, Field(max_length=500)] | None = None

    @field_validator("error")
    @classmethod
    def _utf8(cls, value: str | None) -> str | None:
        return _utf8(value)

    @model_validator(mode="after")
    def _fits(self) -> Self:
        _check_fits(self.records)
        return self


class DecodedSpool(BaseModel):
    """A spool as a tag's records describe it."""

    format: Literal["openspool", "opentag3d"]
    version: str
    material: str | None = None
    subtype: str | None = None
    brand: str | None = None
    colour_name: str | None = None
    rgba: Rgba | None = None
    extra_colours: list[Rgba] | None = None
    diameter_mm: Decimal | None = None
    density_g_cm3: Decimal | None = None
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    bed_temp_min: int | None = None
    bed_temp_max: int | None = None
    drying_temp_c: int | None = None
    drying_hours: Decimal | None = None
    label_weight_g: int | None = None
    empty_weight_g: int | None = None
    serial: str | None = None
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "A field the tag holds that could not be read (`min_temp`), a field read and "
            "then cut (`brand cut to 128 characters`), or a note (`newer minor version`)."
        ),
    )


class TagReadResult(BaseModel):
    tag: TagRow | None
    spool: Spool | None
    decoded: DecodedSpool | None
    draft: SpoolCreate | None
    next: Literal["open_spool", "bind_or_create", "none"]


class LedgerRow(SpoolEvent):
    spool_number: int


class StalePrinter(BaseModel):
    """A printer whose spools an AMS sync counted as ``stale`` (#6679)."""

    id: UUID
    #: Its name; ``None`` when the printer has no row.
    name: str | None = None


class AmsSyncResult(BaseModel):
    """What one sync of the AMS's weights did (#6268). Each spool in a printer's tray place is
    in one count; a spool elsewhere is in none, and so is one placed in a tray while the
    sync ran (the next sync counts it)."""

    booked: int = 0
    #: Spools whose weight is locked.
    locked: int = 0
    #: Spools in a printer that is printing.
    printing: int = 0
    #: Spools whose tray shows no ``remain`` (no RFID, or no tray in the report).
    no_remain: int = 0
    #: Spools in a printer with no report or no row, or a report too old or unreadable.
    stale: int = 0
    #: The printers whose spools are in ``stale``, by id, each once.
    stale_printers: list[StalePrinter] = Field(default_factory=list)
    #: The spools booked.
    spools: list[UUID] = Field(default_factory=list)


class EventPage(BaseModel):
    events: list[LedgerRow]
    #: Opaque: pass it back as ``cursor`` for the next page; ``None`` on the last.
    next_cursor: str | None = None


class MaterialStock(BaseModel):
    material: str | None
    spools: int
    grams_left: float


class InventorySummary(BaseModel):
    """The unarchived spools in total."""

    spools: int
    grams_left: float
    #: Grams left at each spool's effective cost per kg; ``None`` when none has a cost.
    value_left: Decimal | None
    by_material: list[MaterialStock]
    #: Spools whose place is a printer's tray (a location with ``printer_id`` set).
    loaded: int
    low_stock: int
    needs_type: int
    #: Grams of every plate of every spool, archived ones too, each plate counted once
    #: (`metrics.plates`, plan Decision 18).
    consumed_g: float


class TagPushReport(BaseModel):
    """What one drain of the pending-tag queue did (`filament/rfid_push.py`, #6264)."""

    pushed: int = 0
    moved: int = 0
    cleared: int = 0
    unchanged: int = 0
    failed: int = 0


class SlotReport(BaseModel):
    """What one reconcile of the override slots did (#6264). ``no_preset``: the type has no
    preset for the nozzle. ``no_temps``: the spool has no nozzle temperatures, the tray
    reports none, or the spool has no material or colour to send. Neither calls Bambuddy."""

    configured: int = 0
    unchanged: int = 0
    no_preset: int = 0
    no_temps: int = 0
    no_nozzle: int = 0
    failed: int = 0


class RfidState(BaseModel):
    """The ``filament_rfid_state`` row (`filament/rfid_mode.py`, #6264)."""

    override_mode: bool
    #: Bambuddy's ``auto_add_unknown_rfid`` as found when the mode turned it off; ``None``
    #: when nothing is saved.
    saved_auto_add: bool | None
    #: The last reconcile was refused because Bambuddy's auth is on.
    blocked: bool = False
    changed_at: AwareDatetime
    #: ScadBuddy's own setting ``filament_auto_add_unknown_rfid`` is off (#7156, R-7156-2),
    #: read in the same statement as the row.
    auto_add_off: bool = False

    @property
    def holds(self) -> bool:
        """Bambuddy's ``auto_add_unknown_rfid`` is held off: by the mode, or by the setting
        (R-7156-3)."""
        return self.override_mode or self.auto_add_off


class RfidStatus(BaseModel):
    """The answer on the RFID override mode (#6264) and the setting
    ``filament_auto_add_unknown_rfid`` (#7156), the two holds on Bambuddy's
    ``auto_add_unknown_rfid``. ``state`` is ``applied`` when either holds it and Bambuddy
    reads ``false``; ``pending`` when a write has not landed or Bambuddy could not be
    reached (``detail`` then names the error); ``blocked`` when Bambuddy refused the write
    because its auth is on; ``off`` when neither holds and nothing is saved."""

    override_mode: bool
    #: ScadBuddy's setting: ``False`` holds Bambuddy's value off as the mode does.
    auto_add_unknown_rfid: bool = True
    state: Literal["off", "applied", "pending", "blocked"]
    bambuddy_auto_add: bool | None = None
    saved_auto_add: bool | None = None
    detail: str | None = None


class RfidModeSet(BaseModel):
    """The body of ``PUT /filament/rfid/override-mode``."""

    enabled: bool


class RfidReconcileReport(BaseModel):
    """What one reconcile did: the mode, the tags and the override slots (``None`` when no
    Bambuddy is configured: the step is skipped)."""

    mode: RfidStatus
    tags: TagPushReport
    slots: SlotReport | None = None


class SpoolRef(BaseModel):
    """An SB spool by its id and the number a person reads."""

    id: UUID
    number: int
