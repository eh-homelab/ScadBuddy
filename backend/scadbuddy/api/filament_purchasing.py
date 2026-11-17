"""`/api/v1/filament` purchasing (#6266, epic #6260): suppliers, their products, price
points, the price sheet and a type's price history, over `filament/suppliers.py`. Money is
a `Decimal` and goes over the wire as a string."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Response, status

from scadbuddy.core.problems import ApiError
from scadbuddy.filament import purchasing_models as pm
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.suppliers import (
    PriceFromInvoiceError,
    ProductExistsError,
    ProductRefError,
    SupplierExistsError,
)

router = APIRouter(prefix="/filament", tags=["filament"])


def _no_supplier(supplier_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no supplier {supplier_id}")


def _no_product(product_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no supplier product {product_id}")


def _conflict(exc: ValueError) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, str(exc))


@router.get(
    "/suppliers",
    response_model=list[pm.Supplier],
    summary="List suppliers",
    description=(
        "The suppliers by name, each with its `product_count` (unarchived products). "
        "Archived suppliers only with `include_archived`."
    ),
)
async def list_suppliers(
    filament: FilamentDep, include_archived: bool = False
) -> list[pm.Supplier]:
    return await asyncio.to_thread(filament.suppliers.list, include_archived=include_archived)


@router.post(
    "/suppliers",
    response_model=pm.Supplier,
    status_code=status.HTTP_201_CREATED,
    summary="Add a supplier",
    description=(
        "A shop a person buys filament from: a `name`, and optionally a `website` (`http` or "
        "`https` only), a `customer_number` and a `note`. 409 when an unarchived supplier "
        "has the name, 422 for a bad field or a website that is not an http(s) URL."
    ),
)
async def create_supplier(filament: FilamentDep, body: pm.SupplierCreate) -> pm.Supplier:
    try:
        return await asyncio.to_thread(filament.suppliers.create, body)
    except SupplierExistsError as exc:
        raise _conflict(exc) from None


@router.get(
    "/suppliers/{supplier_id}",
    response_model=pm.Supplier,
    summary="Read a supplier",
    description="One supplier (archived too). 404 for none.",
)
async def get_supplier(filament: FilamentDep, supplier_id: UUID) -> pm.Supplier:
    found = await asyncio.to_thread(filament.suppliers.get, supplier_id)
    if found is None:
        raise _no_supplier(supplier_id)
    return found


@router.patch(
    "/suppliers/{supplier_id}",
    response_model=pm.Supplier,
    summary="Change a supplier",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the supplier off the lists and frees its name, `false` puts it back. 404 for no "
        "such supplier, 409 when another unarchived supplier has the name, 422 for a bad field."
    ),
)
async def patch_supplier(
    filament: FilamentDep, supplier_id: UUID, body: pm.SupplierPatch
) -> pm.Supplier:
    try:
        changed = await asyncio.to_thread(filament.suppliers.update, supplier_id, body)
    except SupplierExistsError as exc:
        raise _conflict(exc) from None
    if changed is None:
        raise _no_supplier(supplier_id)
    return changed


@router.get(
    "/suppliers/{supplier_id}/products",
    response_model=list[pm.SupplierProduct],
    summary="List a supplier's products",
    description=(
        "The supplier's products by SKU, each with its `newest_price`. Archived products "
        "only with `include_archived`. 404 for no such supplier."
    ),
)
async def list_products(
    filament: FilamentDep, supplier_id: UUID, include_archived: bool = False
) -> list[pm.SupplierProduct]:
    found = await asyncio.to_thread(
        filament.suppliers.products, supplier_id, include_archived=include_archived
    )
    if found is None:
        raise _no_supplier(supplier_id)
    return found


@router.post(
    "/suppliers/{supplier_id}/products",
    response_model=pm.SupplierProduct,
    status_code=status.HTTP_201_CREATED,
    summary="Add a supplier product",
    description=(
        "One SKU of the supplier for one spool type (`type_id`); `pack_size` is how many "
        "spools one SKU holds. 404 for no such supplier, 409 when the supplier has an "
        "unarchived product with the SKU, 422 for a bad field, a type that does not exist "
        "or a product URL that is not an http(s) URL."
    ),
)
async def create_product(
    filament: FilamentDep, supplier_id: UUID, body: pm.ProductCreate
) -> pm.SupplierProduct:
    try:
        made = await asyncio.to_thread(filament.suppliers.add_product, supplier_id, body)
    except ProductExistsError as exc:
        raise _conflict(exc) from None
    except ProductRefError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if made is None:
        raise _no_supplier(supplier_id)
    return made


@router.get(
    "/suppliers/{supplier_id}/price-sheet",
    response_model=list[pm.PriceSheetRow],
    summary="Read a supplier's price sheet",
    description=(
        "One row per unarchived product, by type code: the product, its type, its newest "
        "and previous price points, and `change_pct`, the change of the price per kg from "
        "the previous point to the newest, in percent to one decimal (`null` without two points). "
        "404 for no such supplier."
    ),
)
async def price_sheet(filament: FilamentDep, supplier_id: UUID) -> list[pm.PriceSheetRow]:
    sheet = await asyncio.to_thread(filament.suppliers.price_sheet, supplier_id)
    if sheet is None:
        raise _no_supplier(supplier_id)
    return sheet


@router.patch(
    "/products/{product_id}",
    response_model=pm.SupplierProduct,
    summary="Change a supplier product",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the product off the lists and frees its SKU, `false` puts it back. 404 for no such "
        "product, 409 when the supplier has another unarchived product with the SKU, 422 "
        "for a bad field or a type that does not exist."
    ),
)
async def patch_product(
    filament: FilamentDep, product_id: UUID, body: pm.ProductPatch
) -> pm.SupplierProduct:
    try:
        changed = await asyncio.to_thread(filament.suppliers.update_product, product_id, body)
    except ProductExistsError as exc:
        raise _conflict(exc) from None
    except ProductRefError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if changed is None:
        raise _no_product(product_id)
    return changed


@router.get(
    "/products/{product_id}/prices",
    response_model=list[pm.PricePoint],
    summary="List a product's price points",
    description=(
        "Newest `on_date` first. `price` is per pack with tax; `price_per_kg` is derived "
        "from the pack size and the type's label weight. 404 for no such product."
    ),
)
async def list_prices(filament: FilamentDep, product_id: UUID) -> list[pm.PricePoint]:
    if await asyncio.to_thread(filament.suppliers.product, product_id) is None:
        raise _no_product(product_id)
    return await asyncio.to_thread(filament.suppliers.prices, product_id)


@router.post(
    "/products/{product_id}/prices",
    response_model=pm.PricePoint,
    status_code=status.HTTP_201_CREATED,
    summary="Add a manual price point",
    description=(
        "A price per pack, with tax, as a decimal string. `currency` defaults to the shop's "
        "(`orders_currency`) and `on_date` to today (UTC). The point's source is `manual`. "
        "404 for no such product, 422 for a bad field."
    ),
)
async def add_price(
    filament: FilamentDep, product_id: UUID, body: pm.PricePointCreate
) -> pm.PricePoint:
    made = await asyncio.to_thread(filament.suppliers.add_price, product_id, body)
    if made is None:
        raise _no_product(product_id)
    return made


@router.delete(
    "/prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a manual price point",
    description=(
        "Only a point of source `manual`. 404 for no such point, 409 for one an invoice made "
        "(it goes with its invoice)."
    ),
)
async def delete_price(filament: FilamentDep, price_id: UUID) -> Response:
    try:
        gone = await asyncio.to_thread(filament.suppliers.delete_price, price_id)
    except PriceFromInvoiceError as exc:
        raise _conflict(exc) from None
    if not gone:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no price point {price_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/types/{type_id}/prices",
    response_model=list[pm.TypePricePoint],
    summary="Read a spool type's price history",
    description=(
        "Every price point of the type's supplier products, newest first, with the supplier "
        "and SKU. Points of archived products and suppliers stay, so the history stays true; "
        "`product_archived` and `supplier_archived` mark them. 404 for no such type."
    ),
)
async def type_prices(filament: FilamentDep, type_id: UUID) -> list[pm.TypePricePoint]:
    if await asyncio.to_thread(filament.types.get, type_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no spool type {type_id}")
    return await asyncio.to_thread(filament.suppliers.type_prices, type_id)
