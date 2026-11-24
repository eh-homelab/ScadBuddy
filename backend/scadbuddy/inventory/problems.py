"""The refusals of the inventory routes (#7067): an RFC 9457 problem whose ``type`` ends in
its ``code``. `refused` maps each store error to its problem; a code shared with the
entity routes (`bad_property_schema`, and the entity reference codes) keeps the entities'
type, so one code has one type."""

from __future__ import annotations

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.entities.problems import (
    bad_entity_ref,
    bad_property_schema,
    entity_not_found,
)
from scadbuddy.entities.problems import problem as entity_problem
from scadbuddy.entities.properties import BadPropertySchema
from scadbuddy.entities.refs import BadEntityRef, EntityRef
from scadbuddy.entities.registry import EntityNotFound, UnknownEntityKind
from scadbuddy.inventory.items import FilamentItemRefused, ItemExists, ItemNotFound
from scadbuddy.inventory.kinds import KindArchived, KindCycle, KindExists, KindInUse, KindNotFound
from scadbuddy.inventory.offers import OfferExists, OfferNotFound, SupplierNotFound
from scadbuddy.inventory.stock import (
    BadQuantity,
    ItemNotCounted,
    LocationNotFound,
    LotNotFound,
    SerialExists,
    UnitInstalled,
    UnitNotFound,
    WrongTracking,
)

PROBLEMS = "https://scadbuddy.dev/problems/inventory/"

_404 = status.HTTP_404_NOT_FOUND
_409 = status.HTTP_409_CONFLICT
_422 = status.HTTP_422_UNPROCESSABLE_CONTENT


def problem(status_code: int, code: str, detail: str) -> ApiError:
    return ApiError(status_code, detail, type_=PROBLEMS + code, code=code)


def kind_not_found(kind_id: str) -> ApiError:
    return problem(_404, "kind_not_found", f"no item kind {kind_id!r}")


def item_not_found(item_id: object) -> ApiError:
    return problem(_404, "item_not_found", f"no item {str(item_id)!r}")


def offer_not_found(offer_id: object) -> ApiError:
    return problem(_404, "offer_not_found", f"no offer {str(offer_id)!r}")


def lot_not_found(lot_id: object) -> ApiError:
    return problem(_404, "lot_not_found", f"no lot {str(lot_id)!r}")


def unit_not_found(unit_id: object) -> ApiError:
    return problem(_404, "unit_not_found", f"no unit {str(unit_id)!r}")


def supplier_not_found(supplier_id: object) -> ApiError:
    return problem(_404, "supplier_not_found", f"no supplier {str(supplier_id)!r}")


#: Each store error and its problem: (status, code, what the detail says before the
#: error's own text).
_CODES: tuple[tuple[type[Exception], int, str, str], ...] = (
    (KindNotFound, _404, "kind_not_found", "no item kind"),
    (ItemNotFound, _404, "item_not_found", "no item"),
    (OfferNotFound, _404, "offer_not_found", "no offer"),
    (LotNotFound, _404, "lot_not_found", "no lot"),
    (UnitNotFound, _404, "unit_not_found", "no unit"),
    (SupplierNotFound, _404, "supplier_not_found", "no supplier, or it is archived"),
    (LocationNotFound, _404, "location_not_found", "no location"),
    (KindExists, _409, "kind_exists", "an item kind has that id"),
    (KindInUse, _409, "kind_in_use", "the kind's items or built-in status forbid it"),
    (KindArchived, _409, "kind_archived", "the item kind is archived; restore it first"),
    (
        ItemExists,
        _409,
        "item_exists",
        "an unarchived item of the kind has that name in any case, or that import ref",
    ),
    (
        OfferExists,
        _409,
        "offer_exists",
        "the item already has an unarchived offer with that SKU at the supplier",
    ),
    (
        WrongTracking,
        _409,
        "wrong_tracking",
        "a lot call on an item counted in units, or a unit call on one counted in lots",
    ),
    (
        ItemNotCounted,
        _409,
        "item_not_counted",
        "the item's kind is not counted (tracking `none`): no lots, units or moves",
    ),
    (UnitInstalled, _409, "unit_installed", "the unit is fitted in another unit"),
    (SerialExists, _409, "serial_exists", "another unit of the item has that serial"),
    (
        FilamentItemRefused,
        _409,
        "filament_item_refused",
        "filament items and their offers wait for the filament phase (#7068)",
    ),
    (KindCycle, _422, "kind_cycle", "the parent is the kind or below it"),
    (
        BadQuantity,
        _422,
        "bad_quantity",
        "a quantity or a cost is out of range: at most six decimal places, whole for an"
        " `each` item, and small enough that a stock move's amount fits",
    ),
)


def refused(error: Exception) -> ApiError | None:
    """The problem for a store error the inventory routes know, or None."""
    if isinstance(error, BadPropertySchema):
        return bad_property_schema(error.reason)
    if isinstance(error, BadEntityRef):
        return bad_entity_ref()
    if isinstance(error, UnknownEntityKind):
        return entity_problem(_422, "unknown_entity_kind", f"no entity kind {str(error)!r}")
    if isinstance(error, EntityNotFound):
        return entity_not_found(EntityRef.parse(str(error)))
    for kind, status_code, code, what in _CODES:
        if isinstance(error, kind):
            return problem(status_code, code, f"{what}: {error}")
    return None
