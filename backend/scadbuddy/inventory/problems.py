"""The refusals of the inventory routes (#7067): an RFC 9457 problem whose ``type`` ends in
its ``code``. Since K1 (#6949) the kind, item and stock routes pass scadbuddy-core's
problems through unchanged (`core_client.py`), with its codes (R-K1-23; the full list is
Go's `routes/problems.go` `Codes`). `refused` maps the errors of the stores Python still
runs (offers, and the orders' stock calls until Task 16) to the same codes; a code shared
with the entity routes (`bad_property_schema`, and the entity reference codes) keeps the
entities' type, so one code has one type."""

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
from scadbuddy.inventory.offers import OfferExists, OfferNotFound, SupplierNotFound
from scadbuddy.inventory.stock import (
    BadQuantity,
    FilamentItemRefused,
    ItemNotCounted,
    ItemNotFound,
    LocationNotFound,
    LotEmpty,
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


def offer_not_found(offer_id: object) -> ApiError:
    return problem(_404, "offer_not_found", f"no offer {str(offer_id)!r}")


def supplier_not_found(supplier_id: object) -> ApiError:
    return problem(_404, "supplier_not_found", f"no supplier {str(supplier_id)!r}")


#: Each store error and its problem: (status, code, what the detail says before the
#: error's own text).
_CODES: tuple[tuple[type[Exception], int, str, str], ...] = (
    (ItemNotFound, _404, "item_not_found", "no item"),
    (OfferNotFound, _404, "offer_not_found", "no offer"),
    (LotNotFound, _404, "lot_not_found", "no lot"),
    (UnitNotFound, _404, "unit_not_found", "no unit"),
    (SupplierNotFound, _404, "supplier_not_found", "no supplier, or it is archived"),
    (LocationNotFound, _404, "location_not_found", "no location"),
    (
        OfferExists,
        _409,
        "offer_exists",
        "the item already has an unarchived offer with that SKU at the supplier",
    ),
    # Go's codes (R-K1-23), for the stock calls Python still makes until Task 16.
    (
        WrongTracking,
        _409,
        "wrong_counting",
        "a lot call on an item counted in units, or a unit call on one counted in lots",
    ),
    (
        ItemNotCounted,
        _409,
        "step_not_applicable",
        "the item's class has no such step: a service is not counted",
    ),
    (LotEmpty, _409, "lot_empty", "the named lot has no stock on hand"),
    (UnitInstalled, _409, "unit_installed", "the unit is fitted in another unit"),
    (SerialExists, _409, "serial_exists", "another unit of the item has that serial"),
    (
        FilamentItemRefused,
        _409,
        "filament_item_refused",
        "filament items and their offers wait for the filament phase (#7068)",
    ),
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
